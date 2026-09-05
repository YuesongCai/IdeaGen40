"""Platform wiring: one switch decides where the system runs.

    IDEAGEN_PLATFORM=local      filesystem + SQLite + direct API      (default)
    IDEAGEN_PLATFORM=byteplus   TOS + RDS + ModelArk + MQ + Redis + KMS

Every other module imports `platform.load()` and never a vendor SDK, so the
pipeline has no idea which cloud it is on. That is what makes the BytePlus move a
configuration change rather than a rewrite, and what keeps the whole test suite
runnable with none of the cloud packages installed.

Ports can be mixed. `IDEAGEN_PLATFORM=byteplus` with no cloud database
configuration keeps SQLite while moving artifacts to TOS, which is the actual
migration path: move one port at a time and keep the others working.
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .base import (BlobMissing, BlobStore, Cache, Completion, EventBus, Health,
                   Inference, NotConfigured, Platform, PlatformError,
                   SecretStore, StateStore, Unavailable, port_or_unavailable)

__all__ = ["load", "Platform", "Health", "RunJournal", "journal_step",
           "NotConfigured",
           "PlatformError", "BlobMissing", "BlobStore", "StateStore",
           "Inference", "EventBus",
           "Cache", "SecretStore", "Completion", "env_report", "Unavailable"]

#: Everything the platform reads, in one place so `env_report` can show operators
#: exactly what is and is not set without hunting through the code.
ENV_KEYS = {
    "IDEAGEN_PLATFORM": "local | byteplus",
    "IDEAGEN_POC_WEEKLY_MODE": (
        "public-synthetic | shelf-fixture | olive-live | olive-auto"
    ),
    "IDEAGEN_TICK_INTERVAL_S": "scheduler loop interval seconds",
    "IDEAGEN_CLOUD_WISBURG_ENABLED": (
        "copy licensed Wisburg corpus to RDS/TOS; default false"
    ),
    "IDEAGEN_DASH_SHOW_LICENSED_NAMES": (
        "show licensed product names in Dashboard; default false"
    ),
    "IDEAGEN_DASH_KEY": "shared Dashboard access key",
    "BYTEPLUS_ACCESS_KEY": "BytePlus AK — TOS and KMS",
    "BYTEPLUS_SECRET_KEY": "BytePlus SK",
    "BYTEPLUS_REGION": "default ap-southeast-1",
    "IDEAGEN_TOS_BUCKET": "artifact bucket",
    "IDEAGEN_TOS_PREFIX": "key prefix inside the bucket, e.g. prod / staging",
    "IDEAGEN_TOS_ENDPOINT": "optional regional/private TOS endpoint",
    "IDEAGEN_STATE_ENGINE": "mysql | postgres | sqlite",
    "IDEAGEN_MYSQL_HOST": "RDS MySQL private/public endpoint",
    "IDEAGEN_MYSQL_PORT": "RDS MySQL port; blank defaults to 3306",
    "IDEAGEN_MYSQL_DATABASE": "RDS MySQL database name",
    "IDEAGEN_MYSQL_USER": "RDS MySQL account",
    "IDEAGEN_MYSQL_PASSWORD": "RDS MySQL password",
    "IDEAGEN_MYSQL_SSL_CA": "optional CA certificate path",
    "IDEAGEN_MYSQL_CONNECT_TIMEOUT": "connection timeout seconds; blank defaults to 10",
    "IDEAGEN_PG_DSN": "optional complete RDS PostgreSQL DSN",
    "IDEAGEN_PG_HOST": "RDS PostgreSQL private/public endpoint",
    "IDEAGEN_PG_PORT": "RDS PostgreSQL port; blank defaults to 5432",
    "IDEAGEN_PG_DATABASE": "RDS PostgreSQL database name",
    "IDEAGEN_PG_USER": "RDS PostgreSQL account",
    "IDEAGEN_PG_PASSWORD": "RDS PostgreSQL password",
    "IDEAGEN_PG_SSLMODE": "optional libpq SSL mode",
    "IDEAGEN_PG_CONNECT_TIMEOUT": "connection timeout seconds; blank defaults to 10",
    "ARK_API_KEY": "ModelArk API key",
    "IDEAGEN_ARK_MODEL": "ModelArk endpoint/model id",
    "IDEAGEN_INFERENCE_MODE": "claude | modelark; model config implies modelark",
    "IDEAGEN_INFERENCE_BASE_URL": "OpenAI-compatible inference endpoint",
    "IDEAGEN_INFERENCE_TIMEOUT_SECONDS": "model request timeout; default 180",
    "IDEAGEN_INFERENCE_MAX_RETRIES": "model request retries; default 2",
    "WISBURG_MCP_URL": "Wisburg streamable-HTTP MCP endpoint",
    "WISBURG_MCP_TOKEN": "Wisburg developer API key",
    "OLIVE_MCP_URL": "Olive streamable-HTTP MCP endpoint",
    "OLIVE_OAUTH_ISSUER": "Olive OAuth 2.1 authorization server",
    "OLIVE_OAUTH_CLIENT_ID": "registered public OAuth client",
    "OLIVE_OAUTH_ACCESS_TOKEN": "Olive OAuth bearer token",
    "OLIVE_OAUTH_REFRESH_TOKEN": "Olive OAuth refresh token",
    "IDEAGEN_OLIVE_TOKEN_FILE": "writable chmod-600 Olive OAuth token file",
    "IDEAGEN_KAFKA_SERVERS": "Message Queue for Kafka bootstrap servers",
    "IDEAGEN_KAFKA_TOPIC": "run-event topic",
    "IDEAGEN_KAFKA_USER": "SASL user, if the instance requires it",
    "IDEAGEN_KAFKA_PASSWORD": "SASL password",
    "IDEAGEN_REDIS_URL": "Cache for Redis URL, e.g. redis://host:6379/0",
    "IDEAGEN_KMS_KEYRING": "KMS keyring name, default ideagen",
    "IDEAGEN_ARTIFACT_ROOT": "local artifact root, default data/artifacts",
    "ANTHROPIC_API_KEY": "local-adapter inference key",
    "OPENAI_API_KEY": "local-adapter inference key (alternative)",
    "IDEAGEN_INFERENCE_MODEL": "local-adapter model id",
}

_ENV_FILE = Path.home() / ".ideagen.env"
_PROJECT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_ENV_FILES = (_ENV_FILE, _PROJECT_ENV_FILE)

# Existing local deployments used Volcano-style names before the platform layer
# settled on BytePlus/IdeaGen names. Resolve them without forcing secrets to be
# copied into duplicate variables.
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "BYTEPLUS_ACCESS_KEY": ("VOLCENGINE_ACCESS_KEY",),
    "BYTEPLUS_SECRET_KEY": ("VOLCENGINE_SECRET_KEY",),
    "BYTEPLUS_REGION": ("VOLCENGINE_REGION",),
    "IDEAGEN_ARK_MODEL": ("ARK_MODEL_ID",),
    "IDEAGEN_INFERENCE_MODEL": ("ARK_MODEL_ID",),
    "IDEAGEN_INFERENCE_BASE_URL": ("ARK_BASE_URL",),
    "IDEAGEN_INFERENCE_TIMEOUT_SECONDS": ("ARK_TIMEOUT_SECONDS",),
    "IDEAGEN_INFERENCE_MAX_RETRIES": ("ARK_MAX_RETRIES",),
    "WISBURG_MCP_TOKEN": ("WISBURG_API_KEY",),
}


def _setting(secrets: SecretStore, name: str, default: str | None = None) -> str | None:
    for key in (name, *ENV_ALIASES.get(name, ())):
        value = secrets.get(key, required=False)
        if value:
            return value
    return default


def _number(value: str | None, *, name: str, default: int,
            minimum: int = 0) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as e:
        raise NotConfigured(f"{name} must be an integer") from e
    if parsed < minimum:
        raise NotConfigured(f"{name} must be >= {minimum}")
    return parsed


def _decimal(value: str | None, *, name: str, default: float,
             minimum: float = 0.0) -> float:
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as e:
        raise NotConfigured(f"{name} must be a number") from e
    if parsed < minimum:
        raise NotConfigured(f"{name} must be >= {minimum}")
    return parsed


def _postgres_options(get: Callable[[str, str | None], str | None]
                      ) -> dict[str, Any] | None:
    """Resolve either a complete DSN or separate RDS connection fields."""
    timeout = _number(get("IDEAGEN_PG_CONNECT_TIMEOUT", None),
                      name="IDEAGEN_PG_CONNECT_TIMEOUT", default=10, minimum=1)
    dsn = get("IDEAGEN_PG_DSN", None)
    if dsn:
        return {"dsn": dsn, "connect_timeout": timeout}

    names = ("IDEAGEN_PG_HOST", "IDEAGEN_PG_DATABASE",
             "IDEAGEN_PG_USER", "IDEAGEN_PG_PASSWORD")
    values = {name: get(name, None) for name in names}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise NotConfigured(
            "incomplete PostgreSQL configuration; missing " + ", ".join(missing))
    options: dict[str, Any] = {
        "host": values["IDEAGEN_PG_HOST"],
        "port": _number(get("IDEAGEN_PG_PORT", None),
                        name="IDEAGEN_PG_PORT", default=5432, minimum=1),
        "dbname": values["IDEAGEN_PG_DATABASE"],
        "user": values["IDEAGEN_PG_USER"],
        "password": values["IDEAGEN_PG_PASSWORD"],
        "connect_timeout": timeout,
    }
    sslmode = get("IDEAGEN_PG_SSLMODE", None)
    if sslmode:
        options["sslmode"] = sslmode
    return options


def _mysql_options(get: Callable[[str, str | None], str | None], *,
                   required: bool = False) -> dict[str, Any] | None:
    """Resolve RDS MySQL fields without embedding credentials in a URL."""
    names = ("IDEAGEN_MYSQL_HOST", "IDEAGEN_MYSQL_DATABASE",
             "IDEAGEN_MYSQL_USER", "IDEAGEN_MYSQL_PASSWORD")
    values = {name: get(name, None) for name in names}
    if not any(values.values()) and not required:
        return None
    # A password with no host/db/user is staged, not misconfigured: the
    # migration flow parks IDEAGEN_MYSQL_PASSWORD in the operator env before
    # the server-side fields exist anywhere but the ECS runtime.env. Selecting
    # MySQL on the password alone took the local dashboard down; the strict
    # all-or-nothing rule stays for host/db/user, where a partial set really
    # does mean a typo.
    if (not required and values["IDEAGEN_MYSQL_PASSWORD"]
            and not any(values[n] for n in names[:3])):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise NotConfigured(
            "incomplete MySQL configuration; missing " + ", ".join(missing))
    options: dict[str, Any] = {
        "host": values["IDEAGEN_MYSQL_HOST"],
        "port": _number(get("IDEAGEN_MYSQL_PORT", None),
                        name="IDEAGEN_MYSQL_PORT", default=3306, minimum=1),
        "database": values["IDEAGEN_MYSQL_DATABASE"],
        "user": values["IDEAGEN_MYSQL_USER"],
        "password": values["IDEAGEN_MYSQL_PASSWORD"],
        "connect_timeout": _number(
            get("IDEAGEN_MYSQL_CONNECT_TIMEOUT", None),
            name="IDEAGEN_MYSQL_CONNECT_TIMEOUT", default=10, minimum=1),
    }
    ssl_ca = get("IDEAGEN_MYSQL_SSL_CA", None)
    if ssl_ca:
        options["ssl_ca"] = ssl_ca
    return options


def env_report() -> list[dict[str, Any]]:
    """Which platform variables are set. Never prints a value."""
    from .local import EnvSecretStore
    sec = EnvSecretStore(_ENV_FILES)
    active_engine = (sec.get("IDEAGEN_STATE_ENGINE", required=False) or "").lower()
    out = []
    for k, why in ENV_KEYS.items():
        if active_engine == "mysql" and k.startswith("IDEAGEN_PG_"):
            continue
        if active_engine == "postgres" and k.startswith("IDEAGEN_MYSQL_"):
            continue
        used = next((key for key in (k, *ENV_ALIASES.get(k, ()))
                     if sec.get(key, required=False)), None)
        source = sec.source(used) if used else None
        out.append({"key": k, "purpose": why, "set": bool(used),
                    "source": ("env" if source == "env"
                               else ("file" if source else None)),
                    "via": used if used != k else None})
    return out


def load(*, platform: str | None = None) -> Platform:
    """Build the platform from the environment."""
    from .local import (DirectInference, EnvSecretStore, FileCache, FileEventBus,
                        LocalBlobStore, SqliteStateStore)
    from .. import config as cfg

    config_store = EnvSecretStore(_ENV_FILES)
    secrets: SecretStore = config_store
    g = lambda k, d=None: _setting(secrets, k, d)  # noqa: E731
    name = (platform or g("IDEAGEN_PLATFORM", "local") or "local").lower()
    if name not in ("local", "byteplus"):
        raise PlatformError(f"unknown IDEAGEN_PLATFORM={name!r}; "
                            f"expected 'local' or 'byteplus'")

    if name == "byteplus":
        from .byteplus import (KafkaEventBus, KmsSecretStore, ModelArkInference,
                               MySQLStateStore, PostgresStateStore, RedisCache,
                               TosBlobStore)
        ak, sk = g("BYTEPLUS_ACCESS_KEY"), g("BYTEPLUS_SECRET_KEY")
        region = g("BYTEPLUS_REGION", "ap-southeast-1")
        # Every port is built through `port_or_unavailable`: a missing credential
        # must produce a health-check failure the operator can read, not a stack
        # trace during import.
        secrets = port_or_unavailable("secrets", lambda: KmsSecretStore(
            ak=ak or "", sk=sk or "", region=region,
            keyring=g("IDEAGEN_KMS_KEYRING", "ideagen"),
            fallback_store=config_store))
        blobs = port_or_unavailable("blobs", lambda: TosBlobStore(
            ak=ak or "", sk=sk or "", bucket=g("IDEAGEN_TOS_BUCKET", "") or "",
            region=region, prefix=g("IDEAGEN_TOS_PREFIX", "") or "",
            endpoint=g("IDEAGEN_TOS_ENDPOINT", "") or None))
        # MySQL is the current POC state engine. PostgreSQL remains supported for
        # existing deployments; explicit selection prevents stale variables from
        # silently choosing the wrong database.
        state_engine = (g("IDEAGEN_STATE_ENGINE", "") or "").lower()
        try:
            if state_engine == "mysql":
                state_options = _mysql_options(g, required=True)
                state_builder = lambda: MySQLStateStore(**state_options)
            elif state_engine == "postgres":
                state_options = _postgres_options(g)
                if not state_options:
                    raise NotConfigured("IDEAGEN_STATE_ENGINE=postgres but no "
                                        "PostgreSQL configuration is set")
                state_builder = lambda: PostgresStateStore(**state_options)
            elif state_engine == "sqlite":
                state_options = None
                state_builder = lambda: SqliteStateStore(cfg.DB_PATH)
            elif state_engine:
                raise NotConfigured(
                    "IDEAGEN_STATE_ENGINE must be mysql, postgres, or sqlite")
            else:
                mysql_options = _mysql_options(g)
                pg_options = _postgres_options(g)
                if mysql_options and pg_options:
                    raise NotConfigured(
                        "both MySQL and PostgreSQL are configured; set "
                        "IDEAGEN_STATE_ENGINE explicitly")
                if mysql_options:
                    state_builder = lambda: MySQLStateStore(**mysql_options)
                elif pg_options:
                    state_builder = lambda: PostgresStateStore(**pg_options)
                else:
                    state_builder = lambda: SqliteStateStore(cfg.DB_PATH)
        except NotConfigured as e:
            state = Unavailable("state", str(e))
        else:
            state = port_or_unavailable("state", state_builder)
        # Keep the old Claude queue as the no-model fallback, but enable ModelArk
        # automatically when both its key and endpoint/model id are configured.
        # An explicit IDEAGEN_INFERENCE_MODE always wins.
        default_mode = ("modelark" if g("ARK_API_KEY") and g("IDEAGEN_ARK_MODEL")
                        else "claude")
        mode = (g("IDEAGEN_INFERENCE_MODE", default_mode) or default_mode).lower()
        if mode == "claude":
            inference = Unavailable(
                "inference", "推理未启用；设置 ARK_API_KEY、模型 id，或显式"
                             "设置 IDEAGEN_INFERENCE_MODE=modelark")
        else:
            inference = port_or_unavailable("inference", lambda: ModelArkInference(
                api_key=g("ARK_API_KEY", "") or "",
                model=g("IDEAGEN_ARK_MODEL", "seed-1-6-flash") or "",
                base_url=g("IDEAGEN_INFERENCE_BASE_URL"),
                timeout=_decimal(
                    g("IDEAGEN_INFERENCE_TIMEOUT_SECONDS", "180"),
                    name="IDEAGEN_INFERENCE_TIMEOUT_SECONDS",
                    default=180.0, minimum=1.0),
                max_retries=_number(
                    g("IDEAGEN_INFERENCE_MAX_RETRIES", "2"),
                    name="IDEAGEN_INFERENCE_MAX_RETRIES", default=2)))
        root = _artifact_root(g("IDEAGEN_ARTIFACT_ROOT"))
        events = port_or_unavailable("events", lambda: (
            KafkaEventBus(servers=g("IDEAGEN_KAFKA_SERVERS", "") or "",
                          topic=g("IDEAGEN_KAFKA_TOPIC", "ideagen.runs") or "",
                          username=g("IDEAGEN_KAFKA_USER"),
                          password=g("IDEAGEN_KAFKA_PASSWORD"))
            if g("IDEAGEN_KAFKA_SERVERS")
            else FileEventBus(root / "events.jsonl")))
        cache = port_or_unavailable("cache", lambda: (
            RedisCache(url=g("IDEAGEN_REDIS_URL") or "")
            if g("IDEAGEN_REDIS_URL")
            else FileCache(root / "cache")))
        return Platform(name="byteplus", blobs=blobs, state=state,
                        inference=inference, events=events, cache=cache,
                        secrets=secrets)

    root = _artifact_root(g("IDEAGEN_ARTIFACT_ROOT"))
    # Prefer an explicitly configured OpenAI-compatible endpoint, then Anthropic,
    # then OpenAI — so pointing local development at ModelArk needs no code change.
    base = g("IDEAGEN_INFERENCE_BASE_URL")
    key = g("ARK_API_KEY") if base else (g("ANTHROPIC_API_KEY") or g("OPENAI_API_KEY"))
    default_mode = ("api" if key and g("IDEAGEN_INFERENCE_MODEL") else "claude")
    mode = (g("IDEAGEN_INFERENCE_MODE", default_mode) or default_mode).lower()
    return Platform(
        name="local",
        blobs=port_or_unavailable("blobs", lambda: LocalBlobStore(root / "blobs")),
        state=port_or_unavailable("state", lambda: SqliteStateStore(cfg.DB_PATH)),
        inference=(Unavailable("inference",
                               "推理未启用；设置模型 API key、base URL 和 model id")
                   if mode == "claude" else
                   port_or_unavailable("inference", lambda: DirectInference(
                       api_key=key, base_url=base,
                       model=g("IDEAGEN_INFERENCE_MODEL", "claude-opus-5") or "",
                       timeout=_decimal(
                           g("IDEAGEN_INFERENCE_TIMEOUT_SECONDS", "180"),
                           name="IDEAGEN_INFERENCE_TIMEOUT_SECONDS",
                           default=180.0, minimum=1.0),
                       max_retries=_number(
                           g("IDEAGEN_INFERENCE_MAX_RETRIES", "2"),
                           name="IDEAGEN_INFERENCE_MAX_RETRIES", default=2)))),
        events=port_or_unavailable("events", lambda: FileEventBus(root / "events.jsonl")),
        cache=port_or_unavailable("cache", lambda: FileCache(root / "cache")),
        secrets=secrets)


def _artifact_root(value: str | None = None) -> Path:
    from .. import config as cfg
    return Path(value) if value else (Path(cfg.DB_PATH).parent / "artifacts")


# ---------------------------------------------------------------------------
def utcnow_iso() -> str:
    """One UTC timestamp source. Run rows and journals must agree on the clock."""
    return datetime.now(timezone.utc).isoformat()


def journal_step(n: int, name: str, /, **fields: Any) -> dict[str, Any]:
    """One journal step record, with the structural keys protected.

    `n`, `step` and `at` are the record's own skeleton — the ordinal, the name
    and the clock. Splatting the caller's fields over a dict that already held
    them let a payload silently take the skeleton's place, and it did: several
    pipeline steps report a count under the key `n`, so the weekly journal's
    ordinals came out as 1, 858, 5, 9, 156 … — the corpus row count sitting
    where the step number belonged. Nothing errored, and the run log simply read
    as though the steps were shuffled.

    The two skeleton parameters are positional-only for the same reason the
    body exists: a caller passing `n=` as data must reach `**fields`, not bind
    the ordinal parameter.

    A payload `n` is always a count here, so it keeps its meaning under `count`
    rather than being dropped; `step` and `at` collisions have no such second
    meaning and are namespaced instead of silently discarded.
    """
    out: dict[str, Any] = {"n": n, "step": name,
                           "at": datetime.now(timezone.utc).isoformat()}
    for k, v in fields.items():
        if k == "n":
            out["count"] = v
        elif k in ("step", "at"):
            out[f"{k}_detail"] = v
        else:
            out[k] = v
    return out


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
        self.steps.append(journal_step(len(self.steps) + 1, name, **fields))
        self.p.events.publish("run.step",
                              {"run_id": self.run_id, **self.steps[-1]})

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
