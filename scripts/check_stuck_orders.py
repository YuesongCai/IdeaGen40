#!/usr/bin/env python3
"""挂着没成交的单，是不是在等一根还没到的 K 线。

2026-09-05 抓到的一次：148 张单挂着不成交，占本期下单量的 13%、07-29 那期的
23%。查下来不是撮合规则的问题，也不是这些标的没有行情——**是它们的行情比别人
少一天**：全站 90 只有价格的标的里，74 只更新到 09-04，16 只停在 09-03；而
订单是 09-04 下的，撮合要求「下单日或之后」的收盘价（不能同根 K 线成交）。
挂着的 17 只标的里 15 只正是那 16 只之一，而本期已成交的 61 只**全部**更新到
09-04。

后果有期限：这批单 09-11 失效。行情不补上，这些想法就永远不会被建仓，也就
永远不会进入任何一条业绩曲线——**它们会安静地从样本里消失**，而面板上只显示
一个「挂单 42」的数字，不说它在等什么。

用法： check_stuck_orders.py [数据库路径]
退出码 0 = 没有被行情卡住的单，1 = 有。
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/ideagen.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    last = {r["code"]: r["d"] for r in con.execute(
        "SELECT code, MAX(d) d FROM prices GROUP BY code")}
    if not last:
        print("库里没有行情。")
        return 0
    newest = max(last.values())
    stale = {c: d for c, d in last.items() if d < newest}

    rows = [dict(r) for r in con.execute(
        "SELECT code, as_of, placed_d, expire_d, COUNT(*) n FROM orders "
        "WHERE status='pending' GROUP BY code, as_of, placed_d, expire_d")]
    if not rows:
        print("没有挂着的单。")
        return 0

    blocked = [r for r in rows if last.get(r["code"], "") < r["placed_d"]]
    total = sum(r["n"] for r in rows)
    n_blocked = sum(r["n"] for r in blocked)

    print(f"全站最新行情日 {newest}；有价格的标的 {len(last)} 只，"
          f"其中 {len(stale)} 只落后。")
    print(f"挂着的单 {total} 张，其中 {n_blocked} 张在等一根还没到的 K 线。")
    if not blocked:
        print("没有被行情卡住的单。")
        return 0

    print("\n被卡住的标的（行情最新日 < 下单日）：")
    for code in sorted({r["code"] for r in blocked}):
        n = sum(r["n"] for r in blocked if r["code"] == code)
        print(f"  {code:12s} 行情停在 {last.get(code, '—')}  ×{n} 张")

    exp = Counter(r["expire_d"] for r in blocked for _ in range(r["n"]))
    print("\n失效日：", dict(sorted(exp.items())))
    print("行情不补上，这些单会到期作废，对应的想法**永远不会进入任何业绩曲线**。")
    return 1


sys.exit(main())
