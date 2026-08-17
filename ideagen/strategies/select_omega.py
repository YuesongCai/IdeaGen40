"""Rank by probability-weighted gain over loss, against the cash hurdle.

Two variants differing only in how many they admit, so the difference between
their books is attributable to admission strictness alone and not to ranking.
"""

from __future__ import annotations

import statistics as st

from ..strategy import RunContext, Verdict, register

#: Monthly cash hurdle. The usual objection to this family of ratios is that the
#: threshold is arbitrary; the opportunity cost of holding cash is not.
DEFAULT_HURDLE_M = 0.0028          # ~3.4% annual money-market yield / 12


def _omega(c: dict, h: float) -> float | None:
    """Gains above the hurdle divided by losses below it, probability-weighted."""
    rs = [c.get("upside_pct"), 0.0, c.get("downside_pct")]
    ps = [c.get("p_up"), c.get("p_base"), c.get("p_down")]
    if any(v is None for v in rs + ps):
        return None
    tot = sum(ps)
    if tot <= 0:
        return None
    ps = [p / tot for p in ps]
    gain = sum(p * max(r / 100.0 - h, 0.0) for p, r in zip(ps, rs))
    loss = sum(p * max(h - r / 100.0, 0.0) for p, r in zip(ps, rs))
    if loss <= 0:
        return float("inf")
    return gain / loss


def _admit(ranked: list[tuple[str, float]], *, strict: bool,
           lo: int, hi: int, floor: float) -> tuple[list[str], dict[str, str]]:
    """Admit by threshold, never by quota.

    A quota forces the last slots to be filled with candidates already known to be
    weak. A threshold lets a quiet week resolve to holding cash, which is a real
    and correct portfolio state. The bounds exist because concentration would
    otherwise drift the wrong way: few passing means an unclear market, and without
    a floor that is exactly when position sizes would be largest.
    """
    rejected: dict[str, str] = {}
    if not ranked:
        return [], rejected
    if strict:
        cut = max(1, int(len(ranked) * 0.40))
        pool = [(i, s) for i, s in ranked[:cut] if s >= floor]
        for i, s in ranked[cut:]:
            rejected[i] = "not in top 40%"
        for i, s in ranked[:cut]:
            if s < floor:
                rejected[i] = f"below floor {floor}"
        hi = min(hi, 8)
    else:
        med = st.median([s for _, s in ranked if s != float("inf")] or [0.0])
        pool = [(i, s) for i, s in ranked if s >= med]
        for i, s in ranked:
            if s < med:
                rejected[i] = "below batch median"
    chosen = [i for i, _ in pool[:hi]]
    if len(chosen) < lo:
        # Not enough passed. The shortfall is parked in cash by the orchestrator
        # rather than back-filled with rejected candidates.
        for i in chosen:
            rejected.pop(i, None)
        return chosen, rejected
    return chosen, rejected


def _rank(ctx: RunContext) -> tuple[list[tuple[str, float]], dict, dict[str, str]]:
    h = float(ctx.params.get("hurdle_monthly", DEFAULT_HURDLE_M))
    scores, bad = {}, {}
    for c in ctx.candidates:
        o = _omega(c, h)
        if o is None:
            bad[c["id"]] = "incomplete scenarios"
        else:
            scores[c["id"]] = o
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return ranked, {"hurdle_monthly": h, "omega": scores,
                    "inf_count": sum(1 for v in scores.values() if v == float("inf"))}, bad


@register("idea_selector", "omega_loose", "1.0", label="3. 按赚亏比排",
          role="primary", params={"n_min": 6, "n_max": 14, "floor": 1.5})
def omega_loose(ctx: RunContext) -> Verdict:
    """Rank by gain-over-loss versus cash; admit everything above the batch median.

    This ratio is already a left-tail measure, which is what the return target
    calls for: reaching 25% annual needs only ~+1.9% net per idea per month, so
    the work is in not losing rather than in finding large winners.
    """
    ranked, meta, bad = _rank(ctx)
    chosen, rej = _admit(ranked, strict=False,
                         lo=int(ctx.params.get("n_min", 6)),
                         hi=int(ctx.params.get("n_max", 14)),
                         floor=float(ctx.params.get("floor", 1.5)))
    rej.update(bad)
    return Verdict(strategy="omega_loose", version="1.0", chosen=chosen,
                   scores=meta["omega"], rejected=rej,
                   meta={**{k: v for k, v in meta.items() if k != "omega"},
                         "n": len(chosen), "admission": "loose"})


@register("idea_selector", "omega_strict", "1.0", label="7. 赚亏比 + 严门槛",
          role="exploratory", params={"n_min": 6, "n_max": 8, "floor": 1.5})
def omega_strict(ctx: RunContext) -> Verdict:
    """Same ranking, top 40% only, floor enforced, remainder held in cash.

    Differs from `omega_loose` in admission alone, so the gap between their books
    prices one question: is cash an under-used position?
    """
    ranked, meta, bad = _rank(ctx)
    chosen, rej = _admit(ranked, strict=True,
                         lo=int(ctx.params.get("n_min", 6)),
                         hi=int(ctx.params.get("n_max", 8)),
                         floor=float(ctx.params.get("floor", 1.5)))
    rej.update(bad)
    return Verdict(strategy="omega_strict", version="1.0", chosen=chosen,
                   scores=meta["omega"], rejected=rej,
                   meta={**{k: v for k, v in meta.items() if k != "omega"},
                         "n": len(chosen), "admission": "strict"})
