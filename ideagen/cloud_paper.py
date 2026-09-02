"""Portable NAV-based paper execution for RDS-backed deployments.

The legacy paper engine is an OHLC simulator tied to SQLite. Cloud fund
execution has a smaller, different contract: a selector creates equal-weight
orders, an order fills only at an observed shelf NAV, and later snapshots mark
or close that position. All state is in RDS, so a restart cannot lose the book.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from . import config, ideas, schema, shelf_store

MAX_NAV_STALE_DAYS = 10
WARN_NAV_STALE_DAYS = 3


def _id(*parts: Any) -> str:
    return hashlib.sha256(
        "|".join(str(part) for part in parts).encode()).hexdigest()[:24]


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _family(classification: str | None) -> str:
    value = str(classification or "")
    if (value.startswith("licensed-")
            or "licensed-private-corpus" in value
            or "+licensed-live-shelf" in value):
        return "licensed"
    if value.startswith("public-synthetic"):
        return "public-synthetic"
    return value


def _shelf_classification(classification: str | None) -> str:
    value = str(classification or "")
    return (
        shelf_store.LIVE_CLASSIFICATION
        if "licensed-live-shelf" in value
        else shelf_store.PUBLIC_FIXTURE_CLASSIFICATION
    )


def _book_id(selector: str, classification: str | None) -> str:
    return f"cloud-{_family(classification)}-{selector}"


def _available_cash(state: Any, book_id: str, capital: float) -> float:
    positions = state.q(
        "SELECT status, cost, realized FROM paper_positions WHERE book_id=?",
        (book_id,),
    )
    pending = state.q(
        "SELECT COALESCE(SUM(notional),0) AS reserved FROM paper_orders "
        "WHERE book_id=? AND status='pending'",
        (book_id,),
    )
    open_cost = sum(
        float(row.get("cost") or 0.0)
        for row in positions if row["status"] == "open"
    )
    realized = sum(
        float(row.get("realized") or 0.0)
        for row in positions if row["status"] == "closed"
    )
    reserved = float((pending[0].get("reserved") if pending else None) or 0.0)
    return max(0.0, capital - open_cost + realized - reserved)


def _fill_order(state: Any, order: dict[str, Any],
                candidate: dict[str, Any], nav: dict[str, Any]) -> str:
    nav_value = float(nav["nav"])
    qty = float(order["notional"]) / nav_value
    pos_id = _id("position", order["order_id"])
    opened = str(nav["d"])
    schema.upsert(state, "paper_positions", {
        "pos_id": pos_id,
        "book_id": order["book_id"],
        "order_id": order["order_id"],
        "run_id": order["run_id"],
        "candidate_id": order["candidate_id"],
        "instrument_id": order["instrument_id"],
        "opened_d": opened,
        "horizon_end": ideas.horizon_end(
            date.fromisoformat(order["as_of"]), 1).isoformat(),
        "qty": qty,
        "entry_nav": nav_value,
        "cost": float(order["notional"]),
        "current_nav": nav_value,
        "mark_d": opened,
        "market_value": float(order["notional"]),
        "unrealized": 0.0,
        "unrealized_pct": 0.0,
        "status": "open",
        "closed_d": None,
        "exit_nav": None,
        "realized": 0.0,
        "exit_reason": None,
        "thesis": str(candidate.get("thesis") or "")[:1000],
        "data_classification": order["data_classification"],
    })
    state.execute(
        "UPDATE paper_orders SET status='filled', fill_d=?, fill_nav=?, "
        "fill_qty=?, snapshot_id=? WHERE order_id=?",
        (opened, nav_value, qty, nav.get("snapshot_id"), order["order_id"]),
    )
    schema.upsert(state, "paper_marks", {
        "pos_id": pos_id,
        "d": opened,
        "nav": nav_value,
        "market_value": float(order["notional"]),
        "unrealized": 0.0,
        "unrealized_pct": 0.0,
        "stale_days": 0,
        "snapshot_id": nav.get("snapshot_id"),
    })
    return pos_id


def _equity(state: Any, book_id: str, d: str) -> dict[str, Any]:
    books = state.q(
        "SELECT capital FROM paper_books WHERE book_id=?", (book_id,))
    if not books:
        raise KeyError(book_id)
    capital = float(books[0]["capital"])
    rows = state.q(
        "SELECT status, cost, market_value, realized FROM paper_positions "
        "WHERE book_id=?", (book_id,))
    open_cost = sum(float(row.get("cost") or 0.0)
                    for row in rows if row["status"] == "open")
    market_value = sum(float(row.get("market_value") or 0.0)
                       for row in rows if row["status"] == "open")
    realized = sum(float(row.get("realized") or 0.0)
                   for row in rows if row["status"] == "closed")
    cash = capital - open_cost + realized
    equity = cash + market_value
    prior = state.q(
        "SELECT MAX(equity) AS peak FROM paper_equity "
        "WHERE book_id=? AND d<=?", (book_id, d))
    peak = max(float((prior[0].get("peak") if prior else None) or equity), equity)
    row = {
        "book_id": book_id,
        "d": d,
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "return_pct": ((equity / capital - 1.0) * 100.0 if capital else None),
        "drawdown": ((equity / peak - 1.0) * 100.0 if peak else None),
        "n_open": sum(1 for row in rows if row["status"] == "open"),
    }
    schema.upsert(state, "paper_equity", row)
    return row


def book_run(platform: Any, run_id: str, *,
             selectors: list[str] | None = None) -> dict[str, Any]:
    """Book each selector into an equal-rule NAV paper book, idempotently."""
    schema.migrate(platform.state)
    run_rows = platform.state.q(
        "SELECT run_id, as_of, ok, data_classification FROM orch_runs "
        "WHERE run_id=?", (run_id,))
    if not run_rows:
        raise KeyError(f"unknown run {run_id}")
    run = dict(run_rows[0])
    if not run["ok"]:
        raise ValueError(f"run {run_id} is not complete")
    as_of = date.fromisoformat(run["as_of"])
    shelf_classification = _shelf_classification(
        run.get("data_classification"))
    snapshot, shelf_universe = shelf_store.universe(
        platform.state,
        as_of=as_of,
        classification=shelf_classification,
    )
    shelf_ids = {
        str(instrument["instrument_id"]) for instrument in shelf_universe
    }

    candidates = {
        str(row["candidate_id"]): _json(row.get("payload"), {})
        for row in platform.state.q(
            "SELECT candidate_id, payload FROM candidates WHERE run_id=?",
            (run_id,))
    }
    verdicts = platform.state.q(
        "SELECT strategy, chosen FROM verdicts "
        "WHERE run_id=? AND kind='idea_selector' ORDER BY strategy",
        (run_id,))
    if not candidates or not verdicts:
        raise ValueError(f"run {run_id} has no candidates or selectors")

    now = datetime.now(timezone.utc).isoformat()
    capital = float(config.SELECTOR_SPEC["capital"])
    tranche = capital * float(config.SELECTOR_SPEC.get("tranche_frac", 1.0))
    output: dict[str, Any] = {
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "books": {},
    }
    with platform.state.tx():
        for verdict in verdicts:
            selector = str(verdict["strategy"])
            if selectors is not None and selector not in selectors:
                continue
            chosen_ids = [
                str(candidate_id) for candidate_id in
                _json(verdict.get("chosen"), [])
                if str(candidate_id) in candidates
            ]
            if not chosen_ids:
                output["books"][selector] = {"skipped": "no selected candidates"}
                continue
            off_shelf = sorted({
                str(candidates[candidate_id].get("instrument_id") or "")
                for candidate_id in chosen_ids
            } - shelf_ids)
            if off_shelf:
                shown = (
                    [shelf_store.public_alias(value) for value in off_shelf]
                    if _family(run.get("data_classification")) == "licensed"
                    else off_shelf
                )
                raise ValueError(
                    f"selector {selector} chose instruments outside snapshot "
                    f"{snapshot['snapshot_id']}: {shown[:5]}"
                )
            book_id = _book_id(selector, run.get("data_classification"))
            prior_book = platform.state.q(
                "SELECT created_at FROM paper_books WHERE book_id=?",
                (book_id,),
            )
            schema.upsert(platform.state, "paper_books", {
                "book_id": book_id,
                "selector": selector,
                "label": f"Selector · {selector}",
                "capital": capital,
                "data_classification": run["data_classification"],
                "created_at": (
                    prior_book[0]["created_at"] if prior_book else now),
                "updated_at": now,
            })
            placed = filled = pending = existing = cash_limited = 0
            missing: list[tuple[str, dict[str, Any], str]] = []
            for candidate_id in chosen_ids:
                candidate = candidates[candidate_id]
                instrument_id = str(candidate.get("instrument_id") or "")
                if not instrument_id:
                    continue
                order_id = _id("order", book_id, run_id, candidate_id)
                have = platform.state.q(
                    "SELECT order_id, status FROM paper_orders WHERE order_id=?",
                    (order_id,))
                if have:
                    existing += 1
                    continue
                missing.append((candidate_id, candidate, instrument_id))
            available = _available_cash(platform.state, book_id, capital)
            target = tranche / len(chosen_ids)
            notional = min(
                target,
                available / len(missing) if missing else 0.0,
            )
            for candidate_id, candidate, instrument_id in missing:
                if notional <= 0:
                    cash_limited += 1
                    continue
                order_id = _id("order", book_id, run_id, candidate_id)
                order = {
                    "order_id": order_id,
                    "book_id": book_id,
                    "run_id": run_id,
                    "selector": selector,
                    "candidate_id": candidate_id,
                    "instrument_id": instrument_id,
                    "as_of": as_of.isoformat(),
                    "notional": round(notional, 6),
                    "status": "pending",
                    "placed_at": now,
                    "fill_d": None,
                    "fill_nav": None,
                    "fill_qty": None,
                    "snapshot_id": snapshot["snapshot_id"],
                    "data_classification": run["data_classification"],
                }
                schema.upsert(platform.state, "paper_orders", order)
                placed += 1
                nav = shelf_store.nav_on_or_before(
                    platform.state,
                    instrument_id,
                    as_of.isoformat(),
                    classification=shelf_classification,
                )
                if nav and str(nav["d"]) >= as_of.isoformat():
                    _fill_order(platform.state, order, candidate, nav)
                    filled += 1
                else:
                    pending += 1
            _equity(platform.state, book_id, as_of.isoformat())
            output["books"][selector] = {
                "book_id": book_id,
                "placed": placed,
                "filled": filled,
                "pending": pending,
                "existing": existing,
                "cash_limited": cash_limited,
            }
    return output


def _alert(state: Any, *, book_id: str, pos_id: str, d: str,
           level: str, kind: str, message: str) -> None:
    schema.upsert(state, "paper_alerts", {
        "alert_id": _id(kind, book_id, pos_id, d),
        "book_id": book_id,
        "pos_id": pos_id,
        "d": d,
        "level": level,
        "kind": kind,
        "message": message,
        "acknowledged": 0,
    })


def monitor(platform: Any, d: date) -> dict[str, Any]:
    """Fill pending NAV orders, mark positions and close completed horizons."""
    schema.migrate(platform.state)
    day = d.isoformat()
    report: dict[str, Any] = {
        "d": day, "filled": 0, "marked": 0, "closed": 0,
        "alerts": 0, "books": {},
    }
    with platform.state.tx():
        for pending in platform.state.q(
                "SELECT * FROM paper_orders WHERE status='pending' "
                "AND as_of<=? ORDER BY order_id", (day,)):
            nav = shelf_store.nav_on_or_before(
                platform.state,
                pending["instrument_id"],
                day,
                classification=_shelf_classification(
                    pending.get("data_classification")),
            )
            if not nav or str(nav["d"]) < str(pending["as_of"]):
                continue
            candidates = platform.state.q(
                "SELECT payload FROM candidates "
                "WHERE run_id=? AND candidate_id=?",
                (pending["run_id"], pending["candidate_id"]))
            candidate = _json(
                candidates[0]["payload"], {}) if candidates else {}
            _fill_order(platform.state, dict(pending), candidate, nav)
            report["filled"] += 1

        positions = platform.state.q(
            "SELECT * FROM paper_positions WHERE status='open' ORDER BY pos_id")
        for position in positions:
            nav = shelf_store.nav_on_or_before(
                platform.state,
                position["instrument_id"],
                day,
                classification=_shelf_classification(
                    position.get("data_classification")),
            )
            if not nav:
                _alert(
                    platform.state,
                    book_id=position["book_id"],
                    pos_id=position["pos_id"],
                    d=day,
                    level="warn",
                    kind="nav_missing",
                    message="No NAV is available on or before the monitor date.",
                )
                report["alerts"] += 1
                continue
            stale = (d - date.fromisoformat(str(nav["d"]))).days
            nav_value = float(nav["nav"])
            market_value = float(position["qty"]) * nav_value
            unrealized = market_value - float(position["cost"])
            unrealized_pct = (
                unrealized / float(position["cost"]) * 100.0
                if position.get("cost") else None)
            schema.upsert(platform.state, "paper_marks", {
                "pos_id": position["pos_id"],
                "d": day,
                "nav": nav_value,
                "market_value": market_value,
                "unrealized": unrealized,
                "unrealized_pct": unrealized_pct,
                "stale_days": stale,
                "snapshot_id": nav.get("snapshot_id"),
            })
            platform.state.execute(
                "UPDATE paper_positions SET current_nav=?, mark_d=?, "
                "market_value=?, unrealized=?, unrealized_pct=? WHERE pos_id=?",
                (nav_value, day, market_value, unrealized, unrealized_pct,
                 position["pos_id"]),
            )
            report["marked"] += 1
            if stale > WARN_NAV_STALE_DAYS:
                _alert(
                    platform.state,
                    book_id=position["book_id"],
                    pos_id=position["pos_id"],
                    d=day,
                    level=("action" if stale > MAX_NAV_STALE_DAYS else "warn"),
                    kind="nav_stale",
                    message=f"NAV is {stale} calendar days old.",
                )
                report["alerts"] += 1
            if position.get("horizon_end") and day >= position["horizon_end"] \
                    and str(nav["d"]) >= position["horizon_end"]:
                platform.state.execute(
                    "UPDATE paper_positions SET status='closed', closed_d=?, "
                    "exit_nav=?, realized=?, exit_reason='horizon' "
                    "WHERE pos_id=?",
                    (day, nav_value, unrealized, position["pos_id"]),
                )
                report["closed"] += 1

        for book in platform.state.q(
                "SELECT book_id FROM paper_books ORDER BY book_id"):
            row = _equity(platform.state, book["book_id"], day)
            report["books"][book["book_id"]] = row
            platform.state.execute(
                "UPDATE paper_books SET updated_at=? WHERE book_id=?",
                (datetime.now(timezone.utc).isoformat(), book["book_id"]),
            )
    return report


def state_view(state: Any) -> list[dict[str, Any]]:
    """Dashboard-compatible paper books with licensed identifiers redacted."""
    books = []
    for book in state.q(
            "SELECT book_id, selector, label, capital, data_classification "
            "FROM paper_books ORDER BY book_id"):
        licensed = _family(book["data_classification"]) == "licensed"
        equity = [{
            "d": row["d"],
            "equity": row["equity"],
            "cash": row["cash"],
            "mv": row["market_value"],
            "drawdown": row["drawdown"],
        } for row in state.q(
            "SELECT d, equity, cash, market_value, drawdown "
            "FROM paper_equity WHERE book_id=? ORDER BY d",
            (book["book_id"],))]
        positions = []
        for row in state.q(
                "SELECT instrument_id, opened_d, entry_nav, current_nav, mark_d, "
                "unrealized, unrealized_pct, status, thesis "
                "FROM paper_positions WHERE book_id=? AND status='open' "
                "ORDER BY opened_d, instrument_id", (book["book_id"],)):
            alias = shelf_store.public_alias(row["instrument_id"])
            positions.append({
                "code": alias if licensed else row["instrument_id"],
                "instrument_name": (f"Licensed fund {alias}" if licensed
                                    else row["instrument_id"]),
                "opened_d": row["opened_d"],
                "entry_px": row["entry_nav"],
                "last_px": row["current_nav"],
                "last_d": row["mark_d"],
                "unrealized": row["unrealized"],
                "unrealized_pct": row["unrealized_pct"],
                "stop_px": None,
                "take_px": None,
                "thesis": None if licensed else row.get("thesis"),
            })
        closed = state.q(
            "SELECT COUNT(*) AS n, COALESCE(SUM(realized),0) AS realized "
            "FROM paper_positions WHERE book_id=? AND status='closed'",
            (book["book_id"],))[0]
        exits = {
            row["exit_reason"]: int(row["n"])
            for row in state.q(
                "SELECT exit_reason, COUNT(*) AS n FROM paper_positions "
                "WHERE book_id=? AND status='closed' GROUP BY exit_reason",
                (book["book_id"],))
            if row.get("exit_reason")
        }
        latest_order = state.q(
            "SELECT run_id, as_of FROM paper_orders WHERE book_id=? "
            "ORDER BY as_of DESC, placed_at DESC LIMIT 1",
            (book["book_id"],))
        books.append({
            "book_id": book["book_id"],
            "selector": book["selector"],
            "label": book.get("label"),
            "capital": book["capital"],
            "data_classification": book["data_classification"],
            "booked_batch": latest_order[0]["run_id"] if latest_order else None,
            "booked_as_of": latest_order[0]["as_of"] if latest_order else None,
            "equity": equity,
            "open_positions": positions,
            "realized": closed.get("realized") or 0,
            "closed_n": int(closed.get("n") or 0),
            "exits": exits,
            "live": False,
        })
    return books
