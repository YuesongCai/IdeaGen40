"""Tradeable instruments from the local registry and the Olive shelf."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .. import db
from ..feeds import register


@register("instruments", "universe", label="上市标的 + Olive 货架", required=True)
def instruments(as_of: date, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Everything the system could trade, with whether it can be marked.

    `priceable` is carried explicitly because an instrument that cannot be marked
    must never reach a book — marking it at cost would insert a free 0% return.
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
            "futu_code": r["futu_code"],
            "olive_key": r["olive_key"],
        }
