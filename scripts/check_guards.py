#!/usr/bin/env python3
"""Do the tests actually protect anything?

A passing suite says the code does what the tests describe. It does not say the
tests would notice if the code stopped doing it — a test that cannot fail costs
the same to run and buys nothing. Tonight several guards were written for bugs
that had just been fixed, so the useful question is not "do they pass" but
"would they have caught the bug they were written for".

Each entry below puts a known-bad implementation back in place — in most cases
literally the code as it was before the fix — and asserts the named tests turn
red. Then it restores the real one and asserts they go green again, because a
mutation harness that silently fails to bite looks exactly like a test that
cannot fail. That happened while writing this: `setUpClass` re-fetched the real
payload and overwrote every injected mutation, so the first run reported that
nothing was caught. The green-again check is what tells the two apart.

    python3 scripts/check_guards.py
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")

from ideagen import db, review  # noqa: E402


def _run(module: str, cls: str, names: list[str]) -> int:
    mod = importlib.import_module(module)
    klass = getattr(mod, cls)
    suite = unittest.TestSuite(klass(n) for n in names)
    result = unittest.TextTestRunner(verbosity=0, stream=io.StringIO()).run(suite)
    return len(result.failures) + len(result.errors)


def _passthrough_meta(meta, hide_licensed):
    """The wall before it was a wall: everything crossed."""
    return dict(meta)


def _all_healthy(p, scrub):
    """Reporting an unknown verdict as a clean bill of health."""
    return {"ports": [], "ports_checked_at": None, "ports_age_s": None,
            "ports_stale": False, "ports_pending": False}


def _naive_aggregate(books):
    """Sum only the books that happen to have a mark that day. The total then
    jumps on the day a book starts, which reads as a gain nobody earned."""
    funded = [b for b in books if b.get("capital")]
    if not funded:
        return {}
    curves = [{m["d"]: float(m["equity"]) for m in (b.get("equity") or [])}
              for b in funded]
    dates = sorted({d for c in curves for d in c})
    capital = sum(float(b["capital"]) for b in funded)
    series = [{"d": d, "equity": round(sum(c[d] for c in curves if d in c), 2)}
              for d in dates]
    return {"capital": capital, "n_books": len(funded), "equity": series,
            "return_pct": (round((series[-1]["equity"] / capital - 1) * 100, 4)
                           if series else None),
            "basis": "naive"}


#: Payload mutations. These guards read the live state document rather than a
#: function, so breaking them means handing the tests a corrupted payload. Their
#: `setUpClass` re-fetches the real one, which silently undoes any injection —
#: that is exactly how the first hand-run of this check reported that all five
#: mutations went unnoticed. Disabling the fixture is what makes the injection
#: stick, and the restore-and-pass step below is what proves the harness bites.
def _corrupt(field_path, value):
    def apply(state):
        node = state
        *parents, leaf = field_path
        for key in parents:
            node = node[key]
        node[leaf] = value
    return apply


def _late_start(state):
    for book in state.get("books") or []:
        if book.get("open_positions"):
            book["first_opened_d"] = "2999-01-01"
            return


def _extra_win(state):
    for book in state.get("books") or []:
        book["wins"] = (book.get("closed_n") or 0) + 1
        return


def _shuffle_curve(state):
    (state.get("books_aggregate") or {}).get("equity", []).reverse()


#: (what breaks, mutate(state), tests on TestStateAddsUp)
PAYLOAD_MUTATIONS = [
    ("建仓日晚于它该覆盖的持仓", _late_start,
     ["test_first_opened_is_not_later_than_any_position_it_covers"]),
    ("合计收益与曲线末值对不上",
     _corrupt(("books_aggregate", "return_pct"), 99.0),
     ["test_the_return_matches_the_last_point_of_its_own_curve"]),
    ("胜数超过已平仓数", _extra_win,
     ["test_win_counts_never_exceed_closed_counts"]),
    ("净值曲线日期乱序", _shuffle_curve,
     ["test_the_aggregate_curve_is_dated_in_order_without_repeats"]),
]


#: (what breaks, attribute on `review`, replacement, module, class, tests)
MUTATIONS = [
    ("meta 整份透传（泄漏前的写法）", "_gen_meta", _passthrough_meta,
     "tests.test_publish_meta", "TestGeneratorMetaWall",
     ["test_an_unnamed_prose_key_does_not_cross",
      "test_a_string_that_looks_numeric_is_still_a_string"]),
    ("meta 全部拦掉（过度收紧）", "_gen_meta",
     lambda meta, hide: ({} if hide else meta),
     "tests.test_publish_meta", "TestGeneratorMetaWall",
     ["test_named_keys_survive", "test_unnamed_numbers_still_cross"]),
    ("聚合只算共同日期（丢掉最早几周）", "_books_aggregate",
     lambda books: {"capital": sum(float(b["capital"]) for b in books
                                   if b.get("capital")),
                    "n_books": len([b for b in books if b.get("capital")]),
                    "equity": [], "return_pct": None, "basis": "x"},
     "tests.test_publish_meta", "TestBooksAggregate",
     ["test_a_late_book_sits_at_its_capital_before_it_starts",
      "test_return_is_against_the_full_capital"]),
    ("聚合缺标记就算 0（起步那天总额会跳）", "_books_aggregate", _naive_aggregate,
     "tests.test_publish_meta", "TestBooksAggregate",
     ["test_the_total_never_jumps_just_because_a_book_starts",
      "test_a_gap_carries_the_last_mark_forward"]),
    ("端口未测到当成一切正常（谎报前的写法）", "_port_health", _all_healthy,
     "tests.test_review_health", "TestPortHealth",
     ["test_unknown_is_pending_not_healthy"]),
]


def _run_payload(mutate, names: list[str]) -> tuple[bool, bool]:
    """Return (caught under mutation, clean after restore)."""
    import copy
    module = importlib.import_module("tests.test_state_consistency")
    klass = module.TestStateAddsUp
    real = review.state(db.init())
    original_fixture = klass.setUpClass
    klass.setUpClass = classmethod(lambda cls: None)
    try:
        broken = copy.deepcopy(real)
        mutate(broken)
        klass.state, klass.books, klass.agg = (
            broken, broken.get("books") or [], broken.get("books_aggregate") or {})
        caught = _run("tests.test_state_consistency", "TestStateAddsUp", names) > 0
        klass.state, klass.books, klass.agg = (
            real, real.get("books") or [], real.get("books_aggregate") or {})
        clean = _run("tests.test_state_consistency", "TestStateAddsUp", names) == 0
    finally:
        klass.setUpClass = original_fixture
    return caught, clean


def main() -> int:
    bad = 0
    print(f"{'把生产代码改回出问题的样子':<40}{'守卫反应'}")
    print("-" * 58)
    for label, attr, broken, module, cls, names in MUTATIONS:
        original = getattr(review, attr)
        try:
            setattr(review, attr, broken)
            caught = _run(module, cls, names) > 0
        finally:
            setattr(review, attr, original)
        restored_clean = _run(module, cls, names) == 0
        if caught and restored_clean:
            mark = "✓ 变红，还原后变绿"
        elif not caught:
            mark, bad = "✗ 没反应 —— 这条守卫是空的", bad + 1
        else:
            mark, bad = "✗ 还原后仍然红 —— 夹具有问题", bad + 1
        print(f"{label:<40}{mark}")
    try:
        real_state = review.state(db.init())
        has_books = bool(real_state.get("books"))
    except Exception:  # noqa: BLE001
        has_books = False
    if not has_books:
        print("\n（本机没有可读的组合数据，跳过接口自洽守卫的变异检验）")
    else:
        print()
        for label, mutate, names in PAYLOAD_MUTATIONS:
            caught, clean = _run_payload(mutate, names)
            if caught and clean:
                mark = "✓ 变红，还原后变绿"
            elif not caught:
                mark, bad = "✗ 没反应 —— 这条守卫是空的", bad + 1
            else:
                mark, bad = "✗ 还原后仍然红 —— 夹具有问题", bad + 1
            print(f"{label:<40}{mark}")
    print()
    if bad:
        print(f"{bad} 条守卫没有通过检验：它们通过，不代表它们在保护什么。")
    else:
        print("全部通过：每条守卫都会因为它要防的那个错误而变红。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
