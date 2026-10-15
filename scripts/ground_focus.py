"""Stage: 聚焦定位。視覺模型依每個鏡頭的 focus_hint 回報物體在圖中的位置,寫回 shots.json 的 focus。"""
import base64
import math
import os
import sys

from common import get_episode_dir, load_json, save_json, setup_logging
from llm_client import chat_json, make_client

log = setup_logging("ground_focus")


def clamp(value) -> float:
    if isinstance(value, bool):
        raise TypeError("bool is not a coordinate")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite coordinate")
    return min(max(number, 0.05), 0.95)


def parse_targets(data, beat_ids) -> dict:
    """從模型輸出取出 {beat_id: [x, y]};格式不對的項目忽略,座標限制在 [0.05, 0.95]。"""
    targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(targets, list):
        return {}
    focus = {}
    for target in targets:
        try:
            if target["beat"] in beat_ids:
                focus[target["beat"]] = [clamp(target["x"]), clamp(target["y"])]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return focus


def ground_scene(client, image_path, beats) -> dict:
    """回傳 {beat_id: [x, y]};任何失敗都回傳 {} (該場景的鏡頭維持置中)。"""
    items = "\n".join(f"- {b['id']}: {b.get('focus_hint') or 'main subject'}" for b in beats)
    prompt = ("這是一張插畫。請針對下列每個描述,回報該物體在圖中『中心點』的正規化座標 "
              "(x 由左到右 0~1,y 由上到下 0~1)。若圖中沒有該物體,回報最相關區域的中心。\n"
              f"{items}\n輸出 JSON: " + '{"targets":[{"beat":"b1","x":0.3,"y":0.5}]}')
    try:
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        data = chat_json(client, prompt, image_b64=b64)
        return parse_targets(data, {b["id"] for b in beats})
    except Exception as exc:
        log.warning("grounding failed for %s: %s", image_path.name, exc)
        return {}


def apply_focus(shots: dict, focus: dict) -> None:
    for beat in shots["beats"]:
        if beat["id"] in focus:
            beat["focus"] = focus[beat["id"]]


def run(client, shots: dict, ep_dir, skip_scenes=()) -> dict:
    grounded, missing = 0, []
    for scene in shots["scenes"]:
        beats = [b for b in shots["beats"] if b["scene"] == scene["id"]]
        if scene["id"] in skip_scenes:
            missing.extend(b["id"] for b in beats)
            continue
        focus = ground_scene(client, ep_dir / scene["image"], beats)
        apply_focus(shots, focus)
        grounded += len(focus)
        missing.extend(b["id"] for b in beats if b["id"] not in focus)
    return {"grounded": grounded, "missing": missing}


def main(episode_id: int) -> int:
    if os.environ.get("VIDEO_MODE", "slideshow") != "shots":
        log.info("VIDEO_MODE is not 'shots'; skipping focus grounding")
        return 0
    ep_dir = get_episode_dir(episode_id)
    shots = load_json(ep_dir / "shots.json")
    try:
        image_report = load_json(ep_dir / "image_report.json")
        skip = {s["scene"] for s in image_report["scenes"] if s["status"] == "fallback"}
    except (OSError, ValueError, KeyError, TypeError):
        skip = set()
    report = run(make_client(), shots, ep_dir, skip)
    save_json(ep_dir / "shots.json", shots)
    save_json(ep_dir / "ground_report.json", report)
    log.info("聚焦定位:%d 個鏡頭已定位,%d 個維持置中", report["grounded"], len(report["missing"]))
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
