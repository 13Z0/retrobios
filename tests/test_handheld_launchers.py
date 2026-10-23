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
import unittest
import zipfile
from pathlib import Path

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

    def test_muxterm_mapping_adds_only_what_muxterm_leaves(self):
        """muxterm writes the D-pad, Start, B and Y to the program itself
        (MustardOS/terminal input.c, osk.c): mapping them again would press
        each twice."""
        text = (PORTS / "muos" / "muxterm.gptk").read_text()
        self.assertIn("[controls]\noverlay = clear\n", text)
        mapping = read_gptk(PORTS / "muos" / "muxterm.gptk")
        mapping.pop("overlay")
        self.assertEqual(mapping, {"a": ["enter"], "x": ["space"],
                                   "l1": ["pageup"], "r1": ["pagedown"]})


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
                    self.assertIn("RetroBIOS/muxterm.gptk", names)
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
import json, os, sys
calls = os.environ["FAKE_CALLS"]


def tty(fd):
    try:
        return os.ttyname(fd)
    except OSError:
        return ""


with open(calls, "a") as fh:
    fh.write(json.dumps({"argv0": sys.argv[0], "args": sys.argv[1:], "stdin": tty(0),
                         "stdout": tty(1), "term": os.environ.get("TERM", ""),
                         "pythonhome": os.environ.get("PYTHONHOME", "")}) + "\n")
if "--check" in sys.argv:
    print(json.dumps({"event": "detect", "os": "linux", "os_name": "Fake OS",
                      "platforms": [{"platform": "retroarch", "name": "RetroArch",
                                     "bios_dir": "/roms/bios"}]}))
    print(json.dumps({"event": "error", "stage": "manifest", "reason": "network",
                      "platform": "retroarch", "message": "unreachable"}))
    print(json.dumps({"event": "end", "code": 1}))
    sys.exit(1)
if os.environ.get("FAKE_REFUSE"):
    print("install.py: error: --tui cannot open the full-screen interface here: "
          "the terminal is 30x10, smaller than 40x12", file=sys.stderr)
    sys.exit(2)
if os.environ.get("FAKE_RAW") and os.isatty(0):
    import tty as _tty
    _tty.setraw(0)
if os.environ.get("FAKE_ERROR"):
    print("Error: " + os.environ["FAKE_ERROR"], file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_CODE", "0")))
'''

# Records its arguments and stays up until it is told to stop, as gptokeyb
# does, and records that it was.
FAKE_MAPPER = '''#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_STATE/mapper.args"
echo $$ > "$FAKE_STATE/mapper.pid"
trap 'echo stopped > "$FAKE_STATE/mapper.stopped"; kill $child 2>/dev/null; exit 0' TERM
sleep 30 &
child=$!
wait $child
'''

FAKE_RECORDER = '''#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_STATE/tools.log"
'''

FAKE_SETFONT = '''#!/bin/sh
printf '%s\\n' "setfont $*" >> "$FAKE_STATE/tools.log"
while [ $# -gt 0 ]; do
  [ "$1" = "-O" ] && { [ -n "$FAKE_NO_SAVE" ] && exit 1; printf 'font' > "$2"; }
  shift
done
'''

# A terminal app: records how it was started, then runs its command.
FAKE_TERMAPP = '''#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_STATE/tools.log"
while [ $# -gt 0 ]; do
  case "$1" in
    --|-e) shift; break ;;
    *) shift ;;
  esac
done
exec "$@"
'''


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
        self.tool("chvt", FAKE_RECORDER)
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
                   if k not in ("DISPLAY", "WAYLAND_DISPLAY", "RETROBIOS_BASE_URL")}
        self.env = {
            **environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "FAKE_STATE": str(self.state),
            "FAKE_CALLS": str(self.state / "calls.jsonl"),
            "RB_FONTS": str(fonts),
            "RB_FB": str(self.root / "fb"),
            "RB_MESSAGE_WAIT": "1",
            "TERM": "dumb",
            **env,
        }

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
        proc = subprocess.run(argv, env={**self.env, **env}, stdin=stdin, stdout=stdout,
                              stderr=subprocess.PIPE, timeout=120, check=False)
        self.screen = self._drain()
        return proc

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

    def calls(self) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def tui_calls(self) -> list[dict]:
        return [c for c in self.calls() if "--tui" in c["args"]]

    def tools(self) -> list[str]:
        path = self.state / "tools.log"
        return path.read_text().splitlines() if path.exists() else []

    def mapper_args(self) -> list[str]:
        path = self.state / "mapper.args"
        return path.read_text().splitlines() if path.exists() else []

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
'''


class ThePortMasterPort(unittest.TestCase):
    """RetroBIOS.sh from the PortMaster archive, on a fake PortMaster."""

    def device(self, **env: str) -> Device:
        device = Device(self, **env)
        roms = device.root / "roms"
        self.gamedir = device.unpack(build_port.PORT_ZIP, roms / "ports") / "retrobios"
        control = device.root / "xdg" / "PortMaster"
        control.mkdir(parents=True)
        (control / "control.txt").write_text(
            CONTROL_TXT.format(roms=str(roms).lstrip("/"), console=device.console))
        (control / "PortMasterDialog.txt").write_text(DIALOG_TXT)
        shutil.copy(device.bin / "gptokeyb", control / "gptokeyb")
        device.env["XDG_DATA_HOME"] = str(device.root / "xdg")
        self.script = str(roms / "ports" / "RetroBIOS.sh")
        return device

    def dialog(self, device: Device) -> list[str]:
        path = device.state / "dialog.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_online_on_the_console(self):
        device = self.device()
        proc = device.run(["bash", self.script], FAKE_RAW="1", FAKE_CODE="0")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual((call["stdin"], call["stdout"]), (device.console, device.console))
        self.assertEqual(call["term"], "linux")
        # Kill mode on the installer's own path, which the interface's command
        # line carries: never python3.
        installer = str(self.gamedir / "install.py")
        self.assertEqual(device.mapper_args(),
                         ["-1", installer, "-c", str(self.gamedir / "retrobios.gptk")])
        self.assertEqual(call["argv0"], installer)
        device.assert_mapper_stopped()
        device.assert_console_restored()
        self.assertIn("\x1bc", device.screen)
        saved = str(self.gamedir / "run" / "font.psf")
        fonts = device.env["RB_FONTS"]
        self.assertEqual(device.tools(), [
            f"setfont -C {device.console} -O {saved} {fonts}/Lat7-TerminusBold22x11.psf.gz",
            f"setfont -C {device.console} {saved}",
        ])
        self.assertEqual(self.dialog(device), [])

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
                   RB_OTHER_CARDS=f"{roms}:{other}")
        [call] = device.tui_calls()
        # The port's games card is roms here: its bios is the destination.
        self.assertEqual(call["args"][:3], ["--tui", "--from", str(other)])

    def test_under_a_compositor_foot_shows_the_interface(self):
        device = self.device(WAYLAND_DISPLAY="wayland-1")
        device.tool("foot", FAKE_TERMAPP)
        proc = device.run(["bash", self.script], FAKE_CODE="4")
        self.assertEqual(proc.returncode, 4)
        [call] = device.tui_calls()
        self.assertEqual(call["args"], ["--tui"])
        self.assertEqual(device.tools(), [f"foot -F -- bash {self.gamedir}/tui.txt here"])
        self.assertEqual(device.mapper_args()[1], str(self.gamedir / "install.py"))
        device.assert_mapper_stopped()


class TheBatoceraPort(unittest.TestCase):
    def device(self, **env: str) -> tuple[Device, Path]:
        device = Device(self, **env)
        ports = device.unpack(build_port.BATOCERA_ZIP, device.root / "userdata" / "roms" / "ports")
        return device, ports

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

    def test_console_switch_when_no_terminal_app(self):
        device, ports = self.device(FAKE_PGREP="0")
        (device.root / "active").write_text("tty2\n")
        number = re.fullmatch(r"/dev/pts/(\d+)", device.console)
        self.assertIsNotNone(number)
        proc = device.run(["/bin/bash", str(ports / "RetroBIOS.sh")],
                          RB_VT_ACTIVE=str(device.root / "active"),
                          RB_VT_PREFIX="/dev/pts/", RB_VT_SWITCH=number.group(1))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        chvt = [line for line in device.tools() if line.startswith("chvt")]
        self.assertEqual(chvt, [f"chvt {number.group(1)}", "chvt 2"])
        device.assert_console_restored()


class TheMuosApp(unittest.TestCase):
    def device(self, muxterm: bool) -> tuple[Device, Path]:
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
        shutil.copy(device.bin / "gptokeyb", pm / "gptokeyb2")
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
        self.assertEqual(device.mapper_args(),
                         [str(app / "install.py"), "-c", str(app / "muxterm.gptk")])
        device.assert_mapper_stopped()

    def test_on_the_console_without_muxterm(self):
        device, app = self.device(muxterm=False)
        proc = device.run([str(app / "mux_launch.sh"), str(app)], RB_CONSOLE=device.console)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual(call["stdin"], device.console)
        self.assertEqual(device.mapper_args()[2], str(app / "retrobios.gptk"))
        device.assert_console_restored()

    def test_offline_reads_a_pack_at_the_card_root(self):
        device, app = self.device(muxterm=True)
        pack = device.root / "mmc" / "RetroArch_Lakka_v1.22.2_BIOS_Pack.zip"
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

    def test_arkos_console_one(self):
        device, tools = self.device()
        fs = device.root / "fs"
        (fs / "opt" / "system").mkdir(parents=True)
        (fs / "opt" / "inttools").mkdir(parents=True)
        shutil.copy(device.bin / "gptokeyb", fs / "opt" / "inttools" / "gptokeyb")
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], RB_CONSOLE=device.console)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual((call["stdin"], call["term"]), (device.console, "linux"))
        installer = str(tools / "retrobios" / "install.py")
        self.assertEqual(device.mapper_args(),
                         ["-1", installer, "-c", str(tools / "retrobios" / "retrobios.gptk")])
        device.assert_mapper_stopped()
        device.assert_console_restored()

    def test_arkos_other_card_without_a_pack_needs_one(self):
        """--from needs the list of files, which an offline run takes from a
        pack: the message says so."""
        device, tools = self.device(FAKE_CURL="7")
        fs = device.root / "fs"
        (fs / "opt" / "system").mkdir(parents=True)
        (fs / "opt" / "inttools").mkdir(parents=True)
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
        self.assertEqual(device.mapper_args()[:2], ["-1", str(tools / "retrobios" / "install.py")])

    def test_rocknix_runs_in_the_terminal_tools_opens(self):
        device, tools = self.device()
        etc = device.root / "fs" / "etc"
        etc.mkdir(parents=True)
        (etc / "os-release").write_text('NAME="ROCKNIX"\n')
        proc = device.run(["bash", str(tools / "RetroBIOS.sh")], stdio_tty=True, TERM="xterm")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        [call] = device.tui_calls()
        self.assertEqual((call["stdin"], call["stdout"]), (device.console, device.console))
        self.assertEqual(call["term"], "xterm")
        # The terminal app owns its window: no console reset, no font.
        self.assertNotIn("\x1bc", device.screen)
        self.assertEqual([t for t in device.tools() if t.startswith("setfont")], [])


if __name__ == "__main__":
    unittest.main()
