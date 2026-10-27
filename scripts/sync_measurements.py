#!/usr/bin/env python3
"""Read the measurements repository at its pin and compile what the site shows.

The records live in their own repository (Abdess/retrobios-measurements),
one append-only .jsonl per emulator. This repository holds only the pin,
`measurements-pin.json`: the repository URL and the commit read. The clone
is kept at .cache/measurements/ and read at the pinned commit with git, so
its working tree may hold records not yet committed.

From the records at the pin it writes, never commits:

  public/data/measurements/index.json        the compact index the explorer and
                                             the emulator pages read, one item
                                             per emulator and subject
  public/downloads/measurements.sqlite       every record, normalised
  public/downloads/measurements/<emu>.sqlite one database per emulator

`--fetch` clones or updates the clone, the only step that uses the network,
and refuses a remote whose history rewrote or removed a line the pin holds.
`--bump` then moves the pin to the fetched head.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import measurement_records as mr  # noqa: E402
from artifacts import json_text, write_if_changed, write_text_atomic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIN_FILE = ROOT / "measurements-pin.json"
CLONE = ROOT / ".cache" / "measurements"
INDEX_PATH = Path("data") / "measurements" / "index.json"
SQLITE_PATH = Path("downloads") / "measurements.sqlite"
PER_EMULATOR_DIR = Path("downloads") / "measurements"
SQLITE_SCHEMA_VERSION = "1"
INDEX_SCHEMA_VERSION = 1


class MeasurementsError(RuntimeError):
    """The measurements at the pin cannot be read, or break a rule."""


@dataclass(frozen=True)
class Stored:
    """One record line as read at a commit."""

    emulator: str
    line: int
    record: dict


# --------------------------------------------------------------------------
# Pin and git
# --------------------------------------------------------------------------

def load_pin(path: Path = PIN_FILE) -> dict:
    pin = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pin.get("repo"), str) or not isinstance(pin.get("commit"), str) \
            or len(pin["commit"]) != 40:
        raise MeasurementsError(f"{path}: needs a repo URL and a 40-character commit")
    return pin


def write_pin(pin: dict, path: Path = PIN_FILE) -> None:
    write_text_atomic(str(path), json.dumps(pin, indent=2, sort_keys=True) + "\n")


def git(clone: Path, *args: str, binary: bool = False) -> str | bytes:
    proc = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise MeasurementsError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout if binary else proc.stdout.decode("utf-8")


def has_commit(clone: Path, commit: str) -> bool:
    if not (clone / ".git").exists():
        return False
    proc = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True, check=False)
    return proc.returncode == 0


def tree_files(clone: Path, commit: str) -> dict[str, str]:
    """Path -> blob id of every file under records/ and data/ at a commit."""
    out: dict[str, str] = {}
    listing = git(clone, "ls-tree", "-r", commit, "--", mr.RECORDS_DIR, mr.DATA_DIR)
    for row in str(listing).splitlines():
        meta, _, path = row.partition("\t")
        out[path] = meta.split()[2]
    return out


def show(clone: Path, commit: str, path: str) -> bytes:
    return bytes(git(clone, "show", f"{commit}:{path}", binary=True))


# --------------------------------------------------------------------------
# Reading and checking
# --------------------------------------------------------------------------

def read_records(clone: Path, commit: str) -> list[Stored]:
    """Every record line at a commit, each checked against its rules and data."""
    files = tree_files(clone, commit)
    problems: list[str] = []
    stored: list[Stored] = []
    for path in sorted(p for p in files if p.startswith(f"{mr.RECORDS_DIR}/")):
        emulator = path[len(mr.RECORDS_DIR) + 1:].removesuffix(".jsonl")
        if not path.endswith(".jsonl") or "/" in emulator:
            problems.append(f"{path}: records/ holds <emulator>.jsonl files only")
            continue
        seen: set[str] = set()
        try:
            records = mr.parse_lines(show(clone, commit, path).decode("utf-8"), path)
        except mr.RecordError as exc:
            problems.append(str(exc))
            continue
        for number, record in enumerate(records, 1):
            where = f"{path}:{number}"
            problems.extend(f"{where}: {p}" for p in mr.check_line(record))
            if record.get("emulator") != emulator:
                problems.append(f"{where}: emulator {record.get('emulator')!r} in {path}")
            if record.get("id") in seen:
                problems.append(f"{where}: id {record.get('id')} appears twice")
            if record.get("supersedes") and record["supersedes"] not in seen:
                problems.append(f"{where}: supersedes {record['supersedes']}, no earlier line of this file")
            seen.add(record.get("id"))
            data = mr.sidecar_path(emulator, str(record.get("id")))
            if data not in files:
                problems.append(f"{where}: {data} is missing")
            else:
                problems.extend(f"{where}: {p}" for p in mr.check_pair(record, show(clone, commit, data)))
            stored.append(Stored(emulator, number, record))
    if problems:
        raise MeasurementsError("records at {}:\n  {}".format(commit[:12], "\n  ".join(problems)))
    return stored


def append_only_problems(clone: Path, old: str, new: str) -> list[str]:
    """What ``new`` changed or removed of the lines and data ``old`` holds."""
    before, after = tree_files(clone, old), tree_files(clone, new)
    problems = []
    for path, blob in sorted(before.items()):
        if path not in after:
            problems.append(f"{path}: removed")
        elif after[path] == blob:
            continue
        elif path.endswith(".jsonl"):
            moved = mr.appended_only(show(clone, old, path).decode("utf-8"),
                                     show(clone, new, path).decode("utf-8"))
            if moved:
                problems.append(f"{path}: {moved}")
        else:
            problems.append(f"{path}: rewritten")
    return problems


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------

def host_summary(host: dict) -> str:
    parts = [host.get("cpu_model") or "unknown CPU"]
    if host.get("cpu_count"):
        parts.append(f"{host['cpu_count']} CPUs")
    parts.append(f"{host.get('machine', '')} Linux {host.get('kernel', '')}".strip()
                 if "Linux" in host.get("os", "") else host.get("os", ""))
    parts.append(f"Python {host.get('python', '')}")
    before, after = host.get("load_before"), host.get("load_after")
    if before and after:
        parts.append(f"load {before[0]} before, {after[0]} after")
    return ", ".join(part for part in parts if part)


def bench_statement(record: dict) -> str:
    timing = record["timing"]
    frame, fps = timing["ms_per_frame"], timing["fps"]
    return (f"median {frame['median']:.3f} ms per frame ({fps['median']:.1f} fps), "
            f"min {frame['min']:.3f}, max {frame['max']:.3f}, over "
            f"{record['inputs']['repetitions']} repetitions of {record['inputs']['frames']} frames")


def permalink(repo: str, commit: str, path: str, line: int | None = None) -> str:
    return f"{repo}/blob/{commit}/{path}" + (f"#L{line}" if line else "")


def index_item(entry: Stored, runs: int, pin: dict, profiles: dict,
               system_names: dict[str, str]) -> dict:
    record = entry.record
    profile = profiles.get(entry.emulator) or {}
    check = record.get("check") or {}
    core = record["core"]
    content = (record["inputs"].get("content") or {})
    return {
        "emulator": entry.emulator,
        "emulator_name": str(profile.get("emulator", entry.emulator)),
        "profiled": bool(profile),
        "systems": sorted(set(profile.get("systems") or [])),
        "system_names": [system_names.get(s) or s for s in sorted(set(profile.get("systems") or []))],
        "subject": record["subject"],
        "kind": record["kind"],
        "claim": check.get("claim"),
        "method": check.get("method"),
        "observable": check.get("observable"),
        "variable": check.get("variable"),
        "status": (check.get("result") or {}).get("status") or ("measured" if record["kind"] == "bench" else "ran"),
        "statement": (check.get("result") or {}).get("statement")
                     or (bench_statement(record) if record.get("timing") else ""),
        "content": content.get("label"),
        "variants": len(record["variants"]),
        "repetitions": record["inputs"]["repetitions"],
        "flags": len(record["flags"]),
        "date": record["recorded_at"],
        "host": host_summary(record["host"]),
        "core": {
            "library": " ".join(filter(None, [core.get("library_name"), core.get("library_version")])),
            "file": core["file"], "sha256": core["sha256"], "repo": core.get("repo"),
            "commit": core.get("commit"), "build": core.get("build_command"),
        },
        "frontend_commit": record["frontend"].get("commit"),
        "id": record["id"],
        "runs": runs,
        "record_url": permalink(pin["repo"], pin["commit"], mr.records_path(entry.emulator), entry.line),
        "data_url": permalink(pin["repo"], pin["commit"], mr.sidecar_path(entry.emulator, record["id"])),
        "sqlite": f"downloads/measurements/{entry.emulator}.sqlite",
    }


def build_index(stored: list[Stored], pin: dict | None, profiles: dict,
                system_names: dict[str, str] | None = None) -> dict:
    """One item per (emulator, subject): its latest record not superseded."""
    by_subject: dict[tuple[str, str], list[Stored]] = {}
    live_ids = {r["id"] for r in mr.live(s.record for s in stored)}
    for entry in stored:
        if entry.record["id"] in live_ids:
            by_subject.setdefault((entry.emulator, entry.record["subject"]), []).append(entry)
    items = []
    for key in sorted(by_subject):
        entries = by_subject[key]
        latest = max(entries, key=lambda s: (s.record["recorded_at"], s.line))
        items.append(index_item(latest, len(entries), pin or {}, profiles, system_names or {}))
    items.sort(key=lambda item: (item["emulator"], item["date"], item["subject"]))
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "kind": "measurements",
        "repo": (pin or {}).get("repo"),
        "commit": (pin or {}).get("commit"),
        "records": len(stored),
        "count": len(items),
        "items": items,
    }


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

SCHEMA_SQL = """
    PRAGMA journal_mode = OFF;
    PRAGMA synchronous = OFF;
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE cores (
        sha256 TEXT PRIMARY KEY, file TEXT NOT NULL, repo TEXT, commit_sha TEXT,
        build_command TEXT, compiler TEXT, library_name TEXT, library_version TEXT
    );
    CREATE TABLE hosts (
        id INTEGER PRIMARY KEY, cpu_model TEXT, cpu_count INTEGER, os TEXT NOT NULL,
        kernel TEXT NOT NULL, machine TEXT NOT NULL, python TEXT NOT NULL
    );
    CREATE TABLE emulator_systems (
        emulator TEXT NOT NULL, system TEXT NOT NULL, PRIMARY KEY (emulator, system)
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY, emulator TEXT NOT NULL, subject TEXT NOT NULL,
        kind TEXT NOT NULL, recorded_at TEXT NOT NULL, supersedes TEXT,
        superseded INTEGER NOT NULL, core_sha256 TEXT NOT NULL REFERENCES cores(sha256),
        host_id INTEGER NOT NULL REFERENCES hosts(id), load_before TEXT, load_after TEXT,
        max_load REAL, frontend_commit TEXT, script_sha256 TEXT NOT NULL,
        script_modified INTEGER, flags TEXT NOT NULL, data_sha256 TEXT NOT NULL,
        data_bytes INTEGER NOT NULL, record_url TEXT NOT NULL, line INTEGER NOT NULL,
        migrated_from_version INTEGER
    );
    CREATE TABLE inputs (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), content_label TEXT, content_sha1 TEXT,
        content_size INTEGER, options TEXT NOT NULL, env TEXT NOT NULL, frames INTEGER NOT NULL,
        warmup INTEGER NOT NULL, repetitions INTEGER NOT NULL, presses TEXT NOT NULL,
        observables TEXT NOT NULL, frame_hash_every INTEGER NOT NULL, joypad_ports INTEGER NOT NULL
    );
    CREATE TABLE content_tracks (
        run_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL,
        file TEXT NOT NULL, sha1 TEXT NOT NULL, size INTEGER NOT NULL,
        PRIMARY KEY (run_id, position)
    );
    CREATE TABLE checks (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), claim TEXT NOT NULL, method TEXT NOT NULL,
        observable TEXT NOT NULL, variable TEXT NOT NULL, status TEXT NOT NULL,
        statement TEXT NOT NULL
    );
    CREATE TABLE check_groups (
        run_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL,
        value TEXT, value_sha1 TEXT, variants TEXT NOT NULL, PRIMARY KEY (run_id, position)
    );
    CREATE TABLE variants (
        run_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL,
        label TEXT NOT NULL, repetitions INTEGER NOT NULL, loaded INTEGER NOT NULL,
        flags TEXT NOT NULL, observables TEXT NOT NULL, files_opened TEXT,
        files_probed_absent TEXT, tracing TEXT, PRIMARY KEY (run_id, position)
    );
    CREATE TABLE system_files (
        run_id TEXT NOT NULL REFERENCES runs(id), variant_position INTEGER NOT NULL,
        destination TEXT NOT NULL, sha1 TEXT,
        PRIMARY KEY (run_id, variant_position, destination)
    );
    CREATE TABLE benchmarks (
        run_id TEXT NOT NULL REFERENCES runs(id), metric TEXT NOT NULL,
        median REAL NOT NULL, mean REAL NOT NULL, stdev REAL NOT NULL,
        min REAL NOT NULL, max REAL NOT NULL, PRIMARY KEY (run_id, metric)
    );
    CREATE INDEX runs_emulator_idx ON runs(emulator, subject);
    CREATE INDEX runs_subject_idx ON runs(subject);
    CREATE INDEX emulator_systems_system_idx ON emulator_systems(system);
    CREATE INDEX checks_claim_idx ON checks(claim);
    CREATE INDEX system_files_sha1_idx ON system_files(sha1);
"""

TABLES = ("meta", "cores", "hosts", "emulator_systems", "runs", "inputs", "content_tracks",
          "checks", "check_groups", "variants", "system_files", "benchmarks")


def _host_key(host: dict) -> tuple:
    return tuple(json_text(host.get(k)) for k in ("cpu_model", "cpu_count", "os", "kernel",
                                                     "machine", "python"))


def write_sqlite(destination: Path, stored: list[Stored], pin: dict | None, profiles: dict,
                 scratch: Path) -> None:
    """A deterministic database of the records: sorted rows, no clock."""
    scratch.mkdir(parents=True, exist_ok=True)
    temp = scratch / f"{destination.stem}-{os.getpid()}.sqlite"
    temp.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stored = sorted(stored, key=lambda s: (s.emulator, s.line))
    live_ids = {r["id"] for r in mr.live(s.record for s in stored)}
    hosts = {key: number for number, key in enumerate(sorted({_host_key(s.record["host"]) for s in stored}), 1)}
    connection = sqlite3.connect(temp)
    try:
        connection.executescript(SCHEMA_SQL)
        meta = {
            "schema_version": SQLITE_SCHEMA_VERSION,
            "record_version": str(mr.RECORD_VERSION),
            "measurements_repo": (pin or {}).get("repo") or "",
            "measurements_commit": (pin or {}).get("commit") or "",
            "license": "MIT",
            "scope": "core probe records: the record lines, not their data files",
        }
        connection.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
        cores = {s.record["core"]["sha256"]: s.record["core"] for s in stored}
        connection.executemany("INSERT INTO cores VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            (sha, core["file"], json_text(core.get("repo")), json_text(core.get("commit")),
             json_text(core.get("build_command")), json_text(core.get("compiler")),
             json_text(core.get("library_name")), json_text(core.get("library_version")))
            for sha, core in sorted(cores.items())])
        connection.executemany("INSERT INTO hosts VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (number, *key) for key, number in sorted(hosts.items(), key=lambda kv: kv[1])])
        emulators = sorted({s.emulator for s in stored})
        connection.executemany("INSERT INTO emulator_systems VALUES (?, ?)", [
            (emu, system) for emu in emulators
            for system in sorted(set((profiles.get(emu) or {}).get("systems") or []))])
        for entry in stored:
            _insert_run(connection, entry, pin or {}, hosts, entry.record["id"] not in live_ids)
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    os.replace(temp, destination)


def _insert_run(connection: sqlite3.Connection, entry: Stored, pin: dict,
                hosts: dict, superseded: bool) -> None:
    r = entry.record
    host, front, inputs = r["host"], r["frontend"], r["inputs"]
    rid = r["id"]
    connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, entry.emulator, r["subject"], r["kind"], r["recorded_at"], r.get("supersedes"),
        int(superseded), r["core"]["sha256"], hosts[_host_key(host)],
        json_text(host.get("load_before")), json_text(host.get("load_after")), host.get("max_load"),
        front.get("commit"), front["script_sha256"],
        None if front.get("script_modified") is None else int(front["script_modified"]),
        json_text(r["flags"]), r["data"]["sha256"], r["data"]["bytes"],
        permalink(pin.get("repo", ""), pin.get("commit", ""), mr.records_path(entry.emulator), entry.line),
        entry.line, r.get("migrated_from_version")))
    content = inputs.get("content") or {}
    connection.execute("INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, content.get("label"), content.get("sha1"), content.get("size"),
        json_text(inputs["options"]), json_text(inputs["env"]), inputs["frames"], inputs["warmup"],
        inputs["repetitions"], json_text(inputs["presses"]), json_text(inputs["observables"]),
        inputs["frame_hash_every"], inputs["joypad_ports"]))
    connection.executemany("INSERT INTO content_tracks VALUES (?, ?, ?, ?, ?)", [
        (rid, position, track["file"], track["sha1"], track["size"])
        for position, track in enumerate(content.get("tracks") or [])])
    check = r.get("check")
    if check:
        result = check["result"]
        connection.execute("INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?)", (
            rid, check["claim"], check["method"], check["observable"], check["variable"],
            result["status"], result["statement"]))
        connection.executemany("INSERT INTO check_groups VALUES (?, ?, ?, ?, ?)", [
            (rid, position, json_text(group["value"]) if "value" in group else None,
             group.get("value_sha1"), json_text(group["variants"]))
            for position, group in enumerate(result["groups"])])
    for position, variant in enumerate(r["variants"]):
        connection.execute("INSERT INTO variants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            rid, position, variant["label"], variant["repetitions"], int(variant["loaded"]),
            json_text(variant["flags"]), json_text(variant["observables"]),
            json_text(variant["files_opened"]), json_text(variant["files_probed_absent"]),
            json_text(variant["tracing"])))
        connection.executemany("INSERT INTO system_files VALUES (?, ?, ?, ?)", [
            (rid, position, dest, sha1) for dest, sha1 in sorted(variant["system_files"].items())])
    for metric, stats in sorted((r.get("timing") or {}).items()):
        connection.execute("INSERT INTO benchmarks VALUES (?, ?, ?, ?, ?, ?, ?)", (
            rid, metric, stats["median"], stats["mean"], stats["stdev"], stats["min"], stats["max"]))


# --------------------------------------------------------------------------
# Building the site's files
# --------------------------------------------------------------------------

@dataclass
class Built:
    index: dict
    paths: list[Path]


def load_profiles(emulators_dir: Path) -> dict:
    from common import load_emulator_profiles  # noqa: PLC0415

    return load_emulator_profiles(str(emulators_dir), skip_aliases=False)


def records_at_pin(clone: Path, pin: dict, require: bool) -> list[Stored] | None:
    """The records at the pin, or None when the clone does not hold it."""
    if has_commit(clone, pin["commit"]):
        return read_records(clone, pin["commit"])
    message = (f"measurements commit {pin['commit'][:12]} is not in {clone}: run "
               f"python scripts/sync_measurements.py --fetch")
    if require:
        raise MeasurementsError(message)
    print(f"WARNING: {message}. The site lists no measurements.", file=sys.stderr)
    return None


def build(public: Path, clone: Path = CLONE, pin_path: Path = PIN_FILE,
          emulators_dir: Path = ROOT / "emulators", require: bool = False,
          scratch: Path = ROOT / "tmp" / "site",
          system_names: dict[str, str] | None = None) -> Built:
    """Write the index and the databases under the site's public folder."""
    pin = load_pin(pin_path)
    stored = records_at_pin(clone, pin, require)
    profiles = load_profiles(emulators_dir)
    if stored is None:
        pin = None
    index = build_index(stored or [], pin, profiles, system_names)
    paths = [public / INDEX_PATH]
    (public / INDEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(str(public / INDEX_PATH),
                     json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    write_sqlite(public / SQLITE_PATH, stored or [], pin, profiles, scratch)
    paths.append(public / SQLITE_PATH)
    for emulator in sorted({s.emulator for s in stored or []}):
        target = public / PER_EMULATOR_DIR / f"{emulator}.sqlite"
        write_sqlite(target, [s for s in stored or [] if s.emulator == emulator], pin, profiles, scratch)
        paths.append(target)
    return Built(index, paths)


def fetch(clone: Path, pin: dict) -> str:
    """Clone or update the measurements repository. Returns the remote head."""
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["git", "clone", "--quiet", pin["repo"], str(clone)],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise MeasurementsError(f"git clone {pin['repo']}: {proc.stderr.strip()}")
    else:
        git(clone, "fetch", "--quiet", "origin")
    head = str(git(clone, "rev-parse", "refs/remotes/origin/HEAD")).strip()
    branch = str(git(clone, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    if branch != "HEAD":
        proc = subprocess.run(["git", "-C", str(clone), "merge", "--ff-only", "--quiet", head],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            print(f"WARNING: {clone} has local commits, its branch was left as is: "
                  f"{proc.stderr.strip()}", file=sys.stderr)
    return head


def remote_head(repo: str) -> str:
    proc = subprocess.run(["git", "ls-remote", repo, "HEAD"], capture_output=True, text=True,
                          check=False, timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise MeasurementsError(f"git ls-remote {repo}: {proc.stderr.strip() or 'no HEAD'}")
    return proc.stdout.split()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__.split("\n\n")[0])
    parser.add_argument("--fetch", action="store_true",
                        help="clone or update the clone, and check the remote kept every line the pin holds")
    parser.add_argument("--bump", action="store_true",
                        help=f"after --fetch, move {PIN_FILE.name} to the fetched head")
    parser.add_argument("--check-only", action="store_true",
                        help="check the records at the pin and write nothing")
    parser.add_argument("--clone", default=str(CLONE), help="the clone (default: %(default)s)")
    parser.add_argument("--pin", default=str(PIN_FILE), help="the pin file (default: %(default)s)")
    parser.add_argument("--site-dir", default=str(ROOT / "website"),
                        help="the site project whose public/ folder is written (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.bump and not args.fetch:
        parser.error("--bump needs --fetch")
    clone, pin_path = Path(args.clone), Path(args.pin)
    try:
        pin = load_pin(pin_path)
        if args.fetch:
            head = fetch(clone, pin)
            problems = append_only_problems(clone, pin["commit"], head) \
                if has_commit(clone, pin["commit"]) else [f"pin {pin['commit']} is not in the remote history"]
            if problems:
                print("ERROR: the remote changed records the pin holds:\n  " + "\n  ".join(problems),
                      file=sys.stderr)
                return 1
            records = read_records(clone, head)
            pinned = len(read_records(clone, pin["commit"]))
            print(f"{head[:12]}: {len(records)} records, {len(records) - pinned} after the pin "
                  f"{pin['commit'][:12]}")
            if args.bump and head != pin["commit"]:
                write_pin({**pin, "commit": head}, pin_path)
                print(f"pin moved to {head}")
            return 0
        if args.check_only:
            stored = records_at_pin(clone, pin, require=True)
            print(f"{pin['commit'][:12]}: {len(stored or [])} records, every line and data file checked")
            return 0
        built = build(Path(args.site_dir) / "public", clone, pin_path, require=True)
        print(f"{built.index['records']} records, {built.index['count']} subjects: "
              + ", ".join(str(p.relative_to(Path(args.site_dir))) for p in built.paths))
        return 0
    except MeasurementsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
