"""Carry the laptop's recorded history into the cloud database, once.

The instance came up with every port green and nothing to show: a dashboard
whose panels all read `orch_runs`, `verdicts`, `candidates` and the backtest
tables, against a database that had just been created. A page that renders
perfectly and says nothing happened is not a working system — it is a working
system's silhouette.

The laptop is the only place that history exists, and it cannot reach the RDS
instance (VPC-only). So the transfer goes the way everything else does here: a
SQLite file to object storage, and the instance reading it from the inside.

Two halves, deliberately separate:

  export  (laptop)   build a small SQLite holding only the platform tables and
                     put it in the bucket.
  import  (instance) if a table is empty, fill it from that file.

Import is guarded per table rather than globally: a partly-seeded database
should finish, and a table the cloud has since written to must never be
overwritten by a laptop's older copy. It is safe to run on every boot, which is
what the dashboard's entrypoint does.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEED_KEY = "seed/platform_state.db"

#: Tables worth carrying, in dependency order. Everything the dashboard reads
#: through the platform port, and nothing else — the corpus bodies are licensed
#: research that already lives in the bucket, and prices resync themselves.
TABLES: tuple[str, ...] = (
    "orch_runs",
    "feed_runs",
    "verdicts",
    "candidates",
    "events",
    "backtest_runs",
    "backtest_points",
    "backtest_positions",
)


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def cmd_export(args) -> int:
    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    have = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out = Path(tempfile.mkdtemp()) / "platform_state.db"
    dst = sqlite3.connect(out)
    total = 0
    for t in TABLES:
        if t not in have:
            print(f"  {t}: 源库没有这张表，跳过")
            continue
        ddl = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (t,)).fetchone()[0]
        dst.execute(ddl)
        cols = _cols(src, t)
        rows = src.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()
        if rows:
            dst.executemany(
                f"INSERT INTO {t} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})", rows)
        total += len(rows)
        print(f"  {t}: {len(rows)} 行")
    dst.commit()
    dst.close()
    size = out.stat().st_size
    print(f"导出 {total} 行 / {size // 1024}KB")

    from ideagen.platform.byteplus import TosBlobStore
    import os
    ak = os.environ.get("BYTEPLUS_ACCESS_KEY") or os.environ["VOLCENGINE_ACCESS_KEY"]
    sk = os.environ.get("BYTEPLUS_SECRET_KEY") or os.environ["VOLCENGINE_SECRET_KEY"]
    store = TosBlobStore(ak=ak, sk=sk, bucket=args.bucket, region=args.region,
                         endpoint=args.endpoint)
    # The blob port refuses to overwrite, because run artifacts are immutable.
    # A seed is not an artifact; it is a copy that should be replaceable.
    store._c().put_object(args.bucket, store._k(SEED_KEY),
                          content=out.read_bytes())
    print(f"已上传 tos://{args.bucket}/{SEED_KEY}")
    return 0


def cmd_import(args) -> int:
    from ideagen import platform as plat, schema
    p = plat.load()
    if not Path(args.db).exists():
        print(f"没有种子文件 {args.db}，跳过")
        return 0
    schema.migrate(p.state)
    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    have = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    moved = 0
    for t in TABLES:
        if t not in have:
            continue
        n = p.state.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        if n:
            print(f"  {t}: 云端已有 {n} 行，不覆盖")
            continue
        cols = _cols(src, t)
        rows = src.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()
        if not rows:
            continue
        ph = ",".join("?" * len(cols))
        p.state.executemany(
            f"INSERT INTO {t} ({','.join(cols)}) VALUES ({ph})",
            [tuple(r) for r in rows])
        moved += len(rows)
        print(f"  {t}: 写入 {len(rows)} 行")
    print(f"导入完成，共 {moved} 行")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser("seed_cloud_state")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="build the seed and upload it (laptop)")
    e.set_defaults(fn=cmd_export)
    e.add_argument("--db", default=str(ROOT / "data" / "ideagen.db"))
    e.add_argument("--bucket", default="ideagen-prod-4b869b")
    e.add_argument("--region", default="ap-southeast-1")
    e.add_argument("--endpoint", default="tos-ap-southeast-1.bytepluses.com")

    i = sub.add_parser("import", help="fill empty tables from the seed (instance)")
    i.set_defaults(fn=cmd_import)
    i.add_argument("--db", default="/app/data/platform_state.db")

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
