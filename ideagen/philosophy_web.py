"""面板上的 PM 语义注入：把准则卡翻译成业务人员看得懂的三句话。

The CLI speaks in cards, arms and frozen slots because that is what the
machinery is made of. Nobody outside this repository should ever have to learn
those words to use the feature, so this layer answers three questions in the
order a person actually asks them:

    我说的话，它听懂成了什么？
    今后每条想法要多回答什么？
    点下去之后会发生什么？

Everything else — card_id, arm names, 筛选B, require_keys, the registry — stays
on this side of the wall. The one machine detail that does cross is the rewrite
warning, and it crosses because it is the one thing only the PM can adjudicate:
whether the sentence the system is about to run is still his.

`runs` is counted from stored verdicts rather than from the calendar. A card
activated on Friday has not run; saying「已生效 3 天」 would invite reading a
P&L that does not exist yet. Zero is stated as zero.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from . import config, philosophy

PENDING = config.DATA / "philosophy" / "pending"

#: Sentences written but not yet distilled. The proposal has always lived on
#: the server so that 「换个浏览器、隔一天回来，那句话还在等你拍板」 was true —
#: but the sentence *before* distillation lived only in one tab's memory, and
#: that is the state a person actually leaves this in: mid-thought, interrupted,
#: three words short of the point. A reload threw away the only part that took
#: any thinking. It gets the same durability as the card it will become.
DRAFTS = config.DATA / "philosophy" / "drafts"

#: How many unfinished rules the desk will hold. Not a storage limit — a
#: reading one. A pile of half-sentences nobody can face is the same as no list,
#: and the pile becomes the reason not to open this at all.
MAX_DRAFTS = 20

DRAFT_ID_RE = re.compile(r"^d-[0-9a-f]{12}$")

#: Where a rule goes unless the PM picks otherwise. The constraint-boundary
#: method is the one whose shape a philosophy usually has — a way of deciding
#: what makes a trade worth doing — so it is the default rather than the only
#: option.
DEFAULT_ARM = "carl_constraint"


def _arm_options() -> list[dict[str, str]]:
    """The four methods, by the names the panel already uses for them.

    Not a dropdown of `carl_constraint` / `ai_native`: those are how the code
    spells them. 「约束边界」「AI 端到端」 are what every other screen calls
    them, so the picker asks a question the PM can already answer.
    """
    try:
        from .strategies import gen_pm
        return gen_pm.options()
    except Exception:  # noqa: BLE001 — a missing picker beats a broken page
        return [{"arm": DEFAULT_ARM, "label": "约束边界"}]


def _runs(p, arm: str) -> int:
    """How many weekly runs this card has actually produced ideas in."""
    try:
        rows = p.state.q("SELECT COUNT(*) AS n FROM verdicts WHERE strategy=?",
                         [arm])
        return int((rows or [{}])[0].get("n") or 0)
    except Exception:  # noqa: BLE001 — a missing table is "none yet", not a 500
        return 0


def _latest(p, arm: str) -> dict[str, Any] | None:
    """The most recent verdict this arm produced, with its run row."""
    try:
        v = p.state.q(
            "SELECT run_id, as_of, chosen, rejected FROM verdicts "
            "WHERE kind='idea_generator' AND strategy=? "
            "ORDER BY as_of DESC LIMIT 1", [arm])
        if not v:
            return None
        run = p.state.q("SELECT * FROM orch_runs WHERE run_id=?",
                        [v[0]["run_id"]])
        return {"verdict": v[0], "run": run[0] if run else None}
    except Exception:  # noqa: BLE001
        return None


def _counts(p, arm: str, base_arm: str) -> dict[str, Any]:
    """The two numbers a rule can be read by before any P&L exists.

    How many ideas it wrote, and how many the arm it was grafted onto wrote in
    the same week. That difference is not performance — it is whether the corpus
    can express this philosophy at all, which is the first thing worth knowing
    and the only thing knowable in week one. `dropped` is the same reading from
    the other side: ideas the rule itself threw away for failing its own fields.
    """
    import json as _json
    out: dict[str, Any] = {"ran": False}
    latest = _latest(p, arm)
    if not latest or p is None:
        return out
    v = latest["verdict"]
    try:
        chosen = _json.loads(v["chosen"] or "[]")
        rejected = _json.loads(v["rejected"] or "{}")
    except (TypeError, ValueError):
        return out
    out.update({"ran": True, "as_of": v["as_of"],
                "produced": len(chosen), "dropped": len(rejected)})
    try:
        b = p.state.q(
            "SELECT chosen FROM verdicts WHERE kind='idea_generator' "
            "AND strategy=? AND as_of=? LIMIT 1", [base_arm, v["as_of"]])
        if b:
            out["base_produced"] = len(_json.loads(b[0]["chosen"] or "[]"))
    except Exception:  # noqa: BLE001
        pass
    return out


def _view(card: dict[str, Any], p=None, *, pending: bool = False
          ) -> dict[str, Any]:
    """One card as the panel shows it."""
    arm = philosophy.arm_name(card)
    out = {
        "id": card["card_id"],
        "said": card["source_utterance"],
        "since": card["as_of"],
        "understood": list(card.get("directives") or []),
        "refuses": list(card.get("forbids") or []),
        "must_answer": [{"field": r["field"], "desc": r["desc"]}
                        for r in (card.get("require") or [])],
        "rewrites": philosophy.translations(card),
        "arm": (card.get("scope") or {}).get("arm"),
        "arm_label": next(
            (o["label"] for o in _arm_options()
             if o["arm"] == (card.get("scope") or {}).get("arm")), ""),
        # Which rule this one was written to replace, carried on the card
        # rather than held in the tab. 「照这条改一版」 used to be a fact known
        # only to the browser that started it: reload before confirming and the
        # revision quietly became an addition, so both versions ran at once and
        # neither one's number meant anything.
        "replaces": str(card.get("replaces") or ""),
        "pending": pending,
    }
    if not pending:
        out["runs"] = _runs(p, arm) if p is not None else 0
        out["retired_on"] = card.get("retired_on")
        out["counts"] = _counts(p, arm, card["scope"]["arm"]) if p else {"ran": False}
    return out


def _pending_cards() -> list[dict[str, Any]]:
    if not PENDING.exists():
        return []
    out = []
    for f in sorted(PENDING.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _history_view(card: dict[str, Any]) -> dict[str, Any]:
    """One rule as the record shows it — the whole card, as it was written.

    `_view` is the operating readout: what is running, how much it produced,
    what to click next. This one answers a different question — 「当时这条准则
    到底说了什么」 — so it carries the parts `_view` drops on purpose, including
    the ones only the record needs: why the distiller read the sentence that
    way, what it checked against the founding principles, and which rule this
    one grew out of.
    """
    arm = str((card.get("scope") or {}).get("arm") or "")
    return {
        "id": card.get("card_id") or "",
        "said": card.get("source_utterance") or "",
        "since": card.get("as_of") or "",
        "retired_on": card.get("retired_on") or "",
        "retired_reason": card.get("retired_reason") or "",
        "replaced_by": card.get("replaced_by") or "",
        "replaces": card.get("replaces") or "",
        "live": not card.get("retired_on"),
        "understood": list(card.get("directives") or []),
        "refuses": list(card.get("forbids") or []),
        "must_answer": [{"field": str(r.get("field") or ""),
                         "desc": str(r.get("desc") or "")}
                        for r in (card.get("require") or [])],
        "rewrites": philosophy.translations(card),
        "why": str(card.get("rationale") or ""),
        "founding_check": str(card.get("founding_check") or ""),
        "arm": arm,
        "arm_label": next((o["label"] for o in _arm_options()
                           if o["arm"] == arm), ""),
    }


def _draft_path(did: str) -> Path | None:
    """The file for a draft id, or None if the id is not one we minted.

    The id reaches here from a request body and is about to be a filename, so
    it is matched against the shape this module writes rather than merely
    checked for slashes.
    """
    return DRAFTS / f"{did}.json" if DRAFT_ID_RE.match(did or "") else None


def _drafts() -> list[dict[str, Any]]:
    """Sentences written but not yet distilled, newest first."""
    if not DRAFTS.exists():
        return []
    out = []
    for f in DRAFTS.glob("d-*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict) or not str(d.get("say") or "").strip():
            continue
        out.append({"id": f.stem, "say": str(d.get("say") or ""),
                    "arm": str(d.get("arm") or ""),
                    "replaces": str(d.get("replaces") or ""),
                    "saved_at": str(d.get("saved_at") or "")})
    out.sort(key=lambda d: d["saved_at"], reverse=True)
    return out


def handle_draft(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/draft — keep the sentence before it is a card.

    Called on a pause in typing, not on a button. A save key you have to
    remember to press is exactly how this goes wrong: the interruption that
    loses the draft is the same interruption that stops you pressing it.
    """
    say = str(payload.get("say") or "").strip()
    did = str(payload.get("id") or "")
    if did and not DRAFT_ID_RE.match(did):
        return {"error": "草稿号不合规范。"}, 400
    if not say:
        # An emptied box is a deletion. A sentence the PM just cleared must not
        # come back tomorrow as though he had never cleared it.
        f = _draft_path(did) if did else None
        if f is not None and f.exists():
            f.unlink()
        return {"ok": True, "id": "", "saved_at": "", "drafts": _drafts()}, 200
    if len(say) > 500:
        return {"error": "过长。一句话即可，越具体越有效（上限 500 字）。"}, 400
    if not did:
        if len(_drafts()) >= MAX_DRAFTS:
            return {"error": f"没写完的准则已经有 {MAX_DRAFTS} 条。"
                             "先把其中一条生成草案或删掉，再写新的。"}, 400
        did = "d-" + uuid.uuid4().hex[:12]
    DRAFTS.mkdir(parents=True, exist_ok=True)
    body = {"say": say,
            "arm": str(payload.get("arm") or DEFAULT_ARM),
            "replaces": str(payload.get("replaces") or ""),
            "saved_at": config.now_hkt().isoformat(timespec="seconds")}
    (DRAFTS / f"{did}.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": did, "saved_at": body["saved_at"],
            "drafts": _drafts()}, 200


def handle_draft_delete(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/draft-delete — throw one unfinished sentence away."""
    f = _draft_path(str(payload.get("id") or ""))
    if f is None or not f.exists():
        return {"error": "没有这条草稿。"}, 404
    f.unlink()
    return {"ok": True, "drafts": _drafts()}, 200


def handle_list() -> tuple[dict[str, Any], int]:
    """GET /api/philosophy — what is running, what is waiting for a decision."""
    from . import ask, platform as plat
    try:
        p = plat.load()
        can, why = ask.inference_state(p)
    except Exception as e:  # noqa: BLE001
        p, can, why = None, False, str(e)[:200]
    live = [_view(c, p) for c in philosophy.cards()]
    return {
        "live": live,
        # Unusable ledger lines are skipped so one mistyped row cannot take the
        # registry down, but skipping silently would be the other failure: a
        # philosophy that stopped running and nobody noticed. So they travel to
        # the panel and get shown.
        "ledger_problems": philosophy.ledger_problems(),
        "pending": [_view(c, pending=True) for c in _pending_cards()],
        # Written, not yet distilled. Server-side for the same reason the
        # proposals are: the tab is not where a person's thinking should live.
        "drafts": _drafts(),
        # Every rule ever in force, retired ones included. `live` above is what
        # is running; this is what was ever said. Retiring a rule used to erase
        # it from every surface anyone looks at, which is the one thing an
        # append-only ledger exists to prevent.
        "history": [_history_view(c) for c in philosophy.history()],
        "arms": _arm_options(),
        "default_arm": DEFAULT_ARM,
        "can_propose": bool(can),
        # Only shown when it blocks the button, so it has to say what to do,
        # not just what failed.
        "why_not": "" if can else (
            "本机未启用推理，无法生成新准则（查看不受影响）。" + (why or "")),
    }, 200


def handle_propose(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/propose — one sentence in, a proposal out.

    Stops at a file on purpose. Distillation is a model call and the model is
    the least trustworthy component in the loop; a sentence parsed fluently
    must not start steering a book before its author has read what it became.
    """
    from . import ask, platform as plat
    from .strategy import available

    say = str(payload.get("say") or "").strip()
    if not say:
        return {"error": "还没有内容。用一句话写下你判断一笔交易的准则。"}, 400
    if len(say) > 500:
        return {"error": "过长。一句话即可，越具体越有效（上限 500 字）。"}, 400

    arm = str(payload.get("arm") or DEFAULT_ARM)
    # Checked against what can actually be derived, not merely what is
    # registered: a card scoped to an arm with no `card=` slot would pass
    # validation, activate, and then quietly never produce anything.
    if arm not in {o["arm"] for o in _arm_options()}:
        return {"error": f"没有这种生成方式：{arm}"}, 400
    p = plat.load()
    ok, why = ask.inference_state(p)
    if not ok:
        return {"error": "本机未启用推理，无法生成新准则。" + (why or ""),
                "unavailable": True}, 503
    try:
        card, bad = philosophy.distill(
            say, p.inference, arm=arm, as_of=config.now_hkt().date(),
            known_arms={r["name"] for r in available("idea_generator")})
    except Exception as e:  # noqa: BLE001 — bounded operator error, no traceback
        return {"error": f"蒸馏失败：{type(e).__name__}: {e}"[:300]}, 502

    if bad:
        # Rejections are the common case for a first attempt and they are the
        # most useful thing this feature says all day, so they come back as
        # advice rather than as a validation dump.
        return {"ok": False, "said": say, "problems": bad,
                "hint": "换一个更具体的说法。有效的准则通常是这个形状："
                        "「看到 X 时，我要的是 Y，因为 Z」。"}, 200

    # What this rule was written to replace rides on the card from here on.
    # Held in the tab instead, a revision confirmed after a reload became an
    # addition — both versions running, neither one's number meaning anything.
    replaces = str(payload.get("replaces") or "")
    if replaces:
        card["replaces"] = replaces
    PENDING.mkdir(parents=True, exist_ok=True)
    (PENDING / f"{card['card_id']}.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    # The sentence is now stored as a card, which carries `source_utterance`
    # verbatim — so the draft is redundant rather than lost, and leaving it
    # would show the same rule twice in two different states.
    f = _draft_path(str(payload.get("draft") or ""))
    if f is not None and f.exists():
        f.unlink()
    return {"ok": True, **_view(card, pending=True)}, 200


def handle_activate(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/activate — put it in force from today."""
    from .strategy import available
    cid = str(payload.get("id") or "")
    f = PENDING / f"{cid}.json"
    if not cid or "/" in cid or not f.exists():
        return {"error": "待确认列表中已无此准则（可能已生效或已撤销）。"}, 404
    card = json.loads(f.read_text(encoding="utf-8"))
    try:
        philosophy.activate(
            card, known_arms={r["name"] for r in available("idea_generator")},
            accept_translations=bool(payload.get("accept_rewrites")))
    except ValueError as e:
        return {"error": str(e)}, 400
    f.unlink()
    # A revision retires what it revises, in the same action. Editing a card in
    # place is not on offer: a rule *is* an arm, and an arm whose content
    # changed while keeping its name turns one track record into a blend of
    # several different rules. Two events on the ledger instead — the lineage
    # is queryable and each arm's series stays clean.
    replaced = str(payload.get("replaces") or card.get("replaces") or "")
    if replaced and replaced != cid:
        try:
            philosophy.retire(replaced, config.now_hkt().date(),
                              f"被 {cid} 替换", replaced_by=cid)
        except ValueError:
            pass  # already retired, or never existed — the new card still stands
    # Registering the derived arm now means the next weekly run picks it up
    # without a restart; a card in force that produces nothing until someone
    # remembers to bounce the service is a card that silently does not exist.
    try:
        from .strategies import gen_pm
        gen_pm._install()
    except Exception:  # noqa: BLE001 — it will install at next import regardless
        pass
    return {"ok": True, "id": cid, "since": card["as_of"],
            "replaced": replaced or None}, 200


def handle_discard(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/discard — throw the card away, keep the sentence.

    「重写」 means rewrite this sentence, not lose it. The card is a machine's
    reading and is disposable; the sentence is the part that took thinking, so
    it goes back to the desk as a draft — on the server, where a reload cannot
    reach it. Nothing is recorded in the ledger either way: a proposal that was
    never in force has no history to keep.
    """
    cid = str(payload.get("id") or "")
    f = PENDING / f"{cid}.json"
    if not cid or "/" in cid or not f.exists():
        return {"error": "待确认列表中已无此项。"}, 404
    try:
        card = json.loads(f.read_text(encoding="utf-8"))
        assert isinstance(card, dict)
    except (OSError, json.JSONDecodeError, AssertionError):
        card = {}
    f.unlink()
    said = str(card.get("source_utterance") or "")
    arm = str((card.get("scope") or {}).get("arm") or "")
    back, _ = handle_draft({"say": said, "arm": arm,
                            "replaces": str(card.get("replaces") or "")})
    # `draft` may be empty if the desk is full. The sentence still travels back
    # in the response, so the box is never the thing that loses it.
    return {"ok": True, "draft": str(back.get("id") or ""),
            "said": said, "arm": arm,
            "replaces": str(card.get("replaces") or "")}, 200


def handle_retire(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/retire — stop it from today forward."""
    cid = str(payload.get("id") or "")
    try:
        philosophy.retire(cid, config.now_hkt().date(),
                          str(payload.get("reason") or ""))
    except ValueError as e:
        return {"error": str(e)}, 404
    return {"ok": True, "id": cid}, 200

#: How many of a rule's ideas the panel will show. The point is to check a few
#: against their sources, not to read a hundred — a list nobody scrolls is the
#: same as no list.
SHOW_IDEAS = 12


def handle_output(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """GET /api/philosophy/output — what this rule actually wrote, last period.

    The panel promises 「这些字段是给你核对用的」 and 「写得实不实，要你点开看」.
    Until this existed there was nothing to click, which made both sentences
    true only in intention. Each idea arrives with the rule's own fields and the
    document id each one claims to rest on, so a claim can be checked against
    the research note it cites in one step.
    """
    from . import ask, platform as plat
    cid = str(payload.get("id") or "")
    card = next((c for c in philosophy.cards() if c["card_id"] == cid), None)
    if not card:
        return {"error": "这条准则不在运行中的列表里。"}, 404

    p = plat.load()
    arm = philosophy.arm_name(card)
    latest = _latest(p, arm)
    if not latest or not latest.get("run"):
        return {"ok": True, "ran": False,
                "hint": "这条准则还没跑过。下一次周跑（周三）它第一次出手。"}, 200

    art = None
    try:
        art = ask._artifact(p, latest["run"], f"B_generators/{arm}.json")
    except Exception:  # noqa: BLE001
        art = None
    if not isinstance(art, dict):
        # The counts survive in the verdict even when the per-arm artifact does
        # not, and saying so beats an empty page that looks like "it wrote
        # nothing".
        return {"ok": True, "ran": True, "as_of": latest["verdict"]["as_of"],
                "ideas": [], "dropped": {},
                "hint": "这一期的产物存档取不到了，只剩计数。"}, 200

    fields = list(philosophy.require_keys(card))
    ideas = []
    for i in (art.get("produced") or [])[:SHOW_IDEAS]:
        ideas.append({
            "instrument_id": i.get("instrument_id"),
            "instrument_name": i.get("instrument_name"),
            "topic_id": i.get("topic_id"),
            "thesis": i.get("thesis"),
            "answers": [{"field": f, "value": i.get(f),
                         "doc": i.get(f"{f}_doc")} for f in fields],
            "citations": i.get("citations") or [],
        })
    # Drops are grouped by reason rather than listed one by one: 「三条因为出处
    # 对不上语料」 is the reading, and thirty lines of the same sentence is not.
    buckets: dict[str, int] = {}
    for reason in (art.get("rejected") or {}).values():
        key = str(reason).split("：")[0].split(":")[0].strip()
        buckets[key] = buckets.get(key, 0) + 1
    return {"ok": True, "ran": True, "as_of": latest["verdict"]["as_of"],
            "n_produced": len(art.get("produced") or []),
            "shown": len(ideas), "ideas": ideas, "dropped": buckets}, 200
