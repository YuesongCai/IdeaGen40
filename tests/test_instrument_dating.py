"""What `first_seen_d` has to be, pinned so nobody has to remember it.

The as-of gate in `universe.eligible` compares `first_seen_d` against the
replayed day as a *string*. That comparison is the whole reason a backfilled
period cannot pick an instrument that did not exist yet, and it is silent about
being fed anything else: a value that is not a date does not raise, it just
compares wrong and the instrument is admitted. The gate then looks like it is
working while it has stopped deciding anything.

That is not hypothetical. The Olive shelf carries a field literally named
`since` whose value is a since-inception *return* — 0.4466, 96.0045. Written
into this column it would fail open on every row, because `"0.4466" > "2026-07-29"`
is False, and the dated-row count would improve at the same time.

So these tests hold three things still:

* the gate excludes when it can and admits when it cannot know, which is the
  documented asymmetry and the reason undated rows are counted rather than
  quietly decided;
* whatever writers put in the column parses as a calendar date;
* the guard that drops the vendor's epoch sentinel stays — the guard itself,
  reached through `vendor_listing_date`, not the constant it compares against;
  asserting the constant while describing the guard left the guard unheld.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import db, universe  # noqa: E402

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _etf(key: str, first_seen: str | None) -> dict:
    row = {"instrument_id": key, "name": f"{key} ETF", "kind": "listed",
           "vehicle": "ETF", "priceable": 1}
    if first_seen is not None:
        row["first_seen_d"] = first_seen
    return row


class AsOfGate(unittest.TestCase):
    def test_excludes_an_instrument_that_did_not_exist_yet(self):
        rows = [_etf("NEW", "2026-08-15")]
        ok, why = universe.eligible(rows, as_of=date(2026, 7, 29))
        self.assertEqual(ok, [], "listed after the replayed day, must not be pickable")
        self.assertIn("NEW", why)
        self.assertIn("2026-08-15", why["NEW"])

    def test_admits_an_instrument_that_already_existed(self):
        rows = [_etf("OLD", "2020-12-02")]
        ok, _ = universe.eligible(rows, as_of=date(2026, 7, 29))
        self.assertEqual([r["instrument_id"] for r in ok], ["OLD"])

    def test_same_day_listing_is_available(self):
        # `>` not `>=`: an instrument listed on the replayed day was pickable
        # that day. Off by one here silently drops a whole cohort's first week.
        rows = [_etf("SAME", "2026-07-29")]
        ok, _ = universe.eligible(rows, as_of=date(2026, 7, 29))
        self.assertEqual([r["instrument_id"] for r in ok], ["SAME"])

    def test_undated_is_admitted_not_dropped(self):
        # The unsafe direction, on purpose: dropping every undated row would
        # empty each historical universe and read as a methodology change
        # rather than a data gap. It is admitted and counted instead.
        ok, why = universe.eligible([_etf("UNK", None)], as_of=date(2026, 7, 29))
        self.assertEqual([r["instrument_id"] for r in ok], ["UNK"])
        self.assertNotIn("UNK", why)

    def test_no_as_of_means_no_dating_gate(self):
        rows = [_etf("NEW", "2026-08-15")]
        ok, _ = universe.eligible(rows, as_of=None)
        self.assertEqual([r["instrument_id"] for r in ok], ["NEW"])

    def test_coverage_reports_the_gap_rather_than_hiding_it(self):
        cov = universe.shelf_asof_coverage(
            [_etf("A", "2020-01-01"), _etf("B", None), _etf("C", None)])
        self.assertEqual(cov, {"total": 3, "dated": 1, "undated": 2})


class ColumnHoldsDates(unittest.TestCase):
    """A non-date in this column fails open, so refuse to store one.

    The guard is at the writer: `_iso_date` drops anything that is not a plain
    calendar date instead of coercing it. These hold that behaviour rather than
    the gate's, because by the time a bad value reaches the gate it has already
    stopped deciding anything and will not say so.
    """

    def test_a_since_inception_return_is_rejected(self):
        # The exact trap. Olive's `since` is a return — 0.4466, 96.0045 — and
        # the column is compared as text, so a stored return sorts below every
        # real date and admits the row on every period.
        from ideagen.sources.olive import _iso_date
        for value in ("0.4466", 0.4466, "96.0045", 96.0045):
            with self.subTest(value=value):
                self.assertIsNone(_iso_date(value))
        self.assertFalse("0.4466" > date(2026, 7, 29).isoformat())

    def test_the_epoch_sentinel_is_dropped_at_the_writer_too(self):
        from ideagen.sources.olive import _iso_date
        self.assertIsNone(_iso_date("1970-01-01"))

    def test_a_real_date_survives_with_or_without_a_time(self):
        from ideagen.sources.olive import _iso_date
        self.assertEqual(_iso_date("2024-04-25"), "2024-04-25")
        self.assertEqual(_iso_date("2024-04-25T09:30:00Z"), "2024-04-25")

    def test_missing_and_malformed_are_nothing_rather_than_a_guess(self):
        from ideagen.sources.olive import _iso_date
        for value in (None, "", "--", "2024/04/25", "2024-13-01", "yesterday"):
            with self.subTest(value=value):
                self.assertIsNone(_iso_date(value))

    def test_every_stored_first_seen_d_parses_as_a_calendar_date(self):
        """The same invariant against what is actually on disk.

        An in-memory database has no rows, so scanning one would pass without
        looking at anything — the shape of a guard with none of the effect. This
        reads the project database when it is there, asserts it actually found
        rows, and skips out loud when it is not.
        """
        from ideagen import config
        path = Path(config.DATA) / "ideagen.db"
        if not path.exists():
            self.skipTest(f"no project database at {path}")
        import sqlite3
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = list(con.execute(
                "SELECT key, first_seen_d FROM instruments "
                "WHERE first_seen_d IS NOT NULL AND first_seen_d<>''"))
        finally:
            con.close()
        if not rows:
            self.skipTest("no dated instruments yet — nothing to check")
        for r in rows:
            value = str(r["first_seen_d"])
            with self.subTest(instrument=r["key"], value=value):
                self.assertRegex(value, ISO)
                datetime.strptime(value, "%Y-%m-%d")


class VendorSentinel(unittest.TestCase):
    """The guard, not the constant.

    This class asserted `_EPOCH_SENTINEL == "1970-01-01"` and said in its
    docstring that it held the guard. Deleting the guard from `listing_dates`
    and leaving the constant untouched kept all twelve tests green — the
    sentinel would have been written into `first_seen_d` and passed every
    as-of gate, with nothing red. The filter now lives in a function a test can
    call, which is the only reason these assertions mean anything.
    """

    def test_the_sentinel_is_refused_by_the_filter_itself(self):
        # OpenD returns 1970-01-01 for every US ETF it has no listing date for.
        from ideagen.sources import futu_px
        self.assertIsNone(futu_px.vendor_listing_date("1970-01-01"))
        self.assertIsNone(futu_px.vendor_listing_date("1970-01-01 00:00:00"))

    def test_a_real_vendor_date_survives_the_filter(self):
        # Without this the filter could refuse everything and still pass above.
        from ideagen.sources import futu_px
        self.assertEqual(futu_px.vendor_listing_date("2020-12-02"), "2020-12-02")
        self.assertEqual(
            futu_px.vendor_listing_date("2019-05-08 09:30:00"), "2019-05-08")

    def test_empty_and_junk_are_no_answer_rather_than_a_guess(self):
        from ideagen.sources import futu_px
        for raw in (None, "", "N/A", "--", 0):
            with self.subTest(raw=raw):
                self.assertIsNone(futu_px.vendor_listing_date(raw))

    def test_why_it_must_never_reach_the_column(self):
        # Stated as the consequence: a 1970 date is indistinguishable from a
        # real one to the gate, so it is admitted on every period.
        from ideagen.sources import futu_px
        ok, _ = universe.eligible(
            [_etf("SENTINEL", futu_px._EPOCH_SENTINEL)], as_of=date(2026, 7, 29))
        self.assertEqual(len(ok), 1, "a 1970 date passes any gate — which is "
                                     "why the filter above must refuse it")


if __name__ == "__main__":
    unittest.main()
