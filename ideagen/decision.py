"""The decision layer: from the full candidate pool to what a PM actually trades.

yifu, 2026-09-11: 「全量可看，决策时精选到个位数」「卫星仓 5–10 个点」「AI 出 idea
→ 人做反方筛选」. The pipeline up to stage C is a research instrument — twelve
arms racing on one pool so selection can be measured. None of that is a thing a
PM can act on on a Wednesday morning. This module is the thin layer between the
two, and it keeps four rules:

1. **Only what can fill.** A fund whose newest NAV is days old does not enter
   stage C (`annotate_pool`). On 2026-09-09 sixty-five public funds sat in the
   pool, their limit orders never filled, and the books stayed empty.
2. **One shortlist, computed once.** The ranking (`rank_shortlist`) is a pure
   function; the `shortlist` selector books it, the panel reads the verdict that
   selector stored, and the ticket sizes it. The panel used to rank its own copy
   in the browser (`poolShortlistSet`), which was a second truth waiting to
   drift from anything a book held.
3. **A PM removes, never adds.** Allspring: the PM challenges the model's output
   and strikes suspicious names; nobody overrides it by inserting one.
   `submit_review` refuses any instrument that is not on that period's
   shortlist. The model is not asked to argue against itself either (「AI 暂时
   不能 play devil's advocate」): next to each name it shows *why it picked it*,
   and the counter-case is written by a person.
4. **Reviews are data, and the data lives on the laptop.** The team clicks on
   the display node, whose database is replaced by the laptop's snapshot every
   sync. So every review is also appended to a journal on the node's data mount
   (which survives both the database swap and a container replacement), served
   read-only at `/api/pm_reviews/export`, and pulled back into the laptop's
   database by `pull_reviews` — after which the next snapshot carries it
   everywhere.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config, db

GRADE_RANK = {"S": 4, "A": 3, "B": 2, "C": 1}

#: Instruments whose daily returns stand in for the three macro sensitivities a
#: PM asks about first. Proxies, named as such on the card: a correlation to
#: TLT is "moves with long rates", not a duration number.
MACRO_PROXIES = (("利率（TLT）", "US.TLT"), ("美元（UUP）", "US.UUP"),
                 ("油价（USO）", "US.USO"))
MACRO_LOOKBACK = 60
LIQ_LOOKBACK = 20


# =========================================================== pool gating
def _resolve(c: dict[str, Any]):
    from . import universe as uni
    return uni.resolve(str(c.get("instrument_id") or ""))


def _is_fund(inst) -> bool:
    return bool(inst) and inst.kind != "listed" and bool(inst.olive_key)


def nav_freshness(con, as_of: date, c: dict[str, Any]) -> dict[str, Any]:
    """How old a fund candidate's newest usable NAV is on the period date.

    Listed instruments return `{}`: they have a close every session and the
    markability gate already covers a missing one. A fund with no NAV at all is
    not *stale*, it is unmarkable — that gate owns it, so the flag here stays
    False rather than double-counting the same exclusion under two names.
    """
    inst = _resolve(c)
    if not _is_fund(inst):
        return {}
    from .sources import olive
    hit = olive.nav_on_or_before(con, str(inst.olive_key), as_of.isoformat())
    if not hit:
        return {"nav_d": None, "nav_stale_days": None, "stale_nav_excluded": False}
    days = (as_of - date.fromisoformat(hit[0])).days
    limit = int(config.FUND_NAV_FRESH_DAYS or 0)
    return {"nav_d": hit[0], "nav_stale_days": days,
            "stale_nav_excluded": bool(limit > 0 and days > limit)}


def _recurrence(con, theme_id: str | None, as_of: date) -> dict[str, Any] | None:
    """The newest scored recurrence for a theme on or before the period date.

    The daily scorer writes `themes.factors.recurrence`; the weekly run is dated
    a day later than the last daily score it can see, so an exact-date lookup
    finds nothing. A week back is the window: older than that is a different
    cycle and must not shade this one. Never reads a date after `as_of`.
    """
    if not theme_id or con is None:
        return None
    try:
        rows = db.q(con, "SELECT as_of, factors FROM themes WHERE theme_id=? "
                         "AND as_of<=? AND as_of>=? ORDER BY as_of DESC",
                    (theme_id, as_of.isoformat(),
                     (as_of - timedelta(days=7)).isoformat()))
    except Exception:  # noqa: BLE001 — no themes table: no discount, not an error
        return None
    for r in rows:
        rec = (db.jl(r["factors"], {}) or {}).get("recurrence")
        if isinstance(rec, dict):
            return {**rec, "scored_on": r["as_of"]}
    return None


def recur_frac(rec: dict[str, Any] | None) -> float:
    """Share of the theme's raw score taken off for being seen again, 0–1.

    「第二次出现幅度也要打折」: the theme score already carries this discount;
    the shortlist applies the same proportion to an idea's expected size, so a
    sixth consecutive week of the same story is not ranked as if it were news.
    Missing data is 0 — no discount — and the card says the reading was absent.
    """
    if not rec:
        return 0.0
    try:
        disc = float(rec.get("discount") or 0.0)
        raw = float(rec.get("tis_raw") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if raw <= 0 or disc <= 0:
        return 0.0
    return max(0.0, min(1.0, disc / raw))


def score_candidate(con, as_of: date, c: dict[str, Any]) -> dict[str, Any]:
    """The same ev_c / odds / grade booking would store for this candidate.

    Goes through `booking.payload_from_candidates` and `ideas.compute` — the
    exact path a booked idea takes — instead of re-deriving the formula here.
    Two copies of a cost-net expectation would agree today and not after the
    next change to either, and the shortlist would then rank on a number no
    book ever held.
    """
    from . import booking, ideas
    try:
        raw = booking.payload_from_candidates([c])["ideas"][0]
        row = ideas.compute(con, raw, as_of, "shortlist-probe")
    except Exception as e:  # noqa: BLE001 — an unscorable candidate is unranked, said so
        return {"ev_c": None, "score_error": f"{type(e).__name__}: {e}"[:160]}
    return {k: row.get(k) for k in ("ev_c", "or_c", "or_k", "grade")}


def annotate_pool(p, as_of: date, candidates: list[dict[str, Any]],
                  dry_run: bool = False, *, score: bool = True) -> dict[str, int]:
    """Stamp NAV freshness and shortlist inputs on candidates, in place.

    Called by the orchestrator and by `reselect` on the same list they filter
    into the stage-C pool, so the two paths cannot diverge. Returns the counts
    the journal records. A dry run or a state store without a local SQLite
    connection checks nothing and says so through `checked=0`.
    """
    con = getattr(getattr(p, "state", None), "connection", None)
    counts = {"checked": 0, "funds": 0, "stale_nav_excluded": 0, "scored": 0}
    if dry_run or con is None:
        return counts
    from . import universe as uni
    try:
        uni.hydrate(con)
    except Exception:  # noqa: BLE001 — no instruments table: nothing resolves as a fund
        pass
    counts["checked"] = len(candidates)
    for c in candidates:
        try:
            fr = nav_freshness(con, as_of, c)
        except Exception:  # noqa: BLE001 — a NAV table this store lacks: not judged
            fr = {}
        if fr:
            counts["funds"] += 1
        c.update(fr)
        c.setdefault("stale_nav_excluded", False)
        counts["stale_nav_excluded"] += 1 if c.get("stale_nav_excluded") else 0
        if score:
            c.update(score_candidate(con, as_of, c))
            rec = _recurrence(con, c.get("topic_id"), as_of)
            c["recurrence"] = rec
            c["recur_frac"] = round(recur_frac(rec), 6)
            counts["scored"] += 1 if c.get("ev_c") is not None else 0
    return counts


def in_selection_pool(c: dict[str, Any]) -> bool:
    """The stage-C admission rule, in one place (markable, not private, NAV fresh)."""
    return (c.get("markable") is not False and not c.get("private_excluded")
            and not c.get("stale_nav_excluded"))


# ======================================================= shortlist ranking
def _n_methods(c: dict[str, Any]) -> int:
    if c.get("n_methods") is not None:
        try:
            return max(1, int(c["n_methods"]))
        except (TypeError, ValueError):
            pass
    return max(1, len({str(m) for m in (c.get("proposed_by") or [])}))


def _gross_ev(c: dict[str, Any]) -> float | None:
    """Scenario expectation before costs, for contexts nobody annotated."""
    rs = [c.get("upside_pct"), 0.0, c.get("downside_pct")]
    ps = [c.get("p_up"), c.get("p_base"), c.get("p_down")]
    if any(v is None for v in rs + ps):
        return None
    tot = sum(float(x) for x in ps)
    if tot <= 0:
        return None
    return sum(float(pp) / tot * float(r) for pp, r in zip(ps, rs))


def rank_shortlist(cands: Iterable[dict[str, Any]], *, n: int | None = None,
                   max_per_theme: int | None = None) -> dict[str, Any]:
    """共识度 × max(期望值, 0) × (1 − 复现折扣比例), ties by grade then ev.

    A candidate whose score is zero is not a pick: a non-positive expectation is
    a worse buy than cash, and filling a slot with it would turn a threshold
    into a quota (the omega arm's argument). So a thin week holds fewer names.
    The theme cap is applied while walking the ranking, so the name that loses
    its slot to the cap is recorded with that reason, not silently skipped.
    """
    n = int(config.SHORTLIST_N if n is None else n)
    cap = int(config.SHORTLIST_MAX_PER_THEME if max_per_theme is None else max_per_theme)
    rows: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    source = "ev_c"
    for c in cands:
        cid = str(c.get("id"))
        ev = c.get("ev_c")
        if ev is None and "ev_c" not in c:
            ev = _gross_ev(c)
            source = "scenario_gross"
        if ev is None:
            rejected[cid] = "没有可用的期望值"
            continue
        ev = float(ev)
        rf = float(c.get("recur_frac") or 0.0)
        nm = _n_methods(c)
        score = nm * max(ev, 0.0) * (1.0 - rf)
        rows[cid] = {"id": cid, "instrument_id": c.get("instrument_id"),
                     "topic_id": c.get("topic_id"), "n_methods": nm,
                     "ev_c": round(ev, 4), "grade": c.get("grade"),
                     "recur_frac": round(rf, 4), "score": round(score, 4)}
        if score <= 0:
            rejected[cid] = f"期望值 {ev:.2f}% 不为正，不进精选"
    ranked = sorted((r for r in rows.values() if r["score"] > 0),
                    key=lambda r: (-r["score"], -GRADE_RANK.get(str(r["grade"]), 0),
                                   -r["ev_c"], str(r["instrument_id"])))
    chosen: list[str] = []
    per_theme: dict[str, int] = {}
    for r in ranked:
        t = str(r["topic_id"] or "—")
        if len(chosen) >= n:
            rejected[r["id"]] = f"不在前 {n} 名"
            continue
        if cap > 0 and per_theme.get(t, 0) >= cap:
            rejected[r["id"]] = f"主题 {t} 已入选 {cap} 只（同主题上限）"
            continue
        per_theme[t] = per_theme.get(t, 0) + 1
        chosen.append(r["id"])
        r["rank"] = len(chosen)
    return {"chosen": chosen, "rows": rows, "rejected": rejected,
            "n": n, "max_per_theme": cap, "score_source": source}


# ================================================== the period's shortlist
_SHORT_CACHE: dict[tuple[str, str, int], dict[str, Any]] = {}


def _first_sentence(text: Any, limit: int = 120) -> str | None:
    s = str(text or "").strip()
    if not s:
        return None
    m = re.split(r"(?<=[。！？；!?;])", s, maxsplit=1)
    head = (m[0] if m else s).strip()
    return head[:limit] + ("…" if len(head) > limit else "")


def period_shortlist(p, con, run_id: str, as_of: str,
                     cands: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The shortlist of one weekly run: the stored verdict if there is one.

    `source="verdict"` means these are the names the `shortlist` book was built
    from. A period run before the arm existed and not replayed has no verdict;
    it is ranked here with the same function and inputs and says
    `source="computed"`, so a reader can tell a record from a reconstruction.
    """
    # Only the reconstruction is cached (it scores every candidate). A stored
    # verdict is read fresh every time: `reselect` replaces it under the same
    # run id, and a long-running server must not keep showing the old one.
    key = (run_id, as_of, int(config.SHORTLIST_N), int(config.SHORTLIST_MAX_PER_THEME),
           int(config.FUND_NAV_FRESH_DAYS))
    ver = p.state.q("SELECT chosen, scores, rejected, meta FROM verdicts "
                    "WHERE run_id=? AND kind='idea_selector' AND strategy='shortlist'",
                    (run_id,))
    if not ver and key in _SHORT_CACHE:
        return _SHORT_CACHE[key]
    if cands is None:
        cands = [db.jl(r["payload"], {}) for r in p.state.q(
            "SELECT payload FROM candidates WHERE run_id=?", (run_id,))]
        cands = [c for c in cands if c]
    by_id = {str(c.get("id")): c for c in cands}
    if ver:
        chosen = [str(x) for x in json.loads(ver[0]["chosen"] or "[]")]
        scores = json.loads(ver[0]["scores"] or "{}")
        meta = json.loads(ver[0]["meta"] or "{}")
        source = "verdict"
    else:
        d = date.fromisoformat(as_of)
        work = [dict(c) for c in cands]
        # Stored payloads of older runs carry neither the NAV flag nor the
        # scores; annotate a copy, never the stored rows.
        annotate_pool(_PCon(con), d, work)
        pool = [c for c in work if in_selection_pool(c)]
        rk = rank_shortlist(pool)
        by_id.update({str(c.get("id")): c for c in work})
        chosen, scores = rk["chosen"], rk["rows"]
        meta = {"n": rk["n"], "max_per_theme": rk["max_per_theme"],
                "score_source": rk["score_source"]}
        source = "computed"
    items = []
    for i, cid in enumerate(chosen, 1):
        c = by_id.get(cid, {})
        sc = scores.get(cid, {}) if isinstance(scores, dict) else {}
        items.append({
            "rank": i, "candidate_id": cid,
            "instrument_id": c.get("instrument_id") or sc.get("instrument_id"),
            "instrument_name": c.get("instrument_name"),
            "vehicle": c.get("vehicle"), "topic_id": c.get("topic_id") or sc.get("topic_id"),
            "topics": sorted({str(t) for t in (c.get("topics") or []) if t}
                             | ({str(c["topic_id"])} if c.get("topic_id") else set())),
            "score": sc.get("score"), "n_methods": sc.get("n_methods"),
            "ev_c": sc.get("ev_c"), "grade": sc.get("grade"),
            "recur_frac": sc.get("recur_frac"),
            "methods": sorted({str(m) for m in (c.get("proposed_by") or [])}),
            # The model's own reason, first sentence only. Never a generated
            # counter-case: the counter-case is the PM's job.
            "ai_reason": _first_sentence(c.get("thesis")),
            "horizon_days": c.get("horizon_days") or 30,
        })
    out = {"as_of": as_of, "run_id": run_id, "source": source,
           "n": int(meta.get("n") or config.SHORTLIST_N),
           "max_per_theme": int(meta.get("max_per_theme") or config.SHORTLIST_MAX_PER_THEME),
           "sector_cap": config.SHORTLIST_SECTOR_CAP,
           "score_source": meta.get("score_source"),
           "items": items}
    if source == "computed":
        if len(_SHORT_CACHE) > 32:
            _SHORT_CACHE.clear()
        _SHORT_CACHE[key] = out
    return out


class _PCon:
    """Just enough of a platform for `annotate_pool` when only a connection is at hand."""
    def __init__(self, con):
        self.state = type("S", (), {"connection": con})()


def latest_weekly_run(p, as_of: str | None = None) -> dict[str, Any] | None:
    if as_of:
        rows = p.state.q("SELECT run_id, as_of FROM orch_runs WHERE kind='weekly' "
                         "AND ok=1 AND as_of=? ORDER BY started_at DESC LIMIT 1", (as_of,))
    else:
        rows = p.state.q("SELECT run_id, as_of FROM orch_runs WHERE kind='weekly' "
                         "AND ok=1 ORDER BY as_of DESC, started_at DESC LIMIT 1")
    return dict(rows[0]) if rows else None


# ================================================= four-layer risk lens
def _bare(inst_id: Any) -> str:
    return str(inst_id or "").split(".")[-1].upper()


def _daily_returns(con, code: str, upto: str, n: int) -> dict[str, float]:
    rows = db.q(con, "SELECT d, close FROM prices WHERE code=? AND d<=? "
                     "ORDER BY d DESC LIMIT ?", (code, upto, n + 1))
    rows = list(reversed(rows))
    out = {}
    for a, b in zip(rows, rows[1:]):
        if a["close"] and b["close"]:
            out[b["d"]] = float(b["close"]) / float(a["close"]) - 1.0
    return out


def _corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 20:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def weightings(con, symbol: str, as_of: str) -> dict[str, Any]:
    """Cached sector / country splits for one ETF, newest on or before `as_of`.

    Falls back to the newest cached split at all, marked `after_period`: an
    ETF's sector mix moves slowly and a PM reading this week's card is better
    served by last month's split, labelled, than by a blank.
    """
    out: dict[str, Any] = {}
    for kind in ("sector", "country"):
        try:
            r = db.q1(con, "SELECT MAX(as_of) d FROM etf_weightings WHERE symbol=? "
                           "AND kind=? AND as_of<=?", (symbol, kind, as_of))
            d = r["d"] if r else None
            late = False
            if not d:
                r = db.q1(con, "SELECT MAX(as_of) d FROM etf_weightings WHERE symbol=? "
                               "AND kind=?", (symbol, kind))
                d, late = (r["d"] if r else None), True
            if not d:
                continue
            rows = db.q(con, "SELECT name, weight FROM etf_weightings WHERE symbol=? "
                             "AND kind=? AND as_of=? ORDER BY weight DESC",
                        (symbol, kind, d))
            out[kind] = {"as_of": d, "after_period": late,
                         "weights": {x["name"]: round(float(x["weight"]), 4) for x in rows}}
        except Exception:  # noqa: BLE001 — table absent on an old store: no split
            continue
    return out


def refresh_weightings(con, symbols: Iterable[str], as_of: date | None = None
                       ) -> dict[str, Any]:
    """Fetch sector and country splits from FMP into `etf_weightings`.

    `etf_lookthrough` stores holdings by ISIN and nothing about sectors, so the
    style/sector lens has nowhere to read from without this. Network; never
    called by tests or by the weekly run — an operator command.
    """
    from .sources import fmp
    d = (as_of or date.today()).isoformat()
    rep: dict[str, Any] = {}
    for s in symbols:
        got = {}
        for kind, fn in (("sector", fmp.sector_weightings),
                         ("country", fmp.country_weightings)):
            try:
                w = fn(s)
            except Exception as e:  # noqa: BLE001
                got[kind] = f"error: {type(e).__name__}"
                continue
            for name, pct in w.items():
                db.upsert(con, "etf_weightings",
                          {"symbol": s, "as_of": d, "kind": kind, "name": str(name),
                           "weight": float(pct) / 100.0}, ["symbol", "as_of", "kind", "name"])
            got[kind] = len(w)
        rep[s] = got
    return rep


def lens(con, item: dict[str, Any], as_of: str) -> dict[str, Any]:
    """What the data can fill in on the PM's four-layer checklist, per name.

    Computed only where a number exists; every other box is left for the PM
    and says 「待 PM 判断」 rather than guessing. 事件风险 is never computed —
    it is precisely the risk a model trained on text does not see coming.
    """
    from . import universe as uni
    inst = uni.resolve(str(item.get("instrument_id") or ""))
    code = getattr(inst, "futu_code", None) if inst else None
    out: dict[str, Any] = {"exposure": getattr(inst, "exposure", None) if inst else None,
                           "market": getattr(inst, "market", None) if inst else None,
                           "currency": getattr(inst, "currency", None) if inst else None,
                           "kind": getattr(inst, "kind", None) if inst else None}
    # ① 风格/行业暴露
    style: dict[str, Any] = {"themes": item.get("topics") or []}
    if inst and inst.kind == "listed" and inst.market == "US":
        style.update(weightings(con, inst.key, as_of))
        try:
            r = db.q1(con, "SELECT MAX(as_of) d FROM etf_lookthrough WHERE symbol=? "
                           "AND as_of<=?", (inst.key, as_of))
            d = r["d"] if r else None
            if d:
                style["top_holdings"] = [
                    {"label": x["label"] or x["asset"], "weight": round(float(x["weight"]), 4)}
                    for x in db.q(con, "SELECT asset, label, weight FROM etf_lookthrough "
                                       "WHERE symbol=? AND as_of=? ORDER BY weight DESC "
                                       "LIMIT 3", (inst.key, d))]
                style["holdings_as_of"] = d
        except Exception:  # noqa: BLE001
            pass
    out["style"] = style
    # ② 宏观敏感度: return correlation with three proxies
    macro: dict[str, Any] = {}
    if code:
        mine = _daily_returns(con, code, as_of, MACRO_LOOKBACK)
        for label, proxy in MACRO_PROXIES:
            if proxy == code:
                continue
            other = _daily_returns(con, proxy, as_of, MACRO_LOOKBACK)
            ds = sorted(set(mine) & set(other))
            c = _corr([mine[x] for x in ds], [other[x] for x in ds])
            macro[label] = None if c is None else round(c, 2)
        macro["sessions"] = len(mine)
    out["macro"] = macro or None
    # ④ 流动性/信用
    liq: dict[str, Any] = {}
    if code:
        rows = db.q(con, "SELECT close, volume FROM prices WHERE code=? AND d<=? "
                         "ORDER BY d DESC LIMIT ?", (code, as_of, LIQ_LOOKBACK))
        vals = [float(r["close"]) * float(r["volume"]) for r in rows
                if r["close"] and r["volume"]]
        if vals:
            liq["adv_value"] = round(sum(vals) / len(vals), 0)
            liq["adv_sessions"] = len(vals)
    elif inst and inst.olive_key:
        from .sources import olive
        m = olive.mark(con, str(inst.olive_key), as_of)
        liq["nav_d"] = m.get("nav_d") if m else None
        liq["nav_stale_days"] = m.get("stale_days") if m else None
    out["liquidity"] = liq or None
    return out


def shortlist_exposure(items: list[dict[str, Any]], lenses: dict[str, dict[str, Any]]
                       ) -> dict[str, Any]:
    """Equal-weight sector split of the shortlist and whether a sector breaches the cap.

    Only names with a cached split count, and the coverage is reported next to
    the number: 「半导体 60%」 over two of five names is a different statement
    from the same share over all five.
    """
    agg: dict[str, float] = {}
    covered = 0
    for it in items:
        sec = ((lenses.get(str(it.get("instrument_id"))) or {}).get("style") or {}).get("sector")
        w = (sec or {}).get("weights") or {}
        tot = sum(v for v in w.values() if v and v > 0)
        if tot <= 0:
            continue
        covered += 1
        for k, v in w.items():
            if v and v > 0:
                agg[k] = agg.get(k, 0.0) + v / tot
    shares = {k: round(v / covered, 4) for k, v in sorted(agg.items(), key=lambda kv: -kv[1])} \
        if covered else {}
    cap = float(config.SHORTLIST_SECTOR_CAP)
    over = [k for k, v in shares.items() if v > cap]
    themes: dict[str, int] = {}
    for it in items:
        t = str(it.get("topic_id") or "—")
        themes[t] = themes.get(t, 0) + 1
    return {"sector_shares": shares, "covered": covered, "of": len(items),
            "sector_cap": cap, "sector_over_cap": over, "theme_counts": themes}


# ============================================================ ticket
def _last_px(con, inst, as_of: str) -> tuple[str | None, float | None, str]:
    from .sources import futu_px, olive
    if inst and inst.futu_code:
        hit = futu_px.last_close_on_or_before(con, inst.futu_code, as_of)
        return (hit[0], hit[1], "收盘价") if hit else (None, None, "收盘价")
    if inst and inst.olive_key:
        hit = olive.nav_on_or_before(con, str(inst.olive_key), as_of)
        return (hit[0], hit[1], "净值") if hit else (None, None, "净值")
    return None, None, ""


def latest_decisions(con, as_of: str) -> dict[str, dict[str, Any]]:
    """The desk's current decision per instrument: the most recently updated review.

    Several reviewers can review one name; the ticket needs one status, and the
    freshest word on it is the one that moved last. Every reviewer's row is
    still returned by `reviews()`.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in reviews(con, as_of):
        k = str(r["instrument_id"])
        if k not in out or str(r["updated_at"]) > str(out[k]["updated_at"]):
            out[k] = r
    return out


def ticket(p, con, as_of: str | None = None) -> dict[str, Any]:
    """The satellite-sleeve order sheet for one period's shortlist.

    Equal weight across the names the desk has not struck: a 否决 row stays on
    the sheet (so the sheet shows what was removed and by whom) with weight 0,
    and the rest share the sleeve. Undecided and 观望 names are sized, and the
    status column is what tells the trader which is which. Shares are floored,
    and only estimated when the price is in the portfolio currency — sizing an
    HKD line off a USD amount without an FX rate would be a made-up number.
    """
    run = latest_weekly_run(p, as_of)
    if not run:
        return {"as_of": as_of, "rows": [], "error": "这一期没有成功完成的周跑"}
    sl = period_shortlist(p, con, run["run_id"], run["as_of"])
    from . import universe as uni
    try:
        uni.hydrate(con)
    except Exception:  # noqa: BLE001
        pass
    dec = latest_decisions(con, run["as_of"])
    items = sl["items"][: config.TICKET_BATCH_MAX]
    live = [it for it in items if (dec.get(str(it["instrument_id"])) or {}).get("decision") != "否决"]
    sleeve = float(config.SATELLITE_SLEEVE_PCT)
    notional = float(config.MODEL_PORTFOLIO_NOTIONAL)
    ccy = config.MODEL_PORTFOLIO_CCY
    rows = []
    for it in items:
        inst = uni.resolve(str(it["instrument_id"] or ""))
        d = dec.get(str(it["instrument_id"])) or {}
        struck = d.get("decision") == "否决"
        w_in = 0.0 if struck or not live else 1.0 / len(live)
        amount = notional * sleeve * w_in
        px_d, px, px_kind = _last_px(con, inst, run["as_of"])
        cur = getattr(inst, "currency", None) if inst else None
        shares = (math.floor(amount / px) if (px and amount > 0 and cur == ccy) else None)
        rows.append({
            "rank": it["rank"],
            "code": (inst.futu_code or inst.olive_key or inst.key) if inst else it["instrument_id"],
            "name": (inst.name if inst else None) or it.get("instrument_name"),
            "market": getattr(inst, "market", None) if inst else None,
            "category": (inst.vehicle if inst else None) or it.get("vehicle"),
            "currency": cur,
            "weight_in_sleeve": round(w_in, 6),
            "weight_total": round(w_in * sleeve, 6),
            "amount": round(amount, 2),
            "last_px": px, "last_px_d": px_d, "px_kind": px_kind,
            "est_shares": shares,
            "shares_note": (None if shares is not None else
                            ("已否决，不下单" if struck else
                             ("无价" if not px else
                              (f"价格币种 {cur} ≠ {ccy}，需换汇后再算" if cur != ccy else "金额为 0")))),
            "pm_decision": d.get("decision") or "未决定",
            "pm_reviewer": d.get("reviewer"),
            "pm_reason": d.get("reason"),
        })
    return {"as_of": run["as_of"], "run_id": run["run_id"], "source": sl["source"],
            "sleeve_pct": sleeve, "notional": notional, "currency": ccy,
            "batch_max": config.TICKET_BATCH_MAX, "rows": rows,
            "truncated": max(0, len(sl["items"]) - len(items))}


TICKET_COLUMNS = (("rank", "序号"), ("code", "代码"), ("name", "名称"), ("market", "市场"),
                  ("category", "品类"), ("weight_in_sleeve", "组合内权重"),
                  ("weight_total", "占总组合权重"), ("amount", "金额"),
                  ("currency", "币种"), ("last_px", "最新价"), ("px_kind", "价格类型"),
                  ("last_px_d", "价格日期"), ("est_shares", "估算股数"),
                  ("shares_note", "股数说明"), ("pm_decision", "PM 决定"),
                  ("pm_reviewer", "决定人"), ("pm_reason", "理由"))


def ticket_csv(t: dict[str, Any]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for _, h in TICKET_COLUMNS])
    for r in t.get("rows") or []:
        w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in TICKET_COLUMNS])
    # BOM: Excel on the desk opens a UTF-8 CSV without it as mojibake.
    return "﻿" + buf.getvalue()


# ======================================================== PM reviews
REVIEW_FIELDS = ("as_of", "instrument_id", "decision", "reason", "reject_category",
                 "lens_checks", "reviewer", "created_at", "updated_at")


def journal_path() -> Path:
    """Where review writes are journaled: beside the database, on the data mount.

    On the display node that is `/data`, which survives both the snapshot swap
    (only `ideagen.db` is replaced) and a container replacement (the mount
    outlives the container) — unlike `config.DATA`, which there is inside the
    image and is thrown away on every code deploy.
    """
    env = os.environ.get("IDEAGEN_PM_REVIEWS_FILE")
    if env:
        return Path(env)
    return Path(os.environ.get("IDEAGEN_DB") or config.DB_PATH).parent / "pm_reviews.jsonl"


def _now() -> str:
    # Microseconds: a PM changing their mind twice within a second must still
    # leave the second decision on top — merges compare this string.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def merge_rows(con, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Upsert reviews by (as_of, instrument_id, reviewer); newer `updated_at` wins.

    Order-independent and idempotent, so the journal, the export and a pulled
    copy can be replayed any number of times in any order and land on the same
    table.
    """
    st = {"inserted": 0, "updated": 0, "kept": 0, "invalid": 0}
    for r in rows:
        if not isinstance(r, dict) or not all(r.get(k) for k in
                                              ("as_of", "instrument_id", "reviewer",
                                               "decision", "updated_at")):
            st["invalid"] += 1
            continue
        if r["decision"] not in config.PM_DECISIONS:
            st["invalid"] += 1
            continue
        cur = db.q1(con, "SELECT updated_at FROM pm_reviews WHERE as_of=? AND "
                         "instrument_id=? AND reviewer=?",
                    (r["as_of"], r["instrument_id"], r["reviewer"]))
        if cur and str(cur["updated_at"]) >= str(r["updated_at"]):
            st["kept"] += 1
            continue
        lc = r.get("lens_checks")
        db.upsert(con, "pm_reviews", {
            "as_of": r["as_of"], "instrument_id": r["instrument_id"],
            "decision": r["decision"], "reason": r.get("reason") or "",
            "reject_category": r.get("reject_category"),
            "lens_checks": lc if isinstance(lc, str) or lc is None
            else json.dumps(lc, ensure_ascii=False),
            "reviewer": r["reviewer"],
            "created_at": r.get("created_at") or r["updated_at"],
            "updated_at": r["updated_at"]}, ["as_of", "instrument_id", "reviewer"])
        st["updated" if cur else "inserted"] += 1
    return st


_JOURNAL_SEEN: dict[str, tuple[float, int]] = {}


def absorb_journal(con, path: Path | None = None) -> dict[str, int] | None:
    """Replay the journal into the table if the file changed since last time.

    On the display node a snapshot swap replaces the database with one that
    may predate the last few clicks; replaying the journal on the next read
    brings them back before anyone sees them missing.
    """
    path = path or journal_path()
    try:
        stt = path.stat()
    except OSError:
        return None
    sig = (stt.st_mtime, stt.st_size)
    key = f"{id(con)}:{path}"
    if _JOURNAL_SEEN.get(key) == sig:
        return None
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue    # a torn last line from a crash is skipped, not fatal
    st = merge_rows(con, rows)
    _JOURNAL_SEEN[key] = sig
    return st


def reviews(con, as_of: str | None = None) -> list[dict[str, Any]]:
    absorb_journal(con)
    if as_of:
        rows = db.q(con, "SELECT * FROM pm_reviews WHERE as_of=? ORDER BY instrument_id, "
                         "updated_at", (as_of,))
    else:
        rows = db.q(con, "SELECT * FROM pm_reviews ORDER BY as_of, instrument_id, updated_at")
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["lens_checks"] = db.jl(d.get("lens_checks"), None)
        out.append(d)
    return out


def can_write(role: str | None) -> bool:
    return role in config.PM_REVIEW_WRITE_ROLES


def validate_review(payload: dict[str, Any], *, reviewer: str | None,
                    role: str | None) -> tuple[dict[str, Any], int]:
    """Everything about a decision that can be checked without the database.

    Split out so the server refuses an unsigned or read-only caller before it
    opens a connection. Returns (normalised fields, 200) or (error, status).
    """
    if not reviewer:
        return {"error": "需要以个人账号登录才能记录决定（决定要署名）"}, 403
    if not can_write(role):
        return {"error": "当前账号只读，不能记录决定"}, 403
    as_of = str(payload.get("as_of") or "").strip()
    try:
        date.fromisoformat(as_of)
    except ValueError:
        return {"error": "缺少期次日期或格式不对（YYYY-MM-DD）"}, 400
    inst = str(payload.get("instrument_id") or "").strip()
    if not inst:
        return {"error": "缺少标的"}, 400
    decision = str(payload.get("decision") or "").strip()
    if decision not in config.PM_DECISIONS:
        return {"error": f"决定只能是 {' / '.join(config.PM_DECISIONS)}"}, 400
    reason = str(payload.get("reason") or "").strip()
    if len(reason) > 280:
        return {"error": "理由限 280 字以内，一句话即可"}, 400
    cat = payload.get("reject_category")
    cat = str(cat).strip() if cat else None
    if decision == "否决":
        if cat not in config.PM_REJECT_CATEGORIES:
            return {"error": "否决要选一个理由类别："
                             + " / ".join(config.PM_REJECT_CATEGORIES)}, 400
        if not reason:
            return {"error": "否决要写一句理由"}, 400
    elif cat is not None and cat not in config.PM_REJECT_CATEGORIES:
        return {"error": "理由类别不在清单里"}, 400
    checks = payload.get("lens_checks")
    if checks is not None:
        if not isinstance(checks, dict):
            return {"error": "复核清单格式不对"}, 400
        checks = {k: v for k, v in checks.items()
                  if k in config.PM_LENSES and isinstance(v, (bool, str))}
    return {"as_of": as_of, "instrument_id": inst, "decision": decision,
            "reason": reason, "reject_category": cat, "lens_checks": checks}, 200


def submit_review(p, con, payload: dict[str, Any], *, reviewer: str | None,
                  role: str | None, journal: Path | None = None
                  ) -> tuple[dict[str, Any], int]:
    """Validate and store one PM decision. Returns (body, http status)."""
    v, status = validate_review(payload, reviewer=reviewer, role=role)
    if status != 200:
        return v, status
    as_of, inst, decision = v["as_of"], v["instrument_id"], v["decision"]
    reason, cat, checks = v["reason"], v["reject_category"], v["lens_checks"]
    run = latest_weekly_run(p, as_of)
    if not run:
        return {"error": "这一期没有成功完成的周跑，没有精选可审"}, 404
    sl = period_shortlist(p, con, run["run_id"], as_of)
    names = {str(it["instrument_id"]) for it in sl["items"]}
    if inst not in names:
        # The rule that makes this a review and not a second selector.
        return {"error": "只能对本期精选里已有的标的做决定——PM 负责剔除，不往里加名字",
                "shortlist": sorted(names)}, 400
    absorb_journal(con, journal)
    now = _now()
    cur = db.q1(con, "SELECT created_at FROM pm_reviews WHERE as_of=? AND instrument_id=? "
                     "AND reviewer=?", (as_of, inst, reviewer))
    row = {"as_of": as_of, "instrument_id": inst, "decision": decision, "reason": reason,
           "reject_category": cat, "lens_checks": checks, "reviewer": reviewer,
           "created_at": cur["created_at"] if cur else now, "updated_at": now}
    merge_rows(con, [row])
    jp = journal or journal_path()
    try:
        jp.parent.mkdir(parents=True, exist_ok=True)
        with jp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        journaled = True
    except OSError as e:
        # The row is in the table; say that it will not survive a snapshot swap.
        journaled = False
        row["journal_error"] = f"{type(e).__name__}: {e}"[:160]
    return {"ok": True, "review": row, "journaled": journaled}, 200


def pull_reviews(con, url: str | None = None, key: str | None = None,
                 timeout: float = 20.0) -> dict[str, Any]:
    """Fetch the display node's reviews and merge them into this database."""
    import urllib.request
    base = (url or config.DISPLAY_NODE_URL).rstrip("/")
    req = urllib.request.Request(f"{base}/api/pm_reviews/export",
                                 headers={"Accept": "application/json",
                                          **({"X-Dash-Key": key} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    rows = body.get("reviews") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"导出接口没有返回 reviews 列表：{str(body)[:120]}")
    st = merge_rows(con, rows)
    return {"source": base, "received": len(rows), **st}


# ======================================================= outcomes
def _px_on_or_before(con, inst, d: str) -> tuple[str, float] | None:
    from .sources import futu_px, olive
    if inst and inst.futu_code:
        return futu_px.last_close_on_or_before(con, inst.futu_code, d)
    if inst and inst.olive_key:
        return olive.nav_on_or_before(con, str(inst.olive_key), d)
    return None


def review_outcomes(con, today: str | None = None) -> dict[str, Any]:
    """Did the PM's strikes help? Return from the period close to horizon end, by decision.

    One outcome per (period, instrument), using the desk's latest decision, so
    two reviewers agreeing on a name do not count its return twice. A name is
    matured when a price exists on or after the horizon end; before that it is
    pending, never a zero. Groups under `PM_REVIEW_MIN_N` say 样本不足 and
    carry no verdict.
    """
    from . import universe as uni
    try:
        uni.hydrate(con)
    except Exception:  # noqa: BLE001
        pass
    all_rows = reviews(con)
    desk: dict[tuple[str, str], dict[str, Any]] = {}
    for r in all_rows:
        k = (str(r["as_of"]), str(r["instrument_id"]))
        if k not in desk or str(r["updated_at"]) > str(desk[k]["updated_at"]):
            desk[k] = r
    groups: dict[str, dict[str, Any]] = {d: {"decision": d, "n": 0, "pending": 0,
                                             "rets": [], "hits": 0}
                                         for d in config.PM_DECISIONS}
    detail = []
    for (as_of, inst_id), r in sorted(desk.items()):
        inst = uni.resolve(inst_id)
        end = (date.fromisoformat(as_of) + timedelta(days=30)).isoformat()
        g = groups[r["decision"]]
        entry = _px_on_or_before(con, inst, as_of)
        exit_ = _px_on_or_before(con, inst, end)
        matured = bool(entry and exit_ and exit_[0] > entry[0]
                       and (today is None or end <= today)
                       and _has_px_after(con, inst, end))
        ret = (exit_[1] / entry[1] - 1.0) * 100.0 if matured and entry[1] else None
        if ret is None:
            g["pending"] += 1
        else:
            g["n"] += 1
            g["rets"].append(ret)
            g["hits"] += 1 if ret > 0 else 0
        detail.append({"as_of": as_of, "instrument_id": inst_id, "decision": r["decision"],
                       "reject_category": r.get("reject_category"),
                       "reviewer": r.get("reviewer"), "horizon_end": end,
                       "ret_pct": None if ret is None else round(ret, 3)})
    min_n = int(config.PM_REVIEW_MIN_N)
    out_groups = []
    for d in config.PM_DECISIONS:
        g = groups[d]
        n = g["n"]
        out_groups.append({"decision": d, "n": n, "pending": g["pending"],
                           "hit_rate": round(g["hits"] / n, 4) if n else None,
                           "mean_ret_pct": round(sum(g["rets"]) / n, 3) if n else None,
                           "enough": n >= min_n})
    a = next(x for x in out_groups if x["decision"] == "采纳")
    v = next(x for x in out_groups if x["decision"] == "否决")
    spread = (round(a["mean_ret_pct"] - v["mean_ret_pct"], 3)
              if a["n"] and v["n"] else None)
    enough = a["enough"] and v["enough"]
    return {"groups": out_groups, "n_reviews": len(all_rows), "n_names": len(desk),
            "min_n": min_n, "adopt_minus_reject_pp": spread,
            "state": ("insufficient" if not enough else
                      ("helps" if (spread or 0) > 0 else "no_help")),
            "horizon_days": 30, "detail": detail[-200:]}


def _has_px_after(con, inst, d: str) -> bool:
    """A close (or NAV) dated on or after `d` exists, i.e. the horizon has been seen."""
    try:
        if inst and inst.futu_code:
            r = db.q1(con, "SELECT 1 x FROM prices WHERE code=? AND d>=? LIMIT 1",
                      (inst.futu_code, d))
        elif inst and inst.olive_key:
            r = db.q1(con, "SELECT 1 x FROM navs WHERE olive_key=? AND d>=? LIMIT 1",
                      (str(inst.olive_key), d))
        else:
            return False
    except Exception:  # noqa: BLE001
        return False
    return bool(r)


# ================================================== the panel block
def panel_block(p, con, run_id: str, as_of: str, cands: list[dict[str, Any]],
                hide_licensed: bool = False) -> dict[str, Any]:
    """Everything the panel's shortlist section reads, for one period."""
    sl = period_shortlist(p, con, run_id, as_of, cands)
    from . import universe as uni
    try:
        uni.hydrate(con)
    except Exception:  # noqa: BLE001
        pass
    lenses = {}
    for it in sl["items"]:
        try:
            lenses[str(it["instrument_id"])] = lens(con, it, as_of)
        except Exception as e:  # noqa: BLE001 — one name's lens must not blank the list
            lenses[str(it["instrument_id"])] = {"error": f"{type(e).__name__}: {e}"[:120]}
    items = [{**it, "lens": lenses.get(str(it["instrument_id"]))} for it in sl["items"]]
    # Per-candidate flags for the pool table. Stored payloads from runs before
    # the NAV gate carry no flag; it is derived here the same way, read-only.
    ranks = {str(it["candidate_id"]): it["rank"] for it in sl["items"]}
    flags = []
    for c in cands:
        f = {"shortlist_rank": ranks.get(str(c.get("id")))}
        if "stale_nav_excluded" in c:
            f.update({k: c.get(k) for k in ("stale_nav_excluded", "nav_d", "nav_stale_days")})
        else:
            try:
                fr = nav_freshness(con, date.fromisoformat(as_of), c)
            except Exception:  # noqa: BLE001
                fr = {}
            f.update({"stale_nav_excluded": bool(fr.get("stale_nav_excluded")),
                      "nav_d": fr.get("nav_d"), "nav_stale_days": fr.get("nav_stale_days")})
        flags.append(f)
    block = {**sl, "items": items, "exposure": shortlist_exposure(sl["items"], lenses),
             "lenses": list(config.PM_LENSES),
             "reject_categories": list(config.PM_REJECT_CATEGORIES),
             "decisions": list(config.PM_DECISIONS),
             "reviews": reviews(con, as_of),
             "pool_flags": flags,
             "fund_nav_fresh_days": config.FUND_NAV_FRESH_DAYS,
             "ticket": {"sleeve_pct": config.SATELLITE_SLEEVE_PCT,
                        "notional": config.MODEL_PORTFOLIO_NOTIONAL,
                        "currency": config.MODEL_PORTFOLIO_CCY,
                        "batch_max": config.TICKET_BATCH_MAX}}
    if hide_licensed:
        from . import shelf_store
        for it in block["items"]:
            it["instrument_id"] = shelf_store.public_alias(it.get("instrument_id"))
            it["instrument_name"] = "Licensed shelf instrument"
            it["ai_reason"] = None
        block["reviews"] = []
    return block
