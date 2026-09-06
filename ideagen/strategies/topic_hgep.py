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

from .. import claims as _claims
from ..strategy import RunContext, Verdict, register

WEIGHTS = {"H": 0.30, "G": 0.25, "E": 0.25, "P": 0.20}

#: The category → score table E used until 2026-09-06. Kept only to compute
#: `E_category_legacy`, the control. It is the coding Jon showed inverting the
#: evidence order: 「政策尚未落地」 → policy → 100, 「盈利预测缺乏依据」 →
#: earnings → 75, 「订单已签署并完成交付」 → orders, unmapped → 25.
DEPTH = {"policy": 100, "earnings": 75, "price": 50, "other": 25}

#: How many documents per period the claim model may be asked to read. Beyond
#: it, documents take the clause path and the cap is reported in `meta`, so a
#: period that was partly mechanical says so. A cap rather than "all": the
#: first period after a theme change misses the cache for every document, and
#: an unbounded pass would put several hundred sequential calls in front of
#: stage B.
CLAIMS_MODEL_MAX_DOCS = 300

#: Value P takes when it was not measured. It is not a reading and every row
#: that carries it says so (`P_measured=False`, `p_source="neutral_default"`).
P_NEUTRAL = 50.0


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
                  live: list[str], unmeasured: list[str],
                  defaulted: dict[str, tuple[int, int]] | None = None) -> str:
    """What actually decided the ranking, with every factor accounted for.

    Each factor lands in exactly one of three states and every state is said
    out loud, so `len(inert) + len(live) + len(unmeasured) == len(WEIGHTS)`
    holds by construction rather than by a number someone typed.

    `defaulted` — {factor: (n_default, n_total)} — is a fourth thing to say
    that is not a fourth state: a factor can be inert *because* every topic
    got the same fill-in value, and 「P 对所有主题取值相同」 alone would let
    that read as a measurement that happened to agree. The fill is named.
    """
    parts = []
    if inert:
        parts.append(f"{'、'.join(inert)} 对所有主题取值相同，合计权重 "
                     f"{inert_weight:.2f} 不参与排序")
    if unmeasured:
        parts.append(f"{'、'.join(unmeasured)} 没有取到值，未参与打分")
    for f, (n_def, n_tot) in sorted((defaulted or {}).items()):
        if n_def:
            parts.append(f"{f} 有 {n_def}/{n_tot} 个主题是缺数默认值（不是读数）"
                         + ("，全部未测量" if n_def == n_tot else ""))
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
    doc_themes: dict[int, list[Any]] = defaultdict(list)
    for doc in ctx.corpus:
        text = _claims.doc_text(doc)
        for t in topics:
            n = _match(text, t.terms)
            # A single keyword is a mention, not evidence: require two terms, or
            # one plus enough body to be a scoreable document.
            if n >= 2 or (n == 1 and len(text) >= 400):
                hits[t.id].append({**doc, "hits": n})
                doc_themes[id(doc)].append(t)

    # G's input: every matched document read once, against every theme it
    # matched, into object-anchored claims (see `claims.py`). The model path
    # is taken when a real inference port is present and the run has not
    # opted out; a replay port is excluded because its FIFO fallback would
    # hand a claim prompt a generator's recorded answer.
    use_model = (ctx.infer is not None
                 and bool(ctx.params.get("claims_model", True))
                 and not getattr(ctx.infer, "replay_only", False))
    max_docs = int(ctx.params.get("claims_model_max_docs", CLAIMS_MODEL_MAX_DOCS))
    claims_by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
    receipts = {"docs": 0, "model": 0, "model_cache": 0, "clause": 0,
                "fallback": 0, "capped": 0, "calls": 0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0}}
    model_calls = 0
    for doc in ctx.corpus:
        themes_for = doc_themes.get(id(doc))
        if not themes_for:
            continue
        receipts["docs"] += 1
        infer = ctx.infer if use_model and model_calls < max_docs else None
        if use_model and infer is None:
            receipts["capped"] += 1
        rows, rc = _claims.extract_claims_detailed(
            doc, themes_for, infer=infer, cache=ctx.claim_cache if infer else None)
        model_calls += rc["calls"]
        receipts["calls"] += rc["calls"]
        if rc["error"]:
            receipts["fallback"] += 1
        elif rc["source"] == "model":
            receipts["model"] += 1
        elif rc["source"] == "model:cache":
            receipts["model_cache"] += 1
        else:
            receipts["clause"] += 1
        for k in receipts["usage"]:
            receipts["usage"][k] += int((rc.get("usage") or {}).get(k) or 0)
        for c in rows:
            claims_by_theme[c["theme_id"]].append(c)

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

        # G — disagreement, from the claims attributed to *this* theme: one
        # institution, one direction, one vote (框架 §12). The whole-document
        # word-list coding is kept beside it as `G_keyword`, the control —
        # on a single-object document the two agree, and where they part is
        # exactly the multi-object / rate-object case the claim path exists for.
        titles = {e.get("doc_id"): e.get("title") or "" for e in ev}
        g_detail = _claims.disagreement(claims_by_theme.get(t.id, []), titles)
        G = _claims.g_score(g_detail["n_pos"], g_detail["n_neg"])
        stances = [lexicon.stance_of(
            " ".join(filter(None, (e.get("title"), e.get("summary"))))) for e in ev]
        kpos, kneg = stances.count(1), stances.count(-1)
        G_keyword = _claims.g_score(kpos, kneg)
        g_source = ("model" if g_detail["source_mix"].get("model")
                    else "clause" if g_detail["n_claims"] else "none")

        # E — how far down the causal chain the strongest evidence sits.
        # Causal depth per docs/scoring_a_hgep.xml (25 叙事 / 50 价格已动 /
        # 75 订单收入 / 100 利润实现·政策落地·签约), read clause by clause
        # with the unrealised downgrade, then the three strongest *distinct*
        # facts — one per institution (or title signature), so a fact retold
        # by four outlets is one fact, not four (H already counts the outlets).
        #
        # What is NOT settled here: the 0.25 weight, and whether E should
        # filter for "solid" themes at all — Jon (2026-09-06 §3) has not
        # accepted any replacement and an early-stage theme may be the
        # better research object. Only the confirmed implementation faults
        # are fixed (inverted ordering, missing orders tier, retell
        # inflation). Both codings are recorded so the comparison can be run
        # on real periods before either is argued for.
        e_by_key: dict[str, dict[str, Any]] = {}
        legacy_depths = []
        for e in ev:
            if int(e.get("tier") or 3) > 2:
                continue
            etext = " ".join(filter(None, (e.get("title"), e.get("summary"))))
            legacy_depths.append(DEPTH.get(lexicon.fact_type_of(etext), 25))
            dd = lexicon.depth_detail(etext)
            key = (f"inst:{e['institution']}" if e.get("institution")
                   else "sig:" + lexicon.title_signature(e.get("title") or ""))
            row = {"doc_id": e.get("doc_id"), "title": (e.get("title") or "")[:80],
                   "tier": int(e.get("tier") or 3), "depth": dd["depth"],
                   "raw_depth": dd["raw_depth"], "matched": dd["matched"],
                   "clause": dd["clause"], "downgraded": dd["downgraded"],
                   "marker": dd["marker"], "institution": e.get("institution"),
                   "fact_type_legacy": lexicon.fact_type_of(etext),
                   "dedupe_key": key}
            if key not in e_by_key or row["depth"] > e_by_key[key]["depth"]:
                e_by_key[key] = row
        e_detail = sorted(e_by_key.values(), key=lambda r: -r["depth"])[:3]
        E = float(st.mean(r["depth"] for r in e_detail)) if e_detail else 25.0
        legacy_top = sorted(legacy_depths, reverse=True)[:3]
        E_category_legacy = float(st.mean(legacy_top)) if legacy_top else 25.0

        # P — how much is already in the price. The view arrives from
        # `pricing.price_view` with its own provenance; a code the view has no
        # series for, or a series too short to rank, scores the neutral fill
        # and says so. The fill enters the formula unchanged (the weight is
        # not the question here), but a measured 50 and a filled 50 leave
        # this function as different rows and stay different to the panel.
        px = ctx.prices.get(t.price_indicator) or {}
        measured = px.get("priced_in_source") == "return_percentile_21s"
        P = float(px["priced_in"]) if measured else P_NEUTRAL
        p_detail = {
            "indicator": t.price_indicator,
            "clamped_to": px.get("clamped_to"),
            "as_of_used": px.get("priced_in_last_d") or px.get("d"),
            "n_samples": int(px.get("priced_in_n") or 0),
            "value": round(P, 1) if measured else None,
            "method": "return_percentile_21s",
            "source": ("return_percentile_21s" if measured
                       else "neutral_default"),
            "reason": (None if measured else
                       (px.get("priced_in_reason") if px else "行情视图里没有这个标的的序列")),
        }

        total = (WEIGHTS["H"] * H + WEIGHTS["G"] * G + WEIGHTS["E"] * E
                 + WEIGHTS["P"] * (100.0 - P))
        scores[t.id] = {
            "label": t.label, "score": round(total, 1),
            "H": round(H, 1), "G": round(G, 1), "E": round(E, 1), "P": round(P, 1),
            "G_keyword": round(G_keyword, 1), "g_detail": g_detail,
            "g_source": g_source,
            "E_category_legacy": round(E_category_legacy, 1), "e_detail": e_detail,
            "P_measured": measured, "p_detail": p_detail,
            "n_evidence": len(ev), "n_institutions": len(insts),
            "indicator": t.price_indicator,
            "p_source": p_detail["source"],
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
    # On 2026-09-02 E was 100 everywhere and P was 50 everywhere, so 0.45 of
    # the declared weight decided nothing and the ordering came from H and G
    # alone — while the panel named four factors and drew four coloured
    # segments. P's constant turned out to be the fill: no live entry point
    # passed prices. E's was the category coding. Both are addressed above;
    # the dispersion is still reported every period, because "it is measured
    # now" is a claim the next period has to keep earning. `spread` is zero
    # exactly when a factor is inert.
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
    # A P that is inert because every topic got the fill is not a P that
    # was measured and agreed. Counted here and said in the note, so the
    # sentence 「P 对所有主题取值相同」 cannot pass for a measurement.
    n_p_default = sum(1 for row in scores.values() if not row.get("P_measured"))
    defaulted = {"P": (n_p_default, len(scores))} if scores else {}
    if "P" in dispersion:
        dispersion["P"]["n_default"] = n_p_default
        dispersion["P"]["n_measured"] = len(scores) - n_p_default

    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["score"])
    top = int(ctx.params.get("top_n", 5))
    chosen = [tid for tid, _ in ranked[:top]]
    return Verdict(
        strategy="hgep", version="1.0", chosen=chosen, scores=scores,
        calls=receipts["calls"],
        rejected={tid: f"rank {i+1}" for i, (tid, _) in enumerate(ranked[top:], top)},
        meta={"weights": WEIGHTS, "registered_topics": len(topics),
              "topics_with_evidence": len(scores), "loudest_count": loudest,
              "top_n": top, "factor_dispersion": dispersion,
              "inert_factors": inert, "inert_weight": inert_weight,
              "unmeasured_factors": unmeasured,
              "p_defaulted": n_p_default, "p_measured": len(scores) - n_p_default,
              # What G was computed from this period, and what it cost: how
              # many documents were read by the model, served from the cache,
              # coded mechanically, or fell back after a bad response, plus
              # the token counts the port reported. `claims_model` says
              # whether the model path was even on.
              "claims": {**receipts, "model_enabled": use_model,
                         "max_docs": max_docs,
                         "extractor": _claims.EXTRACTOR_VERSION,
                         "cache_hits": getattr(ctx.claim_cache, "hits", None),
                         "cache_misses": getattr(ctx.claim_cache, "misses", None)},
              "ranking_note": _ranking_note(inert, inert_weight, live,
                                            unmeasured, defaulted)})
