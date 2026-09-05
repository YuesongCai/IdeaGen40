"""预注册：在数据到来之前写死「什么算成立」。

本仓每一条发现都停在同一句话上——「值得往前跑一段看看」。那句话只有在
「什么算活下来」被提前写死时才有意义；否则下一轮会挑一个当时看起来最好的
估计量，而那仍然是数据窥探，只是换了个地方——从挑组合换成挑怎么量。

所以这里钉三件事：估计量必须是一条能取到数的路径（不是一句描述）、到期之前
不许出判定、以及路径指向的那个数后端真的会写。
"""

from __future__ import annotations

import re
from pathlib import Path

from ideagen import prereg


def test_every_entry_has_the_fields_a_promise_needs():
    for e in prereg.REGISTRY:
        for key in ("id", "registered_on", "claim", "path", "rule",
                    "threshold", "min_live_periods", "why_this_estimator"):
            assert e.get(key) is not None, f"{e.get('id')} 缺 {key}"
        assert e["rule"] in ("gte", "lte")
        assert e["min_live_periods"] >= 4, (
            f"{e['id']}: 少于四期就是不到一轮分批周期，"
            f"每条持仓都还开着，判定不该被允许发生")


def test_ids_are_unique():
    ids = [e["id"] for e in prereg.REGISTRY]
    assert len(ids) == len(set(ids))


def test_the_estimator_is_a_path_the_backend_actually_writes():
    """这条闸门抓的是「承诺指向一个不存在的数」。它不会报错，只会永远显示
    「没有这个数」，而那看起来和「还没到期」几乎一样。"""
    root = Path(__file__).resolve().parent.parent
    produced = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [root / "scripts" / "run_real_backtest.py"]
        + sorted((root / "ideagen").rglob("*.py")))
    for e in prereg.REGISTRY:
        leaf = str(e["path"]).split(".")[-1]
        assert re.search(rf"\b{re.escape(leaf)}\b", produced), (
            f"{e['id']} 指向 {e['path']}，但后端从没写过 {leaf}")


def test_nothing_is_judged_before_it_is_due():
    """到期之前只报数不判定——拿两期读一个结论，正是预注册要防的那件事。"""
    summary = {"ranking_power": {"partial_vs_volatility": {"mean_rho_partial": 0.9}}}
    early = prereg.evaluate(summary, live_periods_since=1)
    row = next(r for r in early["entries"] if r["id"] == "ev_rank_partial_vs_vol")
    assert row["status"] == "未到期" and row["value"] == 0.9
    assert early["n_due"] == 0


def test_a_due_entry_is_judged_in_the_declared_direction():
    summary = {"ranking_power": {"partial_vs_volatility": {"mean_rho_partial": 0.9}}}
    late = prereg.evaluate(summary, live_periods_since=99)
    row = next(r for r in late["entries"] if r["id"] == "ev_rank_partial_vs_vol")
    assert row["status"] == "通过"
    low = prereg.evaluate(
        {"ranking_power": {"partial_vs_volatility": {"mean_rho_partial": 0.0}}},
        live_periods_since=99)
    assert next(r for r in low["entries"]
                if r["id"] == "ev_rank_partial_vs_vol")["status"] == "未通过"


def test_a_missing_number_is_not_a_pass():
    out = prereg.evaluate({}, live_periods_since=99)
    assert {r["status"] for r in out["entries"]} == {"没有这个数"}
    assert out["n_due"] == 0


def test_the_registered_estimator_is_not_the_one_that_looked_best():
    """这条是内容审查，不是形式审查：原始分位阶梯样本内漂亮（+2.68%/期、
    区间不含 0），控波动之后只剩 +0.005。预注册必须钉在后者上，否则往前跑的
    是一条已经知道由风险构成的线。"""
    paths = {e["path"] for e in prereg.REGISTRY}
    assert "ranking_power.pooled.Q5.mean_return_pct" not in paths
    assert any("partial_vs_volatility" in p for p in paths)
    assert any("within_family" in p for p in paths)
