"""Gates on the performance layer.

Every test here pins a place where a performance report is conventionally wrong
in the flattering direction. That is the selection rule: not "does the function
run" but "would the bug this catches make the strategy look better than it is",
because those are the bugs that survive review — nobody double-checks a number
that agrees with them.
"""
from __future__ import annotations

import math

import pytest

from ideagen import perf


# --------------------------------------------------------------- primitives

def test_normal_quantiles_round_trip():
    for p in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.975, 0.999):
        assert perf.norm_cdf(perf.norm_ppf(p)) == pytest.approx(p, abs=1e-6)
    assert perf.norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)


def test_student_t_tail_is_not_the_normal_tail_at_small_df():
    """At df=4 the normal p-value is roughly half the true one.

    Every family this module deflates has df in single digits, so falling back
    to a normal tail would systematically overstate significance exactly where
    the sample is thinnest.
    """
    assert perf.two_sided_p(2.776, 4) == pytest.approx(0.05, abs=5e-4)
    assert perf.two_sided_p(2.228, 10) == pytest.approx(0.05, abs=5e-4)
    normal_p = 2 * (1 - perf.norm_cdf(2.776))
    assert perf.two_sided_p(2.776, 4) > normal_p * 1.8


def test_to_returns_breaks_the_chain_rather_than_inventing_a_flat_day():
    assert perf.to_returns([100.0, 0.0, 110.0]) == []
    assert perf.to_returns([100.0, 110.0]) == [pytest.approx(0.1)]


# --------------------------------------------------------------- absolute

def _curve(rets, start=100.0):
    eq = [start]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    return [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(len(eq))], eq


def test_one_year_of_known_returns_annualises_to_the_cumulative():
    ds, eq = _curve([0.01] * 252)
    p = perf.performance(ds, eq, rf_annual=0.0)
    assert p.cum_return == pytest.approx(1.01 ** 252 - 1, rel=1e-9)
    assert p.ann_return == pytest.approx(p.cum_return, rel=1e-6)


def test_short_sample_refuses_to_annualise_and_says_why():
    """The single most misleading number an amateur report contains."""
    ds, eq = _curve([0.005] * 27)
    p = perf.performance(ds, eq)
    assert p.ann_return is None
    assert p.ann_return_blocked and "外推" in p.ann_return_blocked
    assert p.cum_return is not None          # the honest number is still there
    assert p.calmar is None                  # needs an annualised numerator
    assert p.mar is not None or p.max_drawdown is None


def test_sortino_denominator_uses_every_period_not_just_the_losers():
    """Dividing by len(down) is the standard implementation error, and it
    inflates the ratio most for the curves with the fewest bad days."""
    rets = [0.01] * 9 + [-0.02]
    ds, eq = _curve(rets)
    p = perf.performance(ds, eq, rf_annual=0.0)
    ex = perf.to_returns(eq)
    down = [x for x in ex if x < 0]
    wrong = math.sqrt(sum(x * x for x in down) / len(down)) * math.sqrt(252)
    assert p.downside_dev == pytest.approx(
        math.sqrt(sum(x * x for x in down) / len(ex)) * math.sqrt(252), rel=1e-9)
    assert p.downside_dev < wrong


def test_var_refuses_when_the_tail_would_be_the_single_worst_day():
    ds, eq = _curve([0.001] * 27)
    p = perf.performance(ds, eq)
    assert p.var95 is None and p.cvar95 is None
    assert p.tail_blocked and "退化" in p.tail_blocked
    ds2, eq2 = _curve([0.001 * ((-1) ** i) for i in range(120)])
    p2 = perf.performance(ds2, eq2)
    assert p2.var95 is not None and p2.cvar95 is not None
    assert p2.cvar95 <= p2.var95


def test_sharpe_carries_an_interval_that_widens_on_fat_tails():
    base = [0.004, -0.003] * 40
    fat = list(base)
    fat[10] = -0.09
    fat[11] = 0.09
    a = perf.performance(*_curve(base), rf_annual=0.0)
    b = perf.performance(*_curve(fat), rf_annual=0.0)
    assert a.sharpe_ci95 and b.sharpe_ci95
    assert b.kurtosis > a.kurtosis
    assert (b.sharpe_ci95[1] - b.sharpe_ci95[0]) > (a.sharpe_ci95[1] - a.sharpe_ci95[0])


def test_unrecovered_drawdown_is_reported_as_open():
    ds = [f"2026-01-{d:02d}" for d in range(1, 7)]
    eq = [100, 120, 110, 105, 108, 107]
    dd = perf.drawdowns(ds, eq)
    assert dd[0].peak_d == "2026-01-02"
    assert dd[0].recovered is False
    assert dd[0].recover_d is None
    assert dd[0].to_recover_sessions is None
    assert dd[0].depth == pytest.approx(105 / 120 - 1)


# --------------------------------------------------------------- relative

def test_benchmark_against_itself_is_beta_one_alpha_zero():
    r = [0.01, -0.005, 0.02, 0.0, -0.01, 0.007, 0.003, -0.002]
    rel = perf.relative(r, r, rf_annual=0.0)
    assert rel.beta == pytest.approx(1.0, abs=1e-9)
    assert rel.alpha_ann == pytest.approx(0.0, abs=1e-9)
    assert rel.r2 == pytest.approx(1.0, abs=1e-9)
    assert rel.tracking_error == pytest.approx(0.0, abs=1e-12)


def test_beta_is_measured_not_assumed_to_be_one():
    """The hole this module was written to close: `_exposure` assumed beta=1
    and its own note told the reader not to cite the column."""
    b = [0.01, -0.01, 0.02, -0.02, 0.005, -0.005, 0.015, -0.015]
    p = [0.5 * x for x in b]
    rel = perf.relative(p, b, rf_annual=0.0)
    assert rel.beta == pytest.approx(0.5, abs=1e-9)


def test_tearsheet_aligns_benchmark_on_dates_not_position():
    """One market's holiday must not pair a portfolio Tuesday with a benchmark
    Wednesday; zipping by index does exactly that and no summary statistic
    reveals it."""
    pds = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06", "2026-01-07"]
    peq = [100.0, 101.0, 102.0, 103.0, 104.0]
    bds = ["2026-01-01", "2026-01-03", "2026-01-06", "2026-01-07"]
    bcl = [50.0, 51.0, 52.0, 53.0]
    ts = perf.tearsheet(pds, peq, bench_dates=bds, bench_closes=bcl)
    assert ts["aligned_days"] == 4
    assert ts["unaligned_days"] == 1
    assert ts["relative"]["n"] == 3


def test_capture_ratios_report_the_day_counts_they_rest_on():
    b = [0.01, 0.02, -0.01, 0.005]
    p = [0.02, 0.01, -0.002, 0.004]
    rel = perf.relative(p, b, rf_annual=0.0)
    assert rel.n_up_days == 3 and rel.n_down_days == 1
    assert rel.down_capture is not None


# --------------------------------------------------------------- family

def test_more_trials_raise_the_hurdle_and_lower_the_deflated_sharpe():
    """The whole point of the DSR: the best of ten worthless arms is not zero."""
    few = {f"a{i}": 1.0 + 0.1 * i for i in range(3)}
    many = {f"a{i}": 1.0 + 0.1 * i for i in range(3)}
    f1 = perf.deflated_sharpe(few, n_obs=250, skew=0.0, kurtosis=3.0)
    f2 = perf.deflated_sharpe(many, n_obs=250, skew=0.0, kurtosis=3.0,
                              extra_trials=40)
    assert f2.n_trials > f1.n_trials
    assert f2.expected_max_sharpe_under_null > f1.expected_max_sharpe_under_null
    assert f2.deflated_sharpe < f1.deflated_sharpe


def test_deflation_names_the_best_arm_and_the_null_hurdle():
    f = perf.deflated_sharpe({"win": 2.0, "mid": 1.0, "lose": 0.2},
                             n_obs=200, skew=-0.2, kurtosis=4.0)
    assert f.best_arm == "win"
    assert f.expected_max_sharpe_under_null > 0
    assert "不是 0" in f.message


def test_benjamini_hochberg_reports_bonferroni_alongside():
    pv = {"a": 0.001, "b": 0.02, "c": 0.3, "d": 0.7}
    out = perf.benjamini_hochberg(pv, alpha=0.05)
    assert out["m"] == 4
    assert out["bonferroni_alpha"] == pytest.approx(0.0125)
    assert "a" in out["rejected"]
    assert "c" not in out["rejected"]
    assert set(out["bonferroni_rejected"]) <= set(out["rejected"])


def test_probabilistic_sharpe_is_lower_for_negative_skew():
    hi = perf.probabilistic_sharpe(0.1, 250, skew=0.5, kurt=3.0)
    lo = perf.probabilistic_sharpe(0.1, 250, skew=-0.5, kurt=3.0)
    assert lo < hi


# --------------------------------------------------------------- assembly

def test_comparison_refuses_to_rank_when_every_interval_covers_the_benchmark():
    wiggle = [0.008, -0.006, 0.011, -0.009, 0.004, -0.003, 0.007]
    curves = {}
    for i, drift in enumerate((0.001, 0.0012, 0.0008)):
        d, eq = _curve([w + drift for w in wiggle] * 4)
        curves[f"arm{i}"] = (d, eq)
    bd, beq = _curve([w + 0.0011 for w in wiggle] * 4)
    rep = perf.compare_arms(curves, bench_dates=bd, bench_closes=beq,
                            rf_annual=0.0)
    assert rep["separability"]["n_arms"] == 3
    assert rep["separability"]["n_measurable"] == 3
    assert rep["separability"]["can_rank"] is False
    assert "分不开" in rep["separability"]["note"]


def test_unmeasurable_arms_do_not_read_as_permission_to_rank():
    """Zero-volatility curves have no interval. "Cannot be measured" and
    "separated from the benchmark" must not share the True branch."""
    curves = {f"arm{i}": _curve([0.001] * 27) for i in range(3)}
    bd, beq = _curve([0.0011] * 27)
    rep = perf.compare_arms(curves, bench_dates=bd, bench_closes=beq,
                            rf_annual=0.0)
    assert rep["separability"]["n_measurable"] == 0
    assert rep["separability"]["can_rank"] is False
    assert "不等于可以排名" in rep["separability"]["note"]


def test_monthly_table_flags_partial_months():
    ds = [f"2026-01-{d:02d}" for d in range(1, 25)] + \
         [f"2026-02-{d:02d}" for d in range(1, 4)]
    eq = [100.0 + i for i in range(len(ds))]
    t = perf.monthly_table(ds, eq)
    assert "2026-02" in t["partial"]
    assert t["years"]["2026"]["months"]["02"]["partial"] is True
    assert t["years"]["2026"]["ytd"] == pytest.approx(eq[-1] / eq[0] - 1)


def test_breakeven_cost_is_the_cost_that_zeroes_the_net_edge():
    pos = [{"arm": "x", "period": "2026-01-01", "return_pct": 1.0},
           {"arm": "x", "period": "2026-01-01", "return_pct": 3.0}]
    c = perf.turnover_and_cost(pos, arm="x", horizon_days=30,
                               applied_cost_pct=0.08)
    assert c["mean_position_return_net_pct"] == pytest.approx(2.0)
    assert c["breakeven_round_trip_pct"] == pytest.approx(2.08)


def test_an_arm_separable_only_by_being_worse_is_not_permission_to_rank():
    """Seen live on 2026-09-04: `mom_21`'s Sharpe interval cleared the
    benchmark's from below, and a single can_rank flag went True on it."""
    wiggle = [0.008, -0.006, 0.011, -0.009, 0.004, -0.003, 0.007]
    curves = {}
    for i, drift in enumerate((0.0009, 0.0011)):
        curves[f"arm{i}"] = _curve([w + drift for w in wiggle] * 4)
    curves["awful"] = _curve([w - 0.02 for w in wiggle] * 4)
    bd, beq = _curve([w + 0.001 for w in wiggle] * 4)
    rep = perf.compare_arms(curves, bench_dates=bd, bench_closes=beq,
                            rf_annual=0.0)
    sep = rep["separability"]
    assert sep["arms_below_benchmark"] == ["awful"]
    assert sep["arms_above_benchmark"] == []
    assert sep["beats_benchmark"] is False
    assert sep["can_rank"] is False
    assert "没有任何一条被证明跑赢基准" in sep["note"]
