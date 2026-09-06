"""The cloud weekly runs the same chain as the laptop, and discovery reads the
run's corpus rather than a table the node does not have.

Two facts found on 2026-09-07 while checking where Jon's first item would
actually execute: the production node's weekly was the POC shape (two
generators, two selectors, three topics, `skip_theme_discovery=True`), and
`themes.candidates` read the `documents` table, which on that node is empty
because research lives in `corpus_documents` and reaches the run only as the
`corpus` rows the feed hands over. Either one alone would have made the
Wednesday run report 「无新主题」 forever, with nothing failing.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import db, poc_workflow, themes  # noqa: E402

AS_OF = date(2026, 9, 2)


def _doc(i, title, d="2026-09-02"):
    return {"doc_id": f"t:{i}", "title": title, "summary": "", "body": "",
            "tier": 2, "line": "ib", "institution": f"I{i % 4}", "published_d": d}


class DiscoveryReadsTheRunsCorpus(unittest.TestCase):
    def test_candidates_come_from_injected_rows_when_the_table_is_empty(self):
        con = db.init(":memory:")          # no documents at all
        titles = ["人形机器人量产元年开启", "人形机器人供应链梳理", "人形机器人：谁在受益",
                  "人形机器人产业链深度", "人形机器人订单落地", "人形机器人估值框架",
                  "人形机器人零部件国产化", "人形机器人海外进展", "人形机器人成本曲线",
                  "人形机器人政策催化", "人形机器人：三季度展望", "人形机器人龙头梳理"]
        days = ["2026-08-31", "2026-09-01", "2026-09-02"]
        corpus = [_doc(i, t, d=days[i % 3]) for i, t in enumerate(titles)]
        out = themes.candidates(con, AS_OF, corpus=corpus)
        self.assertEqual(out["corpus_total"], 12)
        self.assertEqual(out["gates"]["lift_gate"],
                         "skipped: no documents before the window")
        self.assertEqual(out["gates"]["baseline_docs"], 0)
        phrases = {t for c in out["candidates"] for t in c["terms"]}
        self.assertTrue(any("人形机器人" in t for t in phrases), phrases)
        # Without rows the same call sees nothing — the table is empty.
        self.assertEqual(themes.candidates(con, AS_OF)["corpus_total"], 0)

    def test_rows_outside_the_window_are_not_mined(self):
        con = db.init(":memory:")
        corpus = [_doc(i, "人形机器人量产元年 %d" % i, d="2026-08-01") for i in range(6)]
        out = themes.candidates(con, AS_OF, corpus=corpus)
        self.assertEqual(out["corpus_total"], 0)


class TheCloudWeeklyIsTheFullChain(unittest.TestCase):
    def test_the_switch_is_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {"IDEAGEN_WEEKLY_FULL_CHAIN": "1"}):
            self.assertTrue(poc_workflow.full_chain_enabled())
        with mock.patch.dict(os.environ, {"IDEAGEN_WEEKLY_FULL_CHAIN": "0"}):
            self.assertFalse(poc_workflow.full_chain_enabled())

    def test_compose_turns_it_on_for_the_scheduler(self):
        # `deploy/` is not in the image (the Dockerfile copies six directories
        # and two entrypoints) and not in the sync gate's checkout either, so
        # this assertion is about the repository and skips where there is none.
        path = ROOT / "deploy" / "compose.yaml"
        if not path.exists():
            raise unittest.SkipTest("no deploy/compose.yaml here (image or gate checkout)")
        text = path.read_text(encoding="utf-8")
        self.assertIn('IDEAGEN_WEEKLY_FULL_CHAIN: "${IDEAGEN_WEEKLY_FULL_CHAIN:-1}"', text)

    def test_full_chain_means_every_arm_five_topics_and_discovery(self):
        src = (ROOT / "ideagen" / "poc_workflow.py").read_text(encoding="utf-8")
        for needle in ('"generators": None if full else ["ai_native", "carl_constraint"]',
                       '"selectors": None if full else ["buy_all", "spread"]',
                       '"top_n": 5 if full else 3', '"n": 20 if full else 6',
                       '"skip_theme_discovery": not full'):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main()
