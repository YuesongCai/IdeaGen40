"""筛选B 第五种：两段式生成方式的合约。

The arm's value rests entirely on stage one's names reaching stage two's
holdings match. A ticker that arrives as `NASDAQ:NVDA` and is compared against
`NVDA` matches nothing, and a basket that matches nothing produces a shortlist
that is empty for a reason nobody can see. So the normalisation, the refusals
and the call accounting each get a test — the refusals especially, because an
arm that quietly falls back to the label menu on the topics it cannot express
would still produce ideas and would no longer be measuring anything.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from ideagen import lookthrough as lt
from ideagen.strategies import _gen, gen_lookthrough as glt
from ideagen.strategy import RunContext


def F(sym, weights):
    return lt.Fund(sym, "2026-09-05", weights, {k: k for k in weights},
                   1.0, len(weights), "ok", "")


FUNDS = {
    "ITA":  F("ITA",  {"RTX": .30, "LMT": .20, "GD": .10, "OTHER": .40}),
    "XAR":  F("XAR",  {"RTX": .05, "LMT": .05, "GD": .05, "OTHER": .85}),
    "SPY":  F("SPY",  {"RTX": .01, "LMT": .01, "OTHER": .98}),
    "SMH":  F("SMH",  {"NVDA": .60, "OTHER": .40}),
    "GLD":  lt.Fund("GLD", "2026-09-05", {}, {}, 0.0, 1, "opaque", "实物"),
}
UNIVERSE = [{"instrument_id": s, "name": s + " ETF"}
            for s in ("ITA", "XAR", "SPY", "GLD")]
TOPIC = {"topic_id": "DEFENCE", "label": "欧洲重整军备", "terms": ["国防"]}


class FakeInfer:
    """One canned answer per call, so a test can drive both stages."""

    def __init__(self, *texts):
        self.texts, self.n = list(texts), 0

    def complete(self, prompt, **kw):
        self.n += 1
        i = min(self.n - 1, len(self.texts) - 1)
        return mock.Mock(text=self.texts[i])


def ctx(infer=None, universe=None):
    return RunContext(as_of=date(2026, 9, 5), inputs_sha="x",
                      topics=[TOPIC], universe=universe or UNIVERSE,
                      infer=infer)


def patched(funds=None, corpus=("材料", 3)):
    """Stand in for the snapshot and the corpus block, which are not under test."""
    return (mock.patch.object(glt, "_funds",
                              lambda c: FUNDS if funds is None else funds),
            mock.patch.object(_gen, "corpus_block", lambda c, t, **k: corpus))


class Tickers(unittest.TestCase):
    def test_strips_the_decorations_models_add(self):
        raw = {"names": ["nvda", "NASDAQ:LMT", "$RTX", "GD.US", " noc "]}
        self.assertEqual(glt._tickers(raw), ["NVDA", "LMT", "RTX", "GD", "NOC"])

    def test_accepts_a_bare_list_and_objects(self):
        self.assertEqual(glt._tickers(["LMT"]), ["LMT"])
        self.assertEqual(glt._tickers([{"ticker": "LMT"}]), ["LMT"])

    def test_drops_prose_and_duplicates(self):
        raw = ["LMT", "LMT", "洛克希德马丁", "a very long sentence here"]
        self.assertEqual(glt._tickers(raw), ["LMT"])

    def test_caps_the_basket(self):
        many = [f"AA{i}" for i in range(40)]
        self.assertEqual(len(glt._tickers(many)), glt.BASKET_MAX)


class Refusals(unittest.TestCase):
    def test_no_snapshot_is_named_not_silently_skipped(self):
        p1, p2 = patched(funds={})
        with p1, p2, self.assertRaises(RuntimeError) as e:
            glt.build_prompt(ctx(FakeInfer('["LMT","RTX","GD"]')), TOPIC)
        self.assertIn("穿透快照", str(e.exception))

    def test_unusable_basket_refuses(self):
        p1, p2 = patched()
        with p1, p2, self.assertRaises(RuntimeError) as e:
            glt.build_prompt(ctx(FakeInfer('["LMT"]')), TOPIC)
        self.assertIn("公司名单", str(e.exception))

    def test_theme_with_no_measurable_vehicle_refuses_rather_than_falling_back(self):
        """A gold thesis has no basket of identifiable securities. Reverting to
        the label menu here would still produce ideas and would silently stop
        measuring the thing this arm exists to measure."""
        p1, p2 = patched()
        with p1, p2, self.assertRaises(RuntimeError) as e:
            glt.build_prompt(ctx(FakeInfer('["AAA","BBB","CCC"]')), TOPIC)
        self.assertIn("没有可度量的载体", str(e.exception))

    def test_refusal_counts_the_opaque_instruments_it_could_not_score(self):
        p1, p2 = patched()
        with p1, p2, self.assertRaises(RuntimeError) as e:
            glt.build_prompt(ctx(FakeInfer('["AAA","BBB","CCC"]')), TOPIC)
        self.assertIn("看不透", str(e.exception))


    def test_stale_snapshot_refuses_with_its_date(self):
        """A strategy may not fetch — RunContext withholds the network so it
        cannot read the future — so an out-of-date snapshot has to be refused
        here rather than quietly repaired here."""
        old = {k: lt.Fund("ITA", "2026-01-01", v.weights, v.labels,
                          v.coverage, v.rows_seen, v.status, v.note)
               for k, v in FUNDS.items()}
        p1, p2 = patched(funds=old)
        with p1, p2, self.assertRaises(RuntimeError) as e:
            glt.build_prompt(ctx(FakeInfer('["RTX","LMT","GD"]')), TOPIC)
        self.assertIn("2026-01-01", str(e.exception))
        self.assertIn("不是这一期的", str(e.exception))

    def test_recent_snapshot_is_fine(self):
        p1, p2 = patched()
        with p1, p2:
            prompt, _, _ = glt.build_prompt(
                ctx(FakeInfer('["RTX","LMT","GD"]')), TOPIC)
        self.assertIn("ITA |", prompt)


class Shortlist(unittest.TestCase):
    def _build(self, universe=None):
        p1, p2 = patched()
        with p1, p2:
            return glt.build_prompt(
                ctx(FakeInfer('["RTX","LMT","GD"]'), universe), TOPIC)

    def test_returns_three_elements_and_counts_stage_one(self):
        prompt, n_docs, calls = self._build()
        self.assertEqual(calls, 1)
        self.assertEqual(n_docs, 3)

    def test_ranks_by_through_weight(self):
        prompt, _, _ = self._build()
        self.assertLess(prompt.index("ITA |"), prompt.index("XAR |"))

    def test_incidental_index_holdings_are_not_an_expression_of_the_theme(self):
        """SPY holds every defence prime. At 2% that is not a defence position."""
        prompt, _, _ = self._build()
        self.assertNotIn("SPY |", prompt)

    def test_only_offers_what_stage_b_may_buy(self):
        """SMH is in the snapshot and not in this period's eligible universe."""
        prompt, _, _ = self._build()
        self.assertNotIn("SMH", prompt)

    def test_shows_measured_weight_and_matched_names_not_a_label(self):
        prompt, _, _ = self._build()
        self.assertIn("真实穿透权重 60.0%", prompt)
        self.assertIn("RTX", prompt)
        self.assertIn("不是标的名称或分类标签", prompt)


class CallAccounting(unittest.TestCase):
    def test_generate_per_topic_adds_the_extra_call(self):
        """An arm that costs two calls per topic and reports one would look like
        the cheapest arm in the column where cost is compared."""
        # A full batch, so the harness's own top-up round does not fire and add
        # a call this test would then misread as stage one's.
        full = ([{"id": f"i{i}", "instrument_id": "ITA"}
                 for i in range(_gen.PER_TOPIC)], {})

        def build(c, t):
            return "p", 3, 1

        with mock.patch.object(_gen, "ask_json", lambda c, p: ([], 1)), \
             mock.patch.object(_gen, "mint", lambda *a, **k: full), \
             mock.patch("ideagen.strategy.spec",
                        lambda k, n: {"version": "1.0"}):
            v = _gen.generate_per_topic(ctx(), "lookthrough", build)
        self.assertEqual(v.calls, 2)            # stage one + stage two

    def test_single_stage_arms_are_unaffected(self):
        full = ([{"id": f"i{i}", "instrument_id": "ITA"}
                 for i in range(_gen.PER_TOPIC)], {})

        def build(c, t):
            return "p", 3

        with mock.patch.object(_gen, "ask_json", lambda c, p: ([], 1)), \
             mock.patch.object(_gen, "mint", lambda *a, **k: full), \
             mock.patch("ideagen.strategy.spec",
                        lambda k, n: {"version": "1.0"}):
            v = _gen.generate_per_topic(ctx(), "ai_native", build)
        self.assertEqual(v.calls, 1)


class MenuBreakpoint(unittest.TestCase):
    """Which menu a period was generated against has to survive in the run.

    The enrichment changes model input for all five arms identically, so they
    stay comparable to each other across the switch and stop being comparable to
    their own history. A note in a doc does not survive a year; the verdict does.
    """

    def _meta(self, on):
        full = ([{"id": f"i{i}", "instrument_id": "ITA"}
                 for i in range(_gen.PER_TOPIC)], {})
        env = {"IDEAGEN_UNIVERSE_LOOKTHROUGH": "1"} if on else {}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(_gen, "ask_json", lambda c, p: ([], 1)), \
             mock.patch.object(_gen, "mint", lambda *a, **k: full), \
             mock.patch("ideagen.strategy.spec",
                        lambda k, n: {"version": "1.0"}):
            if not on:
                import os
                os.environ.pop("IDEAGEN_UNIVERSE_LOOKTHROUGH", None)
            return _gen.generate_per_topic(ctx(), "ai_native",
                                           lambda c, t: ("p", 3)).meta

    def test_recorded_when_off(self):
        self.assertIs(self._meta(False)["universe_lookthrough"], False)

    def test_recorded_when_on(self):
        self.assertIs(self._meta(True)["universe_lookthrough"], True)


class Registration(unittest.TestCase):
    def test_registered_exploratory(self):
        from ideagen import strategy as strat
        spec = strat.spec("idea_generator", "lookthrough")
        self.assertEqual(spec["role"], "exploratory")
        self.assertEqual(spec["label"], "穿透反查")


if __name__ == "__main__":
    unittest.main()
