"""Round 5, package 2: components and runtime.

Source checks always run. Checks on the built site read website/dist and
skip when no build is there. The rules written in a build plugin run under
node and skip without it.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import list_release_pack_platforms  # noqa: E402

WEBSITE = REPO_ROOT / "website"
DIST = WEBSITE / "dist"
COMPONENTS = WEBSITE / "src" / "components"
SITE_TS = WEBSITE / "src" / "scripts" / "site.ts"
PLUGINS = WEBSITE / "src" / "plugins"
SHORT_CODE = PLUGINS / "rehype-short-code.mjs"
STACK_TABLES = PLUGINS / "rehype-stack-tables.mjs"
ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z]+);", re.I)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _built(page: str) -> str:
    path = DIST / page / "index.html"
    if not path.is_file():
        raise unittest.SkipTest(f"no built site at {path}")
    return _read(path)


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is missing")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True, cwd=WEBSITE,
        env={"NODE_DISABLE_COMPILE_CACHE": "1", "PATH": str(Path(node).parent)},
    )
    return result.stdout


def _text(page: str) -> str:
    """A built page's visible words: tags dropped, entities read."""
    return html.unescape(re.sub(r"<[^>]+>", "", page))


def _picker_packs() -> set[str]:
    """The setups the built picker offers a release ZIP line for."""
    page = _built("which-pack")
    match = re.search(r'data-release="([^"]*)"', page)
    if not match:
        raise AssertionError("which-pack has no picker release data")
    return set(json.loads(html.unescape(match[1]))["packs"])


class PickerFollowsTheRegistry(unittest.TestCase):
    """2.1: the picker said "The ZIP of release v2026.10.10 holds 5,804
    files" for ROCKNIX while its setup page and get/ page said "No ZIP for
    ROCKNIX" (N2, J5). The picker read the raw release.json."""

    def test_source(self):
        source = _read(COMPONENTS / "FilePicker.astro")
        self.assertIn("import release from '../generated/release-advertised.json';", source)
        self.assertNotIn("release.json'", source)

    def test_picker_offers_only_registry_packs(self):
        offered = _picker_packs()
        self.assertTrue(offered)
        released = set(list_release_pack_platforms(str(REPO_ROOT / "platforms"), include_archived=True))
        self.assertLessEqual(offered, released)

    def test_picker_setup_and_get_pages_agree(self):
        offered = _picker_packs()
        setups = json.loads(_read(DIST / "data" / "picker" / "index.json"))
        self.assertTrue(setups)
        for entry in setups:
            name = entry["platform"]
            with self.subTest(platform=name):
                get_page = _text(_built(f"get/{name}"))
                setup_page = _text(_built(f"platforms/{name}"))
                no_zip = f"No ZIP for {entry['name']}."
                self.assertEqual(name in offered, no_zip not in get_page)
                self.assertEqual(name in offered, no_zip not in setup_page)


class PathsBreakAtSeparators(unittest.TestCase):
    """2.2: `~/.var/app/org.libretro.RetroArch/config/r` then
    `etroarch/system/` on platforms/retroarch/ at 390 px (V8)."""

    def test_the_rule(self):
        uri = json.dumps(SHORT_CODE.as_uri())
        out = _node(
            f"import {{ markRawCode, withBreaks, breaksAtSeparators }} from {uri};"
            "console.log(JSON.stringify(["
            "markRawCode('<td><code>~/.config/retroarch/system/aaaaaaaaaaa/</code> <code>bios/a.bin</code></td>'),"
            "markRawCode('<td><code class=\"rb-path\">C:\\\\RetroArch-Win64\\\\system\\\\aaaaaaaaaaaaa</code></td>'),"
            "withBreaks('a/b/'), breaksAtSeparators('x'.repeat(31), false),"
            "breaksAtSeparators('a/' + 'x'.repeat(30), true), breaksAtSeparators('a/b', false)]));"
        )
        self.assertEqual(json.loads(out), [
            '<td><code>~/<wbr>.config/<wbr>retroarch/<wbr>system/<wbr>aaaaaaaaaaa/</code>'
            ' <code class="rb-short">bios/a.bin</code></td>',
            '<td><code class="rb-path">C:\\<wbr>RetroArch-Win64\\<wbr>system\\<wbr>aaaaaaaaaaaaa</code></td>',
            "a/<wbr>b/", False, False, False,
        ])

    def test_built_page(self):
        page = _built("platforms/retroarch")
        self.assertIn("~/<wbr>.var/<wbr>app/<wbr>org.libretro.RetroArch/<wbr>config/<wbr>retroarch/<wbr>system/", page)

    def test_the_page_budget(self):
        """Each `wbr` is an element: bluemsx held 3,097 of them on 9,369
        elements against a budget of 10,000. The build spends BREAK_BUDGET
        per page, the runtime writes the others."""
        uri = json.dumps(SHORT_CODE.as_uri())
        out = _node(
            f"import {{ markRawCode, BREAK_BUDGET }} from {uri};"
            "const room = { left: 3 };"
            "const path = '<code>aaaaaaaaaa/bbbbbbbbbb/cccccccccc/d</code>';"
            "console.log(JSON.stringify([BREAK_BUDGET, markRawCode(path, room), markRawCode(path, room), room.left]));"
        )
        budget, first, second, left = json.loads(out)
        self.assertEqual(first, "<code>aaaaaaaaaa/<wbr>bbbbbbbbbb/<wbr>cccccccccc/<wbr>d</code>")
        self.assertEqual(second, "<code>aaaaaaaaaa/bbbbbbbbbb/cccccccccc/d</code>")
        self.assertEqual(left, 0)
        if not DIST.is_dir():
            self.skipTest("no built site")
        # Link text the generator breaks itself (a forge address) is not code.
        code = re.compile(r"<code(?:\s[^>]*)?>(?:[^<]|<wbr>)*</code>")
        over = [str(path.relative_to(DIST)) for path in DIST.rglob("index.html")
                if sum(found.count("<wbr>") for found in code.findall(_read(path))) > budget]
        self.assertEqual(over, [])

    def test_the_runtime_writes_the_rest(self):
        source = _read(SITE_TS)
        self.assertIn("const LONG_CODE = 30;", source)
        self.assertIn("const SEPARATORS = /([/\\\\])(?!$)/g;", source)
        self.assertIn("if (unbrokenPath(code)) observer.observe(code);", source)
        self.assertIn("\nbreakPaths();\n", source)
        plugin = _read(SHORT_CODE)
        self.assertIn("export const SHORT_CODE = 30;", plugin)
        self.assertIn("const SEPARATORS = /([/\\\\])(?!$)/g;", plugin)

    def test_every_long_path_breaks_at_run_time(self):
        """Inline code over 30 characters holding a separator: the build
        broke it, or the runtime rule picks it (all text, no `wbr`)."""
        if not DIST.is_dir():
            self.skipTest("no built site")
        unbroken = []
        for path in sorted(DIST.rglob("index.html")):
            page = _read(path)
            if 'http-equiv="refresh"' in page or "sl-markdown-content" not in page:
                continue
            body = re.sub(r"<pre\b.*?</pre>", "", page[page.index("sl-markdown-content"):], flags=re.S)
            for text in re.findall(r"<code(?:\s[^>]*)?>((?:[^<]|<(?!/code>))*)</code>", body):
                plain = ENTITY.sub("_", re.sub(r"<wbr>", "", text))
                if len(plain) > 30 and re.search(r"[/\\]", plain[:-1]) \
                        and "<wbr>" not in text and "<" in text:
                    unbroken.append(f"{path.relative_to(DIST)}: {text}")
        self.assertEqual(unbroken, [])

    def test_crumb_labels_in_a_span(self):
        source = _read(COMPONENTS / "PageTitle.astro")
        self.assertIn("<a href={crumb.href}><span>{crumb.label}</span></a> : <span>{crumb.label}</span>", source)
        crumbs = _built("platforms/retroarch")
        crumbs = crumbs[crumbs.index('class="rb-breadcrumbs'):]
        crumbs = crumbs[:crumbs.index("</ol>")]
        items = re.findall(r"<li\b[^>]*>(.*?)</li>", crumbs)
        self.assertTrue(items)
        for item in items:
            self.assertRegex(item, r"^(?:<a\b[^>]*>)?<span\b[^>]*>[^<]+</span>")


class ShortStackedLabels(unittest.TestCase):
    """2.3: "Name as declared" and "On its BIOS list (destinations)" wrap
    to two lines in the stacked label column (V9)."""

    def test_the_rule(self):
        uri = json.dumps(STACK_TABLES.as_uri())
        out = _node(
            f"import {{ stackedLabel }} from {uri};"
            "console.log(JSON.stringify([stackedLabel('Declared as', 'Name as declared'),"
            "stackedLabel(undefined, 'Setup'), stackedLabel(' ', 'Setup')]));"
        )
        self.assertEqual(json.loads(out), ["Declared as", "Setup", "Setup"])

    def test_plugin_and_runtime_read_the_title(self):
        plugin = _read(STACK_TABLES)
        self.assertIn("stackedLabel(cell.properties?.title, labels[column])", plugin)
        self.assertIn("ctx.setProperty(cell, 'dataLabel', stacked[column])", plugin)
        runtime = _read(SITE_TS)
        self.assertIn("cell.getAttribute('title')?.trim() || (cell.textContent?.trim() ?? '')", runtime)
        self.assertIn("[...table.querySelectorAll('thead th')].map(stackedLabel)", runtime)
        self.assertIn("'Name as declared': 'Declared as'", runtime)
        self.assertIn("cell.dataset.label = stackedLabel(head.cells[column]);", runtime)


class LookupShowsTheDeclaredHash(unittest.TestCase):
    """2.4: with the CRC32 pasted, Batocera's row read "Hash matched: CRC32",
    though Batocera declares an MD5 (J9, N10)."""

    def test_source(self):
        source = _read(SITE_TS)
        self.assertIn("declared?: string;", source)
        self.assertIn("const declaredHash = (item: Item) => (item.declared || item.algorithm).toUpperCase();", source)
        table = source[source.index("const setupTable"):source.index("const distinct")]
        self.assertIn("declaredHash(item),", table)
        self.assertNotIn("item.algorithm", table)


if __name__ == "__main__":
    unittest.main()
