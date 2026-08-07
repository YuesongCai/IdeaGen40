"""Unit tests for the rules that keep the study honest.

Run with:  python3 -m unittest discover -s tests -v

These cover the invariants that, if broken, would silently flatter the result:
scenario arithmetic, cost deduction, the session-completeness guard, no same-bar
look-ahead, limit-fill pricing, marking idempotency, and the validation gate.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ideagen import analytics, config, db, ideas, lexicon, paper, scoring
from ideagen.sources import futu_px, wisburg


def mem():
    return db.init(":memory:")


class TestOdds(unittest.TestCase):
    def test_matches_v03_worksheet_arithmetic(self):
        """v0.3 §5 must reproduce the published worksheet exactly."""
        # 底稿 §2 row 02 (KRE): 35/40/25, +10/+3/-7, hurdle 0.31 -> 2.44
        # The worksheet prints intermediates to 3 decimals (4.467 for a true
        # 4.4675), so compare against the unrounded value and check the printed
        # ratio at the precision the page actually displays.
        o = ideas.odds([35, 40, 25], [10.0, 3.0, -7.0], 0.31)
        self.assertAlmostEqual(o["gain"], 4.4675, places=4)
        self.assertAlmostEqual(o["loss"], 1.8275, places=4)
        self.assertAlmostEqual(o["or"], 2.44, places=2)
        # row 03 (XLE): 25/45/30, +7/+1.5/-9 -> 0.79
        o = ideas.odds([25, 45, 30], [7.0, 1.5, -9.0], 0.31)
        self.assertAlmostEqual(o["or"], 0.79, places=2)

    def test_expected_return_is_probability_weighted(self):
        o = ideas.odds([50, 30, 20], [10.0, 0.0, -10.0], 0.0)
        self.assertAlmostEqual(o["ev"], 0.5 * 10 + 0.3 * 0 + 0.2 * -10, places=6)

    def test_zero_loss_is_infinite_not_crash(self):
        o = ideas.odds([50, 50, 0], [10.0, 5.0, 1.0], 0.5)
        self.assertTrue(o["or_inf"])
        self.assertIsNone(o["or"])

    def test_costs_reduce_every_leg(self):
        gross = [10.0, 2.0, -6.0]
        net = ideas.net_scenarios(gross, "US", "listed")
        cost = ideas.round_trip_cost_pct("US", "listed")
        self.assertAlmostEqual(cost, 0.08, places=4)
        for g, n in zip(gross, net):
            self.assertAlmostEqual(n, g - cost, places=6)

    def test_costs_make_a_thin_edge_unattractive(self):
        thin = [0.10, 0.02, -0.05]
        h = 0.03
        before = ideas.odds([40, 40, 20], thin, h)["or"]
        after = ideas.odds([40, 40, 20],
                           ideas.net_scenarios(thin, "US", "listed"), h)["or"]
        self.assertGreater(before, after)


class TestGrading(unittest.TestCase):
    def test_absolute_grades_follow_v03(self):
        self.assertEqual(ideas.grade_absolute(3.0, 1.6)[0], "S")
        self.assertEqual(ideas.grade_absolute(3.0, 1.2)[0], "A")
        self.assertEqual(ideas.grade_absolute(1.4, 0.6)[0], "B")
        self.assertEqual(ideas.grade_absolute(0.7, 0.2)[0], "C")

    def test_relative_grade_is_hurdle_invariant(self):
        """Scaling every OR must not move the cross-sectional quartiles."""
        base = [{"or_k": v} for v in (0.1, 0.4, 0.8, 1.2, 2.0, 3.5, 5.0, 9.0)]
        scaled = [{"or_k": r["or_k"] * 7.3} for r in base]
        ideas.grade_batch(base)
        ideas.grade_batch(scaled)
        self.assertEqual([r["grade_rel"] for r in base],
                         [r["grade_rel"] for r in scaled])


class TestVolSanity(unittest.TestCase):
    def test_flags_fantasy_upside(self):
        v, meta = ideas.vol_sanity(30.0, -5.0, 4.0)   # +30% on a 4% monthly sigma
        self.assertIn("wide", v)
        self.assertGreater(meta["k_up"], 2.6)

    def test_flags_no_opinion(self):
        v, _ = ideas.vol_sanity(0.5, -0.4, 6.0)
        self.assertIn("narrow", v)

    def test_accepts_a_reasonable_scenario(self):
        v, _ = ideas.vol_sanity(8.0, -6.0, 6.0)
        self.assertEqual(v, "ok")

    def test_na_without_vol_history(self):
        self.assertEqual(ideas.vol_sanity(8.0, -6.0, None)[0], "na")


class TestSessionGuard(unittest.TestCase):
    ET = ZoneInfo("America/New_York")

    def test_live_bar_is_not_complete(self):
        mid = datetime(2026, 8, 6, 12, 30, tzinfo=self.ET)
        self.assertEqual(futu_px.complete_through("US", now=mid), "2026-08-05")

    def test_bar_is_complete_after_the_close(self):
        after = datetime(2026, 8, 6, 16, 30, tzinfo=self.ET)
        self.assertEqual(futu_px.complete_through("US", now=after), "2026-08-06")

    def test_hk_uses_its_own_close(self):
        hk = ZoneInfo("Asia/Hong_Kong")
        self.assertEqual(
            futu_px.complete_through("HK", now=datetime(2026, 8, 6, 17, 0, tzinfo=hk)),
            "2026-08-06")
        self.assertEqual(
            futu_px.complete_through("HK", now=datetime(2026, 8, 6, 12, 0, tzinfo=hk)),
            "2026-08-05")


class TestFills(unittest.TestCase):
    def setUp(self):
        self.con = mem()
        bars = [
            # d, open, high, low, close
            ("2026-08-03", 100.0, 101.0, 99.0, 100.5),
            ("2026-08-04", 100.5, 102.0, 100.0, 101.5),
            ("2026-08-05", 98.0, 99.0, 94.0, 95.0),      # gaps down through a band
            ("2026-08-06", 95.5, 106.0, 95.0, 105.0),    # breaks out
        ]
        db.upsert_many(self.con, "prices", [
            {"code": "US.TEST", "d": d, "open": o, "high": h, "low": lo,
             "close": c, "volume": 1e6, "src": "test"}
            for d, o, h, lo, c in bars], ["code", "d"])
        db.upsert_many(self.con, "prices", [
            {"code": "US.SPY", "d": d, "open": o, "high": h, "low": lo,
             "close": c, "volume": 1e6, "src": "test"}
            for d, o, h, lo, c in bars], ["code", "d"])
        self.idea = {"instrument": "listed", "futu_code": "US.TEST",
                     "olive_key": None, "tool": "TEST"}

    def test_limit_fills_at_band_edge_not_at_the_low(self):
        """A quiet drift into the band must not fill at the day's low."""
        order = {"kind": "band", "code": "US.TEST", "band_lo": 100.0,
                 "band_hi": 100.6, "trigger": None}
        res = paper._try_fill(self.con, order, "2026-08-04", self.idea)
        self.assertIsNotNone(res)
        # open 100.5 is inside the band -> fill at the open, never at low 100.0
        self.assertAlmostEqual(res["px"], 100.5, places=4)

    def test_gap_through_the_band_fills_at_the_open(self):
        order = {"kind": "band", "code": "US.TEST", "band_lo": 99.0,
                 "band_hi": 99.5, "trigger": None}
        res = paper._try_fill(self.con, order, "2026-08-05", self.idea)
        self.assertAlmostEqual(res["px"], 98.0, places=4)   # the gap price

    def test_band_not_touched_does_not_fill(self):
        order = {"kind": "band", "code": "US.TEST", "band_lo": 90.0,
                 "band_hi": 93.0, "trigger": None}
        self.assertIsNone(
            paper._try_fill(self.con, order, "2026-08-04", self.idea))

    def test_breakout_arms_and_does_not_fill_on_the_trigger_bar(self):
        order = {"kind": "breakout", "code": "US.TEST", "band_lo": None,
                 "band_hi": None, "trigger": 104.0}
        res = paper._try_fill(self.con, order, "2026-08-06", self.idea)
        self.assertTrue(res.get("arm"))
        self.assertIsNone(res.get("px"))


class TestNoLookAhead(unittest.TestCase):
    def setUp(self):
        self.con = mem()
        db.upsert_many(self.con, "prices", [
            {"code": "US.SPY", "d": d, "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1, "src": "t"}
            for d in ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06")],
            ["code", "d"])

    def _batch(self, generated_at: str):
        db.upsert(self.con, "batches", {
            "batch_id": "B", "as_of": "2026-08-06", "generated_at": generated_at,
            "generator": "t", "methodology": "0.4", "n_ideas": 1,
            "validation": {"pass": True}, "status": "validated"}, ["batch_id"])

    def test_batch_written_during_the_session_may_use_that_close(self):
        # 2026-08-06 12:54 ET -> the 08-06 session is still open, so its close
        # is the first fillable bar.
        self._batch("2026-08-07T00:54:00+08:00")
        self.assertEqual(paper.first_fillable(self.con, "B", "US"), "2026-08-06")

    def test_batch_written_after_the_close_must_wait_for_the_next_session(self):
        # 2026-08-06 20:00 ET -> 08-06 already closed; must wait.
        self._batch("2026-08-07T08:00:00+08:00")
        self.assertGreater(paper.first_fillable(self.con, "B", "US"), "2026-08-06")


class TestValidation(unittest.TestCase):
    def _idea(self, i: int, **kw):
        base = {
            "id": i, "instrument_key": "SPY", "tool": "SPY", "theme": "T",
            "theme_id": "AI-CAPEX", "signal_id": "S1", "horizon": "1个月",
            "central": {"p": [30, 50, 20], "r": [6.0, 1.0, -5.0]},
            "conservative": {"p": [25, 50, 25], "r": [4.0, 0.5, -7.0]},
            "ref_price": 100.0, "ref_price_d": "2026-08-05",
            "entry_lo": 98.0, "entry_hi": 100.0, "stop_px": 94.0,
            "entry_src": "formula", "take_src": "formula", "stop_src": "formula",
            "pos_init": 1.0, "pos_max": 2.0,
        }
        base.update(kw)
        return base

    def setUp(self):
        self.con = mem()

    def _validate(self, rows_in):
        from ideagen import universe
        universe.sync_registry(self.con)
        rows = [ideas.compute(self.con, r, date(2026, 8, 6), "B") for r in rows_in]
        ideas.grade_batch(rows)
        return ideas.validate_batch(self.con, rows, date(2026, 8, 6))

    def test_wrong_count_fails(self):
        rep = self._validate([self._idea(1)])
        self.assertFalse(rep["pass"])
        self.assertIn("idea_count_40",
                      [c["check"] for c in rep["checks"] if not c["ok"]])

    def test_probabilities_must_sum_to_100(self):
        bad = self._idea(1, central={"p": [30, 50, 25], "r": [6.0, 1.0, -5.0]})
        rep = self._validate([bad])
        failed = [c["check"] for c in rep["checks"] if not c["ok"]]
        self.assertIn("central_probs_sum_100", failed)

    def test_scenarios_must_be_monotonic(self):
        bad = self._idea(1, central={"p": [30, 50, 20], "r": [1.0, 6.0, -5.0]})
        rep = self._validate([bad])
        self.assertIn("scenario_monotonic",
                      [c["check"] for c in rep["checks"] if not c["ok"]])

    def test_future_reference_price_is_rejected(self):
        bad = self._idea(1, ref_price_d="2026-08-20")
        rep = self._validate([bad])
        self.assertIn("ref_price_not_future",
                      [c["check"] for c in rep["checks"] if not c["ok"]])

    def test_independent_recompute_agrees(self):
        rep = self._validate([self._idea(i) for i in range(1, 3)])
        chk = next(c for c in rep["checks"]
                   if c["check"] == "formula_recompute_within_0.01pp")
        self.assertTrue(chk["ok"])


class TestScoring(unittest.TestCase):
    def test_tis_uses_v03_weights(self):
        tis, meta = scoring.tactical_impact(100, 100, 100, 100)
        self.assertAlmostEqual(tis, 100.0, places=6)
        tis, _ = scoring.tactical_impact(0, 0, 0, 100)
        self.assertAlmostEqual(tis, 35.0, places=6)
        tis, _ = scoring.tactical_impact(100, 0, 0, 0)
        self.assertAlmostEqual(tis, 15.0, places=6)

    def test_missing_factor_renormalises_and_is_reported(self):
        tis, meta = scoring.tactical_impact(None, 100, 100, 100)
        self.assertEqual(meta["missing"], ["D"])
        self.assertAlmostEqual(tis, 100.0, places=6)   # 0.85 weight, all 100
        self.assertAlmostEqual(meta["weight_sum"], 0.85, places=6)

    def test_theme_tiers_follow_v03_thresholds(self):
        self.assertEqual(scoring.theme_tier(80), "core")
        self.assertEqual(scoring.theme_tier(75), "core")
        self.assertEqual(scoring.theme_tier(60), "important")
        self.assertEqual(scoring.theme_tier(45), "watch")
        self.assertEqual(scoring.theme_tier(44.9), "background")

    def test_validation_stages(self):
        self.assertEqual(scoring.validation_stage(10), "尚未定价")
        self.assertEqual(scoring.validation_stage(45), "早期验证")
        self.assertEqual(scoring.validation_stage(70), "已有确认")
        self.assertEqual(scoring.validation_stage(90), "交易成熟")

    def test_crowding_labels(self):
        self.assertEqual(scoring.crowding_label(20), "不拥挤")
        self.assertEqual(scoring.crowding_label(70), "偏拥挤")
        self.assertEqual(scoring.crowding_label(90), "高度拥挤")


class TestLexicon(unittest.TestCase):
    def test_theme_ids_unique_and_indicators_registered(self):
        ids = [t.id for t in lexicon.THEMES]
        self.assertEqual(len(ids), len(set(ids)))
        for t in lexicon.THEMES:
            self.assertTrue(t.price_indicator.startswith(("US.", "HK.")))
            self.assertTrue(t.key_question)
            self.assertLessEqual(len(t.related), 3)

    def test_institution_extraction(self):
        self.assertEqual(lexicon.institution_of("野村研报指出，AI SuperPod…"), "Nomura")
        self.assertEqual(lexicon.institution_of("Citi raised its target"), "Citi")
        self.assertIsNone(lexicon.institution_of("某机构认为通胀会回落"))

    def test_stance_coding(self):
        self.assertEqual(lexicon.stance_of("订单增加，需求强劲，上调评级"), 1)
        self.assertEqual(lexicon.stance_of("需求疲弱，下修指引，承压"), -1)
        self.assertEqual(lexicon.stance_of("方向不明"), 0)

    def test_heavy_hedging_suppresses_a_weak_signal(self):
        self.assertEqual(
            lexicon.stance_of("可能改善，但仍不确定，取决于政策，然而分歧较大"), 0)

    def test_causal_depth_ranks_realised_over_narrative(self):
        self.assertEqual(lexicon.depth_of("归母净利同比增长，自由现金流转正"), 100)
        self.assertEqual(lexicon.depth_of("新订单金额创高，backlog 增加"), 75)
        self.assertEqual(lexicon.depth_of("收益率上行，利差走阔"), 50)
        self.assertEqual(lexicon.depth_of("管理层表示将考虑"), 25)

    def test_title_signature_collapses_syndication(self):
        a = lexicon.title_signature("美联储：通胀仍需观察，降息门槛未达到")
        b = lexicon.title_signature("美联储，通胀仍需观察——降息门槛未达到！")
        self.assertEqual(a, b)


class TestWisburgParsing(unittest.TestCase):
    def test_sse_with_raw_newlines_inside_strings(self):
        """The real server emits bare newlines inside JSON string values."""
        raw = 'event: message\ndata: {"result":{"text":"line1\nline2"},"id":1}\n\n'
        obj = wisburg._parse_sse(raw)
        self.assertEqual(obj["result"]["text"], "line1\nline2")

    def test_text_page_parsing(self):
        text = ("Found 2 reports:\n\n"
                "[99610] 全球AI趋势追踪\n  date: 2026-08-06T23:36:29+08:00\n"
                "  野村研报指出，SuperPod 规模向 1024 卡演进。\n\n"
                "[99609] 外汇基金特别账户\n  date: 2026-08-06T23:35:59+08:00\n\n"
                "--- Page Info ---\nNext cursor: 2\n")
        nodes, cursor, has_next = wisburg._extract_page(text)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["id"], 99610)
        self.assertEqual(nodes[0]["title"], "全球AI趋势追踪")
        self.assertIn("SuperPod", nodes[0]["summary"])
        self.assertEqual(cursor, "2")
        self.assertTrue(has_next)

    def test_detail_sections(self):
        md = ("# 标题\n\n- ID: 123\n- Date: 2026-08-06T00:00:00+08:00\n\n"
              "## Summary\n\n### 主要观点\n1. **甲**\n2. **乙**\n\n"
              "### 事实依据\n1. 一\n2. 二\n3. 三\n")
        sec = wisburg.parse_detail(md)
        self.assertEqual(sec["source_id"], 123)
        self.assertEqual(sec["n_views"], 2)
        self.assertEqual(sec["n_facts"], 3)

    def test_timestamp_normalisation_to_hkt(self):
        self.assertEqual(wisburg._to_hkt_date("2026-08-06T23:36:29+08:00"),
                         "2026-08-06")
        # 16:00 UTC == 2026-08-07 00:00 HKT
        self.assertEqual(wisburg._to_hkt_date(wisburg._norm_ts("2026-08-06T17:00:00Z")),
                         "2026-08-07")


class TestAnalytics(unittest.TestCase):
    def test_spearman_detects_perfect_ordering(self):
        self.assertAlmostEqual(analytics.spearman([1, 2, 3, 4], [10, 20, 30, 40]),
                               1.0, places=6)
        self.assertAlmostEqual(analytics.spearman([1, 2, 3, 4], [40, 30, 20, 10]),
                               -1.0, places=6)

    def test_spearman_needs_enough_pairs(self):
        self.assertIsNone(analytics.spearman([1, 2], [3, 4]))

    def test_scenario_bucket_uses_midpoints_of_the_forecast(self):
        r = [10.0, 2.0, -6.0]          # cuts at +6% and -2%
        self.assertEqual(analytics._scenario_bucket(0.08, r), "up")
        self.assertEqual(analytics._scenario_bucket(0.01, r), "base")
        self.assertEqual(analytics._scenario_bucket(-0.05, r), "down")

    def test_brier_rewards_a_confident_correct_call(self):
        confident = analytics._brier([80, 15, 5], "up")
        uniform = analytics._brier([34, 33, 33], "up")
        self.assertLess(confident, uniform)

    def test_brier_punishes_a_confident_wrong_call(self):
        self.assertGreater(analytics._brier([80, 15, 5], "down"),
                           analytics._brier([34, 33, 33], "down"))


class TestHurdle(unittest.TestCase):
    def test_scales_with_horizon(self):
        con = mem()
        h1, m1 = ideas.hurdle_for(con, "ETF", 1)
        h6, m6 = ideas.hurdle_for(con, "ETF", 6)
        self.assertAlmostEqual(h6, h1 * 6, places=4)

    def test_illiquid_vehicles_face_a_higher_hurdle(self):
        con = mem()
        etf, _ = ideas.hurdle_for(con, "ETF", 6)
        pe, _ = ideas.hurdle_for(con, "私募", 6)
        self.assertGreater(pe, etf)


class TestHorizonEnd(unittest.TestCase):
    def test_one_and_six_months(self):
        self.assertEqual(ideas.horizon_end(date(2026, 7, 27), 1), date(2026, 8, 27))
        self.assertEqual(ideas.horizon_end(date(2026, 7, 27), 6), date(2027, 1, 27))

    def test_month_end_rolls_back_not_over(self):
        self.assertEqual(ideas.horizon_end(date(2026, 1, 31), 1), date(2026, 2, 28))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPayload(unittest.TestCase):
    """The dashboard payload must stay renderable for every stored date."""

    @classmethod
    def setUpClass(cls):
        from ideagen import payload
        cls.con = db.init()
        cls.pl = payload.build(cls.con)

    def test_every_date_has_a_day_entry(self):
        for d in self.pl["meta"]["dates"]:
            self.assertIn(d, self.pl["days"])

    def test_a_day_may_lack_a_report_or_a_batch_without_breaking(self):
        for d, v in self.pl["days"].items():
            self.assertTrue(v["report"] is None or "themes" in v["report"], d)
            self.assertTrue(v["batch"] is None or "ideas" in v["batch"], d)

    def test_frozen_scoring_wins_when_a_batch_exists(self):
        for d, v in self.pl["days"].items():
            if v["batch"] and v["batch"].get("themes_at_generation"):
                self.assertTrue(v["report"]["frozen_at_generation"], d)

    def test_max_drawdown_is_never_positive(self):
        for d, v in self.pl["days"].items():
            for bk, b in v["books"].items():
                if b.get("max_dd") is not None:
                    self.assertLessEqual(b["max_dd"], 0, f"{d}/{bk}")

    def test_evidence_carries_no_body_excerpt(self):
        """The page is public; only titles and metadata may travel with it."""
        for d, v in self.pl["days"].items():
            for e in (v["report"] or {}).get("evidence", []):
                self.assertNotIn("excerpt", e, d)
                self.assertLessEqual(len(e.get("title") or ""), 220, d)

    def test_positions_include_unfilled_orders(self):
        kinds = {p["kind"] for p in self.pl["positions"]}
        self.assertIn("order", kinds)


class TestScoringGuard(unittest.TestCase):
    def test_refuses_to_rescore_a_traded_date(self):
        con = mem()
        as_of = date(2026, 8, 6)
        db.upsert(con, "batches", {
            "batch_id": "B", "as_of": as_of.isoformat(),
            "generated_at": "2026-08-06T10:00:00+08:00", "generator": "t",
            "methodology": "0.4", "n_ideas": 40, "status": "traded"}, ["batch_id"])
        db.upsert(con, "themes", {
            "as_of": as_of.isoformat(), "theme_id": "AI-CAPEX", "label": "x",
            "tis": 50.0}, ["as_of", "theme_id"])
        res = scoring.score_day(con, as_of, verbose=False)
        self.assertTrue(res.get("skipped"))
        # ...and proceeds when forced
        res = scoring.score_day(con, as_of, verbose=False, force=True)
        self.assertFalse(res.get("skipped"))


class TestDashboardRender(unittest.TestCase):
    def test_public_build_makes_no_external_requests(self):
        """The published page must be request-free.

        The local build deliberately hotlinks Wisburg chart images — viewing them
        is the point. The public GitHub Pages build must not, because that would
        republish a subscription service's charts at an indexable URL. This asserts
        the split rather than the old blanket rule.
        """
        import tempfile

        from ideagen import report

        con = db.init()
        with tempfile.TemporaryDirectory() as td:
            pub = report.build(con, Path(td) / "pub.html", embed_images=False)
            s = pub.read_text(encoding="utf-8")
        body = s.replace("http://www.w3.org/2000/svg", "")
        self.assertGreater(len(s), 50_000)
        for bad in ("<img", "@import", "cdn.", "<script src", "<link "):
            self.assertNotIn(bad, body)
        for host in ("rocks.wisburg.com/", "doctext.wisburg.com/"):
            self.assertNotIn(f'src="https://{host}', body)
        self.assertIn("window.__IG40__", s)
        self.assertIn('data-theme="dark"', s)
        self.assertIn("prefers-color-scheme", s)

    def test_local_build_embeds_charts_with_attribution(self):
        import tempfile

        from ideagen import report

        con = db.init()
        with tempfile.TemporaryDirectory() as td:
            loc = report.build(con, Path(td) / "loc.html", embed_images=True)
            s = loc.read_text(encoding="utf-8")
        self.assertIn('"embed_images":true', s.replace(" ", ""))

    def test_artifact_mode_omits_the_document_wrapper(self):
        import tempfile

        from ideagen import report

        con = db.init()
        with tempfile.TemporaryDirectory() as td:
            out = report.build(con, Path(td) / "a.html", artifact=True)
            s = out.read_text(encoding="utf-8")
        for tag in ("<!doctype", "<html", "<head>", "<body>"):
            self.assertNotIn(tag, s.lower())
        self.assertTrue(s.startswith("<title>"))


class TestCohortMarking(unittest.TestCase):
    """Every past day's cohort must track today's prices, not its own entry day."""

    @classmethod
    def setUpClass(cls):
        cls.con = db.init()
        cls.last = futu_px.complete_through("US")

    def test_every_cohort_is_marked_to_the_last_closed_session(self):
        for bk in paper.cohort_books(self.con):
            n = db.q1(self.con, "SELECT COUNT(*) n FROM positions WHERE book_id=?",
                      (bk,))["n"]
            if not n:
                continue          # orders placed, session not closed yet
            mx = db.q1(self.con, "SELECT MAX(d) d FROM mtm WHERE book_id=?", (bk,))["d"]
            self.assertEqual(mx, self.last, bk)

    def test_a_cohort_holds_only_its_own_day(self):
        for bk in paper.cohort_books(self.con):
            as_of = {r["as_of"] for r in db.q(
                self.con, "SELECT DISTINCT i.as_of FROM positions p "
                          "JOIN ideas i ON i.idea_uid=p.idea_uid WHERE p.book_id=?",
                (bk,))}
            self.assertLessEqual(len(as_of), 1, f"{bk} mixes vintages: {as_of}")

    def test_mark_covers_cohorts(self):
        import inspect

        from ideagen import cli

        self.assertIn("all_books", inspect.getsource(cli.cmd_mark))

    def test_horizon_exit_dates_are_in_the_future_or_closed(self):
        for r in db.q(self.con, "SELECT status, closed_d, horizon_end, exit_reason "
                                "FROM positions WHERE horizon_end IS NOT NULL"):
            if r["status"] == "closed" and r["exit_reason"] == "horizon":
                self.assertGreaterEqual(r["closed_d"], r["horizon_end"])
            elif r["status"] == "open":
                self.assertGreater(r["horizon_end"], self.last)
