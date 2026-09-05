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

#: 读者看得到的串所在的文件。加新的展示面时把它加进来。
SURFACES = [
    "web/dash.html",
    "ideagen/review.py", "ideagen/cli.py", "ideagen/scheduler.py",
    "ideagen/authpages.py", "ideagen/config.py", "ideagen/audit.py",
    "ideagen/ask.py", "ideagen/philosophy.py", "ideagen/backtest.py",
    "scripts/run_real_backtest.py", "scripts/sync_report.py",
    "scripts/sync_to_cloud.py",
]

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


class GlossaryGate(unittest.TestCase):

    def test_no_banned_word_reaches_the_reader(self):
        bad = []
        for rel in SURFACES:
            p = ROOT / rel
            self.assertTrue(p.exists(), f"{rel} 不在了——SURFACES 该更新")
            src = p.read_text(encoding="utf-8")
            if p.suffix == ".html":
                body = _strip_comments(src)
                chunks = [l for l in body.split("\n")]
            else:
                chunks = _py_display_strings(src)
            for chunk in chunks:
                if _allowed(chunk):
                    continue
                for word, instead in BANNED.items():
                    if word in chunk:
                        bad.append(f"  {rel}\n"
                                   f"    「{word}」→ {instead}\n"
                                   f"    出处：…{chunk.strip()[:110]}…")
        self.assertFalse(bad, "\n\n读者看得到的地方还留着实现细节的比喻——"
                              "换法见 docs/词表.md：\n\n" + "\n".join(bad[:12])
                              + (f"\n\n（共 {len(bad)} 处）" if len(bad) > 12 else ""))

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


if __name__ == "__main__":
    unittest.main()
