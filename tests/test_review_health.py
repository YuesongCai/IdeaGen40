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
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
