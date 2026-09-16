"""市场阶段：给每一期打一个阶段标签，看各选取策略在不同阶段的表现。

yifu 2026-09-11「按市场阶段 turn on/off」；Allspring 开场讲四个风格因子，动量 7 月
刚急跌。我们只做动量右侧，所以首要的阶段开关不是宏观，而是**动量因子自身的状态**：
MTUM 相对 SPY 的比值在快（21 个交易日）、慢（63 个交易日）两个窗口上的变化。

  顺风  慢窗口 > 0 且快窗口 > 0   动量在赢，而且这个月还在赢
  转弱  慢窗口 > 0 且快窗口 ≤ 0   这一季在赢，这个月掉头——2026-07 的急跌就是这一格
  逆风  慢窗口 ≤ 0 且快窗口 ≤ 0
  转强  慢窗口 ≤ 0 且快窗口 > 0

辅以两个描述，不参与分组：SPY 相对 200 日均线（上行 / 下行），波动水平（VIX 可得时
用 VIX，否则 SPY 21 日已实现波动，两套阈值见 config）。

纪律：
* **标签只用决策日当天及以前的行情**。期 = 该期日期，读的是那天收盘为止的数据；
  用持有期内的行情给那一期贴标签，等于先看答案再分组。
* **只给框架不下开关。** 分阶段表现表每格带期数，少于 `config.REGIME_MIN_PERIODS`
  一律「样本不足」。赛马里跑赢的策略可能只是恰好赶上了它顺风的那一段——拿几期的
  冠军去配下一段，像卖过去三年最牛的基金。这个模块不会、也不该自动停用任何策略；
  停用是人做的决定，入口在 `strategy_inventory`。
* VIX 行情存在 `prices` 的 `IDX.VIX` 行（不用 `US.` 前缀：它不是可交易标的，不能被
  当成候选的行情）。`events` 里的 `fmpvol:^VIX:*` 读数在 2026-07-29…09-07 八个日期
  全是同一个值 14.53，不能当历史用。
"""

from __future__ import annotations

import math
import statistics as st
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from . import config, db

MOMENTUM_ZH = {"tailwind": "动量顺风", "fading": "动量转弱",
               "headwind": "动量逆风", "recovering": "动量转强"}
MOMENTUM_ORDER = ("tailwind", "fading", "headwind", "recovering")

_CACHE: dict[str, Any] = {}


# ------------------------------------------------------------------ tape
class Tape:
    """SPY, MTUM and VIX closes, loaded once."""

    def __init__(self, con):
        def load(code: str) -> tuple[list[str], list[float]]:
            rows = db.q(con, "SELECT d, close FROM prices WHERE code=? AND close IS NOT NULL "
                             "ORDER BY d", (code,))
            return [str(r["d"]) for r in rows], [float(r["close"]) for r in rows]

        self.spy_d, self.spy_v = load(config.BENCHMARKS["SPY"])
        mom_d, mom_v = load(config.REGIME_MOM_CODE)
        self.vix_d, self.vix_v = load(config.REGIME_VIX_CODE)
        spy = dict(zip(self.spy_d, self.spy_v))
        # The ratio lives on dates both series have; a missing bar on either
        # side is a missing ratio, never an interpolated one.
        self.r_d = [d for d in mom_d if d in spy]
        mom = dict(zip(mom_d, mom_v))
        self.r_v = [mom[d] / spy[d] for d in self.r_d if spy[d]]

    def label(self, d: str) -> dict[str, Any]:
        fast, slow = config.REGIME_MOM_FAST, config.REGIME_MOM_SLOW
        i = bisect_right(self.r_d, d) - 1
        out: dict[str, Any] = {"d": d}
        if i < slow:
            out.update(momentum=None, momentum_zh="缺数据",
                       why=f"{config.REGIME_MOM_CODE}/SPY 在 {d} 之前不足 {slow + 1} 个共同交易日")
            return out
        rf = (self.r_v[i] / self.r_v[i - fast] - 1.0) * 100.0
        rs = (self.r_v[i] / self.r_v[i - slow] - 1.0) * 100.0
        if rs > 0:
            mom = "tailwind" if rf > 0 else "fading"
        else:
            mom = "recovering" if rf > 0 else "headwind"
        out.update(px_d=self.r_d[i], momentum=mom, momentum_zh=MOMENTUM_ZH[mom],
                   rel_fast_pct=round(rf, 3), rel_slow_pct=round(rs, 3))

        j = bisect_right(self.spy_d, d) - 1
        ma_n = config.REGIME_TREND_MA
        if j + 1 >= ma_n:
            ma = sum(self.spy_v[j + 1 - ma_n:j + 1]) / ma_n
            gap = (self.spy_v[j] / ma - 1.0) * 100.0
            out.update(trend="up" if gap >= 0 else "down",
                       trend_zh="大盘上行" if gap >= 0 else "大盘下行",
                       spy_vs_ma_pct=round(gap, 3))
        else:
            out.update(trend=None, trend_zh="大盘趋势缺数据")

        k = bisect_right(self.vix_d, d) - 1
        # A VIX bar more than a week stale is not today's volatility.
        if k >= 0 and (date.fromisoformat(d) - date.fromisoformat(self.vix_d[k])).days <= 7:
            lvl, bands, src = self.vix_v[k], config.REGIME_VIX_BANDS, "VIX"
        elif j >= 21:
            rets = [self.spy_v[x] / self.spy_v[x - 1] - 1.0 for x in range(j - 20, j + 1)]
            lvl = st.stdev(rets) * math.sqrt(252) * 100.0
            bands, src = config.REGIME_RVOL_BANDS, "SPY 21 日已实现波动"
        else:
            lvl, bands, src = None, None, None
        if lvl is not None:
            vol = "low" if lvl < bands[0] else ("high" if lvl > bands[1] else "mid")
            out.update(vol=vol, vol_zh={"low": "低波动", "mid": "中等波动", "high": "高波动"}[vol],
                       vol_level=round(lvl, 2), vol_source=src)
        else:
            out.update(vol=None, vol_zh="波动缺数据", vol_source=None)
        out["label"] = " · ".join(x for x in (out["momentum_zh"], out.get("trend_zh"),
                                               out.get("vol_zh")) if x)
        return out

    def history(self, every: int = 5) -> list[dict[str, Any]]:
        """A thinned series for the card: ratio rebased to 100, with the label."""
        slow = config.REGIME_MOM_SLOW
        if len(self.r_d) <= slow:
            return []
        base = self.r_v[slow]
        idx = list(range(slow, len(self.r_d), every))
        if idx[-1] != len(self.r_d) - 1:
            idx.append(len(self.r_d) - 1)
        out = []
        for i in idx:
            lab = self.label(self.r_d[i])
            out.append({"d": self.r_d[i], "ratio": round(self.r_v[i] / base * 100.0, 3),
                        "momentum": lab.get("momentum"),
                        "rel_fast_pct": lab.get("rel_fast_pct"),
                        "rel_slow_pct": lab.get("rel_slow_pct")})
        return out


# ------------------------------------------------------------------ periods
def paper_periods(con) -> list[str]:
    """Every period a selector book acted in, plus every successful weekly run."""
    rows = db.q(con, "SELECT DISTINCT as_of FROM positions WHERE book_id LIKE ? "
                     "AND as_of IS NOT NULL", (config.SELECTOR_PREFIX + "%",))
    out = {str(r["as_of"]) for r in rows}
    try:
        out |= {str(r["as_of"]) for r in db.q(
            con, "SELECT DISTINCT as_of FROM orch_runs WHERE kind='weekly' AND ok=1")}
    except Exception:  # noqa: BLE001 — a fresh test database has no orch_runs
        pass
    return sorted(out)


def strategy_by_regime(con, labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per selector book × momentum state: period-level held return and excess over SPY.

    A period's number is the cost-weighted return of the positions opened for it
    (same windows as the three-layer attribution); its excess is over SPY on the
    same windows. The stored account is used (`subset="all"`): a regime table
    split three ways by run class would put one or two periods in every cell.
    """
    from . import attribution_layers as al
    books = [r["book_id"] for r in db.q(
        con, "SELECT book_id FROM books WHERE book_id LIKE ? ORDER BY book_id",
        (config.SELECTOR_PREFIX + "%",))]
    books = [b for b in books if not config.is_backtest_book(b)]
    spy = config.BENCHMARKS["SPY"]
    rows_by_book: dict[str, list[dict[str, Any]]] = {}
    for b in books:
        pos = [dict(r) for r in db.q(con, "SELECT * FROM positions WHERE book_id=?", (b,))]
        last: dict[str, tuple[str, float]] = {}
        for m in db.q(con, "SELECT pos_id, d, upnl FROM mtm WHERE book_id=? ORDER BY d", (b,)):
            last[m["pos_id"]] = (m["d"], float(m["upnl"] or 0))
        rows_by_book[b] = al.position_rows(pos, last)
    closes = al.Closes(con, [spy])
    table: dict[str, Any] = {}
    for b, rows in rows_by_book.items():
        key = b[len(config.SELECTOR_PREFIX):]
        per_period: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            r["spy"] = closes.window_pct(spy, r["start"], r["end"])
            if r["spy"] is not None and r.get("as_of"):
                per_period[str(r["as_of"])].append(r)
        cells: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for a, rs in per_period.items():
            w = sum(x["cost"] for x in rs)
            if w <= 0:
                continue
            held = sum(x["cost"] * x["ret"] for x in rs) / w
            ex = held - sum(x["cost"] * x["spy"] for x in rs) / w
            state = (labels.get(a) or {}).get("momentum") or "unknown"
            cells[state].append((held, ex))
            cells["all"].append((held, ex))
        out = {}
        for state, xs in cells.items():
            n = len(xs)
            out[state] = {
                "n_periods": n,
                "mean_held_pct": round(sum(h for h, _ in xs) / n, 3),
                "mean_excess_spy_pp": round(sum(e for _, e in xs) / n, 3),
                "hit_rate": round(sum(1 for _, e in xs if e > 0) / n, 3),
                "sample": "ok" if n >= config.REGIME_MIN_PERIODS else "样本不足"}
        table[key] = out
    return table


def compute(con) -> dict[str, Any]:
    tape = Tape(con)
    periods = paper_periods(con)
    labels = {a: tape.label(a) for a in periods}
    today = tape.r_d[-1] if tape.r_d else None
    current = tape.label(today) if today else {"momentum": None, "momentum_zh": "缺数据",
                                               "why": f"库里没有 {config.REGIME_MOM_CODE} 行情"}
    counts = defaultdict(int)
    for lab in labels.values():
        counts[lab.get("momentum") or "unknown"] += 1
    return {
        "available": bool(tape.r_d),
        "momentum_code": config.REGIME_MOM_CODE,
        "windows": {"fast": config.REGIME_MOM_FAST, "slow": config.REGIME_MOM_SLOW,
                    "trend_ma": config.REGIME_TREND_MA},
        "vix_available": bool(tape.vix_d),
        "current": current,
        "periods": [labels[a] for a in periods],
        "period_counts": dict(counts),
        "history": tape.history(),
        "by_strategy": strategy_by_regime(con, labels),
        "min_periods": config.REGIME_MIN_PERIODS,
        "states": [{"key": k, "zh": MOMENTUM_ZH[k]} for k in MOMENTUM_ORDER],
        "rule": (f"动量状态 = {config.REGIME_MOM_CODE}/SPY 比值的 {config.REGIME_MOM_FAST} 日与 "
                 f"{config.REGIME_MOM_SLOW} 日变化：两者皆正为顺风、慢正快负为转弱、两者皆负为"
                 f"逆风、慢负快正为转强。每期只用该期日期当天及以前的收盘。大盘趋势 = SPY 相对 "
                 f"{config.REGIME_TREND_MA} 日均线；波动 = VIX（缺则 SPY 21 日已实现波动）。"),
        "framing": ("只给框架，不下自动开关。各选取策略的赛马名次有阶段性：动量顺风时跑在前面的"
                    "策略，换到转弱或逆风的一段可能垫底。用几期的名次去决定下一段用谁，像卖"
                    "过去三年最牛的基金。停用一个策略是人的决定，记在方法页的「策略在用与入库」。"),
    }


def state_block(con) -> dict[str, Any]:
    """Cached on what the numbers depend on; the state document is polled every minute."""
    try:
        fp = db.q1(con, "SELECT (SELECT MAX(d) FROM prices WHERE code=?) a, "
                        "(SELECT COUNT(*) FROM mtm) b, (SELECT COUNT(*) FROM positions) c, "
                        "(SELECT MAX(d) FROM prices WHERE code=?) v",
                   (config.REGIME_MOM_CODE, config.REGIME_VIX_CODE))
        key = f"{fp['a']}|{fp['b']}|{fp['c']}|{fp['v']}"
    except Exception:  # noqa: BLE001
        key = None
    if key and _CACHE.get("key") == key:
        return _CACHE["val"]
    val = compute(con)
    if key:
        _CACHE.update(key=key, val=val)
    return val


# ------------------------------------------------------------------ VIX
def refresh_vix(con, *, today: date | None = None, lookback_days: int = 800,
                fetch=None) -> dict[str, Any]:
    """Gap-fill `IDX.VIX` closes from FMP. Writes only the connection it is given.

    `fetch(start, end) -> list[dict]` is injectable so tests never touch the network.
    """
    today = today or config.today_hkt()
    row = db.q1(con, "SELECT MAX(d) mx FROM prices WHERE code=?", (config.REGIME_VIX_CODE,))
    start = (date.fromisoformat(row["mx"]) + timedelta(days=1)) if row and row["mx"] \
        else today - timedelta(days=lookback_days)
    if start > today:
        return {"code": config.REGIME_VIX_CODE, "rows": 0, "note": "已是最新"}
    if fetch is None:
        from .sources import fmp

        def fetch(a: date, b: date) -> list[dict]:
            return fmp._get("historical-price-eod/dividend-adjusted", symbol="^VIX",
                            **{"from": a.isoformat(), "to": b.isoformat()}) or []
    raw = fetch(start, today)
    n = 0
    for r in raw:
        d = str(r.get("date") or "")[:10]
        close = r.get("adjClose", r.get("close"))
        if not d or close is None or d >= today.isoformat():
            # Today's bar may still be moving; the next run picks it up closed.
            continue
        con.execute("INSERT OR REPLACE INTO prices (code, d, open, high, low, close, volume, src) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (config.REGIME_VIX_CODE, d, r.get("adjOpen", close), r.get("adjHigh", close),
                     r.get("adjLow", close), float(close), 0.0, "fmp:idx"))
        n += 1
    try:
        con.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"code": config.REGIME_VIX_CODE, "rows": n, "from": start.isoformat()}


def cmd_regime(args) -> int:
    import json
    con = db.init()
    if getattr(args, "refresh_vix", False):
        try:
            print(json.dumps(refresh_vix(con), ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 — network failure is reported, not fatal
            print(f"VIX 刷新失败：{type(e).__name__}: {e}")
    res = compute(con)
    print(json.dumps({"current": res["current"], "periods": res["periods"],
                      "by_strategy": res["by_strategy"]}, ensure_ascii=False, indent=1))
    return 0
