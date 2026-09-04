#!/bin/sh
# Track origin/main on the single-container display node.
#
# The sibling of sync_state.sh: that one keeps the data current, this one keeps
# the code current. Without it the node is frozen at whatever commit it cloned
# at boot, and the only way to pick up a fix is to build a new instance — which
# is how this node ended up eleven commits behind in an afternoon.
#
# deploy/updater.sh does this job for the compose stack. It is not reusable
# here: it deploys with `docker compose up -d`, and this node is one plain
# container. The shape is deliberately copied from it, including the part that
# matters most —
#
# THE TESTS ARE THE GATE. Several agents push to main, so "whoever can push can
# change production" with no human in between. Building the new image and
# running the suite inside it before switching is what keeps that from also
# meaning "whoever can push can break production". A build that fails is
# reported and NOT deployed; the running container is left alone.
#
# Coordination: sync_state.sh stops the container to swap the database, and
# this script replaces the container. Interleaved, one can start the old
# container back up around a half-finished swap. Both take the same lock.
set -u

APP=/opt/ideagen/app
DATA=/opt/ideagen/data
CONF=/opt/ideagen/config/runtime.env
NAME=ideagen-dash
LOCK=/var/lock/ideagen-deploy.lock
REF="${IDEAGEN_UPDATE_REF:-origin/main}"
REQUIRE_TESTS="${IDEAGEN_UPDATE_REQUIRE_TESTS:-1}"

git -C "$APP" fetch -q --tags origin main 2>/dev/null || {
  echo "IG_CODE_FETCH_FAIL $(date -u +%FT%TZ)"; exit 1; }

have=$(git -C "$APP" rev-parse --short HEAD 2>/dev/null)
want=$(git -C "$APP" rev-parse --short "$REF" 2>/dev/null)
[ -n "$want" ] || { echo "IG_CODE_NO_REF $REF"; exit 1; }
if [ "$have" = "$want" ]; then
  echo "IG_CODE_CURRENT $have $(date -u +%FT%TZ)"; exit 0
fi

echo "IG_CODE_UPDATE $have -> $want"
git -C "$APP" reset -q --hard "$REF"

# Build under a throwaway tag. Tagging :live before the suite runs would leave
# the name pointing at an image nothing has vouched for, and the next restart
# would quietly adopt it.
if ! docker build -q -t "ideagen40:cand-$want" -f "$APP/deploy/Dockerfile" "$APP" >/dev/null 2>&1; then
  echo "IG_CODE_BUILD_FAIL $want"; exit 1
fi

if [ "$REQUIRE_TESTS" = "1" ]; then
  if docker run --rm --entrypoint python3 "ideagen40:cand-$want" \
       -m pytest -q -x >/tmp/sync_code_tests.log 2>&1; then
    echo "IG_CODE_TESTS_OK $want"
  else
    tail=$(tail -3 /tmp/sync_code_tests.log | tr '\n' ' ' | cut -c1-240)
    echo "IG_CODE_TESTS_FAIL $want $tail"
    docker rmi -f "ideagen40:cand-$want" >/dev/null 2>&1
    exit 1
  fi
fi

(
  flock -w 300 9 || { echo "IG_CODE_LOCK_TIMEOUT"; exit 1; }
  docker tag "ideagen40:cand-$want" ideagen40:live
  docker rm -f "$NAME" >/dev/null 2>&1
  docker run -d --name "$NAME" --restart always \
    --env-file "$CONF" \
    -e IDEAGEN_DASH_HOST=0.0.0.0 -e IDEAGEN_DB=/data/ideagen.db \
    -v "$DATA":/data -p 80:8765 -p 443:8765 \
    --entrypoint python3 ideagen40:live -m ideagen.cli serve --port 8765 \
    >/dev/null 2>&1 \
    && echo "IG_CODE_DEPLOYED $want $(date -u +%FT%TZ)" \
    || { echo "IG_CODE_RUN_FAIL $want"; exit 1; }
) 9>"$LOCK"

docker rmi -f "ideagen40:cand-$want" >/dev/null 2>&1
docker image prune -f >/dev/null 2>&1
