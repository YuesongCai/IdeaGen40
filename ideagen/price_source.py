"""Pick the price source, so marking never depends on a desktop gateway.

`futu_px` (OpenD) gives forward-adjusted, intraday-grade bars but only on a
machine with the gateway up. `fmp_px` gives back-adjusted EOD bars from a cloud
API that needs nothing local. Both write `prices` on the same convention and
`paper.run` reads only that table, so the choice is invisible downstream.

`IDEAGEN_PRICE_SOURCE` pins one: `futu` | `fmp` | `auto` (default). `auto` uses
OpenD when its port actually answers and FMP otherwise — which is what makes the
cloud monitor (and a daily run with the Mac off) mark books: no OpenD there, so
`auto` resolves to FMP on its own. The OpenD check is a plain socket probe with a
timeout we control, never the futu SDK, which blocks and retries forever against a
closed port (see `scheduler._opend_reachable`).
"""
from __future__ import annotations

import os

from . import config
from .sources import fmp, fmp_px, futu_px


def opend_reachable(timeout_s: float = 1.5) -> bool:
    import socket
    try:
        with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT),
                                      timeout=timeout_s):
            return True
    except OSError:
        return False


def chosen() -> str:
    """`futu` | `fmp` — the source `sync` will use right now."""
    pin = (os.environ.get("IDEAGEN_PRICE_SOURCE") or "auto").strip().lower()
    if pin in ("futu", "fmp"):
        return pin
    if opend_reachable():
        return "futu"
    return "fmp" if fmp.configured() else "futu"


def sync(con, codes, start, end, verbose=False, **kw):
    """Dispatch to the chosen source. Same signature/return shape as futu_px.sync."""
    src = chosen()
    if src == "fmp":
        rep = fmp_px.sync(con, codes, start, end, verbose=verbose)
    else:
        rep = futu_px.sync(con, codes, start, end, verbose=verbose, **kw)
    if isinstance(rep, dict):
        rep.setdefault("source", src)
    return rep
