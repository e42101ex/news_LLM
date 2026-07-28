# 每日排程用的提示詞（Claude Cowork / Claude Code 排程）

把 `---` 以下整段貼進排程任務。內容全部在本機產生，只有 `docs/` 的成品會推到 GitHub Pages。

摘要由本機的 OpenAI-compatible 端點（`.env` 裡的 `LLM_MODEL`）產生，你的工作是**抽查與修正**，
不需要逐條重寫 —— 除非摘要明顯有問題。

---

在 `~/auto_report_news` 目錄下執行今天的 AI 新聞彙整：

1. `cd ~/auto_report_news && git pull --rebase`
2. `./run_daily.sh --no-push` —— 抓取、分群、LLM 摘要、產生 HTML（約 3-5 分鐘）。
   - 若主題數為 0：回報「今天沒抓到新聞」並停止。
   - 若腳本警告「沒有任何 LLM 摘要」：跑 `python build.py --llm-test` 診斷後回報，先不要部署。
3. `python curate.py list -v` 檢查結果，只處理這幾種明顯問題：
   - 同一件事被拆成兩個主題 → `python curate.py merge <保留的key> <被併入的key>`
   - 一個主題裡混進不相干的文章 → `python curate.py split <key> <文章編號…>`
     （編號看 `list -v`；拆完記得用 `set` 給新主題寫標題與摘要）
   - 完全不是 AI 新聞（手機規格、汽車、字典出版之類）→ `python curate.py drop <key>`
   - 標題或摘要明顯錯誤、簡體殘留、或與來源內容不符 →
     `python curate.py set <key> --title "…" --summary "…"`
   - 重要度明顯不合理（多家報導的大事卻只有 2） → `--importance <1-5>`
4. 若第 3 步有任何改動，跑 `python build.py --stage render` 重新產生 HTML。
5. `./deploy.sh` 推送到 GitHub Pages。
6. 回報：主題數、報導數、你修正了哪幾項、最重要的 3 則標題、網站網址。

注意事項：
- 只依據 `data/latest.json` 的內容判斷，不要另外上網查證或加入未出現的事實與數字。
- 沒問題的主題不要為了改而改；抽查 5-8 則具代表性的即可，不必逐條看完。
- 任何指令失敗就停下來回報錯誤，不要略過步驟或自行修改程式。
