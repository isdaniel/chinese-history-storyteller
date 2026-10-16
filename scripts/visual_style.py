"""視覺風格預設:附加在每張圖 prompt 後面的風格後綴。"""
import os

STYLE_SUFFIXES = {
    "ink": (", traditional Chinese ink painting style, scroll painting aesthetic, "
            "muted earth tones, atmospheric, no text, no characters writing, "
            "cinematic composition, 16:9 aspect"),
    "cinematic": (", cinematic realistic historical scene, natural lighting, shallow depth of field, "
                  "muted film color grading, no text, no captions, no watermark, 16:9 aspect"),
}
DEFAULT_STYLE = "cinematic"

def current_style() -> str:
    style = os.environ.get("VISUAL_STYLE") or DEFAULT_STYLE
    if style not in STYLE_SUFFIXES:
        raise ValueError(f"unknown VISUAL_STYLE={style!r}; choose one of {sorted(STYLE_SUFFIXES)}")
    return style


def style_suffix(style: str) -> str:
    try:
        return STYLE_SUFFIXES[style]
    except KeyError:
        raise ValueError(f"unknown style {style!r}; choose one of {sorted(STYLE_SUFFIXES)}") from None
