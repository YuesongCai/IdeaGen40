"""Structural contracts for web/dash.html that nothing else can catch.

CSS 栅格和 HTML 表格都有一个共同的失败模式：**往行里加一格，忘了给它加一列**。
两边都不会报错，页面照常渲染——只是所有东西错位一格。2026-09-05 凌晨真发生过：
持仓行加了一颗「问它为什么选它」按钮，`.pos-line` 的栅格还是四列，于是名称落进
刻度条那一列、刻度条掉进 78px 的浮动列被裁掉、浮动金额换行压到标的下面，行高
从 26 涨到 66。同一个提交里候选池表也多了一个 `<td>`。

这类偏差没有异常、没有控制台报错、快照测试也照过——只有人眼看得出来。所以在这里
把「格子数 == 列数」钉成一条可执行的断言。

刻意只覆盖那些**列数写死**的组件：列宽由内容决定的表格不在此列，它们加一列不会错位。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "web" / "dash.html"


def _tracks(decl: str) -> int:
    """Count columns in a grid-template-columns value.

    括号里的空格不算分隔符：`minmax(0, 1fr)` 是一列不是两列。
    """
    depth = 0
    n = 0
    in_token = False
    for ch in decl.strip():
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            in_token = False
        elif not in_token:
            in_token = True
            n += 1
    return n


def _rule(css: str, selector: str) -> str:
    """The body of the first `selector{...}` rule."""
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", css)
    assert m, f"找不到 {selector} 的样式规则"
    return m.group(1)


def _grid_cols(css: str, selector: str) -> int:
    body = _rule(css, selector)
    m = re.search(r"grid-template-columns:([^;}]+)", body)
    assert m, f"{selector} 没有写 grid-template-columns"
    return _tracks(m.group(1))


def _top_level_spans(html: str) -> int:
    """Count `<span>` elements at nesting depth 0 of an HTML fragment.

    表头里嵌着 `<span class="gloss">ⓘ</span>`，直接数 `<span` 会多算。
    """
    depth = 0
    n = 0
    for tag in re.finditer(r"</?span\b", html):
        if tag.group(0).startswith("</"):
            depth -= 1
        else:
            if depth == 0:
                n += 1
            depth += 1
    return n


class DashGridContracts(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.src = DASH.read_text(encoding="utf-8")

    # ── 持仓行：栅格列数 == 行里的格子数 == 表头里的格子数 ──────────────
    def test_position_row_cells_match_its_grid_columns(self):
        cols_row = _grid_cols(self.src, ".pos-line")
        cols_head = _grid_cols(self.src, ".pos-headr")
        self.assertEqual(
            cols_row, cols_head,
            "行和表头必须用同一套列宽，否则表头标签会错位到别的列上面")

        # 行的生成器：`'<div class="pos-line">'` 之后每一个 `+` 项就是一个格子，
        # 直到 `+'</div>'`。rngBarHTML(p) 也算一格——它返回单个 <span>。
        block = re.search(
            r"return '<div class=\"pos-line\">'\n(.*?)\n\s*\+'</div>';",
            self.src, re.S)
        self.assertIsNotNone(block, "找不到 .pos-line 的生成器")
        cells_row = sum(1 for ln in block.group(1).splitlines()
                        if ln.lstrip().startswith("+"))
        self.assertEqual(
            cells_row, cols_row,
            f"持仓行发出 {cells_row} 个格子，栅格只有 {cols_row} 列。"
            "多出来的格子会换行到下一行最左边，其余每一格都往右错一位。")

        head = re.search(r"'<div class=\"pos-headr\">(.*?)</div>'", self.src, re.S)
        self.assertIsNotNone(head, "找不到 .pos-headr 的表头字符串")
        cells_head = _top_level_spans(head.group(1))
        self.assertEqual(
            cells_head, cols_head,
            f"表头写了 {cells_head} 个标签，栅格有 {cols_head} 列。"
            "没有标签的那一列也要留一个空 <span> 占位，否则标签整体左移。")

    def test_narrow_position_row_assigns_every_cell_by_position(self):
        """窄屏那一档把刻度条挪到第二行，靠 grid-template-areas + nth-child 指派。

        单元格必须按**位置**指派而不是按类名：σ 算不出时第四格换成的是
        `.card-note` 而不是 `.rngwrap`，按类名写的规则会漏掉它，那一格会被
        自动排版塞回第一行、把后面的格子挤走。
        """
        cells = _grid_cols(self.src, ".pos-line")
        # 文件里不止一个 1180px 断点，挑出装着 .pos-line 的那个
        body = next((b for b in re.findall(
            r"@media \(max-width:1180px\)\{(.*?)\n\}", self.src, re.S)
            if ".pos-line" in b), None)
        self.assertIsNotNone(body, "找不到 .pos-line 的窄屏那一档")
        areas = re.search(r'grid-template-areas:([^;]+);', body)
        self.assertIsNotNone(areas, "窄屏那一档没有写 grid-template-areas")
        names = set(re.findall(r"[a-z]+", areas.group(1)))
        assigned = {int(m) for m in re.findall(
            r"\.pos-line>:nth-child\((\d+)\)\{grid-area:", body)}
        self.assertEqual(
            assigned, set(range(1, cells + 1)),
            f"窄屏这一档要给全部 {cells} 个格子逐位指派，现在只指派了 {sorted(assigned)}")
        self.assertEqual(
            len(names), cells,
            f"grid-template-areas 命名了 {len(names)} 个区域，格子有 {cells} 个")

    # ── 候选池全表：表头列数 == 行里的 <td> 数 == 展开行的 colspan ────────
    def test_candidate_table_header_row_and_colspan_agree(self):
        thead = re.search(
            r"\+'<thead><tr>'(.*?)</tr></thead>'", self.src, re.S)
        self.assertIsNotNone(thead, "找不到候选池表的表头")
        n_th = (thead.group(1).count("candTh(")
                + len(re.findall(r"'<th\b|<th ", thead.group(1))))

        # 只看 renderCandBody 里的那一段——页面上还有别的表也用 `return '<tr>'`
        fn = re.search(r"function renderCandBody\(\)\{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(fn, "找不到 renderCandBody")
        row = re.search(r"return '<tr>'\n(.*?)</td></tr>'", fn.group(1), re.S)
        self.assertIsNotNone(row, "找不到候选池表的行生成器")
        n_td = len(re.findall(r"\+'<td\b", row.group(1)))

        self.assertEqual(
            n_td, n_th,
            f"候选池表每行 {n_td} 格，表头 {n_th} 列。"
            "多一格就整行右移，最右边那一列被推出可视区。")

        spans = {int(m) for m in re.findall(r'colspan="(\d+)"', fn.group(1))}
        self.assertIn(
            n_th, spans,
            f"表头有 {n_th} 列，但没有任何 colspan 等于它——"
            "展开的论点行/空态行会横不满整张表")


if __name__ == "__main__":
    unittest.main()


class SwappingTheBoxMustBeatTheTypingGuard(unittest.TestCase):
    """换掉输入框里的内容之前，得先把焦点从框里挪开。

    `renderDrawers` 有一道守卫：光标还在 `#phSay` 里就不重画，并把框里的内容
    收回 `PH.say`。它是对的——每 60 秒一次的整页轮询会把打了一半的话连同光标
    一起换掉，这个功能里为这件事已经付过两次代价。

    但同一道守卫会拦下**故意要换掉框里内容**的那一类动作：接着写另一条草稿、
    照一条在跑的准则改一版、填一个示例、生成草案之后清空。2026-09-05 实测：
    光标停在框里点「接着写」，`PH.draftId` 换了、`PH.arm` 换了，框里的字一个
    没变——从外面看就是点了没反应，而人下一次输入会把新草稿的内容改成旧那条。

    没有异常、没有报错，和本文件开头那两类同宗。所以钉成断言：凡是给 `PH.say`
    赋一个新值的函数，都必须先叫 `phBlurBox()`。
    """

    #: 例外，各自说明为什么。`test_every_exemption_still_points_at_a_real_function`
    #: 会检查它们仍然存在，免得名字改了之后这里悄悄放行一个不该放行的。
    EXEMPT = {
        "phDraftTouch": "它就是 oninput 本身：人正在框里打字，挪开焦点等于打断他",
        "phTyping": "轮询让路前把话收好，读的是框里的现值，不是要换掉它",
        "phSay": "同上，读框里的现值",
        "renderDrawers": "守卫本身",
        # 回填草稿的分支带着 `!(PH.say||'').trim()` 这个条件：框是空的、也没人
        # 动过，才谈得上回填——没有要换掉的东西。守卫最多让这一帧不画，之后
        # 任何一次点击都会把它补上。反过来，在这里强行挪走焦点会打断一个刚
        # 点进框、正准备写的人。
        "phLoad": "只在框空着且无人动过时回填，没有要换掉的内容",
    }

    _ASSIGN = re.compile(r"\bPH\.say\s*=")
    _FN_HEAD = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\(")

    # 自带切分，不用文件里别处的公用件。原因不是洁癖：2026-09-05 这个仓里
    # 一晚上栽过两次「测试进了 HEAD、它依赖的东西还留在某人的工作区」——
    # 闸门 fail-closed，所有人的云端部署一起停，而红的原因看起来跟那个人
    # 毫无关系。一段测试只要引用别人尚未提交的符号，它就随时会以这种方式
    # 变红。十行重复换掉这种耦合，划算。
    @staticmethod
    def _js(src):
        return re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)[-1]

    @classmethod
    def _fns(cls, js):
        """(函数名, 函数体) —— 按顶层 `function` 粗切，够用且不需要 JS 解析器。"""
        for chunk in re.split(r"\n(?=function\s+[A-Za-z_$][\w$]*\s*\()", js):
            m = cls._FN_HEAD.match(chunk)
            if m:
                yield m.group(1), chunk

    def _offenders(self, src):
        bad, scanned = [], 0
        for name, body in self._fns(self._js(src)):
            if not self._ASSIGN.search(body) or name in self.EXEMPT:
                continue
            scanned += 1
            if "phBlurBox()" not in body:
                bad.append(name)
        return bad, scanned

    def test_every_exemption_still_points_at_a_real_function(self):
        src = DASH.read_text(encoding="utf-8")
        names = {n for n, _ in self._fns(self._js(src))}
        missing = sorted(set(self.EXEMPT) - names)
        self.assertFalse(missing,
                         f"例外名单指向已经不存在的函数：{missing}——"
                         "改名的时候这条放行就变成了一个洞")

    def test_nothing_swaps_the_box_without_blurring_it_first(self):
        bad, scanned = self._offenders(DASH.read_text(encoding="utf-8"))
        self.assertTrue(scanned >= 4,
                        f"只扫到 {scanned} 个改写 PH.say 的函数，切分方式可能失效了")
        self.assertFalse(bad,
                         "这些函数要换掉输入框里的内容，却没先把焦点挪开。\n"
                         "光标恰好停在框里时，那次重画会被「正在打字就不重画」的\n"
                         "守卫整个吃掉：状态换了，框里的字没换，看起来像点了没反应。\n"
                         f"在函数开头加一句 phBlurBox()：{bad}")

    def test_the_gate_can_actually_see_a_violation(self):
        """闸门自己不被验，就可能一直绿着却什么都没看。"""
        src = DASH.read_text(encoding="utf-8")
        self.assertEqual([], self._offenders(src)[0], "现在就有漏的，先修它")
        broken = src.replace("  phBlurBox();\n  phDraftSave();"
                             "          /* 桌上原来那条先落盘", "  phDraftSave();"
                             "          /* 桌上原来那条先落盘", 1)
        self.assertNotEqual(src, broken, "没能造出回归样本，自检失效了")
        self.assertIn("phReviseFrom", self._offenders(broken)[0],
                      "拿掉 phBlurBox 后闸门居然没红——它没在看")
