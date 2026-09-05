"""复盘板: one page that answers "现在什么情况" from the live database.

The system's state was spread across logs, the database, TOS and launchd — every
"is it running?" required shell archaeology. This page is the anti-archaeology:
regenerated from the live stores on every request, it answers, in order, the
questions an operator actually asks — is it alive, what did the last run do,
what is the book holding, which assumptions are still unproven, what was broken
and what state is the fix in, and how do I drive it by hand.

Everything here is read-only over the same tables the pipeline writes. The page
computes nothing of its own: a number that exists only on a dashboard is a number
nobody can reproduce, so every figure is a straight query.
"""

from __future__ import annotations

import html
import hashlib
import json
import os
import pathlib
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import cloud_paper, config, db, periods, platform as plat, shelf_store

E = html.escape


def _hkt_now() -> datetime:
    return config.now_hkt()


def _card(title: str, body: str, tone: str = "") -> str:
    return (f'<div class="card {tone}"><div class="ct">{E(title)}</div>'
            f'{body}</div>')


def _tbl(head: list[str], rows: list[list[str]], escape: bool = True) -> str:
    h = "".join(f"<th>{E(x)}</th>" for x in head)
    b = "".join("<tr>" + "".join(
        f"<td>{E(str(c)) if escape else c}</td>" for c in r) + "</tr>"
        for r in rows)
    return f'<table><tr>{h}</tr>{b}</table>'


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _licensed(classification: Any) -> bool:
    value = str(classification or "")
    return (value.startswith("licensed-")
            or "licensed-private-corpus" in value
            or "+licensed-live-" in value)


def _opaque(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:8].upper()
    return f"{prefix}-{digest}"


def _backtest_state(p) -> dict[str, Any]:
    """Latest replay result, read only from the durable cloud state."""
    try:
        rows = p.state.q(
            "SELECT backtest_id, as_of, window_start, window_end, methodology, "
            "data_classification, inputs_sha, artifact_uri, started_at, ended_at, "
            "summary FROM backtest_runs WHERE ok=1 "
            "ORDER BY as_of DESC, ended_at DESC LIMIT 1")
        if not rows:
            return {}
        run = dict(rows[0])
        backtest_id = run["backtest_id"]
        points = [dict(row) for row in p.state.q(
            "SELECT arm, d, equity, period_ret, drawdown, n_positions "
            "FROM backtest_points WHERE backtest_id=? ORDER BY d, arm",
            (backtest_id,))]
        positions = [dict(row) for row in p.state.q(
            "SELECT arm, period, instrument_id, entry_d, exit_d, entry_nav, "
            "exit_nav, return_pct, status, thesis FROM backtest_positions "
            "WHERE backtest_id=? ORDER BY period DESC, arm, instrument_id",
            (backtest_id,))]
    except Exception:  # noqa: BLE001 - old deployments may not have these tables
        return {}

    summary = _json_value(run.pop("summary", None), {})
    run["artifact_archived"] = bool(run.pop("artifact_uri", None))
    run["inputs_sha"] = str(run.get("inputs_sha") or "")[:16]
    run["summary"] = summary
    run["points"] = points
    run["positions"] = positions
    run["latest_period"] = max(
        (str(row["period"]) for row in positions), default=None)
    return run


# ---------------------------------------------------------------- sections
def _alive(p) -> str:
    """Is it running — the question that must be answerable in five seconds."""
    hb_raw = None
    try:
        hb_raw = p.cache.get("scheduler:heartbeat")
    except Exception:  # noqa: BLE001
        pass
    if hb_raw:
        hb = json.loads(hb_raw)
        at = datetime.fromisoformat(hb["at_utc"])
        age = (datetime.now(timezone.utc) - at).total_seconds()
        alive = age < 2 * 900          # two tick intervals of silence = dead
        tone = "good" if alive else "bad"
        head = (f"<b>{'在跑' if alive else '⚠ 心跳超时'}</b> — 上次心跳 "
                f"{int(age)}s 前（{E(hb.get('at_hkt','?')[:19])} HKT），"
                f"platform=<code>{E(hb.get('platform','?'))}</code> "
                f"venue=<code>{E(hb.get('venue','?'))}</code>")
    else:
        tone, head = "bad", "<b>⚠ 没有心跳记录</b> — 调度器从未跑过或缓存被清"
    ports = "".join(
        f'<span class="pill {"ok" if q["ok"] else "no"}">{E(q["name"])}</span>'
        for q in _port_health(p, lambda d: d)["ports"])
    return _card("这套系统现在在不在跑", f"<p>{head}</p><p>{ports}</p>"
                 "<p class=dim>判定标准：launchd 每 15 分钟一次 tick，每次 tick 都写心跳。"
                 "两个周期没有心跳 = 停了，不是安静。</p>", tone)


def _runs(p) -> str:
    rows = []
    for r in p.state.q(
            "SELECT run_id, as_of, kind, platform, ok, started_at, ended_at, error, calls "
            "FROM orch_runs WHERE kind IN ('weekly','monitor') "
            "ORDER BY started_at DESC LIMIT 12"):
        st = ("🔄 进行中" if r["ended_at"] is None and r["kind"] == "weekly"
              else ("✓" if r["ok"] else ("✗ " + str(r["error"] or "")[:50])))
        rows.append([r["as_of"], r["kind"], r["platform"],
                     (r["started_at"] or "")[:16], st, r["calls"] or 0])
    # A gap that has since been filled is not a gap. The marker row stays as
    # history, but a period with a successful weekly run must stop being
    # counted as missing — otherwise the banner keeps reporting a hole the
    # record no longer has.
    gaps = p.state.q(
        "SELECT as_of FROM orch_runs g WHERE run_id LIKE 'gap-%' "
        "AND NOT EXISTS (SELECT 1 FROM orch_runs w WHERE w.kind='weekly' "
        "  AND w.ok=1 AND w.as_of=g.as_of) ORDER BY as_of")
    gap_line = ("<p class=dim>永久错过（不补跑）：" +
                "、".join(g["as_of"] for g in gaps) + "</p>") if gaps else ""
    return _card("运行史（最近 12 次）",
                 _tbl(["期次", "类型", "平台", "开始", "状态", "模型调用"], rows)
                 + gap_line)


def _latest_weekly(p) -> str:
    run = p.state.q("SELECT run_id, as_of, ok, ended_at FROM orch_runs "
                    "WHERE kind='weekly' ORDER BY started_at DESC LIMIT 1")
    if not run:
        return _card("本期流水线", "<p>还没有周跑记录</p>", "warn")
    r = run[0]
    rid = r["run_id"]
    out = [f"<p>run <code>{E(rid)}</code> · 期次 {E(r['as_of'])} · "
           f"{'✓ 完成' if r['ok'] else ('🔄 进行中' if r['ended_at'] is None else '✗ 失败')}</p>"]

    tv = p.state.q("SELECT strategy, chosen FROM verdicts WHERE run_id=? "
                   "AND kind='topic_scorer'", (rid,))
    if tv:
        rows = [[v["strategy"], "、".join(json.loads(v["chosen"]))] for v in tv]
        out.append("<h4>筛选A · 主题</h4>" + _tbl(["打分", "前五主题"], rows))

    gv = p.state.q("SELECT strategy, chosen, meta FROM verdicts WHERE run_id=? "
                   "AND kind='idea_generator'", (rid,))
    if gv:
        rows = []
        for v in gv:
            meta = json.loads(v["meta"] or "{}")
            per = meta.get("per_topic", {})
            rows.append([v["strategy"], len(json.loads(v["chosen"])),
                         "、".join(f"{k}:{n}" for k, n in per.items()) or "—"])
        out.append("<h4>筛选B · 出想法</h4>"
                   + _tbl(["方式", "产出", "每主题"], rows))

    n_pool = p.state.q("SELECT COUNT(*) c FROM candidates WHERE run_id=?",
                       (rid,))[0]["c"]
    sv = p.state.q("SELECT strategy, chosen FROM verdicts WHERE run_id=? "
                   "AND kind='idea_selector'", (rid,))
    if sv:
        rows = [[v["strategy"], len(json.loads(v["chosen"])),
                 "、".join(c.replace("pool:", "")
                           for c in json.loads(v["chosen"])[:10])]
                for v in sorted(sv, key=lambda x: x["strategy"])]
        out.append(f"<h4>筛选C · 定持仓（候选池 {n_pool} 条）</h4>"
                   + _tbl(["选取策略", "选中", "标的"], rows))
    return _card("本期流水线", "".join(out))


def _books(con) -> str:
    rows = []
    for b in db.q(con, "SELECT book_id, label FROM books "
                       "WHERE book_id LIKE 'sel-%' ORDER BY book_id"):
        pos = db.q1(con, "SELECT COUNT(*) n, COALESCE(SUM(qty*COALESCE(last_px,0)),0) mv "
                         "FROM positions WHERE book_id=? AND status='open'",
                    (b["book_id"],)) or {"n": 0, "mv": 0}
        eq = db.q1(con, "SELECT equity, d FROM equity WHERE book_id=? "
                        "ORDER BY d DESC LIMIT 1", (b["book_id"],))
        realized = db.q1(con, "SELECT COALESCE(SUM(realized),0) r FROM positions "
                              "WHERE book_id=? AND status='closed'",
                         (b["book_id"],))
        rows.append([b["book_id"].replace("sel-", ""), pos["n"],
                     f"${eq['equity']:,.0f}" if eq else "未标记",
                     (eq["d"] if eq else "—"),
                     f"${realized['r']:,.0f}" if realized else "$0"])
    if not rows:
        return _card("策略组合", "<p>还没有建仓——第一次周跑完成后自动出现</p>", "warn")
    return _card("策略组合（纸面，各 $10m 等资本）",
                 _tbl(["选取策略", "持仓数", "最新权益", "标记至", "已实现"], rows))


ASSUMPTIONS = [
    ("H 的符号（热度正向预测）", "先验，最大风险", "H×P 检验 t=−0.47，无分辨力；攒样本中"),
    ("HGEP 权重 0.30/0.25/0.25/0.20", "先验", "等逐因子归因（约 7 个月量级）"),
    ("语义打分 > 纯数数", "系统立论", "计数对照打分已并行落库，零成本攒证据中"),
    ("σ×2 止损 / σ×3 止盈", "先验", "由只改止损的两个组合之差检验"),
    ("去集中上限 主题3/敞口3/方式4", "先验", "实测能选满 10；上限收到 1 只能选 5"),
    ("ρ≈0.8（配对相关）", "整个验证时间表的地基", "第一个月实测"),
    ("赚亏比下限 1.5", "先验", "本周中位数 1.82，未触发；低赔率周才生效"),
    ("1 个月单一持有期", "决策（Jon 原设计是 1m+6m 双期限）", "被覆盖的立场，未正式对比"),
]

def _attribution_state() -> str:
    """Which attribution layers exist, and which one this build computed.

    The sentence used to be hand-written, and it said "four layers" while naming
    three — the count came from Jon's taxonomy (选择/择时/仓位/因子) and the names
    came from a later split of the work, so 仓位 was never mentioned by either.
    Not a miscount: the number and the list were about different things. Deriving
    both from the one table means the sentence cannot disagree with itself again.
    """
    try:
        from scripts.run_real_backtest import ATTRIBUTION_LAYERS  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — a caption must never break the payload
        return "层次表暂不可读；已做的是「选择」层：每笔持仓 vs 同窗口改持该主题的 ETF"
    names = [n for n, _, _ in ATTRIBUTION_LAYERS]
    done = [n for n, _, ok in ATTRIBUTION_LAYERS if ok]
    todo = [n for n, _, ok in ATTRIBUTION_LAYERS if not ok]
    return (f"共 {len(names)} 层（" + "、".join(names) + "）。已做「"
            + "、".join(done) + "」层：每笔持仓 vs 同窗口改持该主题的 ETF；"
            + "、".join(todo) + " 层都还没有做")


_ATTRIBUTION_STATE = _attribution_state()

FIXES = [
    ("idea_uid 重绑（+377% 假收益）", "✅ 已修", "产物不可变 + 幂等 + 测试"),
    ("强制凑满 40 条", "✅ 已修", "门槛准入，不足留现金"),
    ("批内同标的重复", "✅ 已修", "候选池一标一条，赔率取中位数"),
    ("thesis 证伪只告警不平仓", "✅ 已修", "事件退出：告警次日收盘平仓"),
    ("止损被进场方式错误闸住", "✅ 已修", "组合自声明风控"),
    ("跨周同标的敞口叠加", "🟡 可见化", "建仓时记录重叠；是否封顶待定"),
    ("没有研报也报成功", "✅ 已修", "没有研报=失败，与「本周没料」区分"),
    ("表撞名静默 / 孤儿数据 / 非法 JSON", "✅ 已修", "建表前查撞名 + orphans() + _finite()"),
    ("Redis 口令写进不可变产物", "✅ 已修", "redact_url 统一脱敏"),
    ("同一期跑两次", "✅ 已修", "数据库部分唯一索引，不只靠锁"),
    ("归因四层反事实（Jon）", "🟡 一层已做", _ATTRIBUTION_STATE),
    ("剔除高频标的复测（Jon）", "✅ 已修",
     "最常被持有的标的与其余标的分组比较，剔 5 / 10 / 20 三个深度，"
     "判定用两组合并的可检出下界"),
    ("波动调整后与指数比（Jon）", "❌ 未做", "现在只有裸超额"),
]
#: This list is maintained by hand, and a hand-maintained list of what is not
#: done drifts the moment something gets done — the page then says a thing is
#:未做 while the section above it renders that thing's output. That happened:
#: 剔除高频标的复测 shipped and sat here marked ❌ until someone read the two
#: side by side. Anything here that could be derived from the payload should
#: be; until then, changing a state here is part of shipping the fix.

RUNBOOK = """
<h4>在不在跑？</h4>
<pre>launchctl list | grep ideagen      # 三个常驻：scheduler / daily / serve
tail -20 data/logs/scheduler_tick.log</pre>
<p>或者直接看本页顶部的心跳。云端怎么知道：产物在 TOS（tos://ideagen-…/runs/…），每次运行必写；桶里今天没新对象=今天没跑。</p>
<h4>平常怎么手动跑</h4>
<pre>python3 -c "from ideagen import cli; cli.main(['platform'])"      # 体检
python3 -c "from ideagen import cli; cli.main(['weekly','--trade'])" # 手动周跑+建仓
python3 -c "from ideagen import cli; cli.main(['book'])"             # 补建仓
python3 -m ideagen.scheduler tick                                    # 手动一次 tick
python3 -m ideagen.scheduler health                                  # 调度健康
python3 -m ideagen.scheduler catch-up --since 2026-07-22             # 缺口记档</pre>
<h4>什么时候自动跑</h4>
<p>每 15 分钟一次 tick（盯市/心跳/feed 体检）；<b>每周三 07:00 HKT</b> 自动周跑并建仓，48 小时内可补跑，过了记永久缺失。</p>
<h4>推送</h4>
<p>周跑完成/失败会发飞书 DM（含结果摘要）。里程碑级进展由 Claude 会话另行推送。</p>
<h4>凭证</h4>
<p>全部在 <code>~/.ideagen.env</code>（chmod 600，仓库外）。仓库、镜像、产物里没有任何密钥——每次提交前有审计。</p>
"""


def build(con=None, p=None) -> "config.Path":
    """Compatibility shim. The dashboard is `web/dash.html` + the live /api/state;
    there is nothing to pre-render any more. Kept because the scheduler's monitor
    pass still calls it, and a missing symbol would fail every tick for a page
    that no longer needs building."""
    return config.WEB / "dash.html"


# ---------------------------------------------------------------- state API
def _books_aggregate(books: list[dict[str, Any]]) -> dict[str, Any]:
    """One curve for all the books, and the return that goes with it.

    Aggregating here rather than in the page is the point: this is a cross-book
    roll-up that a screenshot may be judged on, so it needs one definition that
    can be replayed, not one that each renderer re-derives.

    The awkward case is a book with a shorter history. It is not missing data —
    that book had not been funded into positions yet, and cash is a position.
    So a book carries its last mark forward, and sits at its capital before its
    first mark. Summing only the dates every book shares would drop the earliest
    weeks entirely; summing only what is present would make the total jump on
    the day a book starts, which reads as a gain that nobody earned.

    The page used to require identical date axes across all ten books and
    silently produced nothing when one of them started a week late — answering
    "did this make money" with a dash, over a bookkeeping detail.
    """
    funded = [b for b in books if b.get("capital")]
    if not funded:
        return {}
    curves = []
    for b in funded:
        marks = {m["d"]: float(m["equity"]) for m in (b.get("equity") or [])
                 if m.get("d") is not None and m.get("equity") is not None}
        curves.append((float(b["capital"]), marks))
    dates = sorted({d for _, marks in curves for d in marks})
    capital = sum(c for c, _ in curves)
    if not dates or not capital:
        return {"capital": capital, "n_books": len(funded),
                "equity": [], "return_pct": None,
                "basis": "no marked dates yet"}
    carried: list[float | None] = [None] * len(curves)
    series = []
    for d in dates:
        total = 0.0
        for i, (cap_i, marks) in enumerate(curves):
            if d in marks:
                carried[i] = marks[d]
            total += cap_i if carried[i] is None else carried[i]
        series.append({"d": d, "equity": round(total, 2)})
    last = series[-1]["equity"]
    return {
        "capital": capital,
        "n_books": len(funded),
        "equity": series,
        "return_pct": round((last / capital - 1) * 100, 4),
        "basis": ("union of every marked date; a book carries its last mark "
                  "forward and sits at its capital before its first mark"),
    }


# -- port health ---------------------------------------------------------
# `Platform.check()` is a doctor probe: it opens a live connection to all six
# ports, and on the cloud platform that means a TOS `list_objects` round trip —
# 2.9s of a 3.3s state build, paid again on every 60-second dashboard poll. It
# was written to run once before a run, not once a minute. So the poll path
# reads a cached verdict and refreshes it out of band: one slow port can no
# longer hold up the whole document. A verdict past its TTL is still served,
# stamped with the time it was taken; a verdict that has never been taken is
# reported as pending, which is not the same as a port being down.
_HEALTH_TTL_S = 120.0
_HEALTH_COLD_WAIT_S = 2.5
_health_lock = threading.Lock()
_health_probe: dict[str, Any] | None = None
_health_running = False


def _probe_ports(p) -> None:
    """Take a fresh verdict off the request path. Never raises."""
    global _health_probe, _health_running
    try:
        ports = [{"name": h.name, "ok": h.ok, "detail": h.detail}
                 for h in p.check()]
        with _health_lock:
            _health_probe = {"ports": ports, "at": time.time(),
                             "platform": p.name}
    except Exception:  # noqa: BLE001 — the dashboard degrades, it never 500s
        pass
    finally:
        with _health_lock:
            _health_running = False


def _port_health(p, scrub) -> dict[str, Any]:
    """The `alive` block's port fields, with their own freshness stamp."""
    global _health_running
    with _health_lock:
        cur = _health_probe
        fresh = (cur is not None and cur["platform"] == p.name
                 and time.time() - cur["at"] < _HEALTH_TTL_S)
        start = not fresh and not _health_running
        if start:
            _health_running = True
    if start:
        t = threading.Thread(target=_probe_ports, args=(p,),
                             name="port-health", daemon=True)
        t.start()
        if cur is None:
            # Cold start only: an empty panel on the very first load is worse
            # than a short wait. Every poll after this one returns instantly.
            t.join(_HEALTH_COLD_WAIT_S)
            with _health_lock:
                cur = _health_probe
    if cur is None:
        return {"ports": [], "ports_checked_at": None, "ports_age_s": None,
                "ports_stale": False, "ports_pending": True}
    age = time.time() - cur["at"]
    return {"ports": [{"name": q["name"], "ok": q["ok"],
                       "detail": scrub(q["detail"])} for q in cur["ports"]],
            "ports_checked_at": datetime.fromtimestamp(
                cur["at"], timezone.utc).astimezone(config.TZ).isoformat(),
            "ports_age_s": round(age, 1),
            "ports_stale": age >= _HEALTH_TTL_S,
            "ports_pending": False}


#: Keys a licensed run may publish as-is. Everything here is either rendered by
#: the dashboard (`per_topic`, `target_per_topic`, `topics_without_corpus_match`,
#: `truncated`) or a bounded enum / numeric map with no room for prose. Adding a
#: key here is a decision to publish it; that is the point of the list.
_GEN_META_KEEP = frozenset({
    "per_topic", "target_per_topic", "topup_per_topic",
    "topics_without_corpus_match", "truncated", "topic_errors",
    "weights", "caps", "admission", "generation_method",
})


def _gen_meta(meta: dict, hide_licensed: bool) -> dict:
    """A generator's meta, with the one prose-bearing key redacted for export.

    `thesis` a few lines below is set to None under the same flag, for the same
    reason: model-written prose derived from licensed research bodies is not
    ours to republish. `topic_errors` was a hole through that same wall, and not
    a hypothetical one — a topic whose model call returns prose instead of JSON
    fails with `ValueError: 模型返回无法解析为 JSON：<200 chars of the model's
    answer>`, and every one of those characters was reaching the public payload.

    The class name is kept rather than the whole key dropped. Which topics
    failed, and with what kind of failure, is exactly the diagnostic worth
    publishing; the model's own words are the part that is not.

    The far side used to be the publish gate. It is not enough: the gate scans
    for machine identity and partner identifiers, and a key holding the PM's own
    sentence about how to invest matched none of those. That key existed, and
    only a hand review caught it before a publish.

    So the rule is inverted. A named key passes; anything else passes only if it
    is a bare number or boolean, which cannot carry prose. A new diagnostic that
    happens to be a string is dropped from the public payload until someone
    decides, deliberately, that it belongs there — which is the decision that
    silently did not happen last time.
    """
    if not hide_licensed:
        return meta
    out: dict = {}
    for k, v in meta.items():
        if k in _GEN_META_KEEP or (isinstance(v, (int, float, bool))
                                   and not isinstance(v, str)):
            out[k] = v
    errs = out.get("topic_errors")
    if isinstance(errs, dict) and errs:
        out["topic_errors"] = {k: str(v).split(":", 1)[0].strip()
                               for k, v in errs.items()}
    return out


def weekly_block(p, con, as_of: str | None = None) -> dict[str, Any]:
    """One weekly run, all three stages, for `as_of` or the newest period.

    Lifted out of `state()` so the pipeline can be read for *any* period rather
    than only the newest one. `state()` still embeds the newest as `weekly`;
    `/api/period` serves the rest on demand, the same way `/api/corpus` already
    served one period's documents. Keeping the heavy per-period payload out of
    the cached state document is deliberate — the document is polled every
    minute by every open tab, and six periods of candidate pools in it would be
    paid for on every poll by readers looking at one.
    """
    # -- latest weekly, all three stages ---------------------------------
    # Newest *period*, not newest execution. Ordering by started_at alone was
    # fine while runs only ever happened in period order; the moment a missing
    # historical week is filled in, the front page silently reverts to July
    # while the books show today. as_of decides which week this is; started_at
    # only breaks ties between attempts at the same week.
    if as_of:
        # A named period. Ordered `ok DESC` so a week that failed twice and then
        # succeeded reports the success: the failures are its attempt history,
        # visible in the spine, not its verdict.
        wk = p.state.q("SELECT run_id, as_of, ok, ended_at, calls, "
                       "data_classification FROM orch_runs "
                       "WHERE kind='weekly' AND as_of=? "
                       "ORDER BY ok DESC, started_at DESC LIMIT 1", (as_of,))
    else:
        wk = p.state.q("SELECT run_id, as_of, ok, ended_at, calls, "
                       "data_classification FROM orch_runs "
                       "WHERE kind='weekly' ORDER BY as_of DESC, started_at DESC "
                       "LIMIT 1")
    weekly: dict[str, Any] = {}
    hide_licensed = False
    if wk:
        r = wk[0]
        rid = r["run_id"]
        classification = (
            r.get("data_classification")
            or ("public-synthetic"
                if str(rid).startswith("mock-public-") else "live")
        )
        hide_licensed = _licensed(classification)
        weekly = {"run_id": rid, "as_of": r["as_of"], "ok": bool(r["ok"]),
                  "in_flight": r["ended_at"] is None, "calls": r["calls"],
                  "data_classification": classification}
        corpus_receipts = p.state.q(
            "SELECT n_rows FROM feed_runs WHERE run_id=? AND kind='corpus'",
            (rid,))
        weekly["corpus_total"] = (
            sum(int(row["n_rows"] or 0) for row in corpus_receipts)
            if corpus_receipts else None
        )
        weekly["topics"] = [
            {"scorer": v["strategy"], "chosen": json.loads(v["chosen"]),
             "scores": json.loads(v["scores"] or "{}")}
            for v in p.state.q("SELECT strategy, chosen, scores FROM verdicts "
                               "WHERE run_id=? AND kind='topic_scorer'", (rid,))]
        weekly["generators"] = [
            {"method": v["strategy"], "n": len(json.loads(v["chosen"])),
             "meta": _gen_meta(json.loads(v["meta"] or "{}"), hide_licensed),
             "rejected": len(json.loads(v["rejected"] or "{}"))}
            for v in p.state.q("SELECT strategy, chosen, meta, rejected FROM "
                               "verdicts WHERE run_id=? AND kind='idea_generator'",
                               (rid,))]
        cands = [json.loads(c["payload"]) for c in p.state.q(
            "SELECT payload FROM candidates WHERE run_id=?", (rid,))]
        candidate_alias = {
            str(candidate.get("id")): _opaque("CAND", candidate.get("id"))
            for candidate in cands
        }
        weekly["pool"] = {
            "n": len(cands),
            "convergence": {},
            "candidates": [{
                **{k: c.get(k) for k in
                   ("topic_id", "upside_pct", "downside_pct", "p_up", "p_base",
                    "p_down", "proposed_by", "n_proposals")},
                "id": (candidate_alias[str(c.get("id"))]
                       if hide_licensed else c.get("id")),
                "instrument_id": (
                    shelf_store.public_alias(c.get("instrument_id"))
                    if hide_licensed else c.get("instrument_id")),
                "instrument_name": (
                    "Licensed shelf instrument"
                    if hide_licensed else c.get("instrument_name")),
                "thesis": (
                    None if hide_licensed else c.get("thesis")),
            } for c in cands]}
        for c in cands:
            k = str(len(c.get("proposed_by") or []) or 1)
            weekly["pool"]["convergence"][k] = \
                weekly["pool"]["convergence"].get(k, 0) + 1
        weekly["selectors"] = [
            {
                "name": v["strategy"],
                "chosen": [
                    candidate_alias.get(
                        str(candidate_id),
                        _opaque("CAND", candidate_id),
                    )
                    if hide_licensed else str(candidate_id)
                    for candidate_id in json.loads(v["chosen"])
                ],
            }
            for v in p.state.q("SELECT strategy, chosen FROM verdicts "
                               "WHERE run_id=? AND kind='idea_selector'", (rid,))]
    # -- evidence drill-down: which actual documents back each chosen topic.
    # Without this the dashboard's "29 条证据" is a dead end — a count nobody
    # can audit. The reconciliation chain the operator wants (841 docs → this
    # topic's 29 → this topic's ideas → this topic's picks → positions) has to
    # start from real line items, so they are served here, matched by the same
    # theme vocabulary the scorers themselves use.
    if weekly and weekly.get("topics"):
        try:
            from datetime import date as _date, timedelta as _td
            from . import lexicon
            aof = _date.fromisoformat(weekly["as_of"])
            days = [(aof - _td(days=i)).isoformat() for i in range(3)]
            if hide_licensed:
                docs = p.state.q(
                    "SELECT doc_id, published_d, title, "
                    "COALESCE(institution, line) AS institution, tier, "
                    "content_hash, retrieval "
                    "FROM corpus_documents WHERE published_d IN (%s)"
                    % ",".join("?" * len(days)), days)
            else:
                docs = db.q(con,
                            "SELECT doc_id, published_d, title, "
                            "COALESCE(institution, line) AS institution, tier, "
                            "content_hash, retrieval "
                            "FROM documents WHERE published_d IN (%s)"
                            % ",".join("?" * len(days)), days)
            themes_by_id = {t.id: t for t in lexicon.all_themes(aof)}
            chosen_ids = {tid for tv in weekly["topics"] for tid in tv["chosen"]}
            ev: dict[str, Any] = {}
            for tid in chosen_ids:
                th = themes_by_id.get(tid)
                if not th:
                    continue
                terms = [str(x).lower() for x in (th.terms or [])]
                hits = []
                for d in docs:
                    blob = f"{d['title'] or ''}".lower()
                    if any(w in blob for w in terms):
                        # The credentials that make a line item auditable:
                        # the API retrieval expression that re-fetches it, and
                        # the content hash that proves it is the same text.
                        hits.append({k: d[k] for k in
                                     ("doc_id", "published_d", "title",
                                      "institution", "tier", "retrieval")}
                                    | {"sha": (d["content_hash"] or "")[:12]})
                hits.sort(key=lambda x: (x["tier"] or 3, x["published_d"] or ""),
                          reverse=False)
                ev[tid] = {"n": len(hits), "docs": hits[:60],
                           "truncated": max(0, len(hits) - 60),
                           "matched_on": "标题关键词（与打分同一套主题词表）"}
            weekly["evidence"] = ev
            weekly["current_corpus_total"] = len(docs)
        except Exception as e:  # noqa: BLE001 — drill-down must not break the API
            weekly["evidence_error"] = f"{type(e).__name__}: {e}"
    if weekly and weekly.get("corpus_total") is None:
        rows = p.state.q(
            "SELECT n_rows FROM feed_runs WHERE run_id=? AND kind='corpus'",
            (weekly["run_id"],))
        weekly["corpus_total"] = (
            sum(int(row["n_rows"] or 0) for row in rows)
            if rows else weekly.get("current_corpus_total")
        )
    return weekly


def _snapshot_state() -> dict[str, Any] | None:
    """When this node's database was installed, on a node that is fed one.

    `generated_at` answers "when was this page built", which is now on every
    request and therefore reads fresh even when the underlying data stopped
    arriving days ago. That gap is why "is it in sync?" kept having to be asked
    a person rather than read off the page.

    Only the display node has an answer: its database is swapped in by
    sync_state.sh, which leaves the snapshot's hash in a marker file beside it.
    The laptop writes its own database continuously and has no such moment, so
    it reports None and callers should say nothing rather than invent a time.
    """
    db = os.environ.get("IDEAGEN_DB")
    if not db:
        # 没设这个变量的机器（本机就是）自己写库，本来就没有「装上」这个时刻。
        # 不能省这一步：`Path("").parent` 是当前目录，于是会去找 ./.state-sha，
        # 哪天仓库根下真有这么个文件，本机就会报出一个假的新鲜度 —— 而这个
        # 字段存在的全部意义就是不让人被假新鲜骗。
        return None
    marker = pathlib.Path(db).parent / ".state-sha"
    try:
        if not marker.is_file():
            return None
        st = marker.stat()
        return {
            "sha": marker.read_text().strip()[:12] or None,
            "installed_at": datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc).astimezone(config.TZ).isoformat(),
            "age_s": max(0, int(time.time() - st.st_mtime)),
        }
    except OSError:
        # `is_file()` raises on EACCES rather than returning False, and this
        # runs inside the endpoint that would report the problem.
        return None


def state(con=None, p=None) -> dict[str, Any]:
    """The full system state as one JSON document.

    This is the dashboard's only data source, and it lives here — server side,
    next to the queries — rather than scattered through frontend fetch calls,
    because a page that assembles its own truth from six endpoints is a page
    whose numbers can disagree with each other. One document, one timestamp,
    internally consistent.
    """
    con = con or db.init()
    p = p or plat.load()
    now = _hkt_now()
    out: dict[str, Any] = {"generated_at": now.isoformat(),
                           "platform": p.name,
                           "snapshot": _snapshot_state()}

    # -- liveness ---------------------------------------------------------
    hb = None
    try:
        raw = p.cache.get("scheduler:heartbeat")
        hb = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        pass
    if not hb:
        try:
            rows = p.state.q(
                "SELECT started_at, platform FROM orch_runs WHERE kind='monitor' "
                "ORDER BY started_at DESC LIMIT 1")
            if rows:
                at = datetime.fromisoformat(str(rows[0]["started_at"]))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                hb = {
                    "at_utc": at.astimezone(timezone.utc).isoformat(),
                    "at_hkt": at.astimezone(config.TZ).isoformat(),
                    "platform": rows[0].get("platform") or p.name,
                    "venue": "paper",
                    "source": "rds-monitor",
                }
        except Exception:  # noqa: BLE001
            pass
    age = None
    if hb:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(hb["at_utc"])).total_seconds()
    import re as _re
    def _scrub(text: str) -> str:
        # Bucket names and paths can embed the cloud account id; the dashboard
        # may be screenshotted or shared, so identifiers never leave the server.
        text = _re.sub(r"tos://[\w.-]+", "tos://<bucket>", text or "")
        return _re.sub(r"/Users/[\w.-]+", "~", text)
    out["alive"] = {"heartbeat": hb, "age_s": age,
                    "ok": age is not None and age < 1800,
                    **_port_health(p, _scrub)}

    # -- run history ------------------------------------------------------
    out["runs"] = [dict(r) for r in p.state.q(
        "SELECT run_id, as_of, kind, platform, ok, started_at, ended_at, "
        "error, calls FROM orch_runs ORDER BY started_at DESC LIMIT 30")]
    # Same rule as the run-history card: a filled gap stops being a gap.
    out["gaps"] = [r["as_of"] for r in p.state.q(
        "SELECT as_of FROM orch_runs g WHERE run_id LIKE 'gap-%' "
        "AND NOT EXISTS (SELECT 1 FROM orch_runs w WHERE w.kind='weekly' "
        "  AND w.ok=1 AND w.as_of=g.as_of) ORDER BY as_of")]

    out["weekly"] = weekly_block(p, con)

    # -- the period spine: every week, oldest first -----------------------
    # The ladder. Without this the page can only ever describe the newest run,
    # and the four-week roll (每周 25%，第五周换第一周) has nowhere to be drawn —
    # which is why it lived in a tooltip. See `ideagen/periods.py`.
    try:
        out["periods"] = periods.spine(con, p)
    except Exception as e:  # noqa: BLE001 — the axis must not take the page down
        out["periods"] = []
        out["periods_error"] = f"{type(e).__name__}: {e}"

    # -- books: equity curves + open positions ---------------------------
    books = []
    for b in db.q(con, "SELECT book_id, label, capital FROM books "
                       "WHERE book_id LIKE 'sel-%' ORDER BY book_id"):
        eq = [{"d": m["d"], "equity": m["equity"]} for m in db.q(
            con, "SELECT d, equity FROM equity WHERE book_id=? ORDER BY d",
            (b["book_id"],))]
        # `avg_px` is the entry; the latest mark lives in `mtm`, one row per
        # position per session — joined on the max marked date so an unfilled
        # order shows an honest NULL rather than a stale guess.
        pos = [dict(x) for x in db.q(
            con, "SELECT p.code, p.qty, p.avg_px AS entry_px, m.px AS last_px, "
                 # `as_of` is the vintage, `opened_d` the session it filled in.
                 # The page groups the ladder on the first and dates the fill
                 # with the second; before this column shipped it had only the
                 # second and had to apologise in prose for the difference.
                 "p.stop_px, p.take_px, p.opened_d, p.as_of, m.upnl AS unrealized, "
                 "i.thesis, i.theme_id, i.tool_desc AS instrument_name "
                 "FROM positions p "
                 "LEFT JOIN ideas i USING(idea_uid) "
                 "LEFT JOIN mtm m ON m.pos_id = p.pos_id AND m.d = "
                 "  (SELECT MAX(d) FROM mtm WHERE pos_id = p.pos_id) "
                 "WHERE p.book_id=? AND p.status='open' ORDER BY p.code",
            (b["book_id"],))]
        latest_batch = db.q1(
            con, "SELECT i.batch_id, i.as_of FROM orders o "
                 "JOIN ideas i USING(idea_uid) WHERE o.book_id=? "
                 "ORDER BY i.as_of DESC LIMIT 1", (b["book_id"],))
        # When this book first held anything, over open *and* closed positions.
        # `booked_as_of` is the label of the newest batch of ideas, not a date
        # anything was bought on, and the page was reading it as one — which
        # dated the book a week late and, because that date is the numerator of
        # "week N of about 17", quietly understated how far the experiment had
        # got. Closed positions have to count: a book whose first tranche has
        # since been sold did not start later than it did.
        first_open = db.q1(
            con, "SELECT MIN(opened_d) d FROM positions WHERE book_id=?",
            (b["book_id"],))
        # A win is a close that made money after costs; ties count as losses,
        # because a method that only breaks even must not clear Jon's >50% bar
        # on rounding. win_rate stays null until something has actually closed —
        # 0% would read as "always loses" when the truth is "no verdicts yet".
        # 这个组合实际在几期出过手。面板反复写着「各策略同池同期，净值差异
        # 仅来自选取策略」，而 2026-09-05 实测**十个组合里只有两个覆盖全部六期**：
        # buy_all（全量基准，所有比较的参照）缺 08-12 与 08-19 两期，
        # ai_native 只有三期。选取判决当时是正常做出来的（buy_all 那两期分别选了
        # 74 和 75 条），单子却一张没下——中间安静地丢了。
        # 参照自己缺了两期，那两期里出过手的组合就不是在和它比同一段时间。
        periods_acted = db.q1(
            con, "SELECT COUNT(DISTINCT as_of) n FROM positions WHERE book_id=?",
            (b["book_id"],))
        # `same_day` 是这个胜率能不能被当成业绩读的前提。2026-09-05 实测：
        # 十个组合的 202 笔已平仓**全部**是同日开平（补跑把五期订单挤在撮合
        # 当天，horizon_end 已经在过去，于是建仓当天就按「到期」平掉）。同日
        # 往返在这套成本模型下必亏点差——毛利为正的笔数是 0，不是 0 附近。
        # 所以面板上那个红色的「胜率 0%（0/202）」是算术必然，不是选股结论，
        # 而它是页面上最容易被当成业绩念出来的数字之一。
        realized = db.q1(con, "SELECT COALESCE(SUM(realized),0) r, COUNT(*) n, "
                              "COALESCE(SUM(realized > 0), 0) w, "
                              "COALESCE(SUM(closed_d = opened_d), 0) same_day "
                              "FROM positions WHERE book_id=? AND status='closed'",
                         (b["book_id"],))
        exits = {x["exit_reason"]: x["n"] for x in db.q(
            con, "SELECT exit_reason, COUNT(*) n FROM positions "
                 "WHERE book_id=? AND status='closed' GROUP BY exit_reason",
            (b["book_id"],))}
        books.append({"book_id": b["book_id"],
                      "booked_batch": (latest_batch["batch_id"]
                                       if latest_batch else None),
                      "booked_as_of": (latest_batch["as_of"]
                                       if latest_batch else None),
                      "first_opened_d": (first_open["d"] if first_open
                                         else None),
                      "selector": b["book_id"].replace("sel-", ""),
                      "capital": b["capital"], "equity": eq,
                      "open_positions": pos,
                      "realized": realized["r"] if realized else 0,
                      "closed_n": realized["n"] if realized else 0,
                      "wins": realized["w"] if realized else 0,
                      "closed_same_day": (realized["same_day"]
                                          if realized else 0),
                      "periods_acted": (periods_acted["n"]
                                        if periods_acted else 0),
                      "win_rate": (round(realized["w"] / realized["n"], 4)
                                   if realized and realized["n"] else None),
                      # Orders placed but not yet filled, and orders that
                      # expired without ever filling. A book that shows nine
                      # positions after being handed thirty picks is not
                      # obviously broken or obviously fine — these two numbers
                      # are what tell the difference between "waiting for the
                      # close" and "ran out of cash".
                      "pending_orders": (db.q1(
                          con, "SELECT COUNT(*) n FROM orders "
                               "WHERE book_id=? AND status='pending'",
                          (b["book_id"],)) or {"n": 0})["n"],
                      "expired_orders": (db.q1(
                          con, "SELECT COUNT(*) n FROM orders "
                               "WHERE book_id=? AND status='expired'",
                          (b["book_id"],)) or {"n": 0})["n"],
                      "exits": exits})
    out["books"] = books
    out["books_aggregate"] = _books_aggregate(books)
    # 从没建成仓的批次。它们不是「没跑」——选取判决正常做出来了，批次也写下了，
    # 只是校验没过就停在 draft。2026-09-05 实测九个（外加两个 PROBE 测试批次），
    # 合计 237 条想法从未建仓，而卡住它们的是 `ref_price_present` /
    # `ref_price_dated`：**一个批次里只要有两条想法拿不到参考价，整批七十四条
    # 都不建仓**。而建仓那一步本来就会跳过拿不到价格的标的，两者严格程度不一致。
    #
    # 后果打在对照实验的地基上：buy_all（全量基准）因此缺两期。页面在别处会
    # 主动报「1 期永久缺失」，这一类却完全看不见，所以这里把它交出去。
    out["stuck_batches"] = [
        {"batch_id": r["batch_id"], "as_of": r["as_of"],
         "n_ideas": r["n_ideas"], "status": r["status"],
         "blocked_by": sorted({
             c.get("check") for c in
             (_json_value(r["validation"], {}) or {}).get("checks", [])
             if not c.get("ok") and c.get("severity") == "error"})}
        for r in db.q(con, "SELECT batch_id, as_of, n_ideas, status, validation "
                           "FROM batches WHERE status!='traded' ORDER BY as_of, batch_id")]
    # The cash rate the page quotes when it explains where the un-deployed
    # money's return came from. The page used to print a literal "3.72%" and
    # label it "current" — that literal is config.RISK_FREE_ANNUAL, the
    # *fallback*, so whenever the shelf actually carried a money-market yield
    # the page was quoting a number the system was not using. Ship the rate
    # that was used, and say which of the two it is.
    try:
        from .sources import olive as _olive
        _live = _olive.cash_yield(con, "USD")
    except Exception:  # noqa: BLE001 - the rate is a caption, never a blocker
        _live = None
    out["cash_rate"] = {
        "annual": _live if _live is not None else config.RISK_FREE_ANNUAL,
        "source": "shelf_median_7d" if _live is not None else "config_fallback",
    }
    # The do-nothing alternative, over exactly the dates the books were marked.
    # Jon's frame is not "did it go up" but "did the machinery beat parking the
    # same cash in SPY" — without this series the aggregate return has no zero.
    span = db.q1(con, "SELECT MIN(d) a, MAX(d) b FROM equity")
    if span and span["a"]:
        out["benchmark"] = {
            "code": "US.SPY",
            "series": [{"d": r["d"], "close": r["close"]} for r in db.q(
                con, "SELECT d, close FROM prices WHERE code='US.SPY' "
                     "AND d BETWEEN ? AND ? ORDER BY d",
                (span["a"], span["b"]))]}
    if not books:
        try:
            out["books"] = cloud_paper.state_view(p.state)
        except Exception:  # noqa: BLE001 - pre-migration deployments stay readable
            out["books"] = []
    out["backtest"] = _backtest_state(p)
    try:
        out["shelf"] = shelf_store.dashboard_state(
            p.state,
            as_of=now.date(),
            show_names=(
                os.environ.get("IDEAGEN_DASH_SHOW_LICENSED_NAMES") or ""
            ).strip().lower() in ("1", "true", "yes", "on"),
        )
    except Exception:  # noqa: BLE001 - old schema or unavailable state
        out["shelf"] = {}

    # -- feeds ------------------------------------------------------------
    # This used to be `LIMIT 12` — a cap on *rows*, on a table whose rows are
    # (period x feed x attempt). One busy week fills it by itself, and every
    # earlier period silently vanished: the page's 研报来源 table has a 期次
    # column that could only ever hold one value, so a reader looking at an
    # older vintage was told its corpus did not exist. Bound it by period
    # instead, which is the axis the page actually reads it on.
    out["feeds"] = [dict(r) for r in p.state.q(
        "SELECT feed, kind, as_of, n_rows, ok, error FROM feed_runs "
        "WHERE as_of IN (SELECT as_of FROM ("
        "  SELECT DISTINCT as_of FROM feed_runs ORDER BY as_of DESC LIMIT 26"
        ") recent) "
        "ORDER BY as_of DESC, run_id DESC, feed ASC")]

    # -- which periods have a browsable corpus ----------------------------
    # feed_runs only records a *fetch*. A week that reused an already-ingested
    # corpus (2026-08-26) registers no corpus row at all, yet its documents are
    # right there and `/api/corpus?as_of=` serves them. Counting the stored
    # documents per period window is the honest answer to "which periods can I
    # open", and it is the one the drawer's 往期 list is built from.
    try:
        out["corpus_periods"] = corpus_period_index(
            con, p, [r.get("as_of") for r in out.get("periods") or []]
            + [r["as_of"] for r in out["feeds"] if r.get("kind") == "corpus"])
    except Exception as e:  # noqa: BLE001 — an index must not take the page down
        out["corpus_periods"] = []
        out["corpus_periods_error"] = f"{type(e).__name__}: {e}"

    # -- schedule: the server says when, the page only displays -----------
    try:
        from .scheduler import MONITOR_INTERVAL_S, TICK_INTERVAL_S, weekly_period
        period_start, trigger = weekly_period(now)
        out["schedule"] = {"current_period": period_start.date().isoformat()
                           if hasattr(period_start, "date") else str(period_start),
                           "trigger_hkt": trigger.isoformat(),
                           "tick_interval_s": TICK_INTERVAL_S,
                           "monitor_interval_s": MONITOR_INTERVAL_S}
    except Exception:  # noqa: BLE001
        out["schedule"] = None

    # -- static registers -------------------------------------------------
    out["assumptions"] = [dict(zip(("claim", "nature", "status"), a))
                          for a in ASSUMPTIONS]
    out["fixes"] = [dict(zip(("issue", "state", "how"), f)) for f in FIXES]
    return out


# ---------------------------------------------------------------- corpus API
def corpus_period_index(con=None, p=None,
                        as_ofs: list[str] | None = None) -> list[dict]:
    """How many stored documents each period can show, oldest period last.

    Same window rule as `corpus_list`: a period's corpus is the three days
    ending on its `as_of`. Counted from whichever store actually holds the
    documents, so the answer matches what clicking through will render.
    """
    from datetime import date as _date, timedelta as _td
    p = p or plat.load()
    by_day: dict[str, int] = {}
    try:
        rows = [dict(r) for r in p.state.q(
            "SELECT published_d, COUNT(*) AS n "
            "FROM corpus_documents GROUP BY published_d")]
        by_day = {r["published_d"]: int(r["n"] or 0) for r in rows
                  if r.get("published_d")}
    except Exception:  # noqa: BLE001 - old schema or unavailable state
        by_day = {}
    if not by_day:
        con = con or db.init()
        by_day = {r["published_d"]: int(r["n"] or 0)
                  for r in (dict(x) for x in db.q(
                      con, "SELECT published_d, COUNT(*) AS n FROM documents "
                           "GROUP BY published_d"))
                  if r.get("published_d")}

    seen, out = set(), []
    for as_of in sorted({a for a in (as_ofs or []) if a}, reverse=True):
        if as_of in seen:
            continue
        seen.add(as_of)
        try:
            aof = _date.fromisoformat(as_of)
        except (TypeError, ValueError):
            continue
        days = [(aof - _td(days=i)).isoformat() for i in range(3)]
        n = sum(by_day.get(d, 0) for d in days)
        if n:
            out.append({"as_of": as_of, "n": n, "window": days})
    return out


def corpus_list(con=None, as_of: str | None = None, p=None) -> dict[str, Any]:
    """Every stored document for one period's window — the shelf itself.

    The feed table said "841 条 · 正常" and stopped there, which is a claim
    without an exhibit: the operator asked, reasonably, to see what was actually
    stored. Titles and summaries are ours to show (they are the working corpus);
    what stays out of any public artifact is the verbatim body, which is
    licensed material — served only per-document, locally, on demand.
    """
    p = p or plat.load()
    portable = []
    try:
        if not as_of:
            latest = p.state.q(
                "SELECT MAX(published_d) AS d FROM corpus_documents")
            as_of = latest[0]["d"] if latest else None
        if as_of:
            from datetime import date as _date, timedelta as _td
            aof = _date.fromisoformat(as_of)
            days = [(aof - _td(days=i)).isoformat() for i in range(3)]
            portable = p.state.q(
                "SELECT doc_id, published_d, title, "
                "COALESCE(institution, line) AS institution, tier, summary, "
                "content_hash, retrieval, body "
                "FROM corpus_documents WHERE published_d IN (%s) "
                "ORDER BY published_d DESC, tier, doc_id"
                % ",".join("?" * len(days)), days)
            if portable:
                docs = [{
                    **{key: row.get(key) for key in (
                        "doc_id", "published_d", "title", "institution",
                        "tier", "retrieval")},
                    "summary": str(row.get("summary") or "")[:240],
                    "sha": str(row.get("content_hash") or "")[:12],
                    "body_len": len(str(row.get("body") or "")),
                } for row in portable]
                return {"as_of": as_of, "window": days, "n": len(docs),
                        "docs": docs}
    except Exception:  # noqa: BLE001 - local legacy state remains supported
        pass

    con = con or db.init()
    if not as_of:
        r = db.q1(con, "SELECT MAX(published_d) d FROM documents")
        as_of = r["d"] if r else None
    from datetime import date as _date, timedelta as _td
    # A caller can hand over anything; an unparseable date should not take the
    # request down. Fall back to the latest period and say so, so a wrong
    # parameter shows up as a labelled answer instead of a dead connection.
    bad_as_of = None
    try:
        aof = _date.fromisoformat(as_of or "")
    except (TypeError, ValueError):
        bad_as_of = as_of
        r = db.q1(con, "SELECT MAX(published_d) d FROM documents")
        as_of = (r["d"] if r else None) or _date.today().isoformat()
        aof = _date.fromisoformat(as_of)
    days = [(aof - _td(days=i)).isoformat() for i in range(3)]
    rows = db.q(con,
                "SELECT doc_id, published_d, title, "
                "COALESCE(institution, line) AS institution, tier, "
                "substr(COALESCE(summary,''),1,240) AS summary, "
                "substr(COALESCE(content_hash,''),1,12) AS sha, retrieval, "
                "length(COALESCE(body,'')) AS body_len "
                "FROM documents WHERE published_d IN (%s) "
                "ORDER BY published_d DESC, tier, doc_id"
                % ",".join("?" * len(days)), days)
    return {"as_of": as_of, "window": days, "n": len(rows),
            **({"as_of_invalid": str(bad_as_of)} if bad_as_of else {}),
            "docs": [dict(r) for r in rows]}


def doc_detail(con=None, doc_id: str = "", p=None) -> dict[str, Any]:
    """One document in full, for local audit."""
    p = p or plat.load()
    try:
        rows = p.state.q(
            "SELECT doc_id, published_d, title, "
            "COALESCE(institution, line) AS institution, tier, summary, "
            "body, content_hash, retrieval FROM corpus_documents "
            "WHERE doc_id=?",
            (doc_id,))
        if rows:
            d = dict(rows[0])
            d["body_len"] = len(d.get("body") or "")
            # The protected dashboard can audit title, summary, hash and
            # retrieval receipt. Verbatim licensed text remains server-side for
            # scoring and is never copied into a browser response.
            d["body"] = ""
            return d
    except Exception:  # noqa: BLE001 - local legacy state remains supported
        pass

    con = con or db.init()
    r = db.q1(con, "SELECT doc_id, published_d, title, "
                   "COALESCE(institution, line) AS institution, tier, summary, "
                   "body, content_hash, retrieval FROM documents WHERE doc_id=?",
              (doc_id,))
    if not r:
        return {"error": f"没有 doc_id={doc_id!r} 的文档"}
    d = dict(r)
    d["body_len"] = len(d.get("body") or "")
    return d


# ---------------------------------------------------------------------------
# proposals — what each method actually wrote, before the merge
# ---------------------------------------------------------------------------
#: Parsed generator artifacts, per completed run. Reading all four out of
#: object storage costs about four seconds, and the drawer that needs them is
#: opened once per instrument a reader is curious about — so the whole run is
#: indexed on the first call and every later instrument is free. A finished
#: run's artifacts do not change, which is what makes caching them honest
#: rather than a staleness bug waiting to happen.
_PROPOSAL_INDEX: dict[str, dict[str, list[dict[str, Any]]]] = {}
_PROPOSAL_INDEX_MAX = 4


def _proposal_index(p, run: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    key = str(run["run_id"])
    hit = _PROPOSAL_INDEX.get(key)
    if hit is not None:
        return hit, []
    idx: dict[str, list[dict[str, Any]]] = {}
    #: Methods whose artifact could not be read, with why. Skipping them
    #: silently is how "this node cannot reach the store" reaches the reader as
    #: 「本期生成产物里没有这条的提案记录」 plus a guess about carried-over
    #: holdings — a specific explanation, offered where none is known.
    unread: list[str] = []
    from . import platform as _plat
    # 方法名从注册表取，不写死。写死的那四条会漏掉两类：内置的新方法
    # （lookthrough 是 2026-09-05 加的第五种），以及 PM 写一条准则派生出来的
    # 那一条（`carl_constraint@pm-xxxx`）。漏掉的后果不是少一行，是**答错**：
    # 只有它们提过的标的，抽屉会说「本期生成产物里没有这条的提案记录」，
    # 再附一句「可能是上期滚过来的持仓」——一个凭空给出的解释。
    # 本期没跑过的方法走 BlobMissing 那条安静跳过的路，多列几个不花代价。
    from . import strategy as _strategy
    from . import strategies as _strategies          # noqa: F401  触发注册
    methods = sorted(m["name"] for m in _strategy.available("idea_generator"))
    for method in methods:
        # Not `key`: that name is the cache key for the whole run, three lines
        # up, and shadowing it here filed the index under the last artifact
        # path instead — the cache then never hit and every request re-read
        # four artifacts.
        art_key = (f"runs/{run['as_of']}/{run['run_id']}/"
                   f"B_generators/{method}.json")
        try:
            art = json.loads(p.blobs.get(art_key))
        except _plat.BlobMissing:
            # This run did not use that method — the normal case for a period
            # whose set of generators differs, and a fact about the run that
            # will not change. Skipped quietly, and the index still caches.
            continue
        except Exception as e:  # noqa: BLE001
            unread.append(f"{method}：读不到产物存储"
                          f"（{type(e).__name__}: {e}）")
            continue
        if not isinstance(art, dict):
            unread.append(f"{method}：产物读到了但不是一份记录")
            continue
        for item in (art.get("produced") or []):
            iid = str(item.get("instrument_id") or "")
            if not iid:
                continue
            idx.setdefault(iid.split(".")[-1].upper(), []).append({
                "method": item.get("method") or method,
                "topic_id": item.get("topic_id"),
                "thesis": item.get("thesis"),
                "upside_pct": item.get("upside_pct"),
                "downside_pct": item.get("downside_pct"),
                "p_up": item.get("p_up"), "p_base": item.get("p_base"),
                "p_down": item.get("p_down"),
                "vehicle": item.get("vehicle"), "exposure": item.get("exposure"),
                "horizon_days": item.get("horizon_days"),
                "citations": [str(c) for c in (item.get("citations") or []) if c],
                "bad_citations": item.get("bad_citations"),
                "instrument_name": item.get("instrument_name"),
            })
    if len(_PROPOSAL_INDEX) >= _PROPOSAL_INDEX_MAX:
        _PROPOSAL_INDEX.pop(next(iter(_PROPOSAL_INDEX)), None)
    # A read failure is not cached: it is a property of this moment, and
    # caching it would keep answering "no proposals" long after the store came
    # back. A genuinely empty index is cached as before.
    if not unread:
        _PROPOSAL_INDEX[key] = idx
    return idx, unread


def proposals_for(instrument: str, run_id: str | None = None,
                  p=None, con=None) -> dict[str, Any]:
    """Every individual proposal for one instrument, before they were merged.

    The candidate pool shows one row per instrument: a merged thesis and the
    median of the odds. That is the right unit for selection and the wrong
    unit for the question "how did this idea come about" — four methods wrote
    four different arguments for the same ticker, and the merge is exactly
    where those differences stop being visible.

    So this reads them back out of the run's own generator artifacts, each with
    the reports it cited, and resolves those citations to titles so the chain
    from idea to source document is clickable rather than a bare id.
    """
    p = p or plat.load()
    con = con or db.init()
    rows = (p.state.q("SELECT run_id, as_of FROM orch_runs WHERE run_id=?",
                      (run_id,)) if run_id else
            p.state.q("SELECT run_id, as_of FROM orch_runs "
                      "WHERE kind='weekly' AND ok=1 ORDER BY as_of DESC LIMIT 1"))
    if not rows:
        return {"error": "没有可读的运行记录"}
    run = dict(rows[0])
    want = str(instrument or "").strip()
    if not want:
        return {"error": "缺少标的代码"}
    bare = want.split(".")[-1].upper()

    index, unread = _proposal_index(p, run)
    found = list(index.get(bare) or [])
    cite_ids: set[str] = set()
    for item in found:
        cite_ids.update(item.get("citations") or [])

    # Citations are ids until they carry a title; a bare `ib:103758` is not a
    # link to anything a reader can check.
    docs: dict[str, Any] = {}
    if cite_ids:
        ids = sorted(cite_ids)
        try:
            for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
                for r in db.q(con,
                              "SELECT doc_id, title, published_d, "
                              "COALESCE(institution, line) AS institution, tier "
                              "FROM documents WHERE doc_id IN (%s)"
                              % ",".join("?" * len(chunk)), chunk):
                    docs[r["doc_id"]] = dict(r)
        except Exception:  # noqa: BLE001 — titles are a nicety, ids still work
            pass
    found.sort(key=lambda x: (str(x.get("method")), str(x.get("topic_id"))))
    return {"run_id": run["run_id"], "as_of": run["as_of"],
            "instrument": bare, "n": len(found),
            "proposals": found, "docs": docs,
            # Present only when something could not be read. An empty list of
            # proposals means one thing with this absent and another with it
            # here, and the page has to be able to tell them apart.
            **({"unread": unread} if unread else {})}
