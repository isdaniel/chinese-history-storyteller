"""frame = f(t):依 shots.json 在任一時間點合成一幀 (運動、交叉淡化、字卡、字幕)。

畫面是時間的純函式,所以可以把絕對幀範圍切給多個程序各自渲染。
"""
import math

import numpy as np
from PIL import Image

from render_motion import ease, make_box, window_for
from render_overlays import paste_rgba, render_card, render_subtitle, shade_factor

CROSSFADE_SEC = 0.45
CARD_POS = (96, 640)          # 以 1920x1080 為基準
CARD_FADE_SEC = 0.35
SUBTITLE_BOTTOM_MARGIN = 52


def _timed_beat(beat: dict, image: str) -> dict:
    """複製一個鏡頭並檢查 start/end 是有限數值;格式不對就明確報錯,不要留到渲染中途或畫出錯的畫面。"""
    try:
        start, end = float(beat["start"]), float(beat["end"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError(f"beat {beat.get('id')} needs numeric start and end") from None
    if not (math.isfinite(start) and math.isfinite(end)):
        raise ValueError(f"beat {beat.get('id')} has non-finite start/end")
    return {**beat, "start": start, "end": end, "image": image}


def _overlay_card(beat_id: str, overlay) -> tuple:
    try:
        at, dur, text, label = float(overlay["at"]), float(overlay["dur"]), overlay["text"], overlay["label"]
        valid = math.isfinite(at) and math.isfinite(dur) and isinstance(text, str) and isinstance(label, str)
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"beat {beat_id} has a malformed overlay (needs text, label, finite at/dur): {overlay!r}")
    return at, dur, render_card(text, label)


class FrameComposer:
    def __init__(self, shots: dict, images: dict, cues, total: float, size=(1920, 1080), fps: int = 30):
        self.width, self.height = size
        self.fps = fps
        try:
            scene_image = {s["id"]: s["image"] for s in shots["scenes"]}
        except (KeyError, TypeError):
            raise ValueError("every scene needs an id and an image") from None
        beats = []
        for b in shots["beats"]:
            scene = b.get("scene")
            if scene not in scene_image:
                raise ValueError(f"beat {b.get('id')} refers to unknown scene {scene!r}")
            beats.append(_timed_beat(b, scene_image[scene]))
        if not beats:
            raise ValueError("shots has no beats")
        if any(later["start"] < earlier["start"] for earlier, later in zip(beats, beats[1:])):
            raise ValueError("beats must be in chronological order")
        self.images = {}
        for b in beats:
            name = b["image"]
            if name not in self.images:
                if name not in images:
                    raise ValueError(f"beat {b.get('id')}: image {name!r} was not loaded")
                self.images[name] = images[name] if images[name].mode == "RGB" else images[name].convert("RGB")
        beats[0]["start"] = 0.0
        for k in range(len(beats) - 1):
            beats[k]["end"] = beats[k + 1]["start"]
        beats[-1]["end"] = max(beats[-1]["end"], total)
        self.beats = beats
        # 各交界的淡化半寬,不超過兩側鏡頭長度的一半,相鄰視窗就不會重疊;頭尾沒有交界補 0
        edges = [min(CROSSFADE_SEC / 2, (a["end"] - a["start"]) / 2, (b["end"] - b["start"]) / 2)
                 for a, b in zip(beats, beats[1:])]
        self._halves = [0.0] + [max(h, 0.0) for h in edges] + [0.0]
        self.cards = [_overlay_card(b["id"], o) for b in beats for o in b.get("overlays") or ()]
        self.subtitles = [(a, b, render_subtitle(text)) for a, b, text in cues]
        # 明暗乘數攤平成 (H, W*3) 與畫面直接相乘,避開 (H, W, 1) 廣播的慢路徑,結果逐位元相同
        self._shade3 = np.ascontiguousarray(np.repeat(shade_factor(self.width, self.height), 3, axis=2)
                                            ).reshape(self.height, self.width * 3)
        self.fallback_beats: set[str] = set()

    def _scaled(self, pos):
        return round(pos[0] * self.width / 1920), round(pos[1] * self.height / 1080)

    def _beat_frame(self, beat: dict, t: float) -> Image.Image:
        image = self.images[beat["image"]]
        iw, ih = image.size
        progress = (t - beat["start"]) / max(beat["end"] - beat["start"], 1e-3)
        try:
            box = window_for(beat["motion"], progress, iw, ih, beat["focus"])
        except Exception:
            self.fallback_beats.add(beat["id"])
            box = make_box(iw, ih, 1.0 + 0.08 * ease(progress), iw / 2, ih / 2)
        return image.resize((self.width, self.height), Image.BICUBIC, box=box)

    def compose(self, n: int) -> Image.Image:
        t = n / self.fps
        # 視窗右端為開區間,任何時刻最多兩個鏡頭在場 (零長度鏡頭的視窗是空的)
        active = [k for k, b in enumerate(self.beats)
                  if b["start"] - self._halves[k] <= t < b["end"] + self._halves[k + 1]]
        if not active:
            active = [0 if t < self.beats[0]["start"] else len(self.beats) - 1]
        frame = self._beat_frame(self.beats[active[0]], t)
        if len(active) > 1:
            following = self.beats[active[1]]
            half = self._halves[active[1]]
            alpha = ease((t - (following["start"] - half)) / (2 * half))
            frame = Image.blend(frame, self._beat_frame(following, t), alpha)
        shaded = np.empty((self.height, self.width * 3), np.uint8)
        np.multiply(np.asarray(frame).reshape(self.height, self.width * 3), self._shade3, out=shaded,
                    casting="unsafe")
        frame = Image.fromarray(shaded.reshape(self.height, self.width, 3))
        for at, duration, card in self.cards:
            if at <= t <= at + duration:
                fade = min(1.0, (t - at) / CARD_FADE_SEC, (at + duration - t) / CARD_FADE_SEC)
                paste_rgba(frame, card, self._scaled(CARD_POS), max(fade, 0.0))
        for start, end, image in self.subtitles:
            if start <= t < end:
                paste_rgba(frame, image, ((self.width - image.width) // 2,
                                          self.height - image.height - SUBTITLE_BOTTOM_MARGIN))
                break
        return frame
