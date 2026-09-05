"""Naming a discovered theme — the step whose absence stalled the registry.

`themes.candidates` returns evidence: phrases, counts, doc ids. `themes.validate`
requires an id, a label, a key question with a horizon, at least four synonyms
and a priceable indicator. A phrase cluster carries none of the five, so every
candidate the weekly run proposed after auto-registration was wired on
2026-08-26 was rejected the moment it arrived, and the registry stayed at the
two rows a human had curated on 08-08. Nothing failed loudly: the rejection was
recorded as `theme_register_failed` and the run continued.

The tests below fix the two halves of that in place — that a cluster genuinely
cannot pass `validate` on its own, so the naming step is load-bearing and not
decoration; and that the naming step refuses to mint the same debate twice in
one week, which is the failure mode that cannot be undone afterwards because
the registry is append-only.

No model is called here. `mint` is exercised through a stub port that replays
recorded shapes, so these assert the contract around the call, not the
judgement inside it.
"""
from __future__ import annotations

import unittest
from datetime import date

from ideagen import db, themes


class _Reply:
    def __init__(self, text: str):
        self.text = text


class _Port:
    """An inference port that answers with a scripted queue of completions."""

    def __init__(self, *texts: str):
        self.queue = list(texts)
        self.prompts: list[str] = []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return _Reply(self.queue.pop(0) if self.queue else "{}")


CLUSTER = {"terms": ["日本股票", "日股"], "n_docs": 9, "n_institutions": 5,
           "n_days": 3, "max_lift": 4.1, "evidence": []}

CARD_A = """```json
{"id": "JAPAN-EQUITY-BULL", "label": "日本股市牛市延续",
 "key_question": "未来1–6个月，日本企业盈利能否支撑日经225继续上涨？",
 "terms": ["日股牛市", "日本股市上涨", "日经225上行", "东证指数走强",
           "日本权益资产看好", "盈利驱动日股"],
 "price_indicator": "%s", "related": [], "default_direction": "↑"}
```"""

CARD_SHARED_TERM = """{"id": "JAPAN-EQUITY-SECOND", "label": "日本股市再评估",
 "key_question": "未来1-6个月，日本股市能否再度重估？",
 "terms": ["日股牛市", "日本估值修复", "日本股市重估行情", "日本市场再评价",
           "日本资产重定价", "日本股票增配"],
 "price_indicator": "%s", "related": [], "default_direction": "↑"}"""

CARD_DUP = """{"id": "JAPAN-EQUITY-STRUCTURAL-BULL", "label": "日本股市结构性牛市",
 "key_question": "未来1-6个月，治理改革能否推动日经225指数进一步重估？",
 "terms": ["日本股票看涨", "日经225目标价上调", "日本公司治理行情",
           "日本股市结构性机会", "日股重估", "日本长期牛市"],
 "price_indicator": "%s", "related": [], "default_direction": "↑"}"""


def _a_priceable_code(con) -> str | None:
    r = db.q(con, "SELECT futu_code FROM instruments WHERE COALESCE(priceable,0)=1 "
                  "AND futu_code IS NOT NULL ORDER BY futu_code LIMIT 1")
    return r[0]["futu_code"] if r else None


class TestNamingIsLoadBearing(unittest.TestCase):
    def setUp(self):
        self.con = db.init()
        self.code = _a_priceable_code(self.con)
        if not self.code:
            self.skipTest("no priceable instrument in this database")
        self.as_of = date(2026, 6, 24)

    def test_a_raw_cluster_cannot_register_itself(self):
        """The premise: this is why the registry stood still for four weeks."""
        with self.assertRaises(themes.RegistrationError) as caught:
            themes.validate(self.con, dict(CLUSTER), self.as_of)
        for field in ("id", "label", "key_question", "price_indicator"):
            self.assertIn(field, str(caught.exception))

    def test_minting_fills_exactly_what_validate_demands(self):
        card = themes.mint(self.con, dict(CLUSTER), self.as_of,
                           _Port(CARD_A % self.code))
        self.assertEqual(card["id"], "JAPAN-EQUITY-BULL")
        self.assertEqual(card["origin"], "discovered")
        self.assertEqual(card["registered_d"], self.as_of.isoformat())
        self.assertGreaterEqual(len(card["terms"]), 4)
        self.assertTrue(card["provenance"], "a minted card must say what it came from")

    def test_a_shared_synonym_within_the_week_is_refused(self):
        """The mechanical half, because the registry cannot be edited.

        On 2026-06-24 the clusters 「日本股票」 and 「日经」 were one debate and
        both produced a clean card. Two rows would double-count the same reports
        in D for good, so a second card reusing a phrase is stopped here.
        """
        first = themes.mint(self.con, dict(CLUSTER), self.as_of,
                            _Port(CARD_A % self.code))
        port = _Port(CARD_SHARED_TERM % self.code, CARD_SHARED_TERM % self.code)
        with self.assertRaises(themes.RegistrationError) as caught:
            themes.mint(self.con, dict(CLUSTER), self.as_of, port, minted=[first])
        self.assertIn("JAPAN-EQUITY-BULL", str(caught.exception))

    def test_the_weeks_earlier_cards_reach_the_prompt(self):
        """The other half is the model's, so it has to be told."""
        first = themes.mint(self.con, dict(CLUSTER), self.as_of,
                            _Port(CARD_A % self.code))
        port = _Port('{"skip": "与 JAPAN-EQUITY-BULL 重复"}')
        with self.assertRaises(themes.MintSkipped):
            themes.mint(self.con, dict(CLUSTER), self.as_of, port, minted=[first])
        self.assertIn("JAPAN-EQUITY-BULL", port.prompts[0])
        self.assertIn("日经225上行", port.prompts[0])

    def test_a_reworded_duplicate_passes_the_mechanical_check(self):
        """The limitation, asserted so it stays a known one.

        CARD_DUP argues the same Japanese-equity bull case as CARD_A and shares
        not one phrase with it — 「日经225上行」 against 「日经225目标价上调」.
        `_overlaps` cannot see it, and only the model's own refusal stands
        between that and a second registry row. Recorded here so that a future
        reader does not mistake the guard for complete.
        """
        first = themes.mint(self.con, dict(CLUSTER), self.as_of,
                            _Port(CARD_A % self.code))
        second = themes.mint(self.con, dict(CLUSTER), self.as_of,
                             _Port(CARD_DUP % self.code), minted=[first])
        self.assertEqual(second["id"], "JAPAN-EQUITY-STRUCTURAL-BULL")
        self.assertIsNone(themes._overlaps(second, [first]))

    def test_a_shared_indicator_alone_is_not_a_duplicate(self):
        """95 instruments carry every macro debate; collisions are expected.

        The first version of this guard also called two cards duplicates when
        they named the same ticker, and rejected a carry-trade theme against a
        Fed-hawkishness theme — both reach for the dollar, about different things.
        """
        other = {"id": "FED-HAWKISH-TURN", "label": "美联储鹰派转向",
                 "terms": ["美联储鹰派转向", "联储转鹰"],
                 "price_indicator": self.code}
        card = themes.mint(self.con, dict(CLUSTER), self.as_of,
                           _Port(CARD_A % self.code), minted=[other])
        self.assertEqual(card["price_indicator"], self.code)

    def test_a_cluster_the_model_declines_is_a_finding_not_a_failure(self):
        with self.assertRaises(themes.MintSkipped):
            themes.mint(self.con, dict(CLUSTER), self.as_of,
                        _Port('{"skip": "「预览」是栏目名，不是宏观争论"}'))

    def test_naming_without_a_model_says_so(self):
        with self.assertRaises(themes.RegistrationError) as caught:
            themes.mint(self.con, dict(CLUSTER), self.as_of, None)
        self.assertIn("推理", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
