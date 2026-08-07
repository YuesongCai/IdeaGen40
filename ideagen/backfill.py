"""Backfill a daily history: one report and one 40-idea batch per trading day.

The forward system produces one batch a day going forward. This walks the same
cycle backwards over a date range so the study has a continuous daily record
instead of two isolated batches.

The ordering is what keeps it honest. For each day D, in ascending order:

  1. ingest the corpus published in the 3-day window ending on D
  2. score the themes from that corpus only
  3. build the briefing pack, whose prices are clamped to sessions closed by D
  4. generate 40 ideas from that pack
  5. validate and place orders
  6. advance both books through D

Nothing at step N can see data from step N+1: `briefing` clamps every quote to
`complete_through()` evaluated *as if* it were D, and the fill engine only touches
bars strictly after the batch timestamp. The batch's `generated_at` is set to D's
morning in HKT, which is the same stamp a live run that day would have carried.

A backfilled batch is tagged `rules:v0.4`, never confused with a Claude-authored
one. Its ideas are real decisions against real as-of data; only the prose is
templated.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from . import (briefing, config, db, generator, ideas as ideas_mod, monitor,
               paper, scoring, universe)
from .sources import futu_px, wisburg


def trading_days(con, start: date, end: date, ref: str = "US.SPY") -> list[str]:
    return [r["d"] for r in db.q(
        con, "SELECT d FROM prices WHERE code=? AND d>=? AND d<=? ORDER BY d",
        (ref, start.isoformat(), end.isoformat()))]


def run(con, start: date, end: date, ingest: bool = True,
        fetch_bodies: int = 4, verbose: bool = True) -> dict:
    universe.sync_registry(con)
    days = trading_days(con, start, end)
    if not days:
        raise RuntimeError(f"no trading sessions between {start} and {end}; "
                           f"run `ideagen prices` first")

    rep: dict[str, Any] = {"from": days[0], "to": days[-1], "days": [],
                           "batches": 0, "failed": []}
    if verbose:
        print(f"backfill {days[0]} → {days[-1]}  ({len(days)} 个交易日)")

    for d in days:
        as_of = date.fromisoformat(d)
        row: dict[str, Any] = {"d": d}
        if verbose:
            print(f"\n── {d} " + "─" * 46)

        try:
            if ingest:
                ing = wisburg.ingest(con, as_of, lookback_days=config.OBSERVATION_WINDOW_DAYS,
                                     fetch_bodies=fetch_bodies, verbose=False)
                row["corpus"] = ing["total"]
                if verbose:
                    print(f"    corpus {ing['total']:,} 条（新增 {ing['new']}）"
                          f"  assets +{ing.get('assets', 0)}")

            sc = scoring.score_day(con, as_of, verbose=False, force=True)
            row["themes"] = len(sc.get("themes", []))
            if sc.get("themes"):
                t0 = sc["themes"][0]
                if verbose:
                    print(f"    打分 {row['themes']} 个主题，最高 "
                          f"{t0['label']} TIS {t0['tis']:.1f} "
                          f"(M {t0['m']} C {t0['c']})")

            # The moment a live run that day would have executed at. Every price
            # in the pack is clamped to sessions closed by then, so the backfill
            # cannot see its own future.
            gen_at = f"{d}T07:23:00+08:00"
            px_at = datetime.fromisoformat(gen_at)
            pack = briefing.build(con, as_of, verbose=False, price_asof=px_at)
            row["quotes"] = len(pack["quotes"])
            row["px_cut"] = pack["as_of_prices"]

            payload = generator.generate(con, as_of, verbose=verbose,
                                         price_asof=px_at, rebuild_pack=True)
            bid = f"B{d.replace('-', '')}"
            _, rows, val = ideas_mod.build_batch(
                con, payload, as_of, generator=generator.GENERATOR,
                batch_id=bid, generated_at=gen_at)
            row["batch"] = bid
            row["validation"] = {"pass": val["pass"], "errors": val["n_errors"],
                                 "warnings": val["n_warnings"]}
            if verbose:
                s = val["summary"]
                print(f"    批次 {bid}  校验 {'通过' if val['pass'] else '未通过'}"
                      f"（{val['n_errors']}E/{val['n_warnings']}W）"
                      f"  评级 {s['grades']}  期限 {s['horizons']}")
            if not val["pass"]:
                rep["failed"].append({"d": d, "why": "validation",
                                      "checks": [c["check"] for c in val["checks"]
                                                 if not c["ok"] and c["severity"] == "error"]})
                row["traded"] = False
            else:
                for b in config.BOOKS:
                    paper.open_batch(con, bid, b, verbose=False)
                rep["batches"] += 1
                row["traded"] = True

            # advance the books through this day only
            for b in config.BOOKS:
                paper.step(con, b, d, verbose=False)
            eq = {b: db.q1(con, "SELECT equity,cum_ret,gross,n_open FROM equity "
                                "WHERE book_id=? AND d=?", (b, d))
                  for b in config.BOOKS}
            row["books"] = {b: (dict(v) if v else None) for b, v in eq.items()}
            if verbose:
                for b, v in eq.items():
                    if v:
                        print(f"    {b:<12} ${v['equity']:>12,.0f} "
                              f"{v['cum_ret']*100:+.2f}%  敞口 {v['gross']*100:.0f}%"
                              f"  在场 {v['n_open']}")
            monitor.run(con, d, verbose=False)

        except Exception as e:  # noqa: BLE001 - one bad day must not lose the rest
            row["error"] = f"{type(e).__name__}: {e}"
            rep["failed"].append({"d": d, "why": row["error"]})
            if verbose:
                print(f"    ! {row['error']}")
        rep["days"].append(row)

    # final full mark-forward so every book is current
    last = futu_px.complete_through("US")
    for b in config.BOOKS:
        paper.run(con, b, days[0], last, verbose=False)

    if verbose:
        print(f"\nbackfill 完成：{rep['batches']}/{len(days)} 天有批次，"
              f"失败 {len(rep['failed'])}")
        for f in rep["failed"]:
            print(f"  ! {f['d']}: {f['why']}")
    db.kv_set(con, f"backfill:{days[0]}..{days[-1]}", rep)
    return rep
