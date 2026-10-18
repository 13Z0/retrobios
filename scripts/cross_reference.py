#!/usr/bin/env python3
"""Cross-reference emulator profiles against platform configs.

Identifies BIOS files that emulators need but platforms don't declare,
providing gap analysis for extended coverage.

Usage:
    python scripts/cross_reference.py
    python scripts/cross_reference.py --emulator dolphin
    python scripts/cross_reference.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    require_platform,
    _norm_system_id,
    directory_members,
    get_mame_clone_map,
    list_registered_platforms,
    load_database,
    load_emulator_profiles,
    load_platform_config,
    name_match_size_ok,
    parse_md5_list,
    require_yaml,
    resolve_local_file,
    runs_standalone,
)
from validation import declaration_settles, outside_gap_scope

yaml = require_yaml()

DEFAULT_EMULATORS_DIR = "emulators"
DEFAULT_PLATFORMS_DIR = "platforms"
DEFAULT_DB = "database.json"


def load_platform_files(
    platforms_dir: str,
    platforms: list[str] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Collect declared filenames + data_directories per system.

    Restricted to *platforms* when given, otherwise every registered platform.
    Keyed by the normalized system ID: a profile writes `sega-megacd` where
    RetroArch writes `sega-mega-cd`, and an exact match called 924 declared
    files undeclared.
    """
    declared = {}
    platform_data_dirs = {}
    for platform_name in platforms or list_registered_platforms(
        platforms_dir, include_archived=True
    ):
        config = load_platform_config(platform_name, platforms_dir)
        for sys_id, system in config.get("systems", {}).items():
            for fe in system.get("files", []):
                name = fe.get("name", "")
                if name:
                    declared.setdefault(_norm_system_id(sys_id), set()).add(name)
            for dd in system.get("data_directories", []):
                ref = dd.get("ref", "")
                if ref:
                    platform_data_dirs.setdefault(_norm_system_id(sys_id), set()).add(ref)
    return declared, platform_data_dirs


def declared_names(
    platforms_dir: str, platforms: list[str] | None, db: dict
) -> dict[str, list[tuple[int | None, str]]]:
    """Every name the platforms declare, with each declaration's size and slot.

    The report judged "undeclared" per normalized system id, which does not
    bring bk and elektronika-bk, or nintendo-sgb and nintendo-super-game-boy,
    together: 646 entries whose exact name a platform declares were counted
    undeclared, while verify and the site read one flat index.
    """
    from common import merged_platform_declarations

    return merged_platform_declarations(
        [
            load_platform_config(platform_name, platforms_dir)
            for platform_name in platforms or list_registered_platforms(
                platforms_dir, include_archived=True
            )
        ],
        db,
    )


def _build_supplemental_index(
    data_root: str = "data", bios_root: str = "bios"
) -> set[str]:
    """Build a set of filenames and directory names in data/ and inside bios/ ZIPs.

    A directory is indexed only with its trailing slash: a bare name matched a
    file entry of the same name, and BasiliskII's required `ROM` read as held
    because cpcemu keeps a directory called ROM.
    """
    names: set[str] = set()
    root_path = Path(data_root)
    if root_path.is_dir():
        for fpath in root_path.rglob("*"):
            if fpath.name.startswith("."):
                continue
            if fpath.is_file():
                names.add(fpath.name)
                names.add(fpath.name.lower())
            else:
                names.add(fpath.name + "/")
                names.add(fpath.name.lower() + "/")
            if fpath.is_dir():
                # Also index relative path from data/subdir/ for directory entries
                parts = fpath.relative_to(root_path).parts
                if len(parts) > 1:
                    rel = "/".join(parts[1:])
                    names.add(rel + "/")
                    names.add(rel.lower() + "/")
    bios_path = Path(bios_root)
    if bios_path.is_dir():
        # Index directory names for directory-type entries (e.g., "nestopia/samples/moepro/")
        for dpath in bios_path.rglob("*"):
            if dpath.is_dir() and not dpath.name.startswith("."):
                names.add(dpath.name + "/")
                names.add(dpath.name.lower() + "/")
        names |= _zip_member_names(bios_path)
    return names


def _zip_member_names(bios_path: Path) -> set[str]:
    """Basenames of the files inside every ZIP under bios/, both cases."""
    import zipfile

    names: set[str] = set()
    for zpath in bios_path.rglob("*.zip"):
        try:
            with zipfile.ZipFile(zpath) as zf:
                members = [m for m in zf.namelist() if not m.endswith("/")]
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"  WARNING: unreadable archive {zpath}: {exc}", file=sys.stderr)
            continue
        for member in members:
            basename = member.rsplit("/", 1)[-1]
            names.update((basename, basename.lower()))
    return names


def _resolve_source(
    fname: str,
    by_name: dict[str, list],
    by_name_lower: dict[str, str],
    data_names: set[str] | None = None,
    by_path_suffix: dict | None = None,
    file_entry: dict | None = None,
    db_files: dict | None = None,
    bios_dir: str | None = None,
) -> str | None:
    """Return the source category for a file, or None if not found.

    Returns ``"bios"`` (in database.json / bios/), ``"data"`` (in data/),
    or ``None`` (not available anywhere).

    A name hit whose size contradicts a size the emulator verifies is not the
    file, so it does not count as available.
    """

    def _name_hit(name: str) -> bool:
        """Whether the files indexed under *name* can be this entry."""
        if not file_entry or db_files is None:
            return True
        sha1s = by_name.get(name) or []
        if not isinstance(sha1s, list):
            sha1s = [sha1s]
        return any(
            name_match_size_ok(file_entry, db_files.get(sha1, {}).get("size"))
            for sha1 in sha1s
        )

    # bios/ via database.json by_name
    if fname in by_name and _name_hit(fname):
        return "bios"
    stripped = fname.rstrip("/")
    basename = stripped.rsplit("/", 1)[-1] if "/" in stripped else None
    if basename and basename in by_name and _name_hit(basename):
        return "bios"
    key = fname.lower()
    if key in by_name_lower and _name_hit(by_name_lower[key]):
        return "bios"
    if basename:
        if basename.lower() in by_name_lower and _name_hit(
            by_name_lower[basename.lower()]
        ):
            return "bios"
    # bios/ via by_path_suffix (regional variants)
    if by_path_suffix and fname in by_path_suffix:
        return "bios"
    # bios/ under the canonical MAME set name, as resolve_local_file does:
    # a renamed archive is held once, under the name the dedup kept.
    canonical = get_mame_clone_map(bios_dir).get(fname)
    if canonical and canonical != fname:
        if canonical in by_name and _name_hit(canonical):
            return "bios"
        if canonical.lower() in by_name_lower and _name_hit(
            by_name_lower[canonical.lower()]
        ):
            return "bios"
    if data_names and _data_hit(fname, basename, file_entry, data_names):
        return "data"
    return None


def _data_hit(
    fname: str, basename: str | None, file_entry: dict | None, data_names: set[str]
) -> bool:
    """Whether data/ or a ZIP holds the name: a directory entry looks for a
    directory, a file entry for a file. A file entry that declares a hash is
    held only by a copy carrying it: touchhle's LiberationMono-Regular.ttf
    read as held because idtech4a++'s extras.zip has another font of that
    name."""
    looked_up = [fname, fname.lower()] + ([basename, basename.lower()] if basename else [])
    if fname.endswith("/") or (file_entry or {}).get("type") == "directory":
        looked_up = [name.rstrip("/") + "/" for name in looked_up]
        return any(name in data_names for name in looked_up)
    if not any(name in data_names for name in looked_up):
        return False
    declared = _declared_content(file_entry or {})
    if not declared:
        return True
    return any(
        _content_matches(location, declared)
        for location in _supplemental_locations().get((basename or fname).lower(), ())
    )


_HASH_FIELDS = ("sha1", "md5", "sha256", "crc32")


def _declared_content(file_entry: dict) -> dict[str, set[str]]:
    """The hashes an entry declares, lowercased, by algorithm."""
    declared: dict[str, set[str]] = {}
    for field in _HASH_FIELDS:
        raw = file_entry.get(field)
        values = raw if isinstance(raw, list) else [raw]
        found = {str(v).lower().removeprefix("0x") for v in values if v}
        if found:
            declared[field] = found
    return declared


_locations_cache: dict[tuple[str, str], dict[str, list[tuple[str, str]]]] = {}


def _supplemental_locations(
    data_root: str = "data", bios_root: str = "bios"
) -> dict[str, list[tuple[str, str]]]:
    """Where each name of the supplemental index sits: a data/ file, or a
    member of a ZIP under bios/, keyed by lowercased basename."""
    key = (data_root, bios_root)
    if key in _locations_cache:
        return _locations_cache[key]
    found: dict[str, list[tuple[str, str]]] = {}
    root = Path(data_root)
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                found.setdefault(path.name.lower(), []).append((str(path), ""))
    for zpath in Path(bios_root).rglob("*.zip") if Path(bios_root).is_dir() else ():
        try:
            with zipfile.ZipFile(zpath) as zf:
                members = [m for m in zf.namelist() if not m.endswith("/")]
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"  WARNING: unreadable archive {zpath}: {exc}", file=sys.stderr)
            continue
        for member in members:
            found.setdefault(member.rsplit("/", 1)[-1].lower(), []).append(
                (str(zpath), member)
            )
    _locations_cache[key] = found
    return found


def _content_matches(location: tuple[str, str], declared: dict[str, set[str]]) -> bool:
    """Whether the file or member at *location* carries a declared hash."""
    path, member = location
    try:
        if member:
            with zipfile.ZipFile(path) as zf:
                if set(declared) == {"crc32"}:
                    return f"{zf.getinfo(member).CRC:08x}" in declared["crc32"]
                data = zf.read(member)
        else:
            data = Path(path).read_bytes()
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        print(f"  WARNING: cannot read {path} {member}: {exc}", file=sys.stderr)
        return False
    actual = {
        "sha1": hashlib.sha1(data).hexdigest(),
        "md5": hashlib.md5(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
    }
    return any(actual[field] in values for field, values in declared.items())


class FileConsumers:
    """Who reads each collected file, joined through the resolver.

    The site joined database files to emulators and platforms by bare name:
    each of the four pak0.pk3 of four games was attributed to every engine
    reading a pak0.pk3. A consumer here is an entry that resolves to the
    file by the evidence the pack uses, so a homonym is nobody's file.
    """

    def __init__(self, db: dict, zip_contents: dict | None = None) -> None:
        self._db = db
        self._zip_contents = zip_contents
        self._sha1_by_path = {
            path: sha1
            for sha1, entry in db.get("files", {}).items()
            for path in {entry.get("path", ""), *entry.get("paths", [])}
            if path
        }
        self.platforms: dict[str, set[str]] = {}
        self.emulators: dict[str, set[str]] = {}

    def sha1_of(self, entry: dict, dest: str = "", owner: str = "") -> str | None:
        """The collected file an entry resolves to, or None.

        A profile entry names its owner, as the builder does: without it the
        name step chose another tree's copy, and the page credited the file
        the pack does not ship.
        """
        if not entry.get("name"):
            return None
        if owner:
            entry = {**entry, "source_profile": owner}
        path, status = resolve_local_file(
            entry, self._db, self._zip_contents, dest_hint=dest
        )
        if not path or status in ("not_found", "hash_mismatch"):
            return None
        return self._sha1_by_path.get(path)

    def add_platforms(self, configs: dict[str, dict]) -> None:
        for name, config in configs.items():
            for system in config.get("systems", {}).values():
                for entry in system.get("files", []):
                    sha1 = self.sha1_of(entry, entry.get("destination", ""))
                    if sha1:
                        self.platforms.setdefault(sha1, set()).add(name)

    def add_emulators(self, profiles: dict[str, dict]) -> None:
        for name, profile in profiles.items():
            if profile.get("type") == "alias":
                continue
            for entry in profile.get("files", []):
                sha1 = self.sha1_of(entry, entry.get("path") or "", owner=name)
                if sha1:
                    self.emulators.setdefault(sha1, set()).add(name)


def entry_source(f: dict, index: dict) -> str | None:
    """Where the collection holds a profile entry, or None.

    By name, path and alias, then by any hash the entry declares. The gap
    report and the site pages read this one function, so a file is held or
    missing in the same way on both.
    """
    by_name, by_name_lower = index["by_name"], index["by_name_lower"]
    data_names, by_path_suffix = index.get("data_names"), index["by_path_suffix"]
    db_files = index["db_files"]
    fname = f.get("name", "")
    path_field = f.get("path") or ""
    # A directory is held when the collection holds files under it, the rule
    # the pack and verify --emulator expand it by: a bare-name lookup missed
    # cdogs' .wolf3d/, whose 695 files every pack carries.
    if f.get("type") == "directory" and directory_members(
        {"indexes": {"by_path_suffix": by_path_suffix}}, path_field or fname
    ):
        return "bios"
    for candidate in [fname, *([path_field] if path_field != fname else []),
                      *(f.get("aliases") or [])]:
        if not candidate:
            continue
        source = _resolve_source(
            candidate, by_name, by_name_lower, data_names, by_path_suffix, f, db_files,
            index.get("bios_dir"),
        )
        if source is not None:
            return source

    def values(field: str) -> list[str]:
        raw = f.get(field) or []
        return [str(v).lower() for v in (raw if isinstance(raw, list) else [raw]) if v]

    held = (
        any(index["by_md5"].get(v) for v in parse_md5_list(f.get("md5")))
        or any(v in db_files for v in values("sha1"))
        # rust_dos names its SC-55 ROMs by sha256 alone
        or any(v in index["by_sha256"] for v in values("sha256"))
        or any(index["by_crc32"].get(v) for v in values("crc32"))
    )
    return "bios" if held else None


def _resolve_archive_source(
    archive_name: str,
    by_name: dict[str, list],
    by_name_lower: dict[str, str],
    data_names: set[str] | None = None,
    by_path_suffix: dict | None = None,
) -> str:
    """Resolve source for an archive (ZIP) name, returning a source category string."""
    result = _resolve_source(
        archive_name, by_name, by_name_lower, data_names, by_path_suffix,
    )
    return result if result is not None else "missing"


def _first_hash(entry: dict) -> str:
    """The first hash an entry declares, strongest first, or ''."""
    for field in ("sha1", "sha256", "md5", "crc32"):
        value = entry.get(field)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value:
            return f"{field}:{str(value).lower()}"
    return ""


def slot_key(f: dict) -> tuple:
    """The slot a profile entry fills: its entries on one slot are the
    versions its code accepts there, and holding any of them fills it."""
    fname = f.get("name", "")
    return (
        fname,
        f.get("archive"),
        f.get("path") or fname,
        f.get("system"),
        f.get("variant_group"),
        f.get("mode", "both"),
    )


def _cross_reference_profile(
    emu_name: str,
    profile: dict,
    declared: dict[str, set[str]],
    report: dict,
    index: dict,
    standalone_cores: set[str] | None = None,
) -> None:
    """Compare one emulator profile against what the platforms declare.

    Split out of cross_reference, where it was a 200-line loop body that
    carried the whole function's branching: every file is resolved by name,
    by path, by hash and by archive membership before it can be called a
    gap. *index* carries the database lookups those steps share.
    """
    by_name = index["by_name"]
    by_name_lower = index["by_name_lower"]
    by_path_suffix = index["by_path_suffix"]
    data_names = index["data_names"]
    all_declared = index["all_declared"]
    emu_files = profile.get("files", [])
    systems = profile.get("systems", [])

    # Skip filename-agnostic profiles (BIOS detected without fixed names)
    if profile.get("bios_mode") == "agnostic":
        return

    if all_declared is not None:
        platform_names = all_declared
    else:
        platform_names = set()
        for sys_id in systems:
            platform_names.update(declared.get(_norm_system_id(sys_id), set()))

    # The build a platform runs decides which entries it reads, as in
    # find_undeclared_files: without a platform, the libretro build.
    is_standalone = runs_standalone(emu_name, profile, standalone_cores or set())
    gaps = []
    covered = []
    unsourceable_list: list[dict] = []
    archive_gaps: dict[tuple, dict] = {}
    seen_files: set[tuple] = set()

    # The entries of one profile on one slot are the versions its code
    # accepts there (raze's ten DUKE3D.GRP): holding any of them fills the
    # slot, as verify --emulator credits it.
    versions: dict[tuple, list[dict]] = {}
    for f in emu_files:
        versions.setdefault(slot_key(f), []).append(f)

    for f in emu_files:
        fname = f.get("name", "")
        # The destination the running build reads, as find_undeclared_files
        # takes it: Batocera's standalone Cemu reads keys.txt at
        # standalone_path, and the libretro Cemu/keys.txt reported here
        # settled the declaration on a slot the platform never fills.
        if is_standalone:
            effective_path = f.get("standalone_path") or f.get("path") or fname
        else:
            effective_path = f.get("path") or fname
        seen_key = slot_key(f)
        if not fname or seen_key in seen_files:
            continue

        # Collect unsourceable files separately (documented, not a gap)
        unsourceable_reason = f.get("unsourceable", "")
        if unsourceable_reason:
            seen_files.add(seen_key)
            unsourceable_list.append({
                "name": fname,
                "required": f.get("required", False),
                "reason": unsourceable_reason,
                "source_ref": f.get("source_ref", ""),
            })
            continue

        if outside_gap_scope(f, is_standalone):
            continue

        archive = f.get("archive")

        # Check platform declaration: by name, by an alias the core answers
        # to (RetroArch lists amiberry's cd32.rom as kick40060.CD32), or by
        # archive.
        in_platform = declaration_settles(f, fname, platform_names, effective_path)

        if in_platform:
            seen_files.add(seen_key)
            covered.append({
                "name": fname,
                "path": effective_path,
                "required": f.get("required", False),
                "in_platform": True,
            })
            continue

        seen_files.add(seen_key)

        # Group archived files by archive name
        if archive:
            archive_key = (
                archive,
                f.get("system"),
                f.get("variant_group"),
                f.get("mode", "both"),
            )
            if archive_key not in archive_gaps:
                source = _resolve_archive_source(
                    archive, by_name, by_name_lower, data_names,
                    by_path_suffix,
                )
                archive_gaps[archive_key] = {
                    "name": archive,
                    "path": archive,
                    "required": False,
                    "note": "",
                    "source_ref": "",
                    "in_platform": False,
                    "in_repo": source != "missing",
                    "source": source,
                    "archive": archive,
                    "archive_file_count": 0,
                    "archive_required_count": 0,
                }
            entry = archive_gaps[archive_key]
            entry["archive_file_count"] += 1
            if f.get("required", False):
                entry["archive_required_count"] += 1
                entry["required"] = True
            if not entry["source_ref"] and f.get("source_ref"):
                entry["source_ref"] = f["source_ref"]
            continue

        # --- resolve source provenance ---
        storage = f.get("storage", "")
        if storage in ("release", "large_file"):
            source = "large_file"
        else:
            source = next(
                (
                    found for version in versions[seen_key]
                    if (found := entry_source(version, index)) is not None
                ),
                "missing",
            )

        in_repo = source != "missing"

        entry = {
            "name": fname,
            "path": effective_path,
            "required": f.get("required", False),
            "note": f.get("note", ""),
            "source_ref": f.get("source_ref", ""),
            "in_platform": False,
            "in_repo": in_repo,
            "source": source,
            # What tells this file from a homonym another emulator wants.
            "identity": _first_hash(f),
        }
        gaps.append(entry)

    # Append grouped archive gaps
    for ag in sorted(archive_gaps.values(), key=lambda e: e["name"]):
        gaps.append(ag)

    report[emu_name] = {
        "emulator": profile.get("emulator", emu_name),
        "systems": systems,
        "total_files": len(emu_files),
        "platform_covered": len(covered),
        "gaps": len(gaps),
        "gap_in_repo": sum(1 for g in gaps if g["in_repo"]),
        "gap_missing": sum(1 for g in gaps if g["source"] == "missing"),
        "gap_bios": sum(1 for g in gaps if g["source"] == "bios"),
        "gap_data": sum(1 for g in gaps if g["source"] == "data"),
        "gap_large_file": sum(1 for g in gaps if g["source"] == "large_file"),
        "gap_details": gaps,
        "unsourceable": unsourceable_list,
    }


def cross_reference(
    profiles: dict[str, dict],
    declared: dict[str, set[str]],
    db: dict,
    platform_data_dirs: dict[str, set[str]] | None = None,
    data_names: set[str] | None = None,
    all_declared: dict[str, list[tuple[int | None, str]]] | None = None,
    standalone_cores: set[str] | None = None,
) -> dict:
    """Compare emulator profiles against platform declarations.

    Returns a report with gaps (files emulators need but platforms don't list)
    and coverage stats.  Each gap entry carries a ``source`` field indicating
    where the file is available: ``"bios"`` (bios/ via database.json),
    ``"data"`` (data/ directory), ``"large_file"`` (GitHub release asset),
    or ``"missing"`` (not available anywhere).

    The boolean ``in_repo`` is derived: ``source != "missing"``.

    When *all_declared* is provided (every filename declared by any platform
    for any system, with its sizes and slots), it is used for the ``in_platform`` check
    instead of the per-system lookup.  This is appropriate for the global
    gap analysis page where "undeclared" means "no platform declares it at all".
    """
    platform_data_dirs = platform_data_dirs or {}
    by_name = db.get("indexes", {}).get("by_name", {})
    by_name_lower = {k.lower(): k for k in by_name}
    by_md5 = db.get("indexes", {}).get("by_md5", {})
    by_crc32 = db.get("indexes", {}).get("by_crc32", {})
    by_sha256 = db.get("indexes", {}).get("by_sha256", {})
    by_path_suffix = db.get("indexes", {}).get("by_path_suffix", {})
    db_files = db.get("files", {})
    report = {}

    index = {
        "by_name": by_name,
        "by_name_lower": by_name_lower,
        "by_md5": by_md5,
        "by_crc32": by_crc32,
        "by_sha256": by_sha256,
        "by_path_suffix": by_path_suffix,
        "db_files": db_files,
        "data_names": data_names,
        "all_declared": all_declared,
        "bios_dir": db.get("bios_dir"),
    }
    for emu_name, profile in profiles.items():
        _cross_reference_profile(
            emu_name, profile, declared, report, index, standalone_cores
        )

    return report


def print_report(report: dict, scope: str = "all emulators") -> None:
    """Print a human-readable gap analysis report.

    `scope` names what the totals cover: under --platform, the emulators
    that platform runs, not all of them.
    """
    print("Emulator vs Platform Gap Analysis")
    print("=" * 60)
    print("* marks a file the emulator requires")

    total_gaps = 0
    totals: dict[str, int] = {"bios": 0, "data": 0, "large_file": 0, "missing": 0}

    for emu_name, data in sorted(report.items()):
        gaps = data["gaps"]
        if gaps == 0:
            continue

        parts = []
        for key in ("bios", "data", "large_file", "missing"):
            count = data.get(f"gap_{key}", 0)
            if count:
                parts.append(f"{count} {key}")
        status = ", ".join(parts) if parts else "OK"

        systems = ", ".join(data["systems"])
        print(f"\n{data['emulator']}" + (f" ({systems})" if systems else ""))
        print(
            f"  {data['total_files']} files in profile, "
            f"{data['platform_covered']} declared by platforms, "
            f"{gaps} undeclared"
        )
        print(f"  Gaps: {status}")

        for g in data["gap_details"]:
            req = "*" if g["required"] else " "
            src = g.get("source", "missing").upper()
            # One line per file: a note spanning lines fell to column 0
            # between entries and read as an emulator heading.
            note = f": {' '.join(str(g['note']).split())}" if g["note"] else ""
            archive_info = ""
            if g.get("archive"):
                fc = g.get("archive_file_count", 0)
                rc = g.get("archive_required_count", 0)
                archive_info = f" ({fc} files, {rc} required)"
            print(f"    {req} {g['name']} [{src}]{archive_info}{note}")

        total_gaps += gaps
        for key in totals:
            totals[key] += data.get(f"gap_{key}", 0)

    print(f"\n{'=' * 60}")
    print(f"Total: {total_gaps} undeclared files across {scope}")
    available = totals["bios"] + totals["data"] + totals["large_file"]
    print(f"  {available} available (bios: {totals['bios']}, data: {totals['data']}, "
          f"large_file: {totals['large_file']})")
    print(f"  {totals['missing']} missing (need to be sourced)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Emulator vs platform gap analysis")
    parser.add_argument("--emulators-dir", default=DEFAULT_EMULATORS_DIR)
    parser.add_argument("--platforms-dir", default=DEFAULT_PLATFORMS_DIR)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--emulator", "-e", help="Analyze single emulator")
    parser.add_argument(
        "--platform", "-p", help="Restrict analysis to one platform's cores"
    )
    parser.add_argument("--target", "-t", help="Hardware target (e.g., switch, rpi4)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()
    if args.platform:
        require_platform(args.platform, args.platforms_dir)

    profiles = load_emulator_profiles(args.emulators_dir)
    if args.emulator:
        profiles = {k: v for k, v in profiles.items() if k == args.emulator}

    if args.target and not args.platform:
        parser.error("--target requires --platform")

    standalone_cores: set[str] = set()
    if args.platform:
        from common import load_target_config, resolve_platform_cores

        target_cores = (
            load_target_config(args.platform, args.target, args.platforms_dir)
            if args.target
            else None
        )
        config = load_platform_config(args.platform, args.platforms_dir)
        relevant = resolve_platform_cores(config, profiles, target_cores=target_cores)
        profiles = {k: v for k, v in profiles.items() if k in relevant}
        standalone_cores = set(map(str, config.get("standalone_cores", [])))

    if not profiles:
        print("No emulator profiles found.", file=sys.stderr)
        return

    selected = [args.platform] if args.platform else None
    declared, plat_data_dirs = load_platform_files(args.platforms_dir, selected)
    db = load_database(args.db)
    data_names = _build_supplemental_index()
    report = cross_reference(
        profiles, declared, db, plat_data_dirs, data_names,
        all_declared=declared_names(args.platforms_dir, selected, db),
        standalone_cores=standalone_cores,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(
            report,
            f"the emulators {args.platform} runs" if args.platform else "all emulators",
        )


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
