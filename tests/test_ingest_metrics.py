"""structured-metrics ingest: local day, sets preserved without raw JSON, view priority."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support

support.add_path("quantified-self-coach", "structured-metrics", "scripts")

import ingest_metrics as im  # noqa: E402

UUID = "0b1e2f3a-aaaa-bbbb-cccc-000000000001"


def pin_tz(test: unittest.TestCase, name: str = "America/Los_Angeles") -> None:
    patcher = mock.patch.dict(os.environ, {"TZ": name})
    patcher.start()
    test.addCleanup(patcher.stop)
    time.tzset()
    test.addCleanup(time.tzset)


@unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
class LocalDay(unittest.TestCase):
    def setUp(self):
        pin_tz(self)

    def test_utc_evening_is_the_previous_local_day(self):
        self.assertEqual(im.local_day("2025-09-12T01:30:00Z"), "2025-09-11")
        self.assertEqual(im.local_day("2025-09-12T01:30:00+00:00"), "2025-09-11")
        self.assertEqual(im.local_day("2025-09-12T01:30:00"), "2025-09-11", "naive is UTC")
        self.assertEqual(im.local_day("2025-09-12T12:00:00Z"), "2025-09-12")

    def test_fallbacks(self):
        self.assertEqual(im.local_day("2025-09-12 junk"), "2025-09-12")
        self.assertIsNone(im.local_day(None))
        self.assertIsNone(im.local_day("  "))


class Parsing(unittest.TestCase):
    def test_frontmatter_types(self):
        fm, body = im._split_frontmatter('---\ntitle: "Push Day"\nn: 3\nf: 1.5\nok: true\n'
                                          'tags: ["a", "b"]\nbare: hello world\n---\n\nBody text\n')
        self.assertEqual(fm, {"title": "Push Day", "n": 3, "f": 1.5, "ok": True,
                              "tags": ["a", "b"], "bare": "hello world"})
        self.assertEqual(body, "Body text\n")
        self.assertEqual(im._split_frontmatter("no frontmatter"), ({}, "no frontmatter"))

    def test_slugify(self):
        self.assertEqual(im._slugify("Hack Squat (Machine)"), "hack-squat-machine")
        self.assertEqual(im._slugify("Lat Pulldown - Cable"), "lat-pulldown-cable")

    def test_aggregate_apple_health_points(self):
        self.assertEqual(im._aggregate_ah_metric([{"qty": 100, "units": "count", "source": "Watch"},
                                                  {"qty": 50, "units": "count", "source": "Phone"}]),
                         (150.0, "count", "Phone|Watch"))
        self.assertEqual(im._aggregate_ah_metric([{"Avg": 50, "units": "ms"}, {"Avg": 70}]), (60.0, "ms", None))
        self.assertEqual(im._aggregate_ah_metric([]), (None, None, None))


@unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
class HevyIngest(unittest.TestCase):
    def setUp(self):
        pin_tz(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kb = Path(self.tmp.name) / "knowledge"
        self.imports = Path(self.tmp.name) / "imports"
        (self.kb / "hevy").mkdir(parents=True)
        (self.imports / "hevy").mkdir(parents=True)
        (self.kb / "hevy" / f"{UUID}.md").write_text(
            f"---\nid: hevy:{UUID}\ntitle: Push Day\ncaptured_at: 2025-09-12T01:30:00Z\n"
            "duration_min: 62\nexercise_count: 1\nworking_set_count: 1\nworking_volume_lb: 1763.7\n---\n\n"
            "## Hack Squat\n- 100 kg x 8\n")
        (self.imports / "hevy" / "hevy-workouts-2025-09-12.json").write_text(json.dumps({"workouts": [{
            "id": UUID, "exercises": [{"title": "Hack Squat (Machine)", "sets": [
                {"weight_kg": 50, "reps": 10, "type": "warmup"},
                {"weight_kg": 100, "reps": 8, "rpe": 8.5, "type": "normal"},
            ]}]}]}))
        self.conn = im.ensure_db(None)
        self.addCleanup(self.conn.close)

    def sets(self):
        return self.conn.execute(
            "SELECT set_index, exercise_slug, weight_lb, reps, rpe, is_warmup FROM exercise_sets ORDER BY set_index"
        ).fetchall()

    def test_workout_date_is_local_and_sets_come_from_raw_json(self):
        self.assertEqual(im.ingest_hevy_workouts(self.conn, self.kb, None, self.imports), (1, 2, 1, 0))
        row = self.conn.execute("SELECT id, date, title, total_volume_lb, set_count FROM workouts").fetchone()
        self.assertEqual(row, (f"hevy:{UUID}", "2025-09-11", "Push Day", 1763.7, 1))
        self.assertEqual(self.sets(), [(0, "hack-squat-machine", 110.23, 10, None, 1),
                                       (1, "hack-squat-machine", 220.46, 8, 8.5, 0)])
        self.assertEqual(self.conn.execute(
            "SELECT date, value FROM metrics_daily WHERE metric='training_volume_lb'").fetchone(),
            ("2025-09-11", 1763.7))

    def test_reingest_without_raw_json_keeps_sets(self):
        im.ingest_hevy_workouts(self.conn, self.kb, None, self.imports)
        first = self.conn.execute("SELECT ingested_at FROM workouts").fetchone()[0]
        with mock.patch.object(im, "_iso_utc_now", lambda: "2099-01-01T00:00:00Z"):
            self.assertEqual(im.ingest_hevy_workouts(self.conn, self.kb, None, None), (1, 0, 1, 0))
        self.assertNotEqual(self.conn.execute("SELECT ingested_at FROM workouts").fetchone()[0], first)
        self.assertEqual(len(self.sets()), 2, "upsert must not cascade-delete the sets")

    def test_window_excludes_old_workouts(self):
        import datetime as dt
        self.assertEqual(im.ingest_hevy_workouts(self.conn, self.kb, dt.date(2025, 9, 12), self.imports),
                         (0, 0, 0, 0))
        self.assertEqual(im.ingest_hevy_workouts(self.conn, self.kb, dt.date(2025, 9, 11), self.imports)[0], 1)


class OtherSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kb = Path(self.tmp.name) / "knowledge"
        self.conn = im.ensure_db(None)
        self.addCleanup(self.conn.close)

    def test_oura_metrics_and_sleep(self):
        (self.kb / "oura").mkdir(parents=True)
        (self.kb / "oura" / "daily-2025-09-11.md").write_text(
            "---\nday: 2025-09-11\nhrv_avg: 48\nresting_hr: 52.5\ntotal_sleep_min: 430\n"
            "sleep_score: 81\nreadiness_score: n/a\n---\nprose\n")
        (self.kb / "oura" / "notes.md").write_text("ignored")
        self.assertEqual(im.ingest_oura_metrics(self.conn, self.kb, None), (4, 1, 0))
        rows = dict(self.conn.execute("SELECT metric, value FROM metrics_daily WHERE source='oura'").fetchall())
        self.assertEqual(rows, {"heart_rate_variability": 48.0, "resting_heart_rate": 52.5,
                                "total_sleep_min": 430.0, "sleep_score": 81.0})
        self.assertEqual(self.conn.execute("SELECT date, total_min, score FROM sleep_sessions").fetchone(),
                         ("2025-09-11", 430.0, 81.0))

    def test_apple_health_metrics_and_workouts(self):
        side = self.kb / "apple-health" / ".sidecar"
        side.mkdir(parents=True)
        (side / "2025-09-11.json").write_text(json.dumps({
            "metrics": {
                "step_count": [{"qty": 100, "units": "count", "source": "Watch"}, {"qty": 50, "units": "count"}],
                "heart_rate_variability": [{"Avg": 50, "units": "ms"}, {"Avg": 70, "units": "ms"}],
                "walking_speed": [{"qty": 5}],
            },
            "workouts": [{"id": "w1", "name": "Outdoor Run", "start": "2025-09-11 07:00:00 -0700",
                          "end": "2025-09-11 07:30:00 -0700", "duration": 1800,
                          "heartRate": {"avg": {"qty": 150}, "max": {"qty": 172}},
                          "activeEnergyBurned": {"qty": 300}},
                         {"name": "no id"}],
        }))
        (side / "README.json").write_text("{}")
        self.assertEqual(im.ingest_apple_health_metrics(self.conn, self.kb, None), (2, 1))
        rows = dict(self.conn.execute("SELECT metric, value FROM metrics_daily").fetchall())
        self.assertEqual(rows, {"step_count": 150.0, "heart_rate_variability": 60.0})
        self.assertEqual(im.ingest_apple_health_workouts(self.conn, self.kb, None), (1, 2))
        self.assertEqual(self.conn.execute(
            "SELECT id, date, type, duration_min, avg_hr_bpm, total_calories FROM workouts").fetchone(),
            ("apple-health:w1", "2025-09-11", "outdoor-run", 30.0, 150.0, 300.0))

    def test_resolved_view_prefers_oura_then_apple_health(self):
        self.conn.executemany(
            "INSERT INTO metrics_daily (date, source, metric, value, ingested_at) VALUES (?,?,?,?,'t')",
            [("2025-09-11", "hevy", "resting_heart_rate", 3),
             ("2025-09-11", "apple-health", "resting_heart_rate", 2),
             ("2025-09-11", "oura", "resting_heart_rate", 1),
             ("2025-09-12", "hevy", "resting_heart_rate", 3),
             ("2025-09-12", "apple-health", "resting_heart_rate", 2)])
        rows = self.conn.execute(
            "SELECT date, source, value FROM metrics_daily_resolved ORDER BY date").fetchall()
        self.assertEqual(rows, [("2025-09-11", "oura", 1.0), ("2025-09-12", "apple-health", 2.0)])


class Cli(unittest.TestCase):
    def test_dry_run_writes_nothing_and_real_run_creates_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "knowledge"
            (kb / "oura").mkdir(parents=True)
            (kb / "oura" / "daily-2025-09-11.md").write_text("---\nhrv_avg: 48\n---\n")
            argv = ["ingest", "--kb-root", str(kb), "--imports-root", str(Path(tmp) / "imports"), "--all"]
            out = io.StringIO()
            with mock.patch("sys.argv", argv + ["--dry-run"]), redirect_stdout(out):
                self.assertEqual(im.main(), 0)
            self.assertIn("dry-run oura: metrics=1 sleep_sessions=1", out.getvalue())
            self.assertFalse((kb / "index.db").exists())
            with mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(im.main(), 0)
            con = sqlite3.connect(kb / "index.db")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0], 1)
            con.close()
            if os.name == "posix":
                self.assertEqual((kb / "index.db").stat().st_mode & 0o077, 0, "health data must be owner-only")
            with mock.patch("sys.argv", argv[:-1] + ["--since", "nope"]), redirect_stdout(io.StringIO()):
                self.assertEqual(im.main(), 2)


if __name__ == "__main__":
    unittest.main()
