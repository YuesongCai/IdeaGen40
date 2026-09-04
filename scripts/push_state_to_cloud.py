"""Publish the state database to the object store for the cloud display node.

The cloud dashboard reads a SQLite file, not the laptop's live one. Without
this it freezes at whatever was true when the instance was built — and a page
that looks alive while showing last week's positions is worse than one that
admits it is stale.

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

That open dashboard is also why the unchanged-content check below almost never
fires: serving a page writes to the database, so two snapshots taken minutes
apart differ even with no new runs. The check is kept for the case it does
cover — the same snapshot published twice on an idle machine — and is not a
meaningful bandwidth saving.

  python3 scripts/push_state_to_cloud.py [--prefix deploy/state/]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PREFIX = "deploy/state/"


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

    from ideagen import platform as plat
    p = plat.load()

    data = snapshot()
    digest = hashlib.sha256(data).hexdigest()[:12]

    if not args.force and latest_digest(p.blobs, args.prefix) == digest:
        print(f"状态未变（{digest}），跳过发布")
        return 0

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{args.prefix}{stamp}-{digest}.db"
    p.blobs.put(key, data, content_type="application/x-sqlite3")
    print(f"已发布 {len(data) / 1e6:.1f} MB → {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
