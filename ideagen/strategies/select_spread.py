"""Optimise the held *set*, not the ranking of items in it.

Every other selector scores candidates one at a time and takes the top ten. With
~100 candidates coming from only 5 topics, that ranking is not independent: the
topic that scored best in 筛选A gets the most confident odds in 筛选B, so its
twenty ideas cluster at the top of any per-item score. Ten positions then express
one bet held ten times — the realised variance of the book is far above what "ten
positions" implies, because the ten legs move together, and a single theme being
wrong costs the entire month rather than a tenth of it. At 25% of the portfolio
per weekly tranche and four weeks to roll out, that is a mistake the book carries
for a month with no way to shrink it.

So this arm ranks the same way and then admits under caps: at most N per
topic_id, per exposure, and per generating method. The caps are the strategy. A
displaced idea is not judged worse than the one that took its slot; it is judged
redundant given what is already held, which is a property of the set and cannot
be expressed as a per-item score. When the caps cannot fill ten slots the
shortfall is left in cash (JPST) rather than back-filled by breaking a cap —
filling the last two slots with the eleventh and twelfth copy of the winning
theme would give back exactly what the method exists to buy.
"""

from __future__ import annotations

from collections import Counter

from ..strategy import RunContext, Verdict, register

#: Duplicated from `select_omega` on purpose. These two arms are compared against
#: each other, and importing the ranking from one into the other would mean a
#: later tuning of omega's internals silently changes what this arm did in weeks
#: already stored — the difference between their books would stop being
#: attributable to the caps. Keep the number, not the reference.
HURDLE_M = 0.0028                  # ~3.4% annual money-market yield / 12


def _omega(c: dict, h: float) -> float | None:
    """Probability-weighted gain above the cash hurdle over loss below it."""
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
    return float("inf") if loss <= 0 else gain / loss


#: (candidate field, params key, default, Chinese name). The defaults are repeated
#: here and in `register(params=...)` because nothing merges a strategy's declared
#: params into `ctx.params` — the registry treats them as documentation. Two
#: numbers that must agree and are checked by nobody, so they are stated once per
#: dimension and read from this tuple only.
_DIMS = (("topic_id", "max_per_topic", 3, "主题"),
         ("exposure", "max_per_exposure", 3, "风险敞口"),
         ("method", "max_per_method", 4, "生成方法"))


@register("idea_selector", "spread", "1.0", label="分散度约束", role="primary",
          params={"n": 10, "max_per_topic": 3, "max_per_exposure": 3,
                  "max_per_method": 4})
def spread(ctx: RunContext) -> Verdict:
    """Rank by omega, admit greedily under per-topic / -exposure / -method caps."""
    n = int(ctx.params.get("n", 10))
    caps = {key: int(ctx.params.get(pkey, dflt)) for key, pkey, dflt, _ in _DIMS}

    by_id = {str(c["id"]): c for c in ctx.candidates}
    omega: dict[str, float] = {}
    rejected: dict[str, str] = {}
    for cid, c in by_id.items():
        o = _omega(c, HURDLE_M)
        if o is None:
            rejected[cid] = "三档情景不完整，无法计算赚亏比"
        else:
            omega[cid] = o

    ranked = sorted(omega.items(), key=lambda kv: -kv[1])
    held: Counter[tuple[str, str]] = Counter()
    chosen: list[str] = []
    # Which cap turned each idea away, and who was already occupying the slots.
    # Without the incumbents recorded, a displaced idea cannot argue its case: the
    # only defensible answer to "why not this one" is "these three instead".
    bound: Counter[str] = Counter()
    displaced: dict[str, dict[str, list[str]]] = {}

    for cid, _ in ranked:
        if len(chosen) >= n:
            rejected[cid] = f"名额已满（前 {n} 名在各上限内已被占满）"
            continue
        c = by_id[cid]
        blockers = [(label, key) for key, _p, _d, label in _DIMS
                    if held[(key, str(c.get(key) or "—"))] >= caps[key]]
        if blockers:
            names = "、".join(lab for lab, _ in blockers)
            val = {lab: str(c.get(key) or "—") for lab, key in blockers}
            rejected[cid] = (f"已达{names}上限（"
                             + "，".join(f"{lab}={val[lab]}" for lab, _ in blockers)
                             + "），组合层面重复而非质量不足")
            for lab, key in blockers:
                bound[lab] += 1
                displaced[cid] = {**displaced.get(cid, {}), lab: [
                    h for h in chosen if str(by_id[h].get(key)) == str(c.get(key))]}
            continue
        chosen.append(cid)
        for key, _p, _d, _lab in _DIMS:
            held[(key, str(c.get(key) or "—"))] += 1

    dist = {label: dict(Counter(str(by_id[i].get(key) or "—") for i in chosen))
            for key, _p, _d, label in _DIMS}
    return Verdict(
        strategy="spread", version="1.0", chosen=chosen,
        scores={i: {"omega": omega[i], "rank": r + 1}
                for r, (i, _) in enumerate(ranked)},
        rejected=rejected,
        meta={"n": len(chosen), "target_n": n, "caps": caps,
              "hurdle_monthly": HURDLE_M, "distribution": dist,
              "caps_bound": dict(bound), "displaced": displaced,
              # A shortfall is a real portfolio state (the rest sits in JPST), but
              # it must be visible: unallocated cash that nobody chose is a bug.
              "shortfall": max(0, n - len(chosen)),
              "scoreable": len(omega)},
    )
