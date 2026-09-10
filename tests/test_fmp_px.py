"""FMP EOD prices are the gateway-free source that lets marking run with the Mac
off. This guards the two things that made "Mac off = nothing marks" a bug:

  1. code → FMP symbol mapping (US strips the prefix, HK becomes NNNN.HK, funds
     return None and stay on the NAV path), and
  2. `sync` gap-fills — it adds only the dates after a code's latest stored bar,
     so an FMP bar never overwrites one OpenD wrote and a series does not jump
     where the source changed.

`fmp._get` is stubbed so the test never touches the network.
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
from ideagen.sources import fmp, fmp_px  # noqa: E402


class SymbolMap(unittest.TestCase):
    def test_us_strips_prefix(self):
        self.assertEqual(fmp_px._fmp_symbol("US.CIBR"), "CIBR")

    def test_hk_becomes_four_digit_dot_hk(self):
        self.assertEqual(fmp_px._fmp_symbol("HK.02800"), "2800.HK")
        self.assertEqual(fmp_px._fmp_symbol("HK.00700"), "0700.HK")

    def test_funds_and_isin_have_no_fmp_symbol(self):
        for k in ("L03248", "F02865", "HK0000584752", ""):
            self.assertIsNone(fmp_px._fmp_symbol(k))


class SyncGapFill(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, dict]] = []

        def fake_get(path, **params):
            self.calls.append((params.get("symbol"), params))
            frm = params["from"]
            # Two sessions of adjusted bars; the caller clamps to the last
            # closed one, so returning a bit extra is fine.
            rows = [
                {"date": "2026-09-08", "adjOpen": 10, "adjHigh": 11,
                 "adjLow": 9, "adjClose": 10.5, "volume": 100},
                {"date": "2026-09-09", "adjOpen": 10.5, "adjHigh": 12,
                 "adjLow": 10, "adjClose": 11.0, "volume": 120},
            ]
            return [r for r in rows if r["date"] >= frm]

        self._orig = fmp._get
        fmp._get = fake_get
        # complete_through is wall-clock; pin the clamp so the test is stable.
        self._orig_ct = fmp_px.futu_px.complete_through
        fmp_px.futu_px.complete_through = lambda market="US": "2026-09-09"

    def tearDown(self):
        fmp._get = self._orig
        fmp_px.futu_px.complete_through = self._orig_ct

    def test_a_fresh_code_is_fetched_from_the_requested_start(self):
        con = db.init(":memory:")
        rep = fmp_px.sync(con, ["US.CIBR"], date(2026, 9, 1), date(2026, 9, 9))
        self.assertEqual(rep["source"], "fmp")
        self.assertEqual(rep["fetched"], 1)
        got = db.q1(con, "SELECT close, src FROM prices WHERE code='US.CIBR' AND d='2026-09-09'")
        self.assertEqual((got["close"], got["src"]), (11.0, fmp_px.SRC))
        self.assertEqual(self.calls[0][1]["from"], "2026-09-01")

    def test_an_existing_bar_from_another_source_is_not_overwritten(self):
        con = db.init(":memory:")
        db.upsert(con, "prices", {"code": "US.CIBR", "d": "2026-09-08",
                                  "close": 99.0, "src": "futu:qfq"}, ["code", "d"])
        fmp_px.sync(con, ["US.CIBR"], date(2026, 9, 1), date(2026, 9, 9))
        kept = db.q1(con, "SELECT close, src FROM prices WHERE code='US.CIBR' AND d='2026-09-08'")
        self.assertEqual((kept["close"], kept["src"]), (99.0, "futu:qfq"))  # untouched
        added = db.q1(con, "SELECT close FROM prices WHERE code='US.CIBR' AND d='2026-09-09'")
        self.assertEqual(added["close"], 11.0)                              # gap-filled
        self.assertEqual(self.calls[0][1]["from"], "2026-09-09")            # only the gap

    def test_a_code_already_complete_is_not_fetched(self):
        con = db.init(":memory:")
        db.upsert(con, "prices", {"code": "US.CIBR", "d": "2026-09-09",
                                  "close": 11.0, "src": "futu:qfq"}, ["code", "d"])
        rep = fmp_px.sync(con, ["US.CIBR"], date(2026, 9, 8), date(2026, 9, 9))
        self.assertEqual(rep["fetched"], 0)
        self.assertEqual(self.calls, [])

    def test_a_fund_is_skipped_not_errored(self):
        con = db.init(":memory:")
        rep = fmp_px.sync(con, ["L03248"], date(2026, 9, 1), date(2026, 9, 9))
        self.assertEqual(rep["fetched"], 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
