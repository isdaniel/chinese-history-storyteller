"""Stage: 並行渲染 shots.json → final.mp4。

畫面是時間的純函式 (render_timeline.FrameComposer),所以把絕對幀範圍切成多段,
各程序獨立渲染並編碼成暫存 MP4,最後用 ffmpeg concat 接起來並混入 audio_full.mp3。
另外輸出 subtitles.srt、contact_sheet.png (每個鏡頭一張縮圖) 與 video_report.json (含品質檢查)。

用法: python render_video.py [episode_id] [--limit-seconds N] [--workers N]
"""
import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import env, ffmpeg_bin, get_episode_dir, load_json, save_json, setup_logging
from render_overlays import load_font
from render_timeline import FrameComposer
from subtitles import build_cues, write_srt

log = setup_logging("render_video")

W, H, FPS = 1920, 1080, 30
DURATION_TOLERANCE_SEC = 0.1
INPUT_FILES = ("shots.json", "timings.json", "audio_full.mp3")
MAX_POOL_WORKERS = 61  # ProcessPoolExecutor 在 Windows 的上限
STALE_OUTPUTS = ("final.mp4", "subtitles.srt", "video_report.json", "contact_sheet.png")
UNKNOWN_CHECKS = {"black_segments": None, "freeze_segments": None, "decoded_frames": None}
# RGB 轉 YUV 明確用 BT.709 並標記;否則 swscale 預設用 BT.601 且不留標記,播放器對 HD 假設 BT.709,紅綠會偏色。
# 濾鏡 (新版 ffmpeg 以畫面屬性為準) 與輸出參數 (舊版以參數為準) 兩種標記都給。
COLOR_FILTER = ("scale=out_color_matrix=bt709:out_range=tv,"
                "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv")
COLOR_ARGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


class RenderError(RuntimeError):
    pass


def _tail(text, limit: int = 800) -> str:
    return (text or "").strip()[-limit:]


def _whole_number(value, name: str, low: int, high: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        number = None
    if number is None or number < low or (high is not None and number > high):
        wanted = f"between {low} and {high}" if high is not None else f">= {low}"
        raise RenderError(f"{name} must be an integer {wanted}, got {value!r}")
    return number


def max_workers() -> int:
    return min(MAX_POOL_WORKERS, 2 * (os.cpu_count() or 1))


def part_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")


def plan_chunks(total_frames: int, parts: int) -> list[tuple[int, int]]:
    """把 [0, total_frames) 切成最多 parts 段連續區間,每個影格恰好屬於一段。"""
    parts = max(1, min(parts, total_frames))
    base, extra = divmod(total_frames, parts)
    chunks, start = [], 0
    for k in range(parts):
        size = base + (1 if k < extra else 0)
        chunks.append((start, start + size))
        start += size
    return chunks


def render_seconds(timings: dict, limit_seconds=None) -> float:
    try:
        full = float(timings["total"])
    except (KeyError, TypeError, ValueError):
        raise RenderError("timings.json needs a numeric 'total'") from None
    if not (math.isfinite(full) and full > 0):
        raise RenderError(f"timings.json total must be positive and finite, got {full!r}")
    return full if limit_seconds is None else min(limit_seconds, full)


def read_scene_image(ep_dir: Path, name: str) -> Image.Image:
    with Image.open(ep_dir / name) as image:
        return image.convert("RGB")


def unreadable_scene_ids(ep_dir: Path, shots: dict) -> set[str]:
    """圖檔遺失或損毀的場景 (render 時會被純色圖取代);每張壞圖只在這裡記一次 error,各程序不重複記。"""
    bad = set()
    for scene in shots["scenes"]:
        try:
            read_scene_image(ep_dir, scene["image"])
        except (OSError, ValueError) as exc:
            log.error("圖片 %s 讀取失敗 (%s),改用純色備援", scene["image"], exc)
            bad.add(scene["id"])
    return bad


def load_inputs(ep_dir: Path, limit_seconds=None):
    shots = load_json(ep_dir / "shots.json")
    timings = load_json(ep_dir / "timings.json")
    total = render_seconds(timings, limit_seconds)
    images = {}
    for scene in shots["scenes"]:
        try:
            images[scene["image"]] = read_scene_image(ep_dir, scene["image"])
        except (OSError, ValueError) as exc:
            log.debug("圖片 %s 讀取失敗 (%s),改用純色備援", scene["image"], exc)
            images[scene["image"]] = Image.new("RGB", (1536, 1024), (60, 50, 40))
    return shots, timings, images, build_cues(timings["units"]), total


def build_composer(ep_dir: Path, limit_seconds=None) -> FrameComposer:
    shots, _, images, cues, total = load_inputs(ep_dir, limit_seconds)
    return FrameComposer(shots, images, cues, total, size=(W, H), fps=FPS)


def render_chunk(job: dict) -> dict:
    """在子程序中渲染一段影格並編碼成無聲 MP4。ffmpeg 的 stderr 寫進檔案 (不會塞住管線),失敗時附上結尾。"""
    start, end = job["start"], job["end"]
    span = f"frames {start}-{end}"
    composer = build_composer(Path(job["ep_dir"]), job["limit"])
    out = Path(job["out"])
    log_path = out.with_suffix(".log")
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an",
           "-vf", COLOR_FILTER,
           "-c:v", "libx264", "-preset", "medium", "-crf", str(job["crf"]), "-pix_fmt", "yuv420p",
           *COLOR_ARGS, str(out)]
    with open(log_path, "wb") as ffmpeg_log:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=ffmpeg_log)
        except OSError as exc:
            raise RenderError(f"cannot start ffmpeg for {span}: {exc}") from exc
        try:
            for n in range(start, end):
                try:
                    frame = composer.compose(n).tobytes()
                except Exception as exc:
                    raise RenderError(f"composing frame {n} failed ({span}): {exc!r}") from exc
                proc.stdin.write(frame)
            proc.stdin.close()
            returncode = proc.wait()
        except BaseException as exc:
            proc.kill()
            try:
                proc.stdin.close()
            except OSError:
                pass
            returncode = proc.wait()
            if isinstance(exc, OSError):
                raise RenderError(f"ffmpeg stopped reading {span} (exit {returncode}): "
                                  f"{_tail(log_path.read_text(encoding='utf-8', errors='replace'))}") from exc
            raise
    if returncode != 0:
        raise RenderError(f"ffmpeg failed while encoding {span} (exit {returncode}): "
                          f"{_tail(log_path.read_text(encoding='utf-8', errors='replace'))}")
    return {"out": job["out"], "frames": end - start, "fallback_beats": sorted(composer.fallback_beats)}


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RenderError(f"{what}: cannot run ffmpeg: {exc}") from exc
    if result.returncode != 0:
        raise RenderError(f"{what} failed (exit {result.returncode}): {_tail(result.stderr)}")


def concat_chunks(chunks: list[str], out_path: Path) -> None:
    list_file = out_path.with_suffix(".txt")
    # concat 清單的相對路徑是相對清單檔本身;一律寫絕對路徑,單引號依 ffmpeg 規則跳脫成 '\''
    lines = ["file '" + Path(c).absolute().as_posix().replace("'", "'\\''") + "'" for c in chunks]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run_ffmpeg([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(list_file), "-c", "copy", str(out_path)], "concat")


def mux_audio(video: Path, audio: Path, out_path: Path, seconds: float) -> None:
    """先寫成同資料夾的暫存檔再 os.replace,失敗時不會留下寫到一半的 final.mp4。"""
    part = part_path(out_path)
    try:
        _run_ffmpeg([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio),
                     "-t", f"{seconds:.3f}", "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(part)], "mux")
        os.replace(part, out_path)
    finally:
        part.unlink(missing_ok=True)


def parse_probe_output(text: str) -> dict:
    duration = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    video = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    return {"duration": int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]) if duration else None,
            "width": int(video[1]) if video else None, "height": int(video[2]) if video else None,
            "has_audio": "Audio:" in text}


def probe(path: Path) -> dict:
    """用 `ffmpeg -i` 的輸出取得長度、解析度、是否有音軌 (本機不一定有 ffprobe);跑不起來就回傳未知值。"""
    try:
        text = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True,
                              encoding="utf-8", errors="replace").stderr
    except OSError as exc:
        log.warning("無法探測 %s: %s", Path(path).name, exc)
        text = ""
    return parse_probe_output(text)


def parse_detect_output(stderr: str) -> dict:
    return {"black_segments": stderr.count("black_start"), "freeze_segments": stderr.count("freeze_start")}


def parse_decoded_frames(stderr: str) -> int | None:
    """ffmpeg 結尾的統計行 `frame=  105 fps=...`;取最後一個計數 (進度列以 CR 分隔)。"""
    counts = re.findall(r"\bframe=\s*(\d+)", stderr)
    return int(counts[-1]) if counts else None


def detect_black_freeze(video: Path) -> dict:
    """同一趟解碼同時數黑畫面/靜止段數與實際解出的幀數;只是診斷,ffmpeg 跑不起來或失敗就回報未知 (None)。"""
    try:
        result = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(video), "-vf",
                                 "blackdetect=d=0.5:pic_th=0.98,freezedetect=n=-60dB:d=3", "-an", "-f", "null", "-"],
                                capture_output=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("黑畫面/靜止檢查無法執行: %s", exc)
        return dict(UNKNOWN_CHECKS)
    if result.returncode != 0:
        log.warning("黑畫面/靜止檢查失敗 (exit %d): %s", result.returncode, _tail(result.stderr))
        return dict(UNKNOWN_CHECKS)
    return {**parse_detect_output(result.stderr), "decoded_frames": parse_decoded_frames(result.stderr)}


def fallback_beat_ids(shots: dict, image_report, motion_fallbacks, broken_scenes=()) -> list[str]:
    """使用備援的鏡頭:所屬場景的圖用了備援圖 (圖片階段回報,或渲染時讀不到圖檔),或運動計算失敗退回推近。"""
    bad_scenes = set(broken_scenes)
    entries = image_report.get("scenes") if isinstance(image_report, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and entry.get("status") == "fallback" and isinstance(entry.get("scene"), str):
            bad_scenes.add(entry["scene"])
    return sorted({b["id"] for b in shots["beats"] if b["scene"] in bad_scenes} | set(motion_fallbacks))


def make_contact_sheet(composer: FrameComposer, out_path: Path, columns: int = 4, thumb=(480, 270),
                       limit: int = 24, total=None) -> None:
    end = composer.beats[-1]["end"] if total is None else total
    beats = [b for b in composer.beats if b["start"] < end]
    step = max(1, math.ceil(len(beats) / limit))
    chosen = beats[::step][:limit]
    rows = math.ceil(len(chosen) / columns)
    sheet = Image.new("RGB", (columns * thumb[0], rows * thumb[1]))
    try:
        font = load_font(22, True)
    except FileNotFoundError:
        font = ImageFont.load_default()
    for k, beat in enumerate(chosen):
        middle = (beat["start"] + min(beat["end"], end)) / 2
        tile = composer.compose(int(middle * composer.fps)).resize(thumb)
        draw = ImageDraw.Draw(tile)
        draw.rectangle((0, 0, 190, 30), fill=(0, 0, 0))
        draw.text((6, 3), f"{beat['id']}  {middle:5.1f}s", font=font, fill=(255, 255, 0))
        sheet.paste(tile, ((k % columns) * thumb[0], (k // columns) * thumb[1]))
    sheet.save(out_path)


def _load_input_json(ep_dir: Path, name: str) -> dict:
    try:
        data = load_json(ep_dir / name)
    except (OSError, ValueError) as exc:
        raise RenderError(f"cannot read {name}: {exc}") from exc
    if not isinstance(data, dict):
        raise RenderError(f"{name} must contain a JSON object")
    return data


def _load_image_report(ep_dir: Path):
    path = ep_dir / "image_report.json"
    try:
        return load_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("image_report.json 無法讀取,略過 (%s)", exc)
        return None


def _render_parallel(jobs: list[dict], workers: int) -> list[dict]:
    """依 jobs 的順序回傳各段結果 (不論完成順序);任一段失敗就取消尚未開始的段並丟 RenderError。"""
    results = [None] * len(jobs)
    began = time.time()
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(render_chunk, job): k for k, job in enumerate(jobs)}
            try:
                for done, future in enumerate(as_completed(futures), 1):
                    results[futures[future]] = future.result()
                    log.info("第 %d/%d 段完成 (%.0fs)", done, len(jobs), time.time() - began)
            except BaseException:
                pool.shutdown(wait=True, cancel_futures=True)
                raise
    except Exception as exc:
        raise RenderError(f"parallel render failed: {exc}") from exc
    return results


def main(episode_id: int, limit_seconds=None, workers=None) -> int:
    started = time.time()
    if limit_seconds is not None:
        try:
            limit_seconds = float(limit_seconds)
        except (TypeError, ValueError):
            limit_seconds = math.nan
        if not (math.isfinite(limit_seconds) and limit_seconds > 0):
            raise RenderError("limit_seconds must be a positive, finite number of seconds")
    if workers is None:
        workers = _whole_number(env("RENDER_WORKERS", str(min(4, os.cpu_count() or 1)), required=False),
                                "RENDER_WORKERS", 1, max_workers())
    else:
        workers = _whole_number(workers, "workers", 1, max_workers())
    crf = _whole_number(env("VIDEO_CRF", "22", required=False), "VIDEO_CRF", 1, 51)

    ep_dir = get_episode_dir(episode_id)
    for name in INPUT_FILES:
        if not (ep_dir / name).is_file():
            raise RenderError(f"missing input file: {ep_dir / name}")
    audio_path = ep_dir / "audio_full.mp3"
    audio = probe(audio_path)
    if audio["duration"] is None or not audio["has_audio"]:
        raise RenderError(f"audio file has no readable audio stream: {audio_path}")
    audio_sec = audio["duration"]
    shots = _load_input_json(ep_dir, "shots.json")
    timings = _load_input_json(ep_dir, "timings.json")
    total = render_seconds(timings, limit_seconds)
    total_frames = round(total * FPS)
    if total_frames < 1:
        raise RenderError(f"nothing to render: {total:.3f}s is less than one frame")
    try:
        cues = build_cues(timings["units"])
        composer = build_composer(ep_dir, limit_seconds)
        broken_scenes = unreadable_scene_ids(ep_dir, shots)
    except Exception as exc:
        raise RenderError(f"invalid render inputs: {type(exc).__name__}: {exc}") from exc

    final = ep_dir / "final.mp4"
    for stale in (*STALE_OUTPUTS, part_path(final).name):
        (ep_dir / stale).unlink(missing_ok=True)
    tmp = ep_dir / "render_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    jobs = [{"ep_dir": str(ep_dir), "limit": limit_seconds, "start": a, "end": b, "crf": crf,
             "out": str(tmp / f"chunk_{k:03d}.mp4")}
            for k, (a, b) in enumerate(plan_chunks(total_frames, workers * 2))]
    log.info("渲染 %d 幀 (%.1fs),%d 個程序,%d 段", total_frames, total, workers, len(jobs))
    try:
        results = _render_parallel(jobs, workers)
        concat_chunks([r["out"] for r in results], tmp / "video.mp4")
        mux_audio(tmp / "video.mp4", audio_path, final, total)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    write_srt([c for c in cues if c[0] < total], ep_dir / "subtitles.srt")
    try:
        make_contact_sheet(composer, ep_dir / "contact_sheet.png", total=total)
        contact_sheet = True
    except Exception as exc:
        log.warning("contact sheet 產生失敗,影片不受影響: %s: %s", type(exc).__name__, exc)
        (ep_dir / "contact_sheet.png").unlink(missing_ok=True)
        contact_sheet = False

    info = probe(final)
    checks = detect_black_freeze(final)
    decoded_frames = checks["decoded_frames"]
    quality = {"black_segments": checks["black_segments"], "freeze_segments": checks["freeze_segments"]}
    frames_ok = None if decoded_frames is None else decoded_frames == total_frames
    fallbacks = fallback_beat_ids(shots, _load_image_report(ep_dir), {b for r in results for b in r["fallback_beats"]},
                                  broken_scenes)
    video_ok = info["duration"] is not None and abs(info["duration"] - total) <= DURATION_TOLERANCE_SEC
    audio_ok = abs(audio_sec - float(timings["total"])) <= DURATION_TOLERANCE_SEC
    duration_ok = video_ok and audio_ok
    report = {"seconds": round(total, 3), "frames": total_frames, "decoded_frames": decoded_frames,
              "frames_ok": frames_ok, "workers": workers, "crf": crf,
              "render_sec": round(time.time() - started, 1), "size_mb": round(final.stat().st_size / 1e6, 1),
              "video": info, "audio_sec": round(audio_sec, 3),
              "duration_ok": duration_ok, "beats": len(shots["beats"]),
              "fallback_beats": fallbacks, "quality": quality, "contact_sheet": contact_sheet}
    save_json(ep_dir / "video_report.json", report)
    if not duration_ok:
        log.warning("影片長度 %s、音檔 %.3f 與時間軸 %.3f 相差超過 %.1fs", info["duration"], audio_sec, total,
                    DURATION_TOLERANCE_SEC)
    if frames_ok is False:
        log.warning("影片實際幀數 %d 與預期 %d 不符", decoded_frames, total_frames)
    log.info("完成: %s (%.1f MB,%.0fs;備援鏡頭 %d/%d;黑畫面 %s 段、靜止 %s 段)", final.name, report["size_mb"],
             report["render_sec"], len(fallbacks), len(shots["beats"]), quality["black_segments"],
             quality["freeze_segments"])
    return 0


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="並行渲染 shots.json → final.mp4")
    parser.add_argument("episode_id", nargs="?", type=int)
    parser.add_argument("--limit-seconds", type=float, default=None)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args(argv)
    episode_id = args.episode_id
    if episode_id is None:
        raw = os.environ.get("EPISODE_ID", "")
        if not raw.isdigit():
            parser.error("episode_id is required (argument or EPISODE_ID)")
        episode_id = int(raw)
    try:
        return main(episode_id, args.limit_seconds, args.workers)
    except RenderError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
