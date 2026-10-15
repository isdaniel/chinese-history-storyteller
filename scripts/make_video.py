"""Step 4: 影片合成與降級階梯。

VIDEO_MODE=shots:用分鏡渲染 (render_video)。丟例外、回傳非 0、或沒產出 final.mp4 都算失敗,
發 Discord 通知後清掉這次渲染的殘留 (final.mp4、video_report.json、contact_sheet.png),降級成舊的圖片輪播 (build_video)。
其他模式:直接走舊的圖片輪播。

降級時每段旁白需要 img_{section:02d}.png,依序取第一個可用的:
  1. 已存在且非空的 img_NN.png;
  2. 該段第一個鏡頭的場景圖 (shots.json + timings.json 的 units);
  3. 備用圖 (generate_images._write_fallback)。
段落清單依序取自 timings.json 的 sections、shots.json/units 推得的段落、script.json 的 sections。
"""
import os
import shutil
import sys

import build_video
import render_video
from common import get_episode_dir, load_json, notify_discord, setup_logging
from generate_images import _write_fallback

log = setup_logging("make_video")

FALLBACK_ALERT_RATIO = 0.3
ALERT_REASON_CHARS = 500
RENDER_OUTPUTS = ("final.mp4", "video_report.json", "contact_sheet.png")


def _read_object(path):
    """讀 JSON 物件;檔案不存在、壞掉、或不是物件都回傳 None。"""
    try:
        data = load_json(path)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usable(path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _first_scene_images(ep_dir) -> dict[int, str]:
    """section -> 該段第一個鏡頭的場景圖檔名;shots.json / timings.json 缺漏或壞掉的部分略過。"""
    shots, timings = _read_object(ep_dir / "shots.json"), _read_object(ep_dir / "timings.json")
    if shots is None or timings is None:
        return {}
    section_of_unit = {u["id"]: u["section"] for u in _dicts(timings.get("units"))
                       if _int(u.get("id")) is not None and _int(u.get("section")) is not None}
    scene_image = {s["id"]: s["image"] for s in _dicts(shots.get("scenes"))
                   if isinstance(s.get("id"), str) and isinstance(s.get("image"), str)}
    first = {}
    for beat in _dicts(shots.get("beats")):
        unit_ids = beat.get("unit_ids")
        section = section_of_unit.get(_int(unit_ids[0])) if isinstance(unit_ids, list) and unit_ids else None
        image = scene_image.get(beat.get("scene")) if isinstance(beat.get("scene"), str) else None
        if section is not None and image is not None:
            first.setdefault(section, image)
    return first


def _declared_sections(ep_dir, name: str) -> list[int]:
    data = _read_object(ep_dir / name) or {}
    return sorted({_int(s.get("section_id")) for s in _dicts(data.get("sections"))} - {None})


def _section_ids(ep_dir, first_images: dict) -> list[int]:
    return (_declared_sections(ep_dir, "timings.json") or sorted(first_images)
            or _declared_sections(ep_dir, "script.json"))


def make_slideshow_inputs(ep_dir) -> list[str]:
    """降級用:補齊舊流程需要的 img_{section:02d}.png,回傳這次新建的檔名 (排序後)。"""
    first_images = _first_scene_images(ep_dir)
    if not first_images:
        log.error("shots.json / timings.json 無法使用:輪播圖只能沿用既有的 img_NN.png,缺的用備用圖")
    sections = _section_ids(ep_dir, first_images)
    if not sections:
        log.error("timings.json、shots.json、script.json 都讀不到任何段落,無法準備輪播圖")
    created = []
    for section in sections:
        target = ep_dir / f"img_{section:02d}.png"
        if _usable(target):
            continue
        scene_image = first_images.get(section)
        source = ep_dir / scene_image if scene_image else None
        if source is not None and _usable(source):
            try:
                shutil.copyfile(source, target)
            except OSError as exc:
                log.error("[%d] 複製場景圖 %s 失敗 (%s),改用備用圖", section, scene_image, exc)
                _write_fallback(target)
        else:
            log.error("[%d] 找不到可用的場景圖 (%s),改用備用圖", section, scene_image or "沒有對應的鏡頭")
            _write_fallback(target)
        created.append(target.name)
    return created


def _alert_on_video_fallbacks(episode_id: int, ep_dir):
    """影片報告的備援鏡頭超過比例就發 Discord;回傳報告裡可用的 beats (沒有報告或格式不對是 None),給規劃檢查當分母。"""
    report_path = ep_dir / "video_report.json"
    if not report_path.exists():
        return None
    report = _read_object(report_path)
    fallback = report.get("fallback_beats") if report else None
    beats = _int(report.get("beats")) if report else None
    if beats is not None and beats < 0:
        beats = None
    if not isinstance(fallback, list) or beats is None:
        log.warning("video_report.json 缺少 fallback_beats / beats 或格式不對,略過備援比例檢查")
        return beats
    ratio = len(fallback) / max(beats, 1)
    if ratio > FALLBACK_ALERT_RATIO:
        notify_discord(f"Episode {episode_id}: {len(fallback)}/{beats} 個鏡頭使用備援"
                       f" ({ratio:.0%}),請檢查圖片產生與渲染日誌。")
    return beats


def _alert_on_plan_fallbacks(episode_id: int, ep_dir, video_beats) -> None:
    """規劃保底段落的鏡頭 (LLM 分鏡多次失敗) 超過比例就發 Discord;分母優先用影片報告的 beats,否則用規劃自己的鏡頭總數。"""
    report_path = ep_dir / "plan_report.json"
    if not report_path.exists():
        return
    report = _read_object(report_path)
    sections = _dicts(report.get("sections")) if report else []
    counted = [(_int(s.get("beats")), s.get("fallback") is True) for s in sections]
    counted = [(beats, fallback) for beats, fallback in counted if beats is not None and beats >= 0]
    if not counted:
        log.warning("plan_report.json 缺少 sections 或格式不對,略過規劃保底檢查")
        return
    fallback_beats = sum(beats for beats, fallback in counted if fallback)
    total = video_beats or sum(beats for beats, _ in counted)
    if fallback_beats and fallback_beats / total > FALLBACK_ALERT_RATIO:
        notify_discord(f"Episode {episode_id}: {fallback_beats}/{total} 個鏡頭來自規劃保底"
                       f" ({fallback_beats / total:.0%}),LLM 分鏡規劃多次失敗,請檢查 plan_report.json。")


def check_fallback_ratio(episode_id: int, ep_dir) -> None:
    """備援鏡頭 (video_report.json) 與規劃保底鏡頭 (plan_report.json) 各自超過 FALLBACK_ALERT_RATIO 就各發一則 Discord;
    報告缺漏或格式不對只記 log,絕不拋例外。"""
    video_beats = _alert_on_video_fallbacks(episode_id, ep_dir)
    try:
        _alert_on_plan_fallbacks(episode_id, ep_dir, video_beats)
    except Exception as exc:   # 例如超大整數相除會 OverflowError;檢查失敗不能讓整個影片步驟失敗
        log.warning("plan_report.json 規劃保底檢查失敗,略過 (%s: %s)", type(exc).__name__, exc)


def _discard_render_outputs(ep_dir) -> None:
    for name in RENDER_OUTPUTS:
        try:
            (ep_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("無法刪除 %s: %s", name, exc)


def _render_shots(episode_id: int, ep_dir) -> None:
    code = render_video.main(episode_id)
    if code not in (0, None):
        raise RuntimeError(f"render_video 回傳非零結束碼 {code!r}")
    if not _usable(ep_dir / "final.mp4"):
        raise RuntimeError("render_video 結束了,但沒有產出 final.mp4")


def _degrade_to_slideshow(episode_id: int, ep_dir, exc: Exception) -> int:
    reason = f"{type(exc).__name__}: {exc}".strip()
    if len(reason) > ALERT_REASON_CHARS:
        reason = reason[:ALERT_REASON_CHARS - 1] + "…"
    log.error("分鏡渲染失敗 (%s),降級為圖片輪播", reason, exc_info=exc)
    notify_discord(f"Episode {episode_id}: 分鏡渲染失敗,已降級為圖片輪播。原因: {reason}")
    _discard_render_outputs(ep_dir)
    try:
        make_slideshow_inputs(ep_dir)
    except Exception:
        log.exception("整理輪播圖失敗,仍嘗試用現有的圖檔組裝")
    return build_video.main(episode_id)


def main(episode_id: int) -> int:
    if os.environ.get("VIDEO_MODE", "slideshow") != "shots":
        return build_video.main(episode_id)
    ep_dir = get_episode_dir(episode_id)
    if not (ep_dir / "shots.json").exists():
        raise RuntimeError("VIDEO_MODE=shots 但找不到 shots.json,請先執行 plan_shots.py")
    _discard_render_outputs(ep_dir)
    try:
        _render_shots(episode_id, ep_dir)
    except Exception as exc:
        return _degrade_to_slideshow(episode_id, ep_dir, exc)
    check_fallback_ratio(episode_id, ep_dir)
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
