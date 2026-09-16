"""WS-D: what a read-only (`viewer`) account may do on the dashboard server.

One decision function, called once at the top of each verb handler in
`serve.py`, rather than a role check sprinkled into every write endpoint. The
sprinkled version is how a new POST route ships without the check: the person
adding `/api/whatever` copies the nearest handler, and the nearest handler was
written before viewers existed. Here the default for a viewer is *refuse*, and
the exceptions are listed — a route added tomorrow is closed to viewers until
someone writes it into the allow list on purpose.

Why these exceptions and no others:

* `/login`, `/logout` — a viewer has to be able to enter and leave.
* `/account/password`, `/account/revoke` — managing one's *own* credential is
  not writing to the research record. Refusing it would leave an outside reader
  unable to rotate a password we handed them, which is worse security, not more.

Everything else that is a POST — PM decisions, philosophy cards and drafts, the
「问 AI」 model call (it costs money and writes the ask log), Olive sync — is
refused. Two GETs start side effects and are refused too: the Olive OAuth start
(it would bind *our* data source to whoever clicks) and nothing else today.
"""
from __future__ import annotations

#: POST paths a viewer may still use: getting in and out, and their own password.
VIEWER_POST_ALLOWED = frozenset({"/login", "/logout",
                                 "/account/password", "/account/revoke"})

#: GET paths that are not reads. Kept explicit so a reviewer can see the list.
VIEWER_GET_REFUSED = frozenset({"/api/olive/oauth/start"})

REFUSAL = "只读账号不能执行写入操作（记录决定、写准则、问 AI、同步数据源都不行）"


def viewer_refused(method: str, path: str) -> bool:
    """True when a viewer must be refused this request. Pure; never raises."""
    m = (method or "").upper()
    p = (path or "").split("?", 1)[0]
    if m == "GET" or m == "HEAD":
        return p in VIEWER_GET_REFUSED
    return p not in VIEWER_POST_ALLOWED


def refused_for(accounts_mod, who: str | None, method: str, path: str) -> bool:
    """The whole check: is this session a viewer, and is this request a write.

    `who` is the verified session user (or None for key / loopback access,
    which are not accounts and therefore not viewers). A lookup failure is
    treated as *not* a viewer only because the caller already required a valid
    session to get here; the account store being unreadable makes `check()`
    return None first, so a viewer never slips through on an I/O error.
    """
    if not who:
        return False
    try:
        return bool(accounts_mod.is_viewer(who)) and viewer_refused(method, path)
    except Exception:  # noqa: BLE001 — fail closed for a known session
        return viewer_refused(method, path)
