"""Entries a platform declares that no core it runs is known to open.

The export never removes a platform's entry. An entry the truth did not
pair may still be read: by a name or an alias, by its content, under a
directory a core walks, inside a data directory. Each of those is looked
for in the profiles of the cores the platform runs, and an entry none of
them answers is listed with what was examined. Absence of a match is not
proof that the code ignores the file, so the list is a report, and a core
the platform names that no profile describes leaves the verdict open.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import _norm_system_id, resolve_platform_cores, unresolved_platform_cores
from exporter.baseline import HASH_FIELDS, NativeSystem, _hash_values

# Batocera cuts some md5 to 29 characters: the prefix still names the dump.
_FULL_MD5 = 32
_CUT_MD5 = 29


def _tails(path: str) -> list[str]:
    """Every trailing run of segments of a path, longest first."""
    parts = [p for p in path.casefold().strip("/").split("/") if p]
    return ["/".join(parts[i:]) for i in range(len(parts))]


def _entry_hashes(entry: dict) -> set[str]:
    values = {v for f in HASH_FIELDS for v in _hash_values(entry, f)}
    for member in entry.get("contents") or []:
        if isinstance(member, dict):
            values |= {v for f in HASH_FIELDS for v in _hash_values(member, f)}
    return values


@dataclass
class ReadEvidence:
    """What the examined profiles open, indexed for one lookup per entry."""

    names: set[str] = field(default_factory=set)
    tails: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    directories: set[str] = field(default_factory=set)
    trees: set[str] = field(default_factory=set)
    tree_members: set[str] = field(default_factory=set)

    def add_profile(self, profile: dict, data_registry: dict) -> None:
        for entry in profile.get("files") or []:
            if not isinstance(entry, dict):
                continue
            locations = [
                str(entry[key]) for key in ("path", "standalone_path", "destination")
                if entry.get(key)
            ]
            if entry.get("type") == "directory":
                self.directories |= {
                    "/".join(p for p in loc.casefold().split("/") if p) + "/"
                    for loc in [*locations, str(entry.get("name", ""))] if loc
                }
                continue
            self.names |= {
                str(n).casefold()
                for n in (entry.get("name"), *(entry.get("aliases") or []))
                if n
            }
            for location in locations:
                tails = _tails(location)
                self.tails.update(tails)
                if tails:
                    self.names.add(tails[-1])
            self.hashes |= _entry_hashes(entry)
        for tree in profile.get("data_directories") or []:
            if not isinstance(tree, dict):
                continue
            destination = str(tree.get("destination") or "").casefold().strip("/")
            if destination:
                self.trees.add(destination + "/")
                continue
            # A tree laid at the root: only its own files say what it holds.
            cache = (data_registry.get(str(tree.get("ref", ""))) or {}).get("local_cache")
            if cache and Path(cache).is_dir():
                self.tree_members |= {
                    p.relative_to(cache).as_posix().casefold()
                    for p in Path(cache).rglob("*") if p.is_file()
                }

    def proof(self, entry: dict, destination: str) -> str | None:
        """How a profile reads the entry, or None when nothing answers."""
        names = {
            str(n).casefold()
            for n in (entry.get("name"), *(entry.get("aliases") or []))
            if n
        }
        tails = _tails(destination)
        if any(t in self.tails for t in tails[:-1]):
            return "path"
        if names & self.names or (tails and tails[-1] in self.names):
            return "name"
        hashes = {
            v for f in (*HASH_FIELDS, "alt_md5") for v in _hash_values(entry, f)
        }
        if hashes & self.hashes or any(
            len(v) < _FULL_MD5 and any(h.startswith(v) for h in self.hashes)
            for v in hashes if len(v) >= _CUT_MD5
        ):
            return "hash"
        full = "/" + (tails[0] if tails else "")
        if any("/" + d in full for d in self.directories):
            return "directory"
        if any("/" + t in full for t in self.trees):
            return "data_directory"
        if full[1:] in self.tree_members:
            return "data_directory"
        return None


def _loads_romsets(profile: dict) -> bool:
    """A core that opens ROM sets by name: MAME pulls a device's set from
    device_t::searchpath(), and no profile lists every device set."""
    return any(
        isinstance(f, dict) and f.get("category") == "bios_zip"
        for f in profile.get("files") or []
    )


_ARCHIVES = (".zip", ".7z")

REASONS = {
    "candidate": "no profile of a core the platform runs names it",
    "unresolved": "a core the platform names has no profile yet",
    "romset": "a ROM set, and these cores open sets their profiles do not list",
}


def removal_candidates(
    systems: dict[str, NativeSystem],
    scraped: dict | None,
    profiles: dict[str, dict],
    data_registry: dict | None = None,
) -> list[dict]:
    """The platform's unpaired entries that no examined core is known to open.

    Every core the platform runs is searched. `cores_examined` names those
    among them that emulate the entry's system, the ones a reader expects to
    find it in. `verdict` is `candidate` when nothing examined could open the
    entry, `undetermined` when something still could: a core the platform
    names that no profile describes, or a core of the system that opens ROM
    sets by name. A platform that resolves no core (BizHawk wires each
    firmware in its own code, MiSTer's BiosDB is its authority) gives nothing
    to examine and lists nothing. Nothing is removed from the platform's file.
    """
    if not scraped:
        return []
    examined = resolve_platform_cores(scraped, profiles)
    if not examined:
        return []
    unresolved = sorted(unresolved_platform_cores(scraped, profiles))
    evidence = ReadEvidence()
    for key in sorted(examined):
        evidence.add_profile(profiles[key], data_registry or {})

    system_of = {
        id(file_entry): sys_key
        for sys_key, sys_data in (scraped.get("systems") or {}).items()
        for file_entry in sys_data.get("files", [])
    }
    by_system: dict[str, list[str]] = {}
    for key in sorted(examined):
        for sid in profiles[key].get("systems") or []:
            by_system.setdefault(_norm_system_id(str(sid)), []).append(key)

    romset_cores = {key for key in examined if _loads_romsets(profiles[key])}

    listed: list[dict] = []
    for system in systems.values():
        for entry in system.files:
            if entry.platform is None or entry.truth is not None or not entry.name:
                continue
            if evidence.proof(entry.platform, entry.destination) is not None:
                continue
            slug = system_of.get(id(entry.platform), entry.native_system)
            cores = by_system.get(_norm_system_id(slug), [])
            readers = (
                [c for c in cores if c in romset_cores]
                if entry.name.casefold().endswith(_ARCHIVES) else []
            )
            if unresolved:
                verdict, reason = "undetermined", REASONS["unresolved"]
            elif readers:
                verdict = "undetermined"
                reason = f"{REASONS['romset']}: {', '.join(readers)}"
            else:
                verdict, reason = "candidate", REASONS["candidate"]
            listed.append({
                "system": entry.native_system,
                "name": entry.name,
                "destination": entry.destination,
                "verdict": verdict,
                "reason": reason,
                "cores_examined": cores,
                "unresolved_cores": unresolved,
            })
    return listed
