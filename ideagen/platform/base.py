"""Platform ports: the six things this system needs from whatever it runs on.

The system currently runs on one laptop against a SQLite file. Production means
BytePlus. Rewriting the pipeline for BytePlus would leave no way to develop or
test it, and would couple every module to a cloud account — so instead the
pipeline talks to these six abstract ports and never imports a vendor SDK.

  BlobStore    immutable artifacts: briefing packs, batches, dashboards, journals
  StateStore   transactional state: corpus, scores, ideas, orders, positions, marks
  Inference    the model calls
  EventBus     run lifecycle events, for monitoring and for fan-out later
  Cache        short-lived shared state: MCP responses, locks, dedupe keys
  SecretStore  credentials, never in the repo and never in an image layer

Two adapters implement all six: `local` (filesystem + SQLite + direct API) and
`byteplus` (TOS + RDS for MySQL/PostgreSQL + ModelArk + Message Queue for Kafka,
Cache for Redis and KMS). `IDEAGEN_PLATFORM` picks one.

Three rules the ports exist to enforce, each of which was violated at least once
while this system was a laptop script:

**Artifacts are immutable and addressed by run.** Every write goes under
`runs/{run_id}/`. Replacing a batch in place is what once rebound 58 positions to
the wrong instruments and booked a +377% return on one idea. A blob store with no
overwrite path makes that class of error unrepresentable.

**Nothing important lives only in the sandbox.** A cloud sandbox is discarded
after the run. Anything not pushed through a port is gone, so the ports are the
complete definition of what survives.

**Every port is health-checkable before the run starts.** A weekly run that
discovers at step 6 that the database is unreachable has already spent the
model budget. `check()` on each port, then run.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence


def redact_url(url: str) -> str:
    """Strip credentials from a connection URL, keeping the part worth reading.

    Health details are stored in the run journal, written to object storage as an
    immutable artifact and published to the event bus. A URL of the form
    `redis://:password@host` therefore puts that password somewhere it can never be
    deleted from. A health line exists to tell the operator which host answered,
    not which secret opened it.
    """
    if not url or "@" not in url:
        return url or ""
    scheme, _, rest = url.partition("://")
    if not rest:
        return "***"
    creds, _, host = rest.rpartition("@")
    if not creds:
        return url
    user = creds.split(":", 1)[0]
    shown = f"{user}:***" if user else "***"
    return f"{scheme}://{shown}@{host}" if scheme else f"{shown}@{host}"


@dataclass(frozen=True)
class Health:
    """One port's readiness. `detail` is for humans, `meta` for the run journal."""
    ok: bool
    name: str
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class PlatformError(RuntimeError):
    """A port could not do its job. Never swallowed — a weekly run must fail loud."""


class NotConfigured(PlatformError):
    """The adapter is selected but its credentials or endpoints are missing.

    Separate from PlatformError so `doctor` can tell "you have not set this up"
    apart from "it is set up and broken", which are different problems with
    different fixes.
    """


class BlobMissing(PlatformError):
    """The store answered, and the artifact is not there.

    Separate from PlatformError for the same reason NotConfigured is, and the
    distinction is load-bearing for anything that shows provenance to a reader.
    "This run never wrote that artifact" and "this node cannot read the bucket
    the artifact is in" are opposite facts: the first is a real gap in the
    record, the second is a gap in *this* machine and the record is intact
    elsewhere. Collapsed into one exception type they render as the same
    sentence, and a dashboard ends up telling a reader that evidence was never
    produced when it exists and is simply out of reach.
    """


# ---------------------------------------------------------------------------
class BlobStore(abc.ABC):
    """Append-only artifact storage, keyed by path.

    Keys are hierarchical strings (`runs/2026-08-12T07-23/pack.json`). There is
    deliberately no `overwrite` or `move`: correcting an artifact means writing a
    new run, so the record of what was actually used at decision time survives.
    """

    @abc.abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str | None = None,
            metadata: dict[str, str] | None = None) -> str:
        """Store `data` and return a stable URI. Raises if `key` already exists."""

    @abc.abstractmethod
    def get(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def list(self, prefix: str) -> Iterator[str]: ...

    @abc.abstractmethod
    def uri(self, key: str) -> str:
        """Where this key lives, for the journal. Need not be publicly fetchable."""

    @abc.abstractmethod
    def check(self) -> Health: ...


# ---------------------------------------------------------------------------
class StateStore(abc.ABC):
    """Transactional store for everything the pipeline reads back and updates.

    Deliberately a thin SQL port rather than an ORM: the existing pipeline is
    ~9,000 lines of hand-written SQL that is already correct, and the cost of an
    ORM migration would be paid in exactly the invariants (as-of clamping,
    cash constraints, mark ordering) that took the longest to get right.

    `paramstyle` exists because SQLite uses `?` while cloud drivers use `%s`.
    Modules that need portable SQL call `q()` with `?` and let the adapter
    translate; `dialect` handles the DDL/upsert differences that placeholders
    cannot hide.
    """

    paramstyle: str = "qmark"
    dialect: str = "unknown"

    @abc.abstractmethod
    def q(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        """Run a statement, return affected row count."""

    @abc.abstractmethod
    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int: ...

    @abc.abstractmethod
    def tx(self):
        """Context manager for an atomic unit of work."""

    @abc.abstractmethod
    def migrate(self, ddl: Sequence[str]) -> int:
        """Apply idempotent DDL. Returns the number of statements that ran."""

    @abc.abstractmethod
    def check(self) -> Health: ...


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Completion:
    """One model response, plus what it cost and what produced it.

    `raw_id` and `usage` are stored with every scoring decision because a factor
    score that cannot be traced to the call that produced it cannot be audited
    later — and half this system's factors are about to become model judgements
    rather than counts.
    """
    text: str
    model: str
    raw_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int | None = None


class Inference(abc.ABC):
    """Model calls, with the sampling controls the scoring design depends on.

    `complete_many` is a port method rather than a caller-side loop because the
    scoring design calls for sampling the same prompt k times and taking the
    median — published forecasting work finds that ensembling independent samples
    reduces variance and overconfidence, and that a narrative framing measurably
    degrades probability quality versus a direct structured one. The port makes
    the k-sample call the easy path.
    """

    @abc.abstractmethod
    def complete(self, prompt: str, *, system: str | None = None,
                 model: str | None = None, temperature: float = 0.0,
                 max_tokens: int | None = None,
                 json_schema: dict[str, Any] | None = None) -> Completion: ...

    @abc.abstractmethod
    def complete_many(self, prompt: str, *, k: int = 5,
                      **kw: Any) -> list[Completion]:
        """k independent samples of the same prompt, for median aggregation."""

    @abc.abstractmethod
    def check(self) -> Health: ...


# ---------------------------------------------------------------------------
class EventBus(abc.ABC):
    """Run lifecycle events. Fire-and-forget; never blocks the pipeline.

    Publishing must not be able to fail a run: a monitoring hook that can abort
    the weekly cycle is a liability, not observability. Adapters swallow and
    record transport errors rather than raising.
    """

    @abc.abstractmethod
    def publish(self, topic: str, event: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def check(self) -> Health: ...


# ---------------------------------------------------------------------------
class Cache(abc.ABC):
    """Short-lived shared state: MCP responses, run locks, dedupe keys.

    `lock` matters more than the caching does. The weekly run is scheduled, and a
    retry or a manual re-run overlapping the scheduled one would place the same
    orders twice. The lock is how "exactly one run per week" is enforced.
    """

    @abc.abstractmethod
    def get(self, key: str) -> bytes | None: ...

    @abc.abstractmethod
    def set(self, key: str, value: bytes, *, ttl_s: int | None = None) -> None: ...

    @abc.abstractmethod
    def lock(self, key: str, *, ttl_s: int = 3600):
        """Context manager yielding True if acquired, False if already held."""

    @abc.abstractmethod
    def check(self) -> Health: ...


# ---------------------------------------------------------------------------
class SecretStore(abc.ABC):
    """Credential lookup. The only place the pipeline learns a secret.

    Exists so that moving from `~/.ideagen.env` to KMS touches one adapter rather
    than every call site, and so no secret is ever read from a file inside an
    image layer.
    """

    @abc.abstractmethod
    def get(self, name: str, *, required: bool = True) -> str | None: ...

    @abc.abstractmethod
    def check(self) -> Health: ...


# ---------------------------------------------------------------------------
class Unavailable:
    """Stands in for a port that could not be configured.

    `load()` must never raise. A port whose credentials are missing has to be
    reportable by `doctor`, and an exception during construction means the
    operator gets a stack trace instead of the one line telling them which
    variable to set — which is the opposite of what a health check is for.

    So an unconfigurable port becomes this: `check()` reports the reason, and any
    real call raises. A run that needs the port still fails, loudly and at the
    point of use; a run that does not need it proceeds.
    """

    def __init__(self, name: str, reason: str):
        self._name = name
        self._reason = reason

    def check(self) -> Health:
        return Health(False, self._name, f"not configured: {self._reason}",
                      {"unavailable": True, "reason": self._reason})

    def __getattr__(self, item: str):
        if item.startswith("_"):
            raise AttributeError(item)

        def _raise(*_a: Any, **_kw: Any):
            raise NotConfigured(
                f"{self._name}.{item}() called but the port is not configured: "
                f"{self._reason}")
        return _raise


def port_or_unavailable(name: str, build):
    """Build a port, or return an `Unavailable` describing why it could not be.

    Only `NotConfigured` is absorbed. A genuine error inside an adapter — a typo,
    a bad DSN format — must still surface at load time rather than being disguised
    as "you forgot to set something".
    """
    try:
        return build()
    except NotConfigured as e:
        return Unavailable(name, str(e))


@dataclass
class Platform:
    """The six ports, wired. Built by `platform.load()`; never constructed inline."""
    name: str
    blobs: BlobStore
    state: StateStore
    inference: Inference
    events: EventBus
    cache: Cache
    secrets: SecretStore

    def check(self) -> list[Health]:
        """Health of every port. Call before a run, store the result in the journal."""
        out = []
        for attr in ("secrets", "state", "blobs", "inference", "cache", "events"):
            port = getattr(self, attr)
            try:
                out.append(port.check())
            except NotConfigured as e:
                out.append(Health(False, attr, f"not configured: {e}"))
            except Exception as e:  # noqa: BLE001 — doctor reports, never crashes
                out.append(Health(False, attr, f"{type(e).__name__}: {e}"))
        return out

    #: Ports a run needs unless it says otherwise. `events` is excluded on
    #: purpose: losing monitoring degrades visibility, while refusing to run loses
    #: the week's data — and a week's corpus cannot be re-fetched later at the
    #: depth a live run would have had.
    DEFAULT_NEED = ("secrets", "state", "blobs", "cache")

    def ready(self, *, need: Sequence[str] | None = None) -> bool:
        """Whether a run may start, given the ports it actually uses.

        Readiness is per-run rather than global. A run using only mechanical
        strategies has no reason to be blocked by a missing model key, and an
        all-or-nothing gate would either block it or stop protecting the runs that
        do need inference. Callers declare what they use; `check()` still reports
        everything so nothing is hidden.
        """
        want = set(need if need is not None else self.DEFAULT_NEED)
        return all(h.ok for h in self.check() if h.name in want)

    def missing(self, *, need: Sequence[str] | None = None) -> list[Health]:
        """The ports that are needed and not ready, for a readable error."""
        want = set(need if need is not None else self.DEFAULT_NEED)
        return [h for h in self.check() if h.name in want and not h.ok]
