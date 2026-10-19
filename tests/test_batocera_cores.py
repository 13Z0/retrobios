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



class AnEmulatorLaunchedBothWaysGetsBothLayouts(unittest.TestCase):
    """es_systems.yml offers libretro:dolphin and standalone dolphin for
    GameCube. Filed as standalone only, the pack gave libretro Dolphin no
    dolphin-emu/Sys/GC/ files: no IPL, no DSP ROM."""

    def test_the_scraper_names_dual_mode_emulators(self):
        from scripts.scraper.batocera_scraper import es_systems_emulators, es_systems_libretro

        text = ES_SYSTEMS + (
            "gamecube:\n  emulators:\n    libretro:\n      dolphin: { requireAnyOf: [X] }\n"
            "    dolphin:\n      dolphin: { requireAnyOf: [Y] }\n"
        )
        _cores, standalone = es_systems_emulators(text)
        self.assertEqual(standalone & es_systems_libretro(text), {"dolphin"})

    def test_the_layouts_are_both(self):
        from common import DUAL_MODE_PREFIX, platform_layouts

        profile = {"type": "standalone + libretro", "cores": ["dolphin"], "files": []}
        both = platform_layouts("dolphin", profile, {"dolphin", DUAL_MODE_PREFIX + "dolphin"})
        self.assertEqual([standalone for _view, standalone in both], [False, True])
        only = platform_layouts("dolphin", profile, {"dolphin"})
        self.assertEqual([standalone for _view, standalone in only], [True])

    def test_the_batocera_manifest_carries_both(self):
        import json

        manifest = json.loads((REPO_ROOT / "install" / "batocera.json").read_text())
        dests = {entry["dest"] for entry in manifest["files"]}
        self.assertIn("dolphin-emu/Sys/GC/USA/IPL.bin", dests)
        self.assertIn("GC/USA/IPL.bin", dests)

    def test_retrobat_and_recalbox_name_theirs(self):
        from scripts.scraper.recalbox_scraper import system_ini_cores, system_ini_libretro
        from scripts.scraper.retrobat_scraper import es_systems_dual

        cfg = ("<systemList><system><emulators>"
               "<emulator name=\"libretro\"><cores><core>dolphin</core></cores></emulator>"
               "<emulator name=\"dolphin\"/></emulators></system></systemList>")
        self.assertEqual(es_systems_dual(cfg), {"dolphin"})
        ini = "[core.0]\nemulator = libretro\ncore = dolphin\n[core.1]\nemulator = dolphin\ncore = dolphin\n"
        _cores, standalone = system_ini_cores(ini)
        self.assertEqual(system_ini_libretro(ini) & standalone, {"dolphin"})

    def test_upstream_yaml_is_read_as_its_code_reads_it(self):
        """configgen-defaults.yml names rtcw's core twice. The strict loader
        stopped the rescrape, while Batocera's PyYAML keeps the last value."""
        from safeparse import yaml_load_upstream

        self.assertEqual(
            yaml_load_upstream("rtcw:\n  core: iortcw\n  core: taradino\n"),
            {"rtcw": {"core": "taradino"}},
        )


if __name__ == "__main__":
    unittest.main()
