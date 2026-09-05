"""词表闸门：实现细节的比喻不许漏到读者眼前。

2026-09-05 用户看着面板说「账本到底啥意思，其实这个通篇说到账本就听不懂，
他难道不是一个持有方法吗我理解？对吧，所有这种不 intuitive 的字段都要改」。
他是对的——那东西就是一个选取策略配一个纸面组合，「账本」讲的是它**存在哪儿**，
不是它是什么。全站换词见 `docs/词表.md`。

**为什么需要一道测试而不是一份文档**：换完不到四个小时，「臂」就被写回了两处
（`38b64ca` 里新加的两条表头释义）。这个仓同时有十几个 Claude 会话在写
`web/dash.html`，谁都可能没读过那份文档。文档拦不住这件事，红的测试可以——
而且 `com.ideagen40.cloudsync` 推代码前要跑全量测试，所以这道闸门同时挡住了
「旧词悄悄上云」。

它只管**读者看得到的串**：dash.html 去掉注释后的正文、以及那几个模块里
非 docstring 的字符串字面量。代码注释和 docstring 不管——注册表里的标识符还叫
`arm_name` / `rep.arms`，注释是解释这些代码的，把注释改成「生成方式」而标识符
还是 arm，只会让注释更难对上代码。

要加一条新的禁用词，就往 `BANNED` 里加，并在 `docs/词表.md` 里写上换成什么。
确有必要的例外放进 `ALLOW`，每条都要写清为什么。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 禁用词 → 该写什么。信息会原样印在失败信息里，所以写成能直接照做的话。
BANNED = {
    "账本": "组合（一个选取策略配一个纸面组合；「账本」说的是它存在哪儿）",
    "本账": "个组合（「各记一本账」→「各管一个组合」）",
    "入账": "建仓（读者要问的是买没买，不是记没记）",
    "语料": "研报（输入就是 Wisburg 研报；「语料」是 NLP 黑话）",
    "臂": "按阶段叫真名：筛选A→打分方法、筛选B→生成方式、筛选C/统计检验→组合",
    "挑法": "选取策略（用户 2026-09-04 拍板：术语化优于口语化）",
}

#: 正则规则：有些旧比喻会被别的字劈开，逐字匹配抓不到。
#: 「各记一本模拟账」里那个「一本…账」就整整躲过了 BANNED 的「本账」——
#: 面板上挂了一天，我自己盯着渲染文本才看见。
BANNED_RE = {
    r"[一二三四五六七八九十两几每这那0-9]\s*本[一-鿿]{0,2}账":
        "个组合（「各记一本模拟账」→「各管一个模拟组合」）",
    # 换词换出来的叠词。「研报语料」被全局替换成「研报研报」，在概览的数据源
    # 列表和方法页的来源表上挂了一整天——替换脚本自己制造的毛病，逐字规则
    # 看不见（「研报」本来就是对的词）。
    r"(研报|组合|建仓|选取策略|打分方法|生成方式)\1":
        "去掉重复的那半——这是替换和原文里已有的词撞上了",
}

#: 裸英文：后端字段名漏进中文句子。同一个毛病的另一半——「账本」是内部比喻，
#: 这些是内部标识符，读者两样都不认识。
#:
#: 判据和中文词不同：**只有紧挨着中文时才算**。`live` 在 `class="pchip live"`
#: 里是 CSS 类名、在 `alive` 里是别的词、在 `SELECT as_of FROM` 里是列名——
#: 一律拦就只剩噪音。挨着中文（中间至多一个空格）才是漏进了句子：
#: 「结论以 live 期为准」拦，`licensed-live-` 不拦。
BANNED_EN = {
    "live": "实跑 / 当期实跑（和「补跑」对称，别一半中文一半英文）",
    "as_of": "该期日期 / 该期起（as_of 是字段名，不是说给人听的词）",
}

#: 默认全扫这两棵树。**手写清单挡不住新模块**——第一版列了 13 个文件，
#: 一量才发现另有 14 个模块、68 处旧词漏在外面（booking 的「挑法」、
#: orchestrator 的「没有任何语料」、wisburg 的「增量语料」…）。
#: 所以反过来：默认全查，豁免要写理由。
SCAN_TREES = ("ideagen", "scripts")
SCAN_FILES = ("web/dash.html",)

#: 豁免。每条都要说清为什么这个文件**应该**含旧词。
EXEMPT = {
    "scripts/retag_backtest_summary.py":
        "它本身就是旧词→新词的对照表，不含旧词就没法迁移库里存的摘要",
    "scripts/generate_batch_2026-08-07.py":
        "2026-08-07 那一批的一次性产出与当时的判断记录，改它等于改历史",
}


def _surfaces() -> list[pathlib.Path]:
    out = [ROOT / f for f in SCAN_FILES]
    for tree in SCAN_TREES:
        for p in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            if str(p.relative_to(ROOT)) in EXEMPT:
                continue
            out.append(p)
    return out

#: 故意保留的例外。整串精确匹配，每条注明理由。
ALLOW = {
    # 送进模型的 prompt。改它是行为变更，不是文案变更——派生臂的产出要能和
    # 冻结的原臂逐字节对照，提示词一动，那条对照就不再是同一个实验。
    "【本臂附加准则 · PM 注入 ": "philosophy.py：模型 prompt，非界面文案",
    "注入目标：筛选B 的 ": "philosophy.py：模型 prompt，非界面文案",
}


def _strip_comments(html: str) -> str:
    """去掉 /* … */ 与整行 // 注释。行内 // 不动（URL 里就有 //）。"""
    html = re.sub(r"/\*.*?\*/", " ", html, flags=re.S)
    return "\n".join(l for l in html.split("\n") if not l.lstrip().startswith("//"))


_CJK = re.compile(r"[\u4e00-\u9fff]")
_LITERAL = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"")


def _html_display_strings(html: str) -> list[str]:
    """dash.html 里**含中文的**字符串字面量 —— 也就是读者会读到的那些。

    只看字面量而不是整份正文，是为了能把 `live` / `as_of` 这类裸英文也纳入
    禁用：整份正文里 `d.as_of` 和 `'/api/journal'` 到处都是，一律拦就只剩噪音。
    含中文 = 是写给人看的句子，这是这份文件里最可靠的一条界线。
    """
    body = _strip_comments(html)
    out = []
    for m in _LITERAL.finditer(body):
        t = m.group(1) if m.group(1) is not None else m.group(2)
        if t and _CJK.search(t):
            out.append(t)
    return out


def _py_display_strings(src: str) -> list[str]:
    """模块里非 docstring 的字符串字面量。

    f-string 整条还原成一个 chunk（`{...}` 处填 `{}`）。分开看会把
    `f"…筛选B 的 {arm} 臂。"` 拆成两半，例外清单就永远匹配不上整句。
    """
    tree = ast.parse(src)
    docstrings, consumed = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    joined = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value); consumed.add(id(v))
                else:
                    parts.append("{}")
            joined.append("".join(parts))
    out = list(joined)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and id(node) not in consumed):
            out.append(node.value)
    return out


def _allowed(text: str) -> bool:
    return any(a in text for a in ALLOW)


#: 「实盘」= 真钱。这套系统的实盘通道是**故意没有接**的（execution.py 里那段
#: 拒绝下单的话写得很清楚），所以这个词在界面上只允许出现在否定里：
#: 「纸面成交，非实盘」「不是实盘」。
#:
#: 2026-09-05 抓到面板同时在用它的第二个意思——拿来指「当期向前跑」，于是
#: 出现了「**实盘**模拟组合」这种自相矛盾的抬头，以及「①**实盘**上，哪个选取
#: 策略更好？」。同一页另一处还写着「纸面成交，非实盘」。老板看到「实盘」
#: 会以为真投了钱，而这一页正是拿去见老板的。
#: 「当期向前跑」这个意思，面板自己有词：**当期实跑**。
#: 「否定」也包括「说明这条通道怎样才会被接上」——execution.py 那段写着
#: 「要真正接实盘，必须由人另做一次显式授权」，那是在讲它现在没接，不是在
#: 声称接了。
_NEGATED = ("非实盘", "不是实盘", "不能当作", "禁用", "未接", "没有接",
            "故意没有接", "拒绝下单", "无法下单", "未发送", "没有发出",
            "要真正接", "显式授权", "没有任何开关", "本该发出")


def _live_money_claims(text: str) -> list[str]:
    """没有被否定的「实盘」。

    否定词可能在前也可能在后——`execution.py` 那句拒绝下单的话是
    「实盘通道是**故意没有接**的」，只看前面会把它误报。所以两边都看。
    """
    for m in re.finditer("实盘", text):
        around = text[max(0, m.start() - 14):m.start() + 20]
        if not any(w in around for w in _NEGATED):
            return ["实盘"]
    return []


def _re_leaks(text: str) -> list[str]:
    """被别的字劈开的旧比喻。"""
    return [pat for pat in BANNED_RE if re.search(pat, text)]


def _en_leaks(text: str) -> list[str]:
    """紧挨着中文出现的英文标识符。"""
    return [w for w in BANNED_EN
            if re.search(rf"[\u4e00-\u9fff][ 　]?\b{w}\b"
                         rf"|\b{w}\b[ 　]?[\u4e00-\u9fff]", text)]


class GlossaryGate(unittest.TestCase):

    def test_no_banned_word_reaches_the_reader(self):
        bad = []
        for p in _surfaces():
            rel = str(p.relative_to(ROOT))
            self.assertTrue(p.exists(), f"{rel} 不在了")
            src = p.read_text(encoding="utf-8")
            chunks = (_html_display_strings(src) if p.suffix == ".html"
                      else [c for c in _py_display_strings(src) if _CJK.search(c)])
            for chunk in chunks:
                if _allowed(chunk):
                    continue
                for word, instead in (list(BANNED.items())
                                      + [(w, BANNED_EN[w]) for w in _en_leaks(chunk)]
                                      + [(p, BANNED_RE[p]) for p in _re_leaks(chunk)]
                                      + [(w, "只能出现在否定里（「纸面成交，非实盘」）"
                                             "——这套系统的实盘通道是故意没有接的；"
                                             "要说「当期向前跑」，写「当期实跑」")
                                         for w in _live_money_claims(chunk)]):
                    if word in chunk or word in BANNED_RE:
                        bad.append(f"  {rel}\n"
                                   f"    「{word}」→ {instead}\n"
                                   f"    出处：…{chunk.strip()[:110]}…")
        self.assertFalse(bad, "\n\n读者看得到的地方还留着实现细节的比喻——"
                              "换法见 docs/词表.md：\n\n" + "\n".join(bad[:12])
                              + (f"\n\n（共 {len(bad)} 处）" if len(bad) > 12 else ""))

    def test_every_registered_selector_has_a_chinese_name_on_the_panel(self):
        """后端注册一条策略、面板不知道它，读者就会在业绩表里看见 `mom_21`。

        `SEL_META` 是面板给选取策略取中文名的**唯一**对照表，而它是一份手工
        维护的后端注册表镜像。2026-09-05 就漏了一次：`select_momentum.py` 注册
        `mom_21` 时后端 `label` 写的是「一月动量（对照）」，面板照样把内部键名
        印进夏普表——同一张表其余十行都是中文，只有它露着英文。

        和 `SURFACES` 那份手写清单同一类毛病：镜像不会自己跟上。所以让注册表
        来对账，加一条策略却忘了取名字，这里红。
        """
        import sys
        sys.path.insert(0, str(ROOT))
        from ideagen import strategy
        import ideagen.strategies                       # noqa: F401  触发注册
        registered = {m["name"] for m in strategy.available("idea_selector")}
        html = (ROOT / "web" / "dash.html").read_text(encoding="utf-8")
        block = re.search(r"var SEL_META=\[.*?\n\];", html, re.S)
        self.assertIsNotNone(block, "dash.html 里找不到 SEL_META")
        named = set(re.findall(r"\['([a-z0-9_]+)'", block.group()))
        missing = sorted(registered - named)
        self.assertFalse(
            missing,
            "这些选取策略在后端注册了，面板却没有中文名，会直接印内部键名：\n  "
            + "\n  ".join(missing)
            + "\n往 web/dash.html 的 SEL_META 里加一行："
              "[键名, 中文名, 主策略/常驻探索/对照, 短说明, 长说明]")

    def test_every_exemption_names_a_file_that_exists(self):
        """豁免清单会烂掉——文件改名之后那条豁免就在悄悄免掉一个不存在的东西。"""
        for rel, why in EXEMPT.items():
            self.assertTrue((ROOT / rel).exists(), f"豁免指向不存在的 {rel}")
            self.assertTrue(len(why) > 10, f"{rel} 的豁免理由太短")

    def test_the_gate_can_actually_see_a_violation(self):
        """闸门自己得先是活的：一段带旧词的正文必须被抓到。"""
        sample = "<div>已入账 10 个账本</div>"
        hits = [w for w in BANNED if w in _strip_comments(sample)]
        self.assertEqual(sorted(hits), ["入账", "账本"])

    def test_comments_and_docstrings_are_out_of_scope(self):
        """注释里的「臂」不算违规——标识符还叫 arm，注释得对得上代码。"""
        self.assertNotIn("臂", _strip_comments("/* 第五条臂 */<div>组合</div>"))
        self.assertNotIn("臂", _strip_comments("  // 九条臂\n<div>组合</div>"))
        self.assertEqual(_py_display_strings('"""模块讲的是臂"""\nx = "组合"'),
                         ["组合"])

    def test_code_that_merely_mentions_a_field_name_is_not_prose(self):
        """`d.as_of` 和 `'/api/journal'` 是代码，不是说给人听的话。"""
        got = _html_display_strings(
            "var x=d.as_of; f('/api/journal'); h('第 '+n+' 期 as_of 之前');")
        self.assertEqual(got, ["第 ", " 期 as_of 之前"])
        self.assertTrue(any("as_of" in g for g in got),
                        "中文句子里的 as_of 必须被看见")

    def test_the_word_for_real_money_only_appears_when_denied(self):
        """实盘通道故意没接，所以这个词只允许出现在否定里。"""
        self.assertEqual(_live_money_claims("纸面成交，非实盘"), [])
        self.assertEqual(_live_money_claims("1 亿本金，不是实盘。"), [])
        self.assertEqual(_live_money_claims("实盘通道是故意没有接的"), [])
        self.assertEqual(_live_money_claims("拒绝下单：实盘通道是故意没有接的。"), [],
                         "否定词在后面也算否定")
        self.assertEqual(_live_money_claims("实盘委托已记录、未发送"), [])
        self.assertEqual(
            _live_money_claims("要真正接实盘，必须由人另做一次显式授权"), [],
            "讲「怎样才会被接上」也是在讲它现在没接")
        self.assertEqual(
            _live_money_claims("会把每一笔本该发出的实盘委托记录下来"), [],
            "「本该发出的」本身就是在说它没被发出")
        self.assertEqual(_live_money_claims("第一套证据 · 实盘模拟组合"), ["实盘"])
        self.assertEqual(_live_money_claims("实盘上，哪个选取策略更好？"), ["实盘"])

    def test_a_metaphor_split_by_other_words_is_still_the_metaphor(self):
        """「各记一本模拟账」躲过了逐字匹配的「本账」，正则规则要抓住它。"""
        self.assertTrue(_re_leaks("10 个选取策略各记一本模拟账"))
        self.assertTrue(_re_leaks("八本账"))
        self.assertFalse(_re_leaks("各管一个模拟组合"))
        self.assertTrue(_re_leaks("每本账都写着同一个日期"))
        self.assertFalse(_re_leaks("这一本书讲的是账龄"), "别把不相干的句子也拦了")
        self.assertFalse(_re_leaks("三本书里都提到过账期"), "中间隔太远就不是这个比喻")

    def test_a_replacement_that_collided_with_an_existing_word_is_caught(self):
        """「研报语料」→「研报研报」：换词换出来的叠词，逐字规则看不见。"""
        self.assertTrue(_re_leaks("研报研报 · Wisburg MCP"))
        self.assertTrue(_re_leaks("10 个组合组合合计"))
        self.assertFalse(_re_leaks("研报 · Wisburg MCP"))
        self.assertFalse(_re_leaks("每周读入的宏观研报"))

    def test_an_english_token_only_counts_when_it_touches_chinese(self):
        self.assertEqual(_en_leaks('结论以 live 期为准'), ["live"])
        self.assertEqual(_en_leaks('第 3 期 as_of 之前'), ["as_of"])
        self.assertEqual(_en_leaks('<span class="pchip live">在场</span>'), [])
        self.assertEqual(_en_leaks('心跳 alive 了'), [])
        self.assertEqual(_en_leaks('SELECT as_of FROM orch_runs 的结果'), [])


if __name__ == "__main__":
    unittest.main()
