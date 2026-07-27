#!/usr/bin/env bash
# 把 docs/ 推到 GitHub，由 GitHub Pages（main 分支 /docs）對外提供。
#
#   ./deploy.sh                      # 提交並推送
#   GH_REPO=user/ai-news ./deploy.sh # 第一次跑：順便設定 origin
#
# 需求：本機 git 已能推到該 repo（SSH key 或 credential helper）。
set -euo pipefail
cd "$(dirname "$0")"

BRANCH="${GH_BRANCH:-main}"
MSG="${1:-chore: 更新 AI 每日新聞彙整 $(date +%Y-%m-%d\ %H:%M)}"

if [[ ! -d .git ]]; then
  echo "→ 初始化 git repo"
  git init -q
  git symbolic-ref HEAD "refs/heads/${BRANCH}"
fi

git config user.name  >/dev/null 2>&1 || git config user.name  "${GIT_NAME:-auto-report-news}"
git config user.email >/dev/null 2>&1 || git config user.email "${GIT_EMAIL:-auto-report-news@users.noreply.github.com}"

if ! git remote get-url origin >/dev/null 2>&1; then
  if [[ -n "${GH_REPO:-}" ]]; then
    git remote add origin "git@github.com:${GH_REPO}.git"
    echo "→ 已設定 origin：${GH_REPO}"
  else
    cat <<'EOF'
✗ 還沒有 git remote。請先建立 GitHub repo，然後跑：

    GH_REPO=你的帳號/repo名稱 ./deploy.sh

接著到 GitHub → Settings → Pages，把 Source 設成
「Deploy from a branch」→ Branch: main、Folder: /docs。
EOF
    exit 1
  fi
fi

if [[ -z "$(git status --porcelain docs data)" ]]; then
  echo "→ 內容沒有變化，不需要提交"
  exit 0
fi

git add docs data
git commit -q -m "${MSG}"
git push -u origin "${BRANCH}"

REPO_PATH="$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
OWNER="${REPO_PATH%%/*}"; NAME="${REPO_PATH##*/}"
echo "✓ 已推送。網址：https://${OWNER}.github.io/${NAME}/"
