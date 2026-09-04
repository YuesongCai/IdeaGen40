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
from datetime import date
from pathlib import Path
from typing import Any

from . import config, philosophy

PENDING = config.DATA / "philosophy" / "pending"

#: The panel never asks which arm; there is one place a philosophy goes today
#: and offering a dropdown of internal strategy names would be asking the user
#: to know something this layer exists to hide.
DEFAULT_ARM = "carl_constraint"


def _runs(p, arm: str) -> int:
    """How many weekly runs this card has actually produced ideas in."""
    try:
        rows = p.state.q("SELECT COUNT(*) AS n FROM verdicts WHERE strategy=?",
                         [arm])
        return int((rows or [{}])[0].get("n") or 0)
    except Exception:  # noqa: BLE001 — a missing table is "none yet", not a 500
        return 0


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
        "pending": pending,
    }
    if not pending:
        out["runs"] = _runs(p, arm) if p is not None else 0
        out["retired_on"] = card.get("retired_on")
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
        "pending": [_view(c, pending=True) for c in _pending_cards()],
        "can_propose": bool(can),
        # Only shown when it blocks the button, so it has to say what to do,
        # not just what failed.
        "why_not": "" if can else (
            "这台机器上没开推理，说不了新准则（看的功能不受影响）。" + (why or "")),
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
        return {"error": "还没写呢——用一句话说说你怎么看一笔交易。"}, 400
    if len(say) > 500:
        return {"error": "太长了。一句话就好，越具体越管用（上限 500 字）。"}, 400

    arm = str(payload.get("arm") or DEFAULT_ARM)
    p = plat.load()
    ok, why = ask.inference_state(p)
    if not ok:
        return {"error": "这台机器上没开推理，说不了新准则。" + (why or ""),
                "unavailable": True}, 503
    try:
        card, bad = philosophy.distill(
            say, p.inference, arm=arm, as_of=config.now_hkt().date(),
            known_arms={r["name"] for r in available("idea_generator")})
    except Exception as e:  # noqa: BLE001 — bounded operator error, no traceback
        return {"error": f"没蒸馏出来：{type(e).__name__}: {e}"[:300]}, 502

    if bad:
        # Rejections are the common case for a first attempt and they are the
        # most useful thing this feature says all day, so they come back as
        # advice rather than as a validation dump.
        return {"ok": False, "said": say, "problems": bad,
                "hint": "换个更具体的说法再试。最管用的一句话通常长这样："
                        "「看到 X 的时候，我要的是 Y，因为 Z」。"}, 200

    PENDING.mkdir(parents=True, exist_ok=True)
    (PENDING / f"{card['card_id']}.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **_view(card, pending=True)}, 200


def handle_activate(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/activate — put it in force from today."""
    from .strategy import available
    cid = str(payload.get("id") or "")
    f = PENDING / f"{cid}.json"
    if not cid or "/" in cid or not f.exists():
        return {"error": "这条准则已经不在待确认里了（可能已生效或已撤销）。"}, 404
    card = json.loads(f.read_text(encoding="utf-8"))
    try:
        philosophy.activate(
            card, known_arms={r["name"] for r in available("idea_generator")},
            accept_translations=bool(payload.get("accept_rewrites")))
    except ValueError as e:
        return {"error": str(e)}, 400
    f.unlink()
    # Registering the derived arm now means the next weekly run picks it up
    # without a restart; a card in force that produces nothing until someone
    # remembers to bounce the service is a card that silently does not exist.
    try:
        from .strategies import gen_pm
        gen_pm._install()
    except Exception:  # noqa: BLE001 — it will install at next import regardless
        pass
    return {"ok": True, "id": cid, "since": card["as_of"]}, 200


def handle_discard(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/discard — throw away a proposal, nothing recorded."""
    cid = str(payload.get("id") or "")
    f = PENDING / f"{cid}.json"
    if not cid or "/" in cid or not f.exists():
        return {"error": "已经没有这条待确认了。"}, 404
    f.unlink()
    return {"ok": True}, 200


def handle_retire(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /api/philosophy/retire — stop it from today forward."""
    cid = str(payload.get("id") or "")
    try:
        philosophy.retire(cid, config.now_hkt().date(),
                          str(payload.get("reason") or ""))
    except ValueError as e:
        return {"error": str(e)}, 404
    return {"ok": True, "id": cid}, 200
