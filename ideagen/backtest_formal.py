"""正式回测：用模拟运行的那套交易代码，把已存的周跑判决按当期日历重新走一遍。

Jon 的要求是「正式历史回测应尽量复用模拟运行的交易规则——分批资金、仓位分配、交易
成本、止盈止损、到期退出」。现有的 30 天持有回测（`scripts/run_real_backtest.py`）
回答的是另一个问题：候选和排序有没有信息。它无条件成交、固定持有、没有止损、没有
现金账，所以它的净值不是任何账户会经历的净值。

这里不另写一套引擎。每个成功周跑的每个选取臂，把它当时选中的候选原样经
`booking.payload_from_candidates` → `ideas.build_batch` 建成批次，下到一本回测专用书
`bt:<backtest_id>:<arm>`，然后沿库里 US.SPY 的交易日逐日调用 `paper.step`。成交、
止损、止盈、到期、事件退出、现金计息、两腿成本——全部是 `paper.py` 里同一行代码。
两份记录**唯一**允许不同的地方是订单何时下：模拟账户的订单是在真实时间下的（补跑期
是 09-04 一天内挤着成交），这里按 as_of 当天 07:23 HKT 的生成时点（`backtest.GENERATION_TIME`）
钳制，订单在当期的第一个可成交收盘成交。这一差异和其余无法一致的环节写进
`summary.disclosures`，随记录走。

隔离是硬的：回测书带 `bt:` 前缀，`paper.all_books` 明确排除它们，所以日常盯市循环碰
不到；批次带 `BT-<backtest_id>-` 前缀，generator 记 `backtest:<backtest_id>:<run_id>`。
同一个 backtest_id 重跑先清理再建，幂等。跑完账本保留（不清理），因为业绩页的周 PnL
分解与归因要从它读——清理了就只剩净值点。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import backtest, booking, config, db, ideas as ideas_mod, paper
from . import strategy as strat
from .sources import futu_px

METHODOLOGY = "formal-paper-rules"

#: What is different from the paper account, stated once and stored with every
#: run. Each line names the mechanism, not a feeling about it.
DISCLOSURES = [
    "下单时点：模拟账户的订单在真实生成时刻下（08-26 那期 03:22 HKT；五个补跑期都在 "
    "2026-09-04 生成，订单当天集中成交、且到期日已过，建仓当天即按「到期」平掉）。"
    "正式回测把每期的生成时刻钳制到该期日期 07:23 HKT，订单在当期第一个可成交收盘成交，"
    "并按 5 个交易日的订单寿命等待。",
    "成交价、成本、仓位：与模拟运行同一段代码（paper.step / _apply_fill / size_batch）："
    "收盘市价成交，两腿各计佣金+滑点的一半，每期一个 tranche 占资本 25%、期内等权，"
    "不能超过当时的现金余额。",
    "止损止盈：同一段代码，σ×2 / σ×3 在建仓时按当时的已实现波动固定，不移动。",
    "事件退出（thesis_invalidated）：依赖当时实时产生的 alerts；回测书没有 alerts 记录，"
    "所以这一种退出在正式回测里**永远不会触发**，模拟账户里则可能触发。",
    "现金利息：同一公式（前一交易日现金 × 年化 × 日历天/365），利率是回测运行时 "
    "olive.cash_yield 的取值（货架中位数或兜底常数），每天的 INT 记录里写明。",
    "候选与判决：直接复用 orch_runs 里已存的 verdicts / candidates，不重跑选取；"
    "当日无价的标的与模拟运行一样被剔除（booking._priced_only）。",
    "批次校验：同一个 ideas.validate_batch；模拟运行里因参考价缺失而卡住的批次，"
    "在这里若价格已补齐则会建仓，若仍缺则同样跳过并记录在 summary.arms.<组合>.errors。",
    "价格：模拟账户是逐日按当时拿到的行情盯市；正式回测用库里今天的 prices 表整段回放，"
    "行情后来的修订会体现在这里、不会体现在模拟账户里。",
    "可交易范围：两边都没有按上架日期（first_seen_d）过滤候选。",
]


def _weekly_runs(con, start: str | None, end: str | None) -> list[dict[str, Any]]:
    """One successful weekly run per period, newest attempt wins."""
    rows = db.q(con, "SELECT run_id, as_of, data_classification, ended_at FROM orch_runs "
                     "WHERE kind='weekly' AND ok=1 ORDER BY as_of, ended_at DESC")
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if start and r["as_of"] < start:
            continue
        if end and r["as_of"] > end:
            continue
        seen.setdefault(r["as_of"], dict(r))
    return [seen[k] for k in sorted(seen)]


def _verdicts(con, run_id: str) -> dict[str, list[dict[str, Any]]]:
    """`arm -> chosen candidate payloads` for one run's idea_selector stage."""
    cands = {str(r["candidate_id"]): db.jl(r["payload"], {}) for r in db.q(
        con, "SELECT candidate_id, payload FROM candidates WHERE run_id=?", (run_id,))}
    out: dict[str, list[dict[str, Any]]] = {}
    for v in db.q(con, "SELECT strategy, chosen FROM verdicts WHERE run_id=? "
                       "AND kind='idea_selector'", (run_id,)):
        chosen = db.jl(v["chosen"], []) or []
        out[v["strategy"]] = [cands[c] for c in chosen if c in cands]
    return out


def plan(con, *, start: str | None = None, end: str | None = None,
         arms: list[str] | None = None) -> dict[str, Any]:
    """Periods and arms a run would cover, without touching anything."""
    runs = _weekly_runs(con, start, end)
    periods = []
    all_arms: set[str] = set()
    for r in runs:
        vs = _verdicts(con, r["run_id"])
        names = sorted(a for a in vs if arms is None or a in arms)
        all_arms |= set(names)
        periods.append({"as_of": r["as_of"], "run_id": r["run_id"],
                        "classification": r["data_classification"] or "live",
                        "arms": {a: len(vs[a]) for a in names}})
    return {"periods": periods, "arms": sorted(all_arms)}


def backtest_id_for(dates: list[str], arms: list[str]) -> str:
    sha = hashlib.sha256(json.dumps(
        {"dates": dates, "arms": arms, "methodology": METHODOLOGY},
        sort_keys=True).encode()).hexdigest()
    last = dates[-1].replace("-", "") if dates else "00000000"
    return f"bt-formal-{last}-{sha[:10]}"


def cleanup(con, backtest_id: str) -> dict[str, int]:
    """Remove everything one backtest id wrote, so a rerun starts clean.

    Books first (orders/positions/trades/equity/mtm/alerts), then the batches
    and their ideas, then the result tables. Nothing here matches a `sel-` or
    `W...` row: every pattern carries the backtest id.
    """
    n = {"books": 0, "batches": 0, "points": 0, "positions": 0}
    for b in paper.backtest_books(con, backtest_id):
        paper.reset_book(con, b)
        con.execute("DELETE FROM books WHERE book_id=?", (b,))
        n["books"] += 1
    for r in db.q(con, "SELECT batch_id FROM batches WHERE batch_id LIKE ?",
                  (f"{config.BACKTEST_BATCH_PREFIX}{backtest_id}-%",)):
        ideas_mod.purge_batch(con, r["batch_id"])
        con.execute("DELETE FROM batches WHERE batch_id=?", (r["batch_id"],))
        n["batches"] += 1
    n["points"] = con.execute("DELETE FROM backtest_points WHERE backtest_id=?",
                              (backtest_id,)).rowcount
    n["positions"] = con.execute("DELETE FROM backtest_positions WHERE backtest_id=?",
                                 (backtest_id,)).rowcount
    con.execute("DELETE FROM backtest_runs WHERE backtest_id=?", (backtest_id,))
    con.commit()
    return n


def _ensure_book(con, book_id: str, arm: str) -> None:
    spec = config.SELECTOR_SPEC
    db.upsert(con, "books", {
        "book_id": book_id, "label": f"正式回测 · {arm}", "descr": spec["desc"],
        "capital": spec["capital"], "sizing": spec["sizing"], "entry": spec["entry"],
        "created_at": config.now_hkt().isoformat()}, ["book_id"])


def _open_period(con, backtest_id: str, run: dict[str, Any], arm: str,
                 chosen: list[dict[str, Any]], log) -> dict[str, Any]:
    """Build one (period, arm) batch on the backtest book and place its orders."""
    as_of = date.fromisoformat(run["as_of"])
    book_id = config.backtest_book(backtest_id, arm)
    rep: dict[str, Any] = {"as_of": run["as_of"], "arm": arm}
    chosen, unpriced = booking._priced_only(con, chosen, as_of)
    if unpriced:
        rep["unpriced"] = unpriced
    if not chosen:
        rep["skipped"] = ("选中的标的当日都没有价格" if unpriced else "该期没有选中任何想法")
        return rep
    batch_id = f"{config.BACKTEST_BATCH_PREFIX}{backtest_id}-W{as_of.isoformat().replace('-', '')}-{arm}"
    # The instant a live run for this period would have existed. Everything
    # downstream (first fillable bar, order expiry) derives from it.
    generated_at = f"{as_of.isoformat()}T{backtest.GENERATION_TIME}"
    _, rows, val = ideas_mod.build_batch(
        con, booking.payload_from_candidates(chosen, run_id=run["run_id"]), as_of,
        generator=f"backtest:{backtest_id}:{run['run_id']}", batch_id=batch_id,
        generated_at=generated_at)
    if not (val or {}).get("pass", False):
        failed = sorted({c.get("check") for c in (val or {}).get("checks", [])
                         if not c.get("ok") and c.get("severity") == "error"})
        rep["error"] = f"批次校验未过：{'、'.join(failed) or '未记录'}"
        log(f"  ✗ {run['as_of']} {arm}: {rep['error']}")
        return rep
    rep["stops_fixed"] = booking._fix_stops(con, batch_id)
    _ensure_book(con, book_id, arm)
    orep = paper.open_batch(con, batch_id, book_id, verbose=False)
    rep.update(batch_id=batch_id, orders=orep.get("placed", 0),
               skipped_ideas=orep.get("skipped") or {}, n_ideas=len(rows))
    log(f"  ✓ {run['as_of']} {arm:<26} {len(rows)} 条想法，下单 {rep['orders']} 张"
        + (f"，当日无价剔除 {len(unpriced)}" if unpriced else ""))
    return rep


def _arm_stats(con, book_id: str) -> dict[str, Any]:
    eq = [dict(r) for r in db.q(
        con, "SELECT d, equity, cash, drawdown FROM equity WHERE book_id=? ORDER BY d",
        (book_id,))]
    orders = {r["status"]: r["n"] for r in db.q(
        con, "SELECT status, COUNT(*) n FROM orders WHERE book_id=? GROUP BY status",
        (book_id,))}
    exits = {r["exit_reason"]: r["n"] for r in db.q(
        con, "SELECT exit_reason, COUNT(*) n FROM positions WHERE book_id=? "
             "AND status='closed' GROUP BY exit_reason", (book_id,))}
    pos = db.q1(con, "SELECT COUNT(*) n, SUM(status='open') o, "
                     "COUNT(DISTINCT as_of) np, COALESCE(SUM(realized),0) r "
                     "FROM positions WHERE book_id=?", (book_id,))
    cap = float(config.SELECTOR_SPEC["capital"])
    return {
        "book_id": book_id,
        "first_d": eq[0]["d"] if eq else None, "last_d": eq[-1]["d"] if eq else None,
        "n_days": max(len(eq) - 1, 0),
        "equity_end": round(eq[-1]["equity"], 2) if eq else None,
        "cum_ret_pct": (round((eq[-1]["equity"] / cap - 1) * 100.0, 4) if eq else None),
        "max_dd_pct": (round(min(r["drawdown"] or 0 for r in eq) * 100.0, 4) if eq else None),
        "cash_share_end_pct": (round(eq[-1]["cash"] / eq[-1]["equity"] * 100.0, 4)
                               if eq and eq[-1]["equity"] else None),
        "orders": orders, "exits": exits,
        "n_positions": pos["n"], "n_open": pos["o"] or 0,
        "n_periods_held": pos["np"], "realized": round(pos["r"], 2),
    }


def run(con, *, start: str | None = None, end: str | None = None,
        arms: list[str] | None = None, backtest_id: str | None = None,
        verbose: bool = True) -> dict[str, Any]:
    """Replay every stored weekly verdict through the paper engine and record it.

    Period by period, in date order: open that period's batches on every arm,
    then step every backtest book through the sessions up to the next period.
    Interleaving matters — `size_batch` sizes against the cash the book has on
    the day, which is only right if the earlier tranches have already been
    marked forward to that day.
    """
    log = print if verbose else (lambda *a: None)
    runs = _weekly_runs(con, start, end)
    if not runs:
        raise ValueError("窗口内没有成功完成的周跑（orch_runs kind=weekly ok=1）")
    verdicts = {r["run_id"]: _verdicts(con, r["run_id"]) for r in runs}
    arm_names = sorted({a for vs in verdicts.values() for a in vs
                        if arms is None or a in arms})
    if not arm_names:
        raise ValueError("窗口内没有可回测的选取策略")
    dates = [r["as_of"] for r in runs]
    backtest_id = backtest_id or backtest_id_for(dates, arm_names)
    started = datetime.now(timezone.utc).isoformat()

    cleaned = cleanup(con, backtest_id)
    if any(cleaned.values()):
        log(f"重跑 {backtest_id}：清理 {cleaned}")

    # The last session anything may be marked to: today's closed session,
    # bounded by the bars that actually exist, bounded by the caller's window.
    last_bar = db.q1(con, "SELECT MAX(d) d FROM prices WHERE code=?",
                     (config.BENCHMARKS["SPY"],))
    stop = min(x for x in (futu_px.complete_through("US"),
                           last_bar["d"] if last_bar and last_bar["d"] else None,
                           end) if x)
    log(f"正式回测 {backtest_id}：{len(runs)} 期（{dates[0]} → {dates[-1]}），"
        f"{len(arm_names)} 个组合，盯市至 {stop}")

    per_arm: dict[str, dict[str, Any]] = {
        a: {"periods": {}, "errors": {}, "skipped": {}, "unpriced": {}} for a in arm_names}
    active: list[str] = []
    for i, r in enumerate(runs):
        as_of = r["as_of"]
        for arm in arm_names:
            chosen = verdicts[r["run_id"]].get(arm)
            if chosen is None:
                per_arm[arm]["skipped"][as_of] = "该期没有这个组合的判决"
                continue
            try:
                rep = _open_period(con, backtest_id, r, arm, chosen, log)
            except Exception as e:  # noqa: BLE001 — one arm-period must not sink the run
                rep = {"error": f"{type(e).__name__}: {e}"}
                log(f"  ✗ {as_of} {arm}: {rep['error']}")
            if rep.get("error"):
                per_arm[arm]["errors"][as_of] = rep["error"]
            elif rep.get("skipped"):
                per_arm[arm]["skipped"][as_of] = rep["skipped"]
            else:
                per_arm[arm]["periods"][as_of] = {
                    k: v for k, v in rep.items() if k not in ("as_of", "arm")}
                b = config.backtest_book(backtest_id, arm)
                if b not in active:
                    active.append(b)
            if rep.get("unpriced"):
                per_arm[arm]["unpriced"][as_of] = rep["unpriced"]
        # Sessions of this period: from as_of up to the day before the next
        # period (or the stop). Every active book steps every session, so a
        # book with nothing new this week still accrues interest and marks.
        nxt = runs[i + 1]["as_of"] if i + 1 < len(runs) else None
        seg_end = stop if not nxt else min(
            stop, (date.fromisoformat(nxt) - timedelta(days=1)).isoformat())
        if seg_end < as_of:
            continue
        sessions = paper.sessions_between(con, as_of, seg_end)
        for d in sessions:
            for b in active:
                paper.step(con, b, d, verbose=False)
        log(f"  · {as_of} 段：{len(sessions)} 个交易日，{len(active)} 本书")

    # Results. Points from the books' own equity rows; positions from the
    # ledger, with the open ones marked at their last mark.
    points: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    arm_summary: dict[str, Any] = {}
    for arm in arm_names:
        b = config.backtest_book(backtest_id, arm)
        stats = _arm_stats(con, b) if b in active else {
            "book_id": None, "first_d": None, "last_d": None, "n_days": 0,
            "equity_end": None, "cum_ret_pct": None, "max_dd_pct": None,
            "cash_share_end_pct": None, "orders": {}, "exits": {},
            "n_positions": 0, "n_open": 0, "n_periods_held": 0, "realized": 0.0}
        stats.update(periods_booked=len(per_arm[arm]["periods"]),
                     errors=per_arm[arm]["errors"], skipped=per_arm[arm]["skipped"],
                     unpriced=per_arm[arm]["unpriced"],
                     status=("ok" if per_arm[arm]["periods"] else "未建仓"))
        arm_summary[arm] = stats
        if b not in active:
            continue
        for e in db.q(con, "SELECT d, equity, ret_d, drawdown, n_open FROM equity "
                           "WHERE book_id=? ORDER BY d", (b,)):
            points.append({"backtest_id": backtest_id, "arm": arm, "d": e["d"],
                           "equity": float(e["equity"]),
                           "period_ret": round(float(e["ret_d"] or 0) * 100.0, 6),
                           "drawdown": round(float(e["drawdown"] or 0) * 100.0, 6),
                           "n_positions": int(e["n_open"] or 0)})
        for p_ in db.q(con, "SELECT p.*, i.tool, i.thesis FROM positions p "
                            "JOIN ideas i ON i.idea_uid = p.idea_uid "
                            "WHERE p.book_id=? ORDER BY p.as_of, p.code", (b,)):
            if p_["status"] == "closed":
                exit_d, exit_px = p_["closed_d"], p_["close_px"]
                ret = (float(p_["realized"]) / float(p_["cost"]) * 100.0
                       if p_["cost"] else None)
                status = f"closed:{p_['exit_reason']}"
            else:
                m = db.q1(con, "SELECT d, px, upnl FROM mtm WHERE pos_id=? "
                               "ORDER BY d DESC LIMIT 1", (p_["pos_id"],))
                exit_d, exit_px = (m["d"], m["px"]) if m else (None, None)
                ret = (float(m["upnl"]) / float(p_["cost"]) * 100.0
                       if m and p_["cost"] else None)
                status = "open"
            positions.append({
                "backtest_id": backtest_id, "arm": arm, "period": p_["as_of"],
                "instrument_id": p_["tool"] or p_["code"],
                "entry_d": p_["opened_d"], "exit_d": exit_d,
                "entry_nav": p_["avg_px"], "exit_nav": exit_px,
                "return_pct": None if ret is None else round(ret, 6),
                "status": status, "thesis": p_["thesis"]})

    classes = {r["as_of"]: (r["data_classification"] or "live") for r in runs}
    n_backfill = sum(1 for c in classes.values() if c != "live")
    # Arms the registry has but no verdict in the window carried: absent, and
    # the reason is in the record so the roster can quote it.
    registry = {s["name"]: s for s in strat.available("idea_selector")}
    excluded = sorted(set(registry) - set(arm_names))
    excluded_reasons = {}
    for a in excluded:
        if arms is not None and a not in arms:
            excluded_reasons[a] = "本次回测按 --arms 参数未包含"
        else:
            excluded_reasons[a] = "窗口内的周跑没有这个组合的 idea_selector 判决"
    summary = {
        "methodology": METHODOLOGY,
        "data_classification": "mixed-live-backfill" if n_backfill else "live",
        "periods": len(runs), "dates": dates, "period_classification": classes,
        "n_live_periods": len(runs) - n_backfill, "n_backfill_periods": n_backfill,
        "runs": {r["as_of"]: r["run_id"] for r in runs},
        "capital": config.SELECTOR_SPEC["capital"],
        "tranche_frac": config.SELECTOR_SPEC["tranche_frac"],
        "marked_through": stop,
        "books_retained": True,
        "book_ids": [config.backtest_book(backtest_id, a) for a in arm_names
                     if config.backtest_book(backtest_id, a) in active],
        "arms": arm_summary,
        "excluded_arms": excluded, "excluded_reasons": excluded_reasons,
        "disclosures": DISCLOSURES,
        "model_calls": 0,
        "predictive_claim": False,
    }
    inputs_sha = hashlib.sha256(json.dumps(
        {"dates": dates, "arms": arm_names, "methodology": METHODOLOGY},
        sort_keys=True).encode()).hexdigest()
    with db.tx(con):
        db.upsert(con, "backtest_runs", {
            "backtest_id": backtest_id, "as_of": dates[-1],
            "window_start": dates[0], "window_end": stop,
            "methodology": METHODOLOGY,
            "data_classification": summary["data_classification"],
            "model_id": None, "model_release_date": None, "knowledge_cutoff": None,
            "inputs_sha": inputs_sha, "artifact_uri": None,
            "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
            "ok": 1, "error": None,
            "summary": json.dumps(summary, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False),
        }, ["backtest_id"])
        db.upsert_many(con, "backtest_points", points, ["backtest_id", "arm", "d"])
        db.upsert_many(con, "backtest_positions", positions,
                       ["backtest_id", "arm", "period", "instrument_id"])

    receipt = {"backtest_id": backtest_id, "periods": len(runs), "dates": dates,
               "arms": arm_names, "points": len(points), "positions": len(positions),
               "marked_through": stop, "summary": summary}
    if verbose:
        print(f"\n正式回测已落库 {backtest_id}：{len(points)} 个净值点 · "
              f"{len(positions)} 条持仓 · 盯市至 {stop}")
        for a in arm_names:
            s = arm_summary[a]
            cum = "—" if s["cum_ret_pct"] is None else f"{s['cum_ret_pct']:+.3f}%"
            o = s["orders"]
            print(f"  {a:<26} {cum:>9}  期 {s['periods_booked']}/{len(runs)}  "
                  f"成交 {o.get('filled', 0)} 未成交 {o.get('expired', 0)} 挂单 {o.get('pending', 0)}  "
                  f"退出 {s['exits']}  错误 {len(s['errors'])}")
    return receipt
