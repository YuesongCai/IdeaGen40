"""运行成本（WS-E）。

最重要的一条是白名单：`ve` 的动作是真执行，这个仓曾经把 Delete 当探测、删掉过一个
真的数据库。所以非白名单动作必须在**拼命令行、调用 runner 之前**就被拒绝。其余测
解析（输出前夹着通知）、项目归属、失败不覆盖上一份成功的数、卡片口径。不联网。
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import costs, db  # noqa: E402


def _resp(rows, total=None):
    return json.dumps({"ResponseMetadata": {"Action": "x"},
                       "Result": {"List": rows, "Total": total if total is not None else len(rows)}})


OVERVIEW = [
    {"Product": "ECS", "OriginalBillAmount": "62.9", "CouponAmount": "0", "PayableAmount": "46.40"},
    {"Product": "RDS for MySQL", "OriginalBillAmount": "32.49", "CouponAmount": "0", "PayableAmount": "31.76"},
    {"Product": "AgentKit", "OriginalBillAmount": "69.12", "CouponAmount": "69.12", "PayableAmount": "0"},
    {"Product": "EIP", "OriginalBillAmount": "6.36", "CouponAmount": "0", "PayableAmount": "6.35"},
]
DETAIL = [
    {"Product": "ECS", "InstanceName": "ideagen-live9", "InstanceNo": "i-1", "OriginalBillAmount": "8.9",
     "CouponAmount": "0", "PayableAmount": "8.07"},
    {"Product": "ECS", "InstanceName": "nexus-card-v7", "InstanceNo": "i-2", "OriginalBillAmount": "54.0",
     "CouponAmount": "0", "PayableAmount": "38.33"},
    {"Product": "RDS for MySQL", "InstanceName": "ideagen-prod-mysql", "InstanceNo": "mysql-1",
     "OriginalBillAmount": "32.49", "CouponAmount": "0", "PayableAmount": "31.76"},
    {"Product": "AgentKit", "InstanceName": "", "InstanceNo": "r-1", "OriginalBillAmount": "69.12",
     "CouponAmount": "69.12", "PayableAmount": "0"},
    {"Product": "EIP", "InstanceName": "eip-xyz", "InstanceNo": "eip-xyz", "OriginalBillAmount": "6.36",
     "CouponAmount": "0", "PayableAmount": "6.35"},
]


class FakeVe:
    def __init__(self, fail=False, notice=True):
        self.calls, self.fail, self.notice = [], fail, notice

    def __call__(self, argv):
        self.calls.append(argv)
        if self.fail:
            raise costs.VeError("network down")
        body = _resp(OVERVIEW) if argv[2] == "ListBillOverviewByProd" else _resp(DETAIL)
        # the CLI sometimes prints a JSON notice before the payload
        return ('{"_notice": "upgrade available"}\n' + body) if self.notice else body


@pytest.mark.parametrize("service,action", [
    ("billing", "DeleteBill"),
    ("rds_mysql", "DeleteDBInstance"),
    ("ecs", "DescribeInstances"),          # read-looking, still not on the list
    ("ecs", "StopInstance"),
    ("billing", "listbilldetail"),         # case matters: not the same action
])
def test_non_whitelisted_actions_are_refused_before_anything_runs(service, action):
    fake = FakeVe()
    with pytest.raises(costs.ForbiddenAction):
        costs.ve_call(service, action, {"BillPeriod": "2026-09"}, runner=fake)
    assert fake.calls == []


def test_whitelist_is_exactly_the_two_list_actions():
    assert costs.ALLOWED == {("billing", "ListBillOverviewByProd"), ("billing", "ListBillDetail")}
    assert all(a.startswith("List") for _, a in costs.ALLOWED)


def test_allowed_call_builds_a_read_only_command_and_parses_past_the_notice():
    fake = FakeVe()
    out = costs.ve_call("billing", "ListBillDetail", {"BillPeriod": "2026-09", "GroupTerm": 1}, runner=fake)
    assert out["Result"]["Total"] == len(DETAIL)
    argv = fake.calls[0]
    assert argv[:3] == ["ve", "billing", "ListBillDetail"]
    assert "--profile" in argv and "--region" in argv


def test_parse_raises_on_api_error_and_on_no_json():
    with pytest.raises(costs.VeError):
        costs.parse_ve_output(json.dumps({"ResponseMetadata": {"Error": {"Code": "AccessDenied"}}}))
    with pytest.raises(costs.VeError):
        costs.parse_ve_output("nothing here")


def test_project_attribution_rules():
    assert costs.project_of("ECS", "ideagen-live9", "i")[0] == "IdeaGen"
    assert costs.project_of("ECS", "nexus-card-v7", "i")[0] == "nexus-card"
    assert costs.project_of("VDB_KnowledgeBase", "", "v")[0] == "其他"
    assert costs.project_of("AgentKit", "", "r")[0] == "IdeaGen"
    assert costs.project_of("cr", "agentkit-platform-1", "cr-1")[0] == "IdeaGen"
    assert costs.project_of("EIP", "eip-abc", "eip-abc")[0] == costs.UNATTRIBUTED


def test_refresh_store_card_and_failure_keeps_last_good_snapshot():
    con = db.init(":memory:")
    today = date(2026, 9, 17)
    r = costs.refresh(con, "2026-09", runner=FakeVe(), today=today)
    assert r["ok"] and r["reconciled"]
    card = costs.card(con, today=today)
    ig = card["ideagen"]
    assert ig["payable"] == pytest.approx(8.07 + 31.76)
    assert ig["coupon"] == pytest.approx(69.12)
    assert ig["retired_payable"] == pytest.approx(31.76)
    assert ig["run_rate"] == pytest.approx((8.07 + 31.76) / 17 * 30, abs=0.01)
    assert ig["run_rate_ex_retired"] == pytest.approx(8.07 / 17 * 30, abs=0.01)
    assert [o["project"] for o in card["others"]] == ["nexus-card"]
    assert card["unattributed"]["payable"] == pytest.approx(6.35)
    assert card["claude"]["estimate"] is True
    assert {d["usd"] for d in card["data_sources"]} == {0.0}
    # a failed refresh records the error and leaves the numbers alone
    bad = costs.refresh(con, "2026-09", runner=FakeVe(fail=True), today=today)
    assert not bad["ok"]
    card2 = costs.card(con, today=today)
    assert card2["available"] and card2["ideagen"]["payable"] == ig["payable"]
    assert "network down" in card2["last_error"]


def test_daily_stage_never_raises(monkeypatch):
    con = db.init(":memory:")

    def boom(*a, **k):
        raise RuntimeError("ve not installed")
    monkeypatch.setattr(costs, "_default_runner", boom)
    note = costs.daily_stage(con, today=date(2026, 9, 2))
    assert "未刷新" in note and "2026-08" in note      # early month also retries last month
    assert costs.card(con, today=date(2026, 9, 2))["available"] is False


def test_claude_estimate_reads_calls_and_says_it_is_an_estimate():
    con = db.init(":memory:")
    con.execute("CREATE TABLE IF NOT EXISTS orch_runs (run_id TEXT PRIMARY KEY, as_of TEXT, kind TEXT, "
                "started_at TEXT, calls INTEGER)")
    con.execute("INSERT INTO orch_runs (run_id, as_of, kind, started_at, calls) VALUES "
                "('a','2026-09-09','weekly','2026-09-08T23:00:00',100), "
                "('b','2026-08-26','weekly','2026-08-25T23:00:00',999)")
    est = costs.claude_estimate(con, "2026-09")
    assert est["estimate"] is True and est["calls"] == 100
    assert est["usd"] == pytest.approx(100 * (20_000 * 5 + 3_000 * 25) / 1e6)
    assert "折算" in est["basis"]
