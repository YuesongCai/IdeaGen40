"""Archiving the selector books and rebuilding them from the periods' current runs.

The PM's 2026-09-07 decision after the six-period replay: the method page
read the new runs while the holdings page still held the superseded runs'
positions. `restate_selector_books` moves the old ledger aside under `old:`
(nothing deleted) and walks fresh `sel-<arm>` books through the paper engine
from the current runs. These tests pin the three properties that make that
safe: the archive keeps every row, the archive is invisible to every reader
that walks `sel-%`, and the superseded batches no longer pose as the period's.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

import perf_fixture as fx  # noqa: E402

from ideagen import backtest_formal as bf, config, db, ideas as ideas_mod, paper  # noqa: E402

END = "2026-08-31"


@pytest.fixture(scope="module")
def con():
    c = fx.fresh()
    fx.book(c, generated_at={fx.RUN1: "2026-07-01T03:22:00+08:00",
                             fx.RUN2: "2026-07-27T18:00:00+08:00"}, through=END)
    return c


def _rows(con, book_id):
    return {t: db.q1(con, f"SELECT COUNT(*) n FROM {t} WHERE book_id=?", (book_id,))["n"]
            for t in ("orders", "positions", "trades", "equity", "mtm")}


@pytest.fixture(scope="module")
def receipt(con):
    before = {b: _rows(con, b) for b in paper.selector_books(con)}
    rep = bf.restate_selector_books(con, end=END, note="test", verbose=False)
    rep["_before"] = before
    return rep


def test_the_old_ledger_is_archived_row_for_row(con, receipt):
    assert receipt["archived"], "nothing was archived"
    for arm, new_id in receipt["archived"].items():
        old_id = config.selector_book(arm)
        assert new_id.startswith(bf.ARCHIVE_PREFIX + old_id + "@")
        assert _rows(con, new_id) == receipt["_before"][old_id]
        assert db.q1(con, "SELECT 1 x FROM books WHERE book_id=?", (new_id,))


def test_archived_books_are_outside_every_walk(con, receipt):
    walked = set(paper.all_books(con)) | set(paper.selector_books(con))
    for new_id in receipt["archived"].values():
        assert new_id not in walked


def test_fresh_books_hold_the_current_runs_positions(con, receipt):
    booked = [a for a, s in receipt["arms"].items() if s.get("periods_booked")]
    assert booked, receipt["arms"]
    for arm in booked:
        b = config.selector_book(arm)
        gens = {r["generator"] for r in db.q(
            con, "SELECT DISTINCT i.batch_id, b.generator FROM positions p "
                 "JOIN ideas i ON i.idea_uid=p.idea_uid JOIN batches b USING(batch_id) "
                 "WHERE p.book_id=?", (b,))}
        assert gens, arm
        for g in gens:
            assert g.startswith("weekly:"), g
            rid = g.split(":", 1)[1]
            assert rid in receipt["runs"].values(), (arm, g)


def test_superseded_batches_no_longer_pose_as_the_periods(con, receipt):
    assert receipt["superseded_batches"] > 0
    for d in receipt["periods"]:
        bid = ideas_mod.latest_batch(con, date.fromisoformat(d))
        if bid:
            st = db.q1(con, "SELECT status FROM batches WHERE batch_id=?", (bid,))["status"]
            assert st != "superseded", (d, bid)


def test_rerun_archives_the_rebuilt_books_again(con, receipt):
    rep2 = bf.restate_selector_books(con, end=END, verbose=False)
    assert set(rep2["archived"]) == set(receipt["archived"])
    for arm, new_id in rep2["archived"].items():
        assert new_id != receipt["archived"][arm]
