"""Exporter for Batocera's batocera-systems.

batocera-systems is an executable script: the systems dict is one block
inside it, the rest is the checker Batocera runs. Only the block is
rewritten, entry by entry, so the comments the maintainers wrote between
entries and the code around them survive untouched.
"""

from __future__ import annotations

import ast
import re

from .base_exporter import BaseExporter
from .baseline import NativeFile, NativeSystem, Report

SOURCE_URL = (
    "https://raw.githubusercontent.com/batocera-linux/batocera.linux/master"
    "/package/batocera/core/batocera-scripts/scripts/batocera-systems"
)

_ENTRY_START = re.compile(r'^(\s{4})"([^"]+)":\s*\{')
_INDENT = " " * 4


def _note_key(text: str) -> tuple[str, str]:
    """(file, zippedFile) of the item a line holds, ("", "") for none."""
    path = re.search(r'"file"\s*:\s*"([^"]+)"', text)
    member = re.search(r'"zippedFile"\s*:\s*"([^"]+)"', text)
    return (path.group(1) if path else "", member.group(1) if member else "")


class Exporter(BaseExporter):
    """Write Batocera's batocera-systems, corrected."""

    # checkBios compares one md5 for a plain file. altmd5 is read only for
    # a zippedFile: retrodream's boot.bin written with the first of four
    # md5 refused the three other dumps the core accepts.
    SINGLE_HASH_FIELD = "md5"

    # checkBios reads emulator names from es_systems, and Batocera's
    # standalone_cores are those names. ROCKNIX's list holds package names
    # (duckstation-sa), which its checker does not know.
    STANDALONE_RUNNERS = True

    def __init__(self) -> None:
        super().__init__()
        # The emulator and core each system entry of the original is gated
        # on, read by render.
        self._gates: dict[str, tuple[str, str] | None] = {}

    @staticmethod
    def platform_name() -> str:
        return "batocera"

    @staticmethod
    def native_filename() -> str:
        return "batocera-systems"

    @staticmethod
    def requires() -> str:
        return "md5"

    @staticmethod
    def keys_by_system() -> bool:
        return True

    @staticmethod
    def carries() -> frozenset[str]:
        return frozenset({"md5"})

    @classmethod
    def writable(cls, fe: NativeFile, require: str = "") -> bool:
        """An addition the format can state whole."""
        if fe.platform is None and not cls._usable_runners(fe):
            # Written without its core's emulator and core, checkBios asks
            # every build for it (the defect _runner_keys repairs for the
            # platform's own items).
            return False
        return super().writable(fe, require)

    @classmethod
    def _usable_runners(cls, fe: NativeFile) -> list[tuple[bool, str]]:
        return [r for r in fe.runners if cls.STANDALONE_RUNNERS or not r[0]]

    def _runner_pair(self, runner: tuple[bool, str]) -> tuple[str, str | None] | None:
        """The emulator and core checkBios finds a runner under, None when
        the checker has no name for it. Batocera's es_systems names a
        libretro core under `libretro` and a standalone by itself."""
        standalone, name = runner
        return (name, None) if standalone else ("libretro", name)

    def _addition_keys(self, fe: NativeFile) -> list[tuple[str, str | None]] | None:
        """The emulator/core pairs an addition is written with, [] when its
        system's own gate already names its core, None when the gate names
        another: checkBios walks a gated system only when that core is
        installed, so a file of another core there is asked of the wrong
        users and never of its own."""
        pairs = [
            pair for r in self._usable_runners(fe)
            if (pair := self._runner_pair(r)) is not None
        ]
        if not pairs:
            return None
        gate = self._gates.get(fe.native_system)
        if gate is None:
            return pairs
        return [] if gate in pairs else None

    def exportable(
        self,
        systems: dict[str, NativeSystem],
        require: str = "",
    ) -> list[tuple[NativeSystem, list[NativeFile]]]:
        """What the format can carry, less the additions its gates refuse:
        render and validate read the same list."""
        result = []
        for system, files in super().exportable(systems, require):
            kept = [fe for fe in files if self.lands(fe)]
            if kept:
                result.append((system, kept))
        return result

    def lands(self, fe: NativeFile) -> bool:
        if not self.writable(fe):
            return False
        return fe.platform is not None or self._addition_keys(fe) is not None

    def states(self, fe: NativeFile, field_name: str) -> bool:
        if field_name == "md5" and "md5" in fe.filled:
            return bool(self.single_value(fe, "md5"))
        return super().states(fe, field_name)

    @staticmethod
    def native_sources() -> dict[str, str]:
        return {"batocera-systems": SOURCE_URL}

    @staticmethod
    def needs_original() -> bool:
        # The data block is a fraction of the file, the rest is the checker.
        return True

    def _bios_files(self, files: list[NativeFile], before: dict | None = None) -> str:
        return ", ".join(self._bios_items(files, before))

    @staticmethod
    def _runner_keys(item: dict | None) -> list[str]:
        """The emulator and core checkBios reads through coreExists().

        Not in our model, so they travel from the maintainer's own entry: a
        rewritten item without them made the checker treat the BIOS as
        wanted by every build (vectrex, tvc, macintosh, bk).
        """
        return [
            f'"{key}": "{item[key]}"'
            for key in ("emulator", "core")
            if item and isinstance(item.get(key), str)
        ]

    def _bios_items(self, files: list[NativeFile], before: dict | None = None) -> list[str]:
        originals = {
            str(item.get("file", "")): item
            for item in (before or {}).get("biosFiles", [])
            if isinstance(item, dict)
        }
        parts: list[str] = []
        for fe in files:
            for runner in self._item_runners(fe):
                parts.append(self._bios_item(fe, runner, originals))
        return parts

    def _item_runners(self, fe: NativeFile) -> list[list[str] | None]:
        """One item per runner an addition names. None keeps the original's."""
        if fe.platform is not None:
            return [None]
        pairs = self._addition_keys(fe) or []
        if not pairs:
            return [[]]
        return [
            [f'"emulator": "{emulator}"'] + ([f'"core": "{core}"'] if core else [])
            for emulator, core in pairs
        ]

    def _bios_item(self, fe: NativeFile, runner: list[str] | None, originals: dict) -> str:
        # The platform states an unhashed file as an empty md5 rather
        # than leaving it out, and so do we.
        item = [f'"md5": "{self.single_value(fe, "md5")}"']
        alt = fe.native("alt_md5", "")
        if alt:
            item.append(f'"altmd5": "{alt}"')
        declared = fe.native("native_path", "")
        path = str(declared) if declared else f"bios/{fe.destination}"
        item.append(f'"file": "{path}"')
        zipped = fe.native("zipped_file", "")
        if zipped:
            item.append(f'"zippedFile": "{zipped}"')
        item.extend(self._runner_keys(originals.get(path)) if runner is None else runner)
        return "{ " + ", ".join(item) + " }"

    @staticmethod
    def _own_entry(system: NativeSystem, before: dict | None) -> dict:
        """The maintainer's entry for this system, out of the parsed pair."""
        inner = (before or {}).get(system.native_id)
        return inner if isinstance(inner, dict) else {}

    def _entry_line(
        self, system: NativeSystem, files: list[NativeFile], before: dict | None = None
    ) -> str:
        own = self._own_entry(system, before)
        runner = "".join(f"{key}, " for key in self._runner_keys(own))
        return (
            f'{_INDENT}"{system.native_id}": '
            f'{{ "name": "{self.display_name(system)}", {runner}'
            f'"biosFiles": [ {self._bios_files(files, own)} ] }},'
        )

    @staticmethod
    def _item_notes(
        lines: list[str],
    ) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], list[str]]]:
        """The notes a maintainer wrote about each item, keyed by its file
        and the member it names inside an archive.

        Rewriting an entry as one line would drop them, and a note like
        "ideally - 94bc50..." is the only record of why a hash is blank.
        A comment on its own line belongs to the item below it, a comment at
        the end of a line belongs to the item on it, and a comment after the
        list's opening bracket (`"biosFiles": [# ep128emu-core`) to the first
        item. Keyed by path alone, bk's '# MAME' came back before each of
        twelve items reading bios/bk0010.zip.
        """
        trailing: dict[tuple[str, str], str] = {}
        leading: dict[tuple[str, str], list[str]] = {}
        pending: list[str] = []
        for line in lines:
            stripped = line.strip()
            hash_at = line.find("#")
            if stripped.startswith("#"):
                pending.append(stripped)
                continue
            key = _note_key(line)
            if not key[0]:
                if 0 <= hash_at and "[" in line[:hash_at]:
                    pending.append(line[hash_at:].strip())
                continue
            if pending:
                leading[key] = pending
                pending = []
            if 0 <= hash_at and hash_at > line.index(key[0]):
                trailing[key] = line[hash_at:].rstrip()
        return trailing, leading


    def _entry_block(
        self,
        system: NativeSystem,
        files: list[NativeFile],
        original: list[str],
    ) -> list[str]:
        """One entry, keeping the layout and the notes it was written with."""
        trailing, leading = self._item_notes(original)
        before = self._parse_entry(original)
        own = self._own_entry(system, before)
        items = self._bios_items(files, own)
        one_line = len(original) == 1 and not trailing and not leading
        if one_line:
            return [self._entry_line(system, files, before)]

        runner = "".join(f"{key}, " for key in self._runner_keys(own))
        head = (
            f'{_INDENT}"{system.native_id}": '
            f'{{ "name": "{self.display_name(system)}", {runner}"biosFiles": ['
        )
        pad = " " * (len(_INDENT) + 4)
        lines = [head]
        for index, item in enumerate(items):
            key = _note_key(item)
            lines.extend(f"{pad}{note}" for note in leading.pop(key, []))
            separator = "," if index < len(items) - 1 else ""
            note = trailing.get(key, "")
            suffix = f" {note}" if note else ""
            lines.append(f"{pad}{item}{separator}{suffix}")
        lines.append(f"{_INDENT}] }},")
        return lines

    @staticmethod
    def _split_original(original: str) -> tuple[list[str], list[str], list[str]]:
        """Cut the script into what precedes the dict, the dict, what follows."""
        lines = original.split("\n")
        start = next(
            (i for i, line in enumerate(lines) if line.startswith("systems = {")),
            None,
        )
        if start is None:
            raise ValueError("batocera-systems: no systems dict found")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("}")),
            None,
        )
        if end is None:
            raise ValueError("batocera-systems: the systems dict is not closed")
        return lines[: start + 1], lines[start + 1 : end], lines[end:]

    @staticmethod
    def _entry_spans(body: list[str]) -> dict[str, tuple[int, int]]:
        """Locate each top-level entry, which may wrap over several lines."""
        spans: dict[str, tuple[int, int]] = {}
        index = 0
        while index < len(body):
            match = _ENTRY_START.match(body[index])
            if not match:
                index += 1
                continue
            key = match.group(2)
            depth = 0
            end = index
            for cursor in range(index, len(body)):
                depth += body[cursor].count("{") + body[cursor].count("[")
                depth -= body[cursor].count("}") + body[cursor].count("]")
                if depth <= 0:
                    end = cursor
                    break
            else:
                end = len(body) - 1
            spans[key] = (index, end)
            index = end + 1
        return spans

    @staticmethod
    def _parse_entry(lines: list[str]) -> dict | None:
        """Read one entry so two spellings of the same data compare equal.

        A literal, never executed: the text is the platform's file. The
        closing brace goes on a line of its own, since an entry may end in a
        comment (gba's '] }, # Optional') that swallowed it, and every such
        entry was rewritten with its emulator gate lost.
        """
        text = "\n".join(lines).strip()
        try:
            entry = ast.literal_eval("{" + text + "\n}")
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
            return None
        return entry if isinstance(entry, dict) else None

    def render(
        self,
        systems: dict[str, NativeSystem],
        report: Report,
        originals: dict[str, str],
        scraped: dict | None = None,
    ) -> dict[str, str]:
        original = originals.get(self.native_filename(), "")
        if not original:
            raise ValueError(
                f"{self.native_filename()} cannot be written without the "
                "platform's own file: the systems dict is a fraction of a "
                "script, and the rest of it is the checker"
            )

        head, body, tail = self._split_original(original)
        spans = self._entry_spans(body)
        for key, (start, end) in spans.items():
            parsed = self._parse_entry(body[start:end + 1]) or {}
            own = parsed.get(key) if isinstance(parsed.get(key), dict) else {}
            emulator, core = own.get("emulator"), own.get("core")
            self._gates[key] = (
                (emulator, core if isinstance(core, str) else None)
                if isinstance(emulator, str) else None
            )
        exportable = {
            system.native_id: (system, files)
            for system, files in self.exportable(systems, require="md5")
        }

        rebuilt: list[str] = []
        written: set[str] = set()
        index = 0
        for key, (start, end) in sorted(spans.items(), key=lambda kv: kv[1][0]):
            rebuilt.extend(body[index:start])
            pair = exportable.get(key)
            index = end + 1
            if pair is None:
                # The truth has nothing to say and no file left to declare.
                rebuilt.extend(body[start:end + 1])
                continue
            written.add(key)
            original_lines = body[start:end + 1]
            before = self._parse_entry(original_lines)
            replacement = self._entry_line(*pair, before)
            after = self._parse_entry([replacement])
            if before is not None and before == after:
                # Nothing changed: keep the maintainer's own lines, comments
                # and alignment included, so the diff shows only corrections.
                rebuilt.extend(original_lines)
            else:
                rebuilt.extend(self._entry_block(*pair, original_lines))
        rebuilt.extend(body[index:])

        added = [
            self._entry_line(system, files)
            for native_id, (system, files) in exportable.items()
            if native_id not in written
        ]
        if added:
            while rebuilt and not rebuilt[-1].strip():
                rebuilt.pop()
            rebuilt.append("")
            rebuilt.extend(sorted(added))
            rebuilt.append("")

        return {self.native_filename(): "\n".join([*head, *rebuilt, *tail])}

    def validate(
        self,
        systems: dict[str, NativeSystem],
        produced: dict[str, str],
    ) -> list[str]:
        content = produced[self.native_filename()]
        issues: list[str] = []

        block = content.split("\nsystems = {", 1)
        if len(block) != 2:
            return ["no systems dict in the output"]
        end = block[1].find("\n}")
        if end < 0:
            return ["the systems dict is not closed"]
        try:
            exported = ast.literal_eval("{" + block[1][:end] + "\n}")
        except (SyntaxError, ValueError) as exc:
            return [f"the systems dict does not parse: {exc}"]

        if not isinstance(exported, dict):
            return ["the systems dict did not evaluate to a dict"]

        for system, files in self.exportable(systems, require="md5"):
            entry = exported.get(system.native_id)
            if entry is None:
                issues.append(f"system absent: {system.native_id}")
                continue
            declared = {bios.get("file", "") for bios in entry.get("biosFiles", [])}
            for fe in files:
                path = str(fe.native("native_path", "")) or f"bios/{fe.destination}"
                if path not in declared:
                    issues.append(f"absent: {system.native_id}/{fe.name}")

        for native_id, entry in exported.items():
            if not entry.get("biosFiles"):
                issues.append(f"empty entry: {native_id}")

        if "def checkBios(" not in content:
            issues.append("the checker the script exists for is missing")
        return issues
