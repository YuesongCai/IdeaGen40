"""Stage C ranks only what a book can hold; the pool still shows everything.

2026-09-02: 51 of 79 candidates were shelf funds with no NAV series on this
node. Every selector ranked them, three arms chose nothing else, and booking
then dropped the lot as 「当日无价」. The pool keeps every candidate with a
`markable` flag; the selectors see the markable subset; the panel opens the
performance page on a subset that has something in it.
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

from ideagen import db, orchestrator, performance  # noqa: E402


class _State:
    def __init__(self, con):
        self.connection = con


class _P:
    def __init__(self, con):
        self.state = _State(con)


def _listed(con, key):
    db.upsert(con, "instruments", {"key": key, "name": key, "kind": "listed",
                                   "futu_code": key, "priceable": 1}, ["key"])
    for i, d in enumerate(("2026-08-31", "2026-09-01", "2026-09-02")):
        db.upsert(con, "prices", {"code": key, "d": d, "open": 100 + i, "high": 101 + i,
                                  "low": 99 + i, "close": 100 + i, "volume": 1,
                                  "src": "t"}, ["code", "d"])


class MarkableSplit(unittest.TestCase):
    def test_funds_without_a_series_are_flagged_not_selected(self):
        con = db.init(":memory:")
        _listed(con, "US.GLD")
        db.upsert(con, "instruments", {"key": "LU0001", "name": "Some Fund",
                                       "kind": "fund", "olive_key": "LU0001",
                                       "priceable": 0}, ["key"])
        from ideagen import universe as uni
        uni.hydrate(con)
        cands = [{"id": "pool:US.GLD", "instrument_id": "US.GLD"},
                 {"id": "pool:LU0001", "instrument_id": "LU0001"}]
        ok, bad = orchestrator._markable_candidates(_P(con), date(2026, 9, 2), cands, False)
        self.assertEqual(ok, {"pool:US.GLD"})
        self.assertEqual(bad, ["LU0001"])

    def test_dry_run_excludes_nothing(self):
        con = db.init(":memory:")
        cands = [{"id": "a", "instrument_id": "LU0001"}]
        ok, bad = orchestrator._markable_candidates(_P(con), date(2026, 9, 2), cands, True)
        self.assertEqual(ok, {"a"})
        self.assertEqual(bad, [])

    def test_the_whole_pool_is_still_recorded(self):
        src = (ROOT / "ideagen" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("_save_candidates(p, j.run_id, cctx, candidates_all)", src)
        self.assertIn('"B_pool.json", _blob(candidates_all)', src)


class PerfOpensOnASubsetWithSomethingInIt(unittest.TestCase):
    def test_no_live_positions_means_default_all_with_a_reason(self):
        con = db.init(":memory:")
        idx = performance.perf_index(con)
        paper = [m for m in idx["modes"] if m["mode"] == "paper"][0]
        self.assertEqual(paper["default_subset"], "all")
        self.assertTrue(paper["default_reason"])

    def test_the_page_reads_the_default_instead_of_hardcoding_live(self):
        src = (ROOT / "web" / "dash.html").read_text(encoding="utf-8")
        self.assertIn("var PERF={mode:'paper',subset:null};", src)
        self.assertIn("function perfDefaultSubset()", src)
        self.assertNotIn("var PERF={mode:'paper',subset:'live'};", src)


if __name__ == "__main__":
    unittest.main()
