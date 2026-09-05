"""The dashboard's port-health verdict: cached, stamped, and never optimistic.

`Platform.check()` opens a live connection to all six ports; on the cloud
platform that is a TOS round trip. The dashboard polls every 60 seconds, so the
verdict is cached and refreshed off the request path. These tests pin the three
things that made that safe to do: an unknown verdict is reported as unknown
rather than healthy, an expired verdict is still served but marked, and the
poll path does not pay for a probe it did not need.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import review


class _Health:
    def __init__(self, name, ok, detail):
        self.name, self.ok, self.detail = name, ok, detail


class _Platform:
    """A platform whose probe is slow and counted, like the real one."""

    name = "fake"

    def __init__(self, delay=0.0):
        self.delay, self.calls = delay, 0

    def check(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return [_Health("blobs", True, "tos://secret-bucket @ ep"),
                _Health("state", False, "sqlite at /Users/someone/db")]


def _scrub(text):
    return (text or "").replace("secret-bucket", "<bucket>") \
                       .replace("/Users/someone", "~")


class TestPortHealth(unittest.TestCase):

    def setUp(self):
        review._health_probe = None
        review._health_running = False

    tearDown = setUp

    def test_unknown_is_pending_not_healthy(self):
        """No verdict yet must not read as "no unhealthy ports"."""
        p = _Platform(delay=5.0)          # slower than the cold-start deadline
        review._HEALTH_COLD_WAIT_S, keep = 0.05, review._HEALTH_COLD_WAIT_S
        try:
            out = review._port_health(p, _scrub)
        finally:
            review._HEALTH_COLD_WAIT_S = keep
        self.assertTrue(out["ports_pending"])
        self.assertEqual(out["ports"], [])
        self.assertIsNone(out["ports_checked_at"])
        # The distinction that matters: pending is not a clean bill of health.
        self.assertFalse(out["ports_stale"])

    def test_verdict_is_served_with_its_timestamp(self):
        p = _Platform()
        review._probe_ports(p)
        out = review._port_health(p, _scrub)
        self.assertFalse(out["ports_pending"])
        self.assertFalse(out["ports_stale"])
        self.assertIsNotNone(out["ports_checked_at"])
        self.assertLess(out["ports_age_s"], 5)
        self.assertEqual([q["name"] for q in out["ports"]], ["blobs", "state"])
        self.assertEqual([q["ok"] for q in out["ports"]], [True, False])

    def test_identifiers_are_scrubbed_on_the_way_out(self):
        p = _Platform()
        review._probe_ports(p)
        details = " ".join(q["detail"]
                           for q in review._port_health(p, _scrub)["ports"])
        self.assertNotIn("secret-bucket", details)
        self.assertNotIn("/Users/", details)

    def test_expired_verdict_is_still_served_but_marked(self):
        """An old answer beats an empty panel — as long as it says it is old."""
        p = _Platform()
        review._probe_ports(p)
        review._health_probe["at"] -= review._HEALTH_TTL_S + 30
        out = review._port_health(p, _scrub)
        self.assertTrue(out["ports_stale"])
        self.assertFalse(out["ports_pending"])
        self.assertEqual(len(out["ports"]), 2)
        self.assertGreater(out["ports_age_s"], review._HEALTH_TTL_S)

    def test_fresh_verdict_costs_no_probe(self):
        """The reason this exists: polling must not re-probe every minute."""
        p = _Platform()
        review._probe_ports(p)
        self.assertEqual(p.calls, 1)
        for _ in range(20):
            review._port_health(p, _scrub)
        self.assertEqual(p.calls, 1)

    def test_a_probe_that_raises_leaves_the_last_verdict_intact(self):
        p = _Platform()
        review._probe_ports(p)

        class _Broken(_Platform):
            def check(self):
                raise RuntimeError("TOS unreachable")

        review._probe_ports(_Broken())
        out = review._port_health(p, _scrub)
        self.assertFalse(out["ports_pending"])
        self.assertEqual(len(out["ports"]), 2)
        self.assertFalse(review._health_running)


class SnapshotFreshness(unittest.TestCase):
    """`generated_at` cannot answer "is this data current".

    It is computed per request, so a display node whose feed stopped days ago
    still stamps the page with the current time. `snapshot` reports the moment
    the node's database was actually installed, which is the only thing on the
    page that can go stale — and the question "is it in sync?" had to be asked
    a person instead of read off the page precisely because nothing carried it.
    """

    def _marker(self, text="6eafe968df07\n"):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / ".state-sha").write_text(text)
        return d

    def test_reports_when_the_database_was_installed(self):
        d = self._marker()
        os.utime(d / ".state-sha", (1_760_000_000, 1_760_000_000))
        with mock.patch.dict(os.environ, {"IDEAGEN_DB": str(d / "ideagen.db")}):
            got = review._snapshot_state()
        self.assertIsNotNone(got)
        self.assertEqual(got["sha"], "6eafe968df07")
        self.assertGreater(got["age_s"], 0)
        self.assertIn("installed_at", got)

    def test_says_nothing_rather_than_inventing_a_time(self):
        """The laptop writes its own database continuously; there is no install
        moment to report, and a fabricated one would read as freshness."""
        d = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"IDEAGEN_DB": str(d / "ideagen.db")}):
            self.assertIsNone(review._snapshot_state())

    def test_an_unreadable_marker_is_not_a_crash(self):
        """`Path.is_file()` raises on EACCES rather than returning False, and
        this runs inside the endpoint that would have reported the problem."""
        d = self._marker()
        (d / ".state-sha").chmod(0o000)
        try:
            with mock.patch.dict(os.environ,
                                 {"IDEAGEN_DB": str(d / "ideagen.db")}):
                self.assertIsNone(review._snapshot_state())
        finally:
            (d / ".state-sha").chmod(0o644)

if __name__ == "__main__":
    unittest.main()
