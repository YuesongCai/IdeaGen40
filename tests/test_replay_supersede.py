"""Re-running a completed period retires the old run instead of failing at close.

`orch_runs_done` admits one completed weekly per period. That is right for the
scheduler — a retried tick must not produce a week twice — and wrong for the
one thing it was never written for: replaying every stored week through a
changed chain (the 2026-09-07 corpus-first theme formation). Without
`supersede_completed` such a replay spends its model calls and then fails on
the unique index, which is the expensive shape of 「绿着的失败」 in reverse.

The retired row is re-labelled, not removed, and the spine keeps counting it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import backtest, db, orchestrator, schema  # noqa: E402


class _State:
    """The two methods `supersede_completed` needs, over an in-memory sqlite."""
    def __init__(self):
        self.con = db.init(":memory:")

    def q(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        return self.con.execute(sql, args).rowcount


def _run(state, run_id, as_of, ok=1, kind="weekly", started="2026-09-04T00:00:00"):
    state.execute("INSERT INTO orch_runs (run_id, as_of, kind, ok, started_at, "
                  "ended_at) VALUES (?,?,?,?,?,?)",
                  (run_id, as_of, kind, ok, started, started))


class SupersedeRetiresTheOldRun(unittest.TestCase):
    def test_completed_runs_of_the_period_are_relabelled_not_deleted(self):
        st = _State()
        _run(st, "old-1", "2026-08-05")
        _run(st, "old-fail", "2026-08-05", ok=0)
        _run(st, "other", "2026-08-12")
        got = orchestrator.supersede_completed(st, "2026-08-05", by="new-1")
        self.assertEqual(got, ["old-1"])
        kinds = {r["run_id"]: r["kind"] for r in st.q("SELECT run_id, kind FROM orch_runs")}
        self.assertEqual(kinds, {"old-1": "weekly_superseded", "old-fail": "weekly",
                                 "other": "weekly"})
        # The index now admits the replacement.
        _run(st, "new-1", "2026-08-05", started="2026-09-07T00:00:00")

    def test_the_unique_index_would_have_refused_without_it(self):
        st = _State()
        _run(st, "old-1", "2026-08-05")
        with self.assertRaises(sqlite3.IntegrityError):
            _run(st, "new-1", "2026-08-05")


class StudyReadsTheRunsOwnPool(unittest.TestCase):
    def test_candidates_table_beats_booked_ideas(self):
        con = db.init(":memory:")
        d = date(2026, 8, 5)
        con.execute("INSERT INTO orch_runs (run_id, as_of, kind, ok, started_at, ended_at) "
                    "VALUES ('r1','2026-08-05','weekly',1,'2026-09-07T00:00:00','2026-09-07T01:00:00')")
        import json
        payload = {"id": "pool:US.GLD", "instrument_id": "US.GLD", "topic_id": "INFLATION",
                   "topics": ["INFLATION", "DOLLAR-FX"], "proposed_by": ["chain"],
                   "upside_pct": 8.0, "downside_pct": -4.0, "p_up": 0.4, "p_base": 0.4,
                   "p_down": 0.2, "method": "chain", "horizon_days": 30}
        con.execute("INSERT INTO candidates (run_id, candidate_id, as_of, instrument_id, payload) "
                    "VALUES ('r1','pool:US.GLD','2026-08-05','US.GLD',?)", (json.dumps(payload),))
        db.upsert(con, "instruments", {"key": "US.GLD", "name": "SPDR Gold", "kind": "listed",
                                       "futu_code": "US.GLD", "priceable": 1}, ["key"])
        cands = backtest._candidates(con, d)
        self.assertEqual([c["id"] for c in cands], ["pool:US.GLD"])
        self.assertEqual(cands[0]["futu_code"], "US.GLD")
        self.assertEqual(cands[0]["topics"], ["INFLATION", "DOLLAR-FX"])

    def test_without_a_run_the_old_path_still_answers(self):
        con = db.init(":memory:")
        self.assertEqual(backtest._candidates(con, date(2026, 8, 5)), [])


if __name__ == "__main__":
    unittest.main()
