"""The replay may not pick from a shelf that had not happened yet.

`universe.eligible(as_of=)` was written to stop a July thesis being expressed
through a product that launched in August, and the live path called it. The
replay did not — and would not have been helped if it had, because the rows
`backtest._universe` built carried no `first_seen_d` for the gate to read. Two
correct halves of one mechanism, never connected, which is the shape this
repository keeps finding: the gate is visible in the code and absent from the
run.

Deutsche Bank's "Seven Sins" calls this pair the first and second sin, and notes
they are the same sin seen twice: knowing which instruments would exist later is
knowing which ones survived.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from ideagen import backtest, db


def _con(rows):
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    con = db.init(tmp)
    for key, vehicle, first_seen in rows:
        con.execute(
            "INSERT INTO instruments(key,kind,name,market,currency,priceable,"
            "meta,first_seen_d) VALUES(?,?,?,?,?,?,?,?)",
            (key, "listed", key, "US", "USD", 1,
             json.dumps({"vehicle": vehicle}), first_seen))
    return con


def _audit(con, as_of=date(2026, 7, 29), **kw):
    ctx = backtest.context_for(con, as_of, **kw)
    return ctx, ctx.params[backtest.CTX_TAG]


def test_universe_rows_carry_the_date_the_gate_reads():
    """The wire itself. Without this key every row is 'undated', and undated
    rows are admitted by design — so the gate would run, report exclusions of
    zero, and look like it was working."""
    con = _con([("OLD", "ETF", "2020-01-01")])
    rows = backtest._universe(con)
    assert rows and "first_seen_d" in rows[0]
    assert rows[0]["first_seen_d"] == "2020-01-01"


def test_an_instrument_that_had_not_launched_never_reaches_the_replay():
    con = _con([("OLD", "ETF", "2020-01-01"), ("NEW", "ETF", "2026-08-15")])
    ctx, a = _audit(con)
    ids = {u["instrument_id"] for u in ctx.universe}
    assert ids == {"OLD"}
    assert a.universe_dropped_as_unlisted == 1
    assert a.universe_n == 1


def test_undated_rows_are_admitted_but_counted():
    """Dropping them would silently empty every historical universe; admitting
    them silently is how the exposure stopped being visible. The third option is
    the count, which is why it is on the audit."""
    con = _con([("OLD", "ETF", "2020-01-01"), ("NODATE", "ETF", None)])
    ctx, a = _audit(con)
    assert {u["instrument_id"] for u in ctx.universe} == {"OLD", "NODATE"}
    assert a.universe_undated == 1


def test_a_post_as_of_instrument_left_in_the_universe_is_a_leak_not_a_note():
    """If someone unwires the gate, `strict` must refuse the context rather than
    hand back a wider universe with a friendly count beside it."""
    a = backtest.Audit(as_of="2026-07-29", inputs_sha="x",
                       universe_max_first_seen="2026-08-15")
    leaks = a.check()
    assert any("2026-08-15" in m for m in leaks)


def test_the_replay_universe_is_the_live_mandate_not_a_wider_one():
    """A vehicle the live run refuses to trade must not be offered to a
    generator during a replay. Otherwise the backtest is replaying a pipeline
    that never ran — one allowed to express ideas the mandate forbids."""
    con = _con([("ETF1", "ETF", "2020-01-01"), ("STOCK", "股票", "2020-01-01"),
                ("UNSURE", "待确认", "2020-01-01")])
    ctx, a = _audit(con)
    assert {u["instrument_id"] for u in ctx.universe} == {"ETF1"}
    assert a.universe_excluded == 2


def test_gate_does_not_fire_on_instruments_listed_before_the_period():
    con = _con([(f"E{i}", "ETF", "2019-01-01") for i in range(5)])
    ctx, a = _audit(con)
    assert a.universe_n == 5
    assert a.universe_dropped_as_unlisted == 0
    assert a.universe_max_first_seen == "2019-01-01"
    assert a.check() == []
