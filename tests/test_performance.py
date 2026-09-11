"""Gates on the performance data layer (Jon 2026-09-06 §10).

Each test pins one way this page could quietly lie: a week whose parts do not
add up to its whole, a "live" curve that still carries backfill money, an
excess return measured over a different window than its benchmark, an
attribution table whose rows sum to more than the book made, a missing
benchmark period shown as nothing instead of named, a strategy that never ran
drawn as a flat 0% line, and — the one the front-end contract depends on — a
key renamed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_fixture as fx  # noqa: E402

from ideagen import config, db, performance as pf  # noqa: E402

LIVE_GEN = "2026-07-01T03:22:00+08:00"       # the morning of its own period
BACKFILL_GEN = "2026-07-27T18:00:00+08:00"   # weeks later, like 2026-09-04 was
THROUGH = "2026-08-31"


# ----------------------------------------------------------------- contract
STRATEGY_KEYS = {"key", "name", "role", "available", "status", "reason",
                 "first_d", "last_d", "n_periods", "curve"}
WEEK_KEYS = {"week", "equity_start", "equity_end", "pnl_amt", "pnl_pct", "realized",
             "unrealized_chg", "cash_income", "fees", "flows", "reconciled", "residual"}
ROW_KEYS = {"key", "name", "role", "status", "reason", "cum_ret_pct", "excess_spy_pp",
            "excess_buy_all_pp", "max_dd_pct", "cash_share_end_pct",
            "cash_share_avg_pct", "n_periods", "n_days"}
ATTR_KEYS = {"key", "name", "pnl_full_credit", "pnl_split_equal", "n_positions"}


def assert_contract(v: dict) -> None:
    """The JSON contract the front-end agent builds against. Keys may be added,
    never renamed or dropped; types must hold on every row."""
    assert v["mode"] in ("paper", "backtest")
    assert v["label"] in ("模拟运行", "历史回测")
    assert v["subset"] in ("live", "backfill", "all")
    assert v["methodology"] in ("paper-rules", "formal-paper-rules",
                                "stock-picking-study-30d", None)
    assert "source_id" in v
    assert v["window"] is None or set(v["window"]) >= {"start", "end"}
    assert v["capital"] is None or isinstance(v["capital"], float)
    assert isinstance(v["generated_at"], str)
    for s in v["strategies"]:
        assert set(s) >= STRATEGY_KEYS, s.keys()
        assert isinstance(s["available"], bool)
        assert s["status"] in ("ok", "缺数据", "未运行", "失败")
        assert isinstance(s["n_periods"], int)
        for p in s["curve"]:
            assert set(p) >= {"d", "v"} and isinstance(p["v"], float)
        if not s["available"]:
            assert s["curve"] == [], "an unavailable strategy must not draw a line"
            assert s["status"] != "ok"
    b = v["benchmarks"]
    assert b["spy"]["name"] == "SPY" and isinstance(b["spy"]["curve"], list)
    assert set(b["buy_all"]) >= {"name", "curve", "available", "reason"}
    w = v["weekly_pnl"]
    assert isinstance(w["note"], str)
    for wk in w["weeks"]:
        assert set(wk) >= {"week", "start", "end"}
    for key, rows in w["by_strategy"].items():
        for r in rows:
            assert set(r) >= WEEK_KEYS, r.keys()
            assert isinstance(r["reconciled"], bool)
    s = v["summary"]
    assert s["cash_share_basis"] in ("end", "avg")
    for r in s["rows"]:
        assert set(r) >= ROW_KEYS, r.keys()
    rd = s["roster_diff"]
    assert rd["other_mode"] in ("backtest", "paper")
    assert isinstance(rd["added"], list)
    for a in rd["absent"]:
        assert set(a) >= {"key", "reason"}
    a = v["attribution"]
    assert isinstance(a["rule"], str)
    for name in ("by_topic", "by_instrument", "by_method"):
        for r in a[name]:
            assert set(r) >= ATTR_KEYS, r.keys()
    for r in a["by_instrument"]:
        assert isinstance(r["topics"], list) and isinstance(r["methods"], list)
    rs = v["research"]
    assert set(rs) >= {"sample_note", "n_periods", "n_days", "stats"}
    rec = v["records"]
    assert set(rec) >= {"failed_runs", "gaps", "backfill_periods", "stuck_batches",
                        "affected_ranges"}
    for r in rec["affected_ranges"]:
        assert set(r) >= {"start", "end", "why"}
    assert isinstance(v["disclosures"], list) and all(isinstance(x, str)
                                                       for x in v["disclosures"])
    json.dumps(v, allow_nan=False)      # must serialise as-is, no NaN, no dataclasses


# ----------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def con():
    c = fx.fresh()
    fx.book(c, generated_at={fx.RUN1: LIVE_GEN, fx.RUN2: BACKFILL_GEN}, through=THROUGH)
    # A backtest book in the same database: the paper view must never see it.
    db.upsert(c, "books", {"book_id": config.backtest_book("bt-x", "alpha"),
                           "label": "bt", "descr": "", "capital": 1.0,
                           "sizing": "equal", "entry": "market_close",
                           "created_at": "x"}, ["book_id"])
    db.upsert(c, "equity", {"book_id": config.backtest_book("bt-x", "alpha"),
                            "d": "2026-07-01", "cash": 1.0, "mv": 0.0, "equity": 1.0,
                            "ret_d": 0, "cum_ret": 0, "drawdown": 0, "n_open": 0,
                            "gross": 0}, ["book_id", "d"])
    return c


@pytest.fixture(scope="module")
def views(con):
    return {s: pf.paper_view(con, None, s) for s in ("live", "backfill", "all")}


# ----------------------------------------------------------------- contract
def test_every_subset_satisfies_the_contract(views):
    for v in views.values():
        assert_contract(v)
        assert v["mode"] == "paper" and v["methodology"] == "paper-rules"


def test_paper_view_never_reads_a_backtest_book(views):
    for v in views.values():
        for s in v["strategies"]:
            assert not (s.get("book_id") or "").startswith(config.BACKTEST_BOOK_PREFIX)
        assert all(not k.startswith("bt") for k in v["weekly_pnl"]["by_strategy"])


# ----------------------------------------------------------------- weeks
def test_weeks_are_iso_weeks_and_chain(views):
    for v in views.values():
        for key, rows in v["weekly_pnl"]["by_strategy"].items():
            for i, r in enumerate(rows):
                y, w = r["week"].split("-W")
                assert pf.iso_week(r["first_d"])[0] == r["week"]
                assert r["start"] <= r["first_d"] <= r["last_d"] <= r["end"]
                if i:
                    assert r["equity_start"] == rows[i - 1]["equity_end"]
                else:
                    assert r["equity_start"] == pytest.approx(v["capital"])


def test_every_week_reconciles(views):
    """realized + unrealized_chg + cash_income + fees + flows == Δequity."""
    for v in views.values():
        for key, rows in v["weekly_pnl"]["by_strategy"].items():
            for r in rows:
                parts = (r["realized"] + r["unrealized_chg"] + r["cash_income"]
                         + r["fees"] + r["flows"])
                assert r["reconciled"], (v["subset"], key, r)
                assert abs(r["pnl_amt"] - parts) <= pf.RECON_TOL + 0.01
                assert r["pnl_amt"] == pytest.approx(r["equity_end"] - r["equity_start"], abs=0.02)


def test_a_week_with_a_close_carries_realized_and_fees(views):
    """GLD stops out on 07-06: that week must show a negative realized and
    negative fees, not a silent change in unrealized."""
    rows = views["live"]["weekly_pnl"]["by_strategy"]["alpha"]
    wk = next(r for r in rows if r["first_d"] <= "2026-07-06" <= r["last_d"])
    assert wk["realized"] < 0 and wk["fees"] < 0
    assert wk["cash_income"] > 0


# ----------------------------------------------------------------- isolation
def test_subsets_partition_the_positions(con):
    live = pf.book_ledger(con, "sel-buy_all", "live")
    back = pf.book_ledger(con, "sel-buy_all", "backfill")
    allp = pf.book_ledger(con, "sel-buy_all", "all")
    l, b, a = ({p["pos_id"] for p in x["positions"]} for x in (live, back, allp))
    assert l and b
    assert not (l & b)
    assert l | b == a
    assert {p["as_of"] for p in live["positions"]} == {fx.P1}
    assert {p["as_of"] for p in back["positions"]} == {fx.P2}


def test_live_interest_is_recomputed_on_the_rebuilt_balance(con, views):
    """The stored INT rows belong to the whole account. On the live subset the
    backfill buys never happened, so the balance is higher and the interest on
    it must be higher too — and computed with the ledger's own rate, not a
    constant pulled from config."""
    stored = db.q1(con, "SELECT SUM(cash_delta) s FROM trades WHERE book_id='sel-buy_all' "
                        "AND side='INT' AND d>'2026-07-27'")["s"]
    live = pf.book_ledger(con, "sel-buy_all", "live")
    rebuilt = sum(v for d, v in live["interest"].items() if d > "2026-07-27")
    assert rebuilt > stored * 1.01
    # First accrual day after entry: interest uses the previous rebuilt cash.
    d0 = min(live["interest"])
    rate = pf._rate_on(con, "sel-buy_all", d0)
    assert rate is not None
    prev = pf.paper._prev_session(con, d0)
    base = next(p["cash"] for p in live["points"] if p["d"] == prev)
    from datetime import date
    days = (date.fromisoformat(d0) - date.fromisoformat(prev)).days
    assert live["interest"][d0] == pytest.approx(base * rate * days / 365.0, rel=1e-6)
    # And the three curves end in three different places.
    ends = {s: views[s]["strategies"][0]["curve"][-1]["v"] for s in views}
    assert len(set(ends.values())) == 3


def test_all_subset_is_the_stored_ledger(con, views):
    eq = db.q(con, "SELECT d, equity FROM equity WHERE book_id='sel-alpha' ORDER BY d")
    curve = next(s for s in views["all"]["strategies"] if s["key"] == "alpha")["curve"]
    assert [p["d"] for p in curve] == [r["d"] for r in eq]
    assert all(p["v"] == pytest.approx(r["equity"], abs=0.01) for p, r in zip(curve, eq))


def test_live_window_excludes_old_backfill_days_and_interest():
    c = fx.fresh()
    fx.book(c, generated_at={fx.RUN1: LIVE_GEN,
                            fx.RUN2: "2026-07-08T07:00:00+08:00"}, through=THROUGH)
    c.execute("UPDATE orch_runs SET data_classification='backfill' WHERE run_id=?", (fx.RUN1,))
    c.execute("UPDATE orch_runs SET data_classification='live' WHERE run_id=?", (fx.RUN2,))
    v = pf.paper_view(c, None, "live")
    led = pf.book_ledger(c, "sel-alpha", "live")
    fill = min(p["opened_d"] for p in led["positions"])
    seed = pf.paper._prev_session(c, fill)
    assert led["points"][0]["d"] == seed
    assert led["points"][0]["equity"] == led["capital"]
    assert led["points"][1]["d"] == fill
    assert all(d > fill for d in led["interest"])
    assert v["window"]["start"] == seed
    assert v["benchmarks"]["spy"]["curve"][0]["d"] == seed
    assert pf.paper_view(c, None, "all")["window"]["start"] < seed
    assert all(r["reconciled"] for rows in v["weekly_pnl"]["by_strategy"].values() for r in rows)
    first = led["points"][1]
    trades = led["trades_by_d"][fill]
    assert first["equity"] == pytest.approx(
        led["capital"] + sum(t["cash_delta"] for t in trades) + first["mv"])
    c.execute("UPDATE orch_runs SET data_classification='backfill'")
    empty = pf.paper_view(c, None, "live")
    assert empty["window"] is None
    assert empty["benchmarks"]["spy"]["curve"] == []
    c.close()


def test_disclosures_state_the_rebuild_rule(views):
    assert any("重建" in x for x in views["live"]["disclosures"])
    assert any("原样" in x for x in views["all"]["disclosures"])


# ----------------------------------------------------------------- summary
def test_excess_return_uses_the_strategys_own_window(con, views):
    v = views["all"]
    spy = {p["d"]: p["v"] for p in v["benchmarks"]["spy"]["curve"]}
    for row in v["summary"]["rows"]:
        if row["status"] != "ok":
            assert row["cum_ret_pct"] is None and row["excess_spy_pp"] is None
            continue
        s = next(x for x in v["strategies"] if x["key"] == row["key"])
        spy_ret = (spy[s["last_d"]] / spy[s["first_d"]] - 1) * 100
        assert row["excess_spy_pp"] == pytest.approx(row["cum_ret_pct"] - spy_ret, abs=1e-3)
        assert row["max_dd_pct"] is not None and row["max_dd_pct"] <= 0
        assert row["n_days"] == len(s["curve"]) - 1
        assert 0 <= row["cash_share_end_pct"] <= 100
        assert 0 <= row["cash_share_avg_pct"] <= 100


def test_excess_over_buy_all_is_null_for_buy_all_itself(views):
    row = next(r for r in views["all"]["summary"]["rows"] if r["key"] == "buy_all")
    assert row["excess_buy_all_pp"] is None
    alpha = next(r for r in views["all"]["summary"]["rows"] if r["key"] == "alpha")
    assert alpha["excess_buy_all_pp"] == pytest.approx(
        alpha["cum_ret_pct"] - row["cum_ret_pct"], abs=1e-3)


def test_registry_arms_without_a_book_are_not_run_not_zero(views):
    v = views["live"]
    keys = {s["key"]: s for s in v["strategies"]}
    assert "omega_strict" in keys           # from the registry, no sel- book here
    s = keys["omega_strict"]
    assert s["status"] == "未运行" and s["reason"] and s["curve"] == []
    assert s["name"] == "赔率排序 · 严格"
    row = next(r for r in v["summary"]["rows"] if r["key"] == "omega_strict")
    assert row["cum_ret_pct"] is None


def test_roster_diff_reasons_come_only_from_records(con, views):
    # No backtest recorded: nothing to compare against, and no invented reasons.
    rd = views["live"]["summary"]["roster_diff"]
    assert rd["other_mode"] == "backtest" and rd["added"] == []
    assert all(a["reason"] is None for a in rd["absent"])


# ----------------------------------------------------------------- benchmark
def test_buy_all_missing_period_is_named(con):
    c = fx.fresh()
    fx.book(c, generated_at={fx.RUN1: LIVE_GEN, fx.RUN2: BACKFILL_GEN}, through=THROUGH)
    # Take buy_all's second period away the way the real ledger lost 08-12:
    # the batch exists, nothing was booked from it.
    with db.tx(c):
        for t in ("orders", "positions", "trades"):
            c.execute(f"DELETE FROM {t} WHERE book_id='sel-buy_all' AND idea_uid LIKE 'W20260708-buy_all#%'")
        c.execute("DELETE FROM mtm WHERE pos_id NOT IN (SELECT pos_id FROM positions)")
        c.execute("UPDATE batches SET status='draft' WHERE batch_id='W20260708-buy_all'")
    v = pf.paper_view(c, None, "all")
    b = v["benchmarks"]["buy_all"]
    assert b["available"] is False
    assert "2026-07-08" in b["reason"] and "W20260708-buy_all" in b["reason"]
    assert b["missing_periods"] == ["2026-07-08"]


def test_buy_all_available_when_it_covers_every_period(views):
    for v in views.values():
        assert v["benchmarks"]["buy_all"]["available"] is True
        assert v["benchmarks"]["buy_all"]["reason"] is None


# ----------------------------------------------------------------- attribution
def test_split_equal_adds_up_and_full_credit_does_not(views):
    a = views["all"]["attribution"]
    total = a["total_pnl"]
    for name in ("by_topic", "by_method", "by_instrument"):
        assert sum(r["pnl_split_equal"] for r in a[name]) == pytest.approx(total, abs=0.05)
    # GLD is proposed under two topics and two methods, so the full-credit
    # columns double-count it: their sum must differ from the total.
    gld_pnl = next(r for r in a["by_instrument"] if r["key"] == "US.GLD")["pnl_full_credit"]
    assert sum(r["pnl_full_credit"] for r in a["by_topic"]) == pytest.approx(total + gld_pnl, abs=0.05)
    assert sum(r["pnl_full_credit"] for r in a["by_method"]) == pytest.approx(total + gld_pnl, abs=0.05)


def test_attribution_keeps_the_full_source_relationship(views):
    a = views["all"]["attribution"]
    gld = next(r for r in a["by_instrument"] if r["key"] == "US.GLD")
    assert set(gld["topics"]) == {"INFLATION", "GEOPOLITICS"}
    assert set(gld["methods"]) == {"ai_native", "chain"}
    assert {r["key"] for r in a["by_topic"]} >= {"INFLATION", "GEOPOLITICS", "ENERGY-SUPPLY", "POLICY-PATH"}
    assert "来源未记录" not in {r["key"] for r in a["by_method"]}


def test_attribution_total_equals_trading_pnl_of_the_subset(con, views):
    led = pf.book_ledger(con, "sel-alpha", "live")
    led2 = pf.book_ledger(con, "sel-buy_all", "live")
    expect = sum(float(p["realized"] or 0) for p in led["positions"] + led2["positions"]
                 if p["status"] == "closed")
    assert views["live"]["attribution"]["total_pnl"] == pytest.approx(expect, abs=0.05)


# ----------------------------------------------------------------- records
def test_records_carry_backfill_and_affected_ranges(views):
    rec = views["live"]["records"]
    assert rec["backfill_periods"] == [fx.P2]
    assert any(r["start"] == fx.P2 and "补跑" in r["why"] for r in rec["affected_ranges"])
    assert fx.P2 not in [r["start"] for r in views["backfill"]["records"]["affected_ranges"]]


def test_failed_runs_and_stuck_batches_are_reported(con):
    c = fx.fresh()
    db.upsert(c, "orch_runs", {"run_id": "run-dead", "as_of": "2026-07-15", "kind": "weekly",
                               "platform": "local", "started_at": "x", "ended_at": "y",
                               "ok": 0, "error": "boom", "inputs_sha": "x",
                               "journal_uri": None, "calls": 0,
                               "data_classification": None}, ["run_id"])
    db.upsert(c, "batches", {"batch_id": "W20260715-alpha", "as_of": "2026-07-15",
                             "generated_at": "x", "generator": "weekly:run-dead",
                             "methodology": "0.4", "n_ideas": 3, "status": "draft",
                             "validation": {"pass": False, "checks": [
                                 {"check": "ref_price_present", "ok": False,
                                  "severity": "error"}]}}, ["batch_id"])
    rec = pf.records(c)
    assert [f["as_of"] for f in rec["failed_runs"]] == ["2026-07-15"]
    assert rec["failed_runs"][0]["resolved"] is False
    assert rec["stuck_batches"][0]["blocked_by"] == ["ref_price_present"]
    whys = [r["why"] for r in rec["affected_ranges"] if r["start"] == "2026-07-15"]
    assert any("周跑失败" in w for w in whys) and any("校验未过" in w for w in whys)


# ----------------------------------------------------------------- backtest view
def _study_run(c, *, excluded=("ai_native",), with_reason=True):
    summary = {"dates": [fx.P1, fx.P2], "arms": {"alpha": {}, "buy_all": {}},
               "excluded_arms": list(excluded),
               "disclaimer": ("候选池与价格均为真实数据。"
                              + ("未参与：ai_native（需调用模型，会使复算不可重复）。"
                                 if with_reason else ""))}
    db.upsert(c, "backtest_runs", {
        "backtest_id": "bt-real-test", "as_of": fx.P2, "window_start": fx.P1,
        "window_end": fx.P2, "methodology": "real-pool-asof-replay/v1",
        "data_classification": "mixed-live-backfill", "model_id": None,
        "model_release_date": None, "knowledge_cutoff": None, "inputs_sha": "x",
        "artifact_uri": None, "started_at": "s", "ended_at": "e", "ok": 1, "error": None,
        "summary": json.dumps(summary, ensure_ascii=False)}, ["backtest_id"])
    pts = []
    for arm, path in (("alpha", [100, 101, 99, 102, 103]), ("buy_all", [100, 100.5, 100.2, 101, 101.5])):
        for d, v in zip(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"], path):
            pts.append({"backtest_id": "bt-real-test", "arm": arm, "d": d, "equity": v,
                        "period_ret": 0.0, "drawdown": 0.0, "n_positions": 1})
    db.upsert_many(c, "backtest_points", pts, ["backtest_id", "arm", "d"])
    db.upsert_many(c, "backtest_positions", [
        {"backtest_id": "bt-real-test", "arm": "alpha", "period": fx.P1, "instrument_id": "GLD",
         "entry_d": fx.P1, "exit_d": "2026-07-31", "entry_nav": 200.0, "exit_nav": 190.0,
         "return_pct": -5.0, "status": "OK", "thesis": "t"},
        {"backtest_id": "bt-real-test", "arm": "buy_all", "period": fx.P1, "instrument_id": "GLD",
         "entry_d": fx.P1, "exit_d": "2026-07-31", "entry_nav": 200.0, "exit_nav": 190.0,
         "return_pct": -5.0, "status": "OK", "thesis": "t"},
        {"backtest_id": "bt-real-test", "arm": "buy_all", "period": fx.P1, "instrument_id": "ZZZ",
         "entry_d": None, "exit_d": None, "entry_nav": None, "exit_nav": None,
         "return_pct": 3.0, "status": "OK", "thesis": "t"},
    ], ["backtest_id", "arm", "period", "instrument_id"])


def test_backtest_view_is_built_from_backtest_tables_only(con):
    _study_run(con)
    v = pf.backtest_view(con, None, "bt-real-test")
    assert_contract(v)
    assert v["mode"] == "backtest" and v["methodology"] == "stock-picking-study-30d"
    assert v["source_id"] == "bt-real-test" and v["capital"] is None
    ok = {s["key"]: s for s in v["strategies"] if s["available"]}
    assert set(ok) == {"alpha", "buy_all"}
    # Points, not the sel- ledger: the paper alpha book has 40+ marks, this has 5.
    assert len(ok["alpha"]["curve"]) == 5 and ok["alpha"]["curve"][-1]["v"] == 103.0
    row = next(r for r in v["summary"]["rows"] if r["key"] == "alpha")
    assert row["cum_ret_pct"] == pytest.approx(3.0)
    assert row["cash_share_end_pct"] is None      # the study has no cash account
    assert v["weekly_pnl"]["by_strategy"]["alpha"][0]["reconciled"] is False
    assert "不可" in v["weekly_pnl"]["note"] or "未记录" in v["weekly_pnl"]["note"]
    assert any("30" in x and "止损" in x for x in v["disclosures"])


def test_backtest_roster_reasons_come_from_the_record(con):
    _study_run(con, excluded=("ai_native",), with_reason=True)
    v = pf.backtest_view(con, None, "bt-real-test")
    absent = {a["key"]: a["reason"] for a in v["summary"]["roster_diff"]["absent"]}
    ai = next(s for s in v["strategies"] if s["key"] == "ai_native")
    assert ai["status"] == "未运行"
    assert "需调用模型" in ai["reason"] and ai["reason"].startswith("summary.disclaimer")
    # Arms the record never mentions get null, not a guess.
    calib = next(s for s in v["strategies"] if s["key"] == "calib")
    assert calib["status"] == "未运行" and calib["reason"] is None
    # `sel-` books absent from the backtest, reason only if recorded.
    assert set(absent) <= {"alpha", "buy_all"} or absent == {}


def test_backtest_roster_without_a_recorded_reason_is_null(con):
    _study_run(con, excluded=("ai_native",), with_reason=False)
    v = pf.backtest_view(con, None, "bt-real-test")
    ai = next(s for s in v["strategies"] if s["key"] == "ai_native")
    assert "未注明原因" in ai["reason"]
    _study_run(con, excluded=(), with_reason=False)
    v = pf.backtest_view(con, None, "bt-real-test")
    ai = next(s for s in v["strategies"] if s["key"] == "ai_native")
    assert ai["reason"] is None


def test_study_attribution_walks_candidates_and_marks_unlinked(con):
    _study_run(con)
    a = pf.backtest_view(con, None, "bt-real-test")["attribution"]
    assert a["unit"] == "pct_points"
    gld = next(r for r in a["by_instrument"] if r["key"] == "GLD")
    assert set(gld["topics"]) == {"INFLATION", "GEOPOLITICS"}
    zzz = next(r for r in a["by_instrument"] if r["key"] == "ZZZ")
    assert zzz["topics"] == ["来源未记录"]
    assert sum(r["pnl_split_equal"] for r in a["by_topic"]) == pytest.approx(a["total_pnl"], abs=1e-3)


def test_backtest_view_without_any_run_is_not_a_zero_line():
    c = fx.fresh()
    v = pf.backtest_view(c, None, None)
    assert_contract(v)
    assert all(not s["available"] and s["status"] == "未运行" for s in v["strategies"])
    assert v["summary"]["rows"] == []


# ----------------------------------------------------------------- index
def test_perf_index_is_cheap_and_names_both_modes(con):
    _study_run(con)
    idx = pf.perf_index(con)
    modes = {m["mode"]: m for m in idx["modes"]}
    assert modes["paper"]["available"] and modes["paper"]["n_strategies"] == 2
    assert modes["paper"]["methodology"] == "paper-rules"
    assert modes["backtest"]["available"] and modes["backtest"]["source_id"] == "bt-real-test"
    assert modes["backtest"]["methodology"] == "stock-picking-study-30d"
    assert idx["live"]["available"] is False and idx["live"]["label"].startswith("实盘")
    assert idx["live"]["reason"] == "尚未接入真实资金"
    assert idx["backtest_sources"][0]["backtest_id"] == "bt-real-test"
    json.dumps(idx)
