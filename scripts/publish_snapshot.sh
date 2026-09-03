#!/usr/bin/env bash
# Publish the static dashboard snapshot to GitHub Pages — the current path.
#
# Replaces publish_pages.sh in the daily cycle: that script publishes the
# legacy report, whose contents include partner shelf data the publish gate
# rightly refuses, so the daily log ended with the same WARN every single day.
# This path bakes /api/state + /api/journal into web/_site/index.html
# (export_pages.py scrubs identities), runs the same non-skippable safety
# gate, and pushes to the gh-pages branch via a throwaway worktree.
set -euo pipefail
cd "$(dirname "$0")/.."

PYBIN="${IDEAGEN_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"

"$PYBIN" scripts/export_pages.py
"$PYBIN" scripts/check_publish_safety.py web/_site/index.html   # gate: aborts on hit

WT="$(mktemp -d)/ghp"
# Self-heal: a previous run's temp worktree can survive as a stale registration
# (the OS reaps /var/folders temp dirs without telling git), and git then
# refuses to check gh-pages out anywhere else. Prune what's gone, evict what
# still holds the branch — it was always a throwaway.
git worktree prune
OLD="$(git worktree list --porcelain | awk '/^worktree /{w=substr($0,10)} /^branch refs\/heads\/gh-pages$/{print w}')"
if [ -n "$OLD" ]; then git worktree remove --force "$OLD" || true; git worktree prune; fi
git worktree add "$WT" gh-pages >/dev/null
trap 'cd "$(dirname "$0")/.." 2>/dev/null; git worktree remove --force "$WT" >/dev/null 2>&1; git worktree prune' EXIT
cp web/_site/index.html "$WT/"
touch "$WT/.nojekyll"
cd "$WT"
git add -A
if git diff --cached --quiet; then
  echo "snapshot unchanged; nothing to publish"
  exit 0
fi
git commit -q -m "snapshot: $(date '+%Y-%m-%d %H:%M %Z') 自动刷新（daily）"
git push -q origin gh-pages
echo "published gh-pages $(git rev-parse --short HEAD)"
