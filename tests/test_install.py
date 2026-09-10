"""Installation path: placeholder detection, doctor.py, link_skills.py."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support
from support import REPO

support.add_path()

import doctor  # noqa: E402
import link_skills  # noqa: E402
import skill_config as sc  # noqa: E402


class Placeholders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.object(sc, "CONFIG_PATH", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sc, "LOCAL_PATH", Path(self.tmp.name) / "none.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        sc.reload()
        self.addCleanup(sc.reload)

    def write(self, data: dict) -> None:
        self.config.write_text(json.dumps(data))
        sc.reload()

    def test_placeholder_set_is_learned_from_the_example(self):
        self.assertEqual(sc.placeholders(), {
            "CHANNEL_ID", "USER_ID", "YOUR_NAME", "YOUR_1PASSWORD_VAULT", "STREET, CITY, ST ZIP",
            "/path/to/your/video/drop/folder", "you@example.com", "calendar-reader@example.com",
            "SHARED_CALENDAR_ID@group.calendar.google.com",
        })
        for real in ("credential", "agent-skills", "this-mac", "Home", "NYC"):
            self.assertFalse(sc.is_placeholder(real), real)

    def test_copied_example_counts_as_unset(self):
        self.write(json.loads((REPO / "config.example.json").read_text()))
        for key in ("discord.channels.newsfeed", "discord.user_id", "user.display_name", "user.home",
                    "calendars.shared", "accounts.personal", "paths.video_drop"):
            with self.assertRaisesRegex(sc.ConfigError, "placeholder", msg=key):
                sc.cfg(key)
        self.assertEqual(sc.cfg("secrets.keychain_prefix"), "agent-skills", "real defaults survive")
        self.assertEqual(sc.cfg("paths.video_drop", None), None, "placeholder falls through to the default")
        self.assertEqual(sc.lookup("user.home")[0], "placeholder")
        self.assertIn("user.home.address", sc.lookup("user.home")[1])

    def test_real_values_pass(self):
        self.write({"discord": {"user_id": "123", "channels": {"newsfeed": "456"}},
                    "user": {"home": {"label": "Home", "address": "1 Main St", "lat": 1.0, "lng": 2.0}}})
        self.assertEqual(sc.cfg("discord.user_id"), "123")
        self.assertEqual(sc.cfg("user.home")["address"], "1 Main St")
        self.assertEqual(sc.lookup("discord.channels.newsfeed"), ("config", "456"))
        self.assertEqual(sc.lookup("discord.channels.errors"), ("missing", None))

    def test_env_placeholder_is_rejected_too(self):
        with mock.patch.dict(os.environ, {"SKILLS_DISCORD_USER_ID": "USER_ID"}):
            self.assertEqual(sc.lookup("discord.user_id"), ("placeholder", "USER_ID"))
            with self.assertRaises(sc.ConfigError):
                sc.cfg("discord.user_id")
        with mock.patch.dict(os.environ, {"SKILLS_DISCORD_USER_ID": "789"}):
            self.assertEqual(sc.cfg("discord.user_id"), "789")


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"SKILLS_PATHS_DATA_ROOT": self.tmp.name,
                                               "SKILLS_DISCORD_USER_ID": "USER_ID",
                                               "SKILLS_TOKEN_TRACKER_HOST": "test-box"})
        patcher.start()
        self.addCleanup(patcher.stop)
        sc.reload()
        self.addCleanup(sc.reload)

    def run_doctor(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.argv", ["doctor", *argv]), redirect_stdout(out):
            code = doctor.main()
        return code, out.getvalue()

    def test_json_report_covers_every_package_and_flags_placeholders(self):
        code, out = self.run_doctor("--json", "--no-secrets")
        report = json.loads(out)
        self.assertEqual(set(report["packages"]), set(doctor.PACKAGES))
        self.assertEqual(code, 1, "no config on a fresh clone means blocking checks")
        by_name = {c["name"]: c for c in report["packages"]["restaurant-assistant"]}
        self.assertEqual(by_name["discord.user_id"]["status"], "FAIL")
        self.assertIn("placeholder USER_ID", by_name["discord.user_id"]["detail"])
        self.assertEqual(by_name["DISCORD_BOT_TOKEN"]["status"], "skip")
        tt = {c["name"]: c for c in report["packages"]["token-tracker"]}
        self.assertEqual((tt["token_tracker.host"]["status"], tt["token_tracker.host"]["detail"]),
                         ("ok", "set (environment)"))
        self.assertTrue(any(c["kind"] == "data_root" and c["status"] == "ok" for c in report["environment"]))
        self.assertFalse(any("tok" in json.dumps(c) and c["kind"] == "secret" and c["status"] == "ok"
                             for cs in report["packages"].values() for c in cs))

    def test_package_selection_and_text_output(self):
        code, out = self.run_doctor("video-tools", "--no-secrets")
        self.assertIn("video-tools:", out)
        self.assertNotIn("token-tracker:", out)
        self.assertIn("ffmpeg", out)

    def test_secret_check_reports_source_not_value(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "hunter2"}):
            code, out = self.run_doctor("token-tracker", "--json")
        report = json.loads(out)
        secret = next(c for c in report["packages"]["token-tracker"] if c["name"] == "DISCORD_BOT_TOKEN")
        self.assertEqual((secret["status"], secret["detail"]), ("ok", "via env"))
        self.assertNotIn("hunter2", out)


class LinkSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "skills"

    def run_linker(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.argv", ["link", "--target", str(self.target), *argv]), redirect_stdout(out):
            code = link_skills.main()
        return code, out.getvalue()

    def test_discovers_every_skill_md(self):
        names = sorted(p.name for p in link_skills.skills())
        self.assertEqual(len(names), len(list(REPO.glob("*/*/SKILL.md"))))
        self.assertIn("name-videos", names)
        self.assertEqual(len(names), len(set(names)), "skill names must be unique to link flat")

    def test_dry_run_then_apply_then_idempotent(self):
        code, out = self.run_linker()
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertFalse(self.target.exists())

        code, out = self.run_linker("--apply")
        self.assertEqual(code, 0)
        links = sorted(p for p in self.target.iterdir())
        self.assertEqual(len(links), len(link_skills.skills()))
        for link in links:
            self.assertTrue(link.is_symlink())
            self.assertTrue((link / "SKILL.md").is_file())
            self.assertEqual(Path(os.path.realpath(link)).parent.parent, REPO)

        code, out = self.run_linker("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("ok "), len(links))

    def test_conflict_is_reported_never_replaced(self):
        self.target.mkdir()
        (self.target / "name-videos").mkdir()
        (self.target / "name-videos" / "keep.txt").write_text("mine")
        code, out = self.run_linker("--only", "name-videos", "reading-list", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT", out)
        self.assertTrue((self.target / "name-videos" / "keep.txt").is_file())
        self.assertTrue((self.target / "reading-list").is_symlink())

    def test_unknown_only_name(self):
        code, out = self.run_linker("--only", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no skills found", out)


if __name__ == "__main__":
    unittest.main()
