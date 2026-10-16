"""A check a profile declares has a value to compare against.

validation: [crc32] with no crc32 ran nothing, and fourteen profiles carried
one. validate_schemas now refuses it.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class ChecksHaveValues(unittest.TestCase):
    def setUp(self):
        try:
            import validate_schemas
        except ImportError as exc:
            self.skipTest(f"validate_schemas needs jsonschema: {exc}")
        self.check = validate_schemas._checks_without_values

    def test_a_valueless_check_is_named(self):
        path = REPO_ROOT / "emulators" / "x.yml"
        document = {"files": [
            {"name": "a.bin", "validation": ["crc32"]},
            {"name": "b.bin", "validation": ["size"], "min_size": 4},
            {"name": "c.bin", "validation": {"core": ["size"], "upstream": ["sha1"]},
             "size": 4},
        ]}
        found = self.check(path, document)
        self.assertEqual(len(found), 2)
        self.assertIn("a.bin", found[0])
        self.assertIn("c.bin", found[1])

    def test_every_tracked_profile(self):
        tracked = subprocess.run(
            ["git", "ls-files", "emulators/*.yml"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        found = []
        for name in tracked:
            path = REPO_ROOT / name
            with path.open(encoding="utf-8") as fh:
                found.extend(self.check(path, yaml.safe_load(fh)))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
