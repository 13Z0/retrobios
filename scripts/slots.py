"""Who decides what goes at a destination.

A destination is a slot. Two layers can claim it: the platform YAML, scraped
from what the frontend declares and therefore able to carry an upstream error,
and the emulator profile, read from the emulator's own source. Until this
module existed nothing compared the two: the builder resolves the platform
entry first and drops the profile entry on a bare filename match, so a profile
that names the right file for a slot could never win and never even be heard.

The index is keyed by destination AND by name, the name carrying the union of
what every same-named entry claims. Dolphin declares three IPL.bin separated
only by their path, so a name-only key merges them; a path-only key answers
nothing when a candidate is known by name alone. Ambiguity resolves to the
union and is never discarded, the same rule region.py already applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nativemode
from common import (
    archive_entry,
    build_zip_contents_index,
    check_inside_zip,
    holds_required_members,
    load_data_dir_registry,
    parse_md5_list,
    resolution_is_hash_exact,
    resolve_local_file,
    resolve_platform_cores,
    runs_standalone,
    variant_base_name,
)
from packresolve import preferred_entries
from validation import outside_gap_scope

if TYPE_CHECKING:
    from packextras import ExtraPlacement

# A profile entry can prove a slot without declaring a hash: Dolphin names no
# checksum for the GameCube boot ROM because its source names none, and the
# proof is then the path the repository stores the file at.
PROVEN_STATUSES = frozenset({"path_exact", "members_held"})

# A core that reads ROMs inside an archive proves the archive by its members:
# FBNeo's neocdz.zip is any copy holding neocd.bin and 000-lo.lo by crc.
MEMBERS_HELD = "members_held"


@dataclass
class Claim:
    """One layer's answer to what belongs at a destination."""

    origin: str
    destination: str
    name: str
    emulator: str = ""
    entry: dict = field(default_factory=dict)
    local_path: str | None = None
    status: str = ""

    @property
    def is_proven(self) -> bool:
        """Whether the claim rests on evidence rather than on a bare name."""
        return resolution_is_hash_exact(self.status) or self.status in PROVEN_STATUSES


@dataclass
class Conflict:
    """Two proven claims on one destination that resolve to different files."""

    destination: str
    platform_claim: Claim
    profile_claims: list[Claim]

    @property
    def emulators(self) -> list[str]:
        return sorted({c.emulator for c in self.profile_claims if c.emulator})


def _normalize(destination: str, fold: bool = True) -> str:
    """Comparable form of a destination, since layers write it differently.

    Case is folded only where the platform's filesystem folds it: on Linux
    RetroDECK ships bios/DISK.ROM and bios/disk.rom side by side.
    """
    cleaned = destination.strip().strip("/").replace("\\", "/")
    return cleaned.casefold() if fold else cleaned


def build_claim_index(claims: list[Claim]) -> dict[str, list[Claim]]:
    """Index claims by destination and by name, the name carrying the union.

    A lookup by full destination answers for one slot. A lookup by bare name
    answers with every slot that shares it, which is what a candidate known
    only by its filename needs in order not to be dropped.
    """
    index: dict[str, list[Claim]] = {}
    for claim in claims:
        keys = {_normalize(claim.destination)}
        if claim.name:
            keys.add(_normalize(claim.name))
        base = claim.destination.rsplit("/", 1)[-1]
        if base:
            keys.add(_normalize(base))
        for key in keys - {""}:
            index.setdefault(key, []).append(claim)
    return index


def platform_claims(
    config: dict,
    db: dict,
    base_dest: str = "",
    zip_contents: dict | None = None,
    data_dir_registry: dict | None = None,
) -> list[Claim]:
    """What the platform YAML says belongs at each of its destinations."""
    claims: list[Claim] = []
    for system in (config.get("systems") or {}).values():
        for entry in system.get("files") or []:
            if not isinstance(entry, dict):
                continue
            dest = entry.get("destination") or entry.get("name") or ""
            if not dest:
                continue
            full = f"{base_dest}/{dest}" if base_dest else dest
            local, status = resolve_local_file(
                entry,
                db,
                zip_contents,
                dest_hint=dest,
                data_dir_registry=data_dir_registry,
            )
            claims.append(
                Claim(
                    origin="platform",
                    destination=full,
                    name=entry.get("name", ""),
                    entry=entry,
                    local_path=local,
                    status=status,
                )
            )
    return claims


def profile_claims(
    profiles: dict,
    db: dict,
    base_dest: str = "",
    standalone_cores: set[str] | None = None,
    zip_contents: dict | None = None,
    data_dir_registry: dict | None = None,
    config: dict | None = None,
) -> list[Claim]:
    """What each emulator profile says belongs at each destination it names.

    With the platform's config, a claim sits where the pack puts the core's
    file (ExtraPlacement): under RetroDECK's bios/, under RomM's system
    folder. A file the platform cannot place claims nothing.

    A file can exist in one build of an emulator and not the other, and the
    two builds read from different directories. The mode is decided per
    emulator, not per pack: a platform runs some of its emulators as libretro
    cores and others standalone, naming the latter in ``standalone_cores``.
    An entry the standalone build alone loads does not address a platform
    running that emulator as a core, and where it does, the destination is
    ``standalone_path``. This is the gate verify already applies.
    """
    standalone_cores = standalone_cores or set()
    placement = None
    if config is not None:
        from packextras import ExtraPlacement  # noqa: PLC0415

        placement = ExtraPlacement(config, base_dest, profiles)
    claims: list[Claim] = []
    for emu_name, profile in sorted(profiles.items()):
        if profile.get("type") in ("launcher", "alias"):
            continue
        # The gap report skips a filename-agnostic profile whole: the builder
        # carries its scan candidates, never its named entries.
        if profile.get("bios_mode") == "agnostic":
            continue
        is_standalone = runs_standalone(emu_name, profile, standalone_cores)
        claims.extend(
            _archive_claims(
                emu_name, profile, is_standalone, db, base_dest, placement,
                zip_contents, data_dir_registry,
            )
        )
        for entry in profile.get("files") or []:
            if not isinstance(entry, dict):
                continue
            # The same rule as the gap report and the builder: an entry the
            # pack never carries (read from the save directory, a
            # placeholder, a filename-agnostic scan) arbitrates nothing.
            if outside_gap_scope(entry, is_standalone):
                continue
            # A member of a romset is not a file at the BIOS root: the archive
            # holding it occupies the destination (_archive_claims). Claiming
            # the member put FBNeo's msx.zip contents loose at the root.
            if entry.get("archive"):
                continue
            dest = (
                (entry.get("standalone_path") or entry.get("path"))
                if is_standalone
                else entry.get("path")
            ) or entry.get("name") or ""
            if not dest:
                continue
            if placement is None:
                full = f"{base_dest}/{dest}" if base_dest else dest
            else:
                placed = placement.place(
                    {"profile": emu_name, "system": entry.get("system")}, dest
                )
                if placed is None:
                    continue
                full = placed[1]
            # The owner's own copy is the one the builder ships.
            local, status = resolve_local_file(
                {**entry, "source_profile": emu_name},
                db,
                zip_contents,
                dest_hint=dest,
                data_dir_registry=data_dir_registry,
            )
            claims.append(
                Claim(
                    origin="profile",
                    destination=full,
                    name=entry.get("name", ""),
                    emulator=emu_name,
                    entry=entry,
                    local_path=local,
                    status=status,
                )
            )
    return claims


def _archive_claims(
    emu_name: str,
    profile: dict,
    is_standalone: bool,
    db: dict,
    base_dest: str,
    placement: ExtraPlacement | None,
    zip_contents: dict | None,
    data_dir_registry: dict | None,
) -> list[Claim]:
    """The archives a core reads its ROMs from, where the builder places them.

    The archive sits at the root and, for a core that reads a directory of
    its own (archive_prefix), under it as well. The claim is proven when the
    copy holds every member the core requires.
    """
    files = [
        entry for entry in profile.get("files") or []
        if isinstance(entry, dict) and not outside_gap_scope(entry, is_standalone)
    ]
    prefix = profile.get("archive_prefix", "")
    claims: list[Claim] = []
    for archive in sorted({entry["archive"] for entry in files if entry.get("archive")}):
        entry = archive_entry(archive, files)
        members = entry.get("members")
        if not members:
            continue
        system = next(f.get("system") for f in files if f.get("archive") == archive)
        for dest in (archive, f"{prefix}/{archive}") if prefix else (archive,):
            if placement is None:
                full = f"{base_dest}/{dest}" if base_dest else dest
            else:
                placed = placement.place({"profile": emu_name, "system": system}, dest)
                if placed is None:
                    continue
                full = placed[1]
            local, status = resolve_local_file(
                {**entry, "source_profile": emu_name},
                db,
                zip_contents,
                dest_hint=dest,
                data_dir_registry=data_dir_registry,
            )
            if (
                local
                and not resolution_is_hash_exact(status)
                and holds_required_members(local, members)
            ):
                status = MEMBERS_HELD
            claims.append(
                Claim(
                    origin="profile",
                    destination=full,
                    name=archive,
                    emulator=emu_name,
                    entry=entry,
                    local_path=local,
                    status=status,
                )
            )
    return claims


def _claim_rank(claim: Claim) -> int:
    """How strongly a platform claim speaks for what the pack ships."""
    if resolution_is_hash_exact(claim.status):
        return 2
    return 1 if claim.is_proven else 0


def _slot_claims(
    config: dict,
    profiles: dict,
    db: dict,
    base_dest: str,
    standalone_cores: set[str] | None,
    zip_contents: dict | None,
    data_dir_registry: dict | None,
) -> tuple[dict[str, Claim], dict[str, list[Claim]]]:
    """The platform claim and the proven profile claims at each destination."""
    fold = bool(config.get("case_insensitive_fs", False))
    # The builder's own choice among declarations sharing a destination,
    # read from the same function: the most accepting archive for zipped_file
    # members, then md5_exact. A rule of its own here compared profiles with
    # a file the pack does not ship (Batocera adam_fdc.zip).
    shipped = set(
        preferred_entries(
            config.get("systems") or {}, db, "bios", base_dest, False,
            zip_contents, True,
        ).values()
    )
    by_dest: dict[str, Claim] = {}
    for claim in platform_claims(
        config, db, base_dest, zip_contents, data_dir_registry
    ):
        key = _normalize(claim.destination, fold)
        held = by_dest.get(key)
        if held is not None and id(held.entry) in shipped:
            continue
        # A bare sibling met first must not stand for the destination: it
        # would hide the contradiction with the file actually shipped.
        if (
            held is None
            or id(claim.entry) in shipped
            or _claim_rank(claim) > _claim_rank(held)
        ):
            by_dest[key] = claim

    # Grouped before judging: a profile may declare several revisions that are
    # all acceptable at one destination, and the platform choosing one of them
    # is agreement, not contradiction. Only a destination where no profile
    # claim at all matches what ships is a disagreement.
    # A platform claim resting on a bare name, or on a pin no local file
    # matches, asserts nothing about content. Where the frontend never reads
    # the bytes, a proven profile claim then decides: RetroArch's neocdz.zip
    # pins an md5 nobody holds, and the primary shipped without the 000-lo.lo
    # geolith and FBNeo read inside it.
    path_only = not nativemode.reads_file_contents(
        config.get("verification_mode", "existence")
    )
    by_slot: dict[str, list[Claim]] = {}
    for claim in profile_claims(
        profiles, db, base_dest, standalone_cores, zip_contents, data_dir_registry,
        config=config,
    ):
        key = _normalize(claim.destination, fold)
        platform = by_dest.get(key)
        if platform is None or not claim.is_proven:
            continue
        if not platform.is_proven and not (path_only and platform.local_path):
            continue
        by_slot.setdefault(key, []).append(claim)

    return by_dest, by_slot


def find_conflicts(
    config: dict,
    profiles: dict,
    db: dict,
    base_dest: str = "",
    standalone_cores: set[str] | None = None,
    zip_contents: dict | None = None,
    data_dir_registry: dict | None = None,
) -> list[Conflict]:
    """Destinations where a proven profile claim contradicts what ships.

    Only proven claims are compared. A claim resolved by filename alone
    asserts nothing about content and cannot contradict anything.
    """
    by_dest, by_slot = _slot_claims(
        config, profiles, db, base_dest, standalone_cores, zip_contents,
        data_dir_registry,
    )
    # A declaration that lists several accepted hashes is satisfied by any of
    # them: Recalbox names both scph1001.bin revisions, and mesence names
    # four BS-X dumps among which System.dat pins one. The resolver stops on
    # the first it finds, so the paths differ while both layers agree.
    disputed = {
        key: claims
        for key, claims in by_slot.items()
        if not any(_satisfies(c, by_dest[key], db) for c in claims)
    }

    return [
        Conflict(
            destination=by_dest[key].destination,
            platform_claim=by_dest[key],
            profile_claims=claims,
        )
        for key, claims in sorted(disputed.items())
    ]


# Why a decision went the way it did. Not the mode's own name: the mode is
# spelt in one place only, and nativemode answers whether it reads content.
SERVES_BOTH = "path_only_check"
ADDRESSEE = "addressee"
FRONTEND_CHECKS_CONTENT = "frontend_checks_content"


@dataclass
class Decision:
    """Which claim a pack should honour at a contested destination, and why."""

    conflict: Conflict
    winner: Claim
    reason: str

    @property
    def serves_both(self) -> bool:
        """Whether honouring the winner still satisfies the other layer."""
        return self.reason == SERVES_BOTH


def pack_overrides(
    config: dict,
    profiles: dict,
    db: dict,
    zip_contents: dict | None = None,
    data_dir_registry: dict | None = None,
    target_cores: set[str] | None = None,
    source: str = "full",
) -> dict[str, str]:
    """Full pack destination -> the file a platform pack serves there instead.

    Where a source-verified profile contradicts the scraped baseline on one
    destination, the pack answers to the platform it is built for; in
    existence mode the frontend never reads the bytes, so serving the
    emulator's file satisfies both. The ZIP builder, the install manifest and
    verify all read this: two of them deciding alone gave the one-line
    installer different bytes than the ZIP.
    """
    # A baseline-only pack serves what the platform declares, and a target's
    # pack answers to the cores that hardware builds: DuckStation does not
    # exist on any RetroArch target and still replaced its scph5000.bin.
    if not profiles or source == "platform":
        return {}
    cores = set(resolve_platform_cores(config, profiles, target_cores=target_cores))
    overrides: dict[str, str] = {}
    for destination, choices in slot_choices(
        config, profiles, db, zip_contents, data_dir_registry
    ).items():
        path = next((path for emulator, path in choices if emulator in cores), None)
        if path:
            overrides[destination] = path
    return overrides


def slot_choices(
    config: dict,
    profiles: dict,
    db: dict,
    zip_contents: dict | None = None,
    data_dir_registry: dict | None = None,
) -> dict[str, list[tuple[str, str | None]]]:
    """Destination -> (core, the file it has served there) in decision order.

    A pack built for a set of cores serves the first choice whose core is in
    it, None standing for the platform's own answer; no core in it leaves
    the platform's answer too. The order is arbitrate's: a core agreeing with
    the platform settles the slot whatever else is present, then the
    strongest proof decides. Read for one core set by pack_overrides and
    recorded whole in the install manifest, so a target narrowed from the
    full manifest gets the bytes the target's own pack carries.
    """
    if nativemode.reads_file_contents(config.get("verification_mode", "existence")):
        return {}
    platform_profiles = {
        name: profiles[name] for name in resolve_platform_cores(config, profiles)
    }
    by_dest, by_slot = _slot_claims(
        config,
        platform_profiles,
        db,
        config.get("base_destination", ""),
        {str(c) for c in config.get("standalone_cores", [])},
        zip_contents,
        data_dir_registry,
    )
    choices: dict[str, list[tuple[str, str | None]]] = {}
    for key, claims in sorted(by_slot.items()):
        platform = by_dest[key]
        agree = [_satisfies(c, platform, db) for c in claims]
        agreeing = [c for c, ok in zip(claims, agree) if ok]
        others = sorted(
            (c for c, ok in zip(claims, agree) if not ok),
            key=lambda claim: (-_claim_rank(claim), claims.index(claim)),
        )
        if not others:
            continue
        choices[platform.destination] = [
            *((c.emulator, None) for c in agreeing),
            *((c.emulator, c.local_path) for c in others),
        ]
    return choices


def apply_override(
    override: str | None, local_path: str | None, status: str
) -> tuple[str | None, str]:
    """The file a pack serves at a destination once its slot is settled.

    An entry the platform stores outside the collection, or a release asset
    fetched for its declared hash, keeps its own answer. The builder and the
    install manifest both read this.
    """
    if override and status not in ("external", "release_asset"):
        return override, "slot_arbitrated"
    return local_path, status


def _strongest(claims: list[Claim]) -> Claim:
    """The profile claim with the strongest proof; the first emulator breaks ties.

    yaps2 pins its GameIndex.yaml by sha1 while armsx2 and lrps2 only match a
    path: alphabetical order served armsx2's copy to all three.
    """
    return max(claims, key=lambda claim: (_claim_rank(claim), -claims.index(claim)))


def arbitrate(conflict: Conflict, mode: str, addressee: str = "platform") -> Decision:
    """Decide a contested destination for the pack being built.

    A pack answers to whoever asked for it. A platform pack must leave the
    frontend's own check green, because a user reading red concludes the pack
    is broken; a pack built for one emulator answers to that emulator. The
    other layer is served as well whenever the destination allows it.

    In ``existence`` mode the frontend only looks for a path, so the emulator's
    file satisfies both sides at once and there is nothing to trade away. In a
    content-checking mode the two answers cannot share one path, and the pack's
    addressee decides; the loss is reported rather than absorbed, because the
    cause is an upstream declaration that needs fixing at its source.
    """
    profile = _strongest(conflict.profile_claims)
    if addressee == "emulator":
        return Decision(conflict, profile, ADDRESSEE)
    if not nativemode.reads_file_contents(mode):
        return Decision(conflict, profile, SERVES_BOTH)
    return Decision(conflict, conflict.platform_claim, FRONTEND_CHECKS_CONTENT)


def _pinned_elsewhere(claims: list[Claim], winner: str | None) -> set[str]:
    """Emulators that pin content the served file does not carry."""
    return {
        c.emulator for c in claims
        if c.emulator and c.local_path != winner and resolution_is_hash_exact(c.status)
    }


def format_decision(decision: Decision) -> str:
    """One line naming the contested slot, the winner and the ground for it."""
    conflict = decision.conflict
    if decision.serves_both:
        winner = decision.winner.local_path
        # A claim that only matched a path is answered by whatever file sits
        # there; only one that pins content can be left wanting.
        pinned_elsewhere = _pinned_elsewhere(conflict.profile_claims, winner)
        served = sorted({
            c.emulator for c in conflict.profile_claims if c.emulator
        } - pinned_elsewhere)
        others = sorted(pinned_elsewhere)
        line = (
            f"{conflict.destination}: serve {winner} "
            f"({', '.join(served) or 'profile'}); the frontend only "
            "checks the path, so both are satisfied"
        )
        if others:
            # One path holds one file: an emulator pinning another is not served.
            line += f"; {', '.join(others)} expected another file"
        return line
    if decision.reason == ADDRESSEE:
        return (
            f"{conflict.destination}: serve {decision.winner.local_path}, "
            "the pack answers to the emulator"
        )
    return (
        f"{conflict.destination}: keep {decision.winner.local_path}, the frontend "
        f"verifies content and would reject "
        f"{conflict.profile_claims[0].local_path}; upstream declaration is wrong"
    )


@dataclass
class Collision:
    """One destination a platform fills with two different files."""

    destination: str
    resolved: list[str]


def _collision_json(collision: Collision) -> dict:
    return {
        "destination": collision.destination,
        "resolved": list(collision.resolved),
    }


def find_collisions(
    config: dict,
    db: dict,
    zip_contents: dict | None = None,
    data_dir_registry: dict | None = None,
) -> list[Collision]:
    """Destinations a platform declares twice and resolves two ways.

    One path holds one file, so whichever declaration the builder reaches
    first decides in silence. Declaring an archive several times to name its
    inner ROMs is the documented zipped_file pattern and resolves to one
    archive; two declarations landing on two files is a contradiction the
    upstream list carries. RetroDECK aims a PC-88 disk subsystem ROM and a
    CoCo disk ROM at one bios/disk.rom.
    """
    fold = bool(config.get("case_insensitive_fs", False))
    by_dest: dict[str, list[dict]] = {}
    for system in (config.get("systems") or {}).values():
        for entry in system.get("files") or []:
            if not isinstance(entry, dict):
                continue
            dest = entry.get("destination") or entry.get("name") or ""
            if dest:
                by_dest.setdefault(_normalize(dest, fold), []).append(entry)

    collisions = []
    for key, entries in sorted(by_dest.items()):
        if len(entries) < 2:
            continue
        resolved = []
        for entry in entries:
            local, _ = resolve_local_file(
                entry,
                db,
                zip_contents,
                dest_hint=entry.get("destination", ""),
                data_dir_registry=data_dir_registry,
            )
            if local and local not in resolved:
                resolved.append(local)
        if len(resolved) > 1 and not _same_file_family(resolved):
            # A file every declaration accepts settles the path: RetroDECK
            # declares ATARIOSB.ROM twice, the second with an md5 list that
            # also names the first one's file.
            if any(_accepted_by_all(path, entries, db) for path in resolved):
                continue
            collisions.append(Collision(destination=key, resolved=resolved))
    return collisions


def _satisfies(profile: Claim, platform: Claim, db: dict) -> bool:
    """Whether the two layers agree on what ships at this destination.

    They agree when they resolve to the same file, when the platform's own
    hash list accepts the profile's file, or when the profile's accepts the
    platform's. A contradiction needs both lists to reject the other side.
    """
    if profile.local_path == platform.local_path:
        return True
    members = profile.entry.get("members")
    if members and platform.local_path:
        return holds_required_members(platform.local_path, members)
    if (
        profile.local_path
        and _lists_hashes(platform.entry)
        and _accepted_by_all(profile.local_path, [platform.entry], db)
    ):
        return True
    return bool(
        platform.local_path
        and _lists_hashes(profile.entry)
        and _accepted_by_all(platform.local_path, [profile.entry], db)
    )


def _lists_hashes(entry: dict) -> bool:
    """Whether a declaration says anything about content at all."""
    return any(
        _declared_hashes(entry, field_name)
        for field_name in ("md5", "sha1", "sha256", "crc32")
    )


def _declared_hashes(entry: dict, field_name: str) -> set[str]:
    """Hashes an entry accepts for one field, declared as a string or a list."""
    value = entry.get(field_name)
    values = value if isinstance(value, list) else [value]
    # Recalbox writes its md5 list comma separated, and profiles do the
    # same for sha256 (mesence names four BS-X dumps in one string).
    return {
        part.strip().lower()
        for v in values
        if v
        for part in str(v).split(",")
        if part.strip()
    }


def _accepted_by_all(path: str, entries: list[dict], db: dict) -> bool:
    """Whether every declaration's own hashes accept the file at path."""
    record = next(
        (e for e in db.get("files", {}).values() if e.get("path") == path), None
    )
    if record is None:
        return False
    for entry in entries:
        if entry.get("zipped_file"):
            declared = parse_md5_list(entry.get("md5"))
            if declared and not any(
                check_inside_zip(path, entry["zipped_file"], m) == "ok" for m in declared
            ):
                return False
            continue
        for field_name in ("md5", "sha1", "sha256", "crc32"):
            declared = _declared_hashes(entry, field_name)
            if declared and str(record.get(field_name, "")).lower() not in declared:
                return False
    return True


def _same_file_family(paths: list[str]) -> bool:
    """Whether the paths are one file and its own pinned variants.

    A platform list built on an older romset pins bytes the primary no longer
    carries, which the repository keeps side by side under .variants. That is
    the documented multi-version case, not two machines fighting for a path.
    """
    canonical = set()
    for path in paths:
        if "/.variants/" in path:
            directory, _, name = path.rpartition("/")
            directory = directory[: -len("/.variants")]
            name = variant_base_name(name)
            path = f"{directory}/{name}"
        canonical.add(path.casefold())
    return len(canonical) == 1


def format_collision(collision: Collision) -> str:
    """One line naming the destination and the files fighting for it."""
    return (
        f"{collision.destination}: declared for "
        + " and for ".join(collision.resolved)
    )


def format_conflict(conflict: Conflict) -> str:
    """One line per conflict, naming both answers and who gave them."""
    emus = ", ".join(conflict.emulators) or "profile"
    return (
        f"{conflict.destination}: pack ships {conflict.platform_claim.local_path} "
        f"({conflict.platform_claim.status}), {emus} says "
        f"{conflict.profile_claims[0].local_path} "
        f"({conflict.profile_claims[0].status})"
    )


def scan_platform(
    platform: str, profiles: dict, db: dict, platforms_dir: str = "platforms"
) -> list[Conflict]:
    """Conflicts on one platform, using the cores that platform actually runs."""
    from common import load_platform_config, resolve_platform_cores

    config = load_platform_config(platform, platforms_dir)
    keys = resolve_platform_cores(config, profiles)
    relevant = {k: profiles[k] for k in keys if k in profiles}
    # The same evidence the builder and the verifier resolve with: without
    # the ZIP index and the data-directory registry the arbitration judged
    # with less than the tools that read its verdict.
    return find_conflicts(
        config,
        relevant,
        db,
        config.get("base_destination", ""),
        {str(c) for c in config.get("standalone_cores", [])},
        build_zip_contents_index(db),
        load_data_dir_registry(platforms_dir),
    )


def main() -> int:
    import argparse
    import json

    from common import (
        list_registered_platforms,
        load_emulator_profiles,
        load_platform_config,
    )

    parser = argparse.ArgumentParser(
        description="Report destinations where an emulator profile contradicts "
        "the file a platform baseline would ship.",
    )
    parser.add_argument("--platform", help="one platform instead of all")
    parser.add_argument("--db", default="database.json")
    parser.add_argument("--platforms-dir", default="platforms")
    parser.add_argument("--emulators-dir", default="emulators")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on any contested destination, not only on one the "
        "pack should have settled by itself",
    )
    args = parser.parse_args()

    with open(args.db, encoding="utf-8") as handle:
        db = json.load(handle)
    profiles = load_emulator_profiles(args.emulators_dir)
    names = (
        [args.platform]
        if args.platform
        else list_registered_platforms(args.platforms_dir)
    )

    found: dict[str, list[Conflict]] = {}
    collided: dict[str, list[Collision]] = {}
    for name in names:
        conflicts = scan_platform(name, profiles, db, args.platforms_dir)
        if conflicts:
            found[name] = conflicts
        collisions = find_collisions(
            load_platform_config(name, args.platforms_dir),
            db,
            build_zip_contents_index(db),
            load_data_dir_registry(args.platforms_dir),
        )
        if collisions:
            collided[name] = collisions

    if args.json:
        # --strict gates on collisions too, so a document that carries only
        # conflicts hands a consumer an empty answer and a non-zero exit.
        payload = {
            "conflicts": {
                platform: [
                    {
                        "destination": c.destination,
                        "ships": c.platform_claim.local_path,
                        "ships_evidence": c.platform_claim.status,
                        "emulators": c.emulators,
                        "expected": c.profile_claims[0].local_path,
                        "expected_evidence": c.profile_claims[0].status,
                    }
                    for c in conflicts
                ]
                for platform, conflicts in found.items()
            },
            "collisions": {
                platform: [_collision_json(c) for c in platform_collisions]
                for platform, platform_collisions in collided.items()
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        fixable = 0
        for platform, conflicts in found.items():
            config = load_platform_config(platform, args.platforms_dir)
            mode = config.get("verification_mode", "existence")
            print(f"{platform}: {len(conflicts)} contradicted [{mode}]")
            for conflict in conflicts:
                decision = arbitrate(conflict, mode)
                fixable += decision.serves_both
                print(f"  {format_decision(decision)}")
        total = sum(len(c) for c in found.values())
        print(
            f"\n{total} contested destinations. {fixable} the pack settles on "
            f"its own, {total - fixable} rest on an upstream declaration."
        )
        for platform, collisions in collided.items():
            print(f"{platform}: {len(collisions)} destinations declared twice")
            for collision in collisions:
                print(f"  {format_collision(collision)}")
        if not args.strict and total:
            print(
                "Reported, not failed: the remainder needs the upstream list "
                "corrected, which no build can do. Use --strict to gate on them."
            )

    return 1 if ((found or collided) and args.strict) else 0


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
