"""The gap report reads the collection the way the resolver does.

Three ways it called held files missing, or a missing file held: system IDs
compared verbatim (sega-megacd against sega-mega-cd), no sha256 in the hash
fallback (rust_dos names its SC-55 ROMs by sha256 alone), and directory names
indexed as files (BasiliskII's required ROM read as held because cpcemu keeps
a directory called ROM).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cross_reference import (  # noqa: E402
    _build_supplemental_index,
    cross_reference,
    entry_source,
    print_report,
)


def _index(**over) -> dict:
    base = {
        "by_name": {}, "by_name_lower": {}, "by_path_suffix": {}, "by_md5": {},
        "by_sha256": {}, "by_crc32": {}, "db_files": {}, "data_names": set(),
    }
    base.update(over)
    return base


class GapReportSources(unittest.TestCase):
    def test_a_sha256_alone_finds_the_file(self):
        entry = {"name": "wave1.bin", "sha256": "AB" * 32}
        self.assertEqual(entry_source(entry, _index(by_sha256={"ab" * 32: "s"})), "bios")

    def test_a_name_never_wins_over_a_declared_hash(self):
        """zquestclassic pins ending.mid by sha1, the collection holds only
        Hydra Castle Labyrinth's ending.mid: the report called it held."""
        other = "9" * 40
        index = _index(
            by_name={"ending.mid": [other]}, by_name_lower={"ending.mid": "ending.mid"},
            by_path_suffix={"data/midi/ending.mid": [other]},
            db_files={other: {"path": "bios/hcl/ending.mid", "md5": "1" * 32, "size": 9}},
        )
        pinned = {"name": "ending.mid", "sha1": "5" * 40}
        self.assertIsNone(entry_source(pinned, index))
        self.assertIsNone(entry_source({**pinned, "path": "data/midi/ending.mid"}, index))
        self.assertEqual(entry_source({"name": "ending.mid"}, index), "bios")
        self.assertEqual(entry_source({"name": "ending.mid", "sha1": other}, index), "bios")
        self.assertEqual(entry_source({"name": "ending.mid", "md5": "1" * 32}, index), "bios")

    def test_the_heading_and_the_total_count_bundled_files_alike(self):
        """abuse printed "Gaps: 3 missing" above three [BUNDLED] files and
        "0 missing (need to be sourced)" below."""
        import contextlib
        import io

        report = {"abuse": {
            "emulator": "Abuse", "systems": [], "gaps": 3, "total_files": 3,
            "platform_covered": 0, "gap_missing": 3,
            "gap_details": [
                {"name": f"f{i}", "required": True, "source": "missing", "bundled": True,
                 "note": ""}
                for i in range(3)
            ],
        }}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            print_report(report)
        text = out.getvalue()
        self.assertIn("Gaps: 3 bundled", text)
        self.assertNotIn("3 missing", text)
        self.assertIn("0 missing (need to be sourced)", text)

    def test_a_directory_does_not_stand_for_a_file(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp") as tmp:
            (Path(tmp) / "bios" / "cpcemu" / "ROM").mkdir(parents=True)
            names = _build_supplemental_index(str(Path(tmp) / "data"), str(Path(tmp) / "bios"))
        self.assertIsNone(entry_source({"name": "ROM"}, _index(data_names=names)))
        directory = {"name": "ROM", "type": "directory"}
        self.assertEqual(entry_source(directory, _index(data_names=names)), "data")

    def test_a_system_spelled_otherwise_is_still_declared(self):
        profiles = {"gpgx": {"emulator": "gpgx", "systems": ["sega-megacd"],
                             "files": [{"name": "bios_CD_U.bin"}]}}
        declared = {"megacd": {"bios_CD_U.bin"}}  # sega-mega-cd, normalized
        db = {"files": {}, "indexes": {}}
        report = cross_reference(profiles, declared, db)
        self.assertEqual(report["gpgx"]["gaps"], 0)

    def test_any_accepted_version_fills_the_slot(self):
        """raze accepts ten DUKE3D.GRP, but the report judged the first alone and
        listed the file to acquire while the collection holds the tenth."""
        profiles = {"raze": {"emulator": "Raze", "systems": ["dos"], "files": [
            {"name": "DUKE3D.GRP", "path": "DUKE3D.GRP", "size": 1, "sha1": "a" * 40},
            {"name": "DUKE3D.GRP", "path": "DUKE3D.GRP", "size": 2, "sha1": "b" * 40},
        ]}}
        db = {"files": {"b" * 40: {"path": "bios/DUKE3D.GRP", "name": "DUKE3D.GRP"}},
              "indexes": {"by_name": {}, "by_md5": {}, "by_crc32": {}, "by_sha256": {},
                          "by_path_suffix": {}}}
        report = cross_reference(profiles, {}, db)
        self.assertEqual(report["raze"]["gaps"], 1)
        self.assertEqual(
            [g["source"] for g in report["raze"]["gap_details"]], ["bios"]
        )


if __name__ == "__main__":
    unittest.main()
