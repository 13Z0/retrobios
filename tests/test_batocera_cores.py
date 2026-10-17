"""Batocera runs every emulator its ES lists, not only each system's default.

The platform scraper read configgen-defaults.yml alone, one core per system,
so DOSBox Staging, melonDS DS and nine other cores Batocera builds and lets a
user pick stayed outside the pack. The target scraper read es_systems.yml but
dropped a standalone emulator's own name, keeping only the machines listed
under it: amiberry, fsuae and vice were never in a target list.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

ES_SYSTEMS = """\
dos:
  emulators:
    libretro:
      dosbox_pure: { requireAnyOf: [BR2_PACKAGE_LIBRETRO_DOSBOX_PURE] }
      dosbox_staging: { requireAnyOf: [BR2_PACKAGE_LIBRETRO_DOSBOX_STAGING] }
    dosbox:
      dosbox: { requireAnyOf: [BR2_PACKAGE_DOSBOX] }
amiga500:
  emulators:
    amiberry:
      A500: { requireAnyOf: [BR2_PACKAGE_AMIBERRY] }
      archs_include: [x86_64]
"""


class ThePlatformListsEveryOfferedCore(unittest.TestCase):
    def test_libretro_cores_and_standalone_emulators(self):
        from scripts.scraper.batocera_scraper import es_systems_emulators

        cores, standalone = es_systems_emulators(ES_SYSTEMS)
        self.assertEqual(
            cores, {"dosbox_pure", "dosbox_staging", "dosbox", "amiberry", "A500"}
        )
        self.assertEqual(standalone, {"dosbox", "amiberry", "A500"})

    def test_the_overrides_drop_the_machine_selectors(self):
        from scripts.scraper.batocera_scraper import _not_cores

        self.assertIn("A500", _not_cores())

    def test_the_registry_leaves_the_list_to_the_scraper(self):
        import yaml

        registry = yaml.safe_load((REPO_ROOT / "platforms" / "_registry.yml").read_text())
        self.assertNotIn("cores", registry["platforms"]["batocera"])
        platform = yaml.safe_load((REPO_ROOT / "platforms" / "batocera.yml").read_text())
        self.assertIn("dosbox_staging", platform["cores"])


class TheTargetsNameTheStandaloneEmulator(unittest.TestCase):
    def test_the_group_name_is_a_core(self):
        from scripts.scraper.targets.batocera_targets_scraper import _parse_es_systems

        mapping = _parse_es_systems(ES_SYSTEMS)
        self.assertEqual(mapping["BR2_PACKAGE_AMIBERRY"], ["amiberry", "A500"])
        self.assertEqual(mapping["BR2_PACKAGE_LIBRETRO_DOSBOX_STAGING"], ["dosbox_staging"])


if __name__ == "__main__":
    unittest.main()
