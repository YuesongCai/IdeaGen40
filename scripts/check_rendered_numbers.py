#!/usr/bin/env python3
"""走查工具：把面板渲染出来，找**互相矛盾的数**。

起因（2026-09-05）：持仓页的徽标写着 `n/10`——分母是组合数，分子是「被几个
组合独立选中」。实测有 22 个标的分子超过 10，最高 `21/10`，因为计数时没有对
组合去重。67 个标的里 64 个偏高，而同一个数还是共识度散点图的横轴和「≥6 加亮
描边」的判据。它在面板上挂了不知道多久，因为**没有任何东西在看数与数之间的关系**。

词表闸门看源码里的词，结构闸门看源码里的拼接顺序——两者都静态。这一类不行：
`21/10` 在源码里是 `n+'/'+nb`，两个变量，静态看不出毛病，只有渲染出来才现形。

所以这是一个**脚本，不是 pytest**。故意的：需要浏览器的检查放进测试套件，
在 CI 里只会被静默跳过，而这个仓的 cloudsync 推代码前跑的就是那一套——
一条永远跳过的测试挡不住任何人，还会让人以为有东西在看。走查时手动跑它。

用法：
    python3 scripts/check_rendered_numbers.py [--url http://localhost:8765/review]

它会展开每个视图的全部折叠、逐个打开阶段抽屉，然后在渲染出来的文本里找：
  * `n/m` 形式里分子大于分母（徽标、期数、样本量都是这个形状）
  * 百分比落在 [-100, 1000] 之外（换算错了或者乘了两次 100）
  * 「x / y 期」里 x 大于 y

只报事实和出处，不猜原因——判断该由人做。
"""
from __future__ import annotations

import argparse
import re
import sys

#: 只认**带单位的比值**和括号里的胜率。第一版用的是「任何 n/m」，跑出来 14 条
#: 全是误报：日期 `09/05`、并列计数「开仓 / 已平 26 / 1」、还有被千分位逗号劈开的
#: `1,492 / 3,628` 被读成 `492 / 3`。一个天天误报的检查会训练人忽略它，比没有更糟。
#: 所以判据反过来写：**白名单**——只看我们确知是「几分之几」的那几种形状。
#: 宁可漏报，不可误报；漏掉的那些下次遇到再往白名单里加。
NUM = r"\d[\d,]*"
FRACTION = re.compile(
    rf"(?<![\d.,/])({NUM})\s*/\s*({NUM})\s*(期|笔|条|个|只|仓|篇)"      # 4/6 期、1,492 / 3,628 笔
    rf"|胜率[^0-9%]{{0,6}}[\d.]+%\s*[（(]\s*({NUM})\s*/\s*({NUM})\s*[)）]"  # 胜率 0%（0/202）
)
PERCENT = re.compile(r"(?<![\d.,])([-+]?\d{1,7}(?:\.\d+)?)\s*%")


def _pair(m: "re.Match") -> tuple[int, int]:
    g = [x for x in m.groups() if x and x[0].isdigit()]
    return int(g[0].replace(",", "")), int(g[1].replace(",", ""))


def _fractions(text: str) -> list[str]:
    out = []
    for m in FRACTION.finditer(text):
        num, den = _pair(m)
        if den == 0 or num <= den:
            continue
        ctx = text[max(0, m.start() - 30):m.end() + 24].replace("\n", " ⏎ ")
        out.append(f"分子大于分母：{m.group(0).strip()}    …{ctx}…")
    return out


#: 反例先行：这些**不该**被拦。判据每收紧一次都要先过这一关。
COUNTEREXAMPLES = [
    "最近心跳 09/05 19:16 HKT",                    # 日期
    "开仓 / 已平 26 / 1",                          # 并列计数，不是比值
    "实际只有 41% 的持仓跑满了（1,492 / 3,628 笔）",  # 千分位，且 num<den
    "顶格胜基准 5 / 6 期",                          # 正常比值
    "胜率 0%（0/202）",                             # 正常胜率
    "净值 100.00 / 期初 100",                       # 无单位，不认
]
#: 正例：这些必须被拦住，否则这个脚本什么都没在看。
EXAMPLES = [
    "被 21 / 10 个组合选中",
    "顶格胜基准 7 / 6 期",
    "胜率 50%（8/4）",
]


def _selftest() -> list[str]:
    bad = []
    for t in COUNTEREXAMPLES:
        if _fractions(t):
            bad.append(f"误伤反例：{t!r} → {_fractions(t)}")
    for t in EXAMPLES:
        if not _fractions(t):
            bad.append(f"漏掉正例：{t!r}")
    return bad


def _percents(text: str) -> list[str]:
    out = []
    for m in PERCENT.finditer(text):
        v = float(m.group(1))
        if -100.0 <= v <= 1000.0:
            continue
        ctx = text[max(0, m.start() - 30):m.end() + 24].replace("\n", " ⏎ ")
        out.append(f"百分比越界：{m.group(0)}    …{ctx}…")
    return out


EXPAND = """(v)=>{document.querySelectorAll('#view-'+v+' .fold-lid,#view-'+v+' .nfold-lid')
  .forEach(b=>{const w=b.closest('.fold,.nfold'); if(w&&w.getAttribute('data-open')!=='1')b.click()})}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8765/review")
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright && playwright install chromium")
        return 2

    broken = _selftest()
    if broken:
        print("这个脚本自己的判据坏了，先修它再谈走查：")
        for b in broken:
            print("  " + b)
        return 2

    findings: list[tuple[str, str]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1560, "height": 1050})
        page.goto(args.url, wait_until="load")
        page.evaluate("()=>refresh()")
        page.wait_for_timeout(4500)

        for view in ["overview", "periods", "holdings", "method", "evidence"]:
            page.evaluate("(v)=>switchView(v)", view)
            page.wait_for_timeout(400)
            for _ in range(3):          # 折叠可以嵌套，展开要跑几轮才到底
                page.evaluate(EXPAND, view)
                page.wait_for_timeout(450)
            text = page.evaluate("(v)=>document.getElementById('view-'+v).innerText", view)
            for hit in _fractions(text) + _percents(text):
                findings.append((view, hit))

        for i, name in enumerate(["研报", "筛选A", "筛选B", "候选池", "筛选C", "建仓"]):
            page.evaluate("(i)=>openStageDrawer(i)", i)
            page.wait_for_timeout(900)
            text = page.evaluate(
                "()=>{const d=document.querySelector('.drawer');return d?d.innerText:''}")
            for hit in _fractions(text) + _percents(text):
                findings.append(("抽屉·" + name, hit))
        browser.close()

    # 闸门要在自己的输出里说清它看不见什么。不然绿了会被读成
    # 「这一类都干净」，而下一个人就不会再去看了。
    blind = ("看不见的：无单位的裸徽标（孤零零一行的 `21/10`）、"
             "跨卡片的口径不一致、以及分母本身就错的情况。")
    if not findings:
        print("五个视图（全部折叠展开）+ 六个阶段抽屉：没有互相矛盾的数。")
        print("  " + blind)
        return 0
    print(f"发现 {len(findings)} 处互相矛盾的数：\n")
    for where, what in findings:
        print(f"  [{where}] {what}")
    print("\n  " + blind)
    return 1


if __name__ == "__main__":
    sys.exit(main())
