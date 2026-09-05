"""主题抽屉的「这是什么」：注册表原文必须真的送到面板，而且两端对得上。

这一格存在的理由：在它之前，面板拿到的是主题的**分数**，从来没有主题**本身**。
`themes/registry.jsonl` 和 `lexicon.SEED_THEMES` 里一直写着它在问哪一问、
答「能」是哪个方向、用哪条价格序列读它、由哪些词构成、哪天注册的、凭什么注册——
一个字段都没有出现在页面上，于是「央行政策路径与流动性」在屏幕上只是一个标签
加四个读数。

这个文件盯三件本仓反复踩过的事：

1. **接线接通了不等于契约对得上。**（主题自动注册那次就是这么绿着失败的：
   两端各有测试，中间那道字段契约没人测。）所以这里同时读后端产出的键和
   `web/dash.html` 里实际读的键，对不上就红。
2. **写好了没接上。** `themeIdentity()` 定义在，不代表有人调用它。查调用点。
3. **失败要有名字。** 取不到主题原文时必须写 `themes_error`，不能留一个空字典——
   空字典和「这个主题没有词表」在页面上长得一模一样。
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, lexicon, review
from ideagen import platform as plat

DASH = Path(__file__).resolve().parent.parent / "web" / "dash.html"

#: 后端在 `weekly["themes"][tid]` 里承诺的字段。前端 `themeIdentity()` 读的
#: 就是这几个，任何一边改名而另一边不改，这张表让它红。
CONTRACT = ("label", "key_question", "direction", "indicator", "related",
            "exposures", "terms", "require", "origin", "registered_d",
            "provenance", "arc")


class TestThemeIdentityPayload(unittest.TestCase):
    weekly: dict = {}

    @classmethod
    def setUpClass(cls):
        try:
            cls.weekly = review.weekly_block(plat.load(), db.init()) or {}
        except Exception as exc:  # noqa: BLE001 — 没有库的树上无从检查
            raise unittest.SkipTest(f"no readable store here: {exc}") from exc
        if not cls.weekly.get("topics"):
            raise unittest.SkipTest("no scored topics in this database")

    def test_every_scored_theme_carries_its_own_registry_text(self):
        themes = self.weekly.get("themes")
        self.assertIsNotNone(
            themes, f"themes 缺席而没有报错原因：{self.weekly.get('themes_error')!r}")
        scored = {tid for tv in self.weekly["topics"]
                  for tid in (tv.get("scores") or {})}
        known = {t.id for t in lexicon.all_themes(self.weekly["as_of"])}
        for tid in sorted(scored & known):
            with self.subTest(topic=tid):
                row = themes.get(tid)
                self.assertIsNotNone(row, f"{tid} 打了分却没有注册原文")
                for field in CONTRACT:
                    self.assertIn(field, row)
                self.assertTrue(row["key_question"], f"{tid} 没有它要问的那一问")
                self.assertTrue(row["terms"], f"{tid} 没有词表")

    def test_a_period_before_registration_says_so_instead_of_going_blank(self):
        """留空会被读成「那期它冷了」，而事实是那期它还不存在。"""
        themes = self.weekly.get("themes") or {}
        by_id = {t.id: t for t in lexicon.all_themes(self.weekly["as_of"])}
        checked = 0
        for tid, row in themes.items():
            th = by_id.get(tid)
            if th is None:
                continue
            for point in row.get("arc") or []:
                if point["as_of"] < th.registered_d:
                    self.assertEqual(point["state"], "pre-registration",
                                     f"{tid} 在 {point['as_of']} 早于注册日，"
                                     "却没有标成注册前")
                    self.assertNotIn("score", point,
                                     f"{tid} 在注册日之前拿到了分数——回填了历史")
                    checked += 1
        if not checked:
            self.skipTest("这份库里没有注册日之后才出现的主题")

    def test_the_arc_only_repeats_scores_the_run_already_sealed(self):
        """轨迹不是重算的：本期那一点必须等于本期打分表里的那一行。"""
        themes = self.weekly.get("themes") or {}
        hgep = next((tv for tv in self.weekly["topics"]
                     if tv.get("scorer") == "hgep"), None)
        if hgep is None:
            self.skipTest("这一期没有 hgep 打分")
        for tid, row in themes.items():
            sealed = (hgep.get("scores") or {}).get(tid)
            here = next((p for p in row.get("arc") or []
                         if p["as_of"] == self.weekly["as_of"]
                         and p.get("state") == "scored"), None)
            if not sealed or not here:
                continue
            with self.subTest(topic=tid):
                for key in ("H", "G", "E", "P", "score", "n_evidence"):
                    self.assertEqual(here.get(key), sealed.get(key), key)
                self.assertEqual(here["chosen"], tid in (hgep.get("chosen") or []))


class TestThemeIdentityIsWiredIntoThePage(unittest.TestCase):
    """定义在 ≠ 有人调用。本仓有过一整套路由写好了、零调用点的先例。"""

    @classmethod
    def setUpClass(cls):
        cls.src = DASH.read_text(encoding="utf-8")
        if "function themeIdentity(" not in cls.src:
            raise unittest.SkipTest("themeIdentity 尚未在这棵树上")

    def test_it_has_a_call_site(self):
        calls = len(re.findall(r"themeIdentity\(", self.src)) - 1
        self.assertGreater(calls, 0, "themeIdentity 定义了但没人调用")

    def test_the_page_reads_the_keys_the_server_writes(self):
        """前端读的字段名必须都在后端那张契约表里。"""
        body = self.src[self.src.index("function themeIdentity("):]
        body = body[:body.index("\n}\n")]
        used = set(re.findall(r"\bt\.([a-z_]+)", body))
        unknown = used - set(CONTRACT)
        self.assertEqual(unknown, set(),
                         f"页面读了后端没送的字段：{sorted(unknown)}")

    def test_the_drawer_subtitle_counts_this_group_too(self):
        """副标题在描述这个抽屉自己的结构，多一格就要跟着说。"""
        line = next((ln for ln in self.src.splitlines()
                     if "证据研报 · 下游产出" in ln), "")
        self.assertIn("这是什么", line,
                      "抽屉多了「这是什么」一组，副标题还在念旧的三段")


if __name__ == "__main__":
    unittest.main()
