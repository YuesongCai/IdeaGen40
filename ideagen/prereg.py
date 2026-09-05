"""Pre-registered forward tests: what would count as this working, decided now.

Every finding in this repository currently ends at the same sentence — "worth
running forward for a while". That sentence is only worth anything if what
counts as surviving is written down **before** the data arrives. Otherwise the
next round picks the estimator that happens to look best, which is the fourth
sin with extra steps: the search continues, invisibly, in the choice of how to
measure.

So each entry here fixes, in advance and in one place:

* the claim, in words a person can disagree with;
* the **estimator** — a path into the backtest summary, not a description, so
  nobody has to decide later which number was meant;
* the decision rule and its direction;
* how many live periods must accrue before the question may be asked at all.

The estimator matters more than the threshold, and the choice made here is the
one this repository's own falsification produced: the raw quintile ladder is
*not* the estimator, because the ladder is real and made of risk. What has to
hold forward is the volatility-controlled partial correlation and the
within-family correlation — the two that collapsed in sample.

Entries are append-only. Nothing here stops someone editing one, but `git log`
on this file is the audit trail, and `tests/test_prereg.py` fails when an entry
loses its required fields or points at an estimator the backtest does not
produce — which is the failure that would otherwise be silent.
"""

from __future__ import annotations

from typing import Any, Sequence

#: `path` is a dotted lookup into `backtest_runs.summary`. `rule` is one of
#: "gte" / "lte" applied to `threshold`. `min_live_periods` is the number of
#: live (not backfilled) periods that must exist after `registered_on` before
#: the entry may be read at all.
REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "id": "ev_rank_partial_vs_vol",
        "registered_on": "2026-09-06",
        "claim": "期望值分数在按住入场前波动之后，仍然能排出次序",
        "path": "ranking_power.partial_vs_volatility.mean_rho_partial",
        "rule": "gte",
        "threshold": 0.05,
        "min_live_periods": 8,
        "why_this_estimator": (
            "原始分位阶梯不作数：它是真的（Q5−Q1 +2.68%/期，区间 [+1.70, +3.75] "
            "不含 0），但它由风险构成——同一段样本里控住波动之后只剩 +0.005，"
            "区间 [−0.175, +0.218]。所以往前要检验的是控波动之后那个数。"),
        "in_sample_value": 0.0046,
    },
    {
        "id": "ev_rank_within_family",
        "registered_on": "2026-09-06",
        "claim": "期望值分数在同一个风险家族内部也能排出次序",
        "path": "ranking_power.within_family.families.股票.mean_rho",
        "rule": "gte",
        "threshold": 0.05,
        "min_live_periods": 8,
        "why_this_estimator": (
            "整张货架一起排 +0.231，家族内部最高只剩 +0.076——把商品排在国库券"
            "之上不是选股力。股票是家族里样本最厚的一个（每期中位 37 只），"
            "所以拿它当那条要往前检验的线。"),
        "in_sample_value": 0.0746,
    },
    {
        "id": "selection_beats_random",
        "registered_on": "2026-09-06",
        "claim": "「按此前成绩挑组合」这个动作，胜过随机挑一条",
        "path": "tearsheet.walk_forward.no_posthoc.edge_vs_all_arms",
        "rule": "gte",
        "threshold": 0.0,
        "min_live_periods": 8,
        "why_this_estimator": (
            "样本内是 −0.41%/期：跟随领先者跑输随机挑一条。这条不是关于某个"
            "组合的，是关于「挑」这件事本身值不值得做。"),
        "in_sample_value": -0.004093,
    },
)


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate(summary: dict[str, Any] | None,
             *, live_periods_since: int | None = None,
             entries: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Where each pre-registered test stands, without deciding early.

    `live_periods_since` is how many live periods have accrued since
    registration. Below `min_live_periods` the entry reports `未到期` and its
    current value is shown **greyed out rather than judged** — reading a verdict
    off two periods is the same mistake the registration exists to prevent, and
    the number is shown at all only because hiding it invites the same person to
    go compute it themselves.
    """
    rows = []
    for e in (entries or REGISTRY):
        value = _dig(summary or {}, str(e["path"]))
        have = live_periods_since
        due = (have is not None and have >= int(e["min_live_periods"]))
        passed = None
        if value is not None and due:
            passed = (value >= e["threshold"] if e["rule"] == "gte"
                      else value <= e["threshold"])
        rows.append({
            "id": e["id"],
            "claim": e["claim"],
            "path": e["path"],
            "threshold": e["threshold"],
            "rule": e["rule"],
            "registered_on": e["registered_on"],
            "min_live_periods": e["min_live_periods"],
            "live_periods_since": have,
            "value": value,
            "in_sample_value": e.get("in_sample_value"),
            "status": ("没有这个数" if value is None
                       else "未到期" if not due
                       else "通过" if passed else "未通过"),
        })
    return {
        "entries": rows,
        "n_due": sum(1 for r in rows if r["status"] in ("通过", "未通过")),
        "note": (
            "预注册：在数据到来之前就写死「什么算成立」。估计量用路径写死而不是"
            "用文字描述，是因为下一轮真正的自由度不在门槛上，而在「用哪个数」——"
            "谁都可以在结果出来之后挑一个更好看的估计量，那仍然是数据窥探，"
            "只是换了个地方。到期之前只报数不判定。"),
    }
