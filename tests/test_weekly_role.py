"""Who produces the weekly here — and why an inference must not outrank a statement.

Two nodes run this code: a laptop and a cloud instance. Exactly one of them may
produce a given week. That constraint is not enforced by the database, and the
reason is worth stating because it is the whole hazard: the two write to
different stores, so the uniqueness index guarding one period cannot see the
other. Two runners do not error. They produce the same week twice, and whichever
node someone opens is the version they believe.

On 2026-09-05, four days before the 2026-09-09 trigger, both nodes resolved to
`runner`. The laptop declared `observer` in `~/.ideagen.env` and was promoted
past it: `scripts/tick.py` reads the model key with a regex that matches the
line even when commented out, and then `setdefault` wrote `runner` into the
child environment — the declaration lives in the file, not in `os.environ`, so
it was never there to block the default. The cloud instance declared nothing and
took the default. Each step defensible, the sum a machine acting against its own
configuration.

None of this was covered by a test, which is how it survived. These pin the
order: an explicit statement beats an inference, and only the absence of a
capability beats an explicit statement.
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ideagen import scheduler


def _tick_module():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("tick_under_test",
                                                  root / "scripts" / "tick.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class RoleResolution(unittest.TestCase):
    """`weekly_role()` reports what will actually happen, so it has to match."""

    def _role(self, *, declared: str | None, promoted: bool, env: str | None):
        environ = {k: v for k, v in os.environ.items()
                   if k != "IDEAGEN_WEEKLY_ROLE"}
        if env:
            environ["IDEAGEN_WEEKLY_ROLE"] = env
        with mock.patch.object(scheduler, "_declared",
                               lambda k: declared if k == "IDEAGEN_WEEKLY_ROLE" else None), \
             mock.patch.object(scheduler, "_tick_sees_model_key", lambda: promoted), \
             mock.patch.dict(os.environ, environ, clear=True):
            return scheduler.weekly_role()

    def test_a_declared_observer_survives_a_readable_model_key(self):
        """The 2026-09-05 case, and the reason this file exists."""
        r = self._role(declared="observer", promoted=True, env=None)
        self.assertEqual(r["effective"], "observer")
        self.assertFalse(r["conflict"])
        self.assertTrue(r["promoted_by_model_key"],
                        "the key is still reported — it just no longer decides")

    def test_a_wrapper_setting_the_variable_still_wins(self):
        """A per-run override is a statement too, and a later one."""
        r = self._role(declared="observer", promoted=False, env="runner")
        self.assertEqual(r["effective"], "runner")
        self.assertTrue(r["conflict"], "disagreeing with the file is worth saying")

    def test_no_declaration_takes_the_default(self):
        r = self._role(declared=None, promoted=False, env=None)
        self.assertEqual(r["effective"], "runner")
        self.assertFalse(r["conflict"])

    def test_the_reason_never_claims_the_key_changed_the_role(self):
        r = self._role(declared="observer", promoted=True, env=None)
        self.assertIn("不再改变角色", r["why"])


class WhatTickHandsToTheChild(unittest.TestCase):
    """The other half: `weekly_role` is only right if `tick.py` agrees."""

    def _env_file(self, body: str) -> Path:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "ideagen.env"
        p.write_text(body, encoding="utf-8")
        return p

    def test_a_live_declaration_is_read(self):
        tick = _tick_module()
        with mock.patch.object(tick, "ENV_FILE",
                               self._env_file("IDEAGEN_WEEKLY_ROLE=observer\n")):
            self.assertEqual(tick._declared_role(), "observer")

    def test_a_commented_out_declaration_is_not_a_declaration(self):
        """Unlike the key, where commenting out is how inference is switched off.

        Commenting out a role means the operator withdrew it, so the node falls
        back to the default rather than to a line nobody meant to be live.
        """
        tick = _tick_module()
        with mock.patch.object(tick, "ENV_FILE",
                               self._env_file("# IDEAGEN_WEEKLY_ROLE=observer\n")):
            self.assertEqual(tick._declared_role(), "")

    def test_the_key_is_still_read_through_a_comment(self):
        """The asymmetry is deliberate; assert it so it is not 'tidied' away."""
        tick = _tick_module()
        with mock.patch.object(tick, "ENV_FILE",
                               self._env_file("# ARK_API_KEY=abc123\n")):
            self.assertEqual(tick._ark_key(), "abc123")


if __name__ == "__main__":
    unittest.main()
