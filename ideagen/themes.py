"""Theme discovery: mine candidate macro themes the dictionary cannot see.

A fixed theme list is a bet that the author already knows every macro debate
that will matter. Measured against the corpus that bet loses badly — 46% of
items matched none of the 16 seed themes, and among the misses were
GLP-1 与医保准入、韩国科技股重估、人形机器人、光模块出口管制、央行购金: all
well-sourced, all live, all invisible.

Discovery runs in two stages, deliberately split by who is good at what.

**Stage A — `candidates()`, mechanical, runs unattended.** Take the items in
the trailing window that matched *no* registered theme, mine repeated phrases
from them, keep only phrases that clear hard evidence gates, and cluster
phrases that travel together into candidate themes. This stage reads no prices
and makes no judgements; it only says "these N documents from M institutions
over K days are about something the dictionary has no word for".

**Stage B — registration, done by the generator.** For each candidate worth
admitting, the generator writes the theme: label, key question, synonyms,
price indicator. `register()` validates it and appends one line to
`themes/registry.jsonl`, stamped with the day it was registered.

The split matters because Stage B is where hindsight would enter. Two rules
keep it out, both enforced here rather than by good intentions:

  * `registered_d` may not be backdated, and `lexicon.all_themes(as_of)`
    excludes themes registered after `as_of` — a theme discovered today cannot
    score last week, so it can never be credited with a call it never made.
  * the price indicator must be priceable *and* is chosen from the candidate's
    own evidence, before any return is computed. Picking the instrument that
    already ran is the failure mode; `validate()` cannot detect intent, but
    `registered_d` makes the attempt worthless.
"""

from __future__ import annotations

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


def unmatched(con, as_of: date, days: int = WINDOW_DAYS) -> list[dict]:
    """Window items matching no theme registered as of `as_of`.

    The as-of clamp is not cosmetic: mining against *today's* dictionary would
    make a historical replay claim it had already discovered themes it had not.
    """
    themes = lexicon.all_themes(as_of)
    wdays = _window(as_of, days)
    rows = db.q(con,
                "SELECT doc_id,line,tier,title,institution,published_d,summary,body "
                "FROM documents WHERE published_d IN (%s)" % ",".join("?" * len(wdays)),
                wdays)
    out = []
    for r in rows:
        text = _text_of(r)
        if any(lexicon.match_theme(text, t) >= 1 for t in themes):
            continue
        inst = r["institution"] or lexicon.institution_of(text)
        out.append({
            "doc_id": r["doc_id"], "line": r["line"], "tier": r["tier"],
            "d": r["published_d"], "title": r["title"] or "",
            "institution": inst or f"sig:{lexicon.title_signature(r['title'] or '')[:12]}",
            "text": text,
        })
    return out


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
               limit: int = MAX_CANDIDATES) -> dict:
    """Candidate themes mined from the items no registered theme matched."""
    items = unmatched(con, as_of, days)
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
        lift = (len(ds) / n_win) / ((base_df.get(p, 0) + 1) / (base_n + 1))
        if lift < MIN_LIFT:
            continue
        kept.append({"phrase": p, "docs": ds, "n_docs": len(ds),
                     "n_inst": len(insts[p]), "n_days": len(days_seen[p]),
                     "lift": round(lift, 2)})

    kept = _maximal(kept)
    # Boilerplate is judged only now, on whole phrases. 维持买入评级 is rejected
    # here and takes its fragments with it, because they were absorbed above.
    kept = [k for k in kept if not _noise_composite(k["phrase"])]
    kept.sort(key=lambda k: (-k["n_docs"], -k["lift"], k["phrase"]))

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
        out.append({
            "terms": [p["phrase"] for p in maximal[:12]],
            "n_docs": len(ds),
            "n_institutions": len({e["institution"] for e in ev}),
            "n_days": len({e["d"] for e in ev}),
            "tiers": sorted({e["tier"] for e in ev}),
            "max_lift": max(p["lift"] for p in c["phrases"]),
            "evidence": [{"doc_id": e["doc_id"], "line": e["line"],
                          "tier": e["tier"], "d": e["d"],
                          "institution": e["institution"], "title": e["title"]}
                         for e in sorted(ev, key=lambda e: (e["tier"], e["d"]))[:14]],
        })
    out.sort(key=lambda c: (-c["n_docs"], -c["max_lift"]))

    themes = lexicon.all_themes(as_of)
    matched = len([1 for it in db.q(
        con, "SELECT title,summary,body FROM documents WHERE published_d IN (%s)"
        % ",".join("?" * len(_window(as_of, days))), _window(as_of, days))
        if any(lexicon.match_theme(
            " ".join(filter(None, (it["title"], it["summary"],
                                   (it["body"] or "")[:3000]))), t) >= 1
            for t in themes)])
    total = matched + len(items)
    return {
        "as_of": as_of.isoformat(),
        "window_days": days,
        "registered": len(themes),
        "corpus_total": total,
        "corpus_matched": matched,
        "coverage_pct": lexicon.coverage(matched, total),
        "unmatched": len(items),
        "gates": {"min_docs": MIN_DOCS, "min_institutions": MIN_INSTITUTIONS,
                  "min_days": MIN_DAYS, "min_lift": MIN_LIFT,
                  "min_cluster_docs": MIN_CLUSTER_DOCS},
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
    }


MINT_SYSTEM = """你在给一个宏观交易系统命名它自己刚发现的主题。

输入是一簇当周研报里反复出现、且现有主题词典一个都盖不住的短语，外加它们出现的
标题证据。你的唯一任务是把这簇短语写成一张主题卡。

铁律，逐条服从：
1. 只依据给出的短语和标题证据。不得使用你对这个日期之后的世界的任何了解——
   这张卡会被用来给当周打分，掺进后来的事就是泄露。
2. 主题必须是一个**能用做多标的表达的宏观争论**，不是一家公司、一条新闻、
   一个板块名词。写不成争论的，返回 {"skip": "原因"}。
3. price_indicator 和 related 只能从下面给出的可交易清单里选，原样照抄代码。
4. terms 至少 6 个中文同义说法，覆盖这个争论在研报里会被叫的各种名字，
   不要只是把给定短语切碎重排。
5. key_question 必须写成一句 1–6 个月内能被证实或证伪的问题，且句中含「个月」。
6. id 用大写英文加连字符，看得出主题内容，如 FED-HAWKISH-TURN。

只输出一个 JSON 对象：
{"id": "...", "label": "中文标题", "key_question": "未来1–6个月，……？",
 "terms": ["...", ...], "price_indicator": "US.XXX", "related": ["US.YYY"],
 "default_direction": "↑ 或 ↓"}"""


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


def _mint_prompt(con, cluster: dict, as_of: date, note: str = "",
                 minted: list[dict] | None = None) -> str:
    menu = "\n".join(f"  {i['futu_code']}  {i['name'] or ''}".rstrip()
                      for i in _priceable_menu(con, as_of))
    ev = "\n".join(
        f"  [{e['d']}] {e.get('institution') or '未署名'}: {e['title']}"
        for e in (cluster.get("evidence") or [])[:14])
    head = f"当周为 {as_of.isoformat()}。\n\n" + (f"上一次尝试被拒：{note}\n\n" if note else "")
    if minted:
        head += ("本周已经命名过的主题（说的是同一个争论就返回 "
                 '{"skip": "与 X 重复"}）：\n'
                 + "\n".join(f"  {m.get('id')} {m.get('label') or ''}："
                              f"{'、'.join((m.get('terms') or [])[:6])}"
                              for m in minted) + "\n\n")
    return (f"{head}反复出现的短语（{cluster['n_docs']} 篇 / "
            f"{cluster['n_institutions']} 家机构 / {cluster['n_days']} 天 / "
            f"lift {cluster['max_lift']}）：\n"
            f"  {'、'.join(cluster['terms'])}\n\n"
            f"标题证据：\n{ev}\n\n可交易清单（只能从这里选）：\n{menu}")


class MintSkipped(RegistrationError):
    """The cluster is real corpus noise, not a macro debate. Not a failure."""


def mint(con, cluster: dict, as_of: date, infer, *, attempts: int = 2,
         minted: list[dict] | None = None) -> dict:
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
    """
    if infer is None:
        raise RegistrationError("命名主题需要模型推理，本次运行没有可用的 inference 端口")
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
        if row.get("skip"):
            raise MintSkipped(str(row["skip"])[:200])
        row["provenance"] = [
            f"以{as_of.isoformat()}当周 {cluster['n_docs']}篇/"
            f"{cluster['n_institutions']}家机构/{cluster['n_days']}天 "
            f"lift{cluster['max_lift']} 的零匹配研报簇为依据"]
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
    p = path or lexicon.REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")
    lexicon.reload_registry()
    return lexicon.THEME_BY_ID[clean["id"]]


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
