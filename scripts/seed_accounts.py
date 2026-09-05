#!/usr/bin/env python3
"""Put the same five people on every node that serves the dashboard.

There is no directory service here and there is not going to be one — this is a
dashboard read by a handful of named people. What there has to be is one place
that says who those people are, so that a rebuilt node comes back with all of
them instead of just the operator whose credentials happen to sit in
runtime.env. That list is `ROSTER` below; everything else is transport.

Two transports, because the two nodes are reachable in different ways:

  --local            write straight into the account store (a laptop, or a
                     container that something can exec into)
  --http <base-url>  log in as an admin and use the account page, which is the
                     only way into a machine that has no shell — which is both
                     cloud nodes

Both are additive and idempotent. An account that already exists is left exactly
as it is, password included: re-running this must never be the reason somebody
who was working this morning cannot log in this afternoon. Use `--reset NAME` to
deliberately mint a new password for one person.

Passwords are generated here and printed once. They are not stored in this file,
not committed, and cannot be read back out of the server afterwards — the store
keeps scrypt hashes. Losing one means an admin resets it on /account.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Who has an account, and why. The note shows up next to the name on /account
#: so that six months from now "carl" is still identifiable.
ROSTER: list[dict[str, str]] = [
    {"name": "yuesong",  "role": "admin",  "note": "Yuesong Cai · 运行台负责人"},
    {"name": "jon",      "role": "member", "note": "Jon · 方法论评审"},
    {"name": "carl",     "role": "member", "note": "Carl"},
    {"name": "bytedance", "role": "member", "note": "字节 · 合作方"},
    {"name": "yaojiaqi", "role": "member", "note": "Yao Jiaqi 姚佳琪"},
]

#: No 0/O/1/l/I. These get read off a screen and typed into a phone, and a
#: password that is strong but ambiguous is one that "does not work" the first
#: three times.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_password(groups: int = 3, size: int = 5) -> str:
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(size))
                    for _ in range(groups))


class Vault:
    """The passwords this run used, so the next node gets the same ones.

    Two nodes serve this dashboard and each keeps its own account store. Seeding
    them in two runs would give every person two different passwords for two
    URLs that look identical — which is not a smaller version of "the login does
    not work", it is a bigger one.

    So the first run writes what it generated to a local file (0600, gitignored,
    never committed) and later runs read it. Absent file, absent entry, or
    `--reset` all mean "generate a new one and record it".
    """

    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.data: dict[str, str] = {}
        if self.path and self.path.exists():
            import json
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            print(f"沿用 {self.path} 里已有的口令（{len(self.data)} 个），"
                  f"这样两台节点上是同一套凭证")

    def password_for(self, name: str, *, fresh: bool) -> str:
        if not fresh and name in self.data:
            return self.data[name]
        pw = make_password()
        self.data[name] = pw
        return pw

    def flush(self) -> None:
        if not self.path or not self.data:
            return
        import json
        import os as _os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False),
                             encoding="utf-8")
        _os.chmod(self.path, 0o600)
        print(f"\n口令已记到 {self.path}（0600，已在 .gitignore 里）——"
              f"给下一台节点用，交付完就可以删掉。")


# --------------------------------------------------------------------------
# transport: straight into the store
# --------------------------------------------------------------------------
def seed_local(reset: set[str], vault: Vault) -> list[tuple[str, str, str]]:
    from ideagen import accounts
    st = accounts.store_status()
    print(f"账号存放于 {st['path']}（{st['why']}）"
          + ("" if st["durable"] else "  ⚠ 不持久，换容器就没了"))
    existing = {u["name"] for u in accounts.list_users()}
    out: list[tuple[str, str, str]] = []
    for person in ROSTER:
        name = accounts.normalize_name(person["name"])
        if name in existing and name not in reset:
            # Deliberately silent about the password: this script is run
            # repeatedly, and a run that rotates working credentials is worse
            # than a run that does nothing.
            print(f"  = {name:<10} 已存在，未改动（{accounts.role(name)}）")
            continue
        pw = vault.password_for(name, fresh=name in reset)
        if name in existing:
            accounts.admin_set_password(name, pw)
            accounts.set_role(name, person["role"])
            print(f"  ~ {name:<10} 口令已重置")
        else:
            accounts.add_user(name, pw, role=person["role"],
                              note=person.get("note", ""))
            print(f"  + {name:<10} 已创建（{person['role']}）")
        out.append((name, pw, person["role"]))
    return out


# --------------------------------------------------------------------------
# transport: through the login form, for a node with no shell
# --------------------------------------------------------------------------
class Session:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        import http.cookiejar
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(self, path: str, form: dict) -> tuple[int, str]:
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     # The server checks same-origin on every POST and falls
                     # back to Sec-Fetch-Site, which no script sends. Sending a
                     # real Origin is what makes this work from outside a
                     # browser at all.
                     "Origin": self.base, "Referer": self.base + path,
                     "Accept": "text/html"})
        try:
            r = self.opener.open(req, timeout=60)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, path: str) -> tuple[int, str]:
        req = urllib.request.Request(self.base + path,
                                     headers={"Accept": "text/html"})
        try:
            r = self.opener.open(req, timeout=60)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


def seed_http(base: str, admin: str, password: str,
              reset: set[str], vault: Vault) -> list[tuple[str, str, str]]:
    s = Session(base)
    code, body = s.post("/login", {"username": admin, "password": password,
                                   "next": "/account"})
    if code != 200:
        raise SystemExit(f"以 {admin} 登录 {base} 失败：HTTP {code}\n"
                         f"{body[:400]}")
    code, page = s.get("/account")
    if code != 200 or "新增账号" not in page:
        raise SystemExit(f"{admin} 登录成功但不是管理员，加不了人（HTTP {code}）")

    out: list[tuple[str, str, str]] = []
    for person in ROSTER:
        name = person["name"].strip().lower()
        # The account table renders each name in <b>…</b>; that is the honest
        # signal available over HTTP, and it is checked rather than assumed
        # because "already exists" and "the add silently failed" would
        # otherwise look identical.
        present = f"<b>{name}</b>" in page
        if present and name not in reset:
            print(f"  = {name:<10} 已存在，未改动")
            continue
        pw = vault.password_for(name, fresh=name in reset)
        if present:
            code, page = s.post("/account/reset",
                                {"username": name, "password": pw})
            verb = "口令已重置"
        else:
            code, page = s.post("/account/add",
                                {"username": name, "password": pw,
                                 "role": person["role"],
                                 "note": person.get("note", "")})
            verb = f"已创建（{person['role']}）"
        if code != 200 or f"<b>{name}</b>" not in page:
            print(f"  ! {name:<10} 没成功：HTTP {code}")
            continue
        print(f"  {'~' if present else '+'} {name:<10} {verb}")
        out.append((name, pw, person["role"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--local", action="store_true",
                   help="写本机的账号存储")
    g.add_argument("--http", metavar="URL",
                   help="通过登录页操作远端节点，例如 http://101.47.28.218")
    ap.add_argument("--admin", help="--http 时用哪个管理员登录")
    ap.add_argument("--password", help="--http 时该管理员的口令")
    ap.add_argument("--reset", action="append", default=[],
                    help="对这个人重新生成口令（可重复）")
    ap.add_argument("--passwords", metavar="FILE",
                    help="本地口令记事本：有就沿用，没有就生成后写进去。"
                         "两台节点用同一份，凭证才是一套。")
    a = ap.parse_args()
    reset = {n.strip().lower() for n in a.reset}
    vault = Vault(a.passwords)

    if a.local:
        made = seed_local(reset, vault)
    else:
        if not a.admin or not a.password:
            raise SystemExit("--http 需要 --admin 和 --password")
        made = seed_http(a.http, a.admin, a.password, reset, vault)
    vault.flush()

    if made:
        print("\n新口令（只显示这一次，服务端只存散列，之后谁也读不回来）：")
        width = max(len(n) for n, _, _ in made)
        for name, pw, role in made:
            print(f"  {name:<{width}}  {pw}   [{role}]")
        print("\n交给本人之后请他们自己在 /account 改一次。")
    else:
        print("\n没有新账号需要创建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
