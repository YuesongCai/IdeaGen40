"""The mandate gate must judge a row on the row, not on process state.

`universe.eligible` refuses an instrument whose vehicle it cannot establish. That
refusal used to depend on whether some earlier call in the same process had run
`universe.hydrate` — a module-level global. In a long-lived scheduler one usually
had; in a cold `ideagen weekly` none had, and every fund on the shelf was refused
as「载体未确认」with no error anywhere. The universe was 220 instruments or 86
depending on import order, and both runs recorded `ok`.

So the load-bearing test here is the *cold* one: build the feed rows in a process
that has not hydrated, and require the funds through. A test that hydrates first
would pass against the bug.

The feed opens its own connection through `config.DB_PATH`, so an in-memory handle
is not enough to isolate it — these tests point that constant at a temp file. A
test that skipped that step would silently read the production shelf and pass for
the wrong reason.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ideagen import config, db, feeds, universe as uni

AS_OF = date(2026, 9, 5)

#: One row per stored encoding the shelf actually contains, plus one that is
#: genuinely silent. `MYSTERY` is the control: the fix must not make the gate
#: admit a product whose dealing terms nothing has established.
FIXTURE = [
    ("SPY", "listed", "US", {"exposure": "美国大盘股", "vehicle": "ETF"}, 1, "US.SPY"),
    ("L03028", "fund", "OLIVE", {"group": "funds", "risk_level": "中风险"}, 0, None),
    ("W01350", "fund", "OLIVE", {"group": "private"}, 0, None),
    ("JANUS-BIOTECH", "fund", "US", {"exposure": "生物科技", "vehicle": "公募"}, 0, None),
    ("MYSTERY", "fund", "OLIVE", {}, 0, None),
]


@contextlib.contextmanager
def shelf(rows=FIXTURE):
    """A database holding exactly `rows`, with the feed pointed at it."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        with mock.patch.object(config, "DB_PATH", path):
            con = db.init(path)
            con.execute("DELETE FROM instruments")
            for key, kind, market, meta, priceable, futu in rows:
                db.upsert(con, "instruments", {
                    "key": key, "kind": kind, "market": market, "name": key,
                    "futu_code": futu,
                    "olive_key": key if kind == "fund" else None,
                    "currency": "USD", "priceable": priceable,
                    "meta": json.dumps(meta),
                    "updated_at": "2026-09-05T00:00:00+08:00",
                }, ("key",))
            con.commit()
            try:
                yield con
            finally:
                con.close()


def feed_rows():
    res = feeds.fetch("instruments", AS_OF)
    if not res.ok:
        raise AssertionError(res.error)
    return res.rows


class VehicleOfMeta(unittest.TestCase):
    """Both stored encodings resolve; silence stays silent."""

    def test_explicit_vehicle_wins(self):
        self.assertEqual(uni.vehicle_of({"vehicle": "ETF"}), "ETF")

    def test_shelf_group_is_translated(self):
        # What the Olive sync lands: the shelf's own grouping, no `vehicle` key.
        self.assertEqual(uni.vehicle_of({"group": "funds"}), "公募")
        self.assertEqual(uni.vehicle_of({"group": "private"}), "私募")
        self.assertEqual(uni.vehicle_of({"asset_class": "MM"}), "现金")

    def test_silent_row_returns_empty_not_a_guess(self):
        # `_vehicle_for` answers 「公募」 for a row with no group, which in this
        # position means "deals daily" — a claim nothing in the row supports.
        # The gate is built to refuse an unknown vehicle; it can only do that if
        # the unknown reaches it.
        self.assertEqual(uni.vehicle_of({}), "")
        self.assertEqual(uni.vehicle_of({"exposure": "美国大盘股"}), "")


class FeedCarriesVehicle(unittest.TestCase):
    def test_every_stored_encoding_reaches_the_row(self):
        # Exercise the registered feed, not a hand-built dict: the bug was that
        # the feed dropped a field it already had, which only this path shows.
        with shelf():
            rows = {r["instrument_id"]: r for r in feed_rows()}
        self.assertEqual(rows["SPY"]["vehicle"], "ETF")
        self.assertEqual(rows["L03028"]["vehicle"], "公募")
        self.assertEqual(rows["W01350"]["vehicle"], "私募")
        self.assertEqual(rows["JANUS-BIOTECH"]["vehicle"], "公募")
        self.assertEqual(rows["MYSTERY"]["vehicle"], "")

    def test_vehicle_is_declared_recommended(self):
        # Otherwise a future universe feed can drop it again and the omission
        # reads as a shelf of unconfirmed products rather than as a missing field.
        self.assertIn("vehicle", feeds.RECOMMENDED["universe"])
        with shelf():
            res = feeds.fetch("instruments", AS_OF)
        self.assertNotIn("vehicle", res.meta["recommended_missing"])


class GateDoesNotDependOnHydrate(unittest.TestCase):
    """The regression itself: a cold process must admit the funds."""

    def test_funds_survive_the_gate_without_hydrate(self):
        # No `uni.hydrate()` anywhere in this test — that is the point.
        with shelf():
            ok, why = uni.eligible(feed_rows())
        admitted = {r["instrument_id"] for r in ok}
        self.assertIn("L03028", admitted)
        self.assertIn("JANUS-BIOTECH", admitted)
        self.assertTrue(any(r["kind"] == "fund" for r in ok),
                        f"no fund admitted in a cold process; excluded: {why}")

    def test_a_row_that_says_nothing_is_still_refused(self):
        # The fix must not turn the gate into a rubber stamp: an unknown vehicle
        # is exactly what it exists to catch.
        with shelf():
            _, why = uni.eligible(feed_rows())
        self.assertIn("MYSTERY", why)
        self.assertIn("载体未确认", why["MYSTERY"])

    def test_private_without_daily_dealing_is_still_refused(self):
        # The other half of the mandate. A 私募 row now reaches the gate carrying
        # its vehicle, which must mean "judged and refused", not "admitted".
        with shelf():
            _, why = uni.eligible(feed_rows())
        self.assertIn("W01350", why)
        self.assertIn("日度申赎", why["W01350"])


class HydrateCoversEveryFund(unittest.TestCase):
    """`resolve()` needs the registry, and the registry was scoped by ingest path."""

    def setUp(self):
        self._all, self._by = list(uni.ALL), dict(uni.BY_KEY)

    def tearDown(self):
        uni.ALL[:] = self._all
        uni.BY_KEY.clear()
        uni.BY_KEY.update(self._by)

    def test_a_fund_stored_under_its_listing_market_still_resolves(self):
        with shelf() as con:
            uni.hydrate(con)
        hit = uni.resolve("JANUS-BIOTECH")
        self.assertIsNotNone(hit, "fund with market='US' never entered the registry")
        self.assertEqual(hit.vehicle, "公募")
        self.assertEqual(hit.market, "US", "the row's own market must survive")

    def test_shelf_rows_keep_resolving(self):
        with shelf() as con:
            uni.hydrate(con)
        hit = uni.resolve("L03028")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.vehicle, "公募")

    def test_registry_and_feed_agree_on_every_row(self):
        # The two readings of one `meta` must not be able to disagree. A row the
        # registry calls 「公募」 while the feed calls it unknown is the same bug
        # this module exists to close, one layer up.
        with shelf() as con:
            rows = {r["instrument_id"]: r["vehicle"] for r in feed_rows()}
            uni.hydrate(con)
        for key, from_feed in rows.items():
            hit = uni.BY_KEY.get(key)
            if hit is None:            # listed rows are frozen in source
                continue
            self.assertEqual(hit.vehicle, from_feed,
                             f"{key}: registry says {hit.vehicle!r}, "
                             f"feed says {from_feed!r}")


if __name__ == "__main__":
    unittest.main()
