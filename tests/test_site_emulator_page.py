"""What an emulator page says about a profile and its files.

No network and no database: pages are rendered from small profiles.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_site as gs  # noqa: E402

EMPTY_DB = {"files": {}, "indexes": {"by_name": {}}}


def _page(name: str, profile: dict) -> str:
    return gs.generate_emulator_page(name, profile, EMPTY_DB, None, set())


class WhereAndHowAFileIsRead(unittest.TestCase):
    """load_from, found_by and on_mismatch were in 99, 4 and 25 entries and
    on no page: Dolphin's gba_bios.bin showed a Path a RetroArch user put
    under system/, while the libretro core reads it from its save directory."""

    def test_load_from(self):
        page = _page("dolphin", {"type": "standalone + libretro", "files": [
            {"name": "gba_bios.bin", "path": "GBA/gba_bios.bin",
             "load_from": {"libretro": "save_dir"}},
            {"name": "hdd.rom", "load_from": "hdd_image"},
            {"name": "bios.bin", "load_from": "system_dir"},
        ]})
        self.assertIn("- Read from: the save directory, not the BIOS directory "
                      "(libretro build)", page)
        self.assertIn("- Read from: inside the emulated hard disk image", page)
        self.assertEqual(page.count("Read from:"), 2)

    def test_found_by_and_on_mismatch(self):
        page = _page("x", {"type": "standalone", "found_by": "content", "files": [
            {"name": "scph5501.bin", "found_by": "content", "on_mismatch": "warn"},
            {"name": "boot.bin", "on_mismatch": {"signature": "warn"}},
        ]})
        self.assertIn("| Files found by | content (the code scans the folder", page)
        self.assertIn("- Found by: content (", page)
        self.assertIn("- On a failed check: warns and runs with the file", page)
        self.assertIn("- On a failed signature check: warns and runs with the file", page)


class WhoShipsTheBuildAndWhy(unittest.TestCase):
    """built_by, pin_frozen and version_source were in 9, 13 and 40 profiles
    and on no page: mame0278 did not say only Recalbox ships it."""

    def test_meta_rows(self):
        page = _page("mame0278", {
            "type": "libretro", "built_by": ["recalbox"],
            "pin_frozen": "ROCKNIX 20261001 builds this revision.",
            "version_source": {"url": "https://example.org/news", "pattern": "v(\\S+)",
                               "no_genuine_copy": "13.0"},
            "files": [],
        })
        self.assertIn("| Built by | [recalbox](../platforms/recalbox.md), under its "
                      "core name.", page)
        self.assertIn("| Revision held | ROCKNIX 20261001 builds this revision. |", page)
        self.assertIn("| Releases announced at | [https://example.org/news]"
                      "(https://example.org/news) (version 13.0 announced there, "
                      "found only as rebuilt copies) |", page)


if __name__ == "__main__":
    unittest.main()
