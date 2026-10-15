import subprocess

import numpy as np
from PIL import Image, ImageDraw

import build_video


def _circle_image(path):
    img = Image.new("RGB", (1536, 1024), (0, 0, 0))
    ImageDraw.Draw(img).ellipse((568, 312, 968, 712), fill=(255, 255, 255))
    img.save(path)


def _frame(ffmpeg_exe, video, seconds, out):
    subprocess.run([ffmpeg_exe, "-y", "-loglevel", "error", "-ss", str(seconds), "-i", str(video),
                    "-frames:v", "1", str(out)], check=True)
    return np.asarray(Image.open(out).convert("L"))


def _blob(gray):
    ys, xs = np.nonzero(gray > 200)
    return xs.max() - xs.min() + 1, ys.max() - ys.min() + 1, xs.mean(), ys.mean()


def test_clip_filter_keeps_aspect_and_uses_time_based_zoom():
    vf = build_video.clip_filter(10.0)
    assert "force_original_aspect_ratio=increase" in vf
    assert "crop=2400:1350" in vf
    assert "min(zoom+0.0008" not in vf
    assert "on/300" in vf


def test_clip_does_not_stretch_and_keeps_zooming(tmp_path, ffmpeg_exe):
    img = tmp_path / "circle.png"
    _circle_image(img)
    clip = tmp_path / "clip.mp4"
    build_video.build_image_clip(img, 8.0, clip)

    w0, h0, _, _ = _blob(_frame(ffmpeg_exe, clip, 0.0, tmp_path / "f0.png"))
    assert abs(w0 / h0 - 1.0) < 0.03, "circle must stay round (image is not stretched)"

    w_a, _, _, _ = _blob(_frame(ffmpeg_exe, clip, 7.0, tmp_path / "f7.png"))
    w_b, _, cx, cy = _blob(_frame(ffmpeg_exe, clip, 7.9, tmp_path / "f79.png"))
    assert w_b / w_a > 1.008, "zoom must keep growing after 6 s instead of freezing"
    assert abs(cx - 960) < 15 and abs(cy - 540) < 15, "zoom must stay centred"
