"""一次性工具:生成 podcast cover.jpg (3000x3000 符合 Apple Podcasts 規格)。

跑法 (PowerShell):
  cd infra
  $env:AZURE_OPENAI_ENDPOINT = (terraform output -raw azure_openai_endpoint)
  $env:AZURE_OPENAI_IMAGE_DEPLOYMENT = (terraform output -raw azure_openai_image_deployment)
  $env:AZURE_OPENAI_API_VERSION = "2024-10-21"
  cd ..
  python scripts/generate_cover.py
"""
import base64
import os
import sys
from io import BytesIO
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "cover.jpg"

PROMPT = (
    "A square podcast cover for a Chinese history storytelling channel. "
    "Center: a stylized Chinese ink-painting silhouette of an ancient scholar "
    "holding a bamboo scroll, looking toward distant mountains. "
    "Background: misty mountains and faint calligraphy strokes. "
    "Top of image: large traditional Chinese characters '中文歷史說書' "
    "written in elegant brush calligraphy style. "
    "Color palette: deep ink black, antique parchment beige, subtle vermilion seal red accent. "
    "Style: minimalist, museum-quality Chinese scroll painting aesthetic, "
    "atmospheric, no Western fonts, no people's faces visible, "
    "podcast cover layout, professional, timeless."
)


def main() -> int:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )
    deployment = os.environ.get("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2")

    print(f"Generating 1024x1024 cover via {deployment} (high quality)...")
    resp = client.images.generate(
        model=deployment,
        prompt=PROMPT,
        n=1,
        size="1024x1024",
        quality="high",   # cover 一次性,值得 high quality
    )
    b64 = resp.data[0].b64_json
    raw = base64.b64decode(b64)
    print(f"Got {len(raw)//1024} KB PNG")

    print("Upscaling to 3000x3000 (Apple Podcasts requirement)...")
    img = Image.open(BytesIO(raw)).convert("RGB")
    img_3000 = img.resize((3000, 3000), Image.LANCZOS)

    img_3000.save(OUT_PATH, "JPEG", quality=92, optimize=True)
    size_kb = OUT_PATH.stat().st_size // 1024
    print(f"OK -> {OUT_PATH} ({img_3000.size[0]}x{img_3000.size[1]}, {size_kb} KB, RGB)")
    if size_kb > 500:
        print(f"WARN: file is {size_kb} KB; Apple recommends <500 KB. Consider quality=85.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
