"""A target scraper that cannot read its source writes nothing.

The EmuDeck, RetroPie and RetroArch scrapers turned a failed request (an
anonymous API quota, a moved buildbot directory) into an empty core list
and wrote the file anyway: a target vanished, or kept no cores, and the
packs built for it shrank without an error.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.scraper.targets import (  # noqa: E402
    batocera_targets_scraper,
    emudeck_targets_scraper,
    retroarch_targets_scraper,
    retropie_targets_scraper,
)


def _refuse(*_args, **_kwargs):
    raise urllib.error.HTTPError("https://api.github.com/x", 403, "rate limited", {}, None)


class FailedRequestsStopTheScrape(unittest.TestCase):
    def test_every_scraper_raises(self):
        for module in (
            batocera_targets_scraper,
            emudeck_targets_scraper,
            retropie_targets_scraper,
            retroarch_targets_scraper,
        ):
            with self.subTest(scraper=module.__name__), mock.patch.object(
                module.urllib.request, "urlopen", _refuse
            ), self.assertRaises(RuntimeError):
                module.Scraper().fetch_targets()

    def test_an_empty_listing_is_not_a_target(self):
        class _Empty:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"[]"

        for module in (emudeck_targets_scraper, retropie_targets_scraper):
            with self.subTest(scraper=module.__name__), mock.patch.object(
                module.urllib.request, "urlopen", lambda *_a, **_k: _Empty()
            ), self.assertRaises(RuntimeError):
                module.Scraper().fetch_targets()

    def test_batocera_needs_its_boards(self):
        """No board listed, or one board unreadable, used to print a warning
        and write the targets file without those boards."""

        class _Body:
            def __init__(self, body: bytes):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body

        def serve(boards: list[str], refuse_board: bool):
            def urlopen(request, *_a, **_k):
                url = request.full_url
                if "/tags" in url:
                    return _Body(b'[{"name": "batocera-43"}]')
                if "/contents/configs" in url:
                    return _Body(json.dumps([{"name": b} for b in boards]).encode())
                if "/contents/buildroot" in url:
                    return _Body(b'{"sha": "0123abc"}')
                if url.endswith("x86_wow64.board"):
                    # Builds the 32-bit Wine package, not an image.
                    return _Body(b"BR2_PACKAGE_BATOCERA_TARGET_X86=y\n")
                if url.endswith(".board"):
                    if refuse_board:
                        _refuse()
                    return _Body(b"BR2_PACKAGE_BATOCERA_TARGET_X86_64=y\n"
                                 b"BR2_PACKAGE_BATOCERA_SYSTEM=y\n")
                if url.endswith("Config.in"):
                    return _Body(b"config BR2_PACKAGE_BATOCERA_SYSTEM\n"
                                 b"\tselect BR2_PACKAGE_MGBA\n")
                return _Body(
                    b"gba:\n  emulators:\n    libretro:\n"
                    b"      mgba: {requireAnyOf: [BR2_PACKAGE_MGBA]}\n"
                )

            return urlopen

        for boards, refuse_board in (([], False), (["batocera-x86_64.board"], True)):
            with self.subTest(boards=boards), mock.patch.object(
                batocera_targets_scraper.urllib.request,
                "urlopen",
                serve(boards, refuse_board),
            ), self.assertRaises(RuntimeError):
                batocera_targets_scraper.Scraper().fetch_targets()

        with mock.patch.object(
            batocera_targets_scraper.urllib.request,
            "urlopen",
            serve(["batocera-x86_64.board", "batocera-x86_wow64.board"], False),
        ):
            targets = batocera_targets_scraper.Scraper().fetch_targets()["targets"]
        self.assertEqual(targets["x86_64"]["cores"], ["mgba"])
        self.assertNotIn("x86_wow64", targets)


class BatoceraKconfig(unittest.TestCase):
    """The Batocera target scraper read every select as unconditional and
    never set a symbol Buildroot sets: 171 cores on almost every board, and
    xroar ("if BR2_PACKAGE_HAS_LIBGL") on none, so --target x86_64 dropped
    the CoCo and Dragon systems."""

    CONFIG_IN = (
        "if BR2_PACKAGE_BATOCERA_TARGET_X86_64\n"
        "\tconfig BR2_PACKAGE_BATOCERA_TARGET_X86_64_ANY\n\tdefault y\nendif\n\n"
        "config BR2_PACKAGE_BATOCERA_SYSTEM\n"
        "\tselect BR2_PACKAGE_BATOCERA_XORG\tif BR2_PACKAGE_BATOCERA_TARGET_X86_64_ANY\n"
        "\tselect BR2_PACKAGE_XROAR\t\tif BR2_PACKAGE_HAS_LIBGL # needs OpenGL\n"
        "\tselect BR2_PACKAGE_RUFFLE\tif BR2_PACKAGE_XORG7 && \\\n"
        "\t\t\t\tBR2_ARCH_IS_64\n\n"
        "config BR2_PACKAGE_BATOCERA_XORG\n"
        "\tselect BR2_PACKAGE_XORG7\n"
        "\tselect BR2_PACKAGE_MESA3D_OPENGL_GLX if BR2_PACKAGE_XORG7\n"
        "\tselect BR2_PACKAGE_LIBGLVND if BR2_PACKAGE_XORG7\n\n"
        "config BR2_PACKAGE_BATOCERA_CORES_EXPERIMENTAL\n"
        "\tselect BR2_PACKAGE_APPLEWIN\n"
    )
    BUILDROOT = (
        "config BR2_x86_64\n\tbool \"x86_64\"\n\tselect BR2_ARCH_IS_64\n"
        "config BR2_PACKAGE_MESA3D_OPENGL_GLX\n"
        "\tselect BR2_PACKAGE_HAS_LIBGL if !BR2_PACKAGE_LIBGLVND\n"
        "\tselect BR2_PACKAGE_LIBGLVND_DISPATCH_GL if BR2_PACKAGE_LIBGLVND\n"
        "config BR2_PACKAGE_LIBGLVND_DISPATCH_GL\n\tselect BR2_PACKAGE_HAS_LIBGL\n"
    )

    def active(self, board: set[str]) -> frozenset[str]:
        from scraper.targets.batocera_targets_scraper import (  # noqa: PLC0415
            _active_symbols,
            _parse_meta_flags,
            _parse_owned_selects,
        )

        rules = _parse_owned_selects(self.CONFIG_IN) + _parse_owned_selects(self.BUILDROOT)
        return _active_symbols(board, _parse_meta_flags(self.CONFIG_IN), rules)

    def test_x86_64_gets_opengl_and_its_cores(self):
        active = self.active({"BR2_x86_64", "BR2_PACKAGE_BATOCERA_TARGET_X86_64",
                              "BR2_PACKAGE_BATOCERA_SYSTEM"})
        self.assertIn("BR2_PACKAGE_HAS_LIBGL", active)
        self.assertIn("BR2_PACKAGE_XROAR", active)
        self.assertIn("BR2_PACKAGE_RUFFLE", active)
        self.assertNotIn("BR2_PACKAGE_APPLEWIN", active)

    def test_a_board_without_x11_gets_none_of_it(self):
        active = self.active({"BR2_aarch64", "BR2_PACKAGE_BATOCERA_TARGET_RK3588",
                              "BR2_PACKAGE_BATOCERA_SYSTEM"})
        self.assertFalse({"BR2_PACKAGE_XORG7", "BR2_PACKAGE_XROAR",
                          "BR2_PACKAGE_RUFFLE"} & active)


class RetroPieModuleFlags(unittest.TestCase):
    """The RetroPie target list read flags as requirements and libretro alone.

    sdl1 and sdl2 are build options, not hardware: read as requirements they
    removed dosbox, atari800 and linapple from every target, and with only
    scriptmodules/libretrocores listed, no standalone package (openmsx,
    xroar, amiberry) reached a targeted pack at all.
    """

    def test_flags_follow_rp_register_module(self):
        available = retropie_targets_scraper._is_available
        cases = {
            ("sdl1 !mali", "rpi4"): True,
            ("!all videocore", "rpi3"): True,
            ("!all videocore", "rpi4"): False,
            ("!armv6 !videocore !:\\$__gcc_version:-lt:9", "rpi3"): False,
            ("!armv6 !videocore !:\\$__gcc_version:-lt:9", "rpi4"): True,
            ("!all x86", "rpi4"): False,
            ("!all x86", "x86_64"): True,
            ("!all arm rpi3 rpi4 rpi5 x86", "rpi1"): True,
        }
        for (flags, platform), expected in cases.items():
            with self.subTest(flags=flags, platform=platform):
                self.assertIs(available(flags, platform), expected)

    def test_standalone_sections_are_listed_and_keep_their_id(self):
        listings = {
            "emulators": [{"name": "dosbox-staging.sh"}, {"name": "dosbox.sh"}],
            "libretrocores": [{"name": "lr-beetle-psx.sh"}, {"name": "lr-dosbox.sh"}],
            "ports": [{"name": "openbor.sh"}],
        }
        modules = {
            "emulators/dosbox-staging.sh": 'rp_module_id="dosbox-staging"\nrp_module_flags="sdl2"',
            "libretrocores/lr-beetle-psx.sh": 'rp_module_id="lr-beetle-psx"',
            "libretrocores/lr-dosbox.sh": 'rp_module_id="lr-dosbox"',
            "emulators/dosbox.sh": 'rp_module_id="dosbox"\nrp_module_flags="sdl1 !mali"',
            "ports/openbor.sh": 'rp_module_id="openbor"\nrp_module_flags="sdl1 !mali !x11"',
        }

        def fake_fetch(url, **_kwargs):
            section = url.rstrip("/").rsplit("/", 1)[-1]
            if section in listings:
                return json.dumps(listings[section])
            return modules[url.split("scriptmodules/", 1)[1]]

        with mock.patch.object(retropie_targets_scraper, "_fetch", fake_fetch):
            targets = retropie_targets_scraper.Scraper().fetch_targets()["targets"]
        self.assertEqual(
            targets["rpi4"]["cores"], ["beetle_psx", "dosbox", "dosbox-staging", "openbor"]
        )
        self.assertEqual(targets["x86_64"]["cores"], ["beetle_psx", "dosbox", "dosbox-staging"])



class HashScrapersRefuseAnUnknownVersion(unittest.TestCase):
    """mame_hash_scraper wrote core_version "unknown" after a failed API call
    and cached it for a day; the pipeline step still read OK."""

    def test_mame_raises(self):
        from scripts.scraper import mame_hash_scraper  # noqa: PLC0415

        with mock.patch.object(mame_hash_scraper.urllib.request, "urlopen", _refuse), \
                self.assertRaises(RuntimeError):
            mame_hash_scraper._get_version()


if __name__ == "__main__":
    unittest.main()
