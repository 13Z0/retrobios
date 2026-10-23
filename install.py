#!/usr/bin/env python3
"""Universal BIOS installer for retrogaming platforms.

Self-contained script using only Python stdlib. Downloads missing BIOS files
from the retrobios repository and places them in the correct location for
the detected emulator platform.

Usage:
    python install.py
    python install.py --platform retroarch --dest ~/custom/bios
    python install.py --check
    python install.py --list-platforms
    python install.py --platform retroarch --system sony-playstation --region us
"""
from __future__ import annotations

import argparse
import codecs
import concurrent.futures
import contextlib
import datetime
import errno
import functools
import hashlib
import http.client
import io
import json
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, ContextManager, NamedTuple, TextIO

# Manifests are read from the same ref the bootstrap verified this installer
# against, so a file list and the code reading it always come from one commit.
# RETROBIOS_REF pins an installation to a tag when reproducibility matters.
DEFAULT_RELEASE_REF = "main"
RELEASE_REF = os.environ.get("RETROBIOS_REF", DEFAULT_RELEASE_REF)
DEFAULT_BASE_URL = (
    "https://raw.githubusercontent.com/Abdess/retrobios/"
    + urllib.parse.quote(RELEASE_REF, safe="")
)


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _checked_base_url(value: str) -> str:
    """Refuse a base URL that is neither HTTPS nor loopback.

    This base serves the manifest and the files it declares, so whoever
    controls it controls the expected hashes too and verification stops
    proving anything. install.sh and install.ps1 make the same check on the
    URL they fetch the installer from.

    Plain HTTP to loopback stays allowed: nothing sits between the two ends
    to intercept it, and it is how the installer is exercised end to end.
    """
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "https":
        return value
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        return value
    print(
        f"Error: RETROBIOS_BASE_URL must use HTTPS, got {value!r}",
        file=sys.stderr,
    )
    sys.exit(1)


BASE_URL = _checked_base_url(os.environ.get("RETROBIOS_BASE_URL", DEFAULT_BASE_URL))
MANIFEST_URL = f"{BASE_URL}/install/{{platform}}.json"
TARGETS_URL = f"{BASE_URL}/install/targets/{{platform}}.json"
RAW_FILE_URL = f"{BASE_URL}/{{path}}"
RELEASE_URL = (
    "https://github.com/Abdess/retrobios/releases/download/large-files/{asset}"
)
MAX_RETRIES = 3
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TARGETS_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_FILES = 100_000
MAX_DOWNLOAD_SIZE = 1024 * 1024 * 1024
# A data directory's source archive: PCSX2's source tarball is the largest.
MAX_DATA_ARCHIVE_SIZE = 4 * 1024 * 1024 * 1024
MAX_TOTAL_DOWNLOAD_SIZE = 64 * 1024 * 1024 * 1024
# Why a manifest leaves a file out, as the installer words it. The manifest
# schema and the generator are held to this list by the tests.
OMISSION_REASONS = {
    "hash_mismatch": "hash mismatch",
    "not_found": "not found",
    "external": "external",
    "user_provided": "user provided",
    "no_platform_slug": "no platform folder",
    "below_platform_folder": "below the platform folder",
    "no_save_directory": "no save directory",
    "no_core_folder": "no core folder",
    "no_save_system": "no system runs the core",
}
# What a file is when it is not firmware, as the manifest's category field
# says, and as the installer words it. Firmware carries no category.
GAME_DATA = "game_data"
CATEGORY_LABELS = {GAME_DATA: "game data"}
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# The region vocabulary of scripts/region.py, which this file cannot import.
# A test holds the two copies equal.
WORLD_REGION = "world"
REGION_TREE: dict[str, frozenset] = {
    "north-america": frozenset({"canada"}),
    "latin-america": frozenset({"brazil", "mexico", "argentina"}),
    "europe": frozenset({
        "uk", "france", "germany", "italy", "spain", "netherlands", "portugal",
        "greece", "poland", "russia", "sweden", "norway", "denmark", "finland",
    }),
    "asia": frozenset({
        "japan", "south-korea", "china", "taiwan", "hong-kong", "singapore",
        "india", "asia-ntsc", "asia-pal",
    }),
    "oceania": frozenset({"australia", "new-zealand"}),
}
REGION_ALIASES: dict[str, str] = {
    "jp": "japan",
    "ntsc-j": "japan",
    "us": "north-america",
    "usa": "north-america",
    "na": "north-america",
    "ntsc-u": "north-america",
    "eu": "europe",
    "pal": "europe",
    "kr": "south-korea",
    "korea": "south-korea",
    "auto": WORLD_REGION,
    "gb": "uk",
    "ca": "canada",
    "br": "brazil",
    "mx": "mexico",
    "ar": "argentina",
    "fr": "france",
    "de": "germany",
    "it": "italy",
    "es": "spain",
    "nl": "netherlands",
    "pt": "portugal",
    "gr": "greece",
    "pl": "poland",
    "ru": "russia",
    "se": "sweden",
    "no": "norway",
    "dk": "denmark",
    "fi": "finland",
    "cn": "china",
    "tw": "taiwan",
    "hk": "hong-kong",
    "sg": "singapore",
    "in": "india",
    "au": "australia",
    "nz": "new-zealand",
}
AREA_TERRITORIES: dict[str, frozenset] = {
    "asia-ntsc": frozenset({"taiwan"}),
    "asia-pal": frozenset({"china"}),
}
_REGION_PARENT = {
    member: parent for parent, members in REGION_TREE.items() for member in members
}
REGIONS = frozenset({WORLD_REGION} | set(REGION_TREE) | set(_REGION_PARENT))
# Marks a region group named after one file that no system names.
FILE_GROUP = ":file:"
MAX_SELECTION_TAGS = 1000
SELECTION_FIELDS = ("systems", "regions", "region_groups", "region_system_groups")
# The files cores load for systems the platform does not list (game engines,
# computers): no system of the platform owns them, and choosing systems
# leaves them out unless this name is chosen too.
OTHER_SYSTEMS = "other"

# Platforms with a manifest in install/. Manifest URLs are case sensitive,
# so user input is normalized against this list before any fetch.
AVAILABLE_PLATFORMS = (
    "retroarch", "batocera", "recalbox", "retrobat", "emudeck",
    "lakka", "retrodeck", "rocknix", "romm", "bizhawk", "misterfpga",
    "retropie", "amberelec", "emuelec", "knulli",
    "minui", "nextui", "myminui",
)

# The names the frontends give themselves, as each manifest's display_name
# spells them (a test holds the two equal). Detection and the platform menu
# come before any manifest is fetched, and str.capitalize() wrote Retroarch,
# Retrodeck and Misterfpga.
PLATFORM_NAMES = {
    "retroarch": "RetroArch", "batocera": "Batocera", "recalbox": "Recalbox",
    "retrobat": "RetroBat", "emudeck": "EmuDeck", "lakka": "Lakka",
    "retrodeck": "RetroDECK", "rocknix": "ROCKNIX", "romm": "RomM",
    "bizhawk": "BizHawk", "misterfpga": "MiSTer FPGA", "retropie": "RetroPie",
    "amberelec": "AmberELEC", "emuelec": "EmuELEC", "knulli": "KNULLI",
    "minui": "MinUI", "nextui": "NextUI", "myminui": "MyMinUI",
}

# Frontends whose BIOS live on their SD card, installed from a computer the
# card is mounted on. Each release unpacks as .system/ at the card root and
# writes its release name as the first line of .system/version.txt (MinUI
# makefile:22-24 and 152-157, NextUI makefile:33-35 and 249, MyMinUI
# makefile:16-19 and 124). The registry publishes the same rules as each
# manifest's `detect` (card_marker), and a test holds the two equal.
CARD_MARKER = ".system/version.txt"
_CARD_RULES: tuple[tuple[str, str, str], ...] = (
    ("minui", CARD_MARKER, "MinUI-"),
    ("nextui", CARD_MARKER, "NextUI-"),
    ("myminui", CARD_MARKER, "MyMinUI-"),
)
CARD_PLATFORMS = frozenset(name for name, _file, _prefix in _CARD_RULES)

SITE_URL = "https://abdess.github.io/retrobios/"
INSTALLER_PAGE = f"{SITE_URL}wiki/installer/"
TROUBLESHOOTING_PAGE = f"{SITE_URL}wiki/troubleshooting/"
BOOTSTRAP_URL = "https://raw.githubusercontent.com/Abdess/retrobios/main/install"

# Fallback BIOS destination per platform when --platform is forced
# but auto-detection finds nothing on the machine.
DEFAULT_DESTS = {
    "batocera": Path("/userdata/bios"),
    "recalbox": Path("/recalbox/share/bios"),
    "lakka": Path("/storage/system"),
    "retrodeck": Path.home() / "retrodeck",
    "emudeck": Path.home() / "Emulation" / "bios",
    "rocknix": Path("/storage/roms/bios"),
    # Their BIOS lists name files under /storage/roms beside bios/.
    "amberelec": Path("/storage/roms"),
    "emuelec": Path("/storage/roms"),
    "knulli": Path("/userdata/bios"),
    "misterfpga": Path("/media/fat/games"),
    "retropie": Path.home() / "RetroPie" / "BIOS",
    "romm": Path("/romm/library/bios"),
}


def _os_default_dests(os_type: str) -> dict[str, Path]:
    """Where a platform keeps its BIOS on this OS, when it is not detected.

    The same folders the platform's detection looks for: a platform named on
    the command line but not found must not land in ~/bios, which none of
    them reads.
    """
    home = Path.home()
    if os_type == "windows":
        return {
            # RetroArch's system directory defaults to <exe folder>\system
            # (platform_win32.c), and its installer to C:\RetroArch-Win64.
            "retroarch": _host_path("C:/RetroArch-Win64/system"),
            # RetroBat's installer defaults to C:\RetroBat (installer.iss).
            "retrobat": _host_path("C:/RetroBat/bios"),
            # EmuDeck's Windows edition: C:\Emulation when its settings name
            # no folder (emudeck-we functions/all.ps1:12-14).
            "emudeck": _host_path("C:/Emulation/bios"),
        }
    if os_type == "wsl":
        # RetroArch may be the Linux one inside the distribution, while RetroBat
        # only exists on the Windows side, reached through /mnt/c.
        return {
            "retroarch": home / ".config" / "retroarch" / "system",
            "retrobat": _host_path("C:/RetroBat/bios"),
        }
    if os_type == "darwin":
        return {"retroarch": _macos_retroarch_base() / "system"}
    return {
        "retroarch": home / ".config" / "retroarch" / "system",
    }

# Set when the run is interrupted, so queued downloads end instead of draining
# the pool: every future is submitted before the first one completes.
_stop = threading.Event()
# Set once a run starts writing: Ctrl+C before that has nothing to carry on.
_writing = threading.Event()

class _Interrupted(Exception):
    """Raised inside a worker once the run has been stopped."""


# A file this size or larger keeps its partial download when a transfer
# stops, and the next run asks the server for the rest: the release assets
# run to hundreds of megabytes. Smaller files simply start over.
RESUME_FROM_SIZE = 16 * 1024 * 1024
# Read once, before any download thread runs: reading the umask sets it.
_UMASK = os.umask(0)
os.umask(_UMASK)


def _enable_ansi() -> bool:
    """Whether escape sequences reach the terminal as colour and redraws.

    Windows 10 consoles interpret them once virtual terminal processing is
    switched on. Older ones print them raw, so the answer is then no.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes  # noqa: PLC0415

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


class Terminal:
    """What the output stream can show: a live line, colour, or neither.

    A pipe or a log gets plain lines, and NO_COLOR and TERM=dumb are honoured.
    As no-color.org reads it, an empty NO_COLOR is not set: `NO_COLOR= cmd`
    turned colour off. FORCE_COLOR or CLICOLOR_FORCE paints a pipe, for a
    log viewer that shows colour, and never makes the line live.
    """

    def __init__(self, stream: TextIO | None = None, quiet: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.quiet = quiet
        isatty = getattr(self.stream, "isatty", None)
        self.live = bool(isatty and isatty()) and os.environ.get("TERM") != "dumb"
        forced = any(os.environ.get(name, "0") not in ("", "0")
                     for name in ("FORCE_COLOR", "CLICOLOR_FORCE"))
        self.colour = (
            (self.live or forced) and not os.environ.get("NO_COLOR") and _enable_ansi()
        )
        if not self.colour:
            self.live = self.live and os.name != "nt"

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour else text

    def ok(self, text: str) -> str:
        return self.paint(text, "32")

    def bad(self, text: str) -> str:
        return self.paint(text, "31")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def bold(self, text: str) -> str:
        return self.paint(text, "1")


def format_duration(seconds: float) -> str:
    """0:42, 3:05 or 1:02:09."""
    seconds = max(0, int(seconds + 0.5))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


class Progress:
    """Bytes and files across the parallel downloads, shown as they move.

    On a terminal one line redraws in place: a bar, the bytes, the speed and
    the time left. Elsewhere each finished file gets its line, as a log
    wants it. Failures are always printed, quiet or not.
    """

    BAR = 24

    def __init__(self, total_files: int, total_bytes: int, term: Terminal) -> None:
        self.total_files = total_files
        self.expected_bytes = total_bytes
        self.total_bytes = max(total_bytes, 1)
        self.term = term
        self.files = 0
        self.bytes = 0
        self.resumed = 0
        # Files written to their place, which a stopped run counts: the
        # queue's length said "118 files installed" over 8 on disk.
        self.installed = 0
        self.start = time.monotonic()
        self._drawn = 0.0
        self._evented = self.start
        self._lock = threading.Lock()

    def add(self, count: int, resumed: bool = False) -> None:
        with self._lock:
            self.bytes += count
            if resumed:
                self.resumed += count
            now = time.monotonic()
            if self.term.live and not self.term.quiet and now - self._drawn >= 0.1:
                self._drawn = now
                self._draw()
            if EVENTS.on and now - self._evented >= EVENT_PROGRESS_INTERVAL:
                self._evented = now
                EVENTS.emit("progress", files_done=self.files, files_total=self.total_files,
                            bytes_done=self.bytes, bytes_total=self.expected_bytes)

    def forget_resumed(self, count: int) -> None:
        with self._lock:
            self.resumed -= count

    def transferred(self) -> int:
        """Bytes fetched by this run, a resumed partial copy excluded."""
        return self.bytes - self.resumed

    def done(self, dest: str, ok: bool, reason: str = "") -> None:
        with self._lock:
            self.files += 1
            line = None
            if not ok and not self.term.quiet:
                why = f" ({reason})" if reason else ""
                line = f"  {self.term.bad('FAILED')} {dest}{why}"
            elif not self.term.live and not self.term.quiet:
                line = f"  [{self.files}/{self.total_files}] {dest} {self.term.ok('ok')}"
            if line is not None:
                self._clear()
                print(line, file=self.term.stream, flush=True)
            if self.term.live and not self.term.quiet:
                self._draw()

    def finish(self) -> None:
        with self._lock:
            self._clear()

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def _clear(self) -> None:
        if self.term.live and not self.term.quiet:
            print("\r\033[K", end="", file=self.term.stream, flush=True)

    def _draw(self) -> None:
        share = min(self.bytes / self.total_bytes, 1.0)
        elapsed = self.elapsed()
        # A resumed partial copy was on disk before the run started: counted
        # in the share, never in the speed.
        speed = self.transferred() / elapsed if elapsed > 0 else 0
        left = (self.total_bytes - self.bytes) / speed if speed > 0 else 0
        parts = [
            f"{share * 100:5.1f}%",
            f"{format_size(self.bytes)} / {format_size(self.total_bytes)}",
            f"{format_size(int(speed))}/s",
            f"{format_duration(left)} left",
            f"({self.files}/{self.total_files} files)",
        ]
        # One physical line, whatever the width: \r\033[K clears only the
        # line the cursor is on, so a wrapped line left a ghost per redraw
        # on an 80-column console. The bar gives way first, then the least
        # telling figures.
        width = shutil.get_terminal_size((80, 24)).columns - 1
        while True:
            tail = "  ".join(parts)
            room = width - len(tail) - 6
            if room >= 10 or len(parts) == 1:
                break
            parts.pop()
        if room >= 10:
            bar_width = min(self.BAR, room)
            filled = int(share * bar_width)
            text = f"  [{'#' * filled}{'-' * (bar_width - filled)}] {tail}"
        else:
            text = f"  {tail}"
        print(f"\r\033[K{text[:width]}", end="", file=self.term.stream, flush=True)


# --events: one JSON object per line on stdout, for a program driving the
# installer (the handheld port shows its screens from them). Each kind has
# exactly these keys besides "event", in this order, and nothing else.
EVENT_SCHEMA = 1
EVENT_KEYS: dict[str, tuple[str, ...]] = {
    # mode: install, check, dry-run or list.
    "start": ("schema", "installer", "mode"),
    # platforms: [{"platform", "name", "bios_dir"}], empty when none is found.
    "detect": ("os", "os_name", "platforms"),
    # stage: manifest (reason: network, not_found, invalid, clock for a
    # refused certificate under a clock set in the past, or no_ca for no
    # trusted certificates on the machine) or detect (reason: conflict, two
    # systems found on one machine) or from (reason: not_found or
    # not_a_folder, a folder named with --from).
    "error": ("stage", "reason", "platform", "message"),
    # state: present (hash verified), wrong_hash or missing.
    "file": ("platform", "dest", "systems", "size", "state"),
    "system": ("platform", "system", "name", "files", "present", "missing",
               "wrong_hash", "bytes_needed"),
    # The files in place being read, at most every half second, and the last.
    "checking": ("files_done", "files_total"),
    "check": ("platform", "name", "bios_dir", "files", "present", "missing",
              "wrong_hash", "bytes_needed", "data_missing", "not_installable"),
    # A file to install that this machine already holds: misplaced in the
    # BIOS folder (folder is its bios_dir, reason says why in words) or in
    # a folder named with --from (reason empty). member names the ZIP member
    # when path is an archive, else "". Its copy is a downloaded event.
    "found": ("platform", "dest", "folder", "path", "member", "reason"),
    # What a folder named with --from holds that could not be read, of the
    # size of a file no copy was found for, or of no known size (a folder,
    # a ZIP that would not open). member as in found, reason in words.
    "unreadable": ("platform", "folder", "path", "member", "reason"),
    "download": ("platform", "files", "bytes"),
    "progress": ("files_done", "files_total", "bytes_done", "bytes_total"),
    "downloaded": ("dest", "ok", "reason", "size", "files_done", "files_total",
                   "bytes_done", "bytes_total"),
    # failed: [{"dest", "reason"}]. skipped: [{"reason", "label", "files",
    # "required"}], the files the collection cannot supply. left_out:
    # [{"category", "label", "files", "dests"}], what --no-game-data left
    # out, empty without it. kept: [{"folder", "files", "bytes"}], the files
    # in place the run moved aside before replacing them (under --dry-run,
    # would move), one per backup folder, empty when none.
    "summary": ("mode", "installed", "up_to_date", "pending", "failed", "skipped",
                "left_out", "kept", "interrupted"),
    "end": ("code",),
}
# Seconds between two progress events while bytes arrive.
EVENT_PROGRESS_INTERVAL = 0.5


class EventStream:
    """The --events output: closed (nothing written) until opened."""

    def __init__(self) -> None:
        self.stream: TextIO | None = None
        self._lock = threading.Lock()

    def open(self, stream: TextIO) -> None:
        self.stream = stream

    @property
    def on(self) -> bool:
        return self.stream is not None

    def emit(self, kind: str, **fields: object) -> None:
        if self.stream is None:
            return
        keys = EVENT_KEYS[kind]
        if set(fields) != set(keys):
            raise ValueError(f"event {kind}: keys {sorted(fields)} != {sorted(keys)}")
        line = json.dumps({"event": kind, **{key: fields[key] for key in keys}})
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()


EVENTS = EventStream()


def _event_mode(args: argparse.Namespace) -> str:
    if args.check:
        return "check"
    if args.dry_run:
        return "dry-run"
    if (args.list_platforms or args.list_targets or args.list_systems
            or args.list_cores or args.list_emulators):
        return "list"
    return "install"


def _event_exit_code(code: object) -> int:
    """The process status sys.exit(code) gives."""
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def _os_name() -> str:
    """The OS's own name, from os-release, else the kernel's."""
    release = _parse_os_release()
    return release.get("PRETTY_NAME") or release.get("NAME") or platform.system()


def _emit_check(
    plat: str, manifest: dict, bios_path: Path, files: list[dict], up_to_date: list[dict],
    mismatched: list[dict], data_missing: int, not_installable: int,
    chosen: list[str] | None = None,
) -> None:
    """One event per file, per system and per platform after a check.

    With systems chosen, only those are described: a file of SkyEmu's
    carries four systems, and --system nintendo-gba described gb, gbc and
    ds by the few files the choice kept of them.
    """
    present = {id(entry) for entry in up_to_date}
    wrong = {id(entry) for entry in mismatched}
    systems: dict[str, dict[str, int]] = {}
    for entry in files:
        state = ("present" if id(entry) in present
                 else "wrong_hash" if id(entry) in wrong else "missing")
        size = entry.get("size", 0)
        EVENTS.emit("file", platform=plat, dest=entry["dest"],
                    systems=entry_systems(entry), size=size, state=state)
        for system in entry_systems(entry) or [OTHER_SYSTEMS]:
            counts = systems.setdefault(system, {
                "files": 0, "present": 0, "missing": 0, "wrong_hash": 0,
                "bytes_needed": 0,
            })
            counts["files"] += 1
            counts[state] += 1
            if state != "present":
                counts["bytes_needed"] += size
    names = manifest.get("system_names") or {}
    for system in sorted(systems):
        if chosen and system not in chosen:
            continue
        EVENTS.emit("system", platform=plat, system=system,
                    name=", ".join(names.get(system) or [system]), **systems[system])
    needed = [entry for entry in files if id(entry) not in present]
    EVENTS.emit(
        "check", platform=plat, name=display_name(plat, manifest), bios_dir=str(bios_path),
        files=len(files), present=len(up_to_date), missing=len(needed) - len(mismatched),
        wrong_hash=len(mismatched),
        bytes_needed=sum(entry.get("size", 0) for entry in needed),
        data_missing=data_missing, not_installable=not_installable,
    )


def leave_out_game_data(
    files: list[dict], omitted: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """The files and omissions without game data, and what was left out.

    The same cut as a pack built with --no-game-data: the manifest marks
    each file that is a game's data, not firmware.
    """
    left_out = [e for e in files + omitted if e.get("category") == GAME_DATA]
    return (
        [e for e in files if e.get("category") != GAME_DATA],
        [e for e in omitted if e.get("category") != GAME_DATA],
        left_out,
    )


def _left_out_lines(left_out: list[dict], every: bool) -> list[str]:
    """What --no-game-data left out, named per category, ten each unless every."""
    lines = []
    for group in _left_out_events(left_out):
        dests = group["dests"]
        shown = dests if every else dests[:10]
        more = (f" ... and {len(dests) - len(shown)} more (--verbose lists them all)"
                if len(shown) < len(dests) else "")
        lines.append(f"  Left out as {group['label']} (--no-game-data): "
                     f"{_plural(len(dests), 'file')}: {', '.join(shown)}{more}")
    return lines


def _left_out_events(left_out: list[dict]) -> list[dict]:
    """What --no-game-data left out, per category, every file named."""
    groups: dict[str, dict] = {}
    for entry in left_out:
        category = entry.get("category", "")
        group = groups.setdefault(category, {
            "category": category, "label": CATEGORY_LABELS.get(category, category),
            "files": 0, "dests": [],
        })
        group["files"] += 1
        group["dests"].append(entry["dest"])
    return [groups[category] for category in sorted(groups)]


def _skipped(omitted: list[dict]) -> list[dict]:
    """The files the collection cannot supply, grouped by reason."""
    groups: dict[str, dict] = {}
    for entry in omitted:
        reason = entry.get("reason", "unknown")
        group = groups.setdefault(reason, {
            "reason": reason, "label": OMISSION_REASONS.get(reason, reason),
            "files": 0, "required": 0,
        })
        group["files"] += 1
        group["required"] += bool(entry.get("required", True))
    return [groups[reason] for reason in sorted(groups)]


# The three RetroArch packages published for Android, newest ABI first.
ANDROID_RETROARCH_PACKAGES = (
    "com.retroarch.aarch64",
    "com.retroarch",
    "com.retroarch.ra32",
)
# Where Android keeps each app's private data.
ANDROID_APP_DATA = Path("/data/data")


def _is_android() -> bool:
    """Android reports itself as Linux, so the runtime is named instead.

    Termux inherits the two variables the Android runtime exports, and
    /system/build.prop covers a shell that was started without them.
    """
    if os.environ.get("ANDROID_ROOT") and os.environ.get("ANDROID_DATA"):
        return True
    return Path("/system/build.prop").exists()


def detect_os() -> str:
    """Return normalized OS identifier.

    RETROBIOS_OS names the platform outright (linux, wsl, windows, darwin,
    android), for a run that has to behave like another host: the PowerShell
    wrapper tests drive a Windows layout on a Linux runner.
    """
    forced = os.environ.get("RETROBIOS_OS", "").strip().lower()
    if forced in ("linux", "wsl", "windows", "darwin", "android"):
        return forced
    system = platform.system().lower()
    if system == "linux":
        if _is_android():
            return "android"
        proc_version = Path("/proc/version")
        if proc_version.exists():
            try:
                content = proc_version.read_text(encoding="utf-8", errors="replace")
                if "microsoft" in content.lower():
                    return "wsl"
            except OSError:
                pass
        return "linux"
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "windows"
    return system


def _parse_os_release() -> dict[str, str]:
    """Parse /etc/os-release KEY=value format."""
    result: dict[str, str] = {}
    path = _on_device("/etc/os-release")
    if not path.exists():
        return result
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            value = value.strip('"').strip("'")
            result[key] = value
    except OSError:
        pass
    return result


# Set under WSL: a Windows path read from a config or a rule names a drive
# that Linux sees under /mnt (drvfs). C:\\RetroBat\\bios was used as a relative
# path and nothing Windows-side was ever found from WSL.
_WSL = {"active": False, "root": "/mnt"}
_WINDOWS_VARIABLES = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")


def _host_path(text: str) -> Path:
    """A path as this machine opens it: under WSL, C:\\X is /mnt/c/X."""
    match = re.match(r"^([A-Za-z]):(?:[\\/]+(.*))?$", text) if _WSL["active"] else None
    if match:
        rest = [part for part in re.split(r"[\\/]", match.group(2) or "") if part]
        return Path(_WSL["root"], match.group(1).lower(), *rest)
    return Path(text)


def _enter_wsl() -> None:
    """Read Windows paths through drvfs, and the Windows variables detection
    needs, which WSL does not pass to Linux (only those named in WSLENV)."""
    _WSL["active"] = True
    for variable in _WINDOWS_VARIABLES:
        if os.environ.get(variable):
            continue
        try:
            # No stdin: cmd.exe inherited the terminal and swallowed the
            # answer typed at the first prompt.
            result = subprocess.run(
                ["cmd.exe", "/c", f"echo %{variable}%"],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
            )
        except (OSError, subprocess.SubprocessError):
            return
        value = result.stdout.strip()
        if value and not value.startswith("%"):
            os.environ[variable] = str(_host_path(value))


def _expand_retroarch_path(value: str, app_dir: Path, home: Path | None = None) -> Path:
    """Expand the notations RetroArch writes into its config values.

    fill_pathname_expand_special (libretro-common/file/file_path.c) maps a
    leading '~' to the home directory and a leading ':' to the application
    directory, dropping the two leading characters in both cases.
    """
    if value[:1] == "~":
        return (home or Path(str(Path.home()))) / value[2:]
    if value[:1] == ":":
        return app_dir / value[2:]
    return _host_path(os.path.expandvars(os.path.expanduser(value)))


def _retroarch_cfg_dir(
    cfg_path: Path, key: str, app_dir: Path, default: str, home: Path | None = None
) -> Path | None:
    """A directory setting of retroarch.cfg, as RetroArch expands it.

    A value left empty or at `default` is the directory RetroArch falls back
    on, `default` under app_dir. None when the config is unreadable or does
    not set the key.
    """
    try:
        lines = cfg_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            value = value.strip().strip('"').strip("'")
            if not value or value == "default":
                return app_dir / default
            return _expand_retroarch_path(value, app_dir, home)
    return None


def _steam_libraries() -> list[Path]:
    """Every Steam library, the default one first.

    Steam lists the others in steamapps/libraryfolders.vdf, each as a
    "path" line. A RetroArch installed on D:/SteamLibrary was not found
    when only Program Files was searched.
    """
    roots: list[Path] = []
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var, "")
        if base and (Path(base) / "Steam").is_dir():
            roots.append(Path(base) / "Steam")
    libraries = list(roots)
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            library = Path(raw.replace("\\\\", "\\"))
            if library not in libraries:
                libraries.append(library)
    return libraries


def _windows_retroarch_dirs() -> list[Path]:
    """Folders holding retroarch.exe where its installers put it.

    C:/RetroArch-Win64 by default, C:/RetroArch for 32-bit builds, the same
    names under Program Files, every Steam library, the Emulators folder of
    an installed ES-DE (es_find_rules.xml lists all of them), and a Scoop
    install, user or global.
    """
    candidates = [_host_path("C:/RetroArch-Win64"), _host_path("C:/RetroArch")]
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(var, "")
        if root:
            candidates += [Path(root) / "RetroArch-Win64", Path(root) / "RetroArch"]
            candidates += [
                Path(root) / "ES-DE" / "Emulators" / name
                for name in ("RetroArch-Win64", "RetroArch")
            ]
    candidates += [
        library / "steamapps" / "common" / "RetroArch" for library in _steam_libraries()
    ]
    # Scoop's Extras bucket: <root>\apps\retroarch\current, the root being
    # %SCOOP% or ~\scoop for a user install, %SCOOP_GLOBAL% or
    # %ProgramData%\scoop for a global one. Its system folder is persisted
    # through a junction, so the folder beside retroarch.exe is the real one.
    scoop_roots = [
        os.environ.get("SCOOP") or (
            str(Path(os.environ["USERPROFILE"]) / "scoop") if os.environ.get("USERPROFILE") else ""
        ),
        os.environ.get("SCOOP_GLOBAL") or (
            str(Path(os.environ["ProgramData"]) / "scoop") if os.environ.get("ProgramData") else ""
        ),
    ]
    candidates += [
        Path(root) / "apps" / "retroarch" / "current" for root in scoop_roots if root
    ]
    return [folder for folder in candidates if (folder / "retroarch.exe").is_file()]


def _macos_retroarch_base() -> Path:
    """Where RetroArch on macOS keeps its system folder by default:
    ~/Documents/RetroArch, or Application Support/RetroArch for a portable
    install. The Steam build is always portable (platform_darwin.m, `#if
    HAVE_STEAM portable = YES`), with no portable.txt to say so: its
    bundle under Steam's library, and no other RetroArch.app, mark it."""
    support = Path.home() / "Library" / "Application Support" / "RetroArch"
    if (support / "portable.txt").is_file():
        return support
    steam = (
        Path.home() / "Library" / "Application Support" / "Steam" / "steamapps"
        / "common" / "RetroArch" / "RetroArch.app"
    )
    if steam.is_dir() and not Path("/Applications/RetroArch.app").is_dir():
        return support
    return Path.home() / "Documents" / "RetroArch"


# System directory -> the retroarch.cfg it was read from and the directory
# ':' expands to there, for finding the cores of the same installation.
_RETROARCH_CONFIGS: dict[Path, tuple[Path, Path]] = {}


def _parse_retroarch_system_dir(
    cfg_path: Path, app_dir: Path | None = None, home: Path | None = None
) -> Path | None:
    """Parse system_directory from retroarch.cfg.

    app_dir is RetroArch's application directory, used to expand ':' values.
    It defaults to the directory holding the config, which is where a portable
    install keeps both. home is what '~' means to that RetroArch, the user's
    home when not given.
    """
    if not cfg_path.exists():
        return None
    app_dir = app_dir or cfg_path.parent
    system_dir = _retroarch_cfg_dir(cfg_path, "system_directory", app_dir, "system", home)
    if system_dir is not None:
        _RETROARCH_CONFIGS.setdefault(system_dir, (cfg_path, app_dir))
    return system_dir


def _shell_unquote(value: str) -> str:
    """Resolve a shell right-hand side into its effective string value.

    Handles concatenated quoted/unquoted segments the way bash does
    (e.g. "/run/media/deck/EmuSD"/Emulation), expands variables in
    double-quoted and unquoted segments, and stops at unquoted
    whitespace or a comment.
    """
    parts: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == '"':
            end = value.find('"', i + 1)
            if end == -1:
                parts.append(os.path.expandvars(value[i + 1:]))
                break
            parts.append(os.path.expandvars(value[i + 1:end]))
            i = end + 1
        elif c == "'":
            end = value.find("'", i + 1)
            if end == -1:
                parts.append(value[i + 1:])
                break
            parts.append(value[i + 1:end])
            i = end + 1
        elif c.isspace() or c == "#":
            break
        else:
            end = i
            while end < n and value[end] not in "\"'#" and not value[end].isspace():
                end += 1
            parts.append(os.path.expandvars(value[i:end]))
            i = end
    return os.path.expanduser("".join(parts))


def _parse_bash_var(path: Path, key: str) -> str | None:
    """Extract the effective value of key= from a bash/shell file."""
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                _, _, value = line.partition("=")
                return _shell_unquote(value)
    except OSError:
        pass
    return None


# Where an embedded system names the board it was built for, in the words
# of our target lists: batocera-system.mk writes BATOCERA_SYSTEM_ARCH,
# recalbox-system.mk RECALBOX_SYSTEM_PLATFORM, ROCKNIX's scripts/image puts
# HW_DEVICE in os-release (RK3326, our rk3326). Lakka's scripts/image writes
# LIBREELEC_DEVICE when the image is built for a device (RPi4) and always
# LIBREELEC_PROJECT (Generic): the first one set is the board.
_BOARD_SOURCES: dict[str, tuple[str, str]] = {
    "batocera": ("file", "/usr/share/batocera/batocera.arch"),
    "recalbox": ("file", "/recalbox/recalbox.arch"),
    "rocknix": ("os_release", "HW_DEVICE"),
    "lakka": ("os_release", "LIBREELEC_DEVICE|LIBREELEC_PROJECT"),
}


# A platform that names its board without publishing a core list per board.
# Knulli moves /usr/share/batocera to /usr/share/knulli (knulli-linux
# 57afa662, board/scripts/post-build-script.sh:264-278) and writes its board
# to knulli.arch (package/system/knulli-system/knulli-system.mk:98-99).
_DERIVATIVE_BOARDS: dict[str, tuple[str, str]] = {
    "knulli": ("Knulli", "/usr/share/knulli/knulli.arch"),
}


def derivative_board(platform: str) -> tuple[str, str] | None:
    """(derivative, board) when a derivative of the platform names its board."""
    name, where = _DERIVATIVE_BOARDS.get(platform, ("", ""))
    if not where:
        return None
    try:
        board = Path(where).read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return None
    return (name, board) if board else None


def detect_board(platform: str) -> str | None:
    """The board this system was built for, or None where it names none."""
    kind, where = _BOARD_SOURCES.get(platform, ("", ""))
    if kind == "file":
        try:
            value = Path(where).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    elif kind == "os_release":
        release = _parse_os_release()
        value = next((release[key] for key in where.split("|") if release.get(key)), "")
    else:
        return None
    return value.strip().lower() or None


def _parse_json_path(path: Path, *keys: str) -> str | None:
    """Extract a nested string value from a JSON file."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        for key in keys:
            data = data[key]
        return data if isinstance(data, str) and data else None
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _parse_ps1_var(path: Path, key: str) -> str | None:
    """Extract value of $key= or key= from a PowerShell file."""
    if not path.exists():
        return None
    normalized = key.lstrip("$")
    try:
        raw = path.read_bytes()
        # Windows PowerShell's Out-File writes UTF-16, Set-Content -Encoding
        # UTF8 a byte order mark: EmuDeck's settings.ps1 passes through both.
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        for line in raw.decode(encoding, errors="replace").splitlines():
            line = line.strip()
            check = line.lstrip("$")
            if check.startswith(f"{normalized}="):
                _, _, value = check.partition("=")
                return value.strip('"').strip("'")
    except OSError:
        pass
    return None


# The root the embedded rules read under: "/" on a device, a folder holding a
# copy of one in the tests.
_SYSTEM_ROOT = Path("/")


def _on_device(path: str) -> Path:
    """A path of the running system, read under _SYSTEM_ROOT."""
    if _SYSTEM_ROOT == Path("/"):
        return Path(path)
    return _SYSTEM_ROOT / path.lstrip("/")


class _OsRule(NamedTuple):
    """One piece of evidence that names an embedded system.

    manifest is the platform whose files it gets, "" for a system the
    installer does not lay out. name is the system's own name. values are the
    KEY=value pairs of os_release (any one), the files of file_exists (all),
    or the file and the pattern of file_matches. bios is the folder used when
    none of configs (retroarch.cfg files, read in order, `*` allowed) names a
    system_directory. home is what `~` means to that system's RetroArch.
    base is the system this one is built on: both match, this one is meant.
    """

    manifest: str
    name: str
    method: str
    values: tuple[str, ...]
    bios: str | None
    configs: tuple[str, ...] = ()
    home: str = ""
    base: str = ""


# The platform whose files each of these systems gets. They publish their own
# BIOS list, and these platforms transcribe it.
_STANDIN_MANIFESTS = {
    "AmberELEC": "amberelec",
    "EmuELEC": "emuelec",
    "KNULLI": "knulli",
}

# Where the ELEC family keeps RetroArch: the config runemu writes and copies
# back on every launch, and HOME=/storage for the services that start it.
_ELEC_CONFIGS = ("/storage/.config/retroarch/retroarch.cfg",)
# ArkOS and dArkOS: the 64-bit and the 32-bit RetroArch. "Switch to SD2 for
# Roms" rewrites /roms/ to /roms2/ in both (darkos daecf488,
# scripts/Switch to SD2 for Roms.sh:91-92), so a second card is read here.
_ARK_CONFIGS = ("/home/ark/.config/retroarch/retroarch.cfg",
                "/home/ark/.config/retroarch32/retroarch.cfg")
_PLYMOUTH_TITLE = "/usr/share/plymouth/themes/text.plymouth"

# muOS runs RetroArch on the config it copies from its default on first launch
# (internal f41f4a89, script/control/retroarch.sh:8-20). The default names
# /run/muos/storage/bios, a bind mount of MUOS/bios on the second card when
# that card holds one, else on the first (script/device/bind.sh:4,116-150),
# and loads cores from /opt/muos/share/core (retroarch.default.cfg:2806,3151).
MUOS_RETROARCH_CONFIGS = (
    "/opt/muos/share/info/config/retroarch.cfg",
    "/opt/muos/share/emulator/retroarch/retroarch.default.cfg",
)

# Embedded systems, by what their own builds write. The registry publishes the
# same rules as each manifest's `detect`, and a test holds the two equal.
# MiSTer is a path rule below.
_EMBEDDED_RULES: tuple[_OsRule, ...] = (
    # ROCKNIX writes OS_NAME and no ID since 2025 (scripts/image). Older
    # images wrote ID=rocknix.
    _OsRule("rocknix", "ROCKNIX", "os_release", ("OS_NAME=rocknix", "ID=rocknix"),
            "/storage/roms/bios"),
    # Knulli (knulli-linux 57afa662): OS_NAME in os-release
    # (board/scripts/post-build-script.sh:233-236) and knulli.version
    # (package/system/knulli-system/knulli-system.mk:98-102).
    _OsRule(_STANDIN_MANIFESTS["KNULLI"], "KNULLI", "os_release", ("OS_NAME=knulli",),
            "/userdata/bios", base="Batocera"),
    _OsRule(_STANDIN_MANIFESTS["KNULLI"], "KNULLI", "file_exists",
            ("/usr/share/knulli/knulli.version",), "/userdata/bios", base="Batocera"),
    _OsRule("lakka", "Lakka", "os_release", ("ID=lakka",), "/storage/system"),
    # batocera.linux 38845725, batocera-system.mk:21-22 (the same lines in
    # batocera-30): batocera.arch and batocera.version.
    _OsRule("batocera", "Batocera", "file_exists", ("/usr/share/batocera/batocera.version",),
            "/userdata/bios"),
    _OsRule("recalbox", "Recalbox", "file_exists", ("/recalbox/recalbox.version",),
            "/recalbox/share/bios"),
    _OsRule("recalbox", "Recalbox", "file_exists", ("/usr/bin/recalbox-settings",),
            "/recalbox/share/bios"),
    _OsRule("retroarch", "muOS", "file_exists", ("/opt/muos",), "/run/muos/storage/bios",
            configs=MUOS_RETROARCH_CONFIGS),
    _OsRule("retroarch", "muOS", "file_exists", ("/mnt/mmc/MUOS",), "/mnt/mmc/MUOS/bios"),
    # AmberELEC 5a3882cb: scripts/image:150-152 writes NAME="${DISTRONAME}",
    # AmberELEC in distributions/AmberELEC/options:17. CHAos (tech4bot/AmberELEC
    # c0a4b0c6) is built from the same tree and writes the same name
    # (distributions/AmberELEC/options:14). runemu.py:22 launches with
    # /storage/.config/retroarch/retroarch.cfg, whose default is "~/roms/bios"
    # (packages/games/tools/retroarch/sources/retroarch.cfg:611), and
    # emustation.service:6 sets HOME=/storage.
    _OsRule(_STANDIN_MANIFESTS["AmberELEC"], "AmberELEC", "os_release", ("NAME=AmberELEC",),
            "/storage/roms"),
    # PAN4ELEC (Kegg1701/P4ELEC 9c19bfb2), an overlay on an AmberELEC image:
    # AmberELEC/usr/bin/batocera-info:101 prints its name.
    _OsRule(_STANDIN_MANIFESTS["AmberELEC"], "PAN4ELEC", "file_matches",
            ("/usr/bin/batocera-info", r"OPERATING SYSTEM: PAN4ELEC"),
            "/storage/roms", base="AmberELEC"),
    # EmuELEC d4c7b2f1: scripts/image:172-176 NAME and ID from DISTRONAME,
    # EmuELEC in distributions/EmuELEC/options:4. setsettings.sh:22 names the
    # config, and libretro_base/retroarch/package.mk:117 sets its
    # system_directory to /storage/roms/bios.
    _OsRule(_STANDIN_MANIFESTS["EmuELEC"], "EmuELEC", "os_release", ("NAME=EmuELEC",),
            "/storage/roms"),
    # JELOS 7b2e116c: scripts/image:118-120 OS_NAME="${DISTRONAME}", JELOS in
    # distributions/JELOS/options:17. runemu.sh:37 names the config, whose
    # default is "~/roms/bios" (retroarch/sources/RK3566/retroarch.cfg:716),
    # and emustation.service:8 sets HOME=/storage.
    _OsRule("retroarch", "JELOS", "os_release", ("OS_NAME=JELOS",),
            "/storage/roms/bios", _ELEC_CONFIGS, "/storage"),
    # KOLOS (AnonLinuxOS/KOLOS 7daf5950) keeps OS_NAME=JELOS
    # (distributions/JELOS/options:17) and adds jelos-ready.service
    # ("KOLOS system ready script"), installed by scripts/install:100-102.
    _OsRule("retroarch", "KOLOS", "file_exists",
            ("/usr/lib/systemd/system/jelos-ready.service",),
            "/storage/roms/bios", _ELEC_CONFIGS, "/storage", base="JELOS"),
    # UnofficialOS 22635796: scripts/image:119-120 OS_NAME, UnofficialOS in
    # distributions/UnofficialOS/options:17, config at
    # packages/unofficialos/sources/scripts/runemu.sh:37.
    _OsRule("retroarch", "UnofficialOS", "os_release", ("OS_NAME=UnofficialOS",),
            "/storage/roms/bios", _ELEC_CONFIGS, "/storage"),
    # Arch R (archr-linux/Arch-R dbd45d45): scripts/image:164-177 NAME and
    # OS_NAME, ArchR in distributions/ArchR/options:17. factoryreset:9-10
    # names the config, whose default is "~/roms/bios"
    # (retroarch/sources/RK3326/retroarch.cfg:718).
    _OsRule("retroarch", "ArchR", "os_release", ("OS_NAME=ArchR",),
            "/storage/roms/bios", _ELEC_CONFIGS, "/storage"),
    # ArkOS: the user and the menu folder, and the boot splash title its
    # updates write (christianhaitian/arkos dedf4278, Update-RGB10.sh:76).
    _OsRule("retroarch", "ArkOS", "file_exists", ("/home/ark", "/opt/system"), "/roms/bios",
            _ARK_CONFIGS),
    _OsRule("retroarch", "ArkOS", "file_matches", (_PLYMOUTH_TITLE, r"^title=\s*ArkOS\b"),
            "/roms/bios", _ARK_CONFIGS),
    # dArkOS daecf488: finishing_touches.sh:359-361 writes "title=dArkOS
    # (<date>)", kept by the dArkOSRE, dArkOSen and ArkOS4Clone overlays.
    # Default system_directory "/roms/bios" (retroarch.cfg.rgb10:3043).
    _OsRule("retroarch", "dArkOS", "file_matches", (_PLYMOUTH_TITLE, r"^title=\s*dArkOS"),
            "/roms/bios", _ARK_CONFIGS, base="ArkOS"),
    # RetroOZ 0.60 image (no build source is published): title=RetroOZ in
    # the splash theme, /opt/.retrooz/version, and /etc/retrooz_version, the
    # file PortMaster reads (device_info.txt:95). RetroArch is started with
    # --config /home/odroid/.config/retroarch/retroarch.cfg (es_systems.cfg),
    # system_directory "/roms/bios" (retroarch32/retroarch.cfg:3046).
    _OsRule("retroarch", "RetroOZ", "file_matches", (_PLYMOUTH_TITLE, r"^title=\s*RetroOZ"),
            "/roms/bios", ("/home/odroid/.config/retroarch/retroarch.cfg",
                           "/home/odroid/.config/retroarch32/retroarch.cfg")),
    _OsRule("retroarch", "RetroOZ", "file_exists", ("/opt/.retrooz",),
            "/roms/bios", ("/home/odroid/.config/retroarch/retroarch.cfg",
                           "/home/odroid/.config/retroarch32/retroarch.cfg")),
    _OsRule("retroarch", "RetroOZ", "file_exists", ("/etc/retrooz_version",),
            "/roms/bios", ("/home/odroid/.config/retroarch/retroarch.cfg",
                           "/home/odroid/.config/retroarch32/retroarch.cfg")),
    # TheRA is left undetected: no image or source of its Odroid Go builds is
    # published, so neither its markers nor its folder could be read.
    # spruceOS 824b1aae: the spruce folder at the card root. RetroArch reads
    # Saves/ra-configs/retroarch-<platform>.cfg (spruce/scripts/
    # helperFunctions.sh:157-165), seeded with system_directory
    # "/mnt/SDCARD/BIOS" (RetroArch/platform/retroarch-Brick.cfg.bak:3279).
    _OsRule("retroarch", "spruceOS", "file_exists", ("/mnt/SDCARD/spruce",),
            "/mnt/SDCARD/BIOS", ("/mnt/SDCARD/Saves/ra-configs/retroarch-*.cfg",),
            base="TrimUI"),
    # CrossMix-OS 206d612f: crossmix-version.txt (System/usr/trimui/scripts/
    # update_common.sh:20), RetroArch/retroarch.cfg:3202 (patch_ra_cfg.sh:5).
    _OsRule("retroarch", "CrossMix-OS", "file_exists",
            ("/mnt/SDCARD/System/usr/trimui/crossmix-version.txt",),
            "/mnt/SDCARD/BIOS", ("/mnt/SDCARD/RetroArch/retroarch.cfg",), base="TrimUI"),
    # Onion 07505ea5: Makefile:75-76 writes onionVersion/version.txt, and
    # static/build/.tmp_update/script/patch_ra_cfg.sh:6 names the config
    # (system_directory at static/configs/RetroArch/.retroarch/retroarch.cfg:3009).
    _OsRule("retroarch", "Onion", "file_exists",
            ("/mnt/SDCARD/.tmp_update/onionVersion/version.txt",),
            "/mnt/SDCARD/BIOS", ("/mnt/SDCARD/RetroArch/.retroarch/retroarch.cfg",)),
    # Allium ee0dda97: its base folder /mnt/SDCARD/.allium
    # (crates/common/src/constants.rs:19-34). RetroArch runs with
    # HOME=/mnt/SDCARD/RetroArch on .retroarch/retroarch.cfg
    # (static/.allium/cores/retroarch/launch_without_savestate_auto_load.sh:2-7),
    # and Allium's RetroArch patch makes $ROOT/BIOS the default system folder.
    _OsRule("retroarch", "Allium", "file_exists", ("/mnt/SDCARD/.allium",),
            "/mnt/SDCARD/BIOS", ("/mnt/SDCARD/RetroArch/.retroarch/retroarch.cfg",)),
    # Koriki 8513071c: src/systemInfo/main.c:106 reads Koriki/version.txt,
    # .tmp_update/runtime.sh:271 installs RetroArch/.retroarch/retroarch.cfg
    # (system_directory at base/RetroArch/.retroarch/retroarch.cfg:3191).
    _OsRule("retroarch", "Koriki", "file_exists", ("/mnt/SDCARD/Koriki/version.txt",),
            "/mnt/SDCARD/BIOS", ("/mnt/SDCARD/RetroArch/.retroarch/retroarch.cfg",)),
    # MiyooCFW (buildroot d5912eeb): board/miyoo/rootfs/etc/main sets HOME=/mnt
    # (line 4), package/miyoo/miyoo-ctl/miyoo-ctl.mk:12 installs miyooctl.
    # RetroArch is a DINGUX build: $HOME/.retroarch/retroarch.cfg, and
    # $HOME/.retroarch/system when no config names a folder.
    _OsRule("retroarch", "MiyooCFW", "file_exists", ("/etc/main", "/usr/bin/miyooctl"),
            "/mnt/.retroarch/system", ("/mnt/.retroarch/retroarch.cfg",), "/mnt"),
    # plumOS: os-release ID=plumos (game-de-it/plumOS-pixel2_v2 dec872c0,
    # rootfs/pixel2/etc/os-release:2, PortMaster device_info.txt:101). Each
    # device build appends its own system_directory at launch: the Pixel 2
    # build /mnt/plumos-user/bios (package/app-layer-pixel2/bin/
    # plumos-retroarch-launch:342, mount point made by rootfs/pixel2/sbin/
    # init:29), the MF build <card>/BIOS (plumOS-MF 0095017c,
    # plumos-retroarch-launch:15). Only the first is told apart by a file.
    _OsRule("retroarch", "plumOS", "file_exists", ("/mnt/plumos-user",),
            "/mnt/plumos-user/bios"),
    _OsRule("retroarch", "plumOS", "os_release", ("ID=plumos",), None),
    # TrimUI's stock firmware publishes no source naming its RetroArch BIOS
    # folder. PortMaster finds its libraries in /usr/trimui
    # (trimui/launch_hooks.txt:37). Named so that what runs on top of it is
    # told apart, never installed to by a guess.
    _OsRule("", "TrimUI", "file_exists", ("/usr/trimui",), None),
    # MinUI, MyMinUI and NextUI keep their system in .system at the card root
    # (MinUI dbf89435, workspace/all/common/defines.h:14-16) and give each core
    # Bios/<tag>/ (workspace/all/minarch/minarch.c:2942). Their file lists
    # write that layout below the card root, and the card's version.txt
    # says which of the three it is (card_platform).
    _OsRule("", "MinUI", "file_exists", ("/mnt/SDCARD/.system/res",), None, base="TrimUI"),
    _OsRule("", "MinUI", "file_exists", ("/mnt/sdcard/.system/res",), None, base="TrimUI"),
    _OsRule("retroarch", "Anbernic stock firmware", "file_exists",
            ("/mnt/vendor/bin/dmenu.bin",), "/mnt/mmc/bios"),
)

# Why a system found here gets no folder: said once per run.
_NO_FOLDER = {
    "TrimUI": "its firmware publishes no source naming the folder its RetroArch reads",
    "MinUI": f"its card names no release in {CARD_MARKER}, which tells MinUI, NextUI "
             "and MyMinUI apart. Name the card's root and the one it runs: "
             "--platform minui (or nextui, myminui) --dest /mnt/SDCARD",
}
# A system found by its card, with the end of the path that proves it: the
# card root is what comes before.
_CARD_SYSTEMS = {"MinUI": "/.system/res"}

# (platform, BIOS folder) -> the name the system found there gives itself.
_DETECTED_OS: dict[tuple[str, Path], str] = {}


def _rule_matches(rule: _OsRule, release: dict[str, str]) -> bool:
    if rule.method == "os_release":
        # Any of the KEY=value pairs, the value compared without case.
        return any(
            release.get(key) == value.lower()
            for key, _, value in (pair.partition("=") for pair in rule.values)
        )
    if rule.method == "file_matches":
        path, pattern = rule.values
        try:
            text = _on_device(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None
    return all(_on_device(value).exists() for value in rule.values)


def _config_files(pattern: str) -> list[tuple[Path, Path]]:
    """(file to read, its folder as the system sees it) for a config rule."""
    device = pattern.replace("$HOME", str(Path.home()), 1)
    folder, _, name = device.rpartition("/")
    if "*" not in name:
        return [(_on_device(device), Path(folder))]
    try:
        return [(path, Path(folder)) for path in sorted(_on_device(folder).glob(name))
                if path.is_file()]
    except OSError:
        return []


def _os_system_dir(rules: list[_OsRule]) -> Path | None:
    """The folder a system's RetroArch reads: the system_directory of its
    first config that sets one, else the folder its build defaults to.

    The config also records where the cores of that installation are.
    """
    for rule in rules:
        home = Path(rule.home) if rule.home else None
        for pattern in rule.configs:
            for path, folder in _config_files(pattern):
                system_dir = _parse_retroarch_system_dir(path, folder, home)
                if system_dir is not None:
                    seen = _on_device(str(system_dir))
                    _RETROARCH_CONFIGS.setdefault(seen, _RETROARCH_CONFIGS[system_dir])
                    return seen
    bios = next((rule.bios for rule in rules if rule.bios), None)
    return _on_device(bios) if bios else None


def _detect_embedded() -> list[tuple[str, Path]]:
    """The embedded system this is, by what its own build writes.

    A system built on another matches both: the derivative is meant. Two
    unrelated systems matching are both returned, and os_conflict() names
    them, so that no folder is chosen between them in silence.
    """
    release = {k: v.lower() for k, v in _parse_os_release().items()}
    matched = [rule for rule in _EMBEDDED_RULES if _rule_matches(rule, release)]
    bases = {rule.base for rule in matched if rule.base}
    found: list[tuple[str, Path]] = []
    for name in dict.fromkeys(rule.name for rule in matched):
        if name in bases:
            continue
        rules = [rule for rule in matched if rule.name == name]
        plat = rules[0].manifest
        folder = _os_system_dir(rules) if plat else None
        if name in _CARD_SYSTEMS:
            marker = next(value for rule in rules for value in rule.values
                          if value.endswith(_CARD_SYSTEMS[name]))
            root = _on_device(marker[:-len(_CARD_SYSTEMS[name])])
            plat = card_platform(root) or ""
            if plat:
                folder, name = root, display_name(plat)
        if folder is None:
            reason = _NO_FOLDER.get(
                name, "nothing on it names the folder its RetroArch reads")
            closing = "" if name in _CARD_SYSTEMS else " Name the folder with --dest."
            print(f"{name} found here, but {reason}.{closing}", file=sys.stderr)
            continue
        _DETECTED_OS[(plat, folder)] = name
        found.append((plat, folder))
    return found


def detected_os(plat: str, path: Path) -> str:
    """The name of the system detection found this folder on, or ""."""
    return _DETECTED_OS.get((plat, path), "")


def os_conflict(platforms: list[tuple[str, Path]]) -> list[str]:
    """The systems among these when there are two or more: a machine runs one."""
    names = list(dict.fromkeys(filter(None, (detected_os(n, p) for n, p in platforms))))
    return names if len(names) > 1 else []


def _system_lines() -> list[str]:
    """Each embedded system detection knows, with the files it gets."""
    lines = []
    for name in dict.fromkeys(rule.name for rule in _EMBEDDED_RULES):
        plat = next(rule.manifest for rule in _EMBEDDED_RULES if rule.name == name)
        if name in _CARD_SYSTEMS:
            lines.append(f"{name}: minui, nextui or myminui, as the card's {CARD_MARKER} "
                         "names it (from a computer: --dest <card root>)")
        elif plat:
            lines.append(f"{name}: {plat}")
        else:
            lines.append(f"{name}: not installed to, {_NO_FOLDER[name]} (use --dest)")
    return lines


def found_label(plat: str, path: Path) -> str:
    """How a detected folder is announced: the system's own name, and the
    platform whose files it gets when that is another name."""
    name = detected_os(plat, path)
    shown = display_name(plat)
    moved = "".join(f", {folder}/ at {place}"
                    for folder, place in sorted(_MOVED_FOLDERS.get(path, {}).items())
                    if folder in ("bios", "roms"))
    if not name or name == shown:
        return f"{shown} at {path}{moved}"
    return f"{name} at {path} ({shown} files){moved}"


def _android_storage_root() -> Path:
    """Shared storage root, the parent RetroArch builds its defaults from."""
    value = os.environ.get("EXTERNAL_STORAGE", "").strip()
    return Path(value) if value else Path("/storage/emulated/0")


# Where each RetroArch package keeps its folders, RetroArch v1.22.2
# (pkg/android/phoenix/build.gradle:46-95). com.retroarch and
# com.retroarch.aarch64 are each published twice under one id: by
# retroarch.com (normal, aarch64) and on Google Play (playStoreNormal,
# playStorePlus). com.retroarch.ra32 has no Google Play flavor. The Google
# Play build hands RetroArch Android/data/<package>/files as the storage its
# defaults are built from, the other builds the shared storage root
# (MainMenuActivity.java:182-183).
_ANDROID_RETROARCH_PLAY_IDS = ("com.retroarch", "com.retroarch.aarch64")
# A Google Play versionCode is the manifest's 1597175267 (AndroidManifest.xml:5)
# plus one per half week since 1609632000 (build.gradle:76, 87, 168-175), so
# it stays below that start time. The other flavors use the build time in
# seconds (build.gradle:42), above it for every build since 2021. The launcher
# writes the code to retroarch.cfg (UserPreferences.java:150), which
# RetroArch keeps (configuration.c:2623).
_ANDROID_PLAY_VERSION_CEILING = 1609632000


def _android_cfg_value(cfg: Path, key: str) -> str | None:
    """A value of an Android retroarch.cfg, None when unreadable or unset."""
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def _android_play_build(package: str, storage: Path, cfgs: tuple[Path, ...]) -> bool | None:
    """True for the Google Play build of this package, False for the
    retroarch.com one, None when nothing it wrote tells them apart.

    The version code in its config decides. Without a readable config, the
    folder each build writes does: the Google Play build never asks for
    shared storage (MainMenuActivity.java:199-202), and only it builds its
    defaults under Android/data/<package>/files/RetroArch.
    """
    if package not in _ANDROID_RETROARCH_PLAY_IDS:
        return False
    for cfg in cfgs:
        code = _android_cfg_value(cfg, "bundle_assets_extract_version_current")
        if code and code.isdigit() and int(code) > 0:
            return int(code) < _ANDROID_PLAY_VERSION_CEILING
    if (storage / "Android" / "data" / package / "files" / "RetroArch").is_dir():
        return True
    if (storage / "RetroArch").is_dir():
        return False
    return None


def _android_writable(folder: Path) -> bool:
    """Whether this shell can create folder: its nearest existing parent is
    writable. Android 11 and later keep Android/data/<package> of other apps
    from Termux."""
    for parent in (folder, *folder.parents):
        if parent.exists():
            return parent.is_dir() and os.access(parent, os.W_OK)
    return False


def _android_out_of_reach(name: str, folder: Path, what: str) -> str:
    """The line said for an emulator whose BIOS folder this shell cannot write."""
    return (f"{name} is installed, but this shell cannot write its BIOS folder "
            f"{folder} without root: {what} are not copied there.")


def _android_retroarch_builds() -> list[tuple[Path | None, str]]:
    """Each installed RetroArch build: its system folder when this shell can
    write it, else None and the line that says why.

    RetroArch v1.22.2: the launcher picks retroarch.cfg in the app external
    files directory, else in the internal one
    (pkg/android/phoenix-common/src/com/retroarch/browser/preferences/util/
    UserPreferences.java:36-89), and passes it as CONFIGFILE
    (MainMenuActivity.java:178, read at frontend/drivers/platform_unix.c:
    1428-1444). A system_directory left at default resolves against
    <storage>/RetroArch/system (platform_unix.c:1670-1697), storage being
    what the launcher passes as SDCARD: Android/data/<package>/files for the
    Google Play build, the shared storage root for the others.
    """
    storage = _android_storage_root()
    builds: list[tuple[Path | None, str]] = []
    for package in ANDROID_RETROARCH_PACKAGES:
        external = storage / "Android" / "data" / package / "files"
        if not (ANDROID_APP_DATA / package).exists() and not external.exists():
            continue
        cfgs = (external / "retroarch.cfg",
                ANDROID_APP_DATA / package / "files" / "retroarch.cfg")
        play = _android_play_build(package, storage, cfgs)
        play_root, shared_root = external / "RetroArch", storage / "RetroArch"
        named = next(filter(None, (_android_cfg_value(cfg, "system_directory")
                                   for cfg in cfgs)), "default")
        if play is None and named == "default":
            builds.append((None,
                f"RetroArch ({package}) is installed, but nothing it has written tells "
                f"which build it is: the Google Play build reads {play_root / 'system'}, "
                f"the retroarch.com build reads {shared_root / 'system'}. "
                "Name the folder with --dest."))
            continue
        root = play_root if play else shared_root
        folder = next(filter(None, (_parse_retroarch_system_dir(cfg, root) for cfg in cfgs)),
                      root / "system")
        private = (storage / "Android" / "data", ANDROID_APP_DATA)
        if any(folder == base or base in folder.parents for base in private) \
                and not _android_writable(folder):
            build = "Google Play build" if play else "retroarch.com build"
            builds.append((None, _android_out_of_reach(
                f"RetroArch ({build}, {package})", folder, "BIOS files")))
            continue
        builds.append((folder, ""))
    return builds


def _detect_android() -> list[tuple[str, Path]]:
    """Find the RetroArch system directory of each installed build on
    Android, as _android_retroarch_builds() resolves it (RetroArch v1.22.2,
    platform_unix.c:1670-1697).

    With no package seen, <shared storage>/RetroArch is the folder of a
    retroarch.com build whose private data this shell cannot see.
    """
    found: list[tuple[str, Path]] = []
    builds = _android_retroarch_builds()
    for folder, _line in builds:
        if folder is not None and ("retroarch", folder) not in found:
            found.append(("retroarch", folder))
    default_root = _android_storage_root() / "RetroArch"
    if not builds and default_root.exists():
        found.append(("retroarch", default_root / "system"))
    return found


# Android emulators that keep their BIOS outside RetroArch's folder:
# (name, package, folder, files read there). DraStic keeps it in its private
# data (GammaOS Next 14 bb28e71d, gammaos/setup.sh:345-363 extracts it to
# /data/data/com.dsemu.drastic/files/DraStic/system), which no other app
# writes without root. Flycast reads <home>/data/ (flycast a9bd1c74,
# shell/android-studio/flycast/src/main/jni/src/Android.cpp:143-159), its
# home being Android/data/com.flycast.emulator/files, which Android 11 and
# later keep from other apps.
_ANDROID_APP_FOLDERS = (
    ("DraStic", "com.dsemu.drastic", "/data/data/com.dsemu.drastic/files/DraStic/system",
     ("nds_bios_arm7.bin", "nds_bios_arm9.bin", "nds_firmware.bin")),
    ("Flycast", "com.flycast.emulator",
     "$STORAGE/Android/data/com.flycast.emulator/files/data",
     ("dc_boot.bin", "dc_nvmem.bin")),
)


def android_unreachable_folders() -> list[str]:
    """A line for each Android emulator installed here whose BIOS folder
    this shell cannot write, said instead of skipping it in silence. A
    RetroArch build whose folder is out of reach, or whose build cannot be
    told, is among them.

    An app's private folder can be tested for existence by any app, so the
    emulator is known to be installed even where its folder is out of reach.
    """
    lines = [line for folder, line in _android_retroarch_builds() if folder is None]
    storage = str(_android_storage_root())
    for name, package, folder, files in _ANDROID_APP_FOLDERS:
        target = Path(folder.replace("$STORAGE", storage, 1))
        if folder.startswith("/data/data/"):
            target = ANDROID_APP_DATA / Path(folder).relative_to("/data/data")
        if not (ANDROID_APP_DATA / package).exists() and not target.exists():
            continue
        if target.is_dir() and os.access(target, os.W_OK):
            continue
        lines.append(_android_out_of_reach(name, target, ", ".join(files)))
    return lines


def _lnk_target(lnk_path: Path, exe_name: str) -> Path | None:
    """Read the target path of a Windows shortcut.

    Shortcuts embed the target as a plain local path. Scanning for it avoids
    parsing the whole .lnk structure for the one field that matters here.
    """
    try:
        if lnk_path.stat().st_size > 64 * 1024:
            return None
        blob = lnk_path.read_bytes()
    except OSError:
        return None
    pattern = re.compile(
        rb"[A-Za-z]:\\[ -~]{0,260}?" + re.escape(exe_name.encode()), re.IGNORECASE
    )
    match = pattern.search(blob)
    if not match:
        return None
    return Path(match.group().decode("ascii", "replace").replace("\\", "/"))


def launchbox_root(os_type: str) -> Path | None:
    """Locate the LaunchBox installation directory.

    LaunchBox installs wherever the user points its installer and writes no
    uninstall registry key, so the Start menu shortcut is the only record of
    that choice. Everything else falls back to the default location.
    """
    if os_type not in ("windows", "wsl"):
        return None
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        lnk = (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "LaunchBox"
            / "LaunchBox.lnk"
        )
        if lnk.exists():
            target = _lnk_target(lnk, "LaunchBox.exe")
            if target:
                # The shortcut points at Core/LaunchBox.exe
                candidates.append(target.parent.parent)
                candidates.append(target.parent)
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        candidates.append(Path(userprofile) / "LaunchBox")
    for root in candidates:
        if (root / "Data" / "Emulators.xml").exists():
            return root
    return None


def launchbox_emulators(emulators_xml: Path) -> dict[str, Path]:
    """Map each emulator LaunchBox knows about to its installation directory.

    LaunchBox stores its emulator list in Data/Emulators.xml. ApplicationPath
    is either absolute or relative to the LaunchBox root directory. Keys are
    the lowercase executable names.
    """
    found: dict[str, Path] = {}
    if not emulators_xml.exists():
        return found
    try:
        if emulators_xml.stat().st_size > 10 * 1024 * 1024:
            return found
        raw = emulators_xml.read_text(encoding="utf-8", errors="replace")
        # ElementTree expands internal entities, and LaunchBox writes no doctype
        if "<!DOCTYPE" in raw.upper():
            return found
        tree = ET.ElementTree(ET.fromstring(raw))
    except (ET.ParseError, OSError):
        return found
    lb_root = emulators_xml.parent.parent
    for emu in tree.getroot().iter("Emulator"):
        app_path = (emu.findtext("ApplicationPath") or "").replace("\\", "/")
        if not app_path:
            continue
        exe = Path(app_path)
        if not exe.is_absolute():
            exe = lb_root / exe
        if exe.parent.is_dir():
            found.setdefault(exe.name.lower(), exe.parent)
    return found


def _launchbox_retroarch_system_dir(emulators_xml: Path) -> Path | None:
    """Resolve the system dir of a RetroArch referenced by LaunchBox.

    Mirrors the plugin's UpdateSystemPath: read system_directory from the
    retroarch.cfg sitting next to the executable, else assume system/.
    """
    emu_dir = launchbox_emulators(emulators_xml).get("retroarch.exe")
    if emu_dir is None:
        return None
    system_dir = _parse_retroarch_system_dir(emu_dir / "retroarch.cfg", emu_dir)
    return system_dir or emu_dir / "system"


def _pcsx2_bios_dir(emu_dir: Path) -> Path:
    """Resolve the BIOS folder of a PCSX2 installed under LaunchBox.

    EmuFolders (pcsx2/Pcsx2Config.cpp) enters portable mode when portable.ini
    or portable.txt sits next to the executable, and then takes the data root
    from the contents of portable.txt, relative to the executable. Otherwise
    the data root is Documents/PCSX2. Bios defaults to "bios" below it and the
    Bios key of PCSX2.ini overrides that.
    """
    documents = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
    portable_txt = emu_dir / "portable.txt"
    portable = (emu_dir / "portable.ini").exists() or portable_txt.exists()
    if portable:
        subpath = ""
        try:
            subpath = portable_txt.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            pass
        data_root = emu_dir / subpath if subpath else emu_dir
    else:
        data_root = documents / "PCSX2"
    ini = data_root / "inis" / "PCSX2.ini"
    try:
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Bios = "):
                value = line[len("Bios = "):].strip()
                candidate = Path(value)
                return candidate if candidate.is_absolute() else data_root / value
    except OSError:
        pass
    return data_root / "bios"


def _xemu_bios_dir(emu_dir: Path) -> Path:
    """Resolve the BIOS folder of a xemu managed by LaunchBox.

    Follows the plugin: xemu.toml next to the executable else the roaming
    copy, with bootrom_path and flashrom_path naming a file whose directory
    holds the images. Both default to bios/ under the executable.
    """
    appdata = os.environ.get("APPDATA", "")
    toml = emu_dir / "xemu.toml"
    if not toml.exists() and appdata:
        toml = Path(appdata) / "xemu" / "xemu" / "xemu.toml"
    if toml.exists():
        try:
            for line in toml.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(("bootrom_path", "flashrom_path")):
                    parts = line.split("'")
                    if len(parts) > 1 and Path(parts[1]).exists():
                        return Path(parts[1]).parent
        except OSError:
            pass
    return emu_dir / "bios"


def launchbox_bios_dirs(root: Path) -> dict[str, Path]:
    """Map emulator ids to the BIOS directory LaunchBox expects for them.

    Only emulators whose destination LaunchBox itself computes are listed.
    The rest keep the default locations declared in the manifest.
    """
    emulators = launchbox_emulators(root / "Data" / "Emulators.xml")
    dirs: dict[str, Path] = {}
    emu_dir = emulators.get("pcsx2.exe")
    if emu_dir:
        dirs["pcsx2"] = _pcsx2_bios_dir(emu_dir)
    emu_dir = emulators.get("xemu.exe")
    if emu_dir:
        dirs["xemu"] = _xemu_bios_dir(emu_dir)
    emu_dir = emulators.get("dolphin.exe")
    if emu_dir and (emu_dir / "portable.txt").exists():
        # portable.txt beside the executable moves the user directory to
        # User (SetUserDirectory in Source/Core/UICommon/UICommon.cpp,
        # PORTABLE_USER_DIR in Source/Core/Common/CommonPaths.h)
        dirs["dolphin"] = emu_dir / "User"
    return dirs


def detect_frontends(os_type: str, home: Path | None = None) -> list[str]:
    """Detect frontends that reference emulators without owning a BIOS dir.

    ES-DE resolves its application data directory to $ESDE_APPDATA_DIR or
    <home>/ES-DE (es-core FileSystemUtil.cpp getAppDataDirectory).
    """
    home = home or Path.home()
    frontends: list[str] = []
    esde_env = os.environ.get("ESDE_APPDATA_DIR", "")
    if (esde_env and Path(esde_env).is_dir()) or (home / "ES-DE").is_dir():
        frontends.append("esde")
    if launchbox_root(os_type) is not None:
        frontends.append("launchbox")
    return frontends


# The registry's path_exists rules (platforms/_registry.yml install.detect),
# one row each: (os, platform, path, BIOS directory when it is not the path).
# The manifests publish them too, but detection runs before any manifest is
# fetched. A test holds this table equal to the registry. Without it a Linux
# home holding RetroPie/BIOS or .config/BizHawk/Firmware detected nothing.
_PATH_RULES: tuple[tuple[str, str, str, str | None], ...] = (
    ("windows", "retrobat", "C:\\RetroBat\\bios", None),
    ("windows", "retrobat", "%USERPROFILE%\\RetroBat\\bios", None),
    ("linux", "emudeck", "$HOME/Emulation/bios", None),
    ("darwin", "emudeck", "$HOME/Emulation/bios", None),
    ("linux", "retrodeck", "$HOME/.var/app/net.retrodeck.retrodeck", "$HOME/retrodeck"),
    ("linux", "romm", "/romm/library/bios", None),
    # A library laid out as {platform}/bios has no library/bios: RomM's
    # config folder is the evidence (config_manager.py:264-265).
    ("linux", "romm", "/romm/config/config.yml", "/romm/library/bios"),
    ("linux", "retropie", "$HOME/RetroPie/BIOS", None),
    ("windows", "bizhawk", "%USERPROFILE%\\BizHawk\\Firmware", None),
    ("linux", "misterfpga", "/media/fat/MiSTer", "/media/fat/games"),
)


def _rule_path(template: str) -> Path:
    """A rule's path on this machine: $HOME or %USERPROFILE%, then the rest."""
    bases = (
        ("$HOME", Path.home()),
        ("%USERPROFILE%", Path(os.environ.get("USERPROFILE") or Path.home())),
    )
    for variable, base in bases:
        if template.startswith(variable):
            rest = [part for part in re.split(r"[\\/]", template[len(variable):]) if part]
            return base.joinpath(*rest)
    return _host_path(template)


# The registry's executable rules: (os, platform, glob under $HOME or
# %USERPROFILE%, folder beside the executable that holds the BIOS). BizHawk is
# a portable archive reading <EmuHawk folder>/Firmware (PathExtensions.cs
# DataDirectoryPath), wherever it was extracted; ~/.config/BizHawk/Firmware,
# the old guess, is a folder no build reads.
_EXE_RULES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("linux", "bizhawk", ("$HOME/BizHawk*/EmuHawkMono.sh", "$HOME/*/BizHawk*/EmuHawkMono.sh"),
     "Firmware"),
    ("windows", "bizhawk", ("%USERPROFILE%\\BizHawk*\\EmuHawk.exe",
                            "%USERPROFILE%\\*\\BizHawk*\\EmuHawk.exe"), "Firmware"),
)


def _detect_by_executable(os_type: str, found: list[tuple[str, Path]]) -> None:
    """Add a portable install found by its executable, the newest first."""
    systems = ("linux", "windows") if os_type == "wsl" else (os_type,)
    for rule_os, platform_name, patterns, subdir in _EXE_RULES:
        if rule_os not in systems or any(name == platform_name for name, _ in found):
            continue
        hits: list[Path] = []
        for pattern in patterns:
            base, _, rest = pattern.partition("/" if "/" in pattern else "\\")
            glob = "/".join(part for part in re.split(r"[\\/]", rest) if part)
            try:
                hits.extend(path for path in _rule_path(base).glob(glob) if path.is_file())
            except OSError:
                continue
        if hits:
            newest = max(hits, key=lambda path: path.stat().st_mtime)
            found.append((platform_name, newest.parent / subdir))


# The registry's registry rules: (os, platform, key, value, BIOS folder under
# the folder the value names). RetroBat.exe writes its own folder there on
# every start (retrobat-executable 702da66a, RetroBat/RetroBat/Program.cs:57
# and 99, RegistryTools.cs:15-50), wherever it was installed. The path rules
# only knew C:\RetroBat and the profile folder.
_REGISTRY_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    ("windows", "retrobat", "HKCU\\SOFTWARE\\RetroBat", "LatestKnownInstallPath", "bios"),
)


def _read_registry(key: str, value: str) -> str | None:
    """A string value of the Windows registry, read natively or, under WSL,
    through reg.exe. None when the key, the value or the means is missing."""
    if os.name == "nt":
        import winreg  # noqa: PLC0415

        hive, _, subkey = key.partition("\\")
        roots = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
        try:
            with winreg.OpenKey(roots[hive], subkey) as handle:
                data, _kind = winreg.QueryValueEx(handle, value)
        except (OSError, KeyError):
            return None
        return data.strip() if isinstance(data, str) and data.strip() else None
    if not _WSL["active"]:
        return None
    try:
        result = subprocess.run(
            ["reg.exe", "query", key, "/v", value],
            capture_output=True, text=True, errors="replace", timeout=10,
            stdin=subprocess.DEVNULL, cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(
        rf"^\s*{re.escape(value)}\s+REG_(?:EXPAND_)?SZ\s+(.+?)\s*$", result.stdout, re.M
    )
    return match.group(1) if match else None


def _detect_by_registry(os_type: str, found: list[tuple[str, Path]]) -> None:
    """Add a platform whose own program names its folder in the registry.

    A folder since moved or removed is passed over, so the path rules still
    get their turn.
    """
    systems = ("windows",) if os_type in ("windows", "wsl") else ()
    for rule_os, platform_name, key, value, subdir in _REGISTRY_RULES:
        if rule_os not in systems or any(name == platform_name for name, _ in found):
            continue
        folder = _read_registry(key, value)
        if not folder:
            continue
        base = _host_path(folder.rstrip("\\/") or folder)
        if base.is_dir():
            found.append((platform_name, base / subdir))


def _detect_by_path(os_type: str, found: list[tuple[str, Path]]) -> None:
    """Add what a registry path rule finds, for a platform not found yet."""
    systems = ("linux", "windows") if os_type == "wsl" else (os_type,)
    for rule_os, platform_name, path, bios in _PATH_RULES:
        if rule_os not in systems or any(name == platform_name for name, _ in found):
            continue
        if _rule_path(path).exists():
            found.append((platform_name, _rule_path(bios or path)))


def detect_platforms(os_type: str) -> list[tuple[str, Path]]:
    """Detect installed emulator platforms and their BIOS directories."""
    found: list[tuple[str, Path]] = []

    if os_type == "android":
        found.extend(_detect_android())
        for line in android_unreachable_folders():
            print(line, file=sys.stderr)

    if os_type in ("linux", "wsl"):
        found.extend(_detect_embedded())

    if os_type in ("linux", "wsl", "darwin"):
        # EmuDeck on Linux and macOS: functions/all.sh:3-4 and 24 (EmuDeck
        # dddb3022) reads $emudeckFolder/settings.sh on both, and vars.sh:2
        # sets emudeckFolder to $HOME/.config/EmuDeck.
        emudeck_settings = Path.home() / ".config" / "EmuDeck" / "settings.sh"
        if emudeck_settings.exists():
            emu_path = _parse_bash_var(emudeck_settings, "emulationPath")
            if emu_path:
                found.append(("emudeck", Path(emu_path) / "bios"))

    if os_type in ("linux", "wsl"):
        home = Path.home()
        # RetroDECK: retrodeck.json since the cfg-to-json migration, which
        # renames the old retrodeck.cfg to retrodeck.bak (global.sh:149-153)
        retrodeck_conf_dir = home / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        retrodeck_json = retrodeck_conf_dir / "retrodeck.json"
        retrodeck_cfg = retrodeck_conf_dir / "retrodeck.cfg"
        if retrodeck_json.exists():
            rd_home = _parse_json_path(retrodeck_json, "paths", "rd_home_path")
            if not rd_home:
                # A home moved to an SD card lives only in this file: the
                # default is a guess, and the user is told so.
                print(
                    f"Warning: could not read paths.rd_home_path from "
                    f"{retrodeck_json}; assuming {home / 'retrodeck'}",
                    file=sys.stderr,
                )
            rd_root = Path(rd_home) if rd_home else home / "retrodeck"
            _record_moved_folders(rd_root, retrodeck_json)
            found.append(("retrodeck", rd_root))
        elif retrodeck_cfg.exists():
            rd_home = _parse_bash_var(retrodeck_cfg, "rdhome")
            found.append(("retrodeck", Path(rd_home) if rd_home else home / "retrodeck"))

        # RetroArch Flatpak
        flatpak_cfg = home / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch" / "retroarch.cfg"
        ra_dir = _parse_retroarch_system_dir(flatpak_cfg)
        if ra_dir:
            found.append(("retroarch", ra_dir))

        # RetroArch Snap
        snap_cfg = home / "snap" / "retroarch" / "current" / ".config" / "retroarch" / "retroarch.cfg"
        ra_dir = _parse_retroarch_system_dir(snap_cfg)
        if ra_dir:
            found.append(("retroarch", ra_dir))

        # RetroArch native
        native_cfg = home / ".config" / "retroarch" / "retroarch.cfg"
        ra_dir = _parse_retroarch_system_dir(native_cfg)
        if ra_dir:
            found.append(("retroarch", ra_dir))

    if os_type == "darwin":
        # open_default_config_file reads <Application Support>/RetroArch/config
        # (configuration.c). A value left at default is the documents folder,
        # or Application Support itself for a portable install
        # (platform_darwin.m). The config beside it is where older builds
        # kept it.
        support = Path.home() / "Library" / "Application Support" / "RetroArch"
        for mac_cfg in (support / "config" / "retroarch.cfg", support / "retroarch.cfg"):
            ra_dir = _parse_retroarch_system_dir(mac_cfg, _macos_retroarch_base())
            if ra_dir:
                found.append(("retroarch", ra_dir))
                break

    if os_type in ("windows", "wsl"):
        emudeck = _emudeck_windows_bios()
        if emudeck is not None:
            found.append(("emudeck", emudeck))

        # RetroArch Windows: open_default_config_file reads retroarch.cfg
        # beside the exe, then under %APPDATA%\RetroArch, and a value left at
        # default is <exe folder>\system (configuration.c, platform_win32.c).
        appdata = os.environ.get("APPDATA", "")
        appdata_cfg = Path(appdata) / "RetroArch" / "retroarch.cfg" if appdata else None
        exe_dirs = _windows_retroarch_dirs() if os_type == "windows" else []
        for exe_dir in exe_dirs:
            ra_dir = _parse_retroarch_system_dir(exe_dir / "retroarch.cfg", exe_dir)
            if ra_dir is None and appdata_cfg is not None:
                ra_dir = _parse_retroarch_system_dir(appdata_cfg, exe_dir)
            found.append(("retroarch", ra_dir or exe_dir / "system"))
            break
        if not exe_dirs and appdata_cfg is not None:
            ra_dir = _parse_retroarch_system_dir(appdata_cfg)
            if ra_dir:
                found.append(("retroarch", ra_dir))

        # RetroArch from Steam: a portable install under the default library
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            program_files = os.environ.get(var, "")
            if not program_files:
                continue
            steam_cfg = (
                Path(program_files) / "Steam" / "steamapps" / "common"
                / "RetroArch" / "retroarch.cfg"
            )
            ra_dir = _parse_retroarch_system_dir(steam_cfg)
            if ra_dir and not any(name == "retroarch" for name, _ in found):
                found.append(("retroarch", ra_dir))
                break

        # The UWP/MSIX package (sideloaded, or Xbox in dev mode)
        if not any(name == "retroarch" for name, _ in found):
            uwp = _retroarch_uwp_system_dir()
            if uwp is not None:
                found.append(("retroarch", uwp))

        # Portable RetroArch referenced by LaunchBox
        lb_root = launchbox_root(os_type)
        if lb_root and not any(name == "retroarch" for name, _ in found):
            system_dir = _launchbox_retroarch_system_dir(
                lb_root / "Data" / "Emulators.xml"
            )
            if system_dir:
                found.append(("retroarch", system_dir))
        # BizHawk is a portable archive reading <EmuHawk folder>\Firmware
        # (PathExtensions.cs DataDirectoryPath, PathEntryCollection "Firmware").
        if lb_root and not any(name == "bizhawk" for name, _ in found):
            emu_dir = launchbox_emulators(lb_root / "Data" / "Emulators.xml").get("emuhawk.exe")
            if emu_dir is not None:
                found.append(("bizhawk", emu_dir / "Firmware"))

    if os_type in ("linux", "wsl", "darwin"):
        bizhawk = _bizhawk_unix_firmware()
        if bizhawk is not None and not any(name == "bizhawk" for name, _ in found):
            found.append(("bizhawk", bizhawk))

    _detect_by_executable(os_type, found)
    _detect_by_registry(os_type, found)
    _detect_by_path(os_type, found)
    # An embedded system's config read again as the home folder's one (HOME is
    # /storage on the ELEC family) is the same folder.
    return list(dict.fromkeys(found))


def _emudeck_windows_bios() -> Path | None:
    """The BIOS folder of EmuDeck's Windows edition, as its scripts find it.

    functions/all.ps1:4-14 (emudeck-we 037460ef) loads
    %APPDATA%\\EmuDeck\\settings.ps1, else %USERPROFILE%\\EmuDeck\\settings.ps1,
    and takes C:\\Emulation when neither sets $emulationPath. A network
    install sets it through a drive mapped to $VolName in that session
    (JSONtoPS1.ps1:166-172), which is the share itself here.
    """
    candidates = [Path(os.environ[var]) / "EmuDeck" / "settings.ps1"
                  for var in ("APPDATA", "USERPROFILE") if os.environ.get(var)]
    settings = next((path for path in candidates if path.is_file()), None)
    if settings is None:
        return None
    emu_path = _parse_ps1_var(settings, "$emulationPath")
    if emu_path and ("$" in emu_path or "+" in emu_path):
        share = _parse_ps1_var(settings, "$VolName")
        emu_path = f"{share.rstrip(chr(92))}\\Emulation" if share else None
    return _host_path(emu_path or "C:\\Emulation") / "bios"


# Identity Name in pkg/msvc-uwp/RetroArch-msvcUWP/Package.appxmanifest.
RETROARCH_UWP_ID = "1e4cf179-f3c2-404f-b9f3-cb2070a5aad8"


def _retroarch_uwp_system_dir() -> Path | None:
    """System directory of the RetroArch UWP package, if one is installed.

    The package writes into its LocalState folder: uwp_main.cpp makes it the
    home directory, configuration.c reads retroarch.cfg there, and
    platform_uwp.c defaults the system directory to ~\\system. A `~` in the
    config is therefore LocalState, not the user's profile.
    """
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return None
    states = sorted(Path(local, "Packages").glob(f"{RETROARCH_UWP_ID}_*/LocalState"))
    if not states:
        return None
    state = states[0]
    cfg = state / "retroarch.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == "system_directory":
                value = value.strip().strip('"').strip("'")
                if value.startswith("~"):
                    return state / value[1:].lstrip("\\/")
                break
    except OSError:
        return state / "system"
    return _retroarch_cfg_dir(cfg, "system_directory", state, "system") or state / "system"


def _bizhawk_unix_firmware() -> Path | None:
    """BizHawk's Firmware folder on Unix, where its data home says.

    PathExtensions.cs reads $BIZHAWK_DATA_HOME before the executable's
    folder, and the Nix package sets it to
    $XDG_DATA_HOME/emuhawk-monort-<version> (Dist/launch-scripts.nix).
    """
    data_home = os.environ.get("BIZHAWK_DATA_HOME", "").strip()
    if data_home and Path(data_home).is_dir():
        return Path(data_home) / "Firmware"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
    homes = sorted(Path(xdg).glob("emuhawk-monort-*"), key=lambda p: p.stat().st_mtime)
    return homes[-1] / "Firmware" if homes else None


# A libretro core as RetroArch loads it: <name>_libretro.so|dll|dylib, and
# <name>_libretro_android.so on Android.
_CORE_FILE_RE = re.compile(
    r"^(?P<name>.+?)_libretro(?:_android)?\.(?:so|dll|dylib)$", re.IGNORECASE
)
CORE_PLATFORMS = ("retroarch", "lakka", "retropie")
# Where these systems install cores whatever retroarch.cfg says: RetroPie
# gives each package its own folder, Lakka keeps the image's cores and the
# ones the online updater fetches apart.
_FIXED_CORE_DIRS = {
    "retropie": (Path("/opt/retropie/libretrocores"),),
    "lakka": (Path("/usr/lib/libretro"), Path("/storage/cores"), Path("/tmp/cores")),
}


def core_dirs(plat: str, bios_path: Path) -> list[Path]:
    """The directories the RetroArch writing to bios_path loads cores from.

    The config detection read for this system directory names them in
    libretro_directory. Without one, a retroarch.cfg beside the system
    directory is the portable layout. Unset, the setting is cores/ under
    RetroArch's own directory.
    """
    found = _RETROARCH_CONFIGS.get(bios_path)
    if found is None and (bios_path.parent / "retroarch.cfg").is_file():
        found = (bios_path.parent / "retroarch.cfg", bios_path.parent)
    dirs: list[Path] = []
    if found is not None:
        cfg, app_dir = found
        dirs.append(
            _retroarch_cfg_dir(cfg, "libretro_directory", app_dir, "cores")
            or app_dir / "cores"
        )
    dirs.extend(_FIXED_CORE_DIRS.get(plat, ()))
    return list(dict.fromkeys(dirs))


def keeps_case(directory: Path) -> bool:
    """Whether names differing only by case are distinct files there.

    Probed on the nearest existing directory, since the filesystem decides,
    not the operating system: an ext4 drive mounted on macOS keeps case and
    a casefolded ext4 directory folds it. Unprobeable: the system's default.
    """
    probe_dir = directory
    while not probe_dir.is_dir() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        handle, name = tempfile.mkstemp(prefix=".retrobios-case-", dir=str(probe_dir))
    except OSError:
        return sys.platform not in ("win32", "darwin")
    os.close(handle)
    try:
        twin = os.path.join(os.path.dirname(name), os.path.basename(name).upper())
        return not os.path.exists(twin)
    finally:
        os.unlink(name)


def fold_case_twins(files: list[dict]) -> tuple[list[dict], list[str]]:
    """One file per name where the directory folds case, the others named.

    The first in manifest order stays, as the pack's own order places it.
    """
    kept: list[dict] = []
    seen: set[str] = set()
    folded: list[str] = []
    for entry in files:
        key = str(entry.get("dest", "")).casefold()
        if key in seen:
            folded.append(str(entry.get("dest", "")))
            continue
        seen.add(key)
        kept.append(entry)
    return kept, folded


def installed_cores(dirs: list[Path]) -> list[str]:
    """The cores present in these directories, by the name the buildbot uses.

    A folder inside is looked into once, which is how RetroPie installs
    each package. An unreadable directory holds nothing that can be seen.
    """
    names: set[str] = set()
    for directory in dirs:
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            candidates = [child]
            if child.is_dir():
                try:
                    candidates = list(child.iterdir())
                except OSError:
                    continue
            for path in candidates:
                match = _CORE_FILE_RE.match(path.name)
                if match and path.is_file():
                    names.add(match.group("name"))
    return sorted(names)


def display_name(plat: str, manifest: dict | None = None) -> str:
    """A platform's name as the frontend writes it, the manifest's when it has one."""
    named = (manifest or {}).get("display_name")
    if isinstance(named, str) and named.strip().isprintable() and 0 < len(named.strip()) <= 40:
        return named.strip()
    return PLATFORM_NAMES.get(plat, plat)


def _name_key(name: str) -> str:
    """Letters and digits only: MiSTer FPGA, misterfpga and Mister-FPGA are one name."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# A list this short is printed whole rather than pointed at.
SHORT_LIST = 12


# Names people type for a system, each with its id. Only ever suggested:
# --system gba, psx and ps1 were refused with no hint on RetroArch, whose
# list names no system that way, and taking one in silence could install
# another console than the one meant.
SYSTEM_SHORT_NAMES = {
    **dict.fromkeys(("psx", "ps1", "ps", "playstation1"), "sony-playstation"),
    "ps2": "sony-playstation-2", "ps3": "sony-playstation-3", "psp": "sony-psp",
    **dict.fromkeys(("vita", "psvita"), "sony-playstation-vita"),
    "gb": "nintendo-gb", "gbc": "nintendo-gbc", "gba": "nintendo-gba",
    **dict.fromkeys(("nes", "famicom", "fc"), "nintendo-nes"), "fds": "nintendo-fds",
    **dict.fromkeys(("snes", "sfc", "superfamicom"), "nintendo-snes"),
    "n64": "nintendo-64", "n64dd": "nintendo-64dd", "sgb": "nintendo-sgb",
    **dict.fromkeys(("nds", "ds"), "nintendo-ds"), "dsi": "nintendo-dsi",
    "3ds": "nintendo-3ds", "wiiu": "nintendo-wii-u", "switch": "nintendo-switch",
    **dict.fromkeys(("gc", "ngc", "gcn", "gamecube"), "nintendo-gamecube"),
    "pokemini": "nintendo-pokemon-mini",
    **dict.fromkeys(("md", "gen", "genesis", "megadrive"), "sega-mega-drive"),
    **dict.fromkeys(("scd", "segacd", "megacd"), "sega-mega-cd"),
    "32x": "sega-32x", **dict.fromkeys(("sms", "mastersystem"), "sega-master-system"),
    **dict.fromkeys(("gg", "gamegear"), "sega-game-gear"),
    **dict.fromkeys(("ss", "saturn"), "sega-saturn"),
    **dict.fromkeys(("dc", "dreamcast"), "sega-dreamcast"),
    **dict.fromkeys(("naomi", "atomiswave"), "sega-dreamcast-arcade"),
    **dict.fromkeys(("pce", "tg16", "turbografx", "turbografx16", "pcengine",
                     "supergrafx"), "nec-pc-engine"),
    **dict.fromkeys(("pcecd", "tgcd", "turbografxcd"), "nec-pc-engine-cd"),
    "pcfx": "nec-pc-fx",
    **dict.fromkeys(("mvs", "aes"), "snk-neogeo"),
    **dict.fromkeys(("neocd", "neogeocd"), "snk-neogeo-cd"),
    **dict.fromkeys(("msx", "msx2"), "microsoft-msx"),
    "c64": "commodore-c64", "c128": "commodore-c128",
    **dict.fromkeys(("amiga", "cd32"), "commodore-amiga"),
    "x68k": "sharp-x68000", **dict.fromkeys(("jag", "jaguar"), "atari-jaguar"),
    **dict.fromkeys(("st", "atarist"), "atari-st"), "a800": "atari-400-800",
    **dict.fromkeys(("coleco", "colecovision"), "coleco-colecovision"),
    **dict.fromkeys(("intv", "intellivision"), "mattel-intellivision"),
    "o2": "magnavox-odyssey2", "channelf": "fairchild-channel-f",
    **dict.fromkeys(("zx", "spectrum", "zxspectrum"), "sinclair-zx-spectrum"),
    "cpc": "amstrad-cpc", "bbc": "acorn-bbc-micro", "apple2": "apple-ii",
}


def did_you_mean(
    typed: list[str], known: list[str], listing: str,
    aliases: dict[str, str] | None = None, inside_from: int = 4,
) -> str:
    """The closest known names to what was typed, and the option listing them all.

    A typo was answered with every name the platform has, up to 400 on one
    line, with no hint that playstation is sony-playstation. An alias close
    to what was typed suggests the name it stands for. A name holding what
    was typed, inside_from letters or more, is suggested too.
    """
    import difflib  # noqa: PLC0415

    by_key: dict[str, str] = {}
    for name in known:
        by_key.setdefault(_name_key(name), name)
    for alias, name in (aliases or {}).items():
        by_key.setdefault(_name_key(alias), name)
    close: list[str] = []
    for word in typed:
        key = _name_key(word)
        if not key:
            continue
        matches = difflib.get_close_matches(key, list(by_key), n=3, cutoff=0.6)
        if len(key) >= inside_from:
            matches += sorted(k for k in by_key if key in k and k not in matches)
        for k in matches:
            # Two aliases of one system (psx, ps1) name it once.
            if by_key[k] not in close:
                close.append(by_key[k])
    if close:
        return f"Did you mean: {', '.join(close[:3])}? {listing} lists all {len(known)}."
    if len(known) <= SHORT_LIST:
        return f"Known: {', '.join(known)}."
    return f"{listing} lists all {len(known)}."


def normalize_platform(name: str) -> str:
    """Return the canonical platform id for user-supplied input.

    The site writes MiSTer FPGA and RetroArch: letters and digits decide,
    as they do for verify.py and generate_pack.py.
    """
    key = _name_key(name)
    for plat in AVAILABLE_PLATFORMS:
        if key and key in (_name_key(plat), _name_key(display_name(plat))):
            return plat
    # --list-platforms prints `muOS: retroarch`, and --platform muos was
    # refused: a system named there stands for the list it gets.
    for rule in _EMBEDDED_RULES:
        if key and rule.manifest and key == _name_key(rule.name):
            print(f"  {rule.name} gets the {display_name(rule.manifest)} file list: "
                  f"--platform {rule.manifest}.")
            return rule.manifest
    print(f"Unknown platform '{name.strip()}'. "
          + did_you_mean([name], list(AVAILABLE_PLATFORMS), "--list-platforms"),
          file=sys.stderr)
    sys.exit(1)


def _read_limited_json(
    response: http.client.HTTPResponse, limit: int, label: str
) -> object:
    """Read a bounded UTF-8 JSON response.

    A body shorter than its Content-Length is a dropped connection, raised
    as one so the caller retries: parsed, the cut read as an invalid file
    list and the run ended without the network advice.
    """
    raw_length = response.headers.get("Content-Length") if response.headers else None
    expected: int | None = None
    if raw_length:
        try:
            expected = int(raw_length)
        except ValueError:
            expected = None
        if expected is not None and expected > limit:
            raise ValueError(f"{label} exceeds {limit} bytes")
    try:
        payload = response.read(limit + 1)
    except http.client.IncompleteRead as exc:
        raise ConnectionError(
            f"{label} cut after {len(exc.partial)} bytes"
        ) from exc
    if len(payload) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    if expected is not None and len(payload) < expected:
        raise ConnectionError(f"{label} cut after {len(payload)} of {expected} bytes")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def _safe_relative_path(value: object, field: str) -> PurePosixPath:
    """Validate a manifest-controlled relative POSIX path."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError(f"invalid {field}")
    if (
        "\\" in value
        or "\x00" in value
        or "//" in value
        or value.endswith("/")
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ValueError(f"unsafe {field}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe {field}: {value!r}")
    return path


def _within(candidate: Path, root: Path) -> bool:
    """Whether *candidate* stays under *root* once both are resolved."""
    try:
        (root / candidate.name).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _safe_target_dir(value: object, field: str) -> str:
    """Validate a manifest-controlled standalone-copy directory.

    These name emulator install directories, so unlike a BIOS destination they
    are legitimately absolute and outside the BIOS tree. What they must never
    do is climb: a '..' anywhere turns "copy next to the emulator" into "write
    wherever the manifest likes".
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"invalid {field}")
    if "\x00" in value:
        raise ValueError(f"unsafe {field}: {value!r}")
    parts = re.split(r"[\\/]", value)
    if any(part == ".." for part in parts):
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


# A platform whose folders move one by one: root -> {folder under the root:
# where it is now}. RetroDECK's per-folder move (RetroDECK 7786a849,
# functions/api_data_processing.sh:913-935) records each new place in
# retrodeck.json paths.<folder>_path (config/retrodeck/retrodeck.json:3-23),
# and its manifest places files under bios/ and roms/ of the home folder.
_MOVED_FOLDERS: dict[Path, dict[str, Path]] = {}


def _record_moved_folders(root: Path, retrodeck_json: Path) -> None:
    """Each RetroDECK folder that is no longer <home>/<name>."""
    try:
        paths = json.loads(retrodeck_json.read_text(encoding="utf-8", errors="replace"))["paths"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if not isinstance(paths, dict):
        return
    moved = {}
    for key, value in paths.items():
        name = key[:-len("_path")] if key.endswith("_path") else ""
        if name and name != "rd_home" and isinstance(value, str) and value.strip():
            place = Path(value.strip())
            if place != root / name:
                moved[name] = place
    if moved:
        _MOVED_FOLDERS[root] = moved


def _destination_path(root: Path, value: object) -> Path:
    """Place a manifest destination below *root*.

    The manifest cannot climb: _safe_relative_path refuses absolute paths,
    drive letters and '..'. Symbolic links met under the root are the
    user's own layout and are followed. EmuDeck links bios/shadps4/sys_modules
    into shadPS4's data directory, and the file has to land where the
    emulator reads it.
    """
    relative = _safe_relative_path(value, "dest")
    moved = _MOVED_FOLDERS.get(root, {})
    if len(relative.parts) > 1 and relative.parts[0] in moved:
        return moved[relative.parts[0]].resolve() / Path(*relative.parts[1:])
    return root.resolve() / Path(*relative.parts)


class SavePlace(NamedTuple):
    """The save directory a frontend's config names, whether it hands each
    core a folder of its own below it (RetroArch's sort_savefiles_enable,
    runloop.c runloop_path_set_redirect), and whether the file list's dests
    hold that folder (its save_directory.by_core)."""

    directory: Path
    by_core: bool
    listed_by_core: bool = True


# BIOS folder -> where the files read below its frontend's save directory go,
# set once the save directory is known (save_directory_for).
_SAVE_DIRS: dict[Path, SavePlace] = {}


def _entry_place(root: Path, entry: dict) -> tuple[Path, object]:
    """The folder an entry's dest is relative to, and the dest.

    An entry with root: save is relative to the save directory the frontend
    hands the core, below the BIOS folder's frontend save directory and the
    core's own folder where the frontend sorts saves by core. Any other is
    relative to the BIOS folder.
    """
    if entry.get("root") != "save":
        return root, entry.get("dest")
    place = _SAVE_DIRS.get(root)
    if place is None:
        raise ValueError(f"no save directory known for {entry.get('dest')}")
    dest = entry.get("dest")
    folder = entry.get("core_folder")
    # No core_folder: the core reads the same place whether the frontend
    # sorts saves by core or not (an empty library_name, or Cemu adding its
    # own folder only where the directory is not named after it).
    if folder and isinstance(dest, str) and place.by_core != place.listed_by_core:
        if place.listed_by_core:
            dest = dest.split("/", 1)[1] if "/" in dest else dest
        else:
            dest = f"{folder}/{dest}"
    return place.directory, dest


def _entry_path(root: Path, entry: dict) -> Path:
    """Where an entry is written, as _entry_place places it."""
    return _destination_path(*_entry_place(root, entry))


def _entry_key(entry: dict) -> str:
    """What tells one entry of the list from another: its dest, below the
    BIOS folder or below the save directory. KNULLI lists its own
    fbneo/hiscore.dat in the BIOS tree and FB Alpha 2012's below the save
    directory, two files under one dest. NUL never appears in a dest."""
    dest = str(entry.get("dest"))
    return f"save\0{dest}" if entry.get("root") == "save" else dest


def _check_root(entry: dict, dest: str) -> None:
    """root is absent or save, and core_folder one folder name of a save entry."""
    root = entry.get("root")
    if root is not None and root != "save":
        raise ValueError(f"invalid root for {dest}")
    folder = entry.get("core_folder")
    if folder is None:
        return
    if root != "save":
        raise ValueError(f"core_folder outside root: save for {dest}")
    if len(_safe_relative_path(folder, f"core_folder of {dest}").parts) != 1:
        raise ValueError(f"core_folder must be one folder name for {dest}")


def _check_save_directory(data: dict) -> None:
    """The manifest's save_directory, which every root: save entry needs."""
    layout = data.get("save_directory")
    entries = [
        entry for field in ("files", "case_variants", "omitted_files")
        for entry in data.get(field) or [] if isinstance(entry, dict)
    ]
    if layout is None:
        # An omission may name one: the list says the file exists and why
        # this platform cannot place it.
        installable = [
            entry for field in ("files", "case_variants")
            for entry in data.get(field) or [] if isinstance(entry, dict)
        ]
        if any(entry.get("root") == "save" for entry in installable):
            raise ValueError("entries below the save directory and no save_directory")
        return
    if (
        not isinstance(layout, dict)
        or set(layout) - {"by_system"} != {"folder", "by_core"}
        or not isinstance(layout["by_core"], bool)
        or layout.get("by_system", True) is not True
        or not _safe_relative_path(layout["folder"], "save_directory.folder").parts
    ):
        raise ValueError("invalid save_directory")
    if layout["by_core"]:
        # The core's folder heads each dest it names: dropped, never guessed,
        # where the frontend does not sort saves by core.
        for entry in entries:
            folder = entry.get("core_folder")
            dest = entry.get("dest")
            if folder and isinstance(dest, str) and dest.split("/", 1)[0] != folder:
                raise ValueError(f"{dest} does not start with its core_folder {folder}")


def _check_tags(entry: dict, field: str, where: str) -> None:
    """A selection field is absent or a short list of short strings."""
    value = entry.get(field)
    if value is None:
        return
    if (
        not isinstance(value, list)
        or len(value) > MAX_SELECTION_TAGS
        or not all(isinstance(tag, str) and 0 < len(tag) <= 256 for tag in value)
    ):
        raise ValueError(f"invalid {field} for {where}")


# What an alternative replaces on the entry it stands in for.
_COPY_FIELDS = (
    "sha1", "sha256", "size", "repo_path", "release_asset", "storage", "cores", "executable",
)


def _check_omission(entry: dict, dest: str) -> None:
    """The fields of a file left out: an omitted entry, or the claim of a core
    with nothing held at a destination another core's copy serves."""
    if not isinstance(entry.get("name"), str) or not entry["name"]:
        raise ValueError(f"invalid omitted file name for {dest}")
    if not isinstance(entry.get("system", ""), str):
        raise ValueError(f"invalid omitted system for {dest}")
    if not isinstance(entry.get("required"), bool):
        raise ValueError(f"invalid omitted required flag for {dest}")
    if entry.get("reason") not in OMISSION_REASONS:
        raise ValueError(f"invalid omission reason for {dest}")
    if "held" in entry and entry["held"] is not True:
        raise ValueError(f"invalid omitted held flag for {dest}")
    cores = entry.get("cores")
    if cores is not None and (
        not isinstance(cores, list)
        or not all(isinstance(core, str) for core in cores)
    ):
        raise ValueError(f"invalid omitted cores list for {dest}")
    for field in SELECTION_FIELDS:
        _check_tags(entry, field, dest)


def _check_download(entry: dict, where: str, dest: str) -> int:
    """The size, hashes, source and cores of one downloadable copy, and its size."""
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not (0 <= size <= MAX_DOWNLOAD_SIZE):
        raise ValueError(f"invalid size for {dest}")

    sha1 = entry.get("sha1", "")
    sha256 = entry.get("sha256", "")
    if sha1 and (not isinstance(sha1, str) or not _SHA1_RE.fullmatch(sha1)):
        raise ValueError(f"invalid SHA1 for {dest}")
    if sha256 and (
        not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256)
    ):
        raise ValueError(f"invalid SHA256 for {dest}")
    if not sha1 and not sha256:
        raise ValueError(f"missing content hash for {dest}")

    release_asset = entry.get("release_asset")
    repo_path = entry.get("repo_path")
    if release_asset:
        asset = _safe_relative_path(release_asset, f"{where}.release_asset")
        if len(asset.parts) != 1:
            raise ValueError(f"release asset must be a basename: {release_asset}")
    elif repo_path:
        source = _safe_relative_path(repo_path, f"{where}.repo_path")
        if source.parts[0] != "bios":
            raise ValueError(f"repo_path outside bios/: {repo_path}")
    else:
        raise ValueError(f"no download source for {dest}")

    cores = entry.get("cores")
    if cores is not None and (
        not isinstance(cores, list) or not all(isinstance(core, str) for core in cores)
    ):
        raise ValueError(f"invalid cores list for {dest}")
    if not isinstance(entry.get("executable", False), bool):
        raise ValueError(f"invalid executable flag for {dest}")
    return size


def _check_slot(entry: dict, where: str, dest: str) -> int:
    """The copies a platform file takes as cores and systems drop, and the largest size."""
    choices = entry.get("slot_choices", [])
    declared = entry.get("declared_choices")
    if declared is None and not choices:
        return 0
    if (
        entry.get("cores") is not None
        or not isinstance(choices, list)
        or not isinstance(declared, list)
        or not declared
        or len(choices) > MAX_SELECTION_TAGS
        or len(declared) > MAX_SELECTION_TAGS
    ):
        raise ValueError(f"invalid slot choices for {dest}")
    size = 0
    for index, choice in enumerate(declared):
        systems = choice.get("systems") if isinstance(choice, dict) else None
        if (
            not isinstance(systems, list)
            or not systems
            or not all(isinstance(s, str) for s in systems)
            or not set(choice) <= (set(_COPY_FIELDS) - {"cores"}) | {"systems"}
        ):
            raise ValueError(f"invalid declared choice for {dest}")
        size = max(size, _check_download(choice, f"{where}.declared_choices[{index}]", dest))
    for index, choice in enumerate(choices):
        if (
            not isinstance(choice, dict)
            or not choice.get("cores")
            or not set(choice) <= set(_COPY_FIELDS)
        ):
            raise ValueError(f"invalid slot choice for {dest}")
        # A choice naming only cores leaves the platform's own answer.
        if set(choice) != {"cores"}:
            size = max(size, _check_download(
                choice, f"{where}.slot_choices[{index}]", dest
            ))
    return size


def _check_entry(
    entry: object, where: str, seen_destinations: set[str]
) -> tuple[int, int]:
    """Check one file entry, return its size and the largest copy it may fetch."""
    if not isinstance(entry, dict):
        raise ValueError(f"{where} must be an object")
    dest = str(_safe_relative_path(entry.get("dest"), f"{where}.dest"))
    key = _entry_key({**entry, "dest": dest})
    if key in seen_destinations:
        raise ValueError(f"duplicate manifest destination: {dest}")
    seen_destinations.add(key)
    _check_root(entry, dest)

    size = _check_download(entry, where, dest)
    largest = size
    alternatives = entry.get("alternatives", [])
    if not isinstance(alternatives, list) or len(alternatives) > MAX_SELECTION_TAGS:
        raise ValueError(f"invalid alternatives for {dest}")
    for alt_index, alternative in enumerate(alternatives):
        if (
            not isinstance(alternative, dict)
            or not alternative.get("cores")
            or not set(alternative) <= set(_COPY_FIELDS)
        ):
            raise ValueError(f"invalid alternative for {dest}")
        # Narrowing can swap in any copy: the cap holds for the largest.
        largest = max(largest, _check_download(
            alternative, f"{where}.alternatives[{alt_index}]", dest
        ))
    largest = max(largest, _check_slot(entry, where, dest))
    absent = entry.get("absent_claims", [])
    if not isinstance(absent, list) or len(absent) > MAX_SELECTION_TAGS:
        raise ValueError(f"invalid absent claims for {dest}")
    for claim in absent:
        if not isinstance(claim, dict):
            raise ValueError(f"invalid absent claim for {dest}")
        _check_omission(claim, dest)
    for field in (*SELECTION_FIELDS, "declared_in"):
        _check_tags(entry, field, dest)
    return size, largest


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _check_data_directory(entry: object, where: str) -> None:
    """A data directory: its source archive, its mapping, its file list."""
    if not isinstance(entry, dict):
        raise ValueError(f"{where} must be an object")
    if not isinstance(entry.get("ref"), str) or not entry["ref"]:
        raise ValueError(f"{where}.ref is missing")
    if entry.get("dest", "") != "":
        _safe_relative_path(entry.get("dest"), f"{where}.dest")
    url = entry.get("url")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    if not (parsed.scheme == "https" or (
        parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS
    )):
        raise ValueError(f"{where}.url must use HTTPS")
    kind = entry.get("type")
    if kind not in ("zip", "tarball"):
        raise ValueError(f"{where}.type must be zip or tarball")
    strip = entry.get("strip_components", 0)
    if not isinstance(strip, int) or isinstance(strip, bool) or not 0 <= strip <= 16:
        raise ValueError(f"{where}.strip_components is invalid")
    if kind == "tarball" and (
        not isinstance(entry.get("source_path"), str) or not entry["source_path"]
    ):
        raise ValueError(f"{where}.source_path is missing")
    for field in ("exclude", "systems"):
        values = entry.get(field, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{where}.{field} is invalid")
    files = entry.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_MANIFEST_FILES:
        raise ValueError(f"{where}.files is invalid")
    for rel, meta in files.items():
        _safe_relative_path(rel, f"{where}.files")
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("sha1"), str)
            or not _SHA1_RE.match(meta["sha1"])
            or not isinstance(meta.get("size"), int)
            or not 0 <= meta["size"] <= MAX_DOWNLOAD_SIZE
        ):
            raise ValueError(f"{where}.files[{rel!r}] is invalid")


def _validate_manifest(data: object, plat: str) -> dict:
    """Validate the untrusted install-manifest boundary using stdlib only."""
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    if data.get("manifest_version") not in (1, 2):
        raise ValueError("unsupported manifest_version")
    if data.get("platform") != plat:
        raise ValueError("manifest platform does not match request")
    files = data.get("files")
    if not isinstance(files, list) or len(files) > MAX_MANIFEST_FILES:
        raise ValueError("invalid manifest files list")

    seen_destinations: set[str] = set()
    total_size = 0
    largest_total = 0
    for index, entry in enumerate(files):
        size, largest = _check_entry(entry, f"files[{index}]", seen_destinations)
        total_size += size
        largest_total += largest
        if largest_total > MAX_TOTAL_DOWNLOAD_SIZE:
            raise ValueError("manifest total size exceeds safety limit")
    variants = data.get("case_variants", [])
    if not isinstance(variants, list) or len(variants) > MAX_MANIFEST_FILES:
        raise ValueError("invalid case_variants list")
    for index, entry in enumerate(variants):
        _size, largest = _check_entry(
            entry, f"case_variants[{index}]", seen_destinations
        )
        largest_total += largest
        if largest_total > MAX_TOTAL_DOWNLOAD_SIZE:
            raise ValueError("manifest total size exceeds safety limit")
    data_dirs = data.get("data_directories", [])
    if not isinstance(data_dirs, list) or len(data_dirs) > MAX_SELECTION_TAGS:
        raise ValueError("invalid data_directories list")
    for index, entry in enumerate(data_dirs):
        _check_data_directory(entry, f"data_directories[{index}]")
    _check_save_directory(data)

    declared_total_files = data.get("total_files")
    if declared_total_files is not None and declared_total_files != len(files):
        raise ValueError("manifest total_files does not match files list")
    declared_total_size = data.get("total_size")
    if declared_total_size is not None and declared_total_size != total_size:
        raise ValueError("manifest total_size does not match file sizes")

    omitted = data.get("omitted_files", [])
    if not isinstance(omitted, list) or len(omitted) > MAX_MANIFEST_FILES:
        raise ValueError("invalid omitted_files list")
    seen_omitted: set[str] = set()
    for index, entry in enumerate(omitted):
        if not isinstance(entry, dict):
            raise ValueError(f"omitted_files[{index}] must be an object")
        dest = str(
            _safe_relative_path(
                entry.get("dest"), f"omitted_files[{index}].dest"
            )
        )
        key = _entry_key({**entry, "dest": dest})
        if key in seen_destinations or key in seen_omitted:
            raise ValueError(f"duplicate or conflicting omitted destination: {dest}")
        seen_omitted.add(key)
        _check_omission(entry, dest)
        _check_root(entry, dest)
        claims = entry.get("alternatives", [])
        if not isinstance(claims, list) or len(claims) > MAX_SELECTION_TAGS:
            raise ValueError(f"invalid omitted alternatives for {dest}")
        for claim in claims:
            claim_cores = claim.get("cores") if isinstance(claim, dict) else None
            if (
                not isinstance(claim, dict)
                or set(claim) != {"cores"}
                or not isinstance(claim_cores, list)
                or not claim_cores
                or not all(isinstance(core, str) for core in claim_cores)
            ):
                raise ValueError(f"invalid omitted alternative for {dest}")
        for field in (*SELECTION_FIELDS, "declared_in"):
            _check_tags(entry, field, dest)
    declared_total_omitted = data.get("total_omitted")
    if (
        declared_total_omitted is not None
        and declared_total_omitted != len(omitted)
    ):
        raise ValueError("manifest total_omitted does not match omitted_files")

    for field in ("core_names", "system_cores", "system_names"):
        mapping = data.get(field)
        if mapping is None:
            continue
        if not isinstance(mapping, dict) or len(mapping) > MAX_MANIFEST_FILES:
            raise ValueError(f"invalid {field}")
        for key in mapping:
            if not isinstance(key, str) or not 0 < len(key) <= 256:
                raise ValueError(f"invalid {field} key")
            _check_tags(mapping, key, field)
            if mapping[key] is None:
                raise ValueError(f"invalid {field} for {key}")

    copies = data.get("standalone_copies", [])
    if not isinstance(copies, list) or len(copies) > 10_000:
        raise ValueError("invalid standalone_copies")
    for index, entry in enumerate(copies):
        if not isinstance(entry, dict):
            raise ValueError(f"standalone_copies[{index}] must be an object")
        # The shape do_standalone_copies reads, checked here: a note with a
        # list for detect, a copy naming neither file nor pattern, or a list
        # for emulator passed and crashed the copy step after the download.
        where = f"standalone_copies[{index}]"
        if "note" in entry:
            detect = entry.get("detect", {})
            if not isinstance(entry["note"], str) or not isinstance(detect, dict):
                raise ValueError(f"invalid note entry at {where}")
            for values in detect.values():
                if not isinstance(values, list) or not all(
                    isinstance(value, str) for value in values
                ):
                    raise ValueError(f"invalid note entry at {where}")
            continue
        if "file" not in entry and "pattern" not in entry:
            raise ValueError(f"{where} names neither a file nor a pattern")
        if not isinstance(entry.get("emulator", ""), str):
            raise ValueError(f"invalid emulator at {where}")
        if "file" in entry:
            _safe_relative_path(
                entry["file"], f"standalone_copies[{index}].file"
            )
        if "source" in entry:
            _safe_relative_path(
                entry["source"], f"standalone_copies[{index}].source"
            )
        if "pattern" in entry:
            pattern = entry["pattern"]
            if (
                not isinstance(pattern, str)
                or not pattern
                or len(pattern) > 256
                or "/" in pattern
                or "\\" in pattern
                or ".." in pattern
            ):
                raise ValueError(f"invalid standalone copy pattern: {pattern!r}")
        targets = entry.get("targets", {})
        if targets and not isinstance(targets, dict):
            raise ValueError(f"invalid standalone copy targets at index {index}")
        for values in (targets or {}).values():
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError(f"invalid standalone copy targets at index {index}")
            for value in values:
                _safe_target_dir(value, f"standalone_copies[{index}].targets")
    return data


def _validate_targets(data: object) -> dict[str, dict]:
    """Validate and normalize legacy list-valued target manifests.

    A null core list means the target publishes no core inventory. That is a
    known target with no filter, not a broken manifest: rejecting it would
    discard every other target on the platform.
    """
    if not isinstance(data, dict) or len(data) > 10_000:
        raise ValueError("invalid targets manifest")
    normalized: dict[str, dict] = {}
    for target, value in data.items():
        if not isinstance(target, str) or not target or len(target) > 128:
            raise ValueError("invalid target name")
        if isinstance(value, dict):
            cores = value.get("cores")
        else:
            cores = value
        if cores is None:
            normalized[target] = {"cores": None}
            continue
        if not isinstance(cores, list) or len(cores) > 10_000 or not all(
            isinstance(core, str) and 0 < len(core) <= 256 for core in cores
        ):
            raise ValueError(f"invalid core list for target {target}")
        normalized[target] = {"cores": cores}
    return normalized


NETWORK_ADVICE = ("  Check the connection, and HTTPS_PROXY if this network needs a "
                  "proxy, then run the same command again.\n"
                  f"  More help: {TROUBLESHOOTING_PAGE}")


# The installer was written after this day. A clock that reads earlier is
# wrong, and TLS then finds every certificate not yet valid: a handheld
# with no clock battery boots on 1970 or 1980.
CLOCK_FLOOR = datetime.date(2026, 1, 1)
# Where systems keep their trusted certificates, for a Python built to look
# elsewhere: Debian, Fedora, openSUSE, the BSDs and macOS, Homebrew, Termux.
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem",
    "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem",
    "/data/data/com.termux/files/usr/etc/tls/cert.pem",
)
# Set from --verbose: the error text Python gives stays beside the words.
_RAW_ERRORS = threading.Event()


def _default_ca_present() -> bool:
    """Whether the certificates this Python reads by default exist."""
    import ssl  # noqa: PLC0415

    paths = ssl.get_default_verify_paths()
    for value in (os.environ.get("SSL_CERT_FILE"), paths.cafile, paths.openssl_cafile):
        if value and Path(value).is_file():
            return True
    for value in (os.environ.get("SSL_CERT_DIR"), paths.capath, paths.openssl_capath):
        try:
            if value and any(Path(value).iterdir()):
                return True
        except OSError:
            continue
    return False


def _known_bundle() -> str:
    """A certificate bundle the system keeps where this Python does not look."""
    return next((path for path in CA_BUNDLES if Path(path).is_file()), "")


def _ca_bundle_found() -> bool:
    """Whether any list of trusted certificates is on this machine.

    Windows verifies against its own store, which Python reads directly.
    """
    return os.name == "nt" or _default_ca_present() or bool(_known_bundle())


def network_words(
    exc: BaseException, today: datetime.date | None = None, verbose: bool | None = None,
) -> tuple[str, str]:
    """The reason a request failed, and the same in words a beginner reads.

    The reason is clock, no_ca or network. Offline, the run printed
    "[Errno -2] Name or service not known": the raw text now follows the
    words only under --verbose.
    """
    reason = getattr(exc, "reason", None)
    inner = reason if isinstance(reason, BaseException) else exc
    raw = str(reason or exc)
    code, words = "network", ""
    cert_error = getattr(sys.modules.get("ssl"), "SSLCertVerificationError", None)
    if cert_error is not None and isinstance(inner, cert_error):
        today = today or datetime.date.today()
        if not _ca_bundle_found():
            code = "no_ca"
            words = ("This system has no list of trusted certificates to check the "
                     "server with. Install the ca-certificates package, or name a "
                     "certificate bundle with SSL_CERT_FILE, then run again")
        elif today < CLOCK_FLOOR:
            code = "clock"
            words = (f"The clock reads {today.isoformat()}: connect to Wi-Fi and let the "
                     "time sync, or set the date, then run again")
        else:
            words = "the server's certificate was rejected"
            bundle = "" if _default_ca_present() else _known_bundle()
            words += (f": this Python reads no certificate list, run again with "
                      f"SSL_CERT_FILE={bundle}" if bundle
                      else ", which a proxy or a filter on this network can cause")
    elif isinstance(inner, socket.gaierror):
        words = "no internet connection, or the server name could not be looked up"
    elif isinstance(inner, (socket.timeout, TimeoutError)):
        words = "the connection timed out"
    elif isinstance(inner, ConnectionRefusedError):
        words = "the server refused the connection"
    elif isinstance(inner, OSError) and inner.errno in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        words = "no internet connection: the network cannot be reached"
    elif isinstance(inner, (ConnectionResetError, ConnectionAbortedError,
                            http.client.HTTPException)):
        words = "the connection was cut"
    if not words:
        return code, raw
    if _RAW_ERRORS.is_set() if verbose is None else verbose:
        words += f" [{raw}]"
    return code, words


def _address_lines(url: str) -> list[str]:
    """The address that failed, and the override that chose it if one did.

    A RETROBIOS_BASE_URL left from a mirror that is down read as a network
    fault: neither the host nor the variable was named.
    """
    lines = [f"  Address: {url}"]
    if os.environ.get("RETROBIOS_BASE_URL"):
        lines.append("  RETROBIOS_BASE_URL is set in this environment and chose that "
                     "address: unset it to read from the project itself.")
    return lines


def _fetch_json(
    url: str, limit: int, label: str, attempts: int = MAX_RETRIES, timeout: float = 30,
) -> object:
    """A JSON document, tried MAX_RETRIES times like every file download.

    A file list fetched once ended the run on one timeout over flaky Wi-Fi.
    A 4xx answer or an invalid document is final: asking again changes
    nothing.
    """
    _network_allowed(label)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return _read_limited_json(resp, limit, label)
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429 or attempt == attempts:
                raise
            delay = _retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
            problem = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            code, problem = network_words(exc)
            # A wrong clock or no certificate list fails every attempt alike.
            if attempt == attempts or code != "network":
                if isinstance(exc, http.client.HTTPException):
                    raise ConnectionError(f"connection cut: {exc!r}") from exc
                raise
            delay = _retry_delay(attempt, None)
        # Said, or three 30-second attempts read as a frozen program.
        print(f"  The {label} did not arrive ({problem}), trying again "
              f"({attempt + 1}/{attempts}) in {delay:.0f} s.", file=sys.stderr)
        time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_manifest(plat: str) -> dict:
    """Download platform manifest JSON, or read the one this run was given."""
    if LOCAL.manifest is not None:
        return read_local_manifest(LOCAL.manifest, plat)
    if LOCAL.pack is not None:
        try:
            return _validate_manifest(LOCAL.pack.manifest(plat), plat)
        except ValueError as exc:
            print(f"  The file list in {LOCAL.pack.name} is not valid: {exc}",
                  file=sys.stderr)
            sys.exit(1)
    url = MANIFEST_URL.format(platform=plat)
    try:
        return _validate_manifest(_fetch_json(url, MAX_MANIFEST_BYTES, "file list"), plat)
    except ValueError as exc:
        EVENTS.emit("error", stage="manifest", reason="invalid", platform=plat,
                    message=f"the file list is not valid: {exc}")
        print(f"  The file list for {plat} is not valid: {exc}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.HTTPError as exc:
        EVENTS.emit(
            "error", stage="manifest", reason="not_found" if exc.code == 404 else "network",
            platform=plat, message=f"the file list could not be fetched: HTTP {exc.code}",
        )
        if exc.code == 404 and os.environ.get("RETROBIOS_BASE_URL"):
            print(f"  No file list for {plat} there.", file=sys.stderr)
            for line in _address_lines(url):
                print(line, file=sys.stderr)
            sys.exit(1)
        if exc.code == 404:
            # Asked once, a 4xx being final. The name was checked before any
            # request, so the revision is what has no such list.
            print(f"  No file list for {plat} at {RELEASE_REF}.", file=sys.stderr)
            if RELEASE_REF != DEFAULT_RELEASE_REF:
                print(f"  RETROBIOS_REF={RELEASE_REF} names a tag that does not exist or "
                      f"predates {plat}: unset RETROBIOS_REF, or pick a tag from "
                      "https://github.com/Abdess/retrobios/releases.", file=sys.stderr)
            else:
                print("  The repository may be moving its files: run the same command "
                      "again in a few minutes.", file=sys.stderr)
            sys.exit(1)
        print(f"  Could not fetch the file list for {plat}: HTTP {exc.code}", file=sys.stderr)
        for line in _address_lines(url):
            print(line, file=sys.stderr)
        print(NETWORK_ADVICE, file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        code, words = network_words(exc)
        EVENTS.emit("error", stage="manifest", reason=code, platform=plat,
                    message=words if code != "network"
                    else f"the file list could not be fetched: {words}")
        if code != "network":
            print(f"  Could not fetch the file list for {plat}.", file=sys.stderr)
            print(f"  {words}.", file=sys.stderr)
            sys.exit(1)
        print(f"  Could not fetch the file list for {plat} after {MAX_RETRIES} "
              f"attempts: {words}", file=sys.stderr)
        for line in _address_lines(url):
            print(line, file=sys.stderr)
        print(NETWORK_ADVICE, file=sys.stderr)
        sys.exit(1)


def fetch_targets(plat: str) -> "dict | None":
    """Download the target core list.

    Empty on 404, where the platform publishes no targets. None when the list
    could not be read: a timeout says nothing about the platform, and
    answering "no targets" for it sent users to the full pack.
    """
    url = TARGETS_URL.format(platform=plat)
    try:
        return _validate_targets(_fetch_json(url, MAX_TARGETS_BYTES, "target list"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        print(f"  Error: failed to fetch targets for {plat}: {exc}", file=sys.stderr)
        for line in _address_lines(url):
            print(line, file=sys.stderr)
        print(NETWORK_ADVICE, file=sys.stderr)
        return None
    except ValueError as exc:
        print(f"  Error: the target list for {plat} is not valid: {exc}", file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError) as exc:
        print(f"  Error: failed to fetch targets for {plat}: "
              f"{network_words(exc)[1]}", file=sys.stderr)
        for line in _address_lines(url):
            print(line, file=sys.stderr)
        print(NETWORK_ADVICE, file=sys.stderr)
        return None


def _for_cores(entry: dict, chosen: set[str]) -> dict | None:
    """The copy of a core file the chosen cores read, or None.

    A destination holds one file. When several cores read it and disagree
    on its content, the manifest carries the first core's copy and the
    others as alternatives, in the order the pack builder tries them: a
    pack built without the first core carries the next one's copy.
    """
    if chosen.intersection(entry.get("cores") or []):
        return entry
    for alternative in entry.get("alternatives", []):
        if chosen.intersection(alternative["cores"]):
            kept = {
                k: v for k, v in entry.items()
                if k not in _COPY_FIELDS
                and k not in ("alternatives", "slot_choices", "declared_choices")
            }
            kept.update((k, alternative[k]) for k in _COPY_FIELDS if k in alternative)
            return kept
    return None


def _settle_slot(
    entry: dict,
    chosen: set[str],
    kept_system: Callable[[str], bool] | None = None,
) -> dict:
    """A platform file as a pack built for the chosen cores carries it.

    Where a core's own source contradicts the platform and the frontend only
    checks the path, the pack serves the core's copy: the first of
    `slot_choices` naming a chosen core decides. Otherwise the platform
    answers, through the first of `declared_choices` whose systems remain.
    """
    declared = entry.get("declared_choices")
    if declared is None:
        return entry
    copy = next(
        (c for c in entry.get("slot_choices", []) if chosen.intersection(c["cores"])),
        None,
    )
    if copy is None or "sha1" not in copy:
        copy = next(
            (c for c in declared
             if kept_system is None or any(kept_system(s) for s in c["systems"])),
            declared[0],
        )
    kept = {
        k: v for k, v in entry.items()
        if k not in _COPY_FIELDS and k not in ("slot_choices", "declared_choices")
    }
    kept.update((k, copy[k]) for k in _COPY_FIELDS if k in copy and k != "cores")
    kept["cores"] = None
    return kept


def _filter_by_target(
    files: list[dict], target_cores: list[str]
) -> list[dict]:
    """Keep the platform's files, and the core files the given cores read."""
    chosen = set(target_cores)
    result: list[dict] = []
    for f in files:
        if f.get("cores") is None:
            result.append(_settle_slot(f, chosen))
            continue
        kept = _for_cores(f, chosen)
        if kept is not None:
            result.append(kept)
    return result


def designated_cores(manifest: dict, names: list[str]) -> set[str]:
    """The cores a list of names designates, the way the pack builder reads it.

    A target list, or a core found installed, names a core the buildbot's
    way (`mednafen_psx`), and a file names the profile that reads it
    (`beetle_psx`). `core_names` gives each profile its other names.
    """
    wanted = set(names)
    chosen = set(wanted)
    for profile, aliases in (manifest.get("core_names") or {}).items():
        if wanted.intersection(aliases):
            chosen.add(profile)
    return chosen


def narrow_to_cores(
    manifest: dict,
    names: list[str],
    files: list[dict] | None = None,
    omitted: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Keep what the named cores read, as a pack built for them carries.

    A file a core loads stays when that core is named. A file of the
    platform's own list stays when one of its systems does: a system goes
    when none of the cores `system_cores` gives it is named, and a system
    it does not list stays whatever is named. A dropped system's file gives
    way to the first named core claiming its destination. A manifest without
    the two maps keeps the platform's list whole and matches cores by name.
    """
    cores = designated_cores(manifest, names)
    reach = manifest.get("system_cores") or {}

    def kept_system(system: str) -> bool:
        return system not in reach or bool(cores.intersection(reach[system]))

    displaced: list[dict] = []

    def kept(entries: list[dict]) -> list[dict]:
        result: list[dict] = []
        for entry in entries:
            if entry.get("cores") is not None:
                copy = _for_cores(entry, cores)
                if copy is not None:
                    result.append(copy)
                    continue
                # None of the cores served here is named: a named core that
                # wants the slot and has nothing held is a file left out.
                claim = next(
                    (c for c in entry.get("absent_claims", [])
                     if cores.intersection(c.get("cores") or [])),
                    None,
                )
                if claim is not None:
                    displaced.append({"dest": entry["dest"], **claim})
                continue
            # The systems declaring the file, where they are fewer than the
            # ones reading it.
            systems = entry.get("declared_in") or entry_systems(entry)
            if not systems or any(kept_system(s) for s in systems):
                result.append(_settle_slot(entry, cores, kept_system))
                continue
            # The system is dropped, which frees its destination for the
            # first named core claiming it, as in a pack built for them.
            copy = _for_cores(entry, cores)
            if copy is not None:
                result.append(copy)
        return result

    files = manifest.get("files", []) if files is None else files
    omitted = manifest.get("omitted_files", []) if omitted is None else omitted
    kept_files = kept(files)
    return kept_files, kept(omitted) + displaced


def canonical_region(raw: str) -> str:
    """One region name or alias, spelled the way manifests spell it."""
    key = raw.strip().lower()
    key = REGION_ALIASES.get(key, key)
    if key not in REGIONS:
        import difflib  # noqa: PLC0415

        close = difflib.get_close_matches(key, [*REGION_ALIASES, *REGIONS], n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ValueError(
            f"unknown region: {raw.strip()}.{hint} "
            f"Short forms: {', '.join(REGION_ALIASES)}; regions: {', '.join(sorted(REGIONS))}"
        )
    return key


def _region_comparable(a: str, b: str) -> bool:
    return (
        a == b
        or _REGION_PARENT.get(a) == b
        or _REGION_PARENT.get(b) == a
        or a in AREA_TERRITORIES.get(b, ())
        or b in AREA_TERRITORIES.get(a, ())
    )


def _region_rank(file_regions: set[str], requested: list[str]) -> int:
    for index, wanted in enumerate(requested):
        if any(_region_comparable(wanted, region) for region in file_regions):
            return index
    return len(requested)


def region_drops(
    entries: list[dict],
    requested: list[str],
    systems: list[str] | None = None,
    held: set[str] | None = None,
) -> set[str]:
    """Destinations a region priority list withdraws, as _entry_key spells them.

    The pack builder's rule (region.resolve_region_drops), replayed over the
    groups the manifest records. Per group, the best-ranked regional file
    stays. A world file beats unmatched regional ones the code does not search
    before it (`priority`, lower first). When nothing matches and no world
    file exists, every regional file stays, so a group is never emptied. A
    file with no region never competes and always stays.

    A core extra competes whatever systems are chosen, while a platform's own
    declaration competes only when its system is kept, as in the pack.

    ``held`` names the destinations the manifest can install. A match it
    lacks never displaces a held file: the American BIOS stays under
    --region japan when the Japanese one is missing. A group named after a
    single file (FILE_GROUP) competes with nothing and keeps nothing.
    """
    if not requested:
        return set()
    kept_systems = set(systems) if systems else None
    groups: dict[str, list[tuple[set, str, object]]] = {}
    for entry in entries:
        regions = entry.get("regions")
        if not regions:
            continue
        joined = list(entry.get("region_groups") or [])
        joined += [
            group for group in entry.get("region_system_groups") or []
            if kept_systems is None or group in kept_systems
        ]
        priority = entry.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            priority = None
        for group in joined:
            groups.setdefault(group, []).append((set(regions), _entry_key(entry), priority))
    keep: set[str] = set()
    drop: set[str] = set()
    for group_id, members in groups.items():
        if FILE_GROUP in group_id:
            continue
        world = {dest for regions, dest, _p in members if WORLD_REGION in regions}
        ranks = {dest: priority for _regions, dest, priority in members}
        regional = [
            (_region_rank(regions, requested), dest)
            for regions, dest, _p in members
            if WORLD_REGION not in regions
        ]
        keep |= world
        if not regional:
            continue
        matched = [(rank, dest) for rank, dest in regional if rank < len(requested)]
        winners, rivals = matched, world
        if held is not None:
            winners = [(rank, dest) for rank, dest in matched if dest in held]
            rivals = world & held
        if winners:
            best = min(rank for rank, _dest in winners)
            keep |= {
                dest for rank, dest in matched
                if rank == best or (rank < best and dest not in (held or ()))
            }
            drop |= {dest for _rank, dest in regional if dest not in keep}
        elif rivals:
            world_ranks = [ranks[dest] for dest in rivals]
            best_world = min(world_ranks) if None not in world_ranks else None
            for _rank, dest in regional:
                ours = ranks[dest]
                if best_world is not None and ours is not None and ours < best_world:
                    keep.add(dest)
                else:
                    drop.add(dest)
        else:
            keep |= {dest for _rank, dest in regional}
    return drop - keep


def entry_systems(entry: dict) -> list[str]:
    """The platform systems a file or an omission belongs to.

    An omission's `system` is spelled the way its profile spells it. Only
    `systems` names the platform's own, the ones --system accepts.
    """
    return list(entry.get("systems") or [])


def narrow(
    files: list[dict],
    omitted: list[dict],
    systems: list[str],
    cores: list[str],
    regions: list[str],
) -> tuple[list[dict], list[dict]]:
    """Keep what the chosen systems, cores and regions need.

    A core choice keeps the platform's own list and narrows only the files
    cores load, the rule --target already follows. Regions are then decided
    the way the pack builder decides them under --system: over the chosen
    systems' declarations and every core extra. The system filter comes last.
    """
    if cores:
        files = _filter_by_target(files, cores)
        omitted = _filter_by_target(omitted, cores)
    if regions:
        # The builder's held set: what the collection serves. An omission
        # answers the request but takes no installable file's place, unless
        # the builder holds it (held: true) and only this download lacks it:
        # the pack drops the fallback it displaces, and so does this.
        held = {_entry_key(f) for f in files} | {
            _entry_key(o) for o in omitted if o.get("held")
        }
        dropped = region_drops(files + omitted, regions, systems, held)
        files = [f for f in files if _entry_key(f) not in dropped]
        omitted = [o for o in omitted if _entry_key(o) not in dropped]
    if systems:
        wanted = set(systems)
        files = [f for f in files if _chosen(f, wanted)]
        omitted = [o for o in omitted if _chosen(o, wanted)]
    return files, omitted


def _chosen(entry: dict, wanted: set[str]) -> bool:
    systems = entry_systems(entry)
    if not systems:
        return OTHER_SYSTEMS in wanted
    return bool(wanted & set(systems))


def records_selection(files: list[dict]) -> bool:
    """Whether a manifest carries what narrowing reads. Older ones do not."""
    return any("systems" in entry for entry in files)


def available_choices(files: list[dict], omitted: list[dict]) -> dict[str, list[str]]:
    """The systems, cores and regions a manifest lets one narrow to."""
    systems = {s for e in files + omitted for s in entry_systems(e)}
    ordered = sorted(systems)
    if any(not entry_systems(f) for f in files):
        ordered.append(OTHER_SYSTEMS)
    cores = {
        c for f in files
        for copy in [f, *f.get("alternatives", []), *f.get("slot_choices", [])]
        for c in (copy.get("cores") or [])
    }
    regions = {
        r for e in files + omitted for r in (e.get("regions") or [])
        if r != WORLD_REGION
    }
    return {
        "systems": ordered,
        "cores": sorted(cores),
        "regions": sorted(regions),
    }


def resolve_choices(
    values: list[str], known: list[str], label: str,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    """Names given on the command line, matched without regard to case.

    Each value is one name or a comma-separated list. A whole value that is
    itself a name wins over splitting it: Recalbox names one system
    `msx1,msx2,msxturbor`. An unknown name is refused, never ignored, since
    carrying on would install something other than what was asked.
    aliases maps another name of a known one to it, as Batocera's `psx`
    stands for sony-playstation. A known name always wins over an alias.
    """
    aliases = {alias: name for alias, name in (aliases or {}).items() if name in known}
    by_lower = {name.lower(): name for name in known}
    for alias, name in aliases.items():
        by_lower.setdefault(alias.lower(), name)
    # Spelled as the site writes it (Sony PlayStation), a name is still one
    # name when no other one reads the same once case and spacing go.
    by_key: dict[str, str | None] = {}
    for name in known:
        key = _name_key(name)
        by_key[key] = None if key in by_key else name
    for alias, name in aliases.items():
        by_key.setdefault(_name_key(alias), name)
    chosen: list[str] = []
    unknown: list[str] = []
    for value in values:
        value = value.strip()
        tokens = [value] if value.lower() in by_lower else value.split(",")
        for token in (t.strip() for t in tokens):
            if not token:
                continue
            name = by_lower.get(token.lower()) or by_key.get(_name_key(token))
            if name is None:
                unknown.append(token)
            elif name not in chosen:
                chosen.append(name)
    if unknown:
        if not known:
            raise ValueError(f"unknown {label}: {', '.join(unknown)}. This list records none.")
        listing = {"system": "--list-systems", "emulator": "--list-emulators"}.get(
            label, "--list-cores")
        hints, inside_from = aliases, 4
        if label == "system":
            # gba is the end of nintendo-gba: three letters are enough here.
            hints = {**{short: name for short, name in SYSTEM_SHORT_NAMES.items()
                        if name in known}, **aliases}
            inside_from = 3
        raise ValueError(
            f"unknown {label}: {', '.join(unknown)}. "
            f"{did_you_mean(unknown, known, listing, hints, inside_from)}"
        )
    if values and not chosen:
        raise ValueError(f"--{label} needs at least one name")
    return chosen


def resolve_regions(values: list[str]) -> list[str]:
    """A region priority list, best first, from names or aliases."""
    chosen: list[str] = []
    for value in values:
        for token in value.split(","):
            if token.strip():
                region = canonical_region(token)
                if region not in chosen:
                    chosen.append(region)
    if values and not chosen:
        raise ValueError("--region needs at least one region")
    return chosen


def parse_selection(text: str, count: int, ordered: bool = False) -> list[int]:
    """Numbers typed at a prompt: `3`, `1,4`, `2-5`, or `all`, 1-based.

    Returned sorted, or in the typed order when the order is a priority.
    """
    if text.strip().lower() in ("all", "*"):
        return list(range(1, count + 1))
    chosen: list[int] = []
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        low, sep, high = token.partition("-")
        try:
            start = int(low)
            end = int(high) if sep else start
        except ValueError:
            raise ValueError(f"not a number or range: {token}") from None
        if start > end or start < 1 or end > count:
            raise ValueError(f"out of range: {token} (1-{count})")
        for number in range(start, end + 1):
            if number not in chosen:
                chosen.append(number)
    return chosen if ordered else sorted(chosen)


def _digest_file(path: Path, algorithms: tuple[str, ...]) -> dict[str, str]:
    """Compute several digests of a file in a single read.

    Checking a 3 GB collection against both a SHA-1 and a SHA-256 used to walk
    every file twice, single threaded. Hashing is cheap next to the I/O, so
    the read is what has to happen once.
    """
    with open(path, "rb") as fh:
        return _digest_handle(fh, algorithms)


def _digest_handle(fh: BinaryIO, algorithms: tuple[str, ...]) -> dict[str, str]:
    """Digests of an open file, read from its start."""
    hashers = {name: hashlib.new(name) for name in algorithms}
    fh.seek(0)
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        for hasher in hashers.values():
            hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def _sha1_file(path: Path) -> str:
    """Compute SHA1 of a file."""
    return _digest_file(path, ("sha1",))["sha1"]


def _sha256_file(path: Path) -> str:
    """Compute SHA256 of a file."""
    return _digest_file(path, ("sha256",))["sha256"]


def _cfg_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def save_directory_for(
    bios_path: Path, manifest: dict, override: Path | None = None,
) -> SavePlace | str:
    """The save directory the platform's frontend hands its cores, or why
    this run cannot tell.

    A RetroArch found by its retroarch.cfg answers from it: savefile_directory
    as RetroArch expands it, a value left at default or naming no folder
    being saves/ beside the config (configuration.c config_load_file keeps
    the default unless path_is_directory), and sort_savefiles_enable, true by
    default (config.def.h:1099). savefiles_in_content_dir and
    sort_savefiles_by_content_enable put the directory beside each game: no
    single place then. Without a config, the BIOS folder standing at its
    default name puts the save directory beside it, as the pack lays them out,
    and a list whose dests start above the BIOS folder (RetroDECK) puts it
    in the folder the run installs to. ``override`` (--save-dir) names the directory itself. A launcher that
    writes the save directory at each launch, one folder per system
    (by_system: Batocera, RetroBat), is not read from any retroarch.cfg:
    the one on disk holds the last launch's system.
    """
    layout = manifest.get("save_directory")
    if not layout:
        return "the file list names no save directory for this platform"
    folder = layout["folder"]
    listed = bool(layout["by_core"])
    by_core = listed
    found = None if layout.get("by_system") else _RETROARCH_CONFIGS.get(bios_path)
    if found is not None:
        cfg_path, app_dir = found
        for key in ("savefiles_in_content_dir", "sort_savefiles_by_content_enable"):
            if _cfg_true(_android_cfg_value(cfg_path, key)):
                return (f"RetroArch keeps saves beside each game ({key} in {cfg_path}): "
                        "name the folder with --save-dir")
        sort = _android_cfg_value(cfg_path, "sort_savefiles_enable")
        if sort is not None:
            by_core = _cfg_true(sort)
        if override is not None:
            return SavePlace(override, by_core, listed)
        directory = _retroarch_cfg_dir(cfg_path, "savefile_directory", app_dir, folder)
        if directory is None or not directory.is_dir():
            directory = app_dir / folder
        return SavePlace(directory, by_core, listed)
    if override is not None:
        return SavePlace(override, by_core, listed)
    base = manifest.get("base_destination") or ""
    if base and bios_path.name == base:
        return SavePlace(bios_path.parent / folder, by_core, listed)
    if not base:
        # The dests start below the folder holding the BIOS folder
        # (RetroDECK's ~/retrodeck holds bios/ and saves/).
        return SavePlace(bios_path / folder, by_core, listed)
    return (f"{bios_path} is not the frontend's default BIOS folder: "
            "name its save directory with --save-dir")


def place_save_entries(
    files: list[dict], bios_path: Path, manifest: dict, override: Path | None,
    display: str,
) -> tuple[list[dict], list[dict]]:
    """Find the save directory for the entries read below it.

    Returns the files to install and, when the save directory is unknown,
    the entries left out as omissions, said once with the reason.
    """
    saved = [entry for entry in files if entry.get("root") == "save"]
    if not saved:
        return files, []
    place = save_directory_for(bios_path, manifest, override)
    if isinstance(place, SavePlace):
        _SAVE_DIRS[bios_path] = place
        where = str(place.directory)
        if (manifest.get("save_directory") or {}).get("by_system"):
            where += f"{os.sep}<system>"
        if place.by_core:
            where += f"{os.sep}<core>"
        print(f"  {_plural(len(saved), 'file')} read from {display}'s save directory "
              f"go to {where}")
        return files, []
    print(f"  {_plural(len(saved), 'file')} read from {display}'s save directory "
          f"left out: {place}")
    kept = [entry for entry in files if entry.get("root") != "save"]
    unplaced = [
        {"dest": entry["dest"], "name": PurePosixPath(entry["dest"]).name,
         "system": "", "required": False, "reason": "no_save_directory",
         "cores": entry.get("cores"), "root": "save",
         **({"core_folder": entry["core_folder"]} if entry.get("core_folder") else {})}
        for entry in saved
    ]
    return kept, unplaced


def _classify_local(entry: dict, dest: Path) -> str:
    """Return 'up_to_date' or 'mismatched' for a file already on disk."""
    expected = {
        name: entry.get(name, "").lower()
        for name in ("sha256", "sha1")
        if entry.get(name)
    }
    if not expected:
        return "up_to_date"
    actual = _digest_file(dest, tuple(expected))
    if all(actual[name] == value for name, value in expected.items()):
        return "up_to_date"
    return "mismatched"


def check_local(
    files: list[dict], bios_path: Path, jobs: int = 8,
    term: "Terminal | None" = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Check which files exist locally and have correct hashes.

    Returns (to_download, up_to_date, mismatched). With a terminal, a count
    moves while the files are read: 3.9 GB on an SD card went by in minutes
    of silence, read as a hung run and stopped with Ctrl+C.
    """
    to_download: list[dict] = []
    present: list[tuple[dict, Path]] = []

    for f in files:
        dest = _entry_path(bios_path, f)
        if dest.exists():
            present.append((f, dest))
        else:
            to_download.append(f)

    up_to_date: list[dict] = []
    mismatched: list[dict] = []
    if present:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            verdicts = pool.map(
                lambda item: (item[0], _classify_local(item[0], item[1])), present
            )
            live = term is not None and term.live and not term.quiet
            last = evented = 0.0
            for done, (entry, verdict) in enumerate(verdicts, 1):
                if verdict == "up_to_date":
                    up_to_date.append(entry)
                else:
                    mismatched.append(entry)
                now = time.monotonic()
                # The program driving the installer gets the count the
                # terminal gets: a screen with a bare timer over 6 GB on an
                # SD card read as hung.
                if EVENTS.on and (now - evented >= EVENT_PROGRESS_INTERVAL
                                  or done == len(present)):
                    evented = now
                    EVENTS.emit("checking", files_done=done, files_total=len(present))
                if live and (now - last >= 0.1 or done == len(present)):
                    last = now
                    print(f"\r  read {done}/{len(present)}", end="", file=term.stream,
                          flush=True)
            if live:
                print("\r\033[K", end="", file=term.stream, flush=True)

    return to_download, up_to_date, mismatched


# Where a file in place that this run replaces is kept: a folder beside the
# one written to, never inside it. Several emulators find their BIOS by
# content, not by name: DuckStation, SwanStation and Rustation read every
# file of the BIOS folder, BizHawk walks its firmware folder recursively. A
# kept copy anywhere under that folder could be taken for the BIOS.
BACKUP_DIR = "retrobios-backup"
# Written into every run folder the installer creates. --restore only accepts
# a folder holding it, so a folder someone else placed (a shared BIOS folder
# on a network share) is never copied over the user's files.
BACKUP_MARKER = ".retrobios-backup"


def _check_holder(holder: Path) -> os.stat_result:
    """Refuse a backup holder the user's files must not go into.

    A link would send them wherever it points. On a system with user ids, a
    folder another user owns was not made by this user's runs, and its owner
    could read or swap what is moved in. Returns what was checked, so the
    caller can confirm the same folder is still there once it has written
    into it.
    """
    info = os.lstat(holder)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ELOOP, f"{holder} is a link or not a folder")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise OSError(errno.EPERM, f"{holder} belongs to another user")
    return info


def _same_folder(path: Path, checked: os.stat_result) -> bool:
    """Whether path is still the folder checked, not a link swapped in since."""
    now = os.lstat(path)
    return (stat.S_ISDIR(now.st_mode)
            and (now.st_dev, now.st_ino) == (checked.st_dev, checked.st_ino))


class _NotKept(Exception):
    """The file in place differs and could not be set aside: it stays."""


class KeptFiles:
    """The files this run moved aside before replacing them.

    A file is kept at <holder>/<run>/<folder name>/<path below the folder>,
    the holder being retrobios-backup beside the folder written to. The run
    folder is named by date and time, so a second run never mixes with the
    first, and copying its contents into the holder's parent puts every file
    back where it was. Only when nothing can be written beside the folder
    (a drive root, a read-only parent) does the holder go inside it, with
    no folder name, and the same copy puts the files back.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stamp = ""
        # Holder -> the run folder this run created in it.
        self._runs: dict[Path, Path] = {}
        # Run folder -> [files, bytes] kept (or, under --dry-run, to keep).
        self.counts: dict[Path, list[int]] = {}

    def reset(self) -> None:
        with self._lock:
            self.stamp = ""
            self._runs.clear()
            self.counts.clear()

    def _stamp(self) -> str:
        if not self.stamp:
            self.stamp = time.strftime("%Y%m%d-%H%M%S")
        return self.stamp

    @staticmethod
    def _holders(folder: Path) -> list[tuple[Path, str]]:
        beside = [(folder.parent / BACKUP_DIR, folder.name)] if (
            folder.name and folder.parent != folder) else []
        return beside + [(folder / BACKUP_DIR, "")]

    def place(self, folder: Path, create: bool) -> tuple[Path, Path]:
        """(run folder, folder the relative path goes under) for files of folder."""
        with self._lock:
            stamp = self._stamp()
            failure: OSError | None = None
            for holder, prefix in self._holders(folder):
                if not create:
                    return holder / stamp, holder / stamp / prefix
                run = self._runs.get(holder)
                if run is None:
                    try:
                        holder.mkdir(parents=True, exist_ok=True)
                        checked = _check_holder(holder)
                        for n in range(1, 1000):
                            run = holder / (stamp if n == 1 else f"{stamp}-{n}")
                            try:
                                run.mkdir(mode=0o700)
                                break
                            except FileExistsError:
                                continue
                        else:
                            # Every name taken: never reuse a folder this run
                            # did not create.
                            raise OSError(errno.EEXIST, f"no free folder name in {holder}")
                        # The holder checked before is still the one the run
                        # folder was made in, and the run folder is a folder.
                        if not (_same_folder(holder, checked)
                                and stat.S_ISDIR(os.lstat(run).st_mode)):
                            raise OSError(errno.ELOOP, f"{holder} changed while in use")
                        (run / BACKUP_MARKER).write_text(
                            "Files the RetroBIOS installer replaced.\n", encoding="utf-8")
                    except OSError as exc:
                        failure = exc
                        continue
                    self._runs[holder] = run
                return run, run / prefix
            raise failure or OSError(errno.EEXIST, "no backup folder name left")

    def plan(self, folder: Path, size: int) -> Path:
        """Count a file a real run would keep, writing nothing. Returns its run folder."""
        run, _under = self.place(folder, create=False)
        with self._lock:
            tally = self.counts.setdefault(run, [0, 0])
            tally[0] += 1
            tally[1] += size
        return run

    def keep(
        self, folder: Path, rel: PurePosixPath, dest: Path,
        expected: dict[str, str], fresh: Path | None = None,
    ) -> None:
        """Move the file at dest aside when its content is not the one coming.

        Nothing is done for a missing file, a link (its target is not
        touched by the replace) or a file already holding the content, by
        the manifest's hashes or, without them, byte for byte against the
        fresh copy. Raises _NotKept when the file could not be set aside:
        the replace must not happen then.
        """
        try:
            info = os.lstat(dest)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise _NotKept(exc.strerror or str(exc)) from exc
        if not stat.S_ISREG(info.st_mode):
            return
        try:
            if expected:
                same = _digest_file(dest, tuple(expected)) == expected
            else:
                same = fresh is not None and _same_bytes(dest, fresh)
        except FileNotFoundError:
            return  # another run replaced it meanwhile
        except OSError as exc:
            raise _NotKept(exc.strerror or str(exc)) from exc
        if same:
            return
        try:
            run, under = self.place(folder, create=True)
            kept = under / Path(*rel.parts)
            if os.path.lexists(kept):
                # This run kept the user's file at that path already: the
                # one in place now is one this run wrote.
                return
            kept.parent.mkdir(parents=True, exist_ok=True)
            _move_aside(dest, kept)
        except FileNotFoundError:
            if not os.path.lexists(dest):
                return  # another run moved or replaced it meanwhile
            raise _NotKept("the backup folder could not be written") from None
        except OSError as exc:
            raise _NotKept(exc.strerror or str(exc)) from exc
        with self._lock:
            tally = self.counts.setdefault(run, [0, 0])
            tally[0] += 1
            tally[1] += info.st_size

    def summary(self) -> list[dict]:
        """[{folder, files, bytes}], one per run folder, for the report and events."""
        with self._lock:
            return [{"folder": str(run), "files": files, "bytes": size}
                    for run, (files, size) in sorted(self.counts.items())]


KEPT = KeptFiles()


def _same_bytes(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return _digest_file(a, ("sha1",)) == _digest_file(b, ("sha1",))


def _move_aside(dest: Path, kept: Path) -> None:
    """Move dest to kept: a rename on one drive, else a copy then a delete.

    The copy goes under a temporary name first, so an interrupted copy
    never leaves a cut file under the kept name, and dest is only deleted
    once its copy is whole.
    """
    try:
        os.rename(dest, kept)
        return
    except OSError:
        if not dest.is_file():
            raise
    scratch = kept.parent / f".{kept.name}.part"
    try:
        shutil.copy2(dest, scratch)
        os.replace(scratch, kept)
    except OSError:
        if os.path.lexists(scratch):
            os.unlink(scratch)
        raise
    dest.unlink()


def _kept_slot(root: Path, value: object) -> tuple[Path, PurePosixPath]:
    """The folder a manifest destination is written in, and its path below
    it, as _destination_path places it: a RetroDECK folder moved elsewhere
    is its own folder."""
    relative = _safe_relative_path(value, "dest")
    moved = _MOVED_FOLDERS.get(root, {})
    if len(relative.parts) > 1 and relative.parts[0] in moved:
        return moved[relative.parts[0]].resolve(), PurePosixPath(*relative.parts[1:])
    return root.resolve(), relative


def plan_kept(entries: list[dict], root: Path) -> dict[Path, list[int]]:
    """Count, under --dry-run, the files in place a real run would keep.

    entries are files whose content check failed. Returns {run folder:
    [files, bytes]} for these entries, also counted in KEPT for the summary.
    """
    planned: dict[Path, list[int]] = {}
    for entry in entries:
        try:
            dest = _entry_path(root, entry)
            info = os.lstat(dest)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode):
            run = KEPT.plan(_kept_slot(*_entry_place(root, entry))[0], info.st_size)
            tally = planned.setdefault(run, [0, 0])
            tally[0] += 1
            tally[1] += info.st_size
    return planned


def _kept_lines() -> list[str]:
    """Where the files this run replaced are, and how to put them back."""
    lines = []
    for item in KEPT.summary():
        folder = Path(item["folder"])
        lines.append(f"Kept {_plural(item['files'], 'file')} you had "
                     f"({format_size(item['bytes'])}), which the collection's "
                     f"copies replaced, in {folder}")
        lines.append(f"  To put them back: {_INVOCATION.launcher()} --restore "
                     f"{_INVOCATION.word(str(folder))}")
    return lines


def restore_kept(run: Path, dry_run: bool = False) -> int:
    """Put every file of a backup run folder back where it was.

    run is a <holder>/<date-time> folder a run wrote, its holder named
    retrobios-backup. Each file goes to the same path below the holder's
    parent, replacing what is there. The backup is left in place. Returns
    the exit code: 0 when every file went back, 1 when one failed, 2 when
    the folder is no backup.
    """
    run = Path(os.path.abspath(os.path.expanduser(str(run))))
    if (not run.is_dir() or run.is_symlink() or run.parent.name != BACKUP_DIR
            or run.parent.is_symlink() or not (run / BACKUP_MARKER).is_file()):
        print(f"Error: {run} is not a folder the installer kept files in.", file=sys.stderr)
        print(f"  Name one of the dated folders inside a {BACKUP_DIR} folder, as the "
              "end of the install printed it.", file=sys.stderr)
        return 2
    root = run.parent.parent
    # Links inside the backup are never followed: os.walk does not descend
    # into a linked folder and a linked file is skipped.
    files = sorted(
        Path(base) / name
        for base, _dirs, names in os.walk(run, followlinks=False)
        for name in names
        if not (Path(base) / name).is_symlink()
        and not (name.startswith(".") and name.endswith(".part"))
        and not (Path(base) == run and name == BACKUP_MARKER)
    )
    if not files:
        print(f"{run} holds no file. Nothing to put back.")
        return 0
    restored = size = failed = 0
    for src in files:
        dest = root / src.relative_to(run)
        if dry_run:
            print(f"  would put back {dest}")
            restored += 1
            size += src.stat().st_size
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _clear_read_only(dest)
            _replace_with_copy(src, dest)
        except OSError as exc:
            print(f"  failed: {dest} ({exc.strerror or exc})", file=sys.stderr)
            failed += 1
            continue
        restored += 1
        size += src.stat().st_size
    if dry_run:
        print(f"\nWould put back {_plural(restored, 'file')} ({format_size(size)}) "
              f"under {root}. Nothing was written.")
        return 0
    print(f"\nPut back {_plural(restored, 'file')} ({format_size(size)}) under {root}.")
    if failed:
        print(f"{_plural(failed, 'file')} could not be put back.", file=sys.stderr)
    print(f"The copies are still in {run}. Delete that folder once you no longer need it.")
    return 1 if failed else 0


def _download_one(
    f: dict, bios_path: Path, verbose: bool = False,
    progress: Progress | None = None, copy: LocalCopy | None = None,
) -> tuple[str, bool, str]:
    """Download a single file, or copy it from where copy found it on this
    machine. Returns (dest, success, reason it failed)."""
    if _stop.is_set():
        return str(f.get("dest", "?")), False, "stopped"
    try:
        dest = _entry_path(bios_path, f)
    except ValueError:
        return str(f.get("dest", "?")), False, "unsafe destination"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if _write_refused(exc):
            return f["dest"], False, NOT_WRITABLE
        return f["dest"], False, exc.strerror or str(exc)

    if copy is not None:
        result = _copy_local(f, bios_path, dest, copy, progress)
        if result[1]:
            copy.index.count_copy(copy.misplaced)
            return result
        if result[2] in ("stopped", NOT_WRITABLE, READ_ONLY) or result[2].startswith(NOT_KEPT):
            return result
        # The copy went or changed since it was read (a card pulled out, a
        # file another run replaced): the file is fetched as if never found,
        # and a fetch that fails too names both reasons.
        if verbose:
            print(f"    {f['dest']}: {result[2]}, fetching it instead", file=sys.stderr)
        fetched = _download_one(f, bios_path, verbose, progress)
        why = fetched[2]
        if (fetched[1] or why in ("stopped", NOT_WRITABLE, READ_ONLY)
                or why.startswith(NOT_KEPT)):
            return fetched
        both = f"{result[2]}, and {why}"
        return fetched[0], False, NetworkReason(both) if isinstance(why, NetworkReason) else both

    if LOCAL.pack is not None:
        return _extract_from_pack(f, bios_path, dest, progress)
    _network_allowed(f["dest"])
    if f.get("release_asset"):
        url = RELEASE_URL.format(asset=urllib.parse.quote(f["release_asset"], safe=""))
    else:
        url = RAW_FILE_URL.format(path=urllib.parse.quote(f["repo_path"], safe="/"))

    expected_size = f["size"]
    # A large file keeps a fixed partial name so the next attempt, or the
    # next run, asks only for what is missing.
    resumable = expected_size >= RESUME_FROM_SIZE
    reason = "download failed"
    refused = False
    full_partial = False
    for attempt in range(1, MAX_RETRIES + 1):
        tmp_path: Path | None = None
        counted = 0
        resumed_count = 0
        partial = None
        try:
            offset = 0
            if resumable:
                tmp_path = dest.parent / f".{dest.name}.part"
                _partial_size(tmp_path)
                # Two runs into one BIOS folder used to append to the same
                # partial copy: the one that finds it locked downloads into
                # a private file instead.
                partial = _claim_partial(tmp_path)
                if partial is None:
                    tmp_path = None
                else:
                    offset = os.fstat(partial.fileno()).st_size
                    if offset >= expected_size and _NO_RENAME_WHILE_OPEN:
                        # The run that verified this copy releases its lock
                        # to rename it: truncating it there destroyed that
                        # copy and failed the rename. It is left alone and
                        # this run writes a private file.
                        partial.close()
                        partial = None
                        tmp_path = None
                        offset = 0
                        full_partial = True
                    elif offset >= expected_size:
                        partial.truncate(0)
                        offset = 0
            request = urllib.request.Request(url)
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            with urllib.request.urlopen(request, timeout=60) as resp:
                if offset and getattr(resp, "status", 200) != 206:
                    offset = 0  # the server sent the whole file again
                    partial.truncate(0)
                raw_length = resp.headers.get("Content-Length") if resp.headers else None
                if raw_length and int(raw_length) != expected_size - offset:
                    raise ValueError(
                        f"Content-Length {raw_length} != expected {expected_size - offset}"
                    )
                if partial is not None:
                    partial.seek(offset)
                    out = _Unclosed(partial)
                else:
                    out = tempfile.NamedTemporaryFile(
                        mode="wb", dir=dest.parent, prefix=f".{dest.name}.",
                        suffix=".part", delete=False,
                    )
                    tmp_path = Path(out.name)
                with out:
                    downloaded = offset
                    if progress is not None and offset:
                        progress.add(offset, resumed=True)
                        counted += offset
                        resumed_count = offset
                    while True:
                        if _stop.is_set():
                            raise _Interrupted()
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > expected_size or downloaded > MAX_DOWNLOAD_SIZE:
                            raise ValueError("download exceeded declared size")
                        out.write(chunk)
                        if progress is not None:
                            progress.add(len(chunk))
                            counted += len(chunk)
                if downloaded != expected_size:
                    raise ValueError(
                        f"downloaded {downloaded} bytes, expected {expected_size}"
                    )

            expected = {
                name: f[name].lower() for name in ("sha256", "sha1") if f.get(name)
            }
            # A large file is hashed through the handle that holds its lock.
            # On Windows that lock is mandatory: reopening the path to read
            # it failed with a sharing violation, which read as an unwritable
            # folder for every file of 16 MB or more (issue 82).
            if partial is not None:
                actual = _digest_handle(partial, tuple(expected))
            else:
                actual = _digest_file(tmp_path, tuple(expected))
            if "sha256" in expected and actual["sha256"] != expected["sha256"]:
                reason = "SHA-256 mismatch"
                raise _Corrupt()
            if "sha1" in expected and actual["sha1"] != expected["sha1"]:
                reason = "SHA-1 mismatch"
                raise _Corrupt()

            # The mode a plain open() gives, executable where the repository
            # file is: the temporary file is private, and a BIOS folder
            # written with sudo has to stay readable by the emulator's user.
            os.chmod(tmp_path, (0o777 if f.get("executable") else 0o666) & ~_UMASK)
            _clear_read_only(dest)
            # The file in place, a personal dump or another revision, is
            # moved aside first: replaced, it was gone for good.
            KEPT.keep(*_kept_slot(*_entry_place(bios_path, f)), dest, expected, tmp_path)
            if partial is not None and _NO_RENAME_WHILE_OPEN:
                # Windows renames no open file. Elsewhere the lock is held
                # until the copy has its final name.
                partial.close()
                _replace_released(tmp_path, dest, expected)
            else:
                os.replace(tmp_path, dest)
            if full_partial:
                _drop_full_partial(dest, expected_size)
            return f["dest"], True, ""

        except _Interrupted:
            # Stopped mid-transfer: a large file keeps its partial copy for
            # the next run, and a small one is dropped rather than left beside
            # the destination.
            if tmp_path is not None and partial is None and tmp_path.exists():
                tmp_path.unlink()
            return f["dest"], False, "stopped"

        except _NotKept as exc:
            # No attempt changes it, and the file in place stays as it is.
            if partial is None and tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()
            return f["dest"], False, f"{NOT_KEPT} ({exc})"

        except _Corrupt:
            if verbose:
                print(f"    {reason} on attempt {attempt}", file=sys.stderr)
            # Closed before the delete: Windows refuses to remove a file
            # still open (WinError 32), and the error ended the run.
            if partial is not None:
                partial.close()
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()
            delay = 0.0

        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
            if exc.code in (403, 429):
                reason += " (GitHub rate limit or access refused)"
            if exc.code >= 500 or exc.code == 429:
                reason = NetworkReason(reason)
            if verbose:
                print(f"    Attempt {attempt} failed: {exc}", file=sys.stderr)
            if tmp_path is not None and partial is None and tmp_path.exists():
                tmp_path.unlink()
            if exc.code == 416 and tmp_path is not None and tmp_path.exists():
                if partial is not None:
                    partial.close()
                tmp_path.unlink()  # the partial copy no longer fits the file
            delay = _retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)

        # http.client raises IncompleteRead (an HTTPException, not an
        # OSError) when a chunked transfer is cut, as a proxy re-encoding
        # the stream does: it is a dropped connection like any other.
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            if (isinstance(exc, OSError) and not isinstance(exc, urllib.error.URLError)
                    and _write_refused(exc)):
                # No attempt can change a refused write. A read-only file in
                # place is not a folder this user cannot write.
                reason = READ_ONLY if _read_only_file(dest) else NOT_WRITABLE
                refused = True
            elif _transfer_failure(exc):
                reason = NetworkReason(network_words(exc)[1])
            else:
                reason = str(getattr(exc, "reason", "") or exc)
            if verbose:
                print(f"    Attempt {attempt} failed: {exc}", file=sys.stderr)
            if not refused and tmp_path is not None and partial is None and tmp_path.exists():
                tmp_path.unlink()
            delay = _retry_delay(attempt, None)

        finally:
            if partial is not None:
                partial.close()

        if progress is not None and counted:
            progress.add(-counted)
            if resumed_count:
                progress.forget_resumed(resumed_count)
        if refused:
            return f["dest"], False, reason
        if attempt < MAX_RETRIES and delay and _stop.wait(delay):
            return f["dest"], False, "stopped"

    return f["dest"], False, reason


# Windows renames no file another handle holds open, and Python opens files
# without FILE_SHARE_DELETE: a locked partial copy is closed before its rename.
_NO_RENAME_WHILE_OPEN = os.name == "nt"
# Seconds a closed partial copy waits for a sharing violation to clear: a
# second run closing it again, or a scanner reading the new file.
RELEASE_WAIT = 30.0
_SHARING_VIOLATIONS = (32, 33)


def _replace_released(source: Path, dest: Path, expected: dict[str, str]) -> None:
    """Rename a verified copy whose lock was just released.

    Another run may open the copy in that moment, which refuses the rename
    with a sharing violation until it lets go. When the copy is gone, that
    run placed the same file and removed it: the destination is checked
    against the manifest's hashes before it counts as installed.
    """
    deadline = time.monotonic() + RELEASE_WAIT
    while True:
        try:
            os.replace(source, dest)
            return
        except FileNotFoundError:
            if expected and dest.is_file() and _digest_file(dest, tuple(expected)) == expected:
                return
            raise
        except PermissionError as exc:
            if (getattr(exc, "winerror", None) not in _SHARING_VIOLATIONS
                    or time.monotonic() >= deadline):
                raise
            time.sleep(0.2)


def _drop_full_partial(dest: Path, expected_size: int) -> None:
    """Remove a complete partial copy this run left alone.

    A copy at full size that nothing holds is what a run stopped between its
    check and its rename leaves behind. The destination now holds the
    verified file, so the copy is only clutter. One another run holds is
    left to it.
    """
    path = dest.parent / f".{dest.name}.part"
    if not path.is_file():
        return
    try:
        handle = _claim_partial(path)
    except OSError:
        return
    if handle is None:
        return
    with handle:
        complete = os.fstat(handle.fileno()).st_size >= expected_size
    if complete:
        try:
            path.unlink()
        except OSError:
            pass  # another run opened it meanwhile and finishes with it
    else:
        try:
            if path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass  # created by the claim and taken since by another run


def _partial_size(path: Path) -> int:
    """Bytes already held by a partial copy. Anything but a plain file is dropped."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    path.unlink()
    return 0


def _claim_partial(path: Path) -> BinaryIO | None:
    """The partial copy, opened and locked for this run, or None when
    another run holds it. A link planted at its name is not followed."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    handle = os.fdopen(os.open(path, flags, 0o644), "r+b")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    # Opened and locked are two steps: in between, the run that held the
    # partial may have renamed it into place and released it. The lock then
    # sits on the installed BIOS, which looks like a finished partial and
    # would be truncated. Only the file still under the partial's name is ours.
    try:
        ours = os.path.samestat(os.fstat(handle.fileno()), os.lstat(path))
    except OSError:
        ours = False
    if not ours:
        handle.close()
        return None
    return handle


class _Unclosed:
    """The locked partial copy, written through without closing it."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle

    def __enter__(self) -> BinaryIO:
        return self._handle

    def __exit__(self, *_exc: object) -> None:
        self._handle.flush()

    def write(self, data: bytes) -> int:
        return self._handle.write(data)


class _Corrupt(Exception):
    """The bytes arrived but are not the file the manifest names."""


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds before the next attempt: what the server asks, else 1, 2, 4."""
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 60.0)
        except ValueError:
            pass
    return float(min(2 ** (attempt - 1), 8))


def _locations(paths: list[Path]) -> str:
    """Where the run wrote, naming every destination when it served several."""
    return ", ".join(str(p) for p in paths) if paths else "nowhere"


# A write refused by the file system: retrying cannot change it, and the
# summary names the two ways out.
NOT_WRITABLE = "folder not writable"


def _write_refused(exc: OSError) -> bool:
    # A sharing violation is a file another process holds open, which a
    # later attempt can find released: no refusal of the folder.
    if getattr(exc, "winerror", None) in _SHARING_VIOLATIONS:
        return False
    return isinstance(exc, PermissionError) or exc.errno == errno.EROFS


# A file already at the destination, marked read-only, whose content differs,
# and whose attribute could not be cleared: replacing it is refused while its
# folder stays writable.
READ_ONLY = "file in place is read-only"
# A file in place whose content differs, which could not be moved aside: it
# is left as it is rather than lost.
NOT_KEPT = "file in place could not be kept aside"


def _clear_read_only(dest: Path) -> None:
    """Let the verified copy replace a read-only file the user already has.

    Windows refuses to replace a file carrying the read-only attribute
    (WinError 5) even in a writable folder. The file is about to be replaced
    by the copy the manifest names, so its attribute is cleared first.
    Elsewhere a rename only needs the folder to be writable.
    """
    if os.name != "nt":
        return
    try:
        if dest.is_file() and not os.access(dest, os.W_OK):
            os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass  # the replace that follows reports the refusal with its reason


def _read_only_file(dest: Path) -> bool:
    return dest.is_file() and not os.access(dest, os.W_OK) and _folder_writable(dest.parent)


def _folder_writable(path: Path) -> bool:
    """Whether a file can be created under path, read from its nearest
    existing folder: a BIOS folder not created yet is as writable as the
    folder it would go in."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return os.access(probe, os.W_OK)


# Options that take a value, as a hint gives them back.
_VALUED_OPTIONS = (
    "--system", "--core", "--emulator", "--region", "--target", "--cores-dir", "--save-dir",
    "--from-pack", "--from", "--manifest",
)


class Invocation:
    """How this run was started, so that a hint gives the command back whole.

    A one-liner user has no install.py to run: install.sh and install.ps1 run
    a temporary copy and say so in RETROBIOS_BOOTSTRAP. And a hint that
    dropped --system turned a retry of 2 files into one of 8069.
    """

    def __init__(self) -> None:
        self.os_type = ""
        self.options: list[tuple[str, str | None]] = []
        # Per platform, what was chosen at the questions.
        self.chosen: dict[str, list[tuple[str, str | None]]] = {}

    def record(self, args: argparse.Namespace) -> None:
        """Keep every option that decides what a run installs or reports."""
        options: list[tuple[str, str | None]] = []
        for flag in _VALUED_OPTIONS:
            value = getattr(args, flag[2:].replace("-", "_"), None)
            values = value if isinstance(value, list) else [value] if value else []
            options += [(flag, str(v)) for v in values]
        for flag, on in (
            ("--every-board", args.every_board),
            ("--installed-cores", args.installed_cores and not args.cores_dir),
            ("--no-game-data", args.no_game_data),
            ("--standalone-copies", args.standalone_copies),
            ("--check", args.check),
            ("--dry-run", args.dry_run),
        ):
            if on:
                options.append((flag, None))
        self.options = options

    def powershell(self) -> bool:
        return os.environ.get("RETROBIOS_BOOTSTRAP") == "ps1" or self.os_type == "windows"

    def launcher(self, elevated: bool = False) -> str:
        """The command without its options, as the user typed it."""
        bootstrap = os.environ.get("RETROBIOS_BOOTSTRAP", "")
        if bootstrap == "ps1":
            return f"&([scriptblock]::Create((irm {BOOTSTRAP_URL}.ps1)))"
        if bootstrap == "sh":
            return f"curl -fsSL {BOOTSTRAP_URL}.sh | {'sudo ' if elevated else ''}sh -s --"
        python = "python" if self.os_type == "windows" else "python3"
        return f"{'sudo ' if elevated else ''}{python} install.py"

    def word(self, value: str) -> str:
        if self.powershell():
            # A comma makes an array and @ a splat in PowerShell arguments.
            if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value):
                return value
            return "'" + value.replace("'", "''") + "'"
        return shlex.quote(value)

    def command(
        self, plat: str, dest: Path | str | None, elevated: bool = False,
        without: tuple[str, ...] = (),
    ) -> str:
        """The full command for plat and dest, a str dest being a placeholder."""
        words = [self.launcher(elevated)]
        if plat:
            words += ["--platform", plat]
        if isinstance(dest, Path):
            words += ["--dest", self.word(str(dest.absolute()))]
        elif dest:
            words += ["--dest", dest]
        for flag, value in [*self.options, *self.chosen.get(plat, [])]:
            if flag in without:
                continue
            words += [flag] if value is None else [flag, self.word(value)]
        return " ".join(words)


_INVOCATION = Invocation()


def _on_read_only_mount(path: Path) -> bool:
    """Whether the file system holding path, or the folder it would go in,
    is mounted read-only (a card Linux remounts read-only after an error)."""
    if not hasattr(os, "statvfs"):
        return False
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return bool(os.statvfs(probe).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _write_advice(path: Path, assumed: bool, plat: str = "") -> str:
    """What to do about a folder this run could not write.

    sudo fixes a root-owned BIOS folder on the console itself. On another
    machine it would create that folder on the wrong disk: a platform named
    but not found here is a card or share mounted elsewhere, named by --dest.
    Windows has no sudo, and Termux reaches shared storage once
    termux-setup-storage has run. A read-only mount refuses root as well,
    so sudo is never offered for it.
    """
    if _on_read_only_mount(path):
        again = f"\n    {_INVOCATION.command(plat, path)}" if plat else ""
        return (f"  {path} is on a file system mounted read-only, which no user can "
                "write, sudo included. Mount it read-write again (a card or drive "
                "the system switched to read-only after an error needs a check "
                "first: fsck, or chkdsk on Windows), then run the same command "
                f"again.{again}")
    if assumed:
        return (f"  {path} is where the platform keeps its BIOS on its own system, "
                "and it was not found here. If this machine prepares its card or "
                "share, run it again with --dest pointing at that folder:\n"
                f"    {_INVOCATION.command(plat, '<that folder>')}")
    if plat and _INVOCATION.os_type == "windows":
        return ("  This user cannot write there. Run it again from a PowerShell window "
                "opened as administrator:\n"
                f"    {_INVOCATION.command(plat, path)}\n"
                "  Or choose another folder with --dest.")
    if plat and _INVOCATION.os_type == "android":
        return ("  This user cannot write there. Run termux-setup-storage once to give "
                "Termux the shared storage, then run it again:\n"
                f"    {_INVOCATION.command(plat, path)}\n"
                "  Or choose another folder with --dest.")
    if plat:
        # sudo resets HOME to root's on Debian, Ubuntu, Arch and SteamOS:
        # the same command detected nothing there, fell back to a folder
        # under /root and reported success.
        return ("  This user cannot write there. Run it again with sudo, naming the "
                "folder so root does not look in its own home:\n"
                f"    {_INVOCATION.command(plat, path, elevated=True)}\n"
                "  Or choose another folder with --dest.")
    if _INVOCATION.os_type == "windows":
        return ("  This user cannot write there: run the same command from a PowerShell "
                "window opened as administrator, or choose another folder with --dest.")
    return ("  This user cannot write there: run the same command with sudo, "
            "or choose another folder with --dest.")


def _plural(count: int, word: str) -> str:
    """Count and noun, so a run of one file does not read as machine output."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


class NetworkReason(str):
    """A failure the connection caused: cut, timed out, refused by a server
    or a proxy. The summary names the network instead of the folder."""


def _transfer_failure(exc: BaseException) -> bool:
    """Whether a download error came from the connection, not the disk."""
    # urlopen raises URLError for the name lookup, the connection and TLS.
    # A cut read surfaces as a timeout (socket.timeout is no TimeoutError
    # before 3.10), a reset, an SSL error or a size that does not add up.
    ssl_error = getattr(sys.modules.get("ssl"), "SSLError", ())
    if isinstance(exc, (urllib.error.URLError, http.client.HTTPException, ValueError,
                        ConnectionError, TimeoutError, socket.timeout)):
        return True
    if ssl_error and isinstance(exc, ssl_error):
        return True
    return isinstance(exc, OSError) and exc.errno in (
        errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED, errno.ETIMEDOUT,
        errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN, errno.EPIPE,
    )


NETWORK_FAILURE_ADVICE = (
    "  The transfers were cut or refused on the way: check the connection, and "
    "HTTPS_PROXY if this network needs a proxy.\n"
    f"  More help: {TROUBLESHOOTING_PAGE}")


def _report_outcome(
    paths: list[Path],
    downloaded: int,
    up_to_date: int,
    errors: int,
    omitted: int,
    interrupted: bool,
    failures: list[tuple[str, str]] | None = None,
    transferred: int = 0,
    seconds: float = 0.0,
    declined: int = 0,
    assumed: set[Path] | None = None,
    omitted_entries: list[dict] | None = None,
    platform_of: dict[Path, str] | None = None,
    hints: list[str] | None = None,
    sources: str = "",
) -> None:
    """Close the run on what happened, and leave with a status that says so.

    A run that installed nothing used to end on the word Done and a zero exit,
    which reads as success to a person and to a script alike.
    """
    if transferred and seconds > 0:
        verb = "copied from the pack" if LOCAL.pack is not None else "downloaded"
        print(f"\n{format_size(transferred)} {verb} in "
              f"{format_duration(seconds)} ({format_size(int(transferred / seconds))}/s).")
    reasons = {reason for _dest, reason in failures or []}
    if failures and len(failures) > 1 and len(reasons) == 1 and reasons != {""}:
        # One cause, said once: a read-only folder listed ten of its files
        # and "... and 266 more" above the advice that fixes them all.
        print(f"  failed: {_plural(len(failures), 'file')} ({reasons.pop()}), "
              f"first {failures[0][0]}")
    else:
        for dest, reason in (failures or [])[:10]:
            print(f"  failed: {dest}" + (f" ({reason})" if reason else ""))
        if failures and len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more")
    if declined and not downloaded and not errors and not interrupted:
        print(f"\nNothing installed: {_plural(declined, 'file')} not downloaded, "
              "as answered.")
        print(f"{_plural(up_to_date, 'file')} already up to date at {_locations(paths)}.")
        return
    kept = _kept_lines()
    if interrupted:
        print(f"\nStopped. {_plural(downloaded, 'file')} installed.")
        for line in kept:
            print(line)
        print("Run the same command again to carry on from here. Nothing is lost.")
        sys.exit(130)

    # A standalone copy fails by absolute path, in an emulator's folder
    # rather than the BIOS folder: that folder is where the write went.
    elsewhere = sorted({Path(dest).parent for dest, _reason in failures or []
                        if Path(dest).is_absolute()})
    refused = any(reason == NOT_WRITABLE for dest, reason in failures or []
                  if not Path(dest).is_absolute())
    advice = [
        _write_advice(path, path in (assumed or set()), (platform_of or {}).get(path, ""))
        for path in paths
    ] if refused else []
    advice += [_write_advice(folder, False) for folder in sorted({
        Path(dest).parent for dest, reason in failures or []
        if Path(dest).is_absolute() and reason == NOT_WRITABLE
    })]
    if any(reason == READ_ONLY for _dest, reason in failures or []):
        advice.append("  Files marked read-only are in the way: clear that attribute "
                      "(or delete them), then run again.")
    if any(isinstance(reason, NetworkReason) for _dest, reason in failures or []):
        advice.append(NETWORK_FAILURE_ADVICE)
    paths = paths + [folder for folder in elsewhere if folder not in paths]

    if errors and not downloaded:
        print(f"\nNothing was installed. {_plural(errors, 'file')} failed.")
        if failures and all(isinstance(r, NetworkReason) for _d, r in failures):
            # The folder was never the problem: naming it as the place being
            # written to sent people looking at their disk.
            print(f"Destination: {_locations(paths)}")
        else:
            print(f"Location that was being written to: {_locations(paths)}")
        for line in kept + advice:
            print(line)
        print("Run the same command again once that is resolved. "
              "Files already installed are kept.")
        sys.exit(2)

    if errors:
        print(f"\nFinished with errors. {_plural(downloaded, 'file')} installed, "
              f"{_plural(errors, 'file')} failed.")
        if sources:
            print(f"  {sources}")
        print(f"Location: {_locations(paths)}")
        for line in kept + advice:
            print(line)
        print("Run the same command again to retry only those.")
        sys.exit(1)

    print(f"\nDone. {_plural(downloaded, 'file')} installed, "
          f"{_plural(up_to_date, 'file')} already up to date.")
    if sources:
        print(f"  {sources}")
    print(f"Location: {_locations(paths)}")
    for line in kept:
        print(line)
    if omitted:
        for line in _omission_lines(omitted, omitted_entries or []):
            print(line)
    for line in hints or []:
        print(line)
    print("To check this later, run the same command with --check.")


def _omission_lines(count: int, entries: list[dict]) -> list[str]:
    """What was skipped, the files an emulator cannot start without first.

    "395 file are not in the collection yet" named nothing and gave one
    reason for all, while the manifest says which file and why.
    """
    one = count == 1
    lines = [f"{_plural(count, 'file')} {'was' if one else 'were'} not installed: "
             f"the collection does not hold {'it' if one else 'them'}, or its copy "
             f"fails {'its' if one else 'their'} check."]
    lines += _needed_lines([e for e in entries if e.get("required")],
                           "Needed to start an emulator")
    return lines


def _absent_required(omitted: list[dict], bios_path: Path) -> list[dict]:
    """Required entries the manifest leaves out that the user has not placed."""
    absent = []
    for entry in omitted:
        dest = entry.get("dest") or entry.get("name", "")
        if not entry.get("required", True) or not dest:
            continue
        try:
            placed = _entry_path(bios_path, {**entry, "dest": dest}).is_file()
        except ValueError:
            # A path climbing out of the folder is never one we read, and one
            # below a save directory this run does not know is not looked at.
            placed = False
        if not placed:
            absent.append(entry)
    return absent


def _needed_lines(entries: list[dict], heading: str, every: bool = False) -> list[str]:
    """Required files the collection lacks, by system with the emulators reading them.

    A bare list (DARK.GOB, Data.rsdk, ... and 69 more) left a Batocera user
    unable to tell whether their PlayStation was concerned.
    """
    by_system: dict[str, tuple[set[str], set[str]]] = {}
    for entry in entries:
        system = str(entry.get("system") or ", ".join(entry_systems(entry)) or OTHER_SYSTEMS)
        names, cores = by_system.setdefault(system, (set(), set()))
        names.add(str(entry.get("dest") or entry.get("name")))
        cores.update(str(core) for core in entry.get("cores") or [])
    if not by_system:
        return []
    count = sum(len(names) for names, _cores in by_system.values())
    lines = [f"  {heading} ({_plural(count, 'file')}):"]
    systems = sorted(by_system)
    for system in systems if every else systems[:8]:
        names, cores = by_system[system]
        readers = f" ({', '.join(sorted(cores))})" if cores else ""
        lines.append(f"    {system}{readers}: {', '.join(sorted(names))}")
    if not every and len(systems) > 8:
        lines.append(f"    ... and {len(systems) - 8} more systems (--check --verbose lists them all)")
    lines.append(f"  Each emulator's page says what these files are: {SITE_URL}emulators/")
    return lines


# Asked before, in an interactive run: past this size a download is worth a
# second thought, and --system, --core or --region can make it smaller.
CONFIRM_FROM_BYTES = 1024 * 1024 * 1024


def installer_id() -> str:
    """The first twelve hex digits of this file's SHA-256, as the bootstraps pin it."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except (OSError, NameError):
        return "unknown"


# Answers that stop: every menu before this one taught q as quit.
_REFUSALS = ("n", "q", "quit", "exit", "stop", "abort")


def _confirm(prompt: str) -> bool:
    """Yes unless the answer refuses: n, no, or one of the quit words."""
    answer = _ask(prompt).strip().lower()
    return not (answer.startswith("n") or answer in _REFUSALS)


def _print_plan(
    to_download: list[dict], bios_path: Path, term: Terminal,
    trees: list[tuple[dict, dict[str, dict]]] = (),
    kept: dict[Path, list[int]] | None = None,
    copied: list[str] = (),
) -> None:
    """What a real run would fetch, by system, and nothing written.

    The data trees count in the total and come before the closing line:
    printed after it, they left 2636 RetroArch files out of the figure a
    user plans an SD card from.
    """
    total = sum(f.get("size", 0) for f in to_download)
    trees = [(entry, needed) for entry, needed in trees if needed]
    tree_files = sum(len(needed) for _entry, needed in trees)
    tree_size = sum(meta.get("size", 0) for _entry, needed in trees for meta in needed.values())
    for line in copied:
        print(line)
    if not to_download and not trees:
        if not copied:
            print("\n  All files up to date. A real run would download nothing.")
            return
        print("\n  Nothing to download.")
    parts = []
    if to_download:
        parts.append(f"{_plural(len(to_download), 'file')} ({format_size(total)})")
    if trees:
        parts.append(f"{_plural(tree_files, 'data file')} ({format_size(tree_size)}, "
                     f"extracted from {_plural(len(trees), 'archive')})")
    if parts:
        print(f"\nWould download {' and '.join(parts)} to {bios_path}:")
    by_system: dict[str, list[dict]] = {}
    for entry in to_download:
        system = ", ".join(entry_systems(entry)) or "other"
        by_system.setdefault(system, []).append(entry)
    for system in sorted(by_system):
        print(f"  {term.bold(system)}  {_sized(by_system[system])}")
        for entry in sorted(by_system[system], key=lambda e: e["dest"])[:8]:
            print(f"    {entry['dest']}  {term.dim(format_size(entry.get('size', 0)))}")
        if len(by_system[system]) > 8:
            print(f"    ... and {len(by_system[system]) - 8} more")
    for entry, needed in trees:
        source = LOCAL.pack.name if LOCAL.pack is not None else entry["url"]
        print(f"  {term.bold(entry['ref'] + ' data')}  {_sized(list(needed.values()))}, "
              f"from {source}")
    for folder, (count, size) in sorted((kept or {}).items()):
        print(f"\n  Would keep {_plural(count, 'file')} you have ({format_size(size)}), "
              f"whose contents differ, in {folder}")
    print("\n  Nothing was written. Run without --dry-run to install.")


def _check_free_space(bios_path: Path, needed: int, plat: str = "", code: int = 2) -> None:
    """Stop before writing when the volume cannot hold the download.

    Reported against the nearest existing ancestor, since the destination
    may not exist yet. The run ends with code: 2 when nothing was installed,
    the status a script reads as "nothing installed", 1 after an earlier
    platform of the run installed files.
    """
    probe = bios_path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free >= needed:
        return
    print(f"\nNot enough space on the drive holding {bios_path}.", file=sys.stderr)
    print(f"  Needed: {format_size(needed)}. Free: {format_size(free)}.",
          file=sys.stderr)
    if not plat:
        print(f"  Free up {format_size(needed - free)}, or use --dest to install "
              "to another drive.", file=sys.stderr)
        sys.exit(code)
    # --dest alone used to mean RetroArch's list: the platform is named, so
    # a Batocera install moved to a USB drive stays Batocera's.
    print(f"  Free up {format_size(needed - free)}, or install to another drive:\n"
          f"    {_INVOCATION.command(plat, '<folder on another drive>')}",
          file=sys.stderr)
    sys.exit(code)


def download_files(
    files: list[dict], bios_path: Path, jobs: int = 8, verbose: bool = False,
    term: Terminal | None = None, progress: Progress | None = None,
    copies: dict[int, LocalCopy] | None = None,
) -> tuple[list[tuple[str, str]], bool]:
    """Download files in parallel.

    Returns the destinations that failed, each with its reason, and whether
    the run was interrupted. progress.installed counts the files written.
    Every future is submitted up front, so a bare KeyboardInterrupt would
    let the pool drain the queue in full: the stop flag is what makes Ctrl+C
    end the run rather than only stop the reporting.
    """
    failed: list[tuple[str, str]] = []
    interrupted = False
    _stop.clear()
    _writing.set()
    term = term or Terminal()
    if progress is None:
        progress = Progress(len(files), sum(f.get("size", 0) for f in files), term)

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        future_map = {
            pool.submit(_download_one, f, bios_path, verbose, progress,
                        (copies or {}).get(id(f))): f
            for f in files
        }
        refused = False
        refused_events = 0
        try:
            for future in concurrent.futures.as_completed(future_map):
                dest, success, reason = future.result()
                if refused:
                    # The folder they all go in refused the first: each
                    # would fail alike, counted without a line of its own,
                    # though a program reading the events still gets one.
                    if not success:
                        failed.append((dest, NOT_WRITABLE))
                        refused_events += 1
                        EVENTS.emit(
                            "downloaded", dest=dest, ok=False, reason=NOT_WRITABLE,
                            size=future_map[future].get("size", 0),
                            files_done=progress.files + refused_events,
                            files_total=progress.total_files, bytes_done=progress.bytes,
                            bytes_total=progress.expected_bytes,
                        )
                    continue
                if _stop.is_set():
                    continue
                progress.done(dest, success, reason)
                EVENTS.emit(
                    "downloaded", dest=dest, ok=success, reason=reason,
                    size=future_map[future].get("size", 0), files_done=progress.files,
                    files_total=progress.total_files, bytes_done=progress.bytes,
                    bytes_total=progress.expected_bytes,
                )
                if not success:
                    failed.append((dest, reason))
                    if reason == NOT_WRITABLE and not _folder_writable(bios_path):
                        # A read-only card or share: hundreds of identical
                        # FAILED lines buried the one cause and its advice.
                        refused = True
                        _stop.set()
        except KeyboardInterrupt:
            interrupted = True
            _stop.set()
            progress.finish()
            print("\n  Stopping. Files already installed are kept.")
    # The pool has run every future by now: a file that landed after Ctrl+C
    # is on disk and counts.
    progress.installed = sum(
        1 for future in future_map
        if not future.cancelled() and future.exception() is None and future.result()[1]
    )
    progress.finish()
    return failed, interrupted


def _fetch_archive(url: str, directory: Path) -> Path:
    """A data directory's source archive, in *directory*, retried like files."""
    _network_allowed(url)
    reason: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        handle = tempfile.NamedTemporaryFile(dir=directory, suffix=".archive", delete=False)
        try:
            with handle, urllib.request.urlopen(url, timeout=60) as resp:
                received = 0
                while True:
                    if _stop.is_set():
                        raise _Interrupted()
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_DATA_ARCHIVE_SIZE:
                        raise ValueError("archive exceeds the size limit")
                    handle.write(chunk)
            return Path(handle.name)
        except urllib.error.HTTPError as exc:
            Path(handle.name).unlink(missing_ok=True)
            if exc.code < 500 and exc.code != 429:
                raise
            reason = exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            Path(handle.name).unlink(missing_ok=True)
            reason = exc if isinstance(exc, OSError) else ConnectionError(repr(exc))
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        if attempt < MAX_RETRIES:
            time.sleep(_retry_delay(attempt, None))
    raise reason if reason else OSError("download failed")


def _archive_members(
    archive: Path, entry: dict
) -> Iterator[tuple[str, int, Callable[[], BinaryIO]]]:
    """(path in the tree, declared size, opener) for each file the archive
    maps into it.

    The rules refresh_data_dirs applies to build the cache: a ZIP drops
    strip_components leading parts, a tarball keeps what sits under
    source_path, and a first part named in exclude is left out.
    """
    exclude = set(entry.get("exclude") or [])
    if entry["type"] == "zip":
        strip = entry.get("strip_components", 0)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.split("/")
                if ".." in parts or info.filename.startswith("/"):
                    continue
                if strip:
                    if len(parts) <= strip:
                        continue
                    parts = parts[strip:]
                if parts[0] in exclude:
                    continue
                yield "/".join(parts), info.file_size, (lambda info=info: zf.open(info))
        return
    prefix = entry["source_path"].rstrip("/") + "/"
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf:
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            rel = member.name[len(prefix):]
            if not rel or ".." in rel.split("/") or rel.split("/")[0] in exclude:
                continue
            yield rel, member.size, (lambda member=member: tf.extractfile(member))


def data_directory_state(entry: dict, bios_path: Path) -> tuple[Path, dict[str, dict], int]:
    """(tree root, files missing or wrong, files already right)."""
    root = _destination_path(bios_path, entry["dest"]) if entry.get("dest") else bios_path.resolve()
    needed: dict[str, dict] = {}
    present = 0
    for rel, meta in entry["files"].items():
        target = _destination_path(root, rel)
        if (
            target.is_file()
            and target.stat().st_size == meta["size"]
            and _sha1_file(target) == meta["sha1"]
        ):
            present += 1
        else:
            needed[rel] = meta
    return root, needed, present


def install_data_directory(entry: dict, bios_path: Path) -> dict:
    """Rebuild one data tree from its source archive, file by file verified.

    Returns counts: present, installed, changed (the source no longer holds
    the recorded bytes), absent (not in the archive), error, the reason
    the archive could not be read, and not_kept, the (path, reason) of each
    file in place that differs and could not be moved aside, left as it is.
    """
    root, needed, present = data_directory_state(entry, bios_path)
    counts = {"present": present, "installed": 0, "changed": 0, "absent": 0, "error": "",
              "not_kept": []}
    if not needed:
        return counts
    _writing.set()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as scratch:
            if LOCAL.pack is not None:
                # The pack holds the tree where it extracts, already mapped.
                members = LOCAL.pack.tree(entry.get("dest", ""))
            else:
                archive = _fetch_archive(entry["url"], Path(scratch))
                members = _archive_members(archive, entry)
            for rel, declared, opener in members:
                meta = needed.pop(rel, None)
                if meta is None:
                    continue
                # Read no more than the list records: the archive's own size
                # field is checked first, and the stream is cut at the
                # expected size whatever the member claims, so a member that
                # inflates without end costs one byte past it.
                if declared != meta["size"]:
                    counts["changed"] += 1
                    continue
                target = _destination_path(root, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha1()
                size = 0
                with opener() as src, tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=f".{target.name}.", suffix=".part",
                    delete=False,
                ) as out:
                    while size <= meta["size"]:
                        chunk = src.read(min(1024 * 1024, meta["size"] + 1 - size))
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                if digest.hexdigest() == meta["sha1"] and size == meta["size"]:
                    slot = f"{entry['dest']}/{rel}" if entry.get("dest") else rel
                    try:
                        KEPT.keep(*_kept_slot(bios_path, slot), target,
                                  {"sha1": meta["sha1"]})
                    except _NotKept as exc:
                        Path(out.name).unlink(missing_ok=True)
                        counts["not_kept"].append((rel, str(exc)))
                        continue
                    os.replace(out.name, target)
                    counts["installed"] += 1
                else:
                    Path(out.name).unlink(missing_ok=True)
                    counts["changed"] += 1
    except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile,
            tarfile.TarError) as exc:
        counts["error"] = str(getattr(exc, "reason", None) or exc)
    counts["absent"] = len(needed)
    return counts


def select_data_directories(manifest: dict, files: list[dict], narrowed: bool) -> list[dict]:
    """The data trees this install keeps: all, or those of a kept system."""
    entries = manifest.get("data_directories") or []
    if not narrowed:
        return list(entries)
    kept = {system for f in files for system in entry_systems(f)}
    return [e for e in entries if kept & set(e.get("systems") or [])]


def _copy_targets(
    entry: dict, os_type: str, extra_dirs: dict[str, Path] | None = None,
) -> list[Path]:
    """The emulator folders a standalone copy entry names on this OS.

    WSL falls back to the Linux ones, and extra_dirs adds the folder a
    frontend such as LaunchBox keeps the emulator in.
    """
    targets = entry.get("targets", {}).get(os_type, [])
    if not targets and os_type == "wsl":
        targets = entry.get("targets", {}).get("linux", [])
    target_dirs = [Path(os.path.expandvars(os.path.expanduser(t))) for t in targets]
    extra_root = (extra_dirs or {}).get(entry.get("emulator", ""))
    if extra_root is not None:
        subdir = PurePosixPath(entry.get("file", "")).parent
        extra = extra_root if str(subdir) == "." else extra_root / subdir
        if extra not in target_dirs:
            target_dirs.append(extra)
    return target_dirs


def standalone_folders_here(
    manifest: dict, os_type: str, extra_dirs: dict[str, Path] | None = None,
) -> list[Path]:
    """The emulator folders of the manifest's standalone copies that exist
    on this machine: the ones --standalone-copies would write to."""
    found: list[Path] = []
    for entry in manifest.get("standalone_copies", []):
        if "note" in entry:
            continue
        for folder in _copy_targets(entry, os_type, extra_dirs):
            if folder not in found and folder.is_dir():
                found.append(folder)
    return found


# The file lists a machine with standalone emulators and no platform is
# served from: EmuDeck's holds the Switch keys, RetroArch's the PPSSPP atlas
# and the Flycast flash, and each copy entry names its folders.
STANDALONE_SOURCES = ("emudeck", "retroarch")


def standalone_plan(
    manifests: list[dict], os_type: str, extra_dirs: dict[str, Path] | None = None,
) -> list[tuple[Path, list[dict]]]:
    """What each emulator folder found here receives, by its own file name.

    The standalone copies of the manifests, read as do_standalone_copies
    reads them, but taken from the file lists rather than from a platform
    tree already installed: an ES-DE user with DuckStation and PCSX2 and no
    RetroArch had no tree to copy from. The first list that holds a name for
    a folder serves it.
    """
    from fnmatch import fnmatch  # noqa: PLC0415

    plan: dict[Path, dict[str, dict]] = {}
    for manifest in manifests:
        files = manifest.get("files", [])
        by_dest = {entry["dest"]: entry for entry in files}
        for copy in manifest.get("standalone_copies", []):
            if "note" in copy:
                continue
            if "pattern" in copy:
                sources = [entry for entry in files
                           if fnmatch(PurePosixPath(entry["dest"]).name, copy["pattern"])]
            else:
                held = by_dest.get(copy.get("source") or copy["file"])
                sources = [held] if held else []
            for folder in _copy_targets(copy, os_type, extra_dirs):
                if not sources or not folder.is_dir():
                    continue
                chosen = plan.setdefault(folder, {})
                for entry in sources:
                    name = PurePosixPath(entry["dest"]).name
                    chosen.setdefault(name, {**entry, "dest": name})
    return [(folder, list(entries.values())) for folder, entries in plan.items() if entries]


def _install_standalone_folders(
    args: argparse.Namespace, os_type: str, term: Terminal, interactive: bool,
) -> bool:
    """Fill the standalone emulator folders found here, with no platform.

    False when this machine has none, the run then going on to the platform
    question or hint.
    """
    manifests = []
    for plat in STANDALONE_SOURCES:
        manifests.append(fetch_manifest(plat))
    lb_root = launchbox_root(os_type)
    plan = standalone_plan(manifests, os_type,
                           launchbox_bios_dirs(lb_root) if lb_root else None)
    if not plan:
        return False
    print("  Found standalone emulator folders:")
    for folder, entries in plan:
        print(f"    {folder}  {_sized(entries)}")
    if interactive and not (args.check or args.dry_run or args.yes) and not _confirm(
        "\nInstall the BIOS files these emulators read into their folders? [Y/n] "
    ):
        return False
    downloaded = up_to_date_count = errors = 0
    pending = 0
    failures: list[tuple[str, str]] = []
    transferred, seconds = 0, 0.0
    interrupted = False
    for folder, entries in plan:
        print(f"\n{folder}:")
        to_download, up_to_date, mismatched = check_local(entries, folder, jobs=args.jobs)
        to_download.extend(mismatched)
        up_to_date_count += len(up_to_date)
        if args.check:
            for entry in mismatched:
                print(f"    wrong hash: {entry['dest']}")
            wrong = {id(entry) for entry in mismatched}
            missing = [entry for entry in to_download if id(entry) not in wrong]
            for entry in missing[:10] if not args.verbose else missing:
                print(f"    missing: {entry['dest']}")
            if not args.verbose and len(missing) > 10:
                print(f"    ... and {len(missing) - 10} more missing (--verbose lists them all)")
            if not to_download:
                print("  All files up to date.")
            pending += len(to_download)
            continue
        if args.dry_run:
            _print_plan(to_download, folder, term, kept=plan_kept(mismatched, folder))
            continue
        if not to_download:
            print("  All files up to date.")
            continue
        size = sum(entry.get("size", 0) for entry in to_download)
        _check_free_space(folder, size, code=1 if downloaded else 2)
        progress = Progress(len(to_download), size, term)
        failed, interrupted = download_files(
            to_download, folder, jobs=args.jobs, verbose=args.verbose,
            term=term, progress=progress,
        )
        downloaded += progress.installed
        errors += len(failed)
        failures.extend((str(folder / dest), reason) for dest, reason in failed)
        transferred += progress.transferred()
        seconds += progress.elapsed()
        if interrupted:
            break
    if args.check:
        if pending:
            print("\n  Run the same command without --check to fetch them; "
                  "a file with a wrong hash is replaced, and the one in place "
                  f"kept in {BACKUP_DIR} beside its folder.")
            sys.exit(1)
        return True
    if args.dry_run:
        return True
    _report_outcome(
        [folder for folder, _entries in plan], downloaded, up_to_date_count, errors,
        0, interrupted, failures=failures, transferred=transferred, seconds=seconds,
    )
    return True


def do_standalone_copies(
    manifest: dict, bios_path: Path, os_type: str,
    extra_dirs: dict[str, Path] | None = None,
    failures: list[tuple[str, str]] | None = None,
) -> tuple[int, int, int]:
    """Copy BIOS files to standalone emulator directories.

    Supports:
    - file: single file copy
    - pattern: glob match (e.g. "scph*.bin")
    - note: informational message when detect path exists
    - WSL fallback to linux targets

    extra_dirs maps the emulator id of an entry to a root that replaces the
    default per-OS locations, for setups such as LaunchBox that keep their
    emulators outside them. The layout below the root is the same.

    Returns (copied, skipped, failed): skipped is a target directory that
    does not exist, failed a copy the system refused (permission, disk
    full, read-only mount) or a name leading out of the folder, which used
    to be counted as "dir not found". A link already at a file's name is
    replaced by the file, never written through.
    Each failure is also appended to failures, by absolute path, for the
    summary to name.
    """
    from fnmatch import fnmatch

    copies = manifest.get("standalone_copies", [])
    if not copies:
        return 0, 0, 0

    copied = 0
    skipped = 0
    failed = 0

    for entry in copies:
        # Note entries: print message if emulator detected
        if "note" in entry:
            detect_paths = entry.get("detect", {}).get(os_type, [])
            if not detect_paths and os_type == "wsl":
                detect_paths = entry.get("detect", {}).get("linux", [])
            for dp in detect_paths:
                expanded = Path(os.path.expandvars(os.path.expanduser(dp)))
                if expanded.is_dir():
                    print(f"  {entry['note']}")
                    break
            continue

        if "pattern" in entry:
            # A kept file is never a source, when the backup had to go
            # inside the BIOS folder.
            sources = [
                f for f in bios_path.rglob("*")
                if fnmatch(f.name, entry["pattern"]) and f.is_file()
                and BACKUP_DIR not in f.relative_to(bios_path).parts
            ]
        else:
            # `source` is where this platform's manifest put the file, and `file`
            # names it as the registry does for every platform, and laid out
            # that way it was missing wherever the pack places it elsewhere.
            src = _destination_path(bios_path, entry.get("source") or entry["file"])
            sources = [src] if src.exists() else []

        if not sources:
            continue

        for target_dir in _copy_targets(entry, os_type, extra_dirs):
            if not target_dir.is_dir():
                skipped += len(sources)
                continue
            resolved_dir = target_dir.resolve()
            for src in sources:
                dest = target_dir / src.name
                linked = dest.is_symlink()
                # A crafted source name would climb out of the directory.
                if not linked and not _within(dest, resolved_dir):
                    print(f"  ERROR: {dest}: {OUTSIDE_TARGET}")
                    failed += 1
                    if failures is not None:
                        failures.append((str(dest), OUTSIDE_TARGET))
                    continue
                try:
                    KEPT.keep(resolved_dir, PurePosixPath(src.name), dest, {}, src)
                except _NotKept as exc:
                    print(f"  ERROR: {dest}: {NOT_KEPT} ({exc})")
                    failed += 1
                    if failures is not None:
                        failures.append((str(dest), f"{NOT_KEPT} ({exc})"))
                    continue
                try:
                    if linked:
                        # Writing through a link already at the name would
                        # leave the folder the user opted into. The link
                        # itself gives way to the file.
                        _replace_with_copy(src, dest)
                        print(f"  {dest}: link replaced by the file")
                    else:
                        shutil.copy2(src, dest)
                    copied += 1
                except OSError as exc:
                    print(f"  ERROR: {dest}: {exc.strerror or exc}")
                    failed += 1
                    if failures is not None:
                        reason = NOT_WRITABLE if _write_refused(exc) else str(
                            exc.strerror or exc)
                        failures.append((str(dest), reason))

    return copied, skipped, failed


OUTSIDE_TARGET = "name leads outside the emulator folder"


def _replace_with_copy(src: Path, dest: Path) -> None:
    """Put a copy of src at dest's name without following what is there."""
    fd, scratch = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    os.close(fd)
    try:
        shutil.copy2(src, scratch)
        os.replace(scratch, dest)
    except OSError:
        if os.path.lexists(scratch):
            os.unlink(scratch)
        raise


def format_size(n: int) -> str:
    """Human-readable file size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _choose_platform(title: str, default: str = "") -> str:
    """One platform from the numbered list, Enter taking the default."""
    print(f"\n{title}")
    for i, name in enumerate(AVAILABLE_PLATFORMS, 1):
        print(f"  {i}) {display_name(name)}")
    print("  q) quit")
    print()
    prompt = f"> [Enter for {display_name(default)}] " if default else "> "
    while True:
        choice = _ask(prompt)
        if not choice and default:
            return default
        if choice.lower() == "q":
            _quit()
        if choice.isdigit() and 1 <= int(choice) <= len(AVAILABLE_PLATFORMS):
            return AVAILABLE_PLATFORMS[int(choice) - 1]
        key = _name_key(choice)
        for plat in AVAILABLE_PLATFORMS:
            if key and key in (_name_key(plat), _name_key(display_name(plat))):
                return plat
        print(f"  Type a number from 1 to {len(AVAILABLE_PLATFORMS)}, a platform name, "
              "or q to quit.")


def _prompt_manual_platform(os_type: str) -> list[tuple[str, Path]]:
    """Ask for a platform and a BIOS directory when nothing was detected."""
    plat = _choose_platform("No supported platform found here. Choose the one to "
                            "install for:", "retroarch")
    default_dest = _default_dest(os_type, plat)
    raw = ""
    while not raw:
        raw = _ask(
            f"BIOS directory [{default_dest}]: " if default_dest
            else "BIOS directory (no default on this system): "
        )
        if default_dest is not None:
            break
    if raw:
        return [(plat, _typed_folder(raw))]
    state = "" if default_dest.exists() else ". It does not exist yet and will be created"
    print(f"  {display_name(plat)}'s default BIOS folder: {default_dest}{state}.")
    return [(plat, default_dest)]


def _typed_folder(raw: str) -> Path:
    """A folder typed at a question, read as a shell reads an argument.

    Explorer's "Copy as path" puts quotes around it, and users quote a path
    with spaces: kept, they sent the files into a folder named `"` under
    the current directory, and the run said Done.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    return Path(os.path.expandvars(os.path.expanduser(text)))


# Platforms with no install location of their own: the BIOS folder sits
# beside the executable, wherever the archive was extracted.
PORTABLE_ONLY = {
    "bizhawk": "BizHawk reads the Firmware folder beside EmuHawk, and none was found.",
}


def _default_dest(os_type: str, platform_name: str) -> Path | None:
    """Fall back path for a platform named on the command line but not found,
    or None where no folder on this OS is one the platform reads.

    A home directory is meaningless for RetroArch on Android, where Termux
    owns the home and the frontend reads from shared storage. ~/bios is read
    by no platform, RetroBat lives on Windows only, and a Batocera or Recalbox
    path on Windows resolved to the root of the current drive: 8.7 GB under
    C:\\userdata\\bios, reported done.
    """
    if os_type == "android" and platform_name == "retroarch":
        return _android_storage_root() / "RetroArch" / "system"
    if platform_name in PORTABLE_ONLY:
        return None
    own = _os_default_dests(os_type).get(platform_name)
    if own is not None:
        return own
    if os_type == "windows":
        return None
    if platform_name == "retrobat":
        return None
    return DEFAULT_DESTS.get(platform_name)


def _manual_usage_hint(os_type: str, listing: str = "") -> list[str]:
    """How to name the platform and directory when nothing is detected.

    In the form the run was started with: a one-liner user has no
    install.py, and `python3 install.py` failed with "can't open file".
    """
    example = {
        "windows": "C:\\RetroArch-Win64\\system",
        "android": "/storage/emulated/0/RetroArch/system",
    }.get(os_type, "~/.config/retroarch/system")
    hint = Invocation()
    hint.os_type, hint.options = os_type, _INVOCATION.options
    if listing:
        # A listing reads a platform's list and writes nothing: --dest is
        # not needed, and a rebuilt install command dropped the listing
        # and started a 6.5 GB download.
        return [
            "  Name the platform whose list to print, for example:",
            f"    {hint.command('retroarch', None)} {listing}",
            f"  The platforms: {hint.launcher()} --list-platforms",
        ]
    return [
        "  Use --platform and --dest to specify manually, for example:",
        f"    {hint.command('retroarch', example)}",
        f"  Where each platform keeps its BIOS: {INSTALLER_PAGE}",
    ]


def _prompt_platform_choice(
    platforms: list[tuple[str, Path]], title: str = "Install for:",
) -> list[tuple[str, Path]]:
    """Prompt user to choose among detected platforms."""
    print(f"\n{title}")
    for i, (name, path) in enumerate(platforms, 1):
        system = detected_os(name, path)
        label = f"{system}, {display_name(name)} files" if system and system != display_name(name) \
            else display_name(name)
        print(f"  {i}) {label} ({path})")
    last = len(platforms) + (1 if len(platforms) > 1 else 0)
    if len(platforms) > 1:
        print(f"  {last}) All")
    print("  q) quit")
    print()

    while True:
        choice = _ask("> ").lower()
        if choice == "q":
            _quit()
        idx = int(choice) if choice.isdigit() else 0
        if 1 <= idx <= len(platforms):
            return [platforms[idx - 1]]
        if idx == last and len(platforms) > 1:
            return platforms
        # A wrong answer used to bring back a bare prompt, and nothing said
        # how to leave it.
        print(f"  Type a number from 1 to {last}, or q to quit.")


def _ask(prompt: str) -> str:
    """An answer, or a stop: Ctrl+D at a question is Ctrl+C.

    Both used to leave with 0, so `install.sh && retroarch` went on with no
    BIOS. They now end where Ctrl+C ends everywhere else, on 130.
    """
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        raise KeyboardInterrupt from None


def _quit() -> None:
    """q at a question ends the run where Ctrl+C does, on 130: with 0,
    `install.sh && retroarch` started RetroArch on no BIOS."""
    sys.exit(130)


def _sized(files: list[dict]) -> str:
    return f"{_plural(len(files), 'file')}, {format_size(sum(f.get('size', 0) for f in files))}"


# A core whose files are all the platform's own. "pcsx_rearmed 0 files,
# 0 B" read as a core that gets nothing.
NO_CORE_FILES = "no extra file: it reads the platform's own, always kept"


def _core_sized(files: list[dict]) -> str:
    return _sized(files) if files else NO_CORE_FILES


def _system_sized(files: list[dict]) -> str:
    """A system's files, those other systems share counted apart.

    `3do 1704 files, 349.0 MB` read as the 3DO BIOS: 1689 of them are the
    MAME hash and plugin files six systems carry.
    """
    shared = [f for f in files if len(entry_systems(f)) > 1]
    if not shared:
        return _sized(files)
    if len(shared) == len(files):
        return f"{_sized(shared)}, all shared with other systems"
    own = [f for f in files if len(entry_systems(f)) <= 1]
    return f"{_sized(own)}, and {_sized(shared)} shared with other systems"


def _pick(
    title: str, rows: list[tuple[str, list[dict]]], ordered: bool = False,
    sizer: Callable[[list[dict]], str] = _sized,
) -> list[str]:
    """Numbered list of names. An empty answer keeps every one.

    sizer says what each name holds.
    """
    if not rows:
        return []
    print(f"\n{title}")
    width = len(str(len(rows)))
    name_width = min(max(len(name) for name, _files in rows), 32)
    for index, (name, files) in enumerate(rows, 1):
        print(f"  {index:>{width}}) {name:<{name_width}}  {sizer(files)}")
    hint = "order of preference, e.g. 2,1" if ordered else "e.g. 1,3-5"
    while True:
        text = _ask(f"Numbers ({hint}), Enter for all, q to quit: ")
        if not text:
            return []
        if text.lower() == "q":
            _quit()
        try:
            chosen = parse_selection(text, len(rows), ordered=ordered)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        if chosen:
            return [rows[number - 1][0] for number in chosen]


INSTALLED_ONLY = "installed"


def _prompt_custom_selection(
    plat: str, files: list[dict], omitted: list[dict],
    installed: tuple[list[dict], list[dict]] | None = None,
    name: str = "",
) -> tuple[list[str], list[str], list[str]] | str | None:
    """Offer to narrow the install by system, core and region.

    Enter installs everything, the default most runs want. Returns None
    then, INSTALLED_ONLY for what `installed` (the files the cores found
    installed read) holds, or the three choices. Each empty list keeps
    everything on its axis.
    """
    print(f"\n{name or display_name(plat)}: {_sized(files)}.")
    print("  Enter) install everything")
    if installed is not None:
        print(f"  i) only what the installed cores read: {_sized(installed[0])}")
    print("  c) choose systems, cores and regions")
    print("  q) quit")
    while True:
        answer = _ask("> ").lower()
        if answer == "":
            return None
        if answer == "q":
            _quit()
        if answer == "i" and installed is not None:
            return INSTALLED_ONLY
        if answer == "c":
            break
        keys = "Enter, i, c or q" if installed is not None else "Enter, c or q"
        print(f"  Type {keys}.")

    choices = available_choices(files, omitted)
    if OTHER_SYSTEMS in choices["systems"]:
        print(f"\n'{OTHER_SYSTEMS}' holds what cores load for systems this platform")
        print("does not list, such as game engines and extra computers.")
    systems = _pick("Systems:", [
        (name, [f for f in files if _chosen(f, {name})])
        for name in choices["systems"]
    ], sizer=_system_sized)
    kept, kept_omitted = narrow(files, omitted, systems, [], [])

    cores: list[str] = []
    core_names = available_choices(kept, kept_omitted)["cores"]
    if core_names:
        print("\nThe platform's own files for these systems are always kept;")
        print("the extra files each core loads can be narrowed.")
        cores = _pick("Cores:", [
            (name, _filter_by_target(
                [f for f in kept if f.get("cores") is not None], [name]))
            for name in core_names
        ], sizer=_core_sized)
        kept, kept_omitted = narrow(kept, kept_omitted, [], cores, [])

    regions: list[str] = []
    region_names = available_choices(kept, kept_omitted)["regions"]
    if region_names:
        print("\nSome systems have one BIOS per region. Keeping only the")
        print("preferred region removes the others where a match exists.")
        regions = _pick("Regions:", [
            (name, [f for f in kept if name in (f.get("regions") or [])])
            for name in region_names
        ], ordered=True)
    return systems, cores, regions


def _same_choice_hint(plat: str, systems: list[str], cores: list[str], regions: list[str]) -> str:
    """The options that repeat a choice made at the prompts."""
    def quoted(value: str) -> str:
        return f'"{value}"' if any(c in value for c in " ;&|") else value

    parts = [f"--platform {plat}"]
    parts += [f"--system {quoted(name)}" for name in systems]
    parts += [f"--core {quoted(name)}" for name in cores]
    if regions:
        parts.append(f"--region {','.join(regions)}")
    return " ".join(parts)


def system_aliases(manifest: dict | None) -> dict[str, str]:
    """The platform's own names for its systems, each with the system id."""
    return {
        alias: system
        for system, names in ((manifest or {}).get("system_names") or {}).items()
        for alias in names
    }


def _system_label(name: str, manifest: dict | None) -> str:
    """A system id with the names the platform gives it: nintendo-gba (gba)."""
    names = ((manifest or {}).get("system_names") or {}).get(name)
    return f"{name} ({', '.join(names)})" if names else name


def _print_choices(
    plat: str, files: list[dict], omitted: list[dict], systems: bool,
    manifest: dict | None = None,
) -> None:
    """What --system or --core accepts for a platform, with what each holds."""
    choices = available_choices(files, omitted)
    names = choices["systems"] if systems else choices["cores"]
    if not names:
        print(f"  {plat} records no {'systems' if systems else 'cores'}")
        return
    labels = {name: _system_label(name, manifest) if systems else name for name in names}
    width = min(max(len(label) for label in labels.values()), 40)
    for name in names:
        held = (
            [f for f in files if _chosen(f, {name})]
            if systems
            else _filter_by_target([f for f in files if f.get("cores") is not None], [name])
        )
        print(f"  {labels[name]:<{width}}  "
              f"{_system_sized(held) if systems else _core_sized(held)}")


def emulator_names(manifest: dict, files: list[dict], omitted: list[dict]) -> dict[str, str]:
    """Every name --emulator accepts, with the profile each one stands for.

    A profile answers to its own name and to the names its core is built
    under (core_names: beetle_psx and mednafen_psx). A core named by a file
    and by no profile stands for itself.
    """
    names = {core: core for core in available_choices(files, omitted)["cores"]}
    for profile, aliases in (manifest.get("core_names") or {}).items():
        names[profile] = profile
        for alias in aliases:
            names.setdefault(alias, profile)
    return names


def _print_emulators(plat: str, manifest: dict, files: list[dict], omitted: list[dict]) -> None:
    """What --emulator accepts for a platform, one name per line."""
    names = emulator_names(manifest, files, omitted)
    if not names:
        print(f"  {plat} records no emulators")
        return
    width = min(max(len(name) for name in names), 40)
    for name in sorted(names):
        profile = names[name]
        print(f"  {name:<{width}}  {'' if profile == name else 'same as ' + profile}".rstrip())


# RomM 5.4 places each platform's firmware by `filesystem.structure.firmware`
# in config.yml, "bios/{platform}" by default, relative to the library
# (rommapp/romm 5.4.0 68f71259, backend/config/config_manager.py:58,101-109,
# 205-225,564-568). The library and config.yml both sit under the RomM base
# folder (config/__init__.py:46-48, config_manager.py:264-265). A library
# laid out as {platform}/roms makes RomM stop until config.yml declares its
# templates, and RomM suggests firmware: "{platform}/bios" there
# (config_manager.py:1105-1124).


def parse_romm_firmware(template: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The literal folders before and after {platform}, as RomM parses them."""
    sections = [section for section in template.strip().split("/") if section]
    if not sections:
        raise ValueError("the RomM firmware template is empty")

    def macro(section: str) -> str | None:
        if section.startswith("{") and section.endswith("}"):
            return section[1:-1].strip()
        return None

    if any(section in (".", "..") for section in sections):
        raise ValueError(f"the RomM firmware template {template!r} climbs out of the library")
    platform_at = [i for i, section in enumerate(sections) if macro(section) == "platform"]
    if len(platform_at) != 1:
        raise ValueError(f"the RomM firmware template {template!r} needs one '{{platform}}'")
    before, after = sections[:platform_at[0]], sections[platform_at[0] + 1:]
    if any(macro(section) is not None for section in before + after):
        raise ValueError(f"the RomM firmware template {template!r} takes literal folder "
                         "names around '{platform}'")
    return tuple(before), tuple(after)


def _romm_configured_firmware(config: Path) -> str | None:
    """filesystem.structure.firmware from RomM's config.yml, read without a
    YAML library: only that nested scalar is needed."""
    try:
        lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    path: list[tuple[int, str]] = []
    for line in lines:
        stripped = line.split(" #", 1)[0].rstrip()
        if not stripped.strip() or stripped.lstrip().startswith(("#", "-")):
            continue
        indent = len(stripped) - len(stripped.lstrip())
        key, sep, value = stripped.strip().partition(":")
        if not sep:
            continue
        while path and path[-1][0] >= indent:
            path.pop()
        keys = [name for _indent, name in path] + [key.strip().strip("'\"")]
        if keys == ["filesystem", "structure", "firmware"] and value.strip():
            return value.strip().strip("'\"")
        path.append((indent, key.strip().strip("'\"")))
    return None


def romm_firmware_layout(
    bios_path: Path, template: str | None
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Where RomM reads each platform's firmware, relative to its library.

    bios_path is the folder the default template names, so its parent is the
    library. The template comes from --romm-firmware, else from config.yml
    beside the library. None keeps the pack's own bios/<platform> layout.
    """
    library = bios_path.parent
    if template is None:
        template = _romm_configured_firmware(library.parent / "config" / "config.yml")
    if template is not None:
        return parse_romm_firmware(template)
    platform_first = not (library / "roms").is_dir() and any(
        path.is_dir() for path in library.glob("*/roms")
    )
    if platform_first:
        raise ValueError(
            f"{library} keeps its roms inside each platform folder, and RomM then "
            "reads firmware where filesystem.structure.firmware in its config.yml "
            "says, which was not found here. RomM suggests \"{platform}/bios\" for "
            "this layout: run again with --romm-firmware \"{platform}/bios\", or "
            "with the value config.yml holds."
        )
    return None


def romm_relayout(
    files: list[dict], bios_path: Path,
    before: tuple[str, ...], after: tuple[str, ...],
) -> tuple[list[dict], Path]:
    """The manifest's bios/<platform>/<file> entries under RomM's template,
    with the library as the root they are relative to."""
    if (before, after) == (("bios",), ()):
        return files, bios_path
    moved = []
    for entry in files:
        slug, _, rest = str(entry["dest"]).partition("/")
        if not rest:
            # Firmware of no platform: RomM lists none, the default place stays.
            moved.append({**entry, "dest": f"bios/{slug}"})
            continue
        moved.append({**entry, "dest": "/".join((*before, slug, *after, rest))})
    return moved, bios_path.parent


def _older_list_advice() -> str:
    """How to reach a file list that records what an option needs.

    A list without the fields comes from an older release. Pinned with
    RETROBIOS_REF, as the docs suggest for reproducibility, "a newer one"
    meant unsetting the variable, and nothing said so.
    """
    if RELEASE_REF != DEFAULT_RELEASE_REF:
        return (f"  RETROBIOS_REF={RELEASE_REF} selects file lists published before "
                "this option existed: unset RETROBIOS_REF to use the current ones, "
                "or run without the option.")
    return ("  The file list comes from an older release: run the one-line "
            "installer again to fetch the current one.")


def _narrow_to_emulators(
    names: list[str], manifest: dict, plat: str,
    files: list[dict], omitted: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Keep what the named emulators read, as the pack built for them.

    An emulator page offers this as one command, so a name the platform
    does not run is refused rather than read as nothing to install.
    """
    if "system_cores" not in manifest:
        print(f"Error: the {plat} file list does not say which emulators read "
              "which systems, which --emulator needs.", file=sys.stderr)
        print(_older_list_advice(), file=sys.stderr)
        sys.exit(1)
    known = sorted(emulator_names(manifest, files, omitted))
    try:
        chosen = resolve_choices(names, known, "emulator")
    except ValueError as exc:
        print(f"Error: {plat}: {exc}", file=sys.stderr)
        sys.exit(1)
    before = len(files)
    files, omitted = narrow_to_cores(manifest, chosen, files, omitted)
    # A whole-platform install keeps the platform's files of a system no
    # core is mapped to. Here only the named emulators' systems are wanted.
    cores = designated_cores(manifest, chosen)
    reached = {
        system for system, readers in (manifest.get("system_cores") or {}).items()
        if cores.intersection(readers)
    }

    def theirs(entry: dict) -> bool:
        return entry.get("cores") is not None or bool(reached.intersection(entry_systems(entry)))

    files = [entry for entry in files if theirs(entry)]
    omitted = [entry for entry in omitted if theirs(entry)]
    print(f"  Narrowed {before} -> {len(files)} files to {', '.join(chosen)}")
    return files, omitted


def _requested_narrowing(
    args: argparse.Namespace, plat: str, files: list[dict], omitted: list[dict],
    manifest: dict | None = None,
) -> tuple[list[str], list[str], list[str]] | None:
    """The systems, cores and regions named on the command line, checked."""
    if not (args.system or args.core or args.region):
        return None
    if not records_selection(files):
        # Without the fields, a region filter would match nothing and install
        # everything: refused rather than ignored.
        print(
            f"Error: the {plat} file list records no systems or regions, so "
            "--system, --core and --region cannot narrow it.",
            file=sys.stderr,
        )
        print(_older_list_advice(), file=sys.stderr)
        sys.exit(1)
    choices = available_choices(files, omitted)
    try:
        systems = resolve_choices(
            args.system or [], choices["systems"], "system", system_aliases(manifest)
        )
        cores = resolve_choices(args.core or [], choices["cores"], "core")
        regions = resolve_regions(args.region or [])
    except ValueError as exc:
        print(f"Error: {plat}: {exc}", file=sys.stderr)
        sys.exit(1)
    return systems, cores, regions


# A pack too large for one release file is published as ZIP parts
# (scripts/split_pack.py), each holding at its root a marker that names the
# pack, the part and every part with its size. Extracted with the files,
# the markers say which part a folder lacks.
_PART_FILE = re.compile(r"^(?P<stem>.+_BIOS_Pack)\.part(?P<index>\d+)of(?P<count>\d+)\.zip$")
_PART_MARKER = re.compile(r"^(?P<stem>.+_BIOS_Pack)\.part(?P<index>\d+)of(?P<count>\d+)\.txt$")
MAX_MARKER_BYTES = 64 * 1024
MAX_PARTS = 999


class PackPart(NamedTuple):
    """What one part's marker says, and the folder it was found in."""

    pack: str
    index: int
    count: int
    parts: tuple[tuple[str, int], ...]
    folder: Path


def read_part_marker(path: Path) -> PackPart | None:
    """The marker at path, None when it is not one split_pack wrote."""
    match = _PART_MARKER.match(path.name)
    if match is None:
        return None
    try:
        with open(path, "rb") as handle:
            text = handle.read(MAX_MARKER_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    pack = ""
    index = count = 0
    parts: list[tuple[str, int]] = []
    for line in text.splitlines():
        key, _sep, value = line.partition(":")
        value = value.strip()
        if key == "pack":
            pack = value
        elif key == "part":
            numbers = value.split(" of ")
            if len(numbers) == 2 and all(n.isdigit() for n in numbers):
                index, count = int(numbers[0]), int(numbers[1])
        elif key == "file":
            size, _space, name = value.partition(" ")
            if size.isdigit() and _PART_FILE.match(name.strip()):
                parts.append((name.strip(), int(size)))
    if (
        not pack.endswith("_BIOS_Pack.zip")
        or not 1 <= index <= count <= MAX_PARTS
        or len(parts) != count
        or (index, count) != (int(match["index"]), int(match["count"]))
    ):
        return None
    return PackPart(pack, index, count, tuple(parts), path.parent)


def find_part_markers(root: Path) -> list[PackPart]:
    """Markers in root and in the folders right below it.

    A part extracted into a folder named after itself (the default of
    several archive tools) leaves its marker one level down.
    """
    found: list[PackPart] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.is_file():
            marker = read_part_marker(entry)
            if marker:
                found.append(marker)
        elif entry.is_dir() and not entry.is_symlink() and entry.name != BACKUP_DIR:
            try:
                below = sorted(entry.iterdir())
            except OSError:
                continue
            for child in below:
                marker = read_part_marker(child) if child.is_file() else None
                if marker:
                    found.append(marker)
    return found


def split_pack_lines(root: Path) -> list[str]:
    """What --check says about the parts of a pack extracted into root: one
    missing, or one extracted into a folder of its own."""
    by_pack: dict[str, list[PackPart]] = {}
    for marker in find_part_markers(root):
        by_pack.setdefault(marker.pack, []).append(marker)
    lines: list[str] = []
    for pack, markers in sorted(by_pack.items()):
        listed = max(markers, key=lambda m: m.count)
        here = {m.index for m in markers if m.folder == root and m.count == listed.count}
        missing = [n for n in range(1, listed.count + 1) if n not in here]
        deep = sorted({m.folder for m in markers if m.folder != root})
        if not missing and not deep:
            continue
        lines.append(f"\n  {pack} comes in {listed.count} parts, all needed.")
        for folder in deep:
            names = sorted(m.index for m in markers if m.folder == folder)
            which = ", ".join(str(n) for n in names)
            lines.append(f"    {'Parts' if len(names) > 1 else 'Part'} {which} went into "
                         f"{folder}, a folder of its own: move what it holds up into {root}.")
        absent = [n for n in missing
                  if not any(m.index == n and m.folder != root for m in markers)]
        for number in absent:
            name, size = listed.parts[number - 1]
            lines.append(f"    missing: part {number}, {name} ({format_size(size)})")
        if absent:
            lines.append(f"    Download {'it' if len(absent) == 1 else 'them'} and extract "
                         f"into {root}, beside the other parts.")
    return lines


class PackSource:
    """A pack already downloaded, whole or in parts, read in place of the network.

    Every file it gives is checked against the hashes the file list records,
    as a download is.
    """

    def __init__(self, paths: list[Path]) -> None:
        self.files = self._resolve(paths)
        first = _PART_FILE.match(self.files[0].name)
        self.name = f"{first['stem']}.zip" if first else self.files[0].name
        self.archives: list[zipfile.ZipFile] = []
        self.members: dict[str, tuple[zipfile.ZipFile, zipfile.ZipInfo]] = {}
        try:
            for path in self.files:
                archive = zipfile.ZipFile(path)
                self.archives.append(archive)
                for info in archive.infolist():
                    if not info.is_dir():
                        self.members.setdefault(info.filename, (archive, info))
        except (OSError, zipfile.BadZipFile) as exc:
            self.close()
            raise ValueError(f"{path.name} cannot be read as a ZIP ({exc}). A download "
                             "cut short gives this: download it again.") from exc
        self.listed = self._pack_list()
        self.by_sha1 = {f["sha1"]: f["path"] for f in self.listed}

    @staticmethod
    def _resolve(paths: list[Path]) -> list[Path]:
        """The archives of one pack: a whole ZIP, or every one of its parts."""
        given: list[Path] = []
        for path in paths:
            if path.is_dir():
                given += sorted(p for p in path.iterdir()
                                if p.is_file() and p.name.endswith(".zip")
                                and "_BIOS_Pack" in p.name)
            elif path.is_file():
                given.append(path)
            else:
                raise ValueError(f"{path} does not exist")
        if not given:
            raise ValueError(f"no pack ZIP in {', '.join(map(str, paths))}")
        packs: dict[str, list[Path]] = {}
        for path in given:
            part = _PART_FILE.match(path.name)
            packs.setdefault(f"{part['stem']}.zip" if part else path.name, []).append(path)
        if len(packs) > 1:
            raise ValueError(f"these are several packs ({', '.join(sorted(packs))}): "
                             "name the one to install from")
        name, chosen = next(iter(packs.items()))
        whole = [p for p in chosen if not _PART_FILE.match(p.name)]
        if whole:
            return whole[:1]
        count = int(_PART_FILE.match(chosen[0].name)["count"])
        if count > MAX_PARTS or any(
            int(_PART_FILE.match(p.name)["count"]) != count for p in chosen
        ):
            raise ValueError(f"the parts of {name} disagree on how many there are")
        stem = name[: -len(".zip")]
        folders = list(dict.fromkeys(p.parent for p in chosen))
        found: list[Path] = []
        missing: list[str] = []
        for number in range(1, count + 1):
            part_name = f"{stem}.part{number}of{count}.zip"
            place = next((folder / part_name for folder in folders
                          if (folder / part_name).is_file()), None)
            if place is None:
                missing.append(part_name)
            else:
                found.append(place)
        if missing:
            where = ", ".join(map(str, folders))
            raise ValueError(f"{name} comes in {count} parts and {len(missing)} "
                             f"{'is' if len(missing) == 1 else 'are'} not in {where}: "
                             f"{', '.join(missing)}")
        return found

    def _pack_list(self) -> list[dict]:
        """The file list the pack carries, every file with its SHA-1 and size."""
        held = self.members.get("manifest.json")
        if held is None:
            raise ValueError(f"{self.name} holds no manifest.json. Name the file list "
                             "with --manifest.")
        archive, info = held
        if info.file_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"the manifest.json of {self.name} exceeds "
                             f"{MAX_MANIFEST_BYTES} bytes")
        try:
            data = json.loads(archive.read(info).decode("utf-8"))
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"the manifest.json of {self.name} cannot be read: {exc}") from exc
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            raise ValueError(f"the manifest.json of {self.name} lists no files")
        listed = []
        for item in files:
            if (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("sha1"), str)
                and _SHA1_RE.fullmatch(item["sha1"].lower())
                and isinstance(item.get("size"), int)
            ):
                listed.append({"path": item["path"], "sha1": item["sha1"].lower(),
                               "size": item["size"]})
        return listed

    def platforms(self) -> list[str]:
        """The platforms the pack's name spells, several for a grouped pack."""
        stem = "_" + self.name[: -len("_BIOS_Pack.zip")] + "_"
        return [plat for plat in AVAILABLE_PLATFORMS
                if f"_{PLATFORM_NAMES[plat].replace(' ', '_')}_" in stem]

    def manifest(self, plat: str) -> dict:
        """A file list for plat built from the pack's own: the same files at
        the same places, as extracting the pack would leave them."""
        return {
            "manifest_version": 2,
            "platform": plat,
            "files": [
                {"dest": f["path"], "sha1": f["sha1"], "size": f["size"],
                 "repo_path": f"bios/{f['path']}"}
                for f in self.listed
            ],
        }

    def find(self, entry: dict) -> tuple[zipfile.ZipFile, zipfile.ZipInfo] | None:
        """The member holding an entry's bytes: by its SHA-1 in the pack's list,
        else at its destination. The size has to agree either way."""
        sha1 = str(entry.get("sha1", "")).lower()
        for name in (self.by_sha1.get(sha1), entry.get("dest")):
            held = self.members.get(name) if isinstance(name, str) else None
            if held is not None and held[1].file_size == entry.get("size"):
                return held
        return None

    def tree(self, prefix: str) -> Iterator[tuple[str, int, Callable[[], BinaryIO]]]:
        """(path in the tree, size, opener) of every member below prefix."""
        lead = f"{prefix.rstrip('/')}/" if prefix else ""
        for name, (archive, info) in self.members.items():
            if name.startswith(lead):
                yield name[len(lead):], info.file_size, (
                    lambda archive=archive, info=info: archive.open(info))

    def close(self) -> None:
        for archive in self.archives:
            archive.close()
        self.archives = []


class LocalSources:
    """Where this run reads instead of the network: a file list, a pack, the
    folders it copies BIOS files from."""

    def __init__(self) -> None:
        self.manifest: Path | None = None
        self.pack: PackSource | None = None
        # The folders named with --from, read once for every platform.
        self.folders: list[FolderIndex] = []

    @property
    def offline(self) -> bool:
        return self.pack is not None

    def reset(self) -> None:
        if self.pack is not None:
            self.pack.close()
        self.manifest = None
        self.pack = None
        self.folders = []


LOCAL = LocalSources()
NOT_IN_PACK = "not in the pack"


class _Offline(OSError):
    """A network request in a run told to make none."""


def _network_allowed(what: str) -> None:
    if LOCAL.offline:
        raise _Offline(f"{what}: this run reads a pack and makes no network request")


def read_local_manifest(path: Path, plat: str) -> dict:
    """A file list saved on disk, checked as a downloaded one is."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        print(f"  Cannot read the file list {path}: {exc.strerror or exc}.", file=sys.stderr)
        sys.exit(1)
    try:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError(f"it exceeds {MAX_MANIFEST_BYTES} bytes")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"not JSON ({exc})") from exc
        return _validate_manifest(data, plat)
    except ValueError as exc:
        EVENTS.emit("error", stage="manifest", reason="invalid", platform=plat,
                    message=f"the file list is not valid: {exc}")
        print(f"  The file list {path} is not valid for {plat}: {exc}", file=sys.stderr)
        sys.exit(1)


def local_manifest_platform(path: Path) -> str | None:
    """The platform a saved file list names, None when it names none."""
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(MAX_MANIFEST_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    plat = data.get("platform") if isinstance(data, dict) else None
    return plat if plat in AVAILABLE_PLATFORMS else None


def _extract_from_pack(
    f: dict, bios_path: Path, dest: Path, progress: Progress | None,
) -> tuple[str, bool, str]:
    """Copy one file out of the pack, checked like a download."""
    pack = LOCAL.pack
    held = pack.find(f) if pack is not None else None
    if held is None:
        return f["dest"], False, NOT_IN_PACK
    archive, info = held
    return _write_verified(f, bios_path, dest, lambda: archive.open(info), progress,
                           "the pack", named=f"{info.filename} inside {archive.filename}")


@contextlib.contextmanager
def _open_copy(copy: LocalCopy) -> Iterator[BinaryIO]:
    """The bytes of a copy found on this machine, opened for reading only."""
    if not copy.member:
        with open(copy.path, "rb") as handle:
            yield handle
        return
    with zipfile.ZipFile(copy.path) as archive, archive.open(copy.member) as handle:
        yield handle


def _copy_local(
    f: dict, bios_path: Path, dest: Path, copy: LocalCopy, progress: Progress | None,
) -> tuple[str, bool, str]:
    """Copy one file found on this machine to its place, checked like a
    download. Its bytes count as already here, not in the download speed."""
    return _write_verified(f, bios_path, dest, lambda: _open_copy(copy), progress,
                           f"the copy in {copy.index.label}", local=True,
                           named=copy.named())


class _SourceUnreadable(Exception):
    """The bytes to write could not be read: the source's fault, never the
    folder's, so it falls back like any copy that failed."""


def unreadable_words(exc: BaseException) -> str:
    """Why a file or a ZIP member could not be read, in words."""
    if isinstance(exc, RuntimeError) and "encrypted" in str(exc):
        return "it is encrypted, a password is needed"
    if isinstance(exc, NotImplementedError):
        return "it is packed with a compression method this installer cannot read"
    if isinstance(exc, (zipfile.BadZipFile, zlib.error, EOFError)):
        return "it is damaged"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


def _write_verified(
    f: dict, bios_path: Path, dest: Path,
    source: Callable[[], ContextManager[BinaryIO]], progress: Progress | None,
    held_in: str, local: bool = False, named: str = "",
) -> tuple[str, bool, str]:
    """Write the bytes source opens at dest once they match the list's size
    and hashes, the file in place kept aside first when it differs.

    The source is opened before the temporary file, and its read errors
    are told apart from the write errors: a copy that could not be read
    was reported as a folder not writable. named is the source in the
    reason then, held_in when empty."""
    expected = {name: f[name].lower() for name in ("sha256", "sha1") if f.get(name)}
    tmp_path: Path | None = None
    counted = 0

    def unreadable(exc: BaseException) -> _SourceUnreadable:
        return _SourceUnreadable(f"{named or held_in} could not be read: "
                                 f"{unreadable_words(exc)}")

    try:
        with contextlib.ExitStack() as stack:
            try:
                src = stack.enter_context(source())
            except _MEMBER_ERRORS as exc:
                raise unreadable(exc) from exc
            out = stack.enter_context(tempfile.NamedTemporaryFile(
                mode="wb", dir=dest.parent, prefix=f".{dest.name}.", suffix=".part",
                delete=False,
            ))
            tmp_path = Path(out.name)
            size = 0
            while True:
                if _stop.is_set():
                    raise _Interrupted()
                try:
                    chunk = src.read(min(1024 * 1024, f["size"] + 1 - size))
                except _MEMBER_ERRORS as exc:
                    raise unreadable(exc) from exc
                if not chunk:
                    break
                size += len(chunk)
                if size > f["size"]:
                    raise ValueError(f"{held_in} holds more bytes than the list records")
                out.write(chunk)
                if progress is not None:
                    progress.add(len(chunk), resumed=local)
                    counted += len(chunk)
        if size != f["size"]:
            raise ValueError(f"{held_in} holds {size} bytes, the list records {f['size']}")
        actual = _digest_file(tmp_path, tuple(expected))
        for name, label in (("sha256", "SHA-256"), ("sha1", "SHA-1")):
            if name in expected and actual[name] != expected[name]:
                raise ValueError(f"{label} mismatch in {held_in}")
        os.chmod(tmp_path, (0o777 if f.get("executable") else 0o666) & ~_UMASK)
        _clear_read_only(dest)
        KEPT.keep(*_kept_slot(*_entry_place(bios_path, f)), dest, expected, tmp_path)
        os.replace(tmp_path, dest)
        tmp_path = None
        return f["dest"], True, ""
    except _Interrupted:
        return f["dest"], False, "stopped"
    except _NotKept as exc:
        return f["dest"], False, f"{NOT_KEPT} ({exc})"
    except (_SourceUnreadable, ValueError, *_MEMBER_ERRORS) as exc:
        if progress is not None and counted:
            progress.add(-counted, resumed=local)
        if isinstance(exc, OSError) and _write_refused(exc):
            return f["dest"], False, READ_ONLY if _read_only_file(dest) else NOT_WRITABLE
        return f["dest"], False, str(exc)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


# Files --check reads to tell why a file is refused: past these, the folder
# is not one a person filled by hand, and walking it costs more than it tells.
MAX_SCANNED_FILES = 200_000
_ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")
# Cores named on the line that says who opens a file, before "and N more".
SHOWN_CORES = 4


# Folders a system writes on a drive for itself. No BIOS file a person
# copied is in them, and a Mac's Spotlight index alone holds thousands.
SYSTEM_FOLDERS = frozenset(name.casefold() for name in (
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", "System Volume Information", "$RECYCLE.BIN",
))
# A Mac copying to a FAT or exFAT card writes ._name beside every file: its
# Finder data, 4 KB, never the file.
APPLE_DOUBLE = "._"
# Errors a member of someone's ZIP can raise on reading: damaged, encrypted
# (RuntimeError), or packed with a method zipfile lacks.
_MEMBER_ERRORS = (OSError, EOFError, RuntimeError, NotImplementedError,
                  zipfile.BadZipFile, zlib.error)


class LocalCopy(NamedTuple):
    """A file found on this machine with the contents an entry wants."""
    index: FolderIndex
    path: Path
    # The member holding the bytes when path is a ZIP, else "".
    member: str = ""
    # In the BIOS folder at a place no entry of the list names: a mistake
    # the copy fixes. A copy from another entry's place is no mistake.
    misplaced: bool = False

    def where(self) -> str:
        """The copy as the folder it was found in names it."""
        rel = self.index.relative(self.path)
        return f"{rel}, inside as {self.member}" if self.member else rel

    def named(self) -> str:
        """The copy by its full path, the member first when it is one."""
        return f"{self.member} inside {self.path}" if self.member else str(self.path)


class FolderIndex:
    """The files under a folder, by name and by size, hashed on demand.

    The folder is only read. A link is followed only where it leads to a
    file inside the folder, so an old card holding a link to the home
    folder is not walked through it. A ZIP named as the root is read as a
    folder holding that one archive.
    """

    def __init__(self, root: Path, label: str = "") -> None:
        self.label = label or str(root)
        self.root = root = root.resolve()
        self.by_name: dict[str, list[Path]] = {}
        self.by_size: dict[int, list[Path]] = {}
        self.size: dict[Path, int] = {}
        self.archives: list[Path] = []
        # Name a ._ file stands beside (casefolded) -> the ._ files.
        self.apple_double: dict[str, list[Path]] = {}
        # The list's destinations, casefolded (an archive among them is not
        # unpacked for its members) and as the list writes them.
        self.sealed: set[str] = set()
        self.listed: set[str] = set()
        self.truncated = False
        self.copied = 0
        self.fixed = 0
        self._lock = threading.Lock()
        self._sha1: dict[Path, str] = {}
        self._digests: dict[tuple[Path, str, str], str] = {}
        self._members: dict[Path, list[str]] = {}
        self._member_sizes: dict[int, list[tuple[Path, str]]] | None = None
        # (size, casefolded name) -> the files and the ZIP members, built
        # the first time a copy is looked for.
        self._loose_named: dict[tuple[int, str], list[Path]] | None = None
        self._member_named: dict[int, dict[str, list[tuple[Path, str]]]] = {}
        self._scans: dict[tuple[str, int, str], _HashScan] = {}
        self._member_size: dict[tuple[Path, str], int] = {}
        # (path, member or "") -> (its size or None when unknown, why) of each
        # file, member or folder below root that could not be read.
        self.unreadable: dict[tuple[Path, str], tuple[int | None, str]] = {}
        if root.is_file():
            if root.name.casefold().endswith(".zip"):
                self.archives.append(root)
            return
        seen = 0

        def unlisted(exc: OSError) -> None:
            self._unread(Path(exc.filename or root), "", None, exc)

        for folder, dirs, names in os.walk(root, onerror=unlisted):
            dirs[:] = sorted(d for d in dirs
                             if d != BACKUP_DIR and d.casefold() not in SYSTEM_FOLDERS)
            for name in sorted(names):
                path = Path(folder) / name
                if name.startswith(APPLE_DOUBLE):
                    self.apple_double.setdefault(name[2:].casefold(), []).append(path)
                    continue
                if name.startswith(".") and name.endswith(".part"):
                    continue  # a download of this installer, not finished
                try:
                    if path.is_symlink() and not _inside(path, root):
                        continue
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                except OSError as exc:
                    self._unread(path, "", None, exc)
                    continue
                self.by_name.setdefault(name.casefold(), []).append(path)
                self.by_size.setdefault(size, []).append(path)
                self.size[path] = size
                if name.casefold().endswith(_ARCHIVE_SUFFIXES):
                    self.archives.append(path)
                seen += 1
                if seen >= MAX_SCANNED_FILES:
                    self.truncated = True
                    return

    def seed(self, path: Path, sha1: str) -> None:
        """A SHA-1 already known, so the file is not read again."""
        self._sha1[path] = sha1

    def _unread(self, path: Path, member: str, size: int | None, exc: BaseException) -> None:
        """Note a file, a member or a folder that could not be read: a copy
        the run needs may be the one, and it is named instead of dropped."""
        self.unreadable.setdefault((path, member), (size, unreadable_words(exc)))

    def sha1(self, path: Path) -> str:
        if path not in self._sha1:
            try:
                self._sha1[path] = _sha1_file(path)
            except OSError as exc:
                self._unread(path, "", self.size.get(path), exc)
                self._sha1[path] = ""
        return self._sha1[path]

    def digest(self, path: Path, algorithm: str, member: str = "") -> str:
        """The file's (or the member's) digest, read once, "" when unreadable."""
        if algorithm == "sha1" and not member:
            return self.sha1(path)
        key = (path, member, algorithm)
        if key not in self._digests:
            try:
                if member:
                    with zipfile.ZipFile(path) as archive, archive.open(member) as handle:
                        value = _digest_handle(handle, (algorithm,))[algorithm]
                else:
                    value = _digest_file(path, (algorithm,))[algorithm]
            except _MEMBER_ERRORS as exc:
                size = self._member_size.get((path, member)) if member else self.size.get(path)
                self._unread(path, member, size, exc)
                value = ""
            self._digests[key] = value
        return self._digests[key]

    def unreadable_for(self, sizes: Collection[int]) -> list[tuple[Path, str, str]]:
        """(path, member, why) of what could not be read and may hold a file
        of one of sizes: its size is one of them, or unknown (a folder, an
        archive whose list of members could not be read)."""
        return sorted((path, member, why)
                      for (path, member), (size, why) in self.unreadable.items()
                      if size is None or size in sizes)

    def _scan(self, kind: str, size: int, algorithm: str) -> _HashScan:
        """The files (kind "loose") or ZIP members ("member") of one size,
        hashed in the order the folder was read, each read once."""
        key = (kind, size, algorithm)
        if key not in self._scans:
            if kind == "loose":
                self._scans[key] = _HashScan(
                    self.by_size.get(size, []), lambda path: self.digest(path, algorithm))
            else:
                self._scans[key] = _HashScan(
                    self._members_of_size(size),
                    lambda item: self.digest(item[0], algorithm, item[1]))
        return self._scans[key]

    def first_holding(self, sizes: set[int], sha1s: set[str]) -> Path | None:
        """The first file whose contents are one of sha1s, the smallest size
        first, read only when the size fits."""
        for size in sorted(sizes):
            path = self._scan("loose", size, "sha1").first(sha1s)
            if path is not None:
                return path
        return None

    def zip_members(self, path: Path) -> list[str]:
        if path not in self._members:
            try:
                with zipfile.ZipFile(path) as archive:
                    self._members[path] = [n.rsplit("/", 1)[-1].casefold()
                                           for n in archive.namelist()]
            except (OSError, zipfile.BadZipFile):
                self._members[path] = []
        return self._members[path]

    def _members_of_size(self, size: int) -> list[tuple[Path, str]]:
        """(archive, member) of every ZIP member of that size, the
        archives' directories read once, the first time one is asked."""
        if self._member_sizes is None:
            self._member_sizes = {}
            for path in self.archives:
                if (not path.name.casefold().endswith(".zip")
                        or self.relative(path).casefold() in self.sealed):
                    continue
                try:
                    with zipfile.ZipFile(path) as archive:
                        infos = archive.infolist()
                except OSError as exc:
                    # A ZIP that will not open may hold anything: named. A
                    # damaged one is a download cut short, not a copy.
                    self._unread(path, "", None, exc)
                    continue
                except zipfile.BadZipFile:
                    continue
                for info in infos:
                    if not info.is_dir():
                        self._member_sizes.setdefault(info.file_size, []).append(
                            (path, info.filename))
                        self._member_size[(path, info.filename)] = info.file_size
        return self._member_sizes.get(size, [])

    def find(self, entry: dict, skip: Collection[Path] = ()) -> LocalCopy | None:
        """A copy of the entry's own contents: its size first, then its hash,
        loose files before ZIP members, a file of the same name first."""
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            return None
        algorithm = "sha1" if entry.get("sha1") else "sha256"
        wanted = str(entry.get(algorithm) or "").lower()
        if not wanted:
            return None
        name = str(entry.get("dest", "")).rsplit("/", 1)[-1].casefold()
        if self._loose_named is None:
            self._loose_named = {}
            for path, held in self.size.items():
                self._loose_named.setdefault((held, path.name.casefold()), []).append(path)
        for path in self._loose_named.get((size, name), []):
            if not _being_written(path, skip) and self.digest(path, algorithm) == wanted:
                return LocalCopy(self, path)
        path = self._scan("loose", size, algorithm).first({wanted}, skip)
        if path is not None:
            return LocalCopy(self, path)
        if size not in self._member_named:
            named = self._member_named[size] = {}
            for item in self._members_of_size(size):
                named.setdefault(item[1].rsplit("/", 1)[-1].casefold(), []).append(item)
        for archive, member in self._member_named[size].get(name, []):
            if (not _being_written(archive, skip)
                    and self.digest(archive, algorithm, member) == wanted):
                return LocalCopy(self, archive, member)
        held = self._scan("member", size, algorithm).first({wanted}, skip)
        if held is not None:
            return LocalCopy(self, held[0], held[1])
        return None

    def count_copy(self, misplaced: bool = False) -> None:
        with self._lock:
            if misplaced:
                self.fixed += 1
            else:
                self.copied += 1

    def relative(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
        return rel if rel != "." else path.name


def _being_written(path: Path, skip: Collection[Path]) -> bool:
    """Whether a file, or the archive holding a member, is one this run
    writes: its bytes are being replaced, so they are no source."""
    return bool(skip) and (path in skip or Path(os.path.realpath(path)) in skip)


class _HashScan:
    """Candidates of one size, hashed in order and each once, so finding
    the first that holds a digest never reads the earlier ones again: 3,000
    same-sized files and 3,000 entries of that size cost 3,000 reads, not
    nine million comparisons."""

    def __init__(self, items: list, digest: Callable[[Any], str]) -> None:
        self.items = items
        self.digest = digest
        self.read = 0
        self.at: dict[str, list[int]] = {}

    def first(self, wanted: Collection[str], skip: Collection[Path] = ()) -> Any:
        """The first candidate, in the folder's order, whose digest is one of
        wanted and that skip does not name, or None."""
        best = None
        for value in wanted:
            for position in self.at.get(value, ()):
                if not _being_written(self._holder(position), skip):
                    best = position if best is None else min(best, position)
                    break
        if best is not None:
            return self.items[best]
        while self.read < len(self.items):
            position = self.read
            self.read += 1
            value = self.digest(self.items[position])
            self.at.setdefault(value, []).append(position)
            if value in wanted and not _being_written(self._holder(position), skip):
                return self.items[position]
        return None

    def _holder(self, position: int) -> Path:
        item = self.items[position]
        return item[0] if isinstance(item, tuple) else item


class _ZippedLookup:
    """The archives of a folder not listed as files to install, by the
    folder holding them: each one's names read once, so telling that a
    missing file is still zipped costs two lookups and never a walk."""

    def __init__(self, index: FolderIndex, known: set[str]) -> None:
        self.index = index
        self._by_parent: dict[Path, list[tuple[int, Path]]] = {}
        for position, path in enumerate(index.archives):
            if index.relative(path).casefold() not in known:
                self._by_parent.setdefault(path.parent, []).append((position, path))
        # folder -> (member name -> first archive, stem -> first archive).
        self._maps: dict[Path, tuple[dict[str, tuple[int, Path]],
                                     dict[str, tuple[int, Path]]]] = {}

    def _folder(self, folder: Path) -> tuple[dict[str, tuple[int, Path]],
                                             dict[str, tuple[int, Path]]]:
        if folder not in self._maps:
            members: dict[str, tuple[int, Path]] = {}
            stems: dict[str, tuple[int, Path]] = {}
            for position, path in self._by_parent.get(folder, []):
                lowered = path.name.casefold()
                if lowered.endswith(".zip"):
                    for member in self.index.zip_members(path):
                        members.setdefault(member, (position, path))
                stems.setdefault(lowered.rsplit(".", 1)[0], (position, path))
            self._maps[folder] = (members, stems)
        return self._maps[folder]

    def reason(self, name: str, folders: Collection[Path]) -> str:
        """Why name is missing when an archive in folders holds it or carries
        its name: the first such archive in the order the folder was read,
        and one holding it before one carrying its name."""
        lowered = name.casefold()
        stem = lowered.rsplit(".", 1)[0]
        best: tuple[int, int, Path] | None = None
        for folder in folders:
            members, stems = self._folder(folder)
            for kind, held in ((0, members.get(lowered)), (1, stems.get(stem))):
                if held is not None and (best is None or (held[0], kind) < best[:2]):
                    best = (held[0], kind, held[1])
        if best is None:
            return ""
        where = self.index.relative(best[2])
        if best[1] == 0:
            return f"still zipped: it is inside {where}"
        return f"still zipped: {where} needs extracting"


def _inside(path: Path, root: Path) -> bool:
    """Whether a link at path leads to a place under root."""
    try:
        Path(os.path.realpath(path)).relative_to(root)
    except ValueError:
        return False
    return True


def _copies(entry: dict) -> list[dict]:
    """Every copy the list knows for an entry: its own, other cores', the
    platform's declared one."""
    return [entry, *entry.get("alternatives", []), *entry.get("slot_choices", []),
            *entry.get("declared_choices", [])]


def readers(entry: dict, manifest: dict) -> list[str]:
    """The cores that open an entry: as its copies name them, else every core
    the platform runs for its systems."""
    named = sorted({core for copy in _copies(entry) for core in copy.get("cores") or []})
    if named:
        return named
    system_cores = manifest.get("system_cores") or {}
    return sorted({core for system in entry_systems(entry)
                   for core in system_cores.get(system, [])})


def opened_line(entry: dict, manifest: dict, bios_path: Path) -> str:
    """Where the emulators read an entry, and which ones."""
    try:
        where = _entry_path(bios_path, entry)
    except ValueError:
        where = bios_path / str(entry.get("dest"))
    cores = readers(entry, manifest)
    if not cores:
        return f"opened at {where}"
    shown = ", ".join(cores[:SHOWN_CORES])
    if len(cores) > SHOWN_CORES:
        shown += f" and {len(cores) - SHOWN_CORES} more"
    return f"opened at {where} by {shown}"


def _known_destinations(files: list[dict]) -> set[str]:
    return {str(f.get("dest", "")).casefold() for f in files}


def _content_owners(files: list[dict]) -> dict[str, list[dict]]:
    """SHA-1 -> the entries one of whose copies holds it, in the list's order."""
    owners: dict[str, list[dict]] = {}
    for other in files:
        for sha1 in dict.fromkeys(c.get("sha1") for c in _copies(other) if c.get("sha1")):
            owners.setdefault(sha1, []).append(other)
    return owners


def _content_owner(
    sha1: str, entry: dict, files: list[dict],
    owners: dict[str, list[dict]] | None = None,
) -> str:
    """Why a file holding sha1 is not the one entry wants, if the list knows."""
    for copy in entry.get("alternatives", []) + entry.get("slot_choices", []):
        if copy.get("sha1") == sha1 and copy.get("cores"):
            return (f"another revision: the copy {', '.join(copy['cores'])} "
                    "reads, not the one this list installs here")
    for copy in entry.get("declared_choices", []):
        if copy.get("sha1") == sha1:
            return ("another revision: the one the platform's own list declares, "
                    "not the one the cores read")
    if owners is None:
        owners = _content_owners(files)
    for other in owners.get(sha1, []):
        if other is entry or other.get("dest") == entry.get("dest"):
            continue
        regions = [r for r in other.get("regions") or [] if r != WORLD_REGION]
        if regions:
            return (f"another region: the contents of {other['dest']} "
                    f"({', '.join(regions)})")
        return f"another file: the contents of {other['dest']}"
    return ""


def _place_reason(
    entry: dict, path: Path, index: FolderIndex, expected: Path, known: set[str],
    contents: str = "",
) -> tuple[str, str]:
    """(reason, folder too deep) for an entry's file found at path in the
    BIOS folder, or ("", "") when that place says nothing. contents is
    ", with other contents" for a file of the right name and wrong bytes."""
    dest = str(entry.get("dest", ""))
    name = dest.rsplit("/", 1)[-1]
    rel = index.relative(path)
    # A dest below the save directory is not below this folder: a copy
    # here under the same tail is in the wrong place, not too deep.
    below = entry.get("root") != "save"
    if below and rel.endswith("/" + dest) and path.name == name:
        extra = rel[: -len(dest) - 1]
        top = index.root.name
        if extra.casefold() == top.casefold():
            return (f"nested folder: it is at {top}/{rel}{contents}, a "
                    f"{top} folder inside {top}"), extra
        depth = extra.count("/") + 1
        levels = "one folder" if depth == 1 else f"{depth} folders"
        return f"{levels} too deep: it is at {rel}{contents}", extra
    if path.name.casefold() == name.casefold():
        if path.parent == expected.parent and path.name != name:
            return (f"case of the name: the folder holds {path.name}{contents}, "
                    f"the cores open {name}"), ""
        return f"wrong folder: it is at {rel}{contents}", ""
    if contents:
        return "", ""
    if rel.casefold() in known:
        return f"its contents are at {rel}: copy that file to {dest}", ""
    return f"a name no core opens: its contents are at {rel}, rename it {name}", ""


def _copy_reason(entry: dict, copy: LocalCopy, bios_path: Path, known: set[str]) -> str:
    """Why a copy found in the BIOS folder is not at the entry's place."""
    if not copy.misplaced:
        # np2kai/2608_bd.wav and np2/2608_bd.wav hold the same bytes. Told
        # "wrong folder", a user moved np2's copy and broke that core.
        return f"the same file is at {copy.where()}, where the list installs it too"
    if copy.member:
        return f"still zipped: it is inside {copy.index.relative(copy.path)}"
    try:
        copy.path.relative_to(copy.index.root)
    except ValueError:
        return f"one folder too high: it is at {copy.path}"
    try:
        expected = _entry_path(bios_path, entry)
    except ValueError:
        return ""
    return _place_reason(entry, copy.path, copy.index, expected, known)[0]


def _found_elsewhere(
    entry: dict, index: FolderIndex, bios_path: Path, files: list[dict],
    known: set[str], zipped: _ZippedLookup | None = None,
) -> tuple[str, str]:
    """(reason, folder too deep) for an entry missing at its place."""
    dest = str(entry.get("dest", ""))
    if entry.get("case_variant"):
        # A pack is read on Windows and macOS too, where two names that
        # differ only by case are one file: it carries one of the pair. Moving
        # the other copy here would take it from the core that opens it.
        return ("a second name the pack leaves out: Windows and macOS see it "
                "as the same file. Run the command without --check to add it"), ""
    name = dest.rsplit("/", 1)[-1]
    sizes = {copy.get("size") for copy in _copies(entry) if isinstance(copy.get("size"), int)}
    sha1s = {copy.get("sha1") for copy in _copies(entry) if copy.get("sha1")}
    try:
        expected = _entry_path(bios_path, entry)
    except ValueError:
        return "", ""
    same_name = [p for p in index.by_name.get(name.casefold(), []) if p != expected]
    matching = [p for p in same_name if index.size[p] in sizes and index.sha1(p) in sha1s]
    held = set(matching)
    for path in matching or same_name:
        contents = "" if path in held else ", with other contents"
        found = _place_reason(entry, path, index, expected, known, contents)
        if found[0]:
            return found
    above = index.root.parent / dest
    if index.root.parent != index.root and above.is_file():
        return f"one folder too high: it is at {above}", ""
    holder = index.first_holding(sizes, sha1s)
    if holder is not None:
        return _place_reason(entry, holder, index, expected, known)
    if name.casefold().endswith(_ARCHIVE_SUFFIXES):
        return "", ""
    if zipped is None:
        zipped = _ZippedLookup(index, known)
    reason = zipped.reason(name, {expected.parent, index.root})
    if reason:
        return reason, ""
    for path in index.apple_double.get(name.casefold(), []):
        if path.parent == expected.parent:
            return (f"only a Mac's ._ file: {path.name} holds Finder data, not the "
                    f"file itself. Copy {name} again"), ""
    return "", ""


def _wrong_contents(
    entry: dict, path: Path, index: FolderIndex, files: list[dict],
    owners: dict[str, list[dict]] | None = None,
) -> str:
    """Why the file at an entry's place is not the one the list installs."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return ""
    sha1 = index.sha1(path)
    owner = _content_owner(sha1, entry, files, owners)
    if owner:
        return owner
    if head == b"PK\x03\x04" and not str(entry.get("dest", "")).casefold().endswith(".zip"):
        return "still zipped: this file is a ZIP archive, extract what it holds"
    expected = {copy.get("size") for copy in _copies(entry) if isinstance(copy.get("size"), int)}
    if size not in expected:
        want = " or ".join(f"{s:,}" for s in sorted(expected))
        return f"wrong size: {size:,} bytes, the list expects {want}"
    return "same size, other contents: another dump, or a damaged copy"


class Diagnosis(NamedTuple):
    reasons: dict[int, str]
    deep: dict[str, int]


def diagnose(
    missing: list[dict], mismatched: list[dict], files: list[dict], bios_path: Path,
    verified: list[dict] | None = None, index: FolderIndex | None = None,
) -> Diagnosis:
    """The reason --check gives for each file it flags, from what the folder
    holds and what the list records: id(entry) -> reason, and the folders
    holding files too deep with how many. The files the check verified are
    not read again: their SHA-1 is the one the list records."""
    reasons: dict[int, str] = {}
    deep: dict[str, int] = {}
    if not bios_path.is_dir() or not (missing or mismatched):
        return Diagnosis(reasons, deep)
    if index is None:
        index = bios_folder_index(bios_path, files, verified or [])
    known = _known_destinations(files)
    zipped = _ZippedLookup(index, known)
    for entry in missing:
        reason, extra = _found_elsewhere(entry, index, bios_path, files, known, zipped)
        if reason:
            reasons[id(entry)] = reason
        if extra:
            deep[extra] = deep.get(extra, 0) + 1
    owners = _content_owners(files) if mismatched else {}
    for entry in mismatched:
        try:
            path = _entry_path(bios_path, entry)
        except ValueError:
            continue
        reason = _wrong_contents(entry, path, index, files, owners)
        if reason:
            reasons[id(entry)] = reason
    return Diagnosis(reasons, deep)


def check_lines(
    missing: list[dict], mismatched: list[dict], files: list[dict], manifest: dict,
    bios_path: Path, every: bool, verified: list[dict] | None = None,
    index: FolderIndex | None = None, copies: dict[int, LocalCopy] | None = None,
) -> list[str]:
    """The files --check flags, each with why and where its cores read it.

    "missing: scph5501.bin" left a user with the file one folder too deep,
    or still in its ZIP, to guess. A flagged file with a reason is always
    listed, and so is one a folder named with --from holds. Plain missing
    ones stop at ten unless every is set.
    """
    found = diagnose(missing, mismatched, files, bios_path, verified, index)
    copies = copies or {}
    known = _known_destinations(files)
    lines = []
    for extra, count in sorted(found.deep.items(), key=lambda item: -item[1]):
        if count >= 3:
            lines.append(f"  {_plural(count, 'file')} sit too deep, in "
                         f"{bios_path / extra}: move what it holds up into {bios_path}.")
    lines += _check_copy_lines(missing + mismatched, copies, index)
    explained = [e for e in missing if id(e) in found.reasons or id(e) in copies]
    plain = [e for e in missing if id(e) not in found.reasons and id(e) not in copies]
    shown = plain if every else plain[:10]

    def block(label: str, entry: dict) -> list[str]:
        game = " (game data)" if entry.get("category") == GAME_DATA else ""
        rows = [f"    {label}: {entry['dest']}{game}"]
        if id(entry) in found.reasons:
            rows.append(f"      {found.reasons[id(entry)]}")
        copy = copies.get(id(entry))
        if copy is not None and copy.index is index and not copy.misplaced:
            rows.append(f"      a run without --check copies it from {copy.where()}")
        elif copy is not None and copy.index is index:
            where = _copy_reason(entry, copy, bios_path, known)
            if where and where != found.reasons.get(id(entry)):
                rows.append(f"      the right file: {where}")
            rows.append("      a run without --check copies it into place")
        elif copy is not None:
            rows.append(f"      in {copy.index.label}: {copy.where()}")
        rows.append(f"      {opened_line(entry, manifest, bios_path)}")
        return rows

    for entry in mismatched:
        lines += block("wrong hash", entry)
    for entry in explained:
        lines += block("missing", entry)
    for entry in shown:
        lines += block("missing", entry)
    if len(shown) < len(plain):
        lines.append(f"    ... and {len(plain) - len(shown)} more missing "
                     "(--verbose lists them all)")
    return lines


def bios_folder_index(
    bios_path: Path, files: list[dict], verified: list[dict],
) -> FolderIndex | None:
    """The BIOS folder read for files misplaced in it. The files the check
    verified are not read again: their SHA-1 is the one the list records.
    An archive the list installs is not unpacked for its members: a MAME
    set holding a ROM a core also reads loose is that core's to open."""
    if not bios_path.is_dir():
        return None
    index = FolderIndex(bios_path)
    for entry in verified:
        if entry.get("sha1"):
            try:
                index.seed(_entry_path(bios_path, entry), entry["sha1"].lower())
            except ValueError:
                continue
    index.sealed = _known_destinations(files)
    index.listed = {str(f.get("dest", "")) for f in files}
    return index


def _above(entry: dict, index: FolderIndex) -> LocalCopy | None:
    """The entry's file at its place one folder above the BIOS folder."""
    if entry.get("root") == "save" or index.root.parent == index.root:
        return None
    try:
        above = index.root.parent / _safe_relative_path(entry.get("dest"), "dest")
        if above.stat().st_size != entry.get("size") or not above.is_file():
            return None
    except (OSError, ValueError):
        return None
    algorithm = "sha1" if entry.get("sha1") else "sha256"
    if index.digest(above, algorithm) != str(entry.get(algorithm, "")).lower():
        return None
    return LocalCopy(index, above)


def plan_local_copies(
    entries: list[dict], bios_path: Path, bios_index: FolderIndex | None,
    folders: list[FolderIndex],
) -> dict[int, LocalCopy]:
    """id(entry) -> where this machine already holds the entry's bytes: the
    BIOS folder itself first, a file misplaced in it, then each folder named
    with --from in turn. Every copy is found by size, then by hash."""
    if not entries or (bios_index is None and not folders):
        return {}
    # A file this run replaces is no source: its place is being written.
    written = set()
    for entry in entries:
        try:
            written.add(Path(os.path.realpath(_entry_path(bios_path, entry))))
        except ValueError:
            continue
    copies: dict[int, LocalCopy] = {}
    for entry in entries:
        copy = None
        if bios_index is not None:
            copy = bios_index.find(entry, written) or _above(entry, bios_index)
        if copy is not None:
            copy = copy._replace(misplaced=_misplaced(entry, copy, bios_index))
        for index in folders:
            if copy is not None:
                break
            copy = index.find(entry, written)
        if copy is not None:
            copies[id(entry)] = copy
    return copies


def _misplaced(entry: dict, copy: LocalCopy, index: FolderIndex) -> bool:
    """Whether a copy in the BIOS folder sits where no other entry of the
    list goes: GBA_BIOS.BIN for gba_bios.bin is a mistake, np2/2608_bd.wav
    for np2kai/2608_bd.wav is the other core's file."""
    if copy.member:
        return True
    rel = index.relative(copy.path)
    if rel in index.listed:
        return False
    own = str(entry.get("dest", "")).casefold()
    return rel.casefold() not in index.sealed or rel.casefold() == own


def _check_copy_lines(
    entries: list[dict], copies: dict[int, LocalCopy], bios_index: FolderIndex | None,
) -> list[str]:
    """What --check says a run would copy instead of downloading."""
    fixed = sum(1 for e in entries if id(e) in copies and copies[id(e)].misplaced)
    lines = []
    if fixed:
        verb = "is" if fixed == 1 else "are"
        them = "it" if fixed == 1 else "them"
        lines.append(f"  {fixed} of these files {verb} in the BIOS folder at the wrong place: "
                     f"a run without --check copies {them} where the cores open {them}, and "
                     f"leaves your {'copy' if fixed == 1 else 'copies'} as "
                     f"{'it is' if fixed == 1 else 'they are'}.")
    for label, count in _counts_by_folder(entries, copies, bios_index):
        them = "it" if count == 1 else "them"
        lines.append(f"  {label} holds {count} of these files: a run without --check "
                     f"copies {them}.")
    return lines


def _counts_by_folder(
    entries: list[dict], copies: dict[int, LocalCopy], bios_index: FolderIndex | None,
) -> list[tuple[str, int]]:
    """(folder, files) for each --from folder that gives some of entries."""
    counts: dict[str, int] = {}
    for entry in entries:
        copy = copies.get(id(entry))
        if copy is not None and copy.index is not bios_index:
            counts[copy.index.label] = counts.get(copy.index.label, 0) + 1
    return list(counts.items())


def copy_plan_lines(
    entries: list[dict], copies: dict[int, LocalCopy], bios_index: FolderIndex | None,
    bios_path: Path, files: list[dict], every: bool, planned: bool,
) -> list[str]:
    """The files a run copies from this machine, by folder: the misplaced
    ones with the reason each was not where its cores open it."""
    known = _known_destinations(files)
    # By --from folder, "" for the misplaced files and "=" for the ones the
    # BIOS folder holds at another entry's place.
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        copy = copies.get(id(entry))
        if copy is not None:
            key = (copy.index.label if copy.index is not bios_index
                   else "" if copy.misplaced else "=")
            groups.setdefault(key, []).append(entry)
    lines = []
    for key in sorted(groups, key=lambda k: (k != "", k != "=")):
        chosen = groups[key]
        sized = f"{_plural(len(chosen), 'file')} ({format_size(_bytes(chosen))})"
        if key == "":
            head = (f"Would fix {sized} misplaced in the BIOS folder, copied where the "
                    "cores open them:" if planned else
                    f"Fixing {sized} misplaced in the BIOS folder: copied where the cores "
                    "open them, your copies left as they are.")
        elif key == "=":
            head = (f"Would copy {sized} the BIOS folder already holds at another "
                    "place of the list:" if planned else
                    f"Copying {sized} the BIOS folder already holds at another place "
                    "of the list.")
        else:
            head = (f"Would copy {sized} from {key}:" if planned else
                    f"Copying {sized} from {key}, each one checked by hash.")
        lines.append(f"\n  {head}" if planned else f"  {head}")
        for entry in chosen if every else chosen[:10]:
            copy = copies[id(entry)]
            why = (_copy_reason(entry, copy, bios_path, known) if key == ""
                   else f"from {copy.where()}" if key == "=" else copy.where())
            lines.append(f"    {entry['dest']}: {why}" if why else f"    {entry['dest']}")
        if len(chosen) > 10 and not every:
            lines.append(f"    ... and {len(chosen) - 10} more (--verbose lists them all)")
    return lines


def emit_copies(
    plat: str, entries: list[dict], copies: dict[int, LocalCopy],
    bios_index: FolderIndex | None, bios_path: Path, files: list[dict],
) -> None:
    """One found event per file this machine holds, before it is copied."""
    known = _known_destinations(files)
    for entry in entries:
        copy = copies.get(id(entry))
        if copy is None:
            continue
        reason = (_copy_reason(entry, copy, bios_path, known)
                  if copy.index is bios_index else "")
        EVENTS.emit("found", platform=plat, dest=entry["dest"], folder=copy.index.label,
                    path=str(copy.path), member=copy.member, reason=reason)


class Unreadable(NamedTuple):
    """A file, ZIP member or folder below a --from folder that could not be
    read, and may be a file the run still needs."""
    index: FolderIndex
    path: Path
    member: str
    why: str

    def line(self) -> str:
        where = f"{self.member} inside {self.path}" if self.member else str(self.path)
        return f"{where} could not be read: {self.why}"


def unreadable_copies(
    entries: list[dict], copies: dict[int, LocalCopy], folders: list[FolderIndex],
) -> list[Unreadable]:
    """What the --from folders hold that could not be read, of the size of
    a file no copy was found for, or of no known size: dropped silently,
    a file a user had read as one to download."""
    sizes = {e["size"] for e in entries
             if id(e) not in copies and isinstance(e.get("size"), int)}
    if not sizes:
        return []
    return [Unreadable(index, *held) for index in folders
            for held in index.unreadable_for(sizes)]


def unreadable_lines(found: list[Unreadable], every: bool) -> list[str]:
    lines = [f"  {item.line()}" for item in (found if every else found[:10])]
    if len(found) > len(lines):
        lines.append(f"  ... and {len(found) - len(lines)} more could not be read "
                     "(--verbose lists them all)")
    return lines


def emit_unreadable(plat: str, found: list[Unreadable]) -> None:
    for item in found:
        EVENTS.emit("unreadable", platform=plat, folder=item.index.label,
                    path=str(item.path), member=item.member, reason=item.why)


def sources_line(
    folders: list[tuple[str, int]], fixed: int, downloaded: int, missing: int,
    fetched: str = "downloaded", shared: int = 0,
) -> str:
    """Where the files of a run that copied some came from, in words:
    "38 files copied from E:\\, 4 downloaded, 2 still missing." """
    parts = [(count, f"copied from {label}") for label, count in folders]
    parts.append((fixed, "fixed from copies already in the BIOS folder"))
    parts.append((shared, "copied from another place of the BIOS folder"))
    if not any(count for count, _what in parts):
        return ""
    parts += [(downloaded, fetched), (missing, "still missing")]
    words = []
    for count, what in parts:
        if count:
            words.append(f"{_plural(count, 'file') if not words else count} {what}")
    return ", ".join(words) + "."


# Below this a download is small enough that narrowing it is not worth a line.
BULK_HINT_FROM = 100 * 1024 * 1024


def _bytes(files: list[dict]) -> int:
    return sum(f.get("size", 0) for f in files)


def bulk_hint(
    to_download: list[dict], manifest: dict, systems_chosen: bool, cores_chosen: bool,
    accepted: Collection[str] = (),
) -> list[str]:
    """Name what pulls most of a download, when narrowing saves over half of it.

    `--system 3do` fetched 349 MB for RetroArch, and `--core opera` 12.5 MB:
    the arcade cores that also read 3DO pulled the rest, and nothing said so.
    accepted holds the names --core takes. Drawn from every core of the
    system, the advice named ares, which loads no file of its own, and
    `--core ares` was then refused.
    """
    total = _bytes(to_download)
    if total < BULK_HINT_FROM:
        return []
    systems = {s for entry in to_download for s in entry_systems(entry)}
    if not cores_chosen and len(systems) == 1:
        system = next(iter(systems))
        candidates = [core for core in (manifest.get("system_cores") or {}).get(system) or []
                      if core in accepted]
        by_core: dict[str, int] = {}
        for entry in to_download:
            for core in entry.get("cores") or []:
                by_core[core] = by_core.get(core, 0) + entry.get("size", 0)
        kept = {core: _bytes(_filter_by_target(to_download, [core])) for core in candidates}
        if by_core and kept:
            heavy = max(sorted(by_core), key=lambda core: by_core[core])
            light = min(sorted(kept), key=lambda core: kept[core])
            saved = total - kept[light]
            if saved * 2 > total:
                return [
                    f"  {heavy} pulls {format_size(by_core[heavy])} of this "
                    f"{format_size(total)} download for {system}. --core {light} "
                    f"keeps {format_size(kept[light])} of it and saves "
                    f"{format_size(saved)} ({saved * 100 // total}%)."
                ]
    if not systems_chosen and len(systems) > 1:
        alone: dict[str, int] = {}
        for entry in to_download:
            own = entry_systems(entry)
            if len(own) == 1:
                alone[own[0]] = alone.get(own[0], 0) + entry.get("size", 0)
        if alone:
            heavy = max(sorted(alone), key=lambda system: alone[system])
            if alone[heavy] * 2 > total:
                return [
                    f"  {heavy} pulls {format_size(alone[heavy])} of this "
                    f"{format_size(total)} download. --system naming the systems "
                    f"you play leaves it out and saves {format_size(alone[heavy])} "
                    f"({alone[heavy] * 100 // total}%), --list-systems names them."
                ]
    return []


def _help_epilog() -> str:
    """Examples in the form the run was started with, and where the rest is."""
    run = _INVOCATION.launcher()
    card = "E:\\bios" if _INVOCATION.powershell() else "/media/SHARE/bios"
    examples = (
        ("install for the platforms found here", run),
        ("prepare a card or a share for another device",
         f"{run} --platform batocera --dest {card}"),
        ("only some systems (names: --list-systems)",
         f"{run} --platform retroarch --system sony-playstation,nintendo-gba"),
        ("check what is installed, writing nothing", f"{run} --check"),
    )
    lines = ["examples:"]
    for what, command in examples:
        lines += [f"  {what}", f"    {command}"]
    lines += ["", f"Every option, the environment variables and the exit codes: {INSTALLER_PAGE}"]
    return "\n".join(lines)


def main() -> None:
    """Entry point."""
    _INVOCATION.os_type = detect_os()
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Download missing BIOS files for retrogaming emulators. "
                    "For questions line by line, as a screen reader reads them, "
                    "add --plain.",
        epilog=_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--platform",
        help="target platform (retroarch, batocera, emudeck, ...)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        help="BIOS folder to install into. Without --platform, the file list is "
             "the platform detected here, asked at a terminal, RetroArch's when "
             "nothing is detected",
    )
    parser.add_argument(
        "--target",
        help="hardware target for core filtering (switch, rpi4, ...); "
        "on Batocera, Recalbox, ROCKNIX and Lakka the board is read from the system",
    )
    parser.add_argument(
        "--every-board",
        action="store_true",
        help="install the files of every board, not only the detected one",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check existing files without downloading",
    )
    parser.add_argument(
        "--list-platforms",
        action="store_true",
        help="list detected platforms and exit",
    )
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="list available targets for a platform and exit",
    )
    parser.add_argument(
        "--jobs", "-j",
        type=int,
        default=8,
        help="parallel download threads (default: 8)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="verbose output",
    )
    parser.add_argument(
        "--standalone-copies",
        action="store_true",
        help="opt in to copies into detected standalone-emulator directories",
    )
    parser.add_argument(
        "--emulator",
        action="append",
        metavar="NAME",
        help="install what these emulators read on the platform, as a pack "
             "built for them carries (names: --list-emulators)",
    )
    parser.add_argument(
        "--installed-cores",
        action="store_true",
        help="install only what the cores already installed read "
             "(RetroArch, Lakka, RetroPie)",
    )
    parser.add_argument(
        "--save-dir",
        type=Path,
        metavar="PATH",
        help="the frontend's save directory (RetroArch: savefile_directory), "
             "for the files some cores read there (Azahar's keys and system "
             "data), when it is not found",
    )
    parser.add_argument(
        "--cores-dir",
        type=Path,
        metavar="PATH",
        help="the folder RetroArch loads its cores from, when it is not found "
             "(implies --installed-cores)",
    )
    parser.add_argument(
        "--system",
        action="append",
        metavar="NAME",
        help="install only these systems (repeat or comma-separate, see "
             "--list-systems)",
    )
    parser.add_argument(
        "--core",
        action="append",
        metavar="NAME",
        help="keep only the extra files these cores load (see --list-cores)",
    )
    parser.add_argument(
        "--region",
        action="append",
        metavar="LIST",
        help="preferred regions, best first, e.g. us,eu,jp: keep one BIOS per "
             "region where a system has several",
    )
    parser.add_argument(
        "--no-game-data",
        action="store_true",
        help="leave out game data (files a game reads, not firmware) and "
             "install the firmware only",
    )
    parser.add_argument(
        "--list-systems",
        action="store_true",
        help="list a platform's systems with their file counts and exit",
    )
    parser.add_argument(
        "--list-cores",
        action="store_true",
        help="list the cores whose extra files a platform installs and exit",
    )
    parser.add_argument(
        "--romm-firmware",
        metavar="TEMPLATE",
        help="RomM: where each platform's firmware goes in the library, as "
             "filesystem.structure.firmware in its config.yml (default: read "
             "from config.yml, else bios/{platform})",
    )
    parser.add_argument(
        "--list-emulators",
        action="store_true",
        help="list every name --emulator accepts for a platform and exit",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="never ask a question and fail where an answer is needed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be downloaded, and how much, without writing anything",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="do not ask before a large download",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="print only errors and the final summary (implies --no-input)",
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="write the run as JSON events on stdout, one per line, and the "
             "usual text on stderr (implies --no-input)",
    )
    parser.add_argument(
        "--restore",
        type=Path,
        metavar="FOLDER",
        help=f"put back the files an earlier run replaced and kept in FOLDER "
             f"(a dated folder inside {BACKUP_DIR})",
    )
    parser.add_argument(
        "--from-pack",
        action="append",
        type=Path,
        metavar="PATH",
        help="install from a pack already downloaded: its ZIP, one of its parts "
             "(the others are found beside it) or the folder holding them. "
             "Makes no network request, and checks every file's hash",
    )
    parser.add_argument(
        "--from",
        action="append",
        type=Path,
        metavar="FOLDER",
        help="copy the BIOS files found in FOLDER (an old SD card, a backup, a "
             "ZIP) instead of downloading them: every file recognised by its "
             "hash, never moved. Repeat for several folders",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        metavar="FILE",
        help="read the file list from FILE (install/<platform>.json) instead "
             "of downloading it",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="open the full-screen interface, which a keyboard or a controller "
             "drives. It opens by itself for an install at a terminal of 40x12 "
             "or more",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="ask the questions line by line instead of the full-screen "
             "interface, as a screen reader reads them (RETROBIOS_PLAIN=1 or "
             "ACCESSIBLE=1 do the same)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"retrobios installer {installer_id()} (ref {RELEASE_REF})",
        help="print the installer version and exit",
    )

    args = parser.parse_args()
    if args.verbose:
        _RAW_ERRORS.set()
    if args.cores_dir:
        args.installed_cores = True
    _INVOCATION.record(args)
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be between 1 and 32")
    if args.region:
        try:
            resolve_regions(args.region)
        except ValueError as exc:
            parser.error(str(exc))
    for option, values in (("--system", args.system), ("--core", args.core)):
        if values is not None and not any(
            token.strip() for value in values for token in value.split(",")
        ):
            parser.error(f"{option} needs at least one name")
    if args.quiet and (
        args.check or args.dry_run or args.list_platforms or args.list_targets
        or args.list_systems or args.list_cores or args.list_emulators
    ):
        parser.error("--quiet applies to an install, not to --check, --dry-run or a listing")
    if args.tui and args.plain:
        parser.error("--tui and --plain ask for two different interfaces")
    if args.tui:
        reason = tui_blocker(args)
        if reason:
            # The reason alone: fourteen lines of usage hid it.
            print(f"{parser.prog}: error: --tui cannot open the full-screen interface "
                  f"here: {reason}", file=sys.stderr)
            sys.exit(2)
    if args.restore is not None:
        # The folder says where each file goes: nothing else applies.
        alone = ("platform", "dest", "target", "every_board", "check", "list_platforms",
                 "list_targets", "standalone_copies", "emulator", "installed_cores",
                 "cores_dir", "save_dir", "system", "core", "region", "no_game_data",
                 "list_systems",
                 "list_cores", "romm_firmware", "list_emulators", "events", "quiet",
                 "yes", "verbose", "from_pack", "manifest", "from")
        given = [name for name in alone if getattr(args, name)]
        if args.jobs != 8:
            given.append("jobs")
        if given:
            parser.error("--restore puts files back from a folder and takes no "
                         f"--{given[0].replace('_', '-')}")
        sys.exit(restore_kept(args.restore, args.dry_run))
    if args.every_board and args.target:
        parser.error("--every-board installs every board; --target names one")
    if args.from_pack and args.target:
        parser.error("--target reads a core list from the network, which --from-pack "
                     "does not use")
    if getattr(args, "from") and (
        args.list_platforms or args.list_targets or args.list_systems
        or args.list_cores or args.list_emulators
    ):
        parser.error("--from copies files into the BIOS folder, which a listing "
                     "does not write")
    if (args.from_pack or args.manifest) and (args.list_platforms or args.list_targets):
        parser.error("--from-pack and --manifest give a file list, which --list-platforms "
                     "and --list-targets do not read")
    if args.no_game_data and (args.list_platforms or args.list_targets):
        parser.error("--no-game-data narrows a file list, not --list-platforms "
                     "or --list-targets")
    if args.tui or not (plain_requested(args) or tui_blocker(args)):
        sys.exit(run_tui(args))
    if args.events:
        # The events own stdout. The text a person reads goes to stderr, so a
        # program never has to tell one from the other.
        args.no_input = True
        EVENTS.open(sys.stdout)
        sys.stdout = sys.stderr
        EVENTS.emit("start", schema=EVENT_SCHEMA, installer=installer_id(),
                    mode=_event_mode(args))
        try:
            _run(args)
        except SystemExit as exc:
            EVENTS.emit("end", code=_event_exit_code(exc.code))
            raise
        except KeyboardInterrupt:
            EVENTS.emit("end", code=130)
            raise
        EVENTS.emit("end", code=0)
        return
    _run(args)


def _run(args: argparse.Namespace) -> None:
    """The run, its text quiet or not."""
    if not args.quiet:
        _install(args, sys.stdout)
        return
    # Quiet: what a run prints is kept, and shown on stderr if the run fails,
    # so an error printed on stdout is never lost.
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        _install(args, real_stdout)
    except SystemExit as exc:
        if sys.stdout is captured:
            sys.stdout = real_stdout
            if exc.code not in (0, None):
                sys.stderr.write(captured.getvalue())
        raise
    finally:
        if sys.stdout is captured:
            sys.stdout = real_stdout


def _open_local_sources(args: argparse.Namespace) -> None:
    """Take the file list and the pack this run was given, and the platform
    they are for when none is named."""
    LOCAL.reset()
    _open_from_folders(getattr(args, "from", None) or [])
    if args.manifest is not None:
        LOCAL.manifest = args.manifest
        named = local_manifest_platform(args.manifest)
        if args.platform is None and named:
            args.platform = named
            print(f"  File list: {args.manifest} ({display_name(named)})")
    if not args.from_pack:
        return
    try:
        LOCAL.pack = PackSource(args.from_pack)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    pack = LOCAL.pack
    parts = len(pack.files)
    print(f"  Pack: {pack.name}"
          + (f", {parts} parts" if parts > 1 or _PART_FILE.match(pack.files[0].name) else ""))
    spelled = pack.platforms()
    if args.platform is not None:
        plat = normalize_platform(args.platform)
        if spelled and plat not in spelled:
            print(f"Error: {pack.name} is a pack for "
                  f"{' and '.join(display_name(n) for n in spelled)}, not "
                  f"{display_name(plat)}.", file=sys.stderr)
            sys.exit(1)
    elif len(spelled) == 1:
        args.platform = spelled[0]
    else:
        among = (" (" + ", ".join(spelled) + ")") if spelled else ""
        print(f"Error: name the platform {pack.name} is installed for with "
              f"--platform{among}.", file=sys.stderr)
        sys.exit(1)


def _open_from_folders(folders: list[Path]) -> None:
    """Read each folder named with --from, or stop on one that is not there."""
    for folder in folders:
        label = str(folder)
        folder = Path(os.path.expanduser(label))
        zipped = folder.is_file() and folder.name.casefold().endswith(".zip")
        if not (folder.is_dir() or zipped):
            reason = "not_a_folder" if folder.exists() else "not_found"
            what = ("does not exist" if reason == "not_found"
                    else "is neither a folder nor a ZIP")
            message = f"{label} {what}: --from names the folder to copy BIOS files from"
            EVENTS.emit("error", stage="from", reason=reason, platform="", message=message)
            print(f"Error: {message}.", file=sys.stderr)
            sys.exit(1)
        index = FolderIndex(folder, label)
        LOCAL.folders.append(index)
        print(f"  Copying BIOS files from: {label}")
        if index.truncated:
            print(f"  Read the first {MAX_SCANNED_FILES:,} files of {label} only. Name "
                  "the folder that holds the BIOS files to search it all.")


def _missing_dest(dest: Path, ask: bool, named: str = "named with --dest") -> None:
    """Say that a --dest folder does not exist before anything goes there.

    A typo (~/.config/retroarh/system) was created with every missing
    parent, filled, and reported done in a folder no platform reads, and
    --check said only "0/7 present". One missing level is a BIOS folder not
    made yet and is created. Several make a path that may be mistyped:
    at a terminal the run asks first, and the answer defaults to no.
    """
    if dest.exists():
        return
    nearest = dest.absolute()
    while not nearest.exists() and nearest.parent != nearest:
        nearest = nearest.parent
    missing = len(dest.absolute().relative_to(nearest).parts)
    print(f"  {dest} does not exist. The nearest folder that does: {nearest}")
    if missing > 1 and ask:
        answer = _ask(f"  Create {_plural(missing, 'folder')} under {nearest}? [y/N] ")
        if not answer.strip().lower().startswith("y"):
            print(f"  Nothing installed. Check the folder {named}, then run "
                  "the command again.")
            sys.exit(2)


def _no_core_found(plat: str, bios_path: Path, searched: list[Path], dest_given: bool) -> None:
    """Refuse --installed-cores with no core, and say how to point at them.

    With --dest, detection does not run, and without a retroarch.cfg beside
    the folder nothing names the cores folder: the run stopped on "no known
    directory" with no way forward.
    """
    where = (f"none in {', '.join(map(str, searched))}" if searched
             else f"no retroarch.cfg was found for {bios_path} to name their folder")
    print(f"Error: no installed core found for {display_name(plat)}: {where}.",
          file=sys.stderr)
    print("  Name the folder RetroArch loads cores from (libretro_directory in "
          "retroarch.cfg, cores/ beside RetroArch by default):", file=sys.stderr)
    command = _INVOCATION.command(plat, bios_path, without=("--installed-cores", "--cores-dir"))
    print(f"    {command} --cores-dir <cores folder>", file=sys.stderr)
    if dest_given:
        print("  Or run it without --dest: detection reads retroarch.cfg, which names "
              "that folder.", file=sys.stderr)
    print("  Or run it without --installed-cores to install every file.", file=sys.stderr)
    sys.exit(1)


def card_platform(root: Path) -> str | None:
    """The frontend whose card ``root`` is, from the release name it wrote."""
    for name, marker, prefix in _CARD_RULES:
        try:
            with open(root / marker, encoding="utf-8", errors="replace") as handle:
                first = handle.readline().strip()
        except OSError:
            continue
        if first.startswith(prefix):
            return name
    return None


def _check_card_root(plat: str, dest: Path) -> None:
    """Refuse a folder inside the card where its root is meant.

    The file list writes Bios/<TAG>/ below --dest: given the card's Bios
    folder, every file would land in Bios/Bios/<TAG>/, which no core reads.
    """
    found = card_platform(dest)
    if found is None:
        for parent in list(dest.absolute().parents)[:2]:
            holder = card_platform(parent)
            if holder:
                print(f"Error: {dest} is inside a {display_name(holder)} card. "
                      f"Name the card's root: --dest {parent}", file=sys.stderr)
                sys.exit(1)
        return
    if found != plat:
        print(f"  Note: this card holds {display_name(found)} ({CARD_MARKER}), and the "
              f"{display_name(plat)} file list was asked for.")


def _platform_for_dest(os_type: str, dest: Path, interactive: bool) -> str:
    """The file list for a --dest given without --platform.

    A folder says where, not for whom, and a card is often prepared on a PC
    for a console: --dest alone installed RetroArch's names and layout into
    a Batocera share. The platforms detected here are the evidence. At a
    terminal the user picks, Enter taking the one found. Otherwise the one
    found is used, RetroArch when none is, and several found are refused.
    """
    card = card_platform(dest)
    if card:
        print(f"  File list: {display_name(card)} (the card's {CARD_MARKER}). "
              f"Same choice without detection: --platform {card}")
        return card
    print("Detecting platform...")
    found = list(dict.fromkeys(name for name, _path in detect_platforms(os_type)))
    default = found[0] if len(found) == 1 else "" if found else "retroarch"
    if interactive:
        plat = _choose_platform(f"Which platform's files go to {dest}?", default)
        print(f"  File list: {display_name(plat)}. Same choice without questions: "
              f"--platform {plat}")
        return plat
    if not default:
        print(f"Error: {' and '.join(display_name(n) for n in found)} found here. "
              f"Name the platform {dest} is for with --platform.", file=sys.stderr)
        sys.exit(1)
    why = "the platform found here" if found else "nothing was detected here"
    print(f"  File list: {display_name(default)} ({why}). For another platform's "
          "list, add --platform (--list-platforms names them).")
    return default


def _unknown_target(target: str, plat: str, targets: dict) -> None:
    print(f"Error: unknown target '{target}' for {plat}.", file=sys.stderr)
    if targets:
        print("  " + did_you_mean([target], sorted(targets), "--list-targets"),
              file=sys.stderr)
    else:
        print(f"  {plat} publishes no target list", file=sys.stderr)


def _install(args: argparse.Namespace, real_stdout: TextIO) -> None:
    """One run of the installer, once the options are read."""
    KEPT.reset()
    term = Terminal(real_stdout, quiet=args.quiet)
    interactive = sys.stdin.isatty() and not args.no_input and not args.quiet
    listing = (args.list_targets or args.list_systems or args.list_cores
               or args.list_emulators)
    print(f"{term.bold('RetroBIOS')} {term.dim(installer_id())}\n")

    os_type = detect_os()
    if os_type == "wsl":
        _enter_wsl()
    _open_local_sources(args)

    # Early exit for listing
    if args.list_platforms:
        print("Available platforms:")
        for p in AVAILABLE_PLATFORMS:
            print(f"  {p}")
        print("\nSystems recognized by their own files, and the file list each gets "
              "(--platform takes either name):")
        for line in _system_lines():
            print(f"  {line}")
        detected = detect_platforms(os_type)
        if detected:
            print("\nDetected on this system:")
            for name, path in detected:
                system = detected_os(name, path)
                print(f"  {name}: {path}" + (f" ({system})" if system else ""))
        else:
            print("\nDetected on this system: none")
        return

    # Platform detection or override. A destination taken from the default
    # table, the platform not being found here, is only a guess.
    assumed_dests: set[Path] = set()
    if args.platform:
        args.platform = normalize_platform(args.platform)
    if args.platform and args.dest:
        if args.platform in CARD_PLATFORMS:
            _check_card_root(args.platform, args.dest)
        platforms = [(args.platform, args.dest)]
    elif args.platform:
        print("Detecting platform...")
        detected = detect_platforms(os_type)
        matched = [(n, p) for n, p in detected if n == args.platform]
        if matched:
            platforms = matched
        elif listing:
            # A listing reads the manifest and writes nothing: no folder is
            # needed, and asking BizHawk's for one refused a question asked
            # before downloading BizHawk.
            platforms = [(args.platform, _default_dest(os_type, args.platform) or Path.cwd())]
        elif args.platform in CARD_PLATFORMS:
            # Its files go on its SD card, which is never this machine's
            # own folder: the card's mount point is the user's to name.
            print(
                f"  {display_name(args.platform)} keeps its BIOS files on its SD card. "
                f"Mount the card and name its root with --dest, for example: "
                f"--platform {args.platform} --dest /media/$USER/<card> "
                f"(Windows: --dest E:\\)",
                file=sys.stderr,
            )
            sys.exit(1)
        elif args.platform in PORTABLE_ONLY:
            # A portable emulator reads the folder beside its executable:
            # any default would be a folder it never opens.
            print(
                f"  Platform '{args.platform}' not found. {PORTABLE_ONLY[args.platform]} "
                f"Name it with --dest, for example: --platform {args.platform} "
                f"--dest '<BizHawk folder>{os.sep}Firmware'",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            default_dest = _default_dest(os_type, args.platform)
            if default_dest is None:
                print(
                    f"  Platform '{args.platform}' not found, and no folder on this "
                    f"system is one it reads. Name it with --dest, for example: "
                    f"--platform {args.platform} --dest <its BIOS folder>",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"  Platform '{args.platform}' not detected, "
                f"using default path: {default_dest}"
            )
            platforms = [(args.platform, default_dest)]
            assumed_dests.add(default_dest)
    elif args.dest:
        print(f"  Using destination: {args.dest}")
        platforms = [(_platform_for_dest(os_type, args.dest, interactive and not listing),
                      args.dest)]
    else:
        print("Detecting platform...")
        frontends = detect_frontends(os_type)
        if "esde" in frontends:
            print("  Found ES-DE (BIOS files go to each emulator, not to ES-DE itself)")
        if "launchbox" in frontends:
            print("  Found LaunchBox")
        platforms = detect_platforms(os_type)
        if not platforms:
            print("  No supported platform detected.")
            EVENTS.emit("detect", os=os_type, os_name=_os_name(), platforms=[])
            # Standalone emulators read their BIOS from folders of their own,
            # which the file lists name: an ES-DE user with DuckStation and
            # PCSX2 was sent to RetroArch's folder, which nothing here reads.
            narrowing = (args.system or args.core or args.emulator or args.region
                         or args.target or args.installed_cores)
            if not listing and not narrowing and _install_standalone_folders(
                args, os_type, term, interactive
            ):
                return
            if not interactive or listing:
                asked = next((flag for flag, on in (
                    ("--list-systems", args.list_systems), ("--list-cores", args.list_cores),
                    ("--list-emulators", args.list_emulators),
                    ("--list-targets", args.list_targets),
                ) if on), "")
                for line in _manual_usage_hint(os_type, asked):
                    print(line)
                sys.exit(1)
            platforms = _prompt_manual_platform(os_type)
            plat, typed = platforms[0]
            if typed != _default_dest(os_type, plat):
                _missing_dest(typed, not (args.check or args.dry_run or args.yes),
                              "typed")
        else:
            for name, path in platforms:
                print(f"  Found {found_label(name, path)}")

    conflict = os_conflict(platforms)
    if conflict and not interactive and not listing:
        # A machine runs one system: installing to each folder would write
        # where nothing reads, and picking one would be a guess.
        message = (f"{' and '.join(conflict)} are both detected here, and a "
                   "machine runs one")
        EVENTS.emit("error", stage="detect", reason="conflict", platform="",
                    message=message)
        print(f"Error: {message}. Name the folder with --dest (and --platform for "
              "its file list).",
              file=sys.stderr)
        sys.exit(1)
    if platforms:
        EVENTS.emit("detect", os=os_type, os_name=_os_name(), platforms=[
            {"platform": name, "name": display_name(name), "bios_dir": str(path)}
            for name, path in platforms
        ])

    if len(platforms) > 1 and not listing and interactive:
        # --check installs nothing, and "Install for:" said it would.
        platforms = _prompt_platform_choice(
            platforms, "Check:" if args.check else "Install for:")

    if args.dest is not None and not listing:
        _missing_dest(args.dest, interactive and not (args.check or args.dry_run or args.yes))

    total_downloaded = 0
    total_up_to_date = 0
    total_errors = 0
    total_omitted = 0
    omitted_seen: list[dict] = []
    left_out_seen: list[dict] = []
    total_interrupted = False
    total_bytes = 0
    total_seconds = 0.0
    total_declined = 0
    check_pending = 0
    total_verified = 0
    files_pending = 0
    failures: list[tuple[str, str]] = []
    installed_paths: list[Path] = []
    bulk_lines: list[str] = []
    # The BIOS folders read for misplaced files, one per platform.
    bios_indexes: list[FolderIndex] = []

    # --target is checked for every platform before anything is written, as
    # --installed-cores is below: EmuDeck installed 3.2 GB and only then was
    # steamos refused for RetroArch, with no summary of what had landed.
    target_lists: dict[str, dict] = {}
    if args.target and not args.list_targets:
        for plat_name, _bios_path in platforms:
            targets = fetch_targets(plat_name)
            if targets is None:
                sys.exit(1)
            target_lists[plat_name] = targets
            if args.target not in targets:
                _unknown_target(args.target, plat_name, targets)
                if len(platforms) > 1:
                    print("  Nothing was installed. Name one platform with "
                          "--platform to install it alone.", file=sys.stderr)
                sys.exit(1)

    if args.romm_firmware is not None:
        # Applied or refused, never ignored.
        if not any(name == "romm" for name, _path in platforms):
            print("Error: --romm-firmware applies to RomM, which this run does not "
                  "install for.", file=sys.stderr)
            sys.exit(1)
        try:
            parse_romm_firmware(args.romm_firmware)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    # Every check --installed-cores makes before anything is written: a
    # refusal for the second platform must not follow an install of the first.
    cores_found: dict[tuple[str, Path], list[str]] = {}
    for plat_name, bios_path in platforms:
        if plat_name in CORE_PLATFORMS:
            searched = [args.cores_dir] if args.cores_dir else core_dirs(plat_name, bios_path)
            cores_found[(plat_name, bios_path)] = installed_cores(searched)
        elif args.installed_cores:
            print(
                f"Error: {plat_name} ships its cores with the system; "
                f"--installed-cores applies to {', '.join(CORE_PLATFORMS)}",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.installed_cores and not cores_found[(plat_name, bios_path)]:
            _no_core_found(plat_name, bios_path, searched, bool(args.dest))

    for plat_name, bios_path in platforms:
        if bios_path not in installed_paths:
            installed_paths.append(bios_path)
        if LOCAL.manifest is not None:
            print(f"\nReading the file index for {plat_name} from {LOCAL.manifest}...")
        elif LOCAL.pack is not None:
            print(f"\nReading the file index for {plat_name} from {LOCAL.pack.name}...")
        else:
            print(f"\nFetching file index for {plat_name}...")
        manifest = fetch_manifest(plat_name)
        files = manifest.get("files", [])
        omitted_files = manifest.get("omitted_files", [])
        variants = manifest.get("case_variants", [])
        case_kept = keeps_case(bios_path)
        if variants and case_kept:
            # The list is folded for Windows and macOS, but here bios.col and
            # BIOS.col are two files that two cores open.
            files = files + [{**variant, "case_variant": True} for variant in variants]
        if not case_kept:
            files, folded = fold_case_twins(files)
            if folded:
                # Batocera's share opened over SMB, an exFAT card: the second
                # spelling overwrote the first, and every --check found one
                # of them wrong and fetched it again.
                shown = ", ".join(folded[:3]) + (" ..." if len(folded) > 3 else "")
                print(
                    f"  {_plural(len(folded), 'file')} left out: {bios_path} does not keep "
                    f"case and holds another file under the same name ({shown})"
                )

        game_data_cut = 0
        if args.no_game_data:
            if not manifest.get("categorized"):
                # Without the field nothing is marked, and the run would
                # install the game data it was asked to leave out.
                print(f"Error: the {plat_name} file list records no file category, "
                      "which --no-game-data needs.", file=sys.stderr)
                print(_older_list_advice(), file=sys.stderr)
                sys.exit(1)
            before = len(files)
            files, omitted_files, left_out = leave_out_game_data(files, omitted_files)
            game_data_cut = before - len(files)
            left_out_seen.extend(left_out)
            if left_out:
                for line in _left_out_lines(left_out, args.verbose):
                    print(line)

        if args.list_targets:
            targets = fetch_targets(plat_name)
            if targets is None:
                sys.exit(1)
            if not targets:
                print(f"  No targets available for {plat_name}")
            else:
                for t in sorted(targets.keys()):
                    cores = targets[t].get("cores")
                    label = "no core list" if cores is None else f"{len(cores)} cores"
                    print(f"  {t} ({label})")
            continue

        # Target filtering. With no --target, a system that names its board
        # narrows to it: a Batocera RPi4 took 8.6 GiB where its board needs
        # 5.5. Only on the device itself, where the board file exists.
        target = args.target
        if not target and not args.every_board and args.dest is None and not LOCAL.offline:
            board = detect_board(plat_name)
            if board:
                known = fetch_targets(plat_name) or {}
                if board in known:
                    target = board
                    print(f"  Board {board} detected (--every-board installs all boards)")
                else:
                    print(f"  Board {board} has no {PLATFORM_NAMES.get(plat_name, plat_name)} "
                          "core list: installing the files of every board")
            else:
                derived = derivative_board(plat_name)
                if derived:
                    print(f"  {derived[0]} board {derived[1]} detected. {derived[0]} "
                          "builds its own set of cores and publishes no core list "
                          "per board: installing the files of every board")
        if target:
            targets = target_lists.get(plat_name)
            if targets is None:
                targets = fetch_targets(plat_name)
            if targets is None:
                sys.exit(1)
            target_info = targets.get(target)
            if target_info is None:
                # Carrying on would install every file, which is the opposite
                # of what a filter was asked for: a 4 GB download instead of
                # the subset the target needs. Refuse rather than ignore.
                _unknown_target(target, plat_name, targets)
                sys.exit(1)
            elif target_info.get("cores") is None:
                print(
                    f"  Target '{target}' publishes no core list; "
                    "installing every file"
                )
            else:
                before = len(files)
                files, omitted_files = narrow_to_cores(
                    manifest, target_info["cores"], files, omitted_files
                )
                print(f"  Filtered {before} -> {len(files)} files for target {target}")

        found_cores = cores_found.get((plat_name, bios_path), [])
        if "system_cores" not in manifest:
            found_cores = []
        elif found_cores:
            print(f"  {_plural(len(found_cores), 'core')} installed")
        if args.installed_cores:
            if "system_cores" not in manifest:
                print(
                    f"Error: the {plat_name} file list does not say which cores "
                    "read which systems, which --installed-cores needs.",
                    file=sys.stderr,
                )
                print(_older_list_advice(), file=sys.stderr)
                sys.exit(1)
            before = len(files)
            files, omitted_files = narrow_to_cores(
                manifest, found_cores, files, omitted_files
            )
            print(f"  Narrowed {before} -> {len(files)} files to the installed cores")

        if args.list_emulators:
            _print_emulators(plat_name, manifest, files, omitted_files)
            continue

        if args.emulator:
            files, omitted_files = _narrow_to_emulators(
                args.emulator, manifest, plat_name, files, omitted_files
            )

        # What --core resolves against, for advice that names one.
        core_choices = set(available_choices(files, omitted_files)["cores"])
        selection = _requested_narrowing(args, plat_name, files, omitted_files, manifest)
        # What declining the size question points back at, when it was asked.
        smaller = ""
        if (
            selection is None
            and interactive
            and not args.check
            and not listing
            and records_selection(files)
        ):
            installed = (
                narrow_to_cores(manifest, found_cores, files, omitted_files)
                if found_cores and not args.installed_cores else None
            )
            selection = _prompt_custom_selection(
                plat_name, files, omitted_files, installed,
                display_name(plat_name, manifest),
            )
            if selection is None:
                smaller = ("  Nothing downloaded. To install less, run the same command "
                           "again and type c at the first question to choose systems")
                if installed is not None:
                    smaller += (f", or i for only what the installed cores read "
                                f"({format_size(_bytes(installed[0]))})")
                smaller += "."
            if selection == INSTALLED_ONLY:
                files, omitted_files = installed
                print("\n  Same choice without questions: "
                      f"--platform {plat_name} --installed-cores"
                      + (" --no-game-data" if args.no_game_data else ""))
                _INVOCATION.chosen[plat_name] = [("--installed-cores", None)]
                selection = None
            elif selection is not None:
                print("\n  Same choice without questions: "
                      + _same_choice_hint(plat_name, *selection)
                      + (" --no-game-data" if args.no_game_data else ""))
                systems, cores, regions = selection
                _INVOCATION.chosen[plat_name] = [
                    *(("--system", name) for name in systems),
                    *(("--core", name) for name in cores),
                    *([("--region", ",".join(regions))] if regions else []),
                ]
        if selection is not None:
            before = len(files)
            files, omitted_files = narrow(files, omitted_files, *selection)
            print(f"  Narrowed {before} -> {len(files)} files")

        if args.list_systems or args.list_cores:
            _print_choices(plat_name, files, omitted_files, args.list_systems, manifest)
            continue

        total_size = sum(f.get("size", 0) for f in files)
        print(f"  {len(files)} files ({format_size(total_size)})")
        if omitted_files:
            required_omitted = sum(
                1 for entry in omitted_files if entry.get("required", True)
            )
            reasons: dict[str, int] = {}
            for entry in omitted_files:
                reason = entry.get("reason", "unknown")
                reasons[reason] = reasons.get(reason, 0) + 1
            reason_summary = ", ".join(
                f"{OMISSION_REASONS.get(reason, reason)}: {count}"
                for reason, count in sorted(reasons.items())
            )
            print(
                f"  Not installable: {_plural(len(omitted_files), 'file')} "
                f"({required_omitted} required, {reason_summary})."
            )
            total_omitted += len(omitted_files)
            omitted_seen.extend(omitted_files)

        if plat_name == "romm":
            try:
                layout = romm_firmware_layout(bios_path, args.romm_firmware)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if layout is not None:
                files, library = romm_relayout(files, bios_path, *layout)
                if library != bios_path:
                    installed_paths[installed_paths.index(bios_path)] = library
                    print(f"  Firmware laid out as {'/'.join(layout[0] + ('{platform}',) + layout[1])} "
                          f"under {library}")
                    bios_path = library

        files, unplaced = place_save_entries(
            files, bios_path, manifest, args.save_dir, display_name(plat_name, manifest)
        )
        if unplaced:
            total_omitted += len(unplaced)
            omitted_seen.extend(unplaced)

        print("\nChecking existing files...")
        to_download, up_to_date, mismatched = check_local(
            files, bios_path, jobs=args.jobs, term=term
        )
        present = len(up_to_date) + len(mismatched)
        print(
            f"  {present}/{len(files)} present "
            f"({len(up_to_date)} verified, {len(mismatched)} wrong hash)"
        )

        # A partial copy left by an interrupted run is useless once the file
        # is in place: it goes.
        for entry in up_to_date:
            try:
                stale = _entry_path(bios_path, entry)
            except ValueError:
                continue
            stale = stale.parent / f".{stale.name}.part"
            if stale.is_file():
                stale.unlink()

        # Mismatched files need re-download
        to_download.extend(mismatched)
        # The files this machine already holds: misplaced in the BIOS folder
        # (bios/bios/, a pack extracted into a folder of its own, a name in
        # capitals, still zipped), or in a folder named with --from.
        bios_index = (bios_folder_index(bios_path, files, up_to_date)
                      if to_download else None)
        if bios_index is not None:
            bios_indexes.append(bios_index)
        copies = plan_local_copies(to_download, bios_path, bios_index, LOCAL.folders)
        unread = unreadable_copies(to_download, copies, LOCAL.folders)
        hint = [] if LOCAL.pack is not None else bulk_hint(
            to_download, manifest,
            systems_chosen=bool(selection and selection[0]),
            cores_chosen=bool((selection and selection[1]) or args.emulator
                              or args.installed_cores or target),
            accepted=core_choices,
        )

        narrowed = bool(
            target or args.system or args.core or args.region or args.installed_cores
            or len(files) + game_data_cut + len(unplaced)
            != len(manifest.get("files", [])) + len(variants if case_kept else [])
        )
        data_dirs = select_data_directories(manifest, files, narrowed)
        data_states = [
            (entry, data_directory_state(entry, bios_path)) for entry in data_dirs
        ] if args.check or EVENTS.on else []
        if EVENTS.on:
            _emit_check(
                plat_name, manifest, bios_path, files, up_to_date, mismatched,
                sum(len(state[1]) for _entry, state in data_states), len(omitted_files),
                chosen=selection[0] if selection else None,
            )
            emit_copies(plat_name, to_download, copies, bios_index, bios_path, files)
            emit_unreadable(plat_name, unread)
        total_verified += len(up_to_date)

        if args.check:
            if to_download:
                # Named, not only counted: a user looking for the one bad
                # BIOS learned "1 files need downloading" and nothing else.
                # A wrong hash comes first and always in full: listed after
                # 8000 missing files, it hid behind "... and 8059 more".
                wrong = {id(entry) for entry in mismatched}
                missing = [entry for entry in to_download if id(entry) not in wrong]
                # A pack extracted from some of its parts: the part is named
                # before the hundreds of files it holds.
                for line in split_pack_lines(bios_path):
                    print(line)
                print(f"\n  {_plural(len(to_download), 'file')} to download:")
                for line in check_lines(missing, mismatched, files, manifest, bios_path,
                                        args.verbose, up_to_date, bios_index, copies):
                    print(line)
                for line in unreadable_lines(unread, args.verbose):
                    print(line)
                print("  Run the same command without --check to fetch them; "
                      "a file with a wrong hash is replaced, and the one in place "
                      f"kept in {BACKUP_DIR} beside the BIOS folder.")
                check_pending += len(to_download)
            else:
                print("\n  All files up to date.")
            absent = _absent_required(omitted_files, bios_path)
            # The collection cannot supply these, so they leave the exit
            # code alone, but a user asking why a game will not start needs
            # their names, and which system and emulator each one is for.
            for line in _needed_lines(
                absent, "Not in the collection and absent here, needed to start an emulator",
                every=args.verbose,
            ):
                print(line)
            for entry, (_root, needed, present) in data_states:
                if needed:
                    print(f"  {entry['ref']}: {present}/{len(entry['files'])} data files "
                          f"present, {len(needed)} to fetch")
                    check_pending += len(needed)
            continue

        if args.dry_run:
            files_pending += len(to_download)
            trees = [(entry, data_directory_state(entry, bios_path)[1]) for entry in data_dirs]
            # A data file in place with other contents is kept like a BIOS file.
            kept = plan_kept(mismatched + [
                {"dest": f"{entry['dest']}/{rel}" if entry.get("dest") else rel}
                for entry, needed in trees for rel in needed
            ], bios_path)
            _print_plan([f for f in to_download if id(f) not in copies], bios_path, term,
                        trees, kept, copy_plan_lines(to_download, copies, bios_index,
                                                     bios_path, files, args.verbose, True))
            for line in unreadable_lines(unread, args.verbose):
                print(line)
            for line in hint:
                print(line)
            continue

        if to_download:
            dl_size = sum(f.get("size", 0) for f in to_download)
            fetched = [f for f in to_download if id(f) not in copies]
            fetch_size = _bytes(fetched)
            if (
                interactive
                and not args.yes
                and fetch_size >= CONFIRM_FROM_BYTES
                and not _confirm(
                    f"\nDownload {format_size(fetch_size)} to {bios_path}? [Y/n] "
                )
            ):
                # A beginner who just passed the c entry was sent to a
                # 91-line --help.
                print(smaller or "  Nothing downloaded. --system, --core and --region "
                      "install less (see --help).")
                total_up_to_date += len(up_to_date)
                total_declined += len(to_download)
                files_pending += len(to_download)
                continue
            EVENTS.emit("download", platform=plat_name, files=len(to_download), bytes=dl_size)
            if unread:
                print()
                for line in unreadable_lines(unread, args.verbose):
                    print(line)
            if copies:
                print()
                for line in copy_plan_lines(to_download, copies, bios_index, bios_path,
                                            files, args.verbose, False):
                    print(line)
                rest = (f" and {'copying' if LOCAL.pack is not None else 'downloading'} "
                        f"{len(fetched)}" if fetched else "")
                where = f" from {LOCAL.pack.name}" if fetched and LOCAL.pack is not None else ""
                print(f"\nCopying {_plural(len(copies), 'file')} found on this machine"
                      f"{rest}{where} ({format_size(dl_size)})...")
            elif LOCAL.pack is not None:
                print(f"\nCopying {len(to_download)} files ({format_size(dl_size)}) "
                      f"from {LOCAL.pack.name}...")
            else:
                print(f"\nDownloading {len(to_download)} files ({format_size(dl_size)})...")
            print("  Press Ctrl+C to stop. Running the same command again "
                  "carries on from here.")
            # Checked before the folder is created: a run stopped for space
            # left an empty BIOS folder behind.
            _check_free_space(bios_path, dl_size, plat_name, 1 if total_downloaded else 2)
            created = not bios_path.exists()
            try:
                bios_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                sys.stdout.flush()
                print(f"\nCannot create {bios_path}.", file=sys.stderr)
                print(f"  {exc.strerror}.", file=sys.stderr)
                print(_write_advice(bios_path, bios_path in assumed_dests, plat_name),
                      file=sys.stderr)
                # The documented code for a folder that cannot be written,
                # as when it exists read-only; 1 once an earlier platform
                # of the run installed something.
                sys.exit(1 if total_downloaded else 2)
            if created:
                print(f"  Created {bios_path}")
            progress = Progress(len(to_download), dl_size, term)
            failed, interrupted = download_files(
                to_download, bios_path, jobs=args.jobs, verbose=args.verbose,
                term=term, progress=progress, copies=copies,
            )
            bulk_lines += hint
            total_downloaded += progress.installed
            files_pending += len(to_download) - progress.installed if interrupted \
                else len(failed)
            total_errors += len(failed)
            failures.extend(failed)
            total_bytes += progress.transferred()
            total_seconds += progress.elapsed()
            if interrupted:
                total_interrupted = True
                break
        else:
            print("\n  All files up to date.")

        total_up_to_date += len(up_to_date)

        # Data trees the pack carries beside the files: Dolphin's Sys,
        # PPSSPP's assets. Fetched from their source archive, each file
        # checked against the hash the list records.
        if data_dirs and not total_interrupted:
            print("\nData directories:")
            for entry in data_dirs:
                counts = install_data_directory(entry, bios_path)
                total_downloaded += counts["installed"]
                total_up_to_date += counts["present"]
                line = f"  {entry['ref']}: {counts['installed']} installed, " \
                       f"{counts['present']} already up to date"
                if counts["changed"] or counts["absent"]:
                    line += (f", {counts['changed'] + counts['absent']} no longer in "
                             "the source (the pack has them)")
                print(line)
                if counts["error"]:
                    print(f"  {entry['ref']}: could not read "
                          f"{LOCAL.pack.name if LOCAL.pack is not None else entry['url']}: "
                          f"{counts['error']}", file=sys.stderr)
                    failures.append((entry["ref"], counts["error"]))
                    total_errors += 1
                for rel, reason in counts["not_kept"]:
                    where = f"{entry['dest']}/{rel}" if entry.get("dest") else rel
                    failures.append((where, f"{NOT_KEPT} ({reason})"))
                    total_errors += 1

        # Standalone copies
        if (
            manifest.get("standalone_copies")
            and not args.check
            and args.standalone_copies
        ):
            print("\nStandalone emulators:")
            lb_root = launchbox_root(os_type)
            extra_dirs = launchbox_bios_dirs(lb_root) if lb_root else None
            copied, skipped, copy_failed = do_standalone_copies(
                manifest, bios_path, os_type, extra_dirs, failures
            )
            if copied or skipped or copy_failed:
                print(
                    f"  {copied} copied, {skipped} skipped (dir not found), "
                    f"{copy_failed} failed"
                )
            total_errors += copy_failed
        elif manifest.get("standalone_copies") and not args.check:
            # Said only where it concerns this machine: "skipped" printed on
            # every run read as an incident, and named no emulator.
            lb_root = launchbox_root(os_type)
            folders = standalone_folders_here(
                manifest, os_type, launchbox_bios_dirs(lb_root) if lb_root else None
            )
            if folders:
                print("\nStandalone emulator folders found here:")
                for folder in folders:
                    print(f"  {folder}")
                print("  --standalone-copies also copies the BIOS files and keys "
                      "they read into them.")

    sys.stdout = real_stdout
    if not listing:
        EVENTS.emit(
            "summary", mode=_event_mode(args), installed=total_downloaded,
            up_to_date=total_verified if args.check or args.dry_run else total_up_to_date,
            pending=check_pending if args.check else files_pending,
            failed=[{"dest": dest, "reason": str(reason)} for dest, reason in failures],
            skipped=_skipped(omitted_seen), left_out=_left_out_events(left_out_seen),
            kept=KEPT.summary(), interrupted=total_interrupted,
        )
    # A check scripted as `--check && launch` reads the exit code only.
    if args.check and check_pending:
        sys.exit(1)
    if not args.check and not listing and not args.dry_run:
        _report_outcome(
            installed_paths,
            total_downloaded,
            total_up_to_date,
            total_errors,
            total_omitted,
            total_interrupted,
            failures=failures,
            transferred=total_bytes,
            seconds=total_seconds,
            declined=total_declined,
            assumed=assumed_dests,
            omitted_entries=omitted_seen,
            platform_of={path: plat for plat, path in platforms},
            hints=bulk_lines,
            sources=_run_sources(bios_indexes, total_downloaded, total_errors),
        )


def _run_sources(bios_indexes: list[FolderIndex], installed: int, failed: int) -> str:
    """The end line saying which folder gave how many of the files."""
    folders = [(index.label, index.copied) for index in LOCAL.folders]
    fixed = sum(index.fixed for index in bios_indexes)
    shared = sum(index.copied for index in bios_indexes)
    fetched = installed - fixed - shared - sum(count for _label, count in folders)
    return sources_line(folders, fixed, max(fetched, 0), failed,
                        "copied from the pack" if LOCAL.pack is not None else "downloaded",
                        shared)


# Full-screen interface
#
# A bare run at a terminal opens it, and --tui asks for it. It decides and the
# installer acts: every check and install is this file run again as a child,
# with --events, the contract the handheld port reads, and the choices made on
# screen as its options. The line questions stay behind --plain, for screen
# readers and for any terminal this cannot drive. Stdlib only: curses is
# missing from Windows CPython and from some Buildroot images.

TUI_MIN_COLS = 40
TUI_MIN_ROWS = 12
# Text stops growing past this width: on a 200-column window a row and its
# size sat a whole screen apart.
TUI_MAX_WIDTH = 100
# An arrow key arrives as Esc and two more characters, Esc alone as itself:
# past this wait an Esc with nothing after it is the key.
TUI_ESC_WAIT = 0.04
TUI_FRAME = 0.1
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
# Turned on by the interface, so a pasted path arrives as one piece and its
# newline does not submit it half typed.
_SCREEN_ON = "\x1b[?1049h\x1b[?25l\x1b[?2004h\x1b[2J"
_SCREEN_OFF = "\x1b[?2004l\x1b[0m\x1b[?25h\x1b[?1049l"
_STYLES = {"title": "\x1b[1m", "focus": "\x1b[7m", "dim": "\x1b[2m"}


class TuiKey(NamedTuple):
    """One key press: a name ("up", "enter"), or "char" or "paste" and the text."""

    name: str
    text: str = ""


_CSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
             "Z": "shift-tab"}
_TILDE_KEYS = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn",
               "3": "delete", "11": "f1"}
_SS3_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
             "P": "f1", "M": "enter"}
# msvcrt.getwch gives these after a \x00 or \xe0 prefix.
_WINDOWS_KEYS = {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pgup",
                 "Q": "pgdn", "G": "home", "O": "end", "S": "delete", ";": "f1"}


def _plain_key(char: str) -> TuiKey | None:
    """The key one character stands for, None for a control nobody bound."""
    named = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace",
             "\t": "tab", "\x1b": "esc", "\x03": "ctrl-c", "\x04": "ctrl-d",
             "\x15": "ctrl-u"}.get(char)
    if named:
        return TuiKey(named)
    if char < " " or unicodedata.category(char) in ("Cc", "Cs"):
        return None
    return TuiKey("char", char)


def _csi_key(params: str, final: str) -> TuiKey | None:
    if final == "~":
        if params == "200":
            return TuiKey("paste-start")
        name = _TILDE_KEYS.get(params.split(";")[0])
        return TuiKey(name) if name else None
    name = _CSI_KEYS.get(final)
    if name is None:
        return None
    if params in ("", "1"):
        return TuiKey(name)
    if params == "1;2" and name in ("up", "down"):
        return TuiKey(f"shift-{name}")
    return None


def _escape(buffer: str) -> tuple[int, TuiKey | None]:
    """The sequence at the start of buffer: the characters it takes and its
    key (None for one nobody bound), or 0 while more are needed to tell."""
    if len(buffer) < 2:
        return 0, None
    if buffer[1] == "[":
        if buffer[2:3] == "M":
            # An X10 mouse report: three bytes of position follow.
            return (6, None) if len(buffer) >= 6 else (0, None)
        for end in range(2, len(buffer)):
            code = ord(buffer[end])
            if 0x40 <= code <= 0x7E:
                return end + 1, _csi_key(buffer[2:end], buffer[end])
            if not 0x20 <= code <= 0x3F:
                return 1, TuiKey("esc")
        return 0, None
    if buffer[1] == "O":
        if len(buffer) < 3:
            return 0, None
        name = _SS3_KEYS.get(buffer[2])
        return 3, TuiKey(name) if name else None
    return 1, TuiKey("esc")


class KeyDecoder:
    """Keys from what a terminal sends, however the reads split it.

    Unknown sequences (F13, mouse reports) are dropped, never taken as text.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.paste: str | None = None

    def feed(self, text: str) -> list[TuiKey]:
        self.buffer += text
        return self._drain(final=False)

    def flush(self) -> list[TuiKey]:
        """What is left once the wait for the rest of a sequence is over.

        An open paste stays open: over SSH its end arrives later than 40 ms.
        """
        return self._drain(final=True)

    def close_paste(self) -> list[TuiKey]:
        """The paste so far, its end never seen, without its newlines."""
        text = "".join(char for char in self.paste or "" if char >= " " and char != "\x7f")
        self.paste = None
        return [TuiKey("paste", text)] if text else []

    @property
    def waiting(self) -> bool:
        return bool(self.buffer) or self.paste is not None

    def _drain(self, final: bool) -> list[TuiKey]:
        keys: list[TuiKey] = []
        while self.buffer:
            if self.paste is not None:
                end = self.buffer.find(_PASTE_END)
                if end < 0:
                    # The end marker may be cut between two reads.
                    keep = next((n for n in range(len(_PASTE_END) - 1, 0, -1)
                                 if self.buffer.endswith(_PASTE_END[:n])), 0)
                    cut = len(self.buffer) - keep
                    self.paste += self.buffer[:cut]
                    self.buffer = self.buffer[cut:]
                    break
                self.paste += self.buffer[:end]
                self.buffer = self.buffer[end + len(_PASTE_END):]
                keys += self.close_paste()
                continue
            if self.buffer[0] == "\x1b":
                taken, key = _escape(self.buffer)
                if not taken:
                    if final:
                        if self.buffer == "\x1b":
                            keys.append(TuiKey("esc"))
                        self.buffer = ""
                    break
                self.buffer = self.buffer[taken:]
                if key is not None and key.name == "paste-start":
                    self.paste = ""
                elif key is not None:
                    keys.append(key)
                continue
            key = _plain_key(self.buffer[0])
            self.buffer = self.buffer[1:]
            if key is not None:
                keys.append(key)
        return keys


def decode_windows_key(first: str, read_next: Callable[[], str]) -> TuiKey | None:
    """A key from msvcrt.getwch: arrows and pages come as a prefix, then a code."""
    if first in ("\x00", "\xe0"):
        name = _WINDOWS_KEYS.get(read_next())
        return TuiKey(name) if name else None
    return _plain_key(first)


def _char_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def cell_width(text: str) -> int:
    """Terminal cells the text takes: a CJK character two, an accent none."""
    return sum(_char_width(char) for char in text)


# C0 and C1 controls, DEL and lone surrogates: a file, folder or core name
# holding ESC would drive the terminal (clear it, or set the clipboard with
# OSC 52) instead of being shown.
_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def screen_text(text: str) -> str:
    """Text as a terminal shows it, every control character a "?"."""
    return _CONTROLS.sub("?", text)


def ascii_text(text: str) -> str:
    """The text in ASCII, accents dropped (Edition) and any other character
    as one ? per cell it took, so the columns stay where they were."""
    if text.isascii():
        return text
    plain = "".join(char for char in unicodedata.normalize("NFKD", text)
                    if not unicodedata.combining(char))
    return "".join(char if char.isascii() else "?" * _char_width(char) for char in plain)


def fit(text: str, width: int) -> str:
    """The text cut to width cells, never through a character."""
    text = screen_text(text)
    kept: list[str] = []
    used = 0
    for char in text:
        size = _char_width(char)
        if used + size > width:
            break
        kept.append(char)
        used += size
    return "".join(kept)


def middle(text: str, width: int) -> str:
    """A long path cut in its middle, its start and its last folders kept."""
    if cell_width(text) <= width:
        return text
    if width <= 3:
        return fit(text, width)
    room = width - 3
    tail = fit(text[::-1], room // 2)[::-1]
    return fit(text, room - cell_width(tail)) + "..." + tail


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_width(text))


def _columns(left: str, right: str, width: int) -> str:
    """left, and right against the edge when both fit, else left alone."""
    room = width - cell_width(right) - 2
    if not right or room < 8:
        return fit(left, width)
    return _pad(fit(left, room), room) + "  " + right


def _split_word(word: str, room: int) -> list[str]:
    """A word longer than room, cut after a slash where it can be: a path
    read /home/us and er/bios when cut anywhere."""
    pieces = []
    while cell_width(word) > room:
        head = fit(word, max(room, 1))
        cut = head.rfind("/") + 1
        if cut <= len(head) // 3:
            cut = len(head)
        pieces.append(word[:cut])
        word = word[cut:]
    return pieces + [word] if word else pieces


def _wrap(text: str, width: int, indent: str = "  ") -> list[str]:
    """Lines of at most width cells, the ones after the first indented."""
    if cell_width(text) <= width:
        return [text]
    lead = text[:len(text) - len(text.lstrip(" "))]
    lines: list[str] = []
    line = ""
    for word in text.split():
        for piece in _split_word(word, width - len(lead + indent)):
            prefix = lead + indent if lines else lead
            longer = f"{line} {piece}" if line else piece
            if not line or cell_width(prefix + longer) <= width:
                line = longer
            else:
                lines.append(prefix + line)
                line = piece
    lines.append((lead + indent if lines else lead) + line)
    return lines


class TuiLine(str):
    """A screen line and how it is drawn: "title", "focus", "dim" or plain."""

    style = ""


def _styled(text: str, style: str = "") -> TuiLine:
    line = TuiLine(screen_text(text))
    line.style = style
    return line


def _short_path(path: Path | None, home: Path) -> str:
    if path is None:
        return ""
    text = str(path)
    home_text = str(home)
    if home_text not in ("", "/", ".") and (text == home_text
                                           or text.startswith(home_text + os.sep)):
        return "~" + text[len(home_text):]
    return text


def _folded(text: str) -> str:
    """Text compared without case or accents, as a filter reads it."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


class TuiState:
    """Everything the screens show. Nothing here touches the terminal."""

    def __init__(self, os_type: str = "linux", home: Path | None = None) -> None:
        self.os_type = os_type
        self.home = home or Path("/home/user")
        self.stack: list = ["setup"]
        self.focus: dict = {}
        self.filters: dict = {}
        self.editing: str = ""
        self.help: bool = False
        self.status: str = ""
        self.cols: int = 80
        self.rows: int = 24
        self.now: float = 0.0
        # (platform, folder, label) for each setup found here.
        self.setups: list = []
        self.notes: list = []
        self.platform: str = ""
        self.dest: Path | None = None
        # A --dest given without --platform: the list asks whose files go there.
        self.dest_given: Path | None = None
        self.folder_default: Path | None = None
        self.path_text: str = ""
        self.browse_dir: Path | None = None
        self.browse_rows: list = []
        self.manifest: dict | None = None
        self.manifest_for: str = ""
        self.manifest_path: Path | None = None
        self.manifest_given: bool = False
        self.error: list = []
        self.error_title: str = ""
        self.error_code: int = 1
        # Typed over in one key: a field opening on a long path took one
        # Backspace per character before anything could be typed.
        self.path_selected: bool = False
        # The ticks kept across a check run again for new options.
        self.keep_ticked: set | None = None
        # (files read, files to read) while the check hashes what is in place.
        self.reading: tuple | None = None
        # Folders to copy BIOS files from (--from), and what the check found.
        self.from_folders: list = []
        self.found: list = []
        # What the browser is choosing: the BIOS folder, or one to copy from.
        self.browse_for: str = "dest"
        # Files an install copies from this machine, whose bytes are no download.
        self.local_dests: set = set()
        self.local_done: int = 0
        # The child run in progress: check, confirm, preview or install.
        self.run: str = ""
        self.running: bool = False
        self.run_id: int = 0
        self.started: float = 0.0
        self.checked_flags: list = []
        self.check: dict | None = None
        self.files: list = []
        self.systems: list = []
        self.facts: dict = {}
        self.dry: dict | None = None
        self.dry_files: list = []
        self.dry_summary: dict | None = None
        # Choices, each one an option of the command line.
        self.ticked: set = set()
        self.cli_systems: list = []
        self.regions: list = []
        self.cli_regions: list = []
        self.cores: list = []
        self.no_game_data: bool = False
        self.installed_only: bool = False
        self.jobs: int = 8
        self.passthrough: list = []
        # The install.
        self.download: dict | None = None
        self.progress: dict | None = None
        # (time, bytes done) of the last progress events: a partial copy
        # resumed counts in the first one and never in a speed.
        self.samples: list = []
        self.finished: list = []
        self.summary: dict | None = None
        self.code: int | None = None
        self.stopping: bool = False
        self.tail: list = []
        self.log: str = ""
        self.exit_code: int | None = None

    @property
    def screen(self) -> str:
        return self.stack[-1]


def _env_on(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "") not in ("", "0")


def plain_requested(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> bool:
    """Whether the line questions were asked for: --plain, RETROBIOS_PLAIN, ACCESSIBLE."""
    env = os.environ if env is None else env
    return bool(args.plain) or _env_on(env, "RETROBIOS_PLAIN") or _env_on(env, "ACCESSIBLE")


def _terminal_size(stream: TextIO) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(stream.fileno())
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        return 0, 0
    return size.columns, size.lines


def tui_blocker(
    args: argparse.Namespace, stdin: TextIO | None = None, stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Why the full-screen interface cannot run here, "" when it can."""
    env = os.environ if env is None else env
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for flag, given in (
        ("--check", args.check), ("--dry-run", args.dry_run),
        ("--list-platforms", args.list_platforms), ("--list-targets", args.list_targets),
        ("--list-systems", args.list_systems), ("--list-cores", args.list_cores),
        ("--list-emulators", args.list_emulators), ("--events", args.events),
        ("--quiet", args.quiet), ("--no-input", args.no_input),
        ("--restore", args.restore is not None), ("--yes", args.yes),
    ):
        if given:
            return f"{flag} runs without questions"
    for name, stream in (("input", stdin), ("output", stdout)):
        isatty = getattr(stream, "isatty", None)
        if not (isatty and isatty()):
            return f"the {name} is not a terminal"
    if os.name != "nt" and env.get("TERM", "") in ("", "dumb"):
        return "TERM is dumb or not set"
    # A pseudo-terminal nobody sized reports 0x0: script(1) fed from a pipe
    # is one, and the line questions run there as they always have.
    cols, rows = _terminal_size(stdout)
    if cols < TUI_MIN_COLS or rows < TUI_MIN_ROWS:
        return (f"the terminal is {cols}x{rows}, smaller than "
                f"{TUI_MIN_COLS}x{TUI_MIN_ROWS}")
    if os.name == "nt":
        import importlib  # noqa: PLC0415

        try:
            importlib.import_module("msvcrt")
        except ImportError:
            return "this Python has no msvcrt to read keys"
        if not _enable_ansi():
            return "this console does not draw escape sequences (Windows 10 or later does)"
        return ""
    try:
        import termios  # noqa: PLC0415
    except ImportError:
        return "this Python has no termios to read keys"
    try:
        termios.tcgetattr(stdin.fileno())
    except (termios.error, OSError, ValueError, AttributeError,
            io.UnsupportedOperation) as exc:
        return f"single key presses cannot be read here ({exc})"
    return ""


def tui_flags(state: TuiState) -> list[tuple[str, str | None]]:
    """The options that carry the choices made on screen, CLI ones included."""
    flags = list(state.passthrough)
    needed = {row["system"] for row in state.systems
              if row["missing"] + row["wrong_hash"] > 0}
    if state.systems and not needed <= state.ticked:
        flags += [("--system", name) for name in sorted(state.ticked)]
    else:
        flags += [("--system", name) for name in state.cli_systems]
    flags += [("--core", name) for name in state.cores]
    if state.regions:
        flags.append(("--region", ",".join(state.regions)))
    if state.no_game_data:
        flags.append(("--no-game-data", None))
    if state.installed_only and not any(flag == "--cores-dir" for flag, _v in flags):
        flags.append(("--installed-cores", None))
    if state.jobs != 8:
        flags.append(("--jobs", str(state.jobs)))
    flags += [("--from", str(folder)) for folder in state.from_folders]
    return flags


_TUI_MODES = {
    "check": ["--check", "--events"],
    "confirm": ["--dry-run", "--events"],
    "preview": ["--dry-run", "--events"],
    "install": ["--events", "--yes"],
}


def tui_argv(state: TuiState, run: str, python: str, script: str) -> list[str]:
    """The child that does what the screen decided."""
    # --dest=VALUE: a folder typed as -x would otherwise be read as an option.
    argv = [python, script, f"--platform={state.platform}", f"--dest={state.dest}"]
    if state.manifest_path is not None:
        argv.append(f"--manifest={state.manifest_path}")
    argv += [flag if value is None else f"{flag}={value}" for flag, value in tui_flags(state)]
    return argv + _TUI_MODES[run]


def tui_command(state: TuiState) -> str:
    """The same choice as one command, in the form the run was started with."""
    hint = Invocation()
    hint.os_type = state.os_type
    hint.options = ([("--manifest", str(state.manifest_path))]
                    if state.manifest_given and state.manifest_path else [])
    hint.options += tui_flags(state)
    return hint.command(state.platform, state.dest)

class TuiRow(NamedTuple):
    """One row a screen lists: what Enter does on it, and what it shows."""

    action: str
    label: str
    right: str = ""
    value: object = None
    style: str = ""


# Screens that scroll text instead of listing rows.
_PAGERS = ("preview", "command", "result", "misplaced")
_CHECKLISTS = ("systems", "cores")


def _needed(row: dict) -> int:
    return row["missing"] + row["wrong_hash"]


def _tui_system_label(row: dict, titles: dict | None = None) -> str:
    """The name a person reads: the file list's title when it has one, the
    platform's own name when it is one (Sony - PlayStation), the id with the
    platform's short name otherwise (psx)."""
    title = (titles or {}).get(row["system"])
    if isinstance(title, str) and title.strip():
        return title.strip()
    name = row.get("name") or row["system"]
    if name == row["system"]:
        return name
    if " " in name or any(char.isupper() for char in name):
        return name
    return f"{row['system']} ({name})"


def _matches(text: str, needle: str) -> bool:
    return not needle or _folded(needle) in _folded(text)


def _filter_row(state: TuiState) -> TuiRow:
    text = state.filters.get(state.screen, "")
    if state.editing == state.screen:
        return TuiRow("filter", f"Filter: {text}_")
    if text:
        return TuiRow("filter", f"Filter: {text}")
    return TuiRow("filter", "Filter (/ to type)", style="dim")


def _system_titles(state: TuiState) -> dict:
    titles = (state.manifest or {}).get("system_titles")
    return titles if isinstance(titles, dict) else {}


def _sorted_systems(state: TuiState) -> list[dict]:
    """Systems with something to download first, the ones in place last, each
    group in the order of the names a person reads."""
    titles = _system_titles(state)
    return sorted(state.systems, key=lambda row: (
        _needed(row) == 0, _tui_system_label(row, titles).casefold()))


# Region names as a person writes them, where title case alone is wrong.
_REGION_TITLES = {"asia-ntsc": "Asia (NTSC)", "asia-pal": "Asia (PAL)",
                  "uk": "United Kingdom", WORLD_REGION: "World"}


def region_title(name: str) -> str:
    """north-america as North America, asia-ntsc as Asia (NTSC)."""
    return _REGION_TITLES.get(name) or " ".join(
        word.capitalize() for word in name.split("-"))


def tui_choices(state: TuiState) -> dict[str, list[str]]:
    """What the file list lets one narrow to, read from the list itself."""
    manifest = state.manifest or {}
    return available_choices(manifest.get("files", []), manifest.get("omitted_files", []))


def tui_rows(state: TuiState) -> list[TuiRow]:
    """The rows of the current screen, in the order they are shown."""
    screen = state.screen
    if screen == "setup":
        # The focused one's folder is spelled out below the list.
        rows = [TuiRow("setup", label, value=index)
                for index, (_plat, _path, label) in enumerate(state.setups)]
        return rows + [TuiRow("another", "Another setup or folder")]
    if screen == "platforms":
        needle = state.filters.get(screen, "")
        systems = _systems_reading()
        return [_filter_row(state)] + [
            TuiRow("platform", display_name(plat), ", ".join(systems.get(plat, [])), plat)
            for plat in AVAILABLE_PLATFORMS
            if _matches(" ".join([plat, display_name(plat), *systems.get(plat, [])]), needle)
        ]
    if screen == "folder":
        rows = [TuiRow("default", "Use this folder")] if state.folder_default else []
        return rows + [TuiRow("browse", "Browse for a folder"), TuiRow("type", "Type a path")]
    if screen == "browse":
        return list(state.browse_rows)
    if screen == "create":
        label = "Create the whole path" if state.facts.get("risky") else "Create it and continue"
        return [TuiRow("create", label), TuiRow("other", "Choose another folder")]
    if screen == "cardroot":
        return [TuiRow("cardroot", "Use the card root"), TuiRow("other", "Choose another folder")]
    if screen == "plan":
        return _plan_rows(state)
    if screen == "systems":
        return _system_rows(state)
    if screen == "options":
        return _option_rows(state)
    if screen == "regions":
        ranked = [TuiRow("region", f"[{rank}] {region_title(name)}", value=name)
                  for rank, name in enumerate(state.regions, 1)]
        known = sorted(set(tui_choices(state)["regions"]) | set(state.cli_regions),
                       key=region_title)
        return ranked + [TuiRow("region", f"[ ] {region_title(name)}", value=name)
                         for name in known if name not in state.regions]
    if screen == "cores":
        needle = state.filters.get(screen, "")
        return [_filter_row(state)] + [
            TuiRow("core", f"[{'x' if name in state.cores else ' '}] {name}", value=name)
            for name in tui_choices(state)["cores"] if _matches(name, needle)
        ]
    if screen == "confirm":
        if state.running or state.dry is None:
            return []
        return [TuiRow("install", "Install"), TuiRow("back", "Back")]
    if screen == "error":
        rows = ([TuiRow("retry", "Try again")]
                if state.run == "check" and state.error_title else [])
        return rows + [TuiRow("back", "Choose another setup or folder" if state.run == "check"
                              else "Back"),
                       TuiRow("quit", "Quit")]
    if screen == "quit":
        return [TuiRow("quit", "Quit"), TuiRow("stay", "Stay")]
    return []


def _plan_rows(state: TuiState) -> list[TuiRow]:
    check = state.check or {}
    if not (_needed(check) if check else 0) and not check.get("data_missing"):
        return [TuiRow("close", "Close"), TuiRow("options", "Options")]
    size = check.get("bytes_needed", 0)
    label = f"Install {format_size(size)}" if size >= CONFIRM_FROM_BYTES else "Install"
    rows = [TuiRow("install", label)]
    if state.facts.get("writable") is False:
        # Every file would fail alike: the way out is another folder.
        rows = [TuiRow("folder", "Choose another folder"), TuiRow("install", "Install anyway"),
                *rows[1:]]
    if records_selection((state.manifest or {}).get("files", [])) and len(state.systems) > 1:
        rows.append(TuiRow("systems", "Choose systems"))
    rows.append(TuiRow("fromcard", "Copy the BIOS files from your old card or a folder"))
    if _tui_misplaced(state):
        rows.append(TuiRow("misplaced", "Show the files in the wrong place"))
    return rows + [TuiRow("options", "Options"), TuiRow("quit", "Quit")]


def _system_rows(state: TuiState) -> list[TuiRow]:
    needle = state.filters.get("systems", "")
    titles = _system_titles(state)
    rows = [TuiRow("all", "All"), TuiRow("none", "None"), _filter_row(state)]
    for row in _sorted_systems(state):
        label = _tui_system_label(row, titles)
        if not _matches(f"{label} {row['system']}", needle):
            continue
        box = "[x]" if row["system"] in state.ticked else "[ ]"
        need = _needed(row)
        right = (f"{_plural(need, 'file')}, {format_size(row['bytes_needed'])}"
                 if need else "in place")
        rows.append(TuiRow("system", f"{box} {label}", right, row["system"],
                           "" if need else "dim"))
    return rows


def _cores_dir_given(state: TuiState) -> bool:
    return any(flag == "--cores-dir" for flag, _value in state.passthrough)


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _option_rows(state: TuiState) -> list[TuiRow]:
    manifest = state.manifest or {}
    choices = tui_choices(state)
    rows = []
    if choices["regions"]:
        rows.append(TuiRow("regions", "Preferred regions: "
                           + (", ".join(map(region_title, state.regions)) or "any"),
                           "--region"))
    if manifest.get("categorized"):
        rows.append(TuiRow("game_data", f"Leave out game data: {_yes(state.no_game_data)}",
                           "--no-game-data"))
    found = len(state.facts.get("cores", []))
    if _cores_dir_given(state):
        rows.append(TuiRow("installed", "Installed cores only: yes, implied by --cores-dir",
                           "--cores-dir"))
    elif state.platform in CORE_PLATFORMS and "system_cores" in manifest and found:
        rows.append(TuiRow("installed", f"Installed cores only: "
                           f"{_yes(state.installed_only)} ({found} found)",
                           "--installed-cores"))
    if choices["cores"]:
        chosen = f"{len(state.cores)} chosen" if state.cores else "all"
        rows.append(TuiRow("cores", f"Cores: {chosen}", "--core"))
    copied = ", ".join(str(folder) for folder in state.from_folders) or "none"
    rows += [
        TuiRow("jobs", f"Parallel downloads: {state.jobs}", "--jobs"),
        TuiRow("folder", f"BIOS folder: {_short_path(state.dest, state.home)}", "--dest",
               style="path"),
        TuiRow("from", f"Copy from: {copied}", "--from"),
        TuiRow("preview", "Preview the file list", "--dry-run"),
        TuiRow("command", "Show the command line"),
    ]
    return rows


def _push(state: TuiState, screen: str, focus: int = 0) -> list[tuple]:
    state.stack.append(screen)
    state.focus[screen] = focus
    return []


def _replace(state: TuiState, screen: str) -> None:
    state.stack[-1] = screen
    state.focus[screen] = 0


def _back(state: TuiState) -> list[tuple]:
    effects = [("kill",)] if state.running else []
    if state.running:
        # What the stopped run still reports belongs to no screen.
        state.running = False
        state.run_id += 1
    if len(state.stack) == 1:
        state.stack.append("quit")
        state.focus["quit"] = 0
        return effects
    state.stack.pop()
    if state.screen == "checking" and not state.running:
        # Stay after Esc on a check started from the command line: a
        # Checking screen nothing feeds counted seconds forever.
        return effects + _start_check(state)
    if state.screen == "plan" and _counts_predate_options(state):
        # The plan's numbers were read under the options before: they are
        # read again, the systems ticked kept.
        needed = {row["system"] for row in state.systems if _needed(row)}
        state.keep_ticked = None if needed <= state.ticked else set(state.ticked)
        return effects + _start_check(state)
    return effects


def tui_quit(state: TuiState, code: int = 130) -> list[tuple]:
    """Leave now: with the install's own code once it ran, else 130."""
    if state.screen == "result":
        code = state.code if state.code is not None else 1
    elif state.screen == "error":
        code = state.error_code
    state.exit_code = code
    return [("kill",)] if state.running else []


def tui_interrupt(state: TuiState) -> list[tuple]:
    """Ctrl-C: an install stops and says what it did, anything else ends."""
    if state.run == "install" and state.running:
        if state.stopping:
            return [("kill",)]
        state.stopping = True
        return [("stop",)]
    return tui_quit(state)


def _start_run(state: TuiState, run: str) -> list[tuple]:
    state.run = run
    state.running = True
    state.run_id += 1
    state.started = state.now
    if run == "check":
        # The systems of an earlier check go first: ticks read against them
        # dropped the --system the command line gave.
        state.check, state.files, state.systems, state.reading = None, [], [], None
        state.found = []
        state.checked_flags = tui_flags(state)
    elif run in ("confirm", "preview"):
        state.dry, state.dry_files, state.dry_summary = None, [], None
    else:
        state.download = state.progress = state.reading = None
        state.samples, state.local_dests, state.local_done = [], set(), 0
        state.finished, state.summary, state.code, state.stopping = [], None, None, False
    return [("run", run)]


def _start_check(state: TuiState) -> list[tuple]:
    """Check the chosen folder, after fetching the file list once."""
    if "plan" in state.stack:
        del state.stack[state.stack.index("plan"):]
    while state.stack and state.stack[-1] in ("checking", "error", "create"):
        state.stack.pop()
    state.stack.append("checking")
    state.facts, state.ticked = {}, set()
    if state.manifest is not None and state.manifest_for == state.platform:
        return _start_run(state, "check")
    state.manifest = None
    state.run, state.run_id = "check", state.run_id + 1
    state.started = state.now
    return [("fetch", state.platform)]


def _choose(state: TuiState, plat: str, dest: Path) -> list[tuple]:
    state.platform, state.dest = plat, dest
    if plat in CARD_PLATFORMS and card_platform(dest) is None:
        # The engine refuses a folder inside the card, the list writing
        # Bios/<TAG>/ below the root: the root is offered instead.
        for parent in list(dest.absolute().parents)[:2]:
            holder = card_platform(parent)
            if holder:
                state.facts = {"card_root": parent, "card": holder}
                return _push(state, "cardroot")
    if not dest.exists():
        nearest = _nearest_folder(dest)
        missing = len(dest.absolute().parts) - len(nearest.parts)
        state.facts = {"nearest": nearest, "risky": nearest.parent == nearest or missing > 2}
        # A path whose start was mistyped would be created from the root:
        # the safe answer is the one under A.
        return _push(state, "create", 1 if state.facts["risky"] else 0)
    return _start_check(state)


def _open_folder_screen(state: TuiState, default: Path | None) -> list[tuple]:
    state.folder_default = default
    return _push(state, "folder")


def _mounted_cards() -> list[Path]:
    """Mount points of removable media, where a card or a share appears.

    A handheld's card is most often prepared on a PC with a card reader: on
    Windows every drive but the system one may be that card.
    """
    if os.name == "nt":
        system = os.environ.get("SystemDrive", "C:").upper()
        return [Path(f"{letter}:\\") for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"
                if f"{letter}:" != system and os.path.exists(f"{letter}:\\")]
    found: list[Path] = []
    for root in (Path("/media"), Path("/run/media"), Path("/mnt"), Path("/Volumes")):
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            # WSL's own mounts, and the Windows system drive, hold no card.
            if child.name in ("wsl", "wslg") or (root.name == "mnt" and child.name == "c"):
                continue
            if os.path.ismount(child):
                found.append(child)
            elif root.name in ("media", "run") or root == Path("/run/media"):
                # /media/<user>/<label> and /run/media/<user>/<label>.
                try:
                    found += [g for g in sorted(child.iterdir()) if os.path.ismount(g)]
                except OSError:
                    continue
    return found


def _browse_rows(folder: Path | None, zips: bool = False) -> tuple[list[TuiRow], str]:
    """A directory's rows for the browser, cards first, and what went wrong.
    With zips, the ZIP files too: a backup is often one archive."""
    if folder is None:
        drives = [Path(f"{letter}:\\") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  if os.path.exists(f"{letter}:\\")]
        return [TuiRow("dir", str(drive), value=drive) for drive in drives], ""
    rows = [TuiRow("use", "Use this folder"), TuiRow("up", "..", value=folder.parent)]
    for card in _mounted_cards():
        if card != folder:
            plat = card_platform(card)
            what = f"{display_name(plat)} card" if plat else "card or drive"
            rows.append(TuiRow("dir", f"{card} ({what})", value=card))
    try:
        entries = list(os.scandir(folder))
    except OSError as exc:
        return rows, f"Cannot open this folder: {exc.strerror or exc}"
    entries.sort(key=lambda entry: (entry.name.startswith("."), entry.name.casefold()))
    rows += [TuiRow("dir", f"{entry.name}/", value=Path(entry.path))
             for entry in entries if entry.is_dir()]
    if zips:
        rows += [TuiRow("zip", entry.name, value=Path(entry.path)) for entry in entries
                 if entry.is_file() and entry.name.lower().endswith(".zip")]
    return rows, ""


def _browse_to(state: TuiState, folder: Path | None, came_from: Path | None = None) -> None:
    state.browse_dir = folder
    state.browse_rows, state.status = _browse_rows(folder, state.browse_for == "from")
    focus = 0
    for index, row in enumerate(state.browse_rows):
        if came_from is not None and row.action == "dir" and row.value == came_from:
            focus = index
    state.focus["browse"] = focus


def _nearest_folder(path: Path) -> Path:
    probe = path.absolute()
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    return probe


def typed_path(text: str) -> Path | None:
    """A folder typed or pasted: quotes off, ~ and variables expanded."""
    # Explorer's "Copy as path" quotes both ends, a half-selected copy one.
    text = text.strip().strip("\"'").strip()
    if not text:
        return None
    return Path(os.path.expanduser(os.path.expandvars(text)))


def _tui_folder_facts(state: TuiState) -> dict:
    """Free space, whether the folder takes files, and the cores found."""
    probe = _nearest_folder(state.dest)
    try:
        free: int | None = shutil.disk_usage(probe).free
    except OSError:
        free = None
    cores = (installed_cores(core_dirs(state.platform, state.dest))
             if state.platform in CORE_PLATFORMS else [])
    return {"free": free, "writable": _folder_writable(state.dest), "cores": cores}


def _activate(state: TuiState, row: TuiRow) -> list[tuple]:
    screen, action = state.screen, row.action
    if action == "filter":
        state.editing = screen
        return []
    if screen == "setup":
        if action == "another":
            return _push(state, "platforms", _platform_focus(state, "retroarch"))
        plat, path, _label = state.setups[row.value]
        return _choose(state, plat, path)
    if screen == "platforms":
        if state.dest_given is not None:
            return _choose(state, row.value, state.dest_given)
        state.platform = row.value
        default = None if row.value in CARD_PLATFORMS else _default_dest(state.os_type, row.value)
        return _open_folder_screen(state, default)
    if screen == "folder":
        if action == "default":
            return _choose(state, state.platform, state.folder_default)
        if action == "browse":
            state.browse_for = "dest"
            _push(state, "browse")
            _browse_to(state, _nearest_folder(state.folder_default or state.home))
            return []
        state.path_text = str(state.folder_default or "")
        state.path_selected = bool(state.path_text)
        return _push(state, "path")
    if screen == "browse":
        if action in ("use", "zip") and state.browse_for == "from":
            chosen = state.browse_dir if action == "use" else row.value
            if chosen not in state.from_folders:
                state.from_folders.append(chosen)
            state.browse_for = "dest"
            # The check runs again with it: the plan then says what it holds.
            return _start_check(state)
        if action == "use":
            return _choose(state, state.platform, state.browse_dir)
        if action == "up" and state.browse_dir is not None:
            if row.value == state.browse_dir:
                _browse_to(state, None if os.name == "nt" else state.browse_dir)
            else:
                _browse_to(state, row.value, came_from=state.browse_dir)
            return []
        _browse_to(state, row.value)
        return []
    if screen == "create":
        if action == "create":
            state.stack.pop()
            return _start_check(state)
        return _back(state)
    if screen == "cardroot":
        if action == "cardroot":
            state.stack.pop()
            return _choose(state, state.platform, state.facts["card_root"])
        return _back(state)
    if screen == "plan":
        if action == "install":
            return _begin_install(state)
        if action == "close":
            state.exit_code = 0
            return []
        if action == "quit":
            return tui_quit(state)
        if action == "folder":
            return _open_folder_screen(state, state.dest)
        if action == "fromcard":
            return _open_from_browser(state)
        if action == "misplaced":
            return _push(state, "misplaced")
        # On the first system, so A continues and the D-pad ticks.
        return _push(state, action, 3 if action == "systems" else 0)
    if screen == "systems":
        if action == "all":
            state.ticked = {row["system"] for row in state.systems}
        elif action == "none":
            state.ticked = set()
        else:
            return _begin_install(state)
        return []
    if screen == "options":
        return _option(state, action, 0)
    if screen == "regions":
        _toggle_region(state, row.value)
        return []
    if screen == "cores":
        return _back(state)
    if screen == "confirm":
        if action == "install":
            state.stack.pop()
            return _install_now(state)
        return _back(state)
    if screen == "error":
        if action == "retry":
            return _start_check(state)
        if action == "quit":
            return tui_quit(state, state.error_code)
        return _back(state)
    if screen == "quit":
        if action == "quit":
            return tui_quit(state)
        return _back(state)
    return []


def _systems_reading() -> dict[str, list[str]]:
    """The device systems that get each platform's files, as detection knows
    them: someone on muOS or ArkOS looks for that name, not RetroArch."""
    names: dict[str, list[str]] = {}
    for rule in _EMBEDDED_RULES:
        if (rule.manifest and rule.name != display_name(rule.manifest)
                and rule.name not in names.setdefault(rule.manifest, [])):
            names[rule.manifest].append(rule.name)
    return names


def _platform_focus(state: TuiState, plat: str) -> int:
    """The platform list's row for plat, below its filter row."""
    return 1 + AVAILABLE_PLATFORMS.index(plat) if plat in AVAILABLE_PLATFORMS else 1


def _option(state: TuiState, action: str, step: int) -> list[tuple]:
    """A row of the options: step 0 is Enter, -1 and 1 are Left and Right."""
    if action == "game_data":
        state.no_game_data = step > 0 if step else not state.no_game_data
    elif action == "installed" and not _cores_dir_given(state):
        state.installed_only = step > 0 if step else not state.installed_only
    elif action == "jobs":
        state.jobs = max(1, min(32, state.jobs + (step or 1)))
    elif action == "cores":
        # On the first core: the filter row under A opened a field a
        # controller cannot type into.
        return _push(state, action, 1 if tui_choices(state)["cores"] else 0)
    elif action in ("regions", "command"):
        return _push(state, action)
    elif action == "folder":
        return _open_folder_screen(state, state.dest)
    elif action == "from" and step < 0:
        if state.from_folders:
            state.from_folders.pop()
            return _start_check(state)
    elif action == "from":
        return _open_from_browser(state)
    elif action == "preview":
        _push(state, "preview")
        return _start_run(state, "preview")
    return []


def _open_from_browser(state: TuiState) -> list[tuple]:
    """The browser, to name an old card, a backup or a ZIP to copy from."""
    state.browse_for = "from"
    _push(state, "browse")
    start = state.from_folders[-1].parent if state.from_folders else state.home
    _browse_to(state, _nearest_folder(start))
    return []


def _toggle_region(state: TuiState, name: str) -> None:
    if name in state.regions:
        state.regions.remove(name)
    else:
        state.regions.append(name)
    rows = tui_rows(state)
    state.focus["regions"] = next((i for i, row in enumerate(rows) if row.value == name), 0)


def _move_region(state: TuiState, name: str, step: int) -> None:
    if name not in state.regions:
        return
    index = state.regions.index(name)
    target = max(0, min(len(state.regions) - 1, index + step))
    state.regions.insert(target, state.regions.pop(index))
    state.focus["regions"] = target


def _begin_install(state: TuiState) -> list[tuple]:
    if state.systems and not state.ticked:
        state.status = "No system is ticked: tick one, or choose All."
        return []
    if tui_flags(state) != state.checked_flags:
        _push(state, "confirm")
        return _start_run(state, "confirm")
    return _install_now(state)


def _install_now(state: TuiState) -> list[tuple]:
    _push(state, "installing")
    return _start_run(state, "install")


def _same_plan(one: dict | None, other: dict | None) -> bool:
    keys = ("missing", "wrong_hash", "bytes_needed", "data_missing")
    return bool(one and other) and all(one[k] == other[k] for k in keys)


def _fail(state: TuiState, lines: list[str], code: int) -> list[tuple]:
    state.error_title = ""
    state.error = [line for line in lines if line.strip()] or [
        f"The installer stopped with code {code} and said nothing."]
    state.error_code = code or 1
    state.running = False
    _replace(state, "error")
    return []


def _tick(state: TuiState, row: TuiRow, on: bool | None) -> None:
    if state.screen == "systems" and row.action == "system":
        chosen = state.ticked
    elif state.screen == "cores" and row.action == "core":
        chosen = set(state.cores)
    else:
        return
    value = row.value
    want = (value not in chosen) if on is None else on
    if state.screen == "systems":
        if want:
            state.ticked.add(value)
        else:
            state.ticked.discard(value)
    elif want and value not in state.cores:
        state.cores.append(value)
    elif not want and value in state.cores:
        state.cores.remove(value)


def _edit_text(text: str, key: TuiKey, selected: bool = False) -> str | None:
    """The field after a key, None when the key is not an edit. A selected
    text is replaced by what is typed or pasted, and cleared by Backspace."""
    if key.name in ("char", "paste"):
        return key.text if selected else text + key.text
    if key.name in ("backspace", "delete"):
        return "" if selected else text[:-1]
    if key.name == "ctrl-u":
        return ""
    return None


def _filter_key(state: TuiState, key: TuiKey) -> list[tuple]:
    """Typing in a list's filter. It never ticks or unticks anything, and
    focus never lands on All or None: the next A or X there undid a choice."""
    screen = state.screen
    edited = _edit_text(state.filters.get(screen, ""), key)
    rows = tui_rows(state)
    filter_row = next((i for i, row in enumerate(rows) if row.action == "filter"), 0)
    if edited is not None:
        state.filters[screen] = edited
        state.focus[screen] = filter_row
        return []
    if key.name == "esc":
        state.filters[screen] = ""
    state.editing = ""
    rows = tui_rows(state)
    if key.name in ("enter", "down") and len(rows) > filter_row + 1:
        state.focus[screen] = filter_row + 1
    else:
        state.focus[screen] = filter_row
    return []


def _path_key(state: TuiState, key: TuiKey) -> list[tuple]:
    edited = _edit_text(state.path_text, key, state.path_selected)
    if edited is not None:
        state.path_text, state.path_selected = edited, False
        return []
    if key.name == "esc":
        return _back(state)
    if key.name == "enter":
        path = typed_path(state.path_text)
        if path is None:
            state.status = "Type a folder first."
            return []
        state.stack.pop()
        return _choose(state, state.platform, path)
    return []


def _pager_key(state: TuiState, key: TuiKey) -> list[tuple]:
    screen = state.screen
    top = state.focus.get(screen, 0)
    page = max(1, state.rows - 5)
    name, char = key.name, key.text if key.name == "char" else ""
    if name == "up" or char == "k":
        top -= 1
    elif name == "down" or char == "j":
        top += 1
    elif name == "pgup":
        top -= page
    elif name == "pgdn":
        top += page
    elif name == "home" or char == "g":
        top = 0
    elif name == "end" or char == "G":
        top = 1 << 30
    elif name in ("enter", "esc", "backspace") or char == " ":
        if screen == "result":
            return tui_quit(state)
        return _back(state)
    state.focus[screen] = max(0, min(top, _pager_last(state, _pager_items(state))))
    return []


def _pager_last(state: TuiState, items: list) -> int:
    """The first item shown when the end of a pager is in view, found from
    the end: only the last screenful is wrapped."""
    width = min(state.cols, TUI_MAX_WIDTH) - 2
    room = max(1, state.rows - 4)
    used, index = 0, len(items)
    while index > 0:
        size = len(_text_lines([items[index - 1]], width))
        if used + size > room:
            break
        used += size
        index -= 1
    return index


def tui_update(state: TuiState, key: TuiKey) -> list[tuple]:
    """What a key does on the current screen, and the work it starts."""
    if key.name in ("ctrl-c", "ctrl-d"):
        return tui_interrupt(state)
    if state.cols < TUI_MIN_COLS or state.rows < TUI_MIN_ROWS:
        return []
    if state.help:
        state.help = False
        return []
    state.status = ""
    screen = state.screen
    if screen == "path":
        return _path_key(state, key)
    if state.editing:
        return _filter_key(state, key)
    char = key.text if key.name == "char" else ""
    if key.name == "f1" or char == "?":
        state.help = True
        return []
    if screen == "checking":
        if char == "q":
            return tui_quit(state)
        return _back(state) if key.name in ("esc", "backspace") else []
    if screen == "installing":
        # B, Esc and q are the stops a controller or a plain keyboard has.
        if key.name in ("esc", "backspace") or char == "q":
            return tui_interrupt(state)
        return []
    if char == "q":
        return tui_quit(state)
    if screen in _PAGERS:
        return _pager_key(state, key)
    rows = tui_rows(state)
    if not rows:
        return _back(state) if key.name in ("esc", "backspace") else []
    focus = max(0, min(state.focus.get(screen, 0), len(rows) - 1))
    row = rows[focus]
    last = len(rows) - 1
    page = max(1, state.rows - 6)
    wrap = len(rows) < 10
    name = key.name
    if name == "up" or char == "k":
        focus = last if wrap and focus == 0 else max(0, focus - 1)
    elif name == "down" or char == "j":
        focus = 0 if wrap and focus == last else min(last, focus + 1)
    elif name == "pgup":
        focus = max(0, focus - page)
    elif name == "pgdn":
        focus = min(last, focus + page)
    elif name == "home" or char == "g":
        focus = 0
    elif name == "end" or char == "G":
        focus = last
    elif name in ("esc", "backspace") and state.filters.get(screen):
        # The filter goes first, the screen with the second press, and the
        # row in focus stays in focus.
        state.filters[screen] = ""
        rows = tui_rows(state)
        focus = next((i for i, item in enumerate(rows)
                      if item.action == row.action and item.value == row.value), focus)
    elif name in ("esc", "backspace"):
        return _back(state)
    elif (name == "tab" or char == "o") and screen in ("plan", "systems", "confirm"):
        return _push(state, "options")
    elif char == "/" and any(item.action == "filter" for item in rows):
        state.editing = screen
        return []
    elif screen == "regions" and (name == "enter" or char == " "):
        return _activate(state, row)
    elif screen == "regions" and name in ("left", "shift-up"):
        _move_region(state, row.value, -1)
        return []
    elif screen == "regions" and name in ("right", "shift-down"):
        _move_region(state, row.value, 1)
        return []
    elif screen in _CHECKLISTS and row.action in ("system", "core") and (
            char == " " or name in ("right", "left")):
        _tick(state, row, None if char == " " else name == "right")
        return []
    elif screen in _CHECKLISTS and char == " ":
        # X on All, None or the filter: only A, an explicit choice, acts there.
        return []
    elif screen == "systems" and char in ("a", "n"):
        return _activate(state, rows[0 if char == "a" else 1])
    elif screen == "options" and name in ("left", "right"):
        return _option(state, row.action, 1 if name == "right" else -1) \
            if row.action in ("game_data", "installed", "jobs") \
            or (row.action == "from" and name == "left") else []
    elif name == "enter" or char == " ":
        return _activate(state, row)
    state.focus[screen] = focus
    return []


def _take_event(state: TuiState, event: dict) -> None:
    kind = event.get("event")
    if kind == "checking" and state.run in ("check", "install"):
        state.reading = (event["files_done"], event["files_total"])
    elif kind == "found" and state.run == "check":
        state.found.append(event)
    elif kind == "found" and state.run == "install":
        state.local_dests.add(event["dest"])
    elif state.run == "check":
        if kind == "check":
            state.check = event
        elif kind == "file" and event.get("state") != "present":
            state.files.append(event)
        elif kind == "system":
            state.systems.append(event)
    elif state.run in ("confirm", "preview"):
        if kind == "check":
            state.dry = event
        elif kind == "file" and event.get("state") != "present":
            state.dry_files.append(event)
        elif kind == "summary":
            state.dry_summary = event
    elif state.run == "install":
        if kind == "download":
            state.download = event
        elif kind in ("progress", "downloaded"):
            state.progress = event
            if kind == "downloaded" and event["dest"] in state.local_dests:
                # A copy from an old card or a folder: its bytes come from
                # this machine, and a speed counting them was not the network's.
                state.local_done += event["size"]
            state.samples = [sample for sample in state.samples
                             if sample[0] >= state.now - 10] + [
                (state.now, event["bytes_done"] - state.local_done)]
            if kind == "downloaded":
                state.finished.append((event["dest"], event["ok"], event["reason"]))
        elif kind == "summary":
            state.summary = event


def tui_receive(state: TuiState, message: tuple) -> list[tuple]:
    """What the file list fetch and the child runs report back."""
    kind = message[0]
    if kind == "manifest":
        _kind, plat, manifest = message
        if plat != state.platform or state.screen != "checking":
            return []
        state.manifest, state.manifest_for = manifest, plat
        return _start_run(state, "check")
    if kind == "manifest_failed":
        _kind, plat, title, lines, code = message
        if plat != state.platform or state.screen != "checking":
            return []
        _fail(state, lines, code)
        state.error_title = title
        return []
    _kind, run_id, *rest = message
    if run_id != state.run_id:
        return []
    if kind == "event":
        _take_event(state, rest[0])
        return []
    code, tail = rest
    state.running = False
    state.tail = [line for line in tail if not line.startswith("RetroBIOS ")]
    tail = state.tail
    if state.run == "check":
        if state.check is None:
            return _fail(state, tail, code)
        state.ticked = {row["system"] for row in state.systems if _needed(row)}
        if state.keep_ticked is not None:
            state.ticked = state.keep_ticked & {row["system"] for row in state.systems}
            state.keep_ticked = None
        state.facts = _tui_folder_facts(state)
        _replace(state, "plan")
    elif state.run == "confirm" and state.screen == "confirm":
        if state.dry is None:
            return _fail(state, tail, code)
        if _same_plan(state.check, state.dry):
            state.stack.pop()
            return _install_now(state)
    elif state.run == "install":
        state.code = 130 if state.stopping and code < 0 else code if code >= 0 else 128 - code
        _replace(state, "result")
    return []


# Shorter key names for a narrow bar: at 40 columns "X/Space Tick" was cut,
# and nothing said how to tick with a controller.
_SHORT_KEYS = (("A/Enter ", "A "), ("B/Esc ", "B "), ("X/Space ", "X "), ("Y/o ", "Y "),
               ("Left/Right ", "<> "), ("Up/Down ", "^v "))


def _hint(width: int, *parts: str) -> str:
    """The keys that matter now, short names first given up, then whole
    entries from the right."""
    full = "  ".join(parts)
    if cell_width(full) <= width:
        return full
    short = []
    for part in parts:
        for long_name, short_name in _SHORT_KEYS:
            if part.startswith(long_name):
                part = short_name + part[len(long_name):]
        short.append(part)
    text = ""
    for part in short:
        longer = f"{text}  {part}" if text else part
        if cell_width(longer) > width:
            break
        text = longer
    return text or fit(short[0], width)


# What A does on each kind of row: the bar names it for the row in focus.
_ROW_VERBS = {
    "install": "Install", "close": "Close", "quit": "Quit", "systems": "Open",
    "options": "Open", "setup": "Continue", "another": "Open", "platform": "Choose",
    "default": "Use this folder", "browse": "Browse", "type": "Type a path",
    "use": "Use this folder", "up": "Go up", "dir": "Open", "create": "Create",
    "other": "Choose another", "cardroot": "Use the root", "all": "Tick all",
    "none": "Untick all", "filter": "Filter", "system": "Continue", "core": "Done",
    "region": "Tick", "regions": "Open", "cores": "Open", "command": "Open",
    "preview": "Open", "folder": "Choose", "game_data": "Change", "installed": "Change",
    "jobs": "Change", "retry": "Try again", "back": "Back", "stay": "Stay",
    "fromcard": "Choose", "from": "Add a folder", "misplaced": "Show", "zip": "Copy from it",
}


def _row_line(row: TuiRow, focused: bool, width: int) -> TuiLine:
    right = middle(row.right, max(12, width // 2)) if row.right else ""
    text = _columns(("> " if focused else "  ") + row.label, right, width)
    return _styled(text, "focus" if focused else row.style)


def _text_lines(items: list, width: int) -> list[TuiLine]:
    lines: list[TuiLine] = []
    for item in items:
        style = getattr(item, "style", "")
        for piece in _wrap(str(item), width):
            lines.append(_styled(fit(piece, width), style))
    return lines


def _scrolled(lines: list[TuiLine], first: int, height: int, lead: int = 0) -> list[TuiLine]:
    """height lines of a longer list from first, the last one saying where.
    The lead rows (All, None, Filter) are not counted among the entries."""
    if len(lines) <= height:
        return lines
    shown = max(1, height - 1)
    first = max(0, min(first, len(lines) - shown))
    end = first + shown
    count = len(lines) - lead
    where = f"{max(1, first + 1 - lead)}-{max(1, end - lead)} of {count}"
    return lines[first:end] + [_styled(where, "dim")]


def _compose(
    state: TuiState, width: int, height: int, before: list, rows: list[TuiRow],
    after: list = (), footer: list = (),
) -> list[TuiLine]:
    """Text, then the rows, then more text, in height lines. The rows are
    always shown, scrolled around the focus when they do not fit."""
    footer_lines = _text_lines(list(footer), width)
    room = max(1, height - len(footer_lines))
    focus = max(0, min(state.focus.get(state.screen, 0), len(rows) - 1)) if rows else 0
    row_lines = [_row_line(row, index == focus, width) for index, row in enumerate(rows)]
    if len(row_lines) >= room:
        first = focus - (room - 1) // 2
        lead = next((i for i, row in enumerate(rows)
                     if row.action not in ("all", "none", "filter")), 0)
        body = _scrolled(row_lines, first, room, lead)
    else:
        budget = room - len(row_lines)
        top: list[TuiLine] = []
        for item in before:
            lines = _text_lines([item], width)
            if len(top) + len(lines) > budget:
                break
            top += lines
        budget -= len(top)
        gap = [_styled("")] if top and row_lines and budget > 0 else []
        budget -= len(gap)
        tail = _text_lines(list(after), width)
        bottom = ([_styled("")] + tail)[:budget] if tail and budget > 1 else []
        body = top + gap + row_lines + bottom
    body += [_styled("")] * (room - len(body))
    return body + footer_lines


def _pager(state: TuiState, width: int, height: int, items: list) -> list[TuiLine]:
    """The items in view, from the first shown on, each wrapped alone: the
    8,000 lines of a full preview were wrapped again at every key."""
    first = max(0, min(state.focus.get(state.screen, 0), _pager_last(state, items)))
    if len(items) <= height and first == 0:
        lines = _text_lines(items, width)
        if len(lines) <= height:
            return lines
    room = max(1, height - 1)
    lines: list[TuiLine] = []
    index = first
    while index < len(items) and len(lines) < room:
        lines += _text_lines([items[index]], width)
        index += 1
    whole = index if len(lines) <= room else index - 1
    lines = lines[:room]
    return lines + [_styled(f"{first + 1}-{max(first + 1, whole)} of {len(items)}", "dim")]


def _tui_misplaced(state: TuiState) -> list[dict]:
    """The files the BIOS folder holds at another place, each with its reason."""
    return [event for event in state.found if event["reason"]]


def _tui_found_elsewhere(state: TuiState) -> dict[str, list[dict]]:
    """The files a --from folder holds, by the folder as it was named."""
    found: dict[str, list[dict]] = {}
    for event in state.found:
        if not event["reason"]:
            found.setdefault(event["folder"], []).append(event)
    return found


def _plan_lines(check: dict, dest: Path | None, state: TuiState) -> list:
    need = _needed(check)
    data = check.get("data_missing", 0)
    # A file found on this machine is copied, not downloaded: the count and
    # the size to download leave it out, from the check's own events.
    local = {event["dest"] for event in state.found} if check is state.check else set()
    sizes = {event["dest"]: event["size"] for event in state.files}
    copied = [dest_ for dest_ in local if dest_ in sizes]
    fetch = max(0, need - len(copied))
    fetch_bytes = max(0, check["bytes_needed"] - sum(sizes[dest_] for dest_ in copied))
    elsewhere = _tui_found_elsewhere(state) if check is state.check else {}
    if elsewhere:
        where = ", ".join(f"{len(events)} found in {folder}"
                          for folder, events in elsewhere.items())
        headline = (f"{where}, {fetch} to download ({format_size(fetch_bytes)})"
                    if fetch else f"{where}, nothing to download")
    elif need and fetch:
        headline = f"{_plural(fetch, 'file')} to download, {format_size(fetch_bytes)}"
    elif need:
        headline = f"{_plural(len(copied), 'file')} to copy from this machine"
    if need:
        if data:
            headline += f", and {_plural(data, 'data file')}"
    if not need:
        headline = (f"{_plural(data, 'data file')} to fetch" if data
                    else "Everything is in place")
    wrong = check["wrong_hash"]
    lines: list = [
        _styled(headline, "title"),
        f"{check['present']} in place, {check['missing']} missing, "
        f"{wrong} wrong {'copy' if wrong == 1 else 'copies'}",
        f"BIOS folder: {_short_path(dest, state.home)}",
    ]
    free = state.facts.get("free")
    if free is not None:
        lines.append(f"Free space: {format_size(free)}")
        if check["bytes_needed"] > free:
            lines.append("Not enough free space for this download: free some, or "
                         "choose another folder in Options.")
    if wrong:
        lines.append("They are replaced, and the old copies are kept in a backup folder.")
    misplaced = _tui_misplaced(state) if check is state.check else []
    if misplaced:
        lines.insert(1, f"{_plural(len(misplaced), 'file')} "
                     f"{'is' if len(misplaced) == 1 else 'are'} in the wrong place, "
                     f"{'it' if len(misplaced) == 1 else 'they'} will be copied where the "
                     "emulators read "
                     f"{'it' if len(misplaced) == 1 else 'them'}.")
    if state.facts.get("writable") is False:
        lines.insert(1, "You cannot write to this folder: choose another one, or run "
                     + ("the installer as an administrator." if state.os_type == "windows"
                        else "the installer with sudo."))
    if check.get("not_installable"):
        lines.append(f"{_plural(check['not_installable'], 'file')} of the list "
                     f"{'is' if check['not_installable'] == 1 else 'are'} not in "
                     "the collection.")
    return lines


def _view_setup(state: TuiState, width: int, height: int) -> tuple:
    rows = tui_rows(state)
    focus = rows[min(state.focus.get("setup", 0), len(rows) - 1)]
    after = ([f"BIOS folder: {state.setups[focus.value][1]}"]
             if focus.action == "setup" else [])
    before = ["Found on this machine:" if state.setups else "Nothing was found here."]
    return ("Where do you play?",
            _compose(state, width, height, before, rows, after + state.notes),
            ("A/Enter Continue", "B/Esc Quit", "? Help"))


def _view_platforms(state: TuiState, width: int, height: int) -> tuple:
    rows = tui_rows(state)
    if state.dest_given is not None:
        title = "Whose files go there?"
        before = [f"Choose the setup that reads {_short_path(state.dest_given, state.home)}."]
    else:
        title = "Which setup?"
        before = ["Choose the setup that will use the BIOS files."]
    needle = state.filters.get("platforms", "")
    after = [f"No setup matches '{needle}'."] if len(rows) == 1 and needle else []
    return (title, _compose(state, width, height, before, rows, after),
            ("A/Enter Choose", "B/Esc Back", "/ Filter", "? Help"))


def _view_folder(state: TuiState, width: int, height: int) -> tuple:
    name = display_name(state.platform)
    if state.platform in CARD_PLATFORMS:
        title = "Where is the SD card?"
        before = [f"{name} keeps its BIOS files on its SD card. Choose the card's "
                  "root folder, the one holding .system."]
    elif state.folder_default is not None:
        title = "Which folder?"
        before = [f"{name} reads its BIOS files from:", str(state.folder_default)]
    else:
        title = "Which folder?"
        before = [f"No usual {name} folder here: browse for it or type it."]
    return (title, _compose(state, width, height, before, tui_rows(state)),
            ("A/Enter Choose", "B/Esc Back", "? Help"))


def _view_browse(state: TuiState, width: int, height: int) -> tuple:
    where = "Drives" if state.browse_dir is None else middle(str(state.browse_dir), width)
    after = [state.status] if state.status else []
    if state.browse_for == "from":
        after.insert(0, "Choose the old card, a backup folder or a ZIP: its BIOS files "
                     "are copied, each one checked, nothing on it is changed.")
    return ("Copy from" if state.browse_for == "from" else "Browse for a folder",
            _compose(state, width, height, [where], tui_rows(state), after),
            ("A/Enter Open", "B/Esc Back", "? Help"))


def _view_path(state: TuiState, width: int, height: int) -> tuple:
    text = state.path_text + "_"
    if cell_width(text) > width - 2:
        text = "..." + fit(text[::-1], width - 5)[::-1]
    lines = [f"The folder for {display_name(state.platform)}:", _styled(f"> {text}", "focus"),
             "", ("Typing or pasting replaces it. " if state.path_selected else "")
             + "Ctrl-U clears. Quotes are removed, ~ and variables are expanded."]
    if state.status:
        lines.append(state.status)
    return ("Type a path", _compose(state, width, height, lines, []),
            ("Enter Use this folder", "Esc Back", "Ctrl-U Clear"))


def _view_create(state: TuiState, width: int, height: int) -> tuple:
    before = [f"{state.dest} does not exist.",
              f"The nearest folder that does: {state.facts.get('nearest', '')}"]
    if state.facts.get("risky"):
        before.append("Every folder of this path would be made: check its start before "
                      "creating it.")
    return ("Create the folder?", _compose(state, width, height, before, tui_rows(state)),
            ("A/Enter Choose", "B/Esc Back"))


def _view_cardroot(state: TuiState, width: int, height: int) -> tuple:
    name = display_name(state.facts.get("card", ""))
    before = [f"{state.dest} is inside a {name} card. {name} reads its BIOS files under "
              f"the card's root: {state.facts.get('card_root', '')}"]
    return ("Use the card root?", _compose(state, width, height, before, tui_rows(state)),
            ("A/Enter Choose", "B/Esc Back"))


def _view_checking(state: TuiState, width: int, height: int) -> tuple:
    lines = [f"{display_name(state.platform)}: {_short_path(state.dest, state.home)}", ""]
    elapsed = format_duration(max(0.0, state.now - state.started))
    if state.manifest is None:
        lines.append(f"Getting the file list... {elapsed}")
    else:
        files = len(state.manifest.get("files", []))
        lines += [f"File list: {_plural(files, 'file')}.", _reading_line(state, elapsed)]
    return ("Checking", _compose(state, width, height, lines, []),
            ("Ctrl-C Quit", "B/Esc Back"))


def _reading_line(state: TuiState, elapsed: str) -> str:
    if state.reading:
        done, total = state.reading
        return f"Reading the files already in the folder: {done} of {total}"
    return f"Reading the files already in the folder... {elapsed}"


def _choice_line(state: TuiState) -> list[str]:
    """What the plan's numbers do not include yet. The options are read
    again by a check of their own, so only ticked systems are left here."""
    if tui_flags(state) == state.checked_flags:
        return []
    return [f"For your {len(state.ticked)} of {len(state.systems)} systems, the exact "
            "count comes before the install."]


def _view_plan(state: TuiState, width: int, height: int) -> tuple:
    check = state.check or {}
    lines = _plan_lines(check, state.dest, state)
    lines[1:1] = _choice_line(state)
    if state.status:
        lines.append(state.status)
    rows = tui_rows(state)
    if width < 54:
        rows = [row._replace(label="Copy from your old card or a folder")
                if row.action == "fromcard" else row for row in rows]
    return (display_name(state.platform, state.manifest),
            _compose(state, width, height, lines, rows),
            ("A/Enter Install", "B/Esc Back", "Y/o Options", "? Help"))


def _counts_predate_options(state: TuiState) -> bool:
    """Whether the check's counts were read under other options than the
    ones chosen since: a region or a core changes what each system needs."""
    def others(flags: list) -> list:
        return [flag for flag in flags if flag[0] != "--system"]
    return others(tui_flags(state)) != others(state.checked_flags)


def _ticked_files(state: TuiState) -> list[dict]:
    return [event for event in state.files
            if set(event["systems"]) & state.ticked
            or (not event["systems"] and OTHER_SYSTEMS in state.ticked)]


def _view_systems(state: TuiState, width: int, height: int) -> tuple:
    rows = tui_rows(state)
    if width < 60:
        # The name first: the size alone stays beside it, the count is in
        # the total below.
        rows = [row._replace(right=row.right.split(", ")[-1]) for row in rows]
    elif width >= 78:
        # The id --system takes, beside a name that does not show it.
        # Only where it fits whole: a cut id ("maker26-co") said nothing.
        rows = [row._replace(label=f"{row.label}  {row.value}")
                if row.action == "system" and row.value not in row.label
                and cell_width(f"> {row.label}  {row.value}  {row.right}") + 2 <= width
                else row for row in rows]
    chosen = _ticked_files(state)
    footer = [f"{len(state.ticked)} of {len(state.systems)} systems: "
              f"{_plural(len(chosen), 'file')}, "
              f"{format_size(sum(e['size'] for e in chosen))}"
              + (" before your options" if _counts_predate_options(state) else "")]
    needle = state.filters.get("systems", "")
    after = [f"No system matches '{needle}'."] if len(rows) == 3 and needle else []
    if state.status:
        after.append(state.status)
    return ("Choose systems",
            _compose(state, width, height, [], rows, after, footer),
            ("A/Enter Continue", "B/Esc Back", "X/Space Tick", "? Help"))


def _view_options(state: TuiState, width: int, height: int) -> tuple:
    rows = tui_rows(state)
    before = ["Each row is an option of the command line, named on the right."]
    after: list = []
    if width < 60:
        # A value cut to make room for its flag said nothing: the flag of
        # the focused row is named below instead.
        focus = rows[min(state.focus.get("options", 0), len(rows) - 1)]
        before = []
        after = [f"Sets {focus.right}"] if focus.right else []
        rows = [row._replace(right="") for row in rows]
    # A folder is cut in its middle, its last levels being what tells it apart.
    room = width - 4 - (0 if width < 60 else 2 + max(cell_width(r.right) for r in rows))
    rows = [row._replace(label="BIOS folder: " + middle(row.label[13:], room - 13), style="")
            if row.style == "path" else row for row in rows]
    return ("Options", _compose(state, width, height, before, rows, after),
            ("A/Enter Change", "B/Esc Back", "Left/Right Set", "? Help"))


def _view_regions(state: TuiState, width: int, height: int) -> tuple:
    before = ["Tick the regions to keep, best first. Where a system has one BIOS "
              "per region, the best one found is installed."]
    return ("Preferred regions",
            _compose(state, width, height, before, tui_rows(state)),
            ("A/Enter Tick", "B/Esc Done", "Left/Right Order", "? Help"))


def _view_cores(state: TuiState, width: int, height: int) -> tuple:
    before = ["Tick the cores you use. With none ticked, every core's files are kept."]
    return ("Cores", _compose(state, width, height, before, tui_rows(state)),
            ("A/Enter Done", "B/Esc Back", "X/Space Tick", "? Help"))


_STATE_WORDS = {"missing": "missing", "wrong_hash": "wrong copy"}


def _pager_items(state: TuiState) -> list:
    """The text of a screen that scrolls, the same for its view and its keys."""
    if state.screen == "misplaced":
        return [f"{event['dest']}: {event['reason']}" for event in _tui_misplaced(state)]
    if state.screen == "result":
        return tui_outcome(state)[1]
    if state.screen == "command":
        return ["The same choice without questions:", "", tui_command(state)]
    if state.running:
        items = ["Counting the files for these choices..."]
    elif state.dry is None:
        items = state.tail or ["The preview did not finish."]
    else:
        items = [f"{_plural(_needed(state.dry), 'file')} to download, "
                 f"{format_size(state.dry['bytes_needed'])}. Nothing is written.", ""]
        items += [f"{_STATE_WORDS.get(event['state'], event['state']):<10} "
                  f"{format_size(event['size']):>9}  {event['dest']}"
                  for event in state.dry_files]
        for kept in (state.dry_summary or {}).get("kept") or []:
            items.append(f"Replaced files would be kept in {kept['folder']}")
    return items


def _view_preview(state: TuiState, width: int, height: int) -> tuple:
    return ("Preview", _pager(state, width, height, _pager_items(state)),
            ("A/Enter Back", "Up/Down Scroll"))


def _view_misplaced(state: TuiState, width: int, height: int) -> tuple:
    return ("Files in the wrong place", _pager(state, width, height, _pager_items(state)),
            ("A/Enter Back", "Up/Down Scroll"))


def _view_command(state: TuiState, width: int, height: int) -> tuple:
    return ("Command line", _pager(state, width, height, _pager_items(state)),
            ("A/Enter Back",))


def _view_confirm(state: TuiState, width: int, height: int) -> tuple:
    if state.running or state.dry is None:
        lines: list = ["Counting the files for these choices..."]
    else:
        lines = _plan_lines(state.dry, state.dest, state)
    return ("Confirm", _compose(state, width, height, lines, tui_rows(state)),
            ("A/Enter Install", "B/Esc Back", "? Help"))


def _bar(share: float, width: int) -> str:
    room = max(4, width - 7)
    filled = int(share * room)
    return f"[{'#' * filled}{'-' * (room - filled)}] {int(share * 100):3d}%"


def _view_installing(state: TuiState, width: int, height: int) -> tuple:
    lines: list = []
    if state.download is None:
        elapsed = format_duration(max(0.0, state.now - state.started))
        lines.append(_reading_line(state, elapsed))
    else:
        total = state.download["bytes"]
        progress = state.progress or {}
        done = progress.get("bytes_done", 0)
        lines.append(_bar(min(done / total, 1.0) if total else 1.0, width))
        lines.append(f"{format_size(done)} of {format_size(total)}")
        window = [sample for sample in state.samples if sample[0] >= state.now - 5]
        fetched = done - state.local_done
        start, base = window[0] if window else (state.now, fetched)
        seconds = state.now - start
        speed = (fetched - base) / seconds if seconds >= 1 else 0
        if speed > 0:
            lines.append(f"{format_size(int(speed))}/s, "
                         f"{format_duration((total - done) / speed)} left")
        files_done = progress.get("files_done", 0)
        lines.append(f"{files_done} of {_plural(state.download['files'], 'file')}")
        if files_done >= state.download["files"] and state.summary is None:
            lines.append("Finishing...")
    lines.append(_styled("Stopping, finishing the files in flight. Press B or Ctrl-C "
                         "again to stop now." if state.stopping
                         else "B, Ctrl-C or Start+Select stops. What is done stays done.",
                         "dim"))
    room = height - len(lines) - 1
    failed = [item for item in reversed(state.finished) if not item[1]]
    good = [item for item in reversed(state.finished) if item[1]]
    shown = (failed + good)[:max(0, room)]
    if shown:
        lines.append("")
        lines += [f"{'ok    ' if ok else 'FAILED'} {dest}" + (f" ({reason})" if reason else "")
                  for dest, ok, reason in shown]
    return ("Installing", _compose(state, width, height, lines, []),
            ("B/Esc Stop", "? Help"))


def tui_outcome(state: TuiState) -> tuple[str, list[str]]:
    """The result in words, first what happened, then where and what next."""
    summary = state.summary or {}
    # Killed by the second Ctrl-C, the child wrote no summary: what it
    # reported finished is what is installed.
    installed = summary.get("installed", sum(1 for _dest, ok, _why in state.finished if ok))
    failed = summary.get("failed") or []
    up_to_date = summary.get("up_to_date", 0)
    total = (state.download or {}).get("files", 0)
    if summary.get("interrupted") or state.code == 130:
        title = "Stopped"
        head = (f"Stopped: {installed} of {total} installed, run again to finish."
                if total else "Stopped. Run again to carry on: what is done stays done.")
    elif failed and installed:
        title = "Finished with errors"
        head = f"{installed} installed, {len(failed)} failed."
    elif failed:
        title = "Nothing installed"
        head = f"Nothing was installed: {_plural(len(failed), 'file')} failed."
    elif not summary:
        title = "Stopped"
        head = f"The installer stopped with code {state.code}."
    elif installed:
        title = "Done"
        head = f"{_plural(installed, 'file')} installed."
        if up_to_date:
            head += f" {_plural(up_to_date, 'file')} already in place."
    else:
        title = "Done"
        head = "Everything is in place."
    lines = [head, f"BIOS folder: {state.dest}"]
    causes: dict[str, list[str]] = {}
    for failure in failed:
        causes.setdefault(failure["reason"], []).append(failure["dest"])
    shown = 0
    for reason, dests in causes.items():
        if len(dests) > 3:
            # A read-only folder fails every file alike: said once.
            lines.append(f"  {len(dests)} files failed ({reason or 'no reason given'}), "
                         f"the first {dests[0]}")
            continue
        for dest in dests:
            if shown < 20:
                lines.append(f"  failed: {dest}" + (f" ({reason})" if reason else ""))
            shown += 1
    if shown > 20:
        lines.append(f"  ... and {shown - 20} more")
    if not summary:
        lines += [f"  {line}" for line in state.tail[-8:]]
    run = Invocation()
    run.os_type = state.os_type
    for kept in summary.get("kept") or []:
        lines.append(f"The files it replaced are kept in {kept['folder']}")
        lines.append(f"To put them back: {run.launcher()} --restore "
                     f"{run.word(str(kept['folder']))}")
    skipped = summary.get("skipped") or []
    if skipped:
        count = sum(group["files"] for group in skipped)
        needed = sum(group["required"] for group in skipped)
        lines.append(f"{_plural(count, 'file')} of the list "
                     f"{'is' if count == 1 else 'are'} not in the collection"
                     + ("." if not needed
                        else ", and an emulator needs it to start." if count == 1
                        else f", {needed} of them needed to start an emulator."))
    for group in summary.get("left_out") or []:
        lines.append(f"Left out as {group['label']}: {_plural(group['files'], 'file')}.")
    lines += ["Same choice without questions:", f"  {tui_command(state)}"]
    if tui_keeps_log(state):
        lines.append(f"Details: {state.log}")
    return title, lines


def tui_keeps_log(state: TuiState) -> bool:
    """Whether the run's log is kept: when a failure names it, and only then.
    57 folders of logs from stopped and finished runs were left in /tmp."""
    if state.screen == "crashed":
        return True
    return bool(state.log) and state.code not in (0, 130, None)


def _view_result(state: TuiState, width: int, height: int) -> tuple:
    title, lines = tui_outcome(state)
    return (title, _pager(state, width, height, lines), ("A/Enter Exit", "Up/Down Scroll"))


def _view_error(state: TuiState, width: int, height: int) -> tuple:
    return (state.error_title or "Something went wrong",
            _compose(state, width, height, state.error, tui_rows(state)),
            ("A/Enter Choose", "B/Esc Back"))


def _view_quit(state: TuiState, width: int, height: int) -> tuple:
    return ("Quit?",
            _compose(state, width, height, ["Quit without installing? Nothing was written."],
                     tui_rows(state)),
            ("A/Enter Choose", "B/Esc Stay"))


_TUI_VIEWS: dict[str, Callable[[TuiState, int, int], tuple]] = {
    "setup": _view_setup, "platforms": _view_platforms, "folder": _view_folder,
    "browse": _view_browse, "path": _view_path, "create": _view_create,
    "checking": _view_checking, "plan": _view_plan, "systems": _view_systems,
    "options": _view_options, "regions": _view_regions, "cores": _view_cores,
    "preview": _view_preview, "command": _view_command, "confirm": _view_confirm,
    "installing": _view_installing, "result": _view_result, "error": _view_error,
    "quit": _view_quit, "cardroot": _view_cardroot, "misplaced": _view_misplaced,
}


def _help_lines(state: TuiState) -> list[tuple[str, str]]:
    screen = state.screen
    if screen == "installing":
        return [("B/Esc, Ctrl-C", "Stop: what is done stays done"),
                ("Start+Select", "Stop"), ("?, F1", "This help")]
    if screen == "path":
        return [("Enter", "Use this folder"), ("Backspace", "Delete a character"),
                ("Ctrl-U", "Clear"), ("Esc", "Back"), ("Ctrl-C", "Quit")]
    keys = [("Up/Down, k/j", "Move")]
    if len(tui_rows(state)) > 9 or screen in _PAGERS:
        keys += [("PgUp/PgDn, L1/R1", "Page"), ("Home/End, g/G", "First, last")]
    primary = _primary(state) or _TUI_VIEWS[screen](state, 80, 24)[2][0]
    keys.append(("A/Enter, Start", primary.split(" ", 1)[-1]))
    if screen in _CHECKLISTS:
        keys.append(("X, Right/Left", "Tick, untick"))
    if screen == "systems":
        keys.append(("a, n", "All, none"))
    if screen in ("platforms", "systems", "cores"):
        keys.append(("/", "Filter (Esc clears)"))
    if screen == "regions":
        keys.append(("Left/Right", "Move a region up or down the order"))
    if screen == "options":
        keys.append(("Left/Right", "Change a value"))
    if screen in ("plan", "systems", "confirm"):
        keys.append(("Y, o, Tab", "Options"))
    back = "Quit" if len(state.stack) == 1 else "Back"
    keys += [("B/Esc, Backspace", back), ("q, Ctrl-C", "Quit"), ("?, F1", "This help")]
    return keys


def _primary(state: TuiState) -> str | None:
    """What A does on the row in focus, for the first entry of the bar."""
    if state.screen in _PAGERS:
        return None
    rows = tui_rows(state)
    if not rows:
        return None
    row = rows[max(0, min(state.focus.get(state.screen, 0), len(rows) - 1))]
    verb = _ROW_VERBS.get(row.action)
    return f"A/Enter {verb}" if verb else None


def _view_help(state: TuiState, width: int, height: int) -> tuple:
    keys = _help_lines(state)
    column = min(22, max(cell_width(key) for key, _what in keys) + 2)
    lines = [f"{_pad(key, column)}{what}" for key, what in keys]
    if state.screen in ("setup", "plan", "browse", "folder") and state.dest is not None:
        lines += ["", f"BIOS folder: {state.dest}"]
    return ("Keys", _compose(state, width, height, lines, []), ("Any key closes this help",))




def tui_view(state: TuiState, cols: int, rows: int) -> list[str]:
    """The screen as rows lines of at most cols cells, from the state alone."""
    if cols < TUI_MIN_COLS or rows < TUI_MIN_ROWS:
        lines = [_styled("")] * rows
        text = fit("Make the window larger", cols)
        if rows:
            lines[rows // 2] = _styled(" " * max(0, (cols - cell_width(text)) // 2) + text)
        return lines
    width = min(cols, TUI_MAX_WIDTH) - 2
    height = rows - 3
    view = _view_help if state.help else _TUI_VIEWS[state.screen]
    title, body, hint = view(state, width, height)
    primary = _primary(state) if not state.help and hint[0].startswith("A/Enter") else None
    if primary:
        hint = (primary, *hint[1:])
    lines = [_styled(fit(f" RetroBIOS: {title}", cols), "title"), _styled("")]
    lines += [_styled(fit(" " + line, cols), line.style) for line in body[:height]]
    lines += [_styled("")] * (rows - 1 - len(lines))
    # Never the last cell of the screen: a terminal may scroll when it is written.
    lines.append(_styled(fit(" " + _hint(width, *hint), cols - 1), "dim"))
    return lines

def _setup_label(plat: str, path: Path) -> str:
    system = detected_os(plat, path)
    name = display_name(plat)
    return f"{system} ({name} files)" if system and system != name else name


def _install_kind(path: Path) -> str:
    """What sets one install of a frontend apart from another of the same."""
    # The config detection read it from says how it was installed: the
    # Flatpak's system directory can be anywhere its config names.
    found = _RETROARCH_CONFIGS.get(path)
    text = path.as_posix() + (" " + Path(found[0]).as_posix() if found else "")
    for marker, word in (("/.var/app/", "Flatpak"), ("/steamapps/", "Steam"),
                         ("/snap/", "Snap")):
        if marker in text:
            return word
    if (path.parent / "retroarch.cfg").is_file():
        return "portable"
    return "/".join(path.parts[-2:])


def tell_apart(setups: list[tuple]) -> list[tuple]:
    """Two setups that read alike get the word that separates them: the
    Flatpak and the native RetroArch were both "RetroArch" at 40 columns."""
    labels = [label for _plat, _path, label in setups]
    return [(plat, path, f"{label} ({_install_kind(path)})" if labels.count(label) > 1
             else label) for plat, path, label in setups]


def _tui_passthrough(args: argparse.Namespace) -> list[tuple[str, str | None]]:
    """Options the screens do not change, handed to every run unchanged."""
    flags: list[tuple[str, str | None]] = []
    for flag in ("--target", "--romm-firmware"):
        value = getattr(args, flag[2:].replace("-", "_"))
        if value is not None:
            flags.append((flag, str(value)))
    for flag in ("--save-dir", "--cores-dir"):
        value = getattr(args, flag[2:].replace("-", "_"))
        if value is not None:
            flags.append((flag, str(Path(value).absolute())))
    flags += [("--emulator", name) for name in args.emulator or []]
    flags += [("--from-pack", str(Path(pack).absolute())) for pack in args.from_pack or []]
    for flag in ("--every-board", "--standalone-copies", "--verbose"):
        if getattr(args, flag[2:].replace("-", "_")):
            flags.append((flag, None))
    return flags


def tui_start(
    args: argparse.Namespace, os_type: str, detect: bool = True,
) -> tuple[TuiState, list[tuple]]:
    """The first screen, from what is found here and what the command named."""
    state = TuiState(os_type=os_type, home=Path.home())
    state.cli_systems = list(args.system or [])
    state.regions = resolve_regions(args.region) if args.region else []
    state.cli_regions = list(state.regions)
    state.from_folders = [Path(folder).absolute() for folder in getattr(args, "from") or []]
    state.cores = list(dict.fromkeys(
        token.strip() for value in args.core or [] for token in value.split(",")
        if token.strip()))
    state.no_game_data = bool(args.no_game_data)
    state.installed_only = bool(args.installed_cores)
    state.jobs = args.jobs
    state.passthrough = _tui_passthrough(args)
    if args.manifest is not None:
        state.manifest_path = Path(args.manifest).absolute()
        state.manifest_given = True
    plat = normalize_platform(args.platform) if args.platform else ""
    if detect:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            found = detect_platforms(os_type)
            frontends = detect_frontends(os_type)
            cards = [(card_platform(mount), mount) for mount in _mounted_cards()]
        state.setups = [(name, path, _setup_label(name, path)) for name, path in found]
        state.setups += [(card, mount, f"{display_name(card)} SD card")
                         for card, mount in cards if card and (card, mount) not in found]
        state.setups = tell_apart(state.setups)
        if "esde" in frontends:
            state.notes.append("ES-DE found: its BIOS files go to each emulator, "
                               "not to ES-DE itself.")
        state.notes += [line.strip() for line in captured.getvalue().splitlines()
                        if line.strip()]
    if plat and args.dest is not None:
        state.stack = []
        return state, _choose(state, plat, Path(args.dest))
    if plat:
        matched = [setup for setup in state.setups if setup[0] == plat]
        if len(matched) == 1:
            state.stack = []
            return state, _choose(state, plat, matched[0][1])
        if matched:
            state.setups = matched
            return state, []
        state.platform = plat
        state.stack = []
        default = None if plat in CARD_PLATFORMS else _default_dest(os_type, plat)
        return state, _open_folder_screen(state, default)
    if args.dest is not None:
        card = card_platform(Path(args.dest))
        if card:
            state.stack = []
            return state, _choose(state, card, Path(args.dest))
        state.dest_given = Path(args.dest)
    if state.dest_given is not None or not state.setups:
        found_names = list(dict.fromkeys(setup[0] for setup in state.setups))
        default = found_names[0] if len(found_names) == 1 else "retroarch"
        state.stack = ["platforms"]
        state.focus["platforms"] = _platform_focus(state, default)
    return state, []


def _log_tail(path: Path, count: int = 12) -> list[str]:
    """The last lines a child wrote for a person, which say why it stopped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"The log {path} cannot be read: {exc.strerror or exc}"]
    lines = [line.rstrip() for line in text.replace("\r", "\n").splitlines()]
    # The banner says which installer ran, not what went wrong.
    return [line for line in lines if line.strip() and not line.startswith("RetroBIOS ")
            ][-count:]


class _EngineRun:
    """This installer run as a child with --events, read line by line."""

    def __init__(self, argv: list[str], log: Path, inbox: queue.Queue, run_id: int) -> None:
        self.log = log
        # Its own session: a signal typed at the terminal reaches the
        # interface alone, which forwards one Ctrl-C and waits for the summary.
        options: dict = {} if os.name == "nt" else {"start_new_session": True}
        with open(log, "w", encoding="utf-8", errors="replace") as handle:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=handle,
                text=True, encoding="utf-8", errors="replace", **options,
            )
        threading.Thread(target=self._read, args=(inbox, run_id), daemon=True).start()

    def _read(self, inbox: queue.Queue, run_id: int) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                inbox.put(("event", run_id, event))
        code = self.proc.wait()
        inbox.put(("ended", run_id, code, _log_tail(self.log)))

    def running(self) -> bool:
        return self.proc.poll() is None

    def interrupt(self, from_key: bool) -> None:
        if not self.running():
            return
        if os.name != "nt":
            self.proc.send_signal(signal.SIGINT)
        elif from_key:
            # The child shares the console: Ctrl-C read as a key reached no
            # one, so it is sent to the console, this process ignoring it.
            os.kill(0, signal.CTRL_C_EVENT)  # type: ignore[attr-defined]

    def stop(self, grace: float) -> None:
        """End the child, letting it write its summary for up to grace seconds."""
        if not self.running():
            return
        if grace:
            self.interrupt(from_key=True)
            try:
                self.proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                # It ignored Ctrl-C for that long: it is stopped outright.
                self.proc.kill()
        self.proc.kill()
        self.proc.wait()


# One try, for a screen with Try again on it: three tries of 30 s were 85 s
# of a bare timer before the error said there was no network.
TUI_FETCH_TIMEOUT = 15


def _tui_fetch(plat: str, inbox: queue.Queue, log: io.StringIO) -> None:
    """The file list, fetched once in this process, or what went wrong."""
    if LOCAL.manifest is not None or LOCAL.pack is not None:
        mark = len(log.getvalue())
        try:
            inbox.put(("manifest", plat, fetch_manifest(plat)))
        except SystemExit as exc:
            inbox.put(("manifest_failed", plat, "", log.getvalue()[mark:].splitlines(),
                       _event_exit_code(exc.code)))
        return
    url = MANIFEST_URL.format(platform=plat)
    try:
        data = _fetch_json(url, MAX_MANIFEST_BYTES, "file list", attempts=1,
                           timeout=TUI_FETCH_TIMEOUT)
        inbox.put(("manifest", plat, _validate_manifest(data, plat)))
        return
    except ValueError as exc:
        title, lines = "The file list is not valid", [f"What arrived is not a file list: {exc}."]
    except urllib.error.HTTPError as exc:
        title = "No file list"
        lines = [f"There is no file list for {display_name(plat)} at this address."
                 if exc.code == 404 else f"The server answered HTTP {exc.code}."]
    except (urllib.error.URLError, OSError) as exc:
        code, words = network_words(exc)
        title = {"clock": "The clock is wrong",
                 "no_ca": "No trusted certificates"}.get(code, "No connection")
        lines = [words[0].upper() + words[1:] + "."]
        if code == "network":
            lines = [f"The list of BIOS files could not be downloaded: {words}.",
                     "Check the Wi-Fi or the cable, then choose Try again."]
    if os.environ.get("RETROBIOS_BASE_URL"):
        lines.append(f"RETROBIOS_BASE_URL chose the address: {url}")
    inbox.put(("manifest_failed", plat, title, lines, 1))


class _TuiTerminal:
    """The terminal in raw mode: keys in, frames out, put back as found."""

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self.stdin, self.stdout = stdin, stdout
        self.fd = stdin.fileno()
        self.saved: list | None = None
        self.decoder = KeyDecoder()
        self.text = codecs.getincrementaldecoder(
            getattr(stdin, "encoding", None) or "utf-8")(errors="replace")
        self.encoding = getattr(stdout, "encoding", None) or "utf-8"
        locale_name = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
        try:
            utf8 = codecs.lookup(self.encoding).name == "utf-8"
        except LookupError:
            utf8 = False
        # Python turns a C locale into UTF-8 for itself, not for the
        # terminal: a console font there has no CJK, often no accents.
        self.ascii = not utf8 or locale_name in ("C", "POSIX")
        self.ended = False
        self.heard = 0.0
        self.frame: list[tuple[str, str]] = []

    def enter(self) -> None:
        if os.name != "nt":
            import termios  # noqa: PLC0415

            # Entered again after a stop, the mode to go back to stays the
            # one found the first time.
            if self.saved is None:
                self.saved = termios.tcgetattr(self.fd)
            mode = termios.tcgetattr(self.fd)
            mode[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR | termios.IXON)
            # ISIG off: Ctrl-C arrives as a key and is forwarded once, by hand.
            mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            mode[6][termios.VMIN] = 1
            mode[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        self.write(_SCREEN_ON)

    def leave(self) -> None:
        """The main screen and the mode found, the mode even when the write
        fails: a closed window answers EIO."""
        try:
            self.write(_SCREEN_OFF)
        finally:
            if self.saved is not None:
                import termios  # noqa: PLC0415

                saved, self.saved = self.saved, None
                termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)

    def write(self, text: str) -> None:
        self.stdout.write(text.encode(self.encoding, "replace").decode(self.encoding))
        self.stdout.flush()

    def size(self) -> tuple[int, int]:
        return _terminal_size(self.stdout)

    def draw(self, lines: list[str], cols: int) -> None:
        """Rewrite the lines that changed since the last frame, and only those."""
        out = [] if self.frame else ["\x1b[H\x1b[2J"]
        frame = [(str(line), getattr(line, "style", "")) for line in lines]
        for index, (text, style) in enumerate(frame):
            if index < len(self.frame) and self.frame[index] == (text, style):
                continue
            text = screen_text(text)
            if getattr(self, "ascii", False):
                text = ascii_text(text)
            if style == "focus":
                text = _pad(text, min(cols, TUI_MAX_WIDTH) - (index == len(frame) - 1))
            out.append(f"\x1b[{index + 1};1H{_STYLES.get(style, '')}{text}\x1b[0m\x1b[K")
        self.frame = frame
        if out:
            self.write("".join(out))

    def keys(self, timeout: float) -> list[TuiKey]:
        if os.name == "nt":
            return self._console_keys(timeout)
        if self.ended:
            time.sleep(timeout)
            return []
        import select  # noqa: PLC0415

        if not select.select([self.fd], [], [], timeout)[0]:
            # A paste whose end never came, after a second of silence.
            if self.decoder.paste is not None and time.monotonic() - self.heard > 1:
                return self.decoder.close_paste()
            return []
        keys = self._read()
        while self.decoder.buffer and not self.ended:
            if select.select([self.fd], [], [], TUI_ESC_WAIT)[0]:
                keys += self._read()
            else:
                keys += self.decoder.flush()
                break
        return keys

    def _read(self) -> list[TuiKey]:
        data = os.read(self.fd, 4096)
        self.heard = time.monotonic()
        if not data:
            # The terminal went away: what Ctrl-D at a question does.
            self.ended = True
            return [TuiKey("ctrl-d")]
        return self.decoder.feed(self.text.decode(data))

    def _console_keys(self, timeout: float) -> list[TuiKey]:
        import msvcrt  # noqa: PLC0415

        end = time.monotonic() + timeout
        keys: list[TuiKey] = []
        while True:
            while msvcrt.kbhit():  # type: ignore[attr-defined]
                key = decode_windows_key(msvcrt.getwch(),  # type: ignore[attr-defined]
                                         msvcrt.getwch)  # type: ignore[attr-defined]
                if key is not None:
                    keys.append(key)
            if keys or time.monotonic() >= end:
                return keys
            time.sleep(0.01)


def tui_farewell(state: TuiState) -> list[str]:
    """What stays in the terminal once the full screen is gone."""
    return [screen_text(line) for line in _farewell_lines(state)]


def _farewell_lines(state: TuiState) -> list[str]:
    if state.screen != "result" and state.download is not None:
        # An install ran: what it wrote is said, whatever ended it.
        title, lines = tui_outcome(state)
        return [f"RetroBIOS: {title}", *lines]
    if state.screen == "crashed":
        return [f"RetroBIOS stopped on an unexpected error. What it was is in {state.log}."]
    if state.screen == "result":
        title, lines = tui_outcome(state)
        return [f"RetroBIOS: {title}", *lines]
    if state.screen == "error":
        return [f"RetroBIOS: {state.error_title or 'something went wrong'}",
                *(line.replace("then choose Try again", "then run the installer again")
                  for line in state.error)]
    if state.exit_code == 0:
        return [f"RetroBIOS: everything is in place in {state.dest}."]
    return ["RetroBIOS: stopped. Nothing was installed."]


def run_tui(args: argparse.Namespace) -> int:
    """The full-screen interface, until it closes: the exit code it ends with."""
    os_type = _INVOCATION.os_type or detect_os()
    if os_type == "wsl":
        _enter_wsl()
    _open_local_sources(args)
    state, effects = tui_start(args, os_type)
    work = Path(tempfile.mkdtemp(prefix="retrobios-tui-"))
    script = str(Path(__file__).resolve())
    inbox: queue.Queue = queue.Queue()
    current: list[_EngineRun] = []
    written: dict[str, Path] = {}
    flags = {"interrupt": False, "resized": False, "suspend": False, "resume": False,
             "term": 0}
    # Start+Select on a handheld is gptokeyb's pkill: SIGTERM, then SIGKILL
    # 3 s later. The install gets 2 s to say what it wrote.
    terminating = {"signal": 0, "deadline": 0.0}
    ignore_interrupt_until = [0.0]
    terminal = _TuiTerminal(sys.stdin, sys.stdout)
    real_out, real_err = sys.stdout, sys.stderr
    buffer = io.StringIO()
    handlers: dict[int, object] = {}

    def perform(todo: list[tuple], from_key: bool = True) -> None:
        for effect in todo:
            kind = effect[0]
            if kind == "fetch":
                threading.Thread(target=_tui_fetch, args=(effect[1], inbox, buffer),
                                 daemon=True).start()
            elif kind == "run":
                for run in current:
                    run.stop(0)
                current.clear()
                if not state.manifest_given and not args.from_pack and state.manifest:
                    if state.platform not in written:
                        path = work / f"{state.platform}.json"
                        path.write_text(json.dumps(state.manifest), encoding="utf-8")
                        written[state.platform] = path
                    state.manifest_path = written[state.platform]
                log = work / f"{effect[1]}.log"
                if effect[1] == "install":
                    state.log = str(log)
                try:
                    current.append(_EngineRun(
                        tui_argv(state, effect[1], sys.executable, script), log, inbox,
                        state.run_id))
                except OSError as exc:
                    inbox.put(("ended", state.run_id, 1,
                               [f"The installer could not be started: {exc}"]))
            elif kind == "stop":
                for run in current:
                    run.interrupt(from_key)
                if os.name == "nt" and from_key:
                    ignore_interrupt_until[0] = time.monotonic() + 1.0
            elif kind == "kill":
                for run in current:
                    run.stop(0)

    def on_interrupt(_signum: int, _frame: object) -> None:
        flags["interrupt"] = True

    def on_resize(_signum: int, _frame: object) -> None:
        flags["resized"] = True

    def on_stop(signum: int, _frame: object) -> None:
        flags["term"] = flags["term"] or signum

    def on_suspend(_signum: int, _frame: object) -> None:
        flags["suspend"] = True

    def on_resume(_signum: int, _frame: object) -> None:
        flags["resume"] = True

    try:
        for name, handler in (("SIGINT", on_interrupt), ("SIGWINCH", on_resize),
                              ("SIGTERM", on_stop), ("SIGHUP", on_stop),
                              ("SIGTSTP", on_suspend), ("SIGCONT", on_resume)):
            number = getattr(signal, name, None)
            if number is not None:
                handlers[number] = signal.signal(number, handler)
        sys.stdout = sys.stderr = buffer
        terminal.enter()
        perform(effects)
        while state.exit_code is None:
            if flags["suspend"]:
                # Stopped from outside (kill -TSTP): the shell gets its
                # terminal back as it lent it, and the screen returns on fg.
                flags["suspend"] = False
                terminal.leave()
                os.kill(os.getpid(), signal.SIGSTOP)  # type: ignore[attr-defined]
                flags["resume"] = True
            if flags["resume"]:
                flags["resume"] = False
                terminal.enter()
                terminal.frame = []
            cols, rows = terminal.size()
            if (cols, rows) != (state.cols, state.rows) or flags["resized"]:
                flags["resized"] = False
                state.cols, state.rows = cols, rows
                terminal.frame = []
            state.now = time.monotonic()
            terminal.draw(tui_view(state, cols, rows), cols)
            for key in terminal.keys(TUI_FRAME):
                state.now = time.monotonic()
                perform(tui_update(state, key))
                if state.exit_code is not None:
                    break
            if flags["interrupt"]:
                flags["interrupt"] = False
                if time.monotonic() >= ignore_interrupt_until[0]:
                    perform(tui_interrupt(state), from_key=False)
            if flags["term"] and not terminating["signal"]:
                terminating["signal"] = flags["term"]
                if state.run == "install" and state.running:
                    terminating["deadline"] = time.monotonic() + 2
                    if not state.stopping:
                        perform(tui_interrupt(state))
                elif state.screen == "result" and state.code is not None:
                    state.exit_code = state.code
                else:
                    state.exit_code = 128 + terminating["signal"]
            if terminating["signal"] and state.running and \
                    time.monotonic() > terminating["deadline"]:
                perform([("kill",)])
            while state.exit_code is None:
                try:
                    message = inbox.get_nowait()
                except queue.Empty:
                    break
                state.now = time.monotonic()
                perform(tui_receive(state, message))
            if terminating["signal"] and state.screen == "result":
                # The result is printed in the terminal on the way out.
                state.exit_code = state.code if state.code is not None else 1
    except Exception:  # noqa: BLE001
        # A traceback is where every reporter of #71, #73 and #74 stopped:
        # it goes to a file, and the terminal says one sentence.
        crash = work / "error.log"
        crash.write_text(traceback.format_exc(), encoding="utf-8")
        state.log, state.code, state.exit_code = str(crash), 1, 1
        state.stack = ["crashed"]
    finally:
        # Each step on its own: a write the closed window refuses, or a
        # second signal while the install writes its summary, must not
        # skip the ones after it and leave the terminal raw.
        problems = _tui_cleanup(terminal, current, handlers, state)
        sys.stdout, sys.stderr = real_out, real_err
        for path in written.values():
            if path.exists():
                path.unlink()
        if not tui_keeps_log(state):
            shutil.rmtree(work, ignore_errors=True)
    stream = sys.stderr if state.screen in ("error", "crashed") else sys.stdout
    lines = tui_farewell(state)
    if problems:
        lines.append(f"The terminal may not be restored ({problems[0]}): type reset "
                     "and press Enter if it looks wrong.")
    try:
        for line in lines:
            print(line, file=stream)
    except OSError:
        # The terminal is gone: the exit code is all that still reaches anyone.
        return state.exit_code if state.exit_code is not None else 1
    return state.exit_code if state.exit_code is not None else 1


def _tui_cleanup(
    terminal: _TuiTerminal, runs: list[_EngineRun], handlers: dict, state: TuiState,
) -> list[str]:
    """Stop the child, put the terminal and the signal handlers back: what failed."""
    failures: tuple = (OSError, ValueError)
    if os.name != "nt":
        import termios  # noqa: PLC0415

        failures += (termios.error,)
    problems: list[str] = []

    def step(action: Callable[[], object]) -> None:
        try:
            action()
        except failures as exc:
            problems.append(str(exc) or type(exc).__name__)

    for name in ("SIGTERM", "SIGHUP", "SIGTSTP"):
        number = getattr(signal, name, None)
        if number is not None:
            step(functools.partial(signal.signal, number, signal.SIG_IGN))
    # An install stopped by a signal still gets to write its summary.
    for run in runs:
        step(functools.partial(run.stop, 10 if state.running and state.run == "install"
                               else 0))
    step(terminal.leave)
    for number, previous in handlers.items():
        step(functools.partial(signal.signal, number, previous))
    return problems


def _line_buffered_stdout() -> None:
    """Flush stdout line by line, as stderr is.

    Piped to a log (`| tee install.log`), stdout was block-buffered while
    errors went out at once: the log opened on "Could not fetch the file
    list" before the banner and the step that produced it.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(line_buffering=True)
        except (ValueError, OSError):
            # A stream that cannot be reconfigured keeps its own buffering:
            # the order of a log is cosmetic, the run goes on.
            return


if __name__ == "__main__":
    _line_buffered_stdout()
    # A reader that stops early (`install.py --list | head`) ends the run
    # quietly instead of on a BrokenPipeError traceback.
    try:
        main()
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
    except KeyboardInterrupt:
        # Outside the download phase (fetching the file list, checking the
        # files present, rebuilding data directories): nothing is half
        # written there, and a socket traceback read as a crash. Stopped at
        # a question, nothing has started, and there is nothing to carry on.
        if _writing.is_set():
            print("\nStopped. Run the same command again to carry on from here. "
                  "Nothing is lost.")
        else:
            print("\nStopped. Nothing was installed.")
        sys.exit(130)
