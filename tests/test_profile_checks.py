"""A profile's checks are the ones the emulator's code runs.

Each case reads a profile from emulators/ and offers its entry files of
chosen sizes or contents, then compares the verdict with what the code at
the profile's pin does with the same file. The cases come from reading that
code: a check the code does not run rejects a file the emulator loads, and a
check left out lets through a file the emulator refuses.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validation import _build_validation_index, check_file_validation  # noqa: E402


def _profile(name: str) -> dict:
    with open(REPO_ROOT / "emulators" / f"{name}.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class _Probe(unittest.TestCase):
    """Offer a profile entry a file and read the verdict."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def accepts(self, emulator: str, name: str, data: bytes) -> bool:
        index = _build_validation_index({emulator: _profile(emulator)})
        path = Path(self._tmp.name) / name
        path.write_bytes(data)
        return check_file_validation(str(path), name, index, emulators={emulator}) is None


class BeebEmTeletextFont(_Probe):
    """BuildMode7Font reads 96 characters of 18 two-byte rows, 3456 bytes,
    and returns false at the first EOF, which stops BeebEm at startup;
    bytes past them are never read (Src/Video.cpp:167-215 at a982ba6)."""

    def test_short_is_refused_and_longer_is_read(self):
        self.assertFalse(self.accepts("beebem", "Teletext.fnt", b"\0" * 100))
        self.assertTrue(self.accepts("beebem", "Teletext.fnt", b"\0" * 3456))
        self.assertTrue(self.accepts("beebem", "Teletext.fnt", b"\0" * 4000))


if __name__ == "__main__":
    unittest.main()
