"""业绩页（Jon 2026-09-06 第 9、10 条）的结构闸门。

前端按 /api/perf 的 JSON 契约开发，后端另一条分支按同一份契约产出。这里钉住三件事：

* 契约键名——两份夹具（tests/fixtures/perf_*_sample.json）必须带齐契约里的每一个键，
  键名与后端测试用的是同一份清单。夹具少一个键，或页面读了契约之外的键，联调时
  才会发现，那时两边都已经写完了。
* 页面结构——VIEWS 里有「业绩」且七段齐全；模式切换三个选项、实盘读 perf_index；
  历史回测明细不再只取前四行；表头列数 == 行里的格子数（写法同 test_dash_layout）。
* 语法——把 <script> 抽出来过一遍 `node --check`。没有 node 就跳过，不装假绿。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "web" / "dash.html"
FIXTURES = ROOT / "tests" / "fixtures"

# ── 契约（与后端测试共用同一份键名；改这里要两边一起改） ─────────────────
PERF_TOP_KEYS = {
    "mode", "label", "subset", "methodology", "source_id", "window", "capital",
    "generated_at", "strategies", "benchmarks", "weekly_pnl", "summary",
    "attribution", "research", "records", "disclosures",
}
STRATEGY_KEYS = {"key", "name", "role", "available", "status", "reason",
                 "first_d", "last_d", "n_periods", "curve"}
CURVE_POINT_KEYS = {"d", "v"}
BENCHMARK_KEYS = {"spy", "buy_all"}
WEEKLY_KEYS = {"weeks", "by_strategy", "note"}
WEEK_KEYS = {"week", "start", "end"}
WEEKLY_CELL_KEYS = {"week", "equity_start", "equity_end", "pnl_amt", "pnl_pct",
                    "realized", "unrealized_chg", "cash_income", "fees", "flows",
                    "reconciled", "residual"}
SUMMARY_KEYS = {"as_of", "cash_share_basis", "rows", "roster_diff"}
SUMMARY_ROW_KEYS = {"key", "name", "role", "status", "reason", "cum_ret_pct",
                    "excess_spy_pp", "excess_buy_all_pp", "max_dd_pct",
                    "cash_share_end_pct", "cash_share_avg_pct", "n_periods", "n_days"}
ROSTER_DIFF_KEYS = {"other_mode", "added", "absent"}
ATTRIBUTION_KEYS = {"rule", "by_topic", "by_instrument", "by_method"}
ATTR_ROW_KEYS = {"key", "name", "pnl_full_credit", "pnl_split_equal", "n_positions"}
ATTR_INSTRUMENT_EXTRA = {"topics", "methods"}
RESEARCH_KEYS = {"sample_note", "n_periods", "n_days", "stats"}
RECORDS_KEYS = {"failed_runs", "gaps", "backfill_periods", "stuck_batches", "affected_ranges"}
MODES = {"paper", "backtest"}
SUBSETS = {"live", "backfill", "all"}
METHODOLOGIES = {"paper-rules", "formal-paper-rules", "stock-picking-study-30d"}
STATUSES = {"ok", "缺数据", "未运行", "失败"}


def _src() -> str:
    return DASH.read_text(encoding="utf-8")


def _script(src: str) -> str:
    return re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)[-1]


def _fn(js: str, name: str) -> str:
    """顶层 `function name(` 起，到下一个顶层 function 止。"""
    m = re.search(r"\nfunction " + re.escape(name) + r"\s*\(", js)
    assert m, f"找不到函数 {name}"
    rest = js[m.start() + 1:]
    nxt = re.search(r"\nfunction [A-Za-z_$][\w$]*\s*\(", rest)
    return rest[: nxt.start()] if nxt else rest


class PerfFixturesFollowTheContract(unittest.TestCase):
    """两份夹具都得带齐契约里的每个键；多出来的顶层键也拦——那是页面读不到的东西。"""

    def _load(self, name: str) -> dict:
        p = FIXTURES / name
        self.assertTrue(p.exists(), f"夹具不存在：{p}")
        return json.loads(p.read_text(encoding="utf-8"))

    def _check(self, d: dict, mode: str) -> None:
        self.assertEqual(set(d), PERF_TOP_KEYS, f"{mode}：顶层键与契约不一致")
        self.assertEqual(d["mode"], mode)
        self.assertIn(d["subset"], SUBSETS)
        self.assertIn(d["methodology"], METHODOLOGIES)
        self.assertEqual(set(d["window"]), {"start", "end"})
        self.assertTrue(d["strategies"], f"{mode}：夹具至少要有一条策略")
        for s in d["strategies"]:
            self.assertEqual(set(s), STRATEGY_KEYS, f"{mode}：策略 {s.get('key')} 的键")
            self.assertIn(s["status"], STATUSES)
            self.assertEqual(s["available"], s["status"] == "ok",
                             f"{mode}：{s['key']} available 与 status 打架")
            if not s["available"]:
                self.assertEqual(s["curve"], [], "不可用的策略不能带曲线——否则会被画成有数据")
            for pt in s["curve"]:
                self.assertEqual(set(pt), CURVE_POINT_KEYS)
        self.assertEqual(set(d["benchmarks"]), BENCHMARK_KEYS)
        self.assertEqual(set(d["benchmarks"]["spy"]), {"name", "curve"})
        self.assertEqual(set(d["benchmarks"]["buy_all"]), {"name", "curve", "available", "reason"})
        wp = d["weekly_pnl"]
        self.assertEqual(set(wp), WEEKLY_KEYS)
        for w in wp["weeks"]:
            self.assertEqual(set(w), WEEK_KEYS)
        keys = {s["key"] for s in d["strategies"]}
        for k, cells in wp["by_strategy"].items():
            self.assertIn(k, keys, f"{mode}：by_strategy 里有名单之外的策略 {k}")
            for c in cells:
                self.assertEqual(set(c), WEEKLY_CELL_KEYS, f"{mode}：{k} 的周格子键")
                # 对账：分解相加 == 期末 − 期初（未对账的格子允许残差）
                parts = c["realized"] + c["unrealized_chg"] + c["cash_income"] + c["fees"] + c["flows"]
                if c["reconciled"]:
                    self.assertAlmostEqual(parts, c["equity_end"] - c["equity_start"], places=1,
                                           msg=f"{mode}：{k} {c['week']} 标了已对账，分解却对不上")
        sm = d["summary"]
        self.assertEqual(set(sm), SUMMARY_KEYS)
        self.assertIn(sm["cash_share_basis"], {"end", "avg"})
        self.assertEqual({r["key"] for r in sm["rows"]}, keys,
                         f"{mode}：汇总表的行必须与策略名单一一对应（全部行，不截取）")
        for r in sm["rows"]:
            self.assertEqual(set(r), SUMMARY_ROW_KEYS, f"{mode}：汇总行 {r.get('key')} 的键")
            if r["status"] != "ok":
                self.assertIsNone(r["cum_ret_pct"], "失败/未运行的行不能带一个收益数——那就是画成零收益的成功期")
        rd = sm["roster_diff"]
        self.assertEqual(set(rd), ROSTER_DIFF_KEYS)
        self.assertIn(rd["other_mode"], MODES - {mode})
        for a in rd["absent"]:
            self.assertEqual(set(a), {"key", "reason"})
        at = d["attribution"]
        self.assertEqual(set(at), ATTRIBUTION_KEYS)
        for r in at["by_topic"] + at["by_method"]:
            self.assertEqual(set(r), ATTR_ROW_KEYS)
        for r in at["by_instrument"]:
            self.assertEqual(set(r), ATTR_ROW_KEYS | ATTR_INSTRUMENT_EXTRA)
        self.assertEqual(set(d["research"]), RESEARCH_KEYS)
        self.assertEqual(set(d["records"]), RECORDS_KEYS)
        for x in d["records"]["failed_runs"]:
            self.assertEqual(set(x), {"as_of", "run_id", "error"})
        for x in d["records"]["stuck_batches"]:
            self.assertEqual(set(x), {"batch_id", "as_of", "n_ideas", "blocked_by"})
        for x in d["records"]["affected_ranges"]:
            self.assertEqual(set(x), {"start", "end", "why"})
        self.assertIsInstance(d["disclosures"], list)

    def test_paper_fixture(self):
        d = self._load("perf_paper_sample.json")
        self._check(d, "paper")
        self.assertEqual(d["subset"], "live", "模拟运行夹具默认是按时运行那一份")
        self.assertEqual(d["methodology"], "paper-rules")

    def test_backtest_fixture(self):
        d = self._load("perf_backtest_sample.json")
        self._check(d, "backtest")
        self.assertEqual(d["methodology"], "stock-picking-study-30d",
                         "现有回测是 30 天持有的简化版，夹具要如实标成选股能力研究")

    def test_the_two_modes_have_different_rosters(self):
        """两份名单不同（10 + 2 − 1 = 11），且「来源限定·AI 端到端」与「AI 端到端选取」是两个 key。"""
        paper = {s["key"] for s in self._load("perf_paper_sample.json")["strategies"]}
        bt = {s["key"] for s in self._load("perf_backtest_sample.json")["strategies"]}
        self.assertNotEqual(paper, bt)
        self.assertIn("generated_ai_native", bt)
        self.assertIn("ai_native", paper)
        self.assertNotIn("ai_native", bt)
        rd = self._load("perf_backtest_sample.json")["summary"]["roster_diff"]
        self.assertEqual(set(rd["added"]), bt - paper)
        self.assertEqual({a["key"] for a in rd["absent"]}, paper - bt)


class PerfPageStructure(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _src()
        self.js = _script(self.src)

    def test_views_has_perf_with_all_sections(self):
        block = self.js[self.js.index("var VIEWS=["):]
        m = re.search(r"\{id:'perf',label:'业绩'.*?(?=\{id:'[a-z]+')", block, re.S)
        self.assertIsNotNone(m, "VIEWS 里没有 id:'perf' 的业绩页")
        secs = re.findall(r"\{s:'([a-z]+)'", m.group(0))
        self.assertEqual(secs, ["mode", "nav", "weekly", "table", "attribution", "research", "records"])
        self.assertIn('<section class="view" id="view-perf"></section>', self.src)

    def test_render_all_registers_the_page(self):
        body = _fn(self.js, "renderAll")
        self.assertIn("renderPerf();", body)
        for name in ("renderPerf", "perfModeCard", "perfNavCard", "perfWeeklyCard",
                     "perfTableCard", "perfAttrCard", "perfResearchCard", "perfRecordsCard"):
            self.assertRegex(self.js, r"\nfunction " + name + r"\(", f"缺少 {name}")

    def test_every_perf_section_is_rendered_through_secwrap(self):
        body = _fn(self.js, "renderPerf")
        for sec in ("mode", "nav", "weekly", "table", "attribution", "research", "records"):
            self.assertIn(f"secWrap('perf','{sec}',", body)

    def test_route_carries_mode_and_subset(self):
        self.assertIn("mode=([^&]*)", _fn(self.js, "parseRoute"))
        self.assertIn("'mode='+PERF.mode", _fn(self.js, "routeQuery"))
        self.assertIn("r.perf", _fn(self.js, "applyRoute"))

    def test_mode_switch_has_three_options_and_live_reads_perf_index(self):
        modes = re.search(r"var PERF_MODES=\[(.*?)\];", self.js, re.S).group(1)
        self.assertEqual(re.findall(r"mode:'([a-z]+)'", modes), ["paper", "backtest", "live"])
        subs = re.search(r"var PERF_SUBSETS=\[(.*?)\];", self.js, re.S).group(1)
        self.assertEqual(re.findall(r"s:'([a-z]+)'", subs), ["live", "backfill", "all"])
        self.assertIn("perf_index", _fn(self.js, "perfIndexLive"))
        card = _fn(self.js, "perfModeCard")
        self.assertIn("perfIndexLive()", card)
        self.assertIn("disabled", card, "实盘不可用时按钮要禁用")
        self.assertIn("lv.reason", card, "禁用还得写原因")

    def test_fetch_never_shows_another_modes_data(self):
        """缓存键含 mode 与 subset；响应自报的 mode 与请求不一致时拒绝显示。"""
        self.assertIn("PERF.mode+'/'+perfSubsetOf(PERF.mode)", _fn(self.js, "perfKey"))
        fetch = _fn(self.js, "perfFetch")
        self.assertIn("j.mode!==PERF.mode", fetch)
        self.assertIn("404", fetch, "数据层未就绪（404）要显示成状态，不能白屏")
        self.assertIn("数据层未就绪", fetch)

    def test_fixture_switch_is_off_by_default(self):
        fx = _fn(self.js, "perfFixtureOn")
        self.assertIn("fixture=1", fx)
        self.assertIn("location.search", fx, "夹具开关只认地址栏 ?fixture=1，不认别的")

    def test_summary_table_header_and_rows_share_one_column_list(self):
        """表头和行都从 PERF_COLS 生成；行里的 <td> 数 == 列定义数。"""
        cols = re.search(r"var PERF_COLS=\[(.*?)\n\];", self.js, re.S).group(1)
        n_cols = len(re.findall(r"\{k:'", cols))
        card = _fn(self.js, "perfTableCard")
        self.assertIn("PERF_COLS.map(function(c){return perfTh(c,P)})", card)
        row = re.search(r"return '<tr>'\n(.*?)\+'</tr>';", card, re.S)
        self.assertIsNotNone(row, "找不到汇总表的行生成器")
        n_td = len(re.findall(r"\+'<td\b", row.group(1)))
        self.assertEqual(n_td, n_cols, f"汇总表每行 {n_td} 格，列定义 {n_cols} 列")
        # 现金占比列的口径来自数据（end/avg），不写死
        self.assertIn("cash_share_basis", _fn(self.js, "perfColLabel"))
        self.assertIn("cash_share_basis", _fn(self.js, "perfCashKey"))

    def test_attribution_table_header_row_and_total_agree(self):
        card = _fn(self.js, "perfAttrCard")
        thead = re.search(r"<thead><tr>'(.*?)</tr></thead>", card, re.S).group(1)
        n_th = len(re.findall(r"'<th\b", thead))
        row = re.search(r"return '<tr>'\n(.*?)\+'</tr>';", card, re.S)
        self.assertIsNotNone(row)
        n_td = len(re.findall(r"\+'<td\b", row.group(1)))
        total = re.search(r"var total='<tr class=\"pf-total\">'\n(.*?)\+'</tr>';", card, re.S)
        self.assertIsNotNone(total)
        n_total = len(re.findall(r"\+'<td\b", total.group(1)))
        self.assertEqual(n_td, n_th, f"归因表每行 {n_td} 格，表头 {n_th} 列")
        self.assertEqual(n_total, n_th, f"归因合计行 {n_total} 格，表头 {n_th} 列")
        for dim in ("by_topic", "by_instrument", "by_method"):
            self.assertIn(dim, card)
        self.assertIn("at.rule", card, "分配规则原文来自数据")

    def test_weekly_table_breakdown_spans_all_columns(self):
        card = _fn(self.js, "perfWeeklyCard")
        self.assertIn("colspan=\"'+(1+strats.length)+'\"", card)
        for k in ("realized", "unrealized_chg", "cash_income", "fees", "flows", "reconciled", "residual"):
            self.assertIn(k, card, f"周损益分解缺 {k}")

    def test_nav_card_normalises_and_flags_unequal_samples(self):
        card = _fn(self.js, "perfNavCard")
        self.assertIn("/base*100", card, "起点归一化为 100")
        self.assertIn("样本长度不同", card)
        self.assertIn("affected_ranges", card, "受影响区间要画到净值图上")
        self.assertRegex(self.js, r"\nfunction perfDrawdownSVG\(", "要画回撤")

    def test_research_fold_only_embeds_backtest_cards_in_backtest_mode(self):
        card = _fn(self.js, "perfResearchCard")
        self.assertIn("P.mode==='backtest'", card)
        for name in ("tearsheetCard()", "rankingPowerCard()", "selectionCard()"):
            self.assertIn(name, card)
        self.assertIn("sample_note", card)

    def test_evidence_page_links_to_perf_page(self):
        self.assertIn("+perfCrossLink()", _fn(self.js, "renderEvidence"))
        self.assertIn("perfGo(", _fn(self.js, "perfCrossLink"))


class BacktestDetailShowsEveryStrategy(unittest.TestCase):
    """Jon 第 9 条：明细不再只取前四行；排序可切；数量标签 == 可见行数；名单对照按 key。"""

    def setUp(self) -> None:
        self.js = _script(_src())
        self.card = _fn(self.js, "backtestCard")

    def test_no_slice_of_the_first_four(self):
        self.assertNotIn(".slice(0,4)", self.card)
        self.assertNotIn(".slice(0, 4)", self.card)

    def test_count_label_and_visible_rows_use_the_same_list(self):
        self.assertIn("names.sort(function(x,y){return btArmCmp(arms,x,y)}).map(", self.card)
        self.assertIn("显示全部 '+fmtInt(names.length)+' / '+fmtInt(names.length)+' 个策略", self.card)

    def test_sort_is_switchable_and_labelled(self):
        self.assertIn("var BT_SORT={k:'hit_rate',dir:-1};", self.js)
        for k in ("hit_rate", "mean_return_pct", "name"):
            self.assertIn(f"btTh('{k}'", self.card)
        self.assertIn("btSortLabel()", self.card)

    def test_roster_note_compares_by_key_and_never_guesses_a_reason(self):
        fn = _fn(self.js, "backtestRosterNote")
        self.assertIn("skipped_need_model", fn)
        self.assertIn("excluded_arms", fn)
        self.assertIn("原因未记录", fn)
        self.assertIn("b.selector", fn, "模拟账户名单来自 S.books 的 selector 键")
        self.assertIn("backtestRosterNote(names,sum)", self.card)


class ScriptParses(unittest.TestCase):
    def test_node_check(self):
        node = shutil.which("node") or (Path("/opt/homebrew/bin/node") if Path("/opt/homebrew/bin/node").exists() else None)
        if not node:
            self.skipTest("没有 node，跳过语法检查")
        js = _script(_src())
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dash.js"
            p.write_text(js, encoding="utf-8")
            r = subprocess.run([str(node), "--check", str(p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"node --check 不通过：\n{r.stderr[-2000:]}")


if __name__ == "__main__":
    unittest.main()
