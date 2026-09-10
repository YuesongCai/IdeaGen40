"""EOD daily bars from FMP — the price source that needs no local OpenD.

`futu_px` pulls forward-adjusted bars from a desktop OpenD gateway, so the daily
and the cloud monitor could only mark books on a machine with OpenD running. That
is why "Mac off" meant "nothing marks": the cloud sandbox has no OpenD, so
`scheduler._warm_prices` skipped, and every position sat unmarked.

FMP's `historical-price-eod/dividend-adjusted` is verified live on this key,
covers the whole US+HK ETF/equity universe (`universe.priceable_codes`), and is
back-adjusted the same way OpenD's 前复权 is — so a code's series can carry on in
the same convention when the source changes. Olive funds (L*/F*/ISIN) are not on
FMP; they keep the NAV path (`olive_nav`), unchanged.

Bars are gap-filled per code — fetched only from the day after the latest stored
bar — so FMP never rewrites a bar another source already wrote, and a code's
history does not jump at the point the source changed. `complete_through` and
`_drop_incomplete` are reused from `futu_px`: both are pure calendar arithmetic
and touch no gateway, so the "never write a live, unclosed bar" rule is identical
to the OpenD path.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Sequence

from .. import db
from . import fmp, futu_px

#: Marks FMP-sourced rows in `prices.src`, distinct from `futu:qfq`, so a later
#: audit can see which bars came from which gateway.
SRC = "fmp:adj"

#: The dividend-adjusted EOD endpoint (verified 2026-09-10). The plain
#: `historical-price-eod/full` returns a raw `close` with no adjustment, which
#: would drift from the stored 前复权 series across a dividend; the adjusted
#: endpoint returns adjOpen/adjHigh/adjLow/adjClose on the same convention.
_ENDPOINT = "historical-price-eod/dividend-adjusted"


def _fmp_symbol(code: str) -> str | None:
    """Internal code → FMP symbol, or None when FMP does not carry it.

    `US.SPY` → `SPY`. `HK.02800` → `2800.HK`, `HK.00700` → `0700.HK` (FMP wants
    the 4-digit HK number; the stored code zero-pads to 5, so strip then re-pad).
    Funds and ISIN keys (no market prefix, or an Olive `L*/F*` key) return None —
    they are priced from NAV, not here.
    """
    if not code or "." not in code:
        return None
    market, rest = code.split(".", 1)
    if market == "US":
        return rest or None
    if market == "HK":
        digits = rest.lstrip("0") or "0"
        return f"{digits.zfill(4)}.HK"
    return None


def configured() -> bool:
    return fmp.configured()


def fetch_daily(codes: Sequence[str], start: date, end: date,
                verbose: bool = False) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Back-adjusted daily bars. Returns ({code: [bar,...]}, {code: error})."""
    out: dict[str, list[dict]] = {}
    fail: dict[str, str] = {}
    for code in dict.fromkeys(c for c in codes if c):
        sym = _fmp_symbol(code)
        if not sym:
            fail[code] = "no FMP symbol (fund/ISIN priced from NAV)"
            continue
        try:
            raw = fmp._get(_ENDPOINT, symbol=sym,
                           **{"from": start.isoformat(), "to": end.isoformat()})
        except Exception as e:  # noqa: BLE001 — one bad symbol must not abort the run
            fail[code] = f"{type(e).__name__}: {e}"[:200]
            continue
        rows: list[dict] = []
        for r in (raw or []):
            d = str(r.get("date") or "")[:10]
            close = r.get("adjClose")
            if not d or close is None:
                continue
            close = float(close)
            rows.append({
                "code": code, "d": d,
                "open": float(r.get("adjOpen") if r.get("adjOpen") is not None else close),
                "high": float(r.get("adjHigh") if r.get("adjHigh") is not None else close),
                "low": float(r.get("adjLow") if r.get("adjLow") is not None else close),
                "close": close, "volume": float(r.get("volume") or 0), "src": SRC,
            })
        rows.sort(key=lambda x: x["d"])
        rows = futu_px._drop_incomplete(code, rows)   # calendar clamp, no gateway
        out[code] = rows
        if verbose:
            last = rows[-1]["d"] if rows else "-"
            print(f"    {code:<12} n={len(rows):<4} last={last} (fmp)")
    return out, fail


def sync(con, codes: Iterable[str], start: date, end: date,
         verbose: bool = False, **_ignored) -> dict:
    """Gap-fill `prices` from FMP, skipping codes already complete for the range.

    Mirrors `futu_px.sync`'s contract (skip-complete, clamp to the last closed
    session, per-code errors that never abort the pass) with one deliberate
    difference: each code is fetched only from the day after its latest stored
    bar, so FMP adds to a series instead of overwriting whatever wrote it before.
    """
    codes = list(dict.fromkeys(c for c in codes if c))
    limits: dict[str, str] = {}
    need: dict[str, date] = {}
    for c in codes:
        if not _fmp_symbol(c):
            continue                          # fund/ISIN → NAV path, not FMP
        market = futu_px.market_of(c)
        if market not in limits:
            limits[market] = futu_px.complete_through(market)
        want_end = min(end.isoformat(), limits[market])
        row = db.q1(con, "SELECT MAX(d) mx, MIN(d) mn FROM prices WHERE code=?", (c,))
        cstart = start
        if row and row["mx"]:
            if row["mx"] >= want_end and (not row["mn"] or row["mn"] <= start.isoformat()):
                continue                      # already covers the requested range
            if row["mx"] >= start.isoformat():
                cstart = date.fromisoformat(row["mx"]) + timedelta(days=1)
        need[c] = cstart

    if not need:
        return {"requested": len(codes), "fetched": 0, "rows": 0, "errors": {},
                "source": "fmp", "complete_through": limits}

    rows = 0
    fetched = 0
    errors: dict[str, str] = {}
    for c, cstart in need.items():
        if cstart > end:
            continue
        bars, fail = fetch_daily([c], cstart, end, verbose=verbose)
        if c in fail:
            errors[c] = fail[c]
            continue
        bl = bars.get(c) or []
        rows += db.upsert_many(con, "prices", bl, ["code", "d"])
        fetched += 1
    return {"requested": len(codes), "fetched": fetched, "rows": rows,
            "errors": errors, "source": "fmp", "complete_through": limits}
