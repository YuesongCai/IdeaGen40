"""Rebuild the whole daily record from scratch, in as-of order, once.

The original history was assembled incrementally, one day at a time, over a
window in which the code itself was still changing. Two defects followed:

  * **Stale idea bindings.** `idea_uid` is `<batch_id>#<local_id>`, so replacing
    a batch rebound every uid. 58 positions ended up on the wrong instruments and
    `settle` marked a $40 entry against a $192 close, booking +377% on one idea
    and dragging the published idea-level return from +0.96% to +5.70%. See
    `ideas.purge_batch`.

  * **Days scored before their full text had been retrieved.** `ingest` deep-
    fetches bodies for a few Tier1/2 items per source line over a rolling 3-day
    window, so a document published on 2026-07-26 could still gain a body on the
    2026-07-28 or 2026-07-29 run. Theme matching reads title + summary + body, and
    an item needs two distinct terms to count as evidence, so a late body can turn
    a non-match into evidence. Re-scoring 2026-07-27 today adds exactly two items
    to AI-CAPEX, both with bodies (1,765 and 723 chars), and loses none.

    Note what this is *not*: the corpus document count barely moved. An earlier
    diagnosis here claimed 2026-07-27 had been scored against 370 items where the
    window now holds 497 — that compared Tier 2+3 counting items against an
    all-tier total. The counting pool was 370 then and is 370 now.

This module walks the range once, forwards, so every day's scores, pack, ideas,
orders and marks derive from the same corpus and the same code.

Three choices, each of which could have gone the other way:

**Late-retrieved bodies are used, and that is not look-ahead.** A body is
published with its document; only our fetch of it was later. The replay reads
more deeply into what was already public on the day, which is a different thing
from reading the future. Prices remain hard-clamped: every quote in each pack
comes from a session closed by that day's 07:23 HKT generation stamp.

**Only the 16 seed themes participate.** `lexicon.all_themes(as_of)` enforces it,
since the two discovered themes register 2026-08-08. Replaying discovery day by
day was possible, but the registration decisions would be made now, by a model
that has already seen the outcome period, and a mechanical substitute would admit
the company names and report-series titles the semantic step exists to reject.
Discovery gets judged on the forward record instead, where nobody knows the
answer yet.

**Authored batches are preserved, not regenerated.** The 2026-07-27 PM pack and
the 2026-08-07 Claude batch cannot be reproduced by the rules engine; they are
the only two non-templated batches in the study. Their ideas are kept verbatim
and only re-traded. Every rules-generated day between them is rebuilt — and came
back byte-identical in return terms, which is the reproducibility check this
module also serves as.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from . import (analytics, briefing, config, db, generator, ideas as ideas_mod,
               lexicon, monitor, paper, scoring, universe)
from .sources import futu_px

#: Batches whose ideas are authored rather than generated. Re-traded, never rebuilt.
AUTHORED = ("B20260727", "B20260807")


def _corpus_report(con, days: list[str]) -> dict:
    """Per-day counting pool, and a check that no window is empty.

    An earlier version of this asserted that no document is published after the
    range end. That check was meaningless: `collect_evidence` selects on
    `published_d IN (window)`, so a later document can never enter an earlier
    window no matter how much corpus exists. What is worth verifying is the
    opposite failure — a window with almost nothing in it, which is how the
    original history came to be scored against a partly-filled corpus without
    anyone noticing.
    """
    out: dict[str, int] = {}
    for d in days:
        w = scoring._window(date.fromisoformat(d), config.OBSERVATION_WINDOW_DAYS)
        out[d] = db.q(con, "SELECT COUNT(*) n FROM documents WHERE published_d IN "
                           "(%s)" % ",".join("?" * len(w)), w)[0]["n"]
    thin = {d: n for d, n in out.items() if n < config.MIN_VALID_ITEMS}
    if thin:
        raise RuntimeError(f"windows with fewer than {config.MIN_VALID_ITEMS} "
                           f"documents: {thin}; ingest before replaying")
    return out


def _purge(con, days: list[str], verbose: bool) -> dict[str, int]:
    """Drop every derived row in range, keeping only authored ideas."""
    ids = [f"B{d.replace('-', '')}" for d in days]
    totals: dict[str, int] = {}
    # Enumerate books *before* touching `batches`. `paper.all_books` derives the
    # cohort list from that table, so purging first makes the cohort books
    # invisible and leaves their equity and mtm rows behind — which is how a
    # second replay produced ten cohorts all reporting the same 8-session holding
    # period: each was still carrying the previous run's curve from day one.
    books = paper.all_books(con)
    with db.tx(con):
        for bid in ids:
            if bid in AUTHORED:
                # Keep the ideas; drop only what trading produced from them.
                for t in ("outcomes", "alerts", "trades", "orders", "positions"):
                    n = con.execute(
                        f"DELETE FROM {t} WHERE idea_uid IN "
                        f"(SELECT idea_uid FROM ideas WHERE batch_id=?)",
                        (bid,)).rowcount
                    totals[t] = totals.get(t, 0) + n
            else:
                for k, v in ideas_mod.purge_batch(con, bid).items():
                    totals[k] = totals.get(k, 0) + v
                con.execute("DELETE FROM batches WHERE batch_id=?", (bid,))
        totals["mtm"] = con.execute(
            "DELETE FROM mtm WHERE pos_id NOT IN "
            "(SELECT pos_id FROM positions)").rowcount
        # The cohort *registrations* live in `books`, which reset_book does not
        # touch. Leaving them means `paper.all_books` reports all ten cohorts
        # from the first replayed day, so each gets stepped from that day rather
        # than from its own inception — every cohort then shows the same holding
        # period and the same benchmark, which is exactly the artefact that made
        # a 0-fill batch report an 8-session return.
        totals["books"] = con.execute(
            "DELETE FROM books WHERE book_id IN (%s)"
            % ",".join("?" * len(ids)),
            [config.cohort_book(b) for b in ids]).rowcount
    # Books are rebuilt from zero: a commingled book's equity path depends on the
    # order batches arrived, so partial resets would leave it incoherent.
    for b in books:
        paper.reset_book(con, b)
    # Sweep any book_id that no longer belongs to a live batch. reset_book only
    # clears books it is told about, and a cohort whose batch id changed would
    # otherwise keep a curve nothing can attribute.
    live = set(paper.all_books(con)) | set(books)
    with db.tx(con):
        for t in ("orders", "positions", "trades", "equity", "mtm", "alerts"):
            rows = con.execute(f"SELECT DISTINCT book_id FROM {t}").fetchall()
            for (bid,) in [(r[0],) for r in rows]:
                if bid not in live:
                    con.execute(f"DELETE FROM {t} WHERE book_id=?", (bid,))
    if verbose:
        print("  cleared " + "  ".join(f"{k}={v}" for k, v in totals.items() if v)
              + f"  · reset {len(books)} books")
    return totals


def run(con, start: date, end: date, verbose: bool = True) -> dict:
    universe.sync_registry(con)
    sessions = [r["d"] for r in db.q(
        con, "SELECT d FROM prices WHERE code=? AND d>=? AND d<=? ORDER BY d",
        ("US.SPY", start.isoformat(), end.isoformat()))]
    if not sessions:
        raise RuntimeError(f"no trading sessions between {start} and {end}")

    # A batch can exist on a date with no closed session yet — 2026-08-07 has a
    # full corpus and an authored batch, but the last US bar is 2026-08-06, so
    # its orders are legitimately still pending. Such a day must still be walked
    # (score, pack, open orders) or the batch would silently vanish from the
    # rebuilt record; it just has no book step.
    batch_days = [r["as_of"] for r in db.q(
        con, "SELECT DISTINCT as_of FROM batches WHERE as_of>=? AND as_of<=? "
             "ORDER BY as_of", (start.isoformat(), end.isoformat()))]
    days = sorted(set(sessions) | set(batch_days))
    unpriced = sorted(set(days) - set(sessions))

    pools = _corpus_report(con, days)

    rep: dict[str, Any] = {"from": days[0], "to": days[-1], "days": [],
                           "rebuilt": 0, "preserved": 0, "failed": [],
                           "pools": pools, "unpriced_days": unpriced}
    if verbose:
        print(f"replay {days[0]} → {days[-1]}  ({len(sessions)} sessions"
              + (f" + {len(unpriced)} not yet closed: {', '.join(unpriced)}"
                 if unpriced else "") + ")")
        print(f"  themes as of {days[0]}: "
              f"{len(lexicon.all_themes(date.fromisoformat(days[0])))}"
              f"  ·  as of {days[-1]}: "
              f"{len(lexicon.all_themes(date.fromisoformat(days[-1])))}"
              "   (discovered themes register 2026-08-08, so none apply here)")
        print(f"  counting pool per window: {min(pools.values()):,} … "
              f"{max(pools.values()):,}")
    _purge(con, days, verbose)

    for d in days:
        as_of = date.fromisoformat(d)
        bid = f"B{d.replace('-', '')}"
        row: dict[str, Any] = {"d": d, "batch": bid}
        if verbose:
            print(f"\n── {d} " + "─" * 46)
        try:
            sc = scoring.score_day(con, as_of, verbose=False, force=True)
            row["themes"] = len(sc.get("themes", []))
            pool = sc.get("pool", {})
            row["pool"] = pool.get("counting")
            if verbose and sc.get("themes"):
                t0 = sc["themes"][0]
                print(f"    scored {row['themes']} themes from "
                      f"{pool.get('counting'):,} counting items; top "
                      f"{t0['label']} TIS {t0['tis']:.1f}")

            # The instant a live run that day would have executed. Every quote in
            # the pack is clamped to sessions closed by then, so the replay
            # cannot price against its own future.
            gen_at = f"{d}T07:23:00+08:00"
            px_at = datetime.fromisoformat(gen_at)

            if bid in AUTHORED:
                n = db.q1(con, "SELECT COUNT(*) n FROM ideas WHERE batch_id=?",
                          (bid,))["n"]
                if not n:
                    raise RuntimeError(f"{bid} is authored but has no ideas")
                briefing.build(con, as_of, verbose=False, price_asof=px_at)
                row["mode"] = "preserved"
                rep["preserved"] += 1
                if verbose:
                    print(f"    preserved {n} authored ideas (not regenerated)")
            else:
                briefing.build(con, as_of, verbose=False, price_asof=px_at)
                payload = generator.generate(con, as_of, verbose=False,
                                             price_asof=px_at, rebuild_pack=True)
                _, _, val = ideas_mod.build_batch(
                    con, payload, as_of, generator=generator.GENERATOR,
                    batch_id=bid, generated_at=gen_at)
                row["mode"] = "rebuilt"
                row["validation"] = {"pass": val["pass"], "errors": val["n_errors"],
                                     "warnings": val["n_warnings"]}
                if not val["pass"]:
                    rep["failed"].append(
                        {"d": d, "why": "validation",
                         "checks": [c["check"] for c in val["checks"]
                                    if not c["ok"] and c["severity"] == "error"]})
                    row["traded"] = False
                    rep["days"].append(row)
                    if verbose:
                        print(f"    ! validation failed "
                              f"({val['n_errors']}E) — not traded")
                    continue
                rep["rebuilt"] += 1
                if verbose:
                    s = val["summary"]
                    print(f"    rebuilt 40 ideas  {val['n_errors']}E/"
                          f"{val['n_warnings']}W  grades {s['grades']}")

            for b in config.BOOKS:
                paper.open_batch(con, bid, b, verbose=False)
            paper.open_cohort(con, bid, verbose=False)
            row["traded"] = True

            if d in unpriced:
                row["note"] = "no closed session yet; orders pending"
                if verbose:
                    print("    orders placed, pending (session not closed)")
                rep["days"].append(row)
                continue

            # Step every book that has actually placed an order by now. Cohorts
            # opened on earlier days must keep marking, or each freezes at its own
            # inception — but a book with nothing placed yet must not get an
            # equity row, or its curve starts before it existed.
            for b in paper.all_books(con):
                first = db.q1(con, "SELECT MIN(placed_d) d FROM orders "
                                   "WHERE book_id=?", (b,))
                if not (first and first["d"] and first["d"] <= d):
                    continue
                paper.step(con, b, d, verbose=False)
            monitor.run(con, d, verbose=False)
            eq = db.q1(con, "SELECT equity,cum_ret,gross,n_open FROM equity "
                            "WHERE book_id='naive' AND d=?", (d,))
            row["naive"] = dict(eq) if eq else None
            if verbose and eq:
                print(f"    naive ${eq['equity']:>12,.0f} "
                      f"{eq['cum_ret']*100:+.2f}%  gross {eq['gross']*100:.0f}%"
                      f"  open {eq['n_open']}")
        except Exception as e:  # noqa: BLE001 — one bad day must not lose the rest
            row["error"] = f"{type(e).__name__}: {e}"
            rep["failed"].append({"d": d, "why": row["error"]})
            if verbose:
                print(f"    ! {row['error']}")
        rep["days"].append(row)

    # Mark every book forward to the last closed session, each from *its own*
    # inception. Starting every book at days[0] gives each cohort an equity curve
    # from the first replayed day rather than from the day its batch was placed,
    # which made all ten cohorts report the same 8-session holding period and the
    # same +3.99% SPY comparison — including the one with no fills at all.
    last = futu_px.complete_through("US")
    for b in paper.all_books(con):
        first = db.q1(con, "SELECT MIN(placed_d) d FROM orders WHERE book_id=?", (b,))
        paper.run(con, b, (first["d"] if first and first["d"] else last), last,
                  verbose=False)
    bad = ideas_mod.instrument_mismatches(con)
    if bad:
        raise RuntimeError(f"replay finished with {len(bad)} instrument "
                           f"mismatches; refusing to settle")
    st = analytics.settle(con, verbose=False)
    rep["outcomes"] = st.get("n") if isinstance(st, dict) else None
    rep["marked_to"] = last

    if verbose:
        print(f"\nreplay done: {rep['rebuilt']} rebuilt, "
              f"{rep['preserved']} preserved, {len(rep['failed'])} failed; "
              f"marked to {last}")
        for f in rep["failed"]:
            print(f"  ! {f['d']}: {f['why']}")
    db.kv_set(con, f"replay:{days[0]}..{days[-1]}", rep)
    return rep
