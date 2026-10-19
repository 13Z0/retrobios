"""What the one-liner shows and how it recovers.

A live progress line with speed and time left, a plan before a large
download, a dry run, a quiet mode, a version, a pause between retries that
honours Retry-After, and large files that resume where they stopped.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import install  # noqa: E402
from test_install_selection import FakeRepository  # noqa: E402

PAYLOAD = bytes(range(256)) * 400  # 102400 bytes


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD with Range support and fails first when asked to."""

    ranges: list[str] = []
    fail_first: list[int] = []

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802, D102
        if self.fail_first:
            code = self.fail_first.pop(0)
            self.send_response(code)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        header = self.headers.get("Range")
        self.ranges.append(header or "")
        start = 0
        if header and header.startswith("bytes="):
            start = int(header[6:].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            self.send_response(200)
        body = PAYLOAD[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __enter__(self):
        _RangeHandler.ranges = []
        _RangeHandler.fail_first = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        self.patch = mock.patch.object(install, "RAW_FILE_URL", f"http://127.0.0.1:{port}/{{path}}")
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.httpd.shutdown()
        self.httpd.server_close()


ENTRY = {
    "dest": "big.bin", "repo_path": "bios/big.bin", "size": len(PAYLOAD),
    "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
}


class LargeFilesResume(unittest.TestCase):
    def test_a_partial_copy_is_completed_with_a_range_request(self):
        with _Server(), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(install, "RESUME_FROM_SIZE", 1024):
            part = Path(tmp) / ".big.bin.part"
            part.write_bytes(PAYLOAD[:40000])
            dest, ok, reason = install._download_one(dict(ENTRY), Path(tmp))
            self.assertEqual((dest, ok, reason), ("big.bin", True, ""))
            self.assertEqual((Path(tmp) / "big.bin").read_bytes(), PAYLOAD)
            self.assertEqual(_RangeHandler.ranges, ["bytes=40000-"])
            self.assertFalse(part.exists())

    def test_a_small_file_starts_over(self):
        with _Server(), tempfile.TemporaryDirectory() as tmp:
            dest, ok, _ = install._download_one(dict(ENTRY), Path(tmp))
            self.assertTrue(ok)
            self.assertEqual(_RangeHandler.ranges, [""])

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_link_at_the_partial_name_is_never_written_through(self):
        with _Server(), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(install, "RESUME_FROM_SIZE", 1024):
            victim = Path(tmp) / "victim.txt"
            victim.write_bytes(b"keep me")
            part = Path(tmp) / "bios" / ".big.bin.part"
            part.parent.mkdir()
            part.symlink_to(victim)
            dest, ok, _ = install._download_one(dict(ENTRY), Path(tmp) / "bios")
            self.assertTrue(ok)
            self.assertEqual(victim.read_bytes(), b"keep me")
            final = Path(tmp) / "bios" / "big.bin"
            self.assertFalse(final.is_symlink())
            self.assertEqual(final.read_bytes(), PAYLOAD)
            self.assertEqual(_RangeHandler.ranges, [""])


class TwoRunsOneFolder(unittest.TestCase):
    """Two installs into one BIOS folder appended to the same partial copy:
    each hashed the other's bytes and one renamed a file still growing."""

    @unittest.skipIf(os.name == "nt", "flock semantics")
    def test_a_partial_held_by_another_run_is_left_alone(self):
        with _Server(), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(install, "RESUME_FROM_SIZE", 1024):
            part = Path(tmp) / ".big.bin.part"
            part.write_bytes(PAYLOAD[:40000])
            other = install._claim_partial(part)
            self.assertIsNotNone(other)
            try:
                dest, ok, reason = install._download_one(dict(ENTRY), Path(tmp))
                self.assertEqual((ok, reason), (True, ""))
                self.assertEqual((Path(tmp) / "big.bin").read_bytes(), PAYLOAD)
                self.assertEqual(part.read_bytes(), PAYLOAD[:40000])
                self.assertEqual(_RangeHandler.ranges, [""])
                self.assertIsNone(install._claim_partial(part))
            finally:
                other.close()
            self.assertIsNotNone(install._claim_partial(part))


class APartialRenamedBeforeTheLock(unittest.TestCase):
    """Open and lock are two steps. The run holding the partial could finish
    in between, rename it into place and release it: the second run then
    locked the installed BIOS, took it for a finished partial and truncated it."""

    @unittest.skipIf(os.name == "nt", "flock semantics")
    def test_the_installed_file_is_not_claimed(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / ".big.bin.part"
            final = Path(tmp) / "big.bin"
            part.write_bytes(PAYLOAD)
            real_flock = fcntl.flock

            def finish_first(fd, op):
                os.replace(part, final)
                return real_flock(fd, op)

            with mock.patch.object(fcntl, "flock", finish_first):
                claimed = install._claim_partial(part)
            self.assertIsNone(claimed)
            self.assertEqual(final.read_bytes(), PAYLOAD)


class InstalledFileModes(unittest.TestCase):
    """A file came out 0600, the mode of the temporary it was written to: run
    with sudo for a root-owned BIOS folder, the emulator's own user could not
    read it, and RetroDECK's pico8 and vox launchers were never executable."""

    def _mode(self, entry: dict) -> int:
        with _Server(), tempfile.TemporaryDirectory() as tmp:
            dest, ok, _ = install._download_one(dict(entry), Path(tmp))
            self.assertTrue(ok)
            return (Path(tmp) / dest).stat().st_mode & 0o777

    def test_the_umask_decides_as_for_any_new_file(self):
        umask = os.umask(0o022)
        try:
            self.assertEqual(self._mode(ENTRY), 0o644)
            self.assertEqual(self._mode({**ENTRY, "executable": True}), 0o755)
        finally:
            os.umask(umask)


class ProgramsAreMarked(unittest.TestCase):
    """The manifest says which files are programs, read from their header:
    the git mode cannot, hundreds of MAME archives carrying an execute bit."""

    def test_header_decides(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import generate_pack

        with tempfile.TemporaryDirectory() as tmp:
            for data, expected in ((b"\x7fELF\x02", True), (b"#!/bin/sh\n", True),
                                   (b"PK\x03\x04", False), (b"", False)):
                path = Path(tmp) / "f"
                path.write_bytes(data)
                with self.subTest(head=data):
                    self.assertEqual(generate_pack._is_program(str(path)), expected)

    def test_retrodeck_launchers_are_executable(self):
        manifest = json.loads((REPO_ROOT / "install" / "retrodeck.json").read_text())
        launchers = [f for f in manifest["files"]
                     if f["dest"].endswith(("pico-8/pico8", "voxatron/vox"))]
        if not launchers:
            self.skipTest("no launcher in the RetroDECK manifest")
        for entry in launchers:
            with self.subTest(dest=entry["dest"]):
                self.assertIs(entry.get("executable"), True)


class ManifestHashesMatchTheScan(unittest.TestCase):
    """A manifest entry's sha1 is checked against the database scan of the
    same path: under load this host hashed a 252 MB DSi NAND to a sha1 no
    file holds, the same wrong value in two runs."""

    def test_a_contradicted_hash_is_refused(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import generate_pack

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nand.bin"
            path.write_bytes(b"real")
            real = hashlib.sha1(b"real").hexdigest()
            good = {"files": {real: {"path": str(path)}}}
            self.assertEqual(generate_pack._stable_hashes(str(path), good)["sha1"], real)
            bad = {"files": {"0" * 40: {"path": str(path)}}}
            with self.assertRaises(RuntimeError):
                generate_pack._stable_hashes(str(path), bad)
            self.assertEqual(generate_pack._stable_hashes(str(path), {"files": {}})["sha1"], real)

    def test_the_cache_is_keyed_by_what_was_checked(self):
        """Keyed by id(db), a database freed and replaced at the same address
        got hashes verified against the first one back, unchecked."""
        source = (REPO_ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        body = source[source.index("def _stable_hashes("):]
        body = body[: body.index("\ndef ")]
        key_line = next(line for line in body.splitlines() if line.strip().startswith("key ="))
        self.assertNotIn("id(", key_line)
        self.assertIn("expected", key_line)


class RetriesWait(unittest.TestCase):
    def test_a_server_error_is_retried_after_the_delay_it_asks(self):
        with _Server(), tempfile.TemporaryDirectory() as tmp:
            _RangeHandler.fail_first = [503]
            dest, ok, _ = install._download_one(dict(ENTRY), Path(tmp))
            self.assertTrue(ok)

    def test_the_delay_doubles_and_follows_retry_after(self):
        self.assertEqual([install._retry_delay(n, None) for n in (1, 2, 3, 4, 5)],
                         [1.0, 2.0, 4.0, 8.0, 8.0])
        self.assertEqual(install._retry_delay(1, "5"), 5.0)
        self.assertEqual(install._retry_delay(1, "999"), 60.0)

    def test_a_refusal_names_the_rate_limit(self):
        with _Server(), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(install, "MAX_RETRIES", 1):
            _RangeHandler.fail_first = [403]
            _dest, ok, reason = install._download_one(dict(ENTRY), Path(tmp))
            self.assertFalse(ok)
            self.assertIn("rate limit", reason)


class ProgressOutput(unittest.TestCase):
    def _term(self, quiet=False):
        stream = io.StringIO()
        term = install.Terminal(stream, quiet=quiet)
        return stream, term

    def test_a_log_gets_one_line_per_file_and_failures_say_why(self):
        stream, term = self._term()
        progress = install.Progress(2, 10, term)
        progress.done("a.bin", True)
        progress.done("b.bin", False, "HTTP 404")
        out = stream.getvalue()
        self.assertIn("[1/2] a.bin ok", out)
        self.assertIn("FAILED b.bin (HTTP 404)", out)
        self.assertNotIn("\033", out)

    def test_quiet_leaves_failures_to_the_summary(self):
        stream, term = self._term(quiet=True)
        progress = install.Progress(2, 10, term)
        progress.done("a.bin", True)
        progress.done("b.bin", False, "HTTP 404")
        self.assertEqual(stream.getvalue(), "")

    def test_durations_read_like_a_clock(self):
        self.assertEqual([install.format_duration(s) for s in (0, 42, 185, 3729)],
                         ["0:00", "0:42", "3:05", "1:02:09"])

    def test_the_summary_names_each_failure(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            install._report_outcome(
                [Path("/b")], 1, 0, 1, 0, False,
                failures=[("x.bin", "HTTP 404")], transferred=2048, seconds=2.0,
            )
        out = buf.getvalue()
        self.assertIn("failed: x.bin (HTTP 404)", out)
        self.assertIn("2.0 KB downloaded in 0:02", out)


class CommandLine(unittest.TestCase):
    def test_a_dry_run_writes_nothing(self):
        with FakeRepository() as repo:
            proc = repo.run("--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Would download", proc.stdout)
            self.assertIn("Nothing was written", proc.stdout)
            self.assertEqual(repo.installed(), [])

    def test_quiet_prints_the_summary_only(self):
        with FakeRepository() as repo:
            proc = repo.run("--quiet")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("Checking existing files", proc.stdout)
            self.assertIn("Done.", proc.stdout)
            self.assertTrue(repo.installed())

    def test_version_names_the_file_the_bootstraps_pin(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--version"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        digest = hashlib.sha256((REPO_ROOT / "install.py").read_bytes()).hexdigest()
        self.assertIn(digest[:12], proc.stdout)



class ReviewCases(unittest.TestCase):
    """Saying no to the large-download question ended on Done, --quiet hid
    fatal messages and swallowed --check, and a resumed copy inflated the speed."""

    def test_declining_is_not_done(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            install._report_outcome([Path("/b")], 0, 3, 0, 0, False, declined=12)
        out = buf.getvalue()
        self.assertNotIn("Done.", out)
        self.assertIn("12 files not downloaded", out)
        self.assertIn("3 files already up to date", out)

    def test_quiet_refuses_modes_it_would_silence(self):
        for mode in ("--check", "--dry-run", "--list-platforms", "--list-systems"):
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--quiet", mode],
                capture_output=True, text=True, timeout=60, check=False,
            )
            with self.subTest(mode=mode):
                self.assertEqual(proc.returncode, 2)
                self.assertIn("--quiet applies to an install", proc.stderr)

    def test_quiet_shows_what_a_failed_run_printed(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, USERPROFILE=home, RETROBIOS_OS="linux",
                       XDG_CONFIG_HOME=home)
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--quiet"],
                capture_output=True, text=True, timeout=60, check=False,
                env=env, stdin=subprocess.DEVNULL,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("No supported platform detected", proc.stderr)

    def test_a_resumed_copy_is_not_counted_as_fetched(self):
        progress = install.Progress(1, 100, install.Terminal(io.StringIO()))
        progress.add(60, resumed=True)
        progress.add(40)
        self.assertEqual(progress.transferred(), 40)

if __name__ == "__main__":
    unittest.main()
