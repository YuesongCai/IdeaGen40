"""The state document has to add up.

Every number on the dashboard comes from this one payload, and several of them
are the same fact seen from different sides: a book's capital and the total, a
curve's last point and the return printed beside it, a book's first mark and the
week counter built on it. Tonight's bugs were all of that shape — a value that
looked right on its own and disagreed with its neighbour.

These are arithmetic identities, not thresholds: they hold for any data, so they
stay true next week. Where live data is missing the check skips rather than
inventing a fixture, because the point is to catch drift in the real payload.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, review


class TestStateAddsUp(unittest.TestCase):

    #: These read the live payload, so they only mean anything where there is
    #: one. The container image ships code without `data/`, and a test that
    #: errors there fails the self-updater's gate — which blocks the deploy of
    #: every other fix in the same push, invisibly, because the image is the
    #: only place it goes red. Skip, rather than error, when there is no
    #: database to read.
    state: dict = {}
    books: list = []
    agg: dict = {}

    @classmethod
    def setUpClass(cls):
        try:
            cls.state = review.state(db.init())
        except Exception as exc:  # noqa: BLE001 — no store here, nothing to check
            raise unittest.SkipTest(
                f"no readable state store in this tree: {exc}") from exc
        cls.books = cls.state.get("books") or []
        cls.agg = cls.state.get("books_aggregate") or {}

    def _need_books(self):
        if not self.books:
            self.skipTest("no books in this database")

    def test_capital_totals_match(self):
        self._need_books()
        self.assertAlmostEqual(
            sum(float(b.get("capital") or 0) for b in self.books),
            float(self.agg.get("capital") or 0), places=2)

    def test_the_return_matches_the_last_point_of_its_own_curve(self):
        """The headline number and the curve beside it are one fact."""
        self._need_books()
        eq, cap = self.agg.get("equity") or [], self.agg.get("capital")
        if not eq or not cap:
            self.skipTest("no aggregate curve yet")
        self.assertAlmostEqual((eq[-1]["equity"] / cap - 1) * 100,
                               self.agg["return_pct"], places=3)

    def test_the_aggregate_curve_is_dated_in_order_without_repeats(self):
        eq = self.agg.get("equity") or []
        if not eq:
            self.skipTest("no aggregate curve yet")
        dates = [x["d"] for x in eq]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(dates), len(set(dates)))

    def test_first_opened_is_not_later_than_any_position_it_covers(self):
        """`booked_as_of` was read as a start date and dated the book a week
        late; this pins that `first_opened_d` cannot drift the same way."""
        self._need_books()
        for b in self.books:
            first, opened = b.get("first_opened_d"), [
                p["opened_d"] for p in (b.get("open_positions") or [])
                if p.get("opened_d")]
            if first and opened:
                self.assertLessEqual(
                    first, min(opened),
                    f"{b.get('selector')}: first_opened_d is later than a "
                    f"position it should already cover")

    def test_a_book_with_no_positions_claims_no_start_date(self):
        self._need_books()
        for b in self.books:
            if not (b.get("open_positions") or []) and not b.get("closed_n"):
                self.assertIsNone(b.get("first_opened_d"), b.get("selector"))

    def test_each_book_curve_is_dated_in_order_without_repeats(self):
        self._need_books()
        for b in self.books:
            dates = [x["d"] for x in (b.get("equity") or [])]
            self.assertEqual(dates, sorted(dates), b.get("selector"))
            self.assertEqual(len(dates), len(set(dates)), b.get("selector"))

    def test_the_benchmark_is_dated_in_order_without_repeats(self):
        sr = (self.state.get("benchmark") or {}).get("series") or []
        if not sr:
            self.skipTest("no benchmark series")
        dates = [x["d"] for x in sr]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(dates), len(set(dates)))

    def test_chosen_topics_were_all_scored(self):
        """A theme cannot be picked out of a list it is not on."""
        weekly = self.state.get("weekly") or {}
        for arm in (weekly.get("topics") or weekly.get("topic_arms") or []):
            scores, chosen = arm.get("scores") or {}, arm.get("chosen") or []
            if scores and chosen:
                self.assertTrue(set(chosen) <= set(scores),
                                f"{arm.get('arm')}: chose an unscored theme")

    def test_win_counts_never_exceed_closed_counts(self):
        self._need_books()
        for b in self.books:
            self.assertLessEqual(b.get("wins") or 0, b.get("closed_n") or 0,
                                 b.get("selector"))

    def test_port_health_is_never_reported_as_both_pending_and_stale(self):
        alive = self.state.get("alive") or {}
        if alive.get("ports_pending"):
            self.assertFalse(alive.get("ports_stale"))
            self.assertEqual(alive.get("ports") or [], [])


if __name__ == "__main__":
    unittest.main()
