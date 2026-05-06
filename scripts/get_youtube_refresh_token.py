"""一次性工具:用 Desktop OAuth client 走 localhost flow 拿 refresh_token。

跑法:
  pip install google-auth-oauthlib
  python get_youtube_refresh_token.py

它會開瀏覽器讓你登入授權,完成後印出 refresh_token。
"""
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID") or input("Client ID: ").strip()
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET") or input("Client Secret: ").strip()

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    },
    scopes=["https://www.googleapis.com/auth/youtube.upload"],
)

# access_type=offline 才會回傳 refresh_token; prompt=consent 強迫每次都重發 refresh
creds = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",
    authorization_prompt_message="開啟瀏覽器授權...",
    success_message="OK,可以關掉這個瀏覽器分頁。",
)

print("\n" + "=" * 60)
print("授權完成,複製下面的 refresh_token 設成 GitHub secret YOUTUBE_REFRESH_TOKEN:")
print("=" * 60)
print(creds.refresh_token)
print("=" * 60)
