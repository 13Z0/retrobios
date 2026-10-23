"""One folder per console.

A system section is named after the folder its files live in. Nintendo held
DS and Nintendo DS, 64DD and Nintendo 64DD, Sega held Mega CD and Mega-CD,
Sinclair ZX Spectrum and ZXSpectrum, VTech V.Smile and VSmile, Acorn BBC and
BBC Micro: each console had two sections that hid each other on the site.
Reads the git index, which is what a clone checks out.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def console_key(name: str) -> str:
    return re.sub(r"[\s.\-]", "", name).casefold()


def twin_consoles(paths: list[str]) -> list[list[str]]:
    """Console folders of one maker that read as the same name."""
    spellings: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in paths:
        parts = path.split("/")
        if len(parts) < 4 or parts[0] != "bios":
            continue
        maker, console = parts[1], parts[2]
        spellings[(maker, console_key(console))].add(f"{maker}/{console}")
    return sorted(sorted(names) for names in spellings.values() if len(names) > 1)


class OneFolderPerConsole(unittest.TestCase):
    def test_no_maker_files_one_console_twice(self) -> None:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "bios"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if listed.returncode != 0 or not listed.stdout:
            self.skipTest("not a git checkout with bios/")
        paths = [p for p in listed.stdout.split("\0") if p]
        self.assertEqual(twin_consoles(paths), [])

    def test_spaces_dashes_and_dots_do_not_make_a_new_console(self) -> None:
        self.assertEqual(
            twin_consoles([
                "bios/Sega/Mega CD/a.bin",
                "bios/Sega/Mega-CD/b.bin",
                "bios/VTech/V.Smile/c.bin",
                "bios/VTech/VSmile/d.bin",
                "bios/Atari/Lynx/e.bin",
                "bios/Camputers/Lynx/f.bin",
            ]),
            [["Sega/Mega CD", "Sega/Mega-CD"], ["VTech/V.Smile", "VTech/VSmile"]],
        )


if __name__ == "__main__":
    unittest.main()
