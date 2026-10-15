"""分鏡內容規劃的純邏輯:場景預算、LLM 輸出驗證、保底計畫、字卡對時、shots.json 組裝。"""
import math

MOTIONS = ("push_in", "pull_out", "detail", "detail_hold", "pan_lr", "pan_rl", "tilt_up", "tilt_down")
FALLBACK_MOTIONS = ("push_in", "pan_lr", "pull_out", "pan_rl", "tilt_up", "tilt_down")
LABELS = ("年代", "人物", "地點", "數字")
MAX_BEATS_PER_SCENE = 4
MAX_OVERLAY_CHARS = 8
SECONDS_PER_OVERLAY = 25
MIN_OVERLAY_GAP = 4.0
LEAD_SEC = 0.15
MAX_CARD_SEC = 3.2
MIN_CARD_SEC = 0.8
FALLBACK_OFFSET = 0.4


def beat_ids_for(groups) -> list[str]:
    return [f"b{k + 1}" for k in range(len(groups))]


def overlay_cap(seconds: float) -> int:
    return max(1, math.ceil(seconds / SECONDS_PER_OVERLAY))


def allocate_scene_budget(beat_counts: list[int], max_images: int) -> list[int]:
    """把每集的圖片上限分配給各段旁白:每段至少 ceil(鏡頭數/4),剩餘依鏡頭數比例分配 (無條件捨去)。"""
    minimums = [math.ceil(c / MAX_BEATS_PER_SCENE) for c in beat_counts]
    if sum(minimums) > max_images:
        raise ValueError(f"MAX_IMAGES={max_images} is too small: at least {sum(minimums)} scenes are needed")
    spare = max_images - sum(minimums)
    total = sum(beat_counts) or 1
    return [m + (spare * c) // total for m, c in zip(minimums, beat_counts)]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_content(raw, beat_ids, max_scenes: int, max_overlays: int) -> list[str]:
    """檢查 LLM 的內容規劃;回傳錯誤訊息清單 (空清單代表通過)。絕不拋例外。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("scenes"), list) or not isinstance(raw.get("beats"), list):
        return ["output must be a JSON object with 'scenes' and 'beats' lists"]
    listed = []
    for scene in raw["scenes"]:
        if not isinstance(scene, dict) or not isinstance(scene.get("beats"), list):
            return ["every scene must be an object with a 'beats' list"]
        listed.extend(scene["beats"])
    if listed != list(beat_ids):
        return [f"scenes must list every beat exactly once, in order: expected {list(beat_ids)}, got {listed}"]
    errors = []
    if len(raw["scenes"]) > max_scenes:
        errors.append(f"too many scenes: {len(raw['scenes'])} > {max_scenes}")
    info = {b["id"]: b for b in raw["beats"] if isinstance(b, dict) and isinstance(b.get("id"), str)}
    overlays = 0
    for scene in raw["scenes"]:
        if not scene["beats"]:
            errors.append(f"scene {scene.get('id')} has no beats")
        elif len(scene["beats"]) > MAX_BEATS_PER_SCENE:
            errors.append(f"scene {scene.get('id')} has more than {MAX_BEATS_PER_SCENE} beats")
        if not _text(scene.get("image_prompt")):
            errors.append(f"scene {scene.get('id')} has no image_prompt")
        previous = None
        for beat_id in scene["beats"]:
            beat = info.get(beat_id)
            if beat is None:
                errors.append(f"missing beat entry {beat_id}")
                continue
            motion = beat.get("motion")
            if motion not in MOTIONS:
                errors.append(f"beat {beat_id} has invalid motion {motion}")
            if motion == previous:
                errors.append(f"beat {beat_id} repeats the previous motion inside scene {scene.get('id')}")
            previous = motion
            if not _text(beat.get("focus_hint")):
                errors.append(f"beat {beat_id} needs a focus_hint")
            overlay = beat.get("overlay")
            if overlay:
                overlays += 1
                text = overlay.get("text") if isinstance(overlay, dict) else None
                label = overlay.get("label") if isinstance(overlay, dict) else None
                if (not isinstance(text, str) or not text or len(text) > MAX_OVERLAY_CHARS
                        or label not in LABELS):
                    errors.append(f"beat {beat_id} overlay needs text of 1-{MAX_OVERLAY_CHARS} chars "
                                  f"and label in {'/'.join(LABELS)}")
    if overlays > max_overlays:
        errors.append(f"too many overlays: {overlays} > {max_overlays}")
    return errors


def fallback_content(beat_ids, image_prompt: str) -> dict:
    """保底:整段旁白一個場景,運動輪流,焦點置中,不加字卡。"""
    return {
        "scenes": [{"id": "fb", "beats": list(beat_ids), "image_prompt": image_prompt}],
        "beats": [{"id": bid, "motion": FALLBACK_MOTIONS[k % len(FALLBACK_MOTIONS)],
                   "focus_hint": "main subject", "overlay": None} for k, bid in enumerate(beat_ids)],
    }


def _word_matches(word_text: str, key: str) -> bool:
    return ((len(word_text) >= 2 and word_text in key)
            or (len(key) >= 2 and key[:2] in word_text)
            or (word_text.isdigit() and word_text in key))


def resolve_overlay(overlay: dict, t0: float, t1: float, words: list[dict]):
    """把字卡對到旁白說出該詞的時間;回傳 {text,label,at,dur} (絕對秒數) 或 None (放不下)。"""
    key = overlay["text"]
    hit = next((w for w in words if t0 - 0.05 <= w["start"] <= t1 and _word_matches(w["text"], key)), None)
    start = max(t0 + 0.1, hit["start"] - LEAD_SEC) if hit else t0 + FALLBACK_OFFSET
    start = max(t0 + 0.05, min(start, t1 - 1.2))
    duration = min(MAX_CARD_SEC, t1 - start - 0.05)
    if duration < MIN_CARD_SEC:
        return None
    return {"text": key, "label": overlay["label"], "at": round(start, 2), "dur": round(duration, 2)}


def assemble_shots(style, units, beat_groups, content, focus, words, max_overlays) -> dict:
    """組出 shots.json。content 的場景 id 必須已是全集唯一;focus: {beat_id: [x, y]}。"""
    beat_ids = beat_ids_for(beat_groups)
    info = {b["id"]: b for b in content["beats"]}
    scene_of = {bid: scene["id"] for scene in content["scenes"] for bid in scene["beats"]}
    beats = []
    for bid, unit_ids in zip(beat_ids, beat_groups):
        t0, t1 = units[unit_ids[0]]["start"], units[unit_ids[-1]]["end"]
        entry = info[bid]
        overlay = resolve_overlay(entry["overlay"], t0, t1, words) if entry.get("overlay") else None
        beats.append({"id": bid, "unit_ids": list(unit_ids), "start": t0, "end": t1, "scene": scene_of[bid],
                      "motion": entry["motion"], "focus": list(focus.get(bid, [0.5, 0.5])),
                      "focus_hint": entry.get("focus_hint", ""), "overlays": [overlay] if overlay else []})
    candidates = sorted((o["at"], k) for k, beat in enumerate(beats) for o in beat["overlays"])
    kept, last = set(), -1e9
    for at, k in candidates:
        if len(kept) >= max_overlays or at - last < MIN_OVERLAY_GAP:
            continue
        kept.add((at, k))
        last = at
    for k, beat in enumerate(beats):
        beat["overlays"] = [o for o in beat["overlays"] if (o["at"], k) in kept]
    scenes = [{"id": s["id"], "image_prompt": s["image_prompt"], "image": f"img_{s['id']}.png"}
              for s in content["scenes"]]
    return {"style": style, "scenes": scenes, "beats": beats}
