"""The emulators an EmulationStation system list offers, by profile names.

AmberELEC writes its list as es_systems.cfg XML, EmuELEC as an es_systems.json
it turns into that XML at build time. Both name, per system, the emulators a
user may pick and the cores under each. Some emulators are libretro front
ends (RetroArch, retrorun) whose cores are libretro core names. Every other
emulator is a standalone program named by the OS (`HATARISA`, `PPSSPPSDL`).
"""

from __future__ import annotations

import json
import re

from safeparse import parse_untrusted_xml


def xml_emulators(text: str) -> dict[str, set[str]]:
    """Emulator -> cores, from an es_systems.cfg."""
    root = parse_untrusted_xml(text, "es_systems.cfg")
    pairs: dict[str, set[str]] = {}
    for emulator in root.iter("emulator"):
        name = emulator.get("name")
        if name:
            pairs.setdefault(name, set()).update(
                core.text.strip() for core in emulator.iter("core") if core.text
            )
    if not pairs:
        raise ValueError("es_systems.cfg names no emulator")
    return pairs


def json_emulators(text: str) -> dict[str, set[str]]:
    """Emulator -> cores, from an es_systems.json."""
    pairs: dict[str, set[str]] = {}
    for system in json.loads(text).get("systems", []):
        for emulator in system.get("emulators") or []:
            pairs.setdefault(emulator["name"], set()).update(
                core["name"] for core in emulator.get("cores") or []
            )
    if not pairs:
        raise ValueError("es_systems.json names no emulator")
    return pairs


def standalone_names(emulator: str) -> list[str]:
    """The names a profile may know a standalone emulator by.

    The OS's own spelling, its lowercase, and the lowercase without the
    `sa` suffix the ELEC family appends to a standalone build of an emulator
    that also exists as a core (`HATARISA`, `scummvmsa`, `yabasanshiroSA`).
    """
    lower = emulator.lower()
    names = [emulator, lower]
    stripped = re.sub(r"-?sa$", "", lower)
    if stripped:
        names.append(stripped)
    return list(dict.fromkeys(names))


def es_cores(
    pairs: dict[str, set[str]],
    runners: dict[str, str | None],
    renamed: dict[str, str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """(cores, standalone_cores, dual_mode_cores) of an ES system list.

    `runners` maps each emulator that runs libretro cores to None when the
    cores it lists are libretro core names, or to the one core it always
    loads (AmberELEC's `mame` lists MAME machines and loads the MAME core).
    `renamed` gives the emulator a standalone name stands for when the
    suffix rule cannot (`PPSSPPSDL` is PPSSPP), each one read in the OS's
    launch code.
    """
    renamed = renamed or {}
    libretro: set[str] = set()
    for runner, fixed in runners.items():
        if runner in pairs:
            libretro.update([fixed] if fixed else pairs[runner])
    standalone: set[str] = set()
    for emulator in pairs:
        if emulator in runners:
            continue
        standalone.update(standalone_names(emulator))
        if emulator in renamed:
            standalone.add(renamed[emulator])
    dual = {name for name in standalone if name in libretro}
    return sorted(libretro | standalone), sorted(standalone), sorted(dual)
