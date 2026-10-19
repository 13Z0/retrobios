"""Exporter for RomM's known_bios_files.json.

Keys are "<igdb slug>:<filename>". RomM verifies a firmware file with
`file_size_bytes == int(entry.get("size", 0))` and then one hash among md5,
sha1 and crc, so an entry without a size can never match and an entry
without a hash can never match either. Neither is written.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.romm_scraper import SLUG_MAP

from .base_exporter import BaseExporter
from .baseline import NativeFile, NativeSystem, Report, _hash_values

SOURCE_URL = (
    "https://raw.githubusercontent.com/rommapp/romm/master/backend/models"
    "/fixtures/known_bios_files.json"
)


class Exporter(BaseExporter):
    """Write RomM's known_bios_files.json, corrected."""

    def __init__(self) -> None:
        super().__init__()
        # (system, filename) keys the platform itself declares, and the
        # addition that claims each other key first: both set by render.
        self._platform_keys: set[tuple[str, str]] = set()
        self._first_addition: dict[tuple[str, str], int] = {}

    @staticmethod
    def platform_name() -> str:
        return "romm"

    @staticmethod
    def native_filename() -> str:
        return "known_bios_files.json"

    @staticmethod
    def carries() -> frozenset[str]:
        return frozenset({"size", "crc32", "md5", "sha1"})

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"known_bios_files.json": SOURCE_URL}

    @staticmethod
    def _verifiable(fe: NativeFile) -> bool:
        return bool(fe.size()) and any(
            fe.hash(h) for h in ("md5", "sha1", "crc32")
        )

    @staticmethod
    def _known_platform(native_id: str) -> bool:
        """RomM keys by IGDB platform slug, and only looks up its own.

        A key spelled with one of our slugs (capcom-cps3, snk-neogeo-mvs)
        matches nothing on their side, so it is reported rather than
        written.
        """
        return native_id in SLUG_MAP

    def writable(self, fe: NativeFile, require: str = "") -> bool:  # noqa: ARG002
        """What RomM already ships stays. The conditions gate additions.

        An entry of theirs that could never verify is still theirs, and the
        round trip is not the place to decide otherwise. An addition whose
        slug:filename key the platform already holds cannot be written: the
        fixture is a dict, and the second write replaced RomM's Dreamcast
        boot.bin with fbneo's 480-byte file of the same name.
        """
        if fe.platform is not None:
            return True
        key = (fe.native_system, fe.name)
        if key in self._platform_keys:
            return False
        if not (self._verifiable(fe) and self._known_platform(fe.native_system)):
            return False
        # Two additions under one key: the second would overwrite the first
        # in the dict and both were counted landed. The first one in render
        # order is written, the other is not.
        return self._first_addition.get(key, id(fe)) == id(fe)

    def render(
        self,
        systems: dict[str, NativeSystem],
        report: Report,
        originals: dict[str, str],
        scraped: dict | None = None,
    ) -> dict[str, str]:
        # RomM's own fixture is the base: its key order stays, and a key our
        # transcription lacks (added after the scrape, or filtered from the
        # model) is kept. Rebuilt from the model in our order, three hash
        # corrections came out as a diff of the whole file.
        original = originals.get(self.native_filename(), "")
        output: OrderedDict[str, OrderedDict] = (
            json.loads(original, object_pairs_hook=OrderedDict)
            if original else OrderedDict()
        )
        self._platform_keys = {
            (system.native_id, fe.name)
            for system in systems.values()
            for fe in system.files
            if fe.platform is not None
        }
        self._first_addition = {}
        self.normalized: list[str] = []
        for system in sorted(systems.values(), key=lambda s: s.native_id):
            for fe in sorted(system.files, key=lambda f: f.name):
                key = (fe.native_system, fe.name)
                if (
                    fe.platform is None
                    and key not in self._platform_keys
                    and self._verifiable(fe)
                    and self._known_platform(fe.native_system)
                ):
                    self._first_addition.setdefault(key, id(fe))

        for system in sorted(systems.values(), key=lambda s: s.native_id):
            for fe in sorted(system.files, key=lambda f: f.name):
                if not self.writable(fe):
                    continue
                key = f"{system.native_id}:{fe.name}"
                # Their entry keeps its fields and their order, ours fill in.
                entry: OrderedDict[str, str] = OrderedDict(output.get(key, {}))
                # The fixture states every value as a string, size included.
                # Where the model settles no size, RomM's own value stands.
                # str(None) wrote "None", and RomM's int() on it raised.
                size = fe.size()
                if size is None and fe.platform is not None:
                    size = fe.platform.get("size")
                if size is not None:
                    self._write(entry, "size", str(int(size)), fe, "size")
                for field, hash_name in (("crc", "crc32"), ("md5", "md5"), ("sha1", "sha1")):
                    value = fe.record_hash(hash_name)
                    if value:
                        self._write(entry, field, value, fe, hash_name)
                    elif self._contradicted(fe, hash_name):
                        # RomM accepts any one hash: a value the truth
                        # rejects, left beside the corrected md5, still
                        # passes the dump it describes.
                        entry.pop(field, None)
                output[key] = entry

        text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        return {self.native_filename(): text}

    @staticmethod
    def _contradicted(fe: NativeFile, name: str) -> bool:
        """A hash the platform states and the truth, taking the entry over,
        does not keep."""
        return bool(_hash_values(fe.platform or {}, name)) and not fe.hashes(name)

    def fills(self, fe: NativeFile, field_name: str) -> bool:
        return self.states(fe, field_name) and self.record_fills(fe, field_name)

    def _write(
        self, entry: dict, field: str, value: str, fe: NativeFile, name: str
    ) -> None:
        """Set one field, noting a value the model reads as RomM's own.

        RomM compares strings as stored: PLUTONIA.WAD's sha1 starts with a
        space and never matches. The model normalises it away, so no
        correction names it, yet the written file changes it.
        """
        theirs = entry.get(field)
        if (
            theirs is not None
            and str(theirs) != value
            and str(theirs).strip().lower() == value
            and name not in fe.corrections
        ):
            self.normalized.append(f"{fe.native_system}/{fe.name} {name}")
        entry[field] = value

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
            slug = key.split(":", 1)[0] if ":" in key else ""
            if not slug:
                issues.append(f"key without a platform slug: {key}")
            elif not self._known_platform(slug):
                issues.append(f"platform slug RomM does not know: {slug}")
            if not str(entry.get("size", "")).isdigit():
                issues.append(f"entry without a size, never verifiable: {key}")
            if not any(entry.get(h) for h in ("md5", "sha1", "crc")):
                issues.append(f"entry without a hash, never verifiable: {key}")

        for system in systems.values():
            for fe in system.files:
                if not self.writable(fe):
                    continue
                if f"{system.native_id}:{fe.name}" not in data:
                    issues.append(f"absent: {system.native_id}/{fe.name}")
        return issues
