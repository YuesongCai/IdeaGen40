"""The macro calendar the founding spec asked for, and the implied-vol layer.

`prompts/founding_principles.md` names this system's inputs as「wisburg 日报
（周一–周三前三天）+ 宏观日历」. Only the first half was ever built. What stood
in for the second was `calendar_fred.py`: five FRED levels and a Treasury
auction schedule. Useful, but neither is a calendar of macro releases — as of
2026-09-05 the production `events` table held 51 auctions, 13 levels, and zero
rows of kind `macro_release`, `policy` or `earnings`. No CPI, no PCE, no
payrolls, no FOMC.

That gap had a specific downstream cost. `gen_carl` asks the model for「一个带
日期或带水平的触发条件」and explicitly rejects「如果情绪转弱」because nobody can
adjudicate it a month later. The only dated triggers it could offer were bill
auctions. And the one `expectation` in the whole table was the constant string
「拍卖需求正常」, hard-coded in `calendar_fred`, repeated across all 51 auction
rows — so `watchpoints`, whose entire premise is a *deviation* from an
expectation, has never had a real expectation to deviate from.

Three feeds live here.

`fmp_macro_releases` is the missing half: scheduled US releases with the
consensus attached. Consensus coverage was measured rather than assumed, and
the obvious hypothesis was wrong. It does **not** improve as a release nears —
over a 60-day window, estimate coverage ran 52% / 16% / 31% / 30% across the
0-7, 8-14, 15-30 and 31-60 day buckets, which is noise, not a decay curve. The
real determinant is *which indicator*: over five months of US High+Medium
events, fourteen series carried an estimate every single time (CPI, Core CPI
MoM and YoY, Inflation Rate MoM and YoY, Non Farm Payrolls, Unemployment Rate,
Average Hourly Earnings MoM and YoY, Participation Rate, PPI MoM, Continuing
Jobless Claims, Jobless Claims 4-Week Average, CPI s.a), and the seven that
never did were Fed governors' speeches, CFTC positioning prints and the MBA
mortgage rate — none of which has a consensus to publish. The releases most
worth writing a trigger against are exactly the ones whose expectation is
reliable, which is a better position than the average would suggest.

`fmp_vol_surface` is the implied-volatility layer, and it is index-level
because it has to be: FMP serves **no options data on this key** — five
distinct option and IV paths all 404. That constraint happens to point the
right way. This book holds funds and ETFs against month-horizon macro theses,
so what matters is whether the market is paying up for protection across the
term structure and across asset classes, not one name's smile. Eleven
exchange-published indices cover equity term structure (9-day through 6-month),
vol-of-vol, Nasdaq, Russell, crude, gold, and — through ^MOVE and ^VXTLT —
rates, which is the asset class this mandate trades most.

`fmp_curve` carries the tenors FRED does not, plus the spreads. It deliberately
does *not* re-emit 10y and 30y: `calendar_fred` already publishes those two, and
the same number arriving twice under two labels is how a model comes to believe
two independent sources agree.

All three emit into the existing `calendar` kind. Nothing downstream changes
shape — `gen_gap.price_block` already reads every `kind="level"` row as a price
anchor, and `gen_carl.calendar_block` already splits dated events from levels.
The feeds are the whole change.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any, Iterable

from ..feeds import register
from ..sources import fmp

#: Releases whose name marks them as policy rather than data. Kept as a tuple of
#: substrings rather than exact names because the vendor renames around meetings
#: ("Fed Interest Rate Decision" / "FOMC Economic Projections" / "Fed Press
#: Conference" are three rows for one afternoon), and a policy row mis-filed as
#: a data release still reaches the model — it just loses the distinction a
#: reader needs between "a number prints" and "a decision is taken".
POLICY_MARKERS = ("fed interest rate decision", "fomc", "fed press conference",
                  "interest rate projection", "fed chair", "fed monetary policy")

#: The volatility complex, in the order a reader would want it: equity term
#: structure first, then the cross-asset gauges. ^SKEW and ^VXEEM answer empty
#: on this key and are left out rather than carried as permanent blanks.
VOL_INDICES: dict[str, tuple[str, str]] = {
    "^VIX9D":  ("VIX 9 天", "9 天期标普隐含波动率"),
    "^VIX":    ("VIX", "30 天期标普隐含波动率"),
    "^VIX3M":  ("VIX 3 月", "3 月期标普隐含波动率"),
    "^VIX6M":  ("VIX 6 月", "6 月期标普隐含波动率"),
    "^VVIX":   ("VVIX", "VIX 自身的隐含波动率"),
    "^VXN":    ("VXN", "纳斯达克 100 隐含波动率"),
    "^RVX":    ("RVX", "罗素 2000 隐含波动率"),
    "^MOVE":   ("MOVE", "美债隐含波动率"),
    "^VXTLT":  ("VXTLT", "20 年期美债 ETF 隐含波动率"),
    "^OVX":    ("OVX", "原油隐含波动率"),
    "^GVZ":    ("GVZ", "黄金隐含波动率"),
}

#: Tenors FRED's DGS10/DGS30 do not already carry. The overlap is excluded on
#: purpose — see the module docstring.
CURVE_TENORS = {"month3": "3 个月", "year2": "2 年", "year5": "5 年",
                "year20": "20 年"}


def _is_policy(event: str) -> bool:
    low = (event or "").lower()
    return any(m in low for m in POLICY_MARKERS)


def _event_id(country: Any, d: str, clock: str, event: str) -> str:
    """A key that stays unique after being cut to fit MySQL's VARCHAR(128).

    Truncation alone is not safe: "Core Inflation Rate MoM" and "Core Inflation
    Rate YoY" survive a prefix cut as the same string, and the row that arrives
    second would replace the first rather than sit beside it. So the readable
    part is trimmed and a digest of the *full* identity is appended, which keeps
    the id greppable while making a collision require an actual hash collision.
    """
    full = f"fmp:{country}:{d}:{clock}:{event}"
    if len(full) <= 128:
        return full
    tag = hashlib.sha1(full.encode("utf-8")).hexdigest()[:10]
    return f"{full[:117]}:{tag}"


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


@register("fmp_macro_releases", "calendar",
          label="宏观发布日程（含一致预期）",
          expect_rows=5,
          params={"lookahead_days": 21, "lookback_days": 7,
                  "countries": ["US"], "impacts": ["High"]})
def fmp_macro_releases(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Scheduled releases either side of `as_of`, with consensus where published.

    A backward window as well as a forward one, because the two answer different
    questions and only one of them is a calendar. Ahead of `as_of` a row is a
    trigger a thesis can be written against. Behind it the same row carries an
    `actual`, which is what lets last period's watchpoint be settled instead of
    quietly expiring — the reason `watchpoints` has stayed empty is partly that
    nothing has ever delivered it an outcome to compare against.

    `expect_rows=5` rather than the ~17 a typical three-week US High window
    holds. The floor is there to catch a dead endpoint, and setting it near the
    expected count would fail the feed on a genuinely quiet fortnight — a false
    alarm that trains the reader to ignore the real one.
    """
    ahead = int(params.get("lookahead_days", 21))
    back = int(params.get("lookback_days", 7))
    countries = {str(c).upper() for c in params.get("countries") or ["US"]}
    impacts = {str(i).lower() for i in params.get("impacts") or ["High"]}

    start = (as_of - timedelta(days=back)).isoformat()
    end = (as_of + timedelta(days=ahead)).isoformat()
    rows = fmp.economic_calendar(start, end)   # raises on outage; fetch() records it

    for r in rows:
        if countries and str(r.get("country") or "").upper() not in countries:
            continue
        if impacts and str(r.get("impact") or "").lower() not in impacts:
            continue
        stamp = str(r.get("date") or "")
        d, _, clock = stamp.partition(" ")
        if not d:
            continue
        event = str(r.get("event") or "").strip()
        if not event:
            continue
        est, prev, act = _num(r.get("estimate")), _num(r.get("previous")), _num(r.get("actual"))
        yield {
            # The vendor gives no id, and (date, event) is not unique on its own
            # — "Inflation Rate MoM" and "Inflation Rate YoY" share a timestamp
            # and differ only in the name, while two countries can share both.
            # Capped at 128 because that is `events.event_id` on MySQL: a longer
            # key is silently truncated there and two long release names sharing
            # a prefix would then collide into one row on the cloud instance and
            # not on this laptop.
            "event_id": _event_id(r.get("country"), d, clock[:5], event),
            "date": d,
            "label": event,
            "kind": "policy" if _is_policy(event) else "macro_release",
            # `expectation` is the field `watchpoints` was designed around and
            # has never received. Absent consensus stays absent: a `previous`
            # promoted into an expectation would be an assertion the vendor
            # never made, and a watchpoint would then fire on the difference
            # between this month and last rather than on a surprise.
            "expectation": None if est is None else f"{est}{r.get('unit') or ''}",
            "actual": act,
            "unit": r.get("unit") or None,
            "source": f"FMP economic-calendar ({r.get('country')})",
            "impact": r.get("impact"),
            "previous": prev,
            "time_utc": clock[:5] or None,
        }


@register("fmp_vol_surface", "calendar",
          label="隐含波动率（VIX 期限结构 · 跨资产）",
          expect_rows=8, params={"symbols": list(VOL_INDICES)})
def fmp_vol_surface(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Every published vol index, plus the two ratios worth naming.

    The ratios are emitted as their own rows rather than left for the model to
    compute, because a term-structure inversion is the single most legible
    statement this feed can make and a model asked to divide two numbers in a
    prompt will sometimes decline to. VIX/VIX3M above 1 means the market is
    paying more for one month of protection than for three — the shape that
    accompanies a sold-off tape rather than a calm one.

    Emitted as `kind="level"` so `gen_gap` picks them up as price anchors with
    no change to that arm. That is the correct home for them: gen_gap's method
    is "what does the price already say the market believes", and implied vol is
    the most direct available answer.
    """
    syms = list(params.get("symbols") or VOL_INDICES)
    got = fmp.quotes(syms)
    if not got:
        # Distinguish "no vol today" (impossible) from "the endpoint answered
        # nothing" (routine). Without this the run records a period in which the
        # market had no implied volatility.
        raise RuntimeError(f"volatility complex returned nothing for {len(syms)} symbols")

    px: dict[str, float] = {}
    for s in syms:
        q = got.get(s) or {}
        v = _num(q.get("price"))
        if v is None:
            continue
        px[s] = v
        label, gloss = VOL_INDICES.get(s, (s, ""))
        yield {
            "event_id": f"fmpvol:{s}:{as_of.isoformat()}",
            "date": as_of.isoformat(),
            "label": f"{label}（{gloss}）" if gloss else label,
            "kind": "level",
            "actual": round(v, 4),
            "unit": "",
            "source": f"FMP quote {s}",
            "change_pct": _num(q.get("changePercentage")),
        }

    for name, num, den, gloss in (
            ("VIX 期限结构 1M/3M", "^VIX", "^VIX3M",
             "大于 1 = 近月保护比远月贵，通常是已经在跌的形态"),
            ("VIX 期限结构 9D/1M", "^VIX9D", "^VIX",
             "大于 1 = 市场在为这一两周的某件具体事定价")):
        a, b = px.get(num), px.get(den)
        if a is None or not b:
            continue
        yield {
            "event_id": f"fmpvol:ratio:{num}/{den}:{as_of.isoformat()}",
            "date": as_of.isoformat(),
            "label": f"{name}（{gloss}）",
            "kind": "level",
            "actual": round(a / b, 4),
            "unit": "x",
            "source": "FMP quote (计算值)",
        }


@register("fmp_curve", "calendar", label="美债收益率曲线与利差",
          expect_rows=4, params={"lookback_days": 10})
def fmp_curve(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """The published curve, minus what FRED already says, plus the spreads.

    One call returns twelve tenors for every day in the window; `calendar_fred`
    spends two round trips on two of them. Rather than replace a working feed
    mid-stream, this one yields the tenors FRED omits and the two spreads that
    are read as regime statements rather than as levels.
    """
    back = int(params.get("lookback_days", 10))
    rows = fmp.treasury_curve((as_of - timedelta(days=back)).isoformat(),
                              as_of.isoformat())
    dated = sorted((r for r in rows if r.get("date")), key=lambda r: str(r["date"]))
    if not dated:
        raise RuntimeError(f"treasury-rates returned no dated row in the last {back} days")
    latest = dated[-1]
    d = str(latest["date"])[:10]

    for field, zh in CURVE_TENORS.items():
        v = _num(latest.get(field))
        if v is None:
            continue
        yield {"event_id": f"fmpcurve:{field}:{d}", "date": d,
               "label": f"美债 {zh}", "kind": "level", "actual": round(v, 4),
               "unit": "pct", "source": "FMP treasury-rates"}

    for name, long_f, short_f, gloss in (
            ("2s10s", "year10", "year2", "负值 = 曲线倒挂"),
            ("3m10y", "year10", "month3", "美联储最常引用的那条衰退指标")):
        lo, sh = _num(latest.get(long_f)), _num(latest.get(short_f))
        if lo is None or sh is None:
            continue
        yield {"event_id": f"fmpcurve:{name}:{d}", "date": d,
               "label": f"美债利差 {name}（{gloss}）", "kind": "level",
               "actual": round((lo - sh) * 100, 1), "unit": "bp",
               "source": "FMP treasury-rates (计算值)"}
