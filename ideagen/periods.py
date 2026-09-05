"""The period spine — one row per weekly run, with what that week actually did.

The system is period-native and the interface was not. Every table carries
`as_of`, the mandate is a four-week rolling ladder (每周 25%，第五周换第一周),
and yet the dashboard could only ever show the newest week: `state()` returned a
*singular* `weekly`, the books returned one commingled ledger, and wherever the
two disagreed the page apologised in prose — "当前持仓建于上一期，按的是那一期
选出的主题" — because it had no axis to show the ladder on.

This module is that axis. It answers one question per period: what did this week
pick, what did it deploy, what is it worth now, and is it still on the books.
Five of those rows side by side *are* the ladder; nothing else has to draw it.

Three things it deliberately does not do:

* It does not invent the live/backfill distinction. That word already means
  something precise (`orch_runs.data_classification`, which the real backtest
  reads through `scripts/run_real_backtest.py:_periods`), and a second rule for
  the same word would be a second truth.
* It does not group by `opened_d`. That column records the session a position
  filled in, which equals its period only when the run was on time; the
  2026-09-04 catch-up stamped five periods with one afternoon. The vintage is
  `positions.as_of` (see `scripts/backfill_position_periods.py`).
* It does not decide what is "current". Callers pick; the spine just reports
  every period it can see, oldest first, which is the order the ladder draws in.

And one thing it insists on. A period has two different windows and they are not
the same window: the one it is *nominally* for (`as_of` → `horizon_end`, thirty
days) and the one it was *actually held* for. A catch-up run books a period long
after the fact, so the 2026-07-29 vintage opened and closed on 2026-09-04 with a
horizon that had already expired, and four more carry a single day of marks
against a thirty-day nominal window. Reporting only the nominal window draws six
full-length bars and invites every one of them to be read as a thirty-day result.
So `held_from` / `held_to` / `mark_days` travel beside `as_of` / `horizon_end`,
and the caller is expected to show both.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import config, db

#: Books the ladder is measured on. The `c:` cohort books are one-batch
#: experiments used by the vintage analytics in `payload.py`; the ten `sel-`
#: books are the ones the mandate actually runs.
BOOK_PREFIX = "sel-"


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


def spine(con=None, p=None, *, today: date | None = None) -> list[dict]:
    """Every weekly period, oldest first, with its own economics.

    `con` reads the book tables (positions, orders, mtm); `p.state` reads the
    orchestration tables (orch_runs, candidates, verdicts). On the local
    platform they are the same file, but on the cloud they are not, and mixing
    them up is how a dashboard ends up half-empty on one deployment only.
    """
    con = con or db.init()
    today = today or config.now_hkt().date()

    runs = _run_index(p) if p is not None else {}
    books = _book_index(con)
    held = _held_index(con)
    exits = _exit_index(con)
    themes = _theme_index(con)
    pending = _pending_index(con)

    keys = sorted(set(runs) | set(books))
    out: list[dict] = []
    for as_of in keys:
        run = runs.get(as_of) or {}
        bk = books.get(as_of) or {}
        cost = float(bk.get("cost") or 0.0)
        realized = float(bk.get("realized") or 0.0)
        unrealized = float(bk.get("unrealized") or 0.0)
        pnl = realized + unrealized
        n_open = int(bk.get("n_open") or 0)
        n_closed = int(bk.get("n_closed") or 0)
        horizon_end = bk.get("horizon_end") or run.get("horizon_end")
        hd = held.get(as_of) or {}

        days_left = None
        if horizon_end:
            try:
                days_left = (date.fromisoformat(horizon_end) - today).days
            except ValueError:
                days_left = None

        # Three states, and they are not the same question. "rolled" means the
        # ladder has finished with this vintage — every position closed. "live"
        # means it is still on the books. "pending" means the week ran (or
        # failed) and nothing was ever booked from it, which is the state a
        # reader most needs to be able to tell apart from a quiet week.
        if n_open > 0:
            status = "live"
        elif n_closed > 0:
            status = "rolled"
        else:
            status = "pending"

        out.append({
            "as_of": as_of,
            "classification": run.get("classification") or "live",
            "run_id": run.get("run_id"),
            "ok": run.get("ok"),
            "ended_at": run.get("ended_at"),
            "attempts": run.get("attempts") or 0,
            "n_topics": run.get("n_topics"),
            "n_candidates": run.get("n_candidates"),
            "horizon_end": horizon_end,
            "days_left": days_left,
            # The window this vintage was actually marked over, which is the
            # only window its P&L means anything across. A single mark day and
            # a thirty-day nominal window produce the same-looking percentage.
            "held_from": hd.get("held_from"),
            "held_to": hd.get("held_to"),
            "mark_days": int(hd.get("mark_days") or 0),
            # Booked after the week it is for. True for anything a catch-up run
            # filled in; the nominal window then describes a holding period that
            # never happened.
            "booked_late": bool(hd.get("held_from") and hd["held_from"] > as_of),
            "status": status,
            "n_open": n_open,
            "n_closed": n_closed,
            "n_positions": n_open + n_closed,
            # How this vintage left, broken out by reason. The mandate is a
            # rolling ladder: something enters every week and something leaves
            # every week, and a page that only draws the entering half describes
            # half a mechanism. `horizon` is the roll itself (held to term),
            # `stop` and `take` are the risk rules firing early.
            "exits": exits.get(as_of) or {},
            "exit_day": (exits.get(as_of) or {}).get("_last_d"),
            "n_instruments": int(bk.get("n_instruments") or 0),
            "n_books": int(bk.get("n_books") or 0),
            "pending_orders": int(pending.get(as_of) or 0),
            "cost": round(cost, 2),
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
            "pnl": round(pnl, 2),
            # Return on what this vintage actually deployed, not on the book.
            # A week that only found two ideas has a small denominator and its
            # percentage is not comparable to a fully deployed week's — which is
            # exactly why `cost` travels next to it.
            "ret": (round(pnl / cost, 6) if cost else None),
            "themes": themes.get(as_of) or [],
        })
    return out


def _run_index(p) -> dict[str, dict]:
    """Best weekly run per period, plus how many attempts it took.

    Ordered `ok DESC, started_at DESC`: a period that failed twice and then
    succeeded is a successful period, and the failures are the attempt count,
    not the verdict. Ordering by time alone would report the newest attempt,
    which after a retry storm is often a failure that a later run fixed.
    """
    rows = _rows(p.state.q(
        "SELECT run_id, as_of, ok, ended_at, data_classification "
        "FROM orch_runs WHERE kind='weekly' ORDER BY as_of, ok DESC, started_at DESC"))
    index: dict[str, dict] = {}
    for r in rows:
        as_of = str(r["as_of"])
        cur = index.setdefault(as_of, {"attempts": 0})
        cur["attempts"] += 1
        if "run_id" in cur:
            continue
        cur.update({
            "run_id": r["run_id"],
            "ok": bool(r["ok"]),
            "ended_at": r["ended_at"],
            # The one place this word is defined is `orch_runs`; see module docstring.
            "classification": r.get("data_classification") or "live",
        })

    for as_of, cur in index.items():
        rid = cur.get("run_id")
        if not rid:
            continue
        try:
            n = p.state.q("SELECT COUNT(*) n FROM candidates WHERE run_id=?", (rid,))
            cur["n_candidates"] = int(dict(n[0])["n"]) if n else None
        except Exception:  # noqa: BLE001 — a count is a caption, never a blocker
            cur["n_candidates"] = None
        try:
            import json as _json
            picked: set[str] = set()
            for v in p.state.q("SELECT chosen FROM verdicts WHERE run_id=? "
                               "AND kind='topic_scorer'", (rid,)):
                picked |= {str(x) for x in _json.loads(dict(v)["chosen"] or "[]")}
            cur["n_topics"] = len(picked) or None
        except Exception:  # noqa: BLE001
            cur["n_topics"] = None
    return index


def _book_index(con) -> dict[str, dict]:
    """Positions, cost and marks per period, over the ten selector books.

    Unrealized comes from the latest `mtm` row per position, the same join the
    books card uses — an unmarked position contributes nothing rather than a
    stale guess, so a period with no marks yet reads as 0, not as a loss.
    """
    rows = _rows(db.q(con, f"""
        SELECT p.as_of                              AS as_of,
               SUM(p.status = 'open')               AS n_open,
               SUM(p.status = 'closed')             AS n_closed,
               COUNT(DISTINCT p.code)               AS n_instruments,
               COUNT(DISTINCT p.book_id)            AS n_books,
               SUM(p.cost)                          AS cost,
               SUM(COALESCE(p.realized, 0))         AS realized,
               MIN(p.horizon_end)                   AS horizon_end,
               SUM(CASE WHEN p.status = 'open' THEN
                   (SELECT m.upnl FROM mtm m
                     WHERE m.pos_id = p.pos_id
                     ORDER BY m.d DESC LIMIT 1) ELSE 0 END) AS unrealized
          FROM positions p
         WHERE p.book_id LIKE '{BOOK_PREFIX}%' AND p.as_of IS NOT NULL
         GROUP BY p.as_of
    """))
    return {str(r["as_of"]): r for r in rows}


def _held_index(con) -> dict[str, dict]:
    """When each vintage was actually on the books, from the marks themselves.

    `mtm` has one row per position per marked session, so its span is the
    holding period as the book actually lived it — not as the schedule intended.
    Positions never marked fall back to their fill and close dates so a vintage
    booked moments ago still reports a window rather than a blank.
    """
    rows = _rows(db.q(con, f"""
        SELECT p.as_of                                  AS as_of,
               MIN(COALESCE(m.d0, p.opened_d))          AS held_from,
               MAX(COALESCE(m.d1, p.closed_d, p.opened_d)) AS held_to,
               MAX(COALESCE(m.n, 0))                    AS mark_days
          FROM positions p
          LEFT JOIN (SELECT pos_id, MIN(d) d0, MAX(d) d1, COUNT(DISTINCT d) n
                       FROM mtm GROUP BY pos_id) m ON m.pos_id = p.pos_id
         WHERE p.book_id LIKE '{BOOK_PREFIX}%' AND p.as_of IS NOT NULL
         GROUP BY p.as_of
    """))
    return {str(r["as_of"]): r for r in rows}


def _exit_index(con) -> dict[str, dict]:
    """Why each vintage's closed positions closed, and when the last one did.

    Three reasons, and they mean different things about the week: `horizon` is
    the four-week roll working as designed, while `stop` and `take` are the risk
    rules cutting a position short. A vintage that left entirely on `horizon`
    rolled off; one that left on `stop` was carried out.
    """
    out: dict[str, dict] = {}
    for r in db.q(con, f"""
        SELECT as_of, COALESCE(exit_reason, 'other') reason,
               COUNT(*) n, MAX(closed_d) last_d
          FROM positions
         WHERE book_id LIKE '{BOOK_PREFIX}%' AND as_of IS NOT NULL
           AND status = 'closed'
         GROUP BY as_of, COALESCE(exit_reason, 'other')
    """):
        row = out.setdefault(str(r["as_of"]), {})
        row[str(r["reason"])] = int(r["n"])
        if r["last_d"] and (not row.get("_last_d") or r["last_d"] > row["_last_d"]):
            row["_last_d"] = r["last_d"]
    return out


def _theme_index(con) -> dict[str, list[dict]]:
    """Which macro themes each period actually held, largest first.

    Read across periods this is the second thing the axis buys: a theme that
    runs through every week and one that appears for a single week are different
    kinds of call, and the commingled view cannot tell them apart.
    """
    out: dict[str, list[dict]] = {}
    for r in db.q(con, f"""
        SELECT as_of, theme, COUNT(*) n FROM positions
         WHERE book_id LIKE '{BOOK_PREFIX}%' AND as_of IS NOT NULL
           AND theme IS NOT NULL AND theme <> ''
         GROUP BY as_of, theme ORDER BY as_of, n DESC
    """):
        out.setdefault(str(r["as_of"]), []).append(
            {"theme": r["theme"], "n": int(r["n"])})
    return out


def _pending_index(con) -> dict[str, int]:
    """Orders placed for a period that never filled.

    A week showing thirty picks and nine positions is not obviously broken or
    obviously fine; this is the number that separates "still waiting for a fill"
    from "the money ran out".
    """
    return {str(r["as_of"]): int(r["n"]) for r in db.q(con, f"""
        SELECT as_of, COUNT(*) n FROM orders
         WHERE book_id LIKE '{BOOK_PREFIX}%' AND status = 'pending'
         GROUP BY as_of
    """)}


def latest(spine_rows: list[dict]) -> str | None:
    """The newest period that actually ran, which is not always the newest row.

    A period can exist in the spine because orders were placed against it while
    its run is still in flight. "Latest" for the purpose of a default view means
    the newest week whose run finished, so the page does not open on a period
    with no pipeline behind it.
    """
    done = [r["as_of"] for r in spine_rows if r.get("ok")]
    if done:
        return done[-1]
    return spine_rows[-1]["as_of"] if spine_rows else None


def find(spine_rows: list[dict], as_of: str | None) -> dict[str, Any] | None:
    for r in spine_rows:
        if r["as_of"] == as_of:
            return r
    return None
