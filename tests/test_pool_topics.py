"""Jon 2026-09-06 第 5–8 条：候选池的数字层级、抽屉阅读空间、1.5 门槛、多主题。

四条反馈落到同一张表——候选池全表——上，所以放在一个文件里守着：

* 第 5 条：生成方法框的主读数是「不同标的数」，想法条数退到小字；两个数
  必须是两个数（同一个数印两遍就没有层级可言），并且不能写死。
* 第 6 条：抽屉要能放大：头上有展开键、左边缘有拖拽把手、有一档 `.drawer.wide`。
* 第 7 条：候选池底下那句「赔率低于 1.5 的想法不进入候选池」删掉；1.5 只属于
  赔率排序两条策略，面板上写的阈值要和 `select_omega.py` 注册的常量一致。
* 第 8 条：`_merge_pool` 不再把多主题压成一个——逐条提案全部保留，
  `weekly_block` 透传，`poolMatch` 按全部来源主题匹配。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, orchestrator as orc, review, schema  # noqa: E402
from ideagen.platform import Platform, Unavailable  # noqa: E402
from ideagen.platform.local import (FileCache, FileEventBus,  # noqa: E402
                                    LocalBlobStore, SqliteStateStore)

DASH = Path(__file__).resolve().parent.parent / "web" / "dash.html"
RUN_ID = "20260907T000000Z-pooltest"
AS_OF = "2026-09-09"


def _row(method, topic, inst, up, thesis=None, **extra):
    return {"id": f"{method}:{topic}:{inst}", "instrument_id": inst,
            "instrument_name": f"{inst} ETF", "topic_id": topic,
            "method": method, "thesis": thesis or f"{method} 在 {topic} 下看多 {inst}",
            "upside_pct": up, "downside_pct": -4.0,
            "p_up": .4, "p_base": .4, "p_down": .2, "horizon_days": 30,
            "citations": ["doc:1"], **extra}


#: GLD 那一行的形状：四个主题、四种方法、十四条提案（2026-09-02 期的真实分布）。
GLD_ROWS = [_row(m, t, "GLD", u) for (m, t, u) in (
    ("ai_native", "POLICY-PATH", 6.0), ("ai_native", "INFLATION", 7.0),
    ("ai_native", "TERM-PREMIUM", 8.0),
    ("carl_constraint", "POLICY-PATH", 5.0), ("carl_constraint", "INFLATION", 9.0),
    ("carl_constraint", "TERM-PREMIUM", 10.0), ("carl_constraint", "DOLLAR-FX", 6.5),
    ("chain", "POLICY-PATH", 7.5), ("chain", "INFLATION", 8.5),
    ("chain", "TERM-PREMIUM", 9.5), ("chain", "DOLLAR-FX", 5.5),
    ("gap", "POLICY-PATH", 6.2), ("gap", "TERM-PREMIUM", 7.2),
    ("gap", "DOLLAR-FX", 8.2))]


class MergePoolKeepsTopics(unittest.TestCase):

    def test_every_proposal_survives_the_merge(self):
        merged = orc._merge_pool(list(GLD_ROWS) + [_row("gap", "INFLATION", "TIP", 5.0)])
        gld = next(c for c in merged if c["instrument_id"] == "GLD")
        self.assertEqual(len(gld["proposals"]), 14,
                         "十四条提案合并后必须还是十四条——theses 按方法键存只剩四条")
        self.assertEqual(gld["topics"],
                         ["DOLLAR-FX", "INFLATION", "POLICY-PATH", "TERM-PREMIUM"])
        self.assertEqual(gld["topic_counts"],
                         {"POLICY-PATH": 4, "INFLATION": 3,
                          "TERM-PREMIUM": 4, "DOLLAR-FX": 3})
        self.assertEqual(sum(gld["topic_counts"].values()), gld["n_proposals"])
        # 每条提案带着它自己的主题、方法、论点、赔率与期限
        for x in gld["proposals"]:
            for k in ("topic_id", "method", "thesis", "upside_pct", "downside_pct",
                      "p_up", "p_base", "p_down", "horizon_days", "citations"):
                self.assertIn(k, x)
        self.assertEqual(
            {(x["method"], x["topic_id"]) for x in gld["proposals"]},
            {(r["method"], r["topic_id"]) for r in GLD_ROWS})

    def test_merged_odds_and_primary_topic_are_unchanged(self):
        """保留逐条提案不能改动合并读数：赔率仍取中位数，主主题仍按票数、平票按 ID。"""
        import statistics
        gld = next(c for c in orc._merge_pool(list(GLD_ROWS))
                   if c["instrument_id"] == "GLD")
        self.assertEqual(gld["upside_pct"],
                         round(statistics.median(r["upside_pct"] for r in GLD_ROWS), 4))
        # POLICY-PATH 与 TERM-PREMIUM 各 4 票，平票取字典序大的那个——和 09-02 截图一致
        self.assertEqual(gld["topic_id"], "TERM-PREMIUM")
        self.assertEqual(gld["proposed_by"], ["ai_native", "carl_constraint", "chain", "gap"])
        self.assertEqual(gld["n_methods"], 4)

    def test_payload_stays_json(self):
        c = orc._merge_pool(list(GLD_ROWS))[0]
        json.loads(json.dumps(orc._finite(c), ensure_ascii=False, allow_nan=False))


class _PoolRun:
    """一个只有候选池和生成器裁决的最小周跑。"""

    def setUp(self):
        review._PROPOSAL_INDEX.clear()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.p = Platform(
            name="test",
            blobs=LocalBlobStore(root / "blobs"),
            state=SqliteStateStore(root / "state.db"),
            inference=Unavailable("inference", "test node"),
            events=FileEventBus(root / "events.jsonl"),
            cache=FileCache(root / "cache"),
            secrets=Unavailable("secrets", "test node"),
        )
        schema.migrate(self.p.state)
        schema.upsert(self.p.state, "orch_runs", {
            "run_id": RUN_ID, "as_of": AS_OF, "kind": "weekly",
            "platform": "test", "started_at": "2026-09-09T00:00:00+00:00",
            "ended_at": "2026-09-09T00:00:09+00:00", "ok": 1, "error": None,
            "inputs_sha": None, "journal_uri": None, "calls": 3,
            "data_classification": "live"})
        self.con = db.init(":memory:")

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, candidates, rows):
        by_m: dict[str, list[str]] = {}
        for r in rows:
            by_m.setdefault(r["method"], []).append(r["id"])
        for m, ids in by_m.items():
            schema.upsert(self.p.state, "verdicts", {
                "run_id": RUN_ID, "as_of": AS_OF, "kind": "idea_generator",
                "strategy": m, "version": "1.0", "role": "primary",
                "inputs_sha": None, "chosen": json.dumps(ids), "scores": "{}",
                "rejected": "{}", "meta": json.dumps({"per_topic": {}}),
                "calls": 1})
        for c in candidates:
            schema.upsert(self.p.state, "candidates", {
                "run_id": RUN_ID, "as_of": AS_OF, "candidate_id": c["id"],
                "instrument_id": c["instrument_id"], "topic_id": c["topic_id"],
                "method": c["method"], "upside_pct": c["upside_pct"],
                "downside_pct": c["downside_pct"], "p_up": c["p_up"],
                "p_base": c["p_base"], "p_down": c["p_down"],
                "payload": json.dumps(orc._finite(c), ensure_ascii=False)})


class WeeklyBlockPassesProposalsThrough(_PoolRun, unittest.TestCase):

    def test_new_payloads_reach_the_panel_whole(self):
        rows = list(GLD_ROWS) + [_row("gap", "INFLATION", "TIP", 5.0)]
        self._store(orc._merge_pool(rows), rows)
        blk = review.weekly_block(self.p, self.con, AS_OF)
        cands = blk["pool"]["candidates"]
        gld = next(c for c in cands if c["instrument_id"] == "GLD")
        self.assertEqual(sorted(gld["topics"]),
                         ["DOLLAR-FX", "INFLATION", "POLICY-PATH", "TERM-PREMIUM"])
        self.assertEqual(len(gld["proposals"]), 14)
        self.assertFalse(gld["proposals_partial"])
        self.assertEqual(gld["topic_counts"]["POLICY-PATH"], 4)
        self.assertTrue(all(x["thesis"] for x in gld["proposals"]),
                        "透传不能把论点剔掉")
        self.assertEqual(gld["n_methods"], 4)

    def test_old_payloads_get_method_x_topic_back_from_verdict_ids(self):
        """09-07 之前合并的期次没有 proposals，但生成器裁决里的 id 是
        `方法:主题:标的`——主题×方法能还原，论点与赔率不能，于是标成 partial，
        数字留空而不是拿中位数填十四遍。"""
        rows = list(GLD_ROWS)
        merged = orc._merge_pool(rows)
        for c in merged:
            c.pop("proposals"); c.pop("topic_counts")
        self._store(merged, rows)
        gld = review.weekly_block(self.p, self.con, AS_OF)["pool"]["candidates"][0]
        self.assertTrue(gld["proposals_partial"])
        self.assertEqual(len(gld["proposals"]), 14)
        self.assertEqual(gld["topic_counts"],
                         {"POLICY-PATH": 4, "INFLATION": 3,
                          "TERM-PREMIUM": 4, "DOLLAR-FX": 3})
        self.assertTrue(all(x["thesis"] is None and x["upside_pct"] is None
                            for x in gld["proposals"]))

    def test_licensed_runs_keep_the_structure_but_not_the_words(self):
        rows = list(GLD_ROWS)
        self._store(orc._merge_pool(rows), rows)
        self.p.state.q("UPDATE orch_runs SET data_classification=? WHERE run_id=?",
                       ("licensed-private-corpus", RUN_ID))
        gld = review.weekly_block(self.p, self.con, AS_OF)["pool"]["candidates"][0]
        self.assertEqual(len(gld["proposals"]), 14)
        self.assertTrue(all(x["thesis"] is None and "id" not in x
                            for x in gld["proposals"]))
        self.assertEqual(sorted(gld["topics"]),
                         ["DOLLAR-FX", "INFLATION", "POLICY-PATH", "TERM-PREMIUM"])


class RealPeriodNumbersAreTwoNumbers(unittest.TestCase):
    """在真实库上：每种方法「不同标的数」和「想法条数」必须是两个不同的数。

    没有真实库（CI、干净 checkout）就跳过；有库但没有 2026-09-02 期也跳过。
    """

    def test_distinct_instruments_differ_from_idea_counts(self):
        from ideagen import platform as plat
        try:
            con = db.init()
            p = plat.load()
            blk = review.weekly_block(p, con, "2026-09-02")
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"这棵树里读不到状态库：{exc}")
        if not blk or not (blk.get("pool") or {}).get("candidates"):
            self.skipTest("库里没有 2026-09-02 期的候选池")
        cands = blk["pool"]["candidates"]
        for g in blk["generators"]:
            k = sum(1 for c in cands if g["method"] in (c.get("proposed_by") or []))
            self.assertLess(k, g["n"],
                            f"{g['method']}：去重标的 {k} 应少于想法 {g['n']}")
            self.assertGreater(k, 0)
        self.assertLess(blk["pool"]["n"], sum(g["n"] for g in blk["generators"]))
        for c in cands:
            for k in ("topics", "topic_counts", "proposals", "proposed_by",
                      "n_proposals", "proposals_partial"):
                self.assertIn(k, c, f"面板要读的字段 {k} 没透传")
        gld = next((c for c in cands if c["instrument_id"].endswith("GLD")), None)
        if gld:
            self.assertEqual(len(gld["topics"]), 4)
            self.assertEqual(len(gld["proposals"]), gld["n_proposals"])


# ── 面板结构 ────────────────────────────────────────────────────────────
def _js(src: str) -> str:
    return re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)[-1]


def _strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def _fn(js: str, name: str) -> str:
    m = re.search(r"function " + re.escape(name) + r"\((.*?)\n\}", js, re.S)
    assert m, f"找不到函数 {name}"
    return m.group(0)


class DashPoolContracts(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.src = DASH.read_text(encoding="utf-8")
        self.js = _js(self.src)

    # 第 7 条
    def test_the_pool_no_longer_claims_a_universal_odds_floor(self):
        self.assertNotIn("赔率低于 1.5 的想法不进入候选池", _strip_comments(self.js))
        self.assertNotIn("准入下限 1.5；低于门槛不建仓", self.js)

    def test_omega_thresholds_on_the_panel_match_the_strategy_module(self):
        from ideagen import strategies  # noqa: F401  触发注册
        from ideagen import strategy as strat
        from ideagen.strategies import select_omega as om
        loose = strat.spec("idea_selector", "omega_loose")["params"]
        strict = strat.spec("idea_selector", "omega_strict")["params"]
        meta = re.search(r"var SEL_META=\[(.*?)\n\];", self.js, re.S).group(1)
        lo = re.search(r"\['omega_loose',(.*?)\],", meta, re.S).group(1)
        st = re.search(r"\['omega_strict',(.*?)\],", meta, re.S).group(1)
        floor = f"{loose['floor']:g}"
        self.assertIn(floor, lo, "宽松版文字里的下限和注册参数不一致")
        self.assertIn(floor, st, "严格版文字里的下限和注册参数不一致")
        self.assertIn(f"{loose['n_min']}～{loose['n_max']} 条", lo)
        self.assertIn(f"{strict['n_min']}～{strict['n_max']} 条", st)
        self.assertIn(f"{om.DEFAULT_HURDLE_M * 100:.2f}%", lo, "现金门槛要写成月 0.28% 那样的数")
        src = Path(om.__file__).read_text(encoding="utf-8")
        cut = re.search(r"len\(ranked\) \* (0\.\d+)", src).group(1)
        self.assertIn(f"前 {int(float(cut) * 100)}%", st)
        self.assertIn("中位数", lo, "宽松版的准入线是中位数与下限的较高者，要写出来")
        self.assertIn("候选池本身没有统一的赔率门槛", lo)

    # 第 5 条
    def test_no_period_numbers_are_hard_coded_in_panel_strings(self):
        js = _strip_comments(self.js)
        bad = []
        for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'", js):
            lit = m.group(1)
            if re.search(r"(?<![\d.])(54|73|52|100)\s*(只|条)", lit):
                bad.append(lit[:80])
        self.assertFalse(bad, f"这些数是 2026-09-02 那一期的，得从数据算：{bad}")

    def test_generator_boxes_lead_with_distinct_instruments(self):
        svg = _fn(self.js, "pipeCanvasSVG")
        self.assertIn("poolCountBy('method',g.method)", svg)
        self.assertIn("只标的", svg)
        self.assertIn("条想法合并", svg)
        pool = _fn(self.js, "stagePoolBody")
        self.assertIn("gm-card", pool)
        self.assertIn("只候选标的", pool)
        self.assertIn("不能相加", pool)
        gen = _fn(self.js, "stageGeneratorsBody")
        self.assertIn("只候选标的", gen)

    # 第 6 条
    def test_drawer_can_be_widened(self):
        rd = _fn(self.js, "renderDrawers")
        self.assertIn("drawer-wide-btn", rd)
        self.assertIn("toggleDrawerWide()", rd)
        self.assertIn("drawer-grip", rd)
        self.assertIn("drawerGripDown(event)", rd)
        for fn in ("toggleDrawerWide", "drawerGripDown", "drawerWidthCSS", "drawerIsWide"):
            _fn(self.js, fn)
        self.assertRegex(self.src, r"\.drawer\.wide\b")
        self.assertRegex(self.src, r"\.drawer\.wide #candTable td\.primary\{[^}]*sticky")
        self.assertRegex(self.src, r"\.drawer\.wide \.cand-name\{[^}]*white-space:normal")
        self.assertIn("localStorage.setItem(DRW_KEY", self.js)

    # 第 8 条
    def test_pool_match_reads_every_source_topic(self):
        body = _fn(self.js, "poolMatchCond")
        topic = re.search(r"if\(t==='topic'\)return ([^;]+);", body).group(1)
        self.assertIn("candTopics(c)", topic)
        self.assertNotEqual(topic.strip(), "c.topic_id===v")
        mt = body[body.index("if(t==='mt')"):]
        self.assertIn("candProps(c)", mt)
        self.assertIn("candTopics(c)", mt)
        ct = _fn(self.js, "candTopics")
        self.assertIn("c.topics", ct)
        pm = _fn(self.js, "poolMatch")
        self.assertIn("poolFilterTopics()", pm)

    def test_the_table_has_one_filter_entry_and_shows_every_topic(self):
        pool = _fn(self.js, "stagePoolBody")
        self.assertIn("poolTopicBar(cands)", pool)
        self.assertNotIn("filter-bar", pool.replace("poolTopicBar", ""),
                         "旧的那条筛选栏该并进 poolTopicBar，不要两条")
        bar = _fn(self.js, "poolTopicBar")
        self.assertIn("只标的", bar)
        self.assertIn("条想法", bar)
        self.assertIn("togglePoolTopic", bar)
        self.assertIn("clearPoolFilter()", bar)
        row = _fn(self.js, "renderCandBody")
        self.assertIn("candTopicTags(c)", row)
        self.assertIn("candDetailHTML(c)", row)
        det = _fn(self.js, "candDetailHTML")
        self.assertIn("proposals_partial", det)
        for fn in ("filterPool", "filterPoolMT", "filterPoolConv", "togglePoolTopic"):
            self.assertIn("poolFilter=", _fn(self.js, fn),
                          f"{fn} 必须写同一个 poolFilter，不能另起一套状态")

    def test_position_and_bubble_show_all_topics(self):
        pos = _fn(self.js, "posDrawerBody")
        self.assertIn("candTopics(cand)", pos)
        pool = _fn(self.js, "stagePoolBody")
        self.assertIn("candTopics(c).map(topicName)", pool)

    def test_script_parses(self):
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("没有 node，跳过语法检查")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(self.js)
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(r.returncode, 0, r.stderr[:2000])


if __name__ == "__main__":
    unittest.main()
