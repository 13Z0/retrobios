"""Core probe records as this repository stores them.

A run of core_probe.py produces one full record. It is stored as two parts:

  measurements/<emulator>.jsonl                one slim record per line, append-only
  measurements/data/<emulator>/<id>.json.gz    the bulky rest, named by the record id

The slim record keeps what a reader needs to judge and reproduce the result:
claim, method, observable, result, statistics, host, inputs by hash, core
build and frontend commit, and per variant the values of its first
repetition. The sidecar holds every repetition in full (per-frame hashes,
core logs, declared options), the raw timing values the statistics were
computed from, and any compared value too large to sit in a line. Its
sha256 is in the record, so the line vouches for it. ``join`` puts the two
back into the full record, ``split`` is its inverse.

A record id is the date and the first 12 hex digits of the sha256 of the
record without its id. A record carried over from the first layout keeps
the id it had, which hashes that full record.

Standard library only: core_probe.py, validate_schemas.py and the site
read it.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

RECORD_VERSION = 2
RECORDS_DIR = "measurements"
DATA_DIR = f"{RECORDS_DIR}/data"
# Fields a repetition keeps in the slim record, per variant, from the first one.
VARIANT_SUMMARY = ("observables", "files_opened", "files_probed_absent", "tracing")
# Observables too large for a line: their values stay in the sidecar.
BULKY_OBSERVABLES = ("frame_hashes",)
ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


class RecordError(ValueError):
    """A record or sidecar that does not match its counterpart."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slugify(text: str, limit: int = 40) -> str:
    """Lowercase words joined by hyphens, cut at a word boundary."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for word in words:
        candidate = f"{out}-{word}" if out else word
        if len(candidate) > limit:
            break
        out = candidate
    return out or "record"


def record_id(slim: dict) -> str:
    body = {key: value for key, value in slim.items() if key != "id"}
    return f"{slim['recorded_at'][:10]}-{sha256_hex(canonical(body))[:12]}"


def line_text(slim: dict) -> str:
    """The record as one line of its .jsonl file, keys in a fixed order."""
    return json.dumps(slim, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sidecar_path(emulator: str, rid: str) -> str:
    return f"{DATA_DIR}/{emulator}/{rid}.json.gz"


def records_path(emulator: str) -> str:
    return f"{RECORDS_DIR}/{emulator}.jsonl"


def sidecar_bytes(sidecar: dict) -> bytes:
    """gzip of the sidecar JSON, with no name and no time in the header."""
    return gzip.compress(canonical(sidecar), compresslevel=9, mtime=0)


def read_sidecar(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob))


def _value_digest(value: object) -> str:
    return hashlib.sha1(canonical(value)).hexdigest()


def split(full: dict) -> tuple[dict, dict]:
    """The full record as (slim record without id and data, sidecar)."""
    slim = copy.deepcopy(full)
    sidecar: dict = {"variants": [], "timing_raw": {}, "group_values": {}}
    variants = []
    for variant in slim["variants"]:
        reps = variant["repetitions"]
        sidecar["variants"].append({"label": variant["label"], "repetitions": reps})
        first = reps[0]
        summary = {key: first.get(key) for key in VARIANT_SUMMARY}
        summary["observables"] = {
            key: value for key, value in (first.get("observables") or {}).items()
            if key not in BULKY_OBSERVABLES
        }
        variants.append({
            "label": variant["label"],
            "system_files": variant["system_files"],
            "flags": variant["flags"],
            "repetitions": len(reps),
            "loaded": all(rep.get("loaded") for rep in reps),
            **summary,
        })
    slim["variants"] = variants
    for name, stats in (slim.get("timing") or {}).items():
        sidecar["timing_raw"][name] = stats.pop("raw")
    for group in ((slim.get("check") or {}).get("result") or {}).get("groups", []):
        if isinstance(group.get("value"), (list, dict)):
            digest = _value_digest(group["value"])
            sidecar["group_values"][digest] = group.pop("value")
            group["value_sha1"] = digest
    return slim, sidecar


def join(slim: dict, sidecar: dict) -> dict:
    """The full record a slim record and its sidecar were split from."""
    full = copy.deepcopy(slim)
    full.pop("id", None)
    full.pop("data", None)
    labels = [v["label"] for v in full["variants"]]
    if labels != [v["label"] for v in sidecar["variants"]]:
        raise RecordError(f"variants differ: record {labels}, sidecar "
                          f"{[v['label'] for v in sidecar['variants']]}")
    full["variants"] = [
        {"label": variant["label"], "system_files": variant["system_files"],
         "repetitions": copy.deepcopy(stored["repetitions"]), "flags": variant["flags"]}
        for variant, stored in zip(full["variants"], sidecar["variants"])
    ]
    for name, raw in sidecar.get("timing_raw", {}).items():
        full["timing"][name]["raw"] = list(raw)
    for group in ((full.get("check") or {}).get("result") or {}).get("groups", []):
        digest = group.pop("value_sha1", None)
        if digest is not None:
            group["value"] = copy.deepcopy(sidecar["group_values"][digest])
    return full


def seal(slim: dict, sidecar: dict, rid: str | None = None) -> tuple[dict, bytes]:
    """The slim record with its data reference and id, and the sidecar bytes.

    ``rid`` keeps an id given before (a record carried over from the first
    layout), otherwise the id is computed.
    """
    blob = sidecar_bytes(sidecar)
    sealed = {**slim, "data": {"sha256": sha256_hex(blob), "bytes": len(blob)}}
    sealed["id"] = rid or record_id(sealed)
    return sealed, blob


def check_pair(slim: dict, blob: bytes) -> list[str]:
    """What is wrong between a stored line and its stored sidecar."""
    problems = []
    data = slim.get("data") or {}
    if sha256_hex(blob) != data.get("sha256"):
        problems.append(f"{slim.get('id')}: sidecar sha256 {sha256_hex(blob)} != {data.get('sha256')}")
    if len(blob) != data.get("bytes"):
        problems.append(f"{slim.get('id')}: sidecar is {len(blob)} bytes, record says {data.get('bytes')}")
    if not problems:
        try:
            full = join(slim, read_sidecar(blob))
        except (RecordError, KeyError, OSError, ValueError) as exc:
            return [f"{slim.get('id')}: record and sidecar do not join: {exc}"]
        if slim.get("migrated_from_version") == 1:
            digest = sha256_hex(canonical(first_layout(full)))[:12]
            if str(slim.get("id", "")).rsplit("-", 1)[-1] != digest:
                problems.append(f"{slim.get('id')}: id does not hash the first-layout record ({digest})")
    return problems


V2_IDENTITY = ("emulator", "subject", "supersedes", "migrated_from_version")


def first_layout(full: dict) -> dict:
    """A full record as the first layout wrote it: no emulator, subject or
    supersedes, record_version 1."""
    old = {key: value for key, value in full.items() if key not in V2_IDENTITY}
    old["record_version"] = 1
    return old


def check_line(slim: dict) -> list[str]:
    """The structural rules a line holds, without a JSON Schema validator."""
    rid = slim.get("id", "")
    problems = []
    if slim.get("record_version") != RECORD_VERSION:
        problems.append(f"{rid}: record_version {slim.get('record_version')} != {RECORD_VERSION}")
    if not ID_PATTERN.match(str(rid)):
        problems.append(f"{rid!r}: not an id of the form YYYY-MM-DD-<12 hex>")
    elif str(slim.get("recorded_at", ""))[:10] != rid[:10]:
        problems.append(f"{rid}: id date differs from recorded_at {slim.get('recorded_at')}")
    elif slim.get("migrated_from_version") is None and record_id(slim) != rid:
        problems.append(f"{rid}: id does not hash the record ({record_id(slim)})")
    for key in ("emulator", "subject"):
        if not SLUG_PATTERN.match(str(slim.get(key, ""))):
            problems.append(f"{rid}: {key} {slim.get(key)!r} is not a lowercase slug")
    return problems


def parse_lines(text: str, label: str) -> list[dict]:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise RecordError(f"{label}:{number}: empty line")
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordError(f"{label}:{number}: {exc}") from exc
    return out


def appended_only(before: str, after: str) -> str | None:
    """None when ``after`` keeps every line of ``before``, in place, and
    only adds lines at its end, otherwise the first line that moved."""
    old, new = before.splitlines(), after.splitlines()
    for number, line in enumerate(old, 1):
        if number > len(new):
            return f"line {number} removed"
        if new[number - 1] != line:
            return f"line {number} changed"
    return None


def live(records: Iterable[dict]) -> list[dict]:
    """The records no later record supersedes."""
    records = list(records)
    superseded = {r["supersedes"] for r in records if r.get("supersedes")}
    return [r for r in records if r["id"] not in superseded]


class MeasurementsError(RuntimeError):
    """The records of a tree cannot be read, or break a rule."""


@dataclass(frozen=True)
class Stored:
    """One record line as read from the tree."""

    emulator: str
    line: int
    record: dict


def tree_files(root: Path) -> dict[str, Path]:
    """Repository path -> file of every record file and data file under ``root``."""
    base = root / RECORDS_DIR
    if not base.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): path
            for path in sorted(base.rglob("*")) if path.is_file()}


def read_records(root: Path) -> list[Stored]:
    """Every record line under ``root``, each checked against its rules and data."""
    files = tree_files(root)
    data_prefix = f"{DATA_DIR}/"
    problems: list[str] = []
    stored: list[Stored] = []
    claimed: set[str] = set()
    for path in sorted(p for p in files if not p.startswith(data_prefix)):
        emulator = path[len(RECORDS_DIR) + 1:].removesuffix(".jsonl")
        if not path.endswith(".jsonl") or "/" in emulator:
            problems.append(f"{path}: {RECORDS_DIR}/ holds <emulator>.jsonl files and data/ only")
            continue
        seen: set[str] = set()
        try:
            records = parse_lines(files[path].read_text(encoding="utf-8"), path)
        except RecordError as exc:
            problems.append(str(exc))
            continue
        for number, record in enumerate(records, 1):
            where = f"{path}:{number}"
            problems.extend(f"{where}: {p}" for p in check_line(record))
            if record.get("emulator") != emulator:
                problems.append(f"{where}: emulator {record.get('emulator')!r} in {path}")
            if record.get("id") in seen:
                problems.append(f"{where}: id {record.get('id')} appears twice")
            if record.get("supersedes") and record["supersedes"] not in seen:
                problems.append(f"{where}: supersedes {record['supersedes']}, no earlier line of this file")
            seen.add(record.get("id"))
            data = sidecar_path(emulator, str(record.get("id")))
            claimed.add(data)
            if data not in files:
                problems.append(f"{where}: {data} is missing")
            else:
                problems.extend(f"{where}: {p}" for p in check_pair(record, files[data].read_bytes()))
            stored.append(Stored(emulator, number, record))
    problems.extend(f"{path}: no record line names it"
                    for path in sorted(files) if path.startswith(data_prefix) and path not in claimed)
    if problems:
        raise MeasurementsError("records under {}:\n  {}".format(root, "\n  ".join(problems)))
    return stored


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def committed_files(root: Path, ref: str) -> dict[str, bytes]:
    """Path -> content of every record and data file at ``ref``."""
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", ref, "--", RECORDS_DIR)
    if listing.returncode != 0:
        raise MeasurementsError(f"git ls-tree {ref}: {listing.stderr.decode(errors='replace').strip()}")
    out = {}
    for path in filter(None, listing.stdout.decode("utf-8").split("\0")):
        blob = _git(root, "show", f"{ref}:{path}")
        if blob.returncode != 0:
            raise MeasurementsError(f"git show {ref}:{path}: {blob.stderr.decode(errors='replace').strip()}")
        out[path] = blob.stdout
    return out


def append_only_problems(root: Path, ref: str = "HEAD") -> list[str]:
    """What the tree under ``root`` changed or removed of the lines and data
    files ``ref`` holds. Lines added at the end and new files are allowed."""
    before, after = committed_files(root, ref), tree_files(root)
    problems = []
    for path, content in sorted(before.items()):
        if path not in after:
            problems.append(f"{path}: removed")
            continue
        now = after[path].read_bytes()
        if now == content:
            continue
        if path.endswith(".jsonl"):
            moved = appended_only(content.decode("utf-8"), now.decode("utf-8"))
            if moved:
                problems.append(f"{path}: {moved}")
        else:
            problems.append(f"{path}: rewritten")
    return problems


def last_commit(root: Path) -> str | None:
    """The last commit that touched the records: the one their permalinks name."""
    proc = _git(root, "log", "-1", "--format=%H", "--", RECORDS_DIR)
    commit = proc.stdout.decode().strip() if proc.returncode == 0 else ""
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None
