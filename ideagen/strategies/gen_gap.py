"""共识缺口：anchor on what the price already implies, trade only the residual.

Semantic detection fires on attention. By the time a theme is loud enough across a
week of research to rank in 筛选A, the reason it is loud is that a lot of people
have read the same thing — and some of them traded it. So the strongest topic and
the most crowded topic are frequently the same topic, and the ideas that follow
most naturally from the evidence are the ones with the least left to earn. Nothing
in the other three arms pushes against this: anomaly-first, chain-first and
unstructured all still start from the narrative and reason forward to a trade.

This generator inverts the order. It starts from the price — what does current
pricing and positioning say the market already believes about this theme — and then
only admits ideas where the evidence contradicts that belief, with the specific
unexpressed part named. Two consequences worth the extra step: an idea has to
survive the question "who is on the other side of this", and the arm's edge comes
from a different source than the other three rather than a different reasoning
style, so a month where all four win together is distinguishable from a month
where only the crowded trades worked.

It is expected to return fewer ideas than the others on a hot topic. That is the
finding, not a defect — a topic where nothing is left unpriced is worth knowing
about before it is sized.
"""

from __future__ import annotations

from typing import Any

from . import _gen
from ..strategy import RunContext, Verdict, register

SHAPE = ('[{"instrument_id":"清单里的 id",'
         '"implied_consensus":"当前价格/仓位隐含市场已经相信的那一条",'
         '"contradiction":"材料里哪一条证据与它冲突（引到具体材料）",'
         '"unexpressed":"这个主题里还没被价格表达的那一部分",'
         '"thesis":"为什么这一部分会在一个月内被定价",'
         '"upside_pct":8.0,"downside_pct":-5.0,'
         '"p_up":0.40,"p_base":0.40,"p_down":0.20}]')

STEPS = (
    "第一步：先不要想交易。写出当前价格与仓位隐含了市场对这个主题已经相信什么——"
    "已经涨过/跌过的部分、被反复引用的一致预期、明显拥挤的仓位。\n"
    "第二步：只在材料与第一步的信念相冲突的地方出想法。每一条都要说清：冲突的证据是哪一条，"
    "以及这个主题里还有哪一部分没有被价格表达。\n"
    "判定标准很硬：如果一条想法只是把大家都已经读到的故事再讲一遍，无论它多有道理，都不要交。"
    "这一档方法的价值就在于「还没被表达的那部分」，找不到就少交几条，甚至一条都不交，"
    "这本身就是关于这个主题的有用结论。"
)


def price_block(ctx: RunContext, limit: int = 20) -> str:
    """What can be said about where the price already is, from stored inputs only.

    Deliberately not the model's memory of levels: the anchor has to be something
    the run recorded as of this date, or step one becomes a recollection and the
    whole comparison stops being replayable.
    """
    lines = [f"{e.get('label')} = {e.get('actual')}{e.get('unit') or ''}"
             f"（{e.get('date')}，{e.get('source') or ''}）"
             for e in ctx.calendar if (e.get("kind") or "") == "level"]
    for key, px in list(ctx.prices.items())[:limit]:
        if isinstance(px, dict):
            bits = ", ".join(f"{k}={v}" for k, v in list(px.items())[:6])
            lines.append(f"{key}: {bits}")
        else:
            lines.append(f"{key}: {px}")
    if not lines:
        # No stored anchor, so say so. A model that thinks it was handed levels and
        # was not will assert a consensus it invented, which is the one thing this
        # method must not do — its entire discipline rests on step one being real.
        return ("本次运行没有可用的价格或水平数据。第一步请只依据上面材料里被反复重复的"
                "一致预期来推断市场已经相信什么，并明确写出这是从材料推断、不是从价格读出。")
    return "当前可参考的水平（截至今天，来自本次运行的输入）：\n" + "\n".join(lines[:limit])


def build_prompt(ctx: RunContext,
                 topic: dict[str, Any]) -> tuple[str, int]:
    _docs, n_docs = _gen.corpus_block(ctx, topic)
    return "\n\n".join([
        f"今天是 {ctx.as_of.isoformat()}。这一档方法从价格出发，不从叙事出发。",
        _gen.topic_block(topic),
        "相关原始材料（新到旧）：\n" + _docs,
        price_block(ctx),
        "可买清单（instrument_id | 名称 | 暴露 | 载体）：\n" + _gen.universe_block(ctx),
        STEPS,
        f"就这个主题给出最多 {_gen.PER_TOPIC} 条持有期一个月的做多想法。标的原样取自上面的"
        "清单；每条要有一个月内的上行与下行幅度（百分数）和上行/持平/下行三档概率"
        "（相加为 1）；同一主题内不要重复标的。",
        _gen.CITATION_RULE,
        "只输出 JSON 数组，形如：\n" + SHAPE,
    ]), n_docs


@register("idea_generator", "gap", "1.0", role="exploratory", label="共识缺口")
def gap(ctx: RunContext) -> Verdict:
    """Anchor on implied consensus, then trade only the part the evidence contradicts."""
    # `implied_consensus` is carried per idea rather than once per topic: two ideas
    # on one theme often push against different beliefs, and collapsing them would
    # lose which belief each trade was actually short.
    return _gen.generate_per_topic(
        ctx, "gap", build_prompt,
        require_keys=("implied_consensus", "contradiction", "unexpressed"),
        extra_keys=("implied_consensus", "contradiction", "unexpressed"))
