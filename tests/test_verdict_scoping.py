"""Every verdict has to name the number it covers.

Four times in one night, in this repo, a verdict shipped next to a quantity it
did not describe:

* `verdict` computed for the excess over the theme indicator, rendered beside
  `excess_over_control_pct`, which is a different comparison — a reader ranking
  arms used the second and read the first;
* "not enough data" and "enough data, no edge appeared" collapsed into one
  `underpowered`, which are opposite conclusions;
* a subset compared against the superset containing it, bounded by the subset
  alone;
* nine restated means with a verdict about each against zero, when the thing
  anyone does with nine numbers is rank them, and the ranking had no bound.

Three were found by someone asking a question about something else. The pattern
is not arithmetic — every number was right — it is that what shipped and what it
would be used for did not match. A reader cannot see that mismatch, and neither
could the author, four times.

This mechanises the half that is mechanical: a verdict field must resolve to a
quantity that exists beside it, either by declaring one in `verdict_applies_to`
or by a name sharing its suffix with one.

Checked against the four, so the claim is not larger than the check: it catches
the first — strip the declarations from the attribution layer and nine fields
are flagged. It does not catch the other three. The collapsed states and the
subset-against-superset bound are statistics, not labelling, and the missing
ranking bound is a quantity that was never computed rather than one that was
mislabelled — a verdict cannot be caught pointing at a column that does not
exist yet.

So a green run means the labels are honest, not the method. Saying which one it
covers is the point; a check whose scope is assumed is the same failure it is
here to catch.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config  # noqa: E402

#: Values a verdict field may take. A verdict holding anything else is either a
#: different kind of field or a state nobody documented.
VERDICTS = {"underpowered", "not_ruled_out", "no_edge_detected",
            "stable", "shifted", "conclusive", "inconclusive"}


def unscoped(node: object, path: str = "") -> list[str]:
    """Verdict fields that do not name a quantity sitting beside them."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if (isinstance(value, str) and "verdict" in key
                    and value in VERDICTS):
                declared = node.get("verdict_applies_to")
                # A declaration must start with the name of a sibling. Prose
                # after it is fine — "mean_return_full_horizon_pct 对零" says
                # both which column and against what — but prose alone would let
                # a field declare a target that does not exist, which is the
                # same unverifiable label this check exists to refuse.
                if (isinstance(declared, str) and declared.split()
                        and declared.split()[0] in node):
                    pass
                elif _suffix_match(key, node):
                    pass                                   # verdict_x ↔ *_x_*
                else:
                    out.append(here)
            out += unscoped(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += unscoped(value, f"{path}[{i}]")
    return out


def _suffix_match(key: str, node: dict) -> bool:
    """`verdict_over_control` is scoped by `excess_over_control_pct`."""
    suffix = key.replace("verdict", "").strip("_")
    if not suffix:
        return False
    return any(k is not key and suffix in k and k.endswith("_pct")
               for k in node)


class TheCheckerWorks(unittest.TestCase):
    """Tested on fixtures, so it cannot pass by finding nothing to look at."""

    def test_a_bare_verdict_beside_two_numbers_is_flagged(self):
        # The shape that shipped: one verdict, two candidate quantities, no
        # statement of which one it covers.
        self.assertEqual(
            unscoped({"arms": {"a": {"mean_excess_pct": 1.0,
                                     "excess_over_control_pct": 0.3,
                                     "verdict": "not_ruled_out"}}}),
            ["arms.a.verdict"])

    def test_declaring_the_target_clears_it(self):
        self.assertEqual(
            unscoped({"mean_excess_pct": 1.0, "verdict": "not_ruled_out",
                      "verdict_applies_to": "mean_excess_pct"}),
            [])

    def test_a_declaration_naming_nothing_does_not_count(self):
        for key in ("verdict", "verdict_over_control"):
            with self.subTest(key=key):
                self.assertEqual(
                    unscoped({"mean_excess_pct": 1.0, key: "not_ruled_out",
                              "verdict_applies_to": "a_column_not_here"}),
                    [key])

    def test_a_declaration_may_add_prose_after_the_column_name(self):
        # "mean_return_full_horizon_pct 对零" names the column and then says
        # what it is measured against; both parts are worth keeping.
        self.assertEqual(
            unscoped({"mean_return_full_horizon_pct": 3.7,
                      "full_horizon_verdict": "not_ruled_out",
                      "verdict_applies_to": "mean_return_full_horizon_pct 对零"}),
            [])

    def test_a_matching_suffix_is_enough(self):
        self.assertEqual(
            unscoped({"excess_over_control_pct": 0.3,
                      "verdict_over_control": "underpowered"}),
            [])


class TheBacktestSummaryIsScoped(unittest.TestCase):
    def test_no_verdict_ships_without_its_quantity(self):
        path = Path(config.DATA) / "ideagen.db"
        if not path.exists():
            self.skipTest(f"no project database at {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "backtest_runs" not in names:
                self.skipTest("backtest_runs not created yet")
            row = con.execute("SELECT summary FROM backtest_runs "
                              "ORDER BY ended_at DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            self.skipTest("no backtest has been run yet")
        summary = json.loads(row[0])
        loose = unscoped(summary)
        self.assertEqual(
            loose, [],
            "a verdict that does not name its quantity will be read against "
            "whichever number it is printed beside")


if __name__ == "__main__":
    unittest.main()
