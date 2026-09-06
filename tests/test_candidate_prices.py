"""Stage C sees prices for the pool, not only for the themes' indicators.

Found in the 2026-09-07 replay: `mom_21` chose zero ideas in every period
while the other eleven arms chose ten, because the live run priced only the
indicators stage A scores P from, and a candidate's `ret_21s` was never there
to rank on. The backtest context has always priced the pool.
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

from ideagen import db, orchestrator  # noqa: E402


class _State:
    def __init__(self, con):
        self.connection = con
        self.dialect = "sqlite"


class _P:
    def __init__(self, con):
        self.state = _State(con)


def _bars(con, code, n=320, start=date(2025, 8, 1)):
    from datetime import timedelta
    d = start
    px = 100.0
    rows = []
    while len(rows) < n:
        if d.weekday() < 5:
            px *= 1.0007
            rows.append({"code": code, "d": d.isoformat(), "open": px, "high": px,
                         "low": px, "close": px, "volume": 1, "src": "t"})
        d += timedelta(days=1)
    db.upsert_many(con, "prices", rows, ["code", "d"])


class PoolGetsPriced(unittest.TestCase):
    def test_candidate_codes_are_added_and_indicators_not_refetched(self):
        con = db.init(":memory:")
        _bars(con, "US.GLD")
        _bars(con, "US.SPY")
        cands = [{"instrument_id": "US.GLD", "futu_code": "US.GLD"},
                 {"instrument_id": "US.SPY", "futu_code": "US.SPY"},
                 {"instrument_id": "LU0001", "futu_code": None}]
        have = {"US.SPY": {"priced_in": 40.0}}
        view, summ = orchestrator._candidate_prices(_P(con), date(2026, 9, 2), cands,
                                                    have, dry_run=False)
        self.assertEqual(set(view), {"US.GLD"})
        self.assertIsNotNone(view["US.GLD"].get("ret_21s"))
        self.assertEqual(summ["codes"], 1)
        self.assertEqual(summ["measured"], 1)
        self.assertEqual(summ["source"], "built:prices-table")

    def test_dry_run_reads_nothing(self):
        con = db.init(":memory:")
        view, summ = orchestrator._candidate_prices(_P(con), date(2026, 9, 2),
                                                    [{"instrument_id": "US.GLD",
                                                      "futu_code": "US.GLD"}],
                                                    {}, dry_run=True)
        self.assertEqual(view, {})
        self.assertIn("skipped", summ)

    def test_the_selector_context_carries_the_extended_view(self):
        src = (ROOT / "ideagen" / "orchestrator.py").read_text(encoding="utf-8")
        i = src.index("extra, psumm = _candidate_prices(")
        self.assertIn("prices=prices)", src[i:i + 900])


if __name__ == "__main__":
    unittest.main()
