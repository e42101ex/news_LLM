# auto_report_news · AI 每日新聞彙整

抓取 20 個中英文科技媒體的 RSS，把**講同一件事的報導併成一個主題**，排版成靜態網頁後部署到 GitHub Pages。

```
RSS 抓取 ──► 同主題分群 ──► 摘要（三種模式任選） ──► HTML ──► GitHub Pages
fetch.py     cluster.py     llm.py／Claude／不用      render.py    deploy.sh
```

分群完全靠演算法（TF-IDF + cosine + union-find），**不需要 API key**，而且處理過：

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

來源、時間範圍、分群門檻、模型都在 `config.toml`；要加來源就多一段 `[[sources]]`
（綜合媒體設 `ai_only = false`，會自動用 AI 關鍵字過濾）。

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

**A. 本機 crontab** —— 最單純，這台電腦開著就會跑：

```bash
crontab -e
# 每天 08:10 執行（錯開整點，RSS 比較穩）
10 8 * * * cd ~/auto_report_news && ./run_daily.sh >> logs/cron.log 2>&1
```

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
docs/                    產出（GitHub Pages 的根目錄）
data/latest.json         中繼資料，摘要階段的交接點
```

## 已知限制

- 分群是機器判定，偶爾會漏併或誤併；`curate.py merge` 就是為了補這一刀。
- 週末與台灣時間的清晨，英文媒體常常沒有新文章，這時 `--hours 48` 比較合適。
- RSS 來源會改網址或關閉。抓不到的來源只會印警告，不會中斷整個流程；
  長期抓不到就直接從 `config.toml` 移掉。
