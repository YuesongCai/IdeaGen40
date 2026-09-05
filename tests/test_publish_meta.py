"""What a generator's `meta` may carry across the publish wall.

The dashboard payload is exactly what `scripts/export_pages.py` publishes to a
public GitHub Pages site. `meta` used to cross that wall whole, guarded by a
denylist plus a publish gate that scans for machine identity and partner
identifiers. A key holding the PM's own sentence about how to invest matched
neither guard and was caught by hand. These tests pin the inverted rule: named
keys pass, unnamed keys pass only as numbers.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import review


class TestGeneratorMetaWall(unittest.TestCase):

    def test_local_view_keeps_everything(self):
        """Unlicensed runs are not exported; the operator sees the full record."""
        meta = {"note": "anything at all", "n": 3}
        self.assertEqual(review._gen_meta(meta, False), meta)

    def test_named_keys_survive(self):
        meta = {"per_topic": {"A": 2}, "target_per_topic": 2,
                "topics_without_corpus_match": 0, "truncated": False}
        self.assertEqual(review._gen_meta(dict(meta), True), meta)

    def test_an_unnamed_prose_key_does_not_cross(self):
        """The exact shape of the leak that happened."""
        out = review._gen_meta(
            {"philosophy_utterance": "只做我看得懂的宏观", "n": 5}, True)
        self.assertNotIn("philosophy_utterance", out)
        self.assertEqual(out["n"], 5)

    def test_unnamed_numbers_still_cross(self):
        """Numbers cannot carry prose, so a new counter needs no ceremony."""
        out = review._gen_meta({"some_new_counter": 12, "ratio": 0.5,
                                "flag": True}, True)
        self.assertEqual(out, {"some_new_counter": 12, "ratio": 0.5,
                               "flag": True})

    def test_a_string_that_looks_numeric_is_still_a_string(self):
        out = review._gen_meta({"sneaky": "12"}, True)
        self.assertNotIn("sneaky", out)

    def test_topic_errors_keep_the_class_not_the_model_words(self):
        out = review._gen_meta(
            {"topic_errors": {"INFLATION": "ValueError: 模型返回无法解析：接下来我认为…"}},
            True)
        self.assertEqual(out["topic_errors"]["INFLATION"], "ValueError")

    def test_nested_prose_under_an_unnamed_key_does_not_cross(self):
        out = review._gen_meta({"displaced": [{"why": "长篇模型自述"}]}, True)
        self.assertNotIn("displaced", out)

    def test_the_input_is_not_mutated(self):
        meta = {"note": "keep me here", "n": 1}
        review._gen_meta(meta, True)
        self.assertIn("note", meta)


if __name__ == "__main__":
    unittest.main()


class TestBooksAggregate(unittest.TestCase):
    """One curve for ten books, including the ones that started late."""

    def _b(self, cap, marks):
        return {"capital": cap,
                "equity": [{"d": d, "equity": v} for d, v in marks]}

    def test_a_late_book_sits_at_its_capital_before_it_starts(self):
        """Not missing data — that book was holding cash, and cash is a position."""
        out = review._books_aggregate([
            self._b(100, [("d1", 100), ("d2", 110)]),
            self._b(100, [("d2", 100)]),          # 第二本晚一天才开始
        ])
        self.assertEqual([x["equity"] for x in out["equity"]], [200.0, 210.0])
        self.assertEqual(out["capital"], 200)

    def test_a_gap_carries_the_last_mark_forward(self):
        out = review._books_aggregate([
            self._b(100, [("d1", 100), ("d3", 120)]),
            self._b(100, [("d1", 100), ("d2", 105), ("d3", 105)]),
        ])
        # d2: 第一本没有标记，沿用 d1 的 100
        self.assertEqual([x["equity"] for x in out["equity"]],
                         [200.0, 205.0, 225.0])

    def test_the_total_never_jumps_just_because_a_book_starts(self):
        """A partial sum would read as a gain nobody earned."""
        out = review._books_aggregate([
            self._b(100, [("d1", 100), ("d2", 100)]),
            self._b(100, [("d2", 100)]),
        ])
        vals = [x["equity"] for x in out["equity"]]
        self.assertEqual(vals, [200.0, 200.0])   # 起步不产生收益

    def test_return_is_against_the_full_capital(self):
        out = review._books_aggregate([
            self._b(100, [("d1", 110)]),
            self._b(100, [("d1", 100)]),
        ])
        self.assertAlmostEqual(out["return_pct"], 5.0)

    def test_no_marks_yet_is_not_an_error(self):
        out = review._books_aggregate([self._b(100, [])])
        self.assertEqual(out["equity"], [])
        self.assertIsNone(out["return_pct"])

    def test_unfunded_books_are_ignored(self):
        out = review._books_aggregate([{"capital": 0, "equity": []},
                                       self._b(100, [("d1", 100)])])
        self.assertEqual(out["n_books"], 1)

    def test_empty_input(self):
        self.assertEqual(review._books_aggregate([]), {})
