"""A held romset archive carries the members of its own set and nothing else.

The MAME device sets added in March 2026 each held the ROMs of every slot
option reachable from the device: sys68k_cpu30.zip carried 33 members of the
H19 terminal and other cards next to its five CPU-30 ROMs, slvrball.zip 371
members of ISA cards and other slot devices. MAME reads a slot card from the card's own archive, so
those members were never read there, and the archive matched no recipe.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
import zipfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Sets whose archive also carries the ROMs of devices the machine references
# (listxml device_ref): the NAOMI boards pull the JVS I/O board and the Maple
# interface, and MAME finds those ROMs in the machine's own archive too.
DEVICE_ROMS = {
    "airlbios.zip": ("jvs13551.zip", "mie.zip"),
    "f355bios.zip": ("jvs13551.zip", "mie.zip"),
    "f355dlx.zip": ("jvs13551.zip", "mie.zip"),
    "hod2bios.zip": ("jvs13551.zip", "mie.zip"),
    "naomi.zip": ("jvs13551.zip", "mie.zip"),
    "naomi2.zip": ("jvs13551.zip", "mie.zip"),
    "naomigd.zip": ("jvs13551.zip", "mie.zip"),
}

# Archives that share a set name without being that set.
NOT_ROMSETS = {"samples", "artwork"}

# The defect added whole slot-option trees, dozens of members at a time.
MIN_EXTRA = 3

# Kept as they are: rewriting them would drop bytes the collection holds
# nowhere else, and no recipe names the set those bytes belong to.
KEPT = {
    "bios/Acorn/BBC Micro/.variants/bbcb.zip": "zdfs-0.90.rom is held only here",
}


def _hashes(values: object) -> set[str]:
    raw = values if isinstance(values, list) else str(values or "").split(",")
    return {str(value).strip().lower() for value in raw if str(value).strip()}


class HeldArchivesMatchTheirRecipes(unittest.TestCase):
    def test_no_member_outside_every_recipe_of_the_name(self):
        bios = REPO_ROOT / "bios"
        if not bios.is_dir() or not (REPO_ROOT / "recipes").is_dir():
            self.skipTest("bios/ or recipes/ is not present")
        from common import (  # noqa: PLC0415
            list_registered_platforms,
            load_emulator_profiles,
            load_platform_config,
            variant_base_name,
        )
        from hashing import md5_composite_of  # noqa: PLC0415
        from romset_recipes import load_dat_recipes  # noqa: PLC0415

        allowed: dict[str, set[str]] = defaultdict(set)
        for archive, per_label in load_dat_recipes(REPO_ROOT / "recipes").items():
            for recipe in per_label.values():
                allowed[archive.lower()] |= {crc for _name, crc in recipe}
        profiles = load_emulator_profiles(str(REPO_ROOT / "emulators"), skip_aliases=False)
        for profile in profiles.values():
            for entry in profile.get("files", []) or []:
                crcs = {
                    str(member.get("crc32") or "").lower()
                    for member in entry.get("contents") or []
                }
                crcs.discard("")
                if not crcs:
                    continue
                for name in [entry.get("name", "")] + list(entry.get("aliases") or []):
                    if name:
                        allowed[str(name).lower()] |= crcs
        for name, devices in DEVICE_ROMS.items():
            for device in devices:
                allowed[name] |= allowed.get(device, set())

        pinned: set[str] = set()
        zipped: dict[str, set[tuple[str, str]]] = defaultdict(set)
        platforms_dir = str(REPO_ROOT / "platforms")
        for platform in list_registered_platforms(platforms_dir, include_archived=True):
            config = load_platform_config(platform, platforms_dir)
            for system in config.get("systems", {}).values():
                for entry in system.get("files", []) or []:
                    if entry.get("zipped_file"):
                        for md5 in _hashes(entry.get("md5")):
                            zipped[str(entry.get("name", "")).lower()].add(
                                (str(entry["zipped_file"]).casefold(), md5)
                            )
                        continue
                    for key in ("md5", "sha1", "alt_md5"):
                        pinned |= _hashes(entry.get(key))

        offenders = []
        for path in sorted(bios.rglob("*")):
            name = variant_base_name(path.name).lower()
            if not path.is_file() or name not in allowed or not name.endswith(".zip"):
                continue
            if path.parent.name in NOT_ROMSETS:
                continue
            stem = name[:-4]
            try:
                with zipfile.ZipFile(path) as archive:
                    extra = [
                        info for info in archive.infolist()
                        if not info.is_dir()
                        and f"{info.CRC:08x}" not in allowed[name]
                        # a merged set keeps each clone in a folder of its name
                        and not ("/" in info.filename and info.filename.split("/")[0] != stem)
                    ]
                    extra = [
                        info for info in extra
                        if (info.filename.casefold(),
                            hashlib.md5(archive.read(info)).hexdigest()) not in zipped[name]
                    ]
            except zipfile.BadZipFile:
                continue
            if len(extra) < MIN_EXTRA or str(path.relative_to(REPO_ROOT)) in KEPT:
                continue
            blob = path.read_bytes()
            container = {hashlib.md5(blob).hexdigest(), hashlib.sha1(blob).hexdigest(),
                         md5_composite_of(path)}
            if container & pinned:
                continue
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}: {len(extra)} members outside every "
                f"{name} recipe ({', '.join(info.filename for info in extra[:4])})"
            )
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
