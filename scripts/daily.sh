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

# 只跑一次，但一定要跑到。
#
# launchd 的日历触发有个洞：LaunchAgent 只在有人登录之后才存在，而错过的日历
# 任务它不会补 —— 登录晚于 07:23 的那一天，这一枪就打在空处，`runs` 不动、没有
# 任何报错。这台机器的登录时间并不保证早于 07:23（2026-09-05 是 04:51 开机、
# 09:03 才登录控制台；那天是周六没轮到它，同样的时间差落在工作日就是一次静默
# 漏跑）。所以 plist 配了 RunAtLoad=true：每次加载都来敲一次门，由这里判断今天
# 该不该开门。
#
# 判据是**结果**（runs 表里今天有没有一次成功），不是本脚本自己记的账：自己记的
# 账会在「记完了但活没干成」的那半秒里说谎。已经跑过就整段退出，因为下面的快照
# 发布和状态推送都不便宜（一次 65MB），每次登录重跑一遍是另一种坏掉。
TODAY="$(TZ=Asia/Hong_Kong date +%F)"
if [ "${IDEAGEN_DAILY_FORCE:-0}" != "1" ]; then
  if [ "$(TZ=Asia/Hong_Kong date +%u)" -gt 5 ]; then
    echo "$TODAY 是周末，daily 不跑（要强制：IDEAGEN_DAILY_FORCE=1）"
    exit 0
  fi
  if "$PYBIN" - "$TODAY" <<'PYEOF'
import os, pathlib, sqlite3, sys
db = pathlib.Path(os.environ.get("IDEAGEN_DB", "data/ideagen.db"))
if not db.exists():
    raise SystemExit(1)                      # 没有库 = 今天当然还没跑过
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
try:
    row = con.execute("SELECT 1 FROM runs WHERE as_of=? AND status='ok' "
                      "LIMIT 1", (sys.argv[1],)).fetchone()
except sqlite3.Error:
    raise SystemExit(1)                      # 读不到 ≠ 跑过了，宁可多跑一次
raise SystemExit(0 if row else 1)
PYEOF
  then
    echo "$TODAY 已经有一次成功的 daily，跳过（要强制：IDEAGEN_DAILY_FORCE=1）"
    exit 0
  fi
fi

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

# The artifacts used to be mirrored here, bounded to the newest four weekly
# runs — and that bound is narrower than the gap between two firings of this
# script, which is how a window loses things. Five weekly backfills ran on
# 2026-09-04 after that morning's 07:23; by the next scheduled firing (Monday
# 09-08) the two oldest had already fallen out of `--runs 4` and no timer would
# ever have carried them. The leg now lives on the ten-minute tick, where a
# window of four cannot be outrun, and where a failure raises the same
# throttled alert as the code and data legs instead of a WARN in this log:
#
#   python3 scripts/sync_to_cloud.py --only artifacts
#
# Deliberately not also called from here. Two mirrors racing on the same new
# object would have one of them refused by the destination's no-overwrite rule
# and reported as a failure that is not one.
