"""筛选A 体检（`ideagen/theme_audit.py`）的契约。内存库 + 夹具，不联网。

盯住五件事：
1. 时点：首次提及 / 首次入选 / 滞后天数 / 入选前涨幅都从夹具里算得出来，
   入选以「该期算数的那次运行」为准，失败重试的判决不算。
2. 口径：别名晚于研报就不命中；标题口径不受正文长度影响。
3. 窗口：入选前后共用周二收盘作基点；post 窗口没走满时明说「窗口未满」。
4. 放波动：入选组波动放大的夹具判「显著」；样本不够判「样本不足」，不配颜色。
5. 截断：补跑里披露晚于周三 07:00 的研报算违例；实时运行里晚于运行开始的
   研报不算（运行读不到），而且读法里要说出来。
"""
from __future__ import annotations

import json
import math
import os
import random
import unittest
from datetime import date, timedelta
from unittest import mock

os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import config, db, lexicon, theme_audit as ta  # noqa: E402

AIM = "AI-MONETISATION"
CAPEX = "AI-CAPEX"


def _bdays(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _prices(con, code: str, days: list[str], vol_before: float, vol_after: float,
            switch: str, seed: int, drift: float = 0.0) -> None:
    rng = random.Random(seed)
    px = 100.0
    for d in days:
        sd = vol_before if d < switch else vol_after
        px *= 1 + drift + rng.gauss(0, sd)
        con.execute("INSERT INTO prices(code,d,close) VALUES (?,?,?)", (code, d, px))


def _run(con, as_of: str, run_id: str, chosen: list[str], scores: dict,
         ok: int = 1, started: str | None = None, cls: str = "backfill",
         counting: list[str] | None = None) -> None:
    con.execute("INSERT INTO orch_runs(run_id,as_of,kind,ok,started_at,data_classification) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, as_of, "weekly", ok, started or f"{as_of}T20:00:00+00:00", cls))
    con.execute("INSERT INTO verdicts(run_id,as_of,kind,strategy,version,chosen,scores) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, as_of, "topic_scorer", "hgep", "1.0", json.dumps(chosen),
                 json.dumps(scores)))
    con.execute("INSERT INTO verdicts(run_id,as_of,kind,strategy,version,chosen,scores) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, as_of, "topic_scorer", "counting", "1.0",
                 json.dumps(counting or []), "{}"))


def _doc(con, doc_id: str, published_at: str, title: str, body: str = "",
         summary: str = "") -> None:
    con.execute("INSERT INTO documents(doc_id,line,tier,title,published_at,published_d,"
                "ingested_at,summary,body,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (doc_id, "feed", 2, title, published_at, published_at[:10],
                 "2026-09-10T00:00:00+08:00", summary, body, "h" + doc_id))


def _sc(score, G=50.0, P=50.0):
    return {"score": score, "H": 50.0, "G": G, "E": 50.0, "P": P}


class Windows(unittest.TestCase):
    def test_pre_and_post_share_the_tuesday_close(self):
        con = db.init(":memory:")
        days = _bdays(date(2026, 5, 1), date(2026, 9, 30))
        for i, d in enumerate(days):
            con.execute("INSERT INTO prices(code,d,close) VALUES ('US.X',?,?)", (d, 100 + i))
        w = ta.window_stats(con, "US.X", "2026-07-15", 21)
        self.assertEqual(w["base_d"], "2026-07-14")          # 周三 07:00 看到的是周二收盘
        self.assertTrue(w["complete"])
        i0 = days.index("2026-07-14")
        self.assertAlmostEqual(w["pre_ret"], (100 + i0) / (100 + i0 - 21) - 1)
        self.assertAlmostEqual(w["post_ret"], (100 + i0 + 21) / (100 + i0) - 1)

    def test_an_unfinished_window_says_so(self):
        con = db.init(":memory:")
        for i, d in enumerate(_bdays(date(2026, 5, 1), date(2026, 7, 20))):
            con.execute("INSERT INTO prices(code,d,close) VALUES ('US.X',?,?)", (d, 100 + i))
        w = ta.window_stats(con, "US.X", "2026-07-15", 21)
        self.assertFalse(w["complete"])
        self.assertIn("窗口未满", w["why"])

    def test_missing_history_is_named(self):
        con = db.init(":memory:")
        w = ta.window_stats(con, "US.NONE", "2026-07-15", 21)
        self.assertIsNone(w["pre_ret"])
        self.assertIn("缺数据", w["why"])


class Timing(unittest.TestCase):
    def setUp(self):
        self.con = con = db.init(":memory:")
        days = _bdays(date(2026, 5, 1), date(2026, 9, 30))
        for code in {lexicon.THEME_BY_ID[AIM].price_indicator,
                     lexicon.THEME_BY_ID[CAPEX].price_indicator}:
            _prices(con, code, days, 0.01, 0.01, "2026-01-01", seed=len(code), drift=0.003)
        # 研报：软件主题 06-10 起就有人谈，07-01 那一周密集；另有一篇只有正文命中的。
        _doc(con, "d1", "2026-06-10T09:00:00+08:00", "SaaS 与云订阅收入拐点")
        for i in range(12):
            _doc(con, f"w{i}", f"2026-07-0{1 + i % 5}T09:00:00+08:00",
                 f"软件股 ARR 与云业务第{i}篇")
        _doc(con, "bodyonly", "2026-06-20T09:00:00+08:00", "美股周报",
             body="本周 SaaS 公司订阅续费强劲" + "。" * 420)
        # 07-08 期跑了两次：失败那次选了它，成功那次没选——只有成功那次算。
        _run(con, "2026-07-08", "r-bad", [AIM], {AIM: _sc(80)}, ok=0,
             started="2026-07-08T01:00:00+00:00")
        _run(con, "2026-07-08", "r-ok", [CAPEX], {AIM: _sc(40), CAPEX: _sc(70)},
             started="2026-07-08T02:00:00+00:00", counting=[AIM])
        _run(con, "2026-07-29", "r-729", [AIM], {AIM: _sc(70), CAPEX: _sc(40)})
        con.execute("INSERT INTO themes(as_of,theme_id,label,tis,tier,b) VALUES "
                    "('2026-07-20',?,'x',65,'important',40)", (AIM,))

    def test_selection_follows_the_canonical_run_only(self):
        runs = ta.canonical_runs(self.con)
        self.assertEqual([r["run_id"] for r in runs], ["r-ok", "r-729"])
        sel = ta.selected_index(self.con, runs)
        self.assertEqual(sel[AIM], ["2026-07-29"])

    def test_record_has_the_three_dates_and_the_lag(self):
        t = ta.theme_timing(self.con)
        rec = next(r for r in t["themes"] if r["theme_id"] == AIM)
        self.assertEqual(rec["first_mention"]["doc_id"], "d1")
        self.assertEqual(rec["first_selected"], "2026-07-29")
        self.assertEqual(rec["first_strong_d"], "2026-07-20")
        self.assertEqual(rec["first_counting_d"], "2026-07-08")
        self.assertEqual(rec["lag_days"]["mention"], 49)
        self.assertEqual(rec["lag_days"]["counting"], 21)
        self.assertGreater(rec["pre"]["pre_ret"], 0)       # 夹具每日漂移 +0.3%
        self.assertIn("首次入选", rec["verdict"])
        self.assertIn("已涨", rec["verdict"])
        # 研报库起点就是 06-10，首次提及被截断——读法里必须说
        self.assertTrue(rec["truncated_by_corpus"])
        self.assertIn("截断", rec["verdict"])

    def test_title_basis_ignores_body_only_hits(self):
        m = ta.mention_index(self.con)
        self.assertIn("bodyonly", m[AIM]["full"])
        self.assertNotIn("bodyonly", m[AIM]["title"])

    def test_case_study_reports_overlap_and_p_neutral(self):
        t = ta.theme_timing(self.con)
        m = ta.mention_index(self.con)
        c = ta.case_study(self.con, t, m)
        wk = {w["as_of"]: w for w in c["weekly"]}
        self.assertTrue(wk["2026-07-29"]["selected"])
        self.assertTrue(wk["2026-07-08"]["neighbour_selected"])
        # score 40, P 50 → 中性 P 下得分不变
        self.assertEqual(wk["2026-07-08"]["score_p_neutral"], 40.0)
        self.assertIsNotNone(c["run_up"])

    def test_an_alias_dated_after_the_report_does_not_match_it(self):
        th = lexicon.Theme(id="ZZ-TEST", label="测试", key_question="?",
                           terms=("完全不会出现的词",), price_indicator="US.X")
        con = db.init(":memory:")
        _doc(con, "a1", "2026-07-01T09:00:00+08:00", "冷门新叫法出现")
        _doc(con, "a2", "2026-07-10T09:00:00+08:00", "冷门新叫法再出现")
        alias = ({"theme_id": "ZZ-TEST", "terms": ("冷门新叫法",), "as_of": "2026-07-05",
                  "evidence_doc_ids": ()},)
        with mock.patch.object(lexicon, "ALIASES", alias):
            m = ta.mention_index(con, [th])
        self.assertEqual(set(m["ZZ-TEST"]["title"]), {"a2"})


class VolValidation(unittest.TestCase):
    def _fixture(self, n_themes: int):
        con = db.init(":memory:")
        days = _bdays(date(2026, 4, 1), date(2026, 9, 30))
        ids = [t.id for t in lexicon.SEED_THEMES[:n_themes]]
        half = n_themes // 2
        for i, tid in enumerate(ids):
            code = lexicon.THEME_BY_ID[tid].price_indicator
            if con.execute("SELECT 1 FROM prices WHERE code=?", (code,)).fetchone():
                continue
            # 入选的一半：之前平静、之后剧烈；落选的一半：前后一样
            after = 0.03 if i < half else 0.008
            _prices(con, code, days, 0.008, after, "2026-07-15", seed=i)
            con.execute("INSERT INTO themes(as_of,theme_id,label,tis,tier,b) VALUES "
                        "('2026-07-14',?,'x',50,'watch',?)", (tid, 90.0 if i < half else 10.0))
        scores = {tid: _sc(60 - i) for i, tid in enumerate(ids)}
        _run(con, "2026-07-15", "r1", ids[:half], scores)
        _run(con, "2026-09-23", "r2", ids[:half], scores)      # 窗口必然未满
        return con

    def test_selected_themes_that_then_move_read_as_significant(self):
        con = self._fixture(8)
        with mock.patch.object(config, "THEME_AUDIT_MIN_GROUP_N", 3):
            v = ta.vol_validation(con, n_boot=400)
        s = v["selected_vs_not"]
        self.assertEqual(v["periods_complete"], ["2026-07-15"])
        self.assertIn("2026-09-23", v["periods_incomplete"])
        self.assertGreater(s["diff"], 0)
        self.assertGreater(s["ci95"][0], 0)
        self.assertEqual(s["state"], "held")
        self.assertGreater(v["disagreement"]["B"]["rho"], 0)
        # 期内去中位数后，同一期全部行的中位数是 0
        dm = sorted(r["vol_ratio_log_dm"] for r in v["rows"] if r.get("vol_ratio_log_dm") is not None)
        self.assertAlmostEqual((dm[len(dm) // 2 - 1] + dm[len(dm) // 2]) / 2, 0.0, places=3)

    def test_too_few_rows_is_sample_insufficient_not_a_verdict(self):
        con = self._fixture(8)
        v = ta.vol_validation(con, n_boot=200)       # 默认门槛 10，夹具每组只有 4
        self.assertEqual(v["selected_vs_not"]["state"], "open")
        self.assertEqual(v["disagreement"]["B"]["state"], "open")

    def test_spearman_basics(self):
        self.assertAlmostEqual(ta.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(ta.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(ta.spearman([1, 2], [1, 2]))


class Cutoff(unittest.TestCase):
    def test_backfill_counts_late_reports_and_live_excludes_unreadable_ones(self):
        con = db.init(":memory:")
        # 回填期 07-29：一篇 06:59、一篇 07:01、一篇前一天
        _doc(con, "b-early", "2026-07-29T06:59:00+08:00", "早")
        _doc(con, "b-late", "2026-07-29T07:01:00+08:00", "晚")
        _doc(con, "b-prev", "2026-07-28T22:00:00+08:00", "前一天")
        _run(con, "2026-07-29", "bf", [], {}, cls="backfill",
             started="2026-09-06T18:00:00+00:00")
        con.execute("INSERT INTO feed_runs(run_id,feed,kind,as_of,n_rows,ok) "
                    "VALUES ('bf','wisburg','corpus','2026-07-29',3,1)")
        # 实时期 09-16：运行 07:33 开始；07:10 那篇是真违例，09:00 那篇运行读不到
        _doc(con, "l-ok", "2026-09-16T06:00:00+08:00", "早")
        _doc(con, "l-seen", "2026-09-16T07:10:00+08:00", "开跑前")
        _doc(con, "l-after", "2026-09-16T09:00:00+08:00", "开跑后")
        _run(con, "2026-09-16", "lv", [], {}, cls="live",
             started="2026-09-15T23:33:56+00:00")
        con.execute("INSERT INTO feed_runs(run_id,feed,kind,as_of,n_rows,ok) "
                    "VALUES ('lv','wisburg','corpus','2026-09-16',2,1)")
        a = ta.cutoff_audit(con)
        bf, lv = a["runs"]
        self.assertEqual((bf["n_docs"], bf["violations"], bf["violations_seen"]), (3, 1, 1))
        self.assertTrue(bf["reconstructed_matches"])
        self.assertEqual(bf["samples"][0]["doc_id"], "b-late")
        self.assertIn("真违例", bf["reading"])
        self.assertEqual((lv["violations"], lv["after_start"], lv["violations_seen"]), (2, 1, 1))
        self.assertFalse(lv["reconstructed_matches"])
        self.assertIn("上界", lv["reading"])
        self.assertEqual(a["totals"]["violations_seen"], 2)

    def test_cutoff_is_wednesday_seven_hkt(self):
        c = ta.cutoff_of("2026-09-16")
        self.assertEqual((c.hour, c.minute, c.utcoffset()), (7, 0, timedelta(hours=8)))


class StoreAndLoad(unittest.TestCase):
    def test_run_all_round_trips_through_kv(self):
        con = db.init(":memory:")
        _doc(con, "x", "2026-07-01T09:00:00+08:00", "软件 SaaS")
        _run(con, "2026-07-08", "r", [], {AIM: _sc(40)})
        ta.run_all(con, n_boot=50)
        got = ta.load(con)
        self.assertEqual(got["timing"]["version"], ta.VERSION)
        self.assertIn("selected_vs_not", got["vol"])
        self.assertEqual(len(got["cutoff"]["runs"]), 1)
        self.assertTrue(math.isfinite(len(got["timing"]["themes"])))


class PanelBlock(unittest.TestCase):
    """`review.theme_audit_block`：没跑过要说没跑过，跑过要把面板读的字段都带上。"""

    def test_never_run_names_itself(self):
        from ideagen import review
        b = review.theme_audit_block(db.init(":memory:"))
        self.assertFalse(b["available"])
        self.assertIn("缺数据", b["timing_why"])
        self.assertIn("缺数据", b["vol"]["why"])
        self.assertIn("缺数据", b["cutoff"]["why"])

    def test_after_a_run_the_panel_fields_are_there(self):
        from ideagen import review
        con = db.init(":memory:")
        _doc(con, "x", "2026-07-01T09:00:00+08:00", "软件 SaaS")
        _run(con, "2026-07-08", "r", [AIM], {AIM: _sc(40, G=90)})
        ta.run_all(con, n_boot=50)
        b = review.theme_audit_block(con)
        self.assertTrue(b["available"])
        t = b["themes"][AIM]
        for k in ("first_mention_d", "first_selected", "lag_days", "pre", "verdict",
                  "family", "lineage", "pre_selection_peak", "first_strong_d"):
            self.assertIn(k, t)
        self.assertEqual(t["family"], [AIM])
        self.assertNotIn("rows", b["vol"])          # 明细不进每分钟轮询的状态文档
        self.assertEqual(b["badge_min"], config.THEME_DISAGREE_BADGE_MIN)


if __name__ == "__main__":
    unittest.main()
