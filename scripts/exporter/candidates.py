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
from launchers import _names as launcher_names

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
    launched: set[str] = field(default_factory=set)

    def add_launchers(self, records: list[dict]) -> None:
        """Every name the platform's own launch code opens, links or copies.

        Batocera's DuckStation generator walks 22 names (bios_lists,
        duckstationGenerator.py:501-515): each is read on Batocera whatever
        the profiles call it.
        """
        for record in records:
            if isinstance(record, dict):
                for name in launcher_names(record):
                    self.launched.update(_tails(name))

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
        if names & self.launched or (tails and tails[-1] in self.launched):
            return "launcher"
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
    evidence.add_launchers(scraped.get("launchers") or [])

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


# What a platform entry is, for the gates and the filters: firmware a core
# needs, a game's own data, or a name no examined core opens.
BIOS, GAME_DATA, UNREAD = "bios", "game_data", "unread"


class EntryCategories:
    """Categorize a platform's entries by what the cores it runs read.

    An entry only game_data profile entries answer is the game's data, and
    one nothing examined answers is unread. Unread needs the same certainty
    as a removal candidate: every core the platform names is profiled, and
    no core of the platform opens ROM sets by name when the entry is an
    archive, and the platform declares the cores it runs. Anything else,
    and every entry of a platform with no core to examine (BizHawk, MiSTer),
    stays firmware.

    A system that carries its own `cores:` list is settled by those cores:
    the cores examined are the ones it runs, and only its own unprofiled
    cores leave it open. Otherwise the platform's whole list applies.

    `explain` returns the category with its evidence: the profile entries
    that read the file, or the cores of its system that were examined.
    """

    def __init__(self, config: dict, profiles: dict[str, dict],
                 data_registry: dict | None = None) -> None:
        examined = resolve_platform_cores(config, profiles)
        self._unresolved = sorted(unresolved_platform_cores(config, profiles))
        # Without a declared core list, the examined cores are those sharing
        # a system id: a guess about what runs, which cannot prove a negative.
        self._guessed = not config.get("cores")
        self._romsets = sorted(key for key in examined if _loads_romsets(profiles[key]))
        self._profiles = profiles
        # One index per core and per kind, so a proof names its reader.
        self._readers: list[tuple[str, str, ReadEvidence]] = []
        for key in sorted(examined):
            profile = profiles[key]
            files = [f for f in profile.get("files") or [] if isinstance(f, dict)]
            for kind, keep in ((BIOS, lambda f: f.get("category") != GAME_DATA),
                               (GAME_DATA, lambda f: f.get("category") == GAME_DATA)):
                evidence = ReadEvidence()
                evidence.add_profile(
                    {"files": [f for f in files if keep(f)],
                     "data_directories": profile.get("data_directories") if kind == BIOS else []},
                    data_registry or {},
                )
                self._readers.append((kind, key, evidence))
        self._launchers = ReadEvidence()
        self._launchers.add_launchers(config.get("launchers") or [])
        self._by_system: dict[str, list[str]] = {}
        for key in sorted(examined):
            for sid in profiles[key].get("systems") or []:
                self._by_system.setdefault(_norm_system_id(str(sid)), []).append(key)
        # Where the platform says which cores run a system, those settle its
        # entries: an unprofiled core of another system has no say.
        self._system_runs: dict[str, tuple[list[str], list[str]]] = {}
        for sys_id, system in (config.get("systems") or {}).items():
            names = system.get("cores") if isinstance(system, dict) else None
            if names:
                own = {**config, "cores": list(names)}
                for key in ("standalone_cores", "dual_mode_cores"):
                    if key in config:
                        own[key] = [n for n in config.get(key) or [] if n in names]
                self._system_runs[sys_id] = (
                    sorted(resolve_platform_cores(own, profiles)),
                    sorted(unresolved_platform_cores(own, profiles)),
                )

    def _matching(self, key: str, kind: str, entry: dict) -> list[dict]:
        """The profile entries of `key` that name the platform entry."""
        names = {
            str(n).casefold()
            for n in (entry.get("name"), *(entry.get("aliases") or []))
            if n
        }
        found = []
        for f in self._profiles[key].get("files") or []:
            if not isinstance(f, dict) or (f.get("category") == GAME_DATA) != (kind == GAME_DATA):
                continue
            own = {str(n).casefold() for n in (f.get("name"), *(f.get("aliases") or [])) if n}
            if own & names:
                found.append({k: f[k] for k in ("name", "source_ref") if f.get(k)})
        return found

    def explain(self, entry: dict, destination: str, system: str) -> dict:
        """The category of an entry and what establishes it."""
        readers: dict[str, list[dict]] = {BIOS: [], GAME_DATA: []}
        for kind, key, evidence in self._readers:
            proof = evidence.proof(entry, destination)
            if proof is not None:
                readers[kind].append({
                    "emulator": key, "proof": proof,
                    "entries": self._matching(key, kind, entry),
                })
        if self._launchers.proof(entry, destination) is not None:
            readers[BIOS].append({"emulator": "launcher", "proof": "launcher", "entries": []})
        if system in self._system_runs:
            system_cores, unresolved = self._system_runs[system]
        else:
            system_cores = self._by_system.get(_norm_system_id(system), [])
            unresolved = self._unresolved
        # A reader that runs the entry's system comes first and says so: a
        # core of another system may only share the file's name.
        for found in readers.values():
            for reader in found:
                reader["runs_system"] = reader["emulator"] in system_cores
            found.sort(key=lambda r: not r["runs_system"])
        if readers[BIOS]:
            return {"category": BIOS, "read_by": readers[BIOS]}
        if readers[GAME_DATA]:
            return {"category": GAME_DATA, "read_by": readers[GAME_DATA]}
        examined = {"cores_examined": system_cores}
        if self._guessed:
            return {"category": BIOS, "unsettled": "the platform declares no core list", **examined}
        if unresolved:
            return {"category": BIOS,
                    "unsettled": "cores the platform names have no profile: "
                                 + ", ".join(unresolved), **examined}
        if not system_cores:
            return {"category": BIOS,
                    "unsettled": "no examined core emulates this system", **examined}
        if self._romsets and str(entry.get("name", "")).casefold().endswith(_ARCHIVES):
            return {"category": BIOS,
                    "unsettled": "a ROM set, opened by name by " + ", ".join(self._romsets),
                    **examined}
        return {"category": UNREAD, **examined}

    def category(self, entry: dict, destination: str, system: str) -> str:
        return self.explain(entry, destination, system)["category"]
