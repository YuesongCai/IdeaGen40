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
  import  (instance) insert every row the database does not already have.

Import is guarded by primary key rather than by table: rows the cloud already
holds win, rows it has never seen arrive, and neither side has to go first. It
is safe to run on every boot, which is what the dashboard's entrypoint does.
"""
from __future__ import annotations

import argparse
import json
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
    # The instance reads through a prefixed store (IDEAGEN_TOS_PREFIX=prod), so
    # an unprefixed upload lands somewhere it will never look — the seed was
    # sitting in the bucket while the dashboard reported no seed at all.
    store = TosBlobStore(ak=ak, sk=sk, bucket=args.bucket, region=args.region,
                         endpoint=args.endpoint, prefix=args.prefix)
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
    # Row-level, not table-level. An empty-table guard looked right and was
    # wrong: `platform --state-probe` writes one row into orch_runs to prove the
    # database round-trips, and the scheduler writes a monitor run within
    # minutes, so by the time this ran the table was never empty and 667 runs of
    # history were skipped in silence. Letting the primary key decide is both
    # idempotent and correct — rows the cloud already has win, rows it has never
    # seen arrive, and the order of the two no longer matters.
    ignore = {"mysql": "INSERT IGNORE INTO", "postgres": "INSERT INTO"}.get(
        getattr(p.state, "dialect", ""), "INSERT OR IGNORE INTO")
    suffix = " ON CONFLICT DO NOTHING" if getattr(
        p.state, "dialect", "") == "postgres" else ""
    # Per table, not one transaction. The first version let a failure on one
    # table abort the whole run, and because the caller only logs to a container
    # nobody can read, the visible result was "the first tables arrived and
    # the later ones silently did not" — which looks like a schema mismatch and is
    # not. Each table now reports its own outcome.
    moved, report = 0, {}
    for t in TABLES:
        if t not in have:
            report[t] = "not in seed"
            continue
        try:
            before = p.state.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            cols = _cols(src, t)
            rows = src.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()
            if not rows:
                report[t] = "seed empty"
                continue
            ph = ",".join("?" * len(cols))
            p.state.executemany(
                f"{ignore} {t} ({','.join(cols)}) VALUES ({ph}){suffix}",
                [tuple(r) for r in rows])
            after = p.state.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            moved += after - before
            report[t] = f"seed {len(rows)}, added {after - before}, had {before}"
        except Exception as e:  # noqa: BLE001 — one bad table must not stop the rest
            report[t] = f"FAILED {type(e).__name__}: {e}"[:300]
        print(f"  {t}: {report[t]}")
    print(f"导入完成，新增 {moved} 行")

    # The instance has no shell and its container logs are unreadable from
    # outside, so the run leaves its own report where the operator can read it:
    # the bucket they already have credentials for.
    try:
        import datetime as _dt
        blob = json.dumps({"at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                           "added": moved, "tables": report},
                          ensure_ascii=False, indent=1).encode()
        p.blobs._c().put_object(p.blobs.bucket, p.blobs._k("seed/last_import.json"),
                                content=blob)
    except Exception as e:  # noqa: BLE001 — reporting must never fail the import
        print(f"  （导入报告写入失败，不影响导入本身: {type(e).__name__}）")
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
    e.add_argument("--prefix", default="prod",
                   help="must match the instance IDEAGEN_TOS_PREFIX")

    i = sub.add_parser("import", help="fill empty tables from the seed (instance)")
    i.set_defaults(fn=cmd_import)
    i.add_argument("--db", default="/app/data/platform_state.db")

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
