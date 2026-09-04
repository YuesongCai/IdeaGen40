"""筛选B 的共享机件：模型回答怎么被读进来，以及「没有」和「坏了」的区别。

The distinction these guard is the one the module's own docstrings keep making:
a generator that returns nothing because the corpus does not support the idea,
and a generator that returns nothing because something broke, are opposite
findings. Anything that collapses them makes a bad week look like a bad build.
"""

from __future__ import annotations

import os
import unittest
from datetime import date

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen.strategies import _gen                    # noqa: E402
from ideagen.strategy import RunContext, Verdict, run  # noqa: E402


class ParseJsonReadsReasoningModels(unittest.TestCase):
    """Observed from deepseek-v4-pro on 2026-09-05, answering a tightened
    prompt: `[]</think>[]` — the real answer on both sides of a stray closing
    tag. Left in, `rfind` spans the tag and the parse throws, so 「材料里没有
    可写的」 lands in `topic_errors` as a failed topic."""

    def test_stray_closing_tag_with_an_empty_answer(self):
        self.assertEqual(_gen.parse_json("[]</think>[]"), [])

    def test_full_think_block(self):
        self.assertEqual(_gen.parse_json('<think>先看材料</think>[{"a": 1}]'),
                         [{"a": 1}])

    def test_scratchpad_then_a_fenced_answer(self):
        self.assertEqual(
            _gen.parse_json('我先想一下……</think>\n```json\n[{"a": 2}]\n```'),
            [{"a": 2}])

    def test_plain_answers_are_untouched(self):
        self.assertEqual(_gen.parse_json('[{"a": 3}]'), [{"a": 3}])
        self.assertEqual(_gen.parse_json('```json\n[{"a": 4}]\n```'), [{"a": 4}])

    def test_genuinely_unparseable_still_raises(self):
        """A parser that swallowed everything would hide the failure it exists
        to report."""
        with self.assertRaises(ValueError):
            _gen.parse_json("我觉得这个主题目前没有特别好的机会。")


class NothingToSayIsNotAFailure(unittest.TestCase):
    def _ctx(self):
        return RunContext(
            as_of=date(2026, 9, 5), inputs_sha="x",
            corpus=[{"doc_id": "feed:1", "published_d": "2026-09-03",
                     "title": "欧洲财政", "summary": "s", "body": "b"}],
            topics=[{"topic_id": "EUROPE-FISCAL", "label": "欧洲财政",
                     "terms": ["欧洲"]}],
            universe=[{"instrument_id": "IEUR", "name": "Europe ETF",
                       "exposure": "EU equity", "vehicle": "ETF"}],
            calendar=[], infer=_Empty())

    def test_an_empty_answer_yields_an_empty_verdict_not_an_error(self):
        v = _gen.generate_per_topic(self._ctx(), "carl_constraint",
                                    lambda ctx, t, card=None: ("p", 1))
        self.assertIsInstance(v, Verdict)
        self.assertEqual(v.produced, [])
        self.assertEqual(v.meta["topic_errors"], {})
        self.assertEqual(v.meta["per_topic"], {"EUROPE-FISCAL": 0})

    def test_a_broken_call_is_still_an_error(self):
        ctx = self._ctx().with_(infer=_Boom())
        with self.assertRaises(RuntimeError) as e:
            _gen.generate_per_topic(ctx, "carl_constraint",
                                    lambda c, t, card=None: ("p", 1))
        self.assertIn("所有主题都失败", str(e.exception))


class _Completion:
    def __init__(self, text): self.text, self.model, self.usage = text, "t", {}


class _Empty:
    def complete(self, *a, **k): return _Completion("[]</think>[]")


class _Boom:
    def complete(self, *a, **k): raise RuntimeError("端口没连上")


if __name__ == "__main__":
    unittest.main()
