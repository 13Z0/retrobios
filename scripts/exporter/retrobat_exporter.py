"""Exporter for RetroBat's batocera-systems.json.

Pure data: a system key carrying a name and a biosFiles array whose entries
state md5 then file, in that order.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from .base_exporter import BaseExporter
from .baseline import NativeFile, NativeSystem, Report

SOURCE_URL = (
    "https://raw.githubusercontent.com/RetroBat-Official/emulatorlauncher/master"
    "/batocera-systems/Resources/batocera-systems.json"
)


class Exporter(BaseExporter):
    """Write RetroBat's batocera-systems.json, corrected."""

    # One md5 per entry, compared as Batocera's checkBios does.
    SINGLE_HASH_FIELD = "md5"

    @staticmethod
    def platform_name() -> str:
        return "retrobat"

    @staticmethod
    def native_filename() -> str:
        return "batocera-systems.json"

    @staticmethod
    def requires() -> str:
        return "md5"

    @staticmethod
    def keys_by_system() -> bool:
        return True

    @staticmethod
    def carries() -> frozenset[str]:
        return frozenset({"md5"})

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"batocera-systems.json": SOURCE_URL}

    def states(self, fe: NativeFile, field_name: str) -> bool:
        if field_name == "md5" and "md5" in fe.filled:
            return bool(self.single_value(fe, "md5"))
        return super().states(fe, field_name)

    def render(
        self,
        systems: dict[str, NativeSystem],
        report: Report,
        originals: dict[str, str],
        scraped: dict | None = None,
    ) -> dict[str, str]:
        # The platform's own file is the base: its key and entry order stay,
        # so the diff a maintainer reads is the corrections and nothing else,
        # and a system or file our transcription does not carry (RetroBat
        # renamed ic10_g24lc64 after a scrape) is kept as RetroBat wrote it.
        base: dict = {}
        original = originals.get(self.native_filename(), "")
        if original:
            try:
                base = json.loads(original, object_pairs_hook=OrderedDict)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"RetroBat's {self.native_filename()} does not parse ({exc}); "
                    "fetch it again with --refresh-cache"
                ) from exc

        exportable = {
            system.native_id: (system, files)
            for system, files in self.exportable(systems, require="md5")
        }
        keys = list(base)
        keys.extend(sorted(k for k in exportable if k not in base))

        output: OrderedDict[str, object] = OrderedDict()
        for key in keys:
            if key not in exportable:
                output[key] = base[key]
                continue
            system, files = exportable[key]
            ours: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
            for fe in files:
                entry: OrderedDict[str, str] = OrderedDict()
                entry["md5"] = self.single_value(fe, "md5")
                declared = fe.native("native_path", "")
                entry["file"] = str(declared) if declared else f"bios/{fe.destination}"
                ours.setdefault(entry["file"], entry)
            bios_files = []
            for theirs in (base.get(key) or {}).get("biosFiles", []):
                bios_files.append(ours.pop(theirs.get("file"), theirs))
            bios_files.extend(ours.values())
            system_entry: OrderedDict[str, object] = OrderedDict()
            system_entry["name"] = self.display_name(system)
            system_entry["biosFiles"] = bios_files
            output[key] = system_entry

        text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        return {self.native_filename(): text}

    def validate(
        self,
        systems: dict[str, NativeSystem],
        produced: dict[str, str],
    ) -> list[str]:
        try:
            data = json.loads(produced[self.native_filename()])
        except json.JSONDecodeError as exc:
            return [f"the JSON does not parse: {exc}"]

        issues: list[str] = []
        for key, entry in data.items():
            if not entry.get("name"):
                issues.append(f"system without a name: {key}")
            if not entry.get("biosFiles"):
                issues.append(f"empty entry: {key}")
            for bios in entry.get("biosFiles", []):
                # RetroBat states an unhashed file with an empty md5, so only
                # a missing path makes an entry unusable.
                if "md5" not in bios or not bios.get("file"):
                    issues.append(f"incomplete entry: {key}/{bios.get('file', '?')}")

        for system, files in self.exportable(systems, require="md5"):
            entry = data.get(system.native_id)
            if entry is None:
                issues.append(f"system absent: {system.native_id}")
                continue
            declared = {bios.get("file") for bios in entry.get("biosFiles", [])}
            for fe in files:
                path = str(fe.native("native_path", "")) or f"bios/{fe.destination}"
                if path not in declared:
                    issues.append(f"absent: {system.native_id}/{fe.name}")
        return issues
