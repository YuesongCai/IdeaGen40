"""业绩分析的数据层：模拟运行与历史回测共用一个视图形状，数据完全分开。

Jon 2026-09-06 第 10 条的后端。一个 `PerfView` 文档回答四个问题——净值曲线、
自然周 × 策略的账户 PnL、全部策略的汇总表、收益归因——外加研究检验和运行记录。
`paper_view` 从 `sel-*` 模拟组合的账本（equity/positions/trades/orders/mtm）构建，
`backtest_view` 从 `backtest_*` 表构建。两者**不共享任何数字**：不拼接净值、不合计
盈亏，模式切换是换一份文档，不是换一列。

模拟运行内部还有第二道隔离。同一个 `sel-` 账户里既有按时跑出的仓位，也有 2026-09-04
事后补跑的五期。补跑的仓位在账本上是「09-04 一天内买入并按到期平掉」的，把它们和
按时仓位放在一条净值里，曲线上看不出哪一段是真的当期决策。所以 `subset="live"` 不是
「把补跑的行藏起来」——账户的现金和利息是连续的，藏几行对不上账——而是**按明确口径
重建**：从资本起步，只重放该子集仓位的成交现金流，现金利息按重建后的现金余额、用
账本里当天记录的同一利率重算。`subset="backfill"` 是对称视图，`subset="all"` 是库里
原样的账户。三者各自独立，互不包含对方的仓位。

周 PnL 的分解是对账，不是展示：realized + unrealized_chg + cash_income + fees + flows
必须等于 equity_end − equity_start，差额写进 `residual`，`reconciled` 只在容差内为真。
一条对不上的周，是账本出了问题的信号，不是四舍五入。

「绿着的失败」是本仓最常见的缺陷形状，这里的规则：没有数据的策略给 `status`
和 `reason`，不给一条零收益的直线；缺席的策略只写记录里有的原因，没记录就 null。
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from . import config, db, ideas as ideas_mod, paper, perf
from . import strategy as strat

MODES = {"paper": "模拟运行", "backtest": "历史回测"}
SUBSETS = ("live", "backfill", "all")
SUBSET_LABEL = {"live": "按时运行", "backfill": "事后补跑", "all": "全部"}

PAPER_METHODOLOGY = "paper-rules"
FORMAL_METHODOLOGY = "formal-paper-rules"
STUDY_METHODOLOGY = "stock-picking-study-30d"

#: How a `backtest_runs.methodology` string maps onto the two labels the page
#: knows. Anything not the formal engine is the 30-day holding study: it enters
#: unconditionally at the first close, holds a fixed window, has no stops and no
#: cash — a check on whether the picks carry information, not an account.
METHODOLOGY_MAP = {
    "real-pool-asof-replay/v1": STUDY_METHODOLOGY,
    "mechanical-asof-replay/v1": STUDY_METHODOLOGY,
    FORMAL_METHODOLOGY: FORMAL_METHODOLOGY,
}

#: Tolerance for the weekly reconciliation, in account currency. The ledger is
#: float arithmetic over ~10M of capital; a residual under a few cents is
#: rounding, anything above it is a missing row.
RECON_TOL = 0.05

_RATE_RE = re.compile(r"MM yield ([\d.]+)%")
_EXCLUDED_RE = re.compile(r"未参与：([^。]*)")


# ---------------------------------------------------------------- helpers
def _f(v: Any, nd: int = 6) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def _strategy_meta() -> dict[str, dict[str, Any]]:
    """Display name and role per selector, from the registry the run used."""
    out = {}
    for s in strat.available("idea_selector"):
        out[s["name"]] = {"name": s.get("label") or s["name"],
                          "role": s.get("role") or "?",
                          "needs_model": bool(s.get("needs_model"))}
    return out


def iso_week(d: str) -> tuple[str, str, str]:
    """(`YYYY-Www`, Monday, Sunday) of the ISO week holding `d`."""
    dd = date.fromisoformat(d)
    y, w, wd = dd.isocalendar()
    mon = dd - timedelta(days=wd - 1)
    return f"{y}-W{w:02d}", mon.isoformat(), (mon + timedelta(days=6)).isoformat()


def _spy_closes(con, start: str | None, end: str | None) -> list[dict[str, Any]]:
    if not start or not end:
        return []
    return [{"d": r["d"], "v": float(r["close"])} for r in db.q(
        con, "SELECT d, close FROM prices WHERE code=? AND d>=? AND d<=? ORDER BY d",
        (config.BENCHMARKS["SPY"], start, end))]


def _ret_between(curve: list[dict[str, Any]], a: str, b: str) -> float | None:
    """Percent return of a curve between two dates, both of which must exist.

    Interpolating a missing date would compare a book over one window with a
    benchmark over another, which is the first way an excess return lies.
    """
    by = {p["d"]: p["v"] for p in curve}
    if a not in by or b not in by or not by[a]:
        return None
    return (by[b] / by[a] - 1.0) * 100.0


def _max_dd_pct(curve: list[dict[str, Any]]) -> float | None:
    if len(curve) < 2:
        return None
    eps = perf.drawdowns([p["d"] for p in curve], [p["v"] for p in curve], top=1)
    return _f(eps[0].depth * 100.0, 4) if eps else 0.0


# ---------------------------------------------------------------- run records
def run_classes(con) -> dict[str, str]:
    """`run_id -> live | backfill`, from the one place the word is defined."""
    return {r["run_id"]: (r["data_classification"] or "live")
            for r in db.q(con, "SELECT run_id, data_classification FROM orch_runs "
                               "WHERE kind='weekly'")}


def period_classes(con) -> dict[str, str]:
    """`as_of -> live | backfill` for periods with a successful weekly run."""
    out = {}
    for r in db.q(con, "SELECT as_of, data_classification FROM orch_runs "
                       "WHERE kind='weekly' AND ok=1 ORDER BY as_of, ended_at"):
        out[r["as_of"]] = r["data_classification"] or "live"
    return out


def stuck_batches(con) -> list[dict[str, Any]]:
    """Batches whose verdict was made but never became positions.

    Same algorithm as the state document's `stuck_batches` (review.state), kept
    here so the performance page can relate them to the periods they blank out.
    Backtest batches are excluded: a replay's own drafts are its own record.
    """
    out = []
    for r in db.q(con, "SELECT batch_id, as_of, n_ideas, status, validation, generator "
                       "FROM batches WHERE status NOT IN ('traded','superseded') "
                       "AND batch_id NOT LIKE 'BT-%' "
                       "ORDER BY as_of, batch_id"):
        if str(r["generator"] or "").startswith("backtest:"):
            continue
        val = db.jl(r["validation"], {}) or {}
        out.append({
            "batch_id": r["batch_id"], "as_of": r["as_of"],
            "n_ideas": r["n_ideas"], "status": r["status"],
            "blocked_by": sorted({c.get("check") for c in val.get("checks", [])
                                  if not c.get("ok") and c.get("severity") == "error"})})
    return out


def records(con, *, subset: str | None = None) -> dict[str, Any]:
    """Failed runs, gaps, backfills and stuck batches, with the ranges they blank."""
    ok_by_asof = {r["as_of"] for r in db.q(
        con, "SELECT DISTINCT as_of FROM orch_runs WHERE kind='weekly' AND ok=1")}
    failed = []
    for r in db.q(con, "SELECT run_id, as_of, error FROM orch_runs "
                       "WHERE kind='weekly' AND ok=0 ORDER BY as_of, started_at"):
        failed.append({"as_of": r["as_of"], "run_id": r["run_id"],
                       "error": r["error"],
                       # A later successful run for the same period makes the
                       # failure history, not a hole. The row stays; the page
                       # must not count it as a missing period.
                       "resolved": r["as_of"] in ok_by_asof})
    gaps = [r["as_of"] for r in db.q(
        con, "SELECT as_of FROM orch_runs g WHERE run_id LIKE 'gap-%' "
             "AND NOT EXISTS (SELECT 1 FROM orch_runs w WHERE w.kind='weekly' "
             "  AND w.ok=1 AND w.as_of=g.as_of) ORDER BY as_of")]
    classes = period_classes(con)
    backfill = sorted(a for a, c in classes.items() if c == "backfill")
    stuck = stuck_batches(con)

    def _range(as_of: str, why: str) -> dict[str, str]:
        end = ideas_mod.horizon_end(date.fromisoformat(as_of), 1).isoformat()
        return {"start": as_of, "end": end, "why": why}

    ranges = [_range(a, "该期永久缺失：没有这一期的仓位，净值里这一段只有此前各期的持仓")
              for a in gaps]
    ranges += [_range(f["as_of"], f"周跑失败（{f['run_id']}）且没有后续成功运行")
               for f in failed if not f["resolved"]]
    ranges += [_range(s["as_of"], f"{s['batch_id']} 校验未过、从未建仓"
                                  f"（{'、'.join(s['blocked_by']) or '未记录'}）")
               for s in stuck]
    if subset == "live":
        ranges += [_range(a, "事后补跑期，不在「按时运行」子集内") for a in backfill]
    ranges.sort(key=lambda r: (r["start"], r["why"]))
    return {"failed_runs": failed, "gaps": gaps, "backfill_periods": backfill,
            "stuck_batches": stuck, "affected_ranges": ranges,
            "period_classification": classes}


# ---------------------------------------------------------------- ledgers
def _position_classes(con, book_id: str) -> dict[str, str]:
    """`pos_id -> live | backfill | unknown` through 持仓→想法→批次→周跑."""
    classes = run_classes(con)
    out = {}
    for r in db.q(con, "SELECT p.pos_id, b.generator FROM positions p "
                       "JOIN ideas i ON i.idea_uid = p.idea_uid "
                       "JOIN batches b ON b.batch_id = i.batch_id WHERE p.book_id=?",
                  (book_id,)):
        gen = str(r["generator"] or "")
        if gen.startswith("weekly:"):
            out[r["pos_id"]] = classes.get(gen.split(":", 1)[1], "unknown")
        elif gen.startswith("backtest:"):
            # A backtest batch replays a period; inside its own book it is the
            # whole account, so it reads as the period's own class.
            run_id = gen.split(":")[-1]
            out[r["pos_id"]] = classes.get(run_id, "unknown")
        else:
            out[r["pos_id"]] = "unknown"
    return out


def _rate_on(con, book_id: str, d: str) -> float | None:
    """The money-market rate the book itself booked on `d`, or None if it did
    not accrue that day. Parsed from the INT trade rather than recomputed so the
    reconstruction uses the rate the account actually saw, shelf or fallback."""
    r = db.q1(con, "SELECT reason FROM trades WHERE book_id=? AND side='INT' AND d=?",
              (book_id, d))
    if not r:
        return None
    m = _RATE_RE.search(r["reason"] or "")
    if not m:
        return config.RISK_FREE_ANNUAL
    return float(m.group(1)) / 100.0


def book_ledger(con, book_id: str, subset: str = "all") -> dict[str, Any]:
    """One book's account, on one subset of its positions.

    Returns the daily curve (`points`: d, cash, mv, equity, u_gross), the subset
    positions with their trade fees, the interest series, and enough of the
    blotter to decompose any window. `subset="all"` reads the ledger as stored;
    the other two rebuild it from capital by replaying only that subset's cash
    flows and re-accruing interest on the rebuilt balance.
    """
    book = db.q1(con, "SELECT book_id, label, capital FROM books WHERE book_id=?",
                 (book_id,))
    if not book:
        raise KeyError(book_id)
    capital = float(book["capital"])
    classes = _position_classes(con, book_id)
    pos_rows = [dict(r) for r in db.q(
        con, "SELECT * FROM positions WHERE book_id=? ORDER BY opened_d, pos_id",
        (book_id,))]
    if subset != "all":
        pos_rows = [p for p in pos_rows if classes.get(p["pos_id"]) == subset]
    pos_ids = {p["pos_id"] for p in pos_rows}
    for p in pos_rows:
        p["class"] = classes.get(p["pos_id"], "unknown")

    # Per-position fees, entry and exit separately: the identity in the weekly
    # decomposition needs unrealised P&L gross of the entry fee, which `cost`
    # already contains.
    fee_in: dict[str, float] = defaultdict(float)
    fee_out: dict[str, float] = defaultdict(float)
    trades_by_d: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in db.q(con, "SELECT * FROM trades WHERE book_id=? AND side IN ('BUY','SELL') "
                       "ORDER BY d", (book_id,)):
        if t["pos_id"] not in pos_ids:
            continue
        row = dict(t)
        trades_by_d[row["d"]].append(row)
        (fee_in if row["side"] == "BUY" else fee_out)[row["pos_id"]] += float(row["fee"] or 0)
    for p in pos_rows:
        p["fee_in"] = fee_in.get(p["pos_id"], 0.0)
        p["fee_out"] = fee_out.get(p["pos_id"], 0.0)
        p["gross_cost"] = float(p["cost"]) - p["fee_in"]
    gross_cost = {p["pos_id"]: p["gross_cost"] for p in pos_rows}

    # mv and gross unrealised per day, over the subset only.
    mv_by_d: dict[str, float] = defaultdict(float)
    ug_by_d: dict[str, float] = defaultdict(float)
    upnl_last: dict[str, tuple[str, float]] = {}
    for m in db.q(con, "SELECT pos_id, d, mv, upnl FROM mtm WHERE book_id=? ORDER BY d",
                  (book_id,)):
        if m["pos_id"] not in pos_ids:
            continue
        mv_by_d[m["d"]] += float(m["mv"] or 0)
        ug_by_d[m["d"]] += float(m["mv"] or 0) - gross_cost[m["pos_id"]]
        upnl_last[m["pos_id"]] = (m["d"], float(m["upnl"] or 0))

    eq_rows = [dict(r) for r in db.q(
        con, "SELECT d, cash, mv, equity FROM equity WHERE book_id=? ORDER BY d",
        (book_id,))]
    dates = [r["d"] for r in eq_rows]
    interest: dict[str, float] = {}
    points: list[dict[str, Any]] = []
    if subset == "all":
        for r in db.q(con, "SELECT d, cash_delta FROM trades WHERE book_id=? AND side='INT'",
                      (book_id,)):
            interest[r["d"]] = interest.get(r["d"], 0.0) + float(r["cash_delta"] or 0)
        for r in eq_rows:
            points.append({"d": r["d"], "cash": float(r["cash"] or 0),
                           "mv": float(r["mv"] or 0), "equity": float(r["equity"] or 0),
                           "u_gross": ug_by_d.get(r["d"], 0.0)})
    else:
        # Replay. Interest accrues on the sessions the engine itself accrued on
        # (an INT row exists), at the rate it booked, on the *rebuilt* balance
        # as of the previous session — `paper._accrue_cash` verbatim, minus the
        # other subset's cash flows.
        cash = capital
        cash_at: dict[str, float] = {}
        for d in dates:
            prev = paper._prev_session(con, d)
            rate = _rate_on(con, book_id, d)
            inc = 0.0
            if rate is not None and prev:
                base = cash_at.get(prev, cash)
                if base > 0:
                    days = max((date.fromisoformat(d) - date.fromisoformat(prev)).days, 1)
                    inc = base * rate * days / 365.0
            cash += inc
            cash += sum(float(t["cash_delta"] or 0) for t in trades_by_d.get(d, ()))
            cash_at[d] = cash
            if inc:
                interest[d] = inc
            mv = mv_by_d.get(d, 0.0)
            points.append({"d": d, "cash": cash, "mv": mv, "equity": cash + mv,
                           "u_gross": ug_by_d.get(d, 0.0)})

    return {"book_id": book_id, "label": book["label"], "capital": capital,
            "subset": subset, "points": points, "positions": pos_rows,
            "trades_by_d": dict(trades_by_d), "interest": interest,
            "upnl_last": upnl_last,
            "n_total_positions": len(classes),
            "class_counts": {c: sum(1 for v in classes.values() if v == c)
                             for c in ("live", "backfill", "unknown")}}


def weekly_pnl(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """ISO-week buckets of one ledger, each decomposed and reconciled.

    Identity, per week, with every term gross of fees and the fees on their own
    line so nothing is counted twice:

        equity_end − equity_start
          = realized + unrealized_chg + cash_income + fees + flows

    `realized` is proceeds minus gross cost for positions closed in the week,
    `unrealized_chg` the change in (mark − gross cost) over positions open at
    each end, `fees` the (negative) sum of entry and exit fees paid in the week,
    `cash_income` the money-market interest booked, `flows` external cash (none
    today; the line exists so a deposit cannot masquerade as return).
    """
    pts = ledger["points"]
    if not pts:
        return []
    capital = ledger["capital"]
    weeks: dict[str, dict[str, Any]] = {}
    for i, p in enumerate(pts):
        wk, start, end = iso_week(p["d"])
        w = weeks.get(wk)
        if not w:
            prev = pts[i - 1] if i else None
            w = weeks[wk] = {
                "week": wk, "start": start, "end": end,
                "equity_start": prev["equity"] if prev else capital,
                "u_start": prev["u_gross"] if prev else 0.0,
                "first_d": p["d"], "days": []}
        w["days"].append(p["d"])
        w["equity_end"] = p["equity"]
        w["u_end"] = p["u_gross"]
        w["last_d"] = p["d"]
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        days = set(w["days"])
        realized = fees = 0.0
        for p in ledger["positions"]:
            if p.get("closed_d") in days:
                realized += float(p["realized"] or 0) + p["fee_in"] + p["fee_out"]
        for d in days:
            for t in ledger["trades_by_d"].get(d, ()):
                fees -= float(t["fee"] or 0)
        cash_income = sum(v for d, v in ledger["interest"].items() if d in days)
        unreal = w["u_end"] - w["u_start"]
        pnl = w["equity_end"] - w["equity_start"]
        flows = 0.0
        residual = pnl - (realized + unreal + cash_income + fees + flows)
        out.append({
            "week": wk, "start": w["start"], "end": w["end"],
            "first_d": w["first_d"], "last_d": w["last_d"],
            "equity_start": _f(w["equity_start"], 2), "equity_end": _f(w["equity_end"], 2),
            "pnl_amt": _f(pnl, 2),
            "pnl_pct": _f(pnl / w["equity_start"] * 100.0, 4) if w["equity_start"] else None,
            "realized": _f(realized, 2), "unrealized_chg": _f(unreal, 2),
            "cash_income": _f(cash_income, 2), "fees": _f(fees, 2), "flows": flows,
            "reconciled": abs(residual) <= RECON_TOL, "residual": _f(residual, 4),
        })
    return out


# ---------------------------------------------------------------- attribution
def _sources_of(con, idea: dict[str, Any], cand_cache: dict) -> tuple[list[str], list[str]]:
    """(topics, methods) an idea came from, following stored ids only.

    Topics come from the candidate payload's `topics` list when the idea points
    at a weekly-run candidate; that is where a multi-topic proposal keeps its
    second and third topic — `ideas.theme_id` holds one. Methods come from the
    `sources[].methods` list the booking step wrote. Nothing is inferred from
    names; an idea with no walkable link is 「来源未记录」 rather than guessed.
    """
    topics: list[str] = []
    methods: list[str] = []
    for s in db.jl(idea.get("sources"), []) or []:
        if not isinstance(s, dict):
            continue
        for m in s.get("methods") or []:
            if m and m not in methods:
                methods.append(str(m))
        rid, cid = s.get("run_id"), s.get("candidate_id")
        if rid and cid:
            key = (rid, str(cid))
            if key not in cand_cache:
                r = db.q1(con, "SELECT payload FROM candidates WHERE run_id=? "
                               "AND candidate_id=?", (rid, str(cid)))
                cand_cache[key] = db.jl(r["payload"], {}) if r else {}
            pl = cand_cache[key] or {}
            for t in pl.get("topics") or ([pl["topic_id"]] if pl.get("topic_id") else []):
                if t and t not in topics:
                    topics.append(str(t))
            for m in pl.get("proposed_by") or []:
                if m and m not in methods:
                    methods.append(str(m))
    if not topics and idea.get("theme_id"):
        topics = [str(idea["theme_id"])]
    if not methods and idea.get("role"):
        methods = [str(idea["role"])]
    return topics or ["来源未记录"], methods or ["来源未记录"]


ATTRIBUTION_RULE = (
    "每个仓位的损益 = 已平仓的 realized（扣费后）+ 未平仓的最新浮动盈亏；不含现金利息。"
    "full_credit：一个标的的全部损益完整计入它的每一个来源——多主题/多方法共同提出的"
    "标的会在每个来源下各出现一次，各行**不可相加**，是观察视图。"
    "split_equal：按来源数均分，各行可相加，合计 = 该子集的总交易损益。"
    "来源关系沿 持仓→想法→候选 的存储 id 追溯，追不到的记「来源未记录」。")


def attribution(con, positions: list[dict[str, Any]],
                upnl_last: dict[str, tuple[str, float]]) -> dict[str, Any]:
    cand_cache: dict = {}
    by_topic: dict[str, dict[str, float]] = defaultdict(lambda: {"full": 0.0, "split": 0.0, "n": 0})
    by_method: dict[str, dict[str, float]] = defaultdict(lambda: {"full": 0.0, "split": 0.0, "n": 0})
    by_inst: dict[str, dict[str, Any]] = {}
    total = 0.0
    for p in positions:
        idea = db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?", (p["idea_uid"],))
        idea = dict(idea) if idea else {}
        if p.get("status") == "closed":
            pnl = float(p.get("realized") or 0)
        else:
            pnl = upnl_last.get(p["pos_id"], (None, 0.0))[1]
        total += pnl
        topics, methods = _sources_of(con, idea, cand_cache)
        for t in topics:
            by_topic[t]["full"] += pnl
            by_topic[t]["split"] += pnl / len(topics)
            by_topic[t]["n"] += 1
        for m in methods:
            by_method[m]["full"] += pnl
            by_method[m]["split"] += pnl / len(methods)
            by_method[m]["n"] += 1
        code = p["code"]
        row = by_inst.setdefault(code, {"key": code, "name": idea.get("tool_desc") or code,
                                        "pnl_full_credit": 0.0, "pnl_split_equal": 0.0,
                                        "n_positions": 0, "topics": [], "methods": []})
        row["pnl_full_credit"] += pnl
        row["pnl_split_equal"] += pnl
        row["n_positions"] += 1
        for t in topics:
            if t not in row["topics"]:
                row["topics"].append(t)
        for m in methods:
            if m not in row["methods"]:
                row["methods"].append(m)

    def _rows(d: dict) -> list[dict[str, Any]]:
        return sorted(({"key": k, "name": k, "pnl_full_credit": _f(v["full"], 2),
                        "pnl_split_equal": _f(v["split"], 2), "n_positions": v["n"]}
                       for k, v in d.items()), key=lambda r: -(r["pnl_split_equal"] or 0))

    inst_rows = sorted(by_inst.values(), key=lambda r: -r["pnl_split_equal"])
    for r in inst_rows:
        r["pnl_full_credit"] = _f(r["pnl_full_credit"], 2)
        r["pnl_split_equal"] = _f(r["pnl_split_equal"], 2)
    return {"rule": ATTRIBUTION_RULE, "unit": "usd", "total_pnl": _f(total, 2),
            "by_topic": _rows(by_topic), "by_instrument": inst_rows,
            "by_method": _rows(by_method)}


# ---------------------------------------------------------------- research
def _research(curves: dict[str, list[dict[str, Any]]], spy: list[dict[str, Any]],
              rf: float, n_periods: int, n_days: int,
              positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    usable = {k: ([p["d"] for p in c], [p["v"] for p in c])
              for k, c in curves.items() if len(c) >= 4}
    note = (f"样本：{n_periods} 期出过手，{n_days} 个盯市交易日。"
            f"夏普区间、多重检验折减等按日频净值算，样本这么短时区间盖住 0 是常态，"
            f"不是结论。")
    if not usable:
        return {"sample_note": note, "n_periods": n_periods, "n_days": n_days,
                "stats": {"unavailable": "没有长度 ≥ 4 天的净值曲线"}}
    try:
        stats = perf.compare_arms(
            usable, bench_dates=[p["d"] for p in spy], bench_closes=[p["v"] for p in spy],
            benchmark="SPY", rf_annual=rf)
    except Exception as e:  # noqa: BLE001 — the study block must not take the page down
        stats = {"error": f"{type(e).__name__}: {e}"}
    return {"sample_note": note, "n_periods": n_periods, "n_days": n_days, "stats": stats}


def _cash_rate(con) -> tuple[float, str]:
    try:
        from .sources import olive as _olive
        y = _olive.cash_yield(con, "USD")
    except Exception:  # noqa: BLE001
        y = None
    if y is None:
        return config.RISK_FREE_ANNUAL, "config_fallback"
    return float(y), "shelf_median_7d"


# ---------------------------------------------------------------- roster
def _latest_backtest(con, source: str | None = None) -> dict[str, Any] | None:
    if source:
        r = db.q1(con, "SELECT * FROM backtest_runs WHERE backtest_id=?", (source,))
    else:
        r = db.q1(con, "SELECT * FROM backtest_runs WHERE ok=1 "
                       "ORDER BY as_of DESC, ended_at DESC LIMIT 1")
    if not r:
        return None
    run = dict(r)
    run["summary"] = db.jl(run.get("summary"), {}) or {}
    run["arms"] = [x["arm"] for x in db.q(
        con, "SELECT DISTINCT arm FROM backtest_points WHERE backtest_id=? ORDER BY arm",
        (run["backtest_id"],))]
    return run


def _absent_reason(run: dict[str, Any] | None, arm: str) -> str | None:
    """Why an arm is absent from a backtest — only what its record says.

    Jon's rule for the roster: 「不能填入推测理由」. Three places a record can
    carry the reason, checked in order; an arm named in none of them gets null.
    """
    if not run:
        return None
    s = run.get("summary") or {}
    reasons = s.get("excluded_reasons") or {}
    if arm in reasons:
        return str(reasons[arm])
    if arm in (s.get("skipped_need_model") or []):
        return "summary.skipped_need_model：该策略需要调用模型，复算式回测跳过"
    if arm in (s.get("excluded_arms") or []):
        m = _EXCLUDED_RE.search(str(s.get("disclaimer") or ""))
        if m and arm in m.group(1):
            return f"summary.disclaimer：未参与：{m.group(1).strip()}"
        return "summary.excluded_arms 记录了该策略被排除，但未注明原因"
    return None


# ---------------------------------------------------------------- paper view
def paper_view(con, p=None, subset: str = "live") -> dict[str, Any]:
    """The 模拟运行 side of the page, on one subset of the account."""
    if subset not in SUBSETS:
        raise ValueError(f"subset must be one of {SUBSETS}, got {subset!r}")
    meta = _strategy_meta()
    books = [r["book_id"] for r in db.q(
        con, "SELECT book_id FROM books WHERE book_id LIKE ? ORDER BY book_id",
        (config.SELECTOR_PREFIX + "%",))]
    # `sel-` and only `sel-`: a backtest book is another mode's data.
    books = [b for b in books if not config.is_backtest_book(b)]
    rf, rf_src = _cash_rate(con)

    ledgers: dict[str, dict[str, Any]] = {}
    strategies: list[dict[str, Any]] = []
    curves: dict[str, list[dict[str, Any]]] = {}
    periods_by_key: dict[str, set[str]] = {}
    for b in books:
        key = b[len(config.SELECTOR_PREFIX):]
        led = book_ledger(con, b, subset)
        ledgers[key] = led
        curve = [{"d": pt["d"], "v": _f(pt["equity"], 2)} for pt in led["points"]]
        periods = {p_["as_of"] for p_ in led["positions"] if p_.get("as_of")}
        periods_by_key[key] = periods
        m = meta.get(key, {"name": key, "role": "?"})
        if not led["points"]:
            status, reason = "缺数据", "组合存在但没有任何盯市记录"
        elif not led["positions"]:
            status = "缺数据"
            cc = led["class_counts"]
            reason = (f"该组合在「{SUBSET_LABEL[subset]}」子集里没有仓位"
                      f"（按时 {cc['live']} / 补跑 {cc['backfill']} / 未知 {cc['unknown']}）")
        else:
            status, reason = "ok", None
        available = status == "ok"
        if available:
            curves[key] = curve
        strategies.append({
            "key": key, "name": m["name"], "role": m["role"], "book_id": b,
            "available": available, "status": status, "reason": reason,
            "first_d": curve[0]["d"] if curve else None,
            "last_d": curve[-1]["d"] if curve else None,
            "n_periods": len(periods), "curve": curve if available else [],
        })
    # Selectors the registry knows but no book exists for: 未运行, and the reason
    # is the record itself (no `sel-` book), not a guess about why.
    have = {s["key"] for s in strategies}
    for name, m in sorted(meta.items()):
        if name in have:
            continue
        strategies.append({
            "key": name, "name": m["name"], "role": m["role"], "book_id": None,
            "available": False, "status": "未运行",
            "reason": f"books 表里没有 {config.selector_book(name)}：该策略未在周跑中建仓",
            "first_d": None, "last_d": None, "n_periods": 0, "curve": []})

    first_d = min((s["first_d"] for s in strategies if s["first_d"]), default=None)
    last_d = max((s["last_d"] for s in strategies if s["last_d"]), default=None)
    spy = _spy_closes(con, first_d, last_d)

    # 全量基准 = the buy_all book on the same subset. Its missing periods are
    # named against every period any other book acted in.
    all_periods = set().union(*periods_by_key.values()) if periods_by_key else set()
    ba_periods = periods_by_key.get("buy_all", set())
    ba_missing = sorted(all_periods - ba_periods)
    ba_curve = curves.get("buy_all", [])
    stuck = {s["batch_id"]: s for s in stuck_batches(con)}
    ba_reason = None
    if "buy_all" not in curves:
        ba_reason = "该子集里没有 buy_all 组合的仓位，无法作全量基准"
    elif ba_missing:
        why = []
        for a in ba_missing:
            bid = f"W{a.replace('-', '')}-buy_all"
            if bid in stuck:
                why.append(f"{a}（{bid} 校验未过：{'、'.join(stuck[bid]['blocked_by'])}）")
            else:
                why.append(a)
        ba_reason = ("全量基准缺 " + "、".join(why)
                     + "；这些期里出过手的组合，在那一段不是在和它比同一段时间")

    # 汇总表 — every strategy, same window rule for every benchmark comparison.
    rows = []
    for s in strategies:
        key = s["key"]
        row = {"key": key, "name": s["name"], "role": s["role"], "status": s["status"],
               "reason": s["reason"], "cum_ret_pct": None, "excess_spy_pp": None,
               "excess_buy_all_pp": None, "max_dd_pct": None,
               "cash_share_end_pct": None, "cash_share_avg_pct": None,
               "n_periods": s["n_periods"], "n_days": 0}
        if s["available"]:
            led = ledgers[key]
            pts = led["points"]
            cum = (pts[-1]["equity"] / led["capital"] - 1.0) * 100.0
            row["cum_ret_pct"] = _f(cum, 4)
            spy_ret = _ret_between(spy, pts[0]["d"], pts[-1]["d"])
            row["excess_spy_pp"] = _f(cum - spy_ret, 4) if spy_ret is not None else None
            if spy_ret is None:
                row["reason"] = "SPY 在该组合的起止日缺价，超额收益不算"
            if key != "buy_all":
                ba_ret = _ret_between(ba_curve, pts[0]["d"], pts[-1]["d"])
                row["excess_buy_all_pp"] = (_f(cum - ba_ret, 4)
                                            if ba_ret is not None else None)
            row["max_dd_pct"] = _max_dd_pct(s["curve"])
            shares = [pt["cash"] / pt["equity"] for pt in pts if pt["equity"]]
            row["cash_share_end_pct"] = _f(shares[-1] * 100.0, 4) if shares else None
            row["cash_share_avg_pct"] = (_f(sum(shares) / len(shares) * 100.0, 4)
                                         if shares else None)
            # Days the book was marked; the seed row (capital, day before the
            # first order) is not a trading day.
            row["n_days"] = max(len(pts) - 1, 0)
        rows.append(row)

    bt = _latest_backtest(con)
    bt_arms = set(bt["arms"]) if bt else set()
    paper_keys = {s["key"] for s in strategies if s["book_id"]}
    roster = {"other_mode": "backtest",
              "other_source_id": bt["backtest_id"] if bt else None,
              "added": sorted(bt_arms - paper_keys),
              "absent": [{"key": k, "reason": _absent_reason(bt, k)}
                         for k in sorted(paper_keys - bt_arms)]}

    # 周 PnL
    weeks_seen: dict[str, dict[str, str]] = {}
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for key, led in ledgers.items():
        if not led["positions"]:
            continue
        wp = weekly_pnl(led)
        by_strategy[key] = wp
        for w in wp:
            weeks_seen.setdefault(w["week"], {"week": w["week"], "start": w["start"],
                                              "end": w["end"]})
    weeks = [weeks_seen[k] for k in sorted(weeks_seen)]
    n_unrec = sum(1 for wp in by_strategy.values() for w in wp if not w["reconciled"])

    # 归因 — over the subset's positions, every book together.
    all_pos = [p_ for led in ledgers.values() for p_ in led["positions"]]
    upnl = {}
    for led in ledgers.values():
        upnl.update(led["upnl_last"])
    attr = attribution(con, all_pos, upnl)

    n_periods = len(all_periods)
    n_days = max((r["n_days"] for r in rows), default=0)
    research = _research(curves, spy, rf, n_periods, n_days)

    disclosures = [
        f"现金利率 {rf*100:.3f}%（{'货架 7 日年化中位数' if rf_src == 'shelf_median_7d' else 'config.RISK_FREE_ANNUAL 兜底常数'}）"
        f"，仅用于研究检验的无风险利率；账上的利息按每天记录的利率计。",
        "周 PnL 分解口径：realized 与 unrealized_chg 均为扣费前，费用单列为负数，"
        "五项之和须等于周内净值变化；residual 是差额，reconciled 只在 ±0.05 内为真。",
        "全量基准 = sel-buy_all 组合本身（同一子集、同一账规则），不是另算的指数。",
        "超额收益：策略与基准取同一起止日的累计收益之差（百分点）；起止日在基准里缺价则不算。",
        "现金占比同时给期末与期间平均；表头主用期末（cash_share_basis）。",
    ]
    if subset == "all":
        disclosures.insert(0, (
            "子集「全部」：库里原样的账户。2026-09-04 事后补跑的五期与 08-26 按时期混在同一"
            "条净值里；补跑仓位是 09-04 当天买入、当天按到期平掉的。"))
    else:
        disclosures.insert(0, (
            f"子集「{SUBSET_LABEL[subset]}」按明确口径重建，不是从账户里删几行："
            f"从资本 {config.SELECTOR_SPEC['capital']:,.0f} 起步，只重放该子集仓位的"
            f"成交与平仓现金流；现金利息在账上记过 INT 的每个交易日、按当天记录的同一"
            f"利率、对重建后的前一交易日现金余额重算（paper._accrue_cash 同一公式），"
            f"所以 cash_income 与账上原 INT 不相等是预期的。仓位的分类走 持仓→想法→批次→"
            f"orch_runs.data_classification，NULL 记为按时。"))
    if n_unrec:
        disclosures.append(f"⚠ 有 {n_unrec} 个策略-周没有对上账（residual 超过容差），见 weekly_pnl。")
    overdrawn = [r["key"] for r in rows
                 if r["cash_share_end_pct"] is not None and r["cash_share_end_pct"] < 0]
    if overdrawn:
        # A negative cash balance is the ledger saying the book bought more than
        # it had. On the stored account it comes from the 09-04 backfill: five
        # batches opened on one day were each sized against the same cash
        # figure. It is reported, not smoothed over.
        disclosures.append(
            f"⚠ 期末现金为负（账户透支）：{'、'.join(overdrawn)}。同一天集中建仓的批次"
            f"各自按同一现金余额定额，合计超过了账户现金；这是账上原样，不是口径问题。")

    return {
        "mode": "paper", "label": MODES["paper"], "subset": subset,
        "subset_label": SUBSET_LABEL[subset],
        "methodology": PAPER_METHODOLOGY, "source_id": None,
        "window": ({"start": first_d, "end": last_d} if first_d and last_d else None),
        "capital": config.SELECTOR_SPEC["capital"],
        "generated_at": config.now_hkt().isoformat(),
        "strategies": strategies,
        "benchmarks": {
            "spy": {"name": "SPY", "code": config.BENCHMARKS["SPY"], "curve": spy},
            "buy_all": {"name": "全量基准", "curve": ba_curve,
                        "available": "buy_all" in curves and not ba_missing,
                        "reason": ba_reason, "missing_periods": ba_missing}},
        "weekly_pnl": {"weeks": weeks, "by_strategy": by_strategy,
                       "note": ("自然周（ISO）分桶；每周 equity_start = 上一盯市日净值"
                                "（首周为资本），equity_end = 周内最后盯市日净值。"
                                "分解按仓位的实际成交与逐日 mtm 算，不含任何「某批想法未来"
                                "一个月的回报」。")},
        "summary": {"as_of": last_d, "cash_share_basis": "end", "rows": rows,
                    "roster_diff": roster},
        "attribution": attr,
        "research": research,
        "records": records(con, subset=subset),
        "disclosures": disclosures,
    }


# ---------------------------------------------------------------- backtest view
STUDY_DISCLOSURES = [
    "这是「选股能力研究」（stock-picking-study-30d），不是账户业绩：它检验候选与排序有没有"
    "信息，不能混充完整系统的账户表现。",
    "成交：每个标的在该期日期当日或之后的第一个收盘无条件成交（backtest.outcome_for），"
    "没有挂单区间、没有 5 个交易日的订单寿命、不会「未成交」。",
    "持有：固定 30 个日历日，退出价取该期日期+30 天当日或之前的最后一个收盘；没有止损、"
    "没有止盈、没有事件退出。",
    "成本：只扣一次往返成本（ideas.round_trip_cost_pct，美股 ETF 合计 0.08%），"
    "与模拟运行的「两腿各半」在数值上等价但没有滑点作用在成交价上。",
    "仓位：每期一个 tranche，占 1/4 资本，期内等权；四个 tranche 轮转"
    "（backtest.tranche_curve）。未部署的现金按 0% 计，**不计货币基金利息**。",
    "净值以 100 为起点的指数，不是货币金额；没有现金账、没有费用明细，"
    "所以周 PnL 只有净值变化，分解项填 0。",
    "没有日线的标的（基金 / 结构化产品）从盯市里剔除并记录，不按 0% 计。",
    "补跑期的候选池是事后用当日冻结的研报生成的：文档层 as-of 干净，但模型权重见过之后的世界。",
]


def _bt_positions_sources(con, period: str, instrument_id: str,
                          cache: dict) -> tuple[list[str], list[str]]:
    """Topics / methods of a backtest position, via the period's candidate row."""
    key = (period, instrument_id)
    if key in cache:
        return cache[key]
    r = db.q1(con, "SELECT c.payload FROM candidates c "
                   "JOIN orch_runs o ON o.run_id = c.run_id "
                   "WHERE c.as_of=? AND c.instrument_id=? "
                   "ORDER BY o.ok DESC, o.ended_at DESC LIMIT 1", (period, instrument_id))
    pl = db.jl(r["payload"], {}) if r else {}
    topics = [str(t) for t in (pl.get("topics") or ([pl["topic_id"]] if pl.get("topic_id") else []))]
    methods = [str(m) for m in (pl.get("proposed_by") or ([pl["method"]] if pl.get("method") else []))]
    out = (topics or ["来源未记录"], methods or ["来源未记录"])
    cache[key] = out
    return out


def _study_attribution(con, positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Attribution when the only record is `backtest_positions` (return_pct per
    pick, equal weight). Unit is percentage points summed over picks, not money."""
    cache: dict = {}
    by_topic: dict[str, dict] = defaultdict(lambda: {"full": 0.0, "split": 0.0, "n": 0})
    by_method: dict[str, dict] = defaultdict(lambda: {"full": 0.0, "split": 0.0, "n": 0})
    by_inst: dict[str, dict[str, Any]] = {}
    total = 0.0
    for r in positions:
        if r.get("return_pct") is None:
            continue
        pnl = float(r["return_pct"])
        total += pnl
        topics, methods = _bt_positions_sources(con, r["period"], r["instrument_id"], cache)
        for t in topics:
            by_topic[t]["full"] += pnl; by_topic[t]["split"] += pnl / len(topics); by_topic[t]["n"] += 1
        for m in methods:
            by_method[m]["full"] += pnl; by_method[m]["split"] += pnl / len(methods); by_method[m]["n"] += 1
        row = by_inst.setdefault(r["instrument_id"], {
            "key": r["instrument_id"], "name": r["instrument_id"], "pnl_full_credit": 0.0,
            "pnl_split_equal": 0.0, "n_positions": 0, "topics": [], "methods": []})
        row["pnl_full_credit"] += pnl; row["pnl_split_equal"] += pnl; row["n_positions"] += 1
        for t in topics:
            if t not in row["topics"]:
                row["topics"].append(t)
        for m in methods:
            if m not in row["methods"]:
                row["methods"].append(m)

    def _rows(d):
        return sorted(({"key": k, "name": k, "pnl_full_credit": _f(v["full"], 4),
                        "pnl_split_equal": _f(v["split"], 4), "n_positions": v["n"]}
                       for k, v in d.items()), key=lambda x: -(x["pnl_split_equal"] or 0))
    inst_rows = sorted(by_inst.values(), key=lambda x: -x["pnl_split_equal"])
    for x in inst_rows:
        x["pnl_full_credit"] = _f(x["pnl_full_credit"], 4)
        x["pnl_split_equal"] = _f(x["pnl_split_equal"], 4)
    return {"rule": ATTRIBUTION_RULE + " 本视图单位为**百分点之和**（每个标的×期的等权"
                    "持有期收益，pct），不是货币金额——选股研究没有资金账。",
            "unit": "pct_points", "total_pnl": _f(total, 4),
            "by_topic": _rows(by_topic), "by_instrument": inst_rows,
            "by_method": _rows(by_method)}


def backtest_view(con, p=None, source: str | None = None) -> dict[str, Any]:
    """The 历史回测 side of the page, from `backtest_runs/points/positions` only."""
    meta = _strategy_meta()
    run = _latest_backtest(con, source)
    gen = config.now_hkt().isoformat()
    if not run:
        return {
            "mode": "backtest", "label": MODES["backtest"], "subset": "all",
            "methodology": None, "source_id": source, "window": None, "capital": None,
            "generated_at": gen,
            "strategies": [{"key": n, "name": m["name"], "role": m["role"],
                            "available": False, "status": "未运行",
                            "reason": ("没有这个 backtest_id" if source
                                       else "backtest_runs 里没有成功的回测记录"),
                            "first_d": None, "last_d": None, "n_periods": 0, "curve": []}
                           for n, m in sorted(meta.items())],
            "benchmarks": {"spy": {"name": "SPY", "curve": []},
                           "buy_all": {"name": "全量基准", "curve": [], "available": False,
                                       "reason": "没有回测记录"}},
            "weekly_pnl": {"weeks": [], "by_strategy": {}, "note": "没有回测记录"},
            "summary": {"as_of": None, "cash_share_basis": "end", "rows": [],
                        "roster_diff": {"other_mode": "paper", "added": [], "absent": []}},
            "attribution": {"rule": ATTRIBUTION_RULE, "by_topic": [], "by_instrument": [],
                            "by_method": []},
            "research": {"sample_note": "没有回测记录", "n_periods": 0, "n_days": 0, "stats": {}},
            "records": records(con), "disclosures": ["没有回测记录。"],
        }

    bid = run["backtest_id"]
    summary = run["summary"]
    methodology = METHODOLOGY_MAP.get(run["methodology"], STUDY_METHODOLOGY)
    formal = methodology == FORMAL_METHODOLOGY
    rf, _ = _cash_rate(con)

    points: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in db.q(con, "SELECT arm, d, equity, n_positions FROM backtest_points "
                       "WHERE backtest_id=? ORDER BY arm, d", (bid,)):
        points[r["arm"]].append({"d": r["d"], "v": _f(r["equity"], 6),
                                 "n_positions": r["n_positions"]})
    positions = [dict(r) for r in db.q(
        con, "SELECT * FROM backtest_positions WHERE backtest_id=? ORDER BY period, arm",
        (bid,))]
    periods_by_arm: dict[str, set[str]] = defaultdict(set)
    for r in positions:
        periods_by_arm[r["arm"]].add(r["period"])

    # Formal runs keep their `bt:` books, which carry the full ledger; when they
    # are still there the decomposition and cash share come from the same code
    # path the paper side uses. When they were cleaned up, the points table is
    # what is left and the view says so.
    ledgers: dict[str, dict[str, Any]] = {}
    if formal:
        for arm in points:
            b = config.backtest_book(bid, arm)
            if db.q1(con, "SELECT 1 FROM books WHERE book_id=?", (b,)):
                try:
                    ledgers[arm] = book_ledger(con, b, "all")
                except KeyError:
                    pass

    arm_summary = summary.get("arms") or {}
    strategies = []
    curves: dict[str, list[dict[str, Any]]] = {}
    for arm in sorted(points):
        c = [{"d": q["d"], "v": q["v"]} for q in points[arm]]
        m = meta.get(arm, {"name": arm, "role": "?"})
        errs = (arm_summary.get(arm) or {}).get("errors") or {}
        status = "ok" if len(c) >= 2 else "缺数据"
        reason = None if status == "ok" else "净值点不足两个"
        if errs and status == "ok":
            reason = f"{len(errs)} 期建仓失败（见 summary.arms.{arm}.errors）"
        curves[arm] = c
        strategies.append({"key": arm, "name": m["name"], "role": m["role"],
                           "available": status == "ok", "status": status, "reason": reason,
                           "first_d": c[0]["d"] if c else None,
                           "last_d": c[-1]["d"] if c else None,
                           "n_periods": len(periods_by_arm.get(arm, ())), "curve": c})
    have = set(points)
    for name, m in sorted(meta.items()):
        if name in have:
            continue
        strategies.append({"key": name, "name": m["name"], "role": m["role"],
                           "available": False, "status": "未运行",
                           "reason": _absent_reason(run, name),
                           "first_d": None, "last_d": None, "n_periods": 0, "curve": []})

    first_d = min((s["first_d"] for s in strategies if s["first_d"]), default=None)
    last_d = max((s["last_d"] for s in strategies if s["last_d"]), default=None)
    spy = _spy_closes(con, first_d, last_d)
    ba_curve = curves.get("buy_all", [])
    capital = summary.get("capital") if formal else None

    rows = []
    for s in strategies:
        arm = s["key"]
        row = {"key": arm, "name": s["name"], "role": s["role"], "status": s["status"],
               "reason": s["reason"], "cum_ret_pct": None, "excess_spy_pp": None,
               "excess_buy_all_pp": None, "max_dd_pct": None,
               "cash_share_end_pct": None, "cash_share_avg_pct": None,
               "n_periods": s["n_periods"], "n_days": 0}
        if s["available"]:
            c = s["curve"]
            cum = (c[-1]["v"] / c[0]["v"] - 1.0) * 100.0 if c[0]["v"] else None
            row["cum_ret_pct"] = _f(cum, 4)
            spy_ret = _ret_between(spy, c[0]["d"], c[-1]["d"])
            row["excess_spy_pp"] = (_f(cum - spy_ret, 4)
                                    if (spy_ret is not None and cum is not None) else None)
            if arm != "buy_all":
                ba_ret = _ret_between(ba_curve, c[0]["d"], c[-1]["d"])
                row["excess_buy_all_pp"] = (_f(cum - ba_ret, 4)
                                            if (ba_ret is not None and cum is not None) else None)
            row["max_dd_pct"] = _max_dd_pct(c)
            row["n_days"] = max(len(c) - 1, 0)
            led = ledgers.get(arm)
            if led and led["points"]:
                shares = [pt["cash"] / pt["equity"] for pt in led["points"] if pt["equity"]]
                row["cash_share_end_pct"] = _f(shares[-1] * 100.0, 4)
                row["cash_share_avg_pct"] = _f(sum(shares) / len(shares) * 100.0, 4)
        rows.append(row)

    paper_keys = {r["book_id"][len(config.SELECTOR_PREFIX):] for r in db.q(
        con, "SELECT book_id FROM books WHERE book_id LIKE ?",
        (config.SELECTOR_PREFIX + "%",))}
    roster = {"other_mode": "paper",
              "added": sorted(have - paper_keys),
              "absent": [{"key": k, "reason": _absent_reason(run, k)}
                         for k in sorted(paper_keys - have)]}

    weeks_seen: dict[str, dict[str, str]] = {}
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for arm, c in curves.items():
        if arm in ledgers and ledgers[arm]["points"]:
            wp = weekly_pnl(ledgers[arm])
        else:
            # Points only: the equity delta is real, the decomposition is not
            # recorded. Zeros here are "not recorded", which the note says.
            buckets: dict[str, dict[str, Any]] = {}
            for i, q in enumerate(c):
                wk, st, en = iso_week(q["d"])
                b = buckets.get(wk)
                if not b:
                    prev = c[i - 1]["v"] if i else c[0]["v"]
                    b = buckets[wk] = {"week": wk, "start": st, "end": en,
                                       "equity_start": prev, "first_d": q["d"]}
                b["equity_end"] = q["v"]
                b["last_d"] = q["d"]
            wp = []
            for wk in sorted(buckets):
                b = buckets[wk]
                pnl = b["equity_end"] - b["equity_start"]
                wp.append({"week": wk, "start": b["start"], "end": b["end"],
                           "first_d": b["first_d"], "last_d": b["last_d"],
                           "equity_start": _f(b["equity_start"], 6),
                           "equity_end": _f(b["equity_end"], 6), "pnl_amt": _f(pnl, 6),
                           "pnl_pct": (_f(pnl / b["equity_start"] * 100.0, 4)
                                       if b["equity_start"] else None),
                           "realized": 0.0, "unrealized_chg": 0.0, "cash_income": 0.0,
                           "fees": 0.0, "flows": 0.0, "reconciled": False,
                           "residual": _f(pnl, 6), "decomposition": "not_recorded"})
        by_strategy[arm] = wp
        for w in wp:
            weeks_seen.setdefault(w["week"], {"week": w["week"], "start": w["start"],
                                              "end": w["end"]})
    weeks = [weeks_seen[k] for k in sorted(weeks_seen)]
    if formal and ledgers:
        wnote = ("正式回测保留了 bt: 组合的完整记录，周 PnL 按与模拟运行同一套分解与对账规则算。")
    elif formal:
        wnote = ("正式回测的 bt: 组合记录已清理，只剩净值点：pnl_amt 是真实的净值变化，"
                 "分解项未记录，填 0，reconciled=false。")
    else:
        wnote = ("选股研究只有指数化净值点（起点 100），没有资金账：pnl_amt 是净值变化"
                 "（指数点），realized/unrealized/cash_income/fees 未记录，填 0，"
                 "reconciled=false 表示「无法对账」而不是「对不上」。")

    if formal and ledgers:
        all_pos = [q for led in ledgers.values() for q in led["positions"]]
        upnl: dict = {}
        for led in ledgers.values():
            upnl.update(led["upnl_last"])
        attr = attribution(con, all_pos, upnl)
    else:
        attr = _study_attribution(con, positions)

    n_periods = len(summary.get("dates") or []) or len(
        {r["period"] for r in positions})
    n_days = max((r["n_days"] for r in rows), default=0)
    if summary.get("tearsheet"):
        research = {"sample_note": (f"样本：{n_periods} 期，{n_days} 个净值点；"
                                    f"统计沿用回测记录里的 tearsheet（compare_arms 输出）。"),
                    "n_periods": n_periods, "n_days": n_days,
                    "stats": summary["tearsheet"],
                    "extra": {k: summary.get(k) for k in (
                        "paired", "prereg", "ranking_power", "horizon_completeness",
                        "live_vs_backfill", "robustness_drop_top") if k in summary}}
    else:
        research = _research(curves, spy, rf, n_periods, n_days)

    if formal:
        disclosures = list(summary.get("disclosures") or [])
        disclosures.insert(0, "正式回测（formal-paper-rules）：与模拟运行同一套 paper.step "
                              "代码走成交、止损止盈、到期、现金计息与成本。")
    else:
        disclosures = list(STUDY_DISCLOSURES)
        if run.get("data_classification"):
            disclosures.append(f"数据分类：{run['data_classification']}；"
                               f"期次分类：{json.dumps(summary.get('period_classification') or {}, ensure_ascii=False)}")

    rec = records(con)
    rec["backtest"] = {"backtest_id": bid, "methodology_raw": run["methodology"],
                       "as_of": run["as_of"], "started_at": run.get("started_at"),
                       "ended_at": run.get("ended_at"),
                       "dates": summary.get("dates"),
                       "excluded_arms": summary.get("excluded_arms") or [],
                       "arm_errors": {a: v.get("errors") for a, v in arm_summary.items()
                                      if isinstance(v, dict) and v.get("errors")}}
    return {
        "mode": "backtest", "label": MODES["backtest"], "subset": "all",
        "methodology": methodology, "methodology_raw": run["methodology"],
        "source_id": bid,
        "window": {"start": run["window_start"], "end": run["window_end"]},
        "capital": capital, "generated_at": gen,
        "strategies": strategies,
        "benchmarks": {
            "spy": {"name": "SPY", "code": config.BENCHMARKS["SPY"], "curve": spy},
            "buy_all": {"name": "全量基准", "curve": ba_curve,
                        "available": bool(ba_curve),
                        "reason": None if ba_curve else "该回测里没有 buy_all 组合"}},
        "weekly_pnl": {"weeks": weeks, "by_strategy": by_strategy, "note": wnote},
        "summary": {"as_of": last_d, "cash_share_basis": "end", "rows": rows,
                    "roster_diff": roster},
        "attribution": attr,
        "research": research,
        "records": rec,
        "disclosures": disclosures,
    }


# ---------------------------------------------------------------- index
def perf_index(con) -> dict[str, Any]:
    """The cheap entry-point block for the state document: what modes exist.

    Counts and windows only — never a curve — because this rides along with a
    document polled every minute.
    """
    modes = []
    books = [r["book_id"] for r in db.q(
        con, "SELECT book_id FROM books WHERE book_id LIKE ? ORDER BY book_id",
        (config.SELECTOR_PREFIX + "%",))]
    span = db.q1(con, "SELECT MIN(d) a, MAX(d) b FROM equity WHERE book_id LIKE ?",
                 (config.SELECTOR_PREFIX + "%",))
    modes.append({"mode": "paper", "label": MODES["paper"], "available": bool(books),
                  "window": ({"start": span["a"], "end": span["b"]}
                             if span and span["a"] else None),
                  "n_strategies": len(books), "methodology": PAPER_METHODOLOGY,
                  "source_id": None, "subsets": list(SUBSETS), "default_subset": "live"})
    runs = [dict(r) for r in db.q(
        con, "SELECT backtest_id, as_of, window_start, window_end, methodology "
             "FROM backtest_runs WHERE ok=1 ORDER BY as_of DESC, ended_at DESC")]
    latest = runs[0] if runs else None
    n_arms = 0
    if latest:
        n_arms = db.q1(con, "SELECT COUNT(DISTINCT arm) n FROM backtest_points "
                            "WHERE backtest_id=?", (latest["backtest_id"],))["n"]
    modes.append({"mode": "backtest", "label": MODES["backtest"], "available": bool(latest),
                  "window": ({"start": latest["window_start"], "end": latest["window_end"]}
                             if latest else None),
                  "n_strategies": n_arms,
                  "methodology": (METHODOLOGY_MAP.get(latest["methodology"], STUDY_METHODOLOGY)
                                  if latest else None),
                  "source_id": latest["backtest_id"] if latest else None})
    return {
        "modes": modes,
        "live": {"available": False, "label": "实盘（未接入）", "reason": "尚未接入真实资金"},
        # Every selectable backtest, newest first, so the page can offer the
        # formal run and the study side by side without another round trip.
        "backtest_sources": [{"backtest_id": r["backtest_id"], "as_of": r["as_of"],
                              "window": {"start": r["window_start"], "end": r["window_end"]},
                              "methodology": METHODOLOGY_MAP.get(r["methodology"],
                                                                 STUDY_METHODOLOGY),
                              "methodology_raw": r["methodology"]} for r in runs],
    }
