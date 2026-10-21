"""The pack integrity check counts the core files the builder places.

It rebuilt the core extras with an empty set of taken paths and tested
presence alone. On a Recalbox pack it credited 68 core files at a path the
platform's own file holds, 15 of them other bytes (fuse/trdos.rom, the PS1
BIOS DuckStation reads), and printed a core count that contradicted the
builder's in the same run.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import packverify  # noqa: E402


def _held(files: dict, by_name: dict, path: str, payload: bytes) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(payload)
    sha1 = hashlib.sha1(payload).hexdigest()
    name = Path(path).name
    files[sha1] = {"path": path, "name": name, "sha1": sha1,
                   "md5": hashlib.md5(payload).hexdigest(), "size": len(payload)}
    by_name.setdefault(name, []).append(sha1)
    return sha1


class CoreFilesAsTheBuilderPlacesThem(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp")
        self.addCleanup(tmp.cleanup)
        previous = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous)
        files: dict = {}
        by_name: dict = {}
        _held(files, by_name, "bios/plat/trdos.rom", b"platform dump")
        core_trdos = _held(files, by_name, "bios/core/trdos.rom", b"core dump")
        _held(files, by_name, "bios/core/other.rom", b"core file")
        self.db = {"files": files, "indexes": {
            "by_name": by_name, "by_md5": {e["md5"]: s for s, e in files.items()},
            "by_path_suffix": {}, "by_crc32": {}, "by_sha256": {}}}
        Path("platforms").mkdir()
        config = {"platform": "P", "verification_mode": "existence", "cores": ["fuse"],
                  "systems": {"zx": {"files": [
                      {"name": "trdos.rom", "destination": "fuse/trdos.rom"}]}}}
        Path("platforms/p.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        Path("emulators").mkdir()
        self.profiles = {"fuse": {
            "emulator": "fuse", "type": "libretro", "systems": ["zx"], "files": [
                {"name": "trdos.rom", "path": "fuse/trdos.rom", "sha1": core_trdos},
                {"name": "other.rom", "path": "core/other.rom"},
            ]}}

    def _check(self, other: bytes) -> tuple:
        with zipfile.ZipFile("pack.zip", "w") as zf:
            zf.writestr("fuse/trdos.rom", b"platform dump")
            zf.writestr("core/other.rom", other)
        return packverify.verify_pack_against_platform(
            "pack.zip", "p", "platforms", self.db, emulators_dir="emulators",
            emu_profiles=self.profiles,
        )

    def test_a_path_the_platform_file_holds_is_not_a_core_file(self):
        ok, _checked, _present, errors, _bc, _bp, core_checked, core_present, _ex = (
            self._check(b"core file")
        )
        self.assertTrue(ok, errors)
        self.assertEqual((core_checked, core_present), (1, 1))

    def test_other_bytes_at_a_core_path_are_an_error(self):
        ok, _checked, _present, errors, *_rest = self._check(b"something else")
        self.assertFalse(ok)
        self.assertTrue(
            any(e.startswith("core file differs: core/other.rom") for e in errors), errors
        )


if __name__ == "__main__":
    unittest.main()
