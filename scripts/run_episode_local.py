"""本機 dry-run:依序執行各階段並產出成品,供使用者在本機驗證。

不會上傳 YouTube、不會更新 Podcast RSS、不會寫 published_log.json、不會 commit、不會發 Discord 通知
(執行期間 DISCORD_WEBHOOK_URL 會被清空;本檔不 import upload_youtube / publish_podcast / notify)。

用法 (在 repo 根目錄):
    python scripts/run_episode_local.py --topic-id 161 --short
    python scripts/run_episode_local.py --topic-id 161 --from-stage images
    python scripts/run_episode_local.py --topic-id 161 --only video --limit-seconds 30
需要先 `az login`,並在 .env 設定 AZURE_OPENAI_ENDPOINT 等變數 (見 .env.example)。
"""
import argparse
import importlib
import math
import os
import sys
import traceback

STAGES = ("script", "tts", "plan", "images", "ground", "video")

SHORT_MODE_ENV = {
    "SECTIONS_PER_EPISODE": "2", "TARGET_LENGTH_MIN": "2", "MIN_LENGTH_MIN": "1",
    "MIN_SECTIONS": "2", "MAX_IMAGES": "8",
}

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def build_env(topic_id: int, short: bool, style: str, mode: str = "shots") -> dict:
    values = {"TOPIC_ID": str(topic_id), "VIDEO_MODE": mode, "VISUAL_STYLE": style}
    if short:
        values.update(SHORT_MODE_ENV)
    return values


def select_stages(from_stage: str | None, only: str | None) -> list[str]:
    if only:
        return [only]
    start = STAGES.index(from_stage) if from_stage else 0
    return list(STAGES[start:])


def _call_main(module_name: str, *args, **kwargs) -> None:
    result = importlib.import_module(module_name).main(*args, **kwargs)
    if result not in (0, None):
        raise RuntimeError(f"{module_name} 回傳非零結束碼 {result!r}")


def run_stage(name: str, episode_id: int, limit_seconds) -> None:
    if name == "script":
        _call_main("generate_script")
    elif name == "tts":
        _call_main("synthesize_speech", episode_id)
    elif name == "plan":
        _call_main("plan_shots", episode_id)
    elif name == "images":
        _call_main("generate_images", episode_id)
    elif name == "ground":
        _call_main("ground_focus", episode_id)
    elif name == "video":
        if limit_seconds is not None:
            _call_main("render_video", episode_id, limit_seconds=limit_seconds)
        else:
            _call_main("make_video", episode_id)
    else:
        raise ValueError(f"unknown stage {name!r}")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"需要大於 0 的整數,收到 {text!r}")
    return value


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"需要大於 0 的有限秒數,收到 {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--topic-id", type=_positive_int, required=True,
                        help="data/topics_queue.json 的題材 id (同時是集數資料夾編號)")
    parser.add_argument("--short", action="store_true", help="約 2 分鐘、2 段旁白、最多 8 張圖,省時省錢")
    parser.add_argument("--style", choices=("ink", "cinematic"), default="cinematic")
    parser.add_argument("--from-stage", choices=STAGES)
    parser.add_argument("--only", choices=STAGES, help="只跑這一階段 (同時給 --from-stage 時以 --only 為準)")
    parser.add_argument("--limit-seconds", type=_positive_seconds, default=None,
                        help="只渲染前 N 秒 (只對 video 階段有效,且直接用分鏡渲染器、不走降級)")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # 舊的 Windows 代碼頁印不出中文時,以 ? 取代而不是整個程式崩潰
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    stages = select_stages(args.from_stage, args.only)
    if args.only and args.from_stage:
        print(f"注意:同時給了 --only 與 --from-stage,以 --only {args.only} 為準,忽略 --from-stage", flush=True)
    if args.limit_seconds is not None and "video" not in stages:
        print("注意:--limit-seconds 只對 video 階段有效,這次不會執行 video,已忽略", flush=True)

    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    os.environ.update(build_env(args.topic_id, args.short, args.style))
    # 本機驗證要看到分鏡階段的錯誤:預設不降級 (想測降級流程時自己設 SHOTS_STRICT=0)
    os.environ.setdefault("SHOTS_STRICT", "1")
    # dotenv 不會覆蓋已存在的變數:設成空字串,本機 .env 裡的真實 webhook 就不會在 dry-run 時收到降級/備援警報
    os.environ["DISCORD_WEBHOOK_URL"] = ""
    for stage in stages:
        print(f"==== {stage}", flush=True)
        try:
            run_stage(stage, args.topic_id, args.limit_seconds)
        except Exception as exc:
            traceback.print_exc()
            print(f"==== FAILED {stage}: {exc}", file=sys.stderr, flush=True)
            return 1
    from common import get_episode_dir
    print(f"\n完成。成品與報告在 {get_episode_dir(args.topic_id)}:final.mp4、contact_sheet.png、video_report.json、"
          "plan_report.json、image_report.json、subtitles.srt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
