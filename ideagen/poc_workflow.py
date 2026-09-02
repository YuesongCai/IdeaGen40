"""Explicit-source weekly and backtest runners for the BytePlus POC.

Weekly modes can use a versioned demonstration pack or a persisted Wisburg
research window, always recording the exact corpus/shelf composition. The
backtest runner replays frozen weekly candidate pools through the production
engine with zero model calls.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import (backtest, config, db, orchestrator, platform as plat, poc_fixture,
               schema, shelf_store, strategy as strat)

CLASSIFICATION = "public-synthetic"
WEEKLY_CLASSIFICATION = "public-synthetic-live-model"
SHELF_FIXTURE_WEEKLY_CLASSIFICATION = (
    "public-synthetic-shelf-live-model"
)
OLIVE_LIVE_WEEKLY_CLASSIFICATION = (
    "public-synthetic-corpus+licensed-live-shelf-model"
)
WISBURG_SHELF_FIXTURE_WEEKLY_CLASSIFICATION = (
    "licensed-private-corpus+public-shelf-live-model"
)
WISBURG_OLIVE_LIVE_WEEKLY_CLASSIFICATION = (
    "licensed-private-corpus+licensed-live-shelf-model"
)
BACKTEST_METHODOLOGY = "mechanical-asof-replay/v1"
BACKTEST_ARMS = ("generated_ai_native", "generated_carl_constraint")
WEEKLY_MODES = (
    "public-synthetic",
    "shelf-fixture",
    "olive-live",
    "olive-auto",
    "wisburg-auto",
)

_THEME_MARKERS = (
    ("AI-CAPEX", "AI capex and data center infrastructure"),
    ("POLICY-PATH", "Fed rate cut and central bank liquidity"),
    ("TERM-PREMIUM", "term premium and Treasury auction supply"),
)


def public_inputs(as_of: date) -> dict[str, list[dict[str, Any]]]:
    """Rebase the versioned public fixture to one run date."""
    source = poc_fixture.read().document["inputs"]
    corpus = []
    for index, original in enumerate(source["corpus"]):
        theme_id, marker = _THEME_MARKERS[min(index // 4, 2)]
        published = as_of - timedelta(days=2 - index % 3)
        corpus.append({
            **original,
            "doc_id": f"SYN-{as_of:%Y%m%d}-{index + 1:03d}",
            "published_d": published.isoformat(),
            "title": f"{marker}: {original['title']}",
            "summary": (
                f"Public synthetic {theme_id} scenario. {marker}. "
                f"{original.get('summary') or ''}"
            ),
            "body": (
                f"This is a fabricated demonstration document for {theme_id}. "
                f"It discusses {marker} and contains no licensed research, "
                "client information, observed recommendation, or investment advice."
            ),
            "institution": original.get("institution") or "Public Scenario Lab",
            "tier": int(original.get("tier") or 3),
            "line": "public-synthetic",
        })

    universe = []
    for original in source.get("universe") or []:
        vehicle = ("ETF" if "ETF" in str(original.get("vehicle") or
                                          original.get("name") or "")
                   else "公募")
        universe.append({
            **original,
            "kind": "listed" if vehicle == "ETF" else "fund",
            "priceable": True,
            "currency": "USD",
            "vehicle": vehicle,
            "liquidity": "daily",
            "olive_key": (None if vehicle == "ETF"
                          else original["instrument_id"]),
            "first_seen_d": "2026-01-01",
        })

    calendar = []
    for index, original in enumerate(source.get("calendar") or []):
        calendar.append({
            **original,
            "event_id": f"SYN-{as_of:%Y%m%d}-EVENT-{index + 1}",
            "date": (as_of + timedelta(days=14 + index * 7)).isoformat(),
            "as_of": as_of.isoformat(),
            "feed": "public-synthetic-v1-calendar",
        })
    return {"corpus": corpus, "universe": universe, "calendar": calendar}


def classification_for_mode(mode: str) -> str:
    return {
        "public-synthetic": WEEKLY_CLASSIFICATION,
        "shelf-fixture": SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
        "olive-live": OLIVE_LIVE_WEEKLY_CLASSIFICATION,
    }[mode]


def weekly_kwargs(as_of: date, *, p: plat.Platform | None = None,
                  mode: str = "public-synthetic") -> dict[str, Any]:
    """Build one weekly input set without performing an MCP call.

    ``olive-live`` consumes the latest authorized snapshot already persisted in
    RDS/TOS. Capture and decision remain separate operations, which is what makes
    a retry reproducible and keeps an OAuth outage from changing a run mid-flight.
    """
    requested_mode = mode.strip().lower()
    if requested_mode not in WEEKLY_MODES:
        raise ValueError(
            f"unknown POC weekly mode {requested_mode!r}; expected {WEEKLY_MODES}")
    mode = requested_mode
    if mode == "olive-auto":
        if p is None:
            raise ValueError("olive-auto mode requires a platform")
        schema.migrate(p.state)
        live = shelf_store.latest_snapshot(
            p.state,
            as_of=as_of,
            classification=shelf_store.LIVE_CLASSIFICATION,
            source=shelf_store.LIVE_SOURCE,
        )
        mode = "olive-live" if live else "shelf-fixture"

    if mode == "wisburg-auto":
        if p is None:
            raise ValueError("wisburg-auto mode requires a platform")
        from . import cloud_corpus

        schema.migrate(p.state)
        corpus = cloud_corpus.corpus(p.state, as_of=as_of)
        if not corpus:
            raise RuntimeError(
                f"{as_of} has no persisted Wisburg corpus; run cloud-ingest "
                "before wisburg-auto"
            )
        live = shelf_store.latest_snapshot(
            p.state,
            as_of=as_of,
            classification=shelf_store.LIVE_CLASSIFICATION,
            source=shelf_store.LIVE_SOURCE,
        )
        if live:
            _, universe = shelf_store.universe(
                p.state,
                as_of=as_of,
                classification=shelf_store.LIVE_CLASSIFICATION,
                source=shelf_store.LIVE_SOURCE,
            )
            shelf_mode = "olive-live"
            classification = WISBURG_OLIVE_LIVE_WEEKLY_CLASSIFICATION
            source = "wisburg-mcp-rds+olive-mcp-rds"
        else:
            shelf_store.persist_fixture(p, as_of)
            _, universe = shelf_store.universe(
                p.state,
                as_of=as_of,
                classification=shelf_store.PUBLIC_FIXTURE_CLASSIFICATION,
            )
            shelf_mode = "shelf-fixture"
            classification = WISBURG_SHELF_FIXTURE_WEEKLY_CLASSIFICATION
            source = f"wisburg-mcp-rds+{shelf_store.PUBLIC_FIXTURE_SOURCE}"
        return {
            "corpus": corpus,
            "universe": universe,
            # There is no licensed calendar feed in this integration. An empty
            # feed is more honest than mixing demonstration events into a live
            # research run.
            "calendar": [],
            "generators": ["ai_native", "carl_constraint"],
            "selectors": ["buy_all", "spread"],
            "prices": {},
            "params": {
                "top_n": 3,
                "n": 6,
                "data_classification": classification,
                "input_source": source,
                "skip_theme_discovery": True,
                "fixture_id": None,
                "weekly_mode": "wisburg-auto",
                "shelf_mode": shelf_mode,
                "requested_weekly_mode": requested_mode,
            },
            "needs_inference": True,
        }

    inputs = public_inputs(as_of)
    source = "public-synthetic-v1"
    if mode == "shelf-fixture":
        if p is None:
            raise ValueError("shelf-fixture mode requires a platform")
        shelf_store.persist_fixture(p, as_of)
        _, inputs["universe"] = shelf_store.universe(
            p.state,
            as_of=as_of,
            classification=shelf_store.PUBLIC_FIXTURE_CLASSIFICATION,
        )
        source = shelf_store.PUBLIC_FIXTURE_SOURCE
    elif mode == "olive-live":
        if p is None:
            raise ValueError("olive-live mode requires a platform")
        _, inputs["universe"] = shelf_store.universe(
            p.state,
            as_of=as_of,
            classification=shelf_store.LIVE_CLASSIFICATION,
            source=shelf_store.LIVE_SOURCE,
        )
        # Licensed research remains outside the cloud by default. This mode
        # proves selection from the real shelf with a public synthetic corpus;
        # the composite classification prevents that from being described as a
        # fully live-data decision.
        source = "public-synthetic-corpus+olive-shelf-rds"
    return {
        **inputs,
        "generators": ["ai_native", "carl_constraint"],
        "selectors": ["buy_all", "spread"],
        "prices": {},
        "params": {
            "top_n": 3,
            "n": 6,
            "data_classification": classification_for_mode(mode),
            "input_source": source,
            "skip_theme_discovery": True,
            "fixture_id": (
                "poc-public-dashboard-v1"
                if mode != "olive-live" else None),
            "weekly_mode": mode,
            "requested_weekly_mode": requested_mode,
        },
        "needs_inference": True,
    }


def run_weekly(as_of: date, *, p: plat.Platform | None = None,
               verbose: bool = True,
               mode: str = "public-synthetic",
               prepared: dict[str, Any] | None = None
               ) -> orchestrator.RunResult:
    """Run Stage A/B/C with real inference over one persisted input mode."""
    p = p or plat.load(platform="byteplus")
    result = orchestrator.weekly(
        as_of=as_of,
        p=p,
        verbose=verbose,
        **(prepared or weekly_kwargs(as_of, p=p, mode=mode)),
    )
    if not result.completed:
        return result

    problems = []
    if result.calls <= 0:
        problems.append("no ModelArk calls were recorded")
    if not result.topics:
        problems.append("topic scorer selected no topics")
    for name in ("ai_native", "carl_constraint"):
        arm = result.generators.get(name) or {}
        if arm.get("error") or not arm.get("n"):
            problems.append(f"generator {name} produced no accepted ideas")
    if result.n_candidates <= 0:
        problems.append("merged candidate pool is empty")
    for name in ("buy_all", "spread"):
        arm = result.selectors.get(name) or {}
        if arm.get("error") or not arm.get("n"):
            problems.append(f"selector {name} chose no candidates")
    if problems:
        result.ok = False
        result.error = "POC acceptance failed: " + "; ".join(problems)
        p.state.execute(
            "UPDATE orch_runs SET ok=0, error=? WHERE run_id=?",
            (result.error, result.run_id),
        )
    return result


def _wednesdays(as_of: date, weeks: int) -> list[date]:
    end = as_of - timedelta(days=(as_of.weekday() - 2) % 7)
    return [end - timedelta(days=7 * i) for i in reversed(range(weeks))]


def _stable_phase(instrument_id: str) -> float:
    digest = hashlib.sha256(instrument_id.encode()).digest()
    return int.from_bytes(digest[:2], "big") / 65535.0 * math.tau


def _seed_backtest_db(path: Path, as_of: date, weeks: int) -> tuple[Any, list[date]]:
    # `db.init` creates the legacy replay tables while the portable schema owns
    # `events`, which `backtest.context_for` also reads. Build both surfaces in
    # the disposable database before seeding it.
    from .platform.local import SqliteStateStore

    con = db.init(path)
    portable = SqliteStateStore(path)
    schema.migrate(portable)
    portable.connection.close()
    periods = _wednesdays(as_of, weeks)
    inputs = public_inputs(as_of)
    instruments = inputs["universe"]
    names = {row["instrument_id"]: row["name"] for row in instruments}

    for row in instruments:
        iid = row["instrument_id"]
        db.upsert(con, "instruments", {
            "key": iid,
            "kind": "fund",
            "futu_code": None,
            "olive_key": iid,
            "name": row["name"],
            "market": "SYNTHETIC",
            "currency": "USD",
            "priceable": 1,
            "meta": {
                "exposure": row.get("exposure"),
                "vehicle": row.get("vehicle"),
                "data_classification": CLASSIFICATION,
            },
            "updated_at": as_of.isoformat(),
        }, ["key"])

    start = periods[0] - timedelta(days=7)
    day = start
    levels = {row["instrument_id"]: 100.0 + i * 3.0
              for i, row in enumerate(instruments)}
    while day <= as_of:
        if day.weekday() < 5:
            elapsed = (day - start).days
            for index, row in enumerate(instruments):
                iid = row["instrument_id"]
                phase = _stable_phase(iid)
                regime = math.sin(elapsed / 17.0 + phase)
                drift = (0.00018 + index * 0.000025
                         + 0.00055 * regime
                         + 0.00022 * math.sin(elapsed / 5.0 + index))
                levels[iid] = max(20.0, levels[iid] * (1.0 + drift))
                db.upsert(con, "navs", {
                    "olive_key": iid,
                    "d": day.isoformat(),
                    "nav": round(levels[iid], 6),
                    "src": CLASSIFICATION,
                }, ["olive_key", "d"])
        day += timedelta(days=1)

    ids = [row["instrument_id"] for row in instruments]
    for period_index, period in enumerate(periods):
        rotations = {
            "ai_native": [ids[(period_index + x) % len(ids)]
                          for x in (0, 1, 4)],
            "carl_constraint": [ids[(period_index + x) % len(ids)]
                                for x in (2, 3, 5)],
        }
        for method, selected in rotations.items():
            batch_id = f"BT{period:%Y%m%d}-{method}"
            db.upsert(con, "batches", {
                "batch_id": batch_id,
                "as_of": period.isoformat(),
                "generated_at": f"{period.isoformat()}T07:00:00+08:00",
                "generator": method,
                "methodology": BACKTEST_METHODOLOGY,
                "n_ideas": len(selected),
                "status": "validated",
                "note": "public synthetic frozen candidate pool",
            }, ["batch_id"])
            for rank, iid in enumerate(selected, 1):
                up = 4.5 + ((period_index + rank * 2) % 6) * 0.8
                down = -(2.0 + ((period_index + rank) % 4) * 0.55)
                uid = f"{batch_id}#{rank}"
                db.upsert(con, "ideas", {
                    "idea_uid": uid,
                    "batch_id": batch_id,
                    "as_of": period.isoformat(),
                    "local_id": rank,
                    "rank": rank,
                    "tool": iid,
                    "tool_desc": names[iid],
                    "vehicle": "公募",
                    "theme": _THEME_MARKERS[(period_index + rank) % 3][0],
                    "theme_id": _THEME_MARKERS[(period_index + rank) % 3][0],
                    "direction": "↑",
                    "horizon": "1个月",
                    "horizon_months": 1,
                    "action": "可执行",
                    "instrument": "fund",
                    "futu_code": None,
                    "olive_key": iid,
                    "hurdle": 0.28,
                    "central_p": [40.0, 40.0, 20.0],
                    "central_r": [up, 0.0, down],
                    "conserv_p": [35.0, 40.0, 25.0],
                    "conserv_r": [up * 0.8, 0.0, down * 1.2],
                    "grade": "B",
                    "view": f"{method} sample candidate",
                    "thesis": (
                        f"Public synthetic {method} hypothesis for {names[iid]}; "
                        "used only to prove deterministic replay mechanics."
                    ),
                    "raw": {
                        "instrument_key": iid,
                        "data_classification": CLASSIFICATION,
                    },
                }, ["idea_uid"])
    return con, periods


def _arm_positions(con, periods: list[date], arm: str,
                   horizon_days: int) -> list[dict[str, Any]]:
    rows = []
    for period in periods:
        ctx = backtest.context_for(con, period, allow_model=False)
        verdict = strat.run("idea_selector", arm, ctx)
        candidates = backtest._candidates(con, period)
        by_id = {str(candidate["id"]): candidate for candidate in candidates}
        outcomes = backtest.outcomes_for(
            con, candidates, period, horizon_days=horizon_days,
            require_full_horizon=False)
        for candidate_id in verdict.chosen:
            candidate = by_id[candidate_id]
            outcome = outcomes[candidate_id]
            rows.append({
                "arm": arm,
                "period": period.isoformat(),
                "instrument_id": str(candidate["instrument_id"]),
                "entry_d": outcome.entry_d,
                "exit_d": outcome.exit_d,
                "entry_nav": outcome.entry_px,
                "exit_nav": outcome.exit_px,
                "return_pct": (None if outcome.ret is None
                               else round(outcome.ret * 100.0, 6)),
                "status": outcome.status,
                "thesis": candidate.get("thesis"),
            })
    return rows


def _curves(rep: backtest.Sweep) -> list[dict[str, Any]]:
    points = []
    for arm, score in rep.arms.items():
        equity = 100.0
        peak = equity
        first = date.fromisoformat(rep.dates[0]) - timedelta(days=7)
        points.append({
            "arm": arm, "d": first.isoformat(), "equity": equity,
            "period_ret": 0.0, "drawdown": 0.0, "n_positions": 0,
        })
        for period in rep.dates:
            row = score.per_period.get(period) or {}
            period_ret = row.get("mean")
            if period_ret is not None:
                equity *= 1.0 + float(period_ret)
            peak = max(peak, equity)
            points.append({
                "arm": arm,
                "d": period,
                "equity": round(equity, 6),
                "period_ret": (None if period_ret is None
                               else round(float(period_ret) * 100.0, 6)),
                "drawdown": round((equity / peak - 1.0) * 100.0, 6),
                "n_positions": int(row.get("n_scored") or 0),
            })
    return points


def _summary(rep: backtest.Sweep) -> dict[str, Any]:
    arms = {}
    for name, arm in rep.arms.items():
        arms[name] = {
            "n_chosen": arm.n_chosen,
            "n_scored": arm.n_scored,
            "coverage": arm.coverage,
            "hit_rate": arm.hit_rate,
            "mean_return_pct": (None if arm.mean is None
                                else round(arm.mean * 100.0, 4)),
            "median_return_pct": (None if arm.median is None
                                  else round(arm.median * 100.0, 4)),
            "window_complete_frac": arm.window_complete_frac,
            "unknown": arm.unknown,
        }
    paired = rep.paired.get("generated_carl_constraint")
    paired_summary = asdict(paired) if paired else None
    if paired_summary:
        paired_summary["mechanics_conclusive"] = paired_summary["conclusive"]
        paired_summary["conclusive"] = False
        paired_summary["message"] = (
            "冻结样本价格与候选池只能验证配对计算机制；"
            "即使统计门槛在该合成路径上满足，也不据此判断方法胜负、"
            "alpha 或预测准确性。"
        )
    return {
        "data_classification": CLASSIFICATION,
        "proof": "methodology_mechanical_effectiveness",
        "predictive_claim": False,
        "disclaimer": (
            "Public synthetic prices and frozen candidate pools prove deterministic "
            "as-of replay, persistence, and paired comparison mechanics. They do "
            "not estimate alpha or forecasting accuracy."
        ),
        "periods": len(rep.dates),
        "dates": rep.dates,
        "horizon_days": rep.horizon_days,
        "model_calls": rep.calls,
        "arms": arms,
        "paired": paired_summary,
    }


def run_backtest(as_of: date, *, p: plat.Platform | None = None,
                 weeks: int = 13, horizon_days: int = 30,
                 verbose: bool = True) -> dict[str, Any]:
    """Run, persist, and archive a deterministic three-month replay."""
    p = p or plat.load(platform="byteplus")
    schema.migrate(p.state)
    recipe = {
        "as_of": as_of.isoformat(),
        "weeks": weeks,
        "horizon_days": horizon_days,
        "methodology": BACKTEST_METHODOLOGY,
        "classification": CLASSIFICATION,
        "fixture_sha": poc_fixture.read().sha256,
    }
    inputs_sha = hashlib.sha256(json.dumps(
        recipe, sort_keys=True).encode()).hexdigest()
    backtest_id = f"bt-synth-{as_of:%Y%m%d}-{inputs_sha[:10]}"
    started = datetime.now(timezone.utc).isoformat()
    p.state.execute(
        "DELETE FROM backtest_points WHERE backtest_id=?", (backtest_id,))
    p.state.execute(
        "DELETE FROM backtest_positions WHERE backtest_id=?", (backtest_id,))

    with tempfile.TemporaryDirectory(prefix="ideagen-backtest-") as tmp:
        con, periods = _seed_backtest_db(
            Path(tmp) / "backtest.sqlite", as_of, weeks)
        rep = backtest.sweep(
            con,
            periods,
            stage="idea_selector",
            arms=BACKTEST_ARMS,
            control="generated_ai_native",
            horizon_days=horizon_days,
            require_full_horizon=False,
            allow_model=False,
            strict=True,
        )
        summary = _summary(rep)
        points = _curves(rep)
        positions = [
            row
            for arm in BACKTEST_ARMS
            for row in _arm_positions(con, periods, arm, horizon_days)
        ]

    artifact = {
        "format": "ideagen.public-synthetic-backtest/v1",
        "backtest_id": backtest_id,
        "recipe": recipe,
        "inputs_sha": inputs_sha,
        "summary": summary,
        "points": points,
        "positions": positions,
        "audits": [asdict(audit) for audit in rep.audits],
    }
    raw = json.dumps(
        artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()
    key = f"backtests/{as_of.isoformat()}/{backtest_id}/result.json"
    if p.blobs.exists(key):
        if p.blobs.get(key) != raw:
            raise RuntimeError(f"immutable backtest artifact drifted: {key}")
        artifact_uri = p.blobs.uri(key)
    else:
        artifact_uri = p.blobs.put(
            key, raw, content_type="application/json",
            metadata={
                "classification": CLASSIFICATION,
                "inputs-sha": inputs_sha,
            })

    with p.state.tx():
        schema.upsert(p.state, "backtest_runs", {
            "backtest_id": backtest_id,
            "as_of": as_of.isoformat(),
            "window_start": periods[0].isoformat(),
            "window_end": periods[-1].isoformat(),
            "methodology": BACKTEST_METHODOLOGY,
            "data_classification": CLASSIFICATION,
            "model_id": None,
            "model_release_date": None,
            "knowledge_cutoff": None,
            "inputs_sha": inputs_sha,
            "artifact_uri": artifact_uri,
            "started_at": started,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "ok": 1,
            "error": None,
            "summary": json.dumps(summary, ensure_ascii=False,
                                  separators=(",", ":"), allow_nan=False),
        })
        for row in points:
            schema.upsert(p.state, "backtest_points", {
                "backtest_id": backtest_id,
                **row,
            })
        for row in positions:
            schema.upsert(p.state, "backtest_positions", {
                "backtest_id": backtest_id,
                **row,
            })

    receipt = {
        "backtest_id": backtest_id,
        "classification": CLASSIFICATION,
        "window": [periods[0].isoformat(), periods[-1].isoformat()],
        "periods": len(periods),
        "arms": list(BACKTEST_ARMS),
        "points": len(points),
        "positions": len(positions),
        "model_calls": rep.calls,
        "artifact_uri": artifact_uri,
        "inputs_sha": inputs_sha,
    }
    if verbose:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt
