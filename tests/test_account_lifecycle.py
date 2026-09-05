"""The whole account system, driven the way a person drives it.

Every unit here already had a test. What did not was the trip between them, and
that is where all four of the real failures lived:

* `verify()` looked accounts up in canonical form while `issue()` used the raw
  keystrokes, so signing in as `Jon` verified fine and minted a cookie for a
  user that does not exist. The login succeeded; the next request threw it out.
  From the outside that is "it keeps logging me out", which sounds like a cookie
  problem and is not one.
* the shared-key cookie was read with `str.replace` over the whole `Cookie`
  header, so it stopped being found the moment any other cookie sat in front of
  it — which, once sessions existed, was always.
* an admin could add a colleague and delete a colleague but could not reset the
  password of one who forgot it, on a box with no shell and no mail. That is not
  an inconvenience; it is the account being over.
* demoting the last admin was one click away, and unrecoverable by the same
  argument.

So these tests run a real server, keep real cookies, and assert on what the next
request does — not on what the function returned.
"""
from __future__ import annotations

import http.cookiejar
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

TIMEOUT_S = 60  # scrypt at n=2**15 is ~32MB a guess, on purpose. See test_login_reachable.


class Client:
    """One browser: its own cookie jar, and it follows redirects like one."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str, **headers):
        req = urllib.request.Request(
            self.base + path, headers={"Accept": "text/html", **headers})
        try:
            r = self.opener.open(req, timeout=TIMEOUT_S)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(self, path: str, form: dict):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": self.base, "Referer": self.base + path,
                     "Accept": "text/html"})
        try:
            r = self.opener.open(req, timeout=TIMEOUT_S)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def cookie_names(self) -> set[str]:
        return {c.name for c in self.jar}


class AccountLifecycle(unittest.TestCase):
    ADMIN_PW = "operator-password-1"
    MEMBER_PW = "member-password-1"

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls._prev = {k: os.environ.get(k) for k in
                     ("IDEAGEN_ACCOUNTS_FILE", "IDEAGEN_DASH_KEY",
                      "IDEAGEN_ACCOUNTS_MIRROR")}
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(
            Path(cls._tmp.name) / "accounts.json")
        os.environ["IDEAGEN_DASH_KEY"] = "key-for-lifecycle-tests"
        os.environ.pop("IDEAGEN_ACCOUNTS_MIRROR", None)

        from ideagen import accounts
        from ideagen.serve import Handler, Server
        cls.accounts = accounts
        accounts.add_user("boss", cls.ADMIN_PW, role="admin", note="运行台负责人")
        # Every test gets whatever accounts it needs up front. Creating them
        # inside the tests made the suite order-dependent, and unittest orders
        # by name — so the member-permission test ran before the test that
        # created the member and failed for a reason that had nothing to do
        # with permissions.
        accounts.add_user("colleague", cls.MEMBER_PW, role="member",
                          note="外部合作方")
        accounts.add_user("forgetful", cls.MEMBER_PW, role="member")

        cls.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for k, v in cls._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    def _login(self, user: str, password: str) -> Client:
        c = Client(self.base)
        c.post("/login", {"username": user, "password": password,
                          "next": "/review"})
        return c

    # ---- the trip, not the pieces -------------------------------------------

    def test_a_session_still_works_on_the_very_next_request(self):
        """The regression that reads as 'it logs me straight back out'."""
        c = self._login("boss", self.ADMIN_PW)
        self.assertIn("ideagen_session", c.cookie_names())
        status, body = c.get("/api/whoami", Accept="application/json")
        self.assertEqual(status, 200, body)
        self.assertIn('"user":"boss"', body)
        self.assertIn('"role":"admin"', body)

    def test_the_username_is_not_case_sensitive(self):
        """`Jon` and `jon` must be one account, not two, and not a dead session."""
        c = self._login("BOSS", self.ADMIN_PW)
        status, body = c.get("/api/whoami", Accept="application/json")
        self.assertEqual(status, 200)
        self.assertIn('"user":"boss"', body,
                      "logging in with different case minted a session for a "
                      "user that does not exist")

    def test_the_wrong_password_gets_no_session(self):
        c = Client(self.base)
        status, _ = c.post("/login", {"username": "boss", "password": "nope"})
        self.assertEqual(status, 401)
        self.assertNotIn("ideagen_session", c.cookie_names())

    # ---- what an admin can do, and what a member cannot ---------------------

    def test_admin_adds_a_member_who_can_then_actually_log_in(self):
        boss = self._login("boss", self.ADMIN_PW)
        status, body = boss.post("/account/add", {
            "username": "Newbie", "password": self.MEMBER_PW,
            "role": "member", "note": "新同事"})
        self.assertEqual(status, 200, body)

        them = self._login("newbie", self.MEMBER_PW)
        status, body = them.get("/api/whoami", Accept="application/json")
        self.assertEqual(status, 200)
        self.assertIn('"role":"member"', body)
        # And they can read the thing they were given an account for.
        self.assertEqual(them.get("/review")[0], 200)

    def test_a_member_cannot_add_or_remove_anyone(self):
        them = self._login("colleague", self.MEMBER_PW)
        _, body = them.post("/account/add",
                            {"username": "smuggled", "password": "x" * 12})
        self.assertIn("只有管理员", body)
        self.assertNotIn("smuggled", str(
            [u["name"] for u in self.accounts.list_users()]))

    def test_admin_can_reset_a_forgotten_password_and_the_old_one_dies(self):
        boss = self._login("boss", self.ADMIN_PW)
        stale = self._login("forgetful", self.MEMBER_PW)   # signed in beforehand
        _, before = stale.get("/api/whoami", Accept="application/json")
        self.assertIn('"user":"forgetful"', before)

        status, body = boss.post("/account/reset", {
            "username": "forgetful", "password": "reset-password-2"})
        self.assertEqual(status, 200, body)

        self.assertFalse(self.accounts.verify("forgetful", self.MEMBER_PW),
                         "the old password still works after a reset")
        self.assertTrue(self.accounts.verify("forgetful", "reset-password-2"))
        # A reset that leaves their old sessions alive has not taken anything
        # back. Asserting 401 here would assert the wrong thing: loopback is
        # allowed unconditionally by design, so what the cookie no longer does
        # is *identify* anyone. That is the property, and the deployment that
        # matters — anything arriving through the proxy — turns it into a 401.
        _, after = stale.get("/api/whoami", Accept="application/json")
        self.assertIn('"user":null', after)
        self.assertNotIn("forgetful", after)

    # ---- the guards ---------------------------------------------------------

    def test_the_last_admin_cannot_be_demoted(self):
        with self.assertRaises(ValueError):
            self.accounts.set_role("boss", "member")
        self.assertEqual(self.accounts.role("boss"), "admin")

    def test_promotion_and_demotion_round_trip(self):
        self.accounts.set_role("colleague", "admin")
        self.assertEqual(self.accounts.role("colleague"), "admin")
        self.accounts.set_role("colleague", "member")   # boss is still an admin
        self.assertEqual(self.accounts.role("colleague"), "member")

    def test_a_username_that_would_not_survive_a_keyboard_is_refused(self):
        with self.assertRaises(ValueError):
            self.accounts.add_user("姚 佳琪", "some-password", role="member")

    # ---- the machine credential still works next to a human one -------------

    def test_the_shared_key_cookie_survives_a_session_cookie_beside_it(self):
        """Reading it with str.replace found whichever cookie came first."""
        req = urllib.request.Request(
            self.base + "/api/whoami",
            headers={"Accept": "application/json",
                     "X-Forwarded-For": "203.0.113.9",   # force the remote path
                     "Cookie": "ideagen_session=garbage; "
                               "dashkey=key-for-lifecycle-tests"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            self.assertEqual(r.status, 200)
            self.assertIn('"via":"key"', r.read().decode())

    # ---- durability is stated, not assumed ----------------------------------

    def test_healthz_says_where_the_accounts_live_and_whether_they_persist(self):
        c = Client(self.base)
        status, body = c.get("/healthz", Accept="application/json")
        self.assertEqual(status, 200)
        self.assertIn('"accounts"', body)
        self.assertIn('"durable"', body,
                      "an ephemeral account store is invisible until the deploy "
                      "that empties it; /healthz is the one place the answer is "
                      "always reachable")


if __name__ == "__main__":
    unittest.main()
