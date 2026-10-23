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


# The digest each mode compares. Without one (existence) a file need only be present.
DIGESTS = {"md5": "md5", "sha1": "sha1"}


def accepts(entry: dict, path: Path, mode: str) -> bool:
    """Whether the platform's own check passes the file at path."""
    digest, raw = DIGESTS.get(mode), entry.get(mode) or ""
    values = [v.strip().lower() for v in (raw if isinstance(raw, list) else str(raw).split(",")) if v.strip()]
    if digest is None or not values:
        return path.is_file()  # nothing to compare: the platform checks presence
    try:
        if entry.get("zipped_file"):  # the hash of one member, its name casefolded
            with zipfile.ZipFile(path) as archive:
                names = {name.casefold(): name for name in archive.namelist()}
                member = names.get(entry["zipped_file"].casefold())
                return bool(member) and hashlib.new(digest, archive.read(member)).hexdigest() in values
        actual = hashlib.new(digest, path.read_bytes()).hexdigest()
        if entry.get("zip_digest") != "composite" and any(actual.startswith(v) for v in values):
            return True  # a truncated md5 (Batocera) compares by its prefix
        if digest == DIGESTS["md5"] and path.suffix.lower() == ".zip":  # Recalbox: members in name order
            with zipfile.ZipFile(path) as archive:
                names = sorted(name for name in archive.namelist() if not name.endswith("/"))
                return hashlib.md5(b"".join(map(archive.read, names))).hexdigest() in values
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def check(platform: str, root: Path, api: str = API) -> list[tuple[str, str]]:
    """(destination, ok | wrong | missing) for each entry. Entries with no destination
    (EmuDeck) name no file: their system passes when any file in root has their hash."""
    with urllib.request.urlopen(f"{api}{platform}.json") as response:
        contract = json.load(response)["items"][0]["contract"]
    mode, results = contract["verification_mode"], []
    loose = [path for path in root.iterdir() if path.is_file()] if root.is_dir() else []
    for system_id, system in contract["systems"].items():
        listed = [e for e in system.get("files") or [] if not e.get("destination")]
        for entry in (e for e in system.get("files") or [] if e.get("destination")):
            path = root / entry["destination"]
            verdict = "ok" if accepts(entry, path, mode) else "wrong" if path.is_file() else "missing"
            results.append((entry["destination"], verdict))
        if listed:
            held = any(accepts(e, path, mode) for e in listed for path in loose)
            results.append((f"{system_id}: any file", "ok" if held else "missing"))
    return results


if __name__ == "__main__":
    found = check(sys.argv[1], Path(sys.argv[2]))
    for verdict in ("ok", "wrong", "missing"):
        names = sorted({dest for dest, seen in found if seen == verdict})
        print(f"{verdict}: {len(names)}" + "".join(f"\n  {name}" for name in names if verdict != "ok"))
