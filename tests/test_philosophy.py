"""Tests for PM 语义注入 — the invariants that keep a knob from becoming a leak.

Each test here corresponds to one way a free-text injection could quietly ruin
the study, so a failure names the damage rather than the assertion:

* the control arm's prompt drifting, which would end the comparison;
* a card reaching the shared plumbing, which would move the ruler;
* a directive that no output field can testify to, which would make the
  philosophy unfalsifiable;
* a card applying to weeks it predates, which would manufacture a record.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen import philosophy                              # noqa: E402
from ideagen.strategies import _gen, gen_carl               # noqa: E402
from ideagen.strategy import RunContext                     # noqa: E402


def a_card(**kw):
    card = {
        "card_id": "pm-2026-09-04-forced-seller",
        "as_of": "2026-09-04",
        "source_utterance": "我不买已经被讲烂的东西，我要的是被迫的卖家",
        "scope": {"stage": "idea_generator", "arm": "carl_constraint"},
        "directives": ["第二步「真实动机」必须指名一个被合同、监管或赎回期限"
                       "逼着行动的主体，而不是一个看多的主体"],
        "forbids": ["以「市场共识正在形成」作为论据"],
        "require": [{"field": "forced_seller",
                     "desc": "谁被迫、被什么条款逼着、期限落在哪一天"}],
        "rationale": "把「被迫的卖家」变成每条想法必须指名的一方",
        "touches_frozen": [],
        "founding_check": "与初心一致：仍是语义分析驱动的一个月动量交易",
        "distilled_by": "test",
    }
    card.update(kw)
    return card


def a_ctx(as_of=date(2026, 9, 4)):
    return RunContext(
        as_of=as_of, inputs_sha="x",
        corpus=[{"doc_id": "feed:1", "published_d": "2026-09-02",
                 "title": "欧洲财政", "summary": "s", "body": "b"}],
        topics=[{"topic_id": "EUROPE-FISCAL", "label": "欧洲财政",
                 "terms": ["欧洲"]}],
        universe=[{"instrument_id": "IEUR", "name": "Europe ETF",
                   "exposure": "EU equity", "vehicle": "ETF"}],
        calendar=[])


class ControlStaysFrozen(unittest.TestCase):
    def test_prompt_without_card_is_byte_identical(self):
        """The control arm is only a control while its prompt never moves."""
        ctx = a_ctx()
        p1, _ = gen_carl.build_prompt(ctx, ctx.topics[0])
        p2, _ = gen_carl.build_prompt(ctx, ctx.topics[0], card=None)
        self.assertEqual(p1, p2)
        self.assertNotIn("PM 注入", p1)

    def test_card_adds_only_its_own_block(self):
        ctx = a_ctx()
        base, _ = gen_carl.build_prompt(ctx, ctx.topics[0])
        with_card, _ = gen_carl.build_prompt(ctx, ctx.topics[0], card=a_card())
        self.assertIn(philosophy.render(a_card()), with_card)
        # Everything the control said, the derived arm still says: the card is
        # an addition, never a replacement.
        for block in base.split("\n\n"):
            self.assertIn(block, with_card)


class FrozenPlumbingIsOutOfReach(unittest.TestCase):
    def test_rejects_a_card_that_declares_it_touched_frozen(self):
        bad = philosophy.problems(a_card(touches_frozen=["universe"]),
                                  known_arms={"carl_constraint"})
        self.assertTrue(any("不可注入区" in b for b in bad), bad)

    def test_rejects_a_card_that_touches_frozen_in_prose(self):
        """The model was told the boundary; this is the check that does not
        depend on it having listened."""
        for text, _why in (("允许做空表达", "direction"),
                           ("可以用清单之外的自选标的", "universe"),
                           ("把持有期放宽到三个月", "horizon"),
                           ("这类想法不用引用材料", "citations")):
            bad = philosophy.problems(a_card(directives=[text]),
                                      known_arms={"carl_constraint"})
            self.assertTrue(any("不可注入区" in b for b in bad), (text, bad))

    def test_rejects_a_field_that_collides_with_a_system_field(self):
        bad = philosophy.problems(
            a_card(require=[{"field": "thesis", "desc": "x"}]),
            known_arms={"carl_constraint"})
        self.assertTrue(any("系统字段" in b for b in bad), bad)

    def test_rejects_injection_outside_stage_b(self):
        bad = philosophy.problems(
            a_card(scope={"stage": "topic_scorer", "arm": "hgep"}),
            known_arms={"carl_constraint"})
        self.assertTrue(any("只能注入" in b for b in bad), bad)


class PhilosophyMustBeCheckable(unittest.TestCase):
    def test_rejects_a_card_with_no_required_field(self):
        """A directive nothing has to testify to cannot be judged a month on."""
        bad = philosophy.problems(a_card(require=[]),
                                  known_arms={"carl_constraint"})
        self.assertTrue(any("无法判断" in b for b in bad), bad)

    def test_required_field_is_actually_enforced_on_output(self):
        """An idea that ignored the philosophy is dropped with a reason, not
        counted as compliance."""
        card = a_card()
        ctx = a_ctx()
        keys = ("anomaly", "motive", "constraint", "trigger") \
            + philosophy.require_keys(card)
        row = {"instrument_id": "IEUR", "anomaly": "a", "motive": "m",
               "constraint": "c", "trigger": "t", "thesis": "th",
               "upside_pct": 8, "downside_pct": -5,
               "p_up": .4, "p_base": .4, "p_down": .2, "citations": ["feed:1"]}
        kept, dropped = _gen.mint([row], ctx, ctx.topics[0], "x",
                                  require_keys=keys, extra_keys=keys)
        self.assertEqual(kept, [])
        self.assertTrue(any("forced_seller" in v for v in dropped.values()),
                        dropped)

        kept, dropped = _gen.mint([{**row, "forced_seller": "某养老金 11/30 到期"}],
                                  ctx, ctx.topics[0], "x",
                                  require_keys=keys, extra_keys=keys)
        self.assertEqual(len(kept), 1, dropped)
        self.assertEqual(kept[0]["forced_seller"], "某养老金 11/30 到期")


class LedgerIsAppendOnlyAndAsOf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._real = philosophy.LEDGER
        philosophy.LEDGER = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        philosophy.LEDGER = self._real
        self.tmp.cleanup()

    def test_activate_then_retire_leaves_both_events(self):
        philosophy.activate(a_card(), known_arms={"carl_constraint"})
        self.assertEqual(len(philosophy.cards()), 1)
        philosophy.retire("pm-2026-09-04-forced-seller", date(2026, 10, 1), "试完了")
        self.assertEqual(philosophy.cards(), [])
        self.assertEqual(len(philosophy.cards(include_retired=True)), 1)
        # Two events on file, neither overwritten.
        self.assertEqual(len(philosophy.LEDGER.read_text().strip().split("\n")), 2)

    def test_a_card_does_not_apply_before_its_own_date(self):
        philosophy.activate(a_card(), known_arms={"carl_constraint"})
        self.assertEqual(philosophy.cards(as_of=date(2026, 8, 20)), [])
        self.assertEqual(len(philosophy.cards(as_of=date(2026, 9, 4))), 1)

    def test_same_sentence_cannot_be_registered_twice(self):
        philosophy.activate(a_card(), known_arms={"carl_constraint"})
        with self.assertRaises(ValueError):
            philosophy.activate(a_card(card_id="pm-2026-09-11-forced-seller",
                                       as_of="2026-09-11"),
                                known_arms={"carl_constraint"})


class DerivedArmRegisters(unittest.TestCase):
    def test_arm_name_and_version_carry_the_card(self):
        card = a_card()
        self.assertEqual(philosophy.arm_name(card),
                         "carl_constraint@pm-2026-09-04-forced-seller")

    def test_derived_arm_refuses_a_run_it_predates(self):
        from ideagen.strategies import gen_pm
        run = gen_pm._make(a_card())
        with self.assertRaises(RuntimeError) as e:
            run(a_ctx(as_of=date(2026, 8, 20)))
        self.assertIn("后见之明", str(e.exception))


if __name__ == "__main__":
    unittest.main()
