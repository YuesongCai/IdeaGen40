"""Execution seam: the one place an order leaves this system, and the only place
a real broker can ever be attached.

Every order this system has ever placed was placed by `paper.py` against a 10m USD
book, and the orchestrator calls it directly. That is the right default and it is
not going to change casually — but it means there is no seam: no single point where
"send this order" happens, and therefore nowhere to attach a broker without editing
the pipeline itself. Editing the pipeline to go live is how live trading arrives by
accident. This module is that seam, with three adapters behind one port:

  paper    delegates to `paper.py` unchanged. It re-sizes nothing and re-fills
           nothing. If the abstraction did not fit the engine that is already in
           production it would be describing a system nobody runs.
  shadow   the paper-flow-to-live bridge: routes the intent to the paper book AND
           records the exact live order that would have been sent — venue symbol,
           side, quantity, order type, limit, TIF, expected slippage. Nothing
           leaves the process. This is the stage where the live wiring is proven:
           you diff these recorded orders against a broker's real fills first, and
           discover the symbol mapping, lot rounding and FX errors then, on paper.
  futu     the live shape against Futu OpenD — symbol resolution, placeability,
           cost estimate — whose `submit()` refuses. See `FutuVenue`.

Three properties this module exists to enforce, each a failure this system has
already had or is one line away from:

**Live trading cannot happen by accident.** `IDEAGEN_VENUE` defaults to the string
literal `paper`; an unrecognised value raises instead of falling through; and the
one adapter that knows how to reach a broker cannot place an order at all. An
execution path that works by default is one that a bug, a retry, or a stray cron
entry can trigger — and the loss from that is not recoverable by fixing the bug.

**A retried run must not double-place.** Every intent carries a client order id
derived from (run_id, idea_uid, side), and the ledger below rejects a second
submission of the same id to the same destination instead of sending it again. This
repo has already had a rebuild rebind 58 positions to the wrong instruments and
book a +377% return on one idea; duplicate-safety is a hard requirement here, not
a nicety, because the weekly run is scheduled and therefore *will* be retried.

**A whole batch is checked before any of it is sent.** `validate()` is separate
from `submit()` for the same reason `platform.check()` is separate from the run:
discovering the fifteenth order is unplaceable after fourteen have gone out leaves
a half-expressed book, which is worse than either sending all of it or none.
"""

from __future__ import annotations

import abc
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from . import config, db, ideas as ideas_mod, paper, universe
from .platform.base import Health, NotConfigured, PlatformError
from .sources import futu_px

#: The switch, and the default. `DEFAULT_VENUE` is a literal rather than another
#: environment lookup so that an unset, empty or misspelled variable can only ever
#: resolve to the paper book.
ENV_VENUE = "IDEAGEN_VENUE"
DEFAULT_VENUE = "paper"

SIDES = ("BUY", "SELL")
ORDER_TYPES = ("market", "limit", "band", "breakout")


class ExecutionError(PlatformError):
    """An intent could not be turned into an order. Never swallowed."""


class ExecutionRefused(ExecutionError):
    """The venue declined to act. Distinct from a transport failure: refusing is
    the correct outcome, so a caller must not retry its way past one."""


class LiveTradingDisabled(ExecutionRefused):
    """Raised by `FutuVenue.submit`. Its own type so that no `except
    ExecutionError` anywhere upstream can quietly absorb the refusal."""


# ---------------------------------------------------------------------------
# The ledger. Portable DDL, following `schema.py`: TEXT over VARCHAR(n), JSON
# stored as TEXT, every statement idempotent. Prefixed `exec_` because
# `CREATE TABLE IF NOT EXISTS` against a colliding name is a silent no-op that
# only surfaces later as a missing-column error far from its cause.
#
# This is on disk rather than in memory on purpose. The duplicate a retry
# produces is a *new process*: an in-memory set of submitted ids is empty exactly
# when it matters most.
DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS exec_intents (
         venue           TEXT NOT NULL,
         book_id         TEXT NOT NULL,
         client_order_id TEXT NOT NULL,
         run_id          TEXT NOT NULL,
         idea_uid        TEXT NOT NULL,
         side            TEXT NOT NULL,
         batch_id        TEXT,
         instrument_id   TEXT,
         venue_symbol    TEXT,
         submitted_at    TEXT NOT NULL,
         status          TEXT NOT NULL,
         venue_order_id  TEXT,
         filled_qty      REAL,
         avg_px          REAL,
         fees            REAL,
         live_order      TEXT,
         detail          TEXT,
         PRIMARY KEY (venue, book_id, client_order_id)
       )""",
    "CREATE INDEX IF NOT EXISTS exec_intents_run ON exec_intents (run_id)",
    "CREATE INDEX IF NOT EXISTS exec_intents_idea ON exec_intents (idea_uid)",
)


def ensure_schema(con) -> None:
    """Create the ledger if absent. Cheap enough to call on every submit."""
    for stmt in DDL:
        con.execute(stmt)


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Intent:
    """What the strategy wants, said without naming a venue.

    Strategies and the orchestrator speak `Intent`. No field here is a broker
    symbol, an exchange code or a lot count: the moment a strategy knows that
    `美国大盘股` is `US.SPY` on OpenD and 2800.HK on another broker, changing broker
    means editing strategies. `instrument_id` is a registry key from
    `universe.py`, and translating it is an adapter's only privilege.

    `target_notional` is USD. `target_weight` is a fraction of book equity, for
    callers that size by weight; the paper engine sizes from the idea row itself
    (see `PaperVenue.submit`), so for that adapter both are a statement of intent
    to be compared against what the engine actually did, not an instruction.
    """

    instrument_id: str
    side: str
    idea_uid: str
    run_id: str
    as_of: str
    book_id: str = "disciplined"
    batch_id: str | None = None
    target_weight: float | None = None
    target_notional: float | None = None
    order_type: str = "market"
    limit_px: float | None = None
    # The paper engine's entry discipline is a *band* plus an optional breakout
    # trigger, not a single limit. Dropping either would make the shadow record a
    # worse description of the order the paper book actually placed, which defeats
    # the point of recording it.
    band_lo: float | None = None
    trigger_px: float | None = None
    stop_px: float | None = None
    take_px: float | None = None
    tif: str = "day"
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def client_order_id(self) -> str:
        """Deterministic id derived from (run_id, idea_uid, side).

        Deterministic because the weekly run is scheduled, and a scheduled thing
        gets retried — by a cron that overlaps, by an operator re-running a failed
        step, by a wrapper that treats a timeout as a reason to try again. If the
        id were random, the second attempt would be a second order and the book
        would hold twice the intended position; with this id the venue recognises
        the replay and rejects it. Nothing about the *content* of the order enters
        the hash: a retry that also re-sizes must still collide, because it is the
        same decision, and 58 positions in this repo's history were rebound to the
        wrong instruments by exactly this kind of "same idea, different numbers"
        rebuild.
        """
        h = hashlib.sha1(f"{self.run_id}|{self.idea_uid}|{self.side}".encode())
        return f"IG-{h.hexdigest()[:20]}"

    def as_row(self) -> dict[str, Any]:
        return {"client_order_id": self.client_order_id,
                "instrument_id": self.instrument_id, "side": self.side,
                "idea_uid": self.idea_uid, "run_id": self.run_id,
                "as_of": self.as_of, "book_id": self.book_id,
                "batch_id": self.batch_id, "order_type": self.order_type,
                "target_notional": self.target_notional,
                "target_weight": self.target_weight,
                "limit_px": self.limit_px, "band_lo": self.band_lo,
                "trigger_px": self.trigger_px, "stop_px": self.stop_px,
                "take_px": self.take_px, "tif": self.tif, "note": self.note}


@dataclass(frozen=True)
class Problem:
    """One preflight finding.

    `severity` exists because the two kinds are handled differently and merging
    them would force a choice between refusing batches the paper engine handles
    correctly today, or sending orders that corrupt the book. `block` means
    nothing in the batch is sent; `warn` means the engine's own documented
    behaviour applies (an unmarkable instrument is skipped and disclosed) and the
    operator must be able to see it happened.
    """

    idea_uid: str
    client_order_id: str
    code: str
    message: str
    severity: str = "block"

    @property
    def blocking(self) -> bool:
        return self.severity == "block"

    def __str__(self) -> str:
        return f"[{self.severity}:{self.code}] {self.idea_uid}: {self.message}"


@dataclass(frozen=True)
class LiveOrder:
    """The order a broker would have received. Recorded, never sent.

    This is the artifact the shadow stage exists to produce. Every field is one
    that differs between the paper book and a real venue and can therefore be
    wrong without anyone noticing: the symbol (registry key vs OpenD code), the
    quantity (fractional vs lot-rounded), the price (local currency vs the USD the
    book thinks in), the TIF (the paper engine expires an order after
    `ORDER_TTL_SESSIONS`; a broker needs that expressed as its own TIF), and the
    slippage the cost model assumed. Diff these against real fills before money
    moves and those errors surface on paper.
    """

    client_order_id: str
    idea_uid: str
    venue_symbol: str
    side: str
    qty: float
    order_type: str                  # MARKET | LIMIT — venue vocabulary
    limit_px: float | None
    tif: str
    currency: str
    notional_usd: float
    est_slippage_bps: float
    est_fees_usd: float
    lot_size: int | None = None
    mirrors: str | None = None       # the paper order row this was derived from
    unresolved: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class Ack:
    """What came back, with enough to reconcile against a broker statement.

    `idea_uid` travels all the way through to here on purpose: a fill that cannot
    be traced back to the idea that asked for it cannot be attributed, and
    attribution is the only reason this system keeps a book at all.
    """

    client_order_id: str
    idea_uid: str
    venue: str
    status: str                      # accepted | filled | expired | cancelled
    #                                  | rejected | duplicate
    venue_order_id: str | None = None
    filled_qty: float = 0.0
    avg_px: float | None = None
    fees: float = 0.0
    ts: str = ""
    detail: str = ""
    live_order: LiveOrder | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def placed(self) -> bool:
        return self.status in ("accepted", "filled", "expired", "cancelled")


# ---------------------------------------------------------------------------
def _now() -> str:
    return config.now_hkt().isoformat()


def _instrument(instrument_id: str):
    """Registry entry for a venue-neutral instrument id, or None."""
    return universe.resolve(instrument_id)


def _idea(con, idea_uid: str) -> dict | None:
    r = db.q1(con, "SELECT * FROM ideas WHERE idea_uid=?", (idea_uid,))
    return dict(r) if r else None


def _fx_to_usd(currency: str) -> float | None:
    # Deliberately the paper engine's own table rather than a second one. Two FX
    # sources that disagree would put the shadow record and the paper book at
    # different notionals, and the whole point of the shadow stage is that the
    # difference between them is zero except where a real venue forces it.
    return paper.FX_TO_USD.get(currency)


def _costs(market: str) -> dict[str, float]:
    return config.COSTS.get(market, config.COSTS["US"])


# ---------------------------------------------------------------------------
class ExecutionVenue(abc.ABC):
    """Where intents go. One implementation per destination.

    The port is deliberately four methods. `validate` and `submit` are separate so
    a batch is checked as a batch; `poll` is separate from `submit` because in
    every real venue the fill arrives later and by a different route than the ack;
    `check` is here for the same reason it is on every platform port — a run that
    discovers at step six that the gateway is down has already spent its budget.
    """

    #: Adapter name, and the key `venue()` selects it by.
    name: str = "?"

    # -- translation --------------------------------------------------------
    @abc.abstractmethod
    def symbol(self, con, intent: Intent) -> str:
        """The venue's own identifier for `intent.instrument_id`."""

    # -- preflight ----------------------------------------------------------
    @abc.abstractmethod
    def validate(self, con, intents: Sequence[Intent]) -> list[Problem]:
        """Every problem with the whole batch. Sends nothing, changes nothing."""

    # -- submission ---------------------------------------------------------
    @abc.abstractmethod
    def submit(self, con, intents: Sequence[Intent]) -> list[Ack]:
        """Place the batch. Raises on any blocking problem, before placing any."""

    @abc.abstractmethod
    def poll(self, con, intents: Sequence[Intent] | None = None) -> list[Ack]:
        """Current state of previously submitted intents, for reconciliation."""

    @abc.abstractmethod
    def check(self) -> Health: ...

    # -- shared -------------------------------------------------------------
    def _health_name(self) -> str:
        return f"execution:{self.name}"

    def _base_problems(self, con, intents: Sequence[Intent]) -> list[Problem]:
        """Checks that hold for any venue, including the duplicate scan.

        The duplicate scan runs twice over different things: once within the
        submitted batch (two intents carrying one id is a construction bug and
        must never be sent), and once against the ledger (the same id reaching the
        same destination again is a retry, which is expected and handled
        idempotently rather than refused).
        """
        ensure_schema(con)
        out: list[Problem] = []
        seen: dict[str, str] = {}
        for it in intents:
            coid = it.client_order_id
            if coid in seen:
                out.append(Problem(
                    it.idea_uid, coid, "duplicate_in_batch",
                    f"与 {seen[coid]} 的 client order id 相同：同一批里出现两条相同"
                    f"意图，属于构造错误，整批拒绝"))
            seen[coid] = it.idea_uid

            if it.side not in SIDES:
                out.append(Problem(it.idea_uid, coid, "bad_side",
                                   f"side={it.side!r} 不是 {SIDES}"))
            if it.order_type not in ORDER_TYPES:
                out.append(Problem(it.idea_uid, coid, "bad_order_type",
                                   f"order_type={it.order_type!r} 不在 {ORDER_TYPES}"))
            if it.target_notional is not None and it.target_notional <= 0:
                out.append(Problem(it.idea_uid, coid, "bad_size",
                                   f"target_notional={it.target_notional} 不为正"))
            if it.target_weight is not None and not 0 < it.target_weight <= 1:
                out.append(Problem(it.idea_uid, coid, "bad_size",
                                   f"target_weight={it.target_weight} 超出 (0,1]"))
            if it.target_notional is None and it.target_weight is None:
                out.append(Problem(
                    it.idea_uid, coid, "size_from_engine",
                    "未给出目标金额或权重，由下游引擎自行定仓", "warn"))

            inst = _instrument(it.instrument_id)
            if inst is None:
                out.append(Problem(
                    it.idea_uid, coid, "unknown_instrument",
                    f"{it.instrument_id!r} 不在标的注册表内；只有注册表里的标的"
                    f"才能被映射成任何场所的代码"))
            elif it.side == "BUY":
                # Stops and takes are fixed when the idea is generated and never
                # moved. An inverted pair means the order would stop out the
                # instant it filled — cheap to catch here, expensive to discover
                # from a blotter.
                ref = it.limit_px or it.band_lo
                if ref and it.stop_px and it.stop_px >= ref:
                    out.append(Problem(it.idea_uid, coid, "stop_above_entry",
                                       f"止损 {it.stop_px} 不低于入场价 {ref}"))
                if ref and it.take_px and it.take_px <= ref:
                    out.append(Problem(it.idea_uid, coid, "take_below_entry",
                                       f"止盈 {it.take_px} 不高于入场价 {ref}"))

            prior = self._ledger_get(con, it)
            if prior and prior["status"] != "rejected":
                out.append(Problem(
                    it.idea_uid, coid, "already_submitted",
                    f"该意图已于 {prior['submitted_at'][:19]} 提交到 "
                    f"{prior['venue']}/{prior['book_id']}（状态 {prior['status']}）；"
                    f"重复提交会被幂等拒绝，不会再下一次单", "warn"))
        return out

    # -- ledger -------------------------------------------------------------
    def _ledger_get(self, con, intent: Intent) -> dict | None:
        r = db.q1(con, "SELECT * FROM exec_intents WHERE venue=? AND book_id=? "
                       "AND client_order_id=?",
                  (self.name, intent.book_id, intent.client_order_id))
        return dict(r) if r else None

    def _ledger_put(self, con, intent: Intent, ack: Ack, *,
                    venue_symbol: str | None = None) -> None:
        # The key is (venue, book, client order id) rather than the id alone: the
        # id names the *decision*, and one decision legitimately reaches both the
        # paper book and the shadow book. Duplicate-safety is about the same
        # decision arriving twice at the same destination — which is what a retry
        # produces and what a broker would fill twice.
        db.upsert(con, "exec_intents", {
            "venue": self.name, "book_id": intent.book_id,
            "client_order_id": ack.client_order_id, "run_id": intent.run_id,
            "idea_uid": intent.idea_uid, "side": intent.side,
            "batch_id": intent.batch_id, "instrument_id": intent.instrument_id,
            "venue_symbol": venue_symbol, "submitted_at": ack.ts or _now(),
            "status": ack.status, "venue_order_id": ack.venue_order_id,
            "filled_qty": ack.filled_qty, "avg_px": ack.avg_px,
            "fees": ack.fees,
            "live_order": (json.dumps(ack.live_order.as_row(), ensure_ascii=False)
                           if ack.live_order else None),
            "detail": ack.detail,
        }, ["venue", "book_id", "client_order_id"])

    def _raise_if_blocked(self, problems: Sequence[Problem]) -> None:
        blocking = [p for p in problems if p.blocking]
        if blocking:
            raise ExecutionRefused(
                f"{len(blocking)} 条意图未通过下单前校验，整批未提交：\n  "
                + "\n  ".join(str(p) for p in blocking))


# ---------------------------------------------------------------------------
class PaperVenue(ExecutionVenue):
    """The existing paper engine, behind the port.

    This adapter contains no fill logic, no sizing, no cost model and no calendar.
    All of that is in `paper.py`, is already correct, and encodes invariants that
    took the longest to get right — no same-bar look-ahead, limit fills at the
    worse of band and open, costs on both legs, cash constrained to what the book
    holds. A second implementation of any of them here would drift from the first
    and the drift would show up as a return.

    What this adapter adds is the two things the engine cannot do from where it
    sits: a venue-neutral vocabulary, and duplicate-safety across process
    restarts.
    """

    name = "paper"

    def __init__(self, *, verbose: bool = False):
        self.verbose = verbose

    # -- translation --------------------------------------------------------
    def symbol(self, con, intent: Intent) -> str:
        inst = _instrument(intent.instrument_id)
        if inst is None:
            raise ExecutionError(f"{intent.instrument_id!r} 不在标的注册表内")
        code = inst.futu_code or inst.olive_key
        if not code:
            raise ExecutionError(
                f"{intent.instrument_id!r} 注册表里既无行情代码也无 Olive 代码")
        return code

    # -- preflight ----------------------------------------------------------
    def validate(self, con, intents: Sequence[Intent]) -> list[Problem]:
        out = self._base_problems(con, intents)
        groups: dict[tuple[str, str | None], list[Intent]] = {}
        for it in intents:
            groups.setdefault((it.book_id, it.batch_id), []).append(it)

        for (book_id, batch_id), group in groups.items():
            first = group[0]
            if not batch_id:
                # `paper.open_batch` is the engine's only order-placement entry
                # point and its unit is a batch. An intent with no batch cannot be
                # delegated, and writing the order row here instead would be
                # reimplementing the engine — which is the one thing this adapter
                # must not do.
                out.append(Problem(
                    first.idea_uid, first.client_order_id, "no_batch",
                    "纸面引擎以 batch 为下单单位，该意图没有 batch_id，无法委派"))
                continue
            b = db.q1(con, "SELECT * FROM batches WHERE batch_id=?", (batch_id,))
            if not b:
                out.append(Problem(first.idea_uid, first.client_order_id,
                                   "no_such_batch", f"batch {batch_id} 不存在"))
                continue
            if not (db.jl(b["validation"], {}) or {}).get("pass", False):
                out.append(Problem(
                    first.idea_uid, first.client_order_id, "batch_not_validated",
                    f"batch {batch_id} 未通过校验，引擎会拒绝交易它"))
            if not (config.is_cohort(book_id) or book_id in config.BOOKS):
                out.append(Problem(
                    first.idea_uid, first.client_order_id, "unknown_book",
                    f"book {book_id!r} 既不在 config.BOOKS 也不是当日组合前缀 "
                    f"{config.COHORT_PREFIX}*"))

            n_batch = db.q1(con, "SELECT COUNT(*) n FROM ideas WHERE batch_id=?",
                            (batch_id,))["n"]
            if len(group) < n_batch:
                out.append(Problem(
                    first.idea_uid, first.client_order_id, "paper_batch_scope",
                    f"纸面引擎按 batch 整批下单：提交这 {len(group)} 条意图会让 "
                    f"{batch_id} 的全部 {n_batch} 条一起进入 {book_id}", "warn"))

            already = self._placed_orders(con, book_id, batch_id)
            if already and not all(self._ledger_get(con, it) for it in group):
                out.append(Problem(
                    first.idea_uid, first.client_order_id,
                    "orders_already_on_book",
                    f"{book_id}/{batch_id} 已有 {already} 笔订单；不会再次调用 "
                    f"open_batch（它会把已成交/已过期订单重置为 pending 并按当日现金"
                    f"重新定仓，等于事后改写已有持仓），改为按现有订单回报", "warn"))

            for it in group:
                idea = _idea(con, it.idea_uid)
                if not idea:
                    out.append(Problem(it.idea_uid, it.client_order_id, "no_idea",
                                       f"ideas 表里没有 {it.idea_uid}"))
                    continue
                ok, why = paper.markable(con, idea)
                if not ok:
                    out.append(Problem(
                        it.idea_uid, it.client_order_id, "unmarkable",
                        f"{why} — 引擎会跳过该标的并单独披露，不进入组合", "warn"))
        return out

    def _placed_orders(self, con, book_id: str, batch_id: str) -> int:
        r = db.q1(con, "SELECT COUNT(*) n FROM orders o JOIN ideas i "
                       "ON i.idea_uid=o.idea_uid WHERE o.book_id=? AND i.batch_id=?",
                  (book_id, batch_id))
        return r["n"] if r else 0

    # -- submission ---------------------------------------------------------
    def submit(self, con, intents: Sequence[Intent]) -> list[Ack]:
        """Place the batch by delegating to `paper.open_batch`.

        Two guards sit in front of the delegation, and both are about not placing
        an order twice:

        The ledger check makes a retry idempotent — the second submission of a
        client order id returns the first submission's ack.

        The already-on-book check exists because `paper.open_batch` upserts its
        order rows with `status='pending'`. Calling it a second time over a batch
        that has already traded therefore resurrects filled and expired orders as
        pending, and the next `paper.step` re-fills them at a size derived from
        *today's* cash budget: measured on a copy of the live database, two filled
        orders came back as pending and their positions were rewritten from
        2346.90 to 2346.28 shares — a position silently re-sized after the fact,
        under marks that had already been published. The position count does not
        double only because `pos_id` is deterministic, which is luck rather than
        design. So this adapter never re-enters the engine for a (book, batch)
        that already has orders; it reports the existing rows instead.
        """
        ensure_schema(con)
        problems = self.validate(con, intents)
        self._raise_if_blocked(problems)

        groups: dict[tuple[str, str | None], list[Intent]] = {}
        for it in intents:
            groups.setdefault((it.book_id, it.batch_id), []).append(it)

        acks: list[Ack] = []
        for (book_id, batch_id), group in groups.items():
            # Snapshot the ledger before delegating: after the call, the rows this
            # submission writes would be indistinguishable from a prior run's.
            prior_of = {it.client_order_id: self._ledger_get(con, it)
                        for it in group}
            fresh = [it for it in group if not prior_of[it.client_order_id]]
            delegated = None
            if fresh:
                if self._placed_orders(con, book_id, str(batch_id)):
                    delegated = "adopted"      # rows exist; do not re-enter engine
                else:
                    delegated = "open_batch"
                    paper.open_batch(con, str(batch_id), book_id,
                                     verbose=self.verbose)
            for it in group:
                ack = self._ack_from_book(con, it,
                                          prior=prior_of[it.client_order_id],
                                          delegated=delegated)
                # A rejected duplicate must not touch the ledger row it was
                # rejected against. That row is the reconciliation record of what
                # was actually sent; letting the retry overwrite it would replace
                # a real fill with the ack of an order that never existed.
                if ack.status != "duplicate":
                    self._ledger_put(con, it, ack,
                                     venue_symbol=self._symbol_or_none(con, it))
                acks.append(ack)
        return acks

    def _symbol_or_none(self, con, intent: Intent) -> str | None:
        try:
            return self.symbol(con, intent)
        except ExecutionError:
            return None

    def _ack_from_book(self, con, intent: Intent, *, prior: dict | None,
                       delegated: str | None) -> Ack:
        """Read the engine's own order row back as an ack.

        Read back rather than constructed from what we asked for: the engine is
        the authority on what was placed. It re-derives the entry kind from the
        idea row, sizes from its own cash constraint, and skips instruments it
        cannot mark — so an ack built from the intent would describe an order that
        may not exist.
        """
        if prior:
            return Ack(
                client_order_id=intent.client_order_id, idea_uid=intent.idea_uid,
                venue=self.name, status="duplicate",
                venue_order_id=prior["venue_order_id"],
                filled_qty=prior["filled_qty"] or 0.0, avg_px=prior["avg_px"],
                fees=prior["fees"] or 0.0, ts=_now(),
                detail=f"幂等拒绝：该意图已于 {prior['submitted_at'][:19]} 提交，"
                       f"未重复下单（原状态 {prior['status']}）",
                meta={"first_submitted_at": prior["submitted_at"],
                      "first_status": prior["status"]})

        o = db.q1(con, "SELECT * FROM orders WHERE book_id=? AND idea_uid=?",
                  (intent.book_id, intent.idea_uid))
        if not o:
            why = db.q1(con, "SELECT message FROM alerts WHERE book_id=? AND idea_uid=? "
                             "ORDER BY d DESC LIMIT 1",
                        (intent.book_id, intent.idea_uid))
            return Ack(client_order_id=intent.client_order_id,
                       idea_uid=intent.idea_uid, venue=self.name,
                       status="rejected", ts=_now(),
                       detail=(why["message"] if why
                               else "引擎未为该 idea 生成订单（通常是无法盯市或现金不足）"),
                       meta={"delegated": delegated})
        row = dict(o)
        status = {"pending": "accepted", "filled": "filled",
                  "expired": "expired", "cancelled": "cancelled"}.get(
                      row["status"], row["status"])
        return Ack(
            client_order_id=intent.client_order_id, idea_uid=intent.idea_uid,
            venue=self.name, status=status, venue_order_id=row["order_id"],
            filled_qty=row["fill_qty"] or 0.0, avg_px=row["fill_px"],
            fees=row["fee"] or 0.0, ts=_now(),
            detail=f"{row['kind']} {row['code']} notional=${row['notional']:,.0f}"
                   f" placed={row['placed_d']} expire={row['expire_d']}"
                   + (f" fill={row['fill_rule']}@{row['fill_px']}"
                      if row["fill_px"] else ""),
            meta={"delegated": delegated, "kind": row["kind"],
                  "notional": row["notional"], "code": row["code"],
                  "band_lo": row["band_lo"], "band_hi": row["band_hi"],
                  "trigger": row["trigger"],
                  "placed_d": row["placed_d"], "expire_d": row["expire_d"]})

    def poll(self, con, intents: Sequence[Intent] | None = None) -> list[Ack]:
        ensure_schema(con)
        rows = ([self._ledger_get(con, it) for it in intents] if intents
                else [dict(r) for r in db.q(
                    con, "SELECT * FROM exec_intents WHERE venue=?", (self.name,))])
        acks: list[Ack] = []
        for r in [x for x in rows if x]:
            o = db.q1(con, "SELECT * FROM orders WHERE book_id=? AND idea_uid=?",
                      (r["book_id"], r["idea_uid"]))
            if not o:
                acks.append(Ack(r["client_order_id"], r["idea_uid"], self.name,
                                r["status"], ts=_now(), detail=r["detail"] or ""))
                continue
            row = dict(o)
            status = {"pending": "accepted", "filled": "filled",
                      "expired": "expired"}.get(row["status"], row["status"])
            ack = Ack(r["client_order_id"], r["idea_uid"], self.name, status,
                      venue_order_id=row["order_id"],
                      filled_qty=row["fill_qty"] or 0.0, avg_px=row["fill_px"],
                      fees=row["fee"] or 0.0, ts=_now(),
                      detail=f"{row['kind']} {row['code']} status={row['status']}"
                             + (f" fill_d={row['fill_d']}" if row["fill_d"] else ""))
            con.execute("UPDATE exec_intents SET status=?, filled_qty=?, avg_px=?, "
                        "fees=? WHERE venue=? AND book_id=? AND client_order_id=?",
                        (ack.status, ack.filled_qty, ack.avg_px, ack.fees,
                         self.name, r["book_id"], r["client_order_id"]))
            acks.append(ack)
        return acks

    def advance(self, con, book_id: str, start: str, end: str) -> dict:
        """Run the book forward. Paper-only, and not part of the port.

        A live venue has no such method: fills arrive when the market gives them,
        pushed by the broker. Simulating the passage of time is exactly the part of
        the paper engine that has no live counterpart, so it stays off the
        interface rather than being faked in the other adapters.
        """
        return paper.run(con, book_id, start, end, verbose=self.verbose)

    def check(self) -> Health:
        # `check()` takes no connection on purpose: the platform's doctor runs
        # before anything has opened one, and a health check that needs the thing
        # it is checking to be already wired up is not a health check.
        try:
            con = db.connect()
            books = db.q1(con, "SELECT COUNT(*) n FROM books")["n"]
            orders = db.q1(con, "SELECT COUNT(*) n FROM orders")["n"]
            pos = db.q1(con, "SELECT COUNT(*) n FROM positions WHERE status='open'")["n"]
        except Exception as e:  # noqa: BLE001 — doctor reports, never crashes
            return Health(False, self._health_name(),
                          f"paper book unreadable at {config.DB_PATH}: "
                          f"{type(e).__name__}: {e}")
        return Health(True, self._health_name(),
                      f"paper engine on {config.DB_PATH.name} "
                      f"({books} books, {orders} orders, {pos} open positions); "
                      f"no external venue involved",
                      {"db": str(config.DB_PATH), "books": books,
                       "orders": orders, "open_positions": pos})


# ---------------------------------------------------------------------------
class ShadowVenue(ExecutionVenue):
    """Paper flow with a live order recorded beside it. Sends nothing.

    This is the stage the question "how do I connect paper flow, then live" is
    actually answered in. Going straight from a paper book to a broker means the
    first real order is also the first test of the symbol mapping, the lot
    rounding, the FX direction, the TIF translation and the cost assumptions — five
    things that are wrong by default and expensive to be wrong about once.

    So: the intent is routed to the paper book exactly as today, and the live order
    that *would* have gone out is derived from the resulting paper order row and
    persisted. The order row, not the intent, is the source — the engine re-derives
    the entry kind and the size itself, so building the live order from the intent
    would record something the book never did, and the diff against real fills
    would be measuring the wrong difference.
    """

    name = "shadow"

    def __init__(self, *, book: PaperVenue | None = None,
                 live: "FutuVenue | None" = None, verbose: bool = False):
        self.book = book or PaperVenue(verbose=verbose)
        # The live adapter is held for translation and cost estimation only. It
        # physically cannot place an order, which is why it is safe to hold here.
        self.live = live or FutuVenue()

    def symbol(self, con, intent: Intent) -> str:
        return self.live.symbol(con, intent)

    def validate(self, con, intents: Sequence[Intent]) -> list[Problem]:
        # Both sets of problems, deduplicated by (idea, code): the paper leg must
        # be placeable *and* the live leg must be describable. A shadow run whose
        # live half was never checked would report a clean diff and prove nothing.
        seen: set[tuple[str, str]] = set()
        out: list[Problem] = []
        for p in [*self.book.validate(con, intents),
                  *self.live.validate(con, intents, include_base=False)]:
            k = (p.idea_uid, p.code)
            if k in seen:
                continue
            seen.add(k)
            if p.code in FutuVenue.GATEWAY_DEPENDENT:
                # A live detail this run could not resolve is the shadow stage's
                # output, not its failure. Blocking on it would mean the shadow
                # book can only be run while the gateway is up — which is the
                # opposite of what a pre-live rehearsal is for. It is recorded in
                # the live order's `unresolved` list instead, where the diff will
                # find it.
                p = Problem(p.idea_uid, p.client_order_id, p.code,
                            p.message + "；影子单会照记并标注未解决项", "warn")
            out.append(p)
        return out

    def submit(self, con, intents: Sequence[Intent]) -> list[Ack]:
        ensure_schema(con)
        self._raise_if_blocked(self.validate(con, intents))

        acks: list[Ack] = []
        for it in intents:
            prior = self._ledger_get(con, it)
            if prior and prior["status"] != "rejected":
                acks.append(Ack(
                    it.client_order_id, it.idea_uid, self.name, "duplicate",
                    venue_order_id=prior["venue_order_id"], ts=_now(),
                    detail=f"幂等拒绝：影子单已于 {prior['submitted_at'][:19]} 记录",
                    meta={"first_submitted_at": prior["submitted_at"]}))
                continue

            paper_ack = self.book.submit(con, [it])[0]
            live = (self._live_order(con, it, paper_ack)
                    if paper_ack.placed else None)
            if live:
                detail = (f"纸面已记账；实盘委托已记录、未发送 → {live.venue_symbol} "
                          f"{live.side} {live.qty:g} {live.order_type}"
                          + (f" @{live.limit_px:g}" if live.limit_px else "")
                          + f" {live.tif}"
                          + (f"  ⚠ {'; '.join(live.unresolved)}"
                             if live.unresolved else ""))
            else:
                detail = paper_ack.detail
            ack = Ack(
                client_order_id=paper_ack.client_order_id,
                idea_uid=it.idea_uid, venue=self.name, status=paper_ack.status,
                venue_order_id=paper_ack.venue_order_id,
                filled_qty=paper_ack.filled_qty, avg_px=paper_ack.avg_px,
                fees=paper_ack.fees, ts=_now(), detail=detail,
                live_order=live, meta={**paper_ack.meta, "sent": False})
            self._ledger_put(con, it, ack,
                             venue_symbol=(live.venue_symbol if live else None))
            acks.append(ack)
        return acks

    def _live_order(self, con, intent: Intent, ack: Ack) -> LiveOrder:
        """Translate one placed paper order into the broker order it stands for."""
        inst = _instrument(intent.instrument_id)
        code = ack.meta.get("code") or (inst.futu_code if inst else "") or ""
        ccy = inst.currency if inst else "USD"
        market = code.split(".", 1)[0] if "." in code else "US"
        notional = float(ack.meta.get("notional") or intent.target_notional or 0.0)
        unresolved: list[str] = []

        # The paper engine's entry vocabulary maps onto a broker's like this:
        #   band / limit -> LIMIT at the top of the band (a buy limit)
        #   breakout     -> the trigger is evaluated on a close and executed on the
        #                   next open, which no broker order type expresses; it is
        #                   a working order the caller must place next session, so
        #                   it is recorded as MARKET with the trigger in `note`.
        #   market_close -> MOC where available, else MARKET
        kind = str(ack.meta.get("kind") or intent.order_type)
        if kind in ("band", "limit"):
            # The engine's own band, not the intent's: the band is what the paper
            # order was placed with, and a live order that mirrors anything else is
            # not the order the book actually holds.
            otype, limit = "LIMIT", (ack.meta.get("band_hi") or intent.limit_px
                                     or ack.meta.get("band_lo") or intent.band_lo)
        elif kind.startswith("breakout"):
            otype, limit = "MARKET", None
            unresolved.append(
                "breakout: 收盘触发、次日开盘执行，无对应券商单型，需人工挂单")
        else:
            otype, limit = "MARKET", None

        fx = _fx_to_usd(ccy)
        px = limit
        if px is None:
            hit = futu_px.last_close_on_or_before(con, code, intent.as_of)
            px = hit[1] if hit else None
        qty = 0.0
        if px and fx:
            qty = notional / (px * fx)
        else:
            unresolved.append(
                f"无法定量：{'缺少参考价' if not px else f'{ccy} 无 FX'}")

        # Rounded down to a whole lot even when the lot is one share. The paper
        # book holds fractional quantities because notional / price is a real
        # number; a broker fills whole shares. That difference is small per order
        # and systematic across a book, and it is precisely what this record
        # exists to expose before it shows up as an unexplained tracking error.
        lot = self.live.lot_size(code, allow_remote=False)
        if lot is None:
            unresolved.append(f"{market} 每手股数未知（OpenD 未连接），未做整手取整")
        elif qty:
            qty = math.floor(qty / lot) * lot
            if qty <= 0:
                unresolved.append(f"按每手 {lot} 股取整后数量为 0，实盘无法下单")

        c = _costs(market)
        return LiveOrder(
            client_order_id=intent.client_order_id, idea_uid=intent.idea_uid,
            venue_symbol=code, side=intent.side, qty=round(qty, 4),
            order_type=otype, limit_px=limit,
            # The engine expires an unfilled order after ORDER_TTL_SESSIONS
            # sessions. `day` would silently drop it a day early and `gtc` would
            # leave it working for weeks; either way the live book would diverge
            # from the paper one for a reason that has nothing to do with the idea.
            tif=intent.tif if intent.tif != "day" else f"gtd:{config.ORDER_TTL_SESSIONS}",
            currency=ccy, notional_usd=round(notional, 2),
            est_slippage_bps=c.get("slippage_bps", 0.0),
            est_fees_usd=round(notional * c.get("commission_bps", 0.0) / 10000.0, 2),
            lot_size=lot, mirrors=ack.venue_order_id, unresolved=unresolved)

    def poll(self, con, intents: Sequence[Intent] | None = None) -> list[Ack]:
        # The paper leg is the only leg with state to poll; the live leg has none
        # by construction. Recorded live orders are returned alongside so a
        # reconciliation can line them up against a broker statement.
        out: list[Ack] = []
        for a in self.book.poll(con, intents):
            r = db.q1(con, "SELECT live_order FROM exec_intents WHERE venue=? "
                           "AND client_order_id=?", (self.name, a.client_order_id))
            lo = db.jl(r["live_order"], None) if r else None
            out.append(Ack(a.client_order_id, a.idea_uid, self.name, a.status,
                           venue_order_id=a.venue_order_id,
                           filled_qty=a.filled_qty, avg_px=a.avg_px, fees=a.fees,
                           ts=a.ts, detail=a.detail,
                           live_order=(LiveOrder(**lo) if lo else None),
                           meta={"sent": False}))
        return out

    def recorded(self, con, run_id: str | None = None) -> list[dict]:
        """Every would-be-live order recorded so far — the diff input."""
        sql = "SELECT * FROM exec_intents WHERE venue=? AND live_order IS NOT NULL"
        args: list[Any] = [self.name]
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        ensure_schema(con)
        out = []
        for r in db.q(con, sql + " ORDER BY submitted_at", args):
            row = dict(r)
            row["live_order"] = db.jl(row["live_order"], {})
            out.append(row)
        return out

    def check(self) -> Health:
        paper_h = self.book.check()
        live_h = self.live.check()
        try:
            con = db.connect()
            ensure_schema(con)
            n = db.q1(con, "SELECT COUNT(*) n FROM exec_intents WHERE venue=?",
                      (self.name,))["n"]
        except Exception as e:  # noqa: BLE001
            return Health(False, self._health_name(),
                          f"shadow ledger unreadable: {type(e).__name__}: {e}")
        # Deliberately ok when the live half is down: the shadow stage is designed
        # to be runnable with no gateway at all, and anything it could not resolve
        # is recorded per order in `unresolved` rather than blocking the run.
        return Health(paper_h.ok, self._health_name(),
                      f"records live orders without sending ({n} recorded); "
                      f"paper leg: {paper_h.detail}; live leg: {live_h.detail}",
                      {"recorded": n, "paper_ok": paper_h.ok,
                       "live_ok": live_h.ok, "sends_orders": False})


# ---------------------------------------------------------------------------
class FutuVenue(ExecutionVenue):
    """The live shape against Futu OpenD — which cannot place an order.

    This repo's standing rule is that Futu's TRADE context is never opened: market
    data only. This adapter is written so that the rule is a property of the code
    rather than a discipline someone has to remember. It resolves symbols,
    validates placeability (entitlement, lot size, session, price band), and
    estimates cost — everything you need in order to be *ready* — and `submit()`
    raises `LiveTradingDisabled`.

    There is deliberately no flag, no constructor argument and no environment
    variable that turns submission on, because a flag is a thing a script can set.
    An execution path that works by default is one that a bug, a retry or a stray
    cron entry can trigger, and the difference between "the pipeline crashed" and
    "the pipeline bought 10m USD of the wrong thing" is not recoverable by fixing
    the bug afterwards. Enabling live trading has to be a separate, deliberate,
    human act: a new adapter, reviewed by someone who understands the blast
    radius, with its own credentials, its own position limits and its own kill
    switch — never a value flipped in a config file at 03:00 by a scheduler.

    Note that even the quote path here goes through `sources.futu_px`, which only
    ever constructs `OpenQuoteContext`. No line in this module imports, names or
    constructs a trade context.
    """

    name = "futu"

    #: A limit more than this far from the last close is a stale reference price or
    #: a typo far more often than it is a real order. Blocked rather than warned:
    #: the failure mode is buying at any price.
    PRICE_BAND_PCT = 0.20

    #: Regular-session windows in each market's own timezone. Advisory only — the
    #: authoritative gate is an exchange holiday calendar, which this repo does not
    #: have, so a session finding is a warning and never a block. Claiming holiday
    #: awareness we do not have would be worse than declaring the gap.
    SESSIONS = {"US": (("09:30", "16:00"),),
                "HK": (("09:30", "12:00"), ("13:00", "16:00"))}
    SESSION_TZ = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}

    #: Findings that mean "the gateway could not tell us", as opposed to "the
    #: intent is wrong". They block a live order — you cannot send what you cannot
    #: size — but `ShadowVenue` downgrades them, because a rehearsal that requires
    #: the gateway to be up is not much of a rehearsal.
    GATEWAY_DEPENDENT = frozenset({"lot_size_unknown"})

    def __init__(self) -> None:
        self._lots: dict[str, int] = {}
        # Once OpenD has proved unreachable, stop asking. Validating a 40-idea
        # batch must not pay a connection timeout forty times.
        self._remote_down = False

    # -- translation --------------------------------------------------------
    def symbol(self, con, intent: Intent) -> str:
        inst = _instrument(intent.instrument_id)
        if inst is None:
            raise ExecutionError(f"{intent.instrument_id!r} 不在标的注册表内")
        if inst.kind != "listed" or not inst.futu_code:
            raise ExecutionError(
                f"{intent.instrument_id!r} 是 {inst.kind}，Futu 无法交易；"
                f"基金/结构化产品走 Olive 申赎，不属于本通道")
        if inst.market not in config.PRICEABLE_MARKETS:
            raise ExecutionError(
                f"{inst.futu_code} 所属市场 {inst.market} 未在本 OpenD 订阅范围内 "
                f"{config.PRICEABLE_MARKETS}")
        return inst.futu_code

    def lot_size(self, code: str, *, allow_remote: bool = True) -> int | None:
        """Shares per lot, or None when it cannot be established.

        None rather than a guess. Assuming 1 is correct in the US and wrong in
        Hong Kong, where lots run to 500 or 1000 — an assumed lot size turns into
        a rejected order at best and an unintended position size at worst.
        """
        if code in self._lots:
            return self._lots[code]
        market = code.split(".", 1)[0] if "." in code else "US"
        if market == "US":
            self._lots[code] = 1              # US equities trade in single shares
            return 1
        if not allow_remote or self._remote_down:
            return None
        try:
            snap = self._snapshot([code])
        except NotConfigured:
            self._remote_down = True
            return None
        lot = snap.get(code, {}).get("lot_size")
        if lot:
            self._lots[code] = int(lot)
            return int(lot)
        return None

    #: Seconds allowed for the "is the gateway even listening" probe.
    PROBE_TIMEOUT_S = 1.5

    def _gateway_reachable(self) -> tuple[bool, str]:
        """TCP-level probe of OpenD before any SDK call.

        `futu_px.quote_ctx()` blocks for a long time when nothing is listening on
        the port — the SDK retries in a background thread rather than failing — so
        a `check()` that went straight to it could stall the preflight it exists to
        speed up. A socket connect answers the same question in milliseconds and
        cannot hang. This does not replace `futu_px.health()`; it decides whether
        calling it is safe.
        """
        import socket                         # stdlib, but only needed here

        try:
            with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT),
                                          timeout=self.PROBE_TIMEOUT_S):
                return True, "listening"
        except OSError as e:
            return False, (f"nothing listening on {config.FUTU_HOST}:"
                           f"{config.FUTU_PORT} ({e.__class__.__name__}: {e}). "
                           f"Start Futu_OpenD and log in.")

    def _snapshot(self, codes: Sequence[str]) -> dict[str, dict]:
        """Quote-only snapshot. Raises `NotConfigured` when OpenD is not there.

        `quote_ctx` is the only Futu context this module can reach, and it builds
        an `OpenQuoteContext`. The vendor SDK is imported inside it, so importing
        this module never requires futu-api to be installed.
        """
        ok, why = self._gateway_reachable()
        if not ok:
            raise NotConfigured(why)
        try:
            with futu_px.quote_ctx() as ctx:
                from futu import RET_OK        # lazy: vendor import, never at top

                ret, data = ctx.get_market_snapshot(list(codes))
                if ret != RET_OK:
                    raise NotConfigured(f"OpenD snapshot failed: {str(data)[:160]}")
                return {str(r["code"]): dict(r) for _, r in data.iterrows()}
        except futu_px.OpenDUnavailable as e:
            raise NotConfigured(str(e)) from e

    # -- preflight ----------------------------------------------------------
    def validate(self, con, intents: Sequence[Intent],
                 *, include_base: bool = True) -> list[Problem]:
        """The useful half of this adapter: is this batch actually placeable.

        Everything here is checkable without sending anything, and every finding
        is one that would otherwise be discovered as a broker rejection or, worse,
        as a fill nobody intended.
        """
        out = list(self._base_problems(con, intents)) if include_base else []
        for it in intents:
            coid = it.client_order_id
            try:
                code = self.symbol(con, it)
            except ExecutionError as e:
                out.append(Problem(it.idea_uid, coid, "unsupported_instrument",
                                   str(e)))
                continue

            market = code.split(".", 1)[0]
            lot = self.lot_size(code)
            if lot is None:
                out.append(Problem(
                    it.idea_uid, coid, "lot_size_unknown",
                    f"{code} 每手股数未知（OpenD 未连接），无法确认整手数量"))

            hit = futu_px.last_close_on_or_before(con, code, it.as_of)
            if not hit:
                out.append(Problem(it.idea_uid, coid, "no_reference_price",
                                   f"{code} 在 {it.as_of} 之前没有收盘价，无法估价"))
            elif it.limit_px:
                drift = abs(it.limit_px / hit[1] - 1)
                if drift > self.PRICE_BAND_PCT:
                    out.append(Problem(
                        it.idea_uid, coid, "price_band",
                        f"限价 {it.limit_px:g} 与 {hit[0]} 收盘 {hit[1]:g} 相差 "
                        f"{drift*100:.0f}%，超出 {self.PRICE_BAND_PCT*100:.0f}% "
                        f"保护带，视为参考价过期或手误"))

            if lot and it.target_notional and hit:
                est = self.estimate(con, it)
                if est["qty"] <= 0:
                    out.append(Problem(
                        it.idea_uid, coid, "size_below_one_lot",
                        f"目标金额 ${it.target_notional:,.0f} 不足一手"
                        f"（{lot} 股 ≈ ${est['lot_notional_usd']:,.0f}）"))

            if not self._session_open(market):
                out.append(Problem(
                    it.idea_uid, coid, "market_closed",
                    f"{market} 当前不在常规交易时段（不含假期日历，仅供参考）",
                    "warn"))
        return out

    def _session_open(self, market: str, now: datetime | None = None) -> bool:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self.SESSION_TZ.get(market, "America/New_York"))
        t = (now or datetime.now(tz)).astimezone(tz)
        if t.weekday() >= 5:
            return False
        hhmm = t.strftime("%H:%M")
        return any(a <= hhmm <= b for a, b in self.SESSIONS.get(market, ()))

    def estimate(self, con, intent: Intent) -> dict[str, Any]:
        """Lot-rounded quantity and one-leg cost, without sending anything."""
        code = self.symbol(con, intent)
        market = code.split(".", 1)[0]
        inst = _instrument(intent.instrument_id)
        ccy = inst.currency if inst else "USD"
        fx = _fx_to_usd(ccy)
        hit = futu_px.last_close_on_or_before(con, code, intent.as_of)
        px = intent.limit_px or (hit[1] if hit else None)
        notional = float(intent.target_notional or 0.0)
        lot = self.lot_size(code)
        raw = (notional / (px * fx)) if (px and fx and notional) else 0.0
        qty = (math.floor(raw / lot) * lot) if lot else raw
        c = _costs(market)
        gross_usd = qty * (px or 0.0) * (fx or 0.0)
        return {
            "venue_symbol": code, "market": market, "currency": ccy,
            "reference_px": px, "reference_d": (hit[0] if hit else None),
            "fx_to_usd": fx, "lot_size": lot,
            "qty_unrounded": round(raw, 4), "qty": round(qty, 4),
            "notional_usd": round(gross_usd, 2),
            "lot_notional_usd": (round(lot * (px or 0) * (fx or 0), 2) if lot else None),
            "commission_usd": round(gross_usd * c.get("commission_bps", 0) / 10000.0, 2),
            "slippage_usd": round(gross_usd * c.get("slippage_bps", 0) / 10000.0, 2),
            "slippage_bps": c.get("slippage_bps", 0.0),
        }

    # -- submission ---------------------------------------------------------
    def submit(self, con, intents: Sequence[Intent]) -> list[Ack]:
        raise LiveTradingDisabled(
            "拒绝下单：实盘通道是故意没有接的。\n"
            f"  本次请求 {len(intents)} 笔意图"
            + (f"（{intents[0].idea_uid} …）" if intents else "")
            + "，一笔都没有发出，也没有连接 Futu 交易上下文。\n"
            "  原因：默认可用的下单通道，等于一个 bug、一次重试或一条无人看管的 cron "
            "就能触发的下单通道。\n"
            "  本系统只使用 OpenD 的行情上下文（OpenQuoteContext）；交易上下文从未打开，"
            "本模块也没有任何开关可以打开它。\n"
            "  要真正接实盘，必须由人另做一次显式授权：新增一个经过评审的下单适配器，"
            "配独立凭证、独立仓位上限与独立熔断，并先用 shadow 通道把录下来的委托与"
            "券商真实成交对齐。\n"
            "  现在可以做的：venue('futu').validate(...) 与 .estimate(...) "
            "会在不发送任何指令的前提下告出可交易性与成本；"
            "venue('shadow') 会把每一笔本该发出的实盘委托记录下来。")

    def poll(self, con, intents: Sequence[Intent] | None = None) -> list[Ack]:
        # Nothing can ever have been sent through this adapter, so there is nothing
        # to reconcile. Returning empty is not a lie about the account: if this
        # ever needs to return rows, something other than this module placed them.
        return []

    def check(self) -> Health:
        reachable, why = self._gateway_reachable()
        if not reachable:
            # Not configured rather than broken: the operator has to start a
            # gateway, which is a different fix from "it is up and returning
            # errors". Either way `submit()` still refuses, so this is never the
            # thing standing between a run and a live order.
            return Health(False, self._health_name(),
                          f"not configured: {why} — quote path only; "
                          f"submit() refuses regardless",
                          {"submit": "refused", "trade_context": "never opened",
                           "reachable": False})
        try:
            h = futu_px.health()
        except Exception as e:  # noqa: BLE001 — doctor reports, never crashes
            return Health(False, self._health_name(),
                          f"not configured: {type(e).__name__}: {e}",
                          {"submit": "refused", "trade_context": "never opened"})
        if not h.get("ok"):
            return Health(False, self._health_name(),
                          f"not configured: {h.get('error', 'OpenD unreachable')} "
                          f"— quote path only; submit() refuses regardless",
                          {"submit": "refused", "trade_context": "never opened",
                           "error": h.get("error")})
        return Health(True, self._health_name(),
                      f"OpenD quote reachable at {config.FUTU_HOST}:{config.FUTU_PORT} "
                      f"(probe {h.get('probe')} last={h.get('last')}); "
                      f"market data only — submit() refuses by design",
                      {"submit": "refused", "trade_context": "never opened",
                       "markets": list(config.PRICEABLE_MARKETS), **h})


# ---------------------------------------------------------------------------
#: Name -> adapter. `venue()` is the only way anything should be constructed, so
#: that adding a real broker later means adding a row here and nothing else.
VENUES: dict[str, type[ExecutionVenue]] = {
    "paper": PaperVenue,
    "shadow": ShadowVenue,
    "futu": FutuVenue,
}


def venue(name: str | None = None, **kw: Any) -> ExecutionVenue:
    """Build the selected venue. `IDEAGEN_VENUE`, defaulting to `paper`.

    An unrecognised name raises rather than falling back. A fallback is how a
    typo becomes a different destination than the operator meant, and in this
    module the destinations are not interchangeable.
    """
    key = (name or os.environ.get(ENV_VENUE) or DEFAULT_VENUE).strip().lower()
    if key not in VENUES:
        raise ExecutionError(
            f"未知的 {ENV_VENUE}={key!r}；可选 {sorted(VENUES)}，默认 "
            f"{DEFAULT_VENUE!r}。不做回退：拼错的场所名不应该变成另一个下单目的地")
    return VENUES[key](**kw)


def selected() -> str:
    """Which venue the environment currently selects, without constructing it."""
    key = (os.environ.get(ENV_VENUE) or DEFAULT_VENUE).strip().lower()
    return key if key in VENUES else f"{key}(invalid)"


def check_all() -> list[Health]:
    """Health of every adapter, for `doctor`. Never raises."""
    out = []
    for n, cls in VENUES.items():
        try:
            out.append(cls().check())
        except Exception as e:  # noqa: BLE001
            out.append(Health(False, f"execution:{n}", f"{type(e).__name__}: {e}"))
    return out


# ---------------------------------------------------------------------------
def intents_from_batch(con, batch_id: str, *, run_id: str, book_id: str,
                       side: str = "BUY",
                       idea_uids: Iterable[str] | None = None) -> list[Intent]:
    """Turn a stored batch into venue-neutral intents.

    Sizing comes from `paper.size_batch` rather than from a second rule here, so
    the notional an intent carries is the one the paper book would actually use.
    That is what makes the shadow record comparable: if the recorded live order
    and the paper order disagree on size, the cause is a venue constraint (lot
    rounding, FX) and not two different sizing rules quietly diverging.
    """
    rows = ideas_mod.load_batch(con, batch_id)
    if idea_uids is not None:
        want = set(idea_uids)
        rows = [r for r in rows if r["idea_uid"] in want]
    spec = paper.book_spec(book_id)
    b = db.q1(con, "SELECT as_of FROM batches WHERE batch_id=?", (batch_id,))
    as_of = b["as_of"] if b else config.today_hkt().isoformat()
    equity = paper.current_equity(con, book_id, as_of) or spec["capital"]
    sized = paper.size_batch(con, book_id, rows, equity)["notional"]

    out: list[Intent] = []
    for r in rows:
        # The strategy's statement of entry discipline. The engine re-derives its
        # own order kind from the same idea row, so the authority on what was
        # placed remains the order row — see `PaperVenue._ack_from_book`.
        if r["entry_lo"] or r["entry_hi"]:
            otype = "band"
        elif r["entry_break"]:
            otype = "breakout"
        else:
            otype = "market"
        out.append(Intent(
            instrument_id=(r["futu_code"] or r["olive_key"] or r["tool"]),
            side=side, idea_uid=r["idea_uid"], run_id=run_id, as_of=as_of,
            book_id=book_id, batch_id=batch_id,
            target_notional=sized.get(r["idea_uid"]),
            order_type=otype,
            limit_px=(r["entry_hi"] or r["entry_lo"]),
            band_lo=r["entry_lo"], trigger_px=r["entry_break"],
            stop_px=r["stop_px"], take_px=r["take_lo"],
            note=r["action"] or "", meta={"theme": r["theme"], "grade": r["grade"],
                                          "rank": r["rank"], "tool": r["tool"]}))
    return out
