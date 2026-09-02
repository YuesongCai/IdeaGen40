"""Load the public synthetic POC fixture into TOS and the cloud state store.

The fixture is intentionally split across two persistence surfaces:

* TOS keeps the complete, immutable input document, addressed by SHA-256.
* RDS keeps the queryable run, feed, verdict, candidate, event and watchpoint
  rows consumed by the dashboard.

The loader is idempotent. Re-running the same fixture neither duplicates rows
nor overwrites the TOS object; an existing object is accepted only when its
bytes still match the requested fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import schema

FORMAT = "ideagen.public-poc-fixture/v1"
CLASSIFICATION = "public-synthetic"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "seed" / "poc_public_dashboard_v1.json"
TABLE_ORDER = (
    "orch_runs", "feed_runs", "verdicts", "candidates", "events", "watchpoints",
)
JSON_COLUMNS = {
    "verdicts": ("chosen", "scores", "rejected", "meta"),
    "candidates": ("payload",),
}
FORBIDDEN_SOURCE_MARKERS = ("wisburg", "olive", "nexus hk")


@dataclass(frozen=True)
class Fixture:
    path: Path
    document: dict[str, Any]
    raw: bytes
    sha256: str

    @property
    def fixture_id(self) -> str:
        return str(self.document["metadata"]["fixture_id"])

    @property
    def object_key(self) -> str:
        return f"fixtures/public/{self.fixture_id}/{self.sha256}.json"


def read(path: Path | str = DEFAULT_PATH) -> Fixture:
    """Read and validate one fixture without touching cloud services."""
    fixture_path = Path(path)
    raw = fixture_path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"fixture is not valid JSON: {e}") from e
    validate(document)
    return Fixture(
        path=fixture_path,
        document=document,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def validate(document: dict[str, Any]) -> None:
    """Reject fixtures that are incomplete, ambiguous or not public synthetic."""
    if not isinstance(document, dict):
        raise ValueError("fixture root must be an object")
    metadata = document.get("metadata")
    tables = document.get("tables")
    inputs = document.get("inputs")
    if not isinstance(metadata, dict):
        raise ValueError("fixture.metadata must be an object")
    if metadata.get("format") != FORMAT:
        raise ValueError(f"fixture metadata.format must be {FORMAT!r}")
    if metadata.get("classification") != CLASSIFICATION:
        raise ValueError(
            f"fixture metadata.classification must be {CLASSIFICATION!r}")
    if metadata.get("synthetic") is not True:
        raise ValueError("fixture metadata.synthetic must be true")
    if not metadata.get("fixture_id") or not metadata.get("generated_at"):
        raise ValueError("fixture metadata needs fixture_id and generated_at")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("corpus"), list):
        raise ValueError("fixture.inputs.corpus must be a list")
    if not inputs["corpus"]:
        raise ValueError("fixture must contain public synthetic corpus rows")
    if not isinstance(tables, dict):
        raise ValueError("fixture.tables must be an object")

    unknown = sorted(set(tables) - set(TABLE_ORDER))
    missing = sorted(set(TABLE_ORDER) - set(tables))
    if unknown:
        raise ValueError(f"fixture contains unsupported tables: {unknown}")
    if missing:
        raise ValueError(f"fixture is missing tables: {missing}")
    for table in TABLE_ORDER:
        if not isinstance(tables[table], list):
            raise ValueError(f"fixture.tables.{table} must be a list")

    if len(tables["orch_runs"]) != 1:
        raise ValueError("fixture must describe exactly one run")
    run = tables["orch_runs"][0]
    run_id = run.get("run_id")
    as_of = run.get("as_of")
    if not run_id or run.get("kind") != "weekly" or run.get("ok") != 1:
        raise ValueError("fixture run must be one successful weekly run")
    if metadata.get("as_of") != as_of:
        raise ValueError("fixture metadata.as_of must match its run")

    for table in ("feed_runs", "verdicts", "candidates"):
        if not tables[table]:
            raise ValueError(f"fixture.tables.{table} must not be empty")
        bad = [r for r in tables[table] if r.get("run_id") != run_id]
        if bad:
            raise ValueError(f"every {table} row must belong to {run_id}")
    if not any(r.get("kind") == "corpus" and int(r.get("n_rows") or 0) > 0
               for r in tables["feed_runs"]):
        raise ValueError("fixture needs a non-empty corpus feed")
    input_counts = {
        "corpus": len(inputs["corpus"]),
        "universe": len(inputs.get("universe") or []),
        "calendar": len(inputs.get("calendar") or []),
    }
    for kind, expected in input_counts.items():
        feeds = [r for r in tables["feed_runs"] if r.get("kind") == kind]
        if len(feeds) != 1 or int(feeds[0].get("n_rows") or 0) != expected:
            raise ValueError(
                f"fixture {kind} feed count must match its input rows ({expected})")

    candidates = {str(r.get("candidate_id")) for r in tables["candidates"]}
    if "" in candidates or "None" in candidates:
        raise ValueError("every candidate needs candidate_id")
    selectors = [r for r in tables["verdicts"]
                 if r.get("kind") == "idea_selector"]
    topics = [r for r in tables["verdicts"]
              if r.get("kind") == "topic_scorer"]
    if not selectors or not topics:
        raise ValueError("fixture needs topic_scorer and idea_selector verdicts")
    for verdict in selectors:
        chosen = verdict.get("chosen")
        if not isinstance(chosen, list) or not set(map(str, chosen)) <= candidates:
            raise ValueError(
                f"selector {verdict.get('strategy')} chooses unknown candidates")

    # A public fixture must not accidentally become a credential transport.
    text = json.dumps(document, ensure_ascii=False)
    lowered = text.lower()
    for marker in FORBIDDEN_SOURCE_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"public fixture contains partner-source marker: {marker}")
    for label, pattern in schema.PATTERNS:
        if re.search(pattern, text):
            raise ValueError(f"fixture contains credential-shaped content: {label}")


def publish(blobs: Any, fixture: Fixture) -> str:
    """Write the fixture once, or verify an identical existing TOS object."""
    key = fixture.object_key
    if blobs.exists(key):
        existing = blobs.get(key)
        if existing != fixture.raw:
            raise RuntimeError(
                f"immutable fixture object {key} exists with different bytes")
        return blobs.uri(key)
    return blobs.put(
        key,
        fixture.raw,
        content_type="application/json; charset=utf-8",
        metadata={
            "fixture_id": fixture.fixture_id,
            "sha256": fixture.sha256,
            "classification": CLASSIFICATION,
        },
    )


def write_state(state: Any, fixture: Fixture) -> dict[str, int]:
    """Idempotently write all six query tables and verify their row counts."""
    schema.migrate(state)
    rows_by_table = deepcopy(fixture.document["tables"])
    for row in rows_by_table["orch_runs"]:
        row["inputs_sha"] = fixture.sha256
        row["data_classification"] = CLASSIFICATION
    for row in rows_by_table["feed_runs"]:
        row["rows_sha"] = fixture.sha256

    for table, columns in JSON_COLUMNS.items():
        for row in rows_by_table[table]:
            for column in columns:
                value = row.get(column)
                if not isinstance(value, str):
                    row[column] = json.dumps(
                        value, ensure_ascii=False, separators=(",", ":"),
                        sort_keys=True, allow_nan=False)

    with state.tx():
        for table in TABLE_ORDER:
            for row in rows_by_table[table]:
                schema.upsert(state, table, row)

    run_id = rows_by_table["orch_runs"][0]["run_id"]
    counts: dict[str, int] = {}
    for table in ("orch_runs", "feed_runs", "verdicts", "candidates"):
        counts[table] = int(state.q(
            f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=?", (run_id,)
        )[0]["n"])
        if counts[table] != len(rows_by_table[table]):
            raise RuntimeError(
                f"{table} read-back mismatch: expected "
                f"{len(rows_by_table[table])}, got {counts[table]}")
    for table, id_column in (("events", "event_id"), ("watchpoints", "watch_id")):
        ids = [str(row[id_column]) for row in rows_by_table[table]]
        counts[table] = sum(int(state.q(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {id_column}=?", (item_id,)
        )[0]["n"]) for item_id in ids)
        if counts[table] != len(ids):
            raise RuntimeError(
                f"{table} read-back mismatch: expected {len(ids)}, "
                f"got {counts[table]}")
    return counts


def load(platform: Any, path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    """Publish one fixture, write it to state, and return a non-secret receipt."""
    fixture = read(path)
    if getattr(platform.state, "dialect", "") != "mysql":
        raise RuntimeError(
            "public POC fixture import requires IDEAGEN_STATE_ENGINE=mysql")
    uri = publish(platform.blobs, fixture)
    counts = write_state(platform.state, fixture)
    run_id = fixture.document["tables"]["orch_runs"][0]["run_id"]
    return {
        "fixture_id": fixture.fixture_id,
        "classification": CLASSIFICATION,
        "sha256": fixture.sha256,
        "artifact_uri": uri,
        "run_id": run_id,
        "rows": counts,
    }
