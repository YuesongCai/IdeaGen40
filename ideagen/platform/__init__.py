"""Platform wiring: one switch decides where the system runs.

    IDEAGEN_PLATFORM=local      filesystem + SQLite + direct API      (default)
    IDEAGEN_PLATFORM=byteplus   TOS + RDS PG + ModelArk + MQ + Redis + KMS

Every other module imports `platform.load()` and never a vendor SDK, so the
pipeline has no idea which cloud it is on. That is what makes the BytePlus move a
configuration change rather than a rewrite, and what keeps the whole test suite
runnable with none of the cloud packages installed.

Ports can be mixed. `IDEAGEN_PLATFORM=byteplus` with no `IDEAGEN_PG_DSN` keeps
SQLite while moving artifacts to TOS, which is the actual migration path: move
one port at a time and keep the others working.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import (BlobStore, Cache, Completion, EventBus, Health, Inference,
                   NotConfigured, Platform, PlatformError, SecretStore,
                   StateStore, Unavailable, port_or_unavailable)

__all__ = ["load", "Platform", "Health", "RunJournal", "NotConfigured",
           "PlatformError", "BlobStore", "StateStore", "Inference", "EventBus",
           "Cache", "SecretStore", "Completion", "env_report", "Unavailable"]

#: Everything the platform reads, in one place so `env_report` can show operators
#: exactly what is and is not set without hunting through the code.
ENV_KEYS = {
    "IDEAGEN_PLATFORM": "local | byteplus",
    "BYTEPLUS_ACCESS_KEY": "BytePlus AK — TOS and KMS",
    "BYTEPLUS_SECRET_KEY": "BytePlus SK",
    "BYTEPLUS_REGION": "default ap-southeast-1",
    "IDEAGEN_TOS_BUCKET": "artifact bucket",
    "IDEAGEN_TOS_PREFIX": "key prefix inside the bucket, e.g. prod / staging",
    "IDEAGEN_PG_DSN": "RDS for PostgreSQL DSN; unset keeps SQLite",
    "ARK_API_KEY": "ModelArk API key",
    "IDEAGEN_ARK_MODEL": "ModelArk endpoint/model id",
    "IDEAGEN_KAFKA_SERVERS": "Message Queue for Kafka bootstrap servers",
    "IDEAGEN_KAFKA_TOPIC": "run-event topic",
    "IDEAGEN_KAFKA_USER": "SASL user, if the instance requires it",
    "IDEAGEN_KAFKA_PASSWORD": "SASL password",
    "IDEAGEN_REDIS_URL": "Cache for Redis URL, e.g. redis://host:6379/0",
    "IDEAGEN_KMS_KEYRING": "KMS keyring name, default ideagen",
    "IDEAGEN_ARTIFACT_ROOT": "local artifact root, default data/artifacts",
    "ANTHROPIC_API_KEY": "local-adapter inference key",
    "OPENAI_API_KEY": "local-adapter inference key (alternative)",
    "IDEAGEN_INFERENCE_BASE_URL": "override for an OpenAI-compatible endpoint",
    "IDEAGEN_INFERENCE_MODEL": "local-adapter model id",
}

_ENV_FILE = Path.home() / ".ideagen.env"


def env_report() -> list[dict[str, Any]]:
    """Which platform variables are set. Never prints a value."""
    from .local import EnvSecretStore
    sec = EnvSecretStore(_ENV_FILE)
    out = []
    for k, why in ENV_KEYS.items():
        v = os.environ.get(k) or sec._file.get(k)          # noqa: SLF001
        out.append({"key": k, "purpose": why, "set": bool(v),
                    "source": ("env" if os.environ.get(k)
                               else ("file" if v else None))})
    return out


def load(*, platform: str | None = None) -> Platform:
    """Build the platform from the environment."""
    from .local import (DirectInference, EnvSecretStore, FileCache, FileEventBus,
                        LocalBlobStore, SqliteStateStore)
    from .. import config as cfg

    name = (platform or os.environ.get("IDEAGEN_PLATFORM") or "local").lower()
    if name not in ("local", "byteplus"):
        raise PlatformError(f"unknown IDEAGEN_PLATFORM={name!r}; "
                            f"expected 'local' or 'byteplus'")

    secrets: SecretStore = EnvSecretStore(_ENV_FILE)
    g = lambda k, d=None: (secrets.get(k, required=False) or d)  # noqa: E731

    if name == "byteplus":
        from .byteplus import (KafkaEventBus, KmsSecretStore, ModelArkInference,
                               PostgresStateStore, RedisCache, TosBlobStore)
        ak, sk = g("BYTEPLUS_ACCESS_KEY"), g("BYTEPLUS_SECRET_KEY")
        region = g("BYTEPLUS_REGION", "ap-southeast-1")
        # Every port is built through `port_or_unavailable`: a missing credential
        # must produce a health-check failure the operator can read, not a stack
        # trace during import.
        secrets = port_or_unavailable("secrets", lambda: KmsSecretStore(
            ak=ak or "", sk=sk or "", region=region,
            keyring=g("IDEAGEN_KMS_KEYRING", "ideagen")))
        blobs = port_or_unavailable("blobs", lambda: TosBlobStore(
            ak=ak or "", sk=sk or "", bucket=g("IDEAGEN_TOS_BUCKET", "") or "",
            region=region, prefix=g("IDEAGEN_TOS_PREFIX", "") or ""))
        # Postgres only when a DSN exists. One port at a time is the migration
        # path, and forcing all six to move together is how migrations stall.
        dsn = g("IDEAGEN_PG_DSN")
        state = port_or_unavailable("state", lambda: (
            PostgresStateStore(dsn) if dsn else SqliteStateStore(cfg.DB_PATH)))
        inference = port_or_unavailable("inference", lambda: ModelArkInference(
            api_key=g("ARK_API_KEY", "") or "",
            model=g("IDEAGEN_ARK_MODEL", "seed-1-6-flash")))
        events = port_or_unavailable("events", lambda: (
            KafkaEventBus(servers=g("IDEAGEN_KAFKA_SERVERS", "") or "",
                          topic=g("IDEAGEN_KAFKA_TOPIC", "ideagen.runs") or "",
                          username=g("IDEAGEN_KAFKA_USER"),
                          password=g("IDEAGEN_KAFKA_PASSWORD"))
            if g("IDEAGEN_KAFKA_SERVERS")
            else FileEventBus(_artifact_root() / "events.jsonl")))
        cache = port_or_unavailable("cache", lambda: (
            RedisCache(url=g("IDEAGEN_REDIS_URL") or "")
            if g("IDEAGEN_REDIS_URL")
            else FileCache(_artifact_root() / "cache")))
        return Platform(name="byteplus", blobs=blobs, state=state,
                        inference=inference, events=events, cache=cache,
                        secrets=secrets)

    root = _artifact_root()
    # Prefer an explicitly configured OpenAI-compatible endpoint, then Anthropic,
    # then OpenAI — so pointing local development at ModelArk needs no code change.
    base = g("IDEAGEN_INFERENCE_BASE_URL")
    key = g("ARK_API_KEY") if base else (g("ANTHROPIC_API_KEY") or g("OPENAI_API_KEY"))
    return Platform(
        name="local",
        blobs=port_or_unavailable("blobs", lambda: LocalBlobStore(root / "blobs")),
        state=port_or_unavailable("state", lambda: SqliteStateStore(cfg.DB_PATH)),
        inference=port_or_unavailable("inference", lambda: DirectInference(
            api_key=key, base_url=base,
            model=g("IDEAGEN_INFERENCE_MODEL", "claude-opus-5"))),
        events=port_or_unavailable("events", lambda: FileEventBus(root / "events.jsonl")),
        cache=port_or_unavailable("cache", lambda: FileCache(root / "cache")),
        secrets=secrets)


def _artifact_root() -> Path:
    from .. import config as cfg
    v = os.environ.get("IDEAGEN_ARTIFACT_ROOT")
    return Path(v) if v else (Path(cfg.DB_PATH).parent / "artifacts")


# ---------------------------------------------------------------------------
class RunJournal:
    """One immutable record per run, written through the blob store.

    Exists because the history this system already produced could not answer
    "what did the run actually see" — a batch was replaced under live positions,
    and days were scored against a corpus that kept growing underneath them. The
    journal makes each run a closed object: its inputs, the health of every port
    at the time, every step, every artifact URI, and its outcome.

    `run_id` is time-ordered and unique so artifact keys sort chronologically and
    two runs in the same second cannot collide.
    """

    def __init__(self, platform: Platform, *, kind: str,
                 as_of: str, run_id: str | None = None):
        self.p = platform
        self.kind = kind
        self.as_of = as_of
        self.run_id = run_id or (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"-{uuid.uuid4().hex[:8]}")
        self.steps: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, str]] = []
        self.started = time._time() if hasattr(time, "_time") else None
        self._t0 = datetime.now(timezone.utc)
        self.health = [h.__dict__ for h in platform.check()]
        self.p.events.publish("run.start", {"run_id": self.run_id, "kind": kind,
                                            "as_of": as_of})

    @property
    def prefix(self) -> str:
        return f"runs/{self.as_of}/{self.run_id}"

    def step(self, name: str, **fields: Any) -> None:
        rec = {"n": len(self.steps) + 1, "step": name,
               "at": datetime.now(timezone.utc).isoformat(), **fields}
        self.steps.append(rec)
        self.p.events.publish("run.step", {"run_id": self.run_id, **rec})

    def artifact(self, name: str, data: bytes, *,
                 content_type: str = "application/json") -> str:
        uri = self.p.blobs.put(f"{self.prefix}/{name}", data,
                               content_type=content_type,
                               metadata={"run_id": self.run_id, "kind": self.kind})
        self.artifacts.append({"name": name, "uri": uri, "bytes": str(len(data))})
        self.p.events.publish("run.artifact",
                              {"run_id": self.run_id, "name": name, "uri": uri})
        return uri

    def close(self, *, ok: bool, error: str | None = None) -> str:
        doc = {
            "run_id": self.run_id, "kind": self.kind, "as_of": self.as_of,
            "platform": self.p.name, "host": socket.gethostname(),
            "started_at": self._t0.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(
                (datetime.now(timezone.utc) - self._t0).total_seconds(), 3),
            "ok": ok, "error": error,
            "port_health": self.health,
            "steps": self.steps, "artifacts": self.artifacts,
        }
        uri = self.p.blobs.put(
            f"{self.prefix}/journal.json",
            json.dumps(doc, ensure_ascii=False, indent=1, default=str).encode(),
            content_type="application/json")
        self.p.events.publish("run.end", {"run_id": self.run_id, "ok": ok,
                                          "error": error, "journal": uri})
        return uri


import time  # noqa: E402  — used by RunJournal.started only
