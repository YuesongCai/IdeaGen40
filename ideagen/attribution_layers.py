"""三层归因与跟踪误差：业绩页「收益归因」底下的瀑布图和汇总表的一列。

yifu / Jon 2026-09-11 要「可追溯归因」；Allspring 讲他们的 alpha 几乎全来自行业内
选股、不做行业押注。我们的对应问题是：一个组合赚的钱，是押对了主题、在主题里挑对了
标的，还是组合构建（选哪几只、各多大）本身的贡献？三层各有自己的对照物：

  主题层     持仓对应主题的**指示标的**，在该仓位的持有窗口内，相对 SPY 的收益。
             这一层是「押主题」本身值多少——主题 ETF 涨了，谁持有这个主题都有份。
  选标的层   持仓自身收益 − 同窗口主题指示标的收益。认出主题之后，挑具体标的多赚/少赚了多少。
  组合构建层 本组合（成本加权）的持仓收益 − 同一期「全量基准」组合（buy_all，拿下
             全部可入池候选、等权）的持仓收益。筛选C 从整池里挑几只、配多大，值多少。

前两层与 SPY 相加恰好等于该组合持仓的成本加权收益（逐仓恒等式，汇总时同一组权重），
所以瀑布图 SPY → 主题层 → 选标的层 → 持仓收益 是闭合的；组合构建层的对照物是另一个
组合，画成瀑布图之外的最后一根柱子，同时给出全量基准自己的前两层，读者可以逐层看
「本组合比整池多在哪一层」。三层**不**强行塞进同一个加总——那样只能让其中一层
变成凑数的残差。

口径：
* 单位是「占该组持仓成本的百分比」。每仓收益 = 已平仓 realized / cost（扣费后），
  未平仓用最新一次盯市的浮动盈亏 / cost；窗口 = 建仓日收盘 → 平仓日（或最新盯市日）收盘。
  SPY 与指示标的取「当日或之前最后一个收盘」，与收盘成交同一个时点。
* 对不上指示标的（仓位没有主题、主题没有指示标的、指示标的缺价）的仓位不进三层，
  计数单列在 `n_unmatched`，不按 0 计。
* 不含现金利息与现金拖累：这是持仓层面的归因，账户层面的差异在汇总表。
* 样本：一行能对上的仓位少于 `config.ATTR_MIN_POSITIONS` 写「样本不足」；累计行
  另要求期数不少于 `config.ATTR_MIN_PERIODS`。数字照给，颜色不给。

跟踪误差 = 组合逐日净值收益与 SPY 同日收益之差的样本标准差 × √252，单位百分比。
只用两边都有的相邻交易日；不足 `config.TE_MIN_DAYS` 个差值不给数。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from typing import Any, Iterable

from . import config, db

POOL_KEY = "buy_all"

RULE = (
    "三层归因（持仓层面，单位：占该组持仓成本的 %）。主题层 = 持仓所属主题的指示标的在"
    "同一持有窗口内相对 SPY 的收益；选标的层 = 持仓收益 − 同窗口指示标的收益；二者与 SPY "
    "相加等于持仓收益（成本加权，逐仓恒等）。组合构建层 = 本组合持仓收益 − 同一期全量基准"
    "（buy_all）持仓收益，对照物是另一个组合，所以单独成柱、不并入前面的加总。窗口 = 建仓日"
    "收盘到平仓日（未平仓取最新盯市日）收盘；对不上指示标的的仓位不计入、单列计数；"
    "不含现金利息。")


def _f(x: float | None, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


# ------------------------------------------------------------------ closes
class Closes:
    """All closes of a set of codes, loaded once, looked up by date.

    The page is rebuilt on demand for up to thirteen books and hundreds of
    positions; one query per (code, date) would be thousands of round trips.
    """

    def __init__(self, con, codes: Iterable[str]):
        self._d: dict[str, list[str]] = {}
        self._v: dict[str, list[float]] = {}
        codes = sorted({c for c in codes if c})
        for i in range(0, len(codes), 400):
            chunk = codes[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in db.q(con, f"SELECT code, d, close FROM prices WHERE code IN ({marks}) "
                               f"AND close IS NOT NULL ORDER BY code, d", tuple(chunk)):
                self._d.setdefault(r["code"], []).append(str(r["d"]))
                self._v.setdefault(r["code"], []).append(float(r["close"]))

    def on_or_before(self, code: str, d: str) -> tuple[str, float] | None:
        ds = self._d.get(code)
        if not ds:
            return None
        i = bisect_right(ds, d) - 1
        if i < 0:
            return None
        return ds[i], self._v[code][i]

    def window_pct(self, code: str | None, start: str, end: str) -> float | None:
        """Close-to-close return, both ends taken on or before their date.

        A start bar that is the same as the end bar (a position opened and
        marked on one day) is a zero-length window, not a missing one.
        """
        if not code:
            return None
        a = self.on_or_before(code, start)
        b = self.on_or_before(code, end)
        if not a or not b or not a[1]:
            return None
        # An end bar older than the start date means the series stopped before
        # the position opened: that is missing data, not a flat window.
        if b[0] < a[0]:
            return None
        return (b[1] / a[1] - 1.0) * 100.0


def theme_indicators() -> dict[str, str]:
    from . import lexicon
    return {t.id: t.price_indicator for t in lexicon.all_themes() if t.price_indicator}


# ------------------------------------------------------------------ positions
def position_rows(positions: list[dict[str, Any]],
                  upnl_last: dict[str, tuple[str, float]]) -> list[dict[str, Any]]:
    """Per position: cost, own return, window, theme — nothing priced yet."""
    out = []
    for p in positions:
        cost = float(p.get("cost") or 0)
        if cost <= 0:
            continue
        if p.get("status") == "closed" and p.get("closed_d"):
            pnl, end = float(p.get("realized") or 0), str(p["closed_d"])
        else:
            last = upnl_last.get(p["pos_id"])
            if not last:
                continue          # never marked: no return to decompose
            end, pnl = str(last[0]), float(last[1])
        out.append({"pos_id": p["pos_id"], "as_of": p.get("as_of"),
                    "code": p["code"], "theme": p.get("theme"),
                    "start": str(p["opened_d"]), "end": end,
                    "cost": cost, "ret": pnl / cost * 100.0})
    return out


def price_rows(rows: list[dict[str, Any]], closes: Closes,
               indicators: dict[str, str]) -> None:
    spy = config.BENCHMARKS["SPY"]
    for r in rows:
        r["indicator"] = indicators.get(str(r.get("theme") or ""))
        r["spy"] = closes.window_pct(spy, r["start"], r["end"])
        r["ind"] = closes.window_pct(r["indicator"], r["start"], r["end"])


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost-weighted layers over matched rows; unmatched rows are only counted."""
    ok = [r for r in rows if r.get("spy") is not None and r.get("ind") is not None]
    w = sum(r["cost"] for r in ok)
    out = {"n": len(ok), "n_unmatched": len(rows) - len(ok), "cost": _f(w, 2),
           "market_pct": None, "theme_pct": None, "select_pct": None, "held_pct": None}
    if w <= 0:
        return out
    m = sum(r["cost"] * r["spy"] for r in ok) / w
    t = sum(r["cost"] * (r["ind"] - r["spy"]) for r in ok) / w
    s = sum(r["cost"] * (r["ret"] - r["ind"]) for r in ok) / w
    out.update(market_pct=_f(m), theme_pct=_f(t), select_pct=_f(s), held_pct=_f(m + t + s))
    return out


def _row(key_rows: list[dict], pool_rows: list[dict], *, as_of: str | None,
         is_pool: bool, n_periods: int) -> dict[str, Any]:
    own = aggregate(key_rows)
    pool = aggregate(pool_rows) if pool_rows else None
    row = {"as_of": as_of, **own, "n_periods": n_periods,
           "pool_market_pct": pool["market_pct"] if pool else None,
           "pool_theme_pct": pool["theme_pct"] if pool else None,
           "pool_select_pct": pool["select_pct"] if pool else None,
           "pool_held_pct": pool["held_pct"] if pool else None,
           "pool_n": pool["n"] if pool else 0,
           "construction_pp": None}
    if is_pool:
        row["construction_note"] = "全量基准本身就是组合构建层的对照物"
    elif pool and own["held_pct"] is not None and pool["held_pct"] is not None:
        row["construction_pp"] = _f(own["held_pct"] - pool["held_pct"])
    elif not pool_rows:
        row["construction_note"] = "这一期没有全量基准的仓位，组合构建层不算"
    why = []
    if own["n"] < config.ATTR_MIN_POSITIONS:
        why.append(f"能对上指示标的的仓位 {own['n']} 个，少于 {config.ATTR_MIN_POSITIONS}")
    if as_of is None and n_periods < config.ATTR_MIN_PERIODS:
        why.append(f"只有 {n_periods} 期，少于 {config.ATTR_MIN_PERIODS}")
    row["sample"] = "样本不足" if why else "ok"
    row["sample_why"] = "；".join(why) or None
    return row


def layers(con, ledgers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Three layers per strategy, per period and cumulative, from one set of ledgers.

    `ledgers` is `performance.book_ledger` output keyed by strategy key, all on
    the same subset — the pool comparison is only meaningful inside one subset.
    """
    indicators = theme_indicators()
    per_key: dict[str, list[dict[str, Any]]] = {}
    codes = {config.BENCHMARKS["SPY"], *indicators.values()}
    for key, led in ledgers.items():
        rows = position_rows(led.get("positions") or [], led.get("upnl_last") or {})
        per_key[key] = rows
    closes = Closes(con, codes)
    for rows in per_key.values():
        price_rows(rows, closes, indicators)

    pool = per_key.get(POOL_KEY, [])
    pool_by_period: dict[str, list[dict]] = defaultdict(list)
    for r in pool:
        pool_by_period[str(r["as_of"])].append(r)

    out: dict[str, Any] = {}
    for key, rows in per_key.items():
        if not rows:
            continue
        by_period: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_period[str(r["as_of"])].append(r)
        periods = sorted(by_period)
        prow = [_row(by_period[a], pool_by_period.get(a, []), as_of=a,
                     is_pool=key == POOL_KEY, n_periods=1) for a in periods]
        # The pool for the cumulative row is the pool over the same periods the
        # book acted in; a book that sat out a period is not charged the pool's
        # return for it.
        pool_same = [r for a in periods for r in pool_by_period.get(a, [])]
        cum = _row(rows, pool_same, as_of=None, is_pool=key == POOL_KEY,
                   n_periods=len(periods))
        out[key] = {"cumulative": cum, "periods": prow}
    return {"rule": RULE, "unit": "pct_of_cost",
            "min_positions": config.ATTR_MIN_POSITIONS,
            "min_periods": config.ATTR_MIN_PERIODS,
            "pool_key": POOL_KEY, "by_strategy": out}


# ------------------------------------------------------------------ tracking error
def tracking_error(curve: list[dict[str, Any]], bench: list[dict[str, Any]]
                   ) -> tuple[float | None, int]:
    """Annualised tracking error in %, and how many daily differences it used."""
    b = {p["d"]: p["v"] for p in bench if p.get("v")}
    pts = [(p["d"], p["v"]) for p in curve if p.get("v") and p["d"] in b]
    diffs = []
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if not v0 or not b[d0]:
            continue
        diffs.append((v1 / v0 - 1.0) - (b[d1] / b[d0] - 1.0))
    n = len(diffs)
    if n < max(config.TE_MIN_DAYS, 2):
        return None, n
    mu = sum(diffs) / n
    var = sum((x - mu) ** 2 for x in diffs) / (n - 1)
    return math.sqrt(var) * math.sqrt(config.TE_ANNUALISE) * 100.0, n


def extend_view(con, doc: dict[str, Any],
                ledgers: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Add the tracking-error column and (paper only) the three layers to a PerfView.

    Called at the end of `performance.paper_view` / `backtest_view`. Failures are
    written into the document, never raised: the page must render without them.
    """
    spy = ((doc.get("benchmarks") or {}).get("spy") or {}).get("curve") or []
    curves = {s["key"]: s.get("curve") or [] for s in doc.get("strategies") or []}
    for row in (doc.get("summary") or {}).get("rows") or []:
        c = curves.get(row.get("key")) or []
        if row.get("status") != "ok" or not c:
            row["tracking_error_pct"], row["te_n_days"] = None, 0
            continue
        te, n = tracking_error(c, spy)
        row["tracking_error_pct"], row["te_n_days"] = _f(te), n
        if te is None:
            row["te_why"] = f"与 SPY 共同的日收益只有 {n} 个，少于 {config.TE_MIN_DAYS}"
    doc.setdefault("disclosures", []).append(
        f"跟踪误差 = 组合与 SPY 逐日收益差的样本标准差 × √{config.TE_ANNUALISE}；"
        f"不足 {config.TE_MIN_DAYS} 个共同交易日不给数。")
    attr = doc.setdefault("attribution", {})
    if ledgers is None:
        attr["layers"] = {"available": False, "why": (
            "三层归因只在模拟运行上算：需要逐仓的建仓日、平仓日与成本，"
            "回测记录里只有指数化净值或另一套持仓表")}
        return doc
    try:
        lay = layers(con, ledgers)
        lay["available"] = bool(lay["by_strategy"])
        if not lay["available"]:
            lay["why"] = "这个子集里没有可归因的仓位"
        attr["layers"] = lay
    except Exception as e:  # noqa: BLE001 — a new block must not take the page down
        attr["layers"] = {"available": False, "why": f"计算失败：{type(e).__name__}: {e}"}
    return doc
