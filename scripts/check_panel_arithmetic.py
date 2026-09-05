#!/usr/bin/env python3
"""面板上互相矛盾的数：分子大于分母、比例对不上它自己的分子分母。

`tests/test_stated_counts.py` 查的是另一类——散文里写死的计数和它旁边那张清单
对不上。这里查的是**算术自相矛盾**：两个数都印在屏幕上，读者一眼能看出不对，
但看不出错在哪。2026-09-05 一天里抓到两个，都是这个形状：

* 持仓页的标的徽标 `21/10` —— 分母是组合数，分子不可能更大。真因是合并持仓时
  逐条 push 组合名不去重，四周滚动下同一组合会在好几期买同一只标的，于是
  「被几个组合独立选中」被算成了「它一共有几笔仓」。67 个标的里 64 个偏高，
  而这个数还是共识度散点图的横轴和「≥6 加亮」的判据。
* 头条的「胜率 0%（0/202）」 —— 数字本身没错，但 202 笔全部同日开平，
  在这套成本模型下必亏点差，所以 0% 是算术必然而不是选股结论。

这两个都不是 grep 出来的，是把渲染文本逐字读、看见不可能的数停下来查。
这个脚本把其中**能机械判定的那部分**固定下来：不可能的比值。它抓不到第二类
（数字本身对、但前提没说），那一类只能靠人读——如实说明，免得绿了被读成
「面板上的数都自洽」。

用法： check_panel_arithmetic.py [面板地址]   默认 http://localhost:8765
退出码 0 = 未发现矛盾，1 = 有，2 = 取不到数据。
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"


def fetch(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as r:
        return json.loads(r.read())


def main() -> int:
    try:
        st = fetch("/api/state")
    except Exception as e:  # noqa: BLE001
        print(f"取不到 {BASE}/api/state：{type(e).__name__}: {e}")
        return 2

    books = st.get("books") or []
    bad: list[str] = []
    n_books = len(books)

    # ① 一只标的被几个组合选中，不可能多于组合总数。
    #    这一条**抓不到当初那个 21/10**：那个 bug 在前端，而这里按 book 去重
    #    计数，去重本身就是修复。留着它是防后端将来自己算错，不是防前端。
    holders: dict[str, set] = {}
    rows: dict[tuple, int] = {}
    for b in books:
        for p in b.get("open_positions") or []:
            code = p.get("code") or "?"
            holders.setdefault(code, set()).add(b.get("book_id"))
            rows[(code, b.get("book_id"))] = rows.get((code, b.get("book_id")), 0) + 1
    over = {c: len(s) for c, s in holders.items() if len(s) > n_books}
    for c, n in sorted(over.items(), key=lambda kv: -kv[1]):
        bad.append(f"标的 {c} 被 {n} 个组合持有，而组合总数只有 {n_books}")

    # ①b 真正该报的是**产生那个 bug 的前提**：同一个组合在不同期买了同一只标的，
    #     于是「持仓行数」和「持有它的组合数」不再相等。任何按行数去数组合数的
    #     地方都会偏高，而这正是 21/10 的来路。这是常态不是故障，所以只报告。
    multi = {k: v for k, v in rows.items() if v > 1}
    if multi:
        codes = sorted({c for c, _ in multi})
        worst = max(multi.values())
        note = (f"注意：{len(multi)} 组（标的×组合）持有多于一行，最多 {worst} 行，"
                f"涉及 {len(codes)} 只标的。四周滚动下这是常态——但它意味着"
                f"**持仓行数 ≠ 持有它的组合数**，任何按行数去数组合数的地方都会偏高"
                f"（2026-09-05 的 21/10 就是这么来的）。")
    else:
        note = ""

    # ② 每个组合自报的胜率，要等于它自己的 wins / closed_n。
    for b in books:
        w, n = b.get("wins"), b.get("closed_n")
        r = b.get("win_rate")
        if not n:
            continue
        if w is not None and w > n:
            bad.append(f"{b.get('selector')}：赢 {w} 笔多于已平仓 {n} 笔")
        if r is not None and abs(r - w / n) > 5e-4:
            bad.append(f"{b.get('selector')}：胜率 {r} 与 {w}/{n} 对不上")
        sd = b.get("closed_same_day")
        if sd is not None and sd > n:
            bad.append(f"{b.get('selector')}：同日开平 {sd} 笔多于已平仓 {n} 笔")

    # ③ 回测摘要里那几组「x/y 条组合」，分子同样不得大于分母。
    sep = ((st.get("backtest") or {}).get("summary") or {}).get("separability") or {}
    nm, na = sep.get("n_measurable"), sep.get("n_arms")
    if nm is not None and na is not None and nm > na:
        bad.append(f"可测组合 {nm} 多于组合总数 {na}")
    for key in ("arms_whose_ci_contains_benchmark", "arms_above_benchmark",
                "arms_below_benchmark"):
        v = sep.get(key)
        if isinstance(v, list) and nm is not None and len(v) > nm:
            bad.append(f"{key} 有 {len(v)} 条，多于可测组合 {nm}")

    print(f"面板 {BASE} · {n_books} 个组合 · {len(holders)} 个标的")
    if note:
        print(f"\n{note}")
    if not bad:
        print("未发现算术矛盾。")
        print("注意：这只覆盖「不可能的比值」。数字本身对、但前提没说的那一类"
              "（例如「胜率 0%」其实全是同日开平）查不了，只能靠人读渲染文本。")
        return 0
    print(f"\n发现 {len(bad)} 处：")
    for line in bad:
        print(f"  {line}")
    return 1


sys.exit(main())
