"""A formal-backtest batch must be invisible to everything that reads by period.

`backtest_formal` books its replay through the real `ideas` / `batches` /
`orders` tables so the paper rules cannot drift — that is the point of it. But
half the codebase selects those tables **by `as_of`** rather than by
`batch_id`: the stock-picking study's candidate pool, the latest-batch lookup
the report reads, the monitor's 「今天这批」, the cohort opener in `daily`.
Every one of those would silently absorb a `BT-` batch as if the morning had
written it: the study's pool would double the very candidates each arm chose
(and call that convergence), and `daily` would open a cohort book for a batch
that was never a decision.

The books are already fenced (`paper.all_books` drops `bt:`); this fences the
batches. The rule is one string — `batch_id NOT LIKE 'BT-%'` — and the second
test pins it to the constant the engine actually uses, because a prefix renamed
in `config` without these queries following would re-open every hole at once
while every test that builds its own batch stayed green.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import backtest, config, db, ideas as ideas_mod  # noqa: E402

READERS = ("backtest.py", "ideas.py", "payload.py", "monitor.py", "scoring.py",
           "briefing.py", "replay.py", "cli.py", "analytics.py", "review.py",
           "performance.py")


class TheFenceIsOneString(unittest.TestCase):
    def test_every_by_period_reader_excludes_the_prefix(self):
        want = f"NOT LIKE '{config.BACKTEST_BATCH_PREFIX}%'"
        for name in READERS:
            src = (ROOT / "ideagen" / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertIn(want, src)

    def test_the_prefix_is_the_one_the_engine_writes(self):
        src = (ROOT / "ideagen" / "backtest_formal.py").read_text(encoding="utf-8")
        self.assertIn("config.BACKTEST_BATCH_PREFIX", src)
        self.assertEqual(config.BACKTEST_BATCH_PREFIX, "BT-")


def _batch(con, batch_id, as_of):
    payload = {"ideas": [{
        "id": 1, "instrument_key": "US.SPY", "tool": "US.SPY",
        "theme_id": "AI-CAPEX", "theme": "AI-CAPEX", "direction": "↑",
        "horizon": "1个月", "action": "可执行",
        "central": {"p": [40.0, 40.0, 20.0], "r": [10.0, 0.0, -5.0]},
        "conservative": {"p": [40.0, 40.0, 20.0], "r": [10.0, 0.0, -5.0]},
        "thesis": "t", "role": "chain", "sources": []}]}
    bid, rows, val = ideas_mod.build_batch(con, payload, as_of, generator="test",
                                           batch_id=batch_id)
    return bid


class ReadersDoNotSeeTheReplayBatch(unittest.TestCase):
    def setUp(self):
        self.con = db.init(":memory:")
        d = date(2026, 9, 2)
        for i in range(30):
            db.upsert(self.con, "prices", {
                "code": "US.SPY", "d": f"2026-08-{i % 28 + 1:02d}", "open": 100,
                "high": 101, "low": 99, "close": 100, "volume": 1, "src": "t"},
                ["code", "d"])
        self.real = _batch(self.con, "W20260902-omega", d)
        self.bt = _batch(self.con, f"{config.BACKTEST_BATCH_PREFIX}x-W20260902-omega", d)
        self.d = d

    def test_study_candidates_and_periods_count_only_the_real_batch(self):
        cands = backtest._candidates(self.con, self.d)
        self.assertEqual({c["id"].split("#")[0] for c in cands}, {self.real})
        per = backtest.periods(self.con)
        self.assertEqual([p["candidates"] for p in per], [1])

    def test_latest_batch_is_never_the_replay(self):
        self.assertEqual(ideas_mod.latest_batch(self.con, self.d), self.real)
        self.assertEqual(ideas_mod.latest_batch(self.con), self.real)


if __name__ == "__main__":
    unittest.main()
