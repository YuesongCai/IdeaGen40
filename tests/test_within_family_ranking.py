"""Take the rule to another universe — four of them, on one shelf.

The paper's remedy for data snooping that this repository had no version of:
apply the same rule, unchanged, to a different universe and see whether it
survives. There is no second market here, but the shelf holds four risk
families, and a score with information should rank inside each of them. A score
that is really sorting risk ranks the whole shelf beautifully in a rising month
— commodities over equities over T-bills — and stops ranking the moment the
comparison is confined to one family.

The first test is the one that matters: it builds data with *zero* ranking
inside every family and a large one across them, and demands the report say so.
That is the exact shape of the finding on the live data (pooled +0.23, within
families +0.08 to +0.10), so if this ever comes back green on a broken
implementation the finding disappears with it.
"""

from __future__ import annotations

from ideagen import backtest


def _family_rows():
    """Two families. Inside each, score and return are uncorrelated by
    construction (returns are a fixed permutation unrelated to the score);
    across families, every commodity beats every bond and scores higher."""
    rows = []
    scores = [1, 2, 3, 4, 5, 6, 7, 8]
    rets = [1, 4, 6, 7, 8, 5, 3, 2]          # Spearman with `scores` is exactly 0
    for i, (s, r) in enumerate(zip(scores, rets)):
        rows.append(("2026-01-07", "债/现金", s, r * 0.01))
        rows.append(("2026-01-07", "商品", 100 + s, 100 + r * 0.01))
    return rows


def test_a_score_that_only_sorts_families_is_reported_as_such():
    out = backtest.rank_within_families(_family_rows(), min_names=8)
    # Pooled it reads as strong ranking power (0.75 here) purely because every
    # commodity outranks and outperforms every bond.
    assert out["overall"]["mean_rho"] > 0.7
    for fam in ("债/现金", "商品"):
        assert out["families"][fam]["mean_rho"] == 0.0       # inside: nothing


def test_a_thin_family_is_skipped_rather_than_reported():
    """A rank correlation over six points is noise with a decimal point, and it
    would be read as a family where the score 'stops working'."""
    rows = _family_rows() + [("2026-01-07", "另类", 1, 0.5),
                             ("2026-01-07", "另类", 2, 0.1)]
    out = backtest.rank_within_families(rows, min_names=8)
    assert "另类" not in out["families"]


def test_periods_are_never_pooled():
    """Two periods, each internally unranked, but the second period's returns
    are all higher and its scores all higher. Pooling would report a strong
    correlation that is entirely the calendar."""
    rows = []
    for i in range(10):
        rows.append(("2026-01-07", "股票", i, (10 - i) * 0.01))
        rows.append(("2026-02-04", "股票", 100 + i, 1.0 + (10 - i) * 0.01))
    out = backtest.rank_within_families(rows, min_names=8)
    assert out["families"]["股票"]["periods"] == 2
    assert out["families"]["股票"]["mean_rho"] < 0      # each period is inverted
    assert out["estimator"] == "spearman_within_period"


def test_every_listed_instrument_lands_in_a_family():
    """And the overrides must keep pointing at instruments that exist, or a
    delisted ticker leaves a rule nobody can find the subject of."""
    from ideagen import universe as uni
    keys = {i.key.upper() for i in uni.LISTED}
    for i in uni.LISTED:
        fam = backtest.asset_family(i.key, i.tags)
        assert fam in ("股票", "债/现金", "商品", "另类"), (i.key, fam)
    missing = set(backtest._FAMILY_OVERRIDE) - keys
    assert not missing, f"这些手工归类的标的已经不在货架上了：{sorted(missing)}"


def test_the_overrides_actually_change_something():
    """A bank ETF carries rate tags because rates drive it, and it is still an
    equity. If the tag rule ever starts agreeing, the override is dead weight
    and should be deleted rather than left to look like it is doing work."""
    assert backtest.asset_family("KRE", ("credit", "rates-sensitive")) == "股票"
    assert backtest.asset_family("XYZ", ("credit",)) == "债/现金"
