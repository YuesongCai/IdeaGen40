"""Publish the state database to the object store for the cloud display node.

The cloud dashboard reads a SQLite file, not the laptop's live one. Without
this it freezes at whatever was true when the instance was built — and a page
that looks alive while showing last week's positions is worse than one that
admits it is stale.

The destination is production's bucket and prefix, imported from
`build_runtime_env` — not whatever `~/.ideagen.env` points at. The laptop's own
bucket is a different one, and the first version of this script inherited it:
snapshots were published successfully, the instance listed successfully, and
the two were looking at different namespaces. Nothing errored. The node simply
reported "already current" forever.

Keys are never reused. `BlobStore.put` refuses to overwrite on purpose ("write
a new run"), so each snapshot is its own object and the newest is found by
listing rather than by a mutable pointer:

    deploy/state/20260905T013000Z-a1b2c3d4e5f6.db
                 ^ when                ^ sha256[:12]

Putting the digest in the key means the puller learns both the ordering and the
content identity from one `list` call — no extra request, and no marker object
that can disagree with the data it points at.

The snapshot goes through SQLite's backup API, which matters more than it
looks. The laptop keeps a dashboard process holding this database open in WAL
mode, so recent commits live in `ideagen.db-wal` and not yet in `ideagen.db` —
copying the file alone would publish a database missing the newest runs while
appearing perfectly intact. `backup()` reads through a connection, so it sees
the WAL, and it takes a consistent view rather than a possibly torn page.

Deduplication of *what is worth publishing* is not done here. `sync_to_cloud.py`
owns that decision — it fingerprints the tables a reader would notice and calls
this script only when they move. A second gate here looked like belt and braces
and was actually a trap: this script returning 0 without uploading would let
that caller record a publish that never happened. What is kept is the narrow
check that the identical database is already in the bucket.

The upload timeout is raised well above the SDK's 30s default. A 66MB snapshot
over the operator link does not finish in 30s, and the failure arrives as
`http request timeout` — which reads like a network fault rather than a limit
that was never going to be enough. That single default was behind most of the
sync's failed runs.

  python3 scripts/push_state_to_cloud.py [--prefix deploy/state/]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PREFIX = "deploy/state/"


# 48MB over the operator link does not finish in the SDK default 30s.
UPLOAD_TIMEOUT_S = 600


def snapshot() -> bytes:
    src = ROOT / "data" / "ideagen.db"
    if not src.exists():
        raise SystemExit(f"找不到状态库 {src}")
    tmp = pathlib.Path(tempfile.gettempdir()) / "ideagen_push.db"
    tmp.unlink(missing_ok=True)
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(tmp))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data


def production_blobs():
    """The store the display node reads, addressed with the laptop's credentials."""
    from ideagen.platform.byteplus import TosBlobStore
    import build_runtime_env as bre
    return TosBlobStore(ak=bre.readenv("BYTEPLUS_ACCESS_KEY"),
                        sk=bre.readenv("BYTEPLUS_SECRET_KEY"),
                        bucket=bre.TOS_BUCKET, endpoint=bre.TOS_ENDPOINT,
                        prefix=bre.TOS_PREFIX, timeout_s=UPLOAD_TIMEOUT_S)


def latest_digest(blobs, prefix: str) -> str | None:
    """The sha carried by the newest published snapshot, if there is one."""
    keys = sorted(k for k in blobs.list(prefix) if k.endswith(".db"))
    if not keys:
        return None
    return keys[-1].rsplit("-", 1)[-1][: -len(".db")]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--force", action="store_true",
                    help="内容未变也重新发布")
    args = ap.parse_args(argv)

    blobs = production_blobs()
    data = snapshot()
    digest = hashlib.sha256(data).hexdigest()[:12]

    # Whether this exact database is already up there. Cheap, and it also
    # covers the case where a previous run uploaded successfully but the
    # caller never learned it did.
    if not args.force and latest_digest(blobs, args.prefix) == digest:
        print(f"云端已是同一份（{digest}），跳过上传")
        return 0

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{args.prefix}{stamp}-{digest}.db"
    blobs.put(key, data, content_type="application/x-sqlite3")
    # Only after the upload lands. A marker written first would make the next
    # run skip a snapshot that never arrived.
    print(f"已发布 {len(data) / 1e6:.1f} MB → "
          f"tos://{blobs.bucket}/{blobs.prefix}/{key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
