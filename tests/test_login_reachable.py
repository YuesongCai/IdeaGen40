"""The login form has to survive the trip, not just the code review.

`_same_origin` accepts a POST on three signals, in order: `Origin`, then
`Referer`, then `Sec-Fetch-Site`. Serving `Referrer-Policy: no-referrer`
knocked out the first two — per Fetch, a form POST under that policy is sent
with `Origin: null` and no `Referer` — leaving the whole decision on
`Sec-Fetch-Site`, the only one of the three an intermediary is free to drop.

That is not hypothetical. The same browser with the same password got 401 from
`127.0.0.1` and 403 from the cloud node's public IP, and the 403 said
`cross-origin request rejected`, which names the symptom and hides the cause.
Nobody could log in from outside this machine, and the dashboard looked fine.

So there are two claims here, and the second is the one that would have caught
it: the policy must keep `Origin`, and the check must still pass when
`Sec-Fetch-Site` never arrives.
"""
from __future__ import annotations

import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from ideagen.serve import Handler, Server

#: Policies under which the Fetch spec keeps a real `Origin` on a same-origin
#: form POST. `no-referrer` is absent on purpose — that is the bug.
KEEPS_ORIGIN = {"same-origin", "strict-origin-when-cross-origin",
                "origin-when-cross-origin", "no-referrer-when-downgrade"}


#: Generous on purpose. These tests deliberately reach the password check — that
#: is the point of asserting 401 rather than 403 — and the password check is
#: `hashlib.scrypt` at n=2**15, ~32MB per attempt, chosen to be slow. Three
#: seconds is plenty on an idle laptop and not plenty on this one: the repo runs
#: with twenty-odd agent sessions and a full suite in parallel, and the request
#: then times out client-side. The failure surfaces in the cloud deploy gate as
#: a red test on whichever commit happened to be HEAD, which is never the commit
#: that caused it — and costs everyone a deploy cycle while somebody reproduces
#: it and watches it pass. A timeout here is only a guard against a true hang;
#: it does not need to be tight to do that job.
TIMEOUT_S = 30


class LoginSurvivesTheTrip(unittest.TestCase):
    def setUp(self):
        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _post(self, headers):
        req = urllib.request.Request(
            self.base + "/login", method="POST",
            data=b"username=nobody&password=wrong",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     **headers})
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT_S).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_the_policy_does_not_strip_the_origin_it_checks(self):
        page = urllib.request.urlopen(self.base + "/login", timeout=TIMEOUT_S)
        policy = page.headers.get("Referrer-Policy")
        self.assertIn(
            policy, KEEPS_ORIGIN,
            f"Referrer-Policy {policy!r} makes the browser send Origin: null "
            "on the login POST, and _same_origin then has nothing strong left "
            "to check")

    def test_a_real_origin_is_enough_without_sec_fetch_site(self):
        """403 here means an intermediary dropping one header locks everyone out."""
        code = self._post({"Origin": self.base})
        self.assertNotEqual(
            code, 403,
            "the same-origin check refused a POST carrying a correct Origin "
            "just because Sec-Fetch-Site was missing")
        self.assertEqual(code, 401)  # reached the password check, as it should

    def test_a_genuinely_cross_origin_post_is_still_refused(self):
        self.assertEqual(self._post({"Origin": "http://evil.example"}), 403)


if __name__ == "__main__":
    unittest.main()
