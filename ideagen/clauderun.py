"""Claude-Code-in-the-loop generation: prepare prompts, replay answers.

Cloud inference is gone by instruction — it charged per token while producing
nothing usable in two whole weekly windows. The generator is now the operator's
Claude Code session, which is already paid for and demonstrably better at this.
A Python process cannot call the session synchronously, so the run splits:

  weekly-prepare   assembles the exact inputs a live run would see (corpus,
                   topics, eligible universe), builds every generator prompt with
                   the same builders the live path uses, and writes them to a
                   queue directory as numbered request files.
  <Claude answers> each request gets a `.response.json` beside it — a JSON array
                   of ideas satisfying the same contract `mint` enforces.
  weekly-complete  re-runs the weekly pipeline with the snapshotted inputs and a
                   ReplayInference that serves the recorded answers, so verdicts,
                   pool, selectors, booking and artifacts all flow through the
                   normal path. Nothing downstream knows or cares who answered.

The queue is itself lineage: prompt and answer sit side by side on disk and in
the run journal, which no API-based generation ever gave us.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from . import config, feeds, platform as plat, strategy as strat, universe as uni

QUEUE = Path(config.DATA if hasattr(config, "DATA") else "data") / "infer_queue"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def prepare(as_of: date, *, verbose: bool = True) -> dict[str, Any]:
    """Snapshot inputs and write every generator prompt to the queue."""
    log = print if verbose else (lambda *a: None)
    corpus, _ = feeds.fetch_kind("corpus", as_of)
    if not corpus:
        raise RuntimeError(f"{as_of} 窗口没有语料，无从生成")
    calendar, _ = feeds.fetch_kind("calendar", as_of)
    universe_rows, _ = feeds.fetch_kind("universe", as_of)
    universe_rows, _excl = uni.eligible(universe_rows, as_of=as_of)

    # Stage A runs here exactly as the live path would, so the prompts are built
    # against the same topics the completing run will recompute. Both scorers are
    # mechanical — no model, no cost.
    ctx = strat.RunContext(as_of=as_of, inputs_sha="prepare",
                           corpus=corpus, calendar=calendar)
    tv = strat.run("topic_scorer", "hgep", ctx)
    from .orchestrator import _topic_rows
    topics = _topic_rows(tv, as_of)

    gctx = strat.RunContext(as_of=as_of, inputs_sha="prepare", corpus=corpus,
                            calendar=calendar, topics=topics,
                            universe=universe_rows)
    qdir = QUEUE / as_of.isoformat()
    qdir.mkdir(parents=True, exist_ok=True)
    snapshot = {"as_of": as_of.isoformat(),
                "corpus_doc_ids": [c.get("doc_id") for c in corpus],
                "n_universe": len(universe_rows),
                "topics": topics}
    (qdir / "_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    # Corpus and calendar are frozen to disk so `complete` replays the exact
    # inputs even if the incremental ingest has since added documents — a prompt
    # answered against Tuesday's corpus must not be graded against Wednesday's.
    (qdir / "_corpus.json").write_text(
        json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    (qdir / "_calendar.json").write_text(
        json.dumps(calendar, ensure_ascii=False), encoding="utf-8")

    n = 0
    manifest = []
    for g in strat.available("idea_generator"):
        _mod_name = {"ai_native": "gen_ai_native", "carl_constraint": "gen_carl",
                     "chain": "gen_chain", "gap": "gen_gap"}[g["name"]]
        mod = __import__(f"ideagen.strategies.{_mod_name}",
                         fromlist=["build_prompt"])
        for t in topics:
            prompt, n_docs = mod.build_prompt(gctx, t)
            n += 1
            fid = f"{n:03d}_{g['name']}_{t['topic_id']}"
            (qdir / f"{fid}.prompt.txt").write_text(prompt, encoding="utf-8")
            manifest.append({"file": fid, "method": g["name"],
                             "topic_id": t["topic_id"], "sha": _sha(prompt),
                             "n_docs": n_docs})
    (qdir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"已写 {n} 个生成请求到 {qdir}")
    log("下一步：为每个 .prompt.txt 写同名 .response.json（想法 JSON 数组），"
        "然后运行 weekly-complete")
    return {"queue": str(qdir), "prompts": n, "topics": [t["topic_id"] for t in topics]}


class ReplayInference:
    """Serves recorded answers to the normal pipeline.

    Matches by prompt hash first; falls back to per-run FIFO when a rebuilt
    prompt differs by a byte (the fallback is counted, because silent fuzzy
    matching is how an answer ends up graded against the wrong question).
    """

    def __init__(self, qdir: Path):
        self.by_sha: dict[str, Any] = {}
        self.fifo: list[Any] = []
        self.fallbacks = 0
        man = json.loads((qdir / "_manifest.json").read_text(encoding="utf-8"))
        for m in man:
            rp = qdir / f"{m['file']}.response.json"
            if rp.exists():
                ans = rp.read_text(encoding="utf-8")
                self.by_sha[m["sha"]] = ans
                self.fifo.append(ans)

    def complete(self, prompt: str, **kw: Any):
        from .platform.base import Completion
        ans = self.by_sha.pop(_sha(prompt), None)
        if ans is not None:
            self.fifo.remove(ans)
        elif self.fifo:
            ans = self.fifo.pop(0)
            self.fallbacks += 1
        else:
            raise RuntimeError("队列里没有对应这个提示的回答（response 缺失）")
        return Completion(text=ans, model="claude-code-session")

    def complete_many(self, prompt: str, *, k: int = 5, **kw: Any):
        return [self.complete(prompt, **kw)]

    def check(self):
        from .platform.base import Health
        return Health(True, "inference",
                      f"Claude Code 队列回放（{len(self.fifo)+len(self.by_sha)} 个待用回答）")


def complete(as_of: date, *, trade: bool = True, verbose: bool = True) -> Any:
    """Run the full weekly pipeline against the queue's recorded answers."""
    qdir = QUEUE / as_of.isoformat()
    if not (qdir / "_manifest.json").exists():
        raise RuntimeError(f"{qdir} 没有 prepare 过")
    man = json.loads((qdir / "_manifest.json").read_text(encoding="utf-8"))
    missing = [m["file"] for m in man
               if not (qdir / f"{m['file']}.response.json").exists()]
    if missing:
        raise RuntimeError(f"还有 {len(missing)} 个请求没有回答，例如 {missing[:3]}")

    corpus = json.loads((qdir / "_corpus.json").read_text(encoding="utf-8"))
    calendar = json.loads((qdir / "_calendar.json").read_text(encoding="utf-8"))
    p = plat.load()
    p.inference = ReplayInference(qdir)

    from . import orchestrator
    res = orchestrator.weekly(as_of=as_of, p=p, corpus=corpus,
                              calendar=calendar, verbose=verbose)
    if res.completed and trade:
        from . import booking, db as _db
        print("\n建仓：")
        booking.book_run(_db.init(), p, res.run_id)
    if getattr(p.inference, "fallbacks", 0):
        print(f"⚠ {p.inference.fallbacks} 个回答走了顺序回退匹配（提示重建后不完全一致）")
    return res
