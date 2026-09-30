"""Holdings must reconcile to one account, across currencies and subsets."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_fixture as fx
from ideagen import db, holdings, performance


@pytest.fixture
def con():
    c = db.init(":memory:")
    for book in ("sel-alpha", "sel-beta"):
        db.upsert(c, "books", {"book_id": book, "label": book, "capital": 1000,
                               "sizing": "equal", "entry": "market", "created_at": "2026-09-01"}, ["book_id"])
    db.upsert(c, "batches", {"batch_id": "b", "as_of": "2026-09-01", "generated_at": "2026-09-01",
                              "generator": "test", "methodology": "test", "status": "traded"}, ["batch_id"])
    db.upsert(c, "instruments", {"key": "02800", "kind": "listed", "futu_code": "HK.02800",
                                 "currency": "HKD"}, ["key"])
    for pid, book, qty, px in (("p1", "sel-alpha", 10, 78), ("p2", "sel-alpha", 20, 156), ("p3", "sel-beta", 99, 78)):
        uid = "i" + pid
        gross = qty * px / 7.8
        db.upsert(c, "ideas", {"idea_uid": uid, "batch_id": "b", "as_of": "2026-09-01", "local_id": 1,
                               "tool": "02800", "tool_desc": "恒生ETF", "futu_code": "HK.02800",
                               "instrument": "listed", "horizon": "1个月", "horizon_months": 1,
                               "hurdle": 0, "theme_id": pid, "thesis": pid + "的理由"}, ["idea_uid"])
        db.upsert(c, "positions", {"pos_id": pid, "book_id": book, "idea_uid": uid, "code": "HK.02800",
                                   "kind": "listed", "qty": qty, "avg_px": px, "cost": gross + 1,
                                   "opened_d": "2026-09-01", "as_of": "2026-09-01", "status": "open"}, ["pos_id"])
        db.upsert(c, "trades", {"trade_id": pid, "book_id": book, "pos_id": pid, "idea_uid": uid,
                                "d": "2026-09-01", "side": "BUY", "code": "HK.02800", "qty": qty,
                                "px": px, "gross": gross, "fee": 1, "cash_delta": -gross - 1}, ["trade_id"])
        mv = qty * 156 / 7.8
        db.upsert(c, "mtm", {"book_id": book, "pos_id": pid, "d": "2026-09-29",
                             "px": 156, "mv": mv, "upnl": mv - gross - 1}, ["book_id", "pos_id", "d"])
    db.upsert(c, "equity", {"book_id": "sel-alpha", "d": "2026-09-29", "cash": 498,
                            "mv": 600, "equity": 1098}, ["book_id", "d"])
    db.upsert(c, "orders", {"order_id": "pending", "book_id": "sel-alpha", "idea_uid": "ip1",
                            "as_of": "2026-09-30", "side": "BUY", "code": "HK.02800", "kind": "listed",
                            "notional": 200, "placed_d": "2026-09-30", "status": "pending"}, ["order_id"])
    yield c
    c.close()


def test_account_scoped_weighted_cost_and_hkd_conversion(con):
    a = holdings.account(con, "sel-alpha")
    assert a["reconciled"]
    assert a["cash"] + a["market_value"] == a["equity"]
    p, = a["positions"]
    assert p["qty"] == 30  # the other account's 99 shares must not leak in
    assert p["currency"] == "HKD"
    assert p["cost"] == pytest.approx(502)
    assert p["avg_cost"] == pytest.approx(502 * 7.8 / 30)  # qty weighted, fees included
    assert p["last_px"] == 156  # local quote, not dollar market value
    assert p["market_value"] == 600
    assert p["weight_pct"] == pytest.approx(600 / 1098 * 100)  # denominator includes cash
    assert p["unrealized"] == 98
    assert p["return_pct"] == pytest.approx(98 / 502 * 100)
    assert {l["theme"] for l in p["lots"]} == {"p1", "p2"}
    assert a["pending_notional"] == 200  # not part of the $600 held
    assert len(a["pending"]) == 1


def test_missing_and_stale_marks_are_not_zero_or_current(con):
    con.execute("DELETE FROM mtm WHERE pos_id='p2'")
    a = holdings.account(con, "sel-alpha")
    assert not a["reconciled"]
    assert a["positions"][0]["market_value"] is None
    assert a["unrealized"] is None
    con.execute("UPDATE mtm SET d='2026-09-28' WHERE pos_id='p1'")
    p = holdings.account(con, "sel-alpha")["positions"][0]
    assert p["market_value"] is None
    assert p["weight_pct"] is None


def test_missing_trade_fx_does_not_invent_average_cost(con):
    con.execute("DELETE FROM trades WHERE pos_id='p2'")
    p, = holdings.account(con, "sel-alpha")["positions"]
    assert p["avg_cost"] is None
    assert p["cost"] == 502


def test_holding_date_does_not_mix_future_fills_or_marks(con):
    con.execute("UPDATE positions SET opened_d='2026-09-30' WHERE pos_id='p2'")
    con.execute("UPDATE mtm SET d='2026-09-30' WHERE pos_id='p1'")
    a = holdings.account(con, "sel-alpha")
    assert a["positions"][0]["qty"] == 10
    assert a["positions"][0]["last_px"] is None
    assert not a["reconciled"]


@pytest.mark.parametrize("subset", ["all", "live", "backfill"])
def test_subset_uses_performance_cash_replay(subset):
    c = fx.fresh()
    try:
        fx.book(c, generated_at={fx.RUN1: "2026-07-01T03:22:00+08:00",
                                 fx.RUN2: "2026-07-08T03:22:00+08:00"}, through="2026-07-09")
        a = holdings.account(c, "sel-buy_all", subset)
        ledger = performance.book_ledger(c, "sel-buy_all", subset)
        assert a["equity"] == pytest.approx(ledger["points"][-1]["equity"])
        assert a["cash"] == pytest.approx(ledger["points"][-1]["cash"])
        assert a["reconciled"]
        if subset != "all":
            assert {l["classification"] for p in a["positions"] for l in p["lots"]} == {subset}
            assert a["equity"] != holdings.account(c, "sel-buy_all", "all")["equity"]
    finally:
        c.close()


def test_invalid_subset_cannot_silently_return_all(con):
    with pytest.raises(ValueError):
        holdings.view(con, "typo")


def route(path, authorized=True):
    from ideagen.serve import Handler
    h = Handler.__new__(Handler)
    h.path = path
    h._authorized = lambda: authorized
    h._acct = lambda: None
    h._session_user = lambda: None
    h._login_redirect = lambda: (302, "login")
    h._json = lambda doc, status=200: (status, doc)
    return h


def test_holdings_endpoint_obeys_existing_auth_guard(monkeypatch):
    monkeypatch.setattr(db, "init", lambda: pytest.fail("unauthorized request opened the ledger"))
    assert route("/api/holdings", False)._route_get() == (302, "login")
    status, _ = route("/api/holdings?subset=wrong")._route_get()
    assert status == 400


def test_holdings_endpoint_reads_ledger_and_closes_connection(con, monkeypatch):
    import sqlite3
    monkeypatch.setattr(db, "init", lambda: con)
    status, doc = route("/api/holdings?subset=all")._route_get()
    assert status == 200
    a = next(b for b in doc["books"] if b["selector"] == "alpha")
    assert a["positions"][0]["cost"] == 502
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")
