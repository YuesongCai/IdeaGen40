"""Paired stage-C backtest over the real stored candidate pools.

This is the answer to "回测是真的吗": every period here is a pool the system
actually wrote on that date, every price is the real close, and the whole sweep
runs with `allow_model=False`, so it recomputes byte-for-byte. It replaces the
synthetic `bt-synth-*` replay for anything shown to a PM.

Two honesty rules are enforced rather than documented:

* Periods carry their own classification. `live` means the system called that
  week as it happened; `backfill` means the ideas were generated afterwards
  from documents frozen at that date — as-of clean on the input side, but the
  model's weights have seen the world after it, and no code can undo that. The
  summary states the split, and a window containing any backfill period says so
  in its own headline.
* `ai_native` is excluded: it needs a model, and a model call inside a replay
  would make the replay unrepeatable. Its absence is recorded, not hidden.

  python3 scripts/run_real_backtest.py [--horizon-days 30]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideagen import backtest, db, platform as plat, schema  # noqa: E402
from ideagen import strategy as strat  # noqa: E402
from ideagen.poc_workflow import _arm_positions, _curves  # noqa: E402

METHODOLOGY = "real-pool-asof-replay/v1"
CONTROL = "buy_all"


def _periods(con) -> list[tuple[date, str]]:
    """Every period with a stored pool, with how that pool came to exist."""
    rows = db.q(con, "SELECT DISTINCT as_of FROM candidates ORDER BY as_of")
    out = []
    for r in rows:
        as_of = date.fromisoformat(str(r["as_of"]))
        run = db.q1(con, "SELECT data_classification FROM orch_runs "
                         "WHERE as_of=? AND ok=1 ORDER BY ended_at DESC LIMIT 1",
                    (r["as_of"],))
        out.append((as_of, str((dict(run) if run else {}).get(
            "data_classification") or "live")))
    return out


def _undated_shelf(con) -> int:
    """Instruments with no first-seen date.

    `eligible()` can only exclude an instrument from a past period if it knows
    when the instrument appeared. Undated rows are let through, so a backfilled
    period may pick something that was not yet on the shelf that week. The
    count is the honest measure of how much of the replay is not as-of clean.
    """
    try:
        row = db.q1(con, "SELECT COUNT(*) n FROM instruments "
                         "WHERE first_seen_d IS NULL OR first_seen_d=''")
        return int(dict(row)["n"]) if row else 0
    except Exception:  # noqa: BLE001 — a missing column must not kill the run
        return -1


def _arms() -> list[str]:
    return sorted(s["name"] for s in strat.available("idea_selector")
                  if not s.get("needs_model"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-days", type=int, default=30)
    args = ap.parse_args(argv)

    p = plat.load()
    con = db.init()
    schema.migrate(p.state)

    periods = _periods(con)
    if len(periods) < 2:
        raise SystemExit(
            f"只有 {len(periods)} 期有候选池，配对比较至少需要 2 期。"
            "先跑 scripts/backfill_weeks.py 补历史期。")
    days = [d for d, _ in periods]
    classes = {d.isoformat(): c for d, c in periods}
    arms = _arms()
    excluded = sorted(s["name"] for s in strat.available("idea_selector")
                      if s.get("needs_model"))

    print(f"期数 {len(days)}：{', '.join(d.isoformat() for d in days)}")
    print(f"分类：{json.dumps(classes, ensure_ascii=False)}")
    print(f"参赛挑法 {len(arms)}：{', '.join(arms)}")
    if excluded:
        print(f"排除（需要模型，放进复算式回测会让结果不可复现）：{', '.join(excluded)}")

    rep = backtest.sweep(
        con, days, stage="idea_selector", arms=arms, control=CONTROL,
        horizon_days=args.horizon_days, require_full_horizon=False,
        allow_model=False, strict=True)

    points = _curves(rep)
    positions = [row for arm in arms
                 for row in _arm_positions(con, days, arm, args.horizon_days)]

    n_backfill = sum(1 for c in classes.values() if c != "live")
    summary = {
        "data_classification": ("mixed-live-backfill" if n_backfill else "live"),
        "proof": "real_pools_real_prices_asof_replay",
        "predictive_claim": False,
        "periods": len(days),
        "dates": [d.isoformat() for d in days],
        "period_classification": classes,
        "n_live_periods": len(days) - n_backfill,
        "n_backfill_periods": n_backfill,
        "disclaimer": (
            "候选池与价格都是真实的，as-of 在文档层面严格钳制。"
            + (f"其中 {n_backfill} 期是事后补跑（backfill），有两处无法用代码消除的"
               "前视风险：①模型权重见过该日期之后的世界；②货架上有 "
               f"{_undated_shelf(con)} 个标的没有上架日期，按当期资格过滤时只能放行，"
               "所以补跑期的可选标的可能包含当时尚未上架的产品。"
               "结论性判断以 live 期为准。"
               if n_backfill else "")
            + f"未参与：{'、'.join(excluded)}（需要模型，会让复算不可重复）。"
        ),
        "undated_shelf_instruments": _undated_shelf(con),
        "horizon_days": rep.horizon_days,
        "model_calls": rep.calls,
        "excluded_arms": excluded,
        "arms": {name: {
            "n_chosen": a.n_chosen, "n_scored": a.n_scored,
            "coverage": a.coverage, "hit_rate": a.hit_rate,
            "mean_return_pct": (None if a.mean is None
                                else round(a.mean * 100.0, 4)),
            "median_return_pct": (None if a.median is None
                                  else round(a.median * 100.0, 4)),
            "window_complete_frac": a.window_complete_frac,
            "unknown": a.unknown,
        } for name, a in rep.arms.items()},
        "paired": {k: {kk: vv for kk, vv in vars(v).items()}
                   for k, v in rep.paired.items()},
    }

    inputs_sha = hashlib.sha256(json.dumps(
        {"dates": summary["dates"], "arms": arms, "control": CONTROL,
         "horizon_days": args.horizon_days, "methodology": METHODOLOGY},
        sort_keys=True).encode()).hexdigest()
    backtest_id = f"bt-real-{days[-1]:%Y%m%d}-{inputs_sha[:10]}"
    started = datetime.now(timezone.utc).isoformat()

    p.state.execute("DELETE FROM backtest_points WHERE backtest_id=?",
                    (backtest_id,))
    p.state.execute("DELETE FROM backtest_positions WHERE backtest_id=?",
                    (backtest_id,))
    with p.state.tx():
        schema.upsert(p.state, "backtest_runs", {
            "backtest_id": backtest_id,
            "as_of": days[-1].isoformat(),
            "window_start": days[0].isoformat(),
            "window_end": days[-1].isoformat(),
            "methodology": METHODOLOGY,
            "data_classification": summary["data_classification"],
            "model_id": None, "model_release_date": None,
            "knowledge_cutoff": None,
            "inputs_sha": inputs_sha, "artifact_uri": None,
            "started_at": started,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "ok": 1, "error": None,
            "summary": json.dumps(summary, ensure_ascii=False,
                                  separators=(",", ":"), allow_nan=False),
        })
        for row in points:
            schema.upsert(p.state, "backtest_points",
                          {"backtest_id": backtest_id, **row})
        for row in positions:
            schema.upsert(p.state, "backtest_positions",
                          {"backtest_id": backtest_id, **row})

    print(f"\n回测已落库 {backtest_id}")
    print(f"  {len(points)} 个净值点 · {len(positions)} 条持仓 · "
          f"{rep.calls} 次模型调用（必须是 0）")
    for name, a in sorted(rep.arms.items(),
                          key=lambda kv: -(kv[1].mean or -9)):
        hit = "—" if a.hit_rate is None else f"{a.hit_rate * 100:.0f}%"
        mean = "—" if a.mean is None else f"{a.mean * 100:+.2f}%"
        print(f"  {name:<26} 胜率 {hit:>5}  均值 {mean:>8}  "
              f"选中 {a.n_chosen} 已计价 {a.n_scored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
