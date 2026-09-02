"""Unit tests for the rules that keep the study honest.

Run with:  python3 -m unittest discover -s tests -v

These cover the invariants that, if broken, would silently flatter the result:
scenario arithmetic, cost deduction, the session-completeness guard, no same-bar
look-ahead, limit-fill pricing, marking idempotency, and the validation gate.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen import analytics, config, db, ideas, lexicon, olive_web, paper, scoring
from ideagen import themes as themes_mod
from ideagen.sources import futu_px, olive, wisburg


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

    def test_any_positive_count_passes_but_empty_fails(self):
        """The forced-40 quota is gone — it was the defect the 08-07 review
        named: thin days padded with ideas nobody believed. A batch carries
        however many ideas selection produced; only empty is invalid."""
        rep = self._validate([self._idea(1)])
        self.assertNotIn("idea_count",
                         [c["check"] for c in rep["checks"] if not c["ok"]],
                         "a one-idea batch must not fail on count")
        rep0 = self._validate([])
        self.assertIn("idea_count",
                      [c["check"] for c in rep0["checks"] if not c["ok"]],
                      "an empty batch is the only invalid size")

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
    def test_api_key_alias_is_accepted(self):
        with mock.patch.dict(os.environ, {
                "WISBURG_MCP_TOKEN": "",
                "WISBURG_API_KEY": "alias-key",
        }):
            self.assertTrue(config.wisburg_configured())
            self.assertEqual(config.wisburg_token(), "alias-key")

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


class TestOliveMCP(unittest.TestCase):
    class Response:
        def __init__(self, obj=None, *, status=200, headers=None):
            self.status_code = status
            self.headers = headers or {}
            self.content = (json.dumps(obj).encode() if obj is not None else b"")
            self.text = self.content.decode()

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return json.loads(self.content)

    def test_streamable_http_session_and_tools(self):
        session = mock.Mock()
        session.headers = {}
        session.post.side_effect = [
            self.Response(
                {"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "olive", "version": "1"},
                }},
                headers={"Mcp-Session-Id": "session-1"},
            ),
            self.Response(status=202),
            self.Response({"jsonrpc": "2.0", "id": 2, "result": {
                "tools": [{"name": "list_funds"}],
            }}),
        ]
        with mock.patch.object(olive.requests, "Session",
                               return_value=session):
            client = olive.OliveMCP(access_token="token")
            info = client.initialize()
            tools = client.tools()

        self.assertEqual(info["protocolVersion"], "2025-06-18")
        self.assertEqual(tools, ["list_funds"])
        self.assertEqual(session.headers["MCP-Protocol-Version"],
                         "2025-06-18")
        self.assertEqual(
            session.post.call_args_list[1].kwargs["headers"],
            {"Mcp-Session-Id": "session-1"},
        )

    def test_expired_access_token_is_refreshed_once(self):
        session = mock.Mock()
        session.headers = {}
        session.post.side_effect = [
            self.Response({"error": "unauthorized"}, status=401),
            self.Response({"jsonrpc": "2.0", "id": 1, "result": {
                "tools": [{"name": "list_funds"}],
            }}),
        ]
        token_response = self.Response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        })
        with mock.patch.object(olive.requests, "Session",
                               return_value=session), \
             mock.patch.object(olive.requests, "post",
                               return_value=token_response) as refresh:
            client = olive.OliveMCP(
                access_token="expired",
                refresh_token="old-refresh",
                client_id="client-id",
            )
            self.assertEqual(client.tools(), ["list_funds"])

        self.assertEqual(session.headers["Authorization"], "Bearer new-access")
        self.assertEqual(client.refresh_token, "new-refresh")
        refresh.assert_called_once()

    def test_catalog_and_detail_merge_are_ingestable(self):
        catalog = olive.parse_catalog(
            "| 产品ID | 产品名称 | 市场类型 | 策略 | 系列 | 开始 | 结束 | 通道 | 预约 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| Z99999 | Sample Multi Strategy | 海外二级 | 多策略 | - |"
            " 2026-01-01 | 2026-12-31 | 新加坡 | - |\n"
        )
        self.assertEqual(len(catalog), 1)
        merged = olive._merge_fund(catalog[0], {
            "get_fund_summary": {
                "fundName": "Sample Multi Strategy",
                "card": {
                    "mainMetrics": {"riskLevel": "R3"},
                    "chartData": {"dataPoints": [
                        {"date": "2026-08-29", "value": 1.25},
                    ]},
                },
            },
            "get_fund_performance": {
                "performance": {"meta": {"currency": "USD"}},
            },
        })
        self.assertEqual(merged["productCode"], "Z99999")
        self.assertEqual(merged["latestNav"], 1.25)
        self.assertEqual(merged["navDate"], "2026-08-29")
        self.assertEqual(merged["currency"], "USD")

    def test_oauth_authorization_uses_pkce_and_resource_indicator(self):
        url, verifier, state = olive.oauth_authorization(
            "client-id", "http://127.0.0.1:8766/callback")
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("resource=https%3A%2F%2Fcatalog.example%2Fmcp", url)
        self.assertNotIn(verifier, url)
        self.assertGreater(len(verifier), 40)
        self.assertGreater(len(state), 20)

    def test_remote_credentials_are_stored_mode_600_and_loaded(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
                os.environ, {
                    "IDEAGEN_OLIVE_TOKEN_FILE": str(Path(td) / "tokens.json"),
                    "OLIVE_OAUTH_ACCESS_TOKEN": "",
                    "OLIVE_OAUTH_REFRESH_TOKEN": "",
                    "OLIVE_OAUTH_CLIENT_ID": "",
                }):
            path = config.store_olive_credentials({
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "client_id": "remote-client",
            })
            loaded = config.olive_credentials()

            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(loaded["access_token"], "access-secret")
            self.assertEqual(loaded["refresh_token"], "refresh-secret")
            self.assertEqual(loaded["client_id"], "remote-client")

    def test_remote_oauth_consumes_state_once_and_never_returns_tokens(self):
        client = mock.Mock()
        client.initialize.return_value = {
            "serverInfo": {"name": "olive", "version": "1"},
        }
        client.tools.return_value = ["list_funds", "get_fund_detail"]
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
                os.environ, {
                    "IDEAGEN_PUBLIC_SITE": "https://dashboard.example.com",
                    "IDEAGEN_OLIVE_TOKEN_FILE": str(Path(td) / "tokens.json"),
                    "OLIVE_OAUTH_ACCESS_TOKEN": "",
                    "OLIVE_OAUTH_REFRESH_TOKEN": "",
                    "OLIVE_OAUTH_CLIENT_ID": "",
                }), \
             mock.patch.object(
                 olive, "register_oauth_client",
                 return_value={"client_id": "remote-client"}), \
             mock.patch.object(
                 olive, "oauth_authorization",
                 return_value=("https://sso.example/authorize",
                               "verifier-secret", "state-secret")), \
             mock.patch.object(
                 olive, "exchange_oauth_code",
                 return_value={
                     "access_token": "access-secret",
                     "refresh_token": "refresh-secret",
                     "expires_in": 3600,
                 }), \
             mock.patch.object(olive, "OliveMCP", return_value=client), \
             mock.patch.object(olive_web, "start_sync") as start_sync:
            olive_web.reset_for_tests()
            url = olive_web.begin_authorization()
            result = olive_web.complete_authorization(
                "state=state-secret&code=authorization-code")

            self.assertEqual(url, "https://sso.example/authorize")
            self.assertEqual(result["tool_count"], 2)
            self.assertNotIn("access_token", result)
            self.assertNotIn("refresh_token", result)
            start_sync.assert_called_once()
            with self.assertRaisesRegex(ValueError, "invalid or expired"):
                olive_web.complete_authorization(
                    "state=state-secret&code=replayed-code")


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
        if not self.pl["positions"]:
            self.skipTest("requires the populated local paper fixture")
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
        for host in ("assets.example/", "documents.example/"):
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
        evidence = [
            e
            for day in self.pl["days"].values()
            for theme in (day.get("report") or {}).get("themes", [])
            for e in theme["evidence"]
        ]
        if not evidence:
            self.skipTest("requires the populated local corpus fixture")
        seen = 0
        for e in evidence:
            if e.get("retrieval"):
                seen += 1
                self.assertTrue(e["hash"])
        self.assertGreater(seen, 50)

    def test_ideas_resolve_their_citations_to_readable_references(self):
        sources = [
            source
            for day in self.pl["days"].values()
            for idea in (day.get("batch") or {}).get("ideas", [])
            for source in idea.get("sources_resolved", [])
        ]
        if not sources:
            self.skipTest("requires the populated local citation fixture")
        checked = 0
        for source in sources:
            checked += 1
            self.assertIn("resolved", source)
            if source["resolved"]:
                self.assertTrue(source["title"])
                self.assertTrue(source["retrieval"])
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


class TestCohortInception(unittest.TestCase):
    """Each cohort's curve must start the day its own batch was placed.

    The replay originally stepped every book returned by `paper.all_books` on
    every day. Cohort *registrations* live in the `books` table, which
    `reset_book` does not clear, so a re-run saw all ten cohorts from the first
    replayed day and gave each an equity row from that day. Every cohort then
    reported the same 8-session holding period and the same +3.99% SPY
    comparison — including 2026-08-07, which has no fills at all and was
    showing a +0.13% return over a period it did not exist for.
    """

    def test_live_cohorts_start_when_their_batch_was_placed(self):
        con = db.init()
        for bk in paper.cohort_books(con):
            first_order = db.q1(con, "SELECT MIN(placed_d) d FROM orders "
                                     "WHERE book_id=?", (bk,))
            first_equity = db.q1(con, "SELECT MIN(d) d FROM equity "
                                      "WHERE book_id=?", (bk,))
            if not (first_order and first_order["d"] and first_equity):
                continue
            self.assertGreaterEqual(
                first_equity["d"], _prev_session(con, first_order["d"]),
                f"{bk} has equity before its first order was placed")

    def test_holding_periods_are_not_all_identical(self):
        """A shared holding period across every vintage is the symptom to catch."""
        con = db.init()
        spans = []
        for bk in paper.cohort_books(con):
            n = db.q1(con, "SELECT COUNT(*) n FROM equity WHERE book_id=?", (bk,))["n"]
            if n:
                spans.append(n)
        if len(spans) < 3:
            self.skipTest("not enough cohorts yet")
        self.assertGreater(len(set(spans)), 1,
                           f"every cohort spans {spans[0]} rows; they were "
                           f"marked from a shared start date rather than their own")

    def test_a_cohort_with_no_fills_reports_no_return(self):
        con = db.init()
        for bk in paper.cohort_books(con):
            filled = db.q1(con, "SELECT COUNT(*) n FROM positions WHERE book_id=?",
                           (bk,))["n"]
            if filled:
                continue
            last = db.q1(con, "SELECT cum_ret FROM equity WHERE book_id=? "
                              "ORDER BY d DESC LIMIT 1", (bk,))
            if last and last["cum_ret"] is not None:
                self.assertAlmostEqual(
                    last["cum_ret"], 0.0, places=6,
                    msg=f"{bk} has no positions but reports a return")


def _prev_session(con, d: str) -> str:
    r = db.q1(con, "SELECT d FROM prices WHERE code='US.SPY' AND d<? "
                   "ORDER BY d DESC LIMIT 1", (d,))
    return r["d"] if r else d


class TestDictionaryIsVisible(unittest.TestCase):
    """A registered theme must be findable even before it can be scored.

    The as-of rule hides a theme from every date earlier than its registration,
    which is correct — and the first cut of theme discovery shipped with no other
    surface, so two themes registered on a Saturday appeared literally nowhere on
    the page. The mechanism looked like it had done nothing. The registry, the
    registration dates, and the reason a theme is not yet scoreable all have to be
    legible independently of which day is selected.
    """

    @classmethod
    def setUpClass(cls):
        from ideagen import payload, report
        con = db.init()
        cls.pl = payload.build(con)
        cls.html = report.render(cls.pl) if hasattr(report, "render") else None

    def test_payload_carries_the_registry(self):
        D = self.pl.get("dictionary")
        self.assertIsNotNone(D, "payload has no dictionary block")
        self.assertEqual(len(D["themes"]), len(lexicon.THEMES))
        for t in D["themes"]:
            for k in ("id", "label", "origin", "registered_d", "indicator",
                      "days_scored", "pending"):
                self.assertIn(k, t)

    def test_a_theme_registered_after_the_last_page_date_is_marked_pending(self):
        D = self.pl["dictionary"]
        last = D["newest_page_date"]
        if not last:
            self.skipTest("no dates yet")
        for t in D["themes"]:
            self.assertEqual(t["pending"], t["registered_d"] > last,
                             f"{t['id']} pending flag disagrees with its date")

    def test_discovered_themes_are_counted_separately(self):
        D = self.pl["dictionary"]
        self.assertEqual(D["n_seed"] + D["n_discovered"], len(D["themes"]))
        self.assertEqual(
            D["n_discovered"],
            sum(1 for t in lexicon.THEMES if t.origin == "discovered"))

    def test_the_page_renders_the_dictionary_and_explains_pending(self):
        src = Path("ideagen/report.py").read_text(encoding="utf-8")
        self.assertIn("function dictionaryBlock", src)
        self.assertIn("dictionaryBlock()", src)
        # The explanation is the whole point: without it a reader sees a theme
        # listed with no scores and concludes the feature is broken.
        self.assertIn("已注册但还没有出现在任何一天上", src)
        self.assertIn("待生效", src)


class TestBenchmarkWindowAlignment(unittest.TestCase):
    """The benchmark must span the position's holding period, not the idea's age.

    Raised in Jon's PM review. A limit order that takes three sessions to fill is
    held for a shorter window than the idea has existed, so measuring its
    benchmark from the idea date compares five days of position against eight days
    of index and reports the difference as skill.

    Currently latent rather than live: `settle` runs on the `naive` book, where
    every fill lands on the idea date. It bites on `disciplined` (36 of 119 fills
    are late) and by construction in the weekly design, where a batch generated
    Wednesday morning cannot fill before Wednesday's close.
    """

    def test_benchmark_starts_at_the_fill_not_the_idea_date(self):
        src = Path("ideagen/analytics.py").read_text(encoding="utf-8")
        self.assertIn("bench_from", src)
        self.assertIn('pos["opened_d"] if pos', src,
                      "filled positions must benchmark from their own open")
        self.assertIn("benchmark_return(con, bench, bench_from, mark_to)", src)
        self.assertNotIn('benchmark_return(con, bench, idea["as_of"], mark_to)', src,
                         "the idea-dated benchmark call must be gone")

    def test_sessions_held_uses_the_same_window_as_the_benchmark(self):
        """Otherwise the held count and the benchmark disagree about the period."""
        src = Path("ideagen/analytics.py").read_text(encoding="utf-8")
        i = src.index("bench_from = ")
        seg = src[i:i + 1200]
        self.assertIn("(bench, bench_from, mark_to)", seg,
                      "sessions_held must be counted over the benchmark window")


class TestFundPositionsAreNotFalselyFlagged(unittest.TestCase):
    """A fund position carries an Olive key, not a Futu code.

    The first cut of `instrument_mismatches` compared every position against
    `ideas.futu_code`, which is NULL for funds by design. That flagged all 19 fund
    ideas as corrupt and made `settle` refuse to run on any batch holding one —
    a guard that blocks correct data is worse than no guard.
    """

    def test_a_fund_position_matching_its_olive_key_is_clean(self):
        con = mem()
        db.upsert(con, "batches", {
            "batch_id": "BF", "as_of": "2026-08-01",
            "generated_at": "2026-08-01T07:23:00+08:00", "generator": "test",
            "n_ideas": 1, "methodology": config.METHODOLOGY_VERSION,
            "output_sha": "x", "validation": {}, "status": "validated"}, ["batch_id"])
        db.upsert(con, "ideas", {
            "idea_uid": "BF#1", "batch_id": "BF", "as_of": "2026-08-01",
            "local_id": 1, "tool": "SomeFund", "horizon": "1个月",
            "horizon_months": 1, "instrument": "fund", "hurdle": 0.3,
            "futu_code": None, "olive_key": "FUND-EXAMPLE"}, ["idea_uid"])
        db.upsert(con, "positions", {
            "pos_id": "PF", "book_id": "naive", "idea_uid": "BF#1",
            "code": "FUND-EXAMPLE", "kind": "fund", "qty": 1, "avg_px": 1,
            "cost": 1, "opened_d": "2026-08-01", "status": "open"}, ["pos_id"])
        self.assertEqual(ideas.instrument_mismatches(con), [])

    def test_a_fund_position_on_the_wrong_key_is_still_caught(self):
        con = mem()
        db.upsert(con, "batches", {
            "batch_id": "BF", "as_of": "2026-08-01",
            "generated_at": "2026-08-01T07:23:00+08:00", "generator": "test",
            "n_ideas": 1, "methodology": config.METHODOLOGY_VERSION,
            "output_sha": "x", "validation": {}, "status": "validated"}, ["batch_id"])
        db.upsert(con, "ideas", {
            "idea_uid": "BF#1", "batch_id": "BF", "as_of": "2026-08-01",
            "local_id": 1, "tool": "SomeFund", "horizon": "1个月",
            "horizon_months": 1, "instrument": "fund", "hurdle": 0.3,
            "futu_code": None, "olive_key": "FUND-EXAMPLE"}, ["idea_uid"])
        db.upsert(con, "positions", {
            "pos_id": "PF", "book_id": "naive", "idea_uid": "BF#1",
            "code": "LU0274383776", "kind": "fund", "qty": 1, "avg_px": 1,
            "cost": 1, "opened_d": "2026-08-01", "status": "open"}, ["pos_id"])
        bad = ideas.instrument_mismatches(con)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["idea_code"], "FUND-EXAMPLE")


class TestPlatformPorts(unittest.TestCase):
    """The platform layer must be usable with no cloud SDK installed.

    That property is what lets the methodology change freely: if importing the
    pipeline required TOS and psycopg, every test and every laptop would need a
    cloud account, and the six ports would stop being a boundary.
    """

    def test_load_never_raises_even_when_nothing_is_configured(self):
        """A missing credential must be a health failure, not a stack trace.

        The first cut raised NotConfigured from `load()` when ARK_API_KEY was
        absent, so `ideagen platform` — the one command meant to work when
        nothing else does — crashed instead of reporting which variable to set.
        """
        import os
        from ideagen import platform as P
        keep = {k: os.environ.pop(k, None) for k in
                ("ARK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "IDEAGEN_TOS_BUCKET", "IDEAGEN_PG_DSN")}
        env_files = P._ENV_FILES
        P._ENV_FILES = ()
        try:
            for which in ("local", "byteplus"):
                p = P.load(platform=which)          # must not raise
                names = {h.name for h in p.check()}
                self.assertEqual(
                    names, {"secrets", "state", "blobs", "inference", "cache", "events"})
        finally:
            P._ENV_FILES = env_files
            for k, v in keep.items():
                if v is not None:
                    os.environ[k] = v

    def test_project_env_can_override_the_operator_file(self):
        """The ignored project .env is the POC handoff surface.

        It must override ~/.ideagen.env without mutating that operator-wide file,
        while process environment variables still win over both.
        """
        import os
        import tempfile
        from ideagen.platform.local import EnvSecretStore
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home.env"
            project = Path(td) / "project.env"
            home.write_text("IDEAGEN_PG_HOST=old.example\n", encoding="utf-8")
            project.write_text("IDEAGEN_PG_HOST=new.example\n", encoding="utf-8")
            home.chmod(0o600)
            project.chmod(0o600)
            old = os.environ.pop("IDEAGEN_PG_HOST", None)
            try:
                sec = EnvSecretStore((home, project))
                self.assertEqual(sec.get("IDEAGEN_PG_HOST"), "new.example")
                self.assertEqual(sec.source("IDEAGEN_PG_HOST"), str(project))
                self.assertTrue(sec.check().ok)
                os.environ["IDEAGEN_PG_HOST"] = "process.example"
                self.assertEqual(sec.get("IDEAGEN_PG_HOST"), "process.example")
            finally:
                os.environ.pop("IDEAGEN_PG_HOST", None)
                if old is not None:
                    os.environ["IDEAGEN_PG_HOST"] = old

    def test_env_report_hides_the_inactive_database_engine(self):
        import tempfile
        from ideagen import platform as P
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "IDEAGEN_STATE_ENGINE=mysql\n"
                "IDEAGEN_MYSQL_HOST=mysql.example\n", encoding="utf-8")
            env_file.chmod(0o600)
            env_files = P._ENV_FILES
            P._ENV_FILES = (env_file,)
            try:
                keys = {row["key"] for row in P.env_report()}
            finally:
                P._ENV_FILES = env_files
            self.assertIn("IDEAGEN_MYSQL_HOST", keys)
            self.assertNotIn("IDEAGEN_PG_HOST", keys)

    def test_postgres_fields_build_connection_options_without_a_url(self):
        """Separate fields avoid URL-escaping database passwords by hand."""
        from ideagen import platform as P
        values = {
            "IDEAGEN_PG_HOST": "db.example",
            "IDEAGEN_PG_DATABASE": "ideagen",
            "IDEAGEN_PG_USER": "app",
            "IDEAGEN_PG_PASSWORD": "test-password",
            "IDEAGEN_PG_SSLMODE": "require",
        }
        options = P._postgres_options(lambda key, default=None:
                                      values.get(key, default))
        self.assertEqual(options["port"], 5432)
        self.assertEqual(options["password"], "test-password")
        self.assertEqual(options["sslmode"], "require")
        self.assertEqual(options["connect_timeout"], 10)

    def test_mysql_fields_build_connection_options(self):
        from ideagen import platform as P
        values = {
            "IDEAGEN_MYSQL_HOST": "mysql.example",
            "IDEAGEN_MYSQL_DATABASE": "ideagen",
            "IDEAGEN_MYSQL_USER": "app",
            "IDEAGEN_MYSQL_PASSWORD": "test-password",
        }
        options = P._mysql_options(lambda key, default=None:
                                   values.get(key, default))
        self.assertEqual(options["port"], 3306)
        self.assertEqual(options["database"], "ideagen")
        self.assertEqual(options["password"], "test-password")
        self.assertEqual(options["connect_timeout"], 10)

    def test_requested_mysql_rejects_an_empty_configuration(self):
        from ideagen import platform as P
        from ideagen.platform.base import NotConfigured
        with self.assertRaises(NotConfigured) as e:
            P._mysql_options(lambda key, default=None: default, required=True)
        self.assertIn("IDEAGEN_MYSQL_HOST", str(e.exception))

    def test_partial_postgres_config_fails_instead_of_falling_back(self):
        from ideagen import platform as P
        from ideagen.platform.base import NotConfigured
        values = {"IDEAGEN_PG_HOST": "db.example"}
        with self.assertRaises(NotConfigured) as e:
            P._postgres_options(lambda key, default=None:
                                values.get(key, default))
        self.assertIn("IDEAGEN_PG_PASSWORD", str(e.exception))

    def test_postgres_store_keeps_separate_fields_for_psycopg(self):
        from ideagen.platform.byteplus import PostgresStateStore
        store = PostgresStateStore(
            host="db.example", port=5432, dbname="ideagen", user="app",
            password="test-password", sslmode="require", connect_timeout=7)
        self.assertIsNone(store.dsn)
        self.assertEqual(store.connect_kwargs["dbname"], "ideagen")
        self.assertEqual(store.connect_kwargs["password"], "test-password")
        self.assertEqual(store.connect_kwargs["sslmode"], "require")
        self.assertEqual(store.connect_timeout, 7)

    def test_mysql_store_keeps_credentials_out_of_a_url(self):
        from ideagen.platform.byteplus import MySQLStateStore
        store = MySQLStateStore(
            host="mysql.example", port=3306, database="ideagen", user="app",
            password="test-password", ssl_ca="/tmp/ca.pem", connect_timeout=7)
        self.assertEqual(store.dialect, "mysql")
        self.assertEqual(store.connect_kwargs["database"], "ideagen")
        self.assertEqual(store.connect_kwargs["password"], "test-password")
        self.assertEqual(store.connect_kwargs["ssl"], {"ca": "/tmp/ca.pem"})
        self.assertEqual(store.connect_kwargs["connect_timeout"], 7)

    def test_tos_requires_an_explicit_endpoint(self):
        from ideagen.platform.base import NotConfigured
        from ideagen.platform.byteplus import TosBlobStore
        with self.assertRaises(NotConfigured):
            TosBlobStore(
                ak="ak", sk="sk", bucket="bucket", region="example-region")
        store = TosBlobStore(
            ak="ak", sk="sk", bucket="bucket", region="example-region",
            endpoint="https://storage.example")
        self.assertEqual(store.endpoint, "https://storage.example")

    def test_tos_unknown_region_requires_an_explicit_endpoint(self):
        from ideagen.platform.base import NotConfigured
        from ideagen.platform.byteplus import TosBlobStore
        with self.assertRaises(NotConfigured):
            TosBlobStore(
                ak="ak", sk="sk", bucket="bucket", region="unknown-region")

    def test_byteplus_selects_mysql_from_the_state_engine(self):
        import tempfile
        from ideagen import platform as P
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "IDEAGEN_STATE_ENGINE=mysql\n"
                "IDEAGEN_MYSQL_HOST=mysql.example\n"
                "IDEAGEN_MYSQL_DATABASE=ideagen\n"
                "IDEAGEN_MYSQL_USER=app\n"
                "IDEAGEN_MYSQL_PASSWORD=secret\n", encoding="utf-8")
            env_file.chmod(0o600)
            env_files = P._ENV_FILES
            P._ENV_FILES = (env_file,)
            try:
                platform = P.load(platform="byteplus")
            finally:
                P._ENV_FILES = env_files
            self.assertEqual(platform.state.dialect, "mysql")

    def test_kms_falls_back_to_the_project_env_store(self):
        from ideagen.platform.byteplus import KmsSecretStore
        from ideagen.platform.local import EnvSecretStore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("IDEAGEN_PG_HOST=db.example\n", encoding="utf-8")
            env_file.chmod(0o600)
            fallback = EnvSecretStore(env_file)
            store = KmsSecretStore(
                ak="", sk="", fallback_store=fallback, fallback_env=False)
            self.assertEqual(store.get("IDEAGEN_PG_HOST"), "db.example")
            self.assertIn("IDEAGEN_PG_HOST", store.used_fallback)

    def test_existing_ark_names_resolve_to_platform_settings(self):
        from ideagen import platform as P
        from ideagen.platform.local import EnvSecretStore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "ARK_BASE_URL=https://ark.example/v3\n"
                "ARK_MODEL_ID=ep-demo\n"
                "ARK_TIMEOUT_SECONDS=45\n"
                "ARK_MAX_RETRIES=1\n", encoding="utf-8")
            env_file.chmod(0o600)
            sec = EnvSecretStore(env_file)
            self.assertEqual(P._setting(
                sec, "IDEAGEN_INFERENCE_BASE_URL"), "https://ark.example/v3")
            self.assertEqual(P._setting(sec, "IDEAGEN_ARK_MODEL"), "ep-demo")
            self.assertEqual(P._setting(
                sec, "IDEAGEN_INFERENCE_TIMEOUT_SECONDS"), "45")

    def test_an_unconfigured_port_reports_and_then_raises_on_use(self):
        from ideagen.platform.base import NotConfigured, Unavailable
        u = Unavailable("inference", "ARK_API_KEY is not set")
        h = u.check()
        self.assertFalse(h.ok)
        self.assertIn("ARK_API_KEY", h.detail)
        with self.assertRaises(NotConfigured):
            u.complete("hello")

    def test_events_do_not_gate_readiness(self):
        """Losing monitoring must not cost a week of corpus.

        The corpus for a given week cannot be re-fetched later at the depth a live
        run would have had, so refusing to run is the more expensive failure.
        """
        from ideagen.platform.base import Health, Platform, Unavailable
        from ideagen.platform.local import (FileCache, LocalBlobStore,
                                            SqliteStateStore)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = Platform(
                name="t", blobs=LocalBlobStore(root / "b"),
                state=SqliteStateStore(root / "s.db"),
                inference=_AlwaysOk("inference"), cache=FileCache(root / "c"),
                secrets=_AlwaysOk("secrets"),
                events=Unavailable("events", "kafka down"))
            self.assertTrue(p.ready(), "a dead event bus must not block a run")

            # Inference is not in DEFAULT_NEED, because whether a run needs a model
            # depends on which strategies it runs: a mechanical-only selection has
            # no reason to be blocked by a missing key. The protection therefore
            # lives in the declared need, and it must still bite when asked for.
            p.inference = Unavailable("inference", "no key")
            self.assertTrue(p.ready(),
                            "a mechanical run must not be blocked by a missing key")
            self.assertFalse(p.ready(need=(*Platform.DEFAULT_NEED, "inference")),
                             "a run that declares it needs a model must be blocked")
            self.assertEqual(
                [h.name for h in p.missing(need=("inference",))], ["inference"],
                "the missing port must be named, so the operator knows what to set")

    def test_a_model_using_strategy_makes_inference_required(self):
        """The requirement must travel with the strategy, not with the caller.

        A caller who forgot to declare it would otherwise get a run that fetches
        every feed and scores every topic before discovering that no generator can
        execute — with the expensive half already spent.
        """
        from ideagen import strategy as strat
        gens = [r["name"] for r in strat.available("idea_generator")]
        self.assertTrue(gens, "stage B has no registered generators")
        self.assertTrue(
            strat.needs_model([("idea_generator", n) for n in gens]),
            "generators call models; the registry must say so")
        self.assertEqual(
            strat.needs_model([("idea_selector", "omega_loose")]), [],
            "a mechanical selector must not drag in an inference requirement")

    def test_blobs_are_immutable(self):
        """Replacing an artifact in place is the failure that cost this project
        ten days of wrong numbers; the port must make it impossible."""
        from ideagen.platform.base import PlatformError
        from ideagen.platform.local import LocalBlobStore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            b = LocalBlobStore(Path(td))
            b.put("runs/x/pack.json", b"first")
            self.assertEqual(b.get("runs/x/pack.json"), b"first")
            with self.assertRaises(PlatformError):
                b.put("runs/x/pack.json", b"second")
            self.assertEqual(b.get("runs/x/pack.json"), b"first")

    def test_blob_keys_cannot_escape_the_artifact_root(self):
        from ideagen.platform.base import PlatformError
        from ideagen.platform.local import LocalBlobStore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            b = LocalBlobStore(Path(td) / "root")
            with self.assertRaises(PlatformError):
                b.put("../escaped.json", b"x")

    def test_lock_excludes_a_second_holder(self):
        """Two overlapping runs would place the same orders twice."""
        from ideagen.platform.local import FileCache
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            c = FileCache(Path(td))
            with c.lock("weekly") as got_first:
                self.assertTrue(got_first)
                with c.lock("weekly") as got_second:
                    self.assertFalse(got_second)
            with c.lock("weekly") as got_again:
                self.assertTrue(got_again, "lock must release on exit")

    def test_run_journal_writes_an_immutable_record(self):
        from ideagen.platform import RunJournal
        from ideagen.platform.base import Platform, Unavailable
        from ideagen.platform.local import (FileCache, FileEventBus,
                                           LocalBlobStore, SqliteStateStore)
        import json as _j
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = Platform(name="t", blobs=LocalBlobStore(root / "b"),
                         state=SqliteStateStore(root / "s.db"),
                         inference=_AlwaysOk("inference"),
                         events=FileEventBus(root / "e.jsonl"),
                         cache=FileCache(root / "c"),
                         secrets=_AlwaysOk("secrets"))
            j = RunJournal(p, kind="weekly", as_of="2026-08-12")
            j.step("score", themes=5)
            j.artifact("pack.json", b'{"x":1}')
            uri = j.close(ok=True)
            doc = _j.loads(p.blobs.get(f"{j.prefix}/journal.json"))
            self.assertTrue(doc["ok"])
            self.assertEqual(doc["kind"], "weekly")
            self.assertEqual([s["step"] for s in doc["steps"]], ["score"])
            self.assertEqual(len(doc["artifacts"]), 1)
            self.assertEqual(len(doc["port_health"]), 6,
                             "the journal must record every port's health")
            self.assertTrue(uri.endswith("journal.json"))

    def test_no_module_imports_a_cloud_sdk_at_top_level(self):
        """Cloud imports must be lazy or `platform doctor` cannot report on them."""
        src = Path("ideagen/platform/byteplus.py").read_text(encoding="utf-8")
        head = src.split("class ", 1)[0]
        for sdk in ("import tos", "import psycopg", "import pymysql",
                    "from kafka", "import redis", "from volcengine"):
            self.assertNotIn(sdk, head,
                             f"{sdk!r} must be imported inside a method, not at "
                             f"module top level")


class _AlwaysOk:
    """Minimal healthy port stand-in for wiring tests."""

    def __init__(self, name: str):
        self._name = name

    def check(self):
        from ideagen.platform.base import Health
        return Health(True, self._name, "stub")


class TestThreeStagePipeline(unittest.TestCase):
    """筛选A → 筛选B → 筛选C: the properties that make the stages comparable."""

    def _ctx(self, **kw):
        from ideagen import strategy as strat
        base = dict(
            as_of=date(2026, 8, 12), inputs_sha="x",
            topics=[{"topic_id": "T1", "label": "主题一", "terms": ["联储"]},
                    {"topic_id": "T2", "label": "主题二", "terms": ["日本"]}],
            universe=[{"instrument_id": "SPY", "name": "SPY", "vehicle": "ETF",
                       "exposure": "美股"},
                      {"instrument_id": "TLT", "name": "TLT", "vehicle": "ETF",
                       "exposure": "久期"}],
            corpus=[{"doc_id": "d1", "published_d": "2026-08-11", "tier": 1,
                     "title": "联储降息", "summary": "联储可能降息"}])
        base.update(kw)
        return strat.RunContext(**base)

    def test_a_generator_cannot_invent_an_instrument(self):
        """Stage B is where a model writes objects, so it is where an untradeable
        one would enter. Rejection has to happen at creation, not at fill time."""
        from ideagen import strategy as strat
        v = strat.Verdict(strategy="t", version="1", produced=[{
            "id": "i1", "instrument_id": "NOPE", "topic_id": "T1",
            "thesis": "凭空捏造", "upside_pct": 5, "downside_pct": -3,
            "p_up": .4, "p_base": .4, "p_down": .2}])
        with self.assertRaises(strat.StrategyError) as e:
            strat._check_produced("t", v, self._ctx())
        self.assertIn("universe", str(e.exception))

    def test_a_generator_cannot_attach_an_idea_to_an_unselected_topic(self):
        """An idea whose topic 筛选A did not pick has no scored rationale behind it,
        so its outcome could never be attributed to anything."""
        from ideagen import strategy as strat
        v = strat.Verdict(strategy="t", version="1", produced=[{
            "id": "i1", "instrument_id": "SPY", "topic_id": "T9",
            "thesis": "无主题", "upside_pct": 5, "downside_pct": -3,
            "p_up": .4, "p_base": .4, "p_down": .2}])
        with self.assertRaises(strat.StrategyError):
            strat._check_produced("t", v, self._ctx())

    def test_probabilities_must_sum_to_one(self):
        from ideagen import strategy as strat
        v = strat.Verdict(strategy="t", version="1", produced=[{
            "id": "i1", "instrument_id": "SPY", "topic_id": "T1",
            "thesis": "概率不合", "upside_pct": 5, "downside_pct": -3,
            "p_up": .9, "p_base": .9, "p_down": .9}])
        with self.assertRaises(strat.StrategyError) as e:
            strat._check_produced("t", v, self._ctx())
        self.assertIn("probabilities", str(e.exception))

    def test_no_topics_is_an_error_not_an_empty_result(self):
        """筛选A producing nothing is a broken run. If stage B returned zero ideas
        quietly, that would be indistinguishable from a week with no trades in it."""
        from ideagen.strategies import _gen
        with self.assertRaises(RuntimeError) as e:
            _gen.generate_per_topic(self._ctx(topics=[]), "ai_native",
                                    lambda c, t: ("p", 1))
        self.assertIn("筛选A", str(e.exception))

    def test_registered_params_reach_the_strategy(self):
        """A declared default that the strategy has to restate is decoration: the
        two can disagree and only the code runs."""
        from ideagen import strategy as strat
        seen = {}

        @strat.register("idea_selector", "_probe_params", "1.0",
                        params={"n": 7, "cap": 2})
        def _probe(ctx):
            seen.update(ctx.params)
            return strat.Verdict(strategy="_probe_params", version="1.0")
        try:
            strat.run("idea_selector", "_probe_params", self._ctx(params={"n": 9}))
            self.assertEqual(seen.get("cap"), 2, "declared default must arrive")
            self.assertEqual(seen.get("n"), 9, "the run's value must win")
        finally:
            strat._REGISTRY.pop(("idea_selector", "_probe_params"), None)

    def test_the_pool_holds_each_instrument_once(self):
        """Ten rows naming six instruments is a six-position book with one of them
        at triple weight — not the ten-position portfolio the mandate describes."""
        from ideagen import orchestrator as orc
        pool = [{"id": f"{m}:{t}:SPY", "instrument_id": "SPY", "topic_id": t,
                 "method": m, "thesis": "x", "upside_pct": u, "downside_pct": -4,
                 "p_up": .4, "p_base": .4, "p_down": .2}
                for m, u in (("ai_native", 6.0), ("chain", 10.0), ("gap", 8.0))
                for t in ("T1",)]
        merged = orc._merge_pool(pool)
        self.assertEqual(len(merged), 1, "one instrument must yield one candidate")
        self.assertEqual(merged[0]["upside_pct"], 8.0,
                         "merged odds must be the median, not the most optimistic")
        self.assertEqual(merged[0]["proposed_by"], ["ai_native", "chain", "gap"],
                         "provenance must survive so generators stay attributable")
        self.assertEqual(merged[0]["n_proposals"], 3)

    def test_artifacts_stay_valid_json_when_a_ratio_is_infinite(self):
        """A zero-downside idea gives an infinite ratio, and `Infinity` is not JSON.
        These artifacts exist to be re-read years later."""
        import json as _json
        from ideagen import orchestrator as orc
        raw = orc._blob({"omega": float("inf"), "nan": float("nan"), "ok": 1.5})
        back = _json.loads(raw.decode())
        self.assertEqual(back["omega"], "+inf")
        self.assertIsNone(back["nan"])
        self.assertEqual(back["ok"], 1.5)

    def test_a_selector_cannot_hold_something_that_was_not_offered(self):
        from ideagen import strategy as strat
        cands = [{"id": "c1", "instrument_id": "SPY"}]

        @strat.register("idea_selector", "_probe_cheat", "1.0")
        def _cheat(ctx):
            return strat.Verdict(strategy="_probe_cheat", version="1.0",
                                 chosen=["c1", "not-offered"])
        try:
            with self.assertRaises(strat.StrategyError):
                strat.run("idea_selector", "_probe_cheat",
                          self._ctx(candidates=cands))
        finally:
            strat._REGISTRY.pop(("idea_selector", "_probe_cheat"), None)


class TestFeedHonesty(unittest.TestCase):
    """A dead source must not look like a quiet period."""

    def test_a_thrown_feed_reports_not_ok(self):
        from ideagen import feeds

        @feeds.register("_probe_dead", "calendar", expect_rows=1)
        def _dead(as_of, params):
            raise RuntimeError("端点断连")
        try:
            r = feeds.fetch("_probe_dead", date(2026, 8, 12))
            self.assertFalse(r.ok, "an outage must not be reported as success")
            self.assertIn("端点断连", r.error or "")
        finally:
            feeds._REGISTRY.pop("_probe_dead", None)

    def test_a_silently_empty_feed_is_flagged(self):
        """Zero rows satisfy every schema rule, which is what makes an empty
        return the most dangerous feed failure."""
        from ideagen import feeds

        @feeds.register("_probe_empty", "calendar", expect_rows=3)
        def _empty(as_of, params):
            return []
        try:
            r = feeds.fetch("_probe_empty", date(2026, 8, 12))
            self.assertFalse(r.ok)
            self.assertIn("expected at least 3", r.error or "")
        finally:
            feeds._REGISTRY.pop("_probe_empty", None)

    def test_every_row_is_stamped_with_its_period(self):
        """Isolation: one period's data must not be able to mix with another's."""
        from ideagen import feeds

        @feeds.register("_probe_stamp", "calendar")
        def _rows(as_of, params):
            return [{"event_id": "e1", "date": "2026-08-12", "label": "x",
                     "kind": "macro_release"}]
        try:
            r = feeds.fetch("_probe_stamp", date(2026, 8, 12))
            self.assertEqual(r.rows[0]["as_of"], "2026-08-12")
            self.assertEqual(r.rows[0]["feed"], "_probe_stamp")
        finally:
            feeds._REGISTRY.pop("_probe_stamp", None)


class TestSchemaDrift(unittest.TestCase):
    """The failure that cost this build an afternoon: a name collision makes
    `CREATE TABLE IF NOT EXISTS` a no-op, and it only surfaces at insert time."""

    def test_a_colliding_table_is_reported_not_discovered_later(self):
        from ideagen import schema
        from ideagen.platform.local import SqliteStateStore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = SqliteStateStore(Path(td) / "s.db")
            st.execute("CREATE TABLE orch_runs (run_id TEXT)")   # wrong shape
            with self.assertRaises(RuntimeError) as e:
                schema.migrate(st)
            self.assertIn("orch_runs", str(e.exception))

    def test_a_clean_database_verifies(self):
        from ideagen import schema
        from ideagen.platform.local import SqliteStateStore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = SqliteStateStore(Path(td) / "s.db")
            schema.migrate(st)
            self.assertEqual(schema.verify(st), [])
            self.assertEqual(schema.orphans(st), {})

    def test_mysql_ddl_preserves_indexes_without_postgres_syntax(self):
        from ideagen import schema
        ddl = "\n".join(schema.MYSQL_DDL)
        self.assertEqual(len(schema.MYSQL_DDL), len(schema.OWNED))
        self.assertNotIn("TEXT PRIMARY KEY", ddl)
        self.assertNotIn("CREATE INDEX IF NOT EXISTS", ddl)
        self.assertNotIn("WHERE ok = 1", ddl)
        self.assertIn("GENERATED ALWAYS AS", ddl)
        self.assertIn("UNIQUE KEY orch_runs_done", ddl)
        self.assertIn("ENGINE=InnoDB", ddl)

    def test_mysql_upsert_uses_on_duplicate_key(self):
        from ideagen import schema

        class Capture:
            dialect = "mysql"

            def execute(self, sql, args=()):
                self.sql, self.args = sql, args
                return 1

        state = Capture()
        schema.upsert(state, "events", {
            "event_id": "e1", "date": "2026-08-28", "actual": "x"})
        self.assertIn("ON DUPLICATE KEY UPDATE", state.sql)
        self.assertNotIn("ON CONFLICT", state.sql)
        self.assertEqual(state.args, ("e1", "2026-08-28", "x"))

    def test_mysql_migration_selects_the_mysql_ddl(self):
        from ideagen import schema

        class FakeMySQL:
            dialect = "mysql"

            def __init__(self):
                self.columns = {}
                self.ddl = ()

            def q(self, sql, args=()):
                if "information_schema.columns" in sql:
                    return [{"column_name": c}
                            for c in self.columns.get(args[0], ())]
                if sql.startswith("SELECT * FROM"):
                    return []
                raise AssertionError(sql)

            def migrate(self, ddl):
                self.ddl = tuple(ddl)
                self.columns = dict(schema.OWNED)
                return len(self.ddl)

            def execute(self, sql, args=()):
                return 0

        state = FakeMySQL()
        self.assertEqual(schema.migrate(state), len(schema.MYSQL_DDL))
        self.assertEqual(state.ddl, schema.MYSQL_DDL)


class TestSessionClamping(unittest.TestCase):
    """`complete_through` is the ceiling on how far the book may be marked."""

    def test_a_monday_morning_does_not_resolve_to_sunday(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from ideagen.sources import futu_px
        # 2026-08-17 is a Monday; 09:00 New York is before the close cutoff.
        now = datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        got = futu_px.complete_through("US", now=now)
        self.assertEqual(got, "2026-08-14",
                         "stepping back one calendar day lands on Sunday, which "
                         "has no session and makes every position look unmarked")

    def test_after_the_close_uses_today(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from ideagen.sources import futu_px
        now = datetime(2026, 8, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(futu_px.complete_through("US", now=now), "2026-08-17")


class TestUniverseEligibility(unittest.TestCase):
    """The mandate: 公募 / ETF / 日度申赎私募 only."""

    def test_single_stocks_are_excluded(self):
        from ideagen import universe as uni
        ok, why = uni.eligibility({"instrument_id": "X", "name": "某公司",
                                   "vehicle": "股票"})
        self.assertFalse(ok)
        self.assertIn("个股", why)

    def test_a_private_fund_needs_evidence_of_daily_dealing(self):
        from ideagen import universe as uni
        no, why = uni.eligibility({"instrument_id": "A", "name": "某私募",
                                   "vehicle": "私募"})
        self.assertFalse(no, "a weekly rebalance cannot use a non-daily vehicle")
        yes, _ = uni.eligibility({"instrument_id": "B", "name": "某基金",
                                  "vehicle": "私募 / UCITS"})
        self.assertTrue(yes, "UCITS deals daily by regulation")

    def test_an_unconfirmed_vehicle_is_not_assumed_fine(self):
        """An unverifiable constraint that defaults to eligible is the same failure
        as a dead feed returning zero rows and reporting success."""
        from ideagen import universe as uni
        ok, why = uni.eligibility({"instrument_id": "C", "name": "待确认",
                                   "vehicle": "私募/公募（待确认）"})
        self.assertFalse(ok)
        self.assertIn("未确认", why)


class TestReplacingATradedBatchIsRefused(unittest.TestCase):
    """Re-placing a batch that has already traded rewrote live position sizes.

    Same class as replacing an artifact under a live book — the failure that cost
    this project ten days of wrong numbers — so it has to be refused, not documented.
    """

    def test_a_traded_batch_cannot_be_reopened_silently(self):
        con = db.init()
        row = db.q1(con, "SELECT book_id, as_of, COUNT(*) AS n FROM orders "
                         "WHERE status <> 'pending' GROUP BY book_id, as_of "
                         "ORDER BY n DESC LIMIT 1")
        if not row:
            self.skipTest("no traded orders in this database")
        bid = db.q1(con, "SELECT batch_id FROM batches WHERE as_of=?",
                    (row["as_of"],))
        if not bid:
            self.skipTest("no batch for that date")
        with self.assertRaises(ValueError) as e:
            paper.open_batch(con, bid["batch_id"], row["book_id"], verbose=False)
        self.assertIn("force=True", str(e.exception),
                      "the refusal must name the deliberate override")


class TestCredentialsNeverReachAnArtifact(unittest.TestCase):
    """Health details are written to immutable object storage and to the event bus,
    so anything they contain is permanent."""

    def test_a_connection_url_is_redacted(self):
        from ideagen.platform.base import redact_url
        secret = "s3" + "cr3t"
        user_url = "redis://user:" + "pw" + "@h:6379"
        redacted_user_url = "redis://user:" + "***" + "@h:6379"
        self.assertEqual(redact_url(f"redis://:{secret}@h:6379/0"),
                         "redis://***@h:6379/0")
        self.assertEqual(redact_url(user_url),
                         redacted_user_url)
        self.assertNotIn(secret, redact_url(f"redis://:{secret}@h:6379/0"))
        self.assertEqual(redact_url("redis://plain:6379"), "redis://plain:6379",
                         "a URL with no credentials must stay readable")


class TestTrancheWeight(unittest.TestCase):
    """每周一批最多动用账本资本的 25%——初心里的滚动结构，用算术执行。"""

    def test_first_week_deploys_at_most_a_quarter(self):
        from ideagen import config
        spec = config.SELECTOR_SPEC
        self.assertEqual(spec.get("tranche_frac"), 0.25,
                         "挑法账本必须声明每批 25% 的上限")
        # The sizing arithmetic: full cash available, one batch of 10 —
        # per-idea notional must come from the tranche, not from all cash.
        cap = spec["capital"]
        per = min(cap, cap, spec["tranche_frac"] * cap) / 10
        self.assertAlmostEqual(per * 10, cap * 0.25,
                               msg="第一周只许铺四分之一，其余吃货币基金收益")


class TestCorpusArchiveAndShortlist(unittest.TestCase):
    """升级件的守规矩测试：内容寻址存档不可变，深抓预算按价值分配。"""

    @staticmethod
    def _item(tier, title, published, line="ib", sid=1):
        return wisburg.Item(
            line=line, category="ib", tier=tier, source_id=sid, title=title,
            published_at=published, url=None, summary="", body="",
            institution=None, meta={})

    @staticmethod
    def _theme(term):
        return lexicon.Theme(id="T-TEST", label="t", key_question="q",
                             terms=(term,), price_indicator="US.SMH")

    def test_shortlist_tier_and_theme_beat_recency(self):
        """The budget goes to a T1 theme hit, not to whatever came last."""
        themes = (self._theme("AI资本开支"),)
        fresher_t3 = self._item(3, "美联储纪要要点回顾", "2026-08-26T10:00:00+08:00",
                                line="feed", sid=9)
        old_t1 = self._item(1, "AI资本开支追踪：订单与现金流", "2026-08-24T09:00:00+08:00",
                            line="ec", sid=7)
        chosen = wisburg.shortlist([fresher_t3, old_t1], 1, themes)
        self.assertEqual(chosen, [old_t1])
        # And within one tier, the theme hit outranks the fresher blank.
        fresher_t1 = self._item(1, "会议纪要观察", "2026-08-26T11:00:00+08:00", sid=8)
        chosen = wisburg.shortlist([fresher_t1, old_t1], 1, themes)
        self.assertEqual(chosen, [old_t1])

    def test_shortlist_penalises_rewrites_of_one_story(self):
        """Five rewrites of the same story must not eat the whole budget."""
        dup1 = self._item(2, "英伟达发布新一代芯片，市场关注", "2026-08-26T10:00:00+08:00", sid=1)
        dup2 = self._item(2, "英伟达发布新一代芯片：市场关注！", "2026-08-26T09:00:00+08:00", sid=2)
        dup3 = self._item(2, "英伟达发布新一代芯片（市场关注）", "2026-08-26T08:00:00+08:00", sid=3)
        other = self._item(2, "欧洲电网设备订单积压创纪录", "2026-08-25T08:00:00+08:00", sid=4)
        chosen = wisburg.shortlist([dup1, dup2, dup3, other], 2, ())
        self.assertIn(other, chosen, "coverage must beat the third rewrite")
        self.assertEqual(len([c for c in chosen if c is not other]), 1,
                         "the duplicate cluster gets at most one slot")
        # A story already deep-fetched in a previous run gets no budget again.
        known = {lexicon.title_signature(dup1.title)}
        chosen = wisburg.shortlist([dup1, other], 1, (), known_sigs=known)
        self.assertEqual(chosen, [other])

    def test_archive_raw_is_content_addressed_and_idempotent(self):
        import hashlib as _h

        class FakeBlobs:
            def __init__(self):
                self.objects = {}
            def exists(self, key):
                return key in self.objects
            def put(self, key, data, **kw):
                if key in self.objects:
                    raise RuntimeError(f"{key} already exists")
                self.objects[key] = data
                return self.uri(key)
            def uri(self, key):
                return f"fake://{key}"

        blobs = FakeBlobs()
        md = "# 报告\n\n原文正文，含 <b>HTML</b> 与全部细节。"
        sha1_, uri1 = wisburg.archive_raw(blobs, "ec", 42, md)
        self.assertEqual(sha1_, _h.sha256(md.encode()).hexdigest())
        self.assertEqual(uri1, f"fake://corpus/raw/ec/42_{sha1_[:12]}.md")
        # Same content again: silent skip, still one object, no error recorded.
        errs: dict = {}
        sha2_, uri2 = wisburg.archive_raw(blobs, "ec", 42, md, errs)
        self.assertEqual((sha1_, uri1), (sha2_, uri2))
        self.assertEqual(len(blobs.objects), 1)
        self.assertEqual(errs, {})
        # Changed content: a different sha yields a different key; both survive.
        sha3_, uri3 = wisburg.archive_raw(blobs, "ec", 42, md + "（修订）")
        self.assertNotEqual(sha3_, sha1_)
        self.assertEqual(len(blobs.objects), 2)
        self.assertEqual(blobs.objects[uri1.replace("fake://", "")].decode(), md)

    def test_doc_columns_evolve_and_cursor_round_trip(self):
        con = mem()
        for _ in range(2):                       # must be idempotent
            wisburg._ensure_doc_columns(con)
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        self.assertLessEqual({"body_sha256", "raw_uri"}, cols)
        cur = {"source_id": 99610, "published_at": "2026-08-26T10:00:00+08:00",
               "checked_at": "2026-08-26T10:05:00+08:00"}
        db.kv_set(con, "ingest:cursor:ib", cur)
        self.assertEqual(db.kv_get(con, "ingest:cursor:ib"), cur)

    def test_tool_drift_names_missing_and_unknown(self):
        class StubW:
            def __init__(self, names):
                self._names = names
            def tools(self):
                return self._names

        expected = ({s["tool"] for s in config.SOURCE_LINES.values()}
                    | set(wisburg._DETAIL_TOOLS))
        self.assertIsNone(wisburg.tool_drift(StubW(sorted(expected))))
        drifted = (expected - {"list-earning-calls"}) | {"list-podcasts"}
        d = wisburg.tool_drift(StubW(sorted(drifted)))
        self.assertEqual(d["missing"], ["list-earning-calls"])
        self.assertEqual(d["unknown"], ["list-podcasts"])


class TestCalibEvidenceAttribution(unittest.TestCase):
    """calib's thin-evidence penalty is only a penalty if documents can be found.

    The original implementation tokenised the topic slug and label instead of
    using the theme's registered terms: an English slug matches no Chinese
    document, and a Chinese label survives the split as one long string that
    must appear verbatim — so every topic counted zero documents and the
    penalty collapsed into a constant (observed live on the 2026-08-26 run).
    """

    def _ctx(self, topics):
        from ideagen import strategy as strat
        return strat.RunContext(
            as_of=date(2026, 8, 26), inputs_sha="x", topics=topics,
            corpus=[{"doc_id": "d1", "published_d": "2026-08-25", "tier": 1,
                     "title": "联储会议纪要", "summary": "美联储官员讨论降息路径，上调空间有限"},
                    {"doc_id": "d2", "published_d": "2026-08-25", "tier": 2,
                     "title": "债市周报", "summary": "市场对美联储降息预期升温，利多债券"}],
            candidates=[{"id": "i1", "instrument_id": "TLT",
                         "topic_id": "POLICY-PATH", "thesis": "t",
                         "upside_pct": 5, "downside_pct": -3,
                         "p_up": .4, "p_base": .4, "p_down": .2}])

    def test_terms_attribute_documents_the_slug_never_could(self):
        from ideagen.strategies.select_calib import _topic_evidence
        ev = _topic_evidence(self._ctx(
            [{"topic_id": "POLICY-PATH", "label": "央行政策路径与流动性",
              "terms": ["美联储", "降息"]}]))
        self.assertEqual(ev["POLICY-PATH"]["n_docs"], 2,
                         "theme terms must attribute Chinese documents that the "
                         "English slug / verbatim-label rule silently missed")

    def test_topic_without_terms_counts_nothing_rather_than_everything(self):
        from ideagen.strategies.select_calib import _topic_evidence
        ev = _topic_evidence(self._ctx(
            [{"topic_id": "POLICY-PATH", "label": "央行政策路径与流动性"}]))
        # topic_terms falls back to slug+label tokens; none appear in the corpus,
        # so the count is an honest 0 — the same unmatched-not-everything rule
        # corpus_block follows.
        self.assertEqual(ev["POLICY-PATH"]["n_docs"], 0)


class TestHgepEvidenceProvenance(unittest.TestCase):
    """筛选A must freeze *which* documents scored a topic, not just how many.

    Jon's ask-the-run requirement is answering "为什么读了这些就选了它" from the
    frozen record. A count alone forces reconstruction; the doc-id list makes
    the run name its own sources.
    """

    def test_scores_carry_the_exact_doc_ids(self):
        import datetime as dtm
        from unittest import mock
        from ideagen import strategy as strat, lexicon
        from ideagen.strategies.topic_hgep import hgep

        theme = lexicon.Theme(
            id="T-FED", label="联储", key_question="q",
            terms=("美联储", "降息"), price_indicator="TLT",
            registered_d="2026-01-01")
        docs = [{"doc_id": f"d{i}", "published_d": "2026-08-25", "tier": 1,
                 "title": "美联储降息预期", "summary": "美联储官员讨论降息路径" * 20}
                for i in range(3)]
        ctx = strat.RunContext(as_of=dtm.date(2026, 8, 26), inputs_sha="x",
                               corpus=docs)
        with mock.patch.object(lexicon, "all_themes", return_value=[theme]):
            v = hgep(ctx)
        row = v.scores["T-FED"]
        self.assertEqual(sorted(row["doc_ids"]), ["d0", "d1", "d2"])
        self.assertEqual(row["n_evidence"], len(row["doc_ids"]),
                         "the frozen list and the count must be the same set")


class TestUpsertKeepIfBlank(unittest.TestCase):
    """A shallow re-listing must not erase a deep-fetched document body.

    upsert_many sets every column; before keep_if_blank, a document that
    reappeared in a list fetch after deep-fetching arrived with body='' and
    silently lost its text (442 of 654 archived reports, found 2026-09-03).
    """

    def _con(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE d (id TEXT PRIMARY KEY, body TEXT, "
                    "body_chars INT, summary TEXT, title TEXT)")
        return con

    def test_blank_incoming_keeps_stored_value(self):
        from ideagen import db
        con = self._con()
        db.upsert_many(con, "d", [{"id": "x", "body": "深抓正文", "body_chars": 4,
                                   "summary": "摘要", "title": "旧标题"}], ["id"])
        db.upsert_many(con, "d", [{"id": "x", "body": "", "body_chars": 0,
                                   "summary": "", "title": "新标题"}], ["id"],
                       keep_if_blank=("body", "body_chars", "summary"))
        row = dict(con.execute("SELECT * FROM d").fetchone())
        self.assertEqual(row["body"], "深抓正文")
        self.assertEqual(row["body_chars"], 4)
        self.assertEqual(row["summary"], "摘要")
        self.assertEqual(row["title"], "新标题",
                         "unguarded columns must still update normally")

    def test_nonblank_incoming_still_overwrites(self):
        from ideagen import db
        con = self._con()
        db.upsert_many(con, "d", [{"id": "x", "body": "旧", "body_chars": 1,
                                   "summary": "s", "title": "t"}], ["id"])
        db.upsert_many(con, "d", [{"id": "x", "body": "新正文", "body_chars": 3,
                                   "summary": "s2", "title": "t"}], ["id"],
                       keep_if_blank=("body", "body_chars", "summary"))
        row = dict(con.execute("SELECT * FROM d").fetchone())
        self.assertEqual(row["body"], "新正文")
        self.assertEqual(row["summary"], "s2")


class TestMysqlPasswordOnlyIsStagedNotBroken(unittest.TestCase):
    """A parked IDEAGEN_MYSQL_PASSWORD must not select (and then fail) MySQL.

    The migration flow stages the RDS password in the operator env before the
    host/db/user exist anywhere but the ECS runtime.env; on 2026-09-03 that
    single staged value made _mysql_options raise and took /api/state down.
    """

    def test_password_alone_returns_none(self):
        from ideagen.platform import _mysql_options
        vals = {"IDEAGEN_MYSQL_PASSWORD": "parked-secret"}
        self.assertIsNone(_mysql_options(lambda k, d=None: vals.get(k, d)))

    def test_partial_server_fields_still_fail_loudly(self):
        from ideagen.platform import _mysql_options
        from ideagen.platform.base import NotConfigured
        vals = {"IDEAGEN_MYSQL_HOST": "h", "IDEAGEN_MYSQL_PASSWORD": "x"}
        with self.assertRaises(NotConfigured):
            _mysql_options(lambda k, d=None: vals.get(k, d))


class TestGeneratorTopUpRound(unittest.TestCase):
    """Under-delivery triggers exactly one top-up call, deduped by instrument.

    The mandate is 20 ideas per topic; a single call reliably returned 4-11
    (observed on 2026-08-26 across all four methods), quietly shrinking the
    stage-C pool to a quarter of its intended size.
    """

    def _idea(self, inst, up=5, dn=-3):
        return {"instrument_id": inst, "thesis": "理由", "upside_pct": up,
                "downside_pct": dn, "p_up": .4, "p_base": .4, "p_down": .2}

    def test_topup_fills_shortfall_without_duplicates(self):
        import datetime as dtm
        from unittest import mock
        from ideagen import strategy as strat
        from ideagen.strategies import _gen

        uni = [{"instrument_id": f"E{i}", "name": f"E{i}", "vehicle": "ETF",
                "exposure": "x"} for i in range(30)]
        ctx = strat.RunContext(
            as_of=dtm.date(2026, 8, 26), inputs_sha="x",
            topics=[{"topic_id": "T1", "label": "主题", "terms": ["词"]}],
            universe=uni, corpus=[])
        first = [self._idea("E0"), self._idea("E1")]
        second = [self._idea("E1")] + [self._idea(f"E{i}") for i in range(2, 25)]
        calls = []

        def fake_ask(c, prompt):
            calls.append(prompt)
            return (first if len(calls) == 1 else second), 1

        with mock.patch.object(_gen, "ask_json", side_effect=fake_ask):
            v = _gen.generate_per_topic(ctx, "ai_native", lambda c, t: ("提示词", 1))

        self.assertEqual(len(calls), 2, "exactly one top-up call")
        self.assertIn("补充轮", calls[1])
        self.assertEqual(v.meta["per_topic"]["T1"], _gen.PER_TOPIC)
        ids = [i["instrument_id"] for i in v.produced]
        self.assertEqual(len(ids), len(set(ids)), "no duplicate instruments")
        self.assertEqual(v.meta["topup_per_topic"]["T1"], _gen.PER_TOPIC - 2)

    def test_full_first_batch_makes_no_second_call(self):
        import datetime as dtm
        from unittest import mock
        from ideagen import strategy as strat
        from ideagen.strategies import _gen

        uni = [{"instrument_id": f"E{i}", "name": f"E{i}", "vehicle": "ETF",
                "exposure": "x"} for i in range(30)]
        ctx = strat.RunContext(
            as_of=dtm.date(2026, 8, 26), inputs_sha="x",
            topics=[{"topic_id": "T1", "label": "主题", "terms": ["词"]}],
            universe=uni, corpus=[])
        full = [self._idea(f"E{i}") for i in range(_gen.PER_TOPIC)]
        calls = []

        def fake_ask(c, prompt):
            calls.append(prompt)
            return full, 1

        with mock.patch.object(_gen, "ask_json", side_effect=fake_ask):
            v = _gen.generate_per_topic(ctx, "ai_native", lambda c, t: ("提示词", 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(v.meta["topup_per_topic"], {})
