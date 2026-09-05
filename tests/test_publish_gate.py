"""The publish gate, which had no test at all.

`scripts/check_publish_safety.py` decides whether a payload reaches a public,
indexable URL. It was reachable only from two shell scripts and nothing checked
that it still refused anything — a gate whose failure mode is silence, with no
one listening for it.

Written after finding one such silence. The gate refused an operator's sentence
sitting in a `meta` field when the baked payload parsed, and published the same
sentence when the JSON was one byte short: `_payload` returned None for "not a
baked page" and for "a baked page I could not read", and the caller treated both
as nothing to scan. Its docstring said the text scan covered the gap. It does
not — `scan_payload` is the only check for prose in bookkeeping containers and
has no text-level equivalent — so the verdict turned on whether the gate could
read the thing it was judging, and it said nothing about which had happened.

So these hold both halves: that each rule still fires on the content it names,
and that the gate refuses rather than passes whenever it could not look. Every
test below was confirmed to go red when the rule it covers is removed; the ones
that were not are not here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_publish_safety.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_gate", GATE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _page(payload: object) -> str:
    return ("<html><script>window.__STATIC__="
            + json.dumps(payload, ensure_ascii=False) + ";</script></html>")


def _run(text: str, suffix: str = ".html") -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"page{suffix}"
        p.write_text(text, encoding="utf-8")
        return subprocess.run([sys.executable, str(GATE), str(p)],
                              capture_output=True, text=True)


#: An operator's own sentence: ours, never chosen for a public URL.
PROSE = "这是运行者当时敲进去的一整句话，本不该出现在公开页面上，属于记账字段里的自由文本。"


class NotLookingIsNotAPass(unittest.TestCase):
    """The failure this file was written for."""

    def test_a_payload_that_will_not_parse_is_refused(self):
        good = _page({"ideas": [{"id": "x", "meta": {"note": PROSE}}]})
        broken = good.replace(";</script>", "</script>")   # marker, no terminator
        self.assertEqual(_run(good).returncode, 1, "parses: refused")
        self.assertEqual(_run(broken).returncode, 1,
                         "same content, unreadable payload — refusing to read "
                         "it is not the same as clearing it")

    def test_the_refusal_says_it_could_not_look(self):
        broken = _page({"a": 1}).replace(";</script>", "</script>")
        err = _run(broken).stderr
        self.assertIn("无法解析", err)
        self.assertIn("未能执行", err)

    def test_a_file_with_no_payload_at_all_is_not_treated_as_unreadable(self):
        # report.json and any plain page carry no marker. Refusing those would
        # make the gate refuse every publish, which is a different kind of
        # broken and the reason the three states are distinguished.
        m = _mod()
        self.assertEqual(m._payload('{"just": "json"}')[1], "absent")
        self.assertEqual(m._payload("<html>nothing</html>")[1], "absent")
        self.assertEqual(_run('{"just":"json"}', ".json").returncode, 0)

    def test_no_file_to_check_is_refused(self):
        r = subprocess.run([sys.executable, str(GATE),
                            str(ROOT / "no" / "such" / "file.html")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("没有找到任何待发布文件", r.stderr)


class EachRuleStillFires(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_partner_product_codes(self):
        self.assertTrue(self.m.scan("持有 L02907 与 L03028", "t"))
        self.assertFalse(self.m.scan("持有 L2907", "t"), "not the L0#### form")

    def test_partner_institution_name(self):
        for name in ("Nexus Wealth", "Nexus Capital", "Nexus 资本", "Nexus财富"):
            with self.subTest(name=name):
                self.assertTrue(self.m.scan(f"来自 {name} 的货架", "t"))

    def test_brand_is_incidental_below_the_limit_and_pervasive_above(self):
        limit = self.m.BRAND_LIMIT
        self.assertFalse(self.m.scan("Olive " * limit, "t"),
                         "a fund's own name in a holding row must not block")
        self.assertTrue(self.m.scan("Olive " * (limit + 1), "t"))

    def test_the_thresholds_are_pinned_at_the_values_that_were_decided(self):
        """Both the comparison and the number, because either alone has a hole.

        The test above derives its inputs from `BRAND_LIMIT`, so it holds the
        comparison and nothing else: raising the constant to 500 raises the
        test's own threshold with it and every assertion stays green while the
        gate stops refusing anything short of a flood. That is the mirror of
        the sentinel bug found the same night — there a constant was asserted
        and the behaviour was not; here the behaviour is asserted relative to
        the constant and the constant is not.

        Pinned so a change is a decision someone makes on purpose. If a limit
        should move, move it here too and say why in the commit; what must not
        happen is it moving with nothing going red.
        """
        self.assertEqual(self.m.BRAND_LIMIT, 20)
        self.assertEqual(self.m.PROSE_MIN_CHARS, 40)
        self.assertEqual(self.m.BOOKKEEPING_KEYS,
                         ("meta", "params", "args", "kwargs", "options"))
        self.assertEqual(self.m.BODY_FIELDS,
                         ("body", "full_text", "raw_text", "正文"))
        self.assertEqual(len(self.m.PARTNER_PATTERNS), 2)

    def test_a_long_body_field_is_subscription_text(self):
        long = "研究正文" * 120
        self.assertTrue(self.m.scan(json.dumps({"body": long},
                                               ensure_ascii=False), "t"))
        self.assertFalse(self.m.scan(json.dumps({"body": "短摘要"},
                                                ensure_ascii=False), "t"),
                         "the key alone is not the problem, the content is")

    def test_prose_in_a_bookkeeping_container(self):
        for key in self.m.BOOKKEEPING_KEYS:
            with self.subTest(key=key):
                self.assertTrue(
                    self.m.scan_payload({"x": {key: {"note": PROSE}}}, "t"))

    def test_prose_in_a_first_class_field_is_published_on_purpose(self):
        # The thesis is the product. Flagging it would make the gate refuse
        # every real payload, and a gate that always refuses gets bypassed.
        self.assertFalse(self.m.scan_payload({"idea": {"thesis": PROSE}}, "t"))

    def test_a_label_or_enum_in_a_meta_field_is_not_prose(self):
        self.assertFalse(self.m.scan_payload(
            {"meta": {"strategy": "hgep", "verdict": "not_ruled_out"}}, "t"))

    def test_nesting_below_a_bookkeeping_key_stays_inside(self):
        # `inside` is sticky: prose two levels under `meta` is still in meta.
        self.assertTrue(self.m.scan_payload(
            {"meta": {"a": {"b": [{"note": PROSE}]}}}, "t"))

    def test_the_reported_path_locates_the_string(self):
        hits = self.m.scan_payload({"ideas": [{"meta": {"note": PROSE}}]}, "t")
        self.assertEqual(len(hits), 1)
        self.assertIn("ideas[0].meta.note", hits[0],
                      "a refusal nobody can act on is a refusal nobody acts on")


class TheGateIsNotVacuous(unittest.TestCase):
    """A clean payload must pass, or the tests above prove nothing."""

    def test_a_payload_with_nothing_to_hide_publishes(self):
        clean = _page({"ideas": [{"id": "x", "thesis": PROSE,
                                  "meta": {"n": 3, "as_of": "2026-09-05"}}]})
        r = _run(clean)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("检查通过", r.stdout)


if __name__ == "__main__":
    unittest.main()
