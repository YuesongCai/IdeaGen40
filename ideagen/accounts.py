"""Named accounts for the dashboard, replacing a single shared secret.

`IDEAGEN_DASH_KEY` is a capability: whoever holds it is in, and the server can
never say who that was. That is the right shape for a machine — the proxy, a
health probe, a script — and the wrong shape for a person. It cannot answer any
of the questions an operator actually has: who is looking at this, how do I
change my own password, how do I add my colleague, how do I take access away
from someone who left. The key stays for machines; people get accounts.

Design notes, because each choice here is load-bearing:

*Storage* is one JSON file beside the OAuth tokens, on the host volume that
already survives container restarts. Not the database: this has to work when the
database is the thing that is broken, and locking yourself out of the dashboard
during an incident is exactly when you need it.

*Hashing* is `hashlib.scrypt` from the standard library, not bcrypt. The image
deliberately carries no crypto dependency, and scrypt with these parameters is a
better password hash than bcrypt anyway.

*Sessions* are signed, not stored. A cookie carries `user|epoch|expiry` and an
HMAC over it, so verifying costs no I/O and there is no session table to grow or
to clean. Revocation is the `epoch` counter: bumping a user's epoch invalidates
every cookie ever issued to them, which is what "sign out everywhere" and "reset
their password" both need.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

#: Where the accounts live. The compose file mounts /opt/ideagen/oauth (0700 on
#: the host) at this path, so the file survives image rebuilds and container
#: replacement. A laptop has no such mount and falls back to the data dir.
def _store_path() -> Path:
    explicit = os.environ.get("IDEAGEN_ACCOUNTS_FILE")
    if explicit:
        return Path(explicit)
    run_dir = Path("/run/ideagen-oauth")
    if run_dir.is_dir():
        return run_dir / "accounts.json"
    from . import config
    return Path(getattr(config, "DATA", "data")) / "accounts.json"


SESSION_COOKIE = "ideagen_session"
SESSION_DAYS = 30
#: n=2**15 costs ~32MB, which is exactly OpenSSL's default `maxmem` — leaving it
#: implicit raises ValueError instead of returning a hash, so it is passed.
_SCRYPT = {"n": 1 << 15, "r": 8, "p": 1, "dklen": 32, "maxmem": 1 << 26}


def _secret() -> bytes:
    """The HMAC key for session cookies.

    Derived from the dash key rather than being a second secret to lose, but
    never the key itself — see below. Rotating the key invalidates every
    session, which is the correct behaviour rather than a side effect.
    """
    from .platform.local import EnvSecretStore
    from . import platform as plat_mod
    key = (os.environ.get("IDEAGEN_DASH_KEY")
           or EnvSecretStore(plat_mod._ENV_FILES).get("IDEAGEN_DASH_KEY",
                                                      required=False))
    # Derived, not used directly. The dash key is designed to travel — a query
    # parameter, an X-Dash-Key header, a cookie, runtime.env, a presigned URL —
    # so it leaks far more easily than a value that never leaves this process.
    # Signing sessions with it would turn "they can get in" into "they can forge
    # any user for any duration". The label also leaves somewhere to stand when
    # sessions need rotating without rotating the key.
    return hmac.new((key or "ideagen-no-key").encode(), b"session-v1",
                    hashlib.sha256).digest()


def load() -> dict[str, Any]:
    p = _store_path()
    if not p.exists():
        return {"users": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "users" not in data:
            return {"users": {}}
        return data
    except Exception:  # noqa: BLE001 — a corrupt file must not lock everyone out
        return {"users": {}}


def save(data: dict[str, Any]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _hash(password: str, salt: bytes) -> str:
    return base64.b64encode(
        hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)).decode()


def list_users() -> list[dict[str, Any]]:
    users = load()["users"]
    return [{"name": n, "admin": bool(u.get("admin")),
             "created": u.get("created"), "last_login": u.get("last_login")}
            for n, u in sorted(users.items())]


def add_user(name: str, password: str, *, admin: bool = False) -> None:
    name = name.strip()
    if not name or len(name) > 64:
        raise ValueError("用户名不能为空，且不超过 64 字符")
    if len(password) < 8:
        raise ValueError("口令至少 8 位")
    data = load()
    if name in data["users"]:
        raise ValueError(f"用户 {name} 已存在")
    salt = secrets.token_bytes(16)
    data["users"][name] = {
        "salt": base64.b64encode(salt).decode(),
        "hash": _hash(password, salt),
        "admin": admin,
        "epoch": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save(data)


def set_password(name: str, password: str) -> None:
    """Change a password and, with it, end every session that used the old one.

    Bumping the epoch is the point: a password change that leaves the old
    sessions alive has not actually taken access away from anyone.
    """
    if len(password) < 8:
        raise ValueError("口令至少 8 位")
    data = load()
    u = data["users"].get(name)
    if not u:
        raise ValueError(f"没有用户 {name}")
    salt = secrets.token_bytes(16)
    u["salt"] = base64.b64encode(salt).decode()
    u["hash"] = _hash(password, salt)
    u["epoch"] = int(u.get("epoch", 1)) + 1
    save(data)


def remove_user(name: str) -> None:
    data = load()
    if name not in data["users"]:
        raise ValueError(f"没有用户 {name}")
    if len(data["users"]) == 1:
        raise ValueError("这是最后一个账号，删掉就没人能进来了")
    del data["users"][name]
    save(data)


def revoke_sessions(name: str) -> None:
    data = load()
    u = data["users"].get(name)
    if not u:
        raise ValueError(f"没有用户 {name}")
    u["epoch"] = int(u.get("epoch", 1)) + 1
    save(data)


#: Failed logins per client address. Without this the password is guessable at
#: HTTP speed, and scrypt only makes each guess cost the *server* 32MB — it does
#: not stop the attacker. In memory on purpose: a restart forgiving the counter
#: is a smaller problem than a lockout table to administer, and the server that
#: restarts is the one being attacked, so the attacker gains a few seconds.
_FAILS: dict[str, tuple[int, float]] = {}
_FAILS_LOCK = threading.Lock()
_BACKOFF_AFTER = 3
_BACKOFF_CAP_S = 300.0


def throttle_wait(client: str) -> float:
    """Seconds this client must wait before another attempt, 0 if it may try."""
    with _FAILS_LOCK:
        n, until = _FAILS.get(client, (0, 0.0))
    return max(0.0, until - time.time())


def throttle_fail(client: str) -> None:
    with _FAILS_LOCK:
        n, _ = _FAILS.get(client, (0, 0.0))
        n += 1
        delay = 0.0 if n <= _BACKOFF_AFTER else min(
            _BACKOFF_CAP_S, 2.0 ** (n - _BACKOFF_AFTER))
        _FAILS[client] = (n, time.time() + delay)


def throttle_ok(client: str) -> None:
    with _FAILS_LOCK:
        _FAILS.pop(client, None)


def verify(name: str, password: str) -> bool:
    u = load()["users"].get(name)
    if not u:
        # Hash anyway, so a missing user and a wrong password take the same
        # time. Otherwise the login form answers "does this account exist".
        _hash(password, b"decoy-salt-000000")
        return False
    expect = _hash(password, base64.b64decode(u["salt"]))
    return hmac.compare_digest(expect, u["hash"])


def note_login(name: str) -> None:
    data = load()
    u = data["users"].get(name)
    if u:
        u["last_login"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save(data)


def is_admin(name: str) -> bool:
    u = load()["users"].get(name)
    return bool(u and u.get("admin"))


def issue(name: str, *, days: int = SESSION_DAYS) -> str:
    u = load()["users"].get(name)
    epoch = int((u or {}).get("epoch", 1))
    payload = f"{name}|{epoch}|{int(time.time()) + days * 86400}"
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def check(token: str | None) -> str | None:
    """The username this cookie proves, or None. Never raises."""
    if not token or "." not in token:
        return None
    raw, _, sig = token.rpartition(".")
    expect = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        pad = "=" * (-len(raw) % 4)
        name, epoch, exp = base64.urlsafe_b64decode(raw + pad).decode().split("|")
        if int(exp) < time.time():
            return None
        u = load()["users"].get(name)
        if not u or int(u.get("epoch", 1)) != int(epoch):
            return None
        return name
    except Exception:  # noqa: BLE001 — a malformed cookie is simply not a session
        return None


def bootstrap() -> str | None:
    """Create the first account from the deployment's own configuration.

    A fresh instance would otherwise have a login page and nobody able to use
    it. `IDEAGEN_DASH_USER` / `IDEAGEN_DASH_PASSWORD` are already delivered in
    runtime.env for exactly one person — the operator — so the first boot turns
    them into a real account and everything afterwards is managed in the UI.
    Returns the name created, or None if there was nothing to do.
    """
    data = load()
    if data["users"]:
        return None
    name = (os.environ.get("IDEAGEN_DASH_USER") or "").strip()
    password = os.environ.get("IDEAGEN_DASH_PASSWORD") or ""
    if not name or len(password) < 8:
        return None
    add_user(name, password, admin=True)
    return name
