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
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

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
            "所以那一列不是一个 30 天收益。各臂的满窗口占比还不相同，"
            "因此「两臂被同样截断、截断会抵消」这个前提在本次并不成立。"
            "mean_return_full_horizon_pct 是只用跑满的那部分重算的结果——"
            "它与表中那一列分歧很大（有的臂从正翻到负、名次几乎倒转），"
            "但它的样本是原本就不大的样本的两成，所以这不是「真正的排名」，"
            "是这张表按当前样本无法定夺。两个数都给，判定各自带自己的下界。"),
    }


def _theme_attribution(con, positions: list[dict]) -> dict:
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

    This is one layer, not the four. The market-beta layer and the layer
    separating risk controls from buy-and-hold are still missing, and calling
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
            elif len(own) >= need:
                entry["verdict_over_control"] = "no_edge_detected"
            else:
                entry["verdict_over_control"] = "underpowered"
    return {
        "layer": "theme_indicator_vs_instrument",
        "control": CONTROL,
        "n_positions": sum(a["n"] for a in arms.values()),
        "unmatched_positions": unmatched,
        "arms": arms,
        "note": (
            "把每笔持仓与其主题的指示 ETF 在同一持有窗口内比较。"
            f"对照臂 {CONTROL} 不做任何挑选，所以它相对指示标的的超额是"
            "「候选池」带来的；各臂的超额都含有这一部分，挑选本身只能记在"
            "excess_over_control_pct 上——那一列有它自己的下界与判定"
            "（mde_over_control_pct / verdict_over_control），顶层 verdict "
            "说的是相对指示标的那一列，两者不可混用。"
            "verdict_over_control 有三态：not_ruled_out（变动越过下界，值得盯）、"
            "no_edge_detected（样本已够检出预注册的 2 个百分点优势，而它没有出现"
            "——这是「没看出优势」，不是「还看不出来」）、underpowered"
            "（样本还不够，n_needed_for_edge 给出需要多少笔）。"
            "这是四层归因里的一层——市场 beta 层、以及区分风控与买入持有的那层，"
            "都还没有做。"),
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
    for arm in sorted({str(r["arm"]) for r in positions}):
        mine = [r for r in positions if r["arm"] == arm]
        full = stats(mine)
        kept = stats([r for r in mine
                      if str(r.get("instrument_id")) not in set(dropped)])
        if not full:
            continue
        entry: dict[str, Any] = {"full": full, "excluded": kept,
                                 "kept_share": (round(kept["n"] / full["n"], 3)
                                                if kept else 0.0)}
        delta = (None if not kept else
                 round(kept["mean_return_pct"] - full["mean_return_pct"], 4))
        mde = kept.get("mde_pct") if kept else None
        # The move has to clear what the remaining sample could have seen. A
        # swing smaller than the sample's own detectable effect is not a finding
        # about the strategy, and neither is a swing measured where no effect of
        # any size could have been detected.
        entry["delta_mean_pct"] = delta
        n_kept = kept["n"] if kept else 0
        held = f"（{n_kept} / {full['n']} 笔持仓保留）"
        if mde is None or delta is None:
            entry["verdict"] = "underpowered"
            entry["why"] = f"剔除后剩 {n_kept} 笔已计价持仓，不足以计算可检出差距{held}"
        elif abs(delta) < mde:
            # Not "the arm is stable" — "this sample could not have seen a move
            # this small". The distinction is the whole point of computing an
            # MDE instead of counting rows.
            entry["verdict"] = "underpowered"
            entry["why"] = (
                f"剔除后平均收益变动 {delta:+.2f} 个百分点，小于该样本自身的"
                f"最小可检出差距 {mde:.2f} 个百分点，无法判断{held}")
        else:
            # Deliberately not "shifted". The threshold it cleared is a lower
            # bound on this sample's blindness, so clearing it rules the move
            # in as worth watching and establishes nothing.
            entry["verdict"] = "not_ruled_out"
            entry["why"] = (
                f"剔除后平均收益变动 {delta:+.2f} 个百分点，大于该样本可检出下界 "
                f"{mde:.2f} 个百分点{held}；该下界忽略了同臂持仓的相关性，"
                f"因此这是「未被排除」，不是「已确认变动」")
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
                "变动小于该门槛、或样本小到算不出门槛的臂，标为 underpowered。"
                "该门槛忽略了同臂持仓之间的相关性，是真实盲区的下界，所以只能用来"
                "否定：越过它的臂标为 not_ruled_out（值得盯，未确认），没有任何臂"
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
    attribution = _theme_attribution(con, positions)
    horizon = _horizon_completeness(positions, args.horizon_days)
    robustness["depth_note"] = (
        "顶层字段即 depths['10']，保留是为了不改已有消费方的形状。"
        "verdict_stable_across_depths 为 false 的臂，其结论取决于剪切多少个"
        "标的，不能当成关于该臂的判断——本次 omega_loose 与 left_tail 即如此。")
    dating = _shelf_dating(
        con, {str(r.get("instrument_id")) for r in positions if r.get("instrument_id")},
        days[0].isoformat())
    if dating["held_dated"] and not dating["held_after_window_start"]:
        # The gate can only be said to have cleared the risk for the rows it was
        # able to judge; the undated remainder is reported next to it, not folded
        # into a single reassuring sentence.
        asof_note = (
            f"②货架上架日期：本次回放持有的 {dating['held_total']} 个标的中 "
            f"{dating['held_dated']} 个已定上架日期，最新一个为 "
            f"{dating['held_latest_first_seen']}，均早于窗口起点 "
            f"{days[0].isoformat()}，该项前视风险在这些标的上不成立；"
            f"其余 {dating['held_total'] - dating['held_dated']} 个（基金 / 结构化"
            f"产品，无行情代码）仍未定日期，按当期资格过滤时一律放行。")
    else:
        asof_note = (
            f"②货架上有 {dating['shelf_undated']} 个标的缺少上架日期，按当期资格"
            f"过滤时一律放行，补跑期的可选标的可能包含当时尚未上架的产品。")
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
            "候选池与价格均为真实数据，as-of 在文档层面严格钳制。"
            + (f"其中 {n_backfill} 期是事后补跑（backfill），前视风险两项："
               "①模型权重已见过该日期之后的信息，无法用代码消除；"
               + asof_note
               + (f"③持有期：表中标注 {args.horizon_days} 天，但只有 "
                  f"{(horizon['complete_frac'] or 0) * 100:.0f}% 的持仓跑满该窗口"
                  f"（各臂 {min((v['complete_frac'] or 0) for v in horizon['arms'].values()) * 100:.0f}"
                  f"–{max((v['complete_frac'] or 0) for v in horizon['arms'].values()) * 100:.0f}%"
                  "，并不一致），未满窗口的收益与满窗口的混在同一列。"
                  "只用跑满部分重算的结果见 horizon_completeness。")
               + "结论性判断以 live 期为准。"
               if n_backfill else "")
            + f"未参与：{'、'.join(excluded)}（需调用模型，会使复算不可重复）。"
        ),
        "undated_shelf_instruments": dating["shelf_undated"],
        "shelf_dating": dating,
        "robustness_drop_top": robustness,
        "attribution_theme_layer": attribution,
        "horizon_completeness": horizon,
        # The sweep ran with strict=True, so every period's context passed the
        # as-of audit before any arm scored it — a context carrying a document,
        # a close or a candidate dated after the replayed day raises AsOfLeak
        # instead of quietly producing a number. Reaching this line is the
        # proof; stating it saves a reader from having to know that.
        "asof_audit": {
            "strict": True,
            "periods_audited": len(days),
            "leaks": 0,
            "checks": "语料发布日 / 行情收盘日 / 候选期次，任一晚于回放日即中止",
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
