"""A daily shelf snapshot cannot put a different-basis NAV into a fund's series.

The history loader (`olive_nav.import_dir`) learned this on 2026-09-07 — one
snapshot point at 421.67 inside a series at ~14,750 stopped three books out
at −97%. The daily leg (`olive.ingest`) writes the same table every day, so
it gets the same guard: a snapshot NAV more than BASIS_TOLERANCE away from
the nearest stored series point is dropped and reported, never merged.
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

from ideagen import db  # noqa: E402
from ideagen.sources import olive, olive_nav  # noqa: E402


def _series(con, key, base=15000.0):
    rows = [{"olive_key": key, "d": f"2026-08-{d:02d}", "nav": base + d, "src": olive_nav.SRC}
            for d in (24, 25, 26, 27, 28)]
    db.upsert_many(con, "navs", rows, ["olive_key", "d"])


class SnapshotBasisGuard(unittest.TestCase):
    def test_a_far_off_snapshot_nav_is_dropped_and_named(self):
        con = db.init(":memory:")
        _series(con, "L09999")
        rows = [{"olive_key": "L09999", "d": "2026-08-31", "nav": 421.67, "src": "olive:funds"}]
        keep, conflicts = olive._same_basis_only(con, rows)
        self.assertEqual(keep, [])
        self.assertEqual(conflicts["L09999"]["series"], 15028.0)

    def test_a_snapshot_on_the_same_basis_goes_in(self):
        con = db.init(":memory:")
        _series(con, "L09999")
        rows = [{"olive_key": "L09999", "d": "2026-08-31", "nav": 15100.0, "src": "olive:funds"}]
        keep, conflicts = olive._same_basis_only(con, rows)
        self.assertEqual(len(keep), 1)
        self.assertEqual(conflicts, {})

    def test_a_fund_with_no_series_yet_is_not_judged(self):
        con = db.init(":memory:")
        rows = [{"olive_key": "L00001", "d": "2026-08-31", "nav": 4.2, "src": "olive:funds"}]
        keep, conflicts = olive._same_basis_only(con, rows)
        self.assertEqual(len(keep), 1)


if __name__ == "__main__":
    unittest.main()
