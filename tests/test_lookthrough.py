"""What the look-through layer must refuse to do.

The interesting failures here are not crashes. They are confident numbers: an
opaque commodity fund scored as "0% semiconductors", a failed API call filed as
"not a fund", ten overlapping ETFs reported as ten independent bets. Each of
those is a plausible-looking answer that would be believed, so each gets a test.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

from ideagen import db, lookthrough as lt


def F(sym, weights, coverage=1.0, rows=None, status="ok", note="",
      labels=None):
    """A fund fixture. Storage is keyed by ISIN and displayed by ticker; these
    fixtures key by ticker directly, which is the degenerate case where the two
    coincide — every assertion below is about the arithmetic, not the keying."""
    return lt.Fund(sym, "2026-09-05", weights,
                   labels if labels is not None else {k: k for k in weights},
                   coverage, rows if rows is not None else len(weights),
                   status, note)


class Coverage(unittest.TestCase):
    def test_opaque_fund_is_not_comparable(self):
        """DBC and PDBC are both broad commodity baskets whose holdings carry no
        tickers. Reporting 0% overlap is worse than reporting nothing."""
        a = F("DBC", {}, coverage=0.0, rows=59, status="opaque")
        b = F("PDBC", {}, coverage=0.0, rows=4, status="opaque")
        self.assertIsNone(lt.overlap(a, b))

    def test_below_floor_is_not_comparable(self):
        a = F("X", {"AAA": 1.0}, coverage=0.4, status="opaque")
        b = F("Y", {"AAA": 1.0})
        self.assertIsNone(lt.overlap(a, b))

    def test_opaque_fund_absent_from_theme_ranking_not_zero(self):
        funds = {"SMH": F("SMH", {"NVDA": 0.6, "AVGO": 0.4}),
                 "DBC": F("DBC", {}, coverage=0.0, rows=59, status="opaque")}
        hits = lt.resolve_theme(funds, ["NVDA", "AVGO"])
        self.assertEqual([h.symbol for h in hits], ["SMH"])

    def test_error_is_not_filed_as_not_a_fund(self):
        """A rate-limited call and a single stock both come back with no rows.
        Conflating them is how a dead port reports success."""
        s, _rc, _note = lt._classify({}, {}, 0.0, 0)
        self.assertEqual(s, "not_a_fund")
        err = F("QQQ", {}, coverage=0.0, rows=0, status="error", note="429")
        self.assertNotEqual(err.status, "not_a_fund")
        self.assertFalse(err.usable)


    def test_currency_fund_is_opaque_not_transparent(self):
        """FXY names identifiers for 100% of NAV and holds only JPY deposits.
        Gross coverage calls that fully transparent; its look-through is empty."""
        w = {"CASHJPY06": 1.0}
        labels = {"CASHJPY06": "CASH & EQUIVALENTS"}
        status, rc, note = lt._classify(w, labels, 1.0, 2)
        self.assertEqual(status, "opaque")
        self.assertAlmostEqual(rc, 0.0)
        self.assertIn("现金", note)

    def test_tbill_collateral_is_cash_by_identifier(self):
        """KMLM's five holdings are T-bills labelled `B 09/29/26`. Nothing in
        that string says cash; the CUSIP prefix does. Read as securities they
        made two trend funds look 0% alike, which was collateral, not a finding."""
        self.assertTrue(lt.is_cash("B 09/29/26", "US912797VE44"))
        self.assertFalse(lt.is_cash("TREASURY BOND 4.75% 05/15/2055",
                                    "US912810UK24"))

    def test_futures_are_not_securities(self):
        """A futures weight is notional exposure, not share of NAV. Adding it to
        equity weights is a category error before it is a coverage problem."""
        self.assertTrue(lt.is_derivative("US 2YR NOTE (CBT) DEC26"))
        self.assertTrue(lt.is_derivative("JPN YEN CURR FUT  SEP26"))
        self.assertFalse(lt.is_derivative("NVIDIA CORP"))

    def test_trend_funds_fall_below_the_floor(self):
        """DBMF is futures, KMLM is T-bills; neither can be seen through."""
        dbmf = {"ADI3BN6H1": 0.6, "ADI2VB430": 0.4}
        dl = {"ADI3BN6H1": "US 2YR NOTE (CBT) DEC26",
              "ADI2VB430": "JPN YEN CURR FUT  SEP26"}
        self.assertEqual(lt._classify(dbmf, dl, 0.81, 4)[0], "opaque")
        kmlm = {"US912797VE44": 0.5, "US912797SK41": 0.5}
        kl = {"US912797VE44": "B 09/29/26", "US912797SK41": "B 10/29/26"}
        self.assertEqual(lt._classify(kmlm, kl, 0.65, 5)[0], "opaque")

    def test_risk_coverage_is_net_of_cash(self):
        w = {"A": 0.5, "C": 0.5}
        labels = {"A": "NVDA", "C": "CASH & EQUIVALENTS"}
        self.assertAlmostEqual(lt.risk_coverage(w, labels, 1.0), 0.5)


class Overlap(unittest.TestCase):
    def test_identical_funds_overlap_fully(self):
        a = F("A", {"X": 0.5, "Y": 0.5})
        self.assertAlmostEqual(lt.overlap(a, F("B", {"X": 0.5, "Y": 0.5})), 1.0)

    def test_disjoint_funds_overlap_zero(self):
        a, b = F("A", {"X": 1.0}), F("B", {"Y": 1.0})
        self.assertAlmostEqual(lt.overlap(a, b), 0.0)

    def test_overlap_is_min_weight_not_name_count(self):
        """Holding all the same names at different sizes is not the same bet."""
        a = F("A", {"X": 0.9, "Y": 0.1})
        b = F("B", {"X": 0.1, "Y": 0.9})
        self.assertAlmostEqual(lt.overlap(a, b), 0.2)

    def test_collisions_finds_both_directions(self):
        funds = {"USMV": F("USMV", {"A": 0.7, "B": 0.3}),
                 "SPLV": F("SPLV", {"C": 0.7, "B": 0.3}),
                 "SMH":  F("SMH", {"A": 0.7, "B": 0.3})}
        labels = {"USMV": "低波", "SPLV": "低波", "SMH": "半导体"}
        same, diff = lt.collisions(funds, labels)
        self.assertEqual([(r[1], r[2]) for r in same], [("SPLV", "USMV")])
        self.assertIn(("SMH", "半导体", "USMV", "低波"),
                      [(r[0], r[1], r[2], r[3]) for r in diff])


class Theme(unittest.TestCase):
    def test_ranks_by_through_weight_not_hit_count(self):
        """Nine of ten primes at 0.2% each is not a defence position."""
        funds = {"ITA": F("ITA", {"LMT": 0.3, "RTX": 0.3, "OTHER": 0.4}),
                 "RSP": F("RSP", {n: 0.002 for n in
                                  ["LMT", "RTX", "NOC", "GD", "LHX",
                                   "BA", "HII", "LDOS", "TDG"]}
                          | {"OTHER": 0.982})}
        hits = lt.resolve_theme(funds, ["LMT", "RTX", "NOC", "GD", "LHX",
                                        "BA", "HII", "LDOS", "TDG", "HWM"])
        self.assertEqual(hits[0].symbol, "ITA")
        self.assertGreater(hits[0].weight, hits[1].weight)

    def test_cash_never_scores_a_theme(self):
        funds = {"BIL": F("BIL", {"CASH & EQUIVALENTS": 1.0})}
        self.assertEqual(lt.resolve_theme(funds, ["CASH & EQUIVALENTS"]), [])

    def test_basket_match_is_case_insensitive(self):
        funds = {"SMH": F("SMH", {"NVDA": 1.0})}
        self.assertEqual(len(lt.resolve_theme(funds, ["nvda"])), 1)


class Discover(unittest.TestCase):
    """What the shelf is missing is a fact about the shelf, not about the theme.

    `resolve_theme` returning nothing for a gold-miners basket reads as "no
    vehicle expresses this" and actually means "this shelf carries physical GLD
    and no miners fund" — a curation gap, and a gap has no label to be found by.
    """

    EXPOSURE = {
        "NEM": [{"symbol": "GDX", "weightPercentage": 12.0},
                {"symbol": "ZWT-T.TO", "weightPercentage": 40.0},
                {"symbol": "SPY", "weightPercentage": 0.2}],
        "AEM": [{"symbol": "GDX", "weightPercentage": 11.0},
                {"symbol": "TINY", "weightPercentage": 1.0}],
    }

    def _run(self, **kw):
        with mock.patch.object(lt.fmp, "asset_exposure",
                               lambda n: self.EXPOSURE.get(n, [])):
            return lt.discover(["NEM", "AEM"], {"SPY"}, **kw)

    def test_sums_the_basket_across_one_fund(self):
        hits = self._run()
        self.assertEqual(hits[0].symbol, "GDX")
        self.assertAlmostEqual(hits[0].weight, 0.23)

    def test_excludes_what_the_shelf_already_has(self):
        self.assertNotIn("SPY", [h.symbol for h in self._run(floor=0.0)])

    def test_excludes_listings_this_desk_cannot_trade(self):
        """The reverse index for a US name opens with Canadian covered-call
        funds; a shelf proposal leading with ZWT-T.TO is noise."""
        self.assertNotIn("ZWT-T.TO", [h.symbol for h in self._run(floor=0.0)])

    def test_floor_drops_incidental_holders(self):
        self.assertEqual([h.symbol for h in self._run()], ["GDX"])


class Portfolio(unittest.TestCase):
    def test_overlapping_etfs_are_fewer_bets_than_they_look(self):
        """Three ETFs holding the same name are not three bets."""
        funds = {s: F(s, {"NVDA": 0.9, s + "X": 0.1}) for s in
                 ("SMH", "SOXX", "QQQ")}
        e = lt.portfolio(funds, ["SMH", "SOXX", "QQQ"])
        self.assertLess(e.effective_names, 2.0)
        self.assertAlmostEqual(e.top(1)[0][1], 0.9, places=6)

    def test_opaque_members_are_named_not_dropped_silently(self):
        funds = {"SMH": F("SMH", {"NVDA": 1.0}),
                 "GLD": F("GLD", {}, coverage=0.0, rows=1, status="opaque")}
        e = lt.portfolio(funds, ["SMH", "GLD"])
        self.assertEqual(e.opaque, ("GLD",))
        self.assertAlmostEqual(e.coverage, 0.5)

    def test_cash_is_excluded_from_names_and_reported(self):
        funds = {"A": F("A", {"NVDA": 0.6, "CASH & EQUIVALENTS": 0.4})}
        e = lt.portfolio(funds, ["A"])
        self.assertEqual(list(e.names), ["NVDA"])
        self.assertAlmostEqual(e.cash, 0.4)

    def test_explicit_weights_must_match(self):
        funds = {"A": F("A", {"X": 1.0})}
        with self.assertRaises(ValueError):
            lt.portfolio(funds, ["A", "B"], [1.0])


class Differentiator(unittest.TestCase):
    """The menu line has to differentiate, and it is paid for on every call."""

    def test_concentrated_fund_shows_its_names(self):
        rest = {f"R{i}": .47 / 117 for i in range(117)}   # as ITA is actually built
        f = {"ITA": F("ITA", {"GE": .22, "RTX": .17, "BA": .09, "GD": .05}
                      | rest)}
        d = lt.differentiator(f, "ITA")
        self.assertIn("GE 22%", d)
        self.assertIn("合计 53%", d)

    def test_flat_fund_says_it_is_flat_instead_of_naming_nothing(self):
        """A 1,300-bond credit fund's four largest are ~1% each. Printing
        `ANHEUSER-BUSCH 4.90% 02/01/2046 0%` is noise, and it is also the
        longest line in the menu."""
        f = {"LQD": F("LQD", {f"B{i}": 1 / 3000 for i in range(3000)})}
        d = lt.differentiator(f, "LQD")
        self.assertIn("高度分散", d)
        self.assertIn("3000", d)
        self.assertNotIn("前4大 B0", d)

    def test_long_bond_descriptions_are_cut(self):
        f = {"X": F("X", {"ANHEUSER-BUSCH COMPANIES LLC 4.90% 02/01/2046": .5}
                    | {f"R{i}": .5 / 50 for i in range(50)})}
        self.assertNotIn("02/01/2046", lt.differentiator(f, "X"))

    def test_opaque_and_stock_are_named_not_blank(self):
        f = {"GLD": lt.Fund("GLD", "d", {}, {}, 0.0, 1, "opaque", ""),
             "VRT": lt.Fund("VRT", "d", {}, {}, 0.0, 0, "not_a_fund", "")}
        self.assertIn("不可穿透", lt.differentiator(f, "GLD"))
        self.assertIn("个股", lt.differentiator(f, "VRT"))

    def test_all_cash_holdings_do_not_produce_an_empty_top_list(self):
        """The bug this replaced printed 前0大（合计 0%）."""
        f = {"BIL": F("BIL", {"CASH & EQUIVALENTS": 1.0})}
        d = lt.differentiator(f, "BIL")
        self.assertNotIn("前0大", d)
        self.assertIn("不可穿透", d)


class Storage(unittest.TestCase):
    def test_roundtrip_preserves_status_and_note(self):
        with tempfile.TemporaryDirectory() as d:
            con = db.connect(Path(d) / "t.db")
            lt.ensure_schema(con)
            with db.tx(con) as c:
                c.execute("INSERT INTO etf_lookthrough_runs VALUES"
                          " (?,?,?,?,?,?,?)",
                          ("GLD", "2026-09-05", 0.0, 1, 0, "opaque", "实物"))
                c.execute("INSERT INTO etf_lookthrough_runs VALUES"
                          " (?,?,?,?,?,?,?)",
                          ("SMH", "2026-09-05", 1.0, 2, 1, "ok", None))
                c.execute("INSERT INTO etf_lookthrough"
                          "(symbol,as_of,asset,label,weight) VALUES"
                          " ('SMH','2026-09-05','US67066G1040','NVDA',1.0)")
            funds = lt.load(con)
            self.assertEqual(funds["GLD"].status, "opaque")
            self.assertEqual(funds["GLD"].note, "实物")
            self.assertTrue(funds["SMH"].usable)
            self.assertEqual(funds["SMH"].weights, {"US67066G1040": 1.0})
            self.assertEqual(funds["SMH"].name("US67066G1040"), "NVDA")

    def test_as_of_never_reads_the_future(self):
        """A July replay must not score its thesis against September holdings."""
        with tempfile.TemporaryDirectory() as d:
            con = db.connect(Path(d) / "t.db")
            lt.ensure_schema(con)
            with db.tx(con) as c:
                for day, w in (("2026-07-01", 0.1), ("2026-09-01", 0.9)):
                    c.execute("INSERT INTO etf_lookthrough_runs VALUES"
                              " (?,?,?,?,?,?,?)", ("QQQ", day, 1.0, 1, 1,
                                                   "ok", None))
                    c.execute("INSERT INTO etf_lookthrough"
                              "(symbol,as_of,asset,label,weight) VALUES"
                              " ('QQQ',?,'NVDA','NVDA',?)", (day, w))
            self.assertAlmostEqual(
                lt.load(con, date(2026, 8, 1))["QQQ"].weights["NVDA"], 0.1)
            self.assertAlmostEqual(
                lt.load(con)["QQQ"].weights["NVDA"], 0.9)


if __name__ == "__main__":
    unittest.main()
