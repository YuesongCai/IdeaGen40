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
import math
import bisect
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideagen import backtest, db, platform as plat, schema  # noqa: E402
from ideagen import config  # noqa: E402
from ideagen import strategy as strat  # noqa: E402
from ideagen.poc_workflow import _arm_positions  # noqa: E402

METHODOLOGY = "real-pool-asof-replay/v1"
CONTROL = "buy_all"


#: The index a PM compares against. Named once so the ranking table and the
#: curve cannot drift onto different benchmarks.
BENCHMARK = config.BENCHMARKS["SPY"]

#: Live periods before the live column is worth reading at all. Four is one
#: full rotation of the four-week tranche cycle — below it every position in
#: the column is still open somewhere, and the number moves with one market.
LIVE_PERIODS_BEFORE_READING = 4


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
    period may pick something that was not yet on the shelf that week.
    """
    try:
        row = db.q1(con, "SELECT COUNT(*) n FROM instruments "
                         "WHERE first_seen_d IS NULL OR first_seen_d=''")
        return int(dict(row)["n"]) if row else 0
    except Exception:  # noqa: BLE001 — a missing column must not kill the run
        return -1


def _shelf_dating(con, held: set[str], window_start: str) -> dict:
    """How much of this replay is actually as-of clean, on the rows it held.

    Counting undated rows across the whole shelf answers a question nobody
    asked: most of that shelf is Olive funds the replay never touches. What
    decides whether period P could have picked an instrument that did not exist
    is the dating of the instruments period P actually held, and whether any of
    them appeared after the window opened. Both counts are reported, because the
    narrow one is the live exposure and the broad one is the remaining debt.
    """
    out = {"shelf_total": 0, "shelf_dated": 0, "shelf_undated": _undated_shelf(con),
           "held_total": len(held), "held_dated": 0,
           "held_latest_first_seen": None, "held_after_window_start": 0}
    try:
        row = db.q1(con, "SELECT COUNT(*) n, "
                         "SUM(CASE WHEN first_seen_d IS NOT NULL AND first_seen_d<>'' "
                         "THEN 1 ELSE 0 END) d FROM instruments")
        if row:
            out["shelf_total"] = int(dict(row)["n"] or 0)
            out["shelf_dated"] = int(dict(row)["d"] or 0)
        for key in held:
            r = db.q1(con, "SELECT first_seen_d FROM instruments WHERE key=?", (key,))
            d = (dict(r).get("first_seen_d") or "").strip() if r else ""
            if not d:
                continue
            out["held_dated"] += 1
            if out["held_latest_first_seen"] is None or d > out["held_latest_first_seen"]:
                out["held_latest_first_seen"] = d
            if d > window_start:
                out["held_after_window_start"] += 1
    except Exception:  # noqa: BLE001 — a missing column must not kill the run
        pass
    return out


#: Two-sided α=0.05 critical values by degrees of freedom. At n=7 the normal
#: value understates the interval by a quarter, and n=7 is exactly where these
#: arms land after the exclusion — using z there would manufacture precision
#: out of the smallest samples in the table.
_T_CRIT = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
           7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
           13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
           19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
           25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def _crit(df: int) -> float:
    return _T_CRIT.get(df, backtest.Z_ALPHA)


def _mde_pct(rets: list[float]) -> float | None:
    """The smallest mean this sample could have separated from zero.

    (z_a + z_b)·sd/sqrt(n) at the same α=0.05 / power=0.80 the paired test uses,
    so "too few" is derived here rather than picked. A flat count would have been
    another threshold sitting next to the project's own, which is the two-yardsticks
    problem this codebase keeps having to fix.

    It is a *lower bound* on how blunt the check is, and deliberately so: positions
    inside an arm share periods, holding windows and names, so the independent
    sample is smaller than the count and the true detectable effect is larger.
    `backtest.required_pairs` discounts exactly that for the paired test; doing it
    properly here needs the same overlap accounting.

    Because it is a lower bound, it can only ever refuse. A move smaller than it
    is certainly invisible to this sample; a move larger than it is *not thereby
    established*, because the real bound is higher by an unknown amount. That
    asymmetry is why nothing here returns a verdict that asserts a shift.
    """
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    sd = (sum((x - mean) ** 2 for x in rets) / (n - 1)) ** 0.5
    if sd <= 0:
        return None
    return (_crit(n - 1) + backtest.Z_POWER) * sd / (n ** 0.5)


#: The pair built to answer "which generation method is worth more". Both take
#: everything their method proposed and rank nothing, so their difference is the
#: method and only the method.
SOURCE_ARMS = ("generated_ai_native", "generated_carl_constraint")


def _generation_head_to_head(con, days: list, horizon_days: int) -> dict:
    """The two source arms, split by the ideas they did not share.

    They are not disjoint, by design and with the reason written down:
    `_proposed_by` returns every method that argued for an instrument, so an idea
    both generators reached is admitted by both books — "an idea is no less that
    generator's for having been agreed with". That is the right construction for
    the question those books ask, which is what a portfolio believing one
    generator would have held.

    It is the wrong construction for the question the panel puts to them, which
    is which generation method is worth more. Roughly four fifths of each arm's
    ideas are also in the other, identical on both sides, contributing nothing to
    the difference while dominating both averages. Whatever separates the methods
    lives in the fifth each holds alone.

    Keyed on the idea, not the instrument. Keying on the instrument collapses the
    several ideas that name one — the pool the backtest replays is stage-B output
    before the merge — and the first version of this function did exactly that,
    reporting an overlap that was an artefact of its own key.
    """
    a_name, b_name = SOURCE_ARMS
    shared: list[float] = []
    only_a: list[float] = []
    only_b: list[float] = []
    n_shared_ideas = n_a = n_b = 0

    for period in days:
        ctx = backtest.context_for(con, period, allow_model=False)
        try:
            pick_a = {str(x) for x in strat.run("idea_selector", a_name, ctx).chosen}
            pick_b = {str(x) for x in strat.run("idea_selector", b_name, ctx).chosen}
        except Exception:  # noqa: BLE001 — an arm absent this period is not fatal
            continue
        cands = backtest._candidates(con, period)
        outcomes = backtest.outcomes_for(
            con, cands, period, horizon_days=horizon_days,
            require_full_horizon=False)

        def ret(idea_id: str) -> float | None:
            o = outcomes.get(idea_id)
            return None if o is None or o.ret is None else o.ret * 100.0

        both = pick_a & pick_b
        n_shared_ideas += len(both)
        n_a += len(pick_a - pick_b)
        n_b += len(pick_b - pick_a)
        shared += [r for r in map(ret, both) if r is not None]
        only_a += [r for r in map(ret, pick_a - pick_b) if r is not None]
        only_b += [r for r in map(ret, pick_b - pick_a) if r is not None]

    if not (only_a or only_b):
        return {"pair": list(SOURCE_ARMS), "available": False,
                "note": "两个来源限定组合本期没有各自独有的已计价想法"}

    def block(values: list[float]) -> dict:
        mde = _mde_pct(values)
        return {"n": len(values),
                "mean_return_pct": (round(sum(values) / len(values), 4)
                                    if values else None),
                "mde_pct": None if mde is None else round(mde, 3)}

    a_block, b_block, shared_block = block(only_a), block(only_b), block(shared)
    gap = (None if a_block["mean_return_pct"] is None
           or b_block["mean_return_pct"] is None else
           round(a_block["mean_return_pct"] - b_block["mean_return_pct"], 4))
    joint = (None if a_block["mde_pct"] is None or b_block["mde_pct"] is None
             else round((a_block["mde_pct"] ** 2
                         + b_block["mde_pct"] ** 2) ** 0.5, 3))
    total_a = n_shared_ideas + n_a
    overlap = round(n_shared_ideas / total_a, 3) if total_a else None
    return {
        "pair": list(SOURCE_ARMS), "available": True,
        "keyed_on": "idea_id",
        "shared": shared_block, "only_first": a_block, "only_second": b_block,
        "n_shared_ideas": n_shared_ideas,
        "overlap_frac": overlap,
        "gap_on_unique_pct": gap,
        "mde_gap_pct": joint,
        "verdict": ("underpowered" if joint is None or gap is None
                    or abs(gap) < joint else "not_ruled_out"),
        "verdict_applies_to": "gap_on_unique_pct",
        "note": (
            f"两个来源限定组合共享 {overlap * 100:.0f}% 的想法——被两种方法共同提出的"
            "想法按设计同时进入两个组合。那部分在两边完全相同，对它们的差贡献为零，"
            f"却占了各自大部分样本。方法本身的差异只体现在各自独有的 "
            f"{a_block['n']} 与 {b_block['n']} 笔上，判定按这两组给出。"
            "头条的 n 看着很大，这个问题的有效样本远小于它。"
            if overlap is not None else ""),
    }


#: 主题来源的三种取值，面板图例里本来就叫这三个中文名。摘要 note 是印给读者的，
#: 印 seed / discovered / mixed 等于让人自己去猜后端枚举。
REGIME_CN = {"seed": "人工词典", "discovered": "自己发现",
             "mixed": "混合", "none": "无"}


def _disclaimer(*, n_backfill: int, asof_note: str, horizon: dict,
                horizon_days: int, excluded: list[str],
                provenance: dict | None = None) -> str:
    """The caveats, numbered from the list rather than by hand.

    The count used to be a literal. It said "两项" and listed three, because the
    holding-period note was appended later and the number in front of it was not
    connected to anything — a count that does not come from the list it counts
    drifts the first time someone adds to the list, and it drifted here.

    The holding-period caveat also sat under the look-ahead heading, which is
    backwards: look-ahead is having used information from after the day, and a
    truncated window is not having enough of it. Filed separately, and the
    difference stated, because a reader who accepts "前视风险三项" has been told
    something false about what went wrong.
    """
    parts = ["候选池与价格均为真实数据，as-of 在文档层面严格钳制。"]
    if n_backfill:
        risks = ["模型权重已见过该日期之后的信息，无法用代码消除", asof_note]
        marks = "①②③④⑤"
        parts.append(f"其中 {n_backfill} 期是事后补跑（backfill）。前视风险 "
                     f"{len(risks)} 项：" + "；".join(
                         f"{marks[i]}{r.rstrip('；。')}" for i, r in enumerate(risks))
                     + "。")
        fracs = [v["complete_frac"] or 0 for v in horizon["arms"].values()] or [0]
        parts.append(
            "另有一项口径限制，与前视无关——前视是用了未来的信息，而这一项是未来"
            f"还不够：持有期表中标注 {horizon_days} 天，但只有 "
            f"{(horizon['complete_frac'] or 0) * 100:.0f}% 的持仓跑满该窗口"
            f"（各组合 {min(fracs) * 100:.0f}–{max(fracs) * 100:.0f}%，并不一致），"
            "未满窗口的收益与满窗口的混在同一列；只用跑满部分重算的结果见"
            "「持有期完整度」（horizon_completeness）。")
        parts.append("结论性判断以实跑的那几期为准。")
    regimes = (provenance or {}).get("periods_by_regime") or {}
    if len({r for r in regimes if r != "none"}) > 1:
        shown = "、".join(f"{REGIME_CN.get(k, k)} {v} 期"
                         for k, v in sorted(regimes.items()))
        parts.append(
            f"主题来源并不同质（{shown}）：人工词典（seed）那几期打的是 2026-07 "
            f"人工撰写的主题词典，自己发现（discovered）那几期的主题是该期自己从"
            f"当周研报里发现并命名的。后者没有任何人工事后选题，前者有；"
            f"两类不要合成一个胜率读，分列见「排序力」表里的主题来源标注"
            f"（theme_provenance）。")
    if excluded:
        parts.append(f"未参与：{'、'.join(excluded)}（需调用模型，会使复算不可重复）。")
    return "".join(parts)


def _live_vs_backfill(positions: list[dict], classes: dict[str, str]) -> dict:
    """Each arm's record split by whether the period was called live.

    The pooled hit rate is the number everyone quotes and it is the wrong one
    for anything registered `exploratory`. `ev_rank` was chosen on 2026-09-05
    after looking at these six periods; a figure spanning them measures the
    search that found the rule, not the rule. Its live periods are the only
    ones that will ever test it, and until this block existed nothing computed
    them — so the contaminated number was the only number available, and would
    have kept being cited as live periods accumulated around it.

    Reported with the count in front of the rate, and with `usable` false until
    the live side reaches a size worth reading. One live period is eight
    positions inside one month of one market; a hit rate over it is a fact
    about that month.
    """
    by: dict[str, dict[str, list[float]]] = {}
    for r in positions:
        ret = r.get("return_pct")
        if ret is None:
            continue
        cls = "live" if classes.get(str(r.get("period"))) == "live" else "backfill"
        by.setdefault(str(r["arm"]), {"live": [], "backfill": []})[cls].append(
            float(ret))

    def stat(v: list[float]) -> dict:
        if not v:
            return {"n": 0, "hit_rate": None, "mean_return_pct": None}
        return {"n": len(v),
                "hit_rate": round(sum(1 for x in v if x > 0) / len(v), 4),
                "mean_return_pct": round(sum(v) / len(v), 4)}

    n_live_periods = sum(1 for c in classes.values() if c == "live")
    arms = {}
    for name, sides in sorted(by.items()):
        role = next((r.get("role") for r in strat.available("idea_selector")
                     if r["name"] == name), None)
        arms[name] = {
            "role": role,
            "live": stat(sides["live"]),
            "backfill": stat(sides["backfill"]),
            # An exploratory arm's pooled figure spans the search that chose
            # it. Saying which column is evidence is the point of the split.
            "evidence_column": ("live" if role == "exploratory" else "pooled"),
        }
    return {
        "n_live_periods": n_live_periods,
        "n_backfill_periods": len(classes) - n_live_periods,
        "live_periods": sorted(d for d, c in classes.items() if c == "live"),
        "usable": n_live_periods >= LIVE_PERIODS_BEFORE_READING,
        "periods_needed": max(0, LIVE_PERIODS_BEFORE_READING - n_live_periods),
        "arms": arms,
        "note": (
            f"按期次是否为当期实跑拆开。{LIVE_PERIODS_BEFORE_READING} 期以下的实跑"
            f"列不作数——一期就是一个月一个市场里的几条持仓，它的胜率是关于那个月的"
            f"事实。对按探索类（exploratory）注册的组合（如 ev_rank），合并列跨越了挑出这条规则时"
            f"看过的期次，只有实跑列才是检验；对照组与原有的组合，合并列本身就是"
            f"它们的记录。"),
    }


def _benchmark_series(con, points: list[dict]) -> tuple[float | None, float | None]:
    """Benchmark return and realised volatility over exactly the curve's span.

    The volatility is what makes the comparison honest. A return difference
    against an index says nothing until both sides are divided by the risk that
    produced them, and these arms do not run at the index's risk — the top
    expectation bucket holds copper miners and oil against the bottom's T-bills.
    """
    if not points:
        return None, None
    ds = [p["d"] for p in points]
    a, b = min(ds), max(ds)
    rows = db.q(con, "SELECT d, close c FROM prices WHERE code=? AND d>=? AND d<=? "
                     "ORDER BY d", (BENCHMARK, a, b))
    if len(rows) < 3:
        return None, None
    px = [r["c"] for r in rows]
    return (px[-1] / px[0] - 1) * 100, backtest.realized_vol_pct(px)


def _ev_bucket_of(con, days: list):
    """Map a backtest position to the expectation quintile of its own period."""
    import bisect as _b
    cuts: dict[str, list[float]] = {}
    ev_by: dict[tuple[str, str], float] = {}
    for d in days:
        vals = []
        for r in db.q(con, "SELECT instrument_id, upside_pct, downside_pct, p_up, "
                           "p_base, p_down FROM candidates WHERE as_of=?",
                      (d.isoformat(),)):
            ps = [r["p_up"], r["p_base"], r["p_down"]]
            rs = [r["upside_pct"], 0.0, r["downside_pct"]]
            if any(v is None for v in ps + rs) or sum(ps) <= 0:
                continue
            e = sum(p / sum(ps) * v for p, v in zip(ps, rs))
            ev_by[(d.isoformat(), str(r["instrument_id"]))] = e
            vals.append(e)
        vals.sort()
        if len(vals) >= 15:
            cuts[d.isoformat()] = [vals[int(len(vals) * k / 5)] for k in range(1, 5)]

    def bucket(row: dict):
        per = str(row.get("period"))
        e = ev_by.get((per, str(row.get("instrument_id"))))
        c = cuts.get(per)
        if e is None or not c:
            return None
        return f"Q{_b.bisect_left(c, e) + 1}"
    return bucket


def _exposure(points: list[dict], gap_days: float, horizon_days: int,
              bench_pct: float | None, bench_vol: float | None = None) -> dict:
    """How much of the book was at risk, and what the curve means given that.

    A tranche portfolio ramps: the first period commits one slot of four, so the
    book runs a quarter invested for a week and cannot be read against a
    fully-invested index over that stretch. Comparing the curve to the benchmark
    without saying so understates every arm by the cost of its own ramp.

    `beta_equivalent_pct` is the return the same average exposure would have
    produced holding the benchmark, and `excess_over_exposure_pct` is what each
    arm did relative to that. It assumes a beta of one against SPY, which a book
    of sector, commodity and currency ETFs does not have — so it is a coarse
    adjustment, stated as one, and the right way to read it is as a rank rather
    than a number.
    """
    if not points:
        return {}
    by_arm: dict[str, list[dict]] = {}
    for p in points:
        by_arm.setdefault(p["arm"], []).append(p)
    any_arm = next(iter(by_arm.values()))
    fracs = [p.get("invested_frac") for p in any_arm[1:]
             if p.get("invested_frac") is not None]
    mean_inv = (sum(fracs) / len(fracs)) if fracs else None
    beta_eq = (None if (mean_inv is None or bench_pct is None)
               else mean_inv * bench_pct)
    bench_ratio = (None if (bench_vol is None or not bench_vol or bench_pct is None)
                   else bench_pct / bench_vol)
    arms = {}
    for name, rows in sorted(by_arm.items()):
        ret = rows[-1]["equity"] - 100.0
        vol = backtest.realized_vol_pct([r["equity"] for r in rows])
        arms[name] = {
            "nav_end": round(rows[-1]["equity"], 4),
            "return_pct": round(ret, 4),
            "realized_vol_pct": None if vol is None else round(vol, 4),
            "return_per_vol": (None if not vol else round(ret / vol, 4)),
            "return_per_vol_vs_benchmark": (
                None if not (vol and bench_ratio is not None)
                else round(ret / vol - bench_ratio, 4)),
            "excess_over_benchmark_pct": (None if bench_pct is None
                                          else round(ret - bench_pct, 4)),
            "excess_over_exposure_pct": (None if beta_eq is None
                                         else round(ret - beta_eq, 4)),
            "max_drawdown_pct": round(min(r["drawdown"] for r in rows), 4)}
    no_series = any_arm[0].get("no_series") or []
    return {
        "slots": max(1, round(horizon_days / max(gap_days, 1e-9))),
        "gap_days": gap_days, "horizon_days": horizon_days,
        "days": len(any_arm) - 1,
        "mean_invested_frac": None if mean_inv is None else round(mean_inv, 4),
        "ramp": [{"d": p["d"], "invested_frac": p.get("invested_frac")}
                 for p in any_arm[1:]],
        "benchmark": BENCHMARK,
        "benchmark_pct": None if bench_pct is None else round(bench_pct, 4),
        "benchmark_vol_pct": None if bench_vol is None else round(bench_vol, 4),
        "benchmark_return_per_vol": (None if bench_ratio is None
                                     else round(bench_ratio, 4)),
        "beta_equivalent_pct": None if beta_eq is None else round(beta_eq, 4),
        "arms": arms,
        "instruments_without_daily_series": no_series,
        "note": (
            "净值按分批滚动组合逐日计价：每期投入 1/slots 资本、等权、持有一个"
            "持有期，空出的档位是现金。窗口开头必然欠投——第一期只占一个档位，"
            "所以拿它和满仓指数直接比会低估每个组合一个建仓成本。"
            "beta_equivalent_pct 是同样平均敞口拿着基准会有的收益，"
            "excess_over_exposure_pct 是相对它的差。**该折算假设对 SPY 的 beta 为 1，"
            "而这些组合并不满足**——2026-09-05 实测 ev_rank 的顶格分位持仓入场前年化波动"
            "35%，SPY 约 11%，所以那一列偏乐观，不要单独引用。"
            "要判断有没有超额，看 return_per_vol_vs_benchmark：它拿每个组合自己实现的"
            "净值波动做分母，不假设任何 beta。"),
    }


def _ranking_power(con, days: list, horizon_days: int) -> dict:
    """Does the run's own expected return rank what actually happened?

    Every candidate states a three-point scenario, and its expectation is the one
    number that folds all six inputs together. For six periods nothing selected
    on it: the omega arms rank gains over losses against cash and are blind to
    the size of the upside, calibration ranks the honesty of the probabilities,
    spread and left-tail rank portfolio shape. `analytics.ranking_report` had
    measured Spearman 0.164 of expectation against realised return across 1561
    ideas and no arm acted on it.

    Quintiles are cut *within each period*, never pooled. Pooling would let a
    period with a complete 30-day window and a rising tape supply most of the
    top quintile, and the table would then be reporting the calendar. Cut inside
    the period, every quintile is scored over the same days by construction.

    Reported next to the same-window benchmark, because a quintile that ranks
    but never clears SPY has established that the ordering works and that the
    strategy still does not beat buying the index — two different findings that
    one number would merge.
    """
    inst = {r["key"]: r["futu_code"] for r in db.q(
        con, "SELECT key, futu_code FROM instruments WHERE futu_code IS NOT NULL")}
    last = (db.q1(con, "SELECT MAX(d) m FROM prices") or {"m": None})["m"]

    def fwd(code: str, start: str) -> float | None:
        e = db.q1(con, "SELECT d, close c FROM prices WHERE code=? AND d>=? "
                       "ORDER BY d LIMIT 1", (code, start))
        if not e:
            return None
        end = (date.fromisoformat(start) + timedelta(days=horizon_days)).isoformat()
        if last:
            end = min(end, last)
        x = db.q1(con, "SELECT d, close c FROM prices WHERE code=? AND d<=? "
                       "ORDER BY d DESC LIMIT 1", (code, end))
        if not x or x["d"] <= e["d"]:
            return None
        return (x["c"] / e["c"] - 1) * 100

    def ev_of(r) -> float | None:
        rs = [r["upside_pct"], 0.0, r["downside_pct"]]
        ps = [r["p_up"], r["p_base"], r["p_down"]]
        if any(v is None for v in rs + ps):
            return None
        tot = sum(ps)
        return sum(p / tot * v for p, v in zip(ps, rs)) if tot > 0 else None

    per_period, pooled = [], {}
    for d in days:
        rows = db.q(con, "SELECT instrument_id, upside_pct, downside_pct, p_up, "
                         "p_base, p_down FROM candidates WHERE as_of=?",
                    (d.isoformat(),))
        obs = []
        for r in rows:
            code, ev = inst.get(r["instrument_id"]), ev_of(r)
            if not code or ev is None:
                continue
            ret = fwd(code, d.isoformat())
            if ret is not None:
                obs.append((ev, ret))
        if len(obs) < 15:
            per_period.append({"as_of": d.isoformat(), "n": len(obs),
                               "skipped": "候选太少，切不出五分位"})
            continue
        obs.sort()
        evs = [o[0] for o in obs]
        cuts = [evs[int(len(evs) * k / 5)] for k in range(1, 5)]
        buckets: dict[str, list[float]] = {f"Q{k}": [] for k in range(1, 6)}
        for ev, ret in obs:
            buckets[f"Q{bisect.bisect_left(cuts, ev) + 1}"].append(ret)
        for k, v in buckets.items():
            pooled.setdefault(k, []).extend(v)
        bench = fwd(BENCHMARK, d.isoformat())
        q1 = buckets["Q1"]; q5 = buckets["Q5"]
        per_period.append({
            "as_of": d.isoformat(), "n": len(obs),
            "benchmark_pct": None if bench is None else round(bench, 4),
            "quintiles": {k: {"n": len(v),
                              "hit_rate": round(sum(1 for x in v if x > 0) / len(v), 4),
                              "mean_return_pct": round(sum(v) / len(v), 4)}
                          for k, v in buckets.items() if v},
            "top_minus_bottom_pct": (round(sum(q5) / len(q5) - sum(q1) / len(q1), 4)
                                     if q1 and q5 else None),
            "top_minus_benchmark_pct": (round(sum(q5) / len(q5) - bench, 4)
                                        if q5 and bench is not None else None)})

    scored = [p for p in per_period if "quintiles" in p]
    tb = [p["top_minus_bottom_pct"] for p in scored
          if p["top_minus_bottom_pct"] is not None]
    vb = [p["top_minus_benchmark_pct"] for p in scored
          if p["top_minus_benchmark_pct"] is not None]
    return {
        "score": "ev", "score_label": "候选自陈情景的概率加权期望回报",
        "ex_ante": "只用生成时写下的三点情景，与其后发生的事无关",
        "quintiles_cut": "per_period",
        "benchmark": BENCHMARK,
        "horizon_days": horizon_days,
        "per_period": per_period,
        "pooled": {k: {"n": len(v),
                       "hit_rate": round(sum(1 for x in v if x > 0) / len(v), 4),
                       "mean_return_pct": round(sum(v) / len(v), 4)}
                   for k, v in sorted(pooled.items()) if v},
        "periods_scored": len(scored),
        "periods_top_beats_bottom": sum(1 for x in tb if x > 0),
        "periods_top_beats_benchmark": sum(1 for x in vb if x > 0),
        "note": (
            "分位在每期内部切，不跨期合并——合并会让窗口完整、行情向上的那一期"
            "供出大半个顶格分位，那张表报的就成了日历。顶格分位与同期基准并列，"
            "因为「排序有效」和「跑赢指数」是两个结论，合成一个数会把它们混掉。"),
        "provenance_warning": (
            "这条排序是在看过这些期次的结果之后才被挑出来检验的（试过两种排法："
            "按评级（grade）和按期望值，评级那种排不出结果）。因此上表对这次搜索"
            "是样本内的，只够支持「值得往前跑一段看看」，不足以支持「已确认有效」"
            "——试的次数一多，总会有一种看起来赢了，这就是多重检验"
            "（multiple testing）。期望值排序按探索类注册，"
            "只有它当期实跑的那几期才算证据。"),
    }


def _theme_provenance(days: list) -> dict:
    """Where each period's themes came from: hand-authored, or found that week.

    Not a detail. The sixteen seed themes were written by a person in July 2026
    after looking at 2026 markets, and they carry `registered_d` 2026-07-26 —
    so a period replayed before that date sees none of them and has to discover
    and name its own from the corpus it had. That is the stronger evidence, and
    it is also a *different experiment* from the live period, which scored a
    dictionary a human had already chosen. Averaging the two into one hit rate
    would quietly answer "does the system find tradeable debates" with a number
    partly earned by "does a person". Reported per period so the two cohorts can
    be read apart, which is the first question anyone worried about hindsight
    will ask.
    """
    from ideagen import lexicon
    out, by_regime = {}, {}
    for d in days:
        visible = lexicon.all_themes(d)
        origins = Counter(getattr(t, "origin", "seed") for t in visible)
        regime = ("none" if not visible
                  else "discovered" if not origins.get("seed")
                  else "seed" if not origins.get("discovered") else "mixed")
        out[d.isoformat()] = {"n_themes": len(visible), "regime": regime,
                              "by_origin": dict(origins)}
        by_regime[regime] = by_regime.get(regime, 0) + 1
    return {"per_period": out, "periods_by_regime": by_regime,
            "seed_registered_d": lexicon.SEED_REGISTERED_D,
            "note": "seed = 人工撰写的主题词典（2026-07-26 注册）；"
                    "discovered = 该期自己从当周研报里发现并命名的主题"}


def _horizon_completeness(positions: list[dict], horizon_days: int) -> dict:
    """How much of a table labelled "30 天" actually ran 30 days.

    The sweep is called with `require_full_horizon=False`, and `backtest.py`
    says exactly what that costs: "a 9-session return reported as a one-month
    return is a different statistic wearing the same name… legitimate for a
    paired comparison, where both arms are truncated identically and the
    truncation cancels — but the label then has to say so".

    The label did not say so. Worse, the arms are *not* truncated identically:
    completeness runs from 17% to 24% between them, so the condition the setting
    relies on does not hold here. Both means are therefore reported — the whole
    sample, and the subset that reached the horizon — rather than one number
    under a heading it only partly earns.
    """
    from datetime import date as _date, timedelta as _td

    def reached(row: dict) -> bool | None:
        try:
            period = _date.fromisoformat(str(row["period"])[:10])
            exit_d = _date.fromisoformat(str(row["exit_d"])[:10])
        except (TypeError, ValueError):
            return None
        return exit_d >= period + _td(days=horizon_days)

    arms: dict[str, Any] = {}
    for row in positions:
        if row.get("return_pct") is None:
            continue
        done = reached(row)
        if done is None:
            continue
        entry = arms.setdefault(str(row["arm"]),
                                {"n": 0, "n_full": 0, "all": [], "full": []})
        entry["n"] += 1
        entry["all"].append(float(row["return_pct"]))
        if done:
            entry["n_full"] += 1
            entry["full"].append(float(row["return_pct"]))

    out: dict[str, Any] = {}
    for name, e in arms.items():
        mean_all = sum(e["all"]) / len(e["all"]) if e["all"] else None
        mean_full = sum(e["full"]) / len(e["full"]) if e["full"] else None
        mde_full = _mde_pct(e["full"])
        # The restatement disagrees with the headline sharply — omega_strict
        # goes from +1.85% to -3.33%, random_pick from +0.53% to +4.12%, nearly
        # inverting the order. It would be easy and wrong to present that as the
        # real ranking: the full-horizon subset is 17% of a sample that was
        # already small, eight positions for some arms. So each restatement
        # carries the bound its own sample supports, and the disagreement is
        # reported as the table being unable to adjudicate rather than as a
        # second answer.
        out[name] = {
            "n": e["n"], "n_full_horizon": e["n_full"],
            "complete_frac": round(e["n_full"] / e["n"], 3) if e["n"] else None,
            "mean_return_pct": None if mean_all is None else round(mean_all, 4),
            "mean_return_full_horizon_pct": (
                None if mean_full is None else round(mean_full, 4)),
            "mde_full_horizon_pct": (
                None if mde_full is None else round(mde_full, 3)),
            "full_horizon_verdict": (
                "underpowered" if mde_full is None or mean_full is None
                or abs(mean_full) < mde_full else "not_ruled_out"),
        }
    # A reader given nine restated means will rank them — I did it myself and
    # called the order "nearly inverted". Ranking is a comparison, and it had no
    # bound of its own, which is the same mistake three other places in this file
    # made tonight. So the comparison the panel actually makes, against the
    # control, is stated here with a bound covering both samples.
    control_full = arms.get(CONTROL, {}).get("full") or []
    mde_control = _mde_pct(control_full)
    mean_control = (sum(control_full) / len(control_full)
                    if control_full else None)
    for name, e in arms.items():
        entry = out[name]
        entry["verdict_applies_to"] = "mean_return_full_horizon_pct 对零"
        if name == CONTROL or mean_control is None or not e["full"]:
            continue
        own_mean = sum(e["full"]) / len(e["full"])
        mde_own = _mde_pct(e["full"])
        gap = round(own_mean - mean_control, 4)
        entry["vs_control_full_horizon_pct"] = gap
        if mde_own is None or mde_control is None:
            entry["mde_vs_control_pct"] = None
            entry["verdict_vs_control"] = "underpowered"
        else:
            joint = round((mde_own ** 2 + mde_control ** 2) ** 0.5, 3)
            entry["mde_vs_control_pct"] = joint
            entry["verdict_vs_control"] = (
                "not_ruled_out" if abs(gap) >= joint else "underpowered")

    fracs = [v["complete_frac"] for v in out.values()
             if v["complete_frac"] is not None]
    total = sum(v["n"] for v in out.values())
    total_full = sum(v["n_full_horizon"] for v in out.values())
    return {
        "horizon_days": horizon_days,
        "n_positions": total,
        "n_full_horizon": total_full,
        "complete_frac": round(total_full / total, 3) if total else None,
        "complete_frac_spread": (
            round(max(fracs) - min(fracs), 3) if fracs else None),
        "arms": out,
        "note": (
            f"表头写的是 {horizon_days} 天持有期，但只有 "
            f"{(total_full / total * 100) if total else 0:.0f}% 的持仓真的跑满了"
            "——近几期还没有那么长的后续行情。未满窗口的收益被算进同一列，"
            "所以那一列不是一个 30 天收益。各组合的满窗口占比还不相同，"
            "因此「两个组合被同样截断、截断会抵消」这个前提在本次并不成立。"
            "另给一列只用跑满的那部分重算（mean_return_full_horizon_pct）——"
            "它与表中那一列分歧很大（有的组合从正翻到负、名次几乎倒转），"
            "但它的样本是原本就不大的样本的两成，所以这不是「真正的排名」，"
            "是这张表按当前样本无法定夺。两个数都给，判定各自带自己的下界。"
            "要排名就是在做比较，所以面板实际会做的那个比较——相对对照组合 "
            f"{CONTROL}——单独给出一列及其合并下界（vs_control_full_horizon_pct）；"
            "顶层那个判定（full_horizon_verdict）说的只是该均值与零的关系，"
            "不可拿来排名。"),
    }


#: The four counterfactual layers Jon asked for, named in docs/8个思考点.md:392
#: as 选择、择时、仓位、因子. The count and the names both come from here, because
#: they were separated once: the prose said "四层" — inherited from that ask — and
#: then listed three layers of its own devising, so the missing one was 仓位, and
#: nothing in the sentence let a reader notice. A number borrowed from one
#: taxonomy and attached to a different list cannot be checked by anyone.
ATTRIBUTION_LAYERS = (
    ("选择", "同窗口改持该主题的 ETF，问选中具体标的比认出主题多赚了什么", True),
    ("择时", "区分风控（止损/减仓）与买入持有，问进出时点值多少", False),
    ("仓位", "相同成交、不同 sizing 的反事实组合", False),
    ("因子", "对市场 beta 与常见因子回归，问超额里有多少不是暴露", False),
)


def _layers_note() -> str:
    """Which attribution layers exist, and which of them this run computed."""
    done = [n for n, _, ok in ATTRIBUTION_LAYERS if ok]
    todo = [n for n, _, ok in ATTRIBUTION_LAYERS if not ok]
    return (f"归因共 {len(ATTRIBUTION_LAYERS)} 层（"
            + "、".join(n for n, _, _ in ATTRIBUTION_LAYERS)
            + f"）。本表是其中的{'、'.join(done)}层；"
            + "、".join(todo) + "层都还没有做。")


def _theme_attribution(con, positions: list[dict], powered: set[str]) -> dict:
    """One layer of the attribution Jon asked for: theme versus instrument.

    Every macro theme carries a tradable indicator — POLICY-PATH is US.IEF,
    INFLATION is US.TIP. So there is a counterfactual worth pricing: hold the
    theme's own ETF over exactly the window the position was held, and ask what
    picking a specific instrument bought on top of identifying the theme.

    The control does the real work here. `buy_all` takes every candidate without
    choosing, so its excess over the indicators is what the *pool* adds; an arm's
    excess over the indicators includes that same amount. Selection can only be
    credited with the difference between the two, which is why both are reported
    and why neither is reported alone.

    This is one layer, not the four; `ATTRIBUTION_LAYERS` says which four and
    which of them exists, so the claim and the list cannot drift apart. Calling
    this "attribution done" would be the overclaim the item was raised about.
    """
    from ideagen import lexicon

    indicator = {t.id: t.price_indicator for t in lexicon.THEMES}
    topic_of = {
        (str(r["as_of"])[:10], str(r["instrument_id"])): r["topic_id"]
        for r in db.q(con, "SELECT as_of, instrument_id, topic_id FROM candidates")}

    closes: dict[str, dict[str, float]] = {}
    for r in db.q(con, "SELECT code, d, close FROM prices"):
        closes.setdefault(str(r["code"]), {})[str(r["d"])] = float(r["close"])

    def window_return(code: str, start: str, end: str) -> float | None:
        series = closes.get(code) or {}
        opens = [d for d in series if d >= start]
        shuts = [d for d in series if d <= end]
        if not opens or not shuts:
            return None
        first, last = series[min(opens)], series[max(shuts)]
        return None if not first else (last / first - 1.0) * 100.0

    per_arm: dict[str, list[tuple[float, float]]] = {}
    unmatched = 0
    for row in positions:
        if row.get("return_pct") is None or not row.get("entry_d") or not row.get("exit_d"):
            continue
        topic = topic_of.get(
            (str(row["period"])[:10], str(row["instrument_id"])))
        code = indicator.get(topic)
        bench = (window_return(code, str(row["entry_d"])[:10],
                               str(row["exit_d"])[:10]) if code else None)
        if bench is None:
            unmatched += 1
            continue
        per_arm.setdefault(str(row["arm"]), []).append(
            (float(row["return_pct"]), bench))

    arms: dict[str, Any] = {}
    for name, pairs in per_arm.items():
        excess = [held - bench for held, bench in pairs]
        mde = _mde_pct(excess)
        mean = sum(excess) / len(excess)
        arms[name] = {
            "n": len(pairs),
            "mean_held_pct": round(sum(h for h, _ in pairs) / len(pairs), 4),
            "mean_indicator_pct": round(sum(b for _, b in pairs) / len(pairs), 4),
            "mean_excess_pct": round(mean, 4),
            "mde_pct": None if mde is None else round(mde, 3),
            "verdict": ("underpowered" if mde is None or abs(mean) < mde
                        else "not_ruled_out"),
        }

    control = arms.get(CONTROL)
    control_excess = per_arm.get(CONTROL) or []
    control_vals = [held - bench for held, bench in control_excess]
    for name, entry in arms.items():
        entry["verdict_applies_to"] = "mean_excess_pct"
        if control and name != CONTROL:
            entry["excess_over_control_pct"] = round(
                entry["mean_excess_pct"] - control["mean_excess_pct"], 4)
            # `verdict` above is about the excess over the *indicator*, and a
            # reader ranking arms will use the control-relative number instead —
            # the one that isolates selection. Shipping that number beside a
            # verdict computed for a different quantity invites reading the
            # verdict as if it covered it. It gets its own bound: the two
            # samples are independent groups, so the difference carries both.
            own = [held - bench for held, bench in per_arm[name]]
            mde_a, mde_c = _mde_pct(own), _mde_pct(control_vals)
            if mde_a is None or mde_c is None:
                entry["mde_over_control_pct"] = None
                entry["verdict_over_control"] = "underpowered"
                continue
            joint = (mde_a ** 2 + mde_c ** 2) ** 0.5
            entry["mde_over_control_pct"] = round(joint, 3)
            # Three states, because two of them are opposite conclusions that a
            # single "underpowered" hides — the distinction `backtest.py` makes
            # for the paired test and that this layer collapsed when it shipped.
            # An arm whose sample would have surfaced the pre-registered edge and
            # did not has answered the question; saying "not enough data" there
            # is false, and it is the reading people hope for.
            edge = backtest.TARGET_EDGE * 100.0
            # The bound scales as 1/sqrt(n), so the sample that would put it at
            # the declared edge is n·(bound/edge)². Written this way rather than
            # by recovering sd first, because the critical values cancel and
            # carrying them through only hides that.
            need = math.ceil(len(own) * (joint / edge) ** 2)
            entry["n_needed_for_edge"] = need
            entry["edge_pct"] = round(edge, 2)
            if abs(entry["excess_over_control_pct"]) >= joint:
                entry["verdict_over_control"] = "not_ruled_out"
            elif len(own) >= need and name in powered:
                # `no_edge_detected` asserts that a real edge would have shown
                # up, and this bound cannot support an assertion: it counts
                # positions as independent when they share periods, holding
                # windows and names, which is the reason it is documented as a
                # lower bound on blindness. `backtest`'s paired test does the
                # discount properly — six periods of 30-day holds spaced seven
                # days apart come to n_eff≈1.4 — so the claim is only made where
                # that test also calls the arm powered.
                #
                # It caught one: left_tail read `no_edge_detected` off 84
                # positions while the paired test said it needed four effective
                # periods and had 1.4.
                entry["verdict_over_control"] = "no_edge_detected"
            else:
                entry["verdict_over_control"] = "underpowered"
            entry["paired_powered"] = name in powered
    return {
        "layer": "theme_indicator_vs_instrument",
        "control": CONTROL,
        "n_positions": sum(a["n"] for a in arms.values()),
        "unmatched_positions": unmatched,
        "arms": arms,
        "note": (
            "把每笔持仓与其主题的指示 ETF 在同一持有窗口内比较。"
            f"对照组合 {CONTROL} 不做任何挑选，所以它相对指示标的的超额是"
            "「候选池」带来的；各组合的超额都含有这一部分，挑选本身只能记在"
            "「相对对照」那一列上（excess_over_control_pct）——它有自己的下界"
            "与判定（mde_over_control_pct / verdict_over_control），顶层判定说的"
            "是相对指示标的那一列，两者不可混用。"
            "「相对对照」的判定就是表里那三个徽标："
            "「未被排除」（not_ruled_out，变动越过下界，值得盯）、"
            "「没看出优势」（no_edge_detected，样本已够检出预注册的 2 个百分点"
            "优势，而它没有出现——是「没看出优势」，不是「还看不出来」）、"
            "「样本不足」（underpowered，还需多少笔见 n_needed_for_edge）。"
            "判成「没看出优势」还额外要求配对检验也认为这个组合的样本够了："
            "本层的下界把持仓当独立样本，而它们共享期次与持有窗口，"
            "只能用来否定；肯定「本该看见」需要配对检验按有效独立样本折算后的判断。"
            + _layers_note()),
    }


def _verdicts_agree(depths: dict[str, dict]) -> dict[str, bool]:
    """Whether each arm's verdict survives the choice of exclusion depth.

    An arm that reads `not_ruled_out` at ten names and `underpowered` at five
    has not told us anything about itself; it has told us about the ten. This is
    reported per arm rather than as one flag, because they do not all move
    together and a single boolean would hide which ones did.
    """
    names: set[str] = set()
    for d in depths.values():
        names |= set(d["arms"])
    out: dict[str, bool] = {}
    for name in sorted(names):
        seen = {d["arms"][name]["verdict"] for d in depths.values()
                if name in d["arms"]}
        out[name] = len(seen) == 1
    return out


def _drop_top_instruments(positions: list[dict], top_n: int) -> dict:
    """Recompute each arm with the most-held instruments removed.

    The question is Jon's: is a result carried by a handful of names everything
    happened to hold? It is asked by deleting those names and looking again.

    The answer for most arms here is that the question cannot be asked yet, and
    saying so is the finding. A selective arm places ten or twenty positions
    across the whole window, so removing ten instruments removes most of its
    sample — `omega_strict` drops from 11 priced positions to 4 — and a mean
    computed on four is not evidence about the strategy, it is evidence about
    four trades. Those arms are reported as underpowered rather than given a
    number that reads like a result. The broad arms have the sample to answer:
    they lose roughly a third of a point and hold their order, which is what
    "not carried by a few names" looks like.
    """
    freq: dict[str, int] = {}
    for row in positions:
        if row.get("return_pct") is None:
            continue
        key = str(row.get("instrument_id"))
        freq[key] = freq.get(key, 0) + 1
    dropped = sorted(sorted(freq, key=lambda k: (-freq[k], k))[:top_n])

    def stats(rows: list[dict]) -> dict | None:
        rets = [float(r["return_pct"]) for r in rows
                if r.get("return_pct") is not None]
        if not rets:
            return None
        mde = _mde_pct(rets)
        return {"n": len(rets),
                "hit_rate": round(sum(1 for x in rets if x > 0) / len(rets), 3),
                "mean_return_pct": round(sum(rets) / len(rets), 4),
                "mde_pct": None if mde is None else round(mde, 3)}

    arms: dict[str, Any] = {}
    gone_set = set(dropped)
    for arm in sorted({str(r["arm"]) for r in positions}):
        mine = [r for r in positions if r["arm"] == arm]
        full = stats(mine)
        kept_rows = [r for r in mine
                     if str(r.get("instrument_id")) not in gone_set]
        gone_rows = [r for r in mine
                     if str(r.get("instrument_id")) in gone_set]
        kept = stats(kept_rows)
        gone = stats(gone_rows)
        if not full:
            continue
        entry: dict[str, Any] = {"full": full, "excluded": kept,
                                 "kept_share": (round(kept["n"] / full["n"], 3)
                                                if kept else 0.0)}
        delta = (None if not kept else
                 round(kept["mean_return_pct"] - full["mean_return_pct"], 4))
        # `delta_mean_pct` describes the move and nothing more. The comparison
        # that can carry a verdict is a different one: kept against dropped, two
        # disjoint groups. Bounding kept-minus-full by kept's own MDE — which is
        # what shipped — tests a subset against the superset containing it, and
        # charges the difference to only one of the two samples that produced
        # it. Third time tonight I put a verdict beside a quantity it did not
        # cover, so this one states what it covers.
        contrast = (None if not (kept and gone) else
                    round(kept["mean_return_pct"] - gone["mean_return_pct"], 4))
        mde_k = (kept or {}).get("mde_pct")
        mde_g = (gone or {}).get("mde_pct")
        mde = (None if mde_k is None or mde_g is None
               else round((mde_k ** 2 + mde_g ** 2) ** 0.5, 3))
        entry["delta_mean_pct"] = delta
        entry["dropped"] = gone
        entry["kept_vs_dropped_pct"] = contrast
        entry["mde_kept_vs_dropped_pct"] = mde
        entry["verdict_applies_to"] = "kept_vs_dropped_pct"
        delta = contrast
        n_kept = kept["n"] if kept else 0
        held = f"（{n_kept} / {full['n']} 笔持仓保留）"
        if mde is None or delta is None:
            entry["verdict"] = "underpowered"
            entry["why"] = (f"剔除后剩 {n_kept} 笔已计价持仓，"
                            f"两组之一样本太小，算不出可检出差距{held}")
        elif abs(delta) < mde:
            # Not "the arm is stable" — "this sample could not have seen a move
            # this small". The distinction is the whole point of computing an
            # MDE instead of counting rows.
            entry["verdict"] = "underpowered"
            entry["why"] = (
                f"其余标的与被剔除的高频标的相差 {delta:+.2f} 个百分点，"
                f"小于两组合并的可检出下界 {mde:.2f} 个百分点，无法判断{held}")
        else:
            # Deliberately not "shifted". The threshold it cleared is a lower
            # bound on this sample's blindness, so clearing it rules the move
            # in as worth watching and establishes nothing.
            entry["verdict"] = "not_ruled_out"
            entry["why"] = (
                f"其余标的与被剔除的高频标的相差 {delta:+.2f} 个百分点，"
                f"大于两组合并的可检出下界 {mde:.2f} 个百分点{held}；"
                f"该下界忽略了同一组合内持仓的相关性，因此这是「未被排除」，"
                f"不是「已确认变动」")
        arms[arm] = entry

    answerable = [a for a, e in arms.items() if e["verdict"] != "underpowered"]
    # Kept separate from the verdict: how much of each arm's sample the
    # exclusion removed is the plainest statement of why most of them cannot
    # answer, and it needs no statistics to read.
    for name, entry in arms.items():
        entry["dropped_share"] = round(1.0 - entry["kept_share"], 3)
    return {"top_n": top_n, "dropped_instruments": dropped,
            "alpha": 0.05, "power": 0.80, "arms": arms,
            "n_answerable": len(answerable), "n_arms": len(arms),
            "note": (
                "剔除本期最常被持有的标的后重算。判据不是持仓条数，而是剩余样本"
                "自己的最小可检出差距（α=0.05 / power=0.80，与配对检验同口径）："
                "判定比较的是两个不相交的组——其余标的与被剔除的高频标的，"
                "而不是子集与包含它的全集；下界由两组合并给出。"
                "delta_mean_pct 仅作描述（剔除后均值相对全样本的变动），不带判定。"
                "差距小于该门槛、或某一组小到算不出门槛的组合，标为 underpowered。"
                "该门槛忽略了同一组合内持仓之间的相关性，是真实盲区的下界，所以只能用来"
                "否定：越过它的组合标为 not_ruled_out（值得盯，未确认），没有任何组合"
                "会被这项检验判定为「确实依赖高频标的」。")}


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
    print(f"参赛的选取策略 {len(arms)}：{', '.join(arms)}")
    if excluded:
        print(f"排除（需要模型，放进复算式回测会让结果不可复现）：{', '.join(excluded)}")

    rep = backtest.sweep(
        con, days, stage="idea_selector", arms=arms, control=CONTROL,
        horizon_days=args.horizon_days, require_full_horizon=False,
        allow_model=False, strict=True)

    positions = [row for arm in arms
                 for row in _arm_positions(con, days, arm, args.horizon_days)]
    # Positions first, because the curve is now built from them. The previous
    # curve compounded each period's mean *horizon* return once per *period*,
    # over windows that overlap — a 30-day result banked every week, and the
    # same market move counted four times. It read +22.43% for an arm whose
    # periods averaged +3.48%, with the last step a two-day mark treated as a
    # completed period. See `backtest.tranche_curve`.
    points = backtest.tranche_curve(
        con, positions, horizon_days=args.horizon_days, gap_days=rep.gap_days)

    n_backfill = sum(1 for c in classes.values() if c != "live")
    # Three depths, not one. Ten was a number I chose, and a conclusion that
    # only holds at the depth its author picked is a conclusion about the
    # author. Reporting 5 / 10 / 20 lets a reader see whether a verdict moves
    # with the cut, which is the only way to tell a robust answer from a lucky
    # one — the same objection this whole check exists to raise.
    robustness = _drop_top_instruments(positions, top_n=10)
    robustness["depths"] = {
        str(n): _drop_top_instruments(positions, top_n=n) for n in (5, 10, 20)}
    robustness["verdict_stable_across_depths"] = _verdicts_agree(
        robustness["depths"])
    powered_arms = {name for name, p in rep.paired.items()
                    if getattr(p, "powered", False)}
    attribution = _theme_attribution(con, positions, powered_arms)
    horizon = _horizon_completeness(positions, args.horizon_days)
    head_to_head = _generation_head_to_head(con, days, args.horizon_days)
    robustness["depth_note"] = (
        "顶层字段即 depths['10']，保留是为了不改已有消费方的形状。"
        "判定不随剪切深度稳定（verdict_stable_across_depths=false）的组合，"
        "其结论取决于剪切多少个"
        "标的，不能当成关于该组合的判断——本次 omega_loose 与 left_tail 即如此。")
    dating = _shelf_dating(
        con, {str(r.get("instrument_id")) for r in positions if r.get("instrument_id")},
        days[0].isoformat())
    # Counted, not asserted. The sentence used to announce "两项" and then list
    # three, because the holding-period note was appended later and the number
    # in front of it was a literal. A count that does not come from the list it
    # counts drifts the moment anyone adds to the list.
    if dating["held_dated"] and not dating["held_after_window_start"]:
        # The gate can only be said to have cleared the risk for the rows it was
        # able to judge; the undated remainder is reported next to it, not folded
        # into a single reassuring sentence.
        asof_note = (
            f"货架上架日期：本次回放持有的 {dating['held_total']} 个标的中 "
            f"{dating['held_dated']} 个已定上架日期，最新一个为 "
            f"{dating['held_latest_first_seen']}，均早于窗口起点 "
            f"{days[0].isoformat()}，该项前视风险在这些标的上不成立；"
            f"其余 {dating['held_total'] - dating['held_dated']} 个（基金 / 结构化"
            f"产品，无行情代码）仍未定日期，按当期资格过滤时一律放行。")
    else:
        asof_note = (
            f"货架上有 {dating['shelf_undated']} 个标的缺少上架日期，按当期资格"
            f"过滤时一律放行，补跑期的可选标的可能包含当时尚未上架的产品。")
    provenance = _theme_provenance(days)
    ranking_power = _ranking_power(con, days, args.horizon_days)
    live_split = _live_vs_backfill(positions, classes)
    bench = _benchmark_series(con, points)
    exposure = _exposure(points, rep.gap_days, args.horizon_days,
                         bench[0], bench[1])
    ranking_power["volatility_control"] = backtest.instrument_vol_gradient(
        con, positions, _ev_bucket_of(con, days))
    summary = {
        "data_classification": ("mixed-live-backfill" if n_backfill else "live"),
        "proof": "real_pools_real_prices_asof_replay",
        "predictive_claim": False,
        "periods": len(days),
        "dates": [d.isoformat() for d in days],
        "period_classification": classes,
        "n_live_periods": len(days) - n_backfill,
        "n_backfill_periods": n_backfill,
        "disclaimer": _disclaimer(
            n_backfill=n_backfill, asof_note=asof_note, horizon=horizon,
            horizon_days=args.horizon_days, excluded=excluded,
            provenance=provenance),
        "undated_shelf_instruments": dating["shelf_undated"],
        "shelf_dating": dating,
        "robustness_drop_top": robustness,
        "attribution_theme_layer": attribution,
        "theme_provenance": provenance,
        "ranking_power": ranking_power,
        "exposure": exposure,
        "live_vs_backfill": live_split,
        "horizon_completeness": horizon,
        "generation_head_to_head": head_to_head,
        # The sweep ran with strict=True, so every period's context passed the
        # as-of audit before any arm scored it — a context carrying a document,
        # a close or a candidate dated after the replayed day raises AsOfLeak
        # instead of quietly producing a number. Reaching this line is the
        # proof; stating it saves a reader from having to know that.
        "asof_audit": {
            "strict": True,
            "periods_audited": len(days),
            "leaks": 0,
            "checks": "研报发布日 / 行情收盘日 / 候选期次，任一晚于回放日即中止",
        },
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
            # `invested_frac` / `no_series` are curve context, not columns —
            # `backtest_points` has neither, and adding them would mean a schema
            # migration in a file another session is holding. They travel in the
            # summary instead, where `exposure` already carries the same facts.
            schema.upsert(p.state, "backtest_points",
                          {"backtest_id": backtest_id,
                           **{k: v for k, v in row.items()
                              if k not in ("invested_frac", "no_series")}})
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
