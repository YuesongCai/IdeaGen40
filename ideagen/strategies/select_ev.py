"""Rank by the probability-weighted return the run itself stated.

Every candidate arrives carrying a three-point scenario — upside/base/downside
with probabilities — and its expectation is the one number that combines all
six. Nothing selected on it. The omega family ranks gains over losses against
the cash hurdle, which is a left-tail measure and deliberately blind to how
large the upside is; calibration ranks on honesty of the probabilities; spread
and left-tail rank on portfolio shape. Expectation was computed for every idea,
stored as `ideas.ev_c`, reported by `analytics.ranking_report` — and then not
used to choose anything.

It ranks. On the six real periods, quintiles of expectation within each period
separate the outcome monotonically, and the top quintile is the only slice that
clears the benchmark:

    Q1  n=89  hit 62.9%  mean +0.67%
    Q5  n=74  hit 73.0%  mean +3.13%      buy_all: hit 60.9%  mean +1.37%

Q5 − Q1 is positive in all six periods (+1.3 to +4.9pp) and Q5 beats SPY in
five of six. `ranking_report` had already measured the same thing across 1561
v0.3 ideas without anyone acting on it: Spearman 0.164 against realised return.

**This arm was chosen after seeing those outcomes, and that is the one thing a
reader must not be allowed to forget.** It is the multiple-testing objection
Jon raised on 2026-08-18, and the numbers above are in-sample to the search that
found them: two rankings were tried against the same six periods, `grade` and
expectation; `grade` did not rank (S +1.61%, A +0.95%, C +0.17% — no order at
all) and this one did. A rule with that provenance has earned a forward test and
nothing else, which is why it registers as `exploratory` and why its live
periods are the only ones that will ever be evidence.

The expectation is ex-ante by construction: it is a function of the scenarios
the generator wrote before the period began, and of nothing that happened after.
"""

from __future__ import annotations

import statistics as st

from ..strategy import RunContext, Verdict, register

#: Same monthly cash hurdle the omega family uses, for the same reason: an idea
#: whose expectation is below cash is not a weak buy, it is a worse buy than not
#: buying. Sharing the constant keeps the two arms comparable — they then differ
#: in objective alone, which is the question their books are meant to price.
DEFAULT_HURDLE_M = 0.0028


def _ev(c: dict) -> float | None:
    """Probability-weighted return of the stated three-point scenario, in pct.

    Renormalises the probabilities rather than trusting them to sum to one: a
    generator that emits 0.3/0.3/0.3 is expressing the same view as 1/3 each,
    and scoring it lower for arithmetic would rank writing style.
    """
    rs = [c.get("upside_pct"), 0.0, c.get("downside_pct")]
    ps = [c.get("p_up"), c.get("p_base"), c.get("p_down")]
    if any(v is None for v in rs + ps):
        return None
    tot = sum(ps)
    if tot <= 0:
        return None
    return sum(p / tot * r for p, r in zip(ps, rs))


def _rank(ctx: RunContext) -> tuple[list[tuple[str, float]], dict[str, float],
                                    dict[str, str]]:
    scores, bad = {}, {}
    for c in ctx.candidates:
        e = _ev(c)
        if e is None:
            bad[c["id"]] = "incomplete scenarios"
        else:
            scores[c["id"]] = e
    return sorted(scores.items(), key=lambda kv: -kv[1]), scores, bad


@register("idea_selector", "ev_rank", "1.0", label="期望值排序",
          role="exploratory", params={"n_min": 4, "n_max": 8, "top_frac": 0.20})
def ev_rank(ctx: RunContext) -> Verdict:
    """Take the top fifth by expectation, and only what clears the cash hurdle.

    Admission is a threshold and a fraction, never a quota, for the reason the
    omega arm states: a quota fills its last slots with candidates already known
    to be weak, while a threshold lets a poor week resolve to holding cash, which
    is a real portfolio state. Two bars bind, and the rejection says which — the
    top-fraction cut, and the hurdle. The fraction is what the evidence is about
    (Q5, not "above average"), so widening it would be reading a different rule
    than the one that was measured.
    """
    ranked, scores, bad = _rank(ctx)
    top_frac = float(ctx.params.get("top_frac", 0.20))
    lo = int(ctx.params.get("n_min", 4))
    hi = int(ctx.params.get("n_max", 8))
    hurdle_pct = float(ctx.params.get("hurdle_monthly", DEFAULT_HURDLE_M)) * 100.0

    rejected: dict[str, str] = dict(bad)
    if not ranked:
        return Verdict(strategy="ev_rank", version="1.0", chosen=[],
                       scores=scores, rejected=rejected,
                       meta={"n": 0, "hurdle_pct": hurdle_pct,
                             "why_empty": "本期没有可打分的候选"})

    cut = max(1, int(len(ranked) * top_frac))
    for i, _ in ranked[cut:]:
        rejected[i] = f"不在期望值前 {top_frac:.0%}"
    pool = []
    for i, s in ranked[:cut]:
        if s < hurdle_pct:
            rejected[i] = (f"期望回报 {s:.2f}% 低于同期现金 {hurdle_pct:.2f}%")
        else:
            pool.append(i)

    chosen = pool[:hi]
    if len(chosen) < lo:
        # Short of the floor. The remainder stays in cash rather than being
        # back-filled from the names this arm just rejected.
        for i in chosen:
            rejected.pop(i, None)
    med = st.median([s for _, s in ranked]) if ranked else 0.0
    return Verdict(
        strategy="ev_rank", version="1.0", chosen=chosen, scores=scores,
        rejected=rejected,
        meta={"n": len(chosen), "hurdle_pct": round(hurdle_pct, 4),
              "top_frac": top_frac, "cut_at": cut,
              "batch_median_ev_pct": round(med, 4),
              "admitted_below_floor": len(chosen) < lo})
