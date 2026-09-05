#!/usr/bin/env python3
"""Fill `positions.as_of` — the vintage a position belongs to.

`opened_d` is the session a position filled in. That is the same thing as its
period only when the weekly run happened on time; a catch-up run books several
periods on one afternoon and stamps all of them with that afternoon. Grouping
the four-week ladder by `opened_d` therefore collapses five live vintages into
one date, and any UI that does it reports a portfolio that does not exist.

The period is carried by the idea (`ideas.as_of`), and the order that opened the
position carries the same value. This script writes it onto the position and
refuses to guess: a row whose two sources disagree, or which has neither, is
reported and left alone rather than filled with a plausible date.

Idempotent — safe to run on every deploy and safe to run twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ideagen import db  # noqa: E402


def backfill(con, *, dry_run: bool = False) -> dict:
    rows = db.q(con, """
        SELECT p.pos_id, p.book_id, p.opened_d, p.as_of AS have,
               i.as_of AS idea_as_of,
               (SELECT o.as_of FROM orders o
                 WHERE o.book_id = p.book_id AND o.idea_uid = p.idea_uid
                   AND o.status = 'filled' LIMIT 1) AS order_as_of
          FROM positions p LEFT JOIN ideas i USING(idea_uid)
    """)
    todo, conflicts, orphans, already = [], [], [], 0
    for r in rows:
        idea_p, order_p = r["idea_as_of"], r["order_as_of"]
        # Both sources present and disagreeing means the join itself is wrong;
        # writing either one would launder a real inconsistency into a fact.
        if idea_p and order_p and idea_p != order_p:
            conflicts.append((r["pos_id"], idea_p, order_p))
            continue
        period = idea_p or order_p
        if not period:
            orphans.append(r["pos_id"])
            continue
        if r["have"] == period:
            already += 1
            continue
        todo.append((period, r["pos_id"]))

    if todo and not dry_run:
        with db.tx(con):
            con.executemany("UPDATE positions SET as_of=? WHERE pos_id=?", todo)

    drift = db.q(con, """
        SELECT COUNT(*) n FROM positions
         WHERE as_of IS NOT NULL AND opened_d IS NOT NULL AND as_of <> opened_d
    """)
    return {"scanned": len(rows), "written": len(todo), "already": already,
            "conflicts": conflicts, "orphans": len(orphans),
            "mis_stamped": (drift[0]["n"] if drift else 0)}


def main() -> int:
    dry = "--dry-run" in sys.argv
    con = db.init()
    res = backfill(con, dry_run=dry)
    verb = "would write" if dry else "wrote"
    print(f"positions scanned {res['scanned']} · {verb} {res['written']} · "
          f"already correct {res['already']} · orphans {res['orphans']}")
    print(f"rows whose period differs from opened_d: {res['mis_stamped']} "
          f"(these are exactly the ones opened_d was lying about)")
    if res["conflicts"]:
        print(f"\n{len(res['conflicts'])} rows left alone — idea and order "
              f"disagree on the period:")
        for pos_id, a, b in res["conflicts"][:20]:
            print(f"  {pos_id}  ideas.as_of={a}  orders.as_of={b}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
