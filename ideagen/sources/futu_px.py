"""Futu OpenD price layer.

Daily bars are the marking backbone: entry-band fills, stop/take touches and
mark-to-market all read from `prices`. OpenD is a local gateway, so a run fails
loudly and early if it is not up — silently marking a book with stale prices is
worse than not marking it.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Sequence

from .. import config, db


class OpenDUnavailable(RuntimeError):
    pass


@contextmanager
def quote_ctx() -> Iterator["object"]:
    try:
        from futu import OpenQuoteContext
    except ImportError as e:  # pragma: no cover
        raise OpenDUnavailable(f"futu-api not installed: {e}") from e
    try:
        ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
    except Exception as e:  # noqa: BLE001
        raise OpenDUnavailable(
            f"cannot reach Futu OpenD at {config.FUTU_HOST}:{config.FUTU_PORT} ({e}). "
            "Start Futu_OpenD and log in."
        ) from e
    try:
        yield ctx
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001, S110
            pass


def health() -> dict:
    """Cheap liveness probe used by the daily runner before anything else."""
    try:
        with quote_ctx() as ctx:
            from futu import RET_OK

            ret, data = ctx.get_market_snapshot(["US.SPY"])
            if ret != RET_OK:
                return {"ok": False, "error": str(data)[:200]}
            row = data.iloc[0]
            return {"ok": True, "probe": "US.SPY", "last": float(row["last_price"]),
                    "update_time": str(row.get("update_time"))}
    except OpenDUnavailable as e:
        return {"ok": False, "error": str(e)}


def market_of(code: str) -> str:
    return code.split(".", 1)[0] if "." in code else "US"


def priceable(code: str) -> bool:
    return market_of(code) in config.PRICEABLE_MARKETS


# ---------------------------------------------------------------- session guard
# A daily bar for date D is only usable once D's session has closed. OpenD hands
# back a live, partially-formed bar during the session, and letting that reach
# the fill engine would leak intraday information into an entry decision and
# produce marks that change after the fact.
_CLOSE_HOUR = {"US": (16, 15, "America/New_York"), "HK": (16, 30, "Asia/Hong_Kong")}


def complete_through(market: str = "US", now: datetime | None = None) -> str:
    """Latest date whose `market` session is finished and actually traded.

    Stepping back one calendar day from the cutoff is not enough: run this before
    the close on a Monday and it lands on Sunday, which has no session at all. That
    is not a harmless off-by-one — this value is the ceiling on how far the book may
    be marked, so a weekend date makes every position look unmarked, and it is the
    same ceiling that filters incoming bars.

    Weekends are excluded by calendar arithmetic, which needs no data and is always
    right. Exchange holidays are NOT handled: they cannot be derived and would need
    a real holiday calendar as a feed. On a holiday this therefore still returns a
    non-trading date, which leaves the book reporting as one session stale rather
    than marking against a price that does not exist — the safe direction of the two.
    """
    from zoneinfo import ZoneInfo

    hh, mm, tzname = _CLOSE_HOUR.get(market, _CLOSE_HOUR["US"])
    tz = ZoneInfo(tzname)
    local = (now or datetime.now(tz)).astimezone(tz)
    cutoff = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    d = local.date() if local >= cutoff else local.date() - timedelta(days=1)
    while d.weekday() >= 5:                  # 5 Saturday, 6 Sunday
        d -= timedelta(days=1)
    return d.isoformat()


def _drop_incomplete(code: str, rows: list[dict]) -> list[dict]:
    limit = complete_through(market_of(code))
    return [r for r in rows if r["d"] <= limit]


# ---------------------------------------------------------------- quota
# OpenD caps the number of *distinct* symbols a subscription may pull daily
# history for in a rolling 30-day window. Codes that trip it are recorded so the
# generator stops offering an instrument the book cannot mark, and so a later
# sync can clear the flag once quota frees up.
QUOTA_KEY = "px:quota_blocked"
_QUOTA_MARKERS = ("quota", "额度")


def _is_quota_error(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in _QUOTA_MARKERS)


def quota_blocked(con) -> set[str]:
    return set(db.kv_get(con, QUOTA_KEY, []) or [])


def _record_quota(con, blocked: Iterable[str], cleared: Iterable[str] = ()) -> None:
    cur = quota_blocked(con)
    cur |= set(blocked)
    cur -= set(cleared)
    db.kv_set(con, QUOTA_KEY, sorted(cur))
    for c in cur:
        con.execute("UPDATE instruments SET priceable=0 WHERE futu_code=?", (c,))
    for c in cleared:
        con.execute("UPDATE instruments SET priceable=1 WHERE futu_code=?", (c,))


# ---------------------------------------------------------------- history
def fetch_daily(codes: Sequence[str], start: date, end: date,
                verbose: bool = False) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Forward-adjusted daily bars. Returns ({code: [bar,...]}, {code: error})."""
    from futu import AuType, KLType, RET_OK

    out: dict[str, list[dict]] = {}
    fail: dict[str, str] = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    with quote_ctx() as ctx:
        for code in codes:
            if not priceable(code):
                fail[code] = f"market {market_of(code)} not licensed on this OpenD"
                continue
            for attempt in range(3):
                ret, data, _ = ctx.request_history_kline(
                    code, start=start.isoformat(), end=end.isoformat(),
                    ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000,
                )
                if ret == RET_OK:
                    rows = [{
                        "code": code, "d": str(r["time_key"])[:10],
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r.get("volume") or 0), "src": "futu:qfq",
                    } for _, r in data.iterrows()]
                    out[code] = _drop_incomplete(code, rows)
                    if verbose:
                        n = len(out[code])
                        last = out[code][-1]["d"] if n else "-"
                        print(f"    {code:<12} n={n:<4} last={last}"
                              f"{' (dropped live bar)' if len(rows) != n else ''}")
                    break
                msg = str(data)
                if "frequency" in msg.lower() or "限频" in msg:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                fail[code] = msg[:200]
                break
            else:
                fail[code] = "rate-limited after 3 attempts"
            time.sleep(0.35)   # OpenD history quota: 60 requests / 30s
    return out, fail


def sync(con, codes: Iterable[str], start: date, end: date,
         verbose: bool = False, retry_blocked: bool = False) -> dict:
    """Fetch and upsert bars, skipping codes already complete for the range.

    The effective end is clamped to the last closed session, so a mid-session run
    never writes a partial bar. Quota failures are recorded rather than raised:
    one unavailable symbol must not abort the daily run.
    """
    codes = list(dict.fromkeys(codes))
    blocked = quota_blocked(con)
    if not retry_blocked:
        codes = [c for c in codes if c not in blocked]

    limits = {m: complete_through(m) for m in set(market_of(c) for c in codes)}
    need: list[str] = []
    for c in codes:
        want_end = min(end.isoformat(), limits.get(market_of(c), end.isoformat()))
        r = db.q1(con, "SELECT MAX(d) mx, MIN(d) mn FROM prices WHERE code=?", (c,))
        if not r or not r["mx"] or r["mx"] < want_end or r["mn"] > start.isoformat():
            need.append(c)
    if not need:
        return {"requested": len(codes), "fetched": 0, "rows": 0, "errors": {},
                "blocked": sorted(blocked), "complete_through": limits}

    bars, fail = fetch_daily(need, start, end, verbose=verbose)
    rows = 0
    for code, bl in bars.items():
        rows += db.upsert_many(con, "prices", bl, ["code", "d"])

    newly_blocked = [c for c, m in fail.items() if _is_quota_error(m)]
    cleared = [c for c in bars if c in blocked]
    if newly_blocked or cleared:
        _record_quota(con, newly_blocked, cleared)
    return {"requested": len(codes), "fetched": len(bars), "rows": rows, "errors": fail,
            "quota_blocked": sorted(quota_blocked(con)), "complete_through": limits}


# ---------------------------------------------------------------- accessors
def bars(con, code: str, start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM prices WHERE code=?"
    args: list = [code]
    if start:
        sql += " AND d>=?"
        args.append(start)
    if end:
        sql += " AND d<=?"
        args.append(end)
    return [dict(r) for r in db.q(con, sql + " ORDER BY d", args)]


def bar_on(con, code: str, d: str) -> dict | None:
    r = db.q1(con, "SELECT * FROM prices WHERE code=? AND d=?", (code, d))
    return dict(r) if r else None


def last_close_on_or_before(con, code: str, d: str) -> tuple[str, float] | None:
    r = db.q1(con, "SELECT d, close FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT 1",
              (code, d))
    return (r["d"], float(r["close"])) if r else None


def sessions(con, code: str, after: str, upto: str) -> list[dict]:
    """Bars strictly after `after` and up to `upto` — the only bars an order
    generated on `after` may be filled on. Prevents same-bar look-ahead."""
    return [dict(r) for r in db.q(
        con, "SELECT * FROM prices WHERE code=? AND d>? AND d<=? ORDER BY d",
        (code, after, upto))]


def trading_days(con, upto: str, n: int, ref: str = "US.SPY") -> list[str]:
    rows = db.q(con, "SELECT d FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (ref, upto, n))
    return [r["d"] for r in reversed(rows)]


def calendar(con, start: str, end: str, ref: str = "US.SPY") -> list[str]:
    return [r["d"] for r in db.q(
        con, "SELECT d FROM prices WHERE code=? AND d>=? AND d<=? ORDER BY d",
        (ref, start, end))]


# ---------------------------------------------------------------- statistics
def realized_vol(con, code: str, upto: str, lookback: int = 60) -> float | None:
    """Annualised close-to-close vol from the last `lookback` sessions."""
    rows = db.q(con, "SELECT close FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, upto, lookback + 1))
    if len(rows) < 20:
        return None
    px = [float(r["close"]) for r in reversed(rows)]
    rets = [px[i] / px[i - 1] - 1 for i in range(1, len(px)) if px[i - 1]]
    if len(rets) < 15:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def horizon_sigma(con, code: str, upto: str, months: int, lookback: int = 60) -> float | None:
    ann = realized_vol(con, code, upto, lookback)
    return None if ann is None else ann * ((months / 12.0) ** 0.5)


def trailing_return(con, code: str, upto: str, sessions_back: int) -> float | None:
    rows = db.q(con, "SELECT close FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, upto, sessions_back + 1))
    if len(rows) < sessions_back // 2 or len(rows) < 2:
        return None
    px = [float(r["close"]) for r in reversed(rows)]
    return px[-1] / px[0] - 1 if px[0] else None


def pct_from_52w_high(con, code: str, upto: str) -> float | None:
    r = db.q1(con, "SELECT MAX(high) h FROM prices WHERE code=? AND d<=? AND d>=date(?,'-365 day')",
              (code, upto, upto))
    lc = last_close_on_or_before(con, code, upto)
    if not r or not r["h"] or not lc:
        return None
    return lc[1] / float(r["h"]) - 1


def return_percentile(con, code: str, upto: str, window: int, lookback_days: int = 252) -> float | None:
    """Where the trailing `window`-session return sits in its own 1-year
    distribution. 0 = worst, 100 = best. Feeds M and C."""
    rows = db.q(con, "SELECT d, close FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, upto, lookback_days + window + 1))
    if len(rows) < window + 30:
        return None
    px = [float(r["close"]) for r in reversed(rows)]
    rolls = [px[i] / px[i - window] - 1 for i in range(window, len(px)) if px[i - window]]
    if len(rolls) < 20:
        return None
    cur = rolls[-1]
    below = sum(1 for x in rolls if x <= cur)
    return 100.0 * below / len(rolls)


def vol_percentile(con, code: str, upto: str, window: int = 20,
                   lookback_days: int = 252) -> float | None:
    """Where the trailing `window`-session realised vol sits in its own 1-year
    distribution. 0 = calmest in a year, 100 = most volatile."""
    rows = db.q(con, "SELECT close FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, upto, lookback_days + window + 2))
    if len(rows) < window + 40:
        return None
    px = [float(r["close"]) for r in reversed(rows)]
    rets = [px[i] / px[i - 1] - 1 for i in range(1, len(px)) if px[i - 1]]
    if len(rets) < window + 30:
        return None
    vols: list[float] = []
    for i in range(window, len(rets) + 1):
        w = rets[i - window:i]
        mu = sum(w) / len(w)
        vols.append((sum((x - mu) ** 2 for x in w) / (len(w) - 1)) ** 0.5)
    cur = vols[-1]
    return 100.0 * sum(1 for v in vols if v <= cur) / len(vols)


def move_z(con, code: str, d: str, lookback: int = 60) -> float | None:
    """|1-day move| normalised by trailing daily vol.

    v0.4 uses this as the *market-observable* surprise proxy for N. v0.3 required
    `|actual - consensus| / 2y forecast-error stdev`, which the corpus almost
    never supplies, so that sub-factor sat at NA and silently re-weighted the
    largest factor in the model. See docs/methodology_v0.4.md §5.2.
    """
    rows = db.q(con, "SELECT close FROM prices WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, d, lookback + 2))
    if len(rows) < 25:
        return None
    px = [float(r["close"]) for r in reversed(rows)]
    rets = [px[i] / px[i - 1] - 1 for i in range(1, len(px)) if px[i - 1]]
    if len(rets) < 20:
        return None
    today = rets[-1]
    hist = rets[:-1]
    mu = sum(hist) / len(hist)
    sd = (sum((r - mu) ** 2 for r in hist) / (len(hist) - 1)) ** 0.5
    return abs(today - mu) / sd if sd else None


# ---------------------------------------------------------------- dating
#: OpenD returns this for every US security whose listing date it does not
#: carry. It is a sentinel, not a date, and writing it into `first_seen_d`
#: would assert that SPY listed in 1970.
_EPOCH_SENTINEL = "1970-01-01"


def listing_dates(codes: Sequence[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Vendor listing dates, with the epoch sentinel dropped rather than stored.

    OpenD carries real listing dates for HK securities and for US common stock,
    and returns `1970-01-01` for US ETFs — which is most of this universe. The
    sentinel is filtered here so callers get "no answer" instead of a date that
    would silently pass every as-of gate.
    """
    from futu import Market, RET_OK, SecurityType

    out: dict[str, str] = {}
    fail: dict[str, str] = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    by_market: dict[str, list[str]] = {}
    for c in codes:
        by_market.setdefault(market_of(c), []).append(c)
    markets = {"US": Market.US, "HK": Market.HK}
    with quote_ctx() as ctx:
        for mkt, lst in by_market.items():
            m = markets.get(mkt)
            if m is None:
                for c in lst:
                    fail[c] = f"market {mkt} not licensed on this OpenD"
                continue
            for sec in (SecurityType.ETF, SecurityType.STOCK):
                for i in range(0, len(lst), 200):
                    ret, data = ctx.get_stock_basicinfo(m, sec, lst[i:i + 200])
                    if ret != RET_OK:
                        continue
                    for _, r in data.iterrows():
                        d = str(r.get("listing_date") or "")[:10]
                        if d and d != _EPOCH_SENTINEL and d[:4].isdigit():
                            out.setdefault(str(r["code"]), d)
                    time.sleep(0.35)
    return out, fail


def earliest_bar(codes: Sequence[str], *, floor: str = "1990-01-01",
                 verbose: bool = False) -> tuple[dict[str, str], dict[str, str]]:
    """The earliest daily bar OpenD will serve, per code.

    This is a *bound*, not an inception date, and the direction of the error is
    what makes it usable. OpenD's US history stops around 2006-08-21 regardless
    of how old the security is, so SPY and GLD both come back on that day. For
    anything younger than the cap the first bar is the real thing — KMLM returns
    2020-12-02, DBMF 2019-05-08, which are their actual launches.

    So the value is never earlier than the true listing date, and an as-of gate
    fed with it can only exclude an instrument that in fact existed; it can never
    admit one that did not. Over-exclusion is the safe error for a replay, and
    the one an undated row makes in the opposite direction.
    """
    from futu import AuType, KLType, RET_OK

    out: dict[str, str] = {}
    fail: dict[str, str] = {}
    today = config.now_hkt().date().isoformat()
    codes = [c for c in dict.fromkeys(codes) if c]
    with quote_ctx() as ctx:
        for code in codes:
            if not priceable(code):
                fail[code] = f"market {market_of(code)} not licensed on this OpenD"
                continue
            for attempt in range(3):
                ret, data, _ = ctx.request_history_kline(
                    code, start=floor, end=today,
                    ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1,
                )
                if ret == RET_OK:
                    if len(data):
                        out[code] = str(data.iloc[0]["time_key"])[:10]
                        if verbose:
                            print(f"    {code:<12} {out[code]}")
                    else:
                        fail[code] = "no bars in range"
                    break
                msg = str(data)
                if "frequency" in msg.lower() or "限频" in msg:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                fail[code] = msg[:200]
                break
            else:
                fail[code] = "rate-limited after 3 attempts"
            time.sleep(0.35)   # OpenD history quota: 60 requests / 30s
    return out, fail
