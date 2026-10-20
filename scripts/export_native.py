#!/usr/bin/env python3
"""Rewrite each platform's own BIOS file, corrected by the ground truth.

The output is the file the platform maintains, not a rendering of our data
in its syntax: what the truth can prove is applied, what it says nothing
about is left alone, and what it knows and the platform lacks is added. The
formats that carry code are patched rather than regenerated, so the checker
a platform ships keeps working.

Usage:
    python scripts/export_native.py --all --fetch           # into exports/
    python scripts/export_native.py --platform recalbox --upstream-dir up/
    python scripts/export_native.py --platform retrobat --refresh-cache
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.client
import re
import json
import sys
import tarfile
import urllib.request
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifacts import file_lock, write_bytes_atomic, write_if_changed, write_text_atomic
import yaml

from common import (
    platform_arg,
    list_registered_platforms,
    load_database,
    load_emulator_profiles,
    load_platform_config,
    native_layer,
    require_platform,
    yaml_load,
)
from exporter import discover_exporters
from exporter.baseline import HASH_FIELDS, _hash_values, _single_size, build_native_model
from truth import generate_platform_truth

DEFAULT_CACHE = ".cache/upstream-native"
# The database generate_truth reads by default.
TRUTH_DB = "database.json"
SOURCES_INDEX = ".sources.json"
_USER_AGENT = "retrobios-exporter/1.0"
_MAX_BYTES = 64 * 1024 * 1024


def _load_sources(index: Path | None) -> dict[str, str]:
    if index is None or not index.is_file():
        return {}
    try:
        return json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def source_key(destination: Path, index: Path | None) -> str:
    """How the index names a cached file: its path below the cache root.

    A key that repeated the cache directory as it was typed changed with the
    working directory, and a file recorded from one place looked unrecorded
    from another.
    """
    if index is not None:
        try:
            return destination.resolve().relative_to(index.parent.resolve()).as_posix()
        except ValueError:
            pass
    return str(destination)


def load_sources(upstream_dir: Path) -> dict[str, str]:
    """Cached file -> URL it was fetched from, for a whole cache directory."""
    return _load_sources(upstream_dir / SOURCES_INDEX)


class OriginalUnreadable(ValueError):
    """A platform's file that is not what its format says it is."""


def _read_archive(unpack: Callable[[bytes], dict[str, str]], payload: bytes) -> dict[str, str]:
    """Unpack an original, naming the remedy when its bytes are not one."""
    try:
        return unpack(payload)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, zlib.error, KeyError) as exc:
        raise OriginalUnreadable(
            f"archive unreadable ({exc}): --refresh-cache downloads it again"
        ) from exc


def fetch(
    url: str,
    destination: Path,
    index: Path | None = None,
    refresh: bool = False,
    check: Callable[[bytes], object] | None = None,
) -> bytes:
    """Download an original once, then read it from the cache.

    The cache path carries the file's own name and nothing of the revision it
    came from, so a file fetched under one pin was served under every later
    one and pinned_base() stopped having any effect after the first run. The
    URL that produced each cached file is recorded beside the cache, and a
    different URL refetches. A cache written before this index existed keeps
    being served: nothing recorded means nothing contradicted.

    A branch URL names no revision, so the same URL serves new bytes after
    the platform moves. refresh downloads again whatever the cache holds:
    it is what a rescrape calls, so the original and its transcription
    describe the same moment.
    """
    key = source_key(destination, index)

    def locked() -> contextlib.AbstractContextManager[object]:
        # The bytes and the URL that names them change together: written
        # under two locks, two runs wanting two URLs for one path could leave
        # one's bytes beside the other's index entry, and the wrong revision
        # was then served as current.
        if index is None:
            return contextlib.nullcontext()
        index.parent.mkdir(parents=True, exist_ok=True)
        return file_lock(index.with_name(f".{index.name}.lock"))

    with locked():
        if (
            not refresh
            and destination.exists()
            and _load_sources(index).get(key, url) == url
        ):
            return destination.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(_MAX_BYTES + 1)
    if len(payload) > _MAX_BYTES:
        raise OriginalUnreadable(f"{url}: response larger than {_MAX_BYTES} bytes")
    # An error page served with 200 replaced a good archive and was then
    # served on every run: what does not read is never cached.
    if check is not None:
        check(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with locked():
        write_bytes_atomic(str(destination), payload)
        if index is not None:
            # Platforms refresh in parallel: the index is read again under
            # the lock, or the last writer erased what the others recorded.
            recorded = _load_sources(index)
            recorded[key] = url
            write_text_atomic(
                str(index), json.dumps(recorded, indent=2, sort_keys=True) + "\n"
            )
    return payload


def _remove_stale(platform_dir: Path, produced: set[str]) -> None:
    """Remove what an earlier run wrote and this one does not.

    RetroPie writes one scriptmodule per package it corrects and RetroDECK one
    manifest per component: a package corrected by an older truth kept its
    file beside a report saying it was no longer touched, and the directory
    contradicted the run.
    """
    if not platform_dir.is_dir():
        return
    for path in sorted(platform_dir.rglob("*"), reverse=True):
        relative = path.relative_to(platform_dir).as_posix()
        if path.is_file() and relative not in produced:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def pinned_base(wanted: dict[str, str], scraped: dict | None) -> str:
    """The revision our transcription came from, when the YAML records it.

    A scraper pins a stable tag (batocera-43.1, BizHawk 2.11.1) and writes
    that URL into the platform YAML. Patching the branch tip instead would
    correct a file our data never described.
    """
    source = _raw_url(str((scraped or {}).get("source", "")))
    tree = _GITHUB_TREE.match(source)
    if tree:
        # RetroDECK reads one manifest per component: the YAML records the
        # tree at the commit read, and every file is taken at that commit.
        return "https://raw.githubusercontent.com/{}/{}/{}/".format(*tree.groups())
    for relative in wanted:
        if source.endswith("/" + relative):
            return source[: -len(relative)]
    return ""


_GITHUB_TREE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([0-9a-f]{40})/?$")


def _raw_url(url: str) -> str:
    """A GitHub blob page names a revision but serves HTML, raw serves bytes."""
    marker = "/blob/"
    if url.startswith("https://github.com/") and marker in url:
        owner_repo, _, path = url[len("https://github.com/") :].partition(marker)
        return f"https://raw.githubusercontent.com/{owner_repo}/{path}"
    return url


def wanted_sources(
    exporter: object, systems: dict, scraped: dict | None = None
) -> dict[str, str]:
    """Cache-relative name -> URL of every file the exporter patches."""
    wanted = dict(exporter.native_sources())
    components = getattr(exporter, "components", None)
    if callable(components):
        for component in components(systems):
            wanted[f"{component}/component_manifest.json"] = exporter.component_url(
                component
            )

    base = pinned_base(wanted, scraped)
    if base:
        wanted = {
            relative: _at_revision(wanted[relative], base) or base + relative
            for relative in wanted
        }
    source = str((scraped or {}).get("source", ""))
    return {
        relative: _tarball_at(url, source) or url for relative, url in wanted.items()
    }


_REVISION = (
    re.compile(r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/([^/]+)/"),
    re.compile(r"^https://github\.com/[^/]+/[^/]+/(?:tree|blob)/([^/]+)"),
    re.compile(r"^https://gitlab\.com/.+?/-/(?:raw|archive)/([^/]+)/"),
    re.compile(r"^https://codeload\.github\.com/[^/]+/[^/]+/tar\.gz/([^/]+)$"),
)


def source_revision(source: str) -> str:
    """The revision a platform YAML's source: URL names, '' when none."""
    for pattern in _REVISION:
        match = pattern.match(source)
        if match:
            return match.group(1)
    return ""


def unpinned(wanted: dict[str, str], scraped: dict | None) -> list[str]:
    """Every file the export would read off the revision the YAML records.

    pinned_base() returned '' when the source named no revision or no
    longer ended in the file, and the exporter's own branch URL was then
    patched without a word: a file our transcription never described.
    """
    source = str((scraped or {}).get("source", ""))
    revision = source_revision(source)
    if not revision:
        return [f"the platform's source names no revision ({source or 'none'}): rescrape it"]
    return [
        f"{relative} would be read at {url}, not at {revision}: rescrape the platform"
        for relative, url in wanted.items()
        if f"/{revision}/" not in url and not url.endswith(f"/{revision}")
    ]


_CODELOAD = re.compile(r"^https://codeload\.github\.com/([^/]+)/([^/]+)/tar\.gz/")


def _tarball_at(url: str, source: str) -> str:
    """The recorded tarball when the original is a tarball of the same repository.

    RetroPie's declarations are spread over every scriptmodule, so its scraper
    reads the repository archive. The archive the YAML records is the one at
    the commit it was read from, the exporter's own URL names the branch.
    """
    wanted, recorded = _CODELOAD.match(url), _CODELOAD.match(source)
    if wanted and recorded and wanted.group(1, 2) == recorded.group(1, 2):
        return source
    return ""


_RAW_GITHUB = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/")


def _at_revision(url: str, base: str) -> str:
    """A file of the pinned repository, read at the pinned revision, empty
    when the URL is not a raw GitHub URL of that repository.

    ROCKNIX's checker sits in packages/rocknix/, the emulators it names in
    packages/virtual/: no shared directory, one revision.
    """
    pinned, other = _RAW_GITHUB.match(base), _RAW_GITHUB.match(url)
    if pinned and other and pinned.group(1, 2) == other.group(1, 2):
        return pinned.group(0) + url[other.end():]
    return ""


def collect_originals(
    exporter: object,
    systems: dict,
    upstream_dir: Path,
    allow_fetch: bool,
    scraped: dict | None = None,
    refresh: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Gather the platform's own files, from disk or from upstream.

    With fetching on, an existing file still goes through fetch(): reading
    it straight from disk skipped the recorded URL, so a file cached under
    one pin kept being patched after the platform YAML moved to the next.
    """
    wanted = wanted_sources(exporter, systems, scraped)
    root = upstream_dir / exporter.platform_name()
    index = upstream_dir / SOURCES_INDEX
    originals: dict[str, str] = {}
    missing: list[str] = []
    unpack = getattr(exporter, "unpack", None)
    for relative, url in wanted.items():
        path = root / relative
        archive = callable(unpack) and relative.endswith((".zip", ".tar.gz"))
        check = (lambda raw: _read_archive(unpack, raw)) if archive else None
        try:
            if allow_fetch:
                payload = fetch(url, path, index, refresh, check)
            elif path.exists():
                payload = path.read_bytes()
            else:
                missing.append(f"{relative}: absent and fetching is off")
                continue
            if archive:
                originals.update(_read_archive(unpack, payload))
            else:
                originals[relative] = payload.decode("utf-8", errors="replace")
        # One platform's unreadable file fails that platform: raised, it
        # stopped --all before the platforms after it.
        except (OSError, http.client.HTTPException, OriginalUnreadable) as exc:
            missing.append(f"{relative}: {exc}")

    return originals, missing


class StaleTruth(ValueError):
    """The truth file no longer describes the profiles it was built from."""


@functools.lru_cache(maxsize=None)
def _truth_sources(platforms_dir: str) -> tuple[dict, dict, dict | None]:
    """Registry, profiles and database, as generate_truth reads them."""
    registry_path = Path(platforms_dir) / "_registry.yml"
    registry: dict = {}
    if registry_path.exists():
        with open(registry_path) as handle:
            registry = (yaml_load(handle) or {}).get("platforms", {})
    profiles = load_emulator_profiles("emulators")
    db = load_database(TRUTH_DB) if Path(TRUTH_DB).exists() else None
    return registry, profiles, db


def current_truth(platform: str, config: dict, platforms_dir: str) -> dict:
    """The truth model generate_truth would write now, as YAML reads it back."""
    registry, profiles, db = _truth_sources(platforms_dir)
    model = generate_platform_truth(
        platform, config, registry.get(platform, {}), profiles, db=db
    )
    return yaml_load(yaml.dump(model, default_flow_style=False, sort_keys=False,
                               allow_unicode=True)) or {}


def load_inputs(
    platform: str, truth_dir: Path, platforms_dir: str
) -> tuple[dict | None, dict | None]:
    """The truth and the scraped config of a platform, None where absent.

    A truth file is an artifact of the profiles: read at any age, one from
    before `_required_by` existed silently undid the RetroPie guard that
    reads it. One that no longer matches what generate_truth builds now is
    refused.
    """
    truth_file = truth_dir / f"{platform}.yml"
    truth: dict | None = None
    try:
        config = load_platform_config(platform, platforms_dir)
    except (FileNotFoundError, OSError):
        config = None
    if truth_file.exists():
        with open(truth_file) as handle:
            truth = yaml_load(handle) or {}
        if config is not None and truth != current_truth(platform, config, platforms_dir):
            raise StaleTruth(
                f"{truth_file} no longer matches the profiles: run "
                f"python scripts/generate_truth.py --platform {platform}"
            )
    return truth, native_layer(config)


def refresh_cache(
    platform: str,
    exporter_class: type,
    truth_dir: Path,
    platforms_dir: str,
    upstream_dir: Path,
) -> tuple[bool, list[str]]:
    """Download a platform's own files again. Returns (ok, messages)."""
    try:
        truth, scraped = load_inputs(platform, truth_dir, platforms_dir)
    except StaleTruth as exc:
        return False, [str(exc)]
    systems, _ = build_native_model(truth or {}, scraped)
    exporter = exporter_class()
    wanted = wanted_sources(exporter, systems, scraped)
    off_pin = unpinned(wanted, scraped)
    if off_pin:
        return False, off_pin
    _, missing = collect_originals(
        exporter, systems, upstream_dir, True, scraped, refresh=True
    )
    if missing:
        return False, missing
    return True, [f"{len(wanted)} file(s) fetched"]


@dataclass
class LandedChanges:
    """What an export changed in the platform's file, field by field."""

    applied: list[str] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)
    resized: list[str] = field(default_factory=list)
    unsized: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    requirements: int = 0


def _count(number: int, noun: str, plural: str = "") -> str:
    """"1 hash", "2 hashes": a count with its noun in the right number."""
    plural = plural or noun + ("es" if noun.endswith(("sh", "ch", "s", "x")) else "s")
    return f"{number} {noun if number == 1 else plural}"


def landed_changes(exporter: object, systems: dict) -> LandedChanges:
    """The changes the written file carries.

    Only what the format states: a correction to a field the file has no
    place for, or one its render keeps out, is not a change the maintainer
    will find in the diff, and counting it would announce work the export
    did not do. A hash written where the platform left none changes the
    check from existence to content, so it is counted too. The truth taking
    over an entry also rewrites its size and drops the platform's hashes it
    does not declare: both are in the diff.
    """
    changes = LandedChanges()
    for system in systems.values():
        for entry in system.files:
            label = f"{entry.native_system}/{entry.name}"
            for field_name in entry.corrections:
                if not exporter.states(entry, field_name):
                    continue
                if field_name == "required":
                    changes.requirements += 1
                else:
                    changes.applied.append(f"{label} {field_name}")
            changes.filled.extend(
                f"{label} {field_name}"
                for field_name in entry.filled
                if exporter.fills(entry, field_name)
            )
            if entry.platform is None:
                continue
            theirs = entry.platform.get("size")
            ours = entry.size()
            if (
                theirs
                and ours
                and _single_size(theirs) not in (None, ours)
                and exporter.states(entry, "size")
            ):
                changes.resized.append(label)
            elif (
                theirs
                and ours is None
                and _single_size(theirs) is not None
                and exporter.states(entry, "size")
            ):
                # The truth took the entry over without a size of its own:
                # System.dat's neocdz.zip line went out without size 953078.
                changes.unsized.append(label)
            changes.cleared.extend(
                f"{label} {field_name}"
                for field_name in HASH_FIELDS
                if _hash_values(entry.platform, field_name)
                and not entry.hashes(field_name)
                and exporter.clears(entry, field_name)
            )
    # A value the render rewrote though the model reads it as the
    # platform's own (RomM's sha1 with a leading space).
    changes.applied.extend(getattr(exporter, "normalized", []))
    return changes


def export_platform(
    platform: str,
    exporter_class: type,
    truth_dir: Path,
    output_dir: Path,
    platforms_dir: str,
    upstream_dir: Path,
    allow_fetch: bool,
) -> tuple[bool, list[str]]:
    """Write one platform's corrected file. Returns (ok, messages)."""
    messages: list[str] = []

    try:
        truth, scraped = load_inputs(platform, truth_dir, platforms_dir)
    except StaleTruth as exc:
        return False, [str(exc)]
    if truth is None:
        # Without a truth the export is the platform's file copied back, and
        # it replaced the corrected one in exports/ on a fresh clone.
        return False, [
            f"{truth_dir / f'{platform}.yml'} is absent: run "
            f"python scripts/generate_truth.py --platform {platform}"
        ]

    systems, report = build_native_model(truth, scraped)
    if not systems:
        return False, ["nothing to write: neither the platform nor the truth has data"]

    exporter = exporter_class()
    off_pin = unpinned(wanted_sources(exporter, systems, scraped), scraped)
    if off_pin:
        return False, off_pin
    originals, missing = collect_originals(
        exporter, systems, upstream_dir, allow_fetch, scraped
    )
    if missing:
        return False, [f"the platform's own file is required: {m}" for m in missing]

    try:
        produced = exporter.render(systems, report, originals, scraped)
    except ValueError as exc:
        return False, [str(exc)]

    if not produced and not exporter.may_write_nothing():
        return False, ["the exporter produced no file"]

    issues = exporter.validate(systems, produced)
    if issues:
        # Written first, a file its own validator refused replaced the
        # published export and only the exit code said so.
        invalid = [f"INVALID: {issue}" for issue in issues[:10]]
        if len(issues) > 10:
            invalid.append(f"and {len(issues) - 10} more validation failures")
        return False, invalid

    platform_dir = output_dir / platform
    _remove_stale(platform_dir, set(produced))
    for relative, content in produced.items():
        path = platform_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    changes = landed_changes(exporter, systems)
    applied, filled = changes.applied, changes.filled
    resized, cleared = changes.resized, changes.cleared
    unsized = changes.unsized
    requirements = changes.requirements

    landed = 0
    refused = 0
    lost = 0
    for system in systems.values():
        for entry in system.files:
            if not entry.name:
                continue
            if exporter.lands(entry):
                landed += entry.platform is None
            elif entry.platform is None:
                refused += 1
            else:
                # The platform declares it and the format still cannot state
                # it, so their own file loses a line. Worth saying out loud.
                lost += 1

    own_summary = exporter.outcome(systems, produced)
    summary = own_summary
    if summary is None:
        summary = (
            f"{report.files_kept} kept, {landed} added, "
            f"{_count(len(applied), 'hash')} corrected, "
            f"{_count(requirements, 'requirement')} corrected"
        )
        if filled:
            summary += f", {_count(len(filled), 'hash')} filled"
        if resized:
            summary += f", {_count(len(resized), 'size')} corrected"
        if unsized:
            summary += f", {_count(len(unsized), 'size')} removed"
        if cleared:
            summary += f", {_count(len(cleared), 'hash')} cleared"
        if refused:
            summary += f", {_count(refused, 'addition')} left out"
        if lost:
            summary += f", {_count(lost, 'platform entry', 'platform entries')} the format cannot keep"
    withheld = exporter.withheld()
    if withheld:
        summary += f", {_count(len(withheld), 'correction')} withheld"
    if report.labels:
        # A content-scanning core's names are labels: a list checked by path
        # cannot hold them.
        summary += f", {_count(report.labels, 'entry', 'entries')} found by content not listed"
    messages.append(summary)
    messages.extend(f"withheld: {reason}" for reason in withheld)
    for correction in applied[:5]:
        messages.append(f"hash corrected: {correction}")
    if len(applied) > 5:
        messages.append(f"and {_count(len(applied) - 5, 'more hash correction')}")
    for fill in filled[:5]:
        messages.append(f"hash filled: {fill}")
    if len(filled) > 5:
        messages.append(f"and {_count(len(filled) - 5, 'more hash fill')}")
    for label in resized[:5]:
        messages.append(f"size corrected: {label}")
    if len(resized) > 5:
        messages.append(f"and {_count(len(resized) - 5, 'more size correction')}")
    for label in unsized[:5]:
        messages.append(f"size removed: {label}")
    if len(unsized) > 5:
        messages.append(f"and {len(unsized) - 5} more removed")
    for clear in cleared[:5]:
        messages.append(f"hash cleared: {clear}")
    if len(cleared) > 5:
        messages.append(f"and {len(cleared) - 5} more cleared")

    write_report(output_dir / f"{platform}.json", {
        "platform": platform,
        "originals": wanted_sources(exporter, systems, scraped),
        "files": sorted(produced),
        "summary": summary,
        "counts": None if own_summary is not None else {
            "kept": report.files_kept,
            "added": landed,
            "not_added": refused,
            "not_kept": lost,
        },
        "hashes_corrected": applied,
        "hashes_filled": filled,
        "sizes_corrected": resized,
        "sizes_removed": unsized,
        "hashes_cleared": cleared,
        "requirements_corrected": requirements,
        "withheld": list(withheld),
        "found_by_content": report.labels,
        "invalid": issues,
    })
    return True, messages


def write_report(path: Path, report: dict) -> None:
    """What one platform's export changed, for the page that publishes it.

    The run prints five examples per kind of change. The page lists them
    all, so the maintainer reading it finds each line of the diff named.
    A platform with nothing to write made no directory of its own: the
    output directory itself may not exist yet.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(str(path), json.dumps(report, indent=2) + "\n")


def run(
    platforms: list[str],
    truth_dir: str,
    output_dir: str,
    platforms_dir: str,
    upstream_dir: str,
    allow_fetch: bool,
    refresh_only: bool = False,
) -> int:
    exporters = discover_exporters()
    failures = 0
    skipped: list[str] = []

    for platform in sorted(platforms):
        exporter_class = exporters.get(platform)
        if not exporter_class:
            skipped.append(platform)
            print(f"  SKIP {platform}: no exporter")
            continue

        if refresh_only:
            ok, messages = refresh_cache(
                platform,
                exporter_class,
                Path(truth_dir),
                platforms_dir,
                Path(upstream_dir),
            )
        else:
            ok, messages = export_platform(
                platform,
                exporter_class,
                Path(truth_dir),
                Path(output_dir),
                platforms_dir,
                Path(upstream_dir),
                allow_fetch,
            )
        label = "OK  " if ok else "FAIL"
        print(f"  {label} {platform}")
        for message in messages:
            print(f"       {message}")
        if not ok:
            failures += 1

    if skipped:
        print(f"\n{len(skipped)} platform(s) without an exporter: {', '.join(skipped)}")
        failures += len(skipped)

    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Rewrite each platform's own BIOS file, corrected.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="every platform")
    group.add_argument("--platform", type=platform_arg, help="a single platform")
    parser.add_argument(
        "--output-dir", default="exports",
        help="directory the updated platform files go to (default: %(default)s)",
    )
    parser.add_argument(
        "--truth-dir", default="dist/truth",
        help="directory of the truth models generate_truth writes (default: "
             "%(default)s)",
    )
    parser.add_argument(
        "--platforms-dir", default="platforms",
        help="platform YAML directory (default: %(default)s)",
    )
    parser.add_argument(
        "--upstream-dir",
        default=DEFAULT_CACHE,
        help="where the platforms' own files are read and cached",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="download a platform's file when it is not in the cache, "
        "or when the cache came from another URL",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="download every platform file again and write no export",
    )
    args = parser.parse_args()

    if args.all:
        platforms = list_registered_platforms(
            args.platforms_dir,
            include_archived=True,
        )
    else:
        require_platform(args.platform, args.platforms_dir)
        platforms = [args.platform]

    sys.exit(
        run(
            platforms,
            args.truth_dir,
            args.output_dir,
            args.platforms_dir,
            args.upstream_dir,
            args.fetch,
            args.refresh_cache,
        )
    )


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
