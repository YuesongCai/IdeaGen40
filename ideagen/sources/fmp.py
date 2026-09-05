"""Financial Modeling Prep: the look-through source.

What this port exists for is one thing the other three cannot do. Futu prices an
instrument, Olive lists a shelf, Wisburg supplies the argument — none of them can
say *what an ETF actually owns*. Without that, the universe is a list of labels,
and a label is an assertion by whoever typed it rather than a fact about the
security. `universe.py` carries one hand-written `exposure` string per ticker;
that string is what the generator matches a thesis against, so two products with
the same string are indistinguishable to it no matter how differently they are
built. USMV and SPLV both read 美股低波动 and overlap 27% by weight. ITA, PPA and
XAR all read 国防军工 and deliver 53.7%, 45.0% and 26.1% of the same basket.

Entitlement, measured 2026-09-05 rather than assumed
----------------------------------------------------
The legacy `/api/v3` and `/api/v4` trees are **gone** for keys issued after
2025-08-31 — they answer 200 with an `Error Message` body, which is the failure
mode most likely to be mistaken for data. Everything here therefore uses the
`/stable` tree, and `_get` treats an `Error Message` object as an error even
though the transport succeeded.

Verified live on this key: etf/holdings, etf/asset-exposure,
etf/sector-weightings, etf/country-weightings, quote, historical-price-eod/full,
etf-list, profile, company-screener, ratios, key-metrics, sp500-constituent.
Verified *absent*: etf/info (empty), search-symbol (near-useless for themes —
querying "quality" returns an Indian mutual fund, which is the name-matching
failure this module exists to replace, arriving from the vendor's side).

Identity: ISIN, not ticker
--------------------------
The obvious key is the `asset` ticker, and it is the wrong one twice over. Bond
funds leave it blank — every one of TLT's 49 rows, all of LQD's — so keying on it
throws away the entire rates and credit half of this universe as "opaque". And
across equity funds the same issuer arrives under different strings: ASML in a
US fund, ASML.AS in a European one, which silently understates their overlap.

`isin` is present on 98.5% of weighted rows here, bonds included, and is one
identifier per security by construction. So it is the key, with the ticker or the
vendor's description kept only for display. What remains unidentified after that
is unidentified in the source: futures and physical rows carry no ISIN, no CUSIP
and no ticker.

Coverage is not uniform, and the gap is structural
--------------------------------------------------
Futures-backed and physically-backed products return rows with no identifier at
all: DBC reports its collateral money-market fund at 89% and its actual commodity
futures as unnamed lines, GLD returns a single blank row for bullion, DBMF
returns eighteen blank swap lines. Read naively, DBC and PDBC — both broad
commodity baskets — score 0% overlap, which is worse than having no answer.

So every function that reports a look-through also reports the fraction of NAV it
could actually identify. A caller that ignores coverage will conclude two
commodity funds are unrelated, and that is a wrong answer wearing a number's
clothes. `lookthrough.py` refuses to compare below a coverage floor for exactly
this reason.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import config

BASE = "https://financialmodelingprep.com/stable"

#: Wide enough for IWM (2,000 rows, ~22s observed) and AGG. The default socket
#: timeout returns a truncated body on those, which json parses as an error a
#: long way from its cause.
TIMEOUT_S = 90


class FMPError(RuntimeError):
    """The vendor answered, and the answer was not data."""


def configured() -> bool:
    import os
    return bool(os.environ.get("FMP_API_KEY", "").strip())


def _key() -> str:
    return config.require(
        "FMP_API_KEY",
        "Financial Modeling Prep key; /stable endpoints only")


def _get(path: str, **params: Any) -> Any:
    """One `/stable` call, with the vendor's 200-shaped errors raised.

    Three distinct failures arrive as HTTP 200 here and each would otherwise be
    read as a legitimate empty result:

      * a legacy endpoint, answering with `{"Error Message": "Legacy Endpoint…"}`
      * an out-of-entitlement endpoint, same shape
      * a rate-limit rejection, same shape

    Treating any of them as "no holdings" is how a look-through silently becomes
    a label lookup again. Only a genuine `[]` — the vendor's answer for a symbol
    that is not a fund — comes back as an empty list.
    """
    params["apikey"] = _key()
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
                body = json.loads(r.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < 3:
                time.sleep(1.5 * attempt)
    else:
        raise FMPError(f"{path} 三次都没拿到（{last}）") from last

    if isinstance(body, dict):
        msg = body.get("Error Message") or body.get("error")
        if msg:
            redacted = str(msg).replace(params["apikey"], "***")
            raise FMPError(f"{path}: {redacted}")
    return body


# --------------------------------------------------------------- holdings
def holdings(symbol: str) -> tuple[dict[str, float], dict[str, str], float, int]:
    """One ETF's constituents, as (weights, labels, coverage, rows_seen).

    `weights` maps a canonical security id to its share of NAV, normalised to the
    identified portion so the numbers compare across funds. `labels` maps the
    same ids to something a person can read. `coverage` is the identified share
    of reported weight — the honesty term. `rows_seen` is how many rows the
    vendor returned at all, which separates "this is not a fund" (0) from "this
    is a fund we cannot see into" (18 blank swap lines).

    Weights are summed rather than assigned because a holdings file legitimately
    lists the same security twice — different lots, or a name appearing in both
    the index sleeve and the cash sleeve.
    """
    rows = _get("etf/holdings", symbol=symbol) or []
    named: dict[str, float] = {}
    labels: dict[str, str] = {}
    total = 0.0
    for h in rows:
        w = h.get("weightPercentage")
        if not isinstance(w, (int, float)) or w <= 0:
            continue
        total += float(w)
        key = ((h.get("isin") or "").strip()
               or (h.get("asset") or "").strip()
               or (h.get("securityCusip") or "").strip())
        if not key:
            continue
        named[key] = named.get(key, 0.0) + float(w)
        # Prefer the ticker as the display name; a fund that reports one is
        # naming an equity, and "NVDA" reads better than "NVIDIA CORP" beside a
        # weight. Bonds have only the description, which is what they are.
        if key not in labels or (h.get("asset") or "").strip():
            labels[key] = ((h.get("asset") or "").strip()
                           or (h.get("name") or "").strip() or key)
    if not total:
        return {}, {}, 0.0, len(rows)
    identified = sum(named.values())
    if not identified:
        return {}, {}, 0.0, len(rows)
    return ({k: v / identified for k, v in named.items()}, labels,
            identified / total, len(rows))


def asset_exposure(symbol: str) -> list[dict[str, Any]]:
    """Every fund the vendor knows of that holds `symbol` — the reverse index.

    Useful for discovery beyond a curated universe, and useless without a filter:
    the answer for NVDA opens with three Canadian covered-call listings. Callers
    that care about what they can trade must intersect with their own universe,
    which is why `lookthrough.py` builds the forward matrix instead and keeps
    this for widening the shelf.
    """
    return _get("etf/asset-exposure", symbol=symbol) or []


def sector_weightings(symbol: str) -> dict[str, float]:
    return {r["sector"]: float(r["weightPercentage"])
            for r in (_get("etf/sector-weightings", symbol=symbol) or [])
            if r.get("sector") is not None}


def country_weightings(symbol: str) -> dict[str, float]:
    """Country splits. The vendor returns these as `"95.87%"` strings, unlike
    sector weights which are floats — same endpoint family, two encodings."""
    out: dict[str, float] = {}
    for r in _get("etf/country-weightings", symbol=symbol) or []:
        raw = str(r.get("weightPercentage") or "").strip().rstrip("%")
        try:
            out[r["country"]] = float(raw)
        except (ValueError, KeyError):
            continue
    return out


def profile(symbol: str) -> dict[str, Any]:
    rows = _get("profile", symbol=symbol) or []
    return rows[0] if rows else {}
