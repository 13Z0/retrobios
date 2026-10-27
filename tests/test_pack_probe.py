"""pack_probe.py tests a platform's pack the way a user installs it.

The rules (which cores a platform runs, what is staged, how a run maps back
to the pack, the verdict read from a record, the line written) are checked
on synthetic data. Two classes lay a real BIOS folder out from this
checkout, one per install path: the pack generate_pack.py builds, extracted,
and install.py --dest from that pack and the published manifest. They are
narrowed to one system to stay short, and skip when the collection is not
on disk.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import measurement_records as mr  # noqa: E402
import pack_probe as pp  # noqa: E402

TMP = REPO_ROOT / "tmp" / "tests" / "pack_probe"

SATURN = {
    "type": "libretro",
    "cores": ["mednafen_saturn", "beetle-saturn"],
    "systems": ["sega-saturn"],
    "files": [
        {"name": "sega_101.bin", "region": ["japan"]},
        {"name": "mpr-17933.bin", "aliases": ["saturn_bios.bin"]},
        {"name": "standalone.bin", "mode": "standalone"},
    ],
}
SCANNER = {
    "type": "libretro",
    "cores": ["swanstation"],
    "systems": ["sony-playstation"],
    "found_by": "content",
    "files": [{"name": "scph5501.bin", "size": 524288},
              {"name": "ps2.bin", "size": [524288, 4194304]}],
}
KRONOS = {
    "type": "libretro",
    "cores": ["kronos"],
    "systems": ["sega-saturn"],
    "files": [{"name": "saturn_bios.bin", "path": "kronos/saturn_bios.bin"}],
}


def layout(files: dict[str, int], entries: dict[str, str | None] | None = None) -> pp.Layout:
    """A layout whose sizes are given, nothing on disk."""
    return pp.Layout("pack", Path("/nonexistent"), entries or {rel: rel for rel in files}, {},
                     sizes=dict(files))


class CoreChoice(unittest.TestCase):
    def test_system_ids_compare_normalised(self) -> None:
        config = {"systems": {"megacd": {}, "psx": {}}}
        self.assertEqual(pp.platform_system(config, "sega-mega-cd"), "megacd")
        self.assertIsNone(pp.platform_system(config, "sega-saturn"))

    def test_standalone_only_is_not_run_as_a_core(self) -> None:
        both = {"type": "standalone + libretro", "cores": ["duckstation", "duckstation-sa"]}
        self.assertTrue(pp.libretro_build_runs("duckstation", both, {}))
        self.assertFalse(pp.libretro_build_runs("duckstation", both, {"standalone_cores": ["duckstation"]}))
        self.assertTrue(pp.libretro_build_runs("duckstation", both, {
            "standalone_cores": ["duckstation"], "dual_mode_cores": ["duckstation-sa"]}))
        self.assertFalse(pp.libretro_build_runs("x", {"type": "standalone"}, {}))
        self.assertFalse(pp.libretro_build_runs("x", {"type": "alias"}, {}))

    def test_buildbot_name(self) -> None:
        buildbot = {"mednafen_psx", "mednafen_psx_hw", "kronos"}
        beetle = {"cores": ["beetle_psx", "mednafen_psx", "mednafen_psx_hw"]}
        # The target names both builds: both run.
        self.assertEqual(pp.so_names("beetle_psx", beetle, buildbot, {"mednafen_psx", "mednafen_psx_hw"}),
                         ["mednafen_psx", "mednafen_psx_hw"])
        # A target in the platform's own names: the first buildbot name.
        self.assertEqual(pp.so_names("beetle_psx", beetle, buildbot, {"beetle-psx"}), ["mednafen_psx"])
        self.assertEqual(pp.so_names("kronos", {"cores": ["kronos"]}, buildbot, None), ["kronos"])
        self.assertEqual(pp.so_names("gone", {"cores": ["gone"]}, buildbot, None), [])

    def test_choose_cores(self) -> None:
        profiles = {"beetle_saturn": SATURN, "kronos": KRONOS, "swanstation": SCANNER,
                    "ssf": {"type": "standalone", "cores": ["ssf"], "systems": ["sega-saturn"]}}
        config = {"_key": "p", "cores": ["beetle-saturn", "kronos", "ssf", "swanstation"],
                  "systems": {"saturn": {}}}
        buildbot = {"mednafen_saturn", "kronos", "swanstation"}
        self.assertEqual(pp.choose_cores(config, profiles, "sega-saturn", buildbot, None),
                         [pp.CoreChoice("beetle_saturn", "mednafen_saturn"), pp.CoreChoice("kronos", "kronos")])


class Staging(unittest.TestCase):
    FILES = {"sega_101.bin": 524288, "saturn_bios.bin": 524288, "scph5501.bin": 524288,
             "big.bin": 4194304, "other.rom": 1000, "kronos/saturn_bios.bin": 524288,
             "kronos/stvbios.zip": 10, "mame/hash/saturn.xml": 5, "standalone.bin": 1}

    def test_declared_names_aliases_and_platform_list(self) -> None:
        staged = pp.initial_stage(layout(self.FILES), SATURN, ["other.rom", "absent.bin"])
        self.assertEqual(staged, {"sega_101.bin", "saturn_bios.bin", "other.rom"})

    def test_a_folder_comes_whole(self) -> None:
        staged = pp.initial_stage(layout(self.FILES), KRONOS, [])
        self.assertEqual(staged, {"kronos/saturn_bios.bin", "kronos/stvbios.zip"})

    def test_a_core_that_scans_gets_every_root_file_of_its_sizes(self) -> None:
        staged = pp.initial_stage(layout(self.FILES), SCANNER, [])
        self.assertEqual(staged, {"sega_101.bin", "saturn_bios.bin", "scph5501.bin", "big.bin"})

    def test_grow_to_a_fixed_point(self) -> None:
        staged = {"sega_101.bin"}
        added = pp.grow_stage(self.FILES, staged, ["other.rom", "absent.bin", "mame/hash", "kronos"])
        self.assertEqual(added, {"other.rom", "mame/hash/saturn.xml", "kronos/saturn_bios.bin",
                                 "kronos/stvbios.zip"})
        self.assertEqual(pp.grow_stage(self.FILES, staged | added, ["absent.bin"]), set())


class Mapping(unittest.TestCase):
    def test_opened_files_map_to_their_entry_and_probes_to_findings(self) -> None:
        lay = layout({"sega_101.bin": 1, "BIOS/Saturn_BIOS.bin": 1, "extra.bin": 1},
                     {"sega_101.bin": "sega_101.bin", "BIOS/Saturn_BIOS.bin": "BIOS/Saturn_BIOS.bin",
                      "extra.bin": None})
        rep = {"files_opened": [{"path": "sega_101.bin", "sha1_after": "a" * 40},
                                {"path": "extra.bin", "sha1_after": "b" * 40},
                                {"path": "bkram.bin", "sha1_after": "c" * 40}],
               "files_probed_absent": ["saturn_bios.bin", "mpr-17933.bin", "cfg.ini"]}
        opened, findings = pp.map_run(lay, {"sega_101.bin", "extra.bin"}, rep, SATURN)
        self.assertEqual(opened, [
            {"path": "sega_101.bin", "entry": "sega_101.bin", "sha1": "a" * 40, "created": False},
            {"path": "extra.bin", "entry": None, "sha1": "b" * 40, "created": False},
            {"path": "bkram.bin", "entry": None, "sha1": "c" * 40, "created": True}])
        self.assertEqual(findings, [
            {"kind": "elsewhere", "path": "saturn_bios.bin", "declared_by": ["mpr-17933.bin"],
             "paths": ["BIOS/Saturn_BIOS.bin"]},
            {"kind": "missing_declared", "path": "mpr-17933.bin", "declared_by": ["mpr-17933.bin"], "paths": []},
            {"kind": "missing_undeclared", "path": "cfg.ini", "declared_by": [], "paths": []},
            {"kind": "unlisted", "path": "extra.bin", "declared_by": [], "paths": []}])

    def test_a_name_tried_before_an_alias_that_is_read_is_no_finding(self) -> None:
        profile = {"files": [{"name": "tmss.md", "aliases": ["bios_MD.bin"]}, {"name": "bios_CD_U.bin"}]}
        lay = layout({"bios_MD.bin": 1})
        rep = {"files_opened": [{"path": "bios_MD.bin", "sha1_after": "a" * 40}],
               "files_probed_absent": ["tmss.md", "bios_CD_U.bin"]}
        _, findings = pp.map_run(lay, {"bios_MD.bin"}, rep, profile)
        self.assertEqual(findings, [{"kind": "missing_declared", "path": "bios_CD_U.bin",
                                     "declared_by": ["bios_CD_U.bin"], "paths": []}])

    def test_manifest_entries(self) -> None:
        manifest = {"files": [{"dest": "a.bin"}],
                    "case_variants": [{"dest": "A.BIN"}, {"dest": "a.bin"}],
                    "data_directories": [{"ref": "bluemsx", "dest": "", "files": {"Machines/x.ini": {}}},
                                         {"ref": "dolphin", "dest": "dolphin-emu/Sys/", "files": {"y": {}}}]}
        self.assertEqual(pp.manifest_entries(manifest), {
            "a.bin": "files:a.bin", "A.BIN": "case_variants:A.BIN",
            "Machines/x.ini": "data_directories:bluemsx:Machines/x.ini",
            "dolphin-emu/Sys/y": "data_directories:dolphin:y"})


class Verdict(unittest.TestCase):
    def test_a_record_before_the_oracle_is_unknown(self) -> None:
        self.assertEqual(pp.run_verdict(None), ("unknown", None))
        self.assertEqual(pp.run_verdict({"variants": [{"repetitions": [{}]}]}), ("unknown", None))

    def test_the_first_variant_decides(self) -> None:
        evidence = {"rule": "reference_frame", "detail": "frame 120"}
        record = {"variants": [{"verdict": "boots", "verdict_evidence": evidence, "repetitions": [{}]}]}
        self.assertEqual(pp.run_verdict(record), ("boots", evidence))
        self.assertEqual(pp.run_verdict({"variants": [{"verdict": "maybe"}]}), ("unknown", None))


class Line(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP / "line"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)

    def line(self) -> dict:
        return {
            "schema_version": 1, "probed_at": "2026-10-27T12:00:00+00:00", "platform": "retroarch",
            "platform_version": "v1.22.2", "target": "linux-x86_64", "install": "pack",
            "source": {"pack": {"file": "RetroArch_BIOS_Pack.zip", "sha256": "0" * 64, "size": 1,
                                "members": 1}, "manifest": None, "repo_commit": "1" * 40},
            "system": "sega-saturn", "platform_system": "sega-saturn", "emulator": "beetle_saturn",
            "core": {"file": "mednafen_saturn_libretro.so", "sha256": "2" * 64,
                     "artifact": "https://buildbot.libretro.com/x.zip", "last_modified": None},
            "content": {"label": "Game (Japan)", "catalog": "redump", "region": "japan"},
            "records": ["2026-10-27-0123456789ab"], "rounds": 1, "staged": 3, "verdict": "unknown",
            "verdict_evidence": None,
            "files_opened": [{"path": "sega_101.bin", "entry": "sega_101.bin", "sha1": "3" * 40,
                              "created": False}],
            "findings": [{"kind": "missing_declared", "path": "x.bin", "declared_by": ["x.bin"], "paths": []}],
            "error": None,
        }

    def test_the_line_is_sealed_valid_and_read_back(self) -> None:
        sealed = pp.append_line(self.line(), self.root)
        self.assertEqual(mr.read_campaign(self.root, mr.PACK_PROBES), [sealed])
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is a tooling dependency")
        schema = json.loads((REPO_ROOT / "schemas" / "pack-probe.schema.json").read_text())
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(sealed)), [])

    def test_the_cli_refuses_an_unknown_platform(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            pp.build_parser().parse_args(["run", "--platform", "nowhere"])


class SitePages(unittest.TestCase):
    """The Measurements page and the platform pages show the latest probe."""

    def test_latest_line_per_probe_with_its_link(self) -> None:
        import sitemeasurements as sm

        root = TMP / "site"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        first = Line.line(None)
        later = {**first, "probed_at": "2026-10-28T12:00:00+00:00", "verdict": "boots"}
        other = {**first, "platform": "batocera", "install": "installer"}
        for line in (first, later, other):
            pp.append_line(line, root)
        items = sm.pack_summary(root, {"repo": "https://example.org/r", "commit": "c" * 40})
        self.assertEqual([(i["platform"], i["install"], i["verdict"]) for i in items],
                         [("batocera", "installer", "unknown"), ("retroarch", "pack", "boots")])
        self.assertEqual(items[1]["record_url"],
                         f"https://example.org/r/blob/{'c' * 40}/measurements/pack-probes.jsonl#L2")
        self.assertEqual(items[1]["core"], "mednafen_saturn")
        explorer = "\n".join(sm.packs_section(items))
        self.assertIn("| Platform | System |", explorer)
        self.assertIn("| retroarch | sega-saturn | pack | <code>mednafen&#95;saturn</code> | Game (Japan) "
                      "| `boots` | 1 | [line](", explorer)
        page = sm.pack_tested_section(sm.by_platform({"packs": items})["retroarch"])
        text = "\n".join(page)
        self.assertEqual(page[0], "## Tested")
        self.assertIn("1 game start with this BIOS folder as you install it · 1 work · last run 2026-10-28.",
                      text)
        self.assertIn("wiki/measurements/#packs", text)
        # The emulator pages' row markup and chip, the install path in the third column.
        self.assertIn('<span class="rb-tested-result"><span class="rb-badge rb-badge-success">works</span>',
                      text)
        self.assertIn('<span class="rb-tested-variants">pack</span>', text)
        self.assertIn("sega-saturn, Game <small>mednafen&#95;saturn</small>", text)
        self.assertIn("<strong>Files the core opened:</strong> sega&#95;101.bin (sega&#95;101.bin).", text)
        self.assertIn("<strong>Findings:</strong> missing&#95;declared x.bin.", text)
        self.assertNotIn("| retroarch |", text)

    def test_pack_runs_stay_off_the_emulator_page(self) -> None:
        import sitemeasurements as sm

        subject = pp.subject_for("retroarch", "pack", "sega-saturn", "mednafen_saturn", "Game (Japan)")
        self.assertEqual(subject, "pack-retroarch-pack-sega-saturn-mednafen-saturn-game-japan")
        index = {"items": [{"emulator": "beetle_saturn", "subject": subject},
                           {"emulator": "beetle_saturn", "subject": "boot-sega-101-bin"}]}
        self.assertEqual([i["subject"] for i in sm.by_emulator(index)["beetle_saturn"]],
                         ["boot-sega-101-bin"])

    def test_run_output_is_inert(self) -> None:
        """A file name or log line from a third-party core reaches the page
        as text: no tag, link, code span or MDX expression comes out of it."""
        import sitemarkup
        import sitemeasurements as sm

        hostile = "`x` {alert(1)} <script>alert(1)</script> [x](javascript:alert(1)) \"q\" 'q' **b**"
        item = {"platform": "retroarch", "platform_version": "1", "install": "pack",
                "system": hostile, "emulator": hostile, "core": hostile, "content": hostile,
                "verdict": "boots", "date": "2026-10-28T00:00:00+00:00", "opened": 1, "findings": 1,
                "id": "2026-10-28-0123456789ab", "record_url": "https://example.org/r#L1",
                "files": [hostile], "finding_list": [hostile], "detail": hostile}
        text = "\n".join(sm.pack_tested_section([item]))
        for raw in ("<script", "{alert", "`x`", "[x](", "<code>", "<strong>b", "**b",
                    "href=\"javascript", '"q"', "'q'"):
            self.assertNotIn(raw, text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&#123;alert(1)&#125;", text)
        # A Markdown link in other site text keeps only http(s) and relative targets.
        self.assertEqual(sitemarkup.inline_html("[x](javascript:alert(1))"), "[x](javascript:alert(1))")
        self.assertEqual(sitemarkup.inline_html('[x](https://a.org/"onmouseover=1)'),
                         '<a href="https://a.org/&quot;onmouseover=1">x</a>')
        self.assertEqual(sitemarkup.inline_html("[x](../a.md)"), '<a href="../a.md">x</a>')
        self.assertEqual(sm.pack_tested_section([]), [])


class Lock(unittest.TestCase):
    """The probe lock is held for one core_probe run and released after it."""

    def test_held_for_each_run_only(self) -> None:
        import fcntl
        import subprocess
        import unittest.mock

        work = TMP / "lock"
        shutil.rmtree(work, ignore_errors=True)
        lock = work / "probe.lock"
        held = []

        def try_lock() -> bool:
            with lock.open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(handle, fcntl.LOCK_UN)
                return False

        def fake_run(command, **kwargs):
            held.append(try_lock())
            self.assertEqual(kwargs["env"]["RETROBIOS_PROBE_LOCK_HELD"], "1")
            self.assertEqual(kwargs["env"]["RETROBIOS_PROBE_LOCK"], str(lock))
            return subprocess.CompletedProcess(command, 2, "", "refused")

        lay = layout({"a.bin": 1})
        with unittest.mock.patch.object(pp, "PROBE_LOCK", lock), \
                unittest.mock.patch.object(pp.subprocess, "run", fake_run):
            for _ in range(2):
                self.assertEqual(pp.core_probe(["run"], {"a.bin"}, lay, work), (2, None, "refused"))
        self.assertEqual(held, [True, True])
        self.assertFalse(try_lock())


def collection_on_disk() -> bool:
    return (REPO_ROOT / "bios" / "Sony" / "PlayStation" / "scph5501.bin").is_file()


@unittest.skipUnless(collection_on_disk(), "the collection is not on disk")
class RealLayouts(unittest.TestCase):
    """Both install paths, from this checkout, narrowed to PlayStation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.work = TMP / "real"
        shutil.rmtree(cls.work, ignore_errors=True)
        cls.pack = pp.build_pack("retroarch", cls.work / "dist", ["--system", "sony-playstation"])
        cls.identity = pp.pack_identity(cls.pack)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_pack_path(self) -> None:
        lay = pp.pack_layout(self.pack, self.work / "pack", self.identity)
        with zipfile.ZipFile(self.pack) as handle:
            members = {i.filename for i in handle.infolist() if not i.is_dir()}
        self.assertEqual(set(lay.files), members)
        self.assertEqual(lay.files["scph5501.bin"], "scph5501.bin")
        self.assertEqual(lay.source["pack"]["members"], len(members))
        swan = pp.load_emulator_profiles(str(REPO_ROOT / "emulators"))["swanstation"]
        self.assertIn("scph5501.bin", pp.initial_stage(lay, swan, []))

    def test_installer_path(self) -> None:
        lay = pp.installer_layout("retroarch", self.pack, self.identity,
                                  REPO_ROOT / "install" / "retroarch.json", self.work / "installer",
                                  ["--system", "sony-playstation"])
        self.assertEqual(lay.root, self.work / "installer" / "system")
        self.assertEqual(lay.files["scph5501.bin"], "files:scph5501.bin")
        self.assertEqual(lay.source["manifest"]["path"], "install/retroarch.json")
        self.assertTrue(all(entry for entry in lay.files.values()), lay.files)


if __name__ == "__main__":
    unittest.main()
