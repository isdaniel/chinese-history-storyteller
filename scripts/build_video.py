"""Step 4: 用 FFmpeg 把插畫 + 配音 + 字幕合成 MP4。

每段插畫對應該段配音時長,加上 Ken Burns 緩慢縮放效果。
字幕從 timings.json 拆成短句寫成 .srt。

輸入: output/ep{id}/img_*.png + audio_*.mp3 + timings.json
輸出: output/ep{id}/final.mp4
       output/ep{id}/subtitles.srt
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from common import get_episode_dir, load_json, setup_logging

log = setup_logging("build_video")

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

W, H, FPS = 1920, 1080, 30


def fmt_srt_time(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def split_to_subtitles(text: str, start: float, duration: float) -> list:
    """把一段旁白依標點切成短句,在 duration 內等比分配時間。"""
    sentences = [s.strip() for s in re.split(r"(?<=[。!?,;])", text) if s.strip()]
    if not sentences:
        return []
    total_chars = sum(len(s) for s in sentences) or 1
    cues = []
    cursor = start
    for s in sentences:
        d = duration * (len(s) / total_chars)
        cues.append((cursor, cursor + d, s))
        cursor += d
    return cues


def write_srt(timings: dict, out_path: Path) -> None:
    cues = []
    for sec in timings["sections"]:
        cues.extend(split_to_subtitles(sec["narration"], sec["start"], sec["duration"]))
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        lines.append(f"{i}\n{fmt_srt_time(a)} --> {fmt_srt_time(b)}\n{text}\n")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("字幕寫入 %s (%d 條)", out_path.name, len(cues))


def build_image_clip(img_path: Path, duration: float, out_path: Path) -> None:
    """單張圖 + Ken Burns 縮放 → 無聲影片片段。"""
    zoom_frames = max(1, int(duration * FPS))
    vf = (
        f"scale=2400:1350,zoompan=z='min(zoom+0.0008,1.15)':"
        f"d={zoom_frames}:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    subprocess.run([
        FFMPEG, "-y", "-loop", "1", "-i", str(img_path),
        "-t", f"{duration:.3f}", "-vf", vf, "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(out_path),
    ], check=True, capture_output=True)


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    timings = load_json(ep_dir / "timings.json")

    srt_path = ep_dir / "subtitles.srt"
    write_srt(timings, srt_path)

    # 1. 為每段建立有 Ken Burns 效果的無聲影片片段
    clips = []
    for sec in timings["sections"]:
        idx = sec["section_id"]
        img = ep_dir / f"img_{idx:02d}.png"
        if not img.exists():
            raise FileNotFoundError(img)
        clip = ep_dir / f"clip_{idx:02d}.mp4"
        log.info("[%d] 建立片段 (%.1fs)…", idx, sec["duration"])
        build_image_clip(img, sec["duration"], clip)
        clips.append(clip)

    # 2. 拼接無聲影片
    list_file = ep_dir / "video_list.txt"
    list_file.write_text("\n".join(f"file '{c.as_posix()}'" for c in clips), encoding="utf-8")
    silent_video = ep_dir / "silent.mp4"
    subprocess.run([
        FFMPEG, "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(silent_video),
    ], check=True, capture_output=True)

    # 3. 加上音訊與燒入字幕
    final = ep_dir / "final.mp4"
    audio = ep_dir / "audio_full.mp3"
    # FFmpeg subtitles filter 需要 forward slashes 並轉義
    srt_for_filter = str(srt_path.as_posix()).replace(":", "\\:")
    # SUBTITLE_FONT defaults to a font present on both Windows ("Microsoft JhengHei")
    # and Linux GitHub runners ("Noto Sans CJK TC", installed via fonts-noto-cjk).
    font = os.environ.get("SUBTITLE_FONT", "Noto Sans CJK TC")
    vf = (
        f"subtitles='{srt_for_filter}':force_style="
        f"'FontName={font},FontSize=22,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=60'"
    )
    log.info("最終合成 (含字幕 + 音訊)…")
    subprocess.run([
        FFMPEG, "-y", "-i", str(silent_video), "-i", str(audio),
        "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(final),
    ], check=True)

    log.info("✅ 完成: %s", final)

    # 清理中間檔
    for c in clips:
        c.unlink(missing_ok=True)
    silent_video.unlink(missing_ok=True)
    list_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
