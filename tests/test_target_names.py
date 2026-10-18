"""A hardware target names its artifacts by the name it is filed under.

`switch`, `nx` and `nintendo-switch` are one RetroArch target. The tag came
from the name as typed, so one target gave three pack and manifest names,
and `--verify-packs --target nx` looked for a pack nobody had built.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import canonical_target_name  # noqa: E402


class AliasesResolveToOneName(unittest.TestCase):
    def test_retroarch_switch_aliases(self):
        platforms = str(REPO_ROOT / "platforms")
        for typed in ("switch", "nx", "nintendo-switch"):
            with self.subTest(typed=typed):
                self.assertEqual(
                    canonical_target_name(["retroarch"], typed, platforms),
                    "nintendo-switch",
                )

    def test_raspberry_pi_models_reach_batocera_boards(self):
        """`--target rpi4`, the example every --help gives, was refused on
        Batocera: its boards are named by SoC and no alias pointed there."""
        platforms = str(REPO_ROOT / "platforms")
        for typed, board in (("rpi4", "bcm2711"), ("rpi5", "bcm2712"),
                             ("rpi3", "bcm2837"), ("rpi-zero", "bcm2835")):
            with self.subTest(typed=typed):
                self.assertEqual(canonical_target_name(["batocera"], typed, platforms), board)

    def test_linux_on_arm_and_32_bit_x86_is_a_target(self):
        """The buildbot's linux/aarch64 (232 cores) and linux/x86 builds were
        not scraped: --target refused RetroArch on Raspberry Pi OS 64-bit."""
        import yaml  # noqa: PLC0415

        platforms = str(REPO_ROOT / "platforms")
        targets = yaml.safe_load((REPO_ROOT / "platforms" / "targets" / "retroarch.yml")
                                 .read_text())["targets"]
        for typed, target in (("arm64", "linux-aarch64"), ("i686", "linux-x86")):
            with self.subTest(typed=typed):
                self.assertEqual(canonical_target_name(["retroarch"], typed, platforms), target)
                self.assertGreater(len(targets[target]["cores"]), 100)

    def test_an_unknown_name_is_left_for_the_target_check(self):
        self.assertEqual(
            canonical_target_name(["retroarch"], "bogus", str(REPO_ROOT / "platforms")),
            "bogus",
        )


class TheBuilderNamesByTheCanonicalTarget(unittest.TestCase):
    def test_an_alias_writes_the_canonical_manifest(self):
        if not (REPO_ROOT / "database.json").exists():
            self.skipTest("database.json is not built")
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp") as out:
            proc = subprocess.run(
                [sys.executable, "scripts/generate_pack.py", "--platform", "retroarch",
                 "--target", "nx", "--manifest", "--offline", "--output-dir", out],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=600, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                sorted(p.name for p in Path(out).glob("*.json")),
                ["retroarch_nintendoswitch.json"],
            )



class EachPlatformNamesTheTargetItsOwnWay(unittest.TestCase):
    """rpi4 is Batocera's bcm2711, Recalbox's rpi4_64 and RetroPie's rpi4.
    Named once for the run, --all kept "rpi4" for all three while
    --platform batocera named the same pack Bcm2711, so --verify-packs
    answered SKIP for a pack the --all build had written."""

    def test_the_all_run_and_the_platform_run_agree(self):
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        import generate_pack  # noqa: PLC0415

        platforms_dir = str(REPO_ROOT / "platforms")
        with contextlib.redirect_stdout(io.StringIO()):
            expected = generate_pack._expected_pack_names(
                ["batocera", "recalbox"], platforms_dir, None, "rpi4"
            )
        self.assertTrue(any("_Bcm2711_" in name for name in expected["batocera"]))
        self.assertTrue(any("_Rpi464_" in name for name in expected["recalbox"]))
        for platform, tag in (("batocera", "bcm2711"), ("recalbox", "rpi4_64")):
            self.assertEqual(
                generate_pack._platform_target(platform, "rpi4", platforms_dir), tag
            )

    def test_no_naming_site_reads_the_typed_target(self):
        source = (REPO_ROOT / "scripts" / "generate_pack.py").read_text()
        self.assertNotIn("target_name=args.target,\n                        one_per_slot", source)
        self.assertNotIn("args.target = canonical_target_name(", source)


class TheListingCountsWhatTheFilterUses(unittest.TestCase):
    """--list-targets showed Batocera x86_64 with 198 cores; --target x86_64
    filters on 187 once _overrides.yml removes machine variants and launchers."""

    def test_every_listed_count_is_the_filtered_set(self):
        from common import list_available_targets, load_target_config  # noqa: PLC0415

        platforms = str(REPO_ROOT / "platforms")
        checked = 0
        for target_file in sorted((REPO_ROOT / "platforms" / "targets").glob("[!_]*.yml")):
            platform = target_file.stem
            for row in list_available_targets(platform, platforms):
                with self.subTest(platform=platform, target=row["name"]):
                    resolved = load_target_config(platform, row["name"], platforms)
                    self.assertEqual(row["core_count"],
                                     None if resolved is None else len(resolved))
                checked += 1
        self.assertGreater(checked, 0)


class ASingleTargetFiltersNothing(unittest.TestCase):
    """CLAUDE.md's rule, held on the real data: RetroBat, RetroDECK and RomM
    have one target each, and hand-kept lists dropped 98, 300 and 132 cores
    from a pack the user had only asked to name."""

    def test_the_only_target_resolves_every_declared_core(self):
        import glob  # noqa: PLC0415

        import yaml  # noqa: PLC0415

        from common import (  # noqa: PLC0415
            load_emulator_profiles,
            load_platform_config,
            load_target_config,
            resolve_platform_cores,
        )

        platforms = str(REPO_ROOT / "platforms")
        profiles = load_emulator_profiles(str(REPO_ROOT / "emulators"))
        checked = 0
        for path in sorted(glob.glob(str(REPO_ROOT / "platforms" / "targets" / "*.yml"))):
            name = Path(path).stem
            if name.startswith("_"):
                continue
            targets = yaml.safe_load(Path(path).read_text())["targets"]
            if len(targets) != 1:
                continue
            with self.subTest(platform=name):
                config = load_platform_config(name, platforms)
                only = load_target_config(name, next(iter(targets)), platforms)
                self.assertEqual(resolve_platform_cores(config, profiles, target_cores=only),
                                 resolve_platform_cores(config, profiles))
                checked += 1
        self.assertGreaterEqual(checked, 3)

if __name__ == "__main__":
    unittest.main()
