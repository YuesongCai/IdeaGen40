"""Account-scoped holdings. USD amounts come from the ledger, never quotes.

Use performance's cash replay for live/backfill so changing the subset changes
the whole account, including the weight denominator. No schema changes.
"""
from __future__ import annotations

from collections import defaultdict

from . import db, paper, performance


def _sum_complete(rows, key):
    values = [r[key] for r in rows]
    return sum(values) if all(v is not None for v in values) else None


def account(con, book_id, subset="all"):
    if subset not in performance.SUBSETS:
        raise ValueError("invalid subset")
    ledger = performance.book_ledger(con, book_id, subset)
    point = ledger["points"][-1] if ledger["points"] else None
    as_of = point["d"] if point else None
    lots = []
    for p in ledger["positions"]:
        # Holdings and marks must refer to the same accounting date. A closed
        # position can still have been open on the last stored equity date.
        if not as_of or p["opened_d"] > as_of or (p["closed_d"] and p["closed_d"] <= as_of):
            continue
        idea = db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?", (p["idea_uid"],))
        idea = dict(idea) if idea else {}
        ccy = paper._currency(con, idea) if idea else None
        mark = db.q1(con, "SELECT d, px, mv FROM mtm WHERE pos_id=? AND d<=? ORDER BY d DESC LIMIT 1",
                      (p["pos_id"], as_of))
        buy = db.q1(con, "SELECT SUM(gross) AS gross, SUM(qty*px) AS local_gross FROM trades "
                         "WHERE pos_id=? AND side='BUY' AND d<=?", (p["pos_id"], as_of))
        fx = (buy["gross"] / buy["local_gross"]
              if buy and buy["local_gross"] else None)
        cost = float(p["cost"])
        qty = float(p["qty"])
        # A stale mark is visible, but cannot masquerade as this date's value.
        mv = float(mark["mv"]) if mark and mark["d"] == as_of and mark["mv"] is not None else None
        lots.append({"pos_id": p["pos_id"], "code": p["code"], "currency": ccy,
                     "name": idea.get("tool_desc") or p["code"], "qty": qty,
                     "cost": cost, "local_cost": cost / fx if fx else None,
                     "entry_px": p["avg_px"], "market_value": mv,
                     "last_px": mark["px"] if mark else None,
                     "mark_date": mark["d"] if mark else None,
                     "unrealized": mv - cost if mv is not None else None,
                     "opened_d": p["opened_d"], "period": p["as_of"],
                     "theme": idea.get("theme_id") or p["theme"],
                     "thesis": idea.get("thesis"), "classification": p["class"],
                     "stop_px": p["stop_px"], "take_px": p["take_px"]})
    by_code = defaultdict(list)
    for lot in lots:
        by_code[(lot["code"], lot["currency"])].append(lot)
    groups = []
    equity = point["equity"] if point else None
    for (code, ccy), rows in by_code.items():
        qty = sum(p["qty"] for p in rows)
        cost = sum(p["cost"] for p in rows)
        mv = _sum_complete(rows, "market_value")
        local_cost = _sum_complete(rows, "local_cost")
        pnl = mv - cost if mv is not None else None
        marks = {(p["last_px"], p["mark_date"]) for p in rows}
        groups.append({"code": code, "currency": ccy, "name": rows[0]["name"],
                       "qty": qty, "cost": cost, "market_value": mv,
                       "avg_cost": local_cost / qty if local_cost is not None and qty else None,
                       "last_px": rows[0]["last_px"] if len(marks) == 1 else None,
                       "mark_date": rows[0]["mark_date"] if len(marks) == 1 else None,
                       "weight_pct": mv / equity * 100 if mv is not None and equity and equity > 0 else None,
                       "unrealized": pnl, "return_pct": pnl / cost * 100 if pnl is not None and cost else None,
                       "lots": rows})
    groups.sort(key=lambda g: (g["market_value"] is None, -(g["market_value"] or 0), g["code"]))
    classes = performance.run_classes(con)
    pending = []
    for r in db.q(con, "SELECT o.*, i.tool_desc, b.generator FROM orders o "
                      "LEFT JOIN ideas i USING(idea_uid) LEFT JOIN batches b USING(batch_id) "
                      "WHERE o.book_id=? AND o.status='pending' AND o.side='BUY' "
                      "ORDER BY o.placed_d, o.code", (book_id,)):
        gen = str(r["generator"] or "")
        cls = classes.get(gen.split(":", 1)[1], "unknown") if gen.startswith("weekly:") else "unknown"
        if subset != "all" and cls != subset:
            continue
        pending.append({"code": r["code"], "name": r["tool_desc"] or r["code"],
                        "notional": r["notional"], "placed_d": r["placed_d"],
                        "period": r["as_of"], "expire_d": r["expire_d"],
                        "classification": cls})
    total_mv = _sum_complete(lots, "market_value")
    reconciled = bool(point and total_mv is not None
                      and abs(total_mv - point["mv"]) <= performance.RECON_TOL
                      and abs(point["cash"] + point["mv"] - equity) <= performance.RECON_TOL)
    return {"book_id": book_id, "selector": book_id.removeprefix("sel-"),
            "label": ledger["label"], "subset": subset, "currency": "USD",
            "as_of": as_of, "capital": ledger["capital"], "equity": equity,
            "cash": point["cash"] if point else None,
            "market_value": point["mv"] if point else None,
            "holding_cost": sum(p["cost"] for p in lots) if point else None,
            "unrealized": _sum_complete(lots, "unrealized") if point else None,
            "cash_pct": point["cash"] / equity * 100 if point and equity and equity > 0 else None,
            "reconciled": reconciled, "positions": groups, "pending": pending,
            "pending_notional": _sum_complete(pending, "notional")}


def view(con, subset="all"):
    if subset not in performance.SUBSETS:
        raise ValueError("invalid subset")
    return {"subset": subset, "currency": "USD", "books": [
        account(con, b["book_id"], subset) for b in db.q(
            con, "SELECT book_id FROM books WHERE book_id LIKE 'sel-%' ORDER BY book_id")]}
