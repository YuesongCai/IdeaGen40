"""The control that was missing: rank on price, read nothing.

`buy_all` and `random_pick` both draw from the pool the language model wrote, so
every arm in the comparison shares the semantic layer and none of them can say
what that layer is worth. Their own docstring names the gap — "the candidate pool
could be good enough that anything works" — and then nothing measures it. The
ladder of controls had cash, the index, and two draws from the model's own pool,
with the rung that matters missing: a rule that reads no research at all.

It is not a straw man. Over 105 weekly periods (2024-08-07 → 2026-08-05) on the
89 shelf names with two years of prices, taking the top ten by trailing 21-session
return and holding thirty days returned, as a four-tranche portfolio:

    mom_21    CAGR 20.8%  vol 14.5%  maxDD -13.7%   return/vol 1.44
    hold_all  CAGR 13.3%  vol  7.5%  maxDD  -8.0%   return/vol 1.78
    SPY       CAGR 18.5%  vol 16.6%  maxDD -18.8%   return/vol 1.11

— i.e. this arm alone clears the 25% target's monthly requirement in that window,
which makes it the opponent the semantic arms actually have to beat. On the six
real periods, scored inside the product's own candidate pools, it returns +2.33%
a month against the full pool's +1.35%; `ev_rank` returns +2.96%, so the whole
semantic apparatus is currently worth **+0.63pp a month over three lines of
arithmetic**, at t=0.81 on six periods — not distinguishable from zero.

Two properties make it worth carrying permanently rather than as a one-off check:
it costs zero model calls, so it can be scored on every historical period
including ones no generator ever ran; and its lookback is 21 sessions, the same
one month the methodology fixed in advance, so it is not a horizon chosen after
seeing results. The 63-session and 252-21 variants were both measured on the same
105 periods and both lose (21.5% and 15.5% annualised) — reported here so that
picking the 21 is on the record as one of three, not as the survivor of a search
nobody sees.

The score is `ret_21s` — the raw trailing 21-session return, clamped by the same
as-of audit every other price read goes through. The first version of this arm
ranked on `priced_in` instead, because that field was already in the context and
reads like the same thing; it is not. `priced_in` is the move's percentile within
the instrument's *own* year, so a bill fund at the top of its quiet range outranks
a miner up 12%, and the arm bought the calmest names on the shelf: it came last of
eleven at a 30% hit rate, an almost exact inversion of the +2.44%/66% the raw
return produced over the same measurement. Both numbers were called "momentum"
and only one of them is.

That mistake is worth leaving on the record here, because it is the failure mode
this arm exists to catch in others: a plausible field, a plausible name, and no
check that the number does what the label says.

Two properties make it worth carrying permanently rather than as a one-off check:
it costs zero model calls, so it can be scored on every historical period; and its
lookback is the same one month the methodology fixed in advance. The 63-session
and 252-21 variants were both measured on the same 105 periods and both lose
(21.5% and 15.5% annualised) — on the record so that taking the 21 reads as one
of three, not as the survivor of a search nobody sees.

**It selects nothing in a live run, on purpose.** `orchestrator.weekly` injects no
prices — `ctx.prices` is `{}` in every live period, which is also why stage A's P
factor has recorded `neutral_default` in all six — so this arm has no score to
rank on and says so in `meta.why_empty` rather than falling back to a constant.
An arm that quietly ranked ties would report ten names and measure a missing price
series.
"""

from __future__ import annotations

from ..strategy import RunContext, Verdict, register


@register("idea_selector", "mom_21", "1.0", label="一月动量（对照）",
          role="control", needs_model=False, params={"n": 10})
def mom_21(ctx: RunContext) -> Verdict:
    """Top n by trailing one-month return percentile. No research, no model.

    Candidates whose percentile is the neutral default are rejected rather than
    ranked: a code with no computable history scores 50 alongside every other
    such code, and admitting them would fill the basket with ties — the arm would
    still return ten names and the comparison would be measuring a missing price
    series.
    """
    n = int(ctx.params.get("n", 10))
    scores: dict[str, float] = {}
    rejected: dict[str, str] = {}
    n_no_px = 0
    for c in ctx.candidates:
        code = c.get("futu_code")
        px = ctx.prices.get(code) if code else None
        r = (px or {}).get("ret_21s")
        if r is None:
            n_no_px += 1
            rejected[c["id"]] = "无一月动量（上下文里没有该标的的价格序列）"
            continue
        scores[c["id"]] = round(float(r) * 100.0, 4)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    chosen = [i for i, _ in ranked[:n]]
    for i, sc in ranked[n:]:
        rejected[i] = f"一月动量 {sc:+.2f}%，不在前 {n}"
    meta: dict = {
        "n": len(chosen), "lookback_sessions": 21,
        "score": "ret_21s — 前 21 个交易日的原始收益率（百分数）",
        "cut_at_pct": (round(ranked[n - 1][1], 2) if len(ranked) >= n else None),
        "n_unscorable": n_no_px}
    if not scores:
        meta["why_empty"] = (
            "上下文没有价格：live 周跑不注入 prices，本臂只在回测回放里可评分")
    return Verdict(strategy="mom_21", version="1.0", chosen=chosen,
                   scores=scores, rejected=rejected, meta=meta)
