"""Theme discovery: mine candidate macro themes the dictionary cannot see.

A fixed theme list is a bet that the author already knows every macro debate
that will matter. Measured against the corpus that bet loses badly — 46% of
items matched none of the 16 seed themes, and among the misses were
GLP-1 与医保准入、韩国科技股重估、人形机器人、光模块出口管制、央行购金: all
well-sourced, all live, all invisible.

Discovery runs in two stages, deliberately split by who is good at what.

**Stage A — `candidates()`, mechanical, runs unattended.** Take *every* item
in the trailing window, mine repeated phrases from them, drop the phrases a
registered theme already owns, keep only phrases that clear hard evidence
gates, and cluster phrases that travel together into candidate themes. This
stage reads no prices and makes no judgements; it only says "these N documents
from M institutions over K days are about something the dictionary has no word
for" — and, for each cluster, how many of those documents also matched which
registered theme (`relation`), so Stage B knows whether it is looking at a
new debate, a neighbour of an old one, or an old one under a new name.

Until 2026-09-07 the mining population was only the items that matched *no*
registered theme. That had a blind spot Jon's review named directly: a report
that mentioned 联储 once was claimed by POLICY-PATH and everything else it
argued — a new driver, a new dispute — never reached discovery. The historical
registry must align names and track continuity; it must not decide what this
week is allowed to find.

**Stage B — naming and registration, done by the model.** For each candidate
the model writes the theme card: label, key question, synonyms, price
indicator, and its relation to the neighbouring registered themes with the
reason in words (same driver? same verification condition? same event, cash
flow or risk?). Three outcomes:

  * `distinct` / `split` — `register()` validates the card and appends one
    line to `themes/registry.jsonl`, stamped with the day it was registered;
    a split names its parent in `split_from`.
  * `same_debate` — the cluster is an existing theme in this week's wording.
    No new theme: `add_alias()` appends the new words to
    `themes/aliases.jsonl`, dated, and the theme matches them from that day on.
  * skip — corpus noise, not a macro debate. A finding, not a failure.

The split matters because Stage B is where hindsight would enter. Two rules
keep it out, both enforced here rather than by good intentions:

  * `registered_d` may not be backdated, and `lexicon.all_themes(as_of)`
    excludes themes registered after `as_of` — a theme discovered today cannot
    score last week, so it can never be credited with a call it never made.
    Aliases carry the same stamp and the same clamp.
  * the price indicator must be priceable *and* is chosen from the candidate's
    own evidence, before any return is computed. Picking the instrument that
    already ran is the failure mode; `validate()` cannot detect intent, but
    `registered_d` makes the attempt worthless.

`snapshot(as_of)` freezes the resulting definition set — every theme's id,
registration day and a hash of the words it matched on — so a period's scores
can be tied to the exact vocabulary that produced them.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, db, lexicon

# ---------------------------------------------------------------------------
# Admission gates. A candidate must clear every one of these. They are set so
# that a single institution's pet topic, a one-day news spike, and a recurring
# boilerplate phrase all fail — the three things that would otherwise flood the
# registry with themes that cannot carry a trade.
# ---------------------------------------------------------------------------
WINDOW_DAYS = 5           # wider than the 3-day scoring window: must persist
MIN_DOCS = 8              # distinct documents mentioning the phrase
MIN_INSTITUTIONS = 4      # distinct institutions (or title signatures)
MIN_DAYS = 3              # distinct publication days
MIN_LIFT = 2.0            # frequency vs the pre-window baseline
MIN_CLUSTER_DOCS = 10     # a cluster needs more evidence than a lone phrase
JACCARD = 0.34            # phrase doc-set overlap that counts as "same theme"
MAX_CANDIDATES = 8        # surfaced per day; the rest wait for tomorrow

# ---------------------------------------------------------------------------
# Mining scope. `all` is the default since 2026-09-07: every document in the
# window feeds discovery, whether or not a registered theme already claimed
# it. `unmatched` is the pre-2026-09-07 population, kept so the two can be
# compared on the same period (the case study in docs/主题形成_从当期研报出发.md
# is exactly that comparison) and so a caller can still ask the narrower
# question "what does the dictionary miss entirely".
# ---------------------------------------------------------------------------
SCOPE_ALL = "all"
SCOPE_UNMATCHED = "unmatched"

# ---------------------------------------------------------------------------
# Relation of a candidate cluster to the registered themes, decided by how
# many of the cluster's evidence documents *also* matched a registered theme.
# Mechanical and stated in the output so the model's later judgement (same
# debate / split / distinct) can be checked against a number:
#
#   share >= SPLIT_SHARE     → "possible_split" of that theme: most of the
#                              cluster lives inside documents the theme already
#                              claims. Either the theme's wording has moved
#                              (an alias) or a sub-debate has grown inside it
#                              (a split). Which one is the model's call.
#   ADJACENT_SHARE <= share  → "adjacent": the cluster and the theme share a
#              < SPLIT_SHARE   meaningful slice of evidence but most of the
#                              cluster stands on its own — a neighbour.
#   share < ADJACENT_SHARE   → "distinct": what little overlap exists is what
#                              any two macro debates share.
#
# Sharing a document is not the same as being the same debate — a report can
# argue two things — which is why this is a hint carried into the prompt, not
# a verdict, and why the thresholds are constants a reader can find.
# ---------------------------------------------------------------------------
SPLIT_SHARE = 0.6
ADJACENT_SHARE = 0.2
REL_DISTINCT = "distinct"
REL_ADJACENT = "adjacent"
REL_POSSIBLE_SPLIT = "possible_split"

#: Phrases that are frequent, generic and never a theme on their own. Without
#: this the top candidates are "目标价"/"评级"/"预期" — true of every document
#: and informative about none.
#:
#: Geographies and scope modifiers (香港/亚太/海外/新兴市场) are in here for a
#: subtler reason: they name *where* a debate is happening, never *what* is
#: being debated, so they cluster unrelated documents that share only a region.
#: A region-specific theme still surfaces, because the longer phrase carrying
#: the actual subject (香港保险离岸征税) is mined as its own n-gram.
NOISE = frozenset("""
香港 亚太 海外 境外 离岸 在岸 内地 亚洲 北美 拉美 新兴 新兴市场 发达市场 全球市场
早报 周报 月报 日报 晨报 快评 点评 简评 纪要 会议 论坛 调查 追踪 监测 更新 前瞻
市场 美国 中国 欧洲 日本 全球 投资 投资者 分析 分析师 报告 研究 观点 展望 预期
评级 目标价 买入 增持 减持 中性 卖出 跑赢 跑输 大盘 estimate rating target price
buy sell hold overweight underweight neutral outperform underperform
公司 集团 股份 有限 控股 业务 收入 营收 利润 净利 毛利 业绩 财报 季度 二季度
一季度 三季度 四季度 上半年 下半年 同比 环比 增长 下降 上升 回落 提升 改善
风险 机会 影响 变化 情况 水平 数据 指标 因素 趋势 逻辑 策略 配置 组合 仓位
经济 宏观 政策 央行 利率 通胀 美元 汇率 股市 债市 收益率 估值 盈利 现金流
today update weekly monthly daily outlook review comment note research
维持 重申 上调 下调 首次 覆盖 给予 电话会 电话 业绩会 财年 年报 中报 超配 低配
标普 指数 板块 行业 个股 龙头 标的 上市 港元 美元计 亿美元 亿元 万亿 国际 国内
二季 三季 四季 一季 半年 全年 去年 今年 明年 上年
回顾 系列 机遇 挑战 亮点 要点 摘要 综述 解读 问答 常见 视点 洞察 专题 深度
corporation corp group inc incorporated holdings holding limited ltd plc
technologies technology financial finance company industries international
""".split())

#: Longest noise term, for the composite check's scan window.
_NOISE_MAX = max(len(n) for n in NOISE)

#: Characters that carry no topic on their own, so they never rescue a phrase
#: from being boilerplate: 上调目标价"至", "年"二季度.
_FILLER = set("至的与和及为于在了或对无是被把年月日个第其上下前后新旧")

#: Share of a phrase's characters that may be boilerplate before the phrase is
#: rejected. Tuned against the observed failure: sliding n-grams emit
#: off-by-one fragments of boilerplate (持买入评级, 度财报电话会) that no
#: stopword list contains, and full-coverage testing let every one of them
#: through. Two-thirds catches the fragments while leaving 小米集团 (集团 is
#: noise, 小米 is not) for the generator to reject on semantic grounds.
NOISE_COVER = 0.6


def _noise_composite(p: str) -> bool:
    """True if `p` is mostly boilerplate.

    Character n-grams recombine generic words into phrases no stopword list can
    enumerate — 维持买入评级, 季度财报电话会, 上调目标价至 — and earnings season
    gives every such combination high lift simply by minting it fresh. Scoring
    character coverage rather than exact membership rejects the whole family.
    """
    if p in NOISE:
        return True
    n = len(p)
    covered = [False] * n
    for i in range(n):
        for size in range(min(_NOISE_MAX, n - i), 0, -1):
            if p[i:i + size] in NOISE:
                for j in range(i, i + size):
                    covered[j] = True
                break
    for i, ch in enumerate(p):
        if ch in _FILLER:
            covered[i] = True
    return sum(covered) / n >= NOISE_COVER

_CJK = re.compile(r"[一-鿿]+")
_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]{2,}")
_NUM = re.compile(r"^[\d\W_]+$")


def _phrases(text: str) -> set[str]:
    """Candidate phrases in one string: CJK 2–6-grams plus ASCII tokens.

    Character n-grams rather than word segmentation: the corpus mixes Chinese,
    English and tickers, and a segmenter tuned for none of them would silently
    drop exactly the neologisms discovery exists to catch (GLP-1、人形机器人、
    光模块). n-grams over-generate, which the evidence gates then prune.
    """
    out: set[str] = set()
    for run in _CJK.findall(text or ""):
        n = len(run)
        for size in (2, 3, 4, 5, 6):
            for i in range(n - size + 1):
                out.add(run[i:i + size])
    for tok in _ASCII.findall(text or ""):
        low = tok.lower().strip(".-")
        if len(low) >= 3 and not _NUM.match(low):
            out.add(low)
    # Only exact boilerplate is dropped here. The composite check runs *after*
    # maximal-phrase collapse: applied this early it deletes 维持买入评级 and
    # orphans its fragments (持买入评, 买入评级), which then look like novel
    # phrases with nothing longer left to absorb them.
    return {p for p in out if p not in NOISE}


#: How much of a short phrase's document set must be shared with a longer
#: phrase containing it before the short one is treated as a fragment of it.
SUBSUME = 0.8


def _maximal(kept: list[dict]) -> list[dict]:
    """Drop phrases that are fragments of a longer phrase in the same documents.

    Sliding character n-grams emit every substring, so one real phrase arrives
    as a ladder: 维持买入评级 → 持买入评级 → 持买入评 → 买入评. Each rung has
    nearly the same document set. Keeping only the top rung means the noise
    filter downstream has one honest phrase to judge instead of five fragments
    that individually look novel.
    """
    out: list[dict] = []
    for k in sorted(kept, key=lambda k: (-len(k["phrase"]), -k["n_docs"])):
        p, ds = k["phrase"], k["docs"]
        if any(p != m["phrase"] and p in m["phrase"]
               and len(ds & m["docs"]) / len(ds) >= SUBSUME for m in out):
            continue
        out.append(k)
    return out


def _known_terms(as_of: date | None = None) -> set[str]:
    """Lowercased synonyms of the themes registered as of `as_of`.

    The as-of argument is load-bearing. Suppressing against *today's*
    dictionary made a replay of 2026-08-07 stop surfacing "spacex" the moment
    SPACE-ECONOMY was registered on 08-08 — the historical run would look as if
    it had already discovered the theme it had not yet seen.
    """
    return {t.lower() for th in lexicon.all_themes(as_of) for t in th.terms}


def _window(as_of: date, days: int) -> list[str]:
    return [(as_of - timedelta(days=i)).isoformat() for i in range(days)]


def _text_of(row) -> str:
    return " ".join(filter(None, (row["title"], row["summary"],
                                  (row["body"] or "")[:3000])))


def window_items(con, as_of: date, days: int = WINDOW_DAYS,
                 corpus: list[dict] | None = None) -> list[dict]:
    """Every window item, each tagged with the registered themes it matched.

    `matched` is the list of theme ids (as of `as_of`) whose vocabulary the
    document's title/summary/body hits — the same test scoring applies, so a
    document counted here as claimed by POLICY-PATH is one POLICY-PATH would
    score. An empty list is a document the dictionary cannot see at all.

    The as-of clamp is not cosmetic: tagging against *today's* dictionary would
    make a historical replay claim it had already discovered themes it had not.
    """
    themes = lexicon.all_themes(as_of)
    wdays = _window(as_of, days)
    if corpus is not None:
        # The run's own corpus, already windowed and as-of clamped by the feed
        # that produced it. On the cloud node the `documents` table below is
        # empty — research lives in the state store's `corpus_documents` and
        # reaches the run only as these rows — so reading the table there
        # would report 「无新主题」 every week for want of anything to read.
        wset = set(wdays)
        rows = [{"doc_id": r.get("doc_id"), "line": r.get("line"),
                 "tier": int(r.get("tier") or 3), "title": r.get("title"),
                 "institution": r.get("institution"),
                 "published_d": r.get("published_d"), "summary": r.get("summary"),
                 "body": r.get("body")}
                for r in corpus if str(r.get("published_d") or "") in wset]
    else:
        rows = db.q(con,
                    "SELECT doc_id,line,tier,title,institution,published_d,summary,body "
                    "FROM documents WHERE published_d IN (%s)" % ",".join("?" * len(wdays)),
                    wdays)
    out = []
    for r in rows:
        text = _text_of(r)
        hits = [t.id for t in themes if lexicon.match_theme(text, t) >= 1]
        inst = r["institution"] or lexicon.institution_of(text)
        out.append({
            "doc_id": r["doc_id"], "line": r["line"], "tier": r["tier"],
            "d": r["published_d"], "title": r["title"] or "",
            "institution": inst or f"sig:{lexicon.title_signature(r['title'] or '')[:12]}",
            "text": text,
            "matched": hits,
        })
    return out


def unmatched(con, as_of: date, days: int = WINDOW_DAYS) -> list[dict]:
    """Window items matching no theme registered as of `as_of`."""
    return [it for it in window_items(con, as_of, days) if not it["matched"]]


def _relation(doc_ids: set[str], by_doc: dict[str, dict]) -> dict:
    """How a cluster's evidence overlaps the registered themes.

    `overlap` counts, per theme, the cluster documents that theme also
    matched; `kind`/`of` apply the SPLIT_SHARE / ADJACENT_SHARE rule to the
    theme with the largest overlap. `n_docs_matched` is how many cluster
    documents any registered theme had already claimed — the number that was
    zero by construction before 2026-09-07, and the one that says whether a
    candidate was found inside old themes' territory or outside it.
    """
    overlap: dict[str, int] = defaultdict(int)
    n_matched = 0
    for d in doc_ids:
        hits = (by_doc.get(d) or {}).get("matched") or []
        if hits:
            n_matched += 1
        for tid in hits:
            overlap[tid] += 1
    n = max(1, len(doc_ids))
    kind, of, share = REL_DISTINCT, None, 0.0
    if overlap:
        of, top = max(overlap.items(), key=lambda kv: (kv[1], kv[0]))
        share = top / n
        if share >= SPLIT_SHARE:
            kind = REL_POSSIBLE_SPLIT
        elif share >= ADJACENT_SHARE:
            kind = REL_ADJACENT
        else:
            kind, of = REL_DISTINCT, None
    return {
        "overlap": dict(sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0]))),
        "kind": kind,
        "of": of,
        "share": round(share, 2),
        "n_docs_matched": n_matched,
        "n_docs_unmatched": len(doc_ids) - n_matched,
    }


#: Baseline phrase frequencies keyed by (window start, corpus size). Building it
#: means n-gramming every document published before the window, which the
#: dashboard would otherwise repeat once per rendered day. Including the row
#: count in the key means a fresh ingest invalidates it rather than leaving the
#: long-lived `serve` process quoting stale reach numbers.
_BASELINE_CACHE: dict[tuple[str, int], tuple[dict[str, int], int]] = {}


def _baseline(con, as_of: date, days: int) -> tuple[dict[str, int], int]:
    """Phrase document-frequency before the window, for the lift denominator.

    Generic boilerplate is as common before the window as inside it (lift ≈ 1);
    a genuinely new topic has almost no history (lift large). This is what
    separates 人形机器人 from 目标价 without hand-maintaining a stopword list
    for every phrase the corpus will ever contain.
    """
    start = _window(as_of, days)[-1]
    n_corpus = db.q(con, "SELECT COUNT(*) n FROM documents")[0]["n"]
    key = (start, n_corpus)
    if key in _BASELINE_CACHE:
        return _BASELINE_CACHE[key]
    rows = db.q(con, "SELECT title,summary FROM documents WHERE published_d < ?",
                [start])
    df: dict[str, int] = defaultdict(int)
    for r in rows:
        for p in _phrases(" ".join(filter(None, (r["title"], r["summary"])))):
            df[p] += 1
    _BASELINE_CACHE[key] = (df, len(rows))
    return df, len(rows)


def candidates(con, as_of: date, days: int = WINDOW_DAYS,
               limit: int = MAX_CANDIDATES, scope: str = SCOPE_ALL,
               corpus: list[dict] | None = None) -> dict:
    """Candidate themes mined from the window's documents.

    `scope=SCOPE_ALL` (default) mines every document; `SCOPE_UNMATCHED` mines
    only the ones no registered theme matched, which is what this function
    did before 2026-09-07. In both scopes a phrase a registered theme already
    owns is dropped, so an old theme cannot resurface as its own candidate;
    what changes is whether a report already claimed by one theme may still
    contribute the *other* thing it argues.
    """
    if scope not in (SCOPE_ALL, SCOPE_UNMATCHED):
        raise ValueError(f"scope must be {SCOPE_ALL!r} or {SCOPE_UNMATCHED!r}, "
                         f"got {scope!r}")
    everything = window_items(con, as_of, days, corpus=corpus)
    n_matched_total = sum(1 for it in everything if it["matched"])
    items = (everything if scope == SCOPE_ALL
             else [it for it in everything if not it["matched"]])
    base_df, base_n = _baseline(con, as_of, days)
    known = _known_terms(as_of)

    # Phrase -> evidence. Title+summary only for mining (bodies add noise, and
    # a theme that never reaches a title is not what the corpus is *about*).
    docs: dict[str, set[str]] = defaultdict(set)
    insts: dict[str, set[str]] = defaultdict(set)
    days_seen: dict[str, set[str]] = defaultdict(set)
    by_doc: dict[str, dict] = {}
    for it in items:
        by_doc[it["doc_id"]] = it
        for p in _phrases(it["title"]):
            docs[p].add(it["doc_id"])
            insts[p].add(it["institution"])
            days_seen[p].add(it["d"])

    n_win = max(1, len(items))
    kept: list[dict] = []
    for p, ds in docs.items():
        if len(ds) < MIN_DOCS or len(insts[p]) < MIN_INSTITUTIONS:
            continue
        if len(days_seen[p]) < MIN_DAYS:
            continue
        # Already covered by a registered theme's synonyms, either direction:
        # "光模块" is new, "算力投资" is AI-CAPEX wearing a different collar.
        if any(p in k or k in p for k in known):
            continue
        if base_n:
            lift = (len(ds) / n_win) / ((base_df.get(p, 0) + 1) / (base_n + 1))
            if lift < MIN_LIFT:
                continue
        else:
            # No history to compare against (a node whose corpus starts this
            # week). The lift gate cannot say anything, so it says nothing —
            # recorded in `gates` below rather than silently passing everyone
            # as if they had been tested.
            lift = None
        kept.append({"phrase": p, "docs": ds, "n_docs": len(ds),
                     "n_inst": len(insts[p]), "n_days": len(days_seen[p]),
                     "lift": (round(lift, 2) if lift is not None else None)})

    kept = _maximal(kept)
    # Boilerplate is judged only now, on whole phrases. 维持买入评级 is rejected
    # here and takes its fragments with it, because they were absorbed above.
    kept = [k for k in kept if not _noise_composite(k["phrase"])]
    kept.sort(key=lambda k: (-k["n_docs"], -(k["lift"] or 0.0), k["phrase"]))

    # Cluster phrases that travel through the same documents. "GLP-1", "减肥药"
    # and "司美格鲁肽" are one theme with three names, and admitting them as
    # three themes would triple-count the same evidence in D.
    clusters: list[dict] = []
    for k in kept:
        for c in clusters:
            inter = len(k["docs"] & c["docs"])
            union = len(k["docs"] | c["docs"])
            if union and inter / union >= JACCARD:
                c["phrases"].append(k)
                c["docs"] |= k["docs"]
                break
        else:
            clusters.append({"phrases": [k], "docs": set(k["docs"])})

    out = []
    for c in clusters:
        ds = c["docs"]
        if len(ds) < MIN_CLUSTER_DOCS:
            continue
        ev = [by_doc[d] for d in ds if d in by_doc]
        # Character n-grams over the same word produce ladders of fragments
        # ("香港市场"/"港市场"/"香港市"/"港市"). Keep only maximal phrases, or the
        # term list reads as four synonyms when it is one word cut four ways.
        names = sorted(c["phrases"], key=lambda p: (-len(p["phrase"]), -p["n_docs"]))
        maximal: list[dict] = []
        for p in names:
            if any(p["phrase"] in m["phrase"] for m in maximal):
                continue
            maximal.append(p)
        maximal.sort(key=lambda p: (-p["n_docs"], -len(p["phrase"])))
        ordered = sorted(ev, key=lambda e: (e["tier"], e["d"], e["doc_id"]))
        out.append({
            "terms": [p["phrase"] for p in maximal[:12]],
            "n_docs": len(ds),
            "n_institutions": len({e["institution"] for e in ev}),
            "n_days": len({e["d"] for e in ev}),
            "tiers": sorted({e["tier"] for e in ev}),
            "max_lift": (max((p["lift"] or 0.0) for p in c["phrases"])
                         if base_n else None),
            "relation": _relation(ds, by_doc),
            # Every document, in a stable order, so a registration can cite
            # its evidence by id rather than by the 14 titles shown below.
            "doc_ids": [e["doc_id"] for e in ordered],
            "evidence": [{"doc_id": e["doc_id"], "line": e["line"],
                          "tier": e["tier"], "d": e["d"],
                          "institution": e["institution"], "title": e["title"],
                          "matched": list(e["matched"])}
                         for e in ordered[:14]],
        })
    out.sort(key=lambda c: (-c["n_docs"], -(c["max_lift"] or 0.0)))

    total = len(everything)
    return {
        "as_of": as_of.isoformat(),
        "window_days": days,
        "scope": scope,
        "registered": len(lexicon.all_themes(as_of)),
        "corpus_total": total,
        "corpus_matched": n_matched_total,
        "coverage_pct": lexicon.coverage(n_matched_total, total),
        "unmatched": total - n_matched_total,
        "mined": len(items),
        "gates": {"min_docs": MIN_DOCS, "min_institutions": MIN_INSTITUTIONS,
                  "min_days": MIN_DAYS, "min_lift": MIN_LIFT,
                  "min_cluster_docs": MIN_CLUSTER_DOCS,
                  "lift_gate": ("applied" if base_n else
                                "skipped: no documents before the window"),
                  "baseline_docs": base_n,
                  "split_share": SPLIT_SHARE, "adjacent_share": ADJACENT_SHARE},
        "candidates": out[:limit],
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_ID = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z0-9]+)*$")


class RegistrationError(ValueError):
    """A proposed theme that must not enter the registry."""


def validate(con, row: dict, as_of: date) -> dict:
    """Check a proposed theme, returning the normalised registry row.

    Every rejection here corresponds to a way a bad registration would quietly
    corrupt later scoring rather than fail loudly.
    """
    req = ("id", "label", "key_question", "terms", "price_indicator")
    missing = [k for k in req if not row.get(k)]
    if missing:
        raise RegistrationError(f"missing required fields: {missing}")

    tid = str(row["id"]).strip()
    if not _ID.match(tid):
        raise RegistrationError(
            f"id {tid!r} must be upper-case, hyphen-separated (e.g. GLP1-ACCESS)")
    if tid in lexicon.THEME_BY_ID:
        raise RegistrationError(f"theme id {tid!r} is already registered")

    terms = [str(t).strip() for t in row["terms"] if str(t).strip()]
    if len(terms) < 4:
        raise RegistrationError(
            f"{tid} has {len(terms)} synonyms; at least 4 are needed or the "
            f"theme will only match the one phrasing it was born from")
    known = _known_terms(as_of)
    stolen = [t for t in terms if t.lower() in known]
    if stolen:
        raise RegistrationError(
            f"{tid} claims synonyms already owned by a registered theme: "
            f"{stolen} — that would double-count the same evidence in D")

    kq = str(row["key_question"]).strip()
    if not any(h in kq for h in ("1–6", "1-6", "个月", "month")):
        raise RegistrationError(
            f"{tid} key question has no horizon; it must be answerable within "
            f"the 1–6 month window the odds are computed over")

    # Backdating is the one edit that would turn discovery into hindsight.
    reg_d = str(row.get("registered_d") or as_of.isoformat())
    if reg_d != as_of.isoformat():
        raise RegistrationError(
            f"{tid} would be registered as of {reg_d} while today is "
            f"{as_of.isoformat()}; registration cannot be backdated")

    # Relation to the themes that already exist. A registry row may say it is
    # a distinct debate or a split of a named parent; "same_debate" is not a
    # registration at all (it is an alias — see `add_alias`) and is refused
    # here so a merge can never arrive as a second row. A split must name a
    # parent that is registered as of today, because a parent registered
    # later would make the child older than the debate it split from.
    relation = str(row.get("relation") or "distinct").strip()
    if relation not in ("distinct", "split"):
        raise RegistrationError(
            f"{tid} relation must be 'distinct' or 'split', got {relation!r}; "
            f"a same_debate finding is recorded as an alias, not a theme")
    split_from = str(row.get("split_from") or "").strip() or None
    legal_parents = {t.id for t in lexicon.all_themes(as_of)}
    if relation == "split" and split_from not in legal_parents:
        raise RegistrationError(
            f"{tid} is a split of {split_from!r}, which is not a theme "
            f"registered as of {as_of.isoformat()}")
    if relation == "distinct" and split_from:
        raise RegistrationError(
            f"{tid} names split_from={split_from!r} but relation is 'distinct'")

    # Checked last: unlike the rules above, this one is fixable outside the
    # theme definition, so reporting it first would bury the real problem.
    # A theme whose indicator cannot be priced produces ideas that cannot be
    # marked, which enter the book as a free 0% return.
    code = str(row["price_indicator"]).strip()
    codes = [code] + [str(c).strip() for c in (row.get("related") or [])]
    unpriceable = [c for c in codes
                   if not db.q(con, "SELECT 1 FROM instruments WHERE futu_code=? "
                                    "AND COALESCE(priceable,0)=1", [c])]
    if unpriceable:
        raise RegistrationError(
            f"{tid} registers unpriceable indicators {unpriceable}; run "
            f"`ideagen prices --extra {','.join(unpriceable)}` first, or pick "
            f"instruments already in the universe")

    return {
        "id": tid,
        "label": str(row["label"]).strip(),
        "key_question": kq,
        "terms": terms,
        "price_indicator": code,
        "related": [str(c).strip() for c in (row.get("related") or [])][:3],
        "default_direction": str(row.get("default_direction") or "↑"),
        "exposures": [str(e).strip() for e in (row.get("exposures") or [])],
        "require": [str(r).strip() for r in (row.get("require") or [])],
        "registered_d": reg_d,
        "origin": "discovered",
        "provenance": [str(p) for p in (row.get("provenance") or [])][:20],
        "relation": relation,
        "rationale": str(row.get("rationale") or "").strip(),
        "split_from": split_from,
        "evidence_doc_ids": [str(d) for d in (row.get("evidence_doc_ids") or [])][:20],
    }


MINT_SYSTEM = """你在给一个宏观交易系统整理它自己刚从当周研报里发现的主题。

输入是一簇当周研报里反复出现、且现有主题词典一个都盖不住的短语，外加它们出现的
标题证据，以及这簇证据与哪些**已注册主题**共享了多少篇研报（邻近主题）。
你的任务是先判断这簇短语和邻近主题的关系，再决定写不写主题卡。

判断关系时问三件事：驱动是否相同？验证条件（关键问题）是否相同？是否依赖同一个
事件、同一条现金流、同一种风险？三者都相同就是同一个争论换了叫法；行业相同但
驱动或验证条件不同，就是要区分的两个主题。共享研报本身不算重复——一篇研报可以
同时讲两件事。

铁律，逐条服从：
1. 只依据给出的短语和标题证据。不得使用你对这个日期之后的世界的任何了解——
   这张卡会被用来给当周打分，掺进后来的事就是泄露。
2. 主题必须是一个**能用做多标的表达的宏观争论**，不是一家公司、一条新闻、
   一个板块名词。写不成争论的，返回 {"skip": "原因"}。
3. 与某个邻近主题是**同一个争论**的，不要写新卡，返回
   {"skip": "与 X 是同一争论", "relation": "same_debate", "of": "邻近主题id",
    "new_terms": ["这簇研报里对它的新叫法", ...], "rationale": "中文说明理由"}。
   new_terms 只放当周研报标题里逐字出现的**完整词组**（「绩超预期」要写成「业绩超预期」，
   不能是切碎的片段），而且必须是这个争论的**专有叫法**——「平淡」「议前」这种通用词、
   以及已被词典里某个词覆盖的说法（含「业绩」的词组对 EARNINGS-QUALITY 就是多余的）
   都不要写；没有真正的新叫法就把 new_terms 留空。
4. 与邻近主题同一行业但驱动或验证条件不同的，写新卡，relation 填 "split"，
   of 填被拆出来的那个主题 id；与所有邻近主题都不同的，relation 填 "distinct"。
   两种情况 rationale 都要用中文说清：驱动、验证条件、事件/现金流/风险哪里不同。
5. price_indicator 和 related 只能从下面给出的可交易清单里选，原样照抄代码。
6. terms 至少 6 个中文同义说法，覆盖这个争论在研报里会被叫的各种名字，
   不要只是把给定短语切碎重排。
7. key_question 必须写成一句 1–6 个月内能被证实或证伪的问题，且句中含「个月」。
8. id 用大写英文加连字符，看得出主题内容，如 FED-HAWKISH-TURN。

写卡时只输出一个 JSON 对象：
{"id": "...", "label": "中文标题", "key_question": "未来1–6个月，……？",
 "terms": ["...", ...], "price_indicator": "US.XXX", "related": ["US.YYY"],
 "default_direction": "↑ 或 ↓", "relation": "distinct 或 split",
 "of": "split 时填被拆的主题id，否则 null", "rationale": "中文理由"}"""


def _priceable_menu(con, as_of: date, limit: int = 200) -> list[dict]:
    """Instruments a theme born on `as_of` is allowed to point at.

    Listed on or before `as_of`: an indicator that did not exist yet would let
    a replayed week name a theme after an instrument the week could not have
    traded, which is the same look-ahead the universe filter removes downstream.
    """
    return [dict(r) for r in db.q(
        con, "SELECT futu_code, name FROM instruments "
             "WHERE COALESCE(priceable,0)=1 AND futu_code IS NOT NULL "
             "AND (first_seen_d IS NULL OR first_seen_d <= ?) "
             "ORDER BY futu_code LIMIT ?", [as_of.isoformat(), limit])]


#: How many neighbouring registered themes the prompt describes. Three covers
#: every real cluster seen so far (the largest overlap list on 2026-09-02 had
#: one clear leader and two tails) without turning the prompt into the whole
#: registry, which would invite the model to align with a theme it shares two
#: documents with.
NEIGHBOURS_IN_PROMPT = 3


def neighbours(cluster: dict, as_of: date, limit: int = NEIGHBOURS_IN_PROMPT) -> list[dict]:
    """The registered themes a cluster's evidence overlaps, most-shared first.

    What the model is shown so it can say "same debate as X", "split of X" or
    "distinct" against X's actual key question and vocabulary, rather than
    against a name it may misremember. The as-of clamp keeps a replayed week
    from being told about a theme it had not yet registered.
    """
    overlap = ((cluster.get("relation") or {}).get("overlap") or {})
    by_id = {t.id: t for t in lexicon.all_themes(as_of)}
    out = []
    for tid, n in sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
        t = by_id.get(tid)
        if t is None:
            continue
        out.append({"id": t.id, "label": t.label, "key_question": t.key_question,
                    "terms": list(t.terms[:6]), "shared_docs": int(n)})
    return out


def _mint_prompt(con, cluster: dict, as_of: date, note: str = "",
                 minted: list[dict] | None = None) -> str:
    menu = "\n".join(f"  {i['futu_code']}  {i['name'] or ''}".rstrip()
                      for i in _priceable_menu(con, as_of))
    ev = "\n".join(
        f"  [{e['d']}] {e.get('institution') or '未署名'}: {e['title']}"
        + (f"（已命中 {'、'.join(e['matched'])}）" if e.get("matched") else "")
        for e in (cluster.get("evidence") or [])[:14])
    head = f"当周为 {as_of.isoformat()}。\n\n" + (f"上一次尝试被拒：{note}\n\n" if note else "")
    if minted:
        head += ("本周已经命名过的主题（说的是同一个争论就返回 "
                 '{"skip": "与 X 重复"}）：\n'
                 + "\n".join(f"  {m.get('id')} {m.get('label') or ''}："
                              f"{'、'.join((m.get('terms') or [])[:6])}"
                              for m in minted) + "\n\n")
    rel = cluster.get("relation") or {}
    near = neighbours(cluster, as_of)
    if near:
        head += (f"邻近的已注册主题（这簇 {cluster['n_docs']} 篇证据里有 "
                 f"{rel.get('n_docs_matched', '?')} 篇同时命中了它们；机械判定 "
                 f"{rel.get('kind')}" + (f" of {rel.get('of')}" if rel.get("of") else "")
                 + "）：\n"
                 + "\n".join(f"  {t['id']} {t['label']}（共享 {t['shared_docs']} 篇）\n"
                             f"    关键问题：{t['key_question']}\n"
                             f"    词项：{'、'.join(t['terms'])}"
                             for t in near)
                 + "\n\n请先判断：与其中某个是同一争论（same_debate）、是它的一部分"
                   "但驱动或验证条件不同（split）、还是都不同（distinct）。\n\n")
    else:
        head += "邻近的已注册主题：无（这簇证据没有命中任何已注册主题）。\n\n"
    return (f"{head}反复出现的短语（{cluster['n_docs']} 篇 / "
            f"{cluster['n_institutions']} 家机构 / {cluster['n_days']} 天 / "
            f"lift {cluster['max_lift']}）：\n"
            f"  {'、'.join(cluster['terms'])}\n\n"
            f"标题证据：\n{ev}\n\n可交易清单（只能从这里选）：\n{menu}")


class MintSkipped(RegistrationError):
    """The cluster is real corpus noise, not a macro debate. Not a failure."""


class MintMerged(MintSkipped):
    """The cluster is a registered theme under this week's wording.

    A skip with a payload: no new theme, but `theme_id` should now also match
    `new_terms`, and `rationale` says why the model read the two as one
    debate. `discover` turns it into an alias line and a journal note.
    """

    def __init__(self, theme_id: str, new_terms: list[str], rationale: str):
        super().__init__(f"与 {theme_id} 是同一争论：{rationale[:160]}")
        self.theme_id = theme_id
        self.new_terms = new_terms
        self.rationale = rationale


def mint(con, cluster: dict, as_of: date, infer, *, attempts: int = 2,
         minted: list[dict] | None = None,
         minted_note: str = "") -> dict:
    """Write a registrable theme card for a discovered phrase cluster.

    `candidates` returns evidence, not a theme: terms, counts and doc ids, with
    no id, label, key question or price indicator. `validate` requires all four.
    So every candidate the weekly run proposed was rejected on arrival — the
    auto-registration wired on 2026-08-26 could not register anything, and the
    registry sat at its two hand-curated rows while `theme_register_failed`
    absorbed the evidence. Naming is the missing step, and it is a semantic job:
    deciding that 「美联储鹰派转向」 is a debate expressible as US.TLT, and what
    else the same debate gets called, is exactly what the dictionary cannot do.

    The model may only choose an instrument from the as-of menu, and a rejected
    card is retried once with the rejection quoted back, so a fixable slip
    (three synonyms instead of six) does not cost the theme. A cluster the model
    declines to call a macro debate raises `MintSkipped`, which is a finding —
    「这簇短语不是主题」 — and not the same event as a failed registration.

    `minted` carries the cards this same week already produced. `validate`
    rejects synonyms owned by a *registered* theme, which nothing in this week
    is yet: on 2026-06-24 the clusters 「日本股票」 and 「日经」 were two names
    for one debate and both minted cleanly, as JAPAN-EQUITY-STRUCTURAL-BULL and
    JAPAN-EQUITY-BULL. Two registry rows for one argument double-count the same
    reports in D forever, and the registry is append-only.

    The guard has two halves, and they are not equally strong. `_overlaps` is
    mechanical and certain, and catches only what `validate` would catch: a
    synonym one card shares with another. Two cards can argue the same thing
    with no phrase in common — 「日经225上行」 against 「日经225目标价上调」 —
    and that half is the model's, which is why the week's earlier cards go into
    the prompt with an instruction to decline. It did decline, on the case
    above. Stating the split rather than implying the check is complete: a
    near-duplicate whose wording does not overlap will get through.

    Since 2026-09-07 the prompt also carries the *registered* themes the
    cluster's evidence overlaps (`neighbours`), and the model must place the
    cluster against them. Three answers come back through three channels:
    `same_debate` raises `MintMerged` carrying the theme id and the new words
    (an alias, never a card); `split` and `distinct` return a card whose
    `relation`, `split_from` and `rationale` `validate` then checks. An answer
    that names a theme the week cannot see, or a split without a parent, is
    quoted back and retried like any other fixable slip.
    """
    if infer is None:
        raise RegistrationError("命名主题需要模型推理，本次运行没有可用的 inference 端口")
    near = {t["id"] for t in neighbours(cluster, as_of)}
    legal = {t.id for t in lexicon.all_themes(as_of)}
    note = ""
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        c = infer.complete(_mint_prompt(con, cluster, as_of, note, minted),
                           system=MINT_SYSTEM, temperature=0.2, max_tokens=2000)
        try:
            row = _parse_card(c.text)
        except ValueError as e:
            note, last = str(e), e
            continue
        relation = str(row.get("relation") or "").strip()
        of = str(row.get("of") or row.get("split_from") or "").strip() or None
        if row.get("skip") or relation == "same_debate":
            if relation != "same_debate":
                raise MintSkipped(str(row["skip"])[:200])
            # A merge must point at a theme this week can see. Pointing at a
            # theme registered later, or at an id the model made up, is not a
            # finding — it is the model misreading the neighbour list, which
            # a second attempt with the list quoted back usually fixes.
            if of not in legal:
                note = (f"relation=same_debate 时 of 必须是邻近主题之一"
                        f"（{'、'.join(sorted(near)) or '本簇没有邻近主题'}），"
                        f"得到 {of!r}")
                last = RegistrationError(note)
                continue
            new_terms = [str(t).strip() for t in (row.get("new_terms") or [])
                         if str(t).strip()]
            raise MintMerged(of, new_terms, str(row.get("rationale") or
                                                 row.get("skip") or "").strip())
        if near and relation not in ("split", "distinct"):
            note = ("这簇证据与已注册主题有重叠，必须明确 relation 是 "
                    "same_debate、split 还是 distinct，并给出 rationale")
            last = RegistrationError(note)
            continue
        if near and not str(row.get("rationale") or "").strip():
            note = "缺少 rationale：请用中文说明与邻近主题在驱动/验证条件/事件上的区别"
            last = RegistrationError(note)
            continue
        row["relation"] = relation or "distinct"
        row["split_from"] = of if row["relation"] == "split" else None
        row["evidence_doc_ids"] = list(cluster.get("doc_ids") or
                                       [e["doc_id"] for e in cluster.get("evidence") or []])[:20]
        rel = cluster.get("relation") or {}
        row["provenance"] = [
            f"以{as_of.isoformat()}当周 {cluster['n_docs']}篇/"
            f"{cluster['n_institutions']}家机构/{cluster['n_days']}天 "
            f"lift{cluster['max_lift']} 的研报簇为依据；其中 "
            f"{rel.get('n_docs_matched', 0)} 篇已命中旧主题"
            + (f"，机械判定 {rel.get('kind')}"
               + (f" of {rel.get('of')}" if rel.get("of") else "")
               if rel.get("kind") else "")]
        if minted_note:
            # A theme named in a replay is named by a model that has seen the
            # weeks after `as_of`. The registry row says so in its own words,
            # so a reader of the theme card can tell a live discovery from a
            # backfilled one without consulting the run that made it.
            row["provenance"].append(minted_note)
        try:
            card = validate(con, row, as_of)
        except RegistrationError as e:
            note, last = str(e), e
            continue
        clash = _overlaps(card, minted or [])
        if clash:
            note = (f"与本周已命名的 {clash} 说的是同一个争论；"
                    f"要么换一个真正不同的争论，要么返回 skip")
            last = RegistrationError(note)
            continue
        return card
    raise RegistrationError(f"命名两次都没通过校验：{last}")


def _overlaps(card: dict, minted: list[dict]) -> str | None:
    """The id of an already-minted card that claims the same debate, if any.

    Same test `validate` applies against registered themes: a synonym shared in
    either direction, because 「日经225」 and 「日经」 are one name written two
    ways. This is the mechanical half of the guard only — it does not detect
    two cards that argue one debate in non-overlapping words; see `mint`.

    Sharing a price indicator is deliberately *not* a duplicate. 95 listed
    instruments have to carry every macro debate, so a carry-trade theme and a
    Fed-hawkishness theme both reach for US.UUP while arguing about different
    things. That rule was tried on 2026-09-05 and rejected
    GLOBAL-CARRY-TRADE-RESURGENCE against FED-HAWKISH-TURN on its first outing.
    """
    terms = {t.lower() for t in card["terms"]}
    for m in minted:
        prior = {t.lower() for t in m["terms"]}
        if any(a in b or b in a for a in terms for b in prior):
            return m["id"]
    return None


def _parse_card(text: str) -> dict:
    """Parse the model's card, tolerating fences and reasoning preambles.

    Shares the shape of `strategies._gen.parse_json` rather than importing it:
    themes must not depend on the generator package, which imports this module.
    """
    t = re.sub(r"(?s)<think>.*?</think>", "", (text or "").strip())
    if "</think>" in t:
        t = re.sub(r"(?s)^.*?</think>", "", t).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        row = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("回复里找不到 JSON 对象") from None
        try:
            row = json.loads(t[i:j + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败：{e}") from None
    if not isinstance(row, dict):
        raise ValueError(f"期望一个 JSON 对象，得到 {type(row).__name__}")
    return row


def register(con, row: dict, as_of: date,
             path: Path | None = None) -> lexicon.Theme:
    """Validate and append one theme to the registry, then reload the lexicon."""
    clean = validate(con, row, as_of)
    p = path or lexicon.registry_write_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")
    lexicon.reload_registry()
    return lexicon.THEME_BY_ID[clean["id"]]


# ---------------------------------------------------------------------------
# Aliases: merge-by-name, the outcome that is not a registration
# ---------------------------------------------------------------------------
# One character, not a run: `_CJK` above is the n-gram tokenizer and takes
# whole runs. Shadowing it here silently turned every mined phrase into single
# characters on 2026-09-07 — zero candidates, no error. Named for what it is.
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def _is_cjk(s: str) -> bool:
    return bool(_CJK_CHAR.search(s))


def complete_phrase(term: str, titles: list[str], *, share: float = 0.8,
                    max_ext: int = 4) -> str:
    """Grow an n-gram fragment back into the phrase the titles actually use.

    Discovery mines character n-grams, so a real phrase arrives cut: 「绩超预期」
    for 业绩超预期, 「期并」 for 预期并上调. Written as an alias, the fragment
    would match everything the phrase matches *and* whatever else happens to
    contain the fragment. The repair is mechanical: while one and the same
    neighbouring character precedes (or follows) at least `share` of the
    term's occurrences in the evidence titles, that character belongs to the
    phrase. A neighbour that varies is a word boundary.
    """
    t = term
    for side in ("left", "right"):
        for _ in range(max_ext):
            neigh: dict[str, int] = {}
            total = 0
            for title in titles:
                start = 0
                while True:
                    i = title.find(t, start)
                    if i < 0:
                        break
                    total += 1
                    ch = title[i - 1] if side == "left" and i > 0 else (
                        title[i + len(t)] if side == "right" and i + len(t) < len(title)
                        else "")
                    if ch and _CJK_CHAR.match(ch):
                        neigh[ch] = neigh.get(ch, 0) + 1
                    start = i + 1
            if total < 2 or not neigh:
                break
            ch, n = max(neigh.items(), key=lambda kv: kv[1])
            if n / total < share:
                break
            t = (ch + t) if side == "left" else (t + ch)
    return t


def alias_terms_ok(theme: "lexicon.Theme", terms: list[str],
                   titles: list[str] | None) -> tuple[list[str], dict[str, str]]:
    """Which of the model's `new_terms` may become aliases, and why not the rest.

    Rules, each of which caught a real line on 2026-07-29 before it was
    written: a fragment is completed against the evidence titles; anything
    under three CJK characters (four otherwise) is a generic word, not a
    name (「平淡」「议前」「期并」); a term that contains one of the theme's
    existing words is kept (redundant for matching, harmless, and real
    wording); one contained in an existing word is a fragment of it; and when titles are
    supplied the term must occur verbatim in at least two of them, or it is
    the model's paraphrase rather than the corpus's wording.
    """
    have = [x.lower() for x in theme.terms]
    ok: list[str] = []
    why: dict[str, str] = {}
    for raw in terms:
        t = str(raw).strip()
        if not t:
            continue
        if titles:
            t = complete_phrase(t, titles)
        low = t.lower()
        if low in have:
            why[raw] = f"「{t}」已经是它的词项"
            continue
        n_min = 3 if _is_cjk(t) else 4
        if len(t) < n_min:
            why[raw] = f"「{t}」太短，是通用词不是叫法"
            continue
        # A term that *contains* an existing word (「业绩超预期」 for a theme
        # that already has 「业绩」) is redundant for matching, but harmless,
        # and it is still the corpus's real wording — written, not refused.
        outer = [h for h in have if low in h]
        if outer:
            why[raw] = f"「{t}」只是词项「{outer[0]}」的片段"
            continue
        if titles is not None:
            hits = sum(1 for x in titles if t in x)
            if hits < 2:
                why[raw] = f"「{t}」在证据标题里只逐字出现 {hits} 次"
                continue
        if low not in {x.lower() for x in ok}:
            ok.append(t)
    return ok, why


def add_alias(con, theme_id: str, terms: list[str], as_of: date, *,
              rationale: str = "", evidence_doc_ids: list[str] | None = None,
              candidate_terms: list[str] | None = None,
              path: Path | None = None,
              titles: list[str] | None = None) -> dict:
    """Record that `theme_id` also goes by `terms` from `as_of` on.

    The append-only answer to "same debate, new name". Every rejection below
    mirrors one in `validate`, because an alias changes what a theme matches
    exactly as a new registration would:

      * the theme must exist and be registered on or before `as_of` — an
        alias dated before its theme would let the theme score a day it did
        not exist for, through the words instead of the id;
      * a word another theme already owns (as of `as_of`, aliases included) is
        refused, or the same report would count in D for both — the check
        `validate` calls `stolen`;
      * words the theme already has are dropped, and if nothing is left that
        is an error rather than an empty line: the model said it found new
        wording, and a no-op alias would record the finding as done.

    `con` is unused today and kept in the signature so a later check that
    needs the corpus (does the alias actually occur in this week's reports?)
    does not change every caller.
    """
    del con  # see docstring
    by_id = {t.id: t for t in lexicon.all_themes(as_of)}
    t = by_id.get(theme_id)
    if t is None:
        raise RegistrationError(
            f"别名指向的主题 {theme_id!r} 在 {as_of.isoformat()} 尚未注册或不存在")
    # Ownership first, on the raw words: a word another theme holds is refused
    # loudly whatever else is wrong with it. Filtering for length or fragments
    # before this check would let 「通胀」 (two characters) slip past as
    # "too short" instead of "belongs to INFLATION", and the log would say
    # the wrong thing about why nothing was written.
    raw = [str(x).strip() for x in terms if str(x).strip()]
    others = {x.lower() for o in by_id.values() if o.id != theme_id for x in o.terms}
    stolen = [s for s in raw if s.lower() in others]
    if stolen:
        raise RegistrationError(
            f"{theme_id} 的别名 {stolen} 已属于其它已注册主题，会让同一篇研报在 D 里"
            f"被计两次")
    clean, why = alias_terms_ok(t, raw, titles)
    if not clean:
        raise RegistrationError(
            f"{theme_id} 的别名没有可写的新词：" + ("；".join(why.values())
                                                if why else f"{raw} 都已经是它的词项"))
    row = {
        "theme_id": theme_id,
        "terms": clean,
        "as_of": as_of.isoformat(),
        "rationale": str(rationale or "").strip(),
        "evidence_doc_ids": [str(d) for d in (evidence_doc_ids or [])][:20],
        "candidate_terms": [str(x) for x in (candidate_terms or [])][:12],
    }
    p = path or lexicon.aliases_write_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    lexicon.reload_registry()
    return row


# ---------------------------------------------------------------------------
# Freezing the definition set a period scored with
# ---------------------------------------------------------------------------
def _sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def snapshot(as_of: date | str) -> dict:
    """The theme definitions in force on `as_of`, hashed.

    Written as `A_theme_set.json` before 筛选A and carried as `theme_set_sha`
    on the topics step and the topic verdicts, so any period's scores can be
    tied to the exact vocabulary that produced them — what Jon asked for as
    「读取后续价格前冻结当期主题定义与版本」. The hash covers everything that
    decides a match or a score: id, registration day, key question, indicator,
    direction, `require`, and the term list *with aliases merged as of that
    day*. Two runs of the same period with the same registry and alias files
    get the same sha; a registration or an alias dated on or before the period
    changes it; one dated after does not, which is the as-of clamp made
    checkable.
    """
    d = as_of if isinstance(as_of, str) else as_of.isoformat()
    rows = []
    for t in sorted(lexicon.all_themes(d), key=lambda t: t.id):
        terms = sorted({x.lower() for x in t.terms})
        rows.append({
            "id": t.id, "label": t.label, "registered_d": t.registered_d,
            "origin": t.origin, "key_question": t.key_question,
            "price_indicator": t.price_indicator,
            "default_direction": t.default_direction,
            "require": sorted(x.lower() for x in t.require),
            "terms_sha": _sha(terms), "n_terms": len(terms),
            "n_alias_terms": len(t.alias_terms),
            "aliases_through": t.aliases_through,
            "relation": t.relation or None, "split_from": t.split_from,
        })
    return {
        "as_of": d,
        "lexicon_version": lexicon.LEXICON_VERSION,
        "n_themes": len(rows),
        "theme_set_sha": _sha(rows),
        "themes": rows,
    }


# ---------------------------------------------------------------------------
# The weekly discovery loop
# ---------------------------------------------------------------------------
def discover(con, as_of: date, infer, *, step=None, log=None,
             limit: int = MAX_CANDIDATES, scope: str = SCOPE_ALL,
             registry_path: Path | None = None,
             aliases_path: Path | None = None,
             minted_note: str = "",
             corpus: list[dict] | None = None) -> dict:
    """Mine, name and record this week's themes; report through `step`.

    Lifted out of the orchestrator on 2026-09-07 so the loop can be exercised
    against a scripted model without a platform, and so the orchestrator's
    discovery block reads as one call. `step(name, **fields)` is the journal
    (`RunJournal.step`); `log` is the console line. Journal steps written:

      * `theme_register_failed` — one candidate the model named but
        `validate` refused, with the reason;
      * `theme_merge_note` — one candidate judged the same debate as a
        registered theme: which theme, the new words, the reason, and whether
        the alias was written (it is not, and the note says so, when the
        model gave no new words or the words belong to another theme);
      * `theme_discovery` — the summary, once, last. With `error` set and no
        registrations when there is no model to name with: said once with the
        count it cost, not once per candidate.

    Returns the summary the last step carries.
    """
    step = step or (lambda name, **f: None)
    log = log or (lambda *a: None)
    disc = candidates(con, as_of, limit=limit, scope=scope, corpus=corpus)
    cands = disc.get("candidates") or []
    summary = {"coverage_pct": disc.get("coverage_pct"),
               "unmatched": disc.get("unmatched"),
               "mined": disc.get("mined"), "scope": scope,
               "candidates": len(cands), "registered": [], "merged": [],
               "skipped": [], "failed": 0}
    if infer is None and cands:
        # Naming needs the model. Without it every candidate would raise the
        # same rejection and the journal would carry one copy per candidate —
        # the shape of noise that hid the missing naming step in the first
        # place. Said once, with the count it cost.
        err = f"本次运行没有 inference 端口，{len(cands)} 个候选无法命名"
        step("theme_discovery", **summary, error=err)
        log(f"  主题发现  {len(cands)} 个候选待命名，但本次运行没有模型端口"
            f"——本周不注册新主题")
        return {**summary, "error": err}
    cards: list[dict] = []
    for c in cands:
        head = (c.get("terms") or [None])[0]
        try:
            card = mint(con, c, as_of, infer, minted=cards,
                        minted_note=minted_note)
            t = register(con, card, as_of, path=registry_path)
            cards.append(card)
            summary["registered"].append(t.id)
        except MintMerged as e:
            # Same debate, new name. The alias is the record; the note says
            # what was merged and why, so the merge can be argued with later.
            note = {"theme_id": e.theme_id, "new_terms": e.new_terms,
                    "rationale": e.rationale[:400],
                    "candidate_terms": (c.get("terms") or [])[:6],
                    "n_docs": c.get("n_docs"),
                    "relation": c.get("relation"), "alias_written": False}
            if not e.new_terms:
                note["error"] = "模型判为同一争论但没有给出新叫法，不写别名"
            else:
                try:
                    add_alias(con, e.theme_id, e.new_terms, as_of,
                              rationale=e.rationale,
                              evidence_doc_ids=c.get("doc_ids"),
                              candidate_terms=c.get("terms"),
                              path=aliases_path,
                              titles=[str(ev.get("title") or "")
                                      for ev in (c.get("evidence") or [])])
                    note["alias_written"] = True
                except RegistrationError as err:
                    note["error"] = str(err)[:200]
            step("theme_merge_note", **note)
            summary["merged"].append({"theme_id": e.theme_id,
                                      "new_terms": e.new_terms,
                                      "alias_written": note["alias_written"]})
            log(f"  主题归并  {'、'.join((c.get('terms') or [])[:3])} → "
                f"{e.theme_id}" + ("" if note["alias_written"]
                                    else f"（未写别名：{note.get('error')}）"))
        except MintSkipped as e:
            # Corpus noise the model declined to call a debate. A finding, not
            # a failure — 「预览」 recurring in forty titles is not a theme, and
            # recording it as a failed registration would bury the ones that are.
            summary["skipped"].append({"terms": (c.get("terms") or [])[:3],
                                       "why": str(e)[:120]})
        except Exception as e:  # noqa: BLE001 — one bad candidate must not end the week
            summary["failed"] += 1
            step("theme_register_failed", candidate=head, error=str(e)[:200])
    step("theme_discovery", **summary)
    if summary["registered"]:
        log(f"  主题发现  新注册 {len(summary['registered'])} 个: "
            f"{', '.join(summary['registered'])}")
    else:
        log(f"  主题发现  无新主题（研报覆盖率 {disc.get('coverage_pct')}%，"
            f"归并 {len(summary['merged'])}，跳过 {len(summary['skipped'])}）")
    return summary


def dormant(con, as_of: date, quiet_days: int = 20) -> list[str]:
    """Themes with no scored evidence for `quiet_days` — excluded from quota.

    Never deleted: outcomes and past batches reference them, and a theme that
    goes quiet for a month may be the most interesting thing in the book when
    it comes back.
    """
    # `themes.d` is the D factor score, not a date — the scoring date column is
    # `as_of`. Getting this wrong does not raise in SQLite, which orders numbers
    # before strings, so `d <= '2026-08-06'` silently matched every row and
    # MAX(d) returned a factor score posing as a date.
    rows = db.q(con, "SELECT theme_id, MAX(as_of) AS last_d FROM themes "
                     "WHERE as_of <= ? AND COALESCE(n_items,0) > 0 "
                     "GROUP BY theme_id",
                [as_of.isoformat()])
    last = {r["theme_id"]: r["last_d"] for r in rows}
    cutoff = (as_of - timedelta(days=quiet_days)).isoformat()
    out = []
    for t in lexicon.all_themes(as_of):
        seen = last.get(t.id)
        if seen is None:
            # Never seen and registered long ago: it was a bad registration.
            if t.registered_d <= cutoff:
                out.append(t.id)
        elif seen <= cutoff:
            out.append(t.id)
    return sorted(out)
