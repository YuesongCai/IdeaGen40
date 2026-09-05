"""Backtest arms that isolate the two Stage-B generation methodologies.

The historical candidate pool contains ideas from both generators. These
selectors do not rank across methods; each admits only the ideas produced by its
named generator. Running both over the same RunContext keeps the comparison
paired while attributing the difference to Stage B.
"""

from __future__ import annotations

from ..strategy import RunContext, Verdict, register


def _proposed_by(candidate: dict) -> list[str]:
    """Every method that argued for this instrument.

    The pool merges one candidate per instrument, and a candidate two methods
    reached carries `method="merged"` with the contributors in `proposed_by`.
    Matching on `method` alone therefore asks "which instruments did *only*
    this method reach" — the opposite of what these books are for: they exist
    to answer "what if we believed one generator", and an idea is no less that
    generator's for having been agreed with. Before the pool stored
    provenance, `method` was all there was, so it stays as the fallback.
    """
    by = candidate.get("proposed_by")
    if isinstance(by, (list, tuple)) and by:
        return [str(m) for m in by]
    return [str(candidate.get("method") or "")]


def _pick(ctx: RunContext, method: str, name: str) -> Verdict:
    chosen = [
        str(candidate["id"])
        for candidate in ctx.candidates
        if method in _proposed_by(candidate)
    ]
    return Verdict(
        strategy=name,
        version="1.0",
        chosen=chosen,
        # The reason has to name the rule, not just the antecedent. It used to
        # say 「由 ai_native、chain、gap 提出」 — true, and silent about why that
        # meant rejection. Asked on 2026-09-05 how this arm decides, the model
        # had every rejection in front of it, reconstructed the pattern
        # correctly from which combinations were dropped, and then said the
        # rule itself was not in the record — because it was not. A reason that
        # states the input and withholds the test cannot be reasoned back to
        # the test, and this is the arm Jon asks about by name.
        rejected={
            str(candidate["id"]):
                f"{method} 没有提出它（提出者："
                + "、".join(_proposed_by(candidate)) + "）"
            for candidate in ctx.candidates
            if method not in _proposed_by(candidate)
        },
        meta={
            "generation_method": method,
            "n": len(chosen),
            "selection": "method isolation; no cross-method ranking",
            # Spelled out because the arm's name reads like a filter and is
            # not one: nothing here scores, ranks or thresholds an idea. It
            # holds exactly what one generator wrote, so the book answers
            # "what if we believed only this method" and the difference
            # between two such books is attributable to Stage B alone.
            "rule": (f"只保留 {method} 这条生成方式提出过的想法，其余全部剔除；"
                     f"不打分、不排序、不设阈值。本期 {len(chosen)} 条入选。"),
        },
    )


@register(
    "idea_selector",
    "generated_ai_native",
    "1.0",
    label="来源限定 · AI 端到端",
    role="control",
    params={},
)
def generated_ai_native(ctx: RunContext) -> Verdict:
    return _pick(ctx, "ai_native", "generated_ai_native")


@register(
    "idea_selector",
    "generated_carl_constraint",
    "1.0",
    label="来源限定 · 约束边界",
    # Paired with generated_ai_native above: same _pick, same purpose, and the
    # comment in _pick calls them a matched pair whose difference is
    # attributable to Stage B alone. One of the two was registered "primary" —
    # a copy-paste slip that made one half of a ruler read as a strategy, so
    # anything grouping by role counted a benchmark among the things being
    # benchmarked. The panel's own glossary had it as 对照 all along; the two
    # sources disagreed on exactly this one arm and nothing compared them.
    role="control",
    params={},
)
def generated_carl_constraint(ctx: RunContext) -> Verdict:
    return _pick(ctx, "carl_constraint", "generated_carl_constraint")
