"""组合决策层（工作流 B）：公募收紧、精选组合、卫星仓下单单、PM 反方筛选闭环。

每一条对应 yifu 2026-09-11 对齐里的一句话，测的是那句话落地后**不能悄悄变回去**
的那一点：

* 基金净值不够新就不进筛选C —— 上市标的不受影响，0 = 关闭；
* 精选只有一份：选取策略、面板、下单单读的是同一个排序函数的同一次结果；
* PM 只剔除不塞名字：精选之外的标的一律拒绝，否决必须带理由类别 + 一句话；
* 写接口要署名、只读角色拒绝写，且被拒的请求不碰数据库；
* 展示节点上的决定能回到本机：日志重放与导出合并都是「新的赢」、可重复执行；
* 到期结果按决定分组，样本小写「样本不足」而不是给判定。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config, db, decision  # noqa: E402


class _State:
    def __init__(self, con):
        self.connection = con

    def q(self, sql, args=()):
        return [dict(r) for r in self.connection.execute(sql, tuple(args)).fetchall()]


class _P:
    def __init__(self, con):
        self.state = _State(con)


def _prices(con, code, days, start=100.0, step=1.0, volume=1000.0):
    for i, d in enumerate(days):
        db.upsert(con, "prices", {"code": code, "d": d, "open": start + i * step,
                                  "high": start + i * step, "low": start + i * step,
                                  "close": start + i * step, "volume": volume, "src": "t"},
                  ["code", "d"])


def _cand(inst, topic, up=8.0, dn=-4.0, pu=0.5, pb=0.3, pd=0.2, methods=("chain",),
          **kw):
    return {"id": f"pool:{inst}", "instrument_id": inst, "topic_id": topic,
            "upside_pct": up, "downside_pct": dn, "p_up": pu, "p_base": pb, "p_down": pd,
            "proposed_by": list(methods), "n_methods": len(methods),
            "thesis": f"{inst} 的理由第一句。第二句不该出现。", "method": "merged", **kw}


DAYS = [f"2026-08-{d:02d}" for d in range(3, 32)] + [f"2026-09-{d:02d}" for d in range(1, 17)]


def _world():
    """Three listed ETFs, one fund with a fresh NAV, one with a stale NAV."""
    con = db.init(":memory:")
    for k in ("SMH", "CIBR", "COPX"):
        _prices(con, f"US.{k}", DAYS)
    for key, nav_d in (("LUFRESH", "2026-09-08"), ("LUSTALE", "2026-08-31")):
        db.upsert(con, "instruments", {"key": key, "name": key, "kind": "fund",
                                       "olive_key": key, "priceable": 0,
                                       "market": "OLIVE", "currency": "USD"}, ["key"])
        db.upsert(con, "navs", {"olive_key": key, "d": nav_d, "nav": 10.0, "src": "t"},
                  ["olive_key", "d"])
    from ideagen import universe as uni
    uni.hydrate(con)
    return con


class FundNavMustBeFreshToEnterStageC(unittest.TestCase):
    def setUp(self):
        self._prev = config.FUND_NAV_FRESH_DAYS

    def tearDown(self):
        config.FUND_NAV_FRESH_DAYS = self._prev

    def test_stale_fund_is_flagged_listed_and_fresh_fund_are_not(self):
        con = _world()
        config.FUND_NAV_FRESH_DAYS = 5
        cands = [_cand("SMH", "AI"), _cand("LUFRESH", "AI"), _cand("LUSTALE", "AI")]
        counts = decision.annotate_pool(_P(con), date(2026, 9, 9), cands)
        flags = {c["instrument_id"]: c["stale_nav_excluded"] for c in cands}
        self.assertEqual(flags, {"SMH": False, "LUFRESH": False, "LUSTALE": True})
        self.assertEqual(counts["stale_nav_excluded"], 1)
        self.assertEqual(cands[2]["nav_stale_days"], 9)
        self.assertFalse(decision.in_selection_pool(cands[2]))
        self.assertTrue(decision.in_selection_pool(cands[1]))

    def test_zero_turns_the_gate_off(self):
        con = _world()
        config.FUND_NAV_FRESH_DAYS = 0
        cands = [_cand("LUSTALE", "AI")]
        decision.annotate_pool(_P(con), date(2026, 9, 9), cands)
        self.assertFalse(cands[0]["stale_nav_excluded"])

    def test_dry_run_checks_nothing(self):
        con = _world()
        cands = [_cand("LUSTALE", "AI")]
        self.assertEqual(decision.annotate_pool(_P(con), date(2026, 9, 9), cands,
                                                dry_run=True)["checked"], 0)

    def test_live_run_and_replay_share_the_gate(self):
        """口径须与实时一致: both paths call the same two functions."""
        orch = (ROOT / "ideagen" / "orchestrator.py").read_text(encoding="utf-8")
        rs = (ROOT / "ideagen" / "reselect.py").read_text(encoding="utf-8")
        for src in (orch, rs):
            self.assertIn("annotate_pool(p, as_of,", src)
            self.assertIn("in_selection_pool(c)", src)
        self.assertIn("stale_nav_excluded=", orch)


class TheShortlistRanking(unittest.TestCase):
    def _row(self, inst, topic, ev, n=1, grade="A", rf=0.0):
        return {"id": f"pool:{inst}", "instrument_id": inst, "topic_id": topic,
                "ev_c": ev, "n_methods": n, "grade": grade, "recur_frac": rf}

    def test_score_is_consensus_times_positive_ev_times_undiscounted_share(self):
        rk = decision.rank_shortlist([self._row("A", "t1", 2.0, n=3, rf=0.25)], n=5)
        self.assertAlmostEqual(rk["rows"]["pool:A"]["score"], 3 * 2.0 * 0.75)

    def test_recurrence_changes_the_order(self):
        rows = [self._row("OLD", "t1", 2.0, n=2, rf=0.5), self._row("NEW", "t2", 1.5, n=2)]
        self.assertEqual(decision.rank_shortlist(rows, n=5)["chosen"],
                         ["pool:NEW", "pool:OLD"])

    def test_ties_break_on_grade_then_ev(self):
        rows = [self._row("B1", "t1", 2.0, n=2, grade="B"),
                self._row("S1", "t2", 4.0, n=1, grade="S"),
                self._row("A1", "t3", 2.0, n=2, grade="A")]
        self.assertEqual(decision.rank_shortlist(rows, n=5)["chosen"],
                         ["pool:S1", "pool:A1", "pool:B1"])

    def test_non_positive_ev_is_not_a_pick(self):
        rk = decision.rank_shortlist([self._row("NEG", "t", -0.5, n=4),
                                      self._row("ZERO", "t2", 0.0, n=4)], n=5)
        self.assertEqual(rk["chosen"], [])
        self.assertIn("不为正", rk["rejected"]["pool:NEG"])

    def test_theme_cap_is_applied_and_named(self):
        rows = [self._row(f"X{i}", "same", 5.0 - i) for i in range(3)] + \
               [self._row("Y", "other", 0.5)]
        rk = decision.rank_shortlist(rows, n=5, max_per_theme=2)
        self.assertEqual(rk["chosen"], ["pool:X0", "pool:X1", "pool:Y"])
        self.assertIn("同主题上限", rk["rejected"]["pool:X2"])

    def test_n_limits_and_records_the_rest(self):
        rows = [self._row(f"Z{i}", f"t{i}", 5.0 - i) for i in range(4)]
        rk = decision.rank_shortlist(rows, n=2, max_per_theme=0)
        self.assertEqual(len(rk["chosen"]), 2)
        self.assertIn("不在前 2 名", rk["rejected"]["pool:Z3"])

    def test_recurrence_share_reads_discount_over_raw(self):
        self.assertAlmostEqual(decision.recur_frac({"discount": 12.0, "tis_raw": 60.0}), 0.2)
        self.assertEqual(decision.recur_frac(None), 0.0)
        self.assertEqual(decision.recur_frac({"discount": 0, "tis_raw": 50}), 0.0)

    def test_the_selector_is_registered_and_uses_the_same_function(self):
        from ideagen import strategy, trials
        strategy._load_plugins()
        self.assertIn("shortlist", {r["name"] for r in strategy.available("idea_selector")})
        self.assertIn("shortlist", trials.registered())
        src = (ROOT / "ideagen" / "strategies" / "select_shortlist.py").read_text("utf-8")
        self.assertIn("decision.rank_shortlist(", src)

    def test_selector_verdict(self):
        from ideagen import strategy
        strategy._load_plugins()
        ctx = strategy.RunContext(as_of=date(2026, 9, 9), inputs_sha="x",
                                  candidates=[self._row("A", "t", 2.0, n=2)],
                                  params={"n": 5})
        v = strategy.run("idea_selector", "shortlist", ctx)
        self.assertEqual(v.chosen, ["pool:A"])


class ScoresAreTheBookedScores(unittest.TestCase):
    def test_annotated_ev_equals_what_booking_would_store(self):
        con = _world()
        c = _cand("SMH", "AI")
        decision.annotate_pool(_P(con), date(2026, 9, 9), [c])
        from ideagen import booking, ideas
        row = ideas.compute(con, booking.payload_from_candidates([_cand("SMH", "AI")])
                            ["ideas"][0], date(2026, 9, 9), "x")
        self.assertEqual(c["ev_c"], row["ev_c"])
        self.assertEqual(c["grade"], row["grade"])

    def test_recurrence_is_read_from_the_week_before_never_after(self):
        con = _world()
        for d, disc in (("2026-09-08", 6.0), ("2026-09-10", 30.0)):
            db.upsert(con, "themes", {"as_of": d, "theme_id": "AI", "label": "AI",
                                      "factors": json.dumps({"recurrence": {
                                          "discount": disc, "tis_raw": 60.0}})},
                      ["as_of", "theme_id"])
        c = _cand("SMH", "AI")
        decision.annotate_pool(_P(con), date(2026, 9, 9), [c])
        self.assertAlmostEqual(c["recur_frac"], 0.1)


def _stored_run(con, as_of="2026-09-09", run_id="r1", cands=None, verdict=None):
    con.execute("INSERT INTO orch_runs(run_id, as_of, kind, ok, started_at) "
                "VALUES(?,?,?,?,?)", (run_id, as_of, "weekly", 1, as_of + "T00:00:00Z"))
    for c in cands or []:
        con.execute("INSERT INTO candidates(run_id, candidate_id, as_of, payload) "
                    "VALUES(?,?,?,?)", (run_id, c["id"], as_of, json.dumps(c)))
    if verdict is not None:
        con.execute("INSERT INTO verdicts(run_id, as_of, kind, strategy, version, role, "
                    "inputs_sha, chosen, scores, rejected, meta, calls) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, as_of, "idea_selector", "shortlist", "1.0", "exploratory",
                     "x", json.dumps(verdict), "{}", "{}", "{}", 0))
    con.commit()


class OneShortlistEverywhere(unittest.TestCase):
    def setUp(self):
        decision._SHORT_CACHE.clear()

    def test_a_stored_verdict_is_the_shortlist(self):
        con = _world()
        cands = [_cand("SMH", "AI", markable=True), _cand("CIBR", "AI", markable=True)]
        _stored_run(con, cands=cands, verdict=["pool:CIBR"])
        sl = decision.period_shortlist(_P(con), con, "r1", "2026-09-09")
        self.assertEqual(sl["source"], "verdict")
        self.assertEqual([it["instrument_id"] for it in sl["items"]], ["CIBR"])
        self.assertEqual(sl["items"][0]["ai_reason"], "CIBR 的理由第一句。")

    def test_without_a_verdict_it_is_computed_and_says_so(self):
        con = _world()
        cands = [_cand("SMH", "AI", markable=True, methods=("a", "b")),
                 _cand("LUSTALE", "AI", markable=True, methods=("a", "b", "c"))]
        _stored_run(con, cands=cands)
        sl = decision.period_shortlist(_P(con), con, "r1", "2026-09-09")
        self.assertEqual(sl["source"], "computed")
        # the stale fund outranks on consensus but may not enter
        self.assertEqual([it["instrument_id"] for it in sl["items"]], ["SMH"])

    def test_the_panel_reads_the_backend_rank_not_its_own(self):
        html = (ROOT / "web" / "dash.html").read_text(encoding="utf-8")
        start = html.index("function poolShortlistSet")
        body = html[start:html.index("\n}", start)]
        self.assertIn("shortlist_rank", body)
        self.assertNotIn("ev_c", body)
        self.assertNotIn("var SHORTLIST_N=6", html)

    def test_the_period_block_carries_ranks_onto_the_pool(self):
        src = (ROOT / "ideagen" / "review.py").read_text(encoding="utf-8")
        self.assertIn("_dec.panel_block(p, con, rid", src)


class TheTicket(unittest.TestCase):
    def setUp(self):
        decision._SHORT_CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["IDEAGEN_PM_REVIEWS_FILE"] = str(Path(self._tmp.name) / "j.jsonl")

    def tearDown(self):
        os.environ.pop("IDEAGEN_PM_REVIEWS_FILE", None)
        self._tmp.cleanup()

    def test_equal_weight_floor_shares_and_a_strike_takes_weight_zero(self):
        con = _world()
        cands = [_cand(k, f"t{i}", markable=True) for i, k in enumerate(("SMH", "CIBR", "COPX"))]
        _stored_run(con, cands=cands, verdict=[c["id"] for c in cands])
        p = _P(con)
        t = decision.ticket(p, con, "2026-09-09")
        self.assertEqual(len(t["rows"]), 3)
        sleeve = config.MODEL_PORTFOLIO_NOTIONAL * config.SATELLITE_SLEEVE_PCT
        r0 = t["rows"][0]
        self.assertAlmostEqual(r0["amount"], sleeve / 3, places=2)
        self.assertAlmostEqual(r0["weight_total"], config.SATELLITE_SLEEVE_PCT / 3, places=5)
        self.assertEqual(r0["est_shares"], int((sleeve / 3) // r0["last_px"]))
        self.assertEqual(r0["pm_decision"], "未决定")
        obj, st = decision.submit_review(
            p, con, {"as_of": "2026-09-09", "instrument_id": "CIBR", "decision": "否决",
                     "reject_category": "事件风险", "reason": "财报前不追"},
            reviewer="jon", role="member")
        self.assertEqual(st, 200, obj)
        t = decision.ticket(p, con, "2026-09-09")
        rows = {r["code"]: r for r in t["rows"]}
        self.assertEqual(rows["US.CIBR"]["amount"], 0)
        self.assertEqual(rows["US.CIBR"]["pm_decision"], "否决")
        self.assertAlmostEqual(rows["US.SMH"]["amount"], sleeve / 2, places=2)
        csv = decision.ticket_csv(t)
        self.assertTrue(csv.startswith("﻿代码") or csv.startswith("﻿序号"))
        self.assertIn("PM 决定", csv)

    def test_at_most_ten_lines(self):
        con = _world()
        cands = [_cand("SMH", f"t{i}", markable=True) | {"id": f"pool:{i}"} for i in range(12)]
        _stored_run(con, cands=cands, verdict=[c["id"] for c in cands])
        t = decision.ticket(_P(con), con, "2026-09-09")
        self.assertEqual(len(t["rows"]), config.TICKET_BATCH_MAX)
        self.assertEqual(t["truncated"], 2)


class PmReviewsOnlyStrikeAndAreSigned(unittest.TestCase):
    def setUp(self):
        decision._SHORT_CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self._tmp.name) / "pm_reviews.jsonl"
        os.environ["IDEAGEN_PM_REVIEWS_FILE"] = str(self.journal)
        self.con = _world()
        _stored_run(self.con, cands=[_cand("SMH", "AI", markable=True),
                                     _cand("COPX", "X", markable=True)],
                    verdict=["pool:SMH"])
        self.p = _P(self.con)

    def tearDown(self):
        os.environ.pop("IDEAGEN_PM_REVIEWS_FILE", None)
        self._tmp.cleanup()

    def _post(self, reviewer="jon", role="member", **kw):
        body = {"as_of": "2026-09-09", "instrument_id": "SMH", "decision": "采纳",
                "reason": "", **kw}
        return decision.submit_review(self.p, self.con, body, reviewer=reviewer, role=role)

    def test_unsigned_and_read_only_callers_are_refused(self):
        self.assertEqual(self._post(reviewer=None)[1], 403)
        self.assertEqual(self._post(role="viewer")[1], 403)
        self.assertEqual(self._post(role=None)[1], 403)
        self.assertEqual(self._post(role="admin")[1], 200)

    def test_a_name_not_on_the_shortlist_is_refused(self):
        obj, st = self._post(instrument_id="COPX")
        self.assertEqual(st, 400)
        self.assertIn("不往里加名字", obj["error"])

    def test_a_strike_needs_a_category_and_a_sentence(self):
        self.assertEqual(self._post(decision="否决", reason="x")[1], 400)
        self.assertEqual(self._post(decision="否决", reject_category="宏观敏感度")[1], 400)
        self.assertEqual(self._post(decision="否决", reject_category="宏观敏感度",
                                    reason="利率敏感")[1], 200)
        self.assertEqual(self._post(decision="加入")[1], 400)

    def test_resubmitting_updates_and_keeps_created_at(self):
        a, _ = self._post()
        b, _ = self._post(decision="观望", reason="等财报")
        rows = decision.reviews(self.con, "2026-09-09")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "观望")
        self.assertEqual(rows[0]["created_at"], a["review"]["created_at"])

    def test_the_journal_brings_reviews_back_after_a_snapshot_swap(self):
        self._post(decision="观望", reason="等财报")
        self.assertTrue(self.journal.exists())
        # the display node's database is replaced by a snapshot without the row
        fresh = _world()
        rows = decision.reviews(fresh, "2026-09-09")
        self.assertEqual([r["decision"] for r in rows], ["观望"])

    def test_merge_is_newest_wins_in_any_order(self):
        old = {"as_of": "2026-09-09", "instrument_id": "SMH", "reviewer": "jon",
               "decision": "采纳", "updated_at": "2026-09-10T01:00:00+00:00"}
        new = {**old, "decision": "否决", "reject_category": "其他", "reason": "r",
               "updated_at": "2026-09-10T02:00:00+00:00"}
        for order in ((old, new), (new, old)):
            con = db.init(":memory:")
            decision.merge_rows(con, order)
            decision.merge_rows(con, order)      # idempotent
            got = [dict(r) for r in con.execute("SELECT decision FROM pm_reviews")]
            self.assertEqual(got, [{"decision": "否决"}])

    def test_pull_merges_the_export(self):
        rows = [{"as_of": "2026-09-09", "instrument_id": "SMH", "reviewer": "佳琦",
                 "decision": "观望", "updated_at": "2026-09-10T00:00:00+00:00"}]

        class _H(__import__("http.server").server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.seen_key = self.headers.get("X-Dash-Key")
                body = json.dumps({"reviews": rows}).encode()
                self.send_response(200 if self.path == "/api/pm_reviews/export" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        import http.server
        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            con = db.init(":memory:")
            old = {k: os.environ.pop(k, None) for k in ("http_proxy", "HTTP_PROXY")}
            os.environ["no_proxy"] = "127.0.0.1"
            try:
                rep = decision.pull_reviews(con, url=f"http://127.0.0.1:{srv.server_address[1]}",
                                            key="k")
            finally:
                for k, v in old.items():
                    if v is not None:
                        os.environ[k] = v
            self.assertEqual(rep["inserted"], 1)
            self.assertEqual(con.execute("SELECT reviewer FROM pm_reviews").fetchone()[0], "佳琦")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_reviews_travel_in_the_snapshot_and_the_sync_pulls_them_first(self):
        seed = (ROOT / "scripts" / "seed_cloud_state.py").read_text(encoding="utf-8")
        self.assertIn('"pm_reviews"', seed)
        sync = (ROOT / "scripts" / "sync_to_cloud.py").read_text(encoding="utf-8")
        self.assertLess(sync.index('out["pm_reviews"] = pull_pm_reviews'),
                        sync.index("fp = content_fingerprint()", sync.index("def data_leg")))


class Outcomes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["IDEAGEN_PM_REVIEWS_FILE"] = str(Path(self._tmp.name) / "j.jsonl")

    def tearDown(self):
        os.environ.pop("IDEAGEN_PM_REVIEWS_FILE", None)
        self._tmp.cleanup()

    def test_grouped_by_the_desk_decision_small_samples_say_so(self):
        con = _world()
        _prices(con, "US.SMH", [f"2026-09-{d:02d}" for d in range(17, 31)]
                + [f"2026-10-{d:02d}" for d in range(1, 12)], start=200.0, step=1.0)
        base = {"as_of": "2026-09-01", "updated_at": "2026-09-02T00:00:00+00:00"}
        decision.merge_rows(con, [
            {**base, "instrument_id": "SMH", "reviewer": "a", "decision": "采纳"},
            # a second reviewer later strikes it: the desk's decision is the newer one
            {**base, "instrument_id": "SMH", "reviewer": "b", "decision": "否决",
             "reject_category": "其他", "reason": "r",
             "updated_at": "2026-09-03T00:00:00+00:00"},
            {**base, "instrument_id": "CIBR", "reviewer": "a", "decision": "采纳"},
        ])
        out = decision.review_outcomes(con)
        g = {x["decision"]: x for x in out["groups"]}
        self.assertEqual(g["否决"]["n"], 1)
        self.assertGreater(g["否决"]["mean_ret_pct"], 0)
        self.assertEqual(g["采纳"]["n"] + g["采纳"]["pending"], 1)
        self.assertEqual(out["state"], "insufficient")
        self.assertFalse(g["否决"]["enough"])


class ServerRefusesUnsignedWrites(unittest.TestCase):
    """The POST route, over a real socket: remote without a session is refused
    before any database is opened."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._prev = {k: os.environ.get(k) for k in
                     ("IDEAGEN_ACCOUNTS_FILE", "IDEAGEN_DASH_KEY", "IDEAGEN_ACCOUNTS_MIRROR")}
        os.environ["IDEAGEN_ACCOUNTS_FILE"] = str(Path(cls._tmp.name) / "accounts.json")
        os.environ["IDEAGEN_DASH_KEY"] = "k-decision"
        os.environ.pop("IDEAGEN_ACCOUNTS_MIRROR", None)
        from ideagen.serve import Handler, Server
        cls.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for k, v in cls._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    def _post(self, headers):
        req = urllib.request.Request(
            self.base + "/api/pm_review", method="POST",
            data=json.dumps({"as_of": "2026-09-09", "instrument_id": "SMH",
                             "decision": "采纳"}).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base,
                     "X-Forwarded-For": "203.0.113.9", **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            r = opener.open(req, timeout=30)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_remote_without_key_or_session(self):
        self.assertEqual(self._post({})[0], 401)

    def test_remote_with_only_the_shared_key_cannot_sign(self):
        st, body = self._post({"X-Dash-Key": "k-decision"})
        self.assertEqual(st, 403, body)
        self.assertIn("署名", body)

    def test_reviewer_identity(self):
        from ideagen.serve import Handler

        class Stub:
            headers = {"X-Forwarded-For": "1.2.3.4"}
            client_address = ("127.0.0.1", 1)

            def __init__(self, who, role):
                self._who, self._role = who, role

            def _session_user(self):
                return self._who

            def _acct(self):
                role = self._role
                return type("A", (), {"role": staticmethod(lambda n: role)})

        self.assertEqual(Handler._reviewer(Stub("jon", "member")), ("jon", "member"))
        self.assertEqual(Handler._reviewer(Stub("yifu", "viewer")), ("yifu", "viewer"))
        self.assertEqual(Handler._reviewer(Stub(None, None)), (None, None))
        local = Stub(None, None)
        local.headers = {}
        self.assertEqual(Handler._reviewer(local), ("本机", "admin"))


if __name__ == "__main__":
    unittest.main()
