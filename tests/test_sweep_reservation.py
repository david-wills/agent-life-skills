"""restaurant sweep: only the owner's ✅ / ❌ resolve a suggestion."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from support import BOT, OWNER, STRANGER, FakeDiscord

support.add_path("restaurant-assistant", "restaurant-saver", "scripts")

import discord  # noqa: E402
import restaurant_common as rc  # noqa: E402
import skill_config  # noqa: E402
import sweep_reservation_reactions as sweep  # noqa: E402

CHANNEL = "901"


class SweepReservation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"SKILLS_PATHS_DATA_ROOT": self.tmp.name,
                                               "SKILLS_DISCORD_USER_ID": OWNER})
        patcher.start()
        self.addCleanup(patcher.stop)
        skill_config.reload()
        self.addCleanup(skill_config.reload)

    def seed(self, cards: dict[str, str]) -> None:
        """cards: message_id -> target_date."""
        conn = rc.connect()
        for i, (mid, date) in enumerate(cards.items(), start=1):
            conn.execute("INSERT INTO restaurants (id, name, added_at) VALUES (?,?,?)",
                         (i, f"place {mid}", rc.now_iso()))
            conn.execute(
                "INSERT INTO suggestions (restaurant_id, target_date, channel, message_id, posted_at) "
                "VALUES (?,?,?,?,?)", (i, date, CHANNEL, mid, rc.now_iso()))
        conn.commit()
        conn.close()

    def states(self) -> dict[str, str]:
        conn = rc.connect()
        rows = conn.execute("SELECT message_id, state FROM suggestions").fetchall()
        conn.close()
        return {r["message_id"]: r["state"] for r in rows}

    def run_sweep(self, fake: FakeDiscord, *extra: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(discord, "discord_request", fake), \
                mock.patch.object(sweep, "get_discord_token", lambda: "t"), \
                mock.patch("sys.argv", ["sweep", *extra]), \
                redirect_stdout(out), redirect_stderr(err):
            code = sweep.main()
        result = json.loads(out.getvalue()) if out.getvalue().strip() else {"stderr": err.getvalue()}
        return code, result

    def test_only_owner_reactions_resolve(self):
        base = dt.date.today() + dt.timedelta(days=2)
        dates = [(base + dt.timedelta(days=i)).isoformat() for i in range(5)]
        self.seed({"bot": dates[0], "owner": dates[1], "stranger": dates[2],
                   "conflict": dates[3], "declined": dates[4]})
        fake = FakeDiscord({
            "bot": {"✅": [BOT]},
            "owner": {"✅": [BOT, OWNER]},
            "stranger": {"✅": [BOT, STRANGER], "❌": [STRANGER]},
            "conflict": {"✅": [BOT, OWNER], "❌": [OWNER]},
            "declined": {"✅": [BOT], "❌": [OWNER]},
        })
        code, result = self.run_sweep(fake)
        self.assertEqual(code, 0, result)
        self.assertEqual([i["message_id"] for i in result["to_book"]], ["owner"])
        self.assertEqual([d["name"] for d in result["declined"]], ["place declined"])
        self.assertEqual(result["conflicts"], {})
        self.assertEqual(self.states(), {
            "bot": "offered", "owner": "accepted", "stranger": "offered",
            "conflict": "offered", "declined": "declined",
        })

    def test_two_owner_picks_same_date_is_a_conflict(self):
        date = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        self.seed({"a": date, "b": date})
        fake = FakeDiscord({"a": {"✅": [BOT, OWNER]}, "b": {"✅": [BOT, OWNER]}})
        code, result = self.run_sweep(fake)
        self.assertEqual(code, 0)
        self.assertEqual(result["to_book"], [])
        self.assertEqual(result["conflicts"], {date: ["place a", "place b"]})
        self.assertEqual(self.states(), {"a": "offered", "b": "offered"})

    def test_unreadable_card_fails_the_run(self):
        date = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        self.seed({"gone": date})
        code, result = self.run_sweep(FakeDiscord({}))
        self.assertEqual(code, 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.states(), {"gone": "offered"})

    def test_dry_run_changes_nothing(self):
        date = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        self.seed({"owner": date})
        code, result = self.run_sweep(FakeDiscord({"owner": {"✅": [BOT, OWNER]}}), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(result["to_book"]), 1)
        self.assertEqual(self.states(), {"owner": "offered"})

    def test_missing_user_id_exits_2(self):
        with mock.patch.dict(os.environ, {"SKILLS_DISCORD_USER_ID": ""}):
            code, result = self.run_sweep(FakeDiscord({}))
        self.assertEqual(code, 2)
        self.assertIn("discord.user_id", result["stderr"])


if __name__ == "__main__":
    unittest.main()
