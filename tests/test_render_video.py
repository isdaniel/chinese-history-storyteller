import json
import logging
import math
import re
import subprocess
import sys
import textwrap
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import common
import render_overlays as ro
import render_video as rv
import subtitles


@pytest.mark.parametrize("total,parts", [(1, 8), (10, 3), (16200, 8), (7, 7), (5, 20), (180, 4)])
def test_plan_chunks_cover_every_frame_exactly_once(total, parts):
    chunks = rv.plan_chunks(total, parts)
    assert chunks[0][0] == 0 and chunks[-1][1] == total
    for (a0, a1), (b0, b1) in zip(chunks, chunks[1:]):
        assert a1 == b0
    sizes = [b - a for a, b in chunks]
    assert max(sizes) - min(sizes) <= 1 and min(sizes) >= 1


def test_parse_detect_output_counts_segments():
    text = ("[blackdetect @ 0x1] black_start:0 black_end:1.2 black_duration:1.2\n"
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 4.0\n"
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 20.5\n")
    assert rv.parse_detect_output(text) == {"black_segments": 1, "freeze_segments": 2}
    assert rv.parse_detect_output("") == {"black_segments": 0, "freeze_segments": 0}


def test_fallback_beat_ids_combine_image_and_motion_fallbacks():
    shots = {"beats": [{"id": "b1", "scene": "s1"}, {"id": "b2", "scene": "s1"}, {"id": "b3", "scene": "s2"}]}
    report = {"scenes": [{"scene": "s1", "status": "fallback"}, {"scene": "s2", "status": "generated"}]}
    assert rv.fallback_beat_ids(shots, report, {"b3"}) == ["b1", "b2", "b3"]
    assert rv.fallback_beat_ids(shots, None, set()) == []


@pytest.fixture
def episode(tmp_path, monkeypatch, ffmpeg_exe):
    try:
        ro.find_font(True)
    except FileNotFoundError:
        pytest.skip("no CJK font installed")
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    ep = common.get_episode_dir(9)
    Image.new("RGB", (1536, 1024), (200, 40, 40)).save(ep / "img_s1.png")
    Image.new("RGB", (1536, 1024), (40, 40, 200)).save(ep / "img_s2.png")
    text = "天地玄黃宇宙洪荒日月盈昃。"
    units = [{"id": k, "section": 1, "text": text, "start": 2.0 * k, "end": 2.0 * (k + 1)} for k in range(3)]
    beats = [{"id": "b1", "unit_ids": [0], "start": 0.0, "end": 2.0, "scene": "s1", "motion": "push_in",
              "focus": [0.5, 0.5], "overlays": [{"text": "1974年", "label": "年代", "at": 0.5, "dur": 1.2}]},
             {"id": "b2", "unit_ids": [1], "start": 2.0, "end": 4.0, "scene": "s1", "motion": "pan_lr",
              "focus": [0.5, 0.5], "overlays": []},
             {"id": "b3", "unit_ids": [2], "start": 4.0, "end": 6.0, "scene": "s2", "motion": "detail",
              "focus": [0.4, 0.5], "overlays": []}]
    shots = {"style": "ink", "scenes": [{"id": "s1", "image_prompt": "p", "image": "img_s1.png"},
                                         {"id": "s2", "image_prompt": "p", "image": "img_s2.png"}], "beats": beats}
    (ep / "shots.json").write_text(json.dumps(shots, ensure_ascii=False), encoding="utf-8")
    timings = {"sections": [{"section_id": 1, "start": 0.0, "end": 6.0, "duration": 6.0, "narration": text}],
               "total": 6.0, "units": units, "words": [], "timing_report": []}
    (ep / "timings.json").write_text(json.dumps(timings, ensure_ascii=False), encoding="utf-8")
    subprocess.run([ffmpeg_exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-c:a", "libmp3lame", "-b:a", "192k", str(ep / "audio_full.mp3")], check=True)
    return ep


def test_render_produces_a_playable_1080p_video_with_audio_and_reports(episode):
    assert rv.main(9, workers=2) == 0
    info = rv.probe(episode / "final.mp4")
    assert (info["width"], info["height"], info["has_audio"]) == (1920, 1080, True)
    assert info["duration"] == pytest.approx(6.0, abs=0.1)
    report = json.loads((episode / "video_report.json").read_text(encoding="utf-8"))
    assert report["duration_ok"] is True and report["fallback_beats"] == [] and report["frames"] == 180
    assert (episode / "contact_sheet.png").exists()
    srt = (episode / "subtitles.srt").read_text(encoding="utf-8")
    assert srt.count("-->") == 3 and "天地玄黃宇宙洪荒日月盈昃。" in srt
    assert not (episode / "render_tmp").exists()


def test_limit_seconds_renders_only_the_first_part(episode):
    assert rv.main(9, limit_seconds=3.0, workers=2) == 0
    assert rv.probe(episode / "final.mp4")["duration"] == pytest.approx(3.0, abs=0.1)
    srt = (episode / "subtitles.srt").read_text(encoding="utf-8")
    assert srt.count("-->") == 2


def test_missing_scene_image_is_replaced_instead_of_failing(episode):
    (episode / "img_s2.png").unlink()
    assert rv.main(9, limit_seconds=5.0, workers=2) == 0
    assert (episode / "final.mp4").exists()


# ---------------------------------------------------------------------------
# Hardening tests added on top of the brief's tests above.

REAL_BUILD_COMPOSER = rv.build_composer
REAL_PROBE = rv.probe

# Real output of the bundled ffmpeg 7.1 (paths shortened).
FFMPEG_DETECT_LOG = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'ep0161/final.mp4':
  Duration: 00:00:07.50, start: 0.000000, bitrate: 92 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 320x180 [SAR 1:1 DAR 16:9], 13 kb/s, 30 fps, 30 tbr, 15360 tbn (default)
Stream mapping:
  Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))
Press [q] to stop, [?] for help
[blackdetect @ 000002a059d10700] black_start:0 black_end:1.5 black_duration:1.5
[freezedetect @ 000002a05b800fc0] lavfi.freezedetect.freeze_start: 1.5
[freezedetect @ 000002a05b800fc0] lavfi.freezedetect.freeze_duration: 4
[freezedetect @ 000002a05b800fc0] lavfi.freezedetect.freeze_end: 5.5
[out#0/null @ 000002a059c90640] video:97KiB audio:0KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
frame=  225 fps=0.0 q=-0.0 Lsize=N/A time=00:00:07.50 bitrate=N/A speed=40.4x
"""
PROBE_SAR_DAR = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'a.mp4':
  Duration: 00:00:07.50, start: 0.000000, bitrate: 92 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 320x180 [SAR 1:1 DAR 16:9], 13 kb/s, 30 fps, 30 tbr, 15360 tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, mono, fltp, 69 kb/s (default)
At least one output file must be specified
"""
PROBE_TAGGED = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'ep0161/final.mp4':
  Duration: 00:00:12.00, start: 0.000000, bitrate: 3187 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1920x1080, 2991 kb/s, 30 fps, 30 tbr, 15360 tbn (default)
"""
PROBE_MP3 = """\
Input #0, mp3, from 'ep0161/audio_full.mp3':
  Duration: 00:01:37.54, start: 0.011021, bitrate: 192 kb/s
  Stream #0:0: Audio: mp3 (mp3float), 48000 Hz, mono, fltp, 192 kb/s
At least one output file must be specified
"""
PROBE_MISSING = """\
[in#0 @ 000001a5a36b6940] Error opening input: No such file or directory
Error opening input file nothere.mp4.
Error opening input files: No such file or directory
"""
UNKNOWN_PROBE = {"duration": None, "width": None, "height": None, "has_audio": False}


def test_plan_chunks_gives_the_remainder_frames_to_the_first_chunks():
    assert rv.plan_chunks(10, 3) == [(0, 4), (4, 7), (7, 10)]
    assert rv.plan_chunks(5, 20) == [(k, k + 1) for k in range(5)]
    assert rv.plan_chunks(10, 0) == [(0, 10)] and rv.plan_chunks(10, -3) == [(0, 10)]


def test_parse_detect_output_on_real_ffmpeg_log():
    assert rv.parse_detect_output(FFMPEG_DETECT_LOG) == {"black_segments": 1, "freeze_segments": 1}


@pytest.mark.parametrize("text,expected", [
    (PROBE_SAR_DAR, {"duration": 7.5, "width": 320, "height": 180, "has_audio": True}),
    (PROBE_TAGGED, {"duration": 12.0, "width": 1920, "height": 1080, "has_audio": False}),
    (PROBE_MP3, {"duration": pytest.approx(97.54), "width": None, "height": None, "has_audio": True}),
    (PROBE_MISSING, UNKNOWN_PROBE),
    ("", UNKNOWN_PROBE),
    ("  Duration: N/A, start: 0.000000\n  Stream #0:0: Video: h264, yuv420p, 640x360, 25 fps\n",
     {"duration": None, "width": 640, "height": 360, "has_audio": False}),
    ("  Duration: 01:02:03.50, start: 0.0\n", {**UNKNOWN_PROBE, "duration": 3723.5}),
])
def test_parse_probe_output_handles_real_and_odd_ffmpeg_output(text, expected):
    assert rv.parse_probe_output(text) == expected


SHOTS3 = {"beats": [{"id": "b1", "scene": "s1"}, {"id": "b2", "scene": "s1"}, {"id": "b3", "scene": "s2"}]}


@pytest.mark.parametrize("report", [
    None, [], "x", {}, {"scenes": None}, {"scenes": "s1"}, {"scenes": [None, "s1", 7]},
    {"scenes": [{"status": "fallback"}, {"scene": 1, "status": "fallback"}, {"scene": ["s1"], "status": "fallback"},
                {"scene": "s1"}, {"scene": "s1", "status": None}]}])
def test_fallback_beat_ids_ignores_a_malformed_image_report(report):
    assert rv.fallback_beat_ids(SHOTS3, report, set()) == []


def test_fallback_beat_ids_keeps_valid_entries_next_to_malformed_ones():
    report = {"scenes": [None, {"scene": "s2", "status": "fallback"}, {"scene": "s1", "status": "generated"}]}
    assert rv.fallback_beat_ids(SHOTS3, report, set()) == ["b3"]


def test_fallback_beat_ids_includes_scenes_whose_image_could_not_be_read():
    assert rv.fallback_beat_ids(SHOTS3, None, set(), broken_scenes={"s1"}) == ["b1", "b2"]
    report = {"scenes": [{"scene": "s2", "status": "fallback"}]}
    assert rv.fallback_beat_ids(SHOTS3, report, {"b1"}, ["s1"]) == ["b1", "b2", "b3"]


def test_unreadable_scene_ids_flags_missing_and_corrupt_images(tmp_path, info_logs):
    Image.new("RGB", (8, 8)).save(tmp_path / "ok.png")
    good = (tmp_path / "ok.png").read_bytes()
    (tmp_path / "cut.png").write_bytes(good[:len(good) // 2])
    (tmp_path / "junk.png").write_bytes(b"not an image")
    (tmp_path / "empty.png").write_bytes(b"")
    names = {"s1": "ok.png", "s2": "missing.png", "s3": "cut.png", "s4": "junk.png", "s5": "empty.png"}
    shots = {"scenes": [{"id": sid, "image": name} for sid, name in names.items()]}
    assert rv.unreadable_scene_ids(tmp_path, shots) == {"s2", "s3", "s4", "s5"}
    messages = [record.getMessage() for record in info_logs.records if record.levelno == logging.ERROR]
    for name in ("missing.png", "cut.png", "junk.png", "empty.png"):
        assert sum(message.startswith(f"圖片 {name} ") for message in messages) == 1
    assert len(messages) == 4 and all("純色備援" in message for message in messages)


def test_a_scene_image_path_the_os_rejects_counts_as_unreadable(tmp_path):
    shots = {"scenes": [{"id": "s1", "image": "img\x00.png"}], "beats": []}
    assert rv.unreadable_scene_ids(tmp_path, shots) == {"s1"}


@pytest.fixture
def info_logs(caplog):
    caplog.set_level(logging.INFO, logger="render_video")
    return caplog


def test_load_inputs_replaces_an_unreadable_image_with_a_solid_one_and_stays_quiet_at_info_level(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="render_video")
    Image.new("RGB", (8, 8), (9, 9, 9)).save(tmp_path / "img_s1.png")
    (tmp_path / "shots.json").write_text(json.dumps({"scenes": [{"id": "s1", "image": "img_s1.png"},
                                                                {"id": "s2", "image": "img_s2.png"}],
                                                      "beats": []}), encoding="utf-8")
    (tmp_path / "timings.json").write_text(json.dumps({"total": 6.0, "units": []}), encoding="utf-8")
    shots, timings, images, cues, total = rv.load_inputs(tmp_path, 2.5)
    assert images["img_s1.png"].getpixel((0, 0)) == (9, 9, 9)
    assert images["img_s2.png"].size == (1536, 1024) and images["img_s2.png"].getpixel((0, 0)) == (60, 50, 40)
    assert (cues, total) == ([], 2.5)
    assert "img_s2.png" not in caplog.text
    caplog.set_level(logging.DEBUG, logger="render_video")
    rv.load_inputs(tmp_path, 2.5)
    assert "img_s2.png" in caplog.text and "img_s1.png" not in caplog.text


@pytest.mark.parametrize("timings,limit,expected", [
    ({"total": 6.0}, None, 6.0), ({"total": 6.0}, 3.5, 3.5), ({"total": 6.0}, 9.0, 6.0), ({"total": "6.5"}, None, 6.5)])
def test_render_seconds_is_the_shorter_of_the_limit_and_the_timeline(timings, limit, expected):
    assert rv.render_seconds(timings, limit) == expected


@pytest.mark.parametrize("timings", [{}, {"total": None}, {"total": "abc"}, {"total": math.nan}, {"total": math.inf},
                                     {"total": 0}, {"total": -3.0}, []])
def test_render_seconds_rejects_a_missing_or_unusable_total(timings):
    with pytest.raises(rv.RenderError, match="total"):
        rv.render_seconds(timings)


# ---- real ffmpeg building blocks (tiny clips) -----------------------------------------------------------------

def make_clip(exe, path, source, frames, size="64x36", audio=None, crf=None):
    joiner = ":" if "=" in source else "="
    cmd = [exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"{source}{joiner}s={size}:r=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio}"]
    cmd += ["-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if crf:
        cmd += ["-crf", str(crf)]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def decoded_frames(exe, path):
    text = subprocess.run([exe, "-hide_banner", "-i", str(path), "-map", "0:v", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    return int(re.findall(r"frame=\s*(\d+)", text)[-1])


def player_frame(exe, path, frame=0, size=(64, 36)):
    """Decode one frame like a player does for HD video (BT.709, limited range) into an (h, w, 3) array."""
    raw = subprocess.run([exe, "-loglevel", "error", "-i", str(path), "-vf",
                          f"select=eq(n\\,{frame}),scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
                          "-fps_mode", "passthrough", "-frames:v", "1", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(size[1], size[0], 3)


def player_rgb(exe, path, frame=0, size=(64, 36)):
    """The centre pixel of `player_frame`."""
    pixels = player_frame(exe, path, frame, size)
    return tuple(int(v) for v in pixels[size[1] // 2, size[0] // 2])


def make_mp3(exe, path, seconds):
    subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", str(path)], check=True)
    return path


def image_size(path):
    with Image.open(path) as image:
        return image.size


def test_probe_reads_a_real_file_and_survives_a_missing_one(tmp_path, ffmpeg_exe):
    clip = make_clip(ffmpeg_exe, tmp_path / "c.mp4", "testsrc", frames=15, audio=1)
    info = rv.probe(clip)
    assert (info["width"], info["height"], info["has_audio"]) == (64, 36, True)
    assert info["duration"] == pytest.approx(0.5, abs=0.1)
    assert rv.probe(tmp_path / "nothere.mp4") == UNKNOWN_PROBE


def test_probe_returns_unknown_values_when_ffmpeg_cannot_run(tmp_path, monkeypatch, info_logs):
    monkeypatch.setattr(rv, "ffmpeg_bin", lambda: str(tmp_path / "no-such-ffmpeg"))
    assert rv.probe(tmp_path / "x.mp4") == UNKNOWN_PROBE
    assert "x.mp4" in info_logs.text


def test_detect_black_freeze_counts_real_segments(tmp_path, ffmpeg_exe):
    black = make_clip(ffmpeg_exe, tmp_path / "black.mp4", "color=c=black", frames=45, size="160x90")
    still = make_clip(ffmpeg_exe, tmp_path / "still.mp4", "color=c=red", frames=120, size="160x90")
    moving = make_clip(ffmpeg_exe, tmp_path / "moving.mp4", "testsrc", frames=60, size="160x90")
    joined = tmp_path / "joined.mp4"
    rv.concat_chunks([str(black), str(still)], joined)
    assert rv.detect_black_freeze(joined) == {"black_segments": 1, "freeze_segments": 1, "decoded_frames": 165}
    assert rv.detect_black_freeze(moving) == {"black_segments": 0, "freeze_segments": 0, "decoded_frames": 60}


def test_detect_black_freeze_ignores_mostly_dark_and_slightly_moving_frames(tmp_path, ffmpeg_exe):
    mostly_dark = tmp_path / "dark.mp4"
    subprocess.run([ffmpeg_exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=160x90:r=30,drawbox=x=112:y=0:w=48:h=90:color=white:t=fill", "-frames:v", "60",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mostly_dark)], check=True)
    noisy = tmp_path / "noisy.mp4"
    subprocess.run([ffmpeg_exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=gray:s=160x90:r=30,noise=alls=12:allf=t", "-frames:v", "150", "-c:v", "libx264",
                    "-crf", "10", "-pix_fmt", "yuv420p", str(noisy)], check=True)
    assert rv.detect_black_freeze(mostly_dark) == {"black_segments": 0, "freeze_segments": 0, "decoded_frames": 60}
    assert rv.detect_black_freeze(noisy) == {"black_segments": 0, "freeze_segments": 0, "decoded_frames": 150}


def test_detect_black_freeze_reports_unknown_instead_of_raising(tmp_path, monkeypatch, info_logs):
    unknown = {"black_segments": None, "freeze_segments": None, "decoded_frames": None}
    assert rv.detect_black_freeze(tmp_path / "nothere.mp4") == unknown
    assert "exit" in info_logs.text
    monkeypatch.setattr(rv, "ffmpeg_bin", lambda: str(tmp_path / "no-such-ffmpeg"))
    assert rv.detect_black_freeze(tmp_path / "x.mp4") == unknown


def test_concat_chunks_joins_in_the_given_order_and_handles_quotes_and_relative_paths(tmp_path, monkeypatch,
                                                                                      ffmpeg_exe):
    folder = tmp_path / "it's here"
    folder.mkdir()
    red = make_clip(ffmpeg_exe, folder / "red.mp4", "color=c=red", frames=10, crf=40)
    blue = make_clip(ffmpeg_exe, folder / "blue.mp4", "color=c=blue", frames=5, crf=40)
    rv.concat_chunks([str(red), str(blue)], folder / "ab.mp4")
    rv.concat_chunks([str(blue), str(red)], folder / "ba.mp4")
    assert decoded_frames(ffmpeg_exe, folder / "ab.mp4") == 15 and decoded_frames(ffmpeg_exe, folder / "ba.mp4") == 15
    assert player_rgb(ffmpeg_exe, folder / "ab.mp4")[0] > 150 and player_rgb(ffmpeg_exe, folder / "ba.mp4")[2] > 150
    assert "'\\''" in (folder / "ab.txt").read_text(encoding="utf-8")
    joined = (folder / "ab.mp4").read_bytes()
    assert b"crf=40.0" in joined and b"crf=23.0" not in joined
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    rv.concat_chunks([str(red.relative_to(tmp_path)), str(blue.relative_to(tmp_path))], Path("sub") / "rel.mp4")
    assert decoded_frames(ffmpeg_exe, tmp_path / "sub" / "rel.mp4") == 15


def test_concat_chunks_failure_becomes_a_render_error(tmp_path, ffmpeg_exe):
    with pytest.raises(rv.RenderError, match=r"concat failed \(exit -?\d+\): \S"):
        rv.concat_chunks([str(tmp_path / "nothere.mp4")], tmp_path / "out.mp4")


def test_mux_audio_trims_to_the_given_seconds_and_publishes_atomically(tmp_path, ffmpeg_exe):
    video = make_clip(ffmpeg_exe, tmp_path / "v.mp4", "testsrc", frames=90, crf=40)
    audio = make_mp3(ffmpeg_exe, tmp_path / "a.mp3", 4)
    out = tmp_path / "final.mp4"
    out.write_bytes(b"previous")
    rv.mux_audio(video, audio, out, 1.5)
    info = rv.probe(out)
    assert info["duration"] == pytest.approx(1.5, abs=0.15) and info["has_audio"] and info["width"] == 64
    data = out.read_bytes()
    assert 0 <= data.find(b"moov") < data.find(b"mdat")
    assert b"crf=40.0" in data and b"crf=23.0" not in data
    streams = subprocess.run([ffmpeg_exe, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    audio_line = next(line for line in streams.splitlines() if "Audio:" in line)
    assert "Audio: aac" in audio_line and int(re.search(r"(\d+) kb/s", audio_line)[1]) > 100
    assert not list(tmp_path.glob("*.part*"))


def test_mux_audio_failure_leaves_the_existing_output_untouched(tmp_path, ffmpeg_exe):
    video = make_clip(ffmpeg_exe, tmp_path / "v.mp4", "testsrc", frames=30)
    (tmp_path / "bad.mp3").write_bytes(b"this is not audio")
    out = tmp_path / "final.mp4"
    out.write_bytes(b"previous")
    with pytest.raises(rv.RenderError, match=r"mux failed \(exit -?\d+\): \S"):
        rv.mux_audio(video, tmp_path / "bad.mp3", out, 1.0)
    assert out.read_bytes() == b"previous" and not list(tmp_path.glob("*.part*"))


# ---- render_chunk -------------------------------------------------------------------------------------------

class FlatComposer:
    fps = 30
    beats = []

    def __init__(self, colour=(200, 40, 40), fail_at=None):
        self.colour, self.fail_at, self.fallback_beats = colour, fail_at, {"b2"}

    def compose(self, n):
        if n == self.fail_at:
            raise ValueError("kaboom")
        return Image.new("RGB", (1920, 1080), self.colour)


def chunk_job(tmp_path, start=0, end=6, crf=22):
    return {"ep_dir": str(tmp_path), "limit": None, "start": start, "end": end, "crf": crf,
            "out": str(tmp_path / "chunk_000.mp4")}


@pytest.fixture
def fake_encoder(tmp_path, monkeypatch):
    """Run `python <script>` instead of ffmpeg (same stdin/stderr wiring) and remember the processes."""
    procs = []
    real_popen = subprocess.Popen

    def install(source, composer=None):
        script = tmp_path / "fake_encoder.py"
        script.write_text(textwrap.dedent(source), encoding="utf-8")

        def popen(cmd, **kwargs):
            procs.append(real_popen([sys.executable, str(script)], **kwargs))
            return procs[-1]

        monkeypatch.setattr(rv, "ffmpeg_bin", lambda: "ffmpeg-sentinel")
        monkeypatch.setattr(rv, "build_composer", lambda ep_dir, limit: composer or FlatComposer())
        monkeypatch.setattr(rv.subprocess, "Popen", popen)
        return procs

    return install


def test_render_chunk_encodes_the_range_with_the_expected_stream_settings(tmp_path, monkeypatch, ffmpeg_exe):
    monkeypatch.setattr(rv, "build_composer", lambda ep_dir, limit: FlatComposer())
    result = rv.render_chunk(chunk_job(tmp_path, 5, 17, crf=30))
    out = tmp_path / "chunk_000.mp4"
    assert result == {"out": str(out), "frames": 12, "fallback_beats": ["b2"]}
    assert decoded_frames(ffmpeg_exe, out) == 12
    info = rv.probe(out)
    assert (info["width"], info["height"], info["has_audio"]) == (1920, 1080, False)
    assert info["duration"] == pytest.approx(0.4, abs=0.05)
    text = subprocess.run([ffmpeg_exe, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "yuv420p(tv, bt709, progressive)" in text and " 30 fps" in text
    data = out.read_bytes()
    assert b"crf=30.0" in data and b"subme=7" in data and b"me=hex" in data
    assert all(abs(got - want) <= 4 for got, want in zip(player_rgb(ffmpeg_exe, out, size=(1920, 1080)), (200, 40, 40)))


def test_render_chunk_reports_the_real_encoders_own_error_text(tmp_path, monkeypatch, ffmpeg_exe):
    monkeypatch.setattr(rv, "build_composer", lambda ep_dir, limit: FlatComposer())
    with pytest.raises(rv.RenderError, match=r"frames 0-6 \(exit -?\d+\): \S") as caught:
        rv.render_chunk(chunk_job(tmp_path, crf="abc"))
    assert "crf" in str(caught.value)


def test_render_chunk_reports_an_encoder_that_exits_early_with_its_stderr(tmp_path, fake_encoder):
    procs = fake_encoder("""
        import sys
        sys.stderr.write("fake encoder: out of cheese\\n")
        sys.exit(3)
    """)
    with pytest.raises(rv.RenderError, match=r"frames 0-6 \(exit 3\): fake encoder: out of cheese") as caught:
        rv.render_chunk(chunk_job(tmp_path))
    assert isinstance(caught.value.__cause__, OSError)
    assert procs[0].poll() == 3


def test_render_chunk_does_not_deadlock_on_a_chatty_encoder(tmp_path, fake_encoder):
    fake_encoder("""
        import sys
        sys.stderr.write("x" * 3_000_000)
        sys.stderr.flush()
        while sys.stdin.buffer.read(65536):
            pass
    """)
    assert rv.render_chunk(chunk_job(tmp_path))["frames"] == 6


def test_render_chunk_reports_the_tail_of_a_failing_encoders_stderr(tmp_path, fake_encoder):
    fake_encoder("""
        import sys
        sys.stderr.write("BEGIN" + "." * 5000 + "END-MARKER\\n\\n")
        sys.stderr.flush()
        while sys.stdin.buffer.read(65536):
            pass
        sys.exit(1)
    """)
    with pytest.raises(rv.RenderError, match=r"frames 0-6 \(exit 1\)") as caught:
        rv.render_chunk(chunk_job(tmp_path))
    assert str(caught.value).endswith("END-MARKER") and "BEGIN" not in str(caught.value)
    assert len(str(caught.value)) < 1200


def test_render_chunk_kills_the_encoder_when_composing_a_frame_fails(tmp_path, fake_encoder):
    procs = fake_encoder("""
        import sys, time
        while sys.stdin.buffer.read(65536):
            pass
        time.sleep(20)
    """, composer=FlatComposer(fail_at=3))
    with pytest.raises(rv.RenderError, match=r"composing frame 3 failed \(frames 0-6\).*kaboom") as caught:
        rv.render_chunk(chunk_job(tmp_path))
    assert isinstance(caught.value.__cause__, ValueError)
    assert procs[0].poll() is not None


def test_render_chunk_reports_a_missing_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "build_composer", lambda ep_dir, limit: FlatComposer())
    monkeypatch.setattr(rv, "ffmpeg_bin", lambda: str(tmp_path / "no-such-ffmpeg"))
    with pytest.raises(rv.RenderError, match=r"cannot start ffmpeg for frames 0-6"):
        rv.render_chunk(chunk_job(tmp_path))


# ---- main(): wiring with fakes (no processes, no ffmpeg, no fonts) ----------------------------------------------

VIDEO_INFO = {"duration": 6.0, "width": 1920, "height": 1080, "has_audio": True}
QUIET_QUALITY = {"black_segments": 0, "freeze_segments": 0}


class LazyFuture(Future):
    """A future that runs its call only when the fake as_completed reaches it."""

    def __init__(self, fn, args):
        super().__init__()
        self.call = (fn, args)

    def run(self):
        fn, args = self.call
        try:
            self.set_result(fn(*args))
        except BaseException as exc:
            self.set_exception(exc)


class SerialPool:
    def __init__(self, registry, max_workers=None):
        self.max_workers, self.futures, self.shutdown_kwargs = max_workers, [], None
        registry.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def submit(self, fn, *args):
        self.futures.append(LazyFuture(fn, args))
        return self.futures[-1]

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_kwargs = {"wait": wait, "cancel_futures": cancel_futures}
        if cancel_futures:
            for future in self.futures:
                future.cancel()


@pytest.fixture
def wired(tmp_path, monkeypatch, info_logs):
    """Episode 9 on disk plus fakes for every heavy collaborator of main()."""
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(rv.os, "cpu_count", lambda: 16)
    for name in ("RENDER_WORKERS", "VIDEO_CRF"):
        monkeypatch.delenv(name, raising=False)
    ep = common.get_episode_dir(9)
    for name in ("img_s1.png", "img_s2.png"):
        Image.new("RGB", (8, 8)).save(ep / name)
    text = "天地玄黃宇宙洪荒日月盈昃。"
    units = [{"id": k, "section": 1, "text": text, "start": 2.0 * k, "end": 2.0 * (k + 1)} for k in range(3)]
    beats = [{"id": f"b{k + 1}", "unit_ids": [k], "start": 2.0 * k, "end": 2.0 * (k + 1), "scene": "s1" if k < 2 else "s2",
              "motion": "push_in", "focus": [0.5, 0.5], "overlays": []} for k in range(3)]
    shots = {"style": "ink", "scenes": [{"id": "s1", "image_prompt": "p", "image": "img_s1.png"},
                                         {"id": "s2", "image_prompt": "p", "image": "img_s2.png"}], "beats": beats}
    (ep / "shots.json").write_text(json.dumps(shots, ensure_ascii=False), encoding="utf-8")
    timings = {"sections": [], "total": 6.0, "units": units, "words": [], "timing_report": []}
    (ep / "timings.json").write_text(json.dumps(timings, ensure_ascii=False), encoding="utf-8")
    (ep / "audio_full.mp3").write_bytes(b"audio")

    w = SimpleNamespace(ep=ep, pools=[], jobs=[], concat=[], mux=[], contact=[], composer_calls=[], detect_calls=[],
                        order=list, fail_job=None, motion={}, video_info=dict(VIDEO_INFO),
                        audio_sec=6.0, audio_has=True, quality=dict(QUIET_QUALITY), decoded="jobs", contact_error=None,
                        final_bytes=1_234_567, composer=FlatComposer(), tmp_at_start=None)

    def build_composer(ep_dir, limit):
        w.composer_calls.append((ep_dir, limit))
        return w.composer

    def render_chunk(job):
        if not w.jobs:
            w.tmp_at_start = sorted(p.name for p in Path(job["out"]).parent.iterdir())
        w.jobs.append(job)
        if w.fail_job == len(w.jobs) - 1:
            raise rv.RenderError("boom")
        return {"out": job["out"], "frames": job["end"] - job["start"], "fallback_beats": w.motion.get(job["start"], [])}

    def as_completed(futures):
        for future in w.order(list(futures)):
            future.run()
            yield future

    def concat_chunks(chunks, out_path):
        w.concat.append((list(chunks), out_path))
        Path(out_path).write_bytes(b"video")

    def mux_audio(video, audio, out_path, seconds):
        w.mux.append((video, audio, out_path, seconds))
        Path(out_path).write_bytes(b"f" * w.final_bytes)

    def probe(path):
        if Path(path).name == "final.mp4":
            return dict(w.video_info)
        return {**UNKNOWN_PROBE, "duration": w.audio_sec, "has_audio": w.audio_has}

    def detect(video):
        w.detect_calls.append(video)
        decoded = w.jobs[-1]["end"] if w.decoded == "jobs" else w.decoded
        return {**w.quality, "decoded_frames": decoded}

    def contact_sheet(composer, out_path, **kwargs):
        w.contact.append((composer, out_path, kwargs))
        if w.contact_error:
            Path(out_path).write_bytes(b"partial")
            raise w.contact_error
        Path(out_path).write_bytes(b"png")

    monkeypatch.setattr(rv, "build_composer", build_composer)
    monkeypatch.setattr(rv, "ProcessPoolExecutor", lambda max_workers=None: SerialPool(w.pools, max_workers))
    monkeypatch.setattr(rv, "as_completed", as_completed)
    monkeypatch.setattr(rv, "render_chunk", render_chunk)
    monkeypatch.setattr(rv, "concat_chunks", concat_chunks)
    monkeypatch.setattr(rv, "mux_audio", mux_audio)
    monkeypatch.setattr(rv, "probe", probe)
    monkeypatch.setattr(rv, "detect_black_freeze", detect)
    monkeypatch.setattr(rv, "make_contact_sheet", contact_sheet)
    return w


def report_of(w):
    return json.loads((w.ep / "video_report.json").read_text(encoding="utf-8"))


def test_main_splits_the_frames_into_two_chunks_per_worker_and_wires_the_stages(wired):
    assert rv.main(9, workers=2) == 0
    ep = wired.ep
    assert [p.max_workers for p in wired.pools] == [2]
    assert [(j["start"], j["end"]) for j in wired.jobs] == [(0, 45), (45, 90), (90, 135), (135, 180)]
    assert wired.jobs[0] == {"ep_dir": str(ep), "limit": None, "start": 0, "end": 45, "crf": 22,
                             "out": str(ep / "render_tmp" / "chunk_000.mp4")}
    assert [Path(j["out"]).name for j in wired.jobs] == [f"chunk_00{k}.mp4" for k in range(4)]
    assert wired.concat == [([j["out"] for j in wired.jobs], ep / "render_tmp" / "video.mp4")]
    assert wired.mux == [(ep / "render_tmp" / "video.mp4", ep / "audio_full.mp3", ep / "final.mp4", 6.0)]
    assert wired.detect_calls == [ep / "final.mp4"]
    assert wired.composer_calls == [(ep, None)]
    assert len(wired.contact) == 1 and wired.contact[0][0] is wired.composer
    assert wired.contact[0][1] == ep / "contact_sheet.png" and wired.contact[0][2] == {"total": 6.0}
    assert not (ep / "render_tmp").exists()
    report = report_of(wired)
    assert {k: v for k, v in report.items() if k not in ("render_sec",)} == {
        "seconds": 6.0, "frames": 180, "decoded_frames": 180, "frames_ok": True, "workers": 2, "crf": 22,
        "size_mb": 1.2, "video": VIDEO_INFO, "audio_sec": 6.0, "duration_ok": True, "beats": 3, "fallback_beats": [],
        "quality": QUIET_QUALITY, "contact_sheet": True}
    assert isinstance(report["render_sec"], float) and 0 <= report["render_sec"] < 60
    srt = (ep / "subtitles.srt").read_text(encoding="utf-8")
    assert srt.count("-->") == 3 and "00:00:02,000 --> 00:00:04,000" in srt


def test_main_logs_each_chunk_as_it_completes(wired, info_logs):
    rv.main(9, workers=2)
    assert all(f"{k}/4" in info_logs.text for k in (1, 2, 3, 4))


def test_main_limit_renders_a_prefix_and_keeps_only_cues_that_start_before_it(wired):
    assert rv.main(9, limit_seconds=4.0, workers=1) == 0
    assert [(j["start"], j["end"], j["limit"]) for j in wired.jobs] == [(0, 60, 4.0), (60, 120, 4.0)]
    assert wired.mux[0][3] == 4.0 and wired.composer_calls == [(wired.ep, 4.0)]
    assert wired.contact[0][2] == {"total": 4.0}
    assert (wired.ep / "subtitles.srt").read_text(encoding="utf-8").count("-->") == 2
    report = report_of(wired)
    assert (report["seconds"], report["frames"]) == (4.0, 120)


def test_main_rounds_seconds_to_milliseconds_and_frames_to_the_nearest(wired):
    wired.video_info["duration"] = 3.3333
    assert rv.main(9, limit_seconds=3.3333, workers=1) == 0
    report = report_of(wired)
    assert (report["seconds"], report["frames"]) == (3.333, 100)
    assert wired.jobs[-1]["end"] == 100 and wired.mux[0][3] == 3.3333


def test_main_limit_longer_than_the_timeline_is_capped(wired):
    assert rv.main(9, limit_seconds=99.0, workers=1) == 0
    assert wired.jobs[-1]["end"] == 180 and wired.mux[0][3] == 6.0


@pytest.mark.parametrize("cpus,expected", [(16, 4), (4, 4), (2, 2), (None, 1)])
def test_main_default_workers_is_the_cpu_count_capped_at_four(wired, monkeypatch, cpus, expected):
    monkeypatch.setattr(rv.os, "cpu_count", lambda: cpus)
    rv.main(9)
    assert [p.max_workers for p in wired.pools] == [expected] and len(wired.jobs) == expected * 2
    assert report_of(wired)["workers"] == expected


def test_main_reads_workers_and_crf_from_the_environment_and_an_argument_wins(wired, monkeypatch):
    monkeypatch.setenv("RENDER_WORKERS", "3")
    monkeypatch.setenv("VIDEO_CRF", "30")
    rv.main(9)
    assert [p.max_workers for p in wired.pools] == [3] and len(wired.jobs) == 6
    assert {j["crf"] for j in wired.jobs} == {30} and report_of(wired)["crf"] == 30
    rv.main(9, workers=1)
    assert wired.pools[-1].max_workers == 1


def test_main_treats_empty_environment_values_as_unset(wired, monkeypatch):
    monkeypatch.setenv("RENDER_WORKERS", "")
    monkeypatch.setenv("VIDEO_CRF", "")
    rv.main(9)
    assert report_of(wired)["crf"] == 22 and wired.pools[0].max_workers == min(4, rv.os.cpu_count() or 1)


@pytest.mark.parametrize("workers", [0, -1, "abc", 2.5, True])
def test_main_rejects_an_invalid_workers_argument_before_any_work(wired, workers):
    with pytest.raises(rv.RenderError, match="workers"):
        rv.main(9, workers=workers)
    assert not wired.pools and not (wired.ep / "render_tmp").exists()


@pytest.mark.parametrize("raw", ["abc", "0", "-2", "2.5", "1e1"])
def test_main_rejects_an_invalid_render_workers_environment(wired, monkeypatch, raw):
    monkeypatch.setenv("RENDER_WORKERS", raw)
    with pytest.raises(rv.RenderError, match="RENDER_WORKERS"):
        rv.main(9)
    assert not wired.pools


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "52", "22.5"])
def test_main_rejects_an_invalid_video_crf_environment(wired, monkeypatch, raw):
    monkeypatch.setenv("VIDEO_CRF", raw)
    with pytest.raises(rv.RenderError, match="VIDEO_CRF"):
        rv.main(9, workers=1)
    assert not wired.pools and not wired.jobs


@pytest.mark.parametrize("raw", ["1", "51"])
def test_main_accepts_the_crf_range_limits(wired, monkeypatch, raw):
    monkeypatch.setenv("VIDEO_CRF", raw)
    assert rv.main(9, workers=1) == 0 and report_of(wired)["crf"] == int(raw)


@pytest.mark.parametrize("limit", [0, -1.0, math.nan, math.inf, "abc", [1]])
def test_main_rejects_an_unusable_limit(wired, limit):
    with pytest.raises(rv.RenderError, match="limit_seconds"):
        rv.main(9, limit_seconds=limit, workers=1)
    assert not wired.pools


@pytest.mark.parametrize("name", ["shots.json", "timings.json", "audio_full.mp3"])
def test_main_names_a_missing_input_and_starts_nothing(wired, name):
    (wired.ep / name).unlink()
    with pytest.raises(rv.RenderError, match="missing input file.*" + re.escape(name)):
        rv.main(9, workers=1)
    assert not wired.pools and not (wired.ep / "render_tmp").exists()


@pytest.mark.parametrize("name,content", [("shots.json", "{truncated"), ("timings.json", "[1, 2]"),
                                          ("timings.json", "{}"), ("timings.json", '{"total": 0, "units": []}'),
                                          ("timings.json", '{"total": 0.01, "units": []}')])
def test_main_rejects_unusable_json_inputs(wired, name, content):
    (wired.ep / name).write_text(content, encoding="utf-8")
    with pytest.raises(rv.RenderError):
        rv.main(9, workers=1)
    assert not wired.pools and not (wired.ep / "render_tmp").exists()


@pytest.mark.parametrize("name", ["shots.json", "timings.json"])
def test_main_requires_json_objects(wired, name):
    (wired.ep / name).write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(rv.RenderError, match=f"{re.escape(name)} must contain a JSON object"):
        rv.main(9, workers=1)


def test_main_names_the_file_when_json_is_corrupt(wired):
    (wired.ep / "shots.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(rv.RenderError, match="shots.json"):
        rv.main(9, workers=1)


def test_main_maps_malformed_shots_to_a_render_error_before_starting_workers(wired, monkeypatch):
    monkeypatch.setattr(rv, "build_composer", REAL_BUILD_COMPOSER)
    shots = json.loads((wired.ep / "shots.json").read_text(encoding="utf-8"))
    shots["beats"][2]["scene"] = "s9"
    (wired.ep / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    with pytest.raises(rv.RenderError, match="invalid render inputs.*unknown scene 's9'") as caught:
        rv.main(9, workers=1)
    assert isinstance(caught.value.__cause__, ValueError) and not wired.pools


def test_main_maps_a_timings_file_without_units_to_a_render_error(wired):
    (wired.ep / "timings.json").write_text(json.dumps({"total": 6.0}), encoding="utf-8")
    with pytest.raises(rv.RenderError, match="invalid render inputs"):
        rv.main(9, workers=1)
    assert not wired.pools


def test_a_failing_chunk_becomes_a_render_error_cancels_the_rest_and_cleans_up(wired):
    wired.fail_job = 0
    (wired.ep / "video_report.json").write_text("stale", encoding="utf-8")
    (wired.ep / "contact_sheet.png").write_bytes(b"stale")
    with pytest.raises(rv.RenderError, match="parallel render failed: boom") as caught:
        rv.main(9, workers=2)
    assert isinstance(caught.value.__cause__, rv.RenderError) and str(caught.value.__cause__) == "boom"
    assert len(wired.jobs) == 1
    assert wired.pools[0].shutdown_kwargs == {"wait": True, "cancel_futures": True}
    assert [f.cancelled() for f in wired.pools[0].futures] == [False, True, True, True]
    assert not wired.concat and not wired.mux
    assert not (wired.ep / "render_tmp").exists() and not (wired.ep / "final.mp4").exists()
    assert not (wired.ep / "video_report.json").exists() and not (wired.ep / "contact_sheet.png").exists()


def test_a_failure_in_a_later_chunk_still_stops_the_run(wired):
    wired.fail_job = 2
    with pytest.raises(rv.RenderError, match="boom"):
        rv.main(9, workers=2)
    assert len(wired.jobs) == 3 and not wired.mux


def test_a_pool_that_cannot_start_is_a_render_error(wired, monkeypatch):
    def broken(max_workers=None):
        raise ValueError("max_workers must be <= 61")

    monkeypatch.setattr(rv, "ProcessPoolExecutor", broken)
    with pytest.raises(rv.RenderError, match="parallel render failed: max_workers must be <= 61"):
        rv.main(9, workers=2)
    assert not (wired.ep / "render_tmp").exists()


def test_chunks_are_concatenated_in_job_order_even_when_they_finish_out_of_order(wired):
    wired.order = lambda futures: list(reversed(futures))
    rv.main(9, workers=2)
    assert [Path(c).name for c in wired.concat[0][0]] == [f"chunk_00{k}.mp4" for k in range(4)]
    assert [j["start"] for j in wired.jobs] == [135, 90, 45, 0]


@pytest.mark.parametrize("stage", ["concat", "mux"])
def test_a_failing_concat_or_mux_cleans_up_and_propagates(wired, monkeypatch, stage):
    def fail(*args):
        raise rv.RenderError(f"{stage} failed (exit 1): nope")

    monkeypatch.setattr(rv, f"{stage}_chunks" if stage == "concat" else "mux_audio", fail)
    with pytest.raises(rv.RenderError, match=f"{stage} failed"):
        rv.main(9, workers=1)
    assert not (wired.ep / "render_tmp").exists() and not (wired.ep / "video_report.json").exists()


def test_stale_render_tmp_is_replaced(wired):
    (wired.ep / "render_tmp").mkdir()
    (wired.ep / "render_tmp" / "chunk_000.mp4").write_bytes(b"old")
    rv.main(9, workers=1)
    assert wired.tmp_at_start == [] and not (wired.ep / "render_tmp").exists()


def test_fallback_beats_merge_image_report_motion_fallbacks_and_unreadable_images(wired):
    (wired.ep / "img_s2.png").write_bytes(b"corrupt")
    wired.motion = {0: ["b2"], 45: ["b2", "b1"]}
    report_path = wired.ep / "image_report.json"
    report_path.write_text(json.dumps({"scenes": [{"scene": "s1", "status": "fallback"}]}), encoding="utf-8")
    rv.main(9, workers=2)
    assert report_of(wired)["fallback_beats"] == ["b1", "b2", "b3"]
    report_path.write_text(json.dumps({"scenes": [{"scene": "s9", "status": "fallback"}]}), encoding="utf-8")
    rv.main(9, workers=2)
    assert report_of(wired)["fallback_beats"] == ["b1", "b2", "b3"]


@pytest.mark.parametrize("missing_image", ["img_s1.png", "img_s2.png"])
def test_a_missing_scene_image_marks_that_scenes_beats_as_fallbacks(wired, missing_image, info_logs):
    (wired.ep / missing_image).unlink()
    rv.main(9, workers=1)
    expected = ["b1", "b2"] if missing_image == "img_s1.png" else ["b3"]
    assert report_of(wired)["fallback_beats"] == expected
    assert "純色備援" in info_logs.text
    assert sum(missing_image in record.getMessage() for record in info_logs.records) == 1


@pytest.mark.parametrize("content", ["{truncated", "[]", '"x"', '{"scenes": 5}', '{"scenes": [null]}'])
def test_a_truncated_or_odd_image_report_is_ignored(wired, content):
    (wired.ep / "image_report.json").write_text(content, encoding="utf-8")
    assert rv.main(9, workers=1) == 0
    assert report_of(wired)["fallback_beats"] == []


def test_a_failing_contact_sheet_does_not_lose_the_video(wired, info_logs):
    wired.contact_error = RuntimeError("font exploded")
    assert rv.main(9, workers=1) == 0
    report = report_of(wired)
    assert report["contact_sheet"] is False and (wired.ep / "final.mp4").exists()
    assert not (wired.ep / "contact_sheet.png").exists()
    assert "font exploded" in info_logs.text and "RuntimeError" in info_logs.text
    assert report["duration_ok"] is True and report["fallback_beats"] == []


def test_unknown_quality_results_are_reported_without_breaking_the_final_log(wired, info_logs):
    wired.quality = {"black_segments": None, "freeze_segments": None}
    assert rv.main(9, workers=1) == 0
    assert report_of(wired)["quality"] == {"black_segments": None, "freeze_segments": None}
    assert "Logging error" not in info_logs.text and "黑畫面 None 段、靜止 None 段" in info_logs.text


@pytest.mark.parametrize("video,audio,ok", [
    (6.0, 6.0, True), (6.25, 6.0, True), (5.75, 6.0, True), (6.26, 6.0, False), (5.74, 6.0, False),
    (6.0, 6.25, True), (6.0, 5.75, True), (6.0, 6.26, False), (6.0, 5.74, False), (None, 6.0, False),
    (3.0, 3.0, False)])
def test_duration_ok_requires_video_and_audio_to_match_the_timeline_within_the_tolerance(
        wired, monkeypatch, info_logs, video, audio, ok):
    monkeypatch.setattr(rv, "DURATION_TOLERANCE_SEC", 0.25)
    wired.video_info["duration"], wired.audio_sec = video, audio
    assert rv.main(9, workers=1) == 0
    report = report_of(wired)
    assert report["duration_ok"] is ok and report["audio_sec"] == audio
    assert ("相差超過" in info_logs.text) is (not ok)


def test_duration_tolerance_default_is_a_tenth_of_a_second():
    assert rv.DURATION_TOLERANCE_SEC == 0.1 and (rv.W, rv.H, rv.FPS) == (1920, 1080, 30)


def test_a_limited_run_does_not_flag_the_longer_full_audio(wired):
    wired.video_info["duration"] = 3.0
    assert rv.main(9, limit_seconds=3.0, workers=1) == 0
    assert report_of(wired)["duration_ok"] is True


def test_main_removes_a_stale_report_and_contact_sheet_before_rendering(wired):
    (wired.ep / "video_report.json").write_text("stale", encoding="utf-8")
    wired.contact_error = RuntimeError("nope")
    rv.main(9, workers=1)
    assert report_of(wired)["contact_sheet"] is False and not (wired.ep / "contact_sheet.png").exists()


# ---- cli -----------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(rv, "main", lambda *args: calls.append(args) or 0)
    monkeypatch.delenv("EPISODE_ID", raising=False)
    return calls


def test_cli_passes_arguments_to_main(cli_calls):
    assert rv.cli(["9", "--limit-seconds", "3.5", "--workers", "2"]) == 0
    assert cli_calls == [(9, 3.5, 2)]


def test_cli_accepts_episode_zero_without_consulting_the_environment(cli_calls, monkeypatch):
    monkeypatch.setenv("EPISODE_ID", "161")
    assert rv.cli(["0"]) == 0 and cli_calls == [(0, None, None)]


def test_cli_defaults_and_episode_id_from_the_environment(cli_calls, monkeypatch):
    monkeypatch.setenv("EPISODE_ID", "161")
    assert rv.cli([]) == 0
    assert rv.cli(["7"]) == 0
    assert cli_calls == [(161, None, None), (7, None, None)]


@pytest.mark.parametrize("env_value", [None, "", "abc"])
def test_cli_requires_an_episode_id(cli_calls, monkeypatch, env_value):
    if env_value is not None:
        monkeypatch.setenv("EPISODE_ID", env_value)
    with pytest.raises(SystemExit) as caught:
        rv.cli([])
    assert caught.value.code == 2 and not cli_calls


def test_cli_turns_a_render_error_into_exit_code_1(monkeypatch, info_logs):
    def fail(*args):
        raise rv.RenderError("missing input file: x")

    monkeypatch.setattr(rv, "main", fail)
    assert rv.cli(["9"]) == 1
    assert "missing input file: x" in info_logs.text


# ---- make_contact_sheet ----------------------------------------------------------------------------------------

class TileComposer:
    """Each compose() call returns a tile whose green channel identifies the call order."""
    fps = 30

    def __init__(self, beats):
        self.beats, self.calls = beats, []

    def compose(self, n):
        self.calls.append(n)
        return Image.new("RGB", (1920, 1080), (0, 20 + 10 * (len(self.calls) - 1), 200))


def timeline(count, length=0.5):
    return [{"id": f"b{k}", "start": k * length, "end": (k + 1) * length} for k in range(count)]


def test_contact_sheet_has_one_labelled_tile_per_beat_in_reading_order(tmp_path):
    composer = TileComposer(timeline(13))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png")
    sheet = Image.open(tmp_path / "sheet.png")
    assert sheet.size == (4 * 480, 4 * 270)
    assert composer.calls == [int((0.5 * k + 0.25) * 30) for k in range(13)]
    for k in range(13):
        assert sheet.getpixel(((k % 4) * 480 + 240, (k // 4) * 270 + 135)) == (0, 20 + 10 * k, 200)
        assert sheet.getpixel(((k % 4) * 480 + 1, (k // 4) * 270 + 1)) == (0, 0, 0)
        assert sheet.getpixel(((k % 4) * 480 + 479, (k // 4) * 270 + 269)) == (0, 20 + 10 * k, 200)
    assert sheet.getpixel((1900, 1000)) == (0, 0, 0)
    label = sheet.crop((0, 0, 190, 30))
    assert (255, 255, 0) in {label.getpixel((x, y)) for x in range(190) for y in range(30)}


def test_contact_sheet_columns_and_thumb_size_are_configurable(tmp_path):
    rv.make_contact_sheet(TileComposer(timeline(3)), tmp_path / "sheet.png", columns=2, thumb=(100, 60))
    assert image_size(tmp_path / "sheet.png") == (200, 120)


def test_contact_sheet_thins_out_long_timelines_to_the_tile_limit(tmp_path):
    composer = TileComposer(timeline(30))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png")
    assert composer.calls == [int((0.5 * k + 0.25) * 30) for k in range(0, 30, 2)]
    assert image_size(tmp_path / "sheet.png") == (4 * 480, 4 * 270)
    composer = TileComposer(timeline(40))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png")
    assert len(composer.calls) == 20
    composer = TileComposer(timeline(25))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png")
    assert len(composer.calls) == 13


def test_contact_sheet_never_has_more_tiles_than_the_limit(tmp_path):
    composer = TileComposer(timeline(7))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png", limit=5)
    assert len(composer.calls) == 4 and image_size(tmp_path / "sheet.png") == (4 * 480, 270)


def test_contact_sheet_skips_beats_after_the_end_of_the_timeline(tmp_path):
    beats = timeline(3) + [{"id": "tail", "start": 1.5, "end": 1.5}]
    composer = TileComposer(beats)
    rv.make_contact_sheet(composer, tmp_path / "sheet.png")
    assert len(composer.calls) == 3


def test_contact_sheet_for_a_limited_run_only_shows_the_rendered_part(tmp_path):
    composer = TileComposer(timeline(13))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png", total=3.2)
    assert composer.calls == [int((0.5 * k + 0.25) * 30) for k in range(6)] + [int(3.1 * 30)]


# ---- real multi-process renders: seams and fallback reporting -----------------------------------------------------

def striped_image():
    """Vertical bars: any off-by-one frame at a chunk join shifts the bars and shows up as a large difference."""
    x = np.arange(1536)
    row = (((x // 24) % 2) * 150 + (x * 90 // 1536)).astype(np.uint8)
    return Image.fromarray(np.tile(row, (1024, 1))).convert("RGB")


def test_chunks_rendered_in_parallel_join_without_a_dropped_or_duplicated_frame(episode, ffmpeg_exe):
    striped_image().save(episode / "img_s1.png")
    assert rv.main(9, limit_seconds=3.0, workers=2) == 0
    final = episode / "final.mp4"
    chunks = rv.plan_chunks(90, 4)
    assert len(chunks) == 4

    probe = subprocess.run([ffmpeg_exe, "-hide_banner", "-i", str(final), "-map", "0:v", "-vf", "showinfo", "-f", "null",
                            "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    stamps = [float(t) for t in re.findall(r"pts_time:([\d.]+)", probe)]
    assert len(stamps) == 90 and all(abs(t - k / 30) < 1e-3 for k, t in enumerate(stamps))

    starts = [first for first, _ in chunks[1:]]
    select = "+".join(f"eq(n\\,{k})" for k in starts)
    raw = subprocess.run([ffmpeg_exe, "-loglevel", "error", "-i", str(final), "-vf",
                          f"select='{select}',scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-"], capture_output=True).stdout
    decoded = np.frombuffer(raw, np.uint8).reshape(len(starts), 1080, 1920, 3).astype(int)
    composer = rv.build_composer(episode, 3.0)

    def gap(i, k):
        return float(np.abs(decoded[i] - np.asarray(composer.compose(k)).astype(int)).mean())

    for i, k in enumerate(starts):
        exact, before, after = gap(i, k), gap(i, k - 1), gap(i, k + 1)
        assert exact < 3 and exact * 2 < min(before, after), (k, before, exact, after)


def test_missing_scene_image_beats_are_reported_as_fallbacks(episode):
    (episode / "img_s2.png").unlink()
    assert rv.main(9, limit_seconds=5.0, workers=2) == 0
    report = json.loads((episode / "video_report.json").read_text(encoding="utf-8"))
    assert report["fallback_beats"] == ["b3"] and report["duration_ok"] is True
    assert report["contact_sheet"] is True and report["audio_sec"] == pytest.approx(6.0, abs=0.1)
    assert report["quality"] == {"black_segments": 0, "freeze_segments": 0}
    assert image_size(episode / "contact_sheet.png") == (4 * 480, 270)
    assert (episode / "final.mp4").exists() and not (episode / "render_tmp").exists()


# ---- more behaviours worth pinning ----------------------------------------------------------------------------

def test_mux_audio_publishes_the_finished_file_with_an_atomic_replace(tmp_path, monkeypatch):
    seen = []
    real_replace = rv.os.replace

    def spy(src, dst):
        seen.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(rv, "_run_ffmpeg", lambda cmd, what: Path(cmd[-1]).write_bytes(b"finished"))
    monkeypatch.setattr(rv.os, "replace", spy)
    out = tmp_path / "final.mp4"
    out.write_bytes(b"previous")
    rv.mux_audio(tmp_path / "v.mp4", tmp_path / "a.mp3", out, 1.0)
    assert seen == [(tmp_path / "final.part.mp4", out)] and out.read_bytes() == b"finished"


def test_mux_audio_removes_its_partial_file_when_ffmpeg_fails_midway(tmp_path, monkeypatch):
    out = tmp_path / "final.mp4"
    out.write_bytes(b"previous")

    def failing(cmd, what):
        Path(cmd[-1]).write_bytes(b"half written")
        raise rv.RenderError("mux failed (exit 1): disk full")

    monkeypatch.setattr(rv, "_run_ffmpeg", failing)
    with pytest.raises(rv.RenderError, match="disk full"):
        rv.mux_audio(tmp_path / "v.mp4", tmp_path / "a.mp3", out, 1.0)
    assert out.read_bytes() == b"previous" and not list(tmp_path.glob("*.part*"))


def test_concat_and_mux_report_a_missing_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "ffmpeg_bin", lambda: str(tmp_path / "no-such-ffmpeg"))
    with pytest.raises(rv.RenderError, match="concat: cannot run ffmpeg"):
        rv.concat_chunks([str(tmp_path / "a.mp4")], tmp_path / "out.mp4")
    with pytest.raises(rv.RenderError, match="mux: cannot run ffmpeg"):
        rv.mux_audio(tmp_path / "v.mp4", tmp_path / "a.mp3", tmp_path / "final.mp4", 1.0)
    assert not list(tmp_path.glob("*.part*"))


def test_ffmpeg_helpers_cope_with_non_ascii_paths(tmp_path, ffmpeg_exe):
    folder = tmp_path / "古今 影片"
    folder.mkdir()
    clip = make_clip(ffmpeg_exe, folder / "古今.mp4", "testsrc", frames=15, size="160x90")
    assert rv.probe(clip)["width"] == 160
    assert rv.detect_black_freeze(clip) == {"black_segments": 0, "freeze_segments": 0, "decoded_frames": 15}
    rv.concat_chunks([str(clip)], folder / "合併.mp4")
    assert decoded_frames(ffmpeg_exe, folder / "合併.mp4") == 15


def test_render_chunk_builds_its_composer_from_the_job(tmp_path, fake_encoder, monkeypatch):
    fake_encoder("""
        import sys
        while sys.stdin.buffer.read(65536):
            pass
    """)
    seen = []
    monkeypatch.setattr(rv, "build_composer", lambda ep_dir, limit: seen.append((ep_dir, limit)) or FlatComposer())
    job = {**chunk_job(tmp_path), "limit": 2.5}
    rv.render_chunk(job)
    assert seen == [(tmp_path, 2.5)] and isinstance(seen[0][0], Path)


def test_render_chunk_overwrites_a_stale_output_file(tmp_path, monkeypatch, ffmpeg_exe):
    monkeypatch.setattr(rv, "build_composer", lambda ep_dir, limit: FlatComposer())
    (tmp_path / "chunk_000.mp4").write_bytes(b"stale")
    rv.render_chunk(chunk_job(tmp_path, 0, 3))
    assert decoded_frames(ffmpeg_exe, tmp_path / "chunk_000.mp4") == 3


def test_render_chunk_does_not_wait_for_a_stuck_encoder_after_a_compose_failure(tmp_path, fake_encoder):
    fake_encoder("""
        import sys, time
        while sys.stdin.buffer.read(65536):
            pass
        time.sleep(20)
    """, composer=FlatComposer(fail_at=2))
    started = time.monotonic()
    with pytest.raises(rv.RenderError, match="composing frame 2"):
        rv.render_chunk(chunk_job(tmp_path))
    assert time.monotonic() - started < 10


def test_load_inputs_converts_images_to_rgb(tmp_path):
    Image.new("RGBA", (8, 8), (1, 2, 3, 128)).save(tmp_path / "img_s1.png")
    (tmp_path / "shots.json").write_text(json.dumps({"scenes": [{"id": "s1", "image": "img_s1.png"}], "beats": []}),
                                         encoding="utf-8")
    (tmp_path / "timings.json").write_text(json.dumps({"total": 6.0, "units": []}), encoding="utf-8")
    images = rv.load_inputs(tmp_path)[2]
    assert images["img_s1.png"].mode == "RGB" and images["img_s1.png"].getpixel((0, 0)) == (1, 2, 3)


def test_a_missing_image_report_is_normal_but_an_unreadable_one_is_logged(wired, info_logs):
    rv.main(9, workers=1)
    assert "image_report" not in info_logs.text
    (wired.ep / "image_report.json").write_text("{truncated", encoding="utf-8")
    rv.main(9, workers=1)
    assert "image_report.json" in info_logs.text


def test_report_rounds_the_audio_length_to_milliseconds(wired):
    wired.audio_sec = 5.99996
    rv.main(9, workers=1)
    assert report_of(wired)["audio_sec"] == 6.0


def test_report_render_time_is_rounded_to_a_tenth_of_a_second(wired):
    rv.main(9, workers=1)
    value = report_of(wired)["render_sec"]
    assert value == round(value, 1)


def test_contact_sheet_labels_each_tile_with_the_beat_id_and_time(tmp_path):
    rv.make_contact_sheet(TileComposer(timeline(6)), tmp_path / "sheet.png")
    sheet = Image.open(tmp_path / "sheet.png")
    try:
        font = rv.load_font(22, True)
    except FileNotFoundError:
        font = ImageFont.load_default()
    for k, text in ((0, "b0    0.2s"), (5, "b5    2.8s")):
        expected = Image.new("RGB", (480, 270), (0, 20 + 10 * k, 200))
        draw = ImageDraw.Draw(expected)
        draw.rectangle((0, 0, 190, 30), fill=(0, 0, 0))
        draw.text((6, 3), text, font=font, fill=(255, 255, 0))
        x, y = (k % 4) * 480, (k // 4) * 270
        assert np.array_equal(np.asarray(sheet.crop((x, y, x + 200, y + 40))), np.asarray(expected.crop((0, 0, 200, 40))))


# ---------------------------------------------------------------------------
# Fix round 1: composer wiring, stale files, worker cap, audio check, frame count, log noise.

# Real output of the bundled ffmpeg 7.1: the closing stats line of a null-muxer decode (CR-separated progress).
FFMPEG_STATS_LOG = (
    "[blackdetect @ 000002661e83f800] black_start:0 black_end:1.5 black_duration:1.5\r\n"
    "[out#0/null @ 000002661e7975c0] video:45KiB audio:0KiB subtitle:0KiB other streams:0KiB global headers:0KiB "
    "muxing overhead: unknown\r\n"
    "frame=  105 fps=0.0 q=-0.0 Lsize=N/A time=00:00:03.50 bitrate=N/A speed= 134x    \r\n")


@pytest.mark.parametrize("text,expected", [
    (FFMPEG_STATS_LOG, 105),
    (FFMPEG_DETECT_LOG, 225),
    ("frame=   12 fps=0.0 q=-0.0 size=N/A time=00:00:00.40 bitrate=N/A speed=0.8x    \r"
     "frame=   99 fps=90 q=-0.0 size=N/A time=00:00:03.30 bitrate=N/A speed=3x    \r"
     "frame=  105 fps=0.0 q=-0.0 Lsize=N/A time=00:00:03.50 bitrate=N/A speed= 134x    \n", 105),
    ("frame=0 fps=0.0\n", 0),
    ("", None),
    ("Press [q] to stop\nkeyframe=7 reframe= 9\n", None)])
def test_parse_decoded_frames_takes_the_last_frame_counter(text, expected):
    assert rv.parse_decoded_frames(text) == expected


# ---- build_composer wiring (a mutant here desynchronises the video or drops its subtitles unnoticed) -------------

def test_build_composer_hands_the_composer_the_real_cues_the_clamped_total_and_the_output_format(tmp_path,
                                                                                                  monkeypatch):
    for name in ("img_s1.png", "img_s2.png"):
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(tmp_path / name)
    units = [{"id": k, "section": 1, "text": "天地玄黃宇宙洪荒日月盈昃，辰宿列張寒來暑往秋收冬藏。", "start": 2.0 * k, "end": 2.0 * (k + 1)}
             for k in range(3)]
    shots = {"style": "ink", "scenes": [{"id": "s1", "image": "img_s1.png"}, {"id": "s2", "image": "img_s2.png"}],
             "beats": []}
    (tmp_path / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    (tmp_path / "timings.json").write_text(json.dumps({"total": 6.0, "units": units}, ensure_ascii=False),
                                           encoding="utf-8")
    seen = []

    def recorder(shots, images, cues, total, size=None, fps=None):
        seen.append({"shots": shots, "images": images, "cues": cues, "total": total, "size": size, "fps": fps})
        return "the composer"

    monkeypatch.setattr(rv, "FrameComposer", recorder)
    assert rv.build_composer(tmp_path) == "the composer"
    rv.build_composer(tmp_path, 2.5)
    rv.build_composer(tmp_path, 99)
    full, limited, over = seen
    assert (full["size"], full["fps"]) == ((1920, 1080), 30)
    assert (limited["size"], limited["fps"]) == ((1920, 1080), 30)
    assert [full["total"], limited["total"], over["total"]] == [6.0, 2.5, 6.0]
    assert full["shots"] == shots and sorted(full["images"]) == ["img_s1.png", "img_s2.png"]
    assert all(image.mode == "RGB" for image in full["images"].values())
    assert full["cues"] == subtitles.build_cues(units) and len(full["cues"]) == 6
    assert limited["cues"] == full["cues"]


# ---- the subtitles must really be burned into the frames --------------------------------------------------------

def test_the_rendered_video_has_burned_in_subtitles_in_the_bottom_band(episode, ffmpeg_exe):
    assert rv.main(9, limit_seconds=1.5, workers=1) == 0
    frame = player_frame(ffmpeg_exe, episode / "final.mp4", 30, (1920, 1080)).astype(int)

    def white_pixels(area):
        return int((area > 225).all(axis=2).sum())

    assert white_pixels(frame[940:1030, 200:1720]) > 300
    assert white_pixels(frame[0:300]) == 0


# ---- mux / concat overwrite leftovers of a killed run (ffmpeg without -y refuses and still exits 0) ------------

def test_mux_audio_overwrites_a_leftover_part_file_instead_of_publishing_it(tmp_path, ffmpeg_exe):
    video = make_clip(ffmpeg_exe, tmp_path / "v.mp4", "testsrc", frames=30)
    audio = make_mp3(ffmpeg_exe, tmp_path / "a.mp3", 2)
    (tmp_path / "final.part.mp4").write_bytes(b"junk from a killed run")
    out = tmp_path / "final.mp4"
    rv.mux_audio(video, audio, out, 1.0)
    info = rv.probe(out)
    assert info["has_audio"] and info["width"] == 64 and info["duration"] == pytest.approx(1.0, abs=0.15)
    assert out.stat().st_size > 1000 and not list(tmp_path.glob("*.part*"))


def test_concat_chunks_overwrites_a_leftover_output(tmp_path, ffmpeg_exe):
    clip = make_clip(ffmpeg_exe, tmp_path / "c.mp4", "testsrc", frames=15)
    out = tmp_path / "video.mp4"
    out.write_bytes(b"junk from a killed run")
    rv.concat_chunks([str(clip)], out)
    assert decoded_frames(ffmpeg_exe, out) == 15


def test_mux_audio_keeps_the_full_video_length_when_the_audio_is_shorter(tmp_path, ffmpeg_exe):
    video = make_clip(ffmpeg_exe, tmp_path / "v.mp4", "testsrc", frames=90)
    audio = make_mp3(ffmpeg_exe, tmp_path / "a.mp3", 1)
    out = tmp_path / "final.mp4"
    rv.mux_audio(video, audio, out, 3.0)
    info = rv.probe(out)
    assert info["duration"] == pytest.approx(3.0, abs=0.15) and info["has_audio"]
    assert decoded_frames(ffmpeg_exe, out) == 90


# ---- contact sheet: exactly as many beats as the tile limit -----------------------------------------------------

def test_contact_sheet_shows_every_beat_when_there_are_exactly_as_many_as_the_limit(tmp_path):
    composer = TileComposer(timeline(24))
    rv.make_contact_sheet(composer, tmp_path / "sheet.png")
    assert len(composer.calls) == 24 and image_size(tmp_path / "sheet.png") == (4 * 480, 6 * 270)


# ---- RENDER_WORKERS / --workers upper bound ---------------------------------------------------------------------

@pytest.mark.parametrize("cpus,cap", [(16, 32), (4, 8), (1, 2), (None, 2), (40, 61), (31, 61), (30, 60)])
def test_workers_are_capped_at_twice_the_cpu_count_and_never_above_61(wired, monkeypatch, cpus, cap):
    monkeypatch.setattr(rv.os, "cpu_count", lambda: cpus)
    assert rv.main(9, limit_seconds=2.0, workers=cap) == 0
    assert wired.pools[-1].max_workers == cap
    with pytest.raises(rv.RenderError, match=rf"workers must be an integer between 1 and {cap}, got {cap + 1}"):
        rv.main(9, limit_seconds=2.0, workers=cap + 1)
    assert len(wired.pools) == 1


def test_render_workers_environment_is_capped_too_before_any_work(wired, monkeypatch):
    monkeypatch.setenv("RENDER_WORKERS", "33")
    with pytest.raises(rv.RenderError, match=r"RENDER_WORKERS must be an integer between 1 and 32, got '33'"):
        rv.main(9)
    assert not wired.pools and not (wired.ep / "render_tmp").exists()
    monkeypatch.setenv("RENDER_WORKERS", "32")
    assert rv.main(9, limit_seconds=2.0) == 0 and wired.pools[-1].max_workers == 32


# ---- the audio file is validated before the expensive render ----------------------------------------------------

@pytest.mark.parametrize("audio_sec,audio_has", [(None, False), (None, True), (6.0, False)])
def test_an_unusable_audio_file_fails_before_any_worker_starts(wired, audio_sec, audio_has):
    wired.audio_sec, wired.audio_has = audio_sec, audio_has
    with pytest.raises(rv.RenderError, match=r"audio_full\.mp3"):
        rv.main(9, workers=1)
    assert not wired.pools and not wired.jobs and not (wired.ep / "render_tmp").exists()


@pytest.mark.parametrize("content", [b"", b"this is not audio at all"])
def test_a_real_zero_byte_or_garbage_audio_file_is_rejected_up_front(wired, monkeypatch, ffmpeg_exe, content):
    monkeypatch.setattr(rv, "probe", REAL_PROBE)
    (wired.ep / "audio_full.mp3").write_bytes(content)
    with pytest.raises(rv.RenderError, match=r"audio_full\.mp3"):
        rv.main(9, workers=1)
    assert not wired.pools and not (wired.ep / "render_tmp").exists()


# ---- stale outputs ----------------------------------------------------------------------------------------------

STALE_OUTPUTS = ("final.mp4", "final.part.mp4", "subtitles.srt", "video_report.json", "contact_sheet.png")


def test_a_failed_rerun_leaves_none_of_the_previous_outputs_behind(wired):
    for name in STALE_OUTPUTS:
        (wired.ep / name).write_bytes(b"from the previous run")
    wired.fail_job = 0
    with pytest.raises(rv.RenderError):
        rv.main(9, workers=1)
    assert [name for name in STALE_OUTPUTS if (wired.ep / name).exists()] == []
    assert (wired.ep / "audio_full.mp3").exists() and (wired.ep / "shots.json").exists()


def test_a_rerun_that_fails_input_validation_does_not_touch_the_previous_outputs(wired):
    for name in STALE_OUTPUTS:
        (wired.ep / name).write_bytes(b"from the previous run")
    (wired.ep / "timings.json").write_text('{"total": 0, "units": []}', encoding="utf-8")
    with pytest.raises(rv.RenderError):
        rv.main(9, workers=1)
    assert all((wired.ep / name).read_bytes() == b"from the previous run" for name in STALE_OUTPUTS)


def test_a_successful_rerun_replaces_the_previous_outputs_and_leaves_no_part_file(wired):
    for name in STALE_OUTPUTS:
        (wired.ep / name).write_bytes(b"from the previous run")
    assert rv.main(9, workers=1) == 0
    assert (wired.ep / "final.mp4").stat().st_size == 1_234_567 and not (wired.ep / "final.part.mp4").exists()
    assert b"from the previous run" not in (wired.ep / "subtitles.srt").read_bytes()
    assert report_of(wired)["frames"] == 180


# ---- decoded frame count ----------------------------------------------------------------------------------------

def test_the_report_records_the_decoded_frame_count_and_whether_it_matches(wired, info_logs):
    assert rv.main(9, limit_seconds=4.0, workers=1) == 0
    report = report_of(wired)
    assert (report["frames"], report["decoded_frames"], report["frames_ok"]) == (120, 120, True)
    assert report["quality"] == QUIET_QUALITY
    assert "幀數" not in info_logs.text


@pytest.mark.parametrize("decoded", [179, 181, 0])
def test_a_frame_count_mismatch_is_reported_and_warned_about(wired, info_logs, decoded):
    wired.decoded = decoded
    assert rv.main(9, workers=1) == 0
    report = report_of(wired)
    assert (report["frames"], report["decoded_frames"], report["frames_ok"]) == (180, decoded, False)
    assert report["duration_ok"] is True
    assert "幀數" in info_logs.text and str(decoded) in info_logs.text and "180" in info_logs.text


def test_an_unknown_decoded_frame_count_is_null_and_not_a_mismatch(wired, info_logs):
    wired.decoded = None
    assert rv.main(9, workers=1) == 0
    report = report_of(wired)
    assert (report["decoded_frames"], report["frames_ok"]) == (None, None)
    assert "幀數" not in info_logs.text


# ---- log format -------------------------------------------------------------------------------------------------

def test_the_duration_warning_prints_the_audio_length_to_milliseconds(wired, info_logs):
    wired.audio_sec = 6.30000000001
    assert rv.main(9, workers=1) == 0
    assert "6.300" in info_logs.text and "6.30000000001" not in info_logs.text and "相差超過" in info_logs.text


def test_an_unreadable_scene_image_is_logged_once_not_once_per_worker(episode, info_logs, capfd):
    (episode / "img_s2.png").unlink()
    assert rv.main(9, limit_seconds=5.0, workers=2) == 0
    in_main = sum("img_s2.png" in record.getMessage() for record in info_logs.records)
    in_workers = sum("img_s2.png" in line for line in capfd.readouterr().err.splitlines())
    assert (in_main, in_workers) == (1, 0)
