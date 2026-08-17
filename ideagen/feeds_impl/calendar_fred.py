"""Macro calendar and reference levels from public, key-free sources.

Wisburg supplies research text and nothing else — none of its eleven tools returns
a schedule, a consensus figure or an actual — so the calendar has to be its own
feed. These endpoints need no API key, which is why they were chosen: a feed whose
credentials can lapse is a feed that will one day silently return nothing.

Levels rather than dated events are emitted as `kind="level"` rows so a threshold
watchpoint (a spread approaching 200bp, say) has something to compare against.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date, timedelta
from typing import Any, Iterable

from ..feeds import register

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"

#: Series worth carrying, each tied to a decision the system actually makes.
SERIES = {
    "BAMLH0A0HYM2": ("高收益债 OAS", "bp", 100.0),
    "CPIAUCNS":     ("CPI 未季调", "index", 1.0),
    "DTWEXBGS":     ("美元广义指数", "index", 1.0),
    "DGS10":        ("10 年美债", "pct", 1.0),
    "DGS30":        ("30 年美债", "pct", 1.0),
}


#: Public data endpoints reject a bare urllib request often enough that the
#: header is not optional. A missing User-Agent shows up as a dropped connection,
#: which is indistinguishable from an outage unless you have already ruled it out.
UA = {"User-Agent": "IdeaGen40/0.5 (macro research)", "Accept": "*/*"}


def _get(url: str, *, timeout: int = 20, tries: int = 2) -> str:
    """Fetch a URL, raising on final failure.

    Raising rather than returning empty is the point. A feed that swallows a
    transport error reports zero rows and `ok=True`, so "the endpoint is down"
    and "there is nothing scheduled this period" become the same observation —
    and the run proceeds as though it had looked. `feeds.fetch()` already turns a
    raised error into `ok=False` with the reason attached, which is the behaviour
    the layering exists to produce.
    """
    last: Exception | None = None
    for _ in range(max(1, tries)):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return fh.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 — retried, then re-raised below
            last = e
    raise RuntimeError(f"{type(last).__name__}: {last}") from last


def _fred(sid: str, timeout: int = 20) -> tuple[str, float] | None:
    """Latest non-empty observation, or None if this one series is unavailable."""
    try:
        text = _get(FRED.format(sid=sid), timeout=timeout)
    except Exception:  # noqa: BLE001 — one dead series must not lose the other four
        return None
    rows = list(csv.reader(io.StringIO(text)))
    for r in reversed(rows[1:]):
        if len(r) >= 2 and r[1] not in (".", "", None):
            try:
                return r[0], float(r[1])
            except ValueError:
                continue
    return None


@register("fred_levels", "calendar", label="FRED 参考水平（利差 · 曲线 · 美元）",
          expect_rows=len(SERIES), params={"series": list(SERIES)})
def fred_levels(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Latest level per series, as threshold-comparable calendar rows.

    Degrades per series rather than per feed: four of five spreads is worth more
    than none. The shortfall is not hidden — `expect_rows` above is the full series
    count, so a partial fetch comes back as a named problem on the feed result.
    """
    wanted = list(params.get("series", list(SERIES)))
    dead: list[str] = []
    for sid in wanted:
        label, unit, scale = SERIES.get(sid, (sid, "raw", 1.0))
        got = _fred(sid)
        if not got:
            dead.append(sid)
            continue
        obs_d, val = got
        yield {
            "event_id": f"fred:{sid}:{obs_d}",
            "date": obs_d,
            "label": label,
            "kind": "level",
            "actual": round(val * scale, 4),
            "unit": unit,
            "source": f"FRED {sid}",
        }
    if dead and len(dead) == len(wanted):
        # Every series failed. That is not degradation, it is the feed being down,
        # and it must not look like a period with no macro levels in it.
        raise RuntimeError(f"all {len(dead)} FRED series unavailable: "
                           f"{', '.join(dead)}")


@register("treasury_auctions", "calendar", label="美债拍卖日程",
          expect_rows=1, params={"lookahead_days": 21})
def treasury_auctions(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Upcoming auctions from Treasury's public announcement API.

    Auctions are in here because a deteriorating auction is a concrete, dated
    trigger — the kind a watchpoint can be written against before the fact.

    `expect_rows=1`: bills settle weekly, so any three-week window that comes back
    empty means the endpoint answered but told us nothing useful, which is worth
    seeing rather than reading as a quiet calendar.
    """
    import json as _j
    n = int(params.get("lookahead_days", 21))
    end = as_of + timedelta(days=n)
    url = ("https://www.treasurydirect.gov/TA_WS/securities/announced"
           f"?format=json&pagesize=250&startDate={as_of.isoformat()}")
    data = _j.loads(_get(url))           # raises on outage; fetch() records it
    for s in (data or []):
        d = (s.get("auctionDate") or "")[:10]
        if not d or not (as_of.isoformat() <= d <= end.isoformat()):
            continue
        yield {
            "event_id": f"auction:{s.get('cusip') or d}:{s.get('securityTerm','')}",
            "date": d,
            "label": f"{s.get('securityTerm','')} {s.get('securityType','')} 拍卖".strip(),
            "kind": "auction",
            "expectation": "拍卖需求正常",
            "source": "TreasuryDirect",
        }
