"""Portable DDL for the tables the new layer writes, plus a secrets audit.

The pipeline's existing twenty tables are SQLite-specific and already correct;
these are the tables the registry-based run adds. SQLite and PostgreSQL share
the portable DDL below. MySQL needs a separate shape because it cannot index an
unbounded TEXT primary key and has no partial indexes; both variants expose the
same columns and invariants to the application.

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
         calls       INTEGER DEFAULT 0,
         data_classification TEXT
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
         feed        TEXT,
         payload     TEXT
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

    """CREATE TABLE IF NOT EXISTS backtest_runs (
         backtest_id         TEXT PRIMARY KEY,
         as_of               TEXT NOT NULL,
         window_start        TEXT NOT NULL,
         window_end          TEXT NOT NULL,
         methodology         TEXT NOT NULL,
         data_classification TEXT NOT NULL,
         model_id            TEXT,
         model_release_date  TEXT,
         knowledge_cutoff    TEXT,
         inputs_sha          TEXT NOT NULL,
         artifact_uri        TEXT,
         started_at          TEXT,
         ended_at            TEXT,
         ok                  INTEGER,
         error               TEXT,
         summary             TEXT
       )""",
    "CREATE INDEX IF NOT EXISTS backtest_runs_as_of ON backtest_runs (as_of)",

    """CREATE TABLE IF NOT EXISTS backtest_points (
         backtest_id TEXT NOT NULL,
         arm         TEXT NOT NULL,
         d           TEXT NOT NULL,
         equity      REAL NOT NULL,
         period_ret  REAL,
         drawdown    REAL,
         n_positions INTEGER DEFAULT 0,
         PRIMARY KEY (backtest_id, arm, d)
       )""",

    """CREATE TABLE IF NOT EXISTS backtest_positions (
         backtest_id  TEXT NOT NULL,
         arm          TEXT NOT NULL,
         period       TEXT NOT NULL,
         instrument_id TEXT NOT NULL,
         entry_d      TEXT,
         exit_d       TEXT,
         entry_nav    REAL,
         exit_nav     REAL,
         return_pct   REAL,
         status       TEXT,
         thesis       TEXT,
         PRIMARY KEY (backtest_id, arm, period, instrument_id)
       )""",

    # Versioned product-shelf snapshots. The complete source document lives in
    # the immutable blob store; these tables are the queryable, deliberately
    # narrow projection used by weekly generation, paper marking and Dashboard.
    """CREATE TABLE IF NOT EXISTS shelf_snapshots (
         snapshot_id         TEXT PRIMARY KEY,
         as_of               TEXT NOT NULL,
         source              TEXT NOT NULL,
         data_classification TEXT NOT NULL,
         captured_at         TEXT NOT NULL,
         artifact_uri        TEXT,
         inputs_sha          TEXT NOT NULL,
         item_count          INTEGER DEFAULT 0,
         nav_count           INTEGER DEFAULT 0,
         ok                  INTEGER,
         error               TEXT
       )""",
    "CREATE INDEX IF NOT EXISTS shelf_snapshots_as_of "
    "ON shelf_snapshots (as_of, source)",

    """CREATE TABLE IF NOT EXISTS shelf_instruments (
         snapshot_id  TEXT NOT NULL,
         instrument_id TEXT NOT NULL,
         as_of        TEXT NOT NULL,
         name         TEXT,
         kind         TEXT NOT NULL,
         group_name   TEXT,
         currency     TEXT,
         vehicle      TEXT,
         exposure     TEXT,
         risk_level   TEXT,
         strategy     TEXT,
         first_seen_d TEXT,
         latest_nav   REAL,
         nav_d        TEXT,
         metadata     TEXT,
         PRIMARY KEY (snapshot_id, instrument_id)
       )""",
    "CREATE INDEX IF NOT EXISTS shelf_instruments_as_of "
    "ON shelf_instruments (as_of, instrument_id)",

    """CREATE TABLE IF NOT EXISTS shelf_navs (
         instrument_id       TEXT NOT NULL,
         d                   TEXT NOT NULL,
         nav                 REAL NOT NULL,
         snapshot_id         TEXT NOT NULL,
         source              TEXT NOT NULL,
         data_classification TEXT NOT NULL,
         PRIMARY KEY (instrument_id, d, data_classification)
       )""",
    "CREATE INDEX IF NOT EXISTS shelf_navs_d ON shelf_navs (d)",

    # Portable corpus projection. Verbatim licensed text is archived privately in
    # the blob store; SQL carries only the bounded text needed by the current
    # weekly run plus a hash/retrieval receipt.
    """CREATE TABLE IF NOT EXISTS corpus_documents (
         doc_id              TEXT PRIMARY KEY,
         published_d         TEXT NOT NULL,
         title               TEXT NOT NULL,
         tier                INTEGER NOT NULL,
         line                TEXT,
         institution         TEXT,
         summary             TEXT,
         body                TEXT,
         content_hash        TEXT,
         retrieval           TEXT,
         raw_uri             TEXT,
         data_classification TEXT NOT NULL,
         ingested_at         TEXT NOT NULL,
         metadata            TEXT
       )""",
    "CREATE INDEX IF NOT EXISTS corpus_documents_published "
    "ON corpus_documents (published_d, tier)",

    # Cloud paper book. These tables intentionally model the fund/NAV execution
    # needed by the POC rather than cloning the legacy OHLC engine: orders fill at
    # an observed NAV, positions mark only from stored NAV rows, and every result
    # remains reconstructible after the container disappears.
    """CREATE TABLE IF NOT EXISTS paper_books (
         book_id             TEXT PRIMARY KEY,
         selector            TEXT NOT NULL,
         label               TEXT,
         capital             REAL NOT NULL,
         data_classification TEXT NOT NULL,
         created_at          TEXT NOT NULL,
         updated_at          TEXT NOT NULL
       )""",

    """CREATE TABLE IF NOT EXISTS paper_orders (
         order_id            TEXT PRIMARY KEY,
         book_id             TEXT NOT NULL,
         run_id              TEXT NOT NULL,
         selector            TEXT NOT NULL,
         candidate_id        TEXT NOT NULL,
         instrument_id       TEXT NOT NULL,
         as_of               TEXT NOT NULL,
         notional            REAL NOT NULL,
         status              TEXT NOT NULL,
         placed_at           TEXT NOT NULL,
         fill_d              TEXT,
         fill_nav            REAL,
         fill_qty            REAL,
         snapshot_id         TEXT,
         data_classification TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS paper_orders_book "
    "ON paper_orders (book_id, status, as_of)",

    """CREATE TABLE IF NOT EXISTS paper_positions (
         pos_id              TEXT PRIMARY KEY,
         book_id             TEXT NOT NULL,
         order_id            TEXT NOT NULL,
         run_id              TEXT NOT NULL,
         candidate_id        TEXT NOT NULL,
         instrument_id       TEXT NOT NULL,
         opened_d            TEXT NOT NULL,
         horizon_end         TEXT,
         qty                 REAL NOT NULL,
         entry_nav           REAL NOT NULL,
         cost                REAL NOT NULL,
         current_nav         REAL,
         mark_d              TEXT,
         market_value        REAL,
         unrealized          REAL,
         unrealized_pct      REAL,
         status              TEXT NOT NULL,
         closed_d            TEXT,
         exit_nav            REAL,
         realized            REAL,
         exit_reason         TEXT,
         thesis              TEXT,
         data_classification TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS paper_positions_book "
    "ON paper_positions (book_id, status)",

    """CREATE TABLE IF NOT EXISTS paper_marks (
         pos_id         TEXT NOT NULL,
         d              TEXT NOT NULL,
         nav            REAL NOT NULL,
         market_value   REAL NOT NULL,
         unrealized     REAL NOT NULL,
         unrealized_pct REAL,
         stale_days     INTEGER DEFAULT 0,
         snapshot_id    TEXT,
         PRIMARY KEY (pos_id, d)
       )""",

    """CREATE TABLE IF NOT EXISTS paper_equity (
         book_id   TEXT NOT NULL,
         d         TEXT NOT NULL,
         cash      REAL NOT NULL,
         market_value REAL NOT NULL,
         equity    REAL NOT NULL,
         return_pct REAL,
         drawdown  REAL,
         n_open    INTEGER DEFAULT 0,
         PRIMARY KEY (book_id, d)
       )""",

    """CREATE TABLE IF NOT EXISTS paper_alerts (
         alert_id     TEXT PRIMARY KEY,
         book_id     TEXT NOT NULL,
         pos_id      TEXT,
         d           TEXT NOT NULL,
         level       TEXT NOT NULL,
         kind        TEXT NOT NULL,
         message     TEXT NOT NULL,
         acknowledged INTEGER DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS paper_alerts_d "
    "ON paper_alerts (d, level)",
)


# MySQL cannot use TEXT columns as unbounded primary keys, does not support
# `CREATE INDEX IF NOT EXISTS`, and has no PostgreSQL-style partial indexes.
# Indexes therefore live inside idempotent CREATE TABLE statements. The generated
# nullable column preserves the "only one successful weekly run per as_of" rule:
# MySQL permits many NULLs in a UNIQUE key but only one concrete date.
MYSQL_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS orch_runs (
         run_id      VARCHAR(64) PRIMARY KEY,
         as_of       VARCHAR(10) NOT NULL,
         kind        VARCHAR(32) NOT NULL,
         platform    VARCHAR(32),
         started_at  VARCHAR(40),
         ended_at    VARCHAR(40),
         ok          INTEGER,
         error       TEXT,
         inputs_sha  VARCHAR(128),
         journal_uri TEXT,
         calls       INTEGER DEFAULT 0,
         data_classification VARCHAR(64),
         completed_weekly_as_of VARCHAR(10)
           GENERATED ALWAYS AS (
             CASE WHEN ok = 1 AND kind = 'weekly' THEN as_of ELSE NULL END
           ) STORED,
         KEY orch_runs_as_of (as_of),
         UNIQUE KEY orch_runs_done (completed_weekly_as_of)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS feed_runs (
         run_id   VARCHAR(64) NOT NULL,
         feed     VARCHAR(128) NOT NULL,
         kind     VARCHAR(64) NOT NULL,
         as_of    VARCHAR(10) NOT NULL,
         n_rows   INTEGER DEFAULT 0,
         ok       INTEGER,
         error    TEXT,
         rows_sha VARCHAR(128),
         PRIMARY KEY (run_id, feed)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS verdicts (
         run_id     VARCHAR(64) NOT NULL,
         as_of      VARCHAR(10) NOT NULL,
         kind       VARCHAR(64) NOT NULL,
         strategy   VARCHAR(128) NOT NULL,
         version    VARCHAR(64) NOT NULL,
         role       VARCHAR(32),
         inputs_sha VARCHAR(128),
         chosen     LONGTEXT,
         scores     LONGTEXT,
         rejected   LONGTEXT,
         meta       LONGTEXT,
         calls      INTEGER DEFAULT 0,
         PRIMARY KEY (run_id, kind, strategy),
         KEY verdicts_strategy (strategy, as_of)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS candidates (
         run_id        VARCHAR(64) NOT NULL,
         candidate_id  VARCHAR(128) NOT NULL,
         as_of         VARCHAR(10) NOT NULL,
         instrument_id VARCHAR(128),
         topic_id      VARCHAR(128),
         method        VARCHAR(64),
         direction     VARCHAR(16),
         upside_pct    DOUBLE,
         downside_pct  DOUBLE,
         p_up          DOUBLE,
         p_base        DOUBLE,
         p_down        DOUBLE,
         sigma_1m      DOUBLE,
         payload       LONGTEXT,
         PRIMARY KEY (run_id, candidate_id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS events (
         event_id    VARCHAR(128) PRIMARY KEY,
         date        VARCHAR(10) NOT NULL,
         label       TEXT,
         kind        VARCHAR(64),
         expectation TEXT,
         actual      TEXT,
         unit        VARCHAR(64),
         source      VARCHAR(128),
         as_of       VARCHAR(10),
         feed        VARCHAR(128),
         payload     TEXT,
         KEY events_date (date)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS watchpoints (
         watch_id     VARCHAR(128) PRIMARY KEY,
         run_id       VARCHAR(64),
         candidate_id VARCHAR(128),
         event_id     VARCHAR(128),
         expectation  TEXT,
         deviation    TEXT,
         action       TEXT,
         status       VARCHAR(32) DEFAULT 'armed',
         resolved_d   VARCHAR(10),
         outcome      TEXT,
         KEY watchpoints_event (event_id, status)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS backtest_runs (
         backtest_id         VARCHAR(96) PRIMARY KEY,
         as_of               VARCHAR(10) NOT NULL,
         window_start        VARCHAR(10) NOT NULL,
         window_end          VARCHAR(10) NOT NULL,
         methodology         VARCHAR(128) NOT NULL,
         data_classification VARCHAR(64) NOT NULL,
         model_id            VARCHAR(128),
         model_release_date  VARCHAR(10),
         knowledge_cutoff    VARCHAR(10),
         inputs_sha          VARCHAR(128) NOT NULL,
         artifact_uri        TEXT,
         started_at          VARCHAR(40),
         ended_at            VARCHAR(40),
         ok                  INTEGER,
         error               TEXT,
         summary             LONGTEXT,
         KEY backtest_runs_as_of (as_of)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS backtest_points (
         backtest_id VARCHAR(96) NOT NULL,
         arm         VARCHAR(64) NOT NULL,
         d           VARCHAR(10) NOT NULL,
         equity      DOUBLE NOT NULL,
         period_ret  DOUBLE,
         drawdown    DOUBLE,
         n_positions INTEGER DEFAULT 0,
         PRIMARY KEY (backtest_id, arm, d)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS backtest_positions (
         backtest_id   VARCHAR(96) NOT NULL,
         arm           VARCHAR(64) NOT NULL,
         period        VARCHAR(10) NOT NULL,
         instrument_id VARCHAR(128) NOT NULL,
         entry_d       VARCHAR(10),
         exit_d        VARCHAR(10),
         entry_nav     DOUBLE,
         exit_nav      DOUBLE,
         return_pct    DOUBLE,
         status        VARCHAR(64),
         thesis        TEXT,
         PRIMARY KEY (backtest_id, arm, period, instrument_id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS shelf_snapshots (
         snapshot_id         VARCHAR(96) PRIMARY KEY,
         as_of               VARCHAR(10) NOT NULL,
         source              VARCHAR(64) NOT NULL,
         data_classification VARCHAR(64) NOT NULL,
         captured_at         VARCHAR(40) NOT NULL,
         artifact_uri        TEXT,
         inputs_sha          VARCHAR(128) NOT NULL,
         item_count          INTEGER DEFAULT 0,
         nav_count           INTEGER DEFAULT 0,
         ok                  INTEGER,
         error               TEXT,
         KEY shelf_snapshots_as_of (as_of, source)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS shelf_instruments (
         snapshot_id  VARCHAR(96) NOT NULL,
         instrument_id VARCHAR(128) NOT NULL,
         as_of        VARCHAR(10) NOT NULL,
         name         TEXT,
         kind         VARCHAR(32) NOT NULL,
         group_name   VARCHAR(32),
         currency     VARCHAR(16),
         vehicle      VARCHAR(64),
         exposure     TEXT,
         risk_level   VARCHAR(64),
         strategy     TEXT,
         first_seen_d VARCHAR(10),
         latest_nav   DOUBLE,
         nav_d        VARCHAR(10),
         metadata     LONGTEXT,
         PRIMARY KEY (snapshot_id, instrument_id),
         KEY shelf_instruments_as_of (as_of, instrument_id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS shelf_navs (
         instrument_id       VARCHAR(128) NOT NULL,
         d                   VARCHAR(10) NOT NULL,
         nav                 DOUBLE NOT NULL,
         snapshot_id         VARCHAR(96) NOT NULL,
         source              VARCHAR(64) NOT NULL,
         data_classification VARCHAR(64) NOT NULL,
         PRIMARY KEY (instrument_id, d, data_classification),
         KEY shelf_navs_d (d)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS corpus_documents (
         doc_id              VARCHAR(160) PRIMARY KEY,
         published_d         VARCHAR(10) NOT NULL,
         title               TEXT NOT NULL,
         tier                INTEGER NOT NULL,
         line                VARCHAR(64),
         institution         TEXT,
         summary             LONGTEXT,
         body                LONGTEXT,
         content_hash        VARCHAR(128),
         retrieval           TEXT,
         raw_uri             TEXT,
         data_classification VARCHAR(64) NOT NULL,
         ingested_at         VARCHAR(40) NOT NULL,
         metadata            LONGTEXT,
         KEY corpus_documents_published (published_d, tier)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS paper_books (
         book_id             VARCHAR(128) PRIMARY KEY,
         selector            VARCHAR(128) NOT NULL,
         label               TEXT,
         capital             DOUBLE NOT NULL,
         data_classification VARCHAR(64) NOT NULL,
         created_at          VARCHAR(40) NOT NULL,
         updated_at          VARCHAR(40) NOT NULL
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS paper_orders (
         order_id            VARCHAR(64) PRIMARY KEY,
         book_id             VARCHAR(128) NOT NULL,
         run_id              VARCHAR(64) NOT NULL,
         selector            VARCHAR(128) NOT NULL,
         candidate_id        VARCHAR(128) NOT NULL,
         instrument_id       VARCHAR(128) NOT NULL,
         as_of               VARCHAR(10) NOT NULL,
         notional            DOUBLE NOT NULL,
         status              VARCHAR(32) NOT NULL,
         placed_at           VARCHAR(40) NOT NULL,
         fill_d              VARCHAR(10),
         fill_nav            DOUBLE,
         fill_qty            DOUBLE,
         snapshot_id         VARCHAR(96),
         data_classification VARCHAR(64) NOT NULL,
         KEY paper_orders_book (book_id, status, as_of)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS paper_positions (
         pos_id              VARCHAR(64) PRIMARY KEY,
         book_id             VARCHAR(128) NOT NULL,
         order_id            VARCHAR(64) NOT NULL,
         run_id              VARCHAR(64) NOT NULL,
         candidate_id        VARCHAR(128) NOT NULL,
         instrument_id       VARCHAR(128) NOT NULL,
         opened_d            VARCHAR(10) NOT NULL,
         horizon_end         VARCHAR(10),
         qty                 DOUBLE NOT NULL,
         entry_nav           DOUBLE NOT NULL,
         cost                DOUBLE NOT NULL,
         current_nav         DOUBLE,
         mark_d              VARCHAR(10),
         market_value        DOUBLE,
         unrealized          DOUBLE,
         unrealized_pct      DOUBLE,
         status              VARCHAR(32) NOT NULL,
         closed_d            VARCHAR(10),
         exit_nav            DOUBLE,
         realized            DOUBLE,
         exit_reason         VARCHAR(64),
         thesis              TEXT,
         data_classification VARCHAR(64) NOT NULL,
         KEY paper_positions_book (book_id, status)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS paper_marks (
         pos_id         VARCHAR(64) NOT NULL,
         d              VARCHAR(10) NOT NULL,
         nav            DOUBLE NOT NULL,
         market_value   DOUBLE NOT NULL,
         unrealized     DOUBLE NOT NULL,
         unrealized_pct DOUBLE,
         stale_days     INTEGER DEFAULT 0,
         snapshot_id    VARCHAR(96),
         PRIMARY KEY (pos_id, d)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS paper_equity (
         book_id     VARCHAR(128) NOT NULL,
         d           VARCHAR(10) NOT NULL,
         cash        DOUBLE NOT NULL,
         market_value DOUBLE NOT NULL,
         equity      DOUBLE NOT NULL,
         return_pct  DOUBLE,
         drawdown    DOUBLE,
         n_open      INTEGER DEFAULT 0,
         PRIMARY KEY (book_id, d)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS paper_alerts (
         alert_id       VARCHAR(64) PRIMARY KEY,
         book_id       VARCHAR(128) NOT NULL,
         pos_id        VARCHAR(64),
         d             VARCHAR(10) NOT NULL,
         level         VARCHAR(32) NOT NULL,
         kind          VARCHAR(64) NOT NULL,
         message       TEXT NOT NULL,
         acknowledged  INTEGER DEFAULT 0,
         KEY paper_alerts_d (d, level)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
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
    "backtest_runs": ("backtest_id", "as_of", "window_start", "window_end",
                      "methodology", "data_classification", "inputs_sha",
                      "summary"),
    "backtest_points": ("backtest_id", "arm", "d", "equity", "drawdown"),
    "backtest_positions": ("backtest_id", "arm", "period", "instrument_id",
                           "status"),
    "shelf_snapshots": ("snapshot_id", "as_of", "source",
                        "data_classification", "inputs_sha", "ok"),
    "shelf_instruments": ("snapshot_id", "instrument_id", "as_of", "kind",
                          "metadata"),
    "shelf_navs": ("instrument_id", "d", "nav", "snapshot_id",
                   "data_classification"),
    "corpus_documents": ("doc_id", "published_d", "title", "tier",
                         "data_classification", "ingested_at"),
    "paper_books": ("book_id", "selector", "capital",
                    "data_classification"),
    "paper_orders": ("order_id", "book_id", "run_id", "candidate_id",
                     "instrument_id", "status"),
    "paper_positions": ("pos_id", "book_id", "order_id", "instrument_id",
                        "entry_nav", "status"),
    "paper_marks": ("pos_id", "d", "nav", "market_value"),
    "paper_equity": ("book_id", "d", "cash", "equity", "drawdown"),
    "paper_alerts": ("alert_id", "book_id", "d", "level", "kind"),
}


#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` cannot
#: add a column to an existing table, and SQLite has no `ADD COLUMN IF NOT EXISTS`,
#: so evolving a table means checking what is there and adding what is not. Kept as
#: data rather than hand-written migrations because the check has to run on every
#: boot: a deploy that skipped one migration must self-heal, not fail at insert.
ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("orch_runs", "data_classification", "TEXT"),
    ("candidates", "topic_id", "TEXT"),
    ("candidates", "method",   "TEXT"),
    # When an instrument first appeared on the shelf. Without it the universe has
    # no as-of: a replay of July sees August's shelf, so a July thesis can be
    # expressed through a product that did not exist that month — the strategy
    # then looks prescient for a reason that has nothing to do with the strategy.
    # Rows predating this column stay NULL, meaning "unknown", and `universe.eligible`
    # counts them rather than assuming they were always there.
    ("instruments", "first_seen_d", "TEXT"),
    # The verbatim archive (sources/wisburg.py): sha256 of the detail markdown as
    # served, and the content-addressed blob key holding those exact bytes.
    # `content_hash` stays untouched — it is a normalised title+summary digest
    # serving near-dup detection, a different job from proving what the vendor
    # sent. Rows never deep-fetched stay NULL.
    ("documents", "body_sha256", "TEXT"),
    ("documents", "raw_uri", "TEXT"),
    # The vintage a position belongs to. `opened_d` records the session it
    # filled in, which is the same thing only when the run happened on time;
    # a catch-up run stamps every period it books with that one afternoon.
    # Existing rows stay NULL until `scripts/backfill_position_periods.py`
    # derives them from the order that opened them.
    ("positions", "as_of", "TEXT"),
    # Everything a calendar feed reported that `events` has no column for. The
    # table was designed around a release with an expectation, and the feeds
    # added on 2026-09-05 carry fields it never anticipated: an impact rating and
    # a previous print on a macro release, net position and reversal trend on a
    # COT row, the single-name concentration behind an aggregated congressional
    # flow. Without somewhere to put them the upsert drops them, and a replay
    # then reads a thinner row than the run was actually handed — the run and its
    # record stop being the same thing, which is the one property `backtest.py`
    # depends on when it re-reads `events` for an old period.
    ("events", "payload", "TEXT"),
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
    dialect = getattr(state, "dialect", "sqlite")
    if dialect == "sqlite":
        return [r["name"] for r in state.q(f"PRAGMA table_info({table})")]
    if dialect == "mysql":
        return [r["column_name"] for r in state.q(
            "SELECT column_name AS column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=?", (table,))]
    return [r["column_name"] for r in state.q(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name=?", (table,))]


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
    "backtest_runs": ("backtest_id",),
    "backtest_points": ("backtest_id", "arm", "d"),
    "backtest_positions": ("backtest_id", "arm", "period", "instrument_id"),
    "shelf_snapshots": ("snapshot_id",),
    "shelf_instruments": ("snapshot_id", "instrument_id"),
    "shelf_navs": ("instrument_id", "d", "data_classification"),
    "corpus_documents": ("doc_id",),
    "paper_books": ("book_id",),
    "paper_orders": ("order_id",),
    "paper_positions": ("pos_id",),
    "paper_marks": ("pos_id", "d"),
    "paper_equity": ("book_id", "d"),
    "paper_alerts": ("alert_id",),
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

    The adapters deliberately use each engine's native conflict form:
    `INSERT OR REPLACE` for SQLite, `ON CONFLICT` for PostgreSQL, and
    `ON DUPLICATE KEY UPDATE` for MySQL. Keeping that choice here prevents a
    cloud migration from failing at the first write after feeds and model calls
    have already spent time and budget.
    """
    if table not in CONFLICT_KEY:
        raise KeyError(f"no conflict key declared for {table!r}")
    cols = list(row)
    marks = ", ".join("?" for _ in cols)
    names = ", ".join(cols)
    key = CONFLICT_KEY[table]

    if not replace:
        sql = f"INSERT INTO {table} ({names}) VALUES ({marks})"
    elif getattr(state, "dialect", "sqlite") == "sqlite":
        sql = f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})"
    elif getattr(state, "dialect", "") == "mysql":
        sets = ", ".join(f"{c}=VALUES({c})" for c in cols if c not in key)
        if not sets:
            sets = f"{key[0]}={key[0]}"
        sql = (f"INSERT INTO {table} ({names}) VALUES ({marks}) "
               f"ON DUPLICATE KEY UPDATE {sets}")
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
    ddl = MYSQL_DDL if getattr(state, "dialect", "") == "mysql" else DDL
    n = state.migrate(ddl)
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
