"""Prose in the repository is written the way its maintainer writes.

Em-dashes, clause-joining semicolons, unicode arrows, the ellipsis character,
box-drawing banners and check-mark emoji had spread through profiles, docs,
comments and command output. None of them is wrong, but together they gave the
project a generated look its maintainer did not want. This module names every
place one comes back.
"""

from __future__ import annotations

import io
import re
import subprocess
import tokenize
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

GLYPHS = re.compile(
    "[–—…←-⇿─-╿✓-✘⚠✅❌]"
)
# A semicolon between two words of prose. Code quoted in prose sits in
# backticks, which are blanked before the search.
CLAUSE_SEMICOLON = re.compile(r"[A-Za-z0-9)\]\"']; [A-Za-z(]")
BACKTICKS = re.compile(r"`[^`\n]*`")
FENCED = re.compile(r"^```.*?^```", re.S | re.M)

SKIPPED_TREES = ("bios/", "data/", "recipes/", "provenance/", "install/")
TEXT_SUFFIXES = {
    ".py", ".sh", ".ps1", ".js", ".css", ".md", ".yml", ".yaml", ".html",
    ".txt", ".cfg", ".toml", "",
}
PROSE_KEYS = {
    "notes", "note", "description", "exclusion_note", "size_note",
    "region_note", "hle_note", "comment", "reason",
}


def _tracked() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [
        ROOT / name
        for name in listed
        if not name.startswith(SKIPPED_TREES)
        and Path(name).suffix in TEXT_SUFFIXES
        and (ROOT / name).is_file()
    ]


def _semicolons(text: str) -> list[str]:
    return [
        line.strip()
        for line in BACKTICKS.sub("``", text).splitlines()
        if CLAUSE_SEMICOLON.search(line)
    ]


def _python_prose(source: str) -> str:
    """Comments and docstrings of a module, one per line."""
    parts: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            parts.append(token.string)
        elif token.type == tokenize.STRING and previous in (
            tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT,
        ):
            parts.append(token.string)
        if token.type not in (tokenize.COMMENT, tokenize.NL):
            previous = token.type
    return "\n".join(parts)


class _TagTolerantLoader(yaml.SafeLoader):
    """mkdocs.yml names Python callables with !!python/name tags."""


_TagTolerantLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/", lambda loader, suffix, node: None
)


def _yaml_prose(text: str) -> str:
    """Prose scalars and comments of a YAML document."""
    parts = [
        line.split("#", 1)[1]
        for line in text.splitlines()
        if line.lstrip().startswith("#")
    ]

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PROSE_KEYS and isinstance(value, str):
                    parts.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(yaml.load(text, Loader=_TagTolerantLoader))
    return "\n".join(parts)


class NoGeneratedTypography(unittest.TestCase):
    def test_no_dash_arrow_ellipsis_or_banner_glyphs(self):
        offenders = []
        for path in _tracked():
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if GLYPHS.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "\n".join(offenders[:40]))


class NoClauseSemicolons(unittest.TestCase):
    def _check(self, files: list[Path], extract) -> None:
        offenders = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for line in _semicolons(extract(text)):
                offenders.append(f"{path.relative_to(ROOT)}: {line[:100]}")
        self.assertEqual(offenders, [], "\n".join(offenders[:40]))

    def test_markdown(self):
        files = [p for p in _tracked() if p.suffix == ".md"]
        self._check(files, lambda text: FENCED.sub("", text))

    def test_yaml_prose_and_comments(self):
        files = [p for p in _tracked() if p.suffix in (".yml", ".yaml")]
        self._check(files, _yaml_prose)

    def test_python_comments_and_docstrings(self):
        files = [p for p in _tracked() if p.suffix == ".py"]
        self._check(files, _python_prose)


    def test_schema_descriptions(self):
        offenders = []
        for path in sorted((ROOT / "schemas").glob("*.json")):
            text = path.read_text(encoding="utf-8")
            for line in _semicolons(text) + GLYPHS.findall(text):
                offenders.append(f"{path.relative_to(ROOT)}: {line[:100]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class GeneratedSiteProse(unittest.TestCase):
    """The site pages carry strings no tracked file shows verbatim."""

    def test_generated_pages(self):
        docs = ROOT / "docs"
        pages = [
            p for p in docs.rglob("*.md")
            if "superpowers" not in p.parts and "plans" not in p.parts
            and "specs" not in p.parts
        ] if docs.is_dir() else []
        if not pages:
            self.skipTest("docs/ not generated (python scripts/generate_site.py)")
        offenders = []
        for page in pages:
            text = re.sub(r"<script.*?</script>", "", page.read_text(encoding="utf-8"), flags=re.S)
            text = FENCED.sub("", text)
            for number, line in enumerate(text.splitlines(), 1):
                if GLYPHS.search(line) or _semicolons(line):
                    offenders.append(f"{page.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "\n".join(offenders[:40]))


if __name__ == "__main__":
    unittest.main()
