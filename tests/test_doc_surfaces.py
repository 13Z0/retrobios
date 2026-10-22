"""What the README, the site and the wiki tell an end user.

Someone with a game that does not boot runs the one-line installer, not a
clone of this repository: `scripts/verify.py` reads the collection, never
their BIOS folder. A download link names the file to fetch, parts included,
from the release the table describes. Output shown on a page is output the
tool prints, and a size is the one the release record holds.
"""

from __future__ import annotations

import contextlib
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_record  # noqa: E402
from generate_readme import (  # noqa: E402
    contributing_text,
    download_links,
    download_table,
    format_size,
)

RECORD = release_record.load_record(REPO_ROOT / "release.json")
REPO = "https://github.com/Abdess/retrobios"

SPLIT_RECORD = {
    "tag": "v2000.01.01",
    "packs": {
        "Batocera_1_BIOS_Pack.zip": {
            "files": 3,
            "extracted_size": 3 * 1024**3,
            "download_size": 2 * 1024**3,
            "assets": [
                "Batocera_1_BIOS_Pack.part1of2.zip",
                "Batocera_1_BIOS_Pack.part2of2.zip",
            ],
            "platforms": ["batocera"],
        },
        "RomM_1_BIOS_Pack.zip": {
            "files": 1,
            "extracted_size": 1024**2,
            "download_size": 1024**2,
            "assets": ["RomM_1_BIOS_Pack.zip"],
            "platforms": ["romm"],
        },
    },
}


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The text under a markdown heading, up to the next heading of its level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1:]
    stop = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[: stop.start()] if stop else rest


def _which_pack() -> str:
    import generate_site

    with contextlib.chdir(REPO_ROOT):
        return generate_site.generate_which_pack()


class TheDownloadTablesFollowTheRegistry(unittest.TestCase):
    """EmuDeck has a Windows edition the installer detects, and the Windows
    table of the download page left it out."""

    def test_every_detected_system_lists_the_platform(self):
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load(_read("platforms/_registry.yml"))
        names = {"retroarch": "RetroArch", "retrobat": "RetroBat", "emudeck": "EmuDeck",
                 "bizhawk": "BizHawk"}
        page = _which_pack()
        for os_name, heading in (("windows", "### Windows"), ("darwin", "### macOS")):
            table = _section(page, heading)
            for platform, entry in registry["platforms"].items():
                systems = {rule["os"] for rule in (entry.get("install") or {}).get("detect", [])}
                if os_name in systems:
                    with self.subTest(os=os_name, platform=platform):
                        self.assertIn(f"[{names[platform]}](", table)


class DownloadLinksNameTheFile(unittest.TestCase):
    def test_a_whole_pack_links_its_asset(self):
        self.assertEqual(
            download_links("romm", SPLIT_RECORD, "../..", "Download"),
            "[Download](../../releases/download/v2000.01.01/RomM_1_BIOS_Pack.zip)",
        )

    def test_a_split_pack_links_every_part(self):
        cell = download_links("batocera", SPLIT_RECORD, "../..", "Download")
        for part in ("part1of2", "part2of2"):
            self.assertIn(
                f"releases/download/v2000.01.01/Batocera_1_BIOS_Pack.{part}.zip", cell
            )
        self.assertIn("part 1 of 2", cell)

    def test_buttons_carry_their_attributes_on_each_part(self):
        cell = download_links("batocera", SPLIT_RECORD, REPO, "Pack", " { .x }")
        self.assertEqual(cell.count("{ .x }"), 2)

    def test_a_release_without_the_pack_names_the_installer(self):
        self.assertEqual(
            download_links("bizhawk", SPLIT_RECORD, "../..", "Download"),
            "Not in v2000.01.01: "
            "[use the installer](https://abdess.github.io/retrobios/wiki/installer/)",
        )

    def test_without_a_recorded_release_the_link_is_the_release_page(self):
        self.assertEqual(
            download_links("bizhawk", {}, "../..", "Download"),
            "[Download](../../releases/latest)",
        )

    def test_the_readme_table_links_each_recorded_pack(self):
        coverages = {
            "batocera": {"platform": "Batocera"},
            "romm": {"platform": "RomM"},
        }
        rows = download_table(coverages, set(), {}, SPLIT_RECORD)[2:]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("releases/latest", row)
            self.assertIn("releases/download/v2000.01.01/", row)

    def test_the_generated_readme_links_every_released_asset(self):
        if not RECORD.get("packs"):
            self.skipTest("release.json is not written")
        readme = _read("README.md")
        for pack in RECORD["packs"].values():
            for asset in pack["assets"]:
                with self.subTest(asset=asset):
                    self.assertIn(
                        f"../../releases/download/{RECORD['tag']}/{asset})", readme
                    )

    def test_the_download_page_rows_link_assets(self):
        if not RECORD.get("packs"):
            self.skipTest("release.json is not written")
        page = _which_pack()
        rows = [line for line in page.splitlines() if line.startswith("| ")]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("releases/latest", row)
        for pack in RECORD["packs"].values():
            for asset in pack["assets"]:
                self.assertIn(f"/releases/download/{RECORD['tag']}/{asset})", page)

    def test_a_platform_the_release_lacks_has_no_row(self):
        """KNULLI, AmberELEC and EmuELEC were registered after the recorded
        release: a row for them linked a release page holding no such pack."""
        if not RECORD.get("packs"):
            self.skipTest("release.json is not written")
        from common import list_registered_platforms, load_platform_config
        from release_record import pack_for

        page = _which_pack()
        for key in list_registered_platforms("platforms", include_archived=True):
            if pack_for(key, RECORD) is not None:
                continue
            label = load_platform_config(key, "platforms").get("platform", key)
            with self.subTest(platform=key):
                self.assertNotIn(f"| [{label}](", page)


class SlicesLeaveWithTheirRelease(unittest.TestCase):
    """Parts named `.zip.001` were cut by v2026.09.04, deleted since. A page
    explaining a layout no release serves buries the one the user has."""

    PAGES = ("README.md", "wiki/getting-started.md", "wiki/troubleshooting.md")

    def setUp(self):
        assets = [a for p in RECORD.get("packs", {}).values() for a in p["assets"]]
        if any(re.search(r"\.zip\.\d{3}$", a) for a in assets):
            self.skipTest("the recorded release is published in slices")

    def test_user_pages_name_one_layout(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                self.assertNotIn(".zip.001", _read(page))
                self.assertNotIn("copy /b", _read(page))

    def test_the_download_page_names_one_layout(self):
        self.assertNotIn(".zip.001", _which_pack())


class EndUsersCheckTheirOwnFolder(unittest.TestCase):
    """verify.py reads database.json and bios/. Advice about a user's own
    files points at the installer's --check, which hashes their folder."""

    USER_SECTIONS = (
        ("wiki/troubleshooting.md", "## Game won't start / black screen"),
        ("wiki/faq.md", "## My game shows a black screen"),
        ("wiki/faq.md", "## How do I know which BIOS I need?"),
        ("wiki/getting-started.md", "## Checking what you installed"),
    )

    def test_user_sections_never_send_to_verify(self):
        for page, heading in self.USER_SECTIONS:
            with self.subTest(page=page, heading=heading):
                section = _section(_read(page), heading)
                self.assertNotIn("verify.py", section)
                self.assertNotIn("python install.py --check", section)

    def test_the_download_page_gives_the_one_liner_check(self):
        page = _which_pack()
        self.assertNotIn("from a checkout", page)
        self.assertIn("install.sh | sh -s -- --check", page)

    def test_the_readme_does_not_call_verify_a_check_of_your_bios(self):
        self.assertNotIn("Verify your BIOS collection", _read("README.md"))


class DestIsTheBiosFolder(unittest.TestCase):
    """--dest is used as the BIOS folder as given (install.py), so the root
    of a card puts every file beside the folder the frontend reads."""

    PAGES = (
        "README.md",
        "wiki/getting-started.md",
        "wiki/installer.md",
        "wiki/faq.md",
        "wiki/first-install.md",
    )

    def test_no_example_installs_to_a_card_root(self):
        for page in self.PAGES:
            for dest in re.findall(r"--dest\s+(\S+)", _read(page)):
                with self.subTest(page=page, dest=dest):
                    self.assertFalse(dest.rstrip("/").endswith("sdcard"), dest)


class ShownOutputIsPrintedOutput(unittest.TestCase):
    """Every line in the troubleshooting page's verify samples has a shape
    verify.py prints, and the shapes are read from its source."""

    SHAPES = (
        (r"^\S.*: \d+/\d+ (OK|present)\b.*\[(md5|sha1|existence)\]$", "}: {', '.join(parts)} [{mode}]"),
        (r"^  (OK|UNTESTED|MISSING) \((required|optional)[^)]*\): \S+/\S+", '"  UNTESTED ({req}{hle}): {key}: {reason}"'),
        (r"^  DISCREPANCY: \S+/\S+: ", '"  DISCREPANCY: {key}: {disc}"'),
        (r"^  Core files: \d+ in pack, \d+ required missing, \d+ optional missing$", '"  Core files: {core_in_pack} in pack'),
        (r"^    \S", None),
    )

    def test_the_shapes_are_verify_output(self):
        source = _read("scripts/verify.py")
        for _pattern, literal in self.SHAPES:
            if literal:
                with self.subTest(literal=literal):
                    self.assertIn(literal, source)

    def test_samples_match_a_shape(self):
        section = _section(_read("wiki/troubleshooting.md"), "### Reading the output")
        blocks = re.findall(r"```\n(.*?)```", section, re.DOTALL)
        self.assertTrue(blocks)
        for block in blocks:
            for line in block.splitlines():
                with self.subTest(line=line):
                    self.assertTrue(
                        any(re.match(pattern, line) for pattern, _ in self.SHAPES), line
                    )

    def test_no_severity_column(self):
        self.assertNotIn("[CRITICAL]", _read("wiki/troubleshooting.md"))


class SizesComeFromTheRecord(unittest.TestCase):
    def test_the_quoted_pack_sizes_are_the_recorded_ones(self):
        text = _read("wiki/release-process.md")
        quoted = re.search(
            r"in (v[\d.]+),\s+(\w+) downloaded as ([\d.]+ GB) and extracted to"
            r"\s+([\d.]+ GB)",
            text,
        )
        self.assertIsNotNone(quoted, "the sizes are quoted with their release tag")
        tag, platform, download, extracted = quoted.groups()
        if tag != RECORD.get("tag"):
            self.skipTest(f"release.json records {RECORD.get('tag')}, not {tag}")
        pack = release_record.pack_for(platform.lower(), RECORD)
        self.assertEqual(download, format_size(pack["download_size"]))
        self.assertEqual(extracted, format_size(pack["extracted_size"]))


class ContributorChecksMatchCi(unittest.TestCase):
    """validate.yml fetches the data directories before the suite: the tests
    that build packs exit non-zero without them."""

    def test_the_pr_checklist_fetches_data_directories_before_the_tests(self):
        for text in (
            contributing_text(lambda page: page, "# C", "here"),
            _read("CONTRIBUTING.md"),
        ):
            block = _section(text, "## Before opening a PR")
            self.assertIn("python scripts/refresh_data_dirs.py", block)
            self.assertLess(
                block.index("refresh_data_dirs.py"), block.index("unittest discover")
            )

    def test_ci_does_the_same(self):
        workflow = _read(".github/workflows/validate.yml")
        self.assertLess(
            workflow.index("python scripts/refresh_data_dirs.py"),
            workflow.index("python -m unittest discover"),
        )


class PagesOpenOnTheirContent(unittest.TestCase):
    """The download page said "This page picks the right pack for a setup"
    before saying anything about a pack."""

    def test_no_paragraph_announces_its_page(self):
        import generate_site  # noqa: PLC0415

        text = generate_site.generate_which_pack()
        announcing = [
            line for line in text.splitlines()
            if re.match(r"(This|The) (page|section) ", line)
        ]
        self.assertEqual(announcing, [])


if __name__ == "__main__":
    unittest.main()
