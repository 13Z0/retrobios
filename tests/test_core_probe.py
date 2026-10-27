"""core_probe runs a libretro core headless and records what it measured.

The parsers are checked on fixed inputs. One test builds a ten-line
libretro core with the C compiler, when there is one, and runs the whole
probe on it: staging, the worker process, strace, the record and its schema.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import core_probe
import measurement_records

STRACE_SAMPLE = """\
4242  openat(AT_FDCWD, "/sys/bios/card.bin", O_RDONLY) = 5
4242  openat(AT_FDCWD, "/sys/bios/missing.bin", O_RDONLY) = -1 ENOENT (No such file or directory)
[pid  4243] open("/sys/bios/sub/x.rom", O_RDONLY|O_CLOEXEC) = 7
4242  openat(AT_FDCWD, "/elsewhere/game.cue", O_RDONLY) = 3
4242  read(3, "...", 10) = 10
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
struct retro_log_callback { void (*log)(int, const char *, ...); };
typedef bool (*env_t)(unsigned, void *);
typedef void (*video_t)(const void *, unsigned, unsigned, size_t);
typedef size_t (*batch_t)(const int16_t *, size_t);
typedef int16_t (*input_t)(unsigned, unsigned, unsigned, unsigned);
static env_t env; static video_t video; static batch_t batch; static input_t input;
static unsigned ports, frames; static struct retro_log_callback logger;
static uint16_t pixels[4 * 2]; static unsigned char card;
void retro_set_environment(env_t e) { bool t = true; env = e; env(18, &t); }
void retro_set_video_refresh(video_t v) { video = v; }
void retro_set_audio_sample(void *f) { (void)f; }
void retro_set_audio_sample_batch(batch_t b) { batch = b; }
void retro_set_input_poll(void *f) { (void)f; }
void retro_set_input_state(input_t f) { input = f; }
void retro_set_controller_port_device(unsigned port, unsigned device) { (void)port; if (device == 1) ports++; }
void retro_get_system_info(struct retro_system_info *i) {
  i->n = "Fake"; i->v = "1"; i->e = ""; i->full = true; i->block = false; }
void retro_get_system_av_info(struct retro_system_av_info *i) {
  memset(i, 0, sizeof *i); i->t.fps = 60.0; i->t.rate = 44100.0; }
void retro_init(void) {}
void retro_deinit(void) {}
bool retro_load_game(const void *g) {
  const char *dir = NULL; char path[4096]; struct retro_log_callback cb;
  const char *tag = getenv("CORE_PROBE_TEST_TAG");
  (void)g; env(9, &dir);
  snprintf(path, sizeof path, "%s/card.bin", dir);
  FILE *f = fopen(path, "rb");
  card = 0;
  if (f) { int c = fgetc(f); card = c < 0 ? 0 : (unsigned char)c; fclose(f); }
  if (env(27, &cb)) {
    logger = cb;
    cb.log(1, "card %s byte %d\n", f ? "present" : "absent", (int)card);
    cb.log(1, "tag %s\n", tag ? tag : "unset"); }
  return true; }
void retro_unload_game(void) {}
void retro_run(void) {
  int16_t s[4] = {1, 2, 3, 4};
  if (frames++ == 0 && logger.log) logger.log(1, "ports %u\n", ports);
  input(0, 1, 0, 8);
  for (int i = 0; i < 8; i++) pixels[i] = card;
  video(pixels, 4, 2, 8); batch(s, 2); }
size_t retro_serialize_size(void) { return 1; }
bool retro_serialize(void *d, size_t n) { (void)n; *(unsigned char *)d = card; return true; }
"""


def mednafen_state(sections: dict[str, dict[str, bytes]]) -> bytes:
    body = b""
    for section, entries in sections.items():
        chunk = b"".join(bytes([len(name)]) + name.encode() + struct.pack("<I", len(value)) + value
                         for name, value in entries.items())
        body += section.encode().ljust(32, b"\0") + struct.pack("<I", len(chunk)) + chunk
    return b"MDFNSVST".ljust(32, b"\0") + body


class ParsingTests(unittest.TestCase):
    def test_assignment(self) -> None:
        self.assertEqual(core_probe.parse_assignment("a=b=c", "--option"), ("a", "b=c"))
        self.assertEqual(core_probe.parse_assignment("dest=", "--vary"), ("dest", ""))
        for bad in ("novalue", "=x"):
            with self.assertRaises(argparse.ArgumentTypeError):
                core_probe.parse_assignment(bad, "--option")

    def test_press(self) -> None:
        self.assertEqual(core_probe.parse_press("120:start"), (120, 3, 1))
        self.assertEqual(core_probe.parse_press("5:A:10"), (5, 8, 10))
        for bad in ("x:a", "1:turbo", "1:a:0", "-1:a", "1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                core_probe.parse_press(bad)

    def test_strace(self) -> None:
        calls = core_probe.parse_strace(STRACE_SAMPLE)
        self.assertEqual([c["path"] for c in calls],
                         ["/sys/bios/card.bin", "/sys/bios/missing.bin", "/sys/bios/sub/x.rom",
                          "/elsewhere/game.cue"])
        self.assertEqual([c["ok"] for c in calls], [True, False, True, True])
        self.assertEqual(calls[1]["errno"], "ENOENT")
        opened, absent = core_probe.files_under(calls, Path("/sys/bios"))
        self.assertEqual(opened, ["card.bin", "sub/x.rom"])
        self.assertEqual(absent, ["missing.bin"])

    def test_printf(self) -> None:
        name = core_probe.ctypes.create_string_buffer(b"mpeg_card.bin")
        address = core_probe.ctypes.addressof(name)
        self.assertEqual(core_probe.render_printf(b"open %s: %d%% %x\n", [address, 50, 255]),
                         "open mpeg_card.bin: 50% ff")
        self.assertEqual(core_probe.render_printf(b"%d", [0xFFFFFFFF]), "-1")
        self.assertEqual(core_probe.render_printf(b"%.2f fps %u", [7]), "<double> fps 7")
        self.assertEqual(core_probe.render_printf(b"%s", [0]), "(null)")

    def test_mednafen_state(self) -> None:
        state = b"\x00" * 7 + mednafen_state({"MPEG": {"Present": b"\x01", "AuthState": b"\x02"},
                                              "CDB": {"Word": b"\x34\x12"}})
        self.assertEqual(core_probe.mednafen_state_vars(state),
                         {"MPEG.Present": "01", "MPEG.AuthState": "02", "CDB.Word": "3412"})
        self.assertEqual(core_probe.mednafen_state_vars(b"no state"), {})

    def test_elf_comment(self) -> None:
        self.assertIsInstance(core_probe.elf_comment(Path(sys.executable).resolve()), list)
        with tempfile.NamedTemporaryFile(dir=REPO_ROOT / "tmp" / "tests") as handle:
            handle.write(b"not an elf")
            handle.flush()
            self.assertEqual(core_probe.elf_comment(Path(handle.name)), [])

    def test_env_file_recorded_by_name_and_hash(self) -> None:
        library = Path(sys.executable).resolve()
        described = core_probe.describe_env_value(str(library))
        self.assertEqual(described, f"{library.name} sha256:{core_probe.file_digest(library, 'sha256')}")
        self.assertEqual(core_probe.describe_env_value("2000-01-01 00:00:00"), "2000-01-01 00:00:00")

    def test_cue_tracks(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "tmp" / "tests") as tmp:
            cue = Path(tmp, "d.cue")
            cue.write_text('FILE "a b.bin" BINARY\n  TRACK 01 MODE2/2352\nFILE c.bin BINARY\n',
                           encoding="utf-8")
            self.assertEqual([p.name for p in core_probe.cue_tracks(cue)], ["a b.bin", "c.bin"])


class ResultTests(unittest.TestCase):
    @staticmethod
    def variant(label: str, value: str, loaded: bool = True) -> dict:
        return {"label": label, "flags": [],
                "repetitions": [{"loaded": loaded, "observables": {"video_sha1": value}}]}

    def test_same_differs_inconclusive(self) -> None:
        same = core_probe.check_result([self.variant("a", "1"), self.variant("b", "1")], "video_sha1")
        self.assertEqual(same["status"], "same")
        differs = core_probe.check_result([self.variant("a", "1"), self.variant("b", "2")], "video_sha1")
        self.assertEqual(differs["status"], "differs")
        self.assertEqual(len(differs["groups"]), 2)
        flagged = core_probe.check_result([self.variant("a", "1", loaded=False), self.variant("b", "1")],
                                          "video_sha1")
        self.assertEqual(flagged["status"], "inconclusive")

    def test_other_observable_does_not_void_the_check(self) -> None:
        variant = {"label": "a", "flags": ["repetition 1 disagrees with repetition 0 on state_sha1"],
                   "repetitions": [{"loaded": True, "observables": {"video_sha1": "1", "state_sha1": "x"}},
                                   {"loaded": True, "observables": {"video_sha1": "1", "state_sha1": "y"}}]}
        result = core_probe.check_result([variant], "video_sha1")
        self.assertEqual(result["status"], "same")
        self.assertIn("Other flags", result["statement"])
        self.assertEqual(core_probe.check_result([variant], "state_sha1")["status"], "inconclusive")

    def test_host_paths_hidden(self) -> None:
        places = [("/tmp/x/system", "<system>"), ("/tmp/x", "<repo>")]
        self.assertEqual(core_probe.host_paths_hidden("/tmp/x/system/bios.bin NOT FOUND", places),
                         "<system>/bios.bin NOT FOUND")

    def test_timing_stats(self) -> None:
        stats = core_probe.timing_stats([1.0, 2.0, 3.0, 4.0, 10.0])
        self.assertEqual((stats["median"], stats["mean"], stats["min"], stats["max"]), (3.0, 4.0, 1.0, 10.0))
        self.assertEqual(stats["raw"], [1.0, 2.0, 3.0, 4.0, 10.0])

    def test_refusals(self) -> None:
        parser = core_probe.build_parser()
        core = sys.executable
        bench = parser.parse_args(["bench", "--core", core, "--record", "x", "--repeat", "3"])
        self.assertIn("a recorded benchmark needs --repeat >= 5", core_probe.validate_args(bench))
        check = parser.parse_args(["check", "--core", core, "--vary", "a=", "--vary", "b=",
                                   "--observable", "video_sha1", "--claim", "c", "--method", "m"])
        self.assertTrue(any("same DEST" in p for p in core_probe.validate_args(check)))
        unknown = parser.parse_args(["check", "--core", core, "--vary", "a=", "--observable", "nope",
                                     "--claim", "c", "--method", "m"])
        self.assertTrue(any("--observable nope" in p for p in core_probe.validate_args(unknown)))


@unittest.skipUnless(shutil.which("cc"), "no C compiler")
class FakeCoreTests(unittest.TestCase):
    """The whole probe against a core compiled from FAKE_CORE."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp" / "tests"))
        source = cls.tmp / "fake.c"
        source.write_text(FAKE_CORE, encoding="utf-8")
        cls.core = cls.tmp / "fake_libretro.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-o", str(cls.core), str(source)], check=True)
        cls.cards = []
        for byte in (b"\x01", b"\x01", b"\x02"):
            path = cls.tmp / f"card{len(cls.cards)}.bin"
            path.write_bytes(byte)
            cls.cards.append(path)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def probe(self, argv: list[str]) -> tuple[int, dict | None]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = core_probe.main(argv + ["--work-dir", str(self.tmp / "work")])
        return code, json.loads(out.getvalue()) if code == 0 else None

    def test_run_record(self) -> None:
        record_dir = self.tmp / "records"
        code, record = self.probe(["check", "--core", str(self.core), "--frames", "3",
                                   "--vary", f"card.bin={self.cards[0]}", "--vary", f"card.bin={self.cards[1]}",
                                   "--vary", f"card.bin={self.cards[2]}", "--vary", "card.bin=",
                                   "--observable", "frame_sha1", "--claim", "c", "--method", "m",
                                   "--record", str(record_dir), "--max-load", "1000",
                                   "--env", "CORE_PROBE_TEST_TAG=set-by-test", "--press", "0:a:2"])
        self.assertEqual(code, 0)
        result = record["check"]["result"]
        self.assertEqual(result["status"], "differs")
        self.assertEqual(sorted(len(g["variants"]) for g in result["groups"]), [1, 1, 2])
        first = record["variants"][0]["repetitions"][0]
        self.assertEqual(first["logs"][0]["text"], "card present byte 1")
        self.assertEqual(first["logs"][1]["text"], "tag set-by-test")
        self.assertEqual(first["logs"][2]["text"], "ports 2")
        self.assertEqual((first["input_queries"], first["input_pressed"]), (3, 2))
        self.assertEqual(record["inputs"]["env"], {"CORE_PROBE_TEST_TAG": "set-by-test"})
        self.assertEqual(record["variants"][3]["repetitions"][0]["logs"][0]["text"], "card absent byte 0")
        if first["tracing"] == "strace":
            self.assertEqual([f["path"] for f in first["files_opened"]], ["card.bin"])
            self.assertEqual(record["variants"][3]["repetitions"][0]["files_probed_absent"], ["card.bin"])
        self.assertEqual(first["frames"], 3)
        self.assertEqual(record["emulator"], "fake")
        self.assertEqual(sorted(p.relative_to(record_dir).as_posix()
                                for p in record_dir.rglob("*") if p.is_file()),
                         sorted(["measurements/fake.jsonl",
                                 f"measurements/data/fake/{self._only_id(record_dir)}.json.gz"]))
        line = json.loads((record_dir / "measurements" / "fake.jsonl").read_text())
        blob = (record_dir / "measurements" / "data" / "fake" / f"{line['id']}.json.gz").read_bytes()
        self.assertEqual(measurement_records.check_line(line), [])
        self.assertEqual(measurement_records.check_pair(line, blob), [])
        # The line and its sidecar give back the record the run printed.
        self.assertEqual(measurement_records.join(line, measurement_records.read_sidecar(blob)), record)
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            return
        for name, document in (("core-probe-record", line),
                               ("core-probe-data", measurement_records.read_sidecar(blob))):
            schema = json.loads((REPO_ROOT / "schemas" / f"{name}.schema.json").read_text())
            self.assertEqual(list(Draft202012Validator(schema).iter_errors(document)), [], name)

    @staticmethod
    def _only_id(record_dir: Path) -> str:
        return json.loads((record_dir / "measurements" / "fake.jsonl").read_text())["id"]

    def test_second_run_is_appended_beside_the_first(self) -> None:
        record_dir = self.tmp / "appended"
        argv = ["run", "--core", str(self.core), "--frames", "1", "--no-trace", "--max-load", "1000",
                "--record", str(record_dir), "--subject", "smoke"]
        self.assertEqual(self.probe(argv)[0], 0)
        lines = record_dir / "measurements" / "fake.jsonl"
        first = lines.read_text()
        code, record = self.probe(argv)
        self.assertEqual(code, 0)
        self.assertTrue(lines.read_text().startswith(first))
        ids = [json.loads(line)["id"] for line in lines.read_text().splitlines()]
        self.assertEqual(len(ids), 2)
        code, corrected = self.probe(argv + ["--supersedes", ids[0]])
        self.assertEqual((code, corrected["supersedes"]), (0, ids[0]))
        code, _ = self.probe(argv + ["--supersedes", "2000-01-01-000000000000"])
        self.assertEqual(code, 2)
        self.assertEqual(len(lines.read_text().splitlines()), 3)

    def test_record_defaults_to_this_checkout(self) -> None:
        parser = core_probe.build_parser()
        args = parser.parse_args(["run", "--core", sys.executable, "--record", "--frames", "1"])
        self.assertEqual(Path(args.record), REPO_ROOT)
        self.assertIsNone(parser.parse_args(["run", "--core", sys.executable]).record)

    def test_bench_stats(self) -> None:
        code, record = self.probe(["bench", "--core", str(self.core), "--frames", "2", "--warmup", "1",
                                   "--repeat", "5", "--no-trace", "--max-load", "1000"])
        self.assertEqual(code, 0)
        self.assertEqual(len(record["timing"]["ms_per_frame"]["raw"]), 5)
        self.assertEqual(record["variants"][0]["repetitions"][0]["tracing"], "disabled: --no-trace")
        self.assertEqual(record["flags"], [])

    def test_busy_machine_refused(self) -> None:
        code, _ = self.probe(["run", "--core", str(self.core), "--frames", "1", "--record",
                              str(self.tmp / "refused"), "--max-load", "-1"])
        self.assertEqual(code, 3)
        self.assertFalse((self.tmp / "refused").exists())


if __name__ == "__main__":
    unittest.main()
