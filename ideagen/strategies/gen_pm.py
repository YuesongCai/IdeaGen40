"""派生臂：每一张生效的 PM 准则卡，自动长出一条与原臂并跑的新臂。

Registration rather than modification is the whole point. When a card is
activated, this module registers `carl_constraint@pm-<id>` beside the untouched
`carl_constraint`. The two run in the same week, over the same corpus, against
the same universe, through the same parser — the card is the only difference,
which is the same discipline that makes the original four arms comparable.

What that buys, concretely:

* the control keeps an unbroken series, so the backtest written before the
  injection is still valid after it;
* the derived arm's series starts at n=1 on its birth date and says so, instead
  of inheriting a track record it did not earn;
* four weeks later "was the PM's sentence worth anything" is a difference
  between two arms on paired weeks, not an impression;
* and a card that is retired stops producing, without rewriting what it already
  produced.

The as-of guard below is the counterpart of `first_fillable` refusing to fill an
idea against a bar that had already printed. A card written in September must
not be able to speak in a replay of August; a philosophy that gets to apply
itself to weeks it never saw would manufacture a record out of hindsight.

Adding another base arm here is one line in `BASES` — plus that generator's
`build_prompt` taking the same optional `card` argument, which is where the
frozen-plumbing boundary is actually enforced.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from . import gen_ai_native, gen_carl, gen_chain, gen_gap
from .. import philosophy
from ..strategy import RunContext, Verdict, register, spec

#: Base arms that accept a card, with the reasoning fields they already require.
#: A generator qualifies only once its `build_prompt` takes `card=None` and
#: appends the block after its skeleton — the position that keeps universe,
#: citations, shape and horizon out of a card's reach.
BASES: dict[str, dict[str, Any]] = {
    "carl_constraint": {
        "build_prompt": gen_carl.build_prompt,
        "keys": ("anomaly", "motive", "constraint", "trigger"),
        "label": "约束边界",
    },
    "ai_native": {
        "build_prompt": gen_ai_native.build_prompt,
        # No method-specific fields: this arm's whole point is that it imposes
        # no skeleton. A card grafted here therefore asks a different question
        # from the other three — whether one PM rule beats no rule at all,
        # rather than whether it improves a skeleton that already exists.
        "keys": (),
        "label": "AI 端到端",
    },
    "chain": {
        "build_prompt": gen_chain.build_prompt,
        "keys": ("chain", "watch_variable", "falsifier"),
        "label": "传导链",
    },
    "gap": {
        "build_prompt": gen_gap.build_prompt,
        "keys": ("implied_consensus", "contradiction", "unexpressed"),
        "label": "共识缺口",
    },
}


def options() -> list[dict[str, str]]:
    """The arms a rule can be grafted onto, for the panel's picker.

    Labels rather than names: 「约束边界」 is what the panel already calls this
    method everywhere else, and the PM should never have to learn that it is
    spelled `carl_constraint` underneath.
    """
    return [{"arm": k, "label": v["label"]} for k, v in BASES.items()]


def _make(card: dict[str, Any]):
    base_name = card["scope"]["arm"]
    base = BASES[base_name]
    extra = philosophy.require_keys(card)
    # The card's fields are *required*, not merely requested. A directive the
    # model can skip without consequence is a directive whose effect cannot be
    # read off the output a month later; enforcing it through the same
    # `mint` path as every other arm means a philosophy the corpus cannot
    # support shows up as a drop count instead of as quiet compliance.
    keys = tuple(base["keys"]) + extra

    def run(ctx: RunContext) -> Verdict:
        born = card["as_of"]
        if ctx.as_of.isoformat() < born:
            raise RuntimeError(
                f"准则卡 {card['card_id']} 自 {born} 起生效，"
                f"不能用于 {ctx.as_of.isoformat()} 的运行——"
                "让今天的哲学去跑它没见过的那几周，等于用后见之明造业绩")
        v = _run_base(ctx, card, base, keys)
        # The utterance itself is deliberately absent. `review.state` copies a
        # generator verdict's whole `meta` into the panel payload, and that
        # payload is what gets exported to the public GitHub Pages snapshot —
        # so anything put here is published. The card id is enough to attribute
        # an arm; the sentence behind it lives in the ledger and the audit
        # bundle, both behind the same gate as the licensed research bodies.
        v.meta.update({
            "philosophy_card": card["card_id"],
            "philosophy_base_arm": base_name,
            "philosophy_since": born,
            "philosophy_require": list(extra),
        })
        return v

    run.__doc__ = (f"{base['label']} + PM 准则「{card['source_utterance']}」"
                   f"（{card['card_id']}）")
    return run


def _run_base(ctx: RunContext, card: dict[str, Any], base: dict[str, Any],
              keys: tuple[str, ...]) -> Verdict:
    from . import _gen
    return _gen.generate_per_topic(
        ctx, philosophy.arm_name(card),
        partial(base["build_prompt"], card=card),
        require_keys=keys, extra_keys=keys)


def _install() -> None:
    """Register one arm per card in force. Called at import, like any plugin.

    A card scoped to an arm this module does not know how to derive is skipped
    rather than raised on: the ledger is append-only and outlives any one
    generator, so an old card naming a retired arm must not stop the whole
    registry from loading.
    """
    from ..strategy import available
    have = {r["name"] for r in available("idea_generator")}
    for card in philosophy.cards():
        if card["scope"]["arm"] not in BASES:
            continue
        if philosophy.arm_name(card) in have:
            # Already registered — a second pass (a reload, a test, a server
            # asked to pick up a newly activated card) must be a no-op rather
            # than an exception, or activating a card can take down a process
            # that was running perfectly well without it.
            continue
        base_v = spec("idea_generator", card["scope"]["arm"])["version"]
        register("idea_generator", philosophy.arm_name(card),
                 f"{base_v}+{card['card_id']}",
                 role="exploratory",
                 label=f"{BASES[card['scope']['arm']]['label']} · PM 注入")(
            _make(card))


_install()
