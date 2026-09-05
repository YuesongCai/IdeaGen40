"""Portfolio performance analytics for a backtested equity curve.

`backtest.py` answers one question extremely carefully: *did this selector pick
better than that selector, on the same pool, in the same period, and is the
sample big enough to say so.* That is a cross-sectional inference question and
the pairing, the overlap discount and the power gate are the right machinery for
it.

Nobody outside this repository asks that question first. An allocator — Jon, a
fund-of-funds analyst, an IC — asks **"what would this have done to my money,
and how much did it hurt on the way"**, and that is a time-series question about
a NAV curve. Until this module existed the whole answer was two numbers: the
last point of `tranche_curve` and the minimum of its `drawdown` column. A report
built on those two reads as a homework exercise no matter how rigorous the
inference underneath it is, and that mismatch — sound statistics wearing an
amateur's clothes — is what makes a careful reader distrust the entire thing.

So this module computes the standard institutional performance record from the
same daily curve, and nothing here is new science: annualised return and
volatility, Sharpe / Sortino / Calmar, the drawdown table, the monthly grid,
and against a benchmark, regression beta and alpha, tracking error, information
ratio, up/down capture and batting average. A quant desk, a hedge fund DDQ and a
mutual fund factsheet each expect a subset of exactly this list.

**Three things it does that a stock tear-sheet library would not, and they are
the reason it is written here rather than imported.**

1. *It refuses to annualise a five-week track record.* Twenty-eight trading days
   compounded to a year is the single most misleading number an amateur report
   contains, and it is misleading in the flattering direction roughly half the
   time — which is worse, because that is the half that gets shown. `ann_return`
   is `None` below `MIN_DAYS_ANNUAL` and says why. Volatility *is* annualised at
   any length, because √t scaling of a dispersion estimate is a far milder
   extrapolation than compounding a mean, and the report says which is which.

2. *Every risk-adjusted ratio carries its own error bar.* A Sharpe of 1.4 on 28
   days and a Sharpe of 1.4 on 10 years are different claims, and printing them
   the same way is how a backtest lies without stating a single false number.
   `sharpe_se` uses Lo's estimator with the skew and kurtosis terms, so the
   interval widens for exactly the fat-tailed series where the naive one is most
   overconfident. On the current 28-day sample the intervals come out absurdly
   wide, and that is the finding, not a defect in the estimator.

3. *It deflates for the search that produced the winner.* Ten arms were scored
   and the best one gets quoted; the expected maximum Sharpe of ten *worthless*
   arms is not zero. `deflated_sharpe` implements Bailey & López de Prado's DSR
   against that null, and `benjamini_hochberg` controls the false discovery rate
   across the paired tests. This is the open thread Jon raised on 2026-08-18 and
   it had no number attached to it anywhere in the repository.

**The beta problem this closes.** `run_real_backtest._exposure` computes
`excess_over_exposure_pct` by assuming a beta of one against SPY, and its own
docstring warns the reader not to cite the column, because a book of sector,
commodity and currency ETFs does not have unit beta — the top expectation bucket
measured 33.6% annualised volatility against SPY's ~11%. `relative()` regresses
the curve on the benchmark and reports the beta it actually had, so alpha is the
part the market did not hand over. That is the honest version of the column, and
the reason "61% hit rate" was mostly beta was never visible before.

**What is deliberately absent.** No optimiser, no parameter sweep, no in-sample
selection of anything. This module only ever *describes* a curve that was
produced elsewhere. Anything that chooses between curves belongs in
`backtest.paired_difference`, behind its power gate, where the refusal to
conclude already lives.

Decimal in, decimal out: every function takes and returns plain fractions
(0.0123 = +1.23%), and only the printers multiply by 100.
"""

from __future__ import annotations

import math
import statistics as st
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Conventions

#: Trading days per year. Everything annualised divides or multiplies by this.
TRADING_DAYS = 252

#: Below this many observations `ann_return` refuses. Half a trading year is not
#: a statistical threshold — no number of days makes compounding a mean safe —
#: it is the point below which the extrapolation factor (252/n) exceeds 2 and
#: the annualised figure is more artefact than measurement. Stated as a constant
#: so a reader can disagree with the choice rather than with a hidden literal.
MIN_DAYS_ANNUAL = 126

#: Euler–Mascheroni, used by the expected-maximum-Sharpe null in `deflated_sharpe`.
EULER_GAMMA = 0.5772156649015329


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Written out rather than imported because this package's only third-party
    dependency is `futu-api`, and pulling in scipy for one quantile would make
    the analytics layer un-runnable on the cloud node, where the environment is
    built from `requirements.txt` and nothing else.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf 需要 0<p<1，收到 {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _pct(v: float | None) -> str:
    """Decimal fraction to a signed percent string, for the Chinese notes."""
    return "—" if v is None else f"{v * 100:+.2f}%"


def to_returns(equities: Sequence[float]) -> list[float]:
    """Simple period returns from a NAV series. Zero/None points break the chain.

    A missing or zero NAV is dropped rather than carried as a 0% day: an equity
    curve with a hole in it is a curve whose gap has to be visible, and a 0% day
    inserted to keep the array rectangular is the same zero-fill `backtest`
    refuses on the outcome side, applied one level up.
    """
    out: list[float] = []
    for a, b in zip(equities, equities[1:]):
        if a and b:
            out.append(b / a - 1.0)
    return out


# ---------------------------------------------------------------------------
# Drawdowns

@dataclass
class Drawdown:
    peak_d: str
    trough_d: str
    recover_d: str | None
    depth: float                 #: negative fraction, -0.083 = -8.3%
    #: Counted in **sessions**, not calendar days — these come from positions in
    #: the price calendar. A drawdown reported as "9 days" that a reader takes as
    #: calendar time is understated by weekends and holidays, roughly 40%.
    to_trough_sessions: int
    to_recover_sessions: int | None
    recovered: bool


def drawdowns(dates: Sequence[str], equities: Sequence[float], *,
              top: int = 5) -> list[Drawdown]:
    """Every peak-to-trough episode, deepest first, with its recovery.

    Max drawdown alone is one number describing the worst moment; what a risk
    committee actually asks is how long the book stayed underwater and whether it
    ever came back. An unrecovered drawdown is reported with `recovered=False`
    and a `None` recovery date rather than being silently measured to the last
    observation, which would make an ongoing loss look like a closed episode.
    """
    if len(equities) < 2:
        return []
    eps: list[Drawdown] = []
    peak_i, peak = 0, equities[0]
    trough_i, trough = 0, equities[0]
    in_dd = False
    for i in range(1, len(equities)):
        v = equities[i]
        if v >= peak:
            if in_dd:
                eps.append(Drawdown(
                    peak_d=dates[peak_i], trough_d=dates[trough_i],
                    recover_d=dates[i], depth=trough / peak - 1.0,
                    to_trough_sessions=trough_i - peak_i,
                    to_recover_sessions=i - peak_i, recovered=True))
                in_dd = False
            peak_i, peak = i, v
            trough_i, trough = i, v
        elif v < trough or not in_dd:
            in_dd, trough_i, trough = True, i, v
    if in_dd:
        eps.append(Drawdown(
            peak_d=dates[peak_i], trough_d=dates[trough_i], recover_d=None,
            depth=trough / peak - 1.0, to_trough_sessions=trough_i - peak_i,
            to_recover_sessions=None, recovered=False))
    return sorted(eps, key=lambda e: e.depth)[:top]


# ---------------------------------------------------------------------------
# Absolute performance

@dataclass
class Perf:
    """One curve's standalone record. `None` means refused, never zero."""
    n_days: int = 0
    from_d: str | None = None
    to_d: str | None = None
    cum_return: float | None = None
    ann_return: float | None = None
    ann_return_blocked: str | None = None
    ann_vol: float | None = None
    downside_dev: float | None = None
    sharpe: float | None = None
    sharpe_se: float | None = None
    sharpe_ci95: tuple[float, float] | None = None
    psr_vs_zero: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    mar: float | None = None
    max_drawdown: float | None = None
    drawdown_table: list[Drawdown] = field(default_factory=list)
    days_underwater: int = 0
    frac_underwater: float | None = None
    pct_positive_days: float | None = None
    best_day: float | None = None
    worst_day: float | None = None
    skew: float | None = None
    kurtosis: float | None = None
    var95: float | None = None
    cvar95: float | None = None
    tail_blocked: str | None = None
    rf_annual: float = 0.0
    annualisation_note: str = ""


def _moments(rets: Sequence[float]) -> tuple[float, float, float, float]:
    """mean, population sd, skew, **non-excess** kurtosis (3.0 for a normal).

    Non-excess is not a stylistic choice: Bailey & López de Prado's PSR
    denominator is written with γ4 as the raw fourth standardised moment, and
    feeding it excess kurtosis silently shifts every probability by the same
    0.25·SR². Keeping one convention through the file removes the chance of
    mixing them.
    """
    n = len(rets)
    m = _mean(rets)
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / n)
    if sd <= 0:
        return m, sd, 0.0, 3.0
    z3 = sum(((x - m) / sd) ** 3 for x in rets) / n
    z4 = sum(((x - m) / sd) ** 4 for x in rets) / n
    return m, sd, z3, z4


def probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float,
                         sr_star: float = 0.0) -> float | None:
    """P(true Sharpe > `sr_star`), correcting for sample length and non-normality.

    Bailey & López de Prado (2012). All four arguments must be on the **same
    frequency** — this file always passes per-period (daily) values, never
    annualised ones, because annualising the numerator without annualising the
    variance term is a mistake that produces confident nonsense.
    """
    if n < 3:
        return None
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return None
    return norm_cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom))


def sharpe_stderr(sr: float, n: int, skew: float, kurt: float) -> float | None:
    """Standard error of a Sharpe estimate under IID-with-higher-moments (Lo 2002).

    Same denominator as `probabilistic_sharpe`, which is the point: a report that
    quoted a normal-theory error bar next to a non-normal probability would be
    two different claims about the same sample.
    """
    if n < 3:
        return None
    v = 1.0 + 0.5 * sr * sr - skew * sr + (kurt - 3.0) / 4.0 * sr * sr
    return None if v <= 0 else math.sqrt(v / n)


def performance(dates: Sequence[str], equities: Sequence[float], *,
                rf_annual: float = 0.0,
                periods_per_year: int = TRADING_DAYS,
                min_days_annual: int = MIN_DAYS_ANNUAL) -> Perf:
    """The standalone record for one NAV curve.

    Sharpe is computed on the **per-period** excess return and annualised by
    √periods_per_year at the end. Doing it the other way — annualising the mean
    and the vol separately and dividing — gives the same answer only when the
    risk-free is zero, and this repository's is 3.72%.
    """
    p = Perf(rf_annual=rf_annual)
    if len(equities) < 2:
        return p
    p.n_days, p.from_d, p.to_d = len(equities), dates[0], dates[-1]
    rets = to_returns(equities)
    if len(rets) < 2:
        return p
    p.cum_return = equities[-1] / equities[0] - 1.0

    if p.n_days - 1 >= min_days_annual:
        yrs = (p.n_days - 1) / periods_per_year
        p.ann_return = (1.0 + p.cum_return) ** (1.0 / yrs) - 1.0
    else:
        p.ann_return_blocked = (
            f"样本只有 {p.n_days - 1} 个交易日，不足 {min_days_annual} 天"
            f"（约半年）。把它按 {periods_per_year}/{p.n_days - 1}"
            f"={periods_per_year / max(1, p.n_days - 1):.1f} 倍复利外推到年化，"
            f"得到的是外推假象而不是业绩——所以这里不给年化收益，只给区间累计收益。")

    m, sd, sk, ku = _moments(rets)
    p.skew, p.kurtosis = round(sk, 4), round(ku, 4)
    p.ann_vol = sd * math.sqrt(periods_per_year)
    p.best_day, p.worst_day = max(rets), min(rets)
    p.pct_positive_days = sum(1 for r in rets if r > 0) / len(rets)

    rf_d = (1.0 + rf_annual) ** (1.0 / periods_per_year) - 1.0
    ex = [r - rf_d for r in rets]
    ex_m = _mean(ex)
    ex_sd = math.sqrt(sum((x - ex_m) ** 2 for x in ex) / len(ex))
    if ex_sd > 0:
        sr_d = ex_m / ex_sd
        p.sharpe = sr_d * math.sqrt(periods_per_year)
        se = sharpe_stderr(sr_d, len(ex), sk, ku)
        if se is not None:
            p.sharpe_se = se * math.sqrt(periods_per_year)
            half = 1.959964 * p.sharpe_se
            p.sharpe_ci95 = (p.sharpe - half, p.sharpe + half)
        p.psr_vs_zero = probabilistic_sharpe(sr_d, len(ex), sk, ku, 0.0)

    down = [x for x in ex if x < 0]
    if down:
        # Sortino's denominator is the root-mean-square of the shortfalls over
        # *all* periods, not over the losing ones. Dividing by len(down) is the
        # common implementation error and it inflates the ratio precisely for the
        # curves with the fewest bad days — the ones a reader is least able to
        # sanity-check.
        dd = math.sqrt(sum(x * x for x in down) / len(ex))
        p.downside_dev = dd * math.sqrt(periods_per_year)
        if dd > 0:
            p.sortino = ex_m / dd * math.sqrt(periods_per_year)

    p.drawdown_table = drawdowns(dates, equities, top=5)
    if p.drawdown_table:
        p.max_drawdown = p.drawdown_table[0].depth
    peak = equities[0]
    uw = 0
    for v in equities[1:]:
        peak = max(peak, v)
        uw += 1 if v < peak else 0
    p.days_underwater = uw
    p.frac_underwater = uw / (p.n_days - 1)

    if p.max_drawdown and p.max_drawdown < 0:
        # Calmar wants an annualised numerator and is therefore blocked whenever
        # the annualised return is. MAR here is the un-annualised sibling —
        # cumulative return over max drawdown — which is honest at any length and
        # is what actually gets read on a short sample.
        p.mar = p.cum_return / abs(p.max_drawdown)
        if p.ann_return is not None:
            p.calmar = p.ann_return / abs(p.max_drawdown)

    # A 5% tail needs enough observations to *be* a tail. At n=27 the empirical
    # 5% quantile is the single worst day, so VaR and CVaR both collapse onto it
    # and report the minimum under two more impressive names. Three observations
    # is still thin, but below it the statistic is not thin — it is a different
    # statistic wearing this one's label.
    srt = sorted(rets)
    k = int(math.floor(0.05 * len(srt)))
    if k >= 3:
        p.var95 = srt[k - 1]
        p.cvar95 = _mean(srt[:k])
    else:
        p.tail_blocked = (
            f"样本 {len(srt)} 个交易日，5% 尾部只有 {k} 个观测，"
            f"经验分位数会退化成「最差的那一天」——VaR/CVaR 不给，"
            f"最差单日已单列在 worst_day。")

    p.annualisation_note = (
        f"年化波动按 √{periods_per_year} 缩放（离散度的 √t 缩放是温和外推）；"
        f"年化收益需要复利外推，样本不足时直接不给。无风险利率按 {rf_annual*100:.2f}% 年化，"
        f"逐日折算后从每日收益里减掉，再算夏普——先年化再相除只在无风险为 0 时才等价。")
    return p


# ---------------------------------------------------------------------------
# Benchmark-relative

@dataclass
class Relative:
    benchmark: str = ""
    n: int = 0
    beta: float | None = None
    alpha_ann: float | None = None
    alpha_t: float | None = None
    alpha_significant: bool = False
    r2: float | None = None
    correlation: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    residual_vol: float | None = None
    appraisal_ratio: float | None = None
    #: The appraisal ratio this window would need before alpha clears ±1.96.
    #: Printed beside the ratio so it cannot become the new flattering headline
    #: the moment the information ratio stops being one.
    appraisal_needed_for_significance: float | None = None
    beta_matched_bench_return: float | None = None
    excess_vs_beta_matched: float | None = None
    up_capture: float | None = None
    down_capture: float | None = None
    capture_spread: float | None = None
    batting_average: float | None = None
    n_up_days: int = 0
    n_down_days: int = 0
    excess_cum: float | None = None
    note: str = ""


def relative(port_rets: Sequence[float], bench_rets: Sequence[float], *,
             benchmark: str = "SPY", rf_annual: float = 0.0,
             periods_per_year: int = TRADING_DAYS) -> Relative:
    """Regression beta and alpha, plus the capture statistics a factsheet carries.

    This is the function that replaces `_exposure`'s beta-of-one assumption. That
    approximation was stated honestly in its own note — "该折算假设对 SPY 的 beta
    为 1，而这些组合并不满足" — but an approximation a report tells you not to cite
    is a hole where a number should be. Regressing excess portfolio return on
    excess benchmark return gives the beta the book actually ran, and alpha is
    then the part the market did not hand over.

    `alpha_t` is compared against ±1.96 and reported as `alpha_significant`, with
    the usual caveat that daily alpha t-stats on a few weeks of overlapping
    tranches are optimistic — the honest sample-size discount for this book lives
    in `backtest.paired_difference`, and nothing here overrides it.

    Capture ratios are computed on the benchmark's up and down periods separately
    and both counts are reported, because a down-capture computed from three down
    days is a number about three days.
    """
    r = Relative(benchmark=benchmark)
    n = min(len(port_rets), len(bench_rets))
    if n < 3:
        return r
    pr, br = list(port_rets[:n]), list(bench_rets[:n])
    r.n = n

    rf_d = (1.0 + rf_annual) ** (1.0 / periods_per_year) - 1.0
    px = [x - rf_d for x in pr]
    bx = [x - rf_d for x in br]
    bm, pm = _mean(bx), _mean(px)
    sxx = sum((b - bm) ** 2 for b in bx)
    if sxx > 0:
        sxy = sum((b - bm) * (p - pm) for b, p in zip(bx, px))
        r.beta = sxy / sxx
        a_d = pm - r.beta * bm
        resid = [p - (a_d + r.beta * b) for p, b in zip(px, bx)]
        sse = sum(e * e for e in resid)
        sst = sum((p - pm) ** 2 for p in px)
        r.r2 = 1.0 - sse / sst if sst > 0 else None
        if n > 2 and sse > 0:
            # Standard error of the intercept from the residual variance.
            s2 = sse / (n - 2)
            se_a = math.sqrt(s2 * (1.0 / n + bm * bm / sxx))
            if se_a > 0:
                r.alpha_t = a_d / se_a
                r.alpha_significant = abs(r.alpha_t) >= 1.959964
        # Alpha is a per-period intercept; annualising it by simple scaling is
        # the market convention and is stated rather than compounded, because
        # compounding an intercept mixes it with the beta term.
        r.alpha_ann = a_d * periods_per_year

        # Residual (idiosyncratic) volatility and the appraisal ratio.
        #
        # This is the metric the information ratio should have been compared
        # against all along, and its absence is why the first run of this table
        # read as an indictment. A book running beta 0.09 against a fully
        # invested index is *mostly* being measured on the exposure it does not
        # have: tracking error against SPY is then dominated by the missing
        # 0.91 of market, and IR goes negative in a rising tape no matter how
        # good the selection is. The appraisal ratio divides alpha by the
        # volatility of what is left after beta is taken out, which is the part
        # the manager is actually responsible for.
        #
        # It is not a kinder number by construction — it is a different
        # question. IR asks "was deviating from the index worth it"; the
        # appraisal ratio asks "per unit of risk you took that the index did not
        # give you, what did you earn". A closet tracker can have a fine IR and
        # no appraisal ratio; this book is the opposite shape and needs both.
        if n > 2:
            rv = math.sqrt(sse / (n - 2))
            r.residual_vol = rv * math.sqrt(periods_per_year)
            if rv > 0:
                r.appraisal_ratio = a_d / rv * math.sqrt(periods_per_year)
                # The appraisal ratio and alpha's t statistic are the same
                # quantity in different units: t ≈ AR·√(n/periods_per_year).
                # Writing the inverse down converts "3.52 looks impressive" into
                # "this window needs 6.0", which is the only form that survives
                # being quoted. Without it the appraisal ratio simply becomes
                # the next number someone reads as a result.
                r.appraisal_needed_for_significance = (
                    1.959964 * math.sqrt(periods_per_year / n))

        # What the same *market exposure* would have returned: beta of the
        # index, the rest in cash. Cumulative, because that is the form a PM
        # reads, and stated beside the raw excess so the two cannot be confused.
        bm = [r.beta * x + (1 - r.beta) * rf_d for x in br]
        r.beta_matched_bench_return = math.prod(1 + x for x in bm) - 1

    sp = st.pstdev(pr) if n > 1 else 0.0
    sb = st.pstdev(br) if n > 1 else 0.0
    if sp > 0 and sb > 0:
        cov = sum((a - _mean(pr)) * (b - _mean(br)) for a, b in zip(pr, br)) / n
        r.correlation = cov / (sp * sb)

    diff = [a - b for a, b in zip(pr, br)]
    if len(diff) > 1:
        te_d = st.stdev(diff)
        r.tracking_error = te_d * math.sqrt(periods_per_year)
        if te_d > 0:
            r.information_ratio = _mean(diff) / te_d * math.sqrt(periods_per_year)
    r.excess_cum = (math.prod(1 + x for x in pr)
                    - math.prod(1 + x for x in br))
    if r.beta_matched_bench_return is not None:
        r.excess_vs_beta_matched = (math.prod(1 + x for x in pr) - 1
                                    - r.beta_matched_bench_return)
    r.batting_average = sum(1 for d in diff if d > 0) / len(diff)

    up = [(a, b) for a, b in zip(pr, br) if b > 0]
    dn = [(a, b) for a, b in zip(pr, br) if b < 0]
    r.n_up_days, r.n_down_days = len(up), len(dn)
    if up:
        bu = math.prod(1 + b for _, b in up) - 1
        pu = math.prod(1 + a for a, _ in up) - 1
        r.up_capture = pu / bu if bu else None
    if dn:
        bd = math.prod(1 + b for _, b in dn) - 1
        pd_ = math.prod(1 + a for a, _ in dn) - 1
        r.down_capture = pd_ / bd if bd else None
    if r.up_capture is not None and r.down_capture is not None:
        r.capture_spread = r.up_capture - r.down_capture

    r.note = (
        f"beta / alpha 来自把组合超额收益对基准（{benchmark}）超额收益做回归，"
        f"不再假设 beta=1。上下行捕获分别只用基准上涨的 {r.n_up_days} 天和下跌的 "
        f"{r.n_down_days} 天算——天数少时这两个比率就是关于那几天的陈述，别当稳定特征读。"
        f"alpha 的 t 值按逐日残差算，未对分批持仓的重叠做折算，偏乐观；"
        f"样本量的正式口径在 backtest.paired_difference。"
        f"「IR」和「评价比率」问的不是同一件事：IR 拿相对基准的偏离"
        f"（含「没拿满市场」这部分）做分母，一个 beta 只有 {r.beta:.2f} 的组合"
        f"在上涨行情里 IR 必然难看，那主要是敞口差不是能力差；"
        f"appraisal_ratio 拿剔掉 beta 之后剩下的特质波动做分母，"
        f"量的才是这个选取策略自己挣到的部分。beta_matched_bench_return 是"
        f"「同样市场敞口、其余拿现金」会有的收益，excess_vs_beta_matched 是相对它的差。"
        f"但评价比率不是新的结论：它和 alpha 的 t 值是同一个量的两种单位，"
        f"t ≈ 评价比率 × √(n/252)。本窗口 n={r.n}，"
        f"评价比率要到 {r.appraisal_needed_for_significance:.1f} 以上 alpha 才过 ±1.96——"
        f"低于这个数的，好看也只是好看。"
        if r.appraisal_needed_for_significance else
        f"appraisal_ratio 拿剔掉 beta 之后剩下的特质波动做分母。"
        if r.beta is not None else
        f"alpha 的 t 值按逐日残差算，未对分批持仓的重叠做折算，偏乐观；"
        f"样本量的正式口径在 backtest.paired_difference。")
    return r


# ---------------------------------------------------------------------------
# The monthly grid

def monthly_table(dates: Sequence[str], equities: Sequence[float]
                  ) -> dict[str, Any]:
    """Year × month returns plus a YTD column — the factsheet's signature block.

    Partial months are flagged rather than dropped or padded. A month that has
    six trading days in it is a real observation of those six days and a fiction
    if it is read as a month, so it is returned with `partial: True` and the
    printer marks it.
    """
    if len(equities) < 2:
        return {"years": {}, "months_seen": [], "partial": []}
    by_month: dict[str, list[tuple[str, float]]] = {}
    for d, e in zip(dates, equities):
        by_month.setdefault(d[:7], []).append((d, e))
    ordered = sorted(by_month)
    years: dict[str, dict[str, Any]] = {}
    partial: list[str] = []
    prev_close: float | None = None
    for ym in ordered:
        rows = sorted(by_month[ym])
        start = prev_close if prev_close is not None else rows[0][1]
        end = rows[-1][1]
        ret = end / start - 1.0 if start else None
        y, m = ym[:4], ym[5:7]
        is_partial = len(rows) < 15
        if is_partial:
            partial.append(ym)
        years.setdefault(y, {"months": {}, "ytd": None})
        years[y]["months"][m] = {"ret": ret, "n_days": len(rows),
                                 "partial": is_partial}
        prev_close = end
    for y, blk in years.items():
        acc = 1.0
        for m in sorted(blk["months"]):
            v = blk["months"][m]["ret"]
            if v is not None:
                acc *= 1 + v
        blk["ytd"] = acc - 1.0
    return {"years": years, "months_seen": ordered, "partial": partial}


# ---------------------------------------------------------------------------
# Multiple testing across arms

@dataclass
class FamilyDeflation:
    n_trials: int = 0
    best_arm: str = ""
    best_sharpe: float | None = None
    sharpe_variance: float | None = None
    expected_max_sharpe_under_null: float | None = None
    deflated_sharpe: float | None = None
    survives: bool = False
    message: str = ""


def deflated_sharpe(arm_sharpes: dict[str, float], *, n_obs: int,
                    skew: float, kurtosis: float,
                    periods_per_year: int = TRADING_DAYS,
                    extra_trials: int = 0) -> FamilyDeflation:
    """Bailey & López de Prado's DSR: is the best of N arms better than the best
    of N *worthless* arms?

    The intuition the report has to carry: run ten coin-flipping strategies over
    a short window and the best of them has a visibly positive Sharpe. Quoting it
    without saying ten were tried is the standard way a backtest overstates
    itself, and it is the specific objection Jon raised on 2026-08-18 that this
    repository had no number for.

    `n_trials` counts the arms actually scored plus `extra_trials`, which exists
    because the honest trial count is larger than the arm count and always will
    be: `ev_rank` was chosen after looking at these periods, and every ranking
    rule considered and discarded along the way was a trial too. Passing zero
    understates the deflation; the field is there so a caller states the number
    rather than letting the arm count quietly stand in for it.

    Sharpes go in **annualised** and are converted internally, because that is the
    unit the rest of the report speaks and a silent unit mismatch here would move
    the answer without moving any visible number.
    """
    f = FamilyDeflation()
    vals = [v for v in arm_sharpes.values() if v is not None]
    if len(vals) < 2 or n_obs < 3:
        f.message = "参赛组合少于 2 条或样本不足 3 个观测，无法做多重检验折减。"
        return f
    f.n_trials = len(vals) + max(0, extra_trials)
    f.best_arm = max((k for k, v in arm_sharpes.items() if v is not None),
                     key=lambda k: arm_sharpes[k])
    f.best_sharpe = arm_sharpes[f.best_arm]

    scale = math.sqrt(periods_per_year)
    per = [v / scale for v in vals]
    v_sr = st.variance(per) if len(per) > 1 else 0.0
    f.sharpe_variance = v_sr
    if v_sr <= 0:
        f.message = "各组合夏普完全相同，方差为 0，折减基准无从估计。"
        return f

    n = f.n_trials
    e_max = math.sqrt(v_sr) * (
        (1 - EULER_GAMMA) * norm_ppf(1 - 1.0 / n)
        + EULER_GAMMA * norm_ppf(1 - 1.0 / (n * math.e)))
    f.expected_max_sharpe_under_null = e_max * scale
    f.deflated_sharpe = probabilistic_sharpe(
        f.best_sharpe / scale, n_obs, skew, kurtosis, e_max)
    if f.deflated_sharpe is None:
        f.message = "PSR 分母非正（偏度/峰度组合越界），不给折减后概率。"
        return f
    f.survives = f.deflated_sharpe >= 0.95
    f.message = (
        f"试了 {n} 条组合，最好的是 {f.best_arm}（年化夏普 {f.best_sharpe:+.2f}）。"
        f"{n} 条毫无能力的组合，光靠运气，最好那条的年化夏普期望也有 "
        f"{f.expected_max_sharpe_under_null:+.2f}——这才是它要跨过的门槛，不是 0。"
        f"折减后 DSR={f.deflated_sharpe:.3f}"
        + ("，≥0.95，在这个样本上扛住了多重检验。"
           if f.survives else
           "，未达 0.95：把「试了这么多条」算进去之后，冠军这条还不能算被证明。"))
    return f


def benjamini_hochberg(pvals: dict[str, float], *, alpha: float = 0.05
                       ) -> dict[str, Any]:
    """FDR control over the family of paired tests, one per arm.

    Bonferroni is reported alongside because the two answer different questions
    and a reader who knows one usually wants the other: Bonferroni bounds the
    chance of *any* false positive, BH bounds the expected *share* of the
    discoveries that are false. On a ten-arm family Bonferroni's threshold is
    α/10 = 0.005, which almost nothing here will ever clear, and saying so is
    more useful than quietly picking the looser test.
    """
    items = sorted(((k, v) for k, v in pvals.items() if v is not None),
                   key=lambda kv: kv[1])
    m = len(items)
    if not m:
        return {"m": 0, "alpha": alpha, "rejected": [], "threshold": None,
                "bonferroni_alpha": None, "bonferroni_rejected": []}
    thresh = None
    for i, (_, p) in enumerate(items, start=1):
        if p <= i / m * alpha:
            thresh = p
    rejected = [k for k, p in items if thresh is not None and p <= thresh]
    bonf = alpha / m
    return {
        "m": m, "alpha": alpha,
        "threshold": thresh,
        "rejected": rejected,
        "bonferroni_alpha": bonf,
        "bonferroni_rejected": [k for k, p in items if p <= bonf],
        "ranked": [{"arm": k, "p": p, "bh_line": round(i / m * alpha, 5)}
                   for i, (k, p) in enumerate(items, start=1)],
        "note": (
            f"同时检验 {m} 条组合。Bonferroni 把单条门槛压到 α/m={bonf:.4f}"
            f"（控制「出现任何一个假阳性」的概率）；BH 控制的是"
            f"「被判为发现的那些里，假的占比」的期望，门槛更松。"
            f"两个都给，是因为只报松的那个等于替结论挑尺子。"),
    }


def two_sided_p(t: float | None, df: int) -> float | None:
    """Two-sided p from a t statistic, via a normal approximation above df=30.

    Below that the normal p is materially too small, so the tail is taken from a
    Student-t CDF computed with the regularised incomplete beta — worth the forty
    lines because every family this module will ever deflate has df in single
    digits, which is exactly where the approximation fails.
    """
    if t is None or df < 1:
        return None
    x = abs(t)
    if df > 200:
        return 2.0 * (1.0 - norm_cdf(x))
    return _betainc(df / 2.0, 0.5, df / (df + x * x))


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a,b) by continued fraction (Lentz)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1 - x) / b


def _betacf(a: float, b: float, x: float, itmax: int = 200,
            eps: float = 3e-14) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < 1e-30:
            d = 1e-30
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < 1e-30:
            d = 1e-30
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


# ---------------------------------------------------------------------------
# Implementation reality: turnover and the cost that kills the edge

def turnover_and_cost(positions: Sequence[dict[str, Any]], *,
                      arm: str, horizon_days: int,
                      applied_cost_pct: float) -> dict[str, Any]:
    """Round trips per year and the round-trip cost that would zero the edge.

    Deutsche Bank's seventh sin is presenting a Sharpe without turnover, borrow
    and impact beside it, and it is the sin most likely to survive every other
    check in this repository: the cost model here is a single round-trip constant
    applied identically to every arm, so it can never change a *ranking* and will
    therefore never trip a paired test. What it can do is make the whole exercise
    uneconomic, and the only number that shows it is the break-even.

    `breakeven_round_trip_pct` is what one full round trip would have to cost
    before the arm's average position return reaches zero. Reading it is
    immediate: if the answer is smaller than the cost actually charged, the arm
    has no edge net of trading; if it is many multiples of it, cost is not what
    decides this question.
    """
    # Deduplicated by (period, instrument): the same ETF proposed by three
    # generators is three idea rows and **one purchase**. Counting idea rows
    # here inflated ev_rank's opening count from 2.7 to 8.0 per period, and
    # every number derived from position size — cost per trade, and capacity
    # next door — moves with it. The win-rate table upstream already reports
    # 标的×期; this is the same unit, so the two cannot disagree.
    seen: set[tuple[str, str]] = set()
    rows = []
    for r in positions:
        if r.get("arm") != arm or r.get("return_pct") is None:
            continue
        k = (str(r.get("period")), str(r.get("instrument_id")))
        if k in seen:
            continue
        seen.add(k)
        rows.append(r)
    if not rows:
        return {"arm": arm, "n_positions": 0}
    periods = sorted({str(r.get("period")) for r in rows})
    per_period = len(rows) / max(1, len(periods))
    # A tranche is opened and closed once per horizon, so annual round trips per
    # unit of capital is the number of horizons in a year, not the number of
    # periods — the tranches overlap and each unit of capital only turns once.
    round_trips_yr = TRADING_DAYS / max(1, horizon_days) * (365.0 / 365.0)
    mean_ret = _mean([float(r["return_pct"]) for r in rows]) / 100.0
    return {
        "arm": arm,
        "n_positions": len(rows),
        "n_periods": len(periods),
        "positions_per_period": round(per_period, 2),
        "dedup": "按（期次，标的）去重——同一只标的被多种生成方式提出只买一次",
        "holding_days": horizon_days,
        "round_trips_per_year": round(round_trips_yr, 2),
        "applied_round_trip_pct": applied_cost_pct,
        "mean_position_return_net_pct": round(mean_ret * 100, 4),
        "breakeven_round_trip_pct": round((mean_ret + applied_cost_pct / 100.0)
                                          * 100, 4),
        "note": (
            f"该组合每期开 {per_period:.1f} 个仓、持有 {horizon_days} 天，"
            f"每单位资金一年周转约 {round_trips_yr:.1f} 次。"
            f"已扣的买卖两腿成本是 {applied_cost_pct:.2f}%；"
            f"要把平均单笔收益打到 0，单次往返成本需达到 "
            f"{(mean_ret + applied_cost_pct/100.0)*100:.2f}%。"
            f"这个数小于已扣成本，就说明扣完交易费之后没有超额可言。"),
    }


# ---------------------------------------------------------------------------
# Assembly

def tearsheet(dates: Sequence[str], equities: Sequence[float], *,
              bench_dates: Sequence[str] | None = None,
              bench_closes: Sequence[float] | None = None,
              benchmark: str = "SPY",
              alt_bench_dates: Sequence[str] | None = None,
              alt_bench_closes: Sequence[float] | None = None,
              alt_benchmark: str = "",
              rf_annual: float = 0.0,
              periods_per_year: int = TRADING_DAYS) -> dict[str, Any]:
    """One arm's full record: absolute, relative, monthly grid.

    The benchmark series is aligned on **dates**, not on position. Two series
    zipped by index silently pair a portfolio Tuesday with a benchmark Wednesday
    the first time one market has a holiday the other does not, and the resulting
    beta is wrong in a way no summary statistic reveals.

    A second, optional benchmark is carried in `relative_alt` for one specific
    reason: an index of US large-cap equity and an equal-weight index of this
    book's own shelf answer different questions, and only the second one is about
    picking. Against SPY an arm can look like alpha while it is merely holding a
    lower-beta shelf in a rising market; against the shelf that story has to
    survive its own universe. Both are reported rather than one being chosen,
    because "would the client rather have held stocks" and "did selection add
    anything" are both real questions and neither answer substitutes for the
    other.
    """
    perf = performance(dates, equities, rf_annual=rf_annual,
                       periods_per_year=periods_per_year)
    out: dict[str, Any] = {
        "performance": _clean(asdict(perf)),
        "monthly": monthly_table(dates, equities),
        "relative": None,
    }

    def _against(bd: Sequence[str] | None, bc: Sequence[float] | None,
                 name: str) -> tuple[dict[str, Any] | None, int]:
        if not (bd and bc):
            return None, 0
        bmap = dict(zip(bd, bc))
        common = [d for d in dates if d in bmap]
        if len(common) < 4:
            return None, len(common)
        pe = {d: e for d, e in zip(dates, equities)}
        rel = relative(to_returns([pe[d] for d in common]),
                       to_returns([bmap[d] for d in common]),
                       benchmark=name, rf_annual=rf_annual,
                       periods_per_year=periods_per_year)
        return _clean(asdict(rel)), len(common)

    rel_main, aligned = _against(bench_dates, bench_closes, benchmark)
    if rel_main is not None:
        out["relative"] = rel_main
        out["aligned_days"] = aligned
        out["unaligned_days"] = len(dates) - aligned
    rel_alt, aligned_alt = _against(alt_bench_dates, alt_bench_closes,
                                    alt_benchmark or "alt")
    if rel_alt is not None:
        out["relative_alt"] = rel_alt
        out["aligned_days_alt"] = aligned_alt
    return out


def _clean(d: Any) -> Any:
    """Round floats for storage. NaN/inf become None rather than travelling into
    JSON, where `allow_nan=False` in `run_real_backtest` would abort the write
    with a message pointing at serialisation instead of at the statistic."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_clean(v) for v in d]
    if isinstance(d, float):
        return None if (math.isnan(d) or math.isinf(d)) else round(d, 6)
    return d


# ---------------------------------------------------------------------------
# The one screen a PM reads

def compare_arms(curves: dict[str, tuple[Sequence[str], Sequence[float]]], *,
                 bench_dates: Sequence[str] | None = None,
                 bench_closes: Sequence[float] | None = None,
                 benchmark: str = "SPY",
                 alt_bench_dates: Sequence[str] | None = None,
                 alt_bench_closes: Sequence[float] | None = None,
                 alt_benchmark: str = "",
                 rf_annual: float = 0.0,
                 paired_t: dict[str, tuple[float | None, int]] | None = None,
                 positions: Sequence[dict[str, Any]] | None = None,
                 horizon_days: int = 30,
                 applied_cost_pct: float = 0.0,
                 extra_trials: int = 0,
                 per_period_returns: dict[str, dict[str, float | None]] | None = None,
                 posthoc_arms: Sequence[str] = (),
                 control: str | None = None,
                 periods_per_year: int = TRADING_DAYS) -> dict[str, Any]:
    """Every arm's tear sheet plus the family-level checks, in one object.

    The family checks are the reason this is not just a loop over `tearsheet`.
    Ten arms each get an individually defensible record and the *set* of them
    still overstates itself, because the reader's eye goes to the best row. So
    the deflation and the FDR control are computed once, over the family, and
    returned beside the rows rather than left for a reader to remember to apply.
    """
    out: dict[str, Any] = {
        "benchmark": benchmark, "rf_annual": rf_annual,
        "periods_per_year": periods_per_year, "arms": {},
    }
    for name, (ds, eq) in sorted(curves.items()):
        out["arms"][name] = tearsheet(
            ds, eq, bench_dates=bench_dates, bench_closes=bench_closes,
            benchmark=benchmark,
            alt_bench_dates=alt_bench_dates, alt_bench_closes=alt_bench_closes,
            alt_benchmark=alt_benchmark,
            rf_annual=rf_annual, periods_per_year=periods_per_year)

    if bench_dates and bench_closes:
        bp = performance(list(bench_dates), list(bench_closes),
                         rf_annual=rf_annual, periods_per_year=periods_per_year)
        out["benchmark_performance"] = _clean(asdict(bp))
    if alt_bench_dates and alt_bench_closes:
        out["alt_benchmark"] = alt_benchmark or "alt"
        out["alt_benchmark_performance"] = _clean(asdict(performance(
            list(alt_bench_dates), list(alt_bench_closes),
            rf_annual=rf_annual, periods_per_year=periods_per_year)))

    sharpes = {n: (t["performance"] or {}).get("sharpe")
               for n, t in out["arms"].items()}
    n_obs = max((t["performance"].get("n_days", 1) - 1
                 for t in out["arms"].values()), default=0)
    best = max((k for k, v in sharpes.items() if v is not None),
               key=lambda k: sharpes[k], default=None)
    if best:
        bm = out["arms"][best]["performance"]
        out["family_deflation"] = _clean(asdict(deflated_sharpe(
            {k: v for k, v in sharpes.items() if v is not None},
            n_obs=n_obs, skew=bm.get("skew") or 0.0,
            kurtosis=bm.get("kurtosis") or 3.0,
            periods_per_year=periods_per_year, extra_trials=extra_trials)))

    if paired_t:
        pv = {k: two_sided_p(t, df) for k, (t, df) in paired_t.items()}
        out["fdr"] = _clean(benjamini_hochberg(pv))
        out["paired_p"] = _clean(pv)

    # Does choosing between arms pay for itself? Two runs, because the answer
    # depends entirely on whether an arm designed after seeing this window is
    # allowed into the roster — and on the 2026-09-04 data that single
    # inclusion is worth more than the whole apparent edge.
    if per_period_returns:
        allv = walk_forward_selection(
            per_period_returns, control=control, label="all_arms")
        out["walk_forward"] = {"all_arms": _clean(asdict(allv))}
        if posthoc_arms:
            clean = walk_forward_selection(
                per_period_returns, control=control,
                exclude=posthoc_arms, label="no_posthoc")
            out["walk_forward"]["no_posthoc"] = _clean(asdict(clean))
            if (allv.follow_leader_mean is not None
                    and clean.follow_leader_mean is not None):
                gap = clean.follow_leader_mean - allv.follow_leader_mean
                out["walk_forward"]["contamination"] = round(gap, 6)
                out["walk_forward"]["note"] = (
                    f"把事后设计的组合（{'、'.join(sorted(posthoc_arms))}）从候选名单里"
                    f"拿掉，跟随领先者的成绩从 {_pct(allv.follow_leader_mean)}/期变成 "
                    f"{_pct(clean.follow_leader_mean)}/期，差 {_pct(gap)}。"
                    f"前推检验管的是「什么时候做的选择」，管不了「这个组合什么时候被造出来」"
                    f"——名单本身就可能带未来信息，而且这次带的比全部表面优势还多。"
                    f"该读的是 no_posthoc 那一栏。")

    # PBO over the daily curves, at three block counts.
    daily = {n: to_returns(list(eq)) for n, (_, eq) in curves.items()}
    if len(daily) >= 2 and min(len(v) for v in daily.values()) >= 24:
        out["pbo"] = pbo_sweep(daily)

    if positions:
        out["cost_reality"] = {
            n: turnover_and_cost(positions, arm=n, horizon_days=horizon_days,
                                 applied_cost_pct=applied_cost_pct)
            for n in out["arms"]}

    # The single most important sentence in the object, computed rather than
    # written: when every arm's Sharpe interval contains the benchmark's Sharpe,
    # the table cannot rank anything and must say so before it is read. The
    # direction of that verdict, and why it is not one flag, is in `_separability`.
    out["separability"] = _separability(
        out["arms"], (out.get("benchmark_performance") or {}).get("sharpe"),
        n_obs, benchmark)
    # The same verdict against the book's own shelf. This is the one that speaks
    # to selection: clearing SPY can be had by holding a calmer shelf in a rising
    # tape, and an arm that clears SPY while its interval sits below the shelf's
    # Sharpe has not been shown to have picked anything. Reported beside the
    # first rather than replacing it — see `tearsheet`.
    if out.get("alt_benchmark_performance"):
        out["separability_vs_alt"] = _separability(
            out["arms"], out["alt_benchmark_performance"].get("sharpe"),
            n_obs, out.get("alt_benchmark") or "alt", alt=True)
    return out


def _separability(arms: dict[str, Any], bs: float | None, n_obs: int,
                  label: str, *, alt: bool = False) -> dict[str, Any]:
    """Which arms are separable from a benchmark, and in which direction.

    Separability has a direction, and collapsing it loses the only reading a PM
    cares about. Seen on the 2026-09-04 data: `mom_21`'s interval clears the
    benchmark's Sharpe — from *below*. A single `can_rank` flag went True on
    that, and the panel would have printed permission to rank on the strength of
    one arm being provably worse. Above and below are counted separately, and
    the headline is keyed to `beats_benchmark`, because "nothing here is shown
    to beat the index" is the sentence that survives being read in a hurry.

    One function rather than two because a second benchmark whose verdict is
    computed by a copy of this loop is a verdict that will disagree with the
    first one the day somebody edits a comparison in only one of them.
    """
    overlap, above, below, measurable = [], [], [], []
    for n, t in arms.items():
        ci = (t["performance"] or {}).get("sharpe_ci95")
        if not ci or bs is None:
            continue
        measurable.append(n)
        if ci[0] > bs:
            above.append(n)
        elif ci[1] < bs:
            below.append(n)
        else:
            overlap.append(n)
    who = f"基准（{label}）" if alt else "基准"
    if not measurable:
        note = ("没有任何组合算得出夏普置信区间（样本过短或波动为 0），"
                "可分性无从判断——这不等于可以排名。")
    else:
        note = (f"{len(overlap)}/{len(measurable)} 条可测组合的夏普 95% 置信区间"
                f"覆盖了{who}的夏普（{bs:+.2f}）——覆盖就意味着这段样本分不开它和{who}。"
                f"被分开的有 {len(above)} 条在{who}之上、{len(below)} 条在{who}之下。")
        if not above:
            note += f"没有任何一条被证明跑赢{who}。"
        if len(measurable) < len(arms):
            note += (f" 另有 {len(arms) - len(measurable)} 条算不出区间，"
                     f"未计入判断。")
    return {
        "n_arms": len(arms),
        "n_measurable": len(measurable),
        "n_days": n_obs,
        "benchmark": label,
        "benchmark_sharpe": bs,
        "arms_whose_ci_contains_benchmark": sorted(overlap),
        "arms_above_benchmark": sorted(above),
        "arms_below_benchmark": sorted(below),
        "beats_benchmark": bool(above),
        # Kept for callers that already read it, but redefined to mean what the
        # panel actually needs: some arm is separable *upward*. A flag that goes
        # True because one arm is provably worse is a flag pointing the wrong way.
        "can_rank": bool(above),
        "note": note,
    }


def _dw(s: str) -> int:
    """Display width in terminal columns: CJK glyphs occupy two.

    Python pads by code point, so a header of Chinese labels and a body of ASCII
    numbers drift apart by one column per character and the table stops being
    readable at exactly the width where it matters. Every pad below goes through
    this.
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, w: int, right: bool = True) -> str:
    gap = max(0, w - _dw(s))
    return (" " * gap + s) if right else (s + " " * gap)


def _f(v: float | None, w: int = 8, mult: float = 1.0, dp: int = 2,
       sign: bool = True) -> str:
    if v is None:
        return _pad("—", w)
    return _pad(f"{v*mult:{'+' if sign else ''}.{dp}f}", w)


def print_comparison(rep: dict[str, Any]) -> None:
    """Console tear sheet. Uncertainty is printed before the numbers, not after.

    Ordering is the whole design. A table that leads with a ranked return column
    and puts the confidence interval in a footnote will be read as a ranking no
    matter what the footnote says, because the reader has already formed the
    conclusion by the time they reach it. So the separability verdict is the
    first thing on the screen and the Sharpe column is never printed without its
    interval in the adjacent cell.
    """
    arms = rep.get("arms") or {}
    if not arms:
        print("没有可用的净值曲线，业绩表为空。")
        return
    sep = rep.get("separability") or {}
    bench = rep.get("benchmark", "基准")
    bp = rep.get("benchmark_performance") or {}
    any_perf = next(iter(arms.values()))["performance"]
    print("\n" + "=" * 100)
    alt_sep = rep.get("separability_vs_alt") or {}
    alt_bench = rep.get("alt_benchmark") or ""
    print(f"组合业绩表 · {any_perf.get('from_d')} → {any_perf.get('to_d')} · "
          f"{sep.get('n_days', '?')} 个交易日 · 基准 {bench}"
          + (f" / {alt_bench}（货架等权）" if alt_bench else "")
          + f" · 无风险 {rep.get('rf_annual', 0)*100:.2f}%")
    print("=" * 100)
    if not sep.get("beats_benchmark", sep.get("can_rank", True)):
        print(f"⚠ 本表不能用来排名——没有任何一条被证明跑赢基准。{sep.get('note','')}")
    else:
        print(f"  {sep.get('note','')}")
    # The second verdict is printed, not merely stored. SPY answers "should the
    # client have bought stocks instead"; the shelf answers "did picking beat not
    # picking", and only the second one is a claim these arms are making. A
    # number computed into a payload nobody prints is a number nobody has.
    if alt_sep:
        mark = ("⚠ " if not alt_sep.get("beats_benchmark") else "  ")
        print(f"{mark}对照自己的货架（{alt_bench}，等权）：{alt_sep.get('note','')}")
    if any_perf.get("ann_return_blocked"):
        print(f"⚠ {any_perf['ann_return_blocked']}")

    print(f"\n【收益与风险】")
    print("  " + _pad("组合", 26, False) + _pad("区间收益%", 10)
          + _pad("年化波动%", 10) + _pad("最大回撤%", 10)
          + _pad("水下占比%", 10) + _pad("最差单日%", 10) + _pad("胜日率%", 9))
    rows = sorted(arms.items(),
                  key=lambda kv: -(kv[1]["performance"].get("cum_return") or -9))
    def _bench_row(label: str, b: dict[str, Any]) -> None:
        print("  " + _pad(label, 26, False)
              + _f(b.get('cum_return'),10,100) + _f(b.get('ann_vol'),10,100,sign=False)
              + _f(b.get('max_drawdown'),10,100)
              + _f(b.get('frac_underwater'),10,100,dp=0,sign=False)
              + _f(b.get('worst_day'),10,100)
              + _f(b.get('pct_positive_days'),9,100,dp=0,sign=False))

    if bp:
        _bench_row(f"{bench}（基准）", bp)
    if rep.get("alt_benchmark_performance"):
        _bench_row(f"{alt_bench}（货架等权）", rep["alt_benchmark_performance"])
    for n, t in rows:
        p = t["performance"]
        print("  " + _pad(n, 26, False) + _f(p.get('cum_return'),10,100)
              + _f(p.get('ann_vol'),10,100,sign=False)
              + _f(p.get('max_drawdown'),10,100)
              + _f(p.get('frac_underwater'),10,100,dp=0,sign=False)
              + _f(p.get('worst_day'),10,100)
              + _f(p.get('pct_positive_days'),9,100,dp=0,sign=False))

    print(f"\n【风险调整后】夏普永远和它的区间一起出现——"
          f"区间盖住 0 就说明这段样本连「有没有正收益」都没分开")
    print("  " + _pad("组合", 26, False) + _pad("夏普", 8)
          + "  " + _pad("夏普 95% 区间", 20, False) + _pad("索提诺", 9)
          + _pad("MAR", 8) + _pad("PSR", 7))
    if bp:
        ci = bp.get("sharpe_ci95")
        print("  " + _pad(f"{bench}（基准）", 26, False) + _f(bp.get('sharpe'),8)
              + "  " + _pad(("[" + _f(ci[0],0) + ", " + _f(ci[1],0) + "]") if ci else "—", 20, False)
              + _f(bp.get('sortino'),9) + _f(bp.get('mar'),8)
              + _f(bp.get('psr_vs_zero'),7,dp=2,sign=False))
    for n, t in sorted(arms.items(),
                       key=lambda kv: -(kv[1]["performance"].get("sharpe") or -9)):
        p = t["performance"]
        ci = p.get("sharpe_ci95")
        print("  " + _pad(n, 26, False) + _f(p.get('sharpe'),8)
              + "  " + _pad(("[" + _f(ci[0],0) + ", " + _f(ci[1],0) + "]") if ci else "—", 20, False)
              + _f(p.get('sortino'),9) + _f(p.get('mar'),8)
              + _f(p.get('psr_vs_zero'),7,dp=2,sign=False))

    if any((t.get("relative") for t in arms.values())):
        print(f"\n【相对基准】beta 是回归出来的，不是假设的 1")
        print("  " + _pad("组合", 26, False) + _pad("beta", 7)
              + _pad("alpha年化%", 11) + _pad("alpha_t", 9) + _pad("R²", 7)
              + _pad("跟踪误差%", 10) + _pad("IR", 7) + _pad("评价比率", 10)
              + _pad("上行捕获%", 10) + _pad("下行捕获%", 10) + _pad("胜天率%", 9))
        for n, t in sorted(arms.items(),
                           key=lambda kv: -((kv[1].get("relative") or {}).get(
                               "information_ratio") or -9)):
            r = t.get("relative") or {}
            star = "*" if r.get("alpha_significant") else " "
            print("  " + _pad(n, 26, False) + _f(r.get('beta'),7,sign=False)
                  + _f(r.get('alpha_ann'),11,100) + _f(r.get('alpha_t'),8) + star
                  + _f(r.get('r2'),7,dp=3,sign=False)
                  + _f(r.get('tracking_error'),10,100,sign=False)
                  + _f(r.get('information_ratio'),7)
                  + _f(r.get('appraisal_ratio'),10)
                  + _f(r.get('up_capture'),10,100,dp=0,sign=False)
                  + _f(r.get('down_capture'),10,100,dp=0)
                  + _f(r.get('batting_average'),9,100,dp=0,sign=False))
        r0 = next((t.get("relative") for t in arms.values() if t.get("relative")), {})
        need = r0.get("appraisal_needed_for_significance")
        print(f"    * = alpha 的 t 值过 ±1.96。上行/下行捕获分别只用基准涨的 "
              f"{r0.get('n_up_days','?')} 天和跌的 {r0.get('n_down_days','?')} 天算。")
        print(f"    IR 拿「相对基准的偏离」做分母，含「没拿满市场」那部分——"
              f"低 beta 的组合在涨势里 IR 必然难看，那是敞口差不是能力差。"
              f"评价比率拿剔掉 beta 后的特质波动做分母，量的才是选取策略自己挣的。"
              + (f"但它和 alpha 的 t 值是同一个量：本窗口要到 {need:.1f} 才算显著。"
                 if need else ""))

    fd = rep.get("family_deflation")
    if fd and fd.get("message"):
        print(f"\n【多重检验：试了 {fd.get('n_trials','?')} 条，只报最好那条会高估多少】")
        print(f"  {fd['message']}")
    fdr = rep.get("fdr")
    if fdr and fdr.get("m"):
        print(f"  {fdr.get('note','')}")
        print(f"    BH 判为发现：{fdr.get('rejected') or '无'}；"
              f"Bonferroni 判为发现：{fdr.get('bonferroni_rejected') or '无'}")

    wf = rep.get("walk_forward") or {}
    if wf:
        print(f"\n【挑组合这个动作本身有用吗（前推检验）】"
              f"每期只用此前的成绩挑领先者，再看它当期结果")
        for key in ("all_arms", "no_posthoc"):
            blk = wf.get(key)
            if not blk:
                continue
            tag = "含事后设计的组合" if key == "all_arms" else "剔除事后设计的组合"
            print(f"  · {tag}：跟随领先者 {_pct(blk.get('follow_leader_mean'))}/期"
                  f" | 全量基准 {_pct(blk.get('control_mean'))}"
                  f" | 随机挑一条 {_pct(blk.get('all_arms_mean'))}"
                  f" | 事后最优 {_pct(blk.get('hindsight_best_mean'))}"
                  f" | n={blk.get('n_decisions')}")
            ranks = [x.get("rank_of_pick") for x in (blk.get("picks") or [])]
            n_live = ((blk.get("picks") or [{}])[0] or {}).get("n_arms_live")
            print(f"      被挑中那条的当期实际排名 {ranks}（共 {n_live} 条）"
                  + ("" if blk.get("usable") else "  ← 决策次数不足，还不该读"))
        if wf.get("note"):
            print(f"  {wf['note']}")

    pb = rep.get("pbo") or {}
    if pb.get("splits"):
        rng = pb.get("pbo_range")
        print(f"\n【回测过拟合概率 PBO】样本内最优的那条，样本外落进后半段的比例")
        for k in sorted(pb["splits"], key=int):
            b = pb["splits"][k]
            if b.get("pbo") is None:
                continue
            nul = b.get("null") or {}
            ci = nul.get("ci95") or [None, None]
            print(f"  切 {k:>2} 块（{b.get('n_combinations')} 种组合）"
                  f"  实测 {b['pbo']:.2f}"
                  f"   同形状噪声 {nul.get('median','—')}"
                  f" [{ci[0]}, {ci[1]}]"
                  f"   p={b.get('p_value')}"
                  f"   {'优于噪声' if b.get('better_than_noise') else '与噪声分不开'}")
        if rng:
            print(f"  实测区间 {rng[0]:.2f}~{rng[1]:.2f}。"
                  f"门槛不是 0.5，而是上面那列「同形状噪声」——样本内外取自同一段"
                  f"有限数据，赢家会机械性回归，偏移多少取决于组合数/观测数/块数。"
                  f"{'三个块数方向一致。' if pb.get('verdict_stable_across_splits') else '三个块数不一致，别引用点估计。'}")

    cap = rep.get("capacity") or {}
    if cap.get("arms"):
        print(f"\n【容量：自己的单子会不会把价格推走】"
              f"资本 {cap['capital_usd']/1e6:.0f}M ÷ {cap['slots']} 档，"
              f"参与度 = 单笔金额 ÷ 入场前 20 日均成交额")
        print("  " + _pad("组合", 26, False) + _pad("每期开仓", 10)
              + _pad("单笔(千$)", 11) + _pad("参与度中位%", 12)
              + _pad("参与度p90%", 12) + _pad("容量(百万$)", 13))
        for n, v in sorted(cap["arms"].items(),
                           key=lambda kv: (kv[1]["capacity_usd"] or 9e18)):
            print("  " + _pad(n, 26, False)
                  + _pad(f"{v['median_positions_per_period']:.0f}", 10)
                  + _pad(f"{v['position_usd']/1000:.0f}", 11)
                  + _pad(f"{v['participation_median']*100:.3f}", 12)
                  + _pad(f"{v['participation_p90']*100:.3f}", 12)
                  + _pad(("—" if v["capacity_usd"] is None
                          else f"{v['capacity_usd']/1e6:.0f}"), 13))
        if cap.get("tightest_capacity_usd"):
            print(f"    最紧的容量 ${cap['tightest_capacity_usd']/1e6:.0f}M——"
                  f"容量由最薄的那个标的定，不由典型标的定。"
                  f"仓位越集中，容量越小：这和「集中持仓更容易出成绩」是同一件事的两面。")

    cr = rep.get("cost_reality") or {}
    if cr:
        print(f"\n【交易成本现实】")
        print("  " + _pad("组合", 26, False) + _pad("每期开仓", 10)
              + _pad("年周转", 9) + _pad("已扣成本%", 10)
              + _pad("打平所需往返成本%", 19))
        for n, c in sorted(cr.items(),
                           key=lambda kv: -(kv[1].get(
                               "breakeven_round_trip_pct") or -99)):
            if not c.get("n_positions"):
                continue
            print("  " + _pad(n, 26, False)
                  + _pad(f"{c['positions_per_period']:.1f}", 10)
                  + _pad(f"{c['round_trips_per_year']:.1f}", 9)
                  + _pad(f"{c['applied_round_trip_pct']:.2f}", 10)
                  + _pad(f"{c['breakeven_round_trip_pct']:.2f}", 19))
        print(f"    「打平所需往返成本」低于「已扣成本」的组合，"
              f"扣完交易费之后没有超额可言。")
    print()


# ---------------------------------------------------------------------------
# Does the *selection procedure* work? (walk-forward)

@dataclass
class WalkForward:
    label: str = "all_arms"
    excluded: list[str] = field(default_factory=list)
    n_decisions: int = 0
    picks: list[dict[str, Any]] = field(default_factory=list)
    follow_leader_mean: float | None = None
    control_mean: float | None = None
    all_arms_mean: float | None = None
    hindsight_best_mean: float | None = None
    edge_vs_control: float | None = None
    edge_vs_all_arms: float | None = None
    hit_rate_vs_control: float | None = None
    usable: bool = False
    message: str = ""


def walk_forward_selection(per_period: dict[str, dict[str, float | None]], *,
                           control: str | None = None,
                           exclude: Sequence[str] = (),
                           label: str = "all_arms",
                           min_decisions: int = 4) -> WalkForward:
    """Pick the leader on everything seen so far; take what it did next.

    Every other number in this file grades an arm. This one grades **the act of
    choosing between arms**, and it is the only test that can justify running
    eleven of them. Nine arms exist so that one can be picked; if picking the
    leader does no better than the control, the apparatus is a cost with no
    product, and no per-arm statistic will ever reveal that — each arm can look
    fine while the procedure that selects among them adds nothing.

    Strictly forward: the leader for period *t* is decided on periods 1..t−1
    only, so every decision is out-of-sample by construction. This is the same
    discipline `live_vs_backfill` applies to one exploratory arm, generalised to
    the selection itself.

    Three reference points, because "did it beat the control" alone is not
    interpretable:

    * `control_mean` — never choose; hold the full pool.
    * `all_arms_mean` — choose at random among the arms, in expectation.
    * `hindsight_best_mean` — the ceiling, chosen with the answers. The gap
      between follow-the-leader and this is what the procedure leaves on the
      table; the gap between it and `all_arms_mean` is what it earns.

    Refuses to be read below `min_decisions`, for the same reason
    `live_vs_backfill` does: below one full rotation of the tranche cycle every
    decision is still riding positions that are open somewhere.

    **`exclude` is the part that matters, and the first run without it was
    wrong in a way that looked like a result.** Walk-forward is honest about
    *when an arm was chosen* and says nothing about *when the arm was created*.
    On the 2026-09-04 data, four of five decisions picked `ev_rank` — an arm
    designed on 2026-09-05 after looking at these six periods. The procedure
    was replaying a choice among candidates one of which could not have existed
    at the time, which is look-ahead one level up from the prices: the leak is
    in the arm roster, not in any arm's inputs. Callers pass every
    post-hoc-designed arm here and read the two runs side by side; the gap
    between them is the size of that contamination.
    """
    w = WalkForward(label=label, excluded=sorted(exclude))
    drop = set(exclude)
    per_period = {a: r for a, r in per_period.items() if a not in drop}
    periods = sorted({p for rows in per_period.values() for p in rows})
    arms = sorted(per_period)
    if len(periods) < 2 or not arms:
        w.message = "期数或组合数不足，无法做前推选择检验。"
        return w

    lead_rets: list[float] = []
    ctl_rets: list[float] = []
    all_rets: list[float] = []
    best_rets: list[float] = []
    for i in range(1, len(periods)):
        now = periods[i]
        seen = periods[:i]
        # Leader on the record through the prior period only.
        score: dict[str, float] = {}
        for a in arms:
            vals = [per_period[a][p] for p in seen
                    if per_period[a].get(p) is not None]
            if vals:
                score[a] = _mean(vals)          # type: ignore[arg-type]
        live = {a: per_period[a].get(now) for a in arms}
        live = {a: v for a, v in live.items() if v is not None}
        if not score or not live:
            continue
        pick = max(score, key=lambda a: score[a])
        if pick not in live:
            continue
        lead_rets.append(live[pick])            # type: ignore[arg-type]
        all_rets.append(_mean(list(live.values())))   # type: ignore[arg-type]
        best_rets.append(max(live.values()))    # type: ignore[arg-type]
        c = live.get(control) if control else None
        if c is not None:
            ctl_rets.append(c)
        w.picks.append({
            "period": now, "picked": pick,
            "picked_on_record_through": seen[-1],
            "realised": round(live[pick], 6),          # type: ignore[arg-type]
            "control": None if c is None else round(c, 6),
            "best_available": round(max(live.values()), 6),
            "rank_of_pick": 1 + sorted(live.values(), reverse=True).index(
                live[pick]),
            "n_arms_live": len(live),
        })

    w.n_decisions = len(lead_rets)
    if not w.n_decisions:
        w.message = "没有一期能同时给出「此前的领先者」和「它当期的结果」。"
        return w
    w.follow_leader_mean = round(_mean(lead_rets), 6)
    w.all_arms_mean = round(_mean(all_rets), 6)
    w.hindsight_best_mean = round(_mean(best_rets), 6)
    if ctl_rets:
        w.control_mean = round(_mean(ctl_rets), 6)
        w.edge_vs_control = round(w.follow_leader_mean - w.control_mean, 6)
        w.hit_rate_vs_control = round(
            sum(1 for a, b in zip(lead_rets, ctl_rets) if a > b)
            / len(ctl_rets), 3)
    w.edge_vs_all_arms = round(w.follow_leader_mean - w.all_arms_mean, 6)
    w.usable = w.n_decisions >= min_decisions

    ranks = [p["rank_of_pick"] for p in w.picks]
    w.message = (
        (f"[{label}] " if label != "all_arms" else "")
        + (f"（已剔除事后设计的组合：{'、'.join(w.excluded)}）"
           if w.excluded else "")
        + f"前推选择：每期只用此前各期的成绩挑出领先者，再看它当期的结果，共 "
        f"{w.n_decisions} 次决策。跟随领先者 {_pct(w.follow_leader_mean)}/期"
        + (f"，全量基准 {_pct(w.control_mean)}（差 {_pct(w.edge_vs_control)}）"
           if w.control_mean is not None else "")
        + f"，随机挑一条 {_pct(w.all_arms_mean)}（差 {_pct(w.edge_vs_all_arms)}），"
        f"事后最优 {_pct(w.hindsight_best_mean)}。"
        f"被挑中的那条当期实际排名依次为 {ranks}（共 {w.picks[0]['n_arms_live']} 条）。"
        + ("" if w.usable else
           f" 不足 {min_decisions} 次决策，这一段还不该被读——"
           f"四周才是一轮完整的分批周期。"))
    return w


# ---------------------------------------------------------------------------
# Probability of backtest overfitting (CSCV)

def _combinations(n: int, k: int) -> list[tuple[int, ...]]:
    out: list[tuple[int, ...]] = []

    def rec(start: int, cur: tuple[int, ...]) -> None:
        if len(cur) == k:
            out.append(cur)
            return
        for i in range(start, n):
            rec(i + 1, cur + (i,))
    rec(0, ())
    return out


@dataclass
class PBO:
    n_splits: int = 0
    n_combinations: int = 0
    n_arms: int = 0
    n_obs: int = 0
    pbo: float | None = None
    median_oos_rank_of_is_winner: float | None = None
    oos_beats_median_frac: float | None = None
    usable: bool = False
    message: str = ""


def pbo_cscv(returns: dict[str, Sequence[float]], *, n_splits: int = 8,
             min_obs: int = 24) -> PBO:
    """Bailey et al.'s combinatorially symmetric cross-validation.

    The DSR asks whether the winner's Sharpe survives the number of trials. This
    asks a different and blunter question: **when you pick the in-sample winner,
    does it tend to land below the median out of sample?** A procedure for which
    it does is overfitting by construction, whatever any individual arm's
    statistics look like.

    The construction: cut the observations into `n_splits` contiguous blocks,
    take every way of choosing half of them as in-sample with the complement as
    out-of-sample, pick the best Sharpe in-sample, and record where that arm
    ranks out-of-sample. PBO is the share of splits where it lands in the bottom
    half. 0.5 is a coin flip — no information survives selection; near 0 means
    the in-sample winner keeps winning.

    Two honest caveats, both live here. The blocks are contiguous rather than
    random, so a regime that fills one half biases the answer; and these arms
    hold overlapping tranches of the same pool, so their daily returns are
    strongly correlated and the ranks are less independent than the arithmetic
    assumes. Both make PBO *less* discriminating, not more, which is the safe
    direction for a check whose job is to refuse.
    """
    p = PBO(n_splits=n_splits, n_arms=len(returns))
    names = sorted(returns)
    if len(names) < 2:
        p.message = "少于 2 条组合，PBO 无从谈起。"
        return p
    n = min(len(returns[a]) for a in names)
    p.n_obs = n
    if n < min_obs or n_splits < 2 or n_splits % 2:
        p.message = (f"观测 {n} 个、切 {n_splits} 块——PBO 需要至少 {min_obs} 个观测"
                     f"且块数为偶数。")
        return p

    edges = [round(i * n / n_splits) for i in range(n_splits + 1)]
    blocks = [list(range(edges[i], edges[i + 1])) for i in range(n_splits)]
    combos = _combinations(n_splits, n_splits // 2)
    p.n_combinations = len(combos)

    def sharpe(xs: list[float]) -> float | None:
        if len(xs) < 2:
            return None
        m = _mean(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
        return None if sd <= 0 else m / sd

    ranks: list[float] = []
    below = 0
    for c in combos:
        is_idx = [i for b in c for i in blocks[b]]
        oos_idx = [i for b in range(n_splits) if b not in c for i in blocks[b]]
        is_s, oos_s = {}, {}
        for a in names:
            r = returns[a]
            si = sharpe([r[i] for i in is_idx])
            so = sharpe([r[i] for i in oos_idx])
            if si is not None and so is not None:
                is_s[a], oos_s[a] = si, so
        if len(is_s) < 2:
            continue
        win = max(is_s, key=lambda a: is_s[a])
        order = sorted(oos_s, key=lambda a: oos_s[a])       # worst first
        # Relative rank in [0,1]; 1 = best out of sample.
        rr = (order.index(win) + 1) / len(order)
        ranks.append(rr)
        if rr <= 0.5:
            below += 1
    if not ranks:
        p.message = "每种切分下都算不出足够的夏普，PBO 无结果。"
        return p
    p.pbo = round(below / len(ranks), 4)
    p.median_oos_rank_of_is_winner = round(st.median(ranks), 4)
    p.oos_beats_median_frac = round(1 - p.pbo, 4)
    p.usable = True
    p.message = (
        f"把 {p.n_obs} 个观测切成 {n_splits} 块、取遍 {p.n_combinations} 种"
        f"「一半样本内一半样本外」的组合：样本内最优的那条，样本外落进后半段的比例是 "
        f"PBO={p.pbo:.2f}（越低越好；但门槛不是 0.5——样本内外取自同一段有限数据，"
        f"赢家会机械性回归，这个偏移的大小要按本次的组合数/观测数/块数实算，"
        f"参照值是 pbo_null 给出的零分布中位数）。"
        f"样本外相对排名中位数 {p.median_oos_rank_of_is_winner:.2f}。"
        f"注意：这些组合持有的是同一个候选池的重叠批次，日收益高度相关，"
        f"排名的独立性比算式假设的低——这让 PBO 更钝而不是更利。")
    return p


def pbo_sweep(returns: dict[str, Sequence[float]],
              splits: Sequence[int] = (4, 6, 8),
              n_perm: int = 200) -> dict[str, Any]:
    """PBO at several block counts, because one is a number the author picked.

    Same objection `robustness_drop_top` raises against a single cut depth: a
    verdict that only holds at the granularity its author chose is a verdict
    about the author. On 27 observations the answer moves between 0.55 and 0.67
    across 4/6/8 blocks — all above the 0.5 coin-flip line, none of them stable,
    and reporting the spread is the finding.
    """
    # Each split carries its own permutation null. Reporting PBO without one is
    # what produced the wrong sign here the first time: 0.5 is not the null.
    runs: dict[str, Any] = {}
    for sp in splits:
        blk = pbo_null(returns, n_splits=sp, n_perm=n_perm)
        row = dict(blk["observed"])
        row["null"] = blk.get("null")
        row["p_value"] = blk.get("p_value")
        row["better_than_noise"] = blk.get("better_than_noise")
        runs[str(sp)] = _clean(row)
    vals = [r["pbo"] for r in runs.values() if r.get("pbo") is not None]
    # Stability is about agreeing with each other, not about a 0.5 line that
    # turned out not to be the null. Median of the three is the reference.
    mid = st.median(vals) if vals else 0.0
    agree = bool(vals) and (max(vals) - min(vals)) <= 0.15
    ps = [r["p_value"] for r in runs.values() if r.get("p_value") is not None]
    return {
        "splits": runs,
        "pbo_range": ([min(vals), max(vals)] if vals else None),
        "p_range": ([min(ps), max(ps)] if ps else None),
        "any_split_beats_noise": any(r.get("better_than_noise")
                                     for r in runs.values()),
        "all_splits_beat_noise": bool(runs) and all(
            r.get("better_than_noise") for r in runs.values()),
        "verdict_stable_across_splits": agree,
        "note": (
            "PBO 在 4/6/8 块下各算一次。只报一个块数，等于报一个「作者挑的粒度」下的结论。"
            + ("三个块数指向同一侧，结论方向稳定。" if agree else
               "三个块数没有指向同一侧，说明这个样本撑不起 PBO 的结论，不要引用点估计。")
            + "判断好坏和每个块数各自的置换零分布比，不和 0.5 比："
              "样本内外取自同一段有限数据，赢家会机械性回归，这个偏移有多大"
              "取决于组合数、观测数和块数，只能逐个算出来，不能假定是 0.5。"
            if vals else "样本不足，PBO 无结果。"),
    }


def pbo_null(returns: dict[str, Sequence[float]], *, n_splits: int = 8,
             n_perm: int = 200, seed: int = 20260905) -> dict[str, Any]:
    """The PBO a skill-free version of this same data would produce.

    Written because reading PBO against 0.5 is wrong, and the first version of
    this module said 0.5 in three places. The coin-flip intuition assumes the
    in-sample and out-of-sample ranks are independent; they come from one finite
    sample, so an arm whose in-sample blocks are high by chance has mechanically
    lower out-of-sample blocks. The winner regresses, and PBO on pure noise
    lands far above 0.5 — measured here at 0.77 for eight independent gaussian
    arms. Against 0.5 this book's 0.55-0.67 reads as "worse than a coin flip",
    which has the sign of the conclusion backwards.

    **The null is exchangeability of arms, and getting that right took two
    attempts.** The first permuted the time index with one shared permutation.
    That preserves each arm's own mean, so an arm with a durable edge keeps it
    under permutation, the null distribution contains the very signal it is
    supposed to exclude, and a genuinely persistent arm comes back
    indistinguishable from noise. A null that cannot reject anything is not
    conservative, it is broken.

    What is permuted instead is the **arm label within each observation**. Every
    instant keeps its exact set of returns, so the cross-sectional spread and
    the correlation these overlapping tranches have at each date survive
    untouched — a null that broke those would be a null about a different
    portfolio. What does not survive is arm identity, which is precisely H0:
    no arm is durably better than another.

    `p_value` is the share of permutations whose PBO is at least as low as the
    observed one. Small means the in-sample winner keeps winning more than
    exchangeable arms would.
    """
    import random as _r
    rng = _r.Random(seed)
    names = sorted(returns)
    n = min(len(returns[a]) for a in names) if names else 0
    obs = pbo_cscv(returns, n_splits=n_splits)
    if not obs.usable:
        return {"observed": _clean(asdict(obs)), "null": None,
                "note": "观测样本本身算不出 PBO，零分布无从比较。"}

    cols = [[returns[a][t] for a in names] for t in range(n)]
    draws: list[float] = []
    for _ in range(n_perm):
        shuffled = []
        for c in cols:
            c2 = list(c)
            rng.shuffle(c2)
            shuffled.append(c2)
        perm = {a: [shuffled[t][i] for t in range(n)]
                for i, a in enumerate(names)}
        r = pbo_cscv(perm, n_splits=n_splits)
        if r.pbo is not None:
            draws.append(r.pbo)
    if not draws:
        return {"observed": _clean(asdict(obs)), "null": None,
                "note": "置换样本都算不出 PBO。"}

    draws.sort()
    lo = draws[max(0, int(0.025 * len(draws)) - 1)]
    hi = draws[min(len(draws) - 1, int(0.975 * len(draws)))]
    med = st.median(draws)
    p = sum(1 for d in draws if d <= obs.pbo) / len(draws)
    return {
        "observed": _clean(asdict(obs)),
        "null": {
            "n_perm": len(draws),
            "mean": round(_mean(draws), 4),
            "median": round(med, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "construction": "每个时点内打乱组合标签（各组合可交换）",
        },
        "p_value": round(p, 4),
        "better_than_noise": obs.pbo < med,
        "note": (
            f"实测 PBO={obs.pbo:.2f}。零假设是「各条组合可交换」——在每个交易日内"
            f"打乱组合标签、保留当天的横截面分布与相关结构，只抹掉组合身份，"
            f"{len(draws)} 次给出 PBO 中位数 {med:.2f}，95% 区间 [{lo:.2f}, {hi:.2f}]。"
            f"该比的是这个实测出来的中位数，而不是默认的 0.5：样本内外取自同一段"
            f"有限数据，赢家会机械性回归，这个偏移有多大取决于组合数、观测数和"
            f"块数，只能算不能猜（本次这个形状下是 {med:.2f}）。"
            f"p={p:.3f}（置换中不高于实测的比例）。"
            + ("实测低于零分布中位数，样本内的赢家在样本外确实比可交换的情形更能守住。"
               if obs.pbo < med else
               "实测不低于零分布中位数，挑出来的赢家守不住，选择过程没有留下可辨认的东西。")),
    }


# ---------------------------------------------------------------------------
# Capacity: the other half of the seventh sin

def capacity(rows: Sequence[dict[str, Any]], *, capital: float, slots: int,
             participation_cap: float = 0.10) -> dict[str, Any]:
    """How much money this could run before its own orders move the price.

    `turnover_and_cost` answers "does the edge survive the fee". This answers
    the question a fee-blind reader still has to ask: **could you put real money
    in it at all.** Deutsche Bank's seventh sin is a Sharpe presented without
    turnover, borrow and impact beside it; the fee half was already covered and
    this is the impact half.

    The estimate is deliberately crude and stated as such. Position size is
    `capital / slots / positions-per-period` — the design's own sizing — and
    participation is that divided by the instrument's average daily dollar
    volume before entry. `capacity_usd` is the capital at which the *90th
    percentile* position would hit `participation_cap` of ADV; the 90th rather
    than the median, because capacity is set by the thinnest name a strategy
    reaches for, not by the typical one.

    What it deliberately does not do is model impact. There is no square-root
    law here, no spread, no borrow. Participation of ADV is the input every
    impact model starts from, and reporting it honestly is worth more than
    reporting a dollar cost derived from coefficients nobody here has fitted.

    Rows need `arm` and `adv_usd`; anything missing ADV is counted and excluded
    rather than assumed liquid, which would be the zero-fill again.
    """
    # Same dedup as `turnover_and_cost`, for the same reason: one instrument in
    # one period is one order however many generators proposed it, and position
    # size — the whole input to participation — divides by that count.
    by_arm: dict[str, list[dict[str, Any]]] = {}
    missing = 0
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        k = (str(r.get("arm")), str(r.get("period")),
             str(r.get("instrument_id")))
        if k in seen:
            continue
        seen.add(k)
        if r.get("adv_usd") in (None, 0):
            missing += 1
            continue
        by_arm.setdefault(str(r.get("arm")), []).append(r)
    if not by_arm:
        return {"arms": {}, "missing_adv": missing,
                "note": "没有一条持仓拿得到日均成交额，容量无从估计。"}

    def pctile(xs: list[float], q: float) -> float:
        s = sorted(xs)
        i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return s[i]

    out: dict[str, Any] = {}
    for arm, rs in sorted(by_arm.items()):
        per_period: dict[str, int] = {}
        for r in rs:
            per_period[str(r.get("period"))] = per_period.get(
                str(r.get("period")), 0) + 1
        n_pos = st.median(list(per_period.values())) if per_period else 1
        pos_usd = capital / max(1, slots) / max(1, n_pos)
        part = [pos_usd / float(r["adv_usd"]) for r in rs]
        p90 = pctile(part, 0.90)
        out[arm] = {
            "n_positions_scored": len(rs),
            "median_positions_per_period": n_pos,
            "position_usd": round(pos_usd, 0),
            "participation_median": round(st.median(part), 6),
            "participation_p90": round(p90, 6),
            "participation_max": round(max(part), 6),
            "n_over_cap": sum(1 for x in part if x > participation_cap),
            "capacity_usd": (round(capital * participation_cap / p90, 0)
                             if p90 > 0 else None),
        }
    caps = [v["capacity_usd"] for v in out.values()
            if v["capacity_usd"] is not None]
    return {
        "capital_usd": capital,
        "slots": slots,
        "participation_cap": participation_cap,
        "missing_adv": missing,
        "arms": out,
        "tightest_capacity_usd": min(caps) if caps else None,
        "note": (
            f"单笔金额 = 资本 {capital/1e6:.0f}M ÷ {slots} 个档位 ÷ 当期持仓数；"
            f"参与度 = 单笔金额 ÷ 该标的入场前的日均成交额。"
            f"capacity_usd 是让第 90 百分位的那笔达到 {participation_cap*100:.0f}% "
            f"日均成交额时的资本量——取 90 分位而不是中位数，因为容量由最薄的那个标的定，"
            f"不由典型标的定。"
            f"这里只报参与度，不套冲击成本模型：参与度是所有冲击模型的输入，"
            f"如实报它，好过报一个用没人拟合过的系数算出来的美元数。"
            + (f" 有 {missing} 条持仓拿不到成交额，已剔除而不是当成流动性充足。"
               if missing else "")),
    }
