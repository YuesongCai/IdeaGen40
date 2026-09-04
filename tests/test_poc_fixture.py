from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen import (cloud_corpus, cloud_paper, db, poc_fixture, poc_workflow,
                     review, scheduler, schema, shelf_store, strategy)
from ideagen.platform.base import Platform, Unavailable
from ideagen.platform.byteplus import MySQLStateStore, PostgresStateStore
from ideagen.platform.local import (FileCache, FileEventBus, LocalBlobStore,
                                    SqliteStateStore)
from ideagen.serve import Handler, Server


class _Cache:
    def get(self, key: str):
        return None


def _platform(root: Path, *, state_path: Path | None = None) -> Platform:
    return Platform(
        name="test",
        blobs=LocalBlobStore(root / "blobs"),
        state=SqliteStateStore(state_path or root / "state.db"),
        inference=Unavailable("inference", "not used"),
        events=FileEventBus(root / "events.jsonl"),
        cache=FileCache(root / "cache"),
        secrets=Unavailable("secrets", "not used"),
    )


def _save_weekly(state, *, run_id: str, as_of: date,
                 classification: str,
                 instruments: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    schema.upsert(state, "orch_runs", {
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "kind": "weekly",
        "platform": "test",
        "started_at": now,
        "ended_at": now,
        "ok": 1,
        "error": None,
        "inputs_sha": "test-inputs",
        "journal_uri": "file://test-journal",
        "calls": 1,
        "data_classification": classification,
    })
    chosen = []
    for index, instrument_id in enumerate(instruments):
        candidate_id = f"candidate-{index}"
        chosen.append(candidate_id)
        candidate = {
            "id": candidate_id,
            "instrument_id": instrument_id,
            "instrument_name": f"Instrument {index}",
            "topic_id": "TEST-TOPIC",
            "method": "test-generator",
            "thesis": f"private thesis for {instrument_id}",
            "upside_pct": 8.0,
            "downside_pct": -4.0,
            "p_up": 0.4,
            "p_base": 0.4,
            "p_down": 0.2,
        }
        schema.upsert(state, "candidates", {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "as_of": as_of.isoformat(),
            "instrument_id": instrument_id,
            "topic_id": "TEST-TOPIC",
            "method": "test-generator",
            "payload": json.dumps(candidate),
        })
    schema.upsert(state, "verdicts", {
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "kind": "idea_selector",
        "strategy": "buy_all",
        "version": "test",
        "role": "test",
        "inputs_sha": "test-inputs",
        "chosen": json.dumps(chosen),
        "scores": "{}",
        "rejected": "[]",
        "meta": "{}",
        "calls": 0,
    })


class TestPublicPocFixture(unittest.TestCase):
    def test_default_fixture_is_explicitly_public_and_synthetic(self):
        fixture = poc_fixture.read()
        self.assertEqual(fixture.document["metadata"]["classification"],
                         "public-synthetic")
        self.assertTrue(fixture.document["metadata"]["synthetic"])
        text = fixture.raw.decode().lower()
        self.assertNotIn("olive", text)
        self.assertNotIn("wisburg", text)
        self.assertNotIn("account_id", text)

    def test_publish_and_state_import_are_idempotent(self):
        fixture = poc_fixture.read()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blobs = LocalBlobStore(root / "blobs")
            state = SqliteStateStore(root / "state.db")

            first_uri = poc_fixture.publish(blobs, fixture)
            second_uri = poc_fixture.publish(blobs, fixture)
            self.assertEqual(first_uri, second_uri)
            self.assertEqual(blobs.get(fixture.object_key), fixture.raw)

            first = poc_fixture.write_state(state, fixture)
            second = poc_fixture.write_state(state, fixture)
            self.assertEqual(first, second)
            self.assertEqual(first["orch_runs"], 1)
            self.assertEqual(first["feed_runs"], 3)
            self.assertGreaterEqual(first["verdicts"], 3)
            self.assertGreaterEqual(first["candidates"], 3)

    def test_imported_fixture_populates_dashboard_state(self):
        fixture = poc_fixture.read()
        with tempfile.TemporaryDirectory() as td:
            state = SqliteStateStore(Path(td) / "state.db")
            poc_fixture.write_state(state, fixture)
            platform = SimpleNamespace(
                name="test",
                state=state,
                cache=_Cache(),
                check=lambda: [],
            )
            payload = review.state(con=db.init(":memory:"), p=platform)

        self.assertTrue(payload["runs"])
        self.assertEqual(payload["weekly"]["data_classification"],
                         "public-synthetic")
        self.assertTrue(payload["weekly"]["topics"])
        self.assertTrue(payload["weekly"]["pool"]["candidates"])
        self.assertTrue(payload["weekly"]["selectors"])
        self.assertTrue(payload["feeds"])
        json.dumps(payload, ensure_ascii=False, allow_nan=False)

    def test_selector_cannot_reference_an_unknown_candidate(self):
        fixture = poc_fixture.read()
        broken = json.loads(json.dumps(fixture.document))
        selector = next(v for v in broken["tables"]["verdicts"]
                        if v["kind"] == "idea_selector")
        selector["chosen"].append("NOT-IN-POOL")
        with self.assertRaisesRegex(ValueError, "unknown candidates"):
            poc_fixture.validate(broken)

    def test_cloud_sql_translation_escapes_like_wildcards(self):
        sql = "SELECT '?' AS literal WHERE run_id LIKE 'gap-%' AND run_id=?"
        expected = "SELECT '?' AS literal WHERE run_id LIKE 'gap-%%' AND run_id=%s"
        self.assertEqual(MySQLStateStore._sql(sql), expected)
        self.assertEqual(PostgresStateStore._sql(sql), expected)

    def test_http_root_serves_the_live_state_dashboard(self):
        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/",
                timeout=3).read().decode()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertIn("<title>IdeaGen40 · 运行台</title>", body)
        self.assertIn("fetch('/api/state'", body)

    def test_dashboard_uses_a_single_backtest_date_picker_without_data_banners(self):
        root = Path(__file__).resolve().parent.parent
        dashboard = (root / "web" / "dash.html").read_text(encoding="utf-8")
        olive_page = (root / "web" / "olive.html").read_text(encoding="utf-8")

        self.assertEqual(dashboard.count('id="backtestStart"'), 1)
        self.assertIn('type="date"', dashboard)
        self.assertIn("setBacktestStart", dashboard)
        self.assertIn("backtestArmMetrics", dashboard)
        self.assertIn("backtestPairedSummary", dashboard)
        # The feed-labelling helper has been refactored (corpusFeed.feed →
        # keyed lookup); the property under test is unchanged: the dashboard
        # recognises the live wisburg-mcp feed and labels it as licensed data.
        self.assertIn("String(key).indexOf('wisburg-mcp')", dashboard)
        self.assertIn("key==='wisburg-mcp'", dashboard)
        self.assertIn("function currentBooks()", dashboard)
        # The race compares ten books over time, so every book stays in the
        # chart whether or not it added to its position this period. The old
        # assertion pinned a filter that compared booked_batch (a batch id)
        # against a run id — never equal, so it silently did nothing and the
        # fallback carried the page. `bookedThisPeriod` is the honest form of
        # the question it was trying to ask.
        self.assertIn("function bookedThisPeriod(", dashboard)
        self.assertNotIn("b.booked_batch===rid", dashboard)
        self.assertIn("w.as_of===asOf&&w.corpus_total", dashboard)
        for phrase in ("公开合成", "合成演示", "机械回放",
                       "公开 synthetic", "synthetic fixture"):
            self.assertNotIn(phrase, dashboard)
            self.assertNotIn(phrase, olive_page)

    @mock.patch.dict(os.environ, {"IDEAGEN_DASH_KEY": "test-dash-secret"})
    def test_query_key_redirects_to_a_clean_url_without_logging_secret(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        output = io.StringIO()
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/"
                "?view=weekly&key=test-dash-secret",
                headers={
                    "X-Forwarded-For": "203.0.113.10",
                    "X-Forwarded-Proto": "https",
                },
            )
            with redirect_stdout(output):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.build_opener(NoRedirect).open(
                        request, timeout=3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        response = raised.exception
        self.assertEqual(response.code, 303)
        self.assertEqual(response.headers["Location"], "/?view=weekly")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertNotIn("test-dash-secret", output.getvalue())

    @mock.patch.dict(os.environ, {
        "IDEAGEN_DASH_KEY": "test-dash-secret",
        "IDEAGEN_PUBLIC_SITE": "https://stale.example.com",
    })
    def test_olive_page_is_on_demand_and_start_is_same_origin_only(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        cookie = "dashkey=test-dash-secret"
        try:
            page = urllib.request.Request(
                base + "/olive",
                headers={
                    "Cookie": cookie,
                    "X-Forwarded-For": "203.0.113.10",
                },
            )
            body = urllib.request.urlopen(page, timeout=3).read().decode()
            self.assertIn("Olive MCP", body)
            self.assertIn("点击授权后", body)
            self.assertIn('method="get" action="/api/olive/oauth/start"', body)

            get_start = urllib.request.Request(
                base + "/api/olive/oauth/start",
                headers={
                    "Cookie": cookie,
                    "X-Forwarded-For": "203.0.113.10",
                    "X-Forwarded-Host": "dashboard.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            with mock.patch(
                    "ideagen.olive_web.begin_authorization",
                    return_value="https://noahsso.example/authorize") as begin:
                with self.assertRaises(urllib.error.HTTPError) as redirected:
                    urllib.request.build_opener(NoRedirect).open(
                        get_start, timeout=3)
            self.assertEqual(redirected.exception.code, 303)
            begin.assert_called_once_with("https://dashboard.example.com")

            cross_origin = urllib.request.Request(
                base + "/api/olive/oauth/start",
                method="POST",
                data=b"",
                headers={
                    "Cookie": cookie,
                    "Origin": "https://attacker.example",
                    "X-Forwarded-For": "203.0.113.10",
                    "X-Forwarded-Host": "dashboard.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(cross_origin, timeout=3)
            self.assertEqual(raised.exception.code, 403)

            same_origin = urllib.request.Request(
                base + "/api/olive/oauth/start",
                method="POST",
                data=b"",
                headers={
                    "Cookie": cookie,
                    "Origin": "https://dashboard.example.com",
                    "X-Forwarded-For": "203.0.113.10",
                    "X-Forwarded-Host": "dashboard.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            with mock.patch(
                    "ideagen.olive_web.begin_authorization",
                    return_value="https://noahsso.example/authorize") as begin:
                with self.assertRaises(urllib.error.HTTPError) as redirected:
                    urllib.request.build_opener(NoRedirect).open(
                        same_origin, timeout=3)
            self.assertEqual(redirected.exception.code, 303)
            self.assertEqual(
                redirected.exception.headers["Location"],
                "https://noahsso.example/authorize",
            )
            begin.assert_called_once_with("https://dashboard.example.com")

            opaque_origin = urllib.request.Request(
                base + "/api/olive/oauth/start",
                method="POST",
                data=b"",
                headers={
                    "Cookie": cookie,
                    "Origin": "null",
                    "Sec-Fetch-Site": "same-origin",
                    "X-Forwarded-For": "203.0.113.10",
                    "X-Forwarded-Host": "dashboard.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            with mock.patch(
                    "ideagen.olive_web.begin_authorization",
                    return_value="https://noahsso.example/authorize"):
                with self.assertRaises(urllib.error.HTTPError) as redirected:
                    urllib.request.build_opener(NoRedirect).open(
                        opaque_origin, timeout=3)
            self.assertEqual(redirected.exception.code, 303)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_ecs_runtime_config_is_podman_safe_and_redacts_headers(self):
        root = Path(__file__).resolve().parent.parent
        compose = (root / "deploy" / "compose.yaml").read_text()
        caddy = (root / "deploy" / "Caddyfile").read_text()
        self.assertIn('test: ["CMD-SHELL"', compose)
        self.assertNotIn('test: ["CMD", "python3", "-c"', compose)
        # Every service restarts, checked per service rather than by counting
        # them. The count broke the moment a fourth service was added, which
        # taught nobody anything: what matters is that no service was left
        # without a restart policy, not how many services there are.
        import yaml as _yaml
        services = _yaml.safe_load(compose)["services"]
        for name, spec in services.items():
            self.assertEqual(spec.get("restart"), "always",
                             f"{name} 没有 restart: always")
        self.assertIn("scheduler:", compose)
        self.assertIn(
            'IDEAGEN_POC_WEEKLY_MODE: '
            '"${IDEAGEN_POC_WEEKLY_MODE:-wisburg-auto}"', compose)
        self.assertIn(
            'IDEAGEN_CLOUD_WISBURG_ENABLED: '
            '"${IDEAGEN_CLOUD_WISBURG_ENABLED:-true}"', compose)
        self.assertIn("IDEAGEN_PUBLIC_SITE", compose)
        self.assertIn("IDEAGEN_DEFAULT_SNI", compose)
        self.assertIn("request>headers delete", caddy)
        self.assertIn("request>uri delete", caddy)
        self.assertIn("{$IDEAGEN_PUBLIC_SITE::80}", caddy)
        self.assertIn("default_sni {$IDEAGEN_DEFAULT_SNI:localhost}", caddy)
        self.assertIn("profile shortlived", caddy)
        self.assertNotIn("IDEAGEN_DASH_KEY=", compose + caddy)

    def test_three_month_backtest_is_zero_model_and_visible_in_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = SqliteStateStore(root / "state.db")
            platform = Platform(
                name="test",
                blobs=LocalBlobStore(root / "blobs"),
                state=state,
                inference=Unavailable("inference", "not used"),
                events=FileEventBus(root / "events.jsonl"),
                cache=FileCache(root / "cache"),
                secrets=Unavailable("secrets", "not used"),
            )
            receipt = poc_workflow.run_backtest(
                date(2026, 8, 30), p=platform, verbose=False)
            repeated = poc_workflow.run_backtest(
                date(2026, 8, 30), p=platform, verbose=False)
            now = datetime.now(timezone.utc).isoformat()
            schema.upsert(state, "orch_runs", {
                "run_id": "mon-test",
                "as_of": "2026-08-30",
                "kind": "monitor",
                "platform": "test",
                "started_at": now,
                "ended_at": now,
                "ok": 1,
                "error": None,
                "inputs_sha": None,
                "journal_uri": None,
                "calls": 0,
                "data_classification": poc_workflow.CLASSIFICATION,
            })
            payload = review.state(
                con=db.init(root / "legacy.db"), p=platform)

        self.assertEqual(receipt["periods"], 13)
        self.assertEqual(repeated, receipt)
        self.assertEqual(receipt["model_calls"], 0)
        self.assertEqual(receipt["points"], 28)
        self.assertEqual(receipt["positions"], 78)
        self.assertEqual(payload["backtest"]["backtest_id"],
                         receipt["backtest_id"])
        self.assertEqual(len(payload["backtest"]["points"]), 28)
        self.assertEqual(len(payload["backtest"]["positions"]), 78)
        self.assertFalse(
            payload["backtest"]["summary"]["predictive_claim"])
        self.assertFalse(
            payload["backtest"]["summary"]["paired"]["conclusive"])
        self.assertEqual(
            payload["alive"]["heartbeat"]["source"], "rds-monitor")
        self.assertTrue(payload["alive"]["ok"])

    def test_backtest_generation_arms_only_choose_their_own_method(self):
        from ideagen.strategy import RunContext

        ctx = RunContext(
            as_of=date(2026, 8, 30),
            inputs_sha="same-pool",
            candidates=[
                {"id": "a", "method": "ai_native"},
                {"id": "b", "method": "carl_constraint"},
            ],
        )
        ai = strategy.run("idea_selector", "generated_ai_native", ctx)
        carl = strategy.run(
            "idea_selector", "generated_carl_constraint", ctx)
        self.assertEqual(ai.chosen, ["a"])
        self.assertEqual(carl.chosen, ["b"])

    def test_injected_weekly_persists_all_three_feed_receipts_and_classification(self):
        from ideagen import orchestrator
        from ideagen.platform.base import Health

        class HealthyPort:
            def __init__(self, name):
                self.name = name

            def check(self):
                return Health(True, self.name, "test")

        @strategy.register(
            "idea_generator", "_poc_fixture_generator", "1.0",
            needs_model=False)
        def generate(ctx):
            topic = ctx.topics[0]["topic_id"]
            instrument = ctx.universe[0]
            candidate = {
                "id": f"probe:{topic}:{instrument['instrument_id']}",
                "instrument_id": instrument["instrument_id"],
                "instrument_name": instrument["name"],
                "topic_id": topic,
                "method": "_poc_fixture_generator",
                "thesis": "Synthetic orchestration contract test.",
                "upside_pct": 5.0,
                "downside_pct": -3.0,
                "p_up": 0.4,
                "p_base": 0.4,
                "p_down": 0.2,
            }
            return strategy.Verdict(
                strategy="_poc_fixture_generator",
                version="1.0",
                chosen=[candidate["id"]],
                produced=[candidate],
            )

        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                state = SqliteStateStore(root / "state.db")
                platform = Platform(
                    name="test",
                    blobs=LocalBlobStore(root / "blobs"),
                    state=state,
                    inference=HealthyPort("inference"),
                    events=FileEventBus(root / "events.jsonl"),
                    cache=FileCache(root / "cache"),
                    secrets=HealthyPort("secrets"),
                )
                inputs = poc_workflow.public_inputs(date(2026, 8, 30))
                result = orchestrator.weekly(
                    as_of=date(2026, 8, 30),
                    p=platform,
                    generators=["_poc_fixture_generator"],
                    selectors=["buy_all"],
                    params={
                        "top_n": 1,
                        "input_source": "public-synthetic-v1",
                        "data_classification":
                            poc_workflow.WEEKLY_CLASSIFICATION,
                        "skip_theme_discovery": True,
                    },
                    verbose=False,
                    **inputs,
                )
                run = state.q(
                    "SELECT data_classification FROM orch_runs "
                    "WHERE run_id=?", (result.run_id,))[0]
                feeds = state.q(
                    "SELECT kind, n_rows FROM feed_runs WHERE run_id=?",
                    (result.run_id,))
        finally:
            strategy._REGISTRY.pop(
                ("idea_generator", "_poc_fixture_generator"), None)

        self.assertTrue(result.completed)
        self.assertEqual(
            run["data_classification"],
            poc_workflow.WEEKLY_CLASSIFICATION)
        self.assertEqual(
            {row["kind"] for row in feeds},
            {"corpus", "universe", "calendar"})
        self.assertEqual(sum(row["n_rows"] for row in feeds), 20)


class TestPortableShelfAndPaper(unittest.TestCase):
    AS_OF = date(2026, 8, 30)

    def test_shelf_fixture_is_idempotent_and_drives_weekly_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            first = shelf_store.persist_fixture(p, self.AS_OF)
            second = shelf_store.persist_fixture(p, self.AS_OF)
            weekly = poc_workflow.weekly_kwargs(
                self.AS_OF, p=p, mode="shelf-fixture")
            dashboard = shelf_store.dashboard_state(
                p.state, as_of=self.AS_OF)

            self.assertEqual(first, second)
            self.assertEqual(
                p.state.q("SELECT COUNT(*) AS n FROM shelf_snapshots")[0]["n"],
                1,
            )
            self.assertEqual(
                p.state.q("SELECT COUNT(*) AS n FROM shelf_instruments")[0]["n"],
                6,
            )
            self.assertEqual(
                p.state.q("SELECT COUNT(*) AS n FROM shelf_navs")[0]["n"],
                6,
            )
            self.assertEqual(len(list(p.blobs.list("shelves/"))), 1)
            self.assertEqual(
                weekly["params"]["data_classification"],
                poc_workflow.SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(len(weekly["universe"]), 6)
            self.assertEqual(
                {row["instrument_id"] for row in weekly["universe"]},
                {row["instrument_id"] for row in
                 poc_fixture.read().document["inputs"]["universe"]},
            )
            self.assertFalse(dashboard["identifiers_redacted"])
            self.assertEqual(
                dashboard["instruments"][0]["instrument"],
                weekly["universe"][0]["instrument_id"],
            )

    def test_olive_auto_keeps_fixture_until_a_live_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            before = poc_workflow.weekly_kwargs(
                self.AS_OF, p=p, mode="olive-auto")
            shelf_store.persist(
                p,
                {"funds": [{
                    "productCode": "PRIVATE-AUTO-1",
                    "productName": "Licensed Auto Fund",
                    "latestNav": 101.25,
                    "navDate": self.AS_OF.isoformat(),
                }]},
                as_of=self.AS_OF,
                source=shelf_store.LIVE_SOURCE,
                classification=shelf_store.LIVE_CLASSIFICATION,
                captured_at="2026-08-30T08:00:00+08:00",
            )
            after = poc_workflow.weekly_kwargs(
                self.AS_OF, p=p, mode="olive-auto")

            self.assertEqual(before["params"]["weekly_mode"], "shelf-fixture")
            self.assertEqual(
                before["params"]["data_classification"],
                poc_workflow.SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(after["params"]["weekly_mode"], "olive-live")
            self.assertEqual(
                after["params"]["data_classification"],
                poc_workflow.OLIVE_LIVE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(
                after["params"]["requested_weekly_mode"], "olive-auto")

    def test_wisburg_auto_requires_persisted_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            with self.assertRaisesRegex(
                    RuntimeError, "no persisted Wisburg corpus"):
                poc_workflow.weekly_kwargs(
                    self.AS_OF, p=p, mode="wisburg-auto")

    def test_wisburg_auto_uses_rds_corpus_and_tracks_fixture_shelf(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            cloud_corpus.persist(
                p,
                [{
                    "doc_id": "wisburg-live-1",
                    "published_d": self.AS_OF.isoformat(),
                    "title": "Real licensed research",
                    "tier": 1,
                    "summary": "Persisted Wisburg projection",
                    "body": "Licensed body",
                }],
                as_of=self.AS_OF,
            )

            weekly = poc_workflow.weekly_kwargs(
                self.AS_OF, p=p, mode="wisburg-auto")

            self.assertEqual(
                [row["doc_id"] for row in weekly["corpus"]],
                ["wisburg-live-1"],
            )
            self.assertEqual(weekly["calendar"], [])
            self.assertEqual(
                weekly["params"]["data_classification"],
                poc_workflow.WISBURG_SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(
                weekly["params"]["input_source"],
                "wisburg-mcp-rds+public-shelf-fixture-v1",
            )
            self.assertEqual(
                weekly["params"]["shelf_mode"], "shelf-fixture")
            self.assertEqual(
                cloud_paper._shelf_classification(
                    weekly["params"]["data_classification"]),
                shelf_store.PUBLIC_FIXTURE_CLASSIFICATION,
            )

    def test_wisburg_auto_switches_to_a_persisted_olive_shelf(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            cloud_corpus.persist(
                p,
                [{
                    "doc_id": "wisburg-live-1",
                    "published_d": self.AS_OF.isoformat(),
                    "title": "Real licensed research",
                    "tier": 1,
                }],
                as_of=self.AS_OF,
            )
            shelf_store.persist(
                p,
                {"funds": [{
                    "productCode": "PRIVATE-WISBURG-1",
                    "productName": "Licensed shelf fund",
                    "latestNav": 101.25,
                    "navDate": self.AS_OF.isoformat(),
                }]},
                as_of=self.AS_OF,
                source=shelf_store.LIVE_SOURCE,
                classification=shelf_store.LIVE_CLASSIFICATION,
                captured_at="2026-08-30T08:00:00+08:00",
            )

            weekly = poc_workflow.weekly_kwargs(
                self.AS_OF, p=p, mode="wisburg-auto")

            self.assertEqual(
                weekly["params"]["data_classification"],
                poc_workflow.WISBURG_OLIVE_LIVE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(weekly["params"]["shelf_mode"], "olive-live")
            self.assertEqual(
                weekly["universe"][0]["instrument_id"],
                "PRIVATE-WISBURG-1",
            )
            self.assertEqual(
                cloud_paper._shelf_classification(
                    weekly["params"]["data_classification"]),
                shelf_store.LIVE_CLASSIFICATION,
            )

    def test_public_and_licensed_navs_are_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            shelf_store.persist_fixture(p, self.AS_OF)
            shelf_store.persist(
                p,
                {
                    "funds": [{
                        "productCode": "PUB-AI-INFRA",
                        "productName": "Licensed Alpha Fund",
                        "currency": "USD",
                        "latestNav": 777.0,
                        "navDate": self.AS_OF.isoformat(),
                    }],
                    "metadata": {
                        "capturedAt": "2026-08-30T08:00:00+08:00",
                    },
                },
                as_of=self.AS_OF,
                source=shelf_store.LIVE_SOURCE,
                classification=shelf_store.LIVE_CLASSIFICATION,
            )
            public_nav = shelf_store.nav_on_or_before(
                p.state,
                "PUB-AI-INFRA",
                self.AS_OF.isoformat(),
                classification=shelf_store.PUBLIC_FIXTURE_CLASSIFICATION,
            )
            licensed_nav = shelf_store.nav_on_or_before(
                p.state,
                "PUB-AI-INFRA",
                self.AS_OF.isoformat(),
                classification=shelf_store.LIVE_CLASSIFICATION,
            )

            self.assertNotEqual(public_nav["nav"], licensed_nav["nav"])
            self.assertEqual(licensed_nav["nav"], 777.0)
            self.assertEqual(
                p.state.q(
                    "SELECT COUNT(*) AS n FROM shelf_navs "
                    "WHERE instrument_id='PUB-AI-INFRA'"
                )[0]["n"],
                2,
            )

    def test_paper_booking_survives_restart_and_closes_at_future_nav(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "state.db"
            p = _platform(root, state_path=state_path)
            shelf_store.persist_fixture(p, self.AS_OF)
            _save_weekly(
                p.state,
                run_id="weekly-public",
                as_of=self.AS_OF,
                classification=poc_workflow.SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
                instruments=["PUB-AI-INFRA", "PUB-EQ-QUALITY"],
            )

            first = cloud_paper.book_run(p, "weekly-public")
            repeated = cloud_paper.book_run(p, "weekly-public")
            book = first["books"]["buy_all"]
            self.assertEqual(book["placed"], 2)
            self.assertEqual(book["filled"], 2)
            self.assertEqual(repeated["books"]["buy_all"]["existing"], 2)
            positions = p.state.q(
                "SELECT cost, status FROM paper_positions ORDER BY pos_id")
            self.assertEqual(len(positions), 2)
            self.assertAlmostEqual(
                sum(float(row["cost"]) for row in positions),
                float(cloud_paper.config.SELECTOR_SPEC["capital"]) * 0.25,
            )

            future = date(2026, 9, 30)
            shelf_store.persist_fixture(p, future)
            p.state.connection.close()
            restarted = _platform(root, state_path=state_path)
            marked = cloud_paper.monitor(restarted, future)
            repeated_mark = cloud_paper.monitor(restarted, future)

            self.assertEqual(marked["closed"], 2)
            self.assertEqual(repeated_mark["closed"], 0)
            self.assertEqual(
                restarted.state.q(
                    "SELECT COUNT(*) AS n FROM paper_positions "
                    "WHERE status='closed'"
                )[0]["n"],
                2,
            )
            self.assertTrue(restarted.state.q(
                "SELECT equity FROM paper_equity "
                "WHERE book_id=? AND d=?",
                (book["book_id"], future.isoformat()),
            ))

    def test_licensed_shelf_and_paper_state_are_redacted(self):
        product_code = "PRIVATE-PRODUCT-4711"
        product_name = "Confidential Alpha Opportunities"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = _platform(root)
            shelf_store.persist(
                p,
                {"funds": [{
                    "productCode": product_code,
                    "productName": product_name,
                    "latestNav": 123.45,
                    "navDate": self.AS_OF.isoformat(),
                }]},
                as_of=self.AS_OF,
                source=shelf_store.LIVE_SOURCE,
                classification=shelf_store.LIVE_CLASSIFICATION,
                captured_at="2026-08-30T00:00:00Z",
            )
            weekly = poc_workflow.weekly_kwargs(
                self.AS_OF, p=p, mode="olive-live")
            _save_weekly(
                p.state,
                run_id="weekly-licensed",
                as_of=self.AS_OF,
                classification=poc_workflow.OLIVE_LIVE_WEEKLY_CLASSIFICATION,
                instruments=[product_code],
            )
            booked = cloud_paper.book_run(p, "weekly-licensed")
            payload = review.state(
                con=db.init(root / "legacy.db"),
                p=p,
            )
            rendered = json.dumps(payload, ensure_ascii=False)

            self.assertIn("cloud-licensed-buy_all", {
                value["book_id"] for value in booked["books"].values()
            })
            self.assertEqual(
                weekly["params"]["data_classification"],
                poc_workflow.OLIVE_LIVE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(
                weekly["universe"][0]["instrument_id"], product_code)
            self.assertNotIn(product_code, rendered)
            self.assertNotIn(product_name, rendered)
            self.assertNotIn("private thesis", rendered)
            self.assertTrue(payload["shelf"]["identifiers_redacted"])
            self.assertTrue(
                payload["shelf"]["instruments"][0]["instrument"].startswith(
                    "FUND-"))
            self.assertTrue(
                payload["weekly"]["pool"]["candidates"][0]["id"].startswith(
                    "CAND-"))

    def test_cloud_corpus_persistence_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            rows = [{
                "doc_id": "licensed-doc-1",
                "published_d": self.AS_OF.isoformat(),
                "title": "Licensed research sample",
                "tier": 1,
                "summary": "Bounded projection",
                "body": "Body retained only for the controlled data path.",
                "content_hash": "abc123",
                "retrieval": "provider://licensed-doc-1",
                "metadata": {"test": True},
            }]
            first = cloud_corpus.persist(p, rows, as_of=self.AS_OF)
            second = cloud_corpus.persist(p, rows, as_of=self.AS_OF)

            self.assertEqual(first["inputs_sha"], second["inputs_sha"])
            self.assertEqual(
                p.state.q(
                    "SELECT COUNT(*) AS n FROM corpus_documents"
                )[0]["n"],
                1,
            )
            self.assertEqual(
                cloud_corpus.corpus(p.state, as_of=self.AS_OF)[0]["doc_id"],
                "licensed-doc-1",
            )
            self.assertEqual(len(list(p.blobs.list("corpus/"))), 1)

    def test_cloud_corpus_shallow_refresh_preserves_deep_archive(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            base = {
                "doc_id": "licensed-doc-1",
                "published_d": self.AS_OF.isoformat(),
                "title": "Licensed research sample",
                "tier": 1,
                "summary": "Bounded projection",
                "content_hash": "abc123",
                "retrieval": "provider://licensed-doc-1",
            }
            cloud_corpus.persist(
                p,
                [{
                    **base,
                    "body": "Deep report body",
                    "raw_uri": "tos://private/corpus/raw/report.md",
                    "metadata": {
                        "body_chars": 16,
                        "body_sha256": "deep-sha",
                    },
                }],
                as_of=self.AS_OF,
            )
            cloud_corpus.persist(
                p,
                [{**base, "body": "", "metadata": {"refreshed": True}}],
                as_of=self.AS_OF,
            )

            row = dict(p.state.q(
                "SELECT body, raw_uri, metadata FROM corpus_documents "
                "WHERE doc_id=?",
                ("licensed-doc-1",),
            )[0])
            metadata = json.loads(row["metadata"])
            self.assertEqual(row["body"], "Deep report body")
            self.assertEqual(
                row["raw_uri"], "tos://private/corpus/raw/report.md")
            self.assertEqual(metadata["body_sha256"], "deep-sha")
            self.assertEqual(metadata["body_chars"], len("Deep report body"))
            self.assertTrue(metadata["refreshed"])

    @mock.patch("ideagen.cloud_corpus.config.wisburg_configured",
                return_value=True)
    def test_cloud_corpus_refetches_an_existing_shallow_document(
            self, _configured):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            item = cloud_corpus.wisburg.Item(
                line="company",
                category="company",
                tier=1,
                source_id=42,
                title="Licensed research sample",
                published_at="2026-08-30T08:00:00+08:00",
                url=None,
                summary="Bounded projection",
                body="",
                institution="Research provider",
                meta={},
            )
            cloud_corpus.persist(
                p,
                [{
                    "doc_id": item.doc_id,
                    "published_d": self.AS_OF.isoformat(),
                    "title": item.title,
                    "tier": item.tier,
                    "summary": item.summary,
                    "body": "",
                }],
                as_of=self.AS_OF,
            )
            client = mock.Mock()
            client.list_line.return_value = [item]
            client.detail.return_value = (
                "# Licensed research sample\n\n## 主要观点\n"
                + "Deep report evidence. " * 100
            )
            with mock.patch(
                    "ideagen.cloud_corpus.wisburg.Wisburg",
                    return_value=client):
                receipt = cloud_corpus.ingest_incremental(
                    p,
                    as_of=self.AS_OF,
                    detail_limit=1,
                    lines=["company"],
                )

            stored = p.state.q(
                "SELECT body, raw_uri FROM corpus_documents WHERE doc_id=?",
                (item.doc_id,),
            )[0]
            self.assertEqual(receipt["new"], 0)
            self.assertEqual(receipt["deep"], 1)
            self.assertIn("Deep report evidence", stored["body"])
            self.assertTrue(stored["raw_uri"].startswith("file://"))

    def test_review_reads_portable_wisburg_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            cloud_corpus.persist(
                p,
                [{
                    "doc_id": "licensed-doc-1",
                    "published_d": self.AS_OF.isoformat(),
                    "title": "Licensed research sample",
                    "tier": 1,
                    "institution": "Research provider",
                    "summary": "Bounded projection",
                    "body": "Body retained for the protected audit route.",
                    "content_hash": "abc123",
                    "retrieval": "provider://licensed-doc-1",
                }],
                as_of=self.AS_OF,
            )

            listing = review.corpus_list(
                db.init(Path(td) / "legacy.db"),
                as_of=self.AS_OF.isoformat(),
                p=p,
            )
            detail = review.doc_detail(
                db.init(Path(td) / "legacy.db"),
                doc_id="licensed-doc-1",
                p=p,
            )

            self.assertEqual(listing["n"], 1)
            self.assertEqual(listing["docs"][0]["sha"], "abc123")
            self.assertEqual(detail["title"], "Licensed research sample")
            self.assertGreater(detail["body_len"], 0)
            self.assertEqual(detail["body"], "")

    def test_review_keeps_the_weekly_corpus_count_frozen(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            schema.migrate(p.state)
            _save_weekly(
                p.state,
                run_id="weekly-licensed",
                as_of=self.AS_OF,
                classification=(
                    poc_workflow.WISBURG_SHELF_FIXTURE_WEEKLY_CLASSIFICATION),
                instruments=["PUBLIC-A"],
            )
            schema.upsert(p.state, "feed_runs", {
                "run_id": "weekly-licensed",
                "feed": "wisburg-mcp-rds+public-shelf-fixture-v1-corpus",
                "kind": "corpus",
                "as_of": self.AS_OF.isoformat(),
                "n_rows": 1,
                "ok": 1,
                "error": None,
                "rows_sha": "weekly-input",
            })
            cloud_corpus.persist(
                p,
                [{
                    "doc_id": f"licensed-doc-{index}",
                    "published_d": self.AS_OF.isoformat(),
                    "title": f"Licensed research {index}",
                    "tier": 1,
                } for index in range(2)],
                as_of=self.AS_OF,
            )

            payload = review.state(
                con=db.init(Path(td) / "legacy.db"),
                p=p,
            )

            self.assertEqual(payload["weekly"]["corpus_total"], 1)

    def test_order_waits_for_a_nav_then_fills_on_monitor(self):
        instrument_id = "PUBLIC-PENDING-NAV"
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            shelf_store.persist(
                p,
                {"funds": [{
                    "productCode": instrument_id,
                    "productName": "Public pending NAV fixture",
                }]},
                as_of=self.AS_OF,
                source=shelf_store.PUBLIC_FIXTURE_SOURCE,
                classification=shelf_store.PUBLIC_FIXTURE_CLASSIFICATION,
                captured_at="2026-08-30T00:00:00Z",
            )
            _save_weekly(
                p.state,
                run_id="weekly-pending",
                as_of=self.AS_OF,
                classification=poc_workflow.SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
                instruments=[instrument_id],
            )
            booked = cloud_paper.book_run(p, "weekly-pending")
            self.assertEqual(booked["books"]["buy_all"]["pending"], 1)
            self.assertFalse(p.state.q("SELECT * FROM paper_positions"))

            nav_day = date(2026, 8, 31)
            shelf_store.persist(
                p,
                {"funds": [{
                    "productCode": instrument_id,
                    "productName": "Public pending NAV fixture",
                    "latestNav": 101.25,
                    "navDate": nav_day.isoformat(),
                }]},
                as_of=nav_day,
                source=shelf_store.PUBLIC_FIXTURE_SOURCE,
                classification=shelf_store.PUBLIC_FIXTURE_CLASSIFICATION,
                captured_at="2026-08-31T00:00:00Z",
            )
            marked = cloud_paper.monitor(p, nav_day)

            self.assertEqual(marked["filled"], 1)
            self.assertEqual(
                p.state.q(
                    "SELECT status FROM paper_orders"
                )[0]["status"],
                "filled",
            )
            self.assertEqual(
                p.state.q(
                    "SELECT status FROM paper_positions"
                )[0]["status"],
                "open",
            )

    @mock.patch.dict(
        os.environ,
        {"IDEAGEN_CLOUD_WISBURG_ENABLED": ""},
    )
    def test_scheduler_does_not_copy_wisburg_to_cloud_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            p.state.paramstyle = "pyformat"
            with mock.patch(
                    "ideagen.cloud_corpus.ingest_window") as ingest_window:
                result = scheduler._ingest_corpus(
                    p, self.AS_OF, log=lambda _message: None)

            self.assertIn("disabled", result["skipped"])
            ingest_window.assert_not_called()

    @mock.patch.dict(os.environ, {
        "IDEAGEN_POC_WEEKLY_MODE": "wisburg-auto",
        "IDEAGEN_CLOUD_WISBURG_ENABLED": "true",
    })
    def test_scheduler_ingests_wisburg_before_preparing_live_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            schema.migrate(p.state)
            now = datetime(2026, 8, 30, 9, 0, tzinfo=scheduler.HKT)

            def ingest(platform, as_of, log):
                return cloud_corpus.persist(
                    platform,
                    [{
                        "doc_id": "scheduler-wisburg-1",
                        "published_d": as_of.isoformat(),
                        "title": "Scheduler Wisburg document",
                        "tier": 1,
                    }],
                    as_of=as_of,
                )

            result = SimpleNamespace(
                run_id="weekly-wisburg",
                ok=True,
                skipped=None,
                topics=["AI-CAPEX"],
                selectors={"buy_all": {}},
                artifacts=[],
                calls=1,
                journal="file://journal",
                error=None,
                n_candidates=1,
            )
            with mock.patch(
                    "ideagen.scheduler._ingest_corpus",
                    side_effect=ingest) as ingest_corpus, mock.patch(
                    "ideagen.orchestrator.weekly",
                    return_value=result) as weekly, mock.patch(
                    "ideagen.execution.selected",
                    return_value="disabled"), mock.patch(
                    "ideagen.scheduler._notify"):
                outcome = scheduler._run_weekly(
                    p,
                    now,
                    now.astimezone(timezone.utc),
                    as_of=self.AS_OF,
                    dry_run=False,
                    ingest=True,
                    weekly_kwargs=None,
                    log=lambda _message: None,
                )

            ingest_corpus.assert_called_once()
            supplied = weekly.call_args.kwargs
            self.assertEqual(
                supplied["corpus"][0]["doc_id"], "scheduler-wisburg-1")
            self.assertEqual(
                supplied["params"]["data_classification"],
                poc_workflow.WISBURG_SHELF_FIXTURE_WEEKLY_CLASSIFICATION,
            )
            self.assertEqual(outcome.action, "ran")

    def test_cloud_monitor_dry_run_writes_no_paper_rows(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            p.state.paramstyle = "pyformat"
            schema.migrate(p.state)
            now = datetime(
                2026, 8, 30, 9, 0, tzinfo=scheduler.HKT)
            outcome = scheduler._run_monitoring(
                p,
                now,
                now.astimezone(timezone.utc),
                dry_run=True,
                log=lambda _message: None,
            )

            self.assertEqual(outcome.detail["marks"]["skipped"],
                             "dry-run: no RDS paper writes")
            self.assertFalse(p.state.q("SELECT * FROM paper_equity"))

    @mock.patch.dict(os.environ, {
        "OLIVE_OAUTH_ACCESS_TOKEN": "",
        "IDEAGEN_OLIVE_TOKEN_FILE": "",
    })
    def test_daily_olive_sync_without_authorization_is_a_clean_skip(self):
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            schema.migrate(p.state)
            problems: list[str] = []
            now = datetime(2026, 8, 30, 9, 0, tzinfo=scheduler.HKT)
            result = scheduler._sync_olive_daily(
                p,
                now,
                now.astimezone(timezone.utc),
                problems,
                dry_run=False,
                log=lambda _message: None,
            )

            self.assertIn("尚未授权", result["skipped"])
            self.assertEqual(problems, [])
            self.assertFalse(p.state.q(
                "SELECT * FROM orch_runs WHERE kind='olive_sync'"))

    @mock.patch.dict(os.environ, {
        "OLIVE_OAUTH_ACCESS_TOKEN": "test-access",
        "OLIVE_OAUTH_REFRESH_TOKEN": "test-refresh",
    })
    def test_daily_olive_sync_is_once_per_day(self):
        snapshot = {
            "funds": [{
                "productCode": "PRIVATE-DAILY-1",
                "productName": "Licensed Daily Fund",
                "latestNav": 99.5,
                "navDate": self.AS_OF.isoformat(),
            }],
        }
        with tempfile.TemporaryDirectory() as td:
            p = _platform(Path(td))
            schema.migrate(p.state)
            problems: list[str] = []
            now = datetime(2026, 8, 30, 9, 0, tzinfo=scheduler.HKT)
            with mock.patch(
                    "ideagen.sources.olive.pull_snapshot",
                    return_value=snapshot) as pull:
                first = scheduler._sync_olive_daily(
                    p, now, now.astimezone(timezone.utc), problems,
                    dry_run=False, log=lambda _message: None)
                second = scheduler._sync_olive_daily(
                    p, now, now.astimezone(timezone.utc), problems,
                    dry_run=False, log=lambda _message: None)

            self.assertEqual(first["items"], 1)
            self.assertIn("今日真实货架已同步", second["skipped"])
            pull.assert_called_once()
            self.assertEqual(problems, [])
            self.assertEqual(
                p.state.q(
                    "SELECT COUNT(*) AS n FROM orch_runs "
                    "WHERE kind='olive_sync' AND ok=1"
                )[0]["n"],
                1,
            )


    def test_selector_metadata_table_has_no_holes(self):
        """SEL_META must parse into one complete row per registered selector.

        A row appended after one that lacked a trailing comma turns two array
        elements into an index expression: the array silently loses an entry
        and gains a hole, `selMetaOf` then reads m[0] off undefined, and the
        whole page renders as "无法连接或读取服务". A missing comma is not
        a syntax error, so nothing else catches it.
        """
        import re
        root = Path(__file__).resolve().parent.parent
        dashboard = (root / "web" / "dash.html").read_text(encoding="utf-8")
        block = re.search(r"var SEL_META=\[(.*?)\n\];", dashboard, re.S)
        self.assertIsNotNone(block, "SEL_META 不见了")
        rows = [ln.strip() for ln in block.group(1).splitlines()
                if ln.strip().startswith("[")]
        self.assertGreaterEqual(len(rows), 10)
        for row in rows[:-1]:
            self.assertTrue(row.endswith("],"),
                            f"少了逗号，下一行会被当成索引访问：{row[:40]}…")
        self.assertTrue(rows[-1].rstrip().endswith("]"), "最后一行不该有多余逗号")
        # Every selector the dashboard can be asked to name must have a row.
        for name in ("ai_native", "buy_all", "random_pick", "calib", "spread",
                     "left_tail", "omega_loose", "omega_strict",
                     "generated_ai_native", "generated_carl_constraint"):
            self.assertTrue(any(r.startswith(f"['{name}'") for r in rows),
                            f"SEL_META 缺 {name}")


if __name__ == "__main__":
    unittest.main()
