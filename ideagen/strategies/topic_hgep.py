"""Topic scoring: heat, disagreement, evidence, priced-in.

Mechanical parts run here; the two judgement calls (disagreement, and how much a
move is already priced) route through the inference port when one is available and
fall back to a mechanical proxy when it is not, so a run without model access still
produces a ranking rather than nothing.

The lexicon supplies the topic dictionary and the as-of clamp: a topic registered
after the date being scored is excluded, which is what stops a topic discovered
today from being credited with a call it never made.
"""

from __future__ import annotations

import math
import statistics as st
from collections import defaultdict
from typing import Any

from ..strategy import RunContext, Verdict, register

WEIGHTS = {"H": 0.30, "G": 0.25, "E": 0.25, "P": 0.20}
DEPTH = {"policy": 100, "earnings": 75, "price": 50, "other": 25}


def _match(text: str, terms) -> int:
    low = (text or "").lower()
    return sum(1 for t in terms if t.lower() in low)


@register("topic_scorer", "hgep", "1.0", label="打分 A · HGEP", role="primary",
          params={"top_n": 5})
def hgep(ctx: RunContext) -> Verdict:
    """Rank registered topics and return the top n."""
    from .. import lexicon

    topics = lexicon.all_themes(ctx.as_of)
    if not topics:
        return Verdict(strategy="hgep", version="1.0",
                       meta={"error": "no topics registered as of this date"})

    hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in ctx.corpus:
        text = " ".join(filter(None, (doc.get("title"), doc.get("summary"),
                                      (doc.get("body") or "")[:3000])))
        for t in topics:
            n = _match(text, t.terms)
            # A single keyword is a mention, not evidence: require two terms, or
            # one plus enough body to be a scoreable document.
            if n >= 2 or (n == 1 and len(text) >= 400):
                hits[t.id].append({**doc, "hits": n})

    counts = {tid: len(v) for tid, v in hits.items()}
    loudest = max(counts.values()) if counts else 0
    scores: dict[str, Any] = {}

    for t in topics:
        ev = hits.get(t.id, [])
        if not ev:
            continue
        # H — attention. Log-scaled because the distribution is heavy-tailed; a
        # linear share would compress every topic into the bottom tenth.
        level = 0.0 if not loudest else 100.0 * math.log1p(len(ev)) / math.log1p(loudest)
        insts = {e.get("institution") or f"anon:{(e.get('title') or '')[:16]}" for e in ev}
        # Acceleration proxy: share of the window's evidence landing on the newest
        # day. The full version ranks today against the topic's own 20-day history,
        # which needs stored scorings this context does not carry.
        newest = max((e.get("published_d") or "") for e in ev)
        accel = 100.0 * len([e for e in ev if e.get("published_d") == newest]) / len(ev)
        H = 0.60 * level + 0.40 * accel

        # G — disagreement. Mechanical fallback: spread of stance across evidence.
        stances = [lexicon.stance_of(
            " ".join(filter(None, (e.get("title"), e.get("summary"))))) for e in ev]
        pos, neg = stances.count(1), stances.count(-1)
        G = 0.0 if not (pos + neg) else 100.0 * (1 - abs(pos - neg) / (pos + neg))

        # E — how far down the causal chain the strongest evidence sits.
        depths = sorted((DEPTH.get(lexicon.fact_type_of(
            " ".join(filter(None, (e.get("title"), e.get("summary"))))), 25)
            for e in ev if int(e.get("tier") or 3) <= 2), reverse=True)[:3]
        E = float(st.mean(depths)) if depths else 25.0

        # P — how much is already in the price. Without a price series in context
        # this stays neutral rather than guessing, and neutral is recorded as such.
        px = ctx.prices.get(t.price_indicator) or {}
        P = float(px.get("priced_in", 50.0))

        total = (WEIGHTS["H"] * H + WEIGHTS["G"] * G + WEIGHTS["E"] * E
                 + WEIGHTS["P"] * (100.0 - P))
        scores[t.id] = {
            "label": t.label, "score": round(total, 1),
            "H": round(H, 1), "G": round(G, 1), "E": round(E, 1), "P": round(P, 1),
            "n_evidence": len(ev), "n_institutions": len(insts),
            "indicator": t.price_indicator,
            "p_source": "prices" if px else "neutral_default",
        }

    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["score"])
    top = int(ctx.params.get("top_n", 5))
    chosen = [tid for tid, _ in ranked[:top]]
    return Verdict(
        strategy="hgep", version="1.0", chosen=chosen, scores=scores,
        rejected={tid: f"rank {i+1}" for i, (tid, _) in enumerate(ranked[top:], top)},
        meta={"weights": WEIGHTS, "registered_topics": len(topics),
              "topics_with_evidence": len(scores), "loudest_count": loudest,
              "top_n": top})
