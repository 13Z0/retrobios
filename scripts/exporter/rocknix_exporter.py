"""Exporter for ROCKNIX's rocknix-systems.

Same shape as Batocera's script: a systems mapping inside a checker ROCKNIX
runs, so only the mapping is rewritten.
"""

from __future__ import annotations

import re

from .baseline import NativeSystem, Report
from .batocera_exporter import Exporter as BatoceraExporter

SOURCE_URL = (
    "https://raw.githubusercontent.com/ROCKNIX/distribution/next/projects/ROCKNIX"
    "/packages/rocknix/sources/scripts/rocknix-systems"
)


EMULATORS_URL = (
    "https://raw.githubusercontent.com/ROCKNIX/distribution/next/projects/ROCKNIX"
    "/packages/virtual/emulators/package.mk"
)
EMULATORS_MK = "emulators/package.mk"
# `add_emu_core <system> <emulator> <core> <default>`: the pairs es_systems.cfg
# lists, and so the names checkBios's emuDict holds.
_ADD_EMU_CORE = re.compile(r"^\s*add_emu_core\s+(\S+)\s+(\S+)\s+(\S+)", re.MULTILINE)


class Exporter(BatoceraExporter):
    """Write ROCKNIX's rocknix-systems, corrected."""

    def __init__(self) -> None:
        super().__init__()
        self._emulator_of: dict[str, str] = {}

    def _runner_pair(self, runner: tuple[bool, str]) -> tuple[str, str | None] | None:
        """ROCKNIX registers libretro cores under `retroarch` and each
        standalone under its own emulator (`azahar azahar-sa`), in the
        emulators package; the `libretro` Batocera writes names nothing
        there."""
        _standalone, name = runner
        emulator = self._emulator_of.get(name)
        return (emulator, name) if emulator else None

    def render(
        self,
        systems: dict[str, NativeSystem],
        report: Report,
        originals: dict[str, str],
        scraped: dict | None = None,
    ) -> dict[str, str]:
        self._emulator_of = {
            core: emulator
            for _system, emulator, core in _ADD_EMU_CORE.findall(
                originals.get(EMULATORS_MK, "")
            )
        }
        return super().render(systems, report, originals, scraped)

    @staticmethod
    def platform_name() -> str:
        return "rocknix"

    @staticmethod
    def native_filename() -> str:
        return "rocknix-systems"

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"rocknix-systems": SOURCE_URL, EMULATORS_MK: EMULATORS_URL}
