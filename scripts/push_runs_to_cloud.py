"""Mirror run artifacts into the bucket the cloud display node actually reads.

The state database and the run artifacts travel by different roads, and only
one of them was ever built. `push_state_to_cloud.py` publishes the SQLite
snapshot to production's bucket; the artifacts a run writes — `journal.json`,
`A_topics.json`, `B_pool.json`, every `C_selectors/*.json` — stay in whatever
bucket the machine that ran the week was pointed at. On this laptop that is
`ideagen-3003452293` with no prefix; the display node reads
`ideagen-prod-4b869b/prod`. Both sides worked perfectly and never met.

What that looked like on the page, which is the reason this script exists:

* every "运行日志" opened empty, because `runs/<as_of>/<run>/journal.json`
  resolved to a key that is not in the bucket being read;
* 「问它为什么这么选」 fell back to the two things reachable without artifacts —
  a placeholder for the missing journal and the `verdicts` row from the synced
  database — so a question that should be answered from a 73-name candidate
  pool and a full scoring breakdown got 2,588 characters and no pool at all;
* and the dashboard, having no way to tell "absent" from "unreachable",
  explained all of it as "这不是掩饰，是当时就没写" — which was false. The
  artifacts existed the whole time.

So this is a data-plane fix, not a code one: the code changes that ship with it
make the failure legible, and this makes it stop happening.

Only artifacts move. The corpus, the books and the positions live in the state
database and travel with the snapshot; nothing here reads or writes those.

    python3 scripts/push_runs_to_cloud.py             # the newest weekly run
    python3 scripts/push_runs_to_cloud.py --runs 8    # the last 8 weekly runs
    python3 scripts/push_runs_to_cloud.py --dry-run   # list what would move
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def production_blobs():
    """The store the display node reads, addressed with the laptop's credentials."""
    from ideagen.platform.byteplus import TosBlobStore
    import build_runtime_env as bre
    return TosBlobStore(ak=bre.readenv("BYTEPLUS_ACCESS_KEY"),
                        sk=bre.readenv("BYTEPLUS_SECRET_KEY"),
                        bucket=bre.TOS_BUCKET, endpoint=bre.TOS_ENDPOINT,
                        prefix=bre.TOS_PREFIX)


def runs_to_mirror(p, n: int, kinds: tuple[str, ...]) -> list[dict]:
    marks = ",".join("?" * len(kinds))
    rows = p.state.q(
        f"SELECT run_id, as_of, kind FROM orch_runs WHERE kind IN ({marks}) "
        "AND ok=1 ORDER BY started_at DESC LIMIT ?", (*kinds, n))
    return [dict(r) for r in rows]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1,
                    help="镜像最近几次运行（默认 1，即最新一次）")
    ap.add_argument("--kinds", default="weekly",
                    help="逗号分隔的运行类型，默认只发周跑")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from ideagen import platform as plat
    src = plat.load().blobs
    dst = production_blobs()
    if getattr(src, "bucket", None) == dst.bucket and \
            getattr(src, "prefix", "") == dst.prefix:
        print("源与目标是同一个桶和前缀，无需镜像")
        return 0

    p = plat.load()
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    runs = runs_to_mirror(p, args.runs, kinds)
    if not runs:
        print(f"orch_runs 里没有 kind in {kinds} 的成功运行")
        return 1

    moved = skipped = failed = 0
    for r in runs:
        prefix = f"runs/{r['as_of']}/{r['run_id']}/"
        keys = sorted(src.list(prefix))
        if not keys:
            print(f"⚠ {r['run_id']}（{r['as_of']} 期）在源桶里没有产物")
            continue
        print(f"{r['run_id']} · {r['as_of']} 期 · {len(keys)} 件")
        for k in keys:
            # The destination refuses overwrites by design, and an artifact is
            # immutable, so an object already there is already correct.
            if dst.exists(k):
                skipped += 1
                continue
            if args.dry_run:
                print(f"  会发 {k}")
                moved += 1
                continue
            try:
                dst.put(k, src.get(k), content_type="application/json")
                moved += 1
            except Exception as e:  # noqa: BLE001 — one bad object, not the batch
                failed += 1
                print(f"  ✗ {k}: {type(e).__name__}: {e}")
    verb = "会发" if args.dry_run else "已发"
    print(f"{verb} {moved} 件 · 已存在跳过 {skipped} 件"
          + (f" · 失败 {failed} 件" if failed else ""))
    print(f"目标 tos://{dst.bucket}/{dst.prefix}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
