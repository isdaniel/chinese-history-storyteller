"""Step 2: 用 Azure gpt-image-2 為每段生成插畫。

輸入: output/ep{id}/script.json
輸出: output/ep{id}/img_01.png ... img_08.png
"""
import base64
import os
import sys
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

from common import env, get_episode_dir, load_json, setup_logging

log = setup_logging("generate_images")

STYLE_SUFFIX = (
    ", traditional Chinese ink painting style, scroll painting aesthetic, "
    "muted earth tones, atmospheric, no text, no characters writing, "
    "cinematic composition, 16:9 aspect"
)


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT"),
    )
    deployment = env("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2")

    for section in script["sections"]:
        idx = section["section_id"]
        out_path = ep_dir / f"img_{idx:02d}.png"
        if out_path.exists():
            log.info("[%d] 已存在,跳過", idx)
            continue

        prompt = section["image_prompt"] + STYLE_SUFFIX
        log.info("[%d] 生成插畫…", idx)
        try:
            resp = client.images.generate(
                model=deployment,
                prompt=prompt,
                n=1,
                size="1536x1024",
                quality="medium",
            )
            b64 = resp.data[0].b64_json
            img_bytes = base64.b64decode(b64)
            out_path.write_bytes(img_bytes)
            log.info("[%d] 已存 %s (%d KB)", idx, out_path.name, len(img_bytes) // 1024)
        except Exception as e:
            log.error("[%d] 失敗: %s — 改用 fallback", idx, e)
            fallback = Path(__file__).parent.parent / "assets" / "fallback.png"
            if fallback.exists():
                out_path.write_bytes(fallback.read_bytes())
            else:
                raise
        time.sleep(2)

    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
