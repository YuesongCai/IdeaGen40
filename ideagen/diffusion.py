"""Diffusion diagnosis: is a theme still at its source, spreading, or late?

yifu's path (2026-09-11): X / Reddit / independent writers first, sell-side
research last. With a social series and a research series per theme we can say,
mildly, where on that path a theme sits:

  源头期  social rising, research flat          — the early window
  扩散期  both rising                            — the narrative has volume
  晚期    research still high, social receding   — the summary stage
  研报先动 research rising, social flat          — not every theme starts online
  平稳    none of the above
  数据不足 too few social mentions to say anything

Everything here is **diagnosis, not score**. It reads `social_items` through
`social.items_as_of` (publication-time cut) and `documents` by `published_d`
(the same day axis scoring uses), and writes nothing into `themes` or TIS.

Matching. Research uses `lexicon.match_theme` unchanged, so a research mention
here is the same test `themes.window_items` and `scoring.collect_evidence`
apply. Social text is English prose, where substring matching is wrong in a
way the Chinese corpus never exposed: 「war」 is inside 「software」 and
「award」, 「rent」 inside 「current」. So social matching bounds ASCII terms on
word edges, and borrows scoring's evidence rule — a bare keyword in a summary is
not a mention; it needs two distinct terms, or one term in the title.

Themes registered with Chinese terms only (most discovered ones) cannot match
English posts at all. They are reported as 「词表无英文叫法」, not as a quiet
theme — reading-failure vs absence again.
"""

from __future__ import annotations

import math
import re
import statistics as st
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from . import config, db, lexicon
from .sources import social

STAGE_SOURCE = "源头期"
STAGE_SPREAD = "扩散期"
STAGE_LATE = "晚期"
STAGE_RESEARCH_FIRST = "研报先动"
STAGE_FLAT = "平稳"
STAGE_THIN = "数据不足"


# ---------------------------------------------------------------- matching
def _ascii(t: str) -> bool:
    return all(ord(c) < 128 for c in t)


class SocialMatcher:
    """Word-bounded matcher for one theme over English (and Chinese) posts."""

    def __init__(self, theme: lexicon.Theme):
        self.theme = theme
        terms = list(dict.fromkeys(list(theme.terms) + list(theme.alias_terms or ())))
        self.patterns = []
        for t in terms:
            low = t.lower().strip()
            if not low:
                continue
            if _ascii(low):
                self.patterns.append((low, re.compile(
                    r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])")))
            else:
                self.patterns.append((low, None))
        self.require = [r.lower() for r in theme.require]
        self.has_english = any(p is not None for _, p in self.patterns)

    def _hits(self, low: str) -> set[str]:
        out = set()
        for term, pat in self.patterns:
            if (pat.search(low) if pat is not None else term in low):
                out.add(term)
        return out

    def mentions(self, title: str, summary: str) -> bool:
        # scoring.collect_evidence's rule, plus one allowance: two distinct
        # terms, or one term with a scoreable (≥400 chars) text — and, because
        # a post's headline is its claim, one term in the title.
        tl = (title or "").lower()
        full = f"{tl} {(summary or '').lower()}"
        if self.require and not any(r in full for r in self.require):
            return False
        hits = self._hits(full)
        if len(hits) >= 2:
            return True
        if not hits:
            return False
        return len(full) >= 400 or bool(self._hits(tl))


def _days(as_of: date, n: int) -> list[str]:
    return [(as_of - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _as_date(d: date | str) -> date:
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


# ---------------------------------------------------------------- series
def _research_rows(con, days: list[str]) -> list[dict]:
    rows = db.q(con, "SELECT doc_id, published_d, title, summary, body FROM documents "
                     "WHERE published_d BETWEEN ? AND ?", (days[0], days[-1]))
    # Body capped like themes._text_of: the first 3,000 characters carry the
    # argument, and matching whole 40k-character bodies over four weeks turns a
    # one-second diagnosis into a minute for no change in which themes hit.
    return [{"d": r["published_d"], "title": r["title"] or "", "summary": r["summary"] or "",
             "text": " ".join(filter(None, (r["title"], r["summary"], (r["body"] or "")[:3000])))}
            for r in rows]


def _smooth_share(counts: list[int], totals: list[int], w: int) -> list[float]:
    out = []
    for i in range(len(counts)):
        a = max(0, i - w + 1)
        tot = sum(totals[a:i + 1])
        out.append(sum(counts[a:i + 1]) / tot if tot else 0.0)
    return out


def _seg(counts: list[int], totals: list[int], recent: int, prior: int) -> dict:
    n = len(counts)
    rc, rt = sum(counts[n - recent:]), sum(totals[n - recent:])
    pc, pt = sum(counts[n - recent - prior:n - recent]), sum(totals[n - recent - prior:n - recent])
    rs = rc / rt if rt else 0.0
    ps = pc / pt if pt else 0.0
    if ps > 0:
        ratio = rs / ps
    else:
        ratio = math.inf if rc > 0 else None
    return {"recent": rc, "prior": pc, "recent_total": rt, "prior_total": pt,
            "recent_share": round(rs, 5), "prior_share": round(ps, 5),
            "ratio": (None if ratio is None else ("inf" if ratio == math.inf else round(ratio, 2))),
            "_ratio": ratio}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        sx, sy = st.pstdev(xs), st.pstdev(ys)
    except st.StatisticsError:
        return None
    if sx == 0 or sy == 0:
        return None
    mx, my = st.fmean(xs), st.fmean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) * sx * sy)


def lead_lag(s: list[float], r: list[float], max_lag: int) -> tuple[int | None, float | None]:
    """Lag k maximising corr(s[t], r[t+k]); k > 0 means social leads by k days."""
    best: tuple[int | None, float | None] = (None, None)
    n = len(s)
    for k in range(-max_lag, max_lag + 1):
        pairs = [(s[t], r[t + k]) for t in range(n) if 0 <= t + k < n]
        if len(pairs) < 10:
            continue
        c = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
        if c is None:
            continue
        if best[1] is None or c > best[1] + 1e-9:
            best = (k, round(c, 3))
    return best


def _stage(sm: dict, rs: dict, n_social: int, matchable: bool) -> tuple[str, str]:
    rise, fall = config.DIFFUSION_RISE_RATIO, config.DIFFUSION_FALL_RATIO
    if not matchable:
        return STAGE_THIN, "词表无英文叫法，英文社交源匹配不到这个主题"
    if n_social < config.DIFFUSION_MIN_SOCIAL:
        return STAGE_THIN, (f"窗口内社交提及 {n_social} 条，少于 "
                            f"{config.DIFFUSION_MIN_SOCIAL} 条不下判断")

    def up(x: dict, min_recent: int) -> bool:
        return x["_ratio"] is not None and x["_ratio"] >= rise and x["recent"] >= min_recent

    def down(x: dict, min_prior: int) -> bool:
        return x["_ratio"] is not None and x["_ratio"] <= fall and x["prior"] >= min_prior

    s_up, s_down = up(sm, 2), down(sm, 2)
    r_up, r_down = up(rs, 3), down(rs, 3)
    fmt = lambda x: "新出现" if x["ratio"] == "inf" else f"×{x['ratio']}"  # noqa: E731
    if s_up and not r_up:
        return STAGE_SOURCE, f"社交近 {config.DIFFUSION_RECENT_DAYS} 天占比 {fmt(sm)}，研报 {fmt(rs) if rs['ratio'] is not None else '无'} 未跟"
    if s_up and r_up:
        return STAGE_SPREAD, f"社交 {fmt(sm)}、研报 {fmt(rs)} 同升"
    if s_down and not r_down and rs["recent"] >= config.DIFFUSION_MIN_RESEARCH:
        return STAGE_LATE, f"研报仍在（近期 {rs['recent']} 篇，{fmt(rs)}），社交 {fmt(sm)} 回落"
    if r_up and not s_up:
        return STAGE_RESEARCH_FIRST, f"研报 {fmt(rs)} 在升，社交没有同步"
    return STAGE_FLAT, "两条序列都没有越过升/退阈值"


# ---------------------------------------------------------------- diagnosis
def diagnose(con, as_of: date | str, *, lookback_days: int = config.DIFFUSION_LOOKBACK_DAYS,
             themes: Iterable[lexicon.Theme] | None = None) -> dict:
    as_of = _as_date(as_of)
    days = _days(as_of, lookback_days)
    idx = {d: i for i, d in enumerate(days)}
    themes = list(themes if themes is not None else lexicon.all_themes(as_of))
    L = len(days)

    social_items = social.items_as_of(con, as_of, since=days[0])
    s_tot = [0] * L
    for it in social_items:
        if it["published_d"] in idx:
            s_tot[idx[it["published_d"]]] += 1
    research = _research_rows(con, days)
    r_tot = [0] * L
    for r in research:
        if r["d"] in idx:
            r_tot[idx[r["d"]]] += 1

    rec, pri = config.DIFFUSION_RECENT_DAYS, config.DIFFUSION_PRIOR_DAYS
    w = config.DIFFUSION_SMOOTH_DAYS
    out_themes = []
    mentions_by_theme: dict[str, list[dict]] = {}
    for th in themes:
        m = SocialMatcher(th)
        s_cnt = [0] * L
        ments = []
        for it in social_items:
            i = idx.get(it["published_d"])
            if i is None:
                continue
            if m.mentions(it["title"], it["summary"]):
                s_cnt[i] += 1
                ments.append(it)
        mentions_by_theme[th.id] = ments
        r_cnt = [0] * L
        for r in research:
            i = idx.get(r["d"])
            if i is not None and lexicon.match_theme(r["text"], th) >= 1:
                r_cnt[i] += 1
        sm = _seg(s_cnt, s_tot, rec, pri)
        rs = _seg(r_cnt, r_tot, rec, pri)
        n_s, n_r = sum(s_cnt), sum(r_cnt)
        stage, why = _stage(sm, rs, n_s, m.has_english)
        s_sh = _smooth_share(s_cnt, s_tot, w)
        r_sh = _smooth_share(r_cnt, r_tot, w)
        lead, corr = (None, None)
        lead_why = None
        if n_s >= config.DIFFUSION_MIN_SOCIAL and n_r >= config.DIFFUSION_MIN_RESEARCH:
            lead, corr = lead_lag(s_sh, r_sh, config.DIFFUSION_MAX_LAG_DAYS)
            if corr is None or corr < config.DIFFUSION_MIN_CORR:
                lead_why = (f"两条序列相关性不足（最高 {corr}）" if corr is not None
                            else "序列无波动，算不出相关")
                lead = None
            elif lead is not None and abs(lead) >= config.DIFFUSION_MAX_LAG_DAYS:
                # A best lag pinned to the search edge is two trends sliding
                # past each other, not a lead — the correlation would keep
                # rising if the window were wider.
                lead_why = f"最佳滞后落在搜索边界 ±{config.DIFFUSION_MAX_LAG_DAYS} 天，不可信"
                lead = None
        else:
            lead_why = f"样本不足（社交 {n_s} / 研报 {n_r}）"
        for x in (sm, rs):
            x.pop("_ratio", None)
        out_themes.append({
            "theme_id": th.id, "label": th.label, "stage": stage, "stage_why": why,
            "lead_days": lead, "lead_corr": corr, "lead_why": lead_why,
            "matchable": m.has_english,
            "social": {"counts": s_cnt, "n": n_s, **sm},
            "research": {"counts": r_cnt, "n": n_r, **rs},
            "social_share": [round(x, 4) for x in s_sh],
            "research_share": [round(x, 4) for x in r_sh],
            "sample_social": [{"d": it["published_d"], "title": it["title"][:160],
                               "url": it["url"], "feed": it["feed"], "author": it["author"]}
                              for it in ments[-3:]][::-1],
        })

    feeds_seen = {it["feed"] for it in social_items}
    days_with = sum(1 for x in s_tot if x)
    notes = []
    prior_social = sum(s_tot[: L - rec])
    if prior_social < 3 * pri:
        notes.append(f"窗口前段社交样本偏薄（{prior_social} 条）：RSS 只保留近期若干条，"
                     "高频源翻不到那么早，升/退判断在早期偏乐观")
    order = {STAGE_SOURCE: 0, STAGE_SPREAD: 1, STAGE_LATE: 2, STAGE_RESEARCH_FIRST: 3,
             STAGE_FLAT: 4, STAGE_THIN: 5}
    out_themes.sort(key=lambda t: (order.get(t["stage"], 9), -t["social"]["n"]))
    return {
        "as_of": as_of.isoformat(), "lookback_days": lookback_days, "days": days,
        "coverage": {"social_items": len(social_items), "social_feeds": len(feeds_seen),
                     "social_days_with_items": days_with, "social_by_day": s_tot,
                     "research_docs": len(research), "research_by_day": r_tot},
        "thresholds": {"recent_days": rec, "prior_days": pri,
                       "rise_ratio": config.DIFFUSION_RISE_RATIO,
                       "fall_ratio": config.DIFFUSION_FALL_RATIO,
                       "min_social": config.DIFFUSION_MIN_SOCIAL,
                       "min_research": config.DIFFUSION_MIN_RESEARCH,
                       "max_lag_days": config.DIFFUSION_MAX_LAG_DAYS,
                       "min_corr": config.DIFFUSION_MIN_CORR, "smooth_days": w},
        "notes": notes,
        "themes": out_themes,
        "chatter": chatter(con, as_of, themes=themes, social_items=social_items,
                           research=research),
        "early_voices": early_voices(out_themes, mentions_by_theme, days,
                                     n_social=len(social_items),
                                     author_posts=_author_posts(social_items)),
        "in_tis": False,
    }


# ---------------------------------------------------------------- chatter
_STOP = set("""
a about above after again against all almost also am among an and another any are around as at
back be because been before being below between both but by can cannot could did do does doing
done down during each either else even ever every few for from further get gets getting got had
has have having he her here hers him his how however i if in into is it its itself just last
least less let like make makes making many may me might more most much must my near need needs
never new next no nor not now of off often on once one only or other our out over own per
perhaps put rather really said same say says see seems she should since so some still such take
than that the their them then there these they thing things think this those though through
thus to too two under until up upon us use used very via vs want was way we well were what
when where whether which while who whom why will with within without would yet you your
week weeks weekly today tomorrow yesterday day days year years month months time times
monday tuesday wednesday thursday friday saturday sunday january february march april june july
august september october november december jan feb mar apr jun jul aug sep sept oct nov dec
link links podcast episode newsletter subscribe post posts read reading continue appeared first
chart charts thought thoughts please daily instead source change center revised
roundup edition issue open thread comments note notes update updates quick short long big small good bad
great best better worse worst high higher low lower part three four five six seven eight nine ten
people world thing lot lots going know look looks looking something anything everything nothing
really actually right left top bottom end start started here's that's it's what's there's don't
can't won't isn't doesn't didn't i'm we're they're you're let's report reports data latest
""".split())

_WORD = re.compile(r"[A-Za-z][A-Za-z\-&\.]*[A-Za-z]|[A-Za-z]")
#: Chunks never span punctuation or numbers: "nearly $1 billion" must not
#: become the phrase "nearly billion".
_CHUNK = re.compile(r"[^A-Za-z'’\-&\. ]+|\.\s|\s-\s|—")
_BOILER = re.compile(r"The post .{0,200}? appeared first on .{0,120}?\.|Continue reading.*$|Read more.*$",
                     re.I)
_CJKRUN = re.compile(r"[一-鿿]{2,}")
#: Everyday finance collocations no registered theme owns but no reader needs
#: surfaced either. Kept short on purpose: the lift and multi-source gates do
#: most of the work; this only removes what survives them every single week.
_GENERIC_BIGRAMS = set("""
stock market|bond market|labor market|housing market|interest rate|interest rates|chief economist
central bank|federal reserve|united states|white house|wall street|financial times|new york
last week|this week|next week|last year|this year|next year|per cent|year over year
""".strip().replace("\n", "|").split("|"))


def _clean(text: str) -> str:
    t = (text or "").replace("’", "'")
    # Possessives and contractions: "Anthropic's" is "Anthropic"; "it's" is noise.
    t = re.sub(r"'s\b", "", t)
    return re.sub(r"\b\w+'\w+\b", " ", t)


def _english_phrases(text: str, lower_seen: set[str] | None = None) -> set[str]:
    """2–3-word phrases, plus single words that only ever appear capitalised.

    Single lowercase words are almost all generic ("tariffs", "rates") and the
    registered themes already own the generic finance vocabulary. Names —
    people, companies, bills — are what a source surfaces first. Headlines are
    Title Case, so capitalisation in one place proves nothing; a word counts as
    a name only if it never appears lowercase anywhere in the window
    (`lower_seen`).
    """
    out: set[str] = set()
    for chunk in _CHUNK.split(_clean(text)):
        toks = [t.strip(".-&") for t in _WORD.findall(chunk)]
        toks = [t for t in toks if t]
        low = [t.lower() for t in toks]
        for i, t in enumerate(toks):
            lw = low[i]
            if (len(lw) >= 4 and lw not in _STOP and t[0].isupper()
                    and lower_seen is not None and lw not in lower_seen):
                out.add(lw)
            for n in (2, 3):
                if i + n > len(toks):
                    break
                g = low[i:i + n]
                if g[0] in _STOP or g[-1] in _STOP or any(len(x) < 2 for x in g):
                    continue
                ph = " ".join(g)
                if ph not in _GENERIC_BIGRAMS:
                    out.add(ph)
    return out


def _lower_words(text: str) -> set[str]:
    return {w for w in re.findall(r"\b[a-z][a-z\-]{3,}\b", _clean(text))}


def _item_text(title: str, summary: str) -> str:
    return _BOILER.sub(" ", f"{title}. {(summary or '')[:280]}")


def _phrases_of(title: str, summary: str, lower_seen: set[str] | None = None) -> set[str]:
    text = _item_text(title, summary)
    out = _english_phrases(text, lower_seen)
    for run in _CJKRUN.findall(text):
        n = len(run)
        for size in (2, 3, 4):
            for i in range(n - size + 1):
                out.add(run[i:i + size])
    return out


def _owned_by_theme(p: str, known: list[str]) -> bool:
    pw = f" {p} "
    for k in known:
        if _ascii(k) and _ascii(p):
            if f" {k} " in pw or (f" {p} " in f" {k} "):
                return True
        elif k in p or p in k:
            return True
    return False


def _inside_known_terms(p: str, items: list[dict], known: list[str]) -> bool:
    """True if the phrase mostly exists only as a slice of a registered term.

    "cut odds" is not a new topic when every post that has it says "rate cut
    odds": blank out the registered terms and see whether the phrase survives
    in at least half of its posts.
    """
    multi = [k for k in known if " " in k and _ascii(k)]
    if not multi or not _ascii(p):
        return False
    survive = 0
    for it in items:
        low = _item_text(it["title"], it["summary"]).lower()
        for k in multi:
            low = re.sub(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", " | ", low)
        if _in_text(p, low):
            survive += 1
    return survive * 2 < len(items)


def _in_text(p: str, low: str) -> bool:
    if _ascii(p):
        return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", low) is not None
    return p in low


def chatter(con, as_of: date | str, *, themes: Iterable[lexicon.Theme] | None = None,
            social_items: list[dict] | None = None, research: list[dict] | None = None,
            window_days: int = config.CHATTER_WINDOW_DAYS,
            limit: int = config.CHATTER_LIMIT) -> dict:
    """Phrases several social sources are using this week that research is not.

    A hint list for theme discovery and for a reader, never a registration:
    `themes.discover` receives it read-only. A phrase is kept when at least
    `CHATTER_MIN_FEEDS` different sources used it in `CHATTER_MIN_ITEMS` posts,
    it is new relative to the same sources' previous three weeks, no registered
    theme already owns it, and research in the same window mentions it at most
    `CHATTER_MAX_RESEARCH_DOCS` times. The last test is English-against-a-mostly-
    Chinese corpus, so 「研报没跟」 can also mean 「研报用中文说了」 — said on
    the card, not hidden.
    """
    from . import themes as themes_mod

    as_of = _as_date(as_of)
    themes = list(themes if themes is not None else lexicon.all_themes(as_of))
    win = set(_days(as_of, window_days))
    base_days = set(_days(as_of - timedelta(days=window_days), 21))
    if social_items is None:
        social_items = social.items_as_of(con, as_of, since=min(base_days))
    if research is None:
        research = _research_rows(con, sorted(win))
    cur = [it for it in social_items if it["published_d"] in win]
    base = [it for it in social_items if it["published_d"] in base_days]

    docs: dict[str, set[str]] = defaultdict(set)
    feeds: dict[str, set[str]] = defaultdict(set)
    by_id = {}
    lower_seen: set[str] = set()
    for it in social_items:
        lower_seen |= _lower_words(it["summary"] or "")
    for it in cur:
        by_id[it["item_id"]] = it
        for p in _phrases_of(it["title"], it["summary"], lower_seen):
            docs[p].add(it["item_id"])
            feeds[p].add(it["feed"])
    base_df: dict[str, int] = defaultdict(int)
    for it in base:
        for p in _phrases_of(it["title"], it["summary"], lower_seen):
            base_df[p] += 1

    known = sorted({t.lower() for th in themes for t in list(th.terms) + list(th.alias_terms or ())})
    rlow = [f"{r['title']} {r['summary']}".lower() for r in research if r["d"] in win]
    kept = []
    use_lift = len(base) >= 30
    for p, ds in docs.items():
        if len(ds) < config.CHATTER_MIN_ITEMS or len(feeds[p]) < config.CHATTER_MIN_FEEDS:
            continue
        lift = None
        if use_lift:
            lift = (len(ds) / max(len(cur), 1)) / ((base_df.get(p, 0) + 1) / (len(base) + 1))
            if lift < config.CHATTER_MIN_LIFT:
                continue
        if _owned_by_theme(p, known) or _inside_known_terms(p, [by_id[i] for i in ds], known):
            continue
        n_r = sum(1 for t in rlow if _in_text(p, t))
        if n_r > config.CHATTER_MAX_RESEARCH_DOCS:
            continue
        kept.append({"phrase": p, "n_docs": len(ds), "docs": ds,
                     "n_feeds": len(feeds[p]), "research_docs": n_r,
                     "lift": None if lift is None else round(lift, 1)})
    kept = themes_mod._maximal(kept)
    kept.sort(key=lambda k: (-k["n_feeds"], -k["n_docs"], -(k["lift"] or 0)))
    out = []
    for k in kept[:limit]:
        its = sorted((by_id[i] for i in k["docs"]), key=lambda x: x["published_at"], reverse=True)
        out.append({"phrase": k["phrase"], "n_items": k["n_docs"], "n_feeds": k["n_feeds"],
                    "research_docs": k["research_docs"], "lift": k["lift"],
                    "feeds": sorted({i["feed"] for i in its}),
                    "first_d": min(i["published_d"] for i in its),
                    "samples": [{"d": i["published_d"], "title": i["title"][:160],
                                 "url": i["url"], "feed": i["feed"]} for i in its[:3]]})
    return {"as_of": as_of.isoformat(), "window_days": window_days,
            "n_social_items": len(cur), "lift_gate": use_lift, "items": out,
            "note": ("只作主题发现的参考提示，不自动登记。「研报未跟」按英文词组在研报标题/摘要里找，"
                     "研报若用中文讨论同一件事，这里看不出来。")}


def discovery_hints(con, as_of: date | str, limit: int = 10) -> list[dict]:
    """The read-only hint list `themes.discover` records in its journal."""
    ch = chatter(con, as_of, limit=limit)
    return [{"phrase": x["phrase"], "n_items": x["n_items"], "n_feeds": x["n_feeds"],
             "research_docs": x["research_docs"], "feeds": x["feeds"][:5]}
            for x in ch["items"]]


# ---------------------------------------------------------------- early voices
EARLY_FRAMEWORK = ("对每个主题找出研报「变强」的那一天（7 日平滑占比首次 ≥ 此前中位数 × "
                   f"{config.EARLY_STRENGTHEN_RATIO}，且近 7 天至少 {config.DIFFUSION_MIN_RESEARCH} 篇），"
                   "列出在那天之前就在社交源里提过它的作者。一次命中是运气，同一个人在多个主题上"
                   "反复早于研报，才是要抓的超级个体——名单需要跨期累计才有意义。")


def _author_posts(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for it in items:
        out[it["author"] or it["feed"]] += 1
    return dict(out)


def early_voices(theme_rows: list[dict], mentions: dict[str, list[dict]], days: list[str],
                 *, n_social: int, author_posts: dict[str, int] | None = None) -> dict:
    if n_social < config.EARLY_MIN_SOCIAL_ITEMS:
        return {"available": False, "framework": EARLY_FRAMEWORK, "themes": [],
                "why": (f"窗口内社交条目 {n_social} 条，少于 {config.EARLY_MIN_SOCIAL_ITEMS} 条，"
                        "早期声音名单不下结论")}
    w = config.DIFFUSION_SMOOTH_DAYS
    out = []
    for t in theme_rows:
        r_sh, r_cnt = t["research_share"], t["research"]["counts"]
        s_day = None
        for i in range(w, len(days)):
            prior = [x for x in r_sh[:i] if x > 0]
            if not prior:
                continue
            med = st.median(prior)
            if r_sh[i] >= config.EARLY_STRENGTHEN_RATIO * med and \
                    sum(r_cnt[max(0, i - 6):i + 1]) >= config.DIFFUSION_MIN_RESEARCH:
                s_day = days[i]
                break
        if not s_day:
            continue
        voices: dict[str, dict] = {}
        for it in mentions.get(t["theme_id"]) or []:
            if it["published_d"] >= s_day:
                continue
            who = it["author"] or it["feed"]
            v = voices.setdefault(who, {"author": who, "feed": it["feed"], "n": 0,
                                        "first_d": it["published_d"], "title": it["title"][:160],
                                        "url": it["url"]})
            v["n"] += 1
            if it["published_d"] < v["first_d"]:
                v.update(first_d=it["published_d"], title=it["title"][:160], url=it["url"])
        if voices:
            vs = sorted(voices.values(), key=lambda v: v["first_d"])
            for v in vs:
                v["days_before"] = (date.fromisoformat(s_day) - date.fromisoformat(v["first_d"])).days
            out.append({"theme_id": t["theme_id"], "label": t["label"],
                        "strengthen_d": s_day, "voices": vs[:8]})
    # Across themes: who was early more than once. Divided by how much they
    # post, because a writer with a daily macro column is "early" on every
    # macro theme by sheer volume — that is coverage, not foresight.
    tally: dict[str, dict] = {}
    for t in out:
        for v in t["voices"]:
            a = tally.setdefault(v["author"], {"author": v["author"], "feed": v["feed"],
                                               "themes": [], "posts": (author_posts or {}).get(v["author"], 0)})
            a["themes"].append(t["theme_id"])
    repeat = [dict(a, n_themes=len(a["themes"]),
                   early_per_10_posts=(round(10 * len(a["themes"]) / a["posts"], 2) if a["posts"] else None))
              for a in tally.values() if len(a["themes"]) >= 2]
    repeat.sort(key=lambda a: (-(a["early_per_10_posts"] or 0), -a["n_themes"]))
    return {"available": True, "framework": EARLY_FRAMEWORK, "themes": out,
            "repeat": repeat[:10],
            "caveat": ("高产作者在每个宏观主题上都容易「更早」——按每 10 篇里早于研报的主题数排序，"
                       "单期名单只是候选，要跨期累计才能分出超级个体与高频评论。"),
            "why": None if out else "窗口内没有主题出现可识别的研报变强日，或变强前社交无人提及"}


# ---------------------------------------------------------------- snapshots
def _kv_key(as_of: str) -> str:
    return f"diffusion.snapshot.{as_of}"


def snapshot(con, as_of: date | str) -> dict:
    """Compute and store one diagnosis; the panel reads stored ones only.

    `/api/state` is polled every minute, and a four-week diagnosis re-matches
    thousands of documents. Computing it there would make every poll pay for a
    number that changes once a day.
    """
    d = diagnose(con, as_of)
    db.kv_set(con, _kv_key(d["as_of"]), d)
    idx = sorted(set((db.kv_get(con, "diffusion.index", []) or []) + [d["as_of"]]))[-60:]
    db.kv_set(con, "diffusion.index", idx)
    return d


def state_block(con, as_of: str | None = None) -> dict:
    """The stored diagnosis for `as_of` (or the latest on or before it)."""
    idx = db.kv_get(con, "diffusion.index", []) or []
    if not idx:
        return {"available": False, "why": "还没有算过扩散诊断（每日运行的社交阶段会算）",
                "periods": []}
    pick = None
    for d in sorted(idx, reverse=True):
        if as_of is None or d <= as_of:
            pick = d
            break
    if pick is None:
        return {"available": False, "periods": idx,
                "why": f"{as_of} 当天或之前没有扩散诊断（最早 {idx[0]}）"}
    snap = db.kv_get(con, _kv_key(pick))
    if not snap:
        return {"available": False, "periods": idx, "why": f"{pick} 的诊断快照读不到"}
    # Trim for the one-minute poll: the drawer draws shares, not raw counts,
    # and the per-day totals are for `ideagen social` output, not the page.
    snap = dict(snap)
    snap["coverage"] = {k: v for k, v in (snap.get("coverage") or {}).items()
                        if not k.endswith("_by_day")}
    snap["themes"] = [{**t, "social": {k: v for k, v in t["social"].items() if k != "counts"},
                       "research": {k: v for k, v in t["research"].items() if k != "counts"},
                       "social_share": [round(x, 3) for x in t.get("social_share") or []],
                       "research_share": [round(x, 3) for x in t.get("research_share") or []]}
                      for t in snap.get("themes") or []]
    return {"available": True, "periods": idx, "requested": as_of, **snap}


# ---------------------------------------------------------------- entry points
def daily_stage(con, as_of: date | str, *, log=print, http=None) -> dict:
    """`cmd_daily`'s social stage: fetch, then store today's diagnosis.

    Fetch failures are per-source and already recorded by `social.ingest`; the
    diagnosis runs on whatever is stored, so a dead feed thins the series
    rather than skipping the day.
    """
    rep = social.ingest(con, _as_date(as_of), http=http, log=log)
    snap = snapshot(con, as_of)
    staged = [t for t in snap["themes"] if t["stage"] not in (STAGE_THIN, STAGE_FLAT)]
    log(f"      扩散诊断 {snap['as_of']}：社交 {snap['coverage']['social_items']} 条 / "
        f"研报 {snap['coverage']['research_docs']} 篇；有阶段判断的主题 {len(staged)} 个；"
        f"社交热议未进研报 {len(snap['chatter']['items'])} 条")
    return {"ingest": {k: rep[k] for k in ("n_new", "n_fetched")}, "as_of": snap["as_of"]}


def cli(args) -> int:
    """`ideagen social`: fetch (optionally a backfill), then diagnose dates."""
    import json as _json

    con = db.init()
    if not args.no_fetch:
        social.ingest(con, lookback_days=args.lookback, full=args.backfill,
                      max_pages=args.max_pages)
    dates = [d.strip() for d in (args.diagnose or "").split(",") if d.strip()]
    if not dates and args.as_of:
        dates = [args.as_of]
    for d in dates:
        snap = snapshot(con, d)
        print(_json.dumps({
            "as_of": snap["as_of"], "coverage": {k: v for k, v in snap["coverage"].items()
                                                 if not k.endswith("_by_day")},
            "stages": [(t["theme_id"], t["stage"], t["lead_days"]) for t in snap["themes"]
                       if t["stage"] != STAGE_THIN],
            "chatter": [c["phrase"] for c in snap["chatter"]["items"]],
        }, ensure_ascii=False, indent=1))
    return 0
