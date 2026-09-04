#!/bin/sh
# Install the newest published state snapshot on the display node.
#
# Runs from a systemd timer. The pull itself happens inside the app image,
# which already has the TOS client and the instance's credentials; this script
# only decides whether to swap the file and how.
#
# The container is stopped for the swap rather than restarted around it. SQLite
# keeps `-wal` and `-shm` beside the database, and a fresh database next to a
# stale WAL reads as neither one — so both sidecars go with the old file, while
# nothing holds a handle.
set -u

DATA=/opt/ideagen/data
CONF=/opt/ideagen/config/runtime.env
IMG=ideagen40:live
NAME=ideagen-dash

docker run --rm --env-file "$CONF" -v "$DATA":/data \
  --entrypoint python3 "$IMG" \
  /app/scripts/pull_state.py --dest /data/ideagen.db.new --marker /data/.state-sha
rc=$?

case "$rc" in
  0) ;;
  3) echo "IG_SYNC_CURRENT $(date -u +%FT%TZ)"; exit 0 ;;
  *) echo "IG_SYNC_FAIL rc=$rc $(date -u +%FT%TZ)"; exit 1 ;;
esac

[ -s "$DATA/ideagen.db.new" ] || { echo "IG_SYNC_FAIL 空快照"; exit 1; }

docker stop "$NAME" >/dev/null 2>&1
mv -f "$DATA/ideagen.db.new" "$DATA/ideagen.db"
mv -f "$DATA/ideagen.db.new.sha" "$DATA/.state-sha"
rm -f "$DATA/ideagen.db-wal" "$DATA/ideagen.db-shm"
chown 10001:10001 "$DATA/ideagen.db" "$DATA/.state-sha"
chmod 664 "$DATA/ideagen.db"
docker start "$NAME" >/dev/null 2>&1

echo "IG_SYNC_OK $(cat "$DATA/.state-sha") $(date -u +%FT%TZ)"
