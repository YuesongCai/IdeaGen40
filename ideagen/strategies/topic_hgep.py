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


def _partition_factors(dispersion: dict) -> tuple[list, float, list, list]:
    """Split `WEIGHTS` into inert / live / unmeasured, covering all of it.

    A factor no topic produced a value for is absent from `dispersion`
    entirely, so it is neither inert nor discriminating — it is unmeasured, and
    the two are not the same claim. The note used to end 「本期四个因子都有区分
    度」 with the count written by hand, and an unmeasured factor was counted
    among the four as one that discriminated: the one thing nobody looked at,
    reported as the thing that worked.

    Split here rather than inline so the partition is reachable by a test. It
    was inline first, and a mutation that folded `unmeasured` back into the
    discriminating set passed every check, because the checks could only reach
    the sentence and the fault was in what got handed to it.
    """
    inert = sorted(f for f, d in dispersion.items() if not d["discriminates"])
    inert_weight = round(sum(WEIGHTS[f] for f in inert), 2)
    unmeasured = sorted(f for f in WEIGHTS if f not in dispersion)
    live = [f for f in WEIGHTS if f in dispersion and f not in inert]
    return inert, inert_weight, live, unmeasured


def _ranking_note(inert: list[str], inert_weight: float,
                  live: list[str], unmeasured: list[str]) -> str:
    """What actually decided the ranking, with every factor accounted for.

    Each factor lands in exactly one of three states and every state is said
    out loud, so `len(inert) + len(live) + len(unmeasured) == len(WEIGHTS)`
    holds by construction rather than by a number someone typed.
    """
    parts = []
    if inert:
        parts.append(f"{'、'.join(inert)} 对所有主题取值相同，合计权重 "
                     f"{inert_weight:.2f} 不参与排序")
    if unmeasured:
        parts.append(f"{'、'.join(unmeasured)} 没有取到值，未参与打分")
    if not parts:
        return f"本期 {len(WEIGHTS)} 个因子都有区分度"
    # Every factor inert or unmeasured is not a weaker version of the normal
    # case, it is a different one: the scores are all equal and the top-5 is
    # whatever order the dict happened to be in. Saying "实际由 无 决定" would
    # read as a degenerate phrasing of a working run.
    parts.append("实际由 " + "、".join(live) + " 决定" if live else
                 "没有任何因子参与排序，本期名次不成立")
    return "本期 " + "；".join(parts)


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
            # The audit trail for "为什么读了这些就选了它": exactly which
            # documents scored this topic, strongest match first. Without this
            # list, ask-the-run can only *re-derive* the evidence set and prove
            # the counts agree; with it, the run states its own sources.
            "doc_ids": [e.get("doc_id") for e in
                        sorted(ev, key=lambda e: (e.get("hits", 0),
                                                  str(e.get("published_d") or "")),
                               reverse=True)],
        }

    # A factor that takes the same value for every topic adds a constant to
    # every score and cannot move the ranking, however much weight it carries.
    # This period E is 100 everywhere and P is 50 everywhere, so 0.45 of the
    # declared weight decided nothing and the ordering came from H and G alone
    # — while the panel names four factors and draws four coloured segments.
    #
    # Not corrected here: what E and P should measure is a methodology question,
    # and a scorer is not where it gets answered. Reported instead, every period,
    # so the condition is visible as it happens rather than noticed by someone
    # reading an answer the ask-endpoint gave about something else. `spread` is
    # zero exactly when a factor is inert.
    dispersion = {}
    for factor in WEIGHTS:
        seen = [row[factor] for row in scores.values() if row.get(factor) is not None]
        if not seen:
            continue
        dispersion[factor] = {
            "distinct_values": len(set(seen)),
            "spread": round(max(seen) - min(seen), 1),
            "weight": WEIGHTS[factor],
            "discriminates": len(set(seen)) > 1,
        }
    inert, inert_weight, live, unmeasured = _partition_factors(dispersion)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["score"])
    top = int(ctx.params.get("top_n", 5))
    chosen = [tid for tid, _ in ranked[:top]]
    return Verdict(
        strategy="hgep", version="1.0", chosen=chosen, scores=scores,
        rejected={tid: f"rank {i+1}" for i, (tid, _) in enumerate(ranked[top:], top)},
        meta={"weights": WEIGHTS, "registered_topics": len(topics),
              "topics_with_evidence": len(scores), "loudest_count": loudest,
              "top_n": top, "factor_dispersion": dispersion,
              "inert_factors": inert, "inert_weight": inert_weight,
              "unmeasured_factors": unmeasured,
              "ranking_note": _ranking_note(inert, inert_weight, live,
                                            unmeasured)})
