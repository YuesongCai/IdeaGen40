"""运行成本：这个项目每个月花了多少钱，花在哪，和「约 200 美元/月」的估算对不对得上。

yifu 2026-09-11：「成果还没用、钱已经开始花，有错位」「字节先多要代金券」。在谈要不要
多要代金券之前，得先有一张说得清的账——而字节云的账单上同一个主账号里混着 IdeaGen、
nexus-card 和别的项目。

三个来源，口径各不相同，卡上分开写：

* **字节云账单**（实数）。只读两个接口：`ListBillOverviewByProd`（按产品汇总）与
  `ListBillDetail`（按实例汇总，`GroupTerm=1`）。按实例名前缀归属项目，对不上的单列
  「未归属」，不猜。
* **Claude 推理**（估算）。生成走操作者的 Claude Code 会话，没有逐次账单；按
  `orch_runs.calls` × 每次调用的 token 假设 × API 单价折算，全部参数在 config，卡上标「估算」。
* **数据源**（0）。Wisburg 研报与 FMP 的 key 由佳琦提供，本项目不付费，记 0 并写明。

## 为什么对 `ve` 设白名单

`ve <service> <Action>` 是**真执行**，不是试探。这个仓曾经把一个 Delete 动作当成
「探一下接口在不在」来调，删掉了一个真的数据库（见 memory「字节省钱清理」）。所以
这里的规则写在代码里、而不是文档里：`ve_call` 只接受 `ALLOWED` 里的两个 (服务, 动作)，
其余任何动作——包括看起来无害的 Describe——在拼命令行之前就抛 `ForbiddenAction`，
`tests/test_costs.py` 守着。要加动作，改白名单并在测试里加一条，理由写进提交。

## 存储

`cost_snapshots` 一个账期一行（该期最新一次成功拉取的汇总，不存原始行的客户名等字段）。
拉取失败只写 `last_attempt_at / last_error`，不覆盖上一份成功的数：一次断网不该让
卡片变成 0。`cmd_daily` 的 `costs` 阶段每天刷新本月（月初三天顺带刷新上月，账单会补记）。
"""

from __future__ import annotations

import calendar
import json
import subprocess
from datetime import date, datetime
from typing import Any, Callable

from . import config, db

#: The only `ve` calls this module may make. Both are read-only List actions.
ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("billing", "ListBillOverviewByProd"),
    ("billing", "ListBillDetail"),
})

SOURCE = "byteplus"
DDL = """CREATE TABLE IF NOT EXISTS cost_snapshots (
    source          TEXT NOT NULL,
    period          TEXT NOT NULL,           -- YYYY-MM
    fetched_at      TEXT,                    -- last successful fetch (HKT ISO)
    summary         TEXT,                    -- JSON, see summarize()
    items           TEXT,                    -- JSON, trimmed per-instance rows
    last_attempt_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (source, period)
)"""

UNATTRIBUTED = "未归属"


class ForbiddenAction(RuntimeError):
    """A `ve` action outside the whitelist was requested. Never executed."""


class VeError(RuntimeError):
    pass


def ensure_schema(con) -> None:
    con.execute(DDL)


# ------------------------------------------------------------------ ve
def _default_runner(argv: list[str]) -> str:
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    if cp.returncode != 0:
        raise VeError(f"ve 退出码 {cp.returncode}：{(cp.stderr or cp.stdout)[:300]}")
    return cp.stdout


def parse_ve_output(text: str) -> dict[str, Any]:
    """The response object, skipping any notice printed before it.

    The CLI can print a `_notice` (itself sometimes a JSON object) ahead of the
    payload, so decoding from the first `{` is not enough: walk every `{`, decode,
    and take the first object that looks like an API response.
    """
    dec = json.JSONDecoder()
    i = text.find("{")
    while i >= 0:
        try:
            obj, end = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and ("Result" in obj or "ResponseMetadata" in obj):
            err = (obj.get("ResponseMetadata") or {}).get("Error")
            if err:
                raise VeError(f"接口返回错误：{err}")
            return obj
        i = text.find("{", i + end)
    raise VeError("ve 输出里没有找到接口响应 JSON")


def ve_call(service: str, action: str, params: dict[str, Any], *,
            runner: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    if (service, action) not in ALLOWED:
        raise ForbiddenAction(
            f"拒绝调用 ve {service} {action}：成本模块只允许 "
            f"{sorted(a for _, a in ALLOWED)}。ve 的动作是真执行，不是探测。")
    argv = ["ve", service, action]
    for k, v in params.items():
        argv += [f"--{k}", str(v)]
    argv += ["--profile", config.COST_VE_PROFILE, "--region", config.COST_VE_REGION]
    return parse_ve_output((runner or _default_runner)(argv))


def _paged(action: str, period: str, limit: int, extra: dict[str, Any],
           runner) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {"BillPeriod": period, "Limit": limit, "IgnoreZero": 1, **extra}
        if offset:
            params["Offset"] = offset
        res = ve_call("billing", action, params, runner=runner).get("Result") or {}
        rows = res.get("List") or []
        out.extend(rows)
        total = int(res.get("Total") or len(out))
        offset += len(rows)
        if not rows or offset >= total or offset > 5000:
            return out


def fetch(period: str, runner=None) -> tuple[list[dict], list[dict]]:
    overview = _paged("ListBillOverviewByProd", period, 50, {}, runner)
    detail = _paged("ListBillDetail", period, 200, {"GroupTerm": 1}, runner)
    return overview, detail


# ------------------------------------------------------------------ attribution
def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def project_of(product: str, name: str, instance_no: str) -> tuple[str, str]:
    """(project, rule) for one billed instance. The rule is shown on the card."""
    if instance_no and instance_no in config.COST_INSTANCE_PROJECT:
        return config.COST_INSTANCE_PROJECT[instance_no], "实例号指定"
    low = (name or "").lower()
    for prefix, proj in config.COST_PREFIX_PROJECT:
        if low.startswith(prefix):
            return proj, f"实例名前缀 {prefix}"
    for needle, proj in config.COST_NAME_PROJECT:
        if needle in low:
            return proj, f"实例名含 {needle}"
    if product in config.COST_PRODUCT_PROJECT:
        return config.COST_PRODUCT_PROJECT[product], f"产品 {product}"
    return UNATTRIBUTED, "实例名与产品都对不上归属规则"


def _trim(r: dict[str, Any]) -> dict[str, Any]:
    name = r.get("InstanceName") or ""
    no = r.get("InstanceNo") or ""
    proj, rule = project_of(str(r.get("Product") or ""), name, no)
    return {"product": r.get("Product"), "name": name, "instance_no": no,
            "original": round(_num(r.get("OriginalBillAmount")), 6),
            "coupon": round(_num(r.get("CouponAmount")), 6),
            "preferential": round(_num(r.get("PreferentialBillAmount")), 6),
            "payable": round(_num(r.get("PayableAmount")), 6),
            "project": proj, "rule": rule,
            "retired": config.COST_RETIRED.get(name)}


def _sum(items: list[dict], key: str) -> float:
    return round(sum(i[key] for i in items), 2)


def summarize(period: str, overview: list[dict], detail: list[dict],
              fetched_on: date) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items = [_trim(r) for r in detail]
    y, m = (int(x) for x in period.split("-"))
    days_in = calendar.monthrange(y, m)[1]
    in_month = (fetched_on.year, fetched_on.month) == (y, m)
    # The bill is read on `fetched_on`; its rows run through that day. A past
    # month is complete. A run rate of a finished month is just its total.
    elapsed = fetched_on.day if in_month else days_in
    projects: dict[str, dict[str, Any]] = {}
    for it in items:
        p = projects.setdefault(it["project"], {"items": []})
        p["items"].append(it)
    out_projects = {}
    for name, p in projects.items():
        its = p["items"]
        payable = _sum(its, "payable")
        retired = round(sum(i["payable"] for i in its if i["retired"]), 2)
        by_product: dict[str, dict[str, float]] = {}
        for i in its:
            b = by_product.setdefault(i["product"], {"original": 0.0, "coupon": 0.0, "payable": 0.0})
            for k in b:
                b[k] += i[k]
        out_projects[name] = {
            "original": _sum(its, "original"), "coupon": _sum(its, "coupon"),
            "preferential": _sum(its, "preferential"), "payable": payable,
            "retired_payable": retired,
            "run_rate": round(payable / elapsed * days_in, 2) if elapsed else None,
            "run_rate_ex_retired": (round((payable - retired) / elapsed * days_in, 2)
                                    if elapsed else None),
            "by_product": sorted(({"product": k, **{kk: round(vv, 2) for kk, vv in v.items()}}
                                  for k, v in by_product.items()),
                                 key=lambda r: -(r["payable"] + r["coupon"])),
            "top_items": sorted(({k: i[k] for k in ("product", "name", "original", "coupon",
                                                     "payable", "retired", "rule")}
                                 for i in its if i["original"] >= 0.5),
                                key=lambda r: -r["original"])[:12],
            "n_items": len(its),
        }
    ov = {"original": round(sum(_num(r.get("OriginalBillAmount")) for r in overview), 2),
          "coupon": round(sum(_num(r.get("CouponAmount")) for r in overview), 2),
          "payable": round(sum(_num(r.get("PayableAmount")) for r in overview), 2)}
    det = {"original": _sum(items, "original"), "coupon": _sum(items, "coupon"),
           "payable": _sum(items, "payable")}
    summary = {
        "period": period, "currency": "USD", "fetched_on": fetched_on.isoformat(),
        "is_current": in_month,
        "days_elapsed": elapsed, "days_in_month": days_in,
        "overview_total": ov, "detail_total": det,
        # The per-instance rows are rounded per row by the vendor; the two totals
        # may differ by cents. More than a dollar means rows are missing.
        "reconciled": abs(ov["payable"] - det["payable"]) <= 1.0,
        "projects": out_projects,
        "by_product_overview": sorted(({"product": r.get("Product"),
                                        "original": round(_num(r.get("OriginalBillAmount")), 2),
                                        "coupon": round(_num(r.get("CouponAmount")), 2),
                                        "payable": round(_num(r.get("PayableAmount")), 2)}
                                       for r in overview), key=lambda r: -r["original"]),
    }
    return summary, items


# ------------------------------------------------------------------ store / refresh
def store(con, period: str, summary: dict[str, Any], items: list[dict[str, Any]]) -> None:
    ensure_schema(con)
    now = config.now_hkt().isoformat()
    con.execute(
        "INSERT INTO cost_snapshots (source, period, fetched_at, summary, items, last_attempt_at, last_error) "
        "VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(source, period) DO UPDATE SET "
        "fetched_at=excluded.fetched_at, summary=excluded.summary, items=excluded.items, "
        "last_attempt_at=excluded.last_attempt_at, last_error=NULL",
        (SOURCE, period, now, json.dumps(summary, ensure_ascii=False),
         json.dumps(items, ensure_ascii=False), now))
    _commit(con)


def _record_failure(con, period: str, err: str) -> None:
    ensure_schema(con)
    now = config.now_hkt().isoformat()
    con.execute(
        "INSERT INTO cost_snapshots (source, period, last_attempt_at, last_error) VALUES (?,?,?,?) "
        "ON CONFLICT(source, period) DO UPDATE SET last_attempt_at=excluded.last_attempt_at, "
        "last_error=excluded.last_error", (SOURCE, period, now, err[:500]))
    _commit(con)


def _commit(con) -> None:
    try:
        con.commit()
    except Exception:  # noqa: BLE001 — autocommit connections have nothing to commit
        pass


def refresh(con, period: str | None = None, *, runner=None,
            today: date | None = None) -> dict[str, Any]:
    today = today or config.today_hkt()
    period = period or today.strftime("%Y-%m")
    try:
        overview, detail = fetch(period, runner=runner)
        summary, items = summarize(period, overview, detail, today)
    except ForbiddenAction:
        raise
    except Exception as e:  # noqa: BLE001 — network / CLI failure is recorded, not raised
        msg = f"{type(e).__name__}: {e}"
        _record_failure(con, period, msg)
        return {"period": period, "ok": False, "error": msg}
    store(con, period, summary, items)
    ig = summary["projects"].get("IdeaGen") or {}
    return {"period": period, "ok": True, "payable_total": summary["overview_total"]["payable"],
            "ideagen_payable": ig.get("payable"), "reconciled": summary["reconciled"]}


def daily_stage(con, today: date | None = None) -> str:
    """Never raises: the daily run must not turn partial because a bill API is down."""
    today = today or config.today_hkt()
    periods = [today.strftime("%Y-%m")]
    if today.day <= 3:
        prev = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
        periods.insert(0, prev.strftime("%Y-%m"))
    notes = []
    for p in periods:
        try:
            r = refresh(con, p, today=today)
        except Exception as e:  # noqa: BLE001
            r = {"period": p, "ok": False, "error": f"{type(e).__name__}: {e}"}
        notes.append(f"{p} " + (f"应付 {r.get('payable_total')}（IdeaGen {r.get('ideagen_payable')}）"
                                if r.get("ok") else f"未刷新：{r.get('error')}"))
    return "；".join(notes)


# ------------------------------------------------------------------ claude estimate
def claude_estimate(con, period: str) -> dict[str, Any]:
    try:
        r = db.q1(con, "SELECT COALESCE(SUM(calls),0) n, COUNT(*) runs FROM orch_runs "
                       "WHERE substr(COALESCE(started_at, as_of),1,7)=? AND COALESCE(calls,0)>0",
                  (period,))
        calls, runs = int(r["n"] or 0), int(r["runs"] or 0)
    except Exception:  # noqa: BLE001 — no orch_runs table on a fresh database
        calls, runs = 0, 0
    tin = calls * config.COST_CLAUDE_TOKENS_IN_PER_CALL
    tout = calls * config.COST_CLAUDE_TOKENS_OUT_PER_CALL
    usd = tin / 1e6 * config.COST_CLAUDE_USD_PER_MTOK_IN + tout / 1e6 * config.COST_CLAUDE_USD_PER_MTOK_OUT
    return {"estimate": True, "model": config.COST_CLAUDE_MODEL, "calls": calls, "runs": runs,
            "tokens_in": tin, "tokens_out": tout, "usd": round(usd, 2),
            "basis": (f"orch_runs.calls {calls} 次 × 每次约 {config.COST_CLAUDE_TOKENS_IN_PER_CALL:,} 输入 / "
                      f"{config.COST_CLAUDE_TOKENS_OUT_PER_CALL:,} 输出 token × "
                      f"{config.COST_CLAUDE_MODEL} API 单价 ${config.COST_CLAUDE_USD_PER_MTOK_IN:g}/"
                      f"${config.COST_CLAUDE_USD_PER_MTOK_OUT:g} 每百万 token。实际走 Claude Code 会话订阅，"
                      f"这是按 API 价折算的量级，不是账单。")}


# ------------------------------------------------------------------ card
def card(con, today: date | None = None) -> dict[str, Any]:
    today = today or config.today_hkt()
    period = today.strftime("%Y-%m")
    try:
        # Read path: never create the table here. The state document is served
        # from nodes whose database may be a read-only snapshot.
        if not db.q1(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_snapshots'"):
            out = {"period": period, "available": False,
                   "why": "还没有运行成本快照（运行 ideagen costs refresh，或等每日运行的成本阶段）",
                   "claude": claude_estimate(con, period)}
            return out
        row = db.q1(con, "SELECT * FROM cost_snapshots WHERE source=? AND period=?", (SOURCE, period))
        if not row or not row["summary"]:
            prev = db.q1(con, "SELECT * FROM cost_snapshots WHERE source=? AND summary IS NOT NULL "
                              "ORDER BY period DESC LIMIT 1", (SOURCE,))
        else:
            prev = None
    except Exception as e:  # noqa: BLE001
        return {"available": False, "why": f"读成本快照失败：{type(e).__name__}: {e}"}
    use = row if row and row["summary"] else prev
    out: dict[str, Any] = {
        "period": period, "budget_usd_month": config.COST_BUDGET_USD_MONTH,
        "claude": claude_estimate(con, period),
        "data_sources": [{"name": n, "usd": v, "note": "佳琦提供的 key，本项目不付费"}
                         for n, v in config.COST_DATA_SOURCES],
        "last_attempt_at": row["last_attempt_at"] if row else None,
        "last_error": row["last_error"] if row else None,
    }
    if not use:
        out.update(available=False, why=("还没有成功拉取过字节云账单（运行 ideagen costs refresh，"
                                         "或等每日运行的成本阶段）"))
        return out
    s = json.loads(use["summary"])
    fetched = str(use["fetched_at"] or "")[:10]
    stale = (today - date.fromisoformat(fetched)).days if fetched else None
    ig = s["projects"].get("IdeaGen") or {}
    others = [{"project": k, "payable": v["payable"], "original": v["original"],
               "coupon": v["coupon"], "by_product": v["by_product"]}
              for k, v in s["projects"].items() if k not in ("IdeaGen", UNATTRIBUTED)]
    un = s["projects"].get(UNATTRIBUTED)
    rr = ig.get("run_rate_ex_retired")
    out.update(
        available=True, snapshot_period=s["period"], fetched_at=use["fetched_at"],
        stale_days=stale, is_current_period=s["period"] == period,
        days_elapsed=s["days_elapsed"], days_in_month=s["days_in_month"],
        account_total=s["overview_total"], reconciled=s["reconciled"],
        ideagen=ig, others=sorted(others, key=lambda r: -r["payable"]),
        unattributed=({"payable": un["payable"], "original": un["original"],
                       "items": un["top_items"]} if un else None),
        budget_gap_usd=(round(rr - config.COST_BUDGET_USD_MONTH, 2) if rr is not None else None),
        # Claude is month-to-date too; scale it the same way so the two add up.
        claude_run_rate=(round(out["claude"]["usd"] / s["days_elapsed"] * s["days_in_month"], 2)
                         if s.get("is_current") and s["days_elapsed"] else out["claude"]["usd"]),
        rules=("实例名前缀 ideagen- → IdeaGen、nexus- → nexus-card；AgentKit 与 agentkit 镜像仓库 → "
               "IdeaGen 早期沙箱实验；VDB_KnowledgeBase → 其他（IdeaGen 代码零引用）；对不上的单列未归属。"),
    )
    return out


def cmd_costs(args) -> int:
    con = db.init()
    if args.action == "refresh":
        print(json.dumps(refresh(con, getattr(args, "period", None)), ensure_ascii=False, indent=1))
        return 0
    print(json.dumps(card(con), ensure_ascii=False, indent=1))
    return 0
