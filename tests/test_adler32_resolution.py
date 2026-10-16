"""A file answered by its path or name must carry a checksum its core takes.

Dolphin hashes its DSP ROMs with Adler-32 and warns on every pair older
than the v0.4 free ROM. The collection stores the v0.3.1 dsp_rom.bin at the
path Dolphin reads, so the resolver answered it there: shipped next to the
official dsp_coef.bin, it made a pair Dolphin rejects. The pack builder
corrected it later, every other reader of the resolution did not.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import adler32_of, resolve_local_file  # noqa: E402


class TheAcceptedChecksumWins(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        self.old = root / "bios/Sys/GC/dsp_rom.bin"
        self.good = root / "bios/.variants/dsp_rom.bin.good"
        files, by_name, by_suffix = {}, {}, {}
        for path, data in ((self.old, b"\x01\x02" * 8), (self.good, b"\x03\x04" * 8)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            sha1 = hashlib.sha1(data).hexdigest()
            files[sha1] = {"path": str(path), "name": "dsp_rom.bin", "sha1": sha1,
                           "size": len(data)}
            by_name.setdefault("dsp_rom.bin", []).append(sha1)
        by_suffix["GC/dsp_rom.bin"] = [hashlib.sha1(self.old.read_bytes()).hexdigest()]
        self.db = {"files": files, "indexes": {"by_name": by_name, "by_md5": {},
                                               "by_path_suffix": by_suffix,
                                               "by_crc32": {}}}
        self.entry = {
            "name": "dsp_rom.bin", "path": "GC/dsp_rom.bin", "size": 16,
            "validation": ["size", "adler32"], "adler32_byteswap": True,
            "known_hash_adler32": [f"0x{adler32_of(str(self.good), True)}"],
        }

    def test_the_path_gives_way_to_the_accepted_file(self):
        path, status = resolve_local_file(self.entry, self.db, dest_hint="GC/dsp_rom.bin")
        self.assertEqual((path, status), (str(self.good), "name_exact"))

    def test_an_informative_value_rejects_nothing(self):
        entry = {**self.entry, "validation": ["size"]}
        path, status = resolve_local_file(entry, self.db, dest_hint="GC/dsp_rom.bin")
        self.assertEqual((path, status), (str(self.old), "path_exact"))

    def test_the_swap_is_on_16_bit_words(self):
        self.assertEqual(adler32_of(str(self.old)),
                         format(zlib.adler32(b"\x01\x02" * 8), "08x"))
        self.assertEqual(adler32_of(str(self.old), True),
                         format(zlib.adler32(b"\x02\x01" * 8), "08x"))


class DolphinGetsAPairItTakes(unittest.TestCase):
    def test_each_dolphin_dsp_rom_resolves_to_an_accepted_dump(self):
        from common import load_emulator_profiles  # noqa: PLC0415

        database = REPO_ROOT / "database.json"
        if not database.exists():
            self.skipTest("database.json is not generated")
        db = json.loads(database.read_text())
        profile = load_emulator_profiles(str(REPO_ROOT / "emulators"))["dolphin"]
        for entry in profile["files"]:
            if entry["name"] not in ("dsp_rom.bin", "dsp_coef.bin"):
                continue
            with self.subTest(name=entry["name"]):
                path, _status = resolve_local_file(
                    {**entry, "source_profile": "dolphin"}, db, dest_hint=entry["path"]
                )
                self.assertIsNotNone(path)
                accepted = {v.lower().removeprefix("0x") for v in entry["known_hash_adler32"]}
                self.assertIn(adler32_of(path, True), accepted)


if __name__ == "__main__":
    unittest.main()
