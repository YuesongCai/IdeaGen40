"""`S.backtest` is the stock-picking study, never the formal paper replay.

The two write the same `backtest_runs` table with different `summary` shapes.
The 证据 page's cards are built on the study's; the formal replay is read by
the 业绩 page through /api/perf. `_backtest_state` orders by as_of and end
time, so the first formal run to finish after the study would have become
`S.backtest` and blanked the evidence page without any error.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import db, review  # noqa: E402


class _State:
    def __init__(self):
        self.con = db.init(":memory:")

    def q(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]


class _P:
    def __init__(self):
        self.state = _State()


def _bt(st, bid, method, ended):
    st.con.execute(
        "INSERT INTO backtest_runs (backtest_id, as_of, window_start, window_end, "
        "methodology, data_classification, inputs_sha, ok, ended_at, summary) "
        "VALUES (?,?,?,?,?,?,?,1,?,?)",
        (bid, "2026-09-02", "2026-07-29", "2026-09-02", method, "backfill", "x",
         ended, '{"arms": {}}'))


class EvidencePageKeepsTheStudy(unittest.TestCase):
    def test_a_later_formal_replay_does_not_displace_the_study(self):
        p = _P()
        _bt(p.state, "bt-real-1", "real-pool-asof-replay/v1", "2026-09-07T01:00:00")
        _bt(p.state, "bt-formal-1", "formal-paper-rules", "2026-09-07T03:00:00")
        self.assertEqual(review._backtest_state(p)["backtest_id"], "bt-real-1")

    def test_with_only_a_formal_replay_the_block_is_empty_not_wrong(self):
        p = _P()
        _bt(p.state, "bt-formal-1", "formal-paper-rules", "2026-09-07T03:00:00")
        self.assertEqual(review._backtest_state(p), {})


if __name__ == "__main__":
    unittest.main()
