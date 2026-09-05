"""What leaves the shelf has to be written down, or it never happened.

A snapshot records membership. Nothing recorded non-membership, so a fund
dropped from the shelf did not become "gone since September" — it became a fund
that had never been there, retroactively, for every past period as well. Replays
then pick from a universe the week did not have, and past numbers improve
quietly, because what gets pulled from a shelf is rarely what was working. That
is the first of the seven sins in the only form this system can commit, and the
information exists for exactly one moment: when two consecutive snapshots
differ.

These tests pin that moment, and the two ways it is usually lost — a comeback
deleted rather than dated, and a fixture differenced against the live shelf.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ideagen import db, schema, shelf_store
from ideagen.platform.base import Platform, Unavailable
from ideagen.platform.local import (FileCache, FileEventBus, LocalBlobStore,
                                    SqliteStateStore)

LIVE = shelf_store.LIVE_SOURCE


def _platform(root: Path) -> Platform:
    return Platform(
        name="test",
        blobs=LocalBlobStore(root / "blobs"),
        state=SqliteStateStore(root / "state.db"),
        inference=Unavailable("inference", "not used"),
        events=FileEventBus(root / "events.jsonl"),
        cache=FileCache(root / "cache"),
        secrets=Unavailable("secrets", "not used"),
    )


def _payload(codes):
    return {"funds": [{"productCode": c, "productName": f"FUND {c}",
                       "currency": "USD", "strategy": "多资产",
                       "latestNav": 100.0, "navDate": "2026-09-01",
                       "riskLevel": "R3"} for c in codes]}


class Departures(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.p = _platform(self.root)

    def _snap(self, codes, as_of, source=LIVE):
        return shelf_store.persist(
            self.p, _payload(codes), as_of=as_of, source=source,
            classification=shelf_store.LIVE_CLASSIFICATION)

    def _q(self, sql, args=()):
        return self.p.state.q(sql, args)

    def test_a_product_missing_from_the_next_snapshot_is_recorded_as_gone(self):
        self._snap(["A", "B", "C"], date(2026, 9, 1))
        receipt = self._snap(["A", "C"], date(2026, 9, 8))
        self.assertEqual(receipt["departed"], ["B"])
        gone = shelf_store.departed_by(self._q, date(2026, 9, 8))
        self.assertEqual(gone, {"B": "2026-09-08"})

    def test_the_first_snapshot_invents_no_departures(self):
        """Nothing to difference against. A first capture that reported the
        whole shelf as departed would poison every gate downstream."""
        receipt = self._snap(["A", "B"], date(2026, 9, 1))
        self.assertEqual(receipt["departed"], [])
        self.assertEqual(shelf_store.departed_by(self._q, date(2026, 9, 1)), {})

    def test_a_period_before_the_departure_still_sees_the_product(self):
        """The whole point. `B` was on the shelf in the first week and must stay
        available to a replay of that week, and only that week."""
        self._snap(["A", "B"], date(2026, 9, 1))
        self._snap(["A"], date(2026, 9, 8))
        self.assertEqual(shelf_store.departed_by(self._q, date(2026, 9, 1)), {})
        self.assertIn("B", shelf_store.departed_by(self._q, date(2026, 9, 8)))

    def test_a_comeback_is_dated_not_deleted(self):
        """'Left in September and returned in October' and 'never left' are
        different facts about the same instrument, and a replay of the gap needs
        to tell them apart. Deleting the row on return erases the difference."""
        self._snap(["A", "B"], date(2026, 9, 1))
        self._snap(["A"], date(2026, 9, 8))
        receipt = self._snap(["A", "B"], date(2026, 9, 15))
        self.assertEqual(receipt["returned"], ["B"])
        rows = self._q("SELECT * FROM shelf_departures WHERE instrument_id='B'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["returned_as_of"], "2026-09-15")
        # Gone during the gap, back afterwards.
        self.assertIn("B", shelf_store.departed_by(self._q, date(2026, 9, 10)))
        self.assertNotIn("B", shelf_store.departed_by(self._q, date(2026, 9, 20)))

    def test_a_fixture_is_not_differenced_against_the_live_shelf(self):
        """Two sources are two universes. Differencing them would report the
        entire live shelf as departed the first time anyone loaded the public
        fixture — a hundred fabricated departures, every one of which would then
        exclude a real instrument from every later replay."""
        self._snap(["A", "B", "C"], date(2026, 9, 1), source=LIVE)
        receipt = self._snap(["X"], date(2026, 9, 2),
                             source=shelf_store.PUBLIC_FIXTURE_SOURCE)
        self.assertEqual(receipt["departed"], [])
        self.assertEqual(shelf_store.departed_by(self._q, date(2026, 9, 2)), {})

    def test_survivorship_says_point_in_time_is_impossible_with_one_snapshot(self):
        """The honest report today. One snapshot means every earlier period is
        replayed on the shelf as it looks now, and that has to be stated where
        the backtest is read rather than left to be inferred."""
        self._snap(["A", "B"], date(2026, 9, 1))
        one = shelf_store.survivorship(self._q)
        self.assertEqual(one["snapshots"], 1)
        self.assertIsNone(one["point_in_time_from"])
        self._snap(["A"], date(2026, 9, 8))
        two = shelf_store.survivorship(self._q)
        self.assertEqual(two["point_in_time_from"], "2026-09-01")
        self.assertEqual(two["departures_open"], 1)


class ReplayGate(unittest.TestCase):
    """The gate reads the ledger — the half that was missing entirely."""

    def test_a_departed_instrument_is_dropped_from_a_later_replay(self):
        import json
        from ideagen import backtest
        con = db.init(Path(tempfile.mkdtemp()) / "t.db")
        for key in ("KEEP", "GONE"):
            con.execute(
                "INSERT INTO instruments(key,kind,name,market,currency,"
                "priceable,meta,first_seen_d) VALUES(?,?,?,?,?,?,?,?)",
                (key, "listed", key, "US", "USD", 1,
                 json.dumps({"vehicle": "ETF"}), "2020-01-01"))
        schema.upsert(SimpleNamespace(q=lambda s, a=(): db.q(con, s, a),
                                      execute=lambda s, a=(): con.execute(s, a),
                                      dialect="sqlite"),
                      "shelf_departures", {
                          "instrument_id": "GONE", "source": LIVE,
                          "departed_as_of": "2026-08-01",
                          "last_seen_as_of": "2026-07-25",
                          "last_snapshot_id": "snap-1", "name": "GONE",
                          "returned_as_of": None, "noticed_at": "2026-08-01"})

        before = backtest.context_for(con, date(2026, 7, 29))
        after = backtest.context_for(con, date(2026, 8, 19))
        self.assertEqual({u["instrument_id"] for u in before.universe},
                         {"KEEP", "GONE"})
        self.assertEqual({u["instrument_id"] for u in after.universe}, {"KEEP"})
        self.assertEqual(
            after.params[backtest.CTX_TAG].universe_dropped_as_departed, 1)
