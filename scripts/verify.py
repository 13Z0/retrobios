#!/usr/bin/env python3
"""Platform-native BIOS verification engine.

Replicates the exact verification logic of each platform:
- RetroArch/Lakka/RetroPie: file existence only (core_info.c path_is_valid)
- Batocera: MD5 + checkInsideZip, no required distinction (batocera-systems:1062-1091)
- Recalbox: MD5 + mandatory/hashMatchMandatory, 3-color severity (Bios.cpp:109-130)
- RetroBat: same as Batocera
- EmuDeck: MD5 whitelist per system
- RetroDECK: MD5 per file via component manifests
- RomM: size + any hash, no ZIP inspection (firmware.py verify_file_hashes)
- ROCKNIX: MD5 + checkInsideZip with altmd5 (rocknix-systems checkBios)
- MiSTer FPGA: MD5 of the file at its destination, no ZIP inspection
- BizHawk: SHA1 firmware hash verification

Cross-references emulator profiles to detect undeclared files used by available cores.

Usage:
    python scripts/verify.py --all
    python scripts/verify.py --platform batocera
    python scripts/verify.py --all --include-archived
    python scripts/verify.py --all --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Mapping

import slots

sys.path.insert(0, os.path.dirname(__file__))
from hashing import parse_md5_list
from common import (
    platform_arg,
    target_listing_line,
    suggest_names,
    require_platform,
    archive_entry,
    archive_members,
    profiles_for_systems,
    PROFILE_IDENTITY_FIELDS,
    list_available_targets,
    list_platform_system_ids,
    list_registered_platforms,
    build_target_cores_cache,
    build_zip_contents_index,
    check_inside_zip,
    compute_hashes,
    expand_directory_entries,
    platform_declarations,
    filter_systems_by_target,
    group_identical_platforms,
    list_emulator_profiles,
    list_system_ids,
    load_data_dir_registry,
    load_database,
    load_emulator_profiles,
    load_platform_config,
    md5sum,
    require_yaml,
    resolve_local_file,
    ProfileSelectionError,
    resolve_platform_cores,
    platform_layouts,
    runs_standalone,
    standalone_set as platform_standalone_set,
    entry_serves_systems,
    sanitize_pack_path,
    select_emulator_profiles,
)

yaml = require_yaml()
from nativemode import (
    SIZE_AND_ANY_HASH,
    digest_algorithm,
    hash_mismatch_excludes_file,
    normalize as normalize_mode,
    reads_file_contents,
)
from validation import (
    baseline_verdict,
    declaration_settles,
    frontend_digest_matches,
    existence_discrepancy,
    destination_owners,
    validated_choice,
    agnostic_substitute,
    _build_validation_index,
    _parse_validation,
    build_ground_truth,
    check_file_validation,
    deliverable,
    filter_files_by_mode,
    installed_with_emulator,
    outside_gap_scope,
)

DEFAULT_DB = "database.json"
DEFAULT_PLATFORMS_DIR = "platforms"
DEFAULT_BIOS_DIR = "bios"
# The repository's platforms wherever the script runs from, for library calls
# that pass no directory.
_REPO_PLATFORMS = os.path.join(os.path.dirname(__file__), "..", "platforms")
DEFAULT_EMULATORS_DIR = "emulators"


# Status model -aligned with Batocera BiosStatus (batocera-systems:967-969)


class Status:
    OK = "ok"
    UNTESTED = "untested"  # file present, hash not confirmed: a warning, never --strict
    MISSING = "missing"


# Severity for per-file required/optional distinction
class Severity:
    CRITICAL = "critical"  # required file missing or bad hash (Recalbox RED)
    WARNING = "warning"  # optional missing or hash mismatch (Recalbox YELLOW)
    INFO = "info"  # optional missing on existence-only platform
    OK = "ok"  # file verified


_STATUS_ORDER = {Status.OK: 0, Status.UNTESTED: 1, Status.MISSING: 2}
_SEVERITY_ORDER = {
    Severity.OK: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.CRITICAL: 3,
}


# Verification functions


def verify_entry_existence(
    file_entry: dict,
    local_path: str | None,
    validation_index: dict[str, dict] | None = None,
    db: dict | None = None,
    destination: str = "",
    owners: dict | None = None,
    resolve_status: str = "",
    platform_display: str = "",
) -> dict:
    """RetroArch verification: path_is_valid(), a file that exists is OK."""
    name = file_entry.get("name", "")
    required = file_entry.get("required", True)
    if not local_path:
        return {"name": name, "status": Status.MISSING, "required": required}
    result = {"name": name, "status": Status.OK, "required": required}
    if resolve_status == "hash_mismatch":
        # The frontend reads no bytes, so the file stays, and the declared hash
        # it contradicts is reported, as the pack does.
        result["discrepancy"] = existence_discrepancy(
            file_entry, local_path, platform_display
        )
        return result
    if not validation_index:
        return result
    if db:
        # The pack ships the variant this returns, so a disagreement it
        # settles is not reported.
        _chosen, disagreement = validated_choice(
            file_entry, local_path, db, validation_index, "bios", None,
            destination, owners,
        )
    else:
        check = check_file_validation(local_path, name, validation_index)
        disagreement = f"{', '.join(check[1])} says {check[0]}" if check else None
    if disagreement:
        result["discrepancy"] = f"file present (OK) but {disagreement}"
    return result


def verify_entry_md5(
    file_entry: dict,
    local_path: str | None,
    resolve_status: str = "",
    hash_rule: str = "",
) -> dict:
    """MD5 verification -Batocera md5sum + Recalbox multi-hash + Md5Composite.

    Under SIZE_AND_ANY_HASH (RomM) the size and any one declared hash decide.
    """
    name = file_entry.get("name", "")
    expected_md5 = file_entry.get("md5", "")
    zipped_file = file_entry.get("zipped_file")
    required = file_entry.get("required", True)
    base = {"name": name, "required": required}

    # One parse for verify, the builder and the pack check: Recalbox writes
    # md5 in uppercase, and a copy that lowercased only comma lists judged
    # a single uppercase value differently from the others.
    md5_list = parse_md5_list(expected_md5)

    if not local_path:
        return {**base, "status": Status.MISSING}

    if hash_rule == SIZE_AND_ANY_HASH:
        if frontend_digest_matches(file_entry, local_path, "md5", hash_rule):
            return {**base, "status": Status.OK, "path": local_path}
        return {
            **base,
            "status": Status.UNTESTED,
            "path": local_path,
            "reason": "the declared size and at least one declared hash must match",
        }

    if zipped_file:
        found_in_zip = False
        had_error = False
        for md5_candidate in md5_list or [""]:
            result = check_inside_zip(local_path, zipped_file, md5_candidate)
            if result == Status.OK:
                return {**base, "status": Status.OK, "path": local_path}
            if result == "error":
                had_error = True
            elif result != "not_in_zip":
                found_in_zip = True
        if had_error and not found_in_zip:
            return {
                **base,
                "status": Status.UNTESTED,
                "path": local_path,
                "reason": f"{local_path} read error",
            }
        if not found_in_zip:
            return {
                **base,
                "status": Status.UNTESTED,
                "path": local_path,
                "reason": f"{zipped_file} not found inside ZIP",
            }
        return {
            **base,
            "status": Status.UNTESTED,
            "path": local_path,
            "reason": f"{zipped_file} MD5 mismatch inside ZIP",
        }

    if not md5_list:
        if resolve_status == "hash_mismatch":
            # No md5 to compare does not mean nothing was compared: the entry
            # declares a sha1 or a crc32 the local file contradicts, and the
            # builder drops it for exactly that. Reporting OK here said the
            # collection holds bytes it does not.
            return {
                **base,
                "status": Status.UNTESTED,
                "path": local_path,
                "reason": "declared hash contradicted by the local file",
            }
        return {**base, "status": Status.OK, "path": local_path}

    if resolve_status == "md5_exact":
        return {**base, "status": Status.OK, "path": local_path}

    # The builder's comparison, so the two count the same files as held.
    if frontend_digest_matches(file_entry, local_path, "md5"):
        return {**base, "status": Status.OK, "path": local_path}
    actual_md5 = md5sum(local_path)
    return {
        **base,
        "status": Status.UNTESTED,
        "path": local_path,
        "reason": f"expected {md5_list[0][:12]}... got {actual_md5[:12]}...",
    }


def verify_entry_sha1(
    file_entry: dict,
    local_path: str | None,
) -> dict:
    """SHA1 verification -BizHawk firmware hash check."""
    name = file_entry.get("name", "")
    expected_sha1 = file_entry.get("sha1", "")
    required = file_entry.get("required", True)
    base = {"name": name, "required": required}

    if not local_path:
        return {**base, "status": Status.MISSING}

    if not expected_sha1:
        return {**base, "status": Status.OK, "path": local_path}

    hashes = compute_hashes(local_path)
    actual_sha1 = hashes["sha1"].lower()
    if actual_sha1 == expected_sha1.lower():
        return {**base, "status": Status.OK, "path": local_path}

    return {
        **base,
        "status": Status.UNTESTED,
        "path": local_path,
        "reason": f"expected {expected_sha1[:12]}... got {actual_sha1[:12]}...",
    }


# Severity mapping per platform


def _emulator_severity(status: str, required: bool, hle_fallback: bool) -> str:
    """Severity when the emulator's own code is the check.

    A file present but refused by the core's validation, or a different dump
    than the one the profile declares, is not a file the core runs with:
    read in existence mode it counted OK, and `verify --emulator` announced
    1/1 OK above the line that said the core would refuse it.
    """
    if status == Status.UNTESTED:
        return Severity.WARNING
    return compute_severity(status, required, "existence", hle_fallback)


def compute_severity(
    status: str,
    required: bool,
    mode: str,
    hle_fallback: bool = False,
) -> str:
    """Map (status, required, verification_mode, hle_fallback) -> severity.

    Based on native platform behavior + emulator HLE capability:
    - RetroArch (existence): required+missing = warning, optional+missing = info
    - Batocera/Recalbox/RetroBat/EmuDeck (md5): hash-based verification
    - BizHawk (sha1): same severity rules as md5
    - hle_fallback: core works without this file via HLE -> always INFO when missing
    """
    if status == Status.OK:
        return Severity.OK

    # HLE fallback: core works without this file regardless of platform requirement
    if hle_fallback and status == Status.MISSING:
        return Severity.INFO

    if not reads_file_contents(mode):
        if status == Status.MISSING:
            return Severity.WARNING if required else Severity.INFO
        return Severity.OK

    # md5 mode (Batocera, Recalbox, RetroBat, EmuDeck)
    if status == Status.MISSING:
        return Severity.CRITICAL if required else Severity.WARNING
    if status == Status.UNTESTED:
        return Severity.WARNING
    return Severity.OK


# ZIP content index

# Cross-reference: undeclared files used by cores


def _build_expected(file_entry: dict, checks: list[str]) -> dict:
    """Extract expected validation values from an emulator profile file entry."""
    expected: dict = {}
    if not checks:
        return expected
    if "size" in checks:
        for key in ("size", "min_size", "max_size"):
            if file_entry.get(key) is not None:
                expected[key] = file_entry[key]
    for hash_type in ("crc32", "md5", "sha1", "sha256"):
        if hash_type in checks and file_entry.get(hash_type):
            expected[hash_type] = file_entry[hash_type]
    adler_val = file_entry.get("known_hash_adler32") or file_entry.get("adler32")
    if adler_val:
        expected["adler32"] = adler_val
    return expected


def _name_in_index(
    name: str,
    by_name: dict,
    by_path_suffix: dict | None = None,
    data_names: set[str] | None = None,
    by_name_lower: dict[str, str] | None = None,
) -> bool:
    """Check if a name is resolvable in the database indexes or data directories."""
    # Strip trailing slash for directory-type entries (e.g. nestopia/samples/foo/)
    name = name.rstrip("/")
    if name in by_name:
        return True
    basename = name.rsplit("/", 1)[-1] if "/" in name else name
    if basename != name and basename in by_name:
        return True
    # Case-insensitive by_name lookup
    if by_name_lower:
        key = name.lower()
        if key in by_name_lower:
            return True
        if basename != name and basename.lower() in by_name_lower:
            return True
    if by_path_suffix and name in by_path_suffix:
        return True
    if data_names:
        if name in data_names or name.lower() in data_names:
            return True
        if basename != name and (
            basename in data_names or basename.lower() in data_names
        ):
            return True
    return False


def _candidate_verdict(
    file_entry: dict,
    fname: str,
    is_standalone: bool,
    include_all: bool,
    declared_names: Mapping[str, list[tuple[int | None, str]]] | set[str],
    dest: str = "",
) -> str:
    """Whether a profile entry can be a gap, and whether it is settled.

    Three answers. "keep" means it is a candidate. "settled" means something
    has answered for it -the platform declares it, or the profile documents
    it as unsourceable -so the same requirement reached from another profile
    must not be reconsidered. "skip" means it does not apply in this context,
    which leaves it open for a profile where it does: the same file can be
    libretro-only here and standalone-only there.
    """
    if file_entry.get("unsourceable"):
        return "settled"
    if outside_gap_scope(file_entry, is_standalone):
        return "skip"
    if not include_all and declaration_settles(
        file_entry, fname, declared_names, dest
    ):
        return "settled"
    return "keep"


def _identity_of(entry: dict) -> dict:
    """The fields that tell a profile's file apart from a same-named one."""
    return {
        field: entry.get(field)
        for field in PROFILE_IDENTITY_FIELDS
        if entry.get(field) not in (None, "", [])
    }


def find_undeclared_files(
    config: dict,
    emulators_dir: str,
    db: dict,
    emu_profiles: dict | None = None,
    target_cores: set[str] | None = None,
    data_names: set[str] | None = None,
    include_all: bool = False,
    declared_names: Mapping[str, list[tuple[int | None, str]]] | None = None,
) -> list[dict]:
    """Find files needed by cores but not declared in platform config.

    declared_names overrides the default enriched declarations from
    platform_declarations.  Pass the strict ones (YAML names only) when
    building packs so alias-only names still get packed.
    """
    if declared_names is None:
        declared_names = platform_declarations(config, db)

    # Whether the builder drops a file whose local copy contradicts its
    # declared hash, which decides if such a copy counts as held here.
    shipped_on_mismatch = hash_mismatch_excludes_file(
        config.get("verification_mode")
    )

    # Collect data_directory refs
    declared_dd: set[str] = set()
    for sys_id, system in config.get("systems", {}).items():
        for dd in system.get("data_directories", []):
            ref = dd.get("ref", "")
            if ref:
                declared_dd.add(ref)

    by_name = db.get("indexes", {}).get("by_name", {})
    by_name_lower = {k.lower(): k for k in by_name}
    by_path_suffix = db.get("indexes", {}).get("by_path_suffix", {})
    profiles = (
        emu_profiles
        if emu_profiles is not None
        else load_emulator_profiles(emulators_dir)
    )

    relevant = resolve_platform_cores(config, profiles, target_cores=target_cores)
    standalone_set = platform_standalone_set(config)
    undeclared = []
    seen_files: set[tuple] = set()
    # Key -> the entry reported for it. A later profile reading the same
    # requirement is kept on that entry rather than counted again: a pack
    # built without the first profile still has to name the file.
    reported: dict[tuple, dict] = {}
    # Track archives: archive_name -> {in_repo, emulator, files: [...], ...}
    archive_entries: dict[tuple, dict] = {}

    for emu_name, profile in sorted(profiles.items()):
        if profile.get("type") in ("launcher", "alias"):
            continue
        if emu_name not in relevant:
            continue

        # Skip agnostic profiles entirely (filename-agnostic BIOS detection)
        if profile.get("bios_mode") == "agnostic":
            continue

        for profile, is_standalone in platform_layouts(emu_name, profile, standalone_set):

            for f in expand_directory_entries(
                profile.get("files", []), db, is_standalone
            ):
                fname = f.get("name", "")
                # One destination rule for the dedup key and for the entry: a
                # standalone build without standalone_path falls back on path.
                # The key used to skip that fallback, so clk's Acorn/basic.rom and
                # Electron/basic.rom both keyed as "basic.rom" under Batocera's
                # standalone clk and the second was dropped.
                if is_standalone:
                    dest = f.get("standalone_path") or f.get("path") or fname
                else:
                    dest = f.get("path") or fname
                raw_regions = f.get("region") or []
                region_key = tuple(
                    str(value) for value in (
                        raw_regions if isinstance(raw_regions, list) else [raw_regions]
                    )
                )
                # Same-name requirements at different paths, for different systems,
                # or in distinct variant groups are not interchangeable.
                seen_key = (
                    fname,
                    f.get("archive"),
                    dest,
                    f.get("system"),
                    f.get("variant_group"),
                    region_key,
                )
                if not fname:
                    continue
                first = reported.get(seen_key)
                duplicate = seen_key in seen_files
                if duplicate and (first is None or first["profile"] == emu_name):
                    continue
                verdict = _candidate_verdict(
                    f, fname, is_standalone, include_all, declared_names, dest
                )
                if verdict == "settled":
                    seen_files.add(seen_key)
                    continue
                if verdict == "skip":
                    continue

                archive = f.get("archive")
                seen_files.add(seen_key)

                # Archived files are grouped by archive
                if archive:
                    archive_key = (
                        archive,
                        f.get("system"),
                        f.get("variant_group"),
                        region_key,
                        is_standalone,
                    )
                    if archive_key not in archive_entries:
                        in_repo = _name_in_index(
                            archive, by_name, by_path_suffix, data_names,
                            by_name_lower,
                        )
                        archive_entries[archive_key] = {
                            "profile": emu_name,
                            "emulator": profile.get("emulator", emu_name),
                            "systems": list(profile.get("systems", [])),
                            "system": f.get("system"),
                            "region": f.get("region"),
                            "variant_group": f.get("variant_group"),
                            "name": archive,
                            "archive": archive,
                            "path": archive,
                            "required": False,
                            "hle_fallback": False,
                            "category": f.get("category", "bios"),
                            "in_repo": in_repo,
                            "note": "",
                            "checks": [],
                            "source_ref": None,
                            "expected": {},
                            "archive_file_count": 0,
                            "archive_required_count": 0,
                            "members": archive_members(archive, profile.get("files", [])),
                        }
                    entry = archive_entries[archive_key]
                    entry["archive_file_count"] += 1
                    if f.get("required", False):
                        entry["archive_required_count"] += 1
                        entry["required"] = True
                    continue

                # Resolution: storage flag, then name, then path basename
                storage = f.get("storage", "")
                if storage in ("release", "large_file"):
                    in_repo = True
                elif shipped_on_mismatch and (f.get("md5") or f.get("sha1")):
                    # The entry states what its content should be and the builder
                    # drops a copy that contradicts it, so content decides here
                    # too. A name match would not do: generic names collide across
                    # systems, and answering yes on one describes a pack that will
                    # not contain the file.
                    _lp, _st = resolve_local_file(f, db, dest_hint=dest)
                    in_repo = _lp is not None and _st != "hash_mismatch"
                else:
                    in_repo = _name_in_index(
                        fname, by_name, by_path_suffix, data_names, by_name_lower,
                    )
                    if not in_repo and dest != fname:
                        path_base = dest.rsplit("/", 1)[-1]
                        in_repo = _name_in_index(
                            path_base, by_name, by_path_suffix, data_names,
                            by_name_lower,
                        )
                    if not in_repo:
                        # Hash fallback: the repo may hold the content under a
                        # different filename (exos21.rom vs exos21.bin).
                        #
                        # A copy contradicting the declared hash counts as held
                        # only where the builder would ship it. Under existence
                        # the frontend never opens the file, so the pack carries
                        # it and reports the divergence. Under a digest mode the
                        # frontend would reject it, the builder leaves it out, and
                        # calling it available here would describe a pack that
                        # does not contain it.
                        _lp, _st = resolve_local_file(f, db, dest_hint=dest)
                        in_repo = _st != "not_found" and _lp is not None
                        if in_repo and _st == "hash_mismatch" and shipped_on_mismatch:
                            in_repo = False

                checks = _parse_validation(f.get("validation"))
                entry = {
                    "profile": emu_name,
                    "emulator": profile.get("emulator", emu_name),
                    "systems": list(profile.get("systems", [])),
                    "system": f.get("system"),
                    "region": f.get("region"),
                    "variant_group": f.get("variant_group"),
                    "name": fname,
                    "path": dest,
                    "required": f.get("required", False),
                    "hle_fallback": f.get("hle_fallback", False),
                    "bundled": installed_with_emulator(f, config.get("_key", "")),
                    "category": f.get("category", "bios"),
                    "in_repo": in_repo,
                    "note": f.get("note", ""),
                    "checks": sorted(checks) if checks else [],
                    "source_ref": f.get("source_ref"),
                    "expected": _build_expected(f, checks),
                    **_identity_of(f),
                    "sha1": f.get("sha1"),
                    "md5": f.get("md5"),
                }
                if f.get("contents"):
                    entry["contents"] = f["contents"]
                if duplicate:
                    first.setdefault("other_claims", []).append(entry)
                    continue
                reported[seen_key] = entry
                undeclared.append(entry)

    # Append grouped archive entries
    for entry in sorted(archive_entries.values(), key=lambda e: e["name"]):
        undeclared.append(entry)

    return undeclared


def find_exclusion_notes(
    config: dict,
    emulators_dir: str,
    emu_profiles: dict | None = None,
    target_cores: set[str] | None = None,
) -> list[dict]:
    """Document why certain emulator files are intentionally excluded.

    Reports:
    - Launchers (BIOS managed by standalone emulator)
    - Standalone-only files (not needed in libretro mode)
    - Frozen snapshots with files: [] (code doesn't load .info firmware)
    - Files covered by data_directories
    """
    profiles = (
        emu_profiles
        if emu_profiles is not None
        else load_emulator_profiles(emulators_dir)
    )
    platform_systems = set()
    for sys_id in config.get("systems", {}):
        platform_systems.add(sys_id)

    relevant = resolve_platform_cores(config, profiles, target_cores=target_cores)
    notes = []
    for emu_name, profile in sorted(profiles.items()):
        emu_systems = set(profile.get("systems", []))
        # Match by core resolution OR system intersection (documents all potential emulators)
        if emu_name not in relevant and not (emu_systems & platform_systems):
            continue

        emu_display = profile.get("emulator", emu_name)

        # Launcher excluded entirely
        if profile.get("type") == "launcher":
            notes.append(
                {
                    "emulator": emu_display,
                    "reason": "launcher",
                    "detail": profile.get(
                        "exclusion_note", "BIOS managed by standalone emulator"
                    ),
                }
            )
            continue

        # Profile-level exclusion note (frozen snapshots, etc.)
        exclusion_note = profile.get("exclusion_note")
        if exclusion_note:
            notes.append(
                {
                    "emulator": emu_display,
                    "reason": "exclusion_note",
                    "detail": exclusion_note,
                }
            )
            continue

        # Count standalone-only files -but only report as excluded if the
        # platform does NOT use this emulator in standalone mode
        standalone_set = platform_standalone_set(config)
        is_standalone = runs_standalone(emu_name, profile, standalone_set)
        if not is_standalone:
            standalone_files = [
                f for f in profile.get("files", []) if f.get("mode") == "standalone"
            ]
            if standalone_files:
                names = [f["name"] for f in standalone_files[:3]]
                more = (
                    f" +{len(standalone_files) - 3}"
                    if len(standalone_files) > 3
                    else ""
                )
                notes.append(
                    {
                        "emulator": emu_display,
                        "reason": "standalone_only",
                        "detail": f"{len(standalone_files)} files for standalone mode only ({', '.join(names)}{more})",
                    }
                )

    return notes


# Platform verification


_UNPLACED_REASONS = {
    "no_platform_slug": "no platform slug for its system",
    "below_platform_folder": "below the folder the platform lists",
}


def _mark_unplaced(config: dict, undeclared: list[dict], profiles: dict) -> None:
    """What the builder cannot place is not in the pack, held or not."""
    from packextras import unplaceable_extras

    unplaced = unplaceable_extras(config, undeclared, profiles)
    for u in undeclared:
        if u.get("bundled"):
            # Installed with the emulator, so already on the user's machine:
            # the pack leaves it out, held or not.
            u["in_pack"] = False
            u["omitted"] = "installed with its emulator"
            continue
        reason = unplaced.get(
            (u.get("emulator", ""), u.get("name", ""), u.get("path") or "")
        )
        if reason:
            u["in_pack"] = False
            u["omitted"] = _UNPLACED_REASONS[reason]


def _storage_verdict(file_entry: dict, emulator_pack: bool) -> dict | None:
    """The result a storage tier fixes before any lookup, as the builder sees it.

    A user_provided entry is never taken from the collection: a platform pack
    writes its instructions in the slot and counts it filled, an emulator pack
    lists it to provide. An external one is fetched from its URL when the pack
    is built, never read from bios/. Resolving either by name credited a
    same-named file the pack does not carry.
    """
    storage = file_entry.get("storage", "embedded")
    base = {"name": file_entry.get("name", ""), "required": file_entry.get("required", True)}
    if storage == "user_provided":
        if emulator_pack:
            stated = file_entry.get("unsourceable")
            why = (
                stated if isinstance(stated, str) and stated
                else file_entry.get("instructions") or "the user provides it"
            )
            return {**base, "status": Status.MISSING, "unsourceable": why}
        return {**base, "status": Status.OK, "user_provided": True}
    if storage == "external":
        return {**base, "status": Status.MISSING, "reason": "fetched from its URL when the pack is built"}
    return None


_HIDDEN_REASONS = {
    "case_twin": "a packed file of the same name in another case takes its place",
    "path_conflict": "a packed file occupies its directory",
}


def _mark_hidden(
    config: dict,
    undeclared: list[dict],
    emulators_dir: str,
    db: dict,
    emu_profiles: dict | None,
    shipped: dict[str, str],
    target_cores: set[str] | None,
    region_drops: set[str] | None = None,
    report: list[dict] | None = None,
    resolver: object | None = None,
    placed_by: dict[str, tuple[str, str, str]] | None = None,
) -> set[str]:
    """What the builder holds but cannot place beside the files it packed.

    Returns the full destinations the builder fills with a core's file.
    ``report`` is the gap report the caller already computed with the
    builder's arguments. Without it the builder's pass computes its own.
    """
    from packextras import hidden_extras

    placed: set[str] = set()
    hidden = hidden_extras(
        config, emulators_dir, db,
        emu_profiles if emu_profiles is not None
        else load_emulator_profiles(emulators_dir),
        shipped, target_cores, region_drops, placed, report, resolver, placed_by,
    )
    for u in undeclared:
        reason = hidden.get(
            (u.get("emulator", ""), u.get("name", ""), u.get("path") or "")
        )
        if reason and u.get("in_repo") and u.get("in_pack", True):
            u["in_pack"] = False
            u["omitted"] = _HIDDEN_REASONS[reason]
    return placed


def _dest_key(file_entry: dict, sys_id: str) -> str:
    """The path a declaration occupies, as the builder keys its pack."""
    dest = sanitize_pack_path(file_entry.get("destination", file_entry.get("name", "")))
    return dest or f"{sys_id}/{file_entry.get('name', '')}"


def _twin_index(
    verify_systems: dict, db: dict, base_dest: str, zip_contents: dict,
) -> tuple[dict[str, int], dict[int, tuple[str, dict]]]:
    """Which declaration the pack ships at each destination, and every declaration by id."""
    # From the module that defines the rule, not the builder's CLI: importing
    # generate_pack made every verify depend on its argparse and manifest code.
    from packresolve import preferred_entries as _preferred_entries

    preferred_entries = _preferred_entries(
        verify_systems, db, DEFAULT_BIOS_DIR, base_dest, False,
        zip_contents, True,
    )
    winners = {
        id(fe): (sid, fe)
        for sid, system in verify_systems.items()
        for fe in system.get("files", [])
    }
    return preferred_entries, winners


def _resolve_declaration(
    file_entry: dict, sys_id: str, twins: tuple, db: dict, zip_contents: dict,
    data_dir_registry: dict | None, slot_overrides: dict, mode: str,
    platform_profiles: dict,
) -> tuple[str | None, str, str | None]:
    """Resolve a declaration to the file the pack ships at its destination.

    When another declaration holds the destination, that one's file is
    returned and the evidence reset: the winner's hash match is not this
    declaration's, which is hashed against the shipped file.
    """
    preferred_entries, winners, base_dest = twins
    bare = sanitize_pack_path(file_entry.get("destination", file_entry.get("name", "")))
    preferred = preferred_entries.get(f"{base_dest}/{bare}" if base_dest else bare)
    held_by, winner = (None, file_entry)
    if preferred is not None and preferred != id(file_entry):
        held_by, winner = winners[preferred]
    local_path, resolve_status = _resolve_for_platform(
        winner, held_by or sys_id, db, zip_contents, data_dir_registry,
        slot_overrides, mode, platform_profiles,
    )
    return local_path, "" if held_by else resolve_status, held_by


def _twin_unmet(result: dict, held_by: str | None) -> bool:
    """Mark a declaration unmet at a path another declaration holds.

    The path is answered by the winner's file, which the pack counts once.
    This declaration's failure is reported, never folded into the
    destination's status.
    """
    if held_by is None or result["status"] == Status.OK:
        return False
    result["discrepancy"] = (
        f"{result.get('name', '')} is not met at the path {held_by} holds"
    )
    # Read by reports that list entries: the destination itself is held, and
    # a count by destination does not include this one.
    result["settled_by"] = held_by
    return True


def _resolve_for_platform(
    file_entry: dict,
    sys_id: str,
    db: dict,
    zip_contents: dict,
    data_dir_registry: dict | None,
    slot_overrides: dict[str, str],
    mode: str,
    platform_profiles: dict,
) -> tuple[str | None, str]:
    """Resolve one platform entry the way the pack builder does.

    A slot the arbitration decided wins over the resolver, and a frontend that
    never reads the bytes takes the filename-free core's own image.
    """
    override = slot_overrides.get(
        sanitize_pack_path(file_entry.get("destination", file_entry.get("name", "")))
    )
    if override:
        return override, "slot_arbitrated"
    local_path, status = resolve_local_file(
        file_entry, db, zip_contents, data_dir_registry=data_dir_registry
    )
    if local_path is None and not reads_file_contents(mode):
        found = agnostic_substitute(file_entry, sys_id, db, platform_profiles)
        if found:
            return found[0], "agnostic_fallback"
    return local_path, status


def verify_platform(
    config: dict,
    db: dict,
    emulators_dir: str = DEFAULT_EMULATORS_DIR,
    emu_profiles: dict | None = None,
    target_cores: set[str] | None = None,
    data_dir_registry: dict | None = None,
    regions: list[str] | None = None,
) -> dict:
    """Verify all BIOS files for a platform, including cross-reference gaps.

    A region priority list narrows the report to the files a pack built with the
    same list would carry, using the same selection function as the builder.
    """
    # Normalized once: a typo in a platform YAML must not verify with one
    # mode and be scored with another.
    mode = normalize_mode(config.get("verification_mode"))
    platform = config.get("platform", "unknown")

    # The builder settles a destination claimed by both layers, and this must read
    # the same decision, or the two tools describe different packs.
    has_zipped = any(
        fe.get("zipped_file")
        for sys in config.get("systems", {}).values()
        for fe in sys.get("files", [])
    )
    zip_contents = build_zip_contents_index(db) if has_zipped else {}

    # Loaded before the slot overrides: a caller passing no profiles (the
    # README's coverage) got no override and reported a discrepancy at a slot
    # the pack settles.
    profiles = (
        emu_profiles
        if emu_profiles is not None
        else load_emulator_profiles(emulators_dir)
    )
    base_dest = config.get("base_destination", "")
    slot_overrides = {
        (key[len(base_dest) + 1:] if base_dest and key.startswith(f"{base_dest}/") else key): path
        for key, path in slots.pack_overrides(
            config, profiles, db, zip_contents, data_dir_registry,
            target_cores=target_cores,
        ).items()
    }

    # Build HLE + validation indexes from emulator profiles
    # Ground truth comes from the emulators the platform runs. A standalone
    # profile that loads a same-named file of its own (ZEsarUX's 48K
    # cpc6128.rom against cap32's 32K one) has nothing to say about a
    # RetroArch pack, nor does its HLE: dosbox-x's FONT.ROM fallback turned a
    # missing required Batocera BIOS into INFO.
    plat_cores = resolve_platform_cores(config, profiles)
    platform_profiles = {name: profiles[name] for name in plat_cores}
    hle_index = {
        f.get("name", ""): True
        for profile in platform_profiles.values()
        for f in profile.get("files", [])
        if f.get("hle_fallback")
    }
    validation_index = _build_validation_index(platform_profiles)
    validation_owners = destination_owners(platform_profiles)

    # Filter systems by target
    if not target_cores:
        plat_cores = None
    verify_systems = filter_systems_by_target(
        config.get("systems", {}),
        profiles,
        target_cores,
        platform_cores=plat_cores,
    )

    # Per-entry results
    details = []
    # Per-destination aggregation
    file_status: dict[str, str] = {}
    file_required: dict[str, bool] = {}
    file_severity: dict[str, str] = {}

    region_drops: set[str] = set()
    region_fallbacks: list[str] = []
    region_extra_dests: dict[tuple[str, str, str], str] = {}
    if regions:
        import region as region_mod

        # The builder owns pack composition, so it owns the grouping the region
        # pass reads.  Grouping the platform files here and the core extras
        # there let the two answer differently on one request: the report kept
        # every core extra a region run withdraws from the pack.
        from packextras import platform_region_groups

        held: set[str] = set()
        region_groups, region_extra_dests = platform_region_groups(
            config,
            verify_systems,
            emulators_dir,
            db,
            config.get("base_destination", ""),
            profiles,
            target_cores=target_cores,
            held_out=held,
        )
        region_index = region_mod.platform_region_index(config, profiles, target_cores)
        region_drops = region_mod.resolve_region_drops(
            region_groups, region_index, regions, held,
        )
        # The pack README names these: no file answers the request, so every
        # candidate stays. Reported here too, or verify reads all OK for a
        # system that has no BIOS for the region asked.
        region_fallbacks = region_mod.fallback_groups(
            region_groups, region_index, regions
        )

    # A destination two systems declare with two hashes ships one file, the
    # one the builder prefers. The other declaration is scored against that
    # file, as the frontend will score it after installation: RetroDECK's
    # xroar component hashes bios/disk.rom and finds the PC-88 ROM.
    base_dest = config.get("base_destination", "")
    preferred_entries, winners = _twin_index(
        verify_systems, db, base_dest, zip_contents
    )

    shipped: dict[str, str] = {}
    shipped_keys: set[str] = set()
    results_at: dict[str, list[dict]] = {}
    for sys_id, system in verify_systems.items():
        for file_entry in system.get("files", []):
            if region_drops and (
                sanitize_pack_path(
                    file_entry.get("destination", file_entry.get("name", ""))
                )
                in region_drops
            ):
                continue
            local_path, resolve_status, held_by = _resolve_declaration(
                file_entry, sys_id, (preferred_entries, winners, base_dest),
                db, zip_contents, data_dir_registry, slot_overrides, mode,
                platform_profiles,
            )
            destination = sanitize_pack_path(
                file_entry.get("destination", file_entry.get("name", ""))
            )
            stored = _storage_verdict(file_entry, emulator_pack=False)
            if stored is not None:
                result = stored
                local_path = None
            elif not reads_file_contents(mode):
                result = verify_entry_existence(
                    file_entry,
                    local_path,
                    validation_index,
                    db,
                    destination,
                    validation_owners,
                    resolve_status,
                    config.get("platform", ""),
                )
            elif digest_algorithm(mode) == "sha1":
                result = verify_entry_sha1(file_entry, local_path)
            else:
                result = verify_entry_md5(
                    file_entry, local_path, resolve_status, config.get("hash_rule", "")
                )
                # Emulator-level validation: informational for platform packs.
                # Platform verification (MD5) is the authority. Emulator
                # mismatches are reported as discrepancies, not failures.
                if result["status"] == Status.OK:
                    _chosen, disagreement = validated_choice(
                        file_entry, local_path, db, validation_index, "bios",
                        digest_algorithm(mode), destination, validation_owners,
                    )
                    if disagreement:
                        result["discrepancy"] = f"{platform} says OK but {disagreement}"
            result["system"] = sys_id
            result["hle_fallback"] = hle_index.get(file_entry.get("name", ""), False)
            result["ground_truth"] = build_ground_truth(
                file_entry.get("name", ""),
                validation_index,
            )
            details.append(result)
            # Occupied only where the builder ships the declaration: a hash
            # the frontend refuses is left out, and the destination stays free
            # for a core's file the pack does carry.
            if (
                local_path
                and result["status"] != Status.MISSING
                and baseline_verdict(
                    file_entry, local_path, resolve_status, mode, config.get("hash_rule", "")
                )[1] is None
            ):
                shipped.setdefault(
                    f"{base_dest}/{destination}" if base_dest else destination,
                    local_path,
                )
                shipped_keys.add(_dest_key(file_entry, sys_id))
            if _twin_unmet(result, held_by):
                continue

            # Aggregate by destination, keyed as the builder keys its pack.
            # A declaration that finds nothing leaves the path to one that
            # holds a file: the builder packs that file there, whatever the
            # order the two are read in.
            dest = _dest_key(file_entry, sys_id)
            results_at.setdefault(dest, []).append(result)
            required = file_entry.get("required", True)
            cur = result["status"]
            prev = file_status.get(dest)
            hle = hle_index.get(file_entry.get("name", ""), False)
            sev = compute_severity(cur, required, mode, hle)
            if prev is not None and (cur == Status.MISSING) != (prev == Status.MISSING):
                if cur == Status.MISSING:
                    continue
                del file_status[dest], file_severity[dest]
                prev = None
            if prev is None or _STATUS_ORDER.get(cur, 0) > _STATUS_ORDER.get(prev, 0):
                file_status[dest] = cur
                file_required[dest] = required
            prev_sev = file_severity.get(dest)
            if prev_sev is None or _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(
                prev_sev, 0
            ):
                file_severity[dest] = sev

    # Cross-reference undeclared files, called exactly as the builder calls
    # it: its declarations, not the enriched ones (a core file the platform
    # also declares under another name is packed at the core's own path, 21
    # such files in the RetroArch pack), and no names from inside archives
    # or data trees (a loose file whose name exists only as a member of
    # naomi2.zip was counted "in pack" while the builder shipped nothing).
    undeclared = find_undeclared_files(
        config,
        emulators_dir,
        db,
        emu_profiles,
        target_cores=target_cores,
        declared_names=platform_declarations(config, db, enrich=False),
    )
    # The builder's own pass reads the same report: computed once here and
    # handed over as it stands now, before the markings below.
    report_for_builder = [dict(u) for u in undeclared]
    if region_drops:
        undeclared = [
            u
            for u in undeclared
            if region_extra_dests.get(
                (u.get("emulator", ""), u.get("name", ""), u.get("path") or "")
            )
            not in region_drops
        ]
    _mark_unplaced(config, undeclared, profiles)
    from packextras import ExtraResolver

    placed_by: dict[str, tuple[str, str, str]] = {}
    placed = _mark_hidden(
        config, undeclared, emulators_dir, db, emu_profiles, shipped, target_cores,
        region_drops, report_for_builder,
        ExtraResolver(
            db, DEFAULT_BIOS_DIR, zip_contents, data_dir_registry, validation_index,
            validation_owners,
        ),
        placed_by,
    )
    # What the builder places from the cores, one per pack path, by category:
    # several entries claim one path (rom.key from three Amiga cores) and the
    # builder ships one file there.
    categories = {
        (u.get("emulator", ""), u.get("name", ""), u.get("path") or ""):
            u.get("category", "bios")
        for u in report_for_builder
    }
    packed_extras = {"bios": 0, "game_data": 0}
    for key in placed_by.values():
        kind = "game_data" if categories.get(key) == "game_data" else "bios"
        packed_extras[kind] += 1
    if not reads_file_contents(mode):
        # An existence frontend is satisfied by any file at the path, and the
        # builder fills a free platform destination with a core's file.
        for dest, status in file_status.items():
            full = f"{base_dest}/{dest}" if base_dest else dest
            if status == Status.MISSING and full in placed:
                file_status[dest] = Status.OK
                file_severity[dest] = Severity.OK
                for result in results_at.get(dest, []):
                    result["status"] = Status.OK

    counts = {
        Severity.OK: 0,
        Severity.INFO: 0,
        Severity.WARNING: 0,
        Severity.CRITICAL: 0,
    }
    for s in file_severity.values():
        counts[s] = counts.get(s, 0) + 1

    # Held but refused: on a hash platform the builder leaves out a file whose
    # declared hash the local copy contradicts. Counted apart, so a coverage
    # figure never calls present what the pack does not carry.
    excluded = sorted(
        dest for dest, status in file_status.items()
        if status == Status.UNTESTED and dest not in shipped_keys
    )

    # Count by file status (ok/untested/missing)
    status_counts: dict[str, int] = {}
    for s in file_status.values():
        status_counts[s] = status_counts.get(s, 0) + 1

    exclusions = find_exclusion_notes(
        config, emulators_dir, emu_profiles, target_cores=target_cores
    )

    # Ground truth coverage
    gt_filenames = set(validation_index)
    profiled_names: set[str] = set()
    for profile in profiles.values():
        if profile.get("type") in ("launcher", "alias"):
            continue
        for f in profile.get("files", []):
            name = f.get("name", "")
            if name:
                profiled_names.add(name)
            profiled_names.update(f.get("aliases") or [])
    dest_to_name: dict[str, str] = {}
    for sys_id, system in verify_systems.items():
        for fe in system.get("files", []):
            dest_to_name.setdefault(_dest_key(fe, sys_id), fe.get("name", ""))
    with_validation = sum(
        1 for dest in file_status if dest_to_name.get(dest, "") in gt_filenames
    )
    with_profile = sum(
        1 for dest in file_status if dest_to_name.get(dest, "") in profiled_names
    )
    total = len(file_status)

    return {
        "platform": platform,
        "verification_mode": mode,
        "total_files": total,
        "severity_counts": counts,
        "status_counts": status_counts,
        "excluded_files": excluded,
        "packed_extras": packed_extras,
        "undeclared_files": undeclared,
        "exclusion_notes": exclusions,
        "region_fallbacks": sorted(region_fallbacks),
        "details": details,
        "ground_truth_coverage": {
            "with_validation": with_validation,
            "with_profile": with_profile,
            "platform_only": total - with_validation,
            "total": total,
            "applicable": bool(resolve_platform_cores(config, profiles)),
        },
    }


# Output


def _format_ground_truth_aggregate(ground_truth: list[dict]) -> str:
    """Format ground truth as a single aggregated line.

    Example: beetle_psx [md5], pcsx_rearmed [existence]
    """
    parts = []
    for gt in ground_truth:
        checks_label = "+".join(gt["checks"]) if gt["checks"] else "existence"
        parts.append(f"{gt['emulator']} [{checks_label}]")
    return ", ".join(parts)


def _format_ground_truth_verbose(ground_truth: list[dict]) -> list[str]:
    """Format ground truth as one line per core with expected values and source ref.

    Example: handy validates size=512,crc32=0d973c9d [rom.h:48-49]
    """
    lines = []
    for gt in ground_truth:
        checks_label = "+".join(gt["checks"]) if gt["checks"] else "existence"
        expected = gt.get("expected", {})
        if expected:
            vals = ",".join(f"{k}={v}" for k, v in sorted(expected.items()))
            part = f"{gt['emulator']} validates {vals}"
        else:
            part = f"{gt['emulator']} validates {checks_label}"
        if gt.get("source_ref"):
            part += f" [{gt['source_ref']}]"
        lines.append(part)
    return lines


def _print_ground_truth(gt: list[dict], verbose: bool) -> None:
    """Print ground truth lines for a file entry."""
    if not gt:
        return
    if verbose:
        for line in _format_ground_truth_verbose(gt):
            print(f"    {line}")
    else:
        print(f"    {_format_ground_truth_aggregate(gt)}")


def _print_detail_entries(details: list[dict], seen: set[str], verbose: bool) -> None:
    """Print UNTESTED, MISSING, and DISCREPANCY entries from verification details."""
    for d in details:
        if d["status"] == Status.UNTESTED:
            if d.get("discrepancy"):
                # Reported below with the ground for it: a declaration
                # unmet at a path another declaration holds.
                continue
            key = f"{d['system']}/{d['name']}"
            if key in seen:
                continue
            seen.add(key)
            req = "required" if d.get("required", True) else "optional"
            hle = ", HLE available" if d.get("hle_fallback") else ""
            reason = d.get("reason", "")
            print(f"  UNTESTED ({req}{hle}): {key}: {reason}")
            _print_ground_truth(d.get("ground_truth", []), verbose)
    for d in details:
        if d["status"] == Status.MISSING:
            key = f"{d['system']}/{d['name']}"
            if key in seen:
                continue
            seen.add(key)
            req = "required" if d.get("required", True) else "optional"
            hle = ", HLE available" if d.get("hle_fallback") else ""
            print(f"  MISSING ({req}{hle}): {key}")
            _print_ground_truth(d.get("ground_truth", []), verbose)
    for d in details:
        disc = d.get("discrepancy")
        if disc:
            key = f"{d['system']}/{d['name']}"
            if key in seen:
                continue
            seen.add(key)
            print(f"  DISCREPANCY: {key}: {disc}")
            _print_ground_truth(d.get("ground_truth", []), verbose)

    if verbose:
        for d in details:
            if d["status"] == Status.OK:
                key = f"{d['system']}/{d['name']}"
                if key in seen:
                    continue
                seen.add(key)
                gt = d.get("ground_truth", [])
                if gt:
                    req = "required" if d.get("required", True) else "optional"
                    print(f"  OK ({req}): {key}")
                    for line in _format_ground_truth_verbose(gt):
                        print(f"    {line}")


def _print_undeclared_entry(u: dict, prefix: str, verbose: bool) -> None:
    """Print a single undeclared file entry with its validation checks."""
    arc_count = u.get("archive_file_count")
    if arc_count:
        name_label = f"{u['name']} ({arc_count} file{'s' if arc_count != 1 else ''})"
    else:
        name_label = u["name"]
    print(f"    {prefix}: {u['emulator']} needs {name_label}")
    checks = u.get("checks", [])
    if checks:
        if verbose:
            expected = u.get("expected", {})
            if expected:
                vals = ",".join(f"{k}={v}" for k, v in sorted(expected.items()))
                ref_part = f" [{u['source_ref']}]" if u.get("source_ref") else ""
                print(f"      validates {vals}{ref_part}")
            else:
                checks_label = "+".join(checks)
                ref_part = f" [{u['source_ref']}]" if u.get("source_ref") else ""
                print(f"      validates {checks_label}{ref_part}")
        else:
            print(f"      [{'+'.join(checks)}]")


def _split_undeclared(undeclared: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Game data, firmware the pack places, firmware it holds but cannot place.

    Everything that is not game data is firmware the core loads, archives
    included: a bios_zip sat in neither list, so a required one that was
    missing was never printed.
    """
    game_data = [u for u in undeclared if u.get("category", "bios") == "game_data"]
    firmware = [u for u in undeclared if u.get("category", "bios") != "game_data"]
    unplaced = [u for u in firmware if u["in_repo"] and not u.get("in_pack", True)]
    placed = [u for u in firmware if u.get("in_pack", True)]
    return game_data, placed, unplaced


def _core_required_missing(bios_files: list[dict]) -> list[dict]:
    """Core files a core cannot run without, absent from the collection.

    A file bundled with the emulator (DaedalusX64's n64.psh, the shader
    library its renderer installs beside the binary) arrives with it: the
    collection does not hold it by design, and listing it as missing sent
    readers after a file every install already has.
    """
    return [
        u
        for u in bios_files
        if u["required"] and not u["in_repo"] and not u.get("hle_fallback")
        and not u.get("bundled")
    ]


def _print_undeclared_section(result: dict, verbose: bool) -> None:
    """Print cross-reference section for undeclared files used by cores."""
    undeclared = result.get("undeclared_files", [])
    if not undeclared:
        return

    game_data, bios_files, unplaced = _split_undeclared(undeclared)

    req_not_in_repo = _core_required_missing(bios_files)
    req_hle_not_in_repo = [
        u
        for u in bios_files
        if u["required"] and not u["in_repo"] and u.get("hle_fallback")
    ]
    req_in_repo = [u for u in bios_files if u["required"] and u["in_repo"]]
    opt_in_repo = [u for u in bios_files if not u["required"] and u["in_repo"]]
    opt_not_in_repo = [u for u in bios_files if not u["required"] and not u["in_repo"]]

    bundled = [
        u for u in bios_files
        if u["required"] and not u["in_repo"] and u.get("bundled")
        and not u.get("hle_fallback")
    ]
    # The builder's count, one file per pack path, when the platform pass
    # replayed it. Counting entries said 1130 for RetroArch, 48 more than the
    # pack holds: rom.key alone was claimed by three Amiga cores.
    packed = result.get("packed_extras")
    core_in_pack = (
        packed["bios"] if packed is not None else len(req_in_repo) + len(opt_in_repo)
    )
    core_missing_req = len(req_not_in_repo) + len(req_hle_not_in_repo)
    core_missing_opt = len(opt_not_in_repo)

    print(
        f"  Core files: {core_in_pack} in pack, {core_missing_req} required missing, {core_missing_opt} optional missing"
    )
    if bundled:
        names = ", ".join(sorted({u["name"] for u in bundled}))
        print(f"  Bundled with their emulator, not collected: {names}")
    if unplaced:
        print(f"  Core files held but not packed: {len(unplaced)} ({unplaced[0]['omitted']})")

    for u in req_not_in_repo:
        _print_undeclared_entry(u, "MISSING (required)", verbose)
    for u in req_hle_not_in_repo:
        _print_undeclared_entry(u, "MISSING (required, HLE fallback)", verbose)

    if game_data:
        gd_missing = [u for u in game_data if not u["in_repo"]]
        gd_packed = (
            packed["game_data"] if packed is not None
            else sum(1 for u in game_data if u["in_repo"] and u.get("in_pack", True))
        )
        gd_unplaced = [u for u in game_data if u["in_repo"] and not u.get("in_pack", True)]
        if gd_missing or gd_packed or gd_unplaced:
            print(f"  Game data: {gd_packed} in pack, {len(gd_missing)} missing")
        if gd_unplaced:
            print(
                f"  Game data held but not packed: {len(gd_unplaced)} "
                f"({gd_unplaced[0].get('omitted', 'not placed')})"
            )


def print_platform_result(
    result: dict, group: list[str], verbose: bool = False
) -> None:
    mode = result["verification_mode"]
    total = result["total_files"]
    c = result["severity_counts"]
    label = " / ".join(group)
    ok_count = c[Severity.OK]
    problems = total - ok_count

    # Summary line
    if not reads_file_contents(mode):
        if problems:
            missing = c.get(Severity.WARNING, 0) + c.get(Severity.CRITICAL, 0)
            optional_missing = c.get(Severity.INFO, 0)
            parts = [f"{ok_count}/{total} present"]
            if missing:
                parts.append(f"{missing} missing")
            if optional_missing:
                parts.append(f"{optional_missing} optional missing")
        else:
            parts = [f"{ok_count}/{total} present"]
    else:
        sc = result.get("status_counts", {})
        untested = sc.get(Status.UNTESTED, 0)
        missing = sc.get(Status.MISSING, 0)
        parts = [f"{ok_count}/{total} OK"]
        if untested:
            parts.append(f"{untested} untested")
        if missing:
            parts.append(f"{missing} missing")
    print(f"{label}: {', '.join(parts)} [{mode}]")
    fallbacks = result.get("region_fallbacks") or []
    if fallbacks:
        from packreadme import _group_label

        print(f"  No BIOS for the requested regions, all kept ({len(fallbacks)}): "
              + ", ".join(_group_label(g) for g in fallbacks))

    seen_details: set[str] = set()
    _print_detail_entries(result["details"], seen_details, verbose)
    _print_undeclared_section(result, verbose)

    # The same notes come back for every platform running these cores and
    # filled 985 of the 1114 lines of verify --all: counted, listed on ask.
    exclusions = result.get("exclusion_notes", [])
    if exclusions and verbose:
        print(f"  No external files ({len(exclusions)}):")
        for ex in exclusions:
            print(f"    {ex['emulator']}: {ex['detail']} [{ex['reason']}]")
    elif exclusions:
        print(f"  No external files: {len(exclusions)} cores (--verbose lists them)")

    gt_cov = result.get("ground_truth_coverage")
    if gt_cov and not gt_cov.get("applicable", True):
        # MiSTer declares no core by design: its BiosDB is the answer, and
        # the site shows "-" for it rather than a 0% gap.
        print("  Ground truth: not applicable, the platform runs no profiled core")
    elif gt_cov and gt_cov["total"] > 0:
        # Rounded, as the site rounds: truncated, RetroArch read 20% here
        # and 21% there for one measure.
        pct = round(gt_cov["with_validation"] * 100 / gt_cov["total"])
        print(
            f"  Ground truth: {gt_cov['with_validation']}/{gt_cov['total']} files have emulator validation ({pct}%)"
        )
        no_profile = gt_cov["total"] - gt_cov.get("with_profile", gt_cov["with_validation"])
        if no_profile:
            print(f"    {no_profile} platform-only (no emulator profile)")


# Emulator/system mode verification


def _effective_validation_label(details: list[dict], validation_index: dict) -> str:
    """Determine the bracket label for the report.

    Returns the union of all check types used, e.g. [crc32+existence+size].
    """
    all_checks: set[str] = set()
    has_files = False
    for d in details:
        fname = d.get("name", "")
        if d.get("note"):
            continue  # skip informational entries (empty profiles)
        has_files = True
        entry = validation_index.get(fname)
        if entry:
            all_checks.update(entry["checks"])
        else:
            all_checks.add("existence")
    if not has_files:
        return "existence"
    return "+".join(sorted(all_checks))


def _select_profiles(
    profile_names: list[str],
    all_profiles: dict,
    standalone: bool,
) -> list[tuple[str, dict]]:
    """Resolve the named profiles, stopping the run on a bad name."""
    try:
        return select_emulator_profiles(profile_names, all_profiles, standalone)
    except ProfileSelectionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

def verify_emulator(
    profile_names: list[str],
    emulators_dir: str,
    db: dict,
    standalone: bool = False,
    regions: list[str] | None = None,
    platforms_dir: str = _REPO_PLATFORMS,
    system_ids: list[str] | None = None,
) -> dict:
    """Verify files for specific emulator profiles.

    A region priority list narrows the report the same way a pack built with the
    same list would be narrowed, through the emulator pack's own drop set.
    `system_ids` keeps the entries of those systems, as the system pack does.
    """
    from packextras import expand_agnostic_entries
    from packpaths import (
        _archive_destination,
        _register_path,
        _resolve_destination,
        placement_block,
    )

    # Also load aliases for redirect messages
    all_profiles = load_emulator_profiles(emulators_dir, skip_aliases=False)

    selected = _select_profiles(profile_names, all_profiles, standalone)
    # The member index only answers a zipped_file entry, and decompressing
    # every archive took 23 of the 24 seconds of a run that had none.
    zip_contents = (
        build_zip_contents_index(db)
        if any(f.get("zipped_file") for _name, profile in selected
               for f in profile.get("files") or [])
        else {}
    )

    # Build validation index from selected profiles only
    selected_profiles = {n: p for n, p in selected}
    validation_index = _build_validation_index(selected_profiles)
    data_registry = load_data_dir_registry(platforms_dir)

    details = []
    file_status: dict[str, str] = {}
    file_severity: dict[str, str] = {}
    dest_to_name: dict[str, str] = {}
    data_dir_notices: list[str] = []

    # The emulator pack withdraws these, and the report withdraws the same.
    region_drops: set[str] = set()
    if regions:
        from packextras import emulator_region_drops

        region_drops = emulator_region_drops(selected, standalone, regions, db=db)

    # The emulator pack walks profiles in name order and keeps the first
    # file at a path, folding case: a later profile's different file there
    # (zesarux pentagon.rom after speccy's PENTAGON.ROM) is not in the pack.
    sha1_by_path = {
        entry.get("path"): sha1 for sha1, entry in db.get("files", {}).items()
    }
    # Destination -> (file packed there, its profile, the path as written).
    # Archives and data trees take paths too, before and after the loose
    # files of their profile, as the pack writes them.
    placed: dict[str, tuple[str | None, str, str]] = {}
    placed_files: set[str] = set()
    placed_parents: set[str] = set()
    content_ids: dict[str, str | None] = {}

    def content_id(path: str | None) -> str | None:
        if not path:
            return None
        if path not in content_ids:
            content_ids[path] = sha1_by_path.get(path) or (
                compute_hashes(path)["sha1"] if os.path.isfile(path) else None
            )
        return content_ids[path]

    def occupy(dest: str, path: str | None, owner: str) -> None:
        if placement_block(dest, placed_files, set(placed), placed_parents, True):
            return
        placed[dest.lower()] = (path, owner, dest)
        _register_path(dest, placed_files, placed_parents)
    for emu_name, profile in sorted(selected):
        files = expand_agnostic_entries(
            expand_directory_entries(
                deliverable(profile.get("files", []), standalone),
                db,
                standalone,
            ),
            emu_name, profile, db, standalone, all_profiles,
        )
        if system_ids:
            files = [fe for fe in files if entry_serves_systems(fe, profile, system_ids)]
        if region_drops:
            # Loose files only, as the emulator pack: an archive ships whole,
            # and dropping kronos' regional stvbios.zip members rebuilt the
            # archive here from 4 of its 14 members and changed its required.
            files = [
                fe
                for fe in files
                if fe.get("archive")
                or _resolve_destination(fe, standalone) not in region_drops
            ]

        # Check data directories (only notice if not cached)
        for dd in deliverable(
            profile.get("data_directories", []), standalone
        ):
            ref = dd.get("ref", "")
            if not ref:
                continue
            if data_registry and ref in data_registry:
                cache_path = data_registry[ref].get("local_cache", "")
                if cache_path and os.path.isdir(cache_path):
                    continue  # cached, no notice needed
            data_dir_notices.append(ref)

        if not files:
            details.append(
                {
                    "name": f"({emu_name})",
                    "status": Status.OK,
                    "required": False,
                    "system": "",
                    "note": f"No files needed for {profile.get('emulator', emu_name)}",
                    "ground_truth": [],
                }
            )
            continue

        # Verify archives as units (e.g., neogeo.zip, aes.zip)
        seen_archives: set[str] = set()
        for file_entry in files:
            archive = file_entry.get("archive")
            if archive and archive not in seen_archives:
                seen_archives.add(archive)
                local_path, resolve_status = resolve_local_file(
                    archive_entry(archive, files),
                    db,
                    zip_contents,
                    data_dir_registry=data_registry,
                )
                required = any(
                    f.get("archive") == archive and f.get("required", True)
                    for f in files
                )
                if local_path and resolve_status == "hash_mismatch":
                    # Same policy as the loose-file branch below: the name
                    # matched while the bytes contradict the hash the profile
                    # declares on the container. Reporting it covered says the
                    # collection holds an archive it does not.
                    result = {
                        "name": archive,
                        "status": Status.UNTESTED,
                        "required": required,
                        "path": local_path,
                        "reason": "declared hash contradicted by the local file",
                    }
                elif local_path:
                    result = {
                        "name": archive,
                        "status": Status.OK,
                        "required": required,
                        "path": local_path,
                    }
                else:
                    result = {
                        "name": archive,
                        "status": Status.MISSING,
                        "required": required,
                    }
                result["system"] = file_entry.get("system", "")
                result["hle_fallback"] = False
                result["ground_truth"] = build_ground_truth(archive, validation_index)
                details.append(result)
                # Where the pack puts it: an archive_prefix or pack_structure
                # moves it, and a loose same-named entry is another file.
                dest = _archive_destination(
                    archive, profile.get("archive_prefix", ""),
                    profile.get("pack_structure"), standalone,
                )
                result["destination"] = dest
                block = placement_block(
                    dest, placed_files, set(placed), placed_parents, True
                )
                if block and local_path and result["status"] != Status.MISSING:
                    other_path, owner, other_dest = placed.get(dest.lower(), (None, "", ""))
                    if block == "path_conflict" or content_id(local_path) != content_id(other_path):
                        # The pack serves the other profile's file here: this
                        # archive is not in it, whatever this profile resolves.
                        result["status"] = Status.MISSING
                        result["reason"] = (
                            "a packed file occupies its directory" if block == "path_conflict"
                            else f"the pack holds {owner}'s {other_dest} at this path"
                        )
                elif local_path:
                    occupy(dest, local_path, emu_name)
                dest_to_name[dest] = archive
                cur = result["status"]
                prev = file_status.get(dest)
                if prev is None or _STATUS_ORDER.get(cur, 0) > _STATUS_ORDER.get(
                    prev, 0
                ):
                    file_status[dest] = cur
                sev = _emulator_severity(cur, required, False)
                prev_sev = file_severity.get(dest)
                if prev_sev is None or _SEVERITY_ORDER.get(
                    sev, 0
                ) > _SEVERITY_ORDER.get(prev_sev, 0):
                    file_severity[dest] = sev

        # Results of this emulator, by destination. Several entries of one
        # profile on one destination are the releases the code accepts
        # there (a shareware and a retail HOG, each GRP in a size table):
        # holding one of them fills the slot, and the others are not gaps.
        emu_results: dict[str, list[tuple[dict, bool, bool]]] = {}

        for file_entry in files:
            # Skip archived files (verified as archive units above)
            if file_entry.get("archive"):
                continue

            dest_hint = file_entry.get("path", "")
            local_path, resolve_status = resolve_local_file(
                {**file_entry, "source_profile": emu_name},
                db,
                zip_contents,
                dest_hint=dest_hint,
                data_dir_registry=data_registry,
            )
            name = file_entry.get("name", "")
            required = file_entry.get("required", True)
            hle = file_entry.get("hle_fallback", False)

            stored = _storage_verdict(file_entry, emulator_pack=True)
            if stored is not None:
                result = stored
            elif not local_path:
                result = {"name": name, "status": Status.MISSING, "required": required}
                # An entry the profile documents as unsourceable is absent by
                # design: a per-user key, a slot the user fills, a dump nobody
                # has made. Counting it beside a file somebody could still
                # find invites the wrong repair -- dropping the flag, or
                # chasing a vendor's whole install tree.
                unsourceable = file_entry.get("unsourceable")
                if unsourceable:
                    result["unsourceable"] = (
                        unsourceable if isinstance(unsourceable, str) else ""
                    )
            else:
                # Apply emulator validation
                # Judged by this emulator's own entries, as it runs its check,
                # through the selection the emulator pack makes.
                chosen, disagreement = validated_choice(
                    file_entry, local_path, db, validation_index, DEFAULT_BIOS_DIR,
                    None, emulators={emu_name},
                )
                if disagreement:
                    result = {
                        "name": name,
                        "status": Status.UNTESTED,
                        "required": required,
                        "path": local_path,
                        "reason": disagreement.split(" says ", 1)[-1],
                    }
                elif chosen != local_path:
                    result = {
                        "name": name,
                        "status": Status.OK,
                        "required": required,
                        "path": chosen,
                    }
                elif resolve_status == "hash_mismatch":
                    # Nothing in validation: caught it, but the name matched
                    # while the bytes contradict the hash the profile
                    # declares. That is a different file wearing the right
                    # name -- config.ini and ROM collide across systems -- so
                    # calling it covered reports the collection as holding
                    # something it does not. Validation runs first because it
                    # names the specific field that disagrees.
                    result = {
                        "name": name,
                        "status": Status.UNTESTED,
                        "required": required,
                        "path": local_path,
                        "reason": "declared hash contradicted by the local file",
                    }
                else:
                    result = {
                        "name": name,
                        "status": Status.OK,
                        "required": required,
                        "path": local_path,
                    }

            result["system"] = file_entry.get("system", "")
            result["hle_fallback"] = hle
            result["ground_truth"] = build_ground_truth(name, validation_index)
            # The slot the emulator pack places this entry in: standalone_path
            # under --standalone, the sanitised path.
            dest = _resolve_destination(file_entry, standalone) or name
            emu_results.setdefault(dest, []).append((result, required, hle))

        for dest, alternatives in emu_results.items():
            # What the emulator pack places here: an accepted file, else a
            # file whose hash the profile contradicts. A refused copy is held
            # but never packed.
            fallback_reason = "declared hash contradicted by the local file"
            if len(alternatives) > 1:
                # Reduced in the order generate_emulator_pack chooses, so the
                # destination's status is the one the pack reports: a slot it
                # fills with a contradicted copy, or counts as refused by the
                # core, is not missing because another alternative is absent.
                for keep in (
                    lambda a: a[0]["status"] == Status.OK,
                    lambda a: a[0]["status"] == Status.UNTESTED
                    and a[0].get("reason") == fallback_reason,
                    lambda a: a[0]["status"] != Status.MISSING,
                ):
                    chosen = [a for a in alternatives if keep(a)]
                    if chosen:
                        alternatives = chosen[:1]
                        break
            packed = next(
                (a[0].get("path") for a in alternatives
                 if a[0]["status"] == Status.OK and a[0].get("path")),
                None,
            ) or next(
                (a[0].get("path") for a in alternatives
                 if a[0]["status"] == Status.UNTESTED
                 and a[0].get("reason") == fallback_reason and a[0].get("path")),
                None,
            )
            held = packed or next(
                (a[0].get("path") for a in alternatives
                 if a[0]["status"] != Status.MISSING and a[0].get("path")),
                None,
            )
            block = placement_block(
                dest, placed_files, set(placed), placed_parents, True
            )
            block_reason = ""
            if block and held is not None:
                other_path, owner, other_dest = placed.get(dest.lower(), (None, "", ""))
                same = content_id(held) is not None and content_id(held) == content_id(
                    other_path
                )
                if block == "path_conflict":
                    block_reason = "a packed file occupies its directory"
                elif block == "case_twin":
                    block_reason = (
                        f"packed only as {other_dest}, from {owner}"
                        if same
                        else f"the pack holds {owner}'s {other_dest} at this path"
                    )
                elif not same:
                    block_reason = f"the pack holds {owner}'s {other_dest} at this path"
            elif not block and packed is not None:
                occupy(dest, packed, emu_name)
            for result, required, hle in alternatives:
                if block_reason and result["status"] != Status.MISSING:
                    result["status"] = Status.MISSING
                    result["reason"] = block_reason
                result["destination"] = dest
                details.append(result)

                # Aggregate by destination (path if available, else name)
                dest_to_name[dest] = result["name"]
                cur = result["status"]
                prev = file_status.get(dest)
                if prev is None or _STATUS_ORDER.get(cur, 0) > _STATUS_ORDER.get(
                    prev, 0
                ):
                    file_status[dest] = cur
                sev = _emulator_severity(cur, required, hle)
                prev_sev = file_severity.get(dest)
                if prev_sev is None or _SEVERITY_ORDER.get(
                    sev, 0
                ) > _SEVERITY_ORDER.get(prev_sev, 0):
                    file_severity[dest] = sev

        # The profile's data trees follow its files in the pack.
        for dd in deliverable(profile.get("data_directories", []), standalone):
            cache = (data_registry or {}).get(dd.get("ref", ""), {}).get("local_cache", "")
            if not cache or not os.path.isdir(cache):
                continue
            dd_dest = dd.get("destination", "")
            structure = profile.get("pack_structure")
            prefix = (
                structure.get("standalone" if standalone else "libretro", "")
                if structure else ""
            )
            if prefix:
                dd_dest = f"{prefix}/{dd_dest}" if dd_dest else prefix
            for root, _dirs, names in os.walk(cache):
                for fname in names:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, cache).replace(os.sep, "/")
                    occupy(f"{dd_dest}/{rel}" if dd_dest else rel, src, emu_name)

    counts = {
        Severity.OK: 0,
        Severity.INFO: 0,
        Severity.WARNING: 0,
        Severity.CRITICAL: 0,
    }
    for s in file_severity.values():
        counts[s] = counts.get(s, 0) + 1
    status_counts: dict[str, int] = {}
    for s in file_status.values():
        status_counts[s] = status_counts.get(s, 0) + 1

    label = _effective_validation_label(details, validation_index)

    gt_filenames = set(validation_index)
    total = len(file_status)
    with_validation = sum(
        1 for dest in file_status if dest_to_name.get(dest, "") in gt_filenames
    )

    return {
        "emulators": [n for n, _ in selected],
        "verification_mode": label,
        "total_files": total,
        "severity_counts": counts,
        "status_counts": status_counts,
        "details": details,
        "data_dir_notices": sorted(set(data_dir_notices)),
        "ground_truth_coverage": {
            "with_validation": with_validation,
            "platform_only": total - with_validation,
            "total": total,
        },
    }


def verify_system(
    system_ids: list[str],
    emulators_dir: str,
    db: dict,
    standalone: bool = False,
    regions: list[str] | None = None,
    platforms_dir: str = _REPO_PLATFORMS,
) -> dict:
    """Verify files for all emulators supporting given system IDs."""
    profiles = load_emulator_profiles(emulators_dir)
    matching = profiles_for_systems(profiles, system_ids, standalone)

    if not matching:
        all_systems: set[str] = set()
        for p in profiles.values():
            all_systems.update(p.get("systems", []))
        if standalone:
            print(
                f"No standalone emulators found for system(s): {', '.join(system_ids)}",
                file=sys.stderr,
            )
        else:
            print(
                f"No emulators found for system(s): {', '.join(system_ids)}",
                file=sys.stderr,
            )
        print(suggest_names(system_ids, sorted(all_systems), "--list-systems"),
              file=sys.stderr)
        sys.exit(1)

    return verify_emulator(
        matching, emulators_dir, db, standalone, regions=regions,
        platforms_dir=platforms_dir, system_ids=system_ids,
    )


def print_emulator_result(result: dict, verbose: bool = False) -> None:
    """Print verification result for emulator/system mode."""
    label = " + ".join(result["emulators"])
    mode = result["verification_mode"]
    total = result["total_files"]
    c = result["severity_counts"]
    ok_count = c[Severity.OK]

    sc = result.get("status_counts", {})
    untested = sc.get(Status.UNTESTED, 0)
    missing = sc.get(Status.MISSING, 0)
    # Counted by slot, as missing is: xash3d's thirteen unsourceable
    # pak0.pak and liblist.gam were three names and ten "missing".
    missing_slots: dict[str, bool] = {}
    for d in result["details"]:
        if d["status"] == Status.MISSING:
            slot = d.get("destination") or d["name"]
            missing_slots[slot] = missing_slots.get(slot, True) and "unsourceable" in d
    unsourceable = sum(missing_slots.values())
    parts = [f"{ok_count}/{total} OK"]
    if untested:
        parts.append(f"{untested} untested")
    if missing - unsourceable > 0:
        parts.append(f"{missing - unsourceable} missing")
    if unsourceable:
        parts.append(f"{unsourceable} unsourceable")
    print(f"{label}: {', '.join(parts)} [{mode}]")

    seen = set()
    for d in result["details"]:
        if d["status"] == Status.UNTESTED:
            if d["name"] in seen:
                continue
            seen.add(d["name"])
            req = "required" if d.get("required", True) else "optional"
            hle = ", HLE available" if d.get("hle_fallback") else ""
            reason = d.get("reason", "")
            print(f"  UNTESTED ({req}{hle}): {d['name']}: {reason}")
            gt = d.get("ground_truth", [])
            if gt:
                if verbose:
                    for line in _format_ground_truth_verbose(gt):
                        print(f"    {line}")
                else:
                    print(f"    {_format_ground_truth_aggregate(gt)}")
    for d in result["details"]:
        if d["status"] == Status.MISSING and "unsourceable" not in d:
            if d["name"] in seen:
                continue
            seen.add(d["name"])
            req = "required" if d.get("required", True) else "optional"
            hle = ", HLE available" if d.get("hle_fallback") else ""
            print(f"  MISSING ({req}{hle}): {d['name']}")
            gt = d.get("ground_truth", [])
            if gt:
                if verbose:
                    for line in _format_ground_truth_verbose(gt):
                        print(f"    {line}")
                else:
                    print(f"    {_format_ground_truth_aggregate(gt)}")
    for d in result["details"]:
        if d["status"] == Status.MISSING and "unsourceable" in d:
            if d["name"] in seen:
                continue
            seen.add(d["name"])
            why = d.get("unsourceable") or "documented as unobtainable"
            print(f"  UNSOURCEABLE: {d['name']}: {why}")

    for d in result["details"]:
        if d.get("note"):
            print(f"  {d['note']}")

    if verbose:
        for d in result["details"]:
            if d["status"] == Status.OK:
                if d["name"] in seen:
                    continue
                seen.add(d["name"])
                gt = d.get("ground_truth", [])
                if gt:
                    req = "required" if d.get("required", True) else "optional"
                    print(f"  OK ({req}): {d['name']}")
                    for line in _format_ground_truth_verbose(gt):
                        print(f"    {line}")

    for ref in result.get("data_dir_notices", []):
        print(
            f"  Note: data directory '{ref}' required but not included (use refresh_data_dirs.py)"
        )

    # Ground truth coverage footer
    gt_cov = result.get("ground_truth_coverage")
    if gt_cov and gt_cov["total"] > 0:
        pct = round(gt_cov["with_validation"] * 100 / gt_cov["total"])
        print(
            f"  Ground truth: {gt_cov['with_validation']}/{gt_cov['total']} files have emulator validation ({pct}%)"
        )
        if gt_cov["platform_only"]:
            print(f"    {gt_cov['platform_only']} without declared validation")


def _refuse_listing_narrowings(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    """A listing mode reads --platform at most, anything else is refused.

    Printing past a narrowing flag lets the user believe it applied
    (generate_pack applies the same table).
    """
    listing = next(
        (flag for flag, on in (
            ("--list-emulators", args.list_emulators),
            ("--list-systems", args.list_systems),
            ("--list-targets", args.list_targets),
        ) if on),
        None,
    )
    if not listing:
        return
    reads_platform = listing != "--list-emulators"
    for flag, on in (
        ("--platform", args.platform and not reads_platform),
        ("--all", args.all),
        ("--emulator", args.emulator),
        ("--system", args.system),
        ("--region", getattr(args, "region", None)),
        ("--target", getattr(args, "target", None)),
        ("--standalone", args.standalone),
        ("--include-archived", args.include_archived),
        ("--json", args.json),
        ("--strict", args.strict),
        ("--verbose", args.verbose),
    ):
        if on:
            parser.error(f"{flag} is incompatible with {listing}")


def _refuse_mode_flags(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """A flag the chosen mode does not read is refused, never ignored."""
    for refused, message in (
        (args.standalone and not (args.emulator or args.system),
         "--standalone requires --emulator or --system"),
        (args.include_archived and not args.all, "--include-archived requires --all"),
        (args.target and not (args.platform or args.all),
         "--target requires --platform or --all"),
        (args.target and (args.emulator or args.system),
         "--target is incompatible with --emulator and --system"),
    ):
        if refused:
            parser.error(message)


def _run_listing(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.list_emulators:
        list_emulator_profiles(args.emulators_dir)
    elif args.list_systems and args.platform:
        list_platform_system_ids(args.platform, args.platforms_dir)
    elif args.list_systems:
        list_system_ids(args.emulators_dir)
    else:
        if not args.platform:
            parser.error("--list-targets requires --platform")
        targets = list_available_targets(args.platform, args.platforms_dir)
        if not targets:
            print(f"No targets configured for platform '{args.platform}'")
            return
        for t in targets:
            print(target_listing_line(t))


def main() -> None:
    with contextlib.ExitStack() as held:
        _main(held)


def _main(held: contextlib.ExitStack) -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Platform-native BIOS verification")
    parser.add_argument("--platform", "-p", type=platform_arg, help="Platform name")
    parser.add_argument(
        "--all", action="store_true", help="Verify all active platforms"
    )
    parser.add_argument(
        "--emulator", "-e", help="Emulator profile name(s), comma-separated"
    )
    parser.add_argument("--system", "-s", help="System ID(s), comma-separated")
    parser.add_argument("--standalone", action="store_true", help="Use standalone mode")
    parser.add_argument(
        "--list-emulators", action="store_true", help="List available emulators"
    )
    parser.add_argument(
        "--list-systems", action="store_true", help="List available systems"
    )
    parser.add_argument(
        "--include-archived", action="store_true",
        help="With --all, also verify the archived platforms (RetroPie)",
    )
    parser.add_argument("--target", "-t", help="Hardware target (e.g., switch, rpi4)")
    parser.add_argument(
        "--region", help="Region priority list, best first (e.g. us,eu,jp)"
    )
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="List available targets for the platform",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help="database file to read (default: %(default)s)",
    )
    parser.add_argument(
        "--platforms-dir", default=DEFAULT_PLATFORMS_DIR,
        help="platform YAML directory (default: %(default)s)",
    )
    parser.add_argument(
        "--emulators-dir", default=DEFAULT_EMULATORS_DIR,
        help="emulator profile directory (default: %(default)s)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show emulator ground truth details",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on a required file missing: where the frontend checks "
        "hashes for a platform, without HLE for --emulator and --system. A file "
        "present with an unconfirmed hash is a warning, not a failure",
    )
    args = parser.parse_args()
    if args.platform:
        require_platform(args.platform, args.platforms_dir)

    requested_regions: list[str] = []
    if getattr(args, "region", None):
        import region as region_mod

        try:
            requested_regions = region_mod.parse_requested(args.region)
        except ValueError as exc:
            parser.error(str(exc))

    _refuse_listing_narrowings(args, parser)
    if args.list_emulators or args.list_systems or args.list_targets:
        _run_listing(args, parser)
        return

    # Mutual exclusion
    modes = sum(1 for x in (args.platform, args.all, args.emulator, args.system) if x)
    if modes == 0:
        parser.error("Specify --platform, --all, --emulator, or --system")
    if modes > 1:
        parser.error(
            "--platform, --all, --emulator, and --system are mutually exclusive"
        )
    _refuse_mode_flags(args, parser)
    # The trees stay put while names resolve against them and files are read.
    from refresh_data_dirs import hold_data_dirs

    hold_data_dirs(held, load_data_dir_registry(args.platforms_dir))

    db = load_database(args.db)

    if args.emulator:
        names = [n.strip() for n in args.emulator.split(",") if n.strip()]
        result = verify_emulator(
            names, args.emulators_dir, db, args.standalone,
            regions=requested_regions, platforms_dir=args.platforms_dir,
        )
        if args.json:
            result["details"] = _json_details(result["details"], args.verbose)
            print(json.dumps(result, indent=2))
        else:
            print_emulator_result(result, verbose=args.verbose)
        _exit_if_strict_emulator(args.strict, result)
        return

    if args.system:
        system_ids = [s.strip() for s in args.system.split(",") if s.strip()]
        result = verify_system(
            system_ids, args.emulators_dir, db, args.standalone,
            regions=requested_regions, platforms_dir=args.platforms_dir,
        )
        if args.json:
            result["details"] = _json_details(result["details"], args.verbose)
            print(json.dumps(result, indent=2))
        else:
            print_emulator_result(result, verbose=args.verbose)
        _exit_if_strict_emulator(args.strict, result)
        return

    # Platform mode (existing)
    if args.all:
        platforms = list_registered_platforms(
            args.platforms_dir, include_archived=args.include_archived
        )
    elif args.platform:
        platforms = [args.platform]
    else:
        parser.error("Specify --platform or --all")
        return

    # Load emulator profiles once for cross-reference (not per-platform)
    emu_profiles = load_emulator_profiles(args.emulators_dir)
    data_registry = load_data_dir_registry(args.platforms_dir)

    target_cores_cache: dict[str, set[str] | None] = {}
    if args.target:
        try:
            target_cores_cache, platforms = build_target_cores_cache(
                platforms,
                args.target,
                args.platforms_dir,
                is_all=args.all,
            )
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # Group identical platforms (same function as generate_pack)
    groups = group_identical_platforms(
        platforms, args.platforms_dir, target_cores_cache if args.target else None
    )

    all_results = {}
    group_results: list[tuple[dict, list[str]]] = []
    for group_platforms, representative in groups:
        config = load_platform_config(representative, args.platforms_dir)
        tc = target_cores_cache.get(representative) if args.target else None
        result = verify_platform(
            config,
            db,
            args.emulators_dir,
            emu_profiles,
            target_cores=tc,
            data_dir_registry=data_registry,
            regions=requested_regions,
        )
        names = [
            load_platform_config(p, args.platforms_dir).get("platform", p)
            for p in group_platforms
        ]
        group_results.append((result, names))
        for p in group_platforms:
            all_results[p] = result

    if not args.json:
        for result, group in group_results:
            print_platform_result(result, group, verbose=args.verbose)
            print()
        if len(group_results) > 1:
            print(_overall_summary(group_results))

    if args.json:
        for r in all_results.values():
            r["details"] = _json_details(r["details"], args.verbose)
        print(json.dumps(all_results, indent=2))

    _exit_if_strict(args.strict, [r for r, _ in group_results])


def _json_details(details: list[dict], verbose: bool) -> list[dict]:
    """The entries a JSON report lists: what needs attention, or every one.

    --verbose shows each entry with its ground truth in the text report. It
    used to be accepted beside --json and dropped, the output byte for byte
    the same, so a script asking for the passing entries got nothing.
    """
    return list(details) if verbose else [d for d in details if d["status"] != Status.OK]


def _exit_if_strict(strict: bool, results: list[dict]) -> None:
    """Exit 1 under --strict when a platform result counts a critical file:
    a required file missing where the frontend reads hashes.

    A file present whose hash the frontend does not confirm is UNTESTED, a
    warning (Batocera's BiosStatus calls it so): reported, not failed.
    """
    critical = sum(r["severity_counts"].get(Severity.CRITICAL, 0) for r in results)
    if strict and critical:
        print(f"ERROR: {critical} required file(s) missing where the frontend "
              "checks hashes", file=sys.stderr)
        sys.exit(1)


def _exit_if_strict_emulator(strict: bool, result: dict) -> None:
    """Exit 1 under --strict when an emulator lacks a file it cannot run without.

    The emulator, not a frontend, is the judge here: a required file with no
    HLE fallback that the collection does not hold stops the core.
    """
    missing = sorted({
        d["name"] for d in result["details"]
        if d["status"] == Status.MISSING and d.get("required", True)
        and not d.get("hle_fallback")
    })
    if strict and missing:
        print(f"ERROR: {len(missing)} required file(s) missing: "
              f"{', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def _overall_summary(group_results: list[tuple[dict, list[str]]]) -> str:
    """The closing lines of a multi-platform run, worst severity first.

    Each count names the platforms carrying it, and the core files a core
    cannot run without are counted too: a reader of the summary alone used
    to conclude nothing required was missing, and could not tell where the
    one warning was without reading the whole output.
    """
    platforms = sum(len(group) for _, group in group_results)

    def where(counts: dict[str, int]) -> str:
        named = [f"{name} {count}" if count > 1 else name
                 for name, count in counts.items() if count]
        return f" ({', '.join(named)})" if named else ""

    parts = []
    for severity, label in ((Severity.CRITICAL, "critical"), (Severity.WARNING, "warning"),
                            (Severity.INFO, "info")):
        counts = {
            " / ".join(group): r["severity_counts"].get(severity, 0)
            for r, group in group_results
        }
        parts.append(f"{sum(counts.values())} {label}{where(counts)}")
    lines = [f"{platforms} platforms: {', '.join(parts)}"]

    missing = {
        " / ".join(group): sorted({
            u["name"] for u in _core_required_missing(_split_undeclared(
                r.get("undeclared_files", []))[1])
        })
        for r, group in group_results
    }
    total = sum(len(names) for names in missing.values())
    if total:
        detail = "; ".join(f"{group}: {', '.join(names)}"
                           for group, names in missing.items() if names)
        lines.append(f"{total} core file(s) a core cannot run without are missing ({detail})")
    return "\n".join(lines)


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
