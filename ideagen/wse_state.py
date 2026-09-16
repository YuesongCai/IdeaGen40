"""WS-E 在面板状态文档里的那一块：市场阶段、策略在用/入库、成本卡、精选组合整体体检。

`review.state` 只调这里一次，各块独立兜底：任何一块算失败都写 `available=False` 和原因，
不拖垮其他块，也不拖垮整张状态文档。

精选组合整体体检（Allspring：「单只不必全满足，组合整体要满足」）。PM 把关卡逐只看
四层风险复核，这里补组合层面的三问，每问给数、给判定、给覆盖了几只：

  动量    精选等权的近 21 个交易日收益 vs SPY 同窗口。我们只做动量右侧，组合整体跑输
          大盘这一个月，就不是在右侧。
  拥挤度  精选等权的已定价 P（近 21 日收益在自身一年分布里的分位）。整体 ≥
          `config.SHORTLIST_CROWDED_P` 算偏拥挤——不是不能买，是要知道买在了高位。
  集中度  WS-B 已算好的穿透行业分布（`exposure.sector_shares`），取最大一个行业与上限比。
"""

from __future__ import annotations

from typing import Any

from . import config, db


def _safe(fn, *a, **kw) -> dict[str, Any]:
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 — one block must not take the page down
        return {"available": False, "why": f"{type(e).__name__}: {e}"[:300]}


def shortlist_health(con, shortlist: dict[str, Any] | None) -> dict[str, Any]:
    if not shortlist or not shortlist.get("items"):
        return {"available": False, "why": "这一期没有精选"}
    from . import universe as uni
    from .sources import futu_px
    try:
        uni.hydrate(con)
    except Exception:  # noqa: BLE001
        pass
    spy = config.BENCHMARKS["SPY"]
    last = db.q1(con, "SELECT MAX(d) d FROM prices WHERE code=?", (spy,))
    upto = last["d"] if last and last["d"] else None
    if not upto:
        return {"available": False, "why": "库里没有 SPY 行情"}
    n = len(shortlist["items"])
    moms, ps, names_missing = [], [], []
    for it in shortlist["items"]:
        inst = uni.resolve(str(it.get("instrument_id") or ""))
        code = getattr(inst, "futu_code", None) if inst else None
        if not code:
            names_missing.append(str(it.get("instrument_id")))
            continue
        r = futu_px.trailing_return(con, code, upto, 21)
        if r is not None:
            moms.append(r * 100.0)
        pd = futu_px.return_percentile_detail(con, code, upto, window=21)
        if pd.get("ok") and pd.get("value") is not None:
            ps.append(float(pd["value"]))
    spy_r = futu_px.trailing_return(con, spy, upto, 21)
    spy_r = spy_r * 100.0 if spy_r is not None else None
    out: dict[str, Any] = {"available": True, "as_of_px": upto, "n": n,
                           "period": shortlist.get("as_of"),
                           "unpriced": names_missing}
    if moms and spy_r is not None:
        m = sum(moms) / len(moms)
        out["momentum"] = {"basket_21d_pct": round(m, 3), "spy_21d_pct": round(spy_r, 3),
                           "gap_pp": round(m - spy_r, 3), "covered": len(moms),
                           "ok": m > spy_r}
    else:
        out["momentum"] = {"covered": len(moms), "ok": None,
                           "why": "精选里没有可算近 21 日收益的标的（基金按净值，不在此列）"}
    if ps:
        p = sum(ps) / len(ps)
        out["crowding"] = {"mean_p": round(p, 1), "covered": len(ps),
                           "threshold": config.SHORTLIST_CROWDED_P,
                           "ok": p < config.SHORTLIST_CROWDED_P}
    else:
        out["crowding"] = {"covered": 0, "ok": None, "why": "没有可算已定价 P 的标的"}
    ex = shortlist.get("exposure") or {}
    shares = ex.get("sector_shares") or {}
    if shares:
        top, share = max(shares.items(), key=lambda kv: kv[1])
        cap = float(ex.get("sector_cap") or config.SHORTLIST_SECTOR_CAP)
        out["concentration"] = {"top_sector": top, "top_share": share, "cap": cap,
                                "covered": ex.get("covered"), "of": ex.get("of"),
                                "ok": share <= cap}
    else:
        out["concentration"] = {"ok": None, "covered": 0, "of": n,
                                "why": "精选里没有缓存了行业分布的 ETF（etf_weightings）"}
    checks = [out[k]["ok"] for k in ("momentum", "crowding", "concentration")]
    fails = sum(1 for c in checks if c is False)
    unknown = sum(1 for c in checks if c is None)
    out["verdict"] = ("整体三项都满足" if fails == 0 and unknown == 0 else
                      f"整体有 {fails} 项不满足" + (f"、{unknown} 项缺数据" if unknown else "")
                      if fails else f"已算的都满足，{unknown} 项缺数据")
    return out


def state_block(con, state: dict[str, Any]) -> dict[str, Any]:
    from . import costs, market_regime, strategy_inventory
    weekly = state.get("weekly") or {}
    return {
        "regime": _safe(market_regime.state_block, con),
        "inventory": _safe(strategy_inventory.state_block),
        "costs": _safe(costs.card, con),
        "shortlist_health": _safe(shortlist_health, con, weekly.get("shortlist")),
    }


def daily_stage(con) -> str:
    """The non-fatal daily step: bill snapshot + VIX gap-fill. Returns a note, never raises."""
    from . import costs, market_regime
    notes = [costs.daily_stage(con)]
    try:
        r = market_regime.refresh_vix(con)
        notes.append(f"VIX +{r.get('rows', 0)} 行")
    except Exception as e:  # noqa: BLE001 — network failure is recorded, not fatal
        notes.append(f"VIX 未刷新：{type(e).__name__}: {e}"[:200])
    return "；".join(notes)
