"""The handheld launchers: their scripts, controller mappings and archives,
and what each one does on a simulated device.

A device is simulated with a pseudo-terminal standing for its console, a
fake install.py that records how it was started, and fakes for what the
firmware provides (gptokeyb, setfont, chvt, curl, foot, xterm, muxterm,
PortMaster's control.txt and muOS's retro.sh), all on PATH or in a folder
the launcher reads. What only a device shows (the text reaching the screen,
the pad reaching the console, a compositor covering it) is not exercised.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import zipfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTS = REPO_ROOT / "ports"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_port  # noqa: E402

_spec = importlib.util.spec_from_file_location("install", REPO_ROOT / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

SCRIPTS = [
    PORTS / "shared" / "tui.txt",
    PORTS / "RetroBIOS" / "RetroBIOS.sh",
    PORTS / "RetroBIOS" / "retrobios" / "screens.txt",
    PORTS / "batocera" / "RetroBIOS.sh",
    PORTS / "muos" / "mux_launch.sh",
    PORTS / "tools" / "RetroBIOS.sh",
]

# What each button does in the interface (wiki/installer.md, the full-screen
# interface): the convergent menu mapping of handheld_runtime.md.
ROLES = {
    "a": "enter", "start": "enter", "b": "esc", "back": "esc", "x": "space",
    "y": "tab", "l1": "pageup", "r1": "pagedown",
    "up": "up", "down": "down", "left": "left", "right": "right",
}
# What a Linux console or a terminal sends for each key gptokeyb and evmapy
# emit, and the key the interface reads from it.
SEQUENCES = {
    "enter": "\r", "esc": "\x1b", "space": " ", "tab": "\t",
    "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "up": "\x1b[A", "down": "\x1b[B", "left": "\x1b[D", "right": "\x1b[C",
}
INTERFACE_KEYS = {
    "enter": "enter", "esc": "esc", "space": "char", "tab": "tab",
    "pageup": "pgup", "pagedown": "pgdn",
    "up": "up", "down": "down", "left": "left", "right": "right",
}
# gptokeyb's char_to_keycode (PortsMaster/gptokeyb src/util.cpp) names.
GPTOKEYB_NAMES = set(SEQUENCES) | {"backspace", '\\"'}


def _tempdir(test: unittest.TestCase) -> Path:
    scratch = REPO_ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(scratch)))
    test.addCleanup(shutil.rmtree, path, True)
    return path


def read_gptk(path: Path) -> dict[str, list[str]]:
    """key -> values, as gptokeyb's parseConfigFile reads `key = value`."""
    mapping: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, sep, value = line.partition(" = ")
        assert sep, line
        mapping.setdefault(key, []).append(value.split("#")[0].strip())
    return mapping


class EveryScriptParses(unittest.TestCase):
    def test_bash_n(self):
        for script in SCRIPTS:
            with self.subTest(script=str(script.relative_to(REPO_ROOT))):
                proc = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                                      text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_shellcheck(self):
        """CI provides shellcheck (validate.yml checks it), so there it may
        not skip."""
        if shutil.which("shellcheck") is None:
            if os.environ.get("CI"):
                self.fail("CI must provide shellcheck for the handheld launchers")
            self.skipTest("shellcheck is not installed")
        proc = subprocess.run(
            ["shellcheck", "--shell=bash", "--severity=style", "--external-sources",
             "--source-path=SCRIPTDIR", *map(str, SCRIPTS)],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_launchers_are_executable(self):
        for script in (PORTS / "RetroBIOS" / "RetroBIOS.sh", PORTS / "batocera" / "RetroBIOS.sh",
                       PORTS / "muos" / "mux_launch.sh", PORTS / "tools" / "RetroBIOS.sh"):
            with self.subTest(script=str(script.relative_to(REPO_ROOT))):
                self.assertTrue(script.stat().st_mode & 0o111)
                self.assertTrue(script.read_text().startswith("#!/bin/bash\n"))

    def test_muos_headers(self):
        """muOS's frontend reads the HELP, ICON and GRID lines of an app
        (MustardOS internal share/application/*/mux_launch.sh)."""
        head = (PORTS / "muos" / "mux_launch.sh").read_text().splitlines()[1:4]
        self.assertEqual([line.split(":")[0] for line in head], ["# HELP", "# ICON", "# GRID"])
        self.assertTrue(all(line.split(":", 1)[1].strip() for line in head))


class ControllerMappings(unittest.TestCase):
    def _decodes(self, key: str) -> str:
        decoder = install.KeyDecoder()
        keys = decoder.feed(SEQUENCES[key]) + decoder.flush()
        self.assertEqual(len(keys), 1, key)
        return keys[0].name

    def test_gptk_maps_every_button_the_interface_reads(self):
        mapping = read_gptk(PORTS / "shared" / "retrobios.gptk")
        for button, key in ROLES.items():
            with self.subTest(button=button):
                self.assertEqual(mapping[button][0], key)
                self.assertEqual(self._decodes(key), INTERFACE_KEYS[key])
        for values in mapping.values():
            self.assertIn(values[0], GPTOKEYB_NAMES | {"repeat"})
            self.assertTrue(set(values[1:]) <= {"repeat"})
        self.assertEqual(mapping["up"], ["up", "repeat"])
        self.assertEqual(mapping["down"], ["down", "repeat"])
        self.assertEqual(mapping["guide"], ['\\"'])

    def test_evmapy_keys_map_the_same_buttons(self):
        """Batocera's evmapy names L1 and R1 pageup and pagedown, and Select
        select (batocera-pygame evmapy.keys)."""
        data = json.loads((PORTS / "batocera" / "RetroBIOS.sh.keys").read_text())
        actions = {a["trigger"]: a for a in data["actions_player1"]}
        triggers = {"a": "a", "start": "start", "b": "b", "back": "select", "x": "x",
                    "y": "y", "l1": "pageup", "r1": "pagedown", "up": "up",
                    "down": "down", "left": "left", "right": "right"}
        for button, trigger in triggers.items():
            with self.subTest(button=button):
                action = actions[trigger]
                self.assertEqual(action["type"], "key")
                self.assertEqual(action["target"], [f"KEY_{ROLES[button].upper()}"])
        for stick in ("up", "down", "left", "right"):
            self.assertEqual(actions[f"joystick1{stick}"]["target"], [f"KEY_{stick.upper()}"])
        self.assertEqual(len(actions), len(data["actions_player1"]))

    def test_x_ticks_and_y_opens_the_options_on_every_route(self):
        """The interface reads Space as Tick and Tab as Options: a mapping
        that sends Y as Space activates the focused row instead."""
        self.assertEqual(self._decodes("space"), "char")
        self.assertEqual(self._decodes("tab"), "tab")
        gptk = read_gptk(PORTS / "shared" / "retrobios.gptk")
        self.assertEqual((gptk["x"], gptk["y"]), (["space"], ["tab"]))
        keys = json.loads((PORTS / "batocera" / "RetroBIOS.sh.keys").read_text())
        targets = {a["trigger"]: a["target"] for a in keys["actions_player1"]}
        self.assertEqual((targets["x"], targets["y"]), (["KEY_SPACE"], ["KEY_TAB"]))
        # muxterm sends Y as Space on its own (MustardOS/terminal input.c):
        # where gptokeyb runs beside it, muxterm is told to leave the pad.
        for launcher in (PORTS / "muos" / "mux_launch.sh", PORTS / "RetroBIOS" / "RetroBIOS.sh"):
            text = launcher.read_text()
            self.assertIn("SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT=0x0001/0x0001", text)
        self.assertFalse((PORTS / "muos" / "muxterm.gptk").exists())


class TheArchives(unittest.TestCase):
    LAYOUT = {
        build_port.PORT_ZIP: ("RetroBIOS.sh", "retrobios"),
        build_port.BATOCERA_ZIP: ("RetroBIOS.sh", "retrobios"),
        build_port.MUOS_APP: (None, "RetroBIOS"),
        build_port.TOOLS_ZIP: ("RetroBIOS.sh", "retrobios"),
    }

    def setUp(self):
        self.out = _tempdir(self)

    def test_every_release_archive_is_built(self):
        self.assertEqual(set(build_port.ARCHIVES), set(self.LAYOUT))

    def test_layout(self):
        for name, (script, folder) in self.LAYOUT.items():
            with self.subTest(archive=name):
                with zipfile.ZipFile(build_port.build_archive(name, self.out / name)) as zf:
                    names = zf.namelist()
                    modes = {n: zf.getinfo(n).external_attr >> 16 for n in names}
                self.assertEqual(names, sorted(names))
                top = [n for n in names if "/" not in n]
                self.assertEqual({n.split("/")[0] for n in names if "/" in n}, {folder})
                for member in ("install.py", "tui.txt", "rbevents.py",
                               "licenses/LICENSE-retrobios.txt"):
                    self.assertIn(f"{folder}/{member}", names)
                launcher = script or f"{folder}/mux_launch.sh"
                self.assertTrue(modes[launcher] & 0o111, launcher)
                if name == build_port.BATOCERA_ZIP:
                    self.assertEqual(top, ["RetroBIOS.sh", "RetroBIOS.sh.keys"])
                    self.assertNotIn("retrobios/retrobios.gptk", names)
                elif script:
                    self.assertEqual(top, [script])
                    self.assertIn(f"{folder}/retrobios.gptk", names)
                else:
                    self.assertEqual(top, [])
                    self.assertIn("RetroBIOS/retrobios.gptk", names)

    def test_each_carries_the_pinned_installer(self):
        pinned = build_port.pinned_installer_sha256()
        for name, (_script, folder) in self.LAYOUT.items():
            with self.subTest(archive=name):
                with zipfile.ZipFile(build_port.build_archive(name, self.out / name)) as zf:
                    bundled = hashlib.sha256(zf.read(f"{folder}/install.py")).hexdigest()
                self.assertEqual(bundled, pinned)

    def test_reproducible(self):
        for name in build_port.ARCHIVES:
            with self.subTest(archive=name):
                first = build_port.build_archive(name, self.out / f"a-{name}").read_bytes()
                second = build_port.build_archive(name, self.out / f"b-{name}").read_bytes()
                self.assertEqual(first, second)

    def test_each_names_the_release_packs(self):
        """packs.txt names the platforms a release publishes a pack for, as
        the registry marks them, and the pack's name for each."""
        registry = yaml.safe_load((REPO_ROOT / "platforms" / "_registry.yml").read_text())
        released = {name for name, entry in registry["platforms"].items()
                    if entry.get("release_pack") is True}
        labels = build_port.release_pack_labels()
        self.assertEqual(set(labels), released)
        self.assertEqual(labels["retroarch"], "RetroArch")
        expected = "".join(f"{p}\t{label}\n" for p, label in sorted(labels.items()))
        for name, (_script, folder) in self.LAYOUT.items():
            with self.subTest(archive=name):
                with zipfile.ZipFile(build_port.build_archive(name, self.out / name)) as zf:
                    self.assertEqual(zf.read(f"{folder}/packs.txt").decode(), expected)

    def test_an_unpinned_installer_is_refused(self):
        root = _tempdir(self)
        for name in ("install.py", "install.sh", "LICENSE", "NOTICE"):
            shutil.copy(REPO_ROOT / name, root / name)
        shutil.copytree(PORTS, root / "ports")
        with (root / "install.py").open("a") as fh:
            fh.write("\n")
        for name in build_port.ARCHIVES:
            with self.subTest(archive=name), self.assertRaises(ValueError):
                build_port.build_archive(name, self.out / name, root)

    def test_the_release_process_attaches_each(self):
        text = (REPO_ROOT / "wiki" / "release-process.md").read_text(encoding="utf-8")
        for name in build_port.ARCHIVES:
            self.assertIn(name, text)


FAKE_INSTALL = r'''
import json, os, signal, subprocess, sys, time
calls = os.environ["FAKE_CALLS"]
state = os.environ["FAKE_STATE"]


def tty(fd):
    try:
        return os.ttyname(fd)
    except OSError:
        return ""


def note(name, text=""):
    with open(os.path.join(state, name), "a") as fh:
        fh.write(text + "\n")


if "--engine" in sys.argv:
    # The engine the interface starts in its own session: it writes its
    # summary on SIGINT, as install.py does, and records any other signal.
    def summary(signum, _frame):
        note("engine.log", "summary after SIGINT")
        sys.exit(130)

    def other(signum, _frame):
        note("engine.log", f"signal {signum}")
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, summary)
    signal.signal(signal.SIGTERM, other)
    note("engine.ready")
    time.sleep(60)
    sys.exit(0)

with open(calls, "a") as fh:
    fh.write(json.dumps({"argv0": sys.argv[0], "args": sys.argv[1:], "stdin": tty(0),
                         "stdout": tty(1), "term": os.environ.get("TERM", ""),
                         "pythonhome": os.environ.get("PYTHONHOME", ""),
                         "base_url": os.environ.get("RETROBIOS_BASE_URL", "")}) + "\n")
if "--check" in sys.argv:
    if os.environ.get("FAKE_DETECT_HANG"):
        time.sleep(60)
    print(json.dumps({"event": "detect", "os": "linux", "os_name": "Fake OS",
                      "platforms": [{"platform": os.environ.get("FAKE_PLATFORM", "retroarch"),
                                     "name": os.environ.get("FAKE_PLATFORM_NAME", "RetroArch"),
                                     "bios_dir": "/roms/bios"}]}))
    print(json.dumps({"event": "error", "stage": "manifest", "reason": "network",
                      "platform": os.environ.get("FAKE_PLATFORM", "retroarch"),
                      "message": "unreachable"}))
    print(json.dumps({"event": "end", "code": 1}))
    sys.exit(1)
if os.environ.get("FAKE_REFUSE"):
    print("install.py: error: --tui cannot open the full-screen interface here: "
          "the terminal is 30x10, smaller than 40x12", file=sys.stderr)
    sys.exit(2)
if os.environ.get("FAKE_RAW") and os.isatty(0):
    import tty as _tty
    _tty.setraw(0)
if os.environ.get("FAKE_HOLD"):
    # The interface up and waiting for a key until a signal ends it. With
    # FAKE_ENGINE an install runs, and SIGTERM gives it 2 s to say what it
    # wrote, as install.py's run_tui does. FAKE_NO_HANDLER is a signal that
    # comes before run_tui installs its handlers.
    engine = None
    if os.environ.get("FAKE_ENGINE"):
        engine = subprocess.Popen([sys.executable, sys.argv[0], "--events", "--engine"],
                                  start_new_session=True)
        while not os.path.exists(os.path.join(state, "engine.ready")):
            time.sleep(0.05)

    def stop(signum, _frame):
        note("ui.log", f"signal {signum}")
        if engine is not None:
            engine.send_signal(signal.SIGINT)
            try:
                engine.wait(2)
            except subprocess.TimeoutExpired:
                engine.kill()
            print("RetroBIOS: the install was stopped, 2 files written.")
        if os.environ.get("FAKE_TERM_ERROR"):
            print("Error: " + os.environ["FAKE_TERM_ERROR"], file=sys.stderr)
        sys.exit(128 + signum)

    if not os.environ.get("FAKE_NO_HANDLER"):
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGHUP, stop)
    note("ui.ready")
    time.sleep(60)
    sys.exit(0)
if os.environ.get("FAKE_ERROR"):
    print("Error: " + os.environ["FAKE_ERROR"], file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_CODE", "0")))
'''

# The real pgrep and pkill: Device puts a pgrep on PATH that answers for the
# compositor check.
PGREP = shutil.which("pgrep", path="/usr/bin:/bin") or "pgrep"
PKILL = shutil.which("pkill", path="/usr/bin:/bin") or "pkill"

# gptokeyb as PortsMaster/gptokeyb runs it: kill mode takes the argument
# after the flag (`-1`, `-k` or `-sudokill`) as its target. Start + Select (FAKE_PRESS, once the
# interface is up) is doKillMode in src/util.cpp: pkill -f on the target,
# which also matches gptokeyb's own command line (SDL turns that SIGTERM
# into an event it does not read there), 3 s, then pkill -9 -f and exit.
# Stopped otherwise by a SIGTERM, which it records. FAKE_MAPPER_DIES is a
# mapper that cannot start (no /dev/uinput).
FAKE_MAPPER = f'''#!/bin/bash
printf '%s\\n' "$@" > "$FAKE_STATE/mapper.args"
echo $$ > "$FAKE_STATE/mapper.pid"
printf '%s\\n' "${{SDL_GAMECONTROLLERCONFIG_FILE:-}}" > "$FAKE_STATE/mapper.db"
if [ -n "${{FAKE_MAPPER_DIES:-}}" ]; then
  echo "gptokeyb: cannot open /dev/uinput" >&2
  exit 1
fi
name=""
while [ $# -gt 0 ]; do
  case "$1" in
    -1|-k|-sudokill) name="$2"; shift 2 ;;
    -c) shift 2 ;;
    *) shift ;;
  esac
done
killing=""
trap '[ -n "$killing" ] || {{ echo stopped > "$FAKE_STATE/mapper.stopped"; exit 0; }}' TERM
until [ -n "${{FAKE_PRESS:-}}" ] && [ -e "$FAKE_STATE/ui.ready" ]; do sleep 0.1; done
killing=1
echo "kill $name" >> "$FAKE_STATE/mapper.log"
{PKILL} -f "$name"
sleep 3
if {PGREP} -f "$name" > /dev/null; then {PKILL} -9 -f "$name"; fi
exit 0
'''

# gptokeyb2 as PortsMaster/gptokeyb2 src/util.c kills: SIGKILL only, by
# process name (pkill -9 NAME) unless -X, then pgrep -f and kill -9 on every
# match but its own ancestry and children. It quits after a kill, and keeps
# running when it has no process name.
FAKE_GPTOKEYB2 = f'''#!/bin/bash
printf '%s\\n' "$@" > "$FAKE_STATE/mapper.args"
echo $$ > "$FAKE_STATE/mapper.pid"
want_kill="" name=""
while [ $# -gt 0 ]; do
  case "$1" in
    -X) want_kill=1; shift ;;
    -c) shift 2 ;;
    -*) shift ;;
    *) [ -z "$name" ] && name="$1"; shift ;;
  esac
done
trap 'echo stopped > "$FAKE_STATE/mapper.stopped"; exit 0' TERM
until [ -n "${{FAKE_PRESS:-}}" ] && [ -e "$FAKE_STATE/ui.ready" ]; do sleep 0.1; done
echo "kill $name" >> "$FAKE_STATE/mapper.log"
if [ -z "$name" ]; then
  while :; do sleep 0.1; done
fi
if [ -n "$want_kill" ]; then
  ours=" $$ "
  p=$$
  while [ "$p" -gt 1 ]; do
    p=$(awk '{{print $4}}' "/proc/$p/stat" 2>/dev/null)
    [ -n "$p" ] || break
    ours="$ours$p "
  done
  for pid in $({PGREP} -f "$name"); do
    case "$ours" in *" $pid "*) continue ;; esac
    [ "$(awk '{{print $4}}' "/proc/$pid/stat" 2>/dev/null)" = "$$" ] && continue
    kill -9 "$pid" 2>/dev/null
  done
else
  {PKILL} -9 "$name"
fi
exit 0
'''

FAKE_RECORDER = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_STATE/tools.log"
"""

# chvt brings the console to the front, so RB_VT_ACTIVE then names it. A
# compositor that keeps the display (FAKE_CHVT_REFUSED) leaves chvt waiting
# in VT_WAITACTIVE and the console in front unchanged.
FAKE_CHVT = """#!/bin/sh
printf '%s\\n' "chvt $*" >> "$FAKE_STATE/tools.log"
[ -n "$FAKE_CHVT_REFUSED" ] && exec sleep 30
[ -n "$RB_VT_ACTIVE" ] && printf 'tty%s\\n' "$1" > "$RB_VT_ACTIVE"
exit 0
"""

FAKE_SETFONT = """#!/bin/sh
printf '%s\\n' "setfont $*" >> "$FAKE_STATE/tools.log"
while [ $# -gt 0 ]; do
  [ "$1" = "-O" ] && { [ -n "$FAKE_NO_SAVE" ] && exit 1; printf 'font' > "$2"; }
  shift
done
"""

# A terminal app: records how it was started and the SDL hint it was given,
# then runs its command. FAKE_TERM_FAILS is one that cannot open its window.
FAKE_TERMAPP = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_STATE/tools.log"
printf '%s\\n' "${SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT:-}" > "$FAKE_STATE/termapp.sdl"
if [ -n "$FAKE_TERM_FAILS" ]; then
  echo "$(basename "$0"): Can't open display" >&2
  exit 1
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --|-e) shift; break ;;
    *) shift ;;
  esac
done
exec "$@"
"""

# sudo with env_reset: the environment is dropped but for the variables
# --preserve-env names.
FAKE_SUDO = """#!/bin/bash
printf '%s\\n' "sudo $*" >> "$FAKE_STATE/tools.log"
keep=""
while [ $# -gt 0 ]; do
  case "$1" in
    --preserve-env=*) keep="${1#--preserve-env=}"; shift ;;
    -*) shift ;;
    *) break ;;
  esac
done
case ",$keep," in
  *,SDL_GAMECONTROLLERCONFIG_FILE,*) ;;
  *) unset SDL_GAMECONTROLLERCONFIG_FILE ;;
esac
exec "$@"
"""


class Device:
    """A simulated handheld: fake tools on PATH and a pty as its console."""

    def __init__(self, test: unittest.TestCase, **env: str):
        self.test = test
        self.root = _tempdir(test)
        self.state = self.root / "state"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        master, slave = pty.openpty()
        test.addCleanup(os.close, master)
        test.addCleanup(os.close, slave)
        termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(slave))
        import fcntl  # noqa: PLC0415
        # Wide enough that no path in a message is folded.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 250, 0, 0))
        self.master, self.slave = master, slave
        self.console = os.ttyname(slave)
        self.before = termios.tcgetattr(slave)
        (self.bin / "python3").symlink_to(shutil.which("python3.8") or sys.executable)
        self.tool("gptokeyb", FAKE_MAPPER)
        self.tool("setfont", FAKE_SETFONT)
        self.tool("chvt", FAKE_CHVT)
        # A probe that cannot connect is offline (curl exit 7).
        self.tool("curl", '#!/bin/sh\nexit "${FAKE_CURL:-0}"\n')
        self.tool("pgrep", '#!/bin/sh\nexit "${FAKE_PGREP:-1}"\n')
        fonts = self.root / "fonts"
        fonts.mkdir()
        for name in ("Lat7-TerminusBold32x16", "Lat7-TerminusBold22x11",
                     "Lat7-TerminusBold20x10", "Lat7-Terminus16"):
            (fonts / f"{name}.psf.gz").write_bytes(b"")
        (self.root / "fb").write_text("640,480\n")
        environ = {k: v for k, v in os.environ.items()
                   if k not in ("DISPLAY", "WAYLAND_DISPLAY", "RETROBIOS_BASE_URL",
                                "XAUTHORITY", "XDG_RUNTIME_DIR")}
        self.env = {
            **environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "FAKE_STATE": str(self.state),
            "FAKE_CALLS": str(self.state / "calls.jsonl"),
            "RB_FONTS": str(fonts),
            "RB_FB": str(self.root / "fb"),
            "RB_MESSAGE_WAIT": "1",
            "TERM": "dumb",
            **env,
        }
        test.addCleanup(self._reap)

    def tool(self, name: str, text: str) -> Path:
        path = self.bin / name
        # python3 starts as a link to the real interpreter: never write through it.
        if path.is_symlink() or path.exists():
            path.unlink()
        path.write_text(text)
        path.chmod(0o755)
        return path

    def unpack(self, archive: str, into: Path) -> Path:
        """The launcher archive extracted where the firmware keeps it, with
        the fake installer in place of install.py."""
        into.mkdir(parents=True, exist_ok=True)
        built = build_port.build_archive(archive, self.root / "dist" / archive)
        with zipfile.ZipFile(built) as zf:
            zf.extractall(into)
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if mode:
                    (into / info.filename).chmod(mode & 0o777)
        for found in into.rglob("install.py"):
            found.write_text(FAKE_INSTALL)
        return into

    def run(self, argv: list[str], stdio_tty: bool = False, keys: str = "", **env: str):
        """Run a launcher with *keys* waiting on the console as buttons
        pressed. Its console output is read from the pty."""
        if keys:
            os.write(self.master, keys.encode())
        stdin = self.slave if stdio_tty else subprocess.DEVNULL
        stdout = self.slave if stdio_tty else subprocess.PIPE
        start = time.monotonic()
        proc = subprocess.run(argv, env={**self.env, **env}, stdin=stdin, stdout=stdout,
                              stderr=subprocess.PIPE, timeout=120, check=False)
        self.elapsed = time.monotonic() - start
        self.screen = self._drain()
        return proc

    def spawn(self, argv: list[str], **env: str) -> subprocess.Popen:
        proc = subprocess.Popen(argv, env={**self.env, **env}, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.test.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def wait_for(self, name: str, timeout: float = 20) -> None:
        end = time.monotonic() + timeout
        while not (self.state / name).exists():
            self.test.assertLess(time.monotonic(), end, f"{name} never appeared")
            time.sleep(0.05)

    def _drain(self) -> str:
        chunks = []
        os.set_blocking(self.master, False)
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def _reap(self) -> None:
        """A fake the launcher failed to stop must not outlive the test."""
        for name in ("mapper.pid",):
            path = self.state / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

    def calls(self) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def tui_calls(self) -> list[dict]:
        return [c for c in self.calls() if "--tui" in c["args"]]

    def lines(self, name: str) -> list[str]:
        path = self.state / name
        return path.read_text().splitlines() if path.exists() else []

    def tools(self) -> list[str]:
        return self.lines("tools.log")

    def mapper_args(self) -> list[str]:
        return self.lines("mapper.args")

    def assert_mapper_stopped(self) -> None:
        """The launcher stopped gptokeyb itself, before it exited."""
        pid = int((self.state / "mapper.pid").read_text())
        stopped = (self.state / "mapper.stopped").exists()
        if not stopped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.test.assertTrue(stopped, "gptokeyb was left running")

    def assert_mapper_gone(self) -> None:
        """gptokeyb ended, by its own kill mode or by the launcher."""
        pid = int((self.state / "mapper.pid").read_text())
        end = time.monotonic() + 5
        while Path(f"/proc/{pid}").exists() and time.monotonic() < end:
            time.sleep(0.05)
        self.test.assertFalse(Path(f"/proc/{pid}").exists(), "gptokeyb was left running")

    def assert_console_restored(self) -> None:
        self.test.assertEqual(termios.tcgetattr(self.slave), self.before)


CONTROL_TXT = '''
directory="{roms}"
ESUDO=""
ESUDOKILL="-1"
CUR_TTY="{console}"
GPTOKEYB="$controlfolder/gptokeyb $ESUDOKILL"
get_controls() {{ sdl_controllerconfig="fake"; }}
'''

DIALOG_TXT = '''
PM_PID=""
PM_DONE="$FAKE_STATE/pm_done"
trace() { printf '%s\\n' "$*" >> "$FAKE_STATE/dialog.log"; }
PortMasterIPCheck() { echo 192.168.0.1; }
PortMasterDialogInit() { trace "INIT $*"; }
PortMasterDialogExit() { trace EXIT; }
PortMasterDialog() { trace "CMD $*"; }
PortMasterDialogResult() { trace "ASK $*"; echo quit; }
pm_finish() { trace FINISH; }
'''


class ThePortMasterPort(unittest.TestCase):
    """RetroBIOS.sh from the PortMaster archive, on a fake PortMaster."""

    def device(self, console: str | None = None, **env: str) -> Device:
        device = Device(self, **env)
        roms = device.root / "roms"
        self.gamedir = device.unpack(build_port.PORT_ZIP, roms / "ports") / "retrobios"
        control = device.root / "xdg" / "PortMaster"
        control.mkdir(parents=True)
        self.control = control
        (control / "control.txt").write_text(
            CONTROL_TXT.format(roms=str(roms).lstrip("/"), console=console or device.console))
        (control / "PortMasterDialog.txt").write_text(DIALOG_TXT)
        shutil.copy(device.bin / "gptokeyb", control / "gptokeyb")
        device.env["XDG_DATA_HOME"] = str(device.root / "xdg")
        # No muxterm on this device unless a test puts one there.
        device.env["RB_MUXTERM"] = str(device.root / "no-muxterm")
        self.script = str(roms / "ports" / "RetroBIOS.sh")
        return device

    def dialog(self, device: Device) -> list[str]:
        return device.lines("dialog.log")

    def test_online_on_the_console(self):
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_RAW="1", FAKE_CODE="0")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual((call["stdin"], call["stdout"]), (device.console, device.console))
        self.assertEqual(call["term"], "linux")
        installer = str(self.gamedir / "install.py")
        self.assertEqual(call["argv0"], installer)
        # Kill mode on the Start + Select sentinel, never on the installer's
        # path, which the engine's command line carries too.
        args = device.mapper_args()
        self.assertEqual(args[0], "-1")
        self.assertRegex(args[1], r"^retrobios-stop-\d+x$")
        self.assertEqual(args[2:], ["-c", str(self.gamedir / "retrobios.gptk")])
        device.assert_mapper_stopped()
        device.assert_console_restored()
        # Reset when the console is taken and again when it is handed back.
        self.assertEqual(device.screen.count("\x1bc"), 2)
        self.assertLess(device.screen.index("Checking the connection"),
                        device.screen.rindex("\x1bc"))
        saved = str(self.gamedir / "run" / "font.psf")
        fonts = device.env["RB_FONTS"]
        self.assertEqual(device.tools(), [
            f"setfont -C {device.console} -O {saved} {fonts}/Lat7-TerminusBold22x11.psf.gz",
            f"setfont -C {device.console} {saved}",
        ])
        # PortMaster's own end of a port, after the interface, no screens.
        self.assertEqual(self.dialog(device), ["FINISH"])

    def test_the_interface_status_is_the_port_status(self):
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_CODE="3", FAKE_ERROR="the folder is read-only")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("Error: the folder is read-only", device.screen)
        self.assertIn("log.txt", device.screen)

    def test_offline_installs_from_a_pack_on_the_card(self):
        device = self.device(FAKE_CURL="7")
        roms = device.root / "roms"
        (roms / "Batocera_43.1_BIOS_Pack.zip").write_bytes(b"")
        pack = self.gamedir / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
        pack.write_bytes(b"")
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui", "--from-pack", str(pack),
                                        "--platform", "retroarch"])

    def test_offline_prefers_the_pack_of_this_system(self):
        device = self.device(FAKE_CURL="6")
        (self.gamedir / "Batocera_43.1_BIOS_Pack.zip").write_bytes(b"")
        pack = device.root / "roms" / "RetroArch_Lakka_v1.22.2_BIOS_Pack.part1of2.zip"
        pack.write_bytes(b"")
        device.run(["bash", self.script])
        [call] = device.tui_calls()
        self.assertEqual(call["args"][1:3], ["--from-pack", str(pack)])

    def test_offline_without_a_pack_says_where_to_put_one(self):
        device = self.device(FAKE_CURL="7")
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(device.tui_calls(), [])
        screen = " ".join(device.screen.split())
        self.assertIn("No network connection", screen)
        self.assertIn("RetroArch", screen)
        self.assertIn(str(self.gamedir), screen)
        self.assertIn("github.com/Abdess/retrobios/releases/latest", screen)
        device.assert_mapper_stopped()
        device.assert_console_restored()

    def test_offline_says_so_at_once_and_bounds_the_detection(self):
        """The file-list download is three tries of 30 s: the detection run
        is pointed at a closed loopback port and stopped after
        RB_DETECT_LIMIT, with a line on the console from the start."""
        device = self.device(FAKE_CURL="28")
        pack = self.gamedir / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
        pack.write_bytes(b"")
        proc = device.run(["bash", self.script], FAKE_DETECT_HANG="1", RB_DETECT_LIMIT="2")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertLess(device.elapsed, 15)
        screen = device.screen
        self.assertIn("Checking the connection", screen)
        self.assertIn("No connection. Looking for the BIOS folder", screen)
        [check] = [c for c in device.calls() if "--check" in c["args"]]
        self.assertEqual(check["base_url"], "http://127.0.0.1:9")
        [call] = device.tui_calls()
        # The detection was stopped: no platform, the first pack.
        self.assertEqual(call["args"], ["--tui", "--from-pack", str(pack)])
        self.assertEqual(call["base_url"], "")

    def test_a_certificate_failure_is_not_offline(self):
        # curl 60: the network answered, install.py reports the certificate.
        device = self.device(FAKE_CURL="60")
        device.run(["bash", self.script])
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])

    def test_an_old_python_is_told_on_the_console(self):
        device = self.device()
        device.tool("python3", '#!/bin/sh\n[ "$1" = "--version" ] && echo "Python 3.7.3"\nexit 1\n')
        device.tool("uname", "#!/bin/sh\necho armv7l\n")
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 1)
        screen = " ".join(device.screen.split())
        self.assertIn("Python 3.8 or newer is needed", screen)
        self.assertIn("3.7.3", screen)
        self.assertIn("--dest", screen)
        self.assertEqual(device.calls(), [])

    def test_the_runtime_is_mounted_then_unmounted(self):
        """ArkOS's Python 3.7: PortMaster's python_3.11 runtime, mounted for
        the run and unmounted after it."""
        device = self.device()
        real = shutil.which("python3.8") or sys.executable
        device.tool("python3", '#!/bin/sh\n[ "$1" = "--version" ] && echo "Python 3.7.3"\nexit 1\n')
        device.tool("uname", "#!/bin/sh\necho aarch64\n")
        # The runtime's python, a stand-in that ignores the PYTHONHOME the
        # launcher sets for the real one.
        device.tool("mount", '#!/bin/sh\nprintf "mount %s\\n" "$*" >> "$FAKE_STATE/tools.log"\n'
                    'mkdir -p "$2/bin"\n'
                    f'printf \'#!/bin/sh\\nunset PYTHONHOME\\nexec {real} "$@"\\n\' > "$2/bin/python3"\n'
                    'chmod +x "$2/bin/python3"\n')
        device.tool("umount", FAKE_RECORDER)
        (self.control / "libs").mkdir()
        squashfs = self.control / "libs" / "python_3.11.squashfs"
        squashfs.write_bytes(b"")
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(device.tui_calls()), 1)
        runtime = str(device.root / "python_3.11")
        mounts = [t for t in device.tools() if t.startswith(("mount", "umount"))]
        self.assertEqual(mounts, [f"umount {runtime}", f"mount {squashfs} {runtime}",
                                  f"umount {runtime}"])

    def test_a_refused_console_falls_back_to_portmaster_screens(self):
        device = self.device()
        device.run(["bash", self.script], FAKE_REFUSE="1")
        self.assertEqual(len(device.tui_calls()), 1)
        dialog = self.dialog(device)
        self.assertEqual(dialog[0], "INIT no-check")
        self.assertIn("ASK selection_list --register=notice --want-description --want-cancel",
                      dialog)
        device.assert_mapper_stopped()
        device.assert_console_restored()

    def test_a_console_this_user_cannot_open_is_opened_up(self):
        """PortMaster's control.txt chmods the consoles only under sudo: the
        launcher does it for the one it draws on."""
        device = self.device()
        os.chmod(device.console, 0o000)
        self.addCleanup(os.chmod, device.console, 0o620)
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        self.assertEqual(os.stat(device.console).st_mode & 0o777, 0o666)

    def test_under_a_compositor_without_a_terminal_app_the_screens_open(self):
        device = self.device(FAKE_PGREP="0")
        device.run(["bash", self.script])
        self.assertEqual(device.tui_calls(), [])
        self.assertEqual(self.dialog(device)[0], "INIT no-check")
        self.assertEqual(device.mapper_args(), [])

    def test_arkos_offers_the_card_portmaster_does_not_run_from(self):
        device = self.device(FAKE_CURL="7")
        marker = device.root / "text.plymouth"
        marker.write_text("")
        roms = device.root / "roms" / "bios"
        other = device.root / "roms2" / "bios"
        roms.mkdir(parents=True)
        other.mkdir(parents=True)
        (roms / "a.bin").write_bytes(b"x")
        (other / "b.bin").write_bytes(b"y")
        pack = self.gamedir / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
        pack.write_bytes(b"")
        device.run(["bash", self.script], keys="\r", RB_ARK_MARKER=str(marker),
                   RB_OTHER_CARDS=f"{roms}:{other}",
                   RB_RETROARCH_CFG=str(device.root / "none.cfg"))
        [call] = device.tui_calls()
        # The port's games card is roms here: its bios is the destination.
        self.assertEqual(call["args"][:3], ["--tui", "--from", str(other)])

    def test_arkos_destination_is_the_folder_retroarch_reads(self):
        """control.txt names roms2 as the port's card whenever a second card
        is mounted: RetroArch's system_directory decides."""
        device = self.device(FAKE_CURL="7")
        marker = device.root / "text.plymouth"
        marker.write_text("")
        roms = device.root / "roms" / "bios"
        other = device.root / "roms2" / "bios"
        roms.mkdir(parents=True)
        other.mkdir(parents=True)
        (roms / "a.bin").write_bytes(b"x")
        (other / "b.bin").write_bytes(b"y")
        cfg = device.root / "retroarch.cfg"
        cfg.write_text(f'system_directory = "{other}"\n')
        (self.gamedir / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip").write_bytes(b"")
        device.run(["bash", self.script], keys="\r", RB_ARK_MARKER=str(marker),
                   RB_OTHER_CARDS=f"{roms}:{other}", RB_RETROARCH_CFG=str(cfg))
        [call] = device.tui_calls()
        self.assertEqual(call["args"][:3], ["--tui", "--from", str(roms)])

    def test_under_a_compositor_foot_shows_the_interface(self):
        device = self.device(WAYLAND_DISPLAY="wayland-1")
        device.tool("foot", FAKE_TERMAPP)
        proc = device.run(["bash", self.script], FAKE_CODE="4")
        self.assertEqual(proc.returncode, 4)
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual(device.tools(), [f"foot -F -- bash {self.gamedir}/tui.txt here"])
        self.assertRegex(device.mapper_args()[1], r"^retrobios-stop-\d+x$")
        device.assert_mapper_stopped()

    def test_a_terminal_app_that_cannot_open_hands_over(self):
        """foot refused the display: the interface never ran, and the port
        opens PortMaster's screens instead of ending on a status of 1."""
        device = self.device(WAYLAND_DISPLAY="wayland-1")
        device.tool("foot", FAKE_TERMAPP)
        proc = device.run(["bash", self.script], FAKE_TERM_FAILS="1")
        self.assertEqual(device.tui_calls(), [])
        self.assertEqual(self.dialog(device)[0], "INIT no-check", proc.stderr.decode())
        log = (self.gamedir / "log.txt").read_text()
        self.assertIn("foot ended with status 1 before the interface ran", log)
        device.assert_mapper_stopped()

    def test_on_muos_the_interface_opens_in_muxterm(self):
        """mod_muOS.txt sets CUR_TTY to /dev/null: muxterm shows the
        interface, told to leave the pad to gptokeyb."""
        device = self.device(console="/dev/null")
        muxterm = device.tool("muxterm", FAKE_TERMAPP)
        proc = device.run(["bash", self.script], RB_MUXTERM=str(muxterm))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual(device.tools(), [f"muxterm --gl -- bash {self.gamedir}/tui.txt here"])
        self.assertEqual(device.lines("termapp.sdl"), ["0x0001/0x0001"])
        self.assertEqual(device.mapper_args()[2:], ["-c", str(self.gamedir / "retrobios.gptk")])
        self.assertEqual(self.dialog(device), ["FINISH"])

    def test_on_muos_without_muxterm_the_log_says_why_the_screens_open(self):
        device = self.device(console="/dev/null")
        device.run(["bash", self.script])
        self.assertEqual(device.tui_calls(), [])
        self.assertEqual(self.dialog(device)[0], "INIT no-check")
        log = (self.gamedir / "log.txt").read_text()
        self.assertIn("/dev/null is not a console", log)

    def test_start_select_lets_the_install_say_what_it_wrote(self):
        """gptokeyb's kill reaches the sentinel only: the interface is asked
        to stop, gives the running install its SIGINT and 2 s, and writes
        its result. The engine never sees gptokeyb's SIGTERM or SIGKILL."""
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_HOLD="1", FAKE_ENGINE="1",
                          FAKE_PRESS="1", RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143, proc.stderr.decode())
        self.assertLess(device.elapsed, 15)
        self.assertEqual(device.lines("ui.log"), ["signal 15"])
        self.assertEqual(device.lines("engine.log"), ["summary after SIGINT"])
        self.assertIn("RetroBIOS: the install was stopped, 2 files written.", device.screen)
        self.assertNotIn("Press A", device.screen)
        device.assert_mapper_gone()
        device.assert_console_restored()
        self.assertEqual(self.dialog(device), ["FINISH"])

    def test_start_select_on_an_error_screen_returns_at_once(self):
        """The interface leaves an error screen with its farewell on stderr,
        and gptokeyb is gone: no message may wait for a button."""
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_HOLD="1", FAKE_PRESS="1",
                          FAKE_TERM_ERROR="could not reach the file list",
                          RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143)
        self.assertLess(device.elapsed, 10)
        self.assertNotIn("Press A", device.screen)
        device.assert_mapper_gone()
        device.assert_console_restored()

    def test_start_select_before_the_interface_handles_signals(self):
        """A SIGTERM before run_tui installs its handlers ends Python on the
        default action, status 143."""
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_HOLD="1", FAKE_PRESS="1",
                          FAKE_NO_HANDLER="1", RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143)
        self.assertLess(device.elapsed, 10)
        self.assertNotIn("Press A", device.screen)
        device.assert_console_restored()

    def test_a_mapper_that_dies_at_once_starts_no_interface(self):
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_MAPPER_DIES="1", RB_MESSAGE_WAIT="300",
                          RB_DEAD_WAIT="1")
        self.assertEqual(proc.returncode, 1)
        self.assertLess(device.elapsed, 10)
        self.assertEqual(device.tui_calls(), [])
        screen = " ".join(device.screen.split())
        self.assertIn("The controller cannot be read", screen)
        self.assertIn("log.txt", screen)
        log = (self.gamedir / "log.txt").read_text()
        self.assertIn("cannot open /dev/uinput", log)
        device.assert_console_restored()

    def test_a_mapper_that_dies_later_ends_the_interface(self):
        """Nothing moves the interface once gptokeyb is gone: it is asked to
        stop instead of waiting for a key forever."""
        device = self.device()
        proc = device.spawn(["bash", self.script], FAKE_HOLD="1")
        device.wait_for("ui.ready")
        os.kill(int((device.state / "mapper.pid").read_text()), signal.SIGKILL)
        self.assertEqual(proc.wait(timeout=20), 143)
        self.assertEqual(device.lines("ui.log"), ["signal 15"])
        device.assert_console_restored()

    def test_sigterm_to_the_launcher_stops_the_interface_and_restores(self):
        """A firmware that signals the launcher alone: the trap stops the
        interface, the mapper and puts the console back."""
        device = self.device()
        proc = device.spawn(["bash", self.script], FAKE_HOLD="1", FAKE_RAW="1")
        device.wait_for("ui.ready")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 143)
        self.assertEqual(device.lines("ui.log"), ["signal 15"])
        device.assert_mapper_stopped()
        device.assert_console_restored()

    def test_the_font_is_put_back_without_setfont_o(self):
        """busybox setfont has no -O: the font is changed only when the
        Lat7-Terminus20x10 ArkOS tools restore exists, and that one is put
        back."""
        device = self.device(FAKE_NO_SAVE="1")
        fonts = device.env["RB_FONTS"]
        (Path(fonts) / "Lat7-Terminus20x10.psf.gz").write_bytes(b"")
        device.run(["bash", self.script])
        chosen = f"{fonts}/Lat7-TerminusBold22x11.psf.gz"
        saved = str(self.gamedir / "run" / "font.psf")
        self.assertEqual(device.tools(), [
            f"setfont -C {device.console} -O {saved} {chosen}",
            f"setfont -C {device.console} {chosen}",
            f"setfont -C {device.console} {fonts}/Lat7-Terminus20x10.psf.gz",
        ])

    def test_without_the_fallback_font_the_font_is_left(self):
        device = self.device(FAKE_NO_SAVE="1")
        device.run(["bash", self.script])
        self.assertEqual(len([t for t in device.tools() if t.startswith("setfont")]), 1)

    def test_a_read_only_card_runs_from_the_runtime_folder(self):
        device = self.device()
        self.gamedir.chmod(0o555)
        self.addCleanup(self.gamedir.chmod, 0o755)
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(device.tui_calls()), 1)
        spare = device.root / "runtime" / "retrobios"
        log = (spare / "log.txt").read_text()
        self.assertIn(f"{self.gamedir} cannot be written", log)
        self.assertTrue((spare / "run" / "status").exists())

    def test_nothing_writable_is_said_on_the_console(self):
        device = self.device()
        self.gamedir.chmod(0o555)
        self.addCleanup(self.gamedir.chmod, 0o755)
        runtime = device.root / "runtime"
        runtime.mkdir(mode=0o555)
        self.addCleanup(runtime.chmod, 0o755)
        proc = device.run(["bash", self.script])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(device.calls(), [])
        self.assertIn("the SD card may be read-only or full", " ".join(device.screen.split()))

    def test_the_previous_log_is_kept(self):
        device = self.device()
        (self.gamedir / "log.txt").write_text("the failure\n")
        device.run(["bash", self.script])
        self.assertEqual((self.gamedir / "log.prev.txt").read_text(), "the failure\n")
        self.assertNotIn("the failure", (self.gamedir / "log.txt").read_text())


class TheBatoceraPort(unittest.TestCase):
    def device(self, **env: str) -> tuple[Device, Path]:
        device = Device(self, **env)
        ports = device.unpack(build_port.BATOCERA_ZIP, device.root / "userdata" / "roms" / "ports")
        return device, ports

    def console_switch(self, device: Device) -> dict[str, str]:
        (device.root / "active").write_text("tty2\n")
        number = re.fullmatch(r"/dev/pts/(\d+)", device.console)
        self.assertIsNotNone(number)
        self.number = number.group(1)
        return {"RB_VT_ACTIVE": str(device.root / "active"), "RB_VT_PREFIX": "/dev/pts/",
                "RB_VT_SWITCH": self.number}

    def test_xterm_in_the_x11_session(self):
        device, ports = self.device(DISPLAY=":0")
        device.tool("xterm", FAKE_TERMAPP)
        proc = device.run(["/bin/bash", str(ports / "RetroBIOS.sh")])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual(device.tools(),
                         [f"xterm -fullscreen -e bash {ports}/retrobios/tui.txt here"])
        # evmapy reads RetroBIOS.sh.keys: no gptokeyb is started.
        self.assertEqual(device.mapper_args(), [])

    def test_the_x_cookie_goes_with_the_display(self):
        """S31emulationstation starts X with XAUTHORITY=/var/lib/.Xauthority:
        without it xterm is refused the display."""
        device, ports = self.device()
        device.tool("xterm", FAKE_TERMAPP + "")
        socket_dir = device.root / "x11"
        socket_dir.mkdir()
        import socket as _socket  # noqa: PLC0415
        sock = _socket.socket(_socket.AF_UNIX)
        sock.bind(str(socket_dir / "X0"))
        self.addCleanup(sock.close)
        cookie = device.root / "Xauthority"
        cookie.write_text("")
        device.tool("xterm", '#!/bin/sh\nprintf "%s %s\\n" "$DISPLAY" "$XAUTHORITY" '
                    '> "$FAKE_STATE/x.env"\n' + FAKE_TERMAPP.split("\n", 1)[1])
        proc = device.run(["/bin/bash", str(ports / "RetroBIOS.sh")],
                          RB_X_SOCKET=str(socket_dir / "X0"), RB_XAUTH=str(cookie))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(device.lines("x.env"), [f":0 {cookie}"])

    def test_console_switch_when_no_terminal_app(self):
        device, ports = self.device(FAKE_PGREP="0")
        proc = device.run(["/bin/bash", str(ports / "RetroBIOS.sh")], **self.console_switch(device))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        chvt = [line for line in device.tools() if line.startswith("chvt")]
        self.assertEqual(chvt, [f"chvt {self.number}", "chvt 2"])
        device.assert_console_restored()

    def test_a_refused_console_switch_shows_nothing_on_an_unseen_console(self):
        """The compositor keeps the display: chvt waits and the console in
        front stays the same. The launcher switches back and stops, instead
        of an interface no key reaches."""
        device, ports = self.device(FAKE_PGREP="0")
        proc = device.run(["/bin/bash", str(ports / "RetroBIOS.sh")], FAKE_CHVT_REFUSED="1",
                          **self.console_switch(device))
        self.assertEqual(proc.returncode, 125)
        self.assertLess(device.elapsed, 20)
        self.assertEqual(device.tui_calls(), [])
        chvt = [line for line in device.tools() if line.startswith("chvt")]
        self.assertEqual(chvt, [f"chvt {self.number}", "chvt 2"])
        self.assertEqual((device.root / "active").read_text(), "tty2\n")
        log = (ports / "retrobios" / "log.txt").read_text()
        self.assertIn(f"console {self.number} did not come to the front", log)
        self.assertNotIn("\x1bc", device.screen)

    def test_xterm_that_cannot_open_hands_over_to_the_console(self):
        device, ports = self.device(DISPLAY=":0", FAKE_PGREP="0")
        device.tool("xterm", FAKE_TERMAPP)
        proc = device.run(["/bin/bash", str(ports / "RetroBIOS.sh")], FAKE_TERM_FAILS="1",
                          **self.console_switch(device))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        self.assertTrue(device.tools()[0].startswith("xterm -fullscreen -e bash"))


class TheMuosApp(unittest.TestCase):
    def device(self, muxterm: bool, gptokeyb2: bool = True) -> tuple[Device, Path]:
        device = Device(self)
        apps = device.unpack(build_port.MUOS_APP, device.root / "mmc" / "MUOS" / "application")
        muos = device.root / "opt-muos"
        (muos / "script" / "var").mkdir(parents=True)
        (muos / "script" / "var" / "retro.sh").write_text(
            'SETUP_APP() { printf "SETUP_APP %s\\n" "$1" >> "$FAKE_STATE/tools.log"; }\n'
            'GET_VAR() { case "$2" in\n'
            f'  storage/rom/mount) echo "{device.root / "mmc"}" ;;\n'
            f'  storage/sdcard/mount) echo "{device.root / "sdcard"}" ;;\n'
            '  *) echo "" ;;\nesac; }\n')
        if muxterm:
            (muos / "frontend").mkdir()
            shutil.copy(device.tool("muxterm", FAKE_TERMAPP), muos / "frontend" / "muxterm")
        pm = device.root / "mmc" / "MUOS" / "PortMaster"
        pm.mkdir(parents=True)
        if gptokeyb2:
            (pm / "gptokeyb2").write_text(FAKE_GPTOKEYB2)
            (pm / "gptokeyb2").chmod(0o755)
        device.env["RB_MUOS"] = str(muos)
        return device, apps / "RetroBIOS"

    def test_in_muxterm(self):
        device, app = self.device(muxterm=True)
        # muOS starts an app as <app>/mux_launch.sh <app>.
        proc = device.run([str(app / "mux_launch.sh"), str(app)])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual(device.tools(), ["SETUP_APP retrobios",
                                          f"muxterm --gl -- bash {app}/tui.txt here"])
        # muxterm leaves the pad to gptokeyb2, which sends each button once
        # with the interface's mapping: Y is Tab (Options), not muxterm's Space.
        self.assertEqual(device.lines("termapp.sdl"), ["0x0001/0x0001"])
        args = device.mapper_args()
        self.assertEqual(args[0], "-X")
        self.assertRegex(args[1], r"^retrobios-stop-\d+x$")
        self.assertEqual(args[2:], ["-c", str(app / "retrobios.gptk")])
        self.assertEqual(read_gptk(app / "retrobios.gptk")["y"], ["tab"])
        device.assert_mapper_stopped()

    def test_muxterm_alone_without_gptokeyb2(self):
        device, app = self.device(muxterm=True, gptokeyb2=False)
        proc = device.run([str(app / "mux_launch.sh"), str(app)])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(device.tui_calls()), 1)
        # muxterm then reads the pad itself.
        self.assertEqual(device.lines("termapp.sdl"), [""])
        self.assertEqual(device.mapper_args(), [])

    def test_start_select_ends_the_run_in_muxterm(self):
        """gptokeyb2 kills with SIGKILL: -X reaches the sentinel, and the
        interface is asked to stop as on the other routes."""
        device, app = self.device(muxterm=True)
        proc = device.run([str(app / "mux_launch.sh"), str(app)], FAKE_HOLD="1",
                          FAKE_PRESS="1", RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143, proc.stderr.decode())
        self.assertLess(device.elapsed, 10)
        self.assertEqual(device.lines("ui.log"), ["signal 15"])
        device.assert_mapper_gone()

    def test_start_select_ends_the_run_on_the_console(self):
        device, app = self.device(muxterm=False)
        proc = device.run([str(app / "mux_launch.sh"), str(app)], FAKE_HOLD="1",
                          FAKE_PRESS="1", RB_CONSOLE=device.console, RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143, proc.stderr.decode())
        self.assertLess(device.elapsed, 10)
        self.assertEqual(device.lines("ui.log"), ["signal 15"])
        device.assert_console_restored()

    def test_on_the_console_without_muxterm(self):
        device, app = self.device(muxterm=False)
        proc = device.run([str(app / "mux_launch.sh"), str(app)], RB_CONSOLE=device.console)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        self.assertEqual(device.mapper_args()[3], str(app / "retrobios.gptk"))
        device.assert_console_restored()

    def test_offline_reads_a_pack_at_the_card_root(self):
        device, app = self.device(muxterm=True)
        pack = device.root / "mmc" / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
        pack.write_bytes(b"")
        device.run([str(app / "mux_launch.sh"), str(app)], FAKE_CURL="7")
        [call] = device.tui_calls()
        self.assertEqual(call["args"][:3], ["--tui", "--from-pack", str(pack)])

    def test_offline_names_the_pack_that_serves_muos(self):
        """muOS runs RetroArch, and install.py reads it as retroarch: the
        RetroArch pack serves it."""
        device, app = self.device(muxterm=True)
        proc = device.run([str(app / "mux_launch.sh"), str(app)], FAKE_CURL="7")
        self.assertEqual(proc.returncode, 1)
        # muxterm draws what its child writes: here the launcher's stdout.
        screen = " ".join(proc.stdout.decode().split())
        self.assertIn("download the RetroArch BIOS pack (a file whose name starts with "
                      "RetroArch_ and ends in _BIOS_Pack.zip)", screen)

    def test_offline_takes_no_pack_named_for_another_system(self):
        device, app = self.device(muxterm=True)
        (device.root / "mmc" / "Batocera_43.1_BIOS_Pack.zip").write_bytes(b"")
        proc = device.run([str(app / "mux_launch.sh"), str(app)], FAKE_CURL="7")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(device.tui_calls(), [])

    def test_offline_reads_a_pack_on_the_second_card(self):
        device, app = self.device(muxterm=True)
        (device.root / "sdcard").mkdir()
        pack = device.root / "sdcard" / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
        pack.write_bytes(b"")
        device.run([str(app / "mux_launch.sh"), str(app)], FAKE_CURL="7")
        [call] = device.tui_calls()
        self.assertEqual(call["args"][:3], ["--tui", "--from-pack", str(pack)])

    def two_cards(self, device: Device) -> tuple[Path, Path, Path]:
        """SD1 and SD2 both hold MUOS/bios, SD2's is the one muOS binds."""
        sd1 = device.root / "mmc" / "MUOS" / "bios"
        sd2 = device.root / "sdcard" / "MUOS" / "bios"
        sd1.mkdir(parents=True)
        sd2.mkdir(parents=True)
        (sd1 / "scph5501.bin").write_bytes(b"x")
        (sd2 / "gba_bios.bin").write_bytes(b"y")
        pack = device.root / "mmc" / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
        pack.write_bytes(b"")
        return sd1, sd2, pack

    def test_offline_offers_the_other_card(self):
        device, app = self.device(muxterm=False)
        sd1, sd2, pack = self.two_cards(device)
        proc = device.run([str(app / "mux_launch.sh"), str(app)], keys="\r", FAKE_CURL="7",
                          RB_CONSOLE=device.console, RB_BIOS_DEST=str(sd2))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("Copy the BIOS files from your other card?", device.screen)
        self.assertIn(str(sd1), device.screen)
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui", "--from", str(sd1), "--from-pack", str(pack),
                                        "--platform", "retroarch"])

    def test_b_declines_the_other_card(self):
        device, app = self.device(muxterm=False)
        _sd1, sd2, pack = self.two_cards(device)
        device.run([str(app / "mux_launch.sh"), str(app)], keys="\x1b", FAKE_CURL="7",
                   RB_CONSOLE=device.console, RB_BIOS_DEST=str(sd2))
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui", "--from-pack", str(pack),
                                        "--platform", "retroarch"])

    def test_the_card_the_system_reads_is_never_offered(self):
        device, app = self.device(muxterm=False)
        sd1, _sd2, _pack = self.two_cards(device)
        shutil.rmtree(device.root / "sdcard")
        device.run([str(app / "mux_launch.sh"), str(app)], FAKE_CURL="7",
                   RB_CONSOLE=device.console, RB_BIOS_DEST=str(sd1))
        self.assertNotIn("other card", device.screen)
        [call] = device.tui_calls()
        self.assertNotIn("--from", call["args"])

    def test_online_asks_nothing(self):
        device, app = self.device(muxterm=False)
        _sd1, sd2, _pack = self.two_cards(device)
        device.run([str(app / "mux_launch.sh"), str(app)],
                   RB_CONSOLE=device.console, RB_BIOS_DEST=str(sd2))
        self.assertNotIn("other card", device.screen)
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])


class TheToolsEntry(unittest.TestCase):
    def device(self, **env: str) -> tuple[Device, Path]:
        device = Device(self, **env)
        tools = device.unpack(build_port.TOOLS_ZIP, device.root / "tools")
        device.env["RB_ROOT"] = str(device.root / "fs")
        return device, tools

    def arkos(self, device: Device) -> Path:
        fs = device.root / "fs"
        (fs / "opt" / "system").mkdir(parents=True)
        (fs / "opt" / "inttools").mkdir(parents=True)
        shutil.copy(device.bin / "gptokeyb", fs / "opt" / "inttools" / "gptokeyb")
        return fs

    def test_arkos_console_one(self):
        device, tools = self.device()
        self.arkos(device)
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], RB_CONSOLE=device.console)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual((call["stdin"], call["term"]), (device.console, "linux"))
        args = device.mapper_args()
        self.assertEqual(args[0], "-1")
        self.assertRegex(args[1], r"^retrobios-stop-\d+x$")
        self.assertEqual(args[2:], ["-c", str(tools / "retrobios" / "retrobios.gptk")])
        device.assert_mapper_stopped()
        device.assert_console_restored()

    def test_arkos_sudo_keeps_the_controller_list(self):
        """sudo's env_reset would drop SDL_GAMECONTROLLERCONFIG_FILE, and a
        pad SDL does not know sends nothing."""
        device, tools = self.device()
        fs = self.arkos(device)
        db = fs / "opt" / "inttools" / "gamecontrollerdb.txt"
        db.write_text("")
        device.tool("sudo", FAKE_SUDO)
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], RB_CONSOLE=device.console)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(device.lines("mapper.db"), [str(db)])
        started = [t for t in device.tools() if "gptokeyb" in t]
        self.assertTrue(started[0].startswith(
            "sudo --preserve-env=SDL_GAMECONTROLLERCONFIG_FILE "), started)

    def test_start_select_on_the_console(self):
        device, tools = self.device()
        self.arkos(device)
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], RB_CONSOLE=device.console,
                          FAKE_HOLD="1", FAKE_PRESS="1", RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143, proc.stderr.decode())
        self.assertLess(device.elapsed, 10)
        self.assertEqual(device.lines("ui.log"), ["signal 15"])
        self.assertNotIn("Press A", device.screen)
        device.assert_mapper_gone()
        device.assert_console_restored()

    def test_arkos_other_card_without_a_pack_needs_one(self):
        """--from needs the list of files, which an offline run takes from a
        pack: the message says so."""
        device, tools = self.device(FAKE_CURL="7")
        fs = self.arkos(device)
        roms, roms2 = device.root / "roms" / "bios", device.root / "roms2" / "bios"
        roms.mkdir(parents=True)
        roms2.mkdir(parents=True)
        (roms / "bios.bin").write_bytes(b"x")
        cfg = fs / "home" / "ark" / ".config" / "retroarch"
        cfg.mkdir(parents=True)
        (cfg / "retroarch.cfg").write_text(f'system_directory = "{roms2}"\n')
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], keys="\r",
                          RB_CONSOLE=device.console, RB_OTHER_CARDS=f"{roms}:{roms2}")
        self.assertEqual(proc.returncode, 1)
        screen = " ".join(device.screen.split())
        self.assertIn(f"Copy the BIOS files from your other card? {roms} holds files", screen)
        self.assertIn("with it, the files on your other card are copied too", screen)
        self.assertEqual(device.tui_calls(), [])

    def test_amberelec_console(self):
        device, tools = self.device()
        etc = device.root / "fs" / "etc"
        etc.mkdir(parents=True)
        (etc / "os-release").write_text('NAME="AmberELEC"\n')
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], RB_CONSOLE=device.console)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        self.assertEqual(device.mapper_args()[0], "-1")

    def rocknix(self, device: Device) -> None:
        etc = device.root / "fs" / "etc"
        etc.mkdir(parents=True)
        (etc / "os-release").write_text('NAME="ROCKNIX"\n')

    def test_rocknix_runs_in_the_terminal_tools_opens(self):
        device, tools = self.device()
        self.rocknix(device)
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], stdio_tty=True, TERM="xterm")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual((call["stdin"], call["stdout"]), (device.console, device.console))
        self.assertEqual(call["term"], "xterm")
        # The terminal app owns its window: no console reset, no font.
        self.assertNotIn("\x1bc", device.screen)
        self.assertEqual([t for t in device.tools() if t.startswith("setfont")], [])

    def test_rocknix_loads_the_controller_list_its_own_tools_load(self):
        """control-gen_init.sh writes control.ini, which names the pad's
        controller list and the GPTOKEYB to start (ROCKNIX fex-emu's Start
        FEXConfig.sh). Its ESUDO is not this launcher's."""
        device, tools = self.device()
        self.rocknix(device)
        folder = device.root / "fs" / "storage" / ".config" / "gptokeyb"
        shutil.copy(device.bin / "gptokeyb", device.root / "gptokeyb-rocknix")
        device.tool("control-gen_init.sh", f'''#!/bin/sh
mkdir -p "{folder}"
cat > "{folder}/control.ini" <<'INI'
controlfolder="{device.root}"
ESUDO="sudo"
ESUDOKILL="-sudokill"
export SDL_GAMECONTROLLERCONFIG_FILE="$controlfolder/gamecontrollerdb.txt"
get_controls() {{
DEVICE="03000000"
param_device="fake pad"
}}
GPTOKEYB="$controlfolder/gptokeyb-rocknix $ESUDOKILL"
INI
''')
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], stdio_tty=True, TERM="xterm")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        args = device.mapper_args()
        self.assertEqual(args[0], "-sudokill")
        self.assertRegex(args[1], r"^retrobios-stop-\d+x$")
        self.assertEqual(device.lines("mapper.db"), [f"{device.root}/gamecontrollerdb.txt"])
        # Stopped without the sudo control.ini names.
        device.assert_mapper_stopped()

    def test_rocknix_offline_names_no_pack_the_releases_lack(self):
        """No release pack spells ROCKNIX, and install.py refuses one named
        for another system: the RetroArch pack on the card is not offered,
        and the message gives the computer route."""
        device, tools = self.device(FAKE_CURL="7")
        self.rocknix(device)
        (tools / "retrobios" / "RetroArch_v1.22.2_BIOS_Pack.zip").write_bytes(b"")
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], stdio_tty=True, TERM="xterm",
                          FAKE_PLATFORM="rocknix", FAKE_PLATFORM_NAME="ROCKNIX")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(device.tui_calls(), [])
        screen = " ".join(device.screen.split())
        self.assertIn("the releases carry no BIOS pack for ROCKNIX", screen)
        self.assertIn("--dest", screen)
        self.assertNotIn("download", screen)

    def test_rocknix_start_select_in_foot(self):
        device, tools = self.device()
        self.rocknix(device)
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], stdio_tty=True, TERM="xterm",
                          FAKE_HOLD="1", FAKE_PRESS="1", FAKE_TERM_ERROR="unreachable",
                          RB_MESSAGE_WAIT="300")
        self.assertEqual(proc.returncode, 143, proc.stderr.decode())
        self.assertLess(device.elapsed, 10)
        self.assertNotIn("Press A", device.screen)
        device.assert_mapper_gone()


class TheDocs(unittest.TestCase):
    def test_the_offline_packs_named_are_the_ones_that_serve(self):
        """The wiki names the pack each firmware takes offline: the release
        pack of the platform install.py detects it as, or none."""
        labels = build_port.release_pack_labels()
        served = {}
        for rule in install._EMBEDDED_RULES:
            served.setdefault(rule.name, labels.get(rule.manifest))
        for name in ("muOS", "ArkOS", "dArkOS", "JELOS"):
            self.assertEqual(served[name], "RetroArch", name)
        self.assertEqual(served["Batocera"], "Batocera")
        for name in ("ROCKNIX", "KNULLI", "AmberELEC"):
            self.assertIsNone(served[name], name)
        text = (REPO_ROOT / "wiki" / "installer.md").read_text(encoding="utf-8")
        self.assertIn("The releases carry no pack for ROCKNIX,\nKnulli or AmberELEC", text)

    def test_muos_paths_are_the_card_muos_mounts(self):
        """muOS mounts the first card at /mnt/mmc (MustardOS internal
        mux_launch.sh, GET_VAR storage/rom/mount)."""
        text = (REPO_ROOT / "wiki" / "installer.md").read_text(encoding="utf-8")
        section = text[text.index("## On a handheld"):text.index("## Without a network")]
        self.assertNotIn("/mmc/MUOS", section.replace("/mnt/mmc", ""))
        self.assertIn("`MUOS/PortMaster/autoinstall/`", section)


if __name__ == "__main__":
    unittest.main()
