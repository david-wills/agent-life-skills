"""reading-list sweep: only the owner's reactions drive Reader."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from support import BOT, OWNER, STRANGER, FakeDiscord

support.add_path("news-triage-assistant", "reading-list", "scripts")

import discord  # noqa: E402
import reading_list_common as rl  # noqa: E402
import skill_config  # noqa: E402
import sweep_reading_list_reactions as sweep  # noqa: E402

CHANNEL = "900"


class SweepReadingList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"SKILLS_PATHS_DATA_ROOT": self.tmp.name, "SKILLS_DISCORD_USER_ID": OWNER}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        skill_config.reload()
        self.addCleanup(skill_config.reload)
        self.db = Path(self.tmp.name) / "index.db"
        self.applied: list[tuple[str, str]] = []

    def seed(self, cards: dict[str, str]) -> None:
        conn = sqlite3.connect(self.db)
        rl.ensure_tables(conn)
        for doc_id, message_id in cards.items():
            conn.execute(
                "INSERT INTO reading_list_log (doc_id, message_id, channel, title, posted_at) VALUES (?,?,?,?,?)",
                (doc_id, message_id, CHANNEL, f"title {doc_id}", rl.iso_utc(rl.utc_now())),
            )
        conn.commit()
        conn.close()

    def run_sweep(self, fake: FakeDiscord, *extra: str) -> tuple[int, dict]:
        def apply_action(doc_id, action, token):
            self.applied.append((doc_id, action))
            return True, "ok"

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(discord, "discord_request", fake), \
                mock.patch.object(rl, "discord_token", lambda: "t"), \
                mock.patch.object(rl, "reader_token", lambda: "r"), \
                mock.patch.object(sweep, "apply_action", apply_action), \
                mock.patch("sys.argv", ["sweep", "--db", str(self.db), "--quiet", *extra]), \
                redirect_stdout(out), redirect_stderr(err):
            code = sweep.main()
        result = json.loads(out.getvalue()) if out.getvalue().strip() else {"stderr": err.getvalue()}
        return code, result

    def statuses(self) -> dict[str, str]:
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT doc_id, status FROM reading_list_log").fetchall()
        conn.close()
        return dict(rows)

    def test_user_actions_only_counts_owner(self):
        fake = FakeDiscord({
            "bot": {"✅": [BOT], "📌": [BOT], "🗑️": [BOT]},
            "owner": {"✅": [BOT, OWNER], "📌": [BOT], "🗑️": [BOT]},
            "stranger": {"✅": [BOT, STRANGER], "📌": [BOT], "🗑️": [BOT]},
            "conflict": {"✅": [BOT, OWNER], "📌": [BOT], "🗑️": [BOT, OWNER]},
            "mixed": {"✅": [BOT, STRANGER], "📌": [BOT], "🗑️": [BOT, OWNER]},
        })
        with mock.patch.object(discord, "discord_request", fake):
            actions = {m: sweep.user_actions(CHANNEL, m, "t", OWNER) for m in fake.messages}
        self.assertEqual(actions, {
            "bot": [], "owner": ["archive"], "stranger": [],
            "conflict": ["archive", "delete"], "mixed": ["delete"],
        })

    def test_end_to_end_state_changes(self):
        self.seed({"d-bot": "bot", "d-owner": "owner", "d-stranger": "stranger",
                   "d-conflict": "conflict", "d-later": "later"})
        fake = FakeDiscord({
            "bot": {"✅": [BOT], "📌": [BOT], "🗑️": [BOT]},
            "owner": {"✅": [BOT, OWNER], "📌": [BOT], "🗑️": [BOT]},
            "stranger": {"✅": [BOT, STRANGER], "📌": [BOT], "🗑️": [BOT, STRANGER]},
            "conflict": {"✅": [BOT, OWNER], "📌": [BOT], "🗑️": [BOT, OWNER]},
            "later": {"✅": [BOT, STRANGER], "📌": [BOT, OWNER], "🗑️": [BOT]},
        })
        code, result = self.run_sweep(fake)
        self.assertEqual(code, 0, result)
        self.assertEqual(sorted(self.applied), [("d-later", "later"), ("d-owner", "archive")])
        self.assertEqual(self.statuses(), {
            "d-bot": "posted", "d-owner": "actioned", "d-stranger": "posted",
            "d-conflict": "posted", "d-later": "actioned",
        })
        self.assertEqual((result["archived"], result["later"], result["deleted"], result["conflicts"]),
                         (1, 1, 0, 1))

    def test_unreadable_card_is_an_error_not_expiry(self):
        self.seed({"d-gone": "gone", "d-owner": "owner"})
        fake = FakeDiscord({"owner": {"🗑️": [BOT, OWNER]}})
        code, result = self.run_sweep(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.statuses(), {"d-gone": "posted", "d-owner": "actioned"})

    def test_dry_run_changes_nothing(self):
        self.seed({"d-owner": "owner"})
        code, result = self.run_sweep(FakeDiscord({"owner": {"✅": [BOT, OWNER]}}), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.statuses(), {"d-owner": "posted"})

    def test_missing_user_id_exits_2(self):
        self.seed({"d-owner": "owner"})
        with mock.patch.dict(os.environ, {"SKILLS_DISCORD_USER_ID": "USER_ID"}):
            code, result = self.run_sweep(FakeDiscord({"owner": {"✅": [BOT, OWNER]}}))
        self.assertEqual(code, 2)
        self.assertIn("discord.user_id", result["stderr"])
        self.assertEqual(self.statuses(), {"d-owner": "posted"})


if __name__ == "__main__":
    unittest.main()
