"""A price we cannot refresh is not a price of zero.

2026-09-19. The Olive connector had been failing since 09-09; fund NAVs went
past `olive.MAX_NAV_STALE_DAYS`, `paper.mark_price` started answering None, and
the valuation loop in `paper.run` skipped those positions — which valued them at
zero. Every book's NAV fell off a cliff no position had taken: the panel headline
read −10.72% where the honest number was +0.33%, and the worst arm reported
−28.1% while its own holdings averaged −0.68%. The arm *ordering* was wrong too,
because it was decided by which arm happened to hold more funds.

Two separate things are pinned here, because fixing one without the other is how
the bug comes back:

* Valuation carries the last known mark and says how much of the NAV is carried
  that way (`mv_stale` / `n_stale`).
* Trading does **not**. A stale NAV must still refuse to fill an order — the
  refusal was right for that question and only wrong for valuation.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")
os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, paper  # noqa: E402
from ideagen.sources import olive  # noqa: E402

#: The NAV is observed here and never again, so by `LATE` it is well past the
#: staleness bar and `olive.mark` reports `usable` False.
NAV_D = "2026-09-09"
LATE = "2026-09-30"
FUND = {"idea_uid": "u1", "instrument": "fund", "futu_code": None,
        "olive_key": "LU0001", "tool": "LU0001"}


def _fund_with_one_nav(con):
    db.upsert(con, "instruments", {"key": "LU0001", "name": "Some Fund",
                                   "kind": "fund", "olive_key": "LU0001",
                                   "currency": "USD", "priceable": 1}, ["key"])
    db.upsert(con, "navs", {"olive_key": "LU0001", "d": NAV_D, "nav": 100.0},
              ["olive_key", "d"])


class StaleFundMarks(unittest.TestCase):

    def test_the_fixture_really_is_stale(self):
        """Guard the premise: if this stops being stale the rest proves nothing."""
        con = db.init(":memory:")
        _fund_with_one_nav(con)
        m = olive.mark(con, "LU0001", LATE)
        self.assertIsNotNone(m)
        self.assertGreater(m["stale_days"], olive.MAX_NAV_STALE_DAYS)
        self.assertFalse(m["usable"])

    def test_valuation_carries_the_last_nav_rather_than_dropping_it(self):
        con = db.init(":memory:")
        _fund_with_one_nav(con)
        m = paper.carry_mark(con, FUND, LATE)
        self.assertIsNotNone(m, "a holding with a known NAV must still be valued")
        self.assertEqual(m["px"], 100.0)
        self.assertEqual(m["d"], NAV_D)
        self.assertFalse(m["fresh"], "and it must say the mark is carried")

    def test_trading_still_refuses_a_stale_nav(self):
        """The refusal `mark_price` makes is the right answer to a different
        question, and must survive the valuation fix untouched."""
        con = db.init(":memory:")
        _fund_with_one_nav(con)
        self.assertIsNone(paper.mark_price(con, FUND, LATE))

    def test_a_fresh_nav_is_marked_fresh(self):
        con = db.init(":memory:")
        _fund_with_one_nav(con)
        m = paper.carry_mark(con, FUND, NAV_D)
        self.assertEqual(m["px"], 100.0)
        self.assertTrue(m["fresh"])

    def test_an_instrument_with_no_price_at_all_is_still_none(self):
        """Carrying forward is not inventing: nothing known stays nothing."""
        con = db.init(":memory:")
        self.assertIsNone(paper.carry_mark(con, FUND, LATE))

    def test_the_equity_row_can_record_how_much_is_carried(self):
        """Without these columns the disclosure has nowhere to land, and the
        next reader cannot tell a carried NAV from a live one."""
        con = db.init(":memory:")
        cols = {r[1] for r in con.execute("PRAGMA table_info(equity)")}
        self.assertLessEqual({"mv_stale", "n_stale", "n_unpriced"}, cols)


class EvidenceWindowSaysWhenItIsBehind(unittest.TestCase):
    """The counter that reads 0 because nothing ran, and the counter that reads
    0 because the loop stopped, are the same two characters on the page. On
    2026-09-19 the second one had been true for 12 days. `lag` is what makes
    them different."""

    @staticmethod
    def _con_with(periods, study_end):
        con = db.init(":memory:")
        for i, d in enumerate(periods):
            db.upsert(con, "orch_runs", {"run_id": f"r{i}", "as_of": d,
                                         "kind": "weekly", "ok": 1}, ["run_id"])
        if study_end:
            db.upsert(con, "backtest_runs", {
                "backtest_id": "bt-real-x", "as_of": study_end,
                "window_start": periods[0], "window_end": study_end,
                "methodology": "real-pool-asof-replay/v1", "ok": 1,
                "data_classification": "mixed-live-backfill", "inputs_sha": "x",
                "summary": "{}"}, ["backtest_id"])
        return con

    def test_level_when_the_window_reaches_the_newest_period(self):
        from ideagen import evidence_sync
        con = self._con_with(["2026-09-02", "2026-09-09"], "2026-09-09")
        lag = evidence_sync.lag(con)
        self.assertEqual(lag["studies"]["study"]["behind"], 0)

    def test_behind_by_the_periods_it_never_saw(self):
        from ideagen import evidence_sync
        con = self._con_with(["2026-09-02", "2026-09-09", "2026-09-16"], "2026-09-02")
        lag = evidence_sync.lag(con)
        self.assertEqual(lag["studies"]["study"]["behind"], 2)
        # The top-level number is the worst of the studies, not this one's:
        # a badge that warns only when *every* replay has stopped would stay
        # quiet through exactly the case this exists to catch.
        self.assertGreaterEqual(lag["behind"], 2)
        self.assertFalse(lag["level"])

    def test_a_study_that_never_ran_is_behind_every_period(self):
        from ideagen import evidence_sync
        con = self._con_with(["2026-09-02", "2026-09-09"], None)
        self.assertEqual(evidence_sync.lag(con)["studies"]["study"]["behind"], 2)


if __name__ == "__main__":
    unittest.main()
