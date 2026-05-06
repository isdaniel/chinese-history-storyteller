# 中文歷史說書 - AI 自動發布 Pipeline

每日一集,從題材生成到 YouTube + Podcast 發布全自動。

---

## 這個專案做什麼

每次排程觸發 (預設每天台灣時間早上 7:00),GitHub Actions 會自動完成:

1. **選題** — 從 `data/topics_queue.json` 取下一個未發布題材
2. **寫稿** — Azure OpenAI `gpt-5-mini` 生成 12 分鐘 8 段式中文旁白
3. **配圖** — Azure `gpt-image-2` 為每段生成統一風格水墨插畫
4. **配音** — Azure Speech Neural TTS (預設 `zh-CN-YunjianNeural` 沉穩男聲)
5. **剪片** — FFmpeg 把圖片+配音+字幕合成 1080p MP4 (Ken Burns 效果)
6. **YouTube 上架** — 透過 YouTube Data API v3 自動上傳
7. **Podcast 發布** — 上傳 MP3 到 Azure Blob + 自動更新 RSS feed
8. **Discord 通知** — (選用) 發布完成發訊息

---

## 架構

- **Infra as Code**: Terraform (`infra/`),含 Azure 資源、Entra ID App Registration、Federated Credentials、RBAC
- **Auth**: 完全走 Entra ID (OIDC),無 storage key / API key 流通
- **State**: Remote backend in Azure Storage (`stgsttftateaihistory/tfstate`),use_azuread_auth=true
- **Pipeline**: GitHub Actions 排程 + workflow_dispatch 手動觸發

---

## 快速部署 (新環境從零開始)

### 步驟 1: 前置條件
- Azure subscription (要能建 Cognitive Services 資源,先到 https://aka.ms/oai/access 申請 OpenAI 存取權)
- Google 帳號 + YouTube 頻道
- GitHub 帳號 + 一個 public repo
- 本機裝好: `az` CLI、`terraform` >= 1.5、`gh` CLI、Python 3.11+

### 步驟 2: Bootstrap state storage (一次性)
依照 `infra/README.md` 的「Bootstrap from scratch」章節用 az cli 建立:
- `rg-storyteller-tfstate` resource group
- `stgsttftateaihistory` storage account (shared key disabled、Entra ID auth)
- `tfstate` container
- 給自己 `Storage Blob Data Owner` role

### 步驟 3: Terraform 部署
```bash
az login --tenant <your-tenant-id>

cd infra
cp terraform.tfvars.example terraform.tfvars
# 編輯 terraform.tfvars 填入:
#   subscription_id        = "你的訂閱 ID"
#   current_user_object_id = "az ad signed-in-user show --query id -o tsv"
#   name_suffix            = "你的後綴 (例: ai-history)"
#   github_repo            = "你的GitHub帳號/chinese-history-storyteller"

terraform init
terraform apply
```

完成後 16 個 Azure 資源就建好了 (RG、OpenAI + 2 deployments、Speech、Storage、App Registration、Federated Credentials、RBAC)。

### 步驟 4: YouTube OAuth 設定
依 [docs/MANUAL_SETUP.md](docs/MANUAL_SETUP.md) B 章節完成:
- 在 Google Cloud Console 建專案、啟用 YouTube Data API v3
- 建 OAuth 2.0 client (Desktop app)
- 跑 `python scripts/get_youtube_refresh_token.py` 拿 `refresh_token`

### 步驟 5: 灌 GitHub Secrets

PowerShell:
```powershell
# 一次灌 14 個 Azure secrets (從 terraform output 直接抓值)
.\scripts\set_github_secrets.ps1

# 額外手動加 YouTube + Podcast secrets
$REPO = "你的GitHub帳號/chinese-history-storyteller"
gh secret set YOUTUBE_CLIENT_ID       -R $REPO -b "OAuth client ID"
gh secret set YOUTUBE_CLIENT_SECRET   -R $REPO -b "OAuth secret"
gh secret set YOUTUBE_REFRESH_TOKEN   -R $REPO -b "refresh token"
gh secret set YOUTUBE_CHANNEL_ID      -R $REPO -b "UC..."
gh secret set PODCAST_TITLE           -R $REPO -b "中文歷史說書"
gh secret set PODCAST_AUTHOR          -R $REPO -b "你的名字"
gh secret set PODCAST_EMAIL           -R $REPO -b "你的 email"
gh secret set PODCAST_BASE_URL        -R $REPO -b "https://你的帳號.github.io/chinese-history-storyteller"
gh secret set PODCAST_LANGUAGE        -R $REPO -b "zh-tw"
gh secret set PODCAST_CATEGORY        -R $REPO -b "History"
# 選用:
gh secret set DISCORD_WEBHOOK_URL     -R $REPO -b "https://discord.com/api/webhooks/..."

# 驗證
gh secret list -R $REPO
```

### 步驟 6: 首次手動觸發測試
1. 進到 GitHub repo → **Actions** tab
2. 選 **Publish Episode** workflow
3. 點 **Run workflow** → privacy 選 `unlisted` (先不公開)
4. 等 ~10-15 分鐘
5. 檢查 YouTube 後台是否有新影片、`podcast.xml` 是否更新

---

## 重複部署 / 修改資源

只要改 Terraform 重 apply 就好:
```bash
cd infra
terraform plan
terraform apply
```

state 在 Azure 不在本機,可在任何裝置上 `terraform init` 接續操作 (前提是有 RBAC)。

如果改了 secrets 來源 (例如 storage key 輪換、redeploy 換新 endpoint),重跑 `set_github_secrets.ps1` 同步到 GitHub。

---

## 常見問題

### Azure login (OIDC) 失敗 — `Not all values are present. Ensure 'client-id' and 'tenant-id' are supplied`
GitHub repo 的 secrets 沒設好。跑 `gh secret list -R OWNER/REPO` 確認 14 個 Azure secrets 都在,不在的話跑 `.\scripts\set_github_secrets.ps1`。

### Azure login (OIDC) 失敗 — `AADSTS70021: No matching federated identity record`
Federated credential 的 subject 跟實際 GitHub repo 對不上。
- 在 `terraform.tfvars` 確認 `github_repo = "OWNER/REPO"` 跟你 push 的 repo 一致
- `terraform apply` 重建 federated credentials

### YouTube 401 / `invalid_grant`
refresh token 失效 (Testing 模式 7 天閒置會過期)。重跑 `python scripts/get_youtube_refresh_token.py` 拿新的,然後 `gh secret set YOUTUBE_REFRESH_TOKEN -R OWNER/REPO -b "<新的 token>"`。Workflow 已配置失敗時自動發 Discord 通知 (如有設 webhook)。

### `gpt-image-2` `DeploymentNotFound`
你的 Azure OpenAI 不在支援區域。`gpt-image-2` 只在 Sweden Central / East US 2 / West US 3 開放。Terraform 預設用 Sweden Central。

### 本機 `terraform output` 拿到舊資料
PATH 中可能有舊版 terraform。確認 `terraform version` >= 1.5,需要 import block 與 backend 支援。

---

## 內容策略

### 三大主軸 (避免演算法判定為重複公式)
| 類別 | 比例 | 範例題材 |
|------|------|---------|
| 中華歷史 | 50% | 三國真相、玄武門之變、明朝滅亡 |
| 科技史 | 30% | Nokia 隕落、Tesla 崛起、ChatGPT 革命 |
| 世界文明 | 15% | 馬雅消失、龐貝、拜占庭 |
| 歷史謎團 | 5% | 雍正之死、建文帝下落 |

題材庫已預先準備 100 集 (約 1 年份),在 `data/topics_queue.json`。

### YouTube 政策合規
- AI 揭露:在 Studio 設定預設揭露 "Altered Content"
- **不要** 標記 `selfDeclaredMadeForKids: true` (歷史內容受眾為成人)
- 控制發布頻率,避免被判定為 Content Farm
- 第 8 段強制包含「現代啟示」原創觀點

---

## 本地測試

不一定要在 GitHub 上跑,你也可以本機測試 (前提:terraform 已 apply、az login 過):

```bash
pip install -r requirements.txt

# 從 terraform output 取出環境變數 (Linux/Mac/git-bash)
cd infra
export AZURE_OPENAI_ENDPOINT=$(terraform output -raw azure_openai_endpoint)
export AZURE_OPENAI_GPT_DEPLOYMENT=$(terraform output -raw azure_openai_gpt_deployment)
export AZURE_OPENAI_IMAGE_DEPLOYMENT=$(terraform output -raw azure_openai_image_deployment)
export AZURE_OPENAI_API_VERSION="2024-10-21"
export AZURE_SPEECH_REGION=$(terraform output -raw azure_speech_region)
export AZURE_SPEECH_RESOURCE_ID=$(terraform output -raw azure_speech_resource_id)
export AZURE_SPEECH_CUSTOM_DOMAIN=$(terraform output -raw azure_speech_custom_domain)
export AZURE_SPEECH_VOICE="zh-CN-YunjianNeural"
export AZURE_STORAGE_CONNECTION_STRING=$(terraform output -raw azure_storage_connection_string)
export AZURE_STORAGE_CONTAINER=$(terraform output -raw azure_storage_container)
export AZURE_BLOB_PUBLIC_URL_BASE=$(terraform output -raw azure_blob_public_url_base)
cd ..

# 跑單集
cd scripts
python generate_script.py
# 印出 EPISODE_ID=1,後續腳本接著跑:
EPISODE_ID=1 python generate_images.py
EPISODE_ID=1 python synthesize_speech.py
EPISODE_ID=1 python build_video.py
EPISODE_ID=1 python upload_youtube.py    # 真的會上傳!先把 YOUTUBE_PRIVACY=private
EPISODE_ID=1 python publish_podcast.py
```

Windows PowerShell 把 `export` 改 `$env:VAR=...`、`$()` 改 `(terraform output -raw ...)`, 可以下載某集 output 資料夾來檢查腳本品質、替換不滿意的圖片再重跑 `build_video.py`。

### 出錯時的 debug
- GitHub Actions 失敗時會自動上傳 `output/` 資料夾為 artifact (保留 7 天,不含 mp4)
- 看 Actions log 找到失敗在哪一步
- 大部分問題是金鑰過期、RBAC 沒生效、或 Azure 配額用完

### 內容調校
- **不滿意 GPT 產出**:改 `templates/script_prompt.txt`
- **想換配音**:改 `AZURE_SPEECH_VOICE` secret (建議 `zh-CN-YunyangNeural` 新聞主播風 / `zh-TW-YunJheNeural` 台灣腔)
- **想換插畫風格**:改 `scripts/generate_images.py` 的 `STYLE_SUFFIX`
- **想換 image quality (省錢)**:改 `scripts/generate_images.py` 的 `quality="medium"` → `"low"` (~3x 省)
