"""Tests for 「问当时的它」 (ideagen/ask.py).

The two invariants that make the feature honest, and therefore the two that get
tests: every assembled piece of context must carry its provenance (which
artifact / table / doc it came from), and nothing leaving the server may carry
machine identity (host names, bucket names, home paths) — the same scrubbing
discipline as /api/journal.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

# Same import-time defaults as test_core: this module sorts first, so it is the
# one that imports `ideagen.config` — which freezes these values — before
# test_core's own setdefault lines get a chance to run.
os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen import ask, db, review, schema
from ideagen.platform import Platform, Unavailable
from ideagen.platform.local import (FileCache, FileEventBus, LocalBlobStore,
                                    SqliteStateStore)

RUN_ID = "20260825T000000Z-testtest"
AS_OF = "2026-08-26"


class _FrozenRun:
    """A miniature platform with one frozen run, built entirely in tmpdirs.

    Shared by the ask tests and the audit-bundle tests rather than inherited
    between them: subclassing one test case from the other would re-run its
    assertions under a second name and report every failure twice.
    """

    def setUp(self):
        # The proposal index is a process-level cache keyed by run id. Real
        # runs never reuse an id, but every test here rebuilds the same one on
        # a fresh platform, so a leftover index would answer from the previous
        # test's blobs.
        review._PROPOSAL_INDEX.clear()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.p = Platform(
            name="test",
            blobs=LocalBlobStore(root / "blobs"),
            state=SqliteStateStore(root / "state.db"),
            inference=Unavailable("inference", "test node"),
            events=FileEventBus(root / "events.jsonl"),
            cache=FileCache(root / "cache"),
            secrets=Unavailable("secrets", "test node"),
        )
        schema.migrate(self.p.state)
        schema.upsert(self.p.state, "orch_runs", {
            "run_id": RUN_ID, "as_of": AS_OF, "kind": "weekly",
            "platform": "test", "started_at": "2026-08-25T00:00:00+00:00",
            "ended_at": "2026-08-25T00:00:09+00:00", "ok": 1, "error": None,
            "inputs_sha": None, "journal_uri": None, "calls": 3,
            "data_classification": "live"})
        pre = f"runs/{AS_OF}/{RUN_ID}"
        # The journal deliberately carries the three things that must never
        # leave the server: a host name, a bucket URI, and a home path.
        self.p.blobs.put(f"{pre}/journal.json", json.dumps({
            "run_id": RUN_ID, "as_of": AS_OF, "ok": True, "duration_s": 9.1,
            "host": "operator-macbook.local",
            "steps": [
                {"n": 1, "step": "inputs", "at": "2026-08-25T00:00:01+00:00",
                 "corpus": 2, "note": "cached at /Users/operator/IdeaGen40"},
                {"n": 2, "step": "topics", "at": "2026-08-25T00:00:02+00:00",
                 "chosen": ["T-TEST"],
                 "uri": "tos://ideagen-1234567890/runs/x"},
            ],
            # The structure that actually leaked once: display needs
            # name/ok/detail, and `meta` carries the bucket name with the
            # cloud account id inside it.
            "port_health": [
                {"name": "blobs", "ok": True, "detail": "object store ok",
                 "meta": {"bucket": "ideagen-1234567890",
                          "root": "/Users/operator/blobs"}},
            ],
            "artifacts": []}).encode())
        self.p.blobs.put(f"{pre}/A_topics.json", json.dumps({
            "as_of": AS_OF, "kind": "topic_scorer", "strategy": "hgep",
            "version": "1.0", "chosen": ["T-TEST"],
            "scores": {"T-TEST": {
                "label": "测试主题", "score": 61.0, "H": 70.0, "G": 50.0,
                "E": 50.0, "P": 50.0, "n_evidence": 2, "n_institutions": 2,
                "indicator": "US.TEST", "p_source": "neutral_default"}},
            "rejected": {}, "meta": {"weights": {}, "registered_topics": 1,
                                     "topics_with_evidence": 1,
                                     "loudest_count": 2, "top_n": 5},
        }).encode())
        self.p.blobs.put(f"{pre}/C_selectors/test_sel.json", json.dumps({
            "as_of": AS_OF, "kind": "idea_selector", "strategy": "test_sel",
            "version": "1.0", "chosen": ["pool:AAA"],
            "scores": {"pool:AAA": {"omega": 2.0}},
            "rejected": {"pool:BBB": "赚亏比不够"},
            "meta": {"blob_root": "tos://ideagen-1234567890/prod"},
        }).encode())
        self.p.blobs.put(f"{pre}/B_pool.json", json.dumps([
            {"id": "pool:AAA", "topic_id": "T-TEST", "upside_pct": 4.0,
             "downside_pct": -2.0, "p_up": 0.5, "p_down": 0.2,
             "proposed_by": ["chain"], "thesis": "测试论点"},
        ]).encode())
        self.con = db.init(":memory:")
        for i, d in enumerate(("2026-08-26", "2026-08-25")):
            db.upsert(self.con, "documents", {
                "doc_id": f"feed:{i}", "line": "feed", "tier": 1,
                "title": f"测试研报 {i}", "institution": "TestBank",
                "published_d": d, "ingested_at": "2026-08-24T00:00:00+00:00",
                "summary": "存放于 /Users/operator/data 的样例摘要，"
                           "备份在 tos://ideagen-1234567890/docs",
                "body": "", "content_hash": "abc", "retrieval": "test",
            }, ["doc_id"])

    def tearDown(self):
        self.con.close()
        self.p.state.connection.close()
        self.tmp.cleanup()

    def _ctx(self, kind, sid):
        return ask.assemble_context(self.p, self.con, RUN_ID,
                                    {"kind": kind, "id": sid})


class AskContextCase(_FrozenRun, unittest.TestCase):
    """Provenance and scrubbing on the 「问当时的它」 context path."""

    def test_every_material_carries_provenance(self):
        ctx = self._ctx("topic", "T-TEST")
        self.assertNotIn("error", ctx)
        mats = ctx["materials"]
        self.assertGreaterEqual(len(mats), 2)
        for m in mats:
            for field in ("id", "kind", "title", "source", "text"):
                self.assertTrue(m.get(field), f"material missing {field}: {m}")
        # the provenance list mirrors the materials one-to-one, minus the text
        self.assertEqual([m["id"] for m in mats],
                         [m["id"] for m in ctx["provenance"]])
        sources = " ".join(m["source"] for m in mats)
        self.assertIn(f"runs/{AS_OF}/{RUN_ID}/journal.json", sources)
        self.assertIn(f"runs/{AS_OF}/{RUN_ID}/A_topics.json", sources)

    def test_selector_context_names_artifact_and_pool(self):
        ctx = self._ctx("selector", "test_sel")
        sources = " ".join(m["source"] for m in ctx["materials"])
        self.assertIn(f"runs/{AS_OF}/{RUN_ID}/C_selectors/test_sel.json",
                      sources)
        self.assertIn(f"runs/{AS_OF}/{RUN_ID}/B_pool.json", sources)
        texts = " ".join(m["text"] for m in ctx["materials"])
        self.assertIn("pool:AAA", texts)
        self.assertIn("赚亏比不够", texts)   # rejection reasons are material

    def test_scrubbing_no_host_bucket_or_home_path(self):
        for kind, sid in (("topic", "T-TEST"), ("selector", "test_sel")):
            ctx = self._ctx(kind, sid)
            blob = json.dumps(ctx, ensure_ascii=False)
            self.assertNotIn("operator-macbook", blob)   # journal host dropped
            self.assertNotIn("/Users/", blob)            # home paths → ~
            self.assertNotIn("ideagen-1234567890", blob)  # bucket → <bucket>
            self.assertIn("tos://<bucket>", blob)

    def test_unknown_subject_and_missing_run_are_honest_errors(self):
        self.assertIn("error", ask.assemble_context(
            self.p, self.con, RUN_ID, {"kind": "vibe", "id": "x"}))
        self.assertIn("error", ask.assemble_context(
            self.p, self.con, "no-such-run", {"kind": "topic", "id": "T"}))

    def test_answer_refuses_honestly_without_inference(self):
        ctx = self._ctx("topic", "T-TEST")
        with self.assertRaises(RuntimeError) as caught:
            ask.answer(self.p, "为什么选它？", ctx, [])
        self.assertIn(ask.UNAVAILABLE_MSG, str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class SelectionContextCase(_FrozenRun, unittest.TestCase):
    """The whole selection step, not one topic at a time.

    "You read hundreds of reports — why these five?" cannot be answered well
    from a single topic's context: the model would have to infer the other
    rows, which is how a reconstructed answer gets produced. The context for
    this subject must therefore contain the full table and the control arm.
    """

    def test_context_carries_every_scored_topic_and_the_control_arm(self):
        ctx = self._ctx("selection", "topics")
        self.assertNotIn("error", ctx)
        kinds = {m["kind"] for m in ctx["materials"]}
        self.assertIn("verdict", kinds)
        verdict = next(m for m in ctx["materials"] if m["kind"] == "verdict")
        self.assertIn("T-TEST", verdict["text"])
        self.assertIn("入选", verdict["text"])
        # Every material still names where it came from.
        for m in ctx["materials"]:
            self.assertTrue(m["source"], m)

    def test_the_step_context_does_not_smuggle_in_report_bodies(self):
        """The step's decision was made on the table; adding bodies invites
        the model to answer from the reports instead of from the decision."""
        ctx = self._ctx("selection", "topics")
        self.assertEqual(0, sum(1 for m in ctx["materials"]
                                if m["kind"] == "doc"))
        self.assertTrue(ctx["notes"])

    def test_selection_is_an_accepted_subject_kind(self):
        self.assertIn("selection", ask.SUBJECT_KINDS)
        bad = self._ctx("nonsense", "topics")
        self.assertIn("error", bad)


class ProposalsCase(_FrozenRun, unittest.TestCase):
    """The proposals behind one merged candidate.

    The pool row is a merge: one thesis, median odds. That is the right unit
    to select on and the wrong one to answer "how did this idea come about",
    because the merge is exactly where four methods' different arguments stop
    being visible. These read them back out of the run's own artifacts.
    """

    def setUp(self):
        super().setUp()
        pre = f"runs/{AS_OF}/{RUN_ID}"
        for method, thesis, cite in (
                ("ai_native", "端到端的理由", "feed:0"),
                ("chain", "传导链的理由", "feed:1")):
            self.p.blobs.put(f"{pre}/B_generators/{method}.json", json.dumps({
                "as_of": AS_OF, "kind": "idea_generator", "strategy": method,
                "version": "1.0", "produced": [
                    {"id": f"{method}:T-TEST:AAA", "instrument_id": "AAA",
                     "instrument_name": "A Fund", "method": method,
                     "topic_id": "T-TEST", "thesis": thesis,
                     "upside_pct": 4.0, "downside_pct": -2.0,
                     "p_up": 0.5, "p_base": 0.3, "p_down": 0.2,
                     "citations": [cite], "bad_citations": 0},
                    {"id": f"{method}:T-TEST:ZZZ", "instrument_id": "ZZZ",
                     "topic_id": "T-TEST", "thesis": "别的标的",
                     "citations": []},
                ]}).encode())

    def test_returns_every_method_that_proposed_the_instrument(self):
        out = review.proposals_for("AAA", RUN_ID, p=self.p, con=self.con)
        self.assertEqual(2, out["n"])
        self.assertEqual({"ai_native", "chain"},
                         {x["method"] for x in out["proposals"]})
        self.assertEqual({"端到端的理由", "传导链的理由"},
                         {x["thesis"] for x in out["proposals"]})

    def test_citations_resolve_to_titles_so_the_chain_is_clickable(self):
        out = review.proposals_for("AAA", RUN_ID, p=self.p, con=self.con)
        self.assertIn("feed:0", out["docs"])
        self.assertTrue(out["docs"]["feed:0"]["title"])

    def test_a_prefixed_code_finds_the_same_instrument(self):
        """Positions carry `US.AAA`; the pool carries `AAA`."""
        self.assertEqual(2, review.proposals_for(
            "US.AAA", RUN_ID, p=self.p, con=self.con)["n"])

    def test_an_instrument_nobody_proposed_is_empty_not_an_error(self):
        out = review.proposals_for("NOPE", RUN_ID, p=self.p, con=self.con)
        self.assertEqual(0, out["n"])
        self.assertNotIn("error", out)

    def test_a_blank_code_is_refused(self):
        self.assertIn("error", review.proposals_for(
            "", RUN_ID, p=self.p, con=self.con))

    def test_the_run_is_read_once_however_many_instruments_are_asked_for(self):
        """Reading four artifacts out of object storage took four seconds; a
        reader opens this drawer once per instrument they are curious about."""
        reads = []
        real_get = self.p.blobs.get

        def counting_get(key):
            reads.append(key)
            return real_get(key)

        self.p.blobs.get = counting_get           # type: ignore[method-assign]
        try:
            review.proposals_for("AAA", RUN_ID, p=self.p, con=self.con)
            after_first = len([k for k in reads if "B_generators" in k])
            self.assertGreater(after_first, 0)
            for code in ("ZZZ", "AAA", "NOPE"):
                review.proposals_for(code, RUN_ID, p=self.p, con=self.con)
            self.assertEqual(after_first,
                             len([k for k in reads if "B_generators" in k]))
        finally:
            self.p.blobs.get = real_get           # type: ignore[method-assign]


class AskLogCase(unittest.TestCase):
    """The session record: what was asked, and what the model was handed.

    An answer that cites three of fifty-four materials is a different fact
    from one where only three existed, so the log keeps the whole material
    list rather than just the cited subset — that difference is the only way
    to tell a grounded answer from a lucky one after the fact.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = ask.ASK_LOG
        ask.ASK_LOG = Path(self.tmp.name) / "ask_log.jsonl"

    def tearDown(self):
        ask.ASK_LOG = self._orig
        self.tmp.cleanup()

    def test_missing_log_is_empty_not_an_error(self):
        self.assertEqual([], ask.recent_asks())

    def test_newest_first_and_filtered_by_run(self):
        for i, run in enumerate(["r1", "r1", "r2"]):
            ask.log_ask({"run_id": run, "kind": "topic", "id": f"T{i}",
                         "question": f"q{i}", "answer": "a",
                         "cited": ["M1"], "provenance": [
                             {"id": "M1", "kind": "verdict", "title": "t",
                              "source": "blob x"}]})
        allrows = ask.recent_asks()
        self.assertEqual(["T2", "T1", "T0"], [r["id"] for r in allrows])
        self.assertEqual(["T1", "T0"],
                         [r["id"] for r in ask.recent_asks(run_id="r1")])

    def test_provenance_survives_and_is_scrubbed(self):
        ask.log_ask({"run_id": "r1", "kind": "topic", "id": "T",
                     "question": "q", "answer": "见 /Users/operator/x",
                     "cited": [], "provenance": [
                         {"id": "M1", "kind": "verdict", "title": "t",
                          "source": "tos://ideagen-1234567890/runs/x"}]})
        row = ask.recent_asks()[0]
        self.assertEqual(1, len(row["provenance"]))
        self.assertNotIn("ideagen-1234567890", row["provenance"][0]["source"])
        self.assertNotIn("/Users/operator", row["answer"])

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        ask.log_ask({"run_id": "r1", "id": "good", "question": "q",
                     "answer": "a"})
        with ask.ASK_LOG.open("a", encoding="utf-8") as f:
            f.write("{not json\n\n")
        ask.log_ask({"run_id": "r1", "id": "later", "question": "q",
                     "answer": "a"})
        self.assertEqual(["later", "good"],
                         [r["id"] for r in ask.recent_asks()])


class AuditBundleCase(_FrozenRun, unittest.TestCase):
    """The downloadable audit bundle carries the run, not the machine.

    The bundle was born after /api/journal, and it was born leaking: the
    journal handler stripped `port_health[].meta` inline, so the second
    endpoint to serve a journal did not. The scrubbing now lives in
    `ask.scrub_journal`, and these tests are what keep the next outbound path
    from repeating it.
    """

    def _bundle(self):
        import io
        import zipfile

        from ideagen import audit
        blob, name = audit.build(self.p, RUN_ID, con=self.con)
        self.assertIsNotNone(blob, name)
        return zipfile.ZipFile(io.BytesIO(blob)), name

    def test_bundle_holds_the_whole_run(self):
        z, name = self._bundle()
        names = z.namelist()
        self.assertIn("README.md", names)
        self.assertIn("manifest.json", names)
        self.assertTrue(any(n.startswith("01_") for n in names), names)
        self.assertTrue(any(n.startswith("02_") for n in names), names)
        self.assertTrue(any(n.startswith("04_") for n in names), names)
        self.assertTrue(any(n.startswith("05_") for n in names), names)
        self.assertIn(AS_OF, name)

    def test_manifest_checksums_match_the_files(self):
        import hashlib
        import json as _json
        z, _ = self._bundle()
        manifest = _json.loads(z.read("manifest.json"))
        self.assertTrue(manifest["exported_files"])
        for entry in manifest["exported_files"]:
            body = z.read(entry["name"])
            self.assertEqual(len(body), entry["bytes"], entry["name"])
            self.assertEqual(hashlib.sha256(body).hexdigest(),
                             entry["sha256"], entry["name"])

    def test_nothing_in_the_bundle_names_the_machine(self):
        z, _ = self._bundle()
        for member in z.namelist():
            text = z.read(member).decode("utf-8", "replace")
            for secret in ("ideagen-1234567890", "/Users/operator",
                           "operator-macbook.local"):
                self.assertNotIn(secret, text, f"{secret} leaked in {member}")

    def test_corpus_manifest_carries_receipts_but_not_bodies(self):
        """What the run fetched and how — the part that makes a citation
        checkable offline — without the licensed text itself."""
        import json as _json
        z, _ = self._bundle()
        name = next((n for n in z.namelist() if n.startswith("07_")), None)
        self.assertIsNotNone(name, z.namelist())
        rows = [_json.loads(l) for l in
                z.read(name).decode().strip().split("\n") if l]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("retrieval", r)
            self.assertIn("content_hash", r)
            self.assertIn("body_len", r)
            self.assertNotIn("body", r)

    def test_a_citation_resolves_inside_the_bundle(self):
        """The point of shipping the manifest: no live system needed to check
        that an idea's source exists and which call pulled it."""
        import json as _json
        z, _ = self._bundle()
        manifest = {
            _json.loads(l)["doc_id"]: _json.loads(l)
            for l in z.read(next(n for n in z.namelist()
                                 if n.startswith("07_"))).decode().strip().split("\n")
            if l}
        self.assertIn("feed:0", manifest)
        self.assertTrue(manifest["feed:0"]["title"])

    def test_port_health_survives_without_its_meta(self):
        """Stripping identity must not cost the reader the health readout."""
        import json as _json
        z, _ = self._bundle()
        journal = _json.loads(
            z.read(next(n for n in z.namelist() if n.startswith("01_"))))
        health = journal["port_health"]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["name"], "blobs")
        self.assertTrue(health[0]["ok"])
        self.assertIn("detail", health[0])
        self.assertNotIn("meta", health[0])
