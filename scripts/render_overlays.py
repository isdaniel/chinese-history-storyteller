"""繪製字卡、字幕與畫面明暗處理 (Pillow / numpy)。"""
import logging
import math
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("render_overlays")

ACCENT = (178, 54, 40)
SUBTITLE_MAX_WIDTH = 1700

# (路徑, .ttc 內的字型索引)。Noto Sans CJK 的 .ttc 內順序為 JP, KR, SC, TC, HK,TC = 3;微軟正黑體為 0。
BOLD_CANDIDATES = [("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 3), (r"C:\Windows\Fonts\msjhbd.ttc", 0)]
REGULAR_CANDIDATES = [("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 3), (r"C:\Windows\Fonts\msjh.ttc", 0)]


def find_font(bold: bool) -> tuple[str, int]:
    """SUBTITLE_FONT_FILE / SUBTITLE_FONT_INDEX 優先;否則依序找內建的候選路徑。"""
    configured = os.environ.get("SUBTITLE_FONT_FILE", "").strip()
    if configured:
        raw_index = os.environ.get("SUBTITLE_FONT_INDEX", "").strip()
        try:
            return configured, int(raw_index or "0")
        except ValueError:
            raise ValueError(f"SUBTITLE_FONT_INDEX must be an integer, got {raw_index!r}") from None
    for path, index in (BOLD_CANDIDATES if bold else REGULAR_CANDIDATES):
        if Path(path).exists():
            return path, index
    raise FileNotFoundError("找不到中文字型,請安裝 fonts-noto-cjk 或設定 SUBTITLE_FONT_FILE")


@lru_cache(maxsize=None)
def load_font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    path, index = find_font(bold)
    try:
        font = ImageFont.truetype(path, size, index=index)
    except OSError as exc:
        raise OSError(f"cannot load font {path!r} (index {index}); "
                      f"check SUBTITLE_FONT_FILE / SUBTITLE_FONT_INDEX: {exc}") from exc
    try:
        name = "%s %s" % font.getname()
    except Exception:
        name = "未知"
    log.info("字型 %s (index %d) → %s", path, index, name)
    return font


def _one_line(text: str) -> str:
    """Pillow's textlength rejects multi-line text, and upstream narration is whitespace-free anyway."""
    return "".join(text.split())


def render_card(text: str, label: str) -> Image.Image:
    """深色半透明圓角底、朱紅色左邊條、小標籤加大字。"""
    text, label = _one_line(text), _one_line(label)
    title_font, label_font = load_font(64, True), load_font(30, False)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    text_width = max(probe.textlength(text, font=title_font), probe.textlength(label, font=label_font))
    pad, bar = 30, 10
    width, height = int(text_width + pad * 2 + bar + 8), 150
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=10, fill=(14, 12, 10, 175))
    draw.rectangle((0, 0, bar, height - 1), fill=ACCENT + (255,))
    draw.text((bar + pad, 22), label, font=label_font, fill=(214, 190, 150, 255))
    draw.text((bar + pad, 62), text, font=title_font, fill=(255, 255, 255, 255))
    return card


def render_subtitle(text: str) -> Image.Image:
    """白字黑色描邊;太長時縮小字級 (最小 30 px)。"""
    text = _one_line(text)
    size = 50
    font = load_font(size, True)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    while probe.textlength(text, font=font) > SUBTITLE_MAX_WIDTH and size > 30:
        size -= 2
        font = load_font(size, True)
    width = int(probe.textlength(text, font=font)) + 24
    image = Image.new("RGBA", (width, size + 30), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((width // 2, (size + 30) // 2), text, font=font, fill=(255, 255, 255, 255),
                               anchor="mm", stroke_width=4, stroke_fill=(0, 0, 0, 255))
    return image


def shade_factor(width: int, height: int) -> np.ndarray:
    """底部漸層 + 兩側暈邊的乘數 (height, width, 1);讓不同圖的亮度統一、字幕更清楚。"""
    ys = np.linspace(0, 1, height)[:, None]
    xs = np.linspace(-1, 1, width)[None, :]
    bottom = np.clip((ys - 0.60) / 0.40, 0, 1) ** 1.4 * 0.55
    vignette = np.clip((np.abs(xs) - 0.55) / 0.45, 0, 1) ** 2 * 0.25
    return (1.0 - np.clip(bottom + vignette, 0, 0.7)).astype(np.float32)[:, :, None]


def paste_rgba(frame: Image.Image, overlay: Image.Image, pos, fade: float = 1.0) -> None:
    fade = 0.0 if math.isnan(fade) else max(fade, 0.0)
    alpha = overlay.getchannel("A")
    if fade < 1.0:
        alpha = alpha.point(lambda v: int(v * fade))
    frame.paste(overlay.convert("RGB"), pos, alpha)
