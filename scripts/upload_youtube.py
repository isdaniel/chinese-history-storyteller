"""Step 5a: 用 OAuth refresh token 自動上傳影片到 YouTube。

需先一次性手動取得 refresh_token (見 docs/MANUAL_SETUP.md)。
"""
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from common import env, get_episode_dir, load_json, setup_logging

log = setup_logging("upload_youtube")


def get_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )


def build_description(script: dict, timings: dict) -> str:
    base = script.get("description", "")
    chapters = ["\n\n📖 章節時間軸:"]
    for sec in timings["sections"]:
        m = int(sec["start"] // 60)
        s = int(sec["start"] % 60)
        first_line = sec["narration"].split("。")[0][:30]
        chapters.append(f"{m:02d}:{s:02d} {first_line}")
    chapters.append("\n#中文歷史 #說書 #歷史故事")
    return base + "\n".join(chapters)


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")
    timings = load_json(ep_dir / "timings.json")
    video_path = ep_dir / "final.mp4"
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    youtube = build("youtube", "v3", credentials=get_credentials())

    body = {
        "snippet": {
            "title": script["title"][:100],
            "description": build_description(script, timings)[:5000],
            "tags": script.get("tags", [])[:15],
            "categoryId": "22",  # People & Blogs (歷史內容適用,Education=27 也可)
            "defaultLanguage": "zh-Hant",
            "defaultAudioLanguage": "zh-Hant",
        },
        "status": {
            "privacyStatus": os.environ.get("YOUTUBE_PRIVACY", "public"),
            "selfDeclaredMadeForKids": False,
            "license": "youtube",
            "embeddable": True,
        },
    }

    log.info("開始上傳: %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6)
    media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                             chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("已上傳 %d%%", int(status.progress() * 100))

    video_id = response["id"]
    url = f"https://youtu.be/{video_id}"
    log.info("✅ 完成: %s", url)

    # 寫回 episode dir
    info_path = ep_dir / "youtube_info.json"
    info_path.write_text(
        f'{{"video_id": "{video_id}", "url": "{url}"}}', encoding="utf-8"
    )

    gh_env = Path(os.environ.get("GITHUB_ENV", "/dev/null"))
    if gh_env.parent.exists():
        try:
            with open(gh_env, "a", encoding="utf-8") as f:
                f.write(f"YOUTUBE_VIDEO_ID={video_id}\nYOUTUBE_URL={url}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
