"""Hand the whole candidate set to the model and let it build the book.

The other selectors encode a hypothesis about what makes ten ideas a portfolio —
a ratio, a cap, a penalty. Each is a guess, and each is only testable against
something that made no such commitment. This arm is that something: it sees the
same ~100 candidates with theses and odds, and returns ten with a reason each.
If a hand-built rule cannot beat it, the rule is not carrying information and the
complexity it costs is not being paid for.

Two things are treated as failures rather than smoothed over:

**No inference port is an error, not an empty book.** A selector that quietly
returns nothing when the model key is missing looks identical to a week in which
nothing was worth holding — the opposite conclusion. So it raises, and the
registry records that in `meta["error"]` while the other arms still run.

**A short or invented pick list is clamped and reported.** The model can return
nine ids, twelve, or one that does not exist. Nine held means a quarter of a
tranche sits in JPST because a parser shrugged, and nobody would see it in the
weekly numbers. So unknown ids are dropped, overflow is truncated, a shortfall is
back-filled in stage-B order, and every one of those repairs is named in `meta` —
a clamp that is not visible is indistinguishable from the model being right.
"""

from __future__ import annotations

from ..strategy import RunContext, Verdict, register
from . import _gen

SYSTEM = ("你是宏观投资组合经理。只输出 JSON，不要解释。"
          "持有期固定一个月，只做多，标的只能来自给定候选清单的 id。")

PROMPT = """下面是本周全部候选想法（来自 5 个主题、4 种生成方法）。
请为一个月期的组合挑出正好 {n} 条，构成实际持仓。

要求：
- 只能使用清单里出现过的 id，原样抄写，不要改写、不要新增。
- 恰好 {n} 条，不多不少。
- 每条给一句中文理由，说明为什么它进组合（而不是复述 thesis）。

只输出 JSON：{{"picks":[{{"id":"...","reason":"..."}}]}}

候选（id | 标的 | 敞口 | 主题 | 方法 | 上行%/下行% | p_up/p_base/p_down | 逻辑）：
{rows}"""


def _rows(ctx: RunContext, thesis_chars: int) -> str:
    out = []
    for c in ctx.candidates:
        out.append(
            f"{c.get('id')} | {c.get('instrument_id')} {c.get('instrument_name') or ''}"
            f" | {c.get('exposure') or '未映射'} | {c.get('topic_id')}"
            f" | {c.get('method')} | {c.get('upside_pct')}/{c.get('downside_pct')}"
            f" | {c.get('p_up')}/{c.get('p_base')}/{c.get('p_down')}"
            f" | {str(c.get('thesis') or '')[:thesis_chars]}")
    return "\n".join(out)


@register("idea_selector", "ai_native", "1.0", label="AI 端到端选取",
          needs_model=True,
          role="primary", params={"n": 10, "thesis_chars": 220,
                                  "temperature": 0.2})
def ai_native(ctx: RunContext) -> Verdict:
    """Give the model every candidate and ask for exactly n, one reason each."""
    if ctx.infer is None:
        raise RuntimeError("ai_native 需要模型推理，但本次运行没有可用的 inference 端口")
    n = int(ctx.params.get("n", 10))
    if not ctx.candidates:
        return Verdict(strategy="ai_native", version="1.0",
                       meta={"n": 0, "note": "没有候选想法可挑"})

    c = ctx.infer.complete(
        PROMPT.format(n=n, rows=_rows(ctx, int(ctx.params.get("thesis_chars", 220)))),
        system=SYSTEM, temperature=float(ctx.params.get("temperature", 0.2)),
        max_tokens=4000)
    raw = _gen.parse_json(c.text)
    if isinstance(raw, dict):
        raw = raw.get("picks") or raw.get("ideas") or raw.get("data") or []
    if not isinstance(raw, list):
        raise ValueError(f"模型返回的不是选股列表：{str(raw)[:160]}")

    valid = {str(x["id"]) for x in ctx.candidates}
    reasons: dict[str, str] = {}
    picks: list[str] = []
    unknown: list[str] = []
    for item in raw:
        pid = str((item.get("id") if isinstance(item, dict) else item) or "").strip()
        if pid not in valid:
            # Never repaired by fuzzy matching: a hallucinated ticker coerced onto
            # the nearest real one produces a position nobody reasoned about.
            unknown.append(pid or "<空>")
            continue
        if pid in picks:
            continue
        picks.append(pid)
        if isinstance(item, dict):
            reasons[pid] = str(item.get("reason") or item.get("why") or "").strip()[:200]

    over = picks[n:]
    picks = picks[:n]
    # Back-filled in stage-B order rather than by any score: this arm must not
    # quietly acquire a ranking rule of its own, or the comparison it exists to
    # anchor stops being clean. The substitution is recorded instead.
    fill = [i for i in (str(x["id"]) for x in ctx.candidates) if i not in set(picks)]
    filled = fill[:max(0, n - len(picks))]
    picks += filled

    rejected = {i: "模型未选入" for i in valid - set(picks)}
    for i in filled:
        rejected.pop(i, None)
        reasons[i] = "模型少给了名额，按 筛选B 原始顺序补位（非模型判断）"
    return Verdict(
        strategy="ai_native", version="1.0", chosen=picks,
        scores={i: {"reason": reasons.get(i, ""), "pick_rank": r + 1}
                for r, i in enumerate(picks)},
        rejected=rejected, calls=1,
        meta={"n": len(picks), "target_n": n, "n_candidates": len(ctx.candidates),
              "clamped": {"unknown_ids": unknown, "truncated": over,
                          "backfilled": filled, "raw_returned": len(raw)},
              "model": getattr(c, "model", None)},
    )
