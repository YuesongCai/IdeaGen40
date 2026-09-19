"""WS-D: 每周筛选A 主题周报 — one period's theme output, for a reader who only reads.

yifu's side asked to see 筛选A's themes every week. Everything that answers that
is already computed and scattered: the chosen set in the `topic_scorer` verdict,
the TIS / tier / recurrence discount / measured direction in the `themes`
table's factors, the timing and lineage in the WS-A audit (kv), the diffusion
stage in the WS-C snapshot (kv), the evidence documents in `documents`. A reader
without the panel's six pages and three drawers cannot assemble that, so this
module does it once and hands back one document.

Three outputs from the same `build()`:

* `/api/digest` — the panel's read-only drawer (viewer accounts can open it,
  and print it). Built live, so any period can be read, not only the newest.
* `data/digests/themes_<as_of>.md` — written once per period after the weekly
  run (`after_weekly`), so there is a file to forward that does not depend on
  anyone logging in.
* Feishu — only when `IDEAGEN_DIGEST_FEISHU_CHAT_ID` is set. Unset is not an
  error and not silence: the stored status says 「未配置」 in words.

Rules that keep it honest:

* **Nothing is re-derived here.** Direction and key question come from the same
  fields 「本期方向」 reads (`themes.factors.direction`, registry key question),
  so the digest and the panel cannot disagree about what the week bet on.
* **A missing reading is named, with its date.** The daily `themes` table often
  has no row on the Wednesday itself; the nearest earlier day is used and its
  date is printed beside the number, rather than silently borrowing it.
* **Licensed research stays licensed.** If the run's classification is licensed,
  document titles are withheld exactly as `review.weekly_block` withholds them.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import config, db

KV_STATUS = "theme_digest:{as_of}"

#: How a document line reads when its row carries no institution name. The
#: Wisburg `ib`/`company` lines are broker research whose house is not a field.
_LINE_LABEL = {"ib": "投行研报（机构未标注）", "company": "公司研报（机构未标注）",
               "market-daily": "市场日报", "feed": "研报"}
_LINEAGE_LABEL = {"auto": "已判同一件事", "suspect": "疑似重复·待审", "nested": "包含关系"}
_TIER_LABEL = {"core": "核心", "important": "重要", "watch": "观察", "background": "背景"}


def digest_dir() -> Path:
    env = os.environ.get("IDEAGEN_DIGEST_DIR")
    return Path(env) if env else config.DATA / "digests"


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _theme_row(con, tid: str, as_of: str) -> dict[str, Any] | None:
    """The scored `themes` row on `as_of`, else the nearest earlier day (dated)."""
    r = db.q1(con, "SELECT as_of, tis, tier, c, b, factors FROM themes "
                   "WHERE theme_id=? AND as_of<=? ORDER BY as_of DESC LIMIT 1",
              (tid, as_of))
    if not r:
        return None
    f = db.jl(r["factors"], {}) or {}
    return {"d": r["as_of"], "same_day": r["as_of"] == as_of, "tis": _num(r["tis"]),
            "tier": r["tier"], "priced_c": _num(r["c"]), "b": _num(r["b"]),
            "direction": f.get("direction"), "recurrence": f.get("recurrence")}


def _docs(con, doc_ids: list[str], fallback: list[dict[str, Any]], *,
          hide: bool, n: int = 3) -> list[dict[str, Any]]:
    """Three representative documents: named house first, then tier, then newest."""
    rows: list[dict[str, Any]] = []
    ids = [str(x) for x in (doc_ids or []) if x][:400]
    if ids:
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            rows += [dict(r) for r in db.q(
                con, "SELECT doc_id, title, institution, line, tier, published_d "
                     "FROM documents WHERE doc_id IN (%s)" % ",".join("?" * len(chunk)),
                chunk)]
    if not rows:
        rows = [{"doc_id": d.get("doc_id"), "title": d.get("title"),
                 "institution": d.get("institution"), "line": None,
                 "tier": d.get("tier"), "published_d": d.get("published_d")}
                for d in fallback or []]
    seen: set[str] = set()
    uniq = []
    for r in rows:
        key = str(r.get("title") or r.get("doc_id"))
        if key in seen:
            continue      # the same report is filed under several lines
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda r: (0 if r.get("institution") else 1, r.get("tier") or 9,
                             "" if not r.get("published_d") else
                             "".join(chr(0x7f - ord(c)) for c in str(r["published_d"]))))
    out = []
    for r in uniq[:n]:
        inst = r.get("institution") or _LINE_LABEL.get(str(r.get("line") or ""), "研报")
        out.append({"doc_id": r.get("doc_id"), "published_d": r.get("published_d"),
                    "institution": inst,
                    "title": ("授权研报，标题不公开" if hide else (r.get("title") or r.get("doc_id")))})
    return out


def build(p, con, as_of: str | None = None) -> dict[str, Any]:
    """The digest document for one period (newest when `as_of` is None)."""
    from . import review
    w = review.weekly_block(p, con, as_of)
    if not w:
        return {"available": False, "as_of": as_of,
                "why": "这一期没有周跑记录" if as_of else "还没有任何周跑"}
    out: dict[str, Any] = {"available": True, "as_of": w["as_of"], "run_id": w["run_id"],
                           "ok": w.get("ok"), "in_flight": w.get("in_flight"),
                           "generated_at": config.now_hkt().isoformat(),
                           "notes": []}
    try:
        from . import performance
        out["classification"] = performance.period_classes(con).get(w["as_of"])
    except Exception:  # noqa: BLE001 — a label, not a reason to fail
        out["classification"] = None
    hide = review._licensed(w.get("data_classification"))
    topics = {t["scorer"]: t for t in (w.get("topics") or [])}
    main = topics.get("hgep") or next(iter(topics.values()), None)
    if not main:
        out.update(available=False, why="这一期的筛选A 还没有打分结果"
                   + ("（运行中）" if w.get("in_flight") else ""))
        return out
    out["scorer"] = main["scorer"]
    out["n_scored"] = len(main.get("scores") or {})
    out["corpus_total"] = w.get("corpus_total")

    try:
        audit = review.theme_audit_block(con)
    except Exception as e:  # noqa: BLE001
        audit = {"available": False, "why": f"{type(e).__name__}: {e}"}
    badge_min = float(audit.get("badge_min") or config.THEME_DISAGREE_BADGE_MIN)
    if not audit.get("available"):
        out["notes"].append("时点与谱系：" + str(audit.get("timing_why") or audit.get("why")
                                                or "缺数据：尚未运行筛选A 体检"))
    try:
        from . import diffusion
        dif = diffusion.state_block(con, w["as_of"])
    except Exception as e:  # noqa: BLE001
        dif = {"available": False, "why": f"{type(e).__name__}: {e}"}
    dif_by = {t.get("theme_id"): t for t in (dif.get("themes") or [])} if dif.get("available") else {}
    out["diffusion_as_of"] = dif.get("as_of") if dif.get("available") else None
    if not dif.get("available"):
        out["notes"].append("扩散阶段：" + str(dif.get("why") or "缺数据"))
    cut = next((r for r in ((audit.get("cutoff") or {}).get("runs") or [])
                if r.get("as_of") == w["as_of"]), None)
    out["cutoff"] = ({k: cut.get(k) for k in ("n_docs", "n_runtime", "violations",
                                              "violations_seen", "reading", "classification")}
                     if cut else None)

    themes_meta = w.get("themes") or {}
    evidence = w.get("evidence") or {}
    rows = []
    for tid in main.get("chosen") or []:
        sc = (main.get("scores") or {}).get(tid) or {}
        meta = themes_meta.get(tid) or {}
        tr = _theme_row(con, tid, w["as_of"])
        g = _num(sc.get("G"))
        b = (tr or {}).get("b")
        dis_val, dis_src = (g, "G") if g is not None else (b, "B")
        at = (audit.get("themes") or {}).get(tid) or {}
        dt = dif_by.get(tid) or {}
        rec = (tr or {}).get("recurrence") or {}
        rows.append({
            "theme_id": tid,
            "label": meta.get("label") or sc.get("label") or tid,
            "key_question": meta.get("key_question"),
            "score": _num(sc.get("score")),
            "hgep": {k: _num(sc.get(k)) for k in ("H", "G", "E", "P")},
            "n_evidence": sc.get("n_evidence"), "n_institutions": sc.get("n_institutions"),
            "tis": (tr or {}).get("tis"), "tier": (tr or {}).get("tier"),
            "tier_label": _TIER_LABEL.get(str((tr or {}).get("tier") or ""), (tr or {}).get("tier")),
            "reading_d": (tr or {}).get("d"), "reading_same_day": (tr or {}).get("same_day"),
            # Same field 「本期方向」 reads; the registry's default is the fallback
            # and is labelled as such so a default never passes for a measurement.
            "direction": (tr or {}).get("direction") or meta.get("direction"),
            "direction_source": ("本期打分" if (tr or {}).get("direction") else
                                 ("主题登记默认" if meta.get("direction") else None)),
            "recurrence": {k: rec.get(k) for k in ("occurrence", "consec", "discount",
                                                    "note", "tis_raw")} if rec else None,
            "disagreement": {"value": dis_val, "factor": dis_src, "min": badge_min,
                             "badge": dis_val is not None and dis_val >= badge_min},
            "timing": ({k: at.get(k) for k in ("first_mention_d", "first_surge_period",
                                               "first_strong_d", "first_selected",
                                               "lag_days", "verdict", "truncated_by_corpus")}
                       | {"n_selected_periods": len(at.get("selected_periods") or [])})
                      if at else None,
            "lineage": ({"family": [x for x in (at.get("family") or []) if x != tid],
                         "pairs": [{"other": x.get("other"), "decision": x.get("decision"),
                                    "decision_label": _LINEAGE_LABEL.get(
                                        str(x.get("decision")), x.get("decision"))}
                                   for x in (at.get("lineage") or [])]}
                        if at else None),
            "diffusion": ({k: dt.get(k) for k in ("stage", "stage_why", "lead_days")}
                          if dt else None),
            "docs": _docs(con, sc.get("doc_ids") or [],
                          (evidence.get(tid) or {}).get("docs") or [], hide=hide),
        })
    # 按 TIS 排（与「本期方向」同序）；没有 TIS 读数的沉底，再按本期打分。
    rows.sort(key=lambda r: (-(r["tis"] if r["tis"] is not None else -1e9),
                             -(r["score"] if r["score"] is not None else -1e9)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    out["themes"] = rows
    out["n_chosen"] = len(rows)
    if any(r["reading_d"] and not r["reading_same_day"] for r in rows):
        out["notes"].append("TIS / 层级 / 复现打折取自日度打分；期次当天没有打分时用最近一个更早交易日，日期写在数字旁。")
    return out


# ------------------------------------------------------------------ markdown
def _dir_word(d: Any) -> str:
    return {"↑": "↑ 看多", "↓": "↓ 看空"}.get(str(d or ""), "— 未定向")


def _f1(v: Any) -> str:
    return "缺" if v is None else f"{float(v):.1f}"


def to_markdown(doc: dict[str, Any], *, compact: bool = False) -> str:
    """The digest as Markdown. `compact` is the Feishu version: one block per
    theme, no document list beyond the top title, so it fits one message."""
    if not doc.get("available"):
        return f"# 筛选A 主题周报 · {doc.get('as_of') or '—'}\n\n缺数据：{doc.get('why')}\n"
    cls = {"live": "实时运行", "backfill": "补跑"}.get(str(doc.get("classification")), "运行类型未知")
    L = [f"# 筛选A 主题周报 · {doc['as_of']}",
         "",
         f"{cls} · 运行 `{doc['run_id']}` · 打分器 {doc.get('scorer')} · "
         f"本期打分 {doc.get('n_scored')} 个主题、入选 {doc.get('n_chosen')} 个 · "
         f"研报 {doc.get('corpus_total') if doc.get('corpus_total') is not None else '缺'} 篇",
         ""]
    cut = doc.get("cutoff")
    if cut and not compact:
        L += [f"> 截断审计：{cut.get('reading')}", ""]
    for r in doc.get("themes") or []:
        badge = " ·【多空分歧】" if (r.get("disagreement") or {}).get("badge") else ""
        tis = (f"TIS {_f1(r.get('tis'))}（{r.get('reading_d')}）" if r.get("tis") is not None
               else "TIS 缺")
        L.append(f"## {r['rank']}. {r['label']}（{r['theme_id']}）{badge}")
        L.append("")
        L.append(f"- 方向：{_dir_word(r.get('direction'))}"
                 + (f"（{r['direction_source']}）" if r.get("direction_source") else "")
                 + f" — {r.get('key_question') or '缺主题的那一问'}")
        rec = r.get("recurrence") or {}
        L.append(f"- {tis} · 层级 {r.get('tier_label') or '缺'} · 本期打分 {_f1(r.get('score'))}"
                 + (f" · 复现打折：{rec.get('note')}" if rec.get("note") else " · 复现打折：无"))
        dis = r.get("disagreement") or {}
        if not compact:
            L.append(f"- 分歧因子 {dis.get('factor')}={_f1(dis.get('value'))}（徽章门槛 {_f1(dis.get('min'))}）"
                     f" · 证据 {r.get('n_evidence') or '缺'} 条 / {r.get('n_institutions') or '缺'} 家")
        tm = r.get("timing")
        if tm:
            L.append(f"- 时点：研报首次提及 {tm.get('first_mention_d') or '无'} · 首次入选 "
                     f"{tm.get('first_selected') or '无'}（共 {tm.get('n_selected_periods')} 期）"
                     + ("" if compact else f" · {tm.get('verdict') or ''}"))
        else:
            L.append("- 时点：缺数据（体检里没有这个主题）")
        lin = r.get("lineage") or {}
        if not compact:
            fam = lin.get("family") or []
            L.append("- 谱系：" + ("与 " + "、".join(fam) + " 同一家族" if fam else "独立主题")
                     + ("；疑似/包含关系：" + "、".join(f"{x['other']}（{x['decision_label']}）"
                                                  for x in lin.get("pairs") or [])
                        if lin.get("pairs") else ""))
        dif = r.get("diffusion")
        L.append("- 扩散阶段：" + (f"{dif.get('stage')} — {dif.get('stage_why') or ''}"
                                  if dif else "缺数据"))
        docs = r.get("docs") or []
        if compact:
            if docs:
                L.append(f"- 代表研报：{docs[0]['institution']}《{docs[0]['title']}》")
        else:
            L.append("- 代表研报：" + ("" if docs else "缺"))
            for d in docs:
                L.append(f"  - {d.get('published_d') or ''} · {d['institution']} ·《{d['title']}》")
        L.append("")
    for n in doc.get("notes") or []:
        L.append(f"> 注：{n}")
    L += ["", f"_生成于 {doc.get('generated_at')}；方向为研报观点的净方向，不是价格预测；"
              f"社交扩散阶段只作诊断，不进打分。_", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ after the weekly run
def _feishu(md: str) -> dict[str, Any]:
    chat = os.environ.get("IDEAGEN_DIGEST_FEISHU_CHAT_ID", "").strip()
    if not chat:
        return {"state": "skipped", "why": "未配置 IDEAGEN_DIGEST_FEISHU_CHAT_ID，未推送飞书"}
    cli = os.environ.get("IDEAGEN_LARK_CLI", "lark-cli").strip()
    try:
        # `env=`: this runs from the weekly tick, and launchd's PATH has no
        # node for `lark-cli`'s shebang. Without it the digest yifu asked to
        # receive every week reports 127 and never leaves the machine.
        r = subprocess.run([cli, "im", "+messages-send", "--as", "bot",
                            "--chat-id", chat, "--markdown", md],
                           timeout=60, capture_output=True, text=True,
                           env=config.subprocess_env())
    except Exception as e:  # noqa: BLE001 — reported in the status, never raised
        return {"state": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
    if r.returncode != 0:
        return {"state": "failed", "why": (r.stderr or r.stdout or "").strip()[-300:]}
    return {"state": "sent", "chat_id": chat}


def after_weekly(p, con, as_of: str, *, force: bool = False) -> dict[str, Any]:
    """Generate the period's digest once. Never raises (the weekly run must not
    fail because a report about it could not be written)."""
    key = KV_STATUS.format(as_of=as_of)
    try:
        prev = db.kv_get(con, key)
        if prev and prev.get("ok") and not force:
            return {**prev, "action": "already"}
        doc = build(p, con, as_of)
        if not doc.get("available"):
            st = {"ok": False, "as_of": as_of, "why": doc.get("why"),
                  "at": config.now_hkt().isoformat()}
            db.kv_set(con, key, st)
            return st
        d = digest_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"themes_{as_of}.md"
        path.write_text(to_markdown(doc), encoding="utf-8")
        st = {"ok": True, "as_of": as_of, "path": str(path), "run_id": doc.get("run_id"),
              "n_chosen": doc.get("n_chosen"), "at": config.now_hkt().isoformat(),
              "feishu": _feishu(to_markdown(doc, compact=True))}
        db.kv_set(con, key, st)
        return st
    except Exception as e:  # noqa: BLE001
        st = {"ok": False, "as_of": as_of, "why": f"{type(e).__name__}: {e}"[:300],
              "at": config.now_hkt().isoformat()}
        try:
            db.kv_set(con, key, st)
        except Exception:  # noqa: BLE001
            pass
        return st


def status(con, as_of: str) -> dict[str, Any] | None:
    return db.kv_get(con, KV_STATUS.format(as_of=as_of))


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, default=str)
