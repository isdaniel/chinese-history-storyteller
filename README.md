# 🏛️ 中文歷史說書 - AI 自動發布 Pipeline

每週兩集,從題材生成到 YouTube + Podcast 發布全自動。

> 本專案是**完全被動經營**的中文歷史/科技史內容生產線。所有步驟在 GitHub Actions 上執行,你只需要每週日花 1-2 小時審核下週要發的腳本即可。

---

## 🎯 這個專案做什麼

每次排程觸發 (預設週一、週四早上 7:00 台灣時間),GitHub Actions 會自動完成:

1. **選題** — 從 `data/topics_queue.json` 取下一個未發布題材
2. **寫稿** — Azure OpenAI GPT-4o-mini 生成 12 分鐘 8 段式中文旁白
3. **配圖** — Azure DALL-E 3 為每段生成統一風格水墨插畫
4. **配音** — Azure Speech Neural TTS (預設 `zh-CN-YunjianNeural` 沉穩男聲)
5. **剪片** — FFmpeg 把圖片+配音+字幕合成 1080p MP4 (Ken Burns 效果)
6. **YouTube 上架** — 透過 YouTube Data API v3 自動上傳
7. **Podcast 發布** — 上傳 MP3 到 Azure Blob + 自動更新 RSS feed
8. **Discord 通知** — (選用) 發布完成發訊息

---

## 📂 專案結構

```
chinese-history-storyteller/
├── README.md                    ← 你現在在看的檔
├── requirements.txt             ← Python 套件清單
├── .env.example                 ← 環境變數範本
├── .gitignore
├── podcast.xml                  ← Podcast RSS feed (自動更新)
│
├── .github/workflows/
│   └── publish.yml              ← GitHub Actions 排程 + pipeline
│
├── scripts/                     ← Pipeline 步驟 (照順序)
│   ├── common.py                  共用工具
│   ├── generate_script.py         1. 取題 + GPT 寫稿
│   ├── generate_images.py         2. DALL-E 配圖
│   ├── synthesize_speech.py       3. Azure TTS 配音
│   ├── build_video.py             4. FFmpeg 合成影片
│   ├── upload_youtube.py          5. YouTube 上傳
│   ├── publish_podcast.py         6. Blob 上傳 + RSS 更新
│   └── notify.py                  7. Discord 通知 (選用)
│
├── data/
│   ├── topics_queue.json        ← 100 集題材庫 (1 年份)
│   └── published_log.json       ← 已發布記錄 (自動 append)
│
├── templates/
│   └── script_prompt.txt        ← GPT 腳本生成 prompt
│
├── assets/                      ← 放品牌素材 (Logo、片頭、預設背景)
├── output/                      ← 每集產物 (gitignored)
└── docs/
    └── MANUAL_SETUP.md          ← 你需要手動申請的所有步驟
```

---

## 🚀 快速開始

### 步驟一:閱讀手動設定清單
完整的雲端服務申請與金鑰取得步驟在 [docs/MANUAL_SETUP.md](docs/MANUAL_SETUP.md)。

預估時間:**約 3-4 小時** (一次性)。

完成後你會有:
- Azure OpenAI / Speech / Blob Storage 金鑰
- YouTube OAuth refresh token
- 一個 GitHub repo,Secrets 都設定好

### 步驟二:Push 到 GitHub
```bash
git init
git add .
git commit -m "Initial pipeline setup"
git remote add origin git@github.com:你的帳號/chinese-history-storyteller.git
git push -u origin main
```

### 步驟三:首次手動觸發測試
1. 進到 GitHub repo → **Actions** tab
2. 選 **Publish Episode** workflow
3. 點 **Run workflow** → privacy 選 `unlisted` (先不公開)
4. 等 ~10-15 分鐘
5. 檢查 YouTube 後台是否有新影片、`podcast.xml` 是否更新

### 步驟四:確認每週自動執行
排程在 `.github/workflows/publish.yml` 的 cron 設定:
```yaml
- cron: '0 23 * * 0,3'   # UTC,台灣時間週一/週四 7:00
```

---

## 🎬 內容策略

### 三大主軸 (避免演算法判定為重複公式)
| 類別 | 比例 | 範例題材 |
|------|------|---------|
| 中華歷史 | 50% | 三國真相、玄武門之變、明朝滅亡 |
| 科技史 | 30% | Nokia 隕落、Tesla 崛起、ChatGPT 革命 |
| 世界文明 | 15% | 馬雅消失、龐貝、拜占庭 |
| 歷史謎團 | 5% | 雍正之死、建文帝下落 |

題材庫已預先準備 100 集 (約 1 年份),在 `data/topics_queue.json`。
**每週日花 1-2 小時審核下週要發的腳本** = 完全被動運作。

### 視覺風格 (差異化關鍵)
- 統一**中國水墨/古畫風格**插畫
- 字幕用 Microsoft JhengHei 字體,兼顧繁體中文
- 旁白語速 -8% (莊重感),Yunjian 男聲

### YouTube 政策合規
- ✅ AI 揭露:在 Studio 設定預設揭露 "Altered Content"
- ✅ **不要** 標記 `selfDeclaredMadeForKids: true` (歷史內容受眾為成人)
- ✅ 控制發布頻率 (週 2 集),避免被判定為 Content Farm
- ✅ 第 8 段強制包含「現代啟示」原創觀點

---

## 💰 成本估算 (每月)

| 項目 | 預估用量 (週 2 集) | 月費 |
|------|-------------------|------|
| Azure OpenAI GPT-4o-mini | 8-10 次 × 5K tokens | $1-2 |
| Azure DALL-E 3 | 8 集 × 8 張 = 64 張 | $2-3 |
| Azure Speech TTS | ~25K 字元 | $0.5 |
| Azure Blob Storage | ~5GB | $0.5 |
| **小計** | | **~$5-7** |
| 緩衝 (測試重跑) | | $5 |
| **總計** | | **~$10-15/月** |

**遠低於預算 $50/月** — 有空間升級到 GPT-4o (完整版) 或加大插畫尺寸。

---

## 🛠️ 本地測試

不一定要在 GitHub 上跑,你也可以本機測試:

```bash
# 1. 安裝相依
pip install -r requirements.txt

# 2. 安裝 ffmpeg (macOS 用 brew install ffmpeg, Windows 從官網下載)

# 3. 複製環境變數
cp .env.example .env
# 編輯 .env 填入金鑰

# 4. 跑單集
cd scripts
python generate_script.py
# 上一步會印出 EPISODE_ID=1,後續腳本接著跑:
EPISODE_ID=1 python generate_images.py
EPISODE_ID=1 python synthesize_speech.py
EPISODE_ID=1 python build_video.py
EPISODE_ID=1 python upload_youtube.py    # 真的會上傳!先把 YOUTUBE_PRIVACY=private
EPISODE_ID=1 python publish_podcast.py
```

---

## 📊 監控與調校

### 每集產物保留在 `output/ep0001/`
```
output/ep0001/
├── script.json         GPT 生成的完整腳本
├── img_01.png ~ 08.png 8 張插畫
├── audio_01.mp3 ~ 08.mp3 8 段配音
├── audio_full.mp3      合併後完整音檔 (Podcast 用)
├── timings.json        每段起訖時間 (字幕用)
├── subtitles.srt       自動生成字幕
├── final.mp4           最終影片 (YouTube 上傳)
└── youtube_info.json   上傳後的 video_id
```

可以下載某集 output 資料夾來檢查腳本品質、替換不滿意的圖片再重跑 `build_video.py`。

### 出錯時的 debug
- GitHub Actions 失敗時會自動上傳 `output/` 資料夾為 artifact (保留 7 天,不含 mp4)
- 看 Actions log 找到失敗在哪一步
- 大部分問題是金鑰過期或 Azure 配額用完

### 內容調校
- **不滿意 GPT 產出**:改 `templates/script_prompt.txt`
- **想換配音**:改 `AZURE_SPEECH_VOICE` secret (建議 `zh-CN-YunyangNeural` 新聞主播風 / `zh-TW-YunJheNeural` 台灣腔)
- **想換插畫風格**:改 `scripts/generate_images.py` 的 `STYLE_SUFFIX`

---

## 🎯 預期成長 (現實版)

| 月份 | 訂閱 | 月觀看 | 月收入 |
|------|------|-------|-------|
| 1-3 | 0-100 | <1K | $0 (未達 YPP 門檻) |
| 4-6 | 100-1K | 1-10K | $0-5 |
| 6-12 | 1K-10K | 10-100K | $50-500 |
| 12-24 | 10K-50K | 100K-1M | $500-3000 |

**關鍵突破點**:通常 6-12 個月會有一支爆紅,帶動整個頻道。

---

## ⚠️ 重要提醒

1. **AI 揭露**:YouTube 後台需手動勾選預設「Altered or synthetic content」
2. **史實審核**:GPT 偶爾會編造細節,第一個月每集都要人工核對
3. **題材庫補充**:用完 100 集前要再準備下一批
4. **API 配額**:YouTube 每天預設只能上傳 6 部,週 2 集綽綽有餘
5. **版權音樂**:本 Pipeline 沒有加 BGM,如果要加請用 YouTube Audio Library 免版稅音樂

---

## 📜 授權與責任

- 程式碼:你愛怎麼用就怎麼用
- 內容責任:GPT 生成內容仍由你承擔史實正確性與版權責任
- Azure / Google API 使用條款請自行遵守

---

## 🆘 常見問題

**Q: 一週發兩集會不會被 YouTube 演算法當作 Content Farm?**
A: 不會,週 2 集是健康頻率。被判定為 Content Farm 通常是日更 5+ 部、模板化嚴重。本 Pipeline 三大主軸輪流 + 第 8 段原創觀點就是規避手段。

**Q: 我可以改成日更嗎?**
A: 技術上可以,但**強烈不建議**。改 cron 為 `0 23 * * *` 即可,但要自行承擔降權風險。

**Q: 如何同時做 Podcast 多平台?**
A: Apple/Spotify/Google Podcast 都接受同一個 RSS URL。只要把 `podcast.xml` 透過 GitHub Pages 公開,提交一次給三個平台,以後自動抓新集數。詳見 MANUAL_SETUP.md。

**Q: 要不要做縮圖 (Thumbnail)?**
A: YouTube 自動會挑一幀做封面,初期夠用。後期可以加一個 `generate_thumbnail.py` 用 DALL-E + Pillow 加標題文字。

**Q: 多語言版本怎麼做?**
A: 把 `templates/script_prompt.txt` 翻譯成英文/日文,改 TTS voice,複製整個 repo 開新頻道即可。同一份題材庫、同一份插畫可以重用,大幅降低多語言成本。

---

## 🔗 相關文件

- [docs/MANUAL_SETUP.md](docs/MANUAL_SETUP.md) — **必讀**:雲端服務申請與金鑰取得完整步驟
- [data/topics_queue.json](data/topics_queue.json) — 100 集題材庫
- [.github/workflows/publish.yml](.github/workflows/publish.yml) — Pipeline 排程設定
