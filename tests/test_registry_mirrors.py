"""照着注册表手抄的清单，要么从注册表取，要么配一条对账。

2026-09-05 一天里同一个毛病抓到五处，每一处的形状都一样：某个地方手写了一份
策略名单，注册表后来多了一条，而**没有任何东西会红**：

* `SEL_META` 没有 `mom_21` → 业绩表十一行里十行中文，它印内部键名 `mom_21`；
* `SEL_PATH` 没有 `mom_21` / `ev_rank` → 「执行路径 纯代码 9 种」那排数字少数，
  而且 `ev_rank` 是一直就漏着的，也就是那排数字在这之前就已经错了；
* `GEN_PATH` 没有 `lookthrough` → 同上；
* `GEN_ORDER` 没有 `lookthrough` → 它一跑就会被画成「PM 注入 · 由准则页注入的
  一条准则派生」，界面替一个内置方法编了个来历；
* `review._proposal_index` 写死四种 → 只有新方法提过的标的，「问它为什么选它」
  会答「本期生成产物里没有这条的提案记录」，再附一句「可能是上期滚过来的」。

前四处是展示层、第五处在后端，但根子是同一个：**手工镜像不会自己跟上，而它
不跟上的时候是安静的**。所以这里不再一处一处补，而是拦住这个形状本身：源码里
出现「三个以上注册名并列」的字面量，要么去掉（改成从注册表取），要么进 `ALLOW`
并写清它靠哪条对账守着。

它抓不到什么，说在前面：只认**同一段文本里并列三个以上**的写法。写死一个、
两个（`poc_workflow` 里那种）看不见；跨多行拼起来的看不见；把名字拆成变量再
拼的也看不见。它拦的是最常见、也最容易被顺手写下的那一种。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 允许手抄的地方，以及它靠什么守着。加新条目要写清后半句。
ALLOW = {
    "SEL_META": "面板给选取策略取中文名的唯一对照表；"
                "test_glossary 对着注册表核它（缺一条就印内部键名）",
    "SEL_PATH": "选取策略 → 执行路径；test_glossary 对着注册表核它",
    "GEN_PATH": "生成方式 → 执行路径；test_glossary 对着注册表核它",
    "GEN_ORDER": "生成方式的展示顺序与说明，必须手写（中文名和例子推不出来）；"
                 "test_glossary 对着注册表核它，缺一条会被画成「PM 注入」",
}


def _registered() -> set[str]:
    import sys
    sys.path.insert(0, str(ROOT))
    from ideagen import strategy
    import ideagen.strategies                        # noqa: F401  触发注册
    out: set[str] = set()
    for kind in ("idea_generator", "idea_selector"):
        out |= {m["name"] for m in strategy.available(kind)}
    return out


#: 只认**镜像的形状**：带引号（`'ai_native'`）或作为对象键（`ai_native:`）。
#: 属性访问（`arms.buy_all`）和散文里提到的名字不算——面板里就有一行
#: `ba=arms.buy_all||{},rnd=arms.random_pick||{},ev=arms.ev_rank||{}`，
#: 那是有意挑三条来显示，不是抄名单。第一版判据把它误报了，而一个会喊狼的
#: 检查会被训练成忽略，比没有更糟。
_QUOTED = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_@.\-]*)['\"]")
_KEY = re.compile(r"(?:^|[\{,\s])([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _mirror_names(line: str, names: set[str]) -> set[str]:
    got = set(_QUOTED.findall(line)) | set(_KEY.findall(line))
    return {g for g in got if g in names}


def _sources() -> list[Path]:
    out = [ROOT / "web" / "dash.html"]
    for tree in ("ideagen", "scripts"):
        out += [p for p in sorted((ROOT / tree).rglob("*.py"))
                if "__pycache__" not in p.parts]
    return out


class RegistryMirrors(unittest.TestCase):

    def test_no_new_hand_copied_list_of_strategy_names(self):
        names = _registered()
        self.assertGreaterEqual(len(names), 8, "注册表读空了，这条测试就没意义")
        bad = []
        for p in _sources():
            src = p.read_text(encoding="utf-8")
            for line_no, line in enumerate(src.split("\n"), 1):
                stripped = line.lstrip()
                if stripped.startswith(("#", "//", "*", "/*")):
                    continue                       # 散文里并列几个名字不是名单
                hit = _mirror_names(line, names)
                if len(hit) < 3:
                    continue
                # 这一行属于某个被允许的表吗——往上找最近的 var/常量声明
                head = src[:sum(len(x) + 1 for x in src.split("\n")[:line_no])]
                near = re.findall(r"(?:var|const|let)?\s*([A-Z_][A-Z_0-9]{2,})\s*=",
                                  head[-4000:])
                owner = next((n for n in reversed(near) if n in ALLOW), None)
                if owner:
                    continue
                bad.append(f"  {p.relative_to(ROOT)}:{line_no}\n"
                           f"    并列了 {len(hit)} 个注册名：{sorted(hit)}\n"
                           f"    {line.strip()[:90]}")
        self.assertFalse(
            bad,
            "\n\n这些地方像是照着注册表手抄的名单——注册表多一条它们不会红，"
            "而不跟上的时候是安静的：\n\n" + "\n".join(bad[:8])
            + "\n\n改成从 strategy.available(...) 取；确有必要手写的，"
              "加进 tests/test_registry_mirrors.py 的 ALLOW 并写清它靠哪条对账守着。")

    def test_every_allowance_says_what_guards_it(self):
        for name, why in ALLOW.items():
            self.assertIn("核", why, f"{name} 的理由没说清靠什么守着")

    def test_the_gate_sees_a_hand_copied_list(self):
        names = _registered()
        three = sorted(names)[:3]
        for line in ("var NEW_TABLE={" + ",".join(f"{n}:'x'" for n in three) + "};",
                     "for m in (" + ", ".join(f'"{n}"' for n in three) + "):"):
            self.assertGreaterEqual(len(_mirror_names(line, names)), 3,
                                    f"看不见这条手抄名单：{line[:60]}")

    def test_the_gate_ignores_property_access_and_prose(self):
        """会喊狼的检查会被训练成忽略——这两类必须放行。"""
        names = _registered()
        self.assertLess(
            len(_mirror_names(
                "var ba=arms.buy_all||{},rnd=arms.random_pick||{},ev=arms.ev_rank||{};",
                names)), 3, "属性访问不是抄名单")


if __name__ == "__main__":
    unittest.main()
