"""The corrected platform files are published with every change named.

export_native printed five examples per kind of change and nothing else
recorded the rest: a platform maintainer had no page to fetch the corrected
file from and no list to check the diff against.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_native  # noqa: E402
from siteexports import generate_exports_page, load_export_reports  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _report(platform: str, files: list[str], **changes: object) -> dict:
    report = {
        "platform": platform,
        "originals": {f: f"https://example.org/{platform}/{f}" for f in files},
        "files": files,
        "summary": "3 kept, 1 added, 7 hashes corrected, 0 requirements corrected",
        "hashes_corrected": [], "hashes_filled": [], "sizes_corrected": [],
        "sizes_removed": [], "hashes_cleared": [], "requirements_corrected": 0,
        "withheld": [], "invalid": [],
    }
    report.update(changes)
    return report


class ExportReportIsWrittenAndRead(unittest.TestCase):
    def test_report_round_trips_through_the_page_loader(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as scratch:
            report = _report("recalbox", ["es_bios.xml"])
            export_native.write_report(Path(scratch) / "recalbox.json", report)
            (Path(scratch) / "recalbox").mkdir()
            self.assertEqual(load_export_reports(scratch), [report])

    def test_a_report_goes_into_a_directory_not_yet_made(self):
        """RetroPie with nothing left to correct writes no file, so its
        output directory was never made and the report crashed --all."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as scratch:
            target = Path(scratch) / "fresh" / "retropie.json"
            export_native.write_report(target, _report("retropie", []))
            self.assertTrue(target.exists())

    def test_export_keeps_every_change_not_the_printed_five(self):
        source = (ROOT / "scripts" / "export_native.py").read_text(encoding="utf-8")
        body = source[source.index("def export_platform("):source.index("def write_report(")]
        call = body[body.index("write_report("):]
        for key, value in (
            ("hashes_corrected", "applied"), ("hashes_filled", "filled"),
            ("sizes_corrected", "resized"), ("sizes_removed", "unsized"),
            ("hashes_cleared", "cleared"), ("invalid", "issues"),
        ):
            self.assertIn(f'"{key}": {value},', call)

    def test_default_output_is_the_published_directory(self):
        source = (ROOT / "scripts" / "export_native.py").read_text(encoding="utf-8")
        self.assertIn('"--output-dir", default="exports"', source)


class CorrectedFilesPage(unittest.TestCase):
    def setUp(self):
        corrected = [f"sys/file{i}.bin md5" for i in range(12)]
        self.reports = [
            _report("recalbox", ["es_bios.xml"], hashes_corrected=corrected),
            _report("retropie", ["scriptmodules/a.sh", "scriptmodules/b.sh"],
                    invalid=["bash -n failed on a.sh"],
                    withheld=["a size the format has no field for"]),
        ]
        self.page = generate_exports_page(self.reports, {"recalbox": "Recalbox"})

    def test_every_platform_has_a_row_and_a_section(self):
        self.assertIn("| [Recalbox](#recalbox) | [`es_bios.xml`]", self.page)
        self.assertIn("## Recalbox { #recalbox }", self.page)
        self.assertIn("## retropie { #retropie }", self.page)

    def test_single_file_is_downloadable_raw(self):
        self.assertIn(
            "(https://raw.githubusercontent.com/Abdess/retrobios/main/exports/recalbox/es_bios.xml)",
            self.page,
        )

    def test_several_files_link_to_their_directory(self):
        self.assertIn("tree/main/exports/retropie", self.page)
        self.assertIn('??? note "The 2 original files it is based on"', self.page)

    def test_every_change_is_listed(self):
        self.assertIn('??? note "Hashes updated to the emulator source (12)"', self.page)
        for i in range(12):
            self.assertIn(f"    - `sys/file{i}.bin md5`", self.page)

    def test_a_failing_file_is_said_to_fail(self):
        self.assertIn("    - bash -n failed on a.sh", self.page)
        self.assertIn("    - a size the format has no field for", self.page)

    def test_nothing_published_says_how_to_build(self):
        self.assertIn("export_native.py --all --fetch", generate_exports_page([], {}))

    def test_site_has_the_page_in_its_nav(self):
        source = (ROOT / "scripts" / "generate_site.py").read_text(encoding="utf-8")
        self.assertIn('{"Updated platform files": "exports.md"}', source)
        workflow = (ROOT / ".github" / "workflows" / "deploy-site.yml").read_text(encoding="utf-8")
        self.assertIn('"exports/**"', workflow)


class SummariesReadAsSentences(unittest.TestCase):
    """The page shows each run's summary. RetroPie's joined its parts with
    semicolons and read fs-uae's "(e.g, kick13.rom)" as two file names."""

    def test_an_abbreviation_is_not_a_file_name(self):
        from exporter.retropie_exporter import Exporter as RetroPieExporter

        names = RetroPieExporter._listed("BIOS files (e.g, kick13.rom) i.e. Kickstart")
        self.assertEqual(names, {"kick13.rom"})

    def test_retropie_outcome_has_no_semicolon(self):
        from exporter.retropie_exporter import Exporter as RetroPieExporter

        exporter = RetroPieExporter()
        exporter._skipped = {"whose help text lists no files to add to": ["fs-uae", "amiberry"]}
        text = exporter.outcome({}, {"a.sh": ""})
        self.assertNotIn(";", text)
        self.assertIn("Left as is: amiberry, fs-uae, whose help text lists no files to add to", text)


class ThePageAddressesMaintainersAsPeers(unittest.TestCase):
    """The page offers the platforms' own maintainers a file to take or
    leave. "Their file", "corrected" and "of theirs" read as a verdict on
    their work, which the page is not."""

    def test_the_change_sentence_is_built_from_the_numbers(self):
        from siteexports import change_text

        report = _report("batocera", ["batocera-systems"],
                         hashes_corrected=["a md5", "b md5"], requirements_corrected=3)
        report["counts"] = {"kept": 692, "added": 253, "not_added": 10, "not_kept": 0}
        self.assertEqual(
            change_text(report),
            "692 entries kept, 253 files added, 2 hashes updated, "
            "3 required flags aligned with the code",
        )

    def test_no_wording_that_judges_the_original(self):
        report = _report("batocera", ["batocera-systems"], hashes_corrected=["a md5"])
        report["counts"] = {"kept": 1, "added": 0, "not_added": 4, "not_kept": 1}
        page = generate_exports_page([report], {}).lower()
        for word in ("their file", "theirs", "corrected", "wrong", "drift", "left out"):
            self.assertNotIn(word, page)


class RemovalCandidatesNeedAReadingNoCoreAnswers(unittest.TestCase):
    """An entry the truth does not pair can still be read: by an alias, by its
    content, under a directory a core walks. Of 2776 such entries, 1189
    were read that way. Only what no core answers is listed, and nothing is
    removed."""

    PROFILES = {
        "corea": {
            "type": "libretro",
            "systems": ["sys-one"],
            "files": [
                {"name": "main.rom", "aliases": ["Alt.ROM"]},
                {"name": "other.bin", "md5": "0123456789abcdef0123456789abcdef"},
                {"name": "rtp/2000/", "path": "rtp/2000/", "type": "directory"},
                {"name": "set.zip", "category": "bios_zip"},
            ],
        },
    }

    def _scraped(self, names: list[dict], cores: list[str]) -> dict:
        return {
            "_key": "fake",
            "cores": cores,
            "systems": {"sys-one": {"files": names}},
        }

    def _candidates(self, files: list[dict], cores: list[str] | None = None) -> list[dict]:
        from exporter.baseline import build_native_model
        from exporter.candidates import removal_candidates

        scraped = self._scraped(files, cores or ["corea"])
        systems, _ = build_native_model({"systems": {}}, scraped)
        found = removal_candidates(systems, scraped, self.PROFILES)
        kept = [f.name for s in systems.values() for f in s.files]
        self.assertEqual(kept, [f["name"] for f in files])
        return found

    def test_an_alias_is_a_reading(self):
        self.assertEqual(self._candidates([{"name": "alt.rom"}]), [])

    def test_a_hash_is_a_reading(self):
        found = self._candidates([
            {"name": "renamed.bin", "md5": "0123456789ABCDEF0123456789ABCDEF"},
        ])
        self.assertEqual(found, [])

    def test_a_directory_entry_covers_its_members(self):
        found = self._candidates([
            {"name": "x.png", "destination": "easyrpg/rtp/2000/x.png"},
        ])
        self.assertEqual(found, [])

    def test_an_entry_nothing_answers_is_a_candidate(self):
        found = self._candidates([{"name": "nobody.rom"}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["verdict"], "candidate")
        self.assertEqual(found[0]["cores_examined"], ["corea"])
        self.assertEqual(found[0]["unresolved_cores"], [])

    def test_an_unprofiled_core_leaves_it_undetermined(self):
        found = self._candidates([{"name": "nobody.rom"}], ["corea", "mystery-sa"])
        self.assertEqual(found[0]["verdict"], "undetermined")
        self.assertEqual(found[0]["unresolved_cores"], ["mystery-sa"])

    def test_a_romset_under_a_romset_core_is_undetermined(self):
        found = self._candidates([{"name": "device.zip"}])
        self.assertEqual(found[0]["verdict"], "undetermined")
        self.assertIn("corea", found[0]["reason"])

    def test_unresolved_names_skip_the_all_libretro_markers(self):
        from common import unresolved_platform_cores

        config = {"cores": ["retroarch", "all_libretro", "corea", "nope"]}
        self.assertEqual(unresolved_platform_cores(config, self.PROFILES), {"nope"})
        self.assertEqual(unresolved_platform_cores({"cores": "all_libretro"}, {}), set())

    def test_the_page_gives_the_count_and_each_entry(self):
        report = _report("retroarch", ["System.dat"], removal_candidates=[
            {"system": "Enterprise", "name": "zt18hfnt.rom",
             "destination": "zt18hfnt.rom", "verdict": "candidate",
             "reason": "no core the platform runs opens it",
             "cores_examined": [], "unresolved_cores": []},
        ])
        page = generate_exports_page([report], {})
        self.assertIn("1 entry of the original is not opened", page)
        self.assertIn("- `Enterprise` `zt18hfnt.rom`", page)
        self.assertNotIn("no entry the platform declares is removed", page)

    def test_the_report_carries_the_list(self):
        source = (ROOT / "scripts" / "export_native.py").read_text(encoding="utf-8")
        body = source[source.index("def export_platform("):source.index("def write_report(")]
        self.assertIn('"removal_candidates": candidates,', body)


if __name__ == "__main__":
    unittest.main()
