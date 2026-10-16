"""Step 2: 用 Azure gpt-image-2 產生插畫。

VIDEO_MODE=slideshow (預設):每段旁白一張圖 (img_01.png ...),與舊版相同。
VIDEO_MODE=shots:依 shots.json 的場景各產一張 (img_s1.png ...);張數已由規劃階段的 MAX_IMAGES 限制。
"""
import base64
import math
import os
import random
import struct
import sys
import time
import zlib
from pathlib import Path

from common import env, get_episode_dir, load_json, save_json, setup_logging
from llm_client import make_client
import shots_guard
from retry import _is_retryable_status, with_backoff
from visual_style import current_style, style_suffix

log = setup_logging("generate_images")

IMAGE_SIZE = "1536x1024"

# gpt-image-2 deployment 是 GlobalStandard 2 RPM,30s/req 是理論下限。
# 加 5~10s jitter 避免多個 request 對齊 RPM 視窗邊界。
THROTTLE_BASE_SEC = 30
THROTTLE_JITTER_SEC = 10

MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_IMAGE_STAGE_MAX_SEC = 4500


def _write_placeholder_png(path: Path) -> None:
    """產生 1536x1024 純色 PNG 作為 fallback,避免整集 pipeline 中斷。"""
    width, height = 1536, 1024
    r, g, b = 60, 50, 40
    raw = b"".join(
        b"\x00" + bytes((r, g, b)) * width for _ in range(height)
    )
    compressed = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _write_fallback(out_path: Path) -> None:
    fallback = Path(__file__).parent.parent / "assets" / "fallback.png"
    if fallback.exists():
        out_path.write_bytes(fallback.read_bytes())
    else:
        _write_placeholder_png(out_path)


def generate_image_bytes(client, deployment: str, prompt: str, label) -> bytes:
    """雙層保險:SDK 內 max_retries 已處理 ~1 分鐘內的 429/5xx;這層再加 3 次外層重試
    (base 60s, max 240s),覆蓋 RPM 視窗或區域短暫斷線。"""
    def _do():
        return client.images.generate(model=deployment, prompt=prompt, n=1, size=IMAGE_SIZE, quality="medium")

    resp = with_backoff(_do, max_attempts=3, base_sec=60.0, max_sec=240.0, jitter_sec=15.0,
                        op_name=f"image gen [{label}]")
    return base64.b64decode(resp.data[0].b64_json)


def generate_scene_images(client, deployment, shots, ep_dir: Path, style: str, *, sleep,
                          write_fallback=_write_fallback, max_stage_sec=DEFAULT_IMAGE_STAGE_MAX_SEC,
                          clock=time.monotonic) -> dict:
    """依 shots.json 的場景產圖。單張失敗時沿用前一張圖 (第一張失敗則用備用圖),整集不中斷。"""
    suffix = style_suffix(style)
    report = {"generated": 0, "fallback": 0, "skipped": 0, "scenes": []}
    previous = None
    scenes = shots["scenes"]
    started = clock()
    streak = 0  # 連續暫時性失敗 (429/5xx/無狀態碼) 的張數;成功或非暫時性失敗 (如 400) 歸零,已存在的場景不影響

    def stop_reason():
        if streak >= MAX_CONSECUTIVE_FAILURES:
            return "consecutive_failures"
        if clock() - started >= max_stage_sec:
            return "time_limit"
        return None

    stopped = None
    for k, scene in enumerate(scenes):
        out = ep_dir / scene["image"]
        if out.exists():
            report["skipped"] += 1
            report["scenes"].append({"scene": scene["id"], "status": "skipped"})
            previous = out
            continue
        if stopped is None:
            stopped = stop_reason()
            if stopped == "consecutive_failures":
                log.error("連續 %d 張圖片生成失敗,其餘場景改用備援圖,不再呼叫圖片服務", streak)
            elif stopped == "time_limit":
                log.error("圖片階段已達 %g 秒上限,其餘場景改用備援圖,不再呼叫圖片服務", max_stage_sec)
        status = "fallback"
        if stopped is None:
            log.info("[%s] 生成插畫…", scene["id"])
            try:
                out.write_bytes(generate_image_bytes(client, deployment, scene["image_prompt"] + suffix, scene["id"]))
                status = "generated"
                streak = 0
            except Exception as exc:
                log.error("[%s] 重試後仍失敗: %s — 改用備援圖", scene["id"], exc)
                streak = streak + 1 if _is_retryable_status(exc) else 0
        if status == "fallback":
            if previous is not None:
                out.write_bytes(previous.read_bytes())
            else:
                write_fallback(out)
        report["generated" if status == "generated" else "fallback"] += 1
        report["scenes"].append({"scene": scene["id"], "status": status})
        previous = out
        if k + 1 < len(scenes) and stop_reason() is None:
            sleep()
    report["stopped_early"] = stopped
    return report


def discard_fallback_images(shots, ep_dir: Path) -> list[str]:
    """重跑時刪除上次以備援圖頂替的場景圖片,讓這些場景重新生成;回傳這些場景的 id。"""
    try:
        report = load_json(ep_dir / "image_report.json")
        failed = {entry["scene"] for entry in report["scenes"] if entry["status"] == "fallback"}
    except (OSError, ValueError, KeyError, TypeError):
        return []
    discarded = []
    for scene in shots["scenes"]:
        if scene["id"] in failed:
            (ep_dir / scene["image"]).unlink(missing_ok=True)
            discarded.append(scene["id"])
    return discarded


def main_slideshow(episode_id: int, client, deployment: str) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")
    suffix = style_suffix(current_style())
    fallback_count = 0
    for section in script["sections"]:
        idx = section["section_id"]
        out_path = ep_dir / f"img_{idx:02d}.png"
        if out_path.exists():
            log.info("[%d] 已存在,跳過", idx)
            continue
        log.info("[%d] 生成插畫…", idx)
        try:
            img_bytes = generate_image_bytes(client, deployment, section["image_prompt"] + suffix, idx)
            out_path.write_bytes(img_bytes)
            log.info("[%d] 已存 %s (%d KB)", idx, out_path.name, len(img_bytes) // 1024)
        except Exception as e:
            log.error("[%d] 兩層重試後仍失敗: %s — 改用 fallback", idx, e)
            _write_fallback(out_path)
            fallback_count += 1
        sleep_sec = THROTTLE_BASE_SEC + random.uniform(0, THROTTLE_JITTER_SEC)
        log.info("[%d] throttle sleep %.1fs", idx, sleep_sec)
        time.sleep(sleep_sec)

    total = len(script["sections"])
    if fallback_count > 0:
        log.warning(
            "[FALLBACK ALERT] %d/%d 張插畫使用 fallback 圖片,影片將含預設棕色背景",
            fallback_count, total,
        )
    else:
        log.info("全部 %d 張插畫生成成功", total)
    return 0


def stage_max_sec() -> float:
    try:
        value = float(env("IMAGE_STAGE_MAX_SEC", "", required=False))
    except ValueError:
        return DEFAULT_IMAGE_STAGE_MAX_SEC
    return value if value > 0 and math.isfinite(value) else DEFAULT_IMAGE_STAGE_MAX_SEC


def _shots_images(episode_id: int, ep_dir: Path) -> None:
    client = make_client(timeout=180.0)
    deployment = env("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2")
    shots = load_json(ep_dir / "shots.json")
    retried = discard_fallback_images(shots, ep_dir)
    if retried:
        log.info("上次以備援圖頂替的場景有 %d 個,將重新生成", len(retried))
    base = float(env("IMAGE_THROTTLE_SEC", str(THROTTLE_BASE_SEC)))

    def throttle():
        time.sleep(base + random.uniform(0, THROTTLE_JITTER_SEC))

    report = generate_scene_images(client, deployment, shots, ep_dir, shots["style"], sleep=throttle,
                                   max_stage_sec=stage_max_sec())
    save_json(ep_dir / "image_report.json", report)
    log.info("圖片完成:生成 %d、跳過 %d、備援 %d", report["generated"], report["skipped"], report["fallback"])


def main(episode_id: int) -> int:
    shots_mode = os.environ.get("VIDEO_MODE", "slideshow") == "shots"
    ep_dir = get_episode_dir(episode_id) if shots_mode else None
    if shots_mode and shots_guard.is_degraded(ep_dir):
        log.warning("分鏡規劃已降級,改用舊流程為每段旁白各產一張圖")
        shots_mode = False
    if not shots_mode:
        client = make_client(timeout=180.0)
        return main_slideshow(episode_id, client, env("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2"))
    if not shots_guard.run_stage("images", episode_id, ep_dir, lambda: _shots_images(episode_id, ep_dir)):
        # 已降級。一張場景圖都沒有 (通常是還沒開始產圖就失敗) 時改走舊流程每段一張圖,否則整支輪播會是同一張備用圖;
        # 已經有場景圖時,make_video 會直接沿用它們。
        if not any(ep_dir.glob("img_s*.png")):
            log.warning("分鏡產圖已降級且沒有任何場景圖,改用舊流程為每段旁白各產一張圖")
            return main_slideshow(episode_id, make_client(timeout=180.0),
                                  env("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2"))
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
