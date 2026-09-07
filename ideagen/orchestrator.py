"""The weekly run: one function, six ports, N strategies, one immutable journal.

This is the only module that knows the order things happen in. Strategies do not
persist, do not trade and do not read the clock; sources do not decide anything;
the platform does not know what a topic is. Keeping those separate is what lets
the investment logic change every week without the plumbing moving.

The run is a closed object. It takes a lock, records the health of every port,
walks its steps, writes every artifact under `runs/{as_of}/{run_id}/`, and closes
with a journal. Anything not written through a port does not survive, because the
cloud sandbox it runs in is discarded — so the journal is the complete record of
what the run saw and did.

Two properties matter more than the sequence:

**Exactly one run per period.** The lock is held for the whole run. A retry
overlapping the scheduled run would place the same orders twice, and a file lock
cannot coordinate two sandboxes — this is the reason the platform needs Redis.

**Every selector sees identical candidates.** They share one `RunContext` and one
`inputs_sha`. That is what makes their results differenceable: the common market
move cancels, so separating two selectors takes weeks rather than months.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from . import (config, feeds, platform as plat, schema,
               strategy as strat, universe as uni)


class _SkipDiscovery(Exception):
    """Discovery stopped for a reason it already reported. Not an error path."""


#: The columns `events` has always had. Anything else a feed reports goes to
#: `payload` — `feeds.py` promises extras are "allowed and preserved", and until
#: that column existed the promise held in memory and broke at the last step, so
#: a replay read a thinner row than the run was handed.
EVENT_CORE = ("event_id", "date", "label", "kind", "expectation", "actual",
              "unit", "source", "as_of", "feed")


def event_row(e: dict[str, Any]) -> dict[str, Any]:
    """One calendar row as `events` stores it, extras folded into `payload`.

    Module level rather than inline in `run()` so it can be tested without
    standing up a platform, a journal and a model. The behaviour worth testing
    is narrow and easy to get wrong: `actual` is stringified (the column is
    TEXT, and a float written straight through compares unequal to the same
    number read back), and a row with nothing extra stores NULL rather than
    `"{}"` — an empty object would make "this feed reported nothing extra" and
    "this row predates the column" look different when they are not.
    """
    extra = {k: v for k, v in e.items() if k not in EVENT_CORE}
    return {"event_id": e.get("event_id"), "date": e.get("date"),
            "label": e.get("label"), "kind": e.get("kind"),
            "expectation": e.get("expectation"),
            "actual": None if e.get("actual") is None else str(e["actual"]),
            "unit": e.get("unit"), "source": e.get("source"),
            "as_of": e.get("as_of"), "feed": e.get("feed"),
            "payload": (json.dumps(extra, ensure_ascii=False, default=str)
                        if extra else None)}


@dataclass
class RunResult:
    run_id: str
    as_of: str
    ok: bool
    skipped: str | None = None
    steps: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    n_candidates: int = 0
    generators: dict[str, dict[str, Any]] = field(default_factory=dict)
    selectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    journal: str | None = None
    calls: int = 0
    error: str | None = None

    @property
    def completed(self) -> bool:
        """Whether this period is actually done.

        `ok` alone is ambiguous: a run that exited because another sandbox held the
        lock is not a failure, so it reports `ok=True` — but nothing happened, and a
        scheduler that treated it as a finished period would skip the week. Callers
        deciding "is this period done" must ask this, not `ok`.
        """
        return self.ok and not self.skipped


def weekly(
    *,
    as_of: date,
    p: plat.Platform | None = None,
    topic_scorer: str = "hgep",
    generators: Iterable[str] | None = None,
    selectors: Iterable[str] | None = None,
    corpus: list[dict[str, Any]] | None = None,
    universe: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    calendar: list[dict[str, Any]] | None = None,
    prices: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    dry_run: bool = False,
    needs_inference: bool = False,
    verbose: bool = True,
) -> RunResult:
    """Run one period end to end.

    `corpus` / `candidates` / `calendar` / `prices` are injected rather than
    fetched here so the same function serves the live run, a replay of an old week
    and a test — the only difference being where the inputs came from. A replay
    that fetched its own data would not be a replay.
    """
    p = p or plat.load()
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    params = params or {}

    # Which ports this run needs is derived from the strategies it will actually
    # run, not asserted by the caller. A caller who forgets the flag would get a run
    # that fetches every feed, scores the topics, and only then finds that no
    # generator can call a model — after the expensive part. Asking the registry
    # makes the check match the run.
    # Injected candidates mean stage B does not run at all — a replay reuses the
    # pool it is replaying. Demanding a model key for generators that will never be
    # called would block exactly the runs that need no model.
    picked = [("topic_scorer", topic_scorer)]
    if candidates is None:
        picked += [("idea_generator", n) for n in
                   (list(generators) if generators
                    else [r["name"] for r in strat.available("idea_generator")])]
    picked += [("idea_selector", n) for n in
               (list(selectors) if selectors
                else [r["name"] for r in strat.available("idea_selector")])]
    model_arms = strat.needs_model(picked)
    need = list(plat.Platform.DEFAULT_NEED)
    if not dry_run and (needs_inference or model_arms):
        need.append("inference")
    if not p.ready(need=need):
        bad = [f"{h.name}: {h.detail}" for h in p.missing(need=need)]
        hint = (f"（本次要跑的模型策略：{', '.join(model_arms[:6])}）"
                if model_arms and any(h.name == "inference"
                                      for h in p.missing(need=need)) else "")
        return RunResult(run_id="-", as_of=as_of.isoformat(), ok=False,
                         error="platform not ready — " + "; ".join(bad) + hint)

    lock_key = f"weekly:{as_of.isoformat()}"
    with p.cache.lock(lock_key, ttl_s=3600) as got:
        if not got:
            log(f"  another run holds {lock_key}; exiting without doing anything")
            return RunResult(run_id="-", as_of=as_of.isoformat(), ok=True,
                             skipped="lock held by another run")

        j = plat.RunJournal(p, kind="weekly", as_of=as_of.isoformat())
        res = RunResult(run_id=j.run_id, as_of=as_of.isoformat(), ok=False)
        log(f"run {j.run_id}  platform={p.name}  as_of={as_of}")

        try:
            schema.migrate(p.state)
            # The run row is opened now, with ok=0, and closed at the end. Writing
            # it only on success leaves the rows a failed run already produced —
            # feed results, verdicts — with no parent to mark them untrustworthy,
            # so a half-finished run's output is indistinguishable from a good
            # one's. Opening first makes `ok` the single place that question is
            # answered, and `ended_at IS NULL` identifies runs that died outright.
            if not dry_run:
                schema.upsert(p.state, "orch_runs", {
                    "run_id": j.run_id, "as_of": as_of.isoformat(),
                    "kind": "weekly", "platform": p.name,
                    "started_at": j._t0.isoformat(), "ended_at": None,
                    "ok": 0, "error": None, "inputs_sha": None,
                    "journal_uri": None, "calls": 0,
                    "data_classification": params.get(
                        "data_classification", "live")}, replace=False)
                if params.get("supersede_completed"):
                    # A deliberate re-run of a period that already completed —
                    # the 2026-09-07 replay of every stored week through the
                    # corpus-first theme formation and the rebuilt G/E/P. The
                    # `orch_runs_done` index allows exactly one completed
                    # weekly per period, so without this the new run would
                    # spend every model call and then fail at close. The old
                    # row is not deleted or edited: its kind becomes
                    # `weekly_superseded`, it keeps its verdicts, candidates
                    # and journal, and the spine still counts it as an attempt.
                    old = supersede_completed(p.state, as_of.isoformat(),
                                              by=j.run_id)
                    if old:
                        j.step("superseded", run_ids=old,
                               reason=str(params.get("supersede_reason") or
                                          "同期重跑：旧运行改记为 weekly_superseded"))
                        log(f"  取代  {as_of} 已有 {len(old)} 次完成的运行改记为 "
                            f"weekly_superseded: {', '.join(old)}")

            # Feeds run through the registry unless inputs were injected. Injection
            # is what lets a replay of an old week reuse stored rows instead of
            # re-fetching, which is the difference between a replay and a new run.
            feed_results: list[feeds.FeedResult] = []
            if corpus is None:
                corpus, rs = feeds.fetch_kind("corpus", as_of)
                feed_results += rs
            else:
                feed_results.append(feeds.FeedResult(
                    feed=f"{params.get('input_source') or 'injected'}-corpus",
                    kind="corpus", as_of=as_of.isoformat(), rows=corpus,
                    meta={"data_classification": params.get(
                        "data_classification", "injected")}))
            if calendar is None:
                calendar, rs = feeds.fetch_kind("calendar", as_of)
                feed_results += rs
            else:
                feed_results.append(feeds.FeedResult(
                    feed=f"{params.get('input_source') or 'injected'}-calendar",
                    kind="calendar", as_of=as_of.isoformat(), rows=calendar,
                    meta={"data_classification": params.get(
                        "data_classification", "injected")}))
            if candidates is None:
                if universe is None:
                    universe, rs = feeds.fetch_kind("universe", as_of)
                    feed_results += rs
                else:
                    feed_results.append(feeds.FeedResult(
                        feed=f"{params.get('input_source') or 'injected'}-universe",
                        kind="universe", as_of=as_of.isoformat(), rows=universe,
                        meta={"data_classification": params.get(
                            "data_classification", "injected")}))
            corpus = corpus or []
            universe = universe or []
            # Whether this run generates has to be captured before the line
            # below erases the distinction: `candidates or []` turns "none were
            # handed in" and "an empty pool was handed in" into the same value,
            # and the shelf guard needs to tell them apart.
            generating = candidates is None
            candidates = candidates or []
            calendar = calendar or []
            # P 已定价 is computed from stored bars, and until 2026-09-06 no
            # live entry point handed any in: `cli weekly`, `clauderun` and
            # `poc_workflow` all arrived here with `prices=None`, the line
            # below turned it into `{}`, and every live period scored P = 50
            # for every theme while the panel drew it as a reading (Jon §4).
            # None now means "build it the way the backtest does"; an explicit
            # `{}` still means "score with no prices" — the public synthetic
            # path wants exactly that — but it is journaled as such, so a
            # period whose P was never measured says so in its own record.
            prices, price_step = _price_inputs(p, as_of, prices, dry_run)
            j.step("prices", **price_step)
            if price_step.get("error") or price_step.get("skipped"):
                log(f"  行情  {price_step.get('error') or price_step.get('skipped')}"
                    f"——本期 P 已定价全部为缺数默认值，不是读数")
            else:
                log(f"  行情  {price_step['codes']} 个指示标的，实测 "
                    f"{price_step['measured']} · 缺数默认 {price_step['defaulted']}"
                    f" · 无序列 {price_step['missing']}"
                    f"（截止 {price_step.get('last_d')}，来源 {price_step['source']}）")

            # The mandate limits expression to funds, ETFs and daily-dealing hedge
            # funds. Applying that here, once, is what stops it from becoming four
            # separate almost-identical filters inside four generators — and the
            # exclusions are recorded, because a theme whose only clean expression
            # is an unconfirmed vehicle is a gap to close, not a theme to drop.
            excluded: dict[str, str] = {}
            dating = uni.shelf_asof_coverage(universe) if universe else None
            if universe:
                universe, excluded = uni.eligible(universe, as_of=as_of)
            # Recorded even at zero. Under `if universe:` the step vanished
            # exactly when it mattered, so a run with no shelf left no trace of
            # having had none — the count that would have said so was inside
            # the branch the emptiness skipped.
            j.step("universe", eligible=len(universe), excluded=len(excluded),
                   reasons=_reason_counts(excluded), shelf_dating=dating)
            if universe:
                log(f"  可用标的 {len(universe)}（排除 {len(excluded)}）"
                    + (f"　⚠ {dating['undated']} 个标的没有上架日期，"
                       f"回放这些期次不算 as-of 干净"
                       if dating and dating["undated"] else ""))

            # Fails here rather than at stage B, where it would otherwise
            # surface. The stage-B guard does catch an empty shelf — with no
            # instruments the generators produce nothing and it refuses the
            # period — but it says 「生成器全部失败或全被丢弃」, which sends the
            # reader to the generators and the model, three layers below the
            # actual fault. The corpus guard exists for the same reason one
            # layer up: name the input that was missing, not the step that
            # noticed. Checked against the 2026-09-09 trigger, where the cloud
            # instance has a shelf of zero and this is the message it will give.
            #
            # Only when this run is generating. A batch handed in through
            # `candidates` has already chosen its instruments, and demanding a
            # shelf it will not consult would refuse a run that is complete.
            if not universe and generating:
                raise RuntimeError(
                    f"{as_of} 可用标的为零，筛选B 没有东西可以表达主题——"
                    f"这次运行不算完成。「货架没同步过来」和「本周没有合规标的」"
                    f"必须区分开")

            for fr in feed_results:
                j.step(f"feed:{fr.feed}", kind=fr.kind, n=len(fr.rows),
                       ok=fr.ok, error=fr.error)
                if not dry_run:
                    schema.upsert(p.state, "feed_runs", {
                        "run_id": j.run_id, "feed": fr.feed, "kind": fr.kind,
                        "as_of": fr.as_of, "n_rows": len(fr.rows),
                        "ok": 1 if fr.ok else 0, "error": fr.error,
                        "rows_sha": fr.sha})
                log(f"  feed {fr.feed:<20}{len(fr.rows):>5} rows"
                    + ("" if fr.ok else f"   ! {str(fr.error)[:60]}"))

            # Calendar rows are upserted so a threshold can be compared against a
            # level, and a watchpoint can name an event that already exists.
            if calendar and not dry_run:
                for e in calendar:
                    schema.upsert(p.state, "events", event_row(e))

            # Theme discovery runs before scoring, every week, in the run itself.
            # The founding requirement is that topics emerge from the corpus
            # rather than living in a frozen dictionary — and a discovery step
            # that exists only as a manual CLI command is a frozen dictionary
            # with extra steps, because nobody runs it (the registry sat still
            # from 08-08 until this was wired). The loop itself lives in
            # `themes.discover` since 2026-09-07 so it can be tested against a
            # scripted model; what it writes — new themes append-only with
            # registered_d = as_of, aliases for old themes under new wording,
            # dated the same way — replays of earlier weeks still cannot see.
            from . import themes as _themes
            if not dry_run and corpus and not params.get("skip_theme_discovery"):
                try:
                    from . import db as _db
                    # A backfilled period names its themes today, with a
                    # model that has read the weeks after `as_of`. The
                    # registry row carries that fact (see `themes.mint`), the
                    # same way the run row carries `data_classification`.
                    note = ("" if params.get("data_classification", "live") == "live"
                            else f"于 {config.today_hkt().isoformat()} 事后补跑中命名"
                                 f"（模型已见过 {as_of.isoformat()} 之后的世界）")
                    _themes.discover(_db.init(), as_of,
                                     getattr(p, "inference", None),
                                     step=j.step, log=log, minted_note=note,
                                     corpus=corpus)
                except Exception as e:  # noqa: BLE001 — discovery must not cost the run
                    j.step("theme_discovery", error=f"{type(e).__name__}: {e}")
                    log(f"  ⚠ 主题发现失败（本周用既有注册表继续）: {e}")

            # Freeze the definitions this period scores with, after discovery
            # has had its say and before 筛选A reads a single document. The
            # artifact is the full set; `theme_set_sha` travels on the topics
            # step and every topic verdict, so a score can always be matched to
            # the vocabulary that produced it. A later registration or alias
            # dated after this period leaves the sha unchanged — that is the
            # as-of clamp, made checkable.
            theme_set = _themes.snapshot(as_of)
            theme_set_sha = theme_set["theme_set_sha"]
            j.step("theme_set", sha=theme_set_sha, n_themes=theme_set["n_themes"])
            if not dry_run:
                res.artifacts.append(j.artifact("A_theme_set.json", _blob(theme_set)))
            log(f"  主题定义  {theme_set['n_themes']} 个，sha={theme_set_sha}")

            # A run with no corpus is a failed run, not a successful empty one.
            # Every input validates cleanly at zero rows, so without this check the
            # sequence "no corpus → no topics → no ideas → done" reports success, and
            # a week whose source was down becomes indistinguishable from a week with
            # nothing to trade. That is the same failure the feed layer refuses to
            # commit, and it must not be reintroduced one layer up.
            if not corpus:
                raise RuntimeError(
                    f"{as_of} 没有任何研报，筛选A 无从打分——这次运行不算完成。"
                    f"「数据源不通」和「本周没料」必须区分开")

            # One hash over every input. Two verdicts that disagree on this were
            # not looking at the same thing and must not be compared.
            inputs_sha = strat.RunContext.sha(
                [c.get("doc_id") for c in corpus],
                [c.get("id") for c in candidates],
                sorted(prices.keys()),
                [e.get("event_id") for e in calendar])
            # The hash's own ingredients, frozen. `inputs_sha` covers the
            # documents *and* the calendar this run was handed, but the events
            # table is mutable: a later run upserting the same event ids
            # changes what a reconstruction computes, so a period that
            # verified byte-for-byte last week silently stops verifying (seen
            # on 2026-08-26 after a backfill touched the calendar). The
            # journal is immutable, so recording the id lists here is what
            # keeps "these are the inputs behind this decision" checkable for
            # as long as the artifact exists.
            j.step("inputs", corpus=len(corpus), candidates=len(candidates),
                   calendar=len(calendar), prices=len(prices), sha=inputs_sha,
                   doc_ids=[str(d.get("doc_id")) for d in corpus],
                   event_ids=[str(e.get("event_id")) for e in calendar])
            res.steps.append("inputs")
            log(f"  inputs  corpus={len(corpus)} candidates={len(candidates)} "
                f"calendar={len(calendar)} sha={inputs_sha}")

            # The claim cache rides on the state store so a document the
            # model has already read for G is not paid for again next period;
            # a dry run writes nothing, so it gets none.
            from . import claims as _claims
            ctx = strat.RunContext(
                as_of=as_of, inputs_sha=inputs_sha, corpus=corpus,
                candidates=candidates, prices=prices, calendar=calendar,
                params=params, infer=(None if dry_run else p.inference),
                claim_cache=(None if dry_run else _claims.ClaimCache(p.state)))

            # ---- 筛选A: corpus → 5 topics ----------------------------------
            topics: list[dict[str, Any]] = []
            if corpus:
                tv = strat.run("topic_scorer", topic_scorer, ctx)
                topics = _topic_rows(tv, as_of)
                res.topics = tv.chosen
                res.calls += tv.calls
                # The verdict names the definition set it scored against.
                tv.meta["theme_set_sha"] = theme_set_sha
                j.step("topics", strategy=tv.strategy, version=tv.version,
                       chosen=tv.chosen, calls=tv.calls,
                       theme_set_sha=theme_set_sha)
                res.steps.append("topics")
                if not dry_run:
                    res.artifacts.append(j.artifact(
                        "A_topics.json", _blob(tv.as_row(ctx, "topic_scorer"))))
                    _save_verdict(p, j.run_id, ctx, "topic_scorer", tv,
                                  strat.spec("topic_scorer", topic_scorer)["role"])
                log(f"  筛选A  {tv.strategy} v{tv.version} → {len(tv.chosen)}: "
                    f"{', '.join(tv.chosen[:5])}")

                # Every mechanical (no-model) topic scorer also runs and persists,
                # purely as a recorded opinion — ideas are only generated from the
                # primary scorer's topics. This is how the "语义打分 vs 纯数数"
                # disagreement becomes data instead of argument: each week both
                # verdicts land side by side at zero model cost, and once outcomes
                # exist the backtest layer can ask which topic list the profitable
                # ideas actually came from.
                for r in strat.available("topic_scorer"):
                    if r["name"] == topic_scorer or r.get("needs_model"):
                        continue
                    try:
                        cv = strat.run("topic_scorer", r["name"], ctx)
                    except Exception as e:  # noqa: BLE001 — a control must not cost the run
                        j.step(f"topics:{r['name']}", error=f"{type(e).__name__}: {e}")
                        continue
                    cv.meta["theme_set_sha"] = theme_set_sha
                    if not dry_run:
                        res.artifacts.append(j.artifact(
                            f"A_topics_{r['name']}.json",
                            _blob(cv.as_row(ctx, "topic_scorer"))))
                        _save_verdict(p, j.run_id, ctx, "topic_scorer", cv,
                                      r["role"])
                    agree = len(set(cv.chosen) & set(tv.chosen))
                    log(f"  筛选A対照 {cv.strategy} → {', '.join(cv.chosen[:5])}"
                        f"（与主打分重合 {agree}/{len(tv.chosen)}）")

            # ---- 筛选B: each generator writes ideas for every topic --------
            #
            # All generators' output is pooled into one candidate set rather than
            # kept in four separate pools. Running a full 4×4 cross of generators
            # against selectors would produce sixteen books, and sixteen books
            # cannot be told apart on the history this system will have for a long
            # time. Pooling keeps the selector comparison clean — every selector
            # sees an identical pool, so the comparison stays paired — and the
            # generators are then judged observationally, from the `method` tag on
            # whichever of their ideas got held.
            gen_verdicts: dict[str, strat.Verdict] = {}
            if topics and not candidates:
                gctx = ctx.with_(topics=topics, universe=universe)
                gnames = list(generators) if generators else \
                    [r["name"] for r in strat.available("idea_generator")]
                gen_verdicts = strat.run_all("idea_generator", gctx, names=gnames)
                pool: list[dict[str, Any]] = []
                for n, v in gen_verdicts.items():
                    res.calls += v.calls
                    res.generators[n] = {
                        "n": len(v.produced),
                        "role": strat.spec("idea_generator", n)["role"],
                        "per_topic": v.meta.get("per_topic", {}),
                        "dropped": len(v.rejected), "calls": v.calls,
                        "error": v.meta.get("error"),
                    }
                    pool.extend(v.produced)
                    if not dry_run:
                        res.artifacts.append(j.artifact(
                            f"B_generators/{n}.json",
                            _blob(v.as_row(gctx, "idea_generator"))))
                        _save_verdict(p, j.run_id, gctx, "idea_generator", v,
                                      strat.spec("idea_generator", n)["role"])
                raw_pool = len(pool)
                candidates = _merge_pool(pool)
                j.step("pool", raw=raw_pool, merged=len(candidates),
                       convergence=_convergence(candidates))
                log(f"  池子  {raw_pool} 个想法 → 合并为 {len(candidates)} 个标的")
                j.step("generators", n=len(gen_verdicts), pool=len(pool),
                       produced={n: len(v.produced) for n, v in gen_verdicts.items()},
                       errors={n: v.meta.get("error") for n, v in gen_verdicts.items()
                               if v.meta.get("error")})
                res.steps.append("generators")
                for n, v in sorted(gen_verdicts.items()):
                    mark = "!" if v.meta.get("error") else " "
                    log(f"  {mark} 筛选B {n:<16}{len(v.produced):>4} 想法"
                        f"  丢弃 {len(v.rejected)}"
                        + (f"   {str(v.meta.get('error'))[:60]}"
                           if v.meta.get("error") else ""))

            # ---- 筛选C: every selector over the identical pool -------------
            if candidates:
                # Only what a book can actually hold. 51 of the 79 candidates of
                # 2026-09-02 were shelf funds with no NAV series on this node;
                # every selector was ranking them, three arms picked nothing
                # else, and booking then dropped the lot as 「当日无价」 — so
                # the arms were compared on a pool most of which could never
                # be owned. The pool keeps every candidate (flagged), the
                # selectors see the markable ones, and the count is recorded.
                markable_ids, unmark = _markable_candidates(p, as_of, candidates, dry_run)
                for c in candidates:
                    c["markable"] = str(c.get("id")) in markable_ids
                sel_pool = [c for c in candidates if c["markable"]]
                j.step("selectors:markable", pool=len(candidates),
                       markable=len(sel_pool), unmarkable=len(unmark),
                       unmarkable_ids=unmark[:80])
                if unmark:
                    log(f"  可盯市  {len(sel_pool)}/{len(candidates)} 只候选有价格或净值序列，"
                        f"其余 {len(unmark)} 只不进筛选C")
                candidates_all = candidates
                candidates = sel_pool
                # Stage C hashes its own inputs. Two selectors are comparable only
                # if they agree on this, and it is deliberately not stage B's hash:
                # what stage C must agree on is the pool, not how the pool was made.
                csha = strat.RunContext.sha([c.get("id") for c in candidates])
                # Stage A priced the themes' indicators; stage C's momentum
                # control ranks *candidates* on their own trailing return, and
                # with only indicator codes in `prices` it chose nothing in all
                # six 2026-09-07 replays (「筛选C mom_21 0 持仓」) while every
                # other arm chose ten. The backtest context has always priced
                # the pool; the live run now does the same, after the pool
                # exists and before any selector reads it.
                extra, psumm = _candidate_prices(p, as_of, candidates, prices, dry_run)
                if extra:
                    prices = {**prices, **extra}
                j.step("prices:candidates", **psumm)
                cctx = ctx.with_(topics=topics, universe=universe,
                                 candidates=candidates, inputs_sha=csha,
                                 prices=prices)
                names = list(selectors) if selectors else \
                    [r["name"] for r in strat.available("idea_selector")]
                verdicts = strat.run_all("idea_selector", cctx, names=names)
                for n, v in verdicts.items():
                    res.calls += v.calls
                    res.selectors[n] = {
                        "chosen": v.chosen, "n": len(v.chosen),
                        "role": strat.spec("idea_selector", n)["role"],
                        "by_method": _tally(candidates, v.chosen, "method"),
                        "by_topic": _tally(candidates, v.chosen, "topic_id"),
                        "calls": v.calls, "error": v.meta.get("error"),
                    }
                    if not dry_run:
                        res.artifacts.append(j.artifact(
                            f"C_selectors/{n}.json",
                            _blob(v.as_row(cctx, "idea_selector"))))
                        _save_verdict(p, j.run_id, cctx, "idea_selector", v,
                                      strat.spec("idea_selector", n)["role"])
                j.step("selectors", n=len(verdicts), pool=len(candidates),
                       inputs_sha=csha,
                       chosen={n: len(v.chosen) for n, v in verdicts.items()},
                       errors={n: v.meta.get("error") for n, v in verdicts.items()
                               if v.meta.get("error")})
                res.steps.append("selectors")
                for n, v in sorted(verdicts.items()):
                    mark = "!" if v.meta.get("error") else " "
                    log(f"  {mark} 筛选C {n:<16}{len(v.chosen):>4} 持仓"
                        + (f"   {str(v.meta.get('error'))[:60]}"
                           if v.meta.get("error") else ""))

                # The pool that is recorded is the whole pool, flagged — the
                # unmarkable candidates are still the generators' output and
                # the panel shows them with their tag. Only stage C's input
                # was narrowed.
                res.n_candidates = len(candidates_all)
                if not dry_run:
                    _save_candidates(p, j.run_id, cctx, candidates_all)
                    res.artifacts.append(j.artifact(
                        "B_pool.json", _blob(candidates_all)))

            # A period that produced no candidate is a failed period, whatever
            # happened along the way. Every generator failing on a connection
            # error still walked the happy path to here, so the run was stored
            # ok=1 with an empty pool — and the dashboard then counted it as a
            # completed week, filling a gap that is still a gap (seen on the
            # 2026-09-02 retry). Stage B is allowed to be empty only when the
            # caller asked for no generators at all.
            if not dry_run and generators is not False and not res.n_candidates:
                raise RuntimeError(
                    "筛选B 一条想法都没有产出（生成器全部失败或全被丢弃）——"
                    "这一期没有可挑的候选，不能记为成功")

            res.ok = True
            res.journal = j.close(ok=True) if not dry_run else None
            if not dry_run:
                p.state.execute(
                    "UPDATE orch_runs SET ended_at=?, ok=1, inputs_sha=?, "
                    "journal_uri=?, calls=? WHERE run_id=?",
                    (plat.utcnow_iso(), inputs_sha, res.journal, res.calls,
                     j.run_id))
            log(f"  done  {len(res.artifacts)} artifacts, {res.calls} model calls")
            if res.journal:
                log(f"  journal {res.journal}")
            return res

        except Exception as e:  # noqa: BLE001
            # `ok` is cleared here, not merely left alone. It is set optimistically
            # before the run row is closed, so a failure during persistence would
            # otherwise return ok=True carrying an error — a result that claims
            # success and reports failure at the same time, which is worse for a
            # caller than a plain failure because it is silently believed.
            res.ok = False
            res.error = f"{type(e).__name__}: {e}"
            if not dry_run:
                try:
                    res.journal = j.close(ok=False, error=res.error)
                except Exception:  # noqa: BLE001
                    pass
                try:    # close the run row so its rows are marked untrustworthy
                    p.state.execute(
                        "UPDATE orch_runs SET ended_at=?, ok=0, error=?, "
                        "journal_uri=? WHERE run_id=?",
                        (plat.utcnow_iso(), res.error, res.journal, j.run_id))
                except Exception:  # noqa: BLE001
                    pass
            log(f"  ! {res.error}")
            return res


def supersede_completed(state, as_of: str, *, by: str) -> list[str]:
    """Retire the completed weekly runs of `as_of` so a re-run can complete.

    Returns the run ids retired. Rows are re-labelled, never removed: a
    superseded run is still the run that produced last month's books, and a
    reader of the spine has to be able to see that the period was produced
    twice and by which run each time.
    """
    rows = state.q("SELECT run_id FROM orch_runs WHERE kind='weekly' AND as_of=? "
                   "AND ok=1 AND run_id<>? ORDER BY started_at", (as_of, by))
    ids = [str(dict(r)["run_id"]) for r in rows]
    for rid in ids:
        state.execute("UPDATE orch_runs SET kind='weekly_superseded' "
                      "WHERE run_id=?", (rid,))
    return ids


def _merge_pool(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the four generators' output to one candidate per instrument.

    Without this the pool holds one row per (method, topic, instrument), so the
    same ETF arrives many times over — and a selector picking ten rows can return
    six distinct positions with one of them at triple weight. That is not the
    ten-position portfolio the mandate describes, and every selector would have to
    remember to guard against it independently. Merging once, here, makes the
    defect unrepresentable rather than repeatedly avoided.

    Odds are merged by **median**, not by best. Taking the most attractive
    proposal would hand the pool to whichever generator is most optimistic, which
    would then look like skill when it is only aggression; the median cannot be
    moved by one arm reaching. Convergence is kept as its own field instead: four
    independent methods arriving at the same instrument is information, and it is
    information a selector should be able to use explicitly rather than absorb as
    a duplicate.
    """
    from statistics import median

    groups: dict[str, list[dict[str, Any]]] = {}
    for c in pool:
        groups.setdefault(str(c.get("instrument_id")), []).append(c)

    out: list[dict[str, Any]] = []
    for iid, rows in groups.items():
        rows = sorted(rows, key=lambda r: (str(r.get("method")), str(r.get("topic_id"))))
        methods = sorted({str(r.get("method")) for r in rows})
        topics = sorted({str(r.get("topic_id")) for r in rows})
        top_topic = max(topics, key=lambda t: (
            sum(1 for r in rows if str(r.get("topic_id")) == t), t))

        def med(key: str) -> float:
            vals = [float(r[key]) for r in rows if r.get(key) is not None]
            return round(median(vals), 4) if vals else 0.0

        # Every contributing proposal, whole. `theses` below keys by method and
        # so keeps one thesis per method; a method that reached GLD through four
        # topics had three of its arguments overwritten, and the panel then
        # showed the instrument under a single "main" topic. Jon's reading
        # (2026-09-06): the topics must not be collapsed — a reader has to be
        # able to see which topic produced which idea by which method, with the
        # direction, horizon and scenario odds each one was written with. The
        # merged row stays the unit of selection; this list is the unit of
        # explanation, and it is stored with the row so no later reader has to
        # reopen four generator artifacts to recover it.
        proposals = [{
            "id": r.get("id"),
            "topic_id": str(r.get("topic_id")),
            "method": str(r.get("method")),
            "thesis": r.get("thesis"),
            "upside_pct": r.get("upside_pct"),
            "downside_pct": r.get("downside_pct"),
            "p_up": r.get("p_up"), "p_base": r.get("p_base"),
            "p_down": r.get("p_down"),
            "horizon_days": r.get("horizon_days"),
            "vehicle": r.get("vehicle"), "exposure": r.get("exposure"),
            "citations": [str(x) for x in (r.get("citations") or []) if x],
        } for r in rows]
        topic_counts: dict[str, int] = {}
        for r in rows:
            t = str(r.get("topic_id"))
            topic_counts[t] = topic_counts.get(t, 0) + 1

        base = dict(rows[0])
        base.update({
            "id": f"pool:{iid}",
            "instrument_id": iid,
            "topic_id": top_topic,
            "method": methods[0] if len(methods) == 1 else "merged",
            "upside_pct": med("upside_pct"),
            "downside_pct": med("downside_pct"),
            "p_up": med("p_up"), "p_base": med("p_base"), "p_down": med("p_down"),
            # Provenance, so a realised outcome can still be attributed back to the
            # generators that argued for it. A merged candidate with no record of
            # its contributors would make the generator comparison unrecoverable.
            "proposed_by": methods,
            # Convergence as a number: distinct methods, not contributing rows
            # (one method reaching the same instrument through two topics is
            # persistence, not agreement). Stored so selectors and the panel
            # read one field instead of each re-deriving it.
            "n_methods": len(methods),
            "n_proposals": len(rows),
            "topics": topics,
            # Proposals per source topic — the number the topic filter reports
            # as «想法», next to the instrument count it reports as «标的».
            "topic_counts": topic_counts,
            "proposals": proposals,
            # Kept for readers written against it; `proposals` is the complete
            # record and this is a per-method sample of it.
            "theses": {str(r.get("method")): r.get("thesis") for r in rows},
        })
        out.append(base)
    out.sort(key=lambda c: (-c["n_proposals"], str(c["instrument_id"])))
    return out


def _convergence(candidates: list[dict[str, Any]]) -> dict[str, int]:
    """How many instruments how many distinct methods agreed on.

    Counted over distinct methods, not over contributing rows. One method can reach
    the same instrument through several topics, so a row count reads as "ten methods
    agreed" when at most four exist — which would turn the convergence signal into a
    number that cannot mean what it appears to mean.
    """
    out: dict[str, int] = {}
    for c in candidates:
        n = len(c.get("proposed_by") or []) or 1
        k = f"{n}种方式"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), reverse=True))


def _reason_counts(excluded: dict[str, str]) -> dict[str, int]:
    """Exclusions grouped by reason, so the journal shows the shape of the gap."""
    out: dict[str, int] = {}
    for r in excluded.values():
        out[r] = out.get(r, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _price_inputs(p, as_of: date, prices: dict[str, Any] | None,
                  dry_run: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """The price view stage A scores P from, and the journal line that says
    where it came from.

    Three inputs are told apart because they used to collapse into one `{}`:

      * `prices` given (even empty) — injected by the caller; a replay or the
        synthetic path. Used as is, and counted, so an injected `{}` is
        recorded as "0 measured" rather than looking like a quiet week.
      * `prices` None, live run — built from stored bars for every indicator
        of every theme registered as of `as_of`, with the backtest's clamp.
      * `prices` None, but no bar table is reachable (a state engine with no
        sqlite connection, or a database that has never had prices) — an
        empty view, with `error` naming the reason. This is the case that
        must not return quietly: an empty dict here means every theme's P is
        a fill, and a summary that said `codes=0` without a reason would read
        as "nothing to price" rather than "could not price".

    The connection is the state store's own, read-only. `futu_px` only reads
    here, so the transaction-mode mismatch that makes `scheduler._legacy_con`
    insist on `db.init()` for *writes* does not apply — and reaching for
    `db.init()` would open whatever `IDEAGEN_DB` points at, which is not the
    store this run is writing its verdicts to.
    """
    from . import pricing
    if prices is not None:
        summ = pricing.summarize(prices)
        return prices, {**summ, "source": "injected"}
    if dry_run:
        return {}, {"codes": 0, "measured": 0, "defaulted": 0, "missing": 0,
                    "source": "none", "skipped": "dry_run，未读取行情"}
    con = getattr(p.state, "connection", None)
    if con is None:
        return {}, {"codes": 0, "measured": 0, "defaulted": 0, "missing": 0,
                    "source": "none",
                    "error": f"状态库引擎 {getattr(p.state, 'dialect', '?')} "
                             f"没有本地 K 线表可读"}
    try:
        built, summ = pricing.build_prices(con, as_of)
    except Exception as e:  # noqa: BLE001 — P must degrade to "unmeasured", loudly
        return {}, {"codes": 0, "measured": 0, "defaulted": 0, "missing": 0,
                    "source": "none",
                    "error": f"读取 K 线失败：{type(e).__name__}: {e}"}
    return built, {**summ, "source": "built:prices-table"}


def _markable_candidates(p, as_of: date, candidates: list[dict[str, Any]],
                         dry_run: bool) -> tuple[set[str], list[str]]:
    """Which candidates a paper book could hold on `as_of`, and which it could not.

    The same test booking applies (`booking._priced_only`: a listed code with
    a close on or before the date), asked here so stage C ranks only what can
    be owned. Without a readable price table (a state engine with no sqlite
    connection, or a dry run) nothing is excluded and the caller's journal
    line carries zero unmarkable — "not checked" must not read as "all fine",
    so the second value then says why.
    """
    ids = {str(c.get("id")) for c in candidates}
    if dry_run:
        return ids, []
    con = getattr(p.state, "connection", None)
    if con is None:
        return ids, []
    from . import booking
    ok, bad = booking._priced_only(con, candidates, as_of)
    bad_set = set(bad)
    return ({str(c.get("id")) for c in candidates
             if str(c.get("instrument_id") or "") not in bad_set}, bad)


def _candidate_prices(p, as_of: date, candidates: list[dict[str, Any]],
                      have: dict[str, Any], dry_run: bool
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Price view for the pool's listed instruments, on top of `have`.

    Same clamp and same recipe as the stage-A view (`pricing.price_view`), so
    a selector reading `ret_21s` for a candidate gets the number the backtest
    would have handed it. Codes already in `have` are not fetched twice; a
    candidate that resolves to no listed code (an Olive fund) is counted as
    unlisted rather than silently absent.
    """
    from . import backtest as _bt, pricing, universe as uni
    summ: dict[str, Any] = {"candidates": len(candidates), "codes": 0,
                            "measured": 0, "unlisted": 0, "source": "none"}
    if dry_run:
        return {}, {**summ, "skipped": "dry_run，未读取行情"}
    codes: set[str] = set()
    for c in candidates:
        code = c.get("futu_code")
        if not code:
            hit = uni.resolve(str(c.get("instrument_id") or ""))
            code = getattr(hit, "futu_code", None) if hit else None
            if code:
                # The selectors look prices up by `futu_code` on the candidate
                # row (select_momentum does exactly that), and a live pool row
                # carries only `instrument_id`. Resolving it here, once, is
                # what lets the momentum control see the view built for it —
                # it chose nothing in every 2026-09-07 reselect until then.
                c["futu_code"] = str(code)
        if not code:
            summ["unlisted"] += 1
            continue
        if code not in have:
            codes.add(str(code))
    summ["codes"] = len(codes)
    if not codes:
        return {}, summ
    con = getattr(p.state, "connection", None)
    if con is None:
        return {}, {**summ, "error": f"状态库引擎 {getattr(p.state, 'dialect', '?')} "
                                     f"没有本地 K 线表可读"}
    try:
        view = pricing.price_view(con, as_of, sorted(codes), _bt.clamp_dates(as_of))
    except Exception as e:  # noqa: BLE001 — a selector without prices must say so, not crash the run
        return {}, {**summ, "error": f"读取候选行情失败：{type(e).__name__}: {e}"}
    # "Measured" for a candidate means the trailing return a selector ranks
    # on exists; the one-year percentile (`priced_in`) is stage A's need and
    # a young ETF can lack it while its 21-session return is perfectly real.
    summ["measured"] = sum(1 for v in view.values() if v.get("ret_21s") is not None)
    return view, {**summ, "source": "built:prices-table"}


def _topic_rows(tv, as_of: date) -> list[dict[str, Any]]:
    """Turn 筛选A's verdict into the topic objects 筛选B reads.

    Two things travel with the topic and neither is decoration.

    The evidence, because a generator that had to rediscover why a topic scored
    would be scoring it a second time by a different method, leaving the idea
    resting on a judgement no stored artifact records.

    The theme's own vocabulary — its terms, its key question, its exposures — as of
    the run date, because that is what lets a generator pull the documents actually
    behind this topic. Without it, "the corpus for this topic" degrades to "the
    corpus", every topic's prompt looks the same, and five topics produce one
    topic's ideas five times.
    """
    from . import lexicon

    known = {t.id: t for t in lexicon.all_themes(as_of)}
    rows = []
    for tid in tv.chosen:
        sc = tv.scores.get(tid) if isinstance(tv.scores, dict) else None
        sc = sc if isinstance(sc, dict) else {}
        th = known.get(tid)
        rows.append({
            "topic_id": tid,
            "label": (getattr(th, "label", None) or sc.get("label")
                      or sc.get("name") or tid),
            "key_question": getattr(th, "key_question", "") or "",
            "terms": list(getattr(th, "terms", []) or []),
            "exposures": list(getattr(th, "exposures", []) or []),
            "score": sc.get("score") or sc.get("total"),
            "evidence": sc.get("evidence") or sc.get("why") or sc.get("detail") or "",
            "factors": {k: v for k, v in sc.items()
                        if k in ("H", "G", "E", "P", "h", "g", "e", "p")},
        })
    return rows


def _tally(candidates: list[dict[str, Any]], chosen: list[str],
           key: str) -> dict[str, int]:
    """How a selector's ten distribute over one attribute.

    Recorded at selection time because it is the concentration question: ten
    positions drawn from one topic behave like one position, and that has to be
    visible in the run record rather than reconstructed after a bad month.

    For `method` the count is taken from `proposed_by`, not from the `method` field.
    After the pool merge most candidates are labelled "merged", so reading `method`
    would report that the ideas came from nowhere in particular and the generator
    comparison would be lost — the exact thing the merge was careful to preserve.
    A candidate that four generators argued for credits all four, which is the
    honest reading: they all backed it, and they are all answerable for it.
    """
    by_id = {str(c.get("id")): c for c in candidates}
    out: dict[str, int] = {}
    for cid in chosen:
        c = by_id.get(str(cid)) or {}
        if key == "method" and c.get("proposed_by"):
            keys = [str(m) for m in c["proposed_by"]]
        else:
            keys = [str(c.get(key) or "?")]
        for k in keys:
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _save_candidates(p, run_id: str, ctx, candidates: list[dict[str, Any]]) -> None:
    """Persist the whole pool, not only what was held.

    The ideas that were not selected are the counterfactual. Without them there is
    no way to ask whether 筛选C actually added anything over holding the pool, and
    that question is the only evidence that the selection stage is worth its cost.
    """
    for c in candidates:
        schema.upsert(p.state, "candidates", {
            "run_id": run_id, "as_of": ctx.as_of.isoformat(),
            "candidate_id": str(c.get("id")),
            "instrument_id": str(c.get("instrument_id")),
            "topic_id": str(c.get("topic_id") or ""),
            "method": str(c.get("method") or ""),
            "upside_pct": c.get("upside_pct"),
            "downside_pct": c.get("downside_pct"),
            "p_up": c.get("p_up"), "p_base": c.get("p_base"),
            "p_down": c.get("p_down"),
            "payload": json.dumps(_finite(c), ensure_ascii=False, default=str,
                                  allow_nan=False)})


def _save_verdict(p, run_id: str, ctx, kind: str, v, role: str) -> None:
    """Persist one verdict. `version` and `inputs_sha` travel with it, because a
    stored score whose producing logic cannot be identified is not auditable."""
    schema.upsert(p.state, "verdicts", {
        "run_id": run_id, "as_of": ctx.as_of.isoformat(), "kind": kind,
        "strategy": v.strategy, "version": v.version, "role": role,
        "inputs_sha": ctx.inputs_sha,
        "chosen": json.dumps(v.chosen, ensure_ascii=False),
        "scores": json.dumps(_finite(v.scores), ensure_ascii=False,
                             default=str, allow_nan=False),
        "rejected": json.dumps(v.rejected, ensure_ascii=False),
        "meta": json.dumps(_finite(v.meta), ensure_ascii=False,
                           default=str, allow_nan=False),
        "calls": v.calls})


def _finite(obj: Any) -> Any:
    """Replace non-finite floats so artifacts stay valid JSON.

    A ratio with zero downside is legitimately infinite, and several selectors
    compute one. But `json.dumps` writes `Infinity` and `NaN`, which are not JSON:
    every artifact containing one becomes unreadable to a strict parser, and these
    artifacts exist precisely so a decision can be re-read years later. Sanitising
    at the single serialisation boundary fixes it for every strategy, including the
    ones not written yet — a per-strategy cap would have to be remembered each time.
    """
    if isinstance(obj, float):
        if obj != obj:
            return None                     # NaN: absent, not a number
        if obj == float("inf"):
            return "+inf"
        if obj == float("-inf"):
            return "-inf"
        return obj
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def _blob(obj: Any) -> bytes:
    # allow_nan=False turns any leak past `_finite` into a loud error rather than
    # an artifact that silently fails to parse later.
    return json.dumps(_finite(obj), ensure_ascii=False, indent=1,
                      default=str, allow_nan=False).encode()
