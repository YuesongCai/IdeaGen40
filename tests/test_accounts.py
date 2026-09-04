"""Tests for named accounts and signed sessions (ideagen/accounts.py).

The properties worth pinning are the ones whose absence is invisible: a password
change that leaves old sessions alive, a cookie that verifies under a different
key, a login form that answers "does this user exist".
"""
from __future__ import annotations

import base64
import importlib
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class AccountsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._prev = {k: os.environ.get(k)
                      for k in ("IDEAGEN_ACCOUNTS_FILE", "IDEAGEN_DASH_KEY")}
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(
            Path(self._tmp.name) / "accounts.json")
        os.environ["IDEAGEN_DASH_KEY"] = "key-for-tests"
        from ideagen import accounts
        self.a = importlib.reload(accounts)
        self.a.add_user("alice", "alicepassword", admin=True)

    def tearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_verify_accepts_only_the_real_password(self):
        self.assertTrue(self.a.verify("alice", "alicepassword"))
        self.assertFalse(self.a.verify("alice", "alicepassword "))
        self.assertFalse(self.a.verify("alice", ""))

    def test_unknown_user_is_indistinguishable_from_a_wrong_password(self):
        """Both must be False, and both must actually hash.

        If a missing user returned early, the response time would tell an
        attacker which names have accounts.
        """
        self.assertFalse(self.a.verify("nobody", "alicepassword"))
        t0 = time.perf_counter()
        self.a.verify("nobody", "x")
        missing = time.perf_counter() - t0
        t0 = time.perf_counter()
        self.a.verify("alice", "wrong")
        wrong = time.perf_counter() - t0
        # Same order of magnitude — the decoy hash is real work, not a sleep.
        self.assertLess(max(missing, wrong) / max(min(missing, wrong), 1e-9), 8)

    def test_session_round_trips_and_names_the_user(self):
        self.assertEqual(self.a.check(self.a.issue("alice")), "alice")

    def test_session_is_rejected_under_a_different_key(self):
        token = self.a.issue("alice")
        os.environ["IDEAGEN_DASH_KEY"] = "some-other-key"
        self.assertIsNone(self.a.check(token))

    def test_tampering_with_the_payload_invalidates_the_signature(self):
        token = self.a.issue("alice")
        raw, _, sig = token.rpartition(".")
        pad = "=" * (-len(raw) % 4)
        name, epoch, exp = base64.urlsafe_b64decode(raw + pad).decode().split("|")
        forged = base64.urlsafe_b64encode(
            f"alice|{epoch}|{int(exp) + 86400}".encode()).decode().rstrip("=")
        self.assertIsNone(self.a.check(f"{forged}.{sig}"))

    def test_expired_session_is_not_accepted(self):
        self.assertIsNone(self.a.check(self.a.issue("alice", days=-1)))

    def test_password_change_ends_existing_sessions(self):
        token = self.a.issue("alice")
        self.assertEqual(self.a.check(token), "alice")
        self.a.set_password("alice", "a-new-password")
        self.assertIsNone(self.a.check(token))
        self.assertTrue(self.a.verify("alice", "a-new-password"))

    def test_revoke_ends_sessions_without_changing_the_password(self):
        token = self.a.issue("alice")
        self.a.revoke_sessions("alice")
        self.assertIsNone(self.a.check(token))
        self.assertTrue(self.a.verify("alice", "alicepassword"))

    def test_deleting_a_removed_user_invalidates_their_session(self):
        self.a.add_user("bob", "bobspassword")
        token = self.a.issue("bob")
        self.a.remove_user("bob")
        self.assertIsNone(self.a.check(token))

    def test_the_last_account_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.a.remove_user("alice")

    def test_short_passwords_are_refused(self):
        with self.assertRaises(ValueError):
            self.a.add_user("carol", "short")

    def test_garbage_cookies_are_answered_with_none_not_an_exception(self):
        for junk in (None, "", "no-dot", "....", "a.b", "!!!.???", "x" * 5000):
            self.assertIsNone(self.a.check(junk))

    def test_bootstrap_creates_the_first_admin_then_stops(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(Path(tmp.name) / "a.json")
        os.environ["IDEAGEN_DASH_USER"] = "operator"
        os.environ["IDEAGEN_DASH_PASSWORD"] = "operator-password"
        try:
            self.assertEqual(self.a.bootstrap(), "operator")
            self.assertTrue(self.a.is_admin("operator"))
            # Idempotent: a second start must not resurrect a deleted account or
            # reset a password that has since been changed in the UI.
            self.a.set_password("operator", "changed-in-the-ui")
            self.assertIsNone(self.a.bootstrap())
            self.assertTrue(self.a.verify("operator", "changed-in-the-ui"))
        finally:
            os.environ.pop("IDEAGEN_DASH_USER", None)
            os.environ.pop("IDEAGEN_DASH_PASSWORD", None)

    def test_throttle_backs_off_after_repeated_failures_and_clears_on_success(self):
        who = "198.51.100.7"
        self.assertEqual(self.a.throttle_wait(who), 0)
        for _ in range(5):
            self.a.throttle_fail(who)
        self.assertGreater(self.a.throttle_wait(who), 0)
        self.a.throttle_ok(who)
        self.assertEqual(self.a.throttle_wait(who), 0)

    def test_a_corrupt_store_does_not_lock_everyone_out_with_an_exception(self):
        Path(os.environ["IDEAGEN_ACCOUNTS_FILE"]).write_text("{not json",
                                                             encoding="utf-8")
        self.assertEqual(self.a.list_users(), [])
        self.assertFalse(self.a.verify("alice", "alicepassword"))


if __name__ == "__main__":
    unittest.main()
