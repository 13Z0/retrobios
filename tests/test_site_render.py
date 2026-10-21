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


if __name__ == "__main__":
    unittest.main()
