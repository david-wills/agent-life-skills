"""Every SKILL.md frontmatter stays short, spec-shaped, and in step with doctor.py.

The frontmatter is the only part of a skill every runtime loads at startup, so
it is kept flat and small: one-line description under a fixed budget, the
Agent Skills ``compatibility`` string for human-readable needs, and
``metadata`` holding OpenClaw's gates as one JSON object. The gates are
cross-checked against the requirement table in ``doctor.py`` so a binary or
OS requirement cannot be declared in one place and not the other.
"""

from __future__ import annotations

import json
import re
import unittest

import support
from support import REPO

support.add_path()  # repo root, for doctor

import doctor  # noqa: E402

MAX_DESCRIPTION = 160
MAX_COMPATIBILITY = 500  # the Agent Skills spec limit
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
OPENCLAW_FIELDS = {"requires", "os", "emoji", "primaryEnv", "always", "install"}
OPENCLAW_REQUIRES = {"bins", "anyBins", "env", "config"}
OPENCLAW_OS = {"darwin", "linux", "win32"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
OS_LABEL = {"darwin": "Darwin"}


def frontmatter(text: str) -> dict[str, str]:
    """The flat ``key: value`` block between the first two ``---`` lines."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise AssertionError("no frontmatter block")
    out: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        key, sep, value = line.partition(": ")
        if not sep or key != key.strip() or not key:
            raise AssertionError(f"frontmatter must be flat 'key: value' lines, got {line!r}")
        if key in out:
            raise AssertionError(f"duplicate key {key}")
        out[key] = value
    return out


def doctor_entries(package: str, kind: str) -> dict[str, bool]:
    return {name: required for name, required, _hint in doctor.PACKAGES[package].get(kind, [])}


class SkillMetadata(unittest.TestCase):
    def skills(self):
        found = sorted(p for p in REPO.glob("*/*/SKILL.md") if p.parents[1].name in doctor.PACKAGES)
        self.assertEqual(len(found), len(list(REPO.glob("*/*/SKILL.md"))), "a skill outside a doctor.py package")
        return found

    def test_fields_and_lengths(self):
        for skill_md in self.skills():
            with self.subTest(skill=skill_md.parent.name):
                fm = frontmatter(skill_md.read_text(encoding="utf-8"))
                self.assertLessEqual(set(fm), SPEC_FIELDS, "only Agent Skills spec fields")
                self.assertEqual(fm.get("name"), skill_md.parent.name)
                self.assertRegex(fm["name"], NAME_RE)
                desc = fm.get("description", "")
                self.assertTrue(desc.strip(), "description is required")
                self.assertLessEqual(len(desc), MAX_DESCRIPTION, f"{len(desc)} chars: {desc}")
                self.assertIn("Use ", desc, "say when to use the skill, not only what it does")
                if "compatibility" in fm:
                    self.assertTrue(1 <= len(fm["compatibility"]) <= MAX_COMPATIBILITY)
                for key, value in fm.items():
                    if key == "metadata":
                        continue
                    # Unquoted YAML scalars: ': ' starts a nested mapping, ' #' a comment,
                    # and a leading indicator character changes the type.
                    self.assertNotIn(": ", value, f"{key}: reword, or a YAML parser splits it")
                    self.assertNotIn(" #", value, f"{key}: reword, ' #' starts a YAML comment")
                    self.assertNotIn(value[:1], list("[{\"'*&!%@`|>"), f"{key}: leading YAML indicator")

    def test_real_yaml_parser_agrees(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        for skill_md in self.skills():
            with self.subTest(skill=skill_md.parent.name):
                text = skill_md.read_text(encoding="utf-8")
                fm = frontmatter(text)
                parsed = yaml.safe_load(re.match(r"---\n(.*?)\n---\n", text, re.S).group(1))
                expected = {k: (json.loads(v) if k == "metadata" else v) for k, v in fm.items()}
                self.assertEqual(parsed, expected)

    def test_openclaw_gates_match_doctor(self):
        for skill_md in self.skills():
            fm = frontmatter(skill_md.read_text(encoding="utf-8"))
            if "metadata" not in fm:
                continue
            package = skill_md.parents[1].name
            with self.subTest(skill=skill_md.parent.name):
                meta = json.loads(fm["metadata"])
                self.assertEqual(set(meta), {"openclaw"}, "metadata holds only the OpenClaw block")
                oc = meta["openclaw"]
                self.assertLessEqual(set(oc), OPENCLAW_FIELDS)
                requires = oc.get("requires", {})
                self.assertLessEqual(set(requires), OPENCLAW_REQUIRES)
                bins = doctor_entries(package, "binary")
                for name in requires.get("bins", []):
                    self.assertIsInstance(name, str)
                    self.assertIn(name, bins, f"{name} is gated here but doctor.py does not check it")
                    self.assertTrue(bins[name], f"{name} is a hard gate here but optional in doctor.py")
                oses = doctor_entries(package, "os")
                for name in oc.get("os", []):
                    self.assertIn(name, OPENCLAW_OS)
                    label = OS_LABEL.get(name, name)
                    self.assertIn(label, oses, f"{name} is gated here but doctor.py does not check it")
                    self.assertTrue(oses[label], f"{name} is a hard gate here but optional in doctor.py")


if __name__ == "__main__":
    unittest.main()
