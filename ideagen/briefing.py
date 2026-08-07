"""The daily briefing pack: everything the generator is allowed to see.

This is the contract boundary. The generator (a Claude Code session) receives
exactly this file and must return a batch of 40 ideas conforming to
`prompts/idea_generation.md`. Nothing outside the pack may inform the batch,
which is what makes each day's output reproducible from stored inputs.

The pack is deliberately built *before* generation and hashed, so the audit trail
records which evidence was on the table. Two properties matter:

* Every price in the pack is a close from a session that had already ended.
  `as_of_prices` states the cut-off per market and `sessions_available` proves it.
* Theme scores, evidence and the tradeable catalogue are all as-of, so a batch can
  be regenerated months later against the same inputs and compared.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import config, db, lexicon, scoring, universe
from . import themes as themes_mod
from .sources import futu_px, olive

MAX_EVIDENCE_PER_THEME = 14
MAX_HEADLINES = 60


def build(con, as_of: date, verbose: bool = True,
          price_asof: "datetime | None" = None) -> dict:
    """Build the pack for `as_of`.

    `price_asof` is the wall-clock moment the pack is supposed to have been built
    at. It exists for the backfill: `complete_through()` evaluated at *now* would
    hand a 2026-07-28 pack the 2026-08-06 close, which is exactly the look-ahead
    `validate_batch`'s `ref_price_not_future` check exists to catch. Passing that
    day's own generation time reproduces what a live run would have seen.
    """
    themes = [dict(r) for r in db.q(
        con, "SELECT * FROM themes WHERE as_of=? ORDER BY tis DESC", (as_of.isoformat(),))]
    if not themes:
        raise RuntimeError(f"no theme scores for {as_of}; run `ideagen score` first")

    for t in themes:
        t["factors"] = db.jl(t["factors"], {})
        ev = db.jl(t["evidence"], []) or []
        t["evidence"] = [{k: e.get(k) for k in
                          ("doc_id", "line", "tier", "d", "institution", "title",
                           "stance", "depth", "fact_type")}
                         for e in ev[:MAX_EVIDENCE_PER_THEME]]

    selected = [t for t in themes if (t["factors"] or {}).get("eligible")
                and (t["tis"] or 0) >= config.THEME_TIER_THRESHOLDS["watch"]]
    selected = selected[:config.MAX_REPORT_THEMES]

    universe.hydrate(con)
    px_cut = {m: futu_px.complete_through(m, now=price_asof)
              for m in config.PRICEABLE_MARKETS}
    catalogue = universe.catalogue(con)
    markable = [c for c in catalogue if c["markable"]]

    quotes = {}
    for c in markable:
        if c["kind"] != "listed":
            continue
        inst = universe.BY_KEY.get(c["key"])
        if not inst or not inst.futu_code:
            continue
        cut = px_cut.get(inst.market, as_of.isoformat())
        hit = futu_px.last_close_on_or_before(con, inst.futu_code, cut)
        if not hit:
            continue
        d, px = hit
        quotes[c["key"]] = {
            "code": inst.futu_code, "close": round(px, 4), "close_d": d,
            "ccy": inst.currency, "exposure": inst.exposure,
            "sigma_1m_pct": _r(futu_px.horizon_sigma(con, inst.futu_code, d, 1), 100),
            "sigma_6m_pct": _r(futu_px.horizon_sigma(con, inst.futu_code, d, 6), 100),
            "ret_20d": _r(futu_px.trailing_return(con, inst.futu_code, d, 20)),
            "ret_60d": _r(futu_px.trailing_return(con, inst.futu_code, d, 60)),
            "ret_252d": _r(futu_px.trailing_return(con, inst.futu_code, d, 252)),
            "from_52w_high": _r(futu_px.pct_from_52w_high(con, inst.futu_code, d)),
            "mom_pct_60d": _r(futu_px.return_percentile(con, inst.futu_code, d, 60), 1),
            "vol_pct_20d": _r(futu_px.vol_percentile(con, inst.futu_code, d, 20), 1),
        }

    rf = olive.cash_yield(con, "USD")
    headlines = _headlines(con, as_of)

    # Shelf funds carry their latest observed NAV so the generator can price a
    # fund idea from real data. A fund with no NAV is still listed, but flagged
    # `markable: false` — it cannot enter a book and the generator must not pick
    # it as a priced idea.
    shelf = olive.shelf(con)
    for s in shelf:
        hit = olive.nav_on_or_before(con, s["key"], as_of.isoformat())
        if hit:
            s["nav_d"], s["nav"] = hit
            s["markable"] = True
            quotes[s["key"]] = {
                "code": s["key"], "close": round(hit[1], 6), "close_d": hit[0],
                "ccy": s.get("ccy", "USD"), "exposure": s.get("group") or "fund",
                # A money-market NAV has no meaningful realised vol series yet;
                # these are the shelf's own reported dispersion, floored so the
                # scenario-vol band stays computable.
                "sigma_1m_pct": 0.06, "sigma_6m_pct": 0.15,
                "ret_20d": s.get("ret1m"), "ret_60d": None,
                "ret_252d": s.get("ret1y"), "from_52w_high": 0.0,
                "mom_pct_60d": None, "vol_pct_20d": None,
                "yield7d": s.get("yield7d"), "kind": "fund",
            }
        else:
            s["markable"] = False

    pack: dict[str, Any] = {
        "schema": "ideagen40/briefing/1",
        "as_of": as_of.isoformat(),
        "built_at": config.now_hkt().isoformat(),
        "methodology": config.METHODOLOGY_VERSION,
        "lexicon": lexicon.LEXICON_VERSION,
        "window": [(as_of - timedelta(days=i)).isoformat()
                   for i in range(config.OBSERVATION_WINDOW_DAYS - 1, -1, -1)],
        "as_of_prices": px_cut,
        "sessions_available": {m: futu_px.trading_days(
            con, px_cut[m], 5, "US.SPY" if m == "US" else "HK.02800")
            for m in config.PRICEABLE_MARKETS},
        "corpus": _corpus_stats(con, as_of),
        "themes": themes,
        "selected_theme_ids": [t["theme_id"] for t in selected],
        "theme_dictionary": [{
            "id": t.id, "label": t.label, "key_question": t.key_question,
            "price_indicator": t.price_indicator, "related": list(t.related),
            "exposures": list(t.exposures), "origin": t.origin,
            "registered_d": t.registered_d,
        } for t in lexicon.all_themes(as_of)],
        # Debates the dictionary has no word for. Surfaced in the pack so the
        # generator can register the ones that carry a trade; a fixed theme list
        # discards this slice of the corpus without ever saying so.
        "theme_discovery": themes_mod.candidates(con, as_of),
        "headlines": headlines,
        "universe": {
            "listed_markable": [c for c in markable if c["kind"] == "listed"],
            "funds_on_shelf": shelf[:200],
            "not_markable": [{"key": c["key"], "name": c["name"], "kind": c["kind"]}
                             for c in catalogue if not c["markable"]],
            "exposures": universe.exposures(),
        },
        "quotes": quotes,
        "pricing_rules": {
            "risk_free_annual": rf,
            "risk_free_src": "Olive USD money-market shelf, median 7-day yield",
            "liquidity_premium_annual": config.LIQUIDITY_PREMIUM_ANNUAL,
            "round_trip_cost_pct": {m: round(2 * (v["commission_bps"] + v["slippage_bps"]) / 100, 4)
                                    for m, v in config.COSTS.items()},
            "hurdle_formula": "hurdle_pct = (rf_annual + lp_annual) * months / 12 * 100",
        },
        "constraints": {
            "n_ideas": 40,
            "horizons": list(config.HORIZONS),
            "max_ideas_per_signal": 3,
            "no_duplicate_instrument": True,
            "position_pct_range": [0.25, config.MAX_SINGLE_POSITION * 100],
            "max_theme_exposure_pct": config.MAX_THEME_EXPOSURE * 100,
            "scenario_vol_band": {"k_min": 0.35, "k_max": 2.60,
                                  "note": "abs(R_up)/sigma_h and abs(R_down)/sigma_h "
                                          "should fall in this band"},
            "long_only": True,
            "no_shorting": True,
        },
        "prior_batches": [dict(r) for r in db.q(
            con, "SELECT batch_id, as_of, n_ideas, status FROM batches "
                 "ORDER BY as_of DESC LIMIT 8")],
        "open_positions": _open_positions(con),
    }

    body = json.dumps(pack, ensure_ascii=False, sort_keys=True)
    pack["pack_sha"] = hashlib.sha256(body.encode()).hexdigest()

    path = config.BRIEFINGS / f"briefing_{as_of.isoformat()}.json"
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    db.kv_set(con, f"briefing:{as_of.isoformat()}",
              {"path": str(path), "sha": pack["pack_sha"],
               "themes": len(themes), "selected": len(selected),
               "quotes": len(quotes)})
    if verbose:
        print(f"  briefing {path.name}  sha={pack['pack_sha'][:12]}  "
              f"themes={len(themes)} selected={len(selected)} "
              f"quotes={len(quotes)} headlines={len(headlines)} "
              f"({path.stat().st_size//1024}KB)")
    return pack


def _r(v: float | None, mult: float = 1.0, nd: int = 4) -> float | None:
    return None if v is None else round(v * mult, nd)


def _corpus_stats(con, as_of: date) -> dict:
    wdays = [(as_of - timedelta(days=i)).isoformat()
             for i in range(config.OBSERVATION_WINDOW_DAYS - 1, -1, -1)]
    rows = db.q(con, "SELECT published_d, line, tier, COUNT(*) n FROM documents "
                     "WHERE published_d IN (%s) GROUP BY published_d, line, tier"
                % ",".join("?" * len(wdays)), wdays)
    by_day: dict[str, int] = {d: 0 for d in wdays}
    by_line: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for r in rows:
        by_day[r["published_d"]] = by_day.get(r["published_d"], 0) + r["n"]
        by_line[r["line"]] = by_line.get(r["line"], 0) + r["n"]
        by_tier[f"T{r['tier']}"] = by_tier.get(f"T{r['tier']}", 0) + r["n"]
    return {"total": sum(by_day.values()), "by_day": by_day,
            "by_line": by_line, "by_tier": by_tier}


def _headlines(con, as_of: date) -> list[dict]:
    """Highest-signal items in the window: first-hand and named sell-side first."""
    wdays = [(as_of - timedelta(days=i)).isoformat()
             for i in range(config.OBSERVATION_WINDOW_DAYS - 1, -1, -1)]
    rows = db.q(con,
                "SELECT doc_id,line,tier,title,published_d,institution,"
                "substr(COALESCE(NULLIF(summary,''),body),1,420) txt "
                "FROM documents WHERE published_d IN (%s) "
                "ORDER BY tier ASC, body_chars DESC, published_d DESC LIMIT ?"
                % ",".join("?" * len(wdays)), [*wdays, MAX_HEADLINES])
    return [{"doc_id": r["doc_id"], "line": r["line"], "tier": r["tier"],
             "d": r["published_d"], "title": r["title"],
             "institution": r["institution"], "excerpt": r["txt"]}
            for r in rows]


def _open_positions(con) -> list[dict]:
    rows = db.q(con, "SELECT p.book_id,p.idea_uid,p.code,p.theme,p.horizon,p.grade,"
                     "p.opened_d,p.horizon_end,p.avg_px,i.tool "
                     "FROM positions p JOIN ideas i ON i.idea_uid=p.idea_uid "
                     "WHERE p.status='open' ORDER BY p.book_id,p.code")
    return [dict(r) for r in rows]


def load(as_of: date) -> dict:
    path = config.BRIEFINGS / f"briefing_{as_of.isoformat()}.json"
    return json.loads(path.read_text(encoding="utf-8"))
