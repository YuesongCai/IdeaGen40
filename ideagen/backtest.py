"""Evaluate a strategy against history without re-running the live pipeline.

The strategy layer is now pluggable, which means a new selector can be written in
an afternoon. Nothing about that makes it *evaluable*. Without a harness the only
way to learn whether an arm is any good is to put it in the live weekly run and
wait a quarter, and by then four more arms exist. So this module replays stored
periods through the same registry the live run uses, and scores what came out.

The whole product here is as-of honesty, not throughput. A harness that lets a
strategy see one extra day of data is strictly worse than no harness at all: it
returns a confident number, the number is wrong, and nothing downstream can tell.
Every design choice below is subordinate to that.

**The two leaks this repository has already been bitten by.** Both are prevented
here by construction rather than by care:

  1. *A theme registered later influencing an earlier period.* The two discovered
     themes register 2026-08-08. The `themes` table stores one row per (as_of,
     theme_id), and those rows are written by whatever scoring ran last — a
     re-score of 2026-08-07 performed after 08-08 would deposit rows for themes
     that did not exist on 08-07. Reading stored rows back at face value would
     therefore hand an 08-07 replay a theme discovered a day later, and that theme
     would get credit for a call it never made. `_topics` re-filters every stored
     row through `lexicon.all_themes(as_of)` and reports how many it dropped.

  2. *Prices not clamped to closed sessions.* A daily bar for date D is not
     usable until D's session has closed, and the live pack is generated at 07:23
     HKT, at which point the last finished US session is the previous calendar
     day's. Marking a decision against the bar dated `as_of` hands the strategy
     the outcome of the session it is deciding before. `_clamp` reuses
     `futu_px.complete_through` — the live guard — with the historical generation
     instant, so the replay's price view is the live run's price view.

Two more leak vectors that had not yet bitten anything, closed for the same
reason:

  3. *Event actuals.* `events.date` is the reference period, not the publication
     date: the July CPI level is dated 2026-07-01 and was published in mid-August.
     A calendar row is admitted only when the row's own `as_of` is on or before
     the replayed date, and `actual` is stripped from anything dated on or after
     it. On the current corpus that yields zero calendar rows for every replayable
     period, which is the correct answer and not a bug.

  4. *The candidate pool.* Candidates come from `ideas` rows whose own `as_of`
     equals the replayed date. A pool assembled from any later batch would be
     hindsight wearing a stage-B costume.

**One door in.** `context_for` is the only function that reads inputs, and every
`RunContext` it builds is stamped so `run_period` can refuse one that came from
anywhere else. Strategies get a frozen context with no database handle, exactly as
in the live run, so an arm cannot query its way into next week.

**The live call path is the call path.** `run_period` goes through
`strategy.run` / `strategy.run_all`. A harness with its own execution logic tests
the harness. The only thing here the live run does not do is read prices *after*
`as_of` — that happens in `outcome_for`, on the output side, and its results are
never put back into a context.

**Model calls are opt-in and counted.** A sweep over a year must not cost
inference, so `allow_model=False` withholds the port entirely rather than trusting
arms to behave. Arms that need a model then fail cleanly, are reported as skipped,
and the sweep asserts the total call count came back zero — withholding is
enforcement, a flag would be a promise.

**Refusing to conclude is a feature.** Separating a 2pp monthly edge at α=0.05
and 80% power needs roughly 7 independent monthly samples; paired on identical
candidates at ρ≈0.8 the requirement falls by (1−ρ), to ~1.4. Overlapping holding
periods are then discounted: ten daily periods carrying a 30-day horizon are not
ten independent observations. When the sample cannot support a comparison this
module says so and prints no winner. A function that names a winner from three
weeks of data is a liability, because someone will act on it.
"""

from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from . import config, db, ideas as ideas_mod, lexicon, strategy as strat
from .sources import futu_px

# ---------------------------------------------------------------------------
# As-of clamping

#: The instant a live run for a period would have executed. Every quote a replay
#: sees is clamped to sessions closed by this wall-clock moment on `as_of`. It
#: matches `replay.run`'s `gen_at`, deliberately: two harnesses disagreeing about
#: when the day started would produce two different "correct" answers.
GENERATION_TIME = "07:23:00+08:00"

#: Markets the price layer can mark. Anything else is unmarkable by construction
#: and must surface as UNKNOWN rather than as a return.
MARKETS = tuple(config.PRICEABLE_MARKETS)


class AsOfLeak(RuntimeError):
    """Raised when a reconstructed context can see past its own date."""


def clamp_dates(as_of: date, *, at: str = GENERATION_TIME) -> dict[str, str]:
    """Last calendar date per market whose session had closed by `as_of` 07:23 HKT.

    Delegated to the live session guard instead of reimplemented. The guard's
    subtlety is worth restating: for a Monday `as_of` the US answer is the
    preceding Sunday, which is not a session at all — the caller must then take
    the last close *on or before* that date, which is how Friday's bar ends up
    being the newest thing a Monday decision may use.
    """
    now = datetime.fromisoformat(f"{as_of.isoformat()}T{at}")
    return {m: futu_px.complete_through(m, now=now) for m in MARKETS}


# ---------------------------------------------------------------------------
# Input reconstruction. Only `context_for` may call these.

def _corpus(con, as_of: date, window_days: int) -> list[dict[str, Any]]:
    """Documents published inside the trailing window ending on `as_of`.

    Selection is on `published_d`, never on `ingested_at`. A body our fetcher
    retrieved two days late was published with its document and was public on the
    day; reading it is reading deeper into what already existed, not reading the
    future. The count of such documents is reported in the audit so the assumption
    stays visible rather than becoming folklore — see `replay`'s first note.
    """
    days = [(as_of - timedelta(days=i)).isoformat() for i in range(window_days)]
    rows = db.q(con,
                "SELECT doc_id, published_d, title, tier, line, institution, "
                "       summary, body, content_hash, retrieval, ingested_at "
                "FROM documents WHERE published_d IN (%s) AND published_d<=? "
                "ORDER BY published_d DESC, tier" % ",".join("?" * len(days)),
                [*days, as_of.isoformat()])
    return [{
        "doc_id": r["doc_id"], "published_d": r["published_d"],
        "title": r["title"] or "", "tier": int(r["tier"] or 3),
        "line": r["line"], "institution": r["institution"],
        "summary": r["summary"], "body": r["body"],
        "content_hash": r["content_hash"], "retrieval": r["retrieval"],
        "ingested_at": r["ingested_at"],
    } for r in rows]


def _universe(con) -> list[dict[str, Any]]:
    """Everything the system could trade, in the feed's universe shape.

    `priceable` is carried through because an instrument that cannot be marked
    must never reach a book: marking it at cost inserts a free 0% return, which
    flatters whichever arm picked it.
    """
    out: list[dict[str, Any]] = []
    for r in db.q(con, "SELECT key, name, kind, futu_code, olive_key, currency, "
                       "       COALESCE(priceable,0) AS priceable, meta "
                       "FROM instruments ORDER BY kind, key"):
        meta = db.jl(r["meta"], {}) or {}
        out.append({
            "instrument_id": r["key"], "name": r["name"] or r["key"],
            "kind": r["kind"] or "listed", "priceable": bool(r["priceable"]),
            "currency": r["currency"], "exposure": meta.get("exposure"),
            "vehicle": meta.get("vehicle"),
            "futu_code": r["futu_code"], "olive_key": r["olive_key"],
        })
    return out


def _topics(con, as_of: date, top_n: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Stored 筛选A output for the period, re-clamped to the theme registry.

    This is leak #1's fix and the reason it is a filter rather than a comment. The
    `themes` table is keyed on (as_of, theme_id) and rewritten by whichever
    scoring ran last, so its rows carry no memory of which themes existed on the
    day. `lexicon.all_themes(as_of)` does. Returns (topics, dropped ids).
    """
    legal = {t.id for t in lexicon.all_themes(as_of)}
    rows = db.q(con, "SELECT theme_id, label, key_question, tis, tier, n_items, "
                     "       n_sources, confidence, evidence FROM themes "
                     "WHERE as_of=? ORDER BY tis DESC", (as_of.isoformat(),))
    topics, dropped = [], []
    for r in rows:
        if r["theme_id"] not in legal:
            dropped.append(r["theme_id"])
            continue
        ev = db.jl(r["evidence"], []) or []
        topics.append({
            "topic_id": r["theme_id"], "label": r["label"],
            "key_question": r["key_question"], "score": r["tis"],
            "tier": r["tier"], "n_items": r["n_items"],
            "n_sources": r["n_sources"], "confidence": r["confidence"],
            "evidence": [str(e.get("doc_id") or e) for e in ev][:12],
        })
    return topics[:top_n], dropped


def _methods_of(raw: dict[str, Any]) -> list[str]:
    """The generators recorded against a booked idea, if any.

    `payload_from_candidates` writes them into sources[0].methods; a pool
    candidate reached by two generators keeps method="merged" and lives on in
    that list alone.
    """
    for src in (raw.get("sources") or []):
        if isinstance(src, dict) and src.get("methods"):
            return [str(m) for m in src["methods"] if m]
    return []


def _candidates(con, as_of: date) -> list[dict[str, Any]]:
    """Stage-B output for the period, in the shape stage C reads.

    Built from `ideas` rows whose own `as_of` is the replayed date — the ideas
    that stage B actually wrote that morning. Scenario legs are unpacked from the
    stored central case: `central_p` is percentages summing to 100 and `central_r`
    is (up, base, down) in percent, which is exactly the (upside_pct,
    downside_pct, p_up/p_base/p_down) contract in `strategy.IDEA_FIELDS`.

    `exposure`, `vehicle` and `method` are joined in because two live selectors
    cap on them. Leaving them None would not make those arms fail; it would make
    them silently collapse every candidate into one bucket, which is a far worse
    outcome than an error — the arm would still return ten ideas and the
    comparison would be measuring a bug.
    """
    inst = {u["instrument_id"]: u for u in _universe(con)}
    gen = {r["batch_id"]: r["generator"] for r in
           db.q(con, "SELECT batch_id, generator FROM batches")}
    out: list[dict[str, Any]] = []
    for r in db.q(con, "SELECT * FROM ideas WHERE as_of=? ORDER BY local_id",
                  (as_of.isoformat(),)):
        p = db.jl(r["central_p"], []) or []
        ret = db.jl(r["central_r"], []) or []
        if len(p) != 3 or len(ret) != 3:
            continue
        raw = db.jl(r["raw"], {}) or {}
        key = str(raw.get("instrument_key") or r["tool"] or "")
        u = inst.get(key, {})
        tot = sum(float(x) for x in p)
        out.append({
            "id": r["idea_uid"], "as_of": r["as_of"],
            "instrument_id": key or r["tool"],
            "instrument_name": u.get("name") or r["tool_desc"],
            "kind": r["instrument"], "futu_code": r["futu_code"],
            "olive_key": r["olive_key"], "currency": u.get("currency") or "USD",
            "exposure": u.get("exposure"), "vehicle": u.get("vehicle") or r["vehicle"],
            "topic_id": r["theme_id"], "topic_label": r["theme"],
            "method": gen.get(r["batch_id"]) or "unknown",
            # Which generators argued for this instrument. Booking preserves it
            # under sources[].methods; without it the two generation-method
            # arms see only the batch's generator label and select nothing at
            # all, so a whole arm reads as "chose zero ideas" when it in fact
            # argued for most of the pool.
            "proposed_by": _methods_of(raw) or [gen.get(r["batch_id"])],
            "horizon_days": 30 * int(r["horizon_months"] or 1),
            "thesis": r["thesis"] or r["view"] or "",
            "upside_pct": float(ret[0]), "downside_pct": float(ret[2]),
            "p_up": round(float(p[0]) / tot, 4) if tot else None,
            "p_base": round(float(p[1]) / tot, 4) if tot else None,
            "p_down": round(float(p[2]) / tot, 4) if tot else None,
            "p_sum_raw": round(tot / 100.0, 4),
            "grade": r["grade"], "or_c": r["or_c"], "or_k": r["or_k"],
            "ev_c": r["ev_c"], "sigma_h": r["sigma_h"],
            "ref_price": r["ref_price"], "ref_price_d": r["ref_price_d"],
        })
    return out


def _prices(con, as_of: date, codes: Iterable[str],
            clamp: dict[str, str]) -> dict[str, Any]:
    """Per-code price view as of the clamp, plus the statistics stage A reads.

    Every query underneath is bounded by the clamped date, so `priced_in` — where
    the trailing one-month return sits in its own one-year distribution — cannot
    be computed from a bar the decision had not seen. That number is the one place
    a price series feeds a *scoring* decision rather than a mark, which makes it
    the most valuable thing in this dict and the most damaging to get wrong.
    """
    out: dict[str, Any] = {}
    for code in sorted({c for c in codes if c}):
        upto = clamp.get(futu_px.market_of(code), as_of.isoformat())
        last = futu_px.last_close_on_or_before(con, code, upto)
        if not last:
            continue
        d, close = last
        pctl = futu_px.return_percentile(con, code, upto, window=21)
        out[code] = {
            "d": d, "close": close, "clamped_to": upto,
            # Two different one-month readings, and they disagree. `priced_in`
            # normalises the move against the instrument's own year, which is
            # what stage A wants — "how much of this is already in the price".
            # `ret_21s` is the raw move, which is what a cross-sectional ranking
            # wants: over 105 weekly periods the percentile picks the calmest
            # names at the top of their own range and loses (30% of periods),
            # the raw return picks the strongest and wins (66%). Carrying only
            # one of them is what let a "momentum" arm be written against the
            # wrong one.
            "priced_in": pctl if pctl is not None else 50.0,
            "ret_21s": futu_px.trailing_return(con, code, upto, 21),
            "priced_in_source": "return_percentile_21s" if pctl is not None
                                else "neutral_default",
            "vol_pctl": futu_px.vol_percentile(con, code, upto),
            "sigma_1m": futu_px.horizon_sigma(con, code, upto, months=1),
        }
    return out


def _calendar(con, as_of: date) -> tuple[list[dict[str, Any]], int]:
    """Dated events knowable on the replayed date, with future actuals stripped.

    Leak #3. `events.as_of` records when the row was fetched, so a row fetched
    later cannot be admitted however early its `date` is. Then `actual` is removed
    from anything dated on or after `as_of`, because a level dated inside the
    decision day was not published by 07:23 that morning. Returns (rows, stripped).
    """
    iso = as_of.isoformat()
    rows = db.q(con, "SELECT * FROM events WHERE COALESCE(as_of,date)<=? "
                     "ORDER BY date", (iso,))
    out, stripped = [], 0
    for r in rows:
        e = dict(r)
        if str(e.get("date") or "") >= iso and e.get("actual") is not None:
            e["actual"] = None
            stripped += 1
        out.append(e)
    return out, stripped


# ---------------------------------------------------------------------------
@dataclass
class Audit:
    """Everything needed to disbelieve a reconstructed context.

    Reported next to every result rather than checked once and forgotten: a
    guarantee nobody can see is a guarantee nobody trusts, and this is the object
    a reviewer reads instead of taking the module's word for it.
    """
    as_of: str
    inputs_sha: str
    clamp: dict[str, str] = field(default_factory=dict)
    corpus_n: int = 0
    corpus_max_published_d: str | None = None
    corpus_from: str | None = None
    corpus_retrieved_later: int = 0
    price_codes: int = 0
    price_max_d: str | None = None
    topics_n: int = 0
    topics_dropped: list[str] = field(default_factory=list)
    candidates_n: int = 0
    candidates_max_as_of: str | None = None
    calendar_n: int = 0
    calendar_actuals_stripped: int = 0
    model_port: bool = False
    leaks: list[str] = field(default_factory=list)

    def check(self) -> list[str]:
        """Anything here means the context can see past its own date."""
        bad: list[str] = []
        if self.corpus_max_published_d and self.corpus_max_published_d > self.as_of:
            bad.append(f"研报里有 {self.corpus_max_published_d} 的文档，晚于 {self.as_of}")
        if self.price_max_d and self.price_max_d > max(self.clamp.values() or [""]):
            bad.append(f"行情含 {self.price_max_d} 的收盘，晚于收盘钳制 {self.clamp}")
        if self.candidates_max_as_of and self.candidates_max_as_of > self.as_of:
            bad.append(f"候选含 {self.candidates_max_as_of} 的想法，晚于 {self.as_of}")
        if self.topics_dropped:
            # Not a leak — a leak *caught*. Recorded so the count is visible.
            pass
        self.leaks = bad
        return bad


#: Stamped into `params` so a strategy run can prove where its context came from.
CTX_TAG = "_backtest_audit"


def context_for(con, as_of: date, *,
                window_days: int | None = None,
                top_n: int = 5,
                candidates: list[dict[str, Any]] | None = None,
                topics: list[dict[str, Any]] | None = None,
                params: dict[str, Any] | None = None,
                allow_model: bool = False,
                infer: Any = None,
                strict: bool = True) -> strat.RunContext:
    """Rebuild the context a live run would have had on `as_of`. The only door in.

    `candidates` and `topics` are accepted so one stage's output can feed the
    next inside the same period, which is the one legitimate override — anything
    else a caller could inject would be data whose date this function never
    checked, and that is precisely the hole the module exists to close.

    `strict` refuses to hand back a context that failed its own audit. Default on:
    a leaking context that merely warns will be used anyway, because the warning
    scrolls past and the number looks fine.
    """
    window = int(window_days or config.OBSERVATION_WINDOW_DAYS)
    clamp = clamp_dates(as_of)

    corpus = _corpus(con, as_of, window)
    universe = _universe(con)
    topic_rows, dropped = ((topics, []) if topics is not None
                           else _topics(con, as_of, top_n))
    cands = _candidates(con, as_of) if candidates is None else candidates
    cal, stripped = _calendar(con, as_of)

    # Price codes: the pre-registered theme indicators stage A reads, plus every
    # instrument a candidate names. Nothing else — a wider pull would cost time
    # and widen the surface on which the clamp has to hold.
    codes = {t.price_indicator for t in lexicon.all_themes(as_of)}
    codes |= {c.get("futu_code") for c in cands}
    prices = _prices(con, as_of, codes, clamp)

    # Identical recipe and argument order to `orchestrator.weekly`, so a replay of
    # a live period reproduces its `inputs_sha` byte for byte. Two verdicts that
    # agree on this hash were looking at the same thing; that is the whole basis
    # on which arms may be differenced.
    inputs_sha = strat.RunContext.sha(
        [c.get("doc_id") for c in corpus],
        [c.get("id") for c in cands],
        sorted(prices.keys()),
        [e.get("event_id") for e in cal])

    audit = Audit(
        as_of=as_of.isoformat(), inputs_sha=inputs_sha, clamp=clamp,
        corpus_n=len(corpus),
        corpus_max_published_d=max((c["published_d"] for c in corpus), default=None),
        corpus_from=min((c["published_d"] for c in corpus), default=None),
        # Published on or before `as_of` but fetched afterwards. Not a leak — see
        # `_corpus` — but the honest limit of this harness, and large on the
        # current corpus because it was bulk-ingested from 2026-08-07. So a replay
        # reasons over what was *public* on the day, which is not identical to what
        # our store held that morning. Reported every time rather than argued once.
        corpus_retrieved_later=sum(
            1 for c in corpus
            if str(c.get("ingested_at") or "")[:10] > as_of.isoformat()),
        price_codes=len(prices),
        price_max_d=max((v["d"] for v in prices.values()), default=None),
        topics_n=len(topic_rows), topics_dropped=dropped,
        candidates_n=len(cands),
        candidates_max_as_of=max((str(c.get("as_of") or "") for c in cands),
                                 default=None) or None,
        calendar_n=len(cal), calendar_actuals_stripped=stripped,
        model_port=bool(allow_model and infer is not None))
    leaks = audit.check()
    if leaks and strict:
        raise AsOfLeak(f"{as_of} 的回放上下文越界：" + "；".join(leaks))

    return strat.RunContext(
        as_of=as_of, inputs_sha=inputs_sha, corpus=corpus, topics=topic_rows,
        universe=universe, candidates=cands, prices=prices, calendar=cal,
        params={**(params or {}), CTX_TAG: audit},
        infer=(infer if allow_model else None))


def _require_backtest_context(ctx: strat.RunContext) -> Audit:
    a = (ctx.params or {}).get(CTX_TAG)
    if not isinstance(a, Audit):
        raise AsOfLeak("上下文不是由 backtest.context_for 构建的，拒绝运行："
                       "回测只能使用经过 as-of 审计的输入")
    return a


# ---------------------------------------------------------------------------
# Outcomes. This is the only place forward prices are read, and nothing computed
# here ever re-enters a RunContext.

#: Why an outcome could not be computed. Each is reported and excluded — never
#: imputed. Zero-filling an unmarkable position is not conservative: it inserts a
#: costless, riskless 0% return, which systematically flatters whichever arm
#: preferred illiquid vehicles, and that is exactly the arm a fund shelf tempts a
#: selector into becoming.
UNKNOWN_REASONS = {
    "NO_INSTRUMENT": "没有可定价的标的标识",
    "NAV_ONLY": "基金按 NAV 披露，缺少可用的前后两个净值点",
    "NO_PRICE_SERIES": "该代码在本地行情库中没有任何 K 线",
    "NO_ENTRY_BAR": "该期之后没有可成交的收盘价",
    "SHORT_WINDOW": "持有期内没有足够的交易日",
    "HORIZON_INCOMPLETE": "行情尚未覆盖到期日，1 个月窗口没走完",
}


@dataclass
class Outcome:
    id: str
    code: str | None = None
    ret: float | None = None
    status: str = "OK"
    entry_d: str | None = None
    entry_px: float | None = None
    exit_d: str | None = None
    exit_px: float | None = None
    sessions: int = 0
    horizon_end: str | None = None
    window_complete: bool = False
    cost_pct: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "OK" and self.ret is not None


def outcome_for(con, cand: dict[str, Any], as_of: date, *,
                horizon_days: int = 30,
                require_full_horizon: bool = True,
                min_sessions: int = 1) -> Outcome:
    """Realised holding-period return for one candidate, net of round-trip costs.

    Entry is the first close on or after `as_of`, not the close the decision was
    made from. Buying at the clamped decision price would credit the arm with the
    weekend gap it could not have traded — free alpha, largest exactly when the
    market moved most, which is the shape of an error that looks like skill.

    `require_full_horizon` decides what an unfinished month is. Defaulting to True
    makes it UNKNOWN, because a 9-session return reported as a one-month return is
    a different statistic wearing the same name. Setting it False is legitimate
    for a *paired* comparison, where both arms are truncated identically and the
    truncation cancels — but the label then has to say so, which is why the
    aggregate carries `window_complete_frac`.
    """
    code = cand.get("futu_code")
    kind = str(cand.get("kind") or "listed")
    hz_end = (as_of + timedelta(days=horizon_days)).isoformat()
    o = Outcome(id=str(cand.get("id")), code=code, horizon_end=hz_end)

    if not code:
        if not cand.get("olive_key"):
            o.status = "NO_INSTRUMENT"
            return o
        # A fund's NAV series is the only mark it has, and it is published on the
        # fund's cadence rather than the market's. Two points spanning the window
        # are computed when they exist; when they do not the answer is UNKNOWN, not
        # the subscription price or the last known NAV, either of which would be
        # the zero-fill wearing a different name.
        o.code = str(cand["olive_key"])
        a = db.q1(con, "SELECT d, nav FROM navs WHERE olive_key=? AND d>=? "
                       "ORDER BY d LIMIT 1", (o.code, as_of.isoformat()))
        b = db.q1(con, "SELECT d, nav FROM navs WHERE olive_key=? AND d<=? "
                       "ORDER BY d DESC LIMIT 1", (o.code, hz_end))
        if not a or not b or not a["nav"] or b["d"] <= a["d"]:
            o.status = "NAV_ONLY"
            return o
        o.entry_d, o.entry_px = a["d"], float(a["nav"])
        o.exit_d, o.exit_px = b["d"], float(b["nav"])
        o.window_complete = b["d"] >= hz_end
        if require_full_horizon and not o.window_complete:
            o.status = "HORIZON_INCOMPLETE"
            return o
        o.sessions = 1
        o.cost_pct = ideas_mod.round_trip_cost_pct("FUND", kind) / 100.0
        o.ret = o.exit_px / o.entry_px - 1.0 - o.cost_pct
        return o

    span = db.q1(con, "SELECT MIN(d) a, MAX(d) b FROM prices WHERE code=?", (code,))
    if not span or not span["b"]:
        o.status = "NO_PRICE_SERIES"
        return o

    entry = db.q1(con, "SELECT d, close FROM prices WHERE code=? AND d>=? "
                       "ORDER BY d LIMIT 1", (code, as_of.isoformat()))
    if not entry or not entry["close"]:
        o.status = "NO_ENTRY_BAR"
        return o
    o.entry_d, o.entry_px = entry["d"], float(entry["close"])

    o.window_complete = span["b"] >= hz_end
    if require_full_horizon and not o.window_complete:
        o.status = "HORIZON_INCOMPLETE"
        return o

    ex = futu_px.last_close_on_or_before(con, code, hz_end)
    if not ex:
        o.status = "NO_ENTRY_BAR"
        return o
    o.exit_d, o.exit_px = ex[0], ex[1]
    o.sessions = len(db.q(con, "SELECT d FROM prices WHERE code=? AND d>? AND d<=?",
                          (code, o.entry_d, o.exit_d)))
    if o.sessions < min_sessions or not o.entry_px:
        o.status = "SHORT_WINDOW"
        return o

    # Same cost model as the live book. A gross return here and a net return in
    # `analytics` would make the two records disagree by ~8bp per idea, which is
    # a quarter of the edge being measured.
    o.cost_pct = ideas_mod.round_trip_cost_pct(futu_px.market_of(code), kind) / 100.0
    o.ret = o.exit_px / o.entry_px - 1.0 - o.cost_pct
    return o


def outcomes_for(con, cands: Sequence[dict[str, Any]], as_of: date,
                 **kw: Any) -> dict[str, Outcome]:
    return {str(c.get("id")): outcome_for(con, c, as_of, **kw) for c in cands}


# ---------------------------------------------------------------------------
# One period through the live call path.

@dataclass
class PeriodResult:
    as_of: str
    inputs_sha: str
    audit: Audit
    verdicts: dict[str, dict[str, strat.Verdict]] = field(default_factory=dict)
    calls: int = 0
    stage_sha: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def run_period(con, as_of: date, *,
               stages: Sequence[strat.Kind] = ("idea_selector",),
               arms: dict[str, Iterable[str]] | None = None,
               allow_model: bool = False,
               infer: Any = None,
               params: dict[str, Any] | None = None,
               top_n: int = 5,
               strict: bool = True) -> PeriodResult:
    """Run the chosen stages for one historical period.

    Dispatch is `strategy.run` / `strategy.run_all`, unchanged — including the
    generator output check and the selector's "chose an id outside the candidate
    set" guard. Reimplementing any of that here would mean the harness and the
    live run could disagree about what a valid verdict is, and the backtest would
    be measuring itself.

    Stage output advances via `ctx.with_()`, so `inputs_sha` moves with it: stage
    C's arms are paired on stage C's inputs, which is the hash recorded against
    their verdicts.
    """
    ctx = context_for(con, as_of, top_n=top_n, params=params,
                      allow_model=allow_model, infer=infer, strict=strict)
    audit = _require_backtest_context(ctx)
    res = PeriodResult(as_of=as_of.isoformat(), inputs_sha=ctx.inputs_sha,
                       audit=audit)
    want = [k for k in strat.STAGES if k in set(stages)]
    pick = {k: list(v) for k, v in (arms or {}).items()}

    for kind in want:
        names = pick.get(kind)
        vs = strat.run_all(kind, ctx, names=names)
        res.verdicts[kind] = vs
        res.stage_sha[kind] = ctx.inputs_sha
        res.calls += sum(v.calls for v in vs.values())
        for n, v in vs.items():
            if v.meta.get("error"):
                res.errors[f"{kind}/{n}"] = str(v.meta["error"])

        if kind == "topic_scorer":
            # Advance on the first arm that actually produced a ranking. Feeding
            # forward an empty verdict would silently turn a stage-A failure into
            # "no theme was interesting this week", the opposite conclusion.
            picked = next((v for v in vs.values() if v.chosen), None)
            if picked:
                byid = {str(t["topic_id"]): t for t in ctx.topics}
                ctx = ctx.with_(topics=[byid[t] for t in picked.chosen if t in byid])
        elif kind == "idea_generator":
            pool: dict[str, dict[str, Any]] = {}
            for v in vs.values():
                for i in v.produced:
                    pool.setdefault(str(i.get("id")), i)
            if pool:
                # Every generator's ideas pooled, as the live run does, then a new
                # hash: stage C is being compared on this pool, not on stage B's.
                cands = list(pool.values())
                ctx = ctx.with_(candidates=cands, inputs_sha=strat.RunContext.sha(
                    [c.get("doc_id") for c in ctx.corpus],
                    [c.get("id") for c in cands],
                    sorted(ctx.prices.keys()),
                    [e.get("event_id") for e in ctx.calendar]))
    res.stage_sha["final"] = ctx.inputs_sha
    if not allow_model and res.calls:
        raise AsOfLeak(f"{as_of}: 声明为零模型调用，但记录到 {res.calls} 次推理调用"
                       "——某个策略绕过了被撤走的 inference 端口")
    return res


# ---------------------------------------------------------------------------
# Scoring and the power gate.

#: Two-sided α=0.05 and 80% power.
Z_ALPHA, Z_POWER = 1.959964, 0.841621

#: The edge worth detecting: 2pp per month. Below this the methodology's own
#: return target does not depend on which arm won.
TARGET_EDGE = 0.02

#: Reference monthly dispersion of a held basket's mean return, calibrated so the
#: unpaired requirement at TARGET_EDGE comes out at the ~7 months quoted in the
#: design note. Used only until enough periods exist to measure sd(d) directly.
REF_SIGMA = 0.013355
REF_RHO = 0.80


def required_periods(sd_d: float, edge: float = TARGET_EDGE) -> int:
    """Paired periods needed to separate `edge` at α=0.05 / 80% power."""
    if sd_d <= 0:
        return 1
    return max(1, math.ceil((Z_ALPHA + Z_POWER) ** 2 * sd_d ** 2 / edge ** 2))


def required_unpaired(sigma: float = REF_SIGMA, edge: float = TARGET_EDGE) -> int:
    """The reference unpaired requirement: two independent groups of n each.

    n = 2(z_α+z_β)²σ²/δ². At σ=1.34% monthly and δ=2pp that is 7 — the anchor the
    design note quotes, and the reason REF_SIGMA is set where it is.
    """
    return max(1, math.ceil(2 * (Z_ALPHA + Z_POWER) ** 2 * sigma ** 2 / edge ** 2))


def unpaired_equivalent(required_paired: int, rho: float = REF_RHO) -> int:
    """What the *same* comparison would cost without pairing.

    Derived from the paired requirement rather than from REF_SIGMA, which is the
    only way the ratio means anything: sd(d) = σ√(2(1−ρ)), so the paired
    requirement is the unpaired one times (1−ρ) and the saving is exactly 1/(1−ρ)
    — 5× at ρ=0.80. Computing the two ends from different σ would produce a ratio
    that drifts per arm and quietly stops being a statement about pairing at all.
    """
    return max(1, math.ceil(required_paired / max(1e-6, 1.0 - rho)))


@dataclass
class ArmScore:
    name: str
    version: str = "?"
    role: str = "?"
    n_chosen: int = 0
    n_scored: int = 0
    unknown: dict[str, int] = field(default_factory=dict)
    hit_rate: float | None = None
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    per_period: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0
    error: str | None = None
    window_complete_frac: float | None = None
    bias_if_zero_filled: float | None = None

    @property
    def coverage(self) -> float | None:
        return None if not self.n_chosen else round(self.n_scored / self.n_chosen, 3)


def _score_arm(name: str, spec: dict[str, Any],
               periods: list[tuple[str, strat.Verdict, dict[str, Outcome]]]) -> ArmScore:
    """Aggregate one arm across periods. Unknowns counted, never filled.

    `bias_if_zero_filled` is reported next to `mean` on purpose: it is what the
    arm's number would become if unmarkable picks were treated as flat, and the
    gap is the size of the lie being avoided. Printing it is cheaper than
    explaining the principle a second time.
    """
    a = ArmScore(name=name, version=str(spec.get("version") or "?"),
                 role=str(spec.get("role") or "?"))
    rets: list[float] = []
    complete = 0
    for as_of, v, outs in periods:
        a.calls += v.calls
        if v.meta.get("error") and not a.error:
            a.error = str(v.meta["error"])
        chosen = [str(i) for i in v.chosen]
        a.n_chosen += len(chosen)
        pr: list[float] = []
        for i in chosen:
            o = outs.get(i)
            if o is None:
                a.unknown["NO_INSTRUMENT"] = a.unknown.get("NO_INSTRUMENT", 0) + 1
                continue
            if o.ok:
                rets.append(o.ret)          # type: ignore[arg-type]
                pr.append(o.ret)            # type: ignore[arg-type]
                complete += 1 if o.window_complete else 0
            else:
                a.unknown[o.status] = a.unknown.get(o.status, 0) + 1
        a.per_period[as_of] = {
            "n_chosen": len(chosen), "n_scored": len(pr),
            "mean": (round(st.mean(pr), 6) if pr else None),
        }
    a.n_scored = len(rets)
    if rets:
        a.mean = round(st.mean(rets), 6)
        a.median = round(st.median(rets), 6)
        a.hit_rate = round(sum(1 for x in rets if x > 0) / len(rets), 3)
        a.stdev = round(st.stdev(rets), 6) if len(rets) > 1 else None
        a.window_complete_frac = round(complete / len(rets), 3)
        n_unknown = sum(a.unknown.values())
        if n_unknown:
            filled = (sum(rets) + 0.0) / (len(rets) + n_unknown)
            a.bias_if_zero_filled = round(filled - a.mean, 6)
    return a


@dataclass
class Paired:
    arm: str
    control: str
    n_pairs: int = 0
    n_eff: float = 0.0
    mean_diff: float | None = None
    sd_diff: float | None = None
    t_stat: float | None = None
    t_eff: float | None = None
    required_pairs: int | None = None
    required_unpaired: int | None = None
    #: What `n_eff` takes for granted, stated where the number is read. Without
    #: it the discount looks like a measurement; it is a model of the overlap,
    #: and its assumption is violated by any period the price series cut short.
    n_eff_assumes: str | None = None
    sd_source: str = "reference"
    #: Enough effective samples to detect the pre-registered edge. Powered is
    #: not the same as won: an arm that mirrors the control has a tiny sd, so
    #: one period "powers" it — while the difference it actually shows is
    #: nothing. Both conditions have to hold before anyone says a method beat
    #: the control, which is why `significant` exists separately.
    powered: bool = False
    #: The observed difference clears ±1.96 on effective (overlap-discounted)
    #: samples.
    significant: bool = False
    conclusive: bool = False
    message: str = ""
    pairs: list[dict[str, Any]] = field(default_factory=list)
    sha_mismatch: list[str] = field(default_factory=list)


def paired_difference(arm: ArmScore, control: ArmScore, *,
                      sha: dict[str, str] | None = None,
                      gap_days: float = 1.0,
                      horizon_days: int = 30,
                      edge: float = TARGET_EDGE) -> Paired:
    """Difference the two arms period by period, then decide whether to conclude.

    Pairing is the point. Both arms saw the same candidate pool in the same
    period, so the common market move — which dwarfs any selection edge over a
    month — subtracts out of `mean_diff`. Comparing their unpaired levels instead
    would need roughly five times the history for the same confidence, because the
    market's own monthly variance would then sit in the denominator.

    Two independent reasons to refuse a verdict, and both fire on real data:

    * **Not enough pairs.** `required_pairs` comes from the observed sd of the
      differences, not from a stated prior, so it tightens as evidence arrives.
    * **Overlapping horizons.** Periods one day apart carrying a 30-day horizon
      share 29/30 of their holding window. Ten of those are nowhere near ten
      observations, so `n_eff` discounts the count by gap/horizon before it is
      compared with the requirement.
    """
    p = Paired(arm=arm.name, control=control.name)
    diffs: list[float] = []
    for as_of, a in sorted(arm.per_period.items()):
        c = control.per_period.get(as_of)
        if not c or a["mean"] is None or c["mean"] is None:
            continue
        d = a["mean"] - c["mean"]
        diffs.append(d)
        p.pairs.append({"as_of": as_of, "arm": a["mean"], "control": c["mean"],
                        "diff": round(d, 6), "n_arm": a["n_scored"],
                        "n_control": c["n_scored"]})
    p.n_pairs = len(diffs)
    # Two arms that disagree on inputs_sha were not looking at the same pool, so
    # the difference between them is not attributable to selection at all.
    p.sha_mismatch = sorted(sha.keys()) if sha and len(set(sha.values())) > 1 else []
    # The discount charges every period the *nominal* horizon. Periods whose
    # window was cut short by the last available close overlap their neighbours
    # by less than the formula assumes, so for those the figure understates the
    # independence rather than overstating it — while their returns are, at the
    # same time, short readings inside a table labelled with the full horizon.
    # The two errors run in opposite directions and netting them out would need
    # the realised overlap of every pair, which this does not compute. So the
    # assumption is named and left standing: `horizon_completeness` in the
    # summary says how many positions reached the mark.
    p.n_eff = round(p.n_pairs * min(1.0, gap_days / max(1, horizon_days)), 3)
    p.n_eff_assumes = (
        f"每期都持满 {horizon_days} 天。被最新收盘截断的期次实际重叠更少，"
        f"这一项对它们是低估而非高估；同时它们的收益是短读数。两个方向相反，"
        f"净效应未计算——跑满比例见 horizon_completeness")

    if diffs:
        p.mean_diff = round(st.mean(diffs), 6)
    p.sd_diff = round(st.stdev(diffs), 6) if len(diffs) > 2 else None
    sd = p.sd_diff or REF_SIGMA * math.sqrt(2 * (1 - REF_RHO))
    p.sd_source = "observed" if p.sd_diff else "reference"
    p.required_pairs = required_periods(sd, edge)
    p.required_unpaired = unpaired_equivalent(p.required_pairs)
    if p.sd_diff and p.n_pairs > 2 and p.sd_diff > 0:
        p.t_stat = round(p.mean_diff / (p.sd_diff / math.sqrt(p.n_pairs)), 3)
        # The same statistic charged the sample size the overlap actually leaves.
        # Reporting only `t_stat` is how a harness talks itself into a winner:
        # ten daily periods sharing 29/30 of their holding window produce a t
        # inflated by ~sqrt(n/n_eff), so an arm can print t=-7 on what is barely
        # one third of one independent observation.
        if p.n_eff > 0:
            p.t_eff = round(p.mean_diff / (p.sd_diff / math.sqrt(p.n_eff)), 3)

    why: list[str] = []
    if p.sha_mismatch:
        why.append(f"两组 inputs_sha 不一致（{', '.join(p.sha_mismatch)}），"
                   "看的不是同一个候选池，不可相减")
    if p.n_pairs < 2:
        why.append(f"配对周期只有 {p.n_pairs} 个")
    if p.n_eff < p.required_pairs:
        why.append(
            f"有效独立样本 n_eff={p.n_eff}（{p.n_pairs} 个周期，间隔 {gap_days:.0f} 天，"
            f"但持有期 {horizon_days} 天，窗口重叠按 间隔/持有期 折算）"
            f"，低于所需 {p.required_pairs} 个")
    p.powered = not why
    p.significant = (p.t_eff is not None and abs(p.t_eff) >= Z_ALPHA)
    # Both, or it is not a verdict. Power alone says "an edge this large would
    # have shown up"; on an arm that mirrors the control, sd collapses and a
    # single period clears the power test while the observed difference is
    # nothing at all — reporting that as 可以下结论 announces a winner the data
    # never produced. Seen live: generated_ai_native, required_pairs=1,
    # t_eff=0.625, rendered on the dashboard as "已达到显著性门槛".
    p.conclusive = p.powered and p.significant
    if p.conclusive:
        p.message = (f"可以下结论：配对差 {_pct(p.mean_diff)}/周期（sd {_pct(p.sd_diff)}，"
                     f"t={p.t_stat}，t_eff={p.t_eff}，n={p.n_pairs}，n_eff={p.n_eff}）")
        return p
    if p.powered and not p.significant:
        # A real answer, just not the one people hope for: the window carried
        # enough independent information to surface the declared edge, and no
        # such edge appeared. Saying "样本不足" here would be false.
        p.message = (
            f"样本已够检出 {_pct(edge)}/周期的差距，但没有检出："
            f"配对差 {_pct(p.mean_diff)}，t_eff={p.t_eff}（未过 ±{Z_ALPHA:.2f}）。"
            f"这是「没看出优势」，不是「还看不出来」。")
        return p

    # Two different failures, and they call for different sentences. Saying "it is
    # noise" when the raw t is 7 would be false and would train the reader to
    # ignore this line; the actual objection is that the observations are not
    # independent, which is a statement about n, not about the size of the effect.
    if p.t_eff is not None and abs(p.t_eff) >= Z_ALPHA:
        verdict_note = (f"注意：折算后 t_eff={p.t_eff} 本身已过 ±1.96，"
                        f"效应可能真实存在——但样本量不足以按预先声明的口径确认它，"
                        f"因此仍不出结论。这里缺的是独立周期数，不是效应大小。")
    elif p.t_eff is not None:
        verdict_note = (f"按有效独立样本折算后 t_eff={p.t_eff}（原始 t={p.t_stat}，"
                        f"被重叠窗口放大），未达 ±1.96，不构成证据。")
    else:
        verdict_note = "配对差本身还无法估计标准差，不构成任何证据。"

    p.message = (
        f"样本不足，拒绝给出胜负结论。" + "；".join(why) + "。"
        f"按 {edge*100:.0f}pp 月度优势、α=0.05 双侧、power=0.80，"
        f"sd 取{'实测' if p.sd_source == 'observed' else '参考值'}："
        f"配对需要约 {p.required_pairs} 个独立周期；同一置信度下不配对需要约 "
        f"{p.required_unpaired} 个——ρ≈{REF_RHO} 时正好 1/(1-ρ)="
        f"{1/(1-REF_RHO):.0f} 倍，这就是配对存在的理由。"
        f"（参照：以典型月度离散度 σ≈{REF_SIGMA*100:.2f}% 计，不配对约需 "
        f"{required_unpaired(edge=edge)} 个月；且重叠窗口会低估 sd，"
        f"上面的门槛是下限。）"
        f"当前配对差 {_pct(p.mean_diff)}（sd {_pct(p.sd_diff)}）。{verdict_note}")
    return p


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v*100:+.2f}%"


# ---------------------------------------------------------------------------
# The sweep.

#: An arm reporting this is asking for a model it was not given. Recognised so a
#: mechanical sweep reports it as skipped rather than as a strategy that failed.
_NEEDS_MODEL = ("inference", "需要模型", "模型推理")


@dataclass
class Sweep:
    stage: str
    dates: list[str] = field(default_factory=list)
    control: str = ""
    horizon_days: int = 30
    require_full_horizon: bool = True
    gap_days: float = 1.0
    arms: dict[str, ArmScore] = field(default_factory=dict)
    paired: dict[str, Paired] = field(default_factory=dict)
    skipped_need_model: dict[str, str] = field(default_factory=dict)
    audits: list[Audit] = field(default_factory=list)
    calls: int = 0
    unknown_total: dict[str, int] = field(default_factory=dict)
    n_candidates: dict[str, int] = field(default_factory=dict)


def periods(con) -> list[dict[str, Any]]:
    """Replayable periods, with what history each actually has.

    A sweep should never be handed a date list assembled by guessing. This is the
    ground truth: a period is replayable for stage C when candidates exist for it,
    and the corpus and price columns say whether stages A and B are replayable too.
    """
    out = []
    for r in db.q(con, "SELECT as_of, COUNT(*) n FROM ideas GROUP BY as_of "
                       "ORDER BY as_of"):
        as_of = date.fromisoformat(r["as_of"])
        clamp = clamp_dates(as_of)
        w = [(as_of - timedelta(days=i)).isoformat()
             for i in range(config.OBSERVATION_WINDOW_DAYS)]
        docs = db.q1(con, "SELECT COUNT(*) n FROM documents WHERE published_d IN "
                          "(%s)" % ",".join("?" * len(w)), w)
        th = db.q1(con, "SELECT COUNT(*) n FROM themes WHERE as_of=?", (r["as_of"],))
        spy = futu_px.last_close_on_or_before(con, "US.SPY", clamp["US"])
        out.append({"as_of": r["as_of"], "candidates": r["n"],
                    "corpus_window": docs["n"] if docs else 0,
                    "themes_stored": th["n"] if th else 0,
                    "price_clamp": clamp["US"],
                    "last_close_used": spy[0] if spy else None})
    return out


def _median_gap(dates: list[date]) -> float:
    if len(dates) < 2:
        return 1.0
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    return float(st.median(gaps)) if gaps else 1.0


def sweep(con, dates: Sequence[date | str], *,
          stage: strat.Kind = "idea_selector",
          arms: Iterable[str] | None = None,
          control: str = "buy_all",
          horizon_days: int = 30,
          require_full_horizon: bool = True,
          allow_model: bool = False,
          infer: Any = None,
          params: dict[str, Any] | None = None,
          top_n: int = 5,
          edge: float = TARGET_EDGE,
          strict: bool = True) -> Sweep:
    """Walk periods forward, scoring every arm, and compare them paired.

    Forward order is not cosmetic: it is the only order in which a future
    extension — carrying a position across periods, or letting one period's
    outcome tune the next — cannot accidentally read backwards.

    For stage C each period's arms run over one shared context, so the comparison
    is paired on identical candidates by construction rather than by convention.
    """
    ds = sorted(d if isinstance(d, date) else date.fromisoformat(d) for d in dates)
    rep = Sweep(stage=stage, dates=[d.isoformat() for d in ds], control=control,
                horizon_days=horizon_days,
                require_full_horizon=require_full_horizon,
                gap_days=_median_gap(ds))

    stages: tuple[strat.Kind, ...] = tuple(
        k for k in strat.STAGES
        if strat.STAGES.index(k) <= strat.STAGES.index(stage)
        and (k == stage or k != "idea_generator" or allow_model))
    if stage == "idea_selector":
        # Stage C over stored candidates needs neither A nor B: the pool for the
        # period is what stage B actually wrote that day. Re-deriving it would
        # replace the real historical pool with today's generators' opinion of it.
        stages = ("idea_selector",)

    per_arm: dict[str, list[tuple[str, strat.Verdict, dict[str, Outcome]]]] = {}
    specs: dict[str, dict[str, Any]] = {}
    shas: dict[str, dict[str, str]] = {}

    for d in ds:
        pr = run_period(con, d, stages=stages,
                        arms=({stage: list(arms)} if arms else None),
                        allow_model=allow_model, infer=infer, params=params,
                        top_n=top_n, strict=strict)
        rep.audits.append(pr.audit)
        rep.calls += pr.calls
        rep.n_candidates[d.isoformat()] = pr.audit.candidates_n

        cands = _candidates(con, d) if stage == "idea_selector" else []
        outs = outcomes_for(con, cands, d, horizon_days=horizon_days,
                            require_full_horizon=require_full_horizon)
        for o in outs.values():
            if not o.ok:
                rep.unknown_total[o.status] = rep.unknown_total.get(o.status, 0) + 1

        for name, v in pr.verdicts.get(stage, {}).items():
            err = str(v.meta.get("error") or "")
            if err and any(k in err for k in _NEEDS_MODEL):
                rep.skipped_need_model[name] = err
                continue
            specs.setdefault(name, strat.spec(stage, name))
            per_arm.setdefault(name, []).append((d.isoformat(), v, outs))
            shas.setdefault(name, {})[d.isoformat()] = pr.stage_sha.get(stage, "")

    for name, rows in per_arm.items():
        rep.arms[name] = _score_arm(name, specs.get(name, {}), rows)

    ctl = rep.arms.get(control)
    if ctl:
        for name, a in rep.arms.items():
            if name == control:
                continue
            # One sha per period across arms: they ran on one context, so a
            # mismatch here would mean the harness itself broke the pairing.
            mismatch = {p: s for p, s in shas.get(name, {}).items()
                        if s != shas.get(control, {}).get(p)}
            rep.paired[name] = paired_difference(
                a, ctl, sha=mismatch or None, gap_days=rep.gap_days,
                horizon_days=horizon_days, edge=edge)

    if not allow_model and rep.calls:
        raise AsOfLeak(f"机械回测声明零模型调用，实际 {rep.calls} 次")
    return rep


# ---------------------------------------------------------------------------
def realized_vol_pct(equities: Sequence[float], periods_per_year: int = 252
                     ) -> float | None:
    """Annualised volatility of a daily equity series, in percent."""
    if len(equities) < 3:
        return None
    rets = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities))
            if equities[i - 1]]
    if len(rets) < 2:
        return None
    return st.pstdev(rets) * (periods_per_year ** 0.5) * 100.0


def instrument_vol_gradient(con, positions: Sequence[dict[str, Any]],
                            score_of: Any, *, lookback: int = 60) -> dict:
    """Whether a score that ranks returns is really just ranking risk.

    The check that has to be run against any ranking that works, and the one
    it is most likely to fail. Sorting a pool by expected return over six weeks
    in which risk assets rose produces a clean ladder whether or not the score
    knows anything — because expected return is largely a restatement of how
    much the instrument moves, and in a rising window the movers win.

    So: the realised volatility of each bucket's holdings **before entry**,
    beside its return. If the two ladders have the same shape and return per
    unit of volatility is flat across buckets, the ranking is a risk sorter and
    should be reported as one. Measured before entry so the number is knowable
    on the day the pick was made.
    """
    inst = {r["key"]: r["futu_code"] for r in db.q(
        con, "SELECT key, futu_code FROM instruments WHERE futu_code IS NOT NULL")}

    def vol(code: str, upto: str) -> float | None:
        px = [r["close"] for r in db.q(
            con, "SELECT close FROM prices WHERE code=? AND d<=? "
                 "ORDER BY d DESC LIMIT ?", (code, upto, lookback + 1))][::-1]
        if len(px) < 21:
            return None
        return st.pstdev([px[i] / px[i - 1] - 1 for i in range(1, len(px))]
                         ) * (252 ** 0.5) * 100.0

    out: dict[str, list[tuple[float, float]]] = {}
    for r in positions:
        b = score_of(r)
        code = inst.get(str(r.get("instrument_id")))
        if b is None or not code or r.get("return_pct") is None:
            continue
        v = vol(code, str(r.get("period")))
        if v is None:
            continue
        out.setdefault(str(b), []).append((float(r["return_pct"]), v))

    buckets = {}
    for b, rows in sorted(out.items()):
        mr = sum(x[0] for x in rows) / len(rows)
        mv = sum(x[1] for x in rows) / len(rows)
        buckets[b] = {"n": len(rows), "mean_return_pct": round(mr, 4),
                      "mean_prior_vol_pct": round(mv, 4),
                      "return_per_vol": round(mr / mv, 4) if mv else None}
    ratios = [v["return_per_vol"] for v in buckets.values()
              if v["return_per_vol"] is not None]
    vols = [v["mean_prior_vol_pct"] for v in buckets.values()]
    spread = (max(ratios) - min(ratios)) if len(ratios) > 1 else None
    return {
        "lookback_days": lookback, "buckets": buckets,
        "vol_ratio_top_over_bottom": (round(vols[-1] / vols[0], 2)
                                      if len(vols) > 1 and vols[0] else None),
        "return_per_vol_spread": None if spread is None else round(spread, 4),
        "note": (
            "入场前 60 日年化波动，与同一分桶的收益并列。两条阶梯形状相同、"
            "而单位波动收益基本持平，就说明这个分数在排风险而不是在排能力——"
            "上涨窗口里把风险从低到高排一遍，必然得到一条漂亮的收益阶梯。"),
    }


def tranche_curve(con, positions: Sequence[dict[str, Any]], *,
                  horizon_days: int, gap_days: float,
                  cash_pct_annual: float = 0.0) -> list[dict[str, Any]]:
    """Daily NAV of a portfolio that opens one tranche per period.

    Replaces a curve that compounded each period's mean return once per period.
    That construction is wrong in two ways at once and they compound each other:

    * the returns are **horizon** returns compounded at the **period** cadence.
      Six weekly steps each multiplying by a 30-day return claims a month's
      result was banked every week — roughly four times the exposure any capital
      actually had.
    * the windows **overlap**. The 2026-07-29 tranche was still held through
      08-05, 08-12 and 08-19, so one market move was counted four times.

    What it produced was not small: ev_rank's six periods averaged +3.48% over
    30 days and the curve read +22.43% over six weeks, with the final step a
    **two-day** reading (09-02 → 09-04, the last close available) compounded as
    a completed period. A number built that way is exactly what a PM means by
    "fake", and it was the panel's headline curve.

    The portfolio here is the one the design actually describes: each period
    deploys one tranche of `1/slots` of capital into that period's picks, equally
    weighted, held for the horizon; `slots = round(horizon / gap)` — four, for a
    30-day hold opened weekly — so capital is committed once and released once.
    Slots with no live tranche sit in cash, which is a real portfolio state and
    the reason a curve like this trails a fully-invested index in a rising tape.

    Marking is daily against the close, so a tranche that has not reached its
    horizon contributes what it is worth today rather than a completed trade.
    Instruments with no daily series (funds, structured products) are dropped
    from the mark and reported in `no_series`, never marked flat at 0%.
    """
    slots = max(1, round(horizon_days / max(gap_days, 1e-9)))
    codes = {str(r["instrument_id"]) for r in positions}
    inst = {r["key"]: r["futu_code"] for r in db.q(
        con, "SELECT key, futu_code FROM instruments WHERE futu_code IS NOT NULL")}
    priced = {c: inst[c] for c in codes if c in inst}
    no_series = sorted(codes - set(priced))

    entries = [r["entry_d"] for r in positions if r.get("entry_d")]
    exits = [r["exit_d"] for r in positions if r.get("exit_d")]
    if not entries or not exits:
        return []
    lo, hi = min(entries), max(exits)
    cal = [r["d"] for r in db.q(
        con, "SELECT DISTINCT d FROM prices WHERE d>=? AND d<=? ORDER BY d", (lo, hi))]
    if len(cal) < 2:
        return []

    series: dict[str, dict[str, float]] = {}
    for key, code in priced.items():
        series[key] = {r["d"]: r["close"] for r in db.q(
            con, "SELECT d, close FROM prices WHERE code=? AND d>=? AND d<=?",
            (code, lo, hi))}

    # tranche -> its instruments, and the half-open [entry, exit] it is held over
    tranches: dict[tuple[str, str], dict[str, Any]] = {}
    for r in positions:
        if not r.get("entry_d") or not r.get("exit_d"):
            continue
        t = tranches.setdefault((r["arm"], r["period"]),
                                {"entry": r["entry_d"], "exit": r["exit_d"],
                                 "keys": []})
        t["entry"] = min(t["entry"], r["entry_d"])
        t["exit"] = max(t["exit"], r["exit_d"])
        if str(r["instrument_id"]) in series:
            t["keys"].append(str(r["instrument_id"]))

    daily_cash = (cash_pct_annual / 100.0) / 252.0
    out: list[dict[str, Any]] = []
    for arm in sorted({r["arm"] for r in positions}):
        mine = {k: v for k, v in tranches.items() if k[0] == arm}
        nav, peak = 100.0, 100.0
        out.append({"arm": arm, "d": cal[0], "equity": 100.0, "period_ret": 0.0,
                    "drawdown": 0.0, "n_positions": 0})
        for i in range(1, len(cal)):
            prev, day = cal[i - 1], cal[i]
            invested, n_open = 0.0, 0
            for t in mine.values():
                # held on `day` when the tranche opened on or before the prior
                # close and has not yet passed its exit
                if not (t["entry"] <= prev and day <= t["exit"]):
                    continue
                rets = []
                for k in t["keys"]:
                    a, b = series[k].get(prev), series[k].get(day)
                    if a and b:
                        rets.append(b / a - 1.0)
                if rets:
                    invested += (sum(rets) / len(rets)) / slots
                    n_open += len(rets)
            live_slots = sum(1 for t in mine.values()
                             if t["entry"] <= prev and day <= t["exit"])
            inv_w = min(live_slots, slots) / slots
            cash_w = max(0.0, 1.0 - inv_w)
            nav *= 1.0 + invested + cash_w * daily_cash
            peak = max(peak, nav)
            out.append({"arm": arm, "d": day, "equity": round(nav, 6),
                        "period_ret": round((invested + cash_w * daily_cash) * 100.0, 6),
                        "drawdown": round((nav / peak - 1.0) * 100.0, 6),
                        # How much of the book was actually at risk that day.
                        # Without it the curve is unreadable at the start: the
                        # first tranche is one slot of four, so the book runs a
                        # quarter invested for a week and cannot be compared to
                        # a fully-invested index over that stretch. On the six
                        # real periods the ramp averages 72% — a curve trailing
                        # the index by less than the ramp costs is ahead of its
                        # own exposure, and that is the comparison worth making.
                        "invested_frac": round(inv_w, 4),
                        "n_positions": n_open})
    if out and no_series:
        out[0]["no_series"] = no_series
    return out


def print_sweep(rep: Sweep) -> Sweep:
    """Console report. Every statistic carries its n and its coverage.

    A mean return without n is not a finding, it is a decoration — and the reader
    who has to look up the sample size is the reader who will not.
    """
    print("\n" + "=" * 78)
    print(f"回测 · {strat.STAGE_LABEL.get(rep.stage, rep.stage)}   "
          f"{rep.dates[0]} → {rep.dates[-1]}   {len(rep.dates)} 个周期   "
          f"持有期 {rep.horizon_days} 天")
    print("=" * 78)
    print(f"  模型调用 {rep.calls} 次" + ("（纯机械）" if rep.calls == 0 else ""))
    print(f"  1 个月窗口要求："
          + ("必须走完，未走完记为 UNKNOWN" if rep.require_full_horizon
             else "允许截断（两组同样截断，仅用于配对比较）"))
    if rep.skipped_need_model:
        print(f"  跳过（需要模型，本次未提供 inference 端口）："
              f"{', '.join(sorted(rep.skipped_need_model))}")

    a0 = rep.audits[0] if rep.audits else None
    if a0:
        print(f"\n【as-of 审计（首个周期 {a0.as_of}）】")
        print(f"  研报 {a0.corpus_n} 条，{a0.corpus_from} → "
              f"{a0.corpus_max_published_d}（≤ {a0.as_of}）"
              f"；其中 {a0.corpus_retrieved_later} 条是当日之后才抓回本地的"
              f"（发布日仍在窗口内，属于「读得更深」而非读到未来）")
        print(f"  行情钳制 {a0.clamp}，实际用到的最新收盘 {a0.price_max_d}")
        print(f"  主题 {a0.topics_n} 个"
              + (f"，按注册日剔除 {len(a0.topics_dropped)} 个：{a0.topics_dropped}"
                 if a0.topics_dropped else "，无越界主题"))
        print(f"  候选 {a0.candidates_n} 条（最新一期 {a0.candidates_max_as_of}）"
              f"，日历 {a0.calendar_n} 条（抹掉未公布 actual {a0.calendar_actuals_stripped} 个）")
        print(f"  inputs_sha {a0.inputs_sha}   越界项 {a0.leaks or '无'}")

    print(f"\n【各组表现】控制组 = {rep.control}")
    print(f"  {'策略':<14}{'角色':<12}{'选中':>5}{'可评分':>6}{'覆盖':>7}"
          f"{'胜率':>7}{'均值':>10}{'中位':>10}{'调用':>5}")
    for name, a in sorted(rep.arms.items(),
                          key=lambda kv: (kv[0] != rep.control, kv[0])):
        print(f"  {name:<14}{a.role:<12}{a.n_chosen:>5}{a.n_scored:>6}"
              f"{(f'{a.coverage*100:.0f}%' if a.coverage is not None else '—'):>7}"
              f"{(f'{a.hit_rate*100:.0f}%' if a.hit_rate is not None else '—'):>7}"
              f"{_pct(a.mean):>10}{_pct(a.median):>10}{a.calls:>5}")
        if a.error:
            print(f"    ! {a.error[:70]}")

    if rep.unknown_total:
        tot = sum(rep.unknown_total.values())
        print(f"\n【无法评分（UNKNOWN，已剔除，未按 0 填充）】候选层面共 {tot} 条")
        for k, n in sorted(rep.unknown_total.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<20}{n:>4}   {UNKNOWN_REASONS.get(k, '')}")
        for name, a in sorted(rep.arms.items()):
            if a.bias_if_zero_filled is not None:
                print(f"    若把 {name} 的 {sum(a.unknown.values())} 条 UNKNOWN 按 0% 填充，"
                      f"其均值会变 {_pct(a.bias_if_zero_filled)} —— 这就是被避免的偏差")

    if rep.paired:
        print(f"\n【配对比较 vs {rep.control}】同一周期、同一候选池，共同市场涨跌已相减")
        for name, p in sorted(rep.paired.items()):
            print(f"\n  {name}  配对差 {_pct(p.mean_diff)}/周期   "
                  f"n={p.n_pairs}  n_eff={p.n_eff}  sd={_pct(p.sd_diff)}  "
                  f"t={p.t_stat if p.t_stat is not None else '—'}  "
                  f"t_eff={p.t_eff if p.t_eff is not None else '—'}")
            print(f"    {'✓' if p.conclusive else '✗'} {p.message}")
    print()
    return rep


def print_periods(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'as_of':<12}{'候选':>5}{'研报窗口':>9}{'已存主题':>9}"
          f"{'收盘钳制':>12}{'实际用到':>12}")
    for r in rows:
        print(f"{r['as_of']:<12}{r['candidates']:>5}{r['corpus_window']:>9}"
              f"{r['themes_stored']:>9}{r['price_clamp']:>12}"
              f"{str(r['last_close_used']):>12}")
    print()
