"""video-tools rename.py: collision refusal, case-only rename, undo round trip."""

from __future__ import annotations

import glob
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support

support.add_path("video-tools", "name-videos", "lib")

import rename  # noqa: E402
import skill_config  # noqa: E402
import undo  # noqa: E402


class RenameVideos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.videos = self.root / "videos"
        self.videos.mkdir()
        patcher = mock.patch.dict(os.environ, {"SKILLS_PATHS_DATA_ROOT": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        skill_config.reload()
        self.addCleanup(skill_config.reload)

    def touch(self, *names: str) -> list[str]:
        paths = []
        for n in names:
            p = self.videos / n
            p.write_text(n)
            paths.append(str(p))
        return paths

    def plan(self, items: list[tuple[str, str]]) -> str:
        p = self.root / "plan.json"
        p.write_text(json.dumps([{"path": src, "new_name": new, "confidence": "high", "reason": "test"}
                                 for src, new in items]))
        return str(p)

    def run_main(self, module, *argv: str) -> str:
        out = io.StringIO()
        with mock.patch("sys.argv", ["x", *argv]), redirect_stdout(out):
            module.main()
        return out.getvalue()

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.videos))

    def test_safe_strips_illegal_characters(self):
        self.assertEqual(rename.safe(' TOP 5: "Best" Cars? / v2. '), "TOP 5 Best Cars v2")

    def test_build_reports_missing_unchanged_and_taken(self):
        a, b = self.touch("a.mp4", "b.mp4")
        moves, problems, collisions = rename.build([
            {"path": a, "new_name": "a"},
            {"path": a, "new_name": "b"},
            {"path": str(self.videos / "ghost.mp4"), "new_name": "x"},
        ])
        self.assertEqual(moves, [])
        self.assertEqual(collisions, [])
        self.assertEqual([why for _, why in problems], ["no change", "target exists: b.mp4", "source missing"])

    def test_duplicate_targets_refuse_the_whole_plan(self):
        a, b, c = self.touch("raw_1.mp4", "raw_2.mp4", "raw_3.mp4")
        plan = self.plan([(a, "TOP Best Cars"), (b, "top best cars"), (c, "Other")])
        with self.assertRaises(SystemExit) as cm:
            self.run_main(rename, plan, "--apply")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.listing(), ["raw_1.mp4", "raw_2.mp4", "raw_3.mp4"], "nothing may be renamed")
        self.assertEqual(glob.glob(str(self.root / "data" / "**" / "*.json"), recursive=True), [])

    def test_dry_run_touches_nothing(self):
        (a,) = self.touch("raw_1.mp4")
        out = self.run_main(rename, self.plan([(a, "Named")]))
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.listing(), ["raw_1.mp4"])

    def test_case_only_rename_and_undo_round_trip(self):
        a, b = self.touch("top five.mp4", "raw_2.mov")
        plan = self.plan([(a, "TOP Five"), (b, "BRAND Second - Clip")])
        out = self.run_main(rename, plan, "--apply")
        self.assertIn("Renamed 2", out)
        self.assertEqual(self.listing(), ["BRAND Second - Clip.mov", "TOP Five.mp4"])
        self.assertEqual((self.videos / "TOP Five.mp4").read_text(), "top five.mp4")

        logs = glob.glob(str(self.root / "data" / "name-videos" / "logs" / "rename-*.json"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(json.loads(Path(logs[0]).read_text())), 2)

        out = self.run_main(undo, logs[0], "--dry-run")
        self.assertIn("2/2 would be reverted", out)
        self.assertEqual(self.listing(), ["BRAND Second - Clip.mov", "TOP Five.mp4"])

        out = self.run_main(undo, logs[0])
        self.assertIn("2/2 reverted", out)
        self.assertEqual(self.listing(), ["raw_2.mov", "top five.mp4"])

    def test_undo_skips_when_original_name_is_taken(self):
        (a,) = self.touch("raw_1.mp4")
        logs_dir = self.root / "data" / "name-videos" / "logs"
        self.run_main(rename, self.plan([(a, "Named")]), "--apply")
        self.touch("raw_1.mp4")  # someone re-created the original
        (log,) = glob.glob(str(logs_dir / "rename-*.json"))
        out = self.run_main(undo, log)
        self.assertIn("original name is taken", out)
        self.assertEqual(self.listing(), ["Named.mp4", "raw_1.mp4"])


if __name__ == "__main__":
    unittest.main()
