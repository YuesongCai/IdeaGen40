"""Local adapter: filesystem, SQLite, direct Anthropic/OpenAI call, in-process bus.

This is not a mock. It is the adapter the system runs on during development and
the one every test exercises, so the port contracts are enforced continuously
rather than only when someone points the config at a cloud account.

Two behaviours are deliberately identical to the BytePlus adapter rather than
"good enough locally", because a difference here would hide a production bug:

  * `put` refuses to overwrite. Locally that is a file-exists check; on TOS it is
    a head-then-put. Either way, a second write to the same key raises.
  * `lock` is real. A stale lock file with a live PID blocks a second run, so
    "two runs at once place duplicate orders" fails the same way in both.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .base import (BlobMissing, BlobStore, Cache, Completion, EventBus, Health,
                   Inference, NotConfigured, PlatformError, SecretStore,
                   StateStore)


class LocalBlobStore(BlobStore):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        # Keys are trusted internal strings, but a stray "../" would escape the
        # artifact root and write into the repo, so resolve and confine.
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise PlatformError(f"key escapes the artifact root: {key!r}")
        return p

    def put(self, key: str, data: bytes, *, content_type: str | None = None,
            metadata: dict[str, str] | None = None) -> str:
        p = self._p(key)
        if p.exists():
            raise PlatformError(
                f"{key} already exists; artifacts are immutable — write a new run")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if metadata or content_type:
            side = p.with_suffix(p.suffix + ".meta.json")
            side.write_text(json.dumps(
                {"content_type": content_type, **(metadata or {})},
                ensure_ascii=False), encoding="utf-8")
        return self.uri(key)

    def get(self, key: str) -> bytes:
        p = self._p(key)
        if not p.exists():
            raise BlobMissing(f"no such artifact: {key}")
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def list(self, prefix: str) -> Iterator[str]:
        base = self._p(prefix)
        root = self.root.resolve()
        if base.is_file():
            yield prefix
            return
        if not base.exists():
            return
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.name.endswith(".meta.json"):
                yield str(f.resolve().relative_to(root))

    def uri(self, key: str) -> str:
        return f"file://{self._p(key)}"

    def check(self) -> Health:
        probe = self.root / ".health"
        try:
            probe.write_text(str(time.time()))
            probe.unlink()
        except OSError as e:
            return Health(False, "blobs", f"artifact root not writable: {e}")
        n = sum(1 for _ in self.root.rglob("*") if _.is_file())
        return Health(True, "blobs", f"local fs {self.root} ({n} artifacts)",
                      {"root": str(self.root), "objects": n})


class SqliteStateStore(StateStore):
    paramstyle = "qmark"
    dialect = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")

    @property
    def connection(self) -> sqlite3.Connection:
        """Escape hatch for the existing hand-written SQL modules.

        Kept explicit rather than hidden: those ~9,000 lines already encode the
        as-of and ordering invariants that took longest to get right, and porting
        them to the port API would risk exactly those. New code uses the port.
        """
        return self._con

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._con.execute(sql, tuple(args)).fetchall()]

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        cur = self._con.execute(sql, tuple(args))
        self._con.commit()
        return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        cur = self._con.executemany(sql, [tuple(r) for r in rows])
        self._con.commit()
        return cur.rowcount

    @contextlib.contextmanager
    def tx(self):
        try:
            yield self._con
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise

    def migrate(self, ddl: Sequence[str]) -> int:
        n = 0
        with self.tx():
            for stmt in ddl:
                s = stmt.strip()
                if s:
                    self._con.execute(s)
                    n += 1
        return n

    def check(self) -> Health:
        try:
            v = self._con.execute("SELECT sqlite_version()").fetchone()[0]
            tables = self._con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        except sqlite3.Error as e:
            return Health(False, "state", f"sqlite error: {e}")
        return Health(True, "state", f"sqlite {v} at {self.path} ({tables} tables)",
                      {"engine": "sqlite", "version": v, "tables": tables})


class DirectInference(Inference):
    """Calls the model API directly, using whatever key is present.

    Supports an OpenAI-compatible endpoint so the same code path serves both this
    adapter and ModelArk — ModelArk is OpenAI-compatible, so the difference
    between local and production inference is a base URL and a key.
    """

    def __init__(self, *, api_key: str | None, base_url: str | None,
                 model: str, name: str = "openai-compatible",
                 timeout: float = 180.0, max_retries: int = 2):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.name = name
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = None

    def _c(self):
        if self._client is None:
            if not self.api_key:
                raise NotConfigured("no inference API key set")
            try:
                from openai import OpenAI
            except ImportError as e:
                raise NotConfigured(
                    "openai SDK not installed; pip install openai") from e
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=self.timeout,
                                  max_retries=self.max_retries)
        return self._client

    def complete(self, prompt: str, *, system: str | None = None,
                 model: str | None = None, temperature: float = 0.0,
                 max_tokens: int | None = None,
                 json_schema: dict[str, Any] | None = None) -> Completion:
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        kw: dict[str, Any] = {"model": model or self.model, "messages": msgs,
                              "temperature": temperature}
        if max_tokens:
            kw["max_tokens"] = max_tokens
        if json_schema:
            # Structured output, not prose. The forecasting literature finds a
            # narrative framing measurably worsens probability quality, so every
            # scoring call goes through a schema rather than free text.
            kw["response_format"] = {"type": "json_schema",
                                     "json_schema": json_schema}
        t0 = time.time()
        r = self._c().chat.completions.create(**kw)
        return Completion(
            text=(r.choices[0].message.content or ""),
            model=r.model, raw_id=getattr(r, "id", None),
            usage=(r.usage.model_dump() if getattr(r, "usage", None) else {}),
            latency_ms=int((time.time() - t0) * 1000))

    def complete_many(self, prompt: str, *, k: int = 5, **kw: Any) -> list[Completion]:
        # Independent samples need a non-zero temperature or all k are identical
        # and the median is just one sample wearing a disguise.
        kw.setdefault("temperature", 0.7)
        return [self.complete(prompt, **kw) for _ in range(k)]

    def check(self) -> Health:
        if not self.api_key:
            return Health(False, "inference", "no API key configured")
        return Health(True, "inference",
                      f"{self.name} model={self.model} base={self.base_url or 'default'}",
                      {"model": self.model, "base_url": self.base_url})


class FileEventBus(EventBus):
    """Appends events as JSONL. The local stand-in for a Kafka topic."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.errors = 0

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"topic": topic, **event},
                                    ensure_ascii=False, default=str) + "\n")
        except OSError:
            # Observability must never break the run it observes.
            self.errors += 1

    def check(self) -> Health:
        return Health(True, "events", f"jsonl {self.path} (errors {self.errors})",
                      {"sink": str(self.path), "errors": self.errors})


class FileCache(Cache):
    """Filesystem cache with a PID-aware lock.

    The lock records the holder's PID so a crashed run does not block the next
    one forever — a stale lock whose process is gone is broken deliberately,
    while a live holder is respected.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        safe = key.replace("/", "__")
        return self.root / safe

    def get(self, key: str) -> bytes | None:
        p = self._p(key)
        if not p.exists():
            return None
        head, _, body = p.read_bytes().partition(b"\n")
        try:
            exp = float(head)
        except ValueError:
            return None
        if exp and exp < time.time():
            p.unlink(missing_ok=True)
            return None
        return body

    def set(self, key: str, value: bytes, *, ttl_s: int | None = None) -> None:
        exp = (time.time() + ttl_s) if ttl_s else 0
        self._p(key).write_bytes(f"{exp}\n".encode() + value)

    @contextlib.contextmanager
    def lock(self, key: str, *, ttl_s: int = 3600):
        p = self._p(f"lock__{key}")
        acquired = False
        try:
            if p.exists():
                try:
                    meta = json.loads(p.read_text())
                    alive = _pid_alive(int(meta.get("pid", -1)))
                    fresh = float(meta.get("expires", 0)) > time.time()
                except (ValueError, json.JSONDecodeError):
                    alive, fresh = False, False
                if alive and fresh:
                    yield False
                    return
                p.unlink(missing_ok=True)     # stale: holder gone or expired
            p.write_text(json.dumps({"pid": os.getpid(),
                                     "expires": time.time() + ttl_s}))
            acquired = True
            yield True
        finally:
            if acquired:
                p.unlink(missing_ok=True)

    def check(self) -> Health:
        n = sum(1 for _ in self.root.glob("*"))
        return Health(True, "cache", f"local fs {self.root} ({n} keys)",
                      {"root": str(self.root), "keys": n})


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists but owned by someone else
    return True


class EnvSecretStore(SecretStore):
    """Reads configuration from the environment and chmod-600 env files.

    Process variables have highest priority. When multiple files are supplied,
    later files override earlier ones; the project-local ignored `.env` can
    therefore override the operator-wide `~/.ideagen.env` without changing it.
    """

    def __init__(self, env_file: Path | str | Iterable[Path | str] | None = None):
        if env_file is None:
            files: list[Path] = []
        elif isinstance(env_file, (str, Path)):
            files = [Path(env_file)]
        else:
            files = [Path(p) for p in env_file]
        self.env_files = files
        # Backward compatibility for callers and diagnostics that expect one path.
        self.env_file = files[-1] if files else None
        self._file: dict[str, str] = {}
        self._sources: dict[str, Path] = {}
        for path in files:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    key = k.strip()
                    self._file[key] = v.strip().strip('"').strip("'")
                    self._sources[key] = path

    def get(self, name: str, *, required: bool = True) -> str | None:
        v = os.environ.get(name) or self._file.get(name)
        if not v and required:
            raise NotConfigured(
                f"{name} is not set (checked environment and "
                f"{', '.join(map(str, self.env_files)) or 'no env file'})")
        return v

    def declared(self, name: str) -> str | None:
        """The value the env file states, ignoring the process environment.

        `get` exists to answer "what is in effect", so a process variable wins
        there and should. But that leaves no way to ask what the operator wrote
        down, and the two can differ without anything noticing — a wrapper that
        injects a role reads as configuration when it is an override. Telling
        those apart needs the file's own answer, which is what this returns.
        """
        return self._file.get(name)

    def source(self, name: str) -> str | None:
        """Where a key came from, without exposing its value."""
        if os.environ.get(name):
            return "env"
        path = self._sources.get(name)
        return str(path) if self._file.get(name) and path else None

    def check(self) -> Health:
        found: list[dict[str, str]] = []
        for path in self.env_files:
            if not path.exists():
                continue
            mode = oct(path.stat().st_mode & 0o777)
            found.append({"file": str(path), "mode": mode})
            if mode not in ("0o600", "0o400"):
                return Health(False, "secrets",
                              f"{path} is {mode}; must be 600",
                              {"files": found})
        return Health(True, "secrets",
                      f"env + {len(found)} file(s) ({len(self._file)} keys)",
                      {"files": found, "file_keys": len(self._file)})
