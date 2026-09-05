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

**根因已经定到行**（2026-09-05）：

  booking.py `_fix_stops`   stop = ref_price × (1 − 2σ)， take = ref_price × (1 + 3σ)
  paper.py   `_fill`        positions.avg_px = fill_px，而 stop_px/take_px 原样抄 idea

两个价不是同一个。**成交价与参考价的差一旦超过带宽，风控线就落在成交价的错误一侧**：
往上超过 σ×3，止盈线在成交价下方，这笔仓注定以亏损「止盈」离场；
往下超过 σ×2，止损线在成交价上方，一盯市就触发。实测：

  US.USFR  参考 50.2000 → 成交 50.4001  差 +0.40%   σ×3 = 0.233%
  US.BKLN  参考 20.2578 → 成交 20.6141  差 +1.76%   σ×3 = 1.460%
  US.FXY   参考 56.1300 → 成交 58.6817  差 +4.55%   σ×3 = 3.703%
  US.USFR  参考 50.4900 → 成交 50.3501  差 -0.28%   σ×2 = 0.170%   ← 往下那一侧

低波动标的必然先中招：σ×3 只有几个基点，而参考价到成交价隔着至少一个交易日。

`HK.03199` 是**另一个**问题，与 σ 无关：参考价 **1.2700**、成交 **120.5452**，
差 95 倍——那笔 idea 的参考价和成交价来自不同口径的价格序列，止损止盈跟着错。

还有一条把上面两类都放大了：**参考价属于 idea 自己那一期，成交发生在撮合当天**。
补跑把五期订单挤在同一天下单，于是参考价可能过期好几周，而 σ 是按一期的尺度
定的。US.EWY 参考 144.21 → 成交 188.91（+31%）就是这么来的——EWY 一个月不会
涨三成。所以下面每一行都印出「想法是哪一期的、哪天才成交」。

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
        "SELECT p.pos_id, p.book_id, p.code, p.avg_px, p.take_px, p.stop_px, "
        "       p.status, p.opened_d, p.as_of, i.ref_price, i.sigma_h "
        "FROM positions p LEFT JOIN ideas i ON i.idea_uid = p.idea_uid "
        "WHERE p.avg_px IS NOT NULL "
        "AND (p.take_px IS NOT NULL OR p.stop_px IS NOT NULL)")]
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

    def why(r) -> str:
        """这一笔为什么会这样——参考价与成交价的差，对上 σ×3 的带宽。"""
        ref, sig = r.get("ref_price"), r.get("sigma_h")
        if not ref or sig is None:
            return "（这笔 idea 没有留下参考价或 σ，无从对照）"
        gap = (r["avg_px"] - ref) / ref * 100
        band = 3 * float(sig)
        # 量级先判：95 倍不是「跑出了带」，是两个价根本不同源。
        if abs(gap) > 500:
            verdict = "口径不同：两个价差了 %.0f 倍，与 σ 无关" % (r["avg_px"] / ref)
        elif gap > band:
            verdict = "成交价已涨过止盈线——这笔仓从挂单起就注定以亏损「止盈」离场"
        elif gap < -2 * float(sig):
            verdict = "成交价已跌破止损线——止损落在进场价上方，一盯市就触发"
        else:
            verdict = "带宽够，方向另有原因"
        # 参考价属于 idea 自己那一期，成交发生在撮合当天。补跑把几期订单挤在
        # 同一天，于是参考价可能过期好几周——而 σ 是按一期的尺度定的。
        late = ""
        if r.get("as_of") and r.get("opened_d") and r["as_of"] != r["opened_d"]:
            late = f" · 想法是 {r['as_of']} 的，{r['opened_d']} 才成交"
        return (f"参考 {ref:.4f} → 成交 {r['avg_px']:.4f} 差 {gap:+.2f}%"
                f" · σ×3 = {band:.3f}% · {verdict}{late}")

    def show(title, rs):
        print(f"\n{title}：{len(rs)} 笔")
        for r in rs[:20]:
            print(f"  {r['code']:10s} 进 {r['avg_px']:10.4f} "
                  f"止盈 {r['take_px'] or 0:10.4f} 止损 {r['stop_px'] or 0:10.4f} "
                  f"[{r['status']}] {r['opened_d']} {r['book_id'][:26]}")
            print(f"    {why(r)}")
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
