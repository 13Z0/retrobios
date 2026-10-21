"""Device ROM sets read from a MAME -listxml and merged into a profile.

Synthetic listxml and driver source, no network: the scan keeps only device
sets with dumped ROMs, the source line is the ROM_START block holding them,
and the merge adds sets, refreshes the contents of written ones without
touching what a reader wrote, and leaves BIOS root sets alone.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.common import yaml_load
from scripts.scraper._hash_merge import (
    DEVICE_SECTION,
    mame_ref_role,
    merge_mame_devices,
    merge_mame_profile,
)
from scripts.scraper.mame_devices import (
    ABSENT,
    AMBIGUOUS,
    NO_FILE,
    find_rom_block,
    locate_sources,
    open_listxml,
    scan_device_sets,
    source_path,
)

LISTXML = b"""<?xml version="1.0"?>
<!DOCTYPE mame [
<!ELEMENT mame (machine+)>
]>
<mame build="0.289 (mame0289)" debug="no" mameconfig="10">
\t<machine name="neogeo" sourcefile="snk/neogeo.cpp" isbios="yes">
\t\t<description>Neo-Geo MV-6F</description>
\t\t<rom name="sp-s2.sp1" size="131072" crc="9036d879" sha1="4f5ed7105b7128794654ce82b51723e16e389543" region="mainbios" offset="0"/>
\t</machine>
\t<machine name="qsound" sourcefile="devices/sound/qsound.cpp" isdevice="yes" runnable="no">
\t\t<description>QSound</description>
\t\t<rom name="dl-1425.bin" size="24576" crc="d6cf5ef5" sha1="555f50fe5cdf127619da7d854c03f4a244a0c501" region="dsp" offset="0"/>
\t</machine>
\t<machine name="kbd_new" sourcefile="devices/kbd.cpp" isdevice="yes" runnable="no">
\t\t<description>Keyboard &amp; Mouse - New</description>
\t\t<biosset name="v2" description="Version 2"/>
\t\t<rom name="6570-036" bios="v2" size="2048" crc="4a3fc332" sha1="83b21d0c8b93fc9b9b3b287fde4ec8f3badac5a2" region="mcu" offset="0"/>
\t\t<rom name="bad.bin" size="16" crc="00000011" sha1="0000000000000000000000000000000000000011" status="baddump" region="mcu" offset="0"/>
\t</machine>
\t<machine name="nodump_dev" sourcefile="devices/kbd.cpp" isdevice="yes" runnable="no">
\t\t<description>Undumped</description>
\t\t<rom name="missing.bin" size="1024" status="nodump" region="mcu" offset="0"/>
\t</machine>
\t<machine name="empty_dev" sourcefile="devices/kbd.cpp" isdevice="yes" runnable="no">
\t\t<description>No ROM</description>
\t</machine>
</mame>
"""

KBD_SOURCE = """// keyboards
ROM_START(kbd_old)
\tROM_REGION(0x0800, "mcu", 0)
\tROM_LOAD("6500-1", 0x0000, 0x0800, CRC(4a3fc332) SHA1(83b21d0c8b93fc9b9b3b287fde4ec8f3badac5a2) BAD_DUMP)
\tROM_LOAD("bad.bin", 0x0800, 0x0010, CRC(00000011) SHA1(0000000000000000000000000000000000000011))
ROM_END

ROM_START(kbd_new)
\tROM_REGION(0x0800, "mcu", 0)
\tROM_LOAD("6570-036", 0x0000, 0x0800, CRC(4a3fc332) SHA1(83b21d0c8b93fc9b9b3b287fde4ec8f3badac5a2))
\tROM_LOAD("bad.bin", 0x0800, 0x0010, CRC(00000011) SHA1(0000000000000000000000000000000000000011))
ROM_END
""".splitlines()

QSOUND_SOURCE = """#define QSOUND_ROMS \\
\tROM_LOAD16_WORD_SWAP("dl-1425.bin", 0x0000, 0x6000, CRC(d6cf5ef5) SHA1(555f50fe5cdf127619da7d854c03f4a244a0c501))

// ROM_START( commented ) CRC(d6cf5ef5)
ROM_START( qsound )
\tROM_REGION16_BE( 0x2000, "dsp", 0 )
\tQSOUND_ROMS
ROM_END
""".splitlines()

PROFILE = """emulator: MAME
source: "https://github.com/libretro/mame"
upstream: "https://github.com/mamedev/mame"
upstream_commit: "f34f02505e32c1993c6a782b6814232cbfc74e36"
core_version: "0.289"
files:
  # SNK Neo Geo
  - name: neogeo.zip
    required: true
    category: bios_zip
    system: snk-neogeo-mvs
    source_ref: "upstream src/mame/snk/neogeo.cpp:2400"
    contents:
      - name: "sp-s2.sp1"
        description: "Europe MVS (Ver. 2)"
        size: 131072
        crc32: "oldcrc32"

  - name: qsound.zip
    required: true
    category: bios_zip
    system: capcom-cps2
    source_ref: "upstream src/devices/sound/qsound.cpp:121-125"
    note: >-
      Written by hand.
    contents:
      - name: "dl-1425.bin"
        description: "QSound DSP microcode"
        size: 24576
        crc32: "d6cf5ef5"
"""


def _devices() -> dict[str, dict]:
    tag, devices = scan_device_sets(io.BytesIO(LISTXML))
    assert tag == "mame0289"
    return devices


def _fetch(path: str) -> list[str] | None:
    return {
        "src/devices/kbd.cpp": KBD_SOURCE,
        "src/devices/sound/qsound.cpp": QSOUND_SOURCE,
    }.get(path)


class ScanKeepsDeviceSetsWithDumpedRoms(unittest.TestCase):

    def test_only_devices_with_a_crc_are_kept(self):
        devices = _devices()
        self.assertEqual(sorted(devices), ["kbd_new", "qsound"])

    def test_rom_fields_bios_description_and_bad_dump(self):
        kbd = _devices()["kbd_new"]
        self.assertEqual(kbd["description"], "Keyboard & Mouse - New")
        self.assertEqual(kbd["sourcefile"], "devices/kbd.cpp")
        first, second = kbd["roms"]
        self.assertEqual(
            (first["name"], first["size"], first["crc32"], first["bios_description"]),
            ("6570-036", 2048, "4a3fc332", "Version 2"),
        )
        self.assertTrue(second["bad_dump"])

    def test_a_document_without_a_build_is_refused(self):
        with self.assertRaises(ValueError):
            scan_device_sets(io.BytesIO(b"<mame><machine name='x'></machine></mame>"))

    def test_the_release_zip_is_read(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "mame0289lx.zip"
            with zipfile.ZipFile(pack, "w") as archive:
                archive.writestr("mame0289.xml", LISTXML)
            with open_listxml(pack) as stream:
                tag, devices = scan_device_sets(stream)
        self.assertEqual((tag, sorted(devices)), ("mame0289", ["kbd_new", "qsound"]))

    def test_source_paths(self):
        self.assertEqual(source_path("devices/sound/qsound.cpp"), "src/devices/sound/qsound.cpp")
        self.assertEqual(source_path("snk/neogeo.cpp"), "src/mame/snk/neogeo.cpp")


class SourceLinesAreTheBlockHoldingTheRoms(unittest.TestCase):

    def test_found_through_a_rom_macro_past_a_comment(self):
        self.assertEqual(
            find_rom_block(QSOUND_SOURCE, "qsound", [("dl-1425.bin", "d6cf5ef5")]),
            ("5-8", ""),
        )

    def test_a_block_inside_a_comment_is_not_a_block(self):
        source = [
            "/* ROM_START( old )",
            '\tROM_LOAD("dl-1425.bin", 0, 0x6000, CRC(d6cf5ef5) SHA1(555f50fe5cdf127619da7d854c03f4a244a0c501))',
            "ROM_END */",
            "ROM_START( current )",
            '\tROM_LOAD("dl-1425.bin", 0, 0x6000, CRC(d6cf5ef5) SHA1(555f50fe5cdf127619da7d854c03f4a244a0c501))',
            "ROM_END",
        ]
        self.assertEqual(
            find_rom_block(source, "dsp", [("dl-1425.bin", "d6cf5ef5")]), ("4-6", "")
        )

    def test_same_dump_under_two_names_is_told_apart_by_name(self):
        line, _ = find_rom_block(
            KBD_SOURCE, "kbd_new", [("6570-036", "4a3fc332"), ("bad.bin", "00000011")]
        )
        self.assertEqual(line, "8-12")

    def test_not_found(self):
        self.assertEqual(
            find_rom_block(KBD_SOURCE, "x", [("x.bin", "deadbeef")]), (None, ABSENT)
        )

    def test_two_blocks_and_no_way_to_choose(self):
        self.assertEqual(
            find_rom_block(KBD_SOURCE, "other", [("bad.bin", "00000011")]),
            (None, AMBIGUOUS),
        )

    def test_the_label_device_rom_region_returns_settles_it(self):
        source = KBD_SOURCE + [
            'DEFINE_DEVICE_TYPE(KBD_X, kbd_x_device, "kbd_x", "Keyboard X")',
            "const tiny_rom_entry *kbd_x_device::device_rom_region() const",
            "{",
            "\treturn ROM_NAME(kbd_old);",
            "}",
        ]
        self.assertEqual(
            find_rom_block(source, "kbd_x", [("bad.bin", "00000011")]), ("2-6", "")
        )

    def test_locate_sources_reports_what_it_cannot_place(self):
        devices = _devices()
        devices["ghost"] = {
            "description": "Ghost", "sourcefile": "devices/ghost.cpp",
            "roms": [{"name": "g.bin", "size": 1, "crc32": "00000001", "sha1": ""}],
        }
        unplaced = locate_sources(devices, _fetch, workers=2)
        self.assertEqual(unplaced, {"ghost": NO_FILE})
        self.assertEqual(devices["qsound"]["source_file"], "src/devices/sound/qsound.cpp")
        self.assertEqual(devices["kbd_new"]["source_line"], "8-12")


class MergeIntoTheProfile(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mame.yml"
        self.path.write_text(PROFILE, encoding="utf-8")
        self.devices = _devices()
        locate_sources(self.devices, _fetch, workers=2)

    def tearDown(self):
        self._tmp.cleanup()

    def _merge(self) -> dict[str, list[str]]:
        return merge_mame_devices(str(self.path), self.devices, write=True)

    def _entries(self) -> dict[str, dict]:
        data = yaml_load(self.path.read_text(encoding="utf-8"))
        return {f["name"]: f for f in data["files"]}

    def test_a_device_with_roms_is_added_under_the_section(self):
        report = self._merge()
        self.assertEqual(report["added"], ["kbd_new"])
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index(DEVICE_SECTION), text.index("- name: kbd_new.zip"))
        entry = self._entries()["kbd_new.zip"]
        self.assertIs(entry["required"], False)
        self.assertEqual(entry["category"], "bios_zip")
        self.assertEqual(entry["source_ref"], "upstream src/devices/kbd.cpp:8-12")
        self.assertEqual(
            entry["note"],
            "Device ROM set, loaded when a machine instantiates `Keyboard & Mouse - New`.",
        )
        self.assertEqual(entry["contents"][0]["description"], "`Version 2`")
        self.assertEqual(entry["contents"][0]["sha1"], "83b21d0c8b93fc9b9b3b287fde4ec8f3badac5a2")
        self.assertIs(entry["contents"][1]["bad_dump"], True)

    def test_devices_without_roms_are_not_written(self):
        self._merge()
        entries = self._entries()
        self.assertNotIn("nodump_dev.zip", entries)
        self.assertNotIn("empty_dev.zip", entries)

    def test_an_unplaced_set_is_written_without_a_line(self):
        del self.devices["kbd_new"]["source_line"]
        self._merge()
        self.assertNotIn("source_ref", self._entries()["kbd_new.zip"])

    def test_a_written_entry_gets_its_contents_and_keeps_the_rest(self):
        report = self._merge()
        self.assertEqual(report["updated"], ["qsound"])
        entry = self._entries()["qsound.zip"]
        self.assertIs(entry["required"], True)
        self.assertEqual(entry["system"], "capcom-cps2")
        self.assertEqual(entry["note"], "Written by hand.")
        self.assertEqual(entry["source_ref"], "upstream src/devices/sound/qsound.cpp:121-125")
        member = entry["contents"][0]
        self.assertEqual(member["description"], "QSound DSP microcode")
        self.assertEqual(member["sha1"], "555f50fe5cdf127619da7d854c03f4a244a0c501")

    def test_a_written_entry_without_a_ref_gets_the_found_one(self):
        self.path.write_text(
            PROFILE.replace('    source_ref: "upstream src/devices/sound/qsound.cpp:121-125"\n', ""),
            encoding="utf-8",
        )
        self._merge()
        self.assertEqual(
            self._entries()["qsound.zip"]["source_ref"],
            "upstream src/devices/sound/qsound.cpp:5-8",
        )

    def test_bios_roots_are_left_as_written(self):
        before = PROFILE[PROFILE.index("  # SNK"):PROFILE.index("  - name: qsound.zip")]
        self._merge()
        self.assertIn(before, self.path.read_text(encoding="utf-8"))

    def test_a_second_run_changes_nothing(self):
        self._merge()
        first = self.path.read_text(encoding="utf-8")
        report = self._merge()
        self.assertEqual((report["added"], report["updated"]), ([], []))
        self.assertEqual(self.path.read_text(encoding="utf-8"), first)

    def test_new_sets_keep_name_order_inside_the_section(self):
        self._merge()
        self.devices["aaa_dev"] = dict(self.devices["kbd_new"])
        self.devices["zzz_dev"] = dict(self.devices["kbd_new"])
        self._merge()
        text = self.path.read_text(encoding="utf-8")
        order = [text.index(f"- name: {n}.zip") for n in ("aaa_dev", "kbd_new", "zzz_dev")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(text.endswith('        bad_dump: true\n'))

    def test_another_category_under_a_device_name_is_reported(self):
        self.path.write_text(
            PROFILE + "\n  - name: kbd_new.zip\n    category: game_data\n", encoding="utf-8"
        )
        report = self._merge()
        self.assertEqual(report["conflict"], ["kbd_new"])

    def test_a_new_bios_root_lands_before_the_device_section(self):
        self._merge()
        hashes = Path(self._tmp.name) / "h.json"
        hashes.write_text(json.dumps({
            "version": "0.289",
            "commit": "f34f02505e32c1993c6a782b6814232cbfc74e36",
            "bios_sets": {"newbios": {
                "source_file": "src/mame/x/x.cpp", "source_line": 7,
                "roms": [{"name": "n.bin", "size": 4, "crc32": "12345678"}],
            }},
        }), encoding="utf-8")
        merge_mame_profile(str(self.path), str(hashes), write=True)
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index("newbios.zip"), text.index(DEVICE_SECTION))
        self.assertEqual(self._entries()["newbios.zip"]["source_ref"], "upstream src/mame/x/x.cpp:7")
        self.assertIn("kbd_new.zip", self._entries())


class RefRole(unittest.TestCase):

    def test_a_port_cites_mamedev_as_upstream(self):
        self.assertEqual(mame_ref_role({
            "source": "https://github.com/libretro/mame",
            "upstream": "https://github.com/mamedev/mame",
        }), "upstream ")

    def test_a_profile_with_one_tree_cites_it_bare(self):
        self.assertEqual(mame_ref_role({"emulator": "MAME"}), "")


class Cli(unittest.TestCase):

    def test_devices_needs_a_listxml(self):
        from scripts.scraper.mame_hash_scraper import build_parser, main  # noqa: F401
        import sys
        from unittest import mock

        with mock.patch.object(sys, "argv", ["x", "--devices"]), \
                mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)


class TheDevicesFollowTheRelease(unittest.TestCase):
    """Pipeline step 2a moved mame.yml to a new release and left the device
    sets on the lines of the previous one: the profile named two releases."""

    def _run(self, moves: bool, dry_run: bool = False) -> list:
        import argparse
        from unittest import mock

        from scripts.scraper import mame_hash_scraper as scraper

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "mame.yml"
            profile.write_text('core_version: "0.289"\nfiles: []\n')

            def merge(path, *_args, **_kwargs):
                if moves:
                    profile.write_text('core_version: "0.290"\nfiles: []\n')

            merged = []
            with mock.patch.multiple(
                scraper,
                _EMULATORS_DIR=Path(directory),
                _fetch_hashes=lambda force: {"bios_sets": {}, "version": "0.290"},
                _find_mame_profiles=lambda: [profile],
                compute_diff=lambda *a, **k: {"updated": ["x"], "added": []},
                _format_diff=lambda *a, **k: [],
                merge_mame_profile=merge,
                fetch_release_listxml=lambda version: Path(f"mame{version}lx.zip"),
                _merge_devices=merged.append,
            ), mock.patch("sys.stdout", new=io.StringIO()):
                scraper._run(argparse.Namespace(force=False, json=False, dry_run=dry_run))
            return merged

    def test_a_moved_pin_merges_that_release_devices(self):
        (args,) = self._run(moves=True)
        self.assertEqual(args.listxml, Path("mame0.290lx.zip"))
        self.assertFalse(args.dry_run)

    def test_an_unmoved_pin_leaves_the_devices(self):
        self.assertEqual(self._run(moves=False), [])
        self.assertEqual(self._run(moves=True, dry_run=True), [])


if __name__ == "__main__":
    unittest.main()
