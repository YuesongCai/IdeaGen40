"""WS-D (yifu / Jon 2026-09-11): read-only viewer role, the weekly 筛选A theme
digest, the strategy card, and 「我的准则」 surviving a deploy.

Every write endpoint is tested against a *viewer session over a real socket*,
because the failure this guards against is a route added later that forgot the
check — a unit test on the helper would stay green while that route is open.
Offline throughout: in-memory databases, a local stub for the node export.
"""
from __future__ import annotations

import http.cookiejar
import http.server
import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ideagen import access, db

ROOT = Path(__file__).resolve().parent.parent


class _Client:
    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _open(self, req):
        try:
            r = self.opener.open(req, timeout=60)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, path: str):
        return self._open(urllib.request.Request(self.base + path,
                                                 headers={"Accept": "application/json"}))

    def form(self, path: str, data: dict):
        return self._open(urllib.request.Request(
            self.base + path, method="POST", data=urllib.parse.urlencode(data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": self.base, "Referer": self.base + path}))

    def json_post(self, path: str, body: dict):
        return self._open(urllib.request.Request(
            self.base + path, method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base,
                     "Referer": self.base + path}))


# ------------------------------------------------------------------ roles
class ViewerRole(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in ("IDEAGEN_ACCOUNTS_FILE", "IDEAGEN_ACCOUNTS_MIRROR")}
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(Path(self._tmp.name) / "accounts.json")
        os.environ.pop("IDEAGEN_ACCOUNTS_MIRROR", None)
        from ideagen import accounts
        self.a = accounts

    def tearDown(self):
        for k, v in self._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self._tmp.cleanup()

    def test_viewer_is_a_role_and_round_trips(self):
        a = self.a
        self.assertIn("viewer", a.ROLES)
        a.add_user("boss", "password-123", role="admin")
        a.add_user("yifu", "password-123", role="viewer", note="yifu 方")
        self.assertEqual(a.role("yifu"), "viewer")
        self.assertTrue(a.is_viewer("YIFU"))
        self.assertFalse(a.is_admin("yifu"))
        self.assertEqual({u["name"]: u["role"] for u in a.list_users()},
                         {"boss": "admin", "yifu": "viewer"})
        a.set_role("yifu", "member")
        self.assertEqual(a.role("yifu"), "member")
        a.set_role("yifu", "viewer")
        a.set_role("yifu", "admin")
        self.assertEqual(a.role("yifu"), "admin")
        self.assertFalse(a.is_viewer("yifu"))

    def test_old_files_without_role_key_still_read_as_before(self):
        a = self.a
        a.add_user("old", "password-123", role="member")
        data = a.load()
        data["users"]["old"].pop("role", None)
        a.save(data)
        self.assertEqual(a.role("old"), "member")
        self.assertFalse(a.is_viewer(None))

    def test_account_page_offers_the_viewer_role(self):
        from ideagen import authpages
        a = self.a
        a.add_user("boss", "password-123", role="admin")
        a.add_user("yifu", "password-123", role="viewer")
        html = authpages.account_page("boss", admin=True, users=a.list_users()).decode()
        self.assertIn("value=viewer", html)
        self.assertIn("只读", html)


class AccessRule(unittest.TestCase):
    def test_default_is_refuse_for_any_post_not_listed(self):
        for path in ("/api/pm_review", "/api/ask", "/api/philosophy/propose",
                     "/api/philosophy/draft", "/api/olive/sync", "/account/add",
                     "/account/role", "/api/some-route-added-next-month"):
            self.assertTrue(access.viewer_refused("POST", path), path)
        for path in ("/login", "/logout", "/account/password", "/account/revoke"):
            self.assertFalse(access.viewer_refused("POST", path), path)

    def test_reads_are_open_except_the_listed_side_effect(self):
        self.assertFalse(access.viewer_refused("GET", "/api/state"))
        self.assertFalse(access.viewer_refused("GET", "/api/digest?as_of=2026-09-16"))
        self.assertTrue(access.viewer_refused("GET", "/api/olive/oauth/start"))

    def test_non_session_callers_are_not_viewers(self):
        class A:
            @staticmethod
            def is_viewer(n):
                return True
        self.assertFalse(access.refused_for(A, None, "POST", "/api/ask"))
        self.assertTrue(access.refused_for(A, "yifu", "POST", "/api/ask"))


class ServerRefusesViewerWrites(unittest.TestCase):
    """A viewer session over a real socket: every write refused, reads served."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls._prev = {k: os.environ.get(k) for k in
                     ("IDEAGEN_ACCOUNTS_FILE", "IDEAGEN_DASH_KEY", "IDEAGEN_ACCOUNTS_MIRROR",
                      "IDEAGEN_PHILOSOPHY_DIR")}
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(Path(cls._tmp.name) / "accounts.json")
        os.environ["IDEAGEN_DASH_KEY"] = "k-wsd"
        os.environ.pop("IDEAGEN_ACCOUNTS_MIRROR", None)
        from ideagen import accounts
        from ideagen.serve import Handler, Server
        accounts.add_user("boss", "admin-password-1", role="admin")
        accounts.add_user("jon", "member-password-1", role="member")
        accounts.add_user("yifu", "viewer-password-1", role="viewer")
        cls.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        cls._tmp.cleanup()

    def _login(self, user, pw):
        c = _Client(self.base)
        c.form("/login", {"username": user, "password": pw, "next": "/review"})
        return c

    def test_whoami_says_viewer(self):
        st, body = self._login("yifu", "viewer-password-1").get("/api/whoami")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["role"], "viewer")

    def test_every_write_is_refused_for_a_viewer(self):
        c = self._login("yifu", "viewer-password-1")
        for path, body in (("/api/pm_review", {"as_of": "2026-09-16", "instrument_id": "SMH",
                                               "decision": "采纳"}),
                           ("/api/ask", {"question": "为什么"}),
                           ("/api/philosophy/propose", {"say": "x"}),
                           ("/api/philosophy/activate", {"card_id": "x"}),
                           ("/api/philosophy/draft", {"say": "x"}),
                           ("/api/olive/sync", {}),
                           ("/api/not-yet-written", {})):
            st, text = c.json_post(path, body)
            self.assertEqual(st, 403, f"{path}: {text[:200]}")
            self.assertIn("只读账号", text)
        st, text = c.form("/account/add", {"username": "x", "password": "password-123"})
        self.assertEqual(st, 403)
        st, _ = c.get("/api/olive/oauth/start")
        self.assertEqual(st, 403)

    def test_viewer_can_still_read_and_manage_own_password(self):
        c = self._login("yifu", "viewer-password-1")
        st, body = c.get("/api/philosophy/export")
        self.assertEqual(st, 200, body[:200])
        self.assertIn("events", json.loads(body))
        st, body = c.form("/account/password", {"current": "wrong", "password": "whatever-123"})
        self.assertEqual(st, 200)
        self.assertIn("当前口令不对", body)

    def test_member_is_not_caught_by_the_viewer_gate(self):
        c = self._login("jon", "member-password-1")
        st, text = c.json_post("/api/philosophy/no-such-op", {})
        self.assertEqual(st, 404, text)


# ------------------------------------------------------------------ philosophy persistence
class PhilosophyPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in ("IDEAGEN_DB", "IDEAGEN_PHILOSOPHY_DIR")}

    def tearDown(self):
        for k, v in self._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self._tmp.cleanup()

    def test_store_sits_beside_a_database_outside_the_checkout(self):
        from ideagen import config, philosophy_sync as ps
        os.environ.pop("IDEAGEN_PHILOSOPHY_DIR", None)
        os.environ["IDEAGEN_DB"] = str(Path(self._tmp.name) / "ideagen.db")
        self.assertEqual(ps.philosophy_dir(), Path(self._tmp.name).resolve() / "philosophy")
        os.environ["IDEAGEN_DB"] = str(ROOT / "data" / "ideagen.db")
        self.assertEqual(ps.philosophy_dir(), config.DATA / "philosophy")
        os.environ.pop("IDEAGEN_DB")
        self.assertEqual(ps.philosophy_dir(), config.DATA / "philosophy")
        os.environ["IDEAGEN_PHILOSOPHY_DIR"] = "/x/y"
        self.assertEqual(ps.philosophy_dir(), Path("/x/y"))

    def test_display_node_layout_is_what_the_deploy_scripts_set(self):
        # The fix reaches the node through code alone because the node already
        # runs with IDEAGEN_DB on the /data mount. If that ever changes, this is
        # the test that should say so.
        for f in ("deploy/sync_code.sh", "deploy/display_node_bootstrap.sh"):
            if not (ROOT / f).exists():
                # The deploy image copies no deploy scripts; without this skip the
                # node's pre-switch test gate fails and silently refuses every deploy.
                self.skipTest("这里没有 deploy/（镜像里就是如此）")
            text = (ROOT / f).read_text(encoding="utf-8")
            self.assertIn("IDEAGEN_DB=/data/ideagen.db", text, f)
            self.assertIn(':/data', text, f)

    def test_merge_is_idempotent_ordered_and_rejects_bad_rows(self):
        from ideagen import philosophy_sync as ps
        ledger = Path(self._tmp.name) / "ledger.jsonl"
        act = {"event": "activate", "card_id": "pm-2026-09-17-abcdef",
               "card": {"card_id": "pm-2026-09-17-abcdef", "as_of": "2026-09-17"}}
        ret = {"event": "retire", "card_id": "pm-2026-09-17-abcdef", "as_of": "2026-09-20"}
        st = ps.merge_events([act, ret, {"event": "activate"}, "junk"], ledger)
        self.assertEqual((st["appended"], st["invalid"]), (2, 2))
        st = ps.merge_events([ret, act], ledger)
        self.assertEqual((st["appended"], st["kept"]), (0, 2))
        lines = [json.loads(x) for x in ledger.read_text().splitlines()]
        self.assertEqual([x["event"] for x in lines], ["activate", "retire"])

    def test_pull_merges_the_node_export(self):
        from ideagen import philosophy_sync as ps
        events = [{"event": "activate", "card_id": "pm-2026-09-17-000001",
                   "card": {"card_id": "pm-2026-09-17-000001", "as_of": "2026-09-17"}}]
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen["key"] = self.headers.get("X-Dash-Key")
                ok = self.path == "/api/philosophy/export"
                body = json.dumps({"events": events} if ok else {"error": "x"}).encode()
                self.send_response(200 if ok else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ledger = Path(self._tmp.name) / "ledger.jsonl"
        try:
            with mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "http_proxy": "",
                                              "HTTP_PROXY": ""}):
                rep = ps.pull(url=f"http://127.0.0.1:{srv.server_address[1]}", key="k",
                              ledger=ledger)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(rep["appended"], 1)
        self.assertEqual(seen["key"], "k")

    def test_adopt_legacy_copies_once_and_never_overwrites(self):
        from ideagen import config, philosophy_sync as ps
        legacy = Path(self._tmp.name) / "legacy"
        (legacy / "philosophy").mkdir(parents=True)
        (legacy / "philosophy" / "ledger.jsonl").write_text('{"a":1}\n')
        durable = Path(self._tmp.name) / "durable"
        with mock.patch.object(config, "DATA", legacy):
            self.assertTrue(ps.adopt_legacy(durable))
            (durable / "ledger.jsonl").write_text('{"b":2}\n')
            self.assertFalse(ps.adopt_legacy(durable))
        self.assertEqual((durable / "ledger.jsonl").read_text(), '{"b":2}\n')

    def test_sync_pulls_philosophy_before_publishing(self):
        sync = (ROOT / "scripts" / "sync_to_cloud.py").read_text(encoding="utf-8")
        leg = sync.index("def data_leg")
        self.assertLess(sync.index('out["philosophy"] = pull_philosophy', leg),
                        sync.index("fp = content_fingerprint()", leg))


# ------------------------------------------------------------------ digest
def _digest_fixture(con):
    con.execute("INSERT INTO themes(as_of, theme_id, label, tis, tier, b, factors) VALUES "
                "('2026-09-15','AI-CAPEX','AI资本开支',60.7,'important',40,?)",
                (json.dumps({"direction": "↑", "recurrence": {"consec": 3, "discount": 7.5,
                                                              "note": "连续第 4 次出现，折 7.5 分"}}),))
    con.execute("INSERT INTO themes(as_of, theme_id, label, tis, tier, b, factors) VALUES "
                "('2026-09-15','DOLLAR-FX','美元',63.0,'important',90,?)",
                (json.dumps({"direction": "↓"}),))
    for i, (inst, line, d) in enumerate((("UBS", "feed", "2026-09-15"), (None, "ib", "2026-09-14"),
                                         ("Citi", "feed", "2026-09-13"), ("JPM", "feed", "2026-09-12"))):
        con.execute("INSERT INTO documents(doc_id,line,tier,title,institution,published_d,ingested_at) "
                    "VALUES (?,?,?,?,?,?,?)", (f"d{i}", line, 1, f"标题{i}", inst, d, "x"))
    db.kv_set(con, "diffusion.index", ["2026-09-10"])
    db.kv_set(con, "diffusion.snapshot.2026-09-10", {"as_of": "2026-09-10", "themes": [
        {"theme_id": "AI-CAPEX", "stage": "源头期", "stage_why": "社交先起", "lead_days": 3,
         "social": {}, "research": {}}]})
    return {"run_id": "r1", "as_of": "2026-09-16", "ok": True, "in_flight": False,
            "data_classification": "live", "corpus_total": 713,
            "topics": [{"scorer": "hgep", "chosen": ["AI-CAPEX", "DOLLAR-FX"], "scores": {
                "AI-CAPEX": {"label": "AI资本开支", "score": 71.7, "G": 66.7, "H": 1, "E": 1, "P": 1,
                             "doc_ids": ["d0", "d1", "d2", "d3"], "n_evidence": 49,
                             "n_institutions": 39},
                "DOLLAR-FX": {"label": "美元", "score": 77.1, "G": None, "doc_ids": []}}}],
            "themes": {"AI-CAPEX": {"label": "AI资本开支", "key_question": "订单能否覆盖资本开支",
                                    "direction": "↑"},
                       "DOLLAR-FX": {"label": "美元", "key_question": "美元能否停止升值",
                                     "direction": "↑"}},
            "evidence": {"DOLLAR-FX": {"docs": [{"doc_id": "e1", "title": "证据一",
                                                 "institution": "BofA", "tier": 1,
                                                 "published_d": "2026-09-15"}]}}}


class ThemeDigest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.con = db.init(":memory:")
        self.weekly = _digest_fixture(self.con)
        self.p = mock.patch("ideagen.review.weekly_block", return_value=self.weekly)
        self.p.start()
        self.env = mock.patch.dict(os.environ, {"IDEAGEN_DIGEST_DIR": self._tmp.name})
        self.env.start()
        os.environ.pop("IDEAGEN_DIGEST_FEISHU_CHAT_ID", None)

    def tearDown(self):
        self.p.stop()
        self.env.stop()
        self._tmp.cleanup()

    def test_build_orders_by_tis_and_names_every_source(self):
        from ideagen import theme_digest as td
        doc = td.build(None, self.con, "2026-09-16")
        self.assertTrue(doc["available"])
        self.assertEqual([t["theme_id"] for t in doc["themes"]], ["DOLLAR-FX", "AI-CAPEX"])
        dfx, ai = doc["themes"]
        # The badge falls back to the daily B when this period has no G.
        self.assertEqual((dfx["disagreement"]["factor"], dfx["disagreement"]["badge"]), ("B", True))
        self.assertEqual((ai["disagreement"]["factor"], ai["disagreement"]["badge"]), ("G", False))
        # The reading was borrowed from an earlier day and says so.
        self.assertEqual((ai["reading_d"], ai["reading_same_day"]), ("2026-09-15", False))
        self.assertEqual(ai["recurrence"]["discount"], 7.5)
        self.assertEqual(dfx["direction"], "↓")          # the measured one, not the registry's ↑
        self.assertEqual(ai["diffusion"]["stage"], "源头期")
        self.assertEqual(len(ai["docs"]), 3)
        self.assertEqual(ai["docs"][0]["institution"], "UBS")   # a named house first
        self.assertEqual(dfx["docs"][0]["title"], "证据一")     # fallback to period evidence
        self.assertIsNone(ai["timing"])                          # no audit run → named, not invented
        self.assertTrue(any("时点与谱系" in n for n in doc["notes"]))

    def test_licensed_titles_are_withheld(self):
        from ideagen import theme_digest as td
        self.weekly["data_classification"] = "licensed-private-corpus"
        doc = td.build(None, self.con, "2026-09-16")
        self.assertTrue(all(d["title"] == "授权研报，标题不公开"
                            for t in doc["themes"] for d in t["docs"]))

    def test_after_weekly_writes_once_and_skips_feishu_unless_configured(self):
        from ideagen import theme_digest as td
        with mock.patch("ideagen.theme_digest.subprocess.run") as run:
            st = td.after_weekly(None, self.con, "2026-09-16")
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["feishu"]["state"], "skipped")
            run.assert_not_called()
        md = Path(self._tmp.name, "themes_2026-09-16.md").read_text(encoding="utf-8")
        self.assertIn("筛选A 主题周报 · 2026-09-16", md)
        self.assertIn("连续第 4 次出现", md)
        self.assertEqual(td.after_weekly(None, self.con, "2026-09-16")["action"], "already")

    def test_feishu_uses_the_chat_id_when_set(self):
        from ideagen import theme_digest as td
        with mock.patch.dict(os.environ, {"IDEAGEN_DIGEST_FEISHU_CHAT_ID": "oc_test"}), \
                mock.patch("ideagen.theme_digest.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            st = td.after_weekly(None, self.con, "2026-09-16")
        args = run.call_args[0][0]
        # `args[0]` is whichever binary `IDEAGEN_LARK_CLI` names — a bare
        # `lark-cli` here, an absolute path on a machine that sets it because
        # launchd's PATH cannot find one. Pinning the literal made this test
        # fail on the configuration that actually works in production, which is
        # the wrong way round; what it is here to check is the routing.
        self.assertTrue(args[0].endswith("lark-cli"), args[0])
        self.assertEqual(args[1:6],
                         ["im", "+messages-send", "--as", "bot", "--chat-id"])
        self.assertEqual(args[6], "oc_test")
        self.assertIn("--markdown", args)
        # And the PATH that lets that binary's node shebang resolve.
        self.assertIn("/opt/homebrew/bin",
                      run.call_args.kwargs["env"]["PATH"].split(":"))
        self.assertEqual(st["feishu"]["state"], "sent")

    def test_a_failure_is_recorded_not_raised(self):
        from ideagen import theme_digest as td
        with mock.patch("ideagen.theme_digest.build", side_effect=RuntimeError("boom")):
            st = td.after_weekly(None, self.con, "2026-09-16")
        self.assertFalse(st["ok"])
        self.assertIn("boom", st["why"])

    def test_scheduler_runs_it_after_booking(self):
        src = (ROOT / "ideagen" / "scheduler.py").read_text(encoding="utf-8")
        i = src.index("theme_digest.after_weekly")
        self.assertLess(src.index("booking.book_run(legacy"), i)
        self.assertLess(i, src.index('_notify(f"✅ IdeaGen 周跑完成'))


# ------------------------------------------------------------------ strategy card
class StrategyCard(unittest.TestCase):
    def test_numbers_come_from_the_tables_and_gaps_are_named(self):
        from ideagen import strategy_card as sc
        con = db.init(":memory:")
        for i, (st, realized, theme, cost) in enumerate((("open", 0, "AI-CAPEX", 300.0),
                                                         ("open", 0, "INFLATION", 100.0),
                                                         ("closed", 5, "AI-CAPEX", 100.0),
                                                         ("closed", -2, "AI-CAPEX", 100.0))):
            con.execute("INSERT INTO positions(pos_id,book_id,idea_uid,code,kind,theme,qty,avg_px,"
                        "cost,opened_d,status,realized) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"p{i}", "sel-shortlist", "u", f"C{i}", "etf", theme, 1, 1, cost,
                         "2026-09-01", st, realized))
        con.execute("INSERT INTO equity(book_id,d,equity) VALUES ('sel-shortlist','2026-09-15',1000)")
        con.execute("INSERT INTO trades(trade_id,book_id,d,side,code,gross) VALUES "
                    "('t1','sel-shortlist','2026-09-15','BUY','C0',400)")
        pv = {"strategies": [{"key": "shortlist", "name": "精选", "available": True,
                              "first_d": "2026-09-01",
                              "curve": [{"d": "2026-09-01", "v": 10.0}, {"d": "2026-09-15", "v": 9.0}]}],
              "benchmarks": {"spy": {"name": "SPY", "curve": [{"d": "2026-09-01", "v": 500.0},
                                                             {"d": "2026-09-15", "v": 510.0}]}},
              "summary": {"rows": [{"key": "shortlist", "cum_ret_pct": -10.0, "max_dd_pct": -11.0}]},
              "research": {"stats": {"arms": {}}}, "window": {"start": "2026-09-01", "end": "2026-09-15"}}
        with mock.patch("ideagen.performance.paper_view", return_value=pv), \
                mock.patch("ideagen.review.weekly_block", return_value={}), \
                mock.patch("ideagen.platform.load", return_value=None):
            doc = sc.build(con, fresh=True)
        self.assertEqual((doc["books_in_play"], doc["open_positions"]), (1, 2))
        self.assertEqual(doc["weekly_turnover"]["pct"], 10.0)          # 400 / 4 / 1000
        self.assertEqual(doc["theme_exposure"][0], {"theme_id": "AI-CAPEX", "n": 1, "share": 0.75})
        m = doc["metrics"]["shortlist"]
        self.assertEqual((m["n_closed"], m["n_win"], m["hit_rate"]), (2, 1, 0.5))
        self.assertEqual(m["max_dd_pct"], -11.0)
        spy = next(s for s in doc["curve"]["series"] if s["key"] == "spy")
        self.assertEqual(spy["points"][-1]["v"], 102.0)
        base = next(s for s in doc["curve"]["series"] if s["key"] == "buy_all")
        self.assertEqual(base["points"], [])                           # absent, not invented
        self.assertEqual(doc["periods"], {"backfill": 0, "live": 0, "first": None, "last": None})
        self.assertIsNone(doc["metrics"]["buy_all"]["hit_rate"])
        self.assertIn("实盘", doc["scope"])
        self.assertTrue(doc["scope"].endswith("未接入实盘资金"))


class DashContract(unittest.TestCase):
    def test_drawers_are_addressable_and_have_one_entry_each(self):
        html = (ROOT / "web" / "dash.html").read_text(encoding="utf-8")
        self.assertIn("digest:1,card:1", html)
        self.assertEqual(html.count("onclick=\"openDigestDrawer("), 1)
        self.assertEqual(html.count("onclick=\"openCardDrawer("), 1)
        self.assertIn("/* WS-D */", html)
        self.assertIn("'/api/digest'", html)
        self.assertIn("'/api/strategy_card'", html)


if __name__ == "__main__":
    unittest.main()
