#!/usr/bin/env python3
"""Date the shelf, so an as-of replay can actually exclude something.

`universe.eligible(as_of=...)` drops instruments the shelf had not listed by the
replayed day. The gate has been in place for a while and has never fired: no
writer ever populated `instruments.first_seen_d`, so all 156 rows were undated,
and an undated row is admitted by design — dropping them would silently empty
every historical universe. The backtest disclaimer has been reporting that count
honestly ("156 个标的缺少上架日期，按当期资格过滤时一律放行"), which is the
right thing to say and the wrong thing to keep saying.

Two sources, in order of authority:

1. `get_stock_basicinfo(...).listing_date` — the real thing where OpenD carries
   it, which is HK securities and US common stock. For US ETFs, most of this
   universe, OpenD returns the epoch sentinel; `futu_px.listing_dates` drops it
   rather than storing a date that would pass every gate.

2. The earliest daily bar OpenD will serve. This is a bound, not an inception:
   US history stops around 2006-08-21 whatever the security's age, so SPY and
   GLD both land on that day. Below the cap it is exact — KMLM 2020-12-02,
   DBMF 2019-05-08, their actual launches. The value is therefore never earlier
   than the true listing date, and a gate fed with it can only exclude an
   instrument that did exist; it can never admit one that did not. For a replay
   that is the safe direction, and the opposite of the one an undated row takes.

Which source answered is kept in `instruments.meta.first_seen_src`, because
"dated" and "dated well" are not the same claim and the backtest note has to be
able to tell them apart. Funds and structured products carry no `futu_code` and
stay undated here; they are dated by the Olive shelf snapshot instead.

Idempotent: only fills rows that have no date. `--refresh` re-resolves every
row, `--codes A,B` narrows to a few, `--dry-run` resolves and reports without
writing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideagen import db  # noqa: E402
from ideagen.sources import futu_px  # noqa: E402


def _targets(con, *, refresh: bool, only: list[str]) -> list[dict]:
    rows = [dict(r) for r in db.q(
        con,
        "SELECT key, futu_code, name, first_seen_d, meta FROM instruments "
        "WHERE futu_code IS NOT NULL AND futu_code<>'' ORDER BY key")]
    if only:
        want = {c.upper() for c in only}
        rows = [r for r in rows
                if r["key"].upper() in want or str(r["futu_code"]).upper() in want]
    if not refresh:
        rows = [r for r in rows if not (r.get("first_seen_d") or "").strip()]
    return rows


def _write(con, key: str, d: str, src: str) -> None:
    row = db.q1(con, "SELECT meta FROM instruments WHERE key=?", (key,))
    try:
        meta = json.loads(row["meta"]) if row and row["meta"] else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["first_seen_src"] = src
    con.execute("UPDATE instruments SET first_seen_d=?, meta=? WHERE key=?",
                (d, json.dumps(meta, ensure_ascii=False), key))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="re-resolve rows that already carry a date")
    ap.add_argument("--codes", default="",
                    help="comma-separated keys or futu codes to narrow to")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and report, write nothing")
    args = ap.parse_args()

    con = db.init()
    only = [c.strip() for c in args.codes.split(",") if c.strip()]
    rows = _targets(con, refresh=args.refresh, only=only)
    total = db.q1(con, "SELECT COUNT(*) n FROM instruments")["n"]
    undated = db.q1(con, "SELECT COUNT(*) n FROM instruments "
                         "WHERE first_seen_d IS NULL OR first_seen_d=''")["n"]
    print(f"货架 {total} 个标的，其中 {undated} 个未定日期；"
          f"本次可解析 {len(rows)} 个（有 futu_code）")
    if not rows:
        print("没有需要解析的行。")
        return 0

    codes = [r["futu_code"] for r in rows]
    print("① 向 OpenD 取上架日期 …")
    listed, listed_fail = futu_px.listing_dates(codes)
    print(f"   厂商给出真实日期 {len(listed)} 个"
          f"（其余为 US ETF，OpenD 只返回 1970 哨兵值）")

    need_bar = [c for c in codes if c not in listed]
    bars: dict[str, str] = {}
    bar_fail: dict[str, str] = {}
    if need_bar:
        print(f"② 对其余 {len(need_bar)} 个求最早可得日线 …")
        bars, bar_fail = futu_px.earliest_bar(need_bar)
        print(f"   取到 {len(bars)} 个")

    resolved = 0
    unresolved: list[tuple[str, str]] = []
    for r in rows:
        code = r["futu_code"]
        if code in listed:
            d, src = listed[code], "futu:listing_date"
        elif code in bars:
            d, src = bars[code], "futu:earliest_bar"
        else:
            unresolved.append(
                (r["key"], bar_fail.get(code) or listed_fail.get(code) or "无数据"))
            continue
        if not args.dry_run:
            _write(con, r["key"], d, src)
        resolved += 1
    if not args.dry_run:
        con.commit()

    left = (undated - resolved if args.dry_run else
            db.q1(con, "SELECT COUNT(*) n FROM instruments "
                       "WHERE first_seen_d IS NULL OR first_seen_d=''")["n"])
    print(f"\n{'（试运行，未写入）可定日期' if args.dry_run else '已写入'} "
          f"{resolved} 个；未定日期 {undated} → {left}"
          f"{'（预计）' if args.dry_run else ''}")
    if unresolved:
        print(f"未能解析 {len(unresolved)} 个：")
        for key, why in unresolved[:20]:
            print(f"  {key:<14} {why[:90]}")
    if left:
        print(f"仍未定日期的 {left} 个多为基金 / 结构化产品（无 futu_code），"
              f"由 Olive 货架快照定日期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
