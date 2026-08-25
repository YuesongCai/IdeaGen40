"""Shared machinery for the four idea generators (筛选B).

The four methods differ in one thing only: the reasoning skeleton they impose on
the model. Everything else — how the universe is described, how the response is
parsed, how odds are sanity-checked, how ideas are named — lives here and is
identical across all four.

That is deliberate and it is the whole basis of the comparison. If each generator
carried its own parsing and its own instrument handling, a difference in realised
return could just as easily come from one of them having a laxer parser or a
better-formatted universe list. Holding the plumbing fixed means the only thing
varying between arms is the thing being tested.

The generators are also the one place a model writes objects rather than ranking
them, so the output is treated as untrusted input: instruments are resolved
against the eligible universe, probabilities are renormalised rather than
believed, and anything unresolvable is dropped with a recorded reason instead of
being coerced into something tradeable.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import universe as uni
from ..strategy import RunContext, Verdict

#: Ideas per topic. The mandate asks for 20; five topics therefore put ~100
#: candidates in front of 筛选C, which holds 10.
PER_TOPIC = 20

#: One month, fixed at generation. The horizon is not a model choice — the whole
#: portfolio is built on a one-month momentum realisation window, and a strategy
#: that could pick its own horizon would be optimising the measurement instead of
#: the idea.
HORIZON_DAYS = 30

SYSTEM = (
    "你是宏观交易台的想法生成器。只输出 JSON，不要解释。"
    "所有想法的持有期固定为一个月，方向只做多（做空通过反向 ETF 或防御性标的表达）。"
    "标的只能从给定清单里选，用清单里的 instrument_id 原样填写。"
)


#: Appended to every generator's prompt. Citation is part of the shared
#: contract — all four arms carry the identical requirement, so it cannot
#: confound the comparison — because an idea that cannot name the documents it
#: rests on is an idea whose evidence trail starts at nothing: a month later
#: there is no way to ask whether the thesis misread its sources or the market
#: disagreed with them, and those two failures teach opposite lessons.
CITATION_RULE = ("每条想法必须带 citations 字段：从上面材料里挑最支撑这条论点的 "
                 "1-3 条，原样填它们的 doc_id（形如 feed:100994）。"
                 "没有任何材料支撑的想法不要写。")


def universe_block(ctx: RunContext, limit: int = 120) -> str:
    """The buyable list, as compactly as it can be stated without losing meaning.

    `exposure` is included because it is what makes an instrument choosable for a
    reason: without it the model is matching on ticker strings, and a thesis about
    Japanese rates can end up expressed through whatever happens to have "Japan"
    in its name.
    """
    lines = []
    for u in ctx.universe[:limit]:
        lines.append(f"{u.get('instrument_id')} | {u.get('name')} | "
                     f"{u.get('exposure') or '未映射'} | {u.get('vehicle') or ''}")
    return "\n".join(lines)


def topic_block(t: dict[str, Any]) -> str:
    ev = t.get("evidence") or t.get("why") or ""
    if isinstance(ev, list):
        ev = "；".join(str(x) for x in ev[:6])
    parts = [f"主题 {t.get('topic_id')}：{t.get('label') or t.get('topic_id')}"]
    if t.get("key_question"):
        parts.append(f"核心问题：{t['key_question']}")
    if t.get("exposures"):
        parts.append(f"相关敞口：{'、'.join(str(x) for x in t['exposures'][:8])}")
    parts.append(f"打分A 依据：{str(ev)[:1200]}")
    return "\n".join(parts)


def topic_terms(topic: dict[str, Any]) -> list[str]:
    """The vocabulary that identifies this topic in the corpus.

    Drawn from the theme's registered terms first. Falling back to the slug is
    nearly useless on a Chinese corpus — `POLICY-PATH` matches no document — so a
    topic arriving without terms is treated as unmatched rather than quietly
    matching everything, which is what `corpus_block` then reports.
    """
    terms = [str(x).strip() for x in (topic.get("terms") or []) if str(x).strip()]
    if terms:
        return terms
    slug = str(topic.get("topic_id") or "")
    label = str(topic.get("label") or "")
    return [w for w in re.split(r"[^\w一-鿿]+", f"{slug} {label}")
            if len(w) > 1]


def corpus_block(ctx: RunContext, topic: dict[str, Any],
                 k: int = 24) -> tuple[str, int]:
    """The documents behind one topic, most recent first, plus how many matched.

    Passed as evidence rather than summarised by the orchestrator so a generator
    that wants to reason from the primary text can, and so the same context
    replays identically — a summary written at run time could not be reproduced.

    The match count is returned rather than discarded because falling back to the
    undifferentiated corpus is a silent failure with a specific consequence: every
    topic's prompt becomes the same prompt, and five topics then yield one topic's
    ideas five times over. The caller records the count so that shows up as a
    number instead of as five suspiciously similar idea lists.
    """
    terms = [t.lower() for t in topic_terms(topic)]
    hits = []
    for d in ctx.corpus:
        blob = f"{d.get('title','')} {d.get('summary','')} {d.get('body','')}".lower()
        if any(w in blob for w in terms):
            hits.append(d)
    hits.sort(key=lambda d: str(d.get("published_d") or ""), reverse=True)
    use = hits[:k] if hits else ctx.corpus[:k]
    out = []
    for d in use:
        # The doc_id leads each line because the citation contract asks the
        # model to quote it back. A rule demanding ids the material never shows
        # would make every citation a hallucination by construction.
        out.append(f"{d.get('doc_id')} [{d.get('published_d')}] "
                   f"{d.get('institution') or ''} "
                   f"{d.get('title','')} — {str(d.get('summary') or '')[:220]}")
    return "\n".join(out), len(hits)


def ask_json(ctx: RunContext, prompt: str) -> tuple[Any, int]:
    """One model call returning parsed JSON, plus the call count.

    Raises when inference is unavailable rather than returning nothing: a
    generator that quietly produces zero ideas because there was no model key
    would look like a topic with no expressible trades, which is a completely
    different finding.
    """
    if ctx.infer is None:
        raise RuntimeError("此生成方法需要模型推理，但当前运行没有可用的 inference 端口")
    c = ctx.infer.complete(prompt, system=SYSTEM, temperature=0.2,
                           max_tokens=8000)
    return parse_json(c.text), 1


def parse_json(text: str) -> Any:
    """Extract the JSON payload from a model response.

    Models wrap JSON in prose or fences often enough that a strict `json.loads`
    would discard usable answers, and a discarded answer is indistinguishable from
    a topic the model found nothing in.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i, jx = t.find(opener), t.rfind(closer)
        if 0 <= i < jx:
            try:
                return json.loads(t[i:jx + 1])
            except Exception:  # noqa: BLE001
                continue
    raise ValueError(f"模型返回无法解析为 JSON：{t[:200]}")


def _num(x: Any, default: float | None = None) -> float | None:
    try:
        return float(str(x).strip().rstrip("%"))
    except Exception:  # noqa: BLE001
        return default


def _first(r: dict[str, Any], *keys: str) -> Any:
    """First key actually present. Not `a or b` — that discards a legitimate 0."""
    for k in keys:
        if r.get(k) is not None:
            return r[k]
    return None


def mint(raw: list[dict[str, Any]], ctx: RunContext, topic: dict[str, Any],
         method: str, *, extra_keys: tuple[str, ...] = (),
         require_keys: tuple[str, ...] = ()) -> tuple[list[dict], dict[str, str]]:
    """Turn raw model output into ideas that satisfy the stage-B contract.

    Returns (ideas, {label: reason dropped}). Dropping with a reason rather than
    repairing silently matters here: a generator whose instrument choices are
    routinely unresolvable is a generator that does not understand the shelf, and
    that is worth seeing in its own right rather than hiding behind a fallback.

    `require_keys` is how a method enforces its own reasoning contract. For the
    chain and gap arms the imposed structure *is* the treatment being tested, so an
    idea returned without its named chain or its stated consensus has not actually
    received the treatment — counting it as one would dilute the very difference
    the comparison is meant to detect. Enforcing it here, through the same shared
    code path as every other check, keeps the plumbing identical across arms.

    Drop labels are keyed by position as well as token, because two rejected rows
    naming the same instrument are two rejections, and `rejected` is the evidence
    for whether a generator understands the shelf.
    """
    buyable = {str(u.get("instrument_id")): u for u in ctx.universe}
    ideas: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    seen: set[str] = set()

    for n, r in enumerate(raw if isinstance(raw, list) else [], 1):
        if not isinstance(r, dict):
            dropped[f"#{n}"] = "不是对象"
            continue
        tok = str(_first(r, "instrument_id", "instrument", "ticker") or "").strip()
        tag = f"#{n} {tok}" if tok else f"#{n}"
        inst = buyable.get(tok) or buyable.get(tok.upper())
        if not inst:
            hit = uni.resolve(tok)
            inst = buyable.get(hit.key) if hit else None
        if not inst:
            dropped[tag] = "标的不在可用清单内（或超出授权载体）"
            continue

        lacking = [k for k in require_keys if not str(r.get(k) or "").strip()]
        if lacking:
            dropped[tag] = f"缺少本方法必需的推理字段：{'、'.join(lacking)}"
            continue

        up = _num(_first(r, "upside_pct", "upside"))
        dn = _num(_first(r, "downside_pct", "downside"))
        pu = _num(r.get("p_up"), None)
        pb = _num(r.get("p_base"), None)
        pd = _num(r.get("p_down"), None)
        if up is None or dn is None or None in (pu, pb, pd):
            dropped[tag] = "缺少涨跌幅或三档概率"
            continue
        up, dn = abs(up), -abs(dn)
        if up <= 0 or dn >= 0:
            dropped[tag] = "赔率不成立（上行须为正、下行须为负）"
            continue

        # Probabilities are renormalised, not trusted. A model that returns
        # 0.4/0.4/0.4 has expressed a usable ranking of scenarios with an
        # arithmetic slip; rejecting it would throw away the judgement along with
        # the slip. The original sum is kept so a generator that is habitually
        # sloppy is still visible.
        tot = pu + pb + pd
        if tot <= 0:
            dropped[tok] = "概率之和为零"
            continue
        idea_id = f"{method}:{topic.get('topic_id')}:{inst['instrument_id']}"
        if idea_id in seen:
            dropped[tag] = "同一主题下重复标的"
            continue
        seen.add(idea_id)

        # Citations are validated against the corpus actually shown, then kept.
        # An invalid doc_id is dropped and counted rather than repaired: a
        # generator that habitually cites documents it was never given is
        # telling us something about its reading, and that signal would vanish
        # under silent repair.
        known_docs = {str(d.get("doc_id")) for d in ctx.corpus}
        cits = [str(x) for x in (r.get("citations") or [])
                if str(x) in known_docs][:3]
        bad_cits = len([x for x in (r.get("citations") or [])
                        if str(x) not in known_docs])

        idea = {
            "id": idea_id,
            "citations": cits,
            "bad_citations": bad_cits,
            "instrument_id": str(inst["instrument_id"]),
            "instrument_name": inst.get("name"),
            "vehicle": inst.get("vehicle"),
            "exposure": inst.get("exposure"),
            "topic_id": str(topic.get("topic_id")),
            "method": method,
            "horizon_days": HORIZON_DAYS,
            "thesis": str(r.get("thesis") or r.get("why") or "").strip()[:600],
            "upside_pct": round(up, 3),
            "downside_pct": round(dn, 3),
            "p_up": round(pu / tot, 4),
            "p_base": round(pb / tot, 4),
            "p_down": round(pd / tot, 4),
            "p_sum_raw": round(tot, 4),
        }
        if not idea["thesis"]:
            dropped[tag] = "没有给出理由"
            seen.discard(idea_id)
            continue
        # Method-specific reasoning fields, preserved but bounded. A verbose model
        # would otherwise grow every persisted idea row without limit, and these
        # rows are written to object storage on every run.
        for key in extra_keys:
            if r.get(key) not in (None, ""):
                idea[key] = (str(r[key])[:600] if isinstance(r[key], str)
                             else r[key])
        ideas.append(idea)
    return ideas, dropped


def generate_per_topic(ctx: RunContext, method: str, build_prompt,
                       *, extra_keys: tuple[str, ...] = (),
                       require_keys: tuple[str, ...] = ()) -> Verdict:
    """Run one generator across every selected topic.

    Per-topic rather than one call for all five: a single call has to divide a
    fixed output budget across five themes, and in practice the first theme gets
    the reasoning and the last gets filler. One call per topic also means one
    topic's failure costs that topic rather than the run.
    """
    from ..strategy import spec

    # An empty topic list means 筛选A produced nothing, which is a broken run, not
    # a run with no expressible trades. Returning zero ideas without an error would
    # make those two indistinguishable — the same silent-empty failure the model
    # call above refuses to commit.
    if not ctx.topics:
        raise RuntimeError("筛选A 没有给出任何主题，筛选B 无从生成（不是「没有可做的交易」）")
    if not ctx.universe:
        raise RuntimeError("可用标的清单为空，任何想法都无法表达")

    ideas: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    calls = 0
    per_topic: dict[str, int] = {}
    errors: dict[str, str] = {}
    over: dict[str, int] = {}
    unmatched: list[str] = []

    for t in ctx.topics:
        tid = str(t.get("topic_id"))
        try:
            prompt, n_docs = build_prompt(ctx, t)
            raw, n = ask_json(ctx, prompt)
            calls += n
        except Exception as e:  # noqa: BLE001 — one topic must not lose the rest
            errors[tid] = f"{type(e).__name__}: {e}"
            per_topic[tid] = 0
            continue
        if not n_docs:
            unmatched.append(tid)
        if isinstance(raw, dict):
            raw = raw.get("ideas") or raw.get("data") or []
        got, bad = mint(raw, ctx, t, method, extra_keys=extra_keys,
                        require_keys=require_keys)
        # Over-production is recorded, not just truncated. Twenty kept out of
        # thirty-five offered is a different behaviour from twenty out of twenty,
        # and the truncation is ours — so the arm should not be credited or blamed
        # for ideas the harness threw away.
        if len(got) > PER_TOPIC:
            over[tid] = len(got) - PER_TOPIC
        ideas.extend(got[:PER_TOPIC])
        dropped.update({f"{tid}/{k}": v for k, v in bad.items()})
        per_topic[tid] = len(got[:PER_TOPIC])

    if errors and not ideas:
        raise RuntimeError("所有主题都失败：" + "; ".join(
            f"{k} {v}" for k, v in list(errors.items())[:3]))

    return Verdict(
        strategy=method, version=str(spec("idea_generator", method)["version"]),
        chosen=[i["id"] for i in ideas], produced=ideas,
        rejected=dropped, calls=calls,
        meta={"per_topic": per_topic, "target_per_topic": PER_TOPIC,
              "horizon_days": HORIZON_DAYS, "topic_errors": errors,
              "truncated": over, "universe_size": len(ctx.universe),
              # Topics whose own vocabulary matched no document: those prompts fell
              # back to the undifferentiated corpus, so their ideas are not really
              # "this topic's" ideas.
              "topics_without_corpus_match": unmatched},
    )
