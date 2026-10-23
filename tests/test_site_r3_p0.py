"""Every system id with a section leads to it (round 3, package P0).

260 of 636 system ids linked: a variant (WonderSwan Color, Neo Geo MVS)
never matched a heading, twin folders (DS and Nintendo DS) made the exact
name ambiguous, and every Arcade heading was refused. siterender now reads
a reviewed SYSTEM_PARENTS table after the id's own section, and accepts an
Arcade heading that reads the name exactly. An id whose files the
collection holds and that stays plain is named below, with the reason.
Reads the repository: skips when the database is absent.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import siterender  # noqa: E402

# Ids whose files the collection holds and that lead to no section.
UNLINKED: dict[str, str] = {
    # Files only under a program's folder (Other/<emulator>, Arcade/<core>,
    # Game Engines/<engine>, a top named after the program). The heading
    # names the program, not the system.
    "atari-system1": "Other > fbneo",
    "bbc-master-128": "Other > beebem",
    "bbc-master-et": "Other > beebem",
    "capcom-cps3": "Arcade > FBNeo",
    "cave-story": "Other > NXEngine",
    "cps3": "Arcade > Arcade holds one CPS-3 set among every board's",
    "commanderx16": "Arcade > Arcade",
    "dark-forces-ii": "Game Engines > OpenJKDF2",
    "deco-cassette": "Arcade > FBNeo",
    "dos": "DOSBox, Roland and Other folders",
    "fpinball": "Other > future-pinball-fploader",
    "fruitmachine": "Other > mfme",
    "igs-pgm": "Arcade > FBNeo",
    "konami-bubsys": "Arcade > FBNeo",
    "microsoft-xbox-360": "Other > xenia-edge",
    "midway-mcr": "Arcade > FBNeo",
    "mysteries-of-the-sith": "Game Engines > OpenJKDF2",
    "mz2000": "Arcade > Arcade, not the Sharp MZ section",
    "mz2500": "Arcade > Arcade, not the Sharp MZ section",
    "mz800": "Arcade > Arcade, not the Sharp MZ section",
    "namco-na1": "Arcade > FBNeo",
    "namco-nb1": "Arcade > FBNeo",
    "nmk16": "Arcade > FBNeo",
    "omicron": "Other > omicron",
    "pegasus": "Arcade > MAME",
    "rpgmaker": "RPG Maker > easyrpg",
    "scummvm": "Other > ScummVM",
    "sdlpal": "Other > sdlpal",
    "sega-system16b": "Arcade > FBNeo",
    "sinclair-zx-spectrum-next": "Other > cspect",
    "smbx": "Game Engines > TheXTech",
    "solarus": "Game Engines > Solarus",
    "sunplus-spmp8000": "Other > spmp8000emu",
    "super-kaneko-nova": "Arcade > FBNeo",
    "taito-cchip": "Arcade > FBNeo",
    "thomson-mo6": "Other > Clock Signal",
    "tomb-raider-1": "Game Engines > TRX",
    "vgmplay": "Arcade > Arcade and MAME",
    "vm-labs-nuon": "Other > nuance",
    "vpinball": "Arcade > PinMAME",
    "xrick": "Other > xrick",
    "zc210": "ZC > ZC210, the emulator's folder",
    # Files filed with another machine: a link would name the wrong one.
    "atom": "atom.zip is filed with the BBC Micro",
    "commodore-c64-supercpu": "VICE's tree under the C128",
    "emerson-arcadia-2001": "ar_bios.zip is filed with the Saturn",
    "naomi2": "naomi2.zip is filed with the Dreamcast",
    "neogeo64": "the platform declares the Neo Geo's neogeo.zip",
    "sega-dreamcast-arcade": "naomi.zip and awbios.zip, Arcade and Dreamcast",
    "spectravideo": "the SVI ROMs are filed with the MSX",
    "vvvvvv": "SDL's controller database",
    "ym2608": "an FBNeo sound ROM, also filed with the NES",
    # Files of distinct machines or games, one section each.
    "catacomb-adventure": "three games, three sections",
    "sinclair-zx80-81": "ZX 80 and ZX 81 sections",
}


def _accepts(fe: dict, local: str | None, status: str) -> bool:
    """A resolution that proves the entry's content or its full path."""
    from common import resolution_is_hash_exact

    if not local:
        return False
    if resolution_is_hash_exact(status) or status == "path_exact":
        return True
    path = fe.get("path")
    return bool(path) and local.endswith("/" + str(path))


@unittest.skipUnless((REPO_ROOT / "database.json").exists(), "database.json is not built")
class SystemLinks(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        previous = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            from common import (
                list_registered_platforms,
                load_emulator_profiles,
                load_platform_config,
                resolve_local_file,
            )
            from generate_site import _copy_places, _group_by_manufacturer

            with open("database.json", encoding="utf-8") as handle:
                db = json.load(handle)
            cls.manufacturers = _group_by_manufacturer(db)
            profiles = load_emulator_profiles("emulators", skip_aliases=False)
            siterender.set_console_sections(
                cls.manufacturers, siterender.program_names(profiles))
            platforms = [
                load_platform_config(name, "platforms")
                for name in list_registered_platforms("platforms", include_archived=True)
            ]
            cls.ids = siterender.system_ids(profiles, platforms)

            entries: dict[str, list[dict]] = {}
            for config in platforms:
                for sys_id, system in config.get("systems", {}).items():
                    entries.setdefault(sys_id, []).extend(system.get("files") or [])
            for profile in profiles.values():
                systems = [str(s) for s in profile.get("systems") or []]
                for fe in profile.get("files") or []:
                    if not isinstance(fe, dict):
                        continue
                    owners = {str(fe["system"])} if fe.get("system") else set()
                    owners.update(str(s) for s in fe.get("systems") or [])
                    if not owners and len(systems) == 1:
                        owners = set(systems)
                    for owner in owners:
                        entries.setdefault(owner, []).append(fe)

            places = _copy_places(db)
            by_path = {entry.get("path"): sha1 for sha1, entry in db["files"].items()}
            # Id -> the (manufacturer, console) sections holding its files.
            cls.held: dict[str, set[tuple[str, str]]] = {}
            for sys_id in cls.ids:
                sections: set[tuple[str, str]] = set()
                for fe in entries.get(sys_id, []):
                    local, status = resolve_local_file(fe, db)
                    if not _accepts(fe, local, status):
                        continue
                    sha1 = by_path.get(local)
                    if sha1 is not None:
                        sections.update((m, c) for m, c, _ in places.get(sha1, set()))
                if sections:
                    cls.held[sys_id] = sections
        finally:
            os.chdir(previous)

    def test_every_id_with_files_links(self) -> None:
        linked, total = siterender.link_coverage(self.ids)
        with_files = set(self.held)
        print(f"\n  {linked} of {total} system ids linked, "
              f"{len(with_files - set(UNLINKED))} of {len(with_files)} with files")
        plain = sorted(
            sys_id for sys_id in with_files - set(UNLINKED)
            if siterender.system_section(sys_id) is None
        )
        self.assertEqual(plain, [], "link these or name them in UNLINKED with the reason")

    def test_unlinked_list_is_current(self) -> None:
        stale = sorted(
            sys_id for sys_id in UNLINKED
            if sys_id not in self.held or siterender.system_section(sys_id) is not None
        )
        self.assertEqual(stale, [])

    def test_a_matching_heading_is_linked(self) -> None:
        """An id whose name reads one heading, outside Other, leads there."""
        by_heading: dict[str, set[tuple[str, str]]] = {}
        for mfr, consoles in self.manufacturers.items():
            for console in consoles:
                for name in {siterender._heading_name(mfr, console), console}:
                    by_heading.setdefault(name.casefold(), set()).add((mfr, console))
        missed = []
        for sys_id in sorted(self.ids):
            places = by_heading.get(siterender.system_name(sys_id).casefold(), set())
            if len(places) != 1:
                continue
            mfr, console = next(iter(places))
            if mfr == "Other" or (
                mfr == "Arcade" and console.casefold() in siterender._program_names
            ):
                continue
            if siterender.system_section(sys_id) is None:
                missed.append(sys_id)
        self.assertEqual(missed, [])

    def test_parents_are_sections(self) -> None:
        sections = {
            (mfr, console)
            for mfr, consoles in self.manufacturers.items() for console in consoles
        }
        self.assertEqual(
            sorted(k for k, v in siterender.SYSTEM_PARENTS.items() if v not in sections), [])

    def test_parents_hold_the_files(self) -> None:
        """A parent holds a file of the id, or of an id of the same name.
        An entry its own section shadows is never read and is not judged."""
        by_name: dict[str, set[tuple[str, str]]] = {}
        for sys_id, sections in self.held.items():
            by_name.setdefault(siterender.system_name(sys_id), set()).update(sections)
        wrong = []
        for sys_id, parent in sorted(siterender.SYSTEM_PARENTS.items()):
            if siterender._own_section(sys_id) is not None:
                continue
            held = by_name.get(siterender.system_name(sys_id))
            if held is not None and parent not in held:
                wrong.append((sys_id, parent))
        self.assertEqual(wrong, [])

    def test_variants_lead_to_their_family(self) -> None:
        expect = {
            "wonderswan-color": ("bandai", "WonderSwan"),
            "bandai-wswanc": ("bandai", "WonderSwan"),
            "ngpc": ("snk", "Neo Geo Pocket"),
            "snk-neogeo-mvs": ("snk", "Neo Geo"),
            "snk-neogeo-aes": ("snk", "Neo Geo"),
            "nintendo-ds": ("nintendo", "Nintendo DS"),
            "nintendo-64dd": ("nintendo", "Nintendo 64DD"),
            "zx-spectrum-128": ("sinclair", "ZX Spectrum"),
            "pc-engine-ld": ("pioneer", "LaserActive"),
        }
        for sys_id, section in expect.items():
            with self.subTest(sys_id=sys_id):
                self.assertEqual(siterender.system_section(sys_id), section)

    def test_arcade_heading_by_exact_name(self) -> None:
        self.assertEqual(
            siterender.system_section("namco-system246"), ("arcade", "Namco System 246"))
        # No heading reads NAOMI: its sets are cards of the general section.
        self.assertIsNone(siterender.system_section("sega-naomi"))
        # Never a folder of copies a core loads.
        self.assertIsNone(siterender.system_section("igs-pgm"))

    def test_folder_votes_stay_out(self) -> None:
        for sys_id in ("emerson-arcadia-2001", "naomi2"):
            with self.subTest(sys_id=sys_id):
                self.assertIsNone(siterender.system_section(sys_id))
                self.assertNotIn(sys_id, siterender.SYSTEM_PARENTS)

    def test_emulator_rows_link_the_variant(self) -> None:
        from generate_site import _named_system_link

        self.assertEqual(
            _named_system_link("wonderswan-color", "../"),
            "[Bandai WonderSwan Color](../systems/bandai.md#wonderswan)")


class SearchAliases(unittest.TestCase):

    def test_platform_names(self) -> None:
        self.assertIn("psx", siterender.system_search_aliases("sony-playstation"))
        self.assertIn("dc", siterender.system_search_aliases("sega-dreamcast"))
        self.assertIn("gba", siterender.system_search_aliases("nintendo-gba"))

    def test_one_typed_word_per_name(self) -> None:
        siterender.system_search_aliases("sony-playstation")
        for sys_id, names in siterender._search_aliases.items():
            with self.subTest(sys_id=sys_id):
                self.assertNotIn(sys_id, names)
                self.assertEqual(len({n.casefold() for n in names}), len(names))
                for name in names:
                    self.assertNotRegex(name, r"[\s,]")

    def test_unknown_id(self) -> None:
        self.assertEqual(siterender.system_search_aliases("no-such-system"), [])


class BuildCount(unittest.TestCase):

    def test_generator_prints_the_count(self) -> None:
        source = (REPO_ROOT / "scripts" / "generate_site.py").read_text(encoding="utf-8")
        self.assertIn("link_coverage(", source)
        self.assertIn("system ids linked", source)


if __name__ == "__main__":
    unittest.main()
