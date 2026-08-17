"""Strategy registry: the investment logic is a plugin, the system is not.

The methodology is still moving — which factors score a topic, how candidates get
picked, how tight the stops are. If those live as hardcoded module functions,
every iteration edits the pipeline, and every edit risks the invariants that took
longest to get right (as-of clamping, cash constraints, mark ordering).

So strategies are registered plugins behind three narrow protocols, one per stage:

    TopicScorer    corpus            ->  5 ranked topics       (筛选A)
    IdeaGenerator  topic + universe  ->  20 ideas per topic    (筛选B)
    IdeaSelector   ideas             ->  10 held               (筛选C)

Adding, replacing or running four generators side by side is a registration, not a
refactor. Nothing in the pipeline imports a strategy by name; it asks the registry.

The three stages exist because they fail differently and therefore have to be
measured separately. A bad month can come from picking the wrong themes, from
expressing the right theme through the wrong instrument, or from holding the wrong
ten of twenty defensible ideas. Collapsing them into one score would make those
indistinguishable, and each is fixed by changing a different thing.

Three properties the registry enforces, each of which is what makes swapping
strategies safe rather than merely convenient:

**Every strategy declares a version, and results are stamped with it.** A stored
score whose producing logic cannot be identified is not auditable. `Verdict`
carries `strategy` and `version` so a book can always be traced to the exact
logic that filled it.

**Strategies are pure with respect to the run.** They receive a context and return
a verdict; they do not write to the database, place orders or read the clock.
Persistence and execution belong to the orchestrator, which is why ten selectors
can score the same candidates with no risk of one of them trading.

**Inputs are hashed.** Two selectors given the same candidates must be comparable,
so the context carries `inputs_sha`. If two verdicts disagree on that hash they
were not looking at the same thing and must not be compared.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable, Iterable, Literal, Protocol, runtime_checkable

Kind = Literal["topic_scorer", "idea_generator", "idea_selector"]

#: Stage order. The orchestrator walks this; nothing else encodes the sequence.
STAGES: tuple[Kind, ...] = ("topic_scorer", "idea_generator", "idea_selector")

#: Human labels, used in artifacts and in anything a person reads.
STAGE_LABEL: dict[Kind, str] = {
    "topic_scorer":   "筛选A · 主题",
    "idea_generator": "筛选B · 出想法",
    "idea_selector":  "筛选C · 定持仓",
}


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunContext:
    """Everything a strategy may see. Deliberately narrow.

    No database handle and no network: a strategy that can query is a strategy
    that can accidentally read the future. Whatever it needs must be assembled by
    the orchestrator, as of the run date, and passed in here — which also means
    the same context can be replayed months later and produce the same verdict.
    """
    as_of: date
    inputs_sha: str
    corpus: list[dict[str, Any]] = field(default_factory=list)
    topics: list[dict[str, Any]] = field(default_factory=list)     # 筛选A output
    universe: list[dict[str, Any]] = field(default_factory=list)   # what is buyable
    candidates: list[dict[str, Any]] = field(default_factory=list) # 筛选B output
    prices: dict[str, Any] = field(default_factory=dict)
    calendar: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    infer: Any = None            # platform.Inference, or None for mechanical-only

    def with_(self, **kw: Any) -> "RunContext":
        """A copy with fields replaced, for handing stage B's output to stage C.

        The context is frozen so a strategy cannot mutate what the next one sees;
        advancing a stage therefore means deriving a new context, and `inputs_sha`
        is expected to change with it — stage C's comparison is paired on stage C's
        inputs, not on stage B's.
        """
        return replace(self, **kw)

    @staticmethod
    def sha(*parts: Any) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(json.dumps(p, ensure_ascii=False, sort_keys=True,
                                default=str).encode())
        return h.hexdigest()[:16]


@dataclass
class Verdict:
    """What a strategy returns. One shape for all three stages.

    `chosen` is the ordered result — topic ids, or idea ids. `scores` carries the
    per-item detail that makes the choice explainable, and `rejected` records what
    was dropped and why, because a strategy that cannot say what it discarded
    cannot be evaluated against the counterfactual.

    `produced` is what makes stage B fit the same type as A and C. A generator does
    not choose among things that already exist; it writes new ideas. So it fills
    `produced` with the idea objects and `chosen` with their ids, and the
    orchestrator hands `produced` forward as the next stage's candidates. One type
    across three stages is what lets a single registry, a single persistence path
    and a single comparison harness serve all of them.
    """
    strategy: str
    version: str
    chosen: list[str] = field(default_factory=list)
    produced: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    calls: int = 0                # model calls made, for cost accounting

    def as_row(self, ctx: RunContext, kind: Kind) -> dict[str, Any]:
        return {
            "as_of": ctx.as_of.isoformat(), "kind": kind,
            "stage": STAGE_LABEL.get(kind, kind),
            "strategy": self.strategy, "version": self.version,
            "inputs_sha": ctx.inputs_sha,
            "chosen": self.chosen, "produced": self.produced,
            "scores": self.scores,
            "rejected": self.rejected, "meta": self.meta, "calls": self.calls,
        }


@runtime_checkable
class Strategy(Protocol):
    name: str
    version: str
    kind: Kind

    def __call__(self, ctx: RunContext) -> Verdict: ...


# ---------------------------------------------------------------------------
class StrategyError(RuntimeError):
    pass


_REGISTRY: dict[tuple[Kind, str], dict[str, Any]] = {}
_LOADED = False


def register(kind: Kind, name: str, version: str, *,
             label: str = "", role: str = "exploratory",
             needs_model: bool | None = None,
             params: dict[str, Any] | None = None) -> Callable:
    """Decorate a function to make it available as a strategy.

    `role` distinguishes the arms whose comparison is pre-declared from the ones
    that only generate hypotheses. It is metadata rather than behaviour, but it
    has to live with the strategy: nine simultaneous comparisons against a control
    carry a ~37% chance of at least one false positive, so which comparisons are
    formal must be fixed in advance rather than chosen after seeing results.

    `needs_model` states whether this strategy calls inference. It is declared here
    rather than passed at the call site because only the strategy knows, and the
    consequence of the orchestrator guessing wrong is expensive in one direction: a
    run that assumes no model is needed starts, fetches every feed, scores the
    topics, and only then discovers that all four generators cannot run. Defaults to
    True for generators — a generator that does not call a model is the exception —
    and False otherwise.
    """
    def deco(fn: Callable[[RunContext], Verdict]):
        key = (kind, name)
        if key in _REGISTRY:
            raise StrategyError(f"{kind} {name!r} is already registered by "
                                f"{_REGISTRY[key]['module']}")
        fn.name, fn.version, fn.kind = name, version, kind      # type: ignore[attr-defined]
        _REGISTRY[key] = {"fn": fn, "name": name, "version": version, "kind": kind,
                          "label": label or name, "role": role,
                          "needs_model": (kind == "idea_generator"
                                          if needs_model is None else needs_model),
                          "params": params or {}, "module": fn.__module__,
                          "doc": (fn.__doc__ or "").strip().split("\n")[0]}
        return fn
    return deco


def needs_model(pairs: Iterable[tuple[Kind, str]]) -> list[str]:
    """Which of these strategies require inference. Empty means a mechanical run."""
    _load_plugins()
    out = []
    for kind, name in pairs:
        hit = _REGISTRY.get((kind, name))
        if hit and hit.get("needs_model"):
            out.append(f"{kind}:{name}")
    return out


def _load_plugins() -> None:
    """Import every module under `ideagen.strategies` so decorators run.

    Discovery by package scan rather than an explicit list: adding a strategy
    should be dropping in a file, which is the whole point of the registry.
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        from . import strategies as pkg
    except ImportError:
        return
    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{m.name}")


def available(kind: Kind | None = None) -> list[dict[str, Any]]:
    _load_plugins()
    out = [dict(v) for (k, _), v in _REGISTRY.items() if kind is None or k == kind]
    out.sort(key=lambda r: (r["kind"], r["role"] != "control",
                            r["role"] != "primary", r["name"]))
    for r in out:
        r.pop("fn", None)
    return out


def get(kind: Kind, name: str) -> Callable[[RunContext], Verdict]:
    _load_plugins()
    hit = _REGISTRY.get((kind, name))
    if not hit:
        known = sorted(n for (k, n) in _REGISTRY if k == kind)
        raise StrategyError(f"no {kind} named {name!r}; registered: {known}")
    return hit["fn"]


def spec(kind: Kind, name: str) -> dict[str, Any]:
    _load_plugins()
    hit = _REGISTRY.get((kind, name))
    if not hit:
        raise StrategyError(f"no {kind} named {name!r}")
    return {k: v for k, v in hit.items() if k != "fn"}


def run(kind: Kind, name: str, ctx: RunContext) -> Verdict:
    """Run one strategy, with the guarantees the registry exists to provide."""
    fn = get(kind, name)
    sp = spec(kind, name)

    # The parameters a strategy declared at registration are merged in beneath
    # whatever the run passed. Without this, a declared default is decoration: the
    # strategy has to repeat it as a `.get(key, fallback)` on the other side, and
    # the two can disagree silently — the declared value says one thing while the
    # code does another, and only the code runs. Run-supplied params still win, so
    # a sweep can override.
    if sp.get("params"):
        ctx = ctx.with_(params={**sp["params"], **(ctx.params or {})})

    v = fn(ctx)
    if not isinstance(v, Verdict):
        raise StrategyError(f"{kind} {name!r} returned {type(v).__name__}, "
                            f"expected Verdict")
    # A strategy that mislabels itself makes every stored score unattributable,
    # so the registry overwrites rather than trusts.
    v.strategy, v.version = name, spec(kind, name)["version"]

    if kind == "idea_selector" and ctx.candidates:
        unknown = set(v.chosen) - {c.get("id") for c in ctx.candidates}
        if unknown:
            raise StrategyError(f"{name!r} chose ids not in the candidate set: "
                                f"{sorted(unknown)[:5]}")
    elif kind == "idea_generator":
        _check_produced(name, v, ctx)
    return v


#: Every generated idea must carry these. An idea missing any one of them cannot
#: be traded, marked, or judged a month later, so it is rejected at the boundary
#: rather than after it has already reached a book.
IDEA_FIELDS = ("id", "instrument_id", "topic_id", "thesis",
               "upside_pct", "downside_pct", "p_up", "p_base", "p_down")


def _check_produced(name: str, v: Verdict, ctx: RunContext) -> None:
    """Validate a generator's output before it becomes the next stage's input.

    A generator is the one stage where a model writes objects rather than ranking
    them, which is exactly where a plausible-looking but unusable idea can enter —
    an instrument that is not on the shelf, probabilities that do not sum, a
    thesis attached to no topic. Downstream, every one of those turns into a
    position that cannot be filled or an outcome that cannot be attributed, so
    the check belongs here, at the moment of creation.
    """
    v.chosen = v.chosen or [str(i.get("id")) for i in v.produced]
    if set(v.chosen) != {str(i.get("id")) for i in v.produced}:
        raise StrategyError(f"{name!r}: chosen ids and produced ideas disagree")

    buyable = {str(u.get("instrument_id")) for u in (ctx.universe or [])}
    topics = {str(t.get("topic_id") if isinstance(t, dict) else t)
              for t in (ctx.topics or [])}
    bad: list[str] = []
    for i in v.produced:
        miss = [f for f in IDEA_FIELDS if i.get(f) in (None, "")]
        if miss:
            bad.append(f"{i.get('id')}: missing {miss}")
            continue
        ps = (i["p_up"], i["p_base"], i["p_down"])
        if abs(sum(float(x) for x in ps) - 1.0) > 0.02:
            bad.append(f"{i['id']}: probabilities sum to {sum(map(float, ps)):.3f}")
        if float(i["upside_pct"]) <= 0 or float(i["downside_pct"]) >= 0:
            bad.append(f"{i['id']}: upside must be positive and downside negative")
        if buyable and str(i["instrument_id"]) not in buyable:
            bad.append(f"{i['id']}: {i['instrument_id']} is not in the universe")
        if topics and str(i["topic_id"]) not in topics:
            bad.append(f"{i['id']}: topic {i['topic_id']} was not selected")
    if bad:
        raise StrategyError(f"{name!r} produced {len(bad)} unusable ideas: "
                            + "; ".join(bad[:4]))


def run_all(kind: Kind, ctx: RunContext, *,
            names: Iterable[str] | None = None) -> dict[str, Verdict]:
    """Run several strategies over one context.

    The shared context is what makes the comparison paired: every selector sees
    the same candidates in the same week, so the common market move cancels when
    their results are differenced. Independent samples would need roughly five
    times the history to separate them.
    """
    _load_plugins()
    pick = list(names) if names else [r["name"] for r in available(kind)]
    out: dict[str, Verdict] = {}
    for n in pick:
        try:
            out[n] = run(kind, n, ctx)
        except Exception as e:  # noqa: BLE001 — one bad strategy must not lose the rest
            out[n] = Verdict(strategy=n, version=spec(kind, n).get("version", "?"),
                             meta={"error": f"{type(e).__name__}: {e}"})
    return out
