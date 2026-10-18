"""What the cores need that the platform does not declare.

A pack is the platform's own list plus the files its cores load. The
second half is derived here, and the region pass reads the grouping
this module builds so the report and the pack withdraw the same files."""

from __future__ import annotations

from collections.abc import Iterable

from common import PROFILE_IDENTITY_FIELDS, _norm_system_id
from common import archive_members
from common import declared_root, expand_platform_declared_names, platform_declarations
from common import load_emulator_profiles
from common import resolution_is_hash_exact
from common import resolve_local_file
from common import runs_standalone
from common import sanitize_pack_path
from common import size_fits
from validation import read_from_system_dir


def _detect_extras_prefix(config: dict, base_dest: str) -> str:
    """The prefix core-extra destinations take (may be empty).

    When base_destination is empty (RetroDECK), it is the dominant root of
    the YAML-declared destinations.
    """
    return base_dest or declared_root(config)

def _detect_slug_structure(config: dict) -> tuple[bool, dict[str, str]]:
    """Detect whether a platform uses per-system slug destinations.

    Returns ``(is_slug_based, system_to_slug)`` where ``system_to_slug``
    maps system IDs to their destination slug prefix.  Slug-based means
    each system's files live under a per-system subfolder (e.g. RomM's
    ``bios/{platform_slug}/{file}``), with varying slugs across systems.

    Only returns True when nearly ALL destinations have a subfolder and
    nearly ALL systems map to a consistent slug, distinguishing true
    slug-based layouts (RomM) from platforms that happen to have some
    subfoldered files (RetroArch ``dc/``, ``neocd/``).
    """
    total_files = 0
    files_with_slash = 0
    sys_to_slug: dict[str, str] = {}
    total_systems_with_files = 0
    for sys_id, sys_data in config.get("systems", {}).items():
        files = sys_data.get("files", [])
        if not files:
            continue
        total_systems_with_files += 1
        slugs: set[str] = set()
        for f in files:
            d = f.get("destination", "")
            if d:
                total_files += 1
                if "/" in d:
                    files_with_slash += 1
                    slugs.add(d.split("/", 1)[0])
        if len(slugs) == 1:
            sys_to_slug[sys_id] = slugs.pop()

    if not sys_to_slug or total_files == 0:
        return False, {}
    # All conditions must hold for slug-based detection:
    # 1. Nearly all files have a subfolder
    # 2. Multiple distinct slugs (not a constant prefix)
    # 3. Nearly all systems with files map to a slug
    # 4. Files are exactly slug/filename (depth 2), not deeper
    unique_slugs = set(sys_to_slug.values())
    all_have_slash = files_with_slash / total_files > 0.95
    varying_slugs = len(unique_slugs) > 1
    high_coverage = len(sys_to_slug) / total_systems_with_files > 0.9
    # Count files deeper than slug/filename (e.g., amiga/bios/kick.rom)
    deep_files = 0
    for sys_data in config.get("systems", {}).values():
        for f in sys_data.get("files", []):
            d = f.get("destination", "")
            if d and d.count("/") > 1:
                deep_files += 1
    shallow = deep_files / total_files < 0.05 if total_files else True
    return (all_have_slash and varying_slugs and high_coverage and shallow), sys_to_slug

def _map_emulator_to_slug(
    profile: dict,
    platform_systems: set[str],
    norm_map: dict[str, str],
    sys_to_slug: dict[str, str],
) -> str:
    """Map an emulator to a destination slug for slug-based platforms."""
    from common import _norm_system_id

    emu_systems = set(profile.get("systems", []))
    # Direct match
    direct = emu_systems & platform_systems
    if direct:
        target = sorted(direct)[0]
        return sys_to_slug.get(target, "")
    # Normalized match
    for es in sorted(emu_systems):
        norm = _norm_system_id(es)
        if norm in norm_map:
            target = norm_map[norm]
            return sys_to_slug.get(target, "")
    return ""

def _slug_for(
    u: dict, profiles: dict, platform_systems: set, norm_map: dict, sys_to_slug: dict
) -> str:
    """The platform slug a report entry files under, or empty.

    The entry's own system decides first: picodrive's Mega CD BIOS belongs
    under segacd/, where RomM looks for a Mega CD game's firmware, not under
    gamegear/, the first of the emulator's systems.
    """
    own = u.get("system")
    if own:
        slug = _map_emulator_to_slug(
            {"systems": [own]}, platform_systems, norm_map, sys_to_slug
        )
        if slug:
            return slug
    emu_name = u.get("profile") or u.get("emulator", "")
    profile = profiles.get(emu_name, {})
    if not profile:
        # The report names the display name where the key failed.
        profile = next(
            (pp for pp in profiles.values() if pp.get("emulator") == emu_name), {}
        )
    return _map_emulator_to_slug(profile, platform_systems, norm_map, sys_to_slug)


def hidden_extras(
    config: dict,
    emulators_dir: str,
    db: dict,
    profiles: dict,
    shipped: dict[str, str],
    target_cores: set[str] | None = None,
    region_drops: set[str] | None = None,
    placed: set[str] | None = None,
) -> dict[tuple[str, str, str], str]:
    """Report entries the builder holds and still leaves out, with the reason.

    A destination the region pass withdraws never takes its path in the
    pack, so it blocks nothing here either.

    The builder walks the core extras after the platform's own files and
    skips a destination that a case-insensitive filesystem folds onto one
    already packed, or that a packed file blocks as a directory. RetroPie's
    openmsx fmpac.rom gave way to the platform's FMPAC.ROM, other bytes,
    while the report counted it in the pack. A twin carrying the same bytes
    serves the core on the filesystem that folds the names, so it is not a
    file left out. ``shipped`` maps the full destination of each platform
    file the pack carries to its local copy. ``placed``, when given,
    receives the full destination of each held extra the pack carries.
    """
    from packpaths import _register_path, placement_block

    sha1_by_path = {
        entry.get("path"): sha1 for sha1, entry in db.get("files", {}).items()
    }
    base_dest = config.get("base_destination", "")
    case_insensitive = bool(config.get("case_insensitive_fs", False))
    seen: set[str] = set()
    parents: set[str] = set()
    content: dict[str, str | None] = {}
    for dest, local in shipped.items():
        _register_path(dest, seen, parents)
        content[dest.lower()] = sha1_by_path.get(local)
    lower = set(content) if case_insensitive else set()
    prefix = _detect_extras_prefix(config, base_dest)
    hidden: dict[tuple[str, str, str], str] = {}
    for fe in _collect_emulator_extras(
        config, emulators_dir, db, set(shipped), base_dest, profiles,
        target_cores=target_cores,
    ):
        dest = sanitize_pack_path(fe.get("destination", fe.get("name", "")))
        if not dest or (region_drops and dest in region_drops):
            continue
        full = dest if not prefix or dest.startswith(f"{prefix}/") else f"{prefix}/{dest}"
        reason = placement_block(full, seen, lower, parents, case_insensitive)
        if reason == "taken":
            # Another core's claim on the same path, settled by the claims
            # the collector records: not a file left out.
            continue
        local, _status = resolve_local_file(
            fe, db, dest_hint=fe.get("destination", "")
        )
        held = sha1_by_path.get(local) if local else None
        if reason == "case_twin" and held and content.get(full.lower()) == held:
            continue
        if reason is not None:
            key = (
                fe.get("source_emulator", ""), fe.get("source_name", ""),
                fe.get("source_path", ""),
            )
            hidden.setdefault(key, reason)
            continue
        _register_path(full, seen, parents)
        content[full.lower()] = held
        if case_insensitive:
            lower.add(full.lower())
        if placed is not None and local:
            placed.add(full)
    return hidden


def unplaceable_extras(config: dict, undeclared: list[dict], profiles: dict) -> set:
    """Keys of the report entries a slug-based platform cannot place.

    RomM files every BIOS under the slug of its system and knows no slug
    for DOS or for the arcade sets MAME 2003 reads; the builder skipped
    those entries and the report counted them in the pack.
    """
    from common import _norm_system_id

    is_slug_based, sys_to_slug = _detect_slug_structure(config)
    if not is_slug_based:
        return set()
    platform_systems = set(config.get("systems", {}).keys())
    norm_map = {_norm_system_id(sid): sid for sid in platform_systems}
    return {
        (u.get("emulator", ""), u.get("name", ""), u.get("path") or "")
        for u in undeclared
        if u.get("in_repo")
        and not _slug_for(u, profiles, platform_systems, norm_map, sys_to_slug)
    }


def _agnostic_scan_extras(
    profiles: dict,
    relevant: set,
    db: dict,
    by_name: dict,
    seen_dests: set,
    extras_prefix: str,
    standalone_set: set[str] | None = None,
    taken: dict[str, dict] | None = None,
) -> list[dict]:
    """Every interchangeable candidate a filename-agnostic core accepts.

    Such a core reads whatever sits in its BIOS directory, so the pack
    carries every dump of the right shape rather than one chosen name.
    The directory is the delicate part: anchoring it on a single
    filename walked into another emulator's tree, so the profile's own
    files have to agree before the scan follows an ambiguous name.
    """
    extras: list[dict] = []
    files_db = db.get("files", {})
    # Third pass: the agnostic scan. For filename-agnostic cores, include all
    # DB files matching the system path prefix and size criteria.
    for emu_name, profile in sorted(profiles.items()):
        if profile.get("type") in ("launcher", "alias"):
            continue
        if emu_name not in relevant:
            continue
        is_profile_agnostic = profile.get("bios_mode") == "agnostic"
        if not is_profile_agnostic:
            if not any(f.get("agnostic") for f in profile.get("files", [])):
                continue

        # Where each of this profile's files resolves.  A scan anchored on one
        # filename walks into whatever tree that name happens to hit: rom1.bin
        # is a PS2 ROM and a Roland SC-55 ROM, GameIndex.yaml belongs to four
        # PS2 profiles and to an Android package.  One match is not evidence
        # of a directory; two files of the same profile agreeing is.
        resolved_dirs: dict[int, str] = {}
        named_only: set[int] = set()
        agnostic_votes: dict[str, int] = {}
        for candidate in profile.get("files", []):
            if not isinstance(candidate, dict):
                continue
            local, status = resolve_local_file(
                {**candidate, "source_profile": emu_name},
                db,
                dest_hint=candidate.get("path", ""),
            )
            if not local or "/" not in local:
                continue
            directory = local.rsplit("/", 1)[0]
            if directory.endswith("/.variants"):
                directory = directory[: -len("/.variants")]
            resolved_dirs[id(candidate)] = directory + "/"
            if not (resolution_is_hash_exact(status) or status == "path_exact"):
                named_only.add(id(candidate))
            agnostic_votes[directory + "/"] = (
                agnostic_votes.get(directory + "/", 0) + 1
            )

        standalone = runs_standalone(emu_name, profile, standalone_set or set())
        for f in profile.get("files", []):
            if not is_profile_agnostic and not f.get("agnostic"):
                continue
            fname = f.get("name", "")
            if not fname:
                continue
            # A file of the build the platform does not run seeds nothing:
            # armsx2's proverb.zip is fetched by the Android app into its own
            # arcade/ folder, and the libretro packs carried it at system/.
            mode = f.get("mode")
            if (mode == "standalone" and not standalone) or (
                mode == "libretro" and standalone
            ):
                continue
            # An agnostic BIOS mode says the BIOS filename is free, not that
            # every file the emulator loads is.  A free filename still has a
            # known shape, and that shape is what identifies a candidate: an
            # entry declaring no size takes the whole directory, which is how
            # the flag icons of an Android package walked into a PS2 pack.
            if f.get("category", "bios") != "bios":
                continue
            if not (f.get("size") or f.get("min_size") or f.get("max_size")):
                continue
            # The free name is the BIOS directory's: a shader, a sound or a
            # font the emulator reads from its resources tree has a fixed
            # name and must not seed a scan (yaps2 flattened 189 of them to
            # the root of the RetroArch pack).
            bios_dir = profile.get("bios_directory")
            fpath = f.get("path") or ""
            if bios_dir and "/" in fpath and not fpath.startswith(bios_dir):
                continue

            path_prefix = resolved_dirs.get(id(f), "")
            if not path_prefix:
                continue
            # A name matching one file identifies it. A name matching several
            # identifies nothing on its own, so the profile's other files have
            # to agree on the directory before the scan walks it.
            ambiguous = id(f) in named_only and len(by_name.get(fname, [])) > 1
            if ambiguous and agnostic_votes.get(path_prefix, 0) < 2:
                continue

            # Scan DB for all files under this prefix matching size
            for sha1, entry in files_db.items():
                path = entry.get("path", "")
                if not path.startswith(path_prefix):
                    continue
                if not size_fits(f, entry.get("size", 0)):
                    continue
                scan_name = entry.get("name", "")
                if not scan_name:
                    continue
                # Into the directory the seed entry names, not the root:
                # yaps2 reads its BIOS from pcsx2/bios/.
                seed = f.get("path") or ""
                if standalone:
                    seed = f.get("standalone_path") or seed
                folder = seed.rsplit("/", 1)[0] if "/" in seed else ""
                dest = f"{folder}/{scan_name}" if folder else scan_name
                full_dest = f"{extras_prefix}/{dest}" if extras_prefix else dest
                if full_dest in seen_dests and (taken is None or full_dest not in taken):
                    continue
                extra = {
                    "name": scan_name,
                    "destination": dest,
                    # The scan already holds the entry it selected, so it
                    # names it by content.  Emitting the filename alone
                    # sent the packing step back to a by-name lookup, and
                    # three PS2 resources came back from another
                    # emulator's copy of the same filename.
                    "sha1": sha1,
                    "required": False,
                    "hle_fallback": False,
                    "source_emulator": profile.get("emulator", emu_name),
                    "source_profile": emu_name,
                    "source_system": f.get("system"),
                    "source_systems": list(profile.get("systems", [])),
                    "region": f.get("region"),
                    "variant_group": f.get("variant_group"),
                    "agnostic_scan": True,
                }
                extra["claim_order"] = (4, 0, emu_name)
                if not _take(extra, full_dest, seen_dests, taken):
                    continue
                extras.append(extra)
                extras.extend(_companion_extras(
                    _companions(path, scan_name, _companion_extensions(profile, fname), files_db),
                    folder, extras_prefix, seen_dests, profile, emu_name, f, taken,
                ))
    return extras


def _companion_extras(
    companions: list[tuple[str, dict]],
    folder: str,
    extras_prefix: str,
    seen_dests: set[str],
    profile: dict,
    emu_name: str,
    seed: dict,
    taken: dict[str, dict] | None = None,
) -> list[dict]:
    """Extras for the companions of one scanned image, placed beside it."""
    extras: list[dict] = []
    for companion_sha1, companion in companions:
        companion_dest = f"{folder}/{companion['name']}" if folder else companion["name"]
        companion_full = f"{extras_prefix}/{companion_dest}" if extras_prefix else companion_dest
        if companion_full in seen_dests and (taken is None or companion_full not in taken):
            continue
        extra = {
            "name": companion["name"],
            "destination": companion_dest,
            "sha1": companion_sha1,
            "required": False,
            "hle_fallback": False,
            "source_emulator": profile.get("emulator", emu_name),
            "source_profile": emu_name,
            "source_system": seed.get("system"),
            "source_systems": list(profile.get("systems", [])),
            "region": seed.get("region"),
            "variant_group": seed.get("variant_group"),
            "agnostic_scan": True,
        }
        extra["claim_order"] = (4, 0, emu_name)
        if _take(extra, companion_full, seen_dests, taken):
            extras.append(extra)
    return extras


def _companion_extensions(profile: dict, seed_name: str) -> set[str]:
    """Extensions the profile declares beside a free-named BIOS entry.

    PCSX2 reads `<bios>.rom1` or the BIOS path with its extension replaced
    (`<stem>.rom1`), and likewise nvm and mec (BiosTools.cpp:214-229,
    CDVD.cpp:160-195). A profile states that by naming the companions after
    its BIOS entry: ps2-0230a-20080220.rom1 beside ps2-0230a-20080220.bin.
    """
    stem = seed_name.rsplit(".", 1)[0].lower()
    exts: set[str] = set()
    for entry in profile.get("files", []):
        other = str(entry.get("name", "")).lower()
        if other != seed_name.lower() and other.startswith(stem + "."):
            exts.add(other[len(stem) + 1:])
    return exts


def _companions(
    image_path: str, image_name: str, extensions: set[str], files_db: dict
) -> list[tuple[str, dict]]:
    """Files beside a scanned image that the core opens as its companions."""
    if not extensions:
        return []
    directory = image_path.rsplit("/", 1)[0] + "/"
    stem = image_name.rsplit(".", 1)[0].lower()
    wanted = {f"{image_name.lower()}.{ext}" for ext in extensions}
    wanted |= {f"{stem}.{ext}" for ext in extensions}
    return sorted(
        (sha1, entry)
        for sha1, entry in _files_by_directory(files_db).get(directory, ())
        if str(entry.get("name", "")).lower() in wanted
    )


# The database a directory index was built from, with the index: one walk
# per database instead of one per scanned image (1 023 walks of 19 041
# entries in verify --all).
_DIRECTORY_INDEX: tuple[dict, dict[str, list[tuple[str, dict]]]] | None = None


def _files_by_directory(files_db: dict) -> dict[str, list[tuple[str, dict]]]:
    global _DIRECTORY_INDEX
    if _DIRECTORY_INDEX is None or _DIRECTORY_INDEX[0] is not files_db:
        index: dict[str, list[tuple[str, dict]]] = {}
        for sha1, entry in files_db.items():
            directory = entry.get("path", "").rsplit("/", 1)[0] + "/"
            index.setdefault(directory, []).append((sha1, entry))
        _DIRECTORY_INDEX = (files_db, index)
    return _DIRECTORY_INDEX[1]


def _archive_prefix_extras(
    profiles: dict,
    relevant: set,
    covered_names: set,
    seen_dests: set,
    extras_prefix: str,
    by_name: dict,
    taken: dict[str, dict] | None = None,
) -> list[dict]:
    """A second copy of an archive under the subdirectory a core reads.

    Some cores look for their romset under a directory of their own
    (system/fbneo/neogeo.zip) while the platform declares it at the
    root. Both paths are carried so the core's own check finds it.
    """
    extras: list[dict] = []
    # Archive prefix pass: cores that store BIOS archives in a subdirectory
    # (e.g. system/fbneo/neogeo.zip).  When the archive is already covered at
    # the root, add a copy at the prefixed path so the core's .info firmware
    # check finds it.
    for emu_name, profile in sorted(profiles.items()):
        if profile.get("type") in ("launcher", "alias"):
            continue
        if emu_name not in relevant:
            continue
        prefix = profile.get("archive_prefix", "")
        if not prefix:
            continue
        profile_archives: dict[str, dict] = {}
        for f in profile.get("files", []):
            archive = f.get("archive", "")
            if archive:
                profile_archives.setdefault(archive, f)
        for archive_name, archive_entry in sorted(profile_archives.items()):
            if archive_name not in covered_names:
                continue
            dest = f"{prefix}/{archive_name}"
            full_dest = f"{extras_prefix}/{dest}" if extras_prefix else dest
            if full_dest in seen_dests and (taken is None or full_dest not in taken):
                continue
            if not by_name.get(archive_name):
                continue
            extra = {
                "name": archive_name,
                "destination": dest,
                "required": True,
                "hle_fallback": False,
                "source_emulator": profile.get("emulator", emu_name),
                "source_profile": emu_name,
                "source_system": archive_entry.get("system"),
                "source_systems": list(profile.get("systems", [])),
                "region": archive_entry.get("region"),
                "variant_group": archive_entry.get("variant_group"),
            }
            members = archive_members(archive_name, profile.get("files", []))
            if members:
                extra["members"] = members
            extra["claim_order"] = (3, 0, emu_name)
            if _take(extra, full_dest, seen_dests, taken):
                extras.append(extra)

    return extras


class ExtraPlacement:
    """Where a pack puts a core's file, decided once for every reader.

    The builder, and slots.py judging what a destination holds, both read
    this: placed apart, slot arbitration compared RetroDECK's bios/ claims
    with core claims at the root and never saw them meet.
    """

    def __init__(self, config: dict, base_dest: str, profiles: dict) -> None:
        from common import _norm_system_id

        self.prefix = _detect_extras_prefix(config, base_dest)
        self.slug_based, self.sys_to_slug = _detect_slug_structure(config)
        self.platform_systems = set(config.get("systems", {}).keys())
        self.norm_map = (
            {_norm_system_id(sid): sid for sid in self.platform_systems}
            if self.slug_based
            else {}
        )
        self.profiles = profiles

    def place(self, entry: dict, dest: str) -> tuple[str, str] | None:
        """(pack-relative destination, full destination), or None.

        ``entry`` names its profile and may name its own system. A
        slug-based platform cannot place a file of a system it has no
        folder for.
        """
        if self.slug_based:
            slug = _slug_for(
                entry, self.profiles, self.platform_systems, self.norm_map,
                self.sys_to_slug,
            )
            if not slug:
                return None
            dest = f"{slug}/{dest}"
        return dest, (f"{self.prefix}/{dest}" if self.prefix else dest)


def _collect_emulator_extras(
    config: dict,
    emulators_dir: str,
    db: dict,
    seen: set,
    base_dest: str,
    emu_profiles: dict | None = None,
    target_cores: set[str] | None = None,
    include_all: bool = False,
    claimants: dict[tuple[str, str, str], str] | None = None,
    unplaceable: list[dict] | None = None,
    baseline_claims: dict[str, dict] | None = None,
    absent: list[dict] | None = None,
) -> list[dict]:
    """Collect core requirement files from emulator profiles not in the platform pack.

    ``absent`` receives, placed like the others, the entries the collection
    does not hold: the region pass groups them as it groups the platform's
    own missing files, so the report withdraws the ones a request excludes.

    ``baseline_claims`` receives, for each destination in ``seen``, the core
    claims made on it: a pack built for a target that drops the platform's
    system frees the destination, and the first core claiming it fills it.

    ``claimants`` receives the destination of every report entry that
    asked for one, the ones a sibling already took included: the region
    report withdraws by that key, and an entry without one stayed listed
    after the builder had withdrawn its destination. ``unplaceable``
    receives the entries a slug-based platform cannot place, so the
    manifest and the report can say so instead of counting them packed.

    Uses the same system-overlap matching as verify.py cross-reference:
    - Matches emulators by shared system IDs with the platform
    - Filters mode: standalone, type: launcher, type: alias
    - Respects data_directories coverage
    - Only returns files that exist in the repo (packable)

    When the same file is needed at multiple destinations by different cores
    (e.g. cdimono1.zip at root for cdi2015 and at same_cdi/bios/ for same_cdi),
    all destinations are included so every core finds its files.

    Works for ANY platform (RetroArch, Batocera, Recalbox, etc.)
    """
    from common import resolve_platform_cores
    from verify import find_undeclared_files

    profiles = (
        emu_profiles
        if emu_profiles is not None
        else load_emulator_profiles(emulators_dir)
    )

    # Detect destination conventions for core extras
    placement = ExtraPlacement(config, base_dest, profiles)
    extras_prefix = placement.prefix
    is_slug_based = placement.slug_based

    # Use strict YAML names (no DB alias enrichment) so that files known
    # under an alias still get packed at the emulator's expected path.
    yaml_names = platform_declarations(config, db, enrich=False)

    undeclared = find_undeclared_files(
        config, emulators_dir, db, emu_profiles, target_cores=target_cores,
        include_all=include_all, declared_names=yaml_names,
    )
    # A destination two cores claim goes to the one that names its content:
    # hypseus-singe asks for gamecontrollerdb.txt by name alone and took the
    # BIOS root before Solarus, which pins its own by sha1. Profile order
    # still decides between claims of the same kind.
    undeclared = sorted(undeclared, key=lambda u: not _names_content(u))
    extras = []
    seen_dests: set[str] = set(seen)
    # Destination -> the extra that took it, to record later claimants on.
    taken: dict[str, dict] = {}
    if baseline_claims is not None:
        for destination in seen:
            taken[destination] = baseline_claims.setdefault(destination, {})
    for u in undeclared:
        if not u["in_repo"] and absent is None:
            continue
        # For archive entries, use the archive name for resolution
        archive = u.get("archive")
        name = archive if archive else u["name"]
        raw_dest = archive if archive else (u.get("path") or u["name"])
        # Directory path: append filename (e.g. "cafeLibs/" + "snd_user.rpl")
        dest = f"{raw_dest}{u['name']}" if raw_dest.endswith("/") else raw_dest

        # Slug-based platforms: prefix dest with system slug
        placed = placement.place(u, dest)
        if not u["in_repo"]:
            if placed is not None:
                absent.append(_report_extra(u, name, placed[0]))
            continue
        if placed is None:
            if unplaceable is not None:
                unplaceable.append(u)
            continue
        dest, full_dest = placed
        if claimants is not None:
            claimants[(u.get("emulator", ""), u.get("name", ""), u.get("path") or "")] = (
                sanitize_pack_path(dest)
            )
        extra = _report_extra(u, name, dest)
        claims = [_report_extra(c, name, dest) for c in u.get("other_claims", [])]
        # Where the builder meets each claim: the report lists the profiles in
        # order, archives after the loose files.
        for item in (extra, *claims):
            item["claim_order"] = (1, 1 if u.get("archive") else 0, item["source_profile"])
        if not _take(extra, full_dest, seen_dests, taken):
            for claim in claims:
                _another_claim(taken.get(full_dest), claim)
            continue
        for claim in claims:
            _another_claim(extra, claim)
        extras.append(extra)

    # Second pass: find alternative destinations for files already in the pack.
    # A file declared by the platform or emitted above may also be needed at a
    # different path by another core (e.g. neocd/ vs root, same_cdi/bios/ vs root).
    # Only adds a copy when the file is ALREADY covered at a different path -
    # never introduces a file that wasn't selected by the first pass.
    #
    # Skip for slug-based platforms (RomM): alternative paths don't map to
    # the required {platform_slug}/{file} structure.
    if is_slug_based:
        return extras

    relevant = resolve_platform_cores(config, profiles, target_cores=target_cores)
    standalone_set = {str(c) for c in config.get("standalone_cores", [])}
    by_name = db.get("indexes", {}).get("by_name", {})
    by_path_suffix = db.get("indexes", {}).get("by_path_suffix", {})

    # Build set of filenames already covered (platform baseline + first pass extras)
    # Enriched with canonical names from DB via MD5 (handles platform renaming)
    covered_names = expand_platform_declared_names(config, db)
    for e in extras:
        covered_names.add(e["name"])

    for emu_name, profile in sorted(profiles.items()):
        if profile.get("type") in ("launcher", "alias"):
            continue
        if emu_name not in relevant:
            continue
        is_standalone = runs_standalone(emu_name, profile, standalone_set)
        for f in profile.get("files", []):
            fname = f.get("name", "")
            if not fname:
                continue
            # Only duplicate files already covered at another destination.
            # An unsourceable entry is a file no collection holds: the name
            # covered elsewhere belongs to another program's file.
            if fname not in covered_names or f.get("unsourceable"):
                continue
            # A member of an archive is read inside it, never as a loose
            # file; the archive itself is placed by the passes above.
            if f.get("archive"):
                continue
            # No path: the core reads it from nowhere a pack reaches
            # (Dolphin's codehandler.bin ships in its Sys tree).
            if "path" in f and f["path"] is None:
                continue
            file_mode = f.get("mode")
            if file_mode == "standalone" and not is_standalone:
                continue
            if file_mode == "libretro" and is_standalone:
                continue
            if not read_from_system_dir(f, is_standalone):
                continue
            if is_standalone:
                raw = f.get("standalone_path") or f.get("path") or fname
            else:
                raw = f.get("path") or fname
            dest = f"{raw}{fname}" if raw.endswith("/") else raw
            # A bare name is a destination too: the core reads it at the root
            # of the directory core files go to, which the platform may not
            # declare (Recalbox has its System Cards under pcenginecd/ only,
            # geargrafx reads them from the root).
            full_dest = f"{extras_prefix}/{dest}" if extras_prefix else dest
            if full_dest in seen_dests and full_dest not in taken:
                continue
            # Check file exists in repo or data dirs
            if not (
                by_name.get(fname)
                or by_name.get(dest.rsplit("/", 1)[-1])
                or by_path_suffix.get(dest)
            ):
                continue
            extra = {
                "name": fname,
                "destination": dest,
                "required": f.get("required", False),
                "hle_fallback": f.get("hle_fallback", False),
                "source_emulator": profile.get("emulator", emu_name),
                "source_profile": emu_name,
                "source_system": f.get("system"),
                "source_systems": list(profile.get("systems", [])),
                "region": f.get("region"),
                "variant_group": f.get("variant_group"),
            }
            # The same identity the first pass carries: without it the copy
            # resolves on its name alone and a same-named file of another
            # game fills the slot the profile marks as not held.
            for identity in PROFILE_IDENTITY_FIELDS:
                if f.get(identity) not in (None, "", []):
                    extra[identity] = f[identity]
            if f.get("contents"):
                extra["contents"] = f["contents"]
            extra["claim_order"] = (2, 0, emu_name)
            if _take(extra, full_dest, seen_dests, taken):
                extras.append(extra)

    extras.extend(
        _archive_prefix_extras(
            profiles, relevant, covered_names, seen_dests, extras_prefix,
            by_name, taken,
        )
    )

    extras.extend(
        _agnostic_scan_extras(
            profiles, relevant, db, by_name, seen_dests, extras_prefix,
            standalone_set, taken,
        )
    )

    return extras

def _names_content(entry: dict) -> bool:
    """Whether a report entry declares the bytes it wants, not only a name."""
    return any(entry.get(field) for field in ("sha1", "md5", "sha256", "crc32"))


def _report_extra(u: dict, name: str, dest: str) -> dict:
    """The extra a cross-reference entry asks for at its destination."""
    extra = {
        "name": name,
        "destination": dest,
        "required": u.get("required", False),
        "hle_fallback": u.get("hle_fallback", False),
        "source_emulator": u.get("emulator", ""),
        "source_profile": u.get("profile", ""),
        # Identity of the report entry this extra came from.  The name
        # alone does not identify it: Dolphin declares three IPL.bin that
        # differ only by path, and collapsing them onto one key withdraws
        # the wrong regional variant from the report.
        "source_name": u.get("name", ""),
        "source_path": u.get("path") or "",
        "source_system": u.get("system"),
        "source_systems": u.get("systems", []),
        "region": u.get("region"),
        "variant_group": u.get("variant_group"),
    }
    if u.get("members"):
        extra["members"] = u["members"]
    # The archive layout the core opens ranks same-named copies by what
    # they hold; without it the first copy indexed won.
    if u.get("contents"):
        extra["contents"] = u["contents"]
    # Keep every reproducible identity constraint carried by the source
    # profile.  Some profiles expose CRC32/SHA-256 only through their
    # validation block; dropping those fields here would let a same-named
    # but different payload enter a generated pack.
    expected = u.get("expected") or {}
    for identity in PROFILE_IDENTITY_FIELDS:
        declared = u.get(identity)
        if declared in (None, "", []):
            declared = expected.get(identity)
        if declared not in (None, "", []):
            extra[identity] = declared
    return extra


def _take(
    extra: dict, full_dest: str, seen_dests: set[str], taken: dict[str, dict] | None
) -> bool:
    """Whether an extra takes its destination; a taken one records the claim."""
    if full_dest in seen_dests:
        if taken is not None:
            _another_claim(taken.get(full_dest), extra)
        return False
    seen_dests.add(full_dest)
    if taken is not None:
        taken[full_dest] = extra
    return True


def _another_claim(extra: dict | None, claim: dict) -> None:
    """Record a core that reads a destination another core's extra took.

    The pack ships one file per destination, so the first claimant's extra
    stands; the others are kept on it so the manifest can name every core
    the file serves. A pack built for a target without the first core packs
    the next one's copy, and an installer narrowing by core must keep it.
    """
    if extra is None or claim.get("source_profile") == extra.get("source_profile"):
        return
    claims = extra.setdefault("other_claims", [])
    claims.append(claim)
    claims.sort(key=lambda c: c["claim_order"])


def _extra_system_ids(extra: dict) -> list[str]:
    """Return the narrowest system ownership preserved on a core extra."""
    explicit = extra.get("source_system")
    if explicit:
        return [str(explicit)]
    return [str(value) for value in extra.get("source_systems", []) if value]

def _kept(entries: list[dict], required_only: bool) -> list[dict]:
    """The entries a pack keeps: optional ones go under --required-only."""
    if not required_only:
        return entries
    return [e for e in entries if e.get("required") is not False]


def platform_region_groups(
    config: dict,
    systems: dict,
    emulators_dir: str,
    db: dict | None,
    base_dest: str,
    emu_profiles: dict | None,
    *,
    target_cores: set[str] | None = None,
    include_extras: bool = True,
    include_all: bool = False,
    required_only: bool = False,
    extras_out: list[dict] | None = None,
    held_out: set[str] | None = None,
) -> tuple[dict[str, list[tuple[str, str]]], dict[tuple[str, str, str], str]]:
    """Group a platform's pack candidates the way region filtering reads them.

    ``held_out`` receives the destinations whose file the collection holds:
    a missing file is grouped so the report can withdraw it, but it never
    takes the slot of a held one (resolve_region_drops).

    Returns the groups and, for every core extra, the destination it was
    grouped under, keyed by (emulator, name).  verify.py needs that mapping to
    withdraw from its report exactly what the builder withdraws from the pack:
    grouping the declared files here and the core extras there would let the
    two answer differently on the same request.  `extras_out` receives the
    core extras that were grouped, for the install manifest.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for sys_id, system in systems.items():
        members = groups.setdefault(sys_id, [])
        # Decided over what the pack keeps: an optional winner removed
        # afterwards left its slot empty.
        for file_entry in _kept(system.get("files", []), required_only):
            dest = sanitize_pack_path(
                file_entry.get("destination", file_entry.get("name", ""))
            )
            if dest:
                members.append((dest, file_entry.get("name", "")))
                if held_out is not None and db is not None and resolve_local_file(
                    file_entry, db, dest_hint=dest
                )[0]:
                    held_out.add(dest)

    extra_dests: dict[tuple[str, str, str], str] = {}
    if not include_extras or db is None:
        return groups, extra_dests

    absent: list[dict] = []
    extras = _collect_emulator_extras(
        config,
        emulators_dir,
        db,
        set(),
        base_dest,
        emu_profiles,
        target_cores=target_cores,
        include_all=include_all,
        claimants=extra_dests,
        absent=absent,
    )
    held = {id(extra) for extra in extras}
    spellings = system_spellings(systems)
    for extra in _kept(extras + absent, required_only):
        dest = sanitize_pack_path(extra.get("destination", extra.get("name", "")))
        if not dest:
            continue
        name = extra.get("name", "")
        extra_dests[
            (
                extra.get("source_emulator", ""),
                extra.get("source_name", ""),
                extra.get("source_path", ""),
            )
        ] = dest
        # A file a filename-agnostic scan accepts by shape competes only if
        # its seed names a region: PCSX2 picks no PS2 BIOS by territory, and
        # looked up by name its scph50006 took DuckStation's [japan], which
        # describes DuckStation's own PS1-on-PS2 choice.
        competes = not extra.get("agnostic_scan") or extra.get("region")
        for group_id in extra_region_groups(extra, spellings) if competes else ():
            groups.setdefault(group_id, []).append((dest, name))
        if extras_out is not None and id(extra) in held:
            extras_out.append(extra)
        if held_out is not None and id(extra) in held:
            held_out.add(dest)
    return groups, extra_dests


def system_spellings(systems: Iterable[str]) -> dict[str, str]:
    """A platform's own spelling of each system, keyed the way ids compare.

    RetroDECK files the Master System as sega-master-system, the profiles as
    sega-mastersystem. Grouped under both, the declared bios/bios_J.sms and
    the core's bios_J.sms were reduced apart and a --region us pack kept the
    Japanese BIOS.
    """
    spellings: dict[str, str] = {}
    for sys_id in sorted(systems):
        spellings.setdefault(_norm_system_id(sys_id), sys_id)
    return spellings


def extra_region_groups(
    extra: dict, spellings: dict[str, str] | None = None
) -> list[str]:
    """The region groups a core extra competes in.

    A group named after a system takes the platform's spelling of it, so the
    extra competes beside the files the platform declares for that system.
    """
    def system_group(sys_id: str) -> str:
        return (spellings or {}).get(_norm_system_id(sys_id), sys_id)

    emulator = extra.get("source_emulator", "")
    variant = extra.get("variant_group")
    if variant:
        # A variant group is one slot of one core, whatever system each
        # member is filed under: PicoDrive files its US Mega CD BIOS under
        # sega-segacd and the EU/JP ones under sega-megacd, and find_bios
        # picks one of the twelve. Keyed as emulator mode keys it.
        return [f"{emulator}:variant:{variant}"]
    explicit = extra.get("source_system")
    if explicit:
        return [system_group(str(explicit))]
    systems = [str(s) for s in extra.get("source_systems", []) if s]
    if len(systems) == 1:
        return [system_group(systems[0])]
    # No system names the machine this file is chosen for. Filed under every
    # system of a 30-system profile, CLK's American MSX ROM made the
    # Enterprise group drop its German ROM. Alone, it drops nothing and is
    # never dropped.
    destination = extra.get("destination", extra.get("name", ""))
    return [f"{emulator}:file:{destination}"]

def emulator_region_drops(
    selected: list[tuple[str, dict]],
    standalone: bool,
    regions: list[str],
    required_only: bool = False,
) -> set[str]:
    """Destinations an emulator-mode `--region` withdraws.

    One group per system (or explicit variant_group) inside each profile, so
    a multi-system core such as O2EM keeps a fallback BIOS for every system
    instead of letting one matching system empty another. The emulator pack
    and `verify --emulator` both read this, keyed by pack destination.
    """
    import region as region_mod
    from packpaths import _resolve_destination
    from validation import filter_files_by_mode

    region_index = region_mod.build_region_index(dict(selected))
    region_groups: dict[str, list[tuple[str, str]]] = {}
    for emu_name, profile in sorted(selected):
        # Decided over what the pack keeps, as on the platform side: an
        # optional winner removed afterwards left its slot empty.
        entries = filter_files_by_mode(profile.get("files", []), standalone)
        for fe in _kept(entries, required_only):
            dest = _resolve_destination(fe, standalone)
            if dest:
                group_id = _emulator_region_group(emu_name, profile, fe)
                region_groups.setdefault(group_id, []).append(
                    (dest, fe.get("name", ""))
                )
    return region_mod.resolve_region_drops(region_groups, region_index, regions)


def _emulator_region_group(emu_name: str, profile: dict, file_entry: dict) -> str:
    """Stable group ID for regional alternatives within an emulator profile."""
    variant = file_entry.get("variant_group")
    if variant:
        return f"{emu_name}:variant:{variant}"
    system = file_entry.get("system")
    profile_systems = list(profile.get("systems", []))
    if not system and len(profile_systems) == 1:
        system = profile_systems[0]
    return f"{emu_name}:system:{system or '_profile'}"
