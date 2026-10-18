#!/usr/bin/env python3
"""Scraper for Batocera batocera-systems.

Source: https://github.com/batocera-linux/batocera.linux/.../batocera-systems
Format: Python dict with systems -> biosFiles
Hash: MD5 primary
"""

from __future__ import annotations

import ast
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


from common import yaml_load
from safeparse import yaml_load_upstream

from .base_scraper import (
    BaseScraper,
    BiosRequirement,
    github_headers,
    requirement_entry,
)

PLATFORM_NAME = "batocera"

_RAW_BASE = "https://raw.githubusercontent.com/batocera-linux/batocera.linux"


def pick_stable_tag(names: list[str]) -> str | None:
    """Pick the highest batocera-N(.M) tag from a list of tag names."""
    import re

    stable = []
    for name in names:
        m = re.fullmatch(r"batocera-(\d+(?:\.\d+)?)", name)
        if m:
            stable.append((tuple(int(x) for x in m.group(1).split(".")), name))
    return max(stable)[1] if stable else None


def fetch_stable_tag() -> str:
    """Return the newest stable batocera-N(.M) tag name.

    Raises rather than falling back to master: the YAML records the tag
    it was read from, and the exporter patches that revision.
    """
    url = "https://api.github.com/repos/batocera-linux/batocera.linux/tags?per_page=100"
    try:
        req = urllib.request.Request(url, headers=github_headers())
        with urllib.request.urlopen(req, timeout=15) as resp:
            tags = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot list batocera tags: {e}") from e
    tag = pick_stable_tag([tag["name"] for tag in tags])
    if not tag:
        raise RuntimeError("no stable batocera tag among the latest 100")
    return tag


SOURCE_URL = (
    _RAW_BASE + "/{tag}"
    "/package/batocera/core/batocera-scripts/scripts/batocera-systems"
)

CONFIGGEN_DEFAULTS_URL = (
    _RAW_BASE + "/{tag}"
    "/package/batocera/core/batocera-configgen/configs/"
    "configgen-defaults.yml"
)

ES_SYSTEMS_URL = (
    _RAW_BASE + "/{tag}"
    "/package/batocera/emulationstation/batocera-es-system/es_systems.yml"
)

# Names es_systems.yml files under an emulator that are not emulators:
# machine selectors, video plugins, launchers. Read from the overrides the
# target scraper applies, so one list says what is not a core.
OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "platforms" / "targets" / "_overrides.yml"


def _not_cores() -> set[str]:
    data = yaml_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    default = (data.get("batocera", {}).get("targets", {}) or {}).get("_default", {}) or {}
    return {str(name) for name in default.get("remove_cores", [])}


def es_systems_emulators(text: str) -> tuple[set[str], set[str]]:
    """Every core es_systems.yml offers, and those that run standalone.

    ES lists, per system, each emulator a user may pick and the cores under
    it; configgen-defaults.yml only names the one picked by default, so
    reading it alone left DOSBox Staging, melonDS DS and nine other built
    cores outside the pack.
    """
    data = yaml_load_upstream(text)
    if not isinstance(data, dict):
        raise ValueError("es_systems.yml is not a mapping")
    cores: set[str] = set()
    standalone: set[str] = set()
    for system in data.values():
        emulators = system.get("emulators") if isinstance(system, dict) else None
        if not isinstance(emulators, dict):
            continue
        for emulator, entries in emulators.items():
            if not isinstance(entries, dict):
                continue
            # A standalone emulator is its own core; what it lists under it
            # are machines or builds, and the non-cores among them are named
            # in the overrides. Keys whose value is not a mapping
            # (archs_include) describe the entry, not a core.
            if emulator != "libretro":
                cores.add(str(emulator))
                standalone.add(str(emulator))
            for core, spec in entries.items():
                if not isinstance(spec, dict):
                    continue
                cores.add(str(core))
                if emulator != "libretro":
                    standalone.add(str(core))
    return cores, standalone


def es_systems_libretro(text: str) -> set[str]:
    """The cores es_systems.yml offers under `libretro:` for some system."""
    data = yaml_load_upstream(text)
    if not isinstance(data, dict):
        raise ValueError("es_systems.yml is not a mapping")
    found: set[str] = set()
    for system in data.values():
        emulators = system.get("emulators") if isinstance(system, dict) else None
        libretro = emulators.get("libretro") if isinstance(emulators, dict) else None
        if isinstance(libretro, dict):
            found.update(str(core) for core, spec in libretro.items() if isinstance(spec, dict))
    return found


SYSTEM_SLUG_MAP = {
    "advision": "entex-advision",
    "amiga500": "commodore-amiga",
    "bbc": "acorn-bbc-micro",
    "bk": "elektronika-bk",
    "gp32": "gamepark-gp32",
    # MSU-MD runs on the Mega Drive cores and loads the Mega-CD BIOS.
    "megadrive-msu": "sega-mega-cd",
    "segaai": "sega-ai",
    "socrates": "vtech-socrates",
    "sufami": "nintendo-sufami-turbo",
    "videopacplus": "philips-videopac",
    "vsmile": "vtech-vsmile",
    "atari800": "atari-400-800",
    "atari5200": "atari-5200",
    "atarist": "atari-st",
    "lynx": "atari-lynx",
    "3do": "3do",
    "amiga": "commodore-amiga",
    "amiga600": "commodore-amiga",
    "amiga1200": "commodore-amiga",
    "amigacd32": "commodore-amiga",
    "amigacdtv": "commodore-amiga",
    "c128": "commodore-c128",
    "colecovision": "coleco-colecovision",
    "dreamcast": "sega-dreamcast",
    "naomi": "sega-dreamcast-arcade",
    "naomi2": "sega-dreamcast-arcade",
    "atomiswave": "sega-dreamcast-arcade",
    "fds": "nintendo-fds",
    "gamecube": "nintendo-gamecube",
    "gb": "nintendo-gb",
    "gba": "nintendo-gba",
    "gbc": "nintendo-gbc",
    "nds": "nintendo-ds",
    "n64dd": "nintendo-64dd",
    "satellaview": "nintendo-satellaview",
    "sgb": "nintendo-sgb",
    "snes": "nintendo-snes",
    "channelf": "fairchild-channel-f",
    "intellivision": "mattel-intellivision",
    "msx": "microsoft-msx",
    "msx1": "microsoft-msx",
    "msx2": "microsoft-msx",
    "msxturbor": "microsoft-msx",
    "neogeo": "snk-neogeo",
    "neogeocd": "snk-neogeo-cd",
    "odyssey2": "magnavox-odyssey2",
    "pcengine": "nec-pc-engine",
    "pcenginecd": "nec-pc-engine",
    "supergrafx": "nec-pc-engine",
    "pc88": "nec-pc-88",
    "pc98": "nec-pc-98",
    "pcfx": "nec-pc-fx",
    "psx": "sony-playstation",
    "ps2": "sony-playstation-2",
    "psp": "sony-psp",
    "saturn": "sega-saturn",
    "segacd": "sega-mega-cd",
    "mastersystem": "sega-master-system",
    "megadrive": "sega-mega-drive",
    "gamegear": "sega-game-gear",
    "x1": "sharp-x1",
    "x68000": "sharp-x68000",
    "zxspectrum": "sinclair-zx-spectrum",
    "scummvm": "scummvm",
    "doom": "doom",
    "macintosh": "apple-macintosh-ii",
    "dos": "dos",
    "videopac": "philips-videopac",
    "pokemini": "nintendo-pokemon-mini",
    "gsplus": "apple-iigs",
    "apple2": "apple-ii",
    "apple2gs": "apple-iigs",
    "ps3": "sony-playstation-3",
    "psvita": "sony-playstation-vita",
    "coco": "coco",
    "dragon32": "dragon32",
    "dragon64": "dragon64",
    "mc10": "mc10",
    "msx2+": "microsoft-msx",
    "spectravideo": "spectravideo",
    "tvc": "videoton-tvc",
    "enterprise": "enterprise-64-128",
    "vis": "tandy-vis",
    "supracan": "supracan",
    "jaguar": "atari-jaguar",
    "jaguarcd": "atari-jaguar",
    "switch": "nintendo-switch",
    "wii": "nintendo-wii",
    "xbox360": "microsoft-xbox-360",
}


_MD5_RE = re.compile(r"^[a-fA-F0-9]+$")


def _load_md5_index() -> dict[str, str]:
    """Load by_md5 index from database.json for prefix resolution."""
    db_path = Path(__file__).resolve().parents[2] / "database.json"
    if not db_path.exists():
        return {}
    with open(db_path) as f:
        db = json.load(f)
    return db.get("indexes", {}).get("by_md5", {})


def _resolve_truncated_md5(md5: str, md5_index: dict[str, str]) -> str:
    """Resolve a truncated MD5 to its full 32-char version via prefix match."""
    if not md5 or len(md5) == 32:
        return md5
    if not _MD5_RE.match(md5):
        return md5
    matches = [k for k in md5_index if k.startswith(md5)]
    if len(matches) == 1:
        print(f"  fixed truncated md5: {md5} -> {matches[0]}", file=sys.stderr)
        return matches[0]
    return md5


class Scraper(BaseScraper):
    """Scraper for batocera-systems Python dict."""

    def __init__(self) -> None:
        self.tag = fetch_stable_tag()
        super().__init__(url=SOURCE_URL.format(tag=self.tag))

    def _fetch_cores(self) -> tuple[list[str], list[str], list[str]]:
        """Extract core names and standalone cores from configgen-defaults.yml.

        Returns (all_cores, standalone_cores, dual_mode_cores): standalone
        cores are those with emulator != "libretro", dual-mode ones those
        es_systems.yml also offers under libretro (dolphin for GameCube).
        """
        url = CONFIGGEN_DEFAULTS_URL.format(tag=self.tag)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "retrobios-scraper/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Failed to fetch {url}: {e}"
            ) from e
        data = yaml_load_upstream(raw)
        es_systems = self._fetch_text(ES_SYSTEMS_URL.format(tag=self.tag))
        cores, standalone = es_systems_emulators(es_systems)
        libretro = es_systems_libretro(es_systems)
        if not cores:
            raise RuntimeError("es_systems.yml names no emulator")
        for system, cfg in data.items():
            if system == "default" or not isinstance(cfg, dict):
                continue
            emulator = cfg.get("emulator", "")
            core = cfg.get("core", "")
            if core:
                cores.add(core)
            if emulator and emulator != "libretro":
                standalone.add(emulator)
                if core and core != emulator:
                    standalone.add(core)
        dropped = _not_cores()
        dual = (standalone & libretro) - dropped
        return sorted(cores - dropped), sorted(standalone - dropped), sorted(dual)

    def _fetch_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "retrobios-scraper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Failed to fetch {url}: {e}") from e

    def _extract_systems_dict(self, raw: str) -> dict:
        """Extract and parse the 'systems' dict from the Python source via ast.literal_eval."""
        match = re.search(r"^systems\s*=\s*\{", raw, re.MULTILINE)
        if not match:
            raise ValueError("Could not find 'systems = {' in batocera-systems")

        start = match.start() + raw[match.start() :].index("{")
        depth = 0
        i = start
        in_str = False
        str_ch = None
        while i < len(raw):
            ch = raw[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == str_ch:
                    in_str = False
            elif ch in ('"', "'"):
                in_str = True
                str_ch = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            elif ch == "#":
                while i < len(raw) and raw[i] != "\n":
                    i += 1
            i += 1

        dict_str = raw[start : i + 1]

        lines = []
        for line in dict_str.split("\n"):
            in_string = False
            string_char = None
            clean = []
            j = 0
            while j < len(line):
                ch = line[j]
                if ch == "\\" and j + 1 < len(line):
                    clean.append(ch)
                    clean.append(line[j + 1])
                    j += 2
                    continue
                if ch in ('"', "'") and not in_string:
                    in_string = True
                    string_char = ch
                    clean.append(ch)
                elif ch == string_char and in_string:
                    in_string = False
                    clean.append(ch)
                elif ch == "#" and not in_string:
                    break
                else:
                    clean.append(ch)
                j += 1
            lines.append("".join(clean))

        clean_dict_str = "\n".join(lines)

        # OrderedDict({...}) -> just the inner dict literal
        clean_dict_str = re.sub(r"OrderedDict\(\s*\{", "{", clean_dict_str)
        clean_dict_str = re.sub(r"\}\s*\)", "}", clean_dict_str)

        try:
            return ast.literal_eval(clean_dict_str)
        except (SyntaxError, ValueError) as e:
            raise ValueError(f"Failed to parse systems dict: {e}") from e

    def fetch_requirements(self) -> list[BiosRequirement]:
        """Parse batocera-systems and return BIOS requirements."""
        raw = self._fetch_raw()

        if not self.validate_format(raw):
            raise ValueError("batocera-systems format validation failed")

        systems = self._extract_systems_dict(raw)
        requirements = []
        md5_index = _load_md5_index()

        for sys_key, sys_data in systems.items():
            system_slug = SYSTEM_SLUG_MAP.get(sys_key, sys_key)
            bios_files = sys_data.get("biosFiles", [])

            for bios in bios_files:
                declared_path = bios.get("file", "")
                file_path = declared_path
                md5 = _resolve_truncated_md5(bios.get("md5", ""), md5_index)
                zipped_file = bios.get("zippedFile", "")

                if file_path.startswith("bios/"):
                    file_path = file_path[5:]

                name = file_path.split("/")[-1] if "/" in file_path else file_path

                requirements.append(
                    BiosRequirement(
                        name=name,
                        system=system_slug,
                        md5=md5 or None,
                        destination=file_path,
                        native_path=declared_path,
                        required=True,
                        zipped_file=zipped_file or None,
                        native_id=sys_key,
                        native={"native_name": sys_data.get("name", "")},
                    )
                )

        return requirements

    def validate_format(self, raw_data: str) -> bool:
        """Validate batocera-systems format."""
        has_systems = "systems" in raw_data and "biosFiles" in raw_data
        has_dict = re.search(r"^systems\s*=\s*\{", raw_data, re.MULTILINE) is not None
        has_md5 = '"md5"' in raw_data
        has_file = '"file"' in raw_data
        return has_systems and has_dict and has_md5 and has_file

    def generate_platform_yaml(self) -> dict:
        """Generate a platform YAML config dict from scraped data."""
        requirements = self.fetch_requirements()

        # Parse source to extract display names per system
        raw = self._fetch_raw()
        source_dict = self._extract_systems_dict(raw)
        display_names: dict[str, str] = {}
        for sys_key, sys_data in source_dict.items():
            dname = sys_data.get("name", "")
            if dname:
                slug = SYSTEM_SLUG_MAP.get(sys_key, sys_key)
                display_names[slug] = dname

        systems = {}
        for req in requirements:
            if req.system not in systems:
                sys_entry: dict = {"files": []}
                if req.native_id:
                    sys_entry["native_id"] = req.native_id
                dname = display_names.get(req.system)
                if dname:
                    sys_entry["name"] = dname
                systems[req.system] = sys_entry

            systems[req.system]["files"].append(requirement_entry(req))

        cores, standalone, dual = self._fetch_cores()
        result = {
            "platform": "Batocera",
            "version": self.tag.removeprefix("batocera-"),
            "homepage": "https://batocera.org",
            "source": self.url,
            "base_destination": "bios",
            "hash_type": "md5",
            "verification_mode": "md5",
            "cores": cores,
            "systems": systems,
        }
        if standalone:
            result["standalone_cores"] = standalone
        if dual:
            # Launched both ways: each layout gets its files.
            result["dual_mode_cores"] = dual
        return result


def main() -> None:
    from scripts.scraper.base_scraper import scraper_cli

    scraper_cli(Scraper, "Scrape batocera BIOS requirements")


if __name__ == "__main__":
    main()
