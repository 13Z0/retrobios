"""The installer's full-screen interface.

The interface is a model, a pure update on keys and events, and pure views:
most of it is tested here without a terminal. The end-to-end cases run the
real install.py --tui on a pseudo-terminal read back with pyte, against a
loopback copy of the repository, and skip when pyte is not installed (CI
installs it).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import functools
import hashlib
import http.server
import io
import json
import os
import queue
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import types
import unittest
import warnings
import zipfile
from pathlib import Path, PureWindowsPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import install  # noqa: E402
from test_install_app import make_card  # noqa: E402
from test_install_selection import FakeRepository, _QuietHandler  # noqa: E402

try:
    import pyte
except ImportError:
    pyte = None

try:
    import pty
except ImportError:
    pty = None

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:
    given = None

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "tui"
SIZES = ((40, 12), (40, 15), (80, 24), (120, 40))
# RETROBIOS_PAD's values, and "keys" for a desktop that sets none.
ROUTES = ("keys", "gptokeyb", "gptokeyb_muxterm", "evmapy", "muxterm", "miyoo")
UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
Key = install.TuiKey


def keys(*names: str) -> list:
    return [Key("char", name[5:]) if name.startswith("char:") else Key(name) for name in names]


def _args(**given) -> argparse.Namespace:
    """The namespace main() parses, every option at its default."""
    values = dict(
        platform=None, dest=None, target=None, every_board=False, check=False,
        list_platforms=False, list_targets=False, jobs=8, verbose=False,
        standalone_copies=False, emulator=None, installed_cores=False, save_dir=None,
        cores_dir=None, system=None, core=None, region=None, no_game_data=False,
        list_systems=False, list_cores=False, romm_firmware=None, list_emulators=False,
        no_input=False, dry_run=False, yes=False, quiet=False, events=False,
        restore=None, from_pack=None, manifest=None, tui=False, plain=False, add_app=False,
    )
    values["from"] = None
    values.update(given)
    return argparse.Namespace(**values)


class KeyDecoding(unittest.TestCase):
    def feed(self, *chunks: str, flush: bool = True) -> list:
        decoder = install.KeyDecoder()
        found = []
        for chunk in chunks:
            found += decoder.feed(chunk)
        if flush:
            found += decoder.flush()
        return found

    def test_every_key_of_the_table(self):
        table = {
            "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
            "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
            "\x1b[5~": "pgup", "\x1b[6~": "pgdn", "\x1b[H": "home", "\x1b[F": "end",
            "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[7~": "home", "\x1b[8~": "end",
            "\x1bOH": "home", "\x1bOF": "end", "\x1bOP": "f1", "\x1b[11~": "f1",
            "\x1b[1;2A": "shift-up", "\x1b[1;2B": "shift-down", "\x1b[3~": "delete",
            "\r": "enter", "\n": "enter", "\x1bOM": "enter", "\x7f": "backspace",
            "\x08": "backspace", "\t": "tab", "\x03": "ctrl-c", "\x04": "ctrl-d",
            "\x1b": "esc",
        }
        for sequence, name in table.items():
            with self.subTest(sequence=sequence):
                self.assertEqual(self.feed(sequence), [Key(name)])

    def test_text_keys(self):
        self.assertEqual(self.feed("q /?jké"), keys("char:q", "char: ", "char:/", "char:?",
                                                    "char:j", "char:k", "char:é"))

    def test_two_arrows_in_one_read_are_two_moves(self):
        self.assertEqual(self.feed("\x1b[B\x1b[B"), keys("down", "down"))

    def test_a_sequence_split_across_reads_is_one_key(self):
        for cut in range(1, 4):
            with self.subTest(cut=cut):
                sequence = "\x1b[5~"
                self.assertEqual(self.feed(sequence[:cut], sequence[cut:]), keys("pgup"))

    def test_esc_waits_for_the_rest_then_stands_alone(self):
        decoder = install.KeyDecoder()
        self.assertEqual(decoder.feed("\x1b"), [])
        self.assertTrue(decoder.waiting)
        self.assertEqual(decoder.flush(), keys("esc"))
        self.assertFalse(decoder.waiting)

    def test_esc_then_a_letter(self):
        self.assertEqual(self.feed("\x1bq"), keys("esc", "char:q"))
        self.assertEqual(self.feed("\x1b\x1b[A"), keys("esc", "up"))

    def test_unknown_sequences_are_dropped_not_typed(self):
        for sequence in ("\x1b[25~", "\x1b[1;5A", "\x1b[M !!", "\x1b[<0;10;5M", "\x1b[?1;2c",
                         "\x1bOZ"):
            with self.subTest(sequence=sequence):
                self.assertEqual(self.feed(sequence + "x"), keys("char:x"))

    def test_an_incomplete_sequence_is_dropped_at_the_timeout(self):
        self.assertEqual(self.feed("\x1b[1"), [])

    def test_a_paste_is_one_piece_without_its_newlines(self):
        self.assertEqual(self.feed("\x1b[200~/media/sd\ncard\r\n\x1b[201~"),
                         [Key("paste", "/media/sdcard")])
        self.assertEqual(self.feed("\x1b[200~/a/b\x1b[2", "01~\r"),
                         [Key("paste", "/a/b"), Key("enter")])

    def test_controls_nobody_bound_are_ignored(self):
        self.assertEqual(self.feed("\x00\x1cx"), keys("char:x"))
        # Ctrl-Z is a key once ISIG is off: the run stops on it, as on SIGTSTP.
        self.assertEqual(self.feed("\x1a"), [Key("ctrl-z")])

    def test_windows_scan_codes(self):
        for prefix in ("\x00", "\xe0"):
            for code, name in (("H", "up"), ("P", "down"), ("K", "left"), ("M", "right"),
                               ("I", "pgup"), ("Q", "pgdn"), ("G", "home"), ("O", "end"),
                               (";", "f1")):
                with self.subTest(prefix=prefix, code=code):
                    pending = iter([code])
                    self.assertEqual(install.decode_windows_key(prefix, pending.__next__),
                                     Key(name))
        unread = iter(())
        for char, name in (("\r", "enter"), ("\x1b", "esc"), ("\x08", "backspace"),
                           ("\x03", "ctrl-c"), ("\t", "tab")):
            self.assertEqual(install.decode_windows_key(char, unread.__next__), Key(name))
        self.assertEqual(install.decode_windows_key("a", unread.__next__), Key("char", "a"))
        self.assertIsNone(install.decode_windows_key("\xe0", iter(["\x86"]).__next__))

    @unittest.skipUnless(given, "the decoder fuzz needs hypothesis")
    def test_any_input_decodes_the_same_however_it_is_split(self):
        alphabet = st.sampled_from(["\x1b", "[", "O", "A", "B", "5", "~", "1", ";", "2",
                                    "200~", "201~", "\r", "x", "M", "\x03", "<", " "])

        @settings(max_examples=400, deadline=None)
        @given(st.lists(alphabet, max_size=30), st.data())
        def check(parts, data):
            text = "".join(parts)
            whole = self.feed(text)
            cuts = sorted(data.draw(st.lists(st.integers(0, len(text)), max_size=5)))
            chunks = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
            self.assertEqual(self.feed(*chunks), whole)
            for key in whole:
                if key.name == "char":
                    self.assertNotEqual(key.text, "\x1b")

        check()


class TextWidth(unittest.TestCase):
    def test_wide_and_combining_characters(self):
        self.assertEqual(install.cell_width("PlayStation"), 11)
        self.assertEqual(install.cell_width("ファミコン"), 10)
        self.assertEqual(install.cell_width("e\u0301"), 1)

    def test_fit_never_cuts_a_character(self):
        self.assertEqual(install.fit("ファミコン", 5), "ファ")
        self.assertEqual(install.cell_width(install.fit("aファ", 2)), 1)

    def test_middle_keeps_both_ends(self):
        path = "/run/media/user/SDCARD/Bios/PS"
        cut = install.middle(path, 20)
        self.assertEqual(install.cell_width(cut), 20)
        self.assertTrue(cut.startswith("/run/med"))
        self.assertTrue(cut.endswith("Bios/PS"))
        self.assertIn("...", cut)
        self.assertEqual(install.middle("short", 20), "short")


class StartConditions(unittest.TestCase):
    class _Tty(io.StringIO):
        def __init__(self, fd: int) -> None:
            super().__init__()
            self.fd = fd

        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return self.fd

    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.size(80, 24)
        self.tty = self._Tty(self.slave)

    def size(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def blocker(self, env=None, **given) -> str:
        return install.tui_blocker(_args(**given), self.tty, self.tty,
                                   {"TERM": "xterm"} if env is None else env)

    def test_an_install_at_a_terminal_opens_it(self):
        self.assertEqual(self.blocker(), "")
        self.assertEqual(self.blocker(platform="retroarch", system=["x"], region=["us"]), "")

    def test_flags_that_keep_the_line_questions(self):
        for flag in ("check", "dry_run", "list_platforms", "list_targets", "list_systems",
                     "list_cores", "list_emulators", "events", "quiet", "no_input", "yes"):
            with self.subTest(flag=flag):
                self.assertIn("runs without questions", self.blocker(**{flag: True}))
        self.assertIn("--restore", self.blocker(restore=Path("x")))

    def test_dumb_or_missing_term(self):
        self.assertIn("TERM", self.blocker(env={"TERM": "dumb"}))
        self.assertIn("TERM", self.blocker(env={}))

    def test_not_a_terminal(self):
        args = _args()
        self.assertIn("input", install.tui_blocker(args, io.StringIO(), self.tty,
                                                   {"TERM": "xterm"}))
        self.assertIn("output", install.tui_blocker(args, self.tty, io.StringIO(),
                                                    {"TERM": "xterm"}))

    def test_too_small_and_unsized(self):
        self.size(39, 30)
        self.assertIn("39x30", self.blocker())
        # script(1) fed from a pipe never sizes its terminal.
        self.size(0, 0)
        self.assertIn("0x0", self.blocker())

    def test_a_windows_console_without_escape_sequences(self):
        # Legacy conhost, PowerShell ISE: no virtual terminal, the questions run.
        with mock.patch.dict(sys.modules, {"msvcrt": mock.Mock()}), \
                mock.patch.object(install.os, "name", "nt"), \
                mock.patch.object(install, "_enable_ansi", return_value=False):
            self.assertIn("escape sequences", self.blocker(env={}))
        with mock.patch.dict(sys.modules, {"msvcrt": mock.Mock()}), \
                mock.patch.object(install.os, "name", "nt"), \
                mock.patch.object(install, "_enable_ansi", return_value=True):
            self.assertEqual(self.blocker(env={}), "")

    def test_plain_mode(self):
        self.assertTrue(install.plain_requested(_args(plain=True), {}))
        self.assertTrue(install.plain_requested(_args(), {"RETROBIOS_PLAIN": "1"}))
        self.assertTrue(install.plain_requested(_args(), {"ACCESSIBLE": "1"}))
        self.assertFalse(install.plain_requested(_args(), {"ACCESSIBLE": "0",
                                                           "RETROBIOS_PLAIN": ""}))

    def test_tui_refused_with_a_reason(self):
        for extra, message in ((["--check"], "--check runs without questions"),
                               (["--plain"], "two different interfaces"),
                               ([], "not a terminal")):
            with self.subTest(extra=extra):
                proc = install_run("--tui", *extra)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(message, proc.stderr)
                self.assertNotIn("\x1b", proc.stdout + proc.stderr)


def install_run(*options: str, env: dict | None = None):
    import subprocess

    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "install.py"), *options], capture_output=True,
        text=True, timeout=60, check=False, stdin=subprocess.DEVNULL,
        env={**os.environ, **(env or {})},
    )


# A repository-like state, as the events of a check fill it.
MANIFEST = {
    "manifest_version": 2, "platform": "retroarch", "categorized": True,
    "system_cores": {"sony-playstation": ["pcsx_rearmed"]},
    "files": [
        {"dest": "scph5500.bin", "size": 524288, "systems": ["sony-playstation"],
         "regions": ["japan"], "cores": None},
        {"dest": "scph5501.bin", "size": 524288, "systems": ["sony-playstation"],
         "regions": ["north-america"], "cores": None},
        {"dest": "gba_bios.bin", "size": 16384, "systems": ["nintendo-gba"], "cores": None},
        {"dest": "pcsx/extra.bin", "size": 4, "systems": ["sony-playstation"],
         "cores": ["pcsx_rearmed"]},
        {"dest": "fc/disksys.rom", "size": 8192, "systems": ["nintendo-famicom-disk"],
         "cores": ["mesen"]},
    ],
}
SYSTEMS = [
    {"event": "system", "platform": "retroarch", "system": "nintendo-famicom-disk",
     "name": "ファミコン", "files": 1, "present": 1, "missing": 0, "wrong_hash": 0,
     "bytes_needed": 0},
    {"event": "system", "platform": "retroarch", "system": "nintendo-gba",
     "name": "gba", "files": 1, "present": 0, "missing": 1, "wrong_hash": 0,
     "bytes_needed": 16384},
    {"event": "system", "platform": "retroarch", "system": "sony-playstation",
     "name": "sony-playstation", "files": 3, "present": 0, "missing": 2, "wrong_hash": 1,
     "bytes_needed": 1048580},
]
CHECK = {"event": "check", "platform": "retroarch", "name": "RetroArch",
         "bios_dir": "/home/user/.config/retroarch/system", "files": 5, "present": 1,
         "missing": 3, "wrong_hash": 1, "bytes_needed": 1064964, "data_missing": 0,
         "not_installable": 2}
FILES = [
    {"event": "file", "platform": "retroarch", "dest": "scph5500.bin",
     "systems": ["sony-playstation"], "size": 524288, "state": "missing"},
    {"event": "file", "platform": "retroarch", "dest": "scph5501.bin",
     "systems": ["sony-playstation"], "size": 524288, "state": "wrong_hash"},
    {"event": "file", "platform": "retroarch", "dest": "pcsx/extra.bin",
     "systems": ["sony-playstation"], "size": 4, "state": "missing"},
    {"event": "file", "platform": "retroarch", "dest": "gba_bios.bin",
     "systems": ["nintendo-gba"], "size": 16384, "state": "missing"},
]
HOME = Path("/home/user")
SYSTEM_DIR = HOME / ".config" / "retroarch" / "system"


def fresh_state() -> install.TuiState:
    state = install.TuiState(os_type="linux", home=HOME)
    state.setups = [("retroarch", SYSTEM_DIR, "RetroArch"),
                    ("batocera", Path("/userdata/bios"), "KNULLI (Batocera files)")]
    return state


def checked_state(facts: dict | None = None) -> install.TuiState:
    """The plan screen of a RetroArch folder with files missing."""
    state = fresh_state()
    state.stack = ["setup"]
    with mock.patch.object(install.Path, "exists", return_value=True):
        effects = install.tui_update(state, Key("enter"))
    assert effects == [("fetch", "retroarch")], effects
    effects = install.tui_receive(state, ("manifest", "retroarch", MANIFEST))
    assert effects == [("run", "check")], effects
    for event in [*FILES, *SYSTEMS, CHECK]:
        install.tui_receive(state, ("event", state.run_id, event))
    with mock.patch.object(install, "_tui_folder_facts", return_value=facts or {
            "free": 50 * 1024 ** 3, "writable": True, "cores": ["pcsx_rearmed"]}):
        install.tui_receive(state, ("ended", state.run_id, 1, []))
    return state


def press(state: install.TuiState, *pressed) -> list:
    effects = []
    for key in pressed:
        effects += install.tui_update(state, Key("char", key) if isinstance(key, str)
                                      and len(key) == 1 else Key(key))
    return effects


class Flow(unittest.TestCase):
    """update() on keys and events: what each choice starts and passes on."""

    def test_three_presses_install_everything(self):
        state = checked_state()
        self.assertEqual(state.screen, "plan")
        self.assertEqual(press(state, "enter"), [("run", "install")])
        self.assertEqual(state.screen, "installing")
        argv = install.tui_argv(state, "install", "py", "install.py")
        self.assertEqual(argv, ["py", "install.py", "--platform=retroarch",
                                f"--dest={SYSTEM_DIR}", "--events", "--yes"])
        for event in ({"event": "download", "platform": "retroarch", "files": 4,
                       "bytes": 1064964},
                      {"event": "downloaded", "dest": "gba_bios.bin", "ok": True, "reason": "",
                       "size": 16384, "files_done": 1, "files_total": 4,
                       "bytes_done": 16384, "bytes_total": 1064964},
                      {"event": "summary", "mode": "install", "installed": 4, "up_to_date": 1,
                       "pending": 0, "failed": [], "skipped": [], "left_out": [],
                       "kept": [], "interrupted": False}):
            install.tui_receive(state, ("event", state.run_id, event))
        install.tui_receive(state, ("ended", state.run_id, 0, []))
        self.assertEqual(state.screen, "result")
        press(state, "enter")
        self.assertEqual(state.exit_code, 0)

    def test_every_run_reads_the_folders_given_with_from(self):
        """--tui --from OLD: the check and the install both copy from OLD,
        each folder named once."""
        args = _args(**{"from": [Path("old-card"), Path("backup.zip")],
                        "from_pack": [Path("pack.zip")]})
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            started, _effects = install.tui_start(args, "linux")
        state = checked_state()
        state.passthrough = started.passthrough
        state.from_folders = started.from_folders
        for run in ("check", "install"):
            argv = install.tui_argv(state, run, "py", "i.py")
            for folder in ("old-card", "backup.zip"):
                self.assertEqual(argv.count(f"--from={Path(folder).absolute()}"), 1, argv)
            self.assertIn(f"--from-pack={Path('pack.zip').absolute()}", argv)

    def test_the_check_runs_with_the_command_line_choices(self):
        state = fresh_state()
        state.passthrough = [("--target", "switch")]
        state.cli_systems = ["psx"]
        state.regions = ["north-america"]
        state.platform, state.dest = "retroarch", SYSTEM_DIR
        state.manifest, state.manifest_for = MANIFEST, "retroarch"
        self.assertEqual(install._start_check(state), [("run", "check")])
        self.assertEqual(install.tui_argv(state, "check", "py", "i.py")[4:], [
            "--target=switch", "--system=psx", "--region=north-america",
            "--check", "--events"])

    def test_one_system_goes_through_the_dry_run(self):
        state = checked_state()
        press(state, "down", "enter")
        self.assertEqual(state.screen, "systems")
        self.assertEqual(state.ticked, {"nintendo-gba", "sony-playstation"})
        self.assertEqual(install.tui_rows(state)[state.focus["systems"]].action, "system")
        press(state, "n")
        self.assertEqual(state.ticked, set())
        focus = next(i for i, row in enumerate(install.tui_rows(state))
                     if row.value == "nintendo-gba")
        state.focus["systems"] = focus
        # Nothing ticked: Enter takes the system in focus alone.
        self.assertEqual(press(state, "enter"), [("run", "confirm")])
        self.assertEqual(state.ticked, {"nintendo-gba"})
        self.assertEqual(state.screen, "confirm")
        self.assertIn("--system=nintendo-gba", install.tui_argv(state, "confirm", "py", "i.py"))
        self.assertIn("--dry-run", install.tui_argv(state, "confirm", "py", "i.py"))
        dry = {**CHECK, "files": 1, "present": 0, "missing": 1, "wrong_hash": 0,
               "bytes_needed": 16384}
        install.tui_receive(state, ("event", state.run_id, dry))
        install.tui_receive(state, ("ended", state.run_id, 0, []))
        self.assertEqual(state.screen, "confirm")
        self.assertEqual(press(state, "enter"), [("run", "install")])
        argv = install.tui_argv(state, "install", "py", "i.py")
        self.assertEqual(argv[4:], ["--system=nintendo-gba", "--events", "--yes"])

    def test_a_choice_that_changes_nothing_skips_the_confirmation(self):
        state = checked_state()
        state.jobs = 4
        self.assertEqual(press(state, "enter"), [("run", "confirm")])
        install.tui_receive(state, ("event", state.run_id, dict(CHECK)))
        effects = install.tui_receive(state, ("ended", state.run_id, 0, []))
        self.assertEqual(effects, [("run", "install")])
        self.assertEqual(state.screen, "installing")

    def test_options_set_the_flags_they_name(self):
        state = checked_state()
        press(state, "o")
        self.assertEqual(state.screen, "options")
        rows = install.tui_rows(state)
        self.assertEqual([row.right for row in rows], [
            "--region", "--no-game-data", "--installed-cores", "--core", "--jobs", "--dest",
            "--from", "--dry-run", ""])
        press(state, "enter")
        self.assertEqual(state.screen, "regions")
        # Ticked in the order of preference, then moved up.
        regions = [row.value for row in install.tui_rows(state)]
        self.assertEqual(regions, ["japan", "north-america"])
        press(state, "enter", "down", "enter")
        self.assertEqual(state.regions, ["japan", "north-america"])
        press(state, "left")
        self.assertEqual(state.regions, ["north-america", "japan"])
        self.assertEqual(state.focus["regions"], 0)
        press(state, "shift-down")
        self.assertEqual(state.regions, ["japan", "north-america"])
        press(state, "esc", "down", "enter", "down", "right", "down", "down", "right", "right")
        self.assertTrue(state.no_game_data)
        self.assertTrue(state.installed_only)
        self.assertEqual(state.jobs, 10)
        argv = install.tui_argv(state, "install", "py", "i.py")
        self.assertEqual(argv[4:], ["--region=japan,north-america", "--no-game-data",
                                    "--installed-cores", "--jobs=10", "--events", "--yes"])
        self.assertIn("--region japan,north-america --no-game-data", install.tui_command(state))

    def test_cores_checklist(self):
        state = checked_state()
        press(state, "o")
        state.focus["options"] = 3
        press(state, "enter")
        self.assertEqual(state.screen, "cores")
        self.assertEqual([row.value for row in install.tui_rows(state)][1:],
                         ["mesen", "pcsx_rearmed"])
        self.assertEqual(install.tui_rows(state)[state.focus["cores"]].value, "mesen")
        press(state, " ")
        self.assertEqual(state.cores, ["mesen"])
        press(state, "left")
        self.assertEqual(state.cores, [])
        press(state, "/", "p", "c")
        self.assertEqual([row.value for row in install.tui_rows(state)][1:], ["pcsx_rearmed"])
        press(state, "enter", "right", "enter")
        self.assertEqual(state.screen, "options")
        self.assertEqual(install.tui_flags(state), [("--core", "pcsx_rearmed")])

    def test_preview_is_a_dry_run(self):
        state = checked_state()
        press(state, "o")
        state.focus["options"] = 7
        self.assertEqual(press(state, "enter"), [("run", "preview")])
        self.assertEqual(install.tui_argv(state, "preview", "py", "i.py")[-2:],
                         ["--dry-run", "--events"])
        for event in [*FILES, CHECK]:
            install.tui_receive(state, ("event", state.run_id, event))
        install.tui_receive(state, ("ended", state.run_id, 0, []))
        text = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn("4 files to download, 1.0 MB. Nothing is written.", text)
        self.assertIn("wrong copy", text)
        press(state, "esc")
        self.assertEqual(state.screen, "options")

    def test_back_and_quit(self):
        state = fresh_state()
        press(state, "esc")
        self.assertEqual(state.screen, "quit")
        press(state, "esc")
        self.assertEqual(state.screen, "setup")
        press(state, "esc", "enter")
        self.assertEqual(state.exit_code, 130)
        state = checked_state()
        self.assertEqual(press(state, "q"), [])
        self.assertEqual(state.exit_code, 130)

    def test_ctrl_c_stops_an_install_once_then_kills(self):
        state = checked_state()
        press(state, "enter")
        self.assertEqual(press(state, "ctrl-c"), [("stop",)])
        self.assertTrue(state.stopping)
        self.assertEqual(press(state, "ctrl-c"), [("kill",)])
        summary = {"event": "summary", "mode": "install", "installed": 1, "up_to_date": 1,
                   "pending": 3, "failed": [], "skipped": [], "left_out": [], "kept": [],
                   "interrupted": True}
        install.tui_receive(state, ("event", state.run_id,
                                    {"event": "download", "platform": "retroarch",
                                     "files": 4, "bytes": 1064964}))
        install.tui_receive(state, ("event", state.run_id, summary))
        install.tui_receive(state, ("ended", state.run_id, 130, []))
        title, lines = install.tui_outcome(state)
        self.assertEqual(title, "Stopped")
        self.assertEqual(lines[0], "Stopped: 1 of 4 installed, run again to finish.")
        press(state, "enter")
        self.assertEqual(state.exit_code, 130)

    def test_back_from_the_check_stops_it(self):
        state = fresh_state()
        with mock.patch.object(install.Path, "exists", return_value=True):
            press(state, "enter")
        install.tui_receive(state, ("manifest", "retroarch", MANIFEST))
        self.assertTrue(state.running)
        self.assertEqual(press(state, "esc"), [("kill",)])
        self.assertEqual(state.screen, "setup")
        install.tui_receive(state, ("ended", state.run_id - 1, 1, []))
        self.assertEqual(state.screen, "setup")

    def test_ctrl_c_elsewhere_ends_at_once(self):
        state = fresh_state()
        self.assertEqual(press(state, "ctrl-c"), [])
        self.assertEqual(state.exit_code, 130)
        state = checked_state()
        state.running = True
        self.assertEqual(press(state, "ctrl-d"), [("kill",)])

    def test_the_folder_path(self):
        state = fresh_state()
        press(state, "down", "down", "enter")
        self.assertEqual(state.screen, "platforms")
        self.assertEqual(install.tui_rows(state)[state.focus["platforms"]].value, "retroarch")
        press(state, "/", "b", "a", "t", "o")
        self.assertEqual([row.value for row in install.tui_rows(state)][1:], ["batocera"])
        # tui_audit_7 P3-9.2: the one match is chosen with the first Enter.
        press(state, "enter")
        self.assertEqual(state.screen, "folder")
        self.assertEqual(state.platform, "batocera")
        state.focus["folder"] = 2
        press(state, "enter")
        self.assertEqual(state.screen, "path")
        state.path_text = ""
        press(state, "paste")
        install.tui_update(state, Key("paste", '"~/my bios"'))
        with mock.patch.object(install.Path, "exists", return_value=False), \
                mock.patch.object(install, "_nearest_folder", return_value=HOME):
            press(state, "enter")
        self.assertEqual(state.screen, "create")
        self.assertEqual(state.dest, Path(os.path.expanduser("~/my bios")))
        press(state, "down", "enter")
        self.assertEqual(state.screen, "folder")

    def test_typed_paths(self):
        with mock.patch.dict(os.environ, {"CARD": "/media/sd"}):
            self.assertEqual(install.typed_path(' "$CARD/Bios" '), Path("/media/sd/Bios"))
        self.assertEqual(install.typed_path("'C:\\RetroBat\\bios'"), Path("C:\\RetroBat\\bios"))
        self.assertIsNone(install.typed_path('""'))

    def test_the_browser_lists_folders_and_goes_up(self):
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("system", ".config", "Bios"):
            (root / name).mkdir()
        (root / "file.bin").write_bytes(b"")
        state = fresh_state()
        state.platform = "retroarch"
        state.stack = ["folder"]
        state.folder_default = root / "system"
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "down", "enter")
            self.assertEqual(state.screen, "browse")
            self.assertEqual(state.browse_dir, root / "system")
            self.assertEqual([row.label for row in install.tui_rows(state)],
                             ["Use this folder", ".."])
            press(state, "down", "enter")
            self.assertEqual(state.browse_dir, root)
            labels = [row.label for row in install.tui_rows(state)]
            self.assertEqual(labels, ["Use this folder", "..", "Bios/", "system/", ".config/"])
            self.assertEqual(install.tui_rows(state)[state.focus["browse"]].label, "system/")
            effects = press(state, "home", "enter")
        self.assertEqual(effects, [("fetch", "retroarch")])
        self.assertEqual(state.dest, root)

    def test_a_holds_no_bios_folder_root_never_takes_a(self):
        """With no usual folder the browser opens at HOME, on Use this
        folder: A five times installed the BIOS files into ~. At HOME or a
        drive's root, A goes into the first folder instead."""
        home = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "Games").mkdir()
        state = install.TuiState(os_type="linux", home=home)
        state.platform = "retroarch"
        state.stack = ["folder"]
        state.folder_default = None
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._browse_to(state, home)
            self.assertEqual(state.browse_rows[state.focus["browse"]].label, "Games/")
            install._browse_to(state, home / "Games")
            self.assertEqual(state.browse_rows[state.focus["browse"]].label,
                             "Use this folder")
            install._browse_to(state, Path(home.anchor))
            self.assertNotEqual(state.browse_rows[state.focus["browse"]].action, "use")

    def test_cards_are_listed_first(self):
        card = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, card, True)
        (card / ".system").mkdir()
        (card / ".system" / "version.txt").write_text("NextUI-20250101\n")
        with mock.patch.object(install, "_mounted_cards", return_value=[card]):
            rows, error = install._browse_rows(REPO_ROOT / "tests")
        self.assertEqual(error, "")
        self.assertEqual(rows[2].label, f"{card} (NextUI card)")

    def test_a_mounted_card_is_a_setup_of_its_own(self):
        card = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, card, True)
        (card / ".system").mkdir()
        (card / ".system" / "version.txt").write_text("MinUI-20250101\n")
        with mock.patch.object(install, "_mounted_cards", return_value=[card]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, effects = install.tui_start(_args(), "linux")
        self.assertEqual((state.screen, effects), ("setup", []))
        label = f"MinUI SD card ({card.name})"
        self.assertEqual(state.setups, [("minui", card, label)])
        self.assertEqual([row.label for row in install.tui_rows(state)],
                         [label, "Another setup or folder"])

    def test_an_error_offers_another_try(self):
        state = fresh_state()
        with mock.patch.object(install.Path, "exists", return_value=True):
            press(state, "enter")
        install.tui_receive(state, ("manifest_failed", "retroarch", "No connection",
                                    ["  Could not fetch the file list for retroarch", ""], 1))
        self.assertIn("RetroBIOS: No connection", install.tui_view(state, 80, 24)[0])
        self.assertEqual(state.screen, "error")
        self.assertEqual([row.action for row in install.tui_rows(state)],
                         ["retry", "elsewhere", "quit"])
        self.assertEqual(press(state, "enter"), [("fetch", "retroarch")])
        install.tui_receive(state, ("manifest_failed", "retroarch", "No connection", ["no"], 1))
        press(state, "down", "down", "enter")
        self.assertEqual(state.exit_code, 1)

    def test_a_check_that_writes_no_check_event_is_an_error(self):
        state = fresh_state()
        state.platform, state.dest = "retroarch", SYSTEM_DIR
        state.manifest, state.manifest_for = MANIFEST, "retroarch"
        install._start_check(state)
        install.tui_receive(state, ("ended", state.run_id, 1,
                                    ["Error: /x is inside a NextUI card."]))
        self.assertEqual(state.screen, "error")
        self.assertEqual(state.error, ["Error: /x is inside a NextUI card."])

    def test_events_of_an_abandoned_run_are_ignored(self):
        state = checked_state()
        press(state, "o")
        state.focus["options"] = 7
        press(state, "enter")
        old = state.run_id
        self.assertEqual(press(state, "esc"), [("kill",)])
        install.tui_receive(state, ("event", old, dict(CHECK)))
        install.tui_receive(state, ("ended", old, 0, []))
        self.assertIsNone(state.dry)
        self.assertEqual(state.screen, "options")

    def test_help_and_a_small_window(self):
        state = fresh_state()
        press(state, "?")
        self.assertTrue(state.help)
        self.assertIn("Any key closes", install.tui_view(state, 80, 24)[-1])
        press(state, "down")
        self.assertFalse(state.help)
        self.assertEqual(state.focus.get("setup", 0), 0)
        state.cols, state.rows = 30, 10
        press(state, "enter")
        self.assertEqual(state.screen, "setup")
        lines = install.tui_view(state, 30, 10)
        self.assertEqual([line.strip() for line in lines if line.strip()],
                         ["Make the window larger:", "40x12 or more, now 30x10"])
        self.assertEqual(len(install.tui_view(state, 39, 1)), 1)
        self.assertEqual(install.tui_view(state, 30, 0), [])


class PlanWarnings(unittest.TestCase):
    def test_a_folder_that_cannot_take_the_files_says_so_first(self):
        state = checked_state({"free": 1024, "writable": False, "cores": []})
        text = " ".join(install.tui_view(state, 120, 40))
        self.assertIn("Not enough free space: 1.0 MB needed.", text)
        self.assertIn("You cannot write to this folder: choose another one.", text)
        # sudo changes the home folder: the advice names this one.
        self.assertIn("Or run it as root, this folder named: sudo python3 install.py "
                      f"--platform retroarch --dest {SYSTEM_DIR}", " ".join(text.split()))
        rows = install.tui_rows(state)
        self.assertEqual([row.action for row in rows[:2]], ["folder", "install"])
        self.assertEqual(state.focus.get("plan", 0), 0)
        self.assertEqual(press(state, "enter"), [])
        self.assertEqual(state.screen, "folder")
        self.assertNotIn("--installed-cores", [row.right for row in install._option_rows(state)])


class Widgets(unittest.TestCase):
    def systems_state(self, count: int) -> install.TuiState:
        state = checked_state()
        state.systems = [{"event": "system", "system": f"system-{n:03d}", "name": f"s{n}",
                          "files": 1, "present": 0, "missing": 1, "wrong_hash": 0,
                          "bytes_needed": 10} for n in range(count)]
        state.ticked = {row["system"] for row in state.systems}
        state.stack.append("systems")
        return state

    def test_a_long_list_scrolls_and_says_where(self):
        state = self.systems_state(112)
        press(state, "end")
        lines = install.tui_view(state, 40, 15)
        self.assertIn("> [x] system-111", "\n".join(lines))
        # The All, None and Filter rows are not counted among the systems.
        self.assertTrue(any(re.fullmatch(r" \d+-112 of 112", line) for line in lines), lines)
        press(state, "pgup")
        self.assertLess(state.focus["systems"], 114)
        press(state, "home")
        self.assertEqual(state.focus["systems"], 0)

    def test_up_wraps_only_in_short_menus(self):
        state = fresh_state()
        press(state, "up")
        self.assertEqual(state.focus["setup"], 2)
        state = self.systems_state(20)
        press(state, "up")
        self.assertEqual(state.focus["systems"], 0)

    def test_filter_ignores_case_and_accents(self):
        state = self.systems_state(3)
        state.systems.append({"system": "sega-genesis", "name": "Mégadrive", "files": 1,
                              "present": 0, "missing": 1, "wrong_hash": 0,
                              "bytes_needed": 3})
        press(state, "/", "M", "E", "G")
        self.assertEqual([row.value for row in install.tui_rows(state)][3:], ["sega-genesis"])
        press(state, "esc")
        self.assertEqual(state.filters["systems"], "")
        self.assertEqual(state.screen, "systems")
        press(state, "/", "z", "z", "enter")
        self.assertIn("No system matches 'zz'.", "\n".join(install.tui_view(state, 80, 24)))
        # Esc clears a kept filter first, and goes back only then.
        press(state, "esc")
        self.assertEqual((state.screen, state.filters["systems"]), ("systems", ""))
        press(state, "esc")
        self.assertEqual(state.screen, "plan")

    def test_all_and_none(self):
        state = self.systems_state(5)
        press(state, "n")
        self.assertEqual(state.ticked, set())
        press(state, "a")
        self.assertEqual(len(state.ticked), 5)
        state.focus["systems"] = 1
        # X on None does nothing: only A, an explicit choice, unticks all.
        press(state, " ")
        self.assertEqual(len(state.ticked), 5)
        press(state, "enter")
        self.assertEqual(state.ticked, set())

    def test_running_total_counts_each_file_once(self):
        state = checked_state()
        state.stack.append("systems")
        state.files.append({"event": "file", "dest": "shared.bin", "size": 1024 * 1024,
                            "systems": ["nintendo-gba", "sony-playstation"],
                            "state": "missing"})
        footer = install.tui_view(state, 80, 24)[-2]
        self.assertEqual(footer.strip(), "2 of 3 systems: 5 files, 2.0 MB")
        state.ticked = {"nintendo-gba"}
        footer = install.tui_view(state, 80, 24)[-2]
        self.assertEqual(footer.strip(), "1 of 3 systems: 2 files, 1.0 MB")


def golden_states() -> dict:
    """One state per screen, filled as a run fills it."""
    states = {}
    states["setup"] = fresh_state()
    states["setup"].notes = ["ES-DE found: its BIOS files go to each emulator, not to "
                             "ES-DE itself."]
    platforms = fresh_state()
    platforms.stack = ["setup", "platforms"]
    platforms.focus["platforms"] = 1
    states["platforms"] = platforms
    folder = fresh_state()
    folder.platform, folder.stack = "retroarch", ["platforms", "folder"]
    folder.folder_default = SYSTEM_DIR
    states["folder"] = folder
    card = fresh_state()
    card.platform, card.stack = "nextui", ["platforms", "folder"]
    states["folder-card"] = card
    browse = fresh_state()
    browse.platform, browse.stack = "retroarch", ["folder", "browse"]
    browse.browse_dir = HOME / ".config" / "retroarch"
    browse.browse_rows = [
        install.TuiRow("use", "Use this folder"), install.TuiRow("up", "..", value=HOME),
        install.TuiRow("dir", "/media/user/SDCARD (NextUI card)",
                       value=Path("/media/user/SDCARD")),
        install.TuiRow("dir", "cores/", value=HOME / "cores"),
        install.TuiRow("dir", "system/", value=SYSTEM_DIR)]
    states["browse"] = browse
    path = fresh_state()
    path.platform, path.stack, path.path_text = "retroarch", ["folder", "path"], "~/bios"
    states["path"] = path
    create = fresh_state()
    create.platform, create.dest, create.stack = "retroarch", HOME / "bios" / "new", ["create"]
    create.facts = {"nearest": HOME}
    states["create"] = create
    checking = fresh_state()
    checking.platform, checking.dest, checking.stack = "retroarch", SYSTEM_DIR, ["checking"]
    checking.manifest, checking.started, checking.now = MANIFEST, 10.0, 14.2
    states["checking"] = checking
    states["plan"] = checked_state()
    done = checked_state()
    done.check = {**CHECK, "present": 5, "missing": 0, "wrong_hash": 0, "bytes_needed": 0,
                  "not_installable": 0}
    states["plan-in-place"] = done
    systems = checked_state()
    systems.stack.append("systems")
    systems.focus["systems"] = 3
    states["systems"] = systems
    options = checked_state()
    options.stack.append("options")
    options.regions = ["north-america", "japan"]
    states["options"] = options
    regions = checked_state()
    regions.stack += ["options", "regions"]
    regions.regions = ["north-america"]
    states["regions"] = regions
    cores = checked_state()
    cores.stack += ["options", "cores"]
    cores.cores = ["pcsx_rearmed"]
    states["cores"] = cores
    preview = checked_state()
    preview.stack += ["options", "preview"]
    preview.dry, preview.dry_files = dict(CHECK), list(FILES)
    states["preview"] = preview
    command = checked_state()
    command.stack += ["options", "command"]
    command.ticked = {"nintendo-gba"}
    states["command"] = command
    confirm = checked_state()
    confirm.stack.append("confirm")
    confirm.ticked = {"nintendo-gba"}
    confirm.dry = {**CHECK, "files": 1, "present": 0, "missing": 1, "wrong_hash": 0,
                   "bytes_needed": 16384, "not_installable": 0}
    states["confirm"] = confirm
    installing = checked_state()
    installing.stack.append("installing")
    installing.run, installing.running = "install", True
    installing.download = {"event": "download", "files": 4, "bytes": 1064964}
    installing.samples, installing.now = [(100.0, 12672), (104.0, 540672)], 104.0
    installing.network = 540672
    installing.progress = {"files_done": 2, "files_total": 4, "bytes_done": 540672,
                           "bytes_total": 1064964}
    installing.finished = [("gba_bios.bin", True, ""), ("scph5500.bin", False, "HTTP 404")]
    states["installing"] = installing
    result = checked_state()
    result.stack.append("result")
    result.code = 1
    result.log = "/tmp/retrobios-x/install.log"
    result.download = installing.download
    result.summary = {
        "event": "summary", "mode": "install", "installed": 3, "up_to_date": 1, "pending": 1,
        "failed": [{"dest": "scph5500.bin", "reason": "HTTP 404"}],
        "skipped": [{"reason": "not_found", "label": "not found", "files": 2, "required": 1}],
        "left_out": [], "kept": [{"folder": str(HOME / ".config/retroarch/retrobios-backup/"
                                               "2026-10-23_1200"), "files": 1, "bytes": 4}],
        "interrupted": False}
    states["result"] = result
    error = fresh_state()
    error.stack, error.run = ["setup", "error"], "check"
    error.error = ["  Could not fetch the file list for retroarch after 3 attempts: "
                   "[Errno -2] Name or service not known",
                   "  Address: https://raw.githubusercontent.com/Abdess/retrobios/main/"
                   "install/retroarch.json"]
    states["error"] = error
    quitting = fresh_state()
    quitting.stack = ["setup", "quit"]
    states["quit"] = quitting
    helped = checked_state()
    helped.help = True
    states["help"] = helped
    return states


class Views(unittest.TestCase):
    """Every screen as text, at the sizes a handheld and a desktop show."""

    def setUp(self):
        # The card screen lists the cards mounted here: none, on every machine.
        patcher = mock.patch.object(install, "_mounted_cards", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_goldens(self):
        update = os.environ.get("RETROBIOS_UPDATE_GOLDENS") == "1"
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for name, state in golden_states().items():
            for cols, rows in SIZES:
                with self.subTest(screen=name, size=f"{cols}x{rows}"):
                    state.cols, state.rows = cols, rows
                    lines = install.tui_view(state, cols, rows)
                    self.assertEqual(len(lines), rows)
                    for line in lines:
                        # A path or a command kept whole folds in the terminal.
                        if line.style != "long":
                            self.assertLessEqual(install.cell_width(line), cols, line)
                        self.assertTrue(all(ord(c) < 128 or c in "ファミコン" for c in line),
                                        line)
                    self.assertLess(install.cell_width(lines[-1]), cols)
                    text = "\n".join(line.rstrip() for line in lines) + "\n"
                    path = GOLDEN / f"{name}-{cols}x{rows}.txt"
                    if update:
                        path.write_text(text, encoding="utf-8")
                    self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_goldens_per_route(self):
        """Each input route at the handheld sizes and 80x24, kept only where a
        screen reads differently from the both-names form."""
        update = os.environ.get("RETROBIOS_UPDATE_GOLDENS") == "1"
        for route in ROUTES:
            sections = []
            for name, state in golden_states().items():
                state.pad = route
                for cols, rows in SIZES[:3]:
                    with self.subTest(route=route, screen=name, size=f"{cols}x{rows}"):
                        state.cols, state.rows = cols, rows
                        lines = install.tui_view(state, cols, rows)
                        for line in lines:
                            if line.style != "long":
                                self.assertLessEqual(install.cell_width(line), cols, line)
                        self.assertLess(install.cell_width(lines[-1]), cols)
                    text = "\n".join(line.rstrip() for line in lines) + "\n"
                    both = (GOLDEN / f"{name}-{cols}x{rows}.txt").read_text(encoding="utf-8")
                    if text != both:
                        sections.append(f"=== {name} {cols}x{rows}\n{text}")
            path = GOLDEN / f"route-{route}.txt"
            if update:
                path.write_text("".join(sections), encoding="utf-8")
            with self.subTest(route=route):
                self.assertEqual("".join(sections), path.read_text(encoding="utf-8"))

    def test_the_hint_bar_names_the_primary_action_first(self):
        for name, state in golden_states().items():
            with self.subTest(screen=name):
                hint = install.tui_view(state, 40, 15)[-1].strip()
                self.assertTrue(hint.startswith(("A/Enter", "A ", "Enter", "Ctrl-C", "Any key",
                                                 "B/Esc Stop")), hint)

    def test_focus_is_marked_without_colour(self):
        lines = install.tui_view(fresh_state(), 80, 24)
        focused = [line for line in lines if getattr(line, "style", "") == "focus"]
        self.assertEqual(len(focused), 1)
        self.assertTrue(focused[0].lstrip().startswith("> RetroArch"))


def _bar_keys(line: str) -> set:
    """The key each entry of a hint bar names: its first word."""
    return {part.split(" ", 1)[0] for part in line.strip().split("  ") if part}


# The keys each route has, as the bar may name them. "Any" opens "Any key
# closes this help", <> and ^v are the narrow forms of Left/Right, Up/Down.
ROUTE_KEYS = {
    "keys": {"Enter", "Esc", "Space", "o", "?", "/", "Ctrl-C", "Ctrl-U"},
    "gptokeyb": {"A", "B", "X", "Y", "Select+Start"},
    "gptokeyb_muxterm": {"A", "B", "X", "Y", "Select+Start"},
    "evmapy": {"A", "B", "X", "Y"},
    "muxterm": {"Start", "B", "Y", "Menu", "Select"},
    "miyoo": {"A", "Start", "R2", "L2"},
}
EVERY_ROUTE_HAS = {"Left/Right", "Up/Down", "<>", "^v", "Any"}


class PadRoutes(unittest.TestCase):
    """The bar names the buttons the launcher maps, and a desktop's keys."""

    def test_the_route_from_the_environment(self):
        route = install.tui_pad_route
        for value in install.TUI_PAD_ROUTES:
            self.assertEqual(route("linux", {"RETROBIOS_PAD": value, "DISPLAY": ":0"}), value)
        self.assertEqual(route("linux", {"RETROBIOS_PAD": " MiYoo "}), "miyoo")
        self.assertEqual(route("linux", {"RETROBIOS_PAD": "dualshock"}), "both")
        for os_type in ("windows", "darwin", "wsl"):
            self.assertEqual(route(os_type, {}), "keys", os_type)
        for env in ({"DISPLAY": ":0", "TERM": "xterm-256color"}, {"WAYLAND_DISPLAY": "w-0"},
                    {"SSH_CONNECTION": "10.0.0.2 5 10.0.0.3 22"}, {"SSH_TTY": "/dev/pts/1"}):
            self.assertEqual(route("linux", env), "keys", env)
        # SteamOS game mode and anything Steam starts: Steam Input may be
        # all there is. A console with nothing named, or Android: unknown.
        for os_type, env in (("linux", {"SteamGamepadUI": "1", "DISPLAY": ":0"}),
                             ("linux", {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0",
                                        "WAYLAND_DISPLAY": "w-0"}),
                             ("windows", {"SteamGameId": "123"}),
                             ("linux", {"SteamDeck": "1", "DISPLAY": ":0"}),
                             ("linux", {"TERM": "linux", "SSH_TTY": "/dev/pts/0"}),
                             ("linux", {"TERM": "linux"}), ("linux", {}),
                             ("android", {"DISPLAY": ":0"})):
            self.assertEqual(route(os_type, env), "both", env)

    def test_the_launchers_values_are_the_routes(self):
        tui = (REPO_ROOT / "ports" / "shared" / "tui.txt").read_text(encoding="utf-8")
        named = set(re.findall(r'RB_PAD="(\w+)"', tui))
        for launcher in REPO_ROOT.glob("ports/*/*.sh"):
            named |= set(re.findall(r'RB_PAD="(\w+)"', launcher.read_text(encoding="utf-8")))
        self.assertLessEqual(named, set(install.TUI_PAD_ROUTES))
        self.assertEqual(set(ROUTES) - {"keys"}, set(install.TUI_PAD_ROUTES))

    def test_the_plan_bar_of_each_route(self):
        expected = {
            "both": "A/Enter Install  B/Esc Back  Y/o Options  ? Help",
            "keys": "Enter Install  Esc Back  o Options  ? Help",
            "gptokeyb": "A Install  B Back  Y Options",
            "evmapy": "A Install  B Back  Y Options",
            "muxterm": "Start Install  B Back",
            "miyoo": "A Install  R2 Back  L2 Options",
        }
        for route, bar in expected.items():
            with self.subTest(route=route):
                state = checked_state()
                state.pad = route
                self.assertEqual(install.tui_view(state, 80, 24)[-1].strip(), bar)

    def test_no_bar_names_a_key_its_route_lacks(self):
        for route, have in ROUTE_KEYS.items():
            for name, state in golden_states().items():
                state.pad = route
                for cols, rows in SIZES:
                    with self.subTest(route=route, screen=name, size=f"{cols}x{rows}"):
                        state.cols, state.rows = cols, rows
                        bar = install.tui_view(state, cols, rows)[-1]
                        self.assertLessEqual(_bar_keys(bar) - EVERY_ROUTE_HAS, have, bar)

    def test_the_install_says_what_stops_it_there(self):
        expected = {
            "both": "B, Ctrl-C or Select+Start stops.", "keys": "Esc or Ctrl-C stops.",
            "gptokeyb": "B or Select+Start stops.", "evmapy": "B stops.",
            "muxterm": "B stops.", "miyoo": "R2 stops.",
        }
        for route, words in expected.items():
            with self.subTest(route=route):
                state = golden_states()["installing"]
                state.pad = route
                text = "\n".join(install.tui_view(state, 80, 24))
                self.assertIn(f"{words} What is done stays done.", text)
                state.stopping = True
                again = {"both": "B or Ctrl-C", "keys": "Esc or Ctrl-C", "miyoo": "R2"}
                self.assertIn(f"Press {again.get(route, 'B')} again to stop now.",
                              " ".join(" ".join(install.tui_view(state, 80, 24)).split()))

    def test_the_help_lists_the_buttons_and_the_keys(self):
        def help_text(route: str, screen: str = "plan") -> str:
            state = golden_states()[screen]
            state.pad, state.help = route, True
            return "\n".join(line.rstrip() for line in install.tui_view(state, 80, 24))

        gptk = help_text("gptokeyb")
        for words in ("D-pad, Up/Down", "A, Start, Enter", "B, Select, Esc", "Y, Tab, o",
                      "Select, then Start", "Quit at once", "q, Ctrl-C"):
            self.assertIn(words, gptk)
        self.assertNotIn("Select+Start", help_text("evmapy"))
        keys = help_text("keys")
        for words in ("Enter", "Esc, Backspace", "o, Tab"):
            self.assertIn(words, keys)
        for button in ("A/Enter", "B/Esc", "L1", "Start", "Y,"):
            self.assertNotIn(button, keys)
        muxterm = help_text("muxterm")
        for words in ("Start, Enter", "B, Esc, Backspace", "Keyboard on or off",
                      "Close the terminal"):
            self.assertIn(words, muxterm)
        self.assertNotIn("Select+Start", muxterm)
        # gptokeyb2 owns the pad inside muxterm, which keeps PgUp and PgDn
        # for its scrollback: the gptokeyb buttons, Left and Right to page.
        in_muxterm = help_text("gptokeyb_muxterm", "preview")
        for words in ("A, Start, Enter", "Select, then Start"):
            self.assertIn(words, in_muxterm)
        self.assertNotIn("L1/R1", in_muxterm)
        self.assertIn("L1/R1", help_text("gptokeyb", "preview"))
        # muxterm keeps PgUp and PgDn for its scrollback, and L1 and R1
        # send nothing: the D-pad's Left and Right page there instead.
        for screen in ("systems", "result", "plan"):
            with self.subTest(screen=screen):
                text = help_text("muxterm", screen)
                for words in ("L1", "R1", "PgUp", "PgDn"):
                    self.assertNotIn(words, text)
        self.assertIn("D-pad Left/Right   Page", help_text("muxterm", "result"))
        miyoo = help_text("miyoo", "systems")
        for words in ("Start, Enter", "A, Space, Right/Left", "R2, Esc, Backspace",
                      "L2, Tab, o", "Sends no key here"):
            self.assertIn(words, miyoo)
        # The both-names form is the one it was.
        self.assertEqual(help_text("both"), (GOLDEN / "help-80x24.txt").read_text().rstrip("\n"))

    def test_a_miyoo_a_chooses_and_ticks(self):
        """A is Space on a Miyoo: it chooses where Enter does, ticks where X
        would, and L1 and R1 (E and T) page."""
        state = checked_state()
        state.pad = "miyoo"
        self.assertEqual(press(state, " "), [("run", "install")])
        state = checked_state()
        state.pad = "miyoo"
        state.stack.append("systems")
        rows = install.tui_rows(state)
        first = next(i for i, row in enumerate(rows) if row.action == "system")
        state.focus["systems"] = first
        before = set(state.ticked)
        press(state, " ")
        self.assertNotEqual(state.ticked, before)
        state.focus["systems"] = next(i for i, row in enumerate(rows) if row.action == "all")
        press(state, " ")
        self.assertEqual(state.ticked, {row["system"] for row in state.systems})
        state.focus["systems"] = first
        press(state, "t")
        self.assertGreater(state.focus["systems"], first)
        press(state, "e")
        self.assertEqual(state.focus["systems"], 0)
        # In a text field the letters are letters.
        state.editing = "systems"
        press(state, "t")
        self.assertEqual(state.filters["systems"], "t")

    def test_space_stays_x_on_the_other_routes(self):
        for route in ("both", "keys", "gptokeyb", "evmapy", "muxterm"):
            with self.subTest(route=route):
                state = checked_state()
                state.pad = route
                self.assertEqual(press(state, " "), [("run", "confirm")])


class Rendering(unittest.TestCase):
    def test_only_changed_lines_are_written(self):
        out = io.StringIO()
        terminal = install._TuiTerminal.__new__(install._TuiTerminal)
        terminal.stdout, terminal.encoding, terminal.frame = out, "ascii", []
        state = fresh_state()
        terminal.draw(install.tui_view(state, 40, 12), 40)
        first = out.getvalue()
        self.assertTrue(first.startswith("\x1b[H\x1b[2J"))
        press(state, "down")
        out.seek(0)
        out.truncate()
        terminal.draw(install.tui_view(state, 40, 12), 40)
        changed = out.getvalue()
        self.assertEqual(changed.count("\x1b[7m"), 1)
        before = install.tui_view(fresh_state(), 40, 12)
        after = install.tui_view(state, 40, 12)
        moved = [f"\x1b[{i + 1};1H" for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(changed.count(";1H"), len(moved))
        for move in moved:
            self.assertIn(move, changed)

    def test_text_the_terminal_cannot_encode_is_replaced(self):
        out = io.StringIO()
        terminal = install._TuiTerminal.__new__(install._TuiTerminal)
        terminal.stdout, terminal.encoding, terminal.frame = out, "ascii", []
        terminal.write("ファ")
        self.assertEqual(out.getvalue(), "??")


class TheSectionStandsApart(unittest.TestCase):
    def test_no_name_of_the_installer_is_defined_again(self):
        """_run and _system_label were defined twice: the later one won, and
        the installer's listing called the interface's."""
        import ast

        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        start = source[:source.index("# Full-screen interface\n#")].count("\n") + 1
        seen: dict = {}
        for node in ast.parse(source).body:
            names = [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [
                target.id for target in getattr(node, "targets", [])
                if isinstance(target, ast.Name)]
            for name in names:
                seen.setdefault(name, []).append(node.lineno)
        twice = {name: lines for name, lines in seen.items()
                 if len(lines) > 1 and max(lines) >= start}
        self.assertEqual(twice, {})


class ControlCharacters(unittest.TestCase):
    """Names from the file list, the folders on disk and the installer's own
    words reach the screen: an escape sequence among them would drive the
    terminal (clear it, or set the clipboard with OSC 52)."""

    HOSTILE = "a\x1b[2Jb\x1b]52;c;x\x07c\rd\ne\x7ff\x9bg\x85h"

    def test_the_cleaning(self):
        self.assertEqual(install.screen_text(self.HOSTILE), "a?[2Jb?]52;c;x?c?d?e?f?g?h")
        self.assertEqual(install.screen_text("ファミコン é"), "ファミコン é")

    def assertClean(self, lines) -> None:
        for line in lines:
            self.assertIsNone(re.search(r"[\x00-\x1f\x7f-\x9f]", line), repr(line))

    def test_a_file_name_in_the_preview(self):
        state = checked_state()
        state.stack += ["options", "preview"]
        state.dry = dict(CHECK)
        state.dry_files = [{**FILES[0], "dest": "\x1b[2Jevil.bin"}]
        lines = install.tui_view(state, 80, 24)
        self.assertClean(lines)
        self.assertIn("?[2Jevil.bin", "\n".join(lines))

    def test_a_folder_name_in_the_browser(self):
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "x\x1b]52;c;x\x07").mkdir()
        state = fresh_state()
        state.platform, state.stack = "retroarch", ["folder", "browse"]
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._browse_to(state, root)
        lines = install.tui_view(state, 80, 24)
        self.assertClean(lines)
        self.assertIn("x?]52;c;x?/", "\n".join(lines))

    def test_the_child_tail_on_the_error_screen(self):
        state = fresh_state()
        state.stack, state.run = ["setup", "error"], "check"
        state.error = ["\x1b[31mError\x1b[0m: \x07bad\r"]
        self.assertClean(install.tui_view(state, 40, 15))

    def test_the_renderer_guards_whatever_reaches_it(self):
        out = io.StringIO()
        terminal = install._TuiTerminal.__new__(install._TuiTerminal)
        terminal.stdout, terminal.encoding = out, "utf-8"
        terminal.frame = [("", "")] * 3
        terminal.draw(["x", "\x1b]52;c;x\x07", "z"], 40)
        self.assertNotIn("\x1b]", out.getvalue())
        self.assertNotIn("\x07", out.getvalue())

    def test_the_farewell_is_clean(self):
        state = checked_state()
        state.dest = Path("/media/\x1b[2J")
        state.stack.append("result")
        state.code = 0
        state.summary = {"installed": 1, "failed": [{"dest": "\x1b]0;t\x07x", "reason": "\x1b"}]}
        self.assertClean(install.tui_farewell(state))


class SmallFixes(unittest.TestCase):
    def test_a_system_name_is_said_once_with_a_pack(self):
        """tui_audit_5 P3-8.1: "Onion gets the RetroArch file list" was
        printed by the pack check, then again where the platform is read."""
        pack = mock.Mock(files=[Path("RetroArch_BIOS_Pack.zip")])
        pack.name = "RetroArch_BIOS_Pack.zip"
        pack.platforms.return_value = ["retroarch"]
        args = argparse.Namespace(manifest=None, from_pack=Path("p.zip"), platform="onion",
                                  **{"from": None})
        with mock.patch.object(install, "PackSource", return_value=pack), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            install._open_local_sources(args)
        self.assertNotIn("Onion gets", out.getvalue())
        install.LOCAL.reset()

    def test_a_command_line_region_the_list_lacks(self):
        state = checked_state()
        state.cli_regions = ["europe"]
        state.regions = list(state.cli_regions)
        state.stack += ["options", "regions"]
        self.assertEqual([row.value for row in install.tui_rows(state)],
                         ["europe", "japan", "north-america"])
        press(state, "enter")
        self.assertEqual(state.regions, [])
        self.assertIn("europe", [row.value for row in install.tui_rows(state)])
        press(state, "enter")
        self.assertEqual(state.regions, ["europe"])

    def test_stay_after_esc_on_a_direct_check_checks_again(self):
        state = fresh_state()
        state.stack = []
        state.manifest, state.manifest_for = MANIFEST, "retroarch"
        install._choose(state, "retroarch", Path(REPO_ROOT))
        self.assertEqual(state.screen, "checking")
        self.assertEqual(press(state, "esc"), [("kill",)])
        self.assertEqual(state.screen, "quit")
        self.assertEqual(press(state, "down", "enter"), [("run", "check")])
        self.assertEqual((state.stack, state.running), (["checking"], True))
        press(state, "esc")
        self.assertEqual(press(state, "esc"), [("run", "check")])

    def test_speed_ignores_a_resumed_partial(self):
        state = checked_state()
        press(state, "enter")
        mega = 1024 * 1024
        state.now = 9.0
        install.tui_receive(state, ("event", state.run_id, {
            "event": "download", "platform": "retroarch", "files": 2, "bytes": 1000 * mega}))
        # The first event carries the 500 MB already on disk.
        for when, done in ((10.0, 500 * mega), (11.0, 501 * mega), (12.0, 502 * mega)):
            state.now = when
            install.tui_receive(state, ("event", state.run_id, {
                "event": "progress", "files_done": 0, "files_total": 2,
                "bytes_done": done, "bytes_total": 1000 * mega}))
            # The partial copy resumed is counted local by the engine.
            install.tui_receive(state, ("event", state.run_id, {
                "event": "transfer", "bytes_local": 500 * mega,
                "bytes_network": done - 500 * mega}))
        text = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn("1.0 MB/s, 8:18 left", text)

    def test_numbers_from_before_the_options_say_so(self):
        state = checked_state()
        state.stack.append("systems")
        self.assertNotIn("before your options", "\n".join(install.tui_view(state, 80, 24)))
        state.regions = ["japan"]
        footer = "\n".join(install.tui_view(state, 80, 24)[-3:])
        self.assertIn("before your options", footer)

    def test_a_cores_folder_implies_the_installed_cores(self):
        state = checked_state()
        state.passthrough = [("--cores-dir", "/opt/cores")]
        state.installed_only = True
        state.stack.append("options")
        row = next(r for r in install.tui_rows(state) if r.action == "installed")
        self.assertEqual(row.label, "Installed cores only: yes, implied by --cores-dir")
        state.focus["options"] = install.tui_rows(state).index(row)
        press(state, "enter", "left")
        self.assertTrue(state.installed_only)
        self.assertNotIn("--installed-cores", [f for f, _v in install.tui_flags(state)])

    def test_the_end_of_a_wrapped_pager_is_reachable(self):
        state = checked_state()
        state.stack.append("result")
        state.code, state.summary = 0, {"installed": 3}
        state.dest = Path("/media/" + "long-folder-name/" * 8)
        state.cols, state.rows = 40, 12
        press(state, "end")
        lines = install.tui_view(state, 40, 12)
        self.assertIn("python3 install.py", "\n".join(lines))
        top = state.focus["result"]
        press(state, "up")
        self.assertEqual(state.focus["result"], top - 1)

    def test_end_then_up_in_a_pager(self):
        state = checked_state()
        state.stack += ["options", "preview"]
        state.dry, state.dry_files = dict(CHECK), list(FILES) * 10
        state.cols, state.rows = 40, 15
        press(state, "end")
        last = install.tui_view(state, 40, 15)
        press(state, "up")
        self.assertNotEqual(install.tui_view(state, 40, 15), last)

    def test_a_paste_slower_than_the_escape_wait_stays_one_piece(self):
        decoder = install.KeyDecoder()
        self.assertEqual(decoder.feed("\x1b[200~/media/"), [])
        self.assertEqual(decoder.flush(), [])
        self.assertTrue(decoder.waiting)
        self.assertEqual(decoder.feed("sd\x1b[201~"), [Key("paste", "/media/sd")])

    def test_values_cannot_be_read_as_options(self):
        state = checked_state()
        state.dest = Path("-rf")
        state.cores = ["-x"]
        argv = install.tui_argv(state, "install", "py", "i.py")
        self.assertIn("--dest=-rf", argv)
        self.assertIn("--core=-x", argv)
        self.assertIn("--platform=retroarch", argv)

    def test_the_second_ctrl_c_is_announced_and_the_result_stays_honest(self):
        state = checked_state()
        press(state, "enter")
        install.tui_receive(state, ("event", state.run_id, {
            "event": "download", "platform": "retroarch", "files": 4, "bytes": 100}))
        install.tui_receive(state, ("event", state.run_id, {
            "event": "downloaded", "dest": "a.bin", "ok": True, "reason": "", "size": 1,
            "files_done": 1, "files_total": 4, "bytes_done": 1, "bytes_total": 100}))
        press(state, "ctrl-c")
        self.assertIn("Press B or Ctrl-C again to stop now.",
                      " ".join(install.tui_view(state, 120, 40)))
        self.assertEqual(press(state, "ctrl-c"), [("kill",)])
        install.tui_receive(state, ("ended", state.run_id, -9, []))
        title, lines = install.tui_outcome(state)
        self.assertEqual((title, state.code), ("Stopped", 130))
        self.assertEqual(lines[0], "Stopped: 1 of 4 installed, run again to finish.")

    def test_systems_show_the_platform_names(self):
        state = checked_state()
        state.systems.append({"event": "system", "system": "sega-saturn",
                              "name": "Sega - Saturn", "files": 1, "present": 0,
                              "missing": 1, "wrong_hash": 0, "bytes_needed": 5})
        state.ticked.add("sega-saturn")
        state.stack.append("systems")
        wide = "\n".join(install.tui_view(state, 120, 40))
        self.assertIn("[x] Sega - Saturn", wide)
        self.assertIn("sega-saturn", wide)
        narrow = "\n".join(install.tui_view(state, 40, 15))
        self.assertIn("[x] Sega - Saturn", narrow)
        self.assertNotIn("sega-saturn", narrow)
        press(state, "/", "s", "a", "t", "u")
        self.assertEqual([row.value for row in install.tui_rows(state)][3:], ["sega-saturn"])


class CleanupSurvivesABrokenTerminal(unittest.TestCase):
    """A write that fails (EIO when the window closed, EPIPE) on the way out
    left the terminal raw and the handlers installed."""

    class Tty(io.StringIO):
        broken = False

        def __init__(self, fd: int) -> None:
            super().__init__()
            self.fd = fd

        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return self.fd

        def write(self, text: str) -> int:
            if CleanupSurvivesABrokenTerminal.Tty.broken:
                raise OSError(5, "Input/output error")
            return super().write(text)

    def test_termios_and_handlers_come_back(self):
        import signal

        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        before = termios.tcgetattr(slave)
        handlers = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM,
                                                     signal.SIGHUP)}
        # Keys come in on the terminal itself, the screen goes out on a
        # descriptor of its own, as fd 0 and fd 1 are.
        tty, screen_fd = self.Tty(slave), os.dup(slave)
        self.addCleanup(os.close, screen_fd)
        out = self.Tty(screen_fd)
        real_draw = install._TuiTerminal.draw

        def breaking_draw(terminal, lines, cols):
            terminal.frame = []
            real_draw(terminal, lines, cols)
            CleanupSurvivesABrokenTerminal.Tty.broken = True

        self.addCleanup(setattr, CleanupSurvivesABrokenTerminal.Tty, "broken", False)
        work = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, work, True)
        with mock.patch.object(install._TuiTerminal, "draw", breaking_draw), \
                mock.patch.object(install.tempfile, "mkdtemp", return_value=str(work)), \
                mock.patch.object(install, "tui_start", return_value=(fresh_state(), [])), \
                mock.patch.object(install, "_open_local_sources"), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", out):
            code = install.run_tui(_args())
            self.assertIs(sys.stdout, out)
        # A window that answers EIO is a closed window: its signal's code,
        # not a crash.
        self.assertEqual(code, 129)
        # The farewell found no terminal: what still flushes at exit goes to
        # the null device, and the exit code stays the one returned.
        self.assertEqual(os.fstat(screen_fd).st_rdev, os.stat(os.devnull).st_rdev)
        # No crash log is kept, and nothing else of the run.
        self.assertFalse(work.exists())
        self.assertEqual(termios.tcgetattr(slave), before)
        for number, handler in handlers.items():
            self.assertEqual(signal.getsignal(number), handler)


class AuditRoundTwo(unittest.TestCase):
    """tui_audit_1.md, each finding held where it was found."""

    def systems(self) -> install.TuiState:
        state = checked_state()
        press(state, "down", "enter")
        self.assertEqual(state.screen, "systems")
        return state

    def focused(self, state: install.TuiState) -> install.TuiRow:
        rows = install.tui_rows(state)
        return rows[state.focus[state.screen]]

    def test_p1_3_the_filter_never_changes_the_ticks(self):
        state = self.systems()
        press(state, "n")
        state.focus["systems"] = next(i for i, row in enumerate(install.tui_rows(state))
                                      if row.value == "nintendo-gba")
        press(state, "right")
        self.assertEqual(state.ticked, {"nintendo-gba"})
        press(state, "/", "p", "l", "a", "y")
        self.assertEqual(self.focused(state).action, "filter")
        press(state, "enter")
        # Enter lands on the first match, never on All or None.
        self.assertEqual(self.focused(state).value, "sony-playstation")
        press(state, " ")
        self.assertEqual(state.ticked, {"nintendo-gba", "sony-playstation"})
        press(state, " ")
        press(state, "esc")
        # The filter is cleared, the row in focus stays in focus, and A there
        # continues instead of ticking everything.
        self.assertEqual(state.filters["systems"], "")
        self.assertEqual(self.focused(state).value, "sony-playstation")
        self.assertEqual(state.ticked, {"nintendo-gba"})
        self.assertIn("A/Enter Continue", install.tui_view(state, 80, 24)[-1])
        state.focus["systems"] = 2
        press(state, " ")
        self.assertEqual(state.editing, "")
        self.assertEqual(state.ticked, {"nintendo-gba"})

    def test_p2_2_the_bar_names_what_a_does_on_the_focused_row(self):
        state = checked_state()
        self.assertTrue(install.tui_view(state, 80, 24)[-1].strip().startswith("A/Enter Install"))
        press(state, "down")
        self.assertTrue(install.tui_view(state, 80, 24)[-1].strip().startswith("A/Enter Open"))
        state = self.systems()
        state.focus["systems"] = 0
        self.assertIn("A/Enter Tick all", install.tui_view(state, 80, 24)[-1])

    def test_p2_2_b_esc_and_q_stop_an_install(self):
        for key in ("esc", "backspace", "q"):
            with self.subTest(key=key):
                state = checked_state()
                press(state, "enter")
                self.assertEqual(press(state, key), [("stop",)])
                self.assertTrue(state.stopping)
                self.assertEqual(press(state, key), [("kill",)])

    def test_p2_3_options_read_the_plan_again(self):
        state = self.systems()
        press(state, "n")
        state.focus["systems"] = 3
        press(state, "right")
        ticked = set(state.ticked)
        press(state, "esc", "o")
        self.assertEqual(state.screen, "options")
        state.focus["options"] = 1
        press(state, "enter")
        self.assertTrue(state.no_game_data)
        effects = press(state, "esc")
        self.assertEqual(effects, [("run", "check")])
        self.assertIn("--no-game-data", [f for f, _v in state.checked_flags])
        self.assertNotIn("--system", [f for f, _v in state.checked_flags])
        for event in [*FILES, *SYSTEMS, {**CHECK, "missing": 2, "bytes_needed": 600}]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={}):
            install.tui_receive(state, ("ended", state.run_id, 1, []))
        self.assertEqual(state.screen, "plan")
        self.assertEqual(state.ticked, ticked)
        for cols, rows in ((80, 24), (40, 15)):
            text = "\n".join(install.tui_view(state, cols, rows))
            # tui_audit_6 P3-4: the headline counts the ticked systems.
            self.assertIn("About 1 file, 16.0 KB, for 1 of 3 systems", " ".join(text.split()))
            self.assertNotIn("3 files to download", text)

    def test_p2_4_no_network_is_said_at_once(self):
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        inbox = __import__("queue").Queue()
        started = time.monotonic()
        with mock.patch.object(install, "MANIFEST_URL",
                               f"http://127.0.0.1:{port}/install/{{platform}}.json"):
            install._tui_fetch("retroarch", inbox, io.StringIO())
        self.assertLess(time.monotonic() - started, 5)
        kind, plat, title, lines, code = inbox.get_nowait()
        self.assertEqual((kind, title, code), ("manifest_failed", "No connection", 1))
        self.assertIn("could not be downloaded: the server refused the connection", lines[0])
        self.assertNotIn("--", " ".join(lines))
        state = fresh_state()
        with mock.patch.object(install.Path, "exists", return_value=True):
            press(state, "enter")
        install.tui_receive(state, (kind, plat, title, lines, code))
        self.assertEqual([row.action for row in install.tui_rows(state)],
                         ["retry", "elsewhere", "quit"])

    def test_p2_4_the_clock_says_so(self):
        import ssl

        inbox = __import__("queue").Queue()
        error = urllib_error(ssl.SSLCertVerificationError("certificate is not yet valid"))
        words = install.network_words(error, today=install.datetime.date(1980, 1, 1))
        with mock.patch.object(install, "_fetch_json", side_effect=error), \
                mock.patch.object(install, "network_words", return_value=words):
            install._tui_fetch("retroarch", inbox, io.StringIO())
        _kind, _plat, title, lines, _code = inbox.get_nowait()
        self.assertEqual(title, "The clock is wrong")
        self.assertIn("The clock reads 1980-01-01", lines[0])

    def test_p2_5_one_line_per_cause(self):
        state = checked_state()
        state.stack.append("result")
        state.code = 2
        state.summary = {"installed": 0, "up_to_date": 0, "failed": [
            {"dest": f"f{n}.bin", "reason": "folder not writable"} for n in range(50)]}
        _title, lines = install.tui_outcome(state)
        self.assertEqual([line for line in lines if "failed" in line],
                         ["Nothing was installed: 50 files failed.",
                          "  50 files failed, all for one reason: folder not writable."])

    def test_p2_6_a_folder_inside_a_card_offers_its_root(self):
        card = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, card, True)
        (card / ".system").mkdir()
        (card / ".system" / "version.txt").write_text("MinUI-20250101\n")
        (card / "Bios").mkdir()
        state = fresh_state()
        state.stack = ["folder"]
        self.assertEqual(install._choose(state, "minui", card / "Bios"), [])
        self.assertEqual(state.screen, "cardroot")
        text = " ".join(line.strip() for line in install.tui_view(state, 120, 40))
        self.assertIn(f"under the card's root: {card}", text)
        self.assertEqual(press(state, "enter"), [("fetch", "minui")])
        self.assertEqual(state.dest, card)

    def test_p2_6_the_banner_is_not_an_error_line(self):
        state = fresh_state()
        state.platform, state.dest = "retroarch", SYSTEM_DIR
        state.manifest, state.manifest_for = MANIFEST, "retroarch"
        install._start_check(state)
        install.tui_receive(state, ("ended", state.run_id, 1,
                                    ["RetroBIOS 532f0b8d96e0", "Error: refused."]))
        self.assertEqual(state.error, ["Error: refused."])
        self.assertNotIn("retry", [row.action for row in install.tui_rows(state)])

    def test_p2_7_a_typed_path_replaces_the_one_offered(self):
        state = fresh_state()
        state.platform, state.stack = "retroarch", ["folder"]
        state.folder_default = SYSTEM_DIR
        state.focus["folder"] = 2
        press(state, "enter")
        self.assertEqual((state.screen, state.path_selected), ("path", True))
        install.tui_update(state, Key("paste", '"/media/sd card/bios"'))
        self.assertEqual(state.path_text, '"/media/sd card/bios"')
        install.tui_update(state, Key("ctrl-u"))
        self.assertEqual(state.path_text, "")
        install.tui_update(state, Key("paste", '/media/x"'))
        self.assertEqual(install.typed_path(state.path_text), Path("/media/x"))

    def test_p2_7_a_path_from_the_root_is_not_created_on_a(self):
        state = fresh_state()
        state.stack = ["folder"]
        install._choose(state, "retroarch", Path("/no/such/deep/folder"))
        self.assertEqual(state.screen, "create")
        self.assertEqual(self.focused(state).action, "other")
        self.assertIn("Every folder of this path would be made",
                      " ".join(install.tui_view(state, 120, 40)))
        near = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, near, True)
        install._choose(state, "retroarch", near / "bios")
        self.assertEqual(self.focused(state).action, "create")

    def test_p2_9_two_setups_of_one_name(self):
        flatpak = HOME / ".var/app/org.libretro.RetroArch/config/retroarch/system"
        setups = install.tell_apart([("retroarch", flatpak, "RetroArch"),
                                     ("retroarch", SYSTEM_DIR, "RetroArch"),
                                     ("emudeck", HOME / "Emulation/bios", "EmuDeck")])
        self.assertEqual([label for _p, _f, label in setups],
                         ["RetroArch (Flatpak)", "RetroArch (installed)", "EmuDeck"])
        # Two that no package names keep the end of their folder.
        twice = install.tell_apart([("retroarch", SYSTEM_DIR, "RetroArch"),
                                    ("retroarch", HOME / "ra2/system", "RetroArch")])
        self.assertEqual([label for _p, _f, label in twice],
                         ["RetroArch (retroarch/system)", "RetroArch (ra2/system)"])
        state = fresh_state()
        state.setups = setups
        self.assertIn("RetroArch (Flatpak)", "\n".join(install.tui_view(state, 40, 15)))
        # A Flatpak whose config names a system folder outside its sandbox.
        elsewhere = HOME / "bios"
        config = HOME / ".var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg"
        with mock.patch.dict(install._RETROARCH_CONFIGS, {elsewhere: (config, config.parent)}):
            setups = install.tell_apart([("retroarch", elsewhere, "RetroArch"),
                                         ("retroarch", SYSTEM_DIR, "RetroArch")])
        self.assertEqual(setups[0][2], "RetroArch (Flatpak)")

    def test_p2_10_the_check_counts_while_it_reads(self):
        state = fresh_state()
        state.platform, state.dest = "retroarch", SYSTEM_DIR
        state.manifest, state.manifest_for = MANIFEST, "retroarch"
        install._start_check(state)
        install.tui_receive(state, ("event", state.run_id,
                                    {"event": "checking", "files_done": 120, "files_total": 400}))
        self.assertIn("Reading the files already in the folder: 120 of 400",
                      "\n".join(install.tui_view(state, 80, 24)))

    def test_p2_11_a_narrow_bar_keeps_the_keys_to_finish(self):
        state = self.systems()
        self.assertEqual(install.tui_view(state, 40, 15)[-1].strip(),
                         "A Continue  B Back  X Tick  ? Help")

    def test_p2_12_a_device_name_finds_its_setup(self):
        state = fresh_state()
        state.stack = ["platforms"]
        press(state, "/", "m", "u", "o", "s")
        self.assertEqual([row.value for row in install.tui_rows(state)][1:],
                         [("retroarch", "muOS")])

    def test_p2_13_a_long_preview_wraps_only_what_is_in_view(self):
        state = checked_state()
        state.stack += ["options", "preview"]
        state.dry = dict(CHECK)
        state.dry_files = [{**FILES[0], "dest": f"file{n:05d}.bin"} for n in range(8000)]
        state.cols, state.rows = 40, 15
        real = install._text_lines
        calls = []

        def counted(items, width, whole=False):
            calls.append(len(items))
            return real(items, width, whole)

        with mock.patch.object(install, "_text_lines", counted):
            press(state, "end")
            lines = install.tui_view(state, 40, 15)
        self.assertLess(sum(calls), 40)
        self.assertIn("file07999.bin", "\n".join(lines))

    def test_p2_1_titles_and_regions(self):
        state = checked_state()
        state.manifest = {**MANIFEST, "system_titles": {"nintendo-gba": "Game Boy Advance"}}
        state.stack.append("systems")
        text = "\n".join(install.tui_view(state, 40, 15))
        self.assertIn("[x] Game Boy Advance", text)
        self.assertEqual(install.region_title("asia-ntsc"), "Asia (NTSC)")
        self.assertEqual(install.region_title("north-america"), "North America")

    def test_p3_jobs_stop_at_the_ends_and_q_keeps_the_error_code(self):
        state = checked_state()
        state.jobs = 1
        install._option(state, "jobs", -1)
        self.assertEqual(state.jobs, 1)
        state.jobs = 32
        install._option(state, "jobs", 1)
        self.assertEqual(state.jobs, 32)
        state = fresh_state()
        state.stack, state.error_code = ["setup", "error"], 2
        press(state, "q")
        self.assertEqual(state.exit_code, 2)

    def test_p3_ascii_keeps_the_columns(self):
        self.assertEqual(install.ascii_text("Édition ファミ"), "Edition ??????")

    def test_p3_a_refused_tui_says_why_without_the_usage(self):
        proc = install_run("--tui")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.strip().splitlines(),
                         ["install.py: error: --tui cannot open the full-screen interface "
                          "here: the input is not a terminal"])


def urllib_error(reason: BaseException):
    import urllib.error

    return urllib.error.URLError(reason)


class WorkFolders(unittest.TestCase):
    """Every run's folder of logs goes, but the one a failure names."""

    class Terminal:
        state_to = None

        def __init__(self, *_streams) -> None:
            self.frame = []

        def enter(self) -> None:
            pass

        def leave(self) -> None:
            pass

        def size(self) -> tuple:
            return 80, 24

        def draw(self, lines, cols) -> None:
            pass

        def keys(self, timeout):
            WorkFolders.Terminal.state_to(self)
            return [Key("enter")]

    def run_to(self, code: int) -> tuple:
        work = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, work, True)
        state = checked_state()
        state.stack.append("result")
        state.code = code
        state.log = str(work / "install.log")
        state.summary = {"installed": 1, "failed": [] if code == 0 else [
            {"dest": "a.bin", "reason": "HTTP 404"}]}
        WorkFolders.Terminal.state_to = lambda _t: None
        out = io.StringIO()
        with mock.patch.object(install, "_TuiTerminal", self.Terminal), \
                mock.patch.object(install, "tui_start", return_value=(state, [])), \
                mock.patch.object(install, "_open_local_sources"), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install.tempfile, "mkdtemp", return_value=str(work)), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", out):
            self.assertEqual(install.run_tui(_args()), code)
        return work, out.getvalue()

    def test_a_success_leaves_nothing(self):
        work, out = self.run_to(0)
        self.assertFalse(work.exists())
        self.assertNotIn("Details:", out)

    def test_a_failure_keeps_the_log_it_names(self):
        work, out = self.run_to(1)
        self.assertTrue(work.exists())
        self.assertIn(f"Details: {work / 'install.log'}", out)

    def test_a_stop_leaves_nothing(self):
        work, out = self.run_to(130)
        self.assertFalse(work.exists())

    def test_a_full_temp_disk_or_a_failing_pruner_still_opens(self):
        real_write = Path.write_text

        def full(path, *args, **kwargs):
            if path.name == install.TUI_OWNER:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", full):
            work, out = self.run_to(0)
        self.assertFalse(work.exists())
        with mock.patch.object(install, "_prune_work_folders",
                               side_effect=OSError(errno.EACCES, "denied")):
            work, out = self.run_to(0)
        self.assertFalse(work.exists())
        err = io.StringIO()
        with mock.patch.object(install.tempfile, "mkdtemp",
                               side_effect=OSError(errno.ENOSPC, "No space left")), \
                mock.patch.object(install, "tui_start", return_value=(fresh_state(), [])), \
                mock.patch.object(install, "_open_local_sources"), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(sys, "stderr", err):
            self.assertEqual(install.run_tui(_args()), 1)
        self.assertIn("no work folder could be made", err.getvalue())

    def test_a_stop_while_it_starts_ends_on_the_farewell(self):
        """Start+Select during the detection: the signal is the interface's,
        never the default action that ended Python without a word."""
        import signal

        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                heard = []

                def recorder(signum, _frame, heard=heard):
                    heard.append(signum)

                previous = signal.signal(number, recorder)
                self.addCleanup(signal.signal, number, previous)
                interrupt = signal.getsignal(signal.SIGINT)

                def starting(*_args, **_kwargs):
                    os.kill(os.getpid(), number)
                    return fresh_state(), [("fetch", "retroarch")]

                entered = []
                WorkFolders.Terminal.state_to = lambda _t: None
                out = io.StringIO()
                work = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
                self.addCleanup(shutil.rmtree, work, True)
                with mock.patch.object(install, "_TuiTerminal", self.Terminal), \
                        mock.patch.object(install.tempfile, "mkdtemp", return_value=str(work)), \
                        mock.patch.object(self.Terminal, "enter",
                                          lambda _t: entered.append(True)), \
                        mock.patch.object(install, "tui_start", side_effect=starting), \
                        mock.patch.object(install, "_open_local_sources"), \
                        mock.patch.object(install, "_tui_fetch") as fetch, \
                        mock.patch.object(install, "detect_os", return_value="linux"), \
                        mock.patch.object(sys, "stdout", out), \
                        mock.patch.object(sys, "stderr", out):
                    code = install.run_tui(_args())
                self.assertEqual(code, 128 + number)
                self.assertEqual(heard, [])
                self.assertEqual(entered, [])
                fetch.assert_not_called()
                self.assertEqual(out.getvalue(), "RetroBIOS: stopped. Nothing was installed.\n")
                self.assertIs(signal.getsignal(number), recorder)
                self.assertIs(signal.getsignal(signal.SIGINT), interrupt)


def found(dest: str, folder: str, reason: str = "") -> dict:
    return {"event": "found", "platform": "retroarch", "dest": dest, "folder": folder,
            "path": f"{folder}/{dest}", "member": "", "reason": reason}


class CopyFromAnOldCard(unittest.TestCase):
    """--from in the interface: a row on the plan, the browser, the counts."""

    def recheck(self, state: install.TuiState, events: list) -> None:
        for event in [*FILES, *SYSTEMS, *events, CHECK]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={
                "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
            install.tui_receive(state, ("ended", state.run_id, 1, []))

    def test_the_plan_offers_it_and_the_browser_adds_the_folder(self):
        old = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, old, True)
        (old / "bios").mkdir()
        (old / "backup.zip").write_bytes(b"PK")
        state = checked_state()
        row = next(i for i, r in enumerate(install.tui_rows(state)) if r.action == "fromcard")
        self.assertEqual(install.tui_rows(state)[row].label,
                         "Copy the BIOS files from your old card or a folder")
        state.focus["plan"] = row
        state.home = old
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "enter")
            self.assertEqual((state.screen, state.browse_for), ("browse", "from"))
            labels = [r.label for r in install.tui_rows(state)]
            self.assertEqual(labels, ["Use this folder", "..", "bios/", "backup.zip"])
            self.assertIn("Choose the old card", "\n".join(install.tui_view(state, 80, 24)))
            effects = press(state, "enter")
        self.assertEqual(effects, [("run", "check")])
        self.assertEqual(state.from_folders, [old])
        self.assertIn(f"--from={old}", install.tui_argv(state, "check", "py", "i.py"))
        self.recheck(state, [found("scph5500.bin", str(old)), found("gba_bios.bin", str(old))])
        self.assertEqual(state.screen, "plan")
        text = "".join("".join(install.tui_view(state, 120, 40)).split())
        # The folder by its name: the whole path pushed the BIOS folder off.
        self.assertIn("".join(f"2 found in {old.name}, 2 to download (512.0 KB)".split()), text)
        # A second folder from the options: --from twice.
        press(state, "o")
        state.focus["options"] = next(i for i, r in enumerate(install.tui_rows(state))
                                      if r.action == "from")
        self.assertEqual(install.tui_rows(state)[state.focus["options"]].right, "--from")
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "enter")
            # It opens beside the first folder: the second card sits next to it.
            self.assertEqual(state.browse_dir, old.parent)
            install._browse_to(state, old)
            state.focus["browse"] = 3
            self.assertEqual(press(state, "enter"), [("run", "check")])
        self.assertEqual(state.from_folders, [old, old / "backup.zip"])
        self.assertEqual([flag for flag in install.tui_argv(state, "check", "py", "i.py")
                          if flag.startswith("--from=")],
                         [f"--from={old}", f"--from={old / 'backup.zip'}"])

    def test_left_takes_the_last_folder_back(self):
        state = checked_state()
        state.from_folders = [Path("/media/old")]
        state.checked_flags = install.tui_flags(state)
        state.stack.append("options")
        state.focus["options"] = next(i for i, r in enumerate(install.tui_rows(state))
                                      if r.action == "from")
        # Said, and the screen stays: Back to the plan counts again.
        self.assertEqual(press(state, "left"), [])
        self.assertEqual((state.screen, state.from_folders), ("options", []))
        self.assertIn("No longer copying from old.", "\n".join(install.tui_view(state, 80, 24)))
        self.assertEqual(press(state, "esc"), [("run", "check")])

    def test_the_command_line_folders_are_kept(self):
        with mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            args = _args(platform="retroarch", dest=SYSTEM_DIR)
            setattr(args, "from", [Path("/media/old")])
            state, _effects = install.tui_start(args, "linux")
        self.assertEqual(state.from_folders, [Path("/media/old")])
        self.assertIn(("--from", "/media/old"), install.tui_flags(state))

    def test_files_in_the_wrong_place_are_said_and_listed(self):
        state = checked_state()
        state.platform, state.dest = "retroarch", SYSTEM_DIR
        install._start_check(state)
        self.recheck(state, [found("scph5500.bin", str(SYSTEM_DIR),
                                   "nested folder: bios/scph5500.bin")])
        text = " ".join(line.strip() for line in install.tui_view(state, 120, 40))
        # tui_audit_4 P3-10.13: a fix copies, and the screen says the user's
        # copy stays.
        self.assertIn("1 file is in the wrong place, it will be copied where the emulators "
                      "read it, your copy left as it is.", text)
        row = next(i for i, r in enumerate(install.tui_rows(state)) if r.action == "misplaced")
        state.focus["plan"] = row
        press(state, "enter")
        self.assertEqual(state.screen, "misplaced")
        self.assertIn("scph5500.bin: nested folder: bios/scph5500.bin",
                      "\n".join(install.tui_view(state, 80, 24)))
        press(state, "esc")
        self.assertEqual(state.screen, "plan")

    def test_the_result_says_the_misplaced_copies_stay(self):
        state = checked_state()
        state.stack.append("result")
        state.code = 0
        state.summary = {"event": "summary", "mode": "install", "installed": 2,
                         "up_to_date": 0, "pending": 0, "failed": [], "skipped": [],
                         "left_out": [], "kept": [], "interrupted": False}
        state.local_dests = {"scph5500.bin": (str(SYSTEM_DIR), "nested folder: bios/a")}
        state.finished = [("scph5500.bin", True, ""), ("gba_bios.bin", True, "")]
        text = " ".join(" ".join(install.tui_outcome(state)[1]).split())
        self.assertIn("1 file copied from the wrong place in the BIOS folder, 1 downloaded. "
                      "Your copies in the wrong place were left as they are.", text)

    def test_a_local_copy_is_no_download_speed(self):
        state = checked_state()
        press(state, "enter")
        mega = 1024 * 1024
        events = [(9.0, {"event": "found", **found("big.bin", "/media/old")}),
                  (9.0, {"event": "download", "platform": "retroarch", "files": 2,
                         "bytes": 300 * mega}),
                  (10.0, {"event": "downloaded", "dest": "big.bin", "ok": True, "reason": "",
                          "size": 200 * mega, "files_done": 1, "files_total": 2,
                          "bytes_done": 200 * mega, "bytes_total": 300 * mega}),
                  # The engine says which bytes came from this machine.
                  (10.0, {"event": "transfer", "bytes_local": 200 * mega,
                          "bytes_network": 0}),
                  (11.0, {"event": "progress", "files_done": 1, "files_total": 2,
                          "bytes_done": 201 * mega, "bytes_total": 300 * mega}),
                  (11.0, {"event": "transfer", "bytes_local": 200 * mega,
                          "bytes_network": mega}),
                  (12.0, {"event": "progress", "files_done": 1, "files_total": 2,
                          "bytes_done": 202 * mega, "bytes_total": 300 * mega}),
                  (12.0, {"event": "transfer", "bytes_local": 200 * mega,
                          "bytes_network": 2 * mega})]
        for when, event in events:
            state.now = when
            install.tui_receive(state, ("event", state.run_id, event))
        text = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn("1.0 MB/s, 1:38 left", text)


class AuditRoundThree(unittest.TestCase):
    """tui_audit_2.md: its P1 and P2 findings, each held by a test."""

    CARDS = {
        "muOS": ({"MUOS/": ""}, "retroarch", "MUOS/bios"),
        "Onion": ({".tmp_update/onionVersion/version.txt": "4.3.1\n"}, "retroarch", "BIOS"),
        "spruceOS": ({"spruce/": ""}, "retroarch", "BIOS"),
        "KNULLI": ({"system/batocera.conf": "a=1\nsystem.hostname=KNULLI\n"}, "knulli",
                   "bios"),
        "ROCKNIX": ({".config/system/configs/system.cfg": "root.password=rocknix\n"},
                    "rocknix", "roms/bios"),
    }

    def card(self, files: dict) -> Path:
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        for name, text in files.items():
            path = root / name
            if name.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        return root

    # P1-1: a handheld's card mounted on a computer.

    def test_p1_1_each_card_by_its_own_files(self):
        for name, (files, plat, folder) in self.CARDS.items():
            with self.subTest(card=name):
                root = self.card(files)
                self.assertEqual(install.card_system(root), (plat, root / folder, name))

    def test_p1_1_a_card_nothing_names_is_left_alone(self):
        self.assertIsNone(install.card_system(self.card({"roms/": "", "BIOS/": ""})))
        # code_audit_17 P2-3: Batocera's own share is its SHARE drive, read
        # by a batocera.conf that names no KNULLI.
        share = self.card({"system/batocera.conf": "system.hostname=BATOCERA\n"})
        self.assertEqual(install.card_system(share), ("batocera", share / "bios", "Batocera"))

    def test_p1_1_the_card_rules_are_the_device_rules(self):
        # The folder and the marker come from the rule the device reads, so
        # they are never written twice.
        rules = {rule.name: rule for rule in install._card_rules()}
        self.assertLessEqual({"muOS", "Onion", "spruceOS", "CrossMix-OS", "Allium", "Koriki",
                              "KNULLI", "ROCKNIX"}, set(rules))
        # Only a marker on the part its BIOS folder is on names the card:
        # plumOS's and the Anbernic firmware's are on another part.
        for setup in install.card_setups():
            if setup.rules:
                with self.subTest(system=setup.name):
                    self.assertEqual(rules[setup.name].bios, setup.below)
        self.assertEqual(install.DEFAULT_DESTS["knulli"], Path("/userdata") / "bios")
        self.assertEqual(install.DEFAULT_DESTS["rocknix"], Path("/storage") / "roms/bios")

    def test_p1_1_the_first_screen_offers_the_card_folder(self):
        root = self.card(self.CARDS["muOS"][0])
        with mock.patch.object(install, "_mounted_cards", return_value=[root]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, effects = install.tui_start(_args(), "linux")
        label = f"muOS SD card ({root.name})"
        self.assertEqual(state.setups, [("retroarch", root / "MUOS" / "bios", label)])
        text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn(f"> {label}", text)
        self.assertIn(f"BIOS folder: {root / 'MUOS' / 'bios'}", text)
        (root / "MUOS" / "bios").mkdir()
        self.assertEqual(press(state, "enter"), [("fetch", "retroarch")])
        self.assertEqual(state.dest, root / "MUOS" / "bios")

    def test_p1_1_a_drive_letter_names_the_card(self):
        self.assertEqual(install._mount_name(PureWindowsPath("E:\\")), "E:\\")
        self.assertEqual(install._mount_name(Path("/media/me/MUOS")), "MUOS")
        # tui_audit_5 P3-2: under WSL the card screen listed "d (card or drive)".
        with mock.patch.dict(install._WSL, {"active": True}):
            self.assertEqual(install._mount_name(Path("/mnt/d")), "D:")
            self.assertEqual(install._mount_name(Path("/mnt/wslg")), "wslg")
        self.assertEqual(install._mount_name(Path("/mnt/d")), "d")

    def test_p1_1_on_the_device_its_card_is_not_offered_twice(self):
        root = self.card(self.CARDS["muOS"][0])
        bound = Path("/run/muos/storage/bios")
        with mock.patch.object(install, "_mounted_cards", return_value=[root]), \
                mock.patch.object(install, "detect_platforms", return_value=[("retroarch",
                                                                              bound)]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.dict(install._DETECTED_OS, {("retroarch", bound): "muOS"}):
            state, _effects = install.tui_start(_args(), "linux")
        self.assertEqual([setup[1] for setup in state.setups], [bound])

    def test_p1_1_browsing_to_the_card_root_uses_its_folder(self):
        root = self.card(self.CARDS["KNULLI"][0])
        with mock.patch.object(install, "_mounted_cards", return_value=[root]):
            rows, _error = install._browse_rows(REPO_ROOT / "tests")
        self.assertEqual(rows[2].label, f"{root} (KNULLI card)")
        state = fresh_state()
        install._choose(state, "knulli", root)
        self.assertEqual(state.dest, root / "bios")
        # Another platform's list keeps the folder it was given.
        install._choose(state, "batocera", root)
        self.assertEqual(state.dest, root)

    def test_p1_1_the_command_line_writes_where_muos_reads(self):
        root = self.card(self.CARDS["muOS"][0])
        with FakeRepository() as repo:
            proc = install_run("--platform", "muos", "--dest", str(root), "--yes",
                               env=repo.env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"muOS card: RetroArch files, in {root / 'MUOS' / 'bios'}", proc.stdout)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["MUOS"])
        self.assertTrue((root / "MUOS" / "bios" / "scph5501.bin").is_file())

    def test_p1_1_a_folder_that_is_no_muos_card_is_refused(self):
        root = self.card({"roms/": ""})
        proc = install_run("--platform", "muos", "--dest", str(root), "--yes")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"{root} is not a muOS SD card: no muOS files on it. muOS keeps "
                      "MUOS at the root of its card.", proc.stderr)
        # muOS always names its card: <card>/MUOS/bios would be refused the
        # same (code_audit_15 P3-4), the card's top folder is the remedy.
        self.assertIn("Name the top folder of the muOS card with --dest", proc.stderr)
        self.assertNotIn("<card>/", proc.stderr)
        self.assertEqual(list(root.iterdir()), [root / "roms"])
        # Its BIOS folder named outright is its card's, checked as the card
        # is: on a folder with no MUOS it is refused, as typed it was.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            install.card_dest("muos", "retroarch", root / "MUOS" / "bios")
        card = self.card({"MUOS/bios/": ""})
        self.assertEqual(install.card_dest("muos", "retroarch", card / "MUOS" / "bios"),
                         ("retroarch", card / "MUOS" / "bios"))
        # On the device, the folder muOS binds it to.
        bound = Path("/run/muos/storage/bios")
        self.assertEqual(install.card_dest("muos", "retroarch", bound), ("retroarch", bound))
        # A list named by its own name says which list, not which card.
        self.assertEqual(install.card_dest("retroarch", "retroarch", root),
                         ("retroarch", root))

    def test_p1_1_the_folder_named_is_compared_by_folders(self):
        """NOTMUOS/bios ended with MUOS/bios as text and was taken for a
        muOS card's folder."""
        root = self.card({"roms/": ""})
        stray = root / "NOTMUOS" / "bios"
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            install.card_dest("muos", "retroarch", stray)
        self.assertIn("does not exist", err.getvalue())
        card = self.card({"MUOS/bios/": ""})
        self.assertEqual(install.card_dest("muos", "retroarch", card / "muos" / "BIOS"),
                         ("retroarch", card / "muos" / "BIOS"))

    def test_p1_1_a_card_two_systems_claim_says_so(self):
        root = self.card({"MUOS/": "", "spruce/": ""})
        self.assertIsNone(install.card_system(root))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.card_dest("retroarch", "retroarch", root),
                             ("retroarch", root))
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("holds the files of muOS and spruceOS", lines[0])
        # The system named among them decides.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.card_dest("muos", "retroarch", root),
                             ("retroarch", root / "MUOS/bios"))
        self.assertIn("muOS card", out.getvalue())

    # P1-2: Space is X on PortMaster and Y on muxterm.

    def test_p1_2_space_on_install_opens_the_confirm_screen(self):
        state = checked_state()
        self.assertEqual(install.tui_rows(state)[0].action, "install")
        self.assertEqual(press(state, " "), [("run", "confirm")])
        self.assertEqual(state.screen, "confirm")
        # The same plan comes back: the screen still waits for A.
        install.tui_receive(state, ("event", state.run_id, dict(CHECK)))
        self.assertEqual(install.tui_receive(state, ("ended", state.run_id, 0, [])), [])
        self.assertEqual(state.screen, "confirm")
        self.assertEqual(install.tui_rows(state)[0].action, "install")
        self.assertEqual(press(state, " "), [])
        self.assertEqual(state.screen, "confirm")
        self.assertEqual(press(state, "enter"), [("run", "install")])

    def test_p1_2_space_never_closes_or_quits(self):
        for action in ("quit", "close"):
            with self.subTest(row=action):
                state = checked_state()
                if action == "close":
                    state.check = {**CHECK, "missing": 0, "wrong_hash": 0, "bytes_needed": 0}
                rows = install.tui_rows(state)
                state.focus["plan"] = next(i for i, r in enumerate(rows) if r.action == action)
                self.assertEqual(press(state, " "), [])
                self.assertIsNone(state.exit_code)
        state = checked_state()
        state.focus["plan"] = next(i for i, r in enumerate(install.tui_rows(state))
                                   if r.action == "options")
        press(state, " ")
        self.assertEqual(state.screen, "options")

    def test_p1_2_enter_still_installs_in_three_presses(self):
        state = checked_state()
        self.assertEqual(press(state, "enter"), [("run", "install")])
        self.assertEqual(state.screen, "installing")

    # P2-1: a card too small for the download.

    def test_a_stop_asked_for_says_only_the_outcome(self):
        """Start+Select (SIGTERM) or SIGHUP stops the child before it writes
        a summary: the farewell then listed the log's last lines."""
        state = checked_state()
        state.download = {"files": 50}
        state.finished = [("scph5501.bin", True, "")]
        state.tail = ["    [2/50] scph5501.bin ok",
                      "    Stopping. Files already installed are kept."]
        for code, shown in ((130, False), (1, True)):
            with self.subTest(code=code):
                state.code = code
                _title, lines = install.tui_outcome(state)
                self.assertEqual(any("[2/50]" in line for line in lines), shown, lines)

    def test_a_pack_installs_from_the_pack_not_the_network(self):
        """With --from-pack and no Wi-Fi, the plan said "to download", which
        on a handheld with no network read as a failure on its way."""
        state = checked_state()
        with mock.patch.object(install.LOCAL, "pack", object()):
            headline = str(install.tui_view(state, 80, 24)[0])
            plan = "\n".join(str(line) for line in install.tui_view(state, 80, 24))
            state.summary = {"installed": 3, "failed": [], "up_to_date": 0}
            title, lines = install.tui_outcome(state)
        self.assertIn("to install from the pack", plan, headline)
        self.assertNotIn("to download", plan)
        self.assertEqual(lines[0], "3 files installed from the pack.")

    def test_p2_1_another_folder_comes_first_when_space_is_short(self):
        state = checked_state({"free": 1024, "writable": True, "cores": []})
        self.assertEqual([row.label for row in install.tui_rows(state)[:2]],
                         ["Choose another folder", "Install anyway"])
        self.assertEqual(press(state, "enter"), [])
        self.assertEqual(state.screen, "folder")

    def test_offline_with_nothing_to_copy_offers_no_install_anyway(self):
        """Offline with nothing here to copy, Install is removed: a folder
        short of space then put "Install anyway" back, a run that could
        only fail every file."""
        state = checked_state({"free": 1024, "writable": False, "cores": []})
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            labels = [row.label for row in install.tui_rows(state)]
        self.assertEqual(labels[0], "Choose another folder")
        self.assertNotIn("Install anyway", labels)
        self.assertFalse(any(label.startswith("Install") for label in labels), labels)

    FULL_TAIL = [
        "  Checking existing files...",
        "    0/50 present (0 verified, 0 wrong hash)",
        "  Downloading 50 files (845.7 KB)...",
        "Not enough space on the drive holding /media/me/MUOS/MUOS/bios.",
        "  Needed: 1.2 GB. Free: 300.0 MB.",
        "  Free up 920.0 MB, or install to another drive:",
        "    python3 install.py --platform retroarch --dest '<folder on another drive>'",
    ]

    def test_p2_1_a_full_card_is_said_in_words(self):
        root = self.card(self.CARDS["muOS"][0])
        state = checked_state()
        state.dest, state.log = root / "MUOS" / "bios", "/tmp/retrobios-tui-x/install.log"
        state.stack.append("result")
        state.code, state.tail = 2, list(self.FULL_TAIL)
        with mock.patch.object(install, "_mounted_cards", return_value=[root]):
            title, lines = install.tui_outcome(state)
        self.assertEqual(title, "Not enough space")
        self.assertEqual(lines[0], "The card is full: 1.2 GB needed, 300.0 MB free.")
        self.assertIn("Free some space, or run again and choose another folder.", lines)
        text = "\n".join(lines)
        for raw in ("Checking existing files", "Downloading", "stopped with code", "Details:"):
            self.assertNotIn(raw, text)
        self.assertFalse(install.tui_keeps_log(state))

    def test_p2_1_advice_never_names_the_interface_list(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root writes to a read-only folder")
        folder = self.card({})
        folder.chmod(0o555)
        self.addCleanup(folder.chmod, 0o755)
        with FakeRepository() as repo:
            listed = repo.root / "install" / "retroarch.json"
            proc = install_run("--platform", "retroarch", "--dest", str(folder),
                               f"--manifest={listed}", "--yes",
                               env={**repo.env(), install.TUI_LIST_ENV: "1"})
        self.assertIn("sudo", proc.stdout + proc.stderr)
        self.assertNotIn("--manifest", proc.stdout + proc.stderr)

    # P2-2: the BIOS folder line on a handheld screen.

    def test_p2_2_the_plan_keeps_the_folder_at_handheld_sizes(self):
        zipped = "/home/me/a/long/path/to/the/Downloads/PSX BIOS.zip"
        for cols, rows in ((40, 12), (40, 15)):
            with self.subTest(size=f"{cols}x{rows}"):
                state = checked_state()
                state.dest = Path("/home/someone/with/a/long/path/.config/retroarch/system")
                state.found = [found("scph5500.bin", zipped)]
                state.cols, state.rows = cols, rows
                lines = [line.strip() for line in install.tui_view(state, cols, rows)]
                folder = next(line for line in lines if line.startswith("BIOS folder: "))
                self.assertTrue(folder.endswith("retroarch/system"), folder)
                self.assertIn("...", folder)
                self.assertIn("1 found in PSX BIOS.zip,", " ".join(lines))
                self.assertNotIn("/home/me/a/long", " ".join(lines))

    # P2-3: where the files came from.

    def test_p2_3_the_result_says_what_came_from_the_card(self):
        state = checked_state()
        state.stack.append("result")
        state.code, state.download = 0, {"files": 5, "bytes": 10}
        state.local_dests = {"scph5500.bin": ("/media/me/OLD", ""),
                             "scph5501.bin": ("/media/me/OLD", ""),
                             "gba_bios.bin": (str(SYSTEM_DIR), "one folder too deep")}
        state.finished = [("scph5500.bin", True, ""), ("scph5501.bin", True, ""),
                          ("gba_bios.bin", True, ""), ("pcsx/extra.bin", True, ""),
                          ("fc/disksys.rom", False, "HTTP 404")]
        state.summary = {"installed": 4, "up_to_date": 0, "failed": [
            {"dest": "fc/disksys.rom", "reason": "HTTP 404"}]}
        _title, lines = install.tui_outcome(state)
        self.assertEqual(lines[1], "2 files copied from OLD, 1 copied from the wrong place "
                                   "in the BIOS folder, 1 downloaded, 1 still missing.")
        # The line mode's own sentence, from the same counts.
        self.assertEqual(lines[1], install.sources_line([("OLD", 2)], 1, 1, 1))

    def test_p2_3_a_run_that_copied_nothing_adds_no_line(self):
        state = install.TuiState()
        state.summary, state.finished = {"installed": 2, "failed": []}, [("a", True, "")]
        self.assertEqual(install._tui_sources(state, 2, 0), "")

    # P2-4 and P2-5: the systems list.

    def systems_state(self) -> install.TuiState:
        state = checked_state()
        state.systems = [*state.systems,
                         {"system": "other", "name": "other", "files": 2, "present": 0,
                          "missing": 2, "wrong_hash": 0, "bytes_needed": 300},
                         {"system": "3do", "name": "3do", "files": 3, "present": 0,
                          "missing": 3, "wrong_hash": 0, "bytes_needed": 600}]
        state.files = [*state.files,
                       {"event": "file", "dest": "panafz1.bin", "systems": ["3do"],
                        "size": 100, "state": "missing"},
                       {"event": "file", "dest": "mame/hash/a.xml", "systems": ["3do", "mac"],
                        "size": 200, "state": "missing"},
                       {"event": "file", "dest": "mame/hash/b.xml", "systems": ["3do", "mac"],
                        "size": 300, "state": "missing"}]
        state.ticked |= {"other", "3do"}
        state.stack.append("systems")
        return state

    def test_p2_4_the_other_row_is_named_and_explained(self):
        state = self.systems_state()
        rows = install.tui_rows(state)
        other = next(i for i, row in enumerate(rows) if row.value == "other")
        self.assertEqual(rows[other].label, "[x] Other systems and game engines")
        state.focus["systems"] = other
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("Other systems and game engines: what cores load for systems this "
                      "setup does not list", text)

    def test_p2_5_shared_files_are_counted_apart(self):
        state = self.systems_state()
        rows = install.tui_rows(state)
        three = next(i for i, row in enumerate(rows) if row.value == "3do")
        self.assertEqual(rows[three].right, "1 file, 100 B + 2 shared")
        state.focus["systems"] = three
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("+2 shared: counted once", text)
        # The line mode counts the same files apart.
        self.assertIn("and 2 files, 500 B shared with other systems", install._system_sized([
            {"dest": "panafz1.bin", "size": 100, "systems": ["3do"]},
            {"dest": "a.xml", "size": 200, "systems": ["3do", "mac"]},
            {"dest": "b.xml", "size": 300, "systems": ["3do", "mac"]}]))
        narrow = install.tui_view(state, 40, 15)
        self.assertTrue(any("100 B + 2 shared" in line for line in narrow), narrow)

    # P2-6: a file that cannot be supplied, by name, where it matters.

    def unavailable_state(self) -> install.TuiState:
        state = checked_state()
        state.manifest = {**MANIFEST, "system_titles": {"nintendo-gba": "Game Boy Advance"},
                          "omitted_files": [
                              {"dest": "gba_extra.bin", "system": "nintendo-gba",
                               "required": True, "reason": "not_found"},
                              {"dest": "optional.bin", "system": "nintendo-gba",
                               "required": False, "reason": "not_found"}]}
        state.stack.append("result")
        state.code, state.download = 0, {"files": 1, "bytes": 1}
        state.summary = {"installed": 1, "up_to_date": 0, "failed": []}
        return state

    def test_p2_6_the_missing_file_is_named_with_its_system(self):
        state = self.unavailable_state()
        _title, lines = install.tui_outcome(state)
        self.assertIn("Not available here, and needed to start its emulator: gba_extra.bin "
                      "(Game Boy Advance). Your own copy goes in the BIOS folder.", lines)
        self.assertNotIn("collection", " ".join(lines))

    def test_p2_6_it_is_left_out_when_it_does_not_concern_the_run(self):
        state = self.unavailable_state()
        state.ticked = {"sony-playstation"}
        self.assertEqual(install._unavailable_lines(state), [])
        state = self.unavailable_state()
        state.summary = {"installed": 1, "failed": [], "interrupted": True}
        self.assertFalse(any("Not available" in line for line in install.tui_outcome(state)[1]))

    # P3: small fixes the audit listed.

    def fetch_failure(self, error: BaseException, env: dict | None = None) -> tuple:
        inbox = __import__("queue").Queue()
        with mock.patch.object(install, "_fetch_json", side_effect=error), \
                mock.patch.dict(os.environ, env or {}):
            for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            install._tui_fetch("retroarch", inbox, io.StringIO())
        _kind, _plat, title, lines, _code = inbox.get_nowait()
        return title, lines

    def test_p3_the_clock_names_the_row_to_choose(self):
        import ssl

        error = urllib_error(ssl.SSLCertVerificationError("certificate is not yet valid"))
        words = install.network_words(error, today=install.datetime.date(1980, 1, 1))
        with mock.patch.object(install, "network_words", return_value=words):
            _title, lines = self.fetch_failure(error)
        self.assertTrue(lines[0].endswith("then choose Try again."), lines[0])

    def test_p3_a_proxy_in_the_way_is_named_not_shown(self):
        error = urllib_error(ConnectionRefusedError(111, "Connection refused"))
        _title, lines = self.fetch_failure(
            error, {"HTTPS_PROXY": "http://me:secret@10.0.0.1:3128"})
        self.assertIn("HTTPS_PROXY is set, so the connection goes through a proxy: check "
                      "that it answers.", lines)
        self.assertNotIn("secret", " ".join(lines))
        _title, lines = self.fetch_failure(error)
        self.assertFalse(any("PROXY" in line for line in lines))

    def test_p3_a_list_that_is_no_json_is_said_in_words(self):
        try:
            install.json.loads("<html>")
        except install.json.JSONDecodeError as cause:
            error = ValueError(f"invalid file list: {cause}")
            error.__cause__ = cause
        title, lines = self.fetch_failure(error)
        self.assertEqual(title, "The file list is not valid")
        self.assertNotIn("Expecting value", " ".join(lines))
        self.assertIn("a Wi-Fi sign-in page or a cut download looks like this.", lines[0])

    def test_p3_the_preview_counts_its_files_only(self):
        state = checked_state()
        state.stack += ["options", "preview"]
        state.dry = dict(CHECK)
        state.dry_files = [{**FILES[0], "dest": f"f{n}.bin"} for n in range(40)]
        state.rows = 24
        lines = [line.strip() for line in install.tui_view(state, 80, 24)]
        self.assertIn("1-18 of 40", lines)

    def test_p3_game_data_reads_without_a_double_negative(self):
        state = checked_state()
        label = next(row.label for row in install._option_rows(state)
                     if row.action == "game_data")
        self.assertEqual(label, "Game data: kept")
        state.no_game_data = True
        label = next(row.label for row in install._option_rows(state)
                     if row.action == "game_data")
        self.assertEqual(label, "Game data: left out, firmware only")

    def test_p3_the_plan_says_which_config_named_the_folder(self):
        config = HOME / ".config/retroarch/retroarch.cfg"
        with mock.patch.dict(install._RETROARCH_CONFIGS, {SYSTEM_DIR: (config, config.parent)}):
            text = "\n".join(install.tui_view(checked_state(), 80, 24))
        self.assertIn("Named by ~/.config/retroarch/retroarch.cfg", text)

    # The engine's own events (main, after the second audit).

    def test_the_speed_is_the_network_bytes(self):
        state = checked_state()
        state.stack.append("installing")
        state.run, state.running = "install", True
        install.tui_receive(state, ("event", state.run_id,
                                    {"event": "download", "files": 4, "bytes": 4 << 20}))
        for now, done, network in ((100.0, 1 << 20, 0), (102.0, 3 << 20, 1 << 20)):
            state.now = now
            install.tui_receive(state, ("event", state.run_id, {
                "event": "progress", "files_done": 1, "files_total": 4, "bytes_done": done,
                "bytes_total": 4 << 20}))
            install.tui_receive(state, ("event", state.run_id, {
                "event": "transfer", "bytes_local": done - network, "bytes_network": network}))
        text = "\n".join(install.tui_view(state, 80, 24))
        # 1 MB from the network in 2 s, whatever came from the old card.
        self.assertIn("512.0 KB/s, 0:02 left", text)

    def test_the_plan_says_what_the_scan_left_out(self):
        state = checked_state()
        state.from_folders = [Path("E:\\")]
        install._start_check(state)
        events = [{"event": "scanned", "folder": "E:\\", "files": 200000, "truncated": True,
                   "other_drives": ["E:\\D"]},
                  {"event": "unreadable", "platform": "retroarch", "folder": "E:\\",
                   "path": "E:\\bios\\scph5501.bin", "member": "", "reason": "Permission denied"},
                  {"event": "unreadable", "platform": "retroarch", "folder": "E:\\",
                   "path": "E:\\backup.zip", "member": "gba_bios.bin",
                   "reason": "it is encrypted, a password is needed"}]
        for event in [*FILES, *SYSTEMS, *events, CHECK]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={
                "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
            install.tui_receive(state, ("ended", state.run_id, 1, []))
        text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn("Read the first 200,000 files of E:\\.", text)
        self.assertIn("E:\\D is another drive: add it to copy from it too.", text)
        # The folder by its name: "your old card" was said of any folder.
        self.assertIn("2 files in E:\\ could not be read.", text)
        rows = install.tui_rows(state)
        state.focus["plan"] = next(i for i, row in enumerate(rows) if row.action == "unreadable")
        press(state, "enter")
        self.assertEqual(state.screen, "unreadable")
        self.assertEqual(install._pager_items(state), [
            "E:\\bios\\scph5501.bin: Permission denied",
            "E:\\backup.zip, gba_bios.bin: it is encrypted, a password is needed"])

    def test_the_interface_does_not_index_the_folders_itself(self):
        missing = REPO_ROOT / "tmp" / "no-such-old-card"
        err = io.StringIO()
        with mock.patch.object(install, "FolderIndex", side_effect=AssertionError("indexed")), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install, "tui_start", side_effect=AssertionError("opened")), \
                mock.patch.object(sys, "stderr", err):
            self.assertEqual(install.run_tui(_args(**{"from": [missing]})), 1)
        self.assertIn(f"Error: {missing} does not exist: --from names the folder", err.getvalue())

    # Work folders: only a kill no handler sees leaves one, and the next
    # run takes it away.

    def test_work_folders_left_behind_are_pruned(self):
        import subprocess

        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        now = time.time()
        week = install.TUI_KEEP_SECONDS

        def folder(name: str, pid: int | None, kept: bool, age: float) -> Path:
            path = root / f"{install.TUI_WORK_PREFIX}{name}"
            path.mkdir()
            if pid is not None:
                (path / install.TUI_OWNER).write_text(str(pid))
            if kept:
                (path / install.TUI_KEPT).write_text("")
            os.utime(path, (now - age, now - age))
            return path

        killed = folder("killed", gone.pid, False, 60)
        running = folder("running", os.getpid(), False, 2 * week)
        failed = folder("failed", gone.pid, True, 60)
        failed_old = folder("failed-old", gone.pid, True, 2 * week)
        unknown = folder("unknown", None, False, 60)
        unknown_old = folder("unknown-old", None, False, 2 * week)
        other = root / "something-else"
        other.mkdir()
        # A stray or hostile owner file is an unknown owner: a number past
        # what a pid holds raised OverflowError, and 0 or -1 name a group.
        hostile = [folder(f"hostile{index}", None, False, 60)
                   for index in range(3)]
        for path, text in zip(hostile, ("9" * 40, "0", "-1")):
            (path / install.TUI_OWNER).write_text(text)
            self.assertIsNone(install._owner_alive(path))
        removed = install._prune_work_folders(root, now)
        self.assertEqual(sorted(removed), sorted([killed, failed_old, unknown_old]))
        for path in hostile:
            shutil.rmtree(path)
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         sorted(path.name for path in (running, failed, unknown, other)))


class IdleExit(unittest.TestCase):
    def test_the_variable(self):
        for value, seconds in (("30", 30.0), (" 2.5 ", 2.5), ("", None), ("0", None),
                               ("-4", None), ("soon", None), ("inf", None), ("nan", None)):
            with self.subTest(value=value):
                self.assertEqual(install.tui_idle_exit({install.TUI_IDLE_ENV: value}),
                                 seconds)
        self.assertIsNone(install.tui_idle_exit({}))

    def test_the_farewell(self):
        state = fresh_state()
        state.idle_closed, state.exit_code = True, 0
        self.assertEqual(install.tui_farewell(state),
                         ["RetroBIOS: No button pressed, closing."])

    def test_an_error_screen_closes_with_its_code(self):
        """Closed idle on "No internet: offline mode", the launcher read 0
        and showed nothing."""
        state = fresh_state()
        state.stack = ["setup", "checking"]
        install._fail(state, ["Give the file list with --manifest FILE."], 1)
        state.error_title = "No internet: offline mode"
        self.assertEqual(self.idle_close(state), 1)
        self.assertEqual(install.tui_farewell(state), [
            "RetroBIOS: No button pressed, closing.",
            "RetroBIOS: No internet: offline mode",
            "Give the file list with --manifest FILE."])

    def test_a_result_closes_with_the_install_code(self):
        state = fresh_state()
        state.stack = ["setup", "result"]
        state.code = 2
        state.summary = {"event": "summary", "mode": "install", "installed": 1,
                         "up_to_date": 0, "pending": 0,
                         "failed": [{"dest": "a.bin", "reason": "disk full"}],
                         "skipped": [], "left_out": [], "kept": [], "interrupted": False}
        state.dest = SYSTEM_DIR
        self.assertEqual(self.idle_close(state), 2)
        farewell = install.tui_farewell(state)
        self.assertEqual(farewell[:2], ["RetroBIOS: No button pressed, closing.",
                                        "RetroBIOS: Finished with errors"])

    def test_any_other_screen_closes_as_a_quit(self):
        """0 read as "everything asked for is installed" with nothing done."""
        state = fresh_state()
        state.stack = ["setup"]
        self.assertEqual(self.idle_close(state), 130)

    def idle_close(self, state: install.TuiState) -> int:
        """run_tui on state, no key ever pressed, closed by the idle timer."""
        class Quiet(Crash.FakeTerminal):
            def keys(self, timeout):
                time.sleep(0.01)
                return []

        out = io.StringIO()
        work = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, work, True)
        with mock.patch.object(install, "_TuiTerminal", Quiet), \
                mock.patch.object(install.tempfile, "mkdtemp", return_value=str(work)), \
                mock.patch.object(install, "tui_start", return_value=(state, [])), \
                mock.patch.object(install, "_open_local_sources"), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.dict(os.environ, {install.TUI_IDLE_ENV: "0.05"}), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", out):
            code = install.run_tui(_args())
        self.assertTrue(state.idle_closed)
        self.assertIn("No button pressed, closing.", out.getvalue())
        return code


class AReadErrorIsNotAClosedWindow(unittest.TestCase):
    """Any EIO read as the window closing: a failing SD card answers EIO
    too, and that run ended 129, no error.log, no launcher message."""

    def test_eio_counts_only_once_the_terminal_hung_up(self):
        import pty

        master, slave = pty.openpty()
        screen = open(os.dup(slave), "w", encoding="utf-8")  # noqa: SIM115
        terminal = mock.Mock(fd=slave, stdout=screen)
        try:
            card = OSError(errno.EIO, "Input/output error", "/media/card/bios/x.bin")
            self.assertFalse(install._closed_terminal(card, terminal))
            self.assertFalse(install._closed_terminal(
                OSError(errno.ENOENT, "No such file"), terminal))
            os.close(master)
            master = -1
            self.assertTrue(install._closed_terminal(card, terminal))
            self.assertTrue(install._closed_terminal(termios.error(errno.EIO, "gone"),
                                                     terminal))
        finally:
            with contextlib.suppress(OSError):
                screen.close()
            os.close(slave)
            if master >= 0:
                os.close(master)


class AuditRoundFour(unittest.TestCase):
    """tui_audit_3.md, each finding held where it was found."""

    UNPLACED = {"reason": "no_save_directory", "label": "no save directory", "files": 2,
                "required": 0}

    def test_p1_1_files_left_out_of_the_save_directory_are_said(self):
        state = fresh_state()
        state.stack = ["setup"]
        with mock.patch.object(install.Path, "exists", return_value=True):
            install.tui_update(state, Key("enter"))
        install.tui_receive(state, ("manifest", "retroarch", MANIFEST))
        summary = {"event": "summary", "mode": "check", "installed": 0, "up_to_date": 0,
                   "pending": 5, "failed": [], "skipped": [self.UNPLACED], "left_out": [],
                   "kept": [], "interrupted": False}
        for event in [*FILES, *SYSTEMS, CHECK, summary]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={
                "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
            install.tui_receive(state, ("ended", state.run_id, 1, []))
        line = ("2 files read from RetroArch's save directory left out: its place is "
                "not known. ")
        plan = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        # The plan names the row that sets it, the result the option a
        # command carries.
        self.assertIn(line + "Choose it in Options, Save folder (--save-dir).", plan)
        state.stack.append("result")
        state.code = 0
        state.summary = {**summary, "mode": "install", "installed": 5}
        self.assertIn(line + "Choose it in Options, Save folder (--save-dir), next time.",
                      install.tui_outcome(state)[1])


class TheSaveFolderHasARow(unittest.TestCase):
    """A card on a PC left the save-directory files out and named
    --save-dir, which Options had no row for: the interface user had to
    leave it and type a command."""

    def state(self) -> install.TuiState:
        state = checked_state()
        state.manifest = {**MANIFEST, "save_directory": {"folder": "saves", "by_core": True}}
        return state

    def test_options_list_it_and_the_browser_sets_it(self):
        state = self.state()
        rows = install._option_rows(state)
        [row] = [row for row in rows if row.action == "savedir"]
        self.assertEqual((row.label, row.right), ("Save folder: not known", "--save-dir"))
        saves = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, saves, True)
        state.stack = ["setup", "plan", "options"]
        install._option(state, "savedir", 0)
        self.assertEqual(state.screen, "browse")
        self.assertIn("save directory", " ".join(" ".join(install.tui_view(state, 120, 40))
                                                 .split()))
        install._browse_to(state, saves)
        effects = install._activate(state, install.TuiRow("use", "Use this folder"))
        self.assertIn(("--save-dir", str(saves)), install.tui_flags(state))
        self.assertTrue(any(effect[0] == "run" for effect in effects), effects)
        self.assertEqual(install._save_folder_words(state), str(saves))

    def test_a_list_without_a_save_directory_has_no_row(self):
        self.assertNotIn("savedir", [row.action for row in install._option_rows(checked_state())])


class ACardSystemNamedOnAComputer(unittest.TestCase):
    """`--platform onion` on a PC wrote RetroArch's files into the PC's own
    ~/.config/retroarch/system, which Onion never reads: the system's list
    is RetroArch's, and so was the folder detection found for it."""

    ONION = {".tmp_update/onionVersion/version.txt": "4.3.1\n"}

    def card(self, files: dict) -> Path:
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        return root

    def test_the_systems_that_run_from_a_card(self):
        self.assertEqual(install.card_bound("onion", "retroarch"), ("Onion", "BIOS"))
        self.assertEqual(install.card_bound("muOS", "retroarch"), ("muOS", "MUOS/bios"))
        self.assertEqual(install.card_bound("spruceos", "retroarch"), ("spruceOS", "BIOS"))
        self.assertEqual(install.card_bound("knulli", "knulli"), ("KNULLI", "bios"))
        self.assertEqual(install.card_bound("nextui", "nextui"), ("NextUI", ""))
        self.assertIsNone(install.card_bound("retroarch", "retroarch"))
        self.assertIsNone(install.card_bound("batocera", "batocera"))

    def test_without_questions_the_run_names_the_card_and_writes_nothing(self):
        home = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        system = home / ".config" / "retroarch" / "system"
        system.mkdir(parents=True)
        (home / ".config" / "retroarch" / "retroarch.cfg").write_text(
            f'system_directory = "{system}"\n', encoding="utf-8")
        with FakeRepository() as repo:
            proc = install_run("--platform", "onion", "--yes",
                               env={**repo.env(), "HOME": str(home), "RETROBIOS_OS": "linux"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Onion keeps its BIOS files on its SD card, not on this machine.",
                      proc.stderr)
        self.assertIn("--platform onion --dest /media/$USER/<card>", proc.stderr)
        self.assertEqual(list(system.iterdir()), [])

    def test_one_mounted_card_of_that_system_is_taken(self):
        onion, other = self.card(self.ONION), self.card({"roms/a": ""})
        with mock.patch.object(install, "_mounted_cards", return_value=[other, onion]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("onion", "retroarch", interactive=False)
        self.assertEqual(dest, onion / "BIOS")
        self.assertIn(f"Onion card found at {onion.name}, BIOS folder {onion / 'BIOS'}",
                      out.getvalue())

    def test_at_a_terminal_the_card_root_is_asked(self):
        onion = self.card(self.ONION)
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_ask", return_value=str(onion)) as ask, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("onion", "retroarch", interactive=True)
        self.assertEqual(dest, onion / "BIOS")
        self.assertIn("Mount the card, then type its top folder.", out.getvalue())
        ask.assert_called_once()
        knulli = self.card({"roms/a": ""})
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_ask", return_value=str(knulli)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.card_install_dest("knulli", "knulli", True),
                             knulli / "bios")

    def ask(self, *answers: str):
        """_ask answering in turn, and the prompts it was asked."""
        given = iter(answers)
        asked: list = []

        def answer(prompt: str) -> str:
            asked.append(prompt)
            return next(given)
        return mock.patch.object(install, "_ask", side_effect=answer), asked

    def test_a_typed_root_that_does_not_exist_is_asked_again(self):
        """code_audit_12 P2-3: a typo four folders deep was created and
        filled, and a menu answer (8) became the card."""
        onion = self.card(self.ONION)
        typo = REPO_ROOT / "tmp" / "no-such" / "media" / "NEXT"
        patcher, asked = self.ask("8", str(typo), str(onion))
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("onion", "retroarch", interactive=True)
        self.assertEqual(dest, onion / "BIOS")
        self.assertEqual(len(asked), 3)
        # No card is listed: 8 is no number of the list, and no folder.
        self.assertIn("Type the card's top folder, Enter to look again, or q to quit.",
                      out.getvalue())
        self.assertIn("NEXT does not exist, or is not a folder. Type the top folder of the "
                      "Onion card.", out.getvalue())
        self.assertFalse((REPO_ROOT / "tmp" / "no-such").exists())

    def test_a_typed_root_that_is_no_card_of_that_system_is_asked_again(self):
        """code_audit_12 P3-4: a folder that was no Onion card ended the run."""
        blank, onion = self.card({"roms/a": ""}), self.card(self.ONION)
        patcher, asked = self.ask(str(blank), str(onion))
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("onion", "retroarch", interactive=True)
        self.assertEqual((dest, len(asked)), (onion / "BIOS", 2))
        self.assertIn(f"{blank} is not an Onion SD card: no Onion files on it. Onion keeps "
                      ".tmp_update at the root of its card.", out.getvalue())

    def test_a_minui_root_is_a_minui_card(self):
        blank = self.card({"roms/a": ""})
        minui = self.card({".system/version.txt": "NextUI-20250101\n", "Bios/x": ""})
        patcher, asked = self.ask(str(minui / "Bios"), str(blank), str(minui))
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("nextui", "nextui", interactive=True)
        self.assertEqual((dest, len(asked)), (minui, 3))
        self.assertIn(f"is inside a NextUI card, whose root is {minui}", out.getvalue())
        self.assertIn("is not a NextUI SD card: no NextUI files on it. NextUI keeps "
                      ".system/version.txt at the root of its card.", out.getvalue())

    def test_the_mounted_drives_are_listed_and_taken_by_number(self):
        """tui_audit_5 P3-8.4: the screen lists the mounted cards, the
        prompt did not. tui_audit_6 P3-8, code_audit_13 P3-6: it listed
        every drive, a muOS card beside Onion's question, and the muOS card
        taken by number made the run a muOS install."""
        muos, blank = self.card({"MUOS/bios/x": ""}), self.card({"roms/a": ""})
        first, second = (self.card({"system/batocera.conf": "system.hostname=KNULLI\n"})
                         for _card in range(2))
        patcher, asked = self.ask("4", "2")
        with mock.patch.object(install, "_mounted_cards",
                               return_value=[muos, first, blank, second]), \
                patcher, contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("knulli", "knulli", interactive=True)
        self.assertEqual((dest, len(asked)), (second / "bios", 2))
        self.assertIn(f"1) {first.name} (KNULLI card, {first})", out.getvalue())
        self.assertIn(f"2) {second.name} (KNULLI card, {second})", out.getvalue())
        self.assertNotIn(str(muos), out.getvalue())
        # tui_audit_10 P3-8: KNULLI fills its card when it first starts, and
        # a blank drive is listed after its own cards, as on the screen.
        self.assertIn(f"3) {blank.name} (card or drive, {blank})", out.getvalue())
        self.assertIn("Type a number from 1 to 3, a folder, Enter to look again, or q to "
                      "quit.", out.getvalue())

    def test_creating_several_folders_on_the_card_is_asked(self):
        """tui_audit_6 P3-8: N to the question ended the whole run where it
        meant "not that card"."""
        root = self.card({"roms/a.txt": ""})
        rocknix = root / "R"
        rocknix.mkdir()
        card = self.card({"roms/a": ""})
        make_card(card, "ROCKNIX")
        patcher, asked = self.ask(str(rocknix), "n", str(card))
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("rocknix", "rocknix", interactive=True)
        self.assertEqual(dest, card / "roms" / "bios")
        self.assertIn("[y/N]", asked[1])
        self.assertEqual(asked[2], "Card root, Enter to look again, q to quit: ")
        self.assertIn("Not created.", out.getvalue())
        self.assertFalse((rocknix / "roms").exists())

    def test_the_menu_asks_a_card_system_for_its_card(self):
        """code_audit_12 P3-3: ROCKNIX picked from the menu on a PC was
        offered /storage/roms/bios."""
        card = self.card({"roms/a": ""})
        make_card(card, "ROCKNIX")
        patcher, asked = self.ask(str(card))
        with mock.patch.object(install, "_choose_platform", return_value="rocknix"), \
                mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()):
            chosen = install._prompt_manual_platform("linux")
        self.assertEqual(chosen, [("rocknix", card / "roms" / "bios")])
        self.assertEqual(asked, ["Card root, Enter to look again, q to quit: "])

    def test_a_card_of_another_system_is_not_offered(self):
        """tui_audit_5 P1-1: A on the first mount wrote Onion's BIOS/ on a
        muOS card, which reads MUOS/bios."""
        muos, blank = self.card({"MUOS/bios/x": ""}), self.card({"roms/a": ""})
        found = [("retroarch", SYSTEM_DIR)]
        with mock.patch.object(install, "_mounted_cards", return_value=[muos, blank]), \
                mock.patch.object(install, "detect_platforms", return_value=found), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, effects = install.tui_start(_args(platform="onion"), "linux")
            self.assertEqual((state.screen, effects), ("folder", []))
            rows = install.tui_rows(state)
            text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertEqual([row.action for row in rows], ["browse", "type", "rescan"])
        self.assertEqual(state.focus["folder"], 0)
        self.assertIn("Where is the SD card?", text)
        self.assertIn("No Onion card is mounted. Insert it, then choose Look again.", text)

    def test_only_the_cards_of_that_system_are_listed(self):
        first, second = self.card(self.ONION), self.card(self.ONION)
        muos = self.card({"MUOS/bios/x": ""})
        state = fresh_state()
        state.stack = ["setup", "platforms"]
        state.filters["platforms"] = "onion"
        state.focus["platforms"] = 1
        with mock.patch.object(install, "_mounted_cards", return_value=[muos, first, second]):
            press(state, "enter")
            self.assertEqual(state.screen, "folder")
            rows = install.tui_rows(state)
            install.tui_update(state, Key("enter"))
        self.assertEqual([(row.action, row.value) for row in rows][:2],
                         [("card", first), ("card", second)])
        self.assertNotIn("rescan", [row.action for row in rows])
        self.assertEqual(state.dest, first / "BIOS")
        # Titled as the card found by itself (tui_audit_5 P3-7).
        self.assertEqual(install.detected_os("retroarch", first / "BIOS"), "Onion")

    def test_look_again_finds_the_card_inserted(self):
        onion = self.card(self.ONION)
        mounts: list = []
        with mock.patch.object(install, "_mounted_cards", side_effect=lambda: list(mounts)), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(platform="onion"), "linux")
            press(state, "down", "down", "enter")
            self.assertEqual(state.status, "Still no Onion card is mounted.")
            mounts.append(onion)
            press(state, "enter")
            rows = install.tui_rows(state)
        self.assertEqual(rows[state.focus["folder"]].action, "card")
        self.assertEqual(state.status, "")

    def test_a_folder_that_is_no_card_of_that_system_is_refused(self):
        blank = self.card({"roms/a": ""})
        muos = self.card({"MUOS/bios/x": ""})
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(platform="onion"), "linux")
            press(state, "enter")
            self.assertEqual(state.screen, "browse")
            install._browse_to(state, blank)
            effects = press(state, "enter")
            self.assertEqual((effects, state.screen), ([], "browse"))
            self.assertIn("Not an Onion SD card: no Onion files on it.", state.status)
            self.assertIn(f"{blank} is not an Onion SD card", state.status_more)
            self.assertIsNone(state.dest)
            # tui_audit_6 P3-8: a muOS card made the run a muOS install
            # without a word.
            install._browse_to(state, muos)
            press(state, "enter")
            # tui_audit_7 P3-1: it is a card's top folder, of another card.
            self.assertEqual(state.status, "That is a muOS card. Insert the Onion card, or "
                             "go back and choose muOS.")
        self.assertIsNone(state.dest)

    def test_a_typed_card_root_is_checked_on_the_path_screen(self):
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(platform="rocknix"), "linux")
            press(state, "down", "enter")
            self.assertEqual(state.screen, "path")
            state.path_text = str(REPO_ROOT / "tmp" / "no-such-card")
            effects = press(state, "enter")
        self.assertEqual((effects, state.screen), ([], "path"))
        self.assertEqual("That folder does not exist. Choose the card's top folder.",
                         state.status)

    def test_a_dest_that_is_no_card_of_that_system_is_refused(self):
        blank = self.card({"roms/a": ""})
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, effects = install.tui_start(_args(platform="onion", dest=str(blank)),
                                               "linux")
        self.assertEqual((effects, state.screen), ([], "folder"))
        self.assertIn("Not an Onion SD card", state.status)

    def test_change_folder_names_the_folder_it_reuses(self):
        """code_audit_12 P3-1: Use this folder on the card screen named
        nothing."""
        onion = self.card(self.ONION)
        state = checked_state()
        state.platform, state.card_bound = "retroarch", ("Onion", "BIOS")
        state.dest = onion / "BIOS"
        press(state, "o")
        rows = install.tui_rows(state)
        state.focus["options"] = [row.action for row in rows].index("folder")
        press(state, "enter")
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertEqual(state.screen, "folder")
        self.assertIn("Where is the SD card?", text)
        self.assertIn("The BIOS folder chosen for Onion:", text)
        self.assertEqual(state.folder_default, onion / "BIOS")

    def test_the_interface_takes_the_one_onion_card_mounted(self):
        onion = self.card(self.ONION)
        (onion / "BIOS").mkdir()
        found = [("retroarch", SYSTEM_DIR)]
        with mock.patch.object(install, "_mounted_cards", return_value=[onion]), \
                mock.patch.object(install, "detect_platforms", return_value=found), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, effects = install.tui_start(_args(platform="onion"), "linux")
        self.assertEqual(effects, [("fetch", "retroarch")])
        self.assertEqual(state.dest, onion / "BIOS")

    def test_on_the_device_its_own_folder_is_used(self):
        device = Path("/mnt/SDCARD/BIOS")
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms",
                                  return_value=[("retroarch", device)]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.dict(install._DETECTED_OS, {("retroarch", device): "Onion"}), \
                mock.patch.object(install.Path, "exists", return_value=True):
            state, effects = install.tui_start(_args(platform="onion"), "linux")
        self.assertEqual(effects, [("fetch", "retroarch")])
        self.assertEqual(state.dest, device)


class TheSetupFilterKnowsDevices(unittest.TestCase):
    """tui_audit_4 P3-10.5: the filter found nothing for steam deck, r36s,
    trimui or retroid, and miyoo found RetroArch with muOS, JELOS... beside it."""

    TYPED = {
        "steam deck": {"retroarch", "batocera", "emudeck", "retrodeck"},
        # MinUI and MyMinUI list their devices (code_audit_15 P2-1), and on
        # a computer an Android device is no RetroArch of this machine's.
        "r36s": {"retroarch", "rocknix", "myminui"},
        "rg35xx": {"retroarch", "rocknix", "knulli", "minui", "myminui"},
        "trimui": {"retroarch", "knulli", "minui", "nextui"},
        "retroid": {"rocknix", "knulli"},
        "miyoo": {"retroarch", "knulli", "minui", "myminui"},
        "anbernic": {"retroarch", "rocknix", "amberelec", "knulli", "minui", "myminui"},
        "odin": {"rocknix"},
        "ayn": {"retroarch", "rocknix"},
        "rp5": {"rocknix", "knulli"},
        "rog ally": {"retroarch", "emudeck", "retrobat", "bizhawk"},
        "onion": {"retroarch"},
    }

    def rows(self, needle: str) -> list:
        state = fresh_state()
        state.stack = ["platforms"]
        state.filters["platforms"] = needle
        return [row for row in install.tui_rows(state)
                if row.action in ("platform", "cardsystem")]

    def test_each_device_finds_every_setup_it_runs(self):
        for needle, setups in self.TYPED.items():
            with self.subTest(needle=needle):
                self.assertEqual({row.value[0] if row.action == "cardsystem" else row.value
                                  for row in self.rows(needle)}, setups)

    def test_the_right_column_names_what_matched(self):
        rows = self.rows("miyoo")
        # tui_audit_6 P1-1, code_audit_13 P2-3: MiyooCFW stayed on the
        # RetroArch row, which opened this computer's folder.
        self.assertNotIn("retroarch", [row.value for row in rows if row.action == "platform"])
        self.assertEqual([row.label for row in rows if row.action == "cardsystem"],
                         ["Allium, SD card", "Koriki, SD card", "MiyooCFW, SD card",
                          "Onion, SD card", "spruceOS, SD card"])
        spruce = {row.label: row for row in rows}["spruceOS, SD card"]
        self.assertEqual(spruce.right, "Miyoo A30, Miyoo Flip, Miyoo Mini, Miyoo Mini Plus, "
                         "Miyoo Mini Flip")
        # Cut at a comma, never inside a name ("Miyoo ...s").
        self.assertIn("Miyoo A30, Miyoo Flip, +3", spruce.narrow)
        line = str(install._row_line(spruce, False, 60))
        self.assertTrue(line.rstrip().endswith(("+1", "+2", "+3", "+4")), line)
        deck = {row.value: row for row in self.rows("steam deck")}
        self.assertEqual(deck["retroarch"].right, "Steam Deck")
        self.assertEqual({row.value: row for row in self.rows("")}["retroarch"].right, "")

    def test_the_handheld_lists_are_the_guides(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml is not installed")

        guides = yaml.safe_load((REPO_ROOT / "platforms" / "_guides.yml").read_text())
        for system in guides["handheld_guide"]["systems"]:
            if system.get("devices"):
                with self.subTest(system=system["name"]):
                    self.assertEqual(install._SETUP_DEVICES[system["name"]],
                                     tuple(str(name) for name in system["devices"]))


class ACardSetupIsOneDesign(unittest.TestCase):
    """code_audit_12 P2-1 to P3-2, tui_audit_5 P1-2: a setup that runs from
    its SD card, chosen on a computer, always asks for that card."""

    def platforms(self, needle: str = "") -> install.TuiState:
        state = fresh_state()
        state.stack = ["setup", "platforms"]
        state.filters["platforms"] = needle
        state.focus["platforms"] = 1
        return state

    def test_a_handheld_typed_in_the_filter_asks_for_its_card(self):
        for needle in ("onion", "miyoo mini", "rg35xx", "r36s", "arkos", "anbernic"):
            with self.subTest(needle=needle):
                state = self.platforms(needle)
                with mock.patch.object(install, "_mounted_cards", return_value=[]):
                    row = install.tui_rows(state)[1]
                    effects = press(state, "enter")
                    text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
                plat, name = row.value if row.action == "cardsystem" else (row.value,) * 2
                self.assertEqual((effects, state.screen), ([], "folder"))
                self.assertIsNotNone(state.card_bound)
                self.assertEqual(state.card_bound, install.card_bound(name, plat))
                self.assertIn("Where is the SD card?", text)
                self.assertNotIn("default", [r.action for r in install.tui_rows(state)])
                self.assertNotIn(str(SYSTEM_DIR), text)

    def test_retroarch_typed_is_still_this_computer(self):
        state = self.platforms("retroarch")
        press(state, "enter")
        self.assertIsNone(state.card_bound)
        self.assertEqual(state.folder_default, SYSTEM_DIR)

    def test_a_card_backed_out_of_is_forgotten(self):
        state = fresh_state()
        state.stack = ["setup"]
        state.focus["setup"] = 2
        press(state, "enter")
        state.focus["platforms"] = install._platform_focus(state, "rocknix")
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "enter")
            self.assertEqual(state.card_bound, ("ROCKNIX", "roms/bios"))
            press(state, "esc", "esc")
            self.assertEqual(state.screen, "setup")
            state.focus["setup"] = 0
            with mock.patch.object(install.Path, "exists", return_value=True):
                press(state, "enter")
        self.assertIsNone(state.card_bound)
        self.assertEqual((state.platform, state.dest), ("retroarch", SYSTEM_DIR))
        install._open_folder_screen(state, state.dest, "chosen")
        title = install.tui_view(state, 80, 24)[0]
        self.assertIn("Which folder?", title)
        self.assertNotIn("card", [row.action for row in install.tui_rows(state)])

    def test_on_the_device_its_own_folder_is_offered(self):
        share = Path("/userdata/bios")
        state = fresh_state()
        state.setups = [("knulli", share, "KNULLI")]
        state.stack = ["setup", "platforms"]
        state.focus["platforms"] = install._platform_focus(state, "knulli")
        with mock.patch.dict(install._DETECTED_OS, {("knulli", share): "KNULLI"}), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "enter")
            rows = [row.action for row in install.tui_rows(state)]
            text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertEqual(rows, ["default", "browse", "type"])
        self.assertEqual((state.folder_default, state.folder_kind), (share, "found"))
        self.assertIn("Which folder? KNULLI reads its BIOS files from: /userdata/bios", text)
        self.assertNotIn("No KNULLI card is mounted", text)


class OneSystemInFewPresses(unittest.TestCase):
    """tui_audit_4 P3-10.11: one system with a controller took 15 presses on
    the fixture list, about 60 on muxterm, which has no page key."""

    # The buttons each route sends, as the keys the interface reads.
    PADS = {
        "gptokeyb": {"A": "enter", "X": " ", "Start": "enter", "R1": "pgdn"},
        "muxterm": {"A": "enter", "X": " ", "Start": "enter", "R1": "right"},
    }

    def long_list(self, pad: str) -> install.TuiState:
        state = checked_state()
        state.pad = pad
        state.cols, state.rows = 40, 15
        state.systems = [{"event": "system", "platform": "retroarch", "system": f"sys-{n:02d}",
                          "name": f"System {n:02d}", "files": 1, "present": 0, "missing": 1,
                          "wrong_hash": 0, "bytes_needed": 10} for n in range(66)]
        state.ticked = {row["system"] for row in state.systems}
        state.focus["plan"] = next(i for i, row in enumerate(install.tui_rows(state))
                                   if row.action == "install")
        return state

    def press_button(self, state, pad: str, button: str) -> list:
        name = self.PADS[pad].get(button, button)
        return install.tui_update(state, Key("char", name) if len(name) == 1 else Key(name))

    def go_to(self, state, pad: str, target: str, paging: bool) -> int:
        """Presses from the focus to the target system, as a person does."""
        count = 0
        while True:
            rows = install.tui_rows(state)
            focus = state.focus.get("systems", 0)
            index = next(i for i, row in enumerate(rows) if row.value == target)
            if index == focus:
                return count
            # A page is the rows drawn (tui_audit_9 P2-2).
            page = install._page_step(state)
            button = "R1" if paging and index - focus >= page else "down"
            self.press_button(state, pad, button)
            count += 1

    def presses(self, pad: str, fast: bool) -> int:
        """From the plan to the run that installs sys-45 alone, with the
        setup's A before it."""
        state = self.long_list(pad)
        count = 1
        for button in ("down", "A"):
            self.press_button(state, pad, button)
            count += 1
        self.assertEqual(state.screen, "systems")
        paging = pad != "muxterm" or fast
        if fast:
            self.assertEqual(install.tui_rows(state)[state.focus["systems"]].action, "none")
            self.press_button(state, pad, "A")
            count += 1
            count += self.go_to(state, pad, "sys-45", paging)
            effects = self.press_button(state, pad, "A")
            count += 1
        else:
            state.focus["systems"] = 3
            for button in ("up", "up", "A"):
                self.press_button(state, pad, button)
                count += 1
            count += self.go_to(state, pad, "sys-45", paging)
            self.press_button(state, pad, "X")
            effects = self.press_button(state, pad, "Start")
            count += 2
        self.assertEqual(state.ticked, {"sys-45"})
        self.assertEqual(effects, [("run", "confirm")])
        # Install on the confirm screen, then Exit on the result.
        return count + 2

    def test_presses_before_and_after(self):
        # The audit's path, A Down A Up Up A R1 x5 X Start A A. Before the
        # fast path it took 17 presses here, 57 on muxterm (None kept the
        # focus, and muxterm had no page key).
        # A page is the rows drawn since tui_audit_9 P2-2, ten here where it
        # was nine: sys-45 is four pages and five Downs away, not five pages.
        self.assertEqual(self.presses("gptokeyb", fast=False), 19)
        self.assertEqual(self.presses("gptokeyb", fast=True), 16)
        self.assertEqual(self.presses("muxterm", fast=False), 55)
        self.assertEqual(self.presses("muxterm", fast=True), 16)

    def test_a_on_a_system_with_none_ticked_takes_it_alone(self):
        state = self.long_list("keys")
        state.stack.append("systems")
        state.ticked = set()
        state.focus["systems"] = 5
        text = install.tui_view(state, 80, 24)[-1]
        self.assertIn("Enter Only this system", text)
        focused = install.tui_rows(state)[5].value
        self.assertEqual(press(state, "enter"), [("run", "confirm")])
        self.assertEqual(state.ticked, {focused})

    def test_enter_still_continues_with_the_ticks_as_they_are(self):
        state = self.long_list("keys")
        state.stack.append("systems")
        state.focus["systems"] = 5
        # tui_audit_6 P2-1: Continue opens Confirm, which waits for A.
        self.assertEqual(press(state, "enter"), [("run", "confirm")])
        self.assertEqual((state.screen, state.must_confirm), ("confirm", True))
        self.assertEqual(len(state.ticked), 66)

    def test_left_and_right_page_inside_muxterm_only(self):
        for pad in ("muxterm", "gptokeyb_muxterm", "gptokeyb"):
            with self.subTest(pad=pad):
                state = self.long_list(pad)
                state.stack.append("systems")
                state.focus["systems"] = 3
                press(state, "right")
                paged = pad != "gptokeyb"
                self.assertEqual(state.focus["systems"] > 4, paged)
                self.assertEqual(len(state.ticked), 66)
                state.help = True
                help_text = " ".join(install.tui_view(state, 80, 24))
                self.assertEqual("D-pad Left/Right" in help_text, paged)

    def test_a_short_list_opens_on_its_first_system(self):
        state = checked_state()
        state.focus["plan"] = next(i for i, row in enumerate(install.tui_rows(state))
                                   if row.action == "systems")
        press(state, "enter")
        self.assertEqual(install.tui_rows(state)[state.focus["systems"]].action, "system")


class DataFilesHaveASize(unittest.TestCase):
    """tui_audit_4 P3-10.8: "8179 files to download, 6.6 GB, and 2636 data
    files", the data files with no size."""

    def test_the_plan_gives_the_data_files_size(self):
        state = fresh_state()
        state.stack = ["setup"]
        with mock.patch.object(install.Path, "exists", return_value=True):
            install.tui_update(state, Key("enter"))
        install.tui_receive(state, ("manifest", "retroarch", MANIFEST))
        check = {**CHECK, "data_missing": 2636}
        data = {"event": "data", "platform": "retroarch", "files": 2636,
                "bytes": 412 * 1024 ** 2}
        for event in [*FILES, *SYSTEMS, check, data]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={
                "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
            install.tui_receive(state, ("ended", state.run_id, 1, []))
        text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn("and 2636 data files (412.0 MB)", text)


class ThePagerCountsLines(unittest.TestCase):
    """tui_audit_4 P3-9.4: at 40x12 the result read "1-2 of 5" over eight
    lines, counting paragraphs, and its last paragraph was cut."""

    def result_state(self, cols: int, rows: int) -> install.TuiState:
        state = checked_state()
        state.stack.append("result")
        state.code, state.summary = 0, {"installed": 3}
        state.dest = Path("/media/" + "long-folder-name/" * 12)
        state.cols, state.rows = cols, rows
        return state

    def test_the_last_line_is_reachable_at_every_size(self):
        for cols, rows in (*SIZES, (50, 13), (60, 14)):
            with self.subTest(size=f"{cols}x{rows}"):
                state = self.result_state(cols, rows)
                width = min(cols, install.TUI_MAX_WIDTH) - 2
                # The result keeps a path whole (whole=True): its rows after
                # it are left to the terminal's folding ("cont").
                wrapped = install._text_lines(install._pager_items(state), width, True)
                last = [line for line in wrapped if line.style != "cont"][-1]
                press(state, "end")
                screen = install.tui_view(state, cols, rows)
                body = [line.rstrip() for line in screen[2:-1]]
                self.assertIn(" " + str(last).rstrip(), body)
                if len(wrapped) > rows - 3:
                    self.assertTrue(any(re.fullmatch(rf" \d+-{len(wrapped)} of {len(wrapped)}",
                                                     line) for line in body), body)

    def test_the_counter_moves_one_line_per_press(self):
        state = self.result_state(40, 12)
        first = [line for line in install.tui_view(state, 40, 12) if " of " in line][-1]
        self.assertRegex(first, r"^ 1-8 of \d+")
        press(state, "down")
        second = [line for line in install.tui_view(state, 40, 12) if " of " in line][-1]
        self.assertRegex(second, r"^ 2-9 of \d+")


class OfflineInTheInterface(unittest.TestCase):
    """--offline in the full screen: a "No connection" error offering a Try
    again that can never work, and a plan counting downloads the run
    forbids."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_list_names_the_ways_that_work(self):
        inbox = queue.Queue()
        install._tui_fetch("retroarch", inbox, io.StringIO())
        kind, plat, title, lines, code = inbox.get_nowait()
        self.assertEqual((kind, title, code), ("manifest_failed",
                                               "No internet: offline mode", 1))
        text = " ".join(lines)
        for words in ("--manifest", "--from-pack", "without --offline"):
            self.assertIn(words, text)
        state = fresh_state()
        state.stack = ["setup"]
        with mock.patch.object(install.Path, "exists", return_value=True):
            install.tui_update(state, Key("enter"))
        install.tui_receive(state, (kind, plat, title, lines, code))
        self.assertEqual(state.screen, "error")
        self.assertNotIn("retry", [row.action for row in install.tui_rows(state)])
        view = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn("No internet: offline mode", view)
        self.assertNotIn("Try again", view)

    def test_the_plan_offers_no_download(self):
        state = checked_state()
        text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertNotIn("to download", text)
        self.assertIn("need the internet, which --offline does not use", text)
        self.assertNotIn("install", [row.action for row in install.tui_rows(state)])
        # A file found on this machine can still be copied.
        state.found = [{"event": "found", "platform": "retroarch", "dest": FILES[0]["dest"],
                        "folder": "/media/old", "path": "/media/old/scph5500.bin",
                        "member": "", "reason": ""}]
        text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn("1 found in old,", text)
        self.assertIn("need the internet (--offline)", text)
        self.assertEqual(install.tui_rows(state)[0].action, "install")
        self.assertIn("--offline", install.tui_command(state))


class AuditFive(unittest.TestCase):
    """tui_audit_5 P2-1, P2-2 and its P3 points."""

    def long_list(self) -> install.TuiState:
        state = OneSystemInFewPresses().long_list("keys")
        state.cols, state.rows = 80, 24
        return state

    def test_back_from_none_keeps_the_ticks_it_opened_with(self):
        state = self.long_list()
        everything = set(state.ticked)
        press(state, "down", "enter")
        self.assertEqual(install.tui_rows(state)[state.focus["systems"]].action, "none")
        press(state, "enter")
        self.assertEqual(state.ticked, set())
        press(state, "up", "up")
        self.assertEqual(install._primary(state), "A/Enter First system")
        press(state, "esc")
        self.assertEqual(state.screen, "plan")
        self.assertEqual(state.ticked, everything)
        text = " ".join(install.tui_view(state, 80, 24))
        self.assertNotIn("No system is ticked", text)

    def test_back_after_a_choice_keeps_the_choice(self):
        state = self.long_list()
        press(state, "down", "enter", "enter")
        press(state, " ")
        chosen = set(state.ticked)
        press(state, "esc")
        self.assertEqual((state.screen, state.ticked), ("plan", chosen))

    def test_confirm_offline_with_nothing_on_hand_has_no_install(self):
        state = checked_state()
        state.stack.append("confirm")
        state.dry = dict(CHECK)
        state.dry_files = [dict(event, state="missing") for event in FILES]
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            rows = [row.action for row in install.tui_rows(state)]
            text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
            self.assertEqual(rows, ["back"])
            self.assertIn("Copy them from your old card or a folder", text)
            state.found = [{"event": "found", "platform": "retroarch",
                            "dest": FILES[0]["dest"], "folder": "/media/old",
                            "path": "/media/old/x", "member": "", "reason": ""}]
            self.assertEqual(install.tui_rows(state)[0].action, "install")

    def test_the_checking_clock_starts_at_zero(self):
        state = install.TuiState()
        self.assertLess(abs(state.now - time.monotonic()), 5)

    def test_a_pack_installs_from_the_pack(self):
        state = checked_state()
        state.found = [{"event": "found", "platform": "retroarch", "dest": FILES[0]["dest"],
                        "folder": "/media/old", "path": "/media/old/x", "member": "",
                        "reason": "misplaced"}]
        with mock.patch.object(install.LOCAL, "pack", mock.Mock()):
            text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
            # tui_audit_6 P3-3: one count, "4 files to install" above "3 from
            # the pack", where "3 to install from the pack" sat over "4".
            self.assertIn("4 files to install, 1.0 MB", text)
            self.assertIn("3 from the pack, 1 copied from this machine", text)
            self.assertNotIn("downloaded", text)
            self.assertNotIn("of them", text)
            state.finished = [(FILES[0]["dest"], True, ""), (FILES[1]["dest"], True, "")]
            state.local_dests = {FILES[0]["dest"]: ("/media/old", "misplaced")}
            self.assertIn("1 from the pack", install._tui_sources(state, 2, 0))

    def test_the_wrong_place_screen_says_the_copies_stay(self):
        state = checked_state()
        state.stack.append("misplaced")
        self.assertEqual(install._pager_items(state)[0],
                         "They are copied where the emulators read them. Your copies stay "
                         "where they are.")

    def test_the_save_folder_is_a_line_of_its_own(self):
        state = checked_state()
        saves = {"files": 1, "folder": str(HOME / ".config" / "retroarch" / "saves" / "<core>")}
        lines = install._saves_lines(state, saves)
        # In words: "saves/<core>" read as a folder named <core>.
        self.assertEqual(lines, ["1 file read from RetroArch's save directory goes to a "
                                 "folder per core under:", "~/.config/retroarch/saves"])

    def test_the_shared_note_is_one_line(self):
        state = self.long_list()
        state.cols, state.rows = 40, 12
        state.files = [{"event": "file", "dest": "mame.zip", "size": 10,
                        "systems": ["sys-00", "sys-01"]}]
        state.stack.append("systems")
        state.focus["systems"] = 3
        lines = install.tui_view(state, 40, 12)
        self.assertIn(" Shared, counted once", lines)

    def test_the_save_folder_remedy_is_the_same_on_both_screens(self):
        summary = {"skipped": [{"reason": "no_save_directory", "files": 1}]}
        state = checked_state()
        lines = install._unplaced_lines(summary, state)
        self.assertIn("Options, Save folder (--save-dir)", lines[0])

    def test_a_size_keeps_its_unit(self):
        for width in range(20, 45):
            for line in install._wrap("2 files to install from the pack, 5.1 KB", width):
                self.assertNotEqual(line.strip(), "KB")

    def test_the_miyoo_bar_names_the_tick(self):
        state = self.long_list()
        state.pad = "miyoo"
        press(state, "down", "enter")
        state.ticked = set()
        state.focus["systems"] = 3
        bar = install.tui_view(state, 40, 15)[-1]
        self.assertIn("A Tick", bar)
        self.assertTrue(bar.strip().startswith("Start Only this"))
        # tui_audit_6 P3-9.1: Back was the entry given up at 40 columns.
        self.assertIn("R2 Back", bar)

    def test_muxterm_help_names_no_home_key(self):
        state = self.long_list()
        state.pad = "muxterm"
        press(state, "down", "enter")
        keys = [key for key, _what in install._help_lines(state)]
        self.assertNotIn("Home/End, g/G", keys)
        state.pad = "keys"
        self.assertIn("Home/End, g/G", [key for key, _what in install._help_lines(state)])


class LineModeOffline(unittest.TestCase):
    """tui_audit_5 P2-3 and P3-8.4: the line mode under --offline asked to
    download, and said nothing of the data files."""

    @unittest.skipUnless(pyte and pty and hasattr(pty, "fork"),
                         "the line mode run needs pyte and a pty")
    def test_nothing_is_offered_that_cannot_come(self):
        with FakeRepository() as repo:
            env = {**repo.env(), "TERM": "xterm", install.OFFLINE_VARIABLE: "1"}
            term = Pty(["--plain", "--manifest", str(repo.root / "install" / "retroarch.json"),
                        "--dest", str(repo.dest), "--offline"], env)
            term.wait("q) quit")
            term.send("\r")
            code = term.close()
            out = term.raw.decode("utf-8", "replace")
        self.assertIn("--offline: only the files found on this machine are installed, and no "
                      "--from names a folder to copy them from.", out)
        # tui_audit_7 P3-4: nothing to copy from, nothing to install.
        self.assertIn("Enter) check the BIOS folder, nothing can be downloaded", out)
        self.assertIn("f) copy from a folder or an old card", out)
        self.assertIn("5 files need the internet, which --offline does not use.", out)
        self.assertNotIn("Downloading", out)
        self.assertNotIn("failed", out.lower())
        # tui_audit_6 P2-3, code_audit_13 P1-1: nothing installed ended on
        # exit 0, where the screen's run gives 2.
        self.assertIn("Nothing installed: 5 files need the internet, which --offline does "
                      "not use.", out.replace("\r\n", " ").replace("\n", " "))
        self.assertNotIn("already up to date", out)
        self.assertEqual(code, 2)

    def test_the_data_files_are_counted_before_the_question(self):
        files = [{"dest": "a.bin", "size": 1024}]
        trees = [{"ref": "t", "files": {"x": {"size": 2048}, "y": {"size": 2048}}}]
        with mock.patch.object(install, "_ask", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            install._prompt_custom_selection("retroarch", files, [], data_trees=trees)
        self.assertIn("RetroArch: 1 file, 1.0 KB, and 2 data files (4.0 KB).", out.getvalue())

    def test_one_save_file_goes(self):
        files = [{"dest": "keys.txt", "root": "save", "size": 1}]
        place = install.SavePlace(Path("/saves"), False)
        with mock.patch.object(install, "save_directory_for", return_value=place), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            install.place_save_entries(files, Path("/bios"), {}, None, "RetroArch")
        self.assertIn("1 file read from RetroArch's save directory goes to /saves",
                      out.getvalue())


class NarrowSystemNames(unittest.TestCase):
    """At 40 columns the size column took half the line: Game Boy, Game Boy
    Advance and Game Boy Color all read "Nintendo Gam"."""

    def test_the_name_comes_before_the_size(self):
        state = checked_state()
        names = {"nintendo-gb": "Nintendo Game Boy", "nintendo-gba": "Nintendo Game Boy Advance",
                 "nintendo-gbc": "Nintendo Game Boy Color",
                 "videoton-tvc": "Videoton TV Computer"}
        state.manifest = {**state.manifest, "system_titles": names}
        state.systems = [
            {"event": "system", "platform": "retroarch", "system": system, "name": system,
             "files": 30, "present": 0, "missing": 30, "wrong_hash": 0,
             "bytes_needed": 30 * 1024 * 1024}
            for system in names]
        state.files = [{"event": "file", "platform": "retroarch", "dest": f"s{n}.bin",
                        "systems": list(names), "size": 1024, "state": "missing"}
                       for n in range(27)]
        state.ticked = set(names)
        state.stack.append("systems")
        for cols, rows in ((40, 15), (40, 12), (52, 15)):
            with self.subTest(size=f"{cols}x{rows}"):
                text = "\n".join(install.tui_view(state, cols, rows))
                # tui_audit_10 P3-14: the size stays as the total counts it,
                # 30.0 MB, and a name too long beside it is cut where the
                # three still read apart.
                lines = [line for line in text.splitlines() if "[x]" in line]
                self.assertEqual(len(lines), 4, text)
                self.assertTrue(all(line.rstrip().endswith(("30.0 MB", "30.0 MB + 27 shared"))
                                    for line in lines), text)
                labels = [line.split("[x] ")[1].split("30.0 MB", 1)[0].strip()
                          for line in lines]
                self.assertEqual(len(set(labels)), 4, text)
                for label, name in zip(labels, names.values()):
                    stem = label[:-3] if label.endswith("...") else label
                    self.assertTrue(name.startswith(stem), (label, name))
                for line in text.splitlines():
                    self.assertLessEqual(install.cell_width(line), cols)
        # Wide, the full form stays.
        self.assertIn("3 files, 30.0 MB + 27 shared",
                      "\n".join(install.tui_view(state, 120, 40)))

    def test_one_spelling_of_shared_and_the_focused_size_below(self):
        """At 40 columns the shared part read "+ 1689 shared", "+ 28 sh.",
        ", shared" and ", all shared" on one screen, and a long name kept
        whole lost its size."""
        state = checked_state()
        names = {"epoch-scv": "Epoch Super Cassette Vision", "sega-dc": "Sega Dreamcast",
                 "snk-ng": "SNK Neo Geo"}
        state.manifest = {**state.manifest, "system_titles": names}
        state.systems = [
            {"event": "system", "platform": "retroarch", "system": system, "name": system,
             "files": count, "present": 0, "missing": count, "wrong_hash": 0,
             "bytes_needed": count * 1024}
            for system, count in (("epoch-scv", 30), ("sega-dc", 1690), ("snk-ng", 3))]
        state.files = [{"event": "file", "platform": "retroarch", "dest": f"s{n}.bin",
                        "systems": ["sega-dc", "snk-ng"], "size": 1024, "state": "missing"}
                       for n in range(3)]
        state.ticked = set(names)
        state.stack.append("systems")
        spellings = set()
        for cols in (40, 46, 52, 80):
            text = "\n".join(install.tui_view(state, cols, 15))
            spellings |= set(re.findall(r"\+ \d+ sh\w*\.?|, (?:all )?shared", text))
        self.assertLessEqual(spellings, {"+ 3 shared", ", all shared"}, spellings)
        state.focus["systems"] = next(
            index for index, row in enumerate(install.tui_rows(state))
            if row.value == "epoch-scv")
        text = " ".join(" ".join(install.tui_view(state, 40, 15)).split())
        self.assertIn("Epoch Super Cassette Vision: 30 files, 30.0 KB", text)


class Crash(unittest.TestCase):
    """An unexpected error ends on one sentence and a log, never a traceback."""

    class FakeTerminal:
        left = False

        def __init__(self, *_streams) -> None:
            self.frame = []

        def enter(self) -> None:
            pass

        def leave(self) -> None:
            Crash.FakeTerminal.left = True

        def size(self) -> tuple:
            return 80, 24

        def draw(self, lines, cols) -> None:
            pass

        def keys(self, timeout):
            raise RuntimeError("a defect")

    def test_the_terminal_is_restored_and_one_line_printed(self):
        args = _args()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(install, "_TuiTerminal", self.FakeTerminal), \
                mock.patch.object(install, "tui_start", return_value=(fresh_state(), [])), \
                mock.patch.object(install, "_open_local_sources"), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = install.run_tui(args)
        self.assertEqual(code, 1)
        self.assertTrue(self.FakeTerminal.left)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("stopped on an unexpected error", lines[0])
        log = Path(lines[0].rsplit(" ", 1)[-1].rstrip("."))
        self.addCleanup(shutil.rmtree, log.parent, True)
        self.assertIn("RuntimeError: a defect", log.read_text())
        self.assertNotIn("Traceback", err.getvalue() + out.getvalue())


class TheSaveFolderLineFollowsTheChoice(unittest.TestCase):
    """The save-folder line was the check's: with the system owning the
    save file unticked, the result named a folder nothing was written to,
    and kept, the Confirm screen did not name it."""

    SAVES = {"event": "saves", "platform": "retroarch", "files": 1,
             "folder": "/home/user/MySaves/<core>"}
    LINE = ("1 file read from RetroArch's save directory goes to a folder per core under: "
            "~/MySaves")

    def said(self, lines) -> str:
        return " ".join(" ".join(lines).split())

    def run_to_result(self, confirm_saves: bool, install_saves: bool) -> install.TuiState:
        state = checked_state()
        state.saves = dict(self.SAVES)
        state.must_confirm = True
        state.stack.append("confirm")
        install._start_run(state, "confirm")
        dry = {**CHECK, "files": 3, "missing": 3, "wrong_hash": 0}
        for event in [*([self.SAVES] if confirm_saves else []), dry]:
            install.tui_receive(state, ("event", state.run_id, event))
        install.tui_receive(state, ("ended", state.run_id, 1, []))
        self.confirm = self.said(install.tui_view(state, 200, 40))
        install._start_run(state, "install")
        summary = {"event": "summary", "mode": "install", "installed": 3, "up_to_date": 0,
                   "pending": 0, "failed": [], "skipped": [], "left_out": [], "kept": [],
                   "interrupted": False}
        for event in [*([self.SAVES] if install_saves else []), summary]:
            install.tui_receive(state, ("event", state.run_id, event))
        install.tui_receive(state, ("ended", state.run_id, 0, []))
        return state

    def test_unticked_it_is_named_nowhere(self):
        state = self.run_to_result(confirm_saves=False, install_saves=False)
        self.assertNotIn("save directory", self.confirm)
        self.assertEqual(state.screen, "result")
        self.assertNotIn("save directory", self.said(install.tui_outcome(state)[1]))

    def test_kept_the_confirm_screen_and_the_result_name_it(self):
        state = self.run_to_result(confirm_saves=True, install_saves=True)
        self.assertIn(self.LINE, self.confirm)
        self.assertIn(self.LINE, self.said(install.tui_outcome(state)[1]))


@unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                 "a folder without search permission is POSIX")
class AFolderThisUserCannotOpen(unittest.TestCase):
    """A BIOS folder under a folder this user cannot enter: given with
    --dest the interface ended on a traceback before its first screen, and
    typed on the folder screen it ended on a crash log."""

    def setUp(self):
        base = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, base, True)
        self.lock = base / "lock"
        self.lock.mkdir()
        os.chmod(self.lock, 0o000)
        self.addCleanup(os.chmod, self.lock, 0o755)
        self.dest = self.lock / "system"

    def said(self, state: install.TuiState) -> str:
        return " ".join(" ".join(install.tui_view(state, 200, 40)).split())

    def test_typed_it_stays_on_the_path_screen(self):
        state = fresh_state()
        state.platform = "retroarch"
        state.stack = ["setup", "folder", "path"]
        state.path_text = str(self.dest)
        self.assertEqual(install.tui_update(state, Key("enter")), [])
        self.assertEqual(state.screen, "path")
        self.assertEqual(state.path_text, str(self.dest))
        self.assertIn(f"This user cannot open {install._short_path(self.lock, state.home)} "
                      "(Permission denied)", self.said(state))

    def test_given_with_dest_opens_the_folder_screen(self):
        args = _args(platform="retroarch", dest=str(self.dest))
        with mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            state, effects = install.tui_start(args, "linux")
        self.assertEqual(effects, [])
        self.assertEqual(state.screen, "folder")
        self.assertIn(f"This user cannot open {install._short_path(self.lock, state.home)} "
                      "(Permission denied)", self.said(state))
        self.assertIn("choose another folder", self.said(state))

    def test_chosen_from_the_setups(self):
        state = fresh_state()
        state.setups = [("retroarch", self.dest, "RetroArch")]
        state.stack = ["setup"]
        self.assertEqual(install.tui_update(state, Key("enter")), [])
        self.assertEqual(state.screen, "folder")
        # The folder found is the one RetroArch reads: its built-in default
        # is not, and was offered with Enter on "Use this folder".
        self.assertIsNone(state.folder_default)
        self.assertNotIn("reads its BIOS files from", self.said(state))
        self.assertIn(f"This user cannot open {install._short_path(self.lock, state.home)} "
                      "(Permission denied)", self.said(state))

    def test_a_home_it_cannot_open_is_said_on_the_first_screen(self):
        """HOME kept across su without -: detection raised PermissionError
        before the first screen."""
        home = self.lock.parent / "home"
        home.mkdir()
        os.chmod(home, 0o000)
        self.addCleanup(os.chmod, home, 0o755)
        install._DETECTION_REFUSED.clear()
        self.addCleanup(install._DETECTION_REFUSED.clear)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            state, _effects = install.tui_start(_args(), "linux")
        self.assertIn("Could not look for setups: this user cannot open", self.said(state))

    def test_the_nearest_folder_stops_above_it(self):
        self.assertEqual(install._nearest_folder(self.dest / "deeper"), self.lock)

    def test_a_closed_folder_under_home_hides_nothing_else(self):
        """~/.config/EmuDeck at 000 made detection return nothing, and the
        folder screen offered ~/.config/retroarch/system, which this
        RetroArch does not read: its retroarch.cfg names another."""
        home = self.lock.parent / "home"
        closed = home / ".config" / "EmuDeck"
        closed.mkdir(parents=True)
        cfg = home / ".config" / "retroarch" / "retroarch.cfg"
        cfg.parent.mkdir()
        system = self.lock.parent / "elsewhere" / "system"
        system.mkdir(parents=True)
        cfg.write_text(f'system_directory = "{system}"\n')
        os.chmod(closed, 0o000)
        self.addCleanup(os.chmod, closed, 0o755)
        install._DETECTION_REFUSED.clear()
        self.addCleanup(install._DETECTION_REFUSED.clear)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_RETROARCH_CONFIGS", {}):
            state, _effects = install.tui_start(_args(), "linux")
        self.assertEqual([setup[:2] for setup in state.setups], [("retroarch", system)])
        screen = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertEqual(screen.count("Could not look for setups: this user cannot open "
                                      "~/.config/EmuDeck (Permission denied)"), 1, screen)

    def test_the_refusal_is_drawn_above_a_full_list(self):
        """With a closed HOME the first screen is the 18 setups: the line,
        drawn under them, was dropped at 80x24 and at 40x15."""
        home = self.lock.parent / "home"
        home.mkdir()
        os.chmod(home, 0o000)
        self.addCleanup(os.chmod, home, 0o755)
        install._DETECTION_REFUSED.clear()
        self.addCleanup(install._DETECTION_REFUSED.clear)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            state, _effects = install.tui_start(_args(), "linux")
        self.assertEqual(state.screen, "platforms")
        for cols, rows in ((80, 24), (40, 15)):
            with self.subTest(size=f"{cols}x{rows}"):
                lines = install.tui_view(state, cols, rows)
                self.assertEqual(len(lines), rows)
                screen = " ".join(" ".join(lines).split())
                self.assertIn("Could not look for setups", screen)
                self.assertIn("name the BIOS folder with --dest.", screen)
                # The list keeps its focused row in view.
                self.assertIn("RetroArch", screen)


class AGoesIntoASubfolder(unittest.TestCase):
    """At HOME or a drive root A went into the first "dir" row: a mounted
    card or drive listed first (/mnt/d, the Windows D: drive, then took
    the BIOS files at its root) or .cache."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.card = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.card, True)
        self.state = install.TuiState(os_type="linux", home=self.home)
        self.state.platform = "retroarch"
        self.state.stack = ["folder", "browse"]

    def focused(self) -> install.TuiRow:
        return self.state.browse_rows[self.state.focus["browse"]]

    def test_a_card_and_cache_are_passed_over(self):
        (self.home / ".cache").mkdir()
        (self.home / "Games").mkdir()
        with mock.patch.object(install, "_mounted_cards", return_value=[self.card]):
            install._browse_to(self.state, self.home)
        self.assertEqual(self.state.browse_rows[2].value, self.card)
        self.assertEqual(self.focused().label, "Games/")
        self.assertEqual(self.state.status, "")

    def test_with_no_visible_subfolder_a_folder_below_takes_the_focus(self):
        """tui_audit_10 P2-5: the focus stayed on Use this folder under the
        sentence that warns against it, and A, A, A installed BizHawk into
        the home folder."""
        (self.home / ".cache").mkdir()
        (self.home / ".config").mkdir()
        with mock.patch.object(install, "_mounted_cards", return_value=[self.card]):
            install._browse_to(self.state, self.home)
        self.assertEqual(self.focused().label, ".cache/")
        self.assertIn("No setup reads BIOS files in your home folder", self.state.status)
        screen = " ".join(" ".join(install.tui_view(self.state, 80, 24)).split())
        self.assertIn("No setup reads BIOS files in your home folder.", screen)

    def test_on_windows_the_other_drives_are_passed_over(self):
        """The card rows are drive roots there (E:\\): at D:\\ the first
        was E:\\, and at E:\\ it was D:\\."""
        (self.home / "Games").mkdir()
        drives = [Path("D:\\"), Path("E:\\")]
        windows = WindowsOs()
        windows.path = types.SimpleNamespace(
            **{name: getattr(os.path, name) for name in dir(os.path)
               if not name.startswith("_")})
        windows.path.exists = lambda path: str(path) in ("C:\\", "D:\\", "E:\\")
        with mock.patch.object(install, "os", windows), \
                mock.patch.object(install, "_mounted_cards", return_value=drives):
            install._browse_to(self.state, self.home)
            self.assertEqual(self.focused().label, "Games/")
            # The drives list keeps its first drive focused.
            install._browse_to(self.state, None)
            self.assertEqual([row.label for row in self.state.browse_rows],
                             ["C:\\", "D:\\", "E:\\"])
            self.assertEqual(self.state.focus["browse"], 0)


# POSIX modules a Windows Python does not have: importing one raises there.
POSIX_ONLY = ("termios", "tty", "pty", "fcntl", "select", "resource", "pwd", "grp")
# The signal names a Windows Python defines.
WINDOWS_SIGNALS = ("SIGABRT", "SIGBREAK", "SIGFPE", "SIGILL", "SIGINT", "SIGSEGV",
                   "SIGTERM")


class WindowsOs(types.ModuleType):
    """The os module with os.name "nt", everything else delegated."""

    def __init__(self) -> None:
        super().__init__("os")
        self.name = "nt"

    def __getattr__(self, attribute: str) -> object:
        return getattr(os, attribute)


class SimulatedWindows:
    """os.name "nt", the POSIX modules unimportable, the signal module cut
    down to the names Windows has, and msvcrt reading the given keys."""

    def __init__(self, keys: str = "") -> None:
        self.keys = list(keys)
        self.handlers: dict[int, object] = {}
        self.locked: list[int] = []
        self.msvcrt = types.SimpleNamespace(
            kbhit=lambda: bool(self.keys), getwch=lambda: self.keys.pop(0),
            locking=lambda fd, mode, size: self.locked.append(fd), LK_NBLCK=2)
        self.signal = types.ModuleType("signal")
        for number, name in enumerate(WINDOWS_SIGNALS, start=100):
            setattr(self.signal, name, number)
        self.signal.SIG_DFL, self.signal.SIG_IGN = 0, 1
        self.signal.CTRL_C_EVENT, self.signal.CTRL_BREAK_EVENT = 0, 1
        self.signal.signal = self._install
        self.signal.getsignal = lambda number: self.handlers.get(number, 0)
        self.stack = contextlib.ExitStack()

    def _install(self, number: int, handler: object) -> object:
        if number not in {getattr(self.signal, name) for name in WINDOWS_SIGNALS}:
            raise ValueError(f"invalid signal value {number}")
        previous = self.handlers.get(number, 0)
        self.handlers[number] = handler
        return previous

    def __enter__(self) -> SimulatedWindows:
        self.stack.enter_context(mock.patch.dict(
            sys.modules, {**{name: None for name in POSIX_ONLY}, "msvcrt": self.msvcrt}))
        # install.py alone sees "nt": pathlib here keeps making PosixPaths.
        self.stack.enter_context(mock.patch.object(install, "os", WindowsOs()))
        self.stack.enter_context(mock.patch.object(install, "signal", self.signal))
        return self

    def __exit__(self, *exc: object) -> None:
        self.stack.close()


class WindowsHasNoPosixModules(unittest.TestCase):
    """_terminal_gone imported termios unguarded: on Windows a closed console
    raised ModuleNotFoundError inside run_tui's except block, a traceback
    instead of an exit code. Every path the interface takes on Windows runs
    here with the POSIX modules gone."""

    class Console(io.StringIO):
        broken = False

        def fileno(self) -> int:
            return 0

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            if self.broken:
                raise OSError(errno.EPIPE, "The pipe is being closed")
            return super().write(text)

    def test_the_simulation_refuses_posix_imports(self):
        with SimulatedWindows():
            for name in POSIX_ONLY:
                with self.subTest(module=name), self.assertRaises(ImportError):
                    __import__(name)
            self.assertFalse(hasattr(install.signal, "SIGTSTP"))

    def test_a_closed_console_is_a_closed_terminal(self):
        console = self.Console()
        with SimulatedWindows():
            terminal = install._TuiTerminal(console, console)
            self.assertTrue(install._closed_terminal(
                OSError(errno.EPIPE, "The pipe is being closed"), terminal))
            self.assertFalse(install._closed_terminal(
                OSError(errno.ENOENT, "No such file"), terminal))

    def test_keys_enter_leave_and_cleanup(self):
        console = self.Console()
        with SimulatedWindows("a\xe0H") as windows:
            terminal = install._TuiTerminal(console, console)
            terminal.enter()
            self.assertEqual([(key.name, key.text) for key in terminal.keys(0.05)],
                             [("char", "a"), ("up", "")])
            handlers = {windows.signal.SIGINT: windows.signal.SIG_DFL}
            self.assertEqual(install._tui_cleanup(terminal, [], handlers, fresh_state()), [])
            self.assertIn(install._SCREEN_OFF, console.getvalue())

    def test_a_child_is_interrupted_through_the_console(self):
        run = install._EngineRun.__new__(install._EngineRun)
        run.proc = mock.Mock(**{"poll.return_value": None})
        with SimulatedWindows(), mock.patch.object(install.os, "kill") as kill:
            run.interrupt(from_key=False)
            kill.assert_not_called()
            run.proc.send_signal.assert_not_called()
            run.interrupt(from_key=True)
            kill.assert_called_once_with(0, install.signal.CTRL_C_EVENT)

    def test_work_folder_owner_and_partial_lock(self):
        work = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, work, True)
        (work / install.TUI_OWNER).write_text(str(os.getpid()), encoding="utf-8")
        with SimulatedWindows() as windows:
            self.assertIsNone(install._owner_alive(work))
            handle = install._claim_partial(work / "x.bin.part")
            self.assertIsNotNone(handle)
            handle.close()
            self.assertEqual(len(windows.locked), 1)

    def test_run_tui_ends_on_the_closed_console_code(self):
        console = self.Console()
        real_draw = install._TuiTerminal.draw

        def breaking_draw(terminal, lines, cols):
            console.broken = True
            real_draw(terminal, lines, cols)

        work = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, work, True)
        with SimulatedWindows() as windows, \
                mock.patch.object(install._TuiTerminal, "draw", breaking_draw), \
                mock.patch.object(install, "_drop_closed_terminal"), \
                mock.patch.object(install.tempfile, "mkdtemp", return_value=str(work)), \
                mock.patch.object(install, "tui_start", return_value=(fresh_state(), [])), \
                mock.patch.object(install, "_open_local_sources"), \
                mock.patch.object(install, "detect_os", return_value="windows"), \
                mock.patch.object(sys, "stdin", console), \
                mock.patch.object(sys, "stdout", console), \
                mock.patch.object(sys, "stderr", console):
            code = install.run_tui(_args())
            # SIGHUP does not exist there: the closed window's code is 129.
            self.assertEqual(code, 129)
            self.assertFalse((work / "error.log").exists())
            self.assertTrue(all(handler == 0 for handler in windows.handlers.values()),
                            windows.handlers)


# --- End to end, on a pseudo-terminal -------------------------------------


class Pty:
    """install.py on a pseudo-terminal, its screen read back through pyte."""

    def __init__(self, args: list[str], env: dict, cols: int = 80, rows: int = 24,
                 program: list[str] | None = None) -> None:
        """program runs instead of this Python on install.py: the wrapper
        a user runs, install.sh."""
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.raw = b""
        with warnings.catch_warnings():
            # The child replaces itself at once: the fork-with-threads hazard
            # (a lock held by the loopback server thread) never reaches it.
            warnings.simplefilter("ignore", DeprecationWarning)
            self.pid, self.fd = pty.fork()
        if self.pid == 0:
            argv = program or [sys.executable, str(REPO_ROOT / "install.py")]
            os.execve(argv[0], [*argv, *args], env)
        self.resize(cols, rows)
        self.status: int | None = None

    def resize(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.screen.resize(rows, cols)

    def _pump(self, wait: float) -> bool:
        if not select.select([self.fd], [], [], wait)[0]:
            return True
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            return False
        if not chunk:
            return False
        self.raw += chunk
        self.stream.feed(chunk)
        return True

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def settle(self, quiet: float = 0.3, timeout: float = 10.0) -> str:
        """The screen once nothing more arrives for `quiet` seconds.

        text() shows only what was read: a loop that sent a key and looked
        again saw the screen before the key, and on a slow runner pressed
        Enter twelve rows past the folder it was looking for.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([self.fd], [], [], quiet)[0]:
                break
            if not self._pump(0):
                break
        return self.text()

    def wait(self, needle: str, timeout: float = 20.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.text():
                return self.text()
            if not self._pump(0.05):
                break
        if needle in self.text():
            return self.text()
        raise AssertionError(f"{needle!r} never shown:\n{self.text()}")

    def send(self, *keys: str) -> None:
        for key in keys:
            os.write(self.fd, key.encode())
            # The interface waits 40 ms to tell Esc from an arrow.
            time.sleep(0.15)

    def close(self, timeout: float = 30.0) -> int:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                while self._pump(0.05) and select.select([self.fd], [], [], 0)[0]:
                    pass
                # The modes the run left the terminal in: the master reads
                # the slave's.
                self.modes = termios.tcgetattr(self.fd)
                os.close(self.fd)
                self.status = os.WEXITSTATUS(status)
                return self.status
            self._pump(0.05)
        os.kill(self.pid, 9)
        raise AssertionError(f"still running:\n{self.text()}")

    def after(self) -> str:
        """What the terminal keeps once the full screen is gone."""
        return self.raw.split(b"\x1b[?1049l")[-1].decode("utf-8", "replace")


class _SlowHandler(_QuietHandler):
    """Serves each file after a pause, so a test can stop a download midway."""

    def do_GET(self) -> None:  # noqa: N802, D102
        if self.path.startswith("/bios/"):
            time.sleep(0.6)
        super().do_GET()


class GameDataRepository(FakeRepository):
    """The fake repository with a categorized list and one slow file per file."""

    def __init__(self, slow: bool = False) -> None:
        super().__init__()
        self.slow = slow

    def __enter__(self) -> GameDataRepository:
        super().__enter__()
        path = self.root / "install" / "retroarch.json"
        manifest = json.loads(path.read_text())
        manifest["categorized"] = True
        payload = b"a game's data"
        (self.root / "bios" / "game.dat").write_bytes(payload)
        manifest["files"].append({
            "dest": "game.dat", "sha1": hashlib.sha1(payload).hexdigest(),
            "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
            "repo_path": "bios/game.dat", "cores": None, "systems": ["nintendo-gba"],
            "category": "game_data"})
        path.write_text(json.dumps(manifest))
        if self.slow:
            self.httpd.shutdown()
            self.httpd.server_close()
            handler = functools.partial(_SlowHandler, directory=str(self.root))
            self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self


class SaveDirectoryRepository(FakeRepository):
    """The fake repository with one file read from RetroArch's save
    directory, as Azahar's keys.txt and 105 others of the real list."""

    KEYS = b"azahar keys"

    def __enter__(self) -> SaveDirectoryRepository:
        super().__enter__()
        path = self.root / "install" / "retroarch.json"
        manifest = json.loads(path.read_text())
        (self.root / "bios" / "keys.txt").write_bytes(self.KEYS)
        manifest["files"].append({
            "dest": "Azahar/Azahar/sysdata/keys.txt", "root": "save",
            "core_folder": "Azahar", "sha1": hashlib.sha1(self.KEYS).hexdigest(),
            "sha256": hashlib.sha256(self.KEYS).hexdigest(), "size": len(self.KEYS),
            "repo_path": "bios/keys.txt", "cores": ["azahar"],
            "systems": ["nintendo-3ds"]})
        manifest["save_directory"] = {"folder": "saves", "by_core": True}
        manifest["base_destination"] = "system"
        path.write_text(json.dumps(manifest))
        return self


def _app_card(test: unittest.TestCase, system: str) -> tuple[Path, Path]:
    """A card of system in the repository's tmp/, its root and BIOS folder."""
    from test_install_app import make_card  # noqa: PLC0415

    (REPO_ROOT / "tmp").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
    test.addCleanup(shutil.rmtree, root, True)
    return root / "card", make_card(root / "card", system)


def _installed_state(dest: Path, code: int = 0, failed: list | None = None) -> install.TuiState:
    """The result screen of an install into dest."""
    state = fresh_state()
    state.platform, state.dest, state.manifest = "retroarch", dest, MANIFEST
    state.stack, state.run, state.code = ["result"], "install", code
    state.summary = {"event": "summary", "mode": "install", "installed": 5, "up_to_date": 0,
                     "pending": 0, "failed": failed or [], "skipped": [], "left_out": [],
                     "kept": [], "interrupted": False}
    return state


class AppOnTheCard(unittest.TestCase):
    """The RetroBIOS app offered on the result screen and in the options."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(install.OFFLINE_VARIABLE, None)

    def fields(self, card_root: Path, ok: bool = True) -> dict:
        return {"event": "app", "system": "muOS", "folder": str(card_root), "ok": ok,
                "reason": "" if ok else "it could not be downloaded: no connection",
                "dests": ["ARCHIVE/RetroBIOS.muxapp"],
                "written": ["ARCHIVE/RetroBIOS.muxapp"] if ok else [], "kept": []}

    def test_the_result_offers_it_and_exit_stays_the_default(self):
        root, bios = _app_card(self, "muOS")
        state = _installed_state(bios)
        rows = install.tui_rows(state)
        self.assertEqual([row.action for row in rows], ["exit", "addapp"])
        self.assertEqual(rows[1].label, install.APP_OFFER)
        screen = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn(install.APP_OFFER, screen)
        self.assertIn("A/Enter Exit", screen)
        self.assertEqual(press(state, "enter"), [])
        self.assertEqual(state.exit_code, 0)

    def test_choosing_it_puts_it_then_says_what_it_did(self):
        root, bios = _app_card(self, "muOS")
        state = _installed_state(bios)
        effects = press(state, "down", "enter")
        self.assertEqual(effects, [("app", install.card_app(bios))])
        self.assertTrue(state.app_running)
        screen = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn("Putting RetroBIOS on the card...", screen)
        # Nothing ends the screen while the card is written.
        self.assertEqual(press(state, "enter", "esc"), [])
        self.assertIsNone(state.exit_code)
        install.tui_receive(state, ("app", self.fields(root)))
        self.assertEqual(install.tui_rows(state), [])
        screen = "\n".join(install.tui_view(state, 80, 24))
        self.assertIn("RetroBIOS is on the muOS card: 1 file written", screen)
        self.assertIn("Archive Manager", screen)
        self.assertIn("--add-app", install.tui_command(state))
        press(state, "enter")
        self.assertEqual(state.exit_code, 0)

    def test_a_failure_is_said_and_ends_on_1(self):
        root, bios = _app_card(self, "KNULLI")
        state = _installed_state(bios)
        press(state, "down", "enter")
        install.tui_receive(state, ("app", {**self.fields(root, ok=False), "system": "KNULLI"}))
        _title, lines = install.tui_outcome(state)
        self.assertTrue(any("could not be put on the KNULLI card" in line for line in lines))
        press(state, "enter")
        self.assertEqual(state.exit_code, 1)

    def test_space_and_b_never_put_it(self):
        _root, bios = _app_card(self, "muOS")
        state = _installed_state(bios)
        self.assertEqual(press(state, "down", " "), [])
        self.assertFalse(state.app_running)
        press(state, "esc")
        self.assertEqual(state.exit_code, 0)

    def test_no_offer_where_it_cannot_go(self):
        _root, muos = _app_card(self, "muOS")
        _root, onion = _app_card(self, "Onion")
        cases = {
            "failed install": _installed_state(muos, 1, [{"dest": "a", "reason": "x"}]),
            "plain folder": _installed_state(SYSTEM_DIR),
            "no launcher": _installed_state(onion),
        }
        offline = _installed_state(muos)
        for name, state in cases.items():
            with self.subTest(case=name):
                self.assertEqual(install.tui_rows(state), [])
        os.environ[install.OFFLINE_VARIABLE] = "1"
        self.assertEqual(install.tui_rows(offline), [])
        os.environ.pop(install.OFFLINE_VARIABLE)
        _title, lines = install.tui_outcome(cases["no launcher"])
        self.assertIn(install.no_python_words("Onion"), lines)

    def test_put_by_the_install_itself_no_second_offer(self):
        root, bios = _app_card(self, "muOS")
        state = _installed_state(bios)
        install._take_event(state, self.fields(root))
        self.assertEqual(install.tui_rows(state), [])
        self.assertIn("RetroBIOS is on the muOS card", "\n".join(install.tui_outcome(state)[1]))

    def test_the_options_row_sets_the_flag_of_the_install_only(self):
        _root, bios = _app_card(self, "muOS")
        state = _installed_state(bios)
        state.stack = ["plan", "options"]
        rows = install.tui_rows(state)
        row = next(row for row in rows if row.action == "addapp")
        self.assertEqual((row.label, row.right), ("Put RetroBIOS on this card: no", "--add-app"))
        state.focus["options"] = rows.index(row)
        press(state, "enter")
        self.assertTrue(state.add_app)
        self.assertIn("--add-app", install.tui_argv(state, "install", "py", "install.py"))
        for run in ("check", "confirm", "preview"):
            self.assertNotIn("--add-app", install.tui_argv(state, run, "py", "install.py"))
        self.assertIn("--add-app", install.tui_command(state))
        press(state, "left")
        self.assertFalse(state.add_app)
        plain = _installed_state(SYSTEM_DIR)
        plain.stack = ["plan", "options"]
        self.assertFalse(any(row.action == "addapp" for row in install.tui_rows(plain)))

    def test_the_command_line_flag_ticks_the_row(self):
        _root, bios = _app_card(self, "ROCKNIX")
        state, _effects = install.tui_start(_args(add_app=True, platform="rocknix",
                                                  dest=bios), "linux", detect=False)
        self.assertTrue(state.add_app)


@unittest.skipUnless(pyte and pty and hasattr(pty, "fork"),
                     "screen tests need pyte (pip install pyte==0.8.2) and a pty")
class EndToEnd(unittest.TestCase):
    """The real install.py --tui, keys in and screens out."""

    def repo(self, cls=FakeRepository, **options) -> FakeRepository:
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        repo = cls(**options)
        repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        home = repo.root / "home"
        cfg = home / ".config" / "retroarch"
        cfg.mkdir(parents=True)
        repo.dest.mkdir()
        (cfg / "retroarch.cfg").write_text(f'system_directory = "{repo.dest}"\n')
        self.env = {**repo.env(), "HOME": str(home), "TERM": "xterm",
                    "RETROBIOS_OS": "linux", "XDG_CONFIG_HOME": str(home / ".config")}
        self.env.pop("NO_COLOR", None)
        # Whatever the shell running the tests has, the input is not known:
        # the bar names both the button and the key. Each test names its own.
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "SSH_CONNECTION", "SSH_TTY",
                     install.TUI_PAD_ENV, *install._STEAM_MARKERS):
            self.env.pop(name, None)
        return repo

    def start(self, *args: str, cols: int = 80, rows: int = 24) -> Pty:
        term = Pty(list(args), self.env, cols, rows)
        self.addCleanup(lambda: term.status is None and os.kill(term.pid, 9))
        return term


    def card_repo(self, system: str) -> tuple[Path, Path]:
        from test_install_app import AppRepository, make_card  # noqa: PLC0415

        repo = self.repo(AppRepository)
        root = repo.root / "card"
        return root, make_card(root, system)

    def assertAppOnCard(self, root: Path, system: str) -> None:
        index = json.loads((REPO_ROOT / "install" / "launchers" / "index.json").read_text())
        targets = install.app_targets(index, install.APP_PLACES[system])
        for target in targets:
            data = (root / target["dest"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), target["sha256"],
                             target["dest"])

    def test_put_the_app_on_a_muos_card_from_the_result(self):
        root, bios = self.card_repo("muOS")
        term = self.start("--platform", "retroarch", "--dest", str(root))
        term.wait("files to download")
        term.send("\r")
        screen = term.wait(install.APP_OFFER)
        self.assertIn("> Exit", screen)
        term.send(DOWN, "\r")
        screen = term.wait("RetroBIOS is on the muOS card")
        self.assertIn("Archive Manager", screen)
        term.send("\r")
        self.assertEqual(term.close(), 0)
        self.assertAppOnCard(root, "muOS")
        self.assertEqual(len(list(bios.iterdir())), 5)
        self.assertIn("RetroBIOS is on the muOS card", term.after())

    def test_put_the_app_on_a_knulli_card_from_the_options(self):
        root, _bios = self.card_repo("KNULLI")
        term = self.start("--platform", "knulli", "--dest", str(root))
        term.wait("files to download")
        term.send("o")
        term.wait("Each row is an option")
        for _step in range(20):
            if "> Put RetroBIOS on this card: no" in term.settle():
                break
            term.send(DOWN)
        term.send("\r")
        term.wait("> Put RetroBIOS on this card: yes")
        term.send("\x1b")
        term.wait("files to download")
        term.send("\r")
        screen = term.wait("RetroBIOS is on the KNULLI card")
        self.assertIn("start RetroBIOS from Ports", screen)
        self.assertNotIn(install.APP_OFFER, screen)
        term.send("\r")
        self.assertEqual(term.close(), 0)
        self.assertAppOnCard(root, "KNULLI")
        self.assertIn("--add-app", term.after())

    def test_three_presses_install_everything(self):
        repo = self.repo()
        term = self.start()
        screen = term.wait("Where do you play?")
        self.assertIn("> RetroArch", screen)
        term.send("\r")
        screen = term.wait("files to download")
        self.assertIn("5 files to download, 30 B", screen)
        self.assertIn("> Install", screen)
        term.send("\r")
        term.wait("5 files installed.")
        term.send("\r")
        self.assertEqual(term.close(), 0)
        self.assertEqual(len(repo.installed()), 5)
        after = term.after()
        self.assertIn("RetroBIOS: Done", after)
        self.assertIn("5 files installed.", after)
        self.assertIn("--platform retroarch --dest", after)
        # The cursor and the main screen come back before the result is printed.
        self.assertIn(b"\x1b[?25h\x1b[?1049l", term.raw)

    def test_the_save_directory_is_the_one_line_mode_finds(self):
        """The interface named the BIOS folder with --dest, so its runs never
        read retroarch.cfg: keys.txt went to a guessed RetroArch/saves, or
        was dropped without a word, where line mode writes it to the
        savefile_directory RetroArch reads."""
        import subprocess

        def written(repo, saves: Path) -> list[str]:
            return sorted([*repo.installed(), *(
                "saves/" + p.relative_to(saves).as_posix()
                for p in saves.rglob("*") if p.is_file())])

        results = []
        for mode in ("line", "interface"):
            with self.subTest(mode=mode):
                repo = self.repo(SaveDirectoryRepository)
                home = Path(self.env["HOME"])
                saves = home / "MySaves"
                saves.mkdir()
                cfg = home / ".config" / "retroarch" / "retroarch.cfg"
                cfg.write_text(f'system_directory = "{repo.dest}"\n'
                               f'savefile_directory = "{saves}"\n')
                if mode == "line":
                    proc = subprocess.run(
                        [sys.executable, str(REPO_ROOT / "install.py"), "--platform",
                         "retroarch", "--yes"], env=self.env, capture_output=True,
                        text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertIn("6 files installed", proc.stdout)
                else:
                    term = self.start()
                    term.wait("Where do you play?")
                    term.send("\r")
                    screen = term.wait("files to download")
                    self.assertIn("6 files to download", screen)
                    # The second folder the run writes to, named before
                    # anything is written, and again on the result.
                    self.assertIn("1 file read from RetroArch's save directory goes to:\n"
                                  " ~/MySaves", screen)
                    term.send("\r")
                    screen = term.wait("6 files installed.")
                    self.assertIn("save directory goes to:\n ~/MySaves", screen)
                    term.send("\r")
                    self.assertEqual(term.close(), 0)
                results.append(written(repo, saves))
                self.assertIn("saves/Azahar/Azahar/sysdata/keys.txt", results[-1])
        self.assertEqual(results[0], results[1])

    def test_one_system_from_the_checklist(self):
        repo = self.repo()
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest), cols=40, rows=15)
        term.wait("> Install")
        term.send(DOWN, "\r")
        term.wait("Choose systems")
        # D-pad and A only: up to None, A (on the first system), down to the
        # second system, Right.
        term.send(UP, UP, "\r", DOWN, RIGHT)
        screen = term.wait("1 of 2 systems")
        self.assertIn("> [x] sony-playstation", screen)
        term.send("\r")
        screen = term.wait("> Install")
        self.assertIn("3 files to download", screen)
        term.send("\r")
        term.wait("3 files installed.")
        term.send("\r")
        self.assertEqual(term.close(), 0)
        self.assertEqual(repo.installed(), ["pcsx/extra.bin", "scph5500.bin", "scph5501.bin"])
        self.assertIn("--system sony-playstation", term.after().replace("\n", " "))

    def test_options_region_and_game_data(self):
        repo = self.repo(GameDataRepository)
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest))
        term.wait("6 files to download")
        term.send("o")
        screen = term.wait("Each row is an option")
        self.assertIn("--no-game-data", screen)
        term.send("\r")
        term.wait("Preferred regions")
        term.send(DOWN, "\r", "\x1b")
        screen = term.wait("Preferred regions: North America")
        term.send(DOWN, RIGHT, "\x1b")
        # Back on the plan, the check runs again under the new options.
        screen = term.wait("4 files to download")
        self.assertIn("> Install", screen)
        term.send("\r")
        screen = term.wait("installed.")
        self.assertIn("Left out as game data: 1 file.", screen)
        term.send("\r")
        self.assertEqual(term.close(), 0)
        self.assertEqual(repo.installed(), ["gba_bios.bin", "mgba/extra.bin",
                                            "pcsx/extra.bin", "scph5501.bin"])

    def test_preview_writes_nothing(self):
        repo = self.repo()
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest))
        term.wait("> Install")
        term.send("o", "G", UP, "\r")
        screen = term.wait("Nothing is written.")
        self.assertIn("missing", screen)
        self.assertIn("scph5501.bin", screen)
        term.send("\x1b", "\x1b", "q")
        self.assertEqual(term.close(), 130)
        self.assertEqual(repo.installed(), [])
        self.assertIn("Nothing was installed.", term.after())

    def test_ctrl_c_during_the_download(self):
        repo = self.repo(GameDataRepository, slow=True)
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest), "--jobs", "1")
        term.wait("> Install")
        term.send("\r")
        term.wait("1 of 6 files")
        term.send("\x03")
        screen = term.wait("Stopped:")
        term.send("\r")
        self.assertEqual(term.close(), 130)
        # The count said is the count on disk: "6 of 6" after a stop at 1
        # passed a regex and was false.
        on_disk = len(repo.installed())
        self.assertLess(on_disk, 6)
        self.assertIn(f"Stopped: {on_disk} of 6 installed, run again to finish.", screen)
        self.assertIn(f"Stopped: {on_disk} of 6 installed", term.after())

    def test_sigterm_during_the_download_is_a_stop(self):
        """Start+Select on a handheld is gptokeyb's pkill: SIGTERM, SIGKILL 3 s on."""
        import signal

        repo = self.repo(GameDataRepository, slow=True)
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest), "--jobs", "1")
        term.wait("> Install")
        term.send("\r")
        term.wait("1 of 6 files")
        started = time.monotonic()
        os.kill(term.pid, signal.SIGTERM)
        code = term.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(code, 130)
        on_disk = len(repo.installed())
        self.assertGreaterEqual(on_disk, 1)
        after = term.after()
        self.assertIn(f"Stopped: {on_disk} of 6 installed, run again to finish.", after)
        self.assertNotIn("Nothing was installed", after)
        # The plain outcome only: the log's last lines are for a run that died.
        self.assertIsNone(re.search(r"\[\d+/6\]", after), after)
        self.assertNotIn("Stopping.", after)
        time.sleep(1)
        self.assertEqual(len(repo.installed()), on_disk)

    def test_copy_from_an_old_card(self):
        repo = self.repo()
        old = Path(self.env["HOME"]) / "oldcard"
        (old / "bios").mkdir(parents=True)
        for name in ("scph5500.bin", "scph5501.bin", "gba_bios.bin"):
            payload = FakeRepository.PAYLOADS[name][0]
            (old / "bios" / name).write_bytes(payload)
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest))
        term.wait("5 files to download")
        term.send(DOWN, DOWN, "\r")
        term.wait("Choose the old card")
        for _ in range(12):
            if "> oldcard/" in term.settle():
                break
            term.send(DOWN)
        self.assertIn("> oldcard/", term.settle())
        term.send("\r")
        term.wait("> Use this folder")
        term.send("\r")
        screen = term.wait("found in")
        # The folder wraps at a slash: compared without the spaces.
        self.assertIn("3 found in oldcard, 2 to download (8 B)", screen)
        # Checked again, the plan opens on Install.
        term.send("\r")
        screen = term.wait("files installed.")
        # Where each came from, as the line mode says it.
        self.assertIn("5 files installed.", screen)
        self.assertIn("3 files copied from", screen)
        self.assertIn("2 downloaded.", screen)
        term.send("\r")
        self.assertEqual(term.close(), 0)
        self.assertEqual(len(repo.installed()), 5)
        self.assertIn(f"--from {old}", term.after().replace("\n", " "))

    def test_ctrl_c_on_the_first_screen(self):
        self.repo()
        term = self.start()
        term.wait("Where do you play?")
        term.send("\x03")
        self.assertEqual(term.close(), 130)
        self.assertIn(b"\x1b[?2004l\x1b[0m\x1b[?25h\x1b[?1049l", term.raw)

    def test_a_stop_from_outside_gives_the_terminal_back(self):
        import signal

        self.repo()
        term = self.start()
        term.wait("Where do you play?")
        mark = len(term.raw)
        os.kill(term.pid, signal.SIGTSTP)
        end = time.monotonic() + 10
        stopped = False
        while time.monotonic() < end and not stopped:
            term._pump(0.05)
            pid, status = os.waitpid(term.pid, os.WNOHANG | os.WUNTRACED)
            stopped = bool(pid) and os.WIFSTOPPED(status)
        self.assertTrue(stopped)
        self.assertIn(b"\x1b[?25h\x1b[?1049l", term.raw[mark:])
        mark = len(term.raw)
        os.kill(term.pid, signal.SIGCONT)
        term.wait("A/Enter Continue")
        end = time.monotonic() + 5
        while b"\x1b[?1049h" not in term.raw[mark:] and time.monotonic() < end:
            term._pump(0.05)
        self.assertIn(b"\x1b[?1049h", term.raw[mark:])
        term.send("q")
        self.assertEqual(term.close(), 130)

    def test_closing_the_window_ends_with_its_signal(self):
        """The terminal gone, the farewell has nowhere to go: the code is
        129 (SIGHUP), not 120 from the flush failing at exit."""
        self.repo()
        for screen_name, keys_first in (("Where do you play?", ()),
                                        ("files to download", ("\r",))):
            with self.subTest(screen=screen_name):
                term = self.start()
                term.wait("Where do you play?")
                term.send(*keys_first)
                term.wait(screen_name)
                os.close(term.fd)
                end = time.monotonic() + 20
                status = None
                while time.monotonic() < end:
                    pid, status = os.waitpid(term.pid, os.WNOHANG)
                    if pid:
                        break
                    time.sleep(0.05)
                else:
                    os.kill(term.pid, 9)
                    self.fail("still running once its window closed")
                term.status = os.WEXITSTATUS(status)
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(term.status, 129)

    def test_closing_the_window_while_it_installs(self):
        """The changing screen fails to draw on the closed window: that is
        the window's signal, not a crash with a log kept."""
        repo = self.repo(GameDataRepository, slow=True)
        scratch = repo.root / "scratch"
        scratch.mkdir()
        self.env["TMPDIR"] = str(scratch)
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest), "--jobs", "1")
        term.wait("> Install")
        term.send("\r")
        term.wait("1 of 6 files")
        os.close(term.fd)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            pid, status = os.waitpid(term.pid, os.WNOHANG)
            if pid:
                break
            time.sleep(0.05)
        else:
            os.kill(term.pid, 9)
            self.fail("still running once its window closed")
        term.status = os.WEXITSTATUS(status)
        self.assertEqual(term.status, 129)
        self.assertEqual(list(scratch.glob("retrobios-tui-*/error.log")), [])

    def test_no_key_on_the_first_screen_closes_it(self):
        """A mapper that never opens the pad left the first screen up for
        good: a handheld then has no way out but the power button."""
        self.repo()
        self.env[install.TUI_IDLE_ENV] = "1"
        term = self.start()
        term.wait("Where do you play?")
        self.assertEqual(term.close(), 130)
        self.assertIn("No button pressed, closing.", term.after())
        # A key pressed, the screen stays: the person is there.
        term = self.start()
        term.wait("Where do you play?")
        term.send(DOWN)
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            term._pump(0.05)
        self.assertEqual(os.waitpid(term.pid, os.WNOHANG), (0, 0))
        term.send("q")
        self.assertEqual(term.close(), 130)

    def test_no_key_on_a_screen_reached_by_itself_closes_it(self):
        """Given --platform, as the offline launcher route is, the check
        runs at once and the plan follows without a key: the close counted
        only on the first screen, and never came."""
        repo = self.repo()
        self.env[install.TUI_IDLE_ENV] = "1"
        term = self.start("--platform", "retroarch", "--dest", str(repo.dest))
        term.wait("> Install")
        self.assertEqual(term.close(), 130)
        self.assertIn("No button pressed, closing.", term.after())
        self.assertEqual(repo.installed(), [])

    def test_a_desktop_names_its_keys_and_a_pad_its_buttons(self):
        self.repo()
        for env, bar in (({"DISPLAY": ":0"}, "Enter Continue  Esc Quit  ? Help"),
                         ({"RETROBIOS_PAD": "gptokeyb", "DISPLAY": ":0"},
                          "A Continue  B Quit"),
                         ({"RETROBIOS_PAD": "muxterm"}, "Start Continue  B Quit")):
            with self.subTest(env=env):
                self.env.update(env)
                term = self.start(cols=40, rows=12)
                term.wait("Where do you play?")
                screen = term.wait(bar)
                self.assertEqual(screen.splitlines()[-1].strip(), bar)
                term.send("q")
                self.assertEqual(term.close(), 130)
                for name in env:
                    self.env.pop(name)

    def test_resize_redraws(self):
        self.repo()
        term = self.start(cols=40, rows=15)
        term.wait("Where do you play?")
        term.resize(100, 30)
        term.send(DOWN)
        screen = term.wait("> Another setup or folder")
        self.assertIn("B/Esc Quit", screen.splitlines()[29])
        term.resize(30, 10)
        term.wait("40x12 or more, now 30x10")
        term.resize(40, 12)
        term.wait("Where do you play?")
        term.send("q")
        self.assertEqual(term.close(), 130)

    def test_plain_mode_keeps_the_line_questions(self):
        self.repo()
        for env in ({"RETROBIOS_PLAIN": "1"}, {"ACCESSIBLE": "1"}):
            with self.subTest(env=env):
                self.env.update(env)
                term = self.start()
                # The setup found is asked as the screen asks it.
                term.wait("Enter for RetroArch")
                term.send("\r")
                term.wait("Enter) install everything")
                term.send("q\r")
                term.close()
                self.assertNotIn(b"\x1b[?1049h", term.raw)
                for name in env:
                    self.env.pop(name)

    def test_dumb_terminal_writes_no_escape(self):
        """TERM unset printed the banner bold and dim, and redrew the
        progress with CSI, where the screen itself read it as dumb."""
        self.repo()
        for value in ("dumb", None):
            with self.subTest(term=value):
                if value is None:
                    self.env.pop("TERM", None)
                else:
                    self.env["TERM"] = value
                term = self.start()
                term.wait("Enter for RetroArch")
                term.send("\r")
                term.wait("Enter) install everything")
                term.send("\r")
                term.wait("installed")
                term.close()
                self.assertNotIn(b"\x1b", term.raw)

    def test_a_terminal_that_cannot_draw_the_screen_gets_the_questions(self):
        """TERM=vt52 moves the cursor with ESC Y: the screen opened and drew
        CSI sequences it printed as text."""
        self.repo()
        self.env["TERMINFO"] = str(_terminfo_folder(self))
        for name in ("rbtest-glass", "rbtest-vt52"):
            with self.subTest(term=name):
                self.env["TERM"] = name
                term = self.start()
                term.wait("Enter for RetroArch")
                term.send("\r")
                term.wait("Enter) install everything")
                term.send("q\r")
                term.close()
                self.assertNotIn(b"\x1b[?1049h", term.raw)
                term = self.start("--tui")
                self.assertEqual(term.close(), 2)
                said = term.raw.decode()
                self.assertIn(f"--tui cannot open the full-screen interface here: TERM={name}",
                              said)
                self.assertNotIn("\x1b", said)

    def test_no_color_draws_no_colour(self):
        """The screen marks the row in focus with > as well as reverse video,
        and draws no colour at all: NO_COLOR has nothing left to remove."""
        self.repo()
        self.env["NO_COLOR"] = "1"
        term = self.start()
        screen = term.wait("Where do you play?")
        self.assertIn("> RetroArch", screen)
        term.send("q")
        self.assertEqual(term.close(), 130)
        for params in re.findall(rb"\x1b\[([0-9;]*)m", term.raw):
            codes = {int(code) for code in params.split(b";") if code}
            self.assertFalse(codes & {*range(30, 50), *range(90, 108)}, params)


def _terminfo_entry(name: str, cup: str | None, extended: bool = False) -> bytes:
    """A compiled term(5) entry holding the strings up to cursor_address,
    only that one set."""
    def short(value: int) -> bytes:
        return value.to_bytes(2, "little", signed=True)

    names = name.encode() + b"|test terminal\0"
    table = (cup.encode("latin-1") + b"\0") if cup is not None else b""
    offsets = [-1] * (install._TERMINFO_CUP + 1)
    if cup is not None:
        offsets[install._TERMINFO_CUP] = 0
    body = names + (b"\0" if len(names) % 2 else b"")
    # One number, columns, in the width of the format.
    body += (80).to_bytes(4 if extended else 2, "little")
    body += b"".join(short(offset) for offset in offsets) + table
    header = b"".join(short(value) for value in (
        0o1036 if extended else 0o432, len(names), 0, 1, len(offsets), len(table)))
    return header + body


def _terminfo_folder(test: unittest.TestCase) -> Path:
    """A $TERMINFO with a glass terminal, a VT52 and two ANSI terminals,
    one in the extended format."""
    (REPO_ROOT / "tmp").mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
    test.addCleanup(shutil.rmtree, folder, True)
    for name, cup, extended in (
            ("rbtest-glass", None, False),
            ("rbtest-vt52", "\x1bY%p1%' '%+%c%p2%' '%+%c", False),
            ("rbtest-ansi", "\x1b[%i%p1%d;%p2%dH", False),
            ("rbtest-ansi2", "\x1b[%i%p1%d;%p2%dH", True)):
        (folder / "r").mkdir(exist_ok=True)
        (folder / "r" / name).write_bytes(_terminfo_entry(name, cup, extended))
    return folder


class TerminalsTheScreenCannotDraw(unittest.TestCase):
    """The screen writes CSI sequences: a terminal whose description moves
    the cursor otherwise, or not at all, gets the line questions."""

    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.tty = StartConditions._Tty(self.slave)
        self.env = {"TERMINFO": str(_terminfo_folder(self))}

    def blocker(self, term: str) -> str:
        return install.tui_blocker(_args(), self.tty, self.tty, {**self.env, "TERM": term})

    def test_the_cursor_address_is_read(self):
        self.assertEqual(install.terminal_cursor_address("rbtest-ansi", self.env),
                         "\x1b[%i%p1%d;%p2%dH")
        self.assertEqual(install.terminal_cursor_address("rbtest-ansi2", self.env),
                         "\x1b[%i%p1%d;%p2%dH")
        self.assertEqual(install.terminal_cursor_address("rbtest-glass", self.env), "")
        self.assertIsNone(install.terminal_cursor_address("rbtest-none", self.env))

    def test_ansi_opens_and_the_others_say_why(self):
        self.assertEqual(self.blocker("rbtest-ansi"), "")
        self.assertEqual(self.blocker("rbtest-ansi2"), "")
        self.assertEqual(self.blocker("rbtest-glass"),
                         "TERM=rbtest-glass cannot move the cursor")
        self.assertIn("escape sequences", self.blocker("rbtest-vt52"))

    def test_no_description_is_no_refusal(self):
        """Android and most handheld images carry no terminfo: the screen
        draws there as before."""
        self.assertEqual(self.blocker("rbtest-none"), "")
        self.assertEqual(self.blocker("../r/rbtest-glass"), "")

    def test_the_folders_are_searched_in_order(self):
        home = Path(self.env["TERMINFO"])
        (home / ".terminfo" / "r").mkdir(parents=True)
        (home / ".terminfo" / "r" / "rbtest-ansi").write_bytes(
            _terminfo_entry("rbtest-ansi", None))
        # $TERMINFO first, then ~/.terminfo, then $TERMINFO_DIRS.
        self.assertTrue(install.terminal_cursor_address(
            "rbtest-ansi", {**self.env, "HOME": str(home)}))
        self.assertEqual(install.terminal_cursor_address(
            "rbtest-ansi", {"HOME": str(home), "TERMINFO_DIRS": self.env["TERMINFO"]}), "")
        # macOS files a terminal under the hex of its first letter.
        (home / "72").mkdir()
        (home / "72" / "rbtest-hex").write_bytes(_terminfo_entry("rbtest-hex", None))
        self.assertEqual(install.terminal_cursor_address(
            "rbtest-hex", {"TERMINFO_DIRS": str(home)}), "")

    def test_the_line_questions_draw_no_escape_there_either(self):
        """The progress line redraws with CSI and paints with SGR: on a VT52
        both print as text, as on TERM=dumb."""
        for term, live in (("rbtest-ansi", True), ("rbtest-vt52", False),
                           ("rbtest-glass", False)):
            with self.subTest(term=term), mock.patch.dict(
                    os.environ, {**self.env, "TERM": term}), \
                    mock.patch.object(install, "_enable_ansi", return_value=True):
                os.environ.pop("NO_COLOR", None)
                shown = install.Terminal(self.tty)
                self.assertEqual((shown.live, shown.colour), (live, live))
                self.assertEqual(shown.ok("ok") == "ok", not live)

    def test_an_unset_term_is_a_dumb_one(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": ""}), \
                mock.patch.object(install, "_enable_ansi", return_value=True):
            for name in ("TERM", "NO_COLOR"):
                os.environ.pop(name, None)
            shown = install.Terminal(self.tty)
            self.assertEqual((shown.live, shown.colour), (False, False))
            with mock.patch.object(install.os, "name", "nt"):
                self.assertTrue(install.Terminal(self.tty).live)

    def test_a_damaged_description_is_no_description(self):
        for data in (b"", b"\x1a\x01", b"\x00" * 12, _terminfo_entry("x", "\x1b[H")[:-1]):
            with self.subTest(data=data):
                self.assertIsNone(install.terminfo_string(data, install._TERMINFO_CUP))


@unittest.skipUnless(pyte and pty and hasattr(pty, "fork"),
                     "screen tests need pyte (pip install pyte==0.8.2) and a pty")
class TheTerminalComesBack(unittest.TestCase):
    """However the screen ends or pauses, the terminal is handed back as it
    was lent: the cursor shown, the main screen, echo, lines and signals."""

    repo = EndToEnd.repo
    start = EndToEnd.start

    # What a new pseudo-terminal has, which the child started on.
    @staticmethod
    def fresh_modes() -> list:
        master, slave = os.openpty()
        try:
            return termios.tcgetattr(slave)
        finally:
            os.close(master)
            os.close(slave)

    def assertGivenBack(self, term: Pty, since: int = 0) -> None:
        raw = term.raw[since:]
        self.assertGreater(raw.rfind(b"\x1b[?1049l"), raw.rfind(b"\x1b[?1049h"), raw[-200:])
        self.assertGreater(raw.rfind(b"\x1b[?25h"), raw.rfind(b"\x1b[?25l"), raw[-200:])

    def assertModesBack(self, modes: list) -> None:
        fresh = self.fresh_modes()
        for index, flag in ((3, termios.ECHO), (3, termios.ICANON), (3, termios.ISIG),
                            (3, termios.IEXTEN), (0, termios.ICRNL), (0, termios.IXON)):
            self.assertEqual(modes[index] & flag, fresh[index] & flag, (index, flag))
        self.assertEqual(modes, fresh)

    def ended(self, how) -> Pty:
        self.repo()
        term = self.start()
        term.wait("Where do you play?")
        how(term)
        return term

    def test_ctrl_c(self):
        term = self.ended(lambda term: term.send("\x03"))
        self.assertEqual(term.close(), 130)
        self.assertGivenBack(term)
        self.assertModesBack(term.modes)

    def test_eof_on_stdin(self):
        """Ctrl-D, the terminal's end of input, typed at the screen."""
        term = self.ended(lambda term: term.send("\x04"))
        self.assertEqual(term.close(), 130)
        self.assertGivenBack(term)
        self.assertModesBack(term.modes)

    def test_sigterm(self):
        import signal

        term = self.ended(lambda term: os.kill(term.pid, signal.SIGTERM))
        self.assertEqual(term.close(), 128 + signal.SIGTERM)
        self.assertGivenBack(term)
        self.assertModesBack(term.modes)

    def test_sighup(self):
        """The hangup without the window gone (kill -HUP, an ssh session
        dropped while its pty lingers)."""
        import signal

        term = self.ended(lambda term: os.kill(term.pid, signal.SIGHUP))
        self.assertEqual(term.close(), 128 + signal.SIGHUP)
        self.assertGivenBack(term)
        self.assertModesBack(term.modes)

    def shell(self) -> Pty:
        """An interactive bash on a pseudo-terminal, a shell with job control
        as a person's terminal runs one."""
        self.repo()
        env = {**self.env, "PS1": "PROMPT$ ",
               "PATH": f"{Path(sys.executable).parent}{os.pathsep}{self.env.get('PATH', '')}"}
        term = Pty(["--norc", "--noprofile", "-i"], env, program=["/bin/bash"])
        self.addCleanup(lambda: term.status is None and os.killpg(term.pid, 9))
        term.wait("PROMPT$")
        return term

    def typed(self, term: Pty, line: str) -> None:
        os.write(term.fd, line.encode() + b"\r")

    @unittest.skipUnless(Path("/bin/bash").exists(), "a shell with job control")
    def test_ctrl_z_then_fg(self):
        """ISIG is off on the screen, so Ctrl-Z reached it as a byte and did
        nothing: the shell never got its terminal back. From a shell with
        job control it stops, and fg brings the screen back."""
        term = self.shell()
        self.typed(term, f"{shlex.quote(sys.executable)} {REPO_ROOT / 'install.py'} --tui")
        term.wait("Where do you play?")
        mark = len(term.raw)
        term.send("\x1a")
        end = time.monotonic() + 10
        while b"PROMPT$" not in term.raw[mark:] and time.monotonic() < end:
            term._pump(0.05)
        self.assertIn(b"Stopped", term.raw[mark:], term.raw[mark:][-300:])
        self.assertGivenBack(term, mark)
        # fg: the shell's SIGCONT, and the screen is back, raw again.
        mark = len(term.raw)
        self.typed(term, "fg")
        end = time.monotonic() + 5
        while b"\x1b[?1049h" not in term.raw[mark:] and time.monotonic() < end:
            term._pump(0.05)
        self.assertIn(b"\x1b[?1049h", term.raw[mark:])
        term.wait("Where do you play?")
        self.assertFalse(termios.tcgetattr(term.fd)[3] & termios.ECHO)
        term.send("q")
        term.wait("PROMPT$")
        self.typed(term, "exit")
        self.assertEqual(term.close(), 130)
        self.assertGivenBack(term)

    def test_ctrl_z_with_no_shell_to_resume_it_is_ignored(self):
        """code_audit_18 P3-1: started straight on a terminal (a terminal
        app's -e, a desktop entry) or under a launcher's sh -c, nothing ever
        resumed the screen a Ctrl-Z stopped. It now goes on, and says why."""
        self.repo()
        for program in (None, ["/bin/sh", "-c", f"{shlex.quote(sys.executable)} "
                                                f"{REPO_ROOT / 'install.py'} \"$@\"", "sh"]):
            with self.subTest(program=program):
                term = Pty(["--tui"], self.env, program=program)
                self.addCleanup(lambda term=term: term.status is None
                                and os.killpg(term.pid, 9))
                term.wait("Where do you play?")
                term.send("\x1a")
                screen = term.wait("Ctrl-Z is ignored")
                self.assertIn("no shell started this installer", " ".join(screen.split()))
                pid, status = os.waitpid(term.pid, os.WNOHANG | os.WUNTRACED)
                self.assertEqual(pid, 0, "stopped")
                term.send("q")
                self.assertEqual(term.close(), 130)
                self.assertGivenBack(term)

    def test_ctrl_z_needs_a_shell_that_can_resume(self):
        state = install.TuiState(os_type="linux", home=HOME)
        with mock.patch.object(install.os, "getpgrp", return_value=10), \
                mock.patch.object(install.os, "getsid", return_value=10):
            self.assertFalse(install.tui_ctrl_z(state))
        self.assertEqual(state.status, install.CTRL_Z_NO_SHELL)
        state.status = ""
        with mock.patch.object(install.os, "getpgrp", return_value=12), \
                mock.patch.object(install.os, "getsid", return_value=10):
            self.assertTrue(install.tui_ctrl_z(state))
        self.assertEqual(state.status, "")

    @staticmethod
    def proc_state(pid: int) -> str:
        """The state letter Linux gives a process (T is stopped)."""
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rindex(")") + 2]

    @staticmethod
    def children(pid: int) -> list[int]:
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                text = (entry / "stat").read_text()
            except OSError:
                continue
            if int(text[text.rindex(")") + 2:].split()[1]) == pid:
                found.append(int(entry.name))
        return found

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "reads process states in /proc")
    def test_ctrl_z_through_install_sh_stops_the_wrapper_and_the_install(self):
        """code_audit_17 P2-1: Ctrl-Z stopped the installer alone. install.sh,
        which runs it as a child, kept waiting, the shell never got its prompt
        back, and the install run behind the screen kept writing."""
        import signal

        repo = self.repo(GameDataRepository, slow=True)
        env = {**self.env, "PS1": "PROMPT$ ",
               "PATH": f"{Path(sys.executable).parent}{os.pathsep}{self.env['PATH']}"}
        # Typed at an interactive bash, as a person runs it: install.sh is a
        # job of a shell that can resume it (code_audit_18 P3-1).
        term = Pty(["--norc", "--noprofile", "-i"], env, program=["/bin/bash"])
        self.addCleanup(lambda: term.status is None and os.killpg(term.pid, 9))
        term.wait("PROMPT$")
        self.typed(term, f"sh {REPO_ROOT / 'install.sh'} --jobs 1")
        term.wait("Where do you play?")
        term.send("\r")
        term.wait("to download")
        term.send("\r")
        term.wait("Installing")
        time.sleep(0.8)
        wrapper = self.children(term.pid)
        self.assertEqual(len(wrapper), 1, wrapper)
        installer = self.children(wrapper[0])
        self.assertEqual(len(installer), 1, installer)
        runs = self.children(installer[0])
        self.assertTrue(runs, "no install run")
        mark = len(term.raw)
        term.send("\x1a")
        end = time.monotonic() + 10
        while b"PROMPT$" not in term.raw[mark:] and time.monotonic() < end:
            term._pump(0.05)
        # The wrapper is stopped: the shell reports its job stopped.
        self.assertIn(b"Stopped", term.raw[mark:], term.raw[mark:][-300:])
        self.assertEqual(self.proc_state(wrapper[0]), "T")
        self.assertEqual(self.proc_state(installer[0]), "T")
        self.assertEqual([self.proc_state(run) for run in runs], ["T"] * len(runs))
        written = sorted(repo.dest.rglob("*"))
        time.sleep(2)
        self.assertEqual(sorted(repo.dest.rglob("*")), written)
        # fg continues the job's group: the screen comes back and the install
        # carries on to its end.
        mark = len(term.raw)
        self.typed(term, "fg")
        end = time.monotonic() + 5
        while b"\x1b[?1049h" not in term.raw[mark:] and time.monotonic() < end:
            term._pump(0.05)
        self.assertIn(b"\x1b[?1049h", term.raw[mark:])
        term.wait("files installed", timeout=30)
        term.send("\r")
        term.wait("PROMPT$")
        self.typed(term, "exit")
        self.assertEqual(term.close(), 0)


class TheFolderScreenSaysWhatItOffers(unittest.TestCase):
    """The folder screen said "usually reads its BIOS files from" for any
    folder it offered: the folder named with --dest, reached by Change
    folder, and the built-in default while the retroarch.cfg found here
    names another. A closed folder found here opened on "No usual
    RetroArch folder here" under the setup that listed it."""

    def said(self, state: install.TuiState) -> str:
        return " ".join(" ".join(install.tui_view(state, 200, 40)).split())

    def test_change_folder_names_the_chosen_folder(self):
        state = checked_state()
        state.stack = ["setup", "plan"]
        mine = Path("/home/user/typed/mybios")
        state.dest = mine
        install._activate(state, install.TuiRow("folder", "Change folder"))
        self.assertEqual(state.screen, "folder")
        screen = self.said(state)
        self.assertNotIn("usually reads", screen)
        self.assertIn(f"The BIOS folder chosen for RetroArch: {mine}", screen)

    def test_another_setup_offers_the_folder_found_here(self):
        state = fresh_state()
        found = Path("/home/user/Games/bios")
        state.setups = [("retroarch", found, "RetroArch")]
        state.stack = ["setup", "platforms"]
        install._activate(state, install.TuiRow("platform", "RetroArch", value="retroarch"))
        self.assertEqual(state.folder_default, found)
        self.assertIn(f"RetroArch reads its BIOS files from: {found}", self.said(state))

    def test_with_none_found_the_usual_folder_is_offered(self):
        state = fresh_state()
        state.setups = []
        state.stack = ["setup", "platforms"]
        install._activate(state, install.TuiRow("platform", "RetroArch", value="retroarch"))
        self.assertIn("RetroArch usually reads its BIOS files from:", self.said(state))

    def test_a_closed_folder_found_here_asks_for_another(self):
        state = fresh_state()
        state.stack = ["setup"]
        with mock.patch.object(install, "closed_folder",
                               return_value=(Path("/home/user/lock"), "Permission denied")):
            install.tui_update(state, Key("enter"))
        self.assertEqual(state.screen, "folder")
        screen = self.said(state)
        self.assertNotIn("No usual RetroArch folder here", screen)
        self.assertIn("Choose another folder for RetroArch", screen)

    def test_a_refused_folder_puts_browse_under_a(self):
        for facts in ({"free": 10, "writable": True, "cores": []},
                      {"free": 50 * 1024 ** 3, "writable": False, "cores": []}):
            with self.subTest(facts=facts):
                state = checked_state(facts)
                self.assertEqual(install.tui_rows(state)[0].action, "folder")
                install.tui_update(state, Key("enter"))
                self.assertEqual(state.screen, "folder")
                rows = install.tui_rows(state)
                self.assertEqual(rows[state.focus["folder"]].action, "browse")


class ThePlanNumbersAddUp(unittest.TestCase):
    """"47 files to download" sat above "49 missing, 1 wrong copy": the 3
    found on this machine were the difference, left to the reader."""

    def test_the_copied_files_are_the_difference(self):
        state = checked_state()
        state.found = [found("scph5500.bin", str(SYSTEM_DIR), "one folder too deep")]
        screen = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn("3 files to download", screen)
        # The backup line under the count it explains, then the split
        # (tui_audit_8 P3-5).
        self.assertIn("1 in place, 3 missing, 1 wrong copy The 1 wrong copy is replaced, the "
                      "old one kept in a backup folder. 4 to install: 1 copied from this "
                      "machine, 3 downloaded", screen)


class GameDataIsNoFirmwareAlarm(unittest.TestCase):
    """Every RetroArch plan named jazz2/Source/Anims.j2a (jazz-jackrabbit-2)
    as the file needed to start an emulator: a game's data, under a raw id."""

    def test_game_data_has_its_own_line(self):
        state = checked_state()
        state.systems = []
        state.manifest = {**MANIFEST, "omitted_files": [
            {"dest": "jazz2/Source/Anims.j2a", "system": "jazz-jackrabbit-2",
             "required": True, "category": "game_data"},
            {"dest": "missing.bin", "system": "sony-playstation", "required": True}]}
        lines = install._unavailable_lines(state)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("needed to start its emulator: missing.bin", lines[0])
        self.assertNotIn("Anims.j2a", lines[0])
        # The folder it goes in, and what game data is (the Android run of
        # 2026-10-27 said neither).
        self.assertIn("Not available here, game data: files a game or a game engine reads",
                      lines[1])
        self.assertIn(f"into {SYSTEM_DIR / 'jazz2' / 'Source'}{os.sep}", lines[1])
        self.assertIn("Anims.j2a (Jazz Jackrabbit 2)", lines[1])


class TheHelpNamesKeysThatCome(unittest.TestCase):
    """Every route's help listed "?, F1  This help", which no pad sends, and
    a key as wide as the column touched its text."""

    def test_pad_routes_name_no_help_key(self):
        for pad in ROUTES:
            with self.subTest(route=pad):
                state = checked_state()
                state.pad = pad
                keys = [key for key, _what in install._help_lines(state)]
                self.assertEqual("?, F1" in keys, pad == "keys")

    def test_a_wide_key_keeps_two_spaces(self):
        state = checked_state()
        state.help = True
        with mock.patch.object(install, "_help_lines",
                               return_value=[("A, Start, Enter, Space, x", "Choose"),
                                             ("q", "Quit")]):
            lines = install._view_help(state, 80, 24)[1]
        self.assertTrue(any("A, Start, Enter, Space, x  Choose" in line for line in lines))


class CopyFromStartsInDownloads(unittest.TestCase):
    """A ZIP in Downloads took 18 presses from the first screen: the copy
    browser opened at HOME, under every mounted drive."""

    def test_a_zip_in_downloads_opens_there(self):
        home = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "Downloads").mkdir()
        state = checked_state()
        state.home = home
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._open_from_browser(state)
            self.assertEqual(state.browse_dir, home)
            state.stack.pop()
            (home / "Downloads" / "PSX BIOS.zip").write_bytes(b"")
            install._open_from_browser(state)
        self.assertEqual(state.browse_dir, home / "Downloads")


class NothingFoundIsSaid(unittest.TestCase):
    """With nothing detected the first screen was the list of 18 setups,
    with no line at 40x15 saying nothing was found, which the beginner's
    page names."""

    def test_at_every_size(self):
        with mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            state, _effects = install.tui_start(_args(), "linux")
        self.assertEqual(state.screen, "platforms")
        for cols, rows in ((40, 12), (40, 15), (80, 24)):
            with self.subTest(size=f"{cols}x{rows}"):
                screen = " ".join(" ".join(install.tui_view(state, cols, rows)).split())
                self.assertIn("Nothing was found here", screen)
        page = (REPO_ROOT / "wiki" / "first-install.md").read_text(encoding="utf-8")
        self.assertIn("Nothing was found here", page)


class AFocusesNoSystemFolder(unittest.TestCase):
    """At / A went into bin/, and at a drive root holding $RECYCLE.BIN into
    the recycle bin, which Use this folder then took as the BIOS folder."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state = install.TuiState(os_type="linux", home=self.home)
        self.state.platform = "retroarch"
        self.state.stack = ["folder", "browse"]

    def focused(self) -> install.TuiRow:
        return self.state.browse_rows[self.state.focus["browse"]]

    def test_the_recycle_bin_and_restore_points_are_passed_over(self):
        for name in ("$RECYCLE.BIN", "System Volume Information", "Games"):
            (self.home / name).mkdir()
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._browse_to(self.state, self.home)
        self.assertEqual(self.focused().label, "Games/")

    def test_explorer_hidden_folders_are_passed_over(self):
        for name in ("Boot", "Games"):
            (self.home / name).mkdir()
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            hidden = 0x2 | 0x4 if Path(path).name == "Boot" else 0
            return types.SimpleNamespace(st_file_attributes=hidden, st_mode=result.st_mode)

        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install.os, "stat", stat):
            install._browse_to(self.state, self.home)
        self.assertEqual(self.focused().label, "Games/")

    def test_at_the_root_no_system_folder_is_focused(self):
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._browse_to(self.state, Path("/"))
        row = self.focused()
        if row.action == "dir":
            self.assertNotIn(row.value.name, install._POSIX_ROOT_SYSTEM)
        names = {r.value.name for r in self.state.browse_rows if r.action == "dir"}
        if "bin" in names and names - install._POSIX_ROOT_SYSTEM - {""}:
            self.assertEqual(row.action, "dir")



def _card_from_rules(root: Path, setup: install.CardSetup) -> Path:
    """A card as setup's system leaves it, written from the rules that
    tell it: a test that names each marker by hand misses the next one."""
    root.mkdir(parents=True, exist_ok=True)
    if setup.plat in install.CARD_PLATFORMS:
        (root / ".system").mkdir(exist_ok=True)
        prefix = next(prefix for name, _marker, prefix in install._CARD_RULES
                      if name == setup.plat)
        (root / install.CARD_MARKER).write_text(f"{prefix}20250101\n", encoding="utf-8")
    for rule in setup.rules[:1]:
        if rule.method == "file_matches":
            path, pattern = rule.values
            text = re.sub(r"\\(.)", r"\1", pattern.strip("^$").replace(r"\s*", ""))
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text + "\n", encoding="utf-8")
            continue
        for value in rule.values:
            (root / value).parent.mkdir(parents=True, exist_ok=True)
            (root / value).write_text("", encoding="utf-8")
    return root


def _setup_folder(setup: install.CardSetup, card: Path) -> Path:
    return card / setup.below if setup.below else card


class EveryCardSetupGoesToItsCard(unittest.TestCase):
    """code_audit_13 P2-3, tui_audit_6 P1-1: the card setups were fixed one
    by one, and UnofficialOS, ArchR, ArkOS, MiyooCFW and the Anbernic
    firmware stayed on the RetroArch row, which listed them and opened this
    computer's folder. Each system that runs from its SD card, from every
    way in, goes to its card."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.setups = install.card_setups()

    def test_one_list_holds_every_card_system(self):
        names = {setup.name for setup in self.setups}
        self.assertLessEqual({rule.name for rule in install._card_rules()}, names)
        self.assertLessEqual(set(install._systems_reading()["retroarch"]), names)
        self.assertLessEqual({install.display_name(plat) for plat in install.CARD_PLATFORMS},
                             names)
        self.assertLessEqual({"Onion", "spruceOS", "muOS", "JELOS", "ROCKNIX", "KNULLI",
                              "ArkOS", "dArkOS", "UnofficialOS", "ArchR", "MiyooCFW",
                              "Anbernic stock firmware", "Allium", "Koriki", "CrossMix-OS",
                              "MinUI", "NextUI", "MyMinUI"}, names)
        self.assertEqual(len(names), len(self.setups))
        # A system the guide writes is one of them.
        try:
            import yaml
        except ImportError:
            return
        guides = yaml.safe_load((REPO_ROOT / "platforms" / "_guides.yml").read_text())
        for system in guides["handheld_guide"]["systems"]:
            with self.subTest(system=system["name"]):
                self.assertIn(system["name"], names)

    def test_its_folder_is_the_one_the_guide_names(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml is not installed")
        guides = yaml.safe_load((REPO_ROOT / "platforms" / "_guides.yml").read_text())
        below = {setup.name: setup.below for setup in self.setups}
        for system in guides["handheld_guide"]["systems"]:
            folder = re.search(r"`([^`]+)`", system.get("folder", ""))
            if folder:
                with self.subTest(system=system["name"]):
                    self.assertTrue(folder.group(1).endswith(below[system["name"]]),
                                    (folder.group(1), below[system["name"]]))

    def platforms(self, needle: str = "") -> install.TuiState:
        state = fresh_state()
        state.stack = ["setup", "platforms"]
        state.filters["platforms"] = needle
        return state

    def test_the_setup_list_has_a_row_for_each_without_the_filter(self):
        state = self.platforms()
        rows = install.tui_rows(state)
        self.assertEqual({row.value: row for row in rows if row.action == "platform"}[
            "retroarch"].right, "")
        for setup in self.setups:
            with self.subTest(system=setup.name):
                state = self.platforms()
                rows = install.tui_rows(state)
                index = next(i for i, row in enumerate(rows)
                             if row.label == f"{setup.name}, SD card")
                state.focus["platforms"] = index
                with mock.patch.object(install, "_mounted_cards", return_value=[]):
                    effects = press(state, "enter")
                    text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
                self.assertEqual((effects, state.screen), ([], "folder"))
                self.assertEqual(state.card_bound, (setup.name, setup.below))
                self.assertIsNone(state.folder_default)
                self.assertIn("Where is the SD card?", text)
                self.assertNotIn(str(SYSTEM_DIR), text)

    def test_its_name_in_the_filter_finds_its_row(self):
        for setup in self.setups:
            with self.subTest(system=setup.name):
                rows = install.tui_rows(self.platforms(setup.name))
                self.assertIn(f"{setup.name}, SD card", [row.label for row in rows])

    def test_platform_named_on_a_computer_asks_for_the_card(self):
        for setup in self.setups:
            with self.subTest(system=setup.name):
                plat = install.normalize_platform(setup.name, say=False)
                self.assertEqual(plat, setup.plat)
                self.assertEqual(install.card_bound(setup.name, plat),
                                 (setup.name, setup.below))
                with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                        contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit) as stop:
                    install.card_install_dest(setup.name, plat, interactive=False)
                self.assertEqual(stop.exception.code, 1)
                self.assertIn(f"{setup.name} keeps its BIOS files on its SD card",
                              err.getvalue())
                with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                        mock.patch.object(install, "detect_platforms", return_value=[
                            ("retroarch", SYSTEM_DIR)]), \
                        mock.patch.object(install, "detect_frontends", return_value=[]):
                    state, effects = install.tui_start(_args(platform=setup.name), "linux")
                self.assertEqual((effects, state.screen), ([], "folder"))
                self.assertEqual(state.card_bound, (setup.name, setup.below))
                self.assertIsNone(state.folder_default)

    def test_the_line_menu_offers_it_and_asks_for_its_card(self):
        offered = [name for name, _label in install._line_setups()]
        for setup in self.setups:
            with self.subTest(system=setup.name):
                key = setup.plat if setup.name == install.display_name(setup.plat) \
                    else setup.name
                self.assertIn(key, offered)
                card = _card_from_rules(self.root / f"menu{offered.index(key)}", setup)
                with mock.patch.object(install, "_choose_platform", return_value=key), \
                        mock.patch.object(install, "_mounted_cards", return_value=[]), \
                        mock.patch.object(install, "_ask", return_value=str(card)), \
                        contextlib.redirect_stdout(io.StringIO()):
                    chosen = install._prompt_manual_platform("linux", ask=False)
                self.assertEqual(chosen, [(setup.plat, _setup_folder(setup, card))])

    def test_its_card_gives_one_folder_whichever_way_it_is_named(self):
        for index, setup in enumerate(self.setups):
            with self.subTest(system=setup.name):
                card = _card_from_rules(self.root / f"card{index}", setup)
                # code_audit_14 P2-1: a card no file names gets its folder
                # below the root with --dest too, as typed.
                want = _setup_folder(setup, card)
                with contextlib.redirect_stdout(io.StringIO()):
                    by_dest = install.card_dest(setup.name, setup.plat, card)
                self.assertEqual(by_dest, (setup.plat, want))
                with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                        mock.patch.object(install, "detect_platforms", return_value=[]), \
                        mock.patch.object(install, "detect_frontends", return_value=[]):
                    state, _effects = install.tui_start(
                        _args(platform=setup.name, dest=str(card)), "linux")
                    self.assertEqual(state.dest, want)
                    # --dest alone, then the setup's row: the folder of the
                    # same --dest (code_audit_13 P2-2).
                    state, _effects = install.tui_start(_args(dest=str(card)), "linux")
                    if state.screen == "platforms":
                        rows = install.tui_rows(state)
                        label = (f"{setup.name}, SD card")
                        state.focus["platforms"] = [row.label for row in rows].index(label)
                        press(state, "enter")
                    self.assertEqual(state.dest, want)
                # Its card chosen as a card's root: its BIOS folder below it.
                if setup.known():
                    choice = install.card_choice(setup.name, setup.plat, card)
                    self.assertEqual((choice.refused, choice.folder),
                                     ("", _setup_folder(setup, card)))

    def test_another_systems_card_is_refused_for_each(self):
        """code_audit_13 P2-1, tui_audit_6 P2-5: KNULLI and ROCKNIX took a
        muOS or an Onion card, and wrote their layout into it."""
        cards = {}
        for name in ("muOS", "Onion", "MinUI"):
            setup = next(item for item in self.setups if item.name == name)
            cards[name] = _card_from_rules(self.root / name, setup)
        for setup in self.setups:
            for name, card in cards.items():
                if name == setup.name or (name == "MinUI"
                                          and setup.plat in install.CARD_PLATFORMS):
                    continue
                with self.subTest(system=setup.name, card=name):
                    choice = install.card_choice(setup.name, setup.plat, card)
                    self.assertIn(f"is {install._a(name)} card, not "
                                  f"{install._a(setup.name)} card", choice.refused)
                    self.assertTrue(install.card_choice(setup.name, setup.plat,
                                                        card).refused)
                    with mock.patch.object(install, "_mounted_cards",
                                           return_value=list(cards.values())):
                        self.assertNotIn(card, install.mounted_cards_of(setup.name,
                                                                        setup.plat))

    def test_a_blank_drive_is_refused_where_the_card_always_names_its_system(self):
        blank = self.root / "BLANK"
        blank.mkdir()
        for setup in self.setups:
            with self.subTest(system=setup.name):
                choice = install.card_choice(setup.name, setup.plat, blank)
                if setup.known() and not setup.fresh:
                    self.assertIn(f"is not {install._a(setup.name)} SD card", choice.refused)
                else:
                    self.assertEqual((choice.refused, choice.folder),
                                     ("", _setup_folder(setup, blank)))
                    self.assertTrue(choice.note)


class AuditSix(unittest.TestCase):
    """tui_audit_6 and code_audit_13: what remains of each finding."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def onion(self, name: str = "ONION") -> Path:
        card = self.root / name
        make_card(card, "Onion")
        return card

    def test_continue_never_installs_without_confirm(self):
        """P2-1: A on a system, the ticks unchanged, installed at once,
        offline too where the plan had no Install."""
        state = checked_state()
        press(state, "down", "enter")
        self.assertEqual(state.screen, "systems")
        state.focus["systems"] = 3
        self.assertEqual(install._primary(state), "A/Enter Continue")
        self.assertEqual(press(state, "enter"), [("run", "confirm")])
        self.assertEqual(state.screen, "confirm")
        # The confirm run's plan is the same: it still waits for A.
        install.tui_receive(state, ("event", state.run_id, dict(CHECK)))
        self.assertEqual(install.tui_receive(state, ("ended", state.run_id, 0, [])), [])
        self.assertEqual(state.screen, "confirm")

    def test_offline_with_nothing_on_hand_runs_nothing_from_the_list(self):
        state = checked_state()
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            state.focus["plan"] = [row.action for row in install.tui_rows(state)].index(
                "systems")
            press(state, "enter")
            state.focus["systems"] = 3
            effects = press(state, "enter")
            self.assertEqual(effects, [("run", "confirm")])
            install.tui_receive(state, ("event", state.run_id, dict(CHECK)))
            for event in FILES:
                install.tui_receive(state, ("event", state.run_id, dict(event)))
            install.tui_receive(state, ("ended", state.run_id, 0, []))
            self.assertEqual([row.action for row in install.tui_rows(state)], ["back"])
            view = install.tui_view(state, 40, 12)
            text = " ".join(" ".join(view).split())
        self.assertEqual(state.screen, "confirm")
        # code_audit_13 P3-3: Back alone, no word of an Install.
        self.assertNotIn("before Install", text)
        self.assertNotIn("backup folder", text)
        self.assertIn("Copy them from your old card", text)
        self.assertEqual(view[-1].strip(), "A/Enter Back  ? Help")

    def test_a_refusal_shows_at_40x12_on_type_a_path(self):
        """P2-2: Enter seemed to do nothing, the refusal drawn below the
        last line."""
        blank = self.root / "USBSTICK"
        blank.mkdir()
        state = fresh_state()
        state.stack = ["setup", "folder", "path"]
        state.platform, state.card_bound = "retroarch", ("Onion", "BIOS")
        state.path_text = str(blank)
        press(state, "enter")
        self.assertEqual(state.screen, "path")
        text = " ".join(" ".join(install.tui_view(state, 40, 12)).split())
        self.assertIn("Not an Onion SD card: no Onion files on it.", text)
        self.assertNotIn("Ctrl-U clears", text)
        # P3-1: no hidden folder named in the short form, the detail after.
        self.assertNotIn(".tmp_update", state.status)
        self.assertIn(".tmp_update", state.status_more)
        # code_audit_13 P3-5: the prompt asks for the card.
        self.assertIn("The top folder of the Onion SD card:", text)

    def test_the_help_says_the_refusal_in_full(self):
        state = fresh_state()
        state.stack = ["setup", "folder"]
        state.platform, state.card_bound = "retroarch", ("Onion", "BIOS")
        blank = self.root / "STICK"
        blank.mkdir()
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._choose_card(state, blank)
            press(state, "?")
            text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("STICK is not an Onion SD card: no Onion files on it. Onion keeps "
                      ".tmp_update at the root of its card.", text)

    def test_wsl_drive_letters_are_taken_as_typed(self):
        """P2-4: the screen says D:, and D: typed was refused."""
        with mock.patch.dict(install._WSL, {"active": True}):
            for typed in ("D:", "D:\\", "d:\\", "\"D:\\\""):
                with self.subTest(typed=typed):
                    self.assertEqual(install._typed_folder(typed), Path("/mnt/d"))
                    self.assertEqual(install.typed_path(typed), Path("/mnt/d"))
            self.assertEqual(install._typed_folder("E:\\bios"), Path("/mnt/e/bios"))
            args = _args(dest=Path("D:\\"))
            install._wsl_dest(args)
            self.assertEqual(args.dest, Path("/mnt/d"))
            card = Path("/mnt/d")
            with mock.patch.object(install, "_card_what", return_value="Onion card"):
                self.assertEqual(install.card_line(card), "D: (Onion card)")
        self.assertEqual(install._typed_folder("D:"), Path("D:"))

    def test_a_dest_refused_on_the_setup_list_names_dest(self):
        """code_audit_13 P3-2: "Choose the root of the Onion card", where
        --dest fixes the folder, and the refused system stayed chosen."""
        blank = self.root / "USBSTICK"
        blank.mkdir()
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(dest=str(blank)), "linux")
            state.filters["platforms"] = "onion"
            state.focus["platforms"] = 1
            self.assertEqual(install.tui_rows(state)[1].label, "Onion, SD card")
            self.assertEqual(press(state, "enter"), [])
        self.assertEqual(state.screen, "platforms")
        # --dest is fixed: a stick that is no card is named again in the
        # command, another setup would not help (code_audit_15 P3-3).
        self.assertIn("Run it again with the card's top folder.", state.status)
        self.assertNotIn("choose another setup", state.status)
        self.assertEqual((state.platform, state.card_bound), ("", None))

    def test_on_the_device_a_folder_typed_is_taken_as_typed(self):
        """code_audit_13 P3-1: a KNULLI got bios added to what was typed."""
        share = self.root / "userdata" / "share"
        share.mkdir(parents=True)
        state = fresh_state()
        state.stack = ["setup", "platforms"]
        state.platform, state.card_bound = "knulli", ("KNULLI", "bios")
        install._open_folder_screen(state, Path("/userdata/bios"), "found")
        state.stack.append("path")
        state.path_text = str(share)
        text = " ".join(install.tui_view(state, 80, 24))
        self.assertIn("The folder for KNULLI:", text)
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "enter")
        self.assertEqual(state.dest, share)

    def test_a_card_in_a_reader_asks_for_its_card_once(self):
        """code_audit_13 P3-9, tui_audit_6 P3-7: "Which folder? Onion reads
        its BIOS files from" on a computer, the same card listed twice."""
        first, second = self.onion("D"), self.onion("E")
        with mock.patch.object(install, "_mounted_cards", return_value=[first, second]), \
                mock.patch.object(install, "detect_platforms",
                                  return_value=[("retroarch", SYSTEM_DIR)]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(), "linux")
            self.assertEqual(len(state.setups), 3)
            state.focus["setup"] = len(state.setups)
            press(state, "enter")
            state.filters["platforms"] = "onion"
            state.focus["platforms"] = 1
            press(state, "enter")
            rows = install.tui_rows(state)
            title = install.tui_view(state, 80, 24)[0]
        self.assertIn("Where is the SD card?", title)
        self.assertEqual([(row.action, row.value) for row in rows][:2],
                         [("card", first), ("card", second)])
        self.assertNotIn("default", [row.action for row in rows])
        # The RetroArch row stays this computer's, never the card's folder.
        state.stack = ["setup", "platforms"]
        state.filters["platforms"] = ""
        state.focus["platforms"] = install._platform_focus(state, "retroarch")
        with mock.patch.object(install, "_mounted_cards", return_value=[first, second]):
            press(state, "enter")
        self.assertEqual(state.folder_default, SYSTEM_DIR)

    def test_browsing_for_a_card_starts_on_the_drives(self):
        """tui_audit_6 P3-2: the browser opened on the home folder, Use this
        folder focused, saying to go back, the cards listed under it."""
        card = self.onion()
        state = fresh_state()
        state.stack = ["setup", "platforms"]
        state.platform, state.card_bound = "retroarch", ("Onion", "BIOS")
        with mock.patch.object(install, "_mounted_cards", return_value=[card]):
            install._open_folder_screen(state, None)
            state.focus["folder"] = [row.action for row in install.tui_rows(state)].index(
                "browse")
            press(state, "enter")
            rows = install.tui_rows(state)
        self.assertEqual(state.screen, "browse")
        self.assertEqual(rows[state.focus["browse"]].value, card)
        self.assertEqual(state.status, "Choose the Onion card below.")

    def test_a_number_outside_the_list_is_said(self):
        """tui_audit_6 P3-1: 9 was read as a folder."""
        card, other = self.onion(), self.onion("SECOND")
        answers = iter(["9", "0", "2"])
        with mock.patch.object(install, "_mounted_cards", return_value=[card, other]), \
                mock.patch.object(install, "_ask", side_effect=lambda _p: next(answers)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("onion", "retroarch", True)
        self.assertEqual(dest, other / "BIOS")
        self.assertEqual(out.getvalue().count(
            "Type a number from 1 to 2, a folder, Enter to look again, or q to quit."), 2)

    def test_the_articles_read(self):
        self.assertEqual([install._a(name) for name in ("Onion", "Allium", "ArkOS", "muOS",
                                                        "KNULLI")],
                         ["an Onion", "an Allium", "an ArkOS", "a muOS", "a KNULLI"])

    def test_every_card_row_reads_the_same(self):
        """tui_audit_6 P3-6: KNULLI, ROCKNIX and the MinUI family asked for
        their card without saying so."""
        state = fresh_state()
        state.stack = ["setup", "platforms"]
        labels = {row.value: row.label for row in install.tui_rows(state)
                  if row.action == "platform"}
        for plat in ("knulli", "rocknix", "minui", "nextui", "myminui"):
            self.assertEqual(labels[plat], f"{install.display_name(plat)}, SD card")
        self.assertEqual(labels["retroarch"], "RetroArch")
        # On the device itself it is that device's setup, not a card.
        share = Path("/userdata/bios")
        state.setups.append(("knulli", share, "KNULLI"))
        with mock.patch.dict(install._DETECTED_OS, {("knulli", share): "KNULLI"}):
            labels = {row.value: row.label for row in install.tui_rows(state)
                      if row.action == "platform"}
        self.assertEqual(labels["knulli"], "KNULLI")

    def test_offline_plan_counts_agree(self):
        """code_audit_13 P3-4: "3 downloaded" under "3 need the internet";
        tui_audit_6 P3-9.2: the data files read as if on hand."""
        state = checked_state()
        state.found = [{"event": "found", "platform": "retroarch", "dest": FILES[0]["dest"],
                        "folder": "/media/old", "path": "/media/old/x", "member": "",
                        "reason": ""}]
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
            self.assertNotIn("downloaded", text)
            state.found = []
            state.check = dict(state.check, data_missing=2636, data_bytes=40 * 1024 * 1024)
            text = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn("4 files and 2636 data files (40.0 MB) need the internet, which "
                      "--offline does not use", text)

    def test_the_error_row_and_the_bar_agree(self):
        """tui_audit_6 P3-9.3: "Choose another setup or folder" over
        "A/Enter Back"."""
        state = checked_state()
        state.run = "check"
        install._fail(state, ["went wrong"], 1)
        state.focus["error"] = 0
        self.assertEqual(install.tui_rows(state)[0].label, "Choose another setup or folder")
        self.assertEqual(install._primary(state), "A/Enter Choose")
        press(state, "enter")
        self.assertNotEqual(state.screen, "error")

    def test_a_long_name_keeps_a_size_at_40_columns(self):
        """tui_audit_6 P3-5: Nintendo Game Boy Advance lost its size.
        tui_audit_10 P3-14: and kept it rounded, 3 KB beside a total of
        2.7 KB. The size stays whole, the name is cut."""
        row = install.TuiRow("system", "[x] Nintendo Game Boy Advance", "1 file, 16.0 KB",
                             "nintendo-gba", narrow=("16.0 KB",), keep_right=True)
        line = str(install._row_line(row, False, 38)).rstrip()
        self.assertTrue(line.endswith("16.0 KB"), line)
        self.assertRegex(line, r"Nintendo Game Boy A?d?\.\.\.")

    def test_the_shared_note_keeps_its_line(self):
        """tui_audit_6 P3-5: the page held 4, 3 and 5 systems as the note
        came and went, and "+19" read as 19 more."""
        state = OneSystemInFewPresses().long_list("keys")
        state.files = [{"event": "file", "dest": "mame.zip", "size": 10,
                        "systems": ["sys-00", "sys-01"]},
                       {"event": "file", "dest": "two.zip", "size": 10,
                        "systems": ["sys-00", "sys-01"]}]
        state.stack.append("systems")
        footers = set()
        for focus in range(3, 12):
            state.focus["systems"] = focus
            lines = install.tui_view(state, 40, 12)
            footers.add(next(i for i, line in enumerate(lines) if "of 66 systems:" in line))
        self.assertEqual(len(footers), 1, footers)
        state.focus["systems"] = 3
        self.assertIn(" +2 shared: counted once", install.tui_view(state, 40, 12))


class AuditSevenOneCardReading(unittest.TestCase):
    """code_audit_14 P2-1 to P3-12 and tui_audit_7 P2-5, P3-1: one reading
    of a card's root, said the same on the screen and in line mode."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.dict(install._DETECTED_OS, {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def blank(self, name: str = "USBSTICK") -> Path:
        stick = self.root / name
        (stick / "photos").mkdir(parents=True)
        return stick

    def start(self, **given) -> install.TuiState:
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(**given), "linux")
        return state

    def test_a_blank_card_gets_one_folder_and_one_note_everywhere(self):
        """code_audit_14 P2-1, tui_audit_7 P2-5: --dest kept the stick's
        root, typed it got bios below it, and the screen said nothing."""
        stick = self.blank()
        for named, plat, below in (("knulli", "knulli", "bios"),
                                   ("rocknix", "rocknix", "roms/bios"),
                                   ("ArkOS", "retroarch", "bios")):
            with self.subTest(system=named):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    by_dest = install.card_dest(named, plat, stick)
                self.assertEqual(by_dest, (plat, stick / below))
                note = install.card_choice(named, plat, stick).note
                self.assertTrue(note)
                self.assertIn(note, out.getvalue())
                state = self.start(platform=named, dest=str(stick))
                self.assertEqual(state.dest, stick / below)
                self.assertEqual(state.card_note, note)
                # tui_audit_10 P3-8: one rule with line mode, whose question
                # creates one missing level and says it. Two are asked, and
                # the note is kept on Create the folder? and on the plan.
                one = (stick / below).parent.is_dir()
                self.assertEqual(state.screen, "checking" if one else "create")
                if not one:
                    self.assertIn(note.split(":")[0],
                                  " ".join(" ".join(install.tui_view(state, 120, 40)).split()))
                    install.tui_update(state, Key("enter"))
                self.assertEqual(state.card_note, note)

    def test_the_minui_family_refuses_a_blank_dest(self):
        """code_audit_14 P2-2: taken with --dest, refused typed."""
        stick = self.blank()
        for plat in ("minui", "nextui", "myminui"):
            with self.subTest(platform=plat):
                choice = install.card_choice(plat, plat, stick)
                self.assertIn("SD card", choice.refused)
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stderr(io.StringIO()):
                    install.card_dest(plat, plat, stick)

    def test_a_card_mounted_as_its_bios_folder_is_the_card(self):
        """code_audit_14 P3-7: an Onion card mounted as BIOS was refused,
        ROCKNIX's roms/bios was taken for KNULLI, a muOS card's folder
        too."""
        onion = self.root / "BIOS"
        make_card(onion, "Onion")
        choice = install.card_choice("Onion", "retroarch", onion)
        self.assertEqual((choice.refused, choice.folder), ("", onion / "BIOS"))
        knulli = self.root / "k" / "bios"
        make_card(knulli, "KNULLI")
        self.assertEqual(install.card_choice("knulli", "knulli", knulli).folder,
                         knulli / "bios")
        rocknix = self.root / "ROCKNIX"
        make_card(rocknix, "ROCKNIX")
        refused = install.card_choice("knulli", "knulli", rocknix / "roms" / "bios")
        self.assertIn("is a ROCKNIX card, not a KNULLI card", refused.refused)
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        refused = install.card_choice("knulli", "knulli", muos / "MUOS" / "bios")
        self.assertIn("is a muOS card", refused.refused)

    def test_no_folder_above_a_mounted_drive_is_its_card(self):
        """A card mounted as MUOS beside a stick: /media/me, which holds
        it, is no muOS card for the stick."""
        stick = self.blank()
        (self.root / "MUOS" / "bios").mkdir(parents=True)
        with mock.patch.object(install, "_mounted_cards",
                               return_value=[stick, self.root / "MUOS"]):
            choice = install.card_choice("knulli", "knulli", stick)
            self.assertEqual((choice.refused, choice.folder), ("", stick / "bios"))
            bios = self.root / "mnt" / "bios"
            (bios / "x").mkdir(parents=True)
        with mock.patch.object(install, "_mounted_cards", return_value=[bios]):
            # A drive mounted at a folder named bios is the card itself.
            self.assertEqual(install._card_and_below(
                install.card_setup("knulli", "knulli"), bios), (bios, False))

    def test_a_refused_dest_says_why_in_the_events(self):
        """code_audit_14 P3-8: code 1 and no reason in the stream."""
        stick = self.blank()
        proc = install_run("--events", "--platform", "onion", "--dest", str(stick),
                           "--manifest", str(REPO_ROOT / "install" / "retroarch.json"),
                           "--dry-run", env={install.OFFLINE_VARIABLE: "1"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        events = [json.loads(line) for line in proc.stdout.splitlines()]
        error = next(event for event in events if event["event"] == "error")
        self.assertEqual((error["stage"], error["reason"]), ("detect", "not_found"))
        self.assertIn("is not an Onion SD card", error["message"])
        self.assertEqual(events[-1], {"event": "end", "code": 1})
        table = (REPO_ROOT / "wiki" / "installer.md").read_text(encoding="utf-8")
        self.assertIn("not the card of the system named", table)

    def test_a_card_no_file_names_is_titled_by_its_system(self):
        """code_audit_14 P3-9: an ArkOS card chosen read as RetroArch."""
        stick = self.blank()
        state = self.start(platform="ArkOS", dest=str(stick))
        self.assertEqual(install.detected_os("retroarch", state.dest), "ArkOS")

    def test_a_list_named_for_another_systems_card_says_so(self):
        """code_audit_14 P3-10: --platform batocera on an Onion card."""
        onion = self.root / "ONION"
        make_card(onion, "Onion")
        choice = install.card_choice("batocera", "batocera", onion)
        self.assertEqual((choice.refused, choice.folder), ("", onion))
        self.assertIn("is an Onion card", choice.note)
        self.assertIn("--platform onion", choice.note)
        state = self.start(platform="batocera", dest=str(onion))
        self.assertEqual(state.card_note, choice.note)

    def test_amberelec_and_emuelec_are_asked_for_their_card(self):
        """code_audit_14 P3-11: "Use this folder" on /storage/roms of a PC."""
        for plat, name in (("amberelec", "AmberELEC"), ("emuelec", "EmuELEC")):
            with self.subTest(system=name):
                setup = install.card_setup(plat, plat)
                self.assertEqual((setup.name, setup.below), (name, ""))
                state = self.start(platform=plat)
                self.assertEqual(state.screen, "folder")
                self.assertIsNone(state.folder_default)
                self.assertIn("Where is the SD card?",
                              " ".join(install.tui_view(state, 80, 24)))
                stick = self.blank(f"{name}STICK")
                choice = install.card_choice(plat, plat, stick)
                self.assertEqual(choice.folder, stick)
                self.assertIn("cannot tell", choice.note)

    def test_install_py_has_two_blank_lines_around_each_definition(self):
        """code_audit_14 P3-12: E305 after _card_mount, and the same slip
        before _Interrupted, TuiRow and _setup_label (pycodestyle E302,
        E303, E305 at top level)."""
        import ast

        lines = (REPO_ROOT / "install.py").read_text(encoding="utf-8").splitlines()
        body = ast.parse("\n".join(lines)).body

        def blank_before(node: ast.stmt) -> int:
            # The longest run of blank lines between the code above and the
            # statement, its decorators and comments in between, as
            # pycodestyle counts them.
            index = min([node.lineno] + [item.lineno for item in
                                         getattr(node, "decorator_list", [])]) - 2
            runs, run = [], 0
            while index >= 0 and (not lines[index].strip() or lines[index].startswith("#")):
                if lines[index].strip():
                    runs.append(run)
                    run = 0
                else:
                    run += 1
                index -= 1
            return max(runs + [run])

        definitions = (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)
        for before, after in zip(body, body[1:]):
            count = blank_before(after)
            with self.subTest(line=after.lineno):
                if isinstance(after, definitions):
                    self.assertEqual(count, 2)
                elif isinstance(before, definitions):
                    self.assertEqual(count, 2)

    def test_a_refusal_is_said_once_and_another_card_names_its_remedy(self):
        """tui_audit_7 P3-1: said twice, and "Choose the card's top folder"
        for the top folder of another card."""
        stick = self.blank()
        state = self.start(platform="onion")
        state.pad = "keys"
        install._card_status(state, install.card_choice("Onion", "retroarch", stick))
        after = install._status_after(state)
        self.assertEqual(len(after), 1)
        self.assertTrue(after[0].endswith("? says more."))
        state.help = True
        self.assertIn("Onion keeps .tmp_update",
                      " ".join(" ".join(install.tui_view(state, 80, 24)).split()))
        state.help = False
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        install._card_status(state, install.card_choice("Onion", "retroarch", muos))
        self.assertEqual(state.status, "That is a muOS card. Insert the Onion card, or go "
                         "back and choose muOS.")
        with mock.patch.object(install, "_ask", side_effect=[str(muos), str(stick),
                                                              KeyboardInterrupt]), \
                mock.patch.object(install, "_mounted_cards", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(KeyboardInterrupt):
            install.card_install_dest("Onion", "retroarch", True)
        self.assertIn("Insert the Onion card, or choose muOS for that one.", out.getvalue())

    def test_line_mode_names_the_card_picked_and_its_folder(self):
        """tui_audit_7 P2-3, P3-5: no echo of the card picked, "Mount the
        card" under two mounted, /mnt/d where the list says D:."""
        cards = [self.root / "ONION", self.root / "ONION2"]
        for card in cards:
            make_card(card, "Onion")
        with mock.patch.object(install, "_ask", return_value="2"), \
                mock.patch.object(install, "_mounted_cards", return_value=cards), \
                mock.patch.object(install, "_missing_dest", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            dest = install.card_install_dest("Onion", "retroarch", True)
        self.assertEqual(dest, cards[1] / "BIOS")
        text = out.getvalue()
        self.assertIn("Type its number or its top folder:", text)
        self.assertNotIn("Mount the card", text)
        self.assertIn(f"Onion card ONION2, BIOS folder {cards[1] / 'BIOS'}", text)
        self.assertEqual(install.detected_os("retroarch", dest), "Onion")
        with mock.patch.object(install, "_ask", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            install._prompt_custom_selection("retroarch", [], [], name="Onion",
                                             bios_path=dest)
        self.assertIn("Onion: 0 files", out.getvalue())
        self.assertIn(f"BIOS folder: {dest}", out.getvalue())
        proc = install_run("--platform", "onion", "--dest", str(cards[0]), "--dry-run",
                           "--manifest", str(REPO_ROOT / "install" / "retroarch.json"),
                           env={install.OFFLINE_VARIABLE: "1"})
        self.assertNotIn("gets the RetroArch file list", proc.stdout)

    def test_wsl_names_the_folder_by_its_drive_letter(self):
        """tui_audit_7 P3-6, code_audit_14 P3-5."""
        with mock.patch.dict(install._WSL, {"active": True, "root": "/mnt"}):
            self.assertEqual(install._short_path(Path("/mnt/d/BIOS"), HOME), "D:\\BIOS")
            self.assertEqual(install.shown_path(Path("/mnt/d")), "D:\\")
            args = _args(dest=Path("D:\\"), save_dir=Path("F:\\saves"))
            setattr(args, "from", [Path("E:\\")])
            args.from_pack = [Path("E:\\pack.zip")]
            install._wsl_dest(args)
            self.assertEqual((args.dest, getattr(args, "from"), args.from_pack, args.save_dir),
                             (Path("/mnt/d"), [Path("/mnt/e")], [Path("/mnt/e/pack.zip")],
                              Path("/mnt/f/saves")))
        with mock.patch.dict(install._WSL, {"active": False}):
            self.assertEqual(install._short_path(Path("/mnt/d/BIOS"), HOME), "/mnt/d/BIOS")

    def test_a_multi_word_system_example_can_be_pasted(self):
        """code_audit_14 P3-6."""
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            install.card_install_dest("Anbernic stock firmware", "retroarch", False)
        self.assertIn("--platform anbernicstockfirmware --dest", err.getvalue())


class AuditSevenScreens(unittest.TestCase):
    """tui_audit_7 P2-1 to P3-9: what each screen offers and says."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def setups(self, pad: str = "keys") -> install.TuiState:
        state = install.TuiState(os_type="linux", home=HOME)
        state.pad = pad
        state.stack = ["platforms"]
        state.focus["platforms"] = 1
        return state

    def test_a_handheld_is_found_by_its_device_in_few_presses(self):
        """P2-1: Onion was row 30 of 34, its row said "RetroArch files"."""
        state = self.setups("gptokeyb")
        rows = install.tui_rows(state)
        labels = [row.label for row in rows]
        self.assertEqual(labels[1], "RetroArch")
        cards = [index for index, row in enumerate(rows) if row.label.endswith(", SD card")]
        # One block, right after RetroArch.
        self.assertEqual(cards, list(range(2, 2 + len(cards))))
        onion = labels.index("Onion, SD card")
        self.assertIn("Miyoo Mini", rows[onion].right)
        # At 40x12 a page is 6 rows: L1/R1 then Down, then A.
        page = 12 - 6
        presses = (onion - 1) // page + (onion - 1) % page + 1
        self.assertLessEqual(presses, 4)
        text = " ".join(install.tui_view(state, 40, 12))
        self.assertIn("handhelds come after RetroArch", " ".join(text.split()))
        state.pad = "keys"
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("type your device's name (/)", text)
        self.assertIn("Filter by setup or device name",
                      [what for _key, what in install._help_lines(state)])
        # Something found here: the list keeps its order.
        found = fresh_state()
        found.stack = ["setup", "platforms"]
        self.assertEqual([row.label for row in install.tui_rows(found)][2], "Batocera")

    def test_line_mode_takes_a_device_name(self):
        """P2-1: "miyoo" refused at the line menu."""
        with mock.patch.object(install, "_ask", side_effect=["bittboy"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install._choose_platform("Which?"), "MiyooCFW")
        self.assertIn("your device's name", out.getvalue())
        choices = install._line_setups()
        onion = [name for name, _label in choices].index("Onion") + 1
        with mock.patch.object(install, "_ask", side_effect=["miyoo mini+", str(onion)]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install._choose_platform("Which?"), "Onion")
        self.assertIn("setups run on 'miyoo mini+'", out.getvalue())
        self.assertIn(f"{onion}) Onion, SD card (Miyoo Mini, Miyoo Mini Plus)", out.getvalue())

    def test_one_spelling_per_device_and_one_press_for_one_match(self):
        """P3-9.2, P3-9.3, P3-9.4."""
        for name, devices in install._SETUP_DEVICES.items():
            with self.subTest(system=name):
                self.assertFalse(any("+" in device for device in devices))
        self.assertFalse(any(device.startswith("Anbernic RG35XX")
                             for device in install._SETUP_DEVICES["spruceOS"]))
        state = self.setups()
        state.filters["platforms"] = "mini+"
        self.assertIn("Onion, SD card", [row.label for row in install.tui_rows(state)])
        state.filters["platforms"] = "koriki"
        state.focus["platforms"] = 0
        self.assertEqual(install._primary(state), "A/Enter Choose")
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            press(state, "enter")
        self.assertEqual(state.screen, "folder")
        self.assertEqual(state.card_bound[0], "Koriki")
        rows = {row.value: row for row in install.tui_rows(self.setups())}
        self.assertNotIn("PAN4ELEC", rows["amberelec"].right)

    def test_help_rows_keep_their_column_at_40(self):
        """P3-9.1: "/ Filter (Esc clears)" ran back to the left edge."""
        for pad in ("gptokeyb", "gptokeyb_muxterm", "miyoo", "evmapy"):
            with self.subTest(pad=pad):
                state = self.setups(pad)
                state.filters["platforms"] = "x"
                state.help = True
                lines = [line[1:] for line in install.tui_view(state, 40, 12)[2:-1]
                         if line.strip()]
                column = lines[0].index("Move")
                for line in lines:
                    self.assertTrue(line[:column].strip() == "" or line[column - 2:column]
                                    == "  ", line)

    def test_copy_from_a_folder_with_none_says_so(self):
        """P2-2: the plan came back the same."""
        state = checked_state()
        old = self.root / "old"
        old.mkdir()
        state.from_folders = [old]
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("old holds none of these files.", text)
        state.found = [found("gba_bios.bin", str(old))]
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertNotIn("holds none", text)
        stick = self.root / "STICK"
        stick.mkdir()
        state.home = self.root
        with mock.patch.object(install, "_mounted_cards", return_value=[stick]), \
                mock.patch.object(install, "_listed", return_value=[]):
            install._open_from_browser(state)
        rows = install.tui_rows(state)
        self.assertEqual(rows[state.focus["browse"]].value, stick)

    def test_offline_plan_names_the_folder_already_copied_from(self):
        """code_audit_14 P3-1."""
        state = checked_state()
        state.from_folders = [self.root / "from1"]
        state.found = [found("gba_bios.bin", str(self.root / "from1"))]
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            text = " ".join(" ".join(install.tui_view(state, 100, 40)).split())
        self.assertIn("They are not in from1", text)
        self.assertNotIn("Copy them from your old card", text)

    def test_the_card_browser_focuses_the_card_asked_for(self):
        """P3-2: the muOS card, listed first, was focused for Onion."""
        muos, onion, second = self.root / "A_MUOS", self.root / "B_ONION", self.root / "C"
        make_card(muos, "muOS")
        make_card(onion, "Onion")
        make_card(second, "Onion")
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            state, _effects = install.tui_start(_args(platform="onion"), "linux")
        state.home = self.root
        state.focus["folder"] = [row.action for row in install.tui_rows(state)].index(
            "browse")
        with mock.patch.object(install, "_mounted_cards",
                               return_value=[muos, onion, second]):
            press(state, "enter")
            self.assertEqual(install.tui_rows(state)[state.focus["browse"]].value, onion)
            self.assertEqual(state.status, "Choose the Onion card below.")
            for line in install.tui_view(state, 40, 12):
                self.assertNotIn("Go back", line)

    def test_confirm_offers_no_install_while_it_counts(self):
        """P3-3."""
        state = checked_state()
        state.stack.append("confirm")
        state.running, state.dry = True, None
        self.assertEqual(install.tui_view(state, 80, 24)[-1].split(), ["B/Esc", "Back", "?",
                                                                      "Help"])
        self.assertNotIn("Install", [what for _key, what in install._help_lines(state)])

    def test_back_after_choosing_systems_lands_on_install(self):
        """P3-8."""
        state = checked_state()
        state.focus["plan"] = [row.action for row in install.tui_rows(state)].index("systems")
        press(state, "enter")
        self.assertEqual(state.screen, "systems")
        state.focus["systems"] = next(index for index, row in
                                      enumerate(install.tui_rows(state))
                                      if row.action == "system")
        press(state, " ", "esc")
        self.assertEqual(state.screen, "plan")
        rows = install.tui_rows(state)
        self.assertEqual(rows[state.focus["plan"]].action, "install")

    def test_a_lone_card_opens_the_first_screen_with_a_list(self):
        """P3-7: with --manifest, the card's folder was the first screen."""
        onion = self.root / "ONIONB"
        make_card(onion, "Onion")
        shutil.rmtree(onion / "BIOS")
        args = _args(platform="retroarch", manifest=str(REPO_ROOT / "install" /
                                                        "retroarch.json"))
        args.platform_from_list = True
        with mock.patch.object(install, "_mounted_cards", return_value=[onion]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.dict(install._DETECTED_OS, {}):
            state, effects = install.tui_start(args, "linux")
        self.assertEqual((state.screen, effects), ("setup", []))

    def test_a_file_left_for_its_save_directory_is_said_so(self):
        """P2-4: the menu counted 23, 22 were checked, and the end blamed
        the collection for the file whose save directory is unknown."""
        menus = []

        def menu(_plat, files, *_rest, **_named):
            menus.append([entry["dest"] for entry in files])
            return None

        with SaveDirectoryRepository() as repo:
            argv = ["install.py", "--platform", "retroarch", "--dest", str(repo.dest),
                    "--manifest", str(repo.root / "install" / "retroarch.json")]
            with mock.patch.dict(os.environ, repo.env()), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(install, "_is_terminal", return_value=True), \
                    mock.patch.object(install, "detect_os", return_value="linux"), \
                    mock.patch.object(install, "records_selection", return_value=True), \
                    mock.patch.object(install, "_prompt_custom_selection", menu), \
                    mock.patch.object(install, "_ask", return_value="y"), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    install.main()
                except SystemExit:
                    pass
                finally:
                    install.LOCAL.reset()
        self.assertEqual(len(menus), 1)
        self.assertNotIn("Azahar/Azahar/sysdata/keys.txt", menus[0])
        text = out.getvalue()
        self.assertIn("1 file read from RetroArch's save directory left out: its place is "
                      "not known. Name it with --save-dir next time.", text)
        self.assertNotIn("the collection does not hold", text)

    def test_plural_game_data_and_the_backup_line_at_40x12(self):
        """P3-9.5, P3-9.6."""
        state = checked_state()
        games = [{"dest": f"g{index}.dat", "system": "", "category": install.GAME_DATA}
                 for index in range(2)]
        with mock.patch.object(install, "_unavailable", return_value=(games, 0)):
            self.assertIn("Copy them from", " ".join(install._unavailable_lines(state)))
        with mock.patch.object(install, "_unavailable", return_value=(games[:1], 0)):
            self.assertIn("Copy it from", " ".join(install._unavailable_lines(state)))
        view = " ".join(" ".join(install.tui_view(state, 40, 12)).split())
        self.assertIn("1 wrong copy: backed up, replaced.", view)


class _Decided(Exception):
    """Raised where line mode has chosen its platforms and folders."""


def line_decision(args: argparse.Namespace, answer) -> tuple[list, str]:
    """(platforms, text) line mode reaches at a terminal for args: the run
    stops where its platforms are known, before anything is read or written.
    answer(prompt) answers each question."""
    def stop(_args, platforms):
        raise _Decided(platforms)

    install.LOCAL.reset()
    with mock.patch.object(install, "app_refusal", side_effect=stop), \
            mock.patch.object(install, "detect_os", return_value="linux"), \
            mock.patch.object(install.sys.stdin, "isatty", return_value=True), \
            mock.patch.object(install, "_is_terminal", return_value=True), \
            mock.patch.object(install, "_ask", side_effect=answer), \
            mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            install._install(args, io.StringIO())
        except _Decided as decided:
            return decided.args[0], out.getvalue()
        finally:
            install.LOCAL.reset()
    raise AssertionError("line mode chose nothing:\n" + out.getvalue())


def screen_decision(args: argparse.Namespace) -> tuple[str, Path]:
    """(platform, folder) the full screen reaches for args, A pressed on
    whatever it offers first, as Enter is in line mode."""
    install.LOCAL.reset()
    with mock.patch.object(install, "detect_os", return_value="linux"), \
            mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            state, effects = install._tui_startup(args)
            for _press in range(6):
                if any(effect[0] == "fetch" for effect in effects):
                    return state.platform, state.dest
                effects = install.tui_update(state, Key("enter"))
        finally:
            install.LOCAL.reset()
    raise AssertionError(f"the screen chose nothing: {state.stack}")


def screen_keys(args: argparse.Namespace, presses: list,
                until: tuple = ("fetch", "run")) -> install.TuiState:
    """The full screen for args once the keys are pressed, stopped as soon
    as it fetches the list or starts a run (until)."""
    install.LOCAL.reset()
    with mock.patch.object(install, "detect_os", return_value="linux"), \
            mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            state, effects = install._tui_startup(args)
            for key in presses:
                if any(effect[0] in until for effect in effects):
                    break
                effects = install.tui_update(state, key if isinstance(key, Key) else Key(key))
            return state
        finally:
            install.LOCAL.reset()


def engine_events(args: argparse.Namespace) -> tuple[list[dict], str]:
    """(events, text) of the line engine run in this process with --events,
    the run the screen starts for its check."""
    stream = io.StringIO()
    install.LOCAL.reset()
    try:
        with mock.patch.object(install.EVENTS, "stream", stream), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.suppress(SystemExit):
            install._install(args, io.StringIO())
    finally:
        install.LOCAL.reset()
    return [json.loads(line) for line in stream.getvalue().splitlines()], out.getvalue()


def screen_plan(args: argparse.Namespace, manifest: dict, events: list[dict],
                facts: dict | None = None) -> tuple[install.TuiState, str]:
    """The plan screen fed the events of a check run, as the screen reads
    its child: (state, text at 120x40)."""
    state, _effects = install.tui_start(args, "linux", detect=False)
    state.manifest, state.manifest_for = manifest, state.platform
    state.stack = ["checking"]
    install._start_run(state, "check")
    for event in events:
        install.tui_receive(state, ("event", state.run_id, event))
    with mock.patch.object(install, "_tui_folder_facts", return_value=facts or {
            "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
        install.tui_receive(state, ("ended", state.run_id, 0, []))
    return state, " ".join(" ".join(install.tui_view(state, 120, 40)).split())


class LineModeAndScreenDecideAlike(unittest.TestCase):
    """tui_audit_8 P1-1, P2-1 and code_audit_15 P2-2: line mode looked at
    this machine alone, took this computer's default folder with a card in
    the reader or nothing found, and the screen put RetroArch under A for a
    KNULLI card named with --dest. The same inputs now give the same
    platform and folder on both, A or Enter taking what each offers first."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for patcher in (mock.patch.dict(install._DETECTED_OS, {}),
                        mock.patch.dict(os.environ, {"HOME": str(self.root / "home"),
                                                     "XDG_CONFIG_HOME": str(self.root / "cfg"),
                                                     "RETROBIOS_OS": "linux"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.list = self.root / "retroarch.json"
        self.list.write_text(json.dumps({"manifest_version": 2, "platform": "retroarch",
                                         "files": []}))
        self.here = self.root / "pc" / "system"
        self.here.mkdir(parents=True)

    def card(self, name: str, system: str) -> Path:
        root = self.root / "media" / name
        make_card(root, system)
        return root

    def both(self, mounts: list, found: list, **given) -> tuple:
        def answer(prompt: str) -> str:
            # The first entry of a numbered list, the default of a folder,
            # the first drive of a folder question that has no default.
            first = prompt.startswith(">") or ("Enter for" not in prompt and " or 1" in prompt)
            return "1" if first else ""

        extra = {"declared_only": False, "offline": False}
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=found), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            platforms, text = line_decision(_args(**extra, **given), answer)
            install._DETECTED_OS.clear()
            screen = screen_decision(_args(**extra, **given))
        return platforms[0], screen, text

    def test_the_same_scenarios_give_the_same_platform_and_folder(self):
        onion = self.card("ONION", "Onion")
        muos = self.card("MUOS", "muOS")
        knulli = self.card("KNULLI", "KNULLI")
        bare = self.card("ONIONB", "Onion")
        shutil.rmtree(bare / "BIOS")
        default = install._default_dest("linux", "retroarch")
        pc = [("retroarch", self.here)]
        share = self.root / "media" / "SHARE"
        (share / "roms").mkdir(parents=True)
        batocera = self.root / "batocera.json"
        batocera.write_text(json.dumps({"manifest_version": 2, "platform": "batocera",
                                        "files": []}))
        blank = self.root / "blank"
        blank.mkdir()
        scenarios = {
            # code_audit_16 P1-1, P2-2, tui_audit_9 P1-1: the drive's root in
            # line mode, another system's card first on the screen.
            "a Batocera list, its SHARE drive": (
                [share], [], {"manifest": batocera}, ("batocera", share / "bios")),
            "a Batocera list, a muOS card and its SHARE drive": (
                [muos, share], [], {"manifest": batocera}, ("batocera", share / "bios")),
            # tui_audit_9 P2-3, P3-11: the setups found asked as the screen
            # asks, Enter on the first.
            "nothing named, RetroArch here": ([], pc, {}, ("retroarch", self.here)),
            "a list, RetroArch here and an Onion card": (
                [onion], pc, {"manifest": self.list}, ("retroarch", self.here)),
            # code_audit_16 P3-1: --dest with a setup found.
            "--dest a blank folder, RetroArch here": (
                [onion], pc, {"dest": blank}, ("retroarch", blank)),
            "a list, an Onion card in the reader": (
                [onion], [], {"manifest": self.list}, ("retroarch", onion / "BIOS")),
            "a list, an Onion card without its BIOS folder": (
                [bare], [], {"manifest": self.list}, ("retroarch", bare / "BIOS")),
            "a list, a KNULLI card in the reader": (
                [knulli], [], {"manifest": self.list}, ("retroarch", default)),
            "a list, nothing here": ([], [], {"manifest": self.list}, ("retroarch", default)),
            "nothing named, two cards": (
                [muos, onion], [], {}, ("retroarch", muos / "MUOS" / "bios")),
            "nothing named, nothing here": ([], [], {}, ("retroarch", default)),
            "--dest a KNULLI card, RetroArch here": (
                [knulli], pc, {"dest": knulli}, ("knulli", knulli / "bios")),
            "--platform onion, its card": (
                [onion, muos], pc, {"platform": "onion"}, ("retroarch", onion / "BIOS")),
        }
        for name, (mounts, found, given, expected) in scenarios.items():
            with self.subTest(scenario=name):
                line, screen, text = self.both(mounts, found, **given)
                self.assertEqual(tuple(line), expected, text)
                self.assertEqual(screen, expected)
                self.assertNotIn("using default path", text)

    def unasked(self, mounts: list, found: list, **given) -> tuple:
        """(platforms or exit code, text, events) of a run without
        questions: --no-input, or --yes, which implies it."""
        def stop(_args, platforms):
            raise _Decided(platforms)

        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        args = _args(declared_only=False, offline=False, no_input=True, **given)
        events = []
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=found), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "app_refusal", side_effect=stop), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install.EVENTS, "emit",
                                  side_effect=lambda kind, **keys: events.append((kind, keys))), \
                mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                install._install(args, io.StringIO())
            except _Decided as decided:
                return [tuple(each) for each in decided.args[0]], out.getvalue(), events
            except SystemExit as exit_:
                return exit_.code, out.getvalue() + err.getvalue(), events
        raise AssertionError(out.getvalue())

    def test_a_run_without_questions_sees_the_cards_the_screen_sees(self):
        """tui_audit_11 P1-1: --yes and --no-input looked at this machine
        alone, beside the Onion card the screen and line mode open on: the
        files went to the home folder, or the run said nothing is here."""
        onion = self.card("ONION", "Onion")
        default = install._default_dest("linux", "retroarch")
        for flag in ({"yes": True}, {}):
            for name, (mounts, given) in {
                "an Onion card, a list": ([onion], {"manifest": self.list}),
                "an Onion card, nothing named": ([onion], {}),
            }.items():
                with self.subTest(flag=flag, scenario=name):
                    chosen, text, _events = self.unasked(mounts, [], **flag, **given)
                    self.assertEqual(chosen, [("retroarch", onion / "BIOS")], text)
                    self.assertNotIn(str(default), text)
                    install._DETECTED_OS.clear()
                    with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                            mock.patch.object(install, "_mount_points",
                                              return_value=frozenset()), \
                            mock.patch.object(install, "detect_platforms", return_value=[]), \
                            mock.patch.object(install, "detect_frontends", return_value=[]):
                        screen = screen_decision(_args(declared_only=False, **given))
                    self.assertEqual(chosen, [screen])

    def test_a_run_without_questions_stops_on_two_setups(self):
        """code_audit_18 P2-3, tui_audit_11 P1-1: --yes beside two setups
        installed for both, where its help says it stops on what to name.
        It now stops with an error event naming each and its --dest."""
        onion, muos = self.card("ONION", "Onion"), self.card("MUOS", "muOS")
        pc = [("retroarch", self.here)]
        for flag in ({"yes": True}, {}):
            for name, (mounts, found, given) in {
                "two cards": ([onion, muos], [], {}),
                "a card and RetroArch here, a list": ([onion], pc, {"manifest": self.list}),
                "two setups here": ([], [("retroarch", self.here),
                                         ("emudeck", self.root / "Emulation" / "bios")], {}),
            }.items():
                with self.subTest(flag=flag, scenario=name):
                    code, text, events = self.unasked(mounts, found, **flag, **given)
                    self.assertEqual(code, 1, text)
                    errors = [keys for kind, keys in events if kind == "error"]
                    self.assertEqual([(e["stage"], e["reason"]) for e in errors],
                                     [("detect", "conflict")])
                    self.assertIn("a run without questions installs for one", text)
                    self.assertRegex(text, r"--platform \w+ --dest ")

    def share(self, name: str = "SHARE") -> Path:
        share = self.root / "media" / name
        (share / "system").mkdir(parents=True)
        (share / "roms").mkdir()
        (share / "system" / "batocera.conf").write_text("system.hostname=BATOCERA\n")
        return share

    def test_a_drive_is_offered_by_the_folder_its_system_reads(self):
        """code_audit_18 P2-2, tui_audit_11 P1-2: RetroArch's folder question
        numbered a Batocera SHARE drive and a KNULLI card, and one press put
        RetroArch's list at their root, beside roms/ and system/. A drive
        nothing names went to its root too. Now another system's drive is
        left out and said, in both modes, and a drive is its bios folder."""
        share, knulli = self.share(), self.card("KNULLI", "KNULLI")
        stick = self.root / "media" / "EASYROMS"
        (stick / "bios").mkdir(parents=True)
        onion = self.card("ONION", "Onion")
        mounts = [share, knulli, stick, onion]
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(install, "_ask", side_effect=["q"]), \
                self.assertRaises(SystemExit):
            install.ask_platform_folder("linux", "retroarch")
        text = out.getvalue()
        self.assertIn(f"1) EASYROMS: bios (card or drive) ({stick / 'bios'})", text)
        self.assertIn(f"2) ONION: BIOS (Onion SD card) ({onion / 'BIOS'})", text)
        self.assertNotIn("3)", text)
        self.assertIn("Not offered here, each reads its own system's files: SHARE (Batocera "
                      f"SHARE drive: --platform batocera --dest {share}), KNULLI (KNULLI SD "
                      f"card: --platform knulli --dest {knulli}).", text)
        state = install.TuiState(os_type="linux", home=HOME)
        state.platform, state.stack = "retroarch", ["folder"]
        state.folder_default, state.folder_kind = SYSTEM_DIR, "usual"
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()):
            rows = install.tui_rows(state)
            screen = " ".join(install.tui_view(state, 120, 40))
            state.focus["folder"] = next(index for index, row in enumerate(rows)
                                         if row.action == "drive")
            install.tui_update(state, Key("enter"))
        self.assertEqual([row.value for row in rows if row.action == "drive"],
                         [stick / "bios", onion / "BIOS"])
        self.assertIn("Not offered here", screen)
        self.assertEqual(state.dest, stick / "bios")

    def test_a_list_card_setups_read_offers_them_first(self):
        """tui_audit_11 P1-2: with a RetroArch pack or list and nothing found,
        the first screen was RetroArch's folder question: ArkOS, muOS and
        JELOS, which read the same list, were out of reach. Both modes now
        open on the setups that read it, RetroArch first."""
        stick = self.root / "media" / "EASYROMS"
        (stick / "bios").mkdir(parents=True)
        args = dict(manifest=self.list, declared_only=False, offline=False)
        text, asked = self.line_run(_args(**args), [], mounts=[stick])
        self.assertIn("Which setup is the RetroArch list for?", text)
        self.assertIn("ArkOS", text)
        self.assertNotIn("Batocera", text)
        self.assertIn("[Enter for RetroArch]", asked[0])
        with mock.patch.object(install, "_mounted_cards", return_value=[stick]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            install._DETECTED_OS.clear()
            state = screen_keys(_args(**args), [])
            with contextlib.redirect_stdout(io.StringIO()):
                install._open_local_sources(_args(**args))
            rows = install.tui_rows(state)
            labels = [row.label for row in rows]
            focused = rows[state.focus["platforms"]]
            install._DETECTED_OS.clear()
            ark = screen_keys(_args(**args), ["end", Key("char", "/"), *[
                Key("char", c) for c in "arkos"], "enter", "enter"])
        self.assertEqual(state.screen, "platforms")
        self.assertEqual(focused.value, "retroarch")
        self.assertTrue(any(label.startswith("ArkOS") for label in labels), labels)
        self.assertFalse(any(label.startswith("Batocera") for label in labels), labels)
        self.assertEqual(ark.screen, "folder")
        self.assertEqual(install._tui_card_bound(ark)[0], "ArkOS")

    # The owner's request of 2026-10-27: BIOS files and game data counted
    # apart, the game data choice on the plan itself and in the menu.

    def game_list(self) -> tuple[Path, list]:
        files = [self.entry("gba_bios.bin", b"gba", ["nintendo-gba"]),
                 self.entry("doom/doom1.wad", b"wad" * 10, ["doom"], category="game_data")]
        return self.listing("retroarch", files, categorized=True), files

    def test_line_mode_counts_game_data_apart_and_g_leaves_it_out(self):
        listing, _files = self.game_list()
        args = dict(manifest=listing, dest=self.here, declared_only=False, dry_run=True)
        text, asked = self.line_run(_args(**args), ["g", ""])
        self.assertIn("1 BIOS file (3 B) and 1 game data file (30 B).", text)
        self.assertIn("g) leave out the 1 game data file (30 B), game data: files a game or "
                      "a game engine reads, of no use to the other cores", text)
        after = text[text.index("Counted again"):]
        self.assertIn("RetroArch: 1 file, 3 B.", after)
        self.assertIn("g) keep the 1 game data file", after)
        self.assertIn("--no-game-data", after)
        self.assertIn("Left out as game data", after)
        self.assertNotIn("doom1.wad would", after)

    def test_the_plan_counts_game_data_apart_and_one_press_leaves_it_out(self):
        state = checked_state()
        manifest = json.loads(json.dumps(state.manifest))
        for entry in manifest["files"]:
            if entry["dest"] == "pcsx/extra.bin":
                entry["category"] = "game_data"
        state.manifest = manifest
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("3 BIOS files (1.0 MB) and 1 game data file (4 B). Game data: files a "
                      "game or a game engine reads, of no use to the other cores.", text)
        rows = install.tui_rows(state)
        row = next(row for row in rows if row.action == "gamedata")
        self.assertIn("kept, leave out", row.label)
        state.focus["plan"] = rows.index(row)
        effects = install.tui_update(state, Key("enter"))
        self.assertTrue(state.no_game_data)
        self.assertEqual(effects, [("run", "check")])
        self.assertIn(("--no-game-data", None), install.tui_flags(state))

    def test_the_result_counts_game_data_apart_in_both_modes(self):
        state = _installed_state(Path("/x/system"))
        state.manifest = {**state.manifest, "files": [
            *state.manifest["files"],
            {"dest": "doom/doom1.wad", "size": 30, "category": "game_data", "cores": None}]}
        state.finished = [("scph5500.bin", True, ""), ("doom/doom1.wad", True, "")]
        _title, lines = install.tui_outcome(state)
        self.assertIn("Installed: 1 BIOS file and 1 game data file.", lines)
        listing, _files = self.game_list()
        backup = self.root / "old"
        (backup / "doom").mkdir(parents=True)
        (backup / "gba_bios.bin").write_bytes(b"gba")
        (backup / "doom" / "doom1.wad").write_bytes(b"wad" * 10)
        args = _args(manifest=listing, dest=self.here, offline=True, declared_only=False,
                     no_input=True)
        setattr(args, "from", [backup])
        real = io.StringIO()
        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        with mock.patch.object(install, "detect_os", return_value="linux"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            install._install(args, real)
        self.assertIn("Done. 2 files installed", real.getvalue())
        self.assertIn("Installed: 1 BIOS file and 1 game data file.", real.getvalue())

    def test_line_mode_says_the_card_it_found_and_names_its_folder(self):
        onion = self.card("ONION", "Onion")
        with mock.patch.object(install, "_mounted_cards", return_value=[onion]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            _platforms, text = line_decision(_args(manifest=self.list, declared_only=False),
                                             lambda prompt: "")
        self.assertIn(f"Found Onion at {onion / 'BIOS'} (RetroArch files)", text)
        self.assertNotIn("No supported platform detected", text)

    def test_a_pack_gets_the_menu_and_its_folder_named(self):
        """P1-1: no menu and no folder before the copy, the pack's list
        recording no system."""
        from test_install_offline import _pack, PAYLOAD

        downloads = self.root / "downloads"
        downloads.mkdir()
        pack = _pack(downloads, "RetroArch_v1_BIOS_Pack.zip", PAYLOAD)
        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        menus = []
        real = install._prompt_custom_selection

        def menu(*given, **named):
            with mock.patch.object(install, "_ask", return_value="q"):
                try:
                    real(*given, **named)
                except SystemExit as stop:
                    menus.append(stop.code)
                    raise

        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(install, "_is_terminal", return_value=True), \
                mock.patch.object(install, "_ask", side_effect=lambda prompt: (
                    "" if prompt.startswith(">") else str(self.here))), \
                mock.patch.object(install, "_prompt_custom_selection", menu), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit):
            install._install(_args(from_pack=[pack], declared_only=False), io.StringIO())
        text = out.getvalue()
        self.assertEqual(menus, [130])
        self.assertIn("RetroArch was not found here.", text)
        self.assertIn(f"BIOS folder: {self.here}", text)
        self.assertIn("Enter) install everything", text)
        self.assertNotIn("c) choose systems", text)
        self.assertNotIn("Copying", text)

    # code_audit_17 and tui_audit_10: every line-mode finding, each answered
    # by line mode and the screen from one decision.

    def line_run(self, args: argparse.Namespace, answers: list, mounts: list = (),
                 found: list = ()) -> tuple[str, list]:
        """(text, prompts) of line mode at a terminal for args, the answers
        given in turn: the run stops at the first question left unanswered."""
        given, asked = iter(answers), []

        def answer(prompt: str) -> str:
            asked.append(prompt)
            try:
                return next(given)
            except StopIteration:
                raise _Decided(prompt) from None

        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        # The options the run was started with, as main() records them.
        invocation = install.Invocation()
        invocation.os_type = "linux"
        invocation.record(args)
        with mock.patch.object(install, "_INVOCATION", invocation), \
                mock.patch.object(install, "_mounted_cards", return_value=list(mounts)), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=list(found)), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(install, "_is_terminal", return_value=True), \
                mock.patch.object(install, "_ask", side_effect=answer), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.suppress(_Decided, SystemExit):
            install._install(args, io.StringIO())
        return out.getvalue() + err.getvalue(), asked

    def listing(self, plat: str, files: list, **extra) -> Path:
        path = self.root / f"{plat}-{len(list(self.root.glob('*.json')))}.json"
        path.write_text(json.dumps({"manifest_version": 2, "platform": plat,
                                    "files": files, **extra}))
        return path

    @staticmethod
    def entry(dest: str, payload: bytes, systems: list, **extra) -> dict:
        return {"dest": dest, "sha1": hashlib.sha1(payload).hexdigest(),
                "size": len(payload), "repo_path": f"bios/{dest}", "cores": None,
                "systems": systems, **extra}

    def test_o_then_the_setup_found_offers_the_folder_it_reads(self):
        """code_audit_17 P2-2, tui_audit_10 P1-1: o then RetroArch offered
        its built-in folder, and Enter filled it, where the screen keeps the
        folder retroarch.cfg names."""
        pc = [("retroarch", self.here)]
        args = dict(manifest=self.list, declared_only=False, offline=False)
        text, asked = self.line_run(_args(**args), ["o", "", ""], found=pc)
        self.assertIn(f"RetroArch reads its BIOS files from: {self.here}.", text)
        self.assertIn("(Enter for the folder RetroArch reads)", asked[2])
        self.assertNotIn("default BIOS folder", text)
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_platforms", return_value=pc), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            install._DETECTED_OS.clear()
            given = iter(["o", "", ""])
            line, _text = line_decision(_args(**args), lambda _prompt: next(given))
            install._DETECTED_OS.clear()
            state = screen_keys(_args(**args), ["end", "enter", "enter", "enter"])
        self.assertEqual(tuple(line[0]), ("retroarch", self.here))
        self.assertEqual((state.platform, state.dest), ("retroarch", self.here))

    def test_a_share_drive_typed_is_read_as_its_bios_folder(self):
        """code_audit_17 P2-3, tui_audit_10 P3-11: the SHARE drive's root,
        typed for Batocera or Recalbox, was the BIOS folder, beside roms/
        and system/. Another system's drive is refused, in both modes."""
        shares = {}
        for plat, marker in (("batocera", "system/batocera.conf"),
                             ("recalbox", "system/recalbox.conf")):
            share = self.root / "media" / f"SHARE-{plat}"
            (share / "system").mkdir(parents=True)
            (share / "roms").mkdir()
            # Batocera's own hostname: another would fit KNULLI renamed too.
            (share / marker).write_text("system.hostname=BATOCERA\n")
            shares[plat] = share
        for plat, share in shares.items():
            other = shares["recalbox" if plat == "batocera" else "batocera"]
            listing = self.listing(plat, [])
            with self.subTest(platform=plat):
                text, _asked = self.line_run(_args(manifest=listing, declared_only=False),
                                             [str(other), str(share)])
                self.assertIn(f"{other} is a {install.display_name(install.share_drive(other))}"
                              " SHARE drive", text)
                self.assertIn(f"SHARE drive: its BIOS folder is {share / 'bios'}", text)
                with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                        mock.patch.object(install, "detect_platforms", return_value=[]), \
                        mock.patch.object(install, "detect_frontends", return_value=[]):
                    line, _text = line_decision(_args(manifest=listing, declared_only=False),
                                                lambda _prompt, typed=str(share): typed)
                    refused = screen_keys(_args(manifest=listing), [
                        "end", "enter", Key("paste", str(other)), "enter"])
                    state = screen_keys(_args(manifest=listing), [
                        "end", "enter", Key("paste", str(share)), "enter"])
                self.assertEqual(tuple(line[0]), (plat, share / "bios"))
                self.assertEqual((state.platform, state.dest), (plat, share / "bios"))
                self.assertIn("SHARE drive", refused.status)
                self.assertEqual(refused.screen, "path")

    def test_the_setups_found_are_labelled_alike(self):
        """code_audit_17 P3-1, tui_audit_10 P3-3, P3-13: "Onion SD card
        (ONION) (/media/me/ONION/BIOS)", two pairs of brackets, and a MinUI
        card named by its root where its BIOS folder is Bios."""
        onion, minui = self.card("ONION", "Onion"), self.card("MINUI", "MinUI")
        mounts, pc = [onion, minui], [("retroarch", self.here)]
        text, _asked = self.line_run(_args(declared_only=False), [], mounts, pc)
        self.assertNotIn(") (", text)
        self.assertIn(f"Found MinUI at {minui / 'Bios'}", text)
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=pc), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            install._DETECTED_OS.clear()
            setups, _mounted = install.found_setups("linux")
            state = install.TuiState(os_type="linux", home=HOME)
            state.setups = setups
            for index, (plat, path, label) in enumerate(setups):
                state.focus["setup"] = index
                screen = " ".join(install.tui_view(state, 120, 40))
                folder = install.setup_bios_folder(plat, path)
                with self.subTest(setup=label):
                    self.assertIn(f"{index + 1}) {label}: {folder}", text)
                    self.assertIn(f"BIOSfolder:{folder}", screen.replace(" ", ""))

    def test_both_count_the_systems_out_of_the_same_list(self):
        """code_audit_17 P3-2: the plan said 1 of 12 systems where Choose
        systems and line mode listed 11, a system whose only file is read
        from an unknown save directory counted."""
        payload = b"gba"
        files = [self.entry("gba_bios.bin", payload, ["nintendo-gba"]),
                 self.entry("ds.bin", b"ds", ["nintendo-ds"]),
                 self.entry("Azahar/keys.txt", b"keys", ["nintendo-3ds"], root="save")]
        # No retroarch.cfg, and a BIOS folder not named as the list's: the
        # save directory is not known, and keys.txt is left out.
        manifest = {"base_destination": "RetroArch",
                    "save_directory": {"folder": "saves", "by_core": False}}
        listing = self.listing("retroarch", files, **manifest)
        args = dict(manifest=listing, dest=self.here, declared_only=False,
                    system=["nintendo-gba"])
        text, _asked = self.line_run(_args(**args), [])
        self.assertIn("for 1 of 2 systems (--system).", text)
        events, _out = engine_events(_args(**args, check=True, events=True, no_input=True))
        choices = next(event for event in events if event["event"] == "choices")
        self.assertEqual(choices["systems"], ["nintendo-ds", "nintendo-gba"])
        manifest = json.loads(listing.read_text())
        state, screen = screen_plan(_args(**args), manifest, events)
        self.assertIn("For 1 of 2 systems (--system).", screen)
        # Choose systems, opened from that plan, lists the same two.
        self.assertEqual(len(install.tui_systems(state)), 2)

    def test_after_o_the_list_offers_the_setups_its_file_list_serves(self):
        """code_audit_17 P3-3: Batocera was offered for a RetroArch list,
        and refused once its folder had been asked."""
        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        install.LOCAL.manifest = self.list
        line = [name for name, _label in install._line_setups([], set(), "linux")]
        state = install.TuiState(os_type="linux", home=HOME)
        state.stack = ["platforms"]
        screen = [install._row_name(row) for row in install.tui_rows(state)
                  if row.action != "filter"]
        self.assertEqual(line, screen)
        self.assertIn("retroarch", line)
        self.assertIn("Onion", line)
        self.assertNotIn("batocera", line)
        self.assertNotIn("knulli", line)

    def test_a_choice_of_systems_is_confirmed_before_anything_is_written(self):
        """tui_audit_10 P2-1, code_audit_17 P2-4: c installed as soon as the
        last list was answered, and its command left out --dest and
        --manifest. The menu comes back counted, as the screen's Confirm."""
        files = [self.entry("gba_bios.bin", b"gba", ["nintendo-gba"]),
                 self.entry("scph5501.bin", b"psx", ["sony-playstation"])]
        listing = self.listing("retroarch", files)
        args = _args(manifest=listing, dest=self.here, declared_only=False)
        text, asked = self.line_run(args, ["c", "1"])
        self.assertIn("Counted again for your choices. Nothing is written before Enter.", text)
        self.assertIn("RetroArch: 1 file, 3 B, for 1 of 2 systems.", text)
        self.assertIn("Enter) install these", text)
        self.assertEqual(asked[-1], "> ")
        command = next(line for line in text.splitlines()
                       if "Same choice without questions:" in line)
        for option in (f"--dest {self.here}", f"--manifest {listing}",
                       "--system nintendo-gba"):
            self.assertIn(option, command)
        self.assertNotIn("Copying", text)
        self.assertEqual(list(self.here.iterdir()), [])
        # The screen: Choose systems, A on one system, then Confirm.
        state = checked_state()
        press(state, "down", "enter")
        self.assertEqual(state.screen, "systems")
        effects = press(state, "enter")
        self.assertEqual((state.screen, effects), ("confirm", [("run", "confirm")]))

    def tree(self, ref: str, systems: list, count: int) -> dict:
        return {"ref": ref, "url": "https://example.org/tree.zip", "type": "zip",
                "systems": systems, "dest": ref,
                "files": {f"f{n}.dat": {"sha1": "a" * 40, "size": 10} for n in range(count)}}

    def test_both_count_the_data_files_of_the_systems_kept(self):
        """tui_audit_10 P2-2: line mode counted 2636 data files of other
        systems for every --system tried, where the screen counts none."""
        files = [self.entry("gba_bios.bin", b"gba", ["nintendo-gba"]),
                 self.entry("scph5501.bin", b"psx", ["sony-playstation"])]
        trees = [self.tree("gba-tree", ["nintendo-gba"], 2),
                 self.tree("psx-tree", ["sony-playstation"], 5)]
        listing = self.listing("retroarch", files, data_directories=trees)
        args = dict(manifest=listing, dest=self.here, declared_only=False)
        for system, count in (("nintendo-gba", 2), ("sony-playstation", 5)):
            with self.subTest(system=system):
                text, _asked = self.line_run(_args(**args, system=[system]), [])
                self.assertIn(f"and {count} data files (", text)
                events, _out = engine_events(_args(**args, system=[system], check=True,
                                                   events=True, no_input=True))
                data = next(event for event in events if event["event"] == "data")
                self.assertEqual(data["files"], count)
        text, _asked = self.line_run(_args(**args), [])
        self.assertIn("and 7 data files (", text)

    @unittest.skipUnless(hasattr(os, "statvfs"), "statvfs reads mount flags")
    def test_a_read_only_card_gets_one_remedy_and_no_sudo(self):
        """tui_audit_10 P2-3: the plan offered sudo for a read-only mount,
        with a command that dropped --from-pack and --manifest, and the
        result offered nothing. One advice, line mode before its menu, the
        plan and the result."""
        listing = self.listing("retroarch", [self.entry("a.bin", b"a", ["x"])])
        args = _args(manifest=listing, dest=self.here, declared_only=False)
        words = install.read_only_words(self.here)
        with mock.patch.object(install, "_on_read_only_mount", return_value=True), \
                mock.patch.object(install, "_folder_writable", return_value=False):
            text, _asked = self.line_run(args, [])
            state = checked_state({"free": 10 ** 9, "writable": False, "cores": []})
            plan = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
            state.summary = {"installed": 0, "up_to_date": 0, "pending": 1, "kept": [],
                             "failed": [{"dest": "a.bin", "reason": install.NOT_WRITABLE}],
                             "skipped": [], "left_out": [], "interrupted": False}
            state.code = 2
            _title, result = install.tui_outcome(state)
        menu = text[:text.index("Enter)")]
        for said in (menu, plan, " ".join(result)):
            self.assertIn(words, said)
            self.assertNotIn("sudo", said)

    def test_the_sudo_command_keeps_every_option_of_the_run(self):
        """tui_audit_10 P2-3: the plan's sudo line dropped --from-pack and
        --manifest, and with --offline stopped at the file list."""
        state = checked_state({"free": 10 ** 9, "writable": False, "cores": []})
        state.manifest_path, state.manifest_given = Path("/l/retroarch.json"), True
        state.passthrough = [("--from-pack", "/p/RetroArch_BIOS_Pack.zip")]
        with mock.patch.object(install, "_on_read_only_mount", return_value=False):
            plan = " ".join(str(line) for line in install._plan_lines(state.check, state.dest,
                                                                       state))
        line = plan[plan.index("Or run it as root"):]
        for option in ("--manifest /l/retroarch.json", "--from-pack /p/RetroArch_BIOS_Pack.zip",
                       f"--dest {SYSTEM_DIR}"):
            self.assertIn(option, line)

    def test_the_save_files_are_counted_after_the_choice(self):
        """tui_audit_10 P3-2: --system sony-playstation said 106 files went
        to the save directory, none of them PlayStation's."""
        here = self.root / "pc" / "system"
        files = [self.entry("gba_bios.bin", b"gba", ["nintendo-gba"]),
                 self.entry("Azahar/keys.txt", b"keys", ["nintendo-3ds"], root="save")]
        listing = self.listing("retroarch", files, base_destination="system",
                               save_directory={"folder": "saves", "by_core": False})
        for system, count in ((["nintendo-gba"], 0), (["nintendo-3ds"], 1), (None, 1)):
            args = dict(manifest=listing, dest=here, declared_only=False, system=system)
            with self.subTest(system=system):
                text, _asked = self.line_run(_args(**args), [])
                events, _out = engine_events(_args(**args, check=True, events=True,
                                                   no_input=True))
                saves = [event for event in events if event["event"] == "saves"]
                if count:
                    self.assertIn("1 file read from RetroArch's save directory goes to", text)
                    self.assertEqual([event["files"] for event in saves], [1])
                else:
                    self.assertNotIn("save directory", text)
                    self.assertEqual(saves, [])

    def test_a_list_that_leaves_out_save_files_says_so_before_the_menu(self):
        """tui_audit_10 P3-4: ROCKNIX's 44 files left out were said after
        the install only, as the frontend's, where the plan says them."""
        omitted = [{"dest": f"saves/{n}.key", "name": f"{n}.key", "system": "",
                    "systems": [], "required": False, "reason": "no_save_directory",
                    "cores": None} for n in range(3)]
        listing = self.listing("rocknix", [self.entry("a.bin", b"a", ["x"])],
                               omitted_files=omitted)
        card = self.card("ROCKNIX", "ROCKNIX")
        args = _args(manifest=listing, dest=card, declared_only=False)
        text, _asked = self.line_run(args, [])
        said = "3 files read from ROCKNIX's save directory left out"
        self.assertLess(text.index(said), text.index("Enter)"))
        state = checked_state()
        state.platform = "rocknix"
        summary = {"skipped": [{"reason": "no_save_directory", "files": 3}]}
        self.assertIn(said, " ".join(install._unplaced_lines(summary, state)))
        # And the end of the run names it as the plan does.
        seen = [{**entry, "frontend": "ROCKNIX"} for entry in omitted]
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.suppress(SystemExit):
            install._report_outcome([self.here], 1, 0, 0, 3, False, omitted_entries=seen)
        self.assertIn(said, out.getvalue())

    def test_the_drives_mounted_here_are_offered_in_both(self):
        """tui_audit_10 P3-6: the copy question listed no drive and took q
        as a folder, and the screen's Which folder? listed none where line
        mode numbered them."""
        stick, onion = self.root / "media" / "USBSTICK", self.card("ONION", "Onion")
        stick.mkdir(parents=True)
        mounts = [stick]
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(install, "_ask", side_effect=["q"]), \
                self.assertRaises(SystemExit) as stop:
            install._ask_from_folder([], onion / "BIOS")
        self.assertEqual(stop.exception.code, 130)
        self.assertIn(f"1) {install.card_line(stick)}", out.getvalue())
        with mock.patch.object(install, "_mounted_cards", return_value=[stick, onion]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(install, "_ask", side_effect=["2", ""]):
            install._ask_from_folder([], onion / "BIOS")
        # The card installed to is not offered to copy from.
        self.assertNotIn(str(onion), out.getvalue())
        with mock.patch.object(install, "_mounted_cards", return_value=mounts), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(install, "_ask", side_effect=["1"]), \
                mock.patch.object(install, "_missing_dest", return_value=True):
            folder = install.ask_platform_folder("linux", "retroarch")
        # tui_audit_11 P1-2: a drive is offered by its bios folder, never its
        # root, which no setup reads.
        self.assertEqual(folder, stick / "bios")
        state = install.TuiState(os_type="linux", home=HOME)
        state.platform, state.stack = "retroarch", ["folder"]
        state.folder_default, state.folder_kind = SYSTEM_DIR, "usual"
        with mock.patch.object(install, "_mounted_cards", return_value=mounts):
            rows = install.tui_rows(state)
        self.assertEqual([row.value for row in rows if row.action == "drive"],
                         [stick / "bios"])

    def test_the_systems_are_listed_by_title_in_the_screen_order(self):
        """tui_audit_10 P3-7: line mode listed ids, nintendo-3ds before
        nintendo-ds, where the screen lists titles."""
        titles = {"nintendo-ds": "Nintendo DS", "nintendo-gba": "Nintendo Game Boy Advance"}
        files = [self.entry("ds.bin", b"ds", ["nintendo-ds"]),
                 self.entry("gba.bin", b"gba", ["nintendo-gba"]),
                 self.entry("3ds.bin", b"3ds", ["nintendo-3ds"])]
        manifest = {"system_titles": titles}
        listing = self.listing("retroarch", files, **manifest)
        text, _asked = self.line_run(_args(manifest=listing, dest=self.here,
                                           declared_only=False), ["c"])
        listed = text[text.index("Systems:"):]
        places = [listed.index(label) for label in (
            "1) Nintendo DS  nintendo-ds", "2) Nintendo Game Boy Advance  nintendo-gba",
            "3) nintendo-3ds")]
        self.assertEqual(places, sorted(places))
        state = install.TuiState(os_type="linux", home=HOME)
        state.manifest = {"files": files, **manifest}
        state.systems = [{"system": system, "name": system, "missing": 1, "wrong_hash": 0,
                          "bytes_needed": 2} for system in ("nintendo-3ds", "nintendo-ds",
                                                            "nintendo-gba")]
        order = [row["system"] for row in install._sorted_systems(state)]
        self.assertEqual(order, ["nintendo-ds", "nintendo-gba", "nintendo-3ds"])

    def test_the_card_question_looks_again_and_lists_blank_drives(self):
        """tui_audit_10 P3-8: Enter at Card root never looked again, a
        blank stick was listed for KNULLI on the screen alone, and the two
        modes differed on creating its bios."""
        onion = self.card("ONION", "Onion")
        seen: list = []
        answers = iter(["", "1"])

        def answer(_prompt: str) -> str:
            value = next(answers)
            if not value:
                # The card goes in while the question waits.
                seen.append(onion)
            return value

        with mock.patch.object(install, "_mounted_cards", side_effect=lambda: list(seen)), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "_ask", side_effect=answer), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("onion", "retroarch", True)
        self.assertEqual(dest, onion / "BIOS")
        self.assertIn(f"1) {install.card_line(onion)}", out.getvalue())
        # KNULLI fills its card when it first starts: a blank drive is
        # listed in both, after Look again on the screen, and its bios is
        # created without a question in both.
        stick = self.root / "media" / "USBSTICK"
        stick.mkdir(parents=True)
        with mock.patch.object(install, "_mounted_cards", return_value=[stick]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "_ask", side_effect=["1"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dest = install.card_install_dest("knulli", "knulli", True)
            state = screen_keys(_args(platform="knulli"), [])
            rows = install.tui_rows(state)
            self.assertEqual([row.action for row in rows][:2], ["rescan", "card"])
            self.assertEqual(rows[state.focus["folder"]].action, "rescan")
            press(state, "down", "enter")
        self.assertEqual(dest, stick / "bios")
        self.assertIn(f"1) {install.card_line(stick)}", out.getvalue())
        self.assertEqual((state.dest, state.screen), (stick / "bios", "checking"))

    def test_a_full_or_read_only_folder_is_said_before_enter(self):
        """tui_audit_10 P3-9, P3-19: line mode showed Enter) install
        everything over a full card and refused after it. On a card setup
        both say to free space on the card."""
        onion = self.card("ONION", "Onion")
        listing = self.listing("retroarch", [self.entry("a.bin", b"a" * 4096, ["x"])])
        usage = mock.Mock(free=1024)
        with mock.patch.object(install.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(install, "dest_drive", return_value=onion):
            install._DETECTED_OS[("retroarch", onion / "BIOS")] = "Onion"
            text, _asked = self.line_run(_args(manifest=listing, dest=onion / "BIOS",
                                               platform="retroarch", declared_only=False),
                                         [], [onion])
            state = checked_state({"free": 1024, "writable": True, "cores": []})
            state.dest = onion / "BIOS"
            install._DETECTED_OS[("retroarch", state.dest)] = "Onion"
            plan = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        menu = text[:text.index("Enter)")]
        self.assertIn("Not enough free space: 4.0 KB needed, 1.0 KB free. Free some space "
                      "on the card first.", menu)
        self.assertIn("Free some space on the card first.", plan)

    def test_a_device_typed_is_named_first(self):
        """tui_audit_10 P3-12: "miyoo mini" listed MinUI with RG28XX, RG34XX
        and RG34XX SP, where the screen's row names the Miyoo Mini."""
        install.LOCAL.reset()
        choices = install._line_setups([], set(), "linux")
        found = install._line_matches(choices, "miyoo mini", [], set(), "linux")
        rows = {install._row_name(row): row
                for row in install.setup_list([], set(), "miyoo mini", "linux")}
        self.assertTrue(found)
        for index, label in found:
            name = choices[index][0]
            with self.subTest(setup=name):
                self.assertIn("Miyoo Mini", label.split("(", 1)[1])
                first = rows[name].right.split(", ")[0]
                self.assertTrue(label.split("(", 1)[1].startswith(first), (label, first))

    def test_line_mode_says_setup_where_the_screen_does(self):
        """tui_audit_10 P3-13: "Detecting platform..." and "No supported
        platform found here", where the screen says setup and "Nothing was
        found here.\""""
        text, _asked = self.line_run(_args(declared_only=False), [])
        self.assertIn("Looking for setups here...", text)
        self.assertIn("Nothing was found here. Which setup?", text)
        self.assertNotIn("Detecting platform", text)
        self.assertNotIn("No supported platform", text)
        state = install.TuiState(os_type="linux", home=HOME)
        state.stack = ["platforms"]
        self.assertIn("Nothing was found here.", " ".join(install.tui_view(state, 80, 24)))

    def test_yes_at_a_terminal_asks_nothing(self):
        """tui_audit_10 P3-20: --yes waited at Install for: and at the menu."""
        seen = []
        with mock.patch.object(sys, "argv", ["install.py", "--yes", "--plain"]), \
                mock.patch.object(install, "_run", side_effect=seen.append):
            install.main()
        self.assertTrue(seen[0].no_input)
        self.assertIn("implies --no-input",
                      " ".join(install.build_parser().format_help().split()))
        # Asked nothing, with two setups found where the screen offers both.
        pc = [("retroarch", self.here), ("retroarch", self.root / "other")]
        seen[0].manifest = self.list
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            _text, asked = self.line_run(seen[0], [], found=pc)
        self.assertEqual(asked, [])


class AuditSeventeenAndTen(unittest.TestCase):
    """code_audit_17 and tui_audit_10: what the screen, the packs, the
    wording and the docs answer, line mode's parity being in
    LineModeAndScreenDecideAlike."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)

    @unittest.skipUnless(pyte and pty and hasattr(pty, "fork") and shutil.which("script"),
                         "the line mode run needs a pty")
    def test_the_printed_command_makes_the_same_choice(self):
        """code_audit_17 P2-4, tui_audit_10 P2-1: "Same choice without
        questions" left out the folder chosen at a question and the file
        list, and installed elsewhere or was refused. Run as printed, it
        finds every file of the choice in place and nothing else."""
        with FakeRepository() as repo:
            home = repo.root / "home"
            home.mkdir()
            listing = repo.root / "install" / "retroarch.json"
            env = {**repo.env(), "HOME": str(home), "RETROBIOS_OS": "linux", "TERM": "xterm",
                   "XDG_CONFIG_HOME": str(home / ".config")}
            env.pop("RETROBIOS_BOOTSTRAP", None)
            command = " ".join(shlex.quote(part) for part in [
                sys.executable, str(REPO_ROOT / "install.py"), "--plain",
                "--manifest", str(listing)])
            proc = subprocess.run(
                ["script", "-qec", command, "/dev/null"],
                input=f"{repo.dest}\nc\n2\n\n2\n\n", env=env, capture_output=True,
                text=True, timeout=120, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            installed = repo.installed()
            self.assertEqual(installed, ["pcsx/extra.bin", "scph5501.bin"])
            printed = re.search(r"Same choice without questions: (.+)", proc.stdout)[1]
            words = shlex.split(printed.strip())
            self.assertEqual(words[:2], ["python3", "install.py"])
            for option in ("--dest", "--manifest", "--system"):
                self.assertIn(option, words)
            again = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), *words[2:], "--dry-run",
                 "--events"], env=env, capture_output=True, text=True, timeout=120,
                check=False, stdin=subprocess.DEVNULL)
            files = [json.loads(line) for line in again.stdout.splitlines()
                     if '"event": "file"' in line]
            self.assertEqual(sorted(event["dest"] for event in files), installed)
            self.assertTrue(all(event["state"] == "present" for event in files))

    # tui_audit_10 P2-4: a pack's own list records its systems.

    @unittest.skipIf(sys.version_info < (3, 9),
                     "generate_pack.py's context managers need Python 3.9")
    def test_a_pack_records_the_systems_of_its_files(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(REPO_ROOT / "scripts"))
        import generate_pack  # noqa: PLC0415

        install_list = {"files": [{"dest": "a.bin", "systems": ["3do"]},
                                  {"dest": "b/c.bin", "systems": []}],
                        "system_titles": {"3do": "3DO Interactive Multiplayer"},
                        "system_names": {"3do": ["3do"]}}
        manifest = {"files": [{"path": "a.bin"}, {"path": "b/c.bin"}, {"path": "README.md"}]}
        generate_pack.annotate_pack_systems(manifest, generate_pack.pack_systems(install_list))
        self.assertEqual([entry.get("systems") for entry in manifest["files"]],
                         [["3do"], [], None])
        self.assertEqual(manifest["system_titles"], {"3do": "3DO Interactive Multiplayer"})
        source = (REPO_ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        self.assertIn("pack_systems_by_name=systems_for", source)
        schema = json.loads((REPO_ROOT / "schemas" / "pack-manifest.schema.json").read_text())
        self.assertIn("systems", schema["properties"]["files"]["items"]["properties"])

    def pack(self, systems: bool) -> Path:
        from test_install_offline import GBA, US  # noqa: PLC0415

        members = {"scph5501.bin": US, "gba_bios.bin": GBA}
        listed = [{"path": path, "sha1": hashlib.sha1(data).hexdigest(),
                   "md5": hashlib.md5(data).hexdigest(), "size": len(data),
                   "status": "verified", "name": path,
                   **({"systems": ["sony-playstation" if "scph" in path else "nintendo-gba"]}
                      if systems else {})} for path, data in members.items()]
        folder = self.root / ("new" if systems else "old")
        folder.mkdir()
        path = folder / "RetroArch_v1_BIOS_Pack.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for member, data in members.items():
                archive.writestr(member, data)
            archive.writestr("manifest.json", json.dumps({
                "files": listed, **({"system_titles": {"nintendo-gba": "Game Boy Advance"}}
                                    if systems else {})}))
        return path

    def test_system_works_with_a_pack_alone(self):
        dest = self.root / "system"
        dest.mkdir()
        new = self.pack(True)
        events, _text = engine_events(_args(from_pack=[new], dest=dest, platform="retroarch",
                                            system=["nintendo-gba"], check=True, events=True,
                                            no_input=True, declared_only=False))
        files = [event["dest"] for event in events if event["event"] == "file"]
        self.assertEqual(files, ["gba_bios.bin"])
        # The screen offers Choose systems with that pack, titled from it.
        install.LOCAL.pack = install.PackSource([new])
        manifest = install.LOCAL.pack.manifest("retroarch")
        self.assertEqual(manifest["system_titles"], {"nintendo-gba": "Game Boy Advance"})
        state = checked_state()
        state.manifest = manifest
        self.assertIn("systems", [row.action for row in install.tui_rows(state)])
        # An older pack names the remedy that works.
        old = self.pack(False)
        install.LOCAL.reset()
        err = io.StringIO()
        install.LOCAL.pack = install.PackSource([old])
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            install._requested_narrowing(_args(system=["nintendo-gba"]), "retroarch",
                                         install.LOCAL.pack.manifest("retroarch")["files"], [])
        self.assertIn("Add --manifest FILE, a copy of install/retroarch.json", err.getvalue())
        self.assertNotIn("older release", err.getvalue())

    def test_a_pack_cannot_write_escapes_through_its_system_names(self):
        """Security review of tui_audit_10 P2-4: the titles and ids a pack
        records reached print() raw in the systems list."""
        from test_install_offline import GBA, US  # noqa: PLC0415

        members = {"scph5501.bin": US, "gba_bios.bin": GBA}
        listed = [{"path": path, "sha1": hashlib.sha1(data).hexdigest(), "size": len(data),
                   "systems": ["bad\nid" if "scph" in path else "nintendo-gba"]}
                  for path, data in members.items()]
        path = self.root / "RetroArch_v1_BIOS_Pack.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for member, data in members.items():
                archive.writestr(member, data)
            archive.writestr("manifest.json", json.dumps({
                "files": listed,
                "system_titles": {"nintendo-gba": "\x1b]0;x\x07GBA", "bad\nid": "Bad"},
                "system_names": {"nintendo-gba": ["\x1b[2J"]}}))
        pack = install.PackSource([path])
        self.addCleanup(pack.close)
        manifest = pack.manifest("retroarch")
        self.assertEqual(manifest.get("system_titles", {}), {})
        self.assertEqual(manifest.get("system_names", {}), {})
        self.assertNotIn("systems", next(f for f in manifest["files"]
                                         if f["dest"] == "scph5501.bin"))
        # What _pick prints and the command line go through screen_text.
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(install, "_ask", side_effect=["1"]):
            install._pick("Systems:", [("x", [])], labels={"x": "\x1b]0;x\x07GBA"})
        self.assertNotIn("\x1b", out.getvalue())
        self.assertIn("?]0;x?GBA", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(install, "_ask", side_effect=["c", "1", "q"]), \
                contextlib.suppress(SystemExit):
            install._prompt_custom_selection(
                "retroarch", [{"dest": "a", "size": 1, "systems": ["x"], "cores": None}], [],
                command=lambda _choice: "python3 install.py --dest '\x1b[2J'")
        self.assertNotIn("\x1b", out.getvalue())

    def test_the_home_folder_is_said_on_the_plan(self):
        """tui_audit_10 P2-5: the plan named ~ and said nothing more."""
        state = checked_state()
        state.dest = state.home
        lines = " ".join(str(line) for line in install._plan_lines(state.check, state.dest,
                                                                     state))
        self.assertIn("No setup reads BIOS files in your home folder.", lines)

    def test_systems_seen_and_left_unchanged_keep_the_exact_plan(self):
        """tui_audit_10 P3-1: opening Choose systems on a --system plan and
        going back turned the exact plan into an estimate."""
        state = checked_state()
        state.cli_systems = ["nintendo-gba"]
        state.systems = [row for row in state.systems if row["system"] == "nintendo-gba"]
        state.ticked = {"nintendo-gba"}
        state.checked_flags = install.tui_flags(state)
        exact = install.tui_view(state, 80, 24)
        rows = install.tui_rows(state)
        state.focus["plan"] = next(i for i, row in enumerate(rows) if row.action == "systems")
        press(state, "enter")
        self.assertEqual(state.screen, "checking")
        # The check of every system comes back, the ticks kept.
        for event in [*FILES, *SYSTEMS, CHECK]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={
                "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
            install.tui_receive(state, ("ended", state.run_id, 0, []))
        self.assertEqual((state.screen, state.ticked), ("systems", {"nintendo-gba"}))
        press(state, "esc")
        self.assertEqual(state.screen, "plan")
        self.assertEqual(install.tui_view(state, 80, 24), exact)
        # Ticks changed: an estimate, without the whole list's counts.
        state.focus["plan"] = next(i for i, row in enumerate(install.tui_rows(state))
                                   if row.action == "systems")
        press(state, "enter")
        for event in [*FILES, *SYSTEMS, CHECK]:
            install.tui_receive(state, ("event", state.run_id, event))
        with mock.patch.object(install, "_tui_folder_facts", return_value={
                "free": 50 * 1024 ** 3, "writable": True, "cores": []}):
            install.tui_receive(state, ("ended", state.run_id, 0, []))
        state.ticked = {"sony-playstation"}
        press(state, "esc")
        text = " ".join(install.tui_view(state, 120, 40))
        self.assertIn("About", text)
        self.assertNotIn(f"{CHECK['present']} in place,", text)

    def test_the_copy_browser_leaves_the_card_being_installed_to(self):
        """tui_audit_10 P3-5: the card installed to was focused and taken,
        and WSL rows read /mnt/d where every other screen writes D:."""
        dest_card, old = self.root / "d", self.root / "e"
        make_card(dest_card, "Onion")
        old.mkdir()
        state = install.TuiState(os_type="wsl", home=self.root / "home")
        (self.root / "home").mkdir()
        state.platform, state.dest = "retroarch", dest_card / "BIOS"
        with mock.patch.object(install, "_mounted_cards", return_value=[dest_card, old]), \
                mock.patch.dict(install._WSL, {"active": True, "root": str(self.root)}):
            install._open_from_browser(state)
            rows = install.tui_rows(state)
            self.assertEqual(rows[state.focus["browse"]].value, old)
            self.assertIn("D: (Onion card)", [row.label for row in rows])
            state.focus["browse"] = next(i for i, row in enumerate(rows)
                                         if row.value == dest_card)
            press(state, "enter")
            press(state, "enter")
        self.assertEqual(state.screen, "browse")
        self.assertIn("That is the card being installed to", state.status)
        self.assertEqual(state.from_folders, [])

    def test_typing_a_filter_names_the_keys_that_end_it(self):
        """tui_audit_10 P3-10: while typing, the bar named Tick and Help,
        the focus stayed on None, F1 ended the typing, and a filter that
        matched nothing said nothing."""
        state = checked_state()
        press(state, "down", "enter")
        self.assertEqual(state.screen, "systems")
        press(state, "/")
        rows = install.tui_rows(state)
        self.assertEqual(rows[state.focus["systems"]].action, "filter")
        bar = install.tui_view(state, 80, 24)[-1]
        self.assertEqual(bar.strip(), "Enter Done  Esc Clear")
        press(state, "s", "e", "g", "a", " ", "?")
        self.assertEqual(state.filters["systems"], "sega ?")
        self.assertIn("No system matches 'sega ?'.", " ".join(install.tui_view(state, 80, 24)))
        press(state, "f1")
        self.assertTrue(state.help)
        press(state, "x")
        self.assertEqual((state.help, state.editing), (False, "systems"))

    def test_a_computer_finds_its_own_setups(self):
        """tui_audit_10 P3-11: windows and mac matched nothing, RetroBat
        came 27th on Windows, and a Batocera SHARE drive was not found."""
        names = {install._row_name(row) for row in install.setup_list([], set(), "windows",
                                                                      "windows")}
        self.assertEqual(names, {"retroarch", "retrobat", "emudeck", "bizhawk"})
        self.assertEqual({install._row_name(row) for row in install.setup_list(
            [], set(), "mac", "darwin")}, {"retroarch"})
        for os_type in ("windows", "wsl"):
            rows = [install._row_name(row) for row in install.setup_list([], set(), "", os_type)]
            self.assertEqual(rows[:3], ["retroarch", "retrobat", "emudeck"], os_type)
        share = self.root / "SHARE"
        (share / "system").mkdir(parents=True)
        (share / "system" / "batocera.conf").write_text("system.hostname=BATOCERA\n")
        with mock.patch.object(install, "_mounted_cards", return_value=[share]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.dict(install._DETECTED_OS, {}):
            setups, mounted = install.found_setups("linux")
        self.assertEqual(setups, [("batocera", share / "bios", "Batocera SHARE drive (SHARE)")])
        self.assertEqual(mounted, {("batocera", share / "bios")})

    def test_a_pack_preview_installs_from_the_pack(self):
        """tui_audit_10 P3-15: "23 files to download" on a --from-pack run."""
        state = checked_state()
        state.stack.append("preview")
        state.dry = dict(CHECK)
        install.LOCAL.pack = mock.Mock(name="pack")
        self.assertIn("to install from the pack", install._pager_items(state)[0])

    def test_scroll_is_named_only_where_the_text_scrolls(self):
        """tui_audit_10 P3-16: "Up/Down Scroll" over nine lines that fit."""
        state = checked_state()
        state.stack = ["result"]
        state.summary = {"installed": 1, "up_to_date": 0, "pending": 0, "failed": [],
                         "kept": [], "skipped": [], "left_out": [], "interrupted": False}
        state.code = 0
        self.assertNotIn("Scroll", install.tui_view(state, 80, 40)[-1])
        state.summary["failed"] = [{"dest": f"f{n}.bin", "reason": f"r{n}"} for n in range(20)]
        self.assertIn("Scroll", install.tui_view(state, 80, 12)[-1])

    def test_gptokeyb_names_select_then_start(self):
        """tui_audit_10 P3-17: "Start+Select" on the bars, "Select, then
        Start" in the help and the docs."""
        state = checked_state()
        state.pad = "gptokeyb"
        state.stack.append("checking")
        self.assertIn("Select+Start Quit", install.tui_view(state, 80, 24)[-1])
        self.assertEqual(install.TUI_ROUTES["gptokeyb"].stop[1], "B or Select+Start")

    def test_nothing_to_confirm_is_titled_so(self):
        """tui_audit_10 P3-18: Confirm over nothing to confirm, offline."""
        state = checked_state()
        state.stack.append("confirm")
        state.dry = dict(CHECK)
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            self.assertIn("Nothing to install", install.tui_view(state, 80, 24)[0])

    def test_another_folder_for_a_card_setup_asks_for_its_card(self):
        """tui_audit_10 P3-19: a full Onion card offered another folder for
        RetroArch, and the result said to choose another folder."""
        onion = self.root / "ONION"
        make_card(onion, "Onion")
        state = checked_state({"free": 1024, "writable": True, "cores": []})
        state.dest = onion / "BIOS"
        with mock.patch.object(install, "_mounted_cards", return_value=[onion]), \
                mock.patch.dict(install._DETECTED_OS, {("retroarch", onion / "BIOS"): "Onion"}):
            plan = " ".join(install.tui_view(state, 120, 40))
            self.assertIn("Free some space on the card first.", plan)
            rows = install.tui_rows(state)
            state.focus["plan"] = next(i for i, row in enumerate(rows) if row.action == "folder")
            press(state, "enter")
            view = " ".join(install.tui_view(state, 120, 40))
            state.stack = ["result"]
            state.tail = ["Needed: 87.9 KB. Free: 36.0 KB."]
            _title, lines = install.tui_outcome(state)
        self.assertEqual(state.card_bound, ("Onion", "BIOS"))
        self.assertIn("The BIOS folder chosen for Onion:", view)
        self.assertIn("Free some space on the card, then run again.", lines)

    def test_the_dest_example_is_this_systems(self):
        """code_audit_17 P3-4: /media/$USER/SHARE/bios on macOS and Windows."""
        self.assertEqual(install.drive_example("darwin", "SHARE", "bios"), "/Volumes/SHARE/bios")
        self.assertEqual(install.drive_example("wsl", "SHARE", "bios"), "/mnt/e/bios")
        self.assertEqual(install.drive_example("windows", "SHARE", "bios", "batocera"),
                         "\\\\BATOCERA\\share\\bios")
        self.assertEqual(install.drive_example("windows", "<card>"), "E:\\")
        self.assertEqual(install.drive_example("linux", "<card>"), "/media/$USER/<card>")

    def test_the_copy_browser_opens_on_a_path_without_dot_dot(self):
        """code_audit_17 P3-5: --from t/../packx opened the browser on t/.."""
        packx = self.root / "packx"
        packx.mkdir()
        state, _effects = install.tui_start(
            _args(**{"from": [str(self.root / "t" / ".." / "packx")]}), "linux", detect=False)
        self.assertEqual(state.from_folders, [packx])

    def test_no_clause_semicolon_in_what_the_installer_prints(self):
        """code_audit_17 P3-6."""
        self.assertNotIn("; ", " ".join(install.build_parser().format_help().split()))
        source = Path(install.__file__).read_text(encoding="utf-8")
        for said in ("to fetch them; a file", "every board; --target", "with the system; ",
                     "no core list; ", "{retrodeck_json}; assuming", "REGION_ALIASES)}; ",
                     "extracted; ~/.config", "read-only; 1 once"):
            self.assertNotIn(said, source)

    def test_the_docs_say_what_the_installer_does(self):
        """code_audit_17 P3-7."""
        page = (REPO_ROOT / "wiki" / "installer.md").read_text(encoding="utf-8")
        menu = page[page.index("RetroArch: 8348 files, 7.1 GB."):]
        menu = menu[:menu.index("```")]
        self.assertIn("BIOS folder:", menu)
        self.assertIn("f) copy from a folder or an old card", menu)
        # The owner's request of 2026-10-27: game data counted apart, g.
        self.assertIn("3259 BIOS files (4.4 GB) and 5089 game data files (2.7 GB).", menu)
        self.assertIn("g) leave out the 5089 game data files", menu)
        env = page[page.index("## Environment overrides"):]
        self.assertIn("`TERMINFO`", env)
        self.assertIn("`TERMINFO_DIRS`", env)
        script = (REPO_ROOT / "scripts" / "generate_installer_doc.py").read_text(encoding="utf-8")
        self.assertEqual([n for n, line in enumerate(script.splitlines(), 1) if len(line) > 100],
                         [])


class OneDeviceRegistry(unittest.TestCase):
    """code_audit_15 P2-1, tui_audit_8 P3-2, P3-3, P3-4: one device spelled
    several ways found one system, one press took it, MinUI listed brands."""

    def found(self, typed: str, os_type: str = "linux") -> set:
        return {row.value[1] if row.action == "cardsystem" else row.value
                for row in install.setup_list([], set(), typed, os_type)}

    def test_one_spelling_per_device(self):
        keys = {}
        for name in install._DEVICES:
            keys.setdefault(install._device_key(name), []).append(name)
        self.assertEqual({key: names for key, names in keys.items() if len(names) > 1}, {})
        for system, devices in install._SETUP_DEVICES.items():
            with self.subTest(system=system):
                self.assertEqual([name for name in devices if name not in install._DEVICES], [])

    def test_every_spelling_finds_every_system_that_runs_it(self):
        rk2023 = {"ArkOS", "KOLOS", "JELOS", "rocknix"}
        cubexx = {"muOS", "knulli", "rocknix", "minui"}
        flip2 = {"knulli", "rocknix"}
        for typed, systems in (("Powkiddy RK2023", rk2023), ("RK2023", rk2023),
                               ("rk-2023", rk2023), ("RG CubeXX", cubexx),
                               ("RGCUBEXX", cubexx), ("Anbernic RG CubeXX", cubexx),
                               ("Retroid Pocket Flip 2", flip2), ("flip2", flip2)):
            with self.subTest(typed=typed):
                self.assertEqual(self.found(typed), systems)
        self.assertIn("ArkOS", self.found("Anbernic RG353V"))
        self.assertTrue({"Onion", "minui", "myminui", "Allium"} <= self.found("Miyoo Mini"))
        self.assertTrue({"Onion", "minui"} <= self.found("Miyoo Mini+"))
        self.assertIn("minui", self.found("TrimUI Brick"))
        self.assertIn("Anbernic stock firmware", self.found("rg35xx"))
        self.assertIn("JELOS", self.found("rgb10 max 3"))

    def test_android_is_this_machine_only_on_android(self):
        self.assertNotIn("retroarch", self.found("android"))
        self.assertNotIn("retroarch", self.found("retroid pocket 5"))
        self.assertIn("retroarch", self.found("android", "android"))

    def test_a_device_several_systems_run_is_never_taken_in_one_press(self):
        out = io.StringIO()
        with mock.patch.object(install, "_ask",
                               side_effect=["Powkiddy RK2023", "", "0", "35", "q"]), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            install._choose_platform("Which?", "retroarch")
        text = out.getvalue()
        self.assertIn("4 setups run on 'Powkiddy RK2023':", text)
        # Enter after the matches takes none, and 0 and 35 are numbers.
        self.assertEqual(text.count("Type a number from 1 to 34"), 3)
        self.assertNotIn("run on '0'", text)
        self.assertNotIn("run on '35'", text)

    def test_the_line_menu_is_the_screen_list_numbered(self):
        for setups in ([], [("retroarch", SYSTEM_DIR, "RetroArch")]):
            with self.subTest(found=bool(setups)):
                rows = install.setup_list(setups, set(), "", "linux")
                self.assertEqual([name for name, _label in install._line_setups(setups, None,
                                                                                 "linux")],
                                 [install._row_name(row) for row in rows])
        labels = dict(install._line_setups())
        self.assertIn("RG28XX", labels["minui"])
        self.assertNotIn("(Anbernic, Miyoo", labels["minui"])
        self.assertIn("RG35XX Plus", labels["Anbernic stock firmware"])
        self.assertNotIn("RetroArch files", labels["JELOS"])

    def test_forty_columns_keep_the_name_whole_and_a_device_count(self):
        rows = {row.label: row for row in install.setup_list([], set(), "", "linux")}
        for label in ("KNULLI, SD card", "ROCKNIX, SD card", "CrossMix-OS, SD card",
                      "AmberELEC, SD card", "JELOS, SD card"):
            with self.subTest(row=label):
                line = str(install._row_line(rows[label], False, 38))
                self.assertIn(label, line)
                self.assertTrue(line.rstrip() != ("  " + label).rstrip(), line)
        # tui_audit_9 P3-3: its one device, cut, where "1 device" named none.
        self.assertTrue(str(install._row_line(rows["CrossMix-OS, SD card"], False, 38))
                        .rstrip().endswith("TrimUI Smar..."))


class AuditFifteenAndEightScreens(unittest.TestCase):
    """The P3 points of code_audit_15 and tui_audit_8 the screens show."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.dict(install._DETECTED_OS, {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, mounts=(), found=(), **given) -> tuple:
        with mock.patch.object(install, "_mounted_cards", return_value=list(mounts)), \
                mock.patch.object(install, "detect_platforms", return_value=list(found)), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            return install.tui_start(_args(**given), "linux")

    def test_a_card_found_by_its_files_gets_its_folder_without_a_question(self):
        """tui_audit_8 P3-12: Create the folder? for the Onion card's own
        BIOS, which line mode created."""
        onion = self.root / "ONIONB"
        make_card(onion, "Onion")
        shutil.rmtree(onion / "BIOS")
        state, effects = self.start([onion], platform="onion")
        self.assertEqual(effects, [("fetch", "retroarch")])
        self.assertEqual(state.dest, onion / "BIOS")
        self.assertIn("BIOS is created on ONIONB", state.card_note)

    def test_a_refused_dest_says_the_remedy_that_works(self):
        """code_audit_15 P3-3, P3-4."""
        rocknix = self.root / "ROCKNIX"
        make_card(rocknix, "ROCKNIX")
        stick = self.root / "STICK"
        (stick / "photos").mkdir(parents=True)
        for named, plat, dest, words, never in (
                ("knulli", "knulli", rocknix / "roms" / "bios", "--platform rocknix", "<card>"),
                ("onion", "retroarch", stick, "Name the top folder of the Onion card",
                 "<card>/BIOS")):
            with self.subTest(named=named):
                with contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit):
                    install.card_dest(named, plat, dest)
                self.assertIn(words, err.getvalue())
                self.assertNotIn(never, err.getvalue())
        minui = self.root / "MINUI"
        make_card(minui, "MinUI")
        (minui / "Bios").mkdir()
        state = install.TuiState()
        install._card_status(state, install.card_choice("minui", "minui", minui / "Bios"),
                             given=True)
        self.assertIn(f"Run it again with --dest {minui}.", state.status)

    def test_wsl_names_drives_in_line_mode_messages(self):
        """code_audit_15 P3-7: /mnt/f in a refusal where D:\\ was typed."""
        mnt = self.root / "mnt"
        make_card(mnt / "f", "muOS")
        (mnt / "g" / "photos").mkdir(parents=True)
        with mock.patch.dict(install._WSL, {"active": True, "root": str(mnt)}), \
                mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()):
            with contextlib.redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit):
                install.card_dest("onion", "retroarch", mnt / "f")
            self.assertIn("F:\\ is a muOS card", err.getvalue())
            note = install.card_choice("knulli", "knulli", mnt / "g").note
            self.assertIn("G:\\bios", note)
            self.assertNotIn(str(mnt), note)
            self.assertEqual(install._last_name(str(mnt / "g" / "old")), "old")

    def test_choose_the_card_below_only_when_one_is_mounted(self):
        """tui_audit_8 P3-6."""
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        stick = self.root / "USBSTICK"
        (stick / "photos").mkdir(parents=True)
        with mock.patch.object(install, "_mounted_cards", return_value=[muos, stick]):
            state, _effects = self.start([muos, stick], platform="onion")
            rows = install.tui_rows(state)
            state.focus["folder"] = next(index for index, row in enumerate(rows)
                                         if row.action == "browse")
            install.tui_update(state, Key("enter"))
            self.assertEqual(state.screen, "browse")
            self.assertIn("No Onion card is mounted", state.status)
            focused = install.tui_rows(state)[state.focus["browse"]]
            self.assertEqual(focused.value, stick)

    def test_type_a_path_says_f1_and_f1_says_more(self):
        """tui_audit_8 P2-2."""
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        with mock.patch.object(install, "_mounted_cards", return_value=[]):
            state, _effects = self.start(platform="onion")
        stick = self.root / "STICK"
        stick.mkdir()
        state.stack.append("path")
        install._card_status(state, install.card_choice("Onion", "retroarch", stick))
        text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("F1 says more.", text)
        self.assertNotIn("? says more", text)
        install.tui_update(state, Key("f1"))
        self.assertTrue(state.help)
        help_text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertIn("Onion keeps .tmp_update at the root of its card", help_text)

    def test_batocera_on_a_computer_offers_its_share(self):
        """tui_audit_8 P2-4."""
        stick = self.root / "SHARE"
        stick.mkdir()
        state, _effects = self.start([stick])
        state.stack = ["platforms"]
        state.focus["platforms"] = install._platform_focus(state, "batocera")
        with mock.patch.object(install, "_mounted_cards", return_value=[stick]):
            install.tui_update(state, Key("enter"))
            rows = install.tui_rows(state)
            text = " ".join(" ".join(install.tui_view(state, 80, 24)).split())
        self.assertEqual(state.screen, "folder")
        self.assertNotIn("/userdata/bios", text)
        self.assertIn("SHARE drive", text)
        self.assertEqual([row.action for row in rows], ["share", "browse", "type"])
        self.assertEqual(rows[0].value, stick / "bios")

    def test_the_first_screen_bar_says_quit(self):
        """tui_audit_8 P3-10."""
        state, _effects = self.start()
        self.assertEqual(state.stack, ["platforms"])
        self.assertIn("B/Esc Quit", install._TUI_VIEWS["platforms"](state, 80, 24)[2])
        state.stack = ["setup", "platforms"]
        self.assertIn("B/Esc Back", install._TUI_VIEWS["platforms"](state, 80, 24)[2])

    def test_a_pad_result_prints_no_command(self):
        """tui_audit_8 P3-9: a shell command on a controller-only screen."""
        state = _installed_state(self.root / "bios")
        state.summary["kept"] = [{"folder": str(self.root / "kept"), "files": 1}]
        state.pad = "muxterm"
        _title, lines = install.tui_outcome(state)
        text = " ".join(lines)
        self.assertNotIn("install.py", text)
        self.assertIn("kept in", text)
        state.pad = "keys"
        self.assertIn("Same choice without questions:", install.tui_outcome(state)[1])

    def test_the_checking_screen_is_titled_as_the_plan(self):
        """code_audit_15 P3-9.1."""
        state = install.TuiState()
        state.platform, state.dest = "retroarch", self.root / "bios"
        install._DETECTED_OS[("retroarch", state.dest)] = "ArkOS"
        state.stack = ["checking"]
        text = " ".join(install.tui_view(state, 80, 24))
        self.assertIn("ArkOS (RetroArch files):", text)

    def test_the_backup_line_survives_forty_columns(self):
        """tui_audit_8 P3-5: under the pack and copy split it was cut."""
        state = checked_state()
        state.found = [found("gba_bios.bin", "/media/old")]
        lines = [str(line) for line in install._plan_lines(state.check, state.dest, state)]
        self.assertTrue(any("copied from this machine" in line for line in lines), lines)
        counts = next(index for index, line in enumerate(lines) if " in place, " in line)
        self.assertTrue(lines[counts + 1].startswith("The 1 wrong copy is replaced"), lines)

    def test_an_empty_folder_is_said_once(self):
        """tui_audit_8 P3-7."""
        old = self.root / "USBSTICK"
        old.mkdir()
        state = checked_state()
        state.from_folders = [old]
        with mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}):
            lines = [str(line) for line in install._plan_lines(state.check, state.dest,
                                                              state)]
        said = [line for line in lines if "USBSTICK" in line]
        self.assertEqual(said, ["USBSTICK holds none of these files: run the installer again "
                                "with a pack (--from-pack) or without --offline."])

    def test_amberelec_keeps_its_files_on_the_games_part(self):
        """tui_audit_8 P3-13."""
        self.assertEqual(install.card_where("AmberELEC", ""),
                         "on the games part of its SD card")
        files = [{"dest": "bios/a.bin"}, {"dest": "bios/Machines/b.xml"}]
        self.assertEqual(install.shown_bios_folder(self.root, files), self.root / "bios")
        self.assertEqual(install.shown_bios_folder(self.root, [{"dest": "a.bin"}]),
                         self.root)

    def test_a_save_directory_left_out_has_one_wording(self):
        """code_audit_15 P3-9.3, tui_audit_8 P3-8."""
        files = [{"dest": "keys.txt", "root": "save", "sha1": "0" * 40, "size": 1}]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            install.place_save_entries(files, self.root / "BIOS",
                                       {"platform": "retroarch", "save_directory": {
                                           "folder": "saves", "by_core": False},
                                        "base_destination": "system"},
                                       None, "RetroArch")
        self.assertIn("left out: its place is not known. Name it with --save-dir.",
                      out.getvalue())


class CardBoundaries(unittest.TestCase):
    """code_audit_15 P3-2, P3-9.5: three folders at most, a bind mount not
    seen, a folder named MUOS taken for a card, ismount unprobed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def choice(self, folder: Path, points=frozenset()) -> install.CardChoice:
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_mount_points", return_value=points):
            return install.card_choice("knulli", "knulli", folder)

    def test_every_folder_up_to_the_mount_is_read(self):
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        deep = muos / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        self.assertIn("muOS", self.choice(deep).refused)

    def test_a_bind_mount_inside_a_card_is_its_own_drive(self):
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        stick = muos / "x"
        stick.mkdir()
        self.assertTrue(self.choice(stick).refused)
        choice = self.choice(stick, frozenset({str(stick)}))
        self.assertEqual((choice.refused, choice.folder), ("", stick / "bios"))

    def test_a_folder_named_muos_beside_it_is_no_card(self):
        backups = self.root / "stage" / "backups"
        (backups / "MUOS").mkdir(parents=True)
        (backups / "knulli-new").mkdir()
        choice = self.choice(backups / "knulli-new")
        self.assertEqual(choice.refused, "")
        self.assertEqual(choice.folder, backups / "knulli-new" / "bios")

    def test_mount_points_are_probed(self):
        with mock.patch.object(install, "_probed", wraps=install._probed) as probed, \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "_mounted_cards", return_value=[]):
            install._card_boundary(self.root)
        self.assertTrue(probed.called)
        points = install._mount_points()
        if Path("/proc/self/mountinfo").exists():
            self.assertIn("/", points)



def _screen_text(state: install.TuiState, cols: int = 80, rows: int = 24) -> str:
    return "\n".join(line.rstrip() for line in install.tui_view(state, cols, rows))


class AuditSixteenAndNineLineMode(unittest.TestCase):
    """code_audit_16 and tui_audit_9: line mode read other decisions than the
    screen (a drive's root for Batocera, no other setup once one was found,
    no Enter default, a folder question with no way out)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for patcher in (mock.patch.dict(install._DETECTED_OS, {}),
                        mock.patch.dict(os.environ, {"HOME": str(self.root / "home"),
                                                     "XDG_CONFIG_HOME": str(self.root / "cfg"),
                                                     "RETROBIOS_OS": "linux"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ask(self, *answers: str):
        asked = []
        given = iter(answers)

        def answer(prompt: str) -> str:
            asked.append(prompt)
            return next(given)

        return mock.patch.object(install, "_ask", side_effect=answer), asked

    def share_drive(self, name: str = "SHARE") -> Path:
        drive = self.root / "media" / name
        (drive / "roms").mkdir(parents=True)
        return drive

    def test_a_numbered_share_drive_is_its_bios_folder(self):
        """code_audit_16 P1-1, tui_audit_9 P1-1: 2) SHARE gave the drive's
        root, the screen its bios folder."""
        muos = self.root / "media" / "MUOS"
        make_card(muos, "muOS")
        share = self.share_drive()
        patcher, _asked = self.ask("1")
        with mock.patch.object(install, "_mounted_cards", return_value=[muos, share]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                patcher, contextlib.redirect_stdout(io.StringIO()) as out:
            folder = install.ask_platform_folder("linux", "batocera")
        self.assertEqual(folder, share / "bios")
        text = out.getvalue()
        self.assertIn(f"1) SHARE: bios ({share / 'bios'}, created)", text)
        self.assertNotIn("MUOS", text)

    def test_the_folder_question_quits_on_q_and_says_what_to_type(self):
        """code_audit_16 P2-3: q became the folder ./q, filled, and Enter
        with no default repeated the prompt with no word."""
        patcher, asked = self.ask("", "q")
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as stop:
            install.ask_platform_folder("linux", "batocera")
        self.assertEqual(stop.exception.code, 130)
        self.assertIn("q to quit", asked[0])
        self.assertIn("Type a folder, or q to quit.", out.getvalue())
        self.assertFalse((Path.cwd() / "q").exists())

    def test_a_relative_folder_is_named_in_full(self):
        """code_audit_16 P2-3: BIOS folder: q, Created q, Location: q."""
        here = self.root / "work"
        here.mkdir()
        patcher, _asked = self.ask("./q")
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                mock.patch.object(install.Path, "cwd", return_value=here), \
                mock.patch.object(install.os, "getcwd", return_value=str(here)), \
                contextlib.redirect_stdout(io.StringIO()):
            folder = install.ask_platform_folder("linux", "batocera", ask=False)
        self.assertTrue(folder.is_absolute())
        self.assertEqual(folder, here / "q")

    def test_a_card_root_question_quits_on_q(self):
        patcher, asked = self.ask("q")
        with mock.patch.object(install, "_mounted_cards", return_value=[]), patcher, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            install.card_install_dest("rocknix", "rocknix", interactive=True)
        self.assertEqual(stop.exception.code, 130)
        self.assertEqual(asked, ["Card root, Enter to look again, q to quit: "])

    def test_install_for_takes_enter_and_offers_another_setup(self):
        """tui_audit_9 P2-3, P3-11: no Enter default, an All the screen does
        not have, and no other setup once one was found."""
        here = self.root / "system"
        found = [("retroarch", here)]
        patcher, asked = self.ask("")
        with patcher, contextlib.redirect_stdout(io.StringIO()) as out:
            chosen = install._prompt_platform_choice(
                found, labels={("retroarch", here): "RetroArch"}, other=lambda: [])
        self.assertEqual(chosen, found)
        self.assertEqual(asked, ["> [Enter for RetroArch] "])
        self.assertIn("o) another setup or folder", out.getvalue())
        self.assertNotIn("All", out.getvalue())
        other = [("batocera", self.root / "bios")]
        patcher, _asked = self.ask("o")
        with patcher, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install._prompt_platform_choice(found, other=lambda: other), other)

    def test_the_setups_for_a_dest_come_in_the_screens_order(self):
        """code_audit_16 P3-1: with RetroArch found, --plain --dest put Onion
        at 19, the screen at 29."""
        dest = self.root / "blank"
        dest.mkdir()
        here = self.root / "system"
        listed = {}

        def choose(title, default="", setups=None, mounted=None):
            listed["rows"] = [name for name, _label in install._line_setups(
                setups, mounted, "linux")]
            listed["default"] = default
            raise _Decided(None)

        with mock.patch.object(install, "detect_platforms",
                               return_value=[("retroarch", here)]), \
                mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_choose_platform", side_effect=choose), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(_Decided):
            install._platform_for_dest("linux", dest, True)
        with mock.patch.object(install, "detect_platforms",
                               return_value=[("retroarch", here)]), \
                mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            state, _effects = install.tui_start(_args(dest=dest), "linux")
        screen = [install._row_name(row) for row in install.tui_rows(state)
                  if row.action != "filter"]
        self.assertEqual(listed["rows"], screen)
        rows = install.tui_rows(state)
        self.assertEqual(install._row_name(rows[state.focus["platforms"]]), listed["default"])

    def test_system_named_on_the_command_line_keeps_the_menu(self):
        """tui_audit_9 P3-8: --system skipped the menu and its folder line."""
        files = [{"dest": "a.bin", "size": 1, "systems": ["nintendo-gba"], "cores": None},
                 {"dest": "b.bin", "size": 1, "systems": ["sega-saturn"], "cores": None}]
        given = (["nintendo-gba"], [], [])
        patcher, _asked = self.ask("")
        with patcher, contextlib.redirect_stdout(io.StringIO()) as out:
            chosen = install._prompt_custom_selection(
                "retroarch", files, [], bios_path=self.root, given=given)
        self.assertEqual(chosen, given)
        text = out.getvalue()
        self.assertIn("RetroArch: 1 file, 1 B, for 1 of 2 systems (--system).", text)
        self.assertIn("Enter) install these", text)
        self.assertIn(f"BIOS folder: {self.root}", text)

    def test_narrowing_is_said_without_an_arrow(self):
        source = Path(install.__file__).read_text(encoding="utf-8")
        self.assertNotIn("} -> {len(files)}", source)

    def test_android_typed_names_retroarch(self):
        """tui_audit_9 P3-5: android matched nothing, and nothing more."""
        patcher, _asked = self.ask("android", "q")
        with patcher, mock.patch.object(install, "detect_os", return_value="linux"), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit):
            install._choose_platform("Which setup?")
        self.assertIn("On Android, choose RetroArch", out.getvalue())

    def test_batocera_named_on_a_computer_without_questions_is_refused(self):
        """code_audit_16 P3-3: /userdata/bios taken, Cannot create, end 2
        with no error event."""
        listing = self.root / "batocera.json"
        listing.write_text(json.dumps({"manifest_version": 2, "platform": "batocera",
                                       "files": []}))
        events = io.StringIO()
        with mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install.EVENTS, "stream", events), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as stop:
            install._install(_args(platform="batocera", no_input=True, manifest=listing),
                             io.StringIO())
        self.assertEqual(stop.exception.code, 1)
        self.assertNotIn("/userdata/bios", err.getvalue())
        self.assertIn("--dest /media/$USER/SHARE/bios", err.getvalue())
        kinds = [json.loads(line)["event"] for line in events.getvalue().splitlines()]
        self.assertEqual(kinds[-1], "error")


class AuditSixteenAndNineScreens(unittest.TestCase):
    """The screen points of code_audit_16 and tui_audit_9."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.dict(install._DETECTED_OS, {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, mounts=(), found=(), **given) -> tuple:
        with mock.patch.object(install, "_mounted_cards", return_value=list(mounts)), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_platforms", return_value=list(found)), \
                mock.patch.object(install, "detect_frontends", return_value=[]):
            return install.tui_start(_args(**given), "linux")

    def test_a_folder_holding_a_mounted_muos_card_is_no_muos_card(self):
        """code_audit_16 P2-2: /media/me held the MUOS mount point and read as
        a muOS card, and _own_card_root walked above the mount."""
        parent = self.root / "media"
        muos = parent / "MUOS"
        make_card(muos, "muOS")
        points = frozenset({str(muos)})
        with mock.patch.object(install, "_mount_points", return_value=points), \
                mock.patch.object(install, "_mounted_cards", return_value=[muos]):
            self.assertIsNone(install.card_system(parent))
            self.assertEqual(install.card_system(muos)[1], muos / "MUOS" / "bios")
            self.assertIsNone(install._own_card_root(muos / "bios"))
            self.assertEqual(install._own_card_root(muos / "MUOS" / "bios"), muos)

    def test_batocera_share_rows_leave_out_another_systems_card(self):
        """code_audit_16 P2-2: the muOS card was the first share row."""
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        share = self.root / "SHARE"
        share.mkdir()
        state, _effects = self.start([muos, share], platform="batocera")
        with mock.patch.object(install, "_mounted_cards", return_value=[muos, share]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()):
            rows = [(row.action, row.label) for row in install.tui_rows(state)]
        self.assertIn(("share", "SHARE: bios"), rows)
        self.assertNotIn(("share", "MUOS: bios"), rows)

    def test_a_proposed_folder_is_created_without_the_guard(self):
        """tui_audit_9 P2-1: Use this folder, then Create the folder? focused
        on Choose another folder, and Enter, Enter looped."""
        state = install.TuiState(os_type="linux", home=self.root)
        usual = self.root / ".config" / "retroarch" / "system"
        state.stack, state.platform = ["platforms"], "retroarch"
        install._open_folder_screen(state, usual)
        effects = install.tui_update(state, Key("enter"))
        self.assertEqual(effects, [("fetch", "retroarch")])
        self.assertNotIn("create", state.stack)
        self.assertIn("system is created in", state.card_note)
        typed = self.root / "a" / "b" / "c"
        state = install.TuiState(os_type="linux", home=self.root)
        state.stack, state.platform = ["folder"], "retroarch"
        install._choose(state, "retroarch", typed)
        self.assertEqual(state.screen, "create")
        self.assertEqual(install.tui_rows(state)[state.focus["create"]].action, "other")

    def test_a_page_moves_by_the_rows_drawn(self):
        """tui_audit_9 P2-2: at 40x12 PgDn moved six rows over four drawn,
        and muOS and ROCKNIX were never shown."""
        for cols, rows in ((40, 12), (40, 15), (80, 24)):
            with self.subTest(size=f"{cols}x{rows}"):
                state, _effects = self.start()
                state.cols, state.rows = cols, rows
                seen = set()
                for _press in range(40):
                    text = _screen_text(state, cols, rows)
                    seen |= {line.strip("|> ").split(",")[0].strip()
                             for line in text.splitlines()}
                    install.tui_update(state, Key("pgdn"))
                for name in ("muOS, SD card", "ROCKNIX, SD card", "JELOS, SD card",
                             "EmuELEC, SD card", "RetroOZ, SD card"):
                    self.assertTrue(any(line.startswith(name.split(",")[0]) for line in seen),
                                    name)

    def test_the_first_screen_says_quit_and_a_filter_says_clear(self):
        """tui_audit_9 P3-1, P3-2."""
        state, _effects = self.start(platform="onion")
        self.assertEqual(state.stack, ["folder"])
        bar = install.tui_view(state, 80, 24)[-1]
        self.assertIn("Esc Quit", bar)
        self.assertNotIn("Esc Back", bar)
        state, _effects = self.start()
        press(state, "/", "o", "n")
        self.assertIn("Esc Clear", install.tui_view(state, 80, 24)[-1])
        press(state, "enter")
        self.assertIn("Esc Clear", install.tui_view(state, 80, 24)[-1])

    def test_rows_at_40_columns_name_a_device(self):
        """tui_audit_9 P3-3: "1 device" for CrossMix-OS."""
        state, _effects = self.start()
        text = _screen_text(state, 40, 60)
        self.assertNotIn("1 device", text)
        crossmix = next(line for line in text.splitlines() if "CrossMix-OS" in line)
        self.assertIn("TrimUI", crossmix)

    def test_a_model_finds_the_systems_that_list_its_family(self):
        """code_audit_16 P2-1, tui_audit_9 P3-4: RG353V missed ROCKNIX, RG351P
        missed AmberELEC and ROCKNIX."""
        for system, devices in install._SETUP_DEVICES.items():
            for device in devices:
                for model in (device, *install._DEVICE_FAMILIES.get(device, ())):
                    rows = install.setup_list([], set(), model, "android")
                    names = {install._row_name(row) for row in rows}
                    with self.subTest(system=system, model=model):
                        self.assertIn(system, names)

    def test_the_filter_reads_system_names_as_line_mode_does(self):
        """code_audit_16 P3-2."""
        for typed, system in (("spruce os", "spruceOS"), ("unofficial os", "UnofficialOS"),
                              ("cross mix", "CrossMix-OS")):
            with self.subTest(typed=typed):
                rows = install.setup_list([], set(), typed, "linux")
                self.assertIn(system, {install._row_name(row) for row in rows})

    def test_android_on_the_filter_names_retroarch(self):
        state, _effects = self.start()
        state.filters["platforms"] = "android"
        self.assertIn("On Android, choose RetroArch", " ".join(_screen_text(state).split()))

    def test_a_wrapped_path_has_no_space_inserted(self):
        """tui_audit_9 P3-6: retroarch/ system."""
        path = "/home/moi/retroarch_system/tmp/tui_audit9/sb/nd/home/.config/retroarch/system"
        for width in range(30, 100):
            lines = install._wrap(f"{path} does not exist.", width)
            # Each line after the first is indented by two spaces.
            joined = lines[0] + "".join(line[2:] for line in lines[1:])
            with self.subTest(width=width):
                self.assertIn(path, joined)
                self.assertTrue(all("/ " not in line for line in lines), lines)

    def test_f1_names_what_the_card_lacks(self):
        """tui_audit_9 P3-7: F1 said the refusal again."""
        muos = self.root / "MUOS"
        make_card(muos, "muOS")
        setup = install.card_setup("Onion", "retroarch")
        choice = install._not_the_card(setup, muos, muos, "muOS")
        self.assertIn(".tmp_update", choice.detail)

    def test_help_at_40x12_keeps_quit_and_this_help(self):
        """tui_audit_9 P3-15, and Space for ticking on a keyboard."""
        state = checked_state()
        press(state, "down", "down", "down", "down", "enter")
        state.stack.append("systems")
        state.help = True
        text = _screen_text(state, 40, 12)
        self.assertIn("Quit", text)
        self.assertIn("This help", text)
        self.assertIn("X, Space, Right/Left", " ".join(text.split()))

    def test_the_copy_hint_fits_at_40_columns(self):
        """tui_audit_9 P3-16: cut after "copied,". On the CI runner /home
        held three folders, and the list cut the hint: it is kept whole,
        the list scrolling, from a folder of this test's own."""
        home = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        for name in ("linuxbrew", "packer", "runner"):
            (home / name).mkdir()
        state = checked_state()
        state.home = home
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_listed", return_value=[]):
            install._open_from_browser(state)
        self.assertEqual(state.browse_dir, home)
        text = " ".join(_screen_text(state, 40, 12).split())
        self.assertIn("Choose the old card, a backup folder or a ZIP.", text)
        self.assertIn("> Use this folder", text)

    def test_the_miyoo_bar_says_what_a_does(self):
        """tui_audit_9 P3-17: A Tick on All and None, where A unticks."""
        state = checked_state()
        state.pad = "miyoo"
        state.stack.append("systems")
        state.focus["systems"] = 1
        bar = install.tui_view(state, 40, 12)[-1]
        self.assertNotIn("A Tick", bar)
        state.focus["systems"] = 3
        self.assertIn("A Tick", install.tui_view(state, 40, 12)[-1])

    def test_system_given_is_said_and_widened(self):
        """tui_audit_9 P3-8: one system named, no word on the plan and no
        way to widen it."""
        state = checked_state()
        state.cli_systems = ["gba"]
        state.systems = [row for row in state.systems if row["system"] == "nintendo-gba"]
        state.ticked = {"nintendo-gba"}
        self.assertIn("For 1 of 3 systems (--system).", " ".join(_screen_text(state).split()))
        row = next(row for row in install.tui_rows(state) if row.action == "systems")
        effects = install._activate(state, row)
        self.assertEqual(effects, [("run", "check")])
        self.assertNotIn(("--system", "gba"), install.tui_flags(state))
        self.assertTrue(state.widen)

    def test_a_local_copy_draws_no_speed(self):
        """tui_audit_9 P3-9: 0 B/s 0:00 left."""
        out = io.StringIO()
        term = install.Terminal(out)
        term.live = True
        progress = install.Progress(2, 100, term)
        progress.add(50, resumed=True, network=False)
        progress._draw()
        self.assertNotIn("B/s", out.getvalue())
        self.assertNotIn("left", out.getvalue())

    def test_the_offline_remedy_follows_the_files_it_is_about(self):
        """tui_audit_9 P3-10."""
        unplaced = [{"reason": "no_save_directory", "frontend": "RetroArch"}]
        with mock.patch.object(install, "_offline_only", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit):
            install._report_outcome([self.root], 6, 0, 0, 1, False,
                                    omitted_entries=unplaced, offline=16,
                                    sources="6 files copied from OLD, 16 still missing.")
        lines = out.getvalue().splitlines()
        remedy = next(i for i, line in enumerate(lines) if "Copy them" in line
                      or "They are not in" in line)
        missing = next(i for i, line in enumerate(lines) if "still missing" in line)
        save = next(i for i, line in enumerate(lines) if "save directory" in line)
        self.assertEqual(remedy, missing + 1)
        self.assertLess(remedy, save)

    def test_first_install_says_where_to_type(self):
        """code_audit_16 P3-4, tui_audit_9 P3-19."""
        page = (REPO_ROOT / "wiki" / "first-install.md").read_text(encoding="utf-8")
        self.assertNotIn("There, type its name", page)
        self.assertIn("Press / and type the device", page)

    def test_a_folder_that_cannot_be_created_ends_on_an_error_and_a_summary(self):
        """code_audit_16 P3-3: Cannot create, then end 2 and nothing else."""
        import subprocess  # noqa: PLC0415

        from test_install_offline import PAYLOAD, _offline_env, _pack

        blocker = self.root / "file"
        blocker.write_text("not a folder")
        pack = _pack(self.root, "RetroArch_v1_BIOS_Pack.zip", PAYLOAD)
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--platform", "retroarch",
             "--dest", str(blocker / "system"), "--from-pack", str(pack), "--events"],
            env=_offline_env(), capture_output=True, text=True, timeout=120, check=False,
            stdin=subprocess.DEVNULL)
        events = [json.loads(line) for line in proc.stdout.splitlines()]
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[-3:], ["error", "summary", "end"], proc.stderr)
        self.assertEqual((events[-3]["stage"], events[-3]["reason"]), ("dest", "not_writable"))
        self.assertEqual(events[-1]["code"], 2)


class EdgesTheMutantsFound(unittest.TestCase):
    """Changes to the code that every test let through: the survivors of a
    mutmut run over the device filter, the card bounds and the WSL paths."""

    def test_a_folder_below_a_drive_is_named_with_backslashes(self):
        """Only the drive itself was ever shown: D:\\Games/bios passed."""
        for root in ("/mnt", "/mnt/", "/windows", "/drvX/"):
            with self.subTest(root=root), mock.patch.dict(install._WSL,
                                                         {"active": True, "root": root}):
                base = root.rstrip("/")
                self.assertEqual(install.shown_path(Path(f"{base}/d/Games/bios")),
                                 "D:\\Games\\bios")
                self.assertEqual(install.shown_path(Path(f"{base}/e")), "E:\\")

    def test_every_path_option_is_read_through_drvfs(self):
        """--cores-dir G:\\ under WSL stayed a relative path named G:."""
        with mock.patch.dict(install._WSL, {"active": True, "root": "/mnt"}):
            args = _args(dest=Path("D:\\bios"), save_dir=Path("F:\\saves"),
                         cores_dir=Path("G:\\RetroArch\\cores"))
            install._wsl_dest(args)
        self.assertEqual((args.dest, args.save_dir, args.cores_dir),
                         (Path("/mnt/d/bios"), Path("/mnt/f/saves"),
                          Path("/mnt/g/RetroArch/cores")))

    def card(self, system: str) -> Path:
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        make_card(root / "card", system)
        return root / "card"

    def test_a_card_is_looked_for_up_to_its_mount_point_and_no_further(self):
        """code_audit_15 P3-2 reads every folder up to the mount point:
        three at most took MUOS/a/b/c/d for a KNULLI card."""
        card = self.card("KNULLI")
        setup = install.card_setup("KNULLI", "knulli")
        deep = card / "a" / "b" / "c" / "d"
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install.os.path, "ismount", return_value=False):
            self.assertEqual(install._card_above(setup, deep), ("own", "KNULLI", card))
        mounted = frozenset({str(card / "a")})
        with mock.patch.object(install, "_mounted_cards", return_value=[]), \
                mock.patch.object(install, "_mount_points", return_value=mounted), \
                mock.patch.object(install.os.path, "ismount", return_value=False):
            self.assertIsNone(install._card_above(setup, deep))

    def test_a_minui_root_is_looked_for_two_folders_up_and_no_further(self):
        card = self.card("MinUI")
        self.assertEqual(install.card_root_above(card / "Bios" / "GBA"), ("minui", card))
        self.assertIsNone(install.card_root_above(card / "Bios" / "GBA" / "x"))
        self.assertIsNone(install.card_root_above(card))

    def test_the_filter_compares_the_letters_alone(self):
        self.assertEqual(install._folded("Pokémon Évolution"), "pokemon evolution")
        self.assertFalse(install._matches("Odroid", "x"))

    def test_a_letter_no_name_holds_finds_nothing(self):
        with mock.patch.dict(install._SETUP_DEVICES, {"batocera": ("Odroid",),
                                                      "Onion": ("Miyoo Mini",)}):
            self.assertIsNone(install._setup_matches("batocera", ["Onion"], "x"))
            self.assertEqual(install._setup_matches("batocera", ["Onion"], "mini"),
                             ["Onion"])
            self.assertEqual(install._setup_matches("batocera", ["Onion"], "odroid"),
                             ["Odroid"])



class AuditEighteenAndEleven(unittest.TestCase):
    """code_audit_18 and tui_audit_11: what the cards, the screens and the
    docs answer, line mode's parity being in LineModeAndScreenDecideAlike."""

    def setUp(self):
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        for patcher in (mock.patch.dict(install._DETECTED_OS, {}),
                        mock.patch.object(install, "_mount_points", return_value=frozenset())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def drive(self, name: str, files: dict) -> Path:
        root = self.root / "media" / name
        root.mkdir(parents=True)
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        return root

    ABOUT = "system/configs/emulationstation/about.info"

    def renamed(self) -> dict:
        """The drives code_audit_18 P1-1 names: KNULLI and Batocera each
        renamed in its own menu, with and without the file KNULLI alone
        writes, and Recalbox's share."""
        conf = "system/batocera.conf"
        return {
            "KNULLI renamed": self.drive("K1", {self.ABOUT: "KNULLI\n",
                                                conf: "system.hostname=RG40XXH\n"}),
            "KNULLI as shipped": self.drive("K2", {self.ABOUT: "KNULLI\n",
                                                   conf: "system.hostname=KNULLI\n"}),
            "renamed, no about.info": self.drive("K3", {conf: "system.hostname=RG40XXH\n"}),
            "Batocera as shipped": self.drive("B1", {conf: "system.hostname=BATOCERA\n"}),
            "Batocera, no hostname": self.drive("B2", {conf: "# system.hostname=X\n"}),
            "Recalbox": self.drive("R1", {"system/recalbox.conf": "a=1\n"}),
            "two shares": self.drive("T1", {conf: "system.hostname=BATOCERA\n",
                                            "system/recalbox.conf": ""}),
        }

    def test_a_renamed_card_is_named_by_the_file_no_menu_writes(self):
        """code_audit_18 P1-1: a KNULLI card whose hostname was changed in
        its menu was a Batocera SHARE drive, Enter filled it with Batocera's
        list and --platform knulli was refused."""
        drives = self.renamed()
        expected = {
            "KNULLI renamed": ("knulli", "bios", "KNULLI"),
            "KNULLI as shipped": ("knulli", "bios", "KNULLI"),
            "renamed, no about.info": None,
            "Batocera as shipped": ("batocera", "bios", "Batocera"),
            "Batocera, no hostname": ("batocera", "bios", "Batocera"),
            "Recalbox": ("recalbox", "bios", "Recalbox"),
            "two shares": None,
        }
        for name, drive in drives.items():
            with self.subTest(drive=name):
                found = install.card_system(drive)
                want = expected[name]
                self.assertEqual(found, want and (want[0], drive / want[1], want[2]))
                doubt, _why = install.share_doubt(drive)
                self.assertEqual(bool(doubt), want is None)
        self.assertEqual(install.share_doubt(drives["renamed, no about.info"])[0],
                         ("knulli", "batocera"))
        self.assertEqual(install.share_doubt(drives["two shares"])[0],
                         ("batocera", "recalbox"))

    def test_a_drive_two_systems_fit_is_taken_as_named_and_said(self):
        """code_audit_18 P1-1: --platform knulli is never refused on a
        drive whose only evidence is a batocera.conf, --platform batocera
        takes its bios folder, and the doubt is said. Recalbox refuses it
        with words for a SHARE drive (P3-4)."""
        drives = self.renamed()
        for name in ("renamed, no about.info", "Batocera as shipped"):
            drive = drives[name]
            with self.subTest(drive=name):
                choice = install.card_choice("knulli", "knulli", drive)
                self.assertEqual((choice.plat, choice.folder, choice.refused),
                                 ("knulli", drive / "bios", ""))
                self.assertIn("Taken as KNULLI's, as named", choice.note)
        drive = drives["renamed, no about.info"]
        choice = install.card_choice("batocera", "batocera", drive)
        self.assertEqual((choice.plat, choice.folder), ("batocera", drive / "bios"))
        self.assertIn("names the host RG40XXH", choice.note)
        refused = install.card_choice("recalbox", "recalbox", drive)
        self.assertIn("could be a KNULLI card or a Batocera SHARE drive", refused.refused)
        words = install._dest_refusal_words(refused, None)
        self.assertIn("Connect the Recalbox SHARE drive", words)
        self.assertNotIn("Insert", words)
        other = install.card_choice("recalbox", "recalbox", drives["Batocera as shipped"])
        words = install._dest_refusal_words(other, None)
        self.assertIn("Connect the Recalbox SHARE drive, or name that drive's own system: "
                      "--platform batocera", words)

    def test_the_first_screen_and_line_mode_say_the_doubt(self):
        """code_audit_18 P1-1: the screen's first row was "Batocera SHARE
        drive (KNULLI2)". A renamed card with about.info is KNULLI's, one
        without is listed by neither and said."""
        drives = self.renamed()
        knulli, doubt = drives["KNULLI renamed"], drives["renamed, no about.info"]
        with mock.patch.object(install, "_mounted_cards", return_value=[knulli, doubt]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            setups, _mounted = install.found_setups("linux")
        self.assertEqual([(plat, path) for plat, path, _label in setups],
                         [("knulli", knulli / "bios")])
        self.assertIn("KNULLI SD card", setups[0][2])
        text = out.getvalue()
        self.assertIn(f"{doubt} could be a KNULLI card or a Batocera SHARE drive", text)
        self.assertIn(f"--platform knulli --dest {doubt} or --platform batocera --dest {doubt}",
                      text)
        with mock.patch.object(install, "_mounted_cards", return_value=[knulli, doubt]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            state, _effects = install.tui_start(_args(), "linux")
        self.assertTrue(any("could be a KNULLI card" in note for note in state.notes))
        self.assertEqual([setup[0] for setup in state.setups], ["knulli"])

    def test_a_share_drive_is_never_called_a_card(self):
        """code_audit_18 P3-4, tui_audit_11 P3-14: "is a Batocera card"
        and "is a Recalbox card" for their SHARE drives."""
        drives = self.renamed()
        for name in ("Batocera as shipped", "Recalbox"):
            with self.subTest(drive=name):
                choice = install.card_choice("retroarch", "retroarch", drives[name])
                self.assertIn("SHARE drive, which reads", choice.note)
                self.assertNotIn(" card, which reads", choice.note)

    def test_two_cards_of_one_system_say_what_they_hold(self):
        """tui_audit_11 P3-15: an old Onion card with 6 BIOS files and a new
        empty one looked the same, and Enter took the first by name."""
        old = self.root / "media" / "OLDONION"
        make_card(old, "Onion")
        for index in range(6):
            (old / "BIOS" / f"f{index}.bin").write_bytes(b"x")
        new = self.root / "media" / "ONION"
        make_card(new, "Onion")
        shutil.rmtree(new / "BIOS")
        with mock.patch.object(install, "_mounted_cards", return_value=[old, new]), \
                mock.patch.object(install, "detect_platforms", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            setups, _mounted = install.found_setups("linux")
        self.assertEqual([label for _plat, _path, label in setups],
                         ["Onion SD card (ONION, no BIOS folder)",
                          "Onion SD card (OLDONION, 6 BIOS files)"])

    # tui_audit_11 P2-1: a list or a pack that records some axes only.

    def minui(self, plat: str = "minui", systems: bool = False, cores: bool = True,
              regions: bool = True) -> tuple[Path, Path, dict]:
        """A list of plat's shape (the MinUI family records cores and regions,
        no system), the pack built from it and the files' payloads."""
        from test_install_offline import GBA, JP, US  # noqa: PLC0415

        payloads = {"Bios/MGBA/gba_bios.bin": (GBA, "nintendo-gba", "mgba", None),
                    "Bios/PS/scph5501.bin": (US, "sony-playstation", "pcsx_rearmed",
                                             "north-america"),
                    "Bios/PS/scph5500.bin": (JP, "sony-playstation", "pcsx_rearmed", "japan")}
        files = []
        for dest, (data, system, core, region) in payloads.items():
            entry = {"dest": dest, "sha1": hashlib.sha1(data).hexdigest(), "size": len(data),
                     "repo_path": f"bios/{dest}", "cores": [core] if cores else None}
            if systems:
                entry["systems"] = [system]
            if regions and region:
                entry.update(regions=[region], region_groups=["ps"])
            files.append(entry)
        listing = self.root / f"{plat}.json"
        install_list = {"manifest_version": 2, "platform": plat, "files": files}
        listing.write_text(json.dumps(install_list))
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(REPO_ROOT / "scripts"))
        import generate_pack  # noqa: PLC0415

        pack_list = {"files": [{"path": dest, "sha1": entry["sha1"], "size": entry["size"]}
                               for dest, entry in zip(payloads, files)]}
        generate_pack.annotate_pack_systems(pack_list, generate_pack.pack_systems(install_list))
        name = {"minui": "MinUI", "misterfpga": "MiSTer_FPGA"}[plat]
        pack = self.root / f"{name}_v1_BIOS_Pack.zip"
        with zipfile.ZipFile(pack, "w") as archive:
            for dest, (data, *_rest) in payloads.items():
                archive.writestr(dest, data)
            archive.writestr("manifest.json", json.dumps(pack_list))
        return listing, pack, pack_list

    def narrowed(self, plat: str, **given) -> tuple[int, str, list]:
        """(exit code, what was said, the files kept) of a --dry-run narrowed
        as given."""
        dest = self.root / "dest"
        if not dest.exists():
            make_card(dest, "MinUI")
        stream = io.StringIO()
        code = 0
        with mock.patch.object(install.EVENTS, "stream", stream), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                install._install(_args(platform=plat, dest=dest, dry_run=True, events=True,
                                       no_input=True, declared_only=False, **given),
                                 io.StringIO())
            except SystemExit as stop:
                code = stop.code
            finally:
                install.LOCAL.reset()
        kept = [json.loads(line)["dest"] for line in stream.getvalue().splitlines()
                if '"event": "file"' in line]
        return code, out.getvalue() + err.getvalue(), kept

    def test_a_list_without_systems_narrows_by_what_it_records(self):
        """tui_audit_11 P2-1: install/minui.json records cores and regions,
        no system, and all three options were refused with a remedy that
        fetched the same list."""
        listing, _pack, _listed = self.minui()
        code, text, kept = self.narrowed("minui", manifest=listing, core=["mgba"])
        self.assertEqual(code, 0, text)
        self.assertEqual(kept, ["Bios/MGBA/gba_bios.bin"])
        code, text, kept = self.narrowed("minui", manifest=listing, region=["us"])
        self.assertEqual(code, 0, text)
        self.assertNotIn("Bios/PS/scph5500.bin", kept)
        code, text, _kept = self.narrowed("minui", manifest=listing,
                                          system=["nintendo-gba"])
        self.assertEqual(code, 1)
        self.assertIn("records no systems, so --system cannot narrow it", text)
        self.assertIn("Run it without --system, or narrow with --core and --region", text)
        self.assertNotIn("one-line installer", text)
        # The remedy followed: what it names works.
        code, text, _kept = self.narrowed("minui", manifest=listing, core=["mgba"],
                                          region=["us"])
        self.assertEqual(code, 0, text)

    def test_a_pack_names_its_list_only_when_that_list_records_it(self):
        """tui_audit_11 P2-1: a MinUI pack sent --core to install/minui.json,
        refused the same way, and a MiSTer pack to install/misterfpga.json,
        which records no core. The pack now records what its list narrows
        by, and each remedy changes the outcome."""
        listing, pack, listed = self.minui()
        self.assertEqual(listed["list_records"], ["cores", "regions"])
        code, text, _kept = self.narrowed("minui", from_pack=[pack], core=["mgba"])
        self.assertEqual(code, 1)
        self.assertIn("Add --manifest FILE, a copy of install/minui.json, which records them",
                      text)
        code, text, kept = self.narrowed("minui", from_pack=[pack], manifest=listing,
                                         core=["mgba"])
        self.assertEqual((code, kept), (0, ["Bios/MGBA/gba_bios.bin"]), text)
        mister_list, mister, listed = self.minui("misterfpga", systems=True, cores=False,
                                                 regions=False)
        self.assertEqual(listed["list_records"], ["regions", "systems"])
        code, text, _kept = self.narrowed("misterfpga", from_pack=[mister], core=["x"])
        self.assertEqual(code, 1)
        self.assertIn("MiSTer FPGA's own file list records no cores either. Run it without "
                      "--core, or narrow with --system.", text)
        self.assertNotIn("--manifest", text)
        code, text, kept = self.narrowed("misterfpga", from_pack=[mister],
                                         system=["nintendo-gba"])
        self.assertEqual((code, kept), (0, ["Bios/MGBA/gba_bios.bin"]), text)
        self.assertTrue(mister_list.exists())

    def test_line_mode_offers_what_can_be_chosen(self):
        """tui_audit_11 P3-10: "c) choose systems, cores and regions" with a
        pack that records systems only, and "Cores: 1) azahar" asked with
        one answer."""
        _listing, _pack, listed = self.minui("misterfpga", systems=True, cores=False,
                                             regions=False)
        files = [{"dest": f["path"], "sha1": f["sha1"], "size": f["size"],
                  "repo_path": "x", "systems": f["systems"]} for f in listed["files"]]
        self.assertEqual(install.narrowing_words(files, []), "systems")
        listing, _pack, _listed = self.minui()
        minui = json.loads(listing.read_text())["files"]
        self.assertEqual(install.narrowing_words(minui, []), "cores and regions")
        one_core = [{**entry, "systems": ["nintendo-3ds"], "cores": ["azahar"]}
                    for entry in minui[:1]]
        with mock.patch.object(install, "_ask", side_effect=["", ""]) as asked, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            install._choose_narrowing(one_core, [], {}, None)
        self.assertNotIn("Cores:", out.getvalue())
        self.assertEqual(asked.call_count, 0)

    def test_the_card_marker_table_is_written_from_the_rules(self):
        """code_audit_18 P3-6, tui_audit_11 P2-2: the table left out the
        SHARE drives. It is generated: every card rule and SHARE drive file
        is a row, about.info among them."""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(REPO_ROOT / "scripts"))
        import generate_installer_doc  # noqa: PLC0415

        page = (REPO_ROOT / "wiki" / "installer.md").read_text(encoding="utf-8")
        block = page[page.index("<!-- generated: card-markers -->"):
                     page.index("<!-- /generated: card-markers -->")]
        for rule in install._card_rules():
            self.assertIn(f"`{rule.values[0]}", block)
        for _plat, marker in install._SHARE_DRIVES:
            self.assertIn(f"`{marker}`", block)
        self.assertIn(self.ABOUT, block)
        self.assertIn("Recalbox SHARE drive", block)
        self.assertEqual(generate_installer_doc.render(page), page)

class TheAndroidResultReadsWhole(unittest.TestCase):
    """A run on Android 17 under Termux: the result named /sdcard and
    /storage/emulated/0 for one storage, saves/<core> read as a folder named
    <core>, a backup path and the restore command were split inside a word,
    and the game data line named no folder."""

    def setUp(self):
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for patcher in (mock.patch.dict(install._SPELLED, {}, clear=True),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_one_storage_is_named_one_way(self):
        real = self.root / "storage" / "emulated" / "0"
        (real / "RetroArch" / "system").mkdir(parents=True)
        link = self.root / "sdcard"
        link.symlink_to(real)
        bios = link / "RetroArch" / "system"
        self.assertEqual(install._destination_path(bios, "a.bin"),
                         real.resolve() / "RetroArch" / "system" / "a.bin")
        kept = install.KeptFiles()
        run = kept.plan(install._kept_slot(bios, "a.bin")[0], 3)
        folder = kept.summary()[0]["folder"]
        self.assertTrue(folder.startswith(str(link / "RetroArch" / "retrobios-backup")),
                        folder)
        self.assertEqual(install.shown_path(run), folder)
        self.assertNotIn(str(real), install.shown_path(real.resolve() / "RetroArch" / "saves"))

    def test_a_folder_per_core_is_said_in_words(self):
        where = "/sdcard/RetroArch/saves/<core>"
        line = install.saves_line("RetroArch", 106, where)
        self.assertEqual(line, "106 files read from RetroArch's save directory go to a "
                               "folder per core under /sdcard/RetroArch/saves")
        self.assertEqual(install.save_where_words("/s/saves/<system>"),
                         ("a folder per system under", "/s/saves"))
        state = checked_state()
        screen = install._saves_lines(state, {"files": 106, "folder": where})
        self.assertNotIn("<core>", " ".join(screen))
        self.assertIn("a folder per core under:", screen[0])

    def result(self, cols: int) -> list:
        state = _installed_state(Path("/sdcard/RetroArch/system"))
        state.summary["kept"] = [{"folder": "/sdcard/RetroArch/retrobios-backup/"
                                            "20261027-223524", "files": 3, "bytes": 10}]
        state.cols, state.rows = cols, 40
        return install.tui_view(state, cols, 40)

    def test_a_path_or_a_command_is_never_split_inside_a_word(self):
        for cols in (40, 50, 80):
            with self.subTest(cols=cols):
                lines = [str(line).strip() for line in self.result(cols)]
                self.assertIn("/sdcard/RetroArch/retrobios-backup/20261027-223524", lines)
                words = [word for line in lines for word in line.split()]
                self.assertIn("--restore", words)
                self.assertTrue(all(not word.endswith("/") for word in words), words)
        state = _installed_state(Path("/sdcard/RetroArch/system"))
        command = install._styled("curl -fsSL https://raw.githubusercontent.com/Abdess/"
                                  "retrobios/main/install.sh | sh -s -- --restore "
                                  "'/storage/emulated/0/a b/20261027-223524'", "command")
        state.stack.append("result")
        pieces = install._text_lines([command], 38, whole=True)
        joined = [str(piece).strip() for piece in pieces if piece.style != "cont"]
        self.assertIn("https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh",
                      joined)
        self.assertIn("'/storage/emulated/0/a b/20261027-223524'", joined)

    def test_a_long_line_folds_in_the_terminal(self):
        terminal = install._TuiTerminal.__new__(install._TuiTerminal)
        terminal.frame, written = [], []
        terminal.write = written.append
        long = install._styled("x" * 50, "long")
        terminal.draw([long, install._styled("", "cont"), install._styled("next")], 40)
        out = "".join(written)
        self.assertIn("x" * 50, out)
        self.assertNotIn("\x1b[2;1H", out)
        self.assertIn("\x1b[3;1H", out)

    def test_game_data_names_the_folder_it_goes_in(self):
        entry = {"dest": "jazz2/Source/Anims.j2a", "required": True, "category": "game_data",
                 "system": "jazz-jackrabbit-2"}
        bios = Path("/sdcard/RetroArch/system")
        lines = install._omission_lines(1, [entry], bios)
        said = " ".join(lines)
        self.assertIn(f"into {bios / 'jazz2' / 'Source'}{os.sep}", said)
        self.assertIn("files a game or a game engine reads", said)
        self.assertNotIn("Needed to start an emulator", said)


class NothingFromOutsideReachesTheTerminalRaw(unittest.TestCase):
    """code_audit_18 P2-1: a pack's member path, a --manifest dest, the
    names a list records, a card's label and a retroarch.cfg folder reached
    line mode, the events and the disk with their control characters. Each
    source is fed escape sequences, and none comes out raw."""

    OSC = "\x1b]0;PWNED\x07"
    CSI = "\x1b[41m"

    def setUp(self):
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=REPO_ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        install.LOCAL.reset()
        self.addCleanup(install.LOCAL.reset)
        install._UNNAMEABLE.clear()
        self.addCleanup(install._UNNAMEABLE.clear)
        self.dest = self.root / "dest"
        self.dest.mkdir()

    def assertClean(self, text: str, where: str) -> None:
        self.assertNotIn("\x1b", text, where)
        self.assertNotIn("\x07", text, where)

    def assertNothingRawOnDisk(self) -> None:
        for path in self.root.rglob("*"):
            self.assertIsNone(install._CONTROLS.search(path.name), path)

    def run_install(self, args: argparse.Namespace, mounts: list = (),
                    env: dict | None = None) -> str:
        events = io.StringIO()
        with mock.patch.object(install, "_mounted_cards", return_value=list(mounts)), \
                mock.patch.object(install, "_mount_points", return_value=frozenset()), \
                mock.patch.object(install, "detect_frontends", return_value=[]), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch.object(install.EVENTS, "stream", events), \
                mock.patch.dict(os.environ, {install.OFFLINE_VARIABLE: "1", **(env or {})}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.suppress(SystemExit):
            install._install(args, io.StringIO())
        return out.getvalue() + err.getvalue() + events.getvalue()

    def test_a_pack_member_path_is_refused(self):
        from test_install_offline import _pack, GBA  # noqa: PLC0415

        pack = _pack(self.root, "RetroArch_v1_BIOS_Pack.zip", {f"x{self.OSC}.bin": GBA})
        text = self.run_install(_args(platform="retroarch", dest=self.dest,
                                      from_pack=[pack], no_input=True, declared_only=False))
        self.assertClean(text, "pack")
        self.assertIn("is not valid: unsafe", text)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertNothingRawOnDisk()

    def test_a_list_dest_and_its_names_are_refused_or_shown(self):
        files = [{"dest": f"x{self.CSI}BAD.bin", "sha1": "0" * 40, "size": 1,
                  "repo_path": "bios/x", "cores": None, "systems": ["a"]}]
        listing = self.root / "list.json"
        listing.write_text(json.dumps({"manifest_version": 2, "platform": "retroarch",
                                       "files": files}))
        text = self.run_install(_args(platform="retroarch", dest=self.dest, manifest=listing,
                                      no_input=True, dry_run=True, verbose=True,
                                      declared_only=False))
        self.assertClean(text, "--manifest dest")
        self.assertIn("unsafe", text)
        named = {"manifest_version": 2, "platform": "retroarch", "files": [
            {**files[0], "dest": "a.bin", "systems": [f"a{self.CSI}"],
             "cores": [f"core{self.OSC}"]}],
            "system_titles": {f"a{self.CSI}": f"Title{self.OSC}"}}
        cleaned = install._validate_manifest(named, "retroarch")
        self.assertClean(json.dumps(cleaned, ensure_ascii=False), "names")
        self.assertNothingRawOnDisk()

    def test_a_card_label_is_shown_and_never_obeyed(self):
        card = self.root / "media" / f"S{self.CSI}RED"
        (card / "system").mkdir(parents=True)
        (card / "system" / "batocera.conf").write_text("system.hostname=BATOCERA\n")
        listing = self.root / "batocera.json"
        listing.write_text(json.dumps({"manifest_version": 2, "platform": "batocera",
                                       "files": []}))
        with mock.patch.object(install, "detect_platforms", return_value=[]):
            text = self.run_install(_args(platform="batocera", manifest=listing,
                                          no_input=True, dry_run=True, events=True,
                                          declared_only=False), [card])
        self.assertIn("S?[41mRED", text)
        self.assertClean(text, "card label")
        self.assertClean(install.card_line(card), "card row")

    def test_a_folder_a_config_names_is_refused(self):
        home = self.root / "home"
        weird = home / f"sys{self.OSC}x"
        weird.mkdir(parents=True)
        cfg = home / ".config" / "retroarch" / "retroarch.cfg"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(f'system_directory = "{weird}"\n')
        listing = self.root / "retroarch.json"
        listing.write_text(json.dumps({"manifest_version": 2, "platform": "retroarch",
                                       "files": []}))
        text = self.run_install(
            _args(manifest=listing, no_input=True, dry_run=True, events=True,
                  declared_only=False),
            env={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                 "RETROBIOS_OS": "linux"})
        self.assertClean(text, "retroarch.cfg")
        self.assertIn("holds a control character", text)
        self.assertNotIn("using default path", text)
        self.assertNotIn(str(home / ".config" / "retroarch" / "system"), text)

    def test_a_path_with_a_control_character_is_no_relative_path(self):
        for value in ("a\x1bb.bin", "a\x07.bin", "a\x9bb.bin", "a\nb.bin"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                install._safe_relative_path(value, "dest")
        self.assertEqual(install.shown_path(Path("/a/b\x1b[2Jc")), "/a/b?[2Jc")

if __name__ == "__main__":
    unittest.main()
