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
        rejected={
            str(candidate["id"]): "由 " + "、".join(_proposed_by(candidate)) + " 提出"
            for candidate in ctx.candidates
            if method not in _proposed_by(candidate)
        },
        meta={
            "generation_method": method,
            "n": len(chosen),
            "selection": "method isolation; no cross-method ranking",
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
    role="primary",
    params={},
)
def generated_carl_constraint(ctx: RunContext) -> Verdict:
    return _pick(ctx, "carl_constraint", "generated_carl_constraint")
