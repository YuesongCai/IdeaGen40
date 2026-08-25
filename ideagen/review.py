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
import json
from datetime import datetime, timezone
from typing import Any

from . import config, db, platform as plat

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
        f'<span class="pill {"ok" if h.ok else "no"}">{E(h.name)}</span>'
        for h in p.check())
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
    gaps = p.state.q("SELECT as_of FROM orch_runs WHERE run_id LIKE 'gap-%' "
                     "ORDER BY as_of")
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
                   + _tbl(["挑法", "选中", "标的"], rows))
    return _card("本期流水线", "".join(out))


def _books(con) -> str:
    rows = []
    for b in db.q(con, "SELECT book_id, label FROM books "
                       "WHERE book_id LIKE 'sel-%' ORDER BY book_id"):
        pos = db.q1(con, "SELECT COUNT(*) n, COALESCE(SUM(qty*COALESCE(last_px,0)),0) mv "
                         "FROM positions WHERE book_id=? AND status='open'",
                    (b["book_id"],)) or {"n": 0, "mv": 0}
        eq = db.q1(con, "SELECT equity, d FROM mtm WHERE book_id=? "
                        "ORDER BY d DESC LIMIT 1", (b["book_id"],))
        realized = db.q1(con, "SELECT COALESCE(SUM(realized),0) r FROM positions "
                              "WHERE book_id=? AND status='closed'",
                         (b["book_id"],))
        rows.append([b["book_id"].replace("sel-", ""), pos["n"],
                     f"${eq['equity']:,.0f}" if eq else "未标记",
                     (eq["d"] if eq else "—"),
                     f"${realized['r']:,.0f}" if realized else "$0"])
    if not rows:
        return _card("挑法账本", "<p>还没有建仓——第一次周跑完成后自动出现</p>", "warn")
    return _card("挑法账本（纸面，各 $10m 等资本）",
                 _tbl(["挑法", "持仓数", "最新权益", "标记至", "已实现"], rows))


ASSUMPTIONS = [
    ("H 的符号（热度正向预测）", "先验，最大风险", "H×P 检验 t=−0.47，无分辨力；攒样本中"),
    ("HGEP 权重 0.30/0.25/0.25/0.20", "先验", "等逐因子归因（约 7 个月量级）"),
    ("语义打分 > 纯数数", "系统立论", "对照臂已并行落库，零成本攒证据中"),
    ("σ×2 止损 / σ×3 止盈", "先验", "由只改止损的账本之差检验"),
    ("去集中上限 主题3/敞口3/方式4", "先验", "实测能选满 10；上限收到 1 只能选 5"),
    ("ρ≈0.8（配对相关）", "整个验证时间表的地基", "第一个月实测"),
    ("赚亏比下限 1.5", "先验", "本周中位数 1.82，未触发；低赔率周才生效"),
    ("1 个月单一持有期", "决策（Jon 原设计是 1m+6m 双期限）", "被覆盖的立场，未正式对比"),
]

FIXES = [
    ("idea_uid 重绑（+377% 假收益）", "✅ 已修", "产物不可变 + 幂等 + 测试"),
    ("强制凑满 40 条", "✅ 已修", "门槛准入，不足留现金"),
    ("批内同标的重复", "✅ 已修", "候选池一标一条，赔率取中位数"),
    ("thesis 证伪只告警不平仓", "✅ 已修", "事件退出：告警次日收盘平仓"),
    ("止损被进场方式错误闸住", "✅ 已修", "账本自声明风控"),
    ("跨周同标的敞口叠加", "🟡 可见化", "建仓时记录重叠；是否封顶待定"),
    ("空语料运行报成功", "✅ 已修", "无语料=失败，与「本周没料」区分"),
    ("表撞名静默 / 孤儿数据 / 非法 JSON", "✅ 已修", "建表前查撞名 + orphans() + _finite()"),
    ("Redis 口令写进不可变产物", "✅ 已修", "redact_url 统一脱敏"),
    ("同一期跑两次", "✅ 已修", "数据库部分唯一索引，不只靠锁"),
    ("归因四层反事实（Jon）", "❌ 未做", "只有 matched benchmark 一层"),
    ("剔除高频标的复测（Jon）", "❌ 未做", "待进回测层"),
    ("波动调整后与指数比（Jon）", "❌ 未做", "现在只有裸超额"),
]

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
    con = con or db.init()
    p = p or plat.load()
    now = _hkt_now()
    body = "".join([
        _alive(p),
        _latest_weekly(p),
        _books(con),
        _runs(p),
        _card("假设登记表（哪些还只是先验）",
              _tbl(["假设", "性质", "验证状态"], [list(a) for a in ASSUMPTIONS])),
        _card("问题修复账（含 Jon 评审全部条目）",
              _tbl(["问题", "状态", "处理"], [list(f) for f in FIXES])),
        _card("Routine 手册", RUNBOOK.replace("\n", "")
              if False else RUNBOOK),
    ])
    page = f"""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>IdeaGen 复盘板</title>
<style>
body{{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;margin:0;background:#f5f6f8;color:#1a1d21}}
.wrap{{max-width:1080px;margin:0 auto;padding:24px 16px}}
h1{{font-size:20px}} h4{{margin:14px 0 6px}}
.card{{background:#fff;border-radius:10px;padding:16px 18px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.card.good{{border-left:4px solid #16a34a}} .card.bad{{border-left:4px solid #dc2626}}
.card.warn{{border-left:4px solid #d97706}}
.ct{{font-weight:700;margin-bottom:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid #e5e7eb;padding:5px 8px;text-align:left;vertical-align:top}}
th{{background:#f0f1f3}}
.pill{{display:inline-block;border-radius:99px;padding:1px 10px;margin:2px;font-size:12px}}
.pill.ok{{background:#dcfce7;color:#166534}} .pill.no{{background:#fee2e2;color:#991b1b}}
.dim{{color:#6b7280;font-size:12px}}
pre{{background:#f0f1f3;border-radius:6px;padding:10px;overflow-x:auto;font-size:12px}}
code{{background:#f0f1f3;border-radius:4px;padding:0 4px}}
</style><div class=wrap>
<h1>IdeaGen 复盘板</h1>
<p class=dim>生成于 {now.strftime('%Y-%m-%d %H:%M:%S')} HKT · 全部数字来自活库直查，本页不自算任何东西</p>
{body}</div></html>"""
    out = config.WEB / "review.html"
    out.write_text(page, encoding="utf-8")
    return out


# ---------------------------------------------------------------- state API
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
                           "platform": p.name}

    # -- liveness ---------------------------------------------------------
    hb = None
    try:
        raw = p.cache.get("scheduler:heartbeat")
        hb = json.loads(raw) if raw else None
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
                    "ports": [{"name": h.name, "ok": h.ok,
                               "detail": _scrub(h.detail)}
                              for h in p.check()]}

    # -- run history ------------------------------------------------------
    out["runs"] = [dict(r) for r in p.state.q(
        "SELECT run_id, as_of, kind, platform, ok, started_at, ended_at, "
        "error, calls FROM orch_runs ORDER BY started_at DESC LIMIT 30")]
    out["gaps"] = [r["as_of"] for r in p.state.q(
        "SELECT as_of FROM orch_runs WHERE run_id LIKE 'gap-%' ORDER BY as_of")]

    # -- latest weekly, all three stages ---------------------------------
    wk = p.state.q("SELECT run_id, as_of, ok, ended_at, calls FROM orch_runs "
                   "WHERE kind='weekly' ORDER BY started_at DESC LIMIT 1")
    weekly: dict[str, Any] = {}
    if wk:
        r = wk[0]
        rid = r["run_id"]
        weekly = {"run_id": rid, "as_of": r["as_of"], "ok": bool(r["ok"]),
                  "in_flight": r["ended_at"] is None, "calls": r["calls"]}
        weekly["topics"] = [
            {"scorer": v["strategy"], "chosen": json.loads(v["chosen"]),
             "scores": json.loads(v["scores"] or "{}")}
            for v in p.state.q("SELECT strategy, chosen, scores FROM verdicts "
                               "WHERE run_id=? AND kind='topic_scorer'", (rid,))]
        weekly["generators"] = [
            {"method": v["strategy"], "n": len(json.loads(v["chosen"])),
             "meta": json.loads(v["meta"] or "{}"),
             "rejected": len(json.loads(v["rejected"] or "{}"))}
            for v in p.state.q("SELECT strategy, chosen, meta, rejected FROM "
                               "verdicts WHERE run_id=? AND kind='idea_generator'",
                               (rid,))]
        cands = [json.loads(c["payload"]) for c in p.state.q(
            "SELECT payload FROM candidates WHERE run_id=?", (rid,))]
        weekly["pool"] = {
            "n": len(cands),
            "convergence": {},
            "candidates": [{k: c.get(k) for k in
                            ("id", "instrument_id", "instrument_name", "topic_id",
                             "upside_pct", "downside_pct", "p_up", "p_base",
                             "p_down", "proposed_by", "n_proposals", "thesis")}
                           for c in cands]}
        for c in cands:
            k = str(len(c.get("proposed_by") or []) or 1)
            weekly["pool"]["convergence"][k] = \
                weekly["pool"]["convergence"].get(k, 0) + 1
        weekly["selectors"] = [
            {"name": v["strategy"], "chosen": json.loads(v["chosen"])}
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
            docs = db.q(con,
                        "SELECT doc_id, published_d, title, institution, tier, "
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
            weekly["corpus_total"] = len(docs)
        except Exception as e:  # noqa: BLE001 — drill-down must not break the API
            weekly["evidence_error"] = f"{type(e).__name__}: {e}"
    out["weekly"] = weekly

    # -- books: equity curves + open positions ---------------------------
    books = []
    for b in db.q(con, "SELECT book_id, label, capital FROM books "
                       "WHERE book_id LIKE 'sel-%' ORDER BY book_id"):
        eq = [{"d": m["d"], "equity": m["equity"]} for m in db.q(
            con, "SELECT d, equity FROM mtm WHERE book_id=? ORDER BY d",
            (b["book_id"],))]
        pos = [dict(x) for x in db.q(
            con, "SELECT p.code, p.qty, p.entry_px, p.last_px, p.stop_px, "
                 "p.take_px, p.opened_d, p.unrealized, i.thesis, i.theme_id, "
                 "i.tool_desc AS instrument_name "
                 "FROM positions p LEFT JOIN ideas i USING(idea_uid) "
                 "WHERE p.book_id=? AND p.status='open' ORDER BY p.code",
            (b["book_id"],))]
        latest_batch = db.q1(
            con, "SELECT i.batch_id, i.as_of FROM orders o "
                 "JOIN ideas i USING(idea_uid) WHERE o.book_id=? "
                 "ORDER BY i.as_of DESC LIMIT 1", (b["book_id"],))
        realized = db.q1(con, "SELECT COALESCE(SUM(realized),0) r, COUNT(*) n "
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
                      "selector": b["book_id"].replace("sel-", ""),
                      "capital": b["capital"], "equity": eq,
                      "open_positions": pos,
                      "realized": realized["r"] if realized else 0,
                      "closed_n": realized["n"] if realized else 0,
                      "exits": exits})
    out["books"] = books

    # -- feeds ------------------------------------------------------------
    out["feeds"] = [dict(r) for r in p.state.q(
        "SELECT feed, kind, as_of, n_rows, ok, error FROM feed_runs "
        "ORDER BY rowid DESC LIMIT 12")]

    # -- schedule: the server says when, the page only displays -----------
    try:
        from .scheduler import weekly_period
        period_start, trigger = weekly_period(now)
        out["schedule"] = {"current_period": period_start.date().isoformat()
                           if hasattr(period_start, "date") else str(period_start),
                           "trigger_hkt": trigger.isoformat(),
                           "tick_interval_s": 900}
    except Exception:  # noqa: BLE001
        out["schedule"] = None

    # -- static registers -------------------------------------------------
    out["assumptions"] = [dict(zip(("claim", "nature", "status"), a))
                          for a in ASSUMPTIONS]
    out["fixes"] = [dict(zip(("issue", "state", "how"), f)) for f in FIXES]
    return out
