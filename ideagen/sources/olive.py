"""Olive (Nexus HK) product shelf.

Olive is reachable only over MCP, which means only an interactive/agent session
can talk to it — a bare cron process cannot. Since the daily run is driven by
Claude Code anyway (that is the generator), the split is:

    Claude Code session  ──MCP──>  Olive        (search shelf, read NAV)
             │
             └── writes a JSON snapshot ──> `ideagen olive-ingest`
                                              │
                                              └── instruments + navs tables

Two consequences worth stating plainly:

* Olive publishes a *latest* NAV, not a daily history (`get_fund_nav_chart`
  aggregates the series server-side before returning it). Snapshotting daily is
  therefore the only way to accumulate a genuine daily NAV series — which is
  exactly what a 30-day forward paper-trade does. Funds entering the book on day
  1 have a full daily NAV path by day 30; nothing is back-filled or interpolated.
* A fund position is marked with an explicit staleness count. If Olive did not
  publish, the mark is carried forward and flagged, never silently smoothed.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .. import config, db

SNAPSHOT_DIR = config.SNAPSHOTS
MAX_NAV_STALE_DAYS = 10


# ---------------------------------------------------------------- ingest
def ingest(con, payload: dict | list, as_of: date | None = None,
           verbose: bool = True) -> dict:
    """Ingest an Olive snapshot captured by the agent session.

    Accepted shapes (all tolerated so the agent can paste tool output directly):
        {"funds":[{...}], "cash":[{...}], "structured":[{...}], "private":[{...}]}
        [{...}, {...}]                      # a flat list of product cards
        {"items":[{...}]}                   # a single MCP tool result
    """
    as_of = as_of or config.today_hkt()
    groups = _as_groups(payload)
    now = config.now_hkt().isoformat()
    inst_rows: list[dict] = []
    nav_rows: list[dict] = []
    skipped = 0

    for group, items in groups.items():
        for it in items:
            rec = _normalise(group, it)
            if rec is None:
                skipped += 1
                continue
            inst_rows.append({
                "key": rec["key"], "kind": rec["kind"], "futu_code": None,
                "olive_key": rec["key"], "name": rec["name"],
                "market": "OLIVE", "currency": rec["currency"], "priceable": 0,
                "meta": {k: v for k, v in rec.items()
                         if k not in ("key", "kind", "name", "currency", "nav", "nav_d")},
                "updated_at": now,
            })
            if rec.get("nav") is not None:
                nav_rows.append({"olive_key": rec["key"],
                                 "d": rec.get("nav_d") or as_of.isoformat(),
                                 "nav": float(rec["nav"]), "src": f"olive:{group}"})

    n_i = db.upsert_many(con, "instruments", inst_rows, ["key"])
    n_n = db.upsert_many(con, "navs", nav_rows, ["olive_key", "d"])

    snap = SNAPSHOT_DIR / f"olive_{as_of.isoformat()}.json"
    snap.write_text(json.dumps(
        {"as_of": as_of.isoformat(), "captured_at": now,
         "counts": {g: len(v) for g, v in groups.items()},
         "payload": groups}, ensure_ascii=False, indent=1), encoding="utf-8")

    rep = {"as_of": as_of.isoformat(), "instruments": n_i, "navs": n_n,
           "skipped": skipped, "groups": {g: len(v) for g, v in groups.items()},
           "snapshot": str(snap)}
    db.kv_set(con, f"olive:{as_of.isoformat()}", rep)
    if verbose:
        print(f"  ✓ olive  instruments={n_i} navs={n_n} skipped={skipped} "
              f"groups={rep['groups']}")
    return rep


def _as_groups(payload: Any) -> dict[str, list[dict]]:
    known = ("funds", "public", "cash", "structured", "private", "underlying")
    if isinstance(payload, dict):
        if any(k in payload for k in known):
            return {k: _as_list(v) for k, v in payload.items() if k in known}
        if "items" in payload:
            return {"funds": _as_list(payload["items"])}
        return {"funds": _as_list(payload)}
    return {"funds": _as_list(payload)}


def _as_list(v: Any) -> list[dict]:
    """Unwrap the several envelopes Olive MCP tools use, including the
    JSON-encoded-string-inside-a-text-block form."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        # tool output sometimes arrives as `ToolName\n\n"{...escaped json...}"`
        m = re.search(r'"(\{.*\})"\s*$', s, re.S)
        if m:
            try:
                s = json.loads(f'"{m.group(1)}"')
            except ValueError:
                s = m.group(1)
        else:
            i = s.find("{")
            j = s.find("[")
            k = min([x for x in (i, j) if x >= 0], default=-1)
            if k > 0:
                s = s[k:]
        try:
            v = json.loads(s)
        except ValueError:
            return []
    if isinstance(v, dict):
        for key in ("items", "list", "data", "records", "cards"):
            if isinstance(v.get(key), list):
                return [x for x in v[key] if isinstance(x, dict)]
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


_PCT = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*%\s*$")


def _pct(v: Any) -> float | None:
    if v in (None, "", "--"):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 100.0
    m = _PCT.match(str(v))
    return float(m.group(1)) / 100.0 if m else None


def _num(v: Any) -> float | None:
    if v in (None, "", "--"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalise(group: str, it: dict) -> dict | None:
    key = (it.get("productCode") or it.get("detailKey") or it.get("batchCode")
           or it.get("isinCode") or it.get("code"))
    if not key:
        return None
    name = (it.get("productEnglishName") or it.get("name") or it.get("productName")
            or it.get("fundName") or str(key))
    kind = "structured" if group == "structured" else "fund"
    perf = it.get("performanceMap") or it.get("metrics") or {}
    rec: dict[str, Any] = {
        "key": str(key), "kind": kind, "name": str(name)[:160],
        "currency": it.get("currency") or "USD",
        "group": group,
        "asset_class": it.get("assetClass"),
        "risk_level": it.get("riskLevel"),
        "strategy": it.get("strategy"),
        "nav": _num(it.get("latestNav") if it.get("latestNav") is not None else it.get("nav")),
        "nav_d": (it.get("navDate") or it.get("asOf") or "")[:10] or None,
        "yield7d": _pct(it.get("hebdomad")),
        "ret1m": _pct(perf.get("1month") or perf.get("ret1m")),
        "ret1y": _pct(perf.get("1year") or perf.get("ret1y")),
        "ytd": _pct(perf.get("ytd")),
        "since": _pct(perf.get("sinceLaunch")),
        "house": it.get("fundHouseNameDesc"),
        "min_initial": _num(it.get("minimumInitialInvestAmount")),
        "detail_url": it.get("detailUrl"),
    }
    if group == "structured":
        rec.update({"underlying": it.get("underlying") or it.get("underlyings"),
                    "coupon": _pct(it.get("rate") or it.get("coupon")),
                    "term": it.get("term"), "status": it.get("recruitmentStatus")})
    return rec


# ---------------------------------------------------------------- marking
def nav_on_or_before(con, olive_key: str, d: str) -> tuple[str, float] | None:
    r = db.q1(con, "SELECT d, nav FROM navs WHERE olive_key=? AND d<=? ORDER BY d DESC LIMIT 1",
              (olive_key, d))
    return (r["d"], float(r["nav"])) if r else None


def mark(con, olive_key: str, d: str) -> dict | None:
    """Mark a fund position. Returns the NAV plus how stale it is, so the caller
    can decide (and the report can disclose) rather than guess."""
    hit = nav_on_or_before(con, olive_key, d)
    if hit is None:
        return None
    nav_d, nav = hit
    stale = (date.fromisoformat(d) - date.fromisoformat(nav_d)).days
    return {"olive_key": olive_key, "d": d, "nav_d": nav_d, "nav": nav,
            "stale_days": stale, "usable": stale <= MAX_NAV_STALE_DAYS}


def cash_yield(con, currency: str = "USD") -> float | None:
    """Median 7-day annualised money-market yield on the shelf, per currency.

    This replaces the hard-coded risk-free constant in the hurdle: the account's
    actual cash alternative is what a tactical trade has to beat. The median is
    used deliberately — the top-of-shelf yield is a marketing artefact.
    """
    rows = db.q(con, "SELECT currency, meta FROM instruments WHERE market='OLIVE'")
    ys = []
    for r in rows:
        meta = db.jl(r["meta"], {}) or {}
        if meta.get("group") == "cash" and meta.get("yield7d") and r["currency"] == currency:
            ys.append(float(meta["yield7d"]))
    if not ys:
        return None
    ys.sort()
    return ys[len(ys) // 2]


def shelf(con, group: str | None = None, limit: int = 400) -> list[dict]:
    rows = db.q(con, "SELECT key,name,currency,meta FROM instruments WHERE market='OLIVE'")
    out = []
    for r in rows:
        meta = db.jl(r["meta"], {}) or {}
        if group and meta.get("group") != group:
            continue
        out.append({"key": r["key"], "name": r["name"], "ccy": r["currency"], **meta})
    return out[:limit]


def coverage(con) -> dict:
    r = db.q1(con, "SELECT COUNT(DISTINCT olive_key) k, COUNT(*) n, MIN(d) a, MAX(d) b FROM navs")
    return {"keys": r["k"], "nav_rows": r["n"], "from": r["a"], "to": r["b"]}
