"""Booking: turn one weekly run's verdicts into paper positions, one book per 挑法.

This is the step that closes the loop. The orchestrator ends at "each selector
produced its list"; until those lists become orders on a book, the system stores
opinions, not evidence — a month from now there would be no realised returns to
compare the selectors on, and the whole parallel-book design would have measured
nothing.

Each selector books into its own paper book (`sel-<name>`), all at the same
capital, all equal-weight, all entering at the first fillable close. Holding the
execution rules identical across books is the same discipline as the shared
candidate pool: the only thing allowed to differ between two books is which ideas
they hold, so a return difference can only come from selection.

Booking goes through the existing `ideas.build_batch` → `paper.open_batch` path
rather than writing orders directly. That path owns the invariants that were paid
for in blood — no same-bar look-ahead, costs on both legs, unmarkable instruments
excluded rather than zero-filled, traded batches refusing replacement — and a
parallel "simpler" order writer would slowly drift away from all of them.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from . import config, db, ideas as ideas_mod, paper
from .sources import futu_px

#: Stops and takes are σ-multiples of the instrument's own monthly volatility,
#: fixed at booking and never moved. σ×2 / σ×3 are the spec's baseline numbers:
#: a fixed percentage would mean a bond ETF never stops and a semiconductor ETF
#: stops on a normal week, which makes "how tight is the stop" a different
#: question per asset.
STOP_SIGMA = 2.0
TAKE_SIGMA = 3.0


def _priced_only(con, cands: list[dict[str, Any]],
                 as_of: date) -> tuple[list[dict[str, Any]], list[str]]:
    """Split candidates into those that can be priced on the date and those that cannot.

    A listed instrument with no close at or before `as_of` has no entry price,
    no sigma, and therefore no stop — there is nothing to book. Returning it
    alongside the good ones is what let a single unpriced ticker fail an
    entire batch.
    """
    from . import universe as uni
    from .sources import futu_px

    ok: list[dict[str, Any]] = []
    bad: list[str] = []
    for c in cands:
        token = str(c.get("instrument_id") or "")
        hit = uni.resolve(token)
        code = getattr(hit, "futu_code", None) if hit else None
        if code and futu_px.last_close_on_or_before(con, code, as_of.isoformat()):
            ok.append(c)
        else:
            bad.append(token)
    return ok, bad


def payload_from_candidates(cands: list[dict[str, Any]],
                            run_id: str | None = None) -> dict:
    """Translate stage-B candidates into the shape `ideas.compute` expects.

    The translation is mechanical on purpose. Anything invented here — a haircut,
    a default band, an assumed direction — would be a judgement the generators
    never made, silently attributed to them. The conservative worksheet is set
    equal to the central one for the same reason: the generators stated one set of
    odds, and fabricating a second, more cautious set would be fake prudence.
    """
    ideas = []
    for n, c in enumerate(cands, 1):
        up, dn = float(c["upside_pct"]), float(c["downside_pct"])
        # Stage B stores probabilities as fractions (0-1); the legacy idea
        # contract speaks percentages (0-100). The odds ratio is scale-free so a
        # mismatch never showed up in numbers — it showed up as a validation
        # refusal, which is exactly what the contract check is for.
        ps = [round(float(c["p_up"]) * 100, 1), round(float(c["p_base"]) * 100, 1),
              round(float(c["p_down"]) * 100, 1)]
        # Independent rounding leaves the sum at 99.9 or 100.1 often enough to
        # trip the sum-to-100 contract. The残差 goes to the largest scenario,
        # where a tenth of a point distorts the stated view least.
        drift = round(100.0 - sum(ps), 1)
        if abs(drift) >= 0.1:
            ps[ps.index(max(ps))] = round(max(ps) + drift, 1)
        ideas.append({
            "id": n,
            "instrument_key": str(c["instrument_id"]),
            "tool": str(c["instrument_id"]),
            "theme_id": c.get("topic_id"), "theme": c.get("topic_id"),
            "direction": "↑", "horizon": "1个月",
            "action": "可执行",                    # market_close entry downstream
            "central":      {"p": ps, "r": [up, 0.0, dn]},
            "conservative": {"p": ps, "r": [up, 0.0, dn]},
            "thesis": (c.get("thesis") or "")[:600],
            "role": c.get("method"),
            # Hard lineage, not inference: a position must be traceable to the
            # exact run and candidate that produced it by following stored ids,
            # never by matching dates and hoping. This is what makes the chain
            # 持仓 → 想法 → 候选 → 运行 → 语料 walkable in both directions.
            "sources": [{"provenance": "weekly-run",
                         "run_id": run_id,
                         "candidate_id": c.get("id"),
                         "methods": c.get("proposed_by") or [c.get("method")],
                         "n_proposals": c.get("n_proposals", 1),
                         "citations": c.get("citations") or []}],
        })
    return {"ideas": ideas}


def _fix_stops(con, batch_id: str) -> int:
    """Fill σ-multiple stops and takes on every listed idea of the batch.

    Done as a single pass after `build_batch` because σ is only known once
    `compute()` has resolved the reference price and horizon volatility. An idea
    whose σ cannot be computed keeps no stop rather than a guessed one — the
    monitor reports it, and a guessed stop level would be exercised with real
    (paper) money.
    """
    n = 0
    for r in db.q(con, "SELECT idea_uid, ref_price, sigma_h FROM ideas "
                       "WHERE batch_id=? AND stop_px IS NULL "
                       "AND ref_price IS NOT NULL AND sigma_h IS NOT NULL",
                  (batch_id,)):
        sig = float(r["sigma_h"]) / 100.0
        px = float(r["ref_price"])
        con.execute(
            "UPDATE ideas SET stop_px=?, take_lo=?, stop_src=?, take_src=? "
            "WHERE idea_uid=?",
            (round(px * (1 - STOP_SIGMA * sig), 6),
             round(px * (1 + TAKE_SIGMA * sig), 6),
             f"σ×{STOP_SIGMA:g}", f"σ×{TAKE_SIGMA:g}", r["idea_uid"]))
        n += 1
    con.commit()
    return n


def book_run(con, p, run_id: str, *, selectors: list[str] | None = None,
             verbose: bool = True) -> dict[str, Any]:
    """Book every selector verdict of one orchestrator run into paper books.

    Idempotent at two levels: a batch that already exists is not rebuilt, and
    `paper.open_batch` refuses to re-place a traded batch. Running this twice —
    a retried tick, a manual re-run after a crash — therefore cannot double a
    position, which is the property the whole scheduling layer is built around.
    """
    log = print if verbose else (lambda *a: None)
    run = p.state.q("SELECT run_id, as_of, ok FROM orch_runs WHERE run_id=?",
                    (run_id,))
    if not run:
        raise KeyError(f"没有这个 run：{run_id}")
    if not run[0]["ok"]:
        raise ValueError(f"run {run_id} 未成功完成（ok=0），它的选择不能拿去建仓")
    as_of = date.fromisoformat(run[0]["as_of"])

    cands = {str(r["candidate_id"]): json.loads(r["payload"]) for r in p.state.q(
        "SELECT candidate_id, payload FROM candidates WHERE run_id=?", (run_id,))}
    verdicts = p.state.q(
        "SELECT strategy, chosen FROM verdicts WHERE run_id=? AND kind='idea_selector'",
        (run_id,))
    if not cands or not verdicts:
        raise ValueError(f"run {run_id} 没有候选或没有挑法结论，无仓可建")

    out: dict[str, Any] = {"run_id": run_id, "as_of": as_of.isoformat(), "books": {}}
    for v in verdicts:
        name = v["strategy"]
        if selectors is not None and name not in selectors:
            continue
        chosen = [cands[c] for c in json.loads(v["chosen"]) if c in cands]
        # An instrument with no price series cannot be entered, and the batch
        # validator rightly refuses it — but it refused the whole batch, so one
        # unpriced ticker cost a book its entire week (US.XLF did exactly this
        # to five books across the 2026-08-12 and 08-19 backfills: ten good
        # picks discarded because an eleventh had no rows in `prices`). Drop
        # the ones that cannot be priced, name them, and book the rest.
        chosen, unpriced = _priced_only(con, chosen, as_of)
        if unpriced:
            log(f"  ⚠ {name}: {len(unpriced)} 个标的当日无价，无法建仓（其余照常）："
                + "、".join(unpriced[:6]))
        if not chosen:
            out["books"][name] = {
                "skipped": ("该挑法选中的标的当日都没有价格" if unpriced
                            else "该挑法没有选中任何想法"),
                **({"unpriced": unpriced} if unpriced else {})}
            continue

        batch_id = f"W{as_of.isoformat().replace('-','')}-{name}"
        exists = db.q1(con, "SELECT 1 x FROM batches WHERE batch_id=?", (batch_id,))
        if not exists:
            _, rows, val = ideas_mod.build_batch(
                con, payload_from_candidates(chosen, run_id=run_id), as_of,
                generator=f"weekly:{run_id}", batch_id=batch_id)
            if not (val or {}).get("pass", False):
                out["books"][name] = {"error": f"批次校验未过：{val}"}
                log(f"  ✗ {name}: 校验未过")
                continue
            fixed = _fix_stops(con, batch_id)
            log(f"  批次 {batch_id}: {len(rows)} 条想法，σ 止损止盈补齐 {fixed} 条")

        book_id = config.selector_book(name)
        db.upsert(con, "books", {
            "book_id": book_id, "label": f"挑法 · {name}",
            "descr": config.SELECTOR_SPEC["desc"],
            "capital": config.SELECTOR_SPEC["capital"],
            "sizing": config.SELECTOR_SPEC["sizing"],
            "entry": config.SELECTOR_SPEC["entry"],
            "created_at": config.now_hkt().isoformat()}, ["book_id"])
        # Cross-tranche concentration is recorded before the orders go on. Four
        # weekly tranches roll side by side, so an instrument selected four weeks
        # running quietly stacks four positions in one book — "ten positions"
        # then overstates the diversification the same way ten rows on six
        # instruments once did within a single week. Recording is not capping:
        # re-selecting a working momentum name may be exactly right, but it has
        # to be a visible fact, not a surprise in a drawdown.
        held = {r["code"] for r in db.q(
            con, "SELECT DISTINCT code FROM positions WHERE book_id=? "
                 "AND status='open'", (book_id,))}
        incoming = {r["futu_code"] or r["olive_key"] for r in db.q(
            con, "SELECT futu_code, olive_key FROM ideas WHERE batch_id=?",
            (batch_id,))}
        overlap = sorted(c for c in (held & incoming) if c)
        if overlap:
            log(f"  ⚠ {name}: {len(overlap)} 个标的与在手仓位重叠（敞口叠加）："
                f"{', '.join(overlap[:6])}")

        try:
            rep = paper.open_batch(con, batch_id, book_id, verbose=False)
        except ValueError as e:
            # A traded batch refusing re-placement is the idempotency working,
            # not a failure of this call.
            if "force=True" in str(e):
                out["books"][name] = {"already_traded": True}
                log(f"  = {name}: 本期已建仓，跳过")
                continue
            raise
        last = futu_px.complete_through("US")
        paper.run(con, book_id, as_of.isoformat(), last, verbose=False)
        pos = db.q1(con, "SELECT COUNT(*) n FROM positions WHERE book_id=? "
                         "AND status='open'", (book_id,))
        out["books"][name] = {"batch": batch_id, "orders": rep.get("placed"),
                              "open_positions": pos["n"],
                              "cross_tranche_overlap": overlap}
        log(f"  ✓ {name:<14} 下单 {rep.get('placed')} 张，当前持仓 {pos['n']}")
    return out
