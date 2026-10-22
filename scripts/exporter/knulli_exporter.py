"""Exporter for KNULLI's knulli-systems.

A fork of Batocera's script, checked the same way: es_systems names a
libretro core under `libretro` and a standalone by itself, and entries carry
Batocera's emulator and core gates.
"""

from __future__ import annotations

from .batocera_exporter import Exporter as BatoceraExporter

SOURCE_URL = (
    "https://raw.githubusercontent.com/knulli-cfw/knulli-linux/knulli-main"
    "/package/system/knulli-scripts/scripts/knulli-systems"
)


class Exporter(BatoceraExporter):
    """Write KNULLI's knulli-systems, corrected."""

    @staticmethod
    def platform_name() -> str:
        return "knulli"

    @staticmethod
    def native_filename() -> str:
        return "knulli-systems"

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"knulli-systems": SOURCE_URL}
