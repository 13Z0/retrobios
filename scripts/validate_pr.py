#!/usr/bin/env python3
"""Validate BIOS file contributions in Pull Requests.

Usage:
    python scripts/validate_pr.py [files...]
    python scripts/validate_pr.py --changed   # Auto-detect changed files via git

Multi-layer validation:
1. Hash verified against known databases (System.dat, batocera-systems)
2. File size matches expected value
3. File referenced in ≥1 platform config
4. Duplicate detection against database.json
5. Security checks (no executables, reasonable sizes)

Outputs a structured report suitable for PR comments.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    compute_hashes,
    list_registered_platforms,
    load_database,
    load_emulator_profiles,
    load_platform_config,
    parse_md5_list,
)
from hashing import parse_hash_list

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_DB = "database.json"
DEFAULT_PLATFORMS_DIR = "platforms"
DEFAULT_EMULATORS_DIR = "emulators"

BLOCKED_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".sh",
    ".ps1",
    ".vbs",
    ".js",
    ".msi",
    ".dll",
    ".so",
    ".dylib",
    ".py",
    ".rb",
    ".pl",
}

MAX_FILE_SIZE = 100 * 1024 * 1024


class ValidationResult:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.checks = []  # (status, message) tuples
        self.sha1 = ""
        self.md5 = ""
        self.crc32 = ""
        self.size = 0

    def add_check(self, passed: bool, message: str) -> None:
        self.checks.append(("PASS" if passed else "FAIL", message))

    def add_warning(self, message: str) -> None:
        self.checks.append(("WARN", message))

    def add_info(self, message: str) -> None:
        self.checks.append(("INFO", message))

    @property
    def passed(self) -> bool:
        return all(s != "FAIL" for s, _ in self.checks)

    def to_markdown(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"### {status} `{self.filename}`"]
        lines.append("")
        lines.append(f"- **Path**: `{self.filepath}`")
        lines.append(f"- **Size**: {self.size:,} bytes")
        lines.append(f"- **SHA1**: `{self.sha1}`")
        lines.append(f"- **MD5**: `{self.md5}`")
        lines.append(f"- **CRC32**: `{self.crc32}`")
        lines.append("")

        for status_str, message in self.checks:
            if status_str == "PASS":
                lines.append(f"- Pass: {message}")
            elif status_str == "FAIL":
                lines.append(f"- Fail: {message}")
            elif status_str == "WARN":
                lines.append(f"- Warning: {message}")
            else:
                lines.append(f"- Info: {message}")

        return "\n".join(lines)


def load_platform_hashes(platforms_dir: str, db: dict | None = None) -> dict:
    """Load all known hashes from platform configs.

    With the database, `folders` maps each declared hash to the
    bios/<Manufacturer>/<Console>/ folders the collection files its system
    under, read from the other files of that system it holds.
    """
    known = {"sha1": set(), "md5": set(), "names": set(), "folders": {}}
    systems_of: dict[str, set[str]] = {}
    folders_of: dict[str, dict[str, int]] = {}
    files = (db or {}).get("files", {})
    by_md5 = (db or {}).get("indexes", {}).get("by_md5", {})

    if not os.path.isdir(platforms_dir) or yaml is None:
        return known

    # Read the way verify reads them: inheritance and shared groups resolved,
    # and a Recalbox md5 field split into the revisions it lists. The raw
    # file kept "a,b,c" as one value, so a PR adding the seventh accepted
    # falcon.img was told its hash differs.
    for name in list_registered_platforms(platforms_dir, include_archived=True):
        try:
            config = load_platform_config(name, platforms_dir)
        except (OSError, yaml.YAMLError) as exc:
            print(f"WARNING: {name}: {exc}", file=sys.stderr)
            continue

        for system_id, system in config.get("systems", {}).items():
            for file_entry in system.get("files", []):
                sha1s = parse_hash_list(file_entry.get("sha1"))
                md5s = parse_md5_list(file_entry.get("md5"))
                known["sha1"].update(sha1s)
                known["md5"].update(md5s)
                if "name" in file_entry:
                    known["names"].add(file_entry["name"])
                for value in (*sha1s, *md5s):
                    systems_of.setdefault(value, set()).add(system_id)
                held = next((s for s in sha1s if s in files), None) or next(
                    (by_md5[m] for m in md5s if m in by_md5), None
                )
                parts = str(files.get(held, {}).get("path", "")).split("/")
                if len(parts) >= 4 and parts[0] == "bios" and not parts[2].startswith("."):
                    folder = "/".join(parts[:3]) + "/"
                    counts = folders_of.setdefault(system_id, {})
                    counts[folder] = counts.get(folder, 0) + 1

    # A system whose files sit under several manufacturers (arcade sets,
    # computers sharing ROM chips) names no single home: only a system
    # filed under one manufacturer says where a newcomer belongs.
    for value, systems in systems_of.items():
        folders: set[str] = set()
        for system_id in systems:
            held_in = folders_of.get(system_id, {})
            if len({folder.split("/")[1] for folder in held_in}) == 1:
                folders.update(held_in)
        if folders:
            known["folders"][value] = folders
    return known


def load_profile_hashes(emulators_dir: str) -> dict[str, dict[str, set[str]]]:
    """Hash value to the emulators whose profile declares it, per algorithm.

    A file only a core reads (a standalone emulator's firmware, a game
    engine's data) is in no platform list: the review called every such
    contribution "not referenced, needs manual review".
    """
    known: dict[str, dict] = {"sha1": {}, "sha256": {}, "md5": {}, "crc32": {}}
    for emulator, profile in load_emulator_profiles(emulators_dir).items():
        for entry in profile.get("files") or []:
            if not isinstance(entry, dict):
                continue
            # The resolver's reading: comma lists split (mesence's four
            # sha256), and a crc32 counts only where no stronger hash is
            # declared, bounded by the declared size (626 entries name a
            # file by crc32 alone, clk's Electron os.rom among them).
            for algo in ("sha1", "sha256", "md5"):
                for value in parse_hash_list(entry.get(algo)):
                    known[algo].setdefault(value, set()).add(emulator)
            if not any(entry.get(algo) for algo in ("sha1", "sha256", "md5")):
                size = entry.get("size")
                sizes = [int(v) for v in size] if isinstance(size, list) else (
                    [int(size)] if size else [None]
                )
                for value in parse_hash_list(entry.get("crc32")):
                    for accepted in sizes:
                        key = (value.zfill(8), accepted)
                        known["crc32"].setdefault(key, set()).add(emulator)
    return known


def validate_file(
    filepath: str,
    db: dict | None,
    platform_hashes: dict,
    profile_hashes: dict | None = None,
) -> ValidationResult:
    """Run all validation checks on a file."""
    result = ValidationResult(filepath)

    # Whoever opened the pull request chose this path, so its shape is
    # settled before anything is read. A symlink to an endless device would
    # otherwise be hashed until the job timed out, and a symlink out of the
    # checkout would be reported as though its target had been contributed.
    if os.path.islink(filepath):
        result.add_check(False, "Symlinks are not allowed")
        return result

    if not os.path.exists(filepath):
        result.add_check(False, f"File not found: {filepath}")
        return result

    if not os.path.isfile(filepath):
        result.add_check(False, "Not a regular file")
        return result

    result.size = os.path.getsize(filepath)
    hashes = compute_hashes(filepath)
    result.sha1 = hashes["sha1"]
    result.md5 = hashes["md5"]
    result.crc32 = hashes["crc32"]

    ext = os.path.splitext(filepath)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        result.add_check(False, f"Blocked file extension: {ext}")

    if result.size > MAX_FILE_SIZE:
        result.add_check(
            False,
            f"File too large for embedded storage ({result.size:,} > {MAX_FILE_SIZE:,} bytes). Use storage: external in platform config.",
        )
    elif result.size == 0:
        result.add_check(False, "File is empty (0 bytes)")
    else:
        result.add_check(True, f"File size OK ({result.size:,} bytes)")

    # Paths are compared as the database writes them, relative to the
    # checkout: an absolute path to the file itself read as its duplicate.
    relative = filepath
    if os.path.isabs(filepath):
        candidate = os.path.relpath(filepath, os.getcwd())
        if not candidate.startswith(".."):
            relative = candidate

    if db:
        if result.sha1 in db.get("files", {}):
            existing = db["files"][result.sha1]
            held_at = {
                os.path.normpath(p)
                for p in [existing.get("path"), *(existing.get("paths") or [])] if p
            }
            if os.path.normpath(relative) in held_at:
                # The file the database was built from, not a copy of it: a
                # PR that regenerated database.json was told each new file
                # duplicated itself.
                result.add_check(True, "Already in the database at this path")
            else:
                result.add_warning(
                    f"Duplicate: identical file already exists at `{existing['path']}`"
                )
        else:
            result.add_check(True, "Not a duplicate in database")

    sha1_known = result.sha1 in platform_hashes.get("sha1", set())
    md5_known = result.md5 in platform_hashes.get("md5", set())
    name_known = result.filename in platform_hashes.get("names", set())

    profile_hashes = profile_hashes or {}
    by_profile = (
        profile_hashes.get("sha1", {}).get(result.sha1)
        or profile_hashes.get("sha256", {}).get(hashes.get("sha256", ""))
        or profile_hashes.get("md5", {}).get(result.md5)
        or profile_hashes.get("crc32", {}).get((result.crc32, result.size))
        or profile_hashes.get("crc32", {}).get((result.crc32, None))
    )

    if sha1_known:
        result.add_check(True, "SHA1 matches known platform requirement")
    elif md5_known:
        result.add_check(True, "MD5 matches known platform requirement")
    elif by_profile:
        names = sorted(by_profile)
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        result.add_check(True, f"Hash matches a file the emulator profile declares: {shown}")
    elif name_known:
        result.add_warning(
            "Filename matches a known requirement but hash differs - may be a variant"
        )
    else:
        result.add_warning(
            "File not referenced in any platform config - needs manual review"
        )

    normalized = os.path.normpath(relative)
    expected_folders = sorted(
        platform_hashes.get("folders", {}).get(result.sha1)
        or platform_hashes.get("folders", {}).get(result.md5) or set()
    )
    if normalized.startswith("bios" + os.sep):
        parts = normalized.split(os.sep)
        expected = (platform_hashes.get("folders", {}).get(result.sha1)
                    or platform_hashes.get("folders", {}).get(result.md5) or set())
        here = f"bios/{parts[1]}/{parts[2]}/" if len(parts) >= 4 else ""
        makers = {folder.split("/")[1] for folder in expected}
        if expected and (len(parts) < 4 or parts[1] not in makers):
            # Depth alone passed a PS1 BIOS filed under bios/Nintendo/Game Boy/.
            result.add_warning(
                "Placed under " + (here or "the bios/ root") + ", while the other files of "
                "its system are in " + " or ".join(f"`{f}`" for f in sorted(expected))
            )
        elif len(parts) >= 4:
            result.add_check(True, f"Correct placement: bios/{parts[1]}/{parts[2]}/")
        else:
            result.add_warning("File should be in bios/Manufacturer/Console/ structure")
    elif expected_folders:
        # The contributor asks before forking: say where it goes.
        result.add_warning(
            "File is not under bios/ yet. Its system's files are in "
            + " or ".join(f"`{f}`" for f in expected_folders)
        )
    else:
        result.add_warning(
            "File is not under bios/ yet. Place it in bios/Manufacturer/Console/"
        )

    if name_known and not sha1_known and not md5_known:
        result.add_info(
            "This may be a valid variant. If accepted, it will be placed in "
            f"`.variants/{result.filename}.{result.sha1[:8]}`"
        )

    return result


def _git_lines(*args: str) -> list[str] | None:
    """Output lines of a git command, or None when git refuses it."""
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def get_changed_files() -> list[str]:
    """BIOS files this checkout changes against the project's main line.

    Committed changes are read against upstream/<base> first, then
    origin/<base>: on a fork whose main carries the commit, origin/main is
    HEAD and the diff was empty. Uncommitted and untracked files count too,
    since CONTRIBUTING has the file copied into bios/ before any commit.
    A git that fails outright is an error, never "nothing changed".
    """
    changed: list[str] = []
    compared = False
    for remote in ("upstream", "origin"):
        for base in ("main", "master", "v2"):
            committed = _git_lines("diff", "--name-only", f"{remote}/{base}...HEAD")
            if committed is None:
                continue
            compared = True
            changed.extend(committed)
            break
        if compared and changed:
            break
    staged = _git_lines("diff", "--cached", "--name-only")
    local = _git_lines("diff", "--name-only")
    untracked = _git_lines("ls-files", "--others", "--exclude-standard", "--", "bios")
    if staged is None and local is None and untracked is None:
        raise RuntimeError("git cannot read this checkout")
    for found in (staged, local, untracked):
        changed.extend(found or [])
    if not compared:
        print(
            "no upstream/ or origin/ main line to compare against, reading the "
            "staged, modified and untracked files only",
            file=sys.stderr,
        )
    seen: set[str] = set()
    return [
        f for f in changed
        if f.startswith("bios/") and not (f in seen or seen.add(f)) and os.path.isfile(f)
    ]


def _files_to_validate(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> list:
    """The files named on the command line, or the changed ones."""
    if args.changed and args.files:
        # Named files were dropped for the git diff, and the run reported
        # "No changed BIOS files detected" about files it never looked at.
        parser.error("--changed reads the git diff: name files or pass --changed, not both")
    if not args.changed:
        if not args.files:
            parser.error("No files specified. Use --changed or provide file paths.")
        return args.files
    try:
        files = get_changed_files()
    except (RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    if not files and not args.json:
        print("No changed BIOS files detected")
    return files


def _load_database_if_any(path: str) -> dict | None:
    """The database, optional only where the default is meant.

    A mistyped --db read as "no database" and every file passed its
    duplicate and placement checks unchecked. A damaged one did the same
    after a warning.
    """
    if path == DEFAULT_DB and not os.path.exists(path):
        return None
    return load_database(path)


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Validate BIOS file contributions")
    parser.add_argument("files", nargs="*", help="Files to validate")
    parser.add_argument(
        "--changed", action="store_true", help="Auto-detect changed BIOS files"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to database.json")
    parser.add_argument(
        "--platforms-dir", default=DEFAULT_PLATFORMS_DIR,
        help="platform YAML directory (default: %(default)s)",
    )
    parser.add_argument(
        "--emulators-dir", default=DEFAULT_EMULATORS_DIR,
        help="emulator profile directory (default: %(default)s)",
    )
    # One output format: --json beside --markdown printed JSON and dropped
    # the markdown without a word.
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--markdown", action="store_true", help="Output as markdown (for PR comments)"
    )
    output.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    files = _files_to_validate(args, parser)
    if not files:
        if args.json:
            print("[]")
        return

    db = _load_database_if_any(args.db)
    platform_hashes = load_platform_hashes(args.platforms_dir, db)
    profile_hashes = load_profile_hashes(args.emulators_dir)

    results = []
    for f in files:
        result = validate_file(f, db, platform_hashes, profile_hashes)
        results.append(result)

    all_passed = all(r.passed for r in results)

    if args.json:
        output = []
        for r in results:
            output.append(
                {
                    "file": r.filepath,
                    "passed": r.passed,
                    "sha1": r.sha1,
                    "md5": r.md5,
                    "size": r.size,
                    "checks": [{"status": s, "message": m} for s, m in r.checks],
                }
            )
        print(json.dumps(output, indent=2))
    elif args.markdown:
        lines = ["## BIOS Validation Report", ""]
        status = "all checks passed" if all_passed else "some checks failed"
        lines.append(f"**Status**: {status}")
        lines.append("")

        for r in results:
            lines.append(r.to_markdown())
            lines.append("")

        print("\n".join(lines))
    else:
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"\n[{status}] {r.filepath}")
            print(f"  SHA1: {r.sha1}")
            print(f"  MD5:  {r.md5}")
            print(f"  Size: {r.size:,}")
            for s, m in r.checks:
                marker = (
                    "ok"
                    if s == "PASS"
                    else "fail"
                    if s == "FAIL"
                    else "warn"
                    if s == "WARN"
                    else "info"
                )
                print(f"  [{marker}] {m}")

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
