"""Fetch the newest published state snapshot onto the display node.

The other half of `push_state_to_cloud.py`. The laptop keeps running the daily
cycle and publishing snapshots; this pulls the newest one so the cloud page
tracks the real book instead of the moment the instance was built.

Runs inside the container, using the instance's own credentials from
runtime.env. Deliberately not a presigned URL: those expire in hours, and a
sync path that dies quietly two hours after deployment is the same failure as
having no sync at all, only later and harder to see.

Writes to `--dest`, and leaves the live database alone — swapping a file under
an open SQLite connection is how you get a reader holding a freed page. The
caller moves it into place and restarts the container.

The "which snapshot is installed" marker is written next to `--dest`, not to
its final location, and the caller moves it only after the database itself
lands. A marker that advances on its own would claim we are current while the
old file is still being served, and nothing would ever pull again.

Exit codes are the interface:
    0  a new snapshot was written to --dest
    3  already current, nothing written
    4  the prefix is empty — nobody has ever published here
    1  failed

4 is deliberately not 3. On a node whose entire job is to display published
state, "there is nothing to display" means the sync is misconfigured, and it
must not leave the same trace as a healthy no-op. It already cost one
deployment: the publisher was writing to the laptop's bucket and the node was
listing production's, and because an empty listing reported "already current",
the timer ran every 15 minutes and announced success while nothing was ever
going to arrive.

  python3 scripts/pull_state.py --dest /data/ideagen.db.new
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PREFIX = "deploy/state/"


def digest_of_key(key: str) -> str:
    return key.rsplit("-", 1)[-1][: -len(".db")]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--marker", default="/data/.state-sha",
                    help="记录当前已装载快照的哈希")
    ap.add_argument("--prefix", default=PREFIX)
    args = ap.parse_args(argv)

    from ideagen import platform as plat
    p = plat.load()

    # Say which namespace this is reading. The instance's environment is
    # production, so it is right by construction there; run the same script on
    # the laptop and `plat.load()` hands back the operator's own bucket
    # instead. Both are "successful" reads of different places, which is
    # exactly the confusion that cost a deployment — so name the source out
    # loud rather than leaving it to be inferred.
    b = p.blobs
    src = f"{getattr(b, 'bucket', '?')}/{getattr(b, 'prefix', '')}"
    print(f"PULL_FROM {src}/{args.prefix}", file=sys.stderr)

    keys = sorted(k for k in p.blobs.list(args.prefix) if k.endswith(".db"))
    if not keys:
        print(f"PULL_NONE {getattr(b, 'bucket', '?')}/{getattr(b, 'prefix', '')}"
              f"/{args.prefix} 下没有任何快照——发布端多半写到了别处",
              file=sys.stderr)
        return 4
    key = keys[-1]
    want = digest_of_key(key)

    marker = pathlib.Path(args.marker)
    have = marker.read_text().strip() if marker.exists() else ""
    if have == want:
        print(f"PULL_CURRENT {want}")
        return 3

    data = p.blobs.get(key)
    got = hashlib.sha256(data).hexdigest()[:12]
    if got != want:
        # The key states its own content hash, so a mismatch means the bytes
        # changed underneath the name. Installing them would put a database of
        # unknown provenance on the page.
        print(f"PULL_CORRUPT {key} 期望 {want} 实得 {got}", file=sys.stderr)
        return 1

    dest = pathlib.Path(args.dest)
    dest.write_bytes(data)
    pathlib.Path(str(dest) + ".sha").write_text(want + "\n")
    print(f"PULL_OK {key} {len(data) / 1e6:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
