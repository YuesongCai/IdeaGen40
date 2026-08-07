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
from ideagen import themes as themes_mod
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


class TestFrontEndInvariants(unittest.TestCase):
    """Guard the CSS/JS contract.

    Twice during development a string-replace patch silently missed its anchor and
    dropped a rule instead of adding one — the tooltip ended up clipped inside a
    scroll container, and long prose stretched rows to 240px. These assertions make
    that class of failure loud.
    """

    @classmethod
    def setUpClass(cls):
        from ideagen import report
        cls.css = report.CSS
        cls.js = report.JS
        cls.gloss = report.GLOSSARY

    # ---- tooltip must escape every clipping context ----
    def test_tooltip_is_a_body_level_fixed_layer(self):
        self.assertIn("#tip{", self.css)
        self.assertIn("position:fixed", self.css.split("#tip{")[1][:200])
        self.assertIn("document.body.append(TIP)", self.js)

    def test_tooltip_is_not_a_child_of_the_hint(self):
        """An absolutely-positioned child of .hint gets clipped by .tw's overflow."""
        block = self.css.split(".hint{")[1].split("}")[0]
        self.assertNotIn("position:relative", block)
        self.assertNotIn(".hint>span{", self.css)

    def test_tooltip_is_clamped_to_the_viewport(self):
        self.assertIn("window.innerWidth - b.width", self.js)
        self.assertIn("r.bottom + 9", self.js)      # flips below when no room above

    def test_hint_click_does_not_sort_its_column(self):
        self.assertIn("e.stopPropagation()", self.js)

    # ---- table layout ----
    def test_wide_tables_scroll_instead_of_squeezing(self):
        self.assertIn("min-width:max-content", self.css)
        self.assertIn("overflow-x:auto", self.css)

    def test_cells_do_not_wrap_unless_they_opt_in(self):
        body = self.css.split("tbody td{")[1].split("}")[0]
        self.assertIn("white-space:nowrap", body)
        for rule in ("td.wrap{", "td.prose{"):
            self.assertIn(rule, self.css)
        prose = self.css.split("td.prose{")[1].split("}")[0]
        self.assertIn("white-space:normal", prose)
        self.assertIn("min-width", prose)

    def test_long_prose_is_clamped_and_kept_in_the_title(self):
        self.assertIn("-webkit-line-clamp:3", self.css)
        self.assertIn("title: cell && cell.title", self.js)

    def test_headers_do_not_create_a_stacking_context_inside_a_scroller(self):
        head = self.css.split("thead th{")[1].split("}")[0]
        self.assertNotIn("position:sticky", head)

    # ---- sorting ----
    def test_blank_cells_sort_last_in_both_directions(self):
        self.assertIn("xb ? 1 : -1", self.js)

    # ---- themes / a11y ----
    def test_both_themes_are_defined_for_every_token_block(self):
        for sel in ('@media (prefers-color-scheme: dark)',
                    ':root[data-theme="dark"]', ':root[data-theme="light"]'):
            self.assertIn(sel, self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_hints_are_keyboard_reachable(self):
        self.assertIn("tabindex: '0'", self.js)

    # ---- glossary ----
    def test_every_hinted_term_exists_in_the_glossary(self):
        import re

        used = set(re.findall(r"hint\('([^']+)'\)", self.js))
        used |= {m[1] or m[0] for m in re.findall(r"lbl\('([^']+)'(?:,\s*'([^']+)')?\)", self.js)}
        missing = {t for t in used if t not in self.gloss}
        self.assertFalse(missing, f"hinted but undefined: {missing}")

    def test_glossary_covers_the_factor_letters(self):
        for k in ("TIS", "D", "A", "B", "N", "M", "C", "hurdle",
                  "中心赔率", "保守赔率", "技能分", "排序能力"):
            self.assertIn(k, self.gloss)
            self.assertGreater(len(self.gloss[k]), 20, k)

    # ---- three views ----
    def test_all_three_views_route(self):
        for v in ("cockpit", "report", "book"):
            self.assertIn(f"'{v}'", self.js)
        self.assertIn("viewCockpit", self.js)
        self.assertIn("#(cockpit|report|book)", self.js)


class TestInformationArchitecture(unittest.TestCase):
    """The page is three views with drill-down, not one long scroll.

    These lock the structure the redesign established: sources sit under the thing
    that cites them, classification lives in the row that expresses it, and the
    cross-day view does not pretend to have a selected date.
    """

    @classmethod
    def setUpClass(cls):
        from ideagen import payload, report
        cls.js = report.JS
        cls.css = report.CSS
        cls.con = db.init()
        cls.pl = payload.build(cls.con)

    # ---- three views, clear division of labour ----
    def test_cross_day_view_has_no_date_picker(self):
        """A date control on the cockpit would imply a selection that changes nothing."""
        self.assertIn("const crossDay = view === 'cockpit'", self.js)
        self.assertIn("$('#datenav').hidden = crossDay", self.js)

    def test_cross_day_analytics_live_in_the_cockpit_not_under_one_day(self):
        cockpit = self.js[self.js.index("function viewCockpit"):self.js.index("function barChart")]
        book = self.js[self.js.index("function viewBook"):self.js.index("function orderTable")]
        self.assertIn("skillBlocks()", cockpit)
        self.assertNotIn("skillBlocks", book)

    # ---- drill-down replaces stacked sections ----
    def test_expandable_tables_are_used_for_themes_ideas_and_positions(self):
        self.assertIn("function expTable(", self.js)
        for fn in ("themeDetail", "ideaDetail", "posDetail"):
            self.assertIn(fn, self.js)
        self.assertGreaterEqual(self.js.count("expTable(cols, rows"), 3)

    def test_the_separate_theme_map_is_gone(self):
        """Its information now lives in the ideas table's classification columns."""
        self.assertNotIn("themeMap", self.js)
        self.assertNotIn("function ideaCard", self.js)

    def test_ideas_table_classifies_each_row(self):
        block = self.js[self.js.index("function ideaTable("):self.js.index("function ideaDetail(")]
        for col in ("宏观主题", "传导主线", "资产信号"):
            self.assertIn(col, block)

    def test_detail_panel_stays_inside_the_viewport(self):
        """It spans every column, so inside a scrolling table it must be pinned."""
        block = self.css.split("tr.detail>td{")[1].split("}")[0]
        self.assertIn("position:sticky", block)
        self.assertIn("left:0", block)
        dw = self.css.split(".dw{")[1].split("}")[0]
        self.assertIn("100vw", dw)

    def test_shrinkable_grid_columns_cannot_push_the_panel_wide(self):
        for sel in (".src>div{", ".chain>div{"):
            block = self.css.split(sel)[1].split("}")[0]
            self.assertIn("minmax(0,1fr)", block.replace(" ", ""))

    # ---- sources sit where they are cited ----
    def test_every_theme_carries_its_own_evidence_and_reasoning_trail(self):
        for d, day in self.pl["days"].items():
            rep = day.get("report")
            if not rep:
                continue
            for t in rep["themes"]:
                self.assertIn("trail", t, d)
                self.assertEqual(set(t["trail"]), {"D", "A", "B", "N", "M", "C"}, d)
                for k, v in t["trail"].items():
                    self.assertTrue(v["why"], f"{d}/{t['id']}/{k}")
                self.assertIsInstance(t["evidence"], list)
                self.assertIsInstance(t["charts"], list)

    def test_theme_evidence_carries_a_reproducible_receipt(self):
        seen = 0
        for day in self.pl["days"].values():
            for t in (day.get("report") or {}).get("themes", []):
                for e in t["evidence"]:
                    if e.get("retrieval"):
                        seen += 1
                        self.assertTrue(e["hash"])
        self.assertGreater(seen, 50)

    def test_ideas_resolve_their_citations_to_readable_references(self):
        checked = 0
        for day in self.pl["days"].values():
            for i in (day.get("batch") or {}).get("ideas", []):
                for s in i.get("sources_resolved", []):
                    checked += 1
                    self.assertIn("resolved", s)
                    if s["resolved"]:
                        self.assertTrue(s["title"])
                        self.assertTrue(s["retrieval"])
        self.assertGreater(checked, 100)

    def test_weak_chart_matches_are_labelled(self):
        for day in self.pl["days"].values():
            for t in (day.get("report") or {}).get("themes", []):
                for c in t["charts"]:
                    self.assertIn("weak", c)
                    if not c["weak"]:
                        self.assertGreaterEqual(c["match_terms"], 2)

    def test_no_raw_float_leaks_into_the_reasoning_trail(self):
        import re
        for day in self.pl["days"].values():
            for t in (day.get("report") or {}).get("themes", []):
                for k, v in t["trail"].items():
                    self.assertIsNone(re.search(r"\d\.\d{6,}", v["why"]),
                                      f"{t['id']}/{k}: {v['why'][:80]}")

    def test_no_two_letter_globals_shadowing_view_params(self):
        """A top-level `dd` formatter collided with viewBook(dd) and broke the view.

        A local `const dd = day[cur]` inside render() is fine — the rule is about
        module-level names, which is where the collision came from.
        """
        import re

        top_level = [ln for ln in self.js.splitlines()
                     if re.match(r"^(const|let|var)\s+dd\b", ln)]
        self.assertFalse(top_level, f"top-level dd declared: {top_level}")
        self.assertIn("const ddPct = ", self.js)


class TestThemeDiscovery(unittest.TestCase):
    """The theme set is discovered, and discovery must not become hindsight.

    A fixed 16-theme dictionary matched only 54% of the corpus, so the set has
    to grow. Every test here guards the one property that makes growth
    legitimate: a theme registered today may not score a day it did not exist
    for. Without it, "discovery" is just picking the theme around whatever
    already moved.
    """

    def test_as_of_excludes_themes_registered_later(self):
        seeds = lexicon.all_themes(date.fromisoformat(lexicon.SEED_REGISTERED_D))
        self.assertTrue(seeds, "seed themes must be scoreable on their own day")
        day_before = (date.fromisoformat(lexicon.SEED_REGISTERED_D)
                      - timedelta(days=1))
        self.assertEqual(lexicon.all_themes(day_before), (),
                         "no theme may score a day before it was registered")

    def test_every_registered_theme_declares_a_registration_date(self):
        for t in lexicon.THEMES:
            self.assertTrue(t.registered_d, f"{t.id} has no registered_d")
            date.fromisoformat(t.registered_d)          # must parse
            self.assertIn(t.origin, ("seed", "discovered"))

    def test_discovered_themes_are_not_backdated_before_the_seed(self):
        for t in lexicon.THEMES:
            if t.origin == "discovered":
                self.assertGreater(t.registered_d, lexicon.SEED_REGISTERED_D,
                                   f"{t.id} claims to predate the seed set")

    def test_registration_cannot_be_backdated(self):
        con = mem()
        row = {"id": "TEST-BACKDATE", "label": "x",
               "key_question": "未来1–6个月，x 能否 y", "terms": ["aaa", "bbb", "ccc", "ddd"],
               "price_indicator": "US.SPY", "registered_d": "2026-01-01"}
        with self.assertRaises(themes_mod.RegistrationError) as ctx:
            themes_mod.validate(con, row, date(2026, 8, 8))
        self.assertIn("backdated", str(ctx.exception))

    def test_registration_rejects_unpriceable_indicators(self):
        con = mem()
        row = {"id": "TEST-UNPRICEABLE", "label": "x",
               "key_question": "未来1–6个月，x 能否 y",
               "terms": ["aaa", "bbb", "ccc", "ddd"],
               "price_indicator": "US.NOPE"}
        with self.assertRaises(themes_mod.RegistrationError) as ctx:
            themes_mod.validate(con, row, date(2026, 8, 8))
        self.assertIn("unpriceable", str(ctx.exception))

    def test_registration_rejects_synonyms_owned_by_another_theme(self):
        """Shared synonyms would count one document twice in D."""
        con = mem()
        stolen = lexicon.THEME_BY_ID["AI-CAPEX"].terms[0]
        row = {"id": "TEST-STEAL", "label": "x",
               "key_question": "未来1–6个月，x 能否 y",
               "terms": [stolen, "bbb", "ccc", "ddd"],
               "price_indicator": "US.SPY"}
        with self.assertRaises(themes_mod.RegistrationError) as ctx:
            themes_mod.validate(con, row, date(2026, 8, 8))
        self.assertIn("already owned", str(ctx.exception))

    def test_registration_requires_a_horizon_in_the_key_question(self):
        con = mem()
        row = {"id": "TEST-NOHORIZON", "label": "x",
               "key_question": "这个主题会不会好",
               "terms": ["aaa", "bbb", "ccc", "ddd"],
               "price_indicator": "US.SPY"}
        with self.assertRaises(themes_mod.RegistrationError) as ctx:
            themes_mod.validate(con, row, date(2026, 8, 8))
        self.assertIn("horizon", str(ctx.exception))

    def test_registration_requires_enough_synonyms(self):
        con = mem()
        row = {"id": "TEST-THIN", "label": "x",
               "key_question": "未来1–6个月，x 能否 y", "terms": ["aaa"],
               "price_indicator": "US.SPY"}
        with self.assertRaises(themes_mod.RegistrationError):
            themes_mod.validate(con, row, date(2026, 8, 8))

    def test_registry_ids_are_unique(self):
        ids = [t.id for t in lexicon.THEMES]
        self.assertEqual(len(ids), len(set(ids)), "duplicate theme id in registry")

    def test_boilerplate_phrases_are_rejected(self):
        for junk in ("维持买入评级", "持买入评级", "季度财报电话会", "度财报电话会",
                     "上调目标价至", "标价下调至", "corporation", "三季度财报电",
                     "国际", "香港", "亚太"):
            self.assertTrue(themes_mod._noise_composite(junk),
                            f"{junk!r} is boilerplate but was kept")
        for real in ("spacex", "轮动", "光模块", "人形机器人", "央行购金",
                     "glp-1", "资金流向", "电信", "稀土"):
            self.assertFalse(themes_mod._noise_composite(real),
                             f"{real!r} is a real topic but was filtered out")

    def test_short_fragments_are_absorbed_rather_than_pattern_matched(self):
        """The two filters have distinct jobs; neither can cover for the other.

        '持买入评' is only 50% boilerplate by character coverage, so the noise
        filter keeps it — correctly, since a 4-character window is too small to
        judge. It dies by absorption into 维持买入评级, which shares its documents
        and *is* rejected as boilerplate. Order is load-bearing: filtering noise
        first deletes the parent and leaves the orphaned fragments looking like
        novel high-lift phrases, which is exactly how the first cut of this
        module surfaced 持买入评 as a top candidate on four separate days.
        """
        self.assertFalse(themes_mod._noise_composite("持买入评"))
        docs = {"d1", "d2", "d3", "d4"}
        kept = [{"phrase": p, "docs": set(docs), "n_docs": 4, "lift": 5.0}
                for p in ("维持买入评级", "持买入评级", "持买入评", "买入评")]
        surviving = [k["phrase"] for k in themes_mod._maximal(kept)]
        self.assertEqual(surviving, ["维持买入评级"])
        final = [p for p in surviving if not themes_mod._noise_composite(p)]
        self.assertEqual(final, [], "boilerplate survived the full pipeline")

    def test_fragments_are_absorbed_by_their_maximal_phrase(self):
        docs = {"d1", "d2", "d3"}
        kept = [
            {"phrase": "人形机器人", "docs": set(docs), "n_docs": 3, "lift": 4.0},
            {"phrase": "形机器人", "docs": set(docs), "n_docs": 3, "lift": 4.0},
            {"phrase": "机器人", "docs": set(docs), "n_docs": 3, "lift": 4.0},
        ]
        out = [k["phrase"] for k in themes_mod._maximal(kept)]
        self.assertEqual(out, ["人形机器人"])

    def test_a_distinct_topic_is_not_absorbed_by_a_containing_phrase(self):
        """Subsumption is by shared documents, not by substring alone."""
        kept = [
            {"phrase": "人形机器人", "docs": {"d1", "d2"}, "n_docs": 2, "lift": 4.0},
            {"phrase": "机器人", "docs": {"d7", "d8", "d9", "d10"},
             "n_docs": 4, "lift": 3.0},
        ]
        out = sorted(k["phrase"] for k in themes_mod._maximal(kept))
        self.assertEqual(out, ["人形机器人", "机器人"])

    def test_mining_suppresses_against_the_as_of_dictionary_not_todays(self):
        """A theme registered later must not suppress its own past candidacy.

        `_known_terms()` originally read the whole registry, so the moment
        SPACE-ECONOMY was registered on 2026-08-08 a replay of 08-07 stopped
        surfacing "spacex" — the historical run looked as though it had already
        found a theme it had not yet seen. Suppression must be as-of too.
        """
        discovered = [t for t in lexicon.THEMES if t.origin == "discovered"]
        if not discovered:
            self.skipTest("no discovered themes registered yet")
        t = min(discovered, key=lambda t: t.registered_d)
        before = date.fromisoformat(t.registered_d) - timedelta(days=1)
        terms_before = themes_mod._known_terms(before)
        self.assertNotIn(t.terms[0].lower(), terms_before,
                         f"{t.id}'s synonyms leak into a day before it existed")
        self.assertIn(t.terms[0].lower(),
                      themes_mod._known_terms(date.fromisoformat(t.registered_d)))

    def test_coverage_is_reported_so_the_blind_spot_stays_visible(self):
        self.assertEqual(lexicon.coverage(54, 100), 54.0)
        self.assertIsNone(lexicon.coverage(0, 0))

    def test_scoring_reports_dictionary_reach(self):
        con = mem()
        ev = scoring.collect_evidence(con, date(2026, 8, 6))
        for k in ("registered_themes", "docs_total", "docs_matched", "coverage_pct"):
            self.assertIn(k, ev)

    def test_cold_start_themes_are_flagged_not_hidden(self):
        """A theme younger than the A baseline has no own history to compare to.

        Flagging it makes "are discovered themes worse?" a measurable question
        instead of an invisible one.
        """
        src = Path("ideagen/scoring.py").read_text(encoding="utf-8")
        self.assertIn("cold_start", src)
        self.assertIn("config.BASELINE_WINDOW_DAYS", src)


class TestBatchReplaceIntegrity(unittest.TestCase):
    """Replacing a batch must not leave positions bound to different instruments.

    `idea_uid` is `<batch_id>#<local_id>`, not content-derived, so re-importing a
    batch silently rebinds every uid to whatever instrument now sits at that
    local id. Deleting only the ideas leaves live positions attached to uids that
    have changed meaning, and every downstream join still succeeds.

    This is not hypothetical. Restoring the authored 2026-07-27 pack over a
    backfill-generated batch of the same id left 58 positions across three books
    on the wrong instruments: B20260727#26 held US.URA entered at 40.33 while its
    idea had become US.DLR, so settle marked that entry against DLR's 192.56
    close and booked +377%. The batch mean reached +48.5% and dragged the
    published idea-level equal-weight return from +0.96% to +5.70%. It went
    unnoticed for ten days because nothing compared the two sides.
    """

    def _idea_and_position(self, con, pos_code, idea_code):
        db.upsert(con, "batches", {"batch_id": "BX", "as_of": "2026-08-01",
                                   "generated_at": "2026-08-01T07:23:00+08:00",
                                   "generator": "test", "n_ideas": 1,
                                   "methodology": config.METHODOLOGY_VERSION,
                                   "output_sha": "x", "validation": {},
                                   "status": "validated"}, ["batch_id"])
        db.upsert(con, "ideas", {"idea_uid": "BX#1", "batch_id": "BX",
                                 "as_of": "2026-08-01", "local_id": 1,
                                 "tool": idea_code.split(".")[-1],
                                 "horizon": "1个月", "horizon_months": 1,
                                 "instrument": "listed", "hurdle": 0.3,
                                 "futu_code": idea_code}, ["idea_uid"])
        db.upsert(con, "positions", {"pos_id": "PX", "book_id": "naive",
                                     "idea_uid": "BX#1", "code": pos_code,
                                     "kind": "listed", "qty": 1, "avg_px": 1,
                                     "cost": 1, "opened_d": "2026-08-01",
                                     "status": "open"}, ["pos_id"])

    def test_mismatch_is_detected(self):
        con = mem()
        self._idea_and_position(con, "US.URA", "US.DLR")
        bad = ideas.instrument_mismatches(con)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["position_code"], "US.URA")
        self.assertEqual(bad[0]["idea_code"], "US.DLR")

    def test_matching_instrument_is_not_flagged(self):
        con = mem()
        self._idea_and_position(con, "US.DLR", "US.DLR")
        self.assertEqual(ideas.instrument_mismatches(con), [])

    def test_purge_removes_dependents_not_just_ideas(self):
        con = mem()
        self._idea_and_position(con, "US.URA", "US.URA")
        db.upsert(con, "outcomes", {"idea_uid": "BX#1", "as_of": "2026-08-01"},
                  ["idea_uid"])
        n = ideas.purge_batch(con, "BX")
        self.assertEqual(n["ideas"], 1)
        self.assertEqual(n["positions"], 1)
        self.assertEqual(n["outcomes"], 1)
        for t in ("ideas", "positions", "outcomes"):
            left = db.q1(con, f"SELECT COUNT(*) n FROM {t}")["n"]
            self.assertEqual(left, 0, f"{t} still references the purged batch")

    def test_settle_refuses_to_publish_mismatched_data(self):
        """Better to fail loudly than to book a $40 entry against a $192 close."""
        con = mem()
        self._idea_and_position(con, "US.URA", "US.DLR")
        with self.assertRaises(RuntimeError) as ctx:
            analytics.settle(con, verbose=False)
        msg = str(ctx.exception)
        self.assertIn("US.URA", msg)
        self.assertIn("US.DLR", msg)
        self.assertIn("rebuild-batch", msg)

    def test_live_database_has_no_mismatches(self):
        """Regression guard on the real book, not just a synthetic fixture."""
        con = db.init()
        bad = ideas.instrument_mismatches(con)
        self.assertEqual(bad, [], f"{len(bad)} positions disagree with their idea")
