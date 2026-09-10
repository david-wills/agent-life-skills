"""Ownership resolution in lib/discord.py::fetch_message_reactions."""

from __future__ import annotations

import unittest
from unittest import mock

import support
from support import BOT, OWNER, STRANGER, FakeDiscord

import discord


def fetch(fake: FakeDiscord, message_id: str = "m1", user_id=OWNER):
    with mock.patch.object(discord, "discord_request", fake):
        return discord.fetch_message_reactions("c1", message_id, user_id, token="t")


class ValidateUserId(unittest.TestCase):
    def test_accepts_snowflake_str_and_int(self):
        self.assertEqual(discord.validate_user_id(" 123 "), "123")
        self.assertEqual(discord.validate_user_id(123), "123")

    def test_rejects_placeholder_blank_and_none(self):
        for bad in ("USER_ID", "", None, "12a"):
            with self.assertRaises(discord.DiscordError):
                discord.validate_user_id(bad)


class FetchMessageReactions(unittest.TestCase):
    def test_bot_only_seed_is_not_by_user_and_skips_user_list(self):
        fake = FakeDiscord({"m1": {"✅": [BOT]}})
        out = fetch(fake)
        self.assertEqual(out, [{"name": "✅", "count": 1, "by_user": False}])
        self.assertEqual(len(fake.calls), 1, "no reactor list fetched when only the bot reacted")

    def test_owner_reaction_is_by_user(self):
        out = fetch(FakeDiscord({"m1": {"✅": [BOT, OWNER]}}))
        self.assertEqual(out, [{"name": "✅", "count": 2, "by_user": True}])

    def test_non_owner_reaction_is_not_by_user(self):
        out = fetch(FakeDiscord({"m1": {"✅": [BOT, STRANGER]}}))
        self.assertEqual(out, [{"name": "✅", "count": 2, "by_user": False}])

    def test_owner_among_others_is_by_user(self):
        out = fetch(FakeDiscord({"m1": {"✅": [BOT, STRANGER, OWNER]}}))
        self.assertTrue(out[0]["by_user"])

    def test_owner_reaction_without_seed(self):
        out = fetch(FakeDiscord({"m1": {"📌": [OWNER]}}))
        self.assertEqual(out, [{"name": "📌", "count": 1, "by_user": True}])

    def test_per_emoji_resolution(self):
        out = fetch(FakeDiscord({"m1": {"✅": [BOT, STRANGER], "🗑️": [BOT, OWNER], "📌": [BOT]}}))
        self.assertEqual({r["name"]: r["by_user"] for r in out},
                         {"✅": False, "🗑️": True, "📌": False})

    def test_super_reaction_counts(self):
        fake = FakeDiscord({"m1": {"✅": [BOT]}}, burst={"m1": {"✅": [OWNER]}})
        out = fetch(fake)
        self.assertEqual(out, [{"name": "✅", "count": 2, "by_user": True}])
        self.assertTrue(any("type=1" in c for c in fake.calls))

    def test_pagination_finds_owner_on_second_page(self):
        crowd = [BOT] + [f"4{i:017d}" for i in range(discord.REACTION_PAGE + 5)] + [OWNER]
        fake = FakeDiscord({"m1": {"✅": crowd}})
        out = fetch(fake)
        self.assertTrue(out[0]["by_user"])
        self.assertEqual(sum("after=" in c for c in fake.calls), 1)

    def test_user_id_type_is_normalised(self):
        out = fetch(FakeDiscord({"m1": {"✅": [BOT, OWNER]}}), user_id=int(OWNER))
        self.assertTrue(out[0]["by_user"])

    def test_placeholder_user_id_raises_before_any_call(self):
        fake = FakeDiscord({"m1": {"✅": [BOT, OWNER]}})
        with self.assertRaises(discord.DiscordError):
            fetch(fake, user_id="USER_ID")
        self.assertEqual(fake.calls, [])

    def test_unreadable_message_raises(self):
        with self.assertRaises(discord.DiscordError):
            fetch(FakeDiscord({}), message_id="gone")

    def test_custom_emoji_path(self):
        self.assertEqual(discord._emoji_path("party", "555"), "party:555")
        self.assertEqual(discord._emoji_path("✅"), "%E2%9C%85")


if __name__ == "__main__":
    unittest.main()
