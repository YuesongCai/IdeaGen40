"""The two controls. Neither calls a model.

They exist because a selector that looks good tells you nothing on its own: it
could be picking well, or the candidate pool could be good enough that anything
works. Those two situations call for opposite next steps, so both have to be
measurable.
"""

from __future__ import annotations

import random

from ..strategy import RunContext, Verdict, register

MAX_N = 14


@register("idea_selector", "buy_all", "1.0", label="1. 不筛全买",
          role="control")
def buy_all(ctx: RunContext) -> Verdict:
    """Take every candidate. Answers whether selecting adds anything at all."""
    ids = [c["id"] for c in ctx.candidates]
    return Verdict(strategy="buy_all", version="1.0", chosen=ids,
                   meta={"note": "no ranking; weight halved by the orchestrator",
                         "n": len(ids)})


@register("idea_selector", "random_pick", "1.0", label="2. 随机抽",
          role="control", params={"n": 10, "seed": 20260817})
def random_pick(ctx: RunContext) -> Verdict:
    """Draw n at random from a fixed seed. Answers whether *ranking* carries
    information, which is a different question from whether filtering does.

    The seed is fixed and recorded so the draw is reproducible — an unauditable
    control is not a control.
    """
    n = int(ctx.params.get("n", 10))
    seed = int(ctx.params.get("seed", 20260817))
    ids = [c["id"] for c in ctx.candidates]
    rng = random.Random(f"{seed}:{ctx.as_of.isoformat()}")
    chosen = rng.sample(ids, min(n, len(ids)))
    return Verdict(strategy="random_pick", version="1.0", chosen=chosen,
                   rejected={i: "not drawn" for i in ids if i not in set(chosen)},
                   meta={"seed": seed, "n": len(chosen)})
