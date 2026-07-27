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

PY="$(command -v python3)"
if ! "$PY" -u build.py --llm auto "${BUILD_ARGS[@]}"; then
  echo "✗ 產生失敗，未推送（詳見 $LOG）"
  exit 1
fi

TOPICS=$("$PY" -c "import json;d=json.load(open('data/latest.json'));print(d['topic_count'])" 2>/dev/null || echo "?")
ENRICHED=$("$PY" -c "
import json;d=json.load(open('data/latest.json'))
print(sum(1 for t in d['topics'] if t['llm_enriched']))" 2>/dev/null || echo "?")
echo "→ 產出 ${TOPICS} 個主題（其中 ${ENRICHED} 個有 LLM 摘要）"

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
