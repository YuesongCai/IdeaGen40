"""The control group question: beaten SPY, or beaten the shelf?

Every excess number in this repository used to be computed against SPY, and SPY
is not the control for a book of sector, commodity, rates and currency ETFs. The
Deutsche Bank "Seven Sins" paper makes the point with a dividend event study:
against the broad market, dividend payers appear to drift up before the
announcement; against dividend payers, the drift is gone. The drift was the
benchmark. On this repository's own data the gap is the same shape — over the
replay window the shelf equal-weight ran 23.6%/yr at 11.1% vol against SPY's
21.9% at 16.5% — so an arm can clear SPY and still be losing to the universe it
picked from.

Each test below pins a way that comparison could go quietly wrong: an index
built from three names on a thin day, a shelf verdict copied from the SPY
verdict, or a "beats benchmark" flag that stays True when only the wrong
benchmark was beaten.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ideagen import backtest, db, perf


def _db(rows):
    """A temp database carrying nothing but daily closes."""
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    con = db.init(tmp)
    con.executemany("INSERT INTO prices(code,d,close) VALUES(?,?,?)", rows)
    return con


def _series(code, start_day, closes):
    return [(code, f"2026-01-{start_day + i:02d}", c) for i, c in enumerate(closes)]


# ----------------------------------------------------------- the index itself

def test_equal_weight_is_the_average_of_returns_not_of_prices():
    """A price-average index is a market-cap accident; this one is not that.

    Two names, one at $10 and one at $1000, each up 10%: the index must be up
    10%. Averaging prices instead would report the expensive name's move almost
    exclusively, and nothing downstream would show the difference.
    """
    con = _db(_series("A", 5, [10.0, 10.5, 11.0])
              + _series("B", 5, [1000.0, 1050.0, 1100.0]))
    out = backtest.shelf_equal_weight_series(con, "2026-01-01", "2026-01-31",
                                             min_codes=2)
    assert out["closes"][-1] / out["closes"][0] - 1 == pytest.approx(0.10)


def test_a_thin_day_is_dropped_rather_than_published_as_an_index():
    """Three names is not a market. The failure this prevents is silent: one
    sparse day inside a long window moves the whole index and nothing says the
    observation rested on three quotes."""
    rows = (_series("A", 5, [100.0, 101.0, 102.0, 103.0])
            + _series("B", 5, [100.0, 101.0, 102.0, 103.0])
            + _series("C", 5, [100.0, 101.0, 102.0, 103.0])
            + _series("D", 5, [100.0, 101.0, 102.0, 103.0]))
    # E prices only on the middle day, at a wild level.
    rows += [("E", "2026-01-07", 500.0)]
    con = _db(rows)
    out = backtest.shelf_equal_weight_series(con, "2026-01-01", "2026-01-31",
                                             min_codes=5)
    # Only the day carrying five names clears the floor, and one day cannot make
    # an index, so the honest answer is an empty series plus its counts.
    assert out["dates"] == []
    assert out["coverage"]["days_used"] <= 1
    assert out["coverage"]["codes"] == 5


def test_membership_changes_do_not_inject_a_jump():
    """A name that starts being priced mid-window joins the *next* step.

    Chaining a new member's own level into the index instead of its return is
    the classic way a rebalanced index gains free performance the constituents
    never had.
    """
    rows = (_series("A", 5, [100.0] * 6) + _series("B", 5, [100.0] * 6)
            + _series("C", 5, [100.0] * 6) + _series("D", 5, [100.0] * 6)
            + _series("E", 5, [100.0] * 6))
    rows += _series("F", 8, [900.0, 900.0, 900.0])   # joins late, flat, expensive
    con = _db(rows)
    out = backtest.shelf_equal_weight_series(con, "2026-01-01", "2026-01-31",
                                             min_codes=5)
    assert out["closes"][-1] == 100.0                # nothing moved, so neither did the index
    assert out["coverage"]["members_max"] == 6


def test_the_survivorship_caveat_travels_with_the_number():
    """The series is built from today's shelf. That is the first sin, and the
    only defence is that it is stated where the number is read."""
    con = _db(sum((_series(c, 5, [100.0, 101.0, 102.0]) for c in "ABCDE"), []))
    out = backtest.shelf_equal_weight_series(con, "2026-01-01", "2026-01-31",
                                             min_codes=5)
    assert "幸存者偏差" in out["coverage"]["survivorship"]


# ------------------------------------------------------- the two verdicts

def _straight(n, step):
    return [100.0 * (1 + step) ** i for i in range(n)]


def test_an_arm_can_clear_spy_and_still_lose_to_its_own_shelf():
    """The case the whole change exists for.

    The arm compounds faster than the index but slower than the equal-weight
    shelf it picked from. Against SPY it is a winner; against the shelf it is
    not, and before `separability_vs_alt` existed the report had no way to say
    the second half.
    """
    days = [f"2026-01-{d:02d}" for d in range(4, 30)]
    n = len(days)
    rep = perf.compare_arms(
        {"arm": (days, _straight(n, 0.004))},
        bench_dates=days, bench_closes=_straight(n, 0.001),
        benchmark="SPY",
        alt_bench_dates=days, alt_bench_closes=_straight(n, 0.010),
        alt_benchmark="SHELF_EW")
    assert rep["separability"]["beats_benchmark"] is True
    assert rep["separability_vs_alt"]["beats_benchmark"] is False
    assert rep["separability_vs_alt"]["benchmark"] == "SHELF_EW"
    assert "SHELF_EW" in rep["separability_vs_alt"]["note"]


def test_both_relatives_are_reported_and_are_not_the_same_object():
    """Two benchmarks, two betas. If the second one were quietly the first,
    every shelf-relative alpha in the report would be a copy of the SPY one."""
    days = [f"2026-01-{d:02d}" for d in range(4, 30)]
    n = len(days)
    rep = perf.compare_arms(
        {"arm": (days, _straight(n, 0.004))},
        bench_dates=days, bench_closes=_straight(n, 0.001),
        benchmark="SPY",
        alt_bench_dates=days, alt_bench_closes=_straight(n, 0.010),
        alt_benchmark="SHELF_EW")
    arm = rep["arms"]["arm"]
    assert arm["relative"]["benchmark"] == "SPY"
    assert arm["relative_alt"]["benchmark"] == "SHELF_EW"
    assert arm["relative"]["excess_cum"] != arm["relative_alt"]["excess_cum"]
    assert rep["alt_benchmark_performance"]["cum_return"] > \
        rep["benchmark_performance"]["cum_return"]


def test_no_alt_benchmark_leaves_the_report_exactly_as_it_was():
    """Callers that pass one benchmark must not acquire an empty second verdict
    that reads as 'not measured against the shelf' when nobody asked."""
    days = [f"2026-01-{d:02d}" for d in range(4, 30)]
    n = len(days)
    rep = perf.compare_arms(
        {"arm": (days, _straight(n, 0.004))},
        bench_dates=days, bench_closes=_straight(n, 0.001), benchmark="SPY")
    assert "separability_vs_alt" not in rep
    assert "alt_benchmark_performance" not in rep
    assert "relative_alt" not in rep["arms"]["arm"]
