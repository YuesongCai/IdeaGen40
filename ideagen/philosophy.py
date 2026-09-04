"""PM 语义注入：一句话 → 一张受版本管理的准则卡 → 一条新臂。

The ask is natural: the PM has a way of thinking about a trade that the four
generators do not know about, and he wants to hand it over in one sentence
rather than through a code change. "我不买已经被讲烂的东西，我要的是被迫的卖家"
is a real edge and it is currently nowhere in the system.

The obvious implementation — a text box that appends to a generator's prompt —
would destroy more than it adds, for four reasons that this module is built to
avoid:

**It breaks the comparison.** 筛选B's four arms are comparable only because the
plumbing is byte-identical and the reasoning skeleton is the sole variable. A
prompt that changes week to week turns one arm's track record into a blend of
several different strategies, and the realised win rate stops meaning anything.

**It breaks replay.** `RunContext` is frozen and hashed so a verdict can be
reproduced months later. An override living outside that hash makes the
reproduction silently wrong.

**It breaks attribution.** Every stored score carries `strategy` + `version`. A
prompt edited without a version bump makes those rows unattributable.

**It is an overfitting machine.** A knob the PM can turn each Wednesday while
watching last week's P&L is hand-run gradient descent on a sample of four. That
is precisely the multiple-testing objection Jon raised on 08-18.

So injection here is not an edit. It is a birth:

    一句话  ──蒸馏──▸  准则卡（结构化、带体检、可判定）
                          │
                          └──▸ 派生臂 carl_constraint@pm-01
                                 原臂 carl_constraint 冻结不动，继续作为 control

The原臂 keeps its byte-identical prompt and its uninterrupted history. The
derived arm starts its own series from its birth date with an honest n=1, runs
on the same corpus in the same week against the same universe, and four weeks
later the panel can say what that one sentence was worth. A philosophy that
cannot beat the arm it was grafted onto is a philosophy the book should not be
carrying, and this is the only arrangement in which that sentence is true.

Three properties do the work:

**Only the skeleton slot is writable.** `FROZEN` lists what a card may never
touch — universe, citation rule, JSON shape, horizon, idea count. Those are the
shared plumbing; a card that moved them would be changing the measuring
instrument along with the thing measured. Distillation is told about the frozen
list and the result is checked against it again on the way in, because a model
asked to respect a boundary is not the same as a boundary.

**Every directive must become a field the idea has to fill.** This is the part
that makes a philosophy checkable rather than decorative. 「找被迫的卖家」as a
prompt line produces ideas that may or may not have looked for one; the same
line carrying `require: forced_seller` produces ideas that name the seller, the
covenant and the deadline — or get dropped by `_gen.mint` with a recorded
reason. A month later the question "did the philosophy actually get applied"
has an answer either way, and the drop rate is itself the answer to "does the
corpus support this way of thinking at all".

**The ledger is append-only and as-of stamped.** A card is added or retired,
never rewritten, and a run only sees cards whose `as_of` is on or before its own
run date. Replaying August with a philosophy written in September would
manufacture a track record out of hindsight, which is the same cheat
`first_fillable` refuses on the fill side.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from . import config

#: Where cards live. One JSONL, append-only, one event per line: a card being
#: added or a card being retired. Rewriting a line is not an operation this
#: module offers, for the same reason the theme registry does not offer one.
LEDGER = config.DATA / "philosophy" / "ledger.jsonl"

#: The founding principles, as a file rather than as folklore, so the health
#: check has something to actually check against.
PRINCIPLES = config.ROOT / "prompts" / "founding_principles.md"

#: What a card may never touch. These are the shared parts of 筛选B's prompt —
#: the parts held identical across all four arms so that a difference in
#: realised return can only have come from the reasoning skeleton. A card that
#: reached any of them would be changing the ruler and the measurement at once.
FROZEN = {
    "universe": "可买清单与标的解析（清单从哪来、允许什么载体）",
    "citations": "引用契约（每条想法必须给 1-3 个真实 doc_id）",
    "shape": "输出 JSON 的字段与形状",
    "horizon": "一个月持有期",
    "count": "每主题 20 条的目标产量",
    "direction": "只做多（做空通过反向/防御标的表达）",
    "odds": "上下行幅度与三档概率的定义与归一",
    "sizing": "仓位权重与止盈止损（归筛选C 与建仓，生成臂说了不算）",
}

#: Which stages accept a card at all. 筛选A is deliberately excluded: injecting
#: preferences into topic selection changes what the whole week is about, and
#: every downstream difference then has two possible causes at once. 筛选C is
#: excluded because its knobs — thresholds, stop width, ranking key — are
#: already parameters with declared arms; free text there would duplicate them
#: in a form that cannot be swept.
INJECTABLE_STAGES = ("idea_generator",)

#: Field names a card may not claim, because `_gen.mint` already owns them.
RESERVED_FIELDS = {
    "id", "instrument_id", "instrument_name", "topic_id", "method", "thesis",
    "citations", "bad_citations", "vehicle", "exposure", "horizon_days",
    "upside_pct", "downside_pct", "p_up", "p_base", "p_down", "p_sum_raw",
}

CARD_ID_RE = re.compile(r"^pm-\d{4}-\d{2}-\d{2}-[a-z0-9-]{2,40}$")


# ---------------------------------------------------------------------------
# ledger
def _read_events() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # A corrupt line is reported by `problems()`, not silently skipped
            # into a smaller ledger — a philosophy that vanished without anyone
            # noticing is worse than one that fails loudly.
            out.append({"event": "corrupt", "raw": line[:200]})
    return out


def _append(event: dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def cards(as_of: date | None = None, *, include_retired: bool = False
          ) -> list[dict[str, Any]]:
    """Cards in force as of a date, oldest first.

    A card is in force from its own `as_of` — not from the moment it was
    written to the file. Replaying an earlier period therefore sees the arms
    that period actually had, and a card added today cannot lend its judgement
    to a book that was filled in August.
    """
    live: dict[str, dict[str, Any]] = {}
    retired: dict[str, str] = {}
    for e in _read_events():
        cid = str(e.get("card_id") or "")
        if e.get("event") == "activate" and cid:
            live[cid] = e["card"]
        elif e.get("event") == "retire" and cid:
            retired[cid] = str(e.get("as_of") or "")
    out = []
    for cid, card in live.items():
        born = str(card.get("as_of") or "")
        if as_of and born > as_of.isoformat():
            continue
        gone = retired.get(cid)
        if gone and not include_retired and (not as_of or gone <= as_of.isoformat()):
            continue
        card = dict(card)
        card["retired_on"] = gone
        out.append(card)
    out.sort(key=lambda c: (str(c.get("as_of")), str(c.get("card_id"))))
    return out


def for_arm(arm: str, as_of: date | None = None) -> list[dict[str, Any]]:
    return [c for c in cards(as_of)
            if str((c.get("scope") or {}).get("arm")) == arm]


# ---------------------------------------------------------------------------
# validation
def _slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:40] or "card"


#: Textual backstop for the frozen list. The model is told the boundary in the
#: distillation prompt; these are the checks that do not depend on it having
#: listened. Each pattern targets a *permissive* phrasing — an instruction to do
#: the forbidden thing — rather than any mention of it, because a directive that
#: says 「若观点为看空，改用反向标的做多表达」 is the boundary being respected,
#: not breached.
_PROSE_BACKSTOP: tuple[tuple[str, str], ...] = (
    (r"允许做空|可以做空|直接做空|融券|卖空表达", "只做多是账户层面的硬约束"),
    (r"清单之外|清单以外|自选标的|不在清单|不受清单", "标的只能来自可买清单"),
    (r"半年|三个月|6\s*个月|一年|季度持有|放宽到.{0,4}月", "持有期一个月是钉死的"),
    (r"不用引用|无需引用|不必给 ?doc_id|可以不给引用", "引用契约不可豁免"),
    # Sizing is the quiet one: a generator has no channel to the book, so a
    # directive about weight or stops is not dangerous, it is inert — the model
    # writes a field nobody reads and the PM believes his rule is running.
    (r"仓位|权重|position_size|重仓|加仓|止损|止盈|倍(?=的?仓)", "仓位与止盈止损归筛选C 与建仓，生成臂改不动"),
)


def translations(card: dict[str, Any]) -> list[str]:
    """Where the PM's sentence hit a hard boundary and was rewritten to fit.

    Separate from `problems()` on purpose. 「该做空的时候要能做空」 cannot run as
    stated, and a distiller that turns it into 「看空就买反向标的」 has done the
    right thing — but it has also quietly replaced the PM's instruction with its
    own. He has to see that before it steers a book, because the rewrite may not
    be what he meant, and the moment to find out is now rather than in the
    November review.

    Matching is by substring rather than exact key: the distiller reports these
    as prose (`"direction: 原话要求做空，已改为……"`), and an exact-key check
    silently passes every one of them — which is how this hole was found.
    """
    out: list[str] = []
    for raw in (card.get("touches_frozen") or []):
        text = str(raw).strip()
        if not text:
            continue
        hit = next((k for k in FROZEN if k in text.lower()), None)
        out.append(f"{FROZEN[hit]}：{text}" if hit else f"（未归类）{text}")
    return out


def problems(card: dict[str, Any], *, existing: list[dict[str, Any]] | None = None,
             known_arms: set[str] | None = None) -> list[str]:
    """Everything wrong with a card, in Chinese, for a person to read.

    Run twice by design: once inside distillation so the model can be told what
    it got wrong, and once again at activation. The second pass is the one that
    matters — the first is a courtesy to the model, the second is the boundary.
    """
    bad: list[str] = []
    cid = str(card.get("card_id") or "")
    if not CARD_ID_RE.match(cid):
        bad.append(f"card_id 不合规范（应形如 pm-2026-09-04-forced-seller）：{cid!r}")

    try:
        date.fromisoformat(str(card.get("as_of")))
    except (TypeError, ValueError):
        bad.append("as_of 缺失或不是 YYYY-MM-DD")

    if not str(card.get("source_utterance") or "").strip():
        bad.append("没有留下 PM 的原话——蒸馏结果必须能追回到那一句")

    scope = card.get("scope") or {}
    stage, arm = str(scope.get("stage") or ""), str(scope.get("arm") or "")
    if stage not in INJECTABLE_STAGES:
        bad.append(f"只能注入 {'/'.join(INJECTABLE_STAGES)}，拿到的是 {stage!r}"
                   "（筛选A 会让整周的题目都变，筛选C 的松紧已经是参数臂）")
    if not arm:
        bad.append("没说注入哪一条臂")
    elif known_arms is not None and arm not in known_arms:
        bad.append(f"{arm!r} 不是已注册的臂；现有：{sorted(known_arms)}")

    directives = [str(d).strip() for d in (card.get("directives") or [])
                  if str(d).strip()]
    if not directives:
        bad.append("directives 为空——一句话没有被蒸馏成任何可执行的指令")
    if len(directives) > 6:
        bad.append(f"directives {len(directives)} 条太多；一次注入一个想法，"
                   "多条准则请拆成多张卡，否则跑赢跑输归因不到哪一条")

    req = card.get("require") or []
    if not req:
        bad.append("require 为空——准则没有落成想法必须填的字段，"
                   "事后无法判断模型到底有没有照做")
    seen: set[str] = set()
    for r in req:
        f = str((r or {}).get("field") or "").strip()
        if not re.match(r"^[a-z][a-z0-9_]{2,30}$", f):
            bad.append(f"require 字段名不合规（小写下划线）：{f!r}")
        if f in RESERVED_FIELDS:
            bad.append(f"require 字段 {f!r} 与系统字段冲突")
        if f in seen:
            bad.append(f"require 字段重复：{f!r}")
        seen.add(f)
        if not str((r or {}).get("desc") or "").strip():
            bad.append(f"require 字段 {f!r} 没有说明要写什么")
    if len(req) > 3:
        bad.append(f"require {len(req)} 个字段太多；每多一个必填字段，"
                   "整条想法被丢弃的概率就高一截，最多 3 个")

    blob = " ".join(directives + [str((r or {}).get("desc") or "") for r in req])
    for pat, why in _PROSE_BACKSTOP:
        if re.search(pat, blob):
            bad.append(f"文本触碰了不可注入区：{why}")

    for other in (existing or []):
        if str(other.get("card_id")) == cid:
            bad.append(f"card_id 已存在：{cid}")
        if (str(other.get("source_utterance") or "").strip()
                == str(card.get("source_utterance") or "").strip()):
            bad.append(f"这句话已经登记过了（{other.get('card_id')}）")
    return bad


# ---------------------------------------------------------------------------
# distillation
def _principles_text() -> str:
    try:
        return PRINCIPLES.read_text(encoding="utf-8")
    except OSError:
        return "（初心文件缺失，体检只能做机械检查）"


DISTILL_SYSTEM = """你是 IdeaGen 的准则蒸馏器。基金经理会给你一句口语化的投资
哲学或流程习惯，你要把它变成一张能被机器执行、能被事后检验的准则卡。

你不是在改写他的话，你在回答三个问题：

1. 这句话落到「生成一条交易想法」这个动作上，具体要求模型多做什么、少做什么？
   写成 1-3 条祈使句（directives），每条都要能让人判断有没有做到。
   「更谨慎一点」不合格；「第二步必须指名一个被合同/监管期限逼着行动的主体」合格。

2. 这句话有没有明确禁止的东西？写进 forbids。没有就给空数组，不要凑。

3. **最关键**：怎么让每一条想法自己证明它遵守了这条准则？
   给 1-2 个想法必须填的新字段（require），字段名小写下划线，desc 写清要填什么。
   没有这一步，这条准则一个月后无法被检验，等于没注入。

4. 每条 directive 只能要求模型用它当场拿得到的东西去做：那一批研报原文、
   已排定的日程与当前水平、可买清单。凡是要它去查手上没有的东西——
   「排除被主流媒体上过头条的主题」「看机构持仓集中度」——都不合格，
   换成一个能从材料本身读出来的判据，或者干脆不写。
   一条做不到的准则不会让模型照做，只会让它编一个像样的理由。

硬边界，任何 directive 和 require 都不许越过：
{frozen}

如果他这句话本身就撞上了其中某一条（比如他说要做空、要拿三个月、要加仓），
不要假装没看见，也不要沉默地改掉。在 touches_frozen 里逐条写明：
撞到的是哪一条、他原本要的是什么、你把它改成了什么。
格式形如 "direction: 他要做空，改为看空时买反向标的做多表达"。
这不算失败——这是必须由他本人过目的改写，系统会拦下来让他确认。
真正的失败是你把它改了却不说。

初心（与之冲突的一律不要蒸馏，在 founding_check 里说明冲突在哪）：
{principles}

只输出 JSON，不要解释，形如：
{{"directives":["..."],"forbids":["..."],
  "require":[{{"field":"forced_seller","desc":"谁被迫、被什么条款逼着、期限在哪"}}],
  "rationale":"一句话说明为什么这样蒸馏能落地他那句话",
  "touches_frozen":[],"founding_check":"与初心一致，因为……"}}"""


def distill(utterance: str, infer: Any, *, arm: str, as_of: date,
            slug: str | None = None, known_arms: set[str] | None = None
            ) -> tuple[dict[str, Any], list[str]]:
    """One sentence in, one card plus its health report out.

    The card is *not* activated here. Distillation is a model call and the model
    is the least trustworthy component in the loop; what comes back is a
    proposal a person reads and confirms. `problems()` is run on it so the
    person is reading a checked proposal rather than a fluent one.
    """
    if infer is None:
        raise RuntimeError("蒸馏需要模型推理，当前运行没有可用的 inference 端口")
    frozen = "\n".join(f"- {k}：{v}" for k, v in FROZEN.items())
    system = DISTILL_SYSTEM.format(frozen=frozen, principles=_principles_text())
    prompt = (f"基金经理原话：「{utterance.strip()}」\n\n"
              f"注入目标：筛选B 的 {arm} 臂。\n"
              "把它蒸馏成准则卡。")
    from .strategies import _gen          # local: parse_json only, no cycle
    c = infer.complete(prompt, system=system, temperature=0.1, max_tokens=2000)
    raw = _gen.parse_json(c.text)
    if not isinstance(raw, dict):
        raise ValueError(f"蒸馏返回的不是对象：{str(raw)[:200]}")

    card = {
        "card_id": f"pm-{as_of.isoformat()}-{_slug(slug or utterance)}",
        "as_of": as_of.isoformat(),
        "source_utterance": utterance.strip(),
        "scope": {"stage": "idea_generator", "arm": arm},
        "directives": [str(d) for d in (raw.get("directives") or [])][:6],
        "forbids": [str(f) for f in (raw.get("forbids") or [])][:6],
        "require": [{"field": str(r.get("field") or ""),
                     "desc": str(r.get("desc") or "")}
                    for r in (raw.get("require") or []) if isinstance(r, dict)][:4],
        "rationale": str(raw.get("rationale") or "")[:600],
        "touches_frozen": [str(x) for x in (raw.get("touches_frozen") or [])],
        "founding_check": str(raw.get("founding_check") or "")[:600],
        "distilled_by": getattr(c, "model", None) or "unknown",
    }
    return card, problems(card, existing=cards(), known_arms=known_arms)


def activate(card: dict[str, Any], *, known_arms: set[str] | None = None,
             accept_translations: bool = False) -> dict[str, Any]:
    """Put a card into force.

    Refuses anything `problems()` objects to, and refuses a card carrying an
    unacknowledged rewrite: a philosophy that reached the book in a form its
    author never read is the failure this whole module is arranged against.
    """
    bad = problems(card, existing=cards(), known_arms=known_arms)
    if bad:
        raise ValueError("准则卡未通过体检：\n- " + "\n- ".join(bad))
    tr = translations(card)
    if tr and not accept_translations:
        raise ValueError("这句话有地方碰到硬边界，已被改写成边界内的写法。"
                         "先看一遍改写是不是你的意思，确认后再加 "
                         "--accept-translation：\n- " + "\n- ".join(tr))
    _append({"event": "activate", "card_id": card["card_id"],
             "as_of": card["as_of"], "card": card})
    return card


def retire(card_id: str, as_of: date, reason: str = "") -> None:
    """Stop a card from a date forward. The history it already wrote stays."""
    if card_id not in {c["card_id"] for c in cards(include_retired=True)}:
        raise ValueError(f"没有这张卡：{card_id}")
    _append({"event": "retire", "card_id": card_id,
             "as_of": as_of.isoformat(), "reason": reason})


# ---------------------------------------------------------------------------
# what the generator actually sees
def render(card: dict[str, Any]) -> str:
    """The prompt block a derived arm carries, and nothing more.

    Deliberately fenced and labelled. The model is told this is an addition to
    the method rather than a replacement of it, because a card that quietly read
    as "ignore the four steps" would leave the derived arm sharing a name with a
    skeleton it no longer runs.
    """
    out = [f"【本臂附加准则 · PM 注入 {card['card_id']}，{card['as_of']} 起生效】",
           f"（原话：「{card['source_utterance']}」。以下是在上面方法之外**追加**的要求，"
           "不替换上面的任何一步。）"]
    for n, d in enumerate(card.get("directives") or [], 1):
        out.append(f"{n}. {d}")
    if card.get("forbids"):
        out.append("不接受的写法：" + "；".join(card["forbids"]))
    req = card.get("require") or []
    if req:
        out.append("每条想法必须额外写出下面的字段，写不出就不要凑这一条："
                   + "；".join(f"{r['field']}——{r['desc']}" for r in req))
    return "\n".join(out)


def require_keys(card: dict[str, Any]) -> tuple[str, ...]:
    """The fields `_gen.mint` will enforce for this card. This is the teeth."""
    return tuple(str(r["field"]) for r in (card.get("require") or [])
                 if r.get("field"))


def arm_name(card: dict[str, Any]) -> str:
    """`carl_constraint@pm-2026-09-04-forced-seller` — the base arm is visible in
    the name so a book can be read without a lookup."""
    return f"{card['scope']['arm']}@{card['card_id']}"
