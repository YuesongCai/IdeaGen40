"""市场阶段（WS-E）：动量状态四格判得出来、标签不看未来、VIX 不写当天未收盘的 bar。不联网。"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import config, db, market_regime as mr  # noqa: E402


def _days(n: int, start=date(2025, 1, 1)) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _load(con, code, days, closes):
    db.upsert_many(con, "prices", [{"code": code, "d": d, "open": c, "high": c, "low": c,
                                    "close": c, "volume": 1, "src": "t"}
                                   for d, c in zip(days, closes)], ["code", "d"])


@pytest.fixture()
def tape_con():
    """SPY flat-up; MTUM beats it for 150 sessions, then drops hard for 25, then recovers."""
    con = db.init(":memory:")
    days = _days(320)
    spy = [100 * 1.0003 ** i for i in range(len(days))]
    mom, px = [], 100.0
    for i in range(len(days)):
        if i < 150:
            px *= 1.004
        elif i < 175:
            px *= 0.99
        elif i < 250:
            px *= 0.999
        else:
            px *= 1.006
        mom.append(px)
    _load(con, "US.SPY", days, spy)
    _load(con, config.REGIME_MOM_CODE, days, mom)
    return con, days


def test_four_states(tape_con):
    con, days = tape_con
    t = mr.Tape(con)
    assert t.label(days[140])["momentum"] == "tailwind"
    assert t.label(days[160])["momentum"] == "fading"       # slow still up, fast down
    assert t.label(days[245])["momentum"] == "headwind"
    assert t.label(days[258])["momentum"] == "recovering"
    assert t.label(days[10])["momentum"] is None             # not enough history: 缺数据
    assert t.label(days[10])["momentum_zh"] == "缺数据"


def test_label_never_reads_the_future(tape_con):
    con, days = tape_con
    before = mr.Tape(con).label(days[170])
    # overwrite everything after that day with a wild path
    _load(con, config.REGIME_MOM_CODE, days[171:], [1e6] * len(days[171:]))
    after = mr.Tape(con).label(days[170])
    assert before == after


def test_volatility_falls_back_to_realised_when_no_vix(tape_con):
    con, days = tape_con
    lab = mr.Tape(con).label(days[300])
    assert lab["vol_source"] == "SPY 21 日已实现波动" and lab["vol"] == "low"
    _load(con, config.REGIME_VIX_CODE, days[295:301], [30.0] * 6)
    lab = mr.Tape(con).label(days[300])
    assert lab["vol_source"] == "VIX" and lab["vol"] == "high"


def test_refresh_vix_skips_todays_bar_and_writes_only_idx_code():
    con = db.init(":memory:")
    today = date(2026, 9, 17)
    rows = [{"date": "2026-09-15", "adjClose": 17.1}, {"date": "2026-09-16", "adjClose": 17.2},
            {"date": "2026-09-17", "adjClose": 99.0}]
    rep = mr.refresh_vix(con, today=today, fetch=lambda a, b: rows)
    assert rep["rows"] == 2
    got = [(r["code"], r["d"]) for r in db.q(con, "SELECT code, d FROM prices ORDER BY d")]
    assert got == [("IDX.VIX", "2026-09-15"), ("IDX.VIX", "2026-09-16")]


def test_strategy_table_marks_small_samples(tape_con):
    con, days = tape_con
    res = mr.compute(con)
    assert res["available"] and res["current"]["momentum"] in mr.MOMENTUM_ZH
    assert res["by_strategy"] == {}            # no books in this database
    assert "不下自动开关" in res["framing"] or "不自动" in res["framing"]
