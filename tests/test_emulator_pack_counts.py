"""An emulator pack and verify --emulator count the same slots the same way.

Accepted alternatives on one destination (raze's SW.GRP), unsourceable
entries and user-provided ones were counted one way by the pack and another
by verify: raze was "10 packed, 29 missing, 4 refused" against "10/23 OK,
1 untested, 12 missing", xash3d's thirteen unsourceable slots were three
names and ten "missing".
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# kronos: an archive unit under its archive_prefix and a loose same-named file.
# speccy,zesarux: the pack keeps speccy's PENTAGON.ROM and drops zesarux's
# 32 KB pentagon.rom, which verify called covered.
# dinothawr, cemu: data directories, whose members the pack counted among
# "files packed" (110 against verify's 0/0, 303 against 67/68).
CASES = (
    "raze", "rust_dos", "hatari", "xash3d_fwgs", "catacombgl", "openjk", "sonic2013",
    "kronos", "speccy,zesarux", "dinothawr", "cemu",
)


def _count(text: str, word: str) -> int:
    match = re.search(rf"(\d+) {word}", text)
    return int(match.group(1)) if match else 0


class PackAndReportAgree(unittest.TestCase):
    def test_each_emulator_pack_counts_as_verify_does(self):
        if not (REPO_ROOT / "database.json").is_file():
            self.skipTest("database.json is not built")
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        for emulator in CASES:
            with self.subTest(emulator=emulator), tempfile.TemporaryDirectory(
                dir=REPO_ROOT / "tmp"
            ) as out:
                pack = subprocess.run(
                    [sys.executable, "scripts/generate_pack.py", "--emulator", emulator,
                     "--offline", "--output-dir", out],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=900, check=True,
                ).stdout
                report = subprocess.run(
                    [sys.executable, "scripts/verify.py", "--emulator", emulator],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=900, check=False,
                ).stdout
                summary = next(line for line in pack.splitlines() if "_BIOS_Pack.zip:" in line)
                label = emulator.replace(",", " + ")
                verdict = next(line for line in report.splitlines() if line.startswith(f"{label}:"))
                ok = int(re.search(r"(\d+)/\d+ OK", verdict).group(1))
                self.assertEqual(_count(summary, "files packed"), ok, (summary, verdict))
                self.assertEqual(_count(summary, "missing"), _count(verdict, "missing"))
                self.assertEqual(_count(summary, "unsourceable"), _count(verdict, "unsourceable"))
                self.assertEqual(_count(summary, "refused"), _count(verdict, "untested"))


if __name__ == "__main__":
    unittest.main()
