"""约束边界：anomaly → motive → binding constraint → dated trigger.

The failure this addresses is the consensus restatement. A model reading a week of
sell-side material and asked what matters will reliably answer with what the
material talks about most, because that is what "important" looks like in text.
But volume of coverage is the one property of a theme most likely to be in the
price already, so the ideas that come back are well-argued versions of what
everyone else also read this week.

The four steps are chosen to walk away from that in a specific direction. Asking
for what is *rare* rather than important inverts the frequency bias. Asking who is
forced to act, and by what — mandate, funding, regulation, an election calendar —
gets past stated intentions to the part of behaviour that can be predicted, since
an actor under a binding constraint has few choices and a commentator has many.
Asking for the binding constraint and the level at which it breaks turns a
direction into a threshold. And a trigger tied to a scheduled date or a readable
level is what makes the idea judgeable a month later: "如果情绪转弱" cannot be
checked by anyone, so an idea resting on it can neither be blamed nor learned from.

The calendar is passed in for exactly that last step. A trigger that names a real
auction, release or current spread level is falsifiable against something the
system already stores; one the model invents is not.
"""

from __future__ import annotations

from typing import Any

from . import _gen
from .. import philosophy
from ..strategy import RunContext, Verdict, register

SHAPE = ('[{"instrument_id":"清单里的 id",'
         '"anomaly":"这份材料里罕见/偏离常态的地方",'
         '"motive":"谁在动手，被什么逼着动（授权/资金/监管/政治）",'
         '"constraint":"真正绑死的约束，以及它在什么水平上断掉",'
         '"trigger":"带日期或带水平的触发条件 + 触发后做什么",'
         '"thesis":"把上面四步串成一个月的做多逻辑",'
         '"upside_pct":8.0,"downside_pct":-5.0,'
         '"p_up":0.40,"p_base":0.40,"p_down":0.20}]')

STEPS = (
    "第一步 · 异常检测：材料里什么是罕见的、偏离常态的？不是问什么最重要——"
    "被反复讨论的事大概已经在价格里了，罕见的事才可能还没有。\n"
    "第二步 · 真实动机：谁在动手，什么东西逼着他动（考核与授权、资金来源、监管要求、"
    "政治与选举周期）？写他被什么约束着，不要写他公开说了什么。\n"
    "第三步 · 约束边界：这条链上真正绑死的约束是哪一条？它在什么水平上断掉？给出能读到的数字。\n"
    "第四步 · 触发条件：给一个带日期或带水平的触发条件，并写清触发后做什么。"
    "「如果情绪转弱」「若风险偏好回升」不算触发条件——一个月后没有人能判断它到底有没有发生。"
)


def calendar_block(ctx: RunContext, limit: int = 20) -> str:
    """Scheduled events and current reference levels, split three ways.

    A level fires on a threshold crossing and an event fires on a date — but a
    date that has already passed fires on nothing, and that distinction only
    started to matter on 2026-09-05, when `fmp_macro_releases` began returning a
    backward window alongside the forward one. Sorted ascending and cut at
    `limit`, the combined list put twelve already-published releases at the top
    of a section headed「可作为触发日期」, and a model asked for a dated trigger
    would reasonably have written one against last Friday's payrolls.

    The backward window is worth keeping, because a release that has *already*
    surprised is the most useful thing on this page for a Carl-style thesis —
    it is a live dislocation rather than a scheduled one. It just has to be
    labelled as what it is. So: upcoming events are offered as triggers, recent
    prints are offered as evidence, and only the former are called dates.
    """
    as_of = ctx.as_of.isoformat()
    upcoming: list[str] = []
    published: list[str] = []
    levels: list[tuple[int, str]] = []
    for e in ctx.calendar:
        if (e.get("kind") or "") == "level":
            # Feeds may rank their own rows; a congressional sector aggregate is
            # real but peripheral next to VIX or the curve, and without an order
            # the peripheral rows crowd the central ones out of `limit`.
            levels.append((int(e.get("priority") or 1),
                           f"{e.get('label')} = {e.get('actual')}"
                           f"{e.get('unit') or ''}（{e.get('date')}）"))
            continue
        d = str(e.get("date") or "")
        exp, act = e.get("expectation"), e.get("actual")
        if d >= as_of:
            upcoming.append(f"{d} {e.get('label')}"
                            + (f"，预期 {exp}" if exp else ""))
        elif act is not None:
            published.append(f"{d} {e.get('label')}"
                             + (f"，预期 {exp}" if exp else "")
                             + f"，实际 {act}")
    upcoming.sort()
    published.sort(reverse=True)          # most recent surprise first
    levels.sort()
    dated, levels = upcoming, [t for _, t in levels]
    if not dated and not levels and not published:
        # Said out loud rather than left as an empty section: a model given no
        # schedule will otherwise invent plausible dates, and an invented date is
        # worse than an honest level threshold because it looks checkable.
        return ("本次运行没有可用的日程或水平数据。触发条件请改用可读到的价格/利差水平"
                "（写清水平和方向），不要编造具体日期。")
    out = []
    if dated:
        out.append("未来已排定事件（可作为触发日期）：\n"
                   + "\n".join(dated[:limit]))
    if published:
        out.append("最近已公布（预期与实际的差就是现成的错位，不是触发日期）：\n"
                   + "\n".join(published[:limit // 2 or 1]))
    if levels:
        out.append("当前参考水平（可作为触发阈值）：\n"
                   + "\n".join(levels[:limit]))
    return "\n\n".join(out)


def build_prompt(ctx: RunContext,
                 topic: dict[str, Any],
                 card: dict[str, Any] | None = None) -> tuple[str, int]:
    """The four-step prompt, optionally carrying one PM philosophy card.

    The card slot sits between the skeleton and the output contract, which is
    the only position that lets a philosophy add to the reasoning without
    reaching the shared plumbing — universe, citations, shape, horizon — that
    makes the four arms comparable in the first place.

    With `card=None` the joined string is byte-identical to what this arm has
    always sent. That is not tidiness: `carl_constraint` stays the frozen
    control that every derived arm is measured against, and a control whose
    prompt drifted by even a whitespace would no longer be one.
    """
    _docs, n_docs = _gen.corpus_block(ctx, topic)
    blocks = [
        f"今天是 {ctx.as_of.isoformat()}。按下面四步做，每一条想法都要把四步都写出来。",
        _gen.topic_block(topic),
        "相关原始材料（新到旧）：\n" + _docs,
        calendar_block(ctx),
        "可买清单（instrument_id | 名称 | 暴露 | 载体）：\n" + _gen.universe_block(ctx),
        STEPS,
    ]
    if card is not None:
        blocks.append(philosophy.render(card))
    blocks += [
        f"就这个主题给出 {_gen.PER_TOPIC} 条持有期一个月的做多想法。标的原样取自上面的清单；"
        "每条要有一个月内的上行与下行幅度（百分数）和上行/持平/下行三档概率（相加为 1）；"
        "同一主题内不要重复标的。四步里任何一步写不出具体内容，就不要凑这一条——"
        "宁可少给几条。",
        _gen.CITATION_RULE,
        "只输出 JSON 数组，形如：\n" + SHAPE,
    ]
    return "\n\n".join(blocks), n_docs


@register("idea_generator", "carl_constraint", "1.0", role="primary",
          label="约束边界（Carl 式）")
def carl_constraint(ctx: RunContext) -> Verdict:
    """Four steps: anomaly, real motive, binding constraint, dated or level-based trigger."""
    return _gen.generate_per_topic(
        ctx, "carl_constraint", build_prompt,
        require_keys=("anomaly", "motive", "constraint", "trigger"),
        extra_keys=("anomaly", "motive", "constraint", "trigger"))
