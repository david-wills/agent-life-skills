"""token-tracker collect.py: incremental transcript ingest, and lib pricing helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import support

support.add_path("token-tracker", "token-tracker", "scripts")

import collect  # noqa: E402

lib = collect.lib


def rec(uuid: str, req: str | None, ts: str, model: str = "claude-opus-4-6", *,
        kind: str = "assistant", inp: int = 100, out: int = 10, cache_read: int = 0) -> str:
    d = {
        "type": kind, "uuid": uuid, "sessionId": "s1", "timestamp": ts, "entrypoint": "cli",
        "message": {"model": model, "id": "msg_x", "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        }},
    }
    if req:
        d["requestId"] = req
    return json.dumps(d) + "\n"


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Day boundaries follow token_tracker.timezone; pin it so CI in UTC agrees.
        env = mock.patch.dict(os.environ, {"SKILLS_TOKEN_TRACKER_TIMEZONE": "America/Los_Angeles"})
        env.start()
        self.addCleanup(env.stop)
        # collect derives the project name from the path segment after /projects/.
        self.root = Path(self.tmp.name) / "projects"
        self.file = self.root / "-Users-me-proj" / "s1.jsonl"
        self.file.parent.mkdir(parents=True)
        self.con = lib.connect(":memory:")
        self.addCleanup(self.con.close)
        lib.init(self.con)

    def ingest(self) -> int:
        return collect.ingest(self.con, root=str(self.root), host="test")

    def rows(self) -> list[tuple]:
        return self.con.execute("SELECT uuid, request_id, day, project FROM messages ORDER BY ts, uuid").fetchall()

    def offset(self) -> int:
        return self.con.execute("SELECT offset FROM files WHERE path=?", (str(self.file),)).fetchone()[0]

    def test_one_row_per_api_call_not_per_content_block(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-11T20:00:00Z")
                             + rec("u2", "req_A", "2025-09-11T20:00:01Z")
                             + rec("u3", "req_B", "2025-09-11T20:00:02Z"))
        self.ingest()
        self.assertEqual([r[0] for r in self.rows()], ["u1", "u3"])

    def test_partial_trailing_line_is_reread_once_complete(self):
        line1 = rec("u1", "req_A", "2025-09-11T20:00:00Z")
        line2 = rec("u2", "req_B", "2025-09-11T20:00:01Z")
        line3 = rec("u3", "req_C", "2025-09-11T20:00:02Z")
        self.file.write_bytes((line1 + line2 + line3[:40]).encode())
        self.ingest()
        self.assertEqual([r[0] for r in self.rows()], ["u1", "u2"])
        self.assertEqual(self.offset(), len(line1.encode()) + len(line2.encode()),
                         "offset must sit at the start of the unfinished line")

        with open(self.file, "ab") as fh:
            fh.write(line3[40:].encode())
            fh.write(rec("u4", "req_D", "2025-09-11T20:00:03Z").encode())
        self.ingest()
        self.assertEqual([r[0] for r in self.rows()], ["u1", "u2", "u3", "u4"])
        self.assertEqual(self.offset(), self.file.stat().st_size)

    def test_request_dedupe_survives_a_resume(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-11T20:00:00Z"))
        self.ingest()
        with open(self.file, "a") as fh:
            fh.write(rec("u2", "req_A", "2025-09-11T20:00:01Z"))
            fh.write(rec("u3", "req_B", "2025-09-11T20:00:02Z"))
        self.ingest()
        self.assertEqual([r[0] for r in self.rows()], ["u1", "u3"])

    def test_rewritten_smaller_file_is_reread_from_the_top(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-11T20:00:00Z") * 3)
        self.ingest()
        self.file.write_text(rec("u9", "req_Z", "2025-09-11T21:00:00Z"))
        self.ingest()
        self.assertEqual([r[0] for r in self.rows()], ["u1", "u9"])
        self.assertEqual(self.offset(), self.file.stat().st_size)

    def test_unchanged_file_is_skipped(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-11T20:00:00Z"))
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.ingest(), 0)

    def test_skips_non_billable_records(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-11T20:00:00Z", kind="user")
                             + rec("u2", "req_B", "2025-09-11T20:00:01Z", model="<synthetic>")
                             + rec("u3", "req_C", "")
                             + '{"type": "assistant", "usage": "not a dict"\n'
                             + "not json at all\n"
                             + rec("u4", None, "2025-09-11T20:00:04Z"))
        self.ingest()
        self.assertEqual([r[0] for r in self.rows()], ["u4"])

    def test_day_follows_the_configured_zone_and_project_is_the_path_segment(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-12T03:30:00Z"))
        self.ingest()
        self.assertEqual(self.rows(), [("u1", "req_A", "2025-09-11", "-Users-me-proj")])

    def test_another_zone_cuts_the_day_elsewhere(self):
        self.file.write_text(rec("u1", "req_A", "2025-09-12T03:30:00Z"))
        with mock.patch.dict(os.environ, {"SKILLS_TOKEN_TRACKER_TIMEZONE": "Europe/Berlin"}):
            self.ingest()
        self.assertEqual([r[2] for r in self.rows()], ["2025-09-12"])

    def test_subagent_transcripts_are_included(self):
        sub = self.file.parent / "s1" / "subagents" / "agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(rec("u7", "req_S", "2025-09-11T20:00:00Z"))
        self.file.write_text(rec("u1", "req_A", "2025-09-11T20:00:00Z"))
        self.assertEqual(self.ingest(), 2)
        self.assertEqual(len(self.rows()), 2)


class Pricing(unittest.TestCase):
    def test_normalize_model(self):
        self.assertEqual(lib.normalize_model("claude-opus-4-6-20260101"), "claude-opus-4-6")
        self.assertEqual(lib.normalize_model("anthropic/claude-sonnet-5"), "claude-sonnet-5")
        self.assertEqual(lib.normalize_model("qwen3.6-20260101"), "qwen3.6-20260101")
        self.assertEqual(lib.normalize_model(""), "unknown")

    def test_cost_uses_flat_cache_read_for_fable(self):
        self.assertAlmostEqual(lib.cost_usd("claude-fable-5-1", 0, 0, 1_000_000, 0, 0), 0.25)
        self.assertAlmostEqual(lib.cost_usd("claude-opus-5", 0, 0, 1_000_000, 0, 0), 0.5)
        self.assertAlmostEqual(lib.cost_usd("claude-opus-5", 1_000_000, 1_000_000, 0, 1_000_000, 1_000_000),
                               5 + 25 + 6.25 + 10)

    def test_unknown_model_is_priced_at_default_tier(self):
        self.assertAlmostEqual(lib.cost_usd("claude-future-9", 1_000_000, 0, 0, 0, 0), lib.DEFAULT_PRICE[0])

    def test_classify_provider(self):
        self.assertEqual(lib.classify_provider("claude-opus-5"), "anthropic")
        self.assertEqual(lib.classify_provider("gpt-5.4"), "openai")
        self.assertEqual(lib.classify_provider("qwen3.6"), "local")

    def test_short_project_and_prettify(self):
        home = os.path.expanduser("~").replace("/", "-")
        self.assertEqual(collect.short_project(f"{home}--openclaw-workspace-work"), "workspace/work")
        self.assertEqual(collect.short_project(f"{home}-Local-agent-life-skills"), "Local-agent-life-skills")
        self.assertEqual(collect.short_project(""), "unknown")
        self.assertEqual(collect.prettify("discord:guild#coach"), "discord:#coach")
        self.assertEqual(collect.prettify("discord:direct"), "discord:DM")


if __name__ == "__main__":
    unittest.main()


class Prices(unittest.TestCase):
    def test_unpriced_names_models_outside_the_table(self):
        self.assertEqual(lib.unpriced(["claude-opus-5", "claude-opus-5-20260101", "claude-next-9", "claude-next-9"]),
                         ["claude-next-9"])
        self.assertRegex(lib.PRICES_AS_OF, r"^\d{4}-\d{2}$")


class Export(unittest.TestCase):
    """export_aggregates is standalone (no lib), so its zone and redaction are flags."""

    def setUp(self):
        import export_aggregates
        self.mod = export_aggregates
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "projects"
        f = self.root / "-Users-me-proj" / "s1.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(rec("u1", "req_A", "2025-09-12T03:30:00Z"))

    def test_zone_and_redaction(self):
        rows = self.mod.collect(str(self.root), tz=ZoneInfo("America/Los_Angeles"))
        self.assertEqual((rows[0]["day"], rows[0]["project"]), ("2025-09-11", "-Users-me-proj"))
        rows = self.mod.collect(str(self.root), tz=ZoneInfo("Europe/Berlin"), redact=True)
        self.assertEqual(rows[0]["day"], "2025-09-12")
        self.assertRegex(rows[0]["project"], r"^[0-9a-f]{12}$")
        self.assertNotIn("me", rows[0]["project"])
