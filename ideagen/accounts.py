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

#: Where the accounts live — and whether that place survives a redeploy.
#:
#: Getting this wrong is silent and total. The file lands inside the container,
#: every login still works, and the next code deploy — which on these nodes can
#: be any hour of any day, because several agents push to main and the code leg
#: rebuilds the container whenever it moves — deletes every account except the
#: one `bootstrap()` re-mints from runtime.env. A colleague added on Monday is
#: gone on Tuesday, their password change reverts, and nobody is told any of it.
#: That was the actual reason the login "often did not work".
#:
#: So the choice is no longer a guess with a silent fallback: the candidates are
#: ordered, each one says whether it outlives the container, and the one that
#: got picked is reported by `store_status()` and printed at startup.
def _in_image() -> bool:
    """True when this process is the container, whose filesystem is disposable."""
    from . import config
    return str(getattr(config, "ROOT", "")) == "/app"


def _candidates() -> list[tuple[Path, bool, str]]:
    """(path, survives a redeploy, why) in preference order."""
    from . import config
    out: list[tuple[Path, bool, str]] = []
    explicit = os.environ.get("IDEAGEN_ACCOUNTS_FILE")
    if explicit:
        out.append((Path(explicit), True, "IDEAGEN_ACCOUNTS_FILE 指定"))
    # The compose deployment mounts a host directory here for the OAuth tokens.
    # It is created 0700 by root in the boot script and the container runs
    # unprivileged, so it is frequently NOT writable — checking beats assuming,
    # because an unwritable path turns every login into a 500 that looks like
    # the app being broken rather than a directory mode.
    out.append((Path("/run/ideagen-oauth/accounts.json"), True,
                "宿主机挂载 /run/ideagen-oauth"))
    # The database's own directory. On the display node that is the host mount
    # `/data` — the one durable writable place that container already has. Using
    # it means this fix reaches a running node through the code leg, with no
    # reinstall of a machine nobody can log into. Skipped when the database is
    # the in-image default, which is as disposable as everything else in there.
    try:
        db_dir = Path(getattr(config, "DB_PATH")).parent
        if db_dir.is_absolute() and not str(db_dir).startswith(str(config.ROOT)):
            out.append((db_dir / "accounts.json", True, f"数据库目录 {db_dir}"))
    except Exception:  # noqa: BLE001 — a missing DB_PATH must not break login
        pass
    # Last resort. On a laptop this is an ordinary directory and perfectly
    # durable; inside the image it is the trap described above.
    out.append((Path(getattr(config, "DATA", "data")) / "accounts.json",
                not _in_image(),
                "容器内路径，换容器就没了" if _in_image() else "本机 data 目录"))
    return out


def _writable(path: Path) -> bool:
    """Can this process actually create and rewrite `path`?"""
    parent = path.parent
    if path.exists():
        return os.access(path, os.W_OK) and os.access(parent, os.W_OK)
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return False
    return parent.is_dir() and os.access(parent, os.W_OK)


def store_status() -> dict[str, Any]:
    """Which candidate is in use, and whether accounts survive a redeploy.

    Exposed on /healthz and printed at startup on purpose. An ephemeral store is
    not an error at the moment it is chosen — it only becomes one later, when a
    deploy quietly empties it — so it has to be visible before then.
    """
    for path, durable, why in _candidates():
        if _writable(path):
            return {"path": str(path), "durable": durable, "why": why,
                    "mirror": _mirror_on()}
    last = _candidates()[-1]
    return {"path": str(last[0]), "durable": False,
            "why": "所有候选路径都不可写", "mirror": _mirror_on()}


def _store_path() -> Path:
    return Path(store_status()["path"])


#: Where the accounts are mirrored so they survive the container being
#: replaced. The local file is the working copy — object storage is not a
#: filesystem and must not be in the path of a login — but a redeploy would
#: otherwise silently drop every account added since the last image build.
MIRROR_KEY = "accounts/accounts.json"


def _mirror_on() -> bool:
    """Opt in, never inferred.

    Inferring it from the configured platform was wrong in the way that matters:
    a laptop is configured for the cloud too, so the first test run reached out
    and touched the production bucket. A deployment that wants mirroring says so
    in its compose file; everything else, including every test, stays offline.
    """
    return (os.environ.get("IDEAGEN_ACCOUNTS_MIRROR") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _mirror_push(data: dict[str, Any]) -> None:
    if not _mirror_on():
        return
    try:
        from . import platform as plat
        p = plat.load()
        blobs = p.blobs
        blobs._c().put_object(blobs.bucket, blobs._k(MIRROR_KEY),
                              content=json.dumps(data, ensure_ascii=False).encode())
    except Exception as e:  # noqa: BLE001 — a mirror that fails must not fail a save
        # Silence here is how a colleague added today is missing tomorrow: the
        # save succeeded locally, the mirror never got it, and the next
        # container starts from a mirror that predates them. The save still
        # stands — but somebody has to be told.
        import sys
        print(f"WARN: 账号镜像未写入（{type(e).__name__}: {e}）——"
              f"本地已保存，但容器重建后会丢", file=sys.stderr)


class MirrorUnreadable(RuntimeError):
    """The mirror may exist; this process could not find out."""


def _mirror_pull() -> dict[str, Any] | None:
    """The mirrored account list, or None if there genuinely is not one.

    Raises `MirrorUnreadable` when the store could not be asked. That
    distinction is the whole point of the mirror: `bootstrap` treats None as
    "first boot, mint the operator account", and a store outage answering None
    means a container replacement silently discards every colleague added since
    — which is the exact outcome the mirror was written to prevent, arriving
    under the exact conditions (a fresh container) that make the store most
    likely to be flaky.
    """
    if not _mirror_on():
        return None
    from . import platform as plat
    try:
        raw = plat.load().blobs.get(MIRROR_KEY)
    except plat.BlobMissing:
        return None                      # never mirrored: a real first boot
    except Exception as e:  # noqa: BLE001
        raise MirrorUnreadable(f"{type(e).__name__}: {e}") from e
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — a corrupt mirror is not an absent one
        raise MirrorUnreadable(f"镜像解不开：{type(e).__name__}: {e}") from e
    return data if isinstance(data, dict) and data.get("users") else None


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
    _mirror_push(data)
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


#: Two roles, and deliberately only two.
#:
#: The reference system this was modelled on carries a role *code list* per
#: request, per tenant, checked by a separate gateway. That is the right shape
#: for a product with many customers and many desks; it is the wrong shape for
#: one dashboard read by five named people, where every extra role is a rule
#: somebody has to remember and nobody will test. So: `admin` can manage
#: accounts, `member` can read the dashboard. Everything else — which pages,
#: which portfolios — is the same for both, because it genuinely is.
ROLES = ("admin", "member")
ROLE_LABEL = {"admin": "管理员", "member": "成员"}


def role(name: str) -> str:
    """This account's role. Stored as the `admin` flag for backward compat —
    accounts written before roles existed keep working without a migration."""
    u = load()["users"].get(normalize_name(name))
    if not u:
        return "member"
    return "admin" if u.get("admin") else "member"


def list_users() -> list[dict[str, Any]]:
    users = load()["users"]
    return [{"name": n, "admin": bool(u.get("admin")),
             "role": "admin" if u.get("admin") else "member",
             "note": u.get("note") or "",
             "created": u.get("created"), "last_login": u.get("last_login")}
            for n, u in sorted(users.items())]


def _admins(data: dict[str, Any]) -> list[str]:
    return [n for n, u in data["users"].items() if u.get("admin")]


def set_role(name: str, new_role: str) -> None:
    """Promote or demote. Refuses to remove the last admin.

    Without that guard the account page offers a single click that locks
    everyone out of account management for good — recoverable only by editing a
    JSON file on a machine that has no shell.
    """
    if new_role not in ROLES:
        raise ValueError(f"没有 {new_role} 这个角色")
    data = load()
    u = data["users"].get(normalize_name(name))
    if not u:
        raise ValueError(f"没有用户 {name}")
    if new_role != "admin" and u.get("admin") and len(_admins(data)) == 1:
        raise ValueError("这是最后一个管理员，降级之后就没人能管账号了")
    u["admin"] = (new_role == "admin")
    save(data)


#: Usernames are typed into a login form by five people, so they are kept to
#: what survives a keyboard, a phone, and a copy-paste: ASCII letters, digits,
#: dot, dash, underscore. A name that renders differently than it was typed is
#: an account that "sometimes" does not work.
_NAME_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def normalize_name(name: str) -> str:
    """The canonical form of a typed username: trimmed and lowercased.

    Case-insensitive on purpose. `Jon` and `jon` being two different accounts is
    a support ticket waiting to happen, and the person who hits it experiences
    it as "the password does not work".
    """
    return (name or "").strip().lower()


def add_user(name: str, password: str, *, admin: bool = False,
             role: str | None = None, note: str = "") -> None:
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"没有 {role} 这个角色")
        admin = (role == "admin")
    name = normalize_name(name)
    if not name or len(name) > 64:
        raise ValueError("用户名不能为空，且不超过 64 字符")
    bad = sorted(set(name) - _NAME_OK)
    if bad:
        raise ValueError(
            f"用户名只能用英文字母、数字和 . _ -（这些不行：{''.join(bad)}）")
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
        "note": note,
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
    u = data["users"].get(normalize_name(name))
    if not u:
        raise ValueError(f"没有用户 {name}")
    salt = secrets.token_bytes(16)
    u["salt"] = base64.b64encode(salt).decode()
    u["hash"] = _hash(password, salt)
    u["epoch"] = int(u.get("epoch", 1)) + 1
    save(data)


def admin_set_password(name: str, password: str) -> None:
    """Reset somebody else's password.

    The one thing missing that made "I cannot get in" unfixable: the account
    page could add a colleague and delete a colleague, but the day one of them
    forgot their password there was nothing an admin could do about it, on a
    machine with no shell. Same epoch bump as a self-service change, so the
    reset also ends whatever sessions the old password left behind.
    """
    set_password(name, password)


def remove_user(name: str) -> None:
    name = normalize_name(name)
    data = load()
    if name not in data["users"]:
        raise ValueError(f"没有用户 {name}")
    if len(data["users"]) == 1:
        raise ValueError("这是最后一个账号，删掉就没人能进来了")
    del data["users"][name]
    save(data)


def revoke_sessions(name: str) -> None:
    data = load()
    u = data["users"].get(normalize_name(name))
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
    u = load()["users"].get(normalize_name(name))
    if not u:
        # Hash anyway, so a missing user and a wrong password take the same
        # time. Otherwise the login form answers "does this account exist".
        _hash(password, b"decoy-salt-000000")
        return False
    expect = _hash(password, base64.b64decode(u["salt"]))
    return hmac.compare_digest(expect, u["hash"])


def note_login(name: str) -> None:
    data = load()
    u = data["users"].get(normalize_name(name))
    if u:
        u["last_login"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save(data)


def is_admin(name: str) -> bool:
    u = load()["users"].get(normalize_name(name))
    return bool(u and u.get("admin"))


def issue(name: str, *, days: int = SESSION_DAYS) -> str:
    name = normalize_name(name)
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
    # Nothing local. Before minting a fresh admin, look for accounts this
    # deployment already had: a container replacement must not quietly discard
    # the colleague you added last week and hand you back the bootstrap user.
    try:
        mirrored = _mirror_pull()
    except MirrorUnreadable as e:
        # Refuse rather than mint. A fresh admin here would look like a working
        # instance while the accounts it was supposed to restore are still in
        # the store, unread — and the next mirror push would overwrite them
        # with the single bootstrap user.
        raise RuntimeError(
            f"账号镜像读不到，拒绝新建管理员（可能会覆盖已有账号）：{e}") from e
    if mirrored:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(mirrored, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        os.chmod(p, 0o600)
        return None
    name = (os.environ.get("IDEAGEN_DASH_USER") or "").strip()
    password = os.environ.get("IDEAGEN_DASH_PASSWORD") or ""
    if not name or len(password) < 8:
        return None
    add_user(name, password, admin=True)
    return name
