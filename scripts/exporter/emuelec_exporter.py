"""Exporter for EmuELEC's batocera-systems.

AmberELEC's case: paths relative to /storage/roms with ports/ and neogeo/
beside bios/, and no runner gate, since EmuELEC's checkBios never reads one.
The dict closes on an indented brace.
"""

from __future__ import annotations

from .amberelec_exporter import Exporter as AmberELECExporter

SOURCE_URL = (
    "https://raw.githubusercontent.com/EmuELEC/EmuELEC/master"
    "/packages/sx05re/emuelec/bin/batocera/batocera-systems"
)


class Exporter(AmberELECExporter):
    """Write EmuELEC's batocera-systems, corrected."""

    @staticmethod
    def platform_name() -> str:
        return "emuelec"

    @staticmethod
    def file_modes() -> dict[str, int]:
        # 100755 at v4.8 (git/trees).
        return {"batocera-systems": 0o755}

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"batocera-systems": SOURCE_URL}
