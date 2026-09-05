"""Tradeable instruments from the local registry and the Olive shelf."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .. import db, universe as uni
from ..feeds import register


@register("instruments", "universe", label="上市标的 + Olive 货架", required=True)
def instruments(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Everything the system could trade, with whether it can be marked.

    `priceable` is carried explicitly because an instrument that cannot be marked
    must never reach a book — marking it at cost would insert a free 0% return.

    So is `vehicle`, for the same class of reason. The mandate gate reads it, and
    a row that arrives without one is refused as 「载体未确认」. This feed already
    held the answer — it parses `meta` for `exposure` two lines down — and simply
    did not pass it on, so the gate fell through to a lookup in the in-process
    registry. That registry is a module-level global filled by `universe.hydrate`,
    which nothing on the weekly path calls: in a long-lived scheduler some earlier
    job had usually hydrated it, and in a cold `ideagen weekly` nothing had. Same
    database, same period, two different universes — 220 instruments or 86 — with
    no error either way, because「载体未确认」is a perfectly reasonable-looking
    line to find in a journal. A gate's input belongs in the row it judges, not in
    whatever state the process happens to have accumulated first.
    """
    con = db.init()
    # The quota blocklist is the live truth about what OpenD will serve; the
    # `priceable` column is a cache of it that can fall behind (US.XLF sat on
    # the blocklist with priceable=1, reached the pool, was picked, and then
    # failed whole batches at booking because it has no price rows at all).
    # Reading both and taking the stricter answer costs one kv read.
    from ..sources import futu_px
    blocked = futu_px.quota_blocked(con)
    for r in db.q(con, "SELECT key, name, kind, futu_code, olive_key, currency, "
                       "       COALESCE(priceable,0) AS priceable, meta "
                       "FROM instruments ORDER BY kind, key"):
        meta = db.jl(r["meta"], {}) or {}
        yield {
            "instrument_id": r["key"],
            "name": r["name"] or r["key"],
            "kind": r["kind"] or "listed",
            "priceable": bool(r["priceable"]) and r["futu_code"] not in blocked,
            "currency": r["currency"],
            "exposure": meta.get("exposure"),
            "vehicle": uni.vehicle_of(meta),
            "futu_code": r["futu_code"],
            "olive_key": r["olive_key"],
        }
