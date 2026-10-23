"""BIOS files the user already has, found by their contents.

A user who wiped the card a handheld came with asks where its BIOS files went,
and one who copied the folder from a Mac, unzipped a pack into bios/bios/ or
renamed scph1001.bin in capitals read "missing" for a file sitting in the
folder. --from FOLDER copies what an old card, a backup or a Downloads folder
holds, and an install puts the files it finds misplaced in the BIOS folder
where the cores open them. Every file is recognised by its hash, copied and
never moved, and checked again once written.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import install  # noqa: E402
from test_install_offline import (  # noqa: E402
    EXTRA, GBA, JP, PAYLOAD, US, Scratch, _entry, _offline_env,
)
from test_install_selection import FakeRepository  # noqa: E402


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    """Every file below folder, with its bytes and modification time."""
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in folder.rglob("*") if p.is_file() and not p.is_symlink()}


def _events(proc: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]


class FromFolder(Scratch):
    def setUp(self):
        super().setUp()
        self.old = self.root / "old card"
        self.old.mkdir()

    def keep(self, relative: str, data: bytes) -> Path:
        path = self.old / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def install(self, *options: str) -> subprocess.CompletedProcess:
        return self.run_installer("--manifest", str(self.list), "--from", str(self.old),
                                  "--no-input", *options)

    def installed(self) -> dict[str, bytes]:
        if not self.dest.exists():
            return {}
        return {p.relative_to(self.dest).as_posix(): p.read_bytes()
                for p in self.dest.rglob("*") if p.is_file()}


class CopyFromAnOldCard(FromFolder):
    def test_files_in_nested_folders_and_under_other_names_are_copied(self):
        self.keep("BIOS/psx/SCPH5501.BIN", US)
        self.keep("Roms/psx/japan.bin", JP)
        self.keep("a/b/c/gba.rom", GBA)
        self.keep("pcsx/extra.bin", EXTRA)
        before = _snapshot(self.old)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), PAYLOAD)
        self.assertIn(f"4 files copied from {self.old}.", proc.stdout)
        self.assertEqual(_snapshot(self.old), before, "the old card is only read")

    def test_members_of_a_zip_are_copied(self):
        _zip(self.old / "Downloads" / "psx bios.zip",
             {"PSX/scph5501.bin": US, "PSX/scph5500.bin": JP, "readme.txt": b"hi"})
        self.keep("gba_bios.bin", GBA)
        self.keep("pcsx/extra.bin", EXTRA)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), PAYLOAD)

    def test_a_zip_given_alone_is_read_as_a_folder(self):
        archive = _zip(self.root / "backup.zip", {f"system/{k}": v for k, v in PAYLOAD.items()})
        proc = self.run_installer("--manifest", str(self.list), "--from", str(archive),
                                  "--no-input")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), PAYLOAD)
        self.assertIn(f"4 files copied from {archive}.", proc.stdout)

    def test_an_archive_the_list_wants_whole_is_copied_whole(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("sp-s2.sp1", b"neo geo bios" * 10)
        neogeo = inner.getvalue()
        self.list.write_text(json.dumps({
            "manifest_version": 2, "platform": "retroarch",
            "files": [_entry("neogeo.zip", neogeo, ["snk-neogeo"]),
                      _entry("fbneo/neogeo.zip", neogeo, ["snk-neogeo"])],
        }))
        self.keep("arcade/neogeo (1).zip", neogeo)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), {"neogeo.zip": neogeo, "fbneo/neogeo.zip": neogeo})

    def test_an_archive_inside_an_archive_is_copied(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("sp-s2.sp1", b"neo geo bios" * 10)
        neogeo = inner.getvalue()
        self.list.write_text(json.dumps({
            "manifest_version": 2, "platform": "retroarch",
            "files": [_entry("neogeo.zip", neogeo, ["snk-neogeo"])],
        }))
        _zip(self.old / "RetroArch_BIOS_Pack.zip", {"neogeo.zip": neogeo})
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), {"neogeo.zip": neogeo})

    def test_an_empty_file_and_a_same_size_file_are_not_taken(self):
        """"bios folder is 0kb" after flashing Garlic: a name is no proof."""
        self.keep("scph5501.bin", b"")
        self.keep("gba_bios.bin", b"x" * len(GBA))
        self.keep("scph5500.bin", JP)
        self.keep("pcsx/extra.bin", EXTRA)
        proc = self.install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), {"scph5500.bin": JP, "pcsx/extra.bin": EXTRA})
        self.assertIn(f"2 files copied from {self.old}, 2 still missing.", proc.stdout)

    def test_a_link_out_of_the_folder_is_not_followed(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "scph5501.bin").write_bytes(US)
        (outside / "gba").mkdir()
        (outside / "gba" / "gba_bios.bin").write_bytes(GBA)
        try:
            (self.old / "scph5501.bin").symlink_to(outside / "scph5501.bin")
            (self.old / "gba").symlink_to(outside / "gba", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system makes no symbolic link")
        self.keep("scph5500.bin", JP)
        proc = self.install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), {"scph5500.bin": JP})

    def test_a_link_inside_the_folder_is_read(self):
        self.keep("real/scph5501.bin", US)
        try:
            (self.old / "scph5501.bin").symlink_to(self.old / "real" / "scph5501.bin")
        except (OSError, NotImplementedError):
            self.skipTest("this system makes no symbolic link")
        index = install.FolderIndex(self.old)
        self.assertEqual(len(index.by_size[len(US)]), 2)

    def test_mac_and_system_files_are_never_read(self):
        """A Mac writes ._name beside every file it copies to a FAT card,
        and Spotlight keeps an index there: neither is a file a person put."""
        self.keep("._scph5501.bin", US)
        self.keep(".Spotlight-V100/Store-V2/scph5500.bin", JP)
        self.keep("System Volume Information/gba_bios.bin", GBA)
        self.keep("pcsx/extra.bin", EXTRA)
        proc = self.install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), {"pcsx/extra.bin": EXTRA})

    def test_the_rest_is_downloaded_and_the_summary_says_where_each_came_from(self):
        with FakeRepository() as repo:
            old = repo.root / "old"
            payloads = FakeRepository.PAYLOADS
            (old / "BIOS").mkdir(parents=True)
            (old / "BIOS" / "SCPH5500.BIN").write_bytes(payloads["scph5500.bin"][0])
            (old / "gba.bin").write_bytes(payloads["gba_bios.bin"][0])
            (repo.root / "bios" / "mgba_extra.bin").unlink()
            proc = repo.run("--from", str(old), "--no-input")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn(f"2 files copied from {old}, 2 downloaded, 1 still missing.",
                          proc.stdout)
            self.assertEqual(sorted(repo.installed()), sorted(
                name for name in payloads if name != "mgba/extra.bin"))

    def test_two_folders_are_read_in_turn(self):
        second = self.root / "backup"
        (second / "x").mkdir(parents=True)
        (second / "x" / "gba_bios.bin").write_bytes(GBA)
        (second / "scph5501.bin").write_bytes(US)
        self.keep("scph5501.bin", US)
        self.keep("scph5500.bin", JP)
        self.keep("pcsx/extra.bin", EXTRA)
        proc = self.install("--from", str(second))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.installed(), PAYLOAD)
        self.assertIn(f"3 files copied from {self.old}, 1 copied from {second}.", proc.stdout)

    def test_check_names_what_the_folder_would_give_and_writes_nothing(self):
        self.keep("BIOS/SCPH5501.BIN", US)
        self.keep("gba_bios.bin", GBA)
        proc = self.run_installer("--check", "--manifest", str(self.list), "--from",
                                  str(self.old))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"{self.old} holds 2 of these files: a run without --check "
                      "copies them.", proc.stdout)
        self.assertIn(f"in {self.old}: BIOS/SCPH5501.BIN", proc.stdout)
        self.assertFalse(self.dest.exists())

    def test_dry_run_counts_the_copies_and_writes_nothing(self):
        self.keep("BIOS/SCPH5501.BIN", US)
        self.keep("gba_bios.bin", GBA)
        proc = self.install("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"Would copy 2 files ({install.format_size(len(US) + len(GBA))}) "
                      f"from {self.old}", proc.stdout)
        self.assertIn("Would download 2 files", proc.stdout)
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.root / install.BACKUP_DIR).exists())

    def test_a_folder_that_does_not_exist_is_refused(self):
        proc = self.run_installer("--manifest", str(self.list), "--from",
                                  str(self.root / "nowhere"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("does not exist", proc.stderr)
        self.assertFalse(self.dest.exists())

    def test_a_listing_refuses_the_option(self):
        proc = self.run_installer("--manifest", str(self.list), "--from", str(self.old),
                                  "--list-systems")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--from", proc.stderr)

    def test_restore_refuses_the_option(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--restore", str(self.old),
             "--from", str(self.old)],
            env=_offline_env(), capture_output=True, text=True, timeout=60, check=False,
            stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("takes no --from", proc.stderr)


class CaseTwinsOnAFolderThatFoldsCase(FromFolder):
    """An ext4 card holds font.bin and FONT.BIN, two files. On an exFAT
    card or a Windows folder they are one name: the copy kept is the one
    the list installs, found by its hash, never the other by its name."""

    def test_the_hash_decides_which_twin_lands(self):
        first, second = b"first font" * 9, b"second font" * 9
        self.list.write_text(json.dumps({
            "manifest_version": 2, "platform": "retroarch",
            "files": [_entry("sys/font.bin", first, ["x"]),
                      _entry("SYS/FONT.BIN", second, ["x"])],
        }))
        self.keep("sys/FONT.BIN", first)
        self.keep("sys/font.bin", second)
        out = io.StringIO()
        argv = ["install.py", "--platform", "retroarch", "--dest", str(self.dest),
                "--manifest", str(self.list), "--from", str(self.old), "--no-input"]
        with mock.patch.object(install, "keeps_case", return_value=False), \
                mock.patch.object(install, "detect_os", return_value="linux"), \
                mock.patch("sys.argv", argv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                install.main()
            except SystemExit as exc:
                self.assertIn(exc.code, (0, None), out.getvalue())
        self.addCleanup(install.LOCAL.reset)
        self.assertEqual(self.installed(), {"sys/font.bin": first})
        self.assertIn("1 file copied from", out.getvalue())


class EventsForAFolder(FromFolder):
    def test_each_file_found_is_an_event_before_it_is_copied(self):
        self.keep("BIOS/SCPH5501.BIN", US)
        _zip(self.old / "gba.zip", {"gba_bios.bin": GBA})
        proc = self.install("--events")
        events = _events(proc)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        for event in events:
            self.assertEqual(list(event), ["event", *install.EVENT_KEYS[event["event"]]])
        found = sorted((e for e in events if e["event"] == "found"), key=lambda e: e["dest"])
        self.assertEqual(found, [
            {"event": "found", "platform": "retroarch", "dest": "gba_bios.bin",
             "folder": str(self.old), "path": str((self.old / "gba.zip").resolve()),
             "member": "gba_bios.bin", "reason": ""},
            {"event": "found", "platform": "retroarch", "dest": "scph5501.bin",
             "folder": str(self.old),
             "path": str((self.old / "BIOS" / "SCPH5501.BIN").resolve()),
             "member": "", "reason": ""},
        ])
        kinds = [e["event"] for e in events]
        self.assertLess(kinds.index("found"), kinds.index("download"))
        copied = {e["dest"] for e in events if e["event"] == "downloaded" and e["ok"]}
        self.assertEqual(copied, {"gba_bios.bin", "scph5501.bin"})
        summary = next(e for e in events if e["event"] == "summary")
        self.assertEqual(summary["installed"], 2)

    def test_a_check_writes_the_same_events_and_nothing_else(self):
        self.keep("BIOS/SCPH5501.BIN", US)
        proc = self.run_installer("--check", "--events", "--manifest", str(self.list),
                                  "--from", str(self.old))
        events = _events(proc)
        self.assertEqual([e["dest"] for e in events if e["event"] == "found"],
                         ["scph5501.bin"])
        self.assertNotIn("download", [e["event"] for e in events])
        self.assertFalse(self.dest.exists())

    def test_a_folder_that_does_not_exist_is_an_error_event(self):
        proc = self.run_installer("--events", "--manifest", str(self.list), "--from",
                                  str(self.root / "nowhere"))
        events = _events(proc)
        error = next(e for e in events if e["event"] == "error")
        self.assertEqual((error["stage"], error["reason"]), ("from", "not_found"))
        self.assertEqual(events[-1], {"event": "end", "code": 1})


class TheInterfaceReadsAFoundEvent(unittest.TestCase):
    """The full-screen interface drives the engine through --events. A
    found event, which an install writes for a misplaced file even without
    --from, is what the plan counts after a check and what the download
    speed leaves out during an install. Elsewhere it changes nothing."""

    FOUND = {"event": "found", "platform": "retroarch", "dest": "scph5501.bin",
             "folder": "/bios", "path": "/bios/bios/scph5501.bin", "member": "",
             "reason": "nested folder: it is at bios/bios/scph5501.bin"}

    def receive(self, run: str) -> tuple:
        state = install.TuiState()
        state.run, state.run_id, state.running = run, 3, True
        before = copy.deepcopy(vars(state))
        self.assertEqual(install.tui_receive(state, ("event", 3, dict(self.FOUND))), [])
        return state, before

    def test_a_check_counts_it_and_an_install_copies_it(self):
        state, _before = self.receive("check")
        self.assertEqual(state.found, [self.FOUND])
        state, _before = self.receive("install")
        self.assertEqual(state.local_dests, {"scph5501.bin"})

    def test_a_dry_run_ignores_it(self):
        for run in ("confirm", "preview"):
            with self.subTest(run=run):
                state, before = self.receive(run)
                self.assertEqual(vars(state), before)


class MisplacedInTheBiosFolder(Scratch):
    """F12: the files are in the BIOS folder, at a place no core opens."""

    def install(self, *options: str) -> subprocess.CompletedProcess:
        return self.run_installer("--manifest", str(self.list), "--no-input", *options)

    def test_a_nested_bios_folder_is_fixed_and_left_in_place(self):
        for name, data in PAYLOAD.items():
            self.place(f"system/{name}", data)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name, data in PAYLOAD.items():
            self.assertEqual((self.dest / name).read_bytes(), data)
            self.assertEqual((self.dest / "system" / name).read_bytes(), data)
        self.assertIn("4 files fixed from copies already in the BIOS folder.", proc.stdout)
        self.assertIn("nested folder: it is at system/system/scph5501.bin", proc.stdout)

    def test_a_pack_extracted_into_a_folder_of_its_own_is_fixed(self):
        for name, data in PAYLOAD.items():
            self.place(f"RetroArch_BIOS_Pack/{name}", data)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("one folder too deep: it is at RetroArch_BIOS_Pack/scph5501.bin",
                      proc.stdout)
        self.assertEqual((self.dest / "pcsx" / "extra.bin").read_bytes(), EXTRA)

    def test_a_name_in_capitals_is_fixed_where_case_counts(self):
        probe = self.root / "CaseProbe"
        probe.write_text("x")
        if (self.root / "caseprobe").exists():
            self.skipTest("this filesystem does not keep case")
        self.complete("gba_bios.bin")
        self.place("GBA_BIOS.BIN", GBA)
        check = self.check()
        self.assertIn("case of the name: the folder holds GBA_BIOS.BIN, the cores open "
                      "gba_bios.bin", check)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.dest / "gba_bios.bin").read_bytes(), GBA)
        self.assertEqual((self.dest / "GBA_BIOS.BIN").read_bytes(), GBA)
        self.assertIn("1 file fixed from copies already in the BIOS folder.", proc.stdout)

    def test_a_file_left_in_its_zip_is_extracted(self):
        self.complete("gba_bios.bin")
        _zip(self.dest / "GBA BIOS.zip", {"bios/gba_bios.bin": GBA})
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.dest / "gba_bios.bin").read_bytes(), GBA)
        self.assertTrue((self.dest / "GBA BIOS.zip").is_file())

    def test_a_file_above_the_folder_is_fixed(self):
        self.complete("gba_bios.bin")
        (self.root / "gba_bios.bin").write_bytes(GBA)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.dest / "gba_bios.bin").read_bytes(), GBA)
        self.assertTrue((self.root / "gba_bios.bin").is_file())

    def test_a_wrong_file_in_place_is_kept_before_the_right_bytes_take_its_place(self):
        self.complete("gba_bios.bin")
        wrong = b"y" * len(GBA)
        self.place("gba_bios.bin", wrong)
        self.place("old/gba_bios.bin", GBA)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.dest / "gba_bios.bin").read_bytes(), GBA)
        kept = list((self.root / install.BACKUP_DIR).rglob("gba_bios.bin"))
        self.assertEqual([p.read_bytes() for p in kept], [wrong])

    def test_check_says_a_run_puts_it_in_place(self):
        for name, data in PAYLOAD.items():
            self.place(f"system/{name}", data)
        out = self.check()
        self.assertIn("4 of these files are in the BIOS folder at the wrong place: a run "
                      "without --check copies them where the cores open them, and leaves "
                      "your copies as they are.", out)
        self.assertFalse((self.dest / "scph5501.bin").exists())

    def test_dry_run_counts_them_and_writes_nothing(self):
        for name, data in PAYLOAD.items():
            self.place(f"system/{name}", data)
        before = _snapshot(self.dest)
        proc = self.install("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Would fix 4 files", proc.stdout)
        self.assertEqual(_snapshot(self.dest), before)

    def test_a_mac_file_alone_is_named(self):
        self.complete("gba_bios.bin")
        self.place("._gba_bios.bin", b"\x00\x05\x16\x07" + b"\x00" * 60)
        self.assertIn("only a Mac's ._ file: ._gba_bios.bin holds Finder data, not the "
                      "file itself", self.check())

    def test_a_copy_at_another_place_of_the_list_is_no_mistake(self):
        """np2kai/2608_bd.wav and np2/2608_bd.wav hold the same bytes: the
        second is copied from the first, and neither is called misplaced."""
        probe = self.root / "CaseProbe"
        probe.write_text("x")
        if (self.root / "caseprobe").exists():
            self.skipTest("this filesystem does not keep case")
        data = json.loads(self.list.read_text())
        twin = next(f for f in data["files"] if f["dest"] == "gba_bios.bin")
        data["case_variants"] = [{**twin, "dest": "twin/gba_bios.bin"}]
        self.list.write_text(json.dumps(data))
        self.complete()
        check = self.check()
        self.assertIn("a run without --check copies it from gba_bios.bin", check)
        self.assertNotIn("wrong place", check)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.dest / "twin" / "gba_bios.bin").read_bytes(), GBA)
        self.assertIn("1 file copied from another place of the BIOS folder.", proc.stdout)
        self.assertNotIn("fixed", proc.stdout)

    def test_a_folder_with_nothing_misplaced_says_nothing(self):
        with FakeRepository() as repo:
            proc = repo.run("--no-input")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("fixed", proc.stdout)
            self.assertNotIn("copied from", proc.stdout)


def _unprivileged(test: unittest.TestCase) -> None:
    if os.name != "posix" or os.geteuid() == 0:
        test.skipTest("a file of mode 000 is read here anyway")


class ACopyThatFails(FromFolder):
    """A copy found by the plan that cannot be read when it is copied:
    it read as "folder not writable", and a download failing after it lost
    the first reason."""

    def setUp(self):
        super().setUp()
        self.entry = json.loads(self.list.read_text())["files"][0]
        self.source = self.keep("old/scph5501.bin", US)
        self.copy = install.FolderIndex(self.old).find(self.entry)
        self.assertIsNotNone(self.copy)
        self.addCleanup(install.LOCAL.reset)
        self.dest.mkdir()

    def test_an_unreadable_source_falls_back_and_names_both_reasons(self):
        _unprivileged(self)
        self.source.chmod(0)
        self.addCleanup(self.source.chmod, 0o644)
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        with mock.patch.object(install, "_retry_delay", return_value=0), \
                mock.patch.object(install.urllib.request, "urlopen", side_effect=refused):
            dest, ok, reason = install._download_one(self.entry, self.dest, copy=self.copy)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(
            f"{self.source.resolve()} could not be read: Permission denied, and "), reason)
        self.assertNotIn(install.NOT_WRITABLE, reason)
        self.assertIsInstance(reason, install.NetworkReason)

    def test_a_read_error_is_the_source_and_not_the_folder(self):
        class Failing(io.BytesIO):
            def read(self, *_args):
                raise OSError(errno.EIO, "Input/output error")

        for source, words in (
                (lambda: Failing(b""), "Input/output error"),
                (mock.Mock(side_effect=PermissionError(errno.EACCES, "Permission denied")),
                 "Permission denied")):
            with self.subTest(words=words):
                result = install._write_verified(
                    self.entry, self.dest, self.dest / "scph5501.bin", source, None,
                    "the copy in E:\\", named="E:\\bios\\a.bin")
                self.assertEqual(result, ("scph5501.bin", False,
                                          f"E:\\bios\\a.bin could not be read: {words}"))
        self.assertEqual(list(self.dest.iterdir()), [], "no temporary file is left")

    def test_a_run_reading_a_pack_falls_back_to_the_pack_only(self):
        _unprivileged(self)
        self.source.chmod(0)
        self.addCleanup(self.source.chmod, 0o644)
        install.LOCAL.pack = mock.Mock(find=mock.Mock(return_value=None))
        with mock.patch.object(install.urllib.request, "urlopen",
                               side_effect=AssertionError("network")):
            _dest, ok, reason = install._download_one(self.entry, self.dest, copy=self.copy)
        install.LOCAL.pack = None
        self.assertFalse(ok)
        self.assertEqual(reason, f"{self.source.resolve()} could not be read: "
                                 f"Permission denied, and {install.NOT_IN_PACK}")


class ManySameSizedFiles(FromFolder):
    """A card of 16 KB ROMs and a list of 16 KB entries: each entry sorted
    and walked every file of its size."""

    def test_each_file_is_read_once_and_the_order_holds(self):
        for n in range(3000):
            self.keep(f"roms/r{n:04}.bin", b"%016d" % n)
            _zip(self.old / f"sets/s{n:04}.zip", {f"m{n}.bin": b"m%015d" % n})
        entries = [{"dest": f"e{n}.bin", "size": 16, "sha1": f"{n:040}"}
                   for n in range(3000)]
        wanted = [_entry("late.bin", b"%016d" % 2999, ["x"]),
                  _entry("m5.bin", b"m%015d" % 5, ["x"]),
                  _entry("r0007.bin", b"%016d" % 7, ["x"])]
        index = install.FolderIndex(self.old)
        start = time.monotonic()
        copies = install.plan_local_copies(entries + wanted, self.dest, None, [index])
        elapsed = time.monotonic() - start
        self.assertEqual({e["dest"]: copies[id(e)].where() for e in wanted if id(e) in copies}, {
            "late.bin": "roms/r2999.bin",
            "m5.bin": "sets/s0005.zip, inside as m5.bin",
            "r0007.bin": "roms/r0007.bin",
        })
        self.assertEqual(len(copies), 3)
        self.assertLess(elapsed, 30, f"{elapsed:.1f} s")

    def test_a_file_of_the_same_name_comes_first(self):
        self.keep("a/other.bin", US)
        self.keep("b/scph5501.bin", US)
        _zip(self.old / "c.zip", {"x/gba.bin": GBA})
        _zip(self.old / "d.zip", {"gba_bios.bin": GBA})
        index = install.FolderIndex(self.old)
        files = json.loads(self.list.read_text())["files"]
        copies = install.plan_local_copies(files, self.dest, None, [index])
        found = {e["dest"]: copies[id(e)].where() for e in files if id(e) in copies}
        self.assertEqual(found["scph5501.bin"], "b/scph5501.bin")
        self.assertEqual(found["gba_bios.bin"], "d.zip, inside as gba_bios.bin")


class TheSummaryLine(unittest.TestCase):
    def test_the_parts_in_words(self):
        line = install.sources_line([("E:\\", 38)], fixed=0, downloaded=4, missing=2)
        self.assertEqual(line, "38 files copied from E:\\, 4 downloaded, 2 still missing.")

    def test_one_file_and_fixes(self):
        self.assertEqual(install.sources_line([("/old", 1)], fixed=3, downloaded=0, missing=0),
                         "1 file copied from /old, 3 fixed from copies already in the "
                         "BIOS folder.")
        self.assertEqual(install.sources_line([], fixed=2, downloaded=1, missing=0),
                         "2 files fixed from copies already in the BIOS folder, "
                         "1 downloaded.")

    def test_nothing_copied_is_no_line(self):
        self.assertEqual(install.sources_line([("/old", 0)], 0, 5, 0), "")


if __name__ == "__main__":
    unittest.main()
