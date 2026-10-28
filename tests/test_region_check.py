"""region_check: the variants a region game runs under and what their
records settle."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import region_check as rc  # noqa: E402

PROFILE = {
    "systems": ["sony-playstation"],
    "files": [
        {"name": "scph5500.bin", "region": ["japan"]},
        {"name": "scph5501.bin", "region": ["north-america", "latin-america"]},
        {"name": "scph5502.bin", "region": ["europe", "oceania"]},
        {"name": "openbios.bin"},
        {"name": "ps1_rom.bin", "region": ["world"]},
        {"name": "standalone.bin", "region": ["japan"], "mode": "standalone"},
        {"name": "other.bin", "region": ["japan"], "system": "sony-playstation-2"},
    ],
}


def record(rid: str, staged: list[str], opened: list[str], hashes: list, verdict: str = "unknown",
           loaded: bool = True) -> dict:
    return {"id": rid, "variants": [{
        "label": "default", "verdict": verdict, "system_files": {name: "0" * 40 for name in staged},
        "repetitions": [{"loaded": loaded, "files_opened": [{"path": p} for p in opened],
                         "observables": {"frame_hashes": hashes}}]}]}


class Entries(unittest.TestCase):
    def test_only_regional_bios_of_the_system_in_the_libretro_build(self) -> None:
        names = [e["name"] for e in rc.region_entries(PROFILE, "sony-playstation")]
        self.assertEqual(names, ["scph5500.bin", "scph5501.bin", "scph5502.bin"])

    def test_variants_split_by_the_game_region(self) -> None:
        entries = rc.region_entries(PROFILE, "sony-playstation")
        plan = rc.plan_variants(entries, "japan", lambda e: Path("/bios") / e["name"])
        self.assertEqual(sorted(plan["full"]), ["scph5500.bin", "scph5501.bin", "scph5502.bin"])
        self.assertEqual(sorted(plan["own"]), ["scph5500.bin"])
        self.assertEqual(sorted(plan["others"]), ["scph5501.bin", "scph5502.bin"])

    def test_a_file_the_collection_lacks_is_in_no_variant(self) -> None:
        entries = rc.region_entries(PROFILE, "sony-playstation")
        plan = rc.plan_variants(entries, "europe",
                                lambda e: None if e["name"] == "scph5502.bin" else Path(e["name"]))
        self.assertEqual(plan["own"], {})
        self.assertNotIn("scph5502.bin", plan["full"])

    def test_subject_names_core_game_and_variant(self) -> None:
        slug = rc.subject("mednafen_psx", "Puzzle Bobble 2 (Japan)", "own")
        self.assertEqual(slug, "region-mednafen_psx-puzzle-bobble-2-japan-own")
        self.assertRegex(slug, rc.SUBJECT)


class BusyHost(unittest.TestCase):
    def _run(self, codes: list[int], max_wait: float) -> tuple[int, int]:
        import subprocess

        now = [0.0]
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, codes[min(len(calls), len(codes)) - 1], "", "")

        def pause(seconds: float) -> None:
            now[0] += seconds

        proc = rc.probe_when_idle(["probe"], max_wait, clock=lambda: now[0], pause=pause, runner=runner)
        return proc.returncode, len(calls)

    def test_a_refused_run_is_tried_again_once_the_host_settles(self) -> None:
        self.assertEqual(self._run([rc.BUSY, rc.BUSY, 0], 600), (0, 3))

    def test_it_gives_up_after_the_wait(self) -> None:
        code, calls = self._run([rc.BUSY], 60)
        self.assertEqual(code, rc.BUSY)
        self.assertEqual(calls, 60 // rc.POLL_S + 1)

    def test_another_failure_is_not_retried(self) -> None:
        self.assertEqual(self._run([1, 0], 600), (1, 1))


class Outcome(unittest.TestCase):
    ENTRIES = rc.region_entries(PROFILE, "sony-playstation")
    ALL = ["scph5500.bin", "scph5501.bin", "scph5502.bin"]

    def test_locked_bios_by_verdict(self) -> None:
        records = {
            "full": record("f", self.ALL, ["scph5500.bin"], [[0, "a"]], "boots"),
            "own": record("o", ["scph5500.bin"], ["scph5500.bin"], [[0, "a"]], "boots"),
            "others": record("x", self.ALL[1:], [], [[0, "b"]], "bios_error", loaded=False),
            "pack": record("p", ["scph5500.bin"], ["scph5500.bin"], [[0, "a"]], "boots"),
        }
        result = rc.outcome(records, self.ENTRIES, "japan")
        self.assertEqual(result["selected"], ["scph5500.bin"])
        self.assertEqual(result["claim"], "confirmed")
        self.assertEqual((result["own_boots"], result["others_boot"], result["pack_boots"]),
                         (True, False, True))
        self.assertEqual(result["judged_by"], "verdict")
        self.assertEqual(rc.sentence("japan", result),
                         "The Japanese BIOS boots the Japanese game, the other regions' BIOS do not.")

    def test_interim_rule_counts_the_same_frames_as_a_boot_and_nothing_else(self) -> None:
        records = {
            "full": record("f", self.ALL, ["scph5501.bin"], [[0, "a"], [10, "b"]]),
            "own": record("o", ["scph5501.bin"], ["scph5501.bin"], [[0, "a"], [10, "b"]]),
            "others": record("x", ["scph5500.bin", "scph5502.bin"], ["scph5500.bin"], [[0, "a"], [10, "c"]]),
        }
        result = rc.outcome(records, self.ENTRIES, "north-america")
        self.assertEqual(result["judged_by"], "full_run_frames")
        self.assertTrue(result["own_boots"])
        # Other frames are no verdict: a US disc reaches its game on the
        # Japanese BIOS through another licence screen.
        self.assertIsNone(result["others_boot"])
        self.assertIsNone(result["pack_boots"])
        self.assertEqual(rc.sentence("north-america", result),
                         "The US BIOS boots the US game. The other regions' BIOS have no verdict yet.")

    def test_no_interim_answer_when_the_full_run_fails(self) -> None:
        records = {"full": record("f", self.ALL, [], [[0, "a"]], "bios_error"),
                   "own": record("o", ["scph5502.bin"], [], [[0, "a"]])}
        result = rc.outcome(records, self.ENTRIES, "europe")
        self.assertIsNone(result["own_boots"])
        self.assertEqual(result["claim"], "unread")

    def test_a_core_reading_another_region_contradicts_the_claim(self) -> None:
        records = {"full": record("f", self.ALL, ["scph5501.bin"], [[0, "a"]], "boots")}
        self.assertEqual(rc.outcome(records, self.ENTRIES, "japan")["claim"], "contradicted")

    def test_a_core_opening_every_region_file_settles_nothing(self) -> None:
        """PCSX-ReARMed opens the three regional files whatever the disc."""
        records = {"full": record("f", self.ALL, self.ALL, [[0, "a"]], "boots")}
        self.assertEqual(rc.outcome(records, self.ENTRIES, "japan")["claim"], "ambiguous")

    def test_region_free_bios(self) -> None:
        records = {"full": record("f", self.ALL, ["scph5501.bin"], [[0, "a"]], "boots"),
                   "own": record("o", ["scph5502.bin"], [], [[0, "a"]], "boots"),
                   "others": record("x", ["scph5501.bin"], ["scph5501.bin"], [[0, "a"]], "boots")}
        result = rc.outcome(records, self.ENTRIES, "europe")
        self.assertEqual(rc.sentence("europe", result),
                         "The European BIOS boots the European game, and so do the other regions' BIOS.")

    def test_no_own_run(self) -> None:
        self.assertEqual(rc.sentence("japan", rc.outcome({}, self.ENTRIES, "japan")),
                         "The Japanese game has no run with its own BIOS yet.")


class ContentList(unittest.TestCase):
    def test_every_region_system_has_a_game_per_region_or_says_why(self) -> None:
        """A system whose BIOS a profile ties to JP, US and EU has a game for
        each of the three, or the content list says why it has none."""
        from common import load_emulator_profiles

        import yaml

        data = yaml.safe_load(rc.CONTENT_FILE.read_text(encoding="utf-8"))
        content = rc.load_content()
        uncovered = data.get("uncovered") or {}
        profiles = load_emulator_profiles(str(ROOT / "emulators"))
        regional: set[str] = set()
        for profile in profiles.values():
            for system in profile.get("systems") or ():
                regions = {r for e in rc.region_entries(profile, system) for r in rc.regions_of(e)}
                if {"japan", "north-america", "europe"} <= regions:
                    regional.add(system)
        for system in sorted(regional):
            if system in uncovered:
                continue
            held = {item.get("region") for item in content.get(system, [])}
            missing = {"japan", "north-america", "europe"} - held
            # One cartridge sold in two territories counts for both.
            missing -= {r for item in content.get(system, []) for r in item.get("also_regions") or ()}
            self.assertFalse(missing, f"{system}: no game for {sorted(missing)}")


class RequiredFromTheRegionRuns(unittest.TestCase):
    def test_beetle_psx_boots_each_region_without_its_bios(self) -> None:
        """libretro.c maps its built-in OpenBIOS when the region's file is
        absent: the evidence of required: false is a record where the file
        was not staged and the oracle judged the run a boot."""
        import json

        import yaml

        profile = yaml.safe_load((ROOT / "emulators" / "beetle_psx.yml").read_text(encoding="utf-8"))
        records = {}
        for line in (ROOT / "measurements" / "beetle_psx.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            records[record["id"]] = record
        regional = [e for e in profile["files"] if e["name"] in ("scph5500.bin", "scph5501.bin", "scph5502.bin")]
        self.assertEqual(len(regional), 3)
        for entry in regional:
            with self.subTest(entry=entry["name"]):
                self.assertIs(entry["required"], False)
                self.assertIn("3425-3511", entry["source_ref"])
                ids = entry["evidence"]["required"]
                self.assertTrue(ids)
                for rid in ids:
                    record = records[rid]
                    self.assertEqual(record["record_version"], 4)
                    variant = record["variants"][0]
                    self.assertNotIn(entry["name"], variant["system_files"])
                    self.assertEqual(variant["verdict"], "boots")
                    self.assertIn("game", variant["verdict_evidence"].get("stages", []))


class SitePages(unittest.TestCase):
    def test_region_section_links_every_record(self) -> None:
        import sitemeasurements as sm

        records = {"full": record("f", Outcome.ALL, ["scph5500.bin"], [[0, "a"]], "boots"),
                   "own": record("o", ["scph5500.bin"], ["scph5500.bin"], [[0, "a"]], "boots"),
                   "others": record("x", Outcome.ALL[1:], [], [[0, "b"]], "bios_error")}
        result = rc.outcome(records, Outcome.ENTRIES, "japan")
        result.update(core="mednafen_psx", game="Puzzle Bobble 2 (Japan)",
                      sentence=rc.sentence("japan", result))
        for label, item in result["variants"].items():
            item["url"] = f"https://example.org/{label}"
        text = "\n".join(sm.region_section([result]))
        self.assertIn("- **mednafen&#95;psx**: Each region's game boots on its own BIOS only.", text)
        self.assertIn("The Japanese BIOS boots the Japanese game, the other regions&#x27; BIOS do not.", text)
        # The per-game lines and their records wait in a closed fold.
        self.assertIn('<details class="rb-region-runs">', text)
        self.assertIn('<a href="https://example.org/own">its own region&#x27;s BIOS only</a>', text)
        self.assertIn("<code>scph5500.bin</code>", text)
        self.assertNotIn("reference frames are recorded", text)
        self.assertEqual(sm.region_section([]), [])

    def test_the_emulator_page_folds_region_runs_into_one_line(self) -> None:
        import measurement_records as mr
        import sitemeasurements as sm

        stored = mr.read_records(ROOT)
        index = sm.build_index(stored, {"repo": sm.REPO, "commit": "0" * 40}, sm.load_profiles(ROOT / "emulators"))
        items = sm.by_emulator(index).get("beetle_psx") or []
        regional = [i for i in items if sm.REGION_SUBJECT.match(i["subject"])]
        if not regional:
            self.skipTest("no region record")
        text = "\n".join(sm.tested_section("beetle_psx", items))
        self.assertIn(f"Region check: {len(regional)} runs of 3 games, read on the system page.", text)
        for item in regional:
            self.assertNotIn(item["record_url"], text)

    def test_coverage_names_tested_and_uncovered_systems(self) -> None:
        import sitemeasurements as sm

        cov = {"systems": [{"system": "sony-playstation", "tested": True, "games": [
                   {"name": "A (USA)", "region": "north-america", "emulators": ["beetle_psx"]}]},
                           {"system": "sega-32x", "tested": False, "games": [
                   {"name": "B (World)", "region": None, "emulators": []}]}],
               "uncovered": [{"system": "msx", "want": "Gradius", "reason": "No MSX catalog.",
                              "romfetch": {"exit": 1, "code": "unknown_system"}}]}
        text = "\n".join(sm.coverage_section(cov, {"sony-playstation": "PlayStation"}))
        self.assertIn("1 of 2 listed systems have a recorded run, and 1 system has no test game", text)
        self.assertIn("| PlayStation | A (USA) | beetle&#95;psx |", text)
        self.assertIn("| sega-32x | B (World) | not yet: no run recorded |", text)
        self.assertIn("exit 1, <code>unknown&#95;system</code>", text)

    def test_one_catalog_name_on_two_systems_counts_only_where_it_ran(self) -> None:
        import measurement_records as mr
        import sitemeasurements as sm

        label = "Bust-A-Move 2 - Arcade Edition (USA)"
        stored = [mr.Stored("beetle_psx", 1, {"inputs": {"content": {"label": label}}})]
        cov = sm.coverage(ROOT, stored, {"beetle_psx": {"systems": ["sony-playstation"]}})
        by_system = {item["system"]: item for item in cov["systems"]}
        self.assertTrue(by_system["sony-playstation"]["tested"])
        self.assertFalse(by_system["sega-saturn"]["tested"])

    def test_coverage_reads_the_real_content_list(self) -> None:
        import sitemeasurements as sm

        cov = sm.coverage(ROOT, [])
        systems = {item["system"] for item in cov["systems"]}
        self.assertIn("sony-playstation", systems)
        self.assertTrue(all(item["romfetch"].get("exit") for item in cov["uncovered"]))


if __name__ == "__main__":
    unittest.main()
