"""Every script answers --help with exit 0 on a bare machine: no config, no env, empty HOME."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

from support import REPO

SKIP_DIRS = {".git", "tests", "__pycache__", ".venv"}
# Third-party imports at module scope are the only accepted reason for a script
# to fail --help. Map script name -> module that must be importable.
NEEDS = {"fetch_feeds.py": "feedparser"}


def scripts() -> list[str]:
    out = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if os.path.relpath(root, REPO) == "lib":
            continue
        out.extend(os.path.join(root, f) for f in sorted(files) if f.endswith(".py"))
    return out


class HelpSmoke(unittest.TestCase):
    def test_every_script_prints_help_without_config(self):
        found = scripts()
        self.assertGreater(len(found), 20, "script discovery walked the wrong tree")
        data_existed = (REPO / "_data").exists()  # a prior tour or smoke run may have made it
        failures = []
        with tempfile.TemporaryDirectory() as home:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": home}
            for path in found:
                need = NEEDS.get(os.path.basename(path))
                if need and importlib.util.find_spec(need) is None:
                    continue
                r = subprocess.run([sys.executable, path, "--help"], env=env, cwd=home,
                                   capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    failures.append(f"{os.path.relpath(path, REPO)}: exit {r.returncode}\n{r.stderr[-400:]}")
        self.assertEqual(failures, [], "\n\n".join(failures))
        if not data_existed:
            self.assertFalse((REPO / "_data").exists(), "--help created _data/ in the repo")


if __name__ == "__main__":
    unittest.main()
