"""The unscaffolded arm: model judgement with no imposed reasoning skeleton.

Every other generator in 筛选B adds a human-designed decomposition — anomaly
first, transmission chain, consensus gap — and each of those is a bet that the
structure buys more than it costs. It is not obviously free. A mandated form pulls
a model toward filling slots, and an idea whose logic does not fit the template
gets bent into it or dropped; both show up later as theses that read like
paperwork.

The failure this addresses is an unfalsifiable methodology. The thesis of the
whole system is that the edge is the model's semantic reading of the corpus, so
the version with no scaffolding at all is not the weak arm by assumption — it is
the number the other three have to beat, on the same topics, the same evidence and
the same shelf. Without it, "our framework adds value" is untested, and any month
where the structured arms win could just as easily be the structure and the model
agreeing by luck.

Being the control also fixes what may vary here: the output shape is dictated as
tightly as everywhere else, because response format is plumbing, not treatment.
The only thing withheld is instruction on how to think.
"""

from __future__ import annotations

from typing import Any

from . import _gen
from ..strategy import RunContext, Verdict, register

#: Identical to the shape the other three ask for, minus their method fields. A
#: laxer example here would show up as a higher parse rate and be mistaken for the
#: absence of scaffolding paying off.
SHAPE = ('[{"instrument_id":"清单里的 id","thesis":"为什么这一个月会涨",'
         '"upside_pct":8.0,"downside_pct":-5.0,'
         '"p_up":0.40,"p_base":0.40,"p_down":0.20}]')


def build_prompt(ctx: RunContext,
                 topic: dict[str, Any]) -> tuple[str, int]:
    _docs, n_docs = _gen.corpus_block(ctx, topic)
    return "\n\n".join([
        f"今天是 {ctx.as_of.isoformat()}。下面给你一个本周入选的主题、它的入选依据、"
        f"相关原始材料摘录，以及当前可买清单。",
        _gen.topic_block(topic),
        "相关原始材料（新到旧）：\n" + _docs,
        "可买清单（instrument_id | 名称 | 暴露 | 载体）：\n" + _gen.universe_block(ctx),
        f"请就这个主题给出 {_gen.PER_TOPIC} 条持有期一个月的做多想法。"
        "怎么想、先看什么后看什么、用不用框架，全部由你决定——本方法不规定任何推理步骤，"
        "这里要测的就是你自己的判断。",
        "硬约束只有三条：标的必须原样取自上面的清单；每条要写清理由、一个月内的上行与下行"
        "幅度（百分数）、以及上行/持平/下行三档概率（相加为 1）；同一主题内不要重复标的。",
        _gen.CITATION_RULE,
        "只输出 JSON 数组，形如：\n" + SHAPE,
    ]), n_docs


@register("idea_generator", "ai_native", "1.0", role="primary", label="AI 端到端")
def ai_native(ctx: RunContext) -> Verdict:
    """Control arm: topic, evidence, corpus and shelf, with no reasoning steps imposed."""
    return _gen.generate_per_topic(ctx, "ai_native", build_prompt)
