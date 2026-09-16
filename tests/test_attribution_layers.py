"""三层归因与跟踪误差（WS-E）。

每条测试钉住一种会悄悄说谎的方式：
* 瀑布图不闭合——SPY + 主题层 + 选标的层 ≠ 持仓收益；
* 对不上指示标的的仓位被当成 0 收益混进加权；
* 组合构建层拿一个组合和**另一期**的全量基准比；
* 样本不足的行没有标；
* 跟踪误差在共同交易日太少时仍给数；
* 业绩页契约被加列时改坏（只许加键）。
全部在内存库里，不联网。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ideagen import attribution_layers as al, config, db  # noqa: E402

DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
        "2026-08-10", "2026-08-11"]


def _prices(con, code: str, closes: list[float]) -> None:
    db.upsert_many(con, "prices", [{"code": code, "d": d, "open": c, "high": c, "low": c,
                                    "close": c, "volume": 1, "src": "test"}
                                   for d, c in zip(DAYS, closes)], ["code", "d"])


@pytest.fixture()
def con():
    c = db.init(":memory:")
    _prices(c, "US.SPY", [100, 101, 102, 103, 104, 105, 102])     # +2% over the window
    _prices(c, "US.TIP", [50, 50, 51, 52, 53, 54, 55])            # INFLATION indicator +10%
    _prices(c, "US.XLE", [80, 80, 80, 80, 80, 80, 76])            # ENERGY-SUPPLY −5%
    return c


def _pos(pid, as_of, code, theme, cost, realized, closed="2026-08-11", opened="2026-08-03"):
    return {"pos_id": pid, "as_of": as_of, "code": code, "theme": theme, "cost": cost,
            "status": "closed", "opened_d": opened, "closed_d": closed, "realized": realized}


def test_layers_close_and_construction_uses_same_period_pool(con):
    ledgers = {
        # book: one INFLATION name that made +15% on cost 1000, one ENERGY name −10% on 3000
        "alpha": {"positions": [_pos("a1", "2026-08-03", "US.GLD", "INFLATION", 1000, 150),
                                _pos("a2", "2026-08-03", "US.OIH", "ENERGY-SUPPLY", 3000, -300)],
                  "upnl_last": {}},
        "buy_all": {"positions": [_pos("b1", "2026-08-03", "US.GLD", "INFLATION", 1000, 150),
                                  _pos("b2", "2026-08-03", "US.OIH", "ENERGY-SUPPLY", 1000, -100),
                                  # a different period: must not enter alpha's comparison
                                  _pos("b3", "2026-08-10", "US.GLD", "INFLATION", 1000, 900,
                                       opened="2026-08-10")],
                    "upnl_last": {}},
    }
    out = al.layers(con, ledgers)
    row = out["by_strategy"]["alpha"]["periods"][0]
    # identity: market + theme + select == held, cost weighted
    assert row["market_pct"] + row["theme_pct"] + row["select_pct"] == pytest.approx(row["held_pct"], abs=1e-3)
    # held = (150 − 300) / 4000
    assert row["held_pct"] == pytest.approx(-3.75, abs=1e-3)
    # theme: (1000·(10−2) + 3000·(−5−2)) / 4000
    assert row["theme_pct"] == pytest.approx((1000 * 8 + 3000 * -7) / 4000, abs=1e-3)
    # pool for 08-03 only: (150 − 100) / 2000 = +2.5%
    assert row["pool_held_pct"] == pytest.approx(2.5, abs=1e-3)
    assert row["construction_pp"] == pytest.approx(-3.75 - 2.5, abs=1e-3)
    # the cumulative pool is over the periods alpha acted in, so still 08-03 only
    cum = out["by_strategy"]["alpha"]["cumulative"]
    assert cum["pool_held_pct"] == pytest.approx(2.5, abs=1e-3)
    # the pool never grades itself
    assert out["by_strategy"]["buy_all"]["cumulative"]["construction_pp"] is None


def test_unmatched_positions_are_counted_not_zeroed(con):
    ledgers = {"alpha": {"positions": [
        _pos("a1", "2026-08-03", "US.GLD", "INFLATION", 1000, 150),
        _pos("a2", "2026-08-03", "US.X", "NO-SUCH-THEME", 1000, -900),   # no indicator
        _pos("a3", "2026-08-03", "US.Y", None, 1000, -900)],             # no theme
        "upnl_last": {}}}
    row = al.layers(con, ledgers)["by_strategy"]["alpha"]["periods"][0]
    assert row["n"] == 1 and row["n_unmatched"] == 2
    assert row["held_pct"] == pytest.approx(15.0, abs=1e-3)


def test_open_positions_use_last_mark_and_unmarked_are_skipped(con):
    open_pos = {"pos_id": "o1", "as_of": "2026-08-03", "code": "US.GLD", "theme": "INFLATION",
                "cost": 1000, "status": "open", "opened_d": "2026-08-03"}
    never = dict(open_pos, pos_id="o2")
    rows = al.position_rows([open_pos, never], {"o1": ("2026-08-07", 40.0)})
    assert [r["pos_id"] for r in rows] == ["o1"]
    assert rows[0]["end"] == "2026-08-07" and rows[0]["ret"] == pytest.approx(4.0)


def test_small_samples_are_flagged(con):
    ledgers = {"alpha": {"positions": [_pos("a1", "2026-08-03", "US.GLD", "INFLATION", 1000, 150)],
                         "upnl_last": {}}}
    by = al.layers(con, ledgers)["by_strategy"]["alpha"]
    assert by["periods"][0]["sample"] == "样本不足"
    assert "少于" in by["periods"][0]["sample_why"]
    assert by["cumulative"]["sample"] == "样本不足"


def test_tracking_error():
    d = [f"2026-08-{i:02d}" for i in range(1, 30)]
    bench = [{"d": x, "v": 100 * 1.001 ** i} for i, x in enumerate(d)]
    same = [{"d": x, "v": 50 * 1.001 ** i} for i, x in enumerate(d)]
    te, n = al.tracking_error(same, bench)
    assert n == len(d) - 1 and te == pytest.approx(0.0, abs=1e-6)
    wobble = [{"d": x, "v": 100 * 1.001 ** i * (1.01 if i % 2 else 1.0)} for i, x in enumerate(d)]
    te2, _ = al.tracking_error(wobble, bench)
    assert te2 > 10          # ±1% daily differences ≈ 16% annualised
    short = al.tracking_error(same[:5], bench)
    assert short == (None, 4)


def test_paper_view_carries_the_new_keys_without_breaking_the_contract():
    import perf_fixture as fx
    from ideagen import performance as pf
    c = fx.fresh()
    fx.book(c, generated_at={fx.RUN1: "2026-07-01T03:22:00+08:00",
                             fx.RUN2: "2026-07-27T18:00:00+08:00"}, through="2026-08-31")
    v = pf.paper_view(c, None, "all")
    for r in v["summary"]["rows"]:
        assert "tracking_error_pct" in r and "te_n_days" in r
    lay = v["attribution"]["layers"]
    assert lay["available"] is True and "by_strategy" in lay
    # the existing attribution keys are untouched
    assert {"rule", "by_topic", "by_instrument", "by_method"} <= set(v["attribution"])
    ba = lay["by_strategy"]["buy_all"]["cumulative"]
    # XLE maps to its own indicator (ENERGY-SUPPLY → US.XLE); TLT's IEF is not priced
    assert ba["n"] >= 1 and ba["n_unmatched"] >= 1
    # No backtest in the fixture: the empty view has no layers block at all,
    # and must not grow one that claims to be available.
    b = pf.backtest_view(c)
    assert not (b["attribution"].get("layers") or {}).get("available")
