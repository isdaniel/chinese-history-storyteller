"""共用工具模組:設定載入、檔案處理、日誌"""
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
ASSETS_DIR = PROJECT_ROOT / "assets"

OUTPUT_DIR.mkdir(exist_ok=True)


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return logging.getLogger(name)


def env(key: str, default: str | None = None, required: bool = True) -> str:
    val = os.environ.get(key)
    if not val:  # treat unset and empty-string the same (GitHub Actions secrets that aren't set become "")
        val = default
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_episode_dir(episode_id: int) -> Path:
    """每集獨立資料夾,方便除錯與保留歷史檔案。"""
    d = OUTPUT_DIR / f"ep{episode_id:04d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pick_next_topic() -> dict:
    """從 queue 取下一個未發布題材。剩餘量低於閾值時發 Discord 警告。"""
    queue = load_json(DATA_DIR / "topics_queue.json")
    log = load_json(DATA_DIR / "published_log.json")
    published_ids = {entry["id"] for entry in log["published"]}

    remaining = [t for t in queue["topics"] if t["id"] not in published_ids]
    if not remaining:
        notify_discord(
            "chinese-history-storyteller: 題材庫已用盡!\n"
            "請執行 `python scripts/replenish_topics.py` 補新題目,或手動編輯 data/topics_queue.json"
        )
        raise RuntimeError("題材庫已用盡 — 請更新 topics_queue.json")

    threshold = int(os.environ.get("TOPIC_LOW_THRESHOLD", "21"))
    if len(remaining) <= threshold:
        msg = (
            f"chinese-history-storyteller: 題材庫剩 {len(remaining)} 集 (閾值 {threshold})。\n"
            "請盡快執行 `python scripts/replenish_topics.py` 補新題目。"
        )
        logging.getLogger("pick_next_topic").warning(msg)
        notify_discord(msg)

    return remaining[0]


def count_remaining_topics() -> int:
    """回傳 queue 中尚未發布的題目數 (供 workflow 判斷是否要補題)。"""
    queue = load_json(DATA_DIR / "topics_queue.json")
    log = load_json(DATA_DIR / "published_log.json")
    published_ids = {entry["id"] for entry in log["published"]}
    return sum(1 for t in queue["topics"] if t["id"] not in published_ids)


def mark_published(episode_id: int, info: dict) -> None:
    log = load_json(DATA_DIR / "published_log.json")
    log["published"].append({
        "id": episode_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
        **info,
    })
    save_json(DATA_DIR / "published_log.json", log)


def notify_discord(message: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    try:
        import requests
        from retry import with_backoff

        def _post() -> None:
            r = requests.post(webhook, json={"content": message}, timeout=10)
            r.raise_for_status()

        # Discord webhook 有 per-route rate limit;失敗不阻塞主流程,但盡力重試
        with_backoff(_post, max_attempts=3, base_sec=2.0, max_sec=15.0,
                     jitter_sec=1.0, op_name="discord webhook")
    except Exception:
        pass
