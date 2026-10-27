"""campaign.py runs the real-condition campaign: state, guards, content, rules.

No network and no real core. The rules and the profile editor are checked on
synthetic records and profiles, the guards and the probe retries on a fake
host, the content store on folders made in the test. One class builds a
libretro core from C, when a compiler is there, and runs whole passes on a
synthetic checkout: records, rules applied with their evidence, findings,
resume and rerun on a changed core.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import MappingProxyType

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import campaign
import campaign_profiles
import campaign_rules as rules
import measurement_records as mr
import sitemeasurements

TMP = REPO_ROOT / "tmp" / "tests"

PROFILE = """\
emulator: Fake
type: libretro
cores: [fake]
systems: [fake-system]
notes: >
  A profile for the tests.

files:
  # -- the BIOS --

  - name: "bios.bin"
    description: "The BIOS"
    required: true
    size: 4
    validation: [size]
    aliases:
      - "Bios.bin"

  - name: "other.bin"
    required: false
"""

FAKE_CORE = r"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct retro_system_info { const char *n, *v, *e; bool full, block; };
struct retro_game_geometry { unsigned bw, bh, mw, mh; float a; };
struct retro_system_timing { double fps, rate; };
struct retro_system_av_info { struct retro_game_geometry g; struct retro_system_timing t; };
typedef bool (*env_t)(unsigned, void *);
typedef void (*video_t)(const void *, unsigned, unsigned, size_t);
typedef size_t (*batch_t)(const int16_t *, size_t);
typedef int16_t (*input_t)(unsigned, unsigned, unsigned, unsigned);
static env_t env; static video_t video; static batch_t batch;
static uint16_t pixels[4 * 2]; static int booted; static unsigned frame;
void retro_set_environment(env_t e) { bool t = true; env = e; env(18, &t); }
void retro_set_video_refresh(video_t v) { video = v; }
void retro_set_audio_sample(void *f) { (void)f; }
void retro_set_audio_sample_batch(batch_t b) { batch = b; }
void retro_set_input_poll(void *f) { (void)f; }
void retro_set_input_state(input_t f) { (void)f; }
void retro_set_controller_port_device(unsigned port, unsigned device) { (void)port; (void)device; }
void retro_get_system_info(struct retro_system_info *i) {
  i->n = "Fake"; i->v = "1"; i->e = ""; i->full = true; i->block = false; }
void retro_get_system_av_info(struct retro_system_av_info *i) {
  memset(i, 0, sizeof *i); i->t.fps = 60.0; i->t.rate = 44100.0; }
void retro_init(void) {}
void retro_deinit(void) {}
static FILE *try_open(const char *dir, const char *name) {
  char path[4096]; snprintf(path, sizeof path, "%s/%s", dir, name); return fopen(path, "rb"); }
bool retro_load_game(const void *g) {
  const char *dir = NULL; FILE *f; (void)g; env(9, &dir);
  f = try_open(dir, "bios.bin");
  if (!f) f = try_open(dir, "BIOS.BIN");
#ifdef FAKE_HLE
  if (f) fclose(f);
  booted = 1;
#else
  if (f) { fclose(f); booted = 1; } else booted = 0;
#endif
  return true; }
void retro_unload_game(void) {}
void retro_run(void) {
  int16_t s[4] = {1, 2, 3, 4};
#ifdef FAKE_HANG
  for (;;) {}
#endif
  for (int i = 0; i < 8; i++) pixels[i] = booted ? (uint16_t)(frame / 2 + i) : 0;
  frame++;
  video(pixels, 4, 2, 8); batch(s, 2); }
size_t retro_serialize_size(void) { return 0; }
bool retro_serialize(void *d, size_t n) { (void)d; (void)n; return false; }
"""


def rep(frames: list[str], uniform: list[str] | None = None, loaded: bool = True) -> dict:
    return {"loaded": loaded, "observables": {
        "frame_hashes": [[i * 10, h] for i, h in enumerate(frames)],
        "uniform_frames": sorted(set(uniform or []))}}


def boot_record(absent: list[dict], present: list[dict], rid: str = "2026-10-27-aaaaaaaaaaaa") -> dict:
    return {"id": rid, "variants": [{"label": "bios.bin-absent", "repetitions": absent},
                                    {"label": "bios.bin", "repetitions": present}]}


class BootOutcome(unittest.TestCase):
    GAME = ("black", "logo", "title", "title2")

    def test_boots_when_screens_are_shared(self) -> None:
        record = boot_record([rep(["black", "hle", "logo", "title"], ["black"])] * 2,
                             [rep(self.GAME, ["black"])] * 2)
        boot = rules.boot_outcome(record, "bios.bin-absent", "bios.bin")
        self.assertEqual((boot.status, boot.shared), (rules.BOOTS, 2))

    def test_one_colour_and_no_load_fail(self) -> None:
        black = boot_record([rep(["black"], ["black"]), rep([], [], loaded=False)],
                            [rep(self.GAME, ["black"])])
        self.assertEqual(rules.boot_outcome(black, "bios.bin-absent", "bios.bin").status, rules.FAILS)

    def test_a_shared_blank_screen_proves_nothing(self) -> None:
        # The black frame both runs start on is one colour: it is no game screen.
        record = boot_record([rep(["black", "error"], ["black"])], [rep(self.GAME, ["black"])])
        self.assertEqual(rules.boot_outcome(record, "bios.bin-absent", "bios.bin").status,
                         rules.INCONCLUSIVE)

    def test_one_shared_screen_is_not_enough(self) -> None:
        record = boot_record([rep(["logo", "error"])], [rep(self.GAME, ["black"])])
        self.assertEqual(rules.boot_outcome(record, "bios.bin-absent", "bios.bin").status,
                         rules.INCONCLUSIVE)

    def test_repetitions_that_disagree_settle_nothing(self) -> None:
        record = boot_record([rep(["logo", "title"]), rep(["black"], ["black"])],
                             [rep(self.GAME, ["black"])])
        self.assertEqual(rules.boot_outcome(record, "bios.bin-absent", "bios.bin").status,
                         rules.INCONCLUSIVE)

    def test_a_reference_that_shows_nothing_settles_nothing(self) -> None:
        record = boot_record([rep(["logo", "title"])], [rep(["black"], ["black"])])
        self.assertEqual(rules.boot_outcome(record, "bios.bin-absent", "bios.bin").status,
                         rules.INCONCLUSIVE)
        record = boot_record([rep(["logo", "title"])], [rep(self.GAME, loaded=False)])
        self.assertEqual(rules.boot_outcome(record, "bios.bin-absent", "bios.bin").status,
                         rules.INCONCLUSIVE)


class RuleTable(unittest.TestCase):
    ENTRY = MappingProxyType({"name": "bios.bin", "required": True, "aliases": ["Bios.bin"]})
    RID = "2026-10-27-bbbbbbbbbbbb"

    def test_boots_without_settles_hle_and_queues_required(self) -> None:
        verdicts = rules.rule_bios_need(rules.Boot(rules.BOOTS, "x", 3), self.ENTRY, self.RID)
        self.assertEqual([(v.field, v.value, v.apply) for v in verdicts],
                         [("hle_fallback", True, True), ("required", False, False)])
        self.assertEqual(verdicts[0].evidence, (self.RID,))
        optional = rules.rule_bios_need(rules.Boot(rules.BOOTS, "x", 3),
                                        {"name": "a", "required": False}, self.RID)
        self.assertEqual([v.field for v in optional], ["hle_fallback"])

    def test_fails_without_settles_required_and_no_hle(self) -> None:
        verdicts = rules.rule_bios_need(rules.Boot(rules.FAILS, "x"), self.ENTRY, self.RID)
        self.assertEqual([(v.field, v.value, v.apply) for v in verdicts],
                         [("required", True, True), ("hle_fallback", False, True)])

    def test_inconclusive_is_queued(self) -> None:
        (verdict,) = rules.rule_bios_need(rules.Boot(rules.INCONCLUSIVE, "x"), self.ENTRY, self.RID)
        self.assertFalse(verdict.apply)

    def test_path_and_alias(self) -> None:
        self.assertIsNone(rules.rule_path(self.ENTRY, "bios.bin", rules.Opened("bios.bin", self.RID)))
        moved = rules.rule_path(self.ENTRY, "bios.bin", rules.Opened("sys/bios.bin", self.RID))
        self.assertEqual((moved.field, moved.value, moved.apply), ("path", "sys/bios.bin", True))
        self.assertIsNone(rules.rule_alias(self.ENTRY, rules.Opened("Bios.bin", self.RID)))
        alias = rules.rule_alias(self.ENTRY, rules.Opened("BIOS.BIN", self.RID))
        self.assertEqual((alias.field, alias.value), ("aliases", ["Bios.bin", "BIOS.BIN"]))

    def test_never_opened_is_flagged_not_applied(self) -> None:
        verdict = rules.rule_never_opened(self.ENTRY, [self.RID], "Game (USA)")
        self.assertFalse(verdict.apply)
        self.assertEqual(verdict.rule, "never_opened")

    def test_variants(self) -> None:
        self.assertIsNone(rules.rule_variants("same", ["a", "b"], self.ENTRY, self.RID))
        self.assertFalse(rules.rule_variants("differs", ["b", "a"], self.ENTRY, self.RID).apply)

    def test_agreement_and_evidence(self) -> None:
        self.assertTrue(rules.agrees({"name": "a"}, rules.Verdict("r", "hle_fallback", False, "", ())))
        self.assertFalse(rules.agrees({"name": "a"}, rules.Verdict("r", "hle_fallback", True, "", ())))
        self.assertTrue(rules.agrees(self.ENTRY, rules.Verdict("r", "aliases", ["Bios.bin"], "", ())))
        merged = rules.merge_evidence({"required": ["x"]}, rules.Verdict("r", "required", True, "", ("y", "x")))
        self.assertEqual(merged, {"required": ["x", "y"]})


class ProfileEditor(unittest.TestCase):
    def test_replace_insert_and_keep_the_rest(self) -> None:
        out = campaign_profiles.set_fields(PROFILE, 0, {
            "required": False, "hle_fallback": True,
            "aliases": ["Bios.bin", "BIOS.BIN"], "evidence": {"hle_fallback": ["2026-10-27-cccccccccccc"]}})
        parsed = yaml.safe_load(out)
        self.assertEqual(parsed["files"][0]["hle_fallback"], True)
        self.assertEqual(parsed["files"][0]["aliases"], ["Bios.bin", "BIOS.BIN"])
        self.assertEqual(parsed["files"][1], {"name": "other.bin", "required": False})
        self.assertIn("  # -- the BIOS --", out)
        self.assertIn('    required: false\n    hle_fallback: true\n', out)
        self.assertIn('      - "BIOS.BIN"\n', out)
        lines = out.splitlines()
        self.assertEqual(lines[lines.index('    evidence:') + 1], '      hle_fallback: ["2026-10-27-cccccccccccc"]')

    def test_the_last_entry_and_a_no_op(self) -> None:
        out = campaign_profiles.set_fields(PROFILE, 1, {"path": "sub/other.bin"})
        self.assertEqual(yaml.safe_load(out)["files"][1]["path"], "sub/other.bin")
        self.assertEqual(campaign_profiles.set_fields(PROFILE, 1, {"required": False}), PROFILE)

    def test_refusals(self) -> None:
        with self.assertRaises(campaign_profiles.ProfileEditError):
            campaign_profiles.set_fields(PROFILE, 2, {"required": False})
        with self.assertRaises(campaign_profiles.ProfileEditError):
            campaign_profiles.set_fields("emulator: X\n", 0, {"required": False})
        with self.assertRaises(campaign_profiles.ProfileEditError):
            campaign_profiles.set_fields(PROFILE, 0, {"name": "x.bin"})

    def test_every_profile_reads_as_its_entries(self) -> None:
        # The text layout the editor reads holds in every profile it may edit.
        for path in sorted((REPO_ROOT / "emulators").glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            entries = (yaml.safe_load(text) or {}).get("files") or []
            if not entries or "\nfiles:\n" not in text:
                continue
            with self.subTest(profile=path.name):
                self.assertEqual(len(campaign_profiles.item_spans(text.splitlines())), len(entries))


class FakeHost(campaign.Host):
    def __init__(self, loads: list[float] | None = None, free: float = 500.0) -> None:
        super().__init__()
        self.loads = list(loads or [0.0])
        self.now = 0.0
        self.slept: list[float] = []
        self.free = free
        self.calls: list[list[str]] = []
        self.results: list[object] = []
        self.loadavg = self._load
        self.sleep = self._sleep
        self.clock = lambda: self.now
        self.free_gb = lambda path: self.free
        self.cpu_count = 4
        self.run = self._run

    def _load(self) -> tuple[float, float, float]:
        value = self.loads.pop(0) if len(self.loads) > 1 else self.loads[0]
        return (value, value, value)

    def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def _run(self, argv, **kwargs):
        self.calls.append(list(argv))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        TMP.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP))
        self.root = self.tmp / "repo"
        (self.root / "emulators").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "emulators" / "fake.yml").write_text(PROFILE, encoding="utf-8")
        bios = self.root / "bios" / "Fake" / "bios.bin"
        bios.parent.mkdir(parents=True)
        bios.write_bytes(b"BIOS")
        sha1 = hashlib.sha1(b"BIOS").hexdigest()
        db = {"files": {sha1: {"path": "bios/Fake/bios.bin", "name": "bios.bin", "size": 4,
                               "sha1": sha1, "md5": hashlib.md5(b"BIOS").hexdigest()}},
              "indexes": {"by_name": {"bios.bin": [sha1]}, "by_md5": {}, "by_crc32": {},
                          "by_sha256": {}, "by_path_suffix": {}}}
        (self.root / "database.json").write_text(json.dumps(db), encoding="utf-8")
        self.content_file = self.tmp / "content.yml"
        self.content_file.write_text(yaml.safe_dump({"systems": {"fake-system": {
            "romfetch": "fakesys", "content": [{
                "name": "Fake Game (World)", "catalog": "no-intro", "reason": "a test",
                "frames": 12, "frame_hash_every": 2}]}}}), encoding="utf-8")
        self.game = self.tmp / "content" / "fakesys" / "Fake Game (World)"
        self.paths = campaign.Paths(root=self.root, home=self.tmp / "home",
                                    content=self.tmp / "content", db=self.root / "database.json",
                                    content_file=self.content_file)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put_game(self, folder: Path | None = None, payload: bytes = b"GAME") -> Path:
        folder = folder or self.game
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "game.bin").write_bytes(payload)
        (folder / "identity.json").write_text(json.dumps({"status": "verified", "files": [
            {"name": "game.bin", "size": len(payload), "sha1": hashlib.sha1(payload).hexdigest()}]}))
        return folder

    def make(self, host: campaign.Host | None = None, **settings: object) -> campaign.Campaign:
        values = {"delays": (1, 2), "echo": False, **settings}
        return campaign.Campaign(self.paths, campaign.Settings(**values), host or FakeHost(),
                                 lock_path=self.tmp / "auto.lock")


class StateAndResume(Workspace):
    def test_should_run(self) -> None:
        self.assertTrue(campaign.should_run(None, "a", "d"))
        done = {"status": "done", "core_sha256": "a", "profile_digest": "d"}
        self.assertFalse(campaign.should_run(done, "a", "d"))
        self.assertTrue(campaign.should_run(done, "b", "d"))
        self.assertTrue(campaign.should_run(done, "a", "e"))
        self.assertTrue(campaign.should_run({**done, "status": "pending"}, "a", "d"))
        self.assertFalse(campaign.should_run({**done, "status": "failed"}, "a", "d"))

    def test_evidence_does_not_change_the_digest(self) -> None:
        profile = yaml.safe_load(PROFILE)
        before = campaign.profile_digest(profile)
        profile["files"][0]["evidence"] = {"required": ["2026-10-27-dddddddddddd"]}
        self.assertEqual(campaign.profile_digest(profile), before)
        profile["files"][0]["required"] = False
        self.assertNotEqual(campaign.profile_digest(profile), before)

    def test_state_survives_a_reload(self) -> None:
        c = self.make()
        c.pending("fake", "fake/fake-system/x/files", "content: romfetch not found")
        campaign.save_state(self.paths.state, c.state)
        again = self.make()
        self.assertEqual(again.state["checks"]["fake"]["fake/fake-system/x/files"]["status"], "pending")

    def test_budget(self) -> None:
        self.assertEqual(campaign.parse_budget("6h"), 21600)
        self.assertEqual(campaign.parse_budget("90m"), 5400)
        self.assertEqual(campaign.parse_budget("30"), 30)


class Guards(Workspace):
    def test_busy_host_waits_then_runs(self) -> None:
        host = FakeHost(loads=[5.0, 5.0, 0.5])
        c = self.make(host, max_load_wait=600)
        c.deadline = 10_000
        c.guard()
        self.assertEqual(host.slept, [60.0, 60.0])

    def test_busy_host_pauses(self) -> None:
        host = FakeHost(loads=[5.0])
        c = self.make(host, max_load_wait=120)
        c.deadline = 10_000
        with self.assertRaisesRegex(campaign.Pause, "busy host"):
            c.guard()

    def test_full_disk_pauses(self) -> None:
        c = self.make(FakeHost(free=10.0), min_free_gb=30)
        c.deadline = 10_000
        with self.assertRaisesRegex(campaign.Pause, "disk"):
            c.guard()

    def test_budget_pauses(self) -> None:
        c = self.make()
        c.deadline = 0
        with self.assertRaisesRegex(campaign.Pause, "budget"):
            c.guard()

    def test_the_lock_is_skipped_under_autorun(self) -> None:
        lock = self.tmp / "held.lock"
        with campaign.autorun.global_lock(lock):
            with self.assertRaises(campaign.autorun.LockBusy), campaign.host_lock(lock):
                pass
            os.environ[campaign.LOCK_HELD_ENV] = "1"
            try:
                with campaign.host_lock(lock):
                    pass
            finally:
                del os.environ[campaign.LOCK_HELD_ENV]


class ChildEnvironment(unittest.TestCase):
    def test_probes_builds_and_romfetch_get_no_secret(self) -> None:
        host = {"GITHUB_TOKEN": "t", "GH_TOKEN": "t", "ROMSTATION_USER": "u", "ROMSTATION_PASS": "p",
                "ROMFETCH_CONTENT_DIR": "/c", "SSH_AUTH_SOCK": "/s", "PATH": "/usr/bin"}
        with unittest.mock.patch.dict(os.environ, host):
            probe, fetch = campaign.child_env(), campaign.child_env(romfetch=True)
        for env in (probe, fetch):
            self.assertFalse({"GITHUB_TOKEN", "GH_TOKEN", "ROMSTATION_USER", "ROMSTATION_PASS",
                              "SSH_AUTH_SOCK"} & set(env))
            self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("ROMFETCH_CONTENT_DIR", probe)
        self.assertEqual(fetch["ROMFETCH_CONTENT_DIR"], "/c")

    def test_the_probe_is_started_with_that_environment(self) -> None:
        seen: list[dict] = []
        host = FakeHost()
        host.run = lambda argv, **kw: (seen.append(kw.get("env")),
                                       subprocess.CompletedProcess([], 0, '{"id": "x"}', ""))[1]
        with tempfile.TemporaryDirectory(dir=TMP) as tmp:
            paths = campaign.Paths(root=Path(tmp), home=Path(tmp) / "h", content=Path(tmp) / "c",
                                   db=Path(tmp) / "db.json", content_file=campaign.CONTENT_FILE)
            c = campaign.Campaign(paths, campaign.Settings(echo=False), host, lock_path=Path(tmp) / "l")
            c.deadline = 10_000
            with unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
                c.probe(["run"], "x")
        self.assertNotIn("GITHUB_TOKEN", seen[0])


class FailClosed(Workspace):
    """No core and no build runs outside the sandbox unless asked."""

    def test_a_pass_without_a_sandbox_is_refused(self) -> None:
        host = FakeHost()
        host.results = [subprocess.CompletedProcess([], 0, "{}", "")]
        c = self.make(host)
        with unittest.mock.patch.object(campaign.core_probe.shutil, "which", return_value=None):
            current = c.run(set(), None)
        self.assertEqual(current.result, "refused")
        self.assertIn("bwrap not found", current.detail)
        self.assertEqual(host.calls, [])
        self.assertEqual(mr.read_campaign(self.root, mr.CAMPAIGN_RUNS)[-1]["result"], "refused")

    def test_the_override_lets_it_run(self) -> None:
        c = self.make(allow_unsandboxed=True)
        c.unit_cores = lambda *a, **k: []
        with unittest.mock.patch.object(campaign.core_probe.shutil, "which", return_value=None):
            self.assertEqual(c.run(set(), None).result, "complete")

    def test_every_probe_requires_the_sandbox(self) -> None:
        core = campaign.Core("fake", self.tmp / "x.so", "a" * 64)
        self.assertIn("--require-sandbox", self.make().probe_args("run", core, "fake", "s", None, None))
        self.assertNotIn("--require-sandbox", self.make(allow_unsandboxed=True).probe_args(
            "run", core, "fake", "s", None, None))

    def test_a_build_without_a_sandbox_is_refused(self) -> None:
        c = self.make()
        src = c.paths.home / "src" / campaign.slug("https://example.invalid/x")
        (src / ".git").mkdir(parents=True)
        (src / "Makefile").write_text("all:\n")
        host = c.host
        host.results = [subprocess.CompletedProcess([], 0, "", "")]
        with unittest.mock.patch.object(campaign.core_probe.shutil, "which", return_value=None), \
                self.assertRaisesRegex(campaign.Unavailable, "refused: no sandbox"):
            c.cores.compile("https://example.invalid/x", "a" * 40, self.tmp / "out")
        self.assertFalse(any(call[0] == "make" for call in host.calls))


class ProbeRuns(Workspace):
    def completed(self, code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def probe(self, *results: object) -> tuple[campaign.ProbeResult, FakeHost]:
        host = FakeHost()
        host.results = list(results)
        c = self.make(host)
        c.deadline = 10_000
        return c.probe(["run", "--core", "x.so"], "fake-run"), host

    def test_timeout_is_failed_and_not_retried(self) -> None:
        result, host = self.probe(subprocess.TimeoutExpired(["x"], 1))
        self.assertEqual((result.status, result.reason), ("failed", "timeout"))
        self.assertEqual(len(host.calls), 1)
        self.assertIn("killed after", Path(result.log).read_text())
        result, host = self.probe(self.completed(1, stderr="subprocess.TimeoutExpired: timed out after 900 seconds"))
        self.assertEqual((result.reason, len(host.calls)), ("timeout", 1))

    def test_a_crashing_core_is_tried_three_times(self) -> None:
        result, host = self.probe(self.completed(1, stderr="worker exited -11"))
        self.assertEqual((result.status, result.reason), ("failed", "crashed 3 times"))
        self.assertEqual(len(host.calls), 3)

    def test_a_crash_then_a_record(self) -> None:
        record = {"id": "2026-10-27-eeeeeeeeeeee"}
        result, host = self.probe(self.completed(1, stderr="worker exited -11"),
                                  self.completed(0, stdout=json.dumps(record)))
        self.assertEqual((result.status, result.record, len(host.calls)), ("done", record, 2))

    def test_refusal_on_a_busy_host_pauses(self) -> None:
        with self.assertRaisesRegex(campaign.Pause, "busy host"):
            self.probe(self.completed(3, stderr="load average"))


class Content(Workspace):
    def content(self, name: str = "Fake Game (World)", pinned: bool = False) -> campaign.Content:
        return campaign.Content("fake-system", "fakesys", name, "no-intro", "r", None, "", 12, 2,
                                pinned, None)

    def test_local_content_is_verified_by_its_identity(self) -> None:
        self.put_game()
        c = self.make()
        self.assertEqual(c.store.get(self.content()), self.game)
        (self.game / "game.bin").write_bytes(b"EDIT")
        with self.assertRaisesRegex(campaign.Unavailable, "does not match"):
            self.make().store.get(self.content())

    def test_without_romfetch_the_check_waits_for_content(self) -> None:
        c = self.make(romfetch=str(self.tmp / "absent-romfetch"))
        with self.assertRaisesRegex(campaign.Unavailable, "romfetch not found"):
            c.store.get(self.content())

    def test_romfetch_is_retried_on_a_source_error(self) -> None:
        fetcher = self.tmp / "romfetch"
        fetcher.write_text("#!/bin/sh\n")
        fetcher.chmod(0o755)
        host = FakeHost()
        error = json.dumps({"error": {"code": "network", "message": "m", "hint": "h"}})
        game = self.game

        def fetched(*args, **kwargs):
            self.put_game(game)
            return subprocess.CompletedProcess([], 0, '{"status": "verified"}', "")
        host.results = [subprocess.CompletedProcess([], 3, error, ""), None]
        host.run = lambda argv, **kw: (host.calls.append(argv), fetched() if len(host.calls) == 2
                                        else host.results[0])[1]
        c = self.make(host, romfetch=str(fetcher))
        self.assertEqual(c.store.get(self.content()), self.game)
        self.assertEqual(host.slept, [1])
        self.assertEqual(host.calls[0][1:4], ["get", "fakesys", "Fake Game (World)"])
        self.assertTrue(c.state["content"]["fakesys/Fake Game (World)"]["fetched_by_campaign"])

    def test_romfetch_not_found_names_its_code(self) -> None:
        fetcher = self.tmp / "romfetch"
        fetcher.write_text("#!/bin/sh\n")
        host = FakeHost()
        host.results = [subprocess.CompletedProcess(
            [], 1, json.dumps({"error": {"code": "no_entry", "message": "m", "hint": "h"}}), "")]
        c = self.make(host, romfetch=str(fetcher))
        with self.assertRaisesRegex(campaign.Unavailable, r"exit 1 \(no_entry\)"):
            c.store.get(self.content())
        self.assertEqual(len(host.calls), 1)

    def test_shared_content_stays_until_its_last_user(self) -> None:
        c = self.make()
        self.put_game()
        content = self.content()
        c.state["content"][content.key] = {"fetched_by_campaign": True, "pinned": False, "size": 4}
        c.store.hold(content, "alpha")
        c.store.hold(content, "beta")
        c.store.release(content, "alpha")
        self.assertTrue(self.game.is_dir())
        c.store.release(content, "beta")
        self.assertFalse(self.game.exists())

    def test_pinned_or_foreign_content_is_never_deleted(self) -> None:
        c = self.make()
        self.put_game()
        content = self.content(pinned=True)
        c.state["content"][content.key] = {"fetched_by_campaign": True, "pinned": True}
        c.store.hold(content, "alpha")
        c.store.release(content, "alpha")
        self.assertTrue(self.game.is_dir())
        c.state["content"][content.key] = {"fetched_by_campaign": False, "pinned": False}
        c.store.hold(content, "alpha")
        c.store.release(content, "alpha")
        self.assertTrue(self.game.is_dir())

    def test_least_recently_used_goes_first_past_the_cap(self) -> None:
        c = self.make(cache_cap_gb=5e-9)
        for name, used in (("Old", "2026-01-01"), ("New", "2026-06-01"), ("Kept", "2025-01-01")):
            self.put_game(self.tmp / "content" / "fakesys" / name)
            c.state["content"][f"fakesys/{name}"] = {"fetched_by_campaign": True, "pinned": name == "Kept",
                                                     "size": 4, "last_used": used}
        self.assertEqual(c.store.evict(keep=set()), ["fakesys/Old"])
        self.assertTrue((self.tmp / "content" / "fakesys" / "New").is_dir())
        self.assertTrue((self.tmp / "content" / "fakesys" / "Kept").is_dir())

    def test_load_file(self) -> None:
        self.put_game()
        self.assertEqual(campaign.load_file(self.game, ""), self.game / "game.bin")
        with self.assertRaises(campaign.Unavailable):
            campaign.load_file(self.game, "cue")


class Findings(Workspace):
    RID = "2026-10-27-ffffffffffff"

    def setUp(self) -> None:
        super().setUp()
        self.c = self.make()
        self.c.current = campaign.Pass(started=campaign.now_iso())
        self.content = campaign.Content("fake-system", "fakesys", "Fake Game (World)", "no-intro",
                                        "r", None, "", 12, 2, False, None)
        self.core = campaign.Core("fake", self.tmp / "fake_libretro.so", "a" * 64)
        self.profile = self.root / "emulators" / "fake.yml"

    def lines(self) -> list[dict]:
        return mr.read_campaign(self.root, mr.FINDINGS)

    def entry(self, index: int = 0) -> dict:
        return yaml.safe_load(self.profile.read_text())["files"][index]

    def test_a_settled_field_is_written_with_its_evidence(self) -> None:
        verdicts = rules.rule_bios_need(rules.Boot(rules.BOOTS, "shown", 4), self.entry(), self.RID)
        self.c.settle("fake", self.content, self.core, 0, self.entry(), verdicts)
        entry = self.entry()
        self.assertEqual(entry["hle_fallback"], True)
        self.assertEqual(entry["evidence"], {"hle_fallback": [self.RID]})
        self.assertEqual(entry["required"], True)
        self.assertEqual([(line["field"], line["status"]) for line in self.lines()],
                         [("hle_fallback", "applied"), ("required", "open")])
        self.assertIn("emulators/fake.yml", self.c.current.touched)
        # The same outcome a second time adds nothing.
        self.c.settle("fake", self.content, self.core, 0, self.entry(), verdicts)
        self.assertEqual(len(self.lines()), 2)

    def test_an_agreeing_run_adds_evidence_only(self) -> None:
        verdicts = rules.rule_bios_need(rules.Boot(rules.FAILS, "nothing"), self.entry(), self.RID)
        self.c.settle("fake", self.content, self.core, 0, self.entry(), verdicts)
        entry = self.entry()
        self.assertEqual((entry["required"], entry["hle_fallback"]), (True, False))
        self.assertEqual(entry["evidence"], {"hle_fallback": [self.RID], "required": [self.RID]})
        self.assertEqual(self.lines(), [])

    def test_a_later_agreement_resolves_the_open_finding(self) -> None:
        (verdict,) = rules.rule_bios_need(rules.Boot(rules.INCONCLUSIVE, "?"), self.entry(), self.RID)
        self.c.settle("fake", self.content, self.core, 0, self.entry(), [verdict])
        self.assertEqual(self.lines()[-1]["status"], "open")
        later = rules.Verdict("inconclusive", "hle_fallback", False, "agrees now", ("2026-10-28-000000000000",))
        self.c.settle("fake", self.content, self.core, 0, self.entry(), [later])
        self.assertEqual([line["status"] for line in self.lines()], ["open", "resolved"])

    def test_never_opened_is_flagged(self) -> None:
        verdict = rules.rule_never_opened(self.entry(1), [self.RID], "Fake Game (World)")
        self.c.settle("fake", self.content, self.core, 1, self.entry(1), [verdict])
        self.assertEqual(self.lines()[0]["status"], "flagged")
        self.assertNotIn("evidence", self.entry(1))

    def test_finding_lines_validate(self) -> None:
        verdicts = rules.rule_bios_need(rules.Boot(rules.BOOTS, "shown", 4), self.entry(), self.RID)
        self.c.settle("fake", self.content, self.core, 0, self.entry(), verdicts)
        self.c.write_run_line(self.c.current)
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is a tooling dependency")
        for name, (_, schema_name) in mr.CAMPAIGN_FILES.items():
            schema = json.loads((REPO_ROOT / "schemas" / schema_name).read_text())
            for line in mr.read_campaign(self.root, name):
                self.assertEqual(list(Draft202012Validator(schema).iter_errors(line)), [], name)

    def test_the_batch_commit_plan(self) -> None:
        verdicts = rules.rule_bios_need(rules.Boot(rules.BOOTS, "shown", 4), self.entry(), self.RID)
        self.c.settle("fake", self.content, self.core, 0, self.entry(), verdicts)
        self.c.current.touched.add(mr.records_path("fake"))
        plan = self.c.plan_commits(set())
        self.assertEqual(plan, [
            ("chore: add campaign measurements", ["measurements/fake.jsonl", "measurements/findings.jsonl"]),
            ("chore: apply tested fields to fake", ["emulators/fake.yml"])])
        for subject, _ in plan:
            self.assertEqual(campaign.autorun.check_subject(subject), [], subject)
        # A file someone had changed before the pass is left to them.
        self.assertEqual(self.c.plan_commits({"emulators/fake.yml"})[1:], [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.c.commit_batch(dry_run=True), [])
        self.assertIn("would commit 'chore: add campaign measurements'", out.getvalue())


class Registry(unittest.TestCase):
    def test_no_profile_takes_a_campaign_file_name(self) -> None:
        stems = {p.stem for p in (REPO_ROOT / "emulators").glob("*.yml")}
        self.assertEqual(stems & set(mr.CAMPAIGN_FILES), set())

    def test_content_list(self) -> None:
        content = campaign.load_content(campaign.CONTENT_FILE)
        systems = {s for p in (REPO_ROOT / "emulators").glob("*.yml")
                   for s in (yaml.safe_load(p.read_text()) or {}).get("systems") or ()}
        self.assertTrue(content)
        for system, items in content.items():
            self.assertIn(system, systems)
            for item in items:
                self.assertIn(item.catalog, ("redump", "no-intro"))
                self.assertGreater(len(item.reason), 40)
                self.assertGreater(item.frames, item.frame_hash_every)

    def test_the_autorun_job(self) -> None:
        jobs = campaign.autorun.load_jobs()
        job = jobs["campaign"]
        self.assertTrue(job.lock and job.worktree and job.schedule)
        self.assertEqual(job.gate[0].argv, ("python", "scripts/campaign.py", "gate"))
        self.assertEqual({p for rule in job.commits for p in rule.paths},
                         {"measurements/*.jsonl", "emulators/*.yml"})
        self.assertGreater(job.schedule, jobs["nightly"].schedule)

    def test_autorun_tells_its_steps_the_lock_is_held(self) -> None:
        job = campaign.autorun.load_jobs()["campaign"]
        self.assertEqual(campaign.autorun.job_env(job)[campaign.LOCK_HELD_ENV], "1")


class SitePage(unittest.TestCase):
    def test_last_pass_and_open_findings(self) -> None:
        summary = {
            "last_run": {"ended_at": "2026-10-27T10:00:00+00:00", "result": "complete", "detail": "",
                         "counts": {"done": 12, "failed": 1, "pending": 3},
                         "pending_reasons": {"standalone": 3},
                         "failed": [{"emulator": "fake", "check": "fake/x/y/bench", "reason": "timeout"}]},
            "passes": 1, "applied": 2,
            "open": [{"emulator": "fake", "file": "bios.bin", "field": "required", "status": "open",
                      "profile_says": True, "run_shows": False, "statement": "boots without it",
                      "evidence": ["2026-10-27-aaaaaaaaaaaa"], "links": ["https://x/l#L1"]}],
        }
        page = "\n".join(sitemeasurements.campaign_section(summary))
        self.assertIn("12 tests done, 1 failed, 3 waiting", page)
        self.assertIn("| standalone | 3 |", page)
        self.assertIn("the profile says <code>true</code>, the run shows <code>false</code>", page)
        self.assertIn("[2026-10-27-aaaaaaaaaaaa](https://x/l#L1)", page)
        self.assertEqual(sitemeasurements.campaign_section({"last_run": None}), [])

    def test_observed_values_cannot_leave_their_place(self) -> None:
        # A value comes from a core's output or a romfetch error: it may hold
        # backticks, MDX braces and HTML.
        hostile = "``}{import x}<script>alert(1)</script>`` | [a](javascript:x) *b*"
        summary = {
            "last_run": {"ended_at": "2026-10-27T10:00:00+00:00", "result": "paused", "detail": hostile,
                         "counts": {"done": 0, "failed": 1, "pending": 1},
                         "pending_reasons": {hostile: 1},
                         "failed": [{"emulator": hostile, "check": hostile, "reason": hostile}]},
            "passes": 1, "applied": 0,
            "open": [{"emulator": "fake", "file": hostile, "field": hostile, "status": "open",
                      "profile_says": {"k": hostile}, "run_shows": [hostile], "statement": hostile,
                      "evidence": ["2026-10-27-aaaaaaaaaaaa"], "links": [None]}],
        }
        page = "\n".join(sitemeasurements.campaign_section(summary))
        for raw in ("<script", "{import", "}{", "``", "](javascript", "*b*", "| [a]"):
            self.assertNotIn(raw, page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&#123;import x&#125;", page)
        # The JSON dumps are escaped too: no brace reaches the page as syntax.
        self.assertNotIn("{", page)
        self.assertNotIn("}", page)


@unittest.skipUnless(shutil.which("cc") and shutil.which("strace"), "no C compiler or no strace")
class FakeCorePasses(Workspace):
    """Whole passes against a core compiled from FAKE_CORE."""

    @classmethod
    def setUpClass(cls) -> None:
        TMP.mkdir(parents=True, exist_ok=True)
        cls.build = Path(tempfile.mkdtemp(dir=TMP))
        source = cls.build / "fake.c"
        source.write_text(FAKE_CORE, encoding="utf-8")
        # The core sees no variable of the host, so its behaviour is compiled in.
        cls.cores = {}
        for kind, flags in (("plain", []), ("hle", ["-DFAKE_HLE"]), ("hang", ["-DFAKE_HANG"])):
            cls.cores[kind] = cls.build / f"fake_{kind}_libretro.so"
            subprocess.run(["cc", "-shared", "-fPIC", *flags, "-o", str(cls.cores[kind]), str(source)],
                           check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.build, ignore_errors=True)

    def passing(self, kind: str = "plain", core: Path | None = None,
                **settings: object) -> campaign.Campaign:
        self.put_game()
        # The core_probe threshold follows the host: a test never waits on the real load.
        host = campaign.Host(loadavg=lambda: (0.0, 0.0, 0.0), free_gb=lambda path: 500.0,
                             cpu_count=1_000_000)
        values = {"bench_warmup": 2, "bench_frames": 4, "check_repeat": 2, "probe_timeout": 30,
                  "delays": (1,), "echo": False, **settings}
        c = campaign.Campaign(self.paths, campaign.Settings(**values), host,
                              lock_path=self.tmp / "auto.lock")
        so = core or self.cores[kind]
        c.unit_cores = lambda *a, **k: [campaign.Core("fake", so, campaign.file_sha256(so))]
        return c

    def test_a_stand_in_is_proven_and_written(self) -> None:
        c = self.passing("hle")
        current = c.run(set(), None)
        self.assertEqual(current.result, "complete")
        records = [s.record for s in mr.read_records(self.root)]
        isolations = {rep["isolation"] for r in records for v in r["variants"] for rep in v["repetitions"]}
        self.assertEqual(isolations, {campaign.core_probe.SANDBOX})
        kinds = sorted(r["subject"].split("-")[0] for r in records)
        self.assertEqual(kinds, ["alias", "bench", "boot", "files"])
        boot = next(r for r in records if r["subject"].startswith("boot"))
        alias = next(r for r in records if r["subject"].startswith("alias"))
        entry = yaml.safe_load((self.root / "emulators" / "fake.yml").read_text())["files"][0]
        self.assertEqual(entry["hle_fallback"], True)
        self.assertEqual(entry["evidence"], {"aliases": [alias["id"]], "hle_fallback": [boot["id"]]})
        statuses = {(f["field"], f["status"]) for f in mr.read_campaign(self.root, mr.FINDINGS)}
        self.assertEqual(statuses, {("hle_fallback", "applied"), ("required", "open"),
                                    ("aliases", "applied")})
        runs = mr.read_campaign(self.root, mr.CAMPAIGN_RUNS)
        self.assertEqual(runs[-1]["counts"]["done"], 4)
        # The game is pinned by nobody but was not fetched by the campaign: kept.
        self.assertTrue(self.game.is_dir())

        # A second pass on the same core and profile runs nothing.
        again = self.passing("hle")
        self.assertEqual(again.run(set(), None).records, [])

        # A new build of the core runs everything again.
        rebuilt = self.build / "fake2_libretro.so"
        shutil.copy2(self.cores["hle"], rebuilt)
        with rebuilt.open("ab") as handle:
            handle.write(b"\0")
        third = self.passing("hle", core=rebuilt)
        # BIOS.BIN is declared now, so no alias run follows the boot test.
        self.assertEqual(len(third.run(set(), None).records), 3)

    def test_no_stand_in_settles_required_and_finds_an_alias(self) -> None:
        c = self.passing()
        c.run(set(), None)
        entry = yaml.safe_load((self.root / "emulators" / "fake.yml").read_text())["files"][0]
        self.assertEqual((entry["required"], entry["hle_fallback"]), (True, False))
        self.assertIn("BIOS.BIN", entry["aliases"])
        self.assertEqual(sorted(entry["evidence"]), ["aliases", "hle_fallback", "required"])
        alias = [f for f in mr.read_campaign(self.root, mr.FINDINGS) if f["field"] == "aliases"]
        self.assertEqual(alias[0]["status"], "applied")

    def test_a_hung_core_is_killed_and_the_pass_moves_on(self) -> None:
        c = self.passing("hang", probe_timeout=2)
        c.run(set(), None)
        # The whole process group went with the run: no worker spins on.
        left = subprocess.run(["pgrep", "-f", str(self.cores["hang"])], capture_output=True, check=False)
        self.assertEqual(left.stdout, b"")
        checks = c.state["checks"]["fake"]
        self.assertTrue(checks)
        self.assertEqual({entry["status"] for entry in checks.values()}, {"failed"})
        self.assertEqual({entry["reason"] for entry in checks.values()}, {"timeout"})
        self.assertEqual(mr.read_records(self.root), [])

    def test_missing_content_leaves_the_checks_pending(self) -> None:
        c = self.passing(romfetch=str(self.tmp / "no-romfetch"))
        shutil.rmtree(self.game)
        c.run(set(), None)
        reasons = {entry["reason"] for entry in c.state["checks"]["fake"].values()}
        self.assertEqual(len(reasons), 1)
        self.assertIn("romfetch not found", reasons.pop())


if __name__ == "__main__":
    unittest.main()
