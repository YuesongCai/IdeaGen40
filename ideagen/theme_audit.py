"""筛选A 的三份体检：真时点、放波动验证、后验截断审计。

yifu 2026-09-11 的三个问题，各对应一个函数：

* 「软件现金流 9 月才出现，但行情已经涨了一个月；是筛选耽误了，还是之前收敛在
  别的主题里」 → `theme_timing` + `case_study`。逐主题给出研报首次提及日、首次
  成为强主题日、首次入选日、入选前后指示标的的涨幅与已实现波动。
* 「主题只看热度、找哪里放波动」 → `vol_validation`。主题层的核心主张是可以被
  价格证伪的：入选主题的指示标的在入选后应该比入选前更会动，而且比落选主题
  动得多。不验证，这个主张就只是一句口号。
* 「后验 cut-off」 → `cutoff_audit`。每一期运行用到的研报，披露时间是否都早于
  该期的决策时点。

三条共同的纪律，都是这个仓吃过亏的地方（见 memory「绿着的失败」「读不到 vs
没有」）：

1. **匹配口径不自造。** 研报→主题用的是 `lexicon.match_theme` 加
   `scoring.collect_evidence` 同一条「两个词，或一个词加足够长正文」的门槛。
   另起一套匹配，体检量的就不是线上在用的那个东西。
2. **缺数据写原因，样本小写样本不足。** 返回 None 的地方都带 `why`；判定在样本
   不够时一律是「样本不足」，不给点估计配一个颜色。
3. **正文长度不是常数。** 2026-08 下旬起研报摘要与正文被补抓（ib 线正文均长从
   ~30 字涨到 ~1,500 字），全文口径的命中数在那一周会凭空翻几倍，看起来就像
   「主题突然热了」。所以每个计数同时给「标题」对照口径——标题长度恒定，
   时点结论以它为准。

结果存 kv（不新建表，免得动 schema 基线）：

  theme_audit:timing   {"version", "computed_at", "corpus_start", "periods",
                        "themes": [per-theme record], "case": {...}}
  theme_audit:vol      {"version", "computed_at", "window", "rows": [...],
                        "selected_vs_not": {...}, "disagreement": {...}, ...}
  theme_audit:cutoff   {"version", "computed_at", "runs": [...], "totals": {...}}

字段逐个写在各函数的 docstring 里，前端（`review.theme_audit_block`）只读这三份。
"""

from __future__ import annotations

import math
import random
import statistics as st
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from . import config, db, lexicon

KV_TIMING = "theme_audit:timing"
KV_VOL = "theme_audit:vol"
KV_CUTOFF = "theme_audit:cutoff"
VERSION = 1

STRONG_TIERS = ("core", "important")
CASE_THEME = "AI-MONETISATION"
CASE_NEIGHBOUR = "AI-CAPEX"


# ---------------------------------------------------------------- runs
def canonical_runs(con) -> list[dict]:
    """每期一条「算数」的周跑：与 `review.weekly_block` 同一条挑法（ok 优先、后跑优先）。

    一期可能跑过好几次（07-29 跑了五次），只有面板展示、候选池来自的那一次才算
    这一期的选择；把失败重试也算进去，同一期就会有两份互相矛盾的入选名单。
    """
    rows = db.q(con, "SELECT run_id, as_of, ok, started_at, data_classification "
                     "FROM orch_runs WHERE kind='weekly' "
                     "ORDER BY as_of, ok DESC, started_at DESC")
    out: dict[str, dict] = {}
    for r in rows:
        if r["as_of"] in out:
            continue
        v = {x["strategy"]: x for x in db.q(
            con, "SELECT strategy, chosen, scores FROM verdicts "
                 "WHERE run_id=? AND kind='topic_scorer'", (r["run_id"],))}
        hg = v.get("hgep")
        ct = v.get("counting")
        out[r["as_of"]] = {
            "as_of": r["as_of"], "run_id": r["run_id"], "ok": bool(r["ok"]),
            "started_at": r["started_at"],
            "classification": r["data_classification"] or "",
            "hgep_chosen": list(db.jl(hg["chosen"], []) if hg else []),
            "hgep_scores": dict(db.jl(hg["scores"], {}) if hg else {}),
            "counting_chosen": list(db.jl(ct["chosen"], []) if ct else []),
        }
    return [out[k] for k in sorted(out)]


def selected_index(con, runs: list[dict]) -> dict[str, list[str]]:
    """主题 → 入选的期（升序）。入选 = 该期算数的那次运行里有候选，或有持仓。

    主打分（hgep）的前五名就是进筛选B 的主题，候选池 topic_id 与它逐期一致；
    但只认打分判决会漏掉「判决没存下、候选和持仓却在」的期，所以三路取并集。
    """
    sel: dict[str, set[str]] = defaultdict(set)
    for r in runs:
        for tid in r["hgep_chosen"]:
            sel[tid].add(r["as_of"])
        for c in db.q(con, "SELECT DISTINCT topic_id FROM candidates "
                           "WHERE run_id=? AND topic_id IS NOT NULL", (r["run_id"],)):
            sel[c["topic_id"]].add(r["as_of"])
    known = {r["as_of"] for r in runs}
    for p in db.q(con, "SELECT DISTINCT theme, as_of FROM positions "
                       "WHERE book_id LIKE 'sel-%' AND as_of IS NOT NULL"):
        # 持仓的 theme 列早期存过中文标签，按注册表回到 id。
        tid = p["theme"]
        if tid not in lexicon.THEME_BY_ID:
            tid = next((t.id for t in lexicon.THEMES if t.label == tid), None)
        if tid and p["as_of"] in known:
            sel[tid].add(p["as_of"])
    return {k: sorted(v) for k, v in sel.items()}


def strong_index(con) -> dict[str, list[str]]:
    """主题 → 打分表里 tier 为 core/important 的日子（升序）。"""
    out: dict[str, list[str]] = defaultdict(list)
    for r in db.q(con, "SELECT theme_id, as_of FROM themes WHERE tier IN (?,?) "
                       "ORDER BY as_of", STRONG_TIERS):
        out[r["theme_id"]].append(r["as_of"])
    return dict(out)


# ---------------------------------------------------------------- mentions
def _doc_rows(con, upto: str | None = None) -> list:
    sql = ("SELECT doc_id, published_at, published_d, title, summary, body, "
           "content_hash FROM documents")
    args: list = []
    if upto:
        sql += " WHERE published_d<=?"
        args.append(upto)
    return db.q(con, sql + " ORDER BY published_at, doc_id", args)


def mention_index(con, themes: Iterable[lexicon.Theme] | None = None, *,
                  upto: str | None = None) -> dict[str, dict]:
    """每个主题命中的研报，两种口径各一份。

    返回 {theme_id: {"full": {doc_id: published_d}, "title": {...},
    "first_full": {...} | None, "first_title": {...} | None}}。

    * full 口径：`lexicon.match_theme` + 「≥2 个词，或 1 个词且全文 ≥400 字」，与
      `scoring.collect_evidence` 一字不差；同 content_hash 只算第一篇。
    * title 口径：只看标题、出现一个词即算。它不是线上的匹配，是**对照**：摘要
      和正文在 2026-08 中下旬先后被补抓（摘要均长 70→230 字、正文 30→1,500 字），
      full 口径的周命中数会随抓取深度而不是讨论热度跳变；标题长度两个月里恒在
      26–29 字，拿它判「什么时候开始热」才不被抓取深度骗。
    * 词表：主题的注册词加上**该篇研报披露当天已经登记**的别名。别名晚于研报
      就不算——否则今天补的一个叫法会把上个月的研报追认成证据。
    * 注册日不卡：体检要回答的恰恰是「注册之前它是不是已经在被讨论」。
    """
    ths = list(themes) if themes is not None else list(lexicon.THEMES)
    cache: dict[tuple[str, str], lexicon.Theme] = {}
    out = {t.id: {"full": {}, "title": {}, "first_full": None, "first_title": None}
           for t in ths}
    seen: set[str] = set()
    for r in _doc_rows(con, upto):
        ch = r["content_hash"]
        if ch and ch in seen:
            continue
        if ch:
            seen.add(ch)
        d = r["published_d"]
        full = " ".join(filter(None, (r["title"], r["summary"], r["body"] or "")))
        title = r["title"] or ""
        for t in ths:
            key = (t.id, d)
            tt = cache.get(key)
            if tt is None:
                tt = lexicon._with_aliases(t, d)
                cache[key] = tt
            for basis, text in (("full", full), ("title", title)):
                hits = lexicon.match_theme(text, tt)
                # 标题口径只要求标题里出现一个词：标题平均 27 字，套全文那条
                # 「两个词或 400 字」门槛几乎什么都命中不了，对照就没了意义。
                if (hits >= 1) if basis == "title" else (hits >= 2 or (hits == 1 and len(text) >= 400)):
                    rec = out[t.id]
                    rec[basis][r["doc_id"]] = d
                    fk = "first_" + basis
                    if rec[fk] is None:
                        rec[fk] = {"doc_id": r["doc_id"], "published_at": r["published_at"],
                                   "published_d": d, "title": (r["title"] or "")[:80]}
    return out


def _week_days(as_of: str, n: int = 7) -> set[str]:
    e = date.fromisoformat(as_of)
    return {(e - timedelta(days=i)).isoformat() for i in range(n)}


def weekly_periods(corpus_start: str, last: str) -> list[str]:
    """从研报库起点那一周起、每周三一期，直到 `last`。"""
    e = date.fromisoformat(last)
    s = date.fromisoformat(corpus_start)
    out = []
    while e >= s:
        out.append(e.isoformat())
        e -= timedelta(days=7)
    return sorted(out)


def docs_per_day(con) -> dict[str, int]:
    return {r["published_d"]: r["n"] for r in db.q(
        con, "SELECT published_d, COUNT(DISTINCT COALESCE(content_hash, doc_id)) n "
             "FROM documents GROUP BY published_d")}


# ---------------------------------------------------------------- prices
def _closes(con, code: str, *, before: str | None = None, from_: str | None = None,
            n: int) -> list[tuple[str, float]]:
    if before is not None:
        rows = db.q(con, "SELECT d, close FROM prices WHERE code=? AND d<? AND close>0 "
                         "ORDER BY d DESC LIMIT ?", (code, before, n))
        return [(r["d"], float(r["close"])) for r in reversed(rows)]
    rows = db.q(con, "SELECT d, close FROM prices WHERE code=? AND d>=? AND close>0 "
                     "ORDER BY d LIMIT ?", (code, from_, n))
    return [(r["d"], float(r["close"])) for r in rows]


def _stats(px: list[tuple[str, float]]) -> tuple[float | None, float | None]:
    """(区间收益, 年化已实现波动)。收益少于 5 个时波动不算——不给噪音配数字。"""
    if len(px) < 2:
        return None, None
    ret = px[-1][1] / px[0][1] - 1
    rets = [px[i][1] / px[i - 1][1] - 1 for i in range(1, len(px))]
    rv = (st.stdev(rets) * math.sqrt(252)) if len(rets) >= 5 else None
    return ret, rv


def window_stats(con, code: str, as_of: str,
                 n: int | None = None) -> dict[str, Any]:
    """指示标的在决策时点前后各 n 个交易日的收益与已实现波动。

    决策时点是该期周三 07:00 HKT，此时美股最新一根收盘是周二。所以「入选前」
    是 d < as_of 的最后 n+1 个收盘（n 个收益），「入选后」以同一根周二收盘为
    基点、取 d ≥ as_of 的 n 个收盘。前后共用一个基点，涨幅才首尾相接。
    `post_sessions < n` 表示窗口还没走完——不是没数据，是时间还没到。
    """
    n = n or config.THEME_AUDIT_WINDOW_SESSIONS
    out: dict[str, Any] = {"code": code, "as_of": as_of, "n": n}
    pre = _closes(con, code, before=as_of, n=n + 1)
    if len(pre) < n + 1:
        out.update(pre_ret=None, pre_rv=None, post_ret=None, post_rv=None,
                   post_sessions=0, complete=False,
                   why=f"缺数据：{code} 在 {as_of} 前只有 {len(pre)} 个收盘，不足 {n + 1}")
        return out
    pre_ret, pre_rv = _stats(pre)
    post = _closes(con, code, from_=as_of, n=n)
    post_ret, post_rv = _stats([pre[-1]] + post)
    out.update(pre_from=pre[0][0], base_d=pre[-1][0],
               pre_ret=pre_ret, pre_rv=pre_rv,
               post_to=(post[-1][0] if post else None),
               post_ret=(post_ret if post else None),
               post_rv=post_rv, post_sessions=len(post),
               complete=len(post) >= n)
    if not out["complete"]:
        out["why"] = f"窗口未满：入选后只有 {len(post)}/{n} 个交易日"
    return out


def _r(x: float | None, k: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, k)


# ---------------------------------------------------------------- timing
def theme_timing(con, *, runs: list[dict] | None = None,
                 mentions: dict | None = None) -> dict[str, Any]:
    """逐主题的时点体检。

    每个主题一条记录：
      first_mention      全文口径下最早命中的研报（披露时间、doc_id、标题）
      first_mention_title  同上，标题口径
      truncated_by_corpus  首次提及落在研报库起点 7 天内——「首次提及」被库的
                           起点截断，真实首次提及更早，滞后天数是下界
      first_surge_period  标题口径下，周命中占比首次 ≥ 此前各周中位数的
                          THEME_SURGE_RATIO 倍且命中 ≥ THEME_SURGE_MIN_DOCS 篇
                          的那一期——「开始被当回事地讨论」
      first_strong_d     打分表里首次 tier core/important 的日子
      first_counting_d   对照打分（计数）首次把它排进前五的期
      first_selected     首次入选的期
      lag_days           {mention, surge, strong} → 距首次入选的天数
      pre / post         首次入选时指示标的前后 n 个交易日（window_stats）
      pre_ret_2n         入选前 2n 个交易日的涨幅（一个月的行情往往在 n 之外）
      weekly             每期：命中数（两口径）、占比、hgep 名次/得分/与第五名差距、
                         是否入选、计数打分是否选入
      verdict            一句话结论
    """
    runs = runs if runs is not None else canonical_runs(con)
    mentions = mentions if mentions is not None else mention_index(con)
    sel = selected_index(con, runs)
    strong = strong_index(con)
    per_day = docs_per_day(con)
    cstart = min(per_day) if per_day else None
    last = runs[-1]["as_of"] if runs else (max(per_day) if per_day else None)
    run_by = {r["as_of"]: r for r in runs}
    periods = weekly_periods(cstart, last) if (cstart and last) else []
    n = config.THEME_AUDIT_WINDOW_SESSIONS

    records = []
    for t in lexicon.THEMES:
        m = mentions.get(t.id) or {"full": {}, "title": {}, "first_full": None,
                                   "first_title": None}
        weekly = []
        prior_share: list[float] = []
        surge = None
        for p in periods:
            days = _week_days(p)
            tot = sum(per_day.get(d, 0) for d in days)
            nf = sum(1 for d in m["full"].values() if d in days)
            nt = sum(1 for d in m["title"].values() if d in days)
            share = nt / tot if tot else 0.0
            row: dict[str, Any] = {"as_of": p, "docs_full": nf, "docs_title": nt,
                                   "docs_total": tot, "share_title": _r(share, 5)}
            if (surge is None and len(prior_share) >= 3 and nt >= config.THEME_SURGE_MIN_DOCS
                    and share >= config.THEME_SURGE_RATIO * max(st.median(prior_share), 1e-9)):
                surge = p
            prior_share.append(share)
            run = run_by.get(p)
            if run:
                ranked = sorted(run["hgep_scores"].items(),
                                key=lambda kv: -(kv[1].get("score") or 0))
                ids = [k for k, _ in ranked]
                cut = (ranked[4][1].get("score") if len(ranked) >= 5 else None)
                sc = run["hgep_scores"].get(t.id) or {}
                row.update(
                    rank=(ids.index(t.id) + 1 if t.id in ids else None),
                    n_scored=len(ids), score=sc.get("score"),
                    H=sc.get("H"), G=sc.get("G"), E=sc.get("E"), P=sc.get("P"),
                    gap_to_cut=(_r(cut - sc["score"], 1)
                                if cut is not None and sc.get("score") is not None else None),
                    selected=p in sel.get(t.id, []),
                    counting_chosen=t.id in run["counting_chosen"])
            weekly.append(row)

        sel_list = sel.get(t.id, [])
        first_sel = sel_list[0] if sel_list else None
        # 入选前标题口径周占比最高的那一期。不设阈值：升温判据（倍数）是拍的，
        # 峰值不是——它只回答「入选之前，大家谈它谈得最凶是哪一周」。
        pre_weeks = [w for w in weekly if first_sel and w["as_of"] < first_sel
                     and w["docs_total"]]
        peak = max(pre_weeks, key=lambda w: w["share_title"] or 0) if pre_weeks else None
        strong_list = strong.get(t.id, [])
        first_strong = strong_list[0] if strong_list else None
        first_count = next((r["as_of"] for r in runs if t.id in r["counting_chosen"]), None)
        fm = m["first_full"]
        truncated = bool(fm and cstart and
                         (date.fromisoformat(fm["published_d"]) - date.fromisoformat(cstart)).days < 7)

        def lag(d: str | None) -> int | None:
            if not (d and first_sel):
                return None
            return (date.fromisoformat(first_sel) - date.fromisoformat(d[:10])).days

        rec: dict[str, Any] = {
            "theme_id": t.id, "label": t.label, "origin": t.origin,
            "registered_d": t.registered_d, "indicator": t.price_indicator,
            "first_mention": fm, "first_mention_title": m["first_title"],
            "truncated_by_corpus": truncated,
            "n_docs_full": len(m["full"]), "n_docs_title": len(m["title"]),
            "first_surge_period": surge, "first_strong_d": first_strong,
            "pre_selection_peak": ({"as_of": peak["as_of"], "docs_title": peak["docs_title"],
                                    "share_title": peak["share_title"]} if peak else None),
            "n_strong_days": len(strong_list),
            "first_counting_d": first_count, "first_selected": first_sel,
            "selected_periods": sel_list,
            "lag_days": {"mention": lag(fm["published_d"] if fm else None),
                         "surge": lag(surge), "strong": lag(first_strong),
                         "peak": lag(peak["as_of"] if peak else None),
                         "counting": lag(first_count)},
            "weekly": weekly,
        }
        if first_sel:
            w = window_stats(con, t.price_indicator, first_sel, n)
            rec["pre"] = {k: _r(v) if isinstance(v, float) else v for k, v in w.items()}
            pre2 = _closes(con, t.price_indicator, before=first_sel, n=2 * n + 1)
            rec["pre_ret_2n"] = (_r(pre2[-1][1] / pre2[0][1] - 1)
                                 if len(pre2) == 2 * n + 1 else None)
        rec["verdict"] = timing_verdict(rec)
        records.append(rec)

    return {"version": VERSION, "computed_at": config.now_hkt().isoformat(),
            "corpus_start": cstart, "window": n, "periods": periods,
            "canonical_runs": [{k: r[k] for k in ("as_of", "run_id", "classification")}
                               for r in runs],
            "themes": records}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def timing_verdict(rec: dict) -> str:
    """一句话：滞后多少天、入选前已涨多少。不下「耽误了」的判断——那要看个案。"""
    if not rec.get("first_selected"):
        if rec.get("first_strong_d"):
            return (f"从未入选；打分表 {rec['first_strong_d']} 起已是强主题"
                    f"（共 {rec['n_strong_days']} 天）")
        return "从未入选，也从未成为强主题"
    parts = [f"{rec['first_selected']} 首次入选"]
    lg = rec["lag_days"]
    if lg.get("surge") is not None:
        if lg["surge"] >= 0:
            parts.append(f"距研报升温（{rec['first_surge_period']}）滞后 {lg['surge']} 天")
        else:
            parts.append(f"入选早于研报升温（{rec['first_surge_period']}）")
    elif rec.get("first_selected"):
        parts.append("标题口径下未见明显升温（一直在被讨论）")
    pk = rec.get("pre_selection_peak")
    if pk and lg.get("peak"):
        parts.append(f"入选前讨论最密的一周是 {pk['as_of']}（标题占比 {pk['share_title'] * 100:.1f}%），"
                     f"比入选早 {lg['peak']} 天")
    if lg.get("mention") is not None:
        s = f"距首次提及滞后 {lg['mention']} 天"
        if rec.get("truncated_by_corpus"):
            s += "（首次提及被研报库起点截断，实际更早）"
        parts.append(s)
    pre = rec.get("pre") or {}
    if pre.get("pre_ret") is not None:
        if abs(pre["pre_ret"]) < 0.005:
            parts.append(f"入选前 {pre['n']} 个交易日 {rec['indicator']} 基本持平（{_pct(pre['pre_ret'])}）")
        else:
            parts.append(f"入选前 {pre['n']} 个交易日 {rec['indicator']} 已{'涨' if pre['pre_ret'] >= 0 else '跌'}"
                         f" {abs(pre['pre_ret']) * 100:.1f}%")
    elif pre.get("why"):
        parts.append(pre["why"])
    return "；".join(parts)


# ---------------------------------------------------------------- case study
def case_study(con, timing: dict, mentions: dict, *, theme_id: str = CASE_THEME,
               neighbour: str = CASE_NEIGHBOUR) -> dict[str, Any]:
    """个案：一个主题入选前，它的证据研报在干什么。

    三个假设各给一组数，互不代替：
      A 证据晚到    —— 标题口径下它的周命中占比何时抬头（不受正文补抓影响）
      B 被邻居吃掉  —— 它的命中研报里有多少同时命中邻居主题，那几周邻居入选没有
      C 排序/阈值   —— 每期主打分里它排第几、离第五名差几分；对照打分是否早就选了它
    结论（哪个假设成立）写在 docs/theme_timing_casestudy_*.md，由人对着这些数下。
    """
    rec = next((r for r in timing["themes"] if r["theme_id"] == theme_id), None)
    nb = next((r for r in timing["themes"] if r["theme_id"] == neighbour), None)
    if rec is None:
        return {"theme_id": theme_id, "why": f"缺数据：注册表里没有 {theme_id}"}
    me = mentions.get(theme_id) or {"full": {}, "title": {}}
    ne = mentions.get(neighbour) or {"full": {}, "title": {}}
    nb_sel = set((nb or {}).get("selected_periods") or [])
    rows = []
    for w in rec["weekly"]:
        days = _week_days(w["as_of"])
        a = {k for k, d in me["full"].items() if d in days}
        b = {k for k, d in ne["full"].items() if d in days}
        at = {k for k, d in me["title"].items() if d in days}
        bt = {k for k, d in ne["title"].items() if d in days}
        # 反事实：P（已定价）取中性 50 时的得分。主打分 = 0.30H+0.25G+0.25E+0.20(100−P)，
        # 所以 P 每高 1 分，得分低 0.2。用来分开「排序被已定价压住」和「别的因子不够」。
        p_neutral = (_r(w["score"] + 0.20 * (w["P"] - 50.0), 1)
                     if w.get("score") is not None and w.get("P") is not None else None)
        rows.append({"score_p_neutral": p_neutral,
                     "gap_p_neutral": (_r(w["score"] + (w.get("gap_to_cut") or 0) - p_neutral, 1)
                                       if p_neutral is not None and w.get("gap_to_cut") is not None
                                       else None),
                     **{k: w.get(k) for k in ("as_of", "docs_full", "docs_title",
                                               "share_title", "rank", "score",
                                               "H", "G", "E", "P",
                                               "gap_to_cut", "selected", "counting_chosen")},
                     "overlap_full": len(a & b),
                     "overlap_full_share": _r(len(a & b) / len(a), 3) if a else None,
                     "overlap_title": len(at & bt),
                     "overlap_title_share": _r(len(at & bt) / len(at), 3) if at else None,
                     "neighbour_selected": w["as_of"] in nb_sel})
    ind = rec["indicator"]
    px = [(r["d"], float(r["close"])) for r in db.q(
        con, "SELECT d, close FROM prices WHERE code=? AND d>=? ORDER BY d",
        (ind, (timing.get("periods") or ["2026-07-01"])[0]))]
    # 一个月的行情从哪天起算：入选前 2n 个交易日里的最低收盘 → 入选前最后一根收盘。
    run_up = None
    if rec.get("first_selected") and px:
        before = [x for x in px if x[0] < rec["first_selected"]][-2 * timing["window"]:]
        if before:
            lo = min(before, key=lambda x: x[1])
            hi = max([x for x in before if x[0] >= lo[0]], key=lambda x: x[1])
            run_up = {"low_d": lo[0], "low": lo[1], "high_d": hi[0], "high": hi[1],
                      "low_to_high": _r(hi[1] / lo[1] - 1),
                      "base_d": before[-1][0], "base": before[-1][1],
                      "low_to_base": _r(before[-1][1] / lo[1] - 1)}
    return {"theme_id": theme_id, "label": rec["label"], "neighbour": neighbour,
            "indicator": ind, "first_selected": rec.get("first_selected"),
            "first_counting_d": rec.get("first_counting_d"),
            "first_strong_d": rec.get("first_strong_d"),
            "first_surge_period": rec.get("first_surge_period"),
            "run_up": run_up, "weekly": rows,
            "prices": [{"d": d, "close": c} for d, c in px]}


# ---------------------------------------------------------------- vol validation
def _boot_diff(a: list[float], b: list[float], n_boot: int, rng: random.Random):
    diffs = []
    for _ in range(n_boot):
        ra = [a[rng.randrange(len(a))] for _ in a]
        rb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(st.mean(ra) - st.mean(rb))
    diffs.sort()
    return diffs[int(0.025 * n_boot)], diffs[int(0.975 * n_boot) - 1]


def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            rk[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return rk


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def _boot_rho(x: list[float], y: list[float], n_boot: int, rng: random.Random):
    vals = []
    idx = list(range(len(x)))
    for _ in range(n_boot):
        s = [idx[rng.randrange(len(idx))] for _ in idx]
        r = spearman([x[i] for i in s], [y[i] for i in s])
        if r is not None:
            vals.append(r)
    if len(vals) < n_boot // 2:
        return None, None
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def _state(n_a: int, n_b: int, lo: float | None, hi: float | None) -> str:
    """判定三态，与证据页同一套词：held 显著 / no 不显著（反向显著）/ open 样本不足。

    区间盖住 0 记「样本不足」而不是「不显著」：几十个重叠窗口的样本，区间宽到
    盖住 0 说明还分不出，不说明效应不存在。
    """
    if min(n_a, n_b) < config.THEME_AUDIT_MIN_GROUP_N or lo is None or hi is None:
        return "open"
    if lo > 0:
        return "held"
    if hi < 0:
        return "no"
    return "open"


def vol_validation(con, *, runs: list[dict] | None = None,
                   n_boot: int | None = None, seed: int = 20260911) -> dict[str, Any]:
    """「入选主题之后放波动」与「分歧大的主题之后放波动」两条命题的检验。

    样本单位：（期, 主题），主题取该期主打分里有分数的全部主题（入选与落选同
    一个池子里比）。每行：
      vol_ratio_log     ln(入选后 n 日已实现波动 / 入选前 n 日已实现波动)
      vol_ratio_log_dm  同上减去**同一期所有主题的中位数**——整个市场在那几周
                        一起放波动的部分扣掉，剩下的才是「这个主题比别的更会动」
      absret_pre/post   前后 n 日收益绝对值
      b                 打分表同期（as_of ≤ 该期的最近一天）的 B 分歧因子
      g                 主打分的 G（分歧）——入选靠的是它
    只有 post 窗口走满 n 个交易日的行进检验；没走满的期列在 periods_incomplete。

    检验：
      selected_vs_not   入选组均值 − 落选组均值（vol_ratio_log_dm），bootstrap
                        95% 区间；|收益| 同法另算一份
      disagreement      B 与 vol_ratio_log_dm 的 Spearman 及 bootstrap 区间；
                        B 上三分位 − 下三分位的均值差；G 同法另算
    已知偏窄：相邻两期的 21 日窗口重叠 16 天、一个主题连续几期都在样本里，行与
    行不独立，区间按独立样本算，**实际应更宽**。结论读法因此是保守的：区间
    离 0 很远才算数。
    """
    runs = runs if runs is not None else canonical_runs(con)
    n = config.THEME_AUDIT_WINDOW_SESSIONS
    n_boot = n_boot or config.THEME_AUDIT_BOOTSTRAP
    rng = random.Random(seed)
    rows: list[dict] = []
    incomplete: dict[str, str] = {}
    for run in runs:
        p = run["as_of"]
        bmap = {}
        brow = db.q1(con, "SELECT MAX(as_of) d FROM themes WHERE as_of<=?", (p,))
        b_as_of = brow["d"] if brow else None
        if b_as_of:
            bmap = {r["theme_id"]: r["b"] for r in db.q(
                con, "SELECT theme_id, b FROM themes WHERE as_of=?", (b_as_of,))}
        for tid, sc in run["hgep_scores"].items():
            th = lexicon.THEME_BY_ID.get(tid)
            if th is None:
                continue
            w = window_stats(con, th.price_indicator, p, n)
            row = {"as_of": p, "theme_id": tid, "indicator": th.price_indicator,
                   "selected": tid in run["hgep_chosen"], "score": sc.get("score"),
                   "g": sc.get("G"), "b": bmap.get(tid), "b_as_of": b_as_of,
                   "pre_rv": _r(w.get("pre_rv")), "post_rv": _r(w.get("post_rv")),
                   "absret_pre": _r(abs(w["pre_ret"])) if w.get("pre_ret") is not None else None,
                   "absret_post": _r(abs(w["post_ret"])) if w.get("post_ret") is not None else None,
                   "post_sessions": w.get("post_sessions", 0),
                   "complete": bool(w.get("complete"))}
            if row["complete"] and row["pre_rv"] and row["post_rv"]:
                row["vol_ratio_log"] = _r(math.log(w["post_rv"] / w["pre_rv"]))
            else:
                row["vol_ratio_log"] = None
                if not row["complete"]:
                    incomplete.setdefault(p, w.get("why") or "窗口未满")
            rows.append(row)
    # 期内去中位数
    by_p: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["vol_ratio_log"] is not None:
            by_p[r["as_of"]].append(r)
    for p, rs in by_p.items():
        med = st.median([r["vol_ratio_log"] for r in rs])
        amed = st.median([r["absret_post"] - r["absret_pre"] for r in rs])
        for r in rs:
            r["vol_ratio_log_dm"] = _r(r["vol_ratio_log"] - med)
            r["absret_delta_dm"] = _r((r["absret_post"] - r["absret_pre"]) - amed)
    ok = [r for r in rows if r.get("vol_ratio_log_dm") is not None]

    sel = [r["vol_ratio_log_dm"] for r in ok if r["selected"]]
    nos = [r["vol_ratio_log_dm"] for r in ok if not r["selected"]]
    svn: dict[str, Any] = {"metric": "vol_ratio_log_dm", "n_selected": len(sel),
                           "n_not": len(nos)}
    if sel and nos:
        lo, hi = (_boot_diff(sel, nos, n_boot, rng) if min(len(sel), len(nos)) >= 2
                  else (None, None))
        svn.update(mean_selected=_r(st.mean(sel)), mean_not=_r(st.mean(nos)),
                   diff=_r(st.mean(sel) - st.mean(nos)), ci95=[_r(lo), _r(hi)],
                   raw_mean_selected=_r(st.mean([r["vol_ratio_log"] for r in ok if r["selected"]])),
                   raw_mean_not=_r(st.mean([r["vol_ratio_log"] for r in ok if not r["selected"]])))
        asel = [r["absret_delta_dm"] for r in ok if r["selected"]]
        anos = [r["absret_delta_dm"] for r in ok if not r["selected"]]
        alo, ahi = (_boot_diff(asel, anos, n_boot, rng) if min(len(asel), len(anos)) >= 2
                    else (None, None))
        svn["absret"] = {"metric": "absret_delta_dm", "diff": _r(st.mean(asel) - st.mean(anos)),
                         "ci95": [_r(alo), _r(ahi)],
                         "state": _state(len(asel), len(anos), alo, ahi)}
        svn["state"] = _state(len(sel), len(nos), lo, hi)
    else:
        svn.update(state="open", why="缺数据：没有走满窗口的入选或落选样本")

    dis = {"B": _disagreement(ok, "b", n_boot, rng), "G": _disagreement(ok, "g", n_boot, rng)}

    by_period = []
    for run in runs:
        p = run["as_of"]
        rs = by_p.get(p) or []
        s1 = [r["vol_ratio_log_dm"] for r in rs if r["selected"]]
        by_period.append({"as_of": p, "n": len(rs), "n_selected": len(s1),
                          "mean_selected_dm": _r(st.mean(s1)) if s1 else None,
                          "complete": p not in incomplete,
                          "why": incomplete.get(p)})
    return {"version": VERSION, "computed_at": config.now_hkt().isoformat(),
            "window": n, "n_boot": n_boot, "min_group_n": config.THEME_AUDIT_MIN_GROUP_N,
            "n_rows": len(rows), "n_complete": len(ok),
            "periods_complete": sorted(by_p), "periods_incomplete": incomplete,
            "selected_vs_not": svn, "disagreement": dis, "by_period": by_period,
            "caveats": [
                f"相邻期的 {n} 日窗口重叠、同一主题连续多期入样，行与行不独立；区间按独立样本算，实际应更宽",
                "同一指示标的可被两个主题共用（如 SMH），这两行不是两份独立证据",
                "波动比取对数并减去同期全部主题的中位数，扣掉全市场同涨同落的波动变化"],
            "rows": rows}


def _disagreement(ok: list[dict], key: str, n_boot: int, rng: random.Random) -> dict:
    pts = [(r[key], r["vol_ratio_log_dm"]) for r in ok if r.get(key) is not None]
    out: dict[str, Any] = {"factor": key.upper(), "n": len(pts)}
    if len(pts) < 3:
        out.update(state="open", why=f"缺数据：带 {key.upper()} 读数的完整样本只有 {len(pts)} 行")
        return out
    x = [p[0] for p in pts]
    y = [p[1] for p in pts]
    rho = spearman(x, y)
    lo, hi = _boot_rho(x, y, n_boot, rng)
    out.update(rho=_r(rho, 3), ci95=[_r(lo, 3), _r(hi, 3)])
    srt = sorted(pts, key=lambda p: p[0])
    k = len(srt) // 3
    if k >= 2:
        bot = [p[1] for p in srt[:k]]
        top = [p[1] for p in srt[-k:]]
        tlo, thi = _boot_diff(top, bot, n_boot, rng)
        out["top_vs_bottom"] = {"n_each": k, "diff": _r(st.mean(top) - st.mean(bot)),
                                "ci95": [_r(tlo), _r(thi)],
                                "top_min": srt[-k][0], "bottom_max": srt[k - 1][0]}
    out["state"] = _state(len(pts), len(pts), lo, hi)
    return out


# ---------------------------------------------------------------- cutoff audit
def cutoff_of(as_of: str) -> datetime:
    """该期的决策时点：周三 07:00 HKT（`scheduler.WEEKLY_TRIGGER_HKT`）。"""
    from .scheduler import WEEKLY_TRIGGER_HKT
    return datetime.combine(date.fromisoformat(as_of), WEEKLY_TRIGGER_HKT, tzinfo=config.TZ)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=config.TZ)


def cutoff_audit(con, *, runs: list[dict] | None = None,
                 window_days: int | None = None, max_list: int = 40) -> dict[str, Any]:
    """每期运行用到的研报，披露时间是否都 ≤ 该期决策时点。

    用到的研报清单：运行日志（journal）的 inputs 步骤里早期没存 doc_id，线上日志
    又在对象存储、本机读不到，所以按研报 feed 的取数规则重建——`published_d` 落在
    截至该期的 `window_days` 天里（`feeds_impl/wisburg_corpus.py`）。重建出来的
    篇数和该次运行 `feed_runs` 记下的篇数对得上，重建集合就是运行时那一份；对
    不上（实时运行之后又入库了研报）就如实标出，违例数只能当上界读。

    每期：
      cutoff             决策时点
      n_docs / n_runtime 重建篇数 / 运行时记录篇数；reconstructed_matches
      violations         披露时间 > 决策时点的篇数
      after_start        其中披露时间还晚于运行开始时刻的篇数——实时运行不可能
                         读到它们，是重建的假违例；补跑（运行在事后）则全部是真的
      no_timestamp       无披露时间、无法判定的篇数
      samples            违例样例（至多 max_list 篇）
    """
    runs = runs if runs is not None else canonical_runs(con)
    wd = window_days or config.OBSERVATION_WINDOW_DAYS
    out_runs = []
    tot = {"docs": 0, "violations": 0, "violations_seen": 0, "no_timestamp": 0}
    for run in runs:
        p = run["as_of"]
        cut = cutoff_of(p)
        started = _parse_ts(run.get("started_at"))
        days = [(date.fromisoformat(p) - timedelta(days=i)).isoformat() for i in range(wd)]
        docs = db.q(con, "SELECT doc_id, published_at, published_d, title FROM documents "
                         "WHERE published_d IN (%s) ORDER BY published_at" % ",".join("?" * len(days)),
                    days)
        rt = db.q1(con, "SELECT SUM(n_rows) n FROM feed_runs WHERE run_id=? AND kind='corpus'",
                   (run["run_id"],))
        n_runtime = int(rt["n"]) if rt and rt["n"] is not None else None
        viol, after_start, no_ts = [], 0, 0
        for d in docs:
            ts = _parse_ts(d["published_at"])
            if ts is None:
                no_ts += 1
                continue
            if ts > cut:
                viol.append(d)
                if started and ts > started:
                    after_start += 1
        live = (run["classification"] == "live")
        matches = (n_runtime == len(docs)) if n_runtime is not None else None
        seen = len(viol) - (after_start if live else 0)
        rec = {"as_of": p, "run_id": run["run_id"], "classification": run["classification"],
               "cutoff": cut.isoformat(), "started_at": run.get("started_at"),
               "window_days": wd, "n_docs": len(docs), "n_runtime": n_runtime,
               "reconstructed_matches": matches,
               "violations": len(viol), "after_start": after_start,
               "violations_seen": seen, "no_timestamp": no_ts,
               "latest_published_at": (docs[-1]["published_at"] if docs else None),
               "samples": [{"doc_id": d["doc_id"], "published_at": d["published_at"],
                            "title": (d["title"] or "")[:60]} for d in viol[:max_list]]}
        rec["reading"] = _cutoff_reading(rec)
        out_runs.append(rec)
        tot["docs"] += len(docs)
        tot["violations"] += len(viol)
        tot["violations_seen"] += seen
        tot["no_timestamp"] += no_ts
    return {"version": VERSION, "computed_at": config.now_hkt().isoformat(),
            "rule": "研报披露时间 ≤ 该期周三 07:00 HKT",
            "doc_set": f"按研报 feed 取数规则重建：published_d 落在截至该期的 {wd} 天内",
            "runs": out_runs, "totals": tot}


def _cutoff_reading(r: dict) -> str:
    if r["n_docs"] == 0:
        return "缺数据：该期窗口内库里没有研报"
    base = f"{r['n_docs']} 篇中 {r['violations']} 篇披露晚于决策时点"
    if r["violations"] == 0:
        return f"{r['n_docs']} 篇全部早于决策时点"
    if r["classification"] == "live":
        s = (f"{base}；其中 {r['after_start']} 篇晚于运行开始、实时运行读不到，"
             f"能读到的违例上界 {r['violations_seen']} 篇")
    else:
        s = f"{base}；补跑在事后运行，这些研报当时都在库里，是真违例"
    if r["reconstructed_matches"] is False:
        s += f"（重建 {r['n_docs']} 篇 ≠ 运行时 {r['n_runtime']} 篇，清单不是运行时那一份）"
    return s


# ---------------------------------------------------------------- run & load
def run_all(con, *, store: bool = True, n_boot: int | None = None) -> dict[str, Any]:
    runs = canonical_runs(con)
    mentions = mention_index(con)
    timing = theme_timing(con, runs=runs, mentions=mentions)
    timing["case"] = case_study(con, timing, mentions)
    vol = vol_validation(con, runs=runs, n_boot=n_boot)
    cut = cutoff_audit(con, runs=runs)
    if store:
        with db.tx(con):
            db.kv_set(con, KV_TIMING, timing)
            db.kv_set(con, KV_VOL, vol)
            db.kv_set(con, KV_CUTOFF, cut)
    return {"timing": timing, "vol": vol, "cutoff": cut, "mentions": mentions, "runs": runs}


def load(con) -> dict[str, Any]:
    return {"timing": db.kv_get(con, KV_TIMING), "vol": db.kv_get(con, KV_VOL),
            "cutoff": db.kv_get(con, KV_CUTOFF)}


def cmd_theme_audit(args) -> int:
    """CLI：`ideagen theme-audit [--no-store]`。"""
    import json as _json
    con = db.init()
    res = run_all(con, store=not getattr(args, "no_store", False))
    t, v, c = res["timing"], res["vol"], res["cutoff"]
    print(f"研报库起点 {t['corpus_start']} · {len(t['canonical_runs'])} 期 · 窗口 {t['window']} 个交易日")
    for r in t["themes"]:
        if r.get("first_selected") or r.get("first_strong_d"):
            print(f"  {r['theme_id']:<30} {r['verdict']}")
    s = v["selected_vs_not"]
    print(f"放波动（入选−落选，期内去中位数的对数波动比）：diff={s.get('diff')} "
          f"CI={s.get('ci95')} n={s['n_selected']}/{s['n_not']} → {s.get('state')}")
    for k, d in v["disagreement"].items():
        print(f"  分歧 {k}：rho={d.get('rho')} CI={d.get('ci95')} n={d['n']} → {d.get('state')}")
    print(f"截断审计：{c['totals']}")
    for r in c["runs"]:
        print(f"  {r['as_of']} {r['reading']}")
    if getattr(args, "json", False):
        print(_json.dumps({"case": t.get("case", {}).get("weekly")}, ensure_ascii=False, indent=1))
    return 0
