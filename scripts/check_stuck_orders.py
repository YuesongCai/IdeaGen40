#!/usr/bin/env python3
"""建仓这一段安静丢掉的东西：卡住的批次，和挂着不成交的单。

两件事同源——都是「价格还没到」，都不报错，都只在最终数字里少一块。

## 一、整批卡在 draft（更严重）

2026-08-12 有 5 个批次、08-19 有 4 个批次停在 `status='draft'` 从未成交，
合计 237 条想法。**不是丢了，是校验没过**：`ref_price_present` /
`ref_price_dated` 两项 error——**一个批次里只要有 2 条想法拿不到参考价，
整批 74 条都建不了仓**。

后果直接打在对照实验的地基上：`buy_all`（全量基准，所有「相对基准差距」的
参照）因此缺了 08-12 和 08-19 两期，十个组合里只有两个覆盖全部六期。面板
三处写着「各策略同池同期，净值差异仅来自选取策略」——同池成立，同期不成立。

**值得商榷的是这道校验的粒度**：建仓那一步本来就会跳过拿不到价格的标的
（「该选取策略选中的标的当日都没有价格」是它自己的 skip 理由），而批次校验
是全有全无的。两条坏想法拖垮七十二条好想法，两者对同一件事的严格程度不一致。

## 二、挂着没成交的单，是在等一根还没到的 K 线

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
退出码 0 = 两项都干净，1 = 有卡住的批次或被行情卡住的单。
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter


def stuck_batches(con) -> int:
    """整批没成交的，以及卡住它的那几项校验。"""
    import json
    rows = [dict(r) for r in con.execute(
        "SELECT batch_id, as_of, n_ideas, status, validation FROM batches "
        "WHERE status != 'traded' ORDER BY as_of, batch_id")]
    if not rows:
        print("没有卡住的批次。")
        return 0
    ideas = sum(r["n_ideas"] or 0 for r in rows)
    print(f"卡住的批次 {len(rows)} 个，合计 {ideas} 条想法从未建仓：")
    for r in rows:
        v = {}
        try:
            v = json.loads(r["validation"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        bad = [c.get("check") for c in v.get("checks", []) if not c.get("ok")
               and c.get("severity") == "error"]
        print(f"  {r['batch_id']:38s} {r['as_of']}  {r['n_ideas']:>4} 条  "
              f"[{r['status']}]  卡在：{'、'.join(bad) or '（无 error 级检查）'}")
    print("  校验是全有全无的：一个批次里只要有几条想法拿不到参考价，整批都不建仓，")
    print("  而建仓那一步本来就会跳过拿不到价格的标的——两者的严格程度不一致。")
    return 1


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/ideagen.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("=== 一、整批卡住没建仓 ===")
    rc_b = stuck_batches(con)
    print("\n=== 二、挂着没成交的单 ===")
    last = {r["code"]: r["d"] for r in con.execute(
        "SELECT code, MAX(d) d FROM prices GROUP BY code")}
    if not last:
        print("库里没有行情。")
        return rc_b
    newest = max(last.values())
    stale = {c: d for c, d in last.items() if d < newest}

    rows = [dict(r) for r in con.execute(
        "SELECT code, as_of, placed_d, expire_d, COUNT(*) n FROM orders "
        "WHERE status='pending' GROUP BY code, as_of, placed_d, expire_d")]
    if not rows:
        print("没有挂着的单。")
        return rc_b

    blocked = [r for r in rows if last.get(r["code"], "") < r["placed_d"]]
    total = sum(r["n"] for r in rows)
    n_blocked = sum(r["n"] for r in blocked)

    print(f"全站最新行情日 {newest}；有价格的标的 {len(last)} 只，"
          f"其中 {len(stale)} 只落后。")
    print(f"挂着的单 {total} 张，其中 {n_blocked} 张在等一根还没到的 K 线。")
    if not blocked:
        print("没有被行情卡住的单。")
        return rc_b

    print("\n被卡住的标的（行情最新日 < 下单日）：")
    for code in sorted({r["code"] for r in blocked}):
        n = sum(r["n"] for r in blocked if r["code"] == code)
        print(f"  {code:12s} 行情停在 {last.get(code, '—')}  ×{n} 张")

    exp = Counter(r["expire_d"] for r in blocked for _ in range(r["n"]))
    print("\n失效日：", dict(sorted(exp.items())))
    print("行情不补上，这些单会到期作废，对应的想法**永远不会进入任何业绩曲线**。")
    return 1


sys.exit(main())
