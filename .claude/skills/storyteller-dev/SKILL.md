---
name: storyteller-dev
description: 開發 chinese-history-storyteller 專案時使用 (每日中文歷史 YouTube/Podcast 自動發布 pipeline:腳本→配音→分鏡→產圖→渲染→上傳)。修改 scripts/、tests/、.github/workflows/、README/MANUAL_SETUP、跑測試或 `python -m pytest -q` 失敗、處理 GitHub Actions 失敗、調整 VIDEO_MODE/VISUAL_STYLE/MAX_IMAGES、shots_guard 重試降級、YouTube 上傳設定時,先讀這份。Use when developing, testing, debugging CI, or changing the pipeline/workflows/docs of this repo.
---

# chinese-history-storyteller 開發指南

每日排程 (GitHub Actions) 產出一集 ~9 分鐘的繁體中文歷史影片,上傳 YouTube 並發布 Podcast。
預設走 **shots 模式** (逐句分鏡 + 運鏡 + 字卡 + 字幕),失敗會自動降級為舊的圖片輪播 (**slideshow**)。

## 0. 先做這件事:用對 Python

**`python -m pytest -q` 常失敗的第一個原因:跑到系統 Python。** 系統 `python` 沒有任何依賴 (`No module named pytest`),專案依賴都在 repo 的 `.venv`。

```bash
# Git Bash (Windows)
.venv/Scripts/python.exe -m pytest -q          # 不必 activate,最穩
# 或先啟用
source .venv/Scripts/activate && python -m pytest -q
# PowerShell
.venv\Scripts\python.exe -m pytest -q
```

沒有 `.venv` 時:`python -m venv .venv` → `.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt`
(requirements-dev 有 `pytest`、`pytest-timeout`、`imageio-ffmpeg`、`pyyaml`;`pytest.ini` 設了 `timeout = 180`、`timeout_method = thread`,少了 `pytest-timeout` 會有警告)。

## 1. 專案地圖

| 階段 | 腳本 | 產出 (`output/ep{id:04d}/`,git 忽略) |
|---|---|---|
| 1 腳本 | `generate_script.py` | `script.json` (同時把 `EPISODE_ID` 寫進 `GITHUB_ENV`) |
| 2 配音 | `synthesize_speech.py` | `audio_NN.mp3`、`audio_full.mp3`、`timings.json` (`sections`/`total` 給上傳章節用,另有 `units`/`words`/`timing_report`) |
| 3 規劃 | `plan_shots.py` | `shots.json`、`plan_report.json` |
| 4 產圖 | `generate_images.py` | `img_sN.png` (shots) 或 `img_NN.png` (slideshow)、`image_report.json` |
| 5 聚焦 | `ground_focus.py` | 改寫 `shots.json` 的 `focus`、`ground_report.json` |
| 6 影片 | `make_video.py` → `render_video.py` / `build_video.py` | `final.mp4`、`subtitles.srt`、`contact_sheet.png`、`video_report.json` |
| 7 上傳 | `upload_youtube.py` | `youtube_info.json` |
| 8 Podcast | `publish_podcast.py` | Blob、`podcast.xml`、`data/published_log.json` |
| 9 通知 | `notify.py` | Discord 訊息 (含製作統計) |

輔助模組:`common.py` (env、`get_episode_dir`、`save_json` 為原子寫入、`notify_discord`)、`llm_client.py` (Azure OpenAI)、`retry.py`、`visual_style.py`、`shot_plan.py`/`beats.py`/`timing_units.py` (純邏輯)、`render_motion/overlays/timeline.py` (幀合成)、`subtitles.py`、`shots_guard.py` (重試與降級)、`run_episode_local.py` (本機 dry-run)。

## 2. 測試

- 指令:`.venv/Scripts/python.exe -m pytest -q`,約 2 分鐘。提交前一定要用 `-W error` 再跑一次:`... -m pytest -q -W error`。
- 只跑一個檔或關鍵字:`... -m pytest tests/test_workflows.py -q -k synthetic`。
- **所有測試都必須離線**:不呼叫 Azure/YouTube/Discord/網路,不真的 sleep。假物件在 `tests/fakes.py`;輸出目錄用 `monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)`。
- `tests/conftest.py` 有兩個全域機制:
  - `no_real_retry_sleep` (autouse):把 `shots_guard.time` 換成不睡的版本。**不要**在 autouse fixture 裡用 `monkeypatch`,會打亂其他檔案 fixture 的還原順序。
  - CI 保護:環境變數 `CI` 有值時,因缺 ffmpeg/中文字型/SIGALRM 而被 skip 的測試會讓整個 session 失敗。新的 skip 一律寫成 `pytest.skip("<原因>")` (單行、不能有行尾註解),且原因字串必須同時加進 `conftest.MISSING_TOOL_SKIP_REASONS` 與 `tests/test_workflows.py` 的 `GUARDED_SKIP_REASONS`,否則元測試 `test_every_skip_in_the_suite_is_one_the_ci_guard_fails_on` 會失敗。
- 在本機被跳過的只有 `test_a_stage_that_hangs_is_interrupted_for_real` (Windows 沒有 SIGALRM);它只會在 Linux CI 真的執行,改 `shots_guard._deadline` 時要特別小心。
- 新行為要先寫測試 (RED)→實作 (GREEN),並自己做「變異檢查」:在 repo 外的暫存副本改掉一個常數/分支,確認測試會失敗。

### 最常讓測試變紅的原因:文件與 workflow 的文字被測試釘住

`tests/test_workflows.py` 會讀 `README.md`、`docs/MANUAL_SETUP.md`、`.env.example`、`publish.yml`、`test.yml`,並斷言其中的**句子、表格列、章節字母、預設值、步驟順序**與程式碼一致。所以:

- 改 README/MANUAL_SETUP/.env.example/workflow 的任何文字前,先 `grep -n "那句話" tests/*.py`。
- 刪掉或改寫一句被釘住的話 → 同步改測試。釘選是為了防止文件與程式碼脫節,**改程式碼後也要回頭看對應的文件段落**。
- 新增環境變數:程式碼讀它 (`env("NAME")` 或 `os.environ.get("NAME")`) + README 變數表加一列 + `tests/test_workflows.py` 的 `DOCUMENTED_ENV` + (若 workflow 要設)`PUBLISH_ENV_KEYS`。
- 新增 workflow 步驟:改 `PUBLISH_STEP_NAMES` 與相關順序/`if` 測試;新增腳本要有 `if __name__ == "__main__"` 且從 `EPISODE_ID` 或 argv 取集數;第三方 import 必須在 `requirements*.txt`。
- `test.yml` 的 `paths:` 必須涵蓋測試會讀的檔案 (README、MANUAL_SETUP、`.env.example`、publish.yml…),否則只改這些檔的 PR 不會觸發測試。
- 這陣子 3 次 CI 失敗都是這一類:文件、workflow 文字或常數被改了,測試釘選沒有同步 (含 `timeout-minutes` 的值)。

## 3. GitHub Actions (publish.yml / test.yml) 注意事項

- 步驟順序固定:script → TTS → plan → images → ground → make_video → upload → podcast → notify → commit → artifacts。`generate_script` 把 `EPISODE_ID` 寫進 `GITHUB_ENV`,後面每步靠它。
- 所有腳本步驟都要 `working-directory: scripts`,且**不要**加 `continue-on-error` 或 `if` (降級邏輯在腳本裡,不在 workflow)。
- 排程觸發時 `inputs` 是空的:環境變數一律寫成 `${{ inputs.x || '預設' }}`。現行預設:`VIDEO_MODE=shots`、`VISUAL_STYLE=cinematic`、`MAX_IMAGES=24`;`workflow_dispatch` 的輸入預設要與這些一致。
- Podcast 與 Commit 兩步要有 `if: ${{ github.event_name != 'workflow_dispatch' || inputs.publish_podcast }}`;但 **`upload_youtube.py` 手動測試也會真的上傳** (可見度用 `privacy`,測試請選 `private`)。
- artifact 名稱必須含 `${{ github.run_attempt }}`:Re-run 沿用同一個 `run_id`,artifact 又不可覆寫,沒有後綴會 409 讓整個 job 變紅。
- `timeout-minutes` 目前 **180** (使用者設定)。單階段時限在 `shots_guard.STAGE_MAX_SEC` (規劃 1800、產圖 5400、聚焦 900 秒),產圖整體上限 `IMAGE_STAGE_MAX_SEC` (4500)。改任何一個都要同步 README 文字與測試 (`test_the_documented_time_limits_are_the_ones_the_guard_enforces`)。
- CI 環境與本機不同:**Linux、Python 3.11、apt ffmpeg 6.x、Noto Sans CJK (TC 索引 3)**;本機是 Windows、Python 3.14、imageio-ffmpeg 7.1、微軟正黑體。避免只有 3.12+ 才有的語法/API (巢狀同型引號的 f-string、`itertools.batched`、`Path.walk`…),不要在程式碼裡假設 ffprobe (本機沒有,用 `ffmpeg -i` 解析)。
- Replenish job 不要動 (`test_replenish_job_is_untouched`)。
- 看 CI 失敗:`gh auth status` (未登入就請使用者 `gh auth login`) → `gh run view <run-id> --log-failed` → 找 `FAILED` → 本機用 `.venv/Scripts/python.exe -m pytest <檔> -q -k <名稱>` 重現 → 修 → 全部測試。

## 4. shots 模式、降級與重試 (`shots_guard.py`)

降級階梯:shots → 規劃保底 (每段一個場景) → 舊 slideshow。
- 規劃 / 產圖 / 聚焦各自重試 `SHOTS_STAGE_ATTEMPTS` 次 (預設 3,間隔 30、60 秒);超時或單次跑超過 10 分鐘才失敗就不再重試。
- 規劃或產圖最終失敗 → 寫 `shots_degraded.json`、發 Discord,`make_video` 直接組裝圖片輪播 (規劃失敗時產圖階段改走舊流程每段一張圖);渲染失敗也寫 marker 並降級;聚焦失敗只發 Discord、鏡頭置中。
- `SHOTS_STRICT=1` 時重試用完直接拋錯 (本機 runner 預設為 1,要測降級請設 `SHOTS_STRICT=0`)。
- 時限靠 SIGALRM,**只在 Linux 的主執行緒有效**,Windows 本機不會逾時。
- `MAX_IMAGES` (預設 24) 是軟上限:需要的場景數超過時放寬到 2 倍 (48),並在 `plan_report.json` 的 `budget` 記錄;更多就整集保底。
- 風格:`ink` 水墨 / `cinematic` 寫實 (預設)。`upload_youtube.py` 固定送 `containsSyntheticMedia: false` (使用者決定;YouTube 仍可能自己加「由 AI 生成」標籤)。
- 預算參考:約 72 秒/張圖、每張約 $0.06;31 張圖的一集整個 job 約 70 分鐘,渲染約 15 分鐘 (4 vCPU)。

## 5. 本機驗證 (使用者要求:先在本機驗證,再 commit/push)

```bash
az login                                   # 登入會過期,錯誤是 "Can't find token from MSAL cache"
cp .env.example .env                       # 只需端點,不放金鑰;.env 已被 git 忽略
.venv/Scripts/python.exe scripts/run_episode_local.py --topic-id 162 --short     # 約 16 分鐘、約 $0.4
```
- 不上傳、不更新 RSS、不寫發布紀錄、不發 Discord (會清空 `DISCORD_WEBHOOK_URL`);成品在 `output/ep0162/` (`final.mp4`、`contact_sheet.png`、各種 `*_report.json`)。
- 題材 id 用還沒有成品的號碼,避免覆蓋既有的 `output/epXXXX` (那些是除錯與測試的真實資料)。
- 真實 Azure 會花錢也要登入,**執行前先跟使用者確認**。

## 6. Windows / 編碼 / 換行陷阱

- 工作樹是 **CRLF** (`core.autocrlf=true`)。用腳本改檔時要保留換行 (讀成 bytes → 以 `\n` 比對 → 寫回前轉回 `\r\n`);`git diff` 出現 "LF will be replaced by CRLF" 警告是正常的。單行替換用 Edit 工具即可。
- 在 Git Bash 印中文要設 `PYTHONIOENCODING=utf-8`,否則 cp1252 會 `UnicodeEncodeError`。
- `output/` 與 `.env` 是 git 忽略的;**不要把測試寫到真實的 `output/`**。`common.py` 載入時會建立 `output/`、讀 `.env`,所以測試不能依賴 `.env` 的真實值。
- Git Bash 下 `grep $'\r'` 不可靠,判斷換行用 `file <檔案>`。

## 7. 常見失敗速查

| 症狀 | 原因 | 處理 |
|---|---|---|
| `No module named pytest` | 跑到系統 Python | 用 `.venv/Scripts/python.exe -m pytest -q` |
| `test_workflows.py` 某個 `assert '某句話' in README...` | 改了文件/workflow 文字沒改釘選 | `grep` 那句話,同步改測試或文件 |
| `assert 180 == 150` 之類 | `timeout-minutes` 或常數被改 | 同步 README 文字與測試 |
| CI 紅但本機綠 | 本機還沒 pull 使用者的改動、Linux/3.11 差異、CI 保護把 skip 當失敗 | 先 `git log`/pull,再讀 `--log-failed` |
| `is not covered by the CI guard` | 新增了 `pytest.skip`/`skipif` 但原因沒登記 | 見第 2 節 |
| `Can't find token from MSAL cache` | `az login` 過期 | 請使用者執行 `! az login` |
| Re-run 後 artifact 409 | artifact 名稱缺 `run_attempt` | 名稱加 `${{ github.run_attempt }}` |
| 測試花很久 (數十秒的單一測試) | 測試真的 sleep 了 (重試間隔 30/60 秒) | 確認 `no_real_retry_sleep` 沒被繞過,或測試有 patch `time.sleep` |

## 8. 與使用者協作的約定

- 使用者介面文字 (Discord、log、文件) 用繁體中文,台灣用語。
- **未經明確要求不要 commit/push**;使用者常用 IDE 自行提交,動手前先 `git status`/`git log`,別覆蓋他們的編輯 (README 被他們刪過一行,測試就要跟著放寬而不是加回去)。
- 動到生產行為 (workflow、上傳、重試、預設值) 時,在最終回覆說明風險與你驗證了什麼、沒驗證什麼 (例如只有 Linux CI 能驗的路徑)。
