"""運動庫:每個鏡頭在一張圖上的裁切視窗隨時間變化。視窗一律是 16:9、完全在圖內,不拉伸。"""

import math

MAX_ZOOM = 1.65
PAN_SCALE = 1.3
PUSH_SCALE = 1.24


def ease(t: float) -> float:
    if math.isnan(t):
        return 0.0
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def base_window(iw: float, ih: float) -> tuple[float, float]:
    """放大 1 倍時,圖內最大的 16:9 視窗尺寸。"""
    bw, bh = iw, iw * 9 / 16
    if bh > ih:
        bh, bw = ih, ih * 16 / 9
    return bw, bh


def make_box(iw, ih, scale, cx, cy):
    """以 (cx, cy) 為中心、放大 scale 倍的 16:9 視窗,夾在圖內。回傳 (x0, y0, x1, y1)。"""
    bw, bh = base_window(iw, ih)
    w, h = bw / scale, bh / scale
    x0 = min(max(cx - w / 2, 0), iw - w)
    y0 = min(max(cy - h / 2, 0), ih - h)
    return (x0, y0, x0 + w, y0 + h)


def _focus_xy(focus) -> tuple[float, float]:
    """shots.json 讀來的焦點:有限且夾在 [0, 1];格式不對或非有限數值就退回畫面中心。"""
    if isinstance(focus, (str, bytes)):
        return 0.5, 0.5
    try:
        x, y = float(focus[0]), float(focus[1])
    except (TypeError, ValueError, LookupError, OverflowError):
        return 0.5, 0.5
    if not (math.isfinite(x) and math.isfinite(y)):
        return 0.5, 0.5
    return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)


def window_for(motion: str, p: float, iw: int, ih: int, focus) -> tuple[float, float, float, float]:
    """p ∈ [0, 1] 為鏡頭進度;focus 為 (x, y) 正規化座標。"""
    if not (math.isfinite(iw) and math.isfinite(ih) and iw > 0 and ih > 0):
        raise ValueError(f"image size must be finite and positive, got {iw}x{ih}")
    e = ease(p)
    nx, ny = _focus_xy(focus)
    fx, fy = nx * iw, ny * ih
    mx, my = iw / 2, ih / 2
    if motion == "push_in":
        return make_box(iw, ih, lerp(1.0, PUSH_SCALE, e), lerp(mx, fx, 0.7 * e), lerp(my, fy, 0.7 * e))
    if motion == "pull_out":
        return make_box(iw, ih, lerp(PUSH_SCALE, 1.0, e), lerp(fx, mx, 0.7 * e), lerp(fy, my, 0.7 * e))
    if motion == "detail":
        return make_box(iw, ih, lerp(1.0, MAX_ZOOM, e), lerp(mx, fx, e), lerp(my, fy, e))
    if motion == "detail_hold":
        return make_box(iw, ih, lerp(MAX_ZOOM, MAX_ZOOM - 0.1, e), fx, fy)
    if motion in ("pan_lr", "pan_rl"):
        bw, _ = base_window(iw, ih)
        half = bw / PAN_SCALE / 2
        a, b = (half, iw - half) if motion == "pan_lr" else (iw - half, half)
        return make_box(iw, ih, PAN_SCALE, lerp(a, b, e), fy)
    if motion in ("tilt_up", "tilt_down"):
        _, bh = base_window(iw, ih)
        half = bh / PAN_SCALE / 2
        a, b = (ih - half, half) if motion == "tilt_up" else (half, ih - half)
        return make_box(iw, ih, PAN_SCALE, fx, lerp(a, b, e))
    raise ValueError(f"unknown motion {motion!r}")
