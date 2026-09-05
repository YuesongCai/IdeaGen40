"""Is the score ranking skill, or ranking risk?

`instrument_vol_gradient` puts two ladders side by side and lets the reader
conclude. That was enough to raise the suspicion and not enough to settle it —
this repository has already contradicted itself once about whether the
expectation score ranks skill or volatility. The partial rank correlation
settles it in one number per period, so it can be tracked forward instead of
re-argued.

On the live data: raw +0.231, partial **+0.005** with t=0.04, and the score's
correlation with pre-entry volatility is 0.53–0.81 in every single period. The
tests below pin the two ways this number could lie — reporting skill where there
is only risk, and reporting zero where the control simply had no variation.
"""

from __future__ import annotations

import random

from ideagen import backtest


def _rows(period, n, skill, seed=7):
    """Instruments whose return is driven by volatility, plus `skill` times a
    component the score knows about and volatility does not."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        vol = 5.0 + i * 0.5                       # the control
        edge = rnd.gauss(0, 1)                    # what a real score would know
        # A score identical to the control has no partial to compute — the
        # denominator is zero and the function says so. A real score is a noisy
        # proxy for risk, which is the case worth testing.
        score = vol + rnd.gauss(0, 1.5) + skill * edge * 3.0
        ret = vol * 0.1 + skill * edge * 2.0 + rnd.gauss(0, 0.05)
        out.append((period, score, vol, ret))
    return out


def test_a_pure_risk_sorter_survives_the_raw_correlation_and_not_the_partial():
    """The finding this check exists for. Score and return both track
    volatility; the raw correlation is large and means nothing."""
    out = backtest.partial_rank_correlation(_rows("p1", 60, skill=0.0))
    assert out["mean_rho_raw"] > 0.9
    assert abs(out["mean_rho_partial"]) < 0.2
    assert out["shrinkage"] > 0.7


def test_real_information_survives_the_control():
    """The check has to be able to pass, or it is not a check — it is a way of
    dismissing every score that reaches it."""
    out = backtest.partial_rank_correlation(_rows("p1", 60, skill=1.0))
    assert out["mean_rho_partial"] > 0.5


def test_a_control_with_no_variation_gives_no_partial_rather_than_zero():
    """Zero reads as 'measured, and nothing survived'. Undefined must not be
    able to enter the report wearing that meaning."""
    rows = [("p1", i, 1.0, i * 0.1) for i in range(30)]     # control constant
    out = backtest.partial_rank_correlation(rows)
    assert out["per_period"][0].get("rho_partial") is None
    assert out["mean_rho_partial"] is None


def test_periods_are_never_pooled():
    """Two periods, each internally uninformative, the second one simply higher
    on every axis. Pooled, that is a strong correlation and it is the calendar."""
    rnd = random.Random(3)
    rows = ([("p1", i + rnd.gauss(0, 3), 5.0 + i,
              1.0 + (30 - i) * 0.01 + rnd.gauss(0, 0.05)) for i in range(30)]
            + [("p2", 100 + i + rnd.gauss(0, 3), 50.0 + i,
                9.0 + (30 - i) * 0.01 + rnd.gauss(0, 0.05)) for i in range(30)])
    out = backtest.partial_rank_correlation(rows)
    assert out["periods_scored"] == 2
    assert out["mean_rho_raw"] < 0          # each period is inverted on its own


def test_a_thin_period_is_skipped_and_says_why():
    out = backtest.partial_rank_correlation(_rows("p1", 8, skill=1.0))
    assert out["periods_scored"] == 0
    assert "不足" in out["per_period"][0]["skipped"]


# ------------------------------------------------------- the interval

def test_the_bootstrap_resamples_periods_not_observations():
    """Six periods is six draws however many names each holds. If the interval
    were built from the rows it would shrink with the pool size — which grows
    when the shelf grows — and a wider shelf would start looking like more
    evidence about the same six months."""
    out = backtest.period_bootstrap_ci([0.1, -0.2, 0.3, 0.0, 0.15, -0.05])
    assert out["n_periods"] == 6
    assert out["ci"][0] < out["mean"] < out["ci"][1]
    assert out["covers_zero"] is True


def test_a_consistent_effect_gets_an_interval_that_clears_zero():
    """The check has to be able to say yes, or it only ever says no."""
    out = backtest.period_bootstrap_ci([0.31, 0.28, 0.35, 0.30, 0.33, 0.29])
    assert out["covers_zero"] is False
    assert out["ci"][0] > 0.2


def test_two_periods_get_no_interval_rather_than_a_narrow_one():
    """Two numbers can produce a beautifully tight bootstrap interval that means
    nothing at all — the resample can only ever return one of two values."""
    out = backtest.period_bootstrap_ci([0.2, 0.21])
    assert out["ci"] is None and "不给区间" in out["note"]


def test_the_interval_is_reproducible():
    """A number that changes between two runs of the same data is a number a
    reader cannot quote, and this one is quoted in the funnel."""
    a = backtest.period_bootstrap_ci([0.1, -0.2, 0.3, 0.0, 0.15, -0.05])
    b = backtest.period_bootstrap_ci([0.1, -0.2, 0.3, 0.0, 0.15, -0.05])
    assert a["ci"] == b["ci"]
