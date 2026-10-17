"""A core's file at a free platform destination answers an existence check.

The builder packs a core extra at a destination the platform declares and
does not hold, and in existence mode counts the declaration met: the
frontend sees a file at the path. verify kept it MISSING, so the pack said
1/1 OK and verify 0/1 for the same platform.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

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
            self.config, self.db, str(self.emulators), supplemental_names=set(),
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


if __name__ == "__main__":
    unittest.main()
