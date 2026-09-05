"""Paper-trade engine: orders, fills, exits, marks, equity.

Two books run over the same batches so position management can be attributed
separately from idea quality:

  disciplined  methodology sizing, entry-band limit orders, stops, take-profits,
               horizon exits; unfilled capital stays in cash earning the shelf
               money-market yield.
  naive        every tradeable idea bought equal-weight at the first fillable
               close and held to the horizon. No stops, no bands.

The rules that keep the result honest, each of which is a way this kind of study
usually flatters itself:

*No same-bar look-ahead.* An order can only fill on a session whose close is
strictly after the batch's generation timestamp. `first_fillable` derives that
date from `generated_at` and the market's close time, so a batch written after
Monday's close cannot buy Monday's close.

*Breakouts fill on the next open.* A trigger evaluated on day t's close cannot
also be executed at that close; the fill is day t+1's open.

*Limit fills take the worse of band and open.* A buy limit at the top of the
entry band fills at `min(band_hi, open)` — a gap through the band gives the gap
price, but a quiet drift into it gives the band edge, never the day's low.

*Costs on both legs.* Commission plus slippage from `config.COSTS`, charged on
entry and exit.

*Unmarkable instruments never enter a book.* A fund with no NAV history is
recorded against the idea, alerted on, and excluded from P&L rather than being
marked at cost (which would silently import a 0% return).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from . import config, db, ideas as ideas_mod, universe
from .sources import futu_px, olive

# HKD is pegged; using the peg avoids importing an FX feed for a 7.75-7.85 band.
# Any instrument in a currency without an entry here is refused, not guessed.
FX_TO_USD = {"USD": 1.0, "HKD": 1.0 / 7.80}

EXIT_REASONS = ("stop", "take", "horizon", "event", "unmarkable", "manual")


def _oid(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def book_spec(book_id: str) -> dict:
    """Spec for any book, including the per-batch cohort books."""
    if config.is_cohort(book_id):
        return config.COHORT_SPEC
    if config.is_selector_book(book_id):
        return config.SELECTOR_SPEC
    return config.BOOKS[book_id]


def ensure_cohort(con, batch_id: str) -> str:
    """Register (idempotently) the independent book for one day's batch."""
    bid = config.cohort_book(batch_id)
    as_of = db.q1(con, "SELECT as_of FROM batches WHERE batch_id=?", (batch_id,))
    label = f"{as_of['as_of']} 当日组合" if as_of else config.COHORT_SPEC["label"]
    db.upsert(con, "books", {
        "book_id": bid, "label": label, "descr": config.COHORT_SPEC["desc"],
        "capital": config.COHORT_SPEC["capital"],
        "sizing": config.COHORT_SPEC["sizing"],
        "entry": config.COHORT_SPEC["entry"],
        "created_at": config.now_hkt().isoformat()}, ["book_id"])
    return bid


def cohort_books(con) -> list[str]:
    return [r["book_id"] for r in db.q(
        con, "SELECT book_id FROM books WHERE book_id LIKE ? ORDER BY book_id",
        (config.COHORT_PREFIX + "%",))]


def selector_books(con) -> list[str]:
    return [r["book_id"] for r in db.q(
        con, "SELECT book_id FROM books WHERE book_id LIKE ? ORDER BY book_id",
        (config.SELECTOR_PREFIX + "%",))]


def all_books(con) -> list[str]:
    # Selector books are included because "all" means all: this list is what the
    # daily marking loop walks, and a book family missing from it is a family
    # whose orders sit pending forever while prices move on — 114 orders sat
    # unfilled for two sessions exactly this way.
    return [*config.BOOKS, *cohort_books(con), *selector_books(con)]


def _cost_bps(code: str, kind: str) -> float:
    market = futu_px.market_of(code) if kind == "listed" else "FUND"
    spec = config.COSTS.get(market, config.COSTS["US"])
    return spec.get("commission_bps", 0.0) + spec.get("slippage_bps", 0.0)


def _fx(currency: str) -> float | None:
    return FX_TO_USD.get(currency)


# ---------------------------------------------------------------- calendar
def first_fillable(con, batch_id: str, market: str = "US") -> str:
    """Earliest session an order from this batch may be filled on.

    A bar is fillable only if its close happened after the batch was generated.
    """
    b = db.q1(con, "SELECT as_of, generated_at FROM batches WHERE batch_id=?", (batch_id,))
    if not b:
        raise KeyError(batch_id)
    as_of = b["as_of"]
    try:
        gen = datetime.fromisoformat(b["generated_at"])
    except ValueError:
        gen = datetime.fromisoformat(as_of + "T23:59:59+08:00")
    # The first fillable bar is the first session whose *close* falls after the
    # batch existed. `complete_through(gen)` is the last session that had already
    # closed, so the answer is the session immediately after it. Comparing the
    # HKT `as_of` date directly against a US session date would be off by one:
    # a batch stamped 2026-08-07 00:54 HKT was written at 2026-08-06 12:54 ET,
    # while the 2026-08-06 US session was still open and is therefore fillable.
    closed = futu_px.complete_through(market, now=gen)
    return _next_session(con, closed, market)


def _next_session(con, d: str, market: str = "US") -> str:
    """Next session strictly after `d`. Falls back to the next calendar day when
    the bar does not exist yet, so a freshly placed order waits rather than
    silently collapsing onto the last known session."""
    ref = "US.SPY" if market == "US" else "HK.02800"
    r = db.q1(con, "SELECT d FROM prices WHERE code=? AND d>? ORDER BY d LIMIT 1", (ref, d))
    if r:
        return r["d"]
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def sessions_between(con, start: str, end: str, market: str = "US") -> list[str]:
    ref = "US.SPY" if market == "US" else "HK.02800"
    return [r["d"] for r in db.q(
        con, "SELECT d FROM prices WHERE code=? AND d>=? AND d<=? ORDER BY d",
        (ref, start, end))]


def open_cohort(con, batch_id: str, verbose: bool = False) -> dict:
    """Open the batch's own independent book and mark it forward to today."""
    bid = ensure_cohort(con, batch_id)
    rep = open_batch(con, batch_id, bid, verbose=verbose)
    b = db.q1(con, "SELECT as_of FROM batches WHERE batch_id=?", (batch_id,))
    run(con, bid, b["as_of"], futu_px.complete_through("US"), verbose=verbose)
    return rep


# ---------------------------------------------------------------- marking
def mark_price(con, idea: dict, d: str) -> dict | None:
    """Current mark for an idea's instrument, in its own currency."""
    if idea["instrument"] == "listed" and idea["futu_code"]:
        bar = futu_px.bar_on(con, idea["futu_code"], d)
        if bar:
            return {"px": bar["close"], "d": d, "stale": 0, "src": "futu",
                    "high": bar["high"], "low": bar["low"], "open": bar["open"]}
        hit = futu_px.last_close_on_or_before(con, idea["futu_code"], d)
        if hit:
            stale = (date.fromisoformat(d) - date.fromisoformat(hit[0])).days
            return {"px": hit[1], "d": hit[0], "stale": stale, "src": "futu:carry"}
        return None
    if idea["olive_key"]:
        m = olive.mark(con, idea["olive_key"], d)
        if m and m["usable"]:
            return {"px": m["nav"], "d": m["nav_d"], "stale": m["stale_days"],
                    "src": "olive:nav"}
    return None


def markable(con, idea: dict) -> tuple[bool, str]:
    if idea["instrument"] == "listed":
        if not idea["futu_code"]:
            return False, "listed idea without a futu code"
        if idea["futu_code"] in futu_px.quota_blocked(con):
            return False, f"{idea['futu_code']} blocked by OpenD history quota"
        n = db.q1(con, "SELECT COUNT(*) n FROM prices WHERE code=?",
                  (idea["futu_code"],))["n"]
        return (n >= 20, "ok" if n >= 20 else f"only {n} bars available")
    if idea["olive_key"]:
        n = db.q1(con, "SELECT COUNT(*) n FROM navs WHERE olive_key=?",
                  (idea["olive_key"],))["n"]
        return (n >= 1, "ok" if n else "no NAV observation on the Olive shelf")
    return False, "no instrument mapped"


def _currency(con, idea: dict) -> str:
    key = idea["futu_code"] or idea["olive_key"] or idea["tool"]
    r = db.q1(con, "SELECT currency FROM instruments WHERE futu_code=? OR key=? OR olive_key=?",
              (idea["futu_code"], idea["tool"], idea["olive_key"]))
    return (r["currency"] if r else "USD") or "USD"


# ---------------------------------------------------------------- sizing
def size_batch(con, book_id: str, rows: list[dict], equity: float) -> dict[str, float]:
    """Target USD notional per idea, before any fill happens."""
    spec = book_spec(book_id)
    live = []
    skipped: dict[str, str] = {}
    for r in rows:
        ok, why = markable(con, r)
        if not ok:
            skipped[r["idea_uid"]] = why
            continue
        if _fx(_currency(con, r)) is None:
            skipped[r["idea_uid"]] = f"no FX for {_currency(con, r)}"
            continue
        live.append(r)

    # A book cannot spend cash it does not have. Without this, a commingled book
    # receiving a fresh 40-idea batch every day allocates full equity each time and
    # silently levers up — it reached 825% gross before this was enforced. The
    # cohort books are unaffected (one batch each), but the constraint is real for
    # any book, so it is applied to all of them.
    cash_now = _cash_on(con, book_id, _last_marked(con, book_id), spec["capital"])
    budget = max(0.0, cash_now)

    notional: dict[str, float] = {}
    if spec["sizing"] == "equal":
        # A tranche-capped book may not spend more than its declared fraction of
        # capital on one batch, however much cash is sitting free. Without this,
        # the first week of a four-tranche rolling book deploys everything and
        # the "weekly 25%" of the mandate exists only in the documentation.
        tranche = float(spec.get("tranche_frac", 1.0)) * float(spec["capital"])
        per = min(equity, budget, tranche) / max(len(live), 1)
        for r in live:
            notional[r["idea_uid"]] = per
    else:
        theme_used: dict[str, float] = {}
        for r in sorted(live, key=lambda x: (x["rank"] or 999)):
            base = (r["pos_init"] or 1.0) / 100.0
            mult = config.GRADE_SIZE_MULT.get(r["grade"], 0.5)
            w = min(base * mult, config.MAX_SINGLE_POSITION)
            # Theme budget: 框架 §11.3 forbids double-counting one macro signal
            # across products, so cap aggregate exposure per theme.
            th = r["theme"] or "?"
            room = config.MAX_THEME_EXPOSURE - theme_used.get(th, 0.0)
            w = max(0.0, min(w, room))
            theme_used[th] = theme_used.get(th, 0.0) + w
            notional[r["idea_uid"]] = w * equity
        gross = sum(notional.values())
        cap = min(config.MAX_GROSS_EXPOSURE * equity, budget)
        if gross > cap and gross > 0:
            scale = cap / gross
            notional = {k: v * scale for k, v in notional.items()}
    total = sum(notional.values())
    if total > budget and total > 0:
        scale = budget / total
        notional = {k: v * scale for k, v in notional.items()}
    return {"notional": notional, "skipped": skipped, "n_live": len(live),
            "cash_budget": round(budget, 2), "allocated": round(sum(notional.values()), 2)}


def _last_marked(con, book_id: str) -> str:
    r = db.q1(con, "SELECT MAX(d) d FROM equity WHERE book_id=?", (book_id,))
    return (r["d"] if r and r["d"] else config.today_hkt().isoformat())


# ---------------------------------------------------------------- open
def open_batch(con, batch_id: str, book_id: str, verbose: bool = True,
               force: bool = False) -> dict:
    """Place the batch's orders on a book. Refuses to re-place a traded batch.

    This used to claim idempotence it did not have. The order upsert below writes
    `status="pending"`, so a second call resurrected orders that had already filled
    or expired, and the next `step` re-filled them at a size derived from *today's*
    cash — rewriting quantities on positions that were already trading (measured:
    2346.90 → 2346.28 shares, with a duplicate same-day BUY overwriting the original
    trade row).

    That is the same class of failure as replacing an artifact under a live book,
    which this project has already paid for once. So a batch that has traded is
    closed: re-placing it takes `force=True` and a deliberate decision about what
    happens to the positions that already exist.
    """
    batch = db.q1(con, "SELECT * FROM batches WHERE batch_id=?", (batch_id,))
    if not batch:
        raise KeyError(batch_id)

    if not force:
        traded = db.q1(
            con, "SELECT COUNT(*) AS n FROM orders WHERE book_id=? AND as_of=? "
                 "AND status <> 'pending'", (book_id, batch["as_of"]))["n"]
        if traded:
            raise ValueError(
                f"book {book_id} 上 {batch['as_of']} 这批已经有 {traded} 张非挂单状态"
                f"的委托；重新下单会把已成交仓位按今天的资金重算一遍。确实要重下请显式"
                f"传 force=True，并先决定已有仓位怎么处理。")
    validation = db.jl(batch["validation"], {}) or {}
    if not validation.get("pass", False):
        raise ValueError(f"batch {batch_id} failed validation; refusing to trade it")

    universe.hydrate(con)
    rows = ideas_mod.load_batch(con, batch_id)
    spec = book_spec(book_id)
    equity = current_equity(con, book_id, batch["as_of"]) or spec["capital"]
    sized = size_batch(con, book_id, rows, equity)
    notional = sized["notional"]

    placed, skipped = 0, dict(sized["skipped"])
    alerts: list[dict] = []
    with db.tx(con):
        for r in rows:
            uid = r["idea_uid"]
            if uid not in notional or notional[uid] <= 0:
                if uid in skipped:
                    alerts.append({
                        "alert_id": _oid("skip", book_id, uid), "book_id": book_id,
                        "d": batch["as_of"], "level": "warn", "kind": "unmarkable",
                        "idea_uid": uid, "code": r["futu_code"] or r["olive_key"],
                        "message": f"{r['tool']}: {skipped[uid]} — 不进入组合，"
                                   f"P&L 中剔除并单独披露",
                    })
                continue
            market = futu_px.market_of(r["futu_code"]) if r["instrument"] == "listed" else "US"
            fillable = first_fillable(con, batch_id, market)
            ttl_sessions = sessions_between(
                con, fillable,
                (date.fromisoformat(fillable) + timedelta(days=25)).isoformat(), market)
            if len(ttl_sessions) >= config.ORDER_TTL_SESSIONS:
                expire = ttl_sessions[config.ORDER_TTL_SESSIONS - 1]
            elif ttl_sessions:
                # Fewer known sessions than the TTL asks for: the remaining
                # ones have not printed yet. Taking the last known bar gives
                # the order a life measured in bars that already exist, which
                # for an order placed before today's close is one day — the
                # same collapse as the empty case, just harder to see. Extend
                # past the known bars by the shortfall, in calendar days.
                missing = config.ORDER_TTL_SESSIONS - len(ttl_sessions)
                expire = (date.fromisoformat(ttl_sessions[-1])
                          + timedelta(days=missing + 2)).isoformat()
            else:
                # The sessions that would carry this order have not printed yet
                # — normal for an order placed before today's close, and the
                # rule for every backfilled period, whose orders are placed now
                # rather than in the past. Collapsing to `fillable` gave those
                # orders a single-day life and they expired unfilled before any
                # bar existed to fill them. Fall back to a calendar estimate
                # (five sessions ≈ seven days) so the order waits for its bars
                # instead of dying waiting for them.
                expire = (date.fromisoformat(fillable)
                          + timedelta(days=config.ORDER_TTL_SESSIONS + 2)).isoformat()

            if spec["entry"] == "market_close" or r["instrument"] != "listed":
                kind, lo, hi, trig = "market_close", None, None, None
            elif r["entry_hi"] or r["entry_lo"]:
                kind = "band"
                lo, hi = r["entry_lo"], r["entry_hi"] or r["entry_lo"]
                trig = r["entry_break"]
            elif r["entry_break"]:
                kind, lo, hi, trig = "breakout", None, None, r["entry_break"]
            else:
                # No entry discipline stated: the disciplined book treats an idea
                # with no entry level as executable at the close, matching the
                # pack's "可执行" action, rather than inventing a band.
                kind, lo, hi, trig = "market_close", None, None, None

            db.upsert(con, "orders", {
                "order_id": _oid(book_id, uid, kind), "book_id": book_id,
                "idea_uid": uid, "as_of": batch["as_of"], "side": "BUY",
                "code": r["futu_code"] or r["olive_key"], "kind": kind,
                "band_lo": lo, "band_hi": hi, "trigger": trig,
                "notional": round(notional[uid], 2),
                "placed_d": fillable, "expire_d": expire, "status": "pending",
                "note": r["action"],
            }, ["order_id"])
            placed += 1
        if alerts:
            db.upsert_many(con, "alerts", alerts, ["alert_id"])
        con.execute("UPDATE batches SET status='traded' WHERE batch_id=?", (batch_id,))
        _ensure_equity_seed(con, book_id, batch["as_of"], spec["capital"])

    rep = {"batch_id": batch_id, "book_id": book_id, "placed": placed,
           "skipped": skipped, "equity_at_open": equity}
    if verbose:
        print(f"  {book_id:<12} placed={placed} skipped={len(skipped)} "
              f"equity=${equity:,.0f}")
        for uid, why in list(skipped.items())[:6]:
            print(f"      - {uid.split('#')[-1]:>3} {why}")
    return rep


def _ensure_equity_seed(con, book_id: str, d: str, capital: float) -> None:
    r = db.q1(con, "SELECT 1 FROM equity WHERE book_id=?", (book_id,))
    if r:
        return
    prev = _prev_session(con, d)
    db.upsert(con, "equity", {
        "book_id": book_id, "d": prev or d, "cash": capital, "mv": 0.0,
        "equity": capital, "ret_d": 0.0, "cum_ret": 0.0, "drawdown": 0.0,
        "n_open": 0, "gross": 0.0}, ["book_id", "d"])


def _prev_session(con, d: str) -> str | None:
    r = db.q1(con, "SELECT d FROM prices WHERE code='US.SPY' AND d<? ORDER BY d DESC LIMIT 1",
              (d,))
    return r["d"] if r else None


# ---------------------------------------------------------------- fills
def _try_fill(con, order: dict, d: str, idea: dict) -> dict | None:
    """Attempt to fill `order` using session `d`. Returns fill dict or None."""
    kind = order["kind"]
    if idea["instrument"] != "listed":
        m = mark_price(con, idea, d)
        if not m or m["stale"] > 3:
            return None
        return {"px": m["px"], "rule": f"nav@{m['d']}"}

    bar = futu_px.bar_on(con, order["code"], d)
    if not bar:
        return None

    if kind == "market_close":
        return {"px": bar["close"], "rule": "close"}

    if kind == "band":
        hi = order["band_hi"] or order["band_lo"]
        if hi is None:
            return {"px": bar["close"], "rule": "close(no band)"}
        if bar["low"] <= hi:
            # buy limit at `hi`: a gap below fills at the open, otherwise at hi
            px = min(hi, bar["open"])
            return {"px": px, "rule": f"limit<= {hi:g}"}
        if order["trigger"] and bar["close"] > order["trigger"]:
            return {"px": None, "rule": "breakout_armed", "arm": True}
        return None

    if kind == "breakout":
        if order["trigger"] and bar["close"] > order["trigger"]:
            return {"px": None, "rule": "breakout_armed", "arm": True}
    return None


def _apply_fill(con, order: dict, idea: dict, d: str, px: float, rule: str) -> str:
    ccy = _currency(con, idea)
    fx = _fx(ccy) or 1.0
    bps = _cost_bps(order["code"], idea["instrument"])
    fill_px = px * (1 + bps / 20000.0)        # half the bps as entry slippage/comm
    usd_px = fill_px * fx
    qty = order["notional"] / usd_px if usd_px > 0 else 0.0
    gross = qty * usd_px
    fee = gross * (bps / 20000.0)

    pos_id = _oid("pos", order["book_id"], order["idea_uid"])
    hz_end = ideas_mod.horizon_end(date.fromisoformat(idea["as_of"]),
                                   idea["horizon_months"]).isoformat()
    db.upsert(con, "positions", {
        "pos_id": pos_id, "book_id": order["book_id"], "idea_uid": order["idea_uid"],
        "code": order["code"], "kind": idea["instrument"], "theme": idea["theme"],
        "horizon": idea["horizon"], "grade": idea["grade"],
        "qty": qty, "avg_px": fill_px, "cost": gross + fee,
        # `d` is the session that filled; `as_of` is the week this idea is
        # from. They differ whenever a period is booked late, and every
        # by-period read wants the second one.
        "opened_d": d, "as_of": idea["as_of"], "horizon_end": hz_end,
        "stop_px": idea["stop_px"], "take_px": idea["take_lo"],
        "status": "open", "fees": fee,
        "peak_px": fill_px, "trough_px": fill_px,
    }, ["pos_id"])
    db.upsert(con, "trades", {
        "trade_id": _oid("t", pos_id, d, "BUY"), "book_id": order["book_id"],
        "pos_id": pos_id, "idea_uid": order["idea_uid"], "d": d, "side": "BUY",
        "code": order["code"], "qty": qty, "px": fill_px, "gross": gross,
        "fee": fee, "cash_delta": -(gross + fee), "reason": rule,
    }, ["trade_id"])
    con.execute("UPDATE orders SET status='filled', fill_d=?, fill_px=?, fill_qty=?, "
                "fee=?, fill_rule=? WHERE order_id=?",
                (d, fill_px, qty, fee, rule, order["order_id"]))
    return pos_id


def _event_exit(con, pos: dict, d: str) -> bool:
    """An unacked thesis_invalidated alert from an earlier session."""
    return db.q1(con, "SELECT 1 x FROM alerts WHERE book_id=? AND idea_uid=? "
                      "AND kind='thesis_invalidated' AND d<? AND acked=0 LIMIT 1",
                 (pos["book_id"], pos["idea_uid"], d)) is not None


def _close_position(con, pos: dict, d: str, px: float, reason: str) -> None:
    idea = db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?", (pos["idea_uid"],))
    ccy = _currency(con, dict(idea))
    fx = _fx(ccy) or 1.0
    bps = _cost_bps(pos["code"], pos["kind"])
    exit_px = px * (1 - bps / 20000.0)
    gross = pos["qty"] * exit_px * fx
    fee = gross * (bps / 20000.0)
    proceeds = gross - fee
    realized = proceeds - pos["cost"]
    con.execute(
        "UPDATE positions SET status='closed', closed_d=?, close_px=?, realized=?, "
        "fees=fees+?, exit_reason=? WHERE pos_id=?",
        (d, exit_px, realized, fee, reason, pos["pos_id"]))
    db.upsert(con, "trades", {
        "trade_id": _oid("t", pos["pos_id"], d, "SELL"), "book_id": pos["book_id"],
        "pos_id": pos["pos_id"], "idea_uid": pos["idea_uid"], "d": d, "side": "SELL",
        "code": pos["code"], "qty": pos["qty"], "px": exit_px, "gross": gross,
        "fee": fee, "cash_delta": proceeds, "reason": reason,
    }, ["trade_id"])


# ---------------------------------------------------------------- step
def step(con, book_id: str, d: str, verbose: bool = False) -> dict:
    """Advance one session: fills, exits, marks, equity."""
    ev: dict[str, Any] = {"d": d, "book": book_id, "filled": [], "exits": [],
                          "expired": [], "alerts": 0}
    spec = book_spec(book_id)

    with db.tx(con):
        # ---- 1. pending orders
        pend = db.q(con, "SELECT * FROM orders WHERE book_id=? AND status='pending' "
                         "AND placed_d<=?", (book_id, d))
        for o in pend:
            order = dict(o)
            idea = dict(db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?",
                              (order["idea_uid"],)))
            res = _try_fill(con, order, d, idea)
            if res and res.get("arm"):
                # Breakout confirmed on today's close -> execute at tomorrow's open.
                con.execute("UPDATE orders SET kind='breakout_armed', note=? "
                            "WHERE order_id=?", (f"armed {d}", order["order_id"]))
                continue
            if res and res.get("px"):
                _apply_fill(con, order, idea, d, res["px"], res["rule"])
                ev["filled"].append({"idea": order["idea_uid"], "px": res["px"],
                                     "rule": res["rule"]})
            elif order["expire_d"] and d >= order["expire_d"]:
                con.execute("UPDATE orders SET status='expired' WHERE order_id=?",
                            (order["order_id"],))
                ev["expired"].append(order["idea_uid"])

        # armed breakouts fill at this session's open
        armed = db.q(con, "SELECT * FROM orders WHERE book_id=? AND kind='breakout_armed' "
                          "AND status='pending'", (book_id,))
        for o in armed:
            order = dict(o)
            if (order["note"] or "").endswith(d):
                continue                      # armed today, fills next session
            bar = futu_px.bar_on(con, order["code"], d)
            if not bar:
                continue
            idea = dict(db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?",
                              (order["idea_uid"],)))
            _apply_fill(con, order, idea, d, bar["open"], "breakout@next_open")
            ev["filled"].append({"idea": order["idea_uid"], "px": bar["open"],
                                 "rule": "breakout@next_open"})

        # ---- 2. exits on open positions
        # `opened_d<=?` is load-bearing: without it a re-run of the marking loop
        # applies later positions to earlier sessions, which silently inflates
        # day-one equity.
        for p in db.q(con, "SELECT * FROM positions WHERE book_id=? AND status='open' "
                           "AND opened_d<=?", (book_id, d)):
            pos = dict(p)
            idea = dict(db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?",
                              (pos["idea_uid"],)))
            m = mark_price(con, idea, d)
            if not m:
                continue
            hi = m.get("high", m["px"])
            lo = m.get("low", m["px"])
            con.execute("UPDATE positions SET peak_px=MAX(COALESCE(peak_px,0),?), "
                        "trough_px=MIN(COALESCE(trough_px,1e18),?) WHERE pos_id=?",
                        (hi, lo, pos["pos_id"]))

            reason = px = None
            # Whether risk rules apply is the book's own declaration, not an
            # inference from its entry style. The old inference ("market_close
            # entry means no stops") was true of the naive book and false of the
            # selector books, which enter at the close *and* carry σ-multiple
            # stops fixed at generation — inferring would have silently stripped
            # every stop the spec promises.
            if spec.get("stops", spec["entry"] != "market_close"):
                if pos["stop_px"] and m["px"] < pos["stop_px"]:
                    reason, px = "stop", m["px"]      # pack convention: 日收盘低于
                elif pos["take_px"] and hi >= pos["take_px"]:
                    reason, px = "take", pos["take_px"]
                elif _event_exit(con, pos, d):
                    # The third exit the spec promises: the theme's pre-registered
                    # price indicator has invalidated the thesis. Alerting without
                    # acting left the book long a position whose stated reason to
                    # exist was already gone — the alert fires on day t's data, so
                    # the exit is day t+1's close, never the same bar.
                    reason, px = "event", m["px"]
            if reason is None and pos["horizon_end"] and d >= pos["horizon_end"]:
                reason, px = "horizon", m["px"]
            if reason:
                _close_position(con, pos, d, px, reason)
                ev["exits"].append({"idea": pos["idea_uid"], "reason": reason, "px": px})

        # ---- 3. idle cash earns the shelf money-market yield.
        # Without this the disciplined book is penalised for the very discipline
        # under test: an unfilled entry band leaves capital in cash, and cash in
        # this account is not a 0% asset.
        _accrue_cash(con, book_id, d, spec["capital"])

        # ---- 4. marks and equity
        cash = _cash_on(con, book_id, d, spec["capital"])
        mv = 0.0
        n_open = 0
        for p in db.q(con, "SELECT * FROM positions WHERE book_id=? AND opened_d<=? "
                           "AND (status='open' OR closed_d>?)", (book_id, d, d)):
            pos = dict(p)
            idea = dict(db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?",
                              (pos["idea_uid"],)))
            m = mark_price(con, idea, d)
            if not m:
                continue
            fx = _fx(_currency(con, idea)) or 1.0
            val = pos["qty"] * m["px"] * fx
            mv += val
            n_open += 1
            upnl = val - pos["cost"]
            db.upsert(con, "mtm", {
                "book_id": book_id, "pos_id": pos["pos_id"], "d": d, "px": m["px"],
                "mv": val, "upnl": upnl,
                "upnl_pct": (upnl / pos["cost"] if pos["cost"] else None),
            }, ["book_id", "pos_id", "d"])

        equity = cash + mv
        prev = db.q1(con, "SELECT equity, cum_ret FROM equity WHERE book_id=? AND d<? "
                          "ORDER BY d DESC LIMIT 1", (book_id, d))
        prev_eq = prev["equity"] if prev else spec["capital"]
        ret_d = (equity / prev_eq - 1) if prev_eq else 0.0
        cum = equity / spec["capital"] - 1
        peak = db.q1(con, "SELECT MAX(equity) mx FROM equity WHERE book_id=? AND d<=?",
                     (book_id, d))["mx"] or equity
        peak = max(peak, equity)
        db.upsert(con, "equity", {
            "book_id": book_id, "d": d, "cash": cash, "mv": mv, "equity": equity,
            "ret_d": ret_d, "cum_ret": cum,
            "drawdown": (equity / peak - 1) if peak else 0.0,
            "n_open": n_open, "gross": (mv / equity if equity else 0.0),
        }, ["book_id", "d"])

    ev["equity"] = equity
    if verbose and (ev["filled"] or ev["exits"] or ev["expired"]):
        print(f"    {d} {book_id:<12} fills={len(ev['filled'])} "
              f"exits={len(ev['exits'])} expired={len(ev['expired'])} "
              f"equity=${ev['equity']:,.0f}")
    return ev


def _cash_on(con, book_id: str, d: str, capital: float) -> float:
    """Capital plus every cash movement up to and including `d`."""
    r = db.q1(con, "SELECT COALESCE(SUM(cash_delta),0) s FROM trades "
                   "WHERE book_id=? AND d<=?", (book_id, d))
    return capital + (r["s"] or 0.0)


def _accrue_cash(con, book_id: str, d: str, capital: float) -> float:
    """Credit one session of money-market interest on yesterday's cash balance.

    Booked as an explicit `INT` trade so the cash curve stays reconstructible
    from the trade blotter alone.
    """
    tid = _oid("int", book_id, d)
    if db.q1(con, "SELECT 1 FROM trades WHERE trade_id=?", (tid,)):
        return 0.0
    prev_d = _prev_session(con, d)
    if not prev_d:
        return 0.0
    cash = _cash_on(con, book_id, prev_d, capital)
    if cash <= 0:
        return 0.0
    y = olive.cash_yield(con, "USD")
    # Which rate this was is recorded, because it is not a detail. Interest on
    # the uninvested tranche is most of the book's reported return while the
    # ladder is still filling — currently +0.36 of +0.46 points — so whether it
    # came from the shelf or from a constant decides how much of that return was
    # measured at all. `cash_yield` exists precisely to replace the constant, and
    # it is falling back silently: the right product is on the shelf, JPMorgan
    # Liquidity Funds USD, but no Olive row carries `yield7d`.
    #
    # The scoring layer already does this for the same kind of gap, recording
    # `p_source: neutral_default` when a factor has no reading. A number that is
    # a default should say so wherever it lands.
    source = "shelf"
    if y is None:
        y, source = config.RISK_FREE_ANNUAL, "fallback_constant"
    days = max((date.fromisoformat(d) - date.fromisoformat(prev_d)).days, 1)
    interest = cash * y * days / 365.0
    db.upsert(con, "trades", {
        "trade_id": tid, "book_id": book_id, "pos_id": None, "idea_uid": None,
        "d": d, "side": "INT", "code": "CASH", "qty": None, "px": None,
        "gross": interest, "fee": 0.0, "cash_delta": interest,
        "reason": (f"MM yield {y*100:.3f}% x {days}/365"
                   + ("（兜底常数，货架未提供 7 日年化）"
                      if source == "fallback_constant" else "（货架中位数）")),
    }, ["trade_id"])
    return interest


def current_equity(con, book_id: str, d: str) -> float | None:
    r = db.q1(con, "SELECT equity FROM equity WHERE book_id=? AND d<=? "
                   "ORDER BY d DESC LIMIT 1", (book_id, d))
    return r["equity"] if r else None


# ---------------------------------------------------------------- run
def run(con, book_id: str, start: str, end: str, verbose: bool = True) -> dict:
    days = sessions_between(con, start, end)
    out = {"book": book_id, "sessions": len(days), "fills": 0, "exits": 0,
           "expired": 0, "from": start, "to": end}
    for d in days:
        ev = step(con, book_id, d, verbose=verbose)
        out["fills"] += len(ev["filled"])
        out["exits"] += len(ev["exits"])
        out["expired"] += len(ev["expired"])
    eq = db.q1(con, "SELECT * FROM equity WHERE book_id=? ORDER BY d DESC LIMIT 1",
               (book_id,))
    if eq:
        out.update(equity=eq["equity"], cum_ret=eq["cum_ret"],
                   drawdown=eq["drawdown"], n_open=eq["n_open"],
                   gross=eq["gross"], cash=eq["cash"], as_of=eq["d"])
    if verbose:
        print(f"  {book_id:<12} {out['sessions']}个交易日  fills={out['fills']} "
              f"exits={out['exits']} expired={out['expired']}  "
              f"equity=${out.get('equity', 0):,.0f} "
              f"({out.get('cum_ret', 0)*100:+.2f}%)  "
              f"gross={out.get('gross', 0)*100:.0f}%")
    return out


def reset_book(con, book_id: str) -> None:
    with db.tx(con):
        for t in ("orders", "positions", "trades", "equity", "mtm"):
            con.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
        con.execute("DELETE FROM alerts WHERE book_id=?", (book_id,))


def positions(con, book_id: str, status: str | None = None) -> list[dict]:
    sql = ("SELECT p.*, i.tool, i.tool_desc, i.horizon AS hz, i.or_c, i.or_k, "
           "i.ev_c, i.grade_rel, i.vol_check, i.view "
           "FROM positions p JOIN ideas i ON i.idea_uid=p.idea_uid WHERE p.book_id=?")
    args: list[Any] = [book_id]
    if status:
        sql += " AND p.status=?"
        args.append(status)
    return [dict(r) for r in db.q(con, sql + " ORDER BY p.opened_d, p.code", args)]
