"""A platform page lists one table row per file, the evidence on demand.

Batocera, AmberELEC and RomM ran to 14,000 to 16,000 words: every file
spelled its full MD5, the copy serving it and each source reference of
each reader, with "(emulator code, libretro port)" after every one. A row
now shows the name, the folder, the status and a short hash; its details
hold every hash, the served copy and the readers, the role of each
reference named by one word that a legend defines once.
"""

from __future__ import annotations

import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_site  # noqa: E402

_SKIPPED = frozenset({"script", "style", "template"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "source", "track", "wbr"})


class _Words(HTMLParser):
    """Words in <main>: all of them, and those a reader sees with every
    details block closed (a closed block shows its summary only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.visible = 0
        self.total = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            closed = tag == "details" and "open" not in dict(attrs)
            self.stack.append((tag, closed))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def _counted(self) -> bool:
        names = {name for name, _ in self.stack}
        return "main" in names and not names & _SKIPPED

    def _shown(self) -> bool:
        for index, (_name, closed) in enumerate(self.stack):
            if closed:
                rest = self.stack[index + 1:]
                if not rest or rest[0][0] != "summary":
                    return False
        return True

    def handle_data(self, data: str) -> None:
        if not self._counted():
            return
        count = len(data.split())
        self.total += count
        if self._shown():
            self.visible += count


def page_words(html: str) -> tuple[int, int]:
    """(visible words, all words) in a page's <main>."""
    parser = _Words()
    parser.feed(html)
    return parser.visible, parser.total


class VisibleWordsAreCounted(unittest.TestCase):
    def test_a_closed_block_shows_its_summary_only(self):
        page = ("<main><p>one two</p><details><summary>three</summary>four five"
                "<details open><summary>six</summary>seven</details></details>"
                "<details open><summary>eight</summary>nine</details>"
                "<script>no</script></main><p>outside</p>")
        self.assertEqual(page_words(page), (5, 9))


class ARowPerFile(unittest.TestCase):
    def _row(self, f: dict, entry: dict, hash_type: str = "md5") -> str:
        return generate_site._platform_file_row(f, entry, hash_type)

    def test_the_row_at_a_glance(self):
        md5, sha1 = "a" * 32, "b" * 40
        row = self._row({"name": "bios_a.bin", "status": "ok", "size": 2048},
                        {"name": "bios_a.bin", "md5": md5, "sha1": sha1, "crc32": "0badf00d",
                         "destination": "bios/bios_a.bin"})
        cells = re.split(r"(?<!\\)\|", row)[1:-1]
        self.assertEqual(len(cells), len(generate_site.PLATFORM_FILE_HEADER[0].split("|")) - 2)
        name, folder, status, short, details = (cell.strip() for cell in cells)
        self.assertEqual(name, r"bios\_a.bin")
        # The folder: the name is in the cell before it.
        self.assertEqual(folder, "bios/")
        self.assertEqual(status, '<span class="rb-badge rb-badge-success">OK</span>')
        # The hash the platform checks, short, on a button that copies it whole.
        self.assertIn(f'data-copy="{md5}"', short)
        self.assertIn(f">{md5[:8]}</button>", short)
        # The accessible name holds the visible text (WCAG 2.5.3).
        self.assertIn(f'aria-label="Copy MD5 {md5[:8]}"', short)
        # Every hash whole in the details, with the size.
        for value in (md5, sha1, "0badf00d"):
            self.assertIn(value, details)
        self.assertIn("2.0 KB", details)
        self.assertTrue(details.startswith('<details class="rb-row-details">'
                                           '<summary aria-label="Details of bios_a.bin">Details</summary>'))

    def test_status_words(self):
        cases = {
            ("missing", ""): ("Missing", "danger"),
            ("untested", "declared hash contradicted by the local file"): ("Wrong hash", "warning"),
            ("untested", "expected 1234... got 5678..."): ("Wrong hash", "warning"),
            ("untested", "bios/x read error"): ("Untested", "warning"),
        }
        for (status, reason), (word, tone) in cases.items():
            row = self._row({"name": "x", "status": status, "reason": reason}, {"md5": "c" * 32})
            self.assertIn(f'<span class="rb-badge rb-badge-{tone}">{word}</span>', row)
            if reason:
                self.assertIn(f"Not confirmed: {reason}", row)

    def test_accepted_values_all_in_the_details(self):
        values = ["1" * 32, "2" * 32, "3" * 32]
        row = self._row({"name": "x", "status": "ok"}, {"md5": ",".join(values)})
        self.assertIn(f'data-copy="{values[0]}"', row)
        self.assertIn("</button> +2", row)
        for value in values:
            self.assertIn(f'<code class="rb-hash">{value}</code>', row)

    def test_a_destination_under_another_name_stays_whole(self):
        row = self._row({"name": "a.bin", "status": "ok"}, {"destination": "x/b.bin"})
        self.assertIn("| a.bin | x/b.bin |", row)

    def test_a_pipe_does_not_end_the_cell(self):
        row = self._row({"name": "a|b", "status": "ok"}, {})
        self.assertIn(r"a\|b", row)
        self.assertEqual(len(re.split(r"(?<!\\)\|", row)), 7)


class ReferencesNameTheirRoleOnce(unittest.TestCase):
    PROFILE = {"type": "libretro", "source": "https://example.org/port",
               "upstream": "https://example.org/emu"}
    REFS = "source libretro/libretro.c:1, source src/a.c:2, source src/b.c:3, upstream src/c.c:4"

    def test_grouped_by_role(self):
        text = generate_site._refs_by_role(self.PROFILE, self.REFS)
        self.assertEqual(text, "glue `libretro/libretro.c:1` and port `src/a.c:2`, `src/b.c:3` "
                               "and upstream `src/c.c:4`")
        for word in generate_site.ROLE_WORDS.values():
            self.assertIn(f"**{word}**", generate_site.ROLE_LEGEND)

    def test_other_pages_keep_the_role_after_each_reference(self):
        text = generate_site._source_ref_markdown(self.PROFILE, self.REFS)
        self.assertEqual(text.count("(emulator code, libretro port)"), 2)
        self.assertIn("`src/c.c:4` (upstream emulator)", text)


def _unescaped(text: str) -> str:
    return re.sub(r"\\([\\`*_\[\]~|])", r"\1", text)


@unittest.skipUnless((REPO_ROOT / "database.json").is_file(), "database.json absent")
class NothingIsLost(unittest.TestCase):
    """Every fact a file row carried is on the page or its parts: each
    declared hash whole, the served copy, every reader's references."""

    @classmethod
    def setUpClass(cls) -> None:
        from common import load_database
        from generate_readme import compute_coverage

        db = load_database(str(REPO_ROOT / "database.json"))
        cls.pages = {}
        for name in ("batocera", "retroarch"):
            cov = compute_coverage(name, str(REPO_ROOT / "platforms"), db)
            moved: dict[str, str] = {}
            page = generate_site.generate_platform_page(name, cov, {}, {}, moved)
            cls.pages[name] = (cov, page, moved)

    def test_every_fact_is_there(self):
        for name, (cov, page, moved) in self.pages.items():
            text = _unescaped("\n".join([page, *moved.values()]))
            systems = {d.get("system") for d in cov["details"]}
            for sys_id, system in cov["config"].get("systems", {}).items():
                if sys_id not in systems:
                    continue
                for entry in system.get("files", []):
                    for algorithm in ("sha1", "md5", "crc32", "sha256"):
                        for value in re.split(r"[,\s]+", str(entry.get(algorithm) or "")):
                            if value:
                                self.assertIn(value, text, (name, entry.get("name"), algorithm))
            for detail in cov["details"]:
                self.assertIn(detail["name"], text)
                if detail.get("path") and detail.get("status") != "missing":
                    self.assertIn(detail["path"], text, (name, detail["name"]))
                readers = (detail.get("category_evidence") or {}).get("read_by", [])
                # Cores of other systems that share the name are named, not
                # cited, when a core of this system reads the file.
                running = [reader for reader in readers if reader.get("runs_system")]
                for reader in running or readers:
                    for url in re.findall(r"\]\((https://[^)]+)\)", generate_site._category_evidence_md(
                            {"read_by": [reader]})):
                        self.assertIn(url, text, (name, detail["name"], reader["emulator"]))

    def test_the_role_is_a_word_defined_once(self):
        for name, (_cov, page, moved) in self.pages.items():
            for text in (page, *moved.values()):
                self.assertNotIn("(emulator code, libretro port)", text, name)
                self.assertEqual(text.count(generate_site.ROLE_LEGEND), 1, name)


# Visible words (outside closed details) measured on the built site,
# 2026-10-27. A ceiling is the measured value plus 5 %, never above the
# value before the rows were tables (Batocera 2,433, AmberELEC 1,301, RomM
# 1,607, RetroArch 2,207, the largest main page 2,459, the largest part
# 19,127: RetroDECK's arcade list).
PAGE_CEILINGS = {"batocera": 2433, "amberelec": 1301, "romm": 1607, "retroarch": 2192}
MAIN_PAGE_CEILING = 2459
PART_PAGE_CEILING = 2306

_SITE = REPO_ROOT / "website" / "dist" / "platforms"


@unittest.skipUnless(_SITE.is_dir(), "website/dist not built")
class VisibleWordsStayUnderTheirCeiling(unittest.TestCase):
    def test_platform_pages(self):
        from common import list_registered_platforms

        mains = set(list_registered_platforms(str(REPO_ROOT / "platforms"), include_archived=True))
        for page in sorted(_SITE.glob("*/index.html")):
            name = page.parent.name
            visible, _total = page_words(page.read_text(encoding="utf-8"))
            ceiling = PAGE_CEILINGS.get(name, MAIN_PAGE_CEILING if name in mains else PART_PAGE_CEILING)
            self.assertLessEqual(visible, ceiling, name)


if __name__ == "__main__":
    unittest.main()
