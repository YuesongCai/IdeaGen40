"""Portable DDL for the tables the new layer writes, plus a secrets audit.

The pipeline's existing twenty tables are SQLite-specific and already correct;
these are the tables the registry-based run adds, written to work on both SQLite
and PostgreSQL so moving state to RDS is a DSN change rather than a migration
project. That means: TEXT over VARCHAR(n), no AUTOINCREMENT, no JSON column type
(store JSON as TEXT and parse in Python), and every DDL statement idempotent so
`migrate` can run on every boot.

`secret_audit` is here rather than in a script because handing the project to
another team starts with proving the repository holds no credentials.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

#: Idempotent DDL. Order matters only for the indexes.
DDL: tuple[str, ...] = (
    # One row per completed run. The journal in object storage is the full record;
    # this is the queryable index over it.
    #
    # Named `orch_runs` rather than `runs`: the original pipeline already owns a
    # `runs` table with a different shape and nine rows of history. `CREATE TABLE
    # IF NOT EXISTS` on a colliding name silently does nothing and the mismatch
    # only surfaces later as a missing-column error at insert time, so the new
    # tables take a prefix and `verify()` below refuses to let that happen quietly.
    """CREATE TABLE IF NOT EXISTS orch_runs (
         run_id      TEXT PRIMARY KEY,
         as_of       TEXT NOT NULL,
         kind        TEXT NOT NULL,
         platform    TEXT,
         started_at  TEXT,
         ended_at    TEXT,
         ok          INTEGER,
         error       TEXT,
         inputs_sha  TEXT,
         journal_uri TEXT,
         calls       INTEGER DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS orch_runs_as_of ON orch_runs (as_of)",
    # At most one *completed* weekly run per period. Uniqueness on (kind, as_of)
    # alone would be wrong twice over: monitoring writes many rows for one date, and
    # a failed attempt followed by a successful re-run legitimately leaves two rows
    # for the same period — that history is what says the first one failed. What must
    # never happen is one period counted as done twice, so the constraint is on the
    # completed row, and it is enforced by the database rather than by the lock alone.
    "CREATE UNIQUE INDEX IF NOT EXISTS orch_runs_done "
    "ON orch_runs (kind, as_of) WHERE ok = 1 AND kind = 'weekly'",

    # Which feeds ran, what they returned, and whether they validated. A feed that
    # silently returned nothing looks identical to a quiet week without this.
    """CREATE TABLE IF NOT EXISTS feed_runs (
         run_id   TEXT NOT NULL,
         feed     TEXT NOT NULL,
         kind     TEXT NOT NULL,
         as_of    TEXT NOT NULL,
         n_rows   INTEGER DEFAULT 0,
         ok       INTEGER,
         error    TEXT,
         rows_sha TEXT,
         PRIMARY KEY (run_id, feed)
       )""",

    # One row per strategy per run. `version` and `inputs_sha` are what make a
    # book traceable to the exact logic and the exact inputs that produced it.
    """CREATE TABLE IF NOT EXISTS verdicts (
         run_id     TEXT NOT NULL,
         as_of      TEXT NOT NULL,
         kind       TEXT NOT NULL,
         strategy   TEXT NOT NULL,
         version    TEXT NOT NULL,
         role       TEXT,
         inputs_sha TEXT,
         chosen     TEXT,
         scores     TEXT,
         rejected   TEXT,
         meta       TEXT,
         calls      INTEGER DEFAULT 0,
         PRIMARY KEY (run_id, kind, strategy)
       )""",
    "CREATE INDEX IF NOT EXISTS verdicts_strategy ON verdicts (strategy, as_of)",

    # Candidates as presented to every strategy in a run — the shared input that
    # makes their results differenceable.
    """CREATE TABLE IF NOT EXISTS candidates (
         run_id        TEXT NOT NULL,
         candidate_id  TEXT NOT NULL,
         as_of         TEXT NOT NULL,
         instrument_id TEXT,
         topic_id      TEXT,
         method        TEXT,
         direction     TEXT,
         upside_pct    REAL,
         downside_pct  REAL,
         p_up          REAL,
         p_base        REAL,
         p_down        REAL,
         sigma_1m      REAL,
         payload       TEXT,
         PRIMARY KEY (run_id, candidate_id)
       )""",

    # Dated events with expectations, and the watchpoints written against them.
    """CREATE TABLE IF NOT EXISTS events (
         event_id    TEXT PRIMARY KEY,
         date        TEXT NOT NULL,
         label       TEXT,
         kind        TEXT,
         expectation TEXT,
         actual      TEXT,
         unit        TEXT,
         source      TEXT,
         as_of       TEXT,
         feed        TEXT
       )""",
    "CREATE INDEX IF NOT EXISTS events_date ON events (date)",

    """CREATE TABLE IF NOT EXISTS watchpoints (
         watch_id     TEXT PRIMARY KEY,
         run_id       TEXT,
         candidate_id TEXT,
         event_id     TEXT,
         expectation  TEXT,
         deviation    TEXT,
         action       TEXT,
         status       TEXT DEFAULT 'armed',
         resolved_d   TEXT,
         outcome      TEXT
       )""",
    "CREATE INDEX IF NOT EXISTS watchpoints_event ON watchpoints (event_id, status)",
)


#: Tables this module owns, with the columns insert paths depend on.
OWNED: dict[str, tuple[str, ...]] = {
    "orch_runs":   ("run_id", "as_of", "kind", "platform", "ok", "journal_uri"),
    "feed_runs":   ("run_id", "feed", "kind", "n_rows", "ok", "rows_sha"),
    "verdicts":    ("run_id", "kind", "strategy", "version", "inputs_sha", "chosen"),
    "candidates":  ("run_id", "candidate_id", "instrument_id", "topic_id",
                    "method", "payload"),
    "events":      ("event_id", "date", "kind", "expectation", "actual"),
    "watchpoints": ("watch_id", "event_id", "deviation", "action", "status"),
}


#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` cannot
#: add a column to an existing table, and SQLite has no `ADD COLUMN IF NOT EXISTS`,
#: so evolving a table means checking what is there and adding what is not. Kept as
#: data rather than hand-written migrations because the check has to run on every
#: boot: a deploy that skipped one migration must self-heal, not fail at insert.
ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("candidates", "topic_id", "TEXT"),
    ("candidates", "method",   "TEXT"),
    # When an instrument first appeared on the shelf. Without it the universe has
    # no as-of: a replay of July sees August's shelf, so a July thesis can be
    # expressed through a product that did not exist that month — the strategy
    # then looks prescient for a reason that has nothing to do with the strategy.
    # Rows predating this column stay NULL, meaning "unknown", and `universe.eligible`
    # counts them rather than assuming they were always there.
    ("instruments", "first_seen_d", "TEXT"),
)


def evolve(state: Any) -> list[str]:
    """Add any declared column that a pre-existing table is missing."""
    done: list[str] = []
    for table, col, decl in ADD_COLUMNS:
        try:
            if col in _columns(state, table):
                continue
            state.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            done.append(f"{table}.{col}")
        except Exception:  # noqa: BLE001 — a missing table is created by the DDL above
            continue
    return done


def verify(state: Any) -> list[str]:
    """Confirm every owned table exists with the columns we write to.

    `CREATE TABLE IF NOT EXISTS` is a no-op against a table of the same name and a
    different shape, which turns a schema collision into a missing-column error at
    the first insert — far from the cause. Checking the columns after migrating
    turns that into one readable message at startup.
    """
    problems: list[str] = []
    for table, cols in OWNED.items():
        try:
            rows = state.q(f"SELECT * FROM {table} LIMIT 0")   # noqa: S608 — fixed names
            have = set(_columns(state, table))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{table}: not queryable ({type(e).__name__}: {e})")
            continue
        missing = [c for c in cols if c not in have]
        if missing:
            problems.append(
                f"{table}: missing {missing} — a table of this name already exists "
                f"with a different shape, so the CREATE was a no-op")
    return problems


#: Child tables and the run they must belong to. A row whose run has no record is
#: a row from a crashed run, and it validates exactly like a good one.
CHILDREN: tuple[str, ...] = ("feed_runs", "verdicts", "candidates")


def orphans(state: Any) -> dict[str, int]:
    """Rows whose parent run was never recorded, per table.

    A run that died before writing its own row leaves its output behind with
    nothing to mark it untrustworthy — and a verdict from a half-finished run is
    indistinguishable from a real one, which quietly corrupts any comparison built
    on those rows. This reports; it deliberately does not delete. Discarding data
    because it looks wrong is how a real result gets thrown away, so the call is a
    human's.
    """
    out: dict[str, int] = {}
    for t in CHILDREN:
        try:
            n = state.q(f"SELECT COUNT(*) AS c FROM {t} LEFT JOIN orch_runs r "  # noqa: S608
                        f"ON {t}.run_id = r.run_id WHERE r.run_id IS NULL")[0]["c"]
        except Exception:  # noqa: BLE001
            continue
        if n:
            out[t] = int(n)
    return out


def _columns(state: Any, table: str) -> list[str]:
    """Column names, whichever engine is behind the port."""
    if getattr(state, "paramstyle", "qmark") == "qmark":
        return [r["name"] for r in state.q(f"PRAGMA table_info({table})")]
    return [r["column_name"] for r in state.q(
        "SELECT column_name FROM information_schema.columns WHERE table_name=?",
        (table,))]


#: Conflict key per owned table, matching the PRIMARY KEY in the DDL above. An
#: upsert has to name the key it resolves on, and naming it here keeps the DDL and
#: the write path from drifting apart.
CONFLICT_KEY: dict[str, tuple[str, ...]] = {
    "orch_runs":   ("run_id",),
    "feed_runs":   ("run_id", "feed"),
    "verdicts":    ("run_id", "kind", "strategy"),
    "candidates":  ("run_id", "candidate_id"),
    "events":      ("event_id",),
    "watchpoints": ("watch_id",),
}


def upsert(state: Any, table: str, row: dict[str, Any], *,
           replace: bool = True) -> int:
    """Insert one row, replacing an existing one with the same key by default.

    `replace=False` inserts strictly. It exists because REPLACE semantics defeat a
    unique index rather than being stopped by one: on a conflict SQLite *deletes*
    the conflicting row and inserts the new one, so an `orch_runs` row written with
    `ok=1` would silently erase the completed run it collided with — removing the
    very evidence the constraint was added to protect. Opening a run therefore
    inserts strictly, and closing it is an UPDATE, which does respect the index.

    `INSERT OR REPLACE` is SQLite-only. Writing it directly meant the DDL was
    portable while every write path was not — so moving state to RDS for PostgreSQL
    would have failed at the first insert of the weekly run, after the feeds had
    been fetched and the topics scored. The promise was that the move is a DSN
    change; this is what makes that true.
    """
    if table not in CONFLICT_KEY:
        raise KeyError(f"no conflict key declared for {table!r}")
    cols = list(row)
    marks = ", ".join("?" for _ in cols)
    names = ", ".join(cols)
    key = CONFLICT_KEY[table]

    if not replace:
        sql = f"INSERT INTO {table} ({names}) VALUES ({marks})"
    elif getattr(state, "paramstyle", "qmark") == "qmark":
        sql = f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})"
    else:
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in key)
        conflict = ", ".join(key)
        sql = (f"INSERT INTO {table} ({names}) VALUES ({marks}) "
               f"ON CONFLICT ({conflict}) "
               + (f"DO UPDATE SET {sets}" if sets else "DO NOTHING"))
    return state.execute(sql, tuple(row[c] for c in cols))


def collisions(state: Any) -> list[str]:
    """Owned tables that already exist with the wrong shape.

    Checked *before* the DDL runs, not after. `CREATE TABLE IF NOT EXISTS` on a
    colliding name is a silent no-op, but the index statement that follows it is
    not — it fails with `no such column`, naming neither the table that collided
    nor the reason. Front-loading the check means the operator reads what actually
    happened instead of a symptom three statements downstream.
    """
    bad: list[str] = []
    for table, cols in OWNED.items():
        try:
            have = set(_columns(state, table))
        except Exception:  # noqa: BLE001
            continue
        if not have:                      # absent: the DDL below will create it
            continue
        missing = [c for c in cols if c not in have]
        if missing:
            bad.append(f"{table}: already exists without {missing} — another "
                       f"schema owns this name, so the CREATE would be a no-op")
    return bad


def migrate(state: Any, *, strict: bool = True) -> int:
    """Apply the DDL and verify the result. Safe to run on every boot."""
    pre = collisions(state)
    if pre and strict:
        raise RuntimeError("schema collision: " + "; ".join(pre))
    n = state.migrate(DDL)
    evolve(state)
    problems = verify(state)
    if problems and strict:
        raise RuntimeError("schema verification failed: " + "; ".join(problems))
    return n


# ---------------------------------------------------------------------------
#: Shapes that look like real credentials. Deliberately narrow: a scanner that
#: cries wolf gets switched off, and a switched-off scanner is worse than none.
PATTERNS: tuple[tuple[str, str], ...] = (
    ("BytePlus AK", r"\bAKAP[A-Za-z0-9]{20,}"),
    ("AWS-style AK", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Wisburg token", r"\bsk-[A-Za-z0-9]{24,}"),
    ("OpenAI key", r"\bsk-proj-[A-Za-z0-9_-]{20,}"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("Private key block", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ("Bare secret assignment", r"(?i)\b(secret_access_key|secret_key|password)\s*=\s*['\"][^'\"\s]{16,}"),
)

#: Paths that legitimately contain the patterns as documentation or as tests.
ALLOW = ("ideagen/schema.py", "tests/", "docs/", ".gitignore")


def secret_audit(root: Path | str = ".", *, tracked_only: bool = True) -> dict[str, Any]:
    """Scan the repository for credential-shaped strings.

    Defaults to git-tracked files: what matters is what would be published, and an
    untracked local scratch file is not that. Never prints a matched value —
    reporting a secret in order to warn about it is self-defeating.
    """
    root = Path(root)
    if tracked_only:
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files"],
                                 capture_output=True, text=True, timeout=30)
            files = [root / f for f in out.stdout.splitlines() if f]
        except Exception:  # noqa: BLE001
            files = [p for p in root.rglob("*") if p.is_file()]
    else:
        files = [p for p in root.rglob("*")
                 if p.is_file() and ".git/" not in str(p)]

    findings: list[dict[str, Any]] = []
    scanned = 0
    for f in files:
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        if any(rel.startswith(a) or a in rel for a in ALLOW):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for label, pat in PATTERNS:
            for m in re.finditer(pat, text):
                line = text[:m.start()].count("\n") + 1
                findings.append({"file": rel, "line": line, "kind": label})
    return {"scanned": scanned, "clean": not findings, "findings": findings,
            "allowlisted": list(ALLOW)}
