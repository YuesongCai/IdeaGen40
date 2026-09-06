"""G 分歧按对象归属观点（Jon 2026-09-06 §2）。

旧法 `lexicon.stance_of(整篇)` 给「黄金看多，白银看空」判 0：黄金的正向被白银的
负向抵消。这里钉住的是三件事——观点归到它说的那个对象；「利率下调」这类方向含义
取决于主题的对象不套固定正负号；模型路径的归属、缓存命中、坏输出回退都可核对。
"""

from __future__ import annotations

import datetime as dtm
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import claims, lexicon, schema, strategy as strat  # noqa: E402
from ideagen.platform.base import Completion  # noqa: E402
from ideagen.platform.local import SqliteStateStore  # noqa: E402
from ideagen.strategies.topic_hgep import hgep  # noqa: E402


def _theme(tid, label, terms, **kw):
    return lexicon.Theme(id=tid, label=label, key_question=kw.pop("q", "q"),
                         terms=tuple(terms), price_indicator=kw.pop("px", "US.SPY"),
                         registered_d="2026-01-01", **kw)


GOLD = _theme("GOLD", "黄金", ("黄金", "gold"), px="US.GLD")
SILVER = _theme("SILVER", "白银", ("白银", "silver"), px="US.SLV")
FED = _theme("FED", "联储路径", ("美联储", "利率", "降息"), px="US.TLT")
EARN = _theme("EARN", "盈利兑现", ("盈利", "业绩"), px="US.QUAL")


def _by_theme(rows):
    out = {}
    for r in rows:
        out.setdefault(r["theme_id"], []).append(r)
    return out


class ClausePathAnchorsTheSignToTheObject(unittest.TestCase):
    def test_gold_bullish_silver_bearish_lands_on_each_theme(self):
        doc = {"doc_id": "d1", "title": "贵金属周报", "summary": "黄金看多，白银看空。"}
        by = _by_theme(claims.clause_claims(doc, [GOLD, SILVER]))
        self.assertEqual([c["direction"] for c in by["GOLD"]], [1])
        self.assertEqual([c["direction"] for c in by["SILVER"]], [-1])
        self.assertEqual(by["GOLD"][0]["object"], "黄金")
        self.assertIn("黄金看多", by["GOLD"][0]["quote"])
        # The old coding, for the record: the two cancel.
        self.assertEqual(lexicon.stance_of("黄金看多，白银看空。"), 0)

    def test_contrastive_conjunction_without_a_comma_still_splits(self):
        doc = {"doc_id": "d2", "title": "黄金看多但白银看空", "summary": ""}
        by = _by_theme(claims.clause_claims(doc, [GOLD, SILVER]))
        self.assertEqual(by["GOLD"][0]["direction"], 1)
        self.assertEqual(by["SILVER"][0]["direction"], -1)

    def test_earnings_upgrade_is_positive_and_rate_cut_is_unresolved(self):
        doc = {"doc_id": "d3", "title": "策略周报", "summary": "盈利上调，利率下调。"}
        by = _by_theme(claims.clause_claims(doc, [EARN, FED]))
        self.assertEqual(by["EARN"][0]["direction"], 1)
        self.assertFalse(by["EARN"][0]["unresolved"])
        fed = by["FED"][0]
        self.assertTrue(fed["unresolved"], "利率下调 must be declined, not signed")
        self.assertEqual(fed["direction"], 0)
        self.assertNotEqual(fed["direction"], -1)
        # And the word list alone would have said −1 — that is the bug.
        self.assertEqual(lexicon.stance_of("利率下调"), -1)

    def test_an_explicit_stance_word_beats_the_rate_rule(self):
        # 「降息利好黄金」: the author signed it; no rule needed.
        doc = {"doc_id": "d4", "title": "美联储降息预期升温，利好黄金", "summary": ""}
        by = _by_theme(claims.clause_claims(doc, [GOLD, FED]))
        self.assertEqual(by["GOLD"][0]["direction"], 1)
        self.assertTrue(by["FED"][0]["unresolved"])

    def test_conditional_and_hedged_claims_are_kept_and_marked(self):
        doc = {"doc_id": "d5", "title": "黄金展望",
               "summary": "若美元走弱，黄金或将上行，未来3个月看多。"}
        rows = claims.clause_claims(doc, [GOLD])
        cond = [c for c in rows if c["condition"]]
        self.assertTrue(cond, "the conditional clause must be recorded, not dropped")
        self.assertEqual(cond[0]["condition_marker"], "若")
        hedged = [c for c in rows if c["hedge"]]
        self.assertTrue(hedged)
        self.assertEqual(hedged[0]["direction"], 1)
        firm = [c for c in rows if c["direction"] == 1 and not c["hedge"]
                and not c["condition"]]
        self.assertTrue(firm)
        self.assertLess(hedged[0]["confidence"], firm[0]["confidence"])
        self.assertEqual(firm[0]["horizon"], "未来3个月")

    def test_a_repeated_clause_votes_once(self):
        # summary is often the head of body copied back.
        doc = {"doc_id": "d6", "title": "黄金", "summary": "黄金看多。",
               "body": "黄金看多。其余略。"}
        rows = claims.clause_claims(doc, [GOLD])
        self.assertEqual(len([c for c in rows if c["direction"] == 1]), 1)


class DisagreementCountsOneInstitutionOnce(unittest.TestCase):
    def test_same_institution_same_direction_is_one_vote(self):
        rows = [
            {"doc_id": "a", "theme_id": "GOLD", "direction": 1, "institution": "UBS",
             "quote": "x", "source": "clause"},
            {"doc_id": "b", "theme_id": "GOLD", "direction": 1, "institution": "UBS",
             "quote": "y", "source": "clause"},
            {"doc_id": "c", "theme_id": "GOLD", "direction": -1, "institution": "Citi",
             "quote": "z", "source": "clause"},
            {"doc_id": "d", "theme_id": "GOLD", "direction": 0, "unresolved": True,
             "institution": None, "quote": "w", "source": "clause"},
        ]
        g = claims.disagreement(rows, {"d": "利率周报"})
        self.assertEqual((g["n_pos"], g["n_neg"]), (1, 1))
        self.assertEqual(g["n_unresolved"], 1)
        self.assertEqual(claims.g_score(g["n_pos"], g["n_neg"]), 100.0)
        self.assertEqual(g["source_mix"], {"clause": 4})
        self.assertLessEqual(len(g["samples"]), 8)
        # Unresolved rows do not enter the denominator.
        self.assertEqual(claims.g_score(0, 0), 0.0)

    def test_anonymous_documents_dedupe_on_title_signature(self):
        rows = [{"doc_id": i, "theme_id": "GOLD", "direction": 1, "institution": None,
                 "quote": "q", "source": "clause"} for i in ("a", "b")]
        same = claims.disagreement(rows, {"a": "黄金：看多理由", "b": "黄金——看多理由"})
        diff = claims.disagreement(rows, {"a": "黄金看多", "b": "白银季度回顾"})
        self.assertEqual(same["n_pos"], 1, "a syndicated title is one source")
        self.assertEqual(diff["n_pos"], 2)


class _FakeInfer:
    """Returns a fixed answer and counts how often it was asked."""

    def __init__(self, text):
        self.text, self.calls = text, 0

    def complete(self, prompt, **kw):
        self.calls += 1
        return Completion(text=self.text, model="fake",
                          usage={"prompt_tokens": 120, "completion_tokens": 30,
                                 "total_tokens": 150})


def _state():
    st = SqliteStateStore(Path(tempfile.mkdtemp()) / "s.db")
    schema.migrate(st)
    return st


class ModelPath(unittest.TestCase):
    DOC = {"doc_id": "m1", "title": "贵金属周报", "summary": "黄金看多，白银看空。",
           "institution": "UBS"}
    GOOD = json.dumps({"claims": [
        {"theme_id": "GOLD", "object": "黄金", "direction": "+1", "quote": "黄金看多",
         "confidence": 0.9},
        {"theme_id": "SILVER", "object": "白银", "direction": "-1", "quote": "白银看空",
         "condition": None, "horizon": "未来3个月", "confidence": 0.8},
        {"theme_id": "SILVER", "object": "铂金", "direction": "弃判", "quote": "无",
         "confidence": 0.2},
        {"theme_id": "NOT-A-THEME", "object": "?", "direction": "+1", "quote": ""},
    ]}, ensure_ascii=False)

    def test_attribution_usage_and_unverified_quote(self):
        inf = _FakeInfer(self.GOOD)
        rows, rc = claims.extract_claims_detailed(self.DOC, [GOLD, SILVER], infer=inf)
        self.assertEqual(rc["source"], "model")
        self.assertEqual(rc["calls"], 1)
        self.assertEqual(rc["usage"]["total_tokens"], 150)
        by = _by_theme(rows)
        self.assertEqual(by["GOLD"][0]["direction"], 1)
        self.assertTrue(by["GOLD"][0]["quote_verified"])
        self.assertEqual(by["SILVER"][0]["direction"], -1)
        self.assertEqual(by["SILVER"][0]["horizon"], "未来3个月")
        self.assertTrue(by["SILVER"][1]["unresolved"], "弃判 must be honoured")
        self.assertFalse(by["SILVER"][1]["quote_verified"],
                         "a quote not in the text is kept but flagged")
        self.assertNotIn("NOT-A-THEME", by, "unknown theme ids are dropped, not guessed")
        self.assertTrue(all(r["source"] == "model" for r in rows))

    def test_cache_hit_does_not_call_the_model_again(self):
        inf = _FakeInfer(self.GOOD)
        cache = claims.ClaimCache(_state())
        first, rc1 = claims.extract_claims_detailed(self.DOC, [GOLD, SILVER],
                                                    infer=inf, cache=cache)
        second, rc2 = claims.extract_claims_detailed(self.DOC, [GOLD, SILVER],
                                                     infer=inf, cache=cache)
        self.assertEqual(inf.calls, 1)
        self.assertEqual(rc2["source"], "model:cache")
        self.assertTrue(rc2["cache_hit"])
        self.assertEqual(rc2["usage"]["total_tokens"], 150,
                         "the cost of the original call travels with the hit")
        self.assertEqual([r["direction"] for r in first],
                         [r["direction"] for r in second])
        self.assertEqual((cache.hits, cache.misses), (1, 1))
        # A different question set is a different key.
        claims.extract_claims_detailed(self.DOC, [GOLD], infer=inf, cache=cache)
        self.assertEqual(inf.calls, 2)

    def test_bad_json_falls_back_to_the_clause_path_and_says_so(self):
        inf = _FakeInfer("对不起，我无法判断。")
        cache = claims.ClaimCache(_state())
        rows, rc = claims.extract_claims_detailed(self.DOC, [GOLD, SILVER],
                                                  infer=inf, cache=cache)
        self.assertEqual(rc["calls"], 1)
        self.assertTrue(rc["error"])
        self.assertEqual(rc["source"], "clause")
        by = _by_theme(rows)
        self.assertEqual(by["GOLD"][0]["direction"], 1)
        self.assertEqual(by["GOLD"][0]["source"], "clause:fallback")
        # Failures are not cached: the next period gets another chance.
        self.assertIsNone(cache.get(claims.cache_key(claims.doc_text(self.DOC),
                                                     [GOLD, SILVER])))

    def test_a_model_exception_is_also_a_fallback(self):
        class Boom:
            def complete(self, prompt, **kw):
                raise TimeoutError("slow")
        rows, rc = claims.extract_claims_detailed(self.DOC, [GOLD, SILVER], infer=Boom())
        self.assertIn("TimeoutError", rc["error"])
        self.assertTrue(rows)


class HgepUsesTheClaims(unittest.TestCase):
    def _ctx(self, docs, **kw):
        return strat.RunContext(as_of=dtm.date(2026, 8, 26), inputs_sha="x",
                                corpus=docs, **kw)

    def test_gold_and_silver_get_opposite_votes_from_one_report(self):
        docs = [{"doc_id": "r1", "published_d": "2026-08-25", "tier": 1,
                 "title": "Gold & silver：黄金看多，白银看空", "summary": "黄金看多，白银看空。",
                 "institution": "UBS"},
                {"doc_id": "r2", "published_d": "2026-08-25", "tier": 1,
                 "title": "Gold & silver 周报", "summary": "黄金看空，白银看多。",
                 "institution": "Citi"}]
        with mock.patch.object(lexicon, "all_themes", return_value=[GOLD, SILVER]):
            v = hgep(self._ctx(docs))
        for tid in ("GOLD", "SILVER"):
            g = v.scores[tid]["g_detail"]
            self.assertEqual((g["n_pos"], g["n_neg"]), (1, 1), tid)
            self.assertEqual(v.scores[tid]["G"], 100.0, tid)
            # The old coding saw two neutral documents and no disagreement.
            self.assertEqual(v.scores[tid]["G_keyword"], 0.0, tid)
            self.assertEqual(v.scores[tid]["g_source"], "clause")
        self.assertIn("claims", v.meta)
        self.assertFalse(v.meta["claims"]["model_enabled"])

    def test_single_object_documents_reproduce_the_keyword_g(self):
        """Regression: where there is one object per document the two codings
        must agree, or the new one changed more than it claims to."""
        docs = [{"doc_id": f"p{i}", "published_d": "2026-08-25", "tier": 1,
                 "title": f"黄金 gold 观点 {i}",
                 "summary": "黄金看多，需求强劲，订单增加。"} for i in range(3)]
        docs += [{"doc_id": f"n{i}", "published_d": "2026-08-25", "tier": 1,
                  "title": f"黄金 gold 观点 减持 {i}",
                  "summary": "黄金看空，需求疲弱，承压。"} for i in range(2)]
        with mock.patch.object(lexicon, "all_themes", return_value=[GOLD]):
            v = hgep(self._ctx(docs))
        row = v.scores["GOLD"]
        self.assertEqual(row["G"], row["G_keyword"])
        self.assertEqual((row["g_detail"]["n_pos"], row["g_detail"]["n_neg"]), (3, 2))

    def test_the_model_path_runs_through_hgep_and_counts_calls(self):
        docs = [{"doc_id": "m1", "published_d": "2026-08-25", "tier": 1,
                 "title": "贵金属周报 gold silver", "summary": "黄金看多，白银看空。",
                 "institution": "UBS"}]
        inf = _FakeInfer(ModelPath.GOOD)
        cache = claims.ClaimCache(_state())
        with mock.patch.object(lexicon, "all_themes", return_value=[GOLD, SILVER]):
            v1 = hgep(self._ctx(docs, infer=inf, claim_cache=cache))
            v2 = hgep(self._ctx(docs, infer=inf, claim_cache=cache))
        self.assertEqual(v1.calls, 1)
        self.assertEqual(v2.calls, 0, "second period is served from the cache")
        self.assertEqual(v1.scores["GOLD"]["g_source"], "model")
        self.assertEqual(v1.meta["claims"]["usage"]["total_tokens"], 150)
        self.assertEqual(v2.meta["claims"]["model_cache"], 1)

    def test_a_replay_port_never_gets_claim_prompts(self):
        class Replay:
            replay_only = True

            def complete(self, prompt, **kw):
                raise AssertionError("must not be called")
        docs = [{"doc_id": "m1", "published_d": "2026-08-25", "tier": 1,
                 "title": "黄金 gold", "summary": "黄金看多。"}]
        with mock.patch.object(lexicon, "all_themes", return_value=[GOLD]):
            v = hgep(self._ctx(docs, infer=Replay()))
        self.assertEqual(v.calls, 0)
        self.assertFalse(v.meta["claims"]["model_enabled"])


class TheCacheTableExists(unittest.TestCase):
    def test_migrate_creates_claim_cache_with_the_columns_the_cache_writes(self):
        st = _state()
        cols = {r["name"] for r in st.q("PRAGMA table_info(claim_cache)")}
        self.assertTrue({"cache_key", "doc_id", "extractor", "theme_fp", "source",
                         "model", "claims", "usage", "created_at"} <= cols)
        self.assertIn("claim_cache", schema.OWNED)
        self.assertIn("claim_cache", schema.CONFLICT_KEY)


if __name__ == "__main__":
    unittest.main()
