"""E 实据按因果深度计，且「尚未发生」的事实不算实据（Jon 2026-09-06 §3）。

旧法按类别关键词取第一个命中类别再查 {policy:100, earnings:75, price:50}：
「政策尚未落地」→100、「盈利预测缺乏依据」→75、「订单已签署并完成交付」→25，
把证据强弱排反了。这里只修已核实的实现缺陷：排序、未实现降级、转述去重；
权重 0.25 与 E 该不该进筛选仍未定论，旧口径留作 `E_category_legacy` 对照。
"""

from __future__ import annotations

import datetime as dtm
import os
import unittest
from unittest import mock

os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import lexicon, strategy as strat  # noqa: E402
from ideagen.strategies import topic_hgep  # noqa: E402
from ideagen.strategies.topic_hgep import hgep  # noqa: E402

POLICY_NOT_YET = "政策尚未落地"
FORECAST_NO_BASIS = "盈利预测缺乏依据"
ORDER_SIGNED = "订单已签署并完成交付"

THEME = lexicon.Theme(id="T", label="测试主题", key_question="q",
                      terms=("主题词", "themeword"), price_indicator="US.SPY",
                      registered_d="2026-01-01")


def _legacy(text):
    return topic_hgep.DEPTH.get(lexicon.fact_type_of(text), 25)


class DepthDetailOrdersTheThreeExamples(unittest.TestCase):
    def test_the_inverted_ordering_is_fixed(self):
        a = lexicon.depth_detail(POLICY_NOT_YET)["depth"]
        b = lexicon.depth_detail(FORECAST_NO_BASIS)["depth"]
        c = lexicon.depth_detail(ORDER_SIGNED)["depth"]
        self.assertEqual(a, 25)
        self.assertEqual(b, 25)
        self.assertIn(c, (75, 100))
        self.assertGreater(c, a)
        self.assertGreater(c, b)
        # The legacy coding — recorded so nobody has to take the bug on faith.
        self.assertEqual((_legacy(POLICY_NOT_YET), _legacy(FORECAST_NO_BASIS),
                          _legacy(ORDER_SIGNED)), (100, 75, 25))

    def test_an_unrealised_marker_downgrades_a_strong_word(self):
        d = lexicon.depth_detail("预计净利润同比增长20%")
        self.assertEqual(d["depth"], 25)
        self.assertTrue(d["downgraded"])
        self.assertEqual(d["raw_depth"], 100)
        self.assertEqual(d["matched"], "净利润")
        self.assertEqual(d["marker"], "预计")
        for text in ("新订单有望签署", "拟签订协议", "净利润尚未转正",
                     "the deal is expected to be signed", "capex guidance may be raised"):
            with self.subTest(text=text):
                self.assertEqual(lexicon.depth_detail(text)["depth"], 25)

    def test_the_downgrade_is_per_clause_not_per_document(self):
        # A realised fact in one clause is not undone by a forecast in another.
        d = lexicon.depth_detail("预计明年继续增长，归母净利同比增长30%")
        self.assertEqual(d["depth"], 100)
        self.assertFalse(d["downgraded"])
        self.assertIn("归母净利", d["clause"])

    def test_bare_未_does_not_trigger_on_未来(self):
        d = lexicon.depth_detail("未来收益率上行")
        self.assertEqual(d["depth"], 50)
        self.assertFalse(d["downgraded"])

    def test_depth_of_is_unchanged_as_the_legacy_control(self):
        self.assertEqual(lexicon.depth_of("预计净利润同比增长20%"), 100)
        self.assertEqual(lexicon.depth_of(ORDER_SIGNED), 100)


class HgepEvidenceUsesCausalDepth(unittest.TestCase):
    def _doc(self, i, title, summary="", institution=None, tier=1):
        return {"doc_id": f"d{i}", "published_d": "2026-08-25", "tier": tier,
                "title": f"主题词 themeword {title}", "summary": summary,
                "institution": institution}

    def _score(self, docs):
        ctx = strat.RunContext(as_of=dtm.date(2026, 8, 26), inputs_sha="x", corpus=docs)
        with mock.patch.object(lexicon, "all_themes", return_value=[THEME]):
            return hgep(ctx).scores["T"]

    def test_the_three_examples_score_in_the_right_order_and_legacy_is_kept(self):
        rows = {}
        for name, text in (("policy", POLICY_NOT_YET), ("forecast", FORECAST_NO_BASIS),
                           ("order", ORDER_SIGNED)):
            rows[name] = self._score([self._doc(1, text)])
        self.assertEqual(rows["policy"]["E"], 25.0)
        self.assertEqual(rows["forecast"]["E"], 25.0)
        self.assertGreaterEqual(rows["order"]["E"], 75.0)
        self.assertEqual(rows["policy"]["E_category_legacy"], 100.0)
        self.assertEqual(rows["forecast"]["E_category_legacy"], 75.0)
        self.assertEqual(rows["order"]["E_category_legacy"], 25.0)
        e = rows["order"]["e_detail"]
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["doc_id"], "d1")
        self.assertEqual(e[0]["matched"], "签署")
        self.assertEqual(e[0]["fact_type_legacy"], "orders")

    def test_a_fact_retold_by_four_outlets_is_one_fact(self):
        # Same institution, four notes with a realised profit → one 100, not
        # three 100s averaging to 100. H already counts the outlets.
        retold = [self._doc(i, "季报点评", "归母净利同比增长30%", institution="UBS")
                  for i in range(4)]
        weak = [self._doc(9, "展望", "管理层表示将考虑扩产", institution="Citi")]
        row = self._score(retold + weak)
        self.assertEqual([r["dedupe_key"] for r in row["e_detail"]],
                         ["inst:UBS", "inst:Citi"])
        self.assertEqual(row["E"], 62.5)      # mean(100, 25)
        self.assertEqual(row["E_category_legacy"], 75.0,
                         "legacy took three 75s (earnings) — the retell inflation")

    def test_anonymous_syndication_dedupes_on_the_title(self):
        docs = [self._doc(i, "同一条新闻 签署协议", "签订协议已完成") for i in range(3)]
        row = self._score(docs)
        self.assertEqual(len(row["e_detail"]), 1)

    def test_only_tier_one_and_two_count(self):
        row = self._score([self._doc(1, "x", "归母净利同比增长", tier=3)])
        self.assertEqual(row["E"], 25.0)
        self.assertEqual(row["e_detail"], [])

    def test_e_detail_records_the_downgrade(self):
        row = self._score([self._doc(1, "x", "预计净利润同比增长20%", institution="UBS")])
        e = row["e_detail"][0]
        self.assertTrue(e["downgraded"])
        self.assertEqual((e["raw_depth"], e["depth"], e["marker"]), (100, 25, "预计"))


if __name__ == "__main__":
    unittest.main()
