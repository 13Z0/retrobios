"""Exporter for libretro System.dat (clrmamepro DAT format).

One 'game' block, systems separated by a comment line carrying the name
libretro gives them, matching libretro-database/dat/System.dat.
"""

from __future__ import annotations

import re

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.dat_parser import parse_dat

from .base_exporter import BaseExporter
from .baseline import NativeSystem, Report

SOURCE_URL = (
    "https://raw.githubusercontent.com/libretro/libretro-database/master/dat/System.dat"
)


_QUOTED_NAME = re.compile(r'rom \( name "([^"]+)"')


def _quote(name: str, quoted: frozenset[str] = frozenset()) -> str:
    """Quote a ROM name the way the original does.

    Where it must be, and where the maintainers chose to: the file quotes
    246 names it did not need to, and writing them bare put a quoting change
    on every one of those lines of the diff.
    """
    return f'"{name}"' if name in quoted or any(c in name for c in ' ()') else name


_ROM_NAME = re.compile(r'^\trom \( name ("[^"]+"|\S+)')


def _sections(original: str) -> list[tuple[str, list[str]]]:
    """The system sections of a System.dat, each comment with its rom lines."""
    _, sep, body = original.partition("\ngame (")
    if not sep:
        return []
    sections: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith('\tcomment "') and line != '\tcomment "System"':
            sections.append((line[len('\tcomment "'):].rstrip('"'), []))
        elif line.startswith("\trom (") and sections:
            sections[-1][1].append(line)
    return sections


class Exporter(BaseExporter):
    """Write libretro's System.dat, corrected."""

    @staticmethod
    def platform_name() -> str:
        return "retroarch"

    @staticmethod
    def native_filename() -> str:
        return "System.dat"

    @staticmethod
    def carries() -> frozenset[str]:
        return frozenset({"size", "crc32", "md5", "sha1"})

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"System.dat": SOURCE_URL}

    @classmethod
    def writable(cls, fe, require: str = "") -> bool:
        """A clrmamepro rom line is a hash record, and the DAT has no other.

        The original never states a rom it cannot hash, so neither do we.
        """
        if fe.platform is not None:
            return True
        return any(fe.hash(h) for h in ("crc32", "md5", "sha1"))

    @staticmethod
    def _rom_name(fe) -> str:
        """The name the DAT gives a rom.

        libretro writes the path for some entries (ep128emu/roms/cpc464.rom)
        and the bare name for others whose destination has a directory all
        the same (iplromco.dat, which lives under keropi/), so its own
        spelling is recorded rather than derived.
        """
        declared = fe.native("native_path", "")
        return str(declared) if declared else fe.name

    def _header(self, originals: dict[str, str], scraped: dict | None) -> list[str]:
        """Reuse the original header verbatim when we have the original."""
        original = originals.get(self.native_filename(), "")
        if original:
            head, sep, _ = original.partition("\ngame (")
            if sep:
                return head.split("\n")

        version = ""
        if scraped:
            version = scraped.get("dat_version", scraped.get("version", ""))
        lines = [
            "clrmamepro (",
            '\tname "System"',
            '\tdescription "System"',
            '\tcomment "System, firmware, and BIOS files used by libretro cores."',
        ]
        if version:
            lines.append(f"\tversion {version}")
        lines.extend(
            [
                '\tauthor "libretro"',
                '\thomepage "https://github.com/libretro/libretro-database/blob/master'
                '/dat/System.dat"',
                '\turl "https://raw.githubusercontent.com/libretro/libretro-database'
                '/master/dat/System.dat"',
                ")",
                "",
            ]
        )
        return lines

    def render(
        self,
        systems: dict[str, NativeSystem],
        report: Report,
        originals: dict[str, str],
        scraped: dict | None = None,
    ) -> dict[str, str]:
        lines = self._header(originals, scraped)
        lines.extend(["game (", '\tname "System"', '\tcomment "System"'])
        original = originals.get(self.native_filename(), "")
        quoted = frozenset(_QUOTED_NAME.findall(original))

        ours: dict[str, dict[str, str]] = {}
        for system, files in self.exportable(systems):
            rendered: dict[str, str] = {}
            for fe in files:
                if not any(fe.hash(h) for h in ("crc32", "md5", "sha1")):
                    continue
                name = _quote(self._rom_name(fe), quoted)
                parts = [f"name {name}"]
                size = fe.size()
                if size:
                    parts.append(f"size {size}")
                crc = fe.hash("crc32")
                if crc:
                    parts.append(f"crc {crc.upper()}")
                md5 = fe.hash("md5")
                if md5:
                    parts.append(f"md5 {md5}")
                sha1 = fe.hash("sha1")
                if sha1:
                    parts.append(f"sha1 {sha1}")
                rendered.setdefault(name, f"\trom ( {' '.join(parts)} )")
            if rendered:
                ours[system.native_id] = rendered

        def section(comment: str, roms: list[str]) -> None:
            lines.append("")
            # libretro's comment is the system name as the DAT spells it,
            # "Atari - 400-800". Prettifying it drops the separator.
            lines.append(f'\tcomment "{comment}"')
            lines.extend(roms)

        # libretro's own sections are the base: a rom line our transcription
        # does not carry (added or renamed after a scrape) stays as written.
        for comment, roms in _sections(original):
            rendered = ours.pop(comment, {})
            body = []
            for line in roms:
                match = _ROM_NAME.match(line)
                body.append(rendered.pop(match.group(1), line) if match else line)
            body.extend(rendered.values())
            section(comment, body)
        for comment in sorted(ours):
            section(comment, list(ours[comment].values()))

        lines.append(")")
        lines.append("")
        return {self.native_filename(): "\n".join(lines)}

    def validate(
        self,
        systems: dict[str, NativeSystem],
        produced: dict[str, str],
    ) -> list[str]:
        content = produced[self.native_filename()]
        parsed = parse_dat(content)
        exported = {rom.name for rom in parsed}

        issues: list[str] = []
        for system in systems.values():
            for fe in system.files:
                if not any(fe.hash(h) for h in ("crc32", "md5", "sha1")):
                    continue
                if self._rom_name(fe) not in exported:
                    issues.append(f"absent from the DAT: {system.native_id}/{fe.name}")
        if not content.rstrip().endswith(")"):
            issues.append("the game block is not closed")
        return issues
