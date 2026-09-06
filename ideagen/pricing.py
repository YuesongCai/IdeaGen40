"""The price view stage A reads — one recipe, shared by the live run and the replay.

`priced_in` (P 已定价) is the one place a price series feeds a *scoring*
decision rather than a mark. Until 2026-09-06 the recipe lived inside
`backtest._prices`, and only the backtest called it: `cli weekly`,
`clauderun` and `poc_workflow` all entered `orchestrator.weekly` without a
`prices` argument, the orchestrator turned None into `{}`, and every live
period scored P = 50 for every theme — a factor carrying 0.20 of the weight
that had never once been computed on a live run (Jon 2026-09-06 §4:「P 如果
计算完成的话倒是没问题，但是确认要计算完成」). Moving the recipe here is what
lets the orchestrator build the same view the backtest builds, so a replay of
a live period sees the same numbers the live run saw.

Two views are produced per code and neither is decoration:

  * `priced_in` — where the trailing 21-session return sits in the
    instrument's own one-year distribution. This is P. A code that has bars
    but too few of them for the percentile is recorded with `priced_in=50`
    and `priced_in_source="neutral_default"`; the 50 is a fill, not a reading,
    and every consumer must carry that distinction rather than the number.
  * `ret_21s` — the raw one-month move, for the cross-sectional momentum arm.
    Over 105 weekly periods the percentile picks the calmest names at the top
    of their own range and loses; the raw return picks the strongest and wins.
    Carrying only one of them is what let a "momentum" arm be written against
    the wrong one.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .sources import futu_px

#: The method name recorded on every measured P. A consumer checks for this
#: string, not for "the value is not 50" — a measured 50 is a legitimate reading.
PRICED_IN_METHOD = "return_percentile_21s"
NEUTRAL_DEFAULT = "neutral_default"
NEUTRAL_VALUE = 50.0


def price_view(con, as_of: date, codes: Iterable[str],
               clamp: dict[str, str]) -> dict[str, Any]:
    """Per-code price view as of the clamp, plus the statistics stage A reads.

    Every query underneath is bounded by the clamped date, so `priced_in` cannot
    be computed from a bar the decision had not seen. A code with no bar on or
    before the clamp is absent from the result entirely — "no series" and "a
    series too short to rank" are different facts, and the summary in
    `build_prices` counts them separately.
    """
    out: dict[str, Any] = {}
    for code in sorted({c for c in codes if c}):
        upto = clamp.get(futu_px.market_of(code), as_of.isoformat())
        last = futu_px.last_close_on_or_before(con, code, upto)
        if not last:
            continue
        d, close = last
        pd = futu_px.return_percentile_detail(con, code, upto, window=21)
        out[code] = {
            "d": d, "close": close, "clamped_to": upto,
            "priced_in": pd["value"] if pd["ok"] else NEUTRAL_VALUE,
            "ret_21s": futu_px.trailing_return(con, code, upto, 21),
            "priced_in_source": PRICED_IN_METHOD if pd["ok"] else NEUTRAL_DEFAULT,
            # The facts that make the percentile checkable by hand: how many
            # rolling returns it ranks against, which bar it stopped at, and —
            # when it could not be computed — why not. `last_d` is the newest
            # bar actually read; it equals `d` but is kept under its own name
            # because it is P's cut-off, not the mark's.
            "priced_in_n": pd["n_samples"],
            "priced_in_last_d": pd["last_d"],
            "priced_in_reason": pd["reason"],
            "vol_pctl": futu_px.vol_percentile(con, code, upto),
            "sigma_1m": futu_px.horizon_sigma(con, code, upto, months=1),
        }
    return out


def summarize(prices: dict[str, Any], requested: Iterable[str] | None = None
              ) -> dict[str, Any]:
    """Counts a journal can print: how many codes were measured, filled, missing.

    `requested` lets the summary say how many codes had no bars at all, which
    `prices` alone cannot — they were never written into it.
    """
    measured = sorted(c for c, v in prices.items()
                      if v.get("priced_in_source") == PRICED_IN_METHOD)
    defaulted = sorted(c for c, v in prices.items()
                       if v.get("priced_in_source") != PRICED_IN_METHOD)
    req = sorted({c for c in (requested or ()) if c})
    missing = sorted(set(req) - set(prices)) if req else []
    return {
        "codes": len(prices), "requested": len(req) if req else len(prices),
        "measured": len(measured), "defaulted": len(defaulted),
        "missing": len(missing),
        "defaulted_codes": defaulted, "missing_codes": missing,
        "clamped_to": sorted({str(v.get("clamped_to")) for v in prices.values()
                              if v.get("clamped_to")}),
        "last_d": max((str(v.get("d")) for v in prices.values()
                       if v.get("d")), default=None),
    }


def build_prices(con, as_of: date, codes: Iterable[str] | None = None
                 ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The view a live weekly run should score P from, and a summary of it.

    Codes default to every indicator of every theme registered as of `as_of`
    (primary indicator plus the related codes), because those are the only
    series stage A reads. The clamp is the same one the backtest applies, so a
    live run and a replay of it agree on which bar was the newest allowed.
    """
    from . import backtest, lexicon
    codes = list(codes) if codes is not None else lexicon.all_indicators(as_of)
    clamp = backtest.clamp_dates(as_of)
    prices = price_view(con, as_of, codes, clamp)
    summary = summarize(prices, codes)
    summary["clamp"] = clamp
    return prices, summary
