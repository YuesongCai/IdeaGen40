"""The multiple-testing count must not be a literal that only decays.

`perf.deflated_sharpe` charges for the size of the search. Before this ledger
existed the charge was the number `3`, typed at the call site with a comment
saying it was a floor — and a floor typed by hand goes stale in exactly one
direction, because what makes it stale is somebody trying more rules.

These tests are the enforcement: a new arm cannot join the comparison without
its trial being recorded, and a recorded trial that is not competing has to
raise the charge.
"""

from __future__ import annotations

from ideagen import strategy as strat, trials


def test_every_registered_arm_is_in_the_ledger():
    """The one gate that matters. Adding an arm and forgetting the ledger would
    leave the deflation charging for a smaller search than the one that ran."""
    registered = {s["name"] for s in strat.available("idea_selector")}
    missing = registered - {e["id"] for e in trials.entries("idea_selector")}
    assert not missing, (
        f"这些臂已注册但不在试验账本里：{sorted(missing)}。"
        f"把它们加进 ideagen/trials.py，多重检验的紧缩项才收得对。")


def test_the_ledger_carries_trials_that_never_became_arms():
    """A ledger holding only the survivors would be the arm list with extra
    steps — and would charge nothing for the search that produced them."""
    ids = {e["id"] for e in trials.entries("idea_selector")}
    registered = {s["name"] for s in strat.available("idea_selector")}
    assert ids - registered, "账本里没有任何「试过但没做成臂」的记录"


def test_an_arm_held_out_of_the_run_still_counts_as_a_trial():
    """`ai_native` is excluded from the replay because it needs a model. It was
    still searched over, and being invisible to the family is precisely what
    makes it an *extra* trial rather than a free one."""
    competing = [s["name"] for s in strat.available("idea_selector")
                 if not s.get("needs_model")]
    assert "ai_native" in trials.summary(competing)["unseen"]
    assert trials.extra_trials(competing) >= 1


def test_extra_trials_falls_as_the_comparison_widens():
    """Sanity on the direction: a run that competes more of the ledger has less
    left over to charge for, and a run that competes none charges for all."""
    all_ids = [e["id"] for e in trials.entries("idea_selector")]
    assert trials.extra_trials(all_ids) == 0
    assert trials.extra_trials([]) == len(all_ids)
