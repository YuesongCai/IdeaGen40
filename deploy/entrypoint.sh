#!/usr/bin/env sh
# Container entrypoint: call `tick` forever, and know the difference between
# "this will fix itself" and "this never will".
#
# The loop lives in shell rather than in Python on purpose. Every tick is a fresh
# process, so nothing accumulates between ticks — no leaked connection, no cached
# secret, no half-migrated schema — and a hard crash inside one tick costs one
# tick instead of the schedule. That matches the platform's own rule: the sandbox
# is disposable, and everything that matters was already written through a port.
#
# Exit-code contract with ideagen/scheduler.py:
#   0  healthy — something ran or nothing was due
#   1  degraded — a transient failure; the next tick may well succeed
#   2  unrecoverable — misconfiguration (missing DSN, unsupported venue).
#      Restarting cannot fix it, so we exit non-zero immediately and let the
#      sandbox surface a failed workload instead of crash-looping quietly.
#
# No credentials appear in this file or in its logs. Everything comes from the
# environment the sandbox injects.
set -eu

INTERVAL="${IDEAGEN_TICK_INTERVAL_S:-300}"
# How many consecutive degraded ticks are tolerated before the process gives up.
# Retrying forever would keep a broken sandbox looking alive; failing on the first
# blip would restart the container every time a database hiccups. Roughly an hour
# at the default interval.
MAX_DEGRADED="${IDEAGEN_MAX_DEGRADED:-12}"

echo "[entrypoint] platform=${IDEAGEN_PLATFORM:-local} venue=${IDEAGEN_VENUE:-paper}" \
     "interval=${INTERVAL}s max_degraded=${MAX_DEGRADED}"

# One-shot mode for the first verification run described in the runbook: prove a
# single tick before handing the schedule to the sandbox.
if [ "${1:-loop}" = "once" ]; then
  shift
  exec python3 -m ideagen.scheduler tick --interval "$INTERVAL" "$@"
fi

# Anything else is passed through, so `catch-up` and `health` can be run in this
# same image without a second definition.
if [ "${1:-loop}" != "loop" ]; then
  exec python3 -m ideagen.scheduler "$@"
fi

degraded=0
# A stop signal must not be swallowed: without this, `docker stop` waits the full
# grace period on every deploy because `sleep` ignores TERM in a shell loop.
trap 'echo "[entrypoint] signal received, exiting"; exit 0' TERM INT

while true; do
  set +e
  python3 -m ideagen.scheduler tick --interval "$INTERVAL"
  code=$?
  set -e

  case "$code" in
    0)
      degraded=0
      ;;
    1)
      degraded=$((degraded + 1))
      echo "[entrypoint] degraded tick ${degraded}/${MAX_DEGRADED}"
      if [ "$degraded" -ge "$MAX_DEGRADED" ]; then
        echo "[entrypoint] ${degraded} consecutive degraded ticks — giving up so the" \
             "failure is visible rather than a container that looks alive" >&2
        exit 1
      fi
      ;;
    2)
      echo "[entrypoint] unrecoverable: scheduler reported a configuration fault." \
           "Fix the environment, then redeploy. Not retrying." >&2
      exit 2
      ;;
    *)
      # The tick process died without producing a report at all (OOM kill, SIGKILL,
      # an import that cannot load). Treated as degraded, because a restart is a
      # reasonable response, but never silently.
      degraded=$((degraded + 1))
      echo "[entrypoint] tick exited ${code} without a report (${degraded}/${MAX_DEGRADED})" >&2
      if [ "$degraded" -ge "$MAX_DEGRADED" ]; then
        exit "$code"
      fi
      ;;
  esac

  sleep "$INTERVAL" &
  wait $!
done
