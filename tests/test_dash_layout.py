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


# ── 拆 dash.html 的公用件（下面两道结构闸门共用） ──────────────────────
_STR_LIT = re.compile(r"'((?:[^'\\]|\\.)*)'")
_FN_HEAD = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\(")


def _script(src: str) -> str:
    return re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)[-1]


def _functions(js: str):
    """(函数名, 函数体) —— 按顶层 `function` 粗切，够用且不需要 JS 解析器。"""
    for chunk in re.split(r"\n(?=function\s+[A-Za-z_$][\w$]*\s*\()", js):
        m = _FN_HEAD.match(chunk)
        if m:
            yield m.group(1), chunk


def _return_exprs(body: str):
    """每个 return 的表达式原文，到顶层分号为止；字符串里的分号不算。

    看 return 表达式而不是整个函数体，是因为要判的是**拼接顺序**，而源码里的
    书写顺序不等于它——`rankingPowerCard` 的 `var lead=…` 声明在 `meta` 之后，
    用却在之前。答案在 `return` 那一串 `+` 里。
    """
    for m in re.finditer(r"\breturn\b", body):
        i, depth, quote, buf = m.end(), 0, None, []
        while i < len(body):
            ch = body[i]
            if quote:
                buf.append(ch)
                if ch == "\\":
                    i += 1
                    if i < len(body):
                        buf.append(body[i])
                elif ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
                buf.append(ch)
            elif ch in "([{":
                depth += 1
                buf.append(ch)
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
                buf.append(ch)
            elif ch == ";" and depth == 0:
                break
            else:
                buf.append(ch)
            i += 1
        yield "".join(buf)


def _secwrap_calls(js: str):
    """(视图, 段名, 第三个参数原文) —— 逐个切出 secWrap(view, sec, body)。"""
    for m in re.finditer(r"secWrap\(", js):
        i, depth, quote, buf, out = m.end(), 0, None, [], []
        while i < len(js):
            ch = js[i]
            if quote:
                buf.append(ch)
                if ch == "\\":
                    i += 1
                    if i < len(js):
                        buf.append(js[i])
                elif ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
                buf.append(ch)
            elif ch in "([{":
                depth += 1
                buf.append(ch)
            elif ch in ")]}":
                if depth == 0:
                    out.append("".join(buf))
                    break
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                out.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1
        if len(out) >= 3:
            a0, a1 = out[0].strip(), out[1].strip()
            # 只认字符串字面量。不加这一条，`function secWrap(view,slug,inner)`
            # 这个**定义**会被当成一次调用，切出一个根本不存在的段 ('view','slug')。
            if len(a0) > 2 and a0[0] == a0[-1] == "'" and len(a1) > 2 and a1[0] == a1[-1] == "'":
                yield a0[1:-1], a1[1:-1], out[2]


def _toplevel_pluses(expr: str) -> int:
    depth, quote, n = 0, None, 0
    for i, ch in enumerate(expr):
        if quote:
            if ch == quote and expr[i - 1] != "\\":
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "+" and depth == 0:
            n += 1
    return n


class CardDivsClose(unittest.TestCase):
    """一张卡的外层 `<div>` 忘了闭合，浏览器会替它兜底——直到有人在它后面加东西。

    2026-09-05 发生过：`rankingPowerCard` 的 `</div>` 缺了不知道多久，一直没人
    看得出来，因为那一段后面什么都没有，HTML 解析器在段落末尾自动收口。把
    「当期实跑 vs 事后补跑」拆成同级的第二张卡之后，第二张卡直接掉进第一张里，
    成了卡中卡——两圈边框、两层内边距。

    这类偏差和本文件开头说的那一类同宗：没有异常、没有控制台报错、快照照过。

    判据是**不对称**的，这一点是刻意的：
      * `<div>` 多于 `</div>` —— 有卡没关上，就是这个 bug，拦。
      * `</div>` 多于 `<div>` —— 多条 return 各自把同一个开头闭合一次
        （`phTeachCard` / `universeCard` 就是），或者闭合的是调用方开的壳。
        这是正常写法，不拦。
    按这条扫，59 个产出 `.card` 的函数当前零误报。

    只看字符串字面量里的标签，所以 `foldBlock(...)` 这类返回值自洽的辅助函数
    不参与计数——它们自己平衡与否由它们自己负责。
    """

    _STR = re.compile(r"'((?:[^'\\]|\\.)*)'")
    _FN = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\(")

    def _scan(self, src: str):
        unclosed, scanned = [], 0
        for name, body in _functions(_script(src)):
            blob = "".join(self._STR.findall(body))
            if 'class="card' not in blob:
                continue
            scanned += 1
            opens = len(re.findall(r"<div\b", blob))
            closes = blob.count("</div>")
            if opens > closes:
                unclosed.append(f"  {name}(): <div> ×{opens} 但 </div> ×{closes}"
                                f" —— 少 {opens - closes} 个闭合")
        return unclosed, scanned

    def test_the_gate_can_actually_see_a_violation(self):
        """闸门自己不被验，就可能一直绿着却什么都没看。

        把 `rankingPowerCard` 的闭合标签拿掉——也就是 2026-09-05 之前它真实
        的样子——闸门必须红，而且必须点出是哪个函数。
        """
        src = DASH.read_text(encoding="utf-8")
        self.assertEqual(([], ) [0], self._scan(src)[0],
                         "现在就有没关上的卡，先修它，再谈自检")
        # 锚点必须唯一指向 rankingPowerCard 的那一处：第一版用了
        # `+'</div>';` 这个片段，结果命中的是 robustNote()，自检自己抓到了自己。
        closing = "成了卡中卡。 */\n    +'</div>';"
        self.assertIn(closing, src, "rankingPowerCard 的形状变了，这条自检要跟着改")
        broken = src.replace(closing, "成了卡中卡。 */\n    ;", 1)
        self.assertNotEqual(src, broken, "没能造出回归样本，自检失效了")
        found, _ = self._scan(broken)
        self.assertTrue(any("rankingPowerCard" in f for f in found),
                        f"拿掉闭合标签后闸门居然没红——它没在看。实际结果：{found}")

    def test_every_card_builder_closes_the_div_it_opened(self):
        unclosed, scanned = self._scan(DASH.read_text(encoding="utf-8"))
        self.assertTrue(scanned >= 20,
                        f"只扫到 {scanned} 个产出卡片的函数，切分方式可能失效了")
        self.assertFalse(unclosed,
                         "有卡片的 <div> 没关上。浏览器会替它兜底，直到有人在它后面\n"
                         "加了同级的下一张卡——那张卡会掉进这张里：\n\n"
                         + "\n".join(unclosed))


class ConclusionBeforeMetadata(unittest.TestCase):
    """一张卡先甩数字、后给结论，读者要自己归纳这张卡想说什么。

    2026-09-05 的「历史回测」卡就是这样：源码里写着 `/* 结论先行 */` 的注释，
    摆放却是反的——`pairedVerdictLine(sum)` 排在窗口/期数/持有期/时间钳制
    四组元数据之后，读者要先跨过十五个数字才知道这张卡的判定是「无选取策略
    被判定跑赢全量基准」。同一天「排序力」卡则是压根没有结论句，开头是五个
    没有主语的数字。本页其余每张卡都是标题之后一句话。

    判的是**拼接顺序**，所以看 `return` 表达式而不是函数体：元数据条
    （`bt-meta` / `meta-kv`）不得出现在结论之前。元数据是结论的脚注，
    不是它的前置条件。

    静态查得了，不需要浏览器——这是刻意的。CI 里跳过的测试等于没有测试，
    而这个仓的 `cloudsync` 推代码前跑的就是这一套。
    """

    #: 结论的写法（本文件只认这几种，新增写法要一起加进来）
    _LEAD = re.compile(r'pairedVerdictLine\s*\(|\+\s*lead\b|\+\s*lede\b'
                       r'|class="vd"|class="quiet"|font:400 15px/23px')
    _META = re.compile(r'\+\s*meta\b|class="bt-meta"|class="meta-kv"')

    #: 豁免必须写理由，理由是留给下一个人看的，不是留给闸门看的。
    EXEMPT = {
        "bookCard":
            "十个组合并排的小卡，靠横向对比读，不是各自成篇。"
            "每张都写一句结论是十句噪音；结论在它们上面那张「选取策略对比」卡里。",
        "backtestSection":
            "「完整回测 · 可选窗口」是折在「历史回测」里的子卡，"
            "父卡开头就是配对检验的判定，子卡再说一遍就是同一句话讲两遍。",
    }

    def _offenders(self, src: str):
        out, seen = [], 0
        for name, body in _functions(_script(src)):
            for expr in _return_exprs(body):
                if 'class="card' not in expr:
                    continue
                seen += 1
                meta = self._META.search(expr)
                if not meta:
                    continue
                lead = self._LEAD.search(expr)
                if not lead or lead.start() > meta.start():
                    out.append((name, "结论排在元数据之后" if lead else "没有结论句"))
        return out, seen

    def test_metadata_never_precedes_the_cards_conclusion(self):
        found, seen = self._offenders(DASH.read_text(encoding="utf-8"))
        # 取样下限。闸门自己的切分方式也会坏，而它坏的时候是**绿的**——
        # 一个正则跨过了收尾、或者写法从 `return '<div…` 改成了 `var h=…;return h`，
        # 它就什么都扫不到，然后安静地通过。数一遍看了多少张卡，比结论更早报警。
        self.assertGreaterEqual(seen, 12,
                                f"只扫到 {seen} 个产出卡片的 return，切分方式可能失效了")
        bad = [f"  {n}(): {why}" for n, why in found if n not in self.EXEMPT]
        self.assertFalse(bad,
                         "有卡片把数字摆在了结论前面。读者第一屏看到的应该是这张卡\n"
                         "想说什么，元数据是它的脚注：\n\n" + "\n".join(bad)
                         + "\n\n（确有必要的例外加进 EXEMPT，并写清理由。）")

    def test_every_exemption_still_points_at_a_real_function(self):
        """豁免会烂掉：函数改名或删掉之后，那条理由就变成了误导下一个人的注释。"""
        names = {n for n, _ in _functions(_script(DASH.read_text(encoding="utf-8")))}
        gone = sorted(set(self.EXEMPT) - names)
        self.assertFalse(gone, f"EXEMPT 里这些函数已经不在了，把它们删掉：{gone}")

    def test_the_gate_can_actually_see_a_violation(self):
        """闸门自己不被验，就可能一直绿着却什么都没看。

        把「历史回测」那一处改回它 2026-09-05 之前的样子——结论挪到元数据
        之后——闸门必须红。
        """
        src = DASH.read_text(encoding="utf-8")
        before = "+pairedVerdictLine(sum)\n    +'<div class=\"bt-meta\">'"
        self.assertIn(before, src, "「历史回测」的拼接顺序变了，这条自检要跟着改")
        regressed = src.replace(before, "+'<div class=\"bt-meta\">'", 1)
        self.assertIn("backtestCard", [n for n, _ in self._offenders(regressed)[0]],
                      "把结论挪到元数据之后，闸门居然没红——它没在看")


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


class OneSectionOneCard(unittest.TestCase):
    """一个段里塞几张卡，侧栏就只能用一个名字叫它们。

    2026-09-05 的「概览」页：`sec_overview_verdict` 一个段装着三张卡——
    「它管用吗」260px、「目前唯一站得住的发现」310px、「回测里，十条选取策略
    的次序」464px，合计 1034px、三个不同的问题，而侧栏上只有一项「结论」。
    后两张是首屏最大的卡，**在导航上根本不存在**；点「结论」落到一千像素的
    开头，然后自己往下找。渲染那里的注释当时已经写着「① 证据进度
    ② 目前学到了什么 ③ 回测里的次序」——三件事早就分清楚了，只是没写进 VIEWS。

    这一页两小时里从 2200px 长到 4900px（展开全部折叠是 44,000px），十几个会话
    同时在往里加。手工拆追不上，所以把「一段一张卡」钉成默认：**要在一个段里
    放第二张卡，得先在 EXEMPT 里写清为什么**——那句理由留给下一个人看。

    判据是 `secWrap(view, sec, body)` 第三个参数里的**顶层 `+` 个数**。
    括号内的 `+` 不算：`foldBlock(a, b, x + y)` 是一张卡内部的事。
    """

    #: 例外要写理由。理由是给下一个人看的，不是给闸门看的。
    EXEMPT = {
        ("overview", "health"):
            "状态条 + 它自己的两个折叠明细是一个逻辑单元：状态条是结论，"
            "「系统自检」「运行记录」是它的下一层。分成两段会让侧栏出现"
            "一个点进去只有一行字的段。",
        ("evidence", "receipt"):
            "运行回执条是一整条：它说的是「这一页的数字来自哪一次运行」，"
            "拆开之后每一半都不成句。",
    }

    def _multi(self, src: str):
        js = _script(src)
        out, seen = [], 0
        for view, sec, body in _secwrap_calls(js):
            seen += 1
            n = _toplevel_pluses(body)
            if n >= 1:
                out.append((view, sec, n))
        self._seen = seen
        return out

    def test_a_section_holds_one_card(self):
        found = self._multi(DASH.read_text(encoding="utf-8"))
        # 取样下限，理由同 ConclusionBeforeMetadata：切分坏掉时它是绿的。
        self.assertGreaterEqual(self._seen, 15,
                                f"只切出 {self._seen} 个 secWrap 调用，切分方式可能失效了")
        bad = [f"  secWrap('{v}','{s}', …) 里有 {n + 1} 张卡"
               for v, s, n in found if (v, s) not in self.EXEMPT]
        self.assertFalse(bad,
                         "一个段里放了不止一张卡，侧栏就只能用一个名字叫它们，\n"
                         "而多出来的那些在导航上等于不存在：\n\n" + "\n".join(bad)
                         + "\n\n（拆成各自成段，或加进 EXEMPT 并写清理由。）")

    def test_every_exemption_still_names_a_real_section(self):
        js = _script(DASH.read_text(encoding="utf-8"))
        live = {(v, s) for v, s, _ in _secwrap_calls(js)}
        gone = sorted(set(self.EXEMPT) - live)
        self.assertFalse(gone, f"EXEMPT 里这些段已经不在了，删掉它们：{gone}")

    def test_every_section_rendered_is_named_in_the_sidebar(self):
        """渲染出来的段必须在 VIEWS 里有名字，否则它在导航上不存在。"""
        src = DASH.read_text(encoding="utf-8")
        js = _script(src)
        block = js[js.index("var VIEWS=["):]
        # 按 {id:'…'} 的边界切，不设固定窗口——第一版用了 1200 字，装不下
        # 「证据」页的九个段，于是九段全被判成「导航上没有名字」。
        marks = [m for m in re.finditer(r"\{id:'([a-z]+)'", block)]
        declared = set()
        for i, vm in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
            for sm in re.finditer(r"\{s:'([a-z]+)'", block[vm.end():end]):
                declared.add((vm.group(1), sm.group(1)))
        rendered = {(v, s) for v, s, _ in _secwrap_calls(js)}
        orphan = sorted(rendered - declared)
        self.assertFalse(orphan,
                         f"这些段渲染出来了，但 VIEWS 里没有名字，侧栏到不了：{orphan}")

    def test_the_gate_can_actually_see_a_violation(self):
        """把两张卡塞回一个段——闸门必须红。

        锚点原来指的是概览上的「证据进度 / 学到了什么 / 策略次序」三张卡。
        那三张 2026-09-05 搬去了证据页（判定要和证据待在一起），锚点跟着搬到
        概览现存的一对相邻段上。这条自检本身就是为了防止闸门一直绿着却什么
        都没看，所以锚点没了要改锚点，不是把这条测试删掉。
        """
        src = DASH.read_text(encoding="utf-8")
        now = ("+secWrap('overview','now',nowCard(ag))")
        led = ("+secWrap('overview','ledger',ledgerCard(ag))")
        self.assertIn(now, src, "概览的分段变了，这条自检要跟着改")
        self.assertIn(led, src, "概览的分段变了，这条自检要跟着改")
        before = "+secWrap('overview','now',nowCard(ag)+ledgerCard(ag))"
        merged = src.replace(now, before, 1).replace(led, "", 1)
        found = [(v, s) for v, s, _ in self._multi(merged)]
        self.assertIn(("overview", "now"), found,
                      "两张卡塞回一个段，闸门居然没红——它没在看")
