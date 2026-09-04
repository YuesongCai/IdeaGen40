"""建表和补列这两份声明，必须一起改。

`CREATE TABLE IF NOT EXISTS` 对一个已存在的同名旧表是 no-op，所以往建表里加一列
只救新文件；一台带着几个月历史的机器会留在一个代码不再认识的 schema 上。补列走
`schema.ADD_COLUMNS`。

问题在于这件事**在本机永远看不见**：开发机的库早就有那列了。2026-09-05 已经因此
坏过两轮——`instruments.first_seen_d` 让全新库上的读和写直接抛，克隆、CI、以及
云端每次开机重建的实例上那套功能从来没跑起来过；同一轮还翻出 `documents` 的
`body_sha256` 与 `raw_uri` 同样漏在外面，只是碰巧被别的路径迁过所以没炸。

两次都是同一个动作：给建表加了列，没给补列清单加。所以这里放一份建表列的基线。
它不检查对错，只保证这个动作**不会安静地发生**——改了建表，这里就红，红的时候
去回答一个问题：一个在这列之前建的库还可能在用吗？会，就同时加进
`schema.ADD_COLUMNS`；不会，就只更新基线。

`test_parser_matches_a_real_database` 是这一切的地基：解析器要是读错了，基线就是
一份精确的错误。它拿解析结果和真建一个库的 PRAGMA 对照，两边必须逐列相等。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("WISBURG_MCP_URL", "https://research.example/mcp")
os.environ.setdefault("OLIVE_MCP_URL", "https://catalog.example/mcp")
os.environ.setdefault("OLIVE_OAUTH_ISSUER", "https://sso.example")
os.environ.setdefault("OLIVE_OAUTH_TOKEN_URL", "https://sso.example/token")

from ideagen import db, schema                        # noqa: E402

BASELINE = Path(__file__).with_name("schema_baseline.json")
_CONSTRAINT = re.compile(r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", re.I)


def _split_top(body: str) -> list[str]:
    """Split on commas outside parentheses — `DECIMAL(10,2)` is one column."""
    out: list[str] = []
    depth, cur = 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def declared_columns(sql: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                         sql, re.S):
        table, body = m.group(1), re.sub(r"--[^\n]*", "", m.group(2))
        cols = []
        for piece in _split_top(body):
            piece = piece.strip()
            if not piece or _CONSTRAINT.match(piece):
                continue
            tok = piece.split()[0]
            if re.fullmatch(r"\w+", tok):
                cols.append(tok)
        out[table] = cols
    return out


def _added_by_table() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for table, column, _decl in schema.ADD_COLUMNS:
        out.setdefault(table, []).append(column)
    return out


class TheParserIsTrustworthy(unittest.TestCase):
    def test_parser_matches_a_real_database(self):
        """Read the DDL and build from it; the two must agree column for
        column, or the baseline below records a precise falsehood."""
        p = Path(tempfile.mkdtemp()) / "fresh.db"
        con = db.init(p)
        added = _added_by_table()
        for table, cols in declared_columns(db.SCHEMA).items():
            expect = cols + [c for c in added.get(table, []) if c not in cols]
            real = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            self.assertEqual(real, expect, table)


class SchemaChangesAreNotSilent(unittest.TestCase):
    def test_declared_columns_match_the_recorded_baseline(self):
        want = json.loads(BASELINE.read_text(encoding="utf-8"))
        got = declared_columns(db.SCHEMA)
        if got == want:
            return
        lines = []
        for table in sorted(set(want) | set(got)):
            a, b = want.get(table, []), got.get(table, [])
            if a != b:
                lines.append(f"  {table}: 新增 {sorted(set(b) - set(a))}  "
                             f"移除 {sorted(set(a) - set(b))}")
        self.fail(
            "建表声明变了：\n" + "\n".join(lines)
            + "\n\n这不一定是错的，但必须回答一个问题：一个在这列之前建的库还可能"
              "在用吗（克隆、CI、云端每次开机重建的实例）？\n"
              "  会 → 同时把它加进 ideagen/schema.py 的 ADD_COLUMNS，再更新本基线\n"
              "  不会 → 只更新本基线 tests/schema_baseline.json\n"
              "这道闸门存在的原因：开发机的库早就有那列，漏掉补列在本机永远看不见。")


class OldDatabasesGetTheColumn(unittest.TestCase):
    """The concrete regression: 2026-09-05, a fresh database had no
    `instruments.first_seen_d`, so both the as-of gate's read and the backfill's
    write raised on every machine except this one."""

    def _old_instruments(self) -> Path:
        p = Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE instruments "
                    "(key TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT)")
        con.execute("INSERT INTO instruments VALUES('SPY','listed','SPDR')")
        con.commit()
        con.close()
        return p

    def test_the_column_is_added_and_the_rows_survive(self):
        p = self._old_instruments()
        con = db.init(p)
        cols = [r[1] for r in con.execute("PRAGMA table_info(instruments)")]
        self.assertIn("first_seen_d", cols)
        row = list(con.execute(
            "SELECT key, name, first_seen_d FROM instruments"))[0]
        self.assertEqual((row[0], row[1], row[2]), ("SPY", "SPDR", None))

    def test_reading_and_writing_the_column_both_work(self):
        """Both directions, because the outage was both directions."""
        con = db.init(self._old_instruments())
        con.execute("UPDATE instruments SET first_seen_d='2026-01-02' "
                    "WHERE key='SPY'")
        self.assertEqual(
            list(con.execute("SELECT first_seen_d FROM instruments "
                             "WHERE first_seen_d IS NOT NULL"))[0][0],
            "2026-01-02")

    def test_init_is_idempotent(self):
        p = self._old_instruments()
        first = [r[1] for r in db.init(p).execute(
            "PRAGMA table_info(instruments)")]
        second = [r[1] for r in db.init(p).execute(
            "PRAGMA table_info(instruments)")]
        self.assertEqual(first, second)

    def test_every_listed_column_lands_on_a_table_this_module_owns(self):
        """Entries for tables the platform state store owns are skipped by
        design; entries for this module's tables must actually arrive."""
        con = db.init(Path(tempfile.mkdtemp()) / "fresh.db")
        mine = set(declared_columns(db.SCHEMA))
        for table, column, _decl in schema.ADD_COLUMNS:
            if table not in mine:
                continue
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            self.assertIn(column, cols, f"{table}.{column}")


if __name__ == "__main__":
    unittest.main()
