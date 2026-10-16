"""A platform's region filter listens to the emulators it runs only.

The index was built over every profile: Pizza Boy SC, an Android app no
frontend here runs as a core, declares bios_CD_E.bin without a region, which
voided the [europe] tag Genesis Plus GX and PicoDrive give it, and
`--region us` kept both foreign Mega-CD BIOSes in the RetroArch pack.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import region as region_mod  # noqa: E402

PROFILES = {
    "gpgx": {"emulator": "GPGX", "type": "libretro", "systems": ["sega-mega-cd"],
             "files": [{"name": "bios_CD_E.bin", "region": ["europe"]}]},
    "pizza": {"emulator": "Pizza", "type": "standalone", "systems": ["sega-mega-cd"],
              "files": [{"name": "bios_CD_E.bin"}]},
}


class OnlyThePlatformsCores(unittest.TestCase):
    def test_a_foreign_untagged_entry_does_not_void_the_tag(self):
        config = {"cores": ["gpgx"], "systems": {"sega-mega-cd": {"files": []}}}
        index = region_mod.platform_region_index(config, PROFILES)
        self.assertFalse(index["bios_CD_E.bin"]["has_untagged"])
        self.assertTrue(region_mod.build_region_index(PROFILES)["bios_CD_E.bin"]["has_untagged"])

    def test_every_platform_site_reads_it(self):
        for module in ("verify.py", "generate_pack.py", "packverify.py"):
            source = (REPO_ROOT / "scripts" / module).read_text(encoding="utf-8")
            self.assertNotIn("build_region_index(emu_profiles", source, module)
            self.assertNotIn("build_region_index(profiles)", source, module)
            self.assertIn("platform_region_index(", source, module)


if __name__ == "__main__":
    unittest.main()
