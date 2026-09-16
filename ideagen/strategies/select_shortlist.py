"""精选：the handful of names a PM decides on, booked so it has a track record.

yifu 2026-09-11: 「全量可看，决策时精选到个位数」. The panel already had a
shortlist toggle, but it was ranked in the browser and never owned by anything:
no book held it, so nobody could say whether reading the shortlist instead of the
full pool would have made or lost money. Registering it as a selector gives it
the same book, the same execution rules and the same comparison as every other
arm — which is the only way "the shortlist is better" can ever be tested.

Ranking and admission live in `decision.rank_shortlist` so the book, the panel
and the order ticket are the same list by construction. See that function for
the score (共识度 × max(期望值, 0) × (1 − 复现折扣比例)) and the per-theme cap.

The inputs (`ev_c`, `grade`, `recur_frac`) are stamped on the pool by
`decision.annotate_pool` before stage C runs. A context nobody annotated (a
synthetic backtest) falls back to the scenario's gross expectation and records
`score_source="scenario_gross"`, so the two can never be mistaken for each other.

Registered as exploratory: the formula was proposed in conversation with the
periods' results visible, which is the same provenance `ev_rank` carries.
"""

from __future__ import annotations

from .. import config, decision
from ..strategy import RunContext, Verdict, register


@register("idea_selector", "shortlist", "1.0", label="精选",
          role="exploratory",
          params={"n": config.SHORTLIST_N,
                  "max_per_theme": config.SHORTLIST_MAX_PER_THEME})
def shortlist(ctx: RunContext) -> Verdict:
    """Top `n` by consensus × positive expectation × (1 − recurrence share)."""
    n = int(ctx.params.get("n", config.SHORTLIST_N))
    cap = int(ctx.params.get("max_per_theme", config.SHORTLIST_MAX_PER_THEME))
    rk = decision.rank_shortlist(ctx.candidates, n=n, max_per_theme=cap)
    meta = {"n": n, "max_per_theme": cap, "chosen": len(rk["chosen"]),
            "score_source": rk["score_source"]}
    if not rk["chosen"]:
        meta["why_empty"] = "本期没有期望值为正的可入池候选"
    return Verdict(strategy="shortlist", version="1.0", chosen=rk["chosen"],
                   scores=rk["rows"], rejected=rk["rejected"], meta=meta)
