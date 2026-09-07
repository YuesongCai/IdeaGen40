"""Re-run stage C for chosen arms over a stored period's markable pool.

Why this exists (2026-09-07, Jon: 「为啥 AI 端到端选取没数」): in every one of
the six re-run periods the AI end-to-end selector picked ten shelf funds, and
this node has no NAV series for any shelf fund, so booking dropped all ten and
the book stayed empty — likewise the two source-restricted arms (their
generators propose funds only) and the momentum control (no candidate prices
at the time). The live orchestrator now hands stage C only the markable pool
and prices the candidates (see `orchestrator._markable_candidates`,
`_candidate_prices`), but the stored verdicts of those six periods were made
under the old rules.

This replays stage C for named arms on a stored run: same candidate payloads
the run persisted (narrowed to what a book can hold), the same price view the
live run would build today, the same `strategy.run` dispatch. The verdict row
is replaced (`verdicts` is keyed by run/kind/strategy) and the `C_selectors`
artifact rewritten; the journal keeps its original steps, and a `reselect`
note is written beside them so the run's own record says what was redone and
when. The pool (`candidates`) is not touched.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from . import db, orchestrator as orch, platform as plat, schema
from . import strategy as strat


def _runs(state, start: str | None, end: str | None) -> list[dict[str, Any]]:
    rows = state.q("SELECT run_id, as_of, data_classification FROM orch_runs "
                   "WHERE kind='weekly' AND ok=1 ORDER BY as_of, ended_at DESC")
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        r = dict(r)
        if start and r["as_of"] < start:
            continue
        if end and r["as_of"] > end:
            continue
        seen.setdefault(r["as_of"], r)
    return [seen[k] for k in sorted(seen)]


def reselect(p: plat.Platform, *, arms: list[str], start: str | None = None,
             end: str | None = None, params: dict[str, Any] | None = None,
             verbose: bool = True) -> dict[str, Any]:
    log = print if verbose else (lambda *a: None)
    runs = _runs(p.state, start, end)
    if not runs:
        raise ValueError("窗口内没有成功完成的周跑")
    specs = {a: strat.spec("idea_selector", a) for a in arms}
    out: dict[str, Any] = {"runs": {}, "arms": arms}
    for r in runs:
        as_of = date.fromisoformat(r["as_of"])
        rid = r["run_id"]
        cands = [db.jl(c["payload"], {}) for c in p.state.q(
            "SELECT candidate_id, payload FROM candidates WHERE run_id=? "
            "ORDER BY candidate_id", (rid,))]
        cands = [c for c in cands if c]
        markable_ids, unmark = orch._markable_candidates(p, as_of, cands, False)
        pool = [c for c in cands if str(c.get("id")) in markable_ids]
        prices, psumm = orch._price_inputs(p, as_of, None, False)
        extra, csumm = orch._candidate_prices(p, as_of, pool, prices, False)
        prices = {**prices, **extra}
        csha = strat.RunContext.sha([c.get("id") for c in pool])
        ctx = strat.RunContext(as_of=as_of, inputs_sha=csha, candidates=pool,
                               prices=prices, params=dict(params or {}),
                               infer=getattr(p, "inference", None))
        rec: dict[str, Any] = {"pool": len(cands), "markable": len(pool),
                               "unmarkable": len(unmark), "prices": psumm.get("measured"),
                               "candidate_prices": csumm.get("measured"), "arms": {}}
        log(f"{r['as_of']} run {rid}: 池 {len(cands)} · 可盯市 {len(pool)} · "
            f"行情 {psumm.get('measured')}+{csumm.get('measured')}")
        for a in arms:
            try:
                v = strat.run("idea_selector", a, ctx)
            except Exception as e:  # noqa: BLE001 — one arm must not stop the rest
                rec["arms"][a] = {"error": f"{type(e).__name__}: {e}"}
                log(f"  ✗ {a}: {e}")
                continue
            orch._save_verdict(p, rid, ctx, "idea_selector", v, specs[a]["role"])
            try:
                p.blobs.put(f"runs/{r['as_of']}/{rid}/C_selectors/{a}.json",
                            json.dumps(v.as_row(ctx, "idea_selector"), ensure_ascii=False,
                                       default=str).encode("utf-8"),
                            content_type="application/json",
                            metadata={"run_id": rid, "kind": "weekly", "reselect": "1"})
            except Exception as e:  # noqa: BLE001 — the row is the record; the blob is a copy
                rec["arms"].setdefault(a, {})["artifact_error"] = str(e)[:120]
            rec["arms"][a] = {**rec["arms"].get(a, {}), "chosen": len(v.chosen),
                              "calls": v.calls, "error": v.meta.get("error")}
            log(f"  {'!' if v.meta.get('error') else ' '} {a:<26}{len(v.chosen):>4} 持仓"
                + (f"   {str(v.meta.get('error'))[:60]}" if v.meta.get("error") else ""))
        try:
            p.blobs.put(f"runs/{r['as_of']}/{rid}/reselect.json",
                        json.dumps({"at": plat.utcnow_iso(), "arms": arms,
                                    "why": "筛选C 改为只对可盯市候选选取并给候选配行情后，"
                                           "对已存期次重做这些组合的选取",
                                    **rec}, ensure_ascii=False, default=str).encode("utf-8"),
                        content_type="application/json",
                        metadata={"run_id": rid, "kind": "weekly"})
        except Exception:  # noqa: BLE001
            pass
        out["runs"][r["as_of"]] = rec
    return out
