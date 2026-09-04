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
# 日志只增不减，一年后它比数据库还大。超过 8MB 就留尾部 2000 行——
# 排障看的永远是最近几次，而磁盘满会让整条链路(计价、发布)一起失败。
for f in data/logs/daily.log data/logs/scheduler_tick.log; do
  [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 8388608 ] && {
    tail -2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "$(date -u +%FT%TZ) 日志已轮转（保留最后 2000 行）" >> "$f"
  }
done
[ -f "$HOME/.ideagen.env" ] && set -a && . "$HOME/.ideagen.env" && set +a
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="

# doctor is informational: it prints what is reachable and exits non-zero only if
# OpenD is down. A missing price feed means marks would be wrong, so that case
# stops the run; everything else is recorded per stage by `daily` itself.
# OpenD is a GUI app that does not come back after a reboot, and a marking run
# that aborts because nobody launched it is a day of missing marks discovered
# later. Try to start it first — it restores its saved session — and only give
# up if the port stays shut.
if ! "$PYBIN" -m ideagen.cli doctor; then
  if [ -d /Applications/Futu_OpenD.app ]; then
    echo "Futu OpenD 未响应，尝试启动…"
    open -a /Applications/Futu_OpenD.app || true
    for i in $(seq 1 20); do
      if nc -z 127.0.0.1 11111 2>/dev/null; then echo "OpenD 端口已开"; break; fi
      sleep 3
    done
  fi
fi
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

# Hand the cloud display node today's state. Without this it keeps serving
# whatever was true when its instance was built, and looks entirely healthy
# while doing it — the failure mode nobody catches by looking. Non-fatal for
# the same reason as the publish above: the marks already landed.
if ! "$PYBIN" scripts/push_state_to_cloud.py; then
  echo "WARN: 状态快照未发布到对象存储；云端页面会停在上一份快照"
fi
