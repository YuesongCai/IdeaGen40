"""Wednesday decision-day research is cut at 07:00 HKT, other days are not.

WS-A's cutoff audit (2026-09-17) found replays of a weekly period reading the
whole Wednesday: ~20% of their documents were published after the 07:00 HKT
decision a live run is made at. The feed now drops them; earlier days of the
window are entirely before the decision and stay whole.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config  # noqa: E402


class ResearchCutoff(unittest.TestCase):
    def test_wednesday_is_cut_at_seven(self):
        c = config.research_cutoff_iso(date(2026, 9, 16))
        self.assertEqual(c, "2026-09-16T07:00:00+08:00")
        self.assertTrue("2026-09-16T06:59:59+08:00" <= c)
        self.assertFalse("2026-09-16T07:00:01+08:00" <= c)
        self.assertTrue("2026-09-15T23:00:00+08:00" <= c)

    def test_other_days_admit_the_whole_day(self):
        for d in ("2026-09-15", "2026-09-17", date(2026, 9, 13)):
            self.assertTrue("2026-09-17T23:59:59+08:00" <= config.research_cutoff_iso(d))


if __name__ == "__main__":
    unittest.main()
