"""What the wiki and the generated README state holds against the code.

Each class reads a page and the code it describes. A page that quotes a
function's return shape, a resolution order or a field list drifts when the
code moves, and nothing else notices.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import json
import re
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
sys.path.insert(0, str(ROOT / "scripts"))

from tests.test_prose_style import _python_prose, _sentence_case  # noqa: E402

FENCED = re.compile(r"^```.*?^```", re.S | re.M)
PUBLISHED = sorted(WIKI.glob("*.md")) + [ROOT / "README.md", ROOT / "CONTRIBUTING.md"]


def _page(name: str) -> str:
    return (WIKI / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Body of a markdown section, up to the next heading of any level."""
    start = re.search(rf"(?m)^#+ {re.escape(heading)}\n", text)
    if start is None:
        raise AssertionError(f"no section {heading!r}")
    rest = text[start.end():]
    blanked = FENCED.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), rest)
    end = re.search(r"(?m)^#+ ", blanked)
    return rest[: end.start()] if end else rest


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"\|\s*-", line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split(" | ")])
    return rows[1:]


class TestSkeletonReadsTheResultShape(unittest.TestCase):
    """The skeleton indexed verify_platform's result as a list, and a test
    copied from it stopped on KeyError: 0 before any assertion."""

    def test_every_key_it_reads_is_one_verify_platform_returns(self):
        from verify import verify_platform

        result = verify_platform(
            {"platform": "x", "verification_mode": "existence", "systems": {}},
            {"files": {}, "indexes": {}},
            emu_profiles={},
        )
        text = _page("testing-guide.md").split("Example skeleton:", 1)[1]
        code = re.search(r"```python\n(.*?)```", text, re.S).group(1)
        read = [
            node.slice
            for node in ast.walk(ast.parse(code))
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "result"
        ]
        self.assertTrue(read, "the skeleton no longer reads the result")
        for key in read:
            self.assertIsInstance(key, ast.Constant)
            self.assertIn(key.value, result)


class InstallerOutputExample(unittest.TestCase):
    """The page showed a run with no `Not installable:` line while the
    RetroArch manifest left eleven entries out, and a count that the menu
    example below it no longer shared."""

    def test_run_and_menu_show_one_dated_run_of_the_manifest(self):
        import install

        text = _page("installer.md")
        intro, run = re.search(r"(?s)\n([^\n]*)\n\n```\n(Fetching file index.*?)```", text).groups()
        self.assertRegex(intro, r"\d{4}-\d{2}-\d{2}", "an output example says when it was taken")
        manifest = json.loads((ROOT / "install" / "retroarch.json").read_text(encoding="utf-8"))
        omitted = re.search(r"Not installable: \d+ files? \(\d+ required, (.*)\)\.", run)
        if manifest.get("omitted_files"):
            self.assertIsNotNone(omitted, "the manifest omits files and the run shows no line for it")
        if omitted:
            for part in omitted.group(1).split(", "):
                self.assertIn(part.rsplit(":", 1)[0], install.OMISSION_REASONS.values())
        count, size = re.search(r"\n  (\d+) files \(([\d.]+ [KMG]B)\)", run).groups()
        self.assertIn(f"0/{count} present", run)
        menu = re.search(r"Retroarch: (\d+) files, ([\d.]+ [KMG]B)\.", text)
        self.assertEqual(menu.groups(), (count, size))


class ReleaseRefPromise(unittest.TestCase):
    """RETROBIOS_REF was said to pin a reproducible file set, while every
    release deletes the previous tags and large files come from one fixed
    release whatever the ref."""

    def test_the_page_says_what_a_tag_does_not_pin(self):
        import install

        self.assertIn("/large-files/", install.RELEASE_URL)
        self.assertIn("--cleanup-tag", _page("release-process.md"))
        row = next(
            line for line in _page("installer.md").splitlines()
            if line.startswith("| `RETROBIOS_REF`")
        )
        self.assertIn("`large-files`", row)
        self.assertIn("next release", row)
        for page in PUBLISHED:
            for paragraph in page.read_text(encoding="utf-8").split("\n\n"):
                if "RETROBIOS_REF" in paragraph:
                    self.assertNotIn("reproducible", paragraph, page.name)


class ReleaseCommitCarriesTheManifests(unittest.TestCase):
    """The documented release commit left out install/*.json, which the
    release pipeline regenerates: v2026.10.10 added three by hand."""

    def test_the_commit_step_adds_what_the_pipeline_rewrites(self):
        pipeline = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn('"--manifest"', pipeline)
        block = _section(_page("release-process.md"), "Cutting a release")
        add = next(line for line in block.splitlines() if line.startswith("git add "))
        self.assertIn("install/*.json", add.split())


class PageOpenings(unittest.TestCase):
    """Four pages opened by announcing what they cover."""

    META = re.compile(r"(?i)\bthis (page|guide) (covers|documents|explains|describes|picks)\b|^Diagnosis guide")

    def test_no_page_announces_itself(self):
        offenders = [
            f"{page.name}: {line[:80]}"
            for page in PUBLISHED
            for line in FENCED.sub("", page.read_text(encoding="utf-8")).splitlines()
            if self.META.search(line)
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))


class NoHedgedRules(unittest.TestCase):
    """The profiling guide and the verification discipline turned rules into
    wishes ("it's worth", "Ideally", "something to look into")."""

    FILLER = re.compile(
        r"(?i)\bit'?s worth\b|\bworth noting\b|\bit helps to\b|^Ideally\b"
        r"|\bsomething to look into\b|\busually indicate\b"
    )

    def test_published_pages(self):
        offenders = [
            f"{page.name}: {line[:80]}"
            for page in PUBLISHED
            for line in FENCED.sub("", page.read_text(encoding="utf-8")).splitlines()
            if self.FILLER.search(line)
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))


class ExitStatusTable(unittest.TestCase):
    """Code 2 was documented as 'every file failed' only, while argparse
    exits 2 on every refused option, and the 141 example named an option
    that does not exist."""

    def test_codes_and_options_are_the_installers(self):
        source = (ROOT / "install.py").read_text(encoding="utf-8")
        section = _section(_page("installer.md"), "Exit status")
        documented = {int(row[0].strip("`")) for row in _table_rows(section)}
        raised = {int(code) for code in re.findall(r"sys\.exit\((\d+)\)", source)}
        self.assertEqual(documented, raised | {2})
        self.assertIn("install.py: error:", section)
        for option in set(re.findall(r"(--[a-z][a-z-]+)", section)):
            self.assertIn(f'"{option}"', source, option)


class ProfileFieldsRequired(unittest.TestCase):
    """The table marked source, profiled_date, core_version and systems as
    required and called the schema authoritative, which requires none."""

    def test_yes_matches_the_schema(self):
        schema = json.loads((ROOT / "schemas" / "emulator.schema.json").read_text(encoding="utf-8"))
        required = set(schema["required"])
        for rule in schema.get("allOf", []):
            for branch in ("then", "else"):
                required |= set(rule.get(branch, {}).get("required", []))
        section = _section(_page("profiling.md"), "Profile fields")
        marked = {
            row[0].strip("`") for row in _table_rows(section)
            if row[1].startswith(("yes", "for `type"))
        }
        self.assertEqual(marked, required)


class PythonFloorOnRetroPie(unittest.TestCase):
    """The page offered `apt install python3` to a RetroPie 4.8 image, where
    python3 is 3.7.3 and apt has nothing newer."""

    def test_both_pages_name_the_case(self):
        install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("(3, 8)", install_sh)
        first = _section(_page("first-install.md"), "Error: Python 3.8 or newer is required.")
        self.assertIn("RetroPie 4.8", first)
        self.assertIn("3.7.3", first)
        changing = _section(_page("installer.md"), "Changing the installer")
        self.assertIn("3.7.3", changing)


if __name__ == "__main__":
    unittest.main()
