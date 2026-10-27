#!/usr/bin/env python3
"""Run a libretro core headless and record what it did.

Three modes share one runner:

  run    one execution, printed as JSON
  bench  warm-up then timed repetitions, with timing statistics
  check  the same execution under several variants of one system file,
         compared on a named observable

Each repetition runs in its own process, under strace when it is installed,
so the files a core opened are read from the kernel rather than guessed.
`--record DIR` writes one JSON record per invocation, carrying what is needed
to reproduce it (core build, frontend commit, host, inputs by hash).
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORD_VERSION = 1
MIN_BENCH_REPETITIONS = 5
DEFAULT_WORK_DIR = ROOT / "tmp" / "core_probe"
BASE_OBSERVABLES = ("frame_sha1", "video_sha1", "audio_sha1", "state_sha1")

# libretro.h environment commands the probe answers.
ENV_GET_CAN_DUPE = 3
ENV_GET_SYSTEM_DIRECTORY = 9
ENV_SET_PIXEL_FORMAT = 10
ENV_GET_VARIABLE = 15
ENV_SET_VARIABLES = 16
ENV_GET_VARIABLE_UPDATE = 17
ENV_SET_SUPPORT_NO_GAME = 18
ENV_GET_LOG_INTERFACE = 27
ENV_GET_SAVE_DIRECTORY = 31
ENV_GET_CORE_OPTIONS_VERSION = 52
ENV_EXPERIMENTAL = 0x10000

PIXEL_BYTES = {0: 2, 1: 4, 2: 2}
LOG_LEVELS = ("debug", "info", "warn", "error")
JOYPAD_BUTTONS = {
    "b": 0, "y": 1, "select": 2, "start": 3, "up": 4, "down": 5, "left": 6,
    "right": 7, "a": 8, "x": 9, "l": 10, "r": 11, "l2": 12, "r2": 13,
    "l3": 14, "r3": 15,
}
STRACE_LINE = re.compile(
    r'^(?:\[pid\s+)?(?P<pid>\d+)\]?\s+(?P<call>openat|open)\('
    r'(?:[^,]+,\s*)?"(?P<path>(?:[^"\\]|\\.)*)"(?:\.\.\.)?,\s*(?P<flags>[^,)]+)'
    r'.*\)\s*=\s*(?P<ret>-?\d+)(?:\s+(?P<errno>[A-Z]+))?'
)
PRINTF_SPEC = re.compile(r"%(?P<flags>[-+ #0]*)(?P<width>\*|\d+)?(?:\.(?P<prec>\*|\d+))?"
                         r"(?P<len>hh|h|ll|l|j|z|t|L)?(?P<conv>[diouxXeEfFgGaAcspn%])")


class ProbeError(RuntimeError):
    """A run that cannot produce a trustworthy result."""


# --------------------------------------------------------------------------
# Hashing and small helpers
# --------------------------------------------------------------------------

def file_digest(path: Path, algorithm: str = "sha1") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cue_tracks(cue: Path) -> list[Path]:
    """Files a .cue sheet references, in order."""
    tracks: list[Path] = []
    for line in cue.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r'\s*FILE\s+"([^"]+)"', line) or re.match(r"\s*FILE\s+(\S+)", line)
        if match:
            tracks.append(cue.parent / match.group(1))
    return tracks


def content_identity(path: Path, label: str | None) -> dict:
    tracks = [
        {"file": track.name, "sha1": file_digest(track), "size": track.stat().st_size}
        for track in (cue_tracks(path) if path.suffix.lower() == ".cue" else [])
    ]
    return {
        "label": label,
        "sha1": file_digest(path),
        "size": path.stat().st_size,
        "tracks": tracks,
    }


def describe_env_value(value: str) -> str:
    """An environment value as a record keeps it: a file by name and sha256.

    The path is a detail of the host that ran the probe, the bytes are what
    the run depended on.
    """
    path = Path(value)
    if path.is_absolute() and path.is_file():
        return f"{path.name} sha256:{file_digest(path, 'sha256')}"
    return value


def parse_assignment(text: str, flag: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"{flag} expects KEY=VALUE, got {text!r}")
    return key, value


def parse_press(text: str) -> tuple[int, int, int]:
    """FRAME:BUTTON[:LENGTH] on joypad port 0."""
    parts = text.split(":")
    if len(parts) not in (2, 3) or parts[1].lower() not in JOYPAD_BUTTONS:
        raise argparse.ArgumentTypeError(
            f"--press expects FRAME:BUTTON[:LENGTH] with BUTTON in "
            f"{', '.join(JOYPAD_BUTTONS)}, got {text!r}"
        )
    try:
        frame = int(parts[0])
        length = int(parts[2]) if len(parts) == 3 else 1
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--press {text!r}: {exc}") from exc
    if frame < 0 or length < 1:
        raise argparse.ArgumentTypeError(f"--press {text!r}: frame >= 0, length >= 1")
    return frame, JOYPAD_BUTTONS[parts[1].lower()], length


def parse_strace(text: str) -> list[dict]:
    """open/openat calls from `strace -f -e trace=openat,open` output."""
    calls: list[dict] = []
    for line in text.splitlines():
        match = STRACE_LINE.match(line.strip())
        if not match:
            continue
        ret = int(match.group("ret"))
        calls.append({
            "path": bytes(match.group("path"), "utf-8").decode("unicode_escape"),
            "flags": match.group("flags").strip(),
            "ok": ret >= 0,
            "errno": match.group("errno"),
        })
    return calls


def files_under(calls: Iterable[dict], directory: Path) -> tuple[list[str], list[str]]:
    """Relative paths opened, and probed but absent, under a directory."""
    base = os.path.realpath(directory)
    opened: dict[str, None] = {}
    missing: dict[str, None] = {}
    for call in calls:
        path = os.path.realpath(call["path"])
        if path != base and not path.startswith(base + os.sep):
            continue
        if os.path.isdir(path):
            continue
        rel = os.path.relpath(path, base)
        (opened if call["ok"] else missing)[rel] = None
    return list(opened), [rel for rel in missing if rel not in opened]


def elf_comment(path: Path) -> list[str]:
    """Strings of the ELF .comment section: the compilers that built the file."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return []
    endian = "<" if data[5] == 1 else ">"
    shoff = struct.unpack_from(endian + "Q", data, 0x28)[0]
    shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", data, 0x3A)
    sections = [struct.unpack_from(endian + "IIQQQQIIQQ", data, shoff + i * shentsize)
                for i in range(shnum)]
    if shstrndx >= len(sections):
        return []
    names_off = sections[shstrndx][4]
    for section in sections:
        name_end = data.index(b"\0", names_off + section[0])
        if data[names_off + section[0]:name_end] == b".comment":
            raw = data[section[4]:section[4] + section[5]]
            return sorted({item.decode("utf-8", "replace") for item in raw.split(b"\0") if item})
    return []


def mednafen_state_vars(state: bytes) -> dict[str, str]:
    """SECTION.VAR -> hex value from a Mednafen MDFNSVST save state."""
    start = state.find(b"MDFNSVST")
    if start < 0:
        return {}
    pos = start + 32
    out: dict[str, str] = {}
    while pos + 36 <= len(state):
        section = state[pos:pos + 32].split(b"\0", 1)[0].decode("ascii", "replace")
        size = struct.unpack_from("<I", state, pos + 32)[0]
        body = state[pos + 36:pos + 36 + size]
        pos += 36 + size
        cursor = 0
        while cursor < len(body):
            name_len = body[cursor]
            name = body[cursor + 1:cursor + 1 + name_len].decode("ascii", "replace")
            cursor += 1 + name_len
            if cursor + 4 > len(body):
                break
            value_len = struct.unpack_from("<I", body, cursor)[0] & 0x7FFFFFFF
            cursor += 4
            out[f"{section}.{name}"] = body[cursor:cursor + value_len].hex()
            cursor += value_len
    return out


def render_printf(fmt: bytes, args: Sequence[int]) -> str:
    """Render a C format string with the integer-class arguments captured.

    Floating-point arguments travel in vector registers, which a ctypes
    callback cannot see: they are shown as <double>.
    """
    text = fmt.decode("utf-8", "replace")
    queue = list(args)
    out: list[str] = []
    last = 0
    for match in PRINTF_SPEC.finditer(text):
        out.append(text[last:match.start()])
        last = match.end()
        conv = match.group("conv")
        if conv == "%":
            out.append("%")
            continue
        for star in (match.group("width"), match.group("prec")):
            if star == "*" and queue:
                queue.pop(0)
        if conv in "eEfFgGaA":
            out.append("<double>")
            continue
        value = queue.pop(0) if queue else 0
        if conv == "s":
            out.append(ctypes.string_at(value).decode("utf-8", "replace") if value else "(null)")
        elif conv == "c":
            out.append(chr(value & 0xFF))
        elif conv == "p":
            out.append(hex(value))
        elif conv in "di":
            bits = 64 if match.group("len") in ("l", "ll", "j", "z", "t") else 32
            value &= (1 << bits) - 1
            out.append(str(value - (1 << bits) if value >> (bits - 1) else value))
        else:
            bits = 64 if match.group("len") in ("l", "ll", "j", "z", "t") else 32
            value &= (1 << bits) - 1
            out.append({"o": f"{value:o}", "x": f"{value:x}", "X": f"{value:X}"}.get(conv, str(value)))
    out.append(text[last:])
    return "".join(out).rstrip("\n")


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    rows = b"".join(b"\0" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def to_rgb(fmt: int, width: int, height: int, packed: bytes) -> bytes:
    out = bytearray(width * height * 3)
    if fmt == 1:
        out[0::3], out[1::3], out[2::3] = packed[2::4], packed[1::4], packed[0::4]
        return bytes(out)
    for i, (pixel,) in enumerate(struct.iter_unpack("<H", packed)):
        if fmt == 2:
            r, g, b = (pixel >> 11) & 31, (pixel >> 5) & 63, pixel & 31
            out[i * 3:i * 3 + 3] = bytes((r << 3, g << 2, b << 3))
        else:
            r, g, b = (pixel >> 10) & 31, (pixel >> 5) & 31, pixel & 31
            out[i * 3:i * 3 + 3] = bytes((r << 3, g << 3, b << 3))
    return bytes(out)


# --------------------------------------------------------------------------
# libretro bindings (worker side)
# --------------------------------------------------------------------------

class RetroSystemInfo(ctypes.Structure):
    _fields_ = [("library_name", ctypes.c_char_p), ("library_version", ctypes.c_char_p),
                ("valid_extensions", ctypes.c_char_p), ("need_fullpath", ctypes.c_bool),
                ("block_extract", ctypes.c_bool)]


class RetroGameGeometry(ctypes.Structure):
    _fields_ = [("base_width", ctypes.c_uint), ("base_height", ctypes.c_uint),
                ("max_width", ctypes.c_uint), ("max_height", ctypes.c_uint),
                ("aspect_ratio", ctypes.c_float)]


class RetroSystemTiming(ctypes.Structure):
    _fields_ = [("fps", ctypes.c_double), ("sample_rate", ctypes.c_double)]


class RetroSystemAvInfo(ctypes.Structure):
    _fields_ = [("geometry", RetroGameGeometry), ("timing", RetroSystemTiming)]


class RetroGameInfo(ctypes.Structure):
    _fields_ = [("path", ctypes.c_char_p), ("data", ctypes.c_void_p),
                ("size", ctypes.c_size_t), ("meta", ctypes.c_char_p)]


class RetroVariable(ctypes.Structure):
    _fields_ = [("key", ctypes.c_char_p), ("value", ctypes.c_char_p)]


LOG_ARGS = 16
LogFn = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, *([ctypes.c_size_t] * LOG_ARGS))


class RetroLogCallback(ctypes.Structure):
    _fields_ = [("log", LogFn)]


EnvFn = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)
VideoFn = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t)
AudioFn = ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
AudioBatchFn = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
PollFn = ctypes.CFUNCTYPE(None)
InputFn = ctypes.CFUNCTYPE(ctypes.c_int16, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)


class Frontend:
    """The libretro callbacks for one run."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.system_dir = spec["system_dir"].encode()
        self.save_dir = spec["save_dir"].encode()
        self.options: dict[bytes, bytes] = {k.encode(): v.encode() for k, v in spec["options"].items()}
        self.defaults: dict[bytes, bytes] = {}
        self.declared: dict[str, list[str]] = {}
        self.option_buffers: list[ctypes.Array] = []
        self.pixel_format = 0
        self.frame = 0
        self.last_frame = b""
        self.last_size = (0, 0)
        self.video = hashlib.sha1()
        self.audio = hashlib.sha1()
        self.frame_hashes: list[list] = []
        self.logs: list[dict] = []
        self.presses: list[tuple[int, int, int]] = [tuple(p) for p in spec["presses"]]
        self.unanswered: dict[int, int] = {}
        self.input_queries = 0
        self.input_pressed = 0
        self.callbacks = (
            EnvFn(self.environment), VideoFn(self.video_refresh), AudioFn(self.audio_sample),
            AudioBatchFn(self.audio_batch), PollFn(lambda: None), InputFn(self.input_state),
        )
        self.log_callback = RetroLogCallback(LogFn(self.log))

    def log(self, level: int, fmt: bytes, *args: int) -> None:
        name = LOG_LEVELS[level] if 0 <= level < len(LOG_LEVELS) else str(level)
        self.logs.append({"frame": self.frame, "level": name, "text": render_printf(fmt or b"", args)})

    def environment(self, cmd: int, data: int) -> bool:
        cmd &= ~ENV_EXPERIMENTAL
        if cmd == ENV_GET_CAN_DUPE:
            ctypes.cast(data, ctypes.POINTER(ctypes.c_bool))[0] = True
            return True
        if cmd in (ENV_GET_SYSTEM_DIRECTORY, ENV_GET_SAVE_DIRECTORY):
            value = self.system_dir if cmd == ENV_GET_SYSTEM_DIRECTORY else self.save_dir
            buffer = ctypes.create_string_buffer(value)
            self.option_buffers.append(buffer)
            ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = ctypes.cast(buffer, ctypes.c_char_p)
            return True
        if cmd == ENV_SET_PIXEL_FORMAT:
            fmt = ctypes.cast(data, ctypes.POINTER(ctypes.c_int))[0]
            if fmt not in PIXEL_BYTES:
                return False
            self.pixel_format = fmt
            return True
        if cmd == ENV_SET_VARIABLES:
            array = ctypes.cast(data, ctypes.POINTER(RetroVariable))
            index = 0
            while array[index].key:
                key, desc = array[index].key, (array[index].value or b"").decode("utf-8", "replace")
                values = desc.split(";", 1)[-1].strip().split("|")
                self.declared[key.decode()] = values
                self.defaults[key] = values[0].encode()
                index += 1
            return True
        if cmd == ENV_GET_VARIABLE:
            var = ctypes.cast(data, ctypes.POINTER(RetroVariable))[0]
            value = self.options.get(var.key, self.defaults.get(var.key))
            if value is None:
                return False
            buffer = ctypes.create_string_buffer(value)
            self.option_buffers.append(buffer)
            ctypes.cast(data, ctypes.POINTER(RetroVariable))[0].value = ctypes.cast(buffer, ctypes.c_char_p)
            return True
        if cmd == ENV_GET_VARIABLE_UPDATE:
            ctypes.cast(data, ctypes.POINTER(ctypes.c_bool))[0] = False
            return True
        if cmd == ENV_SET_SUPPORT_NO_GAME:
            return True
        if cmd == ENV_GET_LOG_INTERFACE:
            ctypes.cast(data, ctypes.POINTER(RetroLogCallback))[0] = self.log_callback
            return True
        if cmd == ENV_GET_CORE_OPTIONS_VERSION:
            ctypes.cast(data, ctypes.POINTER(ctypes.c_uint))[0] = 0
            return True
        self.unanswered[cmd] = self.unanswered.get(cmd, 0) + 1
        return False

    def video_refresh(self, data: int, width: int, height: int, pitch: int) -> None:
        if data and data != ctypes.c_void_p(-1).value:
            row = width * PIXEL_BYTES[self.pixel_format]
            raw = ctypes.string_at(data, pitch * (height - 1) + row) if height else b""
            self.last_frame = b"".join(raw[y * pitch:y * pitch + row] for y in range(height))
            self.last_size = (width, height)
        self.video.update(struct.pack("<II", *self.last_size) + hashlib.sha1(self.last_frame).digest())
        every = self.spec["frame_hash_every"]
        if every and self.frame % every == 0:
            self.frame_hashes.append([self.frame, hashlib.sha1(self.last_frame).hexdigest()])
            png_dir = self.spec.get("png_dir")
            if png_dir and self.last_frame:
                rgb = to_rgb(self.pixel_format, *self.last_size, self.last_frame)
                Path(png_dir, f"frame{self.frame:07d}.png").write_bytes(png_bytes(*self.last_size, rgb))

    def audio_sample(self, left: int, right: int) -> None:
        self.audio.update(struct.pack("<hh", left, right))

    def audio_batch(self, data: int, frames: int) -> int:
        self.audio.update(ctypes.string_at(data, frames * 4))
        return frames

    def input_state(self, port: int, device: int, index: int, button: int) -> int:
        self.input_queries += 1
        if port or device != 1 or index:
            return 0
        held = int(any(start <= self.frame < start + length and button == pressed
                       for start, pressed, length in self.presses))
        self.input_pressed += held
        return held


def run_worker(spec: dict) -> dict:
    """Load the core in this process and run it. Returns the measured result."""
    core = ctypes.CDLL(spec["core"])
    front = Frontend(spec)
    for name, fn in zip(("retro_set_environment", "retro_set_video_refresh", "retro_set_audio_sample",
                         "retro_set_audio_sample_batch", "retro_set_input_poll", "retro_set_input_state"),
                        front.callbacks):
        getattr(core, name)(fn)
    info = RetroSystemInfo()
    core.retro_get_system_info(ctypes.byref(info))
    core.retro_init()
    game = None
    if spec.get("content"):
        path = spec["content"].encode()
        game = RetroGameInfo(path, None, 0, None)
        if not info.need_fullpath:
            blob = Path(spec["content"]).read_bytes()
            holder = ctypes.create_string_buffer(blob, len(blob))
            front.option_buffers.append(holder)
            game.data, game.size = ctypes.cast(holder, ctypes.c_void_p), len(blob)
    core.retro_load_game.restype = ctypes.c_bool
    if not core.retro_load_game(ctypes.byref(game) if game else None):
        return {"loaded": False, "logs": front.logs, "library_name": info.library_name.decode()}
    # A frontend plugs a joypad into each port after loading, as RetroArch does.
    for port in range(spec["joypad_ports"]):
        core.retro_set_controller_port_device(ctypes.c_uint(port), ctypes.c_uint(1))
    av = RetroSystemAvInfo()
    core.retro_get_system_av_info(ctypes.byref(av))
    warmup, frames = spec["warmup"], spec["frames"]
    for _ in range(warmup):
        core.retro_run()
        front.frame += 1
    started = time.perf_counter()
    for _ in range(frames):
        core.retro_run()
        front.frame += 1
    wall = time.perf_counter() - started
    core.retro_serialize_size.restype = ctypes.c_size_t
    size = core.retro_serialize_size()
    state = b""
    if size:
        buffer = ctypes.create_string_buffer(size)
        core.retro_serialize.restype = ctypes.c_bool
        if core.retro_serialize(buffer, ctypes.c_size_t(size)):
            state = buffer.raw
    if spec.get("state_out"):
        Path(spec["state_out"]).write_bytes(state)
    state_vars = mednafen_state_vars(state)
    observables = {
        "frame_sha1": hashlib.sha1(front.last_frame).hexdigest(),
        "video_sha1": front.video.hexdigest(),
        "audio_sha1": front.audio.hexdigest(),
        "state_sha1": hashlib.sha1(state).hexdigest() if state else None,
    }
    for name in spec["state_vars"]:
        observables[f"state:{name}"] = state_vars.get(name)
    if spec["frame_hash_every"]:
        observables["frame_hashes"] = front.frame_hashes
    core.retro_unload_game()
    core.retro_deinit()
    return {
        "loaded": True,
        "library_name": info.library_name.decode(),
        "library_version": info.library_version.decode(),
        "fps_nominal": av.timing.fps,
        "frames": frames,
        "warmup": warmup,
        "wall_s": wall,
        "observables": observables,
        "logs": front.logs,
        "options_declared": front.declared,
        "env_unanswered": {str(k): v for k, v in sorted(front.unanswered.items())},
        "last_frame_size": list(front.last_size),
        "input_queries": front.input_queries,
        "input_pressed": front.input_pressed,
    }


# --------------------------------------------------------------------------
# Host, core and frontend identity (parent side)
# --------------------------------------------------------------------------

def load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return platform.processor() or None
    return platform.processor() or None


def host_identity() -> dict:
    return {
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def frontend_identity() -> dict:
    script = Path(__file__).resolve()
    commit: str | None = None
    dirty: bool | None = None
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--", str(script)], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout
        dirty = bool(status.strip())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"WARNING: frontend commit unknown, git failed: {exc}", file=sys.stderr)
    return {"script": "scripts/core_probe.py", "commit": commit, "script_modified": dirty,
            "script_sha256": file_digest(script, "sha256")}


def core_identity(args: argparse.Namespace) -> dict:
    core = Path(args.core)
    return {
        "file": core.name,
        "sha256": file_digest(core, "sha256"),
        "repo": args.core_repo,
        "commit": args.core_commit,
        "build_command": args.build_command,
        "compiler": elf_comment(core) or None,
    }


# --------------------------------------------------------------------------
# Runs (parent side)
# --------------------------------------------------------------------------

@dataclass
class Variant:
    label: str
    files: dict[str, Path | None] = field(default_factory=dict)


def stage_system_dir(base: dict[str, Path], variant: Variant, target: Path) -> dict[str, str | None]:
    """Copy the system files of one variant. Returns dest -> sha1 (None = absent)."""
    target.mkdir(parents=True, exist_ok=True)
    staged: dict[str, str | None] = {}
    for dest, src in {**base, **variant.files}.items():
        out = target / dest
        if src is None:
            staged[dest] = None
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
        staged[dest] = file_digest(out)
    return staged


def host_paths_hidden(text: str, places: Sequence[tuple[str, str]]) -> str:
    """A core log line with the probe's own directories named, not spelled.

    Cores print the paths they open. Those paths belong to the host that ran
    the probe and would make two identical runs on two machines differ.
    """
    for prefix, name in places:
        text = text.replace(prefix, name)
    return text


def run_once(args: argparse.Namespace, system_dir: Path, scratch: Path, state_out: Path | None,
             png_dir: Path | None) -> dict:
    save_dir = scratch / "save"
    save_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "core": str(Path(args.core).resolve()),
        "content": str(Path(args.content).resolve()) if args.content else None,
        "system_dir": str(system_dir.resolve()),
        "save_dir": str(save_dir.resolve()),
        "options": dict(args.option),
        "frames": args.frames,
        "warmup": args.warmup,
        "presses": [list(p) for p in args.press],
        "state_vars": args.state_var,
        "frame_hash_every": args.frame_hash_every,
        "joypad_ports": args.joypad_ports,
        "png_dir": str(png_dir) if png_dir else None,
        "state_out": str(state_out) if state_out else None,
    }
    spec_path, out_path, trace_path = scratch / "spec.json", scratch / "result.json", scratch / "strace.txt"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "_worker", str(spec_path), str(out_path)]
    strace = shutil.which("strace") if not args.no_trace else None
    if strace:
        command = [strace, "-f", "-qq", "-s", "4096", "-e", "trace=openat,open", "-o", str(trace_path)] + command
    env = {**os.environ, **dict(args.env)}
    proc = subprocess.run(command, check=False, capture_output=True, text=True,
                          timeout=args.timeout, env=env)
    if proc.returncode != 0 or not out_path.exists():
        raise ProbeError(f"worker exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")
    result = json.loads(out_path.read_text(encoding="utf-8"))
    places = [(str(system_dir.resolve()), "<system>"), (str(save_dir.resolve()), "<save>")]
    if args.content:
        places.append((str(Path(args.content).resolve().parent), "<content>"))
    places.append((str(ROOT), "<repo>"))
    for line in result.get("logs", []):
        line["text"] = host_paths_hidden(line["text"], places)
    if strace:
        opened, probed = files_under(parse_strace(trace_path.read_text(encoding="utf-8", errors="replace")),
                                     system_dir)
        result["files_opened"] = [{"path": rel, "sha1_after": file_digest(system_dir / rel)}
                                  for rel in opened if (system_dir / rel).is_file()]
        result["files_probed_absent"] = probed
        result["tracing"] = "strace"
    else:
        result["files_opened"] = None
        result["files_probed_absent"] = None
        result["tracing"] = "unavailable: strace not found" if not args.no_trace else "disabled: --no-trace"
    if result.get("wall_s") and result.get("frames"):
        result["ms_per_frame"] = result["wall_s"] * 1000 / result["frames"]
        result["fps"] = result["frames"] / result["wall_s"]
    return result


def timing_stats(values: Sequence[float]) -> dict:
    return {
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "raw": list(values),
    }


def run_variant(args: argparse.Namespace, base: dict[str, Path], variant: Variant,
                work: Path, repetitions: int) -> dict:
    system_dir = work / "system"
    staged = stage_system_dir(base, variant, system_dir)
    reps: list[dict] = []
    flags: list[str] = []
    for index in range(repetitions):
        scratch = work / f"rep{index}"
        scratch.mkdir(parents=True, exist_ok=True)
        png_dir = None
        if args.png_dir and index == 0:
            png_dir = Path(args.png_dir) / variant.label
            png_dir.mkdir(parents=True, exist_ok=True)
        state_out = Path(args.state_dir) / f"{variant.label}.state" if args.state_dir and index == 0 else None
        if state_out:
            state_out.parent.mkdir(parents=True, exist_ok=True)
        rep = run_once(args, system_dir, scratch, state_out, png_dir)
        if not rep.get("loaded"):
            flags.append(f"repetition {index}: retro_load_game returned false")
        for entry in rep.get("files_opened") or []:
            expected = staged.get(entry["path"])
            entry["sha1_staged"] = expected
            if expected is not None and expected != entry["sha1_after"]:
                flags.append(f"repetition {index}: {entry['path']} sha1 changed during the run")
        reps.append(rep)
    reference = reps[0].get("observables")
    for index, rep in enumerate(reps[1:], 1):
        if rep.get("observables") != reference:
            differing = sorted(k for k in (reference or {}) if (rep.get("observables") or {}).get(k) != reference[k])
            flags.append(f"repetition {index} disagrees with repetition 0 on {', '.join(differing)}")
    return {
        "label": variant.label,
        "system_files": {dest: sha for dest, sha in sorted(staged.items())},
        "repetitions": reps,
        "flags": flags,
    }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     fromfile_prefix_chars="@")
    sub = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False, fromfile_prefix_chars="@")
    common.add_argument("--core", required=True, help="libretro .so to load")
    common.add_argument("--content", help="content to load (a .cue is identified with its tracks)")
    common.add_argument("--content-label", help="identity of the content, e.g. its Redump name")
    common.add_argument("--system-file", action="append", default=[], metavar="DEST=SRC",
                        type=lambda t: parse_assignment(t, "--system-file"),
                        help="copy SRC into the staged system dir as DEST")
    common.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                        type=lambda t: parse_assignment(t, "--option"), help="core option")
    common.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        type=lambda t: parse_assignment(t, "--env"),
                        help="environment variable for the core process, e.g. LD_PRELOAD of a fixed clock")
    common.add_argument("--frames", type=int, default=600, help="frames timed after warm-up")
    common.add_argument("--warmup", type=int, default=0, help="frames run before timing starts")
    common.add_argument("--press", action="append", default=[], type=parse_press,
                        metavar="FRAME:BUTTON[:LEN]", help="hold a joypad button on port 0")
    common.add_argument("--joypad-ports", type=int, default=2, metavar="N",
                        help="ports given a joypad after loading (default 2)")
    common.add_argument("--state-var", action="append", default=[], metavar="SECTION.VAR",
                        help="report a Mednafen save-state variable as an observable")
    common.add_argument("--frame-hash-every", type=int, default=0, metavar="K",
                        help="record the sha1 of every K-th frame")
    common.add_argument("--png-dir", help="write the frames sampled by --frame-hash-every as PNG")
    common.add_argument("--state-dir", help="write the save state at the end of the first repetition")
    common.add_argument("--core-repo", help="source repository URL of the core build")
    common.add_argument("--core-commit", help="commit the core was built from")
    common.add_argument("--build-command", help="command and flags that built the core")
    common.add_argument("--record", metavar="DIR", help="write one JSON record under DIR/<core>/")
    common.add_argument("--max-load", type=float, default=None,
                        help="refuse to record when the 1-minute load average exceeds this "
                             "(default: a quarter of the CPU count)")
    common.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR), help="scratch directory")
    common.add_argument("--timeout", type=int, default=1800, help="seconds per repetition")
    common.add_argument("--no-trace", action="store_true", help="do not run the worker under strace")

    sub.add_parser("run", parents=[common], help="one execution")
    bench = sub.add_parser("bench", parents=[common], help="timed repetitions")
    bench.add_argument("--repeat", type=int, default=MIN_BENCH_REPETITIONS,
                       help=f"timed repetitions, each in a fresh process (at least {MIN_BENCH_REPETITIONS} to record)")
    check = sub.add_parser("check", parents=[common], help="compare variants of a system file")
    check.add_argument("--vary", action="append", required=True, metavar="DEST=SRC",
                       type=lambda t: parse_assignment(t, "--vary"),
                       help="one variant per occurrence; an empty SRC leaves DEST absent")
    check.add_argument("--observable", required=True,
                       help="the observable compared across variants")
    check.add_argument("--claim", required=True, help="the statement under test")
    check.add_argument("--method", required=True, help="how the run tests it")
    check.add_argument("--repeat", type=int, default=2, help="runs of each variant, each in a fresh process")
    worker = sub.add_parser("_worker", help="internal: one run in this process, started by the others")
    worker.add_argument("spec", help="JSON file describing the run")
    worker.add_argument("out", help="JSON file the result is written to")
    return parser


def validate_args(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if not Path(args.core).is_file():
        problems.append(f"--core {args.core}: not a file")
    if args.content and not Path(args.content).is_file():
        problems.append(f"--content {args.content}: not a file")
    for dest, src in args.system_file:
        if not Path(src).is_file():
            problems.append(f"--system-file {dest}={src}: not a file")
    if args.frames < 1 or args.warmup < 0:
        problems.append("--frames must be >= 1 and --warmup >= 0")
    if args.png_dir and not args.frame_hash_every:
        problems.append("--png-dir needs --frame-hash-every")
    if args.mode == "bench" and args.record and args.repeat < MIN_BENCH_REPETITIONS:
        problems.append(f"a recorded benchmark needs --repeat >= {MIN_BENCH_REPETITIONS}")
    if args.mode == "check":
        dests = {dest for dest, _ in args.vary}
        if len(dests) != 1:
            problems.append("--vary must name the same DEST in every occurrence")
        for dest, src in args.vary:
            if src and not Path(src).is_file():
                problems.append(f"--vary {dest}={src}: not a file")
        names = set(BASE_OBSERVABLES) | {f"state:{v}" for v in args.state_var}
        if args.frame_hash_every:
            names.add("frame_hashes")
        if args.observable not in names:
            problems.append(f"--observable {args.observable}: not one of {', '.join(sorted(names))}")
    return problems


def variants_for(args: argparse.Namespace) -> list[Variant]:
    if args.mode != "check":
        return [Variant("default")]
    out: list[Variant] = []
    seen: set[str] = set()
    for dest, src in args.vary:
        label = Path(src).name if src else f"{dest}-absent"
        while label in seen:
            label += "+"
        seen.add(label)
        out.append(Variant(label, {dest: Path(src) if src else None}))
    return out


def unsettled(variant: dict, observable: str) -> bool:
    """True when a variant cannot vouch for one value of the observable.

    A repetition that did not load, a file changed during the run, or two
    repetitions that disagree on this observable. A disagreement on another
    observable stays in the variant's flags without voiding this one.
    """
    reps = variant["repetitions"]
    if any(not rep.get("loaded") for rep in reps):
        return True
    if any("changed during the run" in flag for flag in variant["flags"]):
        return True
    values = {json.dumps((rep.get("observables") or {}).get(observable), sort_keys=True) for rep in reps}
    return len(values) > 1


def check_result(variants: list[dict], observable: str) -> dict:
    groups: dict[str, list[str]] = {}
    failed = [v["label"] for v in variants if unsettled(v, observable)]
    for variant in variants:
        value = (variant["repetitions"][0].get("observables") or {}).get(observable)
        groups.setdefault(json.dumps(value, sort_keys=True), []).append(variant["label"])
    if failed:
        status = "inconclusive"
        statement = (f"{observable} is not settled in {len(failed)} of {len(variants)} variants "
                     f"({', '.join(failed)}): no comparison is drawn")
    elif len(groups) == 1:
        status = "same"
        statement = f"{observable} is identical across all {len(variants)} variants"
    else:
        status = "differs"
        statement = f"{observable} takes {len(groups)} distinct values across {len(variants)} variants"
    other = sorted({v["label"] for v in variants if v["flags"]} - set(failed))
    if other:
        statement += f". Other flags, in {len(other)} variants, are listed with them"
    return {
        "status": status,
        "statement": statement,
        "groups": [{"value": json.loads(key), "variants": labels} for key, labels in groups.items()],
    }


def record_path(directory: Path, core_name: str, record: dict) -> Path:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()[:12]
    date = record["recorded_at"][:10]
    slug = re.sub(r"[^a-z0-9]+", "_", core_name.lower()).strip("_") or "core"
    return directory / slug / f"{date}-{digest}.json"


def probe(args: argparse.Namespace) -> int:
    problems = validate_args(args)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 2
    max_load = args.max_load if args.max_load is not None else (os.cpu_count() or 1) / 4
    load_before = load_average()
    if args.record and load_before and load_before[0] > max_load:
        print(f"ERROR: load average {load_before[0]} exceeds {max_load}: the machine is busy, "
              f"nothing recorded (raise --max-load to override)", file=sys.stderr)
        return 3
    repetitions = 1 if args.mode == "run" else args.repeat
    base = {dest: Path(src) for dest, src in args.system_file}
    work = Path(tempfile.mkdtemp(prefix=f"{args.mode}-", dir=_ensure(Path(args.work_dir))))
    try:
        variants = [run_variant(args, base, variant, work / f"v{i}", repetitions)
                    for i, variant in enumerate(variants_for(args))]
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    load_after = load_average()
    first = variants[0]["repetitions"][0]
    record: dict = {
        "record_version": RECORD_VERSION,
        "kind": args.mode,
        "recorded_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "core": {**core_identity(args), "library_name": first.get("library_name"),
                 "library_version": first.get("library_version")},
        "frontend": frontend_identity(),
        "host": {**host_identity(), "load_before": load_before, "load_after": load_after,
                 "max_load": max_load},
        "inputs": {
            "content": content_identity(Path(args.content), args.content_label) if args.content else None,
            "options": dict(args.option),
            "env": {key: describe_env_value(value) for key, value in args.env},
            "frames": args.frames,
            "warmup": args.warmup,
            "repetitions": repetitions,
            "presses": [{"frame": f, "button": b, "length": n} for f, b, n in args.press],
            "observables": list(BASE_OBSERVABLES) + [f"state:{v}" for v in args.state_var]
                           + (["frame_hashes"] if args.frame_hash_every else []),
            "frame_hash_every": args.frame_hash_every,
            "joypad_ports": args.joypad_ports,
        },
        "variants": variants,
        "flags": [f"{v['label']}: {flag}" for v in variants for flag in v["flags"]],
    }
    if load_before and load_after and max(load_before[0], load_after[0]) > max_load:
        record["flags"].append(f"load average reached {max(load_before[0], load_after[0])} "
                               f"(threshold {max_load})")
    if args.mode == "bench":
        reps = variants[0]["repetitions"]
        record["timing"] = {"ms_per_frame": timing_stats([r["ms_per_frame"] for r in reps if "ms_per_frame" in r]),
                            "fps": timing_stats([r["fps"] for r in reps if "fps" in r])}
    if args.mode == "check":
        record["check"] = {"claim": args.claim, "method": args.method, "observable": args.observable,
                           "variable": args.vary[0][0],
                           "result": check_result(variants, args.observable)}
    text = json.dumps(record, indent=2, sort_keys=False)
    if args.record:
        path = record_path(Path(args.record), first.get("library_name") or Path(args.core).stem, record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"recorded {path}", file=sys.stderr)
    print(text)
    return 0


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "_worker":
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        Path(args.out).write_text(json.dumps(run_worker(spec)), encoding="utf-8")
        return 0
    return probe(args)


if __name__ == "__main__":
    # The worker runs under -I, which leaves the script directory off sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import run_cli

    run_cli(main)
