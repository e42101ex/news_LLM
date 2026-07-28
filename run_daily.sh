#!/usr/bin/env bash
# 每日一鍵：在本機抓取 → 分群 → LLM 摘要 → 產生 HTML → 推到 GitHub Pages。
#
#   ./run_daily.sh              # 完整跑（含推送）
#   ./run_daily.sh --no-push    # 只產生，不推送（先看結果再決定）
#   ./run_daily.sh --hours 48   # 放寬收錄範圍（週一、連假時好用）
#
# 所有內容都在本機產生，GitHub 只接收 docs/ 的成品。
set -uo pipefail
cd "$(dirname "$0")"

PUSH=1
BUILD_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH=0 ;;
    *) BUILD_ARGS+=("$arg") ;;
  esac
done

mkdir -p logs
LOG="logs/$(date +%Y-%m-%d).log"
exec > >(tee -a "$LOG") 2>&1
echo "════ $(date '+%Y-%m-%d %H:%M:%S') 開始 ════"

# 找 python：優先用 $PYTHON（crontab 會指定），否則 conda env，最後才是 PATH 上的。
# cron 的 PATH 只有 /usr/bin:/bin，會抓到沒裝套件的系統 python，所以這裡要講清楚。
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for cand in "$HOME/miniforge3/envs/py312/bin/python3" "$(command -v python3 || true)"; do
    [[ -x "$cand" ]] && PY="$cand" && break
  done
fi
if [[ -z "$PY" ]]; then
  echo "✗ 找不到 python3。請設定 PYTHON=/path/to/python3 後重試"
  exit 1
fi

# 先確認套件都在（cron 環境最常見的失敗原因）
if ! "$PY" -c "import feedparser, jinja2, requests" 2>/dev/null; then
  echo "✗ $PY 缺少必要套件（feedparser / jinja2 / requests）"
  echo "  安裝：$PY -m pip install -r requirements.txt"
  echo "  或改用其他 interpreter：PYTHON=/path/to/python3 $0"
  exit 1
fi
echo "使用 python: $PY"

if ! "$PY" -u build.py --llm auto "${BUILD_ARGS[@]}"; then
  echo "✗ 產生失敗，未推送（詳見 $LOG）"
  exit 1
fi

TOPICS=$("$PY" -c "import json;d=json.load(open('data/latest.json'));print(d['topic_count'])" 2>/dev/null || echo "?")
ENRICHED=$("$PY" -c "
import json;d=json.load(open('data/latest.json'))
print(sum(1 for t in d['topics'] if t['llm_enriched']))" 2>/dev/null || echo "?")
echo "→ 產出 ${TOPICS} 個主題（其中 ${ENRICHED} 個有 LLM 摘要）"

if [[ -f data/social.json ]]; then
  SOCIAL=$("$PY" -c "
import json;d=json.load(open('data/social.json'))
print(f\"{len(d['trends'])} 熱搜 / {len(d['bsky_trends'])} 話題 / {len(d['bsky_posts'])} 貼文\")" 2>/dev/null || echo "?")
  echo "→ 社群熱門：${SOCIAL}"
fi

if [[ "$ENRICHED" == "0" && "$TOPICS" != "0" ]]; then
  echo "⚠ 沒有任何 LLM 摘要 —— 檢查 .env 或端點：python build.py --llm-test"
fi

if [[ "$PUSH" -eq 0 ]]; then
  echo "（--no-push：跳過推送，可先開 docs/index.html 檢查）"
  echo "════ 完成 ════"
  exit 0
fi

if ! ./deploy.sh; then
  echo "✗ 推送失敗。內容已產生在 docs/，稍後可重跑 ./deploy.sh"
  exit 1
fi

# 只留最近 30 天的日誌
ls -1t logs/*.log 2>/dev/null | tail -n +31 | xargs -r rm --
echo "════ 完成 ════"
