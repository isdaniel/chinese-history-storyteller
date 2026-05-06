"""Step 5b: 上傳 mp3 到 Azure Blob,然後重新生成 podcast.xml RSS。

流程:
  1. 把 audio_full.mp3 上傳到公開 Blob container
  2. 讀 published_log.json,把所有已發布集數寫進 RSS 2.0 + iTunes namespace 的 xml
  3. RSS xml 路徑:repo 根目錄 podcast.xml (透過 GitHub Pages 公開)
"""
import os
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings
from feedgen.feed import FeedGenerator
from mutagen.mp3 import MP3

from common import (
    DATA_DIR, PROJECT_ROOT, env, get_episode_dir, load_json, mark_published,
    save_json, setup_logging,
)

log = setup_logging("publish_podcast")


def upload_audio(episode_id: int, audio_path: Path) -> str:
    conn = env("AZURE_STORAGE_CONNECTION_STRING")
    container = env("AZURE_STORAGE_CONTAINER", "podcast-episodes")
    base_url = env("AZURE_BLOB_PUBLIC_URL_BASE")
    blob_name = f"ep{episode_id:04d}.mp3"

    svc = BlobServiceClient.from_connection_string(conn)
    blob_client = svc.get_blob_client(container=container, blob=blob_name)
    log.info("上傳 %s → blob %s", audio_path.name, blob_name)
    with open(audio_path, "rb") as f:
        blob_client.upload_blob(
            f, overwrite=True,
            content_settings=ContentSettings(content_type="audio/mpeg"),
        )
    return f"{base_url.rstrip('/')}/{blob_name}"


def build_rss(out_path: Path) -> None:
    log_data = load_json(DATA_DIR / "published_log.json")
    fg = FeedGenerator()
    fg.load_extension("podcast")

    title = env("PODCAST_TITLE", "中文歷史說書")
    fg.title(title)
    fg.author({"name": env("PODCAST_AUTHOR", "Storyteller"),
               "email": env("PODCAST_EMAIL", "noreply@example.com")})
    fg.link(href=env("PODCAST_BASE_URL", "https://example.com"), rel="alternate")
    fg.language(env("PODCAST_LANGUAGE", "zh-tw"))
    fg.description("AI 說書頻道,深度解析中華歷史、世界文明與科技史。每週兩集,涵蓋三國、明清、科技公司興衰與失落文明。")
    fg.podcast.itunes_category(env("PODCAST_CATEGORY", "History"))
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_author(env("PODCAST_AUTHOR", "Storyteller"))
    fg.podcast.itunes_summary("AI 自動製作的中文歷史說書 Podcast")

    for entry in sorted(log_data["published"], key=lambda x: x["published_at"]):
        fe = fg.add_entry()
        fe.id(entry["audio_url"])
        fe.title(entry["title"])
        fe.description(entry.get("description", ""))
        fe.enclosure(entry["audio_url"], str(entry.get("size_bytes", 0)), "audio/mpeg")
        pub_dt = datetime.fromisoformat(entry["published_at"])
        fe.pubDate(format_datetime(pub_dt))
        fe.podcast.itunes_duration(entry.get("duration_str", "10:00"))
        fe.podcast.itunes_explicit("no")

    fg.rss_file(str(out_path), pretty=True)
    log.info("RSS 已寫入 %s (%d 集)", out_path, len(log_data["published"]))


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")
    audio = ep_dir / "audio_full.mp3"
    if not audio.exists():
        raise FileNotFoundError(audio)

    audio_url = upload_audio(episode_id, audio)
    log.info("公開 URL: %s", audio_url)

    mp3 = MP3(str(audio))
    duration_sec = int(mp3.info.length)
    duration_str = f"{duration_sec // 60:02d}:{duration_sec % 60:02d}"

    yt_info = {}
    yt_info_path = ep_dir / "youtube_info.json"
    if yt_info_path.exists():
        yt_info = load_json(yt_info_path)

    mark_published(episode_id, {
        "title": script["title"],
        "description": script.get("description", ""),
        "audio_url": audio_url,
        "youtube_url": yt_info.get("url", ""),
        "size_bytes": audio.stat().st_size,
        "duration_str": duration_str,
        "tags": script.get("tags", []),
    })

    build_rss(PROJECT_ROOT / "podcast.xml")
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
