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

from ideagen import ask, db, schema
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
        blob, name = audit.build(self.p, RUN_ID)
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
