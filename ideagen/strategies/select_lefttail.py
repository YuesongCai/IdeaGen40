"""Rank by expected loss alone, ignoring the upside entirely.

The return target implies the objective is loss containment rather than winner
hunting. This is that inference taken to its limit: if it holds, a selector that
never looks at the upside should not do badly. If it does badly, the inference was
wrong, which is worth knowing early and cheaply — this one costs no model calls.
"""

from __future__ import annotations

import statistics as st

from ..strategy import RunContext, Verdict, register


@register("idea_selector", "left_tail", "1.0", label="9. 只看最多亏多少",
          role="exploratory", params={"n_min": 6, "n_max": 14})
def left_tail(ctx: RunContext) -> Verdict:
    loss = {}
    bad = {}
    for c in ctx.candidates:
        p, d = c.get("p_down"), c.get("downside_pct")
        if p is None or d is None:
            bad[c["id"]] = "incomplete scenarios"
        else:
            loss[c["id"]] = (p / 100.0 if p > 1 else p) * abs(d) / 100.0
    ranked = sorted(loss.items(), key=lambda kv: kv[1])          # smallest first
    if not ranked:
        return Verdict(strategy="left_tail", version="1.0", rejected=bad)
    med = st.median([v for _, v in ranked])
    chosen = [i for i, v in ranked if v <= med][:int(ctx.params.get("n_max", 14))]
    rej = {i: "expected loss above batch median" for i, v in ranked
           if i not in set(chosen)}
    rej.update(bad)
    return Verdict(strategy="left_tail", version="1.0", chosen=chosen,
                   scores=loss, rejected=rej,
                   meta={"n": len(chosen), "median_expected_loss": round(med, 5)})
