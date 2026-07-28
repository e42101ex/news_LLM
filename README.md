# auto_report_news · AI 每日新聞彙整

兩個分頁（上方選單切換）：

| 分頁 | 內容 | 資料來源 |
| --- | --- | --- |
| **AI 相關新聞** | 24 個媒體的 AI 新聞，同主題併成一則 | RSS / 網頁 / WP API |
| **社群熱門** | 台灣即時熱搜、Bluesky 熱門話題、AI 熱門貼文 | Google Trends、Bluesky 公開 API |

抓取 24 個中英文科技媒體，把**講同一件事的報導併成一個主題**，排版成靜態網頁後部署到 GitHub Pages。

```
RSS 抓取 ──► 同主題分群 ──► 摘要（三種模式任選） ──► HTML ──► GitHub Pages
fetch.py     cluster.py     llm.py／Claude／不用      render.py    deploy.sh
```

每則主題會自動配一張縮圖（RSS 帶的圖 → 抓不到就用文章頁的 og:image），實測覆蓋率約 80%。

分群完全靠演算法（TF-IDF + cosine + 質心式分群），**不需要 API key**，而且處理過：

- **簡繁差異**：`英伟达`／`輝達`／`NVIDIA` 會收斂成同一個 token（用 opencc + 實體同義詞表）
- **中英夾雜**：同一則新聞被中文媒體與英文媒體各報一次也能併在一起
- **數字對照**：「2,500 億美元」與「2500 亿美元」視為同一個特徵

## 安裝與試跑

```bash
pip install -r requirements.txt
python build.py --llm off            # 純演算法，不呼叫任何 API
open docs/index.html
```

常用參數：

| 指令 | 用途 |
| --- | --- |
| `python build.py` | 全流程；LLM 設定齊全就自動用它寫中文摘要 |
| `python build.py --llm-test` | 只測 LLM 端點通不通 |
| `python build.py --llm on` | 強制用 LLM 摘要（實測 40 主題約 3 分鐘） |
| `./run_daily.sh` | **每日一鍵**：產生 + 推送 |
| `python build.py --hours 48` | 放寬收錄範圍（週一新聞少時很有用） |
| `python build.py --stage collect` | 只抓取＋分群，結果寫進 `data/latest.json` |
| `python build.py --stage render` | 從 `data/latest.json` 重新產生 HTML |
| `python curate.py list -v` | 檢視主題與底下的原文 |
| `python curate.py merge A B` | 手動把 B 併進 A |
| `python curate.py set A --title … --importance 5` | 手動改寫某個主題 |

來源、時間範圍、分群門檻、模型都在 `config.toml`。

### 三種來源型態

| `type` | 適用 | 需要的欄位 |
| --- | --- | --- |
| `rss`（預設） | 有 RSS 的站台 | `url` 指向 feed |
| `html` | **沒有 RSS 的站台**：抓列表頁連結，再逐篇讀 og / JSON-LD metadata | `url` 指向列表頁、`link_pattern`（文章網址的正則）、`max_items` |
| `wp_json` | WordPress 站但 RSS 被擋（如 Cloudflare 403） | `url` 指向 `/wp-json/wp/v2/posts` |

綜合媒體設 `ai_only = false`，會自動用 AI 關鍵字過濾。`html` 型態的文章頁 metadata
會快取在 `data/pagecache.json`（保留 21 天），所以每天只有新文章需要連線。

目前的非 RSS 來源：DIGITIMES 與數位時代（兩家都沒有可用 RSS —— 數位時代的
feedburner 停在 2009 年）走 `html`；坂本電腦的 `/feed` 被 Cloudflare 擋 403，走 `wp_json`。

## 摘要的三種模式

| 模式 | 怎麼跑 | 需要 | 適用 |
| --- | --- | --- | --- |
| 不用 LLM | `--llm off` | — | 快速預覽、CI 冒煙測試 |
| **OpenAI-compatible LLM** | `--llm on` | base_url + model + key | 無人排程（GitHub Actions／cron） |
| Claude 自己潤稿 | `--stage collect` → 改 JSON → `--stage render` | — | Cowork／Claude Code 排程 |

### 設定 OpenAI-compatible 端點

支援任何提供 `/v1/chat/completions` 的服務：自架 vLLM／Ollama／LiteLLM、公司內部 gateway、
OpenRouter、OpenAI 本家都可以。**API key 走環境變數或 `.env`，不要寫進 `config.toml`。**

```bash
cp .env.example .env      # 填入 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
python build.py --llm-test   # 先確認端點通不通（只打一次最小請求）
python build.py --llm on     # 正式跑
```

也可以用環境變數或旗標臨時覆寫：

```bash
LLM_BASE_URL=https://gw.example.com/v1 LLM_MODEL=gpt-4o-mini LLM_API_KEY=sk-… \
  python build.py --llm on
python build.py --llm-test --base-url https://gw.example.com/v1 --model qwen2.5-72b-instruct
```

幾個實作上的細節：

- **URL 怎麼給都可以**：`https://host`、`https://host/v1`、`https://host/v1/chat/completions`
  都會被補成正確端點（`--llm-test` 失敗時會印出實際打出去的 URL）。
- **JSON 模式自動降級**：先試 `response_format: json_schema`，端點不支援就自動退到
  `json_object`，再不行就純提示詞模式；試成功的模式會沿用到後續批次。可用
  `json_mode` 或 `LLM_JSON_MODE` 鎖定。
- **輸出容錯**：會自動剝掉 `<think>…</think>` 與 ```` ```json ```` 圍籬（推理型模型常見），
  再用大括號配對掃出 JSON。
- **失敗不會毀掉整份報告**：某批摘要失敗就沿用原文標題；連續兩批失敗會直接放棄 LLM 階段。
- **需要額外 header** 的 gateway，在 `config.toml` 的 `[llm.extra_headers]` 加即可。
- 想改回 Anthropic Messages API：`LLM_PROVIDER=anthropic` 或 `config.toml` 設
  `provider = "anthropic"`（`model` 例如 `claude-opus-4-8`）。

## 社群熱門分頁

`config.toml` 的 `[social]`，全部免 API key：

| 區塊 | 來源 | 說明 |
| --- | --- | --- |
| 台灣即時熱搜 | `trends.google.com/trending/rss?geo=TW` | 每個熱搜附熱度值與最多 3 則相關新聞（含來源、連結、圖） |
| Bluesky 熱門話題 | `app.bsky.unspecced.getTrends` | 話題名稱與貼文數；內容以英語圈娛樂／體育／政治為主，頁面上有註明 |
| AI 熱門貼文 | `app.bsky.feed.searchPosts` | 依 `bluesky_queries` 搜尋，30 小時內、依讚數＋轉發排序 |

貼文的過濾（`bluesky_min_likes` 之外）：必須命中 AI 強訊號詞（詞界比對，避免 `llm`
命中別的單字）、排除 AI 繪圖／成人洗版標籤、標籤超過 5 個的丟掉、有標語言且不在
`bluesky_langs` 內的丟掉。

> **為什麼不是 Threads**：官方 API 沒有 trending 端點；`keyword_search` 在 app review
> 通過前只能搜「自己的貼文」；而 insights（讚／回覆數）文件明訂只能讀自己的貼文，
> 所以**拿不到別人貼文的熱度、無法排序熱門**。網頁端是純 JS 殼（實測 258KB HTML 裡
> 0 個貼文欄位），自動化抓取也違反 Meta 服務條款。

```bash
python build.py --section social     # 只重建社群熱門
python build.py --section news       # 只重建 AI 新聞
python build.py                      # 兩個都做（預設）
```

## 縮圖

`config.toml` 的 `[images]`：

| `mode` | 行為 | 取捨 |
| --- | --- | --- |
| `local`（預設） | 下載後裁成 400×267 的 WebP 放進 `docs/img/<日期>/` | 頁面自包含、存檔不會破圖；repo 每天增加約 0.35-0.5 MB |
| `hotlink` | 直接連原站圖片 | repo 不會長大；但存檔頁的圖日後可能失效，且讀者瀏覽器會連向十幾個網域 |
| `off` | 不放圖 | — |

實作細節：

- **來源優先序**：RSS 的 `media:thumbnail` / `media:content` / `enclosure` / 內文第一張 `<img>`
  → 都沒有才去抓文章頁的 `og:image`（只抓前 200KB，不下載整頁）。
- **濾掉站台預設圖**：同一張圖被 3 個以上主題共用就丟掉 —— 例如 36氪 的 `og:image`
  是站台 logo，不濾掉會有好幾則新聞掛同一張圖。門檻可用 `generic_threshold` 調。
- **統一裁切**：`ImageOps.fit` 等比縮放後居中裁切成 3:2，卡片高度才會一致（不會有信箱黑邊）。
- **失敗不影響版面**：下載失敗的主題就不顯示圖；圖片載入失敗時前端的 `onerror` 也會把
  框移除，不會留下破圖圖示。
- **自動清理**：`keep_days`（預設 60）天前的圖片目錄會被刪掉，HTML 存檔保留。
- 覆蓋率約 80%。剩下的通常是本來就沒有配圖的來源（Simon Willison）或 og:image
  指向 404 的站（ZDNet 有時如此）。

## 部署到 GitHub Pages

1. 在 GitHub 建一個 repo（例如 `ai-news`，public）。
2. 第一次推送：

   ```bash
   GH_REPO=你的帳號/ai-news ./deploy.sh
   ```

3. repo → **Settings → Pages** → Source 選 **Deploy from a branch**，Branch `main`、Folder **`/docs`**。
4. 網址：`https://你的帳號.github.io/ai-news/`

之後每次只要 `./deploy.sh`。`docs/archive/YYYY-MM-DD.html` 會保留每天的存檔。

## 每天怎麼跑

所有內容都在**本機**產生，GitHub 只接收 `docs/` 的成品。一鍵指令：

```bash
./run_daily.sh              # 抓取 → 分群 → LLM 摘要 → 產生 HTML → 推送
./run_daily.sh --no-push    # 只產生，先開 docs/index.html 看過再手動 ./deploy.sh
./run_daily.sh --hours 48   # 週一、連假時放寬收錄範圍
```

執行紀錄留在 `logs/YYYY-MM-DD.log`（只保留最近 30 天，不進 git）。
若當天完全沒有 LLM 摘要，腳本會提醒你去跑 `python build.py --llm-test` 檢查端點。

### 排程方式（三選一）

**A. 本機 crontab** —— 最單純，這台電腦開著就會跑（已設定好）：

```bash
crontab -l    # 確認
# 每天 08:10（錯開整點，RSS 比較穩）
MAILTO=""
10 8 * * * PYTHONUTF8=1 PYTHON=/home/yc/miniforge3/envs/py312/bin/python3 \
  /home/yc/auto_report_news/run_daily.sh >> /home/yc/auto_report_news/logs/cron.log 2>&1
```

三個地方**都必須是絕對路徑或明確指定**，否則會靜默失敗：

| 陷阱 | 原因 |
| --- | --- |
| `PYTHON=…/miniforge3/envs/py312/bin/python3` | 套件裝在 conda env，cron 的 PATH 只有 `/usr/bin:/bin`，會抓到沒裝 feedparser 的系統 python |
| 腳本用絕對路徑 | cron 的工作目錄是 `$HOME`，`./run_daily.sh` 找不到 |
| 日誌用絕對路徑 | 同上，`logs/cron.log` 會被解讀成 `~/logs/cron.log`（目錄不存在 → 整個 job 失敗） |

`run_daily.sh` 本身也會先檢查 interpreter 有沒有必要套件，缺了會印出安裝指令而不是丟一串 traceback。

**B. Claude Cowork／Claude Code 排程** —— 想要有人幫你看過一遍再發布時用。
把 `PROMPT_daily.md` 整段貼進排程任務即可：Claude 會先產生、抽查幾則摘要、
修掉明顯的分群或用字問題，再部署並回報當日重點。

**C. 手動** —— 想看什麼時候發就什麼時候跑 `./run_daily.sh`。

### 無人排程需要免密碼推送

A 和 B 都是無人值守，`git push` 不能停下來問密碼。二選一：

```bash
# 方式一：SSH key（推薦，token 不會以明文存在硬碟上）
ssh-keygen -t ed25519 -C "auto-report-news"          # 一路 Enter
cat ~/.ssh/id_ed25519.pub                            # 貼到 GitHub → Settings → SSH keys
git remote set-url origin git@github.com:e42101ex/news_LLM.git

# 方式二：讓 git 記住 PAT（會以明文存在 ~/.git-credentials）
git config credential.helper store                   # 下一次 push 輸入後就記住
```

### GitHub Actions（刻意停用）

`.github/daily.yml.example` 是寫好的 workflow，但**沒有放在 `.github/workflows/` 底下**，
所以不會執行。原因：LLM 端點在私有網段，Actions 在公有雲連不到，跑起來只會產出沒有摘要
的版本，還會 auto-commit 覆蓋掉本機產生的好版本。要啟用的步驟寫在該檔案開頭。

## 檔案結構

```
config.toml              來源清單與參數（含 [llm] 設定）
.env.example             LLM 憑證範本（複製成 .env，不會進 git）
run_daily.sh             每日一鍵（build + deploy + 日誌輪替）
build.py                 主流程 CLI
curate.py                手動／Claude 微調 data/latest.json
ainews/fetch.py          RSS 抓取、AI 關鍵字過濾、去重
ainews/cluster.py        分群（簡繁正規化、實體同義詞、TF-IDF）
ainews/llm.py            選用：呼叫 LLM 產生中文摘要（OpenAI-compatible／Anthropic）
ainews/render.py         Jinja2 渲染、日期存檔
templates/               HTML 模板（深淺色自動切換、手機版）
ainews/social.py         社群熱門：Google Trends + Bluesky
templates/_style.html.j2 兩個分頁共用的 CSS
templates/_sections.html.j2 分頁選單
templates/social.html.j2 社群熱門頁
tools/shot.py            版面截圖驗證
docs/                    產出（GitHub Pages 的根目錄）
docs/social/             社群熱門分頁
data/latest.json         AI 新聞中繼資料
data/social.json         社群熱門中繼資料
data/pagecache.json      非 RSS 來源的文章頁 metadata 快取
```

## 版面驗證

改過模板之後用這支檢查，避免「結構對但看起來壞掉」：

```bash
python tools/shot.py                    # 桌機淺色/深色 + 手機淺色/深色，截頂部
python tools/shot.py --full             # 整頁
python tools/shot.py --url https://e42101ex.github.io/news_LLM/   # 截線上版
```

截圖放在 `.shots/`（不進 git）。腳本會先滾一遍觸發 lazy loading、再等所有
`<img>` decode 完才截圖，並回報有幾張圖沒載入。

需要 Playwright：

```bash
python -m pip install playwright && python -m playwright install chromium
```

> 為什麼不用 Firefox 的 `--screenshot`：它不等圖片解碼，頁面上有 40 張縮圖時
> 會全部拍成灰色佔位框，看不出真正的版面。

## 已知限制

- 分群是機器判定，偶爾會漏併或誤併；`curate.py merge` / `curate.py split` 就是為了補這一刀。
  演算法用質心式分群（新文章要和整群的質心夠像才會加入），比 single-linkage
  不容易把不相干的新聞串成一團，但邊界案例仍需人工判斷。
- 坂本電腦大約每月才發一篇文，在 30 小時的收錄範圍內幾乎不會出現；留著不影響
  其他來源（每天多兩個 HTTP 請求）。
- 週末與台灣時間的清晨，英文媒體常常沒有新文章，這時 `--hours 48` 比較合適。
- RSS 來源會改網址或關閉。抓不到的來源只會印警告，不會中斷整個流程；
  長期抓不到就直接從 `config.toml` 移掉。
