"""主题谱系（`ideagen/theme_lineage.py`）与按家族计数的复现打折。

盯住：
1. 相似度零件：二元组 Dice、问句去套话、词表互含（单字不参与包含）。
2. 换名的一对（标签近、词表互含、研报几乎全重叠、从未同时强）→ 自动归并；
   同样像、但同一天都是强主题的一对 → 只列疑似，反证写出来；研报太少的一对
   不因为「2 篇全重叠」被推上去；窄主题落在宽主题里 → 包含关系，不算疑似。
3. apply 追加写、不重复写，方向是新的并入老的。
4. `scoring.recurrence` 按家族数；谱系登记日之前的打分日看不到这条谱系。

注册表、谱系文件全指向临时目录，仓库里的 themes/ 一个字节都不动。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config, db, lexicon, scoring, theme_lineage as tl  # noqa: E402

REG = [
    {"id": "DUP-OLD", "label": "低空经济与无人机物流", "registered_d": "2026-06-01",
     "key_question": "未来1–6个月，低空经济订单能否兑现为无人机物流收入",
     "terms": ["低空经济", "无人机物流", "eVTOL"], "price_indicator": "US.ZZA"},
    {"id": "DUP-NEW", "label": "低空经济与无人机配送", "registered_d": "2026-07-01",
     "key_question": "未来1–6个月，低空经济订单能否兑现为无人机配送收入",
     "terms": ["低空经济产业", "无人机物流", "eVTOL"], "price_indicator": "US.ZZA"},
    {"id": "CO-A", "label": "固态电池量产节奏", "registered_d": "2026-06-01",
     "key_question": "未来1–6个月，固态电池量产能否提速",
     "terms": ["固态电池", "硫化物电解质"], "price_indicator": "US.ZZB"},
    {"id": "CO-B", "label": "固态电池量产进度", "registered_d": "2026-06-01",
     "key_question": "未来1–6个月，固态电池量产能否加速",
     "terms": ["固态电池", "硫化物电解质"], "price_indicator": "US.ZZB"},
    {"id": "TINY-A", "label": "月球采矿", "registered_d": "2026-06-01",
     "key_question": "未来1–6个月，月球采矿能否立项", "terms": ["月球采矿"],
     "price_indicator": "US.ZZC"},
    {"id": "TINY-B", "label": "小行星采矿", "registered_d": "2026-06-01",
     "key_question": "未来1–6个月，小行星采矿能否立项", "terms": ["月球采矿", "小行星"],
     "price_indicator": "US.ZZC"},
]


class _Iso(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self._saved = (lexicon.REGISTRY_PATH, lexicon.ALIASES_PATH, tl.LINEAGE_PATH)
        (root / "registry.jsonl").write_text(
            "\n".join(json.dumps({**r, "origin": "discovered"}, ensure_ascii=False)
                      for r in REG) + "\n", encoding="utf-8")
        lexicon.REGISTRY_PATH = root / "registry.jsonl"
        lexicon.ALIASES_PATH = root / "aliases.jsonl"
        tl.LINEAGE_PATH = root / "lineage.jsonl"
        self._dur = mock.patch.object(lexicon, "durable_themes_dir", lambda: None)
        self._dur.start()
        lexicon.reload_registry()
        tl._CACHE.clear()
        self.con = db.init(":memory:")
        n = 0
        for i in range(30):        # 两个低空主题几乎全重叠
            n += 1
            self._doc(n, f"低空经济产业 无人机物流 第{i}篇", "2026-07-%02d" % (1 + i % 28))
        for i in range(30):        # 两个电池主题全重叠
            n += 1
            self._doc(n, f"固态电池 硫化物电解质 第{i}篇", "2026-07-%02d" % (1 + i % 28))
        for i in range(2):         # 两个采矿主题：只有 2 篇
            n += 1
            self._doc(n, f"月球采矿 小行星 第{i}篇", "2026-07-02")
        # 反证：电池两主题同一天都是强主题
        for tid in ("CO-A", "CO-B"):
            db.upsert(self.con, "themes", {"as_of": "2026-07-10", "theme_id": tid, "label": tid,
                                           "tis": 80.0, "tier": "core"}, ["as_of", "theme_id"])

    def _doc(self, n, title, d):
        self.con.execute("INSERT INTO documents(doc_id,line,tier,title,published_at,published_d,"
                         "ingested_at,content_hash) VALUES (?,?,?,?,?,?,?,?)",
                         (f"d{n}", "feed", 2, title, d + "T09:00:00+08:00", d,
                          "2026-07-30T00:00:00", f"h{n}"))

    def tearDown(self):
        self._dur.stop()
        lexicon.REGISTRY_PATH, lexicon.ALIASES_PATH, tl.LINEAGE_PATH = self._saved
        lexicon.reload_registry()
        tl._CACHE.clear()
        self.con.close()
        self.td.cleanup()

    def pair(self, res, a, b):
        return next(p for p in res["pairs"] if {p["a"], p["b"]} == {a, b})


class Parts(unittest.TestCase):
    def test_dice_and_boilerplate(self):
        self.assertEqual(tl.dice("abc", "abc"), 1.0)
        self.assertEqual(tl.dice("ab", "cd"), 0.0)
        self.assertNotIn("未来", tl.kq_norm("未来1–6个月，铜价能否上涨"))

    def test_term_containment_but_not_for_single_chars(self):
        self.assertGreater(tl.term_sim(("存储",), ("存储芯片",)), 0.9)
        self.assertEqual(tl.term_sim(("云",), ("云计算",)), 0.0)


class Scan(_Iso):
    def test_renamed_pair_is_auto_and_co_strong_pair_is_only_suspect(self):
        res = tl.scan(self.con, runs=[])
        dup = self.pair(res, "DUP-OLD", "DUP-NEW")
        self.assertEqual(dup["decision"], "auto", dup["why"])
        self.assertEqual(dup["counter"], [])
        co = self.pair(res, "CO-A", "CO-B")
        self.assertGreaterEqual(co["score"], config.LINEAGE_AUTO_SCORE)
        self.assertEqual(co["decision"], "suspect")
        self.assertTrue(any("同一天都是强主题" in c for c in co["counter"]))
        tiny = self.pair(res, "TINY-A", "TINY-B")
        self.assertEqual(tiny["ev_overlap"], 0.0)          # 2 篇的重叠不算证据
        self.assertNotEqual(tiny["decision"], "auto")

    def test_apply_appends_once_newer_into_older_and_recurrence_follows(self):
        res = tl.scan(self.con, runs=[])
        wrote = tl.apply(self.con, res, as_of=date(2026, 8, 1))
        self.assertEqual([(w["theme_id"], w["family"]) for w in wrote], [("DUP-NEW", "DUP-OLD")])
        self.assertEqual(tl.family_ids("DUP-NEW"), {"DUP-NEW", "DUP-OLD"})
        # 再跑一遍：已登记，不重复写
        res2 = tl.scan(self.con, runs=[])
        self.assertEqual(tl.apply(self.con, res2, as_of=date(2026, 8, 2)), [])
        lines = [x for x in tl.LINEAGE_PATH.read_text("utf-8").splitlines()
                 if x.strip() and not x.startswith("#")]
        self.assertEqual(len(lines), 1)

        # 老名字连续三周是强主题；新名字第一次出现在 08-19
        for d in ("2026-08-05", "2026-08-12"):
            db.upsert(self.con, "themes", {"as_of": d, "theme_id": "DUP-OLD", "label": "x",
                                           "tis": 80.0, "tier": "core"}, ["as_of", "theme_id"])
        r = scoring.recurrence(self.con, "DUP-NEW", date(2026, 8, 19))
        self.assertEqual(r["consec"], 2, "同一家族的历史要算进复现")
        # 谱系登记于 08-01；在它之前的打分日看不到这条谱系
        self.assertEqual(tl.family_ids("DUP-NEW", "2026-07-31"), {"DUP-NEW"})

    def test_dry_run_writes_nothing(self):
        res = tl.scan(self.con, runs=[])
        out = tl.apply(self.con, res, as_of=date(2026, 8, 1), dry_run=True)
        self.assertEqual(len(out), 1)
        self.assertFalse(tl.LINEAGE_PATH.exists())

    def test_bad_lines_raise_instead_of_vanishing(self):
        tl.LINEAGE_PATH.write_text(json.dumps({"as_of": "2026-08-01", "theme_id": "NOPE",
                                               "family": "DUP-OLD", "rationale": "x"}) + "\n",
                                   encoding="utf-8")
        tl._CACHE.clear()
        with self.assertRaises(ValueError):
            tl.load_lineage()


class NestedIsNotDuplicate(unittest.TestCase):
    def test_small_inside_big_is_nested(self):
        # 直接喂 mentions：窄主题 30 篇全在宽主题 300 篇里
        con = db.init(":memory:")
        big = {f"d{i}": "2026-07-01" for i in range(300)}
        small = {f"d{i}": "2026-07-01" for i in range(30)}
        ids = [t.id for t in lexicon.SEED_THEMES[:2]]
        mentions = {ids[0]: {"full": big}, ids[1]: {"full": small}}
        res = tl.scan(con, mentions=mentions, runs=[])
        p = next(x for x in res["pairs"] if {x["a"], x["b"]} == set(ids))
        self.assertEqual(p["ev_overlap"], 1.0)
        self.assertLess(p["ev_jaccard"], config.LINEAGE_NESTED_JACCARD)
        self.assertIn(p["decision"], ("nested", "distinct"))
        self.assertNotEqual(p["decision"], "auto")


if __name__ == "__main__":
    unittest.main()
