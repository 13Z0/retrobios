"""The measurements the site shows, from the records under measurements/.

The records are core probe runs, one append-only .jsonl per emulator with one
full record per line (measurement_records.py). From them the build writes,
never committed:

  public/data/measurements/index.json        the compact index the explorer and
                                             the emulator pages read, one item
                                             per emulator and subject
  public/downloads/measurements.sqlite       every record, normalised
  public/downloads/measurements/<emu>.sqlite one database per emulator

Three places read the index: the Tested block of each emulator page, the
Measurements explorer page and the host table of the methodology page. A
number is never shown without its method and host beside it. A record links
its line at the last commit that touched measurements/.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import measurement_records as mr
import sitemarkup as markup
from artifacts import json_text, write_if_changed
from siterender import _escape_tags

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/Abdess/retrobios"
EXPLORER_PAGE = "measurements.md"
METHOD_PAGE = "wiki/measurements.md"
HOSTS_MARKER = "<!-- measurements-hosts -->"
INDEX_PATH = Path("data") / "measurements" / "index.json"
SQLITE_PATH = Path("downloads") / "measurements.sqlite"
PER_EMULATOR_DIR = Path("downloads") / "measurements"
SQLITE_SCHEMA_VERSION = "3"
# Observables the variants table leaves out: one value per sampled frame.
BULKY_OBSERVABLES = ("frame_hashes",)
INDEX_SCHEMA_VERSION = 1


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


def verdicts_of(record: dict) -> list[dict]:
    """Each variant's boot verdict, from record version 4 on."""
    return [{"variant": v["label"], "verdict": v["verdict"],
             "detail": (v.get("verdict_evidence") or {}).get("detail", "")}
            for v in record["variants"] if "verdict" in v]


def result_of(record: dict) -> tuple[str, str]:
    """The status and statement a page shows for a record.

    A run, or a check compared on the sampled frames, is shown by its boot
    verdicts: one verdict when every variant has it, else `mixed`, each
    variant named in the statement. A check on another observable keeps
    its comparison. A frame comparison recorded before the boot oracle
    shows `no verdict`: frames that differ prove nothing about a boot.
    """
    check = record.get("check") or {}
    result = check.get("result") or {}
    if record["kind"] == "bench":
        return "measured", bench_statement(record) if record.get("timing") else ""
    verdicts = verdicts_of(record)
    by_frames = record["kind"] == "run" or check.get("observable") == "frame_hashes"
    if verdicts and by_frames:
        values = {v["verdict"] for v in verdicts}
        status = values.pop() if len(values) == 1 else "mixed"
        if record["kind"] == "run":
            return status, verdicts[0]["detail"]
        return status, ". ".join(f"{v['variant']}: {v['verdict']}" for v in verdicts)
    if by_frames:
        return "no verdict", ("recorded before the boot oracle. Its frames were compared, which "
                              "settles nothing about a boot")
    return result.get("status") or "ran", result.get("statement") or ""


def index_item(entry: mr.Stored, runs: int, source: dict, profiles: dict,
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
        "status": result_of(record)[0],
        "statement": result_of(record)[1],
        "verdicts": verdicts_of(record),
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
        "record_url": permalink(source["repo"], source["commit"], mr.records_path(entry.emulator), entry.line),
        "sqlite": f"downloads/measurements/{entry.emulator}.sqlite",
    }


def build_index(stored: list[mr.Stored], source: dict | None, profiles: dict,
                system_names: dict[str, str] | None = None) -> dict:
    """One item per (emulator, subject): its latest record not superseded."""
    by_subject: dict[tuple[str, str], list[mr.Stored]] = {}
    live_ids = {r["id"] for r in mr.live(s.record for s in stored)}
    for entry in stored:
        if entry.record["id"] in live_ids:
            by_subject.setdefault((entry.emulator, entry.record["subject"]), []).append(entry)
    items = []
    for key in sorted(by_subject):
        entries = by_subject[key]
        latest = max(entries, key=lambda s: (s.record["recorded_at"], s.line))
        items.append(index_item(latest, len(entries), source or {}, profiles, system_names or {}))
    items.sort(key=lambda item: (item["emulator"], item["date"], item["subject"]))
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "kind": "measurements",
        "repo": (source or {}).get("repo"),
        "commit": (source or {}).get("commit"),
        "records": len(stored),
        "count": len(items),
        "items": items,
    }


def campaign_summary(root: Path, stored: list[mr.Stored], source: dict | None) -> dict:
    """The last campaign pass and the findings still waiting for review.

    A finding's history is its lines under one key: the latest line is its
    state, and only an open or flagged one is listed.
    """
    runs = mr.read_campaign(root, mr.CAMPAIGN_RUNS)
    latest: dict[str, dict] = {}
    applied = 0
    for line in mr.read_campaign(root, mr.FINDINGS):
        latest[line["key"]] = line
        applied += line["status"] == "applied"
    where = {s.record["id"]: s for s in stored}
    links = {}
    for line in latest.values():
        for rid in line["evidence"]:
            found = where.get(rid)
            if found and source and source.get("commit"):
                links[rid] = permalink(source["repo"], source["commit"],
                                       mr.records_path(found.emulator), found.line)
    open_items = [{**line, "links": [links.get(rid) for rid in line["evidence"]]}
                  for _, line in sorted(latest.items()) if line["status"] in ("open", "flagged")]
    return {"last_run": runs[-1] if runs else None, "passes": len(runs), "applied": applied,
            "open": open_items}


# Characters a campaign value could use to leave its place on the page: HTML
# and MDX syntax ({ } opens an expression), code spans, emphasis and links.
_UNSAFE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "{": "&#123;", "}": "&#125;", "`": "&#96;",
           "[": "&#91;", "]": "&#93;", "*": "&#42;", "_": "&#95;", "\\": "&#92;", "|": "&#124;"}


def _plain(value: object) -> str:
    """Text a run produced (core output, a romfetch error, a profile name),
    shown as text: every character that is markup on the page is a
    character reference."""
    text = " ".join(str(value if value is not None else "").split())
    return "".join(_UNSAFE.get(char, char) for char in text)


def _sentence(value: object) -> str:
    """A finding's statement as a sentence of the page: capital first."""
    text = _plain(value)
    return text[:1].upper() + text[1:]


def _code(value: object) -> str:
    """A value as code that cannot close its element or open an expression."""
    return f"<code>{_plain(value)}</code>"


def _value(value: object) -> str:
    return _code(json.dumps(value, ensure_ascii=False)) if value is not None else "nothing"


def campaign_section(summary: dict | None) -> list[str]:
    """The campaign's last pass and its open findings, for the explorer page."""
    if not summary or not summary.get("last_run"):
        return []
    last = summary["last_run"]
    counts = last["counts"]
    lines = [
        "## Campaign",
        "",
        f"A maintainer starts a pass, on an idle computer, and nothing runs by itself "
        f"([how]({METHOD_PAGE})). "
        f"Last pass: {_plain(last['ended_at'][:10])}, {_plain(last['result'])}"
        + (f" ({_plain(last['detail'])})" if last.get("detail") else "") + ". "
        f"{int(counts['done'])} tests done, {int(counts['failed'])} failed, {int(counts['pending'])} waiting. "
        f"{summary['applied']} profile fields were set from a run.",
        "",
    ]
    if last["pending_reasons"]:
        lines += ["| Waiting for | Tests |", "|-------------|------:|"]
        lines += [f"| {_plain(reason)} | {int(number)} |"
                  for reason, number in sorted(last["pending_reasons"].items(), key=lambda kv: (-kv[1], kv[0]))]
        lines.append("")
    if last["failed"]:
        lines += ["Failed tests:", ""]
        lines += [f"- {_plain(item['emulator'])}, {_code(item['check'])}: {_plain(item['reason'])}"
                  for item in last["failed"]]
        lines.append("")
    lines += ["### Open findings", ""]
    if not summary["open"]:
        lines += ["None: every finding was settled by a rule or resolved by a later run.", ""]
        return lines
    lines += ["A finding is what a run showed that no rule settles. It waits for someone to "
              "read the emulator's code.", ""]
    for item in summary["open"]:
        records = ", ".join(f"[{_plain(rid)}]({link})" if link else _code(rid)
                            for rid, link in zip(item["evidence"], item["links"]))
        lines += [
            f"- **{_plain(item['emulator'])}**, {_code(item['file'])}, {_code(item['field'])} "
            f"({_plain(item['status'])}): the profile says {_value(item['profile_says'])}, the run "
            f"shows {_value(item['run_shows'])}. {_sentence(item['statement'])}. Records: {records}.",
            "",
        ]
    return lines


# --------------------------------------------------------------------------
# Coverage and region results
# --------------------------------------------------------------------------

CONTENT_FILE = Path("tests") / "campaign" / "content.yml"


def coverage(root: Path, stored: list[mr.Stored], profiles: dict | None = None) -> dict:
    """Per system of the content list: its games, and which emulators ran
    each one. A record counts for a system only when its emulator's profile
    names that system: one catalog name can be a PlayStation disc and a
    Saturn disc. A system no catalog covers comes with the reason and the
    error romfetch returned."""
    import yaml  # noqa: PLC0415

    path = root / CONTENT_FILE
    if not path.is_file():
        return {"systems": [], "uncovered": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ran: dict[str, set[str]] = {}
    for entry in stored:
        label = ((entry.record.get("inputs") or {}).get("content") or {}).get("label")
        if label:
            ran.setdefault(label, set()).add(entry.emulator)
    systems = []
    for system, spec in (data.get("systems") or {}).items():
        games = [{"name": item["name"], "region": item.get("region"),
                  "emulators": sorted(e for e in ran.get(item["name"], ())
                                      if profiles is None
                                      or system in ((profiles.get(e) or {}).get("systems") or ()))}
                 for item in spec.get("content") or ()]
        listed = {item["name"] for item in spec.get("content") or ()}
        # Runs of this system's emulators on games the list does not name.
        other = sorted({e for label, emulators in ran.items() if label not in listed for e in emulators
                        if profiles is not None
                        and system in ((profiles.get(e) or {}).get("systems") or ())})
        systems.append({"system": system, "games": games, "other_games_by": other,
                        "tested": any(game["emulators"] for game in games)})
    uncovered = [{"system": system, "want": item.get("want"), "reason": " ".join(str(item["reason"]).split()),
                  "romfetch": item.get("romfetch") or {}}
                 for system, item in (data.get("uncovered") or {}).items()]
    return {"systems": systems, "uncovered": uncovered}


def coverage_section(cov: dict | None, system_names: dict[str, str] | None = None) -> list[str]:
    """The Coverage block of the explorer page: tested or not, and why not."""
    if not cov or not (cov.get("systems") or cov.get("uncovered")):
        return []
    names = system_names or {}
    tested = [s for s in cov["systems"] if s["tested"]]
    lines = [
        "## Coverage",
        "",
        f"Test games come from [`{CONTENT_FILE.as_posix()}`]({REPO}/blob/main/{CONTENT_FILE.as_posix()}), "
        f"one per region where a BIOS depends on it. {len(tested)} of {len(cov['systems'])} listed "
        f"systems have a recorded run, and {len(cov['uncovered'])} "
        f"{'system has' if len(cov['uncovered']) == 1 else 'systems have'} no test game.",
        "",
        "| System | Games | Tested by |",
        "|--------|-------|-----------|",
    ]
    # Two ids of one console (sega-megacd, mega-cd) share their list: one row.
    rows: dict[tuple[str, str], set[str]] = {}
    others: dict[tuple[str, str], set[str]] = {}
    for item in cov["systems"]:
        row = (_plain(names.get(item["system"], item["system"])),
               ", ".join(_plain(game["name"]) for game in item["games"]))
        rows.setdefault(row, set()).update(e for game in item["games"] for e in game["emulators"])
        others.setdefault(row, set()).update(item.get("other_games_by") or ())
    for (name, games), by in rows.items():
        if by:
            status = ", ".join(_plain(e) for e in sorted(by))
        elif others[(name, games)]:
            status = ("not yet with these games, other games by "
                      + ", ".join(_plain(e) for e in sorted(others[(name, games)])))
        else:
            status = "not yet: no run recorded"
        lines.append(f"| {name} | {games} | {status} |")
    lines.append("")
    if cov["uncovered"]:
        lines += ["Systems without a test game:", "",
                  "| System | Wanted | Why not | romfetch |",
                  "|--------|--------|---------|----------|"]
        for item in cov["uncovered"]:
            code = item["romfetch"].get("code") or "no source"
            lines.append(f"| {_plain(names.get(item['system'], item['system']))} | {_plain(item['want'])} | "
                         f"{_plain(item['reason'])} | exit {_plain(item['romfetch'].get('exit'))}, "
                         f"{_code(code)} |")
        lines.append("")
    return lines


def region_results(root: Path, stored: list[mr.Stored], source: dict | None) -> list[dict]:
    """The region outcomes the records hold (region_check.py), each record
    linked at the commit the explorer links."""
    import region_check  # noqa: PLC0415

    lines = {s.record["id"]: s for s in stored}
    results = region_check.collect(root, stored)
    for result in results:
        for item in result["variants"].values():
            found = lines.get(item["record"])
            item["url"] = (permalink(source["repo"], source["commit"], mr.records_path(found.emulator),
                                     found.line)
                           if found and source and source.get("commit") else None)
    return results


def site_region_results(root: Path = ROOT, repo: str = REPO) -> list[dict]:
    """region_results with the records and commit the explorer uses."""
    stored = mr.read_records(root)
    source = {"repo": repo, "commit": records_commit(root) if stored else None}
    return region_results(root, stored, source)


VARIANT_WORDS = {"full": "every regional BIOS", "own": "its own region's BIOS only",
                 "others": "the other regions' BIOS only", "pack": "the region's pack"}


def region_section(results: list[dict]) -> list[str]:
    """What the region runs showed for one system, with the records."""
    if not results:
        return []
    lines = ["### Region check", "",
             f"Each game ran with {VARIANT_WORDS['own']}, then with {VARIANT_WORDS['others']} "
             f"([method](../{METHOD_PAGE})).", ""]
    for result in results:
        links = ", ".join(
            f"[{VARIANT_WORDS[label]}]({item['url']})" if item.get("url") else _plain(VARIANT_WORDS[label])
            for label, item in result["variants"].items())
        read = (f" It reads {', '.join(_code(name) for name in result['selected'])} "
                f"when every regional BIOS is there ({_plain(result['claim'])} against the profile)."
                if result["selected"] else "")
        basis = ("" if result["judged_by"] == "verdict"
                 else " Judged against the run with every regional BIOS until reference frames are recorded.")
        lines.append(f"- **{_plain(result['core'])}**, {_plain(result['game'])}: "
                     f"{_plain(result['sentence'])}{read}{basis} Records: {links}.")
    lines.append("")
    return lines


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
        script_modified INTEGER, flags TEXT NOT NULL, record_url TEXT NOT NULL,
        line INTEGER NOT NULL,
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
        files_probed_absent TEXT, tracing TEXT, verdict TEXT, verdict_evidence TEXT,
        PRIMARY KEY (run_id, position)
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


def write_sqlite(destination: Path, stored: list[mr.Stored], source: dict | None, profiles: dict,
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
            "measurements_repo": (source or {}).get("repo") or "",
            "measurements_commit": (source or {}).get("commit") or "",
            "license": "MIT",
            "scope": "core probe records, one per line of measurements/<emulator>.jsonl",
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
            _insert_run(connection, entry, source or {}, hosts, entry.record["id"] not in live_ids)
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    os.replace(temp, destination)


def _insert_run(connection: sqlite3.Connection, entry: mr.Stored, source: dict,
                hosts: dict, superseded: bool) -> None:
    r = entry.record
    host, front, inputs = r["host"], r["frontend"], r["inputs"]
    rid = r["id"]
    connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, entry.emulator, r["subject"], r["kind"], r["recorded_at"], r.get("supersedes"),
        int(superseded), r["core"]["sha256"], hosts[_host_key(host)],
        json_text(host.get("load_before")), json_text(host.get("load_after")), host.get("max_load"),
        front.get("commit"), front["script_sha256"],
        None if front.get("script_modified") is None else int(front["script_modified"]),
        json_text(r["flags"]),
        permalink(source.get("repo", ""), source.get("commit", ""), mr.records_path(entry.emulator), entry.line),
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
            (rid, position, json_text(group["value"]),
             mr.value_sha1(group["value"]) if isinstance(group["value"], (list, dict)) else None,
             json_text(group["variants"]))
            for position, group in enumerate(result["groups"])])
    for position, variant in enumerate(r["variants"]):
        reps = variant["repetitions"]
        first = reps[0]
        # The first repetition stands for the variant, its per-frame hashes left out.
        observables = {key: value for key, value in (first.get("observables") or {}).items()
                       if key not in BULKY_OBSERVABLES}
        connection.execute("INSERT INTO variants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            rid, position, variant["label"], len(reps), int(all(rep.get("loaded") for rep in reps)),
            json_text(variant["flags"]), json_text(observables),
            json_text(first.get("files_opened")), json_text(first.get("files_probed_absent")),
            json_text(first.get("tracing")), variant.get("verdict"),
            json_text(variant.get("verdict_evidence")) if "verdict_evidence" in variant else None))
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


def records_commit(root: Path) -> str | None:
    """The commit the record links name, with a warning when the records on
    disk are not the ones it holds."""
    commit = mr.last_commit(root)
    if commit is None:
        print(f"WARNING: no commit holds {root / mr.RECORDS_DIR}: the record links "
              "lead nowhere until the records are committed.", file=sys.stderr)
        return None
    on_disk = {path: file.read_bytes() for path, file in mr.tree_files(root).items()}
    if mr.committed_files(root, commit) != on_disk:
        print(f"WARNING: {mr.RECORDS_DIR}/ differs from {commit[:12]}, the last commit that "
              "touched it: links to records added since lead nowhere until they are "
              "committed.", file=sys.stderr)
    return commit


def build(public: Path, root: Path = ROOT, emulators_dir: Path = ROOT / "emulators",
          scratch: Path = ROOT / "tmp" / "site", system_names: dict[str, str] | None = None,
          repo: str = REPO, commit: str | None = None) -> Built:
    """Write the index and the databases under the site's public folder.

    ``commit`` is the revision the record links name, by default the last
    commit that touched the records.
    """
    stored = mr.read_records(root)
    source = {"repo": repo, "commit": commit or (records_commit(root) if stored else None)}
    profiles = load_profiles(emulators_dir)
    index = build_index(stored, source, profiles, system_names)
    index["campaign"] = campaign_summary(root, stored, source)
    index["coverage"] = coverage(root, stored, profiles)
    paths = [public / INDEX_PATH]
    (public / INDEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(str(public / INDEX_PATH),
                     json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    write_sqlite(public / SQLITE_PATH, stored, source, profiles, scratch)
    paths.append(public / SQLITE_PATH)
    for emulator in sorted({s.emulator for s in stored}):
        target = public / PER_EMULATOR_DIR / f"{emulator}.sqlite"
        write_sqlite(target, [s for s in stored if s.emulator == emulator], source, profiles, scratch)
        paths.append(target)
    return Built(index, paths)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def by_emulator(index: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in index.get("items", []):
        out.setdefault(item["emulator"], []).append(item)
    return out


def _text(value: object) -> str:
    """Record text as the page shows it. A semicolon joining two clauses
    ends a sentence on the site: the record keeps the words as written."""
    text = " ".join(str(value or "").split())
    text = re.sub(r";\s+(\w)", lambda m: ". " + m[1].upper(), text)
    return _escape_tags(text)


def _short(commit: str | None) -> str:
    return f"`{commit[:12]}`" if commit else "unknown"


def tested_item(item: dict) -> list[str]:
    """One test: what ran, the result, the method and host beside it, the record."""
    title = item["claim"] or f"Benchmark of {item['content'] or 'the core alone'}"
    runs = "1 run" if item["runs"] == 1 else f"{item['runs']} runs, the latest shown"
    core = item["core"]
    build = f"{_text(core['library'])} at {_short(core['commit'])}" if core.get("commit") \
        else _text(core["library"])
    lines = [f"- **{_text(title)}**", ""]
    if len(item.get("verdicts") or ()) > 1:
        lines += [f"    **Result:** `{item['status']}`.", ""]
        lines += [f"    - {_text(v['variant'])}: `{v['verdict']}`. {_text(v['detail'])}."
                  for v in item["verdicts"]]
        lines.append("")
    else:
        lines += [f"    **Result:** `{item['status']}`. {_text(item['statement'])}.", ""]
    if item.get("method"):
        lines += [f"    **Method:** {_text(item['method'])}", ""]
    lines += [
        f"    **Ran on:** {_text(item['host'])}, {item['date'][:10]}. Core build: {build}. "
        f"{item['variants']} variant{'s' if item['variants'] != 1 else ''}, "
        f"{item['repetitions']} repetition{'s' if item['repetitions'] != 1 else ''} each"
        + (f", {item['flags']} flag{'s' if item['flags'] != 1 else ''} raised" if item["flags"] else "")
        + ".",
        "",
        f"    [Full record]({item['record_url']}), {runs}.",
        "",
    ]
    return lines


def tested_section(name: str, items: list[dict]) -> list[str]:
    """The Tested block of an emulator page, empty when nothing was recorded."""
    if not items:
        return []
    lines = [
        "## Tested",
        "",
        f"This emulator was run in real conditions by the [core probe](../{METHOD_PAGE}). "
        f"Each test below is the latest of its kind. "
        f"[All measurements](../{EXPLORER_PAGE}), "
        f"[these records as SQLite](../downloads/measurements/{name}.sqlite).",
        "",
    ]
    for item in sorted(items, key=lambda i: (i["date"], i["subject"]), reverse=True):
        lines += tested_item(item)
    return lines


def hosts_table(index: dict) -> list[str]:
    hosts: dict[str, int] = {}
    for item in index.get("items", []):
        # The load average differs per run: the computer is what comes before it.
        computer = item["host"].split(", load ")[0]
        hosts[computer] = hosts.get(computer, 0) + 1
    if not hosts:
        return ["No result is recorded yet.", ""]
    return [
        "| Computer | Tests |",
        "|----------|------:|",
        *(f"| {_text(host)} | {count} |" for host, count in sorted(hosts.items())),
        "",
    ]


def with_hosts(page: str, index: dict) -> str:
    """The methodology page with the computers the results ran on."""
    return page.replace(HOSTS_MARKER, "\n".join(hosts_table(index)).rstrip("\n"))


def generate_explorer_page(index: dict, system_names: dict[str, str] | None = None) -> str:
    commit = index.get("commit")
    repo = index.get("repo") or ""
    source = (f"[`measurements/`]({repo}/tree/{commit}/{mr.RECORDS_DIR}) at {_short(commit)}"
              if commit else f"`{mr.RECORDS_DIR}/` of this repository")
    return "\n".join([
        "# Measurements",
        "",
        f"Emulators run in real conditions: {index.get('records', 0)} records, "
        f"{index.get('count', 0)} tests, read from {source}. "
        f"[How they are made]({METHOD_PAGE}).",
        "",
        " ".join([
            markup.button("SQLite of every record", "downloads/measurements.sqlite", primary=True),
            markup.button("Index as JSON", "data/measurements/index.json"),
        ]),
        "",
        "Each row is the latest record of one test, with its method and the computer it "
        "ran on. One SQLite file per emulator is linked from its page.",
        "",
        "<MeasurementsExplorer />",
        "",
        *campaign_section(index.get("campaign")),
        *coverage_section(index.get("coverage"), system_names),
    ])


def sqlite_tables(path: Path) -> list[tuple[str, list[str]]]:
    """Each table of a database with its columns, in the order created."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
        return [(name, [row[1] for row in connection.execute(f"PRAGMA table_info({name})")])
                for name in names]
    finally:
        connection.close()


def data_page_section(path: Path) -> list[str]:
    """The Data page's description of measurements.sqlite."""
    return [
        "## Measurements",
        "",
        "[`measurements.sqlite`](downloads/measurements.sqlite) holds every core probe "
        f"record kept in [`{mr.RECORDS_DIR}/`]({REPO}/tree/main/{mr.RECORDS_DIR}), and "
        "`downloads/measurements/<emulator>.sqlite` the records of one emulator, linked "
        f"from its page. The [Measurements]({EXPLORER_PAGE}) page reads "
        "[`data/measurements/index.json`](data/measurements/index.json). A record line is "
        "valid against [`core-probe-record.schema.json`](api/v1/schemas/core-probe-record.schema.json).",
        "",
        "| Table | Columns |",
        "|-------|---------|",
        *(f"| `{name}` | {', '.join(f'`{c}`' for c in columns)} |" for name, columns in sqlite_tables(path)),
        "",
        "`meta` names the commit the record links point to and the record version. A list or object "
        "value is JSON text, an absent one empty. `runs.superseded` is 1 for a record a "
        "later one corrects.",
        "",
        "```sql",
        "SELECT r.emulator, c.claim, c.status, r.recorded_at, h.cpu_model",
        "FROM runs r JOIN checks c ON c.run_id = r.id JOIN hosts h ON h.id = r.host_id",
        "WHERE r.superseded = 0 ORDER BY r.recorded_at;",
        "```",
        "",
    ]
