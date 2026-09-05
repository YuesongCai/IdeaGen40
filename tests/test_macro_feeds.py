"""What the macro, volatility and positioning feeds must refuse to report.

None of the failures worth testing here is a crash. Each one is a well-formed,
confident number that would be believed:

  * COT positioning from February 2024 presented as this week's
  * "Congress bought Technology" when it was one member and one micro-cap
  * a `previous` print quietly promoted into a consensus, so a watchpoint fires
    on month-over-month drift and reports it as a surprise
  * two long release names colliding into one row, but only on the cloud
    instance, because MySQL truncates a key that SQLite keeps whole
  * an empty volatility complex read as a day on which the market had no
    implied vol

The vendor returns HTTP 200 for every one of those, so the tests stand in for
the error the transport never raises.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from ideagen import feeds, schema
from ideagen.feeds_impl import calendar_fmp as cal
from ideagen.feeds_impl import corpus_fmp as cnews
from ideagen.feeds_impl import positioning_fmp as pos

AS_OF = date(2026, 9, 5)


def _cot_row(sym, d, cur=50.0, prev=48.0):
    return {"symbol": sym, "date": f"{d} 00:00:00", "name": sym,
            "currentLongMarketSituation": cur,
            "previousLongMarketSituation": prev,
            "marketSentiment": "Increasing Bullish", "netPostion": 1000,
            "changeInNetPosition": 10, "reversalTrend": False}


def _trade(sym, disclosure, typ="Purchase", amount="$1,001 - $15,000",
           office="Rep. A"):
    return {"symbol": sym, "disclosureDate": disclosure,
            "transactionDate": "2025-11-06", "type": typ, "amount": amount,
            "office": office, "assetDescription": sym}


class CotStaleness(unittest.TestCase):
    """The trap that motivated this feed: `?symbol=ES` answers 200 with rows
    whose newest date is 2024-02-27, and says so nowhere."""

    def test_stale_window_raises_rather_than_reporting_2024_as_now(self):
        old = [_cot_row("ES", "2024-02-27"), _cot_row("GC", "2024-02-27")]
        with mock.patch.object(pos.fmp, "cot", return_value=old):
            with self.assertRaises(RuntimeError) as e:
                list(pos.fmp_cot(AS_OF, {"contracts": ["ES", "GC"]}))
        self.assertIn("2024-02-27", str(e.exception))

    def test_fresh_window_is_emitted_as_a_level_not_a_dated_trigger(self):
        """A COT print is a state of the world, not a future event. Filed as a
        dated event it would land in gen_carl's trigger list with a date already
        past — a trigger nobody can act on."""
        fresh = [_cot_row("ES", "2026-09-01", cur=43.33)]
        with mock.patch.object(pos.fmp, "cot", return_value=fresh):
            rows = list(pos.fmp_cot(AS_OF, {"contracts": ["ES"]}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "level")
        self.assertEqual(rows[0]["actual"], 43.33)

    def test_empty_range_is_an_outage_not_a_quiet_week(self):
        with mock.patch.object(pos.fmp, "cot", return_value=[]):
            with self.assertRaises(RuntimeError):
                list(pos.fmp_cot(AS_OF, {}))

    def test_rows_for_other_contracts_do_not_count_as_coverage(self):
        """301 rows came back and none was a contract this book trades. That is
        a shape change at the vendor, not a period with no positioning in it."""
        with mock.patch.object(pos.fmp, "cot",
                               return_value=[_cot_row("ZZZ", "2026-09-01")]):
            with self.assertRaises(RuntimeError):
                list(pos.fmp_cot(AS_OF, {"contracts": ["ES"]}))


class CongressShape(unittest.TestCase):
    def test_single_member_micro_cap_is_reported_as_concentration(self):
        """Observed live: 68 of 200 rows were one member in one micro-cap. A
        sector total built from that is not a consensus, and the summary row has
        to say so on the same line as the number."""
        rows = [_trade("TKNO", "2026-09-02") for _ in range(68)]
        rows += [_trade("AAPL", "2026-09-02")] * 32
        with mock.patch.object(pos.fmp, "congress_trades",
                               side_effect=lambda ch: rows if ch == "house" else []), \
             mock.patch.object(pos.fmp, "profile",
                               return_value={"sector": "Technology"}):
            out = list(pos.fmp_congress_flow(AS_OF, {}))
        summary = out[0]
        self.assertEqual(summary["top_symbol"], "TKNO")
        self.assertAlmostEqual(summary["top_symbol_share"], 0.68, places=2)
        self.assertIn("TKNO", summary["label"])
        self.assertIn("68%", summary["label"])

    def test_keys_on_disclosure_date_not_transaction_date(self):
        """Transaction dates trail disclosure by up to ten months. Keyed on the
        trade, this period's signal is backdated into last year — and a replay
        of last year then sees information that was not public in it."""
        old_disclosure = (AS_OF - timedelta(days=400)).isoformat()
        rows = [_trade("AAPL", old_disclosure)]
        with mock.patch.object(pos.fmp, "congress_trades",
                               side_effect=lambda ch: rows if ch == "house" else []), \
             mock.patch.object(pos.fmp, "profile",
                               return_value={"sector": "Technology"}):
            with self.assertRaises(RuntimeError):
                list(pos.fmp_congress_flow(AS_OF, {"window_days": 45}))

    def test_amount_is_a_band_midpoint_and_every_label_says_so(self):
        self.assertEqual(pos._amount_midpoint("$1,001 - $15,000"), 8000.5)
        self.assertEqual(pos._amount_midpoint("$15,000"), 15000.0)
        self.assertEqual(pos._amount_midpoint(""), 0.0)
        rows = [_trade("AAPL", "2026-09-02", amount="$50,001 - $100,000")]
        with mock.patch.object(pos.fmp, "congress_trades",
                               side_effect=lambda ch: rows if ch == "house" else []), \
             mock.patch.object(pos.fmp, "profile",
                               return_value={"sector": "Technology"}):
            out = list(pos.fmp_congress_flow(AS_OF, {}))
        sector_rows = [r for r in out if r["event_id"].startswith("congress:Tech")]
        self.assertTrue(sector_rows)
        self.assertIn("非精确值", sector_rows[0]["label"])

    def test_exchanges_and_gifts_carry_no_direction(self):
        self.assertIs(pos._is_buy("Exchange"), None)
        self.assertTrue(pos._is_buy("Purchase"))
        self.assertFalse(pos._is_buy("Sale (Full)"))


class MacroReleases(unittest.TestCase):
    def _fetch(self, rows, params=None):
        with mock.patch.object(cal.fmp, "economic_calendar", return_value=rows):
            return list(cal.fmp_macro_releases(AS_OF, params or {"impacts": ["High"],
                                                                 "countries": ["US"]}))

    def test_previous_is_never_promoted_into_an_expectation(self):
        """A watchpoint written against a `previous` fires on the difference
        between this month and last, and reports it as a surprise."""
        out = self._fetch([{"date": "2026-09-17 12:30:00", "country": "US",
                            "event": "Housing Starts (Aug)", "impact": "High",
                            "previous": 1.239, "estimate": None, "unit": "M"}])
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["expectation"])
        self.assertEqual(out[0]["previous"], 1.239)

    def test_fed_decision_is_policy_not_a_data_release(self):
        out = self._fetch([
            {"date": "2026-09-16 18:00:00", "country": "US", "impact": "High",
             "event": "Fed Interest Rate Decision", "estimate": 4, "previous": 3.75,
             "unit": "%"},
            {"date": "2026-09-11 12:30:00", "country": "US", "impact": "High",
             "event": "Core Inflation Rate YoY (Aug)", "estimate": 2.4,
             "previous": 2.5, "unit": "%"}])
        kinds = {r["label"]: r["kind"] for r in out}
        self.assertEqual(kinds["Fed Interest Rate Decision"], "policy")
        self.assertEqual(kinds["Core Inflation Rate YoY (Aug)"], "macro_release")

    def test_backward_window_carries_the_actual_that_settles_a_watchpoint(self):
        past = (AS_OF - timedelta(days=3)).isoformat()
        out = self._fetch([{"date": f"{past} 12:30:00", "country": "US",
                            "impact": "High", "event": "Non Farm Payrolls (Aug)",
                            "estimate": 75, "previous": 60, "actual": 22,
                            "unit": "K"}])
        self.assertEqual(out[0]["actual"], 22.0)
        self.assertEqual(out[0]["expectation"], "75.0K")

    def test_other_countries_and_impacts_are_filtered_not_relabelled(self):
        out = self._fetch([
            {"date": "2026-10-05 22:00:00", "country": "CO", "impact": "Low",
             "event": "Monetary Policy Meeting Minutes"},
            {"date": "2026-09-11 12:30:00", "country": "US", "impact": "Low",
             "event": "Cleveland CPI MoM (Aug)"}])
        self.assertEqual(out, [])

    def test_event_id_survives_the_mysql_key_width(self):
        """`events.event_id` is VARCHAR(128) on MySQL and TEXT on SQLite. A long
        key that collides only after truncation would merge two releases into one
        row on the cloud instance and pass every test on this laptop."""
        long_a = "Core Inflation Rate MoM " + "x" * 160
        long_b = "Core Inflation Rate YoY " + "x" * 160
        a = cal._event_id("US", "2026-09-11", "12:30", long_a)
        b = cal._event_id("US", "2026-09-11", "12:30", long_b)
        self.assertLessEqual(len(a), 128)
        self.assertLessEqual(len(b), 128)
        self.assertNotEqual(a, b)

    def test_short_ids_stay_readable(self):
        self.assertEqual(cal._event_id("US", "2026-09-11", "12:30", "CPI"),
                         "fmp:US:2026-09-11:12:30:CPI")


class VolSurface(unittest.TestCase):
    def test_empty_complex_is_an_outage_not_a_calm_market(self):
        with mock.patch.object(cal.fmp, "quotes", return_value={}):
            with self.assertRaises(RuntimeError):
                list(cal.fmp_vol_surface(AS_OF, {"symbols": ["^VIX"]}))

    def test_term_structure_ratio_is_computed_not_left_to_the_model(self):
        q = {"^VIX": {"price": 14.53}, "^VIX3M": {"price": 17.61},
             "^VIX9D": {"price": 11.97}}
        with mock.patch.object(cal.fmp, "quotes", return_value=q):
            rows = list(cal.fmp_vol_surface(AS_OF, {"symbols": list(q)}))
        ratios = {r["label"]: r["actual"] for r in rows if r["unit"] == "x"}
        self.assertEqual(len(ratios), 2)
        self.assertAlmostEqual(next(v for k, v in ratios.items() if "1M/3M" in k),
                               14.53 / 17.61, places=4)

    def test_a_missing_index_loses_its_ratio_not_the_whole_feed(self):
        with mock.patch.object(cal.fmp, "quotes",
                               return_value={"^VIX": {"price": 14.53}}):
            rows = list(cal.fmp_vol_surface(AS_OF, {"symbols": ["^VIX", "^VIX3M"]}))
        self.assertEqual([r["unit"] for r in rows], [""])


class Curve(unittest.TestCase):
    CURVE = [{"date": "2026-09-04", "month3": 3.91, "year2": 4.37, "year5": 4.54,
              "year10": 4.78, "year20": 5.25, "year30": 5.24}]

    def test_does_not_re_emit_what_fred_already_publishes(self):
        """DGS10 and DGS30 arriving twice under two labels is how a model comes
        to believe two independent sources agree."""
        with mock.patch.object(cal.fmp, "treasury_curve", return_value=self.CURVE):
            rows = list(cal.fmp_curve(AS_OF, {}))
        tenors = [r["label"] for r in rows if r["unit"] == "pct"]
        self.assertNotIn("美债 10 年", tenors)
        self.assertNotIn("美债 30 年", tenors)
        self.assertIn("美债 2 年", tenors)

    def test_spreads_are_basis_points_and_signed(self):
        with mock.patch.object(cal.fmp, "treasury_curve", return_value=self.CURVE):
            rows = {r["event_id"].split(":")[1]: r for r in
                    cal.fmp_curve(AS_OF, {})}
        self.assertAlmostEqual(rows["2s10s"]["actual"], 41.0, places=1)
        self.assertEqual(rows["2s10s"]["unit"], "bp")

    def test_no_dated_row_is_an_outage(self):
        with mock.patch.object(cal.fmp, "treasury_curve", return_value=[]):
            with self.assertRaises(RuntimeError):
                list(cal.fmp_curve(AS_OF, {}))


class Registration(unittest.TestCase):
    def test_every_new_feed_validates_against_its_kind(self):
        names = {"fmp_macro_releases", "fmp_vol_surface", "fmp_curve",
                 "fmp_cot", "fmp_congress_flow"}
        registered = {s["name"]: s for s in feeds.available("calendar")}
        self.assertTrue(names <= set(registered), names - set(registered))

    def test_calendar_rows_satisfy_the_calendar_schema(self):
        rows = [{"event_id": "x", "date": "2026-09-11", "label": "CPI",
                 "kind": "macro_release"}]
        self.assertEqual(feeds.validate("calendar", rows, feed="t"), [])

    def test_news_is_corpus_and_off_by_default(self):
        spec = {s["name"]: s for s in feeds.available("corpus")}
        self.assertIn("fmp_news", spec)
        env = dict(os.environ)
        os.environ.pop("IDEAGEN_CORPUS_FMP_NEWS", None)
        try:
            self.assertFalse(cnews.enabled())
            with mock.patch.object(cnews.fmp, "news_general") as ng:
                self.assertEqual(list(cnews.fmp_news(AS_OF, {})), [])
            ng.assert_not_called()   # disabled must not spend a vendor call
        finally:
            os.environ.clear()
            os.environ.update(env)


class EventsPayload(unittest.TestCase):
    """Extra fields a feed reports must survive the trip into `events`.

    `feeds.py` promises extras are "allowed and preserved". Until the payload
    column existed that promise held in memory and broke at the last step, so a
    replay read a thinner row than the run was handed."""

    def _store(self, path):
        from ideagen.platform.local import SqliteStateStore
        return SqliteStateStore(str(path))

    def test_legacy_events_table_gains_payload_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, "
                        "date TEXT, label TEXT, kind TEXT, expectation TEXT, "
                        "actual TEXT, unit TEXT, source TEXT, as_of TEXT, feed TEXT)")
            con.execute("INSERT INTO events(event_id,date,label,kind) "
                        "VALUES('old:1','2026-01-01','旧行','auction')")
            con.commit()
            con.close()
            schema.migrate(self._store(p))
            con = sqlite3.connect(p)
            cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
            self.assertIn("payload", cols)
            self.assertEqual(
                con.execute("SELECT label FROM events WHERE event_id='old:1'")
                   .fetchone()[0], "旧行")

    def test_impact_and_previous_reach_the_table(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "new.db"
            st = self._store(p)
            schema.migrate(st)
            schema.upsert(st, "events", {
                "event_id": "fmp:US:2026-09-11:12:30:CPI", "date": "2026-09-11",
                "label": "CPI", "kind": "macro_release", "expectation": "2.4%",
                "actual": None, "unit": "%", "source": "FMP", "as_of": "2026-09-05",
                "feed": "fmp_macro_releases",
                "payload": '{"impact": "High", "previous": 2.5}'})
            got = sqlite3.connect(p).execute(
                "SELECT payload FROM events WHERE label='CPI'").fetchone()[0]
            self.assertIn("High", got)


class PromptBlocks(unittest.TestCase):
    """The two defects that only appeared once the prompts were rendered.

    Both were introduced by the feeds above, and neither would have failed a
    test of the feeds: the rows were correct, and the arms that read them broke.
    """

    def _ctx(self, calendar, prices=None):
        from ideagen import strategy
        return strategy.RunContext(as_of=AS_OF, inputs_sha="x", corpus=[],
                                   candidates=[], prices=prices or {},
                                   calendar=calendar, params={})

    def test_carl_does_not_offer_a_past_release_as_a_trigger_date(self):
        """`fmp_macro_releases` returns a backward window so a watchpoint can be
        settled. Sorted ascending under one heading, those rows put last
        Friday's payrolls at the top of a list headed「可作为触发日期」."""
        from ideagen.strategies import gen_carl
        cal = [{"kind": "macro_release", "date": "2026-09-04",
                "label": "Non Farm Payrolls (Aug)", "expectation": "56.0K",
                "actual": 22.0},
               {"kind": "macro_release", "date": "2026-09-11",
                "label": "Core Inflation Rate YoY (Aug)", "expectation": "2.4%",
                "actual": None}]
        block = gen_carl.calendar_block(self._ctx(cal))
        trigger_section = block.split("最近已公布")[0]
        self.assertIn("2026-09-11", trigger_section)
        self.assertNotIn("2026-09-04", trigger_section)
        self.assertIn("实际 22.0", block)

    def test_carl_keeps_the_published_surprise_rather_than_dropping_it(self):
        from ideagen.strategies import gen_carl
        cal = [{"kind": "macro_release", "date": "2026-09-03",
                "label": "ISM Services PMI (Aug)", "expectation": "54.3",
                "actual": 51.1}]
        block = gen_carl.calendar_block(self._ctx(cal))
        self.assertIn("ISM Services PMI", block)
        self.assertIn("不是触发日期", block)

    def test_gap_still_shows_prices_when_levels_outnumber_the_line_budget(self):
        """The calendar went from five levels to forty-two. Levels were appended
        first and the cut was shared, so every instrument price fell off the end
        — in the one arm whose method begins at the price."""
        from ideagen.strategies import gen_gap
        cal = [{"kind": "level", "date": "2026-09-05", "label": f"L{i}",
                "actual": i, "unit": "", "source": "t"} for i in range(42)]
        block = gen_gap.price_block(self._ctx(cal, {"US.SPY": {"close": 770.19}}),
                                    limit=20)
        self.assertIn("US.SPY", block)
        self.assertIn("770.19", block)

    def test_peripheral_levels_cannot_crowd_out_central_ones(self):
        from ideagen.strategies import gen_gap
        cal = [{"kind": "level", "date": "2026-09-05", "label": f"国会 {i}",
                "actual": i, "unit": "", "source": "t", "priority": 3}
               for i in range(30)]
        cal.append({"kind": "level", "date": "2026-09-05", "label": "VIX",
                    "actual": 14.53, "unit": "", "source": "t"})
        block = gen_gap.price_block(self._ctx(cal), limit=5)
        self.assertIn("VIX", block)

    def test_feeds_without_priority_stay_central(self):
        """`calendar_fred` predates the field. Defaulting a missing priority to
        anything but the top would silently demote the levels that have been
        anchoring this arm all along."""
        from ideagen.strategies import gen_gap
        cal = [{"kind": "level", "date": "2026-09-05", "label": "高收益债 OAS",
                "actual": 265.0, "unit": "bp", "source": "FRED"}]
        cal += [{"kind": "level", "date": "2026-09-05", "label": f"国会 {i}",
                 "actual": i, "unit": "", "source": "t", "priority": 3}
                for i in range(10)]
        self.assertIn("高收益债 OAS", gen_gap.price_block(self._ctx(cal), limit=1))

    def test_empty_calendar_still_says_so_out_loud(self):
        from ideagen.strategies import gen_carl, gen_gap
        self.assertIn("不要编造具体日期",
                      gen_carl.calendar_block(self._ctx([])))
        self.assertIn("不是从价格读出",
                      gen_gap.price_block(self._ctx([])))


if __name__ == "__main__":
    unittest.main()
