"""Who is already positioned: futures speculators, and members of Congress.

Both feeds here answer a question `gen_carl` asks in its own prompt —「第二步 ·
真实动机：谁在动手，什么东西逼着他动」— and that nothing in the system could
previously answer with a number. They emit `kind="level"` rows rather than dated
events, which is a deliberate call: a COT print and a disclosure window are
*states of the world as of now*, not future triggers. Filed as dated events they
would land in `gen_carl.calendar_block`'s「已排定事件（可作为触发日期）」list with
dates already in the past, which is a trigger no one can act on. As levels they
reach `gen_gap.price_block`, whose whole method is「市场已经相信什么」— and
positioning is the least deniable form of that.

The vendor trap, kept in front rather than in a footnote
-------------------------------------------------------
`commitment-of-traders-report?symbol=ES` answers HTTP 200 with nine well-formed
rows whose newest date is **2024-02-27**. GC, CL and NQ return the same frozen
window. The `from`/`to` form of the same endpoint family returns 301 rows through
2026-09-01. Nothing about the symbol-form response says it is stale — no error,
no flag, correct schema — so positioning read that way would have been reported
as current while describing a market two and a half years gone. `fmp.cot()` only
issues the range form, and this feed re-checks freshness against `as_of` before
emitting anything.

The shape problem with Congress, and what makes it usable
---------------------------------------------------------
Disclosures are single stocks and they are lumpy: of the 200 most recent rows
across both chambers on 2026-09-05, **68 were one member trading one micro-cap**
(TKNO). This book expresses itself in funds and ETFs, so a per-name feed would be
untradeable rows crowding out the calendar, and a naive count would report that
Congress's dominant conviction was a company nobody here can buy.

So the feed aggregates to **sector**, which is the altitude the universe
actually trades, and reports its own concentration alongside the flow. A sector
number built out of one member's one position is a different object from the
same number built out of thirty members, and a reader who cannot tell them apart
will read noise as consensus.

Dates: `disclosureDate`, never `transactionDate`. The STOCK Act allows 45 days
and compliance is uneven — observed lags here run to ten months. Keying on the
transaction would backdate this period's signal into last year and make a replay
of an old week see information that was not public in it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable

from ..feeds import register
from ..sources import fmp

#: Contracts worth carrying, and the macro leg each one speaks for. The universe
#: here is funds and ETFs, so a contract earns its place by being the cleanest
#: read on an exposure the book can actually hold.
COT_CONTRACTS: dict[str, str] = {
    "ES": "标普 500", "NQ": "纳斯达克 100", "RTY": "罗素 2000",
    "GC": "黄金", "SI": "白银", "HG": "铜", "CL": "WTI 原油", "NG": "天然气",
    "ZN": "10 年美债", "ZB": "30 年美债", "DX": "美元指数",
    "6E": "欧元", "6J": "日元",
}

#: How many distinct tickers to resolve to a sector. Each is one `profile` call,
#: so this is the knob that trades a weekly run's latency against how much of the
#: disclosure tail gets classified. The tail is small money by construction —
#: disclosures are reported in bands and the long tail sits in the lowest one.
SECTOR_LOOKUP_CAP = 24

_AMOUNT = re.compile(r"\$?([\d,]+)")


def _amount_midpoint(raw: str) -> float:
    """Disclosures report a band ("$1,001 - $15,000"), never a number.

    The midpoint is an estimate and is labelled as one everywhere it surfaces.
    Using the low end would understate systematically; using the high end would
    overstate; the midpoint is wrong in both directions, which is the only
    honest option when the truth is a range the filer chose not to narrow.
    """
    nums = [float(m.replace(",", "")) for m in _AMOUNT.findall(raw or "")]
    if not nums:
        return 0.0
    return sum(nums) / len(nums) if len(nums) > 1 else nums[0]


def _is_buy(t: str) -> bool | None:
    low = (t or "").lower()
    if "purchase" in low or low.startswith("buy"):
        return True
    if "sale" in low or low.startswith("sell"):
        return False
    return None                      # exchanges, gifts: real, but not a direction


@register("fmp_cot", "calendar", label="期货持仓（CFTC COT · 投机净头寸）",
          expect_rows=6, params={"contracts": list(COT_CONTRACTS),
                                 "max_stale_days": 21})
def fmp_cot(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Latest speculative positioning per contract, as a percentile-style level.

    `currentLongMarketSituation` is the vendor's net-long share; a reading near
    90 (gold sat at 88.95 on 2026-09-01) says the speculative side is close to
    fully committed, which is a statement about who is *left* to buy rather than
    about value. That is exactly the input `gen_gap` needs to answer「这个主题里
    还有哪一部分没有被价格表达」.

    Staleness is checked rather than assumed — see the module note. A COT print
    older than `max_stale_days` raises instead of yielding, because the failure
    this feed exists to avoid is a confident number from the wrong year.
    """
    want = {str(c).upper() for c in params.get("contracts") or COT_CONTRACTS}
    stale_after = int(params.get("max_stale_days", 21))

    start = (as_of - timedelta(days=45)).isoformat()
    rows = fmp.cot(start, as_of.isoformat())
    if not rows:
        raise RuntimeError(f"COT range form returned nothing for {start}..{as_of}")

    newest: dict[str, dict[str, Any]] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if sym not in want:
            continue
        d = str(r.get("date") or "")[:10]
        if not d:
            continue
        if sym not in newest or d > str(newest[sym].get("date") or "")[:10]:
            newest[sym] = r

    if not newest:
        raise RuntimeError(f"COT returned {len(rows)} rows, none for the "
                           f"{len(want)} contracts this book trades")

    freshest = max(str(r["date"])[:10] for r in newest.values())
    lag = (as_of - date.fromisoformat(freshest)).days
    if lag > stale_after:
        # The symbol form of this endpoint is frozen at 2024-02-27 and says so
        # nowhere. If the range form ever develops the same fault, this is where
        # it stops — loudly, rather than as an authoritative number from 2024.
        raise RuntimeError(
            f"COT 最新一期是 {freshest}，距 {as_of} 已 {lag} 天（上限 {stale_after}）"
            f"——按停摆处理，不当作当前持仓")

    for sym, r in sorted(newest.items()):
        cur = r.get("currentLongMarketSituation")
        prev = r.get("previousLongMarketSituation")
        try:
            cur_f = float(cur)
        except (TypeError, ValueError):
            continue
        d = str(r.get("date"))[:10]
        drift = ("" if prev in (None, "") else
                 f"，前值 {prev}")
        yield {
            "event_id": f"cot:{sym}:{d}",
            "date": d,
            "label": f"{COT_CONTRACTS.get(sym, sym)} 投机净多占比"
                     f"（{r.get('marketSentiment') or '—'}{drift}）",
            "kind": "level",
            "actual": round(cur_f, 2),
            "unit": "%",
            "source": f"FMP COT {sym}（{d} 当期）",
            "net_position": r.get("netPostion"),
            "change_in_net": r.get("changeInNetPosition"),
            "reversal_trend": r.get("reversalTrend"),
        }


@register("fmp_congress_flow", "calendar",
          label="国会披露交易（按板块汇总）",
          expect_rows=3, params={"window_days": 45,
                                 "sector_lookup_cap": SECTOR_LOOKUP_CAP})
def fmp_congress_flow(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Congressional disclosures, netted to sector, with concentration reported.

    Emits one row per sector plus one summary row. The summary is not decoration:
    it carries the member count and the share of the flow sitting in a single
    name, which is what tells a reader whether "Congress bought technology" means
    thirty people or one. On 2026-09-05 the raw feed's single most common ticker
    accounted for 34% of all rows across both chambers.

    Amounts are band midpoints — every disclosure reports a range, never a
    figure — and every label here says so, because a dollar number that looks
    precise will be quoted as though it were.
    """
    window = int(params.get("window_days", 45))
    cap = int(params.get("sector_lookup_cap", SECTOR_LOOKUP_CAP))
    cutoff = (as_of - timedelta(days=window)).isoformat()

    raw: list[dict[str, Any]] = []
    for chamber in ("senate", "house"):
        for r in fmp.congress_trades(chamber):
            r = dict(r)
            r["_chamber"] = chamber
            raw.append(r)

    # Disclosure date, not transaction date — see the module docstring.
    recent = [r for r in raw
              if cutoff <= str(r.get("disclosureDate") or "")[:10] <= as_of.isoformat()]
    if not recent:
        raise RuntimeError(f"两院共 {len(raw)} 条披露，{cutoff} 之后一条都没有"
                           f"——按端点停摆处理")

    by_sym: dict[str, float] = {}
    rows_by_sym: dict[str, int] = {}
    for r in recent:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        rows_by_sym[sym] = rows_by_sym.get(sym, 0) + 1
        side = _is_buy(str(r.get("type") or ""))
        if side is None:
            continue
        amt = _amount_midpoint(str(r.get("amount") or ""))
        by_sym[sym] = by_sym.get(sym, 0.0) + (amt if side else -amt)

    # Resolve only the names carrying the most money. The tail is bounded by the
    # lowest disclosure band by construction, so what it can move is bounded too.
    ranked = sorted(by_sym, key=lambda s: -abs(by_sym[s]))[:cap]
    by_sector: dict[str, float] = {}
    members_by_sector: dict[str, set[str]] = {}
    sector_of: dict[str, str] = {}
    for sym in ranked:
        try:
            sector = (fmp.profile(sym) or {}).get("sector") or "未分类"
        except Exception:  # noqa: BLE001 — one unresolvable ticker must not lose the rest
            sector = "未分类"
        sector_of[sym] = sector
        by_sector[sector] = by_sector.get(sector, 0.0) + by_sym[sym]
    for r in recent:
        sym = str(r.get("symbol") or "").strip().upper()
        s = sector_of.get(sym)
        if s:
            members_by_sector.setdefault(s, set()).add(str(r.get("office") or "?"))

    top_sym = max(rows_by_sym, key=lambda s: rows_by_sym[s]) if rows_by_sym else ""
    conc = (rows_by_sym.get(top_sym, 0) / len(recent)) if recent else 0.0

    yield {
        "event_id": f"congress:summary:{as_of.isoformat()}",
        "date": as_of.isoformat(),
        "label": (f"国会披露交易 {len(recent)} 笔／近 {window} 天，"
                  f"最集中的一只 {top_sym} 占 {conc:.0%}"
                  f"（占比高 = 少数人的个股，不是国会共识）"),
        "kind": "level",
        "actual": len(recent),
        "unit": "笔",
        "source": "FMP senate-latest + house-latest（按披露日）",
            # Real, but peripheral beside VIX or the curve. `gen_gap` and
            # `gen_carl` rank levels by this so an aggregate built from a
            # handful of members cannot push the market-wide gauges out of
            # the prompt's line budget.
            "priority": 3,
        "top_symbol": top_sym,
        "top_symbol_share": round(conc, 4),
    }

    for sector, net in sorted(by_sector.items(), key=lambda kv: -abs(kv[1])):
        if not sector or abs(net) < 1_000:
            continue
        n_members = len(members_by_sector.get(sector, ()))
        yield {
            "event_id": f"congress:{sector}:{as_of.isoformat()}",
            "date": as_of.isoformat(),
            "label": (f"国会净{'买入' if net > 0 else '卖出'} {sector}"
                      f"（{n_members} 位议员，金额取披露区间中点，非精确值）"),
            "kind": "level",
            "actual": round(net / 1000.0, 1),
            "unit": "千美元(估)",
            "source": f"FMP 国会披露 · 近 {window} 天",
            # Real, but peripheral beside VIX or the curve. `gen_gap` and
            # `gen_carl` rank levels by this so an aggregate built from a
            # handful of members cannot push the market-wide gauges out of
            # the prompt's line budget.
            "priority": 3,
            "members": n_members,
        }
