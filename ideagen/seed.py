"""Import an explicitly supplied historical idea pack.

The pack states entry / take-profit / stop as prose ("$73–76分两笔",
"日收盘低于$69.50", "较最新NAV回撤3%–5%"). `parse_levels` recovers the numeric
levels; every value it recovers is written into the idea row with an explicit
`entry_src` tag so the worksheet requirement in 方法论 §4 is preserved.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from . import config, db, ideas as ideas_mod, universe

SEED_PACK = Path(os.environ.get(
    "IDEAGEN_SEED_PACK", config.SEED / "customer_seed_pack.json"))
SEED_AS_OF = date.fromisoformat(
    os.environ.get("IDEAGEN_SEED_AS_OF", "2026-07-27"))

# Customer-specific aliases belong beside the supplied pack, not in source.
TOOL_ALIAS: dict[str, str] = {}

# Optional human-verified overrides belong to the supplied customer pack.
VERIFIED: dict[str, dict[str, Any]] = {}

_MONEY = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[–\-~至]\s*(\d+(?:\.\d+)?)")
_PCT_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*[–\-~至]\s*(\d+(?:\.\d+)?)\s*%")
_PCT_ONE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _ref_price(pack_price: str | None) -> float | None:
    if not pack_price:
        return None
    m = _MONEY.search(pack_price) or re.search(r"(\d+(?:\.\d+)?)", pack_price)
    return float(m.group(1)) if m else None


def parse_levels(text: str, ref: float | None, kind: str) -> dict:
    """Recover numeric levels from the pack's prose.

    `kind` is entry | take | stop and selects the sign convention for
    percentage-relative phrasing ("回撤3%–5%" is below ref; "+5%–7%" is above).
    """
    out: dict[str, Any] = {"lo": None, "hi": None, "src": "research_judgment"}
    if not text:
        return out

    mr = _RANGE.search(text.replace("$", ""))
    money = [float(x) for x in _MONEY.findall(text)]

    if len(money) >= 2:
        out.update(lo=min(money[:2]), hi=max(money[:2]), src="formula")
        return out
    if mr and not _PCT_RANGE.search(text):
        a, b = float(mr.group(1)), float(mr.group(2))
        # A plausible price range, not a percentage pair.
        if ref is None or (0.5 * ref <= a <= 1.8 * ref):
            out.update(lo=min(a, b), hi=max(a, b), src="formula")
            return out
    if len(money) == 1:
        out.update(lo=money[0], hi=None, src="formula")
        return out

    if ref is not None:
        pr = _PCT_RANGE.search(text)
        if pr:
            p1, p2 = float(pr.group(1)) / 100, float(pr.group(2)) / 100
            if kind == "entry":
                out.update(lo=round(ref * (1 - max(p1, p2)), 4),
                           hi=round(ref * (1 - min(p1, p2)), 4), src="hybrid")
            else:
                out.update(lo=round(ref * (1 + min(p1, p2)), 4),
                           hi=round(ref * (1 + max(p1, p2)), 4), src="hybrid")
            return out
        p1m = _PCT_ONE.search(text)
        if p1m:
            p = float(p1m.group(1)) / 100
            sign = -1 if kind in ("entry", "stop") else 1
            out.update(lo=round(ref * (1 + sign * p), 4), hi=None, src="hybrid")
            return out
    return out


def _to_generator_shape(x: dict, note: dict) -> dict:
    tool = (x.get("tool") or "").strip()
    key = TOOL_ALIAS.get(tool.upper(), TOOL_ALIAS.get(tool, tool))
    inst = universe.resolve(key, register_unknown_as="fund")
    ref = _ref_price(x.get("price"))

    v = VERIFIED.get(inst.key if inst else key, {})
    e = parse_levels(x.get("entry", ""), ref, "entry")
    t = parse_levels(x.get("take", ""), ref, "take")
    s = parse_levels(x.get("stop", ""), ref, "stop")

    entry_lo, entry_hi = v.get("entry", (e["lo"], e["hi"]))
    take_lo, take_hi = v.get("take", (t["lo"], t["hi"]))
    stop_px = v["stop"] if "stop" in v else s["lo"]

    for label, parsed, verified in (("entry", (e["lo"], e["hi"]), v.get("entry")),
                                    ("take", (t["lo"], t["hi"]), v.get("take")),
                                    ("stop", (s["lo"], None),
                                     (v.get("stop"), None) if "stop" in v else None)):
        if verified is not None and parsed != tuple(verified):
            note.setdefault("level_divergence", []).append(
                {"tool": tool, "field": label, "parsed": parsed,
                 "verified": list(verified)})

    pos_init, pos_max = _parse_position(x.get("position", ""))
    return {
        "id": int(x["id"]),
        "tool": inst.key if inst else key,
        "instrument_key": inst.key if inst else key,
        "tool_desc": x.get("toolDesc"),
        "vehicle": x.get("vehicle"),
        "theme": x.get("theme"),
        "signal_id": x.get("assetSignalId"),
        "asset": x.get("asset") or (inst.exposure if inst else None),
        "direction": x.get("direction") or "↑",
        "horizon": x.get("horizon"),
        "action": x.get("action"),
        "ref_price": ref, "ref_price_d": SEED_AS_OF.isoformat(),
        "entry_lo": entry_lo, "entry_hi": entry_hi,
        "entry_break": v.get("breakout"),
        "take_lo": take_lo, "take_hi": take_hi, "stop_px": stop_px,
        "entry_src": e["src"], "take_src": t["src"], "stop_src": s["src"],
        "hurdle": x.get("hurdle"),
        "central": x["central"], "conservative": x["conservative"],
        "pos_init": pos_init, "pos_max": pos_max,
        "view": x.get("view"), "thesis": x.get("thesis"), "fit": x.get("fit"),
        "risk": x.get("risk"), "role": x.get("role"),
        "sources": [x.get("source")] if x.get("source") else [],
        "_pack": {"entry": x.get("entry"), "take": x.get("take"),
                  "stop": x.get("stop"), "price": x.get("price"),
                  "position": x.get("position"), "product": x.get("product")},
    }


def _parse_position(text: str) -> tuple[float | None, float | None]:
    """'初始1.5%，上限3%' -> (1.5, 3.0)"""
    if not text:
        return None, None
    pcts = [float(m) for m in _PCT_ONE.findall(text)]
    if not pcts:
        return None, None
    if len(pcts) == 1:
        return pcts[0], pcts[0]
    return pcts[0], max(pcts)


def import_pack(con, path: Path | None = None, as_of: date = SEED_AS_OF,
                verbose: bool = True) -> tuple[str, list[dict], dict]:
    path = path or SEED_PACK
    pack = json.loads(path.read_text(encoding="utf-8"))
    raw_ideas = pack["updatedIdeas"]
    note: dict[str, Any] = {}
    payload = {
        "ideas": [_to_generator_shape(x, note) for x in raw_ideas],
        "note": "historical PM pack 2026-07-27, imported verbatim as batch #1",
        "prompt_sha": None,
    }
    batch_id, rows, report = ideas_mod.build_batch(
        con, payload, as_of, generator="seed-import:PM-pack-2026-07-27",
        batch_id="B20260727",
        # The pack's worksheet is stamped 2026-07-27 10:16 HKT, i.e. before the
        # 2026-07-27 US session opened, so that session's close is legitimately
        # the first fillable bar.
        generated_at="2026-07-27T10:16:00+08:00")

    # Persist the pack's own theme/signal registry for the theme map.
    sig_rows = [{"as_of": as_of.isoformat(), "signal_id": s["id"],
                 "theme_id": s["theme"], "transmission_id": None,
                 "asset": s.get("exposure"), "direction": s.get("direction", "↑"),
                 "horizon": s.get("horizon", "1个月"), "gate": s.get("gate"),
                 "price_indicator": None}
                for s in pack.get("themeSignalRegistry", [])]
    tr_rows = [{"as_of": as_of.isoformat(), "transmission_id": t["id"],
                "theme_id": t["theme"], "label": t["label"]}
               for t in pack.get("themeTransmissionRegistry", [])]
    with db.tx(con):
        db.upsert_many(con, "signals", sig_rows, ["as_of", "signal_id"])
        db.upsert_many(con, "transmissions", tr_rows, ["as_of", "transmission_id"])

    if note:
        db.kv_set(con, "seed:level_divergence", note)
    if verbose:
        s = report["summary"]
        print(f"  seed batch {batch_id}: {s['n']} ideas  grades={s['grades']}  "
              f"kinds={s['kinds']}  horizons={s['horizons']}")
        print(f"  validation pass={report['pass']} errors={report['n_errors']} "
              f"warnings={report['n_warnings']}")
        for c in report["checks"]:
            if not c["ok"]:
                print(f"    [{c['severity']}] {c['check']}: "
                      f"{json.dumps(c['detail'], ensure_ascii=False)[:160]}")
        if note.get("level_divergence"):
            print(f"  level divergences (parser vs verified table): "
                  f"{len(note['level_divergence'])}")
    return batch_id, rows, report


# The worksheet's stated inputs and results, transcribed from
# 40个交易想法_情景评分计算底稿_赔率展开版_2026-07-27_1016_HKT.md §2.
# Each row is annotated "原页面核对 … 一致" in the source document.
WORKSHEET = {
    21: {"tool": "P/E FX", "cp": [30, 45, 25], "cr": [10.0, 2.0, -6.0],
         "kp": [20, 50, 30], "kr": [6.0, 1.0, -7.0], "h": 0.35,
         "or_c": 2.29, "or_k": 0.66},
    29: {"tool": "KRE", "cp": [35, 40, 25], "cr": [10.0, 3.0, -7.0],
         "kp": [30, 45, 25], "kr": [7.0, 2.0, -8.0], "h": 0.31,
         "or_c": 2.44, "or_k": 1.33},
    4:  {"tool": "XLE", "cp": [25, 45, 30], "cr": [7.0, 1.5, -9.0],
         "kp": [20, 45, 35], "kr": [4.0, 0.0, -11.0], "h": 0.31,
         "or_c": 0.79, "or_k": 0.18},
    9:  {"tool": "QUAL", "cp": [35, 45, 20], "cr": [12.0, 6.0, -10.0],
         "kp": [25, 50, 25], "kr": [8.0, 3.0, -13.0], "h": 2.02,
         "or_c": 2.20, "or_k": 0.53},
    7:  {"tool": "PAVE", "cp": [35, 45, 20], "cr": [15.0, 5.0, -13.0],
         "kp": [25, 45, 30], "kr": [8.0, 1.0, -17.0], "h": 2.02,
         "or_c": 1.96, "or_k": 0.24},
}


def verify_worksheet(con, batch_id: str = "B20260727") -> dict:
    """Audit the odds worksheet against the HTML it claims to reproduce.

    Three comparisons, so the source of any gap is unambiguous:

      * `worksheet_selfcheck` — recompute the worksheet's *own* stated inputs
        with v0.3 §5. If this agrees, the worksheet's arithmetic is sound and any
        remaining gap is an input mismatch, not a formula error.
      * `html_gross` — the same formula applied to the inputs actually present in
        `updatedIdeas`, with no cost deduction, i.e. exactly v0.3's convention.
      * `html_net` — what this system stores, which additionally charges the
        round-trip cost v0.3 omits.
    """
    rows = {r["local_id"]: r for r in ideas_mod.load_batch(con, batch_id)}
    out = []
    for lid, w in WORKSHEET.items():
        r = rows.get(lid)
        if not r:
            continue
        self_c = ideas_mod.odds(w["cp"], w["cr"], w["h"])
        self_k = ideas_mod.odds(w["kp"], w["kr"], w["h"])
        gross_c = ideas_mod.odds(r["central_p"], r["central_r"], r["hurdle"])
        gross_k = ideas_mod.odds(r["conserv_p"], r["conserv_r"], r["hurdle"])
        inputs_match = (list(map(float, w["cp"])) == list(map(float, r["central_p"]))
                        and list(map(float, w["cr"])) == list(map(float, r["central_r"])))
        out.append({
            "local_id": lid, "tool": w["tool"],
            "worksheet_or_c": w["or_c"], "worksheet_or_k": w["or_k"],
            "worksheet_selfcheck_or_c": self_c["or"],
            "worksheet_selfcheck_or_k": self_k["or"],
            "worksheet_arithmetic_ok": (abs((self_c["or"] or 0) - w["or_c"]) < 0.02
                                        and abs((self_k["or"] or 0) - w["or_k"]) < 0.02),
            "worksheet_inputs": {"cp": w["cp"], "cr": w["cr"]},
            "html_inputs": {"cp": r["central_p"], "cr": r["central_r"]},
            "inputs_match": inputs_match,
            "html_gross_or_c": gross_c["or"], "html_gross_or_k": gross_k["or"],
            "html_net_or_c": r["or_c"], "html_net_or_k": r["or_k"],
        })
    bad_inputs = [o for o in out if not o["inputs_match"]]
    bad_math = [o for o in out if not o["worksheet_arithmetic_ok"]]
    return {
        "checked": len(out),
        "worksheet_arithmetic_failures": len(bad_math),
        "input_mismatches": len(bad_inputs),
        "verdict": (
            "worksheet arithmetic is internally consistent; its probability/return "
            "inputs differ from the HTML it claims to reproduce"
            if not bad_math and bad_inputs else
            "worksheet arithmetic itself does not reproduce its stated results"
            if bad_math else "worksheet agrees with HTML"),
        "rows": out,
    }
