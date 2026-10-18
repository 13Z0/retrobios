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
import concurrent.futures
import errno
import hashlib
import http.client
import io
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TextIO

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
MAX_TOTAL_DOWNLOAD_SIZE = 64 * 1024 * 1024 * 1024
# Why a manifest leaves a file out, as the installer words it. The manifest
# schema and the generator are held to this list by the tests.
OMISSION_REASONS = {
    "hash_mismatch": "hash mismatch",
    "not_found": "not found",
    "external": "external",
    "user_provided": "user provided",
    "no_platform_slug": "no platform folder",
}
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
}
_REGION_PARENT = {
    member: parent for parent, members in REGION_TREE.items() for member in members
}
REGIONS = frozenset({WORLD_REGION} | set(REGION_TREE) | set(_REGION_PARENT))
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
    "retropie",
)

# Fallback BIOS destination per platform when --platform is forced
# but auto-detection finds nothing on the machine.
DEFAULT_DESTS = {
    "batocera": Path("/userdata/bios"),
    "recalbox": Path("/recalbox/share/bios"),
    "lakka": Path("/storage/system"),
    "retrodeck": Path.home() / "retrodeck",
    "emudeck": Path.home() / "Emulation" / "bios",
    "rocknix": Path("/storage/roms/bios"),
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
        profile = Path(os.environ.get("USERPROFILE", str(home)))
        return {
            # RetroArch's system directory defaults to <exe folder>\system
            # (platform_win32.c), and its installer to C:\RetroArch-Win64.
            "retroarch": Path("C:/RetroArch-Win64/system"),
            # RetroBat's installer defaults to C:\RetroBat (installer.iss).
            "retrobat": Path("C:/RetroBat/bios"),
            "bizhawk": profile / "BizHawk" / "Firmware",
        }
    if os_type == "darwin":
        return {"retroarch": _macos_retroarch_base() / "system"}
    return {
        "retroarch": home / ".config" / "retroarch" / "system",
        "bizhawk": home / ".config" / "BizHawk" / "Firmware",
    }

# Set when the run is interrupted, so queued downloads end instead of draining
# the pool: every future is submitted before the first one completes.
_stop = threading.Event()

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
    switched on; older ones print them raw, so the answer is then no.
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

    A pipe or a log gets plain lines; NO_COLOR and TERM=dumb are honoured.
    """

    def __init__(self, stream: TextIO | None = None, quiet: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.quiet = quiet
        isatty = getattr(self.stream, "isatty", None)
        self.live = bool(isatty and isatty()) and os.environ.get("TERM") != "dumb"
        self.colour = (
            self.live and "NO_COLOR" not in os.environ and _enable_ansi()
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
        self.total_bytes = max(total_bytes, 1)
        self.term = term
        self.files = 0
        self.bytes = 0
        self.resumed = 0
        self.start = time.monotonic()
        self._drawn = 0.0
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
        filled = int(share * self.BAR)
        bar = "#" * filled + "-" * (self.BAR - filled)
        elapsed = self.elapsed()
        speed = self.bytes / elapsed if elapsed > 0 else 0
        left = (self.total_bytes - self.bytes) / speed if speed > 0 else 0
        text = (
            f"  [{bar}] {share * 100:5.1f}%  "
            f"{format_size(self.bytes)} / {format_size(self.total_bytes)}  "
            f"{format_size(int(speed))}/s  "
            f"{format_duration(left)} left  "
            f"({self.files}/{self.total_files} files)"
        )
        print(f"\r\033[K{text}", end="", file=self.term.stream, flush=True)


# The three RetroArch packages published for Android, newest ABI first.
ANDROID_RETROARCH_PACKAGES = (
    "com.retroarch.aarch64",
    "com.retroarch",
    "com.retroarch.ra32",
)


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
    path = Path("/etc/os-release")
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


def _expand_retroarch_path(value: str, app_dir: Path) -> Path:
    """Expand the notations RetroArch writes into its config values.

    fill_pathname_expand_special (libretro-common/file/file_path.c) maps a
    leading '~' to the home directory and a leading ':' to the application
    directory, dropping the two leading characters in both cases.
    """
    if value[:1] == "~":
        return Path(str(Path.home())) / value[2:]
    if value[:1] == ":":
        return app_dir / value[2:]
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _retroarch_cfg_dir(
    cfg_path: Path, key: str, app_dir: Path, default: str
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
            return _expand_retroarch_path(value, app_dir)
    return None


def _steam_libraries() -> list[Path]:
    """Every Steam library, the default one first.

    Steam lists the others in steamapps/libraryfolders.vdf, each as a
    "path" line; a RetroArch installed on D:/SteamLibrary was not found
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
    names under Program Files, every Steam library, and the Emulators folder
    of an installed ES-DE (es_find_rules.xml lists all of them).
    """
    candidates = [Path("C:/RetroArch-Win64"), Path("C:/RetroArch")]
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
    return [folder for folder in candidates if (folder / "retroarch.exe").is_file()]


def _macos_retroarch_base() -> Path:
    """Where RetroArch on macOS keeps its system folder by default:
    ~/Documents/RetroArch, or Application Support/RetroArch when that folder
    holds portable.txt (platform_darwin.m)."""
    support = Path.home() / "Library" / "Application Support" / "RetroArch"
    if (support / "portable.txt").is_file():
        return support
    return Path.home() / "Documents" / "RetroArch"


# System directory -> the retroarch.cfg it was read from and the directory
# ':' expands to there, for finding the cores of the same installation.
_RETROARCH_CONFIGS: dict[Path, tuple[Path, Path]] = {}


def _parse_retroarch_system_dir(
    cfg_path: Path, app_dir: Path | None = None
) -> Path | None:
    """Parse system_directory from retroarch.cfg.

    app_dir is RetroArch's application directory, used to expand ':' values.
    It defaults to the directory holding the config, which is where a portable
    install keeps both.
    """
    if not cfg_path.exists():
        return None
    app_dir = app_dir or cfg_path.parent
    system_dir = _retroarch_cfg_dir(cfg_path, "system_directory", app_dir, "system")
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
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            check = line.lstrip("$")
            if check.startswith(f"{normalized}="):
                _, _, value = check.partition("=")
                return value.strip('"').strip("'")
    except OSError:
        pass
    return None


def _detect_embedded() -> list[tuple[str, Path]]:
    """Check for embedded Linux retrogaming OSes."""
    found: list[tuple[str, Path]] = []
    os_release = _parse_os_release()
    os_id = os_release.get("ID", "").lower()

    if os_id == "rocknix":
        found.append(("rocknix", Path("/storage/roms/bios")))
        return found

    # MiSTer keeps the SD card at /media/fat with the main binary at its root
    # (Main_MiSTer file_io.cpp:1150-1156), and cores read from games/
    # (file_io.h:168)
    if Path("/media/fat/MiSTer").exists():
        found.append(("misterfpga", Path("/media/fat/games")))
        return found

    if Path("/etc/knulli-release").exists():
        found.append(("batocera", Path("/userdata/bios")))
        return found

    if os_id == "lakka":
        found.append(("lakka", Path("/storage/system")))
        return found

    if Path("/etc/batocera-version").exists():
        found.append(("batocera", Path("/userdata/bios")))
        return found

    if (
        Path("/recalbox/recalbox.version").exists()
        or Path("/usr/bin/recalbox-settings").exists()
    ):
        found.append(("recalbox", Path("/recalbox/share/bios")))
        return found

    if Path("/opt/muos").exists() or Path("/mnt/mmc/MUOS/").exists():
        found.append(("retroarch", Path("/mnt/mmc/MUOS/bios")))
        return found

    if Path("/home/ark").exists() and Path("/opt/system").exists():
        found.append(("retroarch", Path("/roms/bios")))
        return found

    if Path("/mnt/vendor/bin/dmenu.bin").exists():
        found.append(("retroarch", Path("/mnt/mmc/bios")))
        return found

    return found


def _android_storage_root() -> Path:
    """Shared storage root, the parent RetroArch builds its defaults from."""
    value = os.environ.get("EXTERNAL_STORAGE", "").strip()
    return Path(value) if value else Path("/storage/emulated/0")


def _detect_android() -> list[tuple[str, Path]]:
    """Find the RetroArch system directory on Android.

    The config is read from the app external files directory first, then from
    the internal one (platform_unix.c:1327-1395). A system_directory left at
    default resolves against <shared storage>/RetroArch, which is also where
    the directory sits when no config has been written yet
    (platform_unix.c:2836-2851).
    """
    found: list[tuple[str, Path]] = []
    storage = _android_storage_root()
    default_root = storage / "RetroArch"

    for package in ANDROID_RETROARCH_PACKAGES:
        for cfg in (
            storage / "Android" / "data" / package / "files" / "retroarch.cfg",
            Path("/data/data") / package / "files" / "retroarch.cfg",
        ):
            system_dir = _parse_retroarch_system_dir(cfg, default_root)
            if system_dir:
                found.append(("retroarch", system_dir))
                return found

    if default_root.exists():
        found.append(("retroarch", default_root / "system"))
    return found


def _lnk_target(lnk_path: Path, exe_name: str) -> Path | None:
    """Read the target path of a Windows shortcut.

    Shortcuts embed the target as a plain local path; scanning for it avoids
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

    LaunchBox stores its emulator list in Data/Emulators.xml; ApplicationPath
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
        # ElementTree expands internal entities; LaunchBox writes no doctype
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

    Only emulators whose destination LaunchBox itself computes are listed;
    the rest keep the default locations declared in the manifest.
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
# fetched; a test holds this table equal to the registry. Without it a Linux
# home holding RetroPie/BIOS or .config/BizHawk/Firmware detected nothing.
_PATH_RULES: tuple[tuple[str, str, str, str | None], ...] = (
    ("windows", "retrobat", "C:\\RetroBat\\bios", None),
    ("windows", "retrobat", "%USERPROFILE%\\RetroBat\\bios", None),
    ("linux", "emudeck", "$HOME/Emulation/bios", None),
    ("linux", "retrodeck", "$HOME/.var/app/net.retrodeck.retrodeck", "$HOME/retrodeck"),
    ("linux", "romm", "/romm/library/bios", None),
    ("linux", "retropie", "$HOME/RetroPie/BIOS", None),
    ("windows", "bizhawk", "%USERPROFILE%\\BizHawk\\Firmware", None),
    ("linux", "bizhawk", "$HOME/.config/BizHawk/Firmware", None),
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
    return Path(template)


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

    if os_type in ("linux", "wsl"):
        found.extend(_detect_embedded())

        # EmuDeck (Linux/SteamOS)
        home = Path.home()
        emudeck_settings = home / ".config" / "EmuDeck" / "settings.sh"
        if emudeck_settings.exists():
            emu_path = _parse_bash_var(emudeck_settings, "emulationPath")
            if emu_path:
                bios_dir = Path(emu_path) / "bios"
                found.append(("emudeck", bios_dir))

        # RetroDECK: retrodeck.json since the cfg-to-json migration, which
        # renames the old retrodeck.cfg to retrodeck.bak (global.sh:149-153)
        retrodeck_conf_dir = home / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        retrodeck_json = retrodeck_conf_dir / "retrodeck.json"
        retrodeck_cfg = retrodeck_conf_dir / "retrodeck.cfg"
        if retrodeck_json.exists():
            rd_home = _parse_json_path(retrodeck_json, "paths", "rd_home_path")
            found.append(("retrodeck", Path(rd_home) if rd_home else home / "retrodeck"))
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
        # (configuration.c); a value left at default is the documents folder,
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
        # EmuDeck Windows
        home = Path.home()
        emudeck_ps1 = Path(os.environ.get("APPDATA", "")) / "EmuDeck" / "settings.ps1"
        if emudeck_ps1.exists():
            emu_path = _parse_ps1_var(emudeck_ps1, "$emulationPath")
            if emu_path:
                found.append(("emudeck", Path(emu_path) / "bios"))

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

        # Portable RetroArch referenced by LaunchBox
        lb_root = launchbox_root(os_type)
        if lb_root and not any(name == "retroarch" for name, _ in found):
            system_dir = _launchbox_retroarch_system_dir(
                lb_root / "Data" / "Emulators.xml"
            )
            if system_dir:
                found.append(("retroarch", system_dir))

    _detect_by_path(os_type, found)
    return found


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
    libretro_directory; without one, a retroarch.cfg beside the system
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


def normalize_platform(name: str) -> str:
    """Return the canonical platform id for user-supplied input."""
    plat = name.strip().lower()
    if plat not in AVAILABLE_PLATFORMS:
        print(f"Unknown platform '{name}'. Available platforms:", file=sys.stderr)
        for p in AVAILABLE_PLATFORMS:
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)
    return plat


def _read_limited_json(
    response: http.client.HTTPResponse, limit: int, label: str
) -> object:
    """Read a bounded UTF-8 JSON response."""
    raw_length = response.headers.get("Content-Length") if response.headers else None
    if raw_length:
        try:
            if int(raw_length) > limit:
                raise ValueError(f"{label} exceeds {limit} bytes")
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
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


def _destination_path(root: Path, value: object) -> Path:
    """Place a manifest destination below *root*.

    The manifest cannot climb: _safe_relative_path refuses absolute paths,
    drive letters and '..'. Symbolic links met under the root are the
    user's own layout and are followed. EmuDeck links bios/shadps4/sys_modules
    into shadPS4's data directory, and the file has to land where the
    emulator reads it.
    """
    relative = _safe_relative_path(value, "dest")
    return root.resolve() / Path(*relative.parts)


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
    cores = entry.get("cores")
    if cores is not None and (
        not isinstance(cores, list)
        or not all(isinstance(core, str) for core in cores)
    ):
        raise ValueError(f"invalid omitted cores list for {dest}")
    for field in SELECTION_FIELDS:
        _check_tags(entry, field, dest)


def _check_download(entry: dict, where: str, dest: str) -> int:
    """The size, hashes, source and cores of one downloadable copy; its size."""
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
    """The copies a platform file takes as cores and systems drop; the largest size."""
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
        if not isinstance(entry, dict):
            raise ValueError(f"files[{index}] must be an object")
        dest = str(_safe_relative_path(entry.get("dest"), f"files[{index}].dest"))
        if dest in seen_destinations:
            raise ValueError(f"duplicate manifest destination: {dest}")
        seen_destinations.add(dest)

        size = _check_download(entry, f"files[{index}]", dest)
        total_size += size
        alternatives = entry.get("alternatives", [])
        if not isinstance(alternatives, list) or len(alternatives) > MAX_SELECTION_TAGS:
            raise ValueError(f"invalid alternatives for {dest}")
        for alt_index, alternative in enumerate(alternatives):
            where = f"files[{index}].alternatives[{alt_index}]"
            if (
                not isinstance(alternative, dict)
                or not alternative.get("cores")
                or not set(alternative) <= set(_COPY_FIELDS)
            ):
                raise ValueError(f"invalid alternative for {dest}")
            # Narrowing can swap in any copy: the cap holds for the largest.
            size = max(size, _check_download(alternative, where, dest))
        size = max(size, _check_slot(entry, f"files[{index}]", dest))
        absent = entry.get("absent_claims", [])
        if not isinstance(absent, list) or len(absent) > MAX_SELECTION_TAGS:
            raise ValueError(f"invalid absent claims for {dest}")
        for claim in absent:
            if not isinstance(claim, dict):
                raise ValueError(f"invalid absent claim for {dest}")
            _check_omission(claim, dest)
        largest_total += size
        if largest_total > MAX_TOTAL_DOWNLOAD_SIZE:
            raise ValueError("manifest total size exceeds safety limit")
        for field in (*SELECTION_FIELDS, "declared_in"):
            _check_tags(entry, field, dest)

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
        if dest in seen_destinations or dest in seen_omitted:
            raise ValueError(f"duplicate or conflicting omitted destination: {dest}")
        seen_omitted.add(dest)
        _check_omission(entry, dest)
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

    for field in ("core_names", "system_cores"):
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


def fetch_manifest(plat: str) -> dict:
    """Download platform manifest JSON."""
    url = MANIFEST_URL.format(platform=plat)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return _validate_manifest(
                _read_limited_json(resp, MAX_MANIFEST_BYTES, "manifest"), plat
            )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print(f"  Failed to fetch manifest for {plat}: {exc}", file=sys.stderr)
        sys.exit(1)


def fetch_targets(plat: str) -> "dict | None":
    """Download the target core list.

    Empty on 404, where the platform publishes no targets. None when the list
    could not be read: a timeout says nothing about the platform, and
    answering "no targets" for it sent users to the full pack.
    """
    url = TARGETS_URL.format(platform=plat)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return _validate_targets(
                _read_limited_json(resp, MAX_TARGETS_BYTES, "targets manifest")
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        print(f"  Error: failed to fetch targets for {plat}: {exc}", file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  Error: failed to fetch targets for {plat}: {exc}", file=sys.stderr)
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
    way (`mednafen_psx`); a file names the profile that reads it
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
        raise ValueError(f"unknown region: {raw.strip()}")
    return key


def _region_comparable(a: str, b: str) -> bool:
    return a == b or _REGION_PARENT.get(a) == b or _REGION_PARENT.get(b) == a


def _region_rank(file_regions: set[str], requested: list[str]) -> int:
    for index, wanted in enumerate(requested):
        if any(_region_comparable(wanted, region) for region in file_regions):
            return index
    return len(requested)


def region_drops(
    entries: list[dict], requested: list[str], systems: list[str] | None = None
) -> set[str]:
    """Destinations a region priority list withdraws.

    The pack builder's rule (region.resolve_region_drops), replayed over the
    groups the manifest records. Per group, the best-ranked regional file
    stays; a world file beats unmatched regional ones the code does not search
    before it (`priority`, lower first); when nothing matches and no world
    file exists, every regional file stays, so a group is never emptied. A file with no region never competes and always stays.

    A core extra competes whatever systems are chosen; a platform's own
    declaration competes only when its system is kept, as in the pack.
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
            groups.setdefault(group, []).append((set(regions), entry["dest"], priority))
    keep: set[str] = set()
    drop: set[str] = set()
    for members in groups.values():
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
        if matched:
            best = min(rank for rank, _dest in matched)
            keep |= {dest for rank, dest in matched if rank == best}
            drop |= {dest for _rank, dest in regional}
        elif world:
            world_ranks = [ranks[dest] for dest in world]
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

    An omission's `system` is spelled the way its profile spells it; only
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
        dropped = region_drops(files + omitted, regions, systems)
        files = [f for f in files if f["dest"] not in dropped]
        omitted = [o for o in omitted if o["dest"] not in dropped]
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
    """Whether a manifest carries what narrowing reads; older ones do not."""
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


def resolve_choices(values: list[str], known: list[str], label: str) -> list[str]:
    """Names given on the command line, matched without regard to case.

    Each value is one name or a comma-separated list. A whole value that is
    itself a name wins over splitting it: Recalbox names one system
    `msx1,msx2,msxturbor`. An unknown name is refused, never ignored, since
    carrying on would install something other than what was asked.
    """
    by_lower = {name.lower(): name for name in known}
    chosen: list[str] = []
    unknown: list[str] = []
    for value in values:
        value = value.strip()
        tokens = [value] if value.lower() in by_lower else value.split(",")
        for token in (t.strip() for t in tokens):
            if not token:
                continue
            name = by_lower.get(token.lower())
            if name is None:
                unknown.append(token)
            elif name not in chosen:
                chosen.append(name)
    if unknown:
        available = ", ".join(known) if known else "none"
        raise ValueError(
            f"unknown {label}: {', '.join(unknown)} (available: {available})"
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
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(path, "rb") as fh:
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
    files: list[dict], bios_path: Path, jobs: int = 8
) -> tuple[list[dict], list[dict], list[dict]]:
    """Check which files exist locally and have correct hashes.

    Returns (to_download, up_to_date, mismatched).
    """
    to_download: list[dict] = []
    present: list[tuple[dict, Path]] = []

    for f in files:
        dest = _destination_path(bios_path, f["dest"])
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
            for entry, verdict in verdicts:
                if verdict == "up_to_date":
                    up_to_date.append(entry)
                else:
                    mismatched.append(entry)

    return to_download, up_to_date, mismatched


def _download_one(
    f: dict, bios_path: Path, verbose: bool = False,
    progress: Progress | None = None,
) -> tuple[str, bool, str]:
    """Download a single file. Returns (dest, success, reason it failed)."""
    if _stop.is_set():
        return str(f.get("dest", "?")), False, "stopped"
    try:
        dest = _destination_path(bios_path, f["dest"])
    except ValueError:
        return str(f.get("dest", "?")), False, "unsafe destination"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if _write_refused(exc):
            return f["dest"], False, NOT_WRITABLE
        return f["dest"], False, exc.strerror or str(exc)

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
                    if offset >= expected_size:
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
                        f"downloaded {downloaded} bytes; expected {expected_size}"
                    )

            expected_sha256 = f.get("sha256", "")
            expected_sha1 = f.get("sha1", "")
            if expected_sha256 and _sha256_file(tmp_path) != expected_sha256.lower():
                reason = "SHA-256 mismatch"
                raise _Corrupt()
            if expected_sha1 and _sha1_file(tmp_path) != expected_sha1.lower():
                reason = "SHA-1 mismatch"
                raise _Corrupt()

            # The mode a plain open() gives, executable where the repository
            # file is: the temporary file is private, and a BIOS folder
            # written with sudo has to stay readable by the emulator's user.
            os.chmod(tmp_path, (0o777 if f.get("executable") else 0o666) & ~_UMASK)
            if partial is not None and os.name == "nt":
                # Windows renames no open file; elsewhere the lock is held
                # until the copy has its final name.
                partial.close()
            os.replace(tmp_path, dest)
            return f["dest"], True, ""

        except _Interrupted:
            # Stopped mid-transfer: a large file keeps its partial copy for
            # the next run; a small one is dropped rather than left beside
            # the destination.
            if tmp_path is not None and partial is None and tmp_path.exists():
                tmp_path.unlink()
            return f["dest"], False, "stopped"

        except _Corrupt:
            if verbose:
                print(f"    {reason} on attempt {attempt}", file=sys.stderr)
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()
            delay = 0.0

        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
            if exc.code in (403, 429):
                reason += " (GitHub rate limit or access refused)"
            if verbose:
                print(f"    Attempt {attempt} failed: {exc}", file=sys.stderr)
            if tmp_path is not None and partial is None and tmp_path.exists():
                tmp_path.unlink()
            if exc.code == 416 and tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()  # the partial copy no longer fits the file
            delay = _retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)

        except (urllib.error.URLError, OSError, ValueError) as exc:
            if (isinstance(exc, OSError) and not isinstance(exc, urllib.error.URLError)
                    and _write_refused(exc)):
                # No attempt can change a refused write.
                reason, refused = NOT_WRITABLE, True
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


def _partial_size(path: Path) -> int:
    """Bytes already held by a partial copy; anything but a plain file is dropped."""
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
    return isinstance(exc, PermissionError) or exc.errno == errno.EROFS


def _write_advice(path: Path, assumed: bool) -> str:
    """What to do about a folder this run could not write.

    sudo fixes a root-owned BIOS folder on the console itself. On another
    machine it would create that folder on the wrong disk: a platform named
    but not found here is a card or share mounted elsewhere, named by --dest.
    """
    if assumed:
        return (f"  {path} is where the platform keeps its BIOS on its own system, "
                "and it was not found here. If this machine prepares its card or "
                "share, run the same command with --dest pointing at that folder.")
    return ("  This user cannot write there: run the same command with sudo, "
            "or choose another folder with --dest.")


def _plural(count: int, word: str) -> str:
    """Count and noun, so a run of one file does not read as machine output."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


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
) -> None:
    """Close the run on what happened, and leave with a status that says so.

    A run that installed nothing used to end on the word Done and a zero exit,
    which reads as success to a person and to a script alike.
    """
    if transferred and seconds > 0:
        print(f"\n{format_size(transferred)} downloaded in "
              f"{format_duration(seconds)} ({format_size(int(transferred / seconds))}/s).")
    for dest, reason in (failures or [])[:10]:
        print(f"  failed: {dest}" + (f" ({reason})" if reason else ""))
    if failures and len(failures) > 10:
        print(f"  ... and {len(failures) - 10} more")
    if declined and not downloaded and not errors and not interrupted:
        print(f"\nNothing installed: {_plural(declined, 'file')} not downloaded, "
              "as answered.")
        print(f"{_plural(up_to_date, 'file')} already up to date at {_locations(paths)}.")
        return
    if interrupted:
        print(f"\nStopped. {_plural(downloaded, 'file')} installed.")
        print("Run the same command again to carry on from here. Nothing is lost.")
        sys.exit(130)

    refused = any(reason == NOT_WRITABLE for _dest, reason in failures or [])
    advice = [_write_advice(path, path in (assumed or set())) for path in paths] if refused else []

    if errors and not downloaded:
        print(f"\nNothing was installed. {_plural(errors, 'file')} failed.")
        print(f"Location that was being written to: {_locations(paths)}")
        for line in advice:
            print(line)
        print("Run the same command again once that is resolved. "
              "Files already installed are kept.")
        sys.exit(2)

    if errors:
        print(f"\nFinished with errors. {_plural(downloaded, 'file')} installed, "
              f"{_plural(errors, 'file')} failed.")
        print(f"Location: {_locations(paths)}")
        for line in advice:
            print(line)
        print("Run the same command again to retry only those.")
        sys.exit(1)

    print(f"\nDone. {_plural(downloaded, 'file')} installed, "
          f"{_plural(up_to_date, 'file')} already up to date.")
    print(f"Location: {_locations(paths)}")
    if omitted:
        for line in _omission_lines(omitted, omitted_entries or []):
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
    required = sorted({e["name"] for e in entries if e.get("required")})
    if required:
        shown = ", ".join(required[:10])
        more = f" and {len(required) - 10} more" if len(required) > 10 else ""
        lines.append(f"  Needed to start an emulator: {shown}{more}.")
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


def _confirm(prompt: str) -> bool:
    """Yes unless the answer starts with n."""
    return not _ask(prompt).lower().startswith("n")


def _print_plan(to_download: list[dict], bios_path: Path, term: Terminal) -> None:
    """What a real run would fetch, by system, and nothing written."""
    total = sum(f.get("size", 0) for f in to_download)
    if not to_download:
        print("\n  All files up to date. A real run would download nothing.")
        return
    print(f"\nWould download {_plural(len(to_download), 'file')} "
          f"({format_size(total)}) to {bios_path}:")
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
    print("\n  Nothing was written. Run without --dry-run to install.")


def _check_free_space(bios_path: Path, needed: int) -> None:
    """Stop before writing when the volume cannot hold the download.

    Reported against the nearest existing ancestor, since the destination
    itself may have just been created on a volume that is already full.
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
    print(f"  Free up {format_size(needed - free)}, or use --dest to install "
          "to another drive.", file=sys.stderr)
    sys.exit(1)


def download_files(
    files: list[dict], bios_path: Path, jobs: int = 8, verbose: bool = False,
    term: Terminal | None = None, progress: Progress | None = None,
) -> tuple[list[tuple[str, str]], bool]:
    """Download files in parallel.

    Returns the destinations that failed, each with its reason, and whether
    the run was interrupted. Every future is submitted up front, so a bare
    KeyboardInterrupt would let the pool drain the queue in full: the stop
    flag is what makes Ctrl+C end the run rather than only stop the
    reporting.
    """
    failed: list[tuple[str, str]] = []
    interrupted = False
    _stop.clear()
    term = term or Terminal()
    if progress is None:
        progress = Progress(len(files), sum(f.get("size", 0) for f in files), term)

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        future_map = {
            pool.submit(_download_one, f, bios_path, verbose, progress): f
            for f in files
        }
        try:
            for future in concurrent.futures.as_completed(future_map):
                dest, success, reason = future.result()
                if _stop.is_set():
                    continue
                progress.done(dest, success, reason)
                if not success:
                    failed.append((dest, reason))
        except KeyboardInterrupt:
            interrupted = True
            _stop.set()
            progress.finish()
            print("\n  Stopping. Files already installed are kept.")
    progress.finish()
    return failed, interrupted


def do_standalone_copies(
    manifest: dict, bios_path: Path, os_type: str,
    extra_dirs: dict[str, Path] | None = None,
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
    full, read-only mount), which used to be counted as "dir not found".
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

        # Resolve source files
        if "pattern" in entry:
            sources = [
                f for f in bios_path.rglob("*")
                if fnmatch(f.name, entry["pattern"]) and f.is_file()
            ]
        else:
            # `source` is where this platform's manifest put the file; `file`
            # names it as the registry does for every platform, and laid out
            # that way it was missing wherever the pack places it elsewhere.
            src = _destination_path(bios_path, entry.get("source") or entry["file"])
            sources = [src] if src.exists() else []

        if not sources:
            continue

        # Resolve target directories with WSL fallback
        targets = entry.get("targets", {}).get(os_type, [])
        if not targets and os_type == "wsl":
            targets = entry.get("targets", {}).get("linux", [])

        target_dirs = [
            Path(os.path.expandvars(os.path.expanduser(t))) for t in targets
        ]
        extra_root = (extra_dirs or {}).get(entry.get("emulator", ""))
        if extra_root is not None:
            subdir = PurePosixPath(entry.get("file", "")).parent
            extra = extra_root if str(subdir) == "." else extra_root / subdir
            if extra not in target_dirs:
                target_dirs.append(extra)

        for target_dir in target_dirs:
            if not target_dir.is_dir():
                skipped += len(sources)
                continue
            resolved_dir = target_dir.resolve()
            for src in sources:
                dest = target_dir / src.name
                # A symlink already sitting at the destination would redirect
                # the write outside the directory the user opted into, and a
                # crafted source name would climb out of it.
                if dest.is_symlink() or not _within(dest, resolved_dir):
                    skipped += 1
                    continue
                try:
                    shutil.copy2(src, dest)
                    copied += 1
                except OSError as exc:
                    print(f"  ERROR: {dest}: {exc.strerror or exc}")
                    failed += 1

    return copied, skipped, failed


def format_size(n: int) -> str:
    """Human-readable file size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _prompt_manual_platform(os_type: str) -> list[tuple[str, Path]]:
    """Ask for a platform and a BIOS directory when nothing was detected."""
    print("\nChoose the platform to install for:")
    for i, name in enumerate(AVAILABLE_PLATFORMS, 1):
        print(f"  {i}) {name}")
    print("  q) quit")
    print()
    plat = ""
    while not plat:
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if choice == "q":
            sys.exit(0)
        if choice.isdigit() and 1 <= int(choice) <= len(AVAILABLE_PLATFORMS):
            plat = AVAILABLE_PLATFORMS[int(choice) - 1]
        elif choice in AVAILABLE_PLATFORMS:
            plat = choice
    default_dest = _default_dest(os_type, plat)
    try:
        raw = input(f"BIOS directory [{default_dest}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    dest = Path(raw).expanduser() if raw else default_dest
    if os_type == "windows" and raw:
        dest = Path(os.path.expandvars(raw))
    return [(plat, dest)]


def _default_dest(os_type: str, platform_name: str) -> Path:
    """Fall back path for a platform named on the command line but not found.

    A home directory is meaningless for RetroArch on Android, where Termux
    owns the home and the frontend reads from shared storage.
    """
    if os_type == "android" and platform_name == "retroarch":
        return _android_storage_root() / "RetroArch" / "system"
    return _os_default_dests(os_type).get(
        platform_name, DEFAULT_DESTS.get(platform_name, Path.home() / "bios")
    )


def _manual_usage_hint(os_type: str) -> list[str]:
    """How to name the platform and directory when nothing is detected."""
    if os_type == "windows":
        return [
            "  Use --platform and --dest to specify manually, for example:",
            "    & ([ScriptBlock]::Create((iwr -useb "
            "https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1))) "
            "--platform retroarch --dest 'C:\\RetroArch\\system'",
        ]
    if os_type == "android":
        return [
            "  Use --platform and --dest to specify manually, for example:",
            "    python3 install.py --platform retroarch "
            "--dest /storage/emulated/0/RetroArch/system",
        ]
    return [
        "  Use --platform and --dest to specify manually, for example:",
        "    python3 install.py --platform retroarch --dest ~/.config/retroarch/system",
    ]


def _prompt_platform_choice(
    platforms: list[tuple[str, Path]],
) -> list[tuple[str, Path]]:
    """Prompt user to choose among detected platforms."""
    print("\nInstall for:")
    for i, (name, path) in enumerate(platforms, 1):
        print(f"  {i}) {name.capitalize()} ({path})")
    if len(platforms) > 1:
        print(f"  {len(platforms) + 1}) All")
    print()

    while True:
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if not choice:
            continue
        try:
            idx = int(choice)
        except ValueError:
            continue
        if 1 <= idx <= len(platforms):
            return [platforms[idx - 1]]
        if idx == len(platforms) + 1 and len(platforms) > 1:
            return platforms


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def _sized(files: list[dict]) -> str:
    return f"{_plural(len(files), 'file')}, {format_size(sum(f.get('size', 0) for f in files))}"


def _pick(title: str, rows: list[tuple[str, list[dict]]], ordered: bool = False) -> list[str]:
    """Numbered list of names; an empty answer keeps every one."""
    if not rows:
        return []
    print(f"\n{title}")
    width = len(str(len(rows)))
    name_width = min(max(len(name) for name, _files in rows), 32)
    for index, (name, files) in enumerate(rows, 1):
        print(f"  {index:>{width}}) {name:<{name_width}}  {_sized(files)}")
    hint = "order of preference, e.g. 2,1" if ordered else "e.g. 1,3-5"
    while True:
        text = _ask(f"Numbers ({hint}), Enter for all, q to quit: ")
        if not text:
            return []
        if text.lower() == "q":
            sys.exit(0)
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
) -> tuple[list[str], list[str], list[str]] | str | None:
    """Offer to narrow the install by system, core and region.

    Enter installs everything, the default most runs want. Returns None
    then, INSTALLED_ONLY for what `installed` (the files the cores found
    installed read) holds, or the three choices; each empty list keeps
    everything on its axis.
    """
    print(f"\n{plat.capitalize()}: {_sized(files)}.")
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
            sys.exit(0)
        if answer == "i" and installed is not None:
            return INSTALLED_ONLY
        if answer == "c":
            break

    choices = available_choices(files, omitted)
    if OTHER_SYSTEMS in choices["systems"]:
        print(f"\n'{OTHER_SYSTEMS}' holds what cores load for systems this platform")
        print("does not list, such as game engines and extra computers.")
    systems = _pick("Systems:", [
        (name, [f for f in files if _chosen(f, {name})])
        for name in choices["systems"]
    ])
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
        ])
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


def _print_choices(
    plat: str, files: list[dict], omitted: list[dict], systems: bool
) -> None:
    """What --system or --core accepts for a platform, with what each holds."""
    choices = available_choices(files, omitted)
    names = choices["systems"] if systems else choices["cores"]
    if not names:
        print(f"  {plat} records no {'systems' if systems else 'cores'}")
        return
    width = min(max(len(name) for name in names), 40)
    for name in names:
        held = (
            [f for f in files if _chosen(f, {name})]
            if systems
            else _filter_by_target([f for f in files if f.get("cores") is not None], [name])
        )
        print(f"  {name:<{width}}  {_sized(held)}")


def _requested_narrowing(
    args: argparse.Namespace, plat: str, files: list[dict], omitted: list[dict]
) -> tuple[list[str], list[str], list[str]] | None:
    """The systems, cores and regions named on the command line, checked."""
    if not (args.system or args.core or args.region):
        return None
    if not records_selection(files):
        # Without the fields, a region filter would match nothing and install
        # everything: refused rather than ignored.
        print(
            f"Error: the {plat} file list records no systems or regions; "
            "narrowing needs a newer one",
            file=sys.stderr,
        )
        sys.exit(1)
    choices = available_choices(files, omitted)
    try:
        systems = resolve_choices(args.system or [], choices["systems"], "system")
        cores = resolve_choices(args.core or [], choices["cores"], "core")
        regions = resolve_regions(args.region or [])
    except ValueError as exc:
        print(f"Error: {plat}: {exc}", file=sys.stderr)
        sys.exit(1)
    return systems, cores, regions


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Download missing BIOS files for retrogaming emulators.",
    )
    parser.add_argument(
        "--platform",
        help="target platform (retroarch, batocera, emudeck, ...)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        help="override BIOS destination directory",
    )
    parser.add_argument(
        "--target",
        help="hardware target for core filtering (switch, rpi4, ...)",
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
        "--installed-cores",
        action="store_true",
        help="install only what the cores already installed read "
             "(RetroArch, Lakka, RetroPie)",
    )
    parser.add_argument(
        "--system",
        action="append",
        metavar="NAME",
        help="install only these systems (repeat or comma-separate; see --list-systems)",
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
        "--no-input",
        action="store_true",
        help="never ask a question; fail where an answer is needed",
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
        "--version",
        action="version",
        version=f"retrobios installer {installer_id()} (ref {RELEASE_REF})",
    )

    args = parser.parse_args()
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be between 1 and 32")
    if args.region:
        try:
            resolve_regions(args.region)
        except ValueError as exc:
            parser.error(f"{exc} (known: {', '.join(sorted(REGIONS))})")
    for option, values in (("--system", args.system), ("--core", args.core)):
        if values is not None and not any(
            token.strip() for value in values for token in value.split(",")
        ):
            parser.error(f"{option} needs at least one name")
    if args.quiet and (
        args.check or args.dry_run or args.list_platforms or args.list_targets
        or args.list_systems or args.list_cores
    ):
        parser.error("--quiet applies to an install, not to --check, --dry-run or a listing")
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


def _install(args: argparse.Namespace, real_stdout: TextIO) -> None:
    """One run of the installer, once the options are read."""
    term = Terminal(real_stdout, quiet=args.quiet)
    interactive = sys.stdin.isatty() and not args.no_input and not args.quiet
    listing = args.list_targets or args.list_systems or args.list_cores
    print(f"{term.bold('RetroBIOS')} {term.dim(installer_id())}\n")

    os_type = detect_os()

    # Early exit for listing
    if args.list_platforms:
        print("Available platforms:")
        for p in AVAILABLE_PLATFORMS:
            print(f"  {p}")
        detected = detect_platforms(os_type)
        if detected:
            print("\nDetected on this system:")
            for name, path in detected:
                print(f"  {name}: {path}")
        return

    # Platform detection or override. A destination taken from the default
    # table, the platform not being found here, is only a guess.
    assumed_dests: set[Path] = set()
    if args.platform:
        args.platform = normalize_platform(args.platform)
    if args.platform and args.dest:
        platforms = [(args.platform, args.dest)]
    elif args.platform:
        print("Detecting platform...")
        detected = detect_platforms(os_type)
        matched = [(n, p) for n, p in detected if n == args.platform]
        if matched:
            platforms = matched
        else:
            default_dest = _default_dest(os_type, args.platform)
            print(
                f"  Platform '{args.platform}' not detected, "
                f"using default path: {default_dest}"
            )
            platforms = [(args.platform, default_dest)]
            assumed_dests.add(default_dest)
    elif args.dest:
        print(f"  Using destination: {args.dest}")
        platforms = [("retroarch", args.dest)]
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
            if not interactive or listing:
                for line in _manual_usage_hint(os_type):
                    print(line)
                sys.exit(1)
            platforms = _prompt_manual_platform(os_type)
        for name, path in platforms:
            print(f"  Found {name.capitalize()} at {path}")

    if len(platforms) > 1 and not listing and interactive:
        platforms = _prompt_platform_choice(platforms)

    total_downloaded = 0
    total_up_to_date = 0
    total_errors = 0
    total_omitted = 0
    omitted_seen: list[dict] = []
    total_interrupted = False
    total_bytes = 0
    total_seconds = 0.0
    total_declined = 0
    check_pending = 0
    failures: list[tuple[str, str]] = []
    installed_paths: list[Path] = []

    # Every check --installed-cores makes before anything is written: a
    # refusal for the second platform must not follow an install of the first.
    cores_found: dict[tuple[str, Path], list[str]] = {}
    for plat_name, bios_path in platforms:
        if plat_name in CORE_PLATFORMS:
            searched = core_dirs(plat_name, bios_path)
            cores_found[(plat_name, bios_path)] = installed_cores(searched)
        elif args.installed_cores:
            print(
                f"Error: {plat_name} ships its cores with the system; "
                f"--installed-cores applies to {', '.join(CORE_PLATFORMS)}",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.installed_cores and not cores_found[(plat_name, bios_path)]:
            print(
                f"Error: no installed core found for {plat_name} in: "
                f"{', '.join(map(str, searched)) or 'no known directory'}",
                file=sys.stderr,
            )
            sys.exit(1)

    for plat_name, bios_path in platforms:
        if bios_path not in installed_paths:
            installed_paths.append(bios_path)
        print(f"\nFetching file index for {plat_name}...")
        manifest = fetch_manifest(plat_name)
        files = manifest.get("files", [])
        omitted_files = manifest.get("omitted_files", [])

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

        # Target filtering
        if args.target:
            targets = fetch_targets(plat_name)
            if targets is None:
                sys.exit(1)
            target_info = targets.get(args.target)
            if target_info is None:
                # Carrying on would install every file, which is the opposite
                # of what a filter was asked for: a 4 GB download instead of
                # the subset the target needs. Refuse rather than ignore.
                print(
                    f"Error: unknown target '{args.target}' for {plat_name}.",
                    file=sys.stderr,
                )
                if targets:
                    print(
                        f"  available: {', '.join(sorted(targets))}", file=sys.stderr
                    )
                else:
                    print(
                        f"  {plat_name} publishes no target list", file=sys.stderr
                    )
                sys.exit(1)
            elif target_info.get("cores") is None:
                print(
                    f"  Target '{args.target}' publishes no core list; "
                    "installing every file"
                )
            else:
                before = len(files)
                files, omitted_files = narrow_to_cores(
                    manifest, target_info["cores"], files, omitted_files
                )
                print(f"  Filtered {before} -> {len(files)} files for target {args.target}")

        found_cores = cores_found.get((plat_name, bios_path), [])
        if "system_cores" not in manifest:
            found_cores = []
        elif found_cores:
            print(f"  {_plural(len(found_cores), 'core')} installed")
        if args.installed_cores:
            if "system_cores" not in manifest:
                print(
                    f"Error: the {plat_name} file list does not say which cores "
                    "read which systems; --installed-cores needs a newer one",
                    file=sys.stderr,
                )
                sys.exit(1)
            before = len(files)
            files, omitted_files = narrow_to_cores(
                manifest, found_cores, files, omitted_files
            )
            print(f"  Narrowed {before} -> {len(files)} files to the installed cores")

        selection = _requested_narrowing(args, plat_name, files, omitted_files)
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
                plat_name, files, omitted_files, installed
            )
            if selection == INSTALLED_ONLY:
                files, omitted_files = installed
                print("\n  Same choice without questions: "
                      f"--platform {plat_name} --installed-cores")
                selection = None
            elif selection is not None:
                print("\n  Same choice without questions: "
                      + _same_choice_hint(plat_name, *selection))
        if selection is not None:
            before = len(files)
            files, omitted_files = narrow(files, omitted_files, *selection)
            print(f"  Narrowed {before} -> {len(files)} files")

        if args.list_systems or args.list_cores:
            _print_choices(plat_name, files, omitted_files, args.list_systems)
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
                f"  Safety notice: {len(omitted_files)} unavailable or unsafe "
                f"entries omitted ({required_omitted} required; {reason_summary})."
            )
            total_omitted += len(omitted_files)
            omitted_seen.extend(omitted_files)

        print("\nChecking existing files...")
        to_download, up_to_date, mismatched = check_local(
            files, bios_path, jobs=args.jobs
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
                stale = _destination_path(bios_path, entry["dest"])
            except ValueError:
                continue
            stale = stale.parent / f".{stale.name}.part"
            if stale.is_file():
                stale.unlink()

        # Mismatched files need re-download
        to_download.extend(mismatched)

        if args.check:
            if to_download:
                print(f"\n  {len(to_download)} files need downloading.")
                check_pending += len(to_download)
            else:
                print("\n  All files up to date.")
            continue

        if args.dry_run:
            _print_plan(to_download, bios_path, term)
            continue

        if to_download:
            dl_size = sum(f.get("size", 0) for f in to_download)
            if (
                interactive
                and not args.yes
                and dl_size >= CONFIRM_FROM_BYTES
                and not _confirm(
                    f"\nDownload {format_size(dl_size)} to {bios_path}? [Y/n] "
                )
            ):
                print("  Nothing downloaded. --system, --core and --region "
                      "install less (see --help).")
                total_up_to_date += len(up_to_date)
                total_declined += len(to_download)
                continue
            print(f"\nDownloading {len(to_download)} files ({format_size(dl_size)})...")
            print("  Press Ctrl+C to stop. Running the same command again "
                  "carries on from here.")
            try:
                bios_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"\nCannot create {bios_path}.", file=sys.stderr)
                print(f"  {exc.strerror}.", file=sys.stderr)
                print(_write_advice(bios_path, bios_path in assumed_dests),
                      file=sys.stderr)
                sys.exit(1)
            _check_free_space(bios_path, dl_size)
            progress = Progress(len(to_download), dl_size, term)
            failed, interrupted = download_files(
                to_download, bios_path, jobs=args.jobs, verbose=args.verbose,
                term=term, progress=progress,
            )
            total_downloaded += len(to_download) - len(failed)
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
                manifest, bios_path, os_type, extra_dirs
            )
            if copied or skipped or copy_failed:
                print(
                    f"  {copied} copied, {skipped} skipped (dir not found), "
                    f"{copy_failed} failed"
                )
            total_errors += copy_failed
        elif manifest.get("standalone_copies") and not args.check:
            print(
                "\nStandalone copies skipped "
                "(use --standalone-copies to opt in)."
            )

    sys.stdout = real_stdout
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
        )


if __name__ == "__main__":
    # A reader that stops early (`install.py --list | head`) ends the run
    # quietly instead of on a BrokenPipeError traceback.
    try:
        main()
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
