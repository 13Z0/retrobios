"""Tests for install.py platform detection and config parsing."""
from __future__ import annotations

import contextlib
import functools
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shlex
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
import unittest.mock
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("install", REPO_ROOT / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


def _tempdir(test: unittest.TestCase) -> str:
    """A scratch directory under the repository's tmp/, removed after the test.

    The system temp directory is a RAM disk here, and directories left by
    every run piled up on it.
    """
    scratch = REPO_ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    path = tempfile.mkdtemp(dir=str(scratch))
    test.addCleanup(shutil.rmtree, path, True)
    return path


class TestShellUnquote(unittest.TestCase):
    def test_plain_value(self):
        self.assertEqual(install._shell_unquote("/userdata/bios"), "/userdata/bios")

    def test_double_quoted(self):
        self.assertEqual(install._shell_unquote('"/home/deck/Emulation"'), "/home/deck/Emulation")

    def test_single_quoted(self):
        self.assertEqual(install._shell_unquote("'/home/deck/Emulation'"), "/home/deck/Emulation")

    def test_concatenated_quoted_unquoted(self):
        # EmuDeck SD card layout (issue: quote left in the middle of the path)
        self.assertEqual(
            install._shell_unquote('"/run/media/deck/EmuSD"/Emulation'),
            "/run/media/deck/EmuSD/Emulation",
        )

    def test_concatenated_preserves_spaces_in_quotes(self):
        self.assertEqual(
            install._shell_unquote('"/run/media/deck/My Card"/Emulation'),
            "/run/media/deck/My Card/Emulation",
        )

    def test_home_variable_expansion(self):
        old = os.environ.get("HOME")
        os.environ["HOME"] = "/home/deck"
        try:
            self.assertEqual(
                install._shell_unquote('"$HOME"/Emulation'),
                "/home/deck/Emulation",
            )
            self.assertEqual(
                install._shell_unquote('"${HOME}/Emulation"'),
                "/home/deck/Emulation",
            )
        finally:
            if old is not None:
                os.environ["HOME"] = old

    def test_single_quotes_do_not_expand(self):
        self.assertEqual(install._shell_unquote("'$HOME/x'"), "$HOME/x")

    def test_trailing_comment(self):
        self.assertEqual(install._shell_unquote("/data/bios # main dir"), "/data/bios")

    def test_unquoted_stops_at_whitespace(self):
        self.assertEqual(install._shell_unquote("/data/bios extra"), "/data/bios")

    def test_tilde_expansion(self):
        old = os.environ.get("HOME")
        os.environ["HOME"] = "/home/deck"
        try:
            self.assertEqual(install._shell_unquote("~/Emulation"), "/home/deck/Emulation")
        finally:
            if old is not None:
                os.environ["HOME"] = old


class TestParseBashVar(unittest.TestCase):
    def _write(self, content: str) -> Path:
        fd, path = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        Path(path).write_text(content, encoding="utf-8")
        self.addCleanup(os.unlink, path)
        return Path(path)

    def test_emudeck_sd_card_concatenation(self):
        path = self._write(
            'emulationPath="/run/media/deck/EmuSD"/Emulation\n'
            'biosPath="/run/media/deck/EmuSD"/Emulation/bios\n'
        )
        self.assertEqual(
            install._parse_bash_var(path, "emulationPath"),
            "/run/media/deck/EmuSD/Emulation",
        )
        self.assertEqual(
            install._parse_bash_var(path, "biosPath"),
            "/run/media/deck/EmuSD/Emulation/bios",
        )

    def test_emudeck_home_variable(self):
        old = os.environ.get("HOME")
        os.environ["HOME"] = "/home/deck"
        try:
            path = self._write('emulationPath="$HOME"/Emulation\n')
            self.assertEqual(
                install._parse_bash_var(path, "emulationPath"),
                "/home/deck/Emulation",
            )
        finally:
            if old is not None:
                os.environ["HOME"] = old

    def test_missing_key(self):
        path = self._write("other=1\n")
        self.assertIsNone(install._parse_bash_var(path, "emulationPath"))

    def test_missing_file(self):
        self.assertIsNone(
            install._parse_bash_var(Path("/nonexistent/settings.sh"), "emulationPath")
        )


class TestParseJsonPath(unittest.TestCase):
    def _write(self, data) -> Path:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(path).write_text(
            data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
        )
        self.addCleanup(os.unlink, path)
        return Path(path)

    def test_retrodeck_rd_home_path(self):
        path = self._write(
            {"version": "1.0", "paths": {"rd_home_path": "/home/deck/retrodeck"}}
        )
        self.assertEqual(
            install._parse_json_path(path, "paths", "rd_home_path"),
            "/home/deck/retrodeck",
        )

    def test_missing_key_returns_none(self):
        path = self._write({"paths": {}})
        self.assertIsNone(install._parse_json_path(path, "paths", "rd_home_path"))

    def test_invalid_json_returns_none(self):
        path = self._write("{not json")
        self.assertIsNone(install._parse_json_path(path, "paths", "rd_home_path"))

    def test_empty_value_returns_none(self):
        path = self._write({"paths": {"rd_home_path": ""}})
        self.assertIsNone(install._parse_json_path(path, "paths", "rd_home_path"))

    def test_missing_file(self):
        self.assertIsNone(
            install._parse_json_path(Path("/nonexistent/rd.json"), "paths", "rd_home_path")
        )


class TestDefaultDests(unittest.TestCase):
    def test_retrodeck_defaults_to_rdhome(self):
        self.assertEqual(
            install.DEFAULT_DESTS["retrodeck"], Path.home() / "retrodeck"
        )

    def test_known_fixed_paths(self):
        self.assertEqual(install.DEFAULT_DESTS["batocera"], Path("/userdata/bios"))
        self.assertEqual(
            install.DEFAULT_DESTS["recalbox"], Path("/recalbox/share/bios")
        )

    def test_mister_defaults_to_games_on_the_sd_card(self):
        self.assertEqual(
            install.DEFAULT_DESTS["misterfpga"], Path("/media/fat/games")
        )


class TestDefaultsFollowTheRegistry(unittest.TestCase):
    """A platform named but not detected fell back to ~/bios on every OS for
    RetroArch, RetroBat, BizHawk and RomM, a folder none of them reads."""

    ENV = {"USERPROFILE": "/u", "APPDATA": "/u/AppData/Roaming"}

    def _expand(self, raw: str) -> Path:
        text = raw.replace("$HOME", str(Path.home()))
        for name, value in self.ENV.items():
            text = text.replace(f"%{name}%", value)
        return Path(text.replace("\\", "/"))

    def test_the_default_is_a_folder_detection_looks_for(self):
        import yaml

        registry = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "platforms" / "_registry.yml")
            .read_text(encoding="utf-8")
        )["platforms"]
        checked = 0
        with unittest.mock.patch.dict(os.environ, self.ENV):
            for name, entry in registry.items():
                by_os: dict[str, set[Path]] = {}
                for rule in (entry.get("install") or {}).get("detect", []):
                    if rule.get("bios_path"):
                        by_os.setdefault(rule["os"], set()).add(self._expand(rule["bios_path"]))
                    elif rule.get("method") == "path_exists":
                        by_os.setdefault(rule["os"], set()).add(self._expand(rule["path"]))
                    elif rule.get("parse_key") == "system_directory":
                        config = self._expand(rule["config"])
                        by_os.setdefault(rule["os"], set()).add(config.parent / "system")
                if name in install.PORTABLE_ONLY:
                    # No folder of its own to fall back on: the run stops
                    # and asks for --dest (test_a_portable_platform_is_not_guessed).
                    continue
                for os_type, candidates in by_os.items():
                    with self.subTest(platform=name, os=os_type):
                        self.assertIn(install._default_dest(os_type, name), candidates)
                        checked += 1
                # The installer's own default comes first in the registry and
                # is the one the pack guide and the installer name.
                first = next(
                    (self._expand(rule["path"]) for rule in (entry.get("install") or {}).get("detect", [])
                     if rule.get("os") == "windows" and rule.get("method") == "path_exists"),
                    None,
                )
                if name == "retrobat":
                    self.assertEqual(install._default_dest("windows", name), first)
        self.assertGreater(checked, 10)


class TestEmbeddedDetection(unittest.TestCase):
    """MiSTer is identified by the main binary at the SD card root."""

    def _detect_with(self, existing: set[str], os_id: str = ""):
        real_exists = install.Path.exists
        real_parse = install._parse_os_release

        def fake_exists(self):
            return str(self) in existing

        install.Path.exists = fake_exists
        install._parse_os_release = lambda: {"ID": os_id} if os_id else {}
        try:
            return install._detect_embedded()
        finally:
            install.Path.exists = real_exists
            install._parse_os_release = real_parse

    def test_mister_detected_from_main_binary(self):
        # A path rule of the registry, read by _detect_by_path.
        real_exists = install.Path.exists
        install.Path.exists = lambda self: str(self) == "/media/fat/MiSTer"
        try:
            found: list = []
            install._detect_by_path("linux", found)
        finally:
            install.Path.exists = real_exists
        self.assertEqual(found, [("misterfpga", Path("/media/fat/games"))])

    def test_rocknix_wins_over_path_probes(self):
        self.assertEqual(
            self._detect_with({"/media/fat/MiSTer"}, os_id="rocknix"),
            [("rocknix", Path("/storage/roms/bios"))],
        )

    def test_no_embedded_os_detected(self):
        self.assertEqual(self._detect_with(set()), [])


class TestRunOutcome(unittest.TestCase):
    """A run says what happened and leaves with a status that matches."""

    def _report(self, **kw):
        defaults = dict(
            paths=[Path("/userdata/bios")],
            downloaded=0,
            up_to_date=0,
            errors=0,
            omitted=0,
            interrupted=False,
        )
        defaults.update(kw)
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                install._report_outcome(**defaults)
            except SystemExit as exc:
                code = exc.code
        return buf.getvalue(), code

    def test_success_names_the_location_and_the_way_to_check(self):
        out, code = self._report(downloaded=12, up_to_date=3)
        self.assertEqual(code, 0)
        self.assertIn("12 files installed", out)
        self.assertIn("/userdata/bios", out)
        self.assertIn("--check", out)

    def test_total_failure_does_not_say_done_and_exits_two(self):
        out, code = self._report(errors=7)
        self.assertEqual(code, 2)
        self.assertNotIn("Done.", out)
        self.assertIn("Nothing was installed", out)

    def test_partial_failure_exits_one(self):
        out, code = self._report(downloaded=5, errors=2)
        self.assertEqual(code, 1)
        self.assertIn("5 files installed", out)
        self.assertIn("2 files failed", out)

    def test_interruption_is_resumable_and_exits_130(self):
        out, code = self._report(downloaded=431, interrupted=True)
        self.assertEqual(code, 130)
        self.assertIn("carry on", out)
        self.assertIn("Nothing is lost", out)

    def test_one_file_reads_as_one_file(self):
        out, _ = self._report(downloaded=1)
        self.assertIn("1 file installed", out)
        self.assertNotIn("1 files", out)

    def test_every_destination_is_named(self):
        out, _ = self._report(
            paths=[Path("/a/bios"), Path("/b/bios")], downloaded=2
        )
        self.assertIn("/a/bios", out)
        self.assertIn("/b/bios", out)


class TestFreeSpaceGuard(unittest.TestCase):
    """The run refuses before writing when the volume cannot hold it."""

    def setUp(self):
        self.root = Path(_tempdir(self))
        self.real = install.shutil.disk_usage

    def tearDown(self):
        install.shutil.disk_usage = self.real
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, free: int, needed: int, path: Path | None = None):
        install.shutil.disk_usage = lambda _p: SimpleNamespace(free=free)
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stderr(buf):
            try:
                install._check_free_space(path or self.root, needed)
            except SystemExit as exc:
                code = exc.code
        return buf.getvalue(), code

    def test_enough_space_says_nothing(self):
        out, code = self._run(free=10_000, needed=1_000)
        self.assertEqual((out, code), ("", 0))

    def test_short_of_space_names_both_numbers_and_stops(self):
        out, code = self._run(free=1_000, needed=5_000)
        self.assertEqual(code, 1)
        self.assertIn("Needed", out)
        self.assertIn("Free", out)
        self.assertIn("--dest", out)

    def test_probe_climbs_to_an_existing_ancestor(self):
        missing = self.root / "not" / "created" / "yet"
        out, code = self._run(free=1, needed=2, path=missing)
        self.assertEqual(code, 1)


class AFolderThatRefusesWritesIsNamed(unittest.TestCase):
    """A read-only BIOS folder ended the run on a traceback (a subfolder to
    create), or on three retries per file and "once that is resolved", and a
    platform named but not detected was told to use sudo, which on another
    machine creates /userdata/bios on the wrong disk."""

    ENTRY = {"dest": "mame2015/neogeo.zip", "repo_path": "bios/x", "size": 4}

    def test_a_subfolder_that_cannot_be_made_is_a_failure_not_a_crash(self):
        with unittest.mock.patch.object(Path, "mkdir", side_effect=PermissionError(13, "denied")):
            result = install._download_one(self.ENTRY, Path("/userdata/bios"))
        self.assertEqual(result, ("mame2015/neogeo.zip", False, install.NOT_WRITABLE))

    def test_a_refused_write_is_not_retried(self):
        class _Body(io.BytesIO):
            status, headers = 200, {"Content-Length": "4"}

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(install.urllib.request, "urlopen",
                                  return_value=_Body(b"data")) as opened, \
                unittest.mock.patch.object(install.tempfile, "NamedTemporaryFile",
                                  side_effect=PermissionError(13, "denied")):
            result = install._download_one(self.ENTRY, Path(tmp))
        self.assertEqual(result[1:], (False, install.NOT_WRITABLE))
        self.assertEqual(opened.call_count, 1)

    def _summary_for(self, assumed):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            install._report_outcome(
                paths=[Path("/userdata/bios")], downloaded=0, up_to_date=0, errors=1,
                omitted=0, interrupted=False,
                failures=[("neogeo.zip", install.NOT_WRITABLE)], assumed=assumed,
            )
        return buf.getvalue()

    def test_the_summary_says_sudo_on_the_console_and_dest_elsewhere(self):
        here = self._summary_for(set())
        self.assertIn("sudo", here)
        guessed = self._summary_for({Path("/userdata/bios")})
        self.assertIn("--dest", guessed)
        self.assertNotIn("sudo", guessed)


class TestStopFlag(unittest.TestCase):
    """Ctrl+C ends the run instead of letting the queue drain."""

    def tearDown(self):
        install._stop.clear()

    def test_worker_returns_immediately_once_stopped(self):
        install._stop.set()
        dest, ok, reason = install._download_one({"dest": "x.bin"}, Path("/nowhere"))
        self.assertEqual((dest, ok, reason), ("x.bin", False, "stopped"))

    def test_download_files_reports_no_interruption_normally(self):
        failed, interrupted = install.download_files([], Path("/nowhere"))
        self.assertEqual((failed, interrupted), ([], False))


class TestAndroidDetection(unittest.TestCase):
    """Android reports itself as Linux, and RetroArch is the frontend there."""

    def setUp(self):
        self.saved = {
            key: os.environ.get(key)
            for key in ("ANDROID_ROOT", "ANDROID_DATA", "EXTERNAL_STORAGE", "RETROBIOS_OS")
        }
        for key in self.saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_runtime_variables_name_android(self):
        os.environ["ANDROID_ROOT"] = "/system"
        os.environ["ANDROID_DATA"] = "/data"
        self.assertTrue(install._is_android())

    def test_plain_linux_is_not_android(self):
        self.assertFalse(
            install._is_android() and not Path("/system/build.prop").exists()
        )

    def test_forced_os_accepts_android(self):
        os.environ["RETROBIOS_OS"] = "android"
        self.assertEqual(install.detect_os(), "android")

    def test_default_directory_found_without_a_config(self):
        storage = Path(_tempdir(self))
        (storage / "RetroArch" / "system").mkdir(parents=True)
        os.environ["EXTERNAL_STORAGE"] = str(storage)
        self.assertEqual(
            install._detect_android(),
            [("retroarch", storage / "RetroArch" / "system")],
        )
        shutil.rmtree(storage)

    def test_config_default_resolves_against_the_retroarch_root(self):
        storage = Path(_tempdir(self))
        files = storage / "Android" / "data" / "com.retroarch.aarch64" / "files"
        files.mkdir(parents=True)
        (files / "retroarch.cfg").write_text('system_directory = "default"\n')
        os.environ["EXTERNAL_STORAGE"] = str(storage)
        self.assertEqual(
            install._detect_android(),
            [("retroarch", storage / "RetroArch" / "system")],
        )
        shutil.rmtree(storage)

    def test_custom_system_directory_wins(self):
        storage = Path(_tempdir(self))
        files = storage / "Android" / "data" / "com.retroarch" / "files"
        files.mkdir(parents=True)
        custom = storage / "Emulation" / "bios"
        (files / "retroarch.cfg").write_text(f'system_directory = "{custom}"\n')
        os.environ["EXTERNAL_STORAGE"] = str(storage)
        self.assertEqual(install._detect_android(), [("retroarch", custom)])
        shutil.rmtree(storage)

    def test_nothing_found_on_a_bare_device(self):
        storage = Path(_tempdir(self))
        os.environ["EXTERNAL_STORAGE"] = str(storage)
        self.assertEqual(install._detect_android(), [])
        shutil.rmtree(storage)

    def test_detect_platforms_routes_android(self):
        storage = Path(_tempdir(self))
        (storage / "RetroArch" / "system").mkdir(parents=True)
        os.environ["EXTERNAL_STORAGE"] = str(storage)
        self.assertEqual(
            install.detect_platforms("android"),
            [("retroarch", storage / "RetroArch" / "system")],
        )
        shutil.rmtree(storage)

    def test_manual_hint_names_the_android_path(self):
        hint = "\n".join(install._manual_usage_hint("android"))
        self.assertIn("/storage/emulated/0/RetroArch/system", hint)

    def test_named_platform_falls_back_to_shared_storage(self):
        storage = Path(_tempdir(self))
        os.environ["EXTERNAL_STORAGE"] = str(storage)
        self.assertEqual(
            install._default_dest("android", "retroarch"),
            storage / "RetroArch" / "system",
        )
        self.assertEqual(
            install._default_dest("linux", "batocera"), Path("/userdata/bios")
        )
        shutil.rmtree(storage)

    def test_packages_match_the_published_flavors(self):
        self.assertEqual(
            install.ANDROID_RETROARCH_PACKAGES,
            ("com.retroarch.aarch64", "com.retroarch", "com.retroarch.ra32"),
        )


class TestLaunchboxDetection(unittest.TestCase):
    """LaunchBox references its emulators in Data/Emulators.xml."""

    def _make_launchbox(self, app_path: str, title: str = "Retroarch"):
        root = Path(_tempdir(self))
        data = root / "LaunchBox" / "Data"
        data.mkdir(parents=True)
        xml = data / "Emulators.xml"
        xml.write_text(
            "<?xml version=\"1.0\" standalone=\"yes\"?>\n"
            "<LaunchBox>\n"
            "  <Emulator>\n"
            "    <ID>x</ID>\n"
            f"    <Title>{title}</Title>\n"
            f"    <ApplicationPath>{app_path}</ApplicationPath>\n"
            "  </Emulator>\n"
            "</LaunchBox>\n"
        )
        return root, xml

    def test_relative_application_path(self):
        root, xml = self._make_launchbox("Emulators\\RetroArch\\retroarch.exe")
        ra_dir = root / "LaunchBox" / "Emulators" / "RetroArch"
        ra_dir.mkdir(parents=True)
        self.assertEqual(
            install._launchbox_retroarch_system_dir(xml), ra_dir / "system"
        )

    def test_absolute_application_path(self):
        emu_dir = Path(_tempdir(self)) / "RetroArch"
        emu_dir.mkdir(parents=True)
        root, xml = self._make_launchbox(str(emu_dir / "retroarch.exe"))
        self.assertEqual(
            install._launchbox_retroarch_system_dir(xml), emu_dir / "system"
        )

    def test_no_retroarch_entry(self):
        root, xml = self._make_launchbox("Emulators\\PCSX2\\pcsx2.exe", "PCSX2")
        self.assertIsNone(install._launchbox_retroarch_system_dir(xml))

    def test_missing_emulator_directory(self):
        root, xml = self._make_launchbox("Emulators\\RetroArch\\retroarch.exe")
        self.assertIsNone(install._launchbox_retroarch_system_dir(xml))

    def test_stale_entry_before_installed_one(self):
        root = Path(_tempdir(self))
        data = root / "LaunchBox" / "Data"
        data.mkdir(parents=True)
        ra_dir = root / "LaunchBox" / "Emulators" / "RetroArch"
        ra_dir.mkdir(parents=True)
        xml = data / "Emulators.xml"
        xml.write_text(
            "<?xml version=\"1.0\" standalone=\"yes\"?>\n"
            "<LaunchBox>\n"
            "  <Emulator>\n"
            "    <Title>RetroArch (old)</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch-old\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n"
            "  <Emulator>\n"
            "    <Title>RetroArch</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n"
            "</LaunchBox>\n"
        )
        self.assertEqual(
            install._launchbox_retroarch_system_dir(xml), ra_dir / "system"
        )

    def test_missing_or_malformed_xml(self):
        self.assertIsNone(
            install._launchbox_retroarch_system_dir(Path("/nonexistent/Emulators.xml"))
        )
        root = Path(_tempdir(self))
        bad = root / "Emulators.xml"
        bad.write_text("not xml at all <<<")
        self.assertIsNone(install._launchbox_retroarch_system_dir(bad))


class TestRetroarchPathExpansion(unittest.TestCase):
    """RetroArch abbreviates paths with '~' and ':' in its config."""

    def _cfg(self, value: str) -> Path:
        app_dir = Path(_tempdir(self))
        (app_dir / "retroarch.cfg").write_text(f'system_directory = "{value}"\n')
        return app_dir

    def test_application_dir_prefix(self):
        app_dir = self._cfg(":\\system")
        self.assertEqual(
            install._parse_retroarch_system_dir(app_dir / "retroarch.cfg"),
            app_dir / "system",
        )

    def test_application_dir_prefix_forward_slash(self):
        app_dir = self._cfg(":/system")
        self.assertEqual(
            install._parse_retroarch_system_dir(app_dir / "retroarch.cfg"),
            app_dir / "system",
        )

    def test_home_prefix(self):
        app_dir = self._cfg("~/bios")
        self.assertEqual(
            install._parse_retroarch_system_dir(app_dir / "retroarch.cfg"),
            Path.home() / "bios",
        )

    def test_application_dir_overrides_config_dir(self):
        app_dir = self._cfg(":\\system")
        elsewhere = Path(_tempdir(self))
        self.assertEqual(
            install._parse_retroarch_system_dir(
                app_dir / "retroarch.cfg", elsewhere
            ),
            elsewhere / "system",
        )

    def test_absolute_path_untouched(self):
        target = Path(_tempdir(self)) / "bios"
        app_dir = self._cfg(str(target))
        self.assertEqual(
            install._parse_retroarch_system_dir(app_dir / "retroarch.cfg"), target
        )


class TestLaunchboxRoot(unittest.TestCase):
    """LaunchBox installs where the user chooses and keeps no registry key."""

    def _make_root(self, root: Path) -> Path:
        (root / "Data").mkdir(parents=True)
        (root / "Data" / "Emulators.xml").write_text(
            "<?xml version=\"1.0\" standalone=\"yes\"?>\n<LaunchBox></LaunchBox>\n"
        )
        return root

    def test_shortcut_target_is_read(self):
        # Byte layout of the shortcut a real install writes
        lnk = Path(_tempdir(self)) / "LaunchBox.lnk"
        lnk.write_bytes(
            b"L\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00"
            b"\x00\x00\x00\x46" + b"\x00" * 8
            + b"C:\\Games\\LaunchBox\\Core\\LaunchBox.exe\x00"
            b"\x1c\x00\x00\x00LaunchBox\x00"
        )
        self.assertEqual(
            install._lnk_target(lnk, "LaunchBox.exe"),
            Path("C:/Games/LaunchBox/Core/LaunchBox.exe"),
        )

    def test_shortcut_without_target_is_ignored(self):
        lnk = Path(_tempdir(self)) / "LaunchBox.lnk"
        lnk.write_bytes(b"L\x00\x00\x00 no target here ")
        self.assertIsNone(install._lnk_target(lnk, "LaunchBox.exe"))

    def test_shortcut_pointing_elsewhere_falls_back(self):
        # A stale shortcut must not win over a real installation
        userprofile = Path(_tempdir(self))
        root = self._make_root(userprofile / "LaunchBox")
        appdata = Path(_tempdir(self))
        lnk_dir = (
            appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "LaunchBox"
        )
        lnk_dir.mkdir(parents=True)
        (lnk_dir / "LaunchBox.lnk").write_bytes(
            b"L\x00\x00\x00D:\\Gone\\LaunchBox\\Core\\LaunchBox.exe\x00"
        )
        env = {"APPDATA": str(appdata), "USERPROFILE": str(userprofile)}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(install.launchbox_root("windows"), root)

    def test_falls_back_to_userprofile(self):
        userprofile = Path(_tempdir(self))
        root = self._make_root(userprofile / "LaunchBox")
        env = {"APPDATA": str(Path(_tempdir(self))), "USERPROFILE": str(userprofile)}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(install.launchbox_root("windows"), root)

    def test_absent_on_linux(self):
        self.assertIsNone(install.launchbox_root("linux"))

    def test_nothing_installed(self):
        env = {
            "APPDATA": str(Path(_tempdir(self))),
            "USERPROFILE": str(Path(_tempdir(self))),
        }
        with unittest.mock.patch.dict(os.environ, env):
            self.assertIsNone(install.launchbox_root("windows"))


class TestLaunchboxBiosDirs(unittest.TestCase):
    """LaunchBox computes where each emulator it manages keeps its BIOS."""

    def _make(self, entries: list[tuple[str, str]]) -> Path:
        root = Path(_tempdir(self)) / "LaunchBox"
        (root / "Data").mkdir(parents=True)
        body = "".join(
            "  <Emulator>\n"
            f"    <Title>{title}</Title>\n"
            f"    <ApplicationPath>{app}</ApplicationPath>\n"
            "  </Emulator>\n"
            for title, app in entries
        )
        (root / "Data" / "Emulators.xml").write_text(
            "<?xml version=\"1.0\" standalone=\"yes\"?>\n"
            f"<LaunchBox>\n{body}</LaunchBox>\n"
        )
        for _, app in entries:
            (root / Path(app.replace("\\", "/")).parent).mkdir(parents=True, exist_ok=True)
        return root

    def test_pcsx2_portable_default_bios(self):
        root = self._make([("PCSX2", "Emulators\\PCSX2\\pcsx2.exe")])
        emu = root / "Emulators" / "PCSX2"
        (emu / "portable.ini").write_text("")
        (emu / "inis").mkdir()
        (emu / "inis" / "PCSX2.ini").write_text("[Folders]\nBios = bios\n")
        self.assertEqual(install.launchbox_bios_dirs(root)["pcsx2"], emu / "bios")

    def test_pcsx2_portable_absolute_bios(self):
        root = self._make([("PCSX2", "Emulators\\PCSX2\\pcsx2.exe")])
        emu = root / "Emulators" / "PCSX2"
        elsewhere = Path(_tempdir(self)) / "ps2bios"
        (emu / "portable.ini").write_text("")
        (emu / "inis").mkdir()
        (emu / "inis" / "PCSX2.ini").write_text(f"Bios = {elsewhere}\n")
        self.assertEqual(install.launchbox_bios_dirs(root)["pcsx2"], elsewhere)

    def test_pcsx2_portable_txt_also_triggers_portable_mode(self):
        root = self._make([("PCSX2", "Emulators\\PCSX2\\pcsx2.exe")])
        emu = root / "Emulators" / "PCSX2"
        (emu / "portable.txt").write_text("")
        self.assertEqual(install.launchbox_bios_dirs(root)["pcsx2"], emu / "bios")

    def test_pcsx2_portable_txt_names_a_data_root(self):
        root = self._make([("PCSX2", "Emulators\\PCSX2\\pcsx2.exe")])
        emu = root / "Emulators" / "PCSX2"
        (emu / "portable.txt").write_text("mydata\n")
        self.assertEqual(
            install.launchbox_bios_dirs(root)["pcsx2"], emu / "mydata" / "bios"
        )

    def test_pcsx2_without_config_uses_documents(self):
        root = self._make([("PCSX2", "Emulators\\PCSX2\\pcsx2.exe")])
        userprofile = Path(_tempdir(self))
        with unittest.mock.patch.dict(os.environ, {"USERPROFILE": str(userprofile)}):
            self.assertEqual(
                install.launchbox_bios_dirs(root)["pcsx2"],
                userprofile / "Documents" / "PCSX2" / "bios",
            )

    def test_xemu_defaults_to_bios_folder(self):
        root = self._make([("Xemu", "Emulators\\Xemu\\xemu.exe")])
        emu = root / "Emulators" / "Xemu"
        with unittest.mock.patch.dict(os.environ, {"APPDATA": str(Path(_tempdir(self)))}):
            self.assertEqual(install.launchbox_bios_dirs(root)["xemu"], emu / "bios")

    def test_xemu_honours_configured_bootrom(self):
        root = self._make([("Xemu", "Emulators\\Xemu\\xemu.exe")])
        emu = root / "Emulators" / "Xemu"
        configured = Path(_tempdir(self)) / "xbox"
        configured.mkdir()
        (configured / "mcpx_1.0.bin").write_text("x")
        (emu / "xemu.toml").write_text(
            "[sys.files]\n"
            f"bootrom_path = '{configured / 'mcpx_1.0.bin'}'\n"
        )
        self.assertEqual(install.launchbox_bios_dirs(root)["xemu"], configured)

    def test_dolphin_portable_user_folder(self):
        root = self._make([("Dolphin", "Emulators\\Dolphin\\Dolphin.exe")])
        emu = root / "Emulators" / "Dolphin"
        (emu / "portable.txt").write_text("")
        self.assertEqual(install.launchbox_bios_dirs(root)["dolphin"], emu / "User")

    def test_dolphin_without_portable_marker_is_absent(self):
        root = self._make([("Dolphin", "Emulators\\Dolphin\\Dolphin.exe")])
        (root / "Emulators" / "Dolphin" / "User").mkdir()
        self.assertNotIn("dolphin", install.launchbox_bios_dirs(root))

    def test_unknown_emulators_are_ignored(self):
        root = self._make([("Cemu", "Emulators\\Cemu\\Cemu.exe")])
        self.assertEqual(install.launchbox_bios_dirs(root), {})

    def test_doctype_is_rejected(self):
        root = Path(_tempdir(self)) / "LaunchBox"
        (root / "Data").mkdir(parents=True)
        (root / "Data" / "Emulators.xml").write_text(
            "<!DOCTYPE LaunchBox [<!ENTITY a 'x'>]>\n<LaunchBox></LaunchBox>\n"
        )
        self.assertEqual(install.launchbox_emulators(root / "Data" / "Emulators.xml"), {})


class TestStandaloneCopiesExtraDirs(unittest.TestCase):
    """Setups that keep emulators outside the default locations."""

    def _manifest(self) -> dict:
        return {
            "standalone_copies": [
                {
                    "pattern": "ps2-*.bin",
                    "emulator": "pcsx2",
                    "targets": {"windows": []},
                },
                {
                    "file": "GC/USA/IPL.bin",
                    "emulator": "dolphin",
                    "targets": {"windows": []},
                },
            ]
        }

    def test_pattern_entry_copies_into_extra_dir(self):
        bios = Path(_tempdir(self))
        (bios / "ps2-0230a.bin").write_text("rom")
        extra = Path(_tempdir(self)) / "bios"
        extra.mkdir()
        copied, _, _ = install.do_standalone_copies(
            self._manifest(), bios, "windows", {"pcsx2": extra}
        )
        self.assertEqual(copied, 1)
        self.assertTrue((extra / "ps2-0230a.bin").exists())

    def test_file_entry_keeps_its_subdirectory(self):
        bios = Path(_tempdir(self))
        (bios / "GC" / "USA").mkdir(parents=True)
        (bios / "GC" / "USA" / "IPL.bin").write_text("ipl")
        user = Path(_tempdir(self)) / "User"
        (user / "GC" / "USA").mkdir(parents=True)
        copied, _, _ = install.do_standalone_copies(
            self._manifest(), bios, "windows", {"dolphin": user}
        )
        self.assertEqual(copied, 1)
        self.assertTrue((user / "GC" / "USA" / "IPL.bin").exists())

    def test_no_extra_dirs_is_unchanged(self):
        bios = Path(_tempdir(self))
        (bios / "ps2-0230a.bin").write_text("rom")
        self.assertEqual(
            install.do_standalone_copies(self._manifest(), bios, "windows"), (0, 0, 0)
        )


class StandaloneCopiesReadWhereThePackPutTheFile(unittest.TestCase):
    """The registry names Citra/sysdata/aes_keys.txt for every platform,
    but EmuDeck's manifest installs it at the root. The installer looked under
    the registry's path, found nothing and copied nothing to Azahar."""

    def _copy(self, entry: dict) -> tuple[Path, tuple[int, int, int]]:
        bios = Path(_tempdir(self))
        (bios / "aes_keys.txt").write_text("keys")
        sysdata = Path(_tempdir(self)) / "sysdata"
        sysdata.mkdir()
        manifest = {"standalone_copies": [
            {**entry, "emulator": "azahar", "targets": {"linux": [str(sysdata)]}},
        ]}
        return sysdata, install.do_standalone_copies(manifest, bios, "linux")

    def test_the_source_the_manifest_names_is_copied(self):
        sysdata, (copied, _, _) = self._copy(
            {"file": "Citra/sysdata/aes_keys.txt", "source": "aes_keys.txt"}
        )
        self.assertEqual(copied, 1)
        self.assertEqual((sysdata / "aes_keys.txt").read_text(), "keys")

    def test_without_a_source_the_file_path_is_read(self):
        _, (copied, _, _) = self._copy({"file": "Citra/sysdata/aes_keys.txt"})
        self.assertEqual(copied, 0)

    def test_an_unsafe_source_is_refused_at_load(self):
        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "install" / "emudeck.json").read_text()
        )
        manifest["standalone_copies"] = [
            {"file": "aes_keys.txt", "source": "../../etc/passwd",
             "targets": {"linux": ["/tmp"]}},
        ]
        with self.assertRaisesRegex(ValueError, r"standalone_copies\[0\]\.source"):
            install._validate_manifest(manifest, "emudeck")


class DetectionFollowsTheRegistry(unittest.TestCase):
    """The registry declared RetroPie at ~/RetroPie/BIOS and BizHawk at
    ~/.config/BizHawk/Firmware, every manifest published it, and the
    installer never read it: such a home detected nothing."""

    def test_the_table_is_the_registry(self):
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "platforms" / "_registry.yml").read_text()
        )
        rules = {
            (rule["os"], name, rule["path"], rule.get("bios_path"))
            for name, entry in registry["platforms"].items()
            for rule in (entry.get("install") or {}).get("detect", [])
            if rule["method"] == "path_exists"
        }
        self.assertEqual(set(install._PATH_RULES), rules)

    def test_the_embedded_table_is_the_registry(self):
        """Only the path rules were compared: Knulli, muOS, ArkOS and the
        Recalbox version file were detected by the installer and absent
        from every manifest's `detect`, and ROCKNIX was declared another
        way. A changed bios_path in the registry reached no installer."""
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "platforms" / "_registry.yml").read_text()
        )
        rules = set()
        for name, entry in registry["platforms"].items():
            for rule in (entry.get("install") or {}).get("detect", []):
                if rule["method"] == "os_release":
                    rules.add((name, "os_release", tuple(rule["match"]), rule["bios_path"]))
                elif rule["method"] == "file_exists":
                    files = rule["file"] if isinstance(rule["file"], list) else [rule["file"]]
                    rules.add((name, "file_exists", tuple(files), rule["bios_path"]))
        self.assertEqual(set(install._EMBEDDED_RULES), rules)

    def test_every_config_file_read_is_published(self):
        """The registry published five RetroArch config files and the fixed
        RetroDECK folder, but the installer also read Snap, the older macOS file,
        C:\\RetroArch, Program Files, ES-DE, Steam and RetroDECK's own
        config. A manifest reader saw a subset of what the installer does."""
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "platforms" / "_registry.yml").read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "HOME": str(root / "home"), "APPDATA": str(root / "appdata"),
                "ProgramFiles": str(root / "pf"), "ProgramFiles(x86)": str(root / "pf86"),
            }

            def expand(text: str) -> str:
                text = text.replace("$HOME", env["HOME"])
                for var in ("ProgramFiles(x86)", "ProgramFiles", "APPDATA"):
                    text = text.replace(f"%{var}%", env[var])
                return str(Path(text.replace("\\", "/")))

            published = {
                (rule["os"], expand(rule["config"]))
                for entry in registry["platforms"].values()
                for rule in (entry.get("install") or {}).get("detect", [])
                if rule["method"] == "config_file"
            }
            read: set[tuple[str, str]] = set()

            def record(os_type: str):
                def probe(path, *_args, **_kwargs):
                    read.add((os_type, str(Path(str(path).replace("\\", "/")))))
                    return None
                return probe

            with unittest.mock.patch.dict(os.environ, env), \
                    unittest.mock.patch.object(install, "_detect_embedded", lambda: []), \
                    unittest.mock.patch.object(install, "_detect_android", lambda: []):
                for os_type in ("linux", "darwin", "windows"):
                    with unittest.mock.patch.object(
                        install, "_parse_retroarch_system_dir", record(os_type)
                    ):
                        install.detect_platforms(os_type)
                with unittest.mock.patch.object(Path, "is_file", lambda _self: True):
                    for folder in install._windows_retroarch_dirs():
                        read.add(("windows", str(folder / "retroarch.cfg")))
            retrodeck = root / "home/.var/app/net.retrodeck.retrodeck/config/retrodeck"
            read |= {("linux", str(retrodeck / "retrodeck.json")),
                     ("linux", str(retrodeck / "retrodeck.cfg"))}
            # The default Steam library is the Program Files one, already read.
            self.assertEqual(sorted(read - published), [])

    def test_the_first_embedded_match_wins(self):
        with unittest.mock.patch.object(install, "_parse_os_release", return_value={}), \
                unittest.mock.patch.object(
                    Path, "exists",
                    lambda p: str(p) in ("/usr/share/knulli/knulli.version",
                                       "/usr/share/batocera/batocera.version"),
                ):
            self.assertEqual(install._detect_embedded(), [("batocera", Path("/userdata/bios"))])
        # What ROCKNIX writes since 2025: OS_NAME and HW_DEVICE, no ID.
        with unittest.mock.patch.object(install, "_parse_os_release",
                                        return_value={"OS_NAME": "ROCKNIX", "HW_DEVICE": "RK3566"}):
            self.assertEqual(
                install._detect_embedded(), [("rocknix", Path("/storage/roms/bios"))]
            )

    def _home(self) -> Path:
        home = Path(_tempdir(self))
        patcher = unittest.mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def test_a_home_layout_is_detected(self):
        home = self._home()
        (home / "RetroPie" / "BIOS").mkdir(parents=True)
        found: list = []
        install._detect_by_path("linux", found)
        self.assertIn(("retropie", home / "RetroPie" / "BIOS"), found)

    def test_bizhawk_is_found_by_its_executable(self):
        """~/.config/BizHawk/Firmware is a folder no build reads: the
        archive extracted to ~/Games/BizHawk-2.11.1 reads the Firmware folder
        beside EmuHawkMono.sh."""
        home = self._home()
        extracted = home / "Games" / "BizHawk-2.11.1"
        extracted.mkdir(parents=True)
        (extracted / "EmuHawkMono.sh").write_text("#!/bin/sh\n")
        found: list = []
        install._detect_by_executable("linux", found)
        self.assertEqual(found, [("bizhawk", extracted / "Firmware")])
        self.assertNotIn("bizhawk", install._os_default_dests("linux"))
        self.assertNotIn("bizhawk", install._os_default_dests("windows"))

    def test_a_portable_platform_is_not_guessed(self):
        """--platform bizhawk with no install found wrote 924 files into
        ~/.config/BizHawk/Firmware, then detected that folder next time."""
        home = self._home()
        with unittest.mock.patch.object(sys, "argv", ["install.py", "--platform", "bizhawk"]), \
                unittest.mock.patch.object(install, "detect_os", return_value="linux"), \
                unittest.mock.patch.object(install, "detect_platforms", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as stop:
            install.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("--dest", err.getvalue())
        self.assertFalse((home / ".config" / "BizHawk").exists())

    def test_the_executable_table_is_the_registry(self):
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "platforms" / "_registry.yml").read_text()
        )
        rules = {
            (rule["os"], name, tuple(rule["file"]), rule["bios_subdir"])
            for name, entry in registry["platforms"].items()
            for rule in (entry.get("install") or {}).get("detect", [])
            if rule["method"] == "executable"
        }
        self.assertEqual(set(install._EXE_RULES), rules)

    def test_a_windows_profile_path_is_detected(self):
        home = self._home()
        (home / "BizHawk" / "Firmware").mkdir(parents=True)
        found: list = []
        install._detect_by_path("windows", found)
        self.assertEqual(found, [("bizhawk", home / "BizHawk" / "Firmware")])

    def test_a_platform_already_found_is_not_added_again(self):
        home = self._home()
        (home / "Emulation" / "bios").mkdir(parents=True)
        found = [("emudeck", home / "elsewhere" / "bios")]
        install._detect_by_path("linux", found)
        self.assertEqual(found, [("emudeck", home / "elsewhere" / "bios")])

    def test_emudeck_on_macos_is_found_by_its_settings(self):
        """EmuDeck's macOS build reads ~/.config/EmuDeck/settings.sh as the
        Linux one does (functions/all.sh:3-4,24, vars.sh:2): a Mac with
        EmuDeck detected nothing and was offered RetroArch's folder."""
        home = self._home()
        settings = home / ".config" / "EmuDeck" / "settings.sh"
        settings.parent.mkdir(parents=True)
        settings.write_text(f"emulationPath={home / 'Games' / 'Emulation'}\n")
        self.assertIn(("emudeck", home / "Games" / "Emulation" / "bios"),
                      install.detect_platforms("darwin"))
        settings.unlink()
        (home / "Emulation" / "bios").mkdir(parents=True)
        self.assertIn(("emudeck", home / "Emulation" / "bios"),
                      install.detect_platforms("darwin"))


class RetroArchIsFoundWhereWindowsInstallsPutIt(unittest.TestCase):
    """Program Files\\RetroArch-Win64, a Steam library on another drive and
    an installed ES-DE's Emulators folder all held a RetroArch the one-liner
    reported as "no supported platform detected"."""

    def _env(self, program_files: Path) -> None:
        patcher = unittest.mock.patch.dict(
            os.environ, {"ProgramFiles": str(program_files), "ProgramFiles(x86)": ""}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _exe(folder: Path) -> Path:
        folder.mkdir(parents=True)
        (folder / "retroarch.exe").write_bytes(b"MZ")
        return folder

    def test_program_files_win64(self):
        program_files = Path(_tempdir(self))
        self._env(program_files)
        found = self._exe(program_files / "RetroArch-Win64")
        self.assertIn(found, install._windows_retroarch_dirs())

    def test_a_scoop_install(self):
        # `scoop install retroarch` puts it under ~/scoop/apps/retroarch/current,
        # which none of the searched folders covered.
        program_files = Path(_tempdir(self))
        self._env(program_files)
        home = Path(_tempdir(self))
        found = self._exe(home / "scoop" / "apps" / "retroarch" / "current")
        with unittest.mock.patch.dict(os.environ, {"USERPROFILE": str(home)}):
            os.environ.pop("SCOOP", None)
            self.assertIn(found, install._windows_retroarch_dirs())

    def test_a_steam_library_on_another_drive(self):
        program_files = Path(_tempdir(self))
        library = Path(_tempdir(self))
        steamapps = program_files / "Steam" / "steamapps"
        steamapps.mkdir(parents=True)
        (steamapps / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"path"\t\t"' + str(library) + '"\n\t}\n}\n'
        )
        self._env(program_files)
        found = self._exe(library / "steamapps" / "common" / "RetroArch")
        self.assertIn(found, install._windows_retroarch_dirs())

    def test_an_installed_es_de(self):
        program_files = Path(_tempdir(self))
        self._env(program_files)
        found = self._exe(program_files / "ES-DE" / "Emulators" / "RetroArch-Win64")
        self.assertIn(found, install._windows_retroarch_dirs())


class InstallerStaysOnPython38AndTheStdlib(unittest.TestCase):
    """install.py runs where the bootstraps let it run: Python 3.8, stdlib only.

    The suite runs on 3.12 with pyyaml installed, so a removeprefix() or an
    import of yaml kept every test green and died at the user's first run,
    after the bootstrap had told them 3.8 was enough.
    """

    SOURCE = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_the_grammar_is_python_38(self):
        import ast  # noqa: PLC0415

        ast.parse(self.SOURCE, filename="install.py", feature_version=(3, 8))

    def test_every_import_is_standard_library(self):
        import ast  # noqa: PLC0415
        import sys  # noqa: PLC0415

        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(sorted(imported - set(sys.stdlib_module_names)), [])

    def test_no_api_newer_than_38(self):
        newer = (".removeprefix(", ".removesuffix(", "zoneinfo", "graphlib",
                 "functools.cache(", ".bit_count(", "math.lcm(", "math.nextafter(",
                 "tomllib", ".is_relative_to(", ".with_stem(", ".hardlink_to(",
                 "itertools.pairwise(", "str.removeprefix", "types.NoneType")
        used = [api for api in newer if api in self.SOURCE]
        self.assertEqual(used, [])

    def test_a_python_38_interpreter_compiles_and_imports_it(self):
        """ast.parse(feature_version=(3, 8)) on 3.12 accepts parenthesized
        context managers and PEP 701 f-strings: only a 3.8 interpreter says."""
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        python38 = shutil.which("python3.8")
        if python38 is None:
            self.skipTest("no python3.8 on PATH, the static checks below still run")
        code = (
            "import importlib.util; "
            "spec = importlib.util.spec_from_file_location('install', 'install.py'); "
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))"
        )
        proc = subprocess.run(
            [python38, "-I", "-c", code], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_python_38_interpreter_runs_it_end_to_end(self):
        """An import proves the grammar, not the calls a function makes.

        dict | dict, zip(strict=True) or functools.cache import cleanly on
        3.8 and raise the first time their path runs: listing, narrowing by
        system, region and target, installing and checking all run here, on
        the repository's own manifests served over loopback. CI installs the
        interpreter, so there it may not skip.
        """
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        python38 = shutil.which("python3.8")
        if python38 is None:
            if os.environ.get("CI"):
                self.fail("CI must provide python3.8 for the installer's floor")
            self.skipTest("no python3.8 on PATH, the static checks below still run")
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(REPO_ROOT)
        )
        handler.log_message = lambda *args: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        home = Path(tempfile.mkdtemp(dir=str(REPO_ROOT / "tmp")))
        self.addCleanup(shutil.rmtree, home, True)
        env = {
            **os.environ,
            "HOME": str(home),
            "RETROBIOS_OS": "linux",
            "RETROBIOS_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        }
        dest = home / "system"
        base = [python38, "-I", "install.py", "--platform", "retroarch", "--no-input"]
        for extra in (
            ["--list-systems"],
            ["--list-cores"],
            ["--list-targets"],
            ["--dest", str(dest), "--system", "atari-lynx", "--region", "us",
             "--target", "switch", "--yes"],
            ["--dest", str(dest), "--system", "atari-lynx", "--check"],
        ):
            with self.subTest(args=extra):
                proc = subprocess.run(
                    base + extra, cwd=REPO_ROOT, env=env,
                    capture_output=True, text=True, timeout=300, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
        self.assertTrue((dest / "lynxboot.img").is_file())
        # A check is scripted as `--check && launch`: missing files fail it.
        proc = subprocess.run(
            base + ["--dest", str(home / "empty"), "--system", "atari-lynx", "--check"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
            check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("to download:", proc.stdout)

    def test_no_syntax_a_312_parser_lets_through(self):
        """Parenthesized context managers (3.9) and f-strings reusing their
        own quote or a backslash inside a replacement field (3.12)."""
        import io  # noqa: PLC0415
        import tokenize  # noqa: PLC0415

        found = []
        tokens = list(tokenize.generate_tokens(io.StringIO(self.SOURCE).readline))
        for i, tok in enumerate(tokens):
            if tok.type == tokenize.NAME and tok.string == "with" and i + 1 < len(tokens):
                if tokens[i + 1].string != "(":
                    continue
                depth = 0
                for inner in tokens[i + 1:]:
                    if inner.string in "([{" and inner.type == tokenize.OP:
                        depth += 1
                    elif inner.string in ")]}" and inner.type == tokenize.OP:
                        depth -= 1
                        if depth == 0:
                            break
                    elif depth == 1 and inner.type == tokenize.NAME and inner.string == "as":
                        found.append(f"line {tok.start[0]}: parenthesized with")
                        break
        fstring_start = getattr(tokenize, "FSTRING_START", None)
        if fstring_start is not None:
            stack = []
            for tok in tokens:
                if tok.type == fstring_start:
                    if stack:
                        found.append(f"line {tok.start[0]}: f-string inside an f-string")
                    stack.append(tok.string[-1])
                elif tok.type == tokenize.FSTRING_END:
                    stack.pop()
                elif stack and tok.type == tokenize.STRING and stack[-1] in tok.string:
                    found.append(f"line {tok.start[0]}: f-string reuses its quote")
                elif stack and tok.type == tokenize.FSTRING_MIDDLE:
                    continue
                elif stack and tok.type == tokenize.ERRORTOKEN and tok.string == "\\":
                    found.append(f"line {tok.start[0]}: backslash in an f-string field")
        self.assertEqual(found, [])

    def test_no_runtime_generic_builtin(self):
        """dict[str, Path] outside an annotation runs on 3.9 and later only."""
        import ast  # noqa: PLC0415

        tree = ast.parse(self.SOURCE)
        annotations = set()
        for node in ast.walk(tree):
            for field in ("annotation", "returns"):
                sub = getattr(node, field, None)
                if sub is not None:
                    annotations.update(id(n) for n in ast.walk(sub))
        generic = {"dict", "list", "tuple", "set", "frozenset", "type"}
        used = sorted(
            f"line {node.lineno}: {node.value.id}[...]"
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in generic
            and id(node) not in annotations
        )
        self.assertEqual(used, [])


    def test_no_local_is_assigned_and_never_read(self):
        """ruff --select F in py38 mode is the installer's lint gate, and an
        unread `appdata` in _os_default_dests made it exit 1. The suite has
        no ruff, so the rule is read from the syntax tree."""
        import ast  # noqa: PLC0415

        tree = ast.parse((Path(__file__).resolve().parents[1] / "install.py").read_text())
        unread = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stored, loaded = {}, set()
            for node in ast.walk(func):
                if isinstance(node, ast.Name):
                    if isinstance(node.ctx, ast.Store):
                        stored.setdefault(node.id, node.lineno)
                    else:
                        loaded.add(node.id)
            nested = {
                n.id for inner in ast.walk(func) if inner is not func
                and isinstance(inner, (ast.FunctionDef, ast.Lambda))
                for n in ast.walk(inner) if isinstance(n, ast.Name)
            }
            unread += [
                f"{func.name}:{line} {name}" for name, line in stored.items()
                if name not in loaded | nested and not name.startswith("_")
                and not any(
                    isinstance(g, (ast.Global, ast.Nonlocal)) and name in g.names
                    for g in ast.walk(func)
                )
            ]
        self.assertEqual(unread, [])
        # Nor a module rebound: a loop variable named `platform` shadowed the
        # platform module in _detect_by_path (F402).
        modules = {
            alias.asname or alias.name.split(".")[0]
            for node in tree.body if isinstance(node, ast.Import)
            for alias in node.names
        }
        rebound = sorted(
            f"{node.id}:{node.lineno}" for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            and node.id in modules
        )
        self.assertEqual(rebound, [])

class ARefusedCopyIsAnError(unittest.TestCase):
    """A PermissionError on a standalone copy was counted as "dir not found"
    and the run ended on Done with exit code 0."""

    def test_a_refused_copy_is_counted_failed_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bios = Path(tmp, "bios")
            bios.mkdir()
            (bios / "prod.keys").write_bytes(b"k")
            target = Path(tmp, "keys")
            target.mkdir()
            manifest = {"standalone_copies": [
                {"file": "prod.keys", "targets": {"linux": [str(target)]}}
            ]}
            with unittest.mock.patch("shutil.copy2", side_effect=PermissionError(13, "denied")):
                result = install.do_standalone_copies(manifest, bios, "linux")
        self.assertEqual(result, (0, 0, 1))


class TargetFlagIsAppliedOrRefused(unittest.TestCase):
    """A filter the installer cannot apply must stop it, not be ignored.

    An unknown target used to print a warning and then install every file:
    4 GB where the user asked for a subset. And the aliases the pack builder
    accepts -- switch, ps2, psp -- were absent from the published target
    manifests, so the documented word was the one that failed.
    """

    def test_the_published_manifests_carry_the_builder_aliases(self):
        path = REPO_ROOT / "install" / "targets" / "retroarch.json"
        if not path.is_file():
            self.skipTest("no target manifest generated yet")
        targets = json.loads(path.read_text())
        self.assertIn("nintendo-switch", targets)
        for alias in ("switch", "nx"):
            with self.subTest(alias=alias):
                self.assertIn(alias, targets, f"builder accepts --target {alias}")
                self.assertEqual(targets[alias], targets["nintendo-switch"])

    def test_every_alias_resolves_to_its_canonical_core_list(self):
        import sys as _sys

        _sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from common import load_target_config

        for name in ("install/targets/retroarch.json",):
            path = REPO_ROOT / name
            if not path.is_file():
                self.skipTest(f"{name} not generated yet")
            targets = json.loads(path.read_text())
            for alias in ("switch", "ps2", "psp"):
                # An alias the builder accepts and the manifest lacks is a
                # documented word that fails on the installer's side.
                self.assertIn(alias, targets, f"builder accepts --target {alias}")
                with self.subTest(alias=alias):
                    resolved = load_target_config(
                        "retroarch", alias, str(REPO_ROOT / "platforms")
                    )
                    self.assertEqual(
                        sorted(targets[alias] or []),
                        sorted(resolved),
                        f"{alias} disagrees with what the builder resolves",
                    )

    def test_every_published_target_is_the_list_the_builder_resolves(self):
        """add_cores and remove_cores reach the installer, not only the builder.

        The published lists were the scraped ones, untouched by the
        overrides: Batocera x86_64 lacked citron and xenia, which the pack
        for that target ships, and carried the Amiga machine variants and
        the launchers the builder drops.

        Both ways: a target the builder knows and the installer lacks answers
        "unknown target" on one side only, and a published null left behind
        once the builder filters a target makes the installer serve every core.
        """
        import sys as _sys

        _sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from common import list_available_targets, load_target_config

        platforms = str(REPO_ROOT / "platforms")
        sources = sorted(
            path.stem for path in (REPO_ROOT / "platforms" / "targets").glob("*.yml")
            if not path.name.startswith("_")
        )
        manifests = {
            path.stem: path for path in (REPO_ROOT / "install" / "targets").glob("*.json")
        }
        if not manifests:
            self.skipTest("no target manifest generated yet")
        self.assertEqual(sorted(manifests), sources)
        for platform in sources:
            targets = json.loads(manifests[platform].read_text())
            known = {
                name
                for target in list_available_targets(platform, platforms)
                for name in (target["name"], *target["aliases"])
            }
            with self.subTest(platform=platform):
                self.assertEqual(sorted(targets), sorted(known))
            for target, cores in targets.items():
                with self.subTest(platform=platform, target=target):
                    resolved = load_target_config(platform, target, platforms)
                    self.assertEqual(cores, None if resolved is None else sorted(resolved))


class TestBaseUrlScheme(unittest.TestCase):
    """The bootstraps insist on HTTPS, so the installer must not be laxer."""

    def test_https_is_accepted(self):
        self.assertEqual(
            install._checked_base_url("https://example.test/repo"),
            "https://example.test/repo",
        )

    def test_plain_http_is_refused(self):
        with self.assertRaises(SystemExit):
            install._checked_base_url("http://example.test/repo")

    def test_local_file_scheme_is_refused(self):
        with self.assertRaises(SystemExit):
            install._checked_base_url("file:///etc")

    def test_loopback_http_is_allowed_for_end_to_end_runs(self):
        for url in (
            "http://127.0.0.1:8080/repo",
            "http://localhost:8080/repo",
            "http://[::1]:8080/repo",
        ):
            self.assertEqual(install._checked_base_url(url), url)


class TestLocalCheckHashing(unittest.TestCase):
    """A file is read once, not once per declared digest."""

    def test_both_digests_come_from_one_read(self):
        tmp = Path(_tempdir(self))
        payload = b"BIOS PAYLOAD"
        (tmp / "boot.bin").write_bytes(payload)
        entry = {
            "dest": "boot.bin",
            "size": len(payload),
            "sha1": hashlib.sha1(payload).hexdigest(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        calls = []
        real_digest = install._digest_file

        def recording(path, algorithms):
            calls.append(tuple(sorted(algorithms)))
            return real_digest(path, algorithms)

        install._digest_file = recording
        try:
            to_download, up_to_date, mismatched = install.check_local([entry], tmp)
        finally:
            install._digest_file = real_digest
        self.assertEqual((len(to_download), len(up_to_date), len(mismatched)), (0, 1, 0))
        self.assertEqual(calls, [("sha1", "sha256")], "file must be read once")

    def test_a_wrong_digest_still_lands_in_mismatched(self):
        tmp = Path(_tempdir(self))
        (tmp / "boot.bin").write_bytes(b"BIOS PAYLOAD")
        entry = {
            "dest": "boot.bin",
            "size": 12,
            "sha1": "0" * 40,
            "sha256": hashlib.sha256(b"BIOS PAYLOAD").hexdigest(),
        }
        _, up_to_date, mismatched = install.check_local([entry], tmp)
        self.assertEqual((len(up_to_date), len(mismatched)), (0, 1))


class TestStandaloneCopyTargetsAreUntrusted(unittest.TestCase):
    """The manifest names these directories, so it does not get to escape them."""

    def _manifest(self, target: str) -> dict:
        return {
            "standalone_copies": [
                {"file": "boot.bin", "targets": {"linux": [target]}}
            ]
        }

    def test_traversal_in_a_target_is_refused_by_validation(self):
        manifest = {
            "manifest_version": 2,
            "platform": "retroarch",
            "files": [],
            "standalone_copies": [
                {"file": "boot.bin", "targets": {"linux": ["~/emu/../../../etc"]}}
            ],
        }
        with self.assertRaises(ValueError):
            install._validate_manifest(manifest, "retroarch")

    def test_nul_in_a_target_is_refused_by_validation(self):
        manifest = {
            "manifest_version": 2,
            "platform": "retroarch",
            "files": [],
            "standalone_copies": [
                {"file": "boot.bin", "targets": {"linux": ["/tmp/a\x00b"]}}
            ],
        }
        with self.assertRaises(ValueError):
            install._validate_manifest(manifest, "retroarch")

    def test_entries_the_copy_step_cannot_read_are_refused(self):
        """Each passed validation and crashed do_standalone_copies after the
        download: a list for detect, no file nor pattern, a list for emulator."""
        for entry in (
            {"note": "hi", "detect": ["/x"]},
            {"emulator": "pcsx2", "targets": {"linux": ["/tmp"]}},
            {"file": "bios/x.bin", "emulator": ["a"], "targets": {"linux": ["/tmp"]}},
        ):
            manifest = {"manifest_version": 2, "platform": "retroarch", "files": [],
                        "standalone_copies": [entry]}
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                install._validate_manifest(manifest, "retroarch")

    def test_plain_absolute_target_still_works(self):
        manifest = {
            "manifest_version": 2,
            "platform": "retroarch",
            "files": [],
            "standalone_copies": [
                {"file": "boot.bin", "targets": {"linux": ["~/.config/emu/bios"]}}
            ],
        }
        self.assertIsInstance(install._validate_manifest(manifest, "retroarch"), dict)

    def test_symlinked_destination_is_not_written_through(self):
        """A link at the name was skipped in silence and counted as "dir
        not found" while the folder existed: the emulator kept whatever the
        link pointed at. The link now gives way to the file."""
        bios = Path(_tempdir(self))
        (bios / "boot.bin").write_text("rom")
        target = Path(_tempdir(self))
        outside = Path(_tempdir(self)) / "secret"
        outside.write_text("untouched")
        (target / "boot.bin").symlink_to(outside)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            copied, skipped, failed = install.do_standalone_copies(
                self._manifest(str(target)), bios, "linux"
            )
        self.assertEqual((copied, skipped, failed), (1, 0, 0))
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse((target / "boot.bin").is_symlink())
        self.assertEqual((target / "boot.bin").read_text(), "rom")
        self.assertIn("link replaced", out.getvalue())
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["boot.bin"])


@unittest.skipUnless(shutil.which("pwsh"), "pwsh not available")
class TestDestinationFollowsUserLinks(unittest.TestCase):
    """EmuDeck links bios/shadps4/sys_modules into shadPS4's own directory.

    The manifest still cannot climb out of the BIOS root, but a link the
    user's frontend placed under it is where the emulator reads the file.
    """

    def test_symlinked_directory_under_root_is_followed(self):
        bios = Path(_tempdir(self))
        elsewhere = Path(_tempdir(self)) / "sys_modules"
        elsewhere.mkdir()
        (bios / "shadps4").mkdir()
        (bios / "shadps4" / "sys_modules").symlink_to(elsewhere)

        dest = install._destination_path(bios, "shadps4/sys_modules/libSceLibcInternal.sprx")

        self.assertEqual(dest, bios.resolve() / "shadps4" / "sys_modules" / "libSceLibcInternal.sprx")
        self.assertEqual(dest.resolve().parent, elsewhere.resolve())

    def test_check_local_survives_a_linked_directory(self):
        bios = Path(_tempdir(self))
        elsewhere = Path(_tempdir(self)) / "sys_modules"
        elsewhere.mkdir()
        (bios / "shadps4").mkdir()
        (bios / "shadps4" / "sys_modules").symlink_to(elsewhere)
        entry = {"dest": "shadps4/sys_modules/x.sprx", "size": 1, "sha1": "0" * 40}

        to_download, up_to_date, mismatched = install.check_local([entry], bios)

        self.assertEqual([e["dest"] for e in to_download], ["shadps4/sys_modules/x.sprx"])
        self.assertEqual((up_to_date, mismatched), ([], []))

    def test_climbing_is_still_refused(self):
        bios = Path(_tempdir(self))
        with self.assertRaises(ValueError):
            install._destination_path(bios, "../outside.bin")
        with self.assertRaises(ValueError):
            install._destination_path(bios, "/etc/passwd")


class TestDetectedPlatformPrompt(unittest.TestCase):
    """With two platforms detected the menu had no quit entry, and q or any
    wrong answer brought back a bare prompt."""

    PLATFORMS = [("emudeck", Path("/e")), ("retroarch", Path("/r"))]

    def _answer(self, *answers: str):
        replies = iter(answers)
        out = io.StringIO()
        with unittest.mock.patch("builtins.input", lambda _prompt="": next(replies)), \
                contextlib.redirect_stdout(out):
            try:
                result = install._prompt_platform_choice(self.PLATFORMS)
            except SystemExit as exc:
                result = exc
        return result, out.getvalue()

    def test_q_quits_and_the_menu_says_so(self):
        result, out = self._answer("q")
        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 0)
        self.assertIn("q) quit", out)

    def test_a_wrong_answer_names_the_choices(self):
        result, out = self._answer("x", "2")
        self.assertEqual(result, [self.PLATFORMS[1]])
        self.assertIn("Type a number from 1 to 3, or q to quit.", out)

    def test_all_installs_every_platform(self):
        result, _ = self._answer("3")
        self.assertEqual(result, self.PLATFORMS)


class TestManualPlatformPrompt(unittest.TestCase):
    """With nothing detected, an interactive run asks instead of exiting."""

    def test_number_picks_the_platform_and_default_directory(self):
        answers = iter(["1", ""])
        with unittest.mock.patch("builtins.input", lambda _prompt="": next(answers)):
            chosen = install._prompt_manual_platform("linux")
        self.assertEqual(
            chosen, [("retroarch", Path.home() / ".config" / "retroarch" / "system")]
        )

    def test_name_and_explicit_directory(self):
        answers = iter(["batocera", "~/my bios"])
        with unittest.mock.patch("builtins.input", lambda _prompt="": next(answers)):
            chosen = install._prompt_manual_platform("linux")
        self.assertEqual(chosen, [("batocera", Path("~/my bios").expanduser())])

    def test_default_directory_comes_from_the_table(self):
        answers = iter(["batocera", ""])
        with unittest.mock.patch("builtins.input", lambda _prompt="": next(answers)):
            chosen = install._prompt_manual_platform("linux")
        self.assertEqual(chosen, [("batocera", install.DEFAULT_DESTS["batocera"])])

    def test_quit_exits_cleanly(self):
        with unittest.mock.patch("builtins.input", lambda _prompt="": "q"):
            with self.assertRaises(SystemExit) as raised:
                install._prompt_manual_platform("linux")
        self.assertEqual(raised.exception.code, 0)

    def test_a_wrong_answer_says_what_to_type(self):
        answers = iter(["99", "1", ""])
        out = io.StringIO()
        with unittest.mock.patch("builtins.input", lambda _prompt="": next(answers)), \
                contextlib.redirect_stdout(out):
            install._prompt_manual_platform("linux")
        self.assertIn("or q to quit", out.getvalue())

    def test_windows_hint_shows_the_one_liner_with_arguments(self):
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_BOOTSTRAP": "ps1"}):
            hint = "\n".join(install._manual_usage_hint("windows"))
        self.assertIn("--platform retroarch --dest", hint)
        self.assertIn("install.ps1", hint)


class TestSteamRetroarchDetection(unittest.TestCase):
    """The Steam build is a portable install under the default library."""

    def test_steam_library_is_found_on_windows(self):
        program_files = Path(_tempdir(self))
        ra_dir = program_files / "Steam" / "steamapps" / "common" / "RetroArch"
        ra_dir.mkdir(parents=True)
        (ra_dir / "retroarch.cfg").write_text('system_directory = ":\\system"\n')
        env = {"ProgramFiles(x86)": str(program_files)}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            found = install.detect_platforms("windows")
        self.assertIn(("retroarch", ra_dir / "system"), found)


class TestLaunchboxDetectionPowershell(unittest.TestCase):
    """install.ps1 must resolve LaunchBox's portable RetroArch on its own."""

    def _run(self, entries: str, seed=None, manifest: dict | None = None):
        userprofile = Path(_tempdir(self))
        ra_dir = userprofile / "LaunchBox" / "Emulators" / "RetroArch"
        ra_dir.mkdir(parents=True)
        data = userprofile / "LaunchBox" / "Data"
        data.mkdir(parents=True)
        (data / "Emulators.xml").write_text(
            "<?xml version=\"1.0\" standalone=\"yes\"?>\n"
            "<LaunchBox>\n"
            f"{entries}"
            "</LaunchBox>\n"
        )
        if seed is not None:
            seed(userprofile)
        serve_root = Path(_tempdir(self))
        (serve_root / "install").mkdir()
        payload = dict(manifest if manifest is not None else {"files": []})
        payload.setdefault("manifest_version", 2)
        payload.setdefault("platform", "retroarch")
        payload.setdefault("files", [])
        (serve_root / "install" / "retroarch.json").write_text(json.dumps(payload))
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(serve_root)
        )
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ)
            env.update(
                USERPROFILE=str(userprofile),
                APPDATA=str(_tempdir(self)),
                RETROBIOS_BASE_URL=f"http://127.0.0.1:{httpd.server_address[1]}",
                # A LaunchBox layout is a Windows layout: the runner is Linux.
                RETROBIOS_OS="wsl",
            )
            proc = subprocess.run(
                [
                    "pwsh", "-NoProfile", "-File",
                    str(REPO_ROOT / "install.ps1"), "--standalone-copies",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            httpd.shutdown()
            httpd.server_close()
        return proc, ra_dir

    def _assert_resolved(self, proc, ra_dir):
        self.assertIn("Found LaunchBox", proc.stdout)
        self.assertIn("Found RetroArch at", proc.stdout)
        self.assertIn(str(ra_dir), proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Done.", proc.stdout)

    def test_detects_launchbox_retroarch(self):
        proc, ra_dir = self._run(
            "  <Emulator>\n"
            "    <Title>Retroarch</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n"
        )
        self._assert_resolved(proc, ra_dir)

    def test_stale_entry_before_installed_one(self):
        proc, ra_dir = self._run(
            "  <Emulator>\n"
            "    <Title>RetroArch (old)</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch-old\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n"
            "  <Emulator>\n"
            "    <Title>RetroArch</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n"
        )
        self._assert_resolved(proc, ra_dir)

    def test_system_directory_from_config(self):
        def seed(root: Path) -> None:
            ra = root / "LaunchBox" / "Emulators" / "RetroArch"
            (ra / "bios").mkdir(parents=True)
            (ra / "retroarch.cfg").write_text('system_directory = ":\\bios"\n')

        proc, ra_dir = self._run(
            "  <Emulator>\n"
            "    <Title>RetroArch</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n",
            seed=seed,
        )
        self.assertIn("Found LaunchBox", proc.stdout)
        self.assertIn("Found RetroArch at", proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(str(ra_dir / "bios"), proc.stdout)

    def test_standalone_copy_reaches_launchbox_emulator(self):
        def seed(root: Path) -> None:
            system = root / "LaunchBox" / "Emulators" / "RetroArch" / "system"
            system.mkdir(parents=True)
            (system / "ps2-0230a.bin").write_text("rom")
            pcsx2 = root / "LaunchBox" / "Emulators" / "PCSX2"
            (pcsx2 / "bios").mkdir(parents=True)
            (pcsx2 / "portable.ini").write_text("")
            (pcsx2 / "inis").mkdir()
            (pcsx2 / "inis" / "PCSX2.ini").write_text("[Folders]\nBios = bios\n")

        manifest = {
            "files": [],
            "standalone_copies": [
                {
                    "pattern": "ps2-*.bin",
                    "emulator": "pcsx2",
                    "targets": {"windows": []},
                }
            ],
        }
        proc, ra_dir = self._run(
            "  <Emulator>\n"
            "    <Title>RetroArch</Title>\n"
            "    <ApplicationPath>Emulators\\RetroArch\\retroarch.exe</ApplicationPath>\n"
            "  </Emulator>\n"
            "  <Emulator>\n"
            "    <Title>PCSX2</Title>\n"
            "    <ApplicationPath>Emulators\\PCSX2\\pcsx2.exe</ApplicationPath>\n"
            "  </Emulator>\n",
            seed=seed,
            manifest=manifest,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        landed = ra_dir.parent / "PCSX2" / "bios" / "ps2-0230a.bin"
        self.assertTrue(landed.exists(), proc.stdout)


class TestForcedOs(unittest.TestCase):
    """RETROBIOS_OS overrides detection with a known name only."""

    def test_known_name_wins(self):
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_OS": "Windows"}):
            self.assertEqual(install.detect_os(), "windows")

    def test_unknown_name_is_ignored(self):
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_OS": "amiga"}):
            self.assertIn(install.detect_os(), ("linux", "wsl", "windows", "darwin"))


class TestDetectFrontends(unittest.TestCase):
    """Frontends have no BIOS directory of their own but hint at the setup."""

    def test_esde_from_home_directory(self):
        home = Path(_tempdir(self))
        (home / "ES-DE").mkdir()
        old = os.environ.pop("ESDE_APPDATA_DIR", None)
        try:
            self.assertIn("esde", install.detect_frontends("linux", home=home))
        finally:
            if old is not None:
                os.environ["ESDE_APPDATA_DIR"] = old

    def test_esde_from_env_dir(self):
        home = Path(_tempdir(self))
        appdata = Path(_tempdir(self))
        os.environ["ESDE_APPDATA_DIR"] = str(appdata)
        try:
            self.assertIn("esde", install.detect_frontends("linux", home=home))
        finally:
            del os.environ["ESDE_APPDATA_DIR"]

    def test_nothing_detected(self):
        home = Path(_tempdir(self))
        old = os.environ.pop("ESDE_APPDATA_DIR", None)
        try:
            self.assertEqual(install.detect_frontends("linux", home=home), [])
        finally:
            if old is not None:
                os.environ["ESDE_APPDATA_DIR"] = old


class TestNormalizePlatform(unittest.TestCase):
    """Manifest URLs are case sensitive: user input must be normalized."""

    def test_lowercases_input(self):
        self.assertEqual(install.normalize_platform("Batocera"), "batocera")

    def test_strips_whitespace(self):
        self.assertEqual(install.normalize_platform(" retroarch "), "retroarch")

    def test_unknown_platform_exits(self):
        with self.assertRaises(SystemExit):
            install.normalize_platform("launchbox")

    def test_all_available_platforms_accepted(self):
        for plat in install.AVAILABLE_PLATFORMS:
            self.assertEqual(install.normalize_platform(plat), plat)


class TestAvailablePlatforms(unittest.TestCase):
    """The platform list must match the generated manifests."""

    def test_matches_install_manifests(self):
        manifests = {p.stem for p in (REPO_ROOT / "install").glob("*.json")}
        self.assertEqual(set(install.AVAILABLE_PLATFORMS), manifests)

    def test_every_registered_platform_is_installable(self):
        """Archived platforms still ship packs, so they still need a manifest."""
        import sys as _sys

        _sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from common import list_registered_platforms

        registered = set(
            list_registered_platforms(
                str(REPO_ROOT / "platforms"), include_archived=True
            )
        )
        missing = registered - set(install.AVAILABLE_PLATFORMS)
        self.assertEqual(missing, set(), f"no install manifest for {sorted(missing)}")

    def test_powershell_wrapper_pins_installer_hash(self):
        content = (REPO_ROOT / "install.ps1").read_text()
        match = re.search(r'\$defaultInstallSha256 = "([0-9a-f]{64})"', content)
        self.assertIsNotNone(match, "install.ps1 must embed the installer SHA256")
        expected = hashlib.sha256((REPO_ROOT / "install.py").read_bytes()).hexdigest()
        self.assertEqual(match.group(1), expected)

    def test_powershell_wrapper_rejects_non_https_bootstrap(self):
        content = (REPO_ROOT / "install.ps1").read_text()
        self.assertIn('$uri.Scheme -ne "https"', content)

    def test_shell_wrapper_pins_installer_hash(self):
        content = (REPO_ROOT / "install.sh").read_text()
        match = re.search(r'DEFAULT_INSTALL_SHA256="([0-9a-f]{64})"', content)
        self.assertIsNotNone(match, "install.sh must embed the installer SHA256")
        expected = hashlib.sha256((REPO_ROOT / "install.py").read_bytes()).hexdigest()
        self.assertEqual(match.group(1), expected)

    def test_shell_one_liner_downloads_verifies_and_forwards_arguments(self):
        scratch = REPO_ROOT / "tmp" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            # A piped script must not trust a same-named file in the caller's
            # working directory. Only a real local install.sh may use its peer.
            (root / "install.py").write_text(
                "raise SystemExit('untrusted cwd installer ran')\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            marker = root / "curl-called"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "output=\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
                "done\n"
                "cp -- \"$FAKE_INSTALLER_SOURCE\" \"$output\"\n"
                ": > \"$FAKE_CURL_MARKER\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
                TMPDIR=str(root),
                RETROBIOS_INSTALL_URL="https://example.invalid/install.py",
                RETROBIOS_INSTALL_SHA256=hashlib.sha256(
                    (REPO_ROOT / "install.py").read_bytes()
                ).hexdigest(),
                FAKE_INSTALLER_SOURCE=str(REPO_ROOT / "install.py"),
                FAKE_CURL_MARKER=str(marker),
            )
            proc = subprocess.run(
                ["sh", "-s", "--", "--list-platforms"],
                cwd=root,
                env=env,
                input=(REPO_ROOT / "install.sh").read_text(encoding="utf-8"),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(marker.is_file(), "piped bootstrap did not download install.py")
            self.assertIn("retroarch", proc.stdout)

    def test_a_failed_download_says_so_in_one_message(self):
        """Offline, the one-liner printed curl's progress table and
        "curl: (6) ...", then exited 6, a code the table does not list."""
        scratch = REPO_ROOT / "tmp" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "curl").write_text(
                "#!/bin/sh\necho 'curl: (6) Could not resolve host: x' >&2\nexit 6\n",
                encoding="utf-8",
            )
            (fake_bin / "curl").chmod(0o755)
            env = os.environ.copy()
            env.update(
                PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
                TMPDIR=str(root),
                RETROBIOS_INSTALL_URL="https://example.invalid/install.py",
            )
            proc = subprocess.run(
                ["sh", "-s"], cwd=root, env=env,
                input=(REPO_ROOT / "install.sh").read_text(encoding="utf-8"),
                capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("could not be downloaded", proc.stderr)
        self.assertIn("Nothing was run and nothing was written", proc.stderr)
        self.assertEqual(proc.stderr.count("curl: (6)"), 1)

    def test_powershell_turns_a_failure_into_one_line(self):
        source = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('[Console]::Error.WriteLine("Error: "', source)
        self.assertIn("winget install Python", source)

    @unittest.skipUnless(shutil.which("script"), "script(1) provides the terminal")
    def test_piped_one_liner_hands_the_terminal_to_the_installer(self):
        """curl | sh gives the installer the script as stdin, so isatty()
        was false and every question it asks was skipped on Linux and Mac."""
        scratch = REPO_ROOT / "tmp" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            probe = root / "probe.py"
            probe.write_text(
                "import sys\nprint('installer stdin is a terminal:', sys.stdin.isatty())\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "curl").write_text(
                "#!/bin/sh\n"
                "output=\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
                "done\n"
                f"cp -- {shlex.quote(str(probe))} \"$output\"\n",
                encoding="utf-8",
            )
            (fake_bin / "curl").chmod(0o755)
            env = os.environ.copy()
            env.update(
                PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
                TMPDIR=str(root),
                RETROBIOS_INSTALL_URL="https://example.invalid/install.py",
                RETROBIOS_INSTALL_SHA256=hashlib.sha256(probe.read_bytes()).hexdigest(),
            )
            piped = f"sh -s < {shlex.quote(str(REPO_ROOT / 'install.sh'))}"
            proc = subprocess.run(
                ["script", "-qec", piped, "/dev/null"],
                cwd=root, env=env, capture_output=True, text=True, timeout=30,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("installer stdin is a terminal: True", proc.stdout)


class FileListsAreRetried(unittest.TestCase):
    """The file list was fetched once with a 30 s timeout: one timeout on
    flaky Wi-Fi ended the run, while each file got three attempts."""

    def _fetch(self, outcomes):
        calls = []

        def urlopen(url, timeout=None):
            calls.append(url)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return contextlib.nullcontext(io.BytesIO(outcome))

        with unittest.mock.patch.object(install.urllib.request, "urlopen", urlopen), \
                unittest.mock.patch.object(install.time, "sleep"), \
                unittest.mock.patch.object(install, "_read_limited_json",
                                           lambda resp, limit, label: json.loads(resp.read())):
            try:
                return install._fetch_json("https://x/list.json", 1000, "list"), calls
            except OSError as exc:
                return exc, calls

    def test_a_timeout_then_an_answer_succeeds(self):
        import urllib.error

        result, calls = self._fetch([urllib.error.URLError("timed out"), b'{"a": 1}'])
        self.assertEqual(result, {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_a_404_is_not_retried(self):
        import urllib.error

        missing = urllib.error.HTTPError("https://x", 404, "Not Found", None, None)
        result, calls = self._fetch([missing])
        self.assertIs(result, missing)
        self.assertEqual(len(calls), 1)

    def test_three_failures_end_with_advice(self):
        import urllib.error

        failures = [urllib.error.URLError("timed out")] * install.MAX_RETRIES
        err = io.StringIO()
        with unittest.mock.patch.object(install.urllib.request, "urlopen",
                                        side_effect=failures), \
                unittest.mock.patch.object(install.time, "sleep"), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                install.fetch_manifest("retroarch")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("HTTPS_PROXY", err.getvalue())
        self.assertIn(f"after {install.MAX_RETRIES} attempts", err.getvalue())


class TargetListFailureIsNotAnAnswer(unittest.TestCase):
    """A timeout is not 'no targets': it sent users to the full pack."""

    def test_unreachable_list_is_none(self):
        import urllib.error

        def boom(*args, **kwargs):
            raise urllib.error.URLError("timed out")

        with unittest.mock.patch.object(install.urllib.request, "urlopen", boom), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(install.fetch_targets("retroarch"))

    def test_absent_list_is_empty(self):
        import urllib.error

        def missing(url, *args, **kwargs):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with unittest.mock.patch.object(install.urllib.request, "urlopen", missing):
            self.assertEqual(install.fetch_targets("retroarch"), {})


class RetroArchWhereItReallyLives(unittest.TestCase):
    """Windows: retroarch.cfg sits beside retroarch.exe and system defaults
    to <exe folder>\\system. Only %APPDATA% was read, and the fallback was a
    folder RetroArch never reads. macOS: the config is under
    Application Support/RetroArch/config, the system folder in Documents."""

    def _exe_dir(self) -> tuple[Path, dict]:
        program_files = Path(_tempdir(self))
        exe_dir = program_files / "RetroArch"
        exe_dir.mkdir()
        (exe_dir / "retroarch.exe").write_bytes(b"MZ")
        return exe_dir, {"ProgramFiles": str(program_files)}

    def test_windows_install_folder_without_a_config(self):
        exe_dir, env = self._exe_dir()
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            found = install.detect_platforms("windows")
        self.assertIn(("retroarch", exe_dir / "system"), found)

    def test_windows_appdata_config_at_default_names_the_exe_folder(self):
        exe_dir, env = self._exe_dir()
        appdata = Path(_tempdir(self))
        (appdata / "RetroArch").mkdir()
        (appdata / "RetroArch" / "retroarch.cfg").write_text('system_directory = "default"\n')
        env["APPDATA"] = str(appdata)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            found = install.detect_platforms("windows")
        self.assertIn(("retroarch", exe_dir / "system"), found)

    def test_windows_config_beside_the_exe_wins(self):
        exe_dir, env = self._exe_dir()
        (exe_dir / "retroarch.cfg").write_text('system_directory = "D:\\bios"\n')
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            found = install.detect_platforms("windows")
        self.assertIn(("retroarch", Path("D:\\bios")), found)

    def test_macos_config_folder_and_documents_default(self):
        home = Path(_tempdir(self))
        support = home / "Library" / "Application Support" / "RetroArch"
        (support / "config").mkdir(parents=True)
        (support / "config" / "retroarch.cfg").write_text('system_directory = "default"\n')
        with unittest.mock.patch.object(install.Path, "home", return_value=home):
            found = install.detect_platforms("darwin")
            self.assertIn(("retroarch", home / "Documents" / "RetroArch" / "system"), found)
            (support / "portable.txt").write_text("")
            found = install.detect_platforms("darwin")
            self.assertIn(("retroarch", support / "system"), found)


class ThePowerShellWrapperPassesTheStatusOn(unittest.TestCase):
    """Every non-zero exit became a red "installer failed" exception and
    code 1: a --check that found work read as a crash, 2 and 130 were lost."""

    @unittest.skipUnless(shutil.which("pwsh"), "needs PowerShell")
    def test_the_installer_code_comes_back_unchanged(self):
        folder = Path(_tempdir(self))
        shutil.copy(REPO_ROOT / "install.ps1", folder / "install.ps1")
        for code in (0, 1, 2, 130):
            with self.subTest(code=code):
                (folder / "install.py").write_text(f"import sys\nsys.exit({code})\n")
                proc = subprocess.run(
                    ["pwsh", "-NoProfile", "-File", str(folder / "install.ps1")],
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertNotIn("installer failed", proc.stdout + proc.stderr)


class ThePowerShellWrapperPassesEveryArgument(unittest.TestCase):
    """A [Parameter()] attribute made install.ps1 an advanced script, whose
    common parameters took `-v` (read as `-Verbose`), `-vb`, `-d` and `-ea`:
    the documented `-v` never reached install.py."""

    @unittest.skipUnless(shutil.which("pwsh"), "needs PowerShell")
    def test_short_flags_reach_the_installer(self):
        folder = Path(_tempdir(self))
        shutil.copy(REPO_ROOT / "install.ps1", folder / "install.ps1")
        (folder / "install.py").write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        for args in (["-v", "--check"], ["-vb"], ["-Verbose"], ["-d", "-ea", "x"],
                     ["--platform", "a b"], []):
            with self.subTest(args=args):
                proc = subprocess.run(
                    ["pwsh", "-NoProfile", "-File", str(folder / "install.ps1"), *args],
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), args)


@unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a folder this user cannot write")
class AnUnwritableFolderIsCodeTwo(unittest.TestCase):
    """A BIOS folder that could not be created ended with 1, while the same
    folder existing read-only ended with the documented 2."""

    def test_both_shapes_exit_two(self):
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(REPO_ROOT)
        )
        handler.log_message = lambda *args: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        home = Path(tempfile.mkdtemp(dir=str(REPO_ROOT / "tmp")))
        locked = home / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(shutil.rmtree, home, True)
        self.addCleanup(locked.chmod, 0o755)
        env = {
            **os.environ,
            "HOME": str(home),
            "RETROBIOS_OS": "linux",
            "RETROBIOS_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        }
        for dest in (locked / "sub", locked):
            with self.subTest(dest=dest.name):
                proc = subprocess.run(
                    [sys.executable, "install.py", "--platform", "retroarch",
                     "--dest", str(dest), "--system", "atari-lynx", "--no-input"],
                    cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                    timeout=300, check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)



class SudoAdviceNamesTheFolder(unittest.TestCase):
    """"Run the same command with sudo" re-ran detection under root's HOME,
    found nothing, installed under /root and reported success."""

    def test_the_advice_carries_platform_and_folder(self):
        advice = install._write_advice(Path("/media/card/Emulation/bios"), False, "emudeck")
        self.assertIn("--platform emudeck", advice)
        self.assertIn("--dest /media/card/Emulation/bios", advice)

    def test_the_closing_report_passes_the_platform(self):
        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn("platform_of={path: plat for plat, path in platforms}", source)



class CaseVariants(unittest.TestCase):
    """The RetroArch list is folded for Windows and macOS: a Linux user got
    BIOS.col and np2kai/2608_BD.WAV only, while Mesen-CE opens bios.col and
    np2kai np2kai/2608_bd.wav. The manifest carries those as case_variants,
    installed where the directory keeps case."""

    def _manifest(self, variants):
        entry = {
            "dest": "BIOS.col", "sha1": "a" * 40, "sha256": "b" * 64,
            "size": 8192, "repo_path": "bios/Coleco/BIOS.col", "cores": None,
        }
        return {
            "manifest_version": 2, "platform": "retroarch", "files": [entry],
            "total_files": 1, "total_size": 8192, "case_variants": variants,
        }

    def test_a_kept_case_directory_is_detected(self):
        directory = Path(_tempdir(self)) / "not" / "yet"
        self.assertTrue(install.keeps_case(directory))
        self.assertEqual(list(Path(directory.parents[1]).iterdir()), [])

    def test_variants_cross_the_trust_boundary(self):
        variant = {
            "dest": "bios.col", "sha1": "a" * 40, "sha256": "b" * 64,
            "size": 8192, "repo_path": "bios/Coleco/BIOS.col", "cores": ["mesence"],
        }
        install._validate_manifest(self._manifest([variant]), "retroarch")
        for bad in ({**variant, "dest": "../x"}, {**variant, "dest": "BIOS.col"}):
            with self.subTest(dest=bad["dest"]), self.assertRaises(ValueError):
                install._validate_manifest(self._manifest([bad]), "retroarch")

    def test_main_adds_them_only_where_case_is_kept(self):
        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        compact = " ".join(source.split())
        self.assertIn(
            'variants = manifest.get("case_variants", []) '
            "case_kept = keeps_case(bios_path) if variants and case_kept:", compact,
        )

    def test_the_published_retroarch_list_carries_them(self):
        manifest = json.loads((REPO_ROOT / "install" / "retroarch.json").read_text())
        variants = {e["dest"] for e in manifest.get("case_variants", [])}
        self.assertIn("bios.col", variants)
        lakka = json.loads((REPO_ROOT / "install" / "lakka.json").read_text())
        folded = {e["dest"] for e in manifest["files"]}
        # A case-keeping RetroArch install is the Lakka list, less what
        # Lakka's own core packages install (installed_on: lakka).
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from common import load_emulator_profiles

        installed = {
            entry.get("path") or entry.get("name")
            for profile in load_emulator_profiles(str(REPO_ROOT / "emulators")).values()
            for entry in profile.get("files") or []
            if isinstance(entry, dict) and "lakka" in (entry.get("installed_on") or [])
        }
        lakka_dests = {e["dest"] for e in lakka["files"]}
        self.assertEqual(lakka_dests - (folded | variants), set())
        self.assertEqual((folded | variants) - lakka_dests - installed, set(),
                         "a case-keeping RetroArch install equals the Lakka list")



class AnUnreadableRetroDeckConfigIsSaid(unittest.TestCase):
    """retrodeck.json present but unreadable: the installer wrote to
    ~/retrodeck without a word, where a home moved to an SD card is not."""

    def test_the_fallback_is_announced(self):
        import contextlib

        home = Path(_tempdir(self))
        conf = home / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        conf.mkdir(parents=True)
        (conf / "retrodeck.json").write_text("{truncated")
        err = io.StringIO()
        with unittest.mock.patch.object(install.Path, "home", return_value=home), \
                unittest.mock.patch.object(install, "_detect_embedded", return_value=[]), \
                contextlib.redirect_stderr(err):
            found = install.detect_platforms("linux")
        self.assertIn(("retrodeck", home / "retrodeck"), found)
        self.assertIn("could not read paths.rd_home_path", err.getvalue())



class TheBoardTheSystemNamesIsTheTarget(unittest.TestCase):
    """Batocera, Recalbox and ROCKNIX each write the board they were built
    for, but the installer read none of them, so a Batocera RPi4 installed
    every board's files (8.6 GiB where bcm2711 needs 5.5)."""

    def test_each_source_is_read(self):
        root = Path(_tempdir(self))
        arch = root / "batocera.arch"
        arch.write_text("bcm2711")
        with unittest.mock.patch.dict(install._BOARD_SOURCES, {"batocera": ("file", str(arch))}):
            self.assertEqual(install.detect_board("batocera"), "bcm2711")
        with unittest.mock.patch.object(install, "_parse_os_release",
                                        return_value={"HW_DEVICE": "RK3326"}):
            self.assertEqual(install.detect_board("rocknix"), "rk3326")
        self.assertIsNone(install.detect_board("retroarch"))

    def test_lakka_names_its_device_or_its_project(self):
        # An RPi4 Lakka installed 5.7 GB of every board's files: its board
        # was in os-release and never read.
        for release, board in (
            ({"LIBREELEC_PROJECT": "RPi", "LIBREELEC_DEVICE": "RPi4"}, "rpi4"),
            ({"LIBREELEC_PROJECT": "Generic"}, "generic"),
        ):
            with self.subTest(board=board), unittest.mock.patch.object(
                install, "_parse_os_release", return_value=release
            ):
                self.assertEqual(install.detect_board("lakka"), board)

    def test_every_board_and_target_are_refused_together(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--every-board",
             "--target", "bcm2711", "--platform", "batocera", "--dry-run"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--every-board", result.stderr)

    def test_the_sources_name_our_target_lists(self):
        import yaml  # noqa: PLC0415

        for platform in install._BOARD_SOURCES:
            with self.subTest(platform=platform):
                targets = yaml.safe_load(
                    (REPO_ROOT / "platforms" / "targets" / f"{platform}.yml").read_text()
                )["targets"]
                self.assertTrue(targets)



class CaseTwinsOnAFoldingVolume(unittest.TestCase):
    """Batocera and Recalbox list np2kai/bios.rom and np2kai/BIOS.ROM, other
    bytes each. Installed to a share opened from Windows or an exFAT card,
    the second overwrote the first and --check never converged."""

    def test_one_file_per_name_is_kept(self):
        files = [{"dest": "np2kai/bios.rom"}, {"dest": "np2kai/BIOS.ROM"}, {"dest": "x.bin"}]
        kept, folded = install.fold_case_twins(files)
        self.assertEqual([f["dest"] for f in kept], ["np2kai/bios.rom", "x.bin"])
        self.assertEqual(folded, ["np2kai/BIOS.ROM"])

    def test_main_folds_only_where_case_is_not_kept(self):
        source = " ".join((REPO_ROOT / "install.py").read_text(encoding="utf-8").split())
        self.assertIn("if not case_kept: files, folded = fold_case_twins(files)", source)



class StandaloneCopiesReachFlatpakInstalls(unittest.TestCase):
    """Copies went only to native and AppImage directories: Dolphin, PPSSPP
    and Azahar from Flathub received nothing. Azahar's native Linux
    directory was wrong too: XDG_DATA_HOME/azahar-emu (common_paths.h
    EMU_DATA_DIR), not azahar."""

    def test_flatpak_and_native_directories(self):
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load((REPO_ROOT / "platforms" / "_registry.yml").read_text())
        linux = {
            target
            for entry in registry["_standalone_copies"]
            for target in (entry.get("targets") or {}).get("linux", [])
        }
        for expected in (
            "$HOME/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC/USA",
            "$HOME/.var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SYSTEM",
            "$HOME/.var/app/org.azahar_emu.Azahar/data/azahar-emu/sysdata",
            "$HOME/.local/share/azahar-emu/sysdata",
        ):
            with self.subTest(target=expected):
                self.assertIn(expected, linux)
        self.assertNotIn("$HOME/.local/share/azahar/sysdata", linux)



class TheInstallerPageNamesEveryEvidence(unittest.TestCase):
    """The page said BizHawk, RetroBat, RomM and RetroPie had no detection
    and fell back to ~/bios, while install.py found each by its directory."""

    def test_every_rule_is_on_the_page(self):
        page = (REPO_ROOT / "wiki" / "installer.md").read_text(encoding="utf-8")
        page = page.replace("\\\\", "\\")
        expected = [path for _p, method, values, _b in install._EMBEDDED_RULES
                    if method == "file_exists" for path in values]
        expected += [path.replace("$HOME", "~").rstrip("/")
                     for _os, _p, path, _b in install._PATH_RULES]
        missing = [path for path in expected if path not in page]
        self.assertEqual(missing, [])
        self.assertNotIn("have no detection", page)



class BizHawkIsFoundWhereItReads(unittest.TestCase):
    """BizHawk reads <data dir>/Firmware: $BIZHAWK_DATA_HOME on Unix (the
    Nix package sets $XDG_DATA_HOME/emuhawk-monort-<version>), else the
    EmuHawk folder. Only two conventional folders were looked at, and a
    forced --platform bizhawk wrote where EmuHawk never reads."""

    def test_the_data_home_variable(self):
        home = Path(_tempdir(self))
        with unittest.mock.patch.dict(os.environ, {"BIZHAWK_DATA_HOME": str(home)}):
            self.assertEqual(install._bizhawk_unix_firmware(), home / "Firmware")

    def test_the_nix_data_home(self):
        xdg = Path(_tempdir(self))
        (xdg / "emuhawk-monort-2.11.1").mkdir()
        env = {"XDG_DATA_HOME": str(xdg), "BIZHAWK_DATA_HOME": ""}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(
                install._bizhawk_unix_firmware(), xdg / "emuhawk-monort-2.11.1" / "Firmware"
            )

    def test_launchbox_names_emuhawk(self):
        source = " ".join((REPO_ROOT / "install.py").read_text(encoding="utf-8").split())
        self.assertIn('.get("emuhawk.exe")', source)



class RetroArchUwpIsFound(unittest.TestCase):
    """The UWP/MSIX RetroArch keeps retroarch.cfg and system/ in its
    package's LocalState, but no rule looked under %LOCALAPPDATA%\\Packages."""

    def _state(self) -> Path:
        local = Path(_tempdir(self))
        state = local / "Packages" / f"{install.RETROARCH_UWP_ID}_abc123" / "LocalState"
        state.mkdir(parents=True)
        patcher = unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return state

    def test_the_default_is_local_state_system(self):
        state = self._state()
        self.assertEqual(install._retroarch_uwp_system_dir(), state / "system")

    def test_a_tilde_is_local_state(self):
        state = self._state()
        (state / "retroarch.cfg").write_text('system_directory = "~\\bios"\n')
        self.assertEqual(install._retroarch_uwp_system_dir(), state / "bios")

    def test_nothing_installed(self):
        with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": _tempdir(self)}):
            self.assertIsNone(install._retroarch_uwp_system_dir())



class QuitMeansNoAtTheLastQuestion(unittest.TestCase):
    """Every menu taught q as quit, yet at "Download 3.2 GB ...? [Y/n]" a q
    started the download."""

    def test_quit_words_refuse(self):
        for answer, expected in (("q", False), ("quit", False), ("exit", False),
                                 ("n", False), ("No", False), ("", True), ("y", True)):
            with self.subTest(answer=answer), \
                    unittest.mock.patch.object(install, "_ask", return_value=answer):
                self.assertIs(install._confirm("? "), expected)



class ACutFileListIsRetried(unittest.TestCase):
    """Wi-Fi dropped mid install/batocera.json: the short body parsed as an
    invalid file list, no retry, no network advice."""

    class _Response(io.BytesIO):
        def __init__(self, body: bytes, length: int):
            super().__init__(body)
            self.headers = {"Content-Length": str(length)}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_the_second_attempt_succeeds(self):
        full = json.dumps({"a": 1}).encode()
        answers = iter([self._Response(full[:4], len(full)), self._Response(full, len(full))])
        with unittest.mock.patch.object(install.urllib.request, "urlopen",
                                        lambda *a, **k: next(answers)), \
                unittest.mock.patch.object(install.time, "sleep"):
            self.assertEqual(install._fetch_json("https://x/y.json", 1000, "manifest"), {"a": 1})

    def test_a_retry_is_announced(self):
        import contextlib

        full = json.dumps({"a": 1}).encode()
        answers = iter([self._Response(full[:4], len(full)), self._Response(full, len(full))])
        err = io.StringIO()
        with unittest.mock.patch.object(install.urllib.request, "urlopen",
                                        lambda *a, **k: next(answers)), \
                unittest.mock.patch.object(install.time, "sleep"), \
                contextlib.redirect_stderr(err):
            install._fetch_json("https://x/y.json", 1000, "file list")
        self.assertIn("trying again (2/3)", err.getvalue())

    def test_a_short_body_is_a_connection_error(self):
        with self.assertRaises(ConnectionError):
            install._read_limited_json(self._Response(b'{"a"', 10), 1000, "manifest")



class DataDirectoriesAreInstalled(unittest.TestCase):
    """The README said the installer does what the pack does, but for RetroArch
    it left out 2,640 data files (Dolphin's Sys, PPSSPP's assets) and
    --check still said "All files up to date"."""

    def _zip(self, root: Path) -> Path:
        import zipfile

        archive = root / "Dolphin.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("Dolphin/Sys/GC/font.bin", b"font")
            zf.writestr("Dolphin/Sys/GC/dsp.bin", b"new bytes upstream")
            zf.writestr("Dolphin/Themes/x.png", b"theme")
        return archive

    def _entry(self) -> dict:
        return {
            "ref": "dolphin-sys", "dest": "dolphin-emu", "url": "https://example.invalid/D.zip",
            "type": "zip", "strip_components": 1, "exclude": ["Themes"],
            "systems": ["nintendo-gamecube"],
            "files": {
                "Sys/GC/font.bin": {"sha1": hashlib.sha1(b"font").hexdigest(), "size": 4},
                "Sys/GC/dsp.bin": {"sha1": hashlib.sha1(b"old").hexdigest(), "size": 3},
                "Sys/GC/gone.bin": {"sha1": "0" * 40, "size": 1},
            },
        }

    def test_verified_members_land_the_rest_are_said(self):
        work = Path(_tempdir(self))
        archive = self._zip(work)
        bios = work / "system"
        bios.mkdir()

        def fetch(url, directory):
            copy = Path(directory) / "a.zip"
            shutil.copy(archive, copy)
            return copy

        with unittest.mock.patch.object(install, "_fetch_archive", fetch):
            counts = install.install_data_directory(self._entry(), bios)
            again = install.install_data_directory(self._entry(), bios)
        self.assertEqual((counts["installed"], counts["changed"], counts["absent"]), (1, 1, 1))
        self.assertEqual((bios / "dolphin-emu" / "Sys" / "GC" / "font.bin").read_bytes(), b"font")
        self.assertFalse((bios / "dolphin-emu" / "Sys" / "GC" / "dsp.bin").exists())
        self.assertFalse((bios / "dolphin-emu" / "Themes").exists())
        self.assertEqual(again["present"], 1)
        self.assertEqual(sorted(p.name for p in (bios / "dolphin-emu").iterdir()), ["Sys"])

    def test_a_member_that_inflates_is_cut_at_its_size(self):
        # A member whose header claims the recorded size and whose stream
        # never ends: reading stops one byte past it.
        class Endless(io.RawIOBase):
            served = 0

            def readable(self):
                return True

            def readinto(self, buffer):
                Endless.served += len(buffer)
                buffer[:] = b"x" * len(buffer)
                return len(buffer)

        work = Path(_tempdir(self))
        entry = self._entry()
        entry["files"] = {"Sys/GC/font.bin": {"sha1": "1" * 40, "size": 4}}

        def members(_archive, _entry):
            yield "Sys/GC/font.bin", 4, lambda: io.BufferedReader(Endless())

        with unittest.mock.patch.object(install, "_fetch_archive",
                                        lambda url, d: Path(d) / "a"), \
                unittest.mock.patch.object(install, "_archive_members", members):
            counts = install.install_data_directory(entry, work)
        self.assertEqual(counts["changed"], 1)
        self.assertLess(Endless.served, 64 * 1024)

    def test_a_tarball_maps_through_source_path(self):
        import io as _io
        import tarfile

        work = Path(_tempdir(self))
        archive = work / "src.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            data = b"index"
            info = tarfile.TarInfo("pcsx2-v1/bin/resources/GameIndex.yaml")
            info.size = len(data)
            tf.addfile(info, _io.BytesIO(data))
        entry = {"type": "tarball", "source_path": "pcsx2-v1/bin/resources"}
        names = [rel for rel, _size, _open in install._archive_members(archive, entry)]
        self.assertEqual(names, ["GameIndex.yaml"])

    def test_the_trust_boundary(self):
        entry = self._entry()
        install._check_data_directory(entry, "d")
        for bad in ({"url": "http://example.com/x.zip"}, {"dest": "../up"},
                    {"files": {"../x": {"sha1": "0" * 40, "size": 1}}},
                    {"type": "rar"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                install._check_data_directory({**entry, **bad}, "d")

    def test_the_published_retroarch_list_carries_them(self):
        manifest = json.loads((REPO_ROOT / "install" / "retroarch.json").read_text())
        dirs = manifest.get("data_directories") or []
        files = sum(len(d["files"]) for d in dirs)
        self.assertEqual(
            manifest["pack_files"] - manifest["total_files"] - files,
            2, "the pack's two documents are the only files the installer lacks",
        )



class AChunkedCutIsADownloadFailure(unittest.TestCase):
    """Behind a proxy that re-encodes as chunked, a cut raised
    http.client.IncompleteRead, which no handler caught: the run ended on a
    traceback with no summary."""

    def test_the_file_fails_and_the_run_goes_on(self):
        import http.client

        class Cut:
            headers = {}
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *_args):
                raise http.client.IncompleteRead(b"abc", 10)

        entry = {"dest": "a.bin", "repo_path": "bios/a.bin", "size": 10,
                 "sha1": "0" * 40, "sha256": "0" * 64}
        with unittest.mock.patch.object(install.urllib.request, "urlopen",
                                        lambda *a, **k: Cut()), \
                unittest.mock.patch.object(install, "_retry_delay", return_value=0):
            dest, ok, reason = install._download_one(entry, Path(_tempdir(self)))
        self.assertFalse(ok)
        self.assertIn("IncompleteRead", reason)



class TheProgressLineFitsTheTerminal(unittest.TestCase):
    """At 80 columns the 94-100 column line wrapped, and every redraw left
    a ghost line behind."""

    def test_each_width(self):
        import os as _os

        for columns in (40, 60, 80, 120):
            with self.subTest(columns=columns):
                out = io.StringIO()
                term = install.Terminal(out)
                term.live = True
                progress = install.Progress(5865, 2_300_000_000, term)
                progress.bytes = 1_000_000_000
                progress.files = 5000
                with unittest.mock.patch.object(
                    install.shutil, "get_terminal_size",
                    return_value=_os.terminal_size((columns, 24)),
                ):
                    progress._draw()
                line = out.getvalue().split("\033[K")[-1]
                self.assertLess(len(line), columns)
                self.assertIn("%", line)



class AResumedCopyIsNotSpeed(unittest.TestCase):
    """The live line divided every byte by the run's time, the 3 GB a
    resumed partial copy already held included: 2.5 GB/s and 0:00 left on a
    1 MB/s link."""

    def test_speed_counts_only_what_this_run_fetched(self):
        import os as _os

        out = io.StringIO()
        term = install.Terminal(out)
        term.live = True
        progress = install.Progress(1, 4_000_000_000, term)
        # Ten seconds exactly: read from the clock, a slow runner added a few
        # milliseconds and moved the last digit of the speed.
        progress.elapsed = lambda: 10.0
        progress.bytes = 3_000_000_000 + 10_000_000
        progress.resumed = 3_000_000_000
        with unittest.mock.patch.object(
            install.shutil, "get_terminal_size",
            return_value=_os.terminal_size((120, 24)),
        ):
            progress._draw()
        line = out.getvalue()
        self.assertIn(f"{install.format_size(1_000_000)}/s", line)
        self.assertNotIn("0:00 left", line)



class ACheckNamesWhatTheCollectionLacks(unittest.TestCase):
    """--check counted the required files the manifest leaves out and named
    none, then said all files were up to date: on Recalbox, c3.555 stayed
    unnamed for a user asking why Caesar III would not start."""

    def test_recalbox_caesar3(self):
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        manifest = json.loads((REPO_ROOT / "install" / "recalbox.json").read_text())
        lacking = sorted(
            e["dest"] for e in manifest.get("omitted_files", [])
            if e.get("required") and e.get("system") == "caesar3"
        )
        if not lacking:
            self.skipTest("the Recalbox manifest omits no required caesar3 file")
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(REPO_ROOT)
        )
        handler.log_message = lambda *args: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        home = Path(tempfile.mkdtemp(dir=str(REPO_ROOT / "tmp")))
        self.addCleanup(shutil.rmtree, home, True)
        env = {
            **os.environ, "HOME": str(home), "RETROBIOS_OS": "linux",
            "RETROBIOS_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        }
        proc = subprocess.run(
            [sys.executable, "-I", "install.py", "--platform", "recalbox",
             "--no-input", "--dest", str(home / "bios"), "--check"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
            check=False,
        )
        self.assertNotIn("Traceback", proc.stderr)
        for dest in lacking:
            self.assertIn(dest, proc.stdout)
        self.assertNotIn("unsafe", proc.stdout)



class ARefusedStandaloneCopyIsNamed(unittest.TestCase):
    """A standalone copy refused by a read-only emulator folder counted as
    an error with no failures entry: the summary named no file, pointed at
    the BIOS folder and gave no advice."""

    def test_the_file_and_its_folder_reach_the_summary(self):
        import contextlib  # noqa: PLC0415

        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("a read-only folder needs POSIX modes and a non-root user")
        bios = Path(_tempdir(self))
        (bios / "scph5501.bin").write_bytes(b"x")
        target = Path(_tempdir(self)) / "pcsx2-bios"
        target.mkdir()
        target.chmod(0o500)
        self.addCleanup(target.chmod, 0o700)
        manifest = {"standalone_copies": [
            {"file": "scph5501.bin", "emulator": "pcsx2",
             "targets": {"linux": [str(target)]}},
        ]}
        failures: list = []
        with contextlib.redirect_stdout(io.StringIO()):
            _copied, _skipped, failed = install.do_standalone_copies(
                manifest, bios, "linux", None, failures)
        self.assertEqual(failed, 1)
        self.assertEqual(failures, [(str(target / "scph5501.bin"), install.NOT_WRITABLE)])

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            install._report_outcome([bios], 0, 1, failed, 0, False, failures=failures)
        text = out.getvalue()
        self.assertIn(str(target / "scph5501.bin"), text)
        self.assertIn(str(target), text.split("Location")[1])
        self.assertIn("cannot write there", text)



class OneEmulatorsFilesInOneCommand(unittest.TestCase):
    """An emulator page offers one command for that emulator's files.
    --core kept the platform's whole list, and narrowing to cores kept the
    platform files of every system no core is mapped to."""

    MANIFEST = {
        "system_cores": {"ibm-pc": ["pcem"], "nintendo-nes": ["fceumm"]},
        "core_names": {"pcem": ["pcem"], "fceumm": ["fceumm"]},
        "files": [],
    }

    def test_only_its_files_and_its_systems(self):
        import contextlib  # noqa: PLC0415

        files = [
            {"dest": "pcem/ibmpc.bin", "cores": ["pcem"], "systems": ["ibm-pc"]},
            {"dest": "disksys.rom", "cores": None, "systems": ["nintendo-nes"]},
            {"dest": "ep128emu/exos.rom", "cores": None, "systems": ["enterprise"]},
            {"dest": "ibm.rom", "cores": None, "systems": ["ibm-pc"]},
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            kept, _omitted = install._narrow_to_emulators(
                ["PCem"], self.MANIFEST, "retroarch", files, [])
        self.assertEqual(sorted(e["dest"] for e in kept), ["ibm.rom", "pcem/ibmpc.bin"])

    def test_an_unknown_name_is_refused(self):
        import contextlib  # noqa: PLC0415

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            install._narrow_to_emulators(["nope"], self.MANIFEST, "retroarch", [], [])



class ThePagesQuoteTheBootstrapsOwnWords(unittest.TestCase):
    """The installer page and the FAQ quoted "install.py SHA-256 mismatch",
    a message the bootstraps no longer print."""

    def test_the_quoted_message_is_printed(self):
        quoted = "does not match its expected"
        for wrapper in ("install.sh", "install.ps1"):
            self.assertIn(quoted, (REPO_ROOT / wrapper).read_text(encoding="utf-8"))
        for page in ("wiki/installer.md", "wiki/faq.md"):
            text = (REPO_ROOT / page).read_text(encoding="utf-8")
            self.assertNotIn("SHA-256 mismatch`", text)
            self.assertIn(quoted, " ".join(text.split()))



class APartialIsClosedBeforeItIsDeleted(unittest.TestCase):
    """Windows refuses to delete an open file: a resumed partial copy whose
    hash no longer matched was unlinked with its handle still open, and the
    PermissionError ended the run."""

    def test_the_source_closes_first(self):
        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        branch = source[source.index("        except _Corrupt:"):]
        branch = branch[:branch.index("delay = 0.0")]
        self.assertLess(branch.index("partial.close()"), branch.index("tmp_path.unlink()"))


class WindowsSeenFromWsl(unittest.TestCase):
    """Under WSL, C:\\RetroBat\\bios was a relative path, %APPDATA% and
    %USERPROFILE% were unset, and --platform retrobat wrote 7 753 files into
    ~/bios inside the distribution, where RetroBat reads nothing."""

    def setUp(self):
        self.root = Path(_tempdir(self))
        patcher = unittest.mock.patch.dict(install._WSL, {"active": True, "root": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_drive_path_is_its_mount(self):
        self.assertEqual(install._host_path("C:\\RetroBat\\bios"), self.root / "c" / "RetroBat" / "bios")
        self.assertEqual(install._rule_path("C:\\RetroBat\\bios"), self.root / "c" / "RetroBat" / "bios")
        self.assertEqual(install._default_dest("wsl", "retrobat"), self.root / "c" / "RetroBat" / "bios")
        self.assertEqual(install._host_path("relative/path"), Path("relative/path"))

    def test_retrobat_on_the_windows_side_is_detected(self):
        (self.root / "c" / "RetroBat" / "bios").mkdir(parents=True)
        found: list = []
        install._detect_by_path("wsl", found)
        self.assertIn(("retrobat", self.root / "c" / "RetroBat" / "bios"), found)

    def test_the_windows_variables_are_read_through_cmd(self):
        answers = {"USERPROFILE": "C:\\Users\\me", "APPDATA": "C:\\Users\\me\\AppData\\Roaming"}

        def cmd(argv, **kwargs):
            # cmd.exe must not read the terminal: it swallowed the answer
            # typed at the first prompt.
            self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
            name = argv[-1].split("%")[1]
            return subprocess.CompletedProcess(argv, 0, answers.get(name, f"%{name}%") + "\r\n", "")

        cleared = {name: "" for name in install._WINDOWS_VARIABLES}
        with unittest.mock.patch.dict(os.environ, cleared), \
                unittest.mock.patch.object(install.subprocess, "run", cmd):
            install._enter_wsl()
            self.assertEqual(os.environ["USERPROFILE"], str(self.root / "c" / "Users" / "me"))
            self.assertEqual(os.environ["APPDATA"],
                             str(self.root / "c" / "Users" / "me" / "AppData" / "Roaming"))
            self.assertEqual(os.environ["ProgramFiles"], "")


class SteamRetroArchOnMacIsPortable(unittest.TestCase):
    """platform_darwin.m makes the Steam build portable (HAVE_STEAM) with no
    portable.txt: a default system_directory is Application Support's, and
    the installer took ~/Documents/RetroArch/system."""

    def test_the_steam_bundle_moves_the_base(self):
        home = Path(_tempdir(self))
        support = home / "Library" / "Application Support"
        with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}), \
                unittest.mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(install._macos_retroarch_base(), home / "Documents" / "RetroArch")
            (support / "Steam/steamapps/common/RetroArch/RetroArch.app").mkdir(parents=True)
            self.assertEqual(install._macos_retroarch_base(), support / "RetroArch")


class AMissingFileListNamesTheRef(unittest.TestCase):
    """A 404 on the manifest said three attempts were made and to check the
    connection and proxy, but it was asked once, and the cause is the ref."""

    def _run(self, ref: str) -> str:
        error = install.urllib.error.HTTPError("u", 404, "Not Found", None, None)
        with unittest.mock.patch.object(install, "RELEASE_REF", ref), \
                unittest.mock.patch.object(install, "_fetch_json", side_effect=error), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            install.fetch_manifest("retroarch")
        return err.getvalue()

    def test_a_pinned_tag_is_named(self):
        text = self._run("v2020.01.01")
        self.assertIn("RETROBIOS_REF=v2020.01.01", text)
        self.assertNotIn("attempts", text)
        self.assertNotIn("proxy", text)

    def test_the_default_ref_is_not_blamed_on_the_network(self):
        text = self._run(install.DEFAULT_RELEASE_REF)
        self.assertIn("No file list for retroarch", text)
        self.assertNotIn("proxy", text)


class TheInstallerSaysWhatItFound(unittest.TestCase):
    """--check gave a count and no name, a read-only folder printed a line
    per file, a piped log put errors before their step, and Ctrl+C while
    the file list was fetched ended on a socket traceback."""

    def setUp(self):
        import functools  # noqa: PLC0415
        import http.server  # noqa: PLC0415
        import shutil  # noqa: PLC0415

        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(REPO_ROOT)
        )
        handler.log_message = lambda *args: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        (REPO_ROOT / "tmp").mkdir(exist_ok=True)
        self.home = Path(tempfile.mkdtemp(dir=str(REPO_ROOT / "tmp")))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = {
            **os.environ, "HOME": str(self.home), "RETROBIOS_OS": "linux",
            "RETROBIOS_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        }

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "install.py", "--platform", "retroarch", "--no-input", *extra],
            cwd=REPO_ROOT, env=self.env, capture_output=True, text=True, timeout=300,
            check=False,
        )

    def test_check_names_the_files(self):
        dest = self.home / "system"
        self.assertEqual(self._run("--dest", str(dest), "--system", "atari-lynx", "--yes").returncode, 0)
        (dest / "lynxboot.img").write_bytes(b"corrupt")
        proc = self._run("--dest", str(dest), "--system", "atari-lynx", "--check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("wrong hash: lynxboot.img", proc.stdout)
        self.assertIn("without --check", proc.stdout)

    def test_a_read_only_folder_is_said_once(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        dest = self.home / "system"
        dest.mkdir()
        dest.chmod(0o500)
        self.addCleanup(dest.chmod, 0o700)
        proc = self._run("--dest", str(dest), "--system", "atari-lynx", "--yes")
        output = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0)
        self.assertLessEqual(output.count("FAILED"), 1, output)
        self.assertIn("failed: 2 files (folder not writable)", output)

    def test_a_piped_log_keeps_its_order(self):
        proc = subprocess.run(
            [sys.executable, "install.py", "--platform", "retroarch", "--no-input",
             "--target", "no-such-target", "--dest", str(self.home / "s")],
            cwd=REPO_ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=300, check=False,
        )
        banner = proc.stdout.find("RetroBIOS")
        error = proc.stdout.lower().find("unknown target")
        self.assertGreaterEqual(banner, 0, proc.stdout)
        self.assertGreater(error, banner, proc.stdout)

    def test_ctrl_c_before_the_downloads_is_a_stop(self):
        import signal  # noqa: PLC0415
        import socket  # noqa: PLC0415

        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        self.addCleanup(silent.close)
        env = {**self.env, "RETROBIOS_BASE_URL": f"http://127.0.0.1:{silent.getsockname()[1]}"}
        proc = subprocess.Popen(
            [sys.executable, "install.py", "--platform", "retroarch", "--no-input",
             "--dest", str(self.home / "s")],
            cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        import time  # noqa: PLC0415

        time.sleep(3)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertIn("Stopped", out)
        self.assertNotIn("Traceback", err)


class NoFallbackOutsideWhatAPlatformReads(unittest.TestCase):
    """A forced platform not found here fell back to ~/bios for RetroBat on
    Linux and for BizHawk from the menu, and on Windows to /userdata/bios,
    which resolves to the root of the current drive. Each run reported done."""

    def test_the_fallback_is_none_where_nothing_is_read(self):
        self.assertIsNone(install._default_dest("linux", "retrobat"))
        self.assertIsNone(install._default_dest("linux", "bizhawk"))
        self.assertIsNone(install._default_dest("windows", "batocera"))
        self.assertIsNone(install._default_dest("windows", "retropie"))
        self.assertEqual(install._default_dest("linux", "batocera"), Path("/userdata/bios"))
        # emudeck-we functions/all.ps1:12-14, not the profile folder.
        self.assertEqual(
            install._default_dest("windows", "emudeck"), install._host_path("C:/Emulation/bios")
        )

    def test_a_forced_platform_without_a_folder_is_refused(self):
        with unittest.mock.patch.object(install, "detect_platforms", return_value=[]), \
                unittest.mock.patch.object(install, "detect_os", return_value="linux"), \
                unittest.mock.patch("sys.argv", ["install.py", "--platform", "retrobat",
                                                 "--no-input"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as caught:
            install.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("--dest", err.getvalue())


class EveryStandaloneCopyNamesItsMacFolder(unittest.TestCase):
    """--standalone-copies copied Azahar's keys and Flycast's boot ROM on
    Linux and Windows and silently skipped them on macOS, where both
    emulators read a fixed Application Support folder."""

    def test_each_copy_has_a_darwin_target(self):
        import yaml

        registry = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "platforms" / "_registry.yml").read_text()
        )
        missing = [
            entry.get("file") or entry.get("pattern")
            for entry in registry["_standalone_copies"]
            if entry.get("targets") and "darwin" not in entry["targets"]
        ]
        self.assertEqual(missing, [])



class _LoopbackRun:
    """install.py against the repository's own manifests over loopback."""

    def serve(self, test: unittest.TestCase) -> dict:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(REPO_ROOT)
        )
        handler.log_message = lambda *args: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        test.addCleanup(httpd.server_close)
        test.addCleanup(httpd.shutdown)
        self.home = Path(_tempdir(test))
        return {
            **os.environ, "HOME": str(self.home), "RETROBIOS_OS": "linux",
            "RETROBIOS_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        }


def _install_run(env: dict, *argv: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "install.py", *argv], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=300, check=False, **kwargs,
    )


class ATypoGetsTheClosestName(unittest.TestCase):
    """--platform batocra, --system playstation or --core swanstaton were
    answered with every name the platform has, up to 400 on one line, and
    MiSTer FPGA as the site writes it was refused."""

    def test_the_names_the_site_writes_are_platforms(self):
        self.assertEqual(install.normalize_platform("MiSTer FPGA"), "misterfpga")
        self.assertEqual(install.normalize_platform("RetroArch "), "retroarch")
        self.assertEqual(install.normalize_platform("RetroDECK"), "retrodeck")

    def test_a_platform_typo_is_answered_with_one_name(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            install.normalize_platform("batocra")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Did you mean: batocera?", err.getvalue())
        self.assertNotIn("recalbox", err.getvalue())

    def test_a_system_is_found_inside_its_full_id(self):
        known = ["3do", "nintendo-gba", "sega-saturn", "sony-playstation", "sony-playstation-2"]
        with self.assertRaises(ValueError) as raised:
            install.resolve_choices(["playstation"], known, "system")
        text = str(raised.exception)
        self.assertIn("unknown system: playstation", text)
        self.assertIn("sony-playstation", text)
        self.assertIn("--list-systems", text)
        self.assertNotIn("3do", text)

    def test_a_core_typo_points_at_the_listing(self):
        known = [f"core{i}" for i in range(200)] + ["swanstation"]
        with self.assertRaises(ValueError) as raised:
            install.resolve_choices(["swanstaton"], known, "core")
        text = str(raised.exception)
        self.assertIn("Did you mean: swanstation?", text)
        self.assertIn("--list-cores lists all 201", text)
        self.assertNotIn("core17", text)

    def test_a_name_spelled_as_the_site_writes_it(self):
        known = ["sony-playstation", "sony-playstation-2"]
        self.assertEqual(
            install.resolve_choices(["Sony PlayStation"], known, "system"),
            ["sony-playstation"],
        )

    def test_a_target_typo(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            install._unknown_target("swich", "retroarch", {"switch": {}, "desktop": {}})
        self.assertIn("Did you mean: switch?", err.getvalue())
        self.assertIn("--list-targets", err.getvalue())


class PlatformsAreNamedAsTheyNameThemselves(unittest.TestCase):
    """str.capitalize() printed Retroarch, Retrodeck and Misterfpga on the
    first screen a user sees."""

    def test_the_table_is_the_manifests(self):
        for plat in install.AVAILABLE_PLATFORMS:
            manifest = json.loads((REPO_ROOT / "install" / f"{plat}.json").read_text())
            with self.subTest(plat=plat):
                self.assertEqual(install.PLATFORM_NAMES[plat], manifest["display_name"])

    def test_the_menus_use_them(self):
        out = io.StringIO()
        with unittest.mock.patch("builtins.input", lambda _prompt="": "1"), \
                contextlib.redirect_stdout(out):
            install._prompt_platform_choice(
                [("misterfpga", Path("/m")), ("retrodeck", Path("/r"))])
        self.assertIn("MiSTer FPGA (/m)", out.getvalue())
        self.assertIn("RetroDECK (/r)", out.getvalue())
        self.assertEqual(install.display_name("romm", {"display_name": "RomM"}), "RomM")
        self.assertEqual(install.display_name("romm", {"display_name": "\x1b[2J"}), "RomM")


class HintsGiveTheUsersOwnCommandBack(unittest.TestCase):
    """A one-liner user was told to run python3 install.py, a file they do
    not have, and the write advice dropped --system, turning a retry of 2
    files into 8069, with sudo on Windows."""

    def setUp(self):
        patcher = unittest.mock.patch.object(install, "_INVOCATION", install.Invocation())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, *argv: str) -> None:
        args = SimpleNamespace(
            system=None, core=None, emulator=None, region=None, target=None,
            cores_dir=None, every_board=False, installed_cores=False,
            standalone_copies=False, check=False, dry_run=False,
        )
        flags = list(argv)
        while flags:
            flag = flags.pop(0)
            attr = flag[2:].replace("-", "_")
            if attr in ("system", "core", "emulator", "region"):
                setattr(args, attr, [*(getattr(args, attr) or []), flags.pop(0)])
            else:
                setattr(args, attr, True)
        install._INVOCATION.record(args)

    def test_the_shell_one_liner_is_rebuilt(self):
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_BOOTSTRAP": "sh"}):
            hint = "\n".join(install._manual_usage_hint("linux"))
        self.assertIn("install.sh | sh -s -- --platform retroarch", hint)
        self.assertNotIn("python3 install.py", hint)
        self.assertIn(install.INSTALLER_PAGE, hint)

    def test_a_clone_keeps_python(self):
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_BOOTSTRAP": ""}):
            hint = "\n".join(install._manual_usage_hint("linux"))
        self.assertIn("python3 install.py --platform retroarch", hint)

    def test_the_write_advice_keeps_the_narrowing(self):
        self._record("--system", "atari-lynx", "--region", "us")
        install._INVOCATION.os_type = "linux"
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_BOOTSTRAP": "sh"}):
            advice = install._write_advice(Path("/media/ro"), False, "retroarch")
        self.assertIn("| sudo sh -s -- --platform retroarch --dest /media/ro "
                      "--system atari-lynx --region us", advice)

    def test_windows_has_no_sudo(self):
        self._record("--system", "atari-lynx")
        install._INVOCATION.os_type = "windows"
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_BOOTSTRAP": "ps1"}):
            advice = install._write_advice(Path("C:/Program Files/RetroArch/system"),
                                           False, "retroarch")
        self.assertNotIn("sudo", advice)
        self.assertIn("administrator", advice)
        self.assertIn("install.ps1", advice)
        self.assertIn("--system atari-lynx", advice)

    def test_a_full_drive_keeps_the_platform(self):
        install._INVOCATION.os_type = "linux"
        err = io.StringIO()
        with unittest.mock.patch.object(install.shutil, "disk_usage",
                                        return_value=SimpleNamespace(free=1)), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            install._check_free_space(Path(_tempdir(self)), 10, "batocera")
        self.assertIn("--platform batocera --dest <folder on another drive>", err.getvalue())

    def test_the_one_liner_tells_the_installer_how_it_was_started(self):
        scratch = REPO_ROOT / "tmp" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            probe = root / "probe.py"
            probe.write_text("import os\nprint('bootstrap:', os.environ.get('RETROBIOS_BOOTSTRAP'))\n")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "curl").write_text(
                "#!/bin/sh\noutput=\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
                "done\n"
                f"cp -- {shlex.quote(str(probe))} \"$output\"\n")
            (fake_bin / "curl").chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                   "TMPDIR": str(root),
                   "RETROBIOS_INSTALL_URL": "https://example.invalid/install.py",
                   "RETROBIOS_INSTALL_SHA256": hashlib.sha256(probe.read_bytes()).hexdigest()}
            proc = subprocess.run(["sh", "-s"], cwd=root, env=env, capture_output=True,
                                  input=(REPO_ROOT / "install.sh").read_text(),
                                  text=True, timeout=30, check=False)
        self.assertIn("bootstrap: sh", proc.stdout, proc.stderr)

    def test_the_help_has_examples_and_the_page(self):
        env = {**os.environ, "RETROBIOS_BOOTSTRAP": "sh"}
        proc = _install_run(env, "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("examples:", proc.stdout)
        self.assertIn("install.sh | sh -s -- --platform batocera --dest", proc.stdout)
        self.assertIn(install.INSTALLER_PAGE, proc.stdout)


class AnOverrideIsNamedWhenItIsTheCause(unittest.TestCase):
    """A RETROBIOS_INSTALL_SHA256 of the user's own read as a cut download,
    and a dead RETROBIOS_BASE_URL as a network fault naming no host."""

    def test_an_unreachable_base_url_is_named(self):
        error = install.urllib.error.URLError("timed out")
        with unittest.mock.patch.dict(os.environ, {"RETROBIOS_BASE_URL": "https://mirror.invalid"}), \
                unittest.mock.patch.object(install, "_fetch_json", side_effect=error), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            install.fetch_manifest("retroarch")
        text = err.getvalue()
        self.assertIn("Address: " + install.MANIFEST_URL.format(platform="retroarch"), text)
        self.assertIn("RETROBIOS_BASE_URL is set", text)

    def test_a_pinned_fingerprint_is_named(self):
        scratch = REPO_ROOT / "tmp" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "curl").write_text(
                "#!/bin/sh\noutput=\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then output=$2; shift 2; else shift; fi\n"
                "done\n"
                "echo 'print(1)' > \"$output\"\n")
            (fake_bin / "curl").chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                   "TMPDIR": str(root), "RETROBIOS_INSTALL_SHA256": "0" * 64}
            proc = subprocess.run(["sh", "-s"], cwd=root, env=env, capture_output=True,
                                  input=(REPO_ROOT / "install.sh").read_text(),
                                  text=True, timeout=30, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RETROBIOS_INSTALL_SHA256 is set", proc.stderr)
        self.assertNotIn("cut short", proc.stderr)
        ps1 = (REPO_ROOT / "install.ps1").read_text()
        self.assertIn('"RETROBIOS_INSTALL_URL", "RETROBIOS_INSTALL_SHA256"', ps1)


@unittest.skipUnless(shutil.which("pwsh"), "needs PowerShell")
class ThePowerShellOneLinerLeavesTheSessionAlone(unittest.TestCase):
    """irm | iex left $ErrorActionPreference = Stop and the script's
    variables in the user's session: their next error stopped everything."""

    def test_nothing_is_left_behind(self):
        script = (
            "Get-Content install.ps1 -Raw | Invoke-Expression; "
            "\"EAP=$ErrorActionPreference temporary=[$temporary] installer=[$installer] "
            "bootstrap=[$env:RETROBIOS_BOOTSTRAP]\""
        )
        # Refused before any request, so the run needs no network.
        env = {**os.environ, "RETROBIOS_INSTALL_URL": "http://127.0.0.1/install.py"}
        env.pop("RETROBIOS_BOOTSTRAP", None)
        proc = subprocess.run(["pwsh", "-NoProfile", "-Command", script], cwd=REPO_ROOT,
                              env=env, capture_output=True, text=True, timeout=120)
        self.assertIn("must use HTTPS", proc.stderr)
        self.assertIn("EAP=Continue temporary=[] installer=[] bootstrap=[]", proc.stdout)


class AQuestionLeftWithCtrlDOrCtrlCIsAStop(unittest.TestCase):
    """Ctrl+C or Ctrl+D at a question left with 0, so `install.sh && retroarch`
    carried on with no BIOS, while the page documents 130."""

    def test_eof_raises_the_stop(self):
        def closed(_prompt=""):
            raise EOFError

        with unittest.mock.patch("builtins.input", closed), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            install._ask("> ")

    @unittest.skipIf(os.name == "nt", "needs a pseudo-terminal")
    def test_ctrl_d_at_the_platform_question_exits_130(self):
        import pty  # noqa: PLC0415
        import time  # noqa: PLC0415

        home = Path(_tempdir(self))
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "install.py"], cwd=REPO_ROOT, stdin=slave,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "HOME": str(home), "RETROBIOS_OS": "linux"},
        )
        os.close(slave)
        self.addCleanup(os.close, master)
        time.sleep(3)
        os.write(master, b"\x04")
        out, _ = proc.communicate(timeout=60)
        if "Choose the platform" not in out:
            self.skipTest("a platform was detected on this machine")
        self.assertEqual(proc.returncode, 130, out)
        self.assertIn("Stopped", out)


class WhatTheCollectionLacksSaysForWhom(unittest.TestCase):
    """Required files the installer cannot supply were listed by bare name:
    DARK.GOB, Data.rsdk, ... and 69 more, with no system and no emulator."""

    def test_grouped_by_system_with_its_emulators(self):
        entries = [
            {"dest": "DARK.GOB", "system": "dark-forces", "cores": ["theforceengine"]},
            {"dest": "SOUNDS.GOB", "system": "dark-forces", "cores": ["theforceengine"]},
            {"dest": "Data.rsdk", "system": "sonic-mania", "cores": ["sonicmania"]},
        ]
        lines = install._needed_lines(entries, "Needed to start an emulator")
        self.assertIn("    dark-forces (theforceengine): DARK.GOB, SOUNDS.GOB", lines)
        self.assertIn("    sonic-mania (sonicmania): Data.rsdk", lines)
        self.assertIn("(3 files)", lines[0])
        self.assertIn(f"{install.SITE_URL}emulators/", lines[-1])


class RunsAgainstTheRealLists(unittest.TestCase, _LoopbackRun):
    """Behaviour read end to end over loopback."""

    def setUp(self):
        self.env = self.serve(self)

    def test_a_wrong_hash_is_listed_first(self):
        dest = self.home / "system"
        dest.mkdir()
        (dest / "scph5501.bin").write_bytes(b"junk")
        proc = _install_run(self.env, "--platform", "retroarch", "--dest", str(dest),
                            "--check", "--no-input")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        listing = proc.stdout.split("to download:\n", 1)[1].splitlines()
        self.assertEqual(listing[0].strip(), "wrong hash: scph5501.bin")

    def test_a_dry_run_counts_the_data_trees_and_ends_on_its_last_line(self):
        proc = _install_run(self.env, "--platform", "retroarch", "--dest",
                            str(self.home / "s"), "--dry-run", "--no-input")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertIn("Nothing was written", lines[-1])
        header = next(line for line in lines if line.startswith("Would download"))
        self.assertIn("data files", header)

    def test_dest_alone_says_whose_list_it_is(self):
        proc = _install_run(self.env, "--dest", str(self.home / "card"), "--dry-run",
                            "--no-input", "--system", "atari-lynx")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("File list: RetroArch", proc.stdout)
        self.assertIn("--platform", proc.stdout)

    def test_a_cores_folder_can_be_named(self):
        cores = self.home / "cores"
        cores.mkdir()
        (cores / "handy_libretro.so").write_bytes(b"")
        proc = _install_run(self.env, "--platform", "retroarch", "--dest",
                            str(self.home / "s"), "--cores-dir", str(cores),
                            "--dry-run", "--no-input")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("to the installed cores", proc.stdout)

    def test_no_core_found_says_how_to_go_on(self):
        proc = _install_run(self.env, "--platform", "retroarch", "--dest",
                            str(self.home / "s"), "--installed-cores", "--dry-run",
                            "--no-input")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--cores-dir <cores folder>", proc.stderr)
        self.assertIn("without --installed-cores", proc.stderr)


class DestAloneFollowsWhatIsFoundHere(unittest.TestCase):
    """--dest without --platform always meant RetroArch's list, even on a
    machine where Batocera was found, and nothing said so."""

    def _pick(self, found, interactive=False, answer=""):
        out = io.StringIO()
        with unittest.mock.patch.object(install, "detect_platforms", return_value=found), \
                unittest.mock.patch("builtins.input", lambda _prompt="": answer), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                return install._platform_for_dest("linux", Path("/media/card"), interactive), out.getvalue()
            except SystemExit as exc:
                return exc, out.getvalue()

    def test_nothing_found_is_retroarch_said_aloud(self):
        plat, out = self._pick([])
        self.assertEqual(plat, "retroarch")
        self.assertIn("File list: RetroArch (nothing was detected here)", out)

    def test_the_one_platform_found_is_used(self):
        plat, out = self._pick([("batocera", Path("/userdata/bios"))])
        self.assertEqual(plat, "batocera")
        self.assertIn("File list: Batocera", out)

    def test_several_found_without_a_terminal_is_refused(self):
        result, out = self._pick([("emudeck", Path("/e")), ("retroarch", Path("/r"))])
        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        self.assertIn("--platform", out)

    def test_a_terminal_asks_and_enter_keeps_the_one_found(self):
        plat, out = self._pick([("batocera", Path("/userdata/bios"))], interactive=True)
        self.assertEqual(plat, "batocera")
        self.assertIn("Which platform's files go to /media/card?", out)


class _FakeResponse:
    """What urlopen returns, serving one payload from its start."""

    def __init__(self, payload: bytes) -> None:
        self.status = 200
        self.headers = {"Content-Length": str(len(payload))}
        self._body = io.BytesIO(payload)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def _sharing_violation() -> PermissionError:
    exc = PermissionError(13, "The process cannot access the file")
    exc.winerror = 32
    return exc


class ACompletePartialIsHandedOffOnWindows(unittest.TestCase):
    """Windows renames no open file, so the run that verified a large copy
    released its lock before the rename. A second run claimed the copy in
    that moment, found it complete and truncated it, and the first run's
    rename failed with a sharing violation reported as "folder not
    writable"."""

    PAYLOAD = bytes(range(256)) * 200
    ENTRY = {
        "dest": "big.bin", "repo_path": "bios/big.bin", "size": len(PAYLOAD),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
    }

    def _run(self, tmp: Path, seen: list[int]):
        part = tmp / ".big.bin.part"

        def urlopen(request, timeout=None):
            seen.append(part.stat().st_size if part.exists() else -1)
            return _FakeResponse(self.PAYLOAD)

        with unittest.mock.patch.object(install, "RESUME_FROM_SIZE", 1024), \
                unittest.mock.patch.object(install, "_NO_RENAME_WHILE_OPEN", True), \
                unittest.mock.patch.object(install.urllib.request, "urlopen", urlopen):
            return install._download_one(dict(self.ENTRY), tmp)

    def test_the_other_runs_copy_is_left_whole(self):
        tmp = Path(_tempdir(self))
        (tmp / ".big.bin.part").write_bytes(self.PAYLOAD)
        seen: list[int] = []
        self.assertEqual(self._run(tmp, seen), ("big.bin", True, ""))
        # The download ran while the complete copy was still whole.
        self.assertEqual(seen, [len(self.PAYLOAD)])
        self.assertEqual((tmp / "big.bin").read_bytes(), self.PAYLOAD)
        # Nothing held it afterwards: it was a leftover, now removed.
        self.assertFalse((tmp / ".big.bin.part").exists())

    def test_the_rename_waits_for_the_other_run_to_let_go(self):
        tmp = Path(_tempdir(self))
        source = tmp / "copy"
        source.write_bytes(b"x")
        real_replace = os.replace
        calls = []

        def replace(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise _sharing_violation()
            return real_replace(src, dst)

        with unittest.mock.patch.object(install.os, "replace", replace), \
                unittest.mock.patch.object(install.time, "sleep"):
            install._replace_released(source, tmp / "dest", {})
        self.assertEqual(len(calls), 3)
        self.assertEqual((tmp / "dest").read_bytes(), b"x")

    def test_a_copy_the_other_run_placed_counts_once_verified(self):
        tmp = Path(_tempdir(self))
        dest = tmp / "dest"
        dest.write_bytes(self.PAYLOAD)
        expected = {"sha256": self.ENTRY["sha256"]}
        install._replace_released(tmp / "gone", dest, expected)
        dest.write_bytes(b"other bytes")
        with self.assertRaises(FileNotFoundError):
            install._replace_released(tmp / "gone", dest, expected)

    def test_a_sharing_violation_is_no_refused_folder(self):
        self.assertFalse(install._write_refused(_sharing_violation()))
        self.assertTrue(install._write_refused(PermissionError(13, "denied")))


class TheEmulatorNamesHaveTheirOwnListing(unittest.TestCase, _LoopbackRun):
    """--emulator nope said "--list-cores lists all 441" and --list-cores
    printed 189: the buildbot names --emulator accepts were listed nowhere."""

    def setUp(self):
        self.env = self.serve(self)

    def test_the_hint_counts_what_the_listing_prints(self):
        proc = _install_run(self.env, "--platform", "retroarch", "--no-input",
                            "--emulator", "nope")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        found = re.search(r"--list-emulators lists all (\d+)\.", proc.stderr)
        self.assertIsNotNone(found, proc.stderr)
        listing = _install_run(self.env, "--platform", "retroarch", "--no-input",
                               "--list-emulators")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        names = [re.split(r"\s{2,}", line.strip())[0]
                 for line in listing.stdout.splitlines() if line.startswith("  ")]
        manifest = json.loads((REPO_ROOT / "install" / "retroarch.json").read_text())
        accepted = install.emulator_names(
            manifest, manifest["files"], manifest.get("omitted_files", []))
        self.assertEqual(int(found.group(1)), len(accepted))
        self.assertTrue(set(accepted) <= set(names), set(accepted) - set(names))
        self.assertIn("mednafen_psx", accepted)
        self.assertRegex(listing.stdout, r"mednafen_psx\s+same as beetle_psx")

    def test_every_listed_name_is_accepted(self):
        manifest = {"core_names": {"beetle_psx": ["mednafen_psx"]}}
        files = [{"dest": "a", "cores": ["pcsx_rearmed"]}]
        names = install.emulator_names(manifest, files, [])
        self.assertEqual(names, {"beetle_psx": "beetle_psx", "mednafen_psx": "beetle_psx",
                                 "pcsx_rearmed": "pcsx_rearmed"})
        self.assertEqual(install.resolve_choices(["MEDNAFEN_PSX"], sorted(names), "emulator"),
                         ["mednafen_psx"])


class ANetworkFailureNamesTheNetwork(unittest.TestCase):
    """Every transfer cut by the connection ended on "Location that was being
    written to" and "once that is resolved", naming the folder and nothing
    about the connection or the proxy."""

    def _fail(self, error: BaseException) -> str:
        def urlopen(*_args, **_kwargs):
            raise error

        entry = {"dest": "a.bin", "repo_path": "bios/a.bin", "size": 10,
                 "sha256": "0" * 64}
        with unittest.mock.patch.object(install.urllib.request, "urlopen", urlopen), \
                unittest.mock.patch.object(install, "_retry_delay", return_value=0):
            _dest, ok, reason = install._download_one(entry, Path(_tempdir(self)))
        self.assertFalse(ok)
        return reason

    def test_cuts_and_refusals_are_network_failures(self):
        import http.client
        import socket

        for error in (
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
            http.client.IncompleteRead(b"abc", 10),
            ConnectionResetError(104, "Connection reset by peer"),
            socket.timeout("timed out"),
            urllib.error.HTTPError("u", 503, "Service Unavailable", None, None),
        ):
            with self.subTest(error=error):
                self.assertIsInstance(self._fail(error), install.NetworkReason)

    def test_a_full_disk_and_a_missing_file_are_not(self):
        import errno

        for error in (OSError(errno.ENOSPC, "No space left on device"),
                      urllib.error.HTTPError("u", 404, "Not Found", None, None)):
            with self.subTest(error=error):
                self.assertNotIsInstance(self._fail(error), install.NetworkReason)

    def test_the_summary_points_at_the_connection(self):
        failures = [("a.bin", install.NetworkReason("timed out")),
                    ("b.bin", install.NetworkReason("Connection reset by peer"))]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            install._report_outcome([Path("/bios")], 0, 0, 2, 0, False, failures=failures)
        self.assertEqual(raised.exception.code, 2)
        text = out.getvalue()
        self.assertIn("HTTPS_PROXY", text)
        self.assertIn("Destination: /bios", text)
        self.assertNotIn("being written to", text)


class RetroBatIsFoundWhereItSaysItIs(unittest.TestCase):
    """RetroBat on D:\\RetroBat was not detected, and --platform retrobat
    wrote 7.6 GB into C:\\RetroBat\\bios, a folder it does not read.
    RetroBat.exe writes its folder to the registry on every start."""

    def test_the_table_is_the_registry(self):
        import yaml  # noqa: PLC0415

        registry = yaml.safe_load((REPO_ROOT / "platforms" / "_registry.yml").read_text())
        rules = {
            (rule["os"], name, rule["key"], rule["value"], rule["bios_subdir"])
            for name, entry in registry["platforms"].items()
            for rule in (entry.get("install") or {}).get("detect", [])
            if rule["method"] == "registry"
        }
        self.assertEqual(set(install._REGISTRY_RULES), rules)

    def test_the_registry_folder_wins_over_the_default(self):
        folder = Path(_tempdir(self))
        with unittest.mock.patch.object(install, "_read_registry",
                                        return_value=str(folder) + "\\"):
            found: list = []
            install._detect_by_registry("windows", found)
        self.assertEqual(found, [("retrobat", folder / "bios")])

    def test_a_folder_since_removed_is_passed_over(self):
        gone = Path(_tempdir(self)) / "moved"
        with unittest.mock.patch.object(install, "_read_registry", return_value=str(gone)):
            found: list = []
            install._detect_by_registry("windows", found)
        self.assertEqual(found, [])

    def test_wsl_reads_it_through_reg_exe(self):
        output = ("\nHKEY_CURRENT_USER\\SOFTWARE\\RetroBat\n"
                  "    LatestKnownInstallPath    REG_SZ    D:\\Games\\RetroBat\\\n\n")
        with unittest.mock.patch.dict(install._WSL, {"active": True, "root": "/mnt"}), \
                unittest.mock.patch.object(install.os, "name", "posix"), \
                unittest.mock.patch.object(
                    install.subprocess, "run",
                    return_value=SimpleNamespace(stdout=output, returncode=0)) as run:
            value = install._read_registry("HKCU\\SOFTWARE\\RetroBat", "LatestKnownInstallPath")
            self.assertEqual(value, "D:\\Games\\RetroBat\\")
            self.assertEqual(install._host_path(value.rstrip("\\")),
                             Path("/mnt/d/Games/RetroBat"))
        self.assertEqual(run.call_args[0][0][:2], ["reg.exe", "query"])


class EmuDeckWindowsIsFoundAsItsScriptsFindIt(unittest.TestCase):
    """EmuDeck's Windows scripts read %APPDATA%\\EmuDeck\\settings.ps1, else
    %USERPROFILE%\\EmuDeck\\settings.ps1, and take C:\\Emulation when neither
    names a folder (functions/all.ps1:4-14). Only the first was read, and a
    forced --platform emudeck wrote into %USERPROFILE%\\Emulation\\bios."""

    def _env(self) -> tuple[Path, Path]:
        root = Path(_tempdir(self))
        appdata, profile = root / "appdata", root / "profile"
        patcher = unittest.mock.patch.dict(
            os.environ, {"APPDATA": str(appdata), "USERPROFILE": str(profile)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return appdata, profile

    @staticmethod
    def _settings(folder: Path, text: str, encoding: str = "utf-8-sig") -> None:
        (folder / "EmuDeck").mkdir(parents=True, exist_ok=True)
        (folder / "EmuDeck" / "settings.ps1").write_bytes(text.encode(encoding))

    def test_the_profile_settings_are_read_when_appdata_has_none(self):
        _appdata, profile = self._env()
        self._settings(profile, '\n$emulationPath="/games/Emulation"\n')
        self.assertEqual(install._emudeck_windows_bios(), Path("/games/Emulation/bios"))

    def test_appdata_comes_first(self):
        appdata, profile = self._env()
        self._settings(profile, '$emulationPath="/b/Emulation"\n')
        self._settings(appdata, '$emulationPath="/a/Emulation"\n', "utf-16")
        self.assertEqual(install._emudeck_windows_bios(), Path("/a/Emulation/bios"))

    def test_settings_naming_no_folder_mean_c_emulation(self):
        appdata, _profile = self._env()
        self._settings(appdata, '$device="Windows"\n')
        self.assertEqual(install._emudeck_windows_bios(),
                         install._host_path("C:\\Emulation") / "bios")

    def test_a_network_install_is_its_share(self):
        appdata, _profile = self._env()
        self._settings(appdata, '$networkInstallation="true"\n'
                       '$VolName="/nas/games"\n'
                       '$emulationPath=$testdrive.ToString() + ":\\Emulation"\n')
        self.assertEqual(install._emudeck_windows_bios(),
                         Path("/nas/games\\Emulation") / "bios")

    def test_no_settings_detects_nothing(self):
        self._env()
        self.assertIsNone(install._emudeck_windows_bios())

    def test_a_forced_platform_falls_back_to_c_emulation(self):
        self.assertEqual(install._default_dest("windows", "emudeck"),
                         install._host_path("C:/Emulation/bios"))


class MuOsIsReadFromItsRetroArchConfig(unittest.TestCase):
    """muOS wrote to /mnt/mmc/MUOS/bios while RetroArch reads
    /run/muos/storage/bios, bound to the second card when it holds MUOS/bios,
    and --installed-cores found no core in /opt/muos/share/core."""

    def _muos(self, *, user_config: bool) -> tuple[Path, Path]:
        root = Path(_tempdir(self))
        muos = root / "opt" / "muos"
        system_dir, cores = root / "run" / "bios", root / "core"
        cores.mkdir(parents=True)
        configs = (muos / "share/info/config/retroarch.cfg",
                   muos / "share/emulator/retroarch/retroarch.default.cfg")
        default = configs[1]
        default.parent.mkdir(parents=True)
        default.write_text(f'libretro_directory = "{cores}"\n'
                           f'system_directory = "{root / "default-bios"}"\n')
        if user_config:
            configs[0].parent.mkdir(parents=True)
            configs[0].write_text(f'libretro_directory = "{cores}"\n'
                                  f'system_directory = "{system_dir}"\n')
        for patcher in (
            unittest.mock.patch.object(install, "MUOS_ROOT", muos),
            unittest.mock.patch.object(install, "MUOS_RETROARCH_CONFIGS", configs),
            unittest.mock.patch.object(
                install, "_detect_embedded",
                lambda: [("retroarch", Path("/run/muos/storage/bios"))]),
            unittest.mock.patch.dict(os.environ, {"HOME": str(root / "home")}),
            unittest.mock.patch.dict(install._RETROARCH_CONFIGS, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return (system_dir if user_config else root / "default-bios"), cores

    def test_the_users_config_decides(self):
        system_dir, cores = self._muos(user_config=True)
        found = install.detect_platforms("linux")
        self.assertIn(("retroarch", system_dir), found)
        self.assertNotIn(("retroarch", Path("/run/muos/storage/bios")), found)
        self.assertIn(cores, install.core_dirs("retroarch", system_dir))

    def test_before_the_first_launch_the_default_does(self):
        system_dir, cores = self._muos(user_config=False)
        self.assertIn(("retroarch", system_dir), install.detect_platforms("linux"))
        self.assertIn(cores, install.core_dirs("retroarch", system_dir))

    def test_without_a_config_the_bound_folder_is_used(self):
        rule = next(r for r in install._EMBEDDED_RULES if r[2] == ("/opt/muos",))
        self.assertEqual(rule[3], "/run/muos/storage/bios")


class BatoceraAndKnulliByWhatTheirBuildsWrite(unittest.TestCase):
    """No Batocera build writes /etc/batocera-version (batocera-system.mk
    writes /usr/share/batocera/batocera.version), and Knulli writes no
    /etc/knulli-release and removes /usr/share/batocera: neither was
    detected, and a Knulli board got every board's files without a word."""

    def _detect(self, release: dict, files: tuple[str, ...]):
        with unittest.mock.patch.object(install, "_parse_os_release", return_value=release), \
                unittest.mock.patch.object(Path, "exists", lambda p: str(p) in files):
            return install._detect_embedded()

    def test_batocera_by_its_version_file(self):
        self.assertEqual(self._detect({}, ("/usr/share/batocera/batocera.version",)),
                         [("batocera", Path("/userdata/bios"))])
        self.assertEqual(self._detect({}, ("/etc/batocera-version",)), [])

    def test_knulli_by_its_os_release_or_version_file(self):
        self.assertEqual(self._detect({"OS_NAME": "knulli"}, ()),
                         [("batocera", Path("/userdata/bios"))])
        self.assertEqual(self._detect({}, ("/usr/share/knulli/knulli.version",)),
                         [("batocera", Path("/userdata/bios"))])

    def test_a_knulli_board_is_named_and_not_narrowed(self):
        board = Path(_tempdir(self)) / "knulli.arch"
        board.write_text("a133")
        with unittest.mock.patch.dict(install._DERIVATIVE_BOARDS,
                                      {"batocera": ("Knulli", str(board))}):
            self.assertEqual(install.derivative_board("batocera"), ("Knulli", "a133"))
            self.assertIsNone(install.derivative_board("recalbox"))

    def test_the_run_says_why_every_board_is_installed(self):
        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        block = source[source.index("board = detect_board(plat_name)"):]
        block = block[:block.index("if target:")]
        self.assertIn("has no", block)
        self.assertIn("derivative_board(plat_name)", block)


class RomMFirmwareFollowsItsTemplate(unittest.TestCase):
    """RomM 5.4 reads firmware where filesystem.structure.firmware says, and
    a {platform}/roms library is told to declare "{platform}/bios". The
    installer only wrote bios/<platform>/, where such a library lists none."""

    CONFIG = (
        "# RomM\n"
        "exclude:\n"
        "  platforms: []\n"
        "filesystem:\n"
        "  structure:\n"
        "    default: \"{platform}/roms/{game}\"\n"
        "    firmware: \"{platform}/bios\"  # as RomM suggests\n"
        "system:\n"
        "  firmware: elsewhere\n"
    )

    def _romm(self, config: str | None) -> Path:
        base = Path(_tempdir(self))
        library = base / "library"
        (library / "psx" / "roms").mkdir(parents=True)
        if config is not None:
            (base / "config").mkdir()
            (base / "config" / "config.yml").write_text(config)
        return library / "bios"

    def test_templates_parse_as_romm_parses_them(self):
        self.assertEqual(install.parse_romm_firmware("bios/{platform}"), (("bios",), ()))
        self.assertEqual(install.parse_romm_firmware("{platform}/bios"), ((), ("bios",)))
        self.assertEqual(install.parse_romm_firmware("/a/{platform}/b/c/"),
                         (("a",), ("b", "c")))
        for bad in ("", "bios", "{platform}/{game}", "{region}/{platform}",
                    "../{platform}", "{platform}/{platform}"):
            with self.subTest(template=bad), self.assertRaises(ValueError):
                install.parse_romm_firmware(bad)

    def test_the_template_is_read_from_config_yml(self):
        bios = self._romm(self.CONFIG)
        layout = install.romm_firmware_layout(bios, None)
        self.assertEqual(layout, ((), ("bios",)))
        files = [{"dest": "psx/scph5501.bin"}, {"dest": "3do/panafz1.bin"}]
        moved, root = install.romm_relayout(files, bios, *layout)
        self.assertEqual(root, bios.parent)
        self.assertEqual([f["dest"] for f in moved],
                         ["psx/bios/scph5501.bin", "3do/bios/panafz1.bin"])

    def test_the_default_keeps_the_pack_layout(self):
        bios = self._romm("filesystem:\n  structure:\n    firmware: bios/{platform}\n")
        files = [{"dest": "psx/scph5501.bin"}]
        self.assertEqual(install.romm_relayout(files, bios, *install.romm_firmware_layout(
            bios, None)), (files, bios))

    def test_a_platform_first_library_without_a_template_is_refused(self):
        bios = self._romm(None)
        with self.assertRaises(ValueError) as raised:
            install.romm_firmware_layout(bios, None)
        self.assertIn('--romm-firmware "{platform}/bios"', str(raised.exception))
        self.assertEqual(install.romm_firmware_layout(bios, "{platform}/bios"),
                         ((), ("bios",)))

    def test_the_option_is_refused_where_romm_is_not_installed(self):
        err = io.StringIO()
        with unittest.mock.patch.object(install, "detect_os", return_value="linux"), \
                unittest.mock.patch("sys.argv", [
                    "install.py", "--platform", "batocera", "--dest", _tempdir(self),
                    "--romm-firmware", "{platform}/bios", "--no-input"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            install.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("--romm-firmware applies to RomM", err.getvalue())


class RomMFirmwareInstallsUnderItsTemplate(unittest.TestCase, _LoopbackRun):
    """The whole run, over loopback: config.yml beside the library decides."""

    def setUp(self):
        self.env = self.serve(self)

    def test_lynx_lands_in_its_platform_folder(self):
        base = self.home / "romm"
        (base / "library" / "lynx" / "roms").mkdir(parents=True)
        (base / "config").mkdir()
        (base / "config" / "config.yml").write_text(
            "filesystem:\n  structure:\n    default: \"{platform}/roms/{game}\"\n"
            "    firmware: \"{platform}/bios\"\n")
        proc = _install_run(self.env, "--platform", "romm", "--dest",
                            str(base / "library" / "bios"), "--system", "atari-lynx",
                            "--no-input")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((base / "library" / "lynx" / "bios" / "lynxboot.img").is_file())
        self.assertFalse((base / "library" / "bios").exists())
        self.assertIn("{platform}/bios", proc.stdout)


if __name__ == "__main__":
    unittest.main()
