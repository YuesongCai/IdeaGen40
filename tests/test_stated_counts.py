"""A stated count has to come from the list it counts.

Three sentences in this repo announced a total and then listed a different
number of things, and each was written by adding to a list without touching the
number sitting in front of it:

* the backtest disclaimer said 「前视风险两项」 and listed three, because the
  count was a literal beside a list it did not connect to;
* the attribution note said 「四层归因」 — a number borrowed from the ask in
  docs/8个思考点.md (选择/择时/仓位/因子) — and then named three layers of its
  own devising, so 仓位 was missing and nothing in the sentence let a reader
  notice that the number and the names came from different taxonomies;
* the scorer said 「本期四个因子都有区分度」 with the four written out by hand,
  which counted a factor that produced no value at all among the ones that
  discriminated — the thing nobody measured reported as the thing that worked.

None is arithmetic. In each, the number was true of something; it was just not
true of the list printed next to it, and a reader has no way to tell.

What is mechanised here is the half that is mechanical: where a count is now
derived, the partition it derives from is total, so a state added later cannot
fall outside every branch and leave the total wrong again. A factor is inert,
live, or unmeasured and never two of those; an attribution layer is done or not.

Not mechanised, said plainly so a green run is not overread: this cannot find
the next hand-written count. A regex over string literals was tried — of fifteen
matches, thirteen were ordinary prose ("两个不相交的组", "四层归因里的一层"),
and a check that cries wolf thirteen times teaches everyone to skip it. So this
holds the three that were fixed and the shape they now share, not the class.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen.strategies import topic_hgep  # noqa: E402


def _backtest():
    spec = importlib.util.spec_from_file_location(
        "_rb", ROOT / "scripts" / "run_real_backtest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FactorNoteAccountsForEveryFactor(unittest.TestCase):
    """inert / live / unmeasured partition `WEIGHTS`, with no overlap."""

    def test_the_three_states_are_disjoint_and_cover_the_table(self):
        # The bug was a factor in none of them being counted in the total.
        weights = set(topic_hgep.WEIGHTS)
        for inert, live, unmeasured in [
                (set(), weights, set()),
                ({"E", "P"}, {"H", "G"}, set()),
                (set(), {"H", "G", "E"}, {"P"}),
                ({"E"}, {"H", "G"}, {"P"}),
                (weights, set(), set())]:
            with self.subTest(inert=sorted(inert), unmeasured=sorted(unmeasured)):
                self.assertEqual(inert | live | unmeasured, weights)
                self.assertEqual(len(inert) + len(live) + len(unmeasured),
                                 len(weights), "a factor in two states")

    def test_the_partition_itself_puts_an_unmeasured_factor_nowhere_else(self):
        """The call site, not just the sentence it produces.

        This existed only as a sentence test first. A mutation that folded
        `unmeasured` back into the discriminating set passed all of them: the
        helper was correct and was being handed the wrong lists.
        """
        seen = {"discriminates": True}
        flat = {"discriminates": False}
        inert, weight, live, unmeasured = topic_hgep._partition_factors(
            {"H": seen, "G": seen, "E": flat})          # P never measured
        self.assertEqual(unmeasured, ["P"])
        self.assertEqual(inert, ["E"])
        self.assertEqual(live, ["H", "G"])
        self.assertNotIn("P", inert + live)
        self.assertEqual(weight, topic_hgep.WEIGHTS["E"])

    def test_the_partition_is_total_for_any_dispersion(self):
        weights = list(topic_hgep.WEIGHTS)
        seen = {"discriminates": True}
        for keep in range(len(weights) + 1):
            with self.subTest(measured=weights[:keep]):
                inert, _, live, unmeasured = topic_hgep._partition_factors(
                    {f: seen for f in weights[:keep]})
                self.assertEqual(sorted(inert + live + unmeasured),
                                 sorted(weights))

    def test_an_unmeasured_factor_is_never_reported_as_discriminating(self):
        # The exact failure: P produced no value and the note said all four
        # discriminated. It must now be named as unmeasured instead.
        note = topic_hgep._ranking_note([], 0.0, ["H", "G", "E"], ["P"])
        self.assertIn("P", note)
        self.assertIn("没有取到值", note)
        self.assertNotIn("都有区分度", note)

    def test_the_clean_case_counts_from_the_table_not_from_a_literal(self):
        note = topic_hgep._ranking_note([], 0.0, list(topic_hgep.WEIGHTS), [])
        self.assertIn(f"{len(topic_hgep.WEIGHTS)} 个因子都有区分度", note)

    def test_nothing_left_to_rank_is_its_own_statement(self):
        # Every factor inert is not a milder version of a working run: the
        # scores are equal and the top-5 is dict order. It must not read as one.
        note = topic_hgep._ranking_note(list(topic_hgep.WEIGHTS), 1.0, [], [])
        self.assertIn("名次不成立", note)
        self.assertNotIn("实际由", note)


class AttributionLayersAreNamedNotCounted(unittest.TestCase):
    def setUp(self):
        self.rb = _backtest()

    def test_the_note_names_every_layer_it_counts(self):
        note = self.rb._layers_note()
        layers = self.rb.ATTRIBUTION_LAYERS
        self.assertIn(f"{len(layers)} 层", note)
        for name, _, _ in layers:
            self.assertIn(name, note, f"counted {len(layers)} layers, "
                                      f"never named {name}")

    def test_done_and_missing_together_are_all_of_them(self):
        layers = self.rb.ATTRIBUTION_LAYERS
        done = [n for n, _, ok in layers if ok]
        todo = [n for n, _, ok in layers if not ok]
        self.assertEqual(len(done) + len(todo), len(layers))
        self.assertTrue(done, "a note claiming a layer exists must have one")
        self.assertTrue(todo, "if every layer were done this note should go")

    def test_the_sizing_layer_the_prose_forgot_is_present(self):
        # 仓位 was the one dropped when the count came from Jon's taxonomy and
        # the names came from somewhere else. Named here so it stays visible
        # until it is built.
        self.assertIn("仓位", [n for n, _, _ in self.rb.ATTRIBUTION_LAYERS])


#: The shape `_disclaimer` reads out of `_horizon_completeness`. Fixed here so
#: these assertions are about the sentence, not about a live run's numbers.
_HORIZON = {"complete_frac": 0.22,
            "arms": {"a": {"complete_frac": 0.18},
                     "b": {"complete_frac": 0.31}}}


class DisclaimerCountsItsOwnRisks(unittest.TestCase):
    def setUp(self):
        self.rb = _backtest()

    def test_the_look_ahead_count_matches_the_items_listed(self):
        text = self.rb._disclaimer(
            n_backfill=3, asof_note="货架上架日期：某某", horizon=_HORIZON,
            horizon_days=30, excluded=[])
        stated = next(int(c) for c in text.split("前视风险 ")[1][:2] if c.isdigit())
        marked = sum(text.count(m) for m in "①②③④⑤")
        self.assertEqual(stated, marked,
                         f"announced {stated} look-ahead risks, marked {marked}")

    def test_each_marker_appears_exactly_once(self):
        # The list-derived count surfaced a `②②`: `asof_note` carried its own
        # marker and the join added another. Numbering belongs to whichever
        # side owns the list.
        text = self.rb._disclaimer(
            n_backfill=3, asof_note="货架上架日期：某某", horizon=_HORIZON,
            horizon_days=30, excluded=[])
        for marker in "①②③④⑤":
            self.assertLessEqual(text.count(marker), 1, f"{marker} appears twice")

    def test_the_holding_period_caveat_is_not_filed_under_look_ahead(self):
        # Look-ahead is using information from the future; an unfinished holding
        # window is the future not having arrived. Opposite failures, and a
        # reader who accepts the merged count has been told something false.
        text = self.rb._disclaimer(
            n_backfill=3, asof_note="货架上架日期：某某", horizon=_HORIZON,
            horizon_days=30, excluded=[])
        self.assertIn("与前视无关", text)


if __name__ == "__main__":
    unittest.main()
