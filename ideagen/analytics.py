"""Attribution and skill measurement.

The book-level curve answers "what would this have done to the account". It is not
the same question as "were the ideas any good", because the methodology's own
position sizes leave most of the book in cash. Both are reported, never conflated:

  book        cash-inclusive equity curve per book, vs benchmark
  idea        equal-weighted return of the ideas themselves, filled or not
  ranking     does the engine's own ordering predict realised return
  calibration were the stated probabilities honest (Brier, hit rates)
  buckets     grade / horizon / theme / crowding / vol-check cuts

The ranking and calibration blocks are the parts v0.3 has no way to produce: it
scores ideas but never records an outcome against the score, so nothing it claims
is falsifiable. Everything below is computed from stored outcomes only.
"""

from __future__ import annotations

import math
import statistics as st
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from . import config, db, ideas as ideas_mod, paper
from .sources import futu_px


# ---------------------------------------------------------------- helpers
def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 4:
        return None
    xs, ys = zip(*pairs)

    def rank(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = rank(xs), rank(ys)
    ma, mb = st.mean(ra), st.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return round(num / den, 4) if den else None


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v*100:+.2f}%"


def benchmark_return(con, code: str, start: str, end: str) -> float | None:
    a = futu_px.last_close_on_or_before(con, code, start)
    b = futu_px.last_close_on_or_before(con, code, end)
    return (b[1] / a[1] - 1) if (a and b and a[1]) else None


# ---------------------------------------------------------------- outcomes
def settle(con, book_id: str = "naive", verbose: bool = True) -> dict:
    """Write one outcome row per idea, whether or not it was ever filled.

    `realized` is the position's own net return when it traded, and the
    counterfactual close-to-close return of the instrument when it did not — so an
    unfilled idea still gets scored, and the cost of missing it is visible.
    """
    # A position whose instrument no longer matches its idea makes every number
    # below meaningless: `entry_px` is read off the position while `exit_px` is
    # marked on the idea's instrument, so a rebound uid prices one asset's entry
    # against another's close. Refuse rather than publish — see
    # `ideas.purge_batch` for how this arose and what it cost.
    bad = ideas_mod.instrument_mismatches(con)
    if bad:
        batches = sorted({b["batch_id"] for b in bad})
        raise RuntimeError(
            f"{len(bad)} positions disagree with their idea's instrument "
            f"(batches: {', '.join(batches)}); e.g. {bad[0]['idea_uid']} holds "
            f"{bad[0]['position_code']} while its idea is {bad[0]['idea_code']}. "
            f"Settling would mark one asset's entry against another's close. "
            f"Fix with:  ideagen rebuild-batch {batches[0]}")

    rows = db.q(con, "SELECT * FROM ideas ORDER BY as_of, local_id")
    bench = config.BENCHMARKS["SPY"]
    out, settled = [], 0
    today = futu_px.complete_through("US")

    for r in rows:
        idea = dict(r)
        for k in ("central_p", "central_r", "conserv_p", "conserv_r"):
            idea[k] = db.jl(idea[k], [])
        hz_end = ideas_mod.horizon_end(date.fromisoformat(idea["as_of"]),
                                       idea["horizon_months"]).isoformat()
        mark_to = min(hz_end, today)

        pos = db.q1(con, "SELECT * FROM positions WHERE book_id=? AND idea_uid=?",
                    (book_id, idea["idea_uid"]))
        filled = bool(pos)
        entry_px = exit_px = realized = None
        reason = None

        if pos:
            p = dict(pos)
            entry_px = p["avg_px"]
            reason = p["exit_reason"] or "open"
            if p["status"] == "closed":
                exit_px = p["close_px"]
                realized = p["realized"] / p["cost"] if p["cost"] else None
            else:
                m = paper.mark_price(con, idea, mark_to)
                if m:
                    exit_px = m["px"]
                    fx = paper._fx(paper._currency(con, idea)) or 1.0
                    val = p["qty"] * m["px"] * fx
                    realized = (val - p["cost"]) / p["cost"] if p["cost"] else None
        elif idea["futu_code"]:
            a = futu_px.last_close_on_or_before(con, idea["futu_code"], idea["as_of"])
            b = futu_px.last_close_on_or_before(con, idea["futu_code"], mark_to)
            if a and b and a[1]:
                entry_px, exit_px = a[1], b[1]
                cost = ideas_mod.round_trip_cost_pct(
                    futu_px.market_of(idea["futu_code"]), "listed") / 100.0
                realized = b[1] / a[1] - 1 - cost
                reason = "not_filled(counterfactual)"

        # The benchmark must span the position's *own* holding period, not the
        # idea's age. A limit order that took three sessions to fill is held for
        # a shorter window than the idea has existed, and charging it a benchmark
        # measured from the idea date compares five days of position against
        # eight days of index. On the `naive` book every fill lands on the idea
        # date so this changes nothing today; on `disciplined` 36 of 119 fills are
        # late, and the new weekly design fills later by construction.
        bench_from = (pos["opened_d"] if pos and pos["opened_d"] else idea["as_of"])

        # Sessions actually elapsed. An idea generated today has none, so its
        # "return" would be nothing but the round-trip cost — including those in
        # the aggregates would drag the hit rate purely as a function of how
        # recently the batch was written.
        held = len([r2["d"] for r2 in db.q(
            con, "SELECT d FROM prices WHERE code=? AND d>? AND d<=? ORDER BY d",
            (bench, bench_from, mark_to))])
        br = benchmark_return(con, bench, bench_from, mark_to)
        scen = _scenario_bucket(realized, idea["central_r"])
        out.append({
            "idea_uid": idea["idea_uid"], "as_of": idea["as_of"],
            "horizon": idea["horizon"], "horizon_end": hz_end,
            "grade": idea["grade"], "or_c": idea["or_c"], "or_k": idea["or_k"],
            "ev_c": idea["ev_c"],
            "entry_px": entry_px, "exit_px": exit_px,
            "realized": realized, "bench_ret": br,
            "excess": (realized - br) if (realized is not None and br is not None) else None,
            "scenario": scen,
            "brier_c": _brier(idea["central_p"], scen),
            "brier_k": _brier(idea["conserv_p"], scen),
            "filled": int(filled), "exit_reason": reason,
            "sessions_held": held,
            "settled_at": config.now_hkt().isoformat(),
        })
        settled += 1

    db.upsert_many(con, "outcomes", out, ["idea_uid"])
    if verbose:
        n_real = sum(1 for o in out if o["realized"] is not None)
        print(f"  settled {settled} outcomes ({n_real} with a realised return) "
              f"on book={book_id}")
    return {"settled": settled, "rows": out}


def _scenario_bucket(realized: float | None, central_r: Sequence[float]) -> str | None:
    """Which of the three pre-stated scenarios did the outcome land in?

    Boundaries are the midpoints between the stated legs, so the mapping is fully
    determined by the forecast itself.
    """
    if realized is None or len(central_r) != 3:
        return None
    up, base, dn = (float(x) / 100.0 for x in central_r)
    hi_cut = (up + base) / 2
    lo_cut = (base + dn) / 2
    if realized >= hi_cut:
        return "up"
    if realized <= lo_cut:
        return "down"
    return "base"


def _brier(probs: Sequence[float], bucket: str | None) -> float | None:
    """Multi-class Brier score over (up, base, down). Lower is better; 0 = perfect."""
    if bucket is None or len(probs) != 3:
        return None
    idx = {"up": 0, "base": 1, "down": 2}[bucket]
    p = [float(x) / 100.0 for x in probs]
    return round(sum((p[i] - (1.0 if i == idx else 0.0)) ** 2 for i in range(3)), 5)


# ---------------------------------------------------------------- reports
def book_report(con, book_id: str) -> dict:
    eq = [dict(r) for r in db.q(
        con, "SELECT * FROM equity WHERE book_id=? ORDER BY d", (book_id,))]
    if not eq:
        return {"book_id": book_id, "empty": True}
    start, end = eq[0]["d"], eq[-1]["d"]
    rets = [e["ret_d"] for e in eq[1:] if e["ret_d"] is not None]
    ann_vol = (st.stdev(rets) * math.sqrt(252)) if len(rets) > 2 else None
    cum = eq[-1]["cum_ret"]
    days = len(eq) - 1
    ann_ret = ((1 + cum) ** (252 / days) - 1) if days > 3 and cum > -1 else None

    benches = {}
    for name, code in config.BENCHMARKS.items():
        if code:
            benches[name] = benchmark_return(con, code, start, end)
    acwi = benches.get("ACWI")
    agg = benches.get("AGG")
    if acwi is not None and agg is not None:
        benches["60/40"] = 0.6 * acwi + 0.4 * agg

    closed = [dict(r) for r in db.q(
        con, "SELECT * FROM positions WHERE book_id=? AND status='closed'", (book_id,))]
    open_p = [dict(r) for r in db.q(
        con, "SELECT * FROM positions WHERE book_id=? AND status='open'", (book_id,))]

    return {
        "book_id": book_id, "label": config.BOOKS[book_id]["label"],
        "desc": config.BOOKS[book_id]["desc"],
        "from": start, "to": end, "sessions": days,
        "capital": config.BOOKS[book_id]["capital"],
        "equity": eq[-1]["equity"], "cash": eq[-1]["cash"], "mv": eq[-1]["mv"],
        "cum_ret": cum, "ann_ret": ann_ret, "ann_vol": ann_vol,
        "sharpe": (round(ann_ret / ann_vol, 3)
                   if (ann_ret is not None and ann_vol) else None),
        "max_drawdown": min((e["drawdown"] or 0) for e in eq),
        "gross": eq[-1]["gross"], "n_open": eq[-1]["n_open"],
        "n_closed": len(closed),
        "exit_reasons": _count(closed, "exit_reason"),
        "benchmarks": benches,
        "excess_vs_spy": (cum - benches["SPY"]) if benches.get("SPY") is not None else None,
        # A book that is 13% invested by design should not be scored against a
        # fully-invested index. The matched benchmark holds SPY at the book's own
        # average gross exposure and the remainder in the same cash yield, which
        # isolates selection from the sizing rule.
        "avg_gross": round(st.mean([e["gross"] or 0 for e in eq[1:]]), 4) if days else None,
        "matched_benchmark": _matched_benchmark(con, eq, benches.get("SPY"), start, end),
        "curve": [{"d": e["d"], "equity": e["equity"], "cum_ret": e["cum_ret"],
                   "drawdown": e["drawdown"], "gross": e["gross"],
                   "n_open": e["n_open"], "cash": e["cash"]} for e in eq],
        "orders": _order_stats(con, book_id),
    }


def _matched_benchmark(con, eq: list[dict], spy_ret: float | None,
                       start: str, end: str) -> dict | None:
    """SPY held at the book's own average gross exposure, rest in cash."""
    from .sources import olive

    if spy_ret is None or len(eq) < 2:
        return None
    g = st.mean([e["gross"] or 0 for e in eq[1:]])
    y = olive.cash_yield(con, "USD") or config.RISK_FREE_ANNUAL
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    cash_ret = y * days / 365.0
    blended = g * spy_ret + (1 - g) * cash_ret
    book_cum = eq[-1]["cum_ret"]
    return {"gross": round(g, 4), "spy_ret": spy_ret, "cash_ret": round(cash_ret, 6),
            "blended_ret": round(blended, 6),
            "excess": round(book_cum - blended, 6),
            "label": f"{g*100:.0f}% SPY + {(1-g)*100:.0f}% 现金"}


def _count(rows: Iterable[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "—")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _order_stats(con, book_id: str) -> dict:
    rows = [dict(r) for r in db.q(con, "SELECT * FROM orders WHERE book_id=?", (book_id,))]
    return {"placed": len(rows), "by_status": _count(rows, "status"),
            "by_kind": _count(rows, "kind"),
            "fill_rate": (round(sum(1 for r in rows if r["status"] == "filled")
                                / len(rows), 3) if rows else None)}


def idea_report(con, as_of: str | None = None) -> dict:
    """Idea-level skill, independent of position sizing."""
    sql = ("SELECT o.*, i.tool, i.theme, i.instrument, i.grade_rel, i.vol_check, "
           "i.horizon_months, i.pos_init, i.signal_id "
           "FROM outcomes o JOIN ideas i ON i.idea_uid=o.idea_uid")
    args: list[Any] = []
    if as_of:
        sql += " WHERE o.as_of=?"
        args.append(as_of)
    rows = [dict(r) for r in db.q(con, sql, args)]
    fresh = [r for r in rows if (r.get("sessions_held") or 0) < 1]
    scored = [r for r in rows
              if r["realized"] is not None and (r.get("sessions_held") or 0) >= 1]
    if not scored:
        return {"n": len(rows), "scored": 0, "too_fresh": len(fresh)}

    rz = [r["realized"] for r in scored]
    ex = [r["excess"] for r in scored if r["excess"] is not None]
    return {
        "n": len(rows), "scored": len(scored),
        "too_fresh": len(fresh),
        "unmarkable": sum(1 for r in rows if r["realized"] is None),
        "equal_weight_ret": round(st.mean(rz), 6),
        "median_ret": round(st.median(rz), 6),
        "hit_rate": round(sum(1 for x in rz if x > 0) / len(rz), 3),
        "excess_mean": (round(st.mean(ex), 6) if ex else None),
        "beat_bench_rate": (round(sum(1 for x in ex if x > 0) / len(ex), 3) if ex else None),
        "best": max(scored, key=lambda r: r["realized"])["tool"],
        "worst": min(scored, key=lambda r: r["realized"])["tool"],
        "ranking": ranking_report(scored),
        "calibration": calibration_report(scored),
        "buckets": {
            "grade": _bucket(scored, "grade"),
            "grade_rel": _bucket(scored, "grade_rel"),
            "horizon": _bucket(scored, "horizon"),
            "theme": _bucket(scored, "theme"),
            "instrument": _bucket(scored, "instrument"),
            "vol_check": _bucket(scored, "vol_check"),
            "filled": _bucket(scored, "filled"),
        },
        "rows": sorted(scored, key=lambda r: -(r["realized"] or 0)),
    }


def ranking_report(scored: list[dict]) -> dict:
    """Does the engine's own ordering predict the outcome?"""
    out: dict[str, Any] = {}
    for key, label in (("or_k", "保守赔率"), ("or_c", "中心赔率"), ("ev_c", "中心期望回报")):
        out[key] = {
            "label": label,
            "rho_vs_realized": spearman([r[key] for r in scored],
                                        [r["realized"] for r in scored]),
            "rho_vs_excess": spearman([r[key] for r in scored],
                                      [r["excess"] for r in scored]),
        }
    return out


def calibration_report(scored: list[dict]) -> dict:
    """Were the stated probabilities honest?

    Brier is reported against a naive 1/3-1/3-1/3 baseline, because a Brier score
    on its own is not interpretable. `skill` > 0 means the forecast beat the
    uninformative prior.
    """
    bc = [r["brier_c"] for r in scored if r["brier_c"] is not None]
    bk = [r["brier_k"] for r in scored if r["brier_k"] is not None]
    uniform = 3 * (1 / 3 - 0) ** 2      # = 0.6667 for a 3-class uniform forecast
    uniform = (1 / 3 - 1) ** 2 + 2 * (1 / 3) ** 2
    buckets = _count(scored, "scenario")
    n = sum(buckets.values()) or 1
    return {
        "n": len(bc),
        "brier_central": (round(st.mean(bc), 5) if bc else None),
        "brier_conservative": (round(st.mean(bk), 5) if bk else None),
        "brier_uniform_baseline": round(uniform, 5),
        "skill_central": (round(1 - st.mean(bc) / uniform, 4) if bc else None),
        "skill_conservative": (round(1 - st.mean(bk) / uniform, 4) if bk else None),
        "scenario_realised": buckets,
        "scenario_realised_pct": {k: round(v / n, 3) for k, v in buckets.items()},
        "stated_avg": _stated_avg(scored),
    }


def _stated_avg(scored: list[dict]) -> dict | None:
    return None if not scored else {
        "note": "average stated central probabilities vs realised scenario mix",
    }


def _bucket(scored: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in scored:
        groups.setdefault(str(r.get(key) if r.get(key) is not None else "—"), []).append(r)
    out = {}
    for k, g in sorted(groups.items()):
        rz = [x["realized"] for x in g]
        ex = [x["excess"] for x in g if x["excess"] is not None]
        out[k] = {"n": len(g), "mean": round(st.mean(rz), 6),
                  "median": round(st.median(rz), 6),
                  "hit": round(sum(1 for v in rz if v > 0) / len(rz), 3),
                  "excess": (round(st.mean(ex), 6) if ex else None)}
    return out


def full_report(con) -> dict:
    settle(con, book_id="disciplined", verbose=False)
    disc_outcomes = idea_report(con)
    settle(con, book_id="naive", verbose=False)
    return {
        "generated_at": config.now_hkt().isoformat(),
        "methodology": config.METHODOLOGY_VERSION,
        "books": {b: book_report(con, b) for b in config.BOOKS},
        "ideas": idea_report(con),
        "ideas_disciplined_fills": disc_outcomes,
        "batches": [dict(r) for r in db.q(
            con, "SELECT batch_id, as_of, generator, n_ideas, status FROM batches "
                 "ORDER BY as_of")],
        "coverage": coverage(con),
    }


def coverage(con) -> dict:
    docs = db.q1(con, "SELECT COUNT(*) n, COUNT(DISTINCT published_d) days, "
                      "MIN(published_d) a, MAX(published_d) b FROM documents")
    px = db.q1(con, "SELECT COUNT(DISTINCT code) codes, COUNT(*) bars, MAX(d) last "
                    "FROM prices")
    nav = db.q1(con, "SELECT COUNT(DISTINCT olive_key) keys, COUNT(*) rows, MAX(d) last "
                     "FROM navs")
    th = db.q1(con, "SELECT COUNT(DISTINCT as_of) days FROM themes")
    return {"documents": dict(docs), "prices": dict(px), "navs": dict(nav),
            "theme_days": th["days"]}


# ---------------------------------------------------------------- console
def print_report(con) -> dict:
    rep = full_report(con)
    print("\n" + "=" * 78)
    print(f"IdeaGen40 · 方法论 v{rep['methodology']} · {rep['generated_at'][:16]}")
    print("=" * 78)

    for bid, b in rep["books"].items():
        if b.get("empty"):
            continue
        print(f"\n【{b['label']}】{b['from']} → {b['to']}  ({b['sessions']} 个交易日)")
        print(f"  权益 ${b['equity']:,.0f}   累计 {_pct(b['cum_ret'])}   "
              f"最大回撤 {_pct(b['max_drawdown'])}   "
              f"年化波动 {_pct(b['ann_vol']) if b['ann_vol'] else '—'}   "
              f"Sharpe {b['sharpe'] if b['sharpe'] is not None else '—'}")
        print(f"  现金 ${b['cash']:,.0f}  持仓市值 ${b['mv']:,.0f}  "
              f"净敞口 {b['gross']*100:.0f}%  在场 {b['n_open']}  已平 {b['n_closed']}")
        bm = "   ".join(f"{k} {_pct(v)}" for k, v in b["benchmarks"].items()
                        if v is not None)
        print(f"  基准: {bm}")
        print(f"  超额(vs SPY): {_pct(b['excess_vs_spy'])}")
        mb = b.get("matched_benchmark")
        if mb:
            print(f"  敞口匹配基准 [{mb['label']}] {_pct(mb['blended_ret'])}   "
                  f"超额 {_pct(mb['excess'])}   ← 剔除仓位规模影响后的选股读数")
        o = b["orders"]
        print(f"  订单 {o['placed']} 笔  成交率 {o['fill_rate']}  {o['by_status']}")
        if b["exit_reasons"]:
            print(f"  离场原因: {b['exit_reasons']}")

    ir = rep["ideas"]
    if ir.get("scored"):
        print(f"\n【想法层面（等权，剔除仓位管理影响）】")
        print(f"  可评分 {ir['scored']}/{ir['n']}   无法盯市 {ir['unmarkable']}"
              f"   持有期不足1个交易日(今日新增) {ir.get('too_fresh', 0)}")
        print(f"  等权收益 {_pct(ir['equal_weight_ret'])}   中位 {_pct(ir['median_ret'])}"
              f"   胜率 {ir['hit_rate']*100:.0f}%")
        print(f"  超额均值 {_pct(ir['excess_mean'])}   跑赢基准比例 "
              f"{(ir['beat_bench_rate'] or 0)*100:.0f}%")
        print(f"\n  排序能力（引擎说的好，后来是否真的好）")
        for k, v in ir["ranking"].items():
            print(f"    {v['label']:<10} ρ vs 实际 {str(v['rho_vs_realized']):>8}"
                  f"    ρ vs 超额 {str(v['rho_vs_excess']):>8}")
        c = ir["calibration"]
        print(f"\n  概率校准  Brier 中心 {c['brier_central']}  保守 {c['brier_conservative']}"
              f"  (均匀基线 {c['brier_uniform_baseline']})")
        print(f"    技能分 中心 {c['skill_central']}  保守 {c['skill_conservative']}"
              f"   >0 表示优于无信息先验")
        print(f"    情景实现分布 {c['scenario_realised_pct']}")
        for name, lab in (("grade", "评级"), ("horizon", "期限"),
                          ("instrument", "工具"), ("filled", "是否成交")):
            print(f"\n  按{lab}分档")
            for k, v in ir["buckets"][name].items():
                print(f"    {k:<12} n={v['n']:<3} 均值 {_pct(v['mean']):>9} "
                      f"中位 {_pct(v['median']):>9} 胜率 {v['hit']*100:>3.0f}%  "
                      f"超额 {_pct(v['excess'])}")
    cv = rep["coverage"]
    print(f"\n【数据覆盖】研报 {cv['documents']['n']} 条 / "
          f"{cv['documents']['days']} 天 ({cv['documents']['a']}→{cv['documents']['b']})"
          f"   行情 {cv['prices']['codes']} 标的 / {cv['prices']['bars']} 根 "
          f"(至 {cv['prices']['last']})   NAV {cv['navs']['keys']} 只 / "
          f"{cv['navs']['rows']} 点   打分日 {cv['theme_days']}")
    print()
    return rep
