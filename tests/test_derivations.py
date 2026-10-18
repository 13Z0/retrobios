"""Catalog entries the collection already holds in another layout.

391773/391774, the two Kickstart 3.1 chips of the A1200, are the high and low
16-bit words of the image the collection keeps, byte-swapped; TOSEC's Basic V7
is two held ROMs end to end. The gap report listed them as acquisition targets.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import derivations  # noqa: E402


def _held(path: Path, data: bytes) -> dict:
    path.write_bytes(data)
    return {"path": str(path), "size": len(data), "crc32": format(zlib.crc32(data), "08x"),
            "sha1": hashlib.sha1(data).hexdigest()}


def _wanted(data: bytes) -> dict:
    return {"sha1": hashlib.sha1(data).hexdigest(), "size": len(data),
            "crc32": format(zlib.crc32(data), "08x")}


class HeldDumpsAnswerCatalogEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_combine_agrees_with_zlib(self):
        for a, b in ((b"abc", b"defgh"), (os.urandom(999), os.urandom(4097))):
            self.assertEqual(derivations.crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)),
                             zlib.crc32(a + b))

    def test_a_word_split_byte_swapped_chip_is_found(self):
        image = os.urandom(64)
        db = {"files": {"x": _held(self.tmp / "kick.rom", image)}}
        high = derivations._swap16(derivations._words(image, 0))
        found = derivations.derivable([_wanted(high)], db)
        self.assertEqual(found[hashlib.sha1(high).hexdigest()]["transform"],
                         "even words, byte-swapped")

    def test_two_held_halves_make_the_catalogued_whole(self):
        low, high = os.urandom(32), os.urandom(32)
        db = {"files": {"a": _held(self.tmp / "basiclo.bin", low),
                        "b": _held(self.tmp / "basichi.bin", high)}}
        found = derivations.derivable([_wanted(low + high)], db)
        self.assertEqual(found[hashlib.sha1(low + high).hexdigest()]["from"],
                         [str(self.tmp / "basiclo.bin"), str(self.tmp / "basichi.bin")])

    def test_unrelated_bytes_are_not_claimed(self):
        db = {"files": {"a": _held(self.tmp / "a.bin", os.urandom(64))}}
        self.assertEqual(derivations.derivable([_wanted(os.urandom(32))], db), {})



class ARomsetMemberIsASource(unittest.TestCase):
    """cdi910.rom inside cdi910.zip is the CD-i 910 BIOS byte-swapped, and
    manager.zip holds the two halves of the Salora Manager: read from the
    top level alone, both stayed MISSING."""

    def test_members_derive_and_concatenate(self):
        import zipfile

        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        chip, low, high = os.urandom(64), os.urandom(32), os.urandom(32)
        archive = root / "set.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("chip.rom", chip)
            zf.writestr("01", low)
            zf.writestr("23", high)
        db = {"files": {"a" * 40: {"path": str(archive), "size": archive.stat().st_size}}}
        swapped = derivations._swap16(chip)
        found = derivations.derivable(
            [_wanted(swapped), {**_wanted(low + high), "crc32": f"{zlib.crc32(low + high):08x}"}],
            db,
        )
        self.assertEqual(found[hashlib.sha1(swapped).hexdigest()]["transform"], "byte-swapped")
        self.assertEqual(found[hashlib.sha1(low + high).hexdigest()]["transform"], "concatenated")

    def test_the_word_stride_and_crc_combine_are_exact(self):
        data = os.urandom(4096)
        slow = b"".join(data[i:i + 2] for i in range(0, len(data), 4))
        self.assertEqual(derivations._words(data, 0), slow)
        for size in (1, 4096, 123457):
            a, b = os.urandom(99), os.urandom(size)
            self.assertEqual(
                derivations.crc32_combine(zlib.crc32(a), zlib.crc32(b), size),
                zlib.crc32(a + b),
            )



class CombineMatchesZlib(unittest.TestCase):
    """The operator is composed once per length; it must stay zlib's."""

    def test_against_zlib(self):
        import os
        import zlib

        from derivations import crc32_combine

        for size in (1, 2, 3, 7, 100, 4096, 65537, 1000003):
            with self.subTest(size=size):
                a, b = os.urandom(37), os.urandom(size)
                self.assertEqual(
                    crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)), zlib.crc32(a + b)
                )


if __name__ == "__main__":
    unittest.main()
