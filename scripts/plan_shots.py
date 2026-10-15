"""Stage: 規劃分鏡 → shots.json。

程式負責切鏡頭 (beats.py),LLM 只決定每個鏡頭的畫面內容 (場景描述、運鏡、聚焦物、字卡),
輸出由 shot_plan.validate_content 驗證,失敗就帶著錯誤重試;整段都失敗則用保底計畫,整集不會中斷。
"""
import math
import os
import sys

import beats as beats_mod
import shot_plan
import shots_guard
from common import env, get_episode_dir, load_json, save_json, setup_logging
from llm_client import chat_json, make_client
from visual_style import current_style

log = setup_logging("plan_shots")

MAX_ATTEMPTS = 4


def max_images() -> int:
    return int(env("MAX_IMAGES", "24"))


def segment(units):
    try:
        return beats_mod.segment_beats(units)
    except beats_mod.BeatSegmentationError as exc:
        log.warning("beat segmentation failed (%s); using one beat per unit", exc)
        return beats_mod.one_beat_per_unit(units), None


def build_prompt(lines: list[str], bible: str, max_scenes: int, max_overlays: int) -> str:
    joined = "\n".join(lines)
    return f"""你是紀錄片分鏡師。下面是已經切好的鏡頭清單 (鏡頭編號、秒數、旁白)。鏡頭的切點與長度已經固定,你不能更動,只負責決定每個鏡頭的畫面內容。
【視覺設定】{bible or '(無)'}
【鏡頭清單】
{joined}

規則:
1. scenes:把相鄰的鏡頭分組成場景 (scene),每個場景共用一張插畫,每個場景包含 1~{shot_plan.MAX_BEATS_PER_SCENE} 個鏡頭,全部最多 {max_scenes} 個場景,每個鏡頭只屬於一個場景,順序不變。
2. 每個 scene 給一個英文 image_prompt:描述場景與構圖 (主體放在畫面左/中/右三分之一處),時代細節要正確 (依視覺設定),畫面中不要有任何文字、印章、題款。風格後綴會自動加上,不要寫風格詞。
3. 內容必須對得上旁白:每個鏡頭的 focus_hint (用英文描述要聚焦的具體物體) 必須明確出現在其 scene 的 image_prompt 描述中;如果旁白在講的事物在該場景看不到,就換到能呈現它的場景,不可硬塞。
4. 每個鏡頭指定 motion,必須是以下之一:{', '.join(shot_plan.MOTIONS)}。同一場景內相鄰的鏡頭不可重複同一個 motion,要有『從全景到細節』的變化;detail 與 detail_hold 的 focus_hint 要是具體的小物體。
5. overlay:只在旁白第一次提到重要的年代、人名、地名、數字時才加,每個鏡頭最多 1 個,這份清單最多 {max_overlays} 個。text 不超過 {shot_plan.MAX_OVERLAY_CHARS} 個字 (例如 "1974年"、"秦始皇"、"驪山"、"數十萬人"),label 只能是 {'、'.join(shot_plan.LABELS)} 之一;不需要則設為 null。
輸出 JSON:
{{"scenes":[{{"id":"A","beats":["b1","b2"],"image_prompt":"..."}}],
 "beats":[{{"id":"b1","motion":"push_in","focus_hint":"...","overlay":{{"text":"1974年","label":"年代"}} }}]}}"""


def plan_section(client, lines, beat_ids, bible, max_scenes, max_overlays):
    """回傳 (content | None, 使用次數, 最後的錯誤清單)。"""
    base = build_prompt(lines, bible, max_scenes, max_overlays)
    prompt, errors = base, []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = chat_json(client, prompt)
        except Exception as exc:
            errors = [f"call failed: {exc}"]
            log.warning("section planning attempt %d failed: %s", attempt, exc)
            continue
        errors = shot_plan.validate_content(raw, beat_ids, max_scenes, max_overlays)
        if not errors:
            return raw, attempt, []
        log.warning("section planning attempt %d invalid: %s", attempt, errors)
        prompt = base + "\n\n上一次輸出的錯誤,請修正:\n" + "\n".join(errors)
    return None, MAX_ATTEMPTS, errors


def make_bible(client, script: dict) -> str:
    excerpt = "".join(s["narration"] for s in script["sections"])[:1500]
    prompt = ("根據以下旁白,寫一段英文『視覺設定』(不超過 80 字),讓整集每張插畫維持一致:"
              "時代與年份、人物服飾、建築與器物、整體色調。\n"
              f"【題材】{script.get('title', '')}\n【旁白節錄】{excerpt}\n"
              '輸出 JSON: {"bible": "..."}')
    try:
        return str(chat_json(client, prompt).get("bible", "")).strip()[:600]
    except Exception as exc:
        log.warning("visual bible failed, continuing without it: %s", exc)
        return ""


def _fallback_prompt(section: dict) -> str:
    return section.get("image_prompt") or "A historical scene illustrating: " + section["narration"][:60]


def scene_budgets(beat_counts, image_limit):
    """MAX_IMAGES 是軟上限:最少需要的場景數超過上限但不超過 1.5 倍時照常規劃 (多留 2 個場景的餘裕),
    再超過才整集改用保底計畫。回傳 (各段場景上限 | None, 寫進 plan_report 的 budget)。"""
    needed = sum(math.ceil(c / shot_plan.MAX_BEATS_PER_SCENE) for c in beat_counts)
    ceiling = image_limit * 3 // 2
    exceeded = needed > image_limit
    if needed > ceiling:
        log.warning("MAX_IMAGES=%d is too small: at least %d scenes are needed; "
                    "every section falls back to a single scene", image_limit, needed)
        used = None
    elif exceeded:
        used = min(ceiling, needed + 2)
        log.warning("MAX_IMAGES=%d exceeded by %d (%d scenes needed); planning with %d scenes",
                    image_limit, needed - image_limit, needed, used)
    else:
        used = image_limit
    budgets = None if used is None else shot_plan.allocate_scene_budget(beat_counts, used)
    return budgets, {"limit": image_limit, "needed": needed, "used": used, "exceeded": exceeded}


def plan_episode(client, script, timings, style, image_limit):
    units, words = timings["units"], timings["words"]
    groups, bounds = segment(units)
    beat_ids = shot_plan.beat_ids_for(groups)
    by_section, order = {}, []
    for bid, group in zip(beat_ids, groups):
        section = units[group[0]]["section"]
        if section not in by_section:
            by_section[section] = []
            order.append(section)
        by_section[section].append((bid, group))
    budgets, budget = scene_budgets([len(by_section[s]) for s in order], image_limit)
    bible = make_bible(client, script)
    section_info = {s["section_id"]: s for s in script["sections"]}
    scenes, beat_entries, section_reports = [], [], []
    for k, section in enumerate(order):
        pairs = by_section[section]
        ids = [bid for bid, _ in pairs]
        lines = [f"{bid} [{units[g[-1]]['end'] - units[g[0]]['start']:.1f}s] "
                 f"{''.join(units[i]['text'] for i in g)}" for bid, g in pairs]
        seconds = units[pairs[-1][1][-1]]["end"] - units[pairs[0][1][0]]["start"]
        content, attempts, errors = None, 0, ["scene budget too small"]
        if budgets is not None:
            content, attempts, errors = plan_section(client, lines, ids, bible, budgets[k],
                                                     shot_plan.overlay_cap(seconds))
        fallback = content is None
        if fallback:
            content = shot_plan.fallback_content(ids, _fallback_prompt(section_info[section]))
        for scene in content["scenes"]:
            new_id = f"s{len(scenes) + 1}"
            prompt = scene["image_prompt"] + (f" Setting notes: {bible}" if bible else "")
            scenes.append({"id": new_id, "beats": scene["beats"], "image_prompt": prompt})
        wanted = set(ids)
        beat_entries.extend(b for b in content["beats"]
                            if isinstance(b, dict) and isinstance(b.get("id"), str) and b["id"] in wanted)
        section_reports.append({"section": section, "beats": len(ids), "attempts": attempts,
                                "fallback": fallback, "errors": errors if fallback else []})
    shots = shot_plan.assemble_shots(style, units, groups, {"scenes": scenes, "beats": beat_entries}, {},
                                     words, shot_plan.overlay_cap(units[-1]["end"]))
    report = {"beats": len(groups), "scenes": len(scenes), "bounds": bounds, "bible": bible,
              "overlays": sum(len(b["overlays"]) for b in shots["beats"]), "sections": section_reports,
              "budget": budget}
    return shots, report


def _plan_once(ep_dir) -> None:
    script = load_json(ep_dir / "script.json")
    timings = load_json(ep_dir / "timings.json")
    if not timings.get("units"):
        raise RuntimeError("timings.json has no units; run synthesize_speech.py first")
    shots, report = plan_episode(make_client(), script, timings, current_style(), max_images())
    save_json(ep_dir / "shots.json", shots)
    save_json(ep_dir / "plan_report.json", report)
    log.info("planned %d beats in %d scenes (%d overlays, %d fallback sections)", report["beats"],
             report["scenes"], report["overlays"], sum(s["fallback"] for s in report["sections"]))


def main(episode_id: int) -> int:
    if os.environ.get("VIDEO_MODE", "slideshow") != "shots":
        log.info("VIDEO_MODE is not 'shots'; skipping shot planning")
        return 0
    ep_dir = get_episode_dir(episode_id)
    shots_guard.clear_marker(ep_dir)   # 規劃是第一個分鏡階段:重新規劃就是重新開始,清掉上次的降級標記
    shots_guard.run_stage("plan", episode_id, ep_dir, lambda: _plan_once(ep_dir))
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
