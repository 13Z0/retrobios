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
import shutil
import struct
import sys
import tempfile
import termios
import threading
import time
import types
import unittest
import warnings
from pathlib import Path, PureWindowsPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import install  # noqa: E402
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
        restore=None, from_pack=None, manifest=None, tui=False, plain=False,
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
        self.assertEqual(self.feed("\x00\x1a\x1cx"), keys("char:x"))

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
        self.assertEqual(press(state, "enter"), [])
        self.assertIn("No system is ticked", state.status)
        focus = next(i for i, row in enumerate(install.tui_rows(state))
                     if row.value == "nintendo-gba")
        state.focus["systems"] = focus
        press(state, "right")
        self.assertEqual(press(state, "enter"), [("run", "confirm")])
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
        press(state, "enter", "enter")
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
                         ["retry", "back", "quit"])
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
                         ["Make the window larger"])


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
    "gptokeyb": {"A", "B", "X", "Y", "Start+Select"},
    "gptokeyb_muxterm": {"A", "B", "X", "Y", "Start+Select"},
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
            "both": "B, Ctrl-C or Start+Select stops.", "keys": "Esc or Ctrl-C stops.",
            "gptokeyb": "B or Start+Select stops.", "evmapy": "B stops.",
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
        self.assertNotIn("Start+Select", help_text("evmapy"))
        keys = help_text("keys")
        for words in ("Enter", "Esc, Backspace", "o, Tab"):
            self.assertIn(words, keys)
        for button in ("A/Enter", "B/Esc", "L1", "Start", "Y,"):
            self.assertNotIn(button, keys)
        muxterm = help_text("muxterm")
        for words in ("Start, Enter", "B, Esc, Backspace", "Keyboard on or off",
                      "Close the terminal"):
            self.assertIn(words, muxterm)
        self.assertNotIn("Start+Select", muxterm)
        # gptokeyb2 owns the pad inside muxterm, which keeps PgUp and PgDn
        # for its scrollback: the gptokeyb buttons, and no page key.
        in_muxterm = help_text("gptokeyb_muxterm", "preview")
        for words in ("A, Start, Enter", "Select, then Start"):
            self.assertIn(words, in_muxterm)
        self.assertNotIn("L1/R1", in_muxterm)
        self.assertIn("L1/R1", help_text("gptokeyb", "preview"))
        # muxterm keeps PgUp and PgDn for its scrollback, and L1 and R1
        # send nothing: no paging is offered there, on a list or a pager.
        for screen in ("systems", "result", "plan"):
            with self.subTest(screen=screen):
                text = help_text("muxterm", screen)
                for words in ("L1", "R1", "PgUp", "PgDn", "Page"):
                    self.assertNotIn(words, text)
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
            self.assertIn("3 files to download, 600 B", text)
            self.assertIn("For your 1 of 3 systems", text)

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
                         ["retry", "back", "quit"])

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
        self.assertEqual([row.value for row in install.tui_rows(state)][1:], ["retroarch"])

    def test_p2_13_a_long_preview_wraps_only_what_is_in_view(self):
        state = checked_state()
        state.stack += ["options", "preview"]
        state.dry = dict(CHECK)
        state.dry_files = [{**FILES[0], "dest": f"file{n:05d}.bin"} for n in range(8000)]
        state.cols, state.rows = 40, 15
        real = install._text_lines
        calls = []

        def counted(items, width):
            calls.append(len(items))
            return real(items, width)

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
        self.assertIn("1 file is in the wrong place, it will be copied where the emulators "
                      "read it.", text)
        row = next(i for i, r in enumerate(install.tui_rows(state)) if r.action == "misplaced")
        state.focus["plan"] = row
        press(state, "enter")
        self.assertEqual(state.screen, "misplaced")
        self.assertIn("scph5500.bin: nested folder: bios/scph5500.bin",
                      "\n".join(install.tui_view(state, 80, 24)))
        press(state, "esc")
        self.assertEqual(state.screen, "plan")

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
        # Batocera's share and a KNULLI board seeded with its name read alike.
        self.assertIsNone(install.card_system(self.card({
            "system/batocera.conf": "system.hostname=BATOCERA\n"})))

    def test_p1_1_the_card_rules_are_the_device_rules(self):
        # The folder and the marker come from the rule the device reads, so
        # they are never written twice.
        rules = {rule.name: rule for rule in install._card_rules()}
        self.assertLessEqual({"muOS", "Onion", "spruceOS", "CrossMix-OS", "Allium", "Koriki",
                              "KNULLI", "ROCKNIX"}, set(rules))
        device = {rule.name: rule for rule in install._EMBEDDED_RULES
                  if rule.values and rule.values[0].startswith(install._CARD_MOUNTS)}
        for name, rule in device.items():
            if rule.bios and rule.manifest:
                with self.subTest(system=name):
                    self.assertTrue(rule.bios.endswith("/" + rules[name].bios))
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
        self.assertIn(f"{root} is not a muOS card: it holds no MUOS.", proc.stderr)
        self.assertIn("--dest <card>/MUOS/bios", proc.stderr)
        self.assertEqual(list(root.iterdir()), [root / "roms"])
        # Its BIOS folder named outright is taken as given.
        self.assertEqual(install.card_dest("muos", "retroarch", root / "MUOS" / "bios"),
                         ("retroarch", root / "MUOS" / "bios"))
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
        self.assertIn("is not a muOS card", err.getvalue())
        self.assertEqual(install.card_dest("muos", "retroarch", root / "muos" / "BIOS"),
                         ("retroarch", root / "muos" / "BIOS"))

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
        self.assertEqual(lines[1], "2 files copied from OLD, 1 fixed from copies "
                                   "already in the BIOS folder, 1 downloaded, 1 still missing.")
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
        self.assertIn("2 of these files are shared with other systems, counted once", text)
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
        self.assertIn("2 files on your old card could not be read.", text)
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

    def test_any_other_screen_closes_with_0(self):
        state = fresh_state()
        state.stack = ["setup"]
        self.assertEqual(self.idle_close(state), 0)

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
                "not known. Name it with --save-dir.")
        plan = " ".join(" ".join(install.tui_view(state, 120, 40)).split())
        self.assertIn(line, plan)
        state.stack.append("result")
        state.code = 0
        state.summary = {**summary, "mode": "install", "installed": 5}
        self.assertIn(line, install.tui_outcome(state)[1])


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
                for name in names.values():
                    self.assertIn(name, text)
                self.assertNotIn("...", text)
                for line in text.splitlines():
                    self.assertLessEqual(install.cell_width(line), cols)
        # Wide, the full form stays.
        self.assertIn("3 files, 30.0 MB + 27 shared",
                      "\n".join(install.tui_view(state, 120, 40)))


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
    LINE = "1 file read from RetroArch's save directory goes to ~/MySaves/<core>"

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

    def __init__(self, args: list[str], env: dict, cols: int = 80, rows: int = 24) -> None:
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.raw = b""
        with warnings.catch_warnings():
            # The child replaces itself at once: the fork-with-threads hazard
            # (a lock held by the loopback server thread) never reaches it.
            warnings.simplefilter("ignore", DeprecationWarning)
            self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(sys.executable, [sys.executable, str(REPO_ROOT / "install.py"), *args],
                      env)
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
                    self.assertIn("1 file read from RetroArch's save directory goes to "
                                  "~/MySaves", screen)
                    term.send("\r")
                    screen = term.wait("6 files installed.")
                    self.assertIn("save directory goes to ~/MySaves", screen)
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
        # D-pad and A only: up to None, A, down to the second system, Right.
        term.send(UP, UP, "\r", DOWN, DOWN, DOWN, RIGHT)
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
        self.assertEqual(term.close(), 0)
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
        self.assertEqual(term.close(), 0)
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
        term.wait("Make the window larger")
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
                term.wait("Enter) install everything")
                term.send("q\r")
                term.close()
                self.assertNotIn(b"\x1b[?1049h", term.raw)
                for name in env:
                    self.env.pop(name)

    def test_dumb_terminal_writes_no_escape(self):
        self.repo()
        self.env["TERM"] = "dumb"
        term = self.start()
        term.wait("Enter) install everything")
        term.send("q\r")
        term.close()
        self.assertNotIn(b"\x1b", term.raw)


if __name__ == "__main__":
    unittest.main()
