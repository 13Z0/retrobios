#!/usr/bin/env python3
"""Generate the site from database.json, platform configs, and emulator profiles.

The pages, navigation and data files are written into the Astro Starlight
project in website/, which builds the site.

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
import functools
import html
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    upstream_profile_index,
    build_zip_contents_index,
    GAME_DATA_TOPS,
    compute_composition,
    count_catalog_matched,
    list_registered_platforms,
    load_platform_registry,
    load_database,
    load_emulator_profiles,
    load_platform_config,
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
from cross_reference import FileConsumers, held_copy
from nativemode import digest_algorithm, reads_file_contents






yaml = require_yaml()
from generate_readme import (
    compute_coverage,
    platform_coverages,
    contributing_text,
    download_links,
    extract_notes,
    extract_targets,
    release_totals,
)
from provenance_report import build_report, distinct_missing
import sitemarkup as markup
import sitestarlight
from siteexports import generate_exports_page, load_export_reports
from siteguides import (
    ALSO_RUNS,
    DEVICE_GROUPS,
    GROUP_ANCHORS,
    GUIDES,
    Guide,
    FENCE_TITLES,
    INSTALL_BASE,
    TERMINALS,
    TRUST_ITEMS,
    detected_systems as _detected_systems,
    fence,
    folder_list,
    install_command,
    setup_count,
    platform_args,
    terminal_tabs,
    trust_box,
)
import release_record

# The Starlight project, and where its pages go inside it.
STARLIGHT_DIR = "website"
STARLIGHT_CONTENT = "src/content/docs"
SITE_NAME = "RetroBIOS"
REPO_URL = "https://github.com/Abdess/retrobios"
RELEASE_URL = f"{REPO_URL}/releases/latest"
SITE_URL = "https://abdess.github.io/retrobios/"
GENERATED_DIRS = ["platforms", "systems", "emulators", "wiki", "api", "downloads"]
# Generated directories of files served as they are, not pages.
PUBLIC_DIRS = ("api", "downloads")




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

# The same classes said of one emulator: its page read "Classification:
# Official ports".
CLS_LABEL_ONE = {
    "official_port": "Official port",
    "community_fork": "Community fork",
    "pure_libretro": "Pure libretro",
    "game_engine": "Game engine",
    "enhanced_fork": "Enhanced fork",
    "frozen_snapshot": "Frozen snapshot",
    "embedded_hle": "Embedded HLE",
    "launcher": "Launcher",
    "unclassified": "Unclassified",
    "other": "Other",
}


def _copy_places(db: dict) -> dict[str, set[tuple[str, str, str]]]:
    """sha1 -> every (manufacturer, console, file name) holding a copy.

    The database keeps one path per content, deduplication having left one
    copy. by_path_suffix still records the other folders a copy is filed in,
    under the name that folder gives it.
    """
    files = db.get("files", {})
    bios_dir = Path(str(db.get("bios_dir") or "bios"))
    places: dict[str, set[tuple[str, str, str]]] = {}
    for sha1, entry in files.items():
        parts = str(entry.get("path", "")).split("/")
        if len(parts) >= 3 and parts[0] == "bios":
            places.setdefault(sha1, set()).add((parts[1], parts[2], parts[-1]))
    for tail, sha1s in db.get("indexes", {}).get("by_path_suffix", {}).items():
        parts = tail.split("/")
        if len(parts) < 3 or parts[1].startswith(".") or len(sha1s) != 1:
            continue
        if sha1s[0] in files and (bios_dir / tail).is_file():
            places.setdefault(sha1s[0], set()).add((parts[0], parts[1], parts[-1]))
    return places


# bios/Other/<emulator> and bios/Arcade/<emulator or board> file a copy by
# who loads it, not by machine. A copy also filed under a machine's maker
# places the system there.
_BUCKET_FOLDERS = frozenset({"Other", "Arcade"})


def _slug(manufacturer: str) -> str:
    return manufacturer.lower().replace(" ", "-")


def _names_manufacturer(sys_id: str, manufacturer: str) -> bool:
    slug = _slug(manufacturer)
    return sys_id == slug or sys_id.startswith(f"{slug}-")


def _copies_by_path(db: dict) -> dict[str, set[tuple[str, str, str]]]:
    """The copy places of each content, keyed by the path the database keeps."""
    files = db.get("files", {})
    return {
        str(files[sha1].get("path", "")): where
        for sha1, where in _copy_places(db).items()
    }


def _system_folder(
    files: list[dict],
    db: dict,
    name_consoles: dict[str, set[tuple[str, str]]],
    sys_id: str = "",
    copies: dict[str, set[tuple[str, str, str]]] | None = None,
) -> tuple[str, str] | None:
    """The bios/<manufacturer>/<console> folder a system's files live in.

    A content votes for every folder holding a copy under the name the system
    declares, not only the one deduplication kept: disksys.rom is kept as
    Arcade/FBNeo/fdsbios.nes and filed again under Nintendo/Famicom Disk
    System. A copy filed under another name, outside the manufacturer the id
    names, is another machine's file and votes only when no copy carries the
    declared name. An id that names its manufacturer (nintendo-fds) keeps the
    votes cast inside that manufacturer when there are any, and a maker's
    folder outranks the folders that file copies by emulator.
    """
    if copies is None:
        copies = _copies_by_path(db)
    votes: dict[tuple[str, str], int] = {}
    for fe in files:
        local, status = resolve_local_file(fe, db)
        if not local or not resolution_is_hash_exact(status):
            continue
        held = copies.get(local, set())
        if not held:
            parts = local.split("/")
            if len(parts) >= 4 and parts[0] == "bios":
                held = {(parts[1], parts[2], parts[-1])}
        declared = str(fe.get("name", "")).casefold()
        named = {
            (m, c) for m, c, base in held
            if base.casefold() == declared or _names_manufacturer(sys_id, m)
        }
        for place in named or {(m, c) for m, c, _ in held}:
            votes[place] = votes.get(place, 0) + 1
    if votes:
        own = [place for place in votes if _names_manufacturer(sys_id, place[0])]
        makers = [place for place in votes if place[0] not in _BUCKET_FOLDERS]
        candidates = own or makers or list(votes)
        return max(sorted(candidates), key=lambda place: votes[place])
    for fe in files:
        places = name_consoles.get(fe.get("name", ""), set())
        if len(places) == 1:
            return next(iter(places))
    return None


def _folders_by_name(manufacturers: dict) -> dict[str, set[tuple[str, str]]]:
    """Normalized system name -> the console folders that spell it.

    nintendo-nes, nes and the NES folder normalize alike, as do
    sega-mastersystem and Sega/Master System.
    """
    from common import _norm_system_id

    index: dict[str, set[tuple[str, str]]] = {}
    for mfr, consoles in manufacturers.items():
        for console in consoles:
            spelled = console.lower().replace(" ", "-")
            for key in {
                _norm_system_id(f"{_slug(mfr)}-{spelled}"), _norm_system_id(spelled)
            }:
                index.setdefault(key, set()).add((mfr, console))
    return index


def _folder_named(sys_id: str, by_name: dict[str, set[tuple[str, str]]]) -> tuple[str, str] | None:
    from common import _norm_system_id

    places = by_name.get(_norm_system_id(sys_id), set())
    return next(iter(places)) if len(places) == 1 else None


def _system_pages_from_data(
    manufacturers: dict,
    coverages: dict,
    db: dict,
    profiles: dict | None = None,
) -> dict[str, tuple[str, str]]:
    """The system_id -> (manufacturer_slug, console_name) table.

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
    copies = _copies_by_path(db)
    folders_by_name = _folders_by_name(manufacturers)
    pages: dict[str, tuple[str, str]] = {}
    for cov in coverages.values():
        config = cov["config"]
        for sys_id, system in config.get("systems", {}).items():
            if sys_id in pages:
                continue
            # Strategy 1: the one console folder the id spells (nintendo-nes,
            # NES). A copy of the disk system BIOS in the NES list does not
            # move the NES to the folder that copy sits in.
            # Strategy 2: the folder holding the files the system declares,
            # found by content, and most votes win. A bare name stands in only
            # where the collection files it in a single console.
            place = _folder_named(sys_id, folders_by_name) or _system_folder(
                system.get("files", []), db, name_consoles, sys_id, copies
            )
            if place is not None:
                mfr, console = place
                pages[sys_id] = (mfr.lower().replace(" ", "-"), console)
                continue
            # Strategy 3: fuzzy match system_id against console directory names
            if sys_id in console_norm:
                pages[sys_id] = console_norm[sys_id]
            else:
                # Try partial match: "nintendo-wii" matches "Wii" under "Nintendo"
                parts = sys_id.split("-")
                for i in range(len(parts)):
                    suffix = "-".join(parts[i:])
                    if suffix in console_norm:
                        pages[sys_id] = console_norm[suffix]
                        break

    # Systems only emulator profiles name (sega-naomi, commodore-pet): placed
    # by the files the profile files under them, as a platform's are. A
    # profile of one system files all of its entries there.
    for profile in (profiles or {}).values():
        systems = [str(sid) for sid in profile.get("systems") or []]
        for sys_id in systems:
            if sys_id in pages:
                continue
            files = [
                entry for entry in profile.get("files") or []
                if isinstance(entry, dict)
                and (entry.get("system") == sys_id or len(systems) == 1)
            ]
            place = _folder_named(sys_id, folders_by_name) or _system_folder(
                files, db, name_consoles, sys_id, copies
            )
            if place is not None:
                mfr, console = place
                pages[sys_id] = (mfr.lower().replace(" ", "-"), console)
            elif sys_id in console_norm:
                pages[sys_id] = console_norm[sys_id]
    return pages







def _timestamp() -> str:
    """The build time for data files (stats, API records), ISO 8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_as_of(moment: datetime | None = None) -> str:
    """The build date for a reader, in words, with the machine form beside it.
    A raw ISO stamp ("2026-10-22T22:43:08Z") closed the home page."""
    moment = moment or datetime.now(timezone.utc)
    return (f'<time datetime="{moment.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
            f"Data as of {moment.day} {moment.strftime('%B %Y')}</time>")


def _date_line() -> str:
    """The date a page closes on, in the words of data_as_of()."""
    return f'<div class="rb-timestamp">{data_as_of()}.</div>'




# Home page


def _stat(value: str, label: str) -> list[str]:
    return [
        markup.block_open("div", 'class="rb-stat"'),
        f'<span class="rb-stat-value">{value}</span>',
        f'<span class="rb-stat-label">{label}</span>',
        "</div>",
        "",
    ]


def _group_members(coverages: dict, registry: dict, group: str) -> list[str]:
    """Registered platforms of a device group, by display name."""
    return sorted(
        (name for name in coverages if registry.get(name, {}).get("device_group") == group),
        key=lambda name: coverages[name]["platform"].casefold(),
    )


def _next_block(links: list[str]) -> list[str]:
    """Where to go from here, at the end of a page: a plain heading and a
    list. A tinted callout with a side stripe said the same as the pager."""
    return ["## Next steps", "", *(f"- {link}" for link in links), ""]


def _tiles(registry: dict, members: list[str], display: dict[str, str], prefix: str = "") -> list[str]:
    """The setup tiles of one device group: logo, name and the hardware it runs on."""
    return markup.platform_tiles([
        (display[name], f"{prefix}platforms/{name}.md",
         str(registry.get(name, {}).get("logo") or ""),
         GUIDES[name].runs_on or _runs_on(registry.get(name, {}), GUIDES[name]))
        for name in members
    ])


def setup_picker(registry: dict, display: dict[str, str], prefix: str = "") -> list[str]:
    """Every setup as a tile, under the device group it belongs to. The
    setups alone in their group share one row: "MiSTer FPGA" above
    "MiSTer FPGA" said nothing."""
    lines: list[str] = []
    alone: list[str] = []
    for group, (label, _description) in DEVICE_GROUPS.items():
        members = _group_keys(registry, group, display)
        if len(members) == 1:
            alone += members
        elif members:
            lines += [f"**{label}**", "", *_tiles(registry, members, display, prefix)]
    if alone:
        lines += ["**Other setups**", "", *_tiles(registry, alone, display, prefix)]
    return lines


def generate_home(
    db: dict,
    coverages: dict,
    profiles: dict,
    registry: dict | None = None,
    platforms_dir: str = "platforms",
) -> str:
    """The page someone lands on knowing nothing: what a BIOS file is,
    whether they need one, and the three steps to get the right ones.

    The figures about the collection, the cards to the deeper pages and the
    call for help sat below the steps and doubled the page: they are on the
    Explore index, one tab further. No date and no "next" box: the hero
    buttons say where to go."""
    registry = registry or load_platform_registry(platforms_dir)
    display = {key: load_platform_config(key, platforms_dir).get("platform", key)
               for key in registry}
    named = [n for n in ("RetroArch", "Batocera", "EmuDeck") if n in display.values()]
    others = setup_count() - len(named)
    works_with = ", ".join(named) + (f" and {others} more" if others else "")

    lines = [
        "# BIOS files for your emulators",
        "",
        "Some consoles need a small program to start their games. It is called "
        "the **BIOS**. Your emulator needs a copy of it. We put the right copy "
        "in the right folder.",
        "",
        markup.button("Get your files", "which-pack.md", primary=True) + " "
        + markup.button("What is a BIOS?", "wiki/what-are-bios-files.md"),
        "",
        f"Free. No account. Works with {works_with}.",
        "",
        "- A game says BIOS missing? [When a game does not start](wiki/troubleshooting.md)",
        "- Have a file already? Reference: [Check it](files/index.md)",
        "",
        "## Do I need it?",
        "",
        "- **Yes**, if your emulator says a BIOS is missing.",
        "- **Yes** for PlayStation 1 and 2, Saturn, Dreamcast and Neo Geo.",
        "- **Usually not** for NES, Super Nintendo, Mega Drive and Game Boy.",
        "",
        "Details: [Every console, one by one](systems/index.md)",
        "",
        "## Three steps",
        "",
        *markup.steps([
            "**Pick your device.** Its page shows your BIOS folder.",
            "**Run one command**, or download a ZIP file.",
            "**Start a game.**",
        ]),
        "## Pick your device",
        "",
        *setup_picker(registry, display),
        "## Is it safe?",
        "",
        *(f"- {item}" for item in TRUST_ITEMS),
        "",
        "[How the command is checked](wiki/faq.md#is-piping-the-installer-into-a-shell-safe). "
        "Details: [Is it legal?](wiki/legal.md)",
        "",
    ]
    return "\n".join(lines) + "\n"


def generate_explore_index(db: dict, coverages: dict, profiles: dict) -> str:
    """The Explore tab opens here: where to look for one console, one
    emulator or one installer option, and the figures of the collection."""
    total_files = db.get("total_files", 0)
    total_size = db.get("total_size", 0)
    emulator_count = len(unique_emulator_profiles(profiles))
    lines = [
        "# Explore",
        "",
        "One console, one emulator, or one installer option: each has its page, "
        "with the files it needs and the code that reads them.",
        "",
        *markup.card_grid([
            ("Systems", ["What each console and computer needs, file by file.", "",
                         "[Browse the systems](systems/index.md)"]),
            ("Emulators", ["What each emulator reads, with the source code that reads it.", "",
                           "[Browse the emulators](emulators/index.md)"]),
            ("Installer options", [
                "Fewer files, one region, another folder, a check without download.", "",
                "[All installer options](wiki/installer.md)"]),
            ("How files are checked", [
                "Each setup checks its files its own way: by name, by MD5 or by SHA1.", "",
                "Reference: [How each platform checks files](wiki/verification-modes.md)"]),
            ("Data and API", ["Every file and hash as JSON, CSV and SQLite.", "",
                              "Reference: [Data and API](data.md)"]),
        ]),
        "## The collection",
        "",
        markup.block_open("div", 'class="rb-stats"'),
        "",
        *_stat(f"{total_files:,}", COLLECTION_FILES_LABEL),
        *_stat(str(setup_count()), "Setups supported"),
        *_stat(str(emulator_count), "Emulators documented"),
        *_stat(_fmt_size(total_size), "Size of the collection"),
        "</div>",
        "",
        composition_sentence(db),
        "",
        "## Help the project",
        "",
        "A file you hold, an emulator profile or a correction fills a gap for "
        "every setup at once. For contributors: [How to contribute](contributing.md).",
        "",
    ]
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


def _release_date(tag: str) -> str:
    """The date a release tag names (v2026.10.10), in words, or empty."""
    match = re.fullmatch(r"v(\d{4})\.(\d{2})\.(\d{2})", tag or "")
    if not match:
        return ""
    moment = datetime(int(match[1]), int(match[2]), int(match[3]))
    return f"{moment.day} {moment.strftime('%B %Y')}"


def _pack_version(pack_name: str) -> str:
    """The platform version a pack was built for, from its name
    (Batocera_43.1_BIOS_Pack.zip is 43.1)."""
    stem = pack_name.removesuffix("_BIOS_Pack.zip")
    return stem.rsplit("_", 1)[1] if "_" in stem else ""


def release_line(platform_name: str, record: dict) -> list[str]:
    """Which release the buttons serve, with its checksums and signature, and
    the version gap when the page describes a newer platform than the pack."""
    tag = record.get("tag")
    pack_name = release_record.pack_name_for(platform_name, record)
    if not tag or not pack_name:
        return []
    base = f"{REPO_URL}/releases/download/{tag}"
    built = _release_date(tag)
    lines = [
        f"Pack {tag}{f', built {built}' if built else ''}. "
        f"[SHA256SUMS.txt]({base}/SHA256SUMS.txt) "
        f"[signature]({base}/SHA256SUMS.txt.sig)",
    ]
    config = load_platform_config(platform_name, "platforms")
    page_version = str(config.get("version") or "")
    pack_version = _pack_version(pack_name)
    if page_version and pack_version and page_version.lstrip("v") != pack_version.lstrip("v"):
        display = config.get("platform", platform_name)
        lines.append(f"This pack was built for {display} {pack_version}. The command "
                     f"installs the files for {display} {page_version}.")
    return lines


def _pack_buttons(platform_name: str, label: str = "Pack") -> str:
    """Buttons to the files of the platform's pack in the recorded release,
    and the release they come from under them."""
    record = release_record.load_record()
    buttons = download_links(
        platform_name,
        record,
        REPO_URL,
        label,
        button=lambda text, url: markup.button(text, url, primary=True),
    )
    return "\n\n".join([buttons, *release_line(platform_name, record)])


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


def _verify_outcomes(
    coverage: dict, sha1_by_path: dict[str, str]
) -> Callable[[str, str], dict]:
    """For each declared entry, in declaration order: what verify found.

    Returns a function of (system, name) giving the next outcome for that
    pair: status, category, the collection path that answers the entry and
    that file's sha1. The CSV gave the declaration and nothing about it.
    """
    details: dict[tuple[str, str], list[dict]] = {}
    for detail in coverage.get("details") or []:
        details.setdefault((detail.get("system", ""), detail.get("name", "")), []).append(detail)
    taken: dict[tuple[str, str], int] = {}

    def outcome(system: str, name: str) -> dict:
        found = details.get((system, name)) or [{}]
        nth = taken.get((system, name), 0)
        taken[(system, name)] = nth + 1
        detail = found[min(nth, len(found) - 1)]
        path = str(detail.get("path") or "") if detail.get("status") != "missing" else ""
        return {
            "status": detail.get("status", ""),
            "category": detail.get("category", ""),
            "collection_path": path,
            "resolved_sha1": sha1_by_path.get(path, "") if path else "",
        }
    return outcome


def _sha1_by_path(db: dict | None) -> dict[str, str]:
    return {str(e.get("path", "")): sha1 for sha1, e in ((db or {}).get("files") or {}).items()}


def _platform_export_rows(
    coverages: dict, db: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Normalized platform summaries and one row per declared file."""
    items: list[dict] = []
    file_rows: list[dict] = []
    sha1_by_path = _sha1_by_path(db)
    for key, coverage in sorted(coverages.items()):
        config = coverage["config"]
        outcome = _verify_outcomes(coverage, sha1_by_path)
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
                    **outcome(system_id, entry.get("name", "")),
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
    from common import platform_declarations, standalone_set
    from validation import case_folded_slots

    # The command line passes these too: without them a RetroBat file named
    # 32X_G_BIOS.BIN read as missing for a core opening 32X_G_BIOS.bin.
    folded = frozenset().union(*(
        case_folded_slots(cov["config"], platform_declarations(cov["config"], db))
        for cov in coverages.values()
    ))
    return run_cross_reference(
        unique_profiles, declared, db,
        data_names=data_names, all_declared=all_declared,
        standalone_cores=set().union(
            *(standalone_set(cov["config"]) for cov in coverages.values())
        ),
        folded_slots=folded,
    )


def _gap_export_rows(
    coverages: dict, gap_report: dict | None = None, db: dict | None = None,
    profiles: dict | None = None,
) -> list[dict]:
    """Every gap the site reports, both layers, in one table.

    ``platform`` rows are anomalies against a platform's own BIOS list.
    ``emulator`` rows are files a profiled core loads that no platform
    declares, which is the larger number the gap analysis page leads with.
    A `layer` column keeps the two apart instead of publishing only one.
    """
    rows: list[dict] = []
    sha1_by_path = _sha1_by_path(db)
    for key, coverage in sorted(coverages.items()):
        for detail in coverage.get("details", []):
            if detail.get("status") == "ok" and not detail.get("discrepancy"):
                continue
            path = str(detail.get("path") or "") if detail.get("status") != "missing" else ""
            rows.append({
                "category": detail.get("category", "bios"),
                "collection_path": path,
                "resolved_sha1": sha1_by_path.get(path, "") if path else "",
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
        categories = _profile_categories((profiles or {}).get(emu_key) or {})
        for gap in data.get("gap_details", []) or []:
            rows.append({
                "category": categories.get(
                    (gap.get("name", ""), gap.get("path") or gap.get("name", "")), ""),
                "collection_path": "",
                "resolved_sha1": "",
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
                "category": categories.get(
                    (entry.get("name", ""), entry.get("path") or entry.get("name", "")), ""),
                "collection_path": "",
                "resolved_sha1": "",
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
    # Two declarations that differ only in what the row does not carry are
    # one row: AmberELEC's hardwareconfig.xml was printed twice.
    unique: dict[tuple, dict] = {}
    for row in rows:
        unique.setdefault(tuple(sorted((k, str(v)) for k, v in row.items())), row)
    return list(unique.values())


def _profile_categories(profile: dict) -> dict[tuple[str, str], str]:
    """(name, path) -> the category a profile gives the entry, bios by default."""
    found: dict[tuple[str, str], str] = {}
    for entry in profile.get("files") or []:
        if isinstance(entry, dict) and entry.get("name"):
            name = str(entry["name"])
            found.setdefault((name, str(entry.get("path") or name)),
                             str(entry.get("category") or "bios"))
    return found


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
                sha1 TEXT, sha256 TEXT, md5 TEXT, crc32 TEXT,
                status TEXT, category TEXT, collection_path TEXT, resolved_sha1 TEXT
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
                destination TEXT, expected TEXT, category TEXT,
                collection_path TEXT, resolved_sha1 TEXT
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
        commit = _source_commit()
        metadata = {
            "schema_version": "1",
            "generated_at": str(db.get("generated_at") or _timestamp()),
            **({"commit": commit} if commit else {}),
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
            ":variant_group, :size, :sha1, :sha256, :md5, :crc32, :status, :category, "
            ":collection_path, :resolved_sha1)",
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
            ":status, :required, :in_repo, :reason, :discrepancy, :destination, :expected, "
            ":category, :collection_path, :resolved_sha1)",
            gap_rows,
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    os.replace(temp_path, destination)


def write_single_item(
    path: Path, kind: str, item: dict, generated_at: str, commit: str = "",
) -> None:
    """One item in the envelope of its collection, for a page to link."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(
        str(path),
        json.dumps(
            {"schema_version": 1, "generated_at": generated_at,
             **({"commit": commit} if commit else {}), "kind": kind,
             "count": 1, "items": [item]},
            indent=2, sort_keys=True, ensure_ascii=False, default=str,
        ) + "\n",
    )


def system_export_item(slug: str, manufacturer: str, consoles: dict[str, list]) -> dict:
    """A system page's files by console, with the hashes its cards show."""
    fields = ("name", "path", "size", "sha1", "md5", "sha256", "crc32")
    return {
        "id": slug,
        "name": manufacturer,
        "consoles": {
            console: [
                {field: entry[field] for field in fields if entry.get(field) is not None}
                for entry in sorted(entries, key=lambda e: (e.get("name", ""), e.get("sha1", "")))
            ]
            for console, entries in sorted(consoles.items())
        },
    }


@functools.lru_cache(maxsize=1)
def _source_commit() -> str:
    """The commit of the checkout the site is built from, empty outside one."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"  no commit recorded in the exports: {error}", file=sys.stderr)
        return ""
    commit = result.stdout.strip()
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ""


# One row per dump a catalog describes and the collection lacks: the list
# the provenance page publishes under "Wanted".
WANTED_FIELDS = ("catalog", "dat", "name", "description", "size", "crc32", "md5", "sha1")


def _provenance_wanted_rows(db: dict) -> list[dict]:
    """The Wanted list of the provenance page, as rows. A dump the
    collection already produces (derivable.json) is not wanted."""
    derivable = load_derivable()
    rows: list[dict] = []
    for catalog, data in build_report(db, load_provenance_snapshots()).items():
        for entry in data["missing"]:
            if (entry.get("sha1") or "").lower() in derivable:
                continue
            for dat in entry.get("dats") or [entry.get("dat", "")]:
                rows.append({
                    "catalog": catalog, "dat": dat,
                    **{f: _json_text(entry.get(f)) for f in WANTED_FIELDS[2:]},
                })
    rows.sort(key=lambda r: (r["catalog"], r["dat"], r["name"], r["sha1"]))
    return rows


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
    # The revision the data was read at: a tool comparing two downloads
    # could not tell which commit each came from.
    commit = _source_commit()
    stamp = {"commit": commit} if commit else {}
    platform_items, platform_files = _platform_export_rows(coverages, db)
    emulator_items = _emulator_export_items(profiles)
    gap_rows = _gap_export_rows(coverages, gap_report, db, profiles)
    cross_rows = _cross_reference_export_rows(coverages, profiles)

    members = data_directory_members()
    locations = file_location_items(db, profiles, members)
    envelopes = {
        "platforms.json": ("platforms", platform_items),
        "emulators.json": ("emulators", emulator_items),
        "gaps.json": ("verification-and-coverage-gaps", gap_rows),
        "file-locations.json": ("file-locations", locations),
    }
    write_lookup_shards(
        api, file_lookup_records(db, coverages, profiles, locations, members), generated_at,
        commit,
    )
    for filename, (kind, items) in envelopes.items():
        document = {
            "schema_version": 1,
            "generated_at": generated_at,
            **stamp,
            "kind": kind,
            "count": len(items),
            "items": items,
        }
        write_if_changed(
            str(api / filename),
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    # One platform without the others, as api/v1/emulators/<id>.json is one
    # profile: platforms.json is 2.8 MB for twelve platforms.
    for item in platform_items:
        write_single_item(api / "platforms" / f"{item['id']}.json",
                          "platforms", item, generated_at, commit)
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
                "sha256", "md5", "crc32", "status", "category",
                "collection_path", "resolved_sha1",
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
                "discrepancy", "destination", "expected", "category",
                "collection_path", "resolved_sha1",
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
    write_if_changed(
        str(downloads / "provenance-wanted.csv"),
        _csv_document(list(WANTED_FIELDS), _provenance_wanted_rows(db)),
    )

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
        (downloads / "provenance-wanted.csv", "Catalogued dumps the collection lacks", "text/csv", None),
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
        **stamp,
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
        "# Data and API",
        "",
        "RetroBIOS publishes the same metadata used by its verifier, pack builder "
        "and website as versioned static files. No account or API key is required.",
        "",
        " ".join([
            markup.button("API catalog", "api/v1/catalog.json", primary=True),
            markup.button("SQLite snapshot", "downloads/retrobios.sqlite"),
            markup.button("Collection DAT", "downloads/retrobios.dat"),
        ]),
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
        "Every JSON document carries `schema_version`, and `commit`, the revision "
        "of this repository the data was read at. Breaking changes use a new "
        "URL prefix (`/api/v2/`), and fields may only be added compatibly within v1.",
        "",
        "One emulator profile without the whole list: "
        "`api/v1/emulators/<id>.json`, the envelope of `emulators.json` holding "
        "that one item, its `profile` valid against "
        "[`emulator.schema.json`](api/v1/schemas/emulator.schema.json). Each "
        "emulator page links its own. `api/v1/platforms/<id>.json` holds one "
        "platform of `platforms.json`, and `api/v1/systems/<slug>.json` the files "
        "of one system page by console. Each platform and system page links its own.",
        "",
        "One hash without the whole list: `api/v1/lookup/<xx>.json`, where `<xx>` "
        "is the first two characters of the hash in lowercase, holds every SHA1, "
        "MD5, CRC32 and SHA256 starting with them. Each record names its `layer` "
        "(the collection, a data directory, a platform's list or an emulator "
        "profile), the file `name` and the `url` of the page that shows it. "
        "[Find a file](files/index.md) reads it.",
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
        "totals. Each row is listed once.",
        "",
        "`platform-files.csv` and the gaps add what the check found: `status` "
        "(`ok`, `untested`, `missing`), `category` (`bios`, `game_data`, "
        "`unread`), and for a held file `collection_path` and `resolved_sha1`, "
        "the collection file that answers the declaration. "
        "`provenance-wanted.csv` is the Wanted list of [Dump provenance]"
        "(provenance.md): each dump a catalog describes and the collection lacks.",
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
        "platforms/index.md": "Your setup",
        "systems/index.md": "Systems",
        "emulators/index.md": "Emulators",
        "wiki/index.md": "Reference",
        "explore.md": "Explore",
    }
    if key in index_titles:
        return index_titles[key]
    if key in PART_PAGES:
        return title
    if key.startswith("emulators/"):
        return f"{title} emulator firmware"
    if key.startswith("systems/"):
        return f"{title} systems"
    return title


def _plain_markdown(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    # Code spans keep their text whole: validate_schemas.py read
    # validateschemas.py once every underscore was taken for emphasis.
    pieces = re.split(r"(`[^`]*`)", text)
    text = "".join(
        piece.strip("`") if piece.startswith("`")
        else re.sub(r"(?<!\w)_+|_+(?!\w)", "", re.sub(r"[*~]", "", piece))
        for piece in pieces
    )
    return re.sub(r"\s+", " ", text).strip()


def _shorten(plain: str, limit: int = 157) -> str:
    """A description cut at a word, never inside one: six snippets ended
    in "op..." or "Fo..."."""
    if len(plain) <= limit:
        return plain
    cut = plain[: limit + 1].rsplit(" ", 1)[0] if " " in plain[:limit] else plain[:limit]
    return cut.rstrip(" ,;:-(.") + "..."


def _page_description(markdown: str, relative: Path, title: str) -> str:
    key = relative.as_posix()
    specific = {
        "index.md": "The BIOS files your emulators need, for RetroArch, Batocera, EmuDeck and more: the folder they go in, one command that installs and checks them, and packs to download.",
        "data.md": "Versioned RetroBIOS JSON API, CSV exports, SQLite snapshot, schemas, checksums and data interpretation guidance.",
        "cross-reference.md": "Every emulator core each retrogaming platform runs, the systems it emulates and the upstream project it comes from.",
        "gaps.md": "Current RetroBIOS verification gaps, platform-to-emulator divergences, missing files and documented source limitations.",
        "exports.md": "Each platform's own BIOS list (System.dat, batocera-systems, es_bios.xml and the rest) with the hashes and files the emulator source code shows, offered to its maintainers.",
        "provenance.md": "Hash-based comparison of RetroBIOS metadata with No-Intro, Redump and TOSEC catalog snapshots.",
        "explore.md": "Systems, emulators and installer options to explore, with the figures of the BIOS collection.",
        "which-pack.md": "Pick your device or setup and get its BIOS files: the folder they go in, one command that installs and checks them, or a ZIP pack to extract.",
        "contributing.md": "Add a BIOS file the collection lacks from the browser, checked on arrival, or add a platform or an emulator profile.",
    }
    if key in specific:
        return specific[key]
    if key in PART_PAGES:
        return (f"{title}: every file with its hashes, size, source references "
                "and who reads it.")
    if key.startswith("platforms/") and relative.stem != "index":
        return (f"{title}: where the BIOS files go, the command that installs them, the pack, "
                "and every file by system with its hashes.")
    if key.startswith("emulators/") and relative.stem != "index":
        # Pinned only when the profile names the revision it was read at:
        # 48 pages without one called themselves source-pinned.
        about = PAGE_DATA.get(key, {}).get("about", {})
        pinned = any(a.get("version") for a in (about if isinstance(about, list) else [about]))
        kind = "source-pinned emulator firmware profile" if pinned else "emulator firmware profile"
        if not PAGE_DATA.get(key, {}).get("hasPart"):
            # 152 cores that load no file announced paths and hashes.
            kind = kind.replace(" firmware", "")
            trees = (" The data directories it reads are listed."
                     if "\n## Data directories\n" in markdown else "")
            return f"{title}: {kind}. No BIOS or firmware file is required.{trees}"
        return f"{title}: {kind} with paths, hashes, requirements and validation behavior."
    if key.startswith("systems/") and relative.stem != "index":
        return f"{title}: indexed firmware files, hashes, variants, provenance and platform or emulator usage."

    paragraphs = re.split(r"\n\s*\n", markdown)
    for paragraph in paragraphs:
        stripped = paragraph.strip()
        if (
            not stripped
            or stripped.startswith(("#", "|", "```", "???", ":::", "<", "- ", "* "))
        ):
            continue
        plain = _plain_markdown(stripped)
        # A paragraph that ends on a colon introduces what follows: alone,
        # it describes nothing ("One file needs no clone and no local setup:").
        if len(plain) >= 35 and not plain.endswith(":"):
            return _shorten(plain)
    return f"{title}: RetroBIOS source-traced retrogaming firmware reference."


# Structured data a page generator knows beyond its text, by page path
# relative to docs/: read by decorate_markdown_pages.
PAGE_DATA: dict[str, dict] = {}
# Pages that hold part of a page too long for one (a console of a split
# manufacturer, a run of an emulator's cards), by path relative to docs/:
# their H1 already says what they are.
PART_PAGES: set[str] = set()


SYSTEM_PARTS_IN_FULL = 250


def collection_dat(db: dict, generated_at: str) -> str:
    """The collection as a Logiqx DAT, the format clrmamepro and RomVault read.

    One game per bios/<Manufacturer>/<Console>/ folder, named
    Manufacturer\\Console, each file a rom named by its path below that
    folder, subfolders joined with a backslash as Logiqx DATs write them. An auditor checking a BIOS tree against the
    collection had JSON, CSV and SQLite, and a converter to write.
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    games: dict[str, list[dict]] = {}
    files = db.get("files", {})
    # Every copy, not the one path the database keeps: an audit of bios/
    # read the 2,502 others as unknown files, and a fix moved them away
    # from the engines that read them by path.
    for sha1, paths in collection_paths(db).items():
        for path in sorted(paths):
            parts = path.split("/")
            folder = parts[1:3] if len(parts) > 3 else parts[1:2]
            # A backslash is how a Logiqx set name writes a subfolder: named
            # "Sony - PlayStation", the set was looked for beside Sony/, and an
            # audit of a copy of bios/ found 330 sets missing.
            games.setdefault("\\".join(folder), []).append(
                {**files[sha1], "_rom": "\\".join(parts[1 + len(folder):])}
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
        # A page of cards carried up to 2.6 MB of JSON-LD (game-engines), a
        # quarter of its weight: past the cap a page lists its
        # catalogue-verified files and points at the full table.
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

    An entry that declares a hash is addressed by it: its anchor depends on
    its own content, never on the entries beside it, and a second version
    added under the same path moves nothing. An entry without one keeps
    its plain anchor when its path is declared once, and otherwise takes
    its size, else, when even the size is shared, its description. The
    page and its JSON-LD both read this, so a structured part links to its
    card. The plain anchor of a hashed card stays reachable through
    emulator_file_aliases.
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

    keys = [
        (b, _version_key(f)) if b in repeated or _first_hash_value(f) else (b, "")
        for f, b in zip(files, bases)
    ]
    used: dict[str, int] = {}
    anchors = []
    for f, (base, key) in zip(files, keys):
        anchor = f"{base}-{key}" if key else base
        if keys.count((base, key)) > 1 and f.get("description"):
            described = re.sub(r"[^a-z0-9]+", "-", str(f["description"]).lower()).strip("-")
            anchor = f"{anchor}-{described[:40].strip('-')}"
        used[anchor] = used.get(anchor, 0) + 1
        if used[anchor] > 1:
            anchor = f"{anchor}-{used[anchor]}"
        anchors.append(anchor)
    return anchors


def emulator_file_aliases(files: list[dict], anchors: list[str]) -> list[str]:
    """The plain file-<path> id each card also answers to, or "".

    A link cited as emulators/raze/#file-duke3d-grp must still land once the
    card is addressed by its hash: the first card of that path keeps the
    plain id, unless another card already is it.
    """
    taken = set(anchors)
    aliases = []
    for f, anchor in zip(files, anchors):
        base = "file-" + (
            re.sub(
                r"[^a-z0-9]+", "-",
                str(f.get("path") or f.get("standalone_path") or f.get("name", "")).lower(),
            ).strip("-") or "entry"
        )
        if base in taken or anchor == base:
            aliases.append("")
        else:
            taken.add(base)
            aliases.append(base)
    return aliases


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
    if len(parts) > SYSTEM_PARTS_IN_FULL:
        # MAME's 1,564 entries and their members were 614 KB of JSON-LD: past
        # the cap the members are left to the profile's JSON.
        for part in parts:
            part.pop("hasPart", None)
    if parts:
        data["hasPart"] = parts
    return data


# Working notes kept under docs/ during development, never part of the site,
# and no generated page lives in a directory of these names.
LOCAL_NOTE_DIRS = ("plans", "specs")


# The funnel: who each page is written for, from someone who knows nothing
# (start) to someone adding to the project (contribute). The label sits above
# the title, and the search ranks the first two levels higher.
LEVELS = {
    "start": "Start here",
    "setup": "Setup guide",
    "explore": "Details",
    "reference": "Reference",
    "contribute": "For contributors",
}
# How much the site search weighs the words of a page (Pagefind's
# data-pagefind-weight, 1 by default). Each value was measured on the
# queries of website/tools/search_gate.py.
# - Start here pages answer a beginner's words first.
# - A system page holds the file cards themselves: a file name finds its
#   card before a guide that cites the name as an example.
# - A setup guide is found by its name (the page title weighs it).
# - A guide behind the first two levels (reference, options, contributing)
#   names everything a reader types, and ranks after the page that answers.
# - A launcher, an alias or a frozen copy answers a name after the emulator.
SEARCH_WEIGHT = {"start": 10, "setup": 1.5, "reference": 0.5, "contribute": 0.5}
SYSTEM_PAGE_WEIGHT = 5
DEEP_GUIDE_WEIGHT = 0.5
SEARCH_DEMOTED: set[str] = set()
DEMOTED_WEIGHT = 0.5


def search_weight(relative: Path, level: str) -> float | None:
    """The search weight of a page, None for the default."""
    if relative.as_posix() in SEARCH_DEMOTED:
        return DEMOTED_WEIGHT
    if relative.parts[0] == "systems":
        return SYSTEM_PAGE_WEIGHT
    if relative.parts[0] == "wiki" and level not in ("start", "setup"):
        return DEEP_GUIDE_WEIGHT
    return SEARCH_WEIGHT.get(level)


_REFERENCE_PAGES = {
    "gaps.md", "cross-reference.md", "provenance.md", "exports.md", "data.md",
    "files/index.md", "wiki/data-model.md",
}


def page_level(relative: Path) -> str:
    """The level of a generated page. A wiki page states its own."""
    path = relative.as_posix()
    if path in ("index.md", "which-pack.md"):
        return "start"
    if relative.parts[0] == "platforms":
        return "setup"
    if relative.parts[0] in ("systems", "emulators") or path == "explore.md":
        return "explore"
    if path in _REFERENCE_PAGES:
        return "reference"
    if path == "contributing.md":
        return "contribute"
    return ""


def level_label(level: str) -> str:
    """The visible label of a level, plain text for the theme to style."""
    return f'<span class="rb-level rb-level-{level}">{LEVELS[level]}</span>'


def _front_matter(markdown: str) -> dict:
    """The YAML front matter of a page, empty when it has none."""
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return {}
    data = yaml.safe_load(markdown[4:end])
    return data if isinstance(data, dict) else {}


def decorate_markdown_pages(docs: Path) -> dict[str, str]:
    """Add per-page descriptions and machine-readable structured data.

    Under Starlight each page is written in its final form (front matter,
    site addresses, pinned heading ids) and the dates a page states are
    returned by address, for the sitemap.
    """
    lastmod: dict[str, str] = {}
    terms = sitestarlight.glossary_terms(_REPO_ROOT / WIKI_SRC_DIR / "glossary.md")
    for path in sorted(docs.rglob("*.md")):
        relative = path.relative_to(docs)
        if any(part in LOCAL_NOTE_DIRS for part in relative.parts[:-1]):
            continue
        original = path.read_text(encoding="utf-8")
        authored = _front_matter(original)
        if "head" in authored:
            # Written in its final form by an earlier pass and rewritten by
            # nothing since: a second pass would find no title to read.
            continue
        markdown = prepare_page(original, relative)
        for image_id in unresolved_images(markdown):
            print(f"  WARNING: {relative}: no diagram named {image_id!r}", file=sys.stderr)
        stripped = _undecorated(markdown)
        if stripped is not markdown:
            markdown = stripped
        elif authored:
            # A wiki page states its own level: keep it, wrap the body.
            markdown = markdown[markdown.find("\n---\n", 4) + 5:].lstrip("\n")
        level = authored.get("level") or page_level(relative)

        title = _page_title(markdown, relative)
        browser_title = _browser_title(relative, title)
        description = _page_description(markdown, relative, title)
        if relative.name == "index.md":
            page_path = "" if len(relative.parts) == 1 else "/".join(relative.parts[:-1]) + "/"
        else:
            page_path = relative.with_suffix("").as_posix() + "/"
        # Encoded as mkdocs encodes the canonical link and the sitemap:
        # idtech4a++ named .../idtech4a++/ here and .../idtech4a%2B%2B/ there.
        canonical = urllib.parse.urljoin(SITE_URL, urllib.parse.quote(page_path))
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
        target, text = sitestarlight.finalize_page(
            relative.as_posix(), markdown, level=level,
            weight=search_weight(relative, level),
            browser_title=browser_title,
            description=description, structured=structured, terms=terms,
            search=authored.get("search") != "exclude",
            logo=str(authored.get("logo") or ""),
        )
        write_decorated(docs / target, text)
        # One file per page: a page that gained or lost its components
        # changed extension, and the other one would claim its address.
        stem = relative.with_suffix("").as_posix()
        for suffix in (".md", ".mdx"):
            if f"{stem}{suffix}" != target:
                (docs / f"{stem}{suffix}").unlink(missing_ok=True)
        if structured.get("dateModified"):
            lastmod[sitestarlight.page_url(target)] = str(structured["dateModified"])
    return lastmod


# Platform pages


def generate_platform_index(coverages: dict, registry: dict | None = None) -> str:
    """The Your setup tab: every platform by the device it runs on, then the
    figures of each one under a reference heading."""
    registry = registry or {}
    total_present = sum(c["present"] for c in coverages.values())

    lines = [
        "# Your setup",
        "",
        "Pick what you play on. Its page gives the folder, the command and the ZIP.",
        "",
    ]
    for group, (label, description) in DEVICE_GROUPS.items():
        members = _group_members(coverages, registry, group)
        if not members:
            continue
        # One setup: its card says what the group description would.
        lines += [f"## {label}", "", *([description, ""] if len(members) > 1 else [])]
        lines += markup.card_grid([
            (f"[{coverages[name]['platform']}]({name}.md)", [GUIDES[name].summary])
            for name in members
        ])

    lines += [
        "## Reference: every platform at a glance { #reference }",
        "",
        level_label("reference"),
        "",
        f"{setup_count()} supported platforms with {total_present:,} verified files.",
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
        status = registry.get(name, {}).get("status", "active")
        if status == "archived":
            archived_any = True
            status_html = '<span class="rb-badge rb-badge-muted">archived</span>'
        else:
            status_html = '<span class="rb-badge rb-badge-success">active</span>'

        lines.append(
            f"| [{display}]({name}.md) | "
            f"{cov['present']:,} | {_released_files(name)} | "
            f"{mode_html} | {status_html} | "
            f"{_pack_cell(name, release_record.load_record(), '../')} |"
        )

    lines.extend(
        [
            "",
            "The BIOS list is what the platform publishes. The pack adds the "
            "files its emulators load and the data directories they read, "
            "which is the count an extraction shows. "
            "Checked by is the test each platform runs on its own, replicated "
            "from its source code.",
            "",
            "Reference: [How each platform checks files](../wiki/verification-modes.md).",
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


# A platform system whose file list passes this much Markdown gets a page
# of its own: RetroDECK's arcade list was 750 KB of a 1.1 MB page.
PLATFORM_SECTION_BUDGET = 200_000
# A system's own page in runs of at most this much Markdown: RetroDECK's
# arcade list, each file with its served copy and pinned readers, rendered
# 1.4 MB of HTML on one page.
PLATFORM_PART_BUDGET = 350_000
# The Markdown of a platform page's file lists, past which the largest
# systems move to pages of their own.
PLATFORM_REFERENCE_BUDGET = 250_000


def _file_runs(body: list[str], budget: int) -> list[list[str]]:
    """A system's file list in runs of at most ``budget`` characters.

    The lines before the first file (counts, emulators) open every run. A
    file is never cut from its hash and evidence lines.
    """
    first = next((i for i, line in enumerate(body) if line.startswith("- ")), len(body))
    head, items = body[:first], []
    for line in body[first:]:
        if line.startswith("- ") or not items:
            items.append([line])
        else:
            items[-1].append(line)
    runs: list[list[str]] = [[]]
    size = 0
    for item in items:
        weight = sum(len(line) + 1 for line in item)
        if runs[-1] and size + weight > budget:
            runs.append([])
            size = 0
        runs[-1].extend(item)
        size += weight
    return [head + run for run in runs]


@functools.lru_cache(maxsize=1)
def _collection_sha1_by_path() -> dict[str, str]:
    """The sha1 of each file the collection holds, by its path."""
    with open(_REPO_ROOT / "database.json", encoding="utf-8") as handle:
        files = json.load(handle).get("files", {})
    return {str(entry.get("path", "")): sha1 for sha1, entry in files.items()}


def _system_heading_link(sys_id: str, prefix: str = "") -> str:
    """A system's name, linked to its own console section when it has one.

    The id stays beside it in small text: it is what the platform's list
    writes. A system with no section of its own is named, never sent to the
    page a storage folder chose.
    """
    from siterender import system_section

    name = _escape_tags(system_name(sys_id))
    section = system_section(sys_id)
    if section is not None:
        slug, console = section
        name = f"[{name}]({console_href(slug, console, prefix)})"
    return f"{name} <small>{_escape_tags(sys_id)}</small>"


def _served_line(f: dict) -> str:
    """Which collection file answers the entry, from the resolver's result.

    "OK" with no path gave an expert nothing to check: Batocera's
    scph1001.bin is served by bios/Sony/PlayStation/scph1001_v20.bin.
    """
    path = str(f.get("path") or "")
    if f.get("status") == "missing":
        return "Not held"
    if not path:
        # A slot the pack fills from a release asset or leaves to the user.
        return ""
    sha1 = _collection_sha1_by_path().get(path, "")
    label = "Served by" if f.get("status") == "ok" else "Held, not the declared hash"
    parts = path.split("/")
    if not sha1 or len(parts) < 3 or parts[0] != "bios":
        return f"{label}: {_code(path)}"
    # The card of that copy: every hash, provenance and the download.
    page = urllib.parse.quote(card_page(path, sha1))
    return (f"{label}: [{_code(path)}](../systems/{page}.md#sha1-{sha1[:12]}) "
            f"(sha1 `{sha1[:8]}`)")


# Platforms whose launcher records are read from the repository and revision
# of their BIOS list: batocera_scraper reads configgen at its tag,
# retrobat_scraper reads emulatorLauncher at the list's commit
# (launcher_revision). The others read another repository.
_LAUNCHERS_IN_LIST_REPO = frozenset({"batocera", "retrobat"})


def _launcher_refs(platform: str, config: dict) -> dict[str, list[str]]:
    """Entry name, casefolded -> each launcher record that opens it, rendered."""
    from launchers import _names as launcher_names
    from sitesources import launcher_ref_markdown, list_source

    source = (list_source(str(config.get("source") or ""))
              if platform in _LAUNCHERS_IN_LIST_REPO else None)
    found: dict[str, list[str]] = {}
    for record in config.get("launchers") or []:
        if not isinstance(record, dict) or not record.get("source_ref"):
            continue
        text = (f"{_escape_tags(str(record.get('emulator', '')))} at "
                f"{launcher_ref_markdown(source, str(record['source_ref']))}")
        for name in launcher_names(record):
            names = found.setdefault(name.rsplit("/", 1)[-1].casefold(), [])
            if text not in names:
                names.append(text)
    return found


def _render_platform_systems(
    by_system: dict, config_files: dict, emulator_files: dict,
    platform: tuple[str, str] | None = None, moved: dict[str, str] | None = None,
    config: dict | None = None,
) -> list[str]:
    """One collapsible block per system, listing every file it needs.

    Split out of generate_platform_page: the block is most of that
    function's branching, since each file renders a different set of
    hashes, sizes and provenance depending on what the platform declares.
    With ``platform`` (its key and name) and ``moved``, a list longer than
    PLATFORM_SECTION_BUDGET goes to a page of its own, written into
    ``moved`` by stem, and the system's heading stays with a link to it.
    """
    lines: list[str] = []
    # Per-system detail sections (collapsible for large platforms)
    use_collapsible = len(by_system) > 10
    by_system_declared = _declared_by_system(config_files)
    launched = _launcher_refs(platform[0] if platform else "", config or {})

    sections: list[dict] = []
    for sys_id, files in sorted(by_system.items()):
        ok_count = sum(1 for f in files if f["status"] == "ok")
        total = len(files)

        sys_emus = _system_emulators(sys_id, emulator_files, by_system_declared.get(sys_id))
        anchor = sys_id.replace(" ", "-")
        body = [f"{ok_count}/{total} files verified"]
        if sys_emus:
            emu_links = ", ".join(_emulator_link(e, "../") for e in sorted(sys_emus))
            body[-1] += "\\"
            body.append(f"Emulators: {emu_links}")
        body.append("")

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
            # Not every entry is firmware: the platform may list a game's data
            # or a file no core it runs opens.
            kind = {"game_data": "game data",
                    "unread": "no core the platform runs opens it"}.get(f.get("category", ""))
            if kind:
                status_display += f", {kind}"

            size_str = _fmt_size(size) if size else ""
            details = [status_display]
            if size_str:
                details.append(size_str)

            # Where the file goes, when its name alone does not say: three
            # GameCube IPL.bin differ only by GC/USA, GC/EUR and GC/JAP.
            destination = str(cfg_entry.get("destination") or "")
            where = f" at `{destination}`" if destination and destination != fname else ""
            body.append(f"- `{fname}`{where} - {', '.join(details)}")
            item_line = len(body) - 1
            facts: list[str] = []
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
                facts.append(" | ".join(hash_parts))
            served = _served_line(f)
            if served:
                facts.append(served)
            if f.get("category_evidence"):
                facts.append(_category_evidence_md(
                    f["category_evidence"], launched.get(fname.rsplit("/", 1)[-1].casefold())))
            # One line each, hashes, the copy that serves it, its readers: as
            # continuation lines they ran into one paragraph.
            if facts:
                body[item_line] += "\\"
            body.extend(f"    {fact}" + ("\\" if n < len(facts) - 1 else "")
                        for n, fact in enumerate(facts))

        # Measured as the Markdown the split was tuned on, a collapsible
        # block indented by four, so both engines split the same systems.
        size = sum(len(line) + 1 for line in body)
        if use_collapsible:
            status_tag = "OK" if ok_count == total else _count(total - ok_count, "issue")
            title = f"{_count(total, 'file')} ({status_tag})"
            size += len(f'??? note "{title}"') + 2 + 4 * sum(1 for line in body if line)
            block = markup.details("note", title, body)
            while block and not block[-1]:
                block.pop()
        else:
            block = body
        sections.append({"sys_id": sys_id, "anchor": anchor, "body": body, "block": block,
                         "size": size, "total": total, "ok": ok_count})

    # A list longer than its budget goes to a page of its own, then the
    # largest lists until the page's reference fits PLATFORM_REFERENCE_BUDGET:
    # with each file's served copy and pinned readers, Batocera's 97 systems
    # rendered 785 KB of HTML on one page.
    own_page: set[str] = set()
    if moved is not None and platform:
        own_page = {sec["sys_id"] for sec in sections if sec["size"] > PLATFORM_SECTION_BUDGET}
        kept = sum(sec["size"] for sec in sections if sec["sys_id"] not in own_page)
        for sec in sorted(sections, key=lambda sec: -sec["size"]):
            if kept <= PLATFORM_REFERENCE_BUDGET:
                break
            if sec["sys_id"] not in own_page:
                own_page.add(sec["sys_id"])
                kept -= sec["size"]

    for sec in sections:
        sys_id, anchor, body = sec["sys_id"], sec["anchor"], sec["body"]
        total, ok_count = sec["total"], sec["ok"]
        # Every system is a heading with the same anchor in both layouts:
        # emulator cards link here, and the site search splits a page at its
        # headings. Folded into blocks under one heading, every hash pasted
        # in the search led to the systems overview of a 100-block page.
        lines.append(f"### {_system_heading_link(sys_id, '../')} {{ #{anchor} }}")
        lines.append("")
        if moved is not None and platform and sys_id in own_page:
            key, display = platform
            runs = _file_runs(body, PLATFORM_PART_BUDGET)
            stems = [f"{key}-{anchor}" + (f"-{n}" if n > 1 else "")
                     for n in range(1, len(runs) + 1)]
            for n, (stem, run) in enumerate(zip(stems, runs), 1):
                part = f" (part {n} of {len(runs)})" if len(runs) > 1 else ""
                moved[stem] = "\n".join([
                    f"# {display}: {system_name(sys_id)}{part}", "",
                    f"Part of the [{display} page]({key}.md#{anchor}), whose other systems "
                    f"are listed there. Each file below is as {display} declares it, with "
                    "its status in the pack.",
                    *([""] + [", ".join(
                        f"[part {i}]({other}.md)" if other != stem else f"part {i}"
                        for i, other in enumerate(stems, 1))] if len(runs) > 1 else []),
                    "", "## Files", "",
                    *search_excluded(run),
                    "", _date_line(),
                ]) + "\n"
            where = (f"[a page of their own]({stems[0]}.md)" if len(stems) == 1 else
                     "pages of their own: " + ", ".join(
                         f"[part {i}]({stem}.md)" for i, stem in enumerate(stems, 1)))
            lines.extend([
                f"{_count(total, 'file')}, {ok_count} verified: listed on {where}.", "",
            ])
            continue
        # The section's heading stays searchable, its file list with every
        # hash is the lookup's: retrodeck's arcade list was 495 KB of index.
        lines.extend(search_excluded(sec["block"]))

    return lines


# Platforms with an updated file published by export_native.py.
_EXPORTED: set[str] = set()


def _platform_data_links(name: str) -> str:
    """The platform's own data, machine-readable: the YAML transcribed from
    its upstream list, this page's JSON and the manifest the installer reads.

    An emulator page linked its YAML and JSON, a platform page nothing: a
    tool had to download all twelve platforms in platforms.json and filter.
    """
    links = [
        f"[YAML]({REPO_URL}/blob/main/platforms/{name}.yml)",
        f"[JSON](../api/v1/platforms/{name}.json)",
    ]
    if (_REPO_ROOT / "install" / f"{name}.json").is_file():
        links.append(f"[install manifest]({REPO_URL}/blob/main/install/{name}.json)")
    return ", ".join(links)


@functools.lru_cache(maxsize=1)
def _site_profiles() -> dict[str, dict]:
    """Every emulator profile, read once for the citations of a page."""
    return load_emulator_profiles(str(_REPO_ROOT / "emulators"))


def _category_evidence_md(evidence: dict, launched: list[str] | None = None) -> str:
    """What establishes an entry's category, as the page shows it.

    ``launched`` holds the platform's launcher records that open the entry,
    already rendered: "(by launcher)" alone named no record and no line.
    """
    if evidence.get("read_by"):
        groups: dict[bool, list[str]] = {True: [], False: []}
        bare: list[str] = []
        profiles = _site_profiles()
        for reader in evidence["read_by"]:
            # Pinned links, the repository named in words: the refs were
            # plain code with the role word ("source") left in front.
            profile = profiles.get(reader["emulator"], {})
            refs = [
                (f"`{e['name']}` at {_source_ref_markdown(profile, e['source_ref'])}"
                 if e.get("source_ref") else f"`{e['name']}`")
                + (" (by name)" if e.get("by") == "name" else "")
                for e in reader.get("entries", [])
            ]
            if reader["emulator"] == "launcher":
                name = "the platform's launcher"
                refs = list(launched or [])
            else:
                name = _emulator_link(reader["emulator"], "../")
            groups[bool(reader.get("runs_system"))].append(
                f"{name} ({', '.join(refs)})" if refs else f"{name} (by {reader['proof']})")
            if not reader.get("runs_system"):
                # A launcher record is this platform's own code: kept whole.
                bare.append(f"{name} ({', '.join(refs)})"
                            if reader["emulator"] == "launcher" and refs else name)
        if not groups[True]:
            return "Read by " + ", ".join(groups[False])
        text = "Read by " + ", ".join(groups[True])
        # Cores of other systems that share the name: named, not cited, as
        # their lines describe another machine's file.
        if bare:
            text += ", also named by " + ", ".join(bare)
        return text
    examined = evidence.get("cores_examined") or []
    cores = ("cores examined: " + ", ".join(_emulator_link(c, "../") for c in examined)
             if examined else "no core examined")
    if evidence.get("unsettled"):
        return f"No reader found, not settled: {evidence['unsettled']} ({cores})"
    return f"No reader found ({cores})"


@functools.lru_cache(maxsize=1)
def _profiles_by_core_name() -> dict[str, list[str]]:
    """Core name as a platform writes it -> the profiles with a page for it.

    MinUI writes `mednafen_vb`, the profile and its page are `beetle_vb`.
    """
    profiles = load_emulator_profiles(str(_REPO_ROOT / "emulators"))
    with_page = {k for k, v in profiles.items() if v.get("type") not in ("alias", "test")}
    return {
        core: sorted(k for k in keys if k in with_page)
        for core, keys in upstream_profile_index(profiles).items()
    }


def _core_name_links(cores: list[str]) -> list[str]:
    """A link per core to its profile page, the bare name where none exists."""
    index = _profiles_by_core_name()
    links = []
    for core in cores:
        keys = [core] if core in index.get(core, []) else index.get(core, [])
        if keys:
            links.extend(_emulator_link(k, "../") for k in keys)
        else:
            links.append(f"`{core}`")
    return links


def _core_folders_section(name: str, config: dict) -> list[str]:
    """Each folder the frontend hands a core, and the files the pack puts in it.

    For a platform that names no file of its own (MinUI and its forks): the
    folders come from its launch scripts, the files from its install manifest.
    """
    folders = config.get("core_system_dirs") or {}
    manifest = _REPO_ROOT / "install" / f"{name}.json"
    files = json.loads(manifest.read_text())["files"] if manifest.is_file() else []
    lines = [
        "### Core folders",
        "",
        "Each folder below is the one the frontend gives that core. The pack "
        "puts the files the core reads there.",
        "",
        "| Folder | Cores | Files |",
        "|--------|-------|-------|",
    ]
    for folder, info in sorted(folders.items()):
        held = sorted(
            f["dest"][len(folder) + 1:] for f in files
            if f.get("dest", "").startswith(folder + "/")
        )
        cores = ", ".join(_core_name_links(info.get("cores", [])))
        shown = ", ".join(f"`{n}`" for n in held) if held else "none needed"
        lines.append(f"| `{folder}/` | {cores} | {shown} |")
    lines.append("")
    return lines


def _check_sentence(display: str, mode: str, config: dict, no_list: bool) -> str:
    """What the frontend itself checks, from the mode its code uses."""
    if no_list:
        return (f"{display} has no BIOS list, so it checks nothing itself. Each "
                "emulator looks for its own files when a game starts.")
    if config.get("hash_rule") == "size_and_any_hash":
        return (f"{display} checks each file's size and fingerprint too. It "
                "reports a file with the wrong size or contents.")
    if digest_algorithm(mode):
        return (f"{display} checks each file's fingerprint too. It reports a file "
                "with the wrong contents.")
    return (f"{display} only checks that a file with the right name is there. "
            "`--check` also checks what is inside.")


def _folder_rows(rows: tuple[tuple[str, str], ...], indent: str = "") -> list[str]:
    return [
        f"{indent}| Where | BIOS folder |",
        f"{indent}|-------|-------------|",
        *(f"{indent}| {label} | {path} |" for label, path in rows),
    ]


# Shown in the "On the device" tab of a guide that has no `terminal:` of its own.
DEVICE_TERMINAL_FALLBACK = "Or fill the folder from your computer, with the ZIP."


def _computer_first(name: str, guide: Guide) -> bool:
    """A system that runs only on its own device, with a ZIP to download: a
    beginner fills its folder from a computer more easily than from a
    terminal on the TV. Without a ZIP the command is the only route."""
    pack = release_record.pack_for(name, release_record.load_record())
    return guide.run == ("device",) and bool(pack and pack.get("assets"))


def _ships_neogeo(name: str) -> bool:
    """Whether the setup's files hold `neogeo.zip`, the archive a reader most
    often unzips by mistake: arcade BIOS sets are read zipped."""
    manifest = _REPO_ROOT / "install" / f"{name}.json"
    if not manifest.is_file():
        return False
    files = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
    return any(f.get("dest", "").rsplit("/", 1)[-1] == "neogeo.zip" for f in files)


def _guide_download(name: str, display: str, guide: Guide, extract_to: str | None,
                    heading: str) -> list[str]:
    """The pack: its buttons, its size, and where it is unzipped."""
    lines = [heading, ""]
    record = release_record.load_record()
    pack = release_record.pack_for(name, record)
    network = any(label.startswith("From ") for label, _path in guide.folders)
    if pack and pack.get("assets"):
        parts = len(pack["assets"])
        bullets = [
            f"{_fmt_size(pack['download_size'])} to download, "
            f"{_fmt_size(pack['extracted_size'])} once unzipped."
        ]
        if parts > 1:
            bullets.append("You need both parts." if parts == 2 else f"You need all {parts} parts.")
        if _computer_first(name, guide) and network:
            target = "the network folder above"
        else:
            target = f"`{extract_to}`" if extract_to else "your BIOS folder"
        bullets.append(f"Unzip {'them all' if parts > 1 else 'it'} into {target}."
                       + (" Files like `neogeo.zip` stay zipped." if _ships_neogeo(name) else ""))
        if guide.download_hint and not _computer_first(name, guide):
            bullets.append(guide.download_hint)
        lines += [
            _pack_buttons(name, f"Download {display} Pack"),
            "",
            *(f"- {b}" for b in bullets),
            "",
        ]
    elif record.get("tag"):
        lines += [
            f"No {display} ZIP yet. The command installs the same files.",
            "",
        ]
    else:
        lines += [_pack_buttons(name, f"Download {display} Pack"), ""]
    return lines


def _guide_install(name: str, guide: Guide, heading: str) -> list[str]:
    """The command, in a tab per place it runs, and whether it is safe."""
    lines = [
        heading,
        "",
        "The command finds the folder, downloads what is missing, and checks "
        "each file.",
        "",
    ]
    if guide.on_card:
        lines += [
            "Put the SD card in a computer. On Windows, note its letter, like "
            "**E:**. Put the card's path after `--dest`.",
            "",
        ]
    device = guide.terminal or f"{TERMINALS['device'][1]} {DEVICE_TERMINAL_FALLBACK}"
    return [
        *lines,
        *terminal_tabs(guide.run, platform_args(name, guide), how={"device": device}),
        *trust_box("../"),
    ]


def _platform_guide(name: str, display: str, cov: dict, registry: dict, no_list: bool) -> list[str]:
    """The guide part of a platform page: folder, command, pack, check, next.

    A system that runs only on its own device puts the computer route
    (download, unzip into the network folder) before the command."""
    guide = GUIDES[name]
    entry = registry.get(name, {})
    extract_to = entry.get("extract_to")
    lines = ["## Where the files go", ""]
    if guide.lead:
        lines += [guide.lead, ""]
    lines += _folder_rows(guide.folders)
    if extract_to:
        lines.append(f"| Unzip the pack into | `{extract_to}` |")
    lines.append("")
    for note in guide.notes:
        lines += [note, ""]
    lines += [line for note in extract_notes("platforms") if f"The {display} pack " in note
              for line in (note, "")]
    if guide.more_folders:
        detected = _detected_systems(registry, name) if guide.more_folders_detected else []
        lines += markup.details("note", guide.more_folders_title, [
            *_folder_rows(guide.more_folders),
            *(f"| {system} | `{folder}` |" for system, folder in detected),
            "",
            *([guide.more_folders_note, ""] if guide.more_folders_note else []),
        ])
    if guide.on_card:
        lines += ["<!-- image: minui-card -->", ""]
    if _computer_first(name, guide):
        lines += [
            *_guide_download(name, display, guide, extract_to,
                             "## From your computer { #download }"),
            *_guide_install(name, guide, "## On the device { #install }"),
        ]
    else:
        lines += [
            *_guide_install(name, guide, "## Install"),
            *_guide_download(name, display, guide, extract_to, "## Download"),
        ]
    check_args = {key: f"{args} --check" for key, args in platform_args(name, guide).items()}
    lines += [
        "## How to check",
        "",
        "Add `--check` to the command. It changes nothing.",
        "",
        *terminal_tabs(guide.run, check_args, steps=False),
        "All good when you see `0 wrong hash`.",
        "",
        _check_sentence(display, cov["mode"], cov["config"], no_list),
        "",
        *([guide.finish, ""] if guide.finish else []),
        *_next_block([
            "[First install, step by step](../wiki/first-install.md)",
            "[When a game does not start](../wiki/troubleshooting.md)",
        ]),
    ]
    return lines


# How a platform checks a file, in words.
_CHECK_WORDS = {"existence": "name only", "md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256",
                "crc32": "CRC32"}


def generate_platform_page(
    name: str,
    cov: dict,
    registry: dict | None = None,
    emulator_files: dict | None = None,
    moved: dict[str, str] | None = None,
) -> str:
    """A guide first (folder, command, pack, check), then every file the
    platform's list and its emulators need, under a reference heading.

    ``moved`` collects the pages of systems whose list is too long for it.
    """
    from sitesources import list_source

    registry = registry or {}
    config = cov["config"]
    display = cov["platform"]
    mode = cov["mode"]
    pct = _pct(cov["present"], cov["total"])

    # The mark is drawn beside the title from the front matter: floated
    # beside the first line at 48 px it read as a broken image.
    logo_url = registry.get(name, {}).get("logo", "")
    logo_url = local_images().get(logo_url, logo_url)

    homepage = config.get("homepage", "")
    version = config.get("version", "")
    hash_type = config.get("hash_type", "")
    base_dest = config.get("base_destination", "")

    mode_badge = (
        "rb-badge-success" if reads_file_contents(mode) else "rb-badge-info"
    )

    lines = [
        *(["---", f"logo: {json.dumps(logo_url)}", "---", ""] if logo_url else []),
        f"# {display}",
        "",
    ]

    if registry.get(name, {}).get("status") == "archived":
        lines.extend(markup.aside("warning", "Archived platform", [
            f"{display}'s list is kept as last read. Its pack is still built.",
        ]))

    summary = GUIDES[name].summary if name in GUIDES else ""
    if summary:
        lines += [summary + (f" [{display} website]({homepage})." if homepage else ""), ""]

    no_list = not cov["total"] and registry.get(name, {}).get("native_list") == "none"
    if name in GUIDES:
        lines += _platform_guide(name, display, cov, registry, no_list)

    lines += [
        "## Reference: every file by system { #reference-every-file-by-system }",
        "",
        level_label("reference"),
        "",
        "For checking the work: the counts, where the list comes from, and "
        "every file with its hashes.",
        "",
        *(line for note in (GUIDES[name].reference_notes if name in GUIDES else ())
          for line in (note, "")),
    ]
    if no_list:
        # MinUI and its forks hand each core a folder and name no file: a
        # 0/0 coverage band read as "this device is not supported".
        lines.extend(markup.aside("info", "No BIOS list of its own", [
            f"{display} names no BIOS file itself: it gives each emulator core "
            "its own folder and the core looks for its files there. The pack "
            "places every file the cores it runs need into those folders. The "
            "core files are listed further down this page.",
        ]))
    band = [] if no_list else [
            markup.block_open("div", 'class="rb-stats"'),
            "",
            *_stat(f"{cov['present']}/{cov['total']}", f"Coverage ({pct})"),
            *_stat(str(cov["verified"]), "Verified"),
            *_stat(str(cov["missing"]), "Missing"),
            *_stat(f'<span class="rb-badge {mode_badge}">{_CHECK_WORDS.get(mode, mode)}</span>',
                   "Checked by"),
            "</div>",
            "",
        ]
    lines.extend([*band, markup.block_open("div", 'class="rb-meta-card"'), "",
                  "| Detail | Value |", "|---|---|"])
    # The hash the list writes, when it is not the one the platform checks:
    # RetroArch checks presence and its System.dat writes sha1.
    if hash_type and hash_type != mode:
        lines.append(f"| Hashes in the list | {_CHECK_WORDS.get(hash_type, hash_type)} |")
    if version:
        lines.append(f"| Version | {version} |")
    # base_destination is only the root the platform's own list writes its
    # paths from: the pack strips it, so it is no folder to extract into.
    if base_dest:
        lines.append(f"| Paths in the platform's list start at | `{base_dest}/` |")
    if homepage:
        lines.append(f"| Homepage | [{homepage}]({homepage}) |")
    transcribed = str(config.get("source") or "")
    if transcribed:
        # The file every declaration on this page was read from: without it
        # a hash could not be traced to the revision that states it.
        listed = list_source(transcribed)
        moving = (listed.moving if listed else bool(
            re.search(r"/(?:master|main|db)(?:/|$)|refs/heads/", transcribed)))
        note = " (a branch: the revision read is not recorded)" if moving else ""
        # Named in words: the raw URL broke into 25 lines of 8 characters
        # on a phone.
        link = (f"[{_escape_tags(listed.label)}]({listed.browse()})" if listed
                else f"<{transcribed}>")
        lines.append(f"| Transcribed from | {link}{note} |")
    if name in _EXPORTED:
        lines.append(f"| Updated file | [Download it and see the changes](../exports.md#{name}) |")
    lines.append(f"| Data | {_platform_data_links(name)} |")
    contrib_list = registry.get(name, {}).get("contributed_by", [])
    if contrib_list:
        for cb in contrib_list:
            username = cb.get("username", "")
            contribution = cb.get("contribution", "")
            pr = cb.get("pr")
            pr_link = f" ([#{pr}]({REPO_URL}/pull/{pr}))" if pr else ""
            lines.append(
                f"| Contributed by | [@{username}](https://github.com/{username}):"
                f" {contribution}{pr_link} |"
            )
    lines.extend(["", "</div>"])
    if name not in GUIDES:
        detect = (registry.get(name, {}).get("install") or {}).get("detect") or []
        on_card = any(rule.get("method") == "card_marker" for rule in detect)
        lines.extend(["", *_platform_install_block(name, display, on_card)])
        lines.extend(["", _pack_buttons(name, f"Download {display} Pack")])
    lines.append("")

    # The declaration behind each detail, by system and name in declaration
    # order: keyed by name alone, the last of MiSTer's 23 boot.rom gave its
    # MD5 to every system's row.
    config_files: dict[tuple[str, str], list[dict]] = {}
    for sys_id, system in config.get("systems", {}).items():
        for fe in system.get("files", []):
            fname = fe.get("name", "")
            if fname:
                config_files.setdefault((sys_id, fname), []).append(fe)

    by_system: dict[str, list] = {}
    for d in cov["details"]:
        sys_id = d.get("system", "unknown")
        by_system.setdefault(sys_id, []).append(d)

    if no_list:
        lines.extend(_core_folders_section(name, config))

    # System summary table (quick navigation)
    lines.extend(
        [] if no_list and not by_system else [
            "### Systems overview",
            "",
            "| System | Files | Status | Emulators |",
            "|--------|-------|--------|-----------|",
        ]
    )
    by_system_declared = _declared_by_system(config_files)
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
        sys_emus = _system_emulators(sys_id, emulator_files, by_system_declared.get(sys_id))
        emu_str = ", ".join(sys_emus[:3])
        if len(sys_emus) > 3:
            emu_str += f" +{len(sys_emus) - 3}"
        anchor = sys_id.replace(" ", "-")
        # The name a reader filters on, the id the list writes beside it.
        lines.append(
            f"| [{_escape_tags(system_name(sys_id))}](#{anchor}) <small>{sys_id}</small> "
            f"| {ok_count}/{total} | {status} | {emu_str} |"
        )
    lines.append("")

    lines.extend(
        _render_platform_systems(by_system, config_files, emulator_files,
                                 (name, display), moved, config)
    )

    lines.extend(["", _date_line()])
    return "\n".join(lines) + "\n"


# System pages


def generate_files_page() -> str:
    """Find a file by any of its hashes: files/?hash=<value>.

    site.js reads the one shard of api/v1/lookup/ the value names and lists
    every page that cites it. files/?sha1=<sha1> is the card's permanent
    address and opens the card directly.
    """
    return "\n".join([
        "# Find a file",
        "",
        "A hash is a fingerprint of a file's contents ([Glossary](../wiki/glossary.md)). "
        "Tools such as clrmamepro and RomVault print one, and so do emulator logs "
        "and the BIOS lists of setups.",
        "",
        "Paste it below. The page shows the file in the collection that has it, the "
        "setups that list it and the emulators that check for it.",
        "",
        '<form class="rb-file-lookup" action="./" method="get">',
        '<label for="rb-hash">SHA1, MD5, CRC32 or SHA256</label> ',
        '<input id="rb-hash" name="hash" size="66" autocomplete="off" spellcheck="false" '
        'pattern="\\s*(0x)?([0-9a-fA-F]{8}|[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\\s*" '
        'required> ',
        '<button type="submit">Find</button>',
        "</form>",
        "",
        '<p id="rb-file-lookup-status" role="status"></p>',
        "",
        "To try it, paste `0555c6fae8906f3f09baf5988f00e55f88e9f30b`, the SHA1 of "
        "`scph5501.bin`, the PlayStation BIOS v3.0. "
        "To look a file up by its name, use the search at the top of the page.",
        "",
        "## Permanent links",
        "",
        "A file's card lives on its manufacturer's page, and a file refiled "
        "between manufacturers moves with it. `files/?sha1=<sha1>` is the address "
        "that does not move: it opens the card wherever it is now. The link icon "
        "beside each card's name is that address.",
        "",
        "## The index behind it",
        "",
        "The lookup reads one file of [`api/v1/lookup/`](../data.md), named by the "
        "first two characters of the hash, about 20 KB compressed. "
        "[`api/v1/file-locations.json`](../api/v1/file-locations.json) lists every "
        "card by SHA1 in one file.",
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


def page_data_directories(name: str, profiles: dict) -> list[dict]:
    """The data directories an emulator page lists.

    Its own, then those of the aliases that point at it: an alias has no
    page, and the 90 FreeIntv overlays its profile declares were linked to
    emulators/freeintv_ts_overlay/, a page that does not exist.
    """
    trees = [t for t in profiles[name].get("data_directories") or [] if isinstance(t, dict)]
    refs = {str(t.get("ref", "")) for t in trees}
    for alias in sorted(profiles):
        profile = profiles[alias]
        if profile.get("type") != "alias" or profile.get("alias_of") != name:
            continue
        for tree in profile.get("data_directories") or []:
            ref = str(tree.get("ref", ""))
            if isinstance(tree, dict) and ref not in refs:
                refs.add(ref)
                trees.append(tree)
    return trees


def _data_member_owners(profiles: dict) -> dict[str, str]:
    """The emulator page that lists each data directory, the first by name.

    Only a profile that has a page can own one.
    """
    owners: dict[str, str] = {}
    for name in sorted(profiles):
        if profiles[name].get("type") in ("alias", "test"):
            continue
        for tree in page_data_directories(name, profiles):
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
        page = card_page(entry.get("path", ""), sha1)
        items.append({"sha1": sha1, "url": f"systems/{page}/#sha1-{sha1[:12]}"})
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


LOOKUP_PREFIX = 2  # hex characters naming a lookup shard: 256 files


def file_lookup_records(
    db: dict,
    coverages: dict,
    profiles: dict,
    locations: list[dict],
    members: dict | None = None,
) -> list[dict]:
    """Every hash the site knows, one record per value, for files/?hash=.

    The collection's own files lead to their card, a platform's declared
    value to the system section of its page, a profile's to the entry's
    card. The site search indexed these values as text, 4 MB of the 21 MB
    it made every visitor download: the lookup reads one shard instead.
    """
    files = db.get("files", {})
    member_names = {
        str(info.get("sha1", "")): member.rsplit("/", 1)[-1]
        for tree in (members or {}).values() for member, info in sorted(tree.items())
    }
    records: list[dict] = []
    for item in locations:
        sha1 = item["sha1"]
        entry = files.get(sha1)
        if entry is None:
            # A data directory member: the manifests record its sha1 only.
            records.append({"value": sha1, "algorithm": "sha1", "layer": "data-directory",
                            "name": member_names.get(sha1, ""), "sha1": sha1,
                            "url": item["url"]})
            continue
        for algorithm in HASH_ALGORITHMS:
            for value in _hash_values(entry.get(algorithm)):
                records.append({
                    "value": value, "algorithm": algorithm, "layer": "collection",
                    "name": str(entry.get("name", "")), "sha1": sha1, "url": item["url"],
                })
    for key, coverage in sorted(coverages.items()):
        for system_id, system in sorted((coverage["config"].get("systems") or {}).items()):
            url = f"platforms/{key}/#{system_id.replace(' ', '-')}"
            for entry in system.get("files") or []:
                for algorithm in HASH_ALGORITHMS:
                    for value in _hash_values(entry.get(algorithm)):
                        records.append({
                            "value": value, "algorithm": algorithm, "layer": "platform",
                            "owner": key, "name": str(entry.get("name", "")), "url": url,
                        })
    for key, profile in sorted(profiles.items()):
        if profile.get("type") in ("alias", "test"):
            continue
        entries = profile.get("files") or []
        for entry, anchor in zip(entries, emulator_file_anchors(entries)):
            url = f"emulators/{emulator_card_page(key, anchor)}/#{anchor}"
            members = [(str(entry.get("name", "")), entry)] + [
                (str(member["name"]), member) for member in entry.get("contents") or []
                if isinstance(member, dict) and member.get("name")
            ]
            for name, holder in members:
                for algorithm in HASH_ALGORITHMS:
                    for value in _hash_values(holder.get(algorithm)):
                        records.append({
                            "value": value, "algorithm": algorithm, "layer": "emulator",
                            "owner": key, "name": name, "url": url,
                        })
    unique = {json.dumps(r, sort_keys=True): r for r in records}
    return sorted(unique.values(), key=lambda r: (
        r["value"], r["layer"], r.get("owner", ""), r["name"], r["url"]))


def write_lookup_shards(
    api: Path, records: list[dict], generated_at: str, commit: str = "",
) -> int:
    """One envelope per leading pair of hex characters, as the lookup asks.

    A value that is not hexadecimal (a typo in a profile) has no shard the
    lookup would read, and is left out. Returns the number of shards.
    """
    shards: dict[str, list[dict]] = {}
    for record in records:
        if re.fullmatch(r"[0-9a-f]{8,}", record["value"]):
            shards.setdefault(record["value"][:LOOKUP_PREFIX], []).append(record)
    folder = api / "lookup"
    folder.mkdir(parents=True, exist_ok=True)
    for prefix, items in sorted(shards.items()):
        write_if_changed(str(folder / f"{prefix}.json"), json.dumps({
            "schema_version": 1,
            "generated_at": generated_at,
            **({"commit": commit} if commit else {}),
            "kind": "file-lookup",
            "count": len(items),
            "items": items,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return len(shards)


def collection_paths(db: dict) -> dict[str, set[str]]:
    """Every path under bios/ that holds each file, by sha1.

    The database keeps one path per content. The others are the full tails
    by_path_suffix records that name a file on disk: RPG Maker, engine
    trees and the copies filed under several consoles keep 2,502 of them.
    """
    files = db.get("files", {})
    bios_dir = Path(str(db.get("bios_dir") or "bios"))
    paths: dict[str, set[str]] = {}
    for sha1, entry in files.items():
        path = str(entry.get("path", ""))
        if path.startswith("bios/") and path.count("/") >= 2:
            paths.setdefault(sha1, set()).add(path)
    for tail, sha1s in db.get("indexes", {}).get("by_path_suffix", {}).items():
        if tail.count("/") < 2 or len(sha1s) != 1 or sha1s[0] not in files:
            continue
        if (bios_dir / tail).is_file():
            paths.setdefault(sha1s[0], set()).add(f"bios/{tail}")
    return paths


def _group_by_manufacturer(db: dict) -> dict[str, dict[str, list]]:
    """Group files by manufacturer -> console -> files.

    The database keeps one path per content, and a copy filed elsewhere was
    on no page of its own: the Channel F BIOS sat under Arcade > FBNeo only,
    and Fairchild had no system page. Every folder holding a copy lists it,
    read from the full paths by_path_suffix records.
    """
    files = db.get("files", {})
    manufacturers: dict[str, dict[str, list]] = {}
    for sha1, where in _copy_places(db).items():
        for manufacturer, console in sorted({(m, c) for m, c, _ in where}):
            manufacturers.setdefault(manufacturer, {}).setdefault(console, []).append(files[sha1])
    return manufacturers


def _console_display(manufacturer: str, console: str) -> str:
    """A console section's name as the reader looks for it: the heading
    system_name gives its id, or the folder alone under a program's top
    (Other, Arcade), where "Other mame" would name nothing."""
    from siterender import _PROGRAM_TOPS, _heading_name  # noqa: PLC0415

    return console if manufacturer in _PROGRAM_TOPS else _heading_name(manufacturer, console)


def generate_systems_index(manufacturers: dict) -> str:
    """Every console in one table, found by its own name.

    The index listed 74 manufacturers and no console: someone looking for
    PlayStation had to know it is filed under Sony, and Other hid 91
    sections. Manufacturer pages stay the containers.
    """
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
        "Every BIOS file the collection holds, by console. Open a console to "
        "see each file, the emulators that read it and the setups that list it.",
        "",
        f"{_count(total_consoles, 'console')}, {_count(total_files, 'file')} in the "
        f"repository, from {_count(total_mfr, 'manufacturer')}.",
        "",
    ]

    # Other holds one folder per emulator: a7800 or altirra in the console
    # table named no console.
    def kind(mfr: str) -> str:
        if mfr in GAME_DATA_TOPS:
            return "games"
        return "programs" if mfr == "Other" else "consoles"

    for heading, group, first, note in (
        ("Consoles and computers", "consoles", "Console", ""),
        ("Emulator folders", "programs", "Folder",
         "Files kept under the emulator that reads them, not under a console."),
        ("Game engines and PC games", "games", "Name",
         "Game engines and PC games hold the files a game engine reads, such "
         "as the data of a game, rather than the BIOS of a machine."),
    ):
        rows = sorted(
            ((_console_display(mfr, console), mfr, console, len(files))
             for mfr, consoles in manufacturers.items() if kind(mfr) == group
             for console, files in consoles.items()),
            key=lambda row: (row[0].casefold(), row[1].casefold(), row[2]),
        )
        if not rows:
            continue
        lines += [f"## {heading}", ""]
        if note:
            lines += [note, ""]
        lines += [
            # The count before the maker: on a phone the last column is
            # the one scrolled off, and most names carry the maker already.
            f"| {first} | Files | Maker |",
            "|---------|------:|-------|",
        ]
        for name, mfr, console, count in rows:
            cell = name.replace("|", "-")
            href = console_href(_slug(mfr), console).removeprefix("systems/")
            lines.append(f"| [{cell}]({href}) | {count:,} | [{mfr}]({_slug(mfr)}.md) |")
        lines.append("")

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


def _date_in_words(stamp: str) -> str:
    """`2026-08-07T...` as `<time datetime="2026-08-07">7 August 2026</time>`."""
    try:
        day = datetime.strptime(str(stamp)[:10], "%Y-%m-%d")
    except ValueError:
        return _escape_tags(str(stamp))
    return f'<time datetime="{day:%Y-%m-%d}">{day.day} {day:%B %Y}</time>'


def generate_provenance_page(
    db: dict, report: dict, derivable: dict[str, dict] | None = None
) -> str:
    """Page explaining the verified dump badges and listing catalog gaps."""
    matched_files = count_catalog_matched(db)
    total_files = compute_composition(db)["systems"]["files"]
    # When each catalog was read: a 76-day-old snapshot showed no age.
    as_of = ", ".join(
        f"{_PROVENANCE_LABELS.get(source, source)} {_date_in_words(data['imported_at'])}"
        for source, data in report.items() if data.get("imported_at")
    )

    lines = [
        "# Dump provenance",
        "",
        *([f'<p class="rb-timestamp">Catalogs as of: {as_of}.</p>', ""] if as_of else []),
        "Provenance says whether a file is, byte for byte, a dump that Redump, "
        "No-Intro or TOSEC catalogues. It tells you where a dump is known from, "
        "not whether an emulator accepts it: that is the emulator's code, read "
        "in its profile. Example: `scph5501.bin` is Redump's `ps-30a.bin`.",
        "",
        f"**{matched_files:,}** of {total_files:,} system files match an entry "
        "in a dump-preservation catalog. Those files carry a "
        '<span class="rb-badge rb-badge-success">catalog</span> badge and the '
        "catalog entry's own name on the [system pages](systems/index.md), "
        'orange (<span class="rb-badge rb-badge-warning">TOSEC</span>) where '
        "TOSEC flags the dump as bad, overdumped, hacked, cracked or fixed. "
        "Arcade ROM sets and engine data "
        "sit outside what those catalogs index, so they are excluded from the "
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
        "modern console firmware updates, game data such as Diablo's `diabdat.mpq`, and "
        "arcade sets tracked by MAME driver source instead.",
        "",
        "When the two views disagree, this project follows the code, because "
        "that is what decides whether your emulator boots. The reasoning is in "
        "the [technical questions](wiki/technical-faq.md#are-these-files-verified-against-original-"
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
            f"{len(data['covered_dats'])} | {_date_in_words(data['imported_at'])} |"
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
            table = ["| Name | Description | SHA1 |", "|------|-------------|------|"]
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
                table.append(f"| `{name}` | {desc} | `{sha1}` |")
            lines.extend(markup.details("note", f"{dat} ({len(entries)})", table))

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

    lines.extend(["", _date_line()])
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


_platform_names: dict[str, str] = {}


def _platform_display(name: str) -> str:
    """A setup's name as its platform file writes it: Batocera, not batocera."""
    display = _platform_names.get(name)
    if display is None:
        try:
            config = load_platform_config(name, str(_REPO_ROOT / "platforms"))
        except FileNotFoundError:
            config = {}
        display = _platform_names[name] = str(config.get("platform") or name)
    return display


# A setup, the link to its page, and where it puts the file.
Place = tuple[str, str, list[str]]


def _platform_places(sha1: str, consumers: FileConsumers) -> list[Place]:
    """Each platform declaring the file, linked to its system section, with
    the destination it gives the file."""
    systems_of = getattr(consumers, "platform_systems", {}).get(sha1, {})
    destinations_of = getattr(consumers, "platform_destinations", {}).get(sha1, {})
    places: list[Place] = []
    for platform in consumers.platforms.get(sha1, ()):
        section = sorted(systems_of.get(platform, ()))
        fragment = f"#{section[0].replace(' ', '-')}" if section else ""
        places.append((_platform_display(platform), f"../platforms/{platform}.md{fragment}",
                       sorted(destinations_of.get(platform, ()))))
    return sorted(places, key=lambda place: place[0].casefold())


def _platform_links(sha1: str, consumers: FileConsumers) -> list[str]:
    """Each platform declaring the file, as Markdown: its name, linked to
    its system section, then the destination it gives the file."""
    return [
        f"[{name}]({href})" + (": " + ", ".join(_code(d) for d in dests) if dests else "")
        for name, href, dests in _platform_places(sha1, consumers)
    ]


_card_anchors: dict[str, tuple[list, list[str]]] = {}


def _emulator_card_anchor(emulator: str, files: list, index: int) -> str:
    """The id of a profile entry's card on its emulator page."""
    cached = _card_anchors.get(emulator)
    if cached is None or cached[0] is not files:
        cached = _card_anchors[emulator] = (files, emulator_file_anchors(files))
    return cached[1][index] if 0 <= index < len(cached[1]) else ""


def _emulator_links(sha1: str, consumers: FileConsumers) -> list[str]:
    """Each emulator reading the file, linked to the entry that reads it.

    The platform links landed on their system section, and the emulator
    links on the page top: duckstation's scph5501.bin entry, with the line
    that accepts it, sat 260 lines down a page of 105 entries.
    """
    entries = getattr(consumers, "emulator_entries", {}).get(sha1, {})
    files_of = getattr(consumers, "emulator_files", {})
    links = []
    for emulator in sorted(consumers.emulators.get(sha1, ())):
        index = entries.get(emulator)
        anchor = (_emulator_card_anchor(emulator, files_of[emulator], index)
                  if index is not None and emulator in files_of else "")
        page = emulator_card_page(emulator, anchor) if anchor else emulator
        fragment = f"#{anchor}" if anchor else ""
        links.append(f"[{emulator}](../emulators/{page}.md{fragment})")
    return links


def _installed_places(sha1: str, consumers: FileConsumers, declared: set[str]) -> list[Place]:
    """Each pack installing the file that its platform's list does not name,
    with the place the pack gives it."""
    places = [
        (_platform_display(platform), f"../platforms/{platform}.md", sorted(d for d in dests if d))
        for platform, dests in getattr(consumers, "installed", {}).get(sha1, {}).items()
        if platform not in declared
    ]
    return sorted(places, key=lambda place: place[0].casefold())


def _places_text(places: list[Place]) -> str:
    """Setups as "Lakka: scph1000.bin", the name linked to its page and the
    destination as text: a code chip per path made the line a row of
    boxes."""
    def plain(text: str) -> str:
        return html.escape(text, quote=False).replace("`", "&#96;")

    return ", ".join(
        f"[{name}]({href})" + (": " + " and ".join(plain(d) for d in dests) if dests else "")
        for name, href, dests in places
    )


def _inline_card_html(text: str) -> str:
    """A card value written in Markdown (code spans, links), as the HTML a
    fact list holds. HTML already in the value is kept."""
    out: list[str] = []
    for part in re.split(r"(`[^`]*`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
        else:
            out.append(re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', part))
    return "".join(out)


# A link icon, the way a heading anchor is drawn: the word "permalink" was
# read as part of every card title, by the search and by screen readers.
# Drawn once per page and used by each card: inline, its 400 bytes were
# 100 KB on the Sony page.
PERMALINK_SYMBOL = (
    '<svg aria-hidden="true" style="display:none"><symbol id="rb-link-icon" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
    '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></symbol></svg>'
)
_PERMALINK_ICON = ('<svg aria-hidden="true" width="16" height="16">'
                   '<use href="#rb-link-icon"/></svg>')


def _icon_symbol() -> list[str]:
    """The icon's drawing, for a page of cards. None in the yardstick."""
    return [PERMALINK_SYMBOL, ""] if markup.starlight() else []


def _system_file_card(
    f: dict, consumers: FileConsumers, anchored: set[str] | None = None,
    searchable: bool = True,
) -> list[str]:
    """One file of a system page: hashes, who reads it, its provenance.

    ``searchable`` false keeps even the name out of the site search: the
    asset trees of game engines hold thousands of 0.ogg and tile.png.
    """
    name = f["name"]
    sha1_full = f.get("sha1", "unknown")
    md5_full = f.get("md5", "unknown")
    size = _fmt_size(f.get("size", 0))
    # Who reads this file, not who reads a file of this name
    plats = sorted(consumers.platforms.get(sha1_full, ()))
    emus = sorted(consumers.emulators.get(sha1_full, ()))
    installed = _installed_places(sha1_full, consumers, set(plats))

    anchor = f"sha1-{sha1_full[:12]}" if sha1_full != "unknown" else ""
    # A copy filed under two consoles of one maker is listed twice on its
    # page: the first card holds the anchor, as an id names one element.
    if anchored is not None:
        if anchor in anchored:
            anchor = ""
        else:
            anchored.add(anchor)
    skip = markup.search_exclude()
    lines = [markup.block_open("div", 'class="rb-sys-file"'), ""]
    # The heading's own link is this page's, the icon is the file's own
    # address, resolved by content wherever its card moves.
    # The yardstick keeps the word, the size the page budget was set on.
    icon = _PERMALINK_ICON if markup.starlight() else "permalink"
    permalink = (
        f' <a class="rb-permalink" href="{markup.site_href(f"files/?sha1={sha1_full}")}" '
        f'aria-label="Permanent link to {html.escape(name)}" '
        f'title="Permanent link, by content" {skip}>{icon}</a>'
    ) if anchor else ""
    # A file nothing reads said so nowhere: 53 of 248 Sony cards had no
    # emulator, platform or pack line and looked unfinished.
    unread = (f' <span class="rb-badge rb-badge-muted" {skip}>No reader</span>'
              if not (emus or plats or installed) else "")
    lines.append(_card_title(
        f'{_code(name)} <span class="rb-badge rb-badge-muted">{size}</span>{unread}{permalink}',
        anchor, searchable,
    ))
    lines.append("")

    # Each fact: its label, its values, whether the search reads it. What a
    # setup does with the file comes first, the fingerprints last.
    facts: list[tuple[str, list[str], bool]] = []
    if plats:
        # Each link lands on the platform's system section, and says where
        # that platform puts the file. The site search reads this line: it
        # names the file as each setup names it, and with the name only in
        # its title the card ranked below every guide citing scph5501 as an
        # example.
        facts.append(("On the setup's list",
                      [_places_text(_platform_places(sha1_full, consumers))], True))
    if installed:
        # The packs that carry the file for their cores: a RetroBat user
        # looking up scph5501.bin found every platform but RetroBat, whose
        # pack installs it for six cores.
        facts.append(("Packed for its emulators by", [_places_text(installed)], False))
    if emus:
        facts.append(("Emulators", [", ".join(_emulator_links(sha1_full, consumers))], False))
    if f.get("path"):
        # The card named a file, its hashes and provenance, but not where to
        # get it: a user missing scph5501.bin found the reference and no way
        # to the bytes.
        facts.append(("Collection path",
                      [f"{_code(f['path'])} ([download]({_fetch_url(f['path'])}))"], False))
    catalogued = [line.split(": ", 1)[1]
                  for line in _provenance_lines(f.get("provenance") or {}, "../")]
    if catalogued:
        facts.append(("Catalogued", catalogued, False))
    # Every hash in full: the site search indexes text, not a title
    # attribute, so a pasted md5 or crc32 (what clrmamepro, RomVault or an
    # emulator log prints) found nothing behind a 12-character prefix.
    # files/?hash= finds them, and the index skips them.
    for label, value in (("SHA1", sha1_full), ("MD5", md5_full),
                         ("CRC32", f.get("crc32")), ("SHA256", f.get("sha256"))):
        if value and value != "unknown":
            facts.append((label, [f'<span class="rb-hash">{html.escape(str(value))}</span>'],
                          False))

    if not markup.starlight():
        # The size yardstick pages are cut by: the list a card was when the
        # budget was set, so a fact grid moves no card to another page.
        lines.extend(search_excluded([f"- {label}: {', '.join(values)}"
                                      for label, values, _searched in facts]))
    else:
        # A grid of facts, label then value: two bullet lists of code chips
        # read as a dump. What the search skips is one list with one
        # attribute: on each row it was 77 KB of the Sony page.
        read = [fact for fact in facts if fact[2] and searchable]
        skipped = [fact for fact in facts if fact not in read]
        for group, attributes in ((read, ""), (skipped, f" {skip}")):
            if group:
                lines.append(f'<dl class="rb-facts"{attributes}>')
                # One value cell per label: the grid pairs each dt with one dd.
                # A searched value keeps its label out: the excerpt ran the two
                # together, "On the setup's listAmberELEC".
                label_skip = f" {skip}" if not attributes else ""
                lines.extend(f"<dt{label_skip}>{label}</dt><dd>"
                             + "<br>".join(_inline_card_html(v) for v in values) + "</dd>"
                             for label, values, _searched in group)
                lines.append("</dl>")
    lines.extend(["", "</div>", ""])
    return lines


SEARCH_TABLE_BYTES = 3000


def tables_out_of_search(markdown: str, threshold: int = SEARCH_TABLE_BYTES) -> str:
    """Each table longer than ``threshold`` bytes, wrapped out of the search.

    A search result shows the start of a section: a 600-row table as one
    snippet helps nobody, and the gaps, provenance and cross-reference
    tables were 900 KB of the index. The headings above them stay.
    """
    lines = markdown.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        match = re.match(r"( *)\|", lines[index])
        if not match:
            out.append(lines[index])
            index += 1
            continue
        indent = match.group(1)
        end = index
        while end < len(lines) and re.match(rf"{indent}\|", lines[end]):
            end += 1
        table = lines[index:end]
        if sum(len(line) for line in table) > threshold:
            out += ["", indent + markup.block_open("section", markup.search_exclude()), "",
                    *table, "", f"{indent}</section>", ""]
        else:
            out += table
        index = end
    return "\n".join(out)


def search_excluded(lines: list[str]) -> list[str]:
    """Lines the site search leaves out, as a block of their own.

    The browser downloads and indexes the whole search index before the
    first result: hashes, link lists and install commands repeated on 600
    pages made it 21 MB. Hashes are found by files/?hash=, names and prose
    stay searchable.

    A section, not a div: the search plugin compares elements by tag, so
    the first div closed inside an excluded div (a code block's) ended the
    exclusion and the rest of the block was indexed.
    """
    return ["", markup.block_open("section", markup.search_exclude()), "",
            *lines, "", "</section>", ""]


# Card lines the site search leaves out. They list where a file goes and
# who cites it, links to other pages that index the same names, and they
# were 6.6 of the 24.7 MB the browser downloads and indexes before the first
# result. The name, the hashes, the entry's path and the notes stay
# searchable.
SEARCH_SKIPPED = (
    "- SHA1:", "- MD5:", "- CRC32:", "- SHA256:", "- Adler32", "- Size:",
    "- Validation", "- Priority:", "- Contents (",
    "- Platforms:", "- Also installed by:", "- Source:", "- Asked for by:",
    "- Collection path:", "- Collection copy:", "- Collection copies:",
    "- Installed by the platform's own package on:",
    "- Not in the platform's package", "- Never opened on these platforms",
)


def _search_skipped_apart(lines: list[str]) -> list[str]:
    """Card list lines, each run the search skips in a block of its own."""
    out: list[str] = []
    skipping = False
    for line in lines:
        skip = line.startswith(SEARCH_SKIPPED) or (skipping and line.startswith("    "))
        if skip and not skipping:
            out.extend(["", markup.block_open(
                "div", f'class="rb-search-skip" {markup.search_exclude()}'), ""])
        elif skipping and not skip:
            out.extend(["", "</div>", ""])
        skipping = skip
        out.append(line)
    if skipping:
        out.extend(["", "</div>", ""])
    return out


def _card_title(text: str, anchor: str, searchable: bool = True, weight: int = 10) -> str:
    """A file card's heading, so the site search lands on the card.

    The search splits a page at its headings: a card titled in bold sent a
    pasted md5 to the top of a page of a hundred cards. Level 3, under
    the console or the Files heading: a level 4 after a level 2 skipped
    one for screen readers on 551 pages. The table of contents of a page
    of cards stops above it (sitestarlight.py), and a card without an
    anchor keeps the id the toc gives it.
    """
    if markup.starlight():
        # Raw HTML: the card links to itself already, and a Markdown heading
        # would add the engine's anchor link and icon to every card.
        title = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m[1], quote=False)}</code>", text)
        ident = f' id="{anchor}"' if anchor else ""
        # A system page's card is the file's own page in all but name: its
        # title weighs the most a word can, so a file name typed in the
        # search finds the card before a guide that cites it as an example.
        # An emulator's card says who reads the file, and weighs half.
        search = f' data-pagefind-weight="{weight}"' if searchable else f" {markup.search_exclude()}"
        return f'<h3{ident} class="rb-file-title"{search}>{title}</h3>'
    attributes = f"#{anchor} .rb-file-title" if anchor else ".rb-file-title"
    if not searchable:
        attributes += " data-search-exclude"
    return f"### {text} {{ {attributes} }}"


def system_install_choices(coverages: dict) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """slug -> console -> the (platform, system id) pairs linked to it.

    Read through the same table as every system link, so a command sits in
    the section the platform's system links to. Only a system the
    platform's install manifest records is offered: --system refuses any
    other name.
    """
    from siterender import _page_by_norm  # noqa: PLC0415

    choices: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for name, cov in sorted(coverages.items()):
        manifest = _REPO_ROOT / "install" / f"{name}.json"
        if not manifest.is_file():
            continue
        document = json.loads(manifest.read_text(encoding="utf-8"))
        recorded = {
            str(system)
            for entry in (document.get("files") or []) + (document.get("omitted_files") or [])
            for system in entry.get("systems") or []
        }
        for sys_id in sorted(set(cov["config"].get("systems") or {}) & recorded):
            page = _system_page_map.get(sys_id) or _page_by_norm(sys_id)
            if page:
                slug, console = page
                choices.setdefault(slug, {}).setdefault(console, []).append((name, sys_id))
    return choices


# A manufacturer page above this much Markdown becomes a summary with one
# page per console, and a console above it is cut into parts. 500 KB of
# Markdown renders to about 750 KB of HTML: game-engines was one 10.5 MB
# page that took 21 s of main-thread work on a phone.
SYSTEM_PAGE_BUDGET = 500_000

# A system card: whether it is a variant, its lines, its sha1.
Card = tuple[bool, list[str], str]


def _reading_entries(sha1: str, consumers: FileConsumers) -> list[dict]:
    """The profile entries that read a file, one per emulator."""
    files_of = getattr(consumers, "emulator_files", {})
    entries = []
    for emulator, index in getattr(consumers, "emulator_entries", {}).get(sha1, {}).items():
        files = files_of.get(emulator) or []
        if 0 <= index < len(files) and isinstance(files[index], dict):
            entries.append(files[index])
    return entries


_NO_PRIORITY = 1 << 30


def _card_rank(f: dict, consumers: FileConsumers) -> tuple:
    """Where a file's card sits in its console: the files an emulator
    requires first, then by the emulators' search order (smallest first),
    then by how many setups list the file, then by name.

    By byte order the PlayStation section opened on LibPS.exe and
    PSX_DTLH2000.bin, capitals first, and scph5501.bin was 28th.
    """
    sha1 = str(f.get("sha1", ""))
    entries = _reading_entries(sha1, consumers)
    priorities = [e["priority"] for e in entries if isinstance(e.get("priority"), int)]
    return (
        not any(e.get("required", True) for e in entries),
        min(priorities, default=_NO_PRIORITY),
        -len(consumers.platforms.get(sha1, ())),
        f["name"].casefold(), f["name"], f.get("path", ""),
    )


def _by_name(f: dict) -> tuple:
    return f["name"].casefold(), f["name"], f.get("path", "")


def _console_cards(
    manufacturer: str,
    files: list[dict],
    consumers: FileConsumers,
    anchored: set[str],
    by_name: bool = False,
) -> list[Card]:
    """Each card of one console, primaries then variants, with whether it
    is a variant. ``by_name`` orders by name alone, for a console cut into
    parts named by their first and last file."""
    searchable = manufacturer not in GAME_DATA_TOPS
    order = _by_name if by_name else (lambda f: _card_rank(f, consumers))
    main_files = [f for f in files if "/.variants/" not in f["path"]]
    variant_files = [f for f in files if "/.variants/" in f["path"]]
    cards = [(False, _system_file_card(f, consumers, anchored, searchable), str(f.get("sha1", "")))
             for f in sorted(main_files, key=order)]
    cards += [(True, _system_file_card(v, consumers, anchored, searchable), str(v.get("sha1", "")))
              for v in sorted(variant_files, key=order)]
    return cards


# A "Start with" line names this many files at most: Game Engines'
# TheXTech requires 2,014.
START_WITH_NAMES = 6


def _region_order() -> dict[str, int]:
    from region import REGION_TREE, WORLD  # noqa: PLC0415

    ordered = [WORLD]
    for parent, members in REGION_TREE.items():
        ordered += [parent, *sorted(members)]
    return {region: index for index, region in enumerate(ordered)}


_REGION_WORDS = {"uk": "UK", "asia-ntsc": "Asia (NTSC)", "asia-pal": "Asia (PAL)"}


def _region_word(region: str) -> str:
    return _REGION_WORDS.get(region) or region.replace("-", " ").title()


def _start_with(files: list[dict], consumers: FileConsumers, first_below: bool = True) -> list[str]:
    """The line a console section opens on: the files an emulator requires,
    by the region the emulator picks each for.

    Read from the profiles' required and region fields, never typed per
    system. A section where no emulator requires a file gets no line.
    """
    from region import _PARENT  # noqa: PLC0415

    order = _region_order()
    by_region: dict[str, list[str]] = {}
    plain: list[str] = []
    primaries = [f for f in files if "/.variants/" not in f["path"]]
    for f in sorted(primaries, key=lambda f: _card_rank(f, consumers)):
        required = [e for e in _reading_entries(str(f.get("sha1", "")), consumers)
                    if e.get("required", True)]
        if not required:
            continue
        regions = {str(r) for e in required for r in e.get("region") or []}
        for names in [by_region.setdefault(r, []) for r in regions] or [plain]:
            if f["name"] not in names:
                names.append(f["name"])
    # A territory its region answers the same way adds nothing: Brazil
    # under Latin America.
    by_region = {r: names for r, names in by_region.items()
                 if by_region.get(_PARENT.get(r, "")) != names}
    # Regions that need the same files share a line.
    regional: dict[tuple[str, ...], list[str]] = {}
    for region in sorted(by_region, key=lambda r: (order.get(r, len(order)), r)):
        names = by_region[region]
        same = next((rs for rs, held in regional.items() if held == names), None)
        if same is None:
            regional[(region,)] = names
        else:
            regional[(*same, region)] = regional.pop(same)
    if not regional and not plain:
        return []

    def named(names: list[str], lead: str) -> str:
        if len(names) <= START_WITH_NAMES:
            return f"{lead}: {', '.join(_code(n) for n in names)}"
        where = ", first below" if first_below else ""
        return f"{lead}: {_count(len(names), 'more file' if regional else 'file')}{where}"

    if not regional:
        if len(plain) > START_WITH_NAMES:
            where = " They come first below." if first_below else ""
            return [f"**Start with** the {_count(len(plain), 'file')} an emulator requires.{where}",
                    ""]
        return [named(plain, "**Start with**") + ".", ""]
    lines = ["**Start with** the BIOS for the region of your games:", ""]
    for regions in sorted(regional, key=lambda rs: (order.get(rs[0], len(order)), rs)):
        lead = ", ".join(_region_word(r) for r in regions)
        lines.append(f"- **{lead}**: {', '.join(_code(n) for n in regional[regions])}")
    if plain:
        lines.append(named(plain, "- **Some emulators also require**"))
    return [*lines, ""]


def _cards_markdown(cards: list[Card]) -> list[str]:
    """Primaries, then the variants in a closed block."""
    lines = [line for variant, card, _sha1 in cards if not variant for line in card]
    variants = [card for variant, card, _sha1 in cards if variant]
    if variants:
        # A variant gets the same card as a primary: the revision a
        # platform pins is the one a reader comes to find, and showed as
        # a name and a truncated MD5, nothing said who pins it.
        lines.extend(markup.details(
            "note", f"Variants ({len(variants)})", [line for card in variants for line in card]))
    return lines


def _parts(cards: list[Card], budget: int, sizes: list[int] | None = None) -> list[list[Card]]:
    """Cards in runs of at most ``budget`` bytes of Markdown, in order.
    ``sizes`` gives each card's weight when it is not its own Markdown."""
    parts: list[list[Card]] = [[]]
    size = 0
    for index, card in enumerate(cards):
        weight = sizes[index] if sizes else sum(len(line) + 1 for line in card[1])
        if parts[-1] and size + weight > budget:
            parts.append([])
            size = 0
        parts[-1].append(card)
        size += weight
    return parts


def _console_heading(manufacturer: str, console: str) -> str:
    """A console's heading: its icon hidden from screen readers, then the
    name with no space before it. The space read as "Section titled
    “ PlayStation”" on five of seven Sony headings."""
    anchor = f" {{ #{console_anchor(manufacturer, console)} }}"
    icon = _icon_name(manufacturer, console)
    if not _icon_available.get(icon):
        return f"## {console}{anchor}"
    image = markup.image("", _icon_url(icon), width="24", **{"aria-hidden": "true"})
    return f"## {image}{console}{anchor}"


_NEXT_FROM_SYSTEMS = [
    "[Get your files](../which-pack.md), for the setup you play on",
    "[When a game does not start](../wiki/troubleshooting.md)",
    "[Every emulator](../emulators/index.md), and the files each one reads",
]


def system_pages(
    manufacturer: str,
    consoles: dict[str, list],
    consumers: FileConsumers,
    installs: dict[str, list[tuple[str, str]]] | None = None,
    budget: int = SYSTEM_PAGE_BUDGET,
) -> dict[str, tuple[str, dict[str, list]]]:
    """A manufacturer's pages by stem under systems/: one page, or a summary
    and a page per console when the whole would pass ``budget``. Each page
    comes with the files whose cards it holds, by console.

    Every console keeps its heading on the summary, so a link to
    systems/<maker>/#<console> still lands, and site.js sends a card
    anchor the page no longer holds to the card's new place. The pages
    each card and console went to are recorded for every other link
    (siterender.record_console_pages).
    """
    slug = _slug(manufacturer)
    json_link = (f"[JSON](../api/v1/systems/{urllib.parse.quote(slug)}.json)")
    def all_sections() -> dict[str, list[Card]]:
        anchors: set[str] = set()
        return {console: _console_cards(manufacturer, consoles[console], consumers, anchors)
                for console in sorted(consoles)}

    intro = (
        f"Every BIOS file the collection holds for {manufacturer} machines, one "
        "card per file. A card gives the file's fingerprints, the emulators that "
        "read it, the setups that list it and where to download it. A file can "
        "have several versions, one per region or revision: the main one comes "
        "first, the others under \"Variants\". To install the files of one "
        "console, open its \"Install\" box."
    )
    # The two setup lines of a card differ, and only a code comment said how.
    key = (
        "On a card, **On the setup's list** names the setups whose own BIOS "
        "list has the file. **Packed for its emulators by** names the setups "
        "whose pack adds it because one of their emulators reads it."
    )
    def whole_page(sections: dict[str, list[Card]]) -> str:
        lines = [
            f"# {manufacturer}", "", intro, "", key, "",
            f"The files below by console, with every hash, are also published as {json_link}.",
            "", *_icon_symbol(),
        ]
        for console, cards in sections.items():
            lines.append(_console_heading(manufacturer, console))
            lines.append("")
            lines.extend(_start_with(consoles[console], consumers))
            lines.extend(_console_install_block(console, (installs or {}).get(console, [])))
            lines.extend(_cards_markdown(cards))
            lines.append("")
        lines.extend(_next_block(_NEXT_FROM_SYSTEMS))
        lines.extend(["", _date_line()])
        return "\n".join(lines) + "\n"

    def card_weight(card: list[str]) -> int:
        return sum(len(line) + 1 for line in card)

    # Sizes are those of the MkDocs Markdown the budget was set on, so the
    # pages split where they always have and no card changes address.
    with markup.using("mkdocs"):
        measured = all_sections()
        measured_text = whole_page(measured)
    if len(measured_text) <= budget:
        text = whole_page(all_sections()) if markup.starlight() else measured_text
        return {slug: (text, consoles)}

    # The heaviest sections move to pages of their own until what stays on
    # the manufacturer's page is half the budget: a section of one file
    # keeps its place, and a reader does not open 91 pages to browse Other.
    weights = {
        console: sum(card_weight(card) for _variant, card, _sha1 in cards)
        for console, cards in measured.items()
    }
    moved: set[str] = set()
    for console in sorted(weights, key=lambda c: (-weights[c], c)):
        if sum(w for c, w in weights.items() if c not in moved) <= budget // 2:
            break
        moved.add(console)

    pages: dict[str, tuple[str, dict[str, list]]] = {}
    summary = [
        f"# {manufacturer}", "", intro, "", key, "",
        f"The files below by console, with every hash, are also published as {json_link}. "
        f"The largest sections have a page of their own, linked from their heading.",
        "", *_icon_symbol(),
    ]
    summary_anchors: set[str] = set()
    for console in sorted(consoles):
        summary.append(_console_heading(manufacturer, console))
        summary.append("")
        if console not in moved:
            summary.extend(_start_with(consoles[console], consumers))
            summary.extend(_console_install_block(console, (installs or {}).get(console, [])))
            summary.extend(_cards_markdown(
                _console_cards(manufacturer, consoles[console], consumers, summary_anchors)))
            summary.append("")
            continue
        # Cards are rendered again for their own page: the anchors a page
        # holds are its own.
        def cut(by_name: bool) -> list[list[Card]]:
            cards = _console_cards(manufacturer, consoles[console], consumers, set(), by_name)
            with markup.using("mkdocs"):
                sizes = [card_weight(card) for _variant, card, _sha1 in _console_cards(
                    manufacturer, consoles[console], consumers, set(), by_name)]
            return _parts(cards, budget, sizes)

        # One page keeps the required files first. Parts are named by their
        # first and last file, so a console cut in parts goes by name.
        parts = cut(False)
        if len(parts) > 1:
            parts = cut(True)
        base = f"{slug}-{_slugify_anchor(console) or 'files'}"
        stems = [base if index == 0 else f"{base}-{index + 1}" for index in range(len(parts))]
        record_console_pages(slug, console, stems, [
            [sha1 for _variant, _card, sha1 in part] for part in parts
        ])
        title = f"{manufacturer}: {console}"
        count = _count(len(consoles[console]), "file")
        summary.extend(_start_with(consoles[console], consumers, first_below=False))
        if len(stems) == 1:
            summary.append(f"{count}, on [their own page]({stems[0]}.md).")
        else:
            summary.append(f"{count}, on {len(stems)} pages by file name:")
            summary.append("")
            for index, (stem, part) in enumerate(zip(stems, parts)):
                summary.append(f"- [Part {index + 1}]({stem}.md): {_part_range(part)}")
        summary.append("")
        for index, (stem, part) in enumerate(zip(stems, parts)):
            heading = title if len(stems) == 1 else f"{title}, part {index + 1} of {len(stems)}"
            lines = [
                f"# {heading}", "",
                f"Part of [{manufacturer}]({slug}.md), in [Systems](index.md). "
                f"Every hash of these files is also published as {json_link}.",
                "", *_icon_symbol(),
            ]
            if len(stems) > 1:
                others = ", ".join(
                    f"[part {i + 1}]({other}.md)" for i, other in enumerate(stems) if i != index)
                lines.extend([f"This part holds {_part_range(part)}. The others: {others}.", ""])
            if index == 0:
                lines.extend(_start_with(consoles[console], consumers, first_below=len(stems) == 1))
            lines.extend([key, ""])
            lines.extend(_console_install_block(console, (installs or {}).get(console, [])))
            lines.extend(["## Files", ""])
            lines.extend(_cards_markdown(part))
            lines.append("")
            lines.extend(_next_block([f"[{manufacturer}]({slug}.md), every section"]
                                     + _NEXT_FROM_SYSTEMS))
            lines.extend(["", _date_line()])
            held = {sha1 for _variant, _card, sha1 in part}
            pages[stem] = ("\n".join(lines) + "\n", {console: [
                f for f in consoles[console] if str(f.get("sha1", "")) in held]})
    summary.extend(_next_block(_NEXT_FROM_SYSTEMS))
    summary.extend(["", _date_line()])
    kept = {console: files for console, files in consoles.items() if console not in moved}
    return {slug: ("\n".join(summary) + "\n", kept), **pages}


def _part_range(part: list[Card]) -> str:
    """The first and last file names of a part, as the cards show them."""
    names = []
    for _variant, card, _sha1 in part:
        # An HTML heading on the site, a Markdown one in the size yardstick.
        match = re.search(r"`([^`]*)`|<code>(.*?)</code>", card[2])
        names.append(match[1] if match[1] is not None else html.unescape(match[2]))
    if len(names) == 1:
        return f"`{names[0]}`"
    return f"{_count(len(names), 'file')}, `{names[0]}` to `{names[-1]}`"


def generate_system_page(
    manufacturer: str,
    consoles: dict[str, list],
    consumers: FileConsumers,
    installs: dict[str, list[tuple[str, str]]] | None = None,
) -> str:
    """One manufacturer's page, a section per console.

    ``installs`` gives, per console, the (platform, system id) pairs whose
    install command the section offers. A page over the budget is the
    summary system_pages writes, its consoles on pages of their own.
    """
    return system_pages(manufacturer, consoles, consumers, installs)[_slug(manufacturer)][0]


# Emulator pages


def _named_system_link(sys_id: str, prefix: str = "") -> str:
    """A system by its display name, linked where _system_link links it.

    The emulator pages printed the id ("sony-playstation") where every
    other page says "Sony PlayStation". The id stays in the address only.
    """
    from siterender import system_section  # noqa: PLC0415

    name = system_name(sys_id).replace("[", r"\[").replace("]", r"\]")
    section = system_section(sys_id)
    if not section:
        return name
    slug, console = section
    return f"[{name}]({console_href(slug, console, prefix)})"


def _distinct_spellings(names: list) -> list[str]:
    """Names as a reader tells them apart: one per spelling, ignoring case.

    DuckStation's cores list `Duckstation` for EmuELEC, whose launch name is
    spelled so, and the page read "duckstation, Duckstation". The profile
    keeps both, the core lookup being case-sensitive.
    """
    seen: dict[str, str] = {}
    for name in map(str, names):
        seen.setdefault(name.casefold(), name)
    return list(seen.values())


# Region slugs whose words are not the slug title-cased.
_REGION_WORDS = {
    "uk": "UK",
    "asia-ntsc": "Asia (NTSC)",
    "asia-pal": "Asia (PAL)",
    "world": "every region",
}


def _region_words(value: object, most: int = 0) -> str:
    """A region: value in words. ``most`` caps the regions named, the rest
    counted: one Saturn BIOS serves seven."""
    raw = value if isinstance(value, list) else [value] if value else []
    words = [_REGION_WORDS.get(str(r), str(r).replace("-", " ").title()) for r in raw]
    if most and len(words) > most:
        words = words[:most] + [f"{len(words) - most} more"]
    return _said_as_list(words)


_CATEGORY_WORDS = {"bios": "BIOS", "game_data": "game data", "bios_zip": "BIOS ZIP"}


INITIAL_DIGITS = "0 to 9"


def emulator_initial(display: str) -> str:
    """The letter an emulator is filed under, in the navigation and on the
    index alike. A name that does not open on a letter goes under "0 to 9"."""
    initial = display[:1].upper()
    return initial if "A" <= initial <= "Z" else INITIAL_DIGITS


def generate_emulators_index(profiles: dict) -> str:
    unique = {
        k: v for k, v in profiles.items() if v.get("type") not in ("alias", "test")
    }
    aliases = {k: v for k, v in profiles.items() if v.get("type") == "alias"}

    by_class: dict[str, list[tuple[str, dict]]] = {}
    for name in sorted(unique.keys()):
        p = unique[name]
        cls = p.get("core_classification", "other")
        by_class.setdefault(cls, []).append((name, p))

    total_files = sum(len(p.get("files", [])) for p in unique.values())

    lines = [
        "# Emulators",
        "",
        "Each page is one emulator: the files it reads, whether it starts games "
        "without them, and the line of its source code that says so. The "
        "kind says how a core relates to the emulator it comes from, which "
        "tells how far its own files can differ.",
        "",
        f"**{len(unique):,}** emulator profiles, **{total_files:,}** files total, **{len(aliases):,}** aliases.",
        "",
        "| Kind | Count | Description |",
        "|------|-------|-------------|",
    ]

    cls_desc = {
        "official_port": "The same authors keep the app and the RetroArch core",
        "community_fork": "Ported to RetroArch by someone else",
        "pure_libretro": "Built for RetroArch, no app of its own",
        "game_engine": "Game engine reimplementation",
        "enhanced_fork": "Fork with added features",
        "frozen_snapshot": "Frozen at an old version",
        "embedded_hle": "Needs no BIOS file: the emulator carries its own",
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
        lines.append(f"| {label} | {len(entries)} | {desc} |")
    lines.append("")

    # One section per initial, the groups the navigation draws: its letter
    # links land on the section of the same name.
    by_initial: dict[str, list[tuple[str, dict]]] = {}
    for name, p in unique.items():
        by_initial.setdefault(emulator_initial(str(p.get("emulator", name))), []).append((name, p))
    for initial in sorted(by_initial, key=lambda g: (g != INITIAL_DIGITS, g)):
        lines.extend([
            f"## {initial}",
            "",
            "| Emulator | Kind | Systems | Files |",
            "|--------|------|---------|-------|",
        ])
        entries = sorted(by_initial[initial], key=lambda e: str(e[1].get("emulator", e[0])).casefold())
        for name, p in entries:
            emu_name = p.get("emulator", name)
            cls = p.get("core_classification", "other")
            systems = list(dict.fromkeys(
                _named_system_link(s, "../") for s in p.get("systems") or []))
            files = p.get("files", [])
            sys_str = ", ".join(systems[:3])
            if len(systems) > 3:
                sys_str += f" and {len(systems) - 3} more"
            file_count = len(files)
            file_str = f"{file_count:,}" if file_count else "-"
            lines.append(f"| [{emu_name}]({name}.md) | {CLS_LABELS.get(cls, cls)} | {sys_str} | {file_str} |")
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
            f'<span class="rb-badge rb-badge-info">{_CATEGORY_WORDS.get(category, category)}</span>'
        )
    if region:
        # Labelled: "optional in repo japan" read as one run-on phrase.
        badges.append(
            f'<span class="rb-badge rb-badge-muted">Region: {_region_words(region)}</span>'
        )
    if storage and storage != "embedded":
        badges.append(
            f'<span class="rb-badge rb-badge-muted">{storage}</span>'
        )
    if bundled:
        # Said in words: the pack leaves the file out because the emulator,
        # in the build the entry names, already installs it.
        build = f" {f['mode']}" if f.get("mode") in ("standalone", "libretro") else ""
        badges.append(
            f'<span class="rb-badge rb-badge-muted">installed with the{build} emulator</span>'
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
            f'<span class="rb-badge rb-badge-muted rb-badge-archive">in {archive}</span>'
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


_LOAD_FROM = {
    "system_dir": "the system (BIOS) directory",
    "save_dir": "the save directory, not the BIOS directory",
    "content_dir": "the folder of the loaded content, not the BIOS directory",
    "working_directory": "the working directory, not the BIOS directory",
    "data_dir": "the emulator's data directory, not the BIOS directory",
    "hdd_image": "inside the emulated hard disk image, not the BIOS directory",
    "internal_storage": "the app's internal storage, not the BIOS directory",
}


def _by_build(value: object, words: Callable[[str], str]) -> str:
    """A field keyed by build, said per build, or a plain value said once."""
    if isinstance(value, dict):
        return ", ".join(f"{words(str(v))} ({mode} build)" for mode, v in value.items() if v)
    return words(str(value)) if value else ""


def _load_from_text(value: object) -> str:
    """Where the code reads a file, in words.

    The card showed the Path alone: Dolphin's libretro core reads
    gba_bios.bin from its save directory, and a reader put it under system/.
    """
    if value in (None, "", "system_dir"):
        return ""
    return _by_build(value, lambda v: _LOAD_FROM.get(v, _code(v)))


def _found_by_text(value: object) -> str:
    """How the code finds a file: by the name it opens, or by content."""
    words = {
        "name": "file name (the code opens this name)",
        "content": "content (the code scans the folder and identifies the file "
                   "by its hash, any file name works)",
    }
    return _by_build(value, lambda v: words.get(v, _code(v)))


def _on_mismatch_lines(value: object) -> list[str]:
    """What the code does when a declared check fails, where it is not reject."""
    said = {"warn": "warns and runs with the file", "reject": "rejects the file"}
    if value == "warn":
        return [f"On a failed check: {said['warn']}"]
    if isinstance(value, dict):
        return [f"On a failed {check} check: {said.get(str(how), _code(str(how)))}"
                for check, how in value.items()]
    return []


def _build_rows(profile: dict) -> list[str]:
    """Meta card rows for who ships the build, how its files are found, why
    its revision is held and where its releases are announced."""
    rows = []
    built_by = profile.get("built_by") or []
    if built_by:
        names = ", ".join(f"[{p}](../platforms/{p}.md)" for p in map(str, built_by))
        rows.append(f"| Built by | {names}, under its core name. The libretro "
                    "buildbot does not publish this build. |")
    found = _found_by_text(profile.get("found_by"))
    if found:
        rows.append(f"| Files found by | {found} |")
    frozen = profile.get("pin_frozen")
    if frozen:
        held = _escape_tags(' '.join(str(frozen).split())).replace('|', r'\|')
        rows.append(f"| Revision held | {held} |")
    announced = profile.get("version_source")
    if isinstance(announced, dict) and announced.get("url"):
        url = str(announced["url"])
        row = f"| Releases announced at | [{url}]({url})"
        if announced.get("no_genuine_copy"):
            row += (f" (version {_escape_tags(str(announced['no_genuine_copy']))} "
                    "announced there, found only as rebuilt copies)")
        rows.append(row + " |")
    return rows


def _render_emulator_file(
    f: dict,
    profile: dict,
    consumers: FileConsumers | None,
    files: list,
    _file_available: Callable[[dict], bool],
    anchor: str = "",
    owner: str = "",
    alias: str = "",
) -> list[str]:
    """Render one row of an emulator profile's file table.

    `anchor` is the card's id on the page, so one file can be cited by URL,
    and `alias` a second id it answers to.

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
    lines.append(markup.block_open("div", 'class="rb-file-entry"'))
    lines.append("")
    marker = f'<span id="{alias}"></span>' if alias else ""
    lines.append(_card_title(f"{marker}{_code(fname)} {badge_str}", anchor, weight=5))
    lines.append("")
    if desc:
        lines.extend([_escape_tags(str(desc)), ""])
    body_start = len(lines)

    details = []
    if fpath and fpath != fname:
        details.append(f"Path: `{fpath}`")
    standalone_path = f.get("standalone_path", "")
    if standalone_path and standalone_path != fpath:
        # The standalone build reads it elsewhere: MesenCE's dsp1.rom goes
        # under Firmware/, and a page naming only the core's path sent its
        # users to the wrong folder.
        details.append(f"Standalone path: `{standalone_path}`")
    read_from = _load_from_text(f.get("load_from"))
    if read_from:
        details.append(f"Read from: {read_from}")
    found = _found_by_text(f.get("found_by"))
    if found:
        details.append(f"Found by: {found}")
    if fsystem:
        details.append(f"System: {_named_system_link(fsystem, '../')}")
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
    details.extend(_on_mismatch_lines(f.get("on_mismatch")))
    if f.get("installed_on"):
        details.append("Installed by the platform's own package on: "
                       + ", ".join(sorted(f["installed_on"])))
    if f.get("not_bundled_on"):
        details.append("Not in the platform's package, so the pack brings it, on: "
                       + ", ".join(sorted(f["not_bundled_on"])))
    if f.get("unread_on"):
        details.append("Never opened on these platforms, whose launcher names other files: "
                       + ", ".join(sorted(f["unread_on"])))
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
    if consumers and f.get("type") == "directory":
        details.extend(_directory_copies(f, consumers))
    if consumers:
        sha1 = consumers.sha1_of(f, f.get("path") or "", owner=owner)
        held = consumers.path_of(sha1) if sha1 and in_repo else ""
        parts = held.split("/")
        if len(parts) >= 3 and parts[0] == "bios":
            # Which copy answers this entry, linked to its collection card
            # (provenance, every hash, download): an entry declaring no hash
            # said "in repo" without naming the file.
            page = card_page(held, sha1)
            details.append(
                f"Collection copy: [{_code(held)}]"
                f"(../systems/{urllib.parse.quote(page)}.md#sha1-{sha1[:12]})"
            )
        elif in_repo and not any(d.startswith("Collection copies:") for d in details):
            copy = held_copy(f)
            if copy:
                details.append(_held_copy_line(copy, consumers))
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
    lines[body_start:] = _search_skipped_apart(lines[body_start:])
    lines.append("")
    lines.append("</div>")
    lines.append("")
    return lines


def _held_copy_line(copy: tuple[str, str], consumers: FileConsumers) -> str:
    """Where a copy the database does not index sits: a ZIP member under
    bios/, or a file or tree of a data directory the packs fetch."""
    path, member = copy
    parts = path.rstrip("/").split("/")
    if member:
        archive = _code(path)
        zsha1 = consumers.sha1_at(path)
        if zsha1 and len(parts) >= 3 and parts[0] == "bios":
            page = urllib.parse.quote(card_page(path, zsha1))
            archive = f"[{archive}](../systems/{page}.md#sha1-{zsha1[:12]})"
        return f"Collection copy: member {_code(member)} of {archive}"
    if parts[0] == "data" and len(parts) >= 2:
        ref, inside = parts[1], "/".join(parts[2:])
        if path.endswith("/"):
            where = _code(inside + "/") if inside else "the tree"
            return f"Collection copy: {where} in the {_code(ref)} data directory"
        info = (_DATA_MEMBERS.get(ref) or {}).get(inside) or {}
        shown = _code(inside)
        if info.get("sha1"):
            shown = (f'<a href="../../files/?sha1={info["sha1"]}">'
                     f"<code>{html.escape(inside)}</code></a>")
        return f"Collection copy: {shown} in the {_code(ref)} data directory"
    tree = f"{REPO_URL}/tree/main/{urllib.parse.quote(path.rstrip('/'))}"
    return f"Collection copy: [{_code(path)}]({tree})"


def _directory_copies(entry: dict, consumers: FileConsumers) -> list[str]:
    """What the collection holds under a directory entry, and where.

    The card said "in repo" and linked none of the files: alephone's
    Plugins/ holds 393 of them, each with its own card on a system page.
    """
    members = consumers.directory_members(entry)
    held = sorted({consumers.path_of(sha1) for sha1 in members.values()} - {""})
    if not held:
        return []
    folder = os.path.commonpath(held) if len(held) > 1 else os.path.dirname(held[0])
    parts = folder.split("/")
    where = _code(folder + "/")
    if len(parts) >= 3 and parts[0] == "bios":
        where = f"[{where}]({console_href(_slug(parts[1]), parts[2], '../')})"
    tree = f"{REPO_URL}/tree/main/{urllib.parse.quote(folder)}"
    return [f"Collection copies: {_count(len(members), 'file')} under {where} "
            f"([browse]({tree}))"]


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
        ("verification", "Checks the file by"),
        ("analysis_date", "Analysis date"),
        ("analysis_commit", "Analysis commit"),
    ]:
        val = profile.get(field)
        if val is None or val == "" or isinstance(val, (dict, list)):
            continue
        if field == "bios_size" and isinstance(val, int):
            lines.append(f"| {label} | {_fmt_size(val)} ({val:,} bytes) |")
        elif isinstance(val, str) and val.startswith("http"):
            lines.append(f"| {label} | [{val}]({val}) |")
        else:
            lines.append(f"| {label} | {val} |")
    if profile.get("source_ref"):
        lines.append(
            f"| Source ref | {_source_ref_markdown(profile, profile['source_ref'])} |"
        )
    return lines


def install_commands(args: str = "", indent: str = "") -> list[str]:
    """The same install for each place a user runs it: one generator for the
    download page and the platform, system and emulator pages, so none of
    them loses the Android or Windows form."""
    lines = []
    for label, shell in (
        ("Linux, macOS, Steam Deck:", "sh"),
        ("Windows (PowerShell):", "powershell"),
        ("Android (Termux, after `pkg install python` and `termux-setup-storage`):", "sh"),
    ):
        lines += [f"{indent}{label}", "", *fence(shell, install_command(shell, args), indent), ""]
    return lines


def _install_block(name: str, profile: dict) -> list[str]:
    """One command that installs this emulator's files where it reads them.

    The installer finds the platform and its BIOS folder, then keeps what
    this emulator reads there: its own files and the platform's files of
    the systems it runs. Each file is also linked one by one below.
    """
    if profile.get("type") in ("alias", "launcher", "test"):
        return []
    return search_excluded(markup.details("tip", "Install this emulator's files", [
        "The installer finds your platform and its BIOS folder, then downloads "
        "only the files this emulator reads there and checks each one.",
        "",
        *install_commands(f"--emulator {name}"),
    ]))


def _platform_install_block(name: str, display: str, on_card: bool = False) -> list[str]:
    """The platform's pack, installed file by file: what the ZIP holds, with
    resume, verification and only what is missing.

    ``on_card``: the files go on the frontend's SD card, installed from the
    computer it is mounted on, whose root is named.
    """
    if on_card:
        return markup.details("tip", "Install with one command", [
            f"With the {display} SD card mounted, the installer downloads the files "
            "of the pack that are missing or differ from it into `Bios/<TAG>/` on "
            "the card, checks each one, and resumes where it stopped. `--dest` "
            "names the card's root.",
            "",
            "Linux, macOS:",
            "",
            *fence("sh", install_command("sh", f"--platform {name} --dest /path/to/card")),
            "",
            "Windows (PowerShell, E: being the card):",
            "",
            *fence("powershell", install_command("powershell", f"--platform {name} --dest E:\\")),
        ], opened=True)
    return markup.details("tip", "Install with one command", [
        f"The installer downloads the files of the {display} pack that are "
        "missing or differ from it, checks each one, and resumes where it "
        "stopped. Add `--system` with the ids of the systems overview below "
        "to install fewer systems.",
        "",
        *install_commands(f"--platform {name}"),
    ], opened=True)


def _ps_quote(value: str) -> str:
    """A PowerShell argument: bare when plain, single-quoted otherwise."""
    return value if re.fullmatch(r"[\w.+-]+", value) else "'" + value.replace("'", "''") + "'"


def _console_install_block(console: str, declared: list[tuple[str, str]]) -> list[str]:
    """Each platform's command for one console, by the system id it gives it,
    for a terminal and for PowerShell."""
    if not declared:
        return []
    sh = [install_command("sh", f"--platform {platform} --system {shlex.quote(system)}")
          for platform, system in declared]
    ps = [install_command("powershell", f"--platform {platform} --system {_ps_quote(system)}")
          for platform, system in declared]
    return search_excluded(markup.details("tip", f"Install the {console} files", [
        "Each platform names the system its own way.",
        "",
        "Linux, macOS, Steam Deck:",
        "",
        *fence("sh", "\n".join(sh)),
        "",
        "Windows (PowerShell):",
        "",
        *fence("powershell", "\n".join(ps)),
    ]))


def _render_platform_details(profile: dict) -> list[str]:
    """The per-platform block, one level of nesting deep."""
    lines: list[str] = []
    platform_details = profile.get("platform_details")
    if platform_details and isinstance(platform_details, dict):
        body: list[str] = []
        for pk, pv in platform_details.items():
            if isinstance(pv, dict):
                body.extend([f"- **{pk}:**", ""])
                for sk, sv in pv.items():
                    body.append(f"    - {sk}: {_detail_value(profile, sk, sv)}")
                body.append("")
            else:
                body.append(f"- **{pk}:** {_detail_value(profile, pk, pv)}")
        lines.extend(markup.details("info", "Platform details", body, opened=True))

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


def _structured_field_text(profile: dict) -> Callable[[object, object], str]:
    """Scalars of a structured block, citations linked at the profile's pin.

    The file entries and platform details linked their source_ref, and the
    analysis and firmware blocks printed 186 of them as plain code.
    """
    def text(key: object, value: object) -> str:
        if key == "source_ref" and isinstance(value, str):
            return _source_ref_markdown(profile, value)
        if isinstance(value, str):
            return link_prose_citations(profile, _field_text(key, value))
        return _field_text(key, value)

    return text


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
        body: list[str] = []
        _render_yaml_value(body, val, indent=0,
                           field_text=_structured_field_text(profile))
        block = markup.details(
            "abstract", label, [_escape_tags(line) for line in body], opened=True)
        # A block listing hashes is the lookup's, as a card's hash lines are:
        # izapple2's ROM table put "md5" 59 times in the search, above the
        # page that explains MD5 checks.
        if any(re.search(r"\b[0-9a-f]{32,64}\b", line) for line in body):
            block = search_excluded(block)
        lines.extend(block)

    return lines


def _hash_spans(value: str | list) -> str:
    """Each hash a profile accepts, in full.

    Cut to 12 characters with the rest in a title attribute, 208 hashes
    that only a profile declares were out of reach of the site search: it
    indexes text, and a pasted sha1 matched nothing.
    """
    from hashing import parse_hash_list  # noqa: PLC0415

    return ", ".join(
        markup.hash_value(v) for v in parse_hash_list(value)
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

    # Sentences, one number format: the line read "1,563 files | 164
    # required, 1399 optional | 1563 in repo".
    held = [f"{in_repo_count:,} in repo", f"{missing_count:,} missing"]
    if unsourceable_count:
        held.append(f"{unsourceable_count:,} unsourceable")
    if hle_count:
        held.append(f"{hle_count:,} with HLE fallback")
    total = f"**{_count(len(slots), 'file')}**"
    if len(slots) != len(files):
        total += f" ({len(files):,} accepted versions)"
    lines = [
        f"{total}: {req_count:,} required, {len(slots) - req_count:,} optional. "
        f"{_said_as_list(held)}.",
        "",
    ]

    categories = [f.get("category", "bios") for f in files]
    if "game_data" in categories or "bios_zip" in categories:
        cats = [
            f"{categories.count(key):,} {label}"
            for key, label in (
                ("bios", "BIOS"), ("bios_zip", "BIOS ZIPs"), ("game_data", "game data")
            )
            if categories.count(key)
        ]
        lines.extend([f"By kind: {_said_as_list(cats)}.", ""])
    return lines


def _said_as_list(items: list[str]) -> str:
    """"a, b and c"."""
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else "".join(items)


# Data directory members and revisions, read once per build.
_DATA_MEMBERS: dict[str, dict[str, dict]] = {}
_DATA_REVISIONS: dict[str, dict] = {}


# An emulator page lists at most this many file cards: MAME's 1,564 made a
# 3 MB page. Past it, the cards go to pages of their own and the emulator's
# page keeps a table of every entry, each row holding the card's old
# anchor. A count, not a size: system cards link to these cards and are
# written before them.
EMULATOR_CARDS_PER_PAGE = 400


# The headings an emulator's cards go under, in page order: what it needs
# first. DuckStation's three required BIOS were cards 12 to 14 under one
# heading, and MAME opened on 25 floppy sound samples.
CARD_SECTIONS = (("required", "Required"), ("optional", "Optional"), ("game_data", "Game data"))
_SECTION_RANK = {key: rank for rank, (key, _title) in enumerate(CARD_SECTIONS)}

# Below this many cards the page is one screen of them, and a table above
# would repeat it.
EMULATOR_SUMMARY_MIN = 6


def _card_section(f: dict) -> str:
    if f.get("category") == "game_data":
        return "game_data"
    return "required" if f.get("required") else "optional"


def _display_order(files: list) -> list[int]:
    """Entry indexes in page order: required, optional, then game data,
    each in profile order."""
    return sorted(range(len(files)), key=lambda i: _SECTION_RANK[_card_section(files[i])])


def emulator_parts(name: str, files: list) -> list[tuple[str, list[int]]]:
    """The pages an emulator's cards go to, with the entries each holds,
    or none when they fit on the emulator's page. Balanced, in page order."""
    if len(files) <= EMULATOR_CARDS_PER_PAGE:
        return []
    order = _display_order(files)
    count = -(-len(files) // EMULATOR_CARDS_PER_PAGE)
    size = -(-len(files) // count)
    return [(f"{name}-files-{index + 1}", order[start:start + size])
            for index, start in enumerate(range(0, len(files), size))]


def _sectioned(files: list, indexes: list[int], render: Callable[[list[int]], list[str]]) -> list[str]:
    """Each section's heading, then ``render`` of its entries among ``indexes``."""
    lines: list[str] = []
    for key, title in CARD_SECTIONS:
        group = [i for i in indexes if _card_section(files[i]) == key]
        if group:
            lines.extend([f"## {title}", "", *render(group)])
    return lines


def _hash_short(f: dict) -> str:
    """The shortest way to tell two dumps apart in a table: an md5 or crc32 prefix."""
    for field in ("md5", "crc32", "sha1"):
        value = f.get(field)
        value = value[0] if isinstance(value, list) and value else value
        if value:
            short = str(value).lower().split(",")[0].strip().removeprefix("0x")[:8]
            # Kept on one line: the label above the value doubled each row.
            return f"{field.upper()}\u00a0{_code(short)}"
    return ""


def _summary_row(f: dict, link: str, ids: str = "") -> str:
    shown = str(f.get("path") or f.get("name", ""))
    return (f"| {ids}[{_code(shown)}]({link}) | {_region_words(f.get('region'), 3) or '-'} "
            f"| {'yes' if f.get('required') else 'no'} | {_hash_short(f) or '-'} |")


_SUMMARY_HEAD = ["| File | Region | Required | Hash |", "|------|--------|----------|------|"]


def plan_emulator_pages(profiles: dict) -> None:
    """Record which page holds each emulator card, before any page links one."""
    stems = set()
    for name, profile in sorted(profiles.items()):
        if profile.get("type") in ("alias", "test"):
            continue
        files = profile.get("files") or []
        anchors = emulator_file_anchors(files)
        for stem, entries in emulator_parts(name, files):
            stems.add(stem)
            record_emulator_cards(name, stem, [anchors[i] for i in entries])
    clash = stems & set(profiles)
    if clash:
        raise RuntimeError(f"an emulator's card page takes a profile's address: {sorted(clash)}")


def generate_emulator_page(
    name: str,
    profile: dict,
    db: dict,
    consumers: FileConsumers | None = None,
    data_names: set[str] | None = None,
    data_registry: dict | None = None,
    data_dirs: list[dict] | None = None,
) -> str:
    """One emulator's page. ``data_dirs`` defaults to the profile's own."""
    return emulator_pages(
        name, profile, db, consumers, data_names, data_registry, data_dirs)[name]


def _emulator_intro(emu_name: str, profile: dict) -> str:
    """What the emulator needs, in plain words, from fields every profile has.

    The page opened on a twelve-row table: "Type", "Version", "Checked by".
    DuckStation needs one PlayStation BIOS of any name, and the reader had
    to infer it from the "Files found by" row."""
    names: list[str] = []
    for sys_id in profile.get("systems") or []:
        label = system_name(sys_id)
        if label not in names:
            names.append(label)
    files = [f for f in profile.get("files") or [] if isinstance(f, dict)]
    required = [f for f in files if f.get("required") is True]
    sentences = []
    if names:
        shown = names if len(names) <= 4 else names[:3] + [f"{len(names) - 3} more systems"]
        sentences.append(f"{emu_name} emulates {_said_as_list(shown)}.")
    by_region = _regional_choice(required)
    if by_region:
        # DuckStation needs the BIOS of the game's region, not all three: the
        # page said "3 of them required" and read as "get all of these".
        others = len(files) - len(required)
        sentences.append(f"It picks the BIOS for your game's region: {by_region}.")
        if others:
            sentences.append(
                f"The {_count(others, 'other file')} {'is' if others == 1 else 'are'} optional.")
    elif files:
        sentences.append(
            f"It reads {_count(len(files), 'file')}, {len(required):,} of them required: "
            "without a required file it does not start games."
            if required else
            f"It reads {_count(len(files), 'file')}, none of them required: it starts "
            "games without them."
        )
    if required and len(names) > 1:
        sentences.append("A required file is needed only by the machines that use it.")
        if profile.get("found_by") == "content":
            sentences.append("It recognises a file by its contents, so any file name works.")
        if any(f.get("source_ref") for f in files):
            sentences.append("Each file below cites the line of source code that reads it.")
    return " ".join(sentences)


def _regional_choice(required: list[dict], most: int = 6) -> str:
    """The required files as one choice by region, or "" when they are not one.

    Only when every required entry carries a region: and they name at least
    two: region: is the territory the code selects the file for, so the
    reader needs the one of the game's region.
    """
    if not required or len(required) > most or not all(f.get("region") for f in required):
        return ""
    if len({_region_words(f["region"]) for f in required}) < 2:
        return ""
    choices = [f"{f.get('name', '')} ({_region_words(f['region'], 3)})" for f in required]
    return ", ".join(choices[:-1]) + f" or {choices[-1]}"


def emulator_pages(
    name: str,
    profile: dict,
    db: dict,
    consumers: FileConsumers | None = None,
    data_names: set[str] | None = None,
    data_registry: dict | None = None,
    data_dirs: list[dict] | None = None,
) -> dict[str, str]:
    """An emulator's page by stem under emulators/, and the pages its cards
    went to when there are more than EMULATOR_CARDS_PER_PAGE."""
    if profile.get("type") == "alias":
        parent = profile.get("alias_of", profile.get("bios_identical_to", "unknown"))
        return {name: (
            f"# {name}\n\n"
            f"This core uses the same firmware as **{parent}**.\n\n"
            f"See [{parent}]({parent}.md) for details.\n"
        )}

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
    if data_dirs is None:
        data_dirs = profile.get("data_directories", [])

    part_pages: dict[str, str] = {}
    intro = _emulator_intro(emu_name, profile)
    lines = [
        f"# {emu_name}",
        "",
        *([intro, ""] if intro else []),
        markup.block_open("div", 'class="rb-meta-card"'),
        "",
        "| Detail | Value |",
        "|---|---|",
        f"| Type | {emu_type} |",
    ]
    if classification:
        cls_display = CLS_LABEL_ONE.get(classification, classification)
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
    lines.extend(_build_rows(profile))
    lines.append(f"| Profiled | {profiled} |")
    # The YAML is the reusable document this site renders, and the JSON is the
    # same profile for a program.
    lines.append(
        f"| Profile | [YAML]({REPO_URL}/blob/main/emulators/{name}.yml), "
        f"[JSON](../api/v1/emulators/{name}.json) |"
    )
    if cores:
        lines.append(f"| Cores | {', '.join(_distinct_spellings(cores))} |")
    if systems:
        sys_links = list(dict.fromkeys(_named_system_link(s, "../") for s in systems))
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

    lines.extend(_install_block(name, profile))

    lines.extend(_render_platform_details(profile))

    lines.extend(_render_structured_blocks(profile))

    if notes:
        body = _admonition_body(link_prose_citations(profile, notes)).replace("\n    ", "\n")
        lines.extend(markup.details("note", "Technical notes", body.split("\n")))

    if not files:
        lines.append("No BIOS or firmware files required.")
        if exclusion:
            lines.append("")
            lines.extend(markup.aside("info", "Why no files", [_escape_tags(str(exclusion))])[:-1])
    else:
        _file_available = _availability_check(db, data_names)

        lines.extend(["## Files", ""])
        lines.extend(_emulator_file_summary(files, _file_available))

        anchors = emulator_file_anchors(files)
        aliases = emulator_file_aliases(files, anchors)
        cards = [
            _render_emulator_file(f, profile, consumers, files, _file_available, anchor, name, alias)
            for f, anchor, alias in zip(files, anchors, aliases)
        ]
        parts = emulator_parts(name, files)
        if not parts:
            order = _display_order(files)
            if len(files) >= EMULATOR_SUMMARY_MIN:
                lines.extend(search_excluded([
                    *_SUMMARY_HEAD,
                    *(_summary_row(files[i], f"#{anchors[i]}") for i in order),
                ]))
            lines.extend(_sectioned(
                files, order, lambda group: [line for i in group for line in cards[i]]))
        else:
            lines.extend(_card_index(emu_name, files, anchors, aliases, parts))
            for number, (stem, entries) in enumerate(parts, 1):
                part_pages[stem] = _emulator_part_page(
                    name, emu_name, number, parts, [cards[i] for i in entries],
                    [files[i] for i in entries])

    if data_dirs:
        # The member tables are a file list with hashes: out of the search,
        # dolphin, primehack and ishiiruka each put 210 KB of it there.
        lines.extend([
            "## Data directories",
            "",
        ])
        trees_start = len(lines)
        member_anchors: set[str] = set()
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
                table = ["| File | Size | SHA1 |", "|------|-----:|------|"]
                for member, info in sorted(members.items()):
                    sha1 = str(info.get("sha1", ""))
                    # One id per page: two members can hold the same bytes
                    # (every empty file), and Dolphin's trees share Sys.
                    anchor = f"sha1-{sha1[:12]}"
                    span = "" if anchor in member_anchors else f'<span id="{anchor}"></span>'
                    member_anchors.add(anchor)
                    table.append(
                        f"| {span}{_code(member)} "
                        f"| {_fmt_size(int(info.get('size', 0)))} | {_code(sha1)} |"
                    )
                lines.append("")
                lines.extend(markup.details(
                    "info", f"{_count(len(members), 'file')}, as the packs ship them",
                    table, indent="    "))
        lines.append("")
        # The member tables are the lookup's.
        lines[trees_start:] = search_excluded(lines[trees_start:])

    lines.extend(_next_block([
        "[Get your files](../which-pack.md), for the setup you play on",
        "[Every system](../systems/index.md), and the files each one needs",
        "[When a game does not start](../wiki/troubleshooting.md)",
    ]))
    lines.extend(["", _date_line()])
    return {name: "\n".join(lines) + "\n", **part_pages}


def _card_index(
    emu_name: str, files: list[dict], anchors: list[str], aliases: list[str],
    parts: list[tuple[str, range]],
) -> list[str]:
    """Every entry of a split emulator page, one row each, linked to its card.

    Each row holds the ids the card had on this page, so a link cited
    before the split lands on the row, and site.js follows it to the card.
    """
    page_of = {index: stem for stem, entries in parts for index in entries}

    def table(group: list[int]) -> list[str]:
        rows = [
            _summary_row(
                files[i], f"{page_of[i]}.md#{anchors[i]}",
                "".join(f'<span id="{a}"></span>' for a in (anchors[i], aliases[i]) if a))
            for i in group
        ]
        return [
            markup.block_open("section", f'class="rb-card-index" {markup.search_exclude()}'),
            "", *_SUMMARY_HEAD, *rows, "", "</section>", "",
        ]

    return [
        f"{emu_name} reads more files than one page holds: each entry below "
        f"links to its card, on {_count(len(parts), 'page')}: "
        + ", ".join(f"[{number}]({stem}.md)" for number, (stem, _e) in enumerate(parts, 1))
        + ".",
        "",
        *_sectioned(files, _display_order(files), table),
    ]


def _emulator_part_page(
    name: str, emu_name: str, number: int, parts: list[tuple[str, range]],
    cards: list[list[str]], files: list[dict],
) -> str:
    """One page of a split emulator's cards."""
    first = str(files[0].get("path") or files[0].get("name", ""))
    last = str(files[-1].get("path") or files[-1].get("name", ""))
    others = ", ".join(
        f"[{n}]({stem}.md)" for n, (stem, _e) in enumerate(parts, 1) if n != number)
    lines = [
        f"# {emu_name} files, part {number} of {len(parts)}",
        "",
        f"Part of [{emu_name}]({name}.md), whose page has the profile, the install "
        f"command and every entry. This part holds {_count(len(files), 'entry', 'entries')}, "
        f"{_code(first)} to {_code(last)}. The other parts: {others}.",
        "",
        *_sectioned(files, list(range(len(files))),
                    lambda group: [line for i in group for line in cards[i]]),
    ]
    lines.extend(_next_block([f"[{emu_name}]({name}.md), the profile and every entry"]))
    lines.extend(["", _date_line()])
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


_PROBLEM_HASHES = ("sha1", "md5", "sha256", "crc32")


def _declared_hashes(config: dict, detail: dict) -> dict[str, list[str]]:
    """The hashes the platform declares for a reported file, by algorithm.

    The untested table said "declared hash contradicted by the local file"
    and named neither: the reader could not look for the dump RetroDECK asks
    for, nor report the difference.
    """
    from hashing import parse_hash_list  # noqa: PLC0415

    system = (config.get("systems") or {}).get(detail.get("system", "")) or {}
    for entry in system.get("files") or []:
        if isinstance(entry, dict) and entry.get("name") == detail.get("name"):
            return {
                algo: parse_hash_list(entry.get(algo))
                for algo in _PROBLEM_HASHES if parse_hash_list(entry.get(algo))
            }
    return {}


def _hash_cell(hashes: dict[str, list[str]]) -> str:
    return "<br>".join(
        f"{algo} " + ", ".join(_code(v) for v in values) for algo, values in hashes.items()
    )


def _copy_cell(path: str, algos: list[str], db: dict | None) -> str:
    """The file a path holds, with its hashes in the algorithms declared."""
    if not path:
        return "-"
    record = next(
        (rec | {"sha1": sha1} for sha1, rec in (db or {}).get("files", {}).items()
         if rec.get("path") == path),
        {},
    )
    hashes = {algo: [str(record[algo]).lower()] for algo in algos if record.get(algo)}
    shown = f"[{_code(path)}]({_fetch_url(path)})"
    return f"{shown}<br>{_hash_cell(hashes)}" if hashes else shown


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
                "platforms": [], "reason": p["reason"], "categories": set(),
            })
            entry["platforms"].append(p["platform"])
            entry["categories"].add(p.get("category", "bios"))
            if p["required"]:
                entry["required"] = True
        elif p["status"] == "untested":
            # Keyed by what is declared and what is held: two platforms
            # asking two dumps under one name are two rows.
            declared = p.get("declared") or {}
            key = f"{fname}\0{sorted(declared.items())}\0{p.get('path', '')}"
            entry = untested_files.setdefault(key, {
                "name": fname, "required": p["required"],
                "platforms": [], "reason": p["reason"],
                "declared": declared, "path": p.get("path", ""),
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
            f"{len(mismatch_files)} discrepancy.",
            "",
        ])

        if missing_files:
            lines.extend([
                f'### Missing <span class="rb-badge rb-badge-danger">'
                f"{_count(len(missing_files), 'file')}</span>",
                "",
                "Required is the platform's own flag. Category says what the "
                "platform's emulators do with the file: `bios` is firmware they "
                "load, `game_data` comes from a copy of the game, `unread` is "
                "opened by none of them. Only a missing `bios` entry stops a "
                "console from starting.",
                "",
                "| File | Category | Required | Platforms |",
                "|------|----------|----------|-----------|",
            ])
            for fname in sorted(missing_files):
                f = missing_files[fname]
                req = "yes" if f["required"] else "no"
                plats = ", ".join(sorted(set(f["platforms"])))
                cats = f["categories"]
                category = "bios" if "bios" in cats else ", ".join(sorted(cats))
                lines.append(f"| `{fname}` | {category} | {req} | {plats} |")
            lines.append("")

        if untested_files:
            lines.extend([
                f'### Untested <span class="rb-badge rb-badge-warning">'
                f"{_count(len(untested_files), 'file')}</span>",
                "",
                "Present, but not the expected hash. Declared is what the platform "
                "asks for, the collection copy is what the path holds.",
                "",
                "| File | Platforms | Reason | Declared | Collection copy |",
                "|------|----------|--------|----------|-----------------|",
            ])
            for key in sorted(untested_files):
                f = untested_files[key]
                plats = ", ".join(sorted(set(f["platforms"])))
                declared = f["declared"]
                lines.append(
                    f"| {_code(f['name'])} | {plats} | {f['reason']} "
                    f"| {_hash_cell(declared) or '-'} "
                    f"| {_copy_cell(f['path'], list(declared) or ['md5'], db)} |"
                )
            lines.append("")

        if mismatch_files:
            lines.extend([
                f'### Discrepancy <span class="rb-badge rb-badge-warning">'
                f"{_count(len(mismatch_files), 'file')}</span>",
                "",
                "The platform's own check passes, but the emulator's size or "
                "hash check fails. Each row names the check that fails.",
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


def _platform_lists_sentence(
    total: int, verified: int, untested: int, missing: int,
    not_firmware: dict[str, int] | None = None,
) -> str:
    """The platforms' own lists in one sentence, every count said.

    Four tiles opened the page: "Verified (100%)" for 5,437 of 5,438 and
    "Missing 0", above a table of red cells, on the page about what is
    missing. The gaps are in the files the emulators load, and the sentence
    says so. The missing count then read as missing firmware, while every
    entry behind it was game data or a file no emulator opens: the split by
    category is said with it.
    """
    def _is(count: int) -> str:
        return "is" if count == 1 else "are"

    not_firmware = {k: v for k, v in (not_firmware or {}).items() if v}
    parts = [f"{verified:,} {_is(verified)} held and pass the platform's own check"]
    if untested:
        parts.append(f"{untested:,} {_is(untested)} held but not verified "
                     "(listed under Untested below)")
    parts.append(f"{missing:,} {_is(missing)} missing")
    listed = ", ".join(parts[:-1]) + f" and {parts[-1]}"
    split = ""
    if missing and not_firmware:
        labels = {"game_data": "game data",
                  "unread": "opened by none of the platform's emulators"}
        firmware = missing - sum(not_firmware.values())
        others = [
            f"{count:,} {'is' if count == 1 else 'are'} {labels.get(category, category)}"
            for category, count in sorted(not_firmware.items())
        ]
        others_text = others[0] if len(others) == 1 else ", ".join(others[:-1]) + f" and {others[-1]}"
        head = (f"None of the missing is firmware: {others_text}" if not firmware
                else f"Of the missing, {firmware:,} {'is' if firmware == 1 else 'are'} "
                     f"firmware, {others_text}")
        split = f" {head}. Only missing firmware stops a console from starting."
    return (
        f"Of the {total:,} files on the platforms' own BIOS lists, {listed}.{split} "
        "The files the emulators load beyond those lists are counted per "
        "platform in the table below."
    )


def _missing_required_firmware(cov: dict) -> int:
    """Entries of a platform's list that are missing, required and firmware.

    Only those stop a console from starting. Game data and files no core
    opens are counted apart, and red on every short row carried no signal.
    """
    return len({
        (d.get("system", ""), d.get("name", "")) for d in cov.get("details") or []
        if d.get("status") == "missing" and d.get("required", True)
        and d.get("category", "bios") == "bios" and not d.get("settled_by")
    })


def _ratio_cell(done: int, total: int, alarm: int = 0) -> str:
    """A held-over-needed cell: the numbers, a bar, and a badge only when
    required firmware is missing."""
    if not total:
        return "-"
    share = done / total * 100
    cell = (f'<span class="rb-bar" style="--v:{share:.1f}%" aria-hidden="true"><span></span></span>'
            f"{done:,}/{total:,}")
    if alarm:
        cell += f' <span class="rb-badge rb-badge-danger">{alarm:,} required missing</span>'
    return cell


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
    1. Verification status, aggregated across all platforms
    2. Problem files: missing, untested, hash mismatch
    3. Core complement: emulator files not declared by any platform
    """

    from common import resolve_platform_cores

    # ---- Section 1: aggregate verify results across all platforms ----

    total_verified = 0
    total_untested = 0
    total_missing_verify = 0
    total_files_verify = 0
    not_firmware_totals: dict[str, int] = {}

    platform_problems: list[dict] = []
    settled_problems: list[dict] = []
    for pname, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        total_verified += cov["verified"]
        total_untested += cov["untested"]
        total_missing_verify += cov["missing"]
        total_files_verify += cov["total"]
        for category, count in (cov.get("missing_not_firmware") or {}).items():
            not_firmware_totals[category] = not_firmware_totals.get(category, 0) + count

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
                    "category": d.get("category", "bios"),
                    "reason": d.get("reason", ""),
                    "discrepancy": d.get("discrepancy", ""),
                    "system": d.get("system", ""),
                    "declared": _declared_hashes(cov.get("config") or {}, d),
                    "path": d.get("path") or "",
                })

    lines = [
        "# What the packs do not hold",
        "",
        "What each pack still cannot give you, and why. Most gaps are not "
        "firmware: a platform's list can also name game files, such as "
        "Diablo's `diabdat.mpq`, which you copy from your own game, or files "
        "no emulator of that platform opens. Those are counted apart. A missing "
        "firmware file means a console that does not start, and each one is "
        "named below with the platforms that list it.",
        "",
        "Each file here is a request: if you hold one of the files under "
        "\"Files to source\", [adding it](contributing.md) fills the gap for "
        "every platform at once.",
        "",
        " ".join([
            markup.button("Download gaps CSV", "downloads/gaps.csv"),
            markup.button("Open gaps API", "api/v1/gaps.json"),
            markup.button("All data exports", "data.md"),
        ]),
        "",
        _platform_lists_sentence(
            total_files_verify, total_verified, total_untested, total_missing_verify,
            not_firmware_totals,
        ),
        "",
    ]

    # ---- Verification per platform ----

    lines.extend([
        "## What each pack contains",
        "",
        "| Platform | On its BIOS list (destinations) | Files its emulators load | Checked by |",
        "|----------|-----------------:|-------------------------:|------------|",
    ])

    mode_labels = {
        "md5": "MD5 hash",
        "sha1": "SHA1 hash",
        "existence": "file presence",
    }

    for pname, cov in sorted(coverages.items(), key=lambda x: x[1]["platform"]):
        display = cov["platform"]
        core_total = (
            cov["core_present"] + cov["core_missing"] + cov["core_unsourceable"]
        )
        lines.append(
            f"| [{display}](platforms/{pname}.md) "
            f"| {_ratio_cell(cov['present'], cov['total'], _missing_required_firmware(cov))} "
            f"| {_ratio_cell(cov['core_present'], core_total)} "
            f"| {mode_labels.get(cov['mode'], cov['mode'])} |"
        )
    lines.extend([
        "",
        "Each fraction is what the pack has over what is needed, counting "
        "required and optional files alike since both ship. The first column "
        "is the BIOS list the platform publishes, counted by destination: a "
        "path the list declares twice is one file to place. A red badge marks "
        "a list that is short of required firmware. The second counts files its "
        "emulators load that this list never mentions, found by reading their "
        "source code, and it is often several times larger. What is short "
        "is named in the sections below. That second "
        "number is a floor, not a ceiling: an emulator that accepts any file "
        "handed to it names none in its code, so nothing there can be counted. "
        "Checked by is the test the platform runs on its own, replicated here "
        "from its source code "
        "([how each one works](wiki/verification-modes.md)).",
        "",
        "## How much of each list the emulator code confirms { #corroboration-against-emulator-source }",
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
        "| Platform | Missing from list | Not read by profiles | Hash conflict | Required status |",
        "|----------|------------------:|--------:|--------------:|----------------:|",
        *div_rows,
        "",
        "- **Missing from list**: a profiled emulator loads the file, the platform list does not mention it",
        "- **Not read by profiles**: on the platform list, loaded by no emulator profiled so far (a profile may still be incomplete)",
        "- **Hash conflict**: the platform list and the emulator source expect different hashes",
        "- **Required status**: required/optional differs between list and code",
        "",
        "A dash means no profiled emulator overlaps the platform's systems, "
        "so there is nothing to compare: the platform's own source is the "
        "only authority for its files.",
        "",
        "The same comparison drives `scripts/exporter/`: each platform's "
        "own file can be regenerated in its native format with these "
        "differences applied (System.dat, es_bios.xml, "
        "batocera-systems.json, ...).",
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

    src_totals: dict[str, int] = {
        "bios": 0, "data": 0, "large_file": 0, "bundled": 0, "missing": 0,
    }
    total_undeclared = 0
    emulator_gaps = []

    for emu_name, data in sorted(report_all.items()):
        if data["gaps"] == 0:
            continue
        total_undeclared += data["gaps"]
        for key, count in _gap_sources(data).items():
            src_totals[key] += count
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
            "## Files emulators load that no platform lists { #core-complement }",
            "",
            f"Files loaded by emulators but not declared by any platform. "
            f"{total_undeclared:,} files across {_count(len(emulator_gaps), 'emulator')}, "
            f"{total_available:,} available ({pct_available}), "
            f"{src_totals['bundled']:,} installed with their emulator, "
            f"{src_totals['missing']:,} to source.",
            "",
            "This counts every profiled emulator, including those no platform "
            "ships yet, so it reaches past what the pack tables above measure: "
            "those cover only the emulators each platform actually ships. "
            "Whatever is not in the collection is an acquisition target, named "
            "per emulator below.",
            "",
            "### Where the files come from",
            "",
            "| Source | Count | Description |",
            "|--------|------:|-------------|",
            f"| bios/ | {src_totals['bios']} | In repository (database.json) |",
            f"| data/ | {src_totals['data']} | Data directories (buildbot, GitHub) |",
            f"| release | {src_totals['large_file']} "
            "| GitHub release assets (large files) |",
            f"| bundled | {src_totals['bundled']} "
            "| Installed with the emulator's own package, not collected |",
            f"| missing | {src_totals['missing']} | Not available, needs sourcing |",
            "",
            "### Per emulator",
            "",
            "| Emulator | Undeclared | bios | data | release | Bundled | Missing |",
            "|----------|----------:|-----:|-----:|--------:|--------:|--------:|",
        ])

        for emu_name, data in sorted(emulator_gaps, key=lambda x: -x[1]["gaps"]):
            display = data["emulator"]
            sources = _gap_sources(data)
            m = sources["missing"]
            missing_str = (
                f'<span class="rb-badge rb-badge-danger">{m}</span>'
                if m > 0
                else '<span class="rb-badge rb-badge-success">0</span>'
            )
            lines.append(
                f"| [{display}](emulators/{emu_name}.md) "
                f"| {data['gaps']} "
                f"| {sources['bios']} "
                f"| {sources['data']} "
                f"| {sources['large_file']} "
                f"| {sources['bundled']} "
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
                "### Files to source",
                "",
                f"{_count(len(src_missing_details), 'file')}, {len(req_src)} required."
                + (
                    f" They come from {_count(entries, 'entry', 'entries')}: a file "
                    "declared with the same hash at several places is listed once."
                    if entries != len(src_missing_details) else ""
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

    lines.extend(["", _date_line()])
    return "\n".join(lines) + "\n"




def _core_scope_note(config: dict, profiles: dict) -> str:
    """" (all libretro)" when the platform resolves every libretro core.

    Read from what resolve_platform_cores returns, not from the shape of
    ``cores:``: RetroPie and RetroDECK name ``retroarch`` in a list, which
    pulls in every libretro core as ``all_libretro`` does, and the label
    showed for RetroArch and Lakka only.
    """
    every_libretro = resolve_platform_cores({"cores": "all_libretro"}, profiles)
    resolved = resolve_platform_cores(config, profiles)
    if not every_libretro or not every_libretro <= resolved:
        return ""
    others = len(resolved - every_libretro)
    if not others:
        return " (all libretro)"
    return f" (all libretro, plus {_count(others, 'other core')})"


def _gap_sources(data: dict) -> dict[str, int]:
    """Where each undeclared file of one emulator comes from.

    A file the emulator's own package installs is not one to find. The
    report's gap_missing counts it, and the gaps page announced 1,356 files
    to source where 609 are, with a red badge on emulators that have none.
    """
    counts = {"bios": 0, "data": 0, "large_file": 0, "bundled": 0, "missing": 0}
    for gap in data.get("gap_details") or []:
        source = gap.get("source", "")
        if source == "missing" and gap.get("bundled"):
            source = "bundled"
        if source in counts:
            counts[source] += 1
    return counts


def _xref_upstream(profile: dict) -> str:
    """The project a core comes from: its upstream, else its own source."""
    raw = profile.get("upstream") or profile.get("source", "")
    return raw if isinstance(raw, str) else ""


_FORGES = frozenset({
    "github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "git.eden-emu.dev",
    "git.ryujinx.app",
})


def upstream_project(url: str) -> tuple[str, str]:
    """(project key, revision or path inside it) for an upstream URL.

    MAME was twelve rows: mamedev/mame and one row per release tree
    (mamedev/mame/tree/mame0289 and the rest). On a forge the project is
    owner/repo, a GitLab project every group before `/-/`, and the rest is
    a revision. Elsewhere the address without its scheme, `www.` and
    trailing slash is the project. A note written after the address
    (`... (sys/src/games/nes/)`) is a path inside it.
    """
    url, _, note = url.strip().partition(" ")
    note = note.strip().strip("()")
    if not url.startswith(("http://", "https://")):
        return " ".join(filter(None, (url, note))), ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]
    if host == "gitlab.com" and len(parts) >= 2:
        cut = parts.index("-") if "-" in parts else len(parts)
        rest = parts[cut + 1:]
        if rest[:1] in (["tree"], ["blob"]):
            rest = rest[1:]
        return f"{host}/{'/'.join(parts[:cut])}", "/".join(filter(None, ("/".join(rest), note)))
    if host in _FORGES and len(parts) >= 2:
        repo = parts[1].removesuffix(".git")
        rest = parts[2:]
        if rest[:1] in (["tree"], ["blob"], ["src"]):
            rest = rest[1:]
        return f"{host}/{parts[0]}/{repo}", "/".join(filter(None, ("/".join(rest), note)))
    return "/".join([host, *parts]), note


def _upstream_label(key: str) -> str:
    """owner/repo on GitHub, host/owner/repo on another forge, and a
    Software Heritage revision by its short id."""
    swh = re.fullmatch(r"archive\.softwareheritage\.org/browse/revision/([0-9a-f]{12})[0-9a-f]*", key)
    if swh:
        return f"Software Heritage revision {swh[1]}"
    return key.removeprefix("github.com/")


def _breakable(label: str) -> str:
    """A project address that may wrap after each slash: unbroken, the
    longest pushed three of five columns off a 1,440 px screen."""
    return "/<wbr>".join(_escape_tags(part) for part in label.split("/"))


def _xref_platform_rows(config: dict, unique: dict) -> tuple[int, list[str]]:
    """The cores a platform pulls in, as table rows, with their count."""
    # The cores the builder and verify resolve: a hand copy here dropped
    # the libretro set a list naming retroarch pulls in, and the system
    # fallback EmuDeck relies on.
    matched = sorted(key for key in resolve_platform_cores(config, unique) if key in unique)
    rows = []
    for emu_name in matched:
        p = unique[emu_name]
        cls_raw = p.get("core_classification", "-")
        systems = p.get("systems", []) or []
        sys_str = ", ".join(_escape_tags(system_name(s)) for s in systems[:3])
        if len(systems) > 3:
            sys_str += f" +{len(systems) - 3}"
        files = p.get("files", []) or []
        libretro_only = sum(1 for f in files if f.get("mode") == "libretro")
        standalone_only = sum(1 for f in files if f.get("mode") == "standalone")
        file_str = str(len(files))
        parts = [f"{n} {label}" for n, label in ((libretro_only, "libretro-only"),
                                                  (standalone_only, "standalone-only")) if n]
        if parts:
            file_str += f" ({', '.join(parts)})"
        upstream = _xref_upstream(p)
        key, _rest = upstream_project(upstream)
        upstream_display = (f"[{_breakable(_upstream_label(key))}]({upstream.split()[0]})"
                            if upstream.startswith("http") else (_escape_tags(upstream) or "-"))
        rows.append(
            f"| [{_escape_tags(p.get('emulator', emu_name))}](emulators/{emu_name}.md) "
            f"| {CLS_LABELS.get(cls_raw, cls_raw)} | {sys_str or '-'} | {file_str} "
            f"| {upstream_display} |"
        )
    return len(matched), rows


def _render_xref_by_platform(
    coverages: dict, unique: dict, moved: dict[str, str] | None = None,
) -> list[str]:
    """Which cores each platform pulls in: counts here, a page per platform.

    One page held every platform's table, 30,551 nodes: the overview keeps
    the counts and each platform's table moves to ``moved`` by stem.
    """
    lines = [
        "## By platform",
        "",
        "| Platform | Cores | Core list |",
        "|----------|------:|-----------|",
    ]
    for pname in sorted(coverages, key=lambda x: coverages[x]["platform"]):
        cov = coverages[pname]
        display = cov["platform"]
        config = cov["config"]
        count, rows = _xref_platform_rows(config, unique)
        stem = f"cross-reference-{pname}"
        lines.append(
            f"| [{_escape_tags(display)}](platforms/{pname}.md) | {count}"
            f"{_core_scope_note(config, unique)} | [Cores on {_escape_tags(display)}]({stem}.md) |"
        )
        if moved is not None:
            moved[stem] = "\n".join([
                "---", "level: reference", "---", "",
                f"# Cores on {display}",
                "",
                f"{_count(count, 'core')}{_core_scope_note(config, unique)} that "
                f"{display} runs, the systems each emulates and the project it comes "
                f"from. Part of [Cores by platform](cross-reference.md). The files "
                f"are listed on the [{display} page](platforms/{pname}.md).",
                "",
                "| Core | Classification | Systems | Files | Upstream |",
                "|------|---------------|---------|-------|----------|",
                *rows,
                "",
            ]) + "\n"
    lines.append("")
    return lines


def _render_xref_by_upstream(
    coverages: dict, unique: dict
) -> list[str]:
    """The same relation read the other way: one row per upstream project."""
    lines: list[str] = [
        "## By upstream emulator",
        "",
        "One row per project: the releases and folders of one repository are "
        "listed in the Revisions column. Platforms counts the platforms that run "
        "at least one of its cores.",
        "",
        "| Cores | Upstream | Revisions | Classification | Platforms |",
        "|-------|----------|-----------|----------------|----------:|",
    ]

    by_upstream: dict[str, list[tuple[str, str, str]]] = {}
    for emu_name, p in sorted(unique.items()):
        # A null upstream is the project's own source: grouping it under
        # the string "None" put eight unrelated engines in one row.
        up_str = _xref_upstream(p)
        if up_str:
            key, rest = upstream_project(up_str)
            by_upstream.setdefault(key, []).append((emu_name, up_str, rest))

    platform_membership: dict[str, set[str]] = {}
    for pname, cov in coverages.items():
        for key in resolve_platform_cores(cov["config"], unique):
            platform_membership.setdefault(key, set()).add(cov["platform"])

    for key in sorted(by_upstream, key=lambda k: _upstream_label(k).casefold()):
        members = by_upstream[key]
        cores = sorted(c for c, _url, _rest in members)
        classifications = sorted({
            CLS_LABELS.get(unique[c].get("core_classification", "-"),
                           unique[c].get("core_classification", "-"))
            for c in cores
        })
        plats = sorted(set().union(*(platform_membership.get(c, set()) for c in cores)))
        revisions = sorted({rest for _c, _url, rest in members if rest})
        urls = sorted({url.split()[0] for _c, url, _rest in members})
        # The project's own address: the bare one when a core cites it.
        home = next((u for u in urls if not upstream_project(u)[1]), urls[0])
        upstream_cell = (f"[{_breakable(_upstream_label(key))}]({home})" if home.startswith("http")
                         else _escape_tags(key))
        lines.append(
            "| " + ", ".join(f"[{c}](emulators/{c}.md)" for c in cores)
            + f" | {upstream_cell} | {', '.join(_breakable(r) for r in revisions) or '-'} "
            f"| {', '.join(classifications)} | {len(plats)} |"
        )
    return lines


def generate_cross_reference(
    coverages: dict,
    profiles: dict,
    moved: dict[str, str] | None = None,
) -> str:
    """Cross-reference: counts per platform, then cores by upstream project.

    ``moved`` collects one page per platform with its core table.
    """
    unique = {
        k: v for k, v in profiles.items() if v.get("type") not in ("alias", "test")
    }

    total_cores = len(unique)
    total_upstreams = len({
        upstream_project(_xref_upstream(p))[0] for p in unique.values() if _xref_upstream(p)
    })

    lines = [
        "# Cores by platform",
        "",
        "Every core each platform runs, the systems it emulates, and the project "
        "it comes from. A core built for RetroArch is often a port of another "
        "emulator, and the two can differ in the files they read and the checks "
        "they make, so each core names its upstream project. "
        f"{total_cores} cores across {len(coverages)} platforms come from "
        f"{total_upstreams} upstream projects.",
        "",
        "The files a platform's emulators load and its own list does not name are "
        "counted on [What the packs do not hold](gaps.md).",
        "",
        " ".join([
            markup.button("Download cross-reference CSV", "downloads/cross-reference.csv"),
            markup.button("Open emulator API", "api/v1/emulators.json"),
            markup.button("All data exports", "data.md"),
        ]),
        "",
    ]

    lines.extend(_render_xref_by_platform(coverages, unique, moved))
    lines.extend(_render_xref_by_upstream(coverages, unique))

    lines.extend(["", _date_line()])
    return "\n".join(lines) + "\n"


def generate_contributing() -> str:
    return contributing_text(
        lambda page: f"{page}.md", "# Contributing", "on this site"
    ) + "\n" + "\n".join(_next_block([
        "[Profiling an emulator](wiki/profiling.md), the first task most "
        "contributions start from",
    ]))


# The end of a reference page: back to the index, or on to contributing.
def generate_not_found() -> str:
    """The page a stale link lands on. Pages have moved before (file cards
    refiled between manufacturers), so it offers the search, the two pages
    a visitor most often wants, and the list a moved page belongs to."""
    return "\n".join([
        "---",
        "search: exclude",
        "---",
        "",
        "# Page not found",
        "",
        '<p class="rb-lead">This address has no page. It may have moved since the link was written.</p>',
        "",
        '<p><button type="button" class="rb-notfound-search" data-rb-open-search>Search the site</button></p>',
        "",
        "- [Start here](index.md): what BIOS files are and how to install them",
        "- [Get your files](which-pack.md): the pack or the command for your device",
        "- Browse [platforms](platforms/index.md), [systems](systems/index.md) "
        "or [emulators](emulators/index.md)",
        "",
        '<p id="rb-notfound-hint" hidden></p>',
        "",
    ])


def reference_next() -> str:
    return "\n" + "\n".join(_next_block([
        "[Reference index](wiki/index.md), every reference page in one list",
        "For contributors: [How to contribute](contributing.md)",
    ]))


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

    # A real entry with real values: the PlayStation BIOS v3.0 (A).
    sample_key = "0555c6fae8906f3f09baf5988f00e55f88e9f30b"
    files = db.get("files", {})
    sample = files.get(sample_key) or next(iter(files.values()), {})
    lines = [
        "# Data model",
        "",
        "Three kinds of file hold everything this project knows. `database.json` "
        "lists every file the collection holds, keyed by its SHA1. A platform YAML "
        "copies the BIOS list a setup publishes. An emulator profile records what "
        "one emulator opens, read from its code. The sections below show the "
        "fields of each, with a real entry, so you can read the data or build on it.",
        "",
        "Example: `scph5501.bin` is one entry of `database.json` (key "
        "`0555c6fa...`), one entry of `platforms/batocera.yml` (its MD5) and one "
        "entry of `emulators/duckstation.yml` (its MD5, region and priority).",
        "",
        "## database.json",
        "",
        f"Primary key: SHA1. **{files_count}** file entries.",
        "",
        f"The entry of `{sample.get('name', '')}`:",
        "",
        "```json",
        *json.dumps(sample, indent=2, ensure_ascii=False).splitlines(),
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
        "`resolve_local_file` finds the file an entry describes by its hashes "
        "first, then by its path, then by its name, and a filename or "
        "destination never overrides a declared hash. The steps and their "
        "return codes are written once, in "
        "[the file resolution chain](verification-modes.md#file-resolution-chain).",
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
        "Core files then go under `core_destination` when the platform states one",
        "(`bios` for AmberELEC and EmuELEC, whose lists also name `neogeo/` and",
        "`ports/`), else under the root nearly every destination shares.",
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
        "from these same structures. Start with the [Data and API](../data.md) "
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
            # Archive members are read inside their archive, never by name.
            "names": {
                str(entry["name"]).lower()
                for entry in profile.get("files") or []
                if isinstance(entry, dict) and entry.get("name")
                and not entry.get("archive")
            },
        }
        for name, profile in profiles.items()
        if profile.get("type") != "alias"
    }


def _declared_by_system(config_files: dict) -> dict[str, list[dict]]:
    """The platform's own entries of each system, hashes included."""
    by_system: dict[str, list[dict]] = {}
    for (sys_id, _name), entries in config_files.items():
        by_system.setdefault(sys_id, []).extend(entries)
    return by_system


def _system_emulators(
    sys_id: str, emulator_files: dict | None, files: list[dict] | None = None
) -> list[str]:
    """The emulators a platform runs for one of its systems.

    A platform can group machines no profile names as one system: RetroArch
    files NAOMI and Atomiswave as sega-dreamcast-arcade, the Enterprise 64
    and 128 as enterprise-64-128. When no system id matches, the emulators
    are the ones that read a file the system lists, by its name. A member of
    an archive does not count: fbneo's boot.bin is read inside bubsys.zip,
    and RomM's Dreamcast boot.bin is not that file.
    """
    from common import _norm_system_id

    norm = _norm_system_id(sys_id)
    by_id = [name for name, data in (emulator_files or {}).items()
             if sys_id in data["systems"] or norm in data["norm"]]
    if by_id or not files:
        return by_id
    names = {str(f.get("name", "")).lower() for f in files} - {""}
    return [name for name, data in (emulator_files or {}).items()
            if names & data.get("names", set())]


# Navigation


_OS_NAMES = {"windows": "Windows", "darwin": "macOS", "linux": "Linux"}


def _runs_on(entry: dict, guide: Guide) -> str:
    """Where a setup runs, read from the systems the installer detects it on."""
    if guide.on_card:
        return "SD card, filled from a computer"
    if "server" in guide.run:
        return "A server"
    if "device" in guide.run:
        return "Its own system"
    systems = {rule.get("os") for rule in (entry.get("install") or {}).get("detect") or []}
    names = [label for key, label in _OS_NAMES.items() if key in systems]
    if "steamdeck" in guide.run:
        names.append("Steam Deck")
    if "android" in guide.run:
        names.append("Android")
    return ", ".join(names)


def _folder_cell(name: str, guide: Guide, prefix: str = "") -> str:
    """The BIOS folder in a table cell, or a link where it depends on the system."""
    if len(guide.folders) > 3:
        return f"Depends on the system: [listed on its page]({prefix}platforms/{name}.md#where-the-files-go)"
    return "<br>".join(f"{label}: {path}" for label, path in guide.folders)


def _pack_cell(name: str, record: dict, prefix: str = "") -> str:
    """The pack's parts and size, or the command where the release has none."""
    pack = release_record.pack_for(name, record)
    if record.get("tag") and not (pack and pack.get("assets")):
        return f"No ZIP yet. [Use the command]({prefix}platforms/{name}.md#install)."
    display = load_platform_config(name, "platforms").get("platform", name)
    links = download_links(name, record, REPO_URL, display)
    size = pack.get("download_size") if pack else None
    return f"{links} ({_fmt_size(size)})" if size else links


def _group_keys(registry: dict, group: str, display: dict[str, str]) -> list[str]:
    return sorted(
        (k for k in registry if registry[k].get("device_group") == group),
        key=lambda k: display[k].casefold(),
    )


def _own_root_folders(registry: dict) -> dict[str, str]:
    """Platforms whose pack holds its own top folder, with where it unzips.

    The same rule as extract_notes: a list written from an empty base
    destination carries its root in every path."""
    folders = {}
    for key, entry in registry.items():
        config = load_platform_config(key, "platforms")
        if config.get("base_destination") == "" and entry.get("extract_to"):
            folders[key] = entry["extract_to"]
    return folders


def generate_which_pack() -> str:
    """The device picker: every registered platform as a tile under the
    device it runs on, then one table per device with its BIOS folder and
    its ZIP, the console picker, and the command for a setup not listed.

    The tiles are the choice. A second set of cards, five tables and the
    picker offered the same choice three times."""
    registry = load_platform_registry("platforms")
    record = release_record.load_record()
    display = {key: load_platform_config(key, "platforms").get("platform", key) for key in registry}
    own_root = _own_root_folders(registry)
    all_tabs = ("windows", "macos", "linux", "steamdeck", "android")
    lines = [
        "# Get your files",
        "",
        "Pick your setup. Its page gives the folder, the command and the ZIP.",
        "",
        "## Pick your setup",
        "",
        *setup_picker(registry, display),
        "## Folders and ZIP files { #manual-download }",
        "",
        "- A big ZIP comes in parts, like `Pack.part1of2.zip`. Download every part.",
        "- Unzip them all into the BIOS folder of your setup. Put the files "
        "straight in it, not in a new folder.",
        "- Some files inside stay zipped, like `neogeo.zip`. Leave them as they are.",
        "- **Unzip into** means the ZIP holds its own top folder. Unzip it there.",
        "",
    ]
    no_zip = False
    for group, (label, _description) in DEVICE_GROUPS.items():
        members = _group_keys(registry, group, display)
        if not members:
            continue
        lines += [
            f"### {label} {{ #{GROUP_ANCHORS[group]} }}",
            "",
            "| Setup | Runs on | BIOS folder | ZIP |",
            "|-------|---------|-------------|-----|",
        ]
        for key in members:
            guide = GUIDES[key]
            folder = (f"Unzip into `{own_root[key]}`" if key in own_root
                      else _folder_cell(key, guide))
            pack = release_record.pack_for(key, record)
            no_zip |= bool(record.get("tag") and not (pack and pack.get("assets")))
            lines.append(
                f"| [{display[key]}](platforms/{key}.md) | "
                f"{guide.runs_on or _runs_on(registry[key], guide)} | {folder} | "
                f"{_pack_cell(key, record)} |"
            )
        for setup, _summary, uses, runs_on, folder in ALSO_RUNS.get(group, ()):
            lines.append(
                f"| {setup} | {runs_on} | {folder}, same as "
                f"[{display[uses]}](platforms/{uses}.md) | {_pack_cell(uses, record)} |"
            )
        lines.append("")
        if group == "handheld":
            lines += markup.details("note", "Other handhelds that run RetroArch", [
                "They use the RetroArch files, in the folder below. The command "
                "finds it.",
                "",
                "| System | Runs | BIOS folder |",
                "|--------|------|-------------|",
                *(f"| {name} | [RetroArch](platforms/retroarch.md) | `{folder}` |"
                  for name, folder in _detected_systems(registry, "retroarch")),
            ])
    if no_zip:
        lines += ["Setups added after the last release have no ZIP until the next one.", ""]
    lines += [
        f"The ZIPs come from the [latest release]({RELEASE_URL}). The command may "
        "install newer files.",
        "",
        "## Only some consoles?",
        "",
        "Pick them here, and get the files and the command. Or run the command "
        "below and press **c** when it asks.",
        "",
        "<FilePicker />",
        "",
        "## Not in the list? One command finds it { #quick-install }",
        "",
        "A **terminal** is a window where you type commands.",
        "",
        *terminal_tabs(all_tabs, {}),
        "On Batocera, Recalbox and other game systems, run it on the device. "
        "Your setup's page shows how.",
        "",
        "The command asks what to install. Press **Enter** for everything.",
        "",
        "```text title=\"What it asks\"",
        "  Enter) install everything",
        "  i) only what the installed cores read",
        "  c) choose systems, cores and regions",
        "  q) quit",
        "```",
        "",
        *trust_box(""),
        "**Check your folder later.** Add `--check`. It changes nothing.",
        "",
        *terminal_tabs(all_tabs, dict.fromkeys(TERMINALS, "--check"), steps=False),
        *markup.details("note", "Fill an SD card from a computer", [
            "Name your setup and the BIOS folder on the card. For RetroArch on a "
            "Nintendo Switch:",
            "",
            "Linux, macOS:",
            "",
            *fence("sh", install_command(
                "sh", "--platform retroarch --dest /path/to/sdcard/retroarch/cores/system")),
            "",
            "Windows (PowerShell, E: being the card):",
            "",
            *fence("powershell", install_command(
                "powershell", "--platform retroarch --dest E:\\retroarch\\cores\\system")),
        ]),
        "Details: [All installer options](wiki/installer.md).",
        "",
        *_next_block([
            "[First install, step by step](wiki/first-install.md)",
            "[When a game does not start](wiki/troubleshooting.md)",
        ]),
    ]
    return "\n".join(lines) + "\n"


def nav_emulators(profiles: dict) -> dict[str, dict]:
    """The emulators listed in the navigation, by key.

    A profile that emulates no system and reads no file (the libretro sample
    and test cores, games with nothing to load) answers no question about
    BIOS files. Its page stays, reachable from the overview table and the
    search, and the navigation is left to the cores people look up.
    """
    return {
        key: profile for key, profile in unique_emulator_profiles(profiles).items()
        if profile.get("systems") or profile.get("files")
    }


def _emulator_nav(profiles: dict) -> list:
    """Emulators by initial (A, B, C), one level deep. A name that does not
    open on a letter goes under "0 to 9". The route middleware draws every
    initial but the open one as a single link, so a page carries the links
    of one initial only."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for key, profile in nav_emulators(profiles).items():
        display = str(profile.get("emulator", key))
        groups.setdefault(emulator_initial(display), []).append((display, f"emulators/{key}.md"))
    nav: list = ["emulators/index.md"]
    for label in sorted(groups, key=lambda g: (g != INITIAL_DIGITS, g)):
        items = sorted(groups[label], key=lambda e: e[0].casefold())
        nav.append({label: [{display: path} for display, path in items]})
    return nav


def _system_nav(manufacturers: dict) -> list:
    """Manufacturer pages, the machines apart from the game and engine trees."""
    machines, games = [], []
    for mfr in sorted(manufacturers, key=str.casefold):
        entry = {mfr: f"systems/{_slug(mfr)}.md"}
        (games if mfr in GAME_DATA_TOPS else machines).append(entry)
    nav: list = ["systems/index.md", {"Consoles and computers": machines}]
    if games:
        nav.append({"Game engines and PC games": games})
    return nav


def _platform_nav(coverages: dict, registry: dict) -> list:
    """Platforms by the kind of device they run on, as on the device picker."""
    nav: list = ["platforms/index.md"]
    for group, (label, _description) in DEVICE_GROUPS.items():
        members = [
            {coverages[name]["platform"]: f"platforms/{name}.md"}
            for name in sorted(coverages, key=lambda n: coverages[n]["platform"].casefold())
            if registry.get(name, {}).get("device_group") == group
        ]
        if members:
            nav.append({label: members})
    return nav


# The tabs in reading order, by the level their pages declare.
TABS = {
    "start": "Start here",
    "setup": "Your setup",
    "explore": "Explore",
    "reference": "Reference",
    "contribute": "Contribute",
}


class NavError(ValueError):
    """A wiki page does not say where it goes in the navigation."""


def _h1(markdown: str) -> str:
    match = re.search(r"^# (.+?)(?:\s*\{[^}]*\})?\s*$", markdown, re.M)
    return match.group(1).strip() if match else ""


NavPlace = tuple[int, object, str]


def wiki_nav(wiki_dir: Path | None = None) -> dict[str, list[NavPlace]]:
    """Wiki pages by tab, each at the place its front matter gives.

    A page states `level:` (its tab), `order:` (its place in the tab, the
    smallest first) and, when the navigation names it otherwise than its
    first heading, `nav_title:`. `group:` files it under a labelled group
    of its tab. `nav: false` keeps a page out on purpose. A page that says
    neither raises, naming what to add.
    """
    wiki_dir = Path(wiki_dir) if wiki_dir else _REPO_ROOT / WIKI_SRC_DIR
    tabs: dict[str, list[NavPlace]] = {level: [] for level in TABS}
    for page in sorted(wiki_dir.glob("*.md")):
        where = f"{WIKI_SRC_DIR}/{page.name}"
        text = page.read_text(encoding="utf-8")
        meta = _front_matter(text)
        level = meta.get("level")
        if level not in TABS:
            raise NavError(
                f"{where}: front matter needs `level:`, one of {', '.join(TABS)}, "
                f"to place the page in a tab (found {level!r})"
            )
        if meta.get("nav") is False:
            continue
        order = meta.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            raise NavError(
                f"{where}: front matter needs `order:`, a whole number giving its "
                f"place in the {TABS[level]} tab, smallest first (found {order!r}), "
                "or `nav: false` to keep it out of the navigation"
            )
        path = f"{WIKI_SRC_DIR}/{page.name}"
        if page.name == "index.md":
            tabs[level].append((order, path, ""))
            continue
        title = meta.get("nav_title") or _h1(text)
        if not title:
            raise NavError(f"{where}: no `# ` heading and no `nav_title:` to name it")
        group = meta.get("group") or ""
        if not isinstance(group, str):
            raise NavError(f"{where}: `group:` names a group of the {TABS[level]} tab (found {group!r})")
        tabs[level].append((order, {str(title): path}, group))
    return tabs


def _overview_anchors(nav: list, docs: Path) -> dict[str, set[str]]:
    """The heading ids of each overview page the navigation names, where a
    group of the sidebar finds its own section."""
    def overviews(value: object) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for entry in value:
                yield from overviews(entry)
        elif isinstance(value, dict):
            for child in value.values():
                if isinstance(child, list):
                    yield from overviews(child)

    anchors: dict[str, set[str]] = {}
    for rel in overviews(nav):
        stem = rel.rsplit(".", 1)[0]
        page = next((docs / f"{stem}{suffix}" for suffix in (".md", ".mdx")
                     if (docs / f"{stem}{suffix}").is_file()), None)
        if page:
            anchors[rel] = sitestarlight.heading_ids(page.read_text(encoding="utf-8"))
    return anchors


def _nav_target(entry: object) -> str:
    return entry if isinstance(entry, str) else str(next(iter(entry.values())))


def generate_nav(
    coverages: dict,
    manufacturers: dict,
    profiles: dict,
    registry: dict | None = None,
    wiki_dir: Path | None = None,
) -> list:
    """The navigation, five tabs in reading order.

    Start here is for someone who knows nothing yet, Your setup for someone
    who knows their device, Explore for one system or emulator, Reference for
    checking the work, Contribute for adding to it. Each tab opens on its
    index page.

    The generated pages hold the places below. A wiki page takes its own
    place from its front matter (wiki_nav), so a new page needs no edit here.
    A group of a tab sits at the place of its first page, its pages in their
    own order.
    """
    generated: dict[str, list[tuple]] = {
        "start": [(0, "index.md"), (20, {"Get your files": "which-pack.md"})],
        "setup": [(10 * i, item)
                  for i, item in enumerate(_platform_nav(coverages, registry or {}))],
        "explore": [
            (0, "explore.md"),
            (10, {"Systems": _system_nav(manufacturers)}),
            (20, {"Emulators": _emulator_nav(profiles)}),
        ],
        "reference": [
            # Find a file first: the one tool a reader comes to with a file in hand.
            (5, {"Find a file": "files/index.md"}, "Checking files"),
            (20, {"What the packs do not hold": "gaps.md"}, "Checking files"),
            (30, {"Cores by platform": "cross-reference.md"}, "Checking files"),
            (40, {"Dump provenance": "provenance.md"}, "Checking files"),
            (60, {"Data and API": "data.md"}, "Data"),
            (64, {"Data model": "wiki/data-model.md"}, "Data"),
            (68, {"Updated platform files": "exports.md"}, "Data"),
        ],
        "contribute": [(0, "contributing.md")],
    }
    wiki = wiki_nav(wiki_dir)
    nav = []
    for level, label in TABS.items():
        places = [(place[0], place[1], place[2] if len(place) > 2 else "")
                  for place in generated[level]]
        entries = sorted(places + wiki[level], key=lambda item: item[0])
        for (first, a, _), (second, b, _) in zip(entries, entries[1:]):
            if first == second:
                raise NavError(
                    f"{label} tab: {_nav_target(a)} and {_nav_target(b)} both take "
                    f"order {first}; give one of them a free number"
                )
        items: list = []
        groups: dict[str, list] = {}
        for _order, entry, group in entries:
            if not group:
                items.append(entry)
            elif group in groups:
                groups[group].append(entry)
            else:
                groups[group] = [entry]
                items.append({group: groups[group]})
        nav.append({label: items})
    return nav


# Main


def main() -> None:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Generate the documentation site from project data"
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
        "--site-dir", default=STARLIGHT_DIR,
        help="the Astro Starlight project the site is written into (default: %(default)s)",
    )
    parser.add_argument(
        "--docs-dir", default=None,
        help="directory the pages are written to (default: the project's "
             "content collection, src/content/docs)",
    )
    args = parser.parse_args()
    if args.docs_dir is None:
        args.docs_dir = str(Path(args.site_dir) / STARLIGHT_CONTENT)
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
    # Files the site serves as they are (data, downloads, images), in the
    # project's public folder.
    public = Path(args.site_dir) / "public"

    for d in GENERATED_DIRS:
        (public / d if d in PUBLIC_DIRS else docs / d).mkdir(parents=True, exist_ok=True)

    # No robots.txt: a crawler reads it only at the host root (RFC 9309), and
    # this project site lives under /retrobios/. Every page's head points at
    # the sitemap instead (Starlight's head links it).
    (public / "robots.txt").unlink(missing_ok=True)

    images_dest = public / "assets" / "images"
    images_dest.mkdir(parents=True, exist_ok=True)
    # Logos, system icons and sharing cards are served from the site itself
    # (docs_assets/*/sources.json records where each came from).
    # brand: the mark, favicons and touch icon (scripts/make_site_icons.py).
    folders = [("logos", "logos"), ("system-icons", "system-icons"), ("og", "og"),
               ("brand", "brand")]
    for folder, dest in folders:
        source = Path("docs_assets") / folder
        if source.is_dir():
            shutil.copytree(source, public / "assets" / dest, dirs_exist_ok=True)
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
    coverages = platform_coverages(
        sorted(platform_names), args.platforms_dir, db, data_registry,
        emulators_dir=args.emulators_dir, db_path=args.db,
        report=lambda _name, cov: print(
            f"  {cov['platform']}: {cov['present']}/{cov['total']} ({_pct(cov['present'], cov['total'])})"
        ),
    )

    print("Loading emulator profiles...")
    profiles = load_emulator_profiles(args.emulators_dir, skip_aliases=False)
    unique_count = sum(1 for p in profiles.values() if p.get("type") != "alias")
    print(
        f"  {len(profiles)} profiles ({unique_count} unique, {len(profiles) - unique_count} aliases)"
    )

    consumers = _build_file_consumers(db, coverages, profiles)
    emulator_files = _build_emulator_file_index(profiles)

    print("Generating home page...")
    write_if_changed(
        str(docs / "index.md"), generate_home(
            db, coverages, profiles, registry, args.platforms_dir
        )
    )

    write_if_changed(str(docs / "explore.md"), generate_explore_index(db, coverages, profiles))

    stats = compute_stats(db, coverages, profiles)
    stats["composition"] = compute_composition(db)
    write_if_changed(str(public / "stats.json"), generate_stats(stats))

    # Computed once: the gap analysis page and the published gaps export must
    # report the same files.
    gap_report = build_emulator_gap_report(profiles, coverages, db, suppl_names)

    # Build system_id -> manufacturer page map (needed by all generators)
    plan_emulator_pages(profiles)
    print("Building system cross-reference map...")
    manufacturers = _group_by_manufacturer(db)
    set_system_page_map(
        _system_pages_from_data(manufacturers, coverages, db, profiles)
    )
    print(f"  {len(_system_page_map)} system IDs mapped to pages")
    set_console_sections(manufacturers, program_names(profiles))

    print("Generating system pages...")

    prime_system_icons({
        _icon_name(mfr, console)
        for mfr, consoles in manufacturers.items()
        for console in consoles
    })

    write_if_changed(
        str(docs / "systems" / "index.md"), generate_systems_index(manufacturers)
    )
    console_installs = system_install_choices(coverages)
    (docs / "files").mkdir(parents=True, exist_ok=True)
    write_if_changed(str(docs / "files" / "index.md"), generate_files_page())
    reset_console_pages()
    for mfr, consoles in manufacturers.items():
        slug = mfr.lower().replace(" ", "-")
        pages = system_pages(mfr, consoles, consumers, console_installs.get(slug))
        for stem, (page, held) in pages.items():
            PAGE_DATA[f"systems/{stem}.md"] = system_structured_data(stem, held)
            if stem != slug:
                PART_PAGES.add(f"systems/{stem}.md")
            write_if_changed(str(docs / "systems" / f"{stem}.md"), page)
        write_single_item(
            public / "api" / "v1" / "systems" / f"{slug}.json", "systems",
            system_export_item(slug, mfr, consoles),
            str(db.get("generated_at") or _timestamp()),
        )
    clash = split_system_stems() & {_slug(m) for m in manufacturers}
    if clash:
        raise RuntimeError(f"a console page takes a manufacturer's address: {sorted(clash)}")

    print("Generating static API and bulk data exports...")
    exports = generate_data_exports(
        public, db, coverages, profiles, stats, gap_report
    )
    write_if_changed(str(docs / "data.md"), generate_data_page(exports) + reference_next())
    PAGE_DATA["data.md"] = dataset_structured_data(exports, db)

    export_reports = load_export_reports()
    _EXPORTED.update(report["platform"] for report in export_reports)

    print("Generating platform pages...")
    write_if_changed(
        str(docs / "platforms" / "index.md"),
        generate_platform_index(coverages, registry),
    )
    for name, cov in coverages.items():
        # The emulators this platform runs, not every one of the system.
        runs = resolve_platform_cores(cov["config"], profiles)
        moved: dict[str, str] = {}
        write_if_changed(
            str(docs / "platforms" / f"{name}.md"),
            generate_platform_page(name, cov, registry, {
                emu: data for emu, data in emulator_files.items() if emu in runs
            }, moved),
        )
        for stem, page in moved.items():
            if stem in coverages:
                raise RuntimeError(f"a system page takes the platform {stem}'s address")
            PART_PAGES.add(f"platforms/{stem}.md")
            write_if_changed(str(docs / "platforms" / f"{stem}.md"), page)
        PAGE_DATA[f"platforms/{name}.md"] = platform_structured_data(name, cov)

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
    (public / "api" / "v1" / "emulators").mkdir(parents=True, exist_ok=True)
    for name, profile in public_profiles.items():
        # The profile itself, one small document per core: reusing one
        # meant downloading emulators.json (5.9 MB) and filtering it.
        # The envelope and item of emulators.json, one item long: the
        # profile validates against the published schema, which an "id"
        # beside its fields broke on all 530, and schema_version is there.
        write_if_changed(
            str(public / "api" / "v1" / "emulators" / f"{name}.json"),
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
        pages = emulator_pages(
            name, profile, db, consumers, suppl_names, data_registry,
            data_dirs=page_data_directories(name, profiles),
        )
        PAGE_DATA[f"emulators/{name}.md"] = emulator_structured_data(profile)
        if (profile.get("type") in ("launcher", "alias")
                or profile.get("core_classification") in ("launcher", "alias", "frozen_snapshot")):
            SEARCH_DEMOTED.add(f"emulators/{name}.md")
        files = profile.get("files") or []
        for stem, entries in emulator_parts(name, files):
            PAGE_DATA[f"emulators/{stem}.md"] = emulator_structured_data(
                {**profile, "files": [files[i] for i in entries]})
            PART_PAGES.add(f"emulators/{stem}.md")
        for stem, page in pages.items():
            write_if_changed(str(docs / "emulators" / f"{stem}.md"), page)

    print("Generating cross-reference page...")
    xref_pages: dict[str, str] = {}
    write_if_changed(
        str(docs / "cross-reference.md"),
        tables_out_of_search(generate_cross_reference(coverages, profiles, xref_pages))
        + reference_next(),
    )
    for stem, page in xref_pages.items():
        write_if_changed(str(docs / f"{stem}.md"), tables_out_of_search(page))

    print("Generating gap analysis page...")
    write_if_changed(
        str(docs / "gaps.md"),
        tables_out_of_search(generate_gap_analysis(
            profiles, coverages, db, suppl_names, registry, gap_report
        )) + reference_next(),
    )

    print("Generating updated platform files page...")
    write_if_changed(
        str(docs / "exports.md"),
        generate_exports_page(
            export_reports,
            {name: cov["platform"] for name, cov in coverages.items()},
        ) + reference_next(),
    )

    print("Generating dump provenance page...")
    provenance_report = build_report(db, load_provenance_snapshots())
    write_if_changed(
        str(docs / "provenance.md"),
        tables_out_of_search(
            generate_provenance_page(db, provenance_report, load_derivable())
        ) + reference_next(),
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

    # Swept before the decoration, which rewrites every page it finds: a
    # page no generator produced any more (a console moved back onto its
    # manufacturer's page) was decorated, recorded, and kept for ever.
    stale = _sweep_generated(docs, [d for d in GENERATED_DIRS if d not in PUBLIC_DIRS])
    stale += _sweep_generated(public, list(PUBLIC_DIRS))

    write_if_changed(str(docs / "404.md"), generate_not_found())

    print("Adding page metadata and structured data...")
    lastmod = decorate_markdown_pages(docs)

    nav = generate_nav(coverages, manufacturers, profiles, registry,
                              Path(WIKI_SRC_DIR))
    print("Writing the navigation and the picker's data...")
    site = Path(args.site_dir)
    present = {
        path.relative_to(docs).with_suffix(".md").as_posix()
        for path in docs.rglob("*.md*") if path.suffix in (".md", ".mdx")
    } - {"404.md"}
    generated = site / "src" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    write_if_changed(str(generated / "sidebar.json"),
                     json.dumps(sitestarlight.sidebar(nav, present, _overview_anchors(nav, docs)),
                                indent=1) + "\n")
    write_if_changed(str(generated / "redirects.json"),
                     json.dumps(sitestarlight.redirects(profiles, present), indent=1,
                                sort_keys=True) + "\n")
    write_if_changed(str(generated / "lastmod.json"),
                     json.dumps(dict(sorted(lastmod.items())), indent=1) + "\n")
    system_ids = set(_system_page_map) | {
        sys_id for cov in coverages.values() for sys_id in cov["config"].get("systems", {})
    }
    titles = {sys_id: system_name(sys_id) for sys_id in sorted(system_ids)}
    sitestarlight.picker_data(_REPO_ROOT / "install", public / "data" / "picker", titles)
    total_pages = len(present)
    print(f"\nGenerated {total_pages} pages in {args.docs_dir}/")
    if stale:
        print(f"Removed {stale} page(s) nothing produced this run")


from sitediagrams import local_images, prepare_page, unresolved_images  # noqa: E402,F401
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
    _field_text,
    _render_yaml_value,
    _slugify_anchor,
    _system_page_map,
    _system_link,
    set_system_page_map,
    set_console_sections,
    program_names,
    system_name,
    card_page,
    console_href,
    console_anchor,
    record_console_pages,
    record_emulator_cards,
    emulator_card_page,
    reset_console_pages,
    split_system_stems,
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
    ICON_DIR,
)

if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
