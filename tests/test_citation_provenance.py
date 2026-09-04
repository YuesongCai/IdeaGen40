"""The citations under each idea, checked as claims rather than as decoration.

The panel's whole offer is that an idea can be walked back to the reports it was
read from. That offer rests on two things being true of every citation a
candidate carries, and neither is self-evident:

* the cited document exists — a model asked for sources will supply
  well-formed ones whether or not it read anything, so an id that resolves is
  the difference between a source and a plausible string;
* the cited document existed *then* — a citation to something published after
  the period it was written for is look-ahead wearing the costume of evidence,
  and it is the harder one to notice because the id resolves perfectly.

The replay audit already refuses a context carrying a document dated after the
day being replayed. This is a different surface: what ended up inside a
candidate's payload, which is what the page renders and what a reader clicks
through to. A candidate can only cite what it was shown, so agreement between
the two is expected — and that is exactly why it is worth asserting rather than
assumed, because the day they disagree, the page is the one telling the story.

Scope, stated so it is not overread: these are mechanical checks. That a cited
report exists and predates the period does not make it *support* the thesis
written above it. Nothing here can check that, and a passing run should not be
read as saying otherwise — the same gap that lets a required free-text field be
filled with something fluent and false.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config  # noqa: E402


def _db() -> Path:
    return Path(config.DATA) / "ideagen.db"


class CitationProvenance(unittest.TestCase):
    """Runs against the project database; skips out loud without one."""

    @classmethod
    def setUpClass(cls):
        path = _db()
        if not path.exists():
            raise unittest.SkipTest(f"no project database at {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            cls.published = {
                str(r["doc_id"]): str(r["published_d"] or "")[:10]
                for r in con.execute("SELECT doc_id, published_d FROM documents")}
            cls.rows = []
            for r in con.execute("SELECT as_of, candidate_id, method, payload "
                                 "FROM candidates"):
                payload = json.loads(r["payload"] or "{}")
                cls.rows.append({
                    "as_of": str(r["as_of"])[:10],
                    "candidate_id": str(r["candidate_id"]),
                    "citations": [str(c) for c in (payload.get("citations") or [])],
                    "bad": int(payload.get("bad_citations") or 0),
                })
        finally:
            con.close()
        if not cls.rows:
            raise unittest.SkipTest("no candidates stored yet")

    def test_every_cited_document_resolves(self):
        missing = [(r["candidate_id"], c) for r in self.rows
                   for c in r["citations"] if c not in self.published]
        self.assertEqual(
            missing, [],
            "a citation that resolves to nothing is a plausible string, not a "
            "source; the generator's id check is what keeps these grounded")

    def test_no_candidate_cites_a_report_published_after_its_period(self):
        late = [(r["as_of"], r["candidate_id"], c, self.published[c])
                for r in self.rows for c in r["citations"]
                if self.published.get(c) and self.published[c] > r["as_of"]]
        self.assertEqual(
            late, [],
            "cited a report that did not exist yet on the period it was written "
            "for — look-ahead that reads as evidence because the id resolves")

    def test_the_check_actually_had_citations_to_check(self):
        # Without this, a batch that stopped citing anything would sail through
        # both assertions above: zero citations violate nothing.
        total = sum(len(r["citations"]) for r in self.rows)
        self.assertGreater(total, 0, "no citations at all — the assertions above "
                                     "would pass by having nothing to test")
        uncited = [r["candidate_id"] for r in self.rows if not r["citations"]]
        self.assertLessEqual(
            len(uncited), len(self.rows) // 10,
            f"{len(uncited)}/{len(self.rows)} candidates carry no citation; "
            f"the traceability claim is only as good as its coverage")

    def test_rejected_citations_stay_rare_and_stay_counted(self):
        # `bad_citations` is the generator's own record of ids it refused. It
        # existing at all is the reason the resolve check above passes, so a
        # sudden climb means the model started inventing sources even though
        # nothing downstream would show it.
        flagged = sum(r["bad"] for r in self.rows)
        total = sum(len(r["citations"]) for r in self.rows)
        self.assertLess(
            flagged, max(3, total // 20),
            f"{flagged} citations were rejected as unresolvable against "
            f"{total} kept — that is a generation problem, not a parsing one")


if __name__ == "__main__":
    unittest.main()
