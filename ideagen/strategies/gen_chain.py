"""传导链：主题 → 宏观中间变量 → 价格通道 → 可买工具，每一环都要点名。

Semantic momentum has one characteristic failure, and it is not being wrong. It is
producing an idea that reads well and has no mechanism joining the news to the
price: the theme is real, the instrument is plausibly related, and nothing in
between was ever specified. A month later that idea cannot be judged. If it made
money nobody knows which link paid, and if it lost money there is no link to
correct, so the monthly loop that is supposed to improve the method learns nothing
from it. Enough of those and the system is running without feedback while
appearing to have plenty.

Naming the intermediate variable is what fixes it, and it has to be a variable
someone publishes — a spread, a yield, an inventory level, a shipping rate, a
policy rate — because the point is that the link can be *read* between now and the
horizon, not that it exists in principle. The falsifier is the same discipline
applied forward: one reading, obtainable within the month, that would mean the
chain is not working. An idea that cannot name it is an idea with no way to be
wrong, and this generator would rather return fewer ideas than that.

The links are kept as separate fields instead of being folded into the thesis so a
later review can ask which link broke, rather than only whether the trade worked.
"""

from __future__ import annotations

from typing import Any

from . import _gen
from ..strategy import RunContext, Verdict, register

SHAPE = ('[{"instrument_id":"清单里的 id",'
         '"chain":"主题 → 中间变量 → 价格通道 → 这个工具（四环都写出来）",'
         '"watch_variable":"中间变量的名字 + 在哪里能读到（数据源/发布方）",'
         '"falsifier":"一个月内能读到的哪个数值，会说明这条链没在走",'
         '"thesis":"为什么这条链在一个月内推动这个标的上行",'
         '"upside_pct":8.0,"downside_pct":-5.0,'
         '"p_up":0.40,"p_base":0.40,"p_down":0.20}]')

CONTRACT = (
    "每一条想法必须是一条完整的传导链，四个环节都要点名：\n"
    "  1. 主题——材料里的哪一件事；\n"
    "  2. 宏观中间变量——它必须是有人定期公布、能查到数的东西（利差、收益率、库存、"
    "运价、产能利用率、政策利率、发行量……），不能是「风险偏好」「市场情绪」这类读不到的说法；\n"
    "  3. 价格通道——这个变量通过什么机制传到资产价格（估值折现、成本转嫁、资金流、"
    "汇率、供需缺口……）；\n"
    "  4. 可买工具——清单里的哪一个标的承接了这个通道。\n"
    "另外给一个 falsifier：一个月内可以读到的一个数值，如果读到它，就说明这条链没在走。\n"
    "四环里有任何一环你只能写得含糊，或者 falsifier 写不出具体读数，这一条就丢掉不要交。"
    "少交几条比交一条无法验证的链要好——无法验证的链一个月后既不能算对也不能算错。"
)


def build_prompt(ctx: RunContext,
                 topic: dict[str, Any]) -> tuple[str, int]:
    _docs, n_docs = _gen.corpus_block(ctx, topic)
    return "\n\n".join([
        f"今天是 {ctx.as_of.isoformat()}。你的任务是把主题拆成可观测的传导链。",
        _gen.topic_block(topic),
        "相关原始材料（新到旧）：\n" + _docs,
        "可买清单（instrument_id | 名称 | 暴露 | 载体）：\n" + _gen.universe_block(ctx),
        CONTRACT,
        f"就这个主题给出最多 {_gen.PER_TOPIC} 条持有期一个月的做多想法。标的原样取自上面的"
        "清单；每条要有一个月内的上行与下行幅度（百分数）和上行/持平/下行三档概率"
        "（相加为 1）；同一主题内不要重复标的。",
        "只输出 JSON 数组，形如：\n" + SHAPE,
    ]), n_docs


@register("idea_generator", "chain", "1.0", role="exploratory", label="传导链")
def chain(ctx: RunContext) -> Verdict:
    """Force a named chain: topic, observable variable, price channel, instrument, falsifier."""
    # The three chain fields ride along on the idea rather than being collapsed
    # into the thesis, so a month-later review can attribute a loss to the link
    # that failed instead of only to the trade.
    return _gen.generate_per_topic(
        ctx, "chain", build_prompt,
        require_keys=("chain", "watch_variable", "falsifier"),
        extra_keys=("chain", "watch_variable", "falsifier"))
