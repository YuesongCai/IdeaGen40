"""BytePlus/Volcano adapter: TOS, RDS, ModelArk, Kafka, Redis, KMS.

Written against the real SDK surfaces, verified by introspection rather than from
memory:

  * `tos` 2.9.2 — `tos.TosClientV2(ak, sk, endpoint, region)`, with
    `put_object` / `get_object` / `head_object` / `list_objects_type2`.
  * The inference service is OpenAI-compatible and driven by the `openai` SDK.
    Its endpoint and key are supplied by the deployment owner.
  * RDS for MySQL over `PyMySQL`, with PostgreSQL compatibility via `psycopg`.
  * Message Queue for Kafka over `kafka-python`.
  * Cache for Redis over `redis`.
  * KMS over the `volcengine` SDK.

Every SDK import is lazy and inside the method that needs it. Importing this
module must never require the cloud dependencies, because `platform doctor` has to
be able to report "the TOS SDK is not installed" rather than failing to import and
reporting nothing. That is also what lets the whole test suite run without any of
these packages present.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, Iterable, Iterator, Sequence

from .base import (BlobStore, Cache, Completion, EventBus, Health, Inference,
                   NotConfigured, PlatformError, SecretStore, StateStore, redact_url)

# Deployment endpoints are intentionally absent from distributed source.
TOS_ENDPOINTS: dict[str, str] = {}
ARK_BASE = ""


def _qmark_to_pyformat(sql: str) -> str:
    """Translate portable qmark SQL while preserving literals and percent signs.

    PyMySQL and psycopg both treat ``%`` as part of their parameter syntax, even
    when the percent is a LIKE wildcard. Literal percent signs therefore become
    ``%%`` before the query is passed to the driver. Question marks inside quoted
    SQL strings stay literal rather than turning into phantom parameters.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        char = sql[i]
        if quote:
            if char == "%":
                out.append("%%")
            else:
                out.append(char)
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%":
            out.append("%%")
        else:
            out.append(char)
        i += 1
    return "".join(out)


class TosBlobStore(BlobStore):
    """TOS object storage. Immutability is enforced, not assumed.

    TOS has no conditional-put, so `put` does a head first. That is a race in
    principle; in practice keys are `runs/{run_id}/...` and the run lock already
    guarantees a single writer, so the head is a guard against operator error
    rather than against concurrency.
    """

    def __init__(self, *, ak: str, sk: str, bucket: str,
                 region: str = "ap-southeast-1", endpoint: str | None = None,
                 prefix: str = ""):
        if not (ak and sk and bucket):
            raise NotConfigured("TOS needs BYTEPLUS_ACCESS_KEY, "
                                "BYTEPLUS_SECRET_KEY and IDEAGEN_TOS_BUCKET")
        self.ak, self.sk = ak, sk
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint or TOS_ENDPOINTS.get(region)
        if not self.endpoint:
            raise NotConfigured(
                f"no TOS endpoint mapping for region {region}; set "
                "IDEAGEN_TOS_ENDPOINT explicitly")
        self.prefix = prefix.strip("/")
        self._client = None

    def _c(self):
        if self._client is None:
            try:
                import tos
            except ImportError as e:
                raise NotConfigured("TOS SDK missing; pip install tos") from e
            self._client = tos.TosClientV2(self.ak, self.sk, self.endpoint, self.region)
        return self._client

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}".lstrip("/") if self.prefix else key

    def put(self, key: str, data: bytes, *, content_type: str | None = None,
            metadata: dict[str, str] | None = None) -> str:
        if self.exists(key):
            raise PlatformError(
                f"{key} already exists in tos://{self.bucket}; artifacts are "
                f"immutable — write a new run")
        kw: dict[str, Any] = {"bucket": self.bucket, "key": self._k(key),
                              "content": data}
        if content_type:
            kw["content_type"] = content_type
        if metadata:
            kw["meta"] = metadata
        self._c().put_object(**kw)
        return self.uri(key)

    def get(self, key: str) -> bytes:
        try:
            return self._c().get_object(self.bucket, self._k(key)).read()
        except Exception as e:  # noqa: BLE001 — tos raises many concrete types
            raise PlatformError(f"tos get {key} failed: {e}") from e

    def exists(self, key: str) -> bool:
        try:
            self._c().head_object(self.bucket, self._k(key))
            return True
        except Exception:  # noqa: BLE001 — a miss is an exception in this SDK
            return False

    def list(self, prefix: str) -> Iterator[str]:
        token, base = None, self._k(prefix)
        while True:
            r = self._c().list_objects_type2(
                self.bucket, prefix=base, continuation_token=token, max_keys=1000)
            for o in (r.contents or []):
                k = o.key
                yield k[len(self.prefix) + 1:] if self.prefix else k
            if not getattr(r, "is_truncated", False):
                return
            token = r.next_continuation_token

    def uri(self, key: str) -> str:
        return f"tos://{self.bucket}/{self._k(key)}"

    def check(self) -> Health:
        try:
            next(iter(self.list("")), None)
        except NotConfigured:
            raise
        except Exception as e:  # noqa: BLE001
            return Health(False, "blobs", f"TOS unreachable: {e}",
                          {"bucket": self.bucket, "endpoint": self.endpoint})
        return Health(True, "blobs", f"tos://{self.bucket} @ {self.endpoint}",
                      {"bucket": self.bucket, "endpoint": self.endpoint,
                       "region": self.region})


class PostgresStateStore(StateStore):
    """RDS for PostgreSQL.

    `paramstyle` is `pyformat` because psycopg uses `%s`. `q()` translates `?` so
    portable call sites keep working, which is what lets the same migration and
    query code serve SQLite locally and Postgres in production.
    """

    paramstyle = "pyformat"
    dialect = "postgres"

    def __init__(self, dsn: str | None = None, *, host: str | None = None,
                 port: int = 5432, dbname: str | None = None,
                 user: str | None = None, password: str | None = None,
                 sslmode: str | None = None, connect_timeout: int = 10):
        if not dsn and not all((host, dbname, user, password)):
            raise NotConfigured(
                "PostgreSQL needs IDEAGEN_PG_DSN, or "
                "IDEAGEN_PG_HOST / DATABASE / USER / PASSWORD")
        self.dsn = dsn
        self.connect_kwargs: dict[str, Any] = {}
        if not dsn:
            self.connect_kwargs = {
                "host": host, "port": port, "dbname": dbname,
                "user": user, "password": password,
            }
            if sslmode:
                self.connect_kwargs["sslmode"] = sslmode
        self.connect_timeout = connect_timeout
        self._con = None

    def _c(self):
        if self._con is None or getattr(self._con, "closed", 1):
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as e:
                raise NotConfigured(
                    "psycopg missing; pip install 'psycopg[binary]'") from e
            options = {
                **self.connect_kwargs,
                "row_factory": dict_row,
                "connect_timeout": self.connect_timeout,
                "autocommit": True,
            }
            self._con = (psycopg.connect(self.dsn, **options)
                         if self.dsn else psycopg.connect(**options))
        return self._con

    @staticmethod
    def _sql(sql: str) -> str:
        return _qmark_to_pyformat(sql)

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._c().cursor() as cur:
            cur.execute(self._sql(sql), tuple(args))
            return list(cur.fetchall())

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self._c().cursor() as cur:
            cur.execute(self._sql(sql), tuple(args))
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._c().cursor() as cur:
            cur.executemany(self._sql(sql), [tuple(r) for r in rows])
            return cur.rowcount

    @contextlib.contextmanager
    def tx(self):
        con = self._c()
        con.autocommit = False
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.autocommit = True

    def migrate(self, ddl: Sequence[str]) -> int:
        n = 0
        with self.tx() as con:
            with con.cursor() as cur:
                for stmt in ddl:
                    s = stmt.strip()
                    if s:
                        cur.execute(s)
                        n += 1
        return n

    def check(self) -> Health:
        try:
            v = self.q("SELECT version() AS v")[0]["v"].split(",")[0]
            t = self.q("SELECT COUNT(*) AS n FROM information_schema.tables "
                       "WHERE table_schema='public'")[0]["n"]
        except NotConfigured:
            raise
        except Exception as e:  # noqa: BLE001
            return Health(False, "state", f"postgres unreachable: {e}")
        return Health(True, "state", f"{v} ({t} tables)",
                      {"engine": "postgres", "version": v, "tables": t})


class MySQLStateStore(StateStore):
    """RDS for MySQL, using PyMySQL and dictionary rows."""

    paramstyle = "pyformat"
    dialect = "mysql"

    def __init__(self, *, host: str, database: str, user: str, password: str,
                 port: int = 3306, ssl_ca: str | None = None,
                 connect_timeout: int = 10):
        if not all((host, database, user, password)):
            raise NotConfigured(
                "MySQL needs IDEAGEN_MYSQL_HOST / DATABASE / USER / PASSWORD")
        self.connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
            "charset": "utf8mb4",
            "connect_timeout": connect_timeout,
            "read_timeout": max(connect_timeout, 30),
            "write_timeout": max(connect_timeout, 30),
            "autocommit": True,
        }
        if ssl_ca:
            self.connect_kwargs["ssl"] = {"ca": ssl_ca}
        self._con = None

    def _c(self):
        if self._con is None or not getattr(self._con, "open", False):
            try:
                import pymysql
                from pymysql.cursors import DictCursor
            except ImportError as e:
                raise NotConfigured(
                    "PyMySQL missing; pip install PyMySQL") from e
            self._con = pymysql.connect(
                **self.connect_kwargs, cursorclass=DictCursor)
        return self._con

    @staticmethod
    def _sql(sql: str) -> str:
        return _qmark_to_pyformat(sql)

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._c().cursor() as cur:
            cur.execute(self._sql(sql), tuple(args))
            return list(cur.fetchall())

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self._c().cursor() as cur:
            cur.execute(self._sql(sql), tuple(args))
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._c().cursor() as cur:
            cur.executemany(self._sql(sql), [tuple(r) for r in rows])
            return cur.rowcount

    @contextlib.contextmanager
    def tx(self):
        con = self._c()
        con.begin()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise

    def migrate(self, ddl: Sequence[str]) -> int:
        n = 0
        with self.tx() as con:
            with con.cursor() as cur:
                for stmt in ddl:
                    s = stmt.strip()
                    if s:
                        cur.execute(s)
                        n += 1
        return n

    def check(self) -> Health:
        try:
            v = self.q("SELECT VERSION() AS v")[0]["v"]
            t = self.q(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema=DATABASE()")[0]["n"]
        except NotConfigured:
            raise
        except Exception as e:  # noqa: BLE001
            return Health(False, "state", f"mysql unreachable: {e}")
        return Health(True, "state", f"MySQL {v} ({t} tables)",
                      {"engine": "mysql", "version": v, "tables": t})


class ModelArkInference(Inference):
    """ModelArk via the OpenAI-compatible endpoint.

    Reuses the local adapter's client logic rather than duplicating it: ModelArk
    speaks the OpenAI protocol, so the only differences are the base URL, the key
    and the model id. Duplicating the call path would mean two places to keep the
    schema/sampling behaviour correct.
    """

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None,
                 timeout: float = 180.0, max_retries: int = 2):
        if not api_key:
            raise NotConfigured("ARK_API_KEY is not set")
        if not (base_url or ARK_BASE):
            raise NotConfigured("IDEAGEN_INFERENCE_BASE_URL is not set")
        from .local import DirectInference
        self._d = DirectInference(api_key=api_key, base_url=base_url or ARK_BASE,
                                  model=model, name="modelark", timeout=timeout,
                                  max_retries=max_retries)

    def complete(self, prompt: str, **kw: Any) -> Completion:
        return self._d.complete(prompt, **kw)

    def complete_many(self, prompt: str, *, k: int = 5, **kw: Any) -> list[Completion]:
        return self._d.complete_many(prompt, k=k, **kw)

    def check(self) -> Health:
        h = self._d.check()
        return Health(h.ok, "inference", f"ModelArk {h.detail}", h.meta)


class KafkaEventBus(EventBus):
    """Message Queue for Kafka. Never raises into the pipeline."""

    def __init__(self, *, servers: str, topic: str,
                 username: str | None = None, password: str | None = None):
        if not (servers and topic):
            raise NotConfigured(
                "Kafka needs IDEAGEN_KAFKA_SERVERS and IDEAGEN_KAFKA_TOPIC")
        self.servers = servers
        self.topic = topic
        self.username, self.password = username, password
        self._p = None
        self.errors = 0

    def _producer(self):
        if self._p is None:
            try:
                from kafka import KafkaProducer
            except ImportError as e:
                raise NotConfigured("kafka-python missing") from e
            kw: dict[str, Any] = {
                "bootstrap_servers": self.servers.split(","),
                "value_serializer": lambda v: json.dumps(
                    v, ensure_ascii=False, default=str).encode(),
                "linger_ms": 50, "retries": 3, "acks": 1,
            }
            if self.username:
                kw.update(security_protocol="SASL_PLAINTEXT",
                          sasl_mechanism="PLAIN",
                          sasl_plain_username=self.username,
                          sasl_plain_password=self.password)
            self._p = KafkaProducer(**kw)
        return self._p

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        try:
            self._producer().send(self.topic, {"topic": topic, **event})
        except Exception:  # noqa: BLE001 — monitoring must not fail the run
            self.errors += 1

    def check(self) -> Health:
        try:
            self._producer()
        except NotConfigured:
            raise
        except Exception as e:  # noqa: BLE001
            return Health(False, "events", f"kafka unreachable: {e}")
        return Health(True, "events", f"kafka {self.servers} → {self.topic}",
                      {"servers": self.servers, "topic": self.topic,
                       "errors": self.errors})


class RedisCache(Cache):
    """Cache for Redis. `lock` uses SET NX EX, which is the point of using Redis.

    A filesystem lock cannot coordinate two sandboxes; a Redis lock can, and
    "exactly one weekly run" is the invariant that stops duplicate orders.
    """

    def __init__(self, *, url: str):
        if not url:
            raise NotConfigured("IDEAGEN_REDIS_URL is not set")
        self.url = url
        self._r = None

    def _c(self):
        if self._r is None:
            try:
                import redis
            except ImportError as e:
                raise NotConfigured("redis missing; pip install redis") from e
            self._r = redis.from_url(self.url, socket_timeout=5)
        return self._r

    def get(self, key: str) -> bytes | None:
        return self._c().get(key)

    def set(self, key: str, value: bytes, *, ttl_s: int | None = None) -> None:
        self._c().set(key, value, ex=ttl_s)

    @contextlib.contextmanager
    def lock(self, key: str, *, ttl_s: int = 3600):
        r, k = self._c(), f"lock:{key}"
        got = bool(r.set(k, str(time.time()).encode(), nx=True, ex=ttl_s))
        try:
            yield got
        finally:
            if got:
                r.delete(k)

    def check(self) -> Health:
        try:
            info = self._c().info("server")
        except NotConfigured:
            raise
        except Exception as e:  # noqa: BLE001
            return Health(False, "cache", f"redis unreachable: {e}")
        return Health(True, "cache",
                      f"redis {info.get('redis_version')} @ {redact_url(self.url)}",
                      {"version": info.get("redis_version")})


class KmsSecretStore(SecretStore):
    """KMS-backed secrets, with the environment as an explicit fallback.

    The fallback is deliberate and logged in `check()`: a sandbox that cannot
    reach KMS should still be able to run from injected environment variables,
    but the operator must be able to see which mode is live — silently degrading
    to environment secrets would hide a misconfigured KMS for weeks.
    """

    def __init__(self, *, ak: str, sk: str, region: str = "ap-southeast-1",
                 keyring: str = "ideagen", fallback_env: bool = True,
                 fallback_store: SecretStore | None = None):
        self.ak, self.sk, self.region, self.keyring = ak, sk, region, keyring
        self.fallback_env = fallback_env
        self.fallback_store = fallback_store
        self._svc = None
        self._cache: dict[str, str] = {}
        self.used_fallback: list[str] = []

    def _s(self):
        if self._svc is None:
            try:
                from volcengine.kms.KmsService import KmsService
            except ImportError as e:
                raise NotConfigured("volcengine SDK missing; pip install volcengine") from e
            svc = KmsService()
            svc.set_ak(self.ak)
            svc.set_sk(self.sk)
            self._svc = svc
        return self._svc

    def get(self, name: str, *, required: bool = True) -> str | None:
        if name in self._cache:
            return self._cache[name]
        try:
            r = self._s().describe_secret({"KeyringName": self.keyring,
                                           "SecretName": name})
            v = (((r or {}).get("Result") or {}).get("Secret") or {}).get("SecretValue")
            if v:
                self._cache[name] = v
                return v
        except Exception:  # noqa: BLE001 — fall through to env, then report
            pass
        if self.fallback_store is not None:
            v = self.fallback_store.get(name, required=False)
            if v:
                self.used_fallback.append(name)
                self._cache[name] = v
                return v
        elif self.fallback_env:
            import os
            v = os.environ.get(name)
            if v:
                self.used_fallback.append(name)
                self._cache[name] = v
                return v
        if required:
            raise NotConfigured(f"{name} not in KMS keyring {self.keyring!r} "
                                f"and not in fallback configuration")
        return None

    def check(self) -> Health:
        if not (self.ak and self.sk):
            return Health(False, "secrets",
                          "BYTEPLUS_ACCESS_KEY / BYTEPLUS_SECRET_KEY not set")
        detail = f"KMS keyring {self.keyring} @ {self.region}"
        if self.used_fallback:
            detail += (f"  ⚠ {len(self.used_fallback)} secret(s) came from the "
                       f"fallback configuration, not KMS: "
                       f"{sorted(set(self.used_fallback))}")
        return Health(True, "secrets", detail,
                      {"keyring": self.keyring, "region": self.region,
                       "env_fallbacks": sorted(set(self.used_fallback))})
