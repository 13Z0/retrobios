"""Catalog entries whose bytes a held dump already contains.

A catalog lists chips as they come off the board; a collection often holds
the image an emulator reads. The Amiga 3.1 Kickstart pair 391773/391774 is
the high and low 16-bit words of amiga-os-310-a1200.rom, byte-swapped; the
TOSEC "Basic V7" is basiclo and basichi end to end. Listed as missing, they
send a maintainer after files that are already here. Each transform below is
one the source boards use; a match is by sha1, never by size or name.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable, Iterable

# A held file larger than this is not read: no chip image is near it.
MAX_SOURCE_SIZE = 64 * 1024 * 1024


def _swap16(data: bytes) -> bytes:
    swapped = bytearray(data)
    swapped[0::2], swapped[1::2] = data[1::2], data[0::2]
    return bytes(swapped)


def _words(data: bytes, start: int) -> bytes:
    return b"".join(data[i:i + 2] for i in range(start * 2, len(data), 4))


# (name, size of the output for an input of size n or None, transform)
TRANSFORMS: tuple[tuple[str, Callable[[int], int | None], Callable[[bytes], bytes]], ...] = (
    ("first half", lambda n: n // 2 if n % 2 == 0 else None, lambda d: d[:len(d) // 2]),
    ("second half", lambda n: n // 2 if n % 2 == 0 else None, lambda d: d[len(d) // 2:]),
    *(
        (f"quarter {q + 1}", lambda n: n // 4 if n % 4 == 0 else None,
         lambda d, q=q: d[q * len(d) // 4:(q + 1) * len(d) // 4])
        for q in range(4)
    ),
    ("even bytes", lambda n: n // 2 if n % 2 == 0 else None, lambda d: d[0::2]),
    ("odd bytes", lambda n: n // 2 if n % 2 == 0 else None, lambda d: d[1::2]),
    ("even words", lambda n: n // 2 if n % 4 == 0 else None, lambda d: _words(d, 0)),
    ("odd words", lambda n: n // 2 if n % 4 == 0 else None, lambda d: _words(d, 1)),
    ("even words, byte-swapped", lambda n: n // 2 if n % 4 == 0 else None,
     lambda d: _swap16(_words(d, 0))),
    ("odd words, byte-swapped", lambda n: n // 2 if n % 4 == 0 else None,
     lambda d: _swap16(_words(d, 1))),
    ("byte-swapped", lambda n: n if n % 2 == 0 else None, _swap16),
    ("mirrored twice", lambda n: n * 2, lambda d: d + d),
)


def _gf2_times(matrix: list[int], vector: int) -> int:
    total, row = 0, 0
    while vector:
        if vector & 1:
            total ^= matrix[row]
        vector >>= 1
        row += 1
    return total


def _gf2_square(matrix: list[int]) -> list[int]:
    return [_gf2_times(matrix, matrix[n]) for n in range(32)]


def crc32_combine(crc1: int, crc2: int, len2: int) -> int:
    """crc32 of A + B from crc32(A), crc32(B) and len(B), as zlib computes it."""
    if len2 <= 0:
        return crc1
    odd = [0xEDB88320] + [1 << (n - 1) for n in range(1, 32)]
    even = _gf2_square(odd)
    odd = _gf2_square(even)
    while True:
        even = _gf2_square(odd)
        if len2 & 1:
            crc1 = _gf2_times(even, crc1)
        len2 >>= 1
        if not len2:
            break
        odd = _gf2_square(even)
        if len2 & 1:
            crc1 = _gf2_times(odd, crc1)
        len2 >>= 1
        if not len2:
            break
    return crc1 ^ crc2


def _read(path: str) -> bytes | None:
    try:
        return open(path, "rb").read()
    except OSError:
        return None


def derivable(entries: Iterable[dict], db: dict) -> dict[str, dict]:
    """Missing sha1 to the held file and transform that produce its bytes.

    Only held files whose size can produce a wanted size are read, and a
    concatenation is only tried between files of one directory: the chip
    pairs a catalog splits come from one machine.
    """
    wanted: dict[tuple[int, str], str] = {}
    by_size_crc: dict[tuple[int, int], list[str]] = defaultdict(list)
    for entry in entries:
        sha1, size = (entry.get("sha1") or "").lower(), entry.get("size")
        if sha1 and isinstance(size, int) and size > 0:
            wanted[(size, sha1)] = sha1
            crc = entry.get("crc32")
            if crc:
                by_size_crc[(size, int(str(crc), 16))].append(sha1)
    sizes = {size for size, _sha1 in wanted}
    found: dict[str, dict] = {}

    files = [f for f in db.get("files", {}).values()
             if 0 < f.get("size", 0) <= MAX_SOURCE_SIZE and os.path.exists(f["path"])]
    for held in files:
        n = held["size"]
        useful = [t for t in TRANSFORMS if t[1](n) in sizes]
        if not useful:
            continue
        data = _read(held["path"])
        if data is None:
            continue
        for name, out_size, transform in useful:
            out = transform(data)
            sha1 = wanted.get((out_size(n), hashlib.sha1(out).hexdigest()))
            if sha1 and sha1 not in found:
                found[sha1] = {"from": [held["path"]], "transform": name}

    by_dir: dict[str, list[dict]] = defaultdict(list)
    for held in files:
        if held.get("crc32"):
            by_dir[os.path.dirname(held["path"])].append(held)
    for group in by_dir.values():
        for first in group:
            for second in group:
                if first is second:
                    continue
                total = first["size"] + second["size"]
                if total not in sizes:
                    continue
                crc = crc32_combine(int(first["crc32"], 16), int(second["crc32"], 16),
                                    second["size"])
                candidates = [s for s in by_size_crc.get((total, crc), []) if s not in found]
                if not candidates:
                    continue
                a, b = _read(first["path"]), _read(second["path"])
                if a is None or b is None:
                    continue
                sha1 = hashlib.sha1(a + b).hexdigest()
                if sha1 in candidates:
                    found[sha1] = {"from": [first["path"], second["path"]],
                                   "transform": "concatenated"}
    return found
