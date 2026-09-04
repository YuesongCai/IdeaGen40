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
