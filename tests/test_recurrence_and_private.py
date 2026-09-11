"""Two feedback-driven guards (Yifu/Jon 2026-09-11):

  1. Recurrence discount — a narrative the market has chewed on for consecutive
     weeks is worth a little less, but gently: capped, and RESET after an absence
     so a re-emergence is a fresh cycle, not more of the same.
  2. Private-fund exclusion — a private / actively-managed fund is a manager's
     view, not a fixed exposure to a direction-neutral theme, so it is kept out
     of the pool the selectors compete over. Narrow: only vehicles that lead with
     私募 are dropped; a public wrapper stays.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config, db, scoring  # noqa: E402
from ideagen import orchestrator as orch  # noqa: E402


def _seed_theme(con, theme_id, dates, tier="core"):
    for d in dates:
        db.upsert(con, "themes", {"as_of": d, "theme_id": theme_id, "label": theme_id,
                                  "tis": 80.0, "tier": tier}, ["as_of", "theme_id"])


class Recurrence(unittest.TestCase):
    def test_first_appearance_is_not_discounted(self):
        con = db.init(":memory:")
        r = scoring.recurrence(con, "NEW", date(2026, 9, 9))
        self.assertEqual((r["consec"], r["discount"]), (0, 0.0))
        self.assertEqual(r["occurrence"], 1)

    def test_consecutive_weeks_ramp_then_cap(self):
        con = db.init(":memory:")
        # 6 consecutive Wednesdays before 2026-09-09
        weeks = ["2026-07-29", "2026-08-05", "2026-08-12", "2026-08-19",
                 "2026-08-26", "2026-09-02"]
        _seed_theme(con, "DRONE", weeks)
        r = scoring.recurrence(con, "DRONE", date(2026, 9, 9))
        self.assertEqual(r["consec"], 6)
        # 6 * 2.5 = 15 → capped at RECUR_DISCOUNT_MAX (12)
        self.assertEqual(r["discount"], config.RECUR_DISCOUNT_MAX)

    def test_a_gap_resets_to_a_fresh_cycle(self):
        con = db.init(":memory:")
        # last seen 2026-07-01, then absent for months → returning is fresh
        _seed_theme(con, "REBORN", ["2026-06-24", "2026-07-01"])
        r = scoring.recurrence(con, "REBORN", date(2026, 9, 9))
        self.assertEqual(r["discount"], 0.0)
        self.assertGreaterEqual(r["weeks_since"], config.RECUR_RESET_GAP_WEEKS)

    def test_only_strong_prior_weeks_count(self):
        con = db.init(":memory:")
        # a week where it was only 'watch' does not count as a prior strong week
        _seed_theme(con, "WEAKISH", ["2026-09-02"], tier="watch")
        r = scoring.recurrence(con, "WEAKISH", date(2026, 9, 9))
        self.assertEqual(r["consec"], 0)


class PrivateExclusion(unittest.TestCase):
    def test_pure_private_vehicles_are_excluded(self):
        for v in ("私募", "私募策略", "私募 / UCITS"):
            self.assertTrue(orch._is_private_vehicle({"vehicle": v}), v)

    def test_public_and_mixed_wrappers_stay(self):
        for v in ("公募", "ETF", "股票", "现金", "公募 / 私募", "公募 / UCITS", None):
            self.assertFalse(orch._is_private_vehicle({"vehicle": v}), v)

    def test_the_switch_keeps_everything(self):
        saved = config.INCLUDE_PRIVATE_FUNDS
        try:
            config.INCLUDE_PRIVATE_FUNDS = True
            self.assertFalse(orch._is_private_vehicle({"vehicle": "私募"}))
        finally:
            config.INCLUDE_PRIVATE_FUNDS = saved


if __name__ == "__main__":
    unittest.main()
