"""SQLite store. Single file, WAL, idempotent schema.

Everything the system ever sees is persisted here so that any day's decision can
be reconstructed from the database alone: the corpus that was available, the
factor scores computed from it, the ideas that came out, the orders those ideas
produced, and every mark applied afterwards.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================ corpus
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,      -- "<line>:<source_id>"
    line          TEXT NOT NULL,         -- market-daily | feed | ib | ...
    category      TEXT,                  -- wisburg get-report-detail category
    source_id     INTEGER,
    tier          INTEGER NOT NULL,
    title         TEXT,
    institution   TEXT,
    published_at  TEXT,                  -- ISO8601, source timestamp
    published_d   TEXT,                  -- YYYY-MM-DD in HKT
    ingested_at   TEXT NOT NULL,
    url           TEXT,
    summary       TEXT,
    body          TEXT,
    body_chars    INTEGER DEFAULT 0,
    content_hash  TEXT,                  -- dedupe across lines
    retrieval     TEXT,                  -- the exact MCP call that reproduces this
    meta          TEXT                   -- JSON
);
CREATE INDEX IF NOT EXISTS ix_doc_pub  ON documents(published_d);
CREATE INDEX IF NOT EXISTS ix_doc_line ON documents(line, published_d);
CREATE INDEX IF NOT EXISTS ix_doc_hash ON documents(content_hash);

-- Assets referenced by a document: chart images from the Wisburg chart library
-- and figures embedded inside report bodies. These are the only externally
-- verifiable URLs the corpus exposes — the platform is a client-rendered SPA with
-- no per-document canonical web URL, so a guessed permalink would be a false
-- citation. Ground truth is therefore (line, category, source_id) + content_hash
-- for the text, and these URLs for the figures.
CREATE TABLE IF NOT EXISTS assets (
    asset_id   TEXT PRIMARY KEY,          -- sha1(url)
    doc_id     TEXT NOT NULL,
    url        TEXT NOT NULL,
    kind       TEXT,                      -- chart | figure
    host       TEXT,
    caption    TEXT,
    title      TEXT,
    published_d TEXT,
    reachable  INTEGER,                   -- 1 ok, 0 checked and failed, NULL unchecked
    checked_at TEXT,
    bytes      INTEGER,
    content_type TEXT
);
CREATE INDEX IF NOT EXISTS ix_assets_doc ON assets(doc_id);

-- ============================================================ runs
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    as_of       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,           -- running | ok | failed | partial
    stages      TEXT,                    -- JSON list of {stage,status,ms,note}
    note        TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_asof ON runs(as_of);

-- ============================================================ macro layer
CREATE TABLE IF NOT EXISTS themes (
    as_of        TEXT NOT NULL,
    theme_id     TEXT NOT NULL,
    label        TEXT NOT NULL,
    key_question TEXT,
    tis          REAL,                   -- Tactical Impact Score
    d            REAL, a REAL, b REAL, n REAL,
    m            REAL,                   -- market validation (independent)
    c            REAL,                   -- crowding (independent, v0.4)
    tier         TEXT,                   -- core | important | watch | background
    n_items      INTEGER,
    n_sources    INTEGER,
    confidence   TEXT,
    factors      TEXT,                   -- JSON: full sub-factor breakdown
    evidence     TEXT,                   -- JSON: [{doc_id,line,tier,stance,fact_type,...}]
    PRIMARY KEY (as_of, theme_id)
);

CREATE TABLE IF NOT EXISTS transmissions (
    as_of           TEXT NOT NULL,
    transmission_id TEXT NOT NULL,
    theme_id        TEXT NOT NULL,
    label           TEXT NOT NULL,
    PRIMARY KEY (as_of, transmission_id)
);

CREATE TABLE IF NOT EXISTS signals (
    as_of           TEXT NOT NULL,
    signal_id       TEXT NOT NULL,
    theme_id        TEXT NOT NULL,
    transmission_id TEXT,
    asset           TEXT NOT NULL,
    direction       TEXT NOT NULL,       -- ↑ | ↓
    horizon         TEXT NOT NULL,       -- 1个月 | 6个月
    gate            TEXT,
    price_indicator TEXT,                -- pre-registered M indicator (futu code)
    PRIMARY KEY (as_of, signal_id)
);

-- ============================================================ ideas
CREATE TABLE IF NOT EXISTS batches (
    batch_id      TEXT PRIMARY KEY,
    as_of         TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    generator     TEXT NOT NULL,         -- claude-code | seed-import | ...
    methodology   TEXT NOT NULL,
    n_ideas       INTEGER,
    prompt_sha    TEXT,
    output_sha    TEXT,
    validation    TEXT,                  -- JSON report from ideas.validate_batch
    status        TEXT NOT NULL,          -- draft | validated | rejected | traded
    note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_batch_asof ON batches(as_of);

CREATE TABLE IF NOT EXISTS ideas (
    idea_uid      TEXT PRIMARY KEY,      -- "<batch_id>#<local_id>"
    batch_id      TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    as_of         TEXT NOT NULL,
    local_id      INTEGER NOT NULL,
    rank          INTEGER,
    tool          TEXT NOT NULL,
    tool_desc     TEXT,
    vehicle       TEXT,
    theme         TEXT,
    theme_id      TEXT,
    signal_id     TEXT,
    asset         TEXT,
    direction     TEXT,
    horizon       TEXT NOT NULL,
    horizon_months INTEGER NOT NULL,
    action        TEXT,
    instrument    TEXT NOT NULL,         -- listed | fund | structured | monitor
    futu_code     TEXT,
    olive_key     TEXT,
    ref_price     REAL,
    ref_price_d   TEXT,
    entry_lo      REAL, entry_hi REAL, entry_break REAL,
    take_lo       REAL, take_hi REAL, stop_px REAL,
    entry_src     TEXT, take_src TEXT, stop_src TEXT,   -- formula|research_judgment|hybrid
    hurdle        REAL NOT NULL,
    hurdle_rf     REAL, hurdle_lp REAL,
    central_p     TEXT, central_r TEXT,
    conserv_p     TEXT, conserv_r TEXT,
    ev_c REAL, gain_c REAL, loss_c REAL, or_c REAL,
    ev_k REAL, gain_k REAL, loss_k REAL, or_k REAL,
    sigma_h       REAL,                  -- horizon realised vol used for the sanity band
    vol_check     TEXT,                  -- ok | wide | narrow
    grade         TEXT, grade_rule TEXT, grade_rel TEXT,
    pos_init      REAL, pos_max REAL,
    view TEXT, thesis TEXT, fit TEXT, risk TEXT, role TEXT,
    sources       TEXT,                  -- JSON list of doc_ids / external refs
    raw           TEXT                   -- JSON: full generator output
);
CREATE INDEX IF NOT EXISTS ix_ideas_batch ON ideas(batch_id);
CREATE INDEX IF NOT EXISTS ix_ideas_asof  ON ideas(as_of);
CREATE INDEX IF NOT EXISTS ix_ideas_code  ON ideas(futu_code);

-- ============================================================ pricing
CREATE TABLE IF NOT EXISTS prices (
    code   TEXT NOT NULL,
    d      TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL, volume REAL,
    src    TEXT,
    PRIMARY KEY (code, d)
);
CREATE TABLE IF NOT EXISTS navs (
    olive_key TEXT NOT NULL,
    d         TEXT NOT NULL,
    nav       REAL NOT NULL,
    src       TEXT,
    PRIMARY KEY (olive_key, d)
);
CREATE TABLE IF NOT EXISTS instruments (
    key         TEXT PRIMARY KEY,        -- ticker or olive id
    kind        TEXT NOT NULL,           -- listed | fund | structured
    futu_code   TEXT,
    olive_key   TEXT,
    name        TEXT,
    market      TEXT,
    currency    TEXT DEFAULT 'USD',
    priceable   INTEGER DEFAULT 1,
    aum         REAL,
    expense     REAL,
    meta        TEXT,
    updated_at  TEXT
);

-- ============================================================ paper book
CREATE TABLE IF NOT EXISTS books (
    book_id  TEXT PRIMARY KEY,
    label    TEXT, descr TEXT,
    capital  REAL NOT NULL,
    sizing   TEXT, entry TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    book_id    TEXT NOT NULL,
    idea_uid   TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    side       TEXT NOT NULL,            -- BUY | SELL
    code       TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- band | breakout | market_close | nav
    band_lo    REAL, band_hi REAL, trigger REAL,
    notional   REAL,
    placed_d   TEXT NOT NULL,
    expire_d   TEXT,
    status     TEXT NOT NULL,            -- pending | filled | expired | cancelled
    fill_d     TEXT, fill_px REAL, fill_qty REAL, fee REAL,
    fill_rule  TEXT,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS ix_ord_book ON orders(book_id, status);

CREATE TABLE IF NOT EXISTS positions (
    pos_id      TEXT PRIMARY KEY,
    book_id     TEXT NOT NULL,
    idea_uid    TEXT NOT NULL,
    code        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    theme       TEXT,
    horizon     TEXT,
    grade       TEXT,
    qty         REAL NOT NULL,
    avg_px      REAL NOT NULL,
    cost        REAL NOT NULL,
    opened_d    TEXT NOT NULL,
    horizon_end TEXT,
    stop_px     REAL, take_px REAL,
    status      TEXT NOT NULL,           -- open | closed
    closed_d    TEXT, close_px REAL,
    realized    REAL DEFAULT 0,
    fees        REAL DEFAULT 0,
    exit_reason TEXT,
    peak_px     REAL, trough_px REAL
);
CREATE INDEX IF NOT EXISTS ix_pos_book ON positions(book_id, status);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    book_id  TEXT NOT NULL,
    pos_id   TEXT,
    idea_uid TEXT,
    d        TEXT NOT NULL,
    side     TEXT NOT NULL,
    code     TEXT NOT NULL,
    qty      REAL, px REAL, gross REAL, fee REAL, cash_delta REAL,
    reason   TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_book ON trades(book_id, d);

CREATE TABLE IF NOT EXISTS equity (
    book_id TEXT NOT NULL,
    d       TEXT NOT NULL,
    cash    REAL, mv REAL, equity REAL,
    ret_d   REAL, cum_ret REAL, drawdown REAL,
    n_open  INTEGER, gross REAL,
    PRIMARY KEY (book_id, d)
);

CREATE TABLE IF NOT EXISTS mtm (
    book_id TEXT NOT NULL,
    pos_id  TEXT NOT NULL,
    d       TEXT NOT NULL,
    px      REAL, mv REAL, upnl REAL, upnl_pct REAL,
    PRIMARY KEY (book_id, pos_id, d)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    book_id  TEXT, d TEXT NOT NULL,
    level    TEXT NOT NULL,             -- info | warn | action
    kind     TEXT NOT NULL,
    idea_uid TEXT, code TEXT,
    message  TEXT NOT NULL,
    acked    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_d ON alerts(d);

-- ============================================================ calibration
CREATE TABLE IF NOT EXISTS outcomes (
    idea_uid   TEXT PRIMARY KEY,
    as_of      TEXT, horizon TEXT, horizon_end TEXT,
    grade      TEXT, or_c REAL, or_k REAL, ev_c REAL,
    entry_px   REAL, exit_px REAL,
    realized   REAL,                    -- holding-period total return, net of costs
    bench_ret  REAL, excess REAL,
    scenario   TEXT,                    -- up | base | down (which bucket it landed in)
    brier_c    REAL, brier_k REAL,
    filled     INTEGER, exit_reason TEXT,
    sessions_held INTEGER,
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init(path: Path | str | None = None) -> sqlite3.Connection:
    con = connect(path)
    con.executescript(SCHEMA)
    _ensure_books(con)
    return con


def _ensure_books(con: sqlite3.Connection) -> None:
    from .config import BOOKS, now_hkt

    for bid, spec in BOOKS.items():
        con.execute(
            "INSERT OR IGNORE INTO books(book_id,label,descr,capital,sizing,entry,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (bid, spec["label"], spec["desc"], spec["capital"], spec["sizing"],
             spec["entry"], now_hkt().isoformat()),
        )


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    con.execute("BEGIN")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- helpers
def upsert(con: sqlite3.Connection, table: str, row: dict[str, Any], keys: Sequence[str]) -> None:
    cols = list(row)
    ph = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) "
        f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
        if updates
        else f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({ph})"
    )
    con.execute(sql, [_enc(row[c]) for c in cols])


def upsert_many(con: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]],
                keys: Sequence[str],
                keep_if_blank: Sequence[str] = ()) -> int:
    """`keep_if_blank` columns only overwrite when the incoming value is
    non-blank (not NULL/''/0). A shallow re-listing of a document the pipeline
    already deep-fetched arrives with an empty body; without this guard the
    upsert's set-every-column rule quietly erases the expensive fetch — which
    is exactly what happened to 442 of 654 archived report bodies before this
    parameter existed."""
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0])
    ph = ",".join("?" * len(cols))
    def _upd(c: str) -> str:
        if c in keep_if_blank:
            return (f"{c}=CASE WHEN excluded.{c} IS NULL OR excluded.{c}='' "
                    f"OR excluded.{c}=0 THEN {table}.{c} ELSE excluded.{c} END")
        return f"{c}=excluded.{c}"
    updates = ",".join(_upd(c) for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) "
        f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
        if updates
        else f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({ph})"
    )
    con.executemany(sql, [[_enc(r.get(c)) for c in cols] for r in rows])
    return len(rows)


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def jl(v: Any, default: Any = None) -> Any:
    """Decode a JSON column, tolerating NULL and already-decoded values."""
    if v is None or v == "":
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def kv_set(con: sqlite3.Connection, k: str, v: Any) -> None:
    from .config import now_hkt

    con.execute(
        "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (k, json.dumps(v, ensure_ascii=False), now_hkt().isoformat()),
    )


def kv_get(con: sqlite3.Connection, k: str, default: Any = None) -> Any:
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return jl(r["v"], default) if r else default


def q(con: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return con.execute(sql, args).fetchall()


def q1(con: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
    return con.execute(sql, args).fetchone()
