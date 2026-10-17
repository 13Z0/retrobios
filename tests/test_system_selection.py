"""One system, two spellings, one pack.

stella2014 and stella2023 write atari_2600 where every other profile writes
atari-2600. --system matched the spelling exactly and named the ZIP from
it, so the two spellings built two different packs under one file name,
the second replacing the first; --from-md5 --emulator X --standalone
accepted a libretro-only core and named a standalone pack that was the
libretro pack.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import common  # noqa: E402
import generate_pack as builder  # noqa: E402


class SpellingsAgree(unittest.TestCase):
    def test_both_spellings_select_the_same_profiles(self):
        profiles = common.load_emulator_profiles(str(REPO_ROOT / "emulators"))
        dashed = common.profiles_for_systems(profiles, ["atari-2600"])
        underscored = common.profiles_for_systems(profiles, ["atari_2600"])
        self.assertEqual(dashed, underscored)
        self.assertIn("stella2014", dashed)
        self.assertIn("gopher2600", dashed)

    def test_the_pack_name_normalizes_the_spelling(self):
        self.assertEqual(
            builder._system_display_name("atari_2600"), builder._system_display_name("atari-2600")
        )
        self.assertEqual(builder._system_display_name("msxturboR"), builder._system_display_name("msxturbor"))


class TheListingCountsLikeTheSelection(unittest.TestCase):
    def test_one_system_is_listed_once_with_every_profile(self):
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            common.list_system_ids(str(REPO_ROOT / "emulators"))
        lines = {line.split()[0]: line for line in out.getvalue().splitlines() if line.strip()}
        profiles = common.load_emulator_profiles(str(REPO_ROOT / "emulators"))
        for spellings in (("3do", "panasonic-3do"), ("atari-2600", "atari_2600")):
            listed = [s for s in spellings if s in lines]
            self.assertEqual(len(listed), 1, f"{spellings}: one line per system")
            count = len(common.profiles_for_systems(profiles, [spellings[0]]))
            self.assertIn(f"({count} emulator", lines[listed[0]])


class StandaloneNeedsAStandaloneBuild(unittest.TestCase):
    def test_a_custom_pack_refuses_it_for_a_libretro_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "emulators").mkdir()
            Path(tmp, "emulators", "handy.yml").write_text(
                "emulator: Handy\ntype: libretro\nsystems: [atari-lynx]\nfiles:\n  - name: lynxboot.img\n"
            )
            common._emulator_profiles_cache.clear()
            self.addCleanup(common._emulator_profiles_cache.clear)
            db = {"files": {}, "indexes": {"by_md5": {}, "by_crc32": {}, "by_name": {}}}
            result = builder.generate_md5_pack(
                [("md5", "d8f1206299c48946e6ec5ef96d014eaa")], db, tmp, tmp,
                emulator_name="handy", emulators_dir=str(Path(tmp, "emulators")), standalone=True,
            )
            self.assertIsNone(result)
            self.assertEqual([p for p in os.listdir(tmp) if p.endswith(".zip")], [])


class ADeclinedBuildIsAFailure(unittest.TestCase):
    def test_a_target_that_removes_every_requested_system_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "scripts/generate_pack.py", "--platform", "retroarch",
                 "--system", "sony-playstation-2", "--target", "switch", "--offline",
                 "--output-dir", tmp],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=600, check=False,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("Traceback", proc.stderr, proc.stderr)
            self.assertEqual([p for p in os.listdir(tmp) if p.endswith(".zip")], [])



class AMistypedPlatformIsNamed(unittest.TestCase):
    """verify and cross_reference ended a typo on a traceback; generate_pack
    ran its whole build first and then said "no pack for batocra"."""

    def test_every_cli_names_the_platform_it_meant(self):
        scratch = tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp")
        self.addCleanup(scratch.cleanup)
        out = Path(scratch.name) / "packs"
        for script, extra in (("verify.py", []), ("cross_reference.py", []),
                              ("generate_pack.py", ["--offline", "--output-dir", str(out)])):
            with self.subTest(script=script):
                proc = subprocess.run(
                    [sys.executable, f"scripts/{script}", "--platform", "batocra", *extra],
                    capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
                    check=False,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("Traceback", proc.stderr, proc.stderr)
                self.assertIn("Did you mean 'batocera'?", proc.stderr)
        self.assertFalse(out.exists())


class StrictVerifyFailsOnACriticalMiss(unittest.TestCase):
    """verify exited 0 with a required file missing on an md5 platform, so
    the pipeline printed "verify OK" over a pack the frontend rejects."""

    def test_strict_exits_nonzero_and_plain_does_not(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp") as tmp:
            root = Path(tmp)
            (root / "plat").mkdir()
            (root / "emu").mkdir()
            (root / "plat" / "fx.yml").write_text(
                "platform: Fx\nverification_mode: md5\nbase_destination: bios\n"
                "systems:\n  sony-playstation:\n    files:\n    - name: absent.bin\n"
                "      destination: absent.bin\n      required: true\n"
                "      md5: 00000000000000000000000000000000\n"
            )
            (root / "db.json").write_text(
                '{"files": {}, "indexes": {"by_md5": {}, "by_name": {}, '
                '"by_crc32": {}, "by_path_suffix": {}}}'
            )
            base = [sys.executable, "scripts/verify.py", "--platform", "fx",
                    "--platforms-dir", str(root / "plat"), "--emulators-dir",
                    str(root / "emu"), "--db", str(root / "db.json")]
            plain = subprocess.run(base, capture_output=True, text=True,
                                   cwd=str(REPO_ROOT), timeout=300, check=False)
            strict = subprocess.run([*base, "--strict"], capture_output=True, text=True,
                                    cwd=str(REPO_ROOT), timeout=300, check=False)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(strict.returncode, 1, strict.stderr)
        self.assertIn("1 required file(s) missing", strict.stderr)

    def test_the_pipeline_asks_for_it(self):
        source = (REPO_ROOT / "scripts" / "pipeline.py").read_text()
        self.assertIn('"scripts/verify.py", "--all", "--strict"', source)


class ExclusionNotesListedOnAsk(unittest.TestCase):
    """verify --all repeated every core's exclusion note under every
    platform: 985 of its 1114 lines, the platform results lost among them."""

    def test_counted_by_default_listed_with_verbose(self):
        if not (REPO_ROOT / "database.json").exists():
            self.skipTest("database.json is not generated")
        cmd = [sys.executable, "scripts/verify.py", "--platform", "retroarch"]
        plain = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT),
                               timeout=900, check=True).stdout
        verbose = subprocess.run([*cmd, "--verbose"], capture_output=True, text=True,
                                 cwd=str(REPO_ROOT), timeout=900, check=True).stdout
        self.assertRegex(plain, r"No external files: \d+ cores \(--verbose lists them\)")
        self.assertRegex(verbose, r"No external files \(\d+\):")
        self.assertLess(len(plain.splitlines()), len(verbose.splitlines()))


class ATypoGetsTheNearestNames(unittest.TestCase):
    """The hint listed the first names of the alphabet behind "...":
    sony-playstaton was answered with 3do to apple-iie."""

    def test_suggestions_follow_the_typed_name(self):
        hint = common.suggest_names(["sony-playstaton"],
                                    ["3do", "apple-ii", "sony-playstation"], "--list-systems")
        self.assertEqual(hint, "Did you mean: sony-playstation? --list-systems lists all 3.")
        self.assertEqual(common.suggest_names(["zzz"], ["3do"], "--list-systems"),
                         "Known: 3do.")
        many = [f"system-{i}" for i in range(40)]
        self.assertEqual(common.suggest_names(["zzz"], many, "--list-systems"),
                         "--list-systems lists all 40.")


class EmulatorVerifyReadsNoArchiveItDoesNotNeed(unittest.TestCase):
    """verify --emulator decompressed every archive of the collection to
    index ZIP members, for zipped_file entries no profile declares: 23 of
    the 24 seconds of a run."""

    def test_the_member_index_is_not_built(self):
        from unittest import mock  # noqa: PLC0415

        import verify  # noqa: PLC0415

        if not (REPO_ROOT / "database.json").exists():
            self.skipTest("database.json is not generated")
        db = common.load_database(str(REPO_ROOT / "database.json"))
        with mock.patch.object(verify, "build_zip_contents_index",
                               side_effect=AssertionError("index built")):
            result = verify.verify_emulator(["handy"], str(REPO_ROOT / "emulators"), db)
        self.assertTrue(result)

if __name__ == "__main__":
    unittest.main()
