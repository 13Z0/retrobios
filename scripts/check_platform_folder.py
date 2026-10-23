"""Check a BIOS folder against one platform's list, from the published data only.

    python check_platform_folder.py batocera path/to/bios
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

API = "https://abdess.github.io/retrobios/api/v1/platforms/"


def accepts(entry: dict, path: Path, mode: str) -> bool:
    """Whether the platform's own check passes the file at path."""
    raw = entry.get(mode) or ""
    values = raw if isinstance(raw, list) else str(raw).split(",")
    declared = [value.strip().lower() for value in values if value.strip()]
    if mode == "existence" or not declared:
        return path.is_file()  # nothing to compare: the platform checks presence
    try:
        if entry.get("zipped_file"):  # the hash of one member, its name casefolded
            with zipfile.ZipFile(path) as archive:
                names = {name.casefold(): name for name in archive.namelist()}
                member = names.get(entry["zipped_file"].casefold())
                return bool(member) and hashlib.new(mode, archive.read(member)).hexdigest() in declared
        actual = hashlib.new(mode, path.read_bytes()).hexdigest()
        if entry.get("zip_digest") != "composite" and any(actual.startswith(d) for d in declared):
            return True  # a truncated md5 (Batocera) compares by its prefix
        if mode == "md5" and path.suffix.lower() == ".zip":  # Recalbox: members in name order
            with zipfile.ZipFile(path) as archive:
                names = sorted(name for name in archive.namelist() if not name.endswith("/"))
                return hashlib.md5(b"".join(map(archive.read, names))).hexdigest() in declared
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def check(platform: str, root: Path, api: str = API) -> list[tuple[str, str]]:
    """(destination, ok | wrong | missing) for each entry of the list."""
    with urllib.request.urlopen(f"{api}{platform}.json") as response:
        contract = json.load(response)["items"][0]["contract"]
    mode = contract["verification_mode"]
    results = []
    for system in contract["systems"].values():
        for entry in system.get("files") or []:
            destination = entry.get("destination") or entry["name"]
            path = root / destination
            verdict = "ok" if accepts(entry, path, mode) else "wrong" if path.is_file() else "missing"
            results.append((destination, verdict))
    return results


if __name__ == "__main__":
    found = check(sys.argv[1], Path(sys.argv[2]))
    for verdict in ("ok", "wrong", "missing"):
        names = sorted({dest for dest, seen in found if seen == verdict})
        print(f"{verdict}: {len(names)}" + "".join(f"\n  {name}" for name in names if verdict != "ok"))
