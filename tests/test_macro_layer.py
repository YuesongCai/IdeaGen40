"""What the consumption layer must refuse to conclude.

Every failure below is a number that would be believed. None is a crash.

  * a z computed against a standard deviation fitted from three observations
  * a surprise attributed to a theme because a release name shared a word with
    it, rather than because the theme's own indicator moved that day
  * copper miners reported as copper positioning with no mark saying so
  * an implied sigma that silently *narrows* a band, when the whole reason to
    reach for implied vol was that bands were too narrow
  * a scoring input applied while its switch is off, which makes this period
    quietly incomparable with the six already booked
  * a regime reading that grew a verdict

The last one is the one to read twice. The others are bugs; that one would be a
methodology change arriving as a convenience.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from unittest import mock

from ideagen import macro


AS_OF = "2026-09-05"


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE events (
        event_id TEXT PRIMARY KEY, date TEXT, label TEXT, kind TEXT,
        expectation TEXT, actual TEXT, unit TEXT, source TEXT,
        as_of TEXT, feed TEXT, payload TEXT)""")
    macro.ensure_schema(con)
    return con


def _event(con, event_id, d, label, kind, expectation=None, actual=None,
           source="FMP economic-calendar (US)", payload=None):
    con.execute("INSERT OR REPLACE INTO events(event_id,date,label,kind,"
                "expectation,actual,unit,source,as_of,feed,payload)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, d, label, kind, expectation,
                 None if actual is None else str(actual), None, source,
                 d, "test", payload))


def _stats(con, key, upto, n, sd, status="ok"):
    con.execute("INSERT OR REPLACE INTO macro_surprise_stats"
                "(event_key,upto,n,mean_err,sd_err,first_d,last_d,status)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (key, upto, n, 0.0, sd, "2024-09-05", upto, status))


class _Theme:
    def __init__(self, code="US.SMH"):
        self.price_indicator = code
        self.id = "T"
        self.label = "t"


# --------------------------------------------------------------- series identity
class TheReferencePeriodIsNotPartOfTheSeriesName(unittest.TestCase):
    """The trap that produced a wrong conclusion before it produced a wrong number.

    FMP writes the reference period into the event name: "Core PCE Price Index
    YoY (Jul)", "GDP Price Index QoQ (Q2)", "Initial Jobless Claims (Aug/22)".
    Keyed on the raw name every print is its own series with n=1, and the first
    two-year fit returned **1 usable series out of 2375** — which reads exactly
    like "this vendor rarely publishes a consensus" and is nothing of the kind.
    Nothing errored, and the number was plausible enough to have been believed.
    """

    def test_the_period_suffix_is_stripped_so_prints_share_a_series(self):
        for raw in ("Core PCE Price Index YoY (Jul)",
                    "Core PCE Price Index YoY (Aug)",
                    "Core PCE Price Index YoY"):
            self.assertEqual(macro._series_name(raw), "core pce price index yoy")
        self.assertEqual(macro._series_name("Initial Jobless Claims (Aug/22)"),
                         "initial jobless claims")
        self.assertEqual(macro._series_name("GDP Price Index QoQ (Q2)"),
                         "gdp price index qoq")

    def test_a_revision_stage_is_not_a_period_and_must_survive(self):
        """A flash estimate and a final print have different error scales.
        Stripping "(Final)" alongside "(Aug)" would fit one distribution across
        two — the opposite error, and a quieter one."""
        for keep in ("Final", "Prel", "Adv", "Flash"):
            self.assertIn(keep.lower(),
                          macro._series_name(f"S&P Global PMI {keep} (Aug)"))

    def test_country_stays_in_the_key(self):
        """Every country publishes "Inflation Rate YoY" and their forecast
        errors are not the same distribution."""
        self.assertNotEqual(macro._event_key("US", "Inflation Rate YoY (Aug)"),
                            macro._event_key("EU", "Inflation Rate YoY (Aug)"))


# --------------------------------------------------------------- surprise
class SurpriseNeedsADistribution(unittest.TestCase):

    def test_a_thin_series_yields_no_z_and_says_why(self):
        """Three prints is a number, not a scale. Dividing by it produces a
        confident z from nothing, which is worse than reporting no z at all."""
        con = _con()
        _event(con, "e1", AS_OF, "Core CPI YoY", "macro_release", "2.4%", 2.6)
        _stats(con, "US|core cpi yoy", AS_OF, n=3, sd=0.1, status="underpowered")
        rows = macro.window_surprises(con, [AS_OF])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["z"])
        self.assertIn("underpowered", rows[0]["why"])

    def test_a_release_with_no_consensus_is_not_a_release_with_a_zero_surprise(self):
        con = _con()
        _event(con, "e1", AS_OF, "Fed Press Conference", "policy", None, None)
        rows = macro.window_surprises(con, [AS_OF])
        self.assertIsNone(rows[0]["z"])
        self.assertEqual(rows[0]["why"], "未结算")

    def test_z_divides_by_the_fitted_sd_not_by_the_level(self):
        con = _con()
        _event(con, "e1", AS_OF, "Core CPI YoY", "macro_release", "2.4%", 2.6)
        _stats(con, "US|core cpi yoy", AS_OF, n=24, sd=0.1)
        rows = macro.window_surprises(con, [AS_OF])
        self.assertAlmostEqual(rows[0]["z"], 2.0, places=6)

    def test_a_fit_made_after_the_window_is_refused_and_says_so(self):
        """The look-ahead guard, and why it needs its own message.

        A fit stamped later than the window has seen prints the window could
        not. Refusing it is right; reporting the refusal as "this series was
        never fitted" is not — a replay of an old period would then look like a
        vendor gap, and someone would go looking for data that is already there.
        """
        con = _con()
        _event(con, "e1", "2026-08-01", "Core CPI YoY", "macro_release",
               "2.4%", 2.6)
        _stats(con, "US|core cpi yoy", "2026-09-05", n=24, sd=0.1)
        rows = macro.window_surprises(con, ["2026-08-01"])
        self.assertIsNone(rows[0]["z"])
        self.assertIn("2026-09-05", rows[0]["why"])
        self.assertIn("不回填", rows[0]["why"])

    def test_an_absent_release_and_an_unscoreable_one_stay_distinguishable(self):
        """`window_surprises` returns the unscoreable row. A caller that filters
        on `z is not None` sees the same thing either way, but the report and the
        stored metadata do not — which is what makes "we looked and could not
        judge it" survivable a month later."""
        con = _con()
        _event(con, "e1", AS_OF, "Retail Sales MoM", "macro_release", "0.3", 0.4)
        rows = macro.window_surprises(con, [AS_OF])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["z"])
        self.assertIn("误差分布", rows[0]["why"])


class AttributionRunsThroughPriceNotNames(unittest.TestCase):
    """The join this module refuses to make.

    Matching a release name against a theme's terms is the failure `lookthrough`
    was built to remove: ITA / PPA / XAR all read 国防军工 while holding 53% /
    45% / 26% of the same basket. A release attributed to a theme because both
    say "inflation" is that mistake with a new label.
    """

    def setUp(self):
        self.con = _con()
        _event(self.con, "e1", "2026-09-02", "Core CPI YoY", "macro_release",
               "2.4%", 2.6)
        _stats(self.con, "US|core cpi yoy", "2026-09-04", n=24, sd=0.1)
        self.ev = {"days": ["2026-09-02", "2026-09-03", "2026-09-04"]}

    def test_no_release_on_the_indicators_biggest_day_means_no_consensus_z(self):
        moves = {"2026-09-02": 0.4, "2026-09-03": 2.9, "2026-09-04": 0.1}
        with mock.patch("ideagen.sources.futu_px.move_z",
                        side_effect=lambda c, code, d: moves.get(d)):
            z, meta = macro.theme_consensus_z(self.con, self.ev, _Theme(),
                                              require_flag=False)
        self.assertIsNone(z)
        self.assertEqual(meta["indicator_day"], "2026-09-03")
        self.assertIn("当天没有已结算发布", meta["reason"])

    def test_the_release_on_that_day_is_the_one_used(self):
        moves = {"2026-09-02": 3.1, "2026-09-03": 0.2, "2026-09-04": 0.1}
        with mock.patch("ideagen.sources.futu_px.move_z",
                        side_effect=lambda c, code, d: moves.get(d)):
            z, meta = macro.theme_consensus_z(self.con, self.ev, _Theme(),
                                              require_flag=False)
        self.assertAlmostEqual(z, 2.0, places=6)
        self.assertEqual(meta["release"], "Core CPI YoY")
        self.assertEqual(meta["release_date"], "2026-09-02")

    def test_a_theme_that_did_not_react_does_not_inherit_the_mornings_surprise(self):
        """Caught on the first live run against real data.

        "The biggest day in the window" always names some day, so on a quiet
        window TERM-PREMIUM picked up the full +1.35 sigma of that morning's
        payrolls on an indicator move of 0.34 sigma, and POLICY-PATH on 0.38.
        That is the attribution the price join was supposed to prevent, arriving
        through the join itself.
        """
        moves = {"2026-09-02": 0.34, "2026-09-03": 0.2, "2026-09-04": 0.1}
        with mock.patch("ideagen.sources.futu_px.move_z",
                        side_effect=lambda c, code, d: moves.get(d)):
            z, meta = macro.theme_consensus_z(self.con, self.ev, _Theme(),
                                              require_flag=False)
        self.assertIsNone(z)
        self.assertIn("没有反应", meta["reason"])
        self.assertEqual(meta["min_reaction_z"], macro.MIN_REACTION_Z)

    def test_the_switch_being_off_records_the_finding_and_applies_nothing(self):
        """The point of the off state. If it returned nothing at all there would
        be no evidence to decide the switch with, and the decision would stay
        where it has been: an argument."""
        moves = {"2026-09-02": 3.1}
        os.environ.pop("IDEAGEN_FACTOR_N_CONSENSUS", None)
        with mock.patch("ideagen.sources.futu_px.move_z",
                        side_effect=lambda c, code, d: moves.get(d)):
            z, meta = macro.theme_consensus_z(self.con, self.ev, _Theme())
        self.assertIsNone(z)                       # nothing applied
        self.assertFalse(meta["enabled"])
        self.assertAlmostEqual(meta["z"], 2.0)     # everything recorded


# --------------------------------------------------------------- positioning
class PositioningNamesItsOwnLink(unittest.TestCase):

    def test_copper_miners_are_reported_as_a_proxy_not_as_copper(self):
        """COPX is equity with copper beta. Reported at the same confidence as
        CPER it would let a reader conclude the book's copper positioning is
        measured when it is inferred."""
        con = _con()
        _event(con, "cot:HG:2026-09-01", "2026-09-01", "铜 投机净多占比",
               "level", actual=62.5, source="FMP COT HG（2026-09-01 当期）")
        direct, dmeta = macro.positioning_crowding(con, "US.CPER", AS_OF)
        proxy, pmeta = macro.positioning_crowding(con, "US.COPX", AS_OF)
        self.assertEqual((direct, proxy), (62.5, 62.5))
        self.assertEqual(dmeta["link"], "direct")
        self.assertEqual(pmeta["link"], "proxy")

    def test_an_unmapped_instrument_returns_none_rather_than_a_guess(self):
        """DBC and PDBC are baskets with no single contract behind them, and the
        factor-tilt funds are not the S&P. Mapping them to raise coverage is the
        label assertion this repo already paid to remove."""
        con = _con()
        for code in ("US.DBC", "US.PDBC", "US.VLUE", "US.QUAL", "US.KWEB"):
            v, meta = macro.positioning_crowding(con, code, AS_OF)
            self.assertIsNone(v, code)
            self.assertEqual(meta["link"], "none", code)

    def test_a_missing_cot_row_is_not_an_uncrowded_market(self):
        con = _con()
        v, meta = macro.positioning_crowding(con, "US.GLD", AS_OF)
        self.assertIsNone(v)
        self.assertIn("没有", meta["note"])

    def test_the_lag_is_reported_because_cot_is_weekly(self):
        con = _con()
        _event(con, "cot:GC:2026-08-25", "2026-08-25", "黄金 投机净多占比",
               "level", actual=88.95)
        v, meta = macro.positioning_crowding(con, "US.GLD", AS_OF)
        self.assertAlmostEqual(v, 89.0, places=1)
        self.assertEqual(meta["cot_date"], "2026-08-25")
        self.assertEqual(meta["lag_days"], 11)


# --------------------------------------------------------------- sigma
class ImpliedVolMayOnlyWidenTheBand(unittest.TestCase):

    def setUp(self):
        self.con = _con()
        # ^VIX at 14.53 -> 30-day sigma = 14.53 * sqrt(1/12) = 4.19%
        _event(self.con, "fmpvol:^VIX:2026-09-04", "2026-09-04", "VIX",
               "level", actual=14.53)
        os.environ.pop("IDEAGEN_SIGMA_IMPLIED", None)

    def tearDown(self):
        os.environ.pop("IDEAGEN_SIGMA_IMPLIED", None)

    def test_off_by_default_and_the_realised_number_comes_back_unchanged(self):
        got, meta = macro.band_sigma_pct(self.con, "US.SPY", AS_OF, 1.0, 6.0)
        self.assertEqual(got, 6.0)
        self.assertEqual(meta["used"], "realised")
        self.assertAlmostEqual(meta["implied_sigma_pct"], 4.194, places=2)

    def test_on_it_widens_but_never_narrows(self):
        """The defect is one-sided — 264 of 383 orders expired unfilled — so the
        remedy is one-sided. Taking implied unconditionally would sometimes
        narrow a band, which is a second and opposite bet nobody placed."""
        os.environ["IDEAGEN_SIGMA_IMPLIED"] = "1"
        wider, m1 = macro.band_sigma_pct(self.con, "US.SPY", AS_OF, 1.0, 2.0)
        narrower, m2 = macro.band_sigma_pct(self.con, "US.SPY", AS_OF, 1.0, 9.0)
        self.assertGreater(wider, 2.0)
        self.assertEqual(m1["used"], "implied")
        self.assertEqual(narrower, 9.0)
        self.assertEqual(m2["used"], "realised")

    def test_an_unmapped_instrument_keeps_realised_vol(self):
        os.environ["IDEAGEN_SIGMA_IMPLIED"] = "1"
        got, meta = macro.band_sigma_pct(self.con, "US.KWEB", AS_OF, 1.0, 7.5)
        self.assertEqual(got, 7.5)
        self.assertEqual(meta["used"], "realised")
        self.assertIn("波动率指数", meta["note"])

    def test_the_mapping_reports_its_own_quality(self):
        """A wrong entry in `VOL_INDEX_FOR` shows up as a ratio far from 1 in the
        idea's stored metadata, on the first period, without anyone auditing the
        dictionary by hand."""
        _, meta = macro.band_sigma_pct(self.con, "US.SPY", AS_OF, 1.0, 4.2)
        self.assertAlmostEqual(meta["ratio_to_realised"], 1.0, places=1)


# --------------------------------------------------------------- regime
class RegimeStaysAReading(unittest.TestCase):

    def test_it_returns_levels_and_refuses_a_verdict(self):
        """The reference compass read one value for ~90% of thirteen months. A
        composite score invites the gate that six periods cannot support, so
        there is deliberately no score and no bull/bear label to reach for."""
        con = _con()
        _event(con, "fmpvol:^VIX:2026-09-04", "2026-09-04", "VIX", "level",
               actual=14.53)
        _event(con, "fmpcurve:2s10s:2026-09-04", "2026-09-04",
               "美债利差 2s10s", "level", actual=41.0)
        out = macro.regime(con, AS_OF)
        self.assertFalse(out["gate"])
        for banned in ("score", "label", "state", "verdict", "bull", "bear"):
            self.assertNotIn(banned, out)
        self.assertEqual(out["coverage"], "2/6")
        self.assertEqual(out["legs"]["股票隐含波动率"]["value"], 14.53)

    def test_a_leg_with_no_row_is_named_rather_than_dropped(self):
        con = _con()
        out = macro.regime(con, AS_OF)
        self.assertEqual(out["coverage"], "0/6")
        self.assertTrue(all(v["value"] is None for v in out["legs"].values()))


# --------------------------------------------------------------- switches
class SwitchesAreReadLate(unittest.TestCase):

    def tearDown(self):
        for k in ("IDEAGEN_FACTOR_N_CONSENSUS", "IDEAGEN_FACTOR_C_POSITIONING",
                  "IDEAGEN_SIGMA_IMPLIED"):
            os.environ.pop(k, None)

    def test_the_default_is_off_for_every_scoring_input(self):
        """A period scored with these on is a different experiment from the six
        already booked. Defaulting to on would have made that switch silently,
        which is the same mistake `IDEAGEN_UNIVERSE_LOOKTHROUGH` exists to avoid."""
        for k, v in macro.flags().items():
            self.assertFalse(v, k)

    def test_a_switch_set_after_import_still_takes_effect(self):
        """Read at call time, not at import. The scheduler imports this module
        once and then runs for days."""
        self.assertFalse(macro.flags()["sigma_implied"])
        os.environ["IDEAGEN_SIGMA_IMPLIED"] = "1"
        self.assertTrue(macro.flags()["sigma_implied"])


if __name__ == "__main__":
    unittest.main()
