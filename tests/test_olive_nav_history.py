"""Olive's month-labelled NAV series gets dated, loaded, and makes funds bookable.

The shelf returns ~20 NAV points per month with only `month` on each point.
`date_series` assigns weekdays (back from month end for completed months,
forward from the 1st for the current one) and refuses a month with more
points than weekdays. `import_dir` loads them under an explicit `src`, never
overwriting a NAV Olive itself dated, and flips `priceable` so booking's
`_priced_only` — which now checks NAV for funds — lets the fund through.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import booking, db, universe as uni  # noqa: E402
from ideagen.sources import olive_nav  # noqa: E402

TODAY = date(2026, 9, 7)


def _payload(points):
    return {"performance": {"meta": {"asOfDate": "2026-08-31", "dataFrequency": "Monthly"},
                            "series": [{"name": "X", "nameCn": "某基金",
                                        "data": {"monthlyReturns": [], "navSeries": points}}]}}


class DateInference(unittest.TestCase):
    def test_completed_month_counts_back_from_the_last_weekday(self):
        # July 2026 has 23 weekdays; 3 points -> Jul 29, 30, 31 (Wed/Thu/Fri).
        pts = [{"month": "2026-07", "nav": "103"}, {"month": "2026-07", "nav": "102"},
               {"month": "2026-07", "nav": "101"}]
        rows, rec = olive_nav.date_series(_payload(pts), today=TODAY)
        self.assertEqual(rows, [("2026-07-29", 101.0), ("2026-07-30", 102.0), ("2026-07-31", 103.0)])
        self.assertEqual(rec["rejected"], {})

    def test_current_month_counts_forward_from_the_first(self):
        pts = [{"month": "2026-09", "nav": "115.255"}, {"month": "2026-09", "nav": "115.2"}]
        rows, _ = olive_nav.date_series(_payload(pts), today=TODAY)
        self.assertEqual(rows, [("2026-09-01", 115.2), ("2026-09-02", 115.255)])

    def test_too_many_points_for_a_month_are_refused_not_squeezed(self):
        pts = [{"month": "2026-07", "nav": str(100 + i)} for i in range(30)]
        rows, rec = olive_nav.date_series(_payload(pts), today=TODAY)
        self.assertEqual(rows, [])
        self.assertIn("2026-07", rec["rejected"])

    def test_the_longest_series_wins(self):
        p = _payload([{"month": "2026-07", "nav": "1"}])
        p["performance"]["series"].append({"name": "Y", "data": {"navSeries": [
            {"month": "2026-07", "nav": "2"}, {"month": "2026-07", "nav": "3"}]}})
        rows, rec = olive_nav.date_series(p, today=TODAY)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rec["series"], "Y")


class ImportAndBooking(unittest.TestCase):
    def test_import_flags_priceable_and_booking_lets_the_fund_through(self):
        con = db.init(":memory:")
        db.upsert(con, "instruments", {"key": "L09999", "olive_key": "L09999", "name": "F",
                                       "kind": "fund", "market": "OLIVE", "priceable": 0},
                  ["key"])
        pts = [{"month": "2026-08", "nav": str(110 - i * 0.1)} for i in range(21)] \
            + [{"month": "2026-07", "nav": str(105 - i * 0.1)} for i in range(23)]
        with TemporaryDirectory() as td:
            Path(td, "L09999.json").write_text(
                json.dumps({"result": json.dumps(_payload(pts))}), encoding="utf-8")
            Path(td, "L00000.json").write_text(
                json.dumps({"result": json.dumps(_payload([]))}), encoding="utf-8")
            rep = olive_nav.import_dir(con, Path(td), today=TODAY)
        self.assertIn("L09999", rep["loaded"])
        self.assertEqual(rep["empty"], ["L00000"])
        self.assertEqual(db.q1(con, "SELECT priceable FROM instruments WHERE key='L09999'")["priceable"], 1)
        self.assertEqual(db.q1(con, "SELECT src FROM navs WHERE olive_key='L09999' LIMIT 1")["src"],
                         olive_nav.SRC)
        uni.hydrate(con)
        ok, bad = booking._priced_only(con, [{"instrument_id": "L09999"}], date(2026, 9, 2))
        self.assertEqual((len(ok), bad), (1, []))

    def test_a_dated_nav_on_the_same_basis_is_kept(self):
        con = db.init(":memory:")
        db.upsert(con, "instruments", {"key": "L09999", "olive_key": "L09999", "name": "F",
                                       "kind": "fund", "market": "OLIVE"}, ["key"])
        db.upsert(con, "navs", {"olive_key": "L09999", "d": "2026-07-31", "nav": 104.9,
                                "src": "olive:snapshot"}, ["olive_key", "d"])
        pts = [{"month": "2026-07", "nav": str(105 - i * 0.1)} for i in range(23)]
        with TemporaryDirectory() as td:
            Path(td, "L09999.json").write_text(json.dumps(_payload(pts)), encoding="utf-8")
            rep = olive_nav.import_dir(con, Path(td), today=TODAY)
        self.assertEqual(db.q1(con, "SELECT nav FROM navs WHERE olive_key='L09999' AND d='2026-07-31'")["nav"], 104.9)
        self.assertNotIn("basis_conflicts", rep)

    def test_a_dated_nav_on_another_basis_is_replaced_and_reported(self):
        # 2026-09-07: the shelf snapshot carried 421.67 for L03244 inside a
        # series at ~15,500; kept, it was a −97% spike that stopped out three
        # books. A dated point outside the tolerance is a different basis.
        con = db.init(":memory:")
        db.upsert(con, "instruments", {"key": "L09999", "olive_key": "L09999", "name": "F",
                                       "kind": "fund", "market": "OLIVE"}, ["key"])
        db.upsert(con, "navs", {"olive_key": "L09999", "d": "2026-07-31", "nav": 4.2,
                                "src": "olive:funds"}, ["olive_key", "d"])
        pts = [{"month": "2026-07", "nav": str(105 - i * 0.1)} for i in range(23)]
        with TemporaryDirectory() as td:
            Path(td, "L09999.json").write_text(json.dumps(_payload(pts)), encoding="utf-8")
            rep = olive_nav.import_dir(con, Path(td), today=TODAY)
        row = db.q1(con, "SELECT nav, src FROM navs WHERE olive_key='L09999' AND d='2026-07-31'")
        self.assertEqual((row["nav"], row["src"]), (105.0, olive_nav.SRC))
        self.assertEqual(rep["basis_conflicts"]["L09999"][0]["snapshot"], 4.2)


if __name__ == "__main__":
    unittest.main()
