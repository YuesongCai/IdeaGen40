"""Date-indexed payload for the dashboard.

The dashboard is two views over a date scrubber, so the page needs every day's
state available at once rather than a single rendered snapshot. This module builds
that structure from the database; `report.py` embeds it and renders client-side.

One deliberate restriction: the evidence list carries titles, institutions, tiers
and doc ids — a citation trail — but not the body excerpts. The published page is
public, and 420-character verbatim quotes from Wisburg's subscription research are
not ours to republish. The excerpts stay in `data/briefings/` locally, where the
generator reads them.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from . import analytics, config, db, ideas as ideas_mod, lexicon, paper
from . import themes as themes_mod
from .sources import futu_px

MAX_EVIDENCE = 40


def build(con) -> dict:
    dates = _dates(con)
    books = list(config.BOOKS)

    out: dict[str, Any] = {
        "meta": {
            "generated_at": config.now_hkt().isoformat(),
            "methodology": config.METHODOLOGY_VERSION,
            "lexicon": lexicon.LEXICON_VERSION,
            "dates": dates,
            "today": dates[-1] if dates else None,
            "books": {b: {"label": config.BOOKS[b]["label"],
                          "desc": config.BOOKS[b]["desc"],
                          "capital": config.BOOKS[b]["capital"]} for b in books},
            "px_through": {m: futu_px.complete_through(m)
                           for m in config.PRICEABLE_MARKETS},
        },
        "days": {d: _day(con, d) for d in dates},
        "curves": _curves(con),
        "positions": _positions(con),
        "attribution": _attribution(con),
        "dictionary": _dictionary(con, dates),
        "cohorts": _cohorts(con),
        "overview": None,   # filled below, needs cohorts
    }
    out["overview"] = _overview(con, out)
    return out


def _overview(con, pl: dict) -> dict:
    """High-level read across every day, for the cockpit view."""
    co = pl["cohorts"]
    scored = [c for c in co.values() if c["sessions"] > 0 and c["excess"] is not None]
    scored.sort(key=lambda c: c["as_of"])
    rets = [c["cum_ret"] for c in scored]
    exs = [c["excess"] for c in scored]
    a = pl["attribution"] or {}

    days = []
    for d, c in sorted(co.items()):
        b = (pl["days"].get(d) or {}).get("batch") or {}
        rep = (pl["days"].get(d) or {}).get("report") or {}
        th = (rep.get("themes") or [])
        acts: dict[str, int] = {}
        for i in b.get("ideas", []):
            acts[i["action"]] = acts.get(i["action"], 0) + 1
        days.append({
            "d": d, "cum_ret": c["cum_ret"], "excess": c["excess"],
            "spy": c["spy"], "sessions": c["sessions"],
            "filled": c["orders"].get("filled") or 0,
            "n_ideas": c["n_ideas"], "generator": c["generator"],
            "max_dd": c["max_dd"], "n_open": c["n_open"],
            "top_theme": (th[0]["label"] if th else None),
            "top_tis": (th[0]["tis"] if th else None),
            "narrative": b.get("narrative"),
            "actions": acts,
            "executable": acts.get("可执行", 0),
            "corpus": (rep.get("corpus") or {}).get("total"),
            "charts": len(rep.get("charts") or []),
        })

    best = max(scored, key=lambda c: c["cum_ret"]) if scored else None
    worst = min(scored, key=lambda c: c["cum_ret"]) if scored else None
    return {
        "n_days": len(co), "n_scored": len(scored),
        "n_ideas": sum(c["n_ideas"] for c in co.values()),
        "n_filled": sum((c["orders"].get("filled") or 0) for c in co.values()),
        "beat": sum(1 for c in scored if c["excess"] > 0),
        "avg_ret": (sum(rets) / len(rets)) if rets else None,
        "avg_excess": (sum(exs) / len(exs)) if exs else None,
        "median_ret": (sorted(rets)[len(rets) // 2] if rets else None),
        "best": ({"d": best["as_of"], "v": best["cum_ret"]} if best else None),
        "worst": ({"d": worst["as_of"], "v": worst["cum_ret"]} if worst else None),
        "worst_dd": (min(c["max_dd"] for c in co.values()) if co else None),
        "idea_equal_weight": a.get("equal_weight_ret"),
        "idea_hit": a.get("hit_rate"),
        "idea_scored": a.get("scored"),
        "beat_bench_rate": a.get("beat_bench_rate"),
        "rank_rho": ((a.get("ranking") or {}).get("ev_c") or {}).get("rho_vs_realized"),
        "skill": (a.get("calibration") or {}).get("skill_central"),
        "corpus_total": sum((x["corpus"] or 0) for x in days),
        "charts_total": sum(x["charts"] for x in days),
        "days": days,
    }


def _dictionary(con, dates: list[str]) -> dict:
    """The theme registry itself, independent of any single day.

    Needed because the as-of rule makes newly registered themes invisible on
    every page date earlier than their registration, which is correct and also
    means the mechanism has no visible surface at all until a later day exists.
    A theme registered on a Saturday shows up nowhere until the next session —
    so the registry, its dates, and the candidates still awaiting judgement are
    surfaced here instead of only appearing implicitly inside a day.
    """
    last = dates[-1] if dates else None
    reg = []
    for t in lexicon.THEMES:
        scored = db.q1(con, "SELECT COUNT(*) n, MAX(as_of) last FROM themes "
                            "WHERE theme_id=?", (t.id,))
        reg.append({
            "id": t.id, "label": t.label, "origin": t.origin,
            "registered_d": t.registered_d,
            "indicator": t.price_indicator, "related": list(t.related),
            "key_question": t.key_question, "n_terms": len(t.terms),
            "days_scored": scored["n"] if scored else 0,
            "last_scored": scored["last"] if scored else None,
            # A theme registered after the newest page date cannot appear on any
            # day shown — this is the field that explains "why do I only see the
            # original topics".
            "pending": bool(last and t.registered_d > last),
        })
    reg.sort(key=lambda r: (r["registered_d"], r["id"]))

    cands = None
    if last:
        try:
            c = themes_mod.candidates(con, date.fromisoformat(last), limit=6)
            cands = {"as_of": c["as_of"], "coverage_pct": c["coverage_pct"],
                     "unmatched": c["unmatched"], "gates": c["gates"],
                     "items": [{"terms": x["terms"][:6], "n_docs": x["n_docs"],
                                "n_institutions": x["n_institutions"],
                                "n_days": x["n_days"], "lift": x["max_lift"]}
                               for x in c["candidates"]]}
        except Exception:
            cands = None
    return {"themes": reg, "n_seed": sum(1 for r in reg if r["origin"] == "seed"),
            "n_discovered": sum(1 for r in reg if r["origin"] == "discovered"),
            "n_pending": sum(1 for r in reg if r["pending"]),
            "newest_page_date": last, "candidates": cands}


def _cohorts(con) -> dict:
    """One independent book per batch: that day's 40 ideas and nothing else.

    This is the vintage read. The two commingled books answer "what would this do
    to the account"; a single blended curve cannot say whether 2026-08-05's ideas
    were any good, because every vintage is mixed into it. After 30 days there are
    30 of these to compare side by side.
    """
    from . import paper

    out: dict[str, Any] = {}
    for bk in paper.cohort_books(con):
        batch_id = bk.split(":", 1)[1]
        b = db.q1(con, "SELECT as_of, n_ideas, generator FROM batches WHERE batch_id=?",
                  (batch_id,))
        if not b:
            continue
        eq = [dict(r) for r in db.q(
            con, "SELECT d,cash,mv,equity,cum_ret,ret_d,drawdown,gross,n_open "
                 "FROM equity WHERE book_id=? ORDER BY d", (bk,))]
        if not eq:
            continue
        first, last = eq[0], eq[-1]
        spy = analytics.benchmark_return(con, config.BENCHMARKS["SPY"],
                                        first["d"], last["d"])
        mdd = min((e["drawdown"] or 0) for e in eq)
        pos = [dict(r) for r in db.q(
            con, "SELECT COUNT(*) n, SUM(status='open') open_n, "
                 "SUM(status='closed') closed_n FROM positions WHERE book_id=?", (bk,))][0]
        ords = db.q1(con, "SELECT COUNT(*) n, SUM(status='filled') filled, "
                          "SUM(status='pending') pending, SUM(status='expired') expired "
                          "FROM orders WHERE book_id=?", (bk,))
        out[b["as_of"]] = {
            "book": bk, "batch_id": batch_id, "as_of": b["as_of"],
            "n_ideas": b["n_ideas"], "generator": b["generator"],
            "sessions": len(eq) - 1,
            "equity": last["equity"], "cum_ret": last["cum_ret"],
            "ret_d": last["ret_d"], "gross": last["gross"],
            "n_open": last["n_open"], "max_dd": mdd,
            "spy": spy,
            "excess": (last["cum_ret"] - spy) if spy is not None else None,
            "positions": dict(pos), "orders": dict(ords),
            "curve": [[e["d"], e["cum_ret"]] for e in eq],
        }
    return out


# ---------------------------------------------------------------- dates
def _dates(con) -> list[str]:
    """Every date the dashboard can show: any day with a scoring or a batch."""
    seen = {r["as_of"] for r in db.q(con, "SELECT DISTINCT as_of FROM themes")}
    seen |= {r["as_of"] for r in db.q(con, "SELECT DISTINCT as_of FROM batches")}
    return sorted(seen)


# ---------------------------------------------------------------- one day
def _day(con, d: str) -> dict:
    b = _batch(con, d)
    return {
        "d": d,
        "report": _report(con, d, frozen=(b or {}).get("themes_at_generation")),
        "batch": b,
        "books": _books_on(con, d),
        "alerts": [dict(r) for r in db.q(
            con, "SELECT level,kind,message,code FROM alerts WHERE d=? "
                 "ORDER BY CASE level WHEN 'action' THEN 0 WHEN 'warn' THEN 1 "
                 "ELSE 2 END", (d,))],
    }


def _report(con, d: str, frozen: list[dict] | None = None) -> dict | None:
    """Theme layer for one day.

    `frozen` is the scoring snapshot taken when that day's batch was generated. It
    wins over the live table, because the narrative above it was written against
    those numbers — a later re-score with more corpus would otherwise leave the
    prose and the table disagreeing.
    """
    if frozen:
        rows = sorted(frozen, key=lambda r: -(r.get("tis") or 0))
        rescored = db.q(con, "SELECT theme_id,tis FROM themes WHERE as_of=?", (d,))
        live = {r["theme_id"]: r["tis"] for r in rescored}
        drift = sum(1 for r in rows
                    if live.get(r["theme_id"]) is not None
                    and abs((live[r["theme_id"]] or 0) - (r.get("tis") or 0)) > 0.5)
    else:
        rows = db.q(con, "SELECT * FROM themes WHERE as_of=? ORDER BY tis DESC", (d,))
        drift = 0
    if not rows:
        return None

    sigs: dict[str, list[dict]] = {}
    for s in db.q(con, "SELECT * FROM signals WHERE as_of=?", (d,)):
        sigs.setdefault(s["theme_id"], []).append(
            {"id": s["signal_id"], "asset": s["asset"], "direction": s["direction"],
             "horizon": s["horizon"], "transmission_id": s["transmission_id"],
             "gate": s["gate"]})
    trans: dict[str, list[dict]] = {}
    for t in db.q(con, "SELECT * FROM transmissions WHERE as_of=?", (d,)):
        trans.setdefault(t["theme_id"], []).append(
            {"id": t["transmission_id"], "label": t["label"]})

    all_charts = _charts(con, d)

    themes = []
    for r in rows:
        f = db.jl(r["factors"], {}) or {}
        ev = db.jl(r["evidence"], []) or []
        themes.append({
            "id": r["theme_id"], "label": r["label"],
            "key_question": r["key_question"],
            "tis": r["tis"], "d": r["d"], "a": r["a"], "b": r["b"], "n": r["n"],
            "m": r["m"], "c": r["c"], "tier": r["tier"],
            "stage": f.get("stage"), "crowd": f.get("crowding"),
            "direction": f.get("direction"),
            "eligible": bool(f.get("eligible")),
            "confidence": r["confidence"],
            # Where the theme came from and how old it is. A discovered theme
            # younger than the A baseline has no own history for the intensity
            # term; a reader comparing it against a seed theme should be told
            # that rather than left to infer it.
            "origin": f.get("origin") or "seed",
            "registered_d": f.get("registered_d"),
            "cold_start": bool(f.get("cold_start")),
            "n_items": r["n_items"], "n_sources": r["n_sources"],
            "debate": (f.get("B") or {}).get("cross_framework_why"),
            "surprise": (f.get("N") or {}).get("surprise_src"),
            "indicator": (f.get("M") or {}).get("primary"),
            "moves": (f.get("M") or {}).get("moves") or {},
            "factors": {k: f.get(k) for k in ("D", "A", "B", "N", "M", "C")},
            "signals": sigs.get(r["theme_id"], []),
            "transmissions": trans.get(r["theme_id"], []),
            # Evidence and charts belong to the theme that they are evidence *for*,
            # not to a table at the bottom of the page. The reasoning trail is
            # assembled here so a theme row can be opened and read on its own.
            "evidence": _theme_evidence(con, ev[:14]),
            "charts": _match_charts(all_charts, r["theme_id"]),
            "trail": _trail(f, r),
        })

    corpus = _corpus(con, d)
    return {
        "frozen_at_generation": bool(frozen),
        "rescore_drift": drift,
        "themes": themes,
        "selected": [t["id"] for t in themes
                     if t["eligible"] and (t["tis"] or 0)
                     >= config.THEME_TIER_THRESHOLDS["watch"]][:config.MAX_REPORT_THEMES],
        "corpus": corpus,
        "evidence": _evidence(con, d),
        "charts": _charts(con, d),
    }


def _theme_evidence(con, ev: list[dict]) -> list[dict]:
    """Evidence rows with their retrieval receipt and verified figures attached."""
    out = []
    for e in ev:
        doc = db.q1(con, "SELECT category,source_id,retrieval,content_hash,body_chars,"
                         "published_at FROM documents WHERE doc_id=?",
                    (e.get("doc_id"),))
        assets = [dict(a) for a in db.q(
            con, "SELECT url,kind,bytes,content_type FROM assets "
                 "WHERE doc_id=? AND reachable=1", (e.get("doc_id"),))] if doc else []
        out.append({
            "doc_id": e.get("doc_id"),
            "line": config.SOURCE_LINES.get(e.get("line"), {}).get("label", e.get("line")),
            "tier": e.get("tier"), "d": e.get("d"),
            "institution": e.get("institution"), "title": e.get("title"),
            "stance": e.get("stance"), "depth": e.get("depth"),
            "fact_type": e.get("fact_type"),
            "retrieval": (doc["retrieval"] if doc else None),
            "hash": ((doc["content_hash"] or "")[:12] if doc else None),
            "chars": (doc["body_chars"] if doc else None),
            "assets": assets,
        })
    return out


def _match_charts(charts: list[dict], theme_id: str) -> list[dict]:
    """Charts whose title or caption matches this theme's frozen dictionary.

    A keyword match, stated as such: the chart library carries no theme tag, so the
    association is inferred from the same term list the factor engine counts on.
    """
    t = lexicon.THEME_BY_ID.get(theme_id)
    if not t:
        return []
    scored = []
    for c in charts:
        text = (c.get("title") or "") + " " + (c.get("caption") or "")
        hits = lexicon.match_theme(text, t)
        if hits >= 1:
            scored.append({**c, "match_terms": hits})
    scored.sort(key=lambda c: -c["match_terms"])
    # Two distinct dictionary terms is a confident match; one is often noise
    # ("欧洲天然气储备" hit 金属与战略补库 on the word 库存 alone). Prefer the
    # confident ones, and only fall back to single-term hits when a theme would
    # otherwise show nothing — flagged as weak so the reader can discount them.
    strong = [c for c in scored if c["match_terms"] >= 2]
    if strong:
        return [{**c, "weak": False} for c in strong[:3]]
    return [{**c, "weak": True} for c in scored[:2]]


def _r1(v) -> str:
    return "—" if v is None else f"{float(v):.1f}"


def _r2(v) -> str:
    return "—" if v is None else f"{float(v):.2f}"


def _pct1(v) -> str:
    return "—" if v is None else f"{float(v)*100:+.1f}%"


def _trail(f: dict, r) -> dict:
    """How each factor got its number — the reasoning chain, not just the score."""
    D, A, B, N = (f.get("D") or {}), (f.get("A") or {}), (f.get("B") or {}), (f.get("N") or {})
    M, C = (f.get("M") or {}), (f.get("C") or {})
    return {
        "D": {"score": r["d"], "why": (
            f"{D.get('raw_items', 0)} 条独立条目 / 窗口计票池 {D.get('pool', 0)} 条"
            f"（{D.get('distinct_institutions', 0)} 家机构、"
            f"{D.get('distinct_lines', 0)} 条来源线）；对数标定到当窗口最响的主题"
            f"（{D.get('max_raw_in_window', 0)} 条）")},
        "A": {"score": r["a"], "why": (
            "三日覆盖率 " + " / ".join(f"{x*100:.2f}%" for x in (A.get("c") or []))
            + f"；重心 {_r2(A.get('centroid'))} → 倾斜项 {_r1(A.get('tilt'))}"
            + (f"；当日热度在自身过去 {A.get('baseline_n')} 天分布的 "
               f"{A.get('intensity')} 百分位" if A.get("intensity") is not None
               else f"；基线不足（{A.get('baseline_n', 0)} 天），强度项 NA，"
                    f"已按剩余权重归一化"))},
        "B": {"score": r["b"], "why": (
            f"方向明确观点 {B.get('n_directional', 0)} 条"
            f"（看多 {B.get('pos', 0)} / 看空 {B.get('neg', 0)}）"
            f"，样本折扣 {B.get('sample_discount', '—')}"
            f"；方向争议 {B.get('directional_debate', '—')}"
            f"；跨框架 {B.get('cross_framework', '—')}——{B.get('cross_framework_why', '')}")},
        "N": {"score": r["n"], "why": (
            f"新事实广度 {N.get('breadth', '—')}"
            f"（{N.get('distinct_pairs', 0)} 组「机构×事实类型」，"
            f"类型 {'/'.join(N.get('fact_types') or []) or '—'}）"
            f"；意外程度 {N.get('surprise', '—')}——{N.get('surprise_src', '')}"
            f"；因果深度 {N.get('depth', '—')}"
            f"（最重要三条 {N.get('depth_top3') or '—'}）")},
        "M": {"score": r["m"], "why": (
            f"预注册指标 {M.get('primary', '—')}；方向命中率 {_r1(M.get('hit_rate'))}"
            f"、幅度百分位 {_r1(M.get('magnitude_pct'))}、广度 {_r1(M.get('breadth'))}"
            f"；窗口涨跌 " + "、".join(
                f"{k} {v*100:+.2f}%" for k, v in (M.get("moves") or {}).items()
                if v is not None))},
        "C": {"score": r["c"], "why": (
            f"{C.get('primary', '—')} 的 60 日动量在 "
            f"{_r1(C.get('mom_pct_60d'))} 百分位"
            f"；距 52 周高点 {_pct1(C.get('dist_52w_high'))}"
            f"；20 日波动百分位 {_r1(C.get('vol_pct_20d'))}"
            f"（安静度 {_r1(C.get('calm_score'))}）")},
    }


def _corpus(con, d: str) -> dict:
    """Corpus statistics for the 3-day window ending on d."""
    from datetime import timedelta

    wd = [(date.fromisoformat(d) - timedelta(days=i)).isoformat()
          for i in range(config.OBSERVATION_WINDOW_DAYS - 1, -1, -1)]
    rows = db.q(con, "SELECT published_d,line,tier,COUNT(*) n FROM documents "
                     "WHERE published_d IN (%s) GROUP BY published_d,line,tier"
                % ",".join("?" * len(wd)), wd)
    by_day: dict[str, int] = {x: 0 for x in wd}
    by_line: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for r in rows:
        by_day[r["published_d"]] = by_day.get(r["published_d"], 0) + r["n"]
        lbl = config.SOURCE_LINES.get(r["line"], {}).get("label", r["line"])
        by_line[lbl] = by_line.get(lbl, 0) + r["n"]
        by_tier[f"T{r['tier']}"] = by_tier.get(f"T{r['tier']}", 0) + r["n"]
    # Dictionary reach: how much of the window any registered theme can name.
    # Shown on the page because it is the one number that makes the theme set's
    # blind spot visible — a fixed list cannot report it by construction.
    try:
        reach = themes_mod.candidates(con, date.fromisoformat(d), limit=6)
    except Exception:
        reach = None
    # Which replay produced this day, and whether its ideas were authored or
    # generated. Recorded because the whole record was rebuilt once, forwards, on
    # 2026-08-08; before that the days had been assembled incrementally and two
    # defects followed (a batch replaced under live positions, and the earliest
    # days scored before `ingest` had deep-fetched some of their full text).
    replay_at, authored = None, False
    kv = db.q1(con, "SELECT k FROM kv WHERE k LIKE 'replay:%' ORDER BY k DESC LIMIT 1")
    if kv:
        replay_at = "2026-08-08"
    b = db.q1(con, "SELECT generator FROM batches WHERE as_of=?", (d,))
    if b:
        authored = not str(b["generator"] or "").startswith("rules:")

    return {"window": wd, "total": sum(by_day.values()), "by_day": by_day,
            "replay": replay_at, "authored": authored,
            "by_line": dict(sorted(by_line.items(), key=lambda kv: -kv[1])),
            "by_tier": by_tier,
            "reach": None if not reach else {
                "registered": reach["registered"],
                "matched": reach["corpus_matched"],
                "items": reach["corpus_total"],
                "pct": reach["coverage_pct"],
                "unmatched": reach["unmatched"],
                "candidates": [{"terms": c["terms"][:6], "n_docs": c["n_docs"],
                                "n_institutions": c["n_institutions"],
                                "n_days": c["n_days"], "lift": c["max_lift"]}
                               for c in reach["candidates"]],
            }}


def _evidence(con, d: str) -> list[dict]:
    """Citation trail for the window.

    Each entry carries the reproducible retrieval call and the content hash rather
    than a permalink: Wisburg is a client-rendered SPA with no per-document
    canonical URL, so a guessed link would be a citation that resolves to nothing.
    Verified asset URLs travel alongside, because those *are* checkable.
    """
    from datetime import timedelta

    wd = [(date.fromisoformat(d) - timedelta(days=i)).isoformat()
          for i in range(config.OBSERVATION_WINDOW_DAYS - 1, -1, -1)]
    rows = db.q(con, "SELECT doc_id,line,category,source_id,tier,title,institution,"
                     "published_at,published_d,ingested_at,content_hash,retrieval,"
                     "body_chars FROM documents WHERE published_d IN (%s) "
                     "AND line<>'images' ORDER BY tier ASC, body_chars DESC LIMIT ?"
                % ",".join("?" * len(wd)), [*wd, MAX_EVIDENCE])
    out = []
    for r in rows:
        assets = [dict(a) for a in db.q(
            con, "SELECT url,kind,reachable,bytes,content_type FROM assets "
                 "WHERE doc_id=? AND reachable=1", (r["doc_id"],))]
        out.append({
            "doc_id": r["doc_id"],
            "line": config.SOURCE_LINES.get(r["line"], {}).get("label", r["line"]),
            "tier": r["tier"], "category": r["category"],
            "source_id": r["source_id"], "d": r["published_d"],
            "published_at": r["published_at"], "ingested_at": r["ingested_at"],
            "title": r["title"], "institution": r["institution"],
            "retrieval": r["retrieval"], "hash": (r["content_hash"] or "")[:12],
            "chars": r["body_chars"], "assets": assets,
        })
    return out


def _charts(con, d: str) -> list[dict]:
    """Wisburg's own charts for the window, with their published interpretation.

    框架 §11.1 requires a report to embed four original Wisburg charts. These are
    those charts: real CDN URLs, HEAD-verified, each with the platform's written
    reading of what the chart shows.
    """
    from datetime import timedelta

    wd = [(date.fromisoformat(d) - timedelta(days=i)).isoformat()
          for i in range(config.OBSERVATION_WINDOW_DAYS - 1, -1, -1)]
    rows = db.q(con,
                "SELECT a.url,a.caption,a.title,a.published_d,a.reachable,a.bytes,"
                "a.content_type,a.checked_at,a.doc_id,d.retrieval,d.published_at "
                "FROM assets a JOIN documents d ON d.doc_id=a.doc_id "
                "WHERE a.kind='chart' AND a.published_d IN (%s) AND a.reachable=1 "
                "ORDER BY a.published_d DESC" % ",".join("?" * len(wd)), wd)
    return [dict(r) for r in rows]


def _batch(con, d: str) -> dict | None:
    b = db.q1(con, "SELECT * FROM batches WHERE as_of=? ORDER BY generated_at DESC "
                   "LIMIT 1", (d,))
    if not b:
        return None
    val = db.jl(b["validation"], {}) or {}
    rows = ideas_mod.load_batch(con, b["batch_id"])
    out = db.q(con, "SELECT * FROM outcomes WHERE idea_uid IN (%s)"
               % ",".join("?" * max(len(rows), 1)),
               [r["idea_uid"] for r in rows] or [""])
    om = {r["idea_uid"]: dict(r) for r in out}

    # the generator's own narrative, stored inside the first idea's raw payload
    narrative = note = None
    if rows:
        raw = rows[0].get("raw") or {}
        narrative = (raw.get("_batch_narrative")
                     or (raw.get("_batch") or {}).get("macro_narrative"))
    meta = db.kv_get(con, f"batch_meta:{b['batch_id']}", {}) or {}
    narrative = narrative or meta.get("macro_narrative")
    note = meta.get("note") or b["note"]

    # classification for the ideas table: the Theme Map's information belongs in
    # the row that expresses it, not in a separate tree above it
    sig_map = {s["signal_id"]: dict(s) for s in db.q(
        con, "SELECT signal_id,theme_id,transmission_id,asset,direction,horizon,gate "
             "FROM signals WHERE as_of=?", (d,))}
    tr_map = {t["transmission_id"]: t["label"] for t in db.q(
        con, "SELECT transmission_id,label FROM transmissions WHERE as_of=?", (d,))}

    ideas = []
    for r in rows:
        o = om.get(r["idea_uid"], {})
        sg = sig_map.get(r["signal_id"] or "", {})
        pos = db.q(con, "SELECT book_id,status,avg_px,qty,cost,opened_d,closed_d,"
                        "close_px,exit_reason,realized FROM positions "
                        "WHERE idea_uid=?", (r["idea_uid"],))
        ideas.append({
            "uid": r["idea_uid"], "id": r["local_id"], "rank": r["rank"],
            "tool": r["tool"], "desc": r["tool_desc"], "kind": r["instrument"],
            "code": r["futu_code"] or r["olive_key"],
            "theme": r["theme"], "theme_id": r["theme_id"],
            "signal_id": r["signal_id"], "asset": r["asset"],
            "signal_label": (f"{sg.get('asset')} {sg.get('direction','↑')}｜{sg.get('horizon')}"
                             if sg else None),
            "transmission": tr_map.get(sg.get("transmission_id") or ""),
            "gate": sg.get("gate"),
            "horizon": r["horizon"], "horizon_months": r["horizon_months"],
            "horizon_end": ideas_mod.horizon_end(
                date.fromisoformat(r["as_of"]), r["horizon_months"]).isoformat(),
            "action": r["action"], "grade": r["grade"], "grade_rel": r["grade_rel"],
            "or_c": r["or_c"], "or_k": r["or_k"], "ev_c": r["ev_c"],
            "hurdle": r["hurdle"], "vol_check": r["vol_check"],
            "sigma_h": r["sigma_h"],
            "ref_price": r["ref_price"], "ref_price_d": r["ref_price_d"],
            "entry_lo": r["entry_lo"], "entry_hi": r["entry_hi"],
            "entry_break": r["entry_break"],
            "take_lo": r["take_lo"], "stop_px": r["stop_px"],
            "pos_init": r["pos_init"], "pos_max": r["pos_max"],
            "central": {"p": r["central_p"], "r": r["central_r"]},
            "conservative": {"p": r["conserv_p"], "r": r["conserv_r"]},
            "view": r["view"], "thesis": r["thesis"], "fit": r["fit"],
            "risk": r["risk"], "sources": r["sources"],
            "sources_resolved": _resolve_sources(con, r["sources"]),
            "realized": o.get("realized"), "excess": o.get("excess"),
            "sessions_held": o.get("sessions_held"),
            "exit_reason": o.get("exit_reason"),
            "fills": [dict(p) for p in pos],
        })

    return {
        "themes_at_generation": meta.get("themes_at_generation"),
        "batch_id": b["batch_id"], "generated_at": b["generated_at"],
        "generator": b["generator"], "n": b["n_ideas"], "status": b["status"],
        "output_sha": (b["output_sha"] or "")[:12],
        "narrative": narrative, "note": note,
        "validation": {"pass": val.get("pass"), "errors": val.get("n_errors"),
                       "warnings": val.get("n_warnings"),
                       "failed": [{"check": c["check"], "severity": c["severity"],
                                   "detail": c["detail"]}
                                  for c in val.get("checks", []) if not c["ok"]],
                       "summary": val.get("summary")},
        "ideas": ideas,
    }


def _resolve_sources(con, sources) -> list[dict]:
    """Turn an idea's doc_ids into readable, verifiable references.

    The reference belongs on the idea that cites it. A citation the reader cannot
    open is not a citation, so each one carries its title, tier, institution, the
    API call that reproduces it, and any verified figures.
    """
    out = []
    for sid in (sources or []):
        r = db.q1(con, "SELECT doc_id,line,tier,title,institution,published_at,"
                       "retrieval,content_hash FROM documents WHERE doc_id=?",
                  (str(sid),))
        if not r:
            out.append({"doc_id": str(sid), "resolved": False,
                        "title": str(sid), "note": "散文式归属，无法解析到语料"})
            continue
        assets = [dict(a) for a in db.q(
            con, "SELECT url,kind FROM assets WHERE doc_id=? AND reachable=1",
            (r["doc_id"],))]
        out.append({
            "doc_id": r["doc_id"], "resolved": True,
            "line": config.SOURCE_LINES.get(r["line"], {}).get("label", r["line"]),
            "tier": r["tier"], "title": r["title"],
            "institution": r["institution"],
            "published_at": r["published_at"], "retrieval": r["retrieval"],
            "hash": (r["content_hash"] or "")[:12], "assets": assets,
        })
    return out


def _books_on(con, d: str) -> dict:
    out = {}
    for b in config.BOOKS:
        e = db.q1(con, "SELECT * FROM equity WHERE book_id=? AND d<=? "
                       "ORDER BY d DESC LIMIT 1", (b, d))
        if not e:
            continue
        # `drawdown` on the latest row is the drawdown *as of that day*. The card
        # wants the worst it ever got to, over the window shown.
        mdd = db.q1(con, "SELECT MIN(drawdown) m FROM equity WHERE book_id=? AND d<=?",
                    (b, d))["m"]
        spy = analytics.benchmark_return(
            con, config.BENCHMARKS["SPY"],
            db.q1(con, "SELECT MIN(d) m FROM equity WHERE book_id=?", (b,))["m"], e["d"])
        out[b] = {"d": e["d"], "equity": e["equity"], "cash": e["cash"],
                  "mv": e["mv"], "cum_ret": e["cum_ret"], "ret_d": e["ret_d"],
                  "drawdown": e["drawdown"], "max_dd": mdd, "gross": e["gross"],
                  "n_open": e["n_open"], "spy": spy,
                  "excess": (e["cum_ret"] - spy) if spy is not None else None}
    return out


# ---------------------------------------------------------------- curves
def _curves(con) -> dict:
    out: dict[str, list] = {}
    anchor = None
    for b in config.BOOKS:
        rows = db.q(con, "SELECT d,cum_ret,equity,gross,n_open,cash,drawdown "
                         "FROM equity WHERE book_id=? ORDER BY d", (b,))
        out[b] = [[r["d"], r["cum_ret"], r["equity"], r["gross"], r["n_open"]]
                  for r in rows]
        if rows:
            anchor = rows[0]["d"] if anchor is None else min(anchor, rows[0]["d"])
    if anchor:
        for name, code in (("SPY", config.BENCHMARKS["SPY"]),
                           ("ACWI", config.BENCHMARKS["ACWI"])):
            base = futu_px.last_close_on_or_before(con, code, anchor)
            if not base:
                continue
            out[name] = [[x["d"], x["close"] / base[1] - 1]
                         for x in futu_px.bars(con, code, anchor)]
    return out


# ---------------------------------------------------------------- positions
def _positions(con) -> list[dict]:
    rows = db.q(con,
                "SELECT p.*, i.tool, i.tool_desc, i.horizon AS hz, i.or_c, i.or_k, "
                "i.ev_c, i.grade_rel, i.vol_check, i.view, i.action, i.as_of, "
                "i.pos_init, i.ref_price, i.instrument "
                "FROM positions p JOIN ideas i ON i.idea_uid=p.idea_uid "
                "ORDER BY p.book_id, p.opened_d, p.code")
    out = []
    for r in rows:
        p = dict(r)
        m = db.q1(con, "SELECT d,px,mv,upnl,upnl_pct FROM mtm WHERE book_id=? "
                       "AND pos_id=? ORDER BY d DESC LIMIT 1",
                  (p["book_id"], p["pos_id"]))
        live = (p["realized"] / p["cost"] if p["status"] == "closed" and p["cost"]
                else (m["upnl_pct"] if m else None))
        out.append({
            "pos_id": p["pos_id"], "book": p["book_id"], "idea_uid": p["idea_uid"],
            "tool": p["tool"], "desc": p["tool_desc"], "code": p["code"],
            "kind": p["kind"], "theme": p["theme"], "horizon": p["hz"],
            "grade": p["grade"], "grade_rel": p["grade_rel"], "action": p["action"],
            "as_of": p["as_of"], "opened_d": p["opened_d"],
            "horizon_end": p["horizon_end"],
            "qty": p["qty"], "avg_px": p["avg_px"], "cost": p["cost"],
            "px": (p["close_px"] if p["status"] == "closed"
                   else (m["px"] if m else None)),
            "mv": (m["mv"] if m and p["status"] == "open" else None),
            "pnl_pct": live,
            "pnl_usd": (p["realized"] if p["status"] == "closed"
                        else (m["upnl"] if m else None)),
            "stop_px": p["stop_px"], "take_px": p["take_px"],
            "status": p["status"], "closed_d": p["closed_d"],
            "exit_reason": p["exit_reason"],
            "peak_px": p["peak_px"], "trough_px": p["trough_px"],
            "or_c": p["or_c"], "or_k": p["or_k"], "vol_check": p["vol_check"],
            "view": p["view"],
        })
    # pending orders sit alongside positions in the portfolio view: an idea that
    # never filled is part of the story, not an absence of one.
    for r in db.q(con, "SELECT o.*, i.tool, i.theme, i.horizon AS hz, i.grade, "
                       "i.action, i.stop_px, i.take_lo, i.ref_price "
                       "FROM orders o JOIN ideas i ON i.idea_uid=o.idea_uid "
                       "WHERE o.status IN ('pending','expired')"):
        o = dict(r)
        out.append({
            "pos_id": o["order_id"], "book": o["book_id"],
            "idea_uid": o["idea_uid"], "tool": o["tool"], "code": o["code"],
            "kind": "order", "theme": o["theme"], "horizon": o["hz"],
            "grade": o["grade"], "action": o["action"], "as_of": o["as_of"],
            "opened_d": None, "status": o["status"],
            "band_lo": o["band_lo"], "band_hi": o["band_hi"],
            "trigger": o["trigger"], "notional": o["notional"],
            "placed_d": o["placed_d"], "expire_d": o["expire_d"],
            "order_kind": o["kind"], "ref_price": o["ref_price"],
            "stop_px": o["stop_px"], "take_px": o["take_lo"],
        })
    return out


def _attribution(con) -> dict:
    ir = analytics.idea_report(con)
    return {k: ir.get(k) for k in
            ("n", "scored", "too_fresh", "unmarkable", "equal_weight_ret",
             "median_ret", "hit_rate", "excess_mean", "beat_bench_rate",
             "ranking", "calibration", "buckets")}
