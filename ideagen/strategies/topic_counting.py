"""筛选A control arm: pure mention counting, no semantics, no model.

This arm exists because of a standing disagreement worth settling with data
rather than argument. The system's founding claim is that AI semantic judgement
is the edge; the counter-position (Jon's, stated 07-27: LLMs "只擅长数数，金融
分析真的不行") implies that a plain mention-count baseline should do about as
well at picking the week's topics as the four-factor semantic score.

Both cannot be right, and the disagreement is testable for free: this arm scores
a topic as nothing but "how many documents mentioned it, weighted by nothing".
If HGEP cannot beat this on realised idea outcomes, the G/E/P factors — the
model calls, the evidence reading, the priced-in judgement — are decoration on
top of counting, and the system should know that about itself. If HGEP beats it
clearly, the founding claim earned its keep. Either answer is worth more than
the argument.

Zero model calls, so it runs on every tick of history for free.
"""

from __future__ import annotations

from ..strategy import RunContext, Verdict, register


@register("topic_scorer", "counting", "1.0", role="control",
          label="纯数数（对照）", needs_model=False,
          params={"n_topics": 5})
def counting(ctx: RunContext) -> Verdict:
    """Rank themes by raw document mention count. Nothing else."""
    from datetime import date as _date
    from .. import lexicon

    themes = lexicon.all_themes(ctx.as_of if isinstance(ctx.as_of, _date)
                                else _date.fromisoformat(str(ctx.as_of)))
    counts: dict[str, int] = {}
    for t in themes:
        terms = [str(x).lower() for x in (t.terms or []) if str(x).strip()]
        if not terms:
            continue
        n = 0
        for d in ctx.corpus:
            blob = (f"{d.get('title','')} {d.get('summary','')} "
                    f"{d.get('body','')}").lower()
            if any(w in blob for w in terms):
                n += 1
        counts[t.id] = n

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    k = int(ctx.params.get("n_topics", 5))
    chosen = [tid for tid, n in ranked[:k] if n > 0]
    return Verdict(
        strategy="counting", version="1.0",
        chosen=chosen,
        scores={tid: {"mentions": n, "score": n} for tid, n in ranked},
        rejected={tid: f"提及 {n} 篇，未进前 {k}" for tid, n in ranked[k:]},
        meta={"themes_scored": len(counts),
              "corpus_docs": len(ctx.corpus)},
    )
