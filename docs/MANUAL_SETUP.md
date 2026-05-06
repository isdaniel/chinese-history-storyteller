# 手動設定步驟 (一次性,約 3-4 小時)

這份文件是**必須親自完成的步驟**,因為涉及帳號註冊、OAuth 授權、付款設定。完成後 Pipeline 就能完全自動運作。

> 建議按順序進行,有些步驟有依賴關係。

---

## 全部清單 (打勾追蹤進度)

- [ ] **A. Azure 訂閱與資源**
  - [ ] A1. 開通 Azure 訂閱
  - [ ] A2. 建立 Azure OpenAI 資源 + 部署 gpt-5-mini + gpt-image-2 (Sweden Central)
  - [ ] A3. 建立 Azure Speech Service 資源
  - [ ] A4. 建立 Azure Storage Account + Blob Container (公開)
  - [ ] A5. 建立 Entra ID App Registration + Federated Credential + 指派 RBAC role
- [ ] **B. YouTube 設定**
  - [ ] B1. 建立頻道 (如果還沒有)
  - [ ] B2. Google Cloud Console 開啟 YouTube Data API v3
  - [ ] B3. 建立 OAuth 2.0 憑證
  - [ ] B4. 用 OAuth Playground 取得 refresh_token
- [ ] **C. Podcast 設定**
  - [ ] C1. 建立 GitHub Pages (託管 podcast.xml)
  - [ ] C2. 提交 RSS 到 Apple Podcasts Connect
  - [ ] C3. 提交 RSS 到 Spotify for Podcasters
- [ ] **D. GitHub Repo 與 Secrets**
  - [ ] D1. 建立 GitHub Repo + Push 程式碼
  - [ ] D2. 設定所有 Secrets
  - [ ] D3. 首次手動觸發 workflow 測試
- [ ] **E. (選用) Discord 通知**

---

## A. Azure 訂閱與資源 (約 60 分鐘)

### A1. 開通 Azure 訂閱
1. 前往 https://azure.microsoft.com/ → 「免費開始使用」
2. 用 Microsoft 帳號登入,新用戶有 $200 USD 試用額度 + 12 個月免費服務
3. 建立後記下 **Subscription ID**

### A2. Azure OpenAI 資源  需先申請存取權

> **重要**:Azure OpenAI 需要先申請,通常 1-2 工作天核准。
> Tenant policy 通常會強制 `disableLocalAuth=true` (停用 API Key),所以本專案改用 **Entra ID (token-based auth)**,見 A5。

1. 申請存取:https://aka.ms/oai/access
   - 填申請表 → 等核准 email
2. 核准後到 Azure Portal → 「建立資源」→ 搜尋 **Azure OpenAI**
3. 設定:
   - **訂閱**: 你的訂閱
   - **資源群組**: 新建 `rg-storyteller`
   - **區域**: **Sweden Central** (gpt-image-2 / gpt-5-mini 都可用,East US 沒有 image 模型)
   - **名稱**: `openai-storyteller-{你的暱稱}-{亂數}` (全球唯一)
   - **定價層**: Standard S0
4. 建立完後進入資源 → 左側 **「金鑰與端點」**:
   - 記下 `Endpoint` (像 `https://xxx.openai.azure.com/`) → 設為 secret `AZURE_OPENAI_ENDPOINT`
   - **不需要 Key** — Entra ID 流程改用 RBAC 授權,見 A5

5. 點左側 **「模型部署」** → 「管理部署」(會跳到 Azure AI Foundry):
   - 部署 **gpt-5-mini** (2025-08-07,GA)
     - Deployment name: `gpt-5-mini`
     - SKU: GlobalStandard, capacity: 10K TPM (預設)
   - 部署 **gpt-image-2** (2026-04-21,GA)
     - Deployment name: `gpt-image-2`
     - SKU: GlobalStandard, capacity: 2 RPM (申請較高需要走 quota)
   - 兩個部署名稱分別填入 secrets `AZURE_OPENAI_GPT_DEPLOYMENT` 和 `AZURE_OPENAI_IMAGE_DEPLOYMENT`

6. API version: 用 `2024-10-21` (穩定版)

> **az cli 一鍵建立** (跳過 Portal):
> ```bash
> az group create --name rg-storyteller --location swedencentral
> az cognitiveservices account create \
> --name openai-storyteller-{你的暱稱}-{亂數} \
> --resource-group rg-storyteller \
> --location swedencentral --kind OpenAI --sku S0 \
> --custom-domain openai-storyteller-{你的暱稱}-{亂數} --yes
> az cognitiveservices account deployment create \
> --name openai-storyteller-{你的暱稱}-{亂數} -g rg-storyteller \
> --deployment-name gpt-5-mini --model-name gpt-5-mini --model-version 2025-08-07 \
> --model-format OpenAI --sku-name GlobalStandard --sku-capacity 10
> az cognitiveservices account deployment create \
> --name openai-storyteller-{你的暱稱}-{亂數} -g rg-storyteller \
> --deployment-name gpt-image-2 --model-name gpt-image-2 --model-version 2026-04-21 \
> --model-format OpenAI --sku-name GlobalStandard --sku-capacity 2
> ```

### A3. Azure Speech Service
1. Azure Portal → 「建立資源」→ 搜尋 **Speech service**
2. 設定:
   - **資源群組**: `rg-storyteller`
   - **區域**: 任選 (`eastus` 即可,Speech 不依賴模型區域)
   - **名稱**: `speech-storyteller-{亂數}`
   - **定價層**: **Free F0** (每月 50 萬字免費,夠你用) 或 Standard S0
3. 建立後 → 左側 **「概觀」**:
   - 記 `區域` (像 `eastus`) → secret `AZURE_SPEECH_REGION`
   - 在資源 JSON 找 `id` → secret `AZURE_SPEECH_RESOURCE_ID` (格式 `/subscriptions/.../speech-storyteller-xxx`)
   - **不需要 Key** — 走 Entra ID,見 A5

### A4. Azure Storage (Blob) 託管 Podcast 音檔
1. Azure Portal → 建立資源 → **Storage account**
2. 設定:
   - **資源群組**: `rg-storyteller`
   - **儲存體帳戶名稱**: `podcaststoryteller{亂數}` (全球唯一,只能小寫+數字)
   - **區域**: East US
   - **效能**: Standard
   - **冗餘**: LRS (本地備援,最便宜)
3. 建立後 → 左側 **「容器」** → 「+ 容器」:
   - 名稱: `podcast-episodes`
   - 公用存取層級: **Container** (匿名讀取,給 Podcast 抓取)
4. 左側 **「存取金鑰」**:
   - 點 「顯示」→ 複製 **「連接字串」**
   - 設為 secret `AZURE_STORAGE_CONNECTION_STRING`
5. Container 名稱填入 secret `AZURE_STORAGE_CONTAINER` = `podcast-episodes`
6. 公開 URL base 是 `https://{帳戶名}.blob.core.windows.net/podcast-episodes`
   - 設為 secret `AZURE_BLOB_PUBLIC_URL_BASE`

> **CORS 設定** (避免某些 Podcast app 抓不到):
> 進 Storage account → 左側「資源共用 (CORS)」→ Blob service → 加一筆:
> Origins=`*`, Methods=`GET, HEAD`, Allowed headers=`*`, Max age=`3600`

### A5. Entra ID App Registration + Federated Credential (給 GitHub Actions 用)

> 因為 tenant policy 強制 `disableLocalAuth=true`,Cognitive Services 不能用 API Key。GitHub Actions 改透過 OIDC 換 Azure token。

1. 建立 App Registration:
   ```bash
   APP_ID=$(az ad app create --display-name "github-storyteller-pipeline" --query appId -o tsv)
   SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
   echo "AZURE_CLIENT_ID=$APP_ID"
   ```
   - `$APP_ID` → secret `AZURE_CLIENT_ID`
   - Tenant ID:`az account show --query tenantId -o tsv` → secret `AZURE_TENANT_ID`
   - Subscription ID:`az account show --query id -o tsv` → secret `AZURE_SUBSCRIPTION_ID`

2. 建立 Federated Credential (允許 GitHub Actions 換 token):
   ```bash
   REPO="你的帳號/chinese-history-storyteller"
   for sub in "repo:$REPO:ref:refs/heads/main" "repo:$REPO:pull_request"; do
     name=$(echo "$sub" | tr ':/' '--')
     az ad app federated-credential create --id "$APP_ID" --parameters "{
       \"name\": \"$name\",
       \"issuer\": \"https://token.actions.githubusercontent.com\",
       \"subject\": \"$sub\",
       \"audiences\": [\"api://AzureADTokenExchange\"]
     }"
   done
   ```

3. 指派 RBAC 角色 (注意 az cli 有 known issue 會回 MissingSubscription,改用 `az rest`):
   ```bash
   SUB=$(az account show --query id -o tsv)
   OPENAI_SCOPE="/subscriptions/$SUB/resourceGroups/rg-storyteller/providers/Microsoft.CognitiveServices/accounts/openai-storyteller-..."
   SPEECH_SCOPE="/subscriptions/$SUB/resourceGroups/rg-storyteller/providers/Microsoft.CognitiveServices/accounts/speech-storyteller-..."
   ROLE_OPENAI="/subscriptions/$SUB/providers/Microsoft.Authorization/roleDefinitions/5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"  # Cognitive Services OpenAI User
   ROLE_SPEECH="/subscriptions/$SUB/providers/Microsoft.Authorization/roleDefinitions/a97b65f3-24c7-4388-baec-2e87135dc908"  # Cognitive Services User
   for pair in "$OPENAI_SCOPE|$ROLE_OPENAI" "$SPEECH_SCOPE|$ROLE_SPEECH"; do
     scope="${pair%|*}"; role="${pair#*|}"
     guid=$(python -c "import uuid; print(uuid.uuid4())")
     az rest --method PUT \
       --url "https://management.azure.com${scope}/providers/Microsoft.Authorization/roleAssignments/${guid}?api-version=2022-04-01" \
       --body "{\"properties\":{\"roleDefinitionId\":\"$role\",\"principalId\":\"$SP_ID\",\"principalType\":\"ServicePrincipal\"}}"
   done
   ```

4. (選用) 給自己同樣的 role,本機才能 `az login` 後直接跑 scripts:把上面 `principalId` 改成 `az ad signed-in-user show --query id -o tsv` 的結果,`principalType` 改成 `User`。

---

## B. YouTube API 設定 (約 45 分鐘)

### B1. 建立 YouTube 頻道
1. 用想經營的 Google 帳號登入 YouTube
2. 右上角頭像 → 「建立頻道」→ 自訂名稱
3. 進入 **YouTube Studio** → 設定 → 頻道:
   - 頻道關鍵字: 「歷史 說書 中國歷史 科技史」
   - 國家/地區: 台灣
4. 進設定 → 頻道 → **進階設定**:
   - 頻道分類選擇:**「沒有針對兒童的頻道」**
5. 記下頻道 URL 中的 channel ID (格式 `UC...`),設為 secret `YOUTUBE_CHANNEL_ID`

### B2. Google Cloud Console — 開啟 YouTube API
1. 前往 https://console.cloud.google.com/
2. 上方點「選取專案」→ 「新增專案」名稱 `youtube-storyteller`
3. 左側選單 → 「API 與服務」→ 「程式庫」→ 搜尋 **YouTube Data API v3** → 啟用

### B3. 建立 OAuth 2.0 憑證
1. 「API 與服務」→ **「OAuth 同意畫面」**:
   - User Type: **External**
   - 應用程式名稱: `YouTube Storyteller`
   - 使用者支援電子郵件: 你的 email
   - 範圍: 加入 `https://www.googleapis.com/auth/youtube.upload`
   - 測試使用者: **加入你自己的 Gmail**  不加會 403
   - 發布狀態: 保持 **Testing** (refresh token 會 7 天過期),或申請成 Production (永久)
2. 「API 與服務」→ **「憑證」** → 「+ 建立憑證」→ **OAuth 用戶端 ID**:
   - 應用程式類型: **桌面應用程式**
   - 名稱: `youtube-storyteller-cli`
3. 建立後跳出視窗,記下:
   - **Client ID** → secret `YOUTUBE_CLIENT_ID`
   - **Client Secret** → secret `YOUTUBE_CLIENT_SECRET`

### B4. 取得 refresh_token (一次性,日後永久有效)

最簡單的方法是 OAuth 2.0 Playground:

1. 前往 https://developers.google.com/oauthplayground
2. 右上齒輪圖示 → 勾選 **Use your own OAuth credentials** → 填 Client ID 和 Secret
3. 左邊 Step 1:在 「Input your own scopes」 輸入:
   ```
   https://www.googleapis.com/auth/youtube.upload
   ```
   點 **Authorize APIs**
4. 跳出 Google 登入 → 選你的 YouTube 頻道帳號 → 同意 (會出現警告 "未驗證 app",點「繼續」)
5. 回到 Playground Step 2 → 點 **Exchange authorization code for tokens**
6. 看到 `Refresh token: 1//0g...` → 複製 → secret `YOUTUBE_REFRESH_TOKEN`

> 如果 OAuth 同意畫面是 Testing 狀態,refresh token 會 7 天過期。
> 解法:把 OAuth 同意畫面切到 **Production** (需提交 app verification,但只用自己 = scope 限制下不需正式驗證)。

### B5. 本地驗證 (選用,確認 token 可用)
```bash
pip install google-api-python-client google-auth-oauthlib
python -c "
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
c = Credentials(None, refresh_token='你的token',
    client_id='你的id', client_secret='你的secret',
    token_uri='https://oauth2.googleapis.com/token',
    scopes=['https://www.googleapis.com/auth/youtube.upload'])
y = build('youtube','v3',credentials=c)
print(y.channels().list(part='snippet',mine=True).execute())
"
```
看到頻道資訊就 OK。

---

## C. Podcast 平台 (約 60 分鐘,但首次審核可能 1-2 週)

### C1. 用 GitHub Pages 託管 podcast.xml

最便宜方案:repo 設定為 public,啟用 GitHub Pages。

1. GitHub repo → Settings → **Pages**
2. Source: 選 `Deploy from a branch`,Branch: `main` / `(root)`
3. 儲存後等 1 分鐘,你的 RSS URL 會是:
   ```
   https://帳號.github.io/chinese-history-storyteller/podcast.xml
   ```
4. 把這個 URL 設為 secret `PODCAST_BASE_URL` (拿掉 `/podcast.xml` 那段)

### C2. Apple Podcasts Connect
1. 你需要 **Apple ID** 和**一張封面圖** (3000x3000 px JPG/PNG,< 500KB)
2. 前往 https://podcastsconnect.apple.com/
3. 同意條款 → **「+ New Show」**:
   - 選 「Add a show with an RSS feed」
   - 貼上 podcast.xml 公開 URL
   - 系統會抓取 RSS 內容預覽
4. 確認沒問題 → Submit
5. 審核 1-7 天,核准後會有 Apple Podcasts URL

> **首次提交前要先有至少一集真的可播放的音檔在 RSS 裡**,先手動跑一次 pipeline 產出第一集。

### C3. Spotify for Podcasters
1. 前往 https://podcasters.spotify.com/
2. 「Get Started」→ 用 Spotify 帳號或 Google 登入
3. 「Add your podcast」→ 貼 RSS URL → 驗證(會發確認碼到你的 RSS author email,然後填回去)
4. 通常 24 小時內上架

### C4. Google Podcast / YouTube Music
- Google Podcast 已併入 YouTube Music,自動從 RSS 抓 (不用提交)
- 也可主動到 https://podcastsmanager.google.com/ 提交加速收錄

---

## D. GitHub Repo 設定 (約 30 分鐘)

### D1. 建立 Repo
```bash
cd "chinese-history-storyteller"
git init
git add .
git commit -m "Initial: AI 中文歷史說書 Pipeline"

# 在 GitHub 網頁建立空 repo (public,才能用免費 Actions)
git remote add origin git@github.com:你的帳號/chinese-history-storyteller.git
git branch -M main
git push -u origin main
```

### D2. 設定 GitHub Secrets

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**

照下表全部加入 (打勾追蹤):

| Secret Name | 來源 |
|------------|------|
|  `AZURE_CLIENT_ID` | A5 (App Registration appId) |
|  `AZURE_TENANT_ID` | A5 (`az account show --query tenantId`) |
|  `AZURE_SUBSCRIPTION_ID` | A5 (`az account show --query id`) |
|  `AZURE_OPENAI_ENDPOINT` | A2 |
|  `AZURE_OPENAI_GPT_DEPLOYMENT` | A2 (`gpt-5-mini`) |
|  `AZURE_OPENAI_IMAGE_DEPLOYMENT` | A2 (`gpt-image-2`) |
|  `AZURE_OPENAI_API_VERSION` | A2 (`2024-10-21`) |
|  `AZURE_SPEECH_REGION` | A3 (`eastus`) |
|  `AZURE_SPEECH_RESOURCE_ID` | A3 (Speech resource ARM id) |
|  `AZURE_SPEECH_VOICE` | 自訂 (`zh-CN-YunjianNeural` 推薦) |
|  `AZURE_STORAGE_CONNECTION_STRING` | A4 |
|  `AZURE_STORAGE_CONTAINER` | A4 (`podcast-episodes`) |
|  `AZURE_BLOB_PUBLIC_URL_BASE` | A4 (`https://...blob.core.windows.net/podcast-episodes`) |
|  `YOUTUBE_CLIENT_ID` | B3 |
|  `YOUTUBE_CLIENT_SECRET` | B3 |
|  `YOUTUBE_REFRESH_TOKEN` | B4 |
|  `YOUTUBE_CHANNEL_ID` | B1 |
|  `PODCAST_TITLE` | 自訂 (`中文歷史說書`) |
|  `PODCAST_AUTHOR` | 自訂 (你的名字) |
|  `PODCAST_EMAIL` | 自訂 |
|  `PODCAST_BASE_URL` | C1 (`https://你帳號.github.io/chinese-history-storyteller`) |
|  `PODCAST_LANGUAGE` | `zh-tw` |
|  `PODCAST_CATEGORY` | `History` |
|  `DISCORD_WEBHOOK_URL` | (選用) |

> 注意:本專案改用 Entra ID,**不再需要** `AZURE_OPENAI_KEY` 與 `AZURE_SPEECH_KEY` secrets。如果你之前的 secrets 還有,可保留(不會被讀取)或刪除。

### D3. 首次手動觸發測試
1. GitHub repo → **Actions** tab
2. 左邊選 **Publish Episode** workflow
3. 右上 **Run workflow**:
   - 把 privacy 設為 **`unlisted`** (測試時不公開)
4. 等 ~10-15 分鐘
5. 檢查:
   - YouTube Studio 是否有新影片
   - Repo 是否多了一個 commit ` Publish episode 1`
   - `data/published_log.json` 是否新增一筆
   - `podcast.xml` 是否有 item

### D4. 確認排程
首次成功後,排程會在每週日/週三 23:00 UTC (台灣時間週一/週四 7:00) 自動觸發。
要改時間就改 `.github/workflows/publish.yml` 裡的 cron。

---

## E. (選用) Discord 通知

1. Discord 頻道 → 設定 → 整合 → Webhook → 新建
2. 複製 Webhook URL
3. 設為 secret `DISCORD_WEBHOOK_URL`

---

## 完成後檢查清單

- [ ] 第一集已成功上傳 YouTube (unlisted)
- [ ] `podcast.xml` 已透過 GitHub Pages 公開可訪問
- [ ] Apple Podcasts Connect 已提交
- [ ] Spotify for Podcasters 已提交
- [ ] 排程確認在 Actions 頁面有顯示「Next scheduled run」
- [ ] 預發布題材人工審核了至少前 5 集

---

## 之後的維運

| 頻率 | 事項 | 時間 |
|------|------|------|
| 每週日 | 審核下週要發的 2 集腳本 (在 GitHub Actions 手動跑 unlisted 預發) | 1-2h |
| 每月 | 看 YouTube Analytics + 微調題材 | 30min |
| 每季 | 補充題材庫 + 更新 prompt 風格 | 1-2h |
| 每年 | 檢查 OAuth token、Azure 額度、API 政策變化 | 1h |

> Refresh token 如果失效會看到 401 錯誤,重做 B4 步驟即可。

---

## 常見錯誤

| 錯誤 | 原因 | 解法 |
|------|------|------|
| `Missing required env var: AZURE_OPENAI_ENDPOINT` | Secret 沒設或拼錯 | 重新確認 D2 |
| `DefaultAzureCredential failed` | OIDC token 換不到 / RBAC 沒設 | 確認 A5 步驟 3 的 role assignment 有跑;workflow `permissions: id-token: write` 有設 |
| YouTube 401 unauthorized | refresh token 過期或 OAuth 還在 Testing | 重做 B4 / 把 OAuth 設成 Production |
| `DeploymentNotFound` (gpt-image-2) | 部署名稱或區域錯 | 確認 deployment name `gpt-image-2`,資源在 Sweden Central |
| FFmpeg `Permission denied` | runner 缺工具 | 已在 workflow 安裝,不應發生;如本地測試自行安裝 ffmpeg |
| Podcast 在 Apple 不出現 | 首次審核 1-7 天 / RSS 沒有有效 enclosure | 等待 / 確認音檔 URL 可下載 |
| 影片字幕亂碼 | runner 缺中文字型 | workflow 已安裝 `fonts-noto-cjk`;本地測試需安裝對應字型 |
