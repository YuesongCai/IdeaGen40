"""Browser-mediated Olive OAuth for the private dashboard.

The CLI flow uses a loopback callback and is appropriate for a laptop. This
module keeps the PKCE verifier on the dashboard server so a remote operator can
authorize through Noah SSO without running Python locally.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import config, platform as platform_mod, shelf_store
from .sources import olive

CALLBACK_PATH = "/api/olive/oauth/callback"
SESSION_TTL_SECONDS = 600

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}
_sync: dict[str, Any] = {"state": "idle"}


def public_base_url(value: str | None = None) -> str:
    raw = (value if value is not None
           else os.environ.get("IDEAGEN_PUBLIC_SITE", "")).strip().rstrip("/")
    parsed = urlsplit(raw)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError(
            "IDEAGEN_PUBLIC_SITE must be an HTTPS origin, for example "
            "https://dashboard.example.com"
        )
    return raw


def callback_url(public_site: str | None = None) -> str:
    return public_base_url(public_site) + CALLBACK_PATH


def _expire_pending(now: float) -> None:
    expired = [
        state for state, session in _pending.items()
        if now - float(session["created_at"]) > SESSION_TTL_SECONDS
    ]
    for state in expired:
        _pending.pop(state, None)


def begin_authorization(public_site: str | None = None) -> str:
    """Create a one-time PKCE session and return the Noah SSO URL."""
    redirect_uri = callback_url(public_site)
    registered = olive.register_oauth_client(redirect_uri)
    client_id = str(registered["client_id"])
    url, verifier, state = olive.oauth_authorization(
        client_id,
        redirect_uri,
        issuer=config.OLIVE_OAUTH_ISSUER,
        resource_url=config.OLIVE_MCP_URL,
    )
    now = time.time()
    with _lock:
        _expire_pending(now)
        _pending[state] = {
            "created_at": now,
            "verifier": verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
    return url


def _consume_session(state: str) -> dict[str, Any]:
    now = time.time()
    with _lock:
        _expire_pending(now)
        session = _pending.pop(state, None)
    if not session:
        raise ValueError("authorization session is invalid or expired")
    return session


def complete_authorization(query: str) -> dict[str, Any]:
    """Exchange one callback code, verify MCP, persist tokens, and start sync."""
    values = parse_qs(query, keep_blank_values=True)
    state = (values.get("state") or [""])[0]
    session = _consume_session(state)
    if error := (values.get("error_description")
                 or values.get("error") or [""])[0]:
        raise ValueError(f"Noah SSO denied authorization: {error}")
    code = (values.get("code") or [""])[0]
    if not code:
        raise ValueError("authorization callback has no code")

    tokens = olive.exchange_oauth_code(
        code,
        str(session["verifier"]),
        str(session["client_id"]),
        str(session["redirect_uri"]),
        token_url=config.OLIVE_OAUTH_TOKEN_URL,
        resource_url=config.OLIVE_MCP_URL,
    )
    access_token = str(tokens["access_token"])
    refresh_token = str(tokens.get("refresh_token") or "")
    client = olive.OliveMCP(
        access_token=access_token,
        refresh_token=refresh_token,
        client_id=str(session["client_id"]),
    )
    info = client.initialize()
    tools = client.tools()
    expires = int(tokens.get("expires_in") or 0)
    config.store_olive_credentials({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_id": session["client_id"],
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=expires)
        ).isoformat() if expires else "",
        "issuer": config.OLIVE_OAUTH_ISSUER,
        "resource": config.OLIVE_MCP_URL,
        "redirect_uri": session["redirect_uri"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    start_sync()
    server = info.get("serverInfo") or {}
    return {
        "ok": True,
        "server": {
            "name": server.get("name"),
            "version": server.get("version"),
        },
        "tool_count": len(tools),
    }


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(
        r"(?i)(access_token|refresh_token|authorization|code)=?[^\s&]+",
        r"\1=REDACTED",
        text,
    )
    return text[:240]


def _sync_worker() -> None:
    global _sync
    try:
        client = olive.OliveMCP()
        snapshot = olive.pull_snapshot(client, detail_limit=1)
        platform = platform_mod.load()
        result = shelf_store.persist(
            platform,
            snapshot,
            as_of=config.today_hkt(),
            source=shelf_store.LIVE_SOURCE,
            classification=shelf_store.LIVE_CLASSIFICATION,
        )
        with _lock:
            _sync = {
                "state": "ok",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "items": int(result.get("items") or 0),
                "navs": int(result.get("navs") or 0),
                "artifact_archived": bool(result.get("artifact_uri")),
            }
    except Exception as exc:  # noqa: BLE001 - surfaced as a bounded status
        with _lock:
            _sync = {
                "state": "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": _safe_error(exc),
            }


def start_sync() -> bool:
    """Start one bounded catalog sync; return False if one is already running."""
    global _sync
    with _lock:
        if _sync.get("state") == "running":
            return False
        _sync = {
            "state": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(
        target=_sync_worker,
        name="olive-initial-sync",
        daemon=True,
    ).start()
    return True


def _boot_log(lines: int = 14) -> list[str]:
    """The tail of the instance's last boot, for a host with no shell.

    The bootstrap serves this log on :80 only until the proxy takes that port,
    which is to say it disappears the moment the stack is healthy. The copy in
    the oauth mount is the only view of it that outlives the boot.
    """
    token_file = config.olive_token_file()
    if token_file is None:
        return []
    try:
        text = (token_file.parent / "bootstrap.log").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]


def status() -> dict[str, Any]:
    credentials = config.olive_credentials()
    live_snapshot: dict[str, Any] | None = None
    error: str | None = None
    try:
        platform = platform_mod.load()
        live_snapshot = shelf_store.latest_snapshot(
            platform.state,
            as_of=config.today_hkt(),
            classification=shelf_store.LIVE_CLASSIFICATION,
            source=shelf_store.LIVE_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001 - status must remain available
        message = str(exc).lower()
        missing_table = (
            "shelf_snapshots" in message
            and any(marker in message for marker in (
                "no such table", "doesn't exist", "does not exist",
                "undefined table",
            ))
        )
        if not missing_table:
            error = _safe_error(exc)
    with _lock:
        sync = dict(_sync)
    snapshot = None
    if live_snapshot:
        snapshot = {
            "as_of": live_snapshot.get("as_of"),
            "captured_at": live_snapshot.get("captured_at"),
            "items": int(live_snapshot.get("item_count") or 0),
            "navs": int(live_snapshot.get("nav_count") or 0),
            "artifact_archived": bool(live_snapshot.get("artifact_uri")),
        }
    token_file = config.olive_token_file()
    token_state = "unset"
    if token_file is not None:
        try:
            token_state = "present" if token_file.is_file() else "absent"
        except OSError as exc:
            token_state = f"unreadable ({exc.__class__.__name__})"
    return {
        # Enough to tell "Olive was never configured here" apart from
        # "configured but the token store is unreachable" without a shell on
        # the box. Names and counts only; no value ever leaves this function.
        "endpoint_set": bool(config.OLIVE_MCP_URL),
        "issuer_set": bool(config.OLIVE_OAUTH_ISSUER),
        "token_file": token_state,
        "boot_log": _boot_log(),
        "credential_keys": sorted(credentials),
        "configured": bool(credentials.get("access_token")),
        "refreshable": bool(credentials.get("refresh_token")),
        "expires_at": credentials.get("expires_at") or None,
        "sync": sync,
        "live_snapshot": snapshot,
        "status_error": error,
    }


def reset_for_tests() -> None:
    global _sync
    with _lock:
        _pending.clear()
        _sync = {"state": "idle"}
