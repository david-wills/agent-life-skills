"""morning-news post_digest: whole-batch validation and resume keyed on channel + count."""

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

support.add_path("news-triage-assistant", "morning-news", "scripts")

import post_digest as pd  # noqa: E402
import skill_config  # noqa: E402

CHANNEL = "111"


class PostDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"SKILLS_PATHS_DATA_ROOT": self.tmp.name,
                                               "SKILLS_DISCORD_CHANNELS_NEWSFEED": CHANNEL})
        patcher.start()
        self.addCleanup(patcher.stop)
        skill_config.reload()
        self.addCleanup(skill_config.reload)
        self.posted: list[tuple[str, str]] = []
        self.fail_at: set[int] = set()
        self.messages = [f"header", "s1", "s2", "s3", "s4"]
        self.path = Path(self.tmp.name) / "digest.json"
        self.path.write_text(json.dumps(self.messages))

    def fake_post(self, channel, text, token):
        if self.messages.index(text) in self.fail_at:
            raise RuntimeError("boom")
        self.posted.append((channel, text))

    def run_main(self, *argv, token_ok=True) -> tuple[int, dict]:
        out = io.StringIO()
        token = (lambda: "tok") if token_ok else mock.Mock(side_effect=AssertionError("token resolved"))
        with mock.patch.object(pd, "post_one", self.fake_post), \
                mock.patch.object(pd.time, "sleep", lambda s: None), \
                mock.patch.object(pd, "get_discord_token", token), \
                mock.patch("sys.argv", ["post", "--messages", str(self.path), *argv]), \
                redirect_stdout(out):
            code = pd.main()
        return code, json.loads(out.getvalue())

    def progress(self) -> dict:
        return pd.read_progress(pd.progress_file())

    # --- pure pieces ---------------------------------------------------------

    def test_validate_reports_every_oversized_message(self):
        msgs = ["ok", "x" * pd.HARD_LIMIT, "Title line\nmore " + "y" * pd.HARD_LIMIT, "z" * 2001]
        bad = pd.validate(msgs)
        self.assertEqual([b["index"] for b in bad], [2, 3])
        self.assertEqual(bad[0]["preview"], "Title line")

    def test_load_messages_rejects_bad_shapes(self):
        for payload in ('{"a": 1}', '["ok", 2]', "[]"):
            self.path.write_text(payload)
            with self.assertRaises(SystemExit):
                pd.load_messages(str(self.path))

    def test_read_progress_tolerates_absent_and_corrupt(self):
        p = Path(self.tmp.name) / "nope.json"
        self.assertEqual(pd.read_progress(p), {})
        p.write_text("{not json")
        self.assertEqual(pd.read_progress(p), {})

    def test_resume_index_keyed_on_channel_and_count(self):
        p = Path(self.tmp.name) / "last_run.json"
        pd.write_progress(p, {"channel": CHANNEL, "total": 5, "posted": 3})
        self.assertEqual(pd.resume_index(p, ["m"] * 5, CHANNEL), 3)
        self.assertEqual(pd.resume_index(p, ["m"] * 6, CHANNEL), 0, "different digest")
        self.assertEqual(pd.resume_index(p, ["m"] * 5, "222"), 0, "different channel")

    # --- main ----------------------------------------------------------------

    def test_dry_run_validates_and_resolves_no_token(self):
        code, result = self.run_main("--dry-run", token_ok=False)
        self.assertEqual(code, 0)
        self.assertEqual((result["dry_run"], result["messages"], result["would_start_at"]), (True, 5, 0))
        self.assertEqual(self.posted, [])
        self.assertFalse(pd.progress_file().exists())

    def test_oversized_message_blocks_the_whole_batch(self):
        self.messages[3] = "x" * 2001
        self.path.write_text(json.dumps(self.messages))
        code, result = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(result["stage"], "validate")
        self.assertEqual(self.posted, [])

    def test_full_run_records_progress(self):
        code, result = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual([t for _, t in self.posted], self.messages)
        self.assertEqual(result["posted"], 5)
        prog = self.progress()
        self.assertEqual((prog["channel"], prog["total"], prog["posted"]), (CHANNEL, 5, 5))
        self.assertIn("finished_at", prog)

    def test_failure_then_resume_does_not_resend(self):
        self.fail_at = {2}
        code, result = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual((result["stage"], result["failed_at"], result["posted"]), ("post", 2, 2))
        self.assertEqual([t for _, t in self.posted], ["header", "s1"])
        self.assertEqual(self.progress()["posted"], 2)

        self.fail_at = set()
        self.posted.clear()
        code, result = self.run_main("--resume")
        self.assertEqual(code, 0)
        self.assertEqual([t for _, t in self.posted], ["s2", "s3", "s4"])
        self.assertEqual(result["resumed_from"], 2)

    def test_resume_of_a_different_digest_starts_over(self):
        pd.write_progress(pd.progress_file(), {"channel": CHANNEL, "total": 9, "posted": 4})
        code, result = self.run_main("--resume")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted), 5)
        self.assertIsNone(result["resumed_from"])

    def test_no_state_leaves_progress_alone(self):
        pd.write_progress(pd.progress_file(), {"channel": CHANNEL, "total": 5, "posted": 2})
        code, _ = self.run_main("--no-state", "--channel", "999")
        self.assertEqual(code, 0)
        self.assertEqual([c for c, _ in self.posted], ["999"] * 5)
        self.assertEqual(self.progress()["posted"], 2, "a notice must not clobber a half-posted digest")
        with self.assertRaises(SystemExit):
            self.run_main("--no-state", "--resume")


if __name__ == "__main__":
    unittest.main()
