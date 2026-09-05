"""One constant cannot reorder arms that all turn over alike. Liquidity can.

`turnover_and_cost` charges every arm the same basis points, and the report says
plainly that a constant cannot change the ranking when turnover is equal. That
left a hole the panel's own footnote described: the arms are not equally liquid,
because two of them open two positions a period and the rest open dozens, so the
same capital buys a very different ticket. Deutsche Bank's Figure 67/68 is that
hole — conviction beats diversification until the cost of reaching the thin
names is subtracted.

Nothing here is fitted. Three schedules are stated and applied, and the output
is "which conclusions depend on the assumption", which is a different and more
defensible claim than "the cost is X".
"""

from __future__ import annotations

from ideagen import perf


def _rows(arm, n_names, adv, ret, periods=("p1", "p2")):
    return [{"arm": arm, "period": p, "instrument_id": f"{arm}-{p}-{i}",
             "adv_usd": adv, "return_pct": ret}
            for p in periods for i in range(n_names)]


def test_a_flat_fee_cannot_reorder_but_a_liquidity_tier_can():
    """Two arms, same gross return. One holds 2 thin names, the other 40 liquid
    ones. Flat: tied, order unchanged. Tiered: the concentrated one pays for its
    ticket and drops."""
    rows = (_rows("thin", 2, 5_000_000.0, 2.0)
            + _rows("wide", 40, 500_000_000.0, 2.0))
    out = perf.cost_sensitivity(rows, capital=100_000_000.0, slots=4)
    thin = out["arms"]["thin"]["schedules"]
    wide = out["arms"]["wide"]["schedules"]
    assert thin["flat"]["round_trip_bps"] == wide["flat"]["round_trip_bps"]
    assert thin["tiered_mid"]["round_trip_bps"] > wide["tiered_mid"]["round_trip_bps"]
    assert out["rank_under"]["tiered_mid"]["order"][0] == "wide"


def test_the_same_strategy_changes_answer_with_the_size_of_the_book():
    """Liquidity is not a property of a strategy, it is a property of a strategy
    at a size — so the sweep is over capital, and a report that fixed one size
    would be answering a question nobody asked twice."""
    rows = (_rows("thin", 2, 20_000_000.0, 3.0)
            + _rows("wide", 40, 500_000_000.0, 2.9))
    out = perf.cost_sensitivity(rows, capital=1_000_000.0, slots=4,
                                capital_multiples=(1.0, 50.0))
    small = out["capital_sweep"]["1x"]["tiered_heavy"]
    large = out["capital_sweep"]["50x"]["tiered_heavy"]
    assert small["top"] == "thin"                  # $125k per name: nothing moves
    assert large["top"] == "wide"                  # $6.25M into a $20M ADV: it does
    assert large["mean_round_trip_bps"] > small["mean_round_trip_bps"]


def test_a_position_without_volume_is_counted_not_assumed_liquid():
    """Zero-filling ADV would make the thinnest names free, which is the
    flattering direction and the one that never gets checked."""
    rows = _rows("a", 10, 100_000_000.0, 1.0)
    rows += [{"arm": "a", "period": "p3", "instrument_id": "x",
              "adv_usd": None, "return_pct": 1.0}]
    out = perf.cost_sensitivity(rows, capital=10_000_000.0, slots=4)
    assert out["missing_adv"] == 1


def test_one_instrument_in_one_period_is_one_order():
    """Two generators proposing the same name is one position, and position size
    divides by the count — so counting it twice would halve the ticket and make
    the arm look more liquid than it is."""
    rows = _rows("a", 4, 50_000_000.0, 1.0)
    dupes = [dict(r) for r in rows]                # identical keys
    single = perf.cost_sensitivity(rows, capital=40_000_000.0, slots=1)
    doubled = perf.cost_sensitivity(rows + dupes, capital=40_000_000.0, slots=1)
    assert (single["arms"]["a"]["position_usd"]
            == doubled["arms"]["a"]["position_usd"])
