"""Step 2: 用 Azure gpt-image-2 為每段生成插畫。

輸入: output/ep{id}/script.json
輸出: output/ep{id}/img_01.png ... img_08.png
"""
import base64
import os
import random
import struct
import sys
import time
import zlib
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

from common import env, get_episode_dir, load_json, setup_logging
from retry import with_backoff

log = setup_logging("generate_images")

STYLE_SUFFIX = (
    ", traditional Chinese ink painting style, scroll painting aesthetic, "
    "muted earth tones, atmospheric, no text, no characters writing, "
    "cinematic composition, 16:9 aspect"
)

# gpt-image-2 deployment 是 GlobalStandard 2 RPM,30s/req 是理論下限。
# 加 5~10s jitter 避免多個 request 對齊 RPM 視窗邊界。
THROTTLE_BASE_SEC = 30
THROTTLE_JITTER_SEC = 10


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


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    # max_retries=5: SDK 會 honor 429 的 Retry-After header 自動指數退避
    # timeout=180:單次呼叫上限,避免 hang
    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT"),
        max_retries=5,
        timeout=180.0,
    )
    deployment = env("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2")

    fallback_count = 0
    for section in script["sections"]:
        idx = section["section_id"]
        out_path = ep_dir / f"img_{idx:02d}.png"
        if out_path.exists():
            log.info("[%d] 已存在,跳過", idx)
            continue

        prompt = section["image_prompt"] + STYLE_SUFFIX
        log.info("[%d] 生成插畫…", idx)
        try:
            # 雙層保險:SDK 內 max_retries=5 已處理 ~1 分鐘內的 429/5xx;
            # 這層再加 3 次外層重試 (base 60s, max 240s),覆蓋 RPM 視窗或區域短暫斷線
            # (Azure gpt-image-2 GlobalStandard 2 RPM,單次失敗後 60~240s 重試剛好錯開視窗)
            def _do_generate():
                return client.images.generate(
                    model=deployment,
                    prompt=prompt,
                    n=1,
                    size="1536x1024",
                    quality="medium",
                )

            resp = with_backoff(
                _do_generate,
                max_attempts=3,
                base_sec=60.0,
                max_sec=240.0,
                jitter_sec=15.0,
                op_name=f"image gen [{idx}]",
            )
            b64 = resp.data[0].b64_json
            img_bytes = base64.b64decode(b64)
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


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
