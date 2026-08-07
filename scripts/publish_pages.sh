#!/usr/bin/env bash
# Publish the dashboard to GitHub Pages.
#
# ⚠️  THIS MAKES THE FULL POSITION BLOTTER PUBLIC. github.com/YuesongCai/IdeaGen40
# is a public repository, so anything pushed to the gh-pages branch is served at a
# public, indexable URL: every idea, every fill price, every open position.
#
# The owner asked for a public URL explicitly, so the daily run calls this with
# --yes. To stop publishing, remove the publish-pages stage from scripts/daily.sh,
# or make the repository private:
#     gh repo edit YuesongCai/IdeaGen40 --visibility private --accept-visibility-change-consequences
#
# Usage:  scripts/publish_pages.sh [--yes]
set -euo pipefail
cd "$(dirname "$0")/.."
PYBIN="${IDEAGEN_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"

# --yes skips the prompt (used by the daily run). Interactive callers still get it.
if [ "${1:-}" != "--yes" ]; then
  read -r -p "这会把完整持仓明细发布到公开 URL。继续？输入 yes： " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 1; }
fi

"$PYBIN" -m ideagen.cli dashboard --public
TMP="$(mktemp -d)"
cp web/index.html "$TMP/index.html"
cp web/report.json "$TMP/report.json"
echo "" > "$TMP/.nojekyll"

git fetch origin gh-pages 2>/dev/null || true
if git show-ref --quiet refs/remotes/origin/gh-pages; then
  git worktree add -f /tmp/ig40-pages gh-pages
else
  git worktree add -f --detach /tmp/ig40-pages
  git -C /tmp/ig40-pages checkout --orphan gh-pages
  git -C /tmp/ig40-pages rm -rf . 2>/dev/null || true
fi
cp "$TMP"/index.html "$TMP"/report.json "$TMP"/.nojekyll /tmp/ig40-pages/
git -C /tmp/ig40-pages add -A
git -C /tmp/ig40-pages commit -q -m "dashboard $(date '+%Y-%m-%d %H:%M %Z')" || echo "no change"
git -C /tmp/ig40-pages push -q origin gh-pages
git worktree remove --force /tmp/ig40-pages
rm -rf "$TMP"

echo "pushed to gh-pages. 首次需要在仓库 Settings → Pages 里把 source 设为 gh-pages 分支。"
echo "地址： https://yuesongcai.github.io/IdeaGen40/"
