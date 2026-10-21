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
        menu = re.search(r"RetroArch: (\d+) files, ([\d.]+ [KMG]B)\.", text)
        self.assertEqual(menu.groups(), (count, size))


class OneResolutionChain(unittest.TestCase):
    """architecture.md drew path_suffix as the first step, before SHA1, while
    the code and verification-modes.md try it after every hash."""

    ORDER = ("SHA1", "SHA256", "CRC32", "MD5", "Path suffix")

    def test_the_table_follows_the_code(self):
        import common

        source = inspect.getsource(common.resolve_local_file)
        steps = re.findall(r"# \d+[a-z]?\. (SHA1|SHA256|CRC32|MD5|Path suffix)", source)
        self.assertEqual(tuple(dict.fromkeys(steps)), self.ORDER)
        table = _table_rows(_section(_page("verification-modes.md"), "File resolution chain"))
        methods = [row[1] for row in table[: len(self.ORDER)]]
        self.assertEqual(
            [next(m for m in self.ORDER if method.startswith(m)) for method in methods],
            list(self.ORDER),
        )

    def test_architecture_links_instead_of_drawing_a_second_one(self):
        section = _section(_page("architecture.md"), "File resolution chain")
        self.assertNotIn("```mermaid", section)
        self.assertIn("verification-modes.md#file-resolution-chain", section)


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


class RequirementFieldsTable(unittest.TestCase):
    """The table stopped at native_id: native_path, mirror_of, alt_md5 and
    the native dict, the fields that make a platform YAML reversible, were
    named nowhere a scraper author reads first."""

    def test_every_field_and_native_key_is_listed(self):
        from dataclasses import fields

        from scraper.base_scraper import BiosRequirement

        section = _section(_page("adding-a-platform.md"), "BiosRequirement fields")
        listed = {row[0].strip("`") for row in _table_rows(section)}
        self.assertEqual(listed, {f.name for f in fields(BiosRequirement)})
        native_row = next(row for row in _table_rows(section) if row[0] == "`native`")[2]
        keys: set[str] = set()
        for path in (ROOT / "scripts" / "scraper").glob("*_scraper.py"):
            source = path.read_text(encoding="utf-8")
            keys |= set(re.findall(r'native\["(\w+)"\]', source))
            keys |= set(re.findall(r'native(?:: dict\[str, object\])?\s*=\s*\{"(\w+)"', source))
        self.assertTrue(keys)
        for key in keys:
            self.assertIn(f"`{key}`", native_row)


class ReleaseCommitCarriesTheManifests(unittest.TestCase):
    """The documented release commit left out install/*.json, which the
    release pipeline regenerates: v2026.10.10 added three by hand."""

    def test_the_commit_step_adds_what_the_pipeline_rewrites(self):
        pipeline = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn('"--manifest"', pipeline)
        block = _section(_page("release-process.md"), "Cutting a release")
        add = next(line for line in block.splitlines() if line.startswith("git add "))
        self.assertIn("install/*.json", add.split())


class PublishedHeadings(unittest.TestCase):
    """README.md kept '## Quick Install', the one Title Case heading left,
    because the casing test read wiki/ only."""

    def test_readme_and_contributing_use_sentence_case(self):
        offenders = []
        for page in (ROOT / "README.md", ROOT / "CONTRIBUTING.md"):
            text = FENCED.sub("", page.read_text(encoding="utf-8"))
            for title in re.findall(r"(?m)^#{1,4} (.+)$", text):
                if _sentence_case(title) != title:
                    offenders.append(f"{page.name}: {title}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class FilesColumnParagraph(unittest.TestCase):
    """'Files is how many the pack holds' was glued to the paragraph above
    it, two paragraphs before the table whose column it described."""

    def test_it_names_the_column_in_its_own_paragraph_before_the_table(self):
        import generate_readme

        source = inspect.getsource(generate_readme)
        self.assertNotIn("Files is how many", source)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        paragraph = next(p for p in readme.split("\n\n") if "The Files column" in p)
        self.assertNotIn("\n", paragraph.strip())
        after = readme.split(paragraph, 1)[1].lstrip("\n")
        self.assertTrue(after.startswith("| Platform | Files |"))


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


class DocstringsWithoutInsistence(unittest.TestCase):
    """truth.py shouted ONE and NEVER in an eighteen-line docstring, and
    generate_pack.py listed three guarantees under 'This ensures:' of which
    two said the same thing."""

    TICS = re.compile(r"\b(NEVER|ALWAYS|ONE|MUST)\b|This ensures:")

    def test_scripts_and_installer(self):
        offenders = []
        paths = sorted((ROOT / "scripts").rglob("*.py")) + [ROOT / "install.py"]
        for path in paths:
            for line in _python_prose(path.read_text(encoding="utf-8")).splitlines():
                if self.TICS.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class PeerToneTowardPlatforms(unittest.TestCase):
    """Platform lists were called wrong, stale or corrected on public pages
    that their maintainers read."""

    WORDS = re.compile(
        r"(?i)\bgets wrong\b|\bstale (hash|entries|entry)\b|<their file>|\bpatch their\b"
        r"|, corrected\b|\bcorrected in place\b|\bdeclare incorrectly\b|\blist error\b"
    )

    def test_published_pages(self):
        offenders = [
            f"{page.name}: {line[:80]}"
            for page in PUBLISHED
            for line in page.read_text(encoding="utf-8").splitlines()
            if self.WORDS.search(line)
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))


class RecalboxSeverity(unittest.TestCase):
    """The table gave verify.py a mapping from hashMatchMandatory that the
    code never reads, and called a missing optional file GREEN."""

    def test_the_verify_column_is_what_compute_severity_returns(self):
        from verify import Status, compute_severity

        section = _section(_page("verification-modes.md"), "Mandatory levels (Recalbox)")
        rows = _table_rows(section)
        self.assertTrue(rows)
        status = {"hash matches": Status.OK, "missing": Status.MISSING, "hash differs": Status.UNTESTED}
        for state, mandatory, _hash_match, _colour, expected in rows:
            for required in ((True, False) if mandatory == "(any)" else (mandatory == "true",)):
                self.assertEqual(
                    compute_severity(status[state], required, "md5").upper(), expected,
                    f"{state} mandatory={mandatory}",
                )
        self.assertNotIn("severity levels", _page("tools.md"))


class Md5CompositeDefinition(unittest.TestCase):
    """The page described the MD5 of the members' MD5s, the code hashes the
    members' contents in name order."""

    def test_page_and_code_agree(self):
        from hashing import md5_composite_of

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("b.rom", b"BBBB")
            zf.writestr("a.rom", b"AAAA")
        buffer.seek(0)
        self.assertEqual(md5_composite_of(buffer), hashlib.md5(b"AAAABBBB").hexdigest())
        section = _section(_page("verification-modes.md"), "md5_composite (Recalbox ZIP verification)")
        self.assertIn("contents of every member", section)
        self.assertNotIn("inner file MD5s", section)

    def test_composite_only_is_read_by_both_checks(self):
        """The page said the direct MD5 is tried before the composite, which
        Recalbox never does for a .zip, and the pack check did the same."""
        import common
        import validation

        section = _section(_page("verification-modes.md"), "md5_composite (Recalbox ZIP verification)")
        self.assertIn("zip_digest: composite", section)
        self.assertNotIn("is tried before reporting a mismatch", section)
        for function in (common.resolve_local_file, validation.digest_verdict):
            self.assertIn('get("zip_digest") == "composite"', inspect.getsource(function))


class ValidationIndexScope(unittest.TestCase):
    """The page said the index reads every profile. verify and the builder
    pass it only the profiles the platform resolves."""

    def test_page_names_the_scope_the_code_uses(self):
        import verify

        source = inspect.getsource(verify.verify_platform)
        self.assertRegex(source, r"plat_cores = resolve_platform_cores\(config, profiles\)")
        self.assertIn("_build_validation_index(platform_profiles)", source)
        section = _section(_page("verification-modes.md"), "Validation index")
        self.assertIn("resolve_platform_cores", section)
        self.assertNotIn("reads all emulator profiles", section)


class CoreResolutionWrittenOnce(unittest.TestCase):
    """architecture.md and adding-a-platform.md each stated the core
    resolution rule, and only one knew that a list naming retroarch takes
    every libretro profile. Neither said built_by profiles stay out."""

    def test_one_statement_matching_the_platforms_and_the_code(self):
        import yaml

        import common

        self.assertNotIn("cores: [list]", _page("architecture.md"))
        source = inspect.getsource(common.resolve_platform_cores)
        self.assertIn('not p.get("built_by")', source)
        self.assertIn('"retroarch" in core_set', source)
        naming = set()
        for path in (ROOT / "platforms").glob("*.yml"):
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cores = config.get("cores")
            if isinstance(cores, list) and "retroarch" in map(str, cores):
                naming.add(str(config.get("platform") or path.stem).lower())
        section = _section(_page("adding-a-platform.md"), "Required fields")
        bullet = next(p for p in section.split("\n- ") if p.startswith("**Explicit list**"))
        used_by = re.search(r"Used by ([^.]+)\.", bullet.replace("\n", " ")).group(1)
        names = [n.strip().lower() for n in used_by.split(",")]
        self.assertIn("the last\n  four", bullet)
        self.assertEqual(set(names[-4:]), naming)
        libretro = next(p for p in section.split("\n- ") if p.startswith("**all_libretro**"))
        self.assertIn("built_by", libretro)


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


class ToolsRunOnTheStatedFloor(unittest.TestCase):
    """tools.md and testing-guide.md state Python 3.9 for every script, and
    check_release_assets.py built `int | None` at import: a TypeError on 3.9
    before any argument was read, in check_release.py too. CI runs 3.12 only,
    so this test reads the source for what 3.9 cannot evaluate."""

    TYPES = {"int", "str", "float", "bytes", "bool", "dict", "list", "tuple", "set", "type"}

    def _type_like(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.Subscript):
            node = node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._type_like(node.left) or self._type_like(node.right)
        return isinstance(node, ast.Name) and node.id in self.TYPES

    def _runtime_unions(self, tree: ast.AST) -> list[int]:
        annotations = set()
        for node in ast.walk(tree):
            for field in ("annotation", "returns"):
                value = getattr(node, field, None)
                if value is not None:
                    annotations.update(id(n) for n in ast.walk(value))
        return [
            node.lineno for node in ast.walk(tree)
            if id(node) not in annotations
            and (isinstance(node, ast.Match)
                 or isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                 and (self._type_like(node.left) or self._type_like(node.right)))
        ]

    def test_pages_state_the_floor(self):
        self.assertIn("Python 3.9 or newer", _page("tools.md"))
        self.assertIn("Python 3.9 or newer", _page("testing-guide.md"))

    def test_no_script_evaluates_310_syntax(self):
        offenders = [
            f"{path.relative_to(ROOT)}:{line}"
            for path in sorted((ROOT / "scripts").rglob("*.py"))
            for line in self._runtime_unions(ast.parse(path.read_text(encoding="utf-8")))
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_check_catches_the_alias(self):
        self.assertTrue(self._runtime_unions(ast.parse("F = tuple[str, int | None]")))
        self.assertFalse(self._runtime_unions(ast.parse("def f(x: int | None) -> str | None: ...")))
        self.assertFalse(self._runtime_unions(ast.parse("mask = flags | other")))


class DeriveFiguresLiveWithTheTool(unittest.TestCase):
    """tools.md gave --derive about 90 s and 67 entries while its --help said
    about 35 s: two hand-kept figures for one run, measured at 33 s."""

    def test_the_page_carries_no_duration_or_count(self):
        paragraph = next(
            p for p in _page("tools.md").split("\n\n") if "`--derive` reads" in p
        )
        self.assertNotRegex(paragraph, r"\d+ ?s\b|\d+ entries")
        self.assertIn("derivable.json", paragraph)


class EmulatorVerboseSample(unittest.TestCase):
    """advanced-usage.md showed `handy validates size=512 crc32=0x0d973c9d
    [src/handy/system.h:45]`, a format verify.py does not print and a
    reference handy.yml does not cite."""

    def test_every_sample_line_is_printed(self):
        import subprocess

        section = _section(_page("advanced-usage.md"), "Per-emulator checks")
        sample = [body for lang, body in re.findall(r"(?ms)^```(\w*)\n(.*?)^```", section) if not lang][-1]
        printed = subprocess.run(
            [sys.executable, "scripts/verify.py", "--emulator", "handy", "--verbose"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        for line in sample.splitlines():
            with self.subTest(line=line):
                self.assertIn(line, printed)


class ShortMd5Handling(unittest.TestCase):
    """Three pages said some Batocera entries carry 29-character MD5s and show
    as untested. No platform YAML holds one, since the Batocera scraper
    completes them, and adding-a-scraper.md said to store them as-is."""

    def test_no_page_claims_short_md5_entries(self):
        offenders = [
            page.name for page in PUBLISHED
            if re.search(r"(?i)29[- ]char|truncated md5 hashes in some entries|store the hash as-is",
                         page.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_the_page_names_what_the_code_does(self):
        import common
        from scraper import batocera_scraper

        scraper = inspect.getsource(batocera_scraper)
        self.assertIn("md5 = _resolve_truncated_md5(", scraper)
        resolver = inspect.getsource(common.resolve_local_file)
        self.assertIn("if len(md5_candidate) < 32:", resolver)
        self.assertIn("_md5_name_ok(path)", resolver)
        section = _section(_page("verification-modes.md"), "MD5 shorter than 32 characters (Batocera)")
        self.assertIn("_resolve_truncated_md5", section)


if __name__ == "__main__":
    unittest.main()
