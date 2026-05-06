"""Step 6 (optional): 發布完成後通知 Discord。"""
import os
import sys

from common import env, get_episode_dir, load_json, notify_discord, setup_logging

log = setup_logging("notify")


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")
    yt_url = (load_json(ep_dir / "youtube_info.json").get("url")
              if (ep_dir / "youtube_info.json").exists() else "(未上傳)")
    msg = (
        f" **新集數已發布**\n"
        f" {script['title']}\n"
        f" YouTube: {yt_url}\n"
        f" Podcast: 已更新 RSS feed"
    )
    notify_discord(msg)
    log.info(msg)
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
