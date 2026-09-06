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

ROOT_DIR="$PWD"
LOCKDIR="$ROOT_DIR/data/.publish_snapshot.lock"
HELD=0
WT=""

# ROOT_DIR is captured absolute, on purpose. The old EXIT trap started with
#     cd "$(dirname "$0")/.." 2>/dev/null
# and by the time it ran, the shell was inside $WT and $0 was the *relative*
# "scripts/publish_snapshot.sh" daily.sh invokes — so it tried to cd into
# "scripts/.." from a temp worktree, failed, and `set -e` turned that into
# exit 1. The daily log therefore read
#     published gh-pages 73953c1
#     WARN: gh-pages snapshot publish failed
# on the same successful publish, every single day. A false alarm on a green
# run is the same disease as a green report on a failed one: after a while
# nobody reads either line.
cleanup() {
  cd "$ROOT_DIR" 2>/dev/null || true
  if [ -n "$WT" ]; then
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
    git worktree prune || true
  fi
  if [ "$HELD" = 1 ]; then rm -rf "$LOCKDIR"; fi
}

# One publisher at a time, for the same reason the self-heal below exists and
# is dangerous without this. That eviction is right about a dead run's
# leftovers and badly wrong about a live one: on 2026-09-04 two publishers
# overlapped, the newer one force-removed the older one's worktree, and the
# older one died with
#     fatal: 'gh-pages' is already used by worktree at /var/folders/.../ghp
# Both were also writing web/_site/index.html at the same time. Serialise
# instead of racing; a second caller is a no-op, not a casualty. Asking whether
# the holder is alive (not just how old the file is) means a killed run does
# not block every later one — the same rule sync_to_cloud.py's lock follows.
mkdir -p data
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  holder="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    echo "另一个 publish_snapshot 正在跑（pid $holder），本次跳过"
    exit 0
  fi
  echo "锁的持有者已不在（pid ${holder:-未知}），接手"
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR"
fi
HELD=1
echo $$ > "$LOCKDIR/pid"
trap cleanup EXIT

"$PYBIN" scripts/export_pages.py
"$PYBIN" scripts/check_publish_safety.py web/_site/index.html   # gate: aborts on hit

WT="$(mktemp -d)/ghp"
# Self-heal: a previous run's temp worktree can survive as a stale registration
# (the OS reaps /var/folders temp dirs without telling git), and git then
# refuses to check gh-pages out anywhere else. Prune what's gone, evict what
# still holds the branch — it was always a throwaway, and the lock above means
# whoever registered it is no longer running.
git worktree prune
OLD="$(git worktree list --porcelain | awk '/^worktree /{w=substr($0,10)} /^branch refs\/heads\/gh-pages$/{print w}')"
if [ -n "$OLD" ]; then git worktree remove --force "$OLD" || true; git worktree prune; fi
git worktree add "$WT" gh-pages >/dev/null
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
