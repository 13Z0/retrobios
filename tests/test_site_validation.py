"""Tests for rendered-site metadata and link validation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = ROOT / "tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))

from validate_site import validate_site  # noqa: E402


def _page(title: str, description: str, body: str = "") -> str:
    structured = json.dumps(
        {"@context": "https://schema.org", "@type": "TechArticle"}
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <title>{title}</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="https://example.test/retrobios/">
  <script type="application/ld+json">{structured}</script>
</head>
<body><main><h1 id="top">{title}</h1>{body}</main></body>
</html>
"""


class ProsePlaceholders(unittest.TestCase):
    """A path written with angle brackets reaches the reader as written."""

    def test_a_placeholder_is_text_not_a_tag(self):
        from siterender import _admonition_body, _escape_tags

        self.assertEqual(
            _escape_tags("gameProfiles/<title id>.ini under <system_dir>/Cemu"),
            "gameProfiles/&lt;title id>.ini under &lt;system_dir>/Cemu",
        )
        self.assertIn("&lt;title id>", _admonition_body("read <title id>.ini"))

    def test_code_spans_and_links_are_left_alone(self):
        from siterender import _escape_tags

        self.assertEqual(_escape_tags("`<system_dir>/x`"), "`<system_dir>/x`")
        self.assertEqual(_escape_tags("see <https://example.org/a>"), "see <https://example.org/a>")
        self.assertEqual(_escape_tags("size < 4096"), "size &lt; 4096")


class WikiSourceHeadings(unittest.TestCase):
    def test_no_prose_line_starts_with_a_hash(self):
        """Python-Markdown reads `#79).` at the start of a line as a heading:
        it asks for no space after the hash. A wrapped issue reference gave
        the release page a second H1 and failed the site build."""
        offenders = []
        for page in sorted((ROOT / "wiki").glob("*.md")):
            fenced = False
            for number, line in enumerate(
                page.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if line.lstrip().startswith(("```", "~~~")):
                    fenced = not fenced
                elif not fenced and line.startswith("#") and not line.startswith(
                    ("# ", "## ", "### ", "#### ", "##### ", "###### ")
                ):
                    offenders.append(f"{page.name}:{number}")
                elif not fenced and line[:1].isspace() and line.lstrip().startswith("#"):
                    # Inside a list item or an admonition an indented hash is
                    # a heading too; code that needs one goes in a fence.
                    offenders.append(f"{page.name}:{number}")
        self.assertEqual(offenders, [])

    def test_an_indented_hash_renders_as_a_heading(self):
        """What the check above guards: a wrapped `#79)` inside a list item."""
        try:
            import markdown
        except ImportError:
            self.skipTest("rendering needs markdown")
        html = markdown.markdown(
            "1. First paragraph.\n\n    Second paragraph (see issue\n    #79). More."
        )
        self.assertIn("<h1", html)


class PlatformDetails(unittest.TestCase):
    def test_lists_and_flags_read_as_prose(self):
        import generate_site

        lines = generate_site._render_platform_details(
            {"platform_details": {"megacd": {
                "extensions_tried": [".bin", ".zip"],
                "hle_available": False,
                "root": "<system_dir>/x",
            }}}
        )
        self.assertIn("    - extensions_tried: .bin, .zip", lines)
        self.assertIn("    - hle_available: no", lines)
        self.assertIn("    - root: &lt;system_dir>/x", lines)


class RenderedSiteValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.site.mkdir()
        self.config = self.root / "mkdocs.yml"
        self.config.write_text(
            "site_url: https://example.test/retrobios/\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_page_and_base_path_link_pass(self):
        asset = self.site / "assets" / "file.json"
        asset.parent.mkdir()
        asset.write_text("{}\n", encoding="utf-8")
        (self.site / "index.html").write_text(
            _page(
                "Home",
                "Unique home description.",
                '<a href="/retrobios/#top">Top</a>'
                '<a href="/retrobios/assets/file.json">Data</a>'
                '<img src="assets/logo.png" alt="Logo">',
            ),
            encoding="utf-8",
        )
        (self.site / "assets" / "logo.png").write_bytes(b"png")
        self.assertEqual(validate_site(self.site, self.config), [])

    def test_broken_fragment_and_missing_alt_fail(self):
        (self.site / "index.html").write_text(
            _page(
                "Home",
                "Unique home description.",
                '<a href="#absent">Missing</a><img src="missing.png">',
            ),
            encoding="utf-8",
        )
        issues = validate_site(self.site, self.config)
        self.assertTrue(any("missing fragment" in issue for issue in issues))
        self.assertTrue(any("images missing alt" in issue for issue in issues))
        self.assertTrue(any("broken local link" in issue for issue in issues))

    def test_duplicate_search_metadata_fails(self):
        (self.site / "index.html").write_text(
            _page("Repeated", "Repeated description."), encoding="utf-8"
        )
        child = self.site / "child"
        child.mkdir()
        (child / "index.html").write_text(
            _page("Repeated", "Repeated description."), encoding="utf-8"
        )
        issues = validate_site(self.site, self.config)
        self.assertTrue(any("duplicate title" in issue for issue in issues))
        self.assertTrue(any("duplicate description" in issue for issue in issues))



class EveryTestDependencyIsInstalledInCi(unittest.TestCase):
    """The markdown rendering test skipped on every CI run: the test job
    installed pyyaml and jsonschema only. A test that skips where it is
    meant to run guards nothing."""

    PACKAGES = {"yaml": "pyyaml", "jsonschema": "jsonschema", "markdown": "markdown"}

    def test_the_test_job_installs_what_the_tests_import(self):
        import ast

        root = Path(__file__).resolve().parents[1]
        local = (
            {p.stem for p in (root / "scripts").rglob("*.py")}
            | {p.name for p in (root / "scripts").iterdir() if p.is_dir()}
            | {p.stem for p in (root / "tests").glob("*.py")}
            | {"scripts", "tests", "install"}
        )
        imported: set[str] = set()
        for path in (root / "tests").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        third_party = {
            m for m in imported if m not in sys.stdlib_module_names and m not in local
        }
        workflow = (root / ".github" / "workflows" / "validate.yml").read_text()
        job = workflow[workflow.index("run-tests:"):]
        install_line = next(
            line for line in job.splitlines() if "pip install" in line
        )
        missing = sorted(
            m for m in third_party if self.PACKAGES.get(m, m) not in install_line
        )
        self.assertEqual(missing, [])



class TheTestingGuideNamesEveryModule(unittest.TestCase):
    """The guide described 18 modules of 144, and a contributor touching
    slots.py or the downloader found no module named for it."""

    def test_every_module_is_in_the_table(self):
        root = Path(__file__).resolve().parents[1]
        guide = (root / "wiki" / "testing-guide.md").read_text(encoding="utf-8")
        missing = sorted(
            path.stem for path in (root / "tests").glob("test_*.py")
            if f"`{path.stem}`" not in guide
        )
        self.assertEqual(missing, [])



class TheSitemapIsAnnouncedWhereCrawlersLook(unittest.TestCase):
    """robots.txt was published as /retrobios/robots.txt, which no crawler
    reads (RFC 9309 reads the host root): the sitemap it announced was
    never found that way."""

    def test_the_head_names_it_and_no_robots_is_written(self):
        root = Path(__file__).resolve().parents[1]
        override = (root / "docs_assets" / "overrides" / "main.html").read_text()
        self.assertIn('rel="sitemap"', override)
        source = (root / "scripts" / "generate_site.py").read_text()
        self.assertNotIn('"robots.txt"),\n        "User-agent', source)
        self.assertNotIn("Sitemap: https://", source)



class TheHomeTitleSaysWhatTheSiteIs(unittest.TestCase):
    def test_not_the_site_name_twice(self):
        from generate_site import SITE_NAME, _browser_title

        title = _browser_title(Path("index.md"), SITE_NAME)
        self.assertNotEqual(title, SITE_NAME)
        self.assertIn("BIOS", title)



class PlatformPagesPrintTheDeclaredCrc32(unittest.TestCase):
    """System.dat pins aes.zip by crc32 91e93107; the RetroArch page printed
    only its sha1 and md5, so the crc a clrmamepro user pastes found nothing."""

    def test_the_rendered_page(self):
        page = ROOT / "docs" / "platforms" / "retroarch.md"
        if not page.is_file():
            self.skipTest("site not generated")
        self.assertIn("CRC32: `91e93107`", page.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
