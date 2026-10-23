"""What the round 4 packages left between them once merged.

A data directory page printed its readers as a Python list, every board
page's breadcrumb read "Arcade / Arcade", the fba2012 section was headed
with its folder name, BizHawk's tile showed a GitHub avatar and MinUI's a
logo that is not its own, and the nodes the search gate starts wrote
Node's compile cache into /tmp.
"""

from __future__ import annotations

import glob
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_freshness  # noqa: E402
import generate_site as gs  # noqa: E402
import siterender  # noqa: E402

DIST = REPO_ROOT / "website" / "dist"


def _profile(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "emulators" / f"{name}.yml").read_text(encoding="utf-8"))


class DataTreeReaders(unittest.TestCase):
    def test_read_by_names_the_emulators(self):
        ref = "y-sys"
        gs._DATA_MEMBERS[ref] = {"a.bin": {"size": 1, "sha1": "0" * 40}}
        gs._DATA_READERS[ref] = [("dolphin", "Dolphin"), ("ishiiruka", "Ishiiruka")]
        try:
            page = gs._data_tree_page(ref, {}, [("y-sys", ["a.bin"])], 0)
        finally:
            del gs._DATA_MEMBERS[ref], gs._DATA_READERS[ref]
        self.assertIn("Read by [Dolphin](dolphin.md#data-directories) and "
                      "[Ishiiruka](ishiiruka.md#data-directories).", page)
        self.assertNotIn("('dolphin'", page)


class EngineIntros(unittest.TestCase):
    def test_every_intro_names_what_it_emulates(self):
        for path in sorted(glob.glob(str(REPO_ROOT / "emulators" / "*.yml"))):
            profile = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            if not isinstance(profile, dict) or profile.get("type") in ("alias", "test"):
                continue
            name = Path(path).stem
            with self.subTest(profile=name):
                intro = gs._emulator_intro(str(profile.get("emulator") or name), profile)
                self.assertNotRegex(intro, r"emulates\s*(?:\.|game\b|and\b|,)")
                self.assertNotIn("  ", intro)


class SectionNames(unittest.TestCase):
    def test_fba2012_is_headed_by_its_program(self):
        self.assertEqual(gs._section_label("Arcade", "fba2012"), "FB Alpha 2012")
        self.assertEqual(gs._section_label("Arcade", "Arcade"), gs.OTHER_ARCADE_LABEL)
        self.assertEqual(gs._section_label("Sony", "PlayStation"), "PlayStation")
        self.assertEqual(siterender.SECTION_LABELS[("Arcade", "fba2012")],
                         _profile("fbalpha2012")["emulator"])


class PlatformLogos(unittest.TestCase):
    def setUp(self):
        self.registry = yaml.safe_load(
            (REPO_ROOT / "platforms" / "_registry.yml").read_text(encoding="utf-8"))["platforms"]

    def test_bizhawk_shows_its_app_icon(self):
        self.assertTrue(self.registry["bizhawk"]["logo"].endswith(
            "/src/BizHawk.Client.EmuHawk/images/logo.ico"))

    def test_minui_has_no_logo_and_no_stale_copy(self):
        self.assertNotIn("logo", self.registry["minui"])
        self.assertEqual(list((REPO_ROOT / "docs_assets" / "logos").glob("minui.*")), [])

    def test_every_logo_copy_has_a_registry_logo(self):
        sources = json.loads((REPO_ROOT / "docs_assets" / "logos" / "sources.json").read_text())
        for entry in sources["images"]:
            with self.subTest(platform=entry["platform"]):
                self.assertEqual(self.registry[entry["platform"]].get("logo"), entry["source"])


class NodeCompileCache(unittest.TestCase):
    def test_the_search_gate_starts_node_without_it(self):
        source = (REPO_ROOT / "website" / "tools" / "search_gate.py").read_text(encoding="utf-8")
        self.assertIn('NODE_ENV = {**os.environ, "NODE_DISABLE_COMPILE_CACHE": "1"}', source)
        starts = [m.start() for m in re.finditer(r'subprocess\.(?:run|Popen)\(\s*\["node"', source)]
        self.assertEqual(len(starts), 3)
        for start in starts:
            call = source[start:source.index("\n\n", start)]
            self.assertIn("env=NODE_ENV", call)

    def test_lighthouse_and_the_deploy(self):
        self.assertIn('"NODE_DISABLE_COMPILE_CACHE": "1"',
                      (REPO_ROOT / "website" / "tools" / "lighthouse.py").read_text(encoding="utf-8"))
        workflow = (REPO_ROOT / ".github" / "workflows" / "deploy-site.yml").read_text(encoding="utf-8")
        build = workflow[workflow.index("  build:"):workflow.index("    steps:")]
        self.assertIn('NODE_DISABLE_COMPILE_CACHE: "1"', build)


class SourcePathsFreshness(unittest.TestCase):
    def test_the_committed_record_is_current(self):
        from common import load_emulator_profiles  # noqa: PLC0415

        profiles = load_emulator_profiles(str(REPO_ROOT / "emulators"), skip_aliases=False)
        finding = check_freshness.check_source_paths(profiles)
        self.assertEqual(finding.status, check_freshness.OK, finding.detail)

    def test_a_moved_pin_is_stale(self):
        profile = _profile("geolith")
        moved = dict(profile, source_commit="f" * 40, upstream_commit="e" * 40)
        finding = check_freshness.check_source_paths({"geolith": moved})
        self.assertEqual(finding.status, check_freshness.STALE)
        self.assertIn("sitesources.py --fetch", finding.detail)


@unittest.skipUnless((DIST / "systems" / "arcade-acclaim-zn-1" / "index.html").is_file(),
                     "site not built")
class BuiltCrumbs(unittest.TestCase):
    def test_no_crumb_repeats_the_one_before(self):
        repeated = []
        for path in sorted(DIST.rglob("index.html")):
            nav = re.search(r'<nav[^>]*rb-breadcrumbs[^>]*>(.*?)</nav>',
                            path.read_text(encoding="utf-8"), re.S)
            if not nav:
                continue
            labels = [re.sub(r"<[^>]+>", "", item).strip()
                      for item in re.findall(r"<li[^>]*>(.*?)</li>", nav[1], re.S)]
            if any(a == b for a, b in zip(labels, labels[1:])):
                repeated.append(str(path.relative_to(DIST)))
        self.assertEqual(repeated, [])

    def test_a_board_page_ends_on_the_arcade_page(self):
        text = (DIST / "systems" / "arcade-acclaim-zn-1" / "index.html").read_text(encoding="utf-8")
        nav = re.search(r'<nav[^>]*rb-breadcrumbs[^>]*>(.*?)</nav>', text, re.S)[1]
        self.assertIn('href="/retrobios/systems/arcade/"', nav)
        self.assertNotIn('href="/retrobios/systems/#arcade"', nav)


if __name__ == "__main__":
    unittest.main()
