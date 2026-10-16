"""A platform page shows each row the hash its own declaration carries.

The page joined verify's details to the platform YAML by file name alone,
the last declaration winning: MiSTer declares boot.rom 23 times with 21
distinct MD5s, and every boot.rom row showed the same one, so a user
checking a dump against the page was told the wrong hash.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_site import _render_platform_systems  # noqa: E402

A, B, C = "a" * 32, "b" * 32, "c" * 32


class EachRowItsDeclaration(unittest.TestCase):
    def test_same_name_in_two_systems_and_twice_in_one(self):
        config_files = {
            ("psx", "boot.rom"): [{"name": "boot.rom", "md5": A}],
            ("saturn", "boot.rom"): [
                {"name": "boot.rom", "md5": B},
                {"name": "boot.rom", "md5": C},
            ],
        }
        by_system = {
            "psx": [{"name": "boot.rom", "status": "ok", "system": "psx"}],
            "saturn": [
                {"name": "boot.rom", "status": "ok", "system": "saturn"},
                {"name": "boot.rom", "status": "ok", "system": "saturn"},
            ],
        }
        text = "\n".join(_render_platform_systems(by_system, config_files, {}))
        psx, saturn = text.split("saturn", 1)[0], text.split("saturn", 1)[1]
        self.assertIn(A, psx)
        self.assertNotIn(B, psx)
        self.assertLess(saturn.index(B), saturn.index(C))
        self.assertNotIn(A, saturn)


if __name__ == "__main__":
    unittest.main()
