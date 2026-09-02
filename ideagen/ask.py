"""问当时的它 — answer questions strictly from what a run saw at decision time.

Jon's requirement, verbatim: the AI picked a topic after reading a hundred
reports, and he wants to ask "为什么读了 100 份报告选出了这个主题" — with the
hard constraint that the answer comes from what the system thought *then*, not
from an opinion invented now. Raw logs were rejected as 「还不是人话版本」.

The design that makes the answer honest is retrieval + strict grounding, not
memory: everything a decision saw is already stored immutably (run journal,
verdicts, per-run artifacts on the blob store, and the corpus rows behind the
evidence). `assemble_context` gathers ONLY that frozen material, each piece
tagged with its provenance; `answer` hands the material to the inference port
under a system prompt that forbids anything outside it and requires
「当时的记录里没有这一点」 when the material does not cover the question.

Two facts make the corpus reconstruction defensible rather than a guess:

* the run recorded `inputs_sha` — a hash over the exact doc-id list it scored —
  and this module recomputes the same recipe (publication window + ingested
  before the run started) and reports whether the hash matches; for run
  20260825T191952Z-aeaa3792 it does, byte for byte;
* the per-topic evidence counts recomputed with the scorer's own matching rule
  are cross-checked against the counts frozen in `A_topics.json`.

When either check fails the context says so instead of pretending.
"""

from __future__ import annotations

import json
import re
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config, db

#: The message the dashboard shows verbatim when this node cannot answer.
UNAVAILABLE_MSG = "本机为观察节点，追问需在生产实例上进行（或临时启用本机推理）"

#: Total character budget for the 当时材料 block handed to the model.
CONTEXT_CHAR_BUDGET = 30_000

#: Where every Q&A lands. The asks are part of the audit trail: a question the
#: operator needed to ask is a gap the artifacts did not answer on their own.
ASK_LOG = config.DATA / "ask_log.jsonl"

SUBJECT_KINDS = ("topic", "idea", "selector")

SYSTEM_PROMPT = """你是 IdeaGen 投研系统在某一次已封存的运行里的「当时的自己」。
用户会追问那次运行为什么做出某个决定。规则，逐条服从：

1. 你只能依据下面提供的「当时材料」作答。材料之外的任何知识——包括你对市场的
   一般了解、材料日期之后发生的事——一律不得使用。
2. 材料里没有的，就明说「当时的记录里没有这一点」，不要推测、不要补全。
3. 用第一人称复盘的口吻（「当时我看到……所以……」），像一个人诚实回忆自己
   当时的判断，但每个论断都必须能指回具体材料条目。
4. 引用材料时在句中标注条目编号，如 [M3]。回答结尾单独一行列出
   「引用材料：」加上你实际用到的全部编号。
5. 中文作答，说人话：面向基金经理，不堆术语，不贴原始日志。长度适中。"""


# ---------------------------------------------------------------------------
# scrubbing — same discipline as /api/journal: no host, no bucket, no home path
def _scrub_text(text: str) -> str:
    text = re.sub(r"tos://[\w.-]+", "tos://<bucket>", text or "")
    text = re.sub(r"/Users/[\w.-]+", "~", text)
    # Provider errors love to echo the cloud account id ("Your account
    # 30034... has not activated..."); the id never leaves the server.
    return re.sub(r"\baccount \d{6,}\b", "account <id>", text)


def scrub(obj: Any) -> Any:
    """Recursively remove machine identity from anything leaving the server."""
    if isinstance(obj, str):
        return _scrub_text(obj)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k != "host"}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
def _run_row(p, run_id: str | None) -> dict[str, Any] | None:
    if run_id:
        rows = p.state.q(
            "SELECT run_id, as_of, kind, ok, started_at, ended_at, calls, "
            "inputs_sha, data_classification FROM orch_runs "
            "WHERE run_id=?", (run_id,))
    else:
        rows = p.state.q(
            "SELECT run_id, as_of, kind, ok, started_at, ended_at, calls, "
            "inputs_sha, data_classification FROM orch_runs "
            "WHERE kind='weekly' AND ok=1 ORDER BY as_of DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def _artifact(p, run: dict[str, Any], name: str) -> dict | list | None:
    try:
        raw = p.blobs.get(f"runs/{run['as_of']}/{run['run_id']}/{name}")
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — a missing artifact is reported, not fatal
        return None


def _journal(p, run: dict[str, Any]) -> dict[str, Any] | None:
    j = _artifact(p, run, "journal.json")
    return j if isinstance(j, dict) else None


# ---------------------------------------------------------------------------
# corpus reconstruction, verified against the run's own inputs_sha
def _run_corpus(con, p, run: dict[str, Any]) -> tuple[list[dict], dict[str, Any]]:
    """The documents the run saw: published in the observation window and
    already ingested when the run started. Verified against inputs_sha."""
    as_of = date.fromisoformat(str(run["as_of"]))
    n = config.OBSERVATION_WINDOW_DAYS
    days = [(as_of - timedelta(days=i)).isoformat() for i in range(n)]
    rows = db.q(con,
                "SELECT doc_id, published_d, title, tier, line, institution, "
                "summary, body, ingested_at FROM documents "
                "WHERE published_d IN (%s) "
                "ORDER BY published_d DESC, tier" % ",".join("?" * len(days)),
                days)
    started = str(run.get("started_at") or "")
    docs = ([dict(r) for r in rows if str(r["ingested_at"]) <= started]
            if started else [dict(r) for r in rows])
    note = {"n_docs": len(docs), "window_days": n,
            "recipe": "发布日在观察窗口内、且运行启动前已入库的研报",
            "verified_sha": None}
    sha = run.get("inputs_sha")
    if sha:
        try:
            from . import strategy as strat
            ev = [r["event_id"] for r in p.state.q(
                "SELECT event_id FROM events WHERE as_of=?",
                (run["as_of"],))]
            cand = [r["candidate_id"] for r in p.state.q(
                "SELECT candidate_id FROM candidates WHERE run_id=?",
                (run["run_id"],))]
            # The weekly orchestrator hashed the injected candidate list, which
            # was empty at the inputs step; stored candidates are stage-B output.
            got = strat.RunContext.sha([d["doc_id"] for d in docs], [], [], ev)
            note["verified_sha"] = bool(got == sha)
            if got != sha and cand:
                note["verified_sha"] = None  # cannot re-derive the injected list
        except Exception:  # noqa: BLE001
            note["verified_sha"] = None
    return docs, note


def _match(text: str, terms) -> int:
    low = (text or "").lower()
    return sum(1 for t in terms if str(t).lower() in low)


def _topic_hits(docs: list[dict], theme) -> list[dict]:
    """The scorer's own evidence rule (topic_hgep, verbatim): match over
    title + summary + first 3000 chars of body; two term hits, or one hit in a
    document long enough to be scoreable."""
    out = []
    for d in docs:
        text = " ".join(filter(None, (d.get("title"), d.get("summary"),
                                      (d.get("body") or "")[:3000])))
        nhit = _match(text, theme.terms)
        if nhit >= 2 or (nhit == 1 and len(text) >= 400):
            out.append({**d, "hits": nhit})
    return out


# ---------------------------------------------------------------------------
def _mk(materials: list[dict], kind: str, title: str, source: str,
        text: str) -> str:
    mid = f"M{len(materials) + 1}"
    materials.append({"id": mid, "kind": kind, "title": _scrub_text(title),
                      "source": _scrub_text(source), "text": _scrub_text(text)})
    return mid


def _journal_material(materials, p, run) -> None:
    j = _journal(p, run)
    if not j:
        _mk(materials, "journal", "运行日志（缺失）",
            f"orch_runs · run_id={run['run_id']}",
            "这次运行没有可读的 journal 快照。")
        return
    lines = [f"运行 {run['run_id']}，{run['as_of']} 期，"
             f"{'完成' if j.get('ok') else '失败'}，"
             f"总时长 {j.get('duration_s')}s，模型调用 {run.get('calls')} 次。"]
    for s in j.get("steps") or []:
        at = str(s.get("at") or "")[11:19]
        kv = {k: v for k, v in s.items()
              if k not in ("n", "step", "at") and not isinstance(v, (dict, list))}
        lines.append(f"- {at} {s.get('step')}: "
                     + ", ".join(f"{k}={v}" for k, v in kv.items()))
    _mk(materials, "journal", "运行日志（逐步时间线）",
        f"blob runs/{run['as_of']}/{run['run_id']}/journal.json",
        "\n".join(lines))


def _doc_material(materials, d: dict, *, cited: bool, budget: int) -> int:
    """Append one corpus row as material; returns chars used (0 if skipped)."""
    summary = (d.get("summary") or "").strip() or (d.get("body") or "").strip()
    if len(summary) > 700:
        summary = summary[:700] + "……（截断）"
    text = (f"《{d.get('title') or ''}》\n"
            f"机构: {d.get('institution') or d.get('line') or '—'} · "
            f"发布日: {d.get('published_d')} · tier {d.get('tier')}\n"
            f"摘要: {summary or '（无摘要）'}")
    if len(text) > budget:
        return 0
    _mk(materials, "doc",
        ("证据研报" if cited else "同窗口研报") + f" {d.get('doc_id')}",
        f"corpus doc_id={d.get('doc_id')}", text)
    return len(text)


# ---------------------------------------------------------------------------
def _topic_context(materials, provenance_notes, con, p, run, topic_id):
    art = _artifact(p, run, "A_topics.json")
    counting = _artifact(p, run, "A_topics_counting.json")
    label = topic_id
    if isinstance(art, dict):
        row = (art.get("scores") or {}).get(topic_id)
        chosen = art.get("chosen") or []
        rej = (art.get("rejected") or {}).get(topic_id)
        meta = art.get("meta") or {}
        if row:
            label = row.get("label") or topic_id
            lines = [
                f"筛选A（{art.get('strategy')} v{art.get('version')}）对 "
                f"{topic_id}（{label}）的打分：综合 {row.get('score')} = "
                f"0.30×H热度({row.get('H')}) + 0.25×G分歧({row.get('G')}) + "
                f"0.25×E实据({row.get('E')}) + 0.20×(100−P已定价({row.get('P')}))。",
                f"证据 {row.get('n_evidence')} 条，来自 "
                f"{row.get('n_institutions')} 家机构；P 来源 "
                f"{row.get('p_source')}（neutral_default = 中性默认 50，"
                f"不是判断）；量尺标的 {row.get('indicator')}。",
                f"本期注册主题 {meta.get('registered_topics')} 个、"
                f"有证据 {meta.get('topics_with_evidence')} 个、"
                f"最响主题证据数 {meta.get('loudest_count')}，取前 "
                f"{meta.get('top_n')} 名。",
                (f"结果：入选，位列 {chosen.index(topic_id) + 1}/{len(chosen)}。"
                 if topic_id in chosen else
                 f"结果：落选（{rej or '未进前列'}）。"),
                "同场对比（全部主题的综合分）: " + "; ".join(
                    f"{tid} {sc.get('score')}"
                    for tid, sc in sorted((art.get("scores") or {}).items(),
                                          key=lambda kv: -(kv[1].get("score") or 0))),
            ]
            # Runs from 2026-09-03 onward freeze the exact evidence set in the
            # verdict itself. When the list exists the run names its own
            # sources; older runs rely on the reconstruction below (which is
            # verified against n_evidence, not assumed).
            if row.get("doc_ids"):
                ids = [str(x) for x in row["doc_ids"] if x]
                lines.append(
                    f"当期打分实际使用的证据文档（共 {len(ids)} 篇，按命中强度"
                    f"排序，打分时冻结）：" + ", ".join(ids))
            _mk(materials, "verdict", f"筛选A 打分明细 · {topic_id}",
                f"blob runs/{run['as_of']}/{run['run_id']}/A_topics.json",
                "\n".join(lines))
        else:
            _mk(materials, "verdict", f"筛选A 打分明细 · {topic_id}",
                f"blob runs/{run['as_of']}/{run['run_id']}/A_topics.json",
                f"筛选A 的打分表里没有 {topic_id} 这一行"
                "（可能当期没有词表证据，未参与打分）。")
    else:
        provenance_notes.append("A_topics.json 产物读不到——打分明细缺失")
    if isinstance(counting, dict):
        crow = (counting.get("scores") or {}).get(topic_id) or {}
        cch = counting.get("chosen") or []
        crej = (counting.get("rejected") or {}).get(topic_id)
        _mk(materials, "counting", f"纯数数对照臂 · {topic_id}",
            f"blob runs/{run['as_of']}/{run['run_id']}/A_topics_counting.json",
            f"对照臂只数提及篇数：{topic_id} 被提及 "
            f"{crow.get('mentions', 0)} 篇，"
            + (f"入选（位列 {cch.index(topic_id) + 1}/{len(cch)}）。"
               if topic_id in cch else f"未入选（{crej or '未进前 5'}）。")
            + " 对照臂全榜: " + "; ".join(
                f"{tid} {sc.get('mentions')}"
                for tid, sc in sorted((counting.get("scores") or {}).items(),
                                      key=lambda kv: -(kv[1].get("mentions") or 0))))
    # -- the actual corpus rows behind the evidence -----------------------
    docs, corpus_note = _run_corpus(con, p, run)
    theme = _theme(run, topic_id)
    used = sum(len(m["text"]) for m in materials)
    budget = CONTEXT_CHAR_BUDGET - used
    n_included = 0
    if theme is not None and docs:
        hits = _topic_hits(docs, theme)
        corpus_note["n_evidence_recomputed"] = len(hits)
        if isinstance(art, dict):
            rec = ((art.get("scores") or {}).get(topic_id) or {}).get("n_evidence")
            corpus_note["n_evidence_recorded"] = rec
            corpus_note["evidence_count_matches"] = (
                rec is not None and rec == len(hits))
        hits.sort(key=lambda d: (-d["hits"], d.get("tier") or 3,
                                 d.get("published_d") or ""), )
        rest = [d for d in docs
                if d["doc_id"] not in {h["doc_id"] for h in hits}
                and int(d.get("tier") or 3) <= 1]
        for d in hits:
            spent = _doc_material(materials, d, cited=True, budget=budget)
            if not spent:
                break
            budget -= spent
            n_included += 1
        for d in rest[:10]:
            spent = _doc_material(materials, d, cited=False, budget=budget)
            if not spent:
                break
            budget -= spent
    elif theme is None:
        provenance_notes.append(
            f"主题词表里没有 {topic_id}（as-of 注册表未包含它），"
            "无法重建它的证据研报清单")
    return label, corpus_note


def _theme(run, topic_id):
    try:
        from . import lexicon
        as_of = date.fromisoformat(str(run["as_of"]))
        for t in lexicon.all_themes(as_of):
            if t.id == topic_id:
                return t
    except Exception:  # noqa: BLE001
        pass
    return None


def _idea_context(materials, provenance_notes, con, p, run, idea_id):
    rows = p.state.q(
        "SELECT candidate_id, payload FROM candidates "
        "WHERE run_id=? AND candidate_id=?", (run["run_id"], idea_id))
    payload: dict[str, Any] = {}
    if rows:
        payload = json.loads(rows[0]["payload"] or "{}")
        _mk(materials, "candidate", f"候选想法 · {idea_id}",
            f"candidates · run_id={run['run_id']} · candidate_id={idea_id}",
            json.dumps({k: payload.get(k) for k in (
                "instrument_id", "instrument_name", "vehicle", "exposure",
                "topic_id", "thesis", "upside_pct", "downside_pct", "p_up",
                "p_base", "p_down", "horizon_days", "proposed_by",
                "n_proposals", "theses", "citations")},
                ensure_ascii=False, indent=1))
    label = payload.get("instrument_id") or idea_id
    # method-specific reasoning from each proposing generator's artifact
    methods = list(payload.get("proposed_by") or [])
    if not rows and ":" in idea_id:
        methods = [idea_id.split(":", 1)[0]]
    reasoning_fields = ("anomaly", "motive", "constraint", "trigger", "chain",
                        "watch_variable", "falsifier", "implied_consensus",
                        "contradiction", "unexpressed")
    cited_ids: list[str] = list(payload.get("citations") or [])
    for m in methods:
        gart = _artifact(p, run, f"B_generators/{m}.json")
        if not isinstance(gart, dict):
            provenance_notes.append(f"B_generators/{m}.json 产物读不到")
            continue
        item = next(
            (it for it in (gart.get("produced") or [])
             if it.get("id") == idea_id
             or (it.get("instrument_id") == payload.get("instrument_id")
                 and (not payload.get("topic_id")
                      or it.get("topic_id") == payload.get("topic_id")))),
            None)
        if not item:
            provenance_notes.append(
                f"{m} 的生成记录里没有这条想法的明细")
            continue
        detail = {k: item.get(k) for k in (
            "thesis", "upside_pct", "downside_pct", "p_up", "p_base",
            "p_down", "citations") if item.get(k) is not None}
        detail.update({k: item.get(k) for k in reasoning_fields
                       if item.get(k) is not None})
        if not payload:
            payload = item
            label = item.get("instrument_id") or idea_id
        _mk(materials, "generator", f"生成记录 · {m} · {item.get('id')}",
            f"blob runs/{run['as_of']}/{run['run_id']}/B_generators/{m}.json",
            json.dumps(detail, ensure_ascii=False, indent=1))
        for c in item.get("citations") or []:
            if c not in cited_ids:
                cited_ids.append(c)
    # the actual corpus rows behind the idea's citations
    used = sum(len(m["text"]) for m in materials)
    budget = CONTEXT_CHAR_BUDGET - used
    for did in cited_ids:
        drow = db.q(con, "SELECT doc_id, published_d, title, tier, line, "
                         "institution, summary FROM documents WHERE doc_id=?",
                    (did,))
        if not drow:
            provenance_notes.append(f"引用的研报 {did} 在语料库里找不到")
            continue
        spent = _doc_material(materials, dict(drow[0]), cited=True,
                              budget=budget)
        budget -= spent
    # a smaller slice of its topic's scoring context
    tid = payload.get("topic_id")
    corpus_note: dict[str, Any] = {}
    if tid:
        art = _artifact(p, run, "A_topics.json")
        if isinstance(art, dict):
            row = (art.get("scores") or {}).get(tid) or {}
            chosen = art.get("chosen") or []
            _mk(materials, "verdict", f"所属主题 · {tid}",
                f"blob runs/{run['as_of']}/{run['run_id']}/A_topics.json",
                f"这条想法属于主题 {tid}（{row.get('label') or tid}），"
                f"筛选A 综合分 {row.get('score')}，"
                f"证据 {row.get('n_evidence')} 条 / "
                f"{row.get('n_institutions')} 家机构，"
                + (f"当期入选（位列 {chosen.index(tid) + 1}/{len(chosen)}）。"
                   if tid in chosen else "当期未入选。"))
    return label, corpus_note


def _selector_context(materials, provenance_notes, con, p, run, selector):
    art = _artifact(p, run, f"C_selectors/{selector}.json")
    label = selector
    if isinstance(art, dict):
        chosen = art.get("chosen") or []
        scores = art.get("scores") or {}
        rejected = art.get("rejected") or {}
        meta = art.get("meta") or {}
        lines = [f"筛选C 策略 {selector} v{art.get('version')}：从同一候选池挑出 "
                 f"{len(chosen)} 条：{', '.join(map(str, chosen)) or '（空）'}。"]
        if meta:
            lines.append("参数/中间量: "
                         + json.dumps(scrub(meta), ensure_ascii=False)[:2000])
        for cid in chosen:
            sc = scores.get(cid)
            if sc:
                lines.append(f"- 选中 {cid}: "
                             + json.dumps(sc, ensure_ascii=False)[:400])
        for cid, why in list(rejected.items())[:40]:
            lines.append(f"- 拒绝 {cid}: {why}")
        _mk(materials, "selector", f"筛选C 判决 · {selector}",
            f"blob runs/{run['as_of']}/{run['run_id']}/C_selectors/{selector}.json",
            "\n".join(lines))
    else:
        vr = p.state.q("SELECT chosen, scores, rejected, meta FROM verdicts "
                       "WHERE run_id=? AND kind='idea_selector' AND strategy=?",
                       (run["run_id"], selector))
        if vr:
            v = vr[0]
            _mk(materials, "selector", f"筛选C 判决 · {selector}",
                f"verdicts · run_id={run['run_id']} · strategy={selector}",
                f"选中: {v['chosen']}\n打分: {v['scores']}\n"
                f"拒绝: {v['rejected']}\n参数: {v['meta']}")
        else:
            provenance_notes.append(
                f"既没有 C_selectors/{selector}.json 产物，"
                "也没有对应的 verdict 行")
    pool = _artifact(p, run, "B_pool.json")
    if isinstance(pool, list) and pool:
        lines = [f"当时的候选池共 {len(pool)} 条（每条一标的），"
                 "八个筛选C 策略看到的是同一个池："]
        for c in pool:
            lines.append(
                f"- {c.get('id')} · {c.get('topic_id')} · "
                f"上行 {c.get('upside_pct')}% (P {c.get('p_up')}) / "
                f"下行 {c.get('downside_pct')}% (P {c.get('p_down')}) · "
                f"{len(c.get('proposed_by') or [])} 种方式提出 · "
                f"论点: {(c.get('thesis') or '')[:90]}")
        _mk(materials, "pool", "候选池全景",
            f"blob runs/{run['as_of']}/{run['run_id']}/B_pool.json",
            "\n".join(lines))
    else:
        provenance_notes.append("B_pool.json 产物读不到——候选池全景缺失")
    return label, {}


# ---------------------------------------------------------------------------
def assemble_context(p, con, run_id: str | None,
                     subject: dict[str, Any]) -> dict[str, Any]:
    """Everything the answering model may see, with provenance per piece.

    `subject` is {"kind": "topic"|"idea"|"selector", "id": ...}. Pulls ONLY
    from the immutable stores: the run journal, the run's artifacts, the
    verdict/candidate rows, and the corpus rows those point to.
    """
    kind = str(subject.get("kind") or "")
    sid = str(subject.get("id") or "")
    if kind not in SUBJECT_KINDS:
        return {"error": f"未知的追问对象类型 {kind!r}；"
                         f"支持 {'/'.join(SUBJECT_KINDS)}"}
    if not sid:
        return {"error": "缺少追问对象 id"}
    run = _run_row(p, run_id)
    if not run:
        return {"error": (f"找不到运行 {run_id}" if run_id
                          else "没有任何完成的周跑记录")}

    materials: list[dict[str, Any]] = []
    provenance_notes: list[str] = []
    _journal_material(materials, p, run)
    if kind == "topic":
        label, corpus_note = _topic_context(
            materials, provenance_notes, con, p, run, sid)
    elif kind == "idea":
        label, corpus_note = _idea_context(
            materials, provenance_notes, con, p, run, sid)
    else:
        label, corpus_note = _selector_context(
            materials, provenance_notes, con, p, run, sid)

    materials = scrub(materials)
    n_docs = sum(1 for m in materials if m["kind"] == "doc")
    ctx = {
        "run": scrub({k: run.get(k) for k in
                      ("run_id", "as_of", "kind", "ok", "calls")}),
        "subject": {"kind": kind, "id": sid, "label": _scrub_text(str(label))},
        "materials": materials,
        "provenance": [{k: m[k] for k in ("id", "kind", "title", "source")}
                       for m in materials],
        "notes": [_scrub_text(x) for x in provenance_notes],
        "corpus": scrub(corpus_note),
        "stats": {"n_materials": len(materials), "n_docs": n_docs,
                  "chars": sum(len(m["text"]) for m in materials)},
    }
    if len(materials) <= 1:
        ctx["empty"] = ("这次运行关于这个对象的封存材料没有找到——"
                        "能拿到的只有运行日志本身。缺的部分见 notes。")
    return ctx


# ---------------------------------------------------------------------------
def _render_materials(context: dict[str, Any]) -> str:
    parts = []
    for m in context.get("materials") or []:
        parts.append(f"[{m['id']}] {m['title']}\n来源: {m['source']}\n{m['text']}")
    return "\n\n".join(parts)


def inference_state(p) -> tuple[bool, str]:
    """Whether this node can answer at all, and the honest reason if not."""
    try:
        h = p.inference.check()
        if h.ok:
            return True, h.detail or ""
        return False, f"{UNAVAILABLE_MSG}——{_scrub_text(h.detail or '')}"
    except Exception as e:  # noqa: BLE001
        return False, f"{UNAVAILABLE_MSG}——{_scrub_text(str(e))}"


def answer(p, question: str, context: dict[str, Any],
           history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """One grounded answer. Raises RuntimeError with the honest message when
    the inference port is unavailable on this node."""
    ok, detail = inference_state(p)
    if not ok:
        raise RuntimeError(detail)
    run = context.get("run") or {}
    subj = context.get("subject") or {}
    convo = ""
    for h in (history or [])[-6:]:
        convo += f"\n\n此前的追问：{h.get('q')}\n此前我的回答：{h.get('a')}"
    corpus_note = context.get("corpus") or {}
    verified = corpus_note.get("verified_sha")
    ver_line = {True: "（研报清单已按运行封存的 inputs_sha 逐一核对，与当时完全一致）",
                False: "（注意：研报清单按当时口径重建，但与运行封存的 inputs_sha 未能完全对上——语料库可能在运行后有过修补）",
                None: ""}[verified]
    prompt = (
        f"这次运行：{run.get('run_id')}，{run.get('as_of')} 期。\n"
        f"追问对象：{subj.get('kind')} · {subj.get('id')}"
        f"（{subj.get('label')}）。\n"
        f"以下是当时封存的全部材料{ver_line}：\n\n"
        + _render_materials(context)
        + (("\n\n材料缺口（如实告知用户）：" + "；".join(context["notes"]))
           if context.get("notes") else "")
        + convo
        + f"\n\n用户现在的追问：{question}")
    t0 = _time.time()
    c = p.inference.complete(prompt, system=SYSTEM_PROMPT,
                             temperature=0.2, max_tokens=1600)
    cited_ids = sorted(set(re.findall(r"\[?(M\d+)\]?", c.text)),
                       key=lambda x: int(x[1:]))
    by_id = {m["id"]: m for m in context.get("provenance") or []}
    cited = [by_id[i] for i in cited_ids if i in by_id]
    return {"answer": _scrub_text(c.text), "cited": cited,
            "model": c.model, "usage": c.usage,
            "latency_ms": c.latency_ms or int((_time.time() - t0) * 1000)}


def log_ask(entry: dict[str, Any]) -> None:
    """Append one Q&A to the local audit log; never fails the request."""
    try:
        ASK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ASK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), **entry},
                ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — the ask must still be answered
        pass


# ---------------------------------------------------------------------------
# route handlers — serve.py stays routes-only
def handle_context(params: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """GET /api/ask/context — the context summary + provenance, for display."""
    from . import platform as plat
    p = plat.load()
    con = db.init()
    ctx = assemble_context(p, con, params.get("run_id") or None,
                           {"kind": params.get("kind"), "id": params.get("id")})
    if "error" in ctx:
        return ctx, 404
    ok, detail = inference_state(p)
    ctx.pop("materials", None)  # display needs the list, not the full text
    ctx["inference"] = {"ok": ok, **({} if ok else {"error": detail})}
    return ctx, 200


def _forward_upstream(payload: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """Relay /api/ask to the production instance, when one is configured.

    IDEAGEN_ASK_UPSTREAM names the base URL (e.g. https://<prod-host>);
    IDEAGEN_ASK_UPSTREAM_KEY, if set, is sent as the dash key. Returns None
    when no upstream is configured or the relay itself fails, so the caller
    falls back to the honest 503.
    """
    import os
    import urllib.request
    base = (os.environ.get("IDEAGEN_ASK_UPSTREAM") or "").strip().rstrip("/")
    if not base:
        return None
    req = urllib.request.Request(
        base + "/api/ask",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 # The upstream's CSRF check compares Origin against its own
                 # external origin; a server-to-server relay has to say so.
                 "Origin": base,
                 **({"X-Dash-Key": k} if (k := os.environ.get(
                     "IDEAGEN_ASK_UPSTREAM_KEY")) else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            obj = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as e:  # upstream answered with an error code
        try:
            obj = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return None
        status = e.code
    except Exception:  # noqa: BLE001 — relay failure, keep the local 503
        return None
    if isinstance(obj, dict):
        obj["answered_by"] = "upstream"
        return obj, status
    return None


def handle_ask(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/ask — one grounded Q&A, appended to the audit log."""
    from . import platform as plat
    question = str(payload.get("question") or "").strip()
    if not question:
        return {"error": "问题是空的"}, 400
    if len(question) > 2000:
        return {"error": "问题太长了（上限 2000 字）"}, 400
    history = [h for h in (payload.get("history") or [])
               if isinstance(h, dict)][-10:]
    p = plat.load()
    con = db.init()
    ctx = assemble_context(p, con, payload.get("run_id") or None,
                           {"kind": payload.get("kind"),
                            "id": payload.get("id")})
    if "error" in ctx:
        return ctx, 404
    try:
        out = answer(p, question, ctx, history)
    except RuntimeError as e:
        # An observer node has no inference on purpose. If a production
        # upstream is configured, the question travels there instead of dying
        # here — the upstream assembles its own frozen context from the same
        # durable stores, so the answer is grounded the same way.
        fwd = _forward_upstream(payload)
        if fwd is not None:
            return fwd
        return {"error": str(e), "unavailable": True}, 503
    except Exception as e:  # noqa: BLE001 — bounded operator error, no traceback
        return {"error": _scrub_text(f"{type(e).__name__}: {e}"[:300])}, 502
    log_ask({"run_id": (ctx.get("run") or {}).get("run_id"),
             "kind": payload.get("kind"), "id": payload.get("id"),
             "question": question, "answer": out["answer"],
             "cited": [c["id"] for c in out["cited"]],
             "model": out.get("model"),
             "context_stats": ctx.get("stats")})
    return {"answer": out["answer"], "cited": out["cited"],
            "model": out.get("model"),
            "context_stats": ctx.get("stats"),
            "corpus": ctx.get("corpus")}, 200
