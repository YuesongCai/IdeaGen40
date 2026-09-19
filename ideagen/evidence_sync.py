"""Keep the two replays level with the weekly periods, and say so when they are not.

Two studies feed the panel, and neither one was in any loop:

* `scripts/run_real_backtest.py` → `bt-real-*`, the stock-picking study behind the
  证据 page: the funnel, ranking power, robustness, and the pre-registration
  counter that the whole credibility argument rests on.
* `ideagen.backtest_formal` → `bt-formal-*`, the paper-rules replay behind 业绩.

Both were commands a person typed. On 2026-09-19 the last one had been typed on
09-07: two live weekly periods (09-09, 09-16) were sitting in the database, and
the 证据 page still showed a window ending 09-02 with `n_live_periods: 0`. The
page did not look broken — a counter reading zero is exactly what a counter that
has not started reads, and the pre-registration bar is eight live periods, so
"0/8, 未到期" is the same sentence whether the loop is running or stopped.

So this module does two separate things, and the second matters more than the
first. `refresh` runs whichever study is behind. `lag` reports how far behind
each one is **regardless of whether the refresh ran**, so a stage that fails
silently still shows up on the page as "证据窗口落后 N 期" rather than as a
confident chart of last week's world. An automation that can quietly stop is
worth less than a number that says when it has.
"""
from __future__ import annotations

import io
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any

from . import db

#: Study id prefix → the `methodology` string its rows carry.
STUDIES = {
    "study": "real-pool-asof-replay/v1",
    "formal": "formal-paper-rules",
}


def _q1(src, sql: str, args: tuple = ()):
    """One row, from either a sqlite connection or a platform state store.

    The panel builds its payload from `p.state`, which on another platform is
    not this file at all; reaching for `db.connect()` here would quietly answer
    from whatever database this process happens to own — right on the laptop,
    wrong everywhere else, and wrong in tests, which is where it would have been
    noticed last.
    """
    if hasattr(src, "q"):
        rows = src.q(sql, args) if args else src.q(sql)
        rows = list(rows)
        return rows[0] if rows else None
    return db.q1(src, sql, args)


def _newest_period(src) -> str | None:
    r = _q1(src, "SELECT MAX(as_of) d FROM orch_runs "
                 "WHERE kind='weekly' AND ok=1")
    return r["d"] if r and r["d"] else None


def _periods_through(src, d: str | None) -> int:
    """Completed weekly periods at or before `d`. Zero when `d` is None."""
    if not d:
        return 0
    r = _q1(src, "SELECT COUNT(DISTINCT as_of) n FROM orch_runs "
                 "WHERE kind='weekly' AND ok=1 AND as_of<=?", (d,))
    return int(r["n"] or 0)


def _window_end(src, methodology: str) -> str | None:
    r = _q1(src, "SELECT window_end FROM backtest_runs WHERE ok=1 AND methodology=? "
                 "ORDER BY as_of DESC, ended_at DESC LIMIT 1", (methodology,))
    return r["window_end"] if r else None


def lag(src) -> dict[str, Any]:
    """How many weekly periods each study is behind the newest one on file.

    `behind` counts periods, not days: a study whose window ends on the newest
    period is level even if it was computed hours later, and one that misses a
    period is behind by one however recently it ran.
    """
    newest = _newest_period(src)
    total = _periods_through(src, newest)
    out: dict[str, Any] = {"newest_period": newest, "n_periods": total,
                           "studies": {}}
    worst = 0
    for name, methodology in STUDIES.items():
        end = _window_end(src, methodology)
        # A study's window_end is a mark-to date, which can sit a few days past
        # the last period it replayed; counting periods at or before it is what
        # makes the comparison mean "did it see that week".
        have = _periods_through(src, end)
        behind = max(0, total - have)
        worst = max(worst, behind)
        out["studies"][name] = {"methodology": methodology, "window_end": end,
                                "periods_covered": have, "behind": behind,
                                "level": behind == 0}
    out["behind"] = worst
    out["level"] = worst == 0
    out["note"] = ("证据窗口与最新期次齐平" if worst == 0 else
                   f"证据窗口落后 {worst} 期：页面上的结论没有看过最近 {worst} 周")
    return out


def refresh(con, *, force: bool = False, verbose: bool = False) -> str:
    """Re-run whichever study is behind. Returns a one-line report.

    Never raises: this is meant to hang off the daily run, where a replay that
    cannot finish must not turn the run partial and must not stop the stages
    after it. What it cannot do, it says, and `lag` keeps saying afterwards.
    """
    before = lag(con)
    if before["level"] and not force:
        return f"证据窗口已齐平（{before['newest_period']}，{before['n_periods']} 期）"

    done, failed = [], []
    sink = None if verbose else io.StringIO()

    def _quiet(fn):
        if sink is None:
            return fn()
        with redirect_stdout(sink):
            return fn()

    if force or before["studies"]["study"]["behind"]:
        try:
            import importlib.util
            from pathlib import Path
            path = Path(__file__).resolve().parent.parent / "scripts" / "run_real_backtest.py"
            spec = importlib.util.spec_from_file_location("_run_real_backtest", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_run_real_backtest"] = mod
            spec.loader.exec_module(mod)
            _quiet(lambda: mod.main([]))
            done.append("证据研究")
        except Exception as e:  # noqa: BLE001 — reported, never raised
            failed.append(f"证据研究（{type(e).__name__}: {e}）")
            if verbose:
                traceback.print_exc()

    if force or before["studies"]["formal"]["behind"]:
        try:
            from . import backtest_formal
            _quiet(lambda: backtest_formal.run(con, verbose=False))
            done.append("模拟复盘")
        except Exception as e:  # noqa: BLE001
            failed.append(f"模拟复盘（{type(e).__name__}: {e}）")
            if verbose:
                traceback.print_exc()

    after = lag(con)
    parts = []
    if done:
        parts.append("已刷新 " + "、".join(done))
    if failed:
        parts.append("失败 " + "、".join(failed))
    parts.append(after["note"])
    return "；".join(parts)
