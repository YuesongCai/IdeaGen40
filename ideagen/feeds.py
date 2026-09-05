"""Feed registry: data sources are pluggable, and every feed emits one shape.

Two distinct asks sit behind this module. Sources have to be addable, removable and
adjustable without touching anything downstream; and whatever comes in has to keep
a consistent shape, because that is what lets the strategy layer be swapped
independently — a strategy can only be a plugin if the data reaching it does not
change form when a source is added.

So a feed declares its kind and returns rows conforming to that kind's schema.
Three kinds cover the system:

    corpus     research text                 (Wisburg lines, and anything like it)
    universe   tradeable instruments         (Olive shelf, iARK, listed registry)
    calendar   dated events with expectations (macro schedule, earnings, auctions)

`validate()` enforces the schema at the boundary. A feed that drifts fails at
ingest with a named field rather than three steps later as a wrong number, which is
the difference between a bad afternoon and a bad quarter.

Feeds are also the isolation boundary. Each returns rows stamped with `as_of` and
`feed`, so one period's data cannot silently mix with another's, and a feed going
dark degrades that kind rather than failing the run.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Literal

Kind = Literal["corpus", "universe", "calendar"]

#: Required fields per kind. Extra fields are allowed and preserved — a feed with
#: something useful to add should not have to ask permission — but these must be
#: present, because everything downstream indexes on them.
SCHEMA: dict[Kind, dict[str, type | tuple[type, ...]]] = {
    "corpus": {
        "doc_id": str,          # stable id, unique within the feed
        "published_d": str,     # YYYY-MM-DD — the as-of basis, never ingest time
        "title": str,
        "tier": int,            # 1 first-hand, 2 sell-side, 3 curated
    },
    "universe": {
        "instrument_id": str,   # stable id
        "name": str,
        "kind": str,            # listed | fund | structured
        "priceable": bool,      # can it be marked at all
    },
    "calendar": {
        "event_id": str,
        "date": str,            # YYYY-MM-DD
        "label": str,
        "kind": str,            # macro_release | earnings | auction | policy
    },
}

#: Optional but load-bearing where present. A calendar event without an
#: expectation cannot support a deviation trigger, so the gap is worth naming
#: rather than discovering when a watchpoint never fires.
RECOMMENDED: dict[Kind, tuple[str, ...]] = {
    "corpus": ("summary", "body", "institution", "url", "content_hash", "retrieval"),
    # `vehicle` is what the mandate gate judges an instrument on. A universe feed
    # that omits it hands every one of its rows to that gate as「载体未确认」, so
    # the omission has to be visible here rather than read as a shelf of
    # unconfirmed products.
    "universe": ("currency", "exposure", "vehicle", "liquidity", "futu_code",
                 "olive_key"),
    "calendar": ("expectation", "actual", "unit", "source"),
}


class FeedError(RuntimeError):
    pass


@dataclass
class FeedResult:
    feed: str
    kind: Kind
    as_of: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def sha(self) -> str:
        key = SCHEMA[self.kind]
        ids = sorted(str(r.get(next(iter(key)))) for r in self.rows)
        return hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16]


_REGISTRY: dict[str, dict[str, Any]] = {}
_LOADED = False


def register(name: str, kind: Kind, *, label: str = "", required: bool = False,
             expect_rows: int = 0,
             params: dict[str, Any] | None = None) -> Callable:
    """Register a feed.

    `required=False` is the default on purpose. A feed that can fail the whole run
    turns every third-party outage into a lost period, and a lost period cannot be
    re-fetched later at the depth a live run would have had. Only mark a feed
    required when the run genuinely produces nothing without it.

    `expect_rows` is the floor below which a result is suspicious rather than
    merely small. An empty return is the most dangerous kind of feed failure
    because it validates cleanly: zero rows satisfy every schema rule, so a dead
    endpoint reads as a quiet week and the run proceeds as though it had looked.
    Declaring a floor turns that into a named problem.
    """
    def deco(fn: Callable[[date, dict[str, Any]], Iterable[dict[str, Any]]]):
        if name in _REGISTRY:
            raise FeedError(f"feed {name!r} already registered by "
                            f"{_REGISTRY[name]['module']}")
        _REGISTRY[name] = {"fn": fn, "name": name, "kind": kind,
                           "label": label or name, "required": required,
                           "expect_rows": int(expect_rows),
                           "params": params or {}, "module": fn.__module__,
                           "doc": (fn.__doc__ or "").strip().split("\n")[0]}
        return fn
    return deco


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        from . import feeds_impl as pkg
    except ImportError:
        return
    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{m.name}")


def available(kind: Kind | None = None) -> list[dict[str, Any]]:
    _load()
    out = [{k: v for k, v in r.items() if k != "fn"}
           for r in _REGISTRY.values() if kind is None or r["kind"] == kind]
    out.sort(key=lambda r: (r["kind"], not r["required"], r["name"]))
    return out


def validate(kind: Kind, rows: list[dict[str, Any]], *,
             feed: str = "?") -> list[str]:
    """Check rows against the kind's schema. Returns problems, worst first.

    Reports at most a handful of examples per problem: a feed that drifted has
    usually drifted on every row, and a thousand identical complaints hide the
    second, different problem underneath.
    """
    req = SCHEMA[kind]
    problems: list[str] = []
    missing: dict[str, int] = {}
    wrong: dict[str, list[str]] = {}
    for r in rows:
        for f, t in req.items():
            if f not in r or r[f] is None:
                missing[f] = missing.get(f, 0) + 1
            elif not isinstance(r[f], t):
                wrong.setdefault(f, []).append(
                    f"{type(r[f]).__name__}={r[f]!r}"[:40])
    for f, n in sorted(missing.items(), key=lambda kv: -kv[1]):
        problems.append(f"{feed}: {n}/{len(rows)} rows missing required {f!r}")
    for f, ex in wrong.items():
        problems.append(f"{feed}: {f!r} has wrong type in {len(ex)} rows "
                        f"(expected {req[f].__name__ if isinstance(req[f], type) else req[f]}; "
                        f"saw {', '.join(ex[:3])})")
    ids = [r.get(next(iter(req))) for r in rows]
    dupes = len(ids) - len(set(ids))
    if dupes:
        problems.append(f"{feed}: {dupes} duplicate ids")
    return problems


def fetch(name: str, as_of: date, *, params: dict[str, Any] | None = None,
          strict: bool = False) -> FeedResult:
    """Run one feed and validate what it returned.

    Validation failures are attached to the result rather than raised unless
    `strict`, so `feeds doctor` can report every feed's condition in one pass
    instead of stopping at the first broken one.
    """
    _load()
    spec = _REGISTRY.get(name)
    if not spec:
        raise FeedError(f"no feed named {name!r}; registered: "
                        f"{sorted(_REGISTRY)}")
    kind: Kind = spec["kind"]
    merged = {**spec["params"], **(params or {})}
    try:
        rows = [dict(r) for r in spec["fn"](as_of, merged)]
    except Exception as e:  # noqa: BLE001
        if strict or spec["required"]:
            raise FeedError(f"feed {name!r} failed: {e}") from e
        return FeedResult(feed=name, kind=kind, as_of=as_of.isoformat(),
                          ok=False, error=f"{type(e).__name__}: {e}")

    for r in rows:                       # isolation: every row knows its period
        r.setdefault("as_of", as_of.isoformat())
        r.setdefault("feed", name)

    problems = validate(kind, rows, feed=name)
    floor = int(spec.get("expect_rows") or 0)
    if floor and len(rows) < floor:
        problems.insert(0, f"{name}: returned {len(rows)} rows, expected at least "
                           f"{floor} — the source answered but gave less than a "
                           f"period's worth of data")
    if problems and strict:
        raise FeedError("; ".join(problems))
    return FeedResult(feed=name, kind=kind, as_of=as_of.isoformat(), rows=rows,
                      ok=not problems, error="; ".join(problems) or None,
                      meta={"n": len(rows), "params": merged,
                            "recommended_missing": sorted(
                                f for f in RECOMMENDED[kind]
                                if not any(f in r for r in rows))})


def fetch_kind(kind: Kind, as_of: date, *, names: Iterable[str] | None = None,
               params: dict[str, Any] | None = None) -> tuple[list[dict], list[FeedResult]]:
    """Run every feed of a kind and concatenate. Returns (rows, per-feed results).

    Concatenation is safe precisely because the schema is enforced per feed: a new
    source becomes more rows of the same shape, which is what makes adding one a
    configuration change rather than a downstream edit.
    """
    _load()
    pick = list(names) if names else [r["name"] for r in available(kind)]
    rows: list[dict[str, Any]] = []
    results: list[FeedResult] = []
    for n in pick:
        r = fetch(n, as_of, params=params)
        results.append(r)
        rows.extend(r.rows)
    return rows, results
