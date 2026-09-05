"""Every ranking rule this repository has tried, including the ones it dropped.

The fourth sin is data snooping, and the paper's point about it is not that
searching is wrong — it is that the search is always larger than the model, and
the correction depends on how much larger. The deflated Sharpe ratio in `perf`
takes that count as an input, and until now the input was the literal `3` typed
at the call site with a comment admitting it was a floor. A hand-typed floor
decays in one direction only: rules get tried, the ones that fail are forgotten
because nothing writes them down, and the deflation quietly gets weaker every
time somebody searches harder.

So the count lives here, as a list of what was actually tried, and
`tests/test_trials_ledger.py` fails when a registered arm is missing from it.
That is the enforceable half: an arm cannot enter the comparison without its
trial being counted, because the test that guards the ledger is the same test
run that would publish the arm's result.

What this cannot enforce is the honesty of `status="dropped"` rows — nothing
makes anyone record a rule they tried in a notebook and abandoned. The ledger is
therefore a floor too, but a floor that goes up when the search does, and one
whose contents a reviewer can read and dispute. Entries are append-only: a rule
that was tried stays tried, whatever happened to it afterwards.
"""

from __future__ import annotations

from typing import Any

#: One entry per ranking rule ever evaluated against realised returns.
#: `status`:
#:   registered  — competing in the comparison today, under `name`
#:   superseded  — the idea survived in changed form; both are trials
#:   dropped     — evaluated and abandoned; invisible in the arm list, and
#:                 exactly the kind of trial the deflation exists to charge for
LEDGER: tuple[dict[str, Any], ...] = (
    # ---- competing today -------------------------------------------------
    {"id": "buy_all", "stage": "idea_selector", "status": "registered",
     "note": "全量基准（对照）"},
    {"id": "random_pick", "stage": "idea_selector", "status": "registered",
     "note": "随机基准（对照）"},
    {"id": "generated_ai_native", "stage": "idea_selector", "status": "registered",
     "note": "来源限定 · AI 端到端"},
    {"id": "generated_carl_constraint", "stage": "idea_selector",
     "status": "registered", "note": "来源限定 · 约束边界"},
    {"id": "mom_21", "stage": "idea_selector", "status": "registered",
     "tried_on": "2026-09-05", "note": "一月动量对照组合，第二版：用 ret_21s"},
    {"id": "ai_native", "stage": "idea_selector", "status": "registered",
     "note": "AI 端到端选取（需要模型，复算式回测里被排除）"},
    {"id": "calib", "stage": "idea_selector", "status": "registered",
     "note": "证据一致性"},
    {"id": "omega_loose", "stage": "idea_selector", "status": "registered",
     "note": "赔率排序 · 宽松"},
    {"id": "omega_strict", "stage": "idea_selector", "status": "registered",
     "note": "赔率排序 · 严格；与 loose 是同一想法的两个阈值，各算一次试验"},
    {"id": "spread", "stage": "idea_selector", "status": "registered",
     "note": "分散度约束"},
    {"id": "left_tail", "stage": "idea_selector", "status": "registered",
     "note": "下行风险优先"},
    {"id": "ev_rank", "stage": "idea_selector", "status": "registered",
     "tried_on": "2026-09-05",
     "note": "期望值排序。看过六期结果之后才设计的，按探索类注册；"
             "前推检验量出它进名单本身值 2.07pp"},

    # ---- tried, and not in the arm list ----------------------------------
    {"id": "grade_buckets", "stage": "idea_selector", "status": "dropped",
     "tried_on": "2026-09-05",
     "note": "按候选评级 S/A/B/C 排序，完全排不出结果（S +1.61% / A +0.95% / "
             "C +0.17%），没有做成组合。与 ev 是同一次搜索里的两个候选。"},
    {"id": "mom_21_priced_in", "stage": "idea_selector", "status": "superseded",
     "tried_on": "2026-09-05",
     "note": "动量组合的第一版，用 priced_in（标的自身一年分布的分位）当动量。"
             "它买的是货架上最安静的名字，十一个组合里垫底、胜率 30%。"
             "同一个名字下换了字段重跑，是两次试验不是一次。"},
)


def entries(stage: str = "idea_selector") -> tuple[dict[str, Any], ...]:
    return tuple(e for e in LEDGER if e["stage"] == stage)


def registered(stage: str = "idea_selector") -> set[str]:
    return {e["id"] for e in entries(stage) if e["status"] == "registered"}


def extra_trials(arms: object, stage: str = "idea_selector") -> int:
    """Trials the comparison cannot see, i.e. what the deflation must add.

    An arm competing in the table is already counted by the deflation through
    the family of Sharpe ratios it is given. What the family cannot know about
    is a rule that was tried and did not become an arm, or a version of an arm
    that was replaced. Those are the entries this returns.

    Passing the arms actually in the run — rather than reading the registry —
    matters because a run that excludes model-dependent arms still searched over
    them. An arm held out of the table is invisible to the family and therefore
    is an extra trial, which is the opposite of what "excluded" suggests.
    """
    names = {str(a) for a in (arms or ())}
    return sum(1 for e in entries(stage) if e["id"] not in names)


def summary(arms: object, stage: str = "idea_selector") -> dict[str, Any]:
    """The count with its own provenance, for the report that prints it."""
    names = {str(a) for a in (arms or ())}
    unseen = [e for e in entries(stage) if e["id"] not in names]
    return {
        "ledger_total": len(entries(stage)),
        "arms_in_comparison": len(names),
        "extra_trials": len(unseen),
        "unseen": sorted(e["id"] for e in unseen),
        "note": (
            "多重检验的试验次数取自 ideagen/trials.py 的试验记录，而不是"
            "「今天恰好注册了几个组合」。记录里记着试过但没做成组合的规则"
            "（如按评级排序）和被换掉的版本（如用错字段的第一版动量），"
            "这些对比较表是隐形的，正是紧缩项要收费的部分。"
            "这份记录本身仍是下限：没人能强制记录一次在别处试过就放弃的排法。"),
    }
