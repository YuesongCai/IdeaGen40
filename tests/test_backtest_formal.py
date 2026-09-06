"""Gates on the formal backtest engine.

What it must prove: that it trades with the paper engine's own rules (a stop, a
take and a horizon exit fire because `paper.step` fired them), that its books
are invisible to the daily marking loop, that rerunning the same id does not
double anything, and that the paper side of the page never sees its data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_fixture as fx  # noqa: E402
from test_performance import assert_contract  # noqa: E402

from ideagen import backtest_formal as bf, config, db, paper, performance as pf  # noqa: E402

END = "2026-08-31"


@pytest.fixture(scope="module")
def con():
    c = fx.fresh()
    # The paper books exist too, booked the live way, so isolation is testable.
    fx.book(c, generated_at={fx.RUN1: "2026-07-01T03:22:00+08:00",
                             fx.RUN2: "2026-07-27T18:00:00+08:00"}, through=END)
    return c


@pytest.fixture(scope="module")
def receipt(con):
    return bf.run(con, end=END, verbose=False)


def _counts(con, bid):
    return {
        "points": db.q1(con, "SELECT COUNT(*) n FROM backtest_points WHERE backtest_id=?", (bid,))["n"],
        "positions": db.q1(con, "SELECT COUNT(*) n FROM backtest_positions WHERE backtest_id=?", (bid,))["n"],
        "runs": db.q1(con, "SELECT COUNT(*) n FROM backtest_runs WHERE backtest_id=?", (bid,))["n"],
        "books": len(paper.backtest_books(con, bid)),
        "trades": db.q1(con, "SELECT COUNT(*) n FROM trades WHERE book_id LIKE ?",
                        (config.backtest_book(bid, "") + "%",))["n"],
        "batches": db.q1(con, "SELECT COUNT(*) n FROM batches WHERE batch_id LIKE ?",
                         (f"{config.BACKTEST_BATCH_PREFIX}{bid}-%",))["n"],
    }


def test_dry_run_plan_lists_periods_and_arms(con):
    plan = bf.plan(con)
    assert [p["as_of"] for p in plan["periods"]] == [fx.P1, fx.P2]
    assert plan["periods"][0]["classification"] == "live"
    assert plan["periods"][1]["classification"] == "backfill"
    assert plan["arms"] == ["alpha", "buy_all"]
    assert plan["periods"][1]["arms"] == {"alpha": 1, "buy_all": 2}


def test_run_creates_isolated_books_and_batches(con, receipt):
    bid = receipt["backtest_id"]
    assert bid.startswith("bt-formal-20260708-")
    books = paper.backtest_books(con, bid)
    assert books == [config.backtest_book(bid, "alpha"), config.backtest_book(bid, "buy_all")]
    for b in books:
        assert config.is_backtest_book(b)
        assert paper.book_spec(b) is config.SELECTOR_SPEC
    # The daily marking loop must not know these books exist.
    assert not any(config.is_backtest_book(b) for b in paper.all_books(con))
    assert "sel-alpha" in paper.all_books(con)
    # Batches carry the id and a generator that names the run they replay.
    gens = {r["generator"] for r in db.q(
        con, "SELECT generator FROM batches WHERE batch_id LIKE ?",
        (f"{config.BACKTEST_BATCH_PREFIX}{bid}-%",))}
    assert gens == {f"backtest:{bid}:{fx.RUN1}", f"backtest:{bid}:{fx.RUN2}"}
    # The generation instant is the clamped one, not now.
    gen_at = {r["generated_at"] for r in db.q(
        con, "SELECT generated_at FROM batches WHERE batch_id LIKE ?",
        (f"{config.BACKTEST_BATCH_PREFIX}{bid}-%",))}
    assert gen_at == {f"{fx.P1}T07:23:00+08:00", f"{fx.P2}T07:23:00+08:00"}


def test_orders_fill_on_the_periods_own_session(con, receipt):
    """The paper backfill bought TLT on 07-27; the replay buys it on 07-08."""
    bid = receipt["backtest_id"]
    opened = {(r["code"], r["as_of"]): r["opened_d"] for r in db.q(
        con, "SELECT code, as_of, opened_d FROM positions WHERE book_id=?",
        (config.backtest_book(bid, "buy_all"),))}
    assert opened[("US.TLT", fx.P2)] == fx.P2
    assert opened[("US.GLD", fx.P1)] == fx.P1
    paper_tlt = db.q1(con, "SELECT opened_d FROM positions WHERE book_id='sel-buy_all' "
                           "AND code='US.TLT'")["opened_d"]
    assert paper_tlt == "2026-07-27"


def test_exits_come_from_the_paper_rules(receipt):
    """A stop (GLD −3%/day), a take (TLT +2%/day) and a horizon exit (XLE flat)
    all have to appear, each fired by `paper.step` itself."""
    arms = receipt["summary"]["arms"]
    assert arms["alpha"]["exits"] == {"stop": 1, "take": 1}
    assert arms["buy_all"]["exits"] == {"horizon": 2, "stop": 1, "take": 1}
    assert arms["buy_all"]["orders"] == {"filled": 4}
    assert arms["alpha"]["errors"] == {} and arms["buy_all"]["periods_booked"] == 2
    assert arms["alpha"]["cum_ret_pct"] is not None


def test_points_and_positions_are_written_from_the_books(con, receipt):
    bid = receipt["backtest_id"]
    n = _counts(con, bid)
    assert n["runs"] == 1 and n["books"] == 2 and n["batches"] == 4
    assert n["points"] == receipt["points"] > 0
    assert n["positions"] == receipt["positions"] == 6
    eq = db.q(con, "SELECT d, equity FROM equity WHERE book_id=? ORDER BY d",
              (config.backtest_book(bid, "alpha"),))
    pts = db.q(con, "SELECT d, equity FROM backtest_points WHERE backtest_id=? AND arm='alpha' ORDER BY d", (bid,))
    assert [(r["d"], r["equity"]) for r in eq] == [(r["d"], r["equity"]) for r in pts]
    pos = {(r["arm"], r["period"], r["instrument_id"]): r for r in db.q(
        con, "SELECT * FROM backtest_positions WHERE backtest_id=?", (bid,))}
    gld = pos[("alpha", fx.P1, "GLD")]
    assert gld["status"] == "closed:stop" and gld["return_pct"] < -5 and gld["thesis"] == "GLD thesis"
    run = db.q1(con, "SELECT * FROM backtest_runs WHERE backtest_id=?", (bid,))
    assert run["methodology"] == "formal-paper-rules" and run["ok"] == 1
    assert run["data_classification"] == "mixed-live-backfill"
    s = json.loads(run["summary"])
    assert s["disclosures"] and s["period_classification"] == {fx.P1: "live", fx.P2: "backfill"}
    assert s["excluded_reasons"]["omega_strict"]


def test_rerun_is_idempotent(con, receipt):
    bid = receipt["backtest_id"]
    before = _counts(con, bid)
    again = bf.run(con, end=END, verbose=False)
    assert again["backtest_id"] == bid
    assert _counts(con, bid) == before
    assert again["summary"]["arms"]["alpha"]["cum_ret_pct"] == receipt["summary"]["arms"]["alpha"]["cum_ret_pct"]


def test_paper_books_are_untouched_by_the_replay(con, receipt):
    """Same instruments, same weeks, different books: the sel- ledger must be
    exactly what booking left, and the paper view must not list bt: books."""
    n_sel = db.q1(con, "SELECT COUNT(*) n FROM positions WHERE book_id LIKE 'sel-%'")["n"]
    assert n_sel == 6
    v = pf.paper_view(con, None, "all")
    assert_contract(v)
    assert {s["book_id"] for s in v["strategies"] if s["book_id"]} == {"sel-alpha", "sel-buy_all"}


def test_formal_view_reads_the_retained_ledger(con, receipt):
    v = pf.backtest_view(con, None, receipt["backtest_id"])
    assert_contract(v)
    assert v["methodology"] == "formal-paper-rules"
    assert v["capital"] == config.SELECTOR_SPEC["capital"]
    for arm, rows in v["weekly_pnl"]["by_strategy"].items():
        assert rows and all(r["reconciled"] for r in rows), (arm, rows)
    row = next(r for r in v["summary"]["rows"] if r["key"] == "alpha")
    assert row["cash_share_end_pct"] is not None and row["cum_ret_pct"] is not None
    assert v["attribution"]["unit"] == "usd"
    assert sum(r["pnl_split_equal"] for r in v["attribution"]["by_topic"]) == pytest.approx(
        v["attribution"]["total_pnl"], abs=0.05)
    assert any("永远不会触发" in x for x in v["disclosures"])
    absent = {a["key"] for a in v["summary"]["roster_diff"]["absent"]}
    assert absent == set()
    omega = next(s for s in v["strategies"] if s["key"] == "omega_strict")
    assert omega["status"] == "未运行" and "判决" in omega["reason"]


def test_arm_filter_and_window(con):
    rep = bf.run(con, start=fx.P2, end=END, arms=["alpha"], backtest_id="bt-formal-sub", verbose=False)
    assert rep["dates"] == [fx.P2] and rep["arms"] == ["alpha"]
    s = rep["summary"]
    assert s["excluded_reasons"]["buy_all"] == "本次回测按 --arms 参数未包含"
    assert paper.backtest_books(con, "bt-formal-sub") == [config.backtest_book("bt-formal-sub", "alpha")]
    n = bf.cleanup(con, "bt-formal-sub")
    assert n["books"] == 1 and n["batches"] == 1
    assert paper.backtest_books(con, "bt-formal-sub") == []


def test_no_successful_runs_is_an_error_not_a_flat_line():
    c = fx.fresh()
    c.execute("UPDATE orch_runs SET ok=0")
    with pytest.raises(ValueError):
        bf.run(c, verbose=False)
