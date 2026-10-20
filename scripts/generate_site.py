#!/usr/bin/env python3
"""Generate MkDocs site pages from database.json, platform configs, and emulator profiles.

Reads the same data sources as verify.py and generate_pack.py to produce
a complete documentation site. Zero manual content.

Usage:
    python scripts/generate_site.py
    python scripts/generate_site.py --db database.json --platforms-dir platforms
"""

from __future__ import annotations

from siterender import WIKI_SRC_DIR
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    build_zip_contents_index,
    compute_composition,
    count_catalog_matched,
    list_registered_platforms,
    load_database,
    load_emulator_profiles,
    load_provenance_snapshots,
    native_layer,
    require_yaml,
    resolution_is_hash_exact,
    resolve_local_file,
    resolve_platform_cores,
    unique_emulator_profiles,
    yaml_load,
)
from artifacts import ArtifactLockBusy, artifact_lock
from cross_reference import FileConsumers
from nativemode import reads_file_contents






yaml = require_yaml()
from generate_readme import (
    compute_coverage,
    contributing_text,
    extract_notes,
    extract_targets,
    release_totals,
)
from provenance_report import build_report, distinct_missing
import release_record

DOCS_DIR = "docs"
SITE_NAME = "RetroBIOS"
REPO_URL = "https://github.com/Abdess/retrobios"
RELEASE_URL = f"{REPO_URL}/releases/latest"
SITE_URL = "https://abdess.github.io/retrobios/"
GENERATED_DIRS = ["platforms", "systems", "emulators", "wiki", "api", "downloads"]




def _content_check_ceiling(profiles: dict) -> str:
    """How far content checking can reach across every profiled entry."""
    hashed = sized = neither = 0
    for profile in unique_emulator_profiles(profiles).values():
        for f in profile.get("files", []) or []:
            if any(
                f.get(k)
                for k in ("md5", "sha1", "crc32", "sha256", "known_hash_adler32")
            ):
                hashed += 1
            elif any(f.get(k) for k in ("size", "min_size", "max_size")):
                sized += 1
            else:
                neither += 1
    total = hashed + sized + neither
    if not total:
        return ""
    return (
        f"Across every profiled entry, {hashed:,} of {total:,} "
        f"({hashed / total * 100:.0f}%) carry a hash the code checks, "
        f"{sized:,} ({sized / total * 100:.0f}%) only a size, and "
        f"{neither:,} ({neither / total * 100:.0f}%) neither. That last share "
        "is the ceiling of the method: where an emulator validates nothing, "
        "reading its source establishes which file it loads, never whether "
        "the content is the right dump. The provenance field answers that "
        "other question, for the files a dump catalog indexes."
    )





CLS_LABELS = {
    "official_port": "Official ports",
    "community_fork": "Community forks",
    "pure_libretro": "Pure libretro",
    "game_engine": "Game engines",
    "enhanced_fork": "Enhanced forks",
    "frozen_snapshot": "Frozen snapshots",
    "embedded_hle": "Embedded HLE",
    "launcher": "Launchers",
    "unclassified": "Unclassified",
    "other": "Other",
}


def _system_folder(
    files: list[dict], db: dict, name_consoles: dict[str, set[tuple[str, str]]]
) -> tuple[str, str] | None:
    """The bios/<manufacturer>/<console> folder a system's files live in."""
    votes: dict[tuple[str, str], int] = {}
    for fe in files:
        local, status = resolve_local_file(fe, db)
        parts = (local or "").split("/")
        if len(parts) >= 4 and parts[0] == "bios" and resolution_is_hash_exact(status):
            place = (parts[1], parts[2])
            votes[place] = votes.get(place, 0) + 1
    if votes:
        return max(sorted(votes), key=lambda place: votes[place])
    for fe in files:
        places = name_consoles.get(fe.get("name", ""), set())
        if len(places) == 1:
            return next(iter(places))
    return None


def _build_system_page_map_from_data(
    manufacturers: dict,
    coverages: dict,
    db: dict,
    profiles: dict | None = None,
) -> None:
    """Build system_id -> (manufacturer_slug, console_name) mapping.

    Uses platform file paths to trace system_id -> bios directory -> manufacturer page.
    """
    # Every console folder a filename sits in. A name in one folder only can
    # place a system, while a name in several says nothing (MiSTer's boot.rom lives
    # under a dozen consoles, and the last one written used to win).
    name_consoles: dict[str, set[tuple[str, str]]] = {}
    for mfr, consoles in manufacturers.items():
        for console, entries in consoles.items():
            for entry in entries:
                name_consoles.setdefault(entry["name"], set()).add((mfr, console))

    # Build normalized console name index for fuzzy matching
    console_norm: dict[str, tuple[str, str]] = {}
    for mfr, consoles in manufacturers.items():
        slug = mfr.lower().replace(" ", "-")
        mfr_norm = mfr.lower().replace(" ", "-")
        for console in consoles:
            norm = console.lower().replace(" ", "-")
            entry = (slug, console)
            console_norm[norm] = entry
            console_norm[f"{mfr_norm}-{norm}"] = entry
            # Short aliases: strip common manufacturer prefix words
            for prefix in (
                f"{mfr_norm}-",
                "nintendo-",
                "sega-",
                "sony-",
                "snk-",
                "nec-",
            ):
                if norm.startswith(prefix.replace(f"{mfr_norm}-", "")):
                    pass  # already covered by norm
                key = f"{prefix}{norm}"
                console_norm[key] = entry

    # Map system_id -> (manufacturer, console) via platform file entries
    for cov in coverages.values():
        config = cov["config"]
        for sys_id, system in config.get("systems", {}).items():
            if sys_id in _system_page_map:
                continue
            # Strategy 1: the folder holding the files the system declares,
            # found by content, and most votes win. A bare name stands in only
            # where the collection files it in a single console.
            place = _system_folder(system.get("files", []), db, name_consoles)
            if place is not None:
                mfr, console = place
                _system_page_map[sys_id] = (mfr.lower().replace(" ", "-"), console)
                continue
            # Strategy 2: fuzzy match system_id against console directory names
            if sys_id in console_norm:
                _system_page_map[sys_id] = console_norm[sys_id]
            else:
                # Try partial match: "nintendo-wii" matches "Wii" under "Nintendo"
                parts = sys_id.split("-")
                for i in range(len(parts)):
                    suffix = "-".join(parts[i:])
                    if suffix in console_norm:
                        _system_page_map[sys_id] = console_norm[suffix]
                        break

    # Systems only emulator profiles name (sega-naomi, commodore-pet): placed
    # by the files the profile files under them, as a platform's are. A
    # profile of one system files all of its entries there.
    for profile in (profiles or {}).values():
        systems = [str(sid) for sid in profile.get("systems") or []]
        for sys_id in systems:
            if sys_id in _system_page_map:
                continue
            files = [
                entry for entry in profile.get("files") or []
                if isinstance(entry, dict)
                and (entry.get("system") == sys_id or len(systems) == 1)
            ]
            place = _system_folder(files, db, name_consoles)
            if place is not None:
                mfr, console = place
                _system_page_map[sys_id] = (mfr.lower().replace(" ", "-"), console)
            elif sys_id in console_norm:
                _system_page_map[sys_id] = console_norm[sys_id]







def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")




# Home page


def _extract_table(platforms_dir: str) -> list[str]:
    """The home page's extraction table, indented for its admonition."""
    rows = [
        f"    | {display} | `{folder}` |"
        for display, folder in extract_targets(platforms_dir)
    ]
    notes = [f"    {note}" for note in extract_notes(platforms_dir)]
    return ["    | Platform | Extract to |", "    |----------|-----------|", *rows, "", *notes]


def generate_home(
    db: dict,
    coverages: dict,
    profiles: dict,
    registry: dict | None = None,
    platforms_dir: str = "platforms",
) -> str:
    total_files = db.get("total_files", 0)
    total_size = db.get("total_size", 0)
    ts = _timestamp()

    unique = unique_emulator_profiles(profiles)
    emulator_count = len(unique)

    classifications: dict[str, int] = {}
    for p in unique.values():
        cls = p.get("core_classification", "other")
        if cls not in CLS_LABELS or cls == "unclassified":
            cls = "other"
        classifications[cls] = classifications.get(cls, 0) + 1

    lines = [
        f"# {SITE_NAME}",
        "",
        "BIOS and firmware metadata checked the way each platform checks it, "
        "cross-referenced against revision-pinned emulator source code.",
        "",
        '<div class="rb-stats" markdown>',
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{total_files:,}</span>',
        f'<span class="rb-stat-label">{COLLECTION_FILES_LABEL}</span>',
        "</div>",
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{len(coverages)}</span>',
        '<span class="rb-stat-label">Platforms</span>',
        "</div>",
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{emulator_count}</span>',
        '<span class="rb-stat-label">Emulators profiled</span>',
        "</div>",
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{_fmt_size(total_size)}</span>',
        '<span class="rb-stat-label">Total size</span>',
        "</div>",
        "",
        "</div>",
        "",
        composition_sentence(db),
        "",
    ]

    # Platforms FIRST (main action)
    lines.extend(
        [
            "## Platforms",
            "",
            "| Icon | Platform | Files in the pack | Checked by | Download |",
            "|---|----------|------:|-----------|----------|",
        ]
    )

    mode_icons = {"md5": "MD5", "sha1": "SHA1", "existence": "exists"}

    for name, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        display = cov["platform"]
        logo_url = (registry or {}).get(name, {}).get("logo", "")
        logo_md = (
            f"![{display}]({logo_url}){{ width=20 loading=lazy }}" if logo_url else ""
        )
        mode_label = mode_icons.get(cov["mode"], cov["mode"])

        lines.append(
            f"| {logo_md} | [{display}](platforms/{name}.md) | "
            f"{_released_files(name)} | {mode_label} | "
            f"[Pack]({RELEASE_URL}){{ .md-button .md-button--primary }} |"
        )

    lines.extend(
        [
            "",
            "Checked by is the test each platform runs on its own, replicated "
            "from its source code. RetroArch only looks for the filename, while "
            "Batocera compares MD5 checksums, the fingerprint of a file's "
            "contents. "
            "[How each mode works](wiki/verification-modes.md).",
        ]
    )

    catalog_matched = count_catalog_matched(db)
    if catalog_matched:
        system_files = compute_composition(db)["systems"]["files"]
        lines.extend(
            [
                "",
                f"**{catalog_matched:,}** of {system_files:,} system files are "
                "byte-identical to a dump catalogued by No-Intro, Redump, or "
                "TOSEC, and say so on their system page. "
                "[What that means](provenance.md).",
            ]
        )

    # Quick start (collapsible -- secondary info)
    lines.extend(
        [
            "",
            '??? info "Where to extract"',
            "",
            *_extract_table(platforms_dir),
            "    Every other pack extracts straight into the BIOS folder."
            " [Full instructions per setup](which-pack.md).",
            "",
        ]
    )

    # Emulator classification breakdown
    lines.extend(
        [
            "## Emulator profiles",
            "",
            "| Classification | Count |",
            "|---------------|-------|",
        ]
    )
    for cls, count in sorted(classifications.items(), key=lambda x: -x[1]):
        label = CLS_LABELS.get(cls, cls)
        lines.append(f"| [{label}](emulators/index.md#{cls}) | {count} |")

    # Methodology (collapsible)
    lines.extend(
        [
            "",
            '??? abstract "Methodology"',
            "",
            "    Platform lists are checked against emulator source code, file "
            "by file where a profile exists ([how far that reaches](gaps.md)). "
            "Documentation and metadata can drift from actual runtime behavior, "
            "so the source is the primary reference.",
            "",
            "    1. **Upstream emulator source** -- what the original project "
            "loads (Dolphin, PCSX2, Mednafen...)",
            "    2. **Libretro core source** -- the RetroArch port, which may "
            "adapt paths or add files",
            "    3. **`.info` declarations** -- metadata that platforms rely on, "
            "checked for accuracy",
            "",
        ]
    )

    # Quick links
    lines.extend(
        [
            "---",
            "",
            "[Systems](systems/index.md){ .md-button } "
            "[Emulators](emulators/index.md){ .md-button } "
            "[Cross-reference](cross-reference.md){ .md-button } "
            "[Gap analysis](gaps.md){ .md-button } "
            "[Dump provenance](provenance.md){ .md-button } "
            "[Data & API](data.md){ .md-button } "
            "[Contributing](contributing.md){ .md-button .md-button--primary }",
            "",
            f'<div class="rb-timestamp">Generated on {ts}.</div>',
        ]
    )

    return "\n".join(lines) + "\n"


def compute_stats(db: dict, coverages: dict, profiles: dict) -> dict:
    unique = unique_emulator_profiles(profiles)
    systems: set[str] = set()
    for p in unique.values():
        systems.update(p.get("systems", []))
    return {
        "schema_version": 1,
        "generated_at": _timestamp(),
        "files": db.get("total_files", 0),
        "size_bytes": db.get("total_size", 0),
        "platforms": len(coverages),
        "emulators": len(unique),
        "systems": len(systems),
        "catalog_matched": count_catalog_matched(db),
        "source": REPO_URL,
        "downloads": RELEASE_URL,
    }


def _released_files(platform_name: str) -> str:
    """File count of the pack the latest release serves, as a table cell."""
    files, _size = release_totals(platform_name, release_record.load_record())
    return f"{files:,}" if files else "-"


# The home tile counts the whole collection. Labelled "Files" above a table
# of pack buttons, it read as what one pack contains.
COLLECTION_FILES_LABEL = "Files collected"


def composition_sentence(db: dict) -> str:
    comp = compute_composition(db)
    return (
        f"The collection holds {comp['systems']['files']:,} console and "
        f"computer system files, {comp['arcade']['files']:,} arcade ROM sets "
        f"(`Arcade/`), and {comp['game_data']['files']:,} game and engine "
        "data files (`Game Engines/`, `RPG Maker/`, `ScummVM/` and the game "
        "publishers' trees). No pack holds "
        "them all: each carries what its platform's emulators load."
    )


def generate_stats(stats: dict) -> str:
    return json.dumps(stats, indent=2) + "\n"


def _json_text(value: object) -> str:
    """Stable scalar representation for CSV and SQLite exports."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, bool)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


HASH_ALGORITHMS = ("sha1", "sha256", "md5", "crc32")


def _hash_values(value: object) -> list[str]:
    """Every accepted value of one hash field, lowercase.

    A field holds one value, a list of accepted revisions, or (Recalbox) a
    comma-joined string: an exact lookup missed all but the first form.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return sorted({str(item).strip().lower() for item in items if str(item).strip()})


def _accepted_hash_rows(platform_files: list[dict], emulator_items: list[dict]) -> list[dict]:
    """One row per accepted hash value, for both layers, lookup-ready."""
    rows: list[dict] = []
    for row in platform_files:
        for algorithm in HASH_ALGORITHMS:
            for value in _hash_values(row.get(f"{algorithm}_raw", row.get(algorithm))):
                rows.append({
                    "layer": "platform", "owner": row["platform_id"],
                    "system": row["system"], "name": row["name"],
                    "path": row["destination"], "algorithm": algorithm, "value": value,
                })
    for item in emulator_items:
        for entry in item["profile"].get("files", []) or []:
            if not isinstance(entry, dict):
                continue
            for algorithm in HASH_ALGORITHMS:
                for value in _hash_values(entry.get(algorithm)):
                    rows.append({
                        "layer": "emulator", "owner": item["id"],
                        "system": _json_text(entry.get("system")),
                        "name": entry.get("name", ""), "path": entry.get("path", "") or "",
                        "algorithm": algorithm, "value": value,
                    })
            # Members of an archive: MAME and FBNeo hash the ROMs inside the
            # set, never the set. Left out, a loose nmk004.bin (crc32
            # 83b6f611) found no row while the site search found it.
            archive = entry.get("name", "")
            for member in entry.get("contents") or []:
                if not isinstance(member, dict) or not member.get("name"):
                    continue
                for algorithm in HASH_ALGORITHMS:
                    for value in _hash_values(member.get(algorithm)):
                        rows.append({
                            "layer": "emulator", "owner": item["id"],
                            "system": _json_text(entry.get("system")),
                            "name": str(member["name"]), "path": f"{archive}/{member['name']}",
                            "algorithm": algorithm, "value": value,
                        })
    rows.sort(key=lambda r: (r["layer"], r["owner"], r["name"], r["algorithm"], r["value"]))
    return rows


def _csv_document(fieldnames: list[str], rows: list[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_export_rows(coverages: dict) -> tuple[list[dict], list[dict]]:
    """Normalized platform summaries and one row per declared file."""
    items: list[dict] = []
    file_rows: list[dict] = []
    for key, coverage in sorted(coverages.items()):
        config = coverage["config"]
        items.append({
            "id": key,
            "name": coverage["platform"],
            "coverage": {
                field: coverage.get(field, 0)
                for field in (
                    "total", "present", "verified", "untested", "missing",
                    "core_present", "core_missing", "core_unsourceable",
                )
            },
            "contract": config,
        })
        for system_id, system in sorted(config.get("systems", {}).items()):
            for entry in system.get("files", []) or []:
                file_rows.append({
                    "platform_id": key,
                    "platform": coverage["platform"],
                    "system": system_id,
                    "name": entry.get("name", ""),
                    "destination": entry.get(
                        "destination", entry.get("dest", entry.get("name", ""))
                    ),
                    "required": bool(entry.get("required", True)),
                    "region": _json_text(entry.get("region")),
                    "variant_group": _json_text(entry.get("variant_group")),
                    "size": entry.get("size"),
                    "sha1": _json_text(entry.get("sha1")),
                    "sha256": _json_text(entry.get("sha256")),
                    "md5": _json_text(entry.get("md5")),
                    "crc32": _json_text(entry.get("crc32")),
                })
    return items, file_rows


def _emulator_export_items(profiles: dict) -> list[dict]:
    return [
        {"id": key, "profile": profile}
        for key, profile in sorted(profiles.items())
    ]


def build_emulator_gap_report(
    profiles: dict,
    coverages: dict,
    db: dict,
    data_names: set[str] | None = None,
) -> dict:
    """Files a core loads that no platform declares, per emulator profile.

    The gap analysis page and the published gaps export must not compute this
    twice and drift. Both read this one report.
    """
    from common import _norm_system_id, merged_platform_declarations
    from cross_reference import cross_reference as run_cross_reference

    # Enriched with alias resolution, with each declaration's size and slot:
    # a name alone settled DOSBox's 32 KiB SC-55 ROM1.BIN on Galaksija's.
    all_declared = merged_platform_declarations(
        [cov["config"] for cov in coverages.values()], db
    )
    declared: dict[str, set[str]] = {}
    for _name, cov in coverages.items():
        config = cov["config"]
        for sys_id, system in config.get("systems", {}).items():
            for fe in system.get("files", []):
                fname = fe.get("name", "")
                if fname:
                    declared.setdefault(_norm_system_id(sys_id), set()).add(fname)

    unique_profiles = {
        k: v
        for k, v in profiles.items()
        if v.get("type") not in ("alias", "test")
    }
    from common import standalone_set

    return run_cross_reference(
        unique_profiles, declared, db,
        data_names=data_names, all_declared=all_declared,
        standalone_cores=set().union(
            *(standalone_set(cov["config"]) for cov in coverages.values())
        ),
    )


def _gap_export_rows(coverages: dict, gap_report: dict | None = None) -> list[dict]:
    """Every gap the site reports, both layers, in one table.

    ``platform`` rows are anomalies against a platform's own BIOS list.
    ``emulator`` rows are files a profiled core loads that no platform
    declares, which is the larger number the gap analysis page leads with.
    A `layer` column keeps the two apart instead of publishing only one.
    """
    rows: list[dict] = []
    for key, coverage in sorted(coverages.items()):
        for detail in coverage.get("details", []):
            if detail.get("status") == "ok" and not detail.get("discrepancy"):
                continue
            rows.append({
                "layer": "platform",
                "platform_id": key,
                "platform": coverage["platform"],
                "emulator": "",
                "system": detail.get("system", ""),
                "name": detail.get("name", ""),
                "status": detail.get("status", ""),
                "required": bool(detail.get("required", True)),
                "in_repo": "",
                "reason": detail.get("reason", ""),
                "discrepancy": detail.get("discrepancy", ""),
                "destination": detail.get("destination", ""),
                "expected": "",
            })
    for emu_key, data in sorted((gap_report or {}).items()):
        systems = ";".join(str(s) for s in data.get("systems", []) or [])
        for gap in data.get("gap_details", []) or []:
            rows.append({
                "layer": "emulator",
                "platform_id": "",
                "platform": "",
                "emulator": data.get("emulator", emu_key),
                "system": systems,
                "name": gap.get("name", ""),
                # A file the emulator's own package installs is not one to
                # find: its own status, as unsourceable has.
                "status": (
                    "bundled" if gap.get("bundled") and gap.get("source") == "missing"
                    else gap.get("source", "")
                ),
                "required": bool(gap.get("required", False)),
                "in_repo": bool(gap.get("in_repo", False)),
                "reason": gap.get("note", ""),
                "discrepancy": "",
                # What a contributor needs to supply the right revision and
                # put it in the right place: the gaps table gave neither.
                "destination": gap.get("path") or gap.get("name", ""),
                "expected": gap.get("identity", ""),
            })
        for entry in data.get("unsourceable", []) or []:
            rows.append({
                "layer": "emulator",
                "platform_id": "",
                "platform": "",
                "emulator": data.get("emulator", emu_key),
                "system": systems,
                "name": entry.get("name", ""),
                "status": "unsourceable",
                "required": bool(entry.get("required", False)),
                "in_repo": False,
                "reason": entry.get("reason", ""),
                "discrepancy": "",
                "destination": entry.get("path") or entry.get("name", ""),
                "expected": "",
            })
    return rows


def _cross_reference_export_rows(coverages: dict, profiles: dict) -> list[dict]:
    from common import resolve_platform_cores

    unique = {
        key: value
        for key, value in profiles.items()
        if value.get("type") not in ("alias", "test")
    }
    rows: list[dict] = []
    for platform_id, coverage in sorted(coverages.items()):
        for profile_id in sorted(resolve_platform_cores(coverage["config"], unique)):
            profile = unique[profile_id]
            cores = profile.get("cores") or [profile_id]
            systems = profile.get("systems") or [""]
            for core in cores:
                for system in systems:
                    rows.append({
                        "platform_id": platform_id,
                        "platform": coverage["platform"],
                        "profile_id": profile_id,
                        "emulator": profile.get("emulator", profile_id),
                        "core": core,
                        "system": system,
                        "classification": profile.get("core_classification", ""),
                        "type": profile.get("type", ""),
                        "source": _json_text(profile.get("source")),
                        "upstream": _json_text(profile.get("upstream")),
                        "profiled_commit": _json_text(profile.get("source_commit", "")),
                        "file_count": len(profile.get("files", []) or []),
                    })
    return rows


def _write_sqlite_export(
    destination: Path,
    db: dict,
    platform_items: list[dict],
    platform_files: list[dict],
    emulator_items: list[dict],
    gap_rows: list[dict],
) -> None:
    """Build a queryable, deterministic snapshot without embedding binaries."""
    temp_dir = Path("tmp") / "site"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"retrobios-{os.getpid()}.sqlite"
    temp_path.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(temp_path)
    try:
        connection.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
                sha1 TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, md5 TEXT NOT NULL, sha256 TEXT NOT NULL,
                crc32 TEXT NOT NULL, adler32 TEXT NOT NULL
            );
            CREATE TABLE file_provenance (
                sha1 TEXT NOT NULL, catalog TEXT NOT NULL, details_json TEXT NOT NULL,
                PRIMARY KEY (sha1, catalog),
                FOREIGN KEY (sha1) REFERENCES files(sha1)
            );
            CREATE TABLE platforms (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, total INTEGER NOT NULL,
                present INTEGER NOT NULL, verified INTEGER NOT NULL,
                missing INTEGER NOT NULL, contract_json TEXT NOT NULL
            );
            CREATE TABLE platform_files (
                platform_id TEXT NOT NULL, system TEXT NOT NULL, name TEXT NOT NULL,
                destination TEXT NOT NULL, required INTEGER NOT NULL,
                region TEXT, variant_group TEXT, size INTEGER,
                sha1 TEXT, sha256 TEXT, md5 TEXT, crc32 TEXT
            );
            CREATE TABLE emulators (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT,
                classification TEXT, source TEXT, upstream TEXT,
                source_commit TEXT, profile_json TEXT NOT NULL
            );
            CREATE TABLE emulator_systems (
                emulator_id TEXT NOT NULL, system TEXT NOT NULL,
                PRIMARY KEY (emulator_id, system)
            );
            CREATE TABLE emulator_files (
                emulator_id TEXT NOT NULL, system TEXT, name TEXT NOT NULL,
                path TEXT, required INTEGER NOT NULL, mode TEXT, region TEXT,
                size TEXT, sha1 TEXT, sha256 TEXT, md5 TEXT, crc32 TEXT,
                source_ref TEXT
            );
            CREATE TABLE gaps (
                layer TEXT NOT NULL, platform_id TEXT, platform TEXT,
                emulator TEXT, system TEXT, name TEXT NOT NULL,
                status TEXT NOT NULL, required INTEGER NOT NULL,
                in_repo TEXT, reason TEXT, discrepancy TEXT,
                destination TEXT, expected TEXT
            );
            CREATE TABLE accepted_hashes (
                layer TEXT NOT NULL, owner TEXT NOT NULL, system TEXT,
                name TEXT NOT NULL, path TEXT, algorithm TEXT NOT NULL,
                value TEXT NOT NULL
            );
            CREATE INDEX accepted_hashes_value_idx ON accepted_hashes(algorithm, value);
            CREATE INDEX files_name_idx ON files(name);
            CREATE INDEX files_md5_idx ON files(md5);
            CREATE INDEX files_sha256_idx ON files(sha256);
            CREATE INDEX platform_files_name_idx ON platform_files(name);
            CREATE INDEX emulator_files_name_idx ON emulator_files(name);
            CREATE INDEX gaps_status_idx ON gaps(layer, status);
        """)
        metadata = {
            "schema_version": "1",
            "generated_at": str(db.get("generated_at") or _timestamp()),
            "source": REPO_URL,
            "scope": "metadata only, no BIOS or firmware payload bytes",
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        for sha1, entry in sorted(db.get("files", {}).items()):
            connection.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha1, entry.get("path", ""), entry.get("name", ""),
                    entry.get("size", 0), entry.get("md5", ""),
                    entry.get("sha256", ""), entry.get("crc32", ""),
                    entry.get("adler32", ""),
                ),
            )
            for catalog, details in sorted((entry.get("provenance") or {}).items()):
                connection.execute(
                    "INSERT INTO file_provenance VALUES (?, ?, ?)",
                    (sha1, catalog, _json_text(details)),
                )
        for item in platform_items:
            coverage = item["coverage"]
            connection.execute(
                "INSERT INTO platforms VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item["id"], item["name"], coverage.get("total", 0),
                    coverage.get("present", 0), coverage.get("verified", 0),
                    coverage.get("missing", 0), _json_text(item["contract"]),
                ),
            )
        connection.executemany(
            "INSERT INTO platform_files VALUES "
            "(:platform_id, :system, :name, :destination, :required, :region, "
            ":variant_group, :size, :sha1, :sha256, :md5, :crc32)",
            platform_files,
        )
        for item in emulator_items:
            profile = item["profile"]
            connection.execute(
                "INSERT INTO emulators VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["id"], profile.get("emulator", item["id"]),
                    profile.get("type", ""),
                    profile.get("core_classification", ""),
                    _json_text(profile.get("source")),
                    _json_text(profile.get("upstream")),
                    # source_commit is a string or, when the builds live in
                    # separate repositories, an object keyed by build mode.
                    # SQLite cannot bind the object form.
                    _json_text(profile.get("source_commit", "")),
                    _json_text(profile),
                ),
            )
            for system in sorted(set(profile.get("systems", []) or [])):
                connection.execute(
                    "INSERT INTO emulator_systems VALUES (?, ?)",
                    (item["id"], system),
                )
            for entry in profile.get("files", []) or []:
                connection.execute(
                    "INSERT INTO emulator_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["id"], _json_text(entry.get("system")),
                        entry.get("name", ""), entry.get("path", ""),
                        int(bool(entry.get("required", False))),
                        entry.get("mode", ""), _json_text(entry.get("region")),
                        _json_text(entry.get("size")), _json_text(entry.get("sha1")),
                        _json_text(entry.get("sha256")), _json_text(entry.get("md5")),
                        _json_text(entry.get("crc32")),
                        _json_text(entry.get("source_ref")),
                    ),
                )
        connection.executemany(
            "INSERT INTO accepted_hashes VALUES "
            "(:layer, :owner, :system, :name, :path, :algorithm, :value)",
            _accepted_hash_rows(platform_files, emulator_items),
        )
        connection.executemany(
            "INSERT INTO gaps VALUES "
            "(:layer, :platform_id, :platform, :emulator, :system, :name, "
            ":status, :required, :in_repo, :reason, :discrepancy, :destination, :expected)",
            gap_rows,
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    os.replace(temp_path, destination)


def generate_data_exports(
    docs: Path,
    db: dict,
    coverages: dict,
    profiles: dict,
    stats: dict,
    gap_report: dict | None = None,
) -> list[dict]:
    """Create versioned static API, CSV and SQLite metadata snapshots."""
    api = docs / "api" / "v1"
    downloads = docs / "downloads"
    schemas_dest = api / "schemas"
    for directory in (api, downloads, schemas_dest):
        directory.mkdir(parents=True, exist_ok=True)

    generated_at = str(db.get("generated_at") or _timestamp())
    platform_items, platform_files = _platform_export_rows(coverages)
    emulator_items = _emulator_export_items(profiles)
    gap_rows = _gap_export_rows(coverages, gap_report)
    cross_rows = _cross_reference_export_rows(coverages, profiles)

    envelopes = {
        "platforms.json": ("platforms", platform_items),
        "emulators.json": ("emulators", emulator_items),
        "gaps.json": ("verification-and-coverage-gaps", gap_rows),
        "file-locations.json": ("file-locations", file_location_items(
            db, profiles, data_directory_members())),
    }
    for filename, (kind, items) in envelopes.items():
        document = {
            "schema_version": 1,
            "generated_at": generated_at,
            "kind": kind,
            "count": len(items),
            "items": items,
        }
        write_if_changed(
            str(api / filename),
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    write_if_changed(str(api / "stats.json"), generate_stats(stats))
    write_if_changed(
        str(api / "database.json"),
        json.dumps(db, ensure_ascii=False, indent=2) + "\n",
    )

    schema_names = (
        "database.schema.json", "emulator.schema.json", "platform.schema.json",
        "site-api-envelope.schema.json", "stats.schema.json",
    )
    for schema_name in schema_names:
        shutil.copy2(Path("schemas") / schema_name, schemas_dest / schema_name)
        _record(schemas_dest / schema_name)

    file_rows = []
    for sha1, entry in sorted(db.get("files", {}).items()):
        file_rows.append({
            "sha1": sha1,
            "path": entry.get("path", ""),
            "name": entry.get("name", ""),
            "size": entry.get("size", 0),
            "md5": entry.get("md5", ""),
            "sha256": entry.get("sha256", ""),
            "crc32": entry.get("crc32", ""),
            "adler32": entry.get("adler32", ""),
            "provenance_catalogs": ";".join(
                sorted((entry.get("provenance") or {}).keys())
            ),
        })
    write_if_changed(str(downloads / "retrobios.dat"), collection_dat(db, generated_at))
    write_if_changed(
        str(downloads / "files.csv"),
        _csv_document(
            [
                "sha1", "path", "name", "size", "md5", "sha256",
                "crc32", "adler32", "provenance_catalogs",
            ],
            file_rows,
        ),
    )
    write_if_changed(
        str(downloads / "platform-files.csv"),
        _csv_document(
            [
                "platform_id", "platform", "system", "name", "destination",
                "required", "region", "variant_group", "size", "sha1",
                "sha256", "md5", "crc32",
            ],
            platform_files,
        ),
    )
    write_if_changed(
        str(downloads / "cross-reference.csv"),
        _csv_document(
            [
                "platform_id", "platform", "profile_id", "emulator", "core",
                "system", "classification", "type", "source", "upstream",
                "profiled_commit", "file_count",
            ],
            cross_rows,
        ),
    )
    write_if_changed(
        str(downloads / "gaps.csv"),
        _csv_document(
            [
                "layer", "platform_id", "platform", "emulator", "system",
                "name", "status", "required", "in_repo", "reason",
                "discrepancy", "destination", "expected",
            ],
            gap_rows,
        ),
    )
    write_if_changed(
        str(downloads / "accepted-hashes.csv"),
        _csv_document(
            ["layer", "owner", "system", "name", "path", "algorithm", "value"],
            _accepted_hash_rows(platform_files, emulator_items),
        ),
    )
    _write_sqlite_export(
        downloads / "retrobios.sqlite", db, platform_items, platform_files,
        emulator_items, gap_rows,
    )
    _record(downloads / "retrobios.sqlite")

    assets = [
        (api / "database.json", "Content database", "application/json", "schemas/database.schema.json"),
        (api / "platforms.json", "Platform contracts", "application/json", "schemas/site-api-envelope.schema.json"),
        (api / "emulators.json", "Emulator profiles", "application/json", "schemas/site-api-envelope.schema.json"),
        (api / "gaps.json", "Verification and coverage gaps", "application/json", "schemas/site-api-envelope.schema.json"),
        (api / "file-locations.json", "Where each file's card is, by SHA1", "application/json", "schemas/site-api-envelope.schema.json"),
        (api / "stats.json", "Project statistics", "application/json", "schemas/stats.schema.json"),
        (downloads / "files.csv", "File hashes CSV", "text/csv", None),
        (downloads / "retrobios.dat", "Collection DAT (Logiqx XML, for clrmamepro and RomVault)",
         "application/xml", None),
        (downloads / "platform-files.csv", "Platform declarations CSV", "text/csv", None),
        (downloads / "cross-reference.csv", "Cross-reference CSV", "text/csv", None),
        (downloads / "gaps.csv", "Verification and coverage gaps CSV", "text/csv", None),
        (downloads / "accepted-hashes.csv", "Accepted hashes, one row per value", "text/csv", None),
        (downloads / "retrobios.sqlite", "SQLite snapshot", "application/vnd.sqlite3", None),
    ]
    catalog_items: list[dict] = []
    published: list[dict] = []
    for path, title, media_type, schema in assets:
        relative = path.relative_to(docs).as_posix()
        item = {
            "title": title,
            "url": relative,
            "media_type": media_type,
            "bytes": path.stat().st_size,
            "sha256": _sha256_path(path),
        }
        if schema:
            item["schema"] = schema
        catalog_items.append(item)
        # In the catalog both references resolve against the catalog's own
        # URL, as RFC 3986 clients do: `url` was relative to the site root
        # and `schema` to api/v1/, so no single base resolved both.
        published.append({
            **item,
            "url": os.path.relpath(path, api).replace(os.sep, "/"),
        })
    catalog = {
        "schema_version": 1,
        "generated_at": generated_at,
        "kind": "catalog",
        "count": len(published),
        "items": published,
    }
    write_if_changed(
        str(api / "catalog.json"),
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return catalog_items


def generate_data_page(exports: list[dict]) -> str:
    lines = [
        "# Data & API",
        "",
        "RetroBIOS publishes the same metadata used by its verifier, pack builder "
        "and website as versioned static files. No account or API key is required.",
        "",
        "[API catalog](api/v1/catalog.json){ .md-button .md-button--primary } "
        "[SQLite snapshot](downloads/retrobios.sqlite){ .md-button } "
        "[Collection DAT](downloads/retrobios.dat){ .md-button }",
        "",
        "The DAT is Logiqx XML, the format clrmamepro and RomVault read: one game "
        "per `bios/<Manufacturer>/<Console>/` folder of the collection, named "
        "`Manufacturer\\Console` so the tools look in that subfolder, each file a "
        "rom named by its path below that folder. It audits a copy of the "
        "collection's `bios/` tree, or rebuilds that layout from your own dumps. A "
        "platform pack has its own layout and is checked with `install.py --check`.",
        "",
        "## Stable JSON endpoints",
        "",
        "| Dataset | Endpoint | Schema | Size |",
        "|---------|----------|--------|-----:|",
    ]
    for item in exports:
        if item["media_type"] != "application/json":
            continue
        schema = (
            f"[JSON Schema](api/v1/{item['schema']})"
            if item.get("schema") else "-"
        )
        lines.append(
            f"| {item['title']} | [`{item['url']}`]({item['url']}) | "
            f"{schema} | {_fmt_size(item['bytes'])} |"
        )
    lines.extend([
        "",
        "Every JSON document carries `schema_version`. Breaking changes use a new "
        "URL prefix (`/api/v2/`), and fields may only be added compatibly within v1.",
        "",
        "One emulator profile without the whole list: "
        "`api/v1/emulators/<id>.json`, the envelope of `emulators.json` holding "
        "that one item, its `profile` valid against "
        "[`emulator.schema.json`](api/v1/schemas/emulator.schema.json). Each "
        "emulator page links its own.",
        "",
        "## Bulk downloads",
        "",
        "| Export | Format | Size | SHA256 |",
        "|--------|--------|-----:|--------|",
    ])
    for item in exports:
        if item["media_type"] == "application/json":
            continue
        lines.append(
            f"| [{item['title']}]({item['url']}) | `{item['media_type']}` | "
            f"{_fmt_size(item['bytes'])} | `{item['sha256']}` |"
        )
    lines.extend([
        "",
        "The SQLite file contains indexed tables for content hashes, provenance "
        "catalog matches, platform declarations, emulator profiles and current "
        "gaps. It contains metadata only, never BIOS or firmware bytes.",
        "",
        "To find who accepts a hash, query `accepted_hashes` (or "
        "`accepted-hashes.csv`): one lowercase row per accepted value, per "
        "algorithm, for platform declarations and emulator profiles alike, "
        "indexed on `(algorithm, value)`. A ROM inside a MAME or FBNeo set "
        "has its own row, its `path` naming the archive and the member "
        "(`nmk004.zip/nmk004.bin`). The hash columns of `platform_files` "
        "and `emulator_files` keep each source's own form: a Recalbox md5 may "
        "list several revisions separated by commas, and an emulator field "
        "holding several accepted values is a JSON array.",
        "",
        "```sql",
        "SELECT layer, owner, name FROM accepted_hashes",
        "WHERE algorithm = 'md5' AND value = 'c87a52c277f7952b41c639fc7bf0a43b';",
        "```",
        "",
        "The gaps dataset carries both layers behind one `layer` column: "
        "`platform` rows are anomalies against a platform's own BIOS list, "
        "`emulator` rows are files a profiled core loads that no platform "
        "declares. The two answer different questions and are not comparable "
        "totals.",
        "",
        "## Semantics and limits",
        "",
        "Treat four questions separately: content identity (hashes), presence in "
        "the collection, acceptance by a specific emulator, and catalog provenance. "
        "One does not imply the others. In particular, presence, a matching dump "
        "catalog, or emulator compatibility is not a statement about copyright, "
        "ownership, or redistribution rights.",
        "",
        f"The data can be newer than the [latest published pack]({RELEASE_URL}). "
        "A pack is published by hand, once every release check has passed.",
        "",
        "See the [data model](wiki/data-model.md), [verification modes]"
        "(wiki/verification-modes.md), and [methodology](wiki/architecture.md) "
        "before interpreting aggregate counts.",
        "",
    ])
    return "\n".join(lines)


def _page_title(markdown: str, path: Path) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    if match:
        title = re.sub(r"<[^>]+>", "", match.group(1))
        return title.replace(f" - {SITE_NAME}", "").strip()
    return path.stem.replace("-", " ").title()


def _browser_title(relative: Path, title: str) -> str:
    """Return a concise, unique browser/search title for a generated page."""
    key = relative.as_posix()
    index_titles = {
        # Material appends " - RetroBIOS": the home title says what the site
        # is, instead of "RetroBIOS - RetroBIOS".
        "index.md": "BIOS and firmware packs for RetroArch, Batocera and more",
        "platforms/index.md": "Platforms",
        "systems/index.md": "Systems",
        "emulators/index.md": "Emulators",
        "wiki/index.md": "Guide and methodology",
    }
    if key in index_titles:
        return index_titles[key]
    if key.startswith("emulators/"):
        return f"{title} emulator firmware"
    if key.startswith("systems/"):
        return f"{title} systems"
    return title


def _plain_markdown(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _page_description(markdown: str, relative: Path, title: str) -> str:
    key = relative.as_posix()
    specific = {
        "index.md": "Source-traced BIOS and firmware metadata, platform verification, emulator profiles, gaps and reproducible retrogaming data exports.",
        "data.md": "Versioned RetroBIOS JSON API, CSV exports, SQLite snapshot, schemas, checksums and data interpretation guidance.",
        "cross-reference.md": "Cross-reference from retrogaming platforms to emulator cores, systems, upstream projects and profiled firmware files.",
        "gaps.md": "Current RetroBIOS verification gaps, platform-to-emulator divergences, missing files and documented source limitations.",
        "provenance.md": "Hash-based comparison of RetroBIOS metadata with No-Intro, Redump and TOSEC catalog snapshots.",
        "which-pack.md": "One-line automatic RetroBIOS installation and platform-specific pack destinations, with verification and release caveats.",
    }
    if key in specific:
        return specific[key]
    if key.startswith("platforms/") and relative.stem != "index":
        return f"{title}: declared BIOS contract, verification mode, coverage, destinations and emulator complement."
    if key.startswith("emulators/") and relative.stem != "index":
        # Pinned only when the profile names the revision it was read at:
        # 48 pages without one called themselves source-pinned.
        about = PAGE_DATA.get(key, {}).get("about", {})
        pinned = any(a.get("version") for a in (about if isinstance(about, list) else [about]))
        kind = "source-pinned emulator firmware profile" if pinned else "emulator firmware profile"
        return f"{title}: {kind} with paths, hashes, requirements and validation behavior."
    if key.startswith("systems/") and relative.stem != "index":
        return f"{title}: indexed firmware files, hashes, variants, provenance and platform or emulator usage."

    paragraphs = re.split(r"\n\s*\n", markdown)
    for paragraph in paragraphs:
        stripped = paragraph.strip()
        if (
            not stripped
            or stripped.startswith(("#", "|", "```", "???", "<", "- ", "* "))
        ):
            continue
        plain = _plain_markdown(stripped)
        if len(plain) >= 35:
            return plain[:157].rstrip(" ,;:-") + ("..." if len(plain) > 157 else "")
    return f"{title}: RetroBIOS source-traced retrogaming firmware reference."


# Structured data a page generator knows beyond its text, by page path
# relative to docs/: read by decorate_markdown_pages.
PAGE_DATA: dict[str, dict] = {}


SYSTEM_PARTS_IN_FULL = 1000


def collection_dat(db: dict, generated_at: str) -> str:
    """The collection as a Logiqx DAT, the format clrmamepro and RomVault read.

    One game per bios/<Manufacturer>/<Console>/ folder, named
    Manufacturer\\Console, each file a rom named by its path below that
    folder, subfolders joined with a backslash as Logiqx DATs write them. An auditor checking a BIOS tree against the
    collection had JSON, CSV and SQLite, and a converter to write.
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    games: dict[str, list[dict]] = {}
    for entry in db.get("files", {}).values():
        parts = str(entry.get("path", "")).split("/")
        if len(parts) < 3 or parts[0] != "bios":
            continue
        folder = parts[1:3] if len(parts) > 3 else parts[1:2]
        # A backslash is how a Logiqx set name writes a subfolder: named
        # "Sony - PlayStation", the set was looked for beside Sony/, and an
        # audit of a copy of bios/ found 330 sets missing.
        games.setdefault("\\".join(folder), []).append(
            {**entry, "_rom": "\\".join(parts[1 + len(folder):])}
        )
    root = ET.Element("datafile")
    header = ET.SubElement(root, "header")
    version = generated_at[:10].replace("-", "")
    for tag, text in (
        ("name", SITE_NAME),
        ("description", f"{SITE_NAME} collection ({sum(map(len, games.values()))} files)"),
        ("version", version),
        ("date", generated_at[:10]),
        ("author", SITE_NAME),
        ("homepage", SITE_URL),
        ("url", REPO_URL),
    ):
        ET.SubElement(header, tag).text = text
    for game_name in sorted(games):
        game = ET.SubElement(root, "game", name=game_name)
        ET.SubElement(game, "description").text = game_name
        for entry in sorted(games[game_name], key=lambda e: e["_rom"]):
            attributes = {"name": entry["_rom"], "size": str(entry.get("size", 0))}
            for attribute, field in (("crc", "crc32"), ("md5", "md5"), ("sha1", "sha1")):
                if entry.get(field):
                    attributes[attribute] = str(entry[field]).lower()
            ET.SubElement(game, "rom", attributes)
    ET.indent(root, space="\t")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE datafile PUBLIC "-//Logiqx//DTD ROM Management Datafile//EN" '
        '"http://www.logiqx.com/Dats/datafile.dtd">\n'
        + ET.tostring(root, encoding="unicode") + "\n"
    )


def system_structured_data(slug: str, consoles: dict[str, list]) -> dict:
    """A system page's files for a machine: each card as a CreativeWork.

    The pages that hold a card per file published a bare TechArticle with
    no file, hash or provenance, while emulator pages listed theirs.
    """
    parts = []
    for _console, entries in sorted(consoles.items()):
        for entry in entries:
            sha1 = str(entry.get("sha1") or "")
            part: dict = {"@type": "CreativeWork", "name": str(entry.get("name", ""))}
            if sha1:
                part["url"] = urllib.parse.urljoin(SITE_URL, f"systems/{slug}/#sha1-{sha1[:12]}")
            identifiers = [
                f"{field}:{str(entry[field]).lower()}"
                for field in ("sha1", "md5", "crc32", "sha256") if entry.get(field)
            ]
            if identifiers:
                part["identifier"] = identifiers
            if entry.get("size"):
                part["contentSize"] = f"{entry['size']} B"
            citations = [
                f"{catalog}: {record.get('dat', '')} ({record.get('name', '')})"
                for catalog, record in sorted((entry.get("provenance") or {}).items())
                if isinstance(record, dict)
            ]
            if citations:
                part["citation"] = citations
            parts.append(part)
    if len(parts) > SYSTEM_PARTS_IN_FULL:
        # game-engines would carry 2.6 MB of JSON-LD: past the cap a page
        # lists its catalogue-verified files and points at the full table.
        return {
            "hasPart": [part for part in parts if part.get("citation")],
            "isBasedOn": urllib.parse.urljoin(SITE_URL, "downloads/files.csv"),
        }
    return {"hasPart": parts} if parts else {}


def platform_structured_data(name: str, coverage: dict) -> dict:
    """A platform page's declarations for a machine reader.

    The page printed thousands of hashes while its JSON-LD named only the
    page: a crawler could not learn which md5 Batocera requires for a file.
    The software and its version are the subject, and each declared file, with
    the hashes the platform checks, is a part. Past SYSTEM_PARTS_IN_FULL the
    parts give way to the platform-files table, as system pages do.
    """
    config = coverage.get("config") or {}
    about: dict = {"@type": "SoftwareApplication", "name": str(coverage.get("platform", name))}
    if config.get("version"):
        about["softwareVersion"] = str(config["version"])
    if config.get("homepage"):
        about["url"] = str(config["homepage"])
    parts = []
    for system_id, system in sorted((config.get("systems") or {}).items()):
        for entry in system.get("files") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            part: dict = {
                "@type": "CreativeWork",
                "name": str(entry.get("destination") or entry["name"]),
                "url": urllib.parse.urljoin(SITE_URL, f"platforms/{name}/#{system_id}"),
            }
            identifiers = [
                f"{field}:{value}"
                for field in ("sha1", "md5", "crc32", "sha256")
                for value in _hash_values(entry.get(field))
            ]
            if identifiers:
                part["identifier"] = identifiers
            parts.append(part)
    data: dict = {"about": about}
    if len(parts) > SYSTEM_PARTS_IN_FULL:
        data["isBasedOn"] = urllib.parse.urljoin(SITE_URL, "downloads/platform-files.csv")
    elif parts:
        data["hasPart"] = parts
    return data


def dataset_structured_data(exports: list[dict], db: dict) -> dict:
    """The data page as a schema.org Dataset a harvester can index.

    It was a Dataset named "Data & API" with no download, licence, date or
    creator, so Google Dataset Search and open-data catalogues had nothing
    to list. The exports are metadata this project authors: MIT, as the
    LICENSE's scope says.
    """
    data: dict = {
        "name": f"{SITE_NAME} collection data",
        "license": "https://opensource.org/licenses/MIT",
        "creator": {"@type": "Organization", "name": SITE_NAME, "url": REPO_URL},
        "distribution": [
            {
                "@type": "DataDownload",
                "name": item["title"],
                "contentUrl": urllib.parse.urljoin(SITE_URL, item["url"]),
                "encodingFormat": item["media_type"],
                "contentSize": f"{item['bytes']} B",
                "sha256": item["sha256"],
            }
            for item in exports
        ],
    }
    generated = str(db.get("generated_at") or "")[:10]
    if generated:
        data["dateModified"] = generated
    return data


def emulator_file_anchors(files: list[dict]) -> list[str]:
    """The id of each file card on an emulator page, in profile order.

    A path declared once keeps its plain anchor. One declared for several
    versions (raze's nine DUKE3D.GRP) takes what tells the version apart:
    its hash, else its size, else, when even the size is shared, its
    description. The page and its JSON-LD both read this, so a structured
    part links to its card.
    """
    bases = [
        "file-" + (
            re.sub(
                r"[^a-z0-9]+", "-",
                str(f.get("path") or f.get("standalone_path") or f.get("name", "")).lower(),
            ).strip("-") or "entry"
        )
        for f in files
    ]
    repeated = {b for b in bases if bases.count(b) > 1}

    def _version_key(f: dict) -> str:
        digest = _first_hash_value(f)
        if digest:
            return digest[:8]
        return f"{f['size']}b" if f.get("size") else ""

    keys = [(b, _version_key(f)) if b in repeated else (b, "") for f, b in zip(files, bases)]
    used: dict[str, int] = {}
    anchors = []
    for f, (base, key) in zip(files, keys):
        anchor = f"{base}-{key}" if key else base
        if base in repeated and keys.count((base, key)) > 1 and f.get("description"):
            described = re.sub(r"[^a-z0-9]+", "-", str(f["description"]).lower()).strip("-")
            anchor = f"{anchor}-{described[:40].strip('-')}"
        used[anchor] = used.get(anchor, 0) + 1
        if used[anchor] > 1:
            anchor = f"{anchor}-{used[anchor]}"
        anchors.append(anchor)
    return anchors


def emulator_structured_data(profile: dict) -> dict:
    """What an emulator page states, for a reader of its JSON-LD.

    The block said only the page name: a crawler could not learn which files
    the core loads, their hashes, the source read or when.
    """
    data: dict = {}
    # Every repository the profile reads, each with its own pin: xemu's
    # libretro build lives in another repository at another commit, and a
    # port's upstream_commit was never published.
    about = []
    seen: set[str] = set()
    for field in ("source", "upstream"):
        urls = profile.get(field)
        pins = profile.get(f"{field}_commit")
        for mode, url in (urls.items() if isinstance(urls, dict) else [("", urls)]):
            if not url or not str(url).startswith("http") or str(url) in seen:
                continue
            seen.add(str(url))
            pin = pins.get(mode) if isinstance(pins, dict) else pins
            entry = {
                "@type": "SoftwareSourceCode",
                "name": str(profile.get("emulator", "")),
                "codeRepository": str(url),
                "description": " ".join(filter(None, [mode, field])),
            }
            if pin:
                entry["version"] = str(pin)
            about.append(entry)
    if about:
        data["about"] = about[0] if len(about) == 1 else about
    if profile.get("profiled_date"):
        data["dateModified"] = str(profile["profiled_date"])
    parts = []
    files = [f for f in profile.get("files") or [] if isinstance(f, dict)]
    for f, anchor in zip(files, emulator_file_anchors(files)):
        if not f.get("name"):
            continue
        # One identifier per accepted value: "sha256:a,b,c,d" matched no
        # exact search (mesence's BS-X.bin).
        identifiers = [
            f"{field}:{value}"
            for field in ("sha1", "sha256", "md5", "crc32")
            for value in _hash_values(f.get(field))
        ]
        part = {
            "@type": "CreativeWork",
            "name": str(f.get("path") or f["name"]),
            # The card's own anchor, as system and platform parts carry.
            "url": f"#{anchor}",
        }
        if identifiers:
            part["identifier"] = identifiers
        # An archive is identified by its members: MAME and FBNeo hash them,
        # never the ZIP, and 1 338 archive parts had no identifier at all.
        members = [
            {
                "@type": "CreativeWork",
                "name": str(member.get("name", "")),
                "identifier": [
                    f"{field}:{value}"
                    for field in ("sha1", "crc32")
                    for value in _hash_values(member.get(field))
                ],
            }
            for member in f.get("contents") or []
            if isinstance(member, dict) and member.get("name")
            and (member.get("sha1") or member.get("crc32"))
        ]
        if members:
            part["hasPart"] = members
        parts.append(part)
    if parts:
        data["hasPart"] = parts
    return data


# Working notes kept under docs/ during development, never part of the site,
# and no generated page lives in a directory of these names.
LOCAL_NOTE_DIRS = ("plans", "specs")


def decorate_markdown_pages(docs: Path) -> None:
    """Add per-page descriptions and machine-readable structured data."""
    for path in sorted(docs.rglob("*.md")):
        relative = path.relative_to(docs)
        if any(part in LOCAL_NOTE_DIRS for part in relative.parts[:-1]):
            continue
        markdown = path.read_text(encoding="utf-8")
        stripped = _undecorated(markdown)
        if stripped is not markdown:
            markdown = stripped
        elif markdown.startswith("---\n"):
            continue

        title = _page_title(markdown, relative)
        browser_title = _browser_title(relative, title)
        description = _page_description(markdown, relative, title)
        if relative.name == "index.md":
            page_path = "" if len(relative.parts) == 1 else "/".join(relative.parts[:-1]) + "/"
        else:
            page_path = relative.with_suffix("").as_posix() + "/"
        canonical = urllib.parse.urljoin(SITE_URL, page_path)
        schema_type = "WebSite" if relative.as_posix() == "index.md" else "TechArticle"
        if relative.as_posix() == "data.md":
            schema_type = "Dataset"
        structured = {
            "@context": "https://schema.org",
            "@type": schema_type,
            "name": title,
            "description": description,
            "url": canonical,
            "isPartOf": {
                "@type": "WebSite",
                "name": SITE_NAME,
                "url": SITE_URL,
            },
            **PAGE_DATA.get(relative.as_posix(), {}),
        }
        structured_json = json.dumps(
            structured, ensure_ascii=False, separators=(",", ":")
        ).replace("</", "<\\/")
        front_matter = (
            "---\n"
            "generated_by: retrobios-site\n"
            f"title: {json.dumps(browser_title, ensure_ascii=False)}\n"
            f"description: {json.dumps(description, ensure_ascii=False)}\n"
            + (f"date_modified: {json.dumps(str(structured['dateModified']))}\n"
               if structured.get("dateModified") else "")
            + "---\n\n"
            '<script type="application/ld+json">\n'
            f"{structured_json}\n"
            "</script>\n\n"
        )
        write_decorated(path, front_matter + markdown)


# Platform pages


def generate_platform_index(coverages: dict, registry: dict | None = None) -> str:
    total_present = sum(c["present"] for c in coverages.values())

    lines = [
        "# Platforms",
        "",
        f"{len(coverages)} supported platforms with "
        f"{total_present:,} verified files.",
        "",
        "| Platform | On its BIOS list | Files in the pack | Checked by | Status | Download |",
        "|----------|------:|------:|-----------|--------|----------|",
    ]

    mode_labels = {
        "md5": '<span class="rb-badge rb-badge-success">MD5</span>',
        "sha1": '<span class="rb-badge rb-badge-success">SHA1</span>',
        "existence": '<span class="rb-badge rb-badge-info">existence</span>',
    }

    archived_any = False
    for name, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        display = cov["platform"]

        mode_html = mode_labels.get(
            cov["mode"],
            f'<span class="rb-badge rb-badge-muted">{cov["mode"]}</span>',
        )
        status = (registry or {}).get(name, {}).get("status", "active")
        if status == "archived":
            archived_any = True
            status_html = '<span class="rb-badge rb-badge-muted">archived</span>'
        else:
            status_html = '<span class="rb-badge rb-badge-success">active</span>'

        lines.append(
            f"| [{display}]({name}.md) | "
            f"{cov['present']:,} | {_released_files(name)} | "
            f"{mode_html} | {status_html} | "
            f"[Pack]({RELEASE_URL}){{ .md-button .md-button--primary }} |"
        )

    lines.extend(
        [
            "",
            "The BIOS list is what the platform publishes. The pack adds the "
            "files its emulators load and the data directories they read, "
            "which is the count an extraction shows. "
            "Checked by is the test each platform runs on its own, replicated "
            "from its source code. "
            "[How each mode works](../wiki/verification-modes.md).",
        ]
    )

    if archived_any:
        lines.extend(
            [
                "",
                "An archived platform keeps its configuration and still gets a "
                "pack, but upstream is no longer scraped on a schedule.",
            ]
        )

    return "\n".join(lines) + "\n"


def _render_platform_systems(
    by_system: dict, config_files: dict, emulator_files: dict
) -> list[str]:
    """One collapsible block per system, listing every file it needs.

    Split out of generate_platform_page: the block is most of that
    function's branching, since each file renders a different set of
    hashes, sizes and provenance depending on what the platform declares.
    """
    lines: list[str] = []
    # Per-system detail sections (collapsible for large platforms)
    use_collapsible = len(by_system) > 10

    for sys_id, files in sorted(by_system.items()):
        ok_count = sum(1 for f in files if f["status"] == "ok")
        total = len(files)

        sys_emus = _system_emulators(sys_id, emulator_files)

        sys_link = _system_link(sys_id, "../")

        anchor = sys_id.replace(" ", "-")
        if use_collapsible:
            status_tag = "OK" if ok_count == total else _count(total - ok_count, "issue")
            lines.append(f'<a id="{anchor}"></a>')
            lines.append(f'??? note "{sys_id} ({ok_count}/{total} - {status_tag})"')
            lines.append("")
            pad = "    "
        else:
            # The same anchor in both layouts: emulator cards link here.
            lines.append(f'<a id="{anchor}"></a>')
            lines.append(f"## {sys_link}")
            lines.append("")
            pad = ""

        lines.append(f"{pad}{ok_count}/{total} files verified")
        if sys_emus:
            emu_links = ", ".join(_emulator_link(e, "../") for e in sorted(sys_emus))
            lines.append(f"{pad}Emulators: {emu_links}")
        lines.append("")

        # File listing. The sort is stable, so same-named entries keep the
        # order verify reported them in, which is the declaration order.
        seen_names: dict[str, int] = {}
        for f in sorted(files, key=lambda x: x["name"]):
            status = f["status"]
            fname = f["name"]
            declared = config_files.get((sys_id, fname), [])
            nth = seen_names.get(fname, 0)
            seen_names[fname] = nth + 1
            cfg_entry = declared[nth] if nth < len(declared) else {}
            sha1 = cfg_entry.get("sha1", f.get("sha1", ""))
            md5 = cfg_entry.get("md5", f.get("expected_md5", ""))
            size = cfg_entry.get("size", f.get("size", 0))

            if status == "ok":
                status_display = "OK"
            elif status == "untested":
                reason = f.get("reason", "")
                status_display = f"untested: {reason}" if reason else "untested"
            elif status == "missing":
                status_display = "**missing**"
            else:
                status_display = status

            size_str = _fmt_size(size) if size else ""
            details = [status_display]
            if size_str:
                details.append(size_str)

            # Where the file goes, when its name alone does not say: three
            # GameCube IPL.bin differ only by GC/USA, GC/EUR and GC/JAP.
            destination = str(cfg_entry.get("destination") or "")
            where = f" at `{destination}`" if destination and destination != fname else ""
            lines.append(f"{pad}- `{fname}`{where} - {', '.join(details)}")
            # Every hash the platform declares, whole: the crc32 is what
            # System.dat and clrmamepro print, and pasting it found nothing
            # for a pinned file the collection holds under other bytes.
            crc32 = str(cfg_entry.get("crc32") or "")
            # RetroDECK declares sha256 too: dsifirmware.bin's was in the CSV
            # exports only, and pasting it into the search found nothing.
            sha256 = str(cfg_entry.get("sha256") or "")
            if sha1 or md5 or crc32 or sha256:
                hash_parts = []
                # One code span per value: Recalbox lists accepted md5s joined
                # by commas, and the search indexed the joined run as a single
                # word, so the second and later values were never found.
                if sha1:
                    hash_parts.append(f"SHA1: {_hash_spans(sha1)}")
                if md5:
                    hash_parts.append(f"MD5: {_hash_spans(md5)}")
                if crc32:
                    hash_parts.append(f"CRC32: `{crc32}`")
                if sha256:
                    hash_parts.append(f"SHA256: `{sha256}`")
                lines.append(f"{pad}    {' | '.join(hash_parts)}")

        lines.append("")

    return lines


def generate_platform_page(
    name: str,
    cov: dict,
    registry: dict | None = None,
    emulator_files: dict | None = None,
) -> str:
    config = cov["config"]
    display = cov["platform"]
    mode = cov["mode"]
    pct = _pct(cov["present"], cov["total"])

    logo_url = (registry or {}).get(name, {}).get("logo", "")
    logo_md = (
        f"![{display}]({logo_url}){{ width=48 align=right }}\n\n" if logo_url else ""
    )

    homepage = config.get("homepage", "")
    version = config.get("version", "")
    hash_type = config.get("hash_type", "")
    base_dest = config.get("base_destination", "")

    mode_badge = (
        "rb-badge-success" if reads_file_contents(mode) else "rb-badge-info"
    )

    lines = [
        f"# {display}",
        "",
        logo_md,
    ]

    if (registry or {}).get(name, {}).get("status") == "archived":
        lines.extend(
            [
                '!!! warning "Archived platform"',
                "",
                "    The configuration is kept and packs are still built, but "
                "upstream is no longer scraped on a schedule, so the file list "
                "reflects the last sync rather than today's upstream.",
                "",
            ]
        )

    lines.extend(
        [
            '<div class="rb-stats" markdown>',
            "",
            '<div class="rb-stat" markdown>',
            f'<span class="rb-stat-value">{cov["present"]}/{cov["total"]}</span>',
            f'<span class="rb-stat-label">Coverage ({pct})</span>',
            "</div>",
            "",
            '<div class="rb-stat" markdown>',
            f'<span class="rb-stat-value">{cov["verified"]}</span>',
            '<span class="rb-stat-label">Verified</span>',
            "</div>",
            "",
            '<div class="rb-stat" markdown>',
            f'<span class="rb-stat-value">{cov["missing"]}</span>',
            '<span class="rb-stat-label">Missing</span>',
            "</div>",
            "",
            '<div class="rb-stat" markdown>',
            f'<span class="rb-stat-value">'
            f'<span class="rb-badge {mode_badge}">{mode}</span></span>',
            '<span class="rb-stat-label">Checked by</span>',
            "</div>",
            "",
            "</div>",
            "",
            "| | |",
            "|---|---|",
        ]
    )
    if hash_type:
        lines.append(f"| Hash type | {hash_type} |")
    if version:
        lines.append(f"| Version | {version} |")
    if base_dest:
        lines.append(f"| BIOS path | `{base_dest}/` |")
    if homepage:
        lines.append(f"| Homepage | [{homepage}]({homepage}) |")
    transcribed = str(config.get("source") or "")
    if transcribed:
        # The file every declaration on this page was read from: without it
        # a hash could not be traced to the revision that states it.
        moving = re.search(r"/(?:master|main|db)(?:/|$)|refs/heads/", transcribed) or (
            re.fullmatch(r"https://(?:github|gitlab)\.com/[^/]+/[^/]+/?", transcribed)
        )
        note = " (a branch: the revision read is not recorded)" if moving else ""
        lines.append(f"| Transcribed from | <{transcribed}>{note} |")
    contrib_list = (registry or {}).get(name, {}).get("contributed_by", [])
    if contrib_list:
        for cb in contrib_list:
            username = cb.get("username", "")
            contribution = cb.get("contribution", "")
            pr = cb.get("pr")
            pr_link = f" ([#{pr}]({REPO_URL}/pull/{pr}))" if pr else ""
            lines.append(
                f"| Contributed by | [@{username}](https://github.com/{username})"
                f" - {contribution}{pr_link} |"
            )
    lines.extend(
        [
            "",
            f"[Download {display} Pack]({RELEASE_URL})"
            "{ .md-button .md-button--primary }",
            "",
        ]
    )

    # The declaration behind each detail, by system and name in declaration
    # order: keyed by name alone, the last of MiSTer's 23 boot.rom gave its
    # MD5 to every system's row.
    config_files: dict[tuple[str, str], list[dict]] = {}
    for sys_id, system in config.get("systems", {}).items():
        for fe in system.get("files", []):
            fname = fe.get("name", "")
            if fname:
                config_files.setdefault((sys_id, fname), []).append(fe)

    # Group details by system
    by_system: dict[str, list] = {}
    for d in cov["details"]:
        sys_id = d.get("system", "unknown")
        by_system.setdefault(sys_id, []).append(d)

    # System summary table (quick navigation)
    lines.extend(
        [
            "## Systems overview",
            "",
            "| System | Files | Status | Emulators |",
            "|--------|-------|--------|-----------|",
        ]
    )
    for sys_id, files in sorted(by_system.items()):
        ok_count = sum(1 for f in files if f["status"] == "ok")
        total = len(files)
        non_ok = total - ok_count
        if non_ok == 0:
            status = '<span class="rb-badge rb-badge-success">OK</span>'
        else:
            status = (
                f'<span class="rb-badge rb-badge-warning">'
                f'{non_ok} issue{"s" if non_ok > 1 else ""}</span>'
            )
        sys_emus = _system_emulators(sys_id, emulator_files)
        emu_str = ", ".join(sys_emus[:3])
        if len(sys_emus) > 3:
            emu_str += f" +{len(sys_emus) - 3}"
        anchor = sys_id.replace(" ", "-")
        lines.append(
            f"| [{sys_id}](#{anchor}) | {ok_count}/{total} | {status} | {emu_str} |"
        )
    lines.append("")

    lines.extend(
        _render_platform_systems(by_system, config_files, emulator_files)
    )

    lines.append(f"*Generated on {_timestamp()}*")
    return "\n".join(lines) + "\n"


# System pages


def generate_files_page() -> str:
    """The content address of every held file: files/?sha1=<sha1>.

    site.js reads api/v1/file-locations.json and goes to the card. Without
    a sha1, or with one the collection does not hold, the page says so.
    """
    return "\n".join([
        "# Find a file by its SHA1",
        "",
        "A file's card lives on its manufacturer's page, and a file refiled "
        "between manufacturers moves with it. `files/?sha1=<sha1>` is the "
        "address that does not move: it opens the card wherever it is now. "
        "Each card's **permalink** is that address.",
        "",
        '<form class="rb-file-lookup" action="./" method="get">',
        '<label for="rb-sha1">SHA1</label> ',
        '<input id="rb-sha1" name="sha1" size="44" pattern="[0-9a-fA-F]{40}" required> ',
        '<button type="submit">Open</button>',
        "</form>",
        "",
        '<p id="rb-file-lookup-status" role="status"></p>',
        "",
        "The index behind it is [`api/v1/file-locations.json`](../api/v1/file-locations.json).",
        "",
    ])


_REPO_ROOT = Path(__file__).resolve().parent.parent


def data_directory_members(install_dir: Path = _REPO_ROOT / "install") -> dict[str, dict[str, dict]]:
    """Each data directory's files with their sha1 and size, as the install
    manifests record them for the installer to check."""
    members: dict[str, dict[str, dict]] = {}
    for path in sorted(install_dir.glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for tree in manifest.get("data_directories") or []:
            members.setdefault(str(tree.get("ref", "")), {}).update(tree.get("files") or {})
    return members


def data_directory_revisions(data_dir: Path = _REPO_ROOT / "data") -> dict[str, dict]:
    """The revision each data directory was fetched at, as refresh_data_dirs records it."""
    path = data_dir / ".versions.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _data_member_owners(profiles: dict) -> dict[str, str]:
    """The emulator page that lists each data directory, the first by name."""
    owners: dict[str, str] = {}
    for name in sorted(profiles):
        for tree in profiles[name].get("data_directories") or []:
            owners.setdefault(str(tree.get("ref", "")), name)
    return owners


def file_location_items(
    db: dict, profiles: dict | None = None, members: dict | None = None
) -> list[dict]:
    """Each held file's card, keyed by its sha1.

    A card lives on its manufacturer's page, and a file refiled between
    manufacturers (mt32_control.rom, Commodore to Roland) moved its link
    with it. files/?sha1= reads this list and goes to the card wherever it
    is now: the address cited depends on the content only.
    """
    items = []
    for sha1, entry in sorted(db.get("files", {}).items()):
        parts = entry.get("path", "").split("/")
        if len(parts) < 3 or parts[0] != "bios":
            continue
        slug = parts[1].lower().replace(" ", "-")
        items.append({"sha1": sha1, "url": f"systems/{slug}/#sha1-{sha1[:12]}"})
    # A data directory's files are listed on the page of an emulator reading
    # the tree: 2,534 hashes the packs ship were found nowhere on the site.
    owners = _data_member_owners(profiles or {})
    known = {item["sha1"] for item in items}
    for ref, files in sorted((members or {}).items()):
        if ref not in owners:
            continue
        for _member, info in sorted(files.items()):
            sha1 = str(info.get("sha1", ""))
            if sha1 and sha1 not in known:
                known.add(sha1)
                items.append({"sha1": sha1,
                              "url": f"emulators/{owners[ref]}/#sha1-{sha1[:12]}"})
    return sorted(items, key=lambda item: item["sha1"])


def _group_by_manufacturer(db: dict) -> dict[str, dict[str, list]]:
    """Group files by manufacturer -> console -> files.

    The database keeps one path per content, and a copy filed elsewhere was
    on no page of its own: the Channel F BIOS sat under Arcade > FBNeo only,
    and Fairchild had no system page. Every folder holding a copy lists it,
    read from the full paths by_path_suffix records.
    """
    files = db.get("files", {})
    bios_dir = Path(str(db.get("bios_dir") or "bios"))
    places: dict[str, set[tuple[str, str]]] = {}
    for sha1, entry in files.items():
        parts = str(entry.get("path", "")).split("/")
        if len(parts) >= 3 and parts[0] == "bios":
            places.setdefault(sha1, set()).add((parts[1], parts[2]))
    for tail, sha1s in db.get("indexes", {}).get("by_path_suffix", {}).items():
        parts = tail.split("/")
        if len(parts) < 3 or parts[1].startswith(".") or len(sha1s) != 1:
            continue
        if sha1s[0] in files and (bios_dir / tail).is_file():
            places.setdefault(sha1s[0], set()).add((parts[0], parts[1]))
    manufacturers: dict[str, dict[str, list]] = {}
    for sha1, where in places.items():
        for manufacturer, console in sorted(where):
            manufacturers.setdefault(manufacturer, {}).setdefault(console, []).append(files[sha1])
    return manufacturers


def generate_systems_index(manufacturers: dict) -> str:
    total_mfr = len(manufacturers)
    total_consoles = sum(len(c) for c in manufacturers.values())
    # A copy filed under two folders appears on both pages, counted once here.
    total_files = len({
        f.get("sha1") for consoles in manufacturers.values()
        for files in consoles.values() for f in files
    })

    lines = [
        "# Systems",
        "",
        f"{total_mfr} manufacturers, {total_consoles} consoles, "
        f"{total_files:,} files in the repository.",
        "",
        "| Manufacturer | Consoles | Files |",
        "|-------------|----------|-------|",
    ]

    for mfr in sorted(manufacturers.keys()):
        consoles = manufacturers[mfr]
        file_count = sum(len(files) for files in consoles.values())
        slug = mfr.lower().replace(" ", "-")
        lines.append(f"| [{mfr}]({slug}.md) | {len(consoles)} | {file_count} |")

    return "\n".join(lines) + "\n"


_PROVENANCE_LABELS = {"redump": "Redump", "no-intro": "No-Intro", "tosec": "TOSEC"}

_PROVENANCE_HOMES = {
    "redump": "http://redump.org/",
    "no-intro": "https://no-intro.org/",
    "tosec": "https://www.tosecdev.org/",
}


def _prov_title(data: dict) -> str:
    """Tooltip text for a provenance badge."""
    parts = [data.get("dat", ""), data.get("description", "")]
    return ": ".join(p for p in parts if p).replace('"', "&quot;")


# TOSEC dump flags that say the dump is not a clean copy of the chip
# (TOSEC Naming Convention, "Dump info flags"); [a] alternate and [!]
# verified good are not among them.
_TOSEC_BAD_FLAGS = {
    "b": "bad dump", "o": "overdump", "h": "hacked", "cr": "cracked",
    "f": "fixed", "t": "trained", "p": "pirate",
}
_TOSEC_FLAG_RE = re.compile(r"\[(b|o|h|cr|f|t|p)\d*(?:\s[^\]]*)?\]")


def _dump_flags(source: str, data: dict) -> list[str]:
    """What a catalog's name says is wrong with the dump, as words."""
    if source != "tosec":
        return []
    name = str(data.get("name") or data.get("description") or "")
    seen: list[str] = []
    for flag in _TOSEC_FLAG_RE.findall(name):
        word = _TOSEC_BAD_FLAGS[flag]
        if word not in seen:
            seen.append(word)
    return seen


def _provenance_lines(provenance: dict, prefix: str) -> list[str]:
    """One visible line per catalog: its badge and the entry's own name.

    The name was only a title attribute, unreadable on a touch screen and
    absent from the search for 793 of 803 provenances, and a TOSEC entry
    flagged [b] or [o] wore the same green badge as a clean dump.
    """
    lines: list[str] = []
    for source, data in sorted(provenance.items()):
        label = _PROVENANCE_LABELS.get(source, source)
        flags = _dump_flags(source, data)
        kind = "rb-badge-warning" if flags else "rb-badge-success"
        badge = f'<span class="rb-badge {kind}">[{label}]({prefix}provenance.md#{source})</span>'
        name = str(data.get("name") or data.get("description") or "")
        line = f"- Catalogued: {badge} {_code(name)}" if name else f"- Catalogued: {badge}"
        if flags:
            line += f" ({', '.join(flags)}, as {label} records it)"
        lines.append(line)
    return lines


def load_derivable(path: str = "derivable.json") -> dict[str, dict]:
    """Missing sha1 -> how the collection produces it, as the pipeline found.

    Empty when the pipeline has not recorded it: the page then lists every
    missing dump, as it did before the record existed.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


_ROMSET_SOURCES = {"mame": "MAME", "fbneo": "FBNeo"}


def generate_provenance_page(
    db: dict, report: dict, derivable: dict[str, dict] | None = None
) -> str:
    """Page explaining the verified dump badges and listing catalog gaps."""
    matched_files = count_catalog_matched(db)
    total_files = compute_composition(db)["systems"]["files"]

    lines = [
        "# Dump provenance",
        "",
        f"**{matched_files:,}** of {total_files:,} system files match an entry "
        "in a dump-preservation catalog. Those files carry a "
        '<span class="rb-badge rb-badge-success">catalog</span> badge and the '
        "catalog entry's own name on the [system pages](systems/index.md), "
        'orange (<span class="rb-badge rb-badge-warning">TOSEC</span>) where '
        "TOSEC flags the dump as bad, overdumped, hacked, cracked or fixed. "
        "Arcade ROM sets and engine data "
        "sit outside what those catalogs index, so they are left out of the "
        "ratio rather than counted as failures.",
        "",
        "## What the badge means",
        "",
        "The badge says the file is byte-identical to a dump catalogued by "
        "No-Intro, Redump, or TOSEC. Matching is done on SHA1, falling back to "
        "MD5 plus size for older catalog entries that predate SHA1. Filenames "
        "are never used: the same dump is `fdsbios.nes` here, "
        "`[BIOS] Family Computer Disk System (Japan) (En) (Rev 1).bin` at "
        "No-Intro, and "
        "`Nintendo Famicom Disk System BIOS (198x)(Nintendo)(JP)(en).bin` at "
        "TOSEC.",
        "",
        "## What it does not mean",
        "",
        "A file without a badge is not inferior and works exactly the same. "
        "Emulator behaviour is decided by [verification]"
        "(wiki/verification-modes.md), which reads the emulator source code, "
        "not by catalog membership. Plenty of files this project ships are "
        "outside any catalog by nature: composites a core assembles for itself "
        "(the MiSTer X68000 `boot.rom` is `cgrom.dat` joined to `iplrom.dat`), "
        "modern console firmware updates, game data such as `prboom.wad`, and "
        "arcade sets tracked by MAME driver source instead.",
        "",
        "When the two views disagree, this project follows the code, because "
        "that is what decides whether your emulator boots. The reasoning is in "
        "the [FAQ](wiki/faq.md#are-these-files-verified-against-original-"
        "hardware-dumps).",
        "",
        "## Coverage",
        "",
        "| Catalog | In collection | Covered DATs | Snapshot |",
        "|---------|--------------|-------------|----------|",
    ]

    for source, data in report.items():
        label = _PROVENANCE_LABELS.get(source, source)
        home = _PROVENANCE_HOMES.get(source, "")
        in_scope = data["matched"] + len(data["missing"])
        pct = 100 * data["matched"] / in_scope if in_scope else 0
        name = f"[{label}]({home})" if home else label
        lines.append(
            f"| {name} | {data['matched']:,}/{in_scope:,} ({pct:.0f}%) | "
            f"{len(data['covered_dats'])} | {data['imported_at']} |"
        )

    lines.extend(
        [
            "",
            "Coverage counts only DATs the collection already reaches: a DAT "
            "counts as covered once at least one of its entries is held. "
            "No-Intro tags every non-game dump `[BIOS]`, including digital "
            "title distribution such as the Wii U and 3DS CDN catalogues, "
            "which this project does not ship.",
            "",
        ]
    )

    out_of_scope = {s: d["out_of_scope"] for s, d in report.items() if d["out_of_scope"]}
    if out_of_scope:
        detail = ", ".join(
            f"{_PROVENANCE_LABELS.get(s, s)} {c:,}" for s, c in sorted(out_of_scope.items())
        )
        lines.extend(
            [
                f"Entries in DATs the collection does not cover at all are "
                f"excluded from those numbers ({detail}).",
                "",
            ]
        )

    # A dump the collection already produces (the odd bytes of a Kickstart,
    # the halves of a character ROM) is no acquisition target: the page asked
    # the public for 79 of them.
    derivable = derivable or {}
    derived_entries: dict[str, dict] = {}
    for data in report.values():
        kept = []
        for entry in data["missing"]:
            sha1 = (entry.get("sha1") or "").lower()
            if sha1 in derivable:
                derived_entries.setdefault(sha1, entry)
            else:
                kept.append(entry)
        data["missing"] = kept
    total_missing = distinct_missing(report)
    lines.extend(
        [
            "## Wanted: catalogued dumps this project does not have",
            "",
            f"The tables below list **{total_missing:,}** dumps a catalog "
            "describes and the collection lacks. Nothing here ships in any "
            "pack: it is an acquisition list, published with hashes so anyone "
            "can check a personal collection against it. A contribution "
            "matching one of these hashes is welcome: see "
            "[Contributing](contributing.md).",
            "",
        ]
    )

    for source, data in report.items():
        if not data["missing"]:
            continue
        label = _PROVENANCE_LABELS.get(source, source)
        by_dat: dict[str, list] = {}
        for entry in data["missing"]:
            for dat in entry.get("dats") or [entry.get("dat", "")]:
                by_dat.setdefault(dat, []).append(entry)
        lines.extend([f"### {label}", ""])
        for dat in sorted(by_dat):
            entries = by_dat[dat]
            lines.extend([f'??? note "{dat} ({len(entries)})"', ""])
            lines.append("    | Name | Description | SHA1 |")
            lines.append("    |------|-------------|------|")
            for entry in sorted(entries, key=lambda e: e["name"]):
                sha1 = entry.get("sha1") or "-"
                # A pipe in catalog text would split the markdown table row
                name = entry["name"].replace("|", "-")
                desc = (entry.get("description") or "").replace("|", "-")
                # Where an emulator's romset already carries the dump.
                for hit in entry.get("romsets", []):
                    desc += (
                        f" Ships in {_ROMSET_SOURCES.get(hit['source'], hit['source'])} "
                        f"`{hit['archive']}` as `{hit['member']}`."
                    )
                lines.append(f"    | `{name}` | {desc} | `{sha1}` |")
            lines.append("")

    if derived_entries:
        lines.extend(
            [
                "## Derivable: dumps the collection already produces",
                "",
                f"**{len(derived_entries):,}** catalogued dumps are not held as "
                "files but come out of one that is: a byte or word split, a "
                "half, a swap, or two chips joined. They are not wanted.",
                "",
                "| Name | Comes from | How | SHA1 |",
                "|------|------------|-----|------|",
            ]
        )
        for sha1, entry in sorted(derived_entries.items(), key=lambda kv: kv[1]["name"]):
            how = derivable[sha1]
            sources = " + ".join(f"`{path}`" for path in how.get("from", []))
            name = entry["name"].replace("|", "-")
            lines.append(f"| `{name}` | {sources} | {how.get('transform', '')} | `{sha1}` |")
        lines.append("")

    lines.append(f'<div class="rb-timestamp">Generated on {_timestamp()}.</div>')
    return "\n".join(lines) + "\n"


_RELEASE_ASSET_NAMES: dict[str, str] | None = None


def _fetch_url(path: str) -> str:
    """Where a collection file downloads from: its raw URL, or the
    large-files release asset when .gitignore keeps it out of git."""
    global _RELEASE_ASSET_NAMES
    if _RELEASE_ASSET_NAMES is None:
        from largefiles import (  # noqa: PLC0415
            LARGE_FILES_RELEASE,
            asset_names,
            load_registered_paths,
            published_asset_name,
        )

        _RELEASE_ASSET_NAMES = {
            registered: f"{REPO_URL}/releases/download/{LARGE_FILES_RELEASE}/"
            + urllib.parse.quote(published_asset_name(name))
            for registered, name in asset_names(load_registered_paths()).items()
        }
    return _RELEASE_ASSET_NAMES.get(path) or (
        "https://raw.githubusercontent.com/Abdess/retrobios/main/"
        + urllib.parse.quote(path)
    )


def _platform_links(sha1: str, consumers: FileConsumers) -> list[str]:
    """Each platform declaring the file: a link to its system section, and
    the destination it gives the file."""
    systems_of = getattr(consumers, "platform_systems", {}).get(sha1, {})
    destinations_of = getattr(consumers, "platform_destinations", {}).get(sha1, {})
    links: list[str] = []
    for platform in sorted(consumers.platforms.get(sha1, ())):
        section = sorted(systems_of.get(platform, ()))
        fragment = f"#{section[0].replace(' ', '-')}" if section else ""
        link = f"[{platform}](../platforms/{platform}.md{fragment})"
        destinations = sorted(destinations_of.get(platform, ()))
        if destinations:
            link += " " + ", ".join(_code(d) for d in destinations)
        links.append(link)
    return links


def _installed_links(sha1: str, consumers: FileConsumers, declared: set[str]) -> list[str]:
    """Each pack installing the file that its platform's list does not name,
    with the place the pack gives it."""
    links = []
    for platform, dests in sorted(getattr(consumers, "installed", {}).get(sha1, {}).items()):
        if platform in declared:
            continue
        where = ", ".join(_code(d) for d in sorted(dests) if d)
        links.append(f"[{platform}](../platforms/{platform}.md)" + (f" {where}" if where else ""))
    return links


def _system_file_card(f: dict, consumers: FileConsumers) -> list[str]:
    """One file of a system page: hashes, who reads it, its provenance."""
    name = f["name"]
    sha1_full = f.get("sha1", "unknown")
    md5_full = f.get("md5", "unknown")
    size = _fmt_size(f.get("size", 0))
    # Who reads this file, not who reads a file of this name
    plats = sorted(consumers.platforms.get(sha1_full, ()))
    emus = sorted(consumers.emulators.get(sha1_full, ()))

    anchor = f"sha1-{sha1_full[:12]}" if sha1_full != "unknown" else ""
    id_attr = f' id="{anchor}"' if anchor else ""
    lines = [f'<div class="rb-sys-file"{id_attr} markdown>', ""]
    # The anchor is this page's, the second link is the file's own address,
    # resolved by content wherever its card moves.
    permalink = (
        f' <a class="rb-permalink" href="#{anchor}" title="Link to this card">#</a>'
        f' <a class="rb-permalink" href="../../files/?sha1={sha1_full}" '
        f'title="Permanent link, by content">permalink</a>'
    ) if anchor else ""
    lines.append(f'**`{name}`** <span class="rb-badge rb-badge-muted">{size}</span>{permalink}')
    lines.append("")
    if f.get("path"):
        # The card named a file, its hashes and provenance, but not where to
        # get it: a user missing scph5501.bin found the reference and no way
        # to the bytes.
        lines.append(f"- Collection path: {_code(f['path'])} ([download]({_fetch_url(f['path'])}))")
    # Every hash in full: the site search indexes text, not a title
    # attribute, so a pasted md5 or crc32 (what clrmamepro, RomVault or an
    # emulator log prints) found nothing behind a 12-character prefix.
    for label, value in (("SHA1", sha1_full), ("MD5", md5_full),
                         ("CRC32", f.get("crc32")), ("SHA256", f.get("sha256"))):
        if value and value != "unknown":
            lines.append(f'- {label}: <span class="rb-hash">{_code(value)}</span>')
    if plats:
        # Each link lands on the platform's system section, and says where
        # that platform puts the file: from the card, retrodeck sent the
        # reader to the top of a 2,000-file page.
        lines.append(f"- Platforms: {', '.join(_platform_links(sha1_full, consumers))}")
    installed = _installed_links(sha1_full, consumers, set(plats))
    if installed:
        # The packs that carry the file for their cores: a RetroBat user
        # looking up scph5501.bin found every platform but RetroBat, whose
        # pack installs it for six cores.
        lines.append(f"- Also installed by: {', '.join(installed)}")
    if emus:
        lines.append(f"- Emulators: {', '.join(_emulator_link(e, '../') for e in emus)}")
    provenance = f.get("provenance", {})
    if provenance:
        lines.extend(_provenance_lines(provenance, "../"))
    lines.extend(["", "</div>", ""])
    return lines


def generate_system_page(
    manufacturer: str,
    consoles: dict[str, list],
    consumers: FileConsumers,
) -> str:
    manufacturer.lower().replace(" ", "-")
    lines = [
        f"# {manufacturer}",
        "",
    ]

    for console_name in sorted(consoles.keys()):
        files = consoles[console_name]
        icon_md = system_icon_markdown(manufacturer, console_name)
        lines.append(f"## {icon_md}{console_name}")
        lines.append("")
        # Separate main files from variants
        main_files = [f for f in files if "/.variants/" not in f["path"]]
        variant_files = [f for f in files if "/.variants/" in f["path"]]

        for f in sorted(main_files, key=lambda x: x["name"]):
            lines.extend(_system_file_card(f, consumers))

        if variant_files:
            # A variant gets the same card as a primary: the revision a
            # platform pins is the one a reader comes to find, and showed as
            # a name and a truncated MD5, nothing said who pins it.
            lines.append(f'??? note "Variants ({len(variant_files)})"')
            lines.append("")
            for v in sorted(variant_files, key=lambda x: (x["name"], x["path"])):
                lines.extend(
                    f"    {line}" if line else ""
                    for line in _system_file_card(v, consumers)
                )
            lines.append("")

        lines.append("")

    lines.append(f'<div class="rb-timestamp">Generated on {_timestamp()}.</div>')
    return "\n".join(lines) + "\n"


# Emulator pages


def generate_emulators_index(profiles: dict) -> str:
    unique = {
        k: v for k, v in profiles.items() if v.get("type") not in ("alias", "test")
    }
    aliases = {k: v for k, v in profiles.items() if v.get("type") == "alias"}

    # Group by classification
    by_class: dict[str, list[tuple[str, dict]]] = {}
    for name in sorted(unique.keys()):
        p = unique[name]
        cls = p.get("core_classification", "other")
        by_class.setdefault(cls, []).append((name, p))

    total_files = sum(len(p.get("files", [])) for p in unique.values())

    lines = [
        "# Emulators",
        "",
        f"**{len(unique)}** emulator profiles, **{total_files}** files total, **{len(aliases)}** aliases.",
        "",
        "| Classification | Count | Description |",
        "|---------------|-------|-------------|",
    ]

    cls_desc = {
        "official_port": "Same author maintains both standalone and libretro",
        "community_fork": "Third-party port to libretro",
        "pure_libretro": "Built for libretro, no standalone version",
        "game_engine": "Game engine reimplementation",
        "enhanced_fork": "Fork with added features",
        "frozen_snapshot": "Frozen at an old version",
        "embedded_hle": "All ROMs compiled into binary",
        "launcher": "Launches an external emulator",
        "other": "Unclassified",
    }

    cls_order = [
        "official_port",
        "community_fork",
        "pure_libretro",
        "game_engine",
        "enhanced_fork",
        "frozen_snapshot",
        "embedded_hle",
        "launcher",
        "other",
    ]

    for cls in cls_order:
        entries = by_class.get(cls, [])
        if not entries:
            continue
        label = CLS_LABELS.get(cls, cls)
        desc = cls_desc.get(cls, "")
        lines.append(f"| [{label}](#{cls}) | {len(entries)} | {desc} |")
    lines.append("")

    for cls in cls_order:
        entries = by_class.get(cls, [])
        if not entries:
            continue
        label = CLS_LABELS.get(cls, cls)
        desc = cls_desc.get(cls, "")
        lines.extend(
            [
                f"## {label} {{ #{cls} }}",
                "",
                f"*{desc}* -- {len(entries)} profiles",
                "",
                "| Engine | Systems | Files |",
                "|--------|---------|-------|",
            ]
        )

        for name, p in entries:
            emu_name = p.get("emulator", name)
            systems = p.get("systems", [])
            files = p.get("files", [])
            sys_str = ", ".join(systems[:3])
            if len(systems) > 3:
                sys_str += f" +{len(systems) - 3}"
            file_count = len(files)
            file_str = str(file_count) if file_count else "-"
            lines.append(f"| [{emu_name}]({name}.md) | {sys_str} | {file_str} |")
        lines.append("")

    if aliases:
        lines.extend(["## Aliases", ""])
        lines.append("| Core | Points to |")
        lines.append("|------|-----------|")
        for name in sorted(aliases.keys()):
            parent = aliases[name].get(
                "alias_of", aliases[name].get("bios_identical_to", "unknown")
            )
            lines.append(f"| {name} | [{parent}]({parent}.md) |")
        lines.append("")

    return "\n".join(lines) + "\n"


def _file_badges(f: dict, in_repo: bool) -> list[str]:
    """The status chips shown beside a file name.

    Fourteen independent optional fields, each contributing at most one
    chip. Kept apart from the rest of the row so the field-by-field
    rendering stays readable.
    """
    required = f.get("required", False)
    hle = f.get("hle_fallback", False)
    mode = f.get("mode", "")
    category = f.get("category", "")
    region = f.get("region", "")
    storage = f.get("storage", "")
    bundled = f.get("bundled", False)
    embedded = f.get("embedded", False)
    has_builtin = f.get("has_builtin", False)
    archive = f.get("archive", "")
    ftype = f.get("type", "")
    badges = []
    if required:
        badges.append(
            '<span class="rb-badge rb-badge-danger">required</span>'
        )
    else:
        badges.append(
            '<span class="rb-badge rb-badge-muted">optional</span>'
        )
    if in_repo:
        badges.append(
            '<span class="rb-badge rb-badge-success">in repo</span>'
        )
    elif f.get("unsourceable"):
        badges.append(
            '<span class="rb-badge rb-badge-muted">unsourceable</span>'
        )
    else:
        badges.append(
            '<span class="rb-badge rb-badge-warning">missing</span>'
        )
    if hle:
        badges.append(
            '<span class="rb-badge rb-badge-info">HLE fallback</span>'
        )
    if mode:
        badges.append(
            f'<span class="rb-badge rb-badge-muted">{mode}</span>'
        )
    if category and category != "bios":
        badges.append(
            f'<span class="rb-badge rb-badge-info">{category}</span>'
        )
    if region:
        region_str = (
            ", ".join(region) if isinstance(region, list) else str(region)
        )
        badges.append(
            f'<span class="rb-badge rb-badge-muted">{region_str}</span>'
        )
    if storage and storage != "embedded":
        badges.append(
            f'<span class="rb-badge rb-badge-muted">{storage}</span>'
        )
    if bundled:
        badges.append(
            '<span class="rb-badge rb-badge-muted">bundled</span>'
        )
    if embedded:
        badges.append(
            '<span class="rb-badge rb-badge-muted">embedded</span>'
        )
    if has_builtin:
        badges.append(
            '<span class="rb-badge rb-badge-info">built-in fallback</span>'
        )
    if archive:
        badges.append(
            f'<span class="rb-badge rb-badge-muted">in {archive}</span>'
        )
    if ftype and ftype != "bios":
        badges.append(
            f'<span class="rb-badge rb-badge-muted">{ftype}</span>'
        )

    return badges


def _first_hash_value(entry: dict) -> str:
    """The first declared hash of an entry, lowercase, for a stable anchor."""
    for field in ("sha1", "md5", "crc32", "sha256"):
        value = entry.get(field)
        value = value[0] if isinstance(value, list) and value else value
        if value:
            return str(value).lower().split(",")[0].strip().removeprefix("0x")
    return ""


def _render_emulator_file(
    f: dict,
    profile: dict,
    consumers: FileConsumers | None,
    files: list,
    _file_available: Callable[[dict], bool],
    anchor: str = "",
    owner: str = "",
) -> list[str]:
    """Render one row of an emulator profile's file table.

    `anchor` is the card's id on the page, so one file can be cited by URL.

    Split out of generate_emulator_page, where it was a 228-line loop body
    carrying most of that function's branching: thirty-odd optional fields,
    each with its own badge, hash line or note.
    """
    lines: list[str] = []
    fname = f.get("name", "")
    in_repo = _file_available(f)
    source_ref = f.get("source_ref", "")
    aliases = f.get("aliases", [])
    validation = f.get("validation", [])
    size = f.get("size")
    fnote = f.get("note", f.get("notes", ""))
    fmd5 = f.get("md5", "")
    fsha1 = f.get("sha1", "")
    fcrc32 = f.get("crc32", "")
    fsha256 = f.get("sha256", "")
    fadler32 = f.get("known_hash_adler32", "")
    fmin = f.get("min_size")
    fmax = f.get("max_size")
    desc = f.get("description", "")
    fpath = f.get("path", "")
    fsystem = f.get("system", "")
    priority = f.get("priority")
    fast_boot = f.get("fast_boot")
    contents = f.get("contents", [])
    config_key = f.get("config_key", "")
    dest = f.get("dest", f.get("destination", ""))
    fpattern = f.get("pattern", "")
    region_check = f.get("region_check")
    size_note = f.get("size_note", "")
    size_range = f.get("size_range", "")

    badges = _file_badges(f, in_repo)
    badge_str = " ".join(badges)
    id_attr = f' id="{anchor}"' if anchor else ""
    lines.append(f'<div class="rb-file-entry"{id_attr} markdown>')
    lines.append("")
    permalink = f' <a class="rb-permalink" href="#{anchor}" title="Link to this file">#</a>' if anchor else ""
    lines.append(f"**`{fname}`** {badge_str}{permalink}")
    if desc:
        lines.append(f"<br>{_escape_tags(str(desc))}")
    lines.append("")

    details = []
    if fpath and fpath != fname:
        details.append(f"Path: `{fpath}`")
    standalone_path = f.get("standalone_path", "")
    if standalone_path and standalone_path != fpath:
        # The standalone build reads it elsewhere: MesenCE's dsp1.rom goes
        # under Firmware/, and a page naming only the core's path sent its
        # users to the wrong folder.
        details.append(f"Standalone path: `{standalone_path}`")
    if fsystem:
        details.append(f"System: {_system_link(fsystem, '../')}")
    if size:
        if isinstance(size, list):
            size_str = " / ".join(_fmt_size(s) for s in size)
        else:
            size_str = _fmt_size(size)
        if fmin or fmax:
            bounds = []
            if fmin:
                bounds.append(f"min {_fmt_size(fmin)}")
            if fmax:
                bounds.append(f"max {_fmt_size(fmax)}")
            size_str += f" ({', '.join(bounds)})"
        details.append(f"Size: {size_str}")
    elif fmin or fmax:
        bounds = []
        if fmin:
            bounds.append(f"min {_fmt_size(fmin)}")
        if fmax:
            bounds.append(f"max {_fmt_size(fmax)}")
        details.append(f"Size: {', '.join(bounds)}")
    if fsha1:
        details.append(f"SHA1: {_hash_spans(fsha1)}")
    if fmd5:
        details.append(f"MD5: {_hash_spans(fmd5)}")
    if fcrc32:
        crcs = fcrc32 if isinstance(fcrc32, list) else [fcrc32]
        details.append("CRC32: " + ", ".join(f"`{crc}`" for crc in crcs))
    if fsha256:
        details.append(f"SHA256: {_hash_spans(fsha256)}")
    if fadler32:
        # Printed as the downloads spell a hash, one per code span: the list
        # repr read `['0x66f334fe', ...]`. Dolphin hashes its DSP ROMs over
        # byte-swapped 16-bit words, so the raw Adler-32 the downloads carry
        # for the same file differs, and the card says which one it lists.
        values = fadler32 if isinstance(fadler32, list) else [fadler32]
        shown = ", ".join(_code(str(v).lower().removeprefix("0x")) for v in values)
        if f.get("adler32_byteswap"):
            details.append(
                f"Adler32 over the file's 16-bit words byte-swapped: {shown} "
                "(the Adler-32 in the downloads is the file's own and differs)"
            )
        else:
            details.append(f"Adler32: {shown}")
    if aliases:
        details.append(f"Aliases: {', '.join(f'`{a}`' for a in aliases)}")
    if priority is not None:
        details.append(f"Priority: {priority}")
    if fast_boot is not None:
        details.append(f"Fast boot: {'yes' if fast_boot else 'no'}")
    if validation:
        if isinstance(validation, list):
            details.append(f"Validation: {', '.join(validation)}")
        elif isinstance(validation, dict):
            for scope, checks in validation.items():
                details.append(f"Validation ({scope}): {', '.join(checks)}")
    if source_ref:
        asker = requested_by(profile, source_ref)
        if asker:
            # A profile reading a libretro core and the original emulator
            # cited both trees alike: readers could not tell which code asks
            # for the file.
            details.append(f"Asked for by: {asker}")
        details.append(
            f"Source: {_source_ref_markdown(profile, source_ref)}"
        )
    if consumers:
        sha1 = consumers.sha1_of(f, f.get("path") or "", owner=owner)
        if sha1 and in_repo:
            # Which copy answers this entry, linked to its collection card
            # (provenance, every hash, download): an entry declaring no hash
            # said "in repo" without naming the file.
            held = consumers.path_of(sha1)
            parts = held.split("/")
            if len(parts) >= 3 and parts[0] == "bios":
                page = parts[1].lower().replace(" ", "-")
                details.append(
                    f"Collection copy: [{_code(held)}]"
                    f"(../systems/{urllib.parse.quote(page)}.md#sha1-{sha1[:12]})"
                )
        plat_links = _platform_links(sha1, consumers) if sha1 else []
        if plat_links:
            details.append(f"Platforms: {', '.join(plat_links)}")

    if dest and dest != fname and dest != fpath:
        details.append(f"Destination: `{dest}`")
    if config_key:
        details.append(f"Config key: `{config_key}`")
    if fpattern:
        details.append(f"Pattern: `{fpattern}`")
    if region_check is not None:
        details.append(f"Region check: {'yes' if region_check else 'no'}")
    if size_note:
        details.append(f"Size note: {size_note}")
    if size_range:
        details.append(f"Size range: {size_range}")

    if details:
        for d in details:
            lines.append(f"- {d}")
    if fnote:
        lines.append(f"- {_escape_tags(link_prose_citations(profile, str(fnote)))}")
    if not in_repo and f.get("unsourceable"):
        # The badge alone sent readers after a dump that cannot exist, and the
        # reason was only on the gaps page, which the card did not link.
        lines.append(
            f"- Not collectable: {_escape_tags(str(f['unsourceable']))} "
            "([acknowledged gaps](../gaps.md#acknowledged-gaps))"
        )
    if contents:
        # Every member with the hashes the emulator checks: MAME and FBNeo
        # verify the members, never the archive, so a member's crc32 is the
        # value a reader compares a set against.
        lines.append(f"- Contents ({len(contents)} members):")
        for c in contents:
            if isinstance(c, dict):
                parts = []
                if c.get("description"):
                    parts.append(_escape_tags(str(c["description"])))
                if c.get("size"):
                    parts.append(_fmt_size(c["size"]))
                if c.get("crc32"):
                    parts.append(f"CRC32 `{c['crc32']}`")
                if c.get("sha1"):
                    parts.append(f"SHA1 `{c['sha1']}`")
                extra = f": {', '.join(parts)}" if parts else ""
                lines.append(f"    - `{c.get('name', '')}`{extra}")
            else:
                lines.append(f"    - {c}")
    lines.append("")
    lines.append("</div>")
    lines.append("")
    return lines


def _render_metadata_rows(profile: dict) -> list[str]:
    """Scalar profile fields, one table row each.

    Anything structured belongs in a collapsible block below, not in a
    row: a dict rendered into a table cell is unreadable.
    """
    lines: list[str] = []
    for field, label in [
        ("core", "Core ID"),
        ("core_name", "Core name"),
        ("bios_size", "BIOS size"),
        ("bios_directory", "BIOS directory"),
        ("bios_detection", "BIOS detection"),
        ("bios_selection", "BIOS selection"),
        ("firmware_file", "Firmware file"),
        ("firmware_source", "Firmware source"),
        ("firmware_install", "Firmware install"),
        ("firmware_detection", "Firmware detection"),
        ("resources_directory", "Resources directory"),
        ("rom_path", "ROM path"),
        ("game_count", "Game count"),
        ("verification", "Checked by"),
        ("analysis_date", "Analysis date"),
        ("analysis_commit", "Analysis commit"),
    ]:
        val = profile.get(field)
        if val is None or val == "" or isinstance(val, (dict, list)):
            continue
        if isinstance(val, str) and val.startswith("http"):
            lines.append(f"| {label} | [{val}]({val}) |")
        else:
            lines.append(f"| {label} | {val} |")
    if profile.get("source_ref"):
        lines.append(
            f"| Source ref | {_source_ref_markdown(profile, profile['source_ref'])} |"
        )
    return lines


def _render_platform_details(profile: dict) -> list[str]:
    """The per-platform block, one level of nesting deep."""
    lines: list[str] = []
    platform_details = profile.get("platform_details")
    if platform_details and isinstance(platform_details, dict):
        lines.extend(['???+ info "Platform details"', ""])
        for pk, pv in platform_details.items():
            if isinstance(pv, dict):
                lines.append(f"    **{pk}:**")
                for sk, sv in pv.items():
                    lines.append(f"    - {sk}: {_detail_value(profile, sk, sv)}")
            else:
                lines.append(f"    **{pk}:** {_detail_value(profile, pk, pv)}")
        lines.append("")

    return lines


def _detail_value(profile: dict, key: object, value: object) -> str:
    """A platform detail, its citations linked at the profile's pin."""
    if key == "source_ref" and isinstance(value, (str, dict)):
        return _source_ref_markdown(profile, value)
    if isinstance(value, str):
        return _escape_tags(link_prose_citations(profile, value))
    return _plain_value(value)


def _plain_value(value: object) -> str:
    """A scalar or a list of scalars as a reader writes it, not as Python does."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        # A YAML null states that there is none, but Python's `None` read as a gap.
        return "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain_value(item) for item in value)
    return _escape_tags(str(value))


def _render_structured_blocks(profile: dict) -> list[str]:
    """Every remaining structured field, as a collapsible section."""
    lines: list[str] = []
    _structured_blocks = [
        ("analysis", "Source analysis"),
        ("memory_layout", "Memory layout"),
        ("regions", "Regions"),
        ("nvm_layout", "NVM layout"),
        ("model_kickstart_map", "Model kickstart map"),
        ("builtin_boot_roms", "Built-in boot ROMs"),
        ("common_bios_filenames", "Common BIOS filenames"),
        ("valid_bios_crc32", "Valid BIOS CRC32"),
        ("dev_flash", "dev_flash"),
        ("dev_flash2", "dev_flash2"),
        ("dev_flash3", "dev_flash3"),
        ("firmware_modules", "Firmware modules"),
        ("firmware_titles", "Firmware titles"),
        ("fallback_fonts", "Fallback fonts"),
        ("io_devices", "I/O devices"),
        ("partitions", "Partitions"),
        ("mlc_structure", "MLC structure"),
        ("machine_directories", "Machine directories"),
        ("machine_properties", "Machine properties"),
        ("whdload_kickstarts", "WHDLoad kickstarts"),
        ("bios_identical_to", "BIOS identical to"),
        ("pack_structure", "Pack structure"),
        ("firmware_version", "Firmware version"),
    ]
    for field, label in _structured_blocks:
        val = profile.get(field)
        if val is None:
            continue
        lines.append(f'???+ abstract "{label}"')
        lines.append("")
        start = len(lines)
        _render_yaml_value(lines, val, indent=4)
        lines[start:] = [_escape_tags(line) for line in lines[start:]]
        lines.append("")

    return lines


def _hash_spans(value: str | list) -> str:
    """Each hash a profile accepts, in full.

    Cut to 12 characters with the rest in a title attribute, 208 hashes
    that only a profile declares were out of reach of the site search: it
    indexes text, and a pasted sha1 matched nothing.
    """
    from hashing import parse_hash_list  # noqa: PLC0415

    return ", ".join(
        f'<span class="rb-hash">{_code(v)}</span>' for v in parse_hash_list(value)
    )


def _availability_check(
    db: dict, data_names: set[str] | None
) -> Callable[[dict], bool]:
    """A predicate answering whether the collection holds a file.

    It answers the way cross_reference does. A page that called a file
    missing while the gap report called it held would describe a
    different collection on two pages of the same site.
    """
    from cross_reference import entry_source

    by_name = db.get("indexes", {}).get("by_name", {})
    by_name_lower = {k.lower(): k for k in by_name}
    by_path_suffix = db.get("indexes", {}).get("by_path_suffix", {})

    index = {
        "by_name": by_name,
        "by_name_lower": by_name_lower,
        "by_path_suffix": by_path_suffix,
        "by_md5": db.get("indexes", {}).get("by_md5", {}),
        "by_sha256": db.get("indexes", {}).get("by_sha256", {}),
        "by_crc32": db.get("indexes", {}).get("by_crc32", {}),
        "db_files": db.get("files", {}),
        "data_names": data_names,
    }

    def _file_available(f: dict) -> bool:
        """Check if a file is available using the same resolution as cross_reference."""
        # cross_reference sets these aside before resolving: the file found
        # under the name belongs to another game or release.
        if not f.get("name") or f.get("unsourceable"):
            return False
        if f.get("storage", "") in ("release", "large_file"):
            return True
        return entry_source(f, index) is not None

    return _file_available


def _emulator_file_summary(
    files: list[dict], available: Callable[[dict], bool]
) -> list[str]:
    """The counts above an emulator's file table, and its categories.

    Counted by slot, as verify --emulator and the gaps page count: raze
    lists every GRP its code accepts under one name, and holding one fills
    the slot. Counted per entry, the page announced 46 missing where 12
    slots are empty.
    """
    from cross_reference import slot_key

    slots: dict[tuple, list[dict]] = {}
    for f in files:
        slots.setdefault(slot_key(f), []).append(f)
    in_repo_count = sum(1 for group in slots.values() if any(available(f) for f in group))
    unsourceable_count = sum(
        1 for group in slots.values()
        if not any(available(f) for f in group) and all(f.get("unsourceable") for f in group)
    )
    missing_count = len(slots) - in_repo_count - unsourceable_count
    req_count = sum(1 for group in slots.values() if any(f.get("required") for f in group))
    hle_count = sum(1 for group in slots.values() if any(f.get("hle_fallback") for f in group))

    held = f"{in_repo_count} in repo, {missing_count} missing"
    if unsourceable_count:
        held += f", {unsourceable_count} unsourceable"
    total = f"**{_count(len(slots), 'file')}**"
    if len(slots) != len(files):
        total += f" ({len(files)} accepted versions)"
    parts = [
        total,
        f"{req_count} required, {len(slots) - req_count} optional",
        held,
    ]
    if hle_count:
        parts.append(f"{hle_count} with HLE fallback")
    lines = [" | ".join(parts)]

    categories = [f.get("category", "bios") for f in files]
    if "game_data" in categories or "bios_zip" in categories:
        cats = [
            f"{categories.count(key)} {label}"
            for key, label in (
                ("bios", "BIOS"), ("game_data", "game data"), ("bios_zip", "BIOS ZIPs")
            )
            if categories.count(key)
        ]
        lines.append(f"Categories: {', '.join(cats)}")
    lines.append("")
    return lines


# Data directory members and revisions, read once per build.
_DATA_MEMBERS: dict[str, dict[str, dict]] = {}
_DATA_REVISIONS: dict[str, dict] = {}


def generate_emulator_page(
    name: str,
    profile: dict,
    db: dict,
    consumers: FileConsumers | None = None,
    data_names: set[str] | None = None,
    data_registry: dict | None = None,
) -> str:
    if profile.get("type") == "alias":
        parent = profile.get("alias_of", profile.get("bios_identical_to", "unknown"))
        return (
            f"# {name}\n\n"
            f"This core uses the same firmware as **{parent}**.\n\n"
            f"See [{parent}]({parent}.md) for details.\n"
        )

    emu_name = profile.get("emulator", name)
    emu_type = profile.get("type", "unknown")
    classification = profile.get("core_classification", "")
    source_raw = profile.get("source", "")
    source = str(source_raw) if not isinstance(source_raw, dict) else ""
    # A key set to null says there is none (the project is its own upstream,
    # it publishes no version), and an absent key says nobody wrote it down. The
    # page printed `None` for the first, which read as the second.
    upstream_raw = profile.get("upstream", "")
    upstream_none = "upstream" in profile and upstream_raw is None
    upstream = str(upstream_raw) if isinstance(upstream_raw, str) else ""
    version_raw = profile.get("core_version", "unknown")
    if version_raw is None:
        version = "none published"
    elif isinstance(version_raw, dict):
        # Keyed by build mode like source: each build's version, named,
        # where the page printed the dict's Python repr.
        version = ", ".join(f"{v} ({mode})" for mode, v in version_raw.items() if v)
    else:
        version = version_raw
    profiled = profile.get("profiled_date", "unknown")
    systems = profile.get("systems", [])
    cores = profile.get("cores", [name])
    files = profile.get("files", [])
    notes_raw = profile.get("notes", profile.get("note", ""))
    notes = (
        str(notes_raw).strip() if notes_raw and not isinstance(notes_raw, dict) else ""
    )
    exclusion = profile.get("exclusion_note", "")
    data_dirs = profile.get("data_directories", [])

    lines = [
        f"# {emu_name}",
        "",
        '<div class="rb-meta-card" markdown>',
        "",
        "| | |",
        "|---|---|",
        f"| Type | {emu_type} |",
    ]
    if classification:
        cls_display = CLS_LABELS.get(classification, classification)
        lines.append(f"| Classification | {cls_display} |")
    if isinstance(source_raw, dict):
        parts = []
        for k, v in source_raw.items():
            if isinstance(v, str) and v.startswith("http"):
                parts.append(f"[{k}]({v})")
            else:
                parts.append(f"{k}: {v}")
        lines.append(f"| Source | {', '.join(parts)} |")
    elif source:
        if source.startswith("http"):
            lines.append(f"| Source | [{source}]({source}) |")
        else:
            lines.append(f"| Source | {source} |")
    if isinstance(upstream_raw, dict):
        parts = []
        for k, v in upstream_raw.items():
            if isinstance(v, str) and v.startswith("http"):
                parts.append(f"[{k}]({v})")
            else:
                parts.append(f"{k}: {v}")
        lines.append(f"| Upstream | {', '.join(parts)} |")
    elif upstream_none:
        lines.append("| Upstream | none: the source is the project itself |")
    elif upstream and upstream != source:
        if upstream.startswith("http"):
            lines.append(f"| Upstream | [{upstream}]({upstream}) |")
        else:
            lines.append(f"| Upstream | {upstream} |")
    gone = profile.get("upstream_gone")
    if gone:
        # The links above answer 451 or reach a domain someone else now
        # owns: said beside them, with the date the profile observed it.
        lines.append(f"| Source status | gone: {_escape_tags(str(gone))} |")
    mirror = profile.get("source_mirror")
    if mirror:
        lines.append(f"| Mirror | [{mirror}]({mirror}) |" if str(mirror).startswith("http")
                     else f"| Mirror | {mirror} |")
    lines.append(f"| Version | {version} |")
    revision = revision_markdown(profile)
    if revision:
        lines.append(f"| Revision | {revision} |")
    lines.append(f"| Profiled | {profiled} |")
    # The YAML is the reusable document this site renders, and the JSON is the
    # same profile for a program.
    lines.append(
        f"| Profile | [YAML]({REPO_URL}/blob/main/emulators/{name}.yml), "
        f"[JSON](../api/v1/emulators/{name}.json) |"
    )
    if cores:
        lines.append(f"| Cores | {', '.join(str(c) for c in cores)} |")
    if systems:
        sys_links = [_system_link(s, "../") for s in systems]
        lines.append(f"| Systems | {', '.join(sys_links)} |")
    mame_ver = profile.get("mame_version", "")
    if mame_ver:
        lines.append(f"| MAME version | {mame_ver} |")
    author = profile.get("author", "")
    if author:
        lines.append(f"| Author | {author} |")
    based_on = profile.get("based_on", "")
    if based_on:
        lines.append(f"| Based on | {based_on} |")
    lines.extend(_render_metadata_rows(profile))
    lines.append("")
    lines.append("</div>")
    lines.append("")

    lines.extend(_render_platform_details(profile))

    lines.extend(_render_structured_blocks(profile))

    if notes:
        indented = _admonition_body(link_prose_citations(profile, notes))
        lines.extend(['???+ note "Technical notes"', f"    {indented}", ""])

    if not files:
        lines.append("No BIOS or firmware files required.")
        if exclusion:
            lines.extend(
                [
                    "",
                    '!!! info "Why no files"',
                    f"    {_escape_tags(str(exclusion))}",
                ]
            )
    else:
        _file_available = _availability_check(db, data_names)

        lines.extend(_emulator_file_summary(files, _file_available))

        for f, anchor in zip(files, emulator_file_anchors(files)):
            lines.extend(
                _render_emulator_file(
                    f, profile, consumers, files, _file_available, anchor, name
                )
            )

    if data_dirs:
        lines.extend(["## Data directories", ""])
        # Each tree with what it holds, where it comes from, which build reads
        # it and the code that does: these trees put thousands of files in
        # the packs, and the page said only `dolphin-sys` > `Sys`.
        for dd in data_dirs:
            ref = dd.get("ref", "")
            dest = dd.get("destination", "")
            entry = (data_registry or {}).get(ref, {})
            line = f"- `{ref}` > `{dest}`"
            if entry.get("description"):
                line += f": {entry['description']}"
            lines.append(line)
            if entry.get("source_url"):
                lines.append(f"    - Fetched from <{entry['source_url']}>")
            fetched = _DATA_REVISIONS.get(ref) or {}
            if fetched.get("version") or fetched.get("sha"):
                # The URL serves whatever is current: the revision says
                # which content the packs were built from.
                revision = str(fetched.get("sha", "")).strip('"')
                lines.append(f"    - Revision: {fetched.get('version', '')}"
                             + (f" ({_code(revision)})" if revision else ""))
            if dd.get("mode"):
                lines.append(f"    - Read by the {dd['mode']} build only")
            if dd.get("bundled"):
                lines.append("    - Installed with the emulator, so packs leave it out")
            if dd.get("source_ref"):
                lines.append(f"    - Source: {_source_ref_markdown(profile, dd['source_ref'])}")
            members = _DATA_MEMBERS.get(ref) or {}
            if members:
                lines.extend([
                    "",
                    f'    ??? info "{_count(len(members), "file")}, as the packs ship them"',
                    "",
                    "        | File | Size | SHA1 |",
                    "        |------|-----:|------|",
                ])
                for member, info in sorted(members.items()):
                    sha1 = str(info.get("sha1", ""))
                    lines.append(
                        f'        | <span id="sha1-{sha1[:12]}"></span>{_code(member)} '
                        f"| {_fmt_size(int(info.get('size', 0)))} | {_code(sha1)} |"
                    )
                lines.append("")
        lines.append("")

    lines.extend([f"*Generated on {_timestamp()}*"])
    return "\n".join(lines) + "\n"


# Contributing page


_ACCEPTED_RE = re.compile(r"\b(sha1|sha256|md5|crc32) mismatch: got \S+, accepted \[([^\]]*)\]")


def _held_accepted(discrepancy: str, db: dict | None) -> str:
    """Where the collection keeps a dump the emulator accepts, if it does.

    The table printed the hash the emulator wants and stopped there, while
    the collection held that exact dump for every one of the ten rows.
    """
    match = _ACCEPTED_RE.search(discrepancy or "")
    if not match or not db:
        return ""
    algo, accepted = match.group(1), match.group(2)
    indexes = db.get("indexes", {})
    for value in re.split(r"[\s,]+", accepted.lower()):
        if algo == "sha1":
            sha1 = value if value in db.get("files", {}) else None
        else:
            sha1 = indexes.get(f"by_{algo}", {}).get(value)
        path = db.get("files", {}).get(sha1 or "", {}).get("path")
        if path:
            return path
    return ""


def _render_problem_files(
    platform_problems: list, settled: list | None = None, db: dict | None = None
) -> list[str]:
    """Files a platform declares that its own check then rejects.

    Split out of generate_gap_analysis, which reached complexity 56. This
    section is the one that stands alone: it reads platform_problems and
    nothing else, where the sections around it share gap_report and the
    resolved core list.
    """
    lines: list[str] = []
    # ---- Section 2: Problem files ----

    missing_files: dict[str, dict] = {}
    untested_files: dict[str, dict] = {}
    mismatch_files: dict[str, dict] = {}

    for p in platform_problems:
        fname = p["name"]
        if p["status"] == "missing":
            entry = missing_files.setdefault(fname, {
                "name": fname, "required": p["required"],
                "platforms": [], "reason": p["reason"],
            })
            entry["platforms"].append(p["platform"])
            if p["required"]:
                entry["required"] = True
        elif p["status"] == "untested":
            entry = untested_files.setdefault(fname, {
                "name": fname, "required": p["required"],
                "platforms": [], "reason": p["reason"],
            })
            entry["platforms"].append(p["platform"])
        if p.get("discrepancy"):
            entry = mismatch_files.setdefault(fname, {
                "name": fname, "platforms": [],
                "discrepancy": p["discrepancy"],
            })
            entry["platforms"].append(p["platform"])

    total_problems = len(missing_files) + len(untested_files) + len(mismatch_files)

    if total_problems > 0:
        lines.extend([
            "## Problem files",
            "",
            f"{len(missing_files)} missing, {len(untested_files)} untested, "
            f"{len(mismatch_files)} hash mismatch.",
            "",
        ])

        if missing_files:
            lines.extend([
                f'### Missing <span class="rb-badge rb-badge-danger">'
                f"{_count(len(missing_files), 'file')}</span>",
                "",
                "| File | Required | Platforms |",
                "|------|----------|-----------|",
            ])
            for fname in sorted(missing_files):
                f = missing_files[fname]
                req = "yes" if f["required"] else "no"
                plats = ", ".join(sorted(set(f["platforms"])))
                lines.append(f"| `{fname}` | {req} | {plats} |")
            lines.append("")

        if untested_files:
            lines.extend([
                f'### Untested <span class="rb-badge rb-badge-warning">'
                f"{_count(len(untested_files), 'file')}</span>",
                "",
                "Present but hash not verified.",
                "",
                "| File | Platforms | Reason |",
                "|------|----------|--------|",
            ])
            for fname in sorted(untested_files):
                f = untested_files[fname]
                plats = ", ".join(sorted(set(f["platforms"])))
                lines.append(f"| `{fname}` | {plats} | {f['reason']} |")
            lines.append("")

        if mismatch_files:
            lines.extend([
                f'### Hash mismatch <span class="rb-badge rb-badge-warning">'
                f"{_count(len(mismatch_files), 'file')}</span>",
                "",
                "Platform says OK but emulator validation disagrees.",
                "",
                "| File | Platforms | Discrepancy | Accepted dump in the collection |",
                "|------|----------|-------------|-------------------|",
            ])
            for fname in sorted(mismatch_files):
                f = mismatch_files[fname]
                plats = ", ".join(sorted(set(f["platforms"])))
                held = _held_accepted(f["discrepancy"], db)
                cell = f"[{_code(held)}]({_fetch_url(held)})" if held else "-"
                lines.append(f"| `{fname}` | {plats} | {f['discrepancy']} | {cell} |")
            lines.append("")

    if settled:
        lines.extend([
            "### Other revisions of a held destination",
            "",
            "A platform can declare one path twice with different hashes. The "
            "path holds the file one declaration accepts, so it is counted once, "
            "as held. The other declaration cannot be met at the same time.",
            "",
            "| File | Platform | Status | Path held for system |",
            "|------|----------|--------|--------------|",
        ])
        for p in sorted(settled, key=lambda row: (row["name"], row["platform"])):
            lines.append(
                f"| {_code(p['name'])} | {_escape_tags(p['platform'])} | {p['status']} "
                f"| {_code(p['held_by'])} |"
            )
        lines.append("")

    return lines


def generate_gap_analysis(
    profiles: dict,
    coverages: dict,
    db: dict,
    data_names: set[str] | None = None,
    registry: dict | None = None,
    gap_report: dict | None = None,
) -> str:
    """Generate a unified gap analysis page.

    Combines verification results (from coverages/verify.py) with source
    provenance (from cross_reference) into a single truth dashboard.

    Sections:
    1. Verification status -- aggregated across all platforms
    2. Problem files -- missing, untested, hash mismatch
    3. Core complement -- emulator files not declared by any platform
    """

    from common import resolve_platform_cores

    # ---- Section 1: aggregate verify results across all platforms ----

    total_verified = 0
    total_untested = 0
    total_missing_verify = 0
    total_files_verify = 0

    platform_problems: list[dict] = []
    settled_problems: list[dict] = []
    for pname, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        total_verified += cov["verified"]
        total_untested += cov["untested"]
        total_missing_verify += cov["missing"]
        total_files_verify += cov["total"]

        for d in cov["details"]:
            if d.get("settled_by") and d["status"] != "ok":
                # Its path holds another declaration's file: the headline,
                # counted by destination, rightly leaves it out, and listing
                # it under the same word made the page contradict itself.
                settled_problems.append({
                    "platform": cov["platform"], "name": d["name"],
                    "status": d["status"], "held_by": d["settled_by"],
                })
                continue
            if d["status"] != "ok" or d.get("discrepancy"):
                platform_problems.append({
                    "platform": cov["platform"],
                    "platform_key": pname,
                    "name": d["name"],
                    "status": d["status"],
                    "required": d.get("required", True),
                    "reason": d.get("reason", ""),
                    "discrepancy": d.get("discrepancy", ""),
                    "system": d.get("system", ""),
                })

    pct_verified = (
        f"{total_verified / total_files_verify * 100:.0f}%"
        if total_files_verify
        else "0%"
    )

    lines = [
        "# Gap analysis",
        "",
        "Unified view of BIOS verification, file provenance, and coverage gaps.",
        "",
        "[Download gaps CSV](downloads/gaps.csv){ .md-button } "
        "[Open gaps API](api/v1/gaps.json){ .md-button } "
        "[All data exports](data.md){ .md-button }",
        "",
        '<div class="rb-stats" markdown>',
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{total_files_verify:,}</span>',
        '<span class="rb-stat-label">Total files (all platforms)</span>',
        "</div>",
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{total_verified:,}</span>',
        f'<span class="rb-stat-label">Verified ({pct_verified})</span>',
        "</div>",
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{total_untested:,}</span>',
        '<span class="rb-stat-label">Untested</span>',
        "</div>",
        "",
        '<div class="rb-stat" markdown>',
        f'<span class="rb-stat-value">{total_missing_verify:,}</span>',
        '<span class="rb-stat-label">Missing</span>',
        "</div>",
        "",
        "</div>",
        "",
    ]

    # ---- Verification per platform ----

    lines.extend([
        "## What each pack contains",
        "",
        "| Platform | On its BIOS list | Files its emulators load | Checked by |",
        "|----------|-----------------:|-------------------------:|------------|",
    ])

    mode_labels = {
        "md5": "MD5 hash",
        "sha1": "SHA1 hash",
        "existence": "file presence",
    }

    def _ratio(done: int, total: int) -> str:
        """A collected-over-needed cell, flagged when short."""
        if not total:
            return "-"
        cell = f"{done:,}/{total:,}"
        if done < total:
            return f'<span class="rb-badge rb-badge-danger">{cell}</span>'
        return cell

    for pname, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        display = cov["platform"]
        core_total = (
            cov["core_present"] + cov["core_missing"] + cov["core_unsourceable"]
        )
        lines.append(
            f"| [{display}](platforms/{pname}.md) "
            f"| {_ratio(cov['present'], cov['total'])} "
            f"| {_ratio(cov['core_present'], core_total)} "
            f"| {mode_labels.get(cov['mode'], cov['mode'])} |"
        )
    lines.extend([
        "",
        "Each fraction is what the pack has over what is needed, counting "
        "required and optional files alike since both ship. The first column "
        "is the BIOS list the platform publishes. The second counts files its "
        "emulators load that this list never mentions, found by reading their "
        "source code, and it is routinely several times larger. A short "
        "fraction is flagged and named in the sections below. That second "
        "number is a floor, not a ceiling: an emulator that accepts any file "
        "handed to it names none in its code, so nothing there can be counted. "
        "Checked by is the test the platform runs on its own, replicated here "
        "from its source code "
        "([how each one works](wiki/verification-modes.md)).",
        "",
        "## Corroboration against emulator source",
        "",
        "| Platform | On its BIOS list | Documented in a profile | Content the code checks |",
        "|----------|-----------------:|------------------------:|------------------------:|",
    ])

    for pname, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        gt = cov["ground_truth"]
        if not gt.get("applicable", True):
            prof_cell = gt_cell = "-"
        elif gt["total"]:
            prof_pct = f"{gt.get('with_profile', 0) / gt['total'] * 100:.0f}%"
            prof_cell = f"{gt.get('with_profile', 0)} ({prof_pct})"
            gt_pct = f"{gt['with_validation'] / gt['total'] * 100:.0f}%"
            gt_cell = f"{gt['with_validation']} ({gt_pct})"
        else:
            prof_cell = gt_cell = "0"
        lines.append(
            f"| [{cov['platform']}](platforms/{pname}.md) "
            f"| {gt['total']:,} | {prof_cell} | {gt_cell} |"
        )

    lines.extend([
        "",
        "Both columns count the files on the platform's own BIOS list, the "
        "same number as the first column of the table above. Documented in a "
        "profile means an emulator profile, written from source, describes the "
        "file. Content the code checks is stricter: the emulator verifies a "
        "size or hash for it, and this tool repeats that check. The gap "
        "between the two is "
        "not a defect, it is what the emulator code does: many emulators load "
        "a file without ever checking its content, and no amount of profiling "
        "can invent a check the code does not perform. A dash means no "
        "profiled emulator applies to the platform, whose own source is then "
        "the only authority.",
        "",
        _content_check_ceiling(profiles),
        "",
    ])

    # ---- Section 1b: platform lists vs emulator source ----

    from truth import diff_platform_truth, generate_platform_truth

    div_rows = []
    for pname, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        truth_data = generate_platform_truth(
            pname, cov["config"], (registry or {}).get(pname, {}), profiles, db
        )
        s = diff_platform_truth(truth_data, native_layer(cov["config"]))["summary"]
        if s["systems_compared"] == 0:
            div_rows.append(
                f"| [{cov['platform']}](platforms/{pname}.md) | - | - | - | - |"
            )
        else:
            div_rows.append(
                f"| [{cov['platform']}](platforms/{pname}.md) "
                f"| {s['total_missing']} "
                f"| {s['total_extra_phantom'] + s['total_extra_unprofiled']} "
                f"| {s['total_hash_mismatch']} "
                f"| {s['total_required_mismatch']} |"
            )

    lines.extend([
        "## Platform lists vs emulator source",
        "",
        "Platform file lists are scraped as-is from each upstream project. "
        "Emulator profiles are read from source code. The two do not always "
        "agree, and this table counts the differences. Packs follow the "
        "platform contract, and these numbers show where that contract diverges "
        "from what the code loads.",
        "",
        "| Platform | Missing from list | Phantom | Hash conflict | Required status |",
        "|----------|------------------:|--------:|--------------:|----------------:|",
        *div_rows,
        "",
        "- **Missing from list**: a profiled emulator loads the file, the platform list does not mention it",
        "- **Phantom**: on the platform list, loaded by no profiled emulator",
        "- **Hash conflict**: the platform list and the emulator source expect different hashes",
        "- **Required status**: required/optional differs between list and code",
        "",
        "A dash means no profiled emulator overlaps the platform's systems, "
        "so there is nothing to compare: the platform's own source is the "
        "only authority for its files.",
        "",
        "The same comparison drives `scripts/exporter/`: each platform's "
        "corrected list can be regenerated in its native format "
        "(System.dat, es_bios.xml, batocera-systems.json, ...).",
        "",
    ])

    lines.extend(_render_problem_files(platform_problems, settled_problems, db))

    # ---- Section 3: Core complement (cross-reference provenance) ----

    unique_profiles = {
        k: v
        for k, v in profiles.items()
        if v.get("type") not in ("alias", "test")
    }
    relevant_set: set[str] = set()
    for _name, cov in coverages.items():
        matched = resolve_platform_cores(cov["config"], unique_profiles)
        relevant_set.update(matched)
    if gap_report is None:
        gap_report = build_emulator_gap_report(profiles, coverages, db, data_names)
    report_all = gap_report

    src_totals: dict[str, int] = {"bios": 0, "data": 0, "large_file": 0, "missing": 0}
    total_undeclared = 0
    emulator_gaps = []

    for emu_name, data in sorted(report_all.items()):
        if data["gaps"] == 0:
            continue
        total_undeclared += data["gaps"]
        for key in src_totals:
            src_totals[key] += data.get(f"gap_{key}", 0)
        emulator_gaps.append((emu_name, data))


    if total_undeclared > 0:
        total_available = (
            src_totals["bios"] + src_totals["data"] + src_totals["large_file"]
        )
        pct_available = (
            f"{total_available / total_undeclared * 100:.0f}%"
            if total_undeclared
            else "0%"
        )

        lines.extend([
            "## Core complement",
            "",
            f"Files loaded by emulators but not declared by any platform. "
            f"{total_undeclared:,} files across {len(emulator_gaps)} emulators, "
            f"{total_available:,} available ({pct_available}), "
            f"{src_totals['missing']} to source.",
            "",
            "This counts every profiled emulator, including those no platform "
            "ships yet, so it reaches past what the pack tables above measure: "
            "those cover only the emulators each platform actually ships. "
            "Whatever is not in the collection is an acquisition target, named "
            "per emulator below.",
            "",
            "### Provenance",
            "",
            "| Source | Count | Description |",
            "|--------|------:|-------------|",
            f"| bios/ | {src_totals['bios']} | In repository (database.json) |",
            f"| data/ | {src_totals['data']} | Data directories (buildbot, GitHub) |",
            f"| release | {src_totals['large_file']} "
            "| GitHub release assets (large files) |",
            f"| missing | {src_totals['missing']} | Not available, needs sourcing |",
            "",
            "### Per emulator",
            "",
            "| Emulator | Undeclared | bios | data | release | Missing |",
            "|----------|----------:|-----:|-----:|--------:|--------:|",
        ])

        for emu_name, data in sorted(emulator_gaps, key=lambda x: -x[1]["gaps"]):
            display = data["emulator"]
            m = data.get("gap_missing", 0)
            missing_str = (
                f'<span class="rb-badge rb-badge-danger">{m}</span>'
                if m > 0
                else '<span class="rb-badge rb-badge-success">0</span>'
            )
            lines.append(
                f"| [{display}](emulators/{emu_name}.md) "
                f"| {data['gaps']} "
                f"| {data.get('gap_bios', 0)} "
                f"| {data.get('gap_data', 0)} "
                f"| {data.get('gap_large_file', 0)} "
                f"| {missing_str} |"
            )
        lines.append("")

        # List truly missing files with platform impact
        emu_to_platforms: dict[str, set[str]] = {}
        unique_profiles = {
            k: v
            for k, v in profiles.items()
            if v.get("type") not in ("alias", "test")
        }
        for pname in coverages:
            config = coverages[pname]["config"]
            matched = resolve_platform_cores(config, unique_profiles)
            for emu_name in matched:
                emu_to_platforms.setdefault(emu_name, set()).add(pname)

        # One row per file to find: a name two emulators share is one file
        # only when both declare the same hash. Keyed by name alone, Fallout
        # 2's master.dat vanished behind Fallout 1's, and the three Sonic
        # decompilations' Data.rsdk showed as one.
        src_missing_details: list[dict] = []
        by_identity: dict[tuple[str, str], dict] = {}
        entries = 0
        bundled_rows: set[tuple[str, str]] = set()
        for emu_name, data in emulator_gaps:
            for g in data["gap_details"]:
                if g["source"] != "missing":
                    continue
                if g.get("bundled"):
                    # It arrives with the emulator's own package, which is
                    # why the collection documents it and does not hold it:
                    # asking readers to source it sent them after a file
                    # every install already has.
                    bundled_rows.add((data["emulator"], g["name"]))
                    continue
                entries += 1
                key = (g["name"], g.get("identity") or f"emulator:{emu_name}")
                row = by_identity.get(key)
                if row is None:
                    row = by_identity[key] = {
                        "name": g["name"],
                        "path": g.get("path") or g["name"],
                        "identity": g.get("identity", ""),
                        "emulators": [data["emulator"]],
                        "emu_keys": [emu_name],
                        "required": g["required"],
                        "source_ref": g["source_ref"],
                    }
                    src_missing_details.append(row)
                elif emu_name not in row["emu_keys"]:
                    row["emulators"].append(data["emulator"])
                    row["emu_keys"].append(emu_name)
                    row["required"] = row["required"] or g["required"]

        if src_missing_details:
            req_src = [m for m in src_missing_details if m["required"]]
            lines.extend([
                f"### Files to source ({_count(len(src_missing_details), 'file')}, "
                f"{len(req_src)} required"
                + (
                    f", {_count(entries, 'entry', 'entries')}: "
                    "a file declared with the same hash at several places is listed once)"
                    if entries != len(src_missing_details) else ")"
                ),
                "",
                "| File | Destination | Expected | Emulator | Required | Affects platforms | Source ref |",
                "|------|-------------|----------|----------|----------|------------------|-----------|",
            ])
            for m in sorted(
                src_missing_details,
                key=lambda x: (not x["required"], x["name"]),
            ):
                plats = sorted(set().union(
                    *(emu_to_platforms.get(key, set()) for key in m["emu_keys"])
                ))
                plat_badges = (
                    " ".join(
                        f'<span class="rb-badge rb-badge-info">{p}</span>'
                        for p in plats
                    )
                    if plats
                    else "-"
                )
                req = "yes" if m["required"] else "no"
                algorithm, _, value = m["identity"].partition(":")
                expected = f"{algorithm} {_code(value)}" if value else "-"
                destination = _code(m["path"]) if m["path"] != m["name"] else "-"
                emulators = ", ".join(
                    f"[{_escape_tags(display)}](emulators/{key}.md)"
                    for display, key in zip(m["emulators"], m["emu_keys"])
                )
                lines.append(
                    f"| `{m['name']}` | {destination} | {expected} | {emulators} | {req} | "
                    f"{plat_badges} | "
                    f"{_source_ref_markdown(profiles[m['emu_keys'][0]], m['source_ref'])} |"
                )
            lines.append("")
        if bundled_rows:
            emulators = sorted({emulator for emulator, _name in bundled_rows})
            lines.extend([
                f"{len(bundled_rows)} further files arrive with their emulator's own "
                f"package ({_escape_tags(', '.join(emulators))}): documented in the profile, not "
                "collected, and not listed here.",
                "",
            ])

    # ---- Section 4: Acknowledged gaps (unsourceable files) ----

    all_unsourceable: list[dict] = []
    for emu_name, data in sorted(report_all.items()):
        for u in data.get("unsourceable", []):
            all_unsourceable.append({
                "name": u["name"],
                "emulator": data["emulator"],
                "emu_key": emu_name,
                "reason": u["reason"],
                "source_ref": u.get("source_ref", ""),
            })

    if all_unsourceable:
        lines.extend([
            "## Acknowledged gaps",
            "",
            f"{len(all_unsourceable)} files documented as unsourceable "
            "(verified from source code).",
            "",
            "| File | Emulator | Reason | Source ref |",
            "|------|----------|--------|-----------|",
        ])
        for u in sorted(all_unsourceable, key=lambda x: x["name"]):
            lines.append(
                f"| `{u['name']}` | {u['emulator']} | {u['reason']} "
                f"| {_source_ref_markdown(profiles[u['emu_key']], u['source_ref'])} |"
            )
        lines.append("")

    lines.extend(["", f'<div class="rb-timestamp">Generated on {_timestamp()}.</div>'])
    return "\n".join(lines) + "\n"




def _render_xref_by_platform(
    coverages: dict, unique: dict
) -> list[str]:
    """Which cores each platform pulls in, and what they need."""
    lines: list[str] = []
    for pname in sorted(coverages.keys(), key=lambda x: coverages[x]["platform"]):
        cov = coverages[pname]
        display = cov["platform"]
        config = cov["config"]
        platform_cores = config.get("cores", [])

        lines.append(f'??? abstract "{display}"')
        lines.append("")
        lines.append(
            f"    [Open {display} platform profile](platforms/{pname}.md)"
            "{ .md-button }"
        )
        lines.append("")

        # The cores the builder and verify resolve: a hand copy here dropped
        # the libretro set a list naming retroarch pulls in, and the system
        # fallback EmuDeck relies on.
        matched = {
            key: unique[key]
            for key in resolve_platform_cores(config, unique)
            if key in unique
        }

        if platform_cores == "all_libretro":
            lines.append(f"    **{len(matched)} cores** (all libretro)")
        else:
            lines.append(f"    **{len(matched)} cores**")
        lines.append("")

        lines.append("    | Core | Classification | Systems | Files | Upstream |")
        lines.append("    |------|---------------|---------|-------|----------|")

        for emu_name in sorted(matched.keys()):
            p = matched[emu_name]
            emu_display = p.get("emulator", emu_name)
            cls_raw = p.get("core_classification", "-")
            cls = CLS_LABELS.get(cls_raw, cls_raw)
            p.get("type", "")
            # Null means the project is its own upstream: show its source.
            upstream_raw2 = p.get("upstream", "")
            upstream = upstream_raw2 if isinstance(upstream_raw2, str) else ""
            source_raw2 = p.get("source", "")
            source = str(source_raw2) if not isinstance(source_raw2, dict) else ""
            systems = p.get("systems", [])
            files = p.get("files", [])

            sys_str = ", ".join(systems[:3])
            if len(systems) > 3:
                sys_str += f" +{len(systems) - 3}"

            file_count = len(files)
            # Count mode divergences
            libretro_only = sum(1 for f in files if f.get("mode") == "libretro")
            standalone_only = sum(1 for f in files if f.get("mode") == "standalone")
            file_str = str(file_count)
            if libretro_only or standalone_only:
                parts = []
                if libretro_only:
                    parts.append(f"{libretro_only} libretro-only")
                if standalone_only:
                    parts.append(f"{standalone_only} standalone-only")
                file_str += f" ({', '.join(parts)})"

            upstream_display = "-"
            if upstream and upstream.startswith("http"):
                upstream_short = upstream.replace("https://github.com/", "")
                upstream_display = f"[{upstream_short}]({upstream})"
            elif upstream:
                upstream_display = upstream
            elif source and source.startswith("http"):
                source_short = source.replace("https://github.com/", "")
                upstream_display = f"[{source_short}]({source})"
            elif source:
                upstream_display = source

            lines.append(
                f"    | [{emu_display}](emulators/{emu_name}.md) | {cls} | "
                f"{sys_str} | {file_str} | {upstream_display} |"
            )

        lines.append("")

    return lines


def _render_xref_by_upstream(
    coverages: dict, unique: dict
) -> list[str]:
    """The same relation read the other way: one row per upstream."""
    lines: list[str] = []
    # Reverse view: by upstream emulator
    lines.extend(
        [
            "## By upstream emulator",
            "",
            "| Upstream | Cores | Classification | Platforms |",
            "|----------|-------|---------------|-----------|",
        ]
    )

    # Group profiles by upstream
    by_upstream: dict[str, list[str]] = {}
    for emu_name, p in sorted(unique.items()):
        # A null upstream is the project's own source: grouping it under
        # the string "None" put eight unrelated engines in one row.
        raw_up = p.get("upstream") or p.get("source", "")
        up_str = raw_up if isinstance(raw_up, str) else ""
        if up_str:
            by_upstream.setdefault(up_str, []).append(emu_name)

    # Build platform membership per core
    platform_membership: dict[str, set[str]] = {}
    for pname, cov in coverages.items():
        for key in resolve_platform_cores(cov["config"], unique):
            platform_membership.setdefault(key, set()).add(pname)

    for upstream_url in sorted(by_upstream.keys()):
        cores = by_upstream[upstream_url]
        upstream_short = upstream_url.replace("https://github.com/", "")
        classifications = set()
        all_plats: set[str] = set()
        for c in cores:
            raw_cls = unique[c].get("core_classification", "-")
            classifications.add(CLS_LABELS.get(raw_cls, raw_cls))
            all_plats.update(platform_membership.get(c, set()))

        cls_str = ", ".join(sorted(classifications))
        plat_str = ", ".join(sorted(all_plats)) if all_plats else "-"
        core_links = ", ".join(f"[{c}](emulators/{c}.md)" for c in sorted(cores))

        if upstream_url.startswith("http"):
            upstream_cell = f"[{upstream_short}]({upstream_url})"
        else:
            upstream_cell = upstream_short
        lines.append(
            f"| {upstream_cell} | {core_links} | "
            f"{cls_str} | {plat_str} |"
        )

    return lines


def generate_cross_reference(
    coverages: dict,
    profiles: dict,
) -> str:
    """Generate cross-reference: Platform -> Core -> Systems -> Upstream."""
    unique = {
        k: v for k, v in profiles.items() if v.get("type") not in ("alias", "test")
    }

    total_cores = len(unique)
    total_upstreams = len({
        p.get("upstream", p.get("source", ""))
        for p in unique.values()
        if p.get("upstream") or p.get("source")
    })

    lines = [
        "# Cross-reference",
        "",
        f"Platform > Core > Systems > Upstream emulator. "
        f"{total_cores} cores across {len(coverages)} platforms, "
        f"tracing back to {total_upstreams} upstream projects.",
        "",
        "The libretro core is a port of the upstream emulator. "
        "Files, features, and validation may differ between the two.",
        "",
        "[Download cross-reference CSV](downloads/cross-reference.csv){ .md-button } "
        "[Open emulator API](api/v1/emulators.json){ .md-button } "
        "[All data exports](data.md){ .md-button }",
        "",
    ]

    lines.extend(_render_xref_by_platform(coverages, unique))
    lines.extend(_render_xref_by_upstream(coverages, unique))

    lines.extend(["", f"*Generated on {_timestamp()}*"])
    return "\n".join(lines) + "\n"


def generate_contributing() -> str:
    return contributing_text(
        lambda page: f"{page}.md", "# Contributing", "on this site"
    )


# Wiki pages
# index, architecture, tools, profiling are maintained as wiki/ sources
# and copied verbatim by main(). Only data-model is generated dynamically.


def generate_wiki_data_model(db: dict, profiles: dict) -> str:
    """Generate data model documentation from actual database structure."""
    files_count = len(db.get("files", {}))
    by_md5 = len(db.get("indexes", {}).get("by_md5", {}))
    by_name = len(db.get("indexes", {}).get("by_name", {}))
    by_crc32 = len(db.get("indexes", {}).get("by_crc32", {}))
    by_path = len(db.get("indexes", {}).get("by_path_suffix", {}))
    by_sha256 = len(db.get("indexes", {}).get("by_sha256", {}))

    lines = [
        "# Data model",
        "",
        "## database.json",
        "",
        f"Primary key: SHA1. **{files_count}** file entries.",
        "",
        "Each entry:",
        "",
        "```json",
        "{",
        '  "path": "bios/Nintendo/GameCube/GC/USA/IPL.bin",',
        '  "name": "IPL.bin",',
        '  "size": 2097152,',
        '  "sha1": "...",',
        '  "md5": "...",',
        '  "sha256": "...",',
        '  "crc32": "...",',
        '  "adler32": "...",',
        '  "provenance": {',
        '    "redump": {"dat": "...", "name": "...", "description": "..."}',
        "  }",
        "}",
        "```",
        "",
        "`provenance` maps each catalog that lists the file to the DAT and entry "
        "it was matched against. It is present only when the file matches a "
        "snapshot under `provenance/`. The join runs by SHA1 first, then by "
        "MD5 + size. See [dump provenance](../provenance.md).",
        "",
        "### Indexes",
        "",
        "| Index | Entries | Purpose |",
        "|-------|---------|---------|",
        f"| `by_md5` | {by_md5} | MD5 to SHA1 lookup (Batocera, Recalbox verification) |",
        f"| `by_name` | {by_name} | filename to SHA1 list (name-based resolution) |",
        f"| `by_crc32` | {by_crc32} | CRC32 to SHA1 lookup |",
        f"| `by_path_suffix` | {by_path} | relative path to SHA1 (regional variant disambiguation) |",
        f"| `by_sha256` | {by_sha256} | SHA256 to SHA1 lookup (emulator profile validation) |",
        "",
        "### File resolution order",
        "",
        "`resolve_local_file` tries these steps in order:",
        "",
        "1. SHA1 exact match: every other declared hash must agree with the record",
        "2. SHA256 exact match, with the same all-declarations-must-agree rule",
        "3. CRC32 plus declared size, only when no stronger hash is present",
        "4. MD5 direct lookup (including explicitly supported truncated MD5 values)",
        "5. Path suffix lookup for regional variants. With hashes it is accepted only if those hashes match",
        "6. Name and alias lookup only when no content hash was declared",
        "7. Candidate inspection for composite ZIP MD5 or direct MD5. A named candidate with the wrong content returns `hash_mismatch`",
        "8. `zipped_file` content match via the inner-ROM MD5 index",
        "9. MAME clone fallback, only for declarations without a content hash",
        "10. Data-directory scan: declared hashes are computed over the candidate before it is accepted",
        "11. Agnostic size/path fallback, only for declarations without a content hash",
        "",
        "A filename or destination can never override a declared hash. The first "
        "evidence-compatible match wins, otherwise the resolver reports a mismatch "
        "or absence. Steps and their return codes are described in "
        "[verification modes](verification-modes.md#file-resolution-chain).",
        "",
        "## Platform YAML",
        "",
        "Scraped from upstream sources. Structure:",
        "",
        "```yaml",
        "platform: Batocera",
        "verification_mode: md5        # how the platform checks files",
        "hash_type: md5                # hash type in file entries",
        "base_destination: bios        # root directory for BIOS files",
        "systems:",
        "  system-id:",
        "    files:",
        "      - name: filename",
        "        destination: path/in/bios/dir",
        "        md5: expected_hash",
        "        sha1: expected_hash",
        "        required: true",
        "```",
        "",
        "Supports inheritance (`inherits: retroarch`) and shared groups",
        "(`includes: [group_name]` referencing `_shared.yml`).",
        "",
        "`base_destination` is the prefix the pack applies to every entry. It is",
        "empty when the upstream destinations already carry their own root, which",
        "is why the RetroDECK pack ships `bios/` and `roms/` at its top level.",
        "",
        "## Emulator YAML",
        "",
        f"**{len(profiles)}** profile files, **{len(unique_emulator_profiles(profiles))}** "
        "distinct emulators once aliases are folded in. Source-verified from "
        "emulator code.",
        "",
        "See the [profiling guide](profiling.md) for the full field reference.",
        "",
        "## Static API and bulk exports",
        "",
        "The website publishes versioned JSON, CSV and SQLite metadata generated "
        "from these same structures. Start with the [Data & API](../data.md) "
        "catalog. Each downloadable artifact carries a SHA256 in "
        "`api/v1/catalog.json`.",
        "",
    ]
    return "\n".join(lines) + "\n"


# Build cross-reference indexes


def _build_file_consumers(db: dict, coverages: dict, profiles: dict) -> FileConsumers:
    """Which platforms and emulators read each collected file."""
    consumers = FileConsumers(db, build_zip_contents_index(db))
    consumers.add_platforms({name: cov["config"] for name, cov in coverages.items()})
    consumers.add_emulators(profiles)
    consumers.add_manifests(Path(__file__).resolve().parent.parent / "install")
    return consumers


def _build_emulator_file_index(profiles: dict) -> dict[str, dict]:
    """Map emulator_name -> {systems, norm}, for the platform pages.

    norm holds the systems as _norm_system_id reads them: a platform files
    the Mega Drive as sega-mega-drive and genesis_plus_gx as sega-megadrive,
    and an exact match left the RetroArch page with no emulator for it.
    """
    from common import _norm_system_id

    return {
        name: {
            "systems": set(profile.get("systems", [])),
            "norm": {_norm_system_id(str(s)) for s in profile.get("systems", [])},
        }
        for name, profile in profiles.items()
        if profile.get("type") != "alias"
    }


def _system_emulators(sys_id: str, emulator_files: dict | None) -> list[str]:
    """The emulators a platform runs for one of its systems."""
    from common import _norm_system_id

    norm = _norm_system_id(sys_id)
    return [name for name, data in (emulator_files or {}).items()
            if sys_id in data["systems"] or norm in data["norm"]]


def _mkdocs_config(docs: Path, text: str) -> tuple[Path, str]:
    """Where the site's config goes, and its text for that place.

    The tracked mkdocs.yml belongs to docs/. A preview into another folder
    rewrote it anyway, with the navigation of work in progress, and mkdocs
    kept reading docs/. Another folder gets its own config beside it, its
    paths taken from there.
    """
    if os.path.normpath(str(docs)) == DOCS_DIR:
        return Path("mkdocs.yml"), text
    where = docs.parent
    for relative in ("scripts/mkdocs_hooks.py", "docs_assets/overrides"):
        text = text.replace(relative, os.path.relpath(relative, where).replace(os.sep, "/"))
    return where / f"{docs.name}.mkdocs.yml", f"docs_dir: {docs.name}\n" + text


# mkdocs.yml nav generator


def generate_which_pack() -> str:
    """Generate the 'Which pack?' decision page."""
    rel = RELEASE_URL
    return f"""\
# Download

Some retro consoles need firmware files (commonly called BIOS) to run games.
Without them, the emulator either refuses to start the game or runs it with
reduced accuracy. RetroBIOS maps those requirements to source-traced emulator
profiles and verifies local content against the evidence each platform exposes.

This page picks the right pack for a setup. For BIOS directory paths per
platform, verification, and the CLI, see
[Getting started](wiki/getting-started.md).

## Quick install

One line detects the platform and BIOS directory, downloads only missing or
incorrect files, verifies their hashes, and installs them atomically. The small
bootstrap checks the Python installer against an embedded SHA-256 before running
it, and the installer reads its file list from that same revision.

**Linux / Mac / Steam Deck:**

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex
```

That is the complete default flow. Extra copies into detected standalone
emulator directories are deliberately opt-in with `--standalone-copies`, so
automatic setup never writes outside the selected platform tree unexpectedly.
Use `python install.py --check` from a checkout for a read-only verification.

Options, environment overrides, how each platform is detected and what the
installer is allowed to write: [Installer](wiki/installer.md).

---

## Manual download

Pick the pack that matches the setup from the [latest release]({rel}),
download it, and extract the files into the BIOS folder listed below.
The metadata and website can be newer than that release: pack publication is
manual and only happens after the release gates pass.

A pack over 2 GB comes in several parts, and every part is needed. How to
open them depends on their name:

- `PackName.part1of2.zip`, `PackName.part2of2.zip`: each part is an ordinary
  ZIP. Extract them all into the same folder.
- `PackName.zip.001`, `PackName.zip.002` (releases up to v2026.09.04): slices
  of one ZIP, none of which opens on its own. Put them in one folder and open
  the `.001` with 7-Zip or PeaZip, or join them first:
    - Linux/macOS: `cat PackName.zip.0* > PackName.zip`
    - Windows: `cmd /c copy /b PackName.zip.001+PackName.zip.002 PackName.zip`

### Steam Deck

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| [EmuDeck](https://www.emudeck.com/) | Installs and configures multiple emulators, adds each game to the Steam library | [EmuDeck]({rel}) | `~/Emulation/bios/` |
| [RetroDECK](https://retrodeck.net/) | Single Flatpak app, all emulators bundled, one-click install from Discover | [RetroDECK]({rel}) | `~/retrodeck/` (the pack carries its own `bios/`) |
| RetroArch standalone | Installed from Discover, Steam, or Flatpak | [RetroArch]({rel}) | Open RetroArch > Settings > Directory > System, that is the folder |

### Windows

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| [RetroArch](https://www.retroarch.com/) | Multi-system emulator, loads different cores for each console | [RetroArch]({rel}) | The `system` folder next to `retroarch.exe` |
| [RetroBat](https://www.retrobat.org/) | Windows frontend with EmulationStation, includes RetroArch and standalone emulators | [RetroBat]({rel}) | The `bios` folder inside the RetroBat installation |
| [BizHawk](https://tasvideos.org/BizHawk) | Accuracy-focused multi-system emulator, popular for speedruns and TAS | [BizHawk]({rel}) | The `Firmware` folder inside the BizHawk installation |
| [LaunchBox](https://www.launchbox-app.com/) | Game library manager and launcher, uses RetroArch or standalone emulators behind the scenes | [RetroArch]({rel}) | Open RetroArch (via LaunchBox) > Settings > Directory > System |

### Linux

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| RetroArch (native) | Installed via package manager or AppImage | [RetroArch]({rel}) | `~/.config/retroarch/system/` |
| RetroArch (Flatpak) | Installed from Flathub | [RetroArch]({rel}) | `~/.var/app/org.libretro.RetroArch/config/retroarch/system/` |
| [Batocera](https://batocera.org/) | Bootable OS dedicated to gaming, runs from USB or full install, supports PC and SBC | [Batocera]({rel}) | `/userdata/bios/` |
| [Recalbox](https://www.recalbox.com/) | Bootable OS for retro gaming that configures its emulators itself | [Recalbox]({rel}) | `/recalbox/share/bios/` |

### macOS

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| [RetroArch](https://www.retroarch.com/) | Multi-system emulator | [RetroArch]({rel}) | `~/Library/Application Support/RetroArch/system/` |

### Raspberry Pi and single-board computers

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| [RetroPie](https://retropie.org.uk/) | The classic Pi emulation setup, largest community, most online guides | [RetroPie]({rel}) | `~/RetroPie/BIOS/` |
| [Lakka](https://www.lakka.tv/) | Lightweight RetroArch OS, minimal config, boots straight into the UI | [Lakka]({rel}) | `/storage/system/` |
| [Batocera](https://batocera.org/) | Easy setup, works on Pi 3/4/5 and many other boards (Odroid, etc.) | [Batocera]({rel}) | `/userdata/bios/` |
| [Recalbox](https://www.recalbox.com/) | Plug-and-play experience, good for a first build | [Recalbox]({rel}) | `/recalbox/share/bios/` |

### Handhelds

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| Android (Retroid Pocket, Odin, etc.) | Most Android handhelds run RetroArch | [RetroArch]({rel}) | `RetroArch/system/` on internal storage or SD card |
| [ROCKNIX](https://rocknix.org/) | Linux OS for ARM and x86 handhelds (RG35XX, RG552, Deck) | [ROCKNIX]({rel}) | `/storage/roms/bios/`, over SSH or the network share |
| [Batocera](https://batocera.org/) | Also images many handhelds | [Batocera]({rel}) | `/userdata/bios/` |

### FPGA

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| [MiSTer FPGA](https://mister-devel.github.io/MkDocs_MiSTer/) | Hardware-level recreation of consoles on a DE10-Nano board | [MiSTer FPGA]({rel}) | `/media/fat/games/`, one subfolder per core |

### Self-hosted ROM manager

| Setup | What it is | Pack | Extract to |
|-------|-----------|------|-----------|
| [RomM](https://github.com/rommapp/romm) | Web-based ROM manager, plays games in the browser via EmulatorJS | [RomM]({rel}) | The `bios` folder in the RomM library, one subfolder per system |

---

## One pack per platform

The [latest release]({rel}) carries one pack for each platform, and that
pack holds everything the platform runs: the BIOS list it publishes, plus
every file its emulator cores load, read from their source code. There is no
lighter variant to choose, because a pack that leaves out a file a core needs
means a game that does not boot, with no message saying why. It is not a
guarantee either: source profiles can document files nobody has dumped or
that only the user can provide, all of them visible in the
[gap analysis](gaps.md).

Want less than everything? At a terminal the installer asks whether to install
everything or to choose systems, cores and regions, and `--system`, `--core`,
`--region` and `--target` make the same choice without questions (see the
[installer reference](wiki/installer.md#choosing-what-to-install)). From a
clone of the repository, `python scripts/generate_pack.py --platform retroarch
--region us` builds the same narrowing as a pack and `--required-only` the bare
minimum each core needs to start, and `--help` lists every way to build your own.

---

## After extraction

Launch a game and use the emulator's firmware status screen where available.
Most supported frontends find files in the documented directory automatically,
but standalone emulators may still need their BIOS path configured.

If a game still asks for a missing file, check the
[platforms section](platforms/index.md) for the full file list, or the
[emulators section](emulators/index.md) for what each core expects.
"""


def generate_mkdocs_nav(
    coverages: dict,
    manufacturers: dict,
    profiles: dict,
) -> list:
    """Generate the nav section for mkdocs.yml."""
    platform_nav = [{"Overview": "platforms/index.md"}]
    for name in sorted(coverages.keys(), key=lambda x: coverages[x]["platform"]):
        display = coverages[name]["platform"]
        platform_nav.append({display: f"platforms/{name}.md"})

    system_nav = [{"Overview": "systems/index.md"}]
    for mfr in sorted(manufacturers.keys()):
        slug = mfr.lower().replace(" ", "-")
        system_nav.append({mfr: f"systems/{slug}.md"})

    unique_profiles = {
        k: v for k, v in profiles.items() if v.get("type") not in ("alias", "test")
    }

    # Group emulators by classification for nav
    by_class: dict[str, list[tuple[str, str]]] = {}
    for name in sorted(unique_profiles.keys()):
        p = unique_profiles[name]
        cls = p.get("core_classification", "other")
        display = p.get("emulator", name)
        by_class.setdefault(cls, []).append((display, f"emulators/{name}.md"))

    # Classification display names
    cls_labels = {
        "pure_libretro": "Pure libretro",
        "official_port": "Official ports",
        "community_fork": "Community forks",
        "frozen_snapshot": "Frozen snapshots",
        "enhanced_fork": "Enhanced forks",
        "game_engine": "Game engines",
        "embedded_hle": "Embedded HLE",
        "launcher": "Launchers",
        "other": "Other",
    }

    emu_nav: list = [{"Overview": "emulators/index.md"}]
    for cls in [
        "official_port",
        "community_fork",
        "pure_libretro",
        "game_engine",
        "enhanced_fork",
        "frozen_snapshot",
        "embedded_hle",
        "launcher",
        "other",
    ]:
        entries = by_class.get(cls, [])
        if not entries:
            continue
        label = cls_labels.get(cls, cls)
        sub = [{display: path} for display, path in entries]
        emu_nav.append({f"{label} ({len(entries)})": sub})

    wiki_nav = [
        {"Overview": "wiki/index.md"},
        {"First install": "wiki/first-install.md"},
        {"Getting started": "wiki/getting-started.md"},
        {"Installer": "wiki/installer.md"},
        {"FAQ": "wiki/faq.md"},
        {"Troubleshooting": "wiki/troubleshooting.md"},
        {"Architecture": "wiki/architecture.md"},
        {"Tools": "wiki/tools.md"},
        {"Advanced usage": "wiki/advanced-usage.md"},
        {"Verification modes": "wiki/verification-modes.md"},
        {"Data model": "wiki/data-model.md"},
        {"Profiling guide": "wiki/profiling.md"},
        {"Adding a platform": "wiki/adding-a-platform.md"},
        {"Adding a scraper": "wiki/adding-a-scraper.md"},
        {"Testing guide": "wiki/testing-guide.md"},
        {"Release process": "wiki/release-process.md"},
        {"Community tools": "wiki/community-tools.md"},
    ]

    return [
        {"Home": "index.md"},
        {"Download": "which-pack.md"},
        {"Platforms": platform_nav},
        {"Systems": system_nav},
        {"Emulators": emu_nav},
        {"Cross-reference": "cross-reference.md"},
        {"Gap analysis": "gaps.md"},
        {"Dump provenance": "provenance.md"},
        {"Data & API": "data.md"},
        {"Wiki": wiki_nav},
        {"Contributing": "contributing.md"},
    ]


# Main


def main() -> None:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Generate MkDocs site from project data"
    )
    parser.add_argument(
        "--db", default="database.json",
        help="database file to read (default: %(default)s)",
    )
    parser.add_argument(
        "--platforms-dir", default="platforms",
        help="platform YAML directory (default: %(default)s)",
    )
    parser.add_argument(
        "--emulators-dir", default="emulators",
        help="emulator profile directory (default: %(default)s)",
    )
    parser.add_argument(
        "--docs-dir", default=DOCS_DIR,
        help="directory the site sources are written to (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        # Two runs over one docs/ delete each other's pages and scratch
        # files in the sweep, so the second is refused instead.
        import contextlib

        from common import load_data_dir_registry
        from refresh_data_dirs import hold_data_dirs

        # The coverage pages resolve against data/, held while they are read.
        with artifact_lock(args.docs_dir), contextlib.ExitStack() as held:
            hold_data_dirs(held, load_data_dir_registry(args.platforms_dir))
            _generate(args)
    except ArtifactLockBusy as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _generate(args: argparse.Namespace) -> None:

    db = load_database(args.db)
    docs = Path(args.docs_dir)

    for d in GENERATED_DIRS:
        (docs / d).mkdir(parents=True, exist_ok=True)

    # Copy stylesheet if source exists
    css_src = Path("docs_assets") / "extra.css"
    css_dest = docs / "stylesheets" / "extra.css"
    if css_src.exists():
        css_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(css_src, css_dest)
    js_src = Path("docs_assets") / "site.js"
    js_dest = docs / "javascripts" / "site.js"
    if js_src.exists():
        js_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(js_src, js_dest)

    # No robots.txt: a crawler reads it only at the host root (RFC 9309), and
    # this project site lives under /retrobios/. Every page's head points at
    # the sitemap instead (overrides/main.html).
    (docs / "robots.txt").unlink(missing_ok=True)

    # Copy branding assets
    images_dest = docs / "assets" / "images"
    images_dest.mkdir(parents=True, exist_ok=True)
    assets_src = Path(".github") / "assets"
    for name, dest_name in [("logo.png", "logo.png"), ("favicon.png", "favicon.png")]:
        src = assets_src / name
        if src.exists():
            shutil.copy2(src, images_dest / dest_name)

    registry_path = Path(args.platforms_dir) / "_registry.yml"
    registry = {}
    if registry_path.exists():
        with open(registry_path) as f:
            registry = (yaml_load(f) or {}).get("platforms", {})

    platform_names = list_registered_platforms(
        args.platforms_dir, include_archived=True
    )

    from common import load_data_dir_registry
    from cross_reference import _build_supplemental_index

    data_registry = load_data_dir_registry(args.platforms_dir)
    suppl_names = _build_supplemental_index()

    print("Computing platform coverage...")
    coverages = {}
    for name in sorted(platform_names):
        try:
            cov = compute_coverage(
                name, args.platforms_dir, db, data_registry,
                emulators_dir=args.emulators_dir,
            )
            coverages[name] = cov
            print(
                f"  {cov['platform']}: {cov['present']}/{cov['total']} ({_pct(cov['present'], cov['total'])})"
            )
        except FileNotFoundError as e:
            print(f"  {name}: skipped ({e})", file=sys.stderr)

    print("Loading emulator profiles...")
    profiles = load_emulator_profiles(args.emulators_dir, skip_aliases=False)
    unique_count = sum(1 for p in profiles.values() if p.get("type") != "alias")
    print(
        f"  {len(profiles)} profiles ({unique_count} unique, {len(profiles) - unique_count} aliases)"
    )

    # Build cross-reference indexes
    consumers = _build_file_consumers(db, coverages, profiles)
    emulator_files = _build_emulator_file_index(profiles)

    print("Generating home page...")
    write_if_changed(
        str(docs / "index.md"), generate_home(
            db, coverages, profiles, registry, args.platforms_dir
        )
    )

    stats = compute_stats(db, coverages, profiles)
    stats["composition"] = compute_composition(db)
    write_if_changed(str(docs / "stats.json"), generate_stats(stats))

    # Computed once: the gap analysis page and the published gaps export must
    # report the same files.
    gap_report = build_emulator_gap_report(profiles, coverages, db, suppl_names)

    print("Generating static API and bulk data exports...")
    exports = generate_data_exports(
        docs, db, coverages, profiles, stats, gap_report
    )
    write_if_changed(str(docs / "data.md"), generate_data_page(exports))
    PAGE_DATA["data.md"] = dataset_structured_data(exports, db)

    # Build system_id -> manufacturer page map (needed by all generators)
    print("Building system cross-reference map...")
    manufacturers = _group_by_manufacturer(db)
    _build_system_page_map_from_data(manufacturers, coverages, db, profiles)
    print(f"  {len(_system_page_map)} system IDs mapped to pages")

    print("Generating platform pages...")
    write_if_changed(
        str(docs / "platforms" / "index.md"),
        generate_platform_index(coverages, registry),
    )
    for name, cov in coverages.items():
        # The emulators this platform runs, not every one of the system.
        runs = resolve_platform_cores(cov["config"], profiles)
        write_if_changed(
            str(docs / "platforms" / f"{name}.md"),
            generate_platform_page(name, cov, registry, {
                emu: data for emu, data in emulator_files.items() if emu in runs
            }),
        )
        PAGE_DATA[f"platforms/{name}.md"] = platform_structured_data(name, cov)

    print("Generating system pages...")

    prime_system_icons({
        _icon_name(mfr, console)
        for mfr, consoles in manufacturers.items()
        for console in consoles
    })

    write_if_changed(
        str(docs / "systems" / "index.md"), generate_systems_index(manufacturers)
    )
    (docs / "files").mkdir(parents=True, exist_ok=True)
    write_if_changed(str(docs / "files" / "index.md"), generate_files_page())
    for mfr, consoles in manufacturers.items():
        slug = mfr.lower().replace(" ", "-")
        page = generate_system_page(mfr, consoles, consumers)
        PAGE_DATA[f"systems/{slug}.md"] = system_structured_data(slug, consoles)
        write_if_changed(str(docs / "systems" / f"{slug}.md"), page)

    print("Generating emulator pages...")
    _DATA_MEMBERS.update(data_directory_members())
    _DATA_REVISIONS.update(data_directory_revisions())
    write_if_changed(
        str(docs / "emulators" / "index.md"), generate_emulators_index(profiles)
    )
    public_profiles = {
        name: profile
        for name, profile in profiles.items()
        if profile.get("type") not in ("alias", "test")
    }
    (docs / "api" / "v1" / "emulators").mkdir(parents=True, exist_ok=True)
    for name, profile in public_profiles.items():
        # The profile itself, one small document per core: reusing one
        # meant downloading emulators.json (5.9 MB) and filtering it.
        # The envelope and item of emulators.json, one item long: the
        # profile validates against the published schema, which an "id"
        # beside its fields broke on all 530, and schema_version is there.
        write_if_changed(
            str(docs / "api" / "v1" / "emulators" / f"{name}.json"),
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": str(db.get("generated_at") or _timestamp()),
                    "kind": "emulators",
                    "count": 1,
                    "items": [{"id": name, "profile": profile}],
                },
                indent=2, sort_keys=True, ensure_ascii=False, default=str,
            ) + "\n",
        )
        page = generate_emulator_page(
            name, profile, db, consumers, suppl_names, data_registry
        )
        PAGE_DATA[f"emulators/{name}.md"] = emulator_structured_data(profile)
        write_if_changed(str(docs / "emulators" / f"{name}.md"), page)

    print("Generating cross-reference page...")
    write_if_changed(
        str(docs / "cross-reference.md"), generate_cross_reference(coverages, profiles)
    )

    print("Generating gap analysis page...")
    write_if_changed(
        str(docs / "gaps.md"),
        generate_gap_analysis(
            profiles, coverages, db, suppl_names, registry, gap_report
        ),
    )

    print("Generating dump provenance page...")
    provenance_report = build_report(db, load_provenance_snapshots())
    write_if_changed(
        str(docs / "provenance.md"),
        generate_provenance_page(db, provenance_report, load_derivable()),
    )

    # Wiki pages: copy manually maintained sources + generate dynamic ones
    print("Generating wiki pages...")
    wiki_dest = docs / "wiki"
    wiki_dest.mkdir(parents=True, exist_ok=True)
    wiki_src = Path(WIKI_SRC_DIR)
    if wiki_src.is_dir():
        for src_file in wiki_src.glob("*.md"):
            shutil.copy2(src_file, wiki_dest / src_file.name)
            _record(wiki_dest / src_file.name)
    # data-model.md is generated (contains live DB stats)
    write_if_changed(
        str(wiki_dest / "data-model.md"), generate_wiki_data_model(db, profiles)
    )

    print("Generating which-pack page...")
    write_if_changed(str(docs / "which-pack.md"), generate_which_pack())

    print("Generating contributing page...")
    write_if_changed(str(docs / "contributing.md"), generate_contributing())

    print("Adding page metadata and structured data...")
    decorate_markdown_pages(docs)

    # Update mkdocs.yml nav section only (avoid yaml.dump round-trip mangling quotes)
    print("Updating mkdocs.yml nav...")
    nav = generate_mkdocs_nav(coverages, manufacturers, profiles)
    nav_yaml = yaml.dump(
        {"nav": nav}, default_flow_style=False, sort_keys=False, allow_unicode=True
    )

    # Rewrite mkdocs.yml entirely (static config + generated nav)
    mkdocs_static = """\
site_name: RetroBIOS
site_description: Source-verified BIOS and firmware packs for RetroArch, Batocera,
  Recalbox, Lakka, RetroPie, EmuDeck, RetroBat, RetroDECK, RomM, BizHawk, ROCKNIX
  and MiSTer FPGA.
site_url: https://abdess.github.io/retrobios/
repo_url: https://github.com/Abdess/retrobios
repo_name: Abdess/retrobios
# Almost every page is generated from platforms/, emulators/ and database.json,
# so a per-page edit link would point at a file that does not exist.
edit_uri: ''
# lastmod in the sitemap from each page's own date, or none at all.
hooks:
  - scripts/mkdocs_hooks.py
# Working notes kept under docs/ during development are not part of the site.
exclude_docs: |
  plans/
  specs/
copyright: MIT for the tooling. BIOS and firmware files are third-party system
  software, preserved for personal backup, archival and interoperability.
theme:
  name: material
  # Open Graph tags live in overrides/main.html: the social plugin that
  # would otherwise emit them needs Pillow and CairoSVG only to render a
  # preview image the pages do not need.
  custom_dir: docs_assets/overrides
  palette:
  - media: (prefers-color-scheme)
    toggle:
      icon: material/brightness-auto
      name: Switch to light mode
  - media: '(prefers-color-scheme: light)'
    scheme: default
    toggle:
      icon: material/brightness-7
      name: Switch to dark mode
  - media: '(prefers-color-scheme: dark)'
    scheme: slate
    toggle:
      icon: material/brightness-4
      name: Switch to auto
  font: false
  logo: assets/images/logo.png
  favicon: assets/images/favicon.png
  icon:
    logo: material/chip
  features:
  - navigation.instant
  - navigation.instant.prefetch
  - navigation.instant.progress
  - navigation.tabs
  - navigation.tabs.sticky
  - navigation.sections
  - navigation.top
  - navigation.tracking
  - navigation.indexes
  # 400+ pages: pruning keeps the navigation out of every page's HTML.
  - navigation.prune
  - navigation.footer
  - search.suggest
  - search.highlight
  - search.share
  - content.code.copy
  - content.tabs.link
  - toc.follow
extra_css:
- stylesheets/extra.css
extra_javascript:
- javascripts/site.js
extra:
  social:
  - icon: fontawesome/brands/github
    link: https://github.com/Abdess/retrobios
    name: RetroBIOS on GitHub
markdown_extensions:
- abbr
- admonition
- attr_list
- def_list
- footnotes
- meta
- md_in_html
- tables
- toc:
    permalink: true
- pymdownx.details
- pymdownx.highlight:
    anchor_linenums: true
- pymdownx.inlinehilite
- pymdownx.keys
- pymdownx.superfences:
    custom_fences:
    - name: mermaid
      class: mermaid
      format: !!python/name:pymdownx.superfences.fence_code_format
- pymdownx.tabbed:
    alternate_style: true
plugins:
- search
# Link rot fails the build: deploy-site.yml runs `mkdocs build --strict`.
# omitted_files stays at its default (info) so a stale local docs/ does not
# break a local build. CI regenerates docs/ from scratch anyway.
validation:
  absolute_links: warn
  unrecognized_links: warn
  anchors: warn
"""
    config_path, config_text = _mkdocs_config(Path(docs), mkdocs_static + nav_yaml)
    write_if_changed(str(config_path), config_text)

    total_pages = (
        1  # home
        + 1
        + len(coverages)  # platform index + detail
        + 1
        + len(manufacturers)  # system index + detail
        + 1  # cross-reference
        + 1
        + sum(
            1 for profile in profiles.values()
            if profile.get("type") not in ("alias", "test")
        )  # emulator detail pages (aliases/tests remain metadata-only)
        + 1  # gap analysis
        + 1  # which-pack
        + len(list(Path("wiki").glob("*.md")))  # wiki pages copied verbatim
        + 1  # generated wiki/data-model
        + 1  # contributing
        + 1  # data and API
    )
    stale = _sweep_generated(docs, GENERATED_DIRS)
    print(f"\nGenerated {total_pages} pages in {args.docs_dir}/")
    if stale:
        print(f"Removed {stale} page(s) nothing produced this run")

from sitewrite import (  # noqa: E402,F401
    _produced,
    _record,
    _undecorated,
    write_if_changed,
    write_decorated,
    _sweep_generated,
)
from sitesources import (  # noqa: E402,F401
    link_prose_citations,
    requested_by,
    revision_markdown,
    _repo_pins,
    _forge_sources,
    _source_permalink,
    _source_ref_markdown,
)
from siterender import (  # noqa: E402,F401
    _by_mode,
    _count,
    _render_yaml_value,
    _slugify_anchor,
    _system_page_map,
    _system_link,
    _platform_link,
    _emulator_link,
    _fmt_size,
    _pct,
    _admonition_body,
    _code,
    _escape_tags,
    _icon_name,
    _icon_url,
    system_icon_markdown,
    prime_system_icons,
    _icon_available,
    SYSTEM_ICON_BASE,
    ICON_CACHE_PATH,
)

if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
