#!/usr/bin/env python3
"""Scraper for EmuELEC's batocera-systems.

Source: packages/sx05re/emuelec/bin/batocera/batocera-systems
Format: Python source, Batocera's `systems` dict, parsed by batocera_scraper
Hash: MD5

`naomi` and `Dinothawr` are written twice in the dict. Python keeps the later
value of a repeated key when the script runs, and so does the parser: naomi
is the entry at line 461 (naomi_boot.bin and naomi.zip, line 444 naming
naomi.zip alone), Dinothawr the one at line 776 (dinothawr.game, line 494
naming level_3-3.tmx).

Verification (batocera-systems at the release tag):
- checkBios (lines 817-833): md5sum() of the file at /storage/roms/<file>
  against the declared md5, an empty md5 meaning presence only. It never
  reads zippedFile: the FM Towns entries that name a member hash the whole
  archive against the member's md5. There is no emulator or core gate.
- Run with no argument by EmulationStation's BIOS screen
  (EmuELEC/emuelec-emulationstation 09356d0d, es-app/src/ApiSystem.cpp:
  645-655, `--filter <system>` for one system), and with `--strictfilter
  <platform>` by ee_check_bios after a failed launch
  (packages/sx05re/emuelec/profile.d/99-emuelec.conf:261, called from
  emuelecRunEmu.sh:605).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

from .base_scraper import BaseScraper, BiosRequirement, fetch_github_latest_version
from .batocera_scraper import (
    SYSTEM_SLUG_MAP as BATOCERA_SLUGS,
)
from .batocera_scraper import (
    extract_systems_dict,
    platform_systems,
    requirements_from_systems,
)
from .es_emulators import es_cores, json_emulators

PLATFORM_NAME = "emuelec"

REPO = "EmuELEC/EmuELEC"
_RAW = "https://raw.githubusercontent.com/EmuELEC/EmuELEC/{ref}/"
SOURCE_TEMPLATE = _RAW + "packages/sx05re/emuelec/bin/batocera/batocera-systems"
ES_SYSTEMS_TEMPLATE = _RAW + "packages/sx05re/emuelec-emulationstation/config/es_systems.json"

# Batocera's names, plus the keys EmuELEC writes that Batocera does not:
# console keys under the slug the other scrapers give them, and each port
# under the system of the profile of the engine its name gives
# (devilutionx, prboom, opentyrian, cannonball, nxengine, tyrquake,
# reminiscence, xrick, vvvvvv, eduke32, sonic2013, sonic-mania, dinothawr).
SYSTEM_SLUG_MAP = {
    **BATOCERA_SLUGS,
    "atari7800": "atari-7800",
    "atarilynx": "atari-lynx",
    "n64": "nintendo-64",
    "o2em": "magnavox-odyssey2",
    "Diablo": "diablo",
    "Hellfire": "diablo",
    "Dinothawr": "dinothawr",
    "Quake": "quake",
    "VVVVVV": "vvvvvv",
    "DOOM1": "doom",
    "DOOM2": "doom",
    "OpenTyrian": "tyrian",
    "Cannonball": "sega-outrun",
    "CaveStory": "cave-story",
    "REminiscence": "flashback",
    "RickDangerous": "xrick",
    "eduke32": "duke3d",
    "sonic1": "sonic-1-2013",
    "sonic2": "sonic-2-2013",
    "sonicmania": "sonic-mania",
}

# es_systems.json names libretro cores under `libretro`, and `retrorun`
# loads the libretro core it lists.
RUNNERS: dict[str, str | None] = {"libretro": None, "retrorun": None}
# packages/sx05re/emulators/PPSSPPSDL builds PPSSPP's SDL front end.
RENAMED = {"PPSSPPSDL": "ppsspp"}


class Scraper(BaseScraper):
    """Scraper for EmuELEC's batocera-systems."""

    def __init__(self, url: str | None = None) -> None:
        self.tag = "" if url else fetch_github_latest_version(REPO)
        super().__init__(url=url or SOURCE_TEMPLATE.format(ref=self.tag))

    def _fetch_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "retrobios-scraper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Failed to fetch {url}: {e}") from e

    def validate_format(self, raw_data: str) -> bool:
        has_dict = re.search(r"^systems\s*=\s*\{", raw_data, re.MULTILINE) is not None
        return has_dict and "def checkBios" in raw_data and '"biosFiles"' in raw_data

    def fetch_requirements(self) -> list[BiosRequirement]:
        raw = self._fetch_raw()
        if not self.validate_format(raw):
            raise ValueError("batocera-systems format validation failed")
        # Paths are relative to /storage/roms (`prefix`, lines 859-865): the
        # list names neogeo/ and ports/ beside bios/.
        return requirements_from_systems(extract_systems_dict(raw), SYSTEM_SLUG_MAP, strip="")

    def generate_platform_yaml(self) -> dict:
        systems = platform_systems(
            self.fetch_requirements(), extract_systems_dict(self._fetch_raw()), SYSTEM_SLUG_MAP
        )
        cores, standalone, dual = es_cores(
            json_emulators(self._fetch_text(ES_SYSTEMS_TEMPLATE.format(ref=self.tag))),
            RUNNERS,
            RENAMED,
        )
        return {
            "platform": "EmuELEC",
            "version": self.tag.removeprefix("v"),
            "homepage": "https://emuelec.org",
            "source": self.url,
            "base_destination": "",
            # RetroArch's system_directory is /storage/roms/bios
            # (packages/sx05re/libretro_base/retroarch/package.mk:117).
            "core_destination": "bios",
            "hash_type": "md5",
            "verification_mode": "md5",
            "cores": cores,
            "standalone_cores": standalone,
            "dual_mode_cores": dual,
            "systems": systems,
        }


def main() -> None:
    from .base_scraper import scraper_cli

    scraper_cli(Scraper, "Scrape EmuELEC BIOS requirements")


if __name__ == "__main__":
    main()
