"""P 已定价必须真算、真接、真用（Jon 2026-09-06 §4）。

`backtest._prices` 能算 `priced_in`，但 cli / clauderun / poc 三个周跑入口都没传
`prices`，orchestrator 把 None 当 {}，于是标准周跑 P 全是 50 默认值，面板却画成读数。
这里钉住：有历史的指示标的 P 实测且来源为 return_percentile_21s；无历史的标 default、
`P_measured=False`；weekly 在 prices=None 时自动从库里构建；P 全默认时排名说明写
「取值相同 / 缺数默认」而不是「有区分度」。
"""

from __future__ import annotations

import datetime as dtm
import inspect
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import backtest, lexicon, orchestrator, pricing, strategy as strat  # noqa: E402
from ideagen.platform.local import SqliteStateStore  # noqa: E402
from ideagen.sources import futu_px  # noqa: E402
from ideagen.strategies import topic_hgep  # noqa: E402
from ideagen.strategies.topic_hgep import hgep  # noqa: E402

AS_OF = dtm.date(2026, 9, 2)          # a Wednesday; US clamp = 2026-09-01


def _bars(code, n, end=dtm.date(2026, 9, 1), seed=7):
    """n business-day closes ending on `end`, a deterministic random walk."""
    import random
    rnd = random.Random(seed + len(code))
    out, d, px = [], end, 100.0
    while len(out) < n:
        if d.weekday() < 5:
            out.append((code, d.isoformat(), px))
            px *= 1 + rnd.uniform(-0.02, 0.02)
        d -= dtm.timedelta(days=1)
    return list(reversed(out))


def _con(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE prices (code TEXT, d TEXT, open REAL, high REAL, "
                "low REAL, close REAL, volume REAL, src TEXT, PRIMARY KEY (code, d))")
    con.executemany("INSERT INTO prices (code, d, close) VALUES (?, ?, ?)", rows)
    return con


LONG, SHORT, NONE = "US.LONG", "US.SHORT", "US.NONE"


class ReturnPercentileDetail(unittest.TestCase):
    def test_a_full_history_is_measured_with_its_sample_size_and_cut_off(self):
        con = _con(_bars(LONG, 300))
        d = futu_px.return_percentile_detail(con, LONG, "2026-09-01", window=21)
        self.assertTrue(d["ok"])
        self.assertIsNotNone(d["value"])
        self.assertGreaterEqual(d["n_samples"], 20)
        self.assertEqual(d["last_d"], "2026-09-01")
        self.assertEqual(d["method"], "return_percentile_21s")
        self.assertIsNone(d["reason"])
        self.assertEqual(futu_px.return_percentile(con, LONG, "2026-09-01", 21), d["value"])

    def test_too_few_bars_says_why(self):
        con = _con(_bars(SHORT, 15))
        d = futu_px.return_percentile_detail(con, SHORT, "2026-09-01", window=21)
        self.assertFalse(d["ok"])
        self.assertIsNone(d["value"])
        self.assertIn("15", d["reason"])
        self.assertIsNone(futu_px.return_percentile(con, SHORT, "2026-09-01", 21))

    def test_the_cut_off_is_respected(self):
        con = _con(_bars(LONG, 300))
        d = futu_px.return_percentile_detail(con, LONG, "2026-08-14", window=21)
        self.assertEqual(d["last_d"], "2026-08-14")


class PriceViewCarriesProvenance(unittest.TestCase):
    def setUp(self):
        self.con = _con(_bars(LONG, 300) + _bars(SHORT, 15))
        self.clamp = backtest.clamp_dates(AS_OF)

    def test_measured_defaulted_and_missing_are_three_different_facts(self):
        px = pricing.price_view(self.con, AS_OF, [LONG, SHORT, NONE], self.clamp)
        self.assertEqual(px[LONG]["priced_in_source"], "return_percentile_21s")
        self.assertGreaterEqual(px[LONG]["priced_in_n"], 20)
        self.assertEqual(px[LONG]["priced_in_last_d"], "2026-09-01")
        self.assertEqual(px[SHORT]["priced_in_source"], "neutral_default")
        self.assertEqual(px[SHORT]["priced_in"], 50.0)
        self.assertTrue(px[SHORT]["priced_in_reason"])
        self.assertNotIn(NONE, px, "no bars at all is absence, not a default")
        s = pricing.summarize(px, [LONG, SHORT, NONE])
        self.assertEqual((s["measured"], s["defaulted"], s["missing"]), (1, 1, 1))
        self.assertEqual(s["defaulted_codes"], [SHORT])
        self.assertEqual(s["missing_codes"], [NONE])

    def test_backtest_prices_is_the_same_recipe(self):
        a = backtest._prices(self.con, AS_OF, [LONG, SHORT], self.clamp)
        b = pricing.price_view(self.con, AS_OF, [LONG, SHORT], self.clamp)
        self.assertEqual(a, b)

    def test_build_prices_reads_the_indicators_registered_as_of_the_date(self):
        with mock.patch.object(lexicon, "all_indicators",
                               return_value=[LONG, SHORT, NONE]) as ai:
            prices, summ = pricing.build_prices(self.con, AS_OF)
        ai.assert_called_once_with(AS_OF)
        self.assertEqual(set(prices), {LONG, SHORT})
        self.assertEqual(summ["measured"], 1)
        self.assertEqual(summ["clamp"]["US"], "2026-09-01")


class AllIndicatorsHonoursAsOf(unittest.TestCase):
    def test_a_theme_registered_later_contributes_nothing(self):
        early = lexicon.Theme(id="E", label="e", key_question="q", terms=("a",),
                              price_indicator="US.A", related=("US.B",),
                              registered_d="2026-01-01")
        late = lexicon.Theme(id="L", label="l", key_question="q", terms=("b",),
                             price_indicator="US.Z", registered_d="2026-12-01")
        with mock.patch.object(lexicon, "THEMES", (early, late)):
            self.assertEqual(lexicon.all_indicators("2026-06-01"), ["US.A", "US.B"])
            self.assertEqual(lexicon.all_indicators(), ["US.A", "US.B", "US.Z"])


class HgepKeepsMeasuredAndFilledApart(unittest.TestCase):
    def _themes(self):
        mk = lambda tid, px: lexicon.Theme(id=tid, label=tid, key_question="q",  # noqa: E731
                                           terms=(tid.lower(), "themeword"),
                                           price_indicator=px, registered_d="2026-01-01")
        return [mk("ALPHA", LONG), mk("BETA", SHORT), mk("GAMMA", NONE)]

    def _docs(self):
        return [{"doc_id": f"{t}{i}", "published_d": "2026-09-01", "tier": 1,
                 "title": f"{t.lower()} themeword 周报 {i}", "summary": "看多。"}
                for t in ("ALPHA", "BETA", "GAMMA") for i in range(2)]

    def _run(self, prices):
        ctx = strat.RunContext(as_of=AS_OF, inputs_sha="x", corpus=self._docs(),
                               prices=prices)
        with mock.patch.object(lexicon, "all_themes", return_value=self._themes()):
            return hgep(ctx)

    def test_measured_default_and_absent_indicators(self):
        con = _con(_bars(LONG, 300) + _bars(SHORT, 15))
        prices = pricing.price_view(con, AS_OF, [LONG, SHORT, NONE],
                                    backtest.clamp_dates(AS_OF))
        v = self._run(prices)
        a, b, g = (v.scores[t] for t in ("ALPHA", "BETA", "GAMMA"))
        self.assertTrue(a["P_measured"])
        self.assertEqual(a["p_source"], "return_percentile_21s")
        self.assertEqual(a["p_detail"]["method"], "return_percentile_21s")
        self.assertEqual(a["p_detail"]["indicator"], LONG)
        self.assertEqual(a["p_detail"]["as_of_used"], "2026-09-01")
        self.assertGreaterEqual(a["p_detail"]["n_samples"], 20)
        self.assertEqual(a["p_detail"]["value"], a["P"])
        for row in (b, g):
            self.assertFalse(row["P_measured"])
            self.assertEqual(row["P"], 50.0)
            self.assertEqual(row["p_source"], "neutral_default")
            self.assertIsNone(row["p_detail"]["value"])
            self.assertTrue(row["p_detail"]["reason"])
        self.assertEqual(v.meta["p_measured"], 1)
        self.assertEqual(v.meta["p_defaulted"], 2)
        self.assertIn("2/3", v.meta["ranking_note"])

    def test_a_series_with_bars_but_no_percentile_is_not_reported_as_measured(self):
        # The exact regression: `p_source = "prices" if px else "neutral_default"`
        # called a short series "prices" and its 50 a reading.
        v = self._run({SHORT: {"d": "2026-09-01", "close": 1.0, "priced_in": 50.0,
                               "priced_in_source": "neutral_default",
                               "priced_in_reason": "只有 15 根 K 线"}})
        self.assertFalse(v.scores["BETA"]["P_measured"])
        self.assertEqual(v.scores["BETA"]["p_source"], "neutral_default")

    def test_all_default_p_is_reported_as_a_fill_not_as_discriminating(self):
        v = self._run({})
        self.assertEqual(v.meta["p_measured"], 0)
        self.assertIn("P", v.meta["inert_factors"])
        note = v.meta["ranking_note"]
        self.assertIn("P", note)
        self.assertIn("取值相同", note)
        self.assertIn("缺数默认值", note)
        self.assertIn("全部未测量", note)
        self.assertNotIn("都有区分度", note)
        self.assertEqual(v.meta["factor_dispersion"]["P"]["n_default"], 3)

    def test_the_note_helper_names_the_fill(self):
        note = topic_hgep._ranking_note(["P"], 0.2, ["H", "G", "E"], [],
                                        defaulted={"P": (5, 5)})
        self.assertIn("P 对所有主题取值相同", note)
        self.assertIn("5/5", note)
        self.assertIn("不是读数", note)
        # And the four-argument form still works for the older callers.
        self.assertIn("都有区分度",
                      topic_hgep._ranking_note([], 0.0, list(topic_hgep.WEIGHTS), []))


class WeeklyBuildsPricesWhenNoneAreInjected(unittest.TestCase):
    """`orchestrator.weekly` no longer turns `prices=None` into `{}` silently."""

    def _platform(self, rows):
        st = SqliteStateStore(Path(tempfile.mkdtemp()) / "s.db")
        st.connection.execute(
            "CREATE TABLE prices (code TEXT, d TEXT, open REAL, high REAL, low REAL, "
            "close REAL, volume REAL, src TEXT, PRIMARY KEY (code, d))")
        st.connection.executemany(
            "INSERT INTO prices (code, d, close) VALUES (?, ?, ?)", rows)
        st.connection.commit()
        return mock.Mock(state=st)

    def test_none_builds_from_the_state_store(self):
        p = self._platform(_bars(LONG, 300) + _bars(SHORT, 15))
        with mock.patch.object(lexicon, "all_indicators", return_value=[LONG, SHORT, NONE]):
            prices, step = orchestrator._price_inputs(p, AS_OF, None, False)
        self.assertEqual(set(prices), {LONG, SHORT})
        self.assertEqual(step["source"], "built:prices-table")
        self.assertEqual((step["codes"], step["measured"], step["defaulted"],
                          step["missing"]), (2, 1, 1, 1))
        self.assertEqual(step["last_d"], "2026-09-01")
        self.assertNotIn("error", step)

    def test_an_injected_empty_dict_stays_empty_but_is_journaled(self):
        p = self._platform(_bars(LONG, 300))
        prices, step = orchestrator._price_inputs(p, AS_OF, {}, False)
        self.assertEqual(prices, {})
        self.assertEqual(step["source"], "injected")
        self.assertEqual(step["measured"], 0)

    def test_dry_run_does_not_read_prices(self):
        p = self._platform(_bars(LONG, 300))
        prices, step = orchestrator._price_inputs(p, AS_OF, None, True)
        self.assertEqual(prices, {})
        self.assertIn("dry_run", step["skipped"])

    def test_a_state_store_without_bars_says_so_rather_than_returning_quietly(self):
        st = SqliteStateStore(Path(tempfile.mkdtemp()) / "s.db")   # no prices table
        with mock.patch.object(lexicon, "all_indicators", return_value=[LONG]):
            prices, step = orchestrator._price_inputs(mock.Mock(state=st), AS_OF, None, False)
        self.assertEqual(prices, {})
        self.assertIn("error", step)
        no_con = mock.Mock(state=mock.Mock(spec=["q", "dialect"], dialect="mysql"))
        prices, step = orchestrator._price_inputs(no_con, AS_OF, None, False)
        self.assertEqual(prices, {})
        self.assertIn("mysql", step["error"])

    def test_weekly_routes_prices_through_the_builder_and_journals_it(self):
        src = inspect.getsource(orchestrator.weekly)
        self.assertIn("_price_inputs(p, as_of, prices, dry_run)", src)
        self.assertIn('j.step("prices"', src)
        self.assertNotIn("prices = prices or {}", src,
                         "the silent None→{} line must be gone")


if __name__ == "__main__":
    unittest.main()
