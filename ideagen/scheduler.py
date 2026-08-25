"""Unattended scheduling: what is due, what already happened, and proof of life.

The pipeline runs only when a human types a command. Production means a BytePlus
sandbox that wakes up on its own, decides what is due, and leaves a record an
absent operator can read. This module is that decision layer — and nothing else:
it owns no investment logic, and every job it starts is an existing entrypoint.

Three failures it exists to prevent, each of which is invisible until it costs a
week of data or a duplicated book:

**A second run for a period.** A sandbox can be restarted, retried by its
supervisor, or scheduled twice by an operator who is not sure the first one took.
Idempotency here is not a nicety, it is the whole design: `tick()` decides from
shared state (the Redis lock plus the `orch_runs` rows) rather than from anything
local, so two sandboxes reach the same conclusion. A file lock cannot do this job
— two sandboxes do not share a filesystem, so each would see an unlocked world
and both would run. That is why `Cache.lock` (SET NX EX in Redis) is the
coordination primitive and `orch_runs` is the durable record of what a period
already produced.

**A missed week quietly backfilled.** A weekly run deep-fetches the corpus as it
stood that morning (`wisburg.ingest(fetch_bodies=N)` pulls the N *most recent*
Tier-1/2 items per line, so its depth is a function of when you ask), and it
places entry-band orders against sessions that had not yet printed. Re-running it
on Saturday would read a different corpus and fill orders with hindsight. So
`catch_up()` refuses to pretend: inside a 48h grace window a period is still
genuinely runnable, and beyond it the period is recorded as permanently missed —
a row and an artifact saying "no run happened, here is why" — rather than
backfilled into something that looks original and is not.

**A dead scheduler that looks like a quiet market.** Between Wednesdays the
system is supposed to be undramatic: no new book, few alerts. A crashed sandbox
produces exactly the same silence. Every tick therefore writes a heartbeat with a
TTL through the cache port and a `kind='monitor'` row through the state port, so
absence of the key and absence of rows both mean "nobody is running", and neither
can be confused with "nothing happened".

Time is never read implicitly. `tick(now_utc)` and `catch_up(..., now_utc=...)`
take the instant as an argument so a week can be replayed and tested exactly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from . import config, execution, feeds, orchestrator, platform as plat, schema

# ---------------------------------------------------------------------------
# The schedule, as constants rather than as a crontab.
#
# TIMEZONE RULE — the conversion is done exactly once, here, from a UTC instant
# to a Hong Kong calendar date, and everything downstream uses that date.
#
# HKT is UTC+8 with no DST, so the arithmetic is trivial and the trap is not
# arithmetic: it is that `as_of` is both the trigger date *and* the corpus window.
# `feeds_impl/wisburg_corpus.py` selects documents published in the 3 days ending
# on `as_of` (Mon–Tue–Wed for a Wednesday). A scheduler that used the UTC date
# would shift that window by a day for every trigger after 16:00 UTC — the run
# would be stamped Wednesday while reading Tue–Wed–Thu, dropping Monday's
# research and adding a day the report claims not to have used. Nothing would
# fail: zero rows and shifted rows both validate, the scores still compute, and
# the comparison between weeks silently stops being like-for-like.
#
# The trigger hour is part of the same decision. Moving it changes which of
# Wednesday's own publications exist yet, without changing the `as_of` stamp, so
# it lives here as one named constant (07:00 HKT, per docs/v05_design.xml) and not
# in a cron expression on a machine nobody reads.
HKT = config.TZ
WEEKLY_WEEKDAY = 2                       # Monday=0 → Wednesday
WEEKLY_TRIGGER_HKT = time(7, 0)

#: How late a weekly period may still be started. Inside this window the corpus
#: the run needs is still retrievable at the depth a live run would have had, and
#: no session has printed that its entry bands would see with hindsight. Outside
#: it, the period is unrecoverable — `tick` and `catch_up` share this constant on
#: purpose: two different windows would create a hole where the tick declines to
#: run and catch_up declines to record.
LATE_START_GRACE = timedelta(hours=48)

#: Monitoring cadence. Far cheaper than the weekly run and far more frequent: it
#: only advances marks over sessions that already closed, evaluates stops the
#: engine already knows how to evaluate, and reads feed health out of SQL.
MONITOR_INTERVAL_S = 900

#: How often the outer loop calls `tick`. Ticks between due windows are no-ops
#: apart from the heartbeat, which is the point: liveness must not depend on
#: there being work to do.
TICK_INTERVAL_S = 300

#: Attempts a failing weekly period gets before the scheduler stops retrying it.
#: Unbounded retries of a run that fails at step 6 would spend the model budget
#: every tick; stopping without a record would hide it. So: bounded, and loud.
MAX_WEEKLY_ATTEMPTS = 3

#: Must match the TTL `orchestrator.weekly` passes to `cache.lock`. A row with no
#: `ended_at` younger than this is a live run; older than this, the holder died.
WEEKLY_LOCK_TTL_S = 3600

#: Price history warmed by the monitoring job. Deliberately short: `futu_px.sync`
#: skips codes whose stored range already covers the request, and asking for a
#: year would put every code whose history is shorter than a year back on the
#: network on every single tick.
PRICE_WARM_DAYS = 30

#: A feed with no successful result in this many days is stale rather than quiet.
FEED_STALE_DAYS = 8

HEARTBEAT_KEY = "scheduler:heartbeat"
GAP_KIND = "weekly_missed"

#: Venues this scheduler may run unattended. There is no live-execution adapter in
#: this repository — `paper.py` is the only engine — so anything else is a
#: misconfiguration, and an unattended process must refuse rather than guess.
SUPPORTED_VENUES = ("paper",)


# ---------------------------------------------------------------------------
@dataclass
class JobOutcome:
    """What one job did on one tick. `action` is what an operator reads first."""
    job: str
    period: str
    action: str                  # ran | declined | not_due | failed | exhausted
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickReport:
    at_utc: str
    at_hkt: str
    platform: str
    venue: str
    outcomes: list[JobOutcome] = field(default_factory=list)
    heartbeat: bool = False
    fatal: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 healthy, 1 degraded (retry is sensible), 2 unrecoverable (stop).

        The distinction is what the container loop acts on: a transport failure
        will probably resolve on the next tick, while a missing DSN never will,
        and restarting forever on the latter hides the misconfiguration.
        """
        if self.fatal:
            return 2
        if self.errors or any(o.action == "failed" for o in self.outcomes):
            return 1
        return 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    """One scheduled thing, declared rather than implied by control flow."""
    name: str
    kind: str                                  # value written to orch_runs.kind
    cadence: str                               # operator-facing description
    period: Callable[[datetime], str]          # HKT instant → period key
    due: Callable[[datetime, Any], tuple[bool, str]]
    run: Callable[..., JobOutcome]
    may_trade: bool                            # may this job open a book?


# ---------------------------------------------------------------------------
def to_hkt(now_utc: datetime) -> datetime:
    """The one place a UTC instant becomes a Hong Kong wall clock."""
    if now_utc.tzinfo is None:
        # Refusing a naive datetime rather than assuming UTC: the whole point of
        # passing the instant in is that the caller states it, and a naive value
        # silently interpreted as UTC is how a run ends up stamped with the wrong
        # day on a machine whose local zone is not UTC.
        raise ValueError("now_utc must be timezone-aware, e.g. "
                         "datetime.now(timezone.utc)")
    return now_utc.astimezone(HKT)


def weekly_period(now_hkt: datetime) -> tuple[date, datetime]:
    """The most recent Wednesday trigger at or before `now_hkt`.

    Returns (as_of, trigger). Before 07:00 on a Wednesday the current period has
    not opened yet, so the answer is the previous week — otherwise a sandbox
    booting at 02:00 HKT Wednesday would treat the day as due and run against a
    corpus that is missing the whole Tuesday US session.
    """
    d = now_hkt.date()
    cand = d - timedelta(days=(d.weekday() - WEEKLY_WEEKDAY) % 7)
    trig = datetime.combine(cand, WEEKLY_TRIGGER_HKT, tzinfo=HKT)
    if trig > now_hkt:
        cand -= timedelta(days=7)
        trig = datetime.combine(cand, WEEKLY_TRIGGER_HKT, tzinfo=HKT)
    return cand, trig


def weekly_triggers(since: date, until_hkt: datetime) -> list[tuple[date, datetime]]:
    """Every Wednesday trigger in [since, until], oldest first."""
    out: list[tuple[date, datetime]] = []
    d = since + timedelta(days=(WEEKLY_WEEKDAY - since.weekday()) % 7)
    while True:
        trig = datetime.combine(d, WEEKLY_TRIGGER_HKT, tzinfo=HKT)
        if trig > until_hkt:
            return out
        out.append((d, trig))
        d += timedelta(days=7)


def venue() -> str:
    return (os.environ.get("IDEAGEN_VENUE") or "paper").strip().lower()


# ---------------------------------------------------------------------------
# Idempotency: the record of what a period already produced.
#
# Everything below reads and writes `orch_runs` with plain SELECT / INSERT /
# UPDATE. No `INSERT OR REPLACE`, no `ON CONFLICT`: the first is SQLite-only and
# the second is Postgres-only, and this module has to behave identically on both
# because the migration path moves the state port one adapter at a time. The
# read-then-write is not a race, because every writer of a weekly period holds the
# Redis lock for that period while it does it.
def _weekly_history(p: plat.Platform, as_of: date) -> dict[str, Any]:
    """What `orch_runs` says about one weekly period."""
    rows = p.state.q(
        "SELECT run_id, kind, ok, started_at, ended_at, error FROM orch_runs "
        "WHERE as_of=? AND kind IN ('weekly','weekly_missed') "
        "ORDER BY started_at", (as_of.isoformat(),))
    done = [r for r in rows if r["kind"] == "weekly" and r["ok"]]
    gap = [r for r in rows if r["kind"] == GAP_KIND]
    unfinished = [r for r in rows if r["kind"] == "weekly" and not r["ended_at"]]
    failed = [r for r in rows if r["kind"] == "weekly" and r["ended_at"] and not r["ok"]]
    return {"rows": rows, "done": done, "gap": gap,
            "unfinished": unfinished, "failed": failed}


def _weekly_state(p: plat.Platform, as_of: date, now_utc: datetime) -> tuple[str, str]:
    """Classify a weekly period: (state, human reason).

    States: `done`, `recorded_missed`, `in_flight`, `exhausted`, `open`.
    """
    h = _weekly_history(p, as_of)
    if h["done"]:
        r = h["done"][0]
        return "done", f"{as_of} 已完成，run_id={r['run_id']}（{r['ended_at']}）"
    if h["gap"]:
        return "recorded_missed", f"{as_of} 已记录为永久错过，不再补跑"
    for r in h["unfinished"]:
        # No `ended_at` means the run either is alive or died without closing its
        # row. The lock TTL is the only honest way to tell them apart, and the
        # lock itself will refuse us if the holder is alive — this check exists so
        # the *report* says "in flight" instead of "failed to acquire lock".
        age = _age_s(r["started_at"], now_utc)
        if age is not None and age < WEEKLY_LOCK_TTL_S:
            return "in_flight", (f"{as_of} 有一次运行仍在进行中（run_id="
                                 f"{r['run_id']}，{int(age)}s 前开始）")
    if len(h["failed"]) + len(h["unfinished"]) >= MAX_WEEKLY_ATTEMPTS:
        return "exhausted", (f"{as_of} 已失败 {len(h['failed']) + len(h['unfinished'])} "
                             f"次，达到上限 {MAX_WEEKLY_ATTEMPTS}，需要人工介入")
    if h["failed"] or h["unfinished"]:
        return "open", (f"{as_of} 之前失败 {len(h['failed']) + len(h['unfinished'])} "
                        f"次，仍可重试")
    return "open", f"{as_of} 尚无运行记录"


def _age_s(iso: str | None, now_utc: datetime) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now_utc - t).total_seconds()


def _insert_run_row(p: plat.Platform, *, run_id: str, as_of: str, kind: str,
                    started: str, ended: str | None, ok: int,
                    error: str | None) -> bool:
    """Write one `orch_runs` row, portably and idempotently.

    Returns False if a row with that id already exists — which is how the
    deterministic ids used for gap records and monitor buckets make a repeated
    call a no-op instead of a duplicate.
    """
    have = p.state.q("SELECT run_id FROM orch_runs WHERE run_id=?", (run_id,))
    if have:
        p.state.execute(
            "UPDATE orch_runs SET ended_at=?, ok=?, error=? WHERE run_id=?",
            (ended, ok, error, run_id))
        return False
    p.state.execute(
        "INSERT INTO orch_runs (run_id,as_of,kind,platform,started_at,ended_at,"
        " ok,error,inputs_sha,journal_uri,calls) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, as_of, kind, p.name, started, ended, ok, error, None, None, 0))
    return True


# ---------------------------------------------------------------------------
# The weekly job.
def _weekly_due(now_hkt: datetime, p: plat.Platform) -> tuple[bool, str]:
    as_of, trig = weekly_period(now_hkt)
    late = now_hkt - trig
    if late > LATE_START_GRACE:
        return False, (f"距 {as_of} 07:00 HKT 触发点已过 "
                       f"{late.total_seconds() / 3600:.1f}h，超过 "
                       f"{LATE_START_GRACE.total_seconds() / 3600:.0f}h 补跑窗口，"
                       f"交给 catch_up 记为错过")
    return True, f"{as_of} 触发点已过 {late.total_seconds() / 3600:.1f}h"


def _run_weekly(p: plat.Platform, now_hkt: datetime, now_utc: datetime, *,
                as_of: date, dry_run: bool, ingest: bool,
                weekly_kwargs: dict[str, Any] | None,
                log: Callable[[str], None]) -> JobOutcome:
    """Ingest the corpus, then run the period once.

    The state check below is advisory, not the safety mechanism: between reading
    it and calling `weekly()` another sandbox can start. The safety mechanism is
    the lock `weekly()` takes itself — this check exists so the common case (a
    retry hours later) is answered without paying for a platform health check and
    a lock round-trip, and so the report can say *why* nothing happened.
    """
    state, why = _weekly_state(p, as_of, now_utc)
    if state != "open":
        action = "exhausted" if state == "exhausted" else "declined"
        log(f"  周策略  跳过：{why}")
        return JobOutcome("weekly", as_of.isoformat(), action, why,
                          {"state": state})

    detail: dict[str, Any] = {"state": state, "dry_run": dry_run}
    if ingest and not dry_run:
        detail["ingest"] = _ingest_corpus(p, as_of, log=log)

    kw = dict(weekly_kwargs or {})
    res = orchestrator.weekly(as_of=as_of, p=p, dry_run=dry_run, verbose=True, **kw)
    detail.update(run_id=res.run_id, ok=res.ok, skipped=res.skipped,
                  topics=len(res.topics), selectors=len(res.selectors),
                  artifacts=len(res.artifacts), calls=res.calls,
                  journal=res.journal, error=res.error)

    if res.skipped:
        # `weekly()` reports a lost lock race as ok=True with `skipped` set. That
        # must NOT be recorded as a completed period: if the winning run then
        # dies, the next tick has to be free to retry.
        log(f"  周策略  跳过：{res.skipped}")
        return JobOutcome("weekly", as_of.isoformat(), "declined", res.skipped, detail)
    if not res.ok:
        # `run_id == "-"` means `weekly()` refused before opening its row: the
        # platform was not ready, so nothing was attempted and nothing was spent.
        # That is deliberately NOT counted as one of the period's attempts —
        # a missing model key can appear on the next tick, and burning the retry
        # budget on a pre-flight refusal would declare the week exhausted within
        # the hour. The container's degraded counter is what escalates this one.
        detail["attempt_recorded"] = res.run_id != "-"
        _notify(f"❌ IdeaGen 周跑失败 {as_of.isoformat()}：{str(res.error)[:180]}")
        log(f"  周策略  失败：{res.error}")
        p.events.publish("scheduler.weekly.failed",
                         {"as_of": as_of.isoformat(), "run_id": res.run_id,
                          "error": res.error})
        return JobOutcome("weekly", as_of.isoformat(), "failed", res.error, detail)

    # An empty corpus validates cleanly — zero rows satisfy every schema rule —
    # so a dead ingest and a quiet week produce the same successful run. Name it.
    thin = _corpus_rows(p, res.run_id)
    detail["corpus_rows"] = thin
    if thin is not None and thin == 0:
        detail["warning"] = "corpus 为空：本周运行没有任何研报输入"
        log("  ⚠ 周策略跑完了，但 corpus 为 0 行——ingest 或 Wisburg 通道有问题")
        p.events.publish("scheduler.weekly.thin_corpus",
                         {"as_of": as_of.isoformat(), "run_id": res.run_id, "rows": 0})

    # Booking is part of the weekly job, not an afterthought: a run whose picks
    # never became positions stores opinions, and a month later there is nothing
    # to compare the selectors on. It is guarded to the paper venue and reported
    # as degraded rather than fatal — the verdicts are already persisted, so a
    # booking failure is recoverable by `ideagen book`, while crashing the tick
    # here would also take the heartbeat down with it.
    if not dry_run and execution.selected() == "paper":
        try:
            from . import booking, db as _db
            bk = booking.book_run(_db.init(), p, res.run_id, verbose=True)
            detail["booked"] = {k: v for k, v in bk["books"].items()}
        except Exception as e:  # noqa: BLE001
            detail["booking_error"] = f"{type(e).__name__}: {e}"
            log(f"  ⚠ 建仓失败（结论已保存，可用 `ideagen book` 补）：{e}")
            p.events.publish("scheduler.weekly.booking_failed",
                             {"run_id": res.run_id, "error": str(e)[:300]})

    _notify(f"✅ IdeaGen 周跑完成 {as_of.isoformat()}：主题 {len(res.topics)} · "
            f"候选 {res.n_candidates} · 账本 {len(res.selectors)} · "
            f"模型调用 {res.calls}。建仓结果见复盘板 http://localhost:8765/review")
    log(f"  周策略  完成 run_id={res.run_id} artifacts={len(res.artifacts)}")
    p.events.publish("scheduler.weekly.ran",
                     {"as_of": as_of.isoformat(), "run_id": res.run_id,
                      "late_h": round((now_hkt - weekly_period(now_hkt)[1])
                                      .total_seconds() / 3600, 2)})
    return JobOutcome("weekly", as_of.isoformat(), "ran", None, detail)


def _notify(text: str) -> None:
    """Feishu DM, fire-and-forget. A notification that can fail the run is a
    liability; one that silently never fires is a dead man's misunderstanding —
    so failures are logged to stderr but never raised."""
    import subprocess, sys
    try:
        subprocess.run(
            ["/opt/homebrew/bin/lark-cli", "im", "+messages-send", "--as", "bot",
             "--user-id", "ou_8d0e4064f46c1d0de14c501c1f5db808",
             "--text", text], timeout=30, capture_output=True)
    except Exception as e:  # noqa: BLE001
        print(f"  (飞书通知失败: {e})", file=sys.stderr)


def _refresh_review() -> None:
    """Regenerate the review board. Best-effort: the board is a view, and a view
    must never be able to take down the thing it views."""
    try:
        from . import review
        review.build()
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"  (复盘板刷新失败: {e})", file=sys.stderr)


def _corpus_rows(p: plat.Platform, run_id: str) -> int | None:
    try:
        r = p.state.q("SELECT n_rows FROM feed_runs WHERE run_id=? AND kind='corpus'",
                      (run_id,))
    except Exception:  # noqa: BLE001 — a reporting nicety must not fail the tick
        return None
    return sum(int(x["n_rows"] or 0) for x in r) if r else None


def _ingest_corpus(p: plat.Platform, as_of: date, *,
                   log: Callable[[str], None]) -> dict[str, Any]:
    """Pull this period's research before scoring it.

    This is the irreplaceable step, and the reason a missed week cannot be
    recreated: `fetch_bodies` deep-fetches the most recent Tier-1/2 items per
    line, so what it returns depends on when it is asked.

    A failure here is recorded and does not abort the run. Refusing to run loses
    the period permanently; running against what is already stored at least
    produces a journal that states what it saw, and the empty-corpus check above
    makes the degradation visible instead of plausible.
    """
    con = _legacy_con(p)
    if con is None:
        return {"skipped": "state store is not SQLite; the ingest path needs a "
                           "sqlite3 connection"}
    if not os.environ.get("WISBURG_MCP_TOKEN"):
        return {"skipped": "WISBURG_MCP_TOKEN 未设置，跳过 ingest"}
    try:
        from .sources import wisburg
        rep = wisburg.ingest(con, as_of, fetch_bodies=8, verbose=False)
        log(f"  ingest  {rep.get('total', 0)} 条（新 {rep.get('new', 0)}）")
        return {"total": rep.get("total"), "new": rep.get("new"),
                "errors": len(rep.get("errors") or {})}
    except Exception as e:  # noqa: BLE001 — see docstring
        log(f"  ! ingest 失败（继续跑，用库里已有语料）：{type(e).__name__}: {e}")
        return {"failed": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# The monitoring job.
def _monitor_due(now_hkt: datetime, p: plat.Platform) -> tuple[bool, str]:
    last = _last_monitor_at(p)
    if last is None:
        return True, "尚无盯市记录"
    age = (now_hkt.astimezone(timezone.utc) - last).total_seconds()
    if age < MONITOR_INTERVAL_S:
        return False, f"上次盯市 {int(age)}s 前，节流窗口 {MONITOR_INTERVAL_S}s"
    return True, f"上次盯市 {int(age)}s 前"


def _last_monitor_at(p: plat.Platform) -> datetime | None:
    try:
        r = p.state.q("SELECT MAX(started_at) AS t FROM orch_runs WHERE kind='monitor'")
    except Exception:  # noqa: BLE001 — if we cannot tell, do the cheap job
        return None
    t = (r[0]["t"] if r else None)
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(str(t))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_monitoring(p: plat.Platform, now_hkt: datetime, now_utc: datetime, *,
                    dry_run: bool, log: Callable[[str], None]) -> JobOutcome:
    """Refresh marks, evaluate stops and triggers, report feed health.

    This job never opens a book and never calls `orchestrator.weekly`. That is
    structural, not a promise: nothing here imports it. The distinction matters
    because this job runs ~100 times a day, and a monitoring pass that could
    start a strategy run would turn a restart loop into a hundred books.

    Everything it does is idempotent by construction: marks are a deterministic
    function of bars that already closed, `paper.step` cannot re-fill an order
    that is no longer pending, and alerts are keyed by (kind, position, day).
    """
    period = now_hkt.strftime("%Y-%m-%dT%H:%M")
    bucket = now_utc.replace(second=0, microsecond=0)
    bucket -= timedelta(minutes=bucket.minute % max(MONITOR_INTERVAL_S // 60, 1))
    run_id = f"mon-{bucket.strftime('%Y%m%dT%H%MZ')}"
    detail: dict[str, Any] = {"run_id": run_id}

    # The durable liveness record. One bounded row per bucket, written before the
    # work: if the sandbox dies mid-pass, the row with no `ended_at` says so. A
    # heartbeat that only lives in Redis is not enough — the byteplus adapter
    # falls back to a *file* cache when IDEAGEN_REDIS_URL is unset, and a file in
    # a disposable sandbox is not evidence of anything.
    if not dry_run:
        _insert_run_row(p, run_id=run_id, as_of=now_hkt.date().isoformat(),
                        kind="monitor", started=plat.utcnow_iso(), ended=None,
                        ok=0, error=None)

    problems: list[str] = []
    detail["feeds"] = _feed_health(p, now_hkt, problems)

    con = _legacy_con(p)
    if con is None:
        detail["marks"] = {"skipped": "state store is not SQLite; paper/monitor "
                                      "need a sqlite3 connection"}
        problems.append("盯市跳过：state 端口不是 SQLite")
    else:
        detail["prices"] = _warm_prices(p, con, now_hkt, problems)
        detail["marks"] = _advance_books(con, now_hkt, problems, log=log)
        detail["alerts"] = _check_triggers(con, now_hkt, problems, log=log)

    if not dry_run:
        _insert_run_row(p, run_id=run_id, as_of=now_hkt.date().isoformat(),
                        kind="monitor", started=plat.utcnow_iso(),
                        ended=plat.utcnow_iso(), ok=0 if problems else 1,
                        error="; ".join(problems)[:500] or None)
    p.events.publish("scheduler.monitor",
                     {"run_id": run_id, "problems": problems,
                      "alerts": (detail.get("alerts") or {}).get("n")})

    detail["problems"] = problems
    log(f"  盯市    {run_id}  "
        f"告警 {(detail.get('alerts') or {}).get('n', '-')}  "
        f"feed 异常 {len([f for f in detail['feeds'] if f.get('problem')])}"
        + (f"  ⚠ {len(problems)} 项降级" if problems else ""))
    # The review board is a view over the same stores this pass just wrote, so
    # refreshing it here keeps "the page" and "the truth" at most 15 minutes
    # apart without anyone remembering to rebuild it.
    _refresh_review()
    # A degraded pass still counts as having run. Reporting it as `failed` would
    # make the container exit non-zero every quarter hour for the normal cloud
    # condition — OpenD is a local gateway and is not reachable from a sandbox —
    # and an exit code that is always 1 stops carrying information.
    return JobOutcome("monitor", period, "ran", "; ".join(problems) or None, detail)


def _feed_health(p: plat.Platform, now_hkt: datetime,
                 problems: list[str]) -> list[dict[str, Any]]:
    """Latest result per registered feed, from SQL alone — no network, no cost.

    A feed that has never run and a feed that returned zero rows are reported
    separately: the first is a configuration gap, the second is the failure mode
    `feeds.register` warns about, where a dead endpoint validates as a quiet week.
    """
    try:
        rows = p.state.q(
            "SELECT feed, kind, as_of, n_rows, ok, error FROM feed_runs f "
            "WHERE as_of=(SELECT MAX(as_of) FROM feed_runs g WHERE g.feed=f.feed)")
    except Exception as e:  # noqa: BLE001
        problems.append(f"feed 健康查询失败：{type(e).__name__}: {e}")
        return []
    latest = {r["feed"]: r for r in rows}
    out: list[dict[str, Any]] = []
    for spec in feeds.available():
        r = latest.get(spec["name"])
        item: dict[str, Any] = {"feed": spec["name"], "kind": spec["kind"],
                                "required": spec["required"]}
        if not r:
            item["problem"] = "从未运行过"
        else:
            item.update(as_of=r["as_of"], n_rows=r["n_rows"], ok=bool(r["ok"]))
            stale_d = (now_hkt.date() - date.fromisoformat(str(r["as_of"]))).days
            item["stale_days"] = stale_d
            if not r["ok"]:
                item["problem"] = f"上次结果不合格：{str(r['error'])[:120]}"
            elif not r["n_rows"]:
                item["problem"] = "上次返回 0 行——端点可能已死，但会被当成安静的一周"
            elif stale_d > FEED_STALE_DAYS:
                item["problem"] = f"{stale_d} 天没有成功结果"
        if item.get("problem") and spec["required"]:
            problems.append(f"必需 feed {spec['name']}：{item['problem']}")
        out.append(item)
    return out


def _opend_reachable(timeout_s: float = 1.5) -> bool:
    """Plain TCP probe before the price SDK is allowed anywhere near this process.

    This is not defensive decoration. `futu_px.quote_ctx()` documents that it
    raises `OpenDUnavailable` when the gateway is down, but the futu SDK does not
    behave that way: `OpenQuoteContext(...)` blocks and retries the connection
    every six seconds, forever. Verified here against a closed port — 17 retries
    and still going. In a cloud sandbox, where OpenD is by definition unreachable
    (it is a desktop gateway), that turns every monitoring pass into a permanent
    hang: no exception to catch, no heartbeat written, and a process that is alive
    while looking dead — the exact failure this module exists to prevent.

    So reachability is decided by a socket with a timeout that we control, and the
    SDK is only entered once the port answers.
    """
    import socket
    try:
        with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT),
                                      timeout=timeout_s):
            return True
    except OSError:
        return False


def _warm_prices(p: plat.Platform, con: Any, now_hkt: datetime,
                 problems: list[str]) -> dict[str, Any]:
    """Top up the bars marking needs. Cheap when nothing is missing.

    `futu_px.sync` skips codes whose stored range already covers the request, so
    a steady state costs one SQL query per code and no network at all. OpenD is a
    desktop gateway: in a cloud sandbox it is unreachable, which degrades marking
    to whatever bars are already stored rather than failing — or hanging — the pass.
    """
    from . import lexicon
    from .sources import futu_px
    if not _opend_reachable():
        return {"skipped": f"OpenD 不可达（{config.FUTU_HOST}:{config.FUTU_PORT}），"
                           f"只用库里已有的 K 线盯市"}
    try:
        codes = [r["code"] for r in con.execute(
            "SELECT DISTINCT code FROM positions WHERE status='open' "
            "AND code IS NOT NULL").fetchall()]
    except Exception:  # noqa: BLE001 — a book may not exist yet
        codes = []
    codes = sorted(set(codes) | set(lexicon.all_indicators()))
    end = now_hkt.date()
    try:
        rep = futu_px.sync(con, codes, end - timedelta(days=PRICE_WARM_DAYS), end)
        return {"requested": rep.get("requested"), "fetched": rep.get("fetched"),
                "rows": rep.get("rows")}
    except Exception as e:  # noqa: BLE001
        problems.append(f"行情同步不可用（用已存 K 线继续）：{type(e).__name__}")
        return {"skipped": f"{type(e).__name__}: {e}"[:200]}


def _advance_books(con: Any, now_hkt: datetime, problems: list[str], *,
                   log: Callable[[str], None]) -> dict[str, Any]:
    """Advance every book from its last marked session to the last closed one.

    Starting from the last marked session rather than from the first order is what
    makes this cheap enough to run every quarter hour: in a steady state
    `sessions_between` returns one day or none.
    """
    from . import db, paper
    from .sources import futu_px
    end = futu_px.complete_through("US", now=now_hkt)
    out: dict[str, Any] = {"to": end, "books": 0, "fills": 0, "exits": 0}
    try:
        books = paper.all_books(con)
    except Exception as e:  # noqa: BLE001
        problems.append(f"读取组合失败：{type(e).__name__}: {e}")
        return out
    for b in books:
        try:
            last = db.q1(con, "SELECT MAX(d) d FROM equity WHERE book_id=?", (b,))
            first = db.q1(con, "SELECT MIN(placed_d) d FROM orders WHERE book_id=?", (b,))
            start = (last["d"] if last and last["d"] else
                     (first["d"] if first and first["d"] else None))
            if not start:
                continue                       # nothing placed on this book yet
            r = paper.run(con, b, start, end, verbose=False)
            out["books"] += 1
            out["fills"] += r.get("fills", 0)
            out["exits"] += r.get("exits", 0)
        except Exception as e:  # noqa: BLE001 — one bad book must not stop the rest
            problems.append(f"组合 {b} 盯市失败：{type(e).__name__}: {e}")
    return out


def _check_triggers(con: Any, now_hkt: datetime, problems: list[str], *,
                    log: Callable[[str], None]) -> dict[str, Any]:
    """Stops, thesis invalidation, crowding, horizons — the existing monitor."""
    from . import monitor as monitor_mod
    from .sources import futu_px
    d = futu_px.complete_through("US", now=now_hkt)
    try:
        rep = monitor_mod.run(con, d, verbose=False)
    except Exception as e:  # noqa: BLE001
        problems.append(f"告警检查失败：{type(e).__name__}: {e}")
        return {"d": d, "failed": f"{type(e).__name__}: {e}"}
    return {"d": d, "n": rep.get("alerts"), "by_level": rep.get("by_level")}


# ---------------------------------------------------------------------------
def _legacy_con(p: plat.Platform) -> Any | None:
    """A sqlite3 connection for the modules that predate the state port.

    `paper`, `monitor`, `futu_px` and `wisburg` are ~9,000 lines of hand-written
    SQLite that already encode the as-of and mark-ordering invariants; the ports
    exist so new code does not add to that, not to pretend it is portable.

    Two specifics, both learned the hard way:

      * The connection comes from `db.init()`, not from `StateStore.connection`.
        `db.connect` opens with `isolation_level=None`, and `db.tx()` issues an
        explicit BEGIN — against the port's connection (default isolation level)
        that raises "cannot start a transaction within a transaction" on the first
        fill. Same file, different connection settings, and only one of them works.
      * When the state port is Postgres there is no such connection, and these
        modules cannot run at all. The caller degrades and says so rather than
        importing sqlite behind the operator's back and marking a book in a file
        that dies with the sandbox.
    """
    if getattr(p.state, "paramstyle", "qmark") != "qmark":
        return None
    from . import db
    return db.init()


# ---------------------------------------------------------------------------
#: The declarative schedule. Adding a job means adding a row here, not another
#: branch inside `tick`.
#:
#: `may_trade` marks the only job permitted to touch a book — and it is a
#: statement of permission, not of current behaviour: `orchestrator.weekly()`
#: today stops at the verdicts, and turning those into a batch and orders is still
#: `ideas.build_batch` + `paper.open_batch` from the CLI. So the unattended loop
#: produces the week's decisions but not yet the week's book; closing that needs a
#: change inside `orchestrator.weekly` (or a third job here), and until it happens
#: the duplicate-order risk lives in a step nothing schedules.
SCHEDULE: tuple[Job, ...] = (
    Job(name="weekly", kind="weekly",
        cadence="每周三 07:00 HKT（48h 内可补跑）",
        period=lambda now_hkt: weekly_period(now_hkt)[0].isoformat(),
        due=_weekly_due, run=_run_weekly, may_trade=True),
    Job(name="monitor", kind="monitor",
        cadence=f"每 {MONITOR_INTERVAL_S // 60} 分钟：盯市 + 止损触发 + feed 健康",
        period=lambda now_hkt: now_hkt.strftime("%Y-%m-%dT%H:%M"),
        due=_monitor_due, run=_run_monitoring, may_trade=False),
)


# ---------------------------------------------------------------------------
def tick(now_utc: datetime, *, p: plat.Platform | None = None,
         interval_s: int = TICK_INTERVAL_S, force: str | None = None,
         dry_run: bool = False, ingest: bool = True,
         weekly_kwargs: dict[str, Any] | None = None,
         verbose: bool = True) -> TickReport:
    """The single entrypoint a cron, a container loop or an AgentKit sandbox calls.

    Idempotent by construction, and that is the whole design constraint. Nothing
    here decides from local state: the weekly period is a function of the instant
    passed in, "already happened" is a row in `orch_runs`, and mutual exclusion is
    a Redis key. A restarted, retried or double-scheduled sandbox therefore
    reaches the same conclusion as the one that ran, and declines.

    `force` names a job to treat as due (an operator whose sandbox missed its
    trigger). It overrides the calendar, never the record: a period that already
    completed still declines.
    """
    now_hkt = to_hkt(now_utc)
    log = (lambda m: print(m)) if verbose else (lambda m: None)
    rep = TickReport(at_utc=now_utc.isoformat(), at_hkt=now_hkt.isoformat(),
                     platform="?", venue=venue())

    try:
        p = p or plat.load()
    except Exception as e:  # noqa: BLE001
        # No platform, no ports, nothing to write anywhere — including the
        # heartbeat. Loud and unrecoverable is the only honest answer.
        rep.fatal = f"platform load failed: {type(e).__name__}: {e}"
        return rep
    rep.platform = p.name

    if rep.venue not in SUPPORTED_VENUES:
        rep.fatal = (f"IDEAGEN_VENUE={rep.venue!r} is not supported "
                     f"{SUPPORTED_VENUES}; this repository has no live-execution "
                     f"adapter, so an unattended run must refuse")
        return rep

    log(f"tick {now_hkt:%Y-%m-%d %H:%M} HKT  platform={p.name} venue={rep.venue}"
        + ("  [dry-run]" if dry_run else ""))

    try:
        # Cheap, idempotent, and the tables the scheduler itself reads have to be
        # there before any job runs — including on a brand-new RDS instance.
        schema.migrate(p.state)
    except plat.NotConfigured as e:
        rep.fatal = f"state port not configured: {e}"
    except Exception as e:  # noqa: BLE001
        # Could be a database that is merely down. Degraded, not fatal: the next
        # tick may find it back, and the heartbeat below still goes out so the
        # sandbox reads as alive-but-failing rather than dead.
        rep.errors.append(f"migrate failed: {type(e).__name__}: {e}")

    if not rep.fatal and not rep.errors:
        for job in SCHEDULE:
            period = job.period(now_hkt)
            try:
                due, why = job.due(now_hkt, p)
                if force == job.name and not due:
                    due, why = True, f"--force {job.name}：{why}"
                if not due:
                    rep.outcomes.append(JobOutcome(job.name, period, "not_due", why))
                    log(f"  {job.name:<7} 未到期：{why}")
                    continue
                if job.name == "weekly":
                    as_of, _ = weekly_period(now_hkt)
                    rep.outcomes.append(_run_weekly(
                        p, now_hkt, now_utc, as_of=as_of, dry_run=dry_run,
                        ingest=ingest, weekly_kwargs=weekly_kwargs, log=log))
                else:
                    rep.outcomes.append(_run_monitoring(
                        p, now_hkt, now_utc, dry_run=dry_run, log=log))
            except plat.NotConfigured as e:
                rep.fatal = f"{job.name}: port not configured: {e}"
                break
            except Exception as e:  # noqa: BLE001 — one job must not lose the tick
                rep.outcomes.append(JobOutcome(job.name, period, "failed",
                                              f"{type(e).__name__}: {e}"))
                log(f"  ! {job.name} 抛异常：{type(e).__name__}: {e}")

    rep.heartbeat = _heartbeat(p, rep, interval_s=interval_s)
    return rep


def _heartbeat(p: plat.Platform, rep: TickReport, *, interval_s: int) -> bool:
    """Proof of life, written every tick whatever else happened.

    Two writes, because they answer different questions. The cache key carries a
    TTL of three intervals: its *absence* is the signal, so a stopped scheduler
    stops being able to hide behind a quiet week. The event is the timeline, for
    whoever is consuming the Kafka topic.

    Deliberately not the blob store: `BlobStore.put` refuses to overwrite by
    design, so a heartbeat there would either fail on the second tick or litter
    the artifact tree with one object every five minutes.
    """
    ok = False
    payload = {
        "at_utc": rep.at_utc, "at_hkt": rep.at_hkt, "platform": rep.platform,
        "venue": rep.venue, "host": os.environ.get("HOSTNAME", ""),
        "interval_s": interval_s, "exit_code": rep.exit_code,
        "fatal": rep.fatal, "errors": rep.errors,
        "outcomes": [{"job": o.job, "period": o.period, "action": o.action}
                     for o in rep.outcomes],
    }
    try:
        p.cache.set(HEARTBEAT_KEY,
                    json.dumps(payload, ensure_ascii=False, default=str).encode(),
                    ttl_s=max(interval_s * 3, 60))
        ok = True
    except Exception as e:  # noqa: BLE001 — never fail a tick over observability
        rep.errors.append(f"heartbeat write failed: {type(e).__name__}: {e}")
    try:
        p.events.publish("scheduler.tick", payload)
    except Exception:  # noqa: BLE001
        pass
    return ok


# ---------------------------------------------------------------------------
@dataclass
class CatchUpReport:
    since: str
    until_hkt: str
    periods: list[dict[str, Any]] = field(default_factory=list)
    monitoring: dict[str, Any] = field(default_factory=dict)

    @property
    def permanently_missed(self) -> list[str]:
        return [x["as_of"] for x in self.periods
                if x["status"] in ("permanently_missed", "recorded_missed")]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["permanently_missed"] = self.permanently_missed
        return d


def catch_up(since: date | datetime, *, now_utc: datetime,
             p: plat.Platform | None = None, run_recoverable: bool = False,
             dry_run: bool = False, ingest: bool = True,
             weekly_kwargs: dict[str, Any] | None = None,
             verbose: bool = True) -> CatchUpReport:
    """Decide honestly what a downed sandbox can and cannot recover.

    The asymmetry is the point, and it is not a limitation of this code:

    **Monitoring is recoverable.** Marks, fills, stops and alerts are a
    deterministic function of bars that already closed. Running the monitoring
    job once walks every session between the last marked one and now, so a
    fortnight of downtime costs nothing but the time to replay it.

    **A weekly run is not.** Two independent reasons. The corpus a live run would
    have deep-fetched is no longer retrievable at that depth — the deep fetch
    takes the most recent Tier-1/2 items per line, so its content is a function
    of when it is asked. And the orders a weekly book places are entry bands
    against sessions that had not printed yet; filling them from bars that now
    exist is not a late run, it is a run with hindsight, and it would enter the
    same tables as the honest ones.

    So beyond the grace window this function records the gap — an `orch_runs` row
    with `kind='weekly_missed'`, `ok=0`, and an artifact next to where the run's
    journal would have been — and moves on. Nothing is backfilled. `catch_up` is
    read-only with respect to strategy: it runs a recoverable period only when
    asked with `run_recoverable=True`, because a sandbox coming back after three
    weeks must not decide by itself to open three books.
    """
    now_hkt = to_hkt(now_utc)
    log = (lambda m: print(m)) if verbose else (lambda m: None)
    since_d = since.date() if isinstance(since, datetime) else since
    rep = CatchUpReport(since=since_d.isoformat(), until_hkt=now_hkt.isoformat())

    p = p or plat.load()
    schema.migrate(p.state)
    log(f"catch_up {since_d} → {now_hkt:%Y-%m-%d %H:%M} HKT  platform={p.name}")

    for as_of, trig in weekly_triggers(since_d, now_hkt):
        state, why = _weekly_state(p, as_of, now_utc)
        late_h = (now_hkt - trig).total_seconds() / 3600
        item: dict[str, Any] = {"as_of": as_of.isoformat(),
                                "trigger_hkt": trig.isoformat(),
                                "late_h": round(late_h, 1)}
        if state == "done":
            item.update(status="ok", reason=why)
        elif state == "recorded_missed":
            item.update(status="recorded_missed", reason=why, recoverable=False)
        elif state == "in_flight":
            item.update(status="in_flight", reason=why)
        elif now_hkt - trig <= LATE_START_GRACE:
            item.update(status="recoverable", recoverable=True,
                        reason=f"仍在 {LATE_START_GRACE.total_seconds() / 3600:.0f}h "
                               f"补跑窗口内（迟 {late_h:.1f}h），语料深度与订单前瞻性都还成立")
            if run_recoverable:
                out = _run_weekly(p, now_hkt, now_utc, as_of=as_of, dry_run=dry_run,
                                  ingest=ingest, weekly_kwargs=weekly_kwargs, log=log)
                item.update(status=("ran" if out.action == "ran" else out.action),
                            reason=out.reason, detail=out.detail)
        else:
            item.update(status="permanently_missed", recoverable=False,
                        reason=(f"迟 {late_h:.1f}h，超过 "
                                f"{LATE_START_GRACE.total_seconds() / 3600:.0f}h："
                                f"当时的语料已无法按同等深度取回，且此时补跑会用"
                                f"已经印出来的 K 线去成交进场区间——那是带后见之明的运行，"
                                f"不是迟到的运行"))
            item["recorded"] = _record_gap(p, as_of, item["reason"],
                                           dry_run=dry_run, log=log)
        rep.periods.append(item)
        log(f"  {as_of}  {item['status']:<18} {item['reason'][:80]}")

    rep.monitoring = _catch_up_monitoring(p, now_hkt, now_utc, since_d,
                                         run_recoverable=run_recoverable,
                                         dry_run=dry_run, log=log)
    log(f"  监控      {rep.monitoring.get('summary')}")
    if rep.permanently_missed:
        p.events.publish("scheduler.catch_up",
                         {"missed": rep.permanently_missed,
                          "since": rep.since, "until": rep.until_hkt})
    return rep


def _record_gap(p: plat.Platform, as_of: date, reason: str, *,
                dry_run: bool, log: Callable[[str], None]) -> dict[str, Any]:
    """Record a permanently missed period. Never a fake run.

    `kind='weekly_missed'` with `ok=0` occupies the period so no later tick tries
    to run it, while being impossible to mistake for a run: anything selecting
    `kind='weekly' AND ok=1` skips it. The artifact is written next to where the
    journal would have been, so an operator reading that week's directory finds an
    explicit statement instead of an empty prefix and their own guess.
    """
    run_id = f"gap-weekly-{as_of.isoformat()}"
    if dry_run:
        return {"dry_run": True, "run_id": run_id}
    now = plat.utcnow_iso()
    fresh = _insert_run_row(p, run_id=run_id, as_of=as_of.isoformat(), kind=GAP_KIND,
                            started=now, ended=now, ok=0,
                            error=f"permanently missed: {reason}")
    out: dict[str, Any] = {"run_id": run_id, "row": "inserted" if fresh else "existing"}
    try:
        out["artifact"] = p.blobs.put(
            f"runs/{as_of.isoformat()}/{run_id}/gap.json",
            json.dumps({"as_of": as_of.isoformat(), "kind": GAP_KIND,
                        "recorded_at": now, "platform": p.name, "reason": reason,
                        "backfilled": False}, ensure_ascii=False, indent=1).encode(),
            content_type="application/json")
    except Exception as e:  # noqa: BLE001 — an existing artifact means already recorded
        out["artifact"] = f"not written: {type(e).__name__}"
    if fresh:
        log(f"  ⚠ {as_of} 记为永久错过（不补跑）：{run_id}")
    return out


def _catch_up_monitoring(p: plat.Platform, now_hkt: datetime, now_utc: datetime,
                         since_d: date, *, run_recoverable: bool, dry_run: bool,
                         log: Callable[[str], None]) -> dict[str, Any]:
    """How much monitoring was missed, and the honest note that it is replayable."""
    try:
        rows = p.state.q("SELECT as_of, COUNT(*) AS n FROM orch_runs "
                         "WHERE kind='monitor' AND as_of>=? GROUP BY as_of",
                         (since_d.isoformat(),))
    except Exception as e:  # noqa: BLE001
        return {"summary": f"无法查询盯市记录：{type(e).__name__}: {e}"}
    seen = {str(r["as_of"]) for r in rows}
    days = [(since_d + timedelta(days=i)).isoformat()
            for i in range((now_hkt.date() - since_d).days + 1)]
    missing = [d for d in days if d not in seen]
    out: dict[str, Any] = {
        "days": len(days), "days_with_monitor": len(seen), "missing_days": missing,
        "recoverable": True,
        "why": ("盯市可完整恢复：成交、止损与告警都是已收盘 K 线的确定性函数，"
                "跑一次盯市就会把上次盯市到现在的每个交易日走完"),
    }
    if missing and run_recoverable:
        out["replay"] = _run_monitoring(p, now_hkt, now_utc, dry_run=dry_run,
                                        log=log).detail
    out["summary"] = (f"{len(missing)}/{len(days)} 天没有盯市记录，可完整补算"
                      + ("（已补）" if out.get("replay") else "（未补，加 "
                         "--run-recoverable）"))
    return out


# ---------------------------------------------------------------------------
def health(now_utc: datetime, *, p: plat.Platform | None = None) -> dict[str, Any]:
    """One answer to "is it running, and is this week's book done?".

    Written for an operator who is not at a laptop and for a probe that has no
    context: `alive` comes from the heartbeat's presence, not from anything the
    scheduler asserts about itself.
    """
    now_hkt = to_hkt(now_utc)
    p = p or plat.load()
    as_of, trig = weekly_period(now_hkt)
    out: dict[str, Any] = {
        "at_hkt": now_hkt.isoformat(), "platform": p.name, "venue": venue(),
        "current_period": as_of.isoformat(), "trigger_hkt": trig.isoformat(),
        "ports": [{"name": h.name, "ok": h.ok, "detail": h.detail}
                  for h in p.check()],
    }
    hb = None
    try:
        raw = p.cache.get(HEARTBEAT_KEY)
        hb = json.loads(raw.decode()) if raw else None
    except Exception as e:  # noqa: BLE001
        out["heartbeat_error"] = f"{type(e).__name__}: {e}"
    out["heartbeat"] = hb
    age = _age_s((hb or {}).get("at_utc"), now_utc)
    out["heartbeat_age_s"] = None if age is None else round(age, 1)
    # Absence is the signal. A key that expired means no tick within three
    # intervals, which is a stopped scheduler however quiet the week has been.
    # `age is None` rather than a falsy test: a heartbeat written this second has
    # age 0.0, and treating that as "no heartbeat" would report a healthy
    # scheduler as dead.
    stale_after = (hb or {}).get("interval_s", TICK_INTERVAL_S) * 3 + 60
    out["alive"] = bool(hb) and age is not None and age < stale_after
    try:
        state, why = _weekly_state(p, as_of, now_utc)
        out["weekly"] = {"state": state, "reason": why}
        last = p.state.q("SELECT run_id, as_of, ended_at, calls FROM orch_runs "
                         "WHERE kind='weekly' AND ok=1 ORDER BY as_of DESC")
        out["last_weekly"] = last[0] if last else None
        gaps = p.state.q("SELECT as_of, error FROM orch_runs WHERE kind=? "
                         "ORDER BY as_of DESC", (GAP_KIND,))
        out["gaps"] = [{"as_of": g["as_of"]} for g in gaps]
        mon = _last_monitor_at(p)
        out["last_monitor_utc"] = mon.isoformat() if mon else None
    except Exception as e:  # noqa: BLE001
        out["state_error"] = f"{type(e).__name__}: {e}"
    return out


def describe() -> list[dict[str, str]]:
    """The schedule, for the runbook and for `python3 -m ideagen.scheduler schedule`."""
    return [{"job": j.name, "kind": j.kind, "cadence": j.cadence,
             "may_trade": str(j.may_trade)} for j in SCHEDULE]


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """CLI for the container entrypoint and for an operator with one terminal.

    Exit codes are the interface: 0 healthy, 1 degraded (retrying is sensible),
    2 unrecoverable (a supervisor should stop, not restart).
    """
    ap = argparse.ArgumentParser("ideagen.scheduler")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tick", help="run whatever is due, once")
    t.add_argument("--now", help="ISO-8601 UTC instant; default: now")
    t.add_argument("--interval", type=int, default=TICK_INTERVAL_S,
                   help="loop interval the caller uses; sets the heartbeat TTL")
    t.add_argument("--force", choices=[j.name for j in SCHEDULE])
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--no-ingest", action="store_true")
    t.add_argument("--json", action="store_true")

    c = sub.add_parser("catch-up", help="classify a gap; record what is unrecoverable")
    c.add_argument("--since", required=True, help="YYYY-MM-DD")
    c.add_argument("--now", help="ISO-8601 UTC instant; default: now")
    c.add_argument("--run-recoverable", action="store_true")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--json", action="store_true")

    h = sub.add_parser("health", help="is it running, and is this week done")
    h.add_argument("--now")

    sub.add_parser("schedule", help="print the declared schedule")

    a = ap.parse_args(argv)
    now = (datetime.fromisoformat(a.now).astimezone(timezone.utc)
           if getattr(a, "now", None) else datetime.now(timezone.utc))

    if a.cmd == "schedule":
        print(json.dumps(describe(), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "health":
        print(json.dumps(health(now), ensure_ascii=False, indent=1, default=str))
        return 0
    if a.cmd == "catch-up":
        rep = catch_up(date.fromisoformat(a.since), now_utc=now,
                       run_recoverable=a.run_recoverable, dry_run=a.dry_run)
        if a.json:
            print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=1, default=str))
        return 0

    rep = tick(now, interval_s=a.interval, force=a.force, dry_run=a.dry_run,
               ingest=not a.no_ingest)
    if a.json:
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=1, default=str))
    if rep.fatal:
        print(f"FATAL {rep.fatal}", file=sys.stderr)
    return rep.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
