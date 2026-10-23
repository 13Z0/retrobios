"""An md5 written as a list of strings in batocera-systems.

batocera.linux 6b08b873 lets an item's md5 be one string or a list, and
checkBios accepts a file whose md5 is any listed value (`md5Matches`, and
`checkInsideZip` for a zippedFile). The scrapers keep the values
comma-joined with the list form recorded, the export writes a list back as
a list, and verify accepts any listed md5.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import resolve_local_file  # noqa: E402
from exporter import discover_exporters  # noqa: E402
from exporter.baseline import build_native_model  # noqa: E402
from scraper import rocknix_scraper  # noqa: E402
from scraper.base_scraper import md5_field, requirement_entry  # noqa: E402
from scraper.batocera_scraper import (  # noqa: E402
    SYSTEM_SLUG_MAP,
    extract_systems_dict,
    platform_systems,
    requirements_from_systems,
)
from verify import Status, verify_entry_md5  # noqa: E402

A = "dc2b9bf8da62ec93e868cfd29f0d067d"
B = "924e392ed05558ffdb115408c263dccf"
C = "8dd7d5296a650fac7319bce665a6a53c"

# The psx entry as 6b08b873 writes it, one string item beside one list item.
SCRIPT = (
    "#!/usr/bin/env python\n"
    "systems = {\n"
    '    "psx": { "name": "PSX", "biosFiles": [  '
    '{ "md5": "' + C + '", "file": "bios/scph5500.bin"    },\n'
    '                                            '
    '{ "md5": [ "' + A + '", "' + B + '" ], "file": "bios/scph1001.bin"    } ] },\n'
    "}\n"
    "def checkBios(systems, prefix, filterROMs):\n"
    "    return {}\n"
)


def _scraped(script: str = SCRIPT) -> dict:
    systems = extract_systems_dict(script)
    requirements = requirements_from_systems(systems, SYSTEM_SLUG_MAP)
    return {"cores": [], "systems": platform_systems(requirements, systems, SYSTEM_SLUG_MAP)}


def _files(scraped: dict) -> dict[str, dict]:
    return {fe["name"]: fe for system in scraped["systems"].values() for fe in system["files"]}


class Md5Field(unittest.TestCase):
    def test_string_and_list(self):
        self.assertEqual(md5_field(A), ([A], False))
        self.assertEqual(md5_field(""), ([], False))
        self.assertEqual(md5_field(None), ([], False))
        self.assertEqual(md5_field([A, B]), ([A, B], True))
        self.assertEqual(md5_field([A]), ([A], True))
        self.assertEqual(md5_field([]), ([], True))

    def test_anything_else_is_refused(self):
        with self.assertRaises(ValueError):
            md5_field(32)
        with self.assertRaises(ValueError):
            md5_field([A, 1])


class BatoceraScraper(unittest.TestCase):
    def test_a_string_stays_one_value(self):
        entry = _files(_scraped())["scph5500.bin"]
        self.assertEqual(entry["md5"], C)
        self.assertNotIn("md5_list", entry)

    def test_a_list_is_joined_and_marked(self):
        entry = _files(_scraped())["scph1001.bin"]
        self.assertEqual(entry["md5"], f"{A},{B}")
        self.assertIs(entry["md5_list"], True)

    def test_a_one_item_list_keeps_its_form(self):
        script = SCRIPT.replace(f'[ "{A}", "{B}" ]', f'[ "{A}" ]')
        entry = _files(_scraped(script))["scph1001.bin"]
        self.assertEqual(entry["md5"], A)
        self.assertIs(entry["md5_list"], True)

    def test_an_empty_list_checks_by_existence(self):
        script = SCRIPT.replace(f'[ "{A}", "{B}" ]', "[]")
        entry = _files(_scraped(script))["scph1001.bin"]
        self.assertNotIn("md5", entry)
        self.assertIs(entry["md5_list"], True)

    def test_the_schema_accepts_the_entry(self):
        import json

        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is a tooling dependency")

        schema = json.loads((REPO_ROOT / "schemas" / "platform.schema.json").read_text())
        entry = _files(_scraped())["scph1001.bin"]
        jsonschema.validate(entry, schema["$defs"]["bios_file"])


class RocknixScraper(unittest.TestCase):
    def _requirements(self, script: str):
        scraper = rocknix_scraper.Scraper(url="https://example.invalid/rocknix-systems")
        scraper._fetch_raw = lambda: script  # type: ignore[method-assign]
        return {req.name: requirement_entry(req) for req in scraper.fetch_requirements()}

    def test_string_and_list(self):
        entries = self._requirements(SCRIPT)
        self.assertEqual(entries["scph5500.bin"]["md5"], C)
        self.assertNotIn("md5_list", entries["scph5500.bin"])
        self.assertEqual(entries["scph1001.bin"]["md5"], f"{A},{B}")
        self.assertIs(entries["scph1001.bin"]["md5_list"], True)


class BatoceraExportRoundTrip(unittest.TestCase):
    def _render(self, truth_files: list[dict], script: str = SCRIPT) -> str:
        scraped = _scraped(script)
        systems, report = build_native_model(
            {"systems": {"sony-playstation": {"files": truth_files}}}, scraped
        )
        exporter = discover_exporters()["batocera"]()
        return exporter.render(systems, report, {"batocera-systems": script}, scraped)[
            "batocera-systems"]

    def test_nothing_to_correct_leaves_the_script_unchanged(self):
        self.assertEqual(self._render([]), SCRIPT)

    def test_a_one_item_list_is_written_back_as_a_list(self):
        script = SCRIPT.replace(f'[ "{A}", "{B}" ]', f'[ "{A}" ]')
        self.assertEqual(self._render([], script), script)

    def test_a_corrected_list_stays_a_list(self):
        produced = self._render([{
            "name": "scph1001.bin", "path": "scph1001.bin", "md5": [A, B, "f" * 32],
            "_cores": ["pcsx_rearmed"],
        }])
        self.assertEqual(
            produced,
            SCRIPT.replace(f'[ "{A}", "{B}" ]', f'[ "{A}", "{B}", "{"f" * 32}" ]'),
        )

    def test_a_string_item_stays_a_string(self):
        produced = self._render([{
            "name": "scph5500.bin", "path": "scph5500.bin", "md5": "e" * 32,
            "_cores": ["pcsx_rearmed"],
        }])
        self.assertEqual(produced, SCRIPT.replace(C, "e" * 32))


class VerifyAcceptsAnyListedMd5(unittest.TestCase):
    def setUp(self) -> None:
        scratch = REPO_ROOT / "tmp"
        self.tmp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _file(self, payload: bytes) -> str:
        path = self.root / "scph1001.bin"
        path.write_bytes(payload)
        return str(path)

    def test_each_listed_md5_matches(self):
        first, second, other = b"first", b"second", b"other"
        entry = _files(_scraped(SCRIPT.replace(A, hashlib.md5(first).hexdigest())
                                .replace(B, hashlib.md5(second).hexdigest())))["scph1001.bin"]
        for payload in (first, second):
            self.assertEqual(verify_entry_md5(entry, self._file(payload))["status"], Status.OK)
        self.assertEqual(verify_entry_md5(entry, self._file(other))["status"], Status.UNTESTED)

    def test_the_resolver_finds_the_second_listed_dump(self):
        """The pack builder and the manifest resolve through this function."""
        payload = b"second"
        md5 = hashlib.md5(payload).hexdigest()
        sha1 = hashlib.sha1(payload).hexdigest()
        path = self._file(payload)
        db = {
            "files": {sha1: {"path": path, "md5": md5, "sha1": sha1, "name": "scph1001.bin"}},
            "indexes": {"by_md5": {md5: sha1}, "by_name": {"scph1001.bin": [sha1]}},
        }
        entry = {"name": "scph1001.bin", "md5": f"{A},{md5}", "md5_list": True}
        self.assertEqual(resolve_local_file(entry, db), (path, "md5_exact"))

    def test_inside_a_zip(self):
        member = b"member"
        entry = {"name": "pack.zip", "zipped_file": "rom.bin",
                 "md5": f"{A},{hashlib.md5(member).hexdigest()}", "md5_list": True}
        archive = self.root / "pack.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("rom.bin", member)
        self.assertEqual(verify_entry_md5(entry, str(archive))["status"], Status.OK)


if __name__ == "__main__":
    unittest.main()
