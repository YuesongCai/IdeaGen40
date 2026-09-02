#!/usr/bin/env bash
# Unattended half of the daily cycle. The generation step needs Claude and is
# therefore not here; see prompts/idea_generation.md.
#
# Scheduled on macOS via a launchd agent rather than cron: cron needs Full Disk
# Access, and launchd cannot reach ~/Downloads at all (TCC), which is why the
# install lives at ~/IdeaGen40.
#   ~/Library/LaunchAgents/com.ideagen40.daily.plist   07:23 HKT, Mon-Fri
#   launchctl start com.ideagen40.daily                run it now
set -euo pipefail
cd "$(dirname "$0")/.."

# launchd starts with a minimal PATH and resolves a different python3 than an
# interactive shell does. Pin the interpreter that actually has the deps, and
# let IDEAGEN_PYTHON override it.
PYBIN="${IDEAGEN_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
mkdir -p data/logs
[ -f "$HOME/.ideagen.env" ] && set -a && . "$HOME/.ideagen.env" && set +a
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="

# doctor is informational: it prints what is reachable and exits non-zero only if
# OpenD is down. A missing price feed means marks would be wrong, so that case
# stops the run; everything else is recorded per stage by `daily` itself.
if ! "$PYBIN" -m ideagen.cli doctor; then
  echo "ABORT: Futu OpenD unreachable — start Futu_OpenD and log in, then:"
  echo "       launchctl start com.ideagen40.daily"
  exit 1
fi

"$PYBIN" -m ideagen.cli daily

# Publish the refreshed dashboard snapshot to GitHub Pages. Non-fatal: a push
# failure must not mark the whole run failed, since the marks and attribution
# already landed. publish_snapshot.sh is the current path (state+journal baked
# into a static page, scrubbed, gated); publish_pages.sh publishes the legacy
# report whose partner shelf data the gate rightly refuses every day.
if ! scripts/publish_snapshot.sh; then
  echo "WARN: gh-pages snapshot publish failed; local dashboard is still current"
fi
