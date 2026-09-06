"""Synthetic market + weekly runs for the performance-layer tests.

Everything the paper engine needs, built in memory: a weekday calendar with a
SPY series (the engine's session clock), three registry ETFs whose paths are
engineered so that a stop, a take-profit and a horizon exit all actually fire,
two weekly runs with candidates and selector verdicts, and a helper that books
those verdicts exactly the way `booking.book_run` does — through
`ideas.build_batch` and `paper.open_batch`, never by writing positions directly.
Data produced by the real code path is the only data worth testing the view on.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import booking, config, db, ideas as ideas_mod, paper  # noqa: E402

CAL_START, CAL_END = date(2026, 4, 1), date(2026, 9, 30)
#: Two Wednesdays, a week apart, like the live schedule.
P1, P2 = "2026-07-01", "2026-07-08"
RUN1, RUN2 = "run-live-0701", "run-backfill-0708"

#: GLD: ±0.8% saw-tooth (σ_1m ≈ 3.7% → stop ≈ −7%), then −3%/day from P1 → stop.
#: TLT: same noise, then +2%/day from P2 → take (σ×3 ≈ +11%).
#: XLE: same noise throughout → horizon exit after one month.
#: SPY: drifts +0.05%/day, the benchmark and the session clock.
PATHS = {"GLD": 200.0, "TLT": 90.0, "XLE": 80.0, "SPY": 500.0}


def calendar() -> list[str]:
    out, d = [], CAL_START
    while d <= CAL_END:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def prices(con) -> None:
    cal = calendar()
    rows = []
    for key, start in PATHS.items():
        px = start
        for i, d in enumerate(cal):
            if key == "SPY":
                px *= 1.0005
            else:
                px *= 1.008 if i % 2 == 0 else (1 / 1.008)
                if key == "GLD" and d > P1 and d <= "2026-07-15":
                    px *= 0.97
                if key == "TLT" and d > P2 and d <= "2026-07-31":
                    px *= 1.02
            rows.append({"code": f"US.{key}", "d": d, "open": px * 0.999,
                         "high": px * 1.004, "low": px * 0.996, "close": px,
                         "volume": 1e6, "src": "test"})
    db.upsert_many(con, "prices", rows, ["code", "d"])


def _cand(inst: str, topics: list[str], methods: list[str], topic_id: str | None = None):
    return {"id": f"pool:{inst}", "instrument_id": inst, "instrument_name": inst,
            "vehicle": "ETF", "topic_id": topic_id or topics[0], "method": "merged",
            "horizon_days": 30, "thesis": f"{inst} thesis", "upside_pct": 4.0,
            "downside_pct": -3.0, "p_up": 0.45, "p_base": 0.37, "p_down": 0.18,
            "proposed_by": methods, "n_proposals": len(methods), "topics": topics,
            "citations": []}


#: Period 1 pool: GLD is a two-topic, two-method candidate — the attribution
#: rule needs at least one of those to be testable.
POOL1 = [_cand("GLD", ["INFLATION", "GEOPOLITICS"], ["ai_native", "chain"]),
         _cand("XLE", ["ENERGY-SUPPLY"], ["carl_constraint"])]
POOL2 = [_cand("TLT", ["POLICY-PATH"], ["ai_native"]),
         _cand("XLE", ["ENERGY-SUPPLY"], ["gap"])]

#: Selector verdicts per run. `alpha` is a made-up arm (the view must not need
#: the registry to describe it); `buy_all` takes the whole pool.
VERDICTS = {
    RUN1: {"alpha": ["pool:GLD"], "buy_all": ["pool:GLD", "pool:XLE"]},
    RUN2: {"alpha": ["pool:TLT"], "buy_all": ["pool:TLT", "pool:XLE"]},
}
POOLS = {RUN1: POOL1, RUN2: POOL2}
AS_OF = {RUN1: P1, RUN2: P2}
CLASS = {RUN1: None, RUN2: "backfill"}


def runs(con) -> None:
    """Two successful weekly runs: one live, one backfill, with pools and verdicts."""
    for rid in (RUN1, RUN2):
        as_of = AS_OF[rid]
        db.upsert(con, "orch_runs", {
            "run_id": rid, "as_of": as_of, "kind": "weekly", "platform": "local",
            "started_at": f"{as_of}T00:00:00+00:00", "ended_at": f"{as_of}T00:10:00+00:00",
            "ok": 1, "error": None, "inputs_sha": "x", "journal_uri": None, "calls": 0,
            "data_classification": CLASS[rid]}, ["run_id"])
        for c in POOLS[rid]:
            db.upsert(con, "candidates", {
                "run_id": rid, "candidate_id": c["id"], "as_of": as_of,
                "instrument_id": c["instrument_id"],
                "direction": "up", "upside_pct": c["upside_pct"],
                "downside_pct": c["downside_pct"], "p_up": c["p_up"],
                "p_base": c["p_base"], "p_down": c["p_down"], "sigma_1m": None,
                "payload": json.dumps(c), "topic_id": c["topic_id"],
                "method": c["method"]}, ["run_id", "candidate_id"])
        for arm, chosen in VERDICTS[rid].items():
            db.upsert(con, "verdicts", {
                "run_id": rid, "as_of": as_of, "kind": "idea_selector",
                "strategy": arm, "version": "1.0", "role": "control" if arm == "buy_all" else "primary",
                "inputs_sha": "x", "chosen": json.dumps(chosen), "scores": None,
                "rejected": None, "meta": None, "calls": 0},
                ["run_id", "kind", "strategy"])


def book(con, *, generated_at: dict[str, str], through: str,
         arms: tuple[str, ...] = ("alpha", "buy_all")) -> None:
    """Book the verdicts into `sel-` books the way `booking.book_run` does.

    `generated_at` per run decides the first fillable bar: the live run is
    stamped the morning of its period, the backfill run weeks later — which is
    exactly the shape the isolation has to separate.

    Marking is interleaved the way the live account was marked: each run's
    books are advanced only to the day before the *next* run was generated,
    then the next batch is opened and marking resumes. `_accrue_cash` is
    idempotent per (book, day), so marking straight to the end first would
    freeze the interest on a cash balance the backfill buys never touched — a
    fixture that hides exactly the effect the live/backfill split has to show.
    """
    order = (RUN1, RUN2)
    mark_through = {}
    for i, rid in enumerate(order):
        nxt = order[i + 1] if i + 1 < len(order) else None
        mark_through[rid] = ((date.fromisoformat(generated_at[nxt][:10])
                              - timedelta(days=1)).isoformat() if nxt else through)
    for rid in order:
        as_of = date.fromisoformat(AS_OF[rid])
        cands = {c["id"]: c for c in POOLS[rid]}
        for arm in arms:
            chosen = [cands[c] for c in VERDICTS[rid].get(arm, [])]
            if not chosen:
                continue
            batch_id = f"W{AS_OF[rid].replace('-', '')}-{arm}"
            _, _, val = ideas_mod.build_batch(
                con, booking.payload_from_candidates(chosen, run_id=rid), as_of,
                generator=f"weekly:{rid}", batch_id=batch_id,
                generated_at=generated_at[rid])
            assert val["pass"], val
            booking._fix_stops(con, batch_id)
            book_id = config.selector_book(arm)
            db.upsert(con, "books", {
                "book_id": book_id, "label": f"选取策略 · {arm}",
                "descr": config.SELECTOR_SPEC["desc"],
                "capital": config.SELECTOR_SPEC["capital"],
                "sizing": config.SELECTOR_SPEC["sizing"],
                "entry": config.SELECTOR_SPEC["entry"],
                "created_at": config.now_hkt().isoformat()}, ["book_id"])
            paper.open_batch(con, batch_id, book_id, verbose=False)
        for arm in arms:
            book_id = config.selector_book(arm)
            if db.q1(con, "SELECT 1 FROM books WHERE book_id=?", (book_id,)):
                paper.run(con, book_id, AS_OF[rid], mark_through[rid], verbose=False)


def fresh() -> "db.sqlite3.Connection":
    con = db.init(":memory:")
    prices(con)
    runs(con)
    return con
