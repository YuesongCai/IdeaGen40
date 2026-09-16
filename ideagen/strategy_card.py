"""WS-D: 策略卡 — the fund-card page: what this strategy is, and how it has done.

Allspring's fund card puts the facts and the philosophy on one page so a reader
judges one result, not two documents. The panel has both halves already — the
method page says what it is, the performance page says how it did — but a PM
deciding 「用 / 不用」 has to cross six pages to put them together.

Every number here comes from a table or from the performance page's own
document (`performance.paper_view`), never from a constant typed into this
file. When a number cannot be read, the field is None and `missing` says why,
and the page prints 「缺」 — a card that fills a gap with a plausible figure is
worse than no card.

Scope of the numbers, stated once so the page can repeat it: the simulated
books (`sel-*`), backfilled and live periods together (subset `all`), labelled
with how many of each. Not live money: no live account is connected.

`paper_view` takes ~15 s on the full database, so the document is cached per
process and invalidated when the books move (new equity date or new trade).
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import config, db

_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "doc": None}
_LOCK = threading.Lock()
_TTL_S = 3600.0

#: The two books the card reads. 精选 is what the satellite sleeve would hold;
#: 全量基准 is every candidate equally weighted — the honest comparison for it.
PRIMARY = "shortlist"
BASE = "buy_all"

GOAL = ("用 AI 每周读卖方研报，找出正在形成共识的宏观主题，以动量右侧的方式"
        "为 5–10 个点的卫星仓提出可执行的标的；AI 出宽名单，PM 只做剔除。")

PROCESS = (
    ("读研报", "每周三 07:00 前披露的卖方研报与宏观日历"),
    ("筛选A · 主题", "打分选出本期主题，复现打折、谱系归并"),
    ("筛选B · 想法", "多种生成方式各自写标的与赔率"),
    ("筛选C · 组合", "多个选取策略并行建纸面组合，精选由 PM 把关"),
    ("建仓与风控", "每周投入 25%、四周滚动；σ 止损止盈；闲置资金计息"),
)


def _books_key(con) -> tuple:
    r = db.q1(con, "SELECT MAX(d) d, COUNT(*) n FROM equity WHERE book_id LIKE ?",
              (config.SELECTOR_PREFIX + "%",))
    t = db.q1(con, "SELECT COUNT(*) n FROM trades WHERE book_id LIKE ?",
              (config.SELECTOR_PREFIX + "%",))
    return (r["d"] if r else None, r["n"] if r else 0, t["n"] if t else 0)


def _norm(curve: list[dict[str, Any]], start: str | None) -> list[dict[str, Any]]:
    pts = [p for p in curve or [] if p.get("v") and (start is None or p["d"] >= start)]
    if not pts:
        return []
    base = float(pts[0]["v"])
    return [{"d": p["d"], "v": round(float(p["v"]) / base * 100.0, 3)} for p in pts]


def _hit_rate(con, book_like: str) -> dict[str, Any]:
    r = db.q1(con, "SELECT COUNT(*) n, SUM(CASE WHEN realized>0 THEN 1 ELSE 0 END) w "
                   "FROM positions WHERE book_id LIKE ? AND status='closed'", (book_like,))
    n = int(r["n"] or 0) if r else 0
    w = int(r["w"] or 0) if r else 0
    return {"n_closed": n, "n_win": w, "hit_rate": round(w / n, 4) if n else None}


def _compute(con, p=None) -> dict[str, Any]:
    from . import performance
    missing: list[str] = []
    prefix = config.SELECTOR_PREFIX + "%"
    doc: dict[str, Any] = {"goal": GOAL, "scope": "模拟组合（sel-*），补跑与实时运行合计；未接入实盘资金",
                           "generated_at": config.now_hkt().isoformat()}

    # -- the process, with this period's counts beside each step
    steps = [{"step": s, "what": w, "n": None, "unit": None} for s, w in PROCESS]
    try:
        from . import platform as plat, review
        wk = review.weekly_block(p or plat.load(), con)
        if wk:
            doc["as_of"] = wk.get("as_of")
            hg = next((t for t in wk.get("topics") or [] if t["scorer"] == "hgep"), None)
            steps[0].update(n=wk.get("corpus_total"), unit="篇")
            steps[1].update(n=len((hg or {}).get("chosen") or []) or None, unit="个主题")
            steps[2].update(n=(wk.get("pool") or {}).get("n"), unit="个候选")
            steps[3].update(n=len(wk.get("selectors") or []) or None, unit="个选取策略")
    except Exception as e:  # noqa: BLE001
        missing.append(f"本期流程计数读不到（{type(e).__name__}）")
    doc["process"] = steps

    # -- in play right now
    r = db.q1(con, "SELECT COUNT(DISTINCT book_id) b, COUNT(*) n, COUNT(DISTINCT code) u "
                   "FROM positions WHERE book_id LIKE ? AND status='open'", (prefix,))
    doc["books_in_play"] = int(r["b"] or 0)
    doc["open_positions"] = int(r["n"] or 0)
    doc["open_instruments"] = int(r["u"] or 0)

    # -- weekly turnover: buys over the last 28 days / 4, over current equity
    last = db.q1(con, "SELECT MAX(d) d FROM trades WHERE book_id LIKE ?", (prefix,))
    eq = db.q1(con, "SELECT SUM(e.equity) s FROM equity e WHERE e.book_id LIKE ? AND "
                    "e.d=(SELECT MAX(d) FROM equity WHERE book_id=e.book_id)", (prefix,))
    if last and last["d"] and eq and eq["s"]:
        from datetime import date, timedelta
        start = (date.fromisoformat(last["d"]) - timedelta(days=27)).isoformat()
        buys = db.q1(con, "SELECT SUM(ABS(gross)) g FROM trades WHERE book_id LIKE ? "
                          "AND side='BUY' AND d>=? AND d<=?", (prefix, start, last["d"]))
        g = float((buys["g"] if buys else 0) or 0)
        doc["weekly_turnover"] = {"pct": round(g / 4.0 / float(eq["s"]) * 100.0, 2),
                                  "window": [start, last["d"]],
                                  "rule": "近 4 周买入成交额 ÷ 4 ÷ 全部组合当前净值"}
    else:
        doc["weekly_turnover"] = None
        missing.append("周换手：没有成交或净值记录")

    # -- theme exposure, top five, by cost of open positions
    rows = db.q(con, "SELECT COALESCE(theme,'（未标主题）') t, SUM(cost) c, COUNT(*) n "
                     "FROM positions WHERE book_id LIKE ? AND status='open' "
                     "GROUP BY 1 ORDER BY c DESC", (prefix,))
    tot = sum(float(x["c"] or 0) for x in rows)
    doc["theme_exposure"] = [{"theme_id": x["t"], "n": int(x["n"]),
                              "share": round(float(x["c"] or 0) / tot, 4) if tot else None}
                             for x in rows[:5]]
    doc["theme_exposure_rule"] = "全部组合未平仓仓位按开仓成本占比"
    if not rows:
        missing.append("主题暴露：当前没有未平仓仓位")

    # -- performance (the performance page's own document)
    try:
        pv = performance.paper_view(con, None, "all")
        by = {s["key"]: s for s in pv.get("strategies") or []}
        rows_s = {x["key"]: x for x in (pv.get("summary") or {}).get("rows") or []}
        prim = by.get(PRIMARY) or {}
        start = prim.get("first_d") if prim.get("available") else None
        spy = (pv.get("benchmarks") or {}).get("spy") or {}
        doc["curve"] = {
            "start": start,
            "series": [
                {"key": PRIMARY, "name": prim.get("name") or "精选",
                 "points": _norm(prim.get("curve") or [], start)},
                {"key": BASE, "name": (by.get(BASE) or {}).get("name") or "全量基准",
                 "points": _norm((by.get(BASE) or {}).get("curve") or [], start)},
                {"key": "spy", "name": spy.get("name") or "SPY",
                 "points": _norm(spy.get("curve") or [], start)},
            ]}
        if not start:
            missing.append(f"净值曲线：{prim.get('reason') or '精选组合没有可用曲线'}")
        stats = ((pv.get("research") or {}).get("stats") or {}).get("arms") or {}

        def _metrics(k: str) -> dict[str, Any]:
            row = rows_s.get(k) or {}
            perf = (stats.get(k) or {}).get("performance") or {}
            return {"name": (by.get(k) or {}).get("name") or k,
                    "cum_ret_pct": row.get("cum_ret_pct"),
                    "excess_spy_pp": row.get("excess_spy_pp"),
                    "max_dd_pct": row.get("max_dd_pct"),
                    "n_days": row.get("n_days"), "n_periods": row.get("n_periods"),
                    "pct_positive_days": perf.get("pct_positive_days")}
        doc["metrics"] = {PRIMARY: {**_metrics(PRIMARY),
                                    **_hit_rate(con, config.SELECTOR_PREFIX + PRIMARY)},
                          BASE: {**_metrics(BASE),
                                 **_hit_rate(con, config.SELECTOR_PREFIX + BASE)}}
        doc["window"] = pv.get("window")
    except Exception as e:  # noqa: BLE001
        doc["curve"], doc["metrics"] = None, None
        missing.append(f"业绩：业绩页文档读不到（{type(e).__name__}: {e}）"[:200])

    # -- how many periods are backfill vs live
    try:
        cls = performance.period_classes(con)
        doc["periods"] = {"backfill": sum(1 for v in cls.values() if v == "backfill"),
                          "live": sum(1 for v in cls.values() if v == "live"),
                          "first": min(cls) if cls else None, "last": max(cls) if cls else None}
    except Exception as e:  # noqa: BLE001
        doc["periods"] = None
        missing.append(f"期数标签读不到（{type(e).__name__}）")
    doc["missing"] = missing
    return doc


def build(con, p=None, *, fresh: bool = False) -> dict[str, Any]:
    key = _books_key(con)
    with _LOCK:
        c = dict(_CACHE)
    if (not fresh and c["doc"] is not None and c["key"] == key
            and time.time() - c["at"] < _TTL_S):
        return {**c["doc"], "cached": True}
    doc = _compute(con, p)
    with _LOCK:
        _CACHE.update(key=key, at=time.time(), doc=doc)
    return {**doc, "cached": False}
