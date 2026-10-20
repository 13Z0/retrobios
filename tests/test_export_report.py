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
        self.assertIn('??? note "The 2 files this export patches"', self.page)

    def test_every_change_is_listed(self):
        self.assertIn('??? note "Hashes corrected (12)"', self.page)
        for i in range(12):
            self.assertIn(f"    - `sys/file{i}.bin md5`", self.page)

    def test_a_failing_file_is_said_to_fail(self):
        self.assertIn("    - bash -n failed on a.sh", self.page)
        self.assertIn("    - a size the format has no field for", self.page)

    def test_nothing_published_says_how_to_build(self):
        self.assertIn("export_native.py --all --fetch", generate_exports_page([], {}))

    def test_site_has_the_page_in_its_nav(self):
        source = (ROOT / "scripts" / "generate_site.py").read_text(encoding="utf-8")
        self.assertIn('{"Corrected platform files": "exports.md"}', source)
        workflow = (ROOT / ".github" / "workflows" / "deploy-site.yml").read_text(encoding="utf-8")
        self.assertIn('"exports/**"', workflow)


if __name__ == "__main__":
    unittest.main()
