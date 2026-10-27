"""Core probe records under measurements/, and what the site makes of them.

Synthetic record trees are built under tmp/tests: records split into a line
and a sidecar, read from the tree, compiled into the index and the SQLite
files. The append-only rule is checked against a commit, two branches that
each add a line merge, and the records this repository holds are checked
against HEAD.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import measurement_records as mr  # noqa: E402
import sitemeasurements as sm  # noqa: E402

REPO_URL = "https://github.com/Abdess/retrobios"
COMMIT = "e" * 40
SHA1 = "a" * 40
SHA256 = "b" * 64


def repetition(value: str, frames: int = 3) -> dict:
    return {
        "loaded": True, "library_name": "Fake", "library_version": "1", "fps_nominal": 60.0,
        "frames": frames, "warmup": 0, "wall_s": 0.5, "ms_per_frame": 2.5, "fps": 400.0,
        "observables": {"frame_sha1": value, "video_sha1": value, "audio_sha1": SHA1,
                        "state_sha1": None, "frame_hashes": [[0, value], [150, value]]},
        "logs": [{"frame": 0, "level": "info", "text": "card present " * 20}],
        "options_declared": {"fake_option": ["a", "b"]}, "env_unanswered": {"65": 1},
        "last_frame_size": [4, 2], "tracing": "strace", "input_queries": 3, "input_pressed": 1,
        "files_opened": [{"path": "card.bin", "sha1_after": SHA1, "sha1_staged": SHA1}],
        "files_probed_absent": [],
    }


def full_record(emulator: str, subject: str, when: str, kind: str = "check",
                values: tuple[str, ...] = ("1" * 40, "2" * 40), observable: str = "video_sha1") -> dict:
    variants = [{"label": f"v{i}", "system_files": {"card.bin": SHA1},
                 "repetitions": [repetition(v), repetition(v)], "flags": []}
                for i, v in enumerate(values)]
    record = {
        "record_version": mr.RECORD_VERSION, "emulator": emulator, "subject": subject,
        "supersedes": None, "kind": kind, "recorded_at": when,
        "core": {"file": "fake_libretro.so", "sha256": SHA256, "repo": "https://example.org/fake",
                 "commit": "c" * 40, "build_command": "make", "compiler": ["GCC 13"],
                 "library_name": "Fake", "library_version": "1"},
        "frontend": {"script": "scripts/core_probe.py", "commit": "d" * 40, "script_modified": False,
                     "script_sha256": SHA256},
        "host": {"cpu_model": "Test CPU", "cpu_count": 8, "os": "Linux-6.1-x86_64-with-glibc2.39",
                 "kernel": "6.1", "machine": "x86_64", "python": "3.12.3",
                 "load_before": [0.5, 0.5, 0.5], "load_after": [0.6, 0.5, 0.5], "max_load": 2.0},
        "inputs": {"content": {"label": "test disc", "sha1": SHA1, "size": 10,
                               "tracks": [{"file": "t.bin", "sha1": SHA1, "size": 9}]},
                   "options": {"fake_option": "a"}, "env": {}, "frames": 3, "warmup": 0,
                   "repetitions": 2 if kind == "check" else 5, "presses": [],
                   "observables": ["frame_sha1", "video_sha1", "audio_sha1", "state_sha1", "frame_hashes"],
                   "frame_hash_every": 150, "joypad_ports": 2},
        "variants": variants,
        "flags": [],
    }
    if kind == "check":
        groups: dict[str, list[str]] = {}
        for variant, value in zip(variants, values):
            key = json.dumps(variant["repetitions"][0]["observables"][observable], sort_keys=True)
            groups.setdefault(key, []).append(variant["label"])
        record["check"] = {
            "claim": f"The card decides what {emulator} shows.", "method": "Each card, twice.",
            "observable": observable, "variable": "card.bin",
            "result": {"status": "differs" if len(groups) > 1 else "same",
                       "statement": f"{observable} takes {len(groups)} values",
                       "groups": [{"value": json.loads(k), "variants": v} for k, v in groups.items()]},
        }
    else:
        record["variants"] = record["variants"][:1]
        record["variants"][0]["repetitions"] = [repetition(values[0]) for _ in range(5)]
        raw = [2.0, 2.5, 3.0, 2.2, 2.4]
        stats = {"median": 2.4, "mean": 2.42, "stdev": 0.37, "min": 2.0, "max": 3.0, "raw": raw}
        record["timing"] = {"ms_per_frame": stats,
                            "fps": {**stats, "median": 416.7, "raw": [1000 / v for v in raw]}}
    return record




def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CEILING_DIRECTORIES": str(cwd.parent)}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def add_record(tree: Path, full: dict) -> dict:
    slim, sidecar = mr.split(full)
    sealed, blob = mr.seal(slim, sidecar)
    data = tree / mr.sidecar_path(sealed["emulator"], sealed["id"])
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(blob)
    lines = tree / mr.records_path(sealed["emulator"])
    lines.parent.mkdir(parents=True, exist_ok=True)
    with lines.open("a", encoding="utf-8") as handle:
        handle.write(mr.line_text(sealed) + "\n")
    return sealed


def commit_all(tree: Path, message: str) -> str:
    git(tree, "add", "--all", ".")
    git(tree, "commit", "-q", "-m", message)
    return git(tree, "rev-parse", "HEAD")


class Workspace(unittest.TestCase):
    """A tree with three records under measurements/ and one profile."""

    def setUp(self) -> None:
        (REPO_ROOT / "tmp" / "tests").mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp" / "tests"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        self.first = add_record(self.tree, full_record("alpha", "card", "2026-10-01T10:00:00+00:00"))
        self.bench = add_record(self.tree, full_record("alpha", "speed", "2026-10-01T11:00:00+00:00",
                                                       kind="bench"))
        self.beta = add_record(self.tree, full_record("beta", "card", "2026-10-02T10:00:00+00:00",
                                                      values=("1" * 40, "1" * 40)))
        self.emulators = self.tmp / "emulators"
        self.emulators.mkdir()
        (self.emulators / "alpha.yml").write_text(
            'emulator: "Alpha"\ntype: libretro\nsystems:\n  - sega-saturn\nfiles: []\n')

    def built(self, public: Path) -> sm.Built:
        return sm.build(public, self.tree, self.emulators, scratch=self.tmp / "scratch",
                        system_names={"sega-saturn": "Sega Saturn"}, repo=REPO_URL, commit=COMMIT)

    def repository(self) -> str:
        """Make the tree a git repository, its records committed on main."""
        git(self.tree, "init", "-q", "-b", "main")
        (self.tree / ".gitattributes").write_text("measurements/*.jsonl merge=union\n")
        return commit_all(self.tree, "feat: first records")


class RecordSplit(unittest.TestCase):
    def test_round_trip(self) -> None:
        for kind in ("check", "bench"):
            full = full_record("alpha", "card", "2026-10-01T10:00:00+00:00", kind=kind)
            slim, sidecar = mr.split(full)
            sealed, blob = mr.seal(slim, sidecar)
            with self.subTest(kind=kind):
                self.assertEqual(mr.join(sealed, mr.read_sidecar(blob)), full)
                self.assertEqual(mr.check_line(sealed), [])
                self.assertEqual(mr.check_pair(sealed, blob), [])
                self.assertNotIn("logs", json.dumps(sealed["variants"]))
                self.assertNotIn("frame_hashes", json.dumps(sealed["variants"]))

    def test_bulky_compared_value_moves_to_the_sidecar(self) -> None:
        full = full_record("alpha", "card", "2026-10-01T10:00:00+00:00", observable="frame_hashes")
        slim, sidecar = mr.split(full)
        groups = slim["check"]["result"]["groups"]
        self.assertTrue(all("value" not in g and len(g["value_sha1"]) == 40 for g in groups))
        sealed, blob = mr.seal(slim, sidecar)
        self.assertEqual(mr.join(sealed, mr.read_sidecar(blob)), full)

    def test_sidecar_bytes_and_id_are_reproducible(self) -> None:
        full = full_record("alpha", "card", "2026-10-01T10:00:00+00:00")
        one = mr.seal(*mr.split(full))
        two = mr.seal(*mr.split(copy.deepcopy(full)))
        self.assertEqual(one, two)
        self.assertRegex(one[0]["id"], r"^2026-10-01-[0-9a-f]{12}$")

    def test_tampering_is_named(self) -> None:
        sealed, blob = mr.seal(*mr.split(full_record("alpha", "card", "2026-10-01T10:00:00+00:00")))
        self.assertTrue(mr.check_pair(sealed, blob + b"x"))
        changed = {**sealed, "subject": "other"}
        self.assertTrue(any("does not hash" in p for p in mr.check_line(changed)))

    def test_a_campaign_line_stays_under_20_kb(self) -> None:
        """Eleven variants, two repetitions each, long logs: the line is what
        a reader opens, the logs are in the sidecar."""
        full = full_record("alpha", "card", "2026-10-01T10:00:00+00:00",
                           values=tuple(f"{i:040x}" for i in range(11)))
        for variant in full["variants"]:
            for rep in variant["repetitions"]:
                rep["logs"] = rep["logs"] * 200
                rep["observables"]["frame_hashes"] = [[k, SHA1] for k in range(0, 3000, 150)]
        sealed, _blob = mr.seal(*mr.split(full))
        self.assertLess(len(mr.line_text(sealed).encode()), 20_000)

    def test_first_layout_record_keeps_its_id(self) -> None:
        old = full_record("alpha", "card", "2026-10-01T10:00:00+00:00")
        old = mr.first_layout(old)
        rid = f"2026-10-01-{mr.sha256_hex(mr.canonical(old))[:12]}"
        full = {**old, "record_version": 2, "emulator": "alpha", "subject": "card",
                "supersedes": None, "migrated_from_version": 1}
        sealed, blob = mr.seal(*mr.split(full), rid)
        self.assertEqual(sealed["id"], rid)
        self.assertEqual(mr.check_line(sealed), [])
        self.assertEqual(mr.check_pair(sealed, blob), [])
        self.assertEqual(mr.first_layout(mr.join(sealed, mr.read_sidecar(blob))), old)
        forged = {**sealed, "id": "2026-10-01-000000000000"}
        self.assertTrue(mr.check_pair(forged, blob))




class ReadAndIndex(Workspace):
    def test_records_are_read_from_the_tree(self) -> None:
        records = mr.read_records(self.tree)
        self.assertEqual([(s.emulator, s.line) for s in records], [("alpha", 1), ("alpha", 2), ("beta", 1)])
        self.assertEqual(mr.read_records(self.tmp / "empty"), [])

    def test_a_broken_tree_is_named(self) -> None:
        stray = self.tree / mr.DATA_DIR / "beta" / "2026-10-02-000000000000.json.gz"
        stray.write_bytes(b"x")
        (self.tree / mr.sidecar_path("alpha", self.bench["id"])).unlink()
        with self.assertRaises(mr.MeasurementsError) as caught:
            mr.read_records(self.tree)
        message = str(caught.exception)
        self.assertIn(f"{mr.sidecar_path('alpha', self.bench['id'])} is missing", message)
        self.assertIn(f"{mr.DATA_DIR}/beta/2026-10-02-000000000000.json.gz: no record line names it", message)

    def test_index_shape(self) -> None:
        index = self.built(self.tmp / "public").index
        self.assertEqual({k: index[k] for k in ("schema_version", "kind", "repo", "commit", "records", "count")},
                         {"schema_version": 1, "kind": "measurements", "repo": REPO_URL,
                          "commit": COMMIT, "records": 3, "count": 3})
        keys = {"emulator", "emulator_name", "profiled", "systems", "system_names", "subject", "kind",
                "claim", "method", "observable", "variable", "status", "statement", "content",
                "variants", "repetitions", "flags", "date", "host", "core", "frontend_commit", "id",
                "runs", "record_url", "data_url", "sqlite"}
        for item in index["items"]:
            self.assertEqual(set(item), keys)
        alpha = {i["subject"]: i for i in index["items"] if i["emulator"] == "alpha"}
        self.assertEqual(alpha["card"]["status"], "differs")
        self.assertEqual(alpha["card"]["system_names"], ["Sega Saturn"])
        self.assertEqual(alpha["card"]["record_url"],
                         f"{REPO_URL}/blob/{COMMIT}/measurements/alpha.jsonl#L1")
        self.assertEqual(alpha["card"]["data_url"],
                         f"{REPO_URL}/blob/{COMMIT}/measurements/data/alpha/{self.first['id']}.json.gz")
        self.assertEqual(alpha["speed"]["status"], "measured")
        self.assertIn("ms per frame", alpha["speed"]["statement"])
        self.assertIn("Test CPU", alpha["speed"]["host"])
        written = json.loads((self.tmp / "public" / sm.INDEX_PATH).read_text())
        self.assertEqual(written, index)

    def test_links_name_the_last_commit_that_touched_the_records(self) -> None:
        first = self.repository()
        (self.tree / "other.txt").write_text("x")
        commit_all(self.tree, "chore: unrelated")
        self.assertEqual(mr.last_commit(self.tree), first)
        index = sm.build(self.tmp / "public", self.tree, self.emulators, scratch=self.tmp / "scratch").index
        self.assertEqual(index["commit"], first)
        self.assertTrue(all(f"/blob/{first}/measurements/" in i["record_url"] for i in index["items"]))

    def test_latest_record_of_a_subject_wins_and_a_superseded_one_is_dropped(self) -> None:
        later = full_record("alpha", "card", "2026-10-05T10:00:00+00:00", values=("3" * 40, "3" * 40))
        later["supersedes"] = self.first["id"]
        add_record(self.tree, later)
        card = next(i for i in self.built(self.tmp / "public").index["items"]
                    if i["emulator"] == "alpha" and i["subject"] == "card")
        self.assertEqual((card["status"], card["runs"]), ("same", 1))
        self.assertTrue(card["record_url"].endswith("#L3"))

    def test_sqlite_tables_and_row_counts_match_the_records(self) -> None:
        public = self.tmp / "public"
        self.built(public)
        connection = sqlite3.connect(public / sm.SQLITE_PATH)
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
        self.assertEqual(tuple(tables), sm.TABLES)
        count = lambda table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
        records = [s.record for s in mr.read_records(self.tree)]
        self.assertEqual(count("runs"), len(records))
        self.assertEqual(count("checks"), sum(1 for r in records if r.get("check")))
        self.assertEqual(count("benchmarks"), 2 * sum(1 for r in records if r.get("timing")))
        self.assertEqual(count("variants"), sum(len(r["variants"]) for r in records))
        self.assertEqual(count("check_groups"),
                         sum(len(r["check"]["result"]["groups"]) for r in records if r.get("check")))
        self.assertEqual(count("hosts"), 1)
        self.assertEqual(count("cores"), 1)
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        self.assertEqual((meta["measurements_repo"], meta["measurements_commit"]), (REPO_URL, COMMIT))
        self.assertEqual(connection.execute("SELECT system FROM emulator_systems").fetchall(),
                         [("sega-saturn",)])
        self.assertEqual(connection.execute("SELECT record_url FROM runs WHERE line = 1 AND emulator = 'beta'")
                         .fetchone()[0], f"{REPO_URL}/blob/{COMMIT}/measurements/beta.jsonl#L1")
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"runs_emulator_idx", "emulator_systems_system_idx", "checks_claim_idx"} <= indexes)
        connection.close()
        for emulator, expected in (("alpha", 2), ("beta", 1)):
            per = sqlite3.connect(public / sm.PER_EMULATOR_DIR / f"{emulator}.sqlite")
            self.assertEqual(per.execute("SELECT COUNT(*), MIN(emulator), MAX(emulator) FROM runs").fetchone(),
                             (expected, emulator, emulator))
            per.close()

    def test_two_builds_are_byte_identical(self) -> None:
        one, two = self.tmp / "one", self.tmp / "two"
        first, second = self.built(one), self.built(two)
        self.assertEqual([p.relative_to(one) for p in first.paths], [p.relative_to(two) for p in second.paths])
        for path in first.paths:
            self.assertEqual(path.read_bytes(), (two / path.relative_to(one)).read_bytes(), path.name)

    def test_no_records_writes_an_empty_index(self) -> None:
        shutil.rmtree(self.tree / mr.RECORDS_DIR)
        built = self.built(self.tmp / "public")
        self.assertEqual((built.index["records"], built.index["count"]), (0, 0))
        self.assertEqual(len(built.paths), 2)

    def test_every_line_validates_against_the_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is not installed")
        import validate_schemas  # noqa: PLC0415

        self.assertEqual(validate_schemas._measurement_errors(self.tree), [])
        lines = self.tree / mr.records_path("beta")
        record = json.loads(lines.read_text())
        record["kind"] = "guess"
        lines.write_text(mr.line_text(record) + "\n")
        errors = validate_schemas._measurement_errors(self.tree)
        self.assertTrue(any("measurements/beta.jsonl:1" in e and "kind" in e for e in errors), errors)
        self.assertIsNotNone(Draft202012Validator)


class AppendOnly(Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.commit = self.repository()

    def test_an_appended_line_and_a_new_file_pass(self) -> None:
        add_record(self.tree, full_record("alpha", "card", "2026-10-04T10:00:00+00:00"))
        add_record(self.tree, full_record("gamma", "card", "2026-10-04T10:00:00+00:00"))
        self.assertEqual(mr.append_only_problems(self.tree, "HEAD"), [])

    def test_a_changed_line_is_named(self) -> None:
        lines = self.tree / mr.records_path("alpha")
        text = lines.read_text().splitlines()
        text[0] = text[0].replace('"Each card, twice."', '"Each card, once."')
        lines.write_text("\n".join(text) + "\n")
        self.assertEqual(mr.append_only_problems(self.tree, "HEAD"),
                         ["measurements/alpha.jsonl: line 1 changed"])

    def test_a_removed_line_or_data_file_is_named(self) -> None:
        lines = self.tree / mr.records_path("alpha")
        lines.write_text(lines.read_text().splitlines()[0] + "\n")
        (self.tree / mr.sidecar_path("beta", self.beta["id"])).unlink()
        problems = mr.append_only_problems(self.tree, "HEAD")
        self.assertIn("measurements/alpha.jsonl: line 2 removed", problems)
        self.assertIn(f"{mr.sidecar_path('beta', self.beta['id'])}: removed", problems)

    def test_a_rewritten_data_file_is_named(self) -> None:
        data = self.tree / mr.sidecar_path("beta", self.beta["id"])
        data.write_bytes(data.read_bytes() + b"x")
        self.assertEqual(mr.append_only_problems(self.tree, "HEAD"),
                         [f"{mr.sidecar_path('beta', self.beta['id'])}: rewritten"])

    def test_two_branches_adding_a_line_merge_cleanly(self) -> None:
        git(self.tree, "checkout", "-q", "-b", "left")
        add_record(self.tree, full_record("alpha", "card", "2026-10-06T10:00:00+00:00"))
        commit_all(self.tree, "left")
        git(self.tree, "checkout", "-q", "main")
        git(self.tree, "checkout", "-q", "-b", "right")
        add_record(self.tree, full_record("alpha", "speed", "2026-10-06T11:00:00+00:00", kind="bench",
                                          values=("4" * 40,)))
        commit_all(self.tree, "right")
        git(self.tree, "merge", "-q", "--no-edit", "left")
        self.assertEqual(mr.append_only_problems(self.tree, self.commit), [])
        records = mr.read_records(self.tree)
        self.assertEqual(sum(1 for s in records if s.emulator == "alpha"), 4)


class CommittedRecords(unittest.TestCase):
    """The records this repository holds."""

    def test_no_line_or_data_file_changed_or_vanished_since_head(self) -> None:
        if subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True, check=False).returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual(mr.append_only_problems(REPO_ROOT, "HEAD"), [])

    def test_every_record_reads_and_joins_its_data(self) -> None:
        records = mr.read_records(REPO_ROOT)
        self.assertGreaterEqual(len(records), 7)

    def test_jsonl_files_merge_by_union(self) -> None:
        rules = (REPO_ROOT / ".gitattributes").read_text().splitlines()
        rule = next(line for line in rules if line.startswith(f"{mr.RECORDS_DIR}/*.jsonl "))
        self.assertIn("merge=union", rule.split())


class SitePages(unittest.TestCase):
    ITEM = {
        "emulator": "beetle_saturn", "emulator_name": "Beetle Saturn", "profiled": True,
        "systems": ["sega-saturn"], "system_names": ["Sega Saturn"], "subject": "mpeg-card",
        "kind": "check", "claim": "The card <image> decides it.", "method": "Twice per card; every input fixed.",
        "observable": "video_sha1", "variable": "mpeg_card.bin", "status": "same",
        "statement": "video_sha1 is identical across all 11 variants", "content": "disc",
        "variants": 11, "repetitions": 2, "flags": 1, "date": "2026-10-27T05:14:32+00:00",
        "host": "Test CPU, 12 CPUs", "core": {"library": "Beetle Saturn v1.32.1", "commit": "2ce77e2" * 5 + "abcde",
                                             "file": "x.so", "sha256": SHA256, "repo": None, "build": "make"},
        "frontend_commit": None, "id": "2026-10-27-43527b763ace", "runs": 2,
        "record_url": f"{REPO_URL}/blob/{'e' * 40}/measurements/beetle_saturn.jsonl#L4",
        "data_url": f"{REPO_URL}/blob/{'e' * 40}/measurements/data/beetle_saturn/2026-10-27-43527b763ace.json.gz",
        "sqlite": "downloads/measurements/beetle_saturn.sqlite",
    }

    def test_tested_block(self) -> None:
        self.assertEqual(sm.tested_section("beetle_saturn", []), [])
        text = "\n".join(sm.tested_section("beetle_saturn", [self.ITEM]))
        self.assertTrue(text.startswith("## Tested\n"))
        for expected in ("**Result:** `same`", "**Method:** Twice per card. Every input fixed.", "**Ran on:** Test CPU, 12 CPUs",
                         self.ITEM["record_url"], self.ITEM["data_url"], "1 flag raised", "2 runs",
                         "../downloads/measurements/beetle_saturn.sqlite", "../wiki/measurements.md",
                         "&lt;image>"):
            self.assertIn(expected, text)

    def test_hosts_table_on_the_method_page(self) -> None:
        page = sm.with_hosts(f"x\n\n{sm.HOSTS_MARKER}\n",
                                           {"items": [self.ITEM, {**self.ITEM, "host": "Test CPU, 12 CPUs, load 1 before, 2 after"}]})
        self.assertIn("| Test CPU, 12 CPUs | 2 |", page)
        self.assertNotIn(sm.HOSTS_MARKER, page)

    def test_the_explorer_reads_the_index(self) -> None:
        source = (REPO_ROOT / "website" / "src" / "components" / "MeasurementsExplorer.astro").read_text()
        self.assertIn("data/measurements/index.json", source)
        self.assertEqual(sm.INDEX_PATH.as_posix(), "data/measurements/index.json")
        read = set(re.findall(r"\b(?:item|i)\.([a-z_]+)", source)) - {"length"}
        self.assertTrue(read)
        self.assertLessEqual(read, set(self.ITEM), read - set(self.ITEM))
        page = sm.generate_explorer_page({"records": 7, "count": 7, "commit": "e" * 40,
                                                        "repo": REPO_URL})
        self.assertIn("<MeasurementsExplorer />", page)
        self.assertIn("downloads/measurements.sqlite", page)
        self.assertIn(f"({REPO_URL}/tree/{'e' * 40}/measurements)", page)


    def test_the_builds_read_the_records_in_this_repository(self) -> None:
        for name in ("deploy-site.yml", "validate.yml"):
            workflow = (REPO_ROOT / ".github" / "workflows" / name).read_text()
            with self.subTest(workflow=name):
                self.assertIn('- "measurements/**"', workflow)
                self.assertNotIn("measurements-pin", workflow)
                self.assertNotIn(".cache/measurements", workflow)
                self.assertNotIn("--require-measurements", workflow)
        self.assertFalse((REPO_ROOT / "measurements-pin.json").exists())
        self.assertFalse((REPO_ROOT / "scripts" / "sync_measurements.py").exists())

    def test_nothing_links_the_former_measurements_repository(self) -> None:
        former = "retrobios-" + "measurements"
        roots = ["wiki", "schemas", "scripts", "website/src", ".github", "tests", "measurements"]
        suffixes = {".md", ".py", ".json", ".jsonl", ".yml", ".astro", ".ts", ".mjs"}
        stray = [path.relative_to(REPO_ROOT).as_posix()
                 for root in roots for path in sorted((REPO_ROOT / root).rglob("*"))
                 if path.is_file() and path.suffix in suffixes
                 and former in path.read_text(encoding="utf-8", errors="replace")]
        stray += [name for name in ("README.md", "CONTRIBUTING.md")
                  if former in (REPO_ROOT / name).read_text(encoding="utf-8")]
        self.assertEqual(stray, [])


class Freshness(unittest.TestCase):
    def test_no_measurements_area(self) -> None:
        import check_freshness  # noqa: PLC0415
        import refresh_stale  # noqa: PLC0415

        self.assertNotIn("measurements", check_freshness.AREAS)
        self.assertNotIn("measurements", refresh_stale.AUTO_AREAS)


if __name__ == "__main__":
    unittest.main()
