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
        "cohorts": _cohorts(con),
    }
    return out


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
            "n_items": r["n_items"], "n_sources": r["n_sources"],
            "debate": (f.get("B") or {}).get("cross_framework_why"),
            "surprise": (f.get("N") or {}).get("surprise_src"),
            "indicator": (f.get("M") or {}).get("primary"),
            "moves": (f.get("M") or {}).get("moves") or {},
            "factors": {k: f.get(k) for k in ("D", "A", "B", "N", "M", "C")},
            "signals": sigs.get(r["theme_id"], []),
            "transmissions": trans.get(r["theme_id"], []),
            "evidence": [{"doc_id": e.get("doc_id"), "line": e.get("line"),
                          "tier": e.get("tier"), "d": e.get("d"),
                          "institution": e.get("institution"),
                          "title": e.get("title"), "stance": e.get("stance"),
                          "depth": e.get("depth"), "fact_type": e.get("fact_type")}
                         for e in ev[:12]],
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
    return {"window": wd, "total": sum(by_day.values()), "by_day": by_day,
            "by_line": dict(sorted(by_line.items(), key=lambda kv: -kv[1])),
            "by_tier": by_tier}


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

    ideas = []
    for r in rows:
        o = om.get(r["idea_uid"], {})
        pos = db.q(con, "SELECT book_id,status,avg_px,qty,cost,opened_d,closed_d,"
                        "close_px,exit_reason,realized FROM positions "
                        "WHERE idea_uid=?", (r["idea_uid"],))
        ideas.append({
            "uid": r["idea_uid"], "id": r["local_id"], "rank": r["rank"],
            "tool": r["tool"], "desc": r["tool_desc"], "kind": r["instrument"],
            "code": r["futu_code"] or r["olive_key"],
            "theme": r["theme"], "theme_id": r["theme_id"],
            "signal_id": r["signal_id"], "asset": r["asset"],
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
