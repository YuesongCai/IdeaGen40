"""The consumption half: what the macro feeds attach to.

`feeds_impl/calendar_fmp.py` and `positioning_fmp.py` put four new kinds of row
into `events` — scheduled releases with a consensus, the volatility complex, the
Treasury curve, and futures positioning. Everything they emit reaches the
*generators*, as prompt text. Nothing reaches the *factors*, the entry band, or
the period record. This module is that half, and it exists because ingesting a
source without attaching it to a decision produces a longer prompt and nothing
else.

Three attachments, each aimed at a defect that was measured rather than guessed.

**Consensus -> factor N.** `scoring.factor_N` already takes a `consensus_z`
argument and no caller has ever passed one; its own docstring says the surprise
term degraded to a price proxy because "the corpus does not carry consensus or a
forecast-error distribution". Both halves now exist: FMP publishes `estimate`
alongside `actual`, and the same endpoint over a two-year window supplies the
distribution to divide by. `theme_consensus_z` supplies the argument.

**Positioning -> factor C.** Crowding is currently `0.45 x momentum percentile +
0.30 x distance from the 52-week high + 0.25 x calm`, and all three come from
`futu_px`, which is to say from price. The 2026-09-05 finding that the `ev`
ranking is one-order-of-magnitude explainable as a volatility ladder was reached
*from* price; a crowding measure built from price cannot test it, because it is
the same observation wearing a different name. CFTC positioning is the only
non-price crowding measure available here, and it covers COPX / USO / GLD / TLT
— the same names the drop-top-N robustness check identified as carrying the
whole advantage. So it can falsify that finding, or fail to, and either is worth
more than a fourth view of the price.

**Implied vol -> the entry band.** `db.py` documents `sigma_h` as "horizon
*realised* vol used for the sanity band", and the band is where the money went
missing: 264 of 383 orders expired unfilled. A 60-day backward-looking number
drawing a 30-day forward band is a mismatch that shows up as fills, not as a
wrong opinion. Implied vol is the market's own answer to the same question. FMP
serves no options, so this is index-level — which suits a book of ETFs held
against month-horizon macro theses better than a single name's smile would.

Nothing here changes behaviour by default
-----------------------------------------
Each attachment is a *model or scoring input*, and changing one mid-stream makes
period 7 incomparable with periods 1-6 — the same reason
`IDEAGEN_UNIVERSE_LOOKTHROUGH` ships off. So all three are off unless switched
on, and — the part that makes the switch decidable — **the counterfactual is
computed and recorded even while off**. `factor_C` reports what the score would
have been with positioning in it; `annotate_idea` records the implied sigma
beside the realised one. After a few periods the question "does this change
anything" has an answer made of this book's own numbers rather than of argument.

Regime is recorded, never gated
-------------------------------
`regime()` is the fourth thing and the odd one out: it is not attached to
anything and must not be. The reference compass read "bull" for roughly ninety
percent of the thirteen months on its own history chart. A state variable that
holds one value nine times out of ten has no variance to contribute across six
periods, and gating on it would be a multiple-testing error in better clothes.
It is written down each period so that at twenty periods the question — were the
returns conditional on regime, or was every arm just long the same tape — can be
asked with data instead of settled by whoever speaks last.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics as st
from datetime import date, timedelta
from typing import Any

from . import db
from .sources import fmp

# ---------------------------------------------------------------- switches
#: Read at call time rather than import time. A module-level constant would
#: freeze the value taken when `ideagen.macro` was first imported, and the
#: scheduler imports it once and then runs for days.
def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def flags() -> dict[str, bool]:
    """Every switch this module reads, so a run can record which were set.

    A period scored with the consensus surprise and a period scored without it
    are different experiments. Recording the flags is what stops them being
    averaged together later by someone who was not here.
    """
    return {
        "factor_n_consensus": _on("IDEAGEN_FACTOR_N_CONSENSUS"),
        "factor_c_positioning": _on("IDEAGEN_FACTOR_C_POSITIONING"),
        "sigma_implied": _on("IDEAGEN_SIGMA_IMPLIED"),
    }


# ---------------------------------------------------------------- schema
#: Local to this module, following `lookthrough.py`. `schema.py` carries the
#: tables other parts of the pipeline need the shape of; a forecast-error cache
#: is read by nothing but this file, and adding a column there means migrating
#: two cloud nodes for a table nobody else selects from.
DDL = (
    """CREATE TABLE IF NOT EXISTS macro_surprise_stats (
         event_key   TEXT NOT NULL,          -- country|event name, lowercased
         upto        TEXT NOT NULL,          -- as-of of the fit window
         n           INTEGER NOT NULL,       -- settled releases with both numbers
         mean_err    REAL NOT NULL,          -- mean (actual - estimate)
         sd_err      REAL NOT NULL,          -- the divisor v0.3 asked for
         first_d     TEXT NOT NULL,
         last_d      TEXT NOT NULL,
         status      TEXT NOT NULL,          -- ok | underpowered | degenerate
         PRIMARY KEY (event_key, upto)
       )""",
    "CREATE INDEX IF NOT EXISTS ix_msz_upto ON macro_surprise_stats(upto)",
)

#: Below this many settled prints, the standard deviation is a number rather
#: than a distribution. Dividing by it produces a confident z from three
#: observations, which is the failure this floor exists to prevent — and the
#: floor is stated in the row (`status='underpowered'`) rather than by dropping
#: it, so a reader can see the series was looked at and rejected.
MIN_OBS = 8

#: How much the theme's indicator has to have moved before a release that
#: settled the same day is treated as *its* news. Without a floor, "the biggest
#: day in the window" always names some day, so a theme whose indicator twitched
#: 0.34 sigma inherits the full 1.35 sigma of that morning's payrolls — an
#: attribution the price attribution was supposed to prevent. Observed on the
#: first live run: TERM-PREMIUM and POLICY-PATH both picked up NFP on moves of
#: 0.34 and 0.38.
MIN_REACTION_Z = 1.0

#: An `sd` of zero means every print matched consensus exactly, which in this
#: data means the vendor is republishing the estimate as the actual rather than
#: that the economy is deterministic. Dividing by it yields infinity.
MIN_SD = 1e-9


def ensure_schema(con) -> None:
    for stmt in DDL:
        con.execute(stmt)


# ---------------------------------------------------------------- refresh
#: Calendar feeds this layer reads back out of `events`. Named rather than
#: "every calendar feed" so that a feed added later for the generators' benefit
#: does not silently become a scoring input the day someone registers it.
CONSUMED_FEEDS = ("fmp_macro_releases", "fmp_vol_surface", "fmp_curve", "fmp_cot")

#: How stale a forecast-error fit may be before it is refitted. The distribution
#: moves on the timescale of years; refitting daily would spend nine calls a day
#: to change the fourth decimal place.
STATS_MAX_AGE_DAYS = 7


def refresh(con, as_of: date, *, feeds_to_run: tuple[str, ...] = CONSUMED_FEEDS,
            force_stats: bool = False, verbose: bool = False) -> dict[str, Any]:
    """Fetch the calendar feeds this layer consumes, persist them, refit stats.

    This runs *before* scoring, not after. `factor_N` and `factor_C` read
    `events`; a factor reading a table that today's run never filled is not
    degraded, it is confidently answering from whenever the table was last
    written — which is the failure shape this repo keeps finding, a wrong number
    where an absence belonged.

    One feed failing costs that feed, not the stage: positioning going dark
    should not also cost the calendar. What is *not* tolerated is silence — every
    failure comes back in `problems` and the caller prints it.
    """
    from . import feeds                       # local: avoids an import cycle
    from .orchestrator import event_row       # one row shape, defined once

    ensure_schema(con)
    problems: list[str] = []
    upserted = 0
    ok = 0
    for name in feeds_to_run:
        try:
            res = feeds.fetch(name, as_of)
        except Exception as e:                # noqa: BLE001 — reported, not raised
            problems.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if not res.ok:
            problems.append(f"{name}: {res.error}")
            continue
        ok += 1
        for row in res.rows:
            db.upsert(con, "events",
                      {**event_row({**row, "as_of": as_of.isoformat(),
                                    "feed": name})},
                      ("event_id",))
            upserted += 1
    con.commit()

    newest = db.q1(con, "SELECT MAX(upto) u FROM macro_surprise_stats")
    age = None
    if newest and newest["u"]:
        age = (as_of - date.fromisoformat(str(newest["u"]))).days
    if force_stats or age is None or age > STATS_MAX_AGE_DAYS:
        try:
            stats = refresh_surprise_stats(con, as_of, verbose=verbose)
        except Exception as e:                # noqa: BLE001
            problems.append(f"surprise stats: {type(e).__name__}: {e}")
            stats = {"ok": 0, "underpowered": 0, "degenerate": 0, "series": 0}
    else:
        counts = db.q(con, "SELECT status, COUNT(*) n FROM macro_surprise_stats"
                           " WHERE upto=? GROUP BY status", (str(newest["u"]),))
        stats = {r["status"]: r["n"] for r in counts}
        stats.setdefault("ok", 0)
        stats["reused_fit"] = str(newest["u"])
        stats["age_days"] = age

    # The period's macro state, written down and used for nothing. One key per
    # day rather than an overwritten "latest", because the whole point is the
    # series: a single current reading answers no question that six periods of
    # readings would not answer better, and the question this is being saved for
    # cannot be asked for another year.
    snap = regime(con, as_of.isoformat())
    db.kv_set(con, f"macro:regime:{as_of.isoformat()}", snap)
    con.commit()

    return {"as_of": as_of.isoformat(), "feeds_tried": len(feeds_to_run),
            "feeds_ok": ok, "events_upserted": upserted, "stats": stats,
            "regime_coverage": snap["coverage"],
            "flags": flags(), "problems": problems}


# ---------------------------------------------------------------- parsing
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _num(v: Any) -> float | None:
    """A number out of whatever the vendor or the `events` column holds.

    `events.actual` is TEXT and `events.expectation` is a formatted string with
    the unit glued on ("3.4%", "-0.6"), because that is what a prompt wants to
    read. Both have to become floats here, and a value that is not a number at
    all — "未公布", an empty string — has to come back as None rather than as
    zero, which would read as a print of exactly nought.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _NUM.search(str(v).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


#: The vendor glues the reference period onto the event name — "Core PCE Price
#: Index YoY (Jul)", "GDP Price Index QoQ (Q2)", "Initial Jobless Claims
#: (Aug/22)". Keyed on the raw name, every print is its own series with n=1, and
#: a two-year fit came back with **1 usable series out of 2375** — a result that
#: reads as "this vendor rarely publishes consensus" and is nothing of the kind.
#:
#: Only period-shaped suffixes are stripped. "(Flash)", "(Prel)", "(Final)" and
#: "(Adv)" stay, because a flash estimate and a final print genuinely have
#: different error scales and merging them would fabricate the opposite error:
#: one distribution fitted across two.
_PERIOD_SUFFIX = re.compile(
    r"\s*\((?:"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"(?:[/\s-]\d{1,4})?"          # (Aug) / (Aug/22) / (Aug 2026)
    r"|q[1-4](?:[/\s-]\d{2,4})?"   # (Q2) / (Q2 2026)
    r"|\d{4}"                      # (2026)
    r"|w\d{1,2}"                   # (W34)
    r")\)\s*$", re.IGNORECASE)


def _series_name(event: Any) -> str:
    """The release name with its reference period removed, lowercased."""
    name = str(event or "").strip()
    prev = None
    while prev != name:                       # "(Q2) (2026)" would need two passes
        prev = name
        name = _PERIOD_SUFFIX.sub("", name).strip()
    return name.lower()


def _event_key(country: Any, event: Any) -> str:
    """The identity a forecast-error distribution is fitted per.

    Per *series*, not per print: "Inflation Rate YoY" has its own error scale and
    that scale is what a z divides by. Country is in the key because "Inflation
    Rate YoY" is published by every country in the file and their errors are not
    the same distribution.
    """
    return f"{str(country or '').upper()}|{_series_name(event)}"


# ---------------------------------------------------------------- N: surprise
def refresh_surprise_stats(con, upto: date, *, years: int = 2,
                           countries: tuple[str, ...] = ("US",),
                           chunk_days: int = 90,
                           verbose: bool = False) -> dict[str, Any]:
    """Fit (actual - estimate) per release series over a trailing window.

    This is the denominator v0.3 specified and could not compute. It is fitted
    from settled history only — a row without both numbers contributes nothing —
    and stamped with the `upto` it was fitted at, so a replay of an old period
    can be given the distribution as it stood then rather than one that has since
    seen the answer.

    Chunked because the endpoint charges one call for the whole world and a
    two-year range is a response large enough that a truncated body parses as a
    JSON error a long way from its cause. Nine calls that each succeed or fail
    visibly beat one that half-arrives.
    """
    ensure_schema(con)
    start = upto - timedelta(days=365 * years)
    want = {c.upper() for c in countries}

    errs: dict[str, list[tuple[str, float]]] = {}
    chunks = 0
    cursor = start
    while cursor < upto:
        end = min(cursor + timedelta(days=chunk_days), upto)
        rows = fmp.economic_calendar(cursor.isoformat(), end.isoformat())
        chunks += 1
        for r in rows:
            if want and str(r.get("country") or "").upper() not in want:
                continue
            act, est = _num(r.get("actual")), _num(r.get("estimate"))
            if act is None or est is None:
                continue
            d = str(r.get("date") or "")[:10]
            if not d:
                continue
            errs.setdefault(_event_key(r.get("country"), r.get("event")),
                            []).append((d, act - est))
        cursor = end + timedelta(days=1)

    fitted = {"ok": 0, "underpowered": 0, "degenerate": 0}
    for key, pairs in errs.items():
        pairs.sort()
        vals = [e for _, e in pairs]
        n = len(vals)
        sd = st.pstdev(vals) if n > 1 else 0.0
        if n < MIN_OBS:
            status = "underpowered"
        elif sd <= MIN_SD:
            status = "degenerate"
        else:
            status = "ok"
        fitted[status] += 1
        db.upsert(con, "macro_surprise_stats", {
            "event_key": key, "upto": upto.isoformat(), "n": n,
            "mean_err": round(st.fmean(vals), 6), "sd_err": round(sd, 6),
            "first_d": pairs[0][0], "last_d": pairs[-1][0], "status": status,
        }, ("event_key", "upto"))
    con.commit()
    out = {"upto": upto.isoformat(), "series": len(errs), "chunks": chunks,
           "years": years, **fitted}
    if verbose:
        print(f"  surprise stats {out}")
    return out


def _stats_for(con, key: str, upto: str) -> dict[str, Any] | None:
    """The newest fit at or before `upto`, so a replay reads the old one."""
    r = db.q1(con, "SELECT * FROM macro_surprise_stats WHERE event_key=?"
                   " AND upto<=? ORDER BY upto DESC LIMIT 1", (key, upto))
    return dict(r) if r else None


def window_surprises(con, days: list[str], *,
                     countries: tuple[str, ...] = ("US",)) -> list[dict[str, Any]]:
    """Every release inside `days` that settled, with its z where one is defined.

    A release with no consensus, or whose series has too few settled prints to
    have an error scale, comes back with `z=None` and a stated reason. That is
    deliberately not the same as being absent: "CPI printed and we had no scale
    to judge it by" and "no CPI this window" are different facts and only one of
    them is a quiet week.
    """
    ensure_schema(con)
    if not days:
        return []
    lo, hi = min(days), max(days)
    rows = db.q(con, "SELECT * FROM events WHERE kind IN ('macro_release','policy')"
                     " AND date>=? AND date<=? ORDER BY date", (lo, hi))
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = {}
        if r["payload"]:
            try:
                payload = json.loads(r["payload"])
            except (ValueError, TypeError):
                payload = {}
        src = str(r["source"] or "")
        country = payload.get("country")
        if country is None:
            m = re.search(r"\(([A-Za-z]{2,3})\)\s*$", src)
            country = m.group(1) if m else "US"
        if countries and str(country).upper() not in {c.upper() for c in countries}:
            continue

        act, est = _num(r["actual"]), _num(r["expectation"])
        item: dict[str, Any] = {
            "event_id": r["event_id"], "date": r["date"], "label": r["label"],
            "kind": r["kind"], "actual": act, "estimate": est,
            "impact": payload.get("impact"), "unit": r["unit"],
            "z": None, "why": None,
        }
        if act is None or est is None:
            item["why"] = "未结算" if act is None else "无一致预期"
            out.append(item)
            continue
        key = _event_key(country, r["label"])
        stats = _stats_for(con, key, hi)
        if not stats:
            # Distinguish "never fitted" from "only fitted after this window",
            # which is the look-ahead guard working as designed. Reported as one
            # message, a replay of an old period looks like a vendor gap.
            later = db.q1(con, "SELECT MIN(upto) u FROM macro_surprise_stats"
                               " WHERE event_key=?", (key,))
            if later and later["u"]:
                item["why"] = (f"误差分布最早拟合于 {later['u']}，晚于本窗口"
                               f"（{hi}）——不回填")
            else:
                item["why"] = "该序列没有拟合过误差分布"
        elif stats["status"] != "ok":
            item["why"] = (f"误差分布 {stats['status']}"
                           f"（n={stats['n']}，需 ≥{MIN_OBS}）")
        else:
            item["z"] = round((act - est) / stats["sd_err"], 3)
            item["sd_err"] = stats["sd_err"]
            item["n_obs"] = stats["n"]
        out.append(item)
    return out


def theme_consensus_z(con, ev: dict, theme, *,
                      require_flag: bool = True) -> tuple[float | None, dict[str, Any]]:
    """The `consensus_z` to hand `factor_N`, attributed by day, not by name.

    The tempting join is release name against theme synonyms, and it is the join
    that already failed once here: `lookthrough` was built because matching a
    thesis to an instrument by its label produced ITA, PPA and XAR reading as the
    same thing while holding 53%, 45% and 26% of one basket. A release named
    "Core CPI YoY" matched against a theme's term list would be that mistake in a
    new place.

    So the attribution runs through price, which is pre-registered and cannot be
    chosen after the fact: take the day inside the scoring window on which the
    theme's own registered indicator moved most, and use the surprise of the
    release that settled that day. If nothing settled that day, or nothing has an
    error scale, return None and `factor_N` keeps the behaviour it has today.

    The magnitude is then the market's surprise rather than the indicator's move,
    which is the whole point: a 2σ move on a day when everything came in as
    expected is not new information about this theme, and today it scores as if
    it were.
    """
    from .sources import futu_px          # local: keeps import cost off cold paths

    meta: dict[str, Any] = {"enabled": (not require_flag) or flags()["factor_n_consensus"]}
    if require_flag and not meta["enabled"]:
        meta["note"] = "IDEAGEN_FACTOR_N_CONSENSUS 未开启"

    days = list(ev.get("days") or [])
    rel = [s for s in window_surprises(con, days) if s["z"] is not None]
    meta["settled_with_z"] = len(rel)
    if not rel:
        meta["reason"] = "窗口内没有带 z 的已结算发布"
        return None, meta

    # The indicator's biggest absolute day inside the window. `move_z` normalises
    # by the indicator's own trailing vol, so "biggest" is comparable across
    # instruments of very different volatility.
    best_day, best_z = None, 0.0
    for d in days:
        z = futu_px.move_z(con, theme.price_indicator, d)
        if z is not None and abs(z) > abs(best_z):
            best_day, best_z = d, z
    meta["indicator"] = theme.price_indicator
    meta["indicator_day"] = best_day
    meta["indicator_move_z"] = (round(best_z, 3) if best_day else None)
    meta["min_reaction_z"] = MIN_REACTION_Z
    if not best_day:
        meta["reason"] = "指标在窗口内没有可用价格"
        return None, meta
    if abs(best_z) < MIN_REACTION_Z:
        # The theme did not react to anything this window. Handing it the
        # morning's surprise anyway would say "this release was news for this
        # theme" on the evidence that it was news for somebody.
        meta["reason"] = (f"指标最大波动仅 {best_z:+.2f}σ，低于 {MIN_REACTION_Z}σ"
                          f"——本窗口该主题没有反应，不归因")
        return None, meta

    same_day = [s for s in rel if s["date"] == best_day]
    if not same_day:
        meta["reason"] = f"指标最大波动日 {best_day} 当天没有已结算发布"
        return None, meta

    pick = max(same_day, key=lambda s: abs(s["z"]))
    meta.update({"release": pick["label"], "release_date": pick["date"],
                 "actual": pick["actual"], "estimate": pick["estimate"],
                 "sd_err": pick.get("sd_err"), "n_obs": pick.get("n_obs"),
                 "z": pick["z"],
                 "candidates_same_day": len(same_day)})
    if not meta["enabled"]:
        # Recorded, not applied. This is what makes the switch decidable: the
        # difference between the two surprise sources accumulates in the factor
        # metadata from now, whether or not anyone flips it.
        return None, meta
    return float(pick["z"]), meta


# ---------------------------------------------------------------- C: positioning
#: Instrument -> CFTC contract, split by how the link is made.
#:
#: `DIRECT` is identity: the fund's exposure *is* that future, so the
#: speculative position in the contract is the position in the thing the book
#: holds. `PROXY` is a beta relationship — copper miners are not copper — and
#: everything that reads a proxy says so, because the two are not the same claim
#: and a reader who cannot tell them apart will over-trust the second.
#:
#: What is deliberately absent is as informative: DBC and PDBC are broad
#: commodity baskets with no single contract behind them, and the factor-tilt
#: funds (VLUE, QUAL, MTUM) are not the S&P — VLUE is 21% semiconductors by
#: weight. Mapping them to ES to raise coverage would be exactly the label
#: assertion this system already paid to remove once.
COT_DIRECT: dict[str, str] = {
    "US.SPY": "ES", "US.RSP": "ES",
    "US.QQQ": "NQ",
    "US.GLD": "GC", "US.SLV": "SI",
    "US.CPER": "HG",
    "US.USO": "CL",
    "US.TLT": "ZB", "US.IEF": "ZN",
    "US.UUP": "DX", "US.FXE": "6E", "US.FXY": "6J",
}

COT_PROXY: dict[str, str] = {
    "US.COPX": "HG",                     # copper miners: equity with copper beta
    "US.XLE": "CL", "US.XOP": "CL", "US.OIH": "CL",
    "US.SHY": "ZN", "US.MBB": "ZN", "US.AGG": "ZN",
}


def positioning_crowding(con, code: str, as_of: str) -> tuple[float | None, dict[str, Any]]:
    """Speculative net-long share for `code`'s contract, as a 0-100 crowding leg.

    The vendor's `currentLongMarketSituation` is already a percentage of the
    speculative book on the long side, so it needs no rescaling to be read as
    crowding: gold at 88.95 on 2026-09-01 says the speculative side is nearly
    fully committed, which is a statement about who is left to buy. That is the
    same direction `factor_C`'s other legs point in, and unlike them it is not
    derived from the price.

    Direction-agnostic, matching the rest of `factor_C`: a high momentum
    percentile is treated as crowded there without asking which way the theme
    leans, and mixing conventions inside one factor would make the number
    uninterpretable.
    """
    contract = COT_DIRECT.get(code)
    link = "direct"
    if not contract:
        contract = COT_PROXY.get(code)
        link = "proxy"
    if not contract:
        return None, {"code": code, "link": "none",
                      "note": "该标的没有对应的 CFTC 合约；不硬映射"}

    r = db.q1(con, "SELECT * FROM events WHERE event_id LIKE ? AND date<=?"
                   " ORDER BY date DESC LIMIT 1",
              (f"cot:{contract}:%", as_of))
    if not r:
        return None, {"code": code, "contract": contract, "link": link,
                      "note": "库里没有该合约的 COT 记录"}
    share = _num(r["actual"])
    if share is None:
        return None, {"code": code, "contract": contract, "link": link,
                      "note": "COT 记录里没有可解析的净多占比"}

    lag = (date.fromisoformat(as_of) - date.fromisoformat(str(r["date"]))).days
    payload = {}
    if r["payload"]:
        try:
            payload = json.loads(r["payload"])
        except (ValueError, TypeError):
            payload = {}
    return round(max(0.0, min(100.0, share)), 1), {
        "code": code, "contract": contract, "link": link,
        "cot_date": r["date"], "lag_days": lag,
        "net_long_pct": share,
        "change_in_net": payload.get("change_in_net"),
        "source": r["source"],
    }


# ---------------------------------------------------------------- sigma: implied
#: Instrument -> the published volatility index that prices *its* risk.
#:
#: This is an assertion, and the way it is kept honest is that it is measurable
#: from the first run: `implied_sigma_pct` reports the implied/realised ratio, so
#: a mapping that is wrong shows up as a ratio far from 1 rather than as a
#: quietly wrong band. An instrument with no entry keeps realised vol — degrading
#: to today's behaviour, not to a wrong number.
VOL_INDEX_FOR: dict[str, str] = {
    # broad US equity
    "US.SPY": "^VIX", "US.RSP": "^VIX", "US.SPLV": "^VIX", "US.USMV": "^VIX",
    "US.MTUM": "^VIX", "US.QUAL": "^VIX", "US.VLUE": "^VIX",
    "US.XLF": "^VIX", "US.XLI": "^VIX", "US.XLP": "^VIX", "US.XLU": "^VIX",
    "US.XLV": "^VIX", "US.KBE": "^VIX", "US.KRE": "^VIX", "US.KIE": "^VIX",
    # nasdaq / technology complex
    "US.QQQ": "^VXN", "US.SMH": "^VXN", "US.SOXX": "^VXN", "US.IGV": "^VXN",
    "US.SKYY": "^VXN", "US.ARKW": "^VXN", "US.CIBR": "^VXN", "US.XSD": "^VXN",
    # rates
    "US.TLT": "^VXTLT",
    "US.IEF": "^MOVE", "US.AGG": "^MOVE", "US.MBB": "^MOVE", "US.SHY": "^MOVE",
    "US.TIP": "^MOVE", "US.STIP": "^MOVE",
    # energy
    "US.USO": "^OVX", "US.XLE": "^OVX", "US.XOP": "^OVX", "US.OIH": "^OVX",
    # metals
    "US.GLD": "^GVZ", "US.SLV": "^GVZ",
}


def implied_sigma_pct(con, code: str, as_of: str, months: float,
                      realised_pct: float | None = None
                      ) -> tuple[float | None, dict[str, Any]]:
    """Horizon sigma from the volatility complex, in percent, or None.

    A vol index quotes an annualised standard deviation in points, so the horizon
    number is `index / 100 x sqrt(months / 12)` — the same square-root scaling
    `futu_px.horizon_sigma` applies to the realised figure, which is what makes
    the two comparable at all.

    `ratio_to_realised` is the mapping's own report card. TLT against ^VXTLT
    should sit near 1; if a mapping is wrong the ratio drifts and says so in the
    idea's metadata, on the first period, without anyone having to audit this
    dictionary by hand.
    """
    sym = VOL_INDEX_FOR.get(code)
    if not sym:
        return None, {"code": code, "note": "该标的没有对应的波动率指数；沿用已实现波动"}

    r = db.q1(con, "SELECT * FROM events WHERE event_id LIKE ? AND date<=?"
                   " ORDER BY date DESC LIMIT 1", (f"fmpvol:{sym}:%", as_of))
    if not r:
        return None, {"code": code, "vol_index": sym,
                      "note": "库里没有该波动率指数的记录"}
    level = _num(r["actual"])
    if level is None or level <= 0:
        return None, {"code": code, "vol_index": sym,
                      "note": "波动率指数记录不可解析"}

    sigma = (level / 100.0) * math.sqrt(max(months, 0.0) / 12.0) * 100.0
    meta: dict[str, Any] = {
        "code": code, "vol_index": sym, "index_level": level,
        "index_date": r["date"], "months": months,
        "implied_sigma_pct": round(sigma, 3),
        "realised_sigma_pct": (round(realised_pct, 3)
                               if realised_pct is not None else None),
        "enabled": flags()["sigma_implied"],
    }
    if realised_pct:
        meta["ratio_to_realised"] = round(sigma / realised_pct, 3)
    return round(sigma, 3), meta


def band_sigma_pct(con, code: str, as_of: str, months: float,
                   realised_pct: float | None) -> tuple[float | None, dict[str, Any]]:
    """The sigma the entry band and the stops should use, and why that one.

    Off (the default) this returns the realised figure unchanged and records what
    implied would have said. On, it returns the larger of the two.

    The larger rather than the implied, because the failure being addressed is
    one-sided. 264 of 383 orders expired unfilled: the bands were too narrow, and
    a rule that can only widen them cannot make that worse. Taking implied
    unconditionally would sometimes *narrow* a band — on a name where the market
    is charging less for the next month than the last sixty days delivered — and
    that is a second, opposite bet, made silently, with no evidence behind it.
    """
    implied, meta = implied_sigma_pct(con, code, as_of, months, realised_pct)
    meta["realised_sigma_pct"] = (round(realised_pct, 3)
                                  if realised_pct is not None else None)
    if implied is None or not flags()["sigma_implied"]:
        meta["used"] = "realised"
        return realised_pct, meta
    if realised_pct is None:
        meta["used"] = "implied"
        return implied, meta
    meta["used"] = "implied" if implied > realised_pct else "realised"
    return max(implied, realised_pct), meta


# ---------------------------------------------------------------- regime record
#: Each leg is (event_id prefix, label, direction). `direction` is +1 when a
#: higher reading means a friendlier tape and -1 when it means a rougher one, so
#: the legs can be summarised without every reader having to remember which way
#: the MOVE index points.
REGIME_LEGS: tuple[tuple[str, str, int], ...] = (
    ("fmpvol:^VIX:",                  "股票隐含波动率", -1),
    ("fmpvol:ratio:^VIX/^VIX3M:",     "VIX 期限结构 1M/3M", -1),
    ("fmpvol:^MOVE:",                 "美债隐含波动率", -1),
    ("fred:BAMLH0A0HYM2:",            "高收益债利差", -1),
    ("fmpcurve:2s10s:",               "2s10s 利差", +1),
    ("fmpcurve:3m10y:",               "3m10y 利差", +1),
)


def regime(con, as_of: str) -> dict[str, Any]:
    """The period's macro state, written down and used for nothing.

    Read the module note before wiring this into anything. It is here so that the
    2026-09-05 finding — that the 61% hit rate is approximately beta, because
    every arm rose and fell together within each period — can eventually be
    decomposed. That decomposition needs periods, not a better indicator, and
    six is not enough for any conditioning statement to mean anything.

    So this returns levels and no verdict. There is deliberately no bull/bear
    label and no composite score: a single number invites exactly the gate this
    is meant to postpone, and a level per leg is what a later regression wants
    anyway.
    """
    legs: dict[str, Any] = {}
    for prefix, label, direction in REGIME_LEGS:
        r = db.q1(con, "SELECT * FROM events WHERE event_id LIKE ? AND date<=?"
                       " ORDER BY date DESC LIMIT 1", (f"{prefix}%", as_of))
        if not r:
            legs[label] = {"value": None, "note": "库里没有记录"}
            continue
        legs[label] = {"value": _num(r["actual"]), "unit": r["unit"],
                       "d": r["date"], "direction": direction,
                       "lag_days": (date.fromisoformat(as_of)
                                    - date.fromisoformat(str(r["date"]))).days}
    have = [k for k, v in legs.items() if v.get("value") is not None]
    return {
        "as_of": as_of,
        "legs": legs,
        "coverage": f"{len(have)}/{len(REGIME_LEGS)}",
        "gate": False,
        "note": ("记录用，不作闸门。参照实现的状态指示在 13 个月里约九成时间读同一个值，"
                 "在只有 6 期的样本上没有方差可贡献；攒够期次后用它做条件回归。"),
    }
