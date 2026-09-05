"""Idea schema, scenario mathematics, grading and the pre-publication gate.

The scenario arithmetic is v0.3 §5 unchanged, so every number the historical pack
produced can be reproduced bit-for-bit:

    Expected Return          = Σ P_s · R_s
    Expected Gain Above H    = Σ P_s · max(R_s − H, 0)
    Expected Loss Below H    = Σ P_s · max(H − R_s, 0)
    Opportunity Ratio        = Gain / Loss

What v0.4 adds around it:

1. Data-driven hurdle. v0.3 fixed the risk-free by hand. Here it is the median
   7-day yield of the USD money-market shelf the account can actually buy, which
   is the true opportunity cost of not trading, plus a liquidity premium keyed to
   the vehicle. Both components are stored per idea.  -> `hurdle_for`

2. A falsifiability check on the scenarios. v0.3 lets probabilities and returns
   be pure `research_judgment`, which makes an idea unscoreable: any outcome is
   consistent with any forecast. v0.4 requires the up/down legs to sit inside a
   band set by the instrument's own realised horizon volatility and flags
   `narrow` / `wide` when they do not. Nothing is rejected for it — the flag is
   carried into the outcome record so that after 30 days one can ask whether
   wide-scenario ideas were actually better.  -> `vol_sanity`

3. A cross-sectional grade beside the absolute one. v0.3 grades on OR ≥ 1
   against a hurdle whose scale it also sets, so a hurdle mis-scaling silently
   moves every grade at once (the 2026-07-28 diagnostic makes the same point
   about the 75/60/45 thresholds). The relative grade is the OR quartile within
   the same day's batch and cannot drift.  -> `grade_batch`

4. Costs. v0.3 models none. Round-trip cost is subtracted from every scenario
   before the odds are computed, so an idea whose edge is thinner than its
   spread stops grading as attractive.  -> `net_scenarios`

5. Look-ahead control. `ref_price_d` must be a session that closed at or before
   the generation timestamp, and the fill engine only ever looks at bars strictly
   after it.  -> `validate_batch` check `ref_price_not_future`
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics as st
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from . import config, db, macro, universe
from .sources import futu_px, olive

SCENARIOS = ("up", "base", "down")

# Scenario legs are expected to sit inside these multiples of the instrument's
# own realised horizon sigma. Outside the band the idea is flagged, not dropped.
VOL_BAND = {"up": (0.35, 2.60), "down": (0.35, 2.60)}


# ---------------------------------------------------------------- hurdle
def hurdle_for(con, vehicle: str, horizon_months: int,
               currency: str = "USD") -> tuple[float, dict]:
    """Holding-period hurdle in percent, plus its decomposition.

    Returns percent (not fraction) to match the pack's `hurdle` field, where
    e.g. 0.31 means 0.31%.
    """
    rf_annual = olive.cash_yield(con, currency)
    rf_src = f"olive MM shelf median 7d yield ({currency})"
    if rf_annual is None:
        rf_annual = config.RISK_FREE_ANNUAL
        rf_src = "config.RISK_FREE_ANNUAL fallback"
    lp_annual = config.LIQUIDITY_PREMIUM_ANNUAL.get(
        vehicle, config.DEFAULT_LIQUIDITY_PREMIUM)

    # v0.3 §3 approximation, kept for comparability.
    rf_h = rf_annual * horizon_months / 12.0
    lp_h = lp_annual * horizon_months / 12.0
    h = (rf_h + lp_h) * 100.0
    return round(h, 4), {
        "rf_annual": round(rf_annual, 6), "rf_src": rf_src,
        "lp_annual": round(lp_annual, 6), "vehicle": vehicle,
        "rf_holding_pct": round(rf_h * 100, 4),
        "lp_holding_pct": round(lp_h * 100, 4),
        "horizon_months": horizon_months,
    }


# ---------------------------------------------------------------- costs
def round_trip_cost_pct(market: str, kind: str) -> float:
    spec = config.COSTS.get("FUND" if kind != "listed" else market, config.COSTS["US"])
    one_way = (spec.get("commission_bps", 0) + spec.get("slippage_bps", 0)) / 100.0
    return round(2 * one_way, 4)      # percent, both legs


def net_scenarios(returns: Sequence[float], market: str, kind: str) -> list[float]:
    c = round_trip_cost_pct(market, kind)
    return [round(r - c, 6) for r in returns]


# ---------------------------------------------------------------- odds
def odds(p: Sequence[float], r: Sequence[float], hurdle: float) -> dict:
    """v0.3 §5, verbatim. `p` in percent, `r` and `hurdle` in percent."""
    if len(p) != 3 or len(r) != 3:
        raise ValueError("scenarios must be (up, base, down)")
    w = [pi / 100.0 for pi in p]
    ev = sum(wi * ri for wi, ri in zip(w, r))
    gain = sum(wi * max(ri - hurdle, 0.0) for wi, ri in zip(w, r))
    loss = sum(wi * max(hurdle - ri, 0.0) for wi, ri in zip(w, r))
    ratio = (gain / loss) if loss > 0 else (math.inf if gain > 0 else 0.0)
    return {"ev": round(ev, 6), "gain": round(gain, 6), "loss": round(loss, 6),
            "or": (round(ratio, 4) if math.isfinite(ratio) else None),
            "or_inf": not math.isfinite(ratio)}


# ---------------------------------------------------------------- grading
def grade_absolute(or_central: float | None, or_conservative: float | None,
                   or_c_inf: bool = False, or_k_inf: bool = False) -> tuple[str, str]:
    """v0.3 rule set: conservative odds carry the grade, central is the fallback."""
    ok = math.inf if or_k_inf else (or_conservative if or_conservative is not None else 0.0)
    oc = math.inf if or_c_inf else (or_central if or_central is not None else 0.0)
    if ok >= 1.5:
        return "S", "保守赔率 ≥ 1.5"
    if ok >= 1.0:
        return "A", "保守赔率 ≥ 1.0"
    if oc >= 1.0:
        return "B", "中心赔率 ≥ 1.0，保守 < 1.0"
    return "C", "中心赔率 < 1.0"


def grade_batch(rows: list[dict]) -> None:
    """Attach the cross-sectional grade (`grade_rel`) in place.

    Quartiles of conservative OR within the same day's batch. Immune to hurdle
    mis-scaling, which shifts every idea's absolute grade together.
    """
    vals = [(i, (math.inf if r.get("or_k_inf") else (r.get("or_k") or 0.0)))
            for i, r in enumerate(rows)]
    finite = sorted([v for _, v in vals if math.isfinite(v)])
    if not finite:
        for r in rows:
            r["grade_rel"] = "Q?"
        return
    q1, q2, q3 = (st.quantiles(finite, n=4) if len(finite) >= 4
                  else (finite[0], st.median(finite), finite[-1]))
    for i, v in vals:
        if not math.isfinite(v) or v >= q3:
            rows[i]["grade_rel"] = "Q1"        # best quartile
        elif v >= q2:
            rows[i]["grade_rel"] = "Q2"
        elif v >= q1:
            rows[i]["grade_rel"] = "Q3"
        else:
            rows[i]["grade_rel"] = "Q4"


# ---------------------------------------------------------------- vol sanity
def vol_sanity(up: float, down: float, sigma_h_pct: float | None) -> tuple[str, dict]:
    """Are the scenario legs plausible against the instrument's own volatility?"""
    if not sigma_h_pct or sigma_h_pct <= 0:
        return "na", {"sigma_h_pct": None, "note": "no realised vol available"}
    ku = abs(up) / sigma_h_pct
    kd = abs(down) / sigma_h_pct
    lo_u, hi_u = VOL_BAND["up"]
    lo_d, hi_d = VOL_BAND["down"]
    flags = []
    if ku < lo_u or kd < lo_d:
        flags.append("narrow")
    if ku > hi_u or kd > hi_d:
        flags.append("wide")
    verdict = "ok" if not flags else "/".join(flags)
    return verdict, {"sigma_h_pct": round(sigma_h_pct, 3),
                     "k_up": round(ku, 2), "k_down": round(kd, 2),
                     "band": VOL_BAND}


# ---------------------------------------------------------------- compute
def compute(con, raw: dict, as_of: date, batch_id: str) -> dict:
    """Turn one generator idea into a fully-derived, storable row."""
    inst = universe.resolve(raw.get("instrument_key") or raw.get("tool") or "")
    kind = inst.kind if inst else (raw.get("instrument") or "monitor")
    market = inst.market if inst else "US"
    futu_code = inst.futu_code if inst and inst.kind == "listed" else None
    olive_key = inst.olive_key if inst and inst.kind != "listed" else raw.get("olive_key")
    vehicle = raw.get("vehicle") or (inst.vehicle if inst else "ETF")

    horizon = raw["horizon"]
    hm = config.HORIZONS[horizon]

    hurdle, hmeta = hurdle_for(con, vehicle, hm,
                               currency=(inst.currency if inst else "USD"))
    if raw.get("hurdle") is not None:            # generator may pin it explicitly
        hurdle = float(raw["hurdle"])
        hmeta["overridden_by_generator"] = True

    cp = [float(x) for x in raw["central"]["p"]]
    cr = [float(x) for x in raw["central"]["r"]]
    kp = [float(x) for x in raw["conservative"]["p"]]
    kr = [float(x) for x in raw["conservative"]["r"]]

    cr_net = net_scenarios(cr, market, kind)
    kr_net = net_scenarios(kr, market, kind)
    oc = odds(cp, cr_net, hurdle)
    ok = odds(kp, kr_net, hurdle)

    ref_px = raw.get("ref_price")
    ref_d = raw.get("ref_price_d")
    if futu_code and (ref_px is None or ref_d is None):
        hit = futu_px.last_close_on_or_before(con, futu_code, as_of.isoformat())
        if hit:
            ref_d, ref_px = hit[0], hit[1]

    # The band's sigma. Realised vol is what this has always used — `db.py`
    # says so in the column comment — and the band is where the money went
    # missing: 264 of 383 orders expired unfilled, which is a statement about
    # width, not about direction. `macro.band_sigma_pct` records what the
    # market's own implied number says either way, and only widens the band when
    # `IDEAGEN_SIGMA_IMPLIED` is set, because a change to the band changes fills
    # and therefore is not comparable with the periods already booked.
    sigma_h = None
    sigma_meta: dict[str, Any] = {"note": "无价格或无参考日，未计算"}
    if futu_code and ref_d:
        s = futu_px.horizon_sigma(con, futu_code, ref_d, hm)
        realised_pct = (s * 100.0) if s is not None else None
        sigma_h, sigma_meta = macro.band_sigma_pct(
            con, futu_code, ref_d, hm, realised_pct)
    vcheck, vmeta = vol_sanity(cr[0], cr[2], sigma_h)

    grade, rule = grade_absolute(oc["or"], ok["or"], oc["or_inf"], ok["or_inf"])

    row = {
        "idea_uid": f"{batch_id}#{int(raw['id'])}",
        "batch_id": batch_id, "as_of": as_of.isoformat(),
        "local_id": int(raw["id"]), "rank": raw.get("rank"),
        "tool": raw.get("tool") or (inst.key if inst else "?"),
        "tool_desc": raw.get("tool_desc") or (inst.name if inst else None),
        "vehicle": vehicle,
        "theme": raw.get("theme"), "theme_id": raw.get("theme_id"),
        "signal_id": raw.get("signal_id"),
        "asset": raw.get("asset") or (inst.exposure if inst else None),
        "direction": raw.get("direction") or "↑",
        "horizon": horizon, "horizon_months": hm,
        "action": raw.get("action"),
        "instrument": kind, "futu_code": futu_code, "olive_key": olive_key,
        "ref_price": ref_px, "ref_price_d": ref_d,
        "entry_lo": raw.get("entry_lo"), "entry_hi": raw.get("entry_hi"),
        "entry_break": raw.get("entry_break"),
        "take_lo": raw.get("take_lo"), "take_hi": raw.get("take_hi"),
        "stop_px": raw.get("stop_px"),
        "entry_src": raw.get("entry_src"), "take_src": raw.get("take_src"),
        "stop_src": raw.get("stop_src"),
        "hurdle": hurdle,
        "hurdle_rf": hmeta["rf_holding_pct"], "hurdle_lp": hmeta["lp_holding_pct"],
        "central_p": cp, "central_r": cr,
        "conserv_p": kp, "conserv_r": kr,
        "ev_c": oc["ev"], "gain_c": oc["gain"], "loss_c": oc["loss"], "or_c": oc["or"],
        "ev_k": ok["ev"], "gain_k": ok["gain"], "loss_k": ok["loss"], "or_k": ok["or"],
        "sigma_h": sigma_h, "vol_check": vcheck,
        "grade": grade, "grade_rule": rule, "grade_rel": None,
        "pos_init": raw.get("pos_init"), "pos_max": raw.get("pos_max"),
        "view": raw.get("view"), "thesis": raw.get("thesis"), "fit": raw.get("fit"),
        "risk": raw.get("risk"), "role": raw.get("role"),
        "sources": raw.get("sources") or [],
        "raw": {**raw, "_derived": {"hurdle": hmeta, "vol": vmeta,
                                    "sigma": sigma_meta,
                                    "cost_pct": round_trip_cost_pct(market, kind),
                                    "central_net": cr_net, "conservative_net": kr_net}},
    }
    row["or_c_inf"] = oc["or_inf"]
    row["or_k_inf"] = ok["or_inf"]
    return row


#: Everything that hangs off an idea. Replacing a batch must remove all of it.
_IDEA_DEPENDENTS = ("outcomes", "alerts", "trades", "orders", "positions")


def purge_batch(con, batch_id: str) -> dict[str, int]:
    """Delete a batch's ideas *and* everything that references them.

    `idea_uid` is `<batch_id>#<local_id>`, so re-importing a batch rebinds every
    uid to whatever instrument now sits at that local id. Deleting only the
    ideas therefore leaves live positions pointing at uids that have silently
    changed meaning — and nothing downstream notices, because the join on
    `idea_uid` still succeeds.

    That is not hypothetical. Restoring the authored 2026-07-27 pack over a
    backfill-generated batch of the same id left 58 positions across three books
    attached to the wrong instruments: `B20260727#26` held US.URA at 40.33 while
    its idea had become US.DLR, so `settle` marked a $40 entry against a $192
    close and booked **+377%**. That one batch dragged the published idea-level
    equal-weight return from +0.91% to +5.70%.
    """
    n = {}
    for t in _IDEA_DEPENDENTS:
        cur = con.execute(
            f"DELETE FROM {t} WHERE idea_uid IN "
            f"(SELECT idea_uid FROM ideas WHERE batch_id=?)", (batch_id,))
        n[t] = cur.rowcount
    # mtm keys on pos_id, which the positions delete above has already orphaned.
    n["mtm"] = con.execute(
        "DELETE FROM mtm WHERE pos_id NOT IN (SELECT pos_id FROM positions)").rowcount
    n["ideas"] = con.execute("DELETE FROM ideas WHERE batch_id=?",
                             (batch_id,)).rowcount
    return n


def instrument_mismatches(con) -> list[dict]:
    """Positions whose instrument disagrees with the idea that created them.

    This must always be empty. It is the assertion that would have caught the
    2026-07-27 corruption on the day it was introduced instead of ten days
    later: a join on `idea_uid` succeeds even when the uid has been rebound to a
    different instrument, so nothing else in the pipeline can notice.
    """
    # Which column holds the instrument depends on its kind: a listed position
    # carries the Futu code, a fund position carries the Olive key. Comparing
    # every position against `futu_code` flags all 19 fund ideas as corrupt,
    # because their `futu_code` is NULL by design — a false positive that would
    # block `settle` on any batch holding a fund.
    return [dict(r) for r in db.q(con, """
        SELECT p.book_id, p.idea_uid, p.code AS position_code,
               COALESCE(i.futu_code, i.olive_key) AS idea_code, i.batch_id
        FROM positions p JOIN ideas i ON i.idea_uid = p.idea_uid
        WHERE COALESCE(p.code,'') <> COALESCE(i.futu_code, i.olive_key, '')
        ORDER BY i.batch_id, p.book_id, p.idea_uid""")]


def build_batch(con, payload: dict, as_of: date, generator: str = "claude-code",
                batch_id: str | None = None,
                generated_at: str | None = None) -> tuple[str, list[dict], dict]:
    """Compute every idea in a generator payload and persist as a draft batch."""
    ideas_in = payload.get("ideas") or payload.get("updatedIdeas") or payload
    if not isinstance(ideas_in, list):
        raise ValueError("payload must contain a list under `ideas`")
    universe.hydrate(con)          # Olive shelf keys must resolve, not degrade

    batch_id = batch_id or f"B{as_of.isoformat().replace('-', '')}"
    rows = [compute(con, r, as_of, batch_id) for r in ideas_in]
    grade_batch(rows)
    rows.sort(key=lambda r: (-(r["or_k"] if r["or_k"] is not None else 9e9),
                             -(r["or_c"] or 0)))
    for i, r in enumerate(rows, 1):
        r.setdefault("rank", None)
        if r["rank"] is None:
            r["rank"] = i

    report = validate_batch(con, rows, as_of, payload)
    out_sha = hashlib.sha256(
        json.dumps(ideas_in, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    store = [{k: v for k, v in r.items() if k not in ("or_c_inf", "or_k_inf")}
             for r in rows]
    with db.tx(con):
        purge_batch(con, batch_id)
        db.upsert(con, "batches", {
            "batch_id": batch_id, "as_of": as_of.isoformat(),
            "generated_at": generated_at or config.now_hkt().isoformat(),
            "generator": generator, "methodology": config.METHODOLOGY_VERSION,
            "n_ideas": len(rows),
            "prompt_sha": payload.get("prompt_sha"), "output_sha": out_sha,
            "validation": report,
            "status": "validated" if report["pass"] else "draft",
            "note": payload.get("note"),
        }, ["batch_id"])
        db.upsert_many(con, "ideas", store, ["idea_uid"])

        # The batch's own transmission / signal registry. This lives here rather
        # than in the CLI because every path that creates a batch needs it — the
        # backfill went through `build_batch` directly, so its days had no signal
        # rows and the Theme Map rendered as a flat list of themes with nothing
        # underneath them.
        from . import lexicon as _lex

        db.upsert_many(con, "transmissions", [
            {"as_of": as_of.isoformat(), "transmission_id": t["id"],
             "theme_id": t.get("theme_id"), "label": t.get("label")}
            for t in (payload.get("transmissions") or []) if t.get("id")],
            ["as_of", "transmission_id"])
        db.upsert_many(con, "signals", [
            {"as_of": as_of.isoformat(), "signal_id": g["id"],
             "theme_id": g.get("theme_id"),
             "transmission_id": g.get("transmission_id"),
             "asset": g.get("asset"), "direction": g.get("direction", "↑"),
             "horizon": g.get("horizon", "1个月"), "gate": g.get("gate"),
             "price_indicator": (_lex.THEME_BY_ID[g["theme_id"]].price_indicator
                                 if g.get("theme_id") in _lex.THEME_BY_ID else None)}
            for g in (payload.get("signals") or []) if g.get("id")],
            ["as_of", "signal_id"])

        # The generator's macro narrative is the top of the daily report, so keep
        # it addressable by batch rather than buried in an idea's raw payload.
        # Snapshot the theme scores the batch was actually generated against.
        # A later `score` run on the same date legitimately sees more corpus and
        # produces different numbers; without this snapshot the daily report would
        # show a narrative frozen at generation time above a table that has since
        # moved underneath it.
        themes_at_gen = [dict(r) for r in db.q(
            con, "SELECT * FROM themes WHERE as_of=?", (as_of.isoformat(),))]
        db.kv_set(con, f"batch_meta:{batch_id}", {
            "macro_narrative": payload.get("macro_narrative"),
            "note": payload.get("note"),
            "pack_sha": payload.get("pack_sha"),
            "transmissions": payload.get("transmissions") or [],
            "signals": payload.get("signals") or [],
            "themes_at_generation": themes_at_gen,
        })
    return batch_id, rows, report


# ---------------------------------------------------------------- validation
def validate_batch(con, rows: list[dict], as_of: date, payload: dict | None = None) -> dict:
    """Every §6 check from the ranking methodology, plus the v0.4 additions.

    A failing check does not silently degrade the batch: `pass` goes false, the
    batch is stored as `draft`, and the paper-trade step refuses to act on it.
    """
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: Any = None, severity: str = "error") -> None:
        checks.append({"check": name, "ok": bool(ok), "severity": severity,
                       "detail": detail})

    n = len(rows)
    # A batch carries however many ideas its selection produced. The old rule
    # (exactly 40) was the daily methodology's forced quota — the same forced
    # quota the 08-07 review flagged as a defect, because it padded thin days
    # with ideas nobody believed. Weekly selector batches hold ~10 by design,
    # and an empty batch is the only genuinely invalid size.
    chk("idea_count", n >= 1, {"n": n})
    ids = [r["local_id"] for r in rows]
    chk("ids_unique", len(set(ids)) == n, {"dupes": sorted({i for i in ids
                                                            if ids.count(i) > 1})})
    chk("horizon_allowed", all(r["horizon"] in config.HORIZONS for r in rows),
        {"bad": [r["local_id"] for r in rows if r["horizon"] not in config.HORIZONS]})
    chk("horizon_months_consistent",
        all(r["horizon_months"] == config.HORIZONS[r["horizon"]] for r in rows))

    bad_c = [r["local_id"] for r in rows if abs(sum(r["central_p"]) - 100) > 1e-6]
    chk("central_probs_sum_100", not bad_c, {"bad": bad_c})
    bad_k = [r["local_id"] for r in rows if abs(sum(r["conserv_p"]) - 100) > 1e-6]
    chk("conservative_probs_sum_100", not bad_k, {"bad": bad_k})

    bad_ord = [r["local_id"] for r in rows
               if not (r["central_r"][0] >= r["central_r"][1] >= r["central_r"][2])]
    chk("scenario_monotonic", not bad_ord, {"bad": bad_ord})

    # Recompute the odds independently and require agreement to 0.01pp (§6).
    worst = 0.0
    off: list[int] = []
    for r in rows:
        cost = r["raw"]["_derived"]["cost_pct"]
        a = odds(r["central_p"], [x - cost for x in r["central_r"]], r["hurdle"])
        b = odds(r["conserv_p"], [x - cost for x in r["conserv_r"]], r["hurdle"])
        for got, exp in ((r["ev_c"], a["ev"]), (r["ev_k"], b["ev"]),
                         (r["gain_c"], a["gain"]), (r["loss_c"], a["loss"])):
            worst = max(worst, abs((got or 0) - (exp or 0)))
        if abs((r["ev_c"] or 0) - a["ev"]) > 0.01:
            off.append(r["local_id"])
    chk("formula_recompute_within_0.01pp", worst <= 0.01,
        {"max_abs_diff_pp": round(worst, 6), "off": off})

    chk("hurdle_present", all(r["hurdle"] is not None for r in rows))
    chk("ref_price_present",
        all(r["ref_price"] is not None for r in rows if r["instrument"] == "listed"),
        {"missing": [r["local_id"] for r in rows
                     if r["instrument"] == "listed" and r["ref_price"] is None]})
    chk("ref_price_dated",
        all(r["ref_price_d"] for r in rows if r["instrument"] == "listed"),
        {"missing": [r["local_id"] for r in rows
                     if r["instrument"] == "listed" and not r["ref_price_d"]]})

    # v0.4: no idea may reference a bar that had not closed at generation time.
    future = [r["local_id"] for r in rows
              if r["ref_price_d"] and r["ref_price_d"] > as_of.isoformat()]
    chk("ref_price_not_future", not future, {"bad": future})

    src_ok = [r["local_id"] for r in rows
              if not (r["entry_src"] and r["take_src"] and r["stop_src"])]
    chk("input_source_tags_present", not src_ok, {"missing": src_ok},
        severity="warn")

    unmapped = [r["local_id"] for r in rows
                if r["instrument"] == "listed" and not r["futu_code"]]
    chk("listed_ideas_mapped", not unmapped, {"bad": unmapped})

    nomark = [r["local_id"] for r in rows
              if r["instrument"] not in ("listed", "fund", "structured", "monitor")]
    chk("instrument_kind_known", not nomark, {"bad": nomark})

    # v0.4 informational: scenario plausibility and single-signal concentration.
    wide = [r["local_id"] for r in rows if r["vol_check"] and "wide" in r["vol_check"]]
    narrow = [r["local_id"] for r in rows if r["vol_check"] and "narrow" in r["vol_check"]]
    chk("scenario_vol_plausible", not (wide or narrow),
        {"wide": wide, "narrow": narrow}, severity="warn")

    sig_counts: dict[str, int] = {}
    for r in rows:
        if r["signal_id"]:
            sig_counts[r["signal_id"]] = sig_counts.get(r["signal_id"], 0) + 1
    crowded = {k: v for k, v in sig_counts.items() if v > 3}
    chk("no_signal_over_3_ideas", not crowded, crowded, severity="warn")

    codes: dict[str, int] = {}
    for r in rows:
        if r["futu_code"]:
            codes[r["futu_code"]] = codes.get(r["futu_code"], 0) + 1
    dup_codes = {k: v for k, v in codes.items() if v > 1}
    chk("no_duplicate_instrument", not dup_codes, dup_codes, severity="warn")

    themed = [r["local_id"] for r in rows if not r["theme"]]
    chk("theme_assigned", not themed, {"bad": themed})

    errors = [c for c in checks if not c["ok"] and c["severity"] == "error"]
    warns = [c for c in checks if not c["ok"] and c["severity"] == "warn"]
    return {
        "pass": not errors, "n_errors": len(errors), "n_warnings": len(warns),
        "checks": checks, "methodology": config.METHODOLOGY_VERSION,
        "validated_at": config.now_hkt().isoformat(),
        "summary": {
            "n": n,
            "grades": {g: sum(1 for r in rows if r["grade"] == g) for g in "SABC"},
            "horizons": {h: sum(1 for r in rows if r["horizon"] == h)
                         for h in config.HORIZONS},
            "kinds": {k: sum(1 for r in rows if r["instrument"] == k)
                      for k in ("listed", "fund", "structured", "monitor")},
            "median_or_c": (round(st.median([r["or_c"] for r in rows
                                             if r["or_c"] is not None]), 3)
                            if any(r["or_c"] is not None for r in rows) else None),
        },
    }


# ---------------------------------------------------------------- accessors
def load_batch(con, batch_id: str) -> list[dict]:
    rows = db.q(con, "SELECT * FROM ideas WHERE batch_id=? ORDER BY rank", (batch_id,))
    out = []
    for r in rows:
        d = dict(r)
        for k in ("central_p", "central_r", "conserv_p", "conserv_r", "sources", "raw"):
            d[k] = db.jl(d.get(k), [] if k != "raw" else {})
        out.append(d)
    return out


def latest_batch(con, as_of: date | None = None) -> str | None:
    if as_of:
        r = db.q1(con, "SELECT batch_id FROM batches WHERE as_of=? "
                       "ORDER BY generated_at DESC LIMIT 1", (as_of.isoformat(),))
    else:
        r = db.q1(con, "SELECT batch_id FROM batches ORDER BY as_of DESC, "
                       "generated_at DESC LIMIT 1")
    return r["batch_id"] if r else None


def horizon_end(as_of: date, months: int) -> date:
    """Calendar horizon end. Trading-day resolution happens in the paper engine."""
    y, m = as_of.year, as_of.month + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    try:
        return date(y, m, as_of.day)
    except ValueError:                            # e.g. 31 Jan + 1 month
        nxt = date(y, m, 28) + timedelta(days=4)
        return nxt - timedelta(days=nxt.day)
