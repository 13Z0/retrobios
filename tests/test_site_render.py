"""Renderings shared by the site pages, and the icon cache they read.

No network: icon checks are answered by a stub.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import siterender  # noqa: E402


class _Served:
    status = 200

    def __enter__(self) -> "_Served":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class IconCacheIsReplacedWhole(unittest.TestCase):
    """The cache was truncated then dumped in place: a concurrent reader saw
    half a file, took it for empty, and an offline run then wrote ``{}``
    over every recorded icon."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "system_icons.json"
        patcher = mock.patch.object(siterender, "ICON_CACHE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = dict(siterender._icon_available)
        self.addCleanup(lambda: (siterender._icon_available.clear(),
                                 siterender._icon_available.update(saved)))

    def test_an_offline_run_leaves_an_unreadable_cache_untouched(self):
        self.path.write_text('{"Sega - Mega Drive": tr')
        offline = mock.patch.object(
            siterender.urllib.request, "urlopen",
            side_effect=urllib.error.URLError("offline"))
        with offline, mock.patch("sys.stderr"), mock.patch("builtins.print"):
            siterender.prime_system_icons({"Sega - Mega Drive"})
        self.assertEqual(self.path.read_text(), '{"Sega - Mega Drive": tr')

    def test_names_another_run_recorded_meanwhile_are_kept(self):
        self.path.write_text(json.dumps({"A - One": True}))

        def answer(*_args: object, **_kwargs: object) -> _Served:
            # A second build records its own check while this one runs.
            self.path.write_text(json.dumps({"A - One": True, "C - Three": False}))
            return _Served()

        with mock.patch.object(siterender.urllib.request, "urlopen", side_effect=answer), \
                mock.patch("builtins.print"):
            siterender.prime_system_icons({"A - One", "B - Two"})
        self.assertEqual(json.loads(self.path.read_text()),
                         {"A - One": True, "B - Two": True, "C - Three": False})
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["system_icons.json"])

    def test_the_write_goes_through_a_scratch_file(self):
        with mock.patch.object(siterender.urllib.request, "urlopen",
                               return_value=_Served()), \
                mock.patch.object(siterender, "write_text_atomic",
                                  wraps=siterender.write_text_atomic) as atomic, \
                mock.patch("builtins.print"):
            siterender.prime_system_icons({"B - Two"})
        atomic.assert_called_once()


class EveryPageAddressesPlatformsAsPeers(unittest.TestCase):
    """The exports page was held to neutral wording, and the gaps page next
    to it still called each platform's file a "corrected list". Reads the
    text every site generator writes, docstrings and bare identifiers aside."""

    WORDS = ("their file", "theirs", "of ours", "corrected", "left out", "drift")
    SOURCES = ("generate_site.py", "siterender.py", "sitesources.py", "siteexports.py")

    def test_no_page_text_judges_another_project(self):
        import ast
        import re

        found = []
        for name in self.SOURCES:
            tree = ast.parse((REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef))
                and ast.get_docstring(node, clean=False) is not None
            }
            for node in ast.walk(tree):
                if (not isinstance(node, ast.Constant) or not isinstance(node.value, str)
                        or id(node) in docstrings or re.fullmatch(r"\w+", node.value)):
                    continue
                text = node.value.lower()
                found += [f"{name}:{node.lineno} {word!r}"
                          for word in self.WORDS if word in text]
        self.assertEqual(found, [])


class BundledFilesAreNotToSource(unittest.TestCase):
    """The gaps page summed the report's gap_missing, bundled files included:
    1,356 files to source where 609 were, and a red badge of 13 on ZQuest
    Classic, whose 13 missing files all come with its own package."""

    def test_the_summary_and_the_row_count_only_what_must_be_found(self):
        import generate_site

        gap = {"required": True, "source_ref": "x", "in_repo": False}
        report = {"zq": {
            "emulator": "ZQuest Classic", "gaps": 4, "gap_missing": 3, "gap_bios": 1,
            "unsourceable": [],
            "gap_details": [
                {**gap, "name": "a.dll", "source": "missing", "bundled": True},
                {**gap, "name": "b.dll", "source": "missing", "bundled": True},
                {**gap, "name": "c.bin", "source": "missing"},
                {**gap, "name": "d.bin", "source": "bios", "in_repo": True},
            ],
        }}
        page = generate_site.generate_gap_analysis(
            {"zq": {"emulator": "ZQuest Classic", "files": []}}, {},
            {"files": {}, "indexes": {}}, gap_report=report)
        self.assertIn("2 installed with their emulator, 1 to source.", page)
        self.assertIn("| missing | 1 | Not available, needs sourcing |", page)
        self.assertIn("| [ZQuest Classic](emulators/zq.md) | 4 | 1 | 0 | 0 | 2 | "
                      '<span class="rb-badge rb-badge-danger">1</span> |', page)


class StructuredBlocks(unittest.TestCase):
    PIN = "1e0de94dc7e669c0925a22c0fccf6cdc837af0a0"

    def profile(self, block: str, value: object) -> dict:
        return {"source": "https://github.com/libretro/blastem",
                "source_commit": self.PIN, block: value}

    def render(self, profile: dict) -> str:
        import generate_site

        return "\n".join(generate_site._render_structured_blocks(profile))

    def test_a_source_ref_in_a_block_is_a_pinned_link(self):
        """186 citations in analysis and firmware blocks were plain code."""
        page = self.render(self.profile("analysis", {"tmss": {
            "source_ref": "libblastem.c:355-359"}}))
        self.assertIn(
            f"[`libblastem.c:355-359`](https://github.com/libretro/blastem/blob/"
            f"{self.PIN}/libblastem.c#L355-L359)", page)

    def test_a_list_inside_a_list_item_is_shown(self):
        """cemu's firmware titles lost the files each title carries."""
        page = self.render(self.profile("firmware_titles", [
            {"id": "0005001b-10042400", "name": "Shared fonts",
             "content": ["CafeCn.ttf", "CafeKr.ttf"]}]))
        self.assertIn("    - id: 0005001b-10042400, name: Shared fonts\n", page)
        self.assertIn("        - **content:**", page)
        self.assertIn("            - CafeKr.ttf", page)

    def test_a_folded_note_stays_inside_its_block(self):
        page = self.render(self.profile("analysis", {"tmss": {
            "note": "The core points\nsystem/tmss_path at the system directory."}}))
        self.assertIn("- **note:** The core points system/tmss_path at the", page)
        body = page.split("\n")[1:]
        self.assertEqual([line for line in body if line and not line.startswith("    ")], [])

    def test_a_long_note_is_not_cut(self):
        note = "word " * 80 + "end."
        page = self.render(self.profile("analysis", {"x": {"note": note}}))
        self.assertIn("end.", page)


if __name__ == "__main__":
    unittest.main()
