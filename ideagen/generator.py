"""Rule-based batch generator.

Purpose: give the study a *daily* history. The Claude-authored path
(`prompts/idea_generation.md`) is the going-forward generator and writes better
theses, but it cannot retroactively produce a batch for every past day. This
module can, from the same briefing pack and through the same validation gate.

Batches it produces are tagged `generator="rules:v0.4"` so they are never confused
with Claude-authored ones — `analytics` can cut by generator, and the dashboard
labels each batch with its source.

Every decision below is a rule over the day's own scores, not a free parameter:

  theme quota      proportional to TIS rank among admitted themes
  instrument       the theme's registered exposures, mapped through the frozen
                   universe, cheapest-unused first
  action           C >= 80 -> wait for pullback and halve size (v0.4 crowding
                   discipline); M >= 80 -> mature, observation-sized only;
                   M < 30 and C < 60 -> executable; otherwise wait
  conviction       k_up scales with TIS; k_down with C (a crowded trade is given
                   a wider downside)
  probabilities    up-probability rises with TIS, falls with C
  scenarios        every leg is a multiple of the instrument's own realised
                   horizon sigma, so `vol_check` is `ok` by construction
  citations        real doc_ids drawn from that theme's own evidence list

The thesis text is templated. That is stated plainly rather than dressed up: its
job is to record *why the rule fired*, with the actual factor values and real
source ids, so a reader can audit the decision. It is not a substitute for the
research prose a human or Claude writes.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from . import config, db, ideas as ideas_mod, lexicon, universe

GENERATOR = "rules:v0.4"
N_IDEAS = 40
TARGET_1M = 13

# exposure label -> ordered instrument preference. Only keys present in the frozen
# universe are used; the first markable one wins.
_EXPOSURE_FALLBACK = {
    "美元现金等价": ["BIL", "USFR", "SHY"],
}


def _pool(pack: dict) -> dict[str, list[str]]:
    """exposure -> [instrument keys], restricted to what the pack says is markable."""
    ok = {c["key"] for c in pack["universe"]["listed_markable"]}
    ok |= {f["key"] for f in pack["universe"]["funds_on_shelf"] if f.get("markable")}
    out: dict[str, list[str]] = {}
    for key, insts in pack["universe"]["exposures"].items():
        keep = [i for i in insts if i in ok and i in pack["quotes"]]
        if keep:
            out[key] = keep
    return out


def _quota(themes: list[dict]) -> dict[str, int]:
    """Split 40 ideas across themes, best-scoring first.

    Admitted themes (eligible, TIS >= watch) take 4-6 each; the remainder is spread
    over the next tier at 2-3, then 1 each until 40 is reached. The count is exact
    by construction — a batch of 39 or 41 fails validation.
    """
    admitted = [t for t in themes
                if t.get("factors", {}).get("eligible")
                and (t["tis"] or 0) >= config.THEME_TIER_THRESHOLDS["watch"]]
    admitted = admitted[:config.MAX_REPORT_THEMES]
    rest = [t for t in themes if t not in admitted and (t["tis"] or 0) > 0]

    q: dict[str, int] = {}
    for i, t in enumerate(admitted):
        q[t["theme_id"]] = 6 if i < 2 else 5 if i < 4 else 4
    for t in rest:
        q[t["theme_id"]] = 2
    return q


def _action(m: float | None, c: float | None) -> tuple[str, float]:
    """(action, size multiplier) from market validation and crowding."""
    m = 50.0 if m is None else m
    c = 50.0 if c is None else c
    if c >= 80:
        return "等待回踩", 0.5          # v0.4 crowding discipline
    if m >= 80:
        return "小仓试错", 0.5          # 交易成熟: observation only
    if m < 30 and c < 60:
        return "可执行", 1.0
    if m < 30:
        return "等待突破", 0.75         # unpriced but crowded: make price prove it
    return "等待回踩", 0.75


def _shape(tis: float, m: float | None, c: float | None) -> dict:
    """Conviction and probabilities, both driven by the day's own factor values."""
    tis = max(0.0, min(100.0, tis or 0.0))
    c = 50.0 if c is None else c
    k_up = 0.80 + 0.55 * (tis / 100.0)                 # 0.80 .. 1.35
    k_dn = -(0.80 + 0.35 * (c / 100.0))                # -0.80 .. -1.15
    p_up = 26 + 12 * (tis / 100.0) - 6 * (c / 100.0)   # 20 .. 38
    p_up = int(round(max(20, min(38, p_up))))
    p_dn = int(round(max(16, min(30, 18 + 10 * (c / 100.0)))))
    return {"k_up": round(k_up, 3), "k_base": 0.30, "k_dn": round(k_dn, 3),
            "p": [p_up, 100 - p_up - p_dn, p_dn]}


def build_payload(pack: dict) -> dict:
    themes = pack["themes"]
    quotes = pack["quotes"]
    pool = _pool(pack)
    quota = _quota(themes)
    by_id = {t["theme_id"]: t for t in themes}
    dict_by_id = {t["id"]: t for t in pack["theme_dictionary"]}

    used: set[str] = set()
    ideas: list[dict] = []
    signals: dict[str, dict] = {}
    transmissions: dict[str, dict] = {}
    n_1m = 0

    def emit(theme_id: str, horizon: str) -> bool:
        nonlocal n_1m
        t = by_id.get(theme_id)
        td = dict_by_id.get(theme_id)
        if not t or not td:
            return False
        # the theme's own registered exposures, in order
        for exposure in td["exposures"]:
            for key in pool.get(exposure, []):
                if key in used:
                    continue
                q = quotes.get(key)
                sig = q.get("sigma_6m_pct" if horizon == "6个月" else "sigma_1m_pct")
                if not sig or sig <= 0:
                    continue
                used.add(key)
                _mk(t, td, exposure, key, q, sig, horizon)
                if horizon == "1个月":
                    n_1m += 1
                return True
        return False

    def _mk(t, td, exposure, key, q, sig, horizon):
        sh = _shape(t["tis"], t.get("m"), t.get("c"))
        action, mult = _action(t.get("m"), t.get("c"))
        close = q["close"]
        f = sig / 100.0
        f1 = (q.get("sigma_1m_pct") or sig) / 100.0

        sid = f"{td['id']}-{'1M' if horizon == '1个月' else '6M'}-{_slug(exposure)}"
        trid = f"{td['id']}-TR"
        transmissions.setdefault(trid, {"id": trid, "theme_id": td["id"],
                                        "label": td["key_question"][:60]})
        signals.setdefault(sid, {"id": sid, "theme_id": td["id"],
                                 "transmission_id": trid, "asset": exposure,
                                 "direction": "↑", "horizon": horizon})
        # cite this theme's own evidence
        srcs = [e["doc_id"] for e in (t.get("evidence") or [])[:2] if e.get("doc_id")]

        idea: dict[str, Any] = {
            "id": len(ideas) + 1,
            "instrument_key": key, "tool": key, "tool_desc": exposure,
            "theme_id": td["id"], "theme": t["label"], "signal_id": sid,
            "asset": exposure, "direction": "↑",
            "horizon": horizon, "action": action,
            "ref_price": close, "ref_price_d": q["close_d"],
            "central": {"p": sh["p"],
                        "r": [round(sh["k_up"] * sig, 2),
                              round(sh["k_base"] * sig, 2),
                              round(sh["k_dn"] * sig, 2)]},
            "conservative": {
                "p": [max(sh["p"][0] - 6, 14), 0, min(sh["p"][2] + 5, 34)],
                "r": [round(sh["k_up"] * 0.65 * sig, 2),
                      round(sh["k_base"] * 0.5 * sig, 2),
                      round(sh["k_dn"] * 1.25 * sig, 2)]},
            "pos_init": round(max(0.4, min(2.5, 1.4 * mult)), 2),
            "pos_max": round(max(0.8, min(5.0, 2.8 * mult)), 2),
            "entry_src": "formula", "take_src": "formula", "stop_src": "formula",
            "sources": srcs or ["(无当日证据)"],
            "view": (f"{t['label']}：TIS {t['tis']:.1f}，入价 M {_n(t.get('m'))}"
                     f"（{t['factors'].get('stage','')}），拥挤 C {_n(t.get('c'))}"
                     f"（{t['factors'].get('crowding','')}）→ {action}"),
            "thesis": _thesis(t, td, exposure, key, q, horizon, action, mult),
            "fit": (f"{horizon}内由预注册指标 {td['price_indicator']} 与标的自身价格"
                    f"共同验证；σ={sig:.2f}%，上行 {sh['k_up']:.2f}σ，"
                    f"下行 {abs(sh['k_dn']):.2f}σ。"),
            "risk": _risk(t, td, action),
            "role": f"{t['label']} · {exposure}",
        }
        idea["conservative"]["p"][1] = (100 - idea["conservative"]["p"][0]
                                        - idea["conservative"]["p"][2])

        if action == "等待突破":
            idea["entry_break"] = round(close * (1 + 0.60 * f1), 4)
        elif action == "可执行":
            idea["entry_lo"] = round(close * (1 - 0.45 * f1), 4)
            idea["entry_hi"] = round(close * (1 + 0.10 * f1), 4)
        else:
            idea["entry_lo"] = round(close * (1 - 1.10 * f1), 4)
            idea["entry_hi"] = round(close * (1 - 0.40 * f1), 4)
        idea["take_lo"] = round(close * (1 + 0.85 * sh["k_up"] * f), 4)
        idea["take_hi"] = round(close * (1 + sh["k_up"] * f), 4)
        # the stop is set on the 1-month sigma even for 6-month ideas: it has to be
        # a level the position can be managed against day to day
        idea["stop_px"] = round(close * (1 - 1.60 * f1), 4)
        ideas.append(idea)

    # ---- fill the quota, alternating horizon to hit the 1-month band
    order = sorted(quota.items(), key=lambda kv: -(by_id[kv[0]]["tis"] or 0))
    for theme_id, n in order:
        for k in range(n):
            if len(ideas) >= N_IDEAS:
                break
            want = "1个月" if (n_1m < TARGET_1M and k % 2 == 0) else "6个月"
            if not emit(theme_id, want):
                emit(theme_id, "6个月" if want == "1个月" else "1个月")
        if len(ideas) >= N_IDEAS:
            break

    # ---- top up from any remaining theme/exposure until exactly 40
    if len(ideas) < N_IDEAS:
        for t in sorted(themes, key=lambda x: -(x["tis"] or 0)):
            while len(ideas) < N_IDEAS:
                want = "1个月" if n_1m < TARGET_1M else "6个月"
                if not emit(t["theme_id"], want):
                    break
            if len(ideas) >= N_IDEAS:
                break

    for i, x in enumerate(ideas, 1):
        x["id"] = i

    return {
        "schema": "ideagen40/batch/1",
        "as_of": pack["as_of"], "pack_sha": pack["pack_sha"],
        "note": f"规则化生成（{GENERATOR}）。",
        "macro_narrative": _narrative(pack, themes, ideas),
        "transmissions": list(transmissions.values()),
        "signals": list(signals.values()),
        "ideas": ideas,
    }


# ---------------------------------------------------------------- prose
def _n(v) -> str:
    return "—" if v is None else f"{v:.0f}"


def _slug(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "", s)[:10].upper() or "X"


def _thesis(t, td, exposure, key, q, horizon, action, mult) -> str:
    """Why this rule fired. Short. No reciting factor scores back at the reader —
    those are already in the table above, and repeating them in prose is noise."""
    m, c = t.get("m") or 0, t.get("c") or 0
    if c >= 80:
        why = "价格已在自身一年分布的极值附近，只等回踩，仓位砍半。"
    elif m >= 80:
        why = "已经被充分交易，只留观察仓，等回撤或找没补涨的第二层。"
    elif m < 30 and c < 60:
        why = "证据够厚而价格还没反映，可以直接买。"
    elif m < 30:
        why = "价格没反映但已经偏挤，要它先突破证明自己。"
    else:
        why = "既不便宜也不挤，等价格给个确认再进。"
    dist = _pct(q.get("from_52w_high"))
    mom = _n(q.get("mom_pct_60d"))
    return (f"赌的是：{td['key_question']}。{why}"
            f"用 {key} 表达（{exposure}），现价 {q['close']}，距 52 周高点 {dist}，"
            f"60 日动量 {mom} 百分位。规则引擎生成。")


def _risk(t, td, action) -> str:
    r = [f"最可能错在：{td['key_question']}的答案转向，"
         f"或 {td['price_indicator']} 与本条方向持续背离。"]
    if (t.get("c") or 0) >= 70:
        r.append("而且已经偏挤，回撤没缓冲。")
    if (t.get("b") or 0) < 20:
        r.append("证据几乎一边倒，可能是共识而非预期差。")
    if t.get("confidence") != "ok":
        r.append("该主题当日低置信。")
    return "".join(r)


def _pct(v) -> str:
    return "—" if v is None else f"{v*100:+.1f}%"


def _narrative(pack, themes, ideas) -> str:
    """Three sentences: what to do today, what is off-limits, what the batch is.

    Deliberately not a recital of factor values. Those are in the table, and
    restating them in prose reads as filler.
    """
    lead = [t for t in themes[:8] if (t.get("m") or 50) < 30 and (t.get("c") or 50) < 60]
    crowded = [t for t in themes[:8] if (t.get("c") or 0) >= 80]
    mature = [t for t in themes[:8] if (t.get("m") or 0) >= 80]
    top = themes[0]["label"] if themes else "—"

    bits = []
    if lead:
        bits.append("可以直接买的是 " + "、".join(t["label"] for t in lead[:3])
                    + "——证据够厚而价格还没反映。")
    else:
        bits.append(f"今天没有「证据厚 + 价格未反映」的组合，冲击最高的是 {top}，"
                    f"但价格已经在动，所以全部等确认。")
    off = [t["label"] for t in crowded[:3]] + [t["label"] for t in mature[:2]]
    if off:
        bits.append("要回避追高的是 " + "、".join(dict.fromkeys(off))
                    + "——降级为等回踩、仓位砍半。")
    n1 = sum(1 for i in ideas if i["horizon"] == "1个月")
    ex = sum(1 for i in ideas if i["action"] == "可执行")
    bits.append(f"{len(ideas)} 条：{n1} 条一个月，{ex} 条可直接执行，"
                f"其余等回踩或突破。规则引擎生成，用于补齐每日历史。")
    return "".join(bits)


# ---------------------------------------------------------------- entry point
def generate(con, as_of: date, write: bool = True, verbose: bool = True,
             price_asof=None, rebuild_pack: bool = False) -> dict:
    from . import briefing

    path = config.BRIEFINGS / f"briefing_{as_of.isoformat()}.json"
    if rebuild_pack or not path.exists():
        pack = briefing.build(con, as_of, verbose=False, price_asof=price_asof)
    else:
        pack = json.loads(path.read_text(encoding="utf-8"))
    payload = build_payload(pack)
    if write:
        out = config.BATCHES / f"batch_{as_of.isoformat()}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    if verbose:
        n1 = sum(1 for i in payload["ideas"] if i["horizon"] == "1个月")
        acts: dict[str, int] = {}
        for i in payload["ideas"]:
            acts[i["action"]] = acts.get(i["action"], 0) + 1
        print(f"    generated {len(payload['ideas'])} ideas  1个月={n1}  {acts}")
    return payload
