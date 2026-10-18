from __future__ import annotations

import os
import unittest
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BIOS_ROOT = REPO_ROOT / "bios"


class TestNoCaseCollisions(unittest.TestCase):
    """Guard against case-colliding paths in bios/.

    On case-insensitive filesystems (Windows, macOS default), git can only
    check out one path per casefold-equivalence class, silently corrupting
    clones. Issue #33 and #49 both stemmed from this.

    .variants/ subdirs are exempt: they intentionally hold genuine content
    variants disambiguated by hash suffix (e.g., BIOS.ROM.910fae67).
    """

    def test_bios_has_no_case_colliding_paths(self) -> None:
        if not BIOS_ROOT.is_dir():
            self.skipTest("bios/ directory not present")

        collisions: list[str] = []
        for root, dirs, files in os.walk(BIOS_ROOT):
            if ".variants" in Path(root).parts:
                continue

            dir_groups: dict[str, list[str]] = defaultdict(list)
            for d in dirs:
                dir_groups[d.casefold()].append(d)
            for variants in dir_groups.values():
                if len(variants) > 1:
                    rel = Path(root).relative_to(REPO_ROOT)
                    collisions.append(f"DIR  {rel}: {sorted(variants)}")

            file_groups: dict[str, list[str]] = defaultdict(list)
            for f in files:
                file_groups[f.casefold()].append(f)
            for variants in file_groups.values():
                if len(variants) > 1:
                    rel = Path(root).relative_to(REPO_ROOT)
                    collisions.append(f"FILE {rel}: {sorted(variants)}")

        self.assertEqual(
            collisions,
            [],
            "Case-colliding paths in bios/ would break Windows/macOS clones:\n"
            + "\n".join(collisions),
        )


class SharedGroupsAddNoSecondSpelling(unittest.TestCase):
    """The np2kai group added np2kai/font.rom beside System.dat's
    np2kai/FONT.ROM. NP2kai reads font.rom only when FONT.ROM is absent: an
    alternative name, which belongs in the profile's aliases. On Lakka and
    RetroPie, whose filesystems keep both, the pack carried the font twice
    and verify counted one more required file."""

    def test_no_group_entry_respells_a_declared_destination(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from common import (
            list_registered_platforms,
            load_emulator_profiles,
            load_platform_config,
        )

        # A spelling some code opens as its own file stands: NP2kai's rhythm
        # engine reads 2608_bd.wav, its fmgen engine 2608_BD.WAV, and a
        # case-sensitive filesystem needs both. A spelling that is only ever
        # an alias is the same file asked for twice.
        read_as_named = {
            str(entry.get("path") or entry.get("name") or "")
            for profile in load_emulator_profiles(str(REPO_ROOT / "emulators")).values()
            for entry in profile.get("files", [])
            if isinstance(entry, dict)
        }
        platforms_dir = str(REPO_ROOT / "platforms")
        respelled: list[str] = []
        for name in list_registered_platforms(platforms_dir, include_archived=True):
            config = load_platform_config(name, platforms_dir)
            own: dict[str, str] = {}
            grouped: list[tuple[str, str]] = []
            for system in config.get("systems", {}).values():
                for entry in system.get("files", []):
                    dest = entry.get("destination") or entry.get("name") or ""
                    if entry.get("shared_group"):
                        grouped.append((dest, entry["shared_group"]))
                    else:
                        own.setdefault(dest.casefold(), dest)
            for dest, group in grouped:
                spelled = own.get(dest.casefold())
                named = dest in read_as_named
                if spelled and spelled != dest and not named:
                    respelled.append(f"{name}: {group} adds {dest} beside {spelled}")
        self.assertEqual(respelled, [])


if __name__ == "__main__":
    unittest.main()
