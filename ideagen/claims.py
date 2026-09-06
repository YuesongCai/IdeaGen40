"""Opinion extraction with an object: which theme a stance is *about*.

G 分歧 used to be computed from `lexicon.stance_of(text)` over a whole
document. That function has no idea what the document is about: it counts
positive and negative words anywhere in the text and returns the sign of the
difference. Jon's counter-example (2026-09-06 §2) is a report that is bullish
gold and bearish silver — the gold theme should receive a +1 from it and the
silver theme a −1, and the old function returned 0 to both, the two stances
cancelling each other inside a document that in fact took a clear position on
each. His second example: 「盈利上调」 and 「利率下调」 are coded +1 / −1 by
the same word list, but the direction of a rate cut *for a theme* depends on
what the theme's key question asks; a fixed rule 「降息一定利好」 would be a
second mistake stacked on the first.

This module produces *claims*: one row per (document, theme, clause), each
carrying the object the stance was found next to, the direction, whether the
extractor declined to assign one, any condition or horizon attached, and the
original wording so a reader can check the coding against the text. Two paths
produce the same shape:

  * the clause path — mechanical, no model. Text is split into clauses on
    Chinese and English punctuation; a clause is attributed to the themes
    whose terms it mentions (or, when it mentions none, to the themes the
    discourse is currently about — the ones last mentioned, and before any
    mention, the ones in the title). Direction comes from the existing word
    lists *within that clause only*, which is what anchors the sign to the
    object. When the clause's object is a rate, a yield, a central bank or
    another macro variable whose direction has no fixed meaning for a theme,
    the clause is marked `unresolved` and given no sign, rather than the sign
    the word list would have picked.
  * the model path — one request per document covering every theme it
    matched, returning JSON. Results are cached by content hash, extractor
    version and theme-set fingerprint so a document is never sent twice for
    the same question; token usage is kept on the receipt so the cost Jon
    asked to measure is measured rather than estimated. A response that does
    not parse falls back to the clause path and says so in `source`.

The clause path is also the control: `topic_hgep` keeps the whole-document
word-list G as `G_keyword` beside the claim-based G, so the two can be compared
on the same periods before either is declared better (框架: 「同时保留机械词典
值作为对照落库」).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from . import lexicon

#: Bump when the extraction changes in a way that should invalidate cached
#: model output. The cache key carries it, so old rows are simply not found.
EXTRACTOR_VERSION = "claims/1"

#: Characters a clause ends on. `、` is deliberately absent: 「黄金、白银看多」
#: is one clause about two objects, and splitting it would leave 「黄金」 with
#: no stance word and 「白银看多」 with the only one.
#: Contrastive conjunctions split too: 「黄金看多但白银看空」 has no comma and
#: still holds two opposite claims about two objects. Bare 「而」 is not in the
#: list — it lives inside 从而 / 进而 / 然而 and would cut words in half.
#: The colon is a boundary too: 「贵金属：黄金看多」 is a header and a claim, and
#: keeping them together would hand the header's objects the claim's sign.
_CLAUSE_SPLIT = re.compile(r"[，。；！？：\n,.;!?:]+|但是|但|然而|不过|\s+(?:but|however|while)\s+")

#: Words that state a stance outright. A clause containing one is coded from
#: the word lists whatever else it mentions — 「降息利好黄金」 is +1 for gold
#: because the author said so, not because of a rule about rate cuts.
EXPLICIT_POS = ("看多", "看好", "利好", "增持", "超预期", "强于预期", "上修",
                "买入", "beat", "upgrade", "outperform", "bullish", "overweight")
EXPLICIT_NEG = ("看空", "看淡", "利空", "减持", "低于预期", "弱于预期", "下修",
                "卖出", "miss", "downgrade", "underperform", "bearish", "underweight")

#: Words whose sign depends on what moved. 「上调」 is good news for earnings
#: and unreadable for a policy rate until you know what the theme asks.
DIRECTION_DEPENDENT_ZH = ("上调", "下调", "加息", "降息", "上行", "下行", "上升",
                          "下降", "走高", "走低", "抬升", "回落", "收紧", "放松")
_DIRECTION_DEPENDENT_EN = re.compile(
    r"\b(cut|cuts|raise|raises|hike|hikes|lower|lowers|rise|rises|fall|falls|"
    r"tighten|tightening|ease|easing)\b")

#: Objects for which a direction word has no theme-independent sign. Rates,
#: yields, central banks, inflation, the currency: each can be good or bad for
#: a theme depending on its key question, and the clause path does not
#: pretend to know which. The list is data so Jon can extend it without
#: touching the coding logic.
RATE_OBJECTS_ZH = ("利率", "收益率", "央行", "联储", "政策利率", "基准利率", "国债",
                   "美债", "债券", "通胀", "汇率", "美元", "流动性", "货币政策")
_RATE_OBJECTS_EN = re.compile(
    r"\b(rate|rates|yield|yields|fed|fomc|ecb|boj|pboc|treasur\w*|inflation|"
    r"dollar|liquidity)\b")

#: Conditional and hedging markers, recorded on the claim rather than used to
#: discard it: the reader can see the stance was conditional, and the count
#: can weigh it later once we know whether that matters.
CONDITION_MARKERS = ("若", "如果", "假如", "一旦", "只要", "除非", "倘若", "取决于",
                     "if ", "unless", "provided", "depends on", "conditional on")
HEDGE_MARKERS = ("或将", "可能", "或许", "预计", "预期", "有望", "不确定", "may ",
                 "could ", "might ", "likely", "uncertain")
_HORIZON = re.compile(
    r"(未来\s*\d+\s*[–\-~到至]?\s*\d*\s*(?:个月|季度|年|周|个季度)|"
    r"年内|短期|中期|长期|中长期|下半年|上半年|[一二三四]季度|Q[1-4]|"
    r"\d+\s*[–\-]?\s*\d*\s*(?:-|\s)?(?:month|months|quarter|quarters|year|years|week|weeks)|"
    r"near[- ]term|medium[- ]term|long[- ]term|year[- ]end)", re.I)

_DIRECTION_WORDS = {
    "+1": 1, "1": 1, "看多": 1, "bullish": 1, "positive": 1, "up": 1,
    "-1": -1, "看空": -1, "bearish": -1, "negative": -1, "down": -1,
    "0": 0, "中性": 0, "neutral": 0,
    "弃判": None, "不确定": None, "unresolved": None, "abstain": None,
    "unknown": None,
}


# ---------------------------------------------------------------------------
# Text and identity

def doc_text(doc: dict[str, Any], body_chars: int = 3000) -> str:
    """The text the extractor reads: title, summary, and the head of the body.

    Same recipe as the matcher in `topic_hgep`, so what a claim quotes is
    inside what the theme was matched on.
    """
    return " ".join(filter(None, (doc.get("title"), doc.get("summary"),
                                  (doc.get("body") or "")[:body_chars])))


def theme_fingerprint(themes: Iterable[Any]) -> str:
    """Identity of the question set a cached extraction answered.

    A theme whose terms or key question changed is a different question, and
    Jon's note says as much: 「主题发生变化时，也要检查旧抽取结果是否仍能回答
    新的问题」. Fingerprinting the whole set means a changed theme misses the
    cache for every document, which is the safe direction.
    """
    parts = sorted((t.id, t.key_question, list(t.terms), list(t.require))
                   for t in themes)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()[:16]


def cache_key(text: str, themes: Iterable[Any],
              version: str = EXTRACTOR_VERSION) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(b"\x00" + version.encode() + b"\x00" + theme_fingerprint(themes).encode())
    return h.hexdigest()


def split_clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(text or "") if c and c.strip()]


def doc_clauses(doc: dict[str, Any], body_chars: int = 3000) -> list[str]:
    """Clauses of title, summary and body head, each field split on its own.

    Splitting the joined `doc_text` instead would glue the title onto the
    summary's first clause (the join is a space, not a stop), so a summary
    that repeats the body's opening sentence would no longer look repeated.
    """
    out: list[str] = []
    for part in (doc.get("title"), doc.get("summary"), (doc.get("body") or "")[:body_chars]):
        out.extend(split_clauses(part or ""))
    return out


# ---------------------------------------------------------------------------
# Clause path

def _has(low: str, terms: Iterable[str]) -> bool:
    return any(t.lower() in low for t in terms)


def is_rate_object_clause(clause: str) -> bool:
    """Whether a clause's stance would be about a macro variable whose sign
    for a theme is undetermined — a rate move, a yield move, a central bank."""
    low = clause.lower()
    dir_dep = _has(low, DIRECTION_DEPENDENT_ZH) or bool(_DIRECTION_DEPENDENT_EN.search(low))
    obj = _has(low, RATE_OBJECTS_ZH) or bool(_RATE_OBJECTS_EN.search(low))
    return dir_dep and obj


def clause_direction(clause: str) -> tuple[int, bool]:
    """(direction, unresolved) for one clause.

    Explicit stance words win. Failing those, a direction word next to a rate
    object is declined rather than signed — 「利率下调」 is not −1, it is
    「方向取决于主题在问什么」. Everything else goes through the word lists.
    """
    low = clause.lower()
    if _has(low, EXPLICIT_POS) or _has(low, EXPLICIT_NEG):
        return lexicon.stance_of(clause), False
    if is_rate_object_clause(clause):
        return 0, True
    return lexicon.stance_of(clause), False


def _themes_in(clause: str, themes: list[Any]) -> list[Any]:
    return [t for t in themes if lexicon.match_theme(clause, t) > 0]


def _object_word(clause: str, theme: Any) -> str:
    low = clause.lower()
    for term in theme.terms:
        if term.lower() in low:
            return term
    return theme.label


def clause_claims(doc: dict[str, Any], themes: list[Any]) -> list[dict[str, Any]]:
    """Object-anchored claims without a model.

    Attribution rule, in order: a clause naming a theme's terms belongs to
    that theme (and only the themes it names); a clause naming none belongs
    to the themes the previous clause did — the discourse object carries
    forward, so 「黄金看多，因为实际利率下行」 keeps 「因为…」 on gold; before
    any clause has named a theme, the title's themes are the object, which for
    a single-theme document reproduces the whole-document coding exactly.
    """
    themes = list(themes)
    if not themes:
        return []
    title_themes = _themes_in(doc.get("title") or "", themes) or themes
    current = list(title_themes)
    out: list[dict[str, Any]] = []
    # `summary` is often the head of `body` copied back (wisburg.py backfills
    # it from the first ~500 characters), so the same clause arrives twice.
    # One clause, one claim — otherwise a document's opening sentence votes
    # twice for the accident of having been summarised by truncation.
    seen: set[str] = set()
    # 「若美元走弱，黄金或将上行」: the condition is its own clause and the
    # stance is the next one. A conditional clause that takes no side is
    # held and attached to the clause that follows it, so the claim carries
    # its condition instead of the condition being dropped as filler.
    pending: tuple[str, str] | None = None
    for clause in doc_clauses(doc):
        norm = re.sub(r"\s+", "", clause).lower()
        if norm in seen:
            continue
        seen.add(norm)
        named = _themes_in(clause, themes)
        owners = named or current
        if named:
            current = named
        direction, unresolved = clause_direction(clause)
        low = clause.lower()
        cond = next((m for m in CONDITION_MARKERS if m.lower() in low), None)
        hedge = next((m for m in HEDGE_MARKERS if m.lower() in low), None)
        hz = _HORIZON.search(clause)
        cond_text, cond_marker = (clause, cond) if cond else (pending or (None, None))
        pending = (clause, cond) if (cond and direction == 0 and not unresolved) else None
        # A clause with no sign and no declined sign is filler for G's
        # purposes; it is still emitted (as neutral) so the counts say how
        # much of the document said nothing, but only when it names an object
        # — inherited neutral clauses would just be the document's length.
        if direction == 0 and not unresolved and not named:
            continue
        for t in owners:
            out.append({
                "doc_id": doc.get("doc_id"), "theme_id": t.id,
                "object": _object_word(clause, t) if t in named else t.label,
                "direction": direction, "unresolved": unresolved,
                "condition": cond_text,
                "condition_marker": cond_marker,
                "hedge": hedge,
                "horizon": hz.group(0) if hz else None,
                "quote": clause[:200], "quote_verified": True,
                "source": "clause",
                # Explicit ≥ inferred ≥ conditional ≥ hedged; the numbers are
                # ranks, not probabilities, and are recorded so a later weighting
                # experiment has something to weight by.
                "confidence": (0.4 if cond_text else 0.5 if hedge else 0.7
                               if direction else 0.6),
                "institution": doc.get("institution") or None,
            })
    return out


# ---------------------------------------------------------------------------
# Model path

def build_prompt(doc: dict[str, Any], themes: list[Any]) -> str:
    """One request, every object: the whole document against every theme it
    matched, so a report is read once per period rather than once per theme."""
    lines = ["你是宏观研究助理。下面是一篇研报的文本，以及它可能涉及的几个主题。",
             "请逐个「对象」抽取作者的观点，而不是给整篇文章一个正负号：",
             "同一篇里「黄金看多、白银看空」要输出两条，分别归到各自主题。",
             "",
             "每条观点回答的是该主题的「关键问题」；方向指作者对该问题「是」的一侧的立场：",
             "  +1 = 支持/看多；-1 = 反对/看空；0 = 明确中性；弃判 = 文中方向对该主题无法判定。",
             "利率、收益率、央行等对象的升降对主题是好是坏，取决于主题关键问题；判不出就写「弃判」，不要套固定规则。",
             "有条件（若/如果/取决于）或期限（未来N个月/年内）的，写进 condition / horizon。",
             "quote 必须是原文中的一段，逐字引用，不要改写。",
             "",
             "主题："]
    for t in themes:
        lines.append(f"  - theme_id={t.id}｜{t.label}｜关键问题：{t.key_question}"
                     f"｜词项：{'、'.join(t.terms[:8])}")
    lines += ["", "研报：",
              f"  标题：{doc.get('title') or ''}",
              f"  机构：{doc.get('institution') or '未知'}",
              f"  文本：{doc_text(doc)[:3500]}",
              "",
              "只输出 JSON，不要散文，形如：",
              '{"claims":[{"theme_id":"...","object":"原文中的对象词","direction":"+1|-1|0|弃判",'
              '"condition":null,"horizon":null,"quote":"原文片段","confidence":0.0}]}',
              "文中对某个主题没有任何观点时，就不要为它输出条目。"]
    return "\n".join(lines)


def _parse_json(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s, flags=re.S)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            raise
        return json.loads(s[i:j + 1])


def _norm_direction(v: Any) -> tuple[int, bool]:
    if isinstance(v, bool):
        raise ValueError("direction is a bool")
    if isinstance(v, (int, float)):
        d = int(v)
        if d not in (-1, 0, 1):
            raise ValueError(f"direction {v!r} out of range")
        return d, False
    key = str(v).strip().lower()
    if key not in _DIRECTION_WORDS:
        raise ValueError(f"direction {v!r} not understood")
    d = _DIRECTION_WORDS[key]
    return (0, True) if d is None else (d, False)


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def parse_model_claims(text: str, doc: dict[str, Any],
                       themes: list[Any]) -> list[dict[str, Any]]:
    """Model JSON → claim rows, or raise. Raising is the fallback signal.

    Unknown theme ids are dropped rather than guessed; a quote that is not in
    the document is kept but marked unverified, because a claim without a
    locatable source is exactly what a reviewer must be able to spot.
    """
    payload = _parse_json(text)
    rows = payload.get("claims") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("no claims list")
    known = {t.id: t for t in themes}
    body = _squash(doc_text(doc))
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("theme_id") or "")
        if tid not in known:
            continue
        direction, unresolved = _norm_direction(r.get("direction"))
        quote = str(r.get("quote") or "")[:300]
        try:
            conf = float(r.get("confidence")) if r.get("confidence") is not None else None
        except (TypeError, ValueError):
            conf = None
        hz = r.get("horizon")
        out.append({
            "doc_id": doc.get("doc_id"), "theme_id": tid,
            "object": str(r.get("object") or known[tid].label)[:80],
            "direction": direction, "unresolved": unresolved,
            "condition": (str(r["condition"])[:200] if r.get("condition") else None),
            "condition_marker": None, "hedge": None,
            "horizon": (str(hz)[:60] if hz else None),
            "quote": quote,
            "quote_verified": bool(quote) and _squash(quote) in body,
            "source": "model",
            "confidence": conf,
            "institution": doc.get("institution") or None,
        })
    return out


class ClaimCache:
    """Model extractions keyed by content, extractor version and theme set.

    Backed by the `claim_cache` table through the state port, so the cache
    lives with the verdicts it fed rather than in a sandbox that is thrown
    away. Only successful model output is stored: caching a fallback would
    turn one bad response into a permanent one.
    """

    def __init__(self, state: Any):
        self.state = state
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            rows = self.state.q(
                "SELECT claims, usage, model, source FROM claim_cache "
                "WHERE cache_key=?", (key,))
        except Exception:  # noqa: BLE001 — a missing table is a miss, not a crash
            rows = []
        if not rows:
            self.misses += 1
            return None
        self.hits += 1
        r = rows[0]
        return {"claims": json.loads(r["claims"] or "[]"),
                "usage": json.loads(r["usage"] or "{}"),
                "model": r.get("model"), "source": r.get("source")}

    def put(self, key: str, *, doc_id: str | None, themes: Iterable[Any],
            claims: list[dict[str, Any]], usage: dict[str, Any],
            model: str | None) -> None:
        from . import schema
        schema.upsert(self.state, "claim_cache", {
            "cache_key": key, "doc_id": doc_id,
            "extractor": EXTRACTOR_VERSION,
            "theme_fp": theme_fingerprint(themes),
            "source": "model", "model": model,
            "claims": json.dumps(claims, ensure_ascii=False),
            "usage": json.dumps(usage or {}, ensure_ascii=False),
            "created_at": datetime.now(timezone.utc).isoformat()})


def extract_claims_detailed(doc: dict[str, Any], themes: Iterable[Any], *,
                            infer: Any = None, cache: Any = None
                            ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Claims for one document plus a receipt saying how they were produced.

    Receipt keys: `source` ("model" | "model:cache" | "clause"), `calls`
    (model requests actually made, 0 or 1), `usage` (token counts as the
    port reported them), `cache_hit`, and `error` when the model path was
    attempted and abandoned. The receipt is what lets a period say how many
    documents it paid for and how many it declined to sign.
    """
    themes = list(themes)
    receipt: dict[str, Any] = {"source": "clause", "calls": 0, "usage": {},
                               "cache_hit": False, "error": None}
    if infer is None or not themes:
        return clause_claims(doc, themes), receipt
    text = doc_text(doc)
    key = cache_key(text, themes)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            receipt.update(source="model:cache", cache_hit=True,
                           usage=hit.get("usage") or {})
            return hit["claims"], receipt
    try:
        comp = infer.complete(build_prompt(doc, themes), temperature=0.0)
        usage = dict(getattr(comp, "usage", None) or {})
        rows = parse_model_claims(getattr(comp, "text", ""), doc, themes)
    except Exception as e:  # noqa: BLE001 — the fallback is the contract
        receipt.update(calls=1, error=f"{type(e).__name__}: {str(e)[:120]}")
        rows = clause_claims(doc, themes)
        for r in rows:
            r["source"] = "clause:fallback"
        return rows, receipt
    receipt.update(source="model", calls=1, usage=usage)
    if cache is not None:
        try:
            cache.put(key, doc_id=doc.get("doc_id"), themes=themes, claims=rows,
                      usage=usage, model=getattr(comp, "model", None))
        except Exception as e:  # noqa: BLE001 — a cache that cannot write must not cost the run
            receipt["cache_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return rows, receipt


def extract_claims(doc: dict[str, Any], themes: Iterable[Any], *,
                   infer: Any = None, cache: Any = None) -> list[dict[str, Any]]:
    """`extract_claims_detailed` without the receipt."""
    return extract_claims_detailed(doc, themes, infer=infer, cache=cache)[0]


# ---------------------------------------------------------------------------
# Aggregation

def _institution_key(c: dict[str, Any], titles: dict[Any, str]) -> str:
    """框架 §12: one institution, one view, one vote. Where no institution
    was recovered, the title signature stands in — collapsing to the source
    line would rebuild the 8×3 cell degeneracy v0.4 exists to remove."""
    inst = c.get("institution")
    if inst:
        return f"inst:{inst}"
    return "sig:" + lexicon.title_signature(titles.get(c.get("doc_id")) or
                                           str(c.get("doc_id")))


def disagreement(claims: list[dict[str, Any]], titles: dict[Any, str] | None = None,
                 max_samples: int = 8) -> dict[str, Any]:
    """Counts G is computed from, plus the rows a reader needs to check them.

    `n_pos` / `n_neg` are institutions (or anonymous documents) holding that
    view — the same institution saying the same thing in three notes counts
    once. `n_neutral` and `n_unresolved` are claims, not institutions: they
    are reported so the reader can see how much of the evidence declined to
    take a side, and they do not enter G's denominator.
    """
    titles = titles or {}
    pos: set[str] = set()
    neg: set[str] = set()
    n_neutral = n_unresolved = 0
    sources: dict[str, int] = {}
    for c in claims:
        sources[c.get("source") or "?"] = sources.get(c.get("source") or "?", 0) + 1
        if c.get("unresolved"):
            n_unresolved += 1
            continue
        d = int(c.get("direction") or 0)
        if d > 0:
            pos.add(_institution_key(c, titles))
        elif d < 0:
            neg.add(_institution_key(c, titles))
        else:
            n_neutral += 1
    # Signed claims first, most confident first, so the samples a reader sees
    # are the ones that moved the number.
    ranked = sorted(claims, key=lambda c: (c.get("unresolved") or False,
                                           c.get("direction") == 0,
                                           -(c.get("confidence") or 0)))
    samples = [{"doc_id": c.get("doc_id"), "direction": c.get("direction"),
                "unresolved": bool(c.get("unresolved")), "object": c.get("object"),
                "quote": c.get("quote"), "source": c.get("source"),
                "institution": c.get("institution"), "horizon": c.get("horizon"),
                "condition": c.get("condition")}
               for c in ranked[:max_samples]]
    return {"n_pos": len(pos), "n_neg": len(neg), "n_neutral": n_neutral,
            "n_unresolved": n_unresolved, "n_claims": len(claims),
            "source_mix": sources, "samples": samples}


def g_score(n_pos: int, n_neg: int) -> float:
    """G = 100 × [1 − |pos − neg| / (pos + neg)]; 0 when nobody took a side."""
    return 0.0 if not (n_pos + n_neg) else 100.0 * (1 - abs(n_pos - n_neg) / (n_pos + n_neg))
