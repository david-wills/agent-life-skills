"""The FTS5 ``entries`` table is defined once, in lib/fts_schema.py."""

from __future__ import annotations

import re
import sqlite3
import unittest

import support  # noqa: F401
from support import REPO

import fts_schema  # noqa: E402


class FtsSchema(unittest.TestCase):
    def test_no_script_carries_its_own_copy(self):
        offenders = []
        for path in REPO.rglob("*.py"):
            rel = path.relative_to(REPO)
            if rel.parts[0] in ("lib", "tests", ".git") or "_data" in rel.parts:
                continue
            if re.search(r"CREATE VIRTUAL TABLE", path.read_text(encoding="utf-8")):
                offenders.append(str(rel))
        self.assertEqual(offenders, [], "define FTS tables in lib/fts_schema.py, then import them")

    def test_ddl_creates_the_entries_table(self):
        con = sqlite3.connect(":memory:")
        try:
            fts_schema.ensure_entries(con)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                self.skipTest("sqlite built without FTS5")
            raise
        cols = [r[1] for r in con.execute("PRAGMA table_info(entries)")]
        self.assertEqual(cols[:3], ["id", "source", "source_type"])
        self.assertIn("body", cols)
        fts_schema.ensure_entries(con)  # idempotent
        con.execute("INSERT INTO entries (id, source, title, body) VALUES ('a', 'x', 'Hello', 'world')")
        self.assertEqual(con.execute("SELECT id FROM entries WHERE entries MATCH 'world'").fetchone(), ("a",))
        con.close()


if __name__ == "__main__":
    unittest.main()
