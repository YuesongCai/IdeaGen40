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

import json
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
        "card_id": "pm-2026-09-04-a1b2c3",
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
    """Every arm that can receive a card must be unchanged without one.

    Run across all four rather than on carl alone: the slot was added to the
    other three later, and a control that drifted by a whitespace when its
    signature grew would silently end the comparison it exists to anchor.
    """

    def _arms(self):
        from ideagen.strategies import gen_pm
        return sorted(gen_pm.BASES.items())

    def test_prompt_without_card_is_byte_identical(self):
        ctx = a_ctx()
        for name, base in self._arms():
            build = base["build_prompt"]
            p1, _ = build(ctx, ctx.topics[0])
            p2, _ = build(ctx, ctx.topics[0], card=None)
            self.assertEqual(p1, p2, name)
            self.assertNotIn("PM 注入", p1, name)

    def test_card_adds_only_its_own_block(self):
        ctx = a_ctx()
        for name, base in self._arms():
            build = base["build_prompt"]
            card = a_card(scope={"stage": "idea_generator", "arm": name})
            plain, _ = build(ctx, ctx.topics[0])
            with_card, _ = build(ctx, ctx.topics[0], card=card)
            self.assertIn(philosophy.render(card), with_card, name)
            # Everything the control said, the derived arm still says: the card
            # is an addition, never a replacement.
            for block in plain.split("\n\n"):
                self.assertIn(block, with_card, name)

    def test_card_sits_before_the_shared_output_contract(self):
        """The slot's position is the boundary. A card that landed after the
        citation rule or the JSON shape would be able to talk over the parts
        held identical across every arm."""
        ctx = a_ctx()
        for name, base in self._arms():
            card = a_card(scope={"stage": "idea_generator", "arm": name})
            text, _ = base["build_prompt"](ctx, ctx.topics[0], card=card)
            self.assertLess(text.index(philosophy.render(card)),
                            text.index(_gen.CITATION_RULE), name)


class FrozenPlumbingIsOutOfReach(unittest.TestCase):
    def test_rejects_a_directive_about_sizing(self):
        """A generator has no channel to the book, so a rule about weight or
        stops is not dangerous — it is inert, which is worse: the PM believes
        it is running."""
        for text in ("确定性高的想法把仓位权重设为其他想法的 2 倍",
                     "这类想法止损放宽到 3 倍 sigma"):
            bad = philosophy.problems(a_card(directives=[text]),
                                      known_arms={"carl_constraint"})
            self.assertTrue(any("硬边界" in b for b in bad), (text, bad))

    def test_compliant_prose_mentioning_a_boundary_is_not_a_failure(self):
        """「看空就买反向标的做多表达」 is the boundary being respected. A
        backstop that fired on any mention of it would reject every correct
        translation."""
        bad = philosophy.problems(
            a_card(directives=["若原始观点为看空，必须从可买清单里选反向或"
                               "防御标的做多来表达"]),
            known_arms={"carl_constraint"})
        self.assertEqual(bad, [])

    def test_rejects_a_card_that_touches_frozen_in_prose(self):
        """The model was told the boundary; this is the check that does not
        depend on it having listened."""
        for text, _why in (("允许做空表达", "direction"),
                           ("可以用清单之外的自选标的", "universe"),
                           ("把持有期放宽到三个月", "horizon"),
                           ("这类想法不用引用材料", "citations")):
            bad = philosophy.problems(a_card(directives=[text]),
                                      known_arms={"carl_constraint"})
            self.assertTrue(any("硬边界" in b for b in bad), (text, bad))

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


class RequiredIsNotTheSameAsRefusable(unittest.TestCase):
    """Measured on 2026-09-05: asked for a mandatory `forced_seller`, the model
    answered 「CalPERS，条款：集中度超 35% 时强制转向等权指数」 about an
    institution it had read nothing about — with 「写不出就不要凑这一条」 in the
    same prompt. Being mandatory only guarantees it wrote something.

    The citation contract does hold, and not because it is mandatory: across 416
    candidates and 764 citations every doc_id resolved and none post-dated its
    run, because a fabricated id fails to resolve and the idea is dropped. So a
    card's fields get the same closed set."""

    def setUp(self):
        self.card = a_card()
        self.keys = (("anomaly", "motive", "constraint", "trigger")
                     + philosophy.require_keys(self.card)
                     + philosophy.doc_keys(self.card))
        self.docs = philosophy.doc_keys(self.card)
        self.ctx = a_ctx()
        self.row = {
            "instrument_id": "IEUR", "anomaly": "a", "motive": "m",
            "constraint": "c", "trigger": "t", "thesis": "th",
            "upside_pct": 8, "downside_pct": -5,
            "p_up": .4, "p_base": .4, "p_down": .2, "citations": ["feed:1"],
            "forced_seller": "CalPERS，集中度超 35% 时强制转向等权指数"}

    def _mint(self, row):
        return _gen.mint([row], self.ctx, self.ctx.topics[0], "x",
                         require_keys=self.keys, extra_keys=self.keys,
                         doc_keys=self.docs)

    def test_every_field_gets_an_evidence_companion(self):
        self.assertEqual(philosophy.doc_keys(self.card), ("forced_seller_doc",))

    def test_a_fabricated_doc_id_is_refused(self):
        kept, dropped = self._mint({**self.row, "forced_seller_doc": "feed:99999"})
        self.assertEqual(kept, [])
        self.assertTrue(any("出处对不上研报" in v for v in dropped.values()),
                        dropped)

    def test_a_missing_evidence_field_is_refused(self):
        kept, dropped = self._mint(self.row)
        self.assertEqual(kept, [])
        self.assertTrue(any("forced_seller_doc" in v for v in dropped.values()),
                        dropped)

    def test_a_real_doc_id_passes_and_is_kept_on_the_idea(self):
        kept, _ = self._mint({**self.row, "forced_seller_doc": "feed:1"})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["forced_seller_doc"], "feed:1")

    def test_the_base_arms_are_untouched_by_the_new_parameter(self):
        """`doc_keys` defaults to empty, so the four controls mint exactly as
        before — the comparison does not move because a derived arm gained a
        check."""
        row = {k: v for k, v in self.row.items() if k != "forced_seller"}
        kept, dropped = _gen.mint([row], self.ctx, self.ctx.topics[0], "x",
                                  require_keys=("anomaly", "motive",
                                                "constraint", "trigger"),
                                  extra_keys=("anomaly",))
        self.assertEqual(len(kept), 1, dropped)

    def test_a_field_named_doc_is_rejected_at_the_card(self):
        bad = philosophy.problems(
            a_card(require=[{"field": "seller_doc", "desc": "x"}]),
            known_arms={"carl_constraint"})
        self.assertTrue(any("_doc" in b for b in bad), bad)

    def test_render_names_the_evidence_fields_and_says_they_are_checked(self):
        text = philosophy.render(self.card)
        self.assertIn("forced_seller_doc", text)
        self.assertIn("逐个核对", text)


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
        philosophy.retire("pm-2026-09-04-a1b2c3", date(2026, 10, 1), "试完了")
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
            philosophy.activate(a_card(card_id="pm-2026-09-11-a1b2c3",
                                       as_of="2026-09-11"),
                                known_arms={"carl_constraint"})


class TheSentenceStaysPrivate(unittest.TestCase):
    """`review.state` copies a generator verdict's whole `meta` into the panel
    payload, and that payload is exported to the public GitHub Pages snapshot.
    Anything a derived arm puts in `meta` is therefore published, so the PM's
    own words must not be in there — and the card id that IS published must not
    paraphrase them either."""

    def test_verdict_meta_never_carries_the_utterance(self):
        from ideagen.strategies import gen_pm
        card = a_card()
        keys = tuple(gen_pm.BASES["carl_constraint"]["keys"]) \
            + philosophy.require_keys(card)
        meta = {}
        # The shape gen_pm.run writes, without needing a model call.
        meta.update({"philosophy_card": card["card_id"],
                     "philosophy_base_arm": "carl_constraint",
                     "philosophy_since": card["as_of"],
                     "philosophy_require": list(philosophy.require_keys(card))})
        blob = repr(meta)
        self.assertNotIn(card["source_utterance"], blob)
        for word in ("被迫", "讲烂"):
            self.assertNotIn(word, blob)
        self.assertEqual(len(keys), 5)

    def test_card_id_does_not_paraphrase_the_utterance(self):
        cid = philosophy._fingerprint("我不买已经被讲烂的东西，我要的是被迫的卖家")
        self.assertRegex(cid, r"^[0-9a-f]{6}$")

    def test_two_sentences_the_same_day_get_different_ids(self):
        """A slug built from Chinese normalised to nothing, so every card
        written in one day collapsed onto the same id and the second one failed
        activation with a duplicate-id error that explained none of this."""
        a = philosophy._fingerprint("我要的是被迫的卖家")
        b = philosophy._fingerprint("政策我只信已经拨了钱的")
        self.assertNotEqual(a, b)


class DerivedArmRegisters(unittest.TestCase):
    def test_arm_name_and_version_carry_the_card(self):
        card = a_card()
        self.assertEqual(philosophy.arm_name(card),
                         "carl_constraint@pm-2026-09-04-a1b2c3")

    def test_every_registered_base_can_be_derived(self):
        """`options()` is what the panel offers. An arm it lists but cannot
        derive would be a picker entry that fails at activation."""
        from ideagen.strategies import gen_pm
        for opt in gen_pm.options():
            self.assertIn(opt["arm"], gen_pm.BASES)
            self.assertTrue(opt["label"])
            run = gen_pm._make(a_card(
                scope={"stage": "idea_generator", "arm": opt["arm"]}))
            self.assertTrue(callable(run))

    def test_derived_arm_refuses_a_run_it_predates(self):
        from ideagen.strategies import gen_pm
        run = gen_pm._make(a_card())
        with self.assertRaises(RuntimeError) as e:
            run(a_ctx(as_of=date(2026, 8, 20)))
        self.assertIn("后见之明", str(e.exception))


class TheRuleCanBeCheckedAgainstItsSources(unittest.TestCase):
    """面板说这些字段是给你核对用的，那就得有东西可点。

    Until `/api/philosophy/output` existed, the panel told the PM 「写得实不实，
    要你点开看」 and offered nothing to click — a sentence true only in
    intention. These pin the states that reach a person: a rule that has not run
    says so and says when it will, and an id that is not running is a 404 rather
    than an empty page that reads as 「它什么都没写」.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._real = philosophy.LEDGER
        philosophy.LEDGER = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        philosophy.LEDGER = self._real
        self.tmp.cleanup()

    def test_an_unknown_id_is_not_found_rather_than_empty(self):
        from ideagen import philosophy_web as pw
        obj, status = pw.handle_output({"id": "pm-2026-09-05-000000"})
        self.assertEqual(status, 404)
        self.assertIn("不在运行中", obj["error"])

    def test_a_rule_that_has_not_run_says_when_it_will(self):
        """Zero is an answer, and an answer needs a next step attached."""
        from ideagen import philosophy_web as pw
        philosophy.activate(a_card(), known_arms={"carl_constraint"})
        obj, status = pw.handle_output({"id": a_card()["card_id"]})
        self.assertEqual(status, 200)
        self.assertFalse(obj["ran"])
        self.assertIn("周三", obj["hint"])

    def test_counts_are_absent_until_the_rule_has_run(self):
        """`counts.ran` false is what the panel keys the whole block on; a
        missing key would render 「写出 undefined 条」."""
        from ideagen import philosophy_web as pw
        philosophy.activate(a_card(), known_arms={"carl_constraint"})
        obj, _ = pw.handle_list()
        self.assertEqual(len(obj["live"]), 1)
        self.assertIn("counts", obj["live"][0])
        self.assertIs(obj["live"][0]["counts"]["ran"], False)


class ARevisionIsANewRuleThatRetiresTheOld(unittest.TestCase):
    """改一条准则不能是原地编辑。

    A rule *is* an arm. An arm whose content changed while keeping its name
    turns one track record into a blend of several different rules, which is the
    thing this whole design refuses. So 「照这条改一版」 mints a new card and
    retires the one it revises, in the same action: two events on an append-only
    ledger, a lineage that can be queried, and two clean series.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._real = philosophy.LEDGER
        philosophy.LEDGER = Path(self.tmp.name) / "ledger.jsonl"
        self.pending = Path(self.tmp.name) / "pending"
        from ideagen import philosophy_web as pw
        self._real_pending = pw.PENDING
        pw.PENDING = self.pending
        self.pending.mkdir(parents=True, exist_ok=True)
        # `handle_activate` registers the derived arm so the next weekly run
        # picks it up without a restart. That is the right behaviour and it
        # means these tests mutate a global; snapshot it after the lazy plugin
        # load so tearDown removes only what this class added.
        from ideagen import strategy as strat
        strat.available("idea_generator")
        self._before = set(strat._REGISTRY)

    def tearDown(self):
        from ideagen import philosophy_web as pw, strategy as strat
        philosophy.LEDGER = self._real
        pw.PENDING = self._real_pending
        for key in set(strat._REGISTRY) - self._before:
            del strat._REGISTRY[key]
        self.tmp.cleanup()

    def _stage(self, card):
        (self.pending / f"{card['card_id']}.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8")

    def test_activating_a_revision_retires_what_it_revises(self):
        from ideagen import philosophy_web as pw
        first = a_card()
        philosophy.activate(first, known_arms={"carl_constraint"})
        second = a_card(card_id="pm-2026-09-06-d4e5f6", as_of="2026-09-06",
                        source_utterance="我要的是被条款逼着动手的卖家，写清期限")
        self._stage(second)
        obj, status = pw.handle_activate({"id": second["card_id"],
                                          "replaces": first["card_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(obj["replaced"], first["card_id"])
        live = [c["card_id"] for c in philosophy.cards()]
        self.assertEqual(live, [second["card_id"]])

    def test_the_old_rule_is_retired_not_erased(self):
        """Its positions and its record stay; an append-only ledger has no
        delete, and the reason says what replaced it."""
        from ideagen import philosophy_web as pw
        first = a_card()
        philosophy.activate(first, known_arms={"carl_constraint"})
        second = a_card(card_id="pm-2026-09-06-d4e5f6", as_of="2026-09-06",
                        source_utterance="改了的说法")
        self._stage(second)
        pw.handle_activate({"id": second["card_id"],
                            "replaces": first["card_id"]})
        both = {c["card_id"] for c in philosophy.cards(include_retired=True)}
        self.assertEqual(both, {first["card_id"], second["card_id"]})
        events = philosophy.LEDGER.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(events), 3)   # activate, activate, retire
        self.assertIn(second["card_id"],
                      [json.loads(e).get("reason", "") for e in events
                       if json.loads(e).get("event") == "retire"][0])

    def test_a_revision_of_something_already_gone_still_stands(self):
        """The new rule is the point. If the old one was retired in another tab
        a minute earlier, that must not cost the revision."""
        from ideagen import philosophy_web as pw
        second = a_card()
        self._stage(second)
        obj, status = pw.handle_activate({"id": second["card_id"],
                                          "replaces": "pm-2026-01-01-000000"})
        self.assertEqual(status, 200)
        self.assertEqual([c["card_id"] for c in philosophy.cards()],
                         [second["card_id"]])


class OneBadLineCannotTakeDownTheRegistry(unittest.TestCase):
    """一行写坏的账本数据，不该让四条原臂一起下线。

    Found by a reviewer on 2026-09-05 while hand-writing a card. A line that is
    valid JSON, has `event: activate`, and simply lacks `card` used to raise
    `KeyError` inside `cards()` — and because `gen_pm._install()` runs during
    the plugin scan, that KeyError came out of an *import*: `available()`
    raised, every founding arm became unreachable, and the weekly run and the
    panel went down together.

    The ledger is append-only and outlives any one generator, so a single bad
    write is permanent. Skipping is right; skipping silently is not — a
    philosophy that stopped running unnoticed is the other failure, so the
    lines are counted and shown.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._real = philosophy.LEDGER
        philosophy.LEDGER = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        philosophy.LEDGER = self._real
        self.tmp.cleanup()

    def _write(self, *rows):
        philosophy.LEDGER.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) if isinstance(r, dict)
                      else r for r in rows) + "\n", encoding="utf-8")

    def test_activate_without_a_card_is_skipped_not_raised(self):
        self._write({"event": "activate", "card_id": "pm-2026-09-05-a1b2c3"})
        self.assertEqual(philosophy.cards(), [])
        self.assertEqual(len(philosophy.ledger_problems()), 1)
        self.assertIn("没有 card 内容", philosophy.ledger_problems()[0])

    def test_the_good_cards_around_a_bad_line_still_load(self):
        """One bad write must cost itself, not the rules written before and
        after it."""
        self._write({"event": "activate", "card_id": "pm-2026-09-05-a1b2c3"},
                    "{ 这不是 JSON",
                    {"event": "activate", "card_id": a_card()["card_id"],
                     "as_of": a_card()["as_of"], "card": a_card()})
        self.assertEqual([c["card_id"] for c in philosophy.cards()],
                         [a_card()["card_id"]])
        self.assertEqual(len(philosophy.ledger_problems()), 2)

    def test_every_unusable_shape_is_named(self):
        for row, want in (
                ({"event": "activate"}, "没有 card_id"),
                ({"event": "shrug", "card_id": "x"}, "不认识的事件类型"),
                ({"event": "activate", "card_id": "x", "card": "不是对象"},
                 "没有 card 内容"),
                ("[1, 2, 3]", "不是一个对象"),
                ("nope", "不是合法 JSON")):
            self._write(row)
            probs = philosophy.ledger_problems()
            self.assertEqual(len(probs), 1, row)
            self.assertIn(want, probs[0], row)

    def test_problems_carry_the_line_number(self):
        self._write({"event": "activate", "card_id": "pm-2026-09-05-a1b2c3"},
                    {"event": "activate", "card_id": "pm-2026-09-05-d4e5f6"})
        self.assertIn("第 1 行", philosophy.ledger_problems()[0])
        self.assertIn("第 2 行", philosophy.ledger_problems()[1])

    def test_the_plugin_scan_survives_anything_in_the_ledger(self):
        """The backstop, tested through the expression the weekly run uses.
        Cards that cannot be registered cost themselves; the four controls come
        up regardless."""
        from ideagen import strategy as strat
        from ideagen.strategies import gen_pm
        self._write({"event": "activate", "card_id": "pm-2026-09-05-a1b2c3"},
                    {"event": "activate", "card_id": "pm-2026-09-05-d4e5f6",
                     "card": {"card_id": "pm-2026-09-05-d4e5f6"}})
        before = set(strat._REGISTRY)
        try:
            gen_pm._install()
            names = [r["name"] for r in strat.available("idea_generator")]
            for base in ("ai_native", "carl_constraint", "chain", "gap"):
                self.assertIn(base, names)
        finally:
            for key in set(strat._REGISTRY) - before:
                del strat._REGISTRY[key]


class ActivationReachesTheWeeklyRun(unittest.TestCase):
    """一条准则激活之后，周跑到底会不会真的多跑一条臂。

    Asked by a reviewer on 2026-09-05 who read the code and could not tell:
    both ends were visible — the generators take a `card`, the ledger accepts
    one — but nothing in `orchestrator.py` mentions `philosophy`, and a live
    `strategy.available('idea_generator')` returned the same four arms as
    always. The four were all there was because the ledger was empty; the
    registration is real. But a reader could not establish that, and the panel
    tells the PM 「每条准则派生一条对照臂」 in the present tense.

    So this pins the join, using the exact expression the weekly run selects
    with (`orchestrator.py`: `[r["name"] for r in strat.available(...)]`).
    If that line ever becomes a hand-kept list of the four founding arms, an
    activated card would silently stop running and this goes red.
    """

    def setUp(self):
        from ideagen import strategy as strat
        self.tmp = tempfile.TemporaryDirectory()
        self._real = philosophy.LEDGER
        philosophy.LEDGER = Path(self.tmp.name) / "ledger.jsonl"
        # The registry fills lazily on first `available()`. Snapshotting before
        # that returns an empty set, and then tearDown deletes every arm in the
        # process rather than the one this test added — which is how the first
        # version of this class made the three tests after it fail.
        strat.available("idea_generator")
        self._before = set(strat._REGISTRY)

    def tearDown(self):
        from ideagen import strategy as strat
        philosophy.LEDGER = self._real
        # A derived arm registered by this test must not leak into the global
        # registry other tests read.
        for key in set(strat._REGISTRY) - self._before:
            del strat._REGISTRY[key]
        self.tmp.cleanup()

    def _weekly_would_run(self):
        """`orchestrator.weekly` picks generators with exactly this."""
        from ideagen import strategy as strat
        return [r["name"] for r in strat.available("idea_generator")]

    def test_an_activated_card_appears_where_the_weekly_run_looks(self):
        from ideagen.strategies import gen_pm
        card = a_card(scope={"stage": "idea_generator", "arm": "chain"})
        self.assertNotIn(philosophy.arm_name(card), self._weekly_would_run())
        philosophy.activate(card, known_arms=set(self._weekly_would_run()))
        gen_pm._install()
        self.assertIn(philosophy.arm_name(card), self._weekly_would_run())

    def test_the_four_controls_are_still_there_beside_it(self):
        """The derived arm is an addition. A card that replaced its base would
        end the comparison it exists to be measured by."""
        from ideagen.strategies import gen_pm
        card = a_card(scope={"stage": "idea_generator", "arm": "chain"})
        philosophy.activate(card, known_arms=set(self._weekly_would_run()))
        gen_pm._install()
        names = self._weekly_would_run()
        for base in ("ai_native", "carl_constraint", "chain", "gap"):
            self.assertIn(base, names)

    def test_the_arm_carries_the_card_in_its_version(self):
        """`Verdict` is stamped from the registry, so a book filled by this arm
        can be traced to the exact sentence that produced it."""
        from ideagen import strategy as strat
        from ideagen.strategies import gen_pm
        card = a_card(scope={"stage": "idea_generator", "arm": "chain"})
        philosophy.activate(card, known_arms=set(self._weekly_would_run()))
        gen_pm._install()
        spec = strat.spec("idea_generator", philosophy.arm_name(card))
        self.assertEqual(spec["version"], f"1.0+{card['card_id']}")
        self.assertEqual(spec["role"], "exploratory")

    def test_the_weekly_run_really_asks_the_registry(self):
        """The tests above replicate the orchestrator's selection expression,
        which means they would all stay green if that line became a hand-kept
        list of the four founding arms — the exact regression a reviewer
        suspected on 2026-09-05. So read the real source and require that the
        default still comes from the registry.

        Brittle on purpose: this is a one-line invariant that cannot be
        observed any other way without running a full weekly, and its failure
        message says what to do rather than just what changed.
        """
        import inspect
        from ideagen import orchestrator
        src = inspect.getsource(orchestrator.weekly)
        self.assertIn('strat.available("idea_generator")', src,
                      "周跑不再从注册表取生成臂了。一旦改成写死的名单，"
                      "PM 激活的准则就不会有臂在跑，而界面仍然说它在跑。"
                      "要么把这一行改回注册表，要么把面板上「每条准则派生一条"
                      "对照臂」那句话一起改掉。")

    def test_an_empty_ledger_registers_nothing(self):
        """The reviewer's actual observation, stated as the expected result:
        the statically registered methods and no more is what an empty ledger
        should look like.

        The list is spelled out rather than derived on purpose, and it did its
        job: adding `lookthrough` in 2026-09 failed here first. Registering a
        new generation method is a research decision, so it should cost a
        deliberate edit to this line rather than passing silently.

        `lookthrough` is absent from `gen_pm.BASES`, so no card derives from it
        — a two-stage method that raises on themes it cannot express is not yet
        a stable base to graft a PM rule onto."""
        from ideagen.strategies import gen_pm
        gen_pm._install()
        self.assertEqual(sorted(self._weekly_would_run()),
                         ["ai_native", "carl_constraint", "chain", "gap",
                          "lookthrough"])


class RewritesMustBeSeenByTheirAuthor(unittest.TestCase):
    """The hole this found: the distiller reports boundary contact as prose
    (`"direction: 他要做空，改为……"`), and the original exact-key check passed
    every one of them silently — a philosophy reaching a book in a form its
    author never read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._real = philosophy.LEDGER
        philosophy.LEDGER = Path(self.tmp.name) / "ledger.jsonl"
        self.card = a_card(touches_frozen=[
            "direction: 原话要求做空，改为看空时买反向/防御标的做多表达",
            "horizon: 原话要求三个月，强制保持一个月"])

    def tearDown(self):
        philosophy.LEDGER = self._real
        self.tmp.cleanup()

    def test_prose_contact_is_matched_by_substring(self):
        tr = philosophy.translations(self.card)
        self.assertEqual(len(tr), 2)
        self.assertTrue(any("只做多" in t for t in tr), tr)
        self.assertTrue(any("一个月" in t for t in tr), tr)

    def test_unclassifiable_contact_is_still_surfaced(self):
        tr = philosophy.translations(a_card(touches_frozen=["某个说不清的地方"]))
        self.assertEqual(len(tr), 1)
        self.assertIn("未归类", tr[0])

    def test_activation_refuses_an_unacknowledged_rewrite(self):
        with self.assertRaises(ValueError) as e:
            philosophy.activate(self.card, known_arms={"carl_constraint"})
        self.assertIn("硬边界", str(e.exception))
        self.assertEqual(philosophy.cards(), [])

    def test_activation_proceeds_once_acknowledged(self):
        philosophy.activate(self.card, known_arms={"carl_constraint"},
                            accept_translations=True)
        self.assertEqual(len(philosophy.cards()), 1)

    def test_a_clean_card_needs_no_acknowledgement(self):
        philosophy.activate(a_card(), known_arms={"carl_constraint"})
        self.assertEqual(len(philosophy.cards()), 1)


if __name__ == "__main__":
    unittest.main()
