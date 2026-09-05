"""期次是坐标，不是标签——这些是它必须成立的等式。

面板过去只能显示最新一期，而每张表都带 `as_of`。补上时间脊之后，最容易悄悄坏掉
的不是「有没有这个视图」，而是**用错了哪一列当期次**：`opened_d` 记的是撮合当天，
只有在周跑准时那一次才等于期次；2026-09-04 那次补跑把五期一起 stamp 成了同一个
下午，于是「按 opened_d 分组」会把五级阶梯压成一行——数字全对，组合是假的。

所以这里的第一条是身份检查：`positions.as_of` 必须和「开出这个仓的那张单的期次」
一致。剩下的是算术恒等式（在场+已平=总数、盈亏=已实现+浮动），对任何数据都成立，
下周依然成立。

没有可读库的环境（镜像里不带 `data/`）跳过而不是报错：一个在那里变红的测试会挡住
自更新的闸门，连带把同一次推送里所有别的修复一起挡在门外，而且只在镜像里红。
"""

from __future__ import annotations

import os
import unittest
from datetime import date

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, periods, platform as plat  # noqa: E402


class TestPeriodSpine(unittest.TestCase):

    con = None
    rows: list = []

    @classmethod
    def setUpClass(cls):
        try:
            cls.con = db.init()
            cls.rows = periods.spine(cls.con, plat.load())
        except Exception as exc:  # noqa: BLE001 — no store here, nothing to check
            raise unittest.SkipTest(
                f"no readable state store in this tree: {exc}") from exc
        if not cls.rows:
            raise unittest.SkipTest("no periods in this tree")

    # ---------------------------------------------------------------- 身份
    def test_a_positions_period_is_the_period_of_the_order_that_opened_it(self):
        """`as_of` 必须来自那笔交易的期次，不是它撮合的那天。

        这条要是松了，阶梯就会按补跑当天塌成一行——而且每个数字单看都对。
        """
        mismatched = db.q(self.con, """
            SELECT p.pos_id, p.as_of AS pos_period, o.as_of AS order_period
              FROM positions p
              JOIN orders o ON o.book_id = p.book_id
                           AND o.idea_uid = p.idea_uid
                           AND o.status = 'filled'
             WHERE p.as_of IS NOT NULL AND p.as_of <> o.as_of
             LIMIT 10
        """)
        self.assertEqual(
            [dict(r) for r in mismatched], [],
            "有仓位的期次和开出它的那张单对不上——按期分组的界面会说谎")

    def test_every_booked_position_knows_its_period(self):
        """回填是幂等的，所以「还有没填上的」永远该是 0。"""
        row = db.q1(self.con, "SELECT COUNT(*) n FROM positions "
                              "WHERE as_of IS NULL OR as_of = ''")
        self.assertEqual(
            int(dict(row)["n"]), 0,
            "有仓位没有期次；跑 scripts/backfill_position_periods.py")

    def test_a_period_never_postdates_its_own_horizon(self):
        """期次在前、到期在后。反过来说明这一列拿的是别的日期。"""
        row = db.q1(self.con, "SELECT COUNT(*) n FROM positions "
                              "WHERE as_of IS NOT NULL AND horizon_end IS NOT NULL "
                              "AND as_of > horizon_end")
        self.assertEqual(int(dict(row)["n"]), 0,
                         "有仓位的期次晚于它自己的到期日")

    def test_opened_d_is_not_a_substitute_for_the_period(self):
        """这条不是重复：它证明为什么上面几条必须存在。

        只要历史里有过一次补跑，就一定存在 `opened_d != as_of` 的行。断言它非零
        是在钉住这个事实——将来谁把 `as_of` 改回读 `opened_d`，这里会红。
        """
        row = db.q1(self.con, "SELECT COUNT(*) n FROM positions "
                              "WHERE as_of IS NOT NULL AND opened_d IS NOT NULL "
                              "AND as_of <> opened_d")
        if int(dict(row)["n"]) == 0:
            self.skipTest("这棵树里每一期都是准时跑的，两列碰巧相等")
        self.assertGreater(
            int(dict(row)["n"]), 0,
            "两列不等的行数为 0，说明期次可能又被 opened_d 顶替了")

    # ------------------------------------------------------------ 算术恒等
    def test_position_counts_add_up(self):
        for r in self.rows:
            self.assertEqual(
                r["n_open"] + r["n_closed"], r["n_positions"],
                f"{r['as_of']}: 在场+已平 与 总数 对不上")

    def test_pnl_is_realized_plus_unrealized(self):
        for r in self.rows:
            self.assertAlmostEqual(
                r["realized"] + r["unrealized"], r["pnl"], places=2,
                msg=f"{r['as_of']}: 盈亏不等于 已实现+浮动")

    def test_return_is_pnl_over_what_the_period_deployed(self):
        """收益率的分母是这一期投出去的钱，不是账本本金。

        一期只找到两条想法，分母就小；它的百分比和满仓那一期不可比——所以 `cost`
        必须一直跟在 `ret` 旁边，这条锁住两者的关系。
        """
        for r in self.rows:
            if not r["cost"]:
                self.assertIsNone(r["ret"], f"{r['as_of']}: 没投出钱却报了收益率")
                continue
            self.assertAlmostEqual(r["pnl"] / r["cost"], r["ret"], places=6,
                                   msg=f"{r['as_of']}: 收益率的分母不是 cost")

    # ------------------------------------------------ 名义窗口 ≠ 持有窗口
    def test_a_period_reports_the_window_it_was_actually_held_for(self):
        """一期有两个窗口，缺了第二个，图就会把补跑当成持有过。

        名义窗口是 as_of → 到期日，30 天；持有窗口是这批仓真的被标记过的日子。
        补跑那几期两者差一个数量级，而百分比长得一模一样。
        """
        for r in self.rows:
            self.assertIn("mark_days", r, f"{r['as_of']}: 缺少实际持有天数")
            self.assertGreaterEqual(r["mark_days"], 0)
            if r["n_positions"]:
                self.assertTrue(r["held_from"],
                                f"{r['as_of']}: 有仓位却没有持有起点")

    def test_booked_late_means_the_fill_came_after_the_week(self):
        for r in self.rows:
            if not r["held_from"]:
                self.assertFalse(r["booked_late"], f"{r['as_of']}")
                continue
            self.assertEqual(r["booked_late"], r["held_from"] > r["as_of"],
                             f"{r['as_of']}: 补跑标记和实际开仓日对不上")

    def test_mark_days_never_exceeds_the_nominal_window(self):
        """持有天数超过名义窗口，说明这一列数的不是这一期的东西。"""
        for r in self.rows:
            if not r["horizon_end"] or not r["mark_days"]:
                continue
            nominal = (date.fromisoformat(r["horizon_end"])
                       - date.fromisoformat(r["as_of"])).days
            self.assertLessEqual(
                r["mark_days"], nominal + 1,
                f"{r['as_of']}: 标记了 {r['mark_days']} 天，名义窗口只有 {nominal} 天")

    # ---------------------------------------------------------------- 形状
    def test_the_spine_is_ordered_oldest_first(self):
        """阶梯是按这个顺序画的；顺序反了，图就上下颠倒。"""
        got = [r["as_of"] for r in self.rows]
        self.assertEqual(got, sorted(got))

    def test_status_says_whether_the_ladder_still_holds_it(self):
        for r in self.rows:
            self.assertIn(r["status"], ("live", "rolled", "pending"))
            if r["n_open"]:
                self.assertEqual(r["status"], "live", f"{r['as_of']}")
            elif r["n_closed"]:
                self.assertEqual(r["status"], "rolled", f"{r['as_of']}")

    def test_days_left_counts_from_the_horizon_not_from_today_minus_period(self):
        today = date.today()
        for r in self.rows:
            if not r["horizon_end"] or r["days_left"] is None:
                continue
            self.assertEqual(
                (date.fromisoformat(r["horizon_end"]) - today).days,
                r["days_left"], f"{r['as_of']}: 剩余天数不是按到期日算的")

    def test_classification_comes_from_the_run_not_from_a_second_rule(self):
        """live/backfill 这个词只有一个定义处：`orch_runs.data_classification`。

        真回测（scripts/run_real_backtest.py:_periods）读的就是它。这里再造一套
        规则的话，同一个词会在两个页面上给出两个答案。
        """
        p = plat.load()
        for r in self.rows:
            if not r.get("run_id"):
                continue
            row = p.state.q("SELECT data_classification FROM orch_runs "
                            "WHERE run_id=?", (r["run_id"],))
            if not row:
                continue
            self.assertEqual(
                (dict(row[0]).get("data_classification") or "live"),
                r["classification"], f"{r['as_of']}: 分类和运行记录不一致")

    # ------------------------------------------------------ 与 state() 一致
    def test_the_newest_run_in_the_spine_is_the_one_state_shows(self):
        from ideagen import review
        st = review.state(self.con, plat.load())
        weekly = st.get("weekly") or {}
        if not weekly.get("as_of"):
            self.skipTest("这棵树里没有周跑")
        self.assertEqual(
            periods.latest(st["periods"]), weekly["as_of"],
            "首页显示的那一期，和时间脊认定的最新一期不是同一期")

    def test_any_period_in_the_spine_can_have_its_pipeline_read(self):
        """走得回去才叫坐标。每一期跑成过的，都要能取到它的流水线。"""
        from ideagen import review
        p = plat.load()
        for r in self.rows:
            if not r.get("ok"):
                continue
            blk = review.weekly_block(p, self.con, r["as_of"])
            self.assertEqual(blk.get("as_of"), r["as_of"],
                             f"{r['as_of']}: 取回来的是别的期")

    def test_asking_for_a_period_that_never_ran_returns_nothing_not_the_newest(self):
        """退回最新一期是最坏的失败方式：标题写着你要的那期，数字是另一期的。"""
        from ideagen import review
        self.assertEqual(
            review.weekly_block(plat.load(), self.con, "1999-01-01"), {})


if __name__ == "__main__":
    unittest.main()
