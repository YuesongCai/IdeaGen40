"""v0.4 factor engine: D / A / B / N, plus independent M and C.

What changed relative to 战术宏观主题评分框架 v0.3, and why. Each item below is a
defect the v0.3 text either creates or leaves unmeasurable; the fix is stated
next to it and implemented in the function named.

D  讨论覆盖
   v0.3: `D = 100 × 提及主题的独立日报数 ÷ 窗口有效日报总数`. With one homepage
   daily per day and a 3-day window the denominator is 3, so D takes exactly four
   values. `minimum_valid_reports: 5` can then never be satisfied and every run
   is stamped low-confidence.
   v0.4: the pool is *tiered source items* across all eight Wisburg lines
   (~1,600 in a 3-day window), deduped by content hash and by institution-day so
   a syndicated note still counts once. Because attention counts are heavy-
   tailed, D is log-scaled against the loudest theme in the same window, which
   keeps it continuous and comparable across days.  -> `factor_D`

A  三日升温
   v0.3: a weighted centroid of three daily coverage ratios. With binary daily
   coverage it collapses to five values and cannot distinguish (1,0,1) from
   (1,1,1) — precisely the "sustained vs one-off" distinction it exists to make.
   v0.4: keeps the centroid tilt for continuity, then blends in an intensity term
   that ranks the latest day's share against the same theme's own trailing
   20-day distribution. A now measures acceleration relative to a theme's own
   history rather than which calendar days happened to mention it.  -> `factor_A`

B  关键争议
   v0.3: needs both sides of a debate, but a curated daily keeps one side. The
   `min(N/4,1)` sample discount therefore almost always bites.
   v0.4: stance is coded per item from a published lexicon over the full tiered
   pool, and cross-framework conflict is measured between *source tiers* (first-
   hand vs sell-side vs curation) and between institutions, which is where
   genuinely opposed frameworks actually live.  -> `factor_B`

N  最新变化
   v0.3: Surprise Magnitude needs `|actual − consensus| ÷ 2y forecast-error sd`.
   The corpus does not carry consensus or a forecast-error distribution, so this
   sub-factor sits at NA and §12's renormalisation silently drops the largest
   weight inside the largest factor.
   v0.4: surprise is measured where it is observable — the pre-registered price
   indicator's largest 1-day move in the window, normalised by its own trailing
   daily vol. Same 5-bucket table as v0.3, now always computable. A corpus-
   supplied actual-vs-consensus z overrides it when present.  -> `factor_N`

M  市场验证 (independent, not in TIS — unchanged in spirit)
   Computed against indicators registered before any price is read, so the
   "pre-registration" requirement is structurally enforced. v0.4.1: the theme
   set itself is no longer fixed — see `lexicon.all_themes`, which clamps
   scoring to themes registered on or before the day being scored.

C  拥挤度 (new, independent)
   v0.3 has no crowding measure at all: a theme can score 90 on impact while its
   expression is at a 1-year momentum extreme, and nothing in the model objects.
   C combines trailing-momentum percentile, distance from the 52-week high and
   the volatility regime. High M with high C is a fade condition, and the idea
   layer applies a position haircut rather than a veto.  -> `factor_C`

Tactical Impact Score keeps v0.3's weights (0.15/0.25/0.25/0.35) so scores stay
comparable with the historical pack.
"""

from __future__ import annotations

import json
import math
import statistics as st
from datetime import date, timedelta
from typing import Any, Iterable

from . import config, db, lexicon
from . import themes as themes_mod
from .lexicon import Theme, all_themes
from .sources import futu_px

MIN_STANCE_SAMPLE = 8          # v0.3 used 4; the pool is ~400x larger now
FACT_BREADTH_TARGET = 6        # v0.3 used 4 independent facts for full marks


# ---------------------------------------------------------------- evidence
def _window(as_of: date, days: int) -> list[str]:
    return [(as_of - timedelta(days=i)).isoformat()
            for i in range(days - 1, -1, -1)]


def collect_evidence(con, as_of: date, days: int = config.OBSERVATION_WINDOW_DAYS) -> dict:
    """Tag every in-window document with the themes it mentions.

    Dedupe happens twice, both mandated by 框架 §12:
      * identical content across lines collapses to one item (content_hash);
      * one institution contributes at most one item per theme per day.
    """
    wdays = _window(as_of, days)
    rows = db.q(con,
                "SELECT doc_id,line,tier,title,institution,published_d,summary,body,meta "
                "FROM documents WHERE published_d IN (%s)" % ",".join("?" * len(wdays)),
                wdays)

    themes = all_themes(as_of)
    per_theme: dict[str, list[dict]] = {t.id: [] for t in themes}
    matched_docs: set[str] = set()
    valid_by_day: dict[str, int] = {d: 0 for d in wdays}
    counting_by_day: dict[str, int] = {d: 0 for d in wdays}
    seen_content: set[str] = set()
    dedupe_keys: set[tuple[str, str, str]] = set()
    dropped = {"content": 0, "institution": 0, "syndication": 0}

    for r in rows:
        d = r["published_d"]
        if d not in valid_by_day:
            continue
        # Identical content re-posted on another line counts once (框架 §12).
        ch = r["content_hash"] if "content_hash" in r.keys() else None
        if ch and ch in seen_content:
            dropped["content"] += 1
            continue
        if ch:
            seen_content.add(ch)

        text = " ".join(filter(None, (r["title"], r["summary"], r["body"] or "")))
        valid_by_day[d] += 1
        if r["tier"] in config.COUNTING_TIERS:
            counting_by_day[d] += 1

        inst = r["institution"] or lexicon.institution_of(text)
        sig = lexicon.title_signature(r["title"] or "")

        for t in themes:
            hits = lexicon.match_theme(text, t)
            if hits < 1:
                continue
            matched_docs.add(r["doc_id"])
            # A bare keyword is not evidence: require either two distinct terms
            # or one term plus a scoreable body (框架 §5.1 counting rules).
            if hits < 2 and len(text) < 400:
                continue
            # One institution contributes at most one item per theme per day.
            # When the institution is unknown, fall back to a title signature —
            # NOT to the source line, which would cap every theme at
            # 8 lines x 3 days = 24 items and rebuild the degeneracy v0.4 removes.
            if inst:
                key = ("i", f"{inst}|{d}", t.id)
                bucket = "institution"
            else:
                key = ("s", f"{sig}|{d}", t.id)
                bucket = "syndication"
            if key in dedupe_keys:
                dropped[bucket] += 1
                continue
            dedupe_keys.add(key)

            meta = db.jl(r["meta"], {}) or {}
            sections = meta.get("sections") or {}
            per_theme[t.id].append({
                "doc_id": r["doc_id"], "line": r["line"], "tier": r["tier"], "d": d,
                "institution": inst or f"unattributed:{sig[:12]}",
                "attributed": bool(inst),
                "title": r["title"], "hits": hits,
                "stance": lexicon.stance_of(text),
                "depth": lexicon.depth_of(text),
                "fact_type": lexicon.fact_type_of(text),
                "n_facts": int(sections.get("n_facts") or 0),
                "chars": len(text),
            })

    n_docs = len({r["doc_id"] for r in rows if r["published_d"] in valid_by_day})
    return {"as_of": as_of.isoformat(), "days": wdays,
            "valid_by_day": valid_by_day, "counting_by_day": counting_by_day,
            "valid_total": sum(valid_by_day.values()),
            "counting_total": sum(counting_by_day.values()),
            "dedupe_dropped": dropped,
            # Dictionary reach, reported every day so the blind spot stays
            # visible. A fixed 16-theme list sat at 54% and had no way to say so.
            "registered_themes": len(themes),
            "docs_total": n_docs,
            "docs_matched": len(matched_docs),
            "coverage_pct": lexicon.coverage(len(matched_docs), n_docs),
            "per_theme": per_theme}


# ---------------------------------------------------------------- D
def factor_D(ev: dict, theme_id: str, max_raw: int) -> tuple[float, dict]:
    items = [e for e in ev["per_theme"][theme_id] if e["tier"] in config.COUNTING_TIERS]
    raw = len(items)
    pool = max(ev["counting_total"], 1)
    share = raw / pool
    # Log scaling: attention counts across themes span ~2 orders of magnitude in
    # a single window, so a linear share would compress every theme under 10.
    score = 0.0 if raw == 0 or max_raw == 0 else \
        100.0 * math.log1p(raw) / math.log1p(max_raw)
    return round(min(score, 100.0), 1), {
        "raw_items": raw, "pool": pool, "share": round(share, 5),
        "max_raw_in_window": max_raw,
        "distinct_institutions": len({e["institution"] for e in items}),
        "distinct_lines": len({e["line"] for e in items}),
    }


# ---------------------------------------------------------------- A
def factor_A(con, ev: dict, theme_id: str) -> tuple[float | None, dict]:
    days = ev["days"]
    items = [e for e in ev["per_theme"][theme_id] if e["tier"] in config.COUNTING_TIERS]
    c = []
    for d in days:
        denom = max(ev["counting_by_day"].get(d, 0), 1)
        c.append(len([e for e in items if e["d"] == d]) / denom)

    detail: dict[str, Any] = {"c": [round(x, 5) for x in c]}
    if sum(c) <= 0:
        return 0.0, {**detail, "note": "no coverage in window -> A=0 per 框架 §6"}

    centroid = sum((i + 1) * ci for i, ci in enumerate(c)) / sum(c)
    tilt = 50.0 * (centroid - 1)
    detail["centroid"] = round(centroid, 4)
    detail["tilt"] = round(tilt, 1)

    # Intensity: where does today's share sit in this theme's own recent history?
    hist = _daily_share_history(con, theme_id, days[0], config.BASELINE_WINDOW_DAYS)
    if len(hist) >= 8:
        cur = c[-1]
        below = sum(1 for x in hist if x <= cur)
        intensity = 100.0 * below / len(hist)
        detail["intensity"] = round(intensity, 1)
        detail["baseline_n"] = len(hist)
        score = 0.6 * tilt + 0.4 * intensity
    else:
        detail["intensity"] = None
        detail["baseline_n"] = len(hist)
        detail["note"] = ("baseline <8 days -> intensity NA, renormalised to tilt "
                          "only (框架 §12)")
        score = tilt
    return round(max(0.0, min(score, 100.0)), 1), detail


def _daily_share_history(con, theme_id: str, before: str, days: int) -> list[float]:
    """Trailing daily coverage shares for one theme, from stored scorings."""
    rows = db.q(con, "SELECT as_of, factors FROM themes WHERE theme_id=? AND as_of<? "
                     "ORDER BY as_of DESC LIMIT ?", (theme_id, before, days))
    out = []
    for r in rows:
        f = db.jl(r["factors"], {}) or {}
        c = ((f.get("A") or {}).get("c") or [])
        if c:
            out.append(float(c[-1]))
    return out


# ---------------------------------------------------------------- B
def factor_B(ev: dict, theme_id: str) -> tuple[float, dict]:
    items = ev["per_theme"][theme_id]
    directional = [e for e in items if e["stance"] != 0]
    n = len(directional)
    pos = sum(1 for e in directional if e["stance"] > 0)
    neg = n - pos

    if n == 0:
        dd = 0.0
        pp = pn = 0.0
    else:
        pp, pn = pos / n, neg / n
        dd = 100.0 * 4 * pp * pn * min(n / MIN_STANCE_SAMPLE, 1.0)

    # Cross-framework conflict: opposed median stance between independent source
    # tiers is the strongest available evidence of two frameworks disagreeing on
    # the same causal chain.
    by_tier: dict[int, list[int]] = {}
    for e in directional:
        by_tier.setdefault(e["tier"], []).append(e["stance"])
    tier_signs = {t: (1 if sum(v) > 0 else -1 if sum(v) < 0 else 0)
                  for t, v in by_tier.items() if len(v) >= 2}
    signs = set(tier_signs.values()) - {0}

    by_inst: dict[str, list[int]] = {}
    for e in directional:
        by_inst.setdefault(e["institution"], []).append(e["stance"])
    inst_signs = {i: (1 if sum(v) > 0 else -1 if sum(v) < 0 else 0)
                  for i, v in by_inst.items()}
    opposed_inst = (sum(1 for s in inst_signs.values() if s > 0) >= 2
                    and sum(1 for s in inst_signs.values() if s < 0) >= 2)

    if len(signs) >= 2 and len(tier_signs) >= 2:
        cf, why = 100.0, "≥2 独立来源层级结论明确对立"
    elif len(signs) >= 2:
        cf, why = 70.0, "两类来源结论相反，传导链仍需验证"
    elif opposed_inst:
        cf, why = 40.0, "同一层级内多家机构显著分歧"
    elif n >= MIN_STANCE_SAMPLE and 0.3 <= pp <= 0.7:
        cf, why = 40.0, "方向分布接近对半，但无跨层级对立"
    elif n > 0:
        cf, why = 10.0, "仅条件差异，无方向冲突"
    else:
        cf, why = 0.0, "无方向明确观点"

    score = 0.65 * dd + 0.35 * cf
    return round(min(score, 100.0), 1), {
        "n_directional": n, "pos": pos, "neg": neg,
        "p_pos": round(pp, 3), "p_neg": round(pn, 3),
        "sample_discount": round(min(n / MIN_STANCE_SAMPLE, 1.0), 3),
        "directional_debate": round(dd, 1),
        "cross_framework": cf, "cross_framework_why": why,
        "tier_signs": tier_signs,
    }


# ---------------------------------------------------------------- N
_Z_TABLE = ((0.5, 20.0), (1.0, 40.0), (1.5, 60.0), (2.0, 80.0))


def _z_to_score(z: float) -> float:
    for cut, s in _Z_TABLE:
        if z < cut:
            return s
    return 100.0


def factor_N(con, ev: dict, theme_id: str, theme: Theme,
             consensus_z: float | None = None) -> tuple[float, dict]:
    items = ev["per_theme"][theme_id]
    facts = [e for e in items if e["tier"] in config.FACT_TIERS]

    # Breadth: distinct (institution, fact_type) pairs among first-hand/sell-side
    # documents. Counting pairs rather than documents stops one institution
    # publishing five notes on the same event from reading as five new facts.
    pairs = {(e["institution"], e["fact_type"]) for e in facts}
    breadth = 100.0 * min(len(pairs) / FACT_BREADTH_TARGET, 1.0)

    # Surprise: largest normalised 1-day move of the pre-registered indicator.
    zs: list[tuple[str, float]] = []
    for d in ev["days"]:
        z = futu_px.move_z(con, theme.price_indicator, d)
        if z is not None:
            zs.append((d, z))
    if consensus_z is not None:
        surprise, src = _z_to_score(consensus_z), f"corpus consensus z={consensus_z:.2f}"
        zmax = consensus_z
    elif zs:
        day, zmax = max(zs, key=lambda kv: kv[1])
        surprise, src = _z_to_score(zmax), f"{theme.price_indicator} 1d z={zmax:.2f} on {day}"
    else:
        surprise, src, zmax = 20.0, "no price history for indicator -> floor 20", None

    # Causal depth: mean of the three most consequential facts (框架 §8.3).
    depths = sorted((e["depth"] for e in facts), reverse=True)[:3]
    depth = st.mean(depths) if depths else 25.0

    score = 0.30 * breadth + 0.40 * surprise + 0.30 * depth
    return round(min(score, 100.0), 1), {
        "fact_items": len(facts), "distinct_pairs": len(pairs),
        "breadth": round(breadth, 1),
        "surprise": round(surprise, 1), "surprise_src": src,
        "surprise_z": (round(zmax, 3) if zmax is not None else None),
        "depth": round(depth, 1), "depth_top3": depths,
        "fact_types": sorted({e["fact_type"] for e in facts}),
    }


# ---------------------------------------------------------------- M
def factor_M(con, ev: dict, theme: Theme, direction: str) -> tuple[float | None, dict]:
    d0, d1 = ev["days"][0], ev["days"][-1]
    want = 1 if direction == "↑" else -1
    codes = [theme.price_indicator, *theme.related]

    moves: dict[str, float | None] = {}
    for c in codes:
        a = futu_px.last_close_on_or_before(con, c, d0)
        b = futu_px.last_close_on_or_before(con, c, d1)
        moves[c] = (b[1] / a[1] - 1) if (a and b and a[1]) else None

    have = {c: m for c, m in moves.items() if m is not None}
    if not have:
        return None, {"note": "no price data for registered indicators", "moves": moves}

    hit = st.mean([1.0 if (m > 0) == (want > 0) else 0.0 for m in have.values()]) * 100
    pct = futu_px.return_percentile(con, theme.price_indicator, d1,
                                    window=len(ev["days"]))
    mag = pct if pct is not None else 50.0
    if want < 0 and pct is not None:
        mag = 100.0 - pct
    breadth = 100.0 * sum(1 for m in have.values() if (m > 0) == (want > 0)) / len(have)

    score = 0.40 * hit + 0.30 * mag + 0.30 * breadth
    return round(min(score, 100.0), 1), {
        "direction": direction, "moves": {k: (round(v, 4) if v is not None else None)
                                          for k, v in moves.items()},
        "hit_rate": round(hit, 1), "magnitude_pct": round(mag, 1),
        "breadth": round(breadth, 1), "primary": theme.price_indicator,
    }


def validation_stage(m: float | None) -> str:
    if m is None:
        return "NA"
    if m < 30:
        return "尚未定价"
    if m < 60:
        return "早期验证"
    if m < 80:
        return "已有确认"
    return "交易成熟"


# ---------------------------------------------------------------- C (new)
def factor_C(con, theme: Theme, as_of: str) -> tuple[float | None, dict]:
    """Crowding of the theme's primary expression. Independent of TIS."""
    code = theme.price_indicator
    mom = futu_px.return_percentile(con, code, as_of, window=60)
    dist = futu_px.pct_from_52w_high(con, code, as_of)
    rv = futu_px.realized_vol(con, code, as_of, 60)

    parts: dict[str, Any] = {"primary": code, "mom_pct_60d": mom,
                             "dist_52w_high": (round(dist, 4) if dist is not None else None),
                             "realized_vol_60d": (round(rv, 4) if rv is not None else None)}
    if mom is None:
        return None, {**parts, "note": "insufficient history"}

    # Within 3% of the 52-week high is maximally crowded; 20% below is not.
    near_high = 100.0 if dist is None else \
        max(0.0, min(100.0, 100.0 * (1.0 - (abs(dist) - 0.03) / 0.17)))
    # Volatility regime: trailing 20d realised vol ranked against its own year.
    # A quiet tape at a price extreme is the classic crowded setup, so a *low*
    # vol percentile at a high momentum percentile raises crowding.
    vol_pct = futu_px.vol_percentile(con, code, as_of, window=20)
    calm = 50.0 if vol_pct is None else (100.0 - vol_pct)

    score = 0.45 * mom + 0.30 * near_high + 0.25 * calm
    parts.update({"near_high": round(near_high, 1),
                  "vol_pct_20d": (round(vol_pct, 1) if vol_pct is not None else None),
                  "calm_score": round(calm, 1)})
    return round(min(score, 100.0), 1), parts


def crowding_label(c: float | None) -> str:
    if c is None:
        return "NA"
    if c < 35:
        return "不拥挤"
    if c < 60:
        return "中性"
    if c < 80:
        return "偏拥挤"
    return "高度拥挤"


# ---------------------------------------------------------------- assembly
def tactical_impact(d: float | None, a: float | None, b: float | None,
                    n: float | None) -> tuple[float, dict]:
    """0.15D + 0.25A + 0.25B + 0.35N, renormalising over present factors."""
    vals = {"D": d, "A": a, "B": b, "N": n}
    present = {k: v for k, v in vals.items() if v is not None}
    if not present:
        return 0.0, {"missing": list(vals)}
    wsum = sum(config.FACTOR_WEIGHTS[k] for k in present)
    tis = sum(config.FACTOR_WEIGHTS[k] * v for k, v in present.items()) / wsum
    return round(tis, 1), {"missing": [k for k, v in vals.items() if v is None],
                           "weight_sum": round(wsum, 3)}


def theme_tier(tis: float) -> str:
    th = config.THEME_TIER_THRESHOLDS
    if tis >= th["core"]:
        return "core"
    if tis >= th["important"]:
        return "important"
    if tis >= th["watch"]:
        return "watch"
    return "background"


def score_day(con, as_of: date, days: int = config.OBSERVATION_WINDOW_DAYS,
              verbose: bool = True, force: bool = False) -> dict:
    """Score every dictionary theme for `as_of` and persist to `themes`.

    Refuses to overwrite a scoring that a traded batch was generated against,
    unless `force`. Re-scoring the same date later legitimately sees more corpus,
    but the batch's macro narrative was written against the earlier numbers — a
    silent overwrite leaves the daily report arguing from figures that are no
    longer on the page. Pass `force=True` to accept that and re-score anyway; the
    batch keeps its own snapshot either way.
    """
    traded = db.q1(con, "SELECT batch_id FROM batches WHERE as_of=? AND status='traded'",
                   (as_of.isoformat(),))
    if traded and not force:
        existing = db.q1(con, "SELECT COUNT(*) n FROM themes WHERE as_of=?",
                         (as_of.isoformat(),))["n"]
        if existing:
            if verbose:
                print(f"  skip scoring {as_of}: batch {traded['batch_id']} already "
                      f"traded against it (use --force to re-score)")
            return {"as_of": as_of.isoformat(), "skipped": True,
                    "reason": f"batch {traded['batch_id']} already traded",
                    "themes": []}
    ev = collect_evidence(con, as_of, days)
    counting = {tid: len([e for e in items if e["tier"] in config.COUNTING_TIERS])
                for tid, items in ev["per_theme"].items()}
    max_raw = max(counting.values()) if counting else 0

    dormant_ids = set(themes_mod.dormant(con, as_of))

    results: list[dict] = []
    for t in all_themes(as_of):
        items = ev["per_theme"][t.id]
        d, dd = factor_D(ev, t.id, max_raw)
        a, ad = factor_A(con, ev, t.id)
        b, bd = factor_B(ev, t.id)
        n, nd = factor_N(con, ev, t.id, t)

        # Direction registered for M comes from the corpus stance, never from the
        # price series — otherwise M would validate itself.
        net = sum(e["stance"] for e in items)
        direction = "↑" if net >= 0 else "↓"
        m, md = factor_M(con, ev, t, direction)
        c, cd = factor_C(con, t, ev["days"][-1])

        tis, tmeta = tactical_impact(d, a, b, n)
        n_sources = len({e["institution"] for e in items})
        eligible = (n_sources >= config.MIN_THEME_SOURCES
                    and len({e["line"] for e in items}) >= 2
                    and ev["counting_total"] >= config.MIN_VALID_ITEMS)

        # A's intensity term compares today's attention against the theme's own
        # trailing distribution, so a theme registered days ago has no baseline
        # to compare against. Flag it rather than hide it: cold-start themes are
        # a bucket the attribution layer can test separately, and if discovered
        # themes turn out to be systematically worse, that must be measurable.
        age = (as_of - date.fromisoformat(t.registered_d)).days
        cold_start = age < config.BASELINE_WINDOW_DAYS

        results.append({
            "as_of": as_of.isoformat(), "theme_id": t.id, "label": t.label,
            "key_question": t.key_question,
            "tis": tis, "d": d, "a": a, "b": b, "n": n, "m": m, "c": c,
            "tier": theme_tier(tis),
            "n_items": len(items), "n_sources": n_sources,
            "confidence": "ok" if eligible else "low",
            "factors": {"D": dd, "A": ad, "B": bd, "N": nd, "M": md, "C": cd,
                        "TIS": tmeta, "direction": direction,
                        "stage": validation_stage(m), "crowding": crowding_label(c),
                        "eligible": eligible,
                        "origin": t.origin, "registered_d": t.registered_d,
                        "age_days": age, "cold_start": cold_start,
                        "dormant": t.id in dormant_ids,
                        "lexicon_version": lexicon.LEXICON_VERSION,
                        "methodology": config.METHODOLOGY_VERSION},
            "evidence": sorted(items, key=lambda e: (-e["tier"], -e["hits"]))[:60],
        })

    results.sort(key=lambda r: (-r["tis"], -(r["n"] or 0), -(r["a"] or 0)))
    with db.tx(con):
        con.execute("DELETE FROM themes WHERE as_of=?", (as_of.isoformat(),))
        db.upsert_many(con, "themes", results, ["as_of", "theme_id"])

    if verbose:
        print(f"  scored {len(results)} themes on {as_of} "
              f"(pool={ev['counting_total']} counting items, "
              f"{ev['valid_total']} total)")
        print(f"  {'theme':<24}{'TIS':>6}{'D':>6}{'A':>6}{'B':>6}{'N':>6}"
              f"{'M':>6}{'C':>6}  tier / stage / crowd")
        for r in results[:8]:
            f = r["factors"]
            print(f"  {r['label'][:22]:<24}{r['tis']:>6.1f}{r['d']:>6.1f}{r['a']:>6.1f}"
                  f"{r['b']:>6.1f}{r['n']:>6.1f}"
                  f"{(r['m'] if r['m'] is not None else float('nan')):>6.1f}"
                  f"{(r['c'] if r['c'] is not None else float('nan')):>6.1f}"
                  f"  {r['tier']} / {f['stage']} / {f['crowding']}")

    return {"as_of": as_of.isoformat(), "themes": results,
            "pool": {"valid": ev["valid_total"], "counting": ev["counting_total"],
                     "by_day": ev["counting_by_day"]}}


def selected_themes(con, as_of: date, limit: int = config.MAX_REPORT_THEMES) -> list[dict]:
    """Themes that clear the §10.2 admission gate, best first."""
    rows = db.q(con, "SELECT * FROM themes WHERE as_of=? ORDER BY tis DESC",
                (as_of.isoformat(),))
    out = []
    for r in rows:
        f = db.jl(r["factors"], {}) or {}
        if not f.get("eligible"):
            continue
        if (r["tis"] or 0) < config.THEME_TIER_THRESHOLDS["watch"]:
            continue
        out.append({**dict(r), "factors": f, "evidence": db.jl(r["evidence"], [])})
        if len(out) >= limit:
            break
    return out
