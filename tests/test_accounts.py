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



class WhereTheAccountsLive(unittest.TestCase):
    """The bug that made the login "often not work", pinned.

    On the display node the container is recreated by the code leg every time
    origin/main moves — several times a day, because several agents push there.
    `_store_path()` fell through to `config.DATA`, which inside the image is
    `/app/data`: part of the container. So every deploy deleted every account
    except the one `bootstrap()` re-mints from runtime.env, and the people added
    since simply stopped existing. Nothing logged it. The login page kept
    working perfectly for the one account that kept being recreated.

    The container has exactly one durable writable directory — the host mount
    the database sits on — and the fix is to prefer it. Which means the fix
    travels on the code leg and lands on a machine nobody can log in to.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._prev = os.environ.get("IDEAGEN_ACCOUNTS_FILE")
        os.environ.pop("IDEAGEN_ACCOUNTS_FILE", None)
        from ideagen import accounts
        self.a = accounts

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("IDEAGEN_ACCOUNTS_FILE", None)
        else:
            os.environ["IDEAGEN_ACCOUNTS_FILE"] = self._prev
        self._tmp.cleanup()

    def _as_container(self, db_dir: Path):
        """Pretend to be the image: WORKDIR /app, database on a host mount."""
        from unittest import mock
        from ideagen import config
        return mock.patch.multiple(
            config, ROOT=Path("/app"), DATA=Path("/app/data"),
            DB_PATH=db_dir / "ideagen.db")

    def test_the_database_mount_wins_over_the_disposable_image_directory(self):
        mount = Path(self._tmp.name) / "data"
        mount.mkdir()
        with self._as_container(mount):
            st = self.a.store_status()
        self.assertEqual(st["path"], str(mount / "accounts.json"),
                         "accounts landed inside the container again; the next "
                         "deploy would delete every colleague added since")
        self.assertTrue(st["durable"])

    def test_an_ephemeral_store_says_so_instead_of_looking_fine(self):
        """With no mount to fall back to, the answer must be loud, not silent."""
        from unittest import mock
        from ideagen import config
        with mock.patch.multiple(config, ROOT=Path("/app"),
                                 DATA=Path(self._tmp.name) / "in-image",
                                 DB_PATH=Path("/app/data/ideagen.db")):
            st = self.a.store_status()
        self.assertFalse(st["durable"])
        self.assertIn("换容器", st["why"])

    def test_a_directory_it_cannot_look_inside_is_a_no_not_a_crash(self):
        """The production node found this one within minutes of deploying.

        `/run/ideagen-oauth` is 0700 and the container is not its owner, so
        stat-ing a file inside it raises EACCES — which `Path.exists()` does NOT
        swallow, unlike ENOENT and its neighbours. The probe raised out of
        `store_status()`, `/healthz` answered 500, and the container healthcheck
        reads `/healthz`. A yes/no question must come back yes or no.
        """
        locked = Path(self._tmp.name) / "locked"
        locked.mkdir()
        (locked / "accounts.json").write_text("{}", encoding="utf-8")
        locked.chmod(0o000)
        try:
            if os.access(locked / "accounts.json", os.F_OK):
                self.skipTest("以 root 运行时权限位不起作用")
            os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(locked / "accounts.json")
            st = self.a.store_status()      # must not raise
        finally:
            # Restored here, not in addCleanup: cleanups run *after* tearDown,
            # and tearDown removes the temporary directory — so the restore
            # would arrive at a path that no longer exists and fail the test
            # for a reason with nothing to do with what it checks.
            locked.chmod(0o700)
        self.assertNotEqual(st["path"], str(locked / "accounts.json"),
                            "entrusted the account list to a directory it "
                            "cannot even look inside")

    def test_a_mirrored_store_counts_as_surviving_even_inside_the_container(self):
        """Two different mechanisms, one honest answer.

        The production node has no writable host mount, so its accounts do live
        in the container — but every write is mirrored to object storage and the
        next container reads it back before it will mint anything. Reporting
        that as "will be lost" would send an operator chasing a problem that is
        already solved; reporting it as plain "durable" would hide which
        mechanism is holding it up. Both are said.
        """
        from unittest import mock
        from ideagen import config
        prev = os.environ.get("IDEAGEN_ACCOUNTS_MIRROR")
        os.environ["IDEAGEN_ACCOUNTS_MIRROR"] = "1"
        try:
            with mock.patch.multiple(config, ROOT=Path("/app"),
                                     DATA=Path(self._tmp.name) / "in-image",
                                     DB_PATH=Path("/app/data/ideagen.db")):
                st = self.a.store_status()
        finally:
            if prev is None:
                os.environ.pop("IDEAGEN_ACCOUNTS_MIRROR", None)
            else:
                os.environ["IDEAGEN_ACCOUNTS_MIRROR"] = prev
        self.assertTrue(st["durable"])
        self.assertFalse(st["local_durable"])
        self.assertTrue(st["mirror"])

    def test_an_explicit_setting_beats_every_guess(self):
        want = Path(self._tmp.name) / "explicit" / "accounts.json"
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(want)
        st = self.a.store_status()
        self.assertEqual(st["path"], str(want))
        self.assertTrue(st["durable"])


class TheRosterIsNotPublished(unittest.TestCase):
    """This repository is public and the account store sits in `data/`.

    The file was untracked but not ignored, which is the worst of the three
    states available: invisible in `git status -s` terms until the day somebody
    runs `git add -A`, and then permanent. It holds every username with access
    and a scrypt hash per password — the roster is itself the disclosure, before
    anyone bothers to attack the hashes.

    Checked by reading .gitignore rather than shelling out to git, because this
    suite also runs inside the image, where there is no repository at all —
    and the image is also why the file has to be looked for rather than opened.
    The Dockerfile copies `ideagen scripts tests prompts seed web` and nothing
    from the root, so `.gitignore` is not there either, and reading it blind
    raised FileNotFoundError. The cloud updater runs this suite before it will
    swap containers, so on 2026-09-05 that error stopped deployment at 2297d64
    while the same commit passed every local gate: they test a worktree, which
    has the file. Dropping the dependency on the `git` binary was the right
    instinct applied one step short of the dependency that mattered.

    Skipping is right rather than shipping .gitignore into the image: the claim
    is about what this repository would publish, and inside a container there
    is no repository for it to be true or false about.
    """

    def test_the_account_store_and_the_password_notebook_are_ignored(self):
        from ideagen import config
        ignore = Path(config.ROOT) / ".gitignore"
        if not ignore.is_file():
            self.skipTest("没有 .gitignore —— 这里不是仓库（镜像里就是如此），"
                          "这条断言无从谈起")
        patterns = {
            line.strip()
            for line in ignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}
        for needed in ("data/accounts.json", "data/seed_passwords.json"):
            self.assertIn(needed, patterns,
                          f"{needed} would be publishable by `git add -A`")

if __name__ == "__main__":
    unittest.main()
