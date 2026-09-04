"""Discount odds that the evidence in the run does not support.

筛选B is generative, and the characteristic failure of a generative stage is not
a bad idea — it is a *well-formed* one. The model returns 0.70 / 0.20 / 0.10 and
+9% / -3% on a theme carried by three documents, and every downstream selector
that ranks on those numbers rewards exactly the ideas whose numbers were least
constrained by anything observed. Ranking harder on omega cannot detect this,
because the fabricated idea wins on omega by construction.

So this arm ranks on omega and charges four penalties, each comparing an idea's
stated confidence against something else in the same context that ought to move
with it: how few corpus documents back its topic (3 documents cannot carry the
confidence 40 can); p_up above what that topic's own disagreement allows (if the
corpus contradicts itself, 0.7 is a claim about the evidence, not the trade);
|p_sum_raw - 1| from stage B's renormalisation, which has no bearing on the trade
and is therefore an uncontaminated sample of how much care went into the idea;
and an upside far above same-topic peers with no deeper evidence behind it.

Every penalty is bounded, every input comes from `ctx`, and all four land in
`v.scores` per idea, because an idea rejected on a penalty that cannot be
inspected is an idea that cannot be argued with.
"""

from __future__ import annotations

import math
import statistics as st

from ..strategy import RunContext, Verdict, register

#: Duplicated rather than imported from `select_omega`: this arm is compared with
#: that one, so its ranking must stay fixed even if omega's internals are tuned.
HURDLE_M = 0.0028

#: An infinite ratio means stage B gave the loss branch zero probability. That is
#: a modelling artifact, and it is precisely the kind of overconfidence this arm
#: exists to charge for — so it is capped instead of being allowed to win outright.
OMEGA_CAP = 50.0

#: Penalty ceilings, and the total discount a single idea can suffer. Bounded so a
#: thin-evidence idea is demoted rather than deleted: the corpus attribution below
#: is a keyword match, and a method that could zero an idea out would let one
#: missed match silently remove a good trade.
W = {"evidence": 0.25, "confidence": 0.30, "arith": 0.15, "asym": 0.20}
PEN_MAX = 0.70


def _omega(c: dict, h: float) -> float | None:
    rs = [c.get("upside_pct"), 0.0, c.get("downside_pct")]
    ps = [c.get("p_up"), c.get("p_base"), c.get("p_down")]
    if any(v is None for v in rs + ps) or sum(ps) <= 0:
        return None
    ps = [p / sum(ps) for p in ps]
    gain = sum(p * max(r / 100.0 - h, 0.0) for p, r in zip(ps, rs))
    loss = sum(p * max(h - r / 100.0, 0.0) for p, r in zip(ps, rs))
    return OMEGA_CAP if loss <= 0 else min(gain / loss, OMEGA_CAP)


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _topic_evidence(ctx: RunContext) -> dict[str, dict]:
    """Per topic: how many corpus documents back it, and how much they disagree.

    Attribution uses the same theme-term vocabulary as `_gen.corpus_block`
    (via `topic_terms`), so the count here is the evidence the generator was
    actually shown for that topic — a different rule would penalise ideas for
    documents their author never saw.
    """
    from .. import lexicon                      # stance coding only; no file read

    from ._gen import topic_terms

    # The shared vocabulary rule, actually shared. An earlier version rebuilt the
    # matching from the topic slug and label: an English slug tokenises into words
    # no Chinese document contains, and a Chinese label stays one long string that
    # must appear verbatim — so every topic counted zero documents, the thin-
    # evidence penalty became the same constant for every idea, and this arm's
    # entire reason to exist silently stopped discriminating. Using the theme's
    # registered terms via `topic_terms` is what "the evidence the generator was
    # actually shown" really means.
    topics_by_id = {str(t.get("topic_id")): t
                    for t in ctx.topics if isinstance(t, dict)}
    out: dict[str, dict] = {}
    for tid in {str(c.get("topic_id")) for c in ctx.candidates}:
        terms = [w.lower() for w in
                 topic_terms(topics_by_id.get(tid, {"topic_id": tid}))]
        hits = [d for d in ctx.corpus
                if any(w in f"{d.get('title','')} {d.get('summary','')}".lower()
                       for w in terms)] if terms else []
        stances = [lexicon.stance_of(f"{d.get('title','')} {d.get('summary','')}")
                   for d in hits]
        pos, neg = stances.count(1), stances.count(-1)
        # Contested share on hgep's G scale: 1.0 when the corpus splits evenly.
        # With no directional document the corpus makes no claim either way, so
        # 0.5 is recorded as an explicit neutral rather than read as consensus.
        g = 0.5 if not (pos + neg) else 1.0 - abs(pos - neg) / (pos + neg)
        out[tid] = {"n_docs": len(hits), "contested": round(g, 3),
                    "directional": pos + neg}
    return out


@register("idea_selector", "calib", "1.0", label="证据一致性",
          role="primary",
          params={"n": 10, "thin_docs": 12, "p_up_floor": 0.40,
                  "p_up_span": 0.30, "peer_mult": 1.5, "sum_tol": 0.20})
def calib(ctx: RunContext) -> Verdict:
    """Omega, discounted where the run's own evidence contradicts the confidence."""
    p = ctx.params
    n, thin = int(p.get("n", 10)), max(1.0, float(p.get("thin_docs", 12)))
    floor_p, span_p = float(p.get("p_up_floor", 0.40)), float(p.get("p_up_span", 0.30))
    peer_m, tol = float(p.get("peer_mult", 1.5)), float(p.get("sum_tol", 0.20))

    ev = _topic_evidence(ctx)
    peers = {tid: (st.median(ups) if ups else 0.0) for tid, ups in (
        (tid, [abs(float(c["upside_pct"])) for c in ctx.candidates
               if str(c.get("topic_id")) == tid and c.get("upside_pct") is not None])
        for tid in ev)}

    scores: dict[str, dict] = {}
    rejected: dict[str, str] = {}
    for c in ctx.candidates:
        cid, tid = str(c["id"]), str(c.get("topic_id"))
        o = _omega(c, HURDLE_M)
        if o is None:
            rejected[cid] = "三档情景不完整，无法计算赚亏比"
            continue
        e = ev.get(tid, {"n_docs": 0, "contested": 0.5, "directional": 0})

        thin_share = _clamp((thin - e["n_docs"]) / thin)
        p_ev = W["evidence"] * thin_share

        # The more the corpus contradicts itself on the theme, the lower the p_up
        # the evidence can carry: unanimous → 0.70, evenly split → 0.40.
        allow = floor_p + span_p * (1.0 - e["contested"])
        p_up = float(c.get("p_up") or 0.0)
        p_conf = W["confidence"] * _clamp((p_up - allow) / span_p)

        dev = abs(float(c.get("p_sum_raw", 1.0) or 1.0) - 1.0)
        p_ar = W["arith"] * _clamp(dev / tol)

        base = peers.get(tid) or 0.0
        ratio = (abs(float(c["upside_pct"])) / base) if base > 0 else 1.0
        # Halved where the topic is well documented: an outsized target is only a
        # flag when nothing extra in the corpus stands behind it.
        p_as = (W["asym"] * _clamp((ratio - peer_m) / peer_m)
                * (0.5 + 0.5 * thin_share))

        pen = min(PEN_MAX, p_ev + p_conf + p_ar + p_as)
        # The discount applies to log omega, not omega. Omega is a ratio with a
        # long right tail — an idea whose loss branch was given 5% probability
        # scores 20 while a soberly-priced one scores 3, so a 60% haircut off the
        # raw ratio still leaves the least-constrained idea on top and the method
        # would do nothing. On the log scale a percentage penalty is a fixed
        # number of doublings of the ratio, which is the unit the ratio is
        # actually read in, and the haircut can change the ranking.
        scores[cid] = {
            "omega": round(o, 3),
            "adjusted": round(math.log1p(o) * (1.0 - pen), 4),
            "penalty_total": round(pen, 3),
            "pen_evidence": round(p_ev, 3), "pen_confidence": round(p_conf, 3),
            "pen_arith": round(p_ar, 3), "pen_asymmetry": round(p_as, 3),
            "topic_docs": e["n_docs"], "topic_contested": e["contested"],
            "p_up": p_up, "p_up_allowed": round(allow, 3),
            "p_sum_raw": c.get("p_sum_raw"), "upside_vs_peer_median": round(ratio, 2),
        }

    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["adjusted"])
    chosen = [i for i, _ in ranked[:n]]
    worst = {"pen_evidence": "该主题证据太薄，不足以支撑这个信心",
             "pen_confidence": "p_up 高于主题分歧度所能支撑的水平",
             "pen_arith": "三档概率原始和偏离 1 过多，作业粗糙",
             "pen_asymmetry": "上行空间显著高于同主题同侪，但证据没有更厚"}
    for rank, (i, s) in enumerate(ranked[n:], n + 1):
        top = max(worst, key=lambda k: s[k])
        rejected[i] = (f"调整后赚亏比排名 {rank}"
                       + (f"；主要扣分：{worst[top]}" if s[top] > 0.01 else "；赔率本身不够"))
    return Verdict(strategy="calib", version="1.0", chosen=chosen, scores=scores,
                   rejected=rejected,
                   meta={"n": len(chosen), "target_n": n, "weights": W,
                         "penalty_cap": PEN_MAX, "hurdle_monthly": HURDLE_M,
                         "topic_evidence": ev, "peer_upside_median": peers,
                         "corpus_docs": len(ctx.corpus)})
