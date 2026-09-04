"""A database this module creates must have the columns this code writes to.

`db.init()` runs db.py's own SCHEMA. `schema.py` separately declares columns that
were added later, and `schema.evolve` applies them — to the platform state store.
On the local platform those happen to be the same SQLite file, so on a machine
that has run almost anything the two agree and the split is invisible.

They diverged. `instruments.first_seen_d` was declared in schema.py, never in
db.py, and `universe.eligible`'s as-of gate reads it. Every freshly created
database came up without the column: a clone, CI, and an instance that rebuilds
from scratch on boot. Locally it was there from an earlier migration, so the
break was total everywhere except the one place anyone looked.

`documents.body_sha256` and `documents.raw_uri` were in the same state and had
been for longer.

So this checks the join rather than either side: for every column schema.py
declares on a table db.py owns, a database db.py just created has it. It fails
on the next person who adds a column to one file and not the other, which is the
mistake that produced all three.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("IDEAGEN_PLATFORM", "local")

from ideagen import db, schema  # noqa: E402


def _owned(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


class FreshDatabaseHasDeclaredColumns(unittest.TestCase):
    def setUp(self):
        self.con = db.init(":memory:")

    def test_every_declared_column_exists_on_a_table_we_own(self):
        owned = _owned(self.con)
        missing = [
            f"{table}.{column}"
            for table, column, _ in schema.ADD_COLUMNS
            if table in owned and column not in _cols(self.con, table)]
        self.assertEqual(
            missing, [],
            "declared in schema.py, absent from a database db.py just made — "
            "code reading these raises `no such column` on every fresh install "
            "while working on any machine that migrated earlier")

    def test_the_check_covers_something(self):
        # schema.ADD_COLUMNS also names tables that live only in the platform
        # state store. If none of them were ours the assertion above would pass
        # by testing nothing.
        owned = _owned(self.con)
        covered = [t for t, _, _ in schema.ADD_COLUMNS if t in owned]
        self.assertTrue(covered, "no declared column lands on a table db.py owns")

    def test_the_as_of_gate_can_read_and_write_its_column(self):
        # The failure this file exists for, stated as the operation that broke:
        # both directions raised OperationalError on a fresh database.
        self.con.execute(
            "SELECT COUNT(*) FROM instruments WHERE first_seen_d IS NULL")
        self.con.execute(
            "UPDATE instruments SET first_seen_d='2020-01-01' WHERE key='none'")


class LegacyDatabaseIsBroughtForward(unittest.TestCase):
    """`CREATE TABLE IF NOT EXISTS` is a no-op on an older table of that name."""

    def _legacy(self, table: str, drop: tuple[str, ...]) -> str:
        """A database whose `table` predates `drop`, built from the real DDL."""
        match = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", db.SCHEMA, re.S)
        assert match, f"{table} not found in db.SCHEMA"
        # Comment lines go too: dropping a column can leave the previous line's
        # trailing comma followed only by comments, which is a syntax error and
        # not the thing under test.
        kept = [line for line in match.group(1).split("\n")
                if line.strip() and not line.strip().startswith("--")
                and not any(c in line for c in drop)]
        ddl = (f"CREATE TABLE {table} ("
               + "\n".join(kept).rstrip().rstrip(",") + "\n)")
        path = str(Path(tempfile.mkdtemp()) / "legacy.db")
        con = sqlite3.connect(path)
        con.execute(ddl)
        required = [(r[1], r[2]) for r in con.execute(f"PRAGMA table_info({table})")
                    if r[3] and r[4] is None]
        con.execute(
            f"INSERT INTO {table}({','.join(n for n, _ in required)}) "
            f"VALUES({','.join('?' * len(required))})",
            [1 if "INT" in t.upper() else "x" for _, t in required])
        con.commit()
        con.close()
        return path

    def test_a_missing_column_is_added_without_touching_the_rows(self):
        path = self._legacy("documents", ("body_sha256", "raw_uri"))
        before = sqlite3.connect(path)
        self.assertNotIn("body_sha256", _cols(before, "documents"))
        rows_before = before.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        before.close()

        con = db.init(path)
        self.assertIn("body_sha256", _cols(con, "documents"))
        self.assertIn("raw_uri", _cols(con, "documents"))
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            rows_before, "adding a column must not disturb the rows")
        con.execute("UPDATE documents SET body_sha256='abc', raw_uri='tos://x'")

    def test_running_init_again_changes_nothing(self):
        path = self._legacy("instruments", ("first_seen_d",))
        first = _cols(db.init(path), "instruments")
        self.assertIn("first_seen_d", first)
        self.assertEqual(_cols(db.init(path), "instruments"), first)


if __name__ == "__main__":
    unittest.main()
