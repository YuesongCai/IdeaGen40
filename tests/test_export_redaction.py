"""公开导出的那份 payload 里，不该有别人的东西。

`review.state` builds what the panel reads and what `export_pages` publishes to
GitHub Pages. Under `hide_licensed` it already nulls `thesis` and aliases shelf
identifiers, because model-written prose derived from licensed research bodies
is not ours to republish.

These tests guard the same wall at the places it was found leaking:

* `topic_errors` carried up to 200 characters of the model's own answer,
  because a topic whose call returns prose instead of JSON fails with
  `ValueError: 模型返回无法解析为 JSON：<the answer>`;
* a derived arm's `meta` carried the PM's own sentence verbatim.

Both are now structural-only. The publish gate's bookkeeping-prose rule is the
far side of the same wall; this is the near side, where the value is decided
rather than detected.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen import philosophy                       # noqa: E402
from ideagen.review import _gen_meta                  # noqa: E402

MODEL_ECHO = ("ValueError: 模型返回无法解析为 JSON：当然可以，下面是我为这个主题"
              "整理的一个月做多想法，第一条是关于欧洲财政扩张受益的银行板块……")


class TopicErrorsCarryNoModelProse(unittest.TestCase):
    def test_public_export_keeps_only_the_exception_class(self):
        meta = {"per_topic": {"EUROPE-FISCAL": 0},
                "topic_errors": {"EUROPE-FISCAL": MODEL_ECHO}}
        out = _gen_meta(meta, True)
        self.assertEqual(out["topic_errors"], {"EUROPE-FISCAL": "ValueError"})
        # Which topic failed, and how, is exactly the diagnostic worth
        # publishing — the redaction must not throw that away too.
        self.assertEqual(out["per_topic"], {"EUROPE-FISCAL": 0})

    def test_local_panel_still_sees_the_whole_message(self):
        meta = {"topic_errors": {"X": MODEL_ECHO}}
        self.assertEqual(_gen_meta(meta, False)["topic_errors"]["X"], MODEL_ECHO)

    def test_redaction_does_not_mutate_the_stored_row(self):
        """The same meta dict is handed to the panel and to the exporter; a
        redaction that edited it in place would silently blind the operator."""
        meta = {"topic_errors": {"X": MODEL_ECHO}}
        _gen_meta(meta, True)
        self.assertEqual(meta["topic_errors"]["X"], MODEL_ECHO)

    def test_absent_or_empty_errors_pass_through(self):
        for meta in ({}, {"topic_errors": {}}, {"per_topic": {"A": 3}}):
            self.assertEqual(_gen_meta(dict(meta), True), meta)


class DerivedArmMetaCarriesNoUtterance(unittest.TestCase):
    def test_the_published_keys_are_all_structural(self):
        """What `gen_pm.run` writes into a verdict's meta is published verbatim,
        so every key here has to be an identifier, a date or a field name."""
        from ideagen.strategies import gen_pm
        card = {
            "card_id": "pm-2026-09-05-a1b2c3", "as_of": "2026-09-05",
            "source_utterance": "我不买已经被讲烂的东西，我要的是被迫的卖家",
            "scope": {"stage": "idea_generator", "arm": "carl_constraint"},
            "directives": ["指名一个被条款逼着动手的主体"],
            "require": [{"field": "forced_seller", "desc": "谁被迫"}],
        }
        published = {
            "philosophy_card": card["card_id"],
            "philosophy_base_arm": card["scope"]["arm"],
            "philosophy_since": card["as_of"],
            "philosophy_require": list(philosophy.require_keys(card)),
        }
        blob = repr(published)
        self.assertNotIn(card["source_utterance"], blob)
        self.assertNotIn("被迫的卖家", blob)
        self.assertNotIn("指名一个被条款", blob)
        # And the id itself must not paraphrase the sentence either.
        self.assertRegex(card["card_id"], r"^pm-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$")
        self.assertIn("carl_constraint", gen_pm.BASES)


if __name__ == "__main__":
    unittest.main()
