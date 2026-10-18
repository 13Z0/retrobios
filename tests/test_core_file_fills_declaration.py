"""A core's file at a free platform destination answers an existence check.

The builder packs a core extra at a destination the platform declares and
does not hold, and in existence mode counts the declaration met: the
frontend sees a file at the path. verify kept it MISSING, so the pack said
1/1 OK and verify 0/1 for the same platform.
"""

from __future__ import annotations

import hashlib
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import _emulator_profiles_cache, load_platform_config  # noqa: E402
from generate_pack import generate_pack  # noqa: E402
from verify import verify_platform  # noqa: E402


class ACoreFileFillsAFreeDeclaration(unittest.TestCase):
    def setUp(self):
        _emulator_profiles_cache.clear()
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        bios = self.root / "bios" / "Maker" / "Console"
        bios.mkdir(parents=True)
        data = b"HELD BOOT ROM"
        (bios / "boot.bin").write_bytes(data)
        sha1 = hashlib.sha1(data).hexdigest()
        md5 = hashlib.md5(data).hexdigest()
        self.db = {
            "files": {sha1: {
                "path": str(bios / "boot.bin"), "name": "boot.bin",
                "md5": md5, "crc32": "", "size": len(data),
            }},
            "indexes": {
                "by_md5": {md5: sha1}, "by_name": {"boot.bin": [sha1]},
                "by_crc32": {}, "by_path_suffix": {},
            },
        }
        self.platforms = self.root / "platforms"
        self.emulators = self.root / "emulators"
        self.platforms.mkdir()
        self.emulators.mkdir()
        (self.platforms / "fakeplat.yml").write_text(yaml.safe_dump({
            "platform": "FakePlat",
            "verification_mode": "existence",
            "base_destination": "system",
            "cores": ["fakecore"],
            "systems": {"console-a": {"files": [
                {"name": "zzz_absent.bin", "destination": "zzz_absent.bin",
                 "required": True},
            ]}},
        }))
        # The entry's name differs from the destination it fills, as
        # descriptive names do ("MDA font ROM" at mda.rom).
        (self.emulators / "fakecore.yml").write_text(yaml.safe_dump({
            "emulator": "FakeCore", "type": "libretro", "cores": ["fakecore"],
            "systems": ["console-a"],
            "files": [{"name": "Fake boot ROM", "path": "zzz_absent.bin",
                       "sha1": sha1, "required": True}],
        }))
        self.config = load_platform_config("fakeplat", str(self.platforms))

    def test_verify_counts_what_the_pack_counts(self):
        result = verify_platform(
            self.config, self.db, str(self.emulators),
        )
        out = self.root / "out"
        with redirect_stdout(io.StringIO()):
            zip_path = generate_pack(
                "fakeplat", str(self.platforms), self.db, str(self.root / "bios"),
                str(out), emulators_dir=str(self.emulators), offline=True,
            )
        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("zzz_absent.bin", zf.namelist())
        self.assertEqual(result["status_counts"], {"ok": 1})
        self.assertEqual(
            [d["status"] for d in result["details"]], ["ok"],
        )



class AFileDeclaredUnderAnotherName(unittest.TestCase):
    """The platform declares ROM1 with the md5 of the core's ROM. The
    builder packs ROM at the core's path; verify read the enriched
    declarations, took ROM as declared and never counted it."""

    def test_the_report_lists_what_the_builder_packs(self):
        _emulator_profiles_cache.clear()
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        bios = root / "bios" / "Apple" / "IIgs"
        bios.mkdir(parents=True)
        data = b"IIGS ROM BYTES"
        (bios / "ROM").write_bytes(data)
        sha1, md5 = hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest()
        db = {
            "files": {sha1: {"path": str(bios / "ROM"), "name": "ROM", "md5": md5,
                             "crc32": "", "size": len(data)}},
            "indexes": {"by_md5": {md5: sha1}, "by_name": {"ROM": [sha1]},
                        "by_crc32": {}, "by_path_suffix": {}},
        }
        (root / "platforms").mkdir()
        (root / "emulators").mkdir()
        (root / "platforms" / "fakeplat.yml").write_text(yaml.safe_dump({
            "platform": "FakePlat", "verification_mode": "existence",
            "base_destination": "system", "cores": ["gs"],
            "systems": {"apple-iigs": {"files": [
                {"name": "ROM1", "destination": "ROM1", "md5": md5},
            ]}},
        }))
        (root / "emulators" / "gs.yml").write_text(yaml.safe_dump({
            "emulator": "GS", "type": "libretro", "cores": ["gs"],
            "systems": ["apple-iigs"],
            "files": [{"name": "ROM", "path": "gs/ROM", "md5": md5, "required": True}],
        }))
        config = load_platform_config("fakeplat", str(root / "platforms"))
        result = verify_platform(config, db, str(root / "emulators"))
        with redirect_stdout(io.StringIO()):
            zip_path = generate_pack(
                "fakeplat", str(root / "platforms"), db, str(root / "bios"),
                str(root / "out"), emulators_dir=str(root / "emulators"), offline=True,
            )
        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("gs/ROM", zf.namelist())
        reported = [(u["name"], u["in_repo"]) for u in result["undeclared_files"]]
        self.assertIn(("ROM", True), reported)



class AMissingDataDirectoryFailsTheRun(unittest.TestCase):
    """A refresh refused by a quota left the PPSSPP pack with 2 files and
    the run exited 0; nothing downstream checks data directories."""

    def test_the_pack_records_it_and_main_exits_non_zero(self):
        import generate_pack as gp

        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "platforms").mkdir()
        (root / "emulators").mkdir()
        (root / "bios").mkdir()
        (root / "platforms" / "fakeplat.yml").write_text(yaml.safe_dump({
            "platform": "FakePlat", "verification_mode": "existence",
            "base_destination": "system", "cores": [],
            "systems": {"console-a": {"files": [], "data_directories": [
                {"ref": "assets", "destination": "assets"},
            ]}},
        }))
        db = {"files": {}, "indexes": {"by_md5": {}, "by_name": {}, "by_crc32": {},
                                      "by_path_suffix": {}}}
        gp._MISSING_DATA_DIRS.clear()
        self.addCleanup(gp._MISSING_DATA_DIRS.clear)
        with redirect_stdout(io.StringIO()):
            gp.generate_pack(
                "fakeplat", str(root / "platforms"), db, str(root / "bios"),
                str(root / "out"), emulators_dir=str(root / "emulators"),
                data_registry={"assets": {"local_cache": str(root / "absent")}},
                offline=True,
            )
        self.assertEqual(gp._MISSING_DATA_DIRS, {"assets"})
        with mock.patch.object(gp, "_main", lambda held: None), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            gp.main()
        self.assertEqual(stop.exception.code, 1)



class AFileNobodyHoldsIsAnOmission(unittest.TestCase):
    """DevilutionX's diabdat.mpq, required and not in the collection, was
    in neither the manifest's files nor its omitted_files: the installer
    showed no notice and said everything was verified."""

    def test_the_manifest_names_it(self):
        from generate_pack import generate_manifest

        _emulator_profiles_cache.clear()
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        for folder in ("platforms", "emulators", "bios"):
            (root / folder).mkdir()
        (root / "platforms" / "fakeplat.yml").write_text(yaml.safe_dump({
            "platform": "FakePlat", "verification_mode": "existence",
            "base_destination": "system", "cores": ["devil"],
            "systems": {"diablo": {"files": []}},
        }))
        (root / "platforms" / "_registry.yml").write_text(yaml.safe_dump({
            "platforms": {"fakeplat": {"config": "fakeplat.yml", "status": "active"}},
        }))
        (root / "emulators" / "devil.yml").write_text(yaml.safe_dump({
            "emulator": "Devil", "type": "libretro", "cores": ["devil"],
            "systems": ["diablo"],
            "files": [{"name": "diabdat.mpq", "required": True,
                       "sha1": "1" * 40, "category": "game_data"}],
        }))
        db = {"files": {}, "indexes": {"by_md5": {}, "by_name": {}, "by_crc32": {},
                                      "by_path_suffix": {}}}
        manifest = generate_manifest(
            "fakeplat", str(root / "platforms"), db, str(root / "bios"),
            str(root / "platforms" / "_registry.yml"),
            emulators_dir=str(root / "emulators"), offline=True,
        )
        omitted = {o["name"]: o for o in manifest["omitted_files"]}
        self.assertIn("diabdat.mpq", omitted)
        self.assertTrue(omitted["diabdat.mpq"]["required"])
        self.assertEqual(omitted["diabdat.mpq"]["reason"], "not_found")



class AMemberNameIsNotALooseFile(unittest.TestCase):
    """verify counted a core file "in pack" when its name existed only as a
    member of some ZIP (naomi2.zip's 315-6146.bin); the builder, which reads
    no archive member names, shipped nothing."""

    def test_verify_reads_undeclared_files_as_the_builder_does(self):
        import cross_reference

        _emulator_profiles_cache.clear()
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "platforms").mkdir()
        (root / "emulators").mkdir()
        (root / "platforms" / "fakeplat.yml").write_text(yaml.safe_dump({
            "platform": "FakePlat", "verification_mode": "existence",
            "base_destination": "system", "cores": ["naomi2core"],
            "systems": {"sega-naomi2": {"files": []}},
        }))
        (root / "emulators" / "naomi2core.yml").write_text(yaml.safe_dump({
            "emulator": "N2", "type": "libretro", "cores": ["naomi2core"],
            "systems": ["sega-naomi2"],
            "files": [{"name": "315-6146.bin", "required": True}],
        }))
        db = {"files": {}, "indexes": {"by_md5": {}, "by_name": {}, "by_crc32": {},
                                      "by_path_suffix": {}}}
        config = load_platform_config("fakeplat", str(root / "platforms"))
        with mock.patch.object(cross_reference, "_build_supplemental_index",
                               lambda *a, **k: {"315-6146.bin"}):
            result = verify_platform(config, db, str(root / "emulators"))
        self.assertEqual(
            [(u["name"], u["in_repo"]) for u in result["undeclared_files"]],
            [("315-6146.bin", False)],
        )


if __name__ == "__main__":
    unittest.main()
