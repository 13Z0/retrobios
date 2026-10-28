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

import html
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
        "host_short": short_host(record["host"]),
        "setup": setup_of(record),
        "groups": variant_groups(record),
        "proves": proves(record["id"], profile),
        "timing": ({"ms_per_frame": record["timing"]["ms_per_frame"]["median"],
                    "fps": record["timing"]["fps"]["median"],
                    "stdev": record["timing"]["ms_per_frame"]["stdev"]}
                   if record.get("timing") else None),
    }


def with_title(item: dict) -> dict:
    return {**item, "title": short_title(item)}


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
        items.append(with_title(index_item(latest, len(entries), source or {}, profiles,
                                           system_names or {})))
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


def pack_summary(root: Path, source: dict | None) -> list[dict]:
    """The latest pack probe of each platform, install path, system, core
    and game, with a link to its line."""
    latest: dict[tuple, tuple[int, dict]] = {}
    for number, line in enumerate(mr.read_campaign(root, mr.PACK_PROBES), 1):
        key = (line["platform"], line["install"], line["system"], line["core"]["file"],
               line["content"]["label"])
        latest[key] = (number, line)
    items = []
    for key in sorted(latest):
        number, line = latest[key]
        url = (permalink(source["repo"], source["commit"], mr.records_path(mr.PACK_PROBES), number)
               if source and source.get("commit") else None)
        items.append({
            "platform": line["platform"], "platform_version": line["platform_version"],
            "install": line["install"], "system": line["system"], "emulator": line["emulator"],
            "core": line["core"]["file"].removesuffix("_libretro.so"),
            "content": line["content"]["label"], "verdict": line["verdict"],
            "date": line["probed_at"], "opened": len(line["files_opened"]),
            "findings": len(line["findings"]), "id": line["id"], "record_url": url,
            "files": [f"{f['path']} ({f['entry'] or 'written by the core'})" for f in line["files_opened"]],
            "finding_list": [f"{f['kind']} {f['path']}" for f in line["findings"]],
            "detail": (line.get("verdict_evidence") or {}).get("detail") or line.get("error") or "",
        })
    return items


def by_platform(index: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in index.get("packs") or ():
        out.setdefault(item["platform"], []).append(item)
    return out


INSTALL_WORDS = {"pack": "pack", "installer": "installer"}


def _pack_row(item: dict) -> str:
    link = f"[line]({item['record_url']})" if item["record_url"] else _code(item["id"])
    cells = [_plain(item["platform"]), _plain(item["system"]), INSTALL_WORDS[item["install"]],
             _code(item["core"]), _plain(item["content"]), f"`{item['verdict']}`",
             str(item["findings"]), link]
    return "| " + " | ".join(cells) + " |"


def packs_section(items: list[dict]) -> list[str]:
    """The Measurements page's table of packs tested the way a user installs
    them: one row per platform, system, install path, core and game."""
    if not items:
        return []
    columns = ["Platform", "System", "Installed by", "Core", "Game", "Verdict", "Findings", "Record"]
    return [
        "## Packs",
        "",
        "A game started with the platform's BIOS folder as a user gets it, from the pack or "
        f"from the installer ([how]({METHOD_PAGE}#packs)). A finding is a file the core looked "
        "for that the folder lacks or holds under another path.",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
        *(_pack_row(item) for item in
          sorted(items, key=lambda i: (i["platform"], i["system"], i["install"], i["core"], i["content"]))),
        "",
    ]


def _inert(value: object) -> str:
    """Run output (a core's log line, a file name it opened) placed in raw
    HTML as text: every character HTML, MDX or Markdown reads as markup,
    quotes included, written as a character reference."""
    return _plain(value).replace('"', "&quot;").replace("'", "&#x27;")


def _pack_details(item: dict) -> list[str]:
    def line(label: str, value: object) -> str:
        return _sentence_html(label, _inert(value), markup_ready=True)

    lines = [
        line("Result", item["detail"]),
        line("Core", f"{item['core']} ({item['emulator']})"),
        line("Files the core opened", ", ".join(item["files"]) or "none"),
        line("Findings", ", ".join(item["finding_list"]) or "none"),
        f'<p><a href="{_attr(item["record_url"])}">Full record</a>.</p>' if item["record_url"] else "",
    ]
    return [line for line in lines if line]


def pack_tested_section(items: list[dict]) -> list[str]:
    """The Tested block of a platform page, in the emulator pages' Tested
    markup: one summary line, then, folded under one line as the emulator
    pages fold their earlier tests, a row per game, core and install path
    with its verdict chip and date, the files and findings in its details."""
    if not items:
        return []
    works = sum(1 for i in items if i["verdict"] == "boots")
    last = max(i["date"][:10] for i in items)
    summary = (f"{len(items)} game start{'s' if len(items) != 1 else ''} with this BIOS folder as "
               f"you install it · {works} work · last run {last}.")
    links = (f'<a href="{markup.site_href(METHOD_PAGE.removesuffix(".md") + "/")}#packs">How</a> · '
             f'<a href="{markup.site_href(EXPLORER_PAGE.removesuffix(".md") + "/")}">All tests</a>')
    lines = ["## Tested", "", '<div class="rb-tested">',
             f'<p class="rb-tested-summary">{html.escape(summary)} {links}</p>',
             '<details class="rb-tested-earlier">',
             "<summary>Each start, by system</summary>",
             '<div class="rb-tested-head" aria-hidden="true"><span>Test</span>'
             "<span>Result</span><span>Installed by</span><span>Date</span></div>"]
    for item in sorted(items, key=lambda i: (i["system"], i["content"], i["core"], i["install"])):
        title = (f"{_inert(item['system'])}, {_inert(short_content(item['content']))} "
                 f"<small>{_inert(item['core'])}</small>")
        lines += [
            '<details class="rb-tested-row">',
            ("<summary>"
             f'<span class="rb-tested-test">{title}</span>'
             f'<span class="rb-tested-result">{_chip({"status": item["verdict"]})}</span>'
             f'<span class="rb-tested-variants">{INSTALL_WORDS[item["install"]]}</span>'
             f'<span class="rb-tested-date">{item["date"][:10]}</span></summary>'),
            '<div class="rb-tested-more">',
            *_pack_details(item),
            "</div>",
            "</details>",
        ]
    return [*lines, "</details>", "</div>", ""]


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
        f"Tests run by themselves every night on an idle computer ([how]({METHOD_PAGE})). "
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
    index["packs"] = pack_summary(root, source)
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
    """Each emulator's items, with the count of its findings still open."""
    opened: dict[str, int] = {}
    for line in (index.get("campaign") or {}).get("open") or ():
        opened[line["emulator"]] = opened.get(line["emulator"], 0) + 1
    out: dict[str, list[dict]] = {}
    for item in index.get("items", []):
        # A pack probe's run is shown on its platform's page, beside the pack.
        if item["subject"].startswith(mr.PACK_SUBJECT_PREFIX):
            continue
        out.setdefault(item["emulator"], []).append(
            {**item, "open_findings": opened.get(item["emulator"], 0)})
    return out


def _text(value: object) -> str:
    """Record text as the page shows it. A semicolon joining two clauses
    ends a sentence on the site: the record keeps the words as written."""
    text = " ".join(str(value or "").split())
    text = re.sub(r";\s+(\w)", lambda m: ". " + m[1].upper(), text)
    return _escape_tags(text)


def _short(commit: str | None) -> str:
    return f"`{commit[:12]}`" if commit else "unknown"


# A verdict or a comparison, as the block says it: words and a chip kind.
RESULT_WORDS = {
    "boots": ("works", "success"),
    "bios_error": ("BIOS error", "danger"),
    "crash": ("crash", "danger"),
    "hang": ("hang", "warning"),
    "unknown": ("unknown", "muted"),
    "mixed": ("depends on the file", "info"),
    "same": ("same with every file", "muted"),
    "differs": ("depends on the file", "info"),
    "inconclusive": ("inconclusive", "warning"),
    "no verdict": ("no verdict", "muted"),
    "ran": ("ran", "muted"),
}
# Words a subject slug spells that read as one term.
TERMS = {"bios": "BIOS", "vcd": "Video CD", "cd": "CD", "jp": "JP", "us": "US", "eu": "EU",
         "cr2": "CR2", "mpeg": "MPEG", "psx": "PSX", "hle": "HLE", "usa": "USA"}
# The campaign's own check names, as the block says them.
CHECK_NAMES = {"files": "full set", "none": "no BIOS", "negative": "wrong files",
               "references": "reference run", "variants": "copies", "alias": "other name",
               "path": "other path", "bench": "benchmark", "boot": "boot test", "alone": "file alone"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def short_content(label: str | None) -> str:
    """A content label cut to the game's name: no source prefix, no
    edition, region or identity detail."""
    text = str(label or "").strip()
    if ":" in text.split("(")[0]:
        text = text.split(":", 1)[1].strip()
    for cut in (" - ", " (", ", "):
        text = text.split(cut, 1)[0]
    return text.strip()


def short_title(item: dict) -> str:
    """A test's name in a few words: the file it varies, the game, and what
    the subject adds to them (a build, a region, a check of the campaign)."""
    thing = Path(str(item.get("variable") or "")).name
    game = short_content(item.get("content"))
    known = set(_words(thing)) | set(_words(item.get("content"))) | set(_words(game))
    known |= set(_words(Path(str((item.get("core") or {}).get("file") or "")).name.split("_libretro")[0]))
    axis = []
    for word in _words(item.get("subject")):
        if word in known or re.fullmatch(r"[0-9a-f]{8}", word):
            continue
        issue = re.fullmatch(r"issue(\d+)", word)
        word = f"issue {issue[1]}" if issue else CHECK_NAMES.get(word, TERMS.get(word, word))
        if word not in axis:
            axis.append(word)
    head = ", ".join(part for part in (thing, game) if part)
    title = f"{head} ({' '.join(axis)})" if axis and head else head or " ".join(axis)
    return title or str(item.get("subject") or "")


OBSERVABLE_WORDS = {"video_sha1": "video", "frame_hashes": "screens", "audio_sha1": "audio",
                    "state_sha1": "save state", "frame_sha1": "last frame"}


def setup_of(record: dict) -> dict:
    """What told two runs of one test apart, each as the words a title
    would use: the campaign check, the core build, the core options, the
    clock, the number of files tried and what was compared."""
    build = str(record["core"].get("build_command") or "")
    patched = re.search(r"\b(patch|edit(?:ed|ing)?)\b", build, re.IGNORECASE)
    named = re.search(r"\b([A-Z]+[0-9]+)\b", build) if patched else None
    check = record.get("check") or {}
    first = (_words(record["subject"]) or [""])[0]
    options = {}
    for key, value in sorted(record["inputs"].get("options", {}).items()):
        options[key] = f"{value} region" if key.endswith("_region") else f"{key.rsplit('_', 1)[-1]} {value}"
    observable = str(check.get("observable") or "")
    return {
        "check": CHECK_NAMES.get(first, ""),
        "build": (f"{named[1]} patch" if named else "patched build") if patched else "published build",
        "options": options,
        "clock": "fixed clock" if "FAKETIME" in record["inputs"].get("env", {}) else "running clock",
        "files": (f"{len(record['variants'])} file{'s' if len(record['variants']) != 1 else ''}"
                  if record["kind"] == "check" else ""),
        "observable": (observable.split(":", 1)[1] if observable.startswith("state:")
                       else OBSERVABLE_WORDS.get(observable, observable)),
    }


def _features(item: dict) -> dict[str, str]:
    """A test's setup as named features, the options one by one."""
    setup = item.get("setup") or {}
    out = {name: str(setup.get(name) or "") for name in ("build", "clock", "files", "observable")}
    for key, value in (setup.get("options") or {}).items():
        out[f"option:{key}"] = value
    return out


def distinct_titles(items: list[dict]) -> dict[str, str]:
    """A title per item: its game and campaign check, then what sets it
    apart from the other tests of the same file, game and core.

    A test whose setup is the common one of its group is named by its game.
    Another adds the features where it leaves the common setup (a patched
    build, a running clock, another region). Two titles still alike take
    every feature that varies in the group, then the subject's own words.
    Nothing is cut.
    """
    out: dict[str, str] = {}
    peers: dict[tuple, list[dict]] = {}
    for item in items:
        game = short_content(item.get("content")) or short_title(item)
        peers.setdefault((item.get("variable") or "", game, item["core"].get("file")), []).append(item)
    for (_, game, _), group in peers.items():
        features = [_features(item) for item in group]
        names = sorted({name for f in features for name in f},
                       key=lambda n: (n.startswith("option:"), ["build", "clock", "files", "observable"].index(n)
                                      if not n.startswith("option:") else 0, n))
        common = {}
        for name in names:
            values = [f.get(name, "") for f in features]
            ranked = sorted(set(values), key=lambda v: -values.count(v))
            # A tie has no common value: every test names its own.
            tied = len(ranked) > 1 and values.count(ranked[0]) == values.count(ranked[1])
            common[name] = None if tied else ranked[0]
        varying = [n for n in names if len({f.get(n, "") for f in features}) > 1]

        def title(item: dict, own: dict[str, str], every: bool, varying: list[str] = varying,
                  common: dict[str, str] = common, game: str = game) -> str:
            words = [(item.get("setup") or {}).get("check", "")]
            for name in varying:
                value = own.get(name, "")
                if every or value != common[name]:
                    words.append(value or f"no {name.rsplit('_', 1)[-1]}")
            return ", ".join([game, *[w for w in words if w]])

        for item, own in zip(group, features):
            out[item["id"]] = title(item, own, every=False)
        for every in (True, None):
            seen: dict[str, int] = {}
            for item in group:
                seen[out[item["id"]]] = seen.get(out[item["id"]], 0) + 1
            for item, own in zip(group, features):
                if seen[out[item["id"]]] > 1:
                    if every:
                        out[item["id"]] = title(item, own, every=True)
                    else:
                        axis = short_title({**item, "variable": None, "content": None})
                        out[item["id"]] = f"{out[item['id']]}, {axis}"
    return out


def short_host(host: dict) -> str:
    """The computer as a reader names it: the CPU model and the OS."""
    cpu = str(host.get("cpu_model") or "unknown CPU")
    cpu = re.sub(r"\(R\)|\(TM\)|\bCPU\b|@.*$|^\d+(?:st|nd|rd|th) Gen ", "", cpu)
    cpu = " ".join(cpu.split())
    system = str(host.get("os") or "")
    name = "Linux" if "Linux" in system else system.split("-")[0] or "unknown OS"
    if "microsoft" in system.lower():
        name += " (WSL2)"
    return f"{cpu}, {name}"


def variant_groups(record: dict) -> list[dict]:
    """One entry per variant: the result group it falls in, and whether it
    staged no file. A run that carries verdicts is grouped by verdict, a
    comparison by the value its observable took."""
    check = record.get("check") or {}
    position: dict[str, int] = {}
    result: dict[str, str] = {}
    if all("verdict" in v for v in record["variants"]) and (
            record["kind"] == "run" or check.get("observable") == "frame_hashes"):
        for variant in record["variants"]:
            position.setdefault(variant["verdict"], len(position))
            result[variant["label"]] = RESULT_WORDS[variant["verdict"]][0]
        groups = {v["label"]: position[v["verdict"]] for v in record["variants"]}
    else:
        groups = {}
        for number, group in enumerate((check.get("result") or {}).get("groups") or ()):
            for label in group["variants"]:
                groups[label] = number
                result[label] = f"result {chr(ord('A') + number % 26)}"
    varied = check.get("variable")
    return [{"variant": v["label"], "group": groups.get(v["label"], 0),
             "result": result.get(v["label"], ""),
             "absent": bool(varied) and varied in v["system_files"] and v["system_files"][varied] is None}
            for v in record["variants"]]


def proves(record_id: str, profile: dict) -> list[str]:
    """The profile fields this record is evidence for, as `field of file`."""
    out = []
    for entry in profile.get("files") or ():
        for name, ids in sorted((entry.get("evidence") or {}).items()):
            if record_id in ids:
                out.append(f"{name} of {entry.get('name')}")
    return out


# An MDX page reads a brace as the start of an expression, in text and in
# an attribute alike.
_BRACES = str.maketrans({"{": "&#123;", "}": "&#125;"})


def _attr(value: object) -> str:
    return html.escape(" ".join(str(value or "").split()), quote=True).translate(_BRACES)


def _cell(value: object) -> str:
    """Record text placed in raw HTML: escaped as text, never read as
    Markdown (a run's output is no markup), braces made inert for MDX. A
    semicolon joining two clauses ends a sentence, as on the rest of the
    site."""
    text = " ".join(str(value or "").split())
    text = re.sub(r";\s+(\w)", lambda m: ". " + m[1].upper(), text)
    return html.escape(text, quote=True).translate(_BRACES)


_SAFE_HREF = re.compile(r"^(?:https?://[^\s\"'<>]+|/[^\s\"'<>]*|\.{0,2}/?[\w.~-][^\s\"'<>:]*)$")


def _href(url: object) -> str | None:
    """A link target from a record: http(s) or relative only, else none."""
    text = str(url or "")
    return _attr(text) if text and _SAFE_HREF.match(text) and not text.lower().startswith(
        ("javascript:", "data:", "vbscript:")) else None


def _link(url: object, text: str) -> str:
    target = _href(url)
    return f'<a href="{target}">{text}</a>' if target else text


def _dots(item: dict) -> str:
    groups = item.get("groups") or []
    names = ". ".join(f"{g['variant']}: {g['result']}" for g in groups)
    dots = "".join(
        f'<span class="rb-dot rb-dot-{g["group"] % 5}{" rb-dot-empty" if g["absent"] else ""}" '
        f'title="{_attr(g["variant"] + ": " + g["result"])}"></span>' for g in groups)
    return (f'<span class="rb-dots" role="img" aria-label="{_attr(f"{len(groups)} variants. {names}")}">'
            f"{dots}</span>")


def _chip(item: dict) -> str:
    words, kind = RESULT_WORDS.get(item["status"], (item["status"], "muted"))
    return f'<span class="rb-badge rb-badge-{kind}">{_cell(words)}</span>'


def _sentence_html(label: str, value: object, markup_ready: bool = False) -> str:
    """One line of the details, or nothing: an empty value is never printed."""
    text = (str(value or "") if markup_ready else _cell(value)).strip().rstrip(".")
    return f"<p><strong>{label}:</strong> {text}.</p>" if text else ""


def _details(item: dict) -> list[str]:
    core = item["core"]
    build = _cell(core["library"]) + (f" at <code>{_cell(core['commit'][:12])}</code>"
                                      if core.get("commit") else "")
    reps = f"{item['variants']} variant{'s' if item['variants'] != 1 else ''}, " \
           f"{item['repetitions']} repetition{'s' if item['repetitions'] != 1 else ''} each"
    if item["flags"]:
        reps += f", {item['flags']} flag{'s' if item['flags'] != 1 else ''} raised"
    runs = "" if item["runs"] == 1 else f", {item['runs']} runs, the latest shown"
    lines = [
        _sentence_html("Claim", item.get("claim")),
        _sentence_html("Result", item.get("statement")),
        _sentence_html("Method", item.get("method")),
        _sentence_html("Proves", ", ".join(item.get("proves") or [])),
        _sentence_html("Ran on", item.get("host_short") or item["host"]),
        _sentence_html("Core build", build and f"{build}. {reps}", markup_ready=True),
        f'<p>{_link(item["record_url"], "Full record")}{runs}.</p>',
    ]
    return [line for line in lines if line]


def tested_counts(items: list[dict]) -> dict:
    tests = [i for i in items if i["kind"] != "bench"]
    return {"tests": len(items), "confirmed": sum(1 for i in items if i.get("proves")),
            "open": max((i.get("open_findings", 0) for i in items), default=0),
            "benchmarks": len(items) - len(tests),
            "last": max((i["date"][:10] for i in items), default="")}


def core_label(item: dict) -> str:
    return Path(str(item["core"].get("file") or "")).name.split("_libretro")[0]


def _row(item: dict, title: str, page_host: str, main_core: str) -> list[str]:
    elsewhere = (item.get("host_short") or item["host"]) != page_host
    title = _cell(title)
    if core_label(item) != main_core:
        title += f' <small>{_cell(core_label(item))}</small>'
    return [
        '<details class="rb-tested-row">',
        ("<summary>"
         f'<span class="rb-tested-test">{title}</span>'
         f'<span class="rb-tested-result">{_chip(item)}</span>'
         f'<span class="rb-tested-variants">{_dots(item)}</span>'
         f'<span class="rb-tested-date">{item["date"][:10]}'
         + (f' <small>{_attr(item.get("host_short"))}</small>' if elsewhere else "")
         + "</span></summary>"),
        '<div class="rb-tested-more">',
        *_details(item),
        "</div>",
        "</details>",
    ]


def by_file(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """The items under the file each varies, in order of first appearance;
    the runs that vary none last."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(Path(str(item.get("variable") or "")).name, []).append(item)
    return sorted(groups.items(), key=lambda kv: kv[0] == "")


def folded(item: dict) -> bool:
    """A row the block folds away: a test from before the boot oracle, or a
    run that recorded reference frames for the oracle, which a reviewer
    reads and a visitor does not."""
    return item["status"] == "no verdict" or str(item.get("subject") or "").startswith("references-")


def summary_line(items: list[dict], page_host: str) -> str:
    """N tests, the confirmed and open counts when there are any, the last
    run and its computer."""
    counts = tested_counts(items)
    parts = [f"{counts['tests']} test{'s' if counts['tests'] != 1 else ''}"]
    parts += [f"{counts[key]} {key}" for key in ("confirmed", "open") if counts[key]]
    parts.append(f"last run {counts['last']} on {page_host}")
    return " · ".join(parts) + "."


def tested_section(name: str, items: list[dict]) -> list[str]:
    """The Tested block of an emulator page, empty when nothing was recorded.

    The answer first: one summary line, then the tests under the file each
    one varies, a row per test with its result chip, a dot per variant and
    its date. What proves it (method, claim, computer, core build, record)
    waits in each row's details. Tests recorded before the boot oracle fold
    under one line, and benchmarks get their own table at the end.
    """
    if not items:
        return []
    hosts = [i.get("host_short") or i["host"] for i in items]
    page_host = max(sorted(set(hosts)), key=hosts.count)
    cores = [core_label(i) for i in items]
    main_core = max(sorted(set(cores)), key=cores.count)
    titles = distinct_titles(items)
    links = (f'<a href="{markup.site_href(METHOD_PAGE.removesuffix(".md") + "/")}">How</a> · '
             f'<a href="{markup.site_href(EXPLORER_PAGE.removesuffix(".md") + "/")}">All tests</a> · '
             f'<a href="{markup.site_href(f"downloads/measurements/{name}.sqlite")}">SQLite</a>')
    ordered = sorted(items, key=lambda i: (i["date"], i["subject"], core_label(i)), reverse=True)
    benches = [i for i in ordered if i["kind"] == "bench"]
    earlier = [i for i in ordered if i["kind"] != "bench" and folded(i)]
    current = [i for i in ordered if i["kind"] != "bench" and not folded(i)]
    lines = ["## Tested", "", '<div class="rb-tested">',
             f'<p class="rb-tested-summary">{_cell(summary_line(items, page_host))} {links}</p>']
    if current or earlier:
        lines.append('<div class="rb-tested-head" aria-hidden="true"><span>Test</span>'
                     "<span>Result</span><span>Variants</span><span>Date</span></div>")
    for file, group in by_file(current):
        label = f"<code>{_cell(file)}</code>" if file else "No file varied"
        lines.append(f'<p class="rb-tested-file">{label}</p>')
        for item in group:
            lines += _row(item, titles[item["id"]], page_host, main_core)
    if current:
        lines.append('<p class="rb-tested-legend"><span class="rb-dot rb-dot-0"></span> A dot per file tried, '
                     'the same colour for the same result. <span class="rb-dot rb-dot-0 rb-dot-empty">'
                     "</span> No file staged.</p>")
    if earlier:
        old = sum(1 for i in earlier if i["status"] == "no verdict")
        parts = [f"{old} test{'s' if old != 1 else ''} from before the boot oracle" if old else "",
                 (f"{len(earlier) - old} reference run{'s' if len(earlier) - old != 1 else ''}"
                  if len(earlier) - old else "")]
        lines += ['<details class="rb-tested-earlier">',
                  f"<summary>{_cell(' and '.join(p for p in parts if p))}</summary>"]
        for item in earlier:
            file = Path(str(item.get("variable") or "")).name
            title = f"{file}, {titles[item['id']]}" if file else titles[item["id"]]
            lines += _row(item, title, page_host, main_core)
        lines.append("</details>")
    if benches:
        # One line: MDX reads a table split over lines as paragraphs.
        table = ['<table class="rb-tested-bench"><thead><tr><th>Benchmark</th><th>ms per frame</th>'
                 "<th>fps</th><th>stdev</th><th>runs</th></tr></thead><tbody>"]
        for item in benches:
            timing = item.get("timing") or {}
            title = _cell(titles[item["id"]])
            if core_label(item) != main_core:
                title += f' <small>{_cell(core_label(item))}</small>'
            table.append(
                f'<tr><td>{_link(item["record_url"], title)}</td>'
                f'<td>{timing.get("ms_per_frame", 0):.3f}</td><td>{timing.get("fps", 0):.1f}</td>'
                f'<td>{timing.get("stdev", 0):.3f}</td><td>{item["repetitions"]}</td></tr>')
        lines.append("".join([*table, "</tbody></table>"]))
    return [*lines, "</div>", ""]


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


def generate_explorer_page(index: dict) -> str:
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
        *packs_section(index.get("packs") or []),
        *campaign_section(index.get("campaign")),
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
