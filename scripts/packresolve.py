"""Finding the bytes a pack entry names.

Resolution proper lives in common. This layer adds what only a build
needs: storage tiers, release assets for files too large to commit,
and the offline switch that forbids reaching for the network."""

from __future__ import annotations

import re
_HEX_RE = re.compile(r"\b([0-9a-fA-F]{8,40})\b")
from largefiles import fetch_large_file
import os
from common import (
    declared_hash_verdict,
    resolution_is_hash_exact,
    resolve_local_file,
    sanitize_pack_path,
)
from validation import inner_rom_check
# CLI-wide network policy.  Public helpers can override it explicitly, while
# every CLI mode inherits --offline (including large-file release fallback).
_OFFLINE = False


def set_offline(value: bool) -> None:
    """Forbid every network reach for the rest of the run."""
    global _OFFLINE
    _OFFLINE = value

def _values(raw: object) -> list[str]:
    """A hash field's values: one, a list, or a comma-separated string."""
    items = raw if isinstance(raw, list) else [raw] if raw else []
    return [
        value.strip().lower().removeprefix("0x")
        for item in items
        for value in str(item).split(",")
        if value.strip()
    ]


def _declared_hashes(file_entry: dict) -> dict[str, list[str]]:
    return {
        field: _values(file_entry.get(field))
        for field in ("sha1", "md5", "sha256", "crc32")
    }


def _verified_asset(
    name: str, declared: dict[str, list[str]], file_entry: dict, offline: bool
) -> str | None:
    """The release asset of that name, if it is a copy the entry accepts.

    The store is asked once per accepted sha1 (or md5), since it keeps one
    revision under a name. What it returns is held against every hash the
    entry declares, sha256 and crc32 included.
    """
    attempts = (
        [(sha1, "") for sha1 in declared["sha1"]]
        or [("", md5) for md5 in declared["md5"]]
        or [("", "")]
    )
    for sha1, md5 in attempts:
        cached = fetch_large_file(
            name, expected_sha1=sha1, expected_md5=md5, offline=offline,
        )
        if not cached:
            continue
        verdict = declared_hash_verdict(
            cached,
            has_strong_hash=any(declared.values()),
            sha1_candidates=set(declared["sha1"]),
            sha256_candidates=set(declared["sha256"]),
            md5_list=declared["md5"],
            crc_candidates=set(declared["crc32"]),
            zipped_file=file_entry.get("zipped_file"),
            declared_size=file_entry.get("size"),
            file_entry=file_entry,
        )
        if verdict != "hash_mismatch":
            return cached
    return None


def _detect_hash_type(h: str) -> str:
    n = len(h)
    if n == 40:
        return "sha1"
    if n == 32:
        return "md5"
    if n == 8:
        return "crc32"
    return "md5"

def parse_hash_input(raw: str) -> list[tuple[str, str]]:
    """Parse comma-separated hash string into (type, hash) tuples."""
    results: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        m = _HEX_RE.search(part)
        if m:
            h = m.group(1)
            results.append((_detect_hash_type(h), h))
    return results

def parse_hash_file(path: str) -> list[tuple[str, str]]:
    """Parse hash file (one per line, comments with #, mixed formats)."""
    results: list[tuple[str, str]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _HEX_RE.search(line.lower())
            if m:
                h = m.group(1)
                results.append((_detect_hash_type(h), h))
    return results

def lookup_hashes(
    hashes: list[tuple[str, str]],
    db: dict,
    bios_dir: str,
    emulators_dir: str,
    platforms_dir: str,
) -> int:
    """Print diagnostic info for each hash and return how many were not found."""
    missing = 0
    files_db = db.get("files", {})
    by_md5 = db.get("indexes", {}).get("by_md5", {})
    by_crc32 = db.get("indexes", {}).get("by_crc32", {})

    for hash_type, hash_val in hashes:
        sha1 = None
        if hash_type == "sha1" and hash_val in files_db:
            sha1 = hash_val
        elif hash_type == "md5":
            sha1 = by_md5.get(hash_val)
        elif hash_type == "crc32":
            sha1 = by_crc32.get(hash_val)

        if not sha1 or sha1 not in files_db:
            print(f"\n{hash_type.upper()}: {hash_val}")
            print("  NOT FOUND in database")
            missing += 1
            continue

        entry = files_db[sha1]
        name = entry.get("name", "?")
        md5 = entry.get("md5", "?")
        paths = entry.get("paths") or []
        aliases = entry.get("aliases") or []

        print(f"\n{hash_type.upper()}: {hash_val}")
        print(f"  SHA1: {sha1}")
        print(f"  MD5:  {md5}")
        print(f"  Name: {name}")
        if paths:
            print(f"  Path: {paths[0]}")
        if aliases:
            print(f"  Aliases: {aliases}")

        # Check if file exists in repo (by path or by resolve_local_file)
        in_repo = False
        if paths:
            primary = os.path.join(bios_dir, paths[0])
            if os.path.exists(primary):
                in_repo = True
        if not in_repo:
            try:
                fe_check = {"name": name, "sha1": sha1, "md5": md5}
                local, status = resolve_file(fe_check, db, bios_dir, {})
                if local and status != "not_found":
                    in_repo = True
            except (KeyError, OSError):
                pass
        print(f"  In repo: {'YES' if in_repo else 'NO'}")
    if missing:
        print(f"\n{missing} of {len(hashes)} hash(es) not in the database")
    return missing

def resolve_file(
    file_entry: dict,
    db: dict,
    bios_dir: str,
    zip_contents: dict | None = None,
    dest_hint: str = "",
    data_dir_registry: dict | None = None,
    *,
    offline: bool | None = None,
) -> tuple[str | None, str]:
    """Resolve a BIOS file with storage tiers and release asset fallback.

    Wraps common.resolve_local_file() with pack-specific logic for
    storage tiers (external/user_provided), large file release assets,
    and MAME clone mapping (deduped ZIPs).
    """
    storage = file_entry.get("storage", "embedded")
    if storage == "user_provided":
        return None, "user_provided"
    if storage == "external":
        return None, "external"

    path, status = resolve_local_file(
        file_entry,
        db,
        zip_contents,
        dest_hint=dest_hint,
        data_dir_registry=data_dir_registry,
    )
    if path and status != "hash_mismatch":
        return path, status

    # Large files from GitHub release assets: tried when local file is
    # missing OR has a hash mismatch (wrong variant on disk)
    name = file_entry.get("name", "")
    if file_entry.get("type") == "directory" or name.endswith("/"):
        # A tree the collection does not hold is missing, as verify says,
        # since the asset store holds files, not trees.
        return (path, status) if path else (None, "not_found")
    declared = _declared_hashes(file_entry)
    if file_entry.get("unsourceable") and not any(declared.values()):
        # The asset store is addressed by file name: with no hash to check,
        # it would hand back the same-named file of another game, which is
        # exactly what the flag forbids.
        return (path, status) if path else (None, "not_found")
    cached = _verified_asset(
        name, declared, file_entry, _OFFLINE if offline is None else offline,
    )
    if cached:
        return cached, "release_asset"

    # Fall back to hash_mismatch local file if release asset unavailable
    if path:
        return path, status

    return None, "not_found"


def preferred_entries(
    pack_systems: dict,
    db: dict,
    bios_dir: str,
    base_dest: str,
    required_only: bool,
    zip_contents: dict | None,
    offline: bool | None,
) -> dict[str, int]:
    """Which declaration wins when several claim the same destination.

    A platform can declare one destination in more than one system, bare
    in one and hash-constrained in another. First-come dedup would let
    the bare entry pack whatever answers to the name, so a constrained
    sibling that resolves by content claims the destination instead.
    The choice is about the baseline and reads no data directory, for the
    pack, the manifest and verify alike: verify alone passed the registry
    and could crown a declaration only a data/ cache satisfies.
    """
    preferred_entries: dict[str, int] = {}
    dest_entries: dict[str, list[dict]] = {}
    for sys_id, system in sorted(pack_systems.items()):
        for file_entry in system.get("files", []):
            if required_only and file_entry.get("required") is False:
                continue
            dest = sanitize_pack_path(
                file_entry.get("destination", file_entry.get("name", ""))
            )
            if not dest:
                continue
            full = f"{base_dest}/{dest}" if base_dest else dest
            dest_entries.setdefault(full, []).append(file_entry)
    for full, entries in dest_entries.items():
        if len(entries) < 2:
            continue
        constrained = [
            fe
            for fe in entries
            if fe.get("md5") or fe.get("sha1") or fe.get("zipped_file")
        ]
        if not constrained:
            continue
        resolved = [
            (fe, *resolve_file(
                fe,
                db,
                bios_dir,
                zip_contents,
                offline=offline,
            ))
            for fe in constrained
        ]
        members = [fe for fe in entries if fe.get("zipped_file")]
        if members:
            # One archive declared once per ROM it must hold: the archive
            # to ship is the one the most declarations accept, not the first
            # that answers one of them (adam_fdc.zip: a one-member MAME set
            # answered first, the eight-member set sat beside it).
            def accepted(path: str | None) -> int:
                if not path or not path.endswith(".zip"):
                    return -1
                return sum(inner_rom_check(fe, path) == "ok" for fe in members)

            fe, path, _st = max(resolved, key=lambda item: accepted(item[1]))
            if accepted(path) > 0:
                preferred_entries[full] = id(fe)
                continue
        best = None
        for fe, _lp, _st in resolved:
            if _lp and _st == "md5_exact":
                best = fe
                break
            if best is None and _lp and resolution_is_hash_exact(_st):
                best = fe
        if best is not None:
            preferred_entries[full] = id(best)

    return preferred_entries
