#!/usr/bin/env python3
"""建仓时挂的止损止盈，有没有挂在进场价的错误一侧。

面板在好几处承诺「等权买入 · 进场即挂 σ×2 止损 / σ×3 止盈 · 自动执行」。
对一笔多头，这句话只有在 `stop_px < avg_px < take_px` 时才成立。2026-09-05
实测 1366 笔里有 18 笔不成立，分两种：

**方向错（16 笔）**——都是极低波动的标的（USFR / BKLN / FXY）。σ×3 在它们身上
只有几个基点，比「算风控价位用的参考价」和「实际成交价 avg_px」之间的差还小，
于是止盈线在建仓那一刻就已经落在进场价下方。这笔仓从挂单起就注定以亏损
「止盈」离场——buy_all 的 4 笔止盈平仓全是亏的，就是这么来的。

**量级错（9 笔，两种成因）**——
`HK.03199` 进场 120.55，止盈 1.46、止损 1.08，是进场价的百分之一量级：止损低
99% 等于**没有止损**，而面板仍然写着它挂着。其余港股（00700 / 02800 / 02840）
都正常，所以不是市场级问题，是这一个标的的价格序列口径。
`US.EWY` 是另一种：进场 188.91、止损 88.30，σ×2 竟有 53%，反推 σ≈26%——一只
国家 ETF 的月波动不到 10%。价位方向是对的，但那么宽的带在 30 天里几乎不可能
被触到，效果同样是没有风控。这一类是波动估计本身错了，不是价格口径。

这个脚本只报告，不改数据。要不要把它变成测试闸门由人定：现在把它接进全量
测试会立刻变红，而红的那一刻会挡住所有人的推送。

用法： check_risk_levels.py [数据库路径]
退出码 0 = 全部正常，1 = 有异常。
"""
from __future__ import annotations

import sqlite3
import sys


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/ideagen.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT pos_id, book_id, code, avg_px, take_px, stop_px, status, opened_d "
        "FROM positions WHERE avg_px IS NOT NULL "
        "AND (take_px IS NOT NULL OR stop_px IS NOT NULL)")]
    total = len(rows)
    wrong_side, wrong_scale = [], []
    for r in rows:
        px, tk, st = r["avg_px"], r["take_px"], r["stop_px"]
        if not px:
            continue
        # 量级：风控价位偏离进场价超过一半，不可能是 σ 的倍数
        if (tk and not 0.5 <= tk / px <= 2.0) or (st and not 0.5 <= st / px <= 2.0):
            wrong_scale.append(r)
        elif (tk and tk <= px) or (st and st >= px):
            wrong_side.append(r)

    def show(title, rs):
        print(f"\n{title}：{len(rs)} 笔")
        for r in rs[:20]:
            print(f"  {r['code']:10s} 进 {r['avg_px']:10.4f} "
                  f"止盈 {r['take_px'] or 0:10.4f} 止损 {r['stop_px'] or 0:10.4f} "
                  f"[{r['status']}] {r['opened_d']} {r['book_id'][:26]}")
        if len(rs) > 20:
            print(f"  …另有 {len(rs) - 20} 笔")

    print(f"检查 {total} 笔有风控价位的持仓（多头应满足 止损 < 进场 < 止盈）")
    show("量级错——风控价位不在进场价的同一个数量级，等于没有风控", wrong_scale)
    show("方向错——止盈在进场价下方或止损在上方，注定以亏损触发", wrong_side)
    bad = len(wrong_scale) + len(wrong_side)
    if not bad:
        print("\n全部正常。")
        return 0
    print(f"\n合计 {bad} / {total} 笔（{bad / total * 100:.2f}%）"
          f"与面板那句「进场即挂 σ×2 止损 / σ×3 止盈」不符。")
    return 1


sys.exit(main())
