"""RetroBat's launcher layer, read from emulatorLauncher's C# generators.

emulatorLauncher decides, per emulator, which file names it looks for in the
BIOS directory, in what order, and what it writes into the emulator's
configuration. The C# is read as text, never compiled: each extractor targets
one construct the generators use and raises LauncherPatternMissing when that
construct is gone, so an upstream rewrite stops the scrape instead of
publishing an empty list.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterator

GENERATORS = "emulatorLauncher/Generators"
FEATURES = ".emulationstation/es_features.cfg"

SOURCE_FILES = (
    f"{GENERATORS}/Amiberry.Generator.cs",
    f"{GENERATORS}/Ares.Generator.cs",
    f"{GENERATORS}/Bizhawk.Generator.cs",
    f"{GENERATORS}/Duckstation.Generator.cs",
    f"{GENERATORS}/Flycast.Generator.cs",
    f"{GENERATORS}/Hatari.Generator.cs",
    f"{GENERATORS}/Jgenesis.Generator.cs",
    f"{GENERATORS}/KegaFusion.Generator.cs",
    f"{GENERATORS}/LibRetro.CoreOptions.cs",
    f"{GENERATORS}/Mednafen.Generator.cs",
    f"{GENERATORS}/MelonDS.Generator.cs",
    f"{GENERATORS}/Mesen.Generator.cs",
    f"{GENERATORS}/Mupen64.Generator.cs",
    f"{GENERATORS}/OpenMSX.Generator.cs",
    f"{GENERATORS}/Pcsx2.Generator.cs",
    f"{GENERATORS}/Project64.Generator.cs",
    f"{GENERATORS}/Uae.Generator.cs",
    f"{GENERATORS}/Xemu.Generator.cs",
    f"{GENERATORS}/mGBA.Generator.cs",
    FEATURES,
)

BIOS = r'AppConfig\.GetFullPath\("bios"\)'
STRING = r'"((?:[^"\\\n]|\\.)*)"'

Record = dict[str, object]


class LauncherPatternMissing(ValueError):
    """A construct an extractor reads is absent from the launcher source."""

    def __init__(self, path: str, what: str) -> None:
        super().__init__(f"{path}: {what} not found")


class Source:
    """One source file with line lookup and C#-aware brace matching."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self._starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        self._blocks: list[tuple[int, int]] | None = None
        self._ends: list[int] | None = None

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self._starts, offset)

    def lines(self, start: int, end: int) -> tuple[int, int]:
        return self.line(start), self.line(max(start, end - 1))

    def find(
        self, pattern: str, what: str, start: int = 0, end: int | None = None, flags: int = 0
    ) -> re.Match[str]:
        found = re.compile(pattern, flags).search(
            self.text, start, len(self.text) if end is None else end
        )
        if found is None:
            raise LauncherPatternMissing(self.path, what)
        return found

    def find_all(
        self, pattern: str, what: str, start: int = 0, end: int | None = None
    ) -> list[re.Match[str]]:
        found = list(
            re.compile(pattern).finditer(self.text, start, len(self.text) if end is None else end)
        )
        if not found:
            raise LauncherPatternMissing(self.path, what)
        return found

    def blocks(self) -> list[tuple[int, int]]:
        """Every brace pair, as (offset of "{", offset after "}")."""
        if self._blocks is None:
            self._blocks = []
            stack: list[int] = []
            for offset, char in _code_chars(self.text):
                if char == "{":
                    stack.append(offset)
                elif char == "}" and stack:
                    self._blocks.append((stack.pop(), offset + 1))
        return self._blocks

    def block_at(self, open_offset: int) -> tuple[int, int]:
        """The block opened by the first "{" at or after open_offset."""
        candidates = [b for b in self.blocks() if b[0] >= open_offset]
        if not candidates:
            raise LauncherPatternMissing(self.path, f"block after line {self.line(open_offset)}")
        return min(candidates)

    def enclosing(self, offset: int) -> tuple[int, int]:
        """The innermost block holding offset."""
        holding = [b for b in self.blocks() if b[0] < offset < b[1]]
        if not holding:
            raise LauncherPatternMissing(self.path, f"block around line {self.line(offset)}")
        return max(holding)

    def header(self, block: tuple[int, int]) -> str:
        """The statement that owns a block's opening brace, comments removed."""
        if self._ends is None:
            self._ends = [o for o, c in _code_chars(self.text) if c in ";{}"]
        previous = bisect.bisect_left(self._ends, block[0]) - 1
        start = self._ends[previous] + 1 if previous >= 0 else 0
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", self.text[start : block[0]], flags=re.S)
        return " ".join(text.split())

    def statement_span(self, block: tuple[int, int]) -> tuple[int, int]:
        """Lines of a block, from the statement that owns its opening brace."""
        first, last = self.lines(*block)
        line_text = self.text[self._starts[first - 1] : block[0]].strip()
        return (first - 1 if not line_text and first > 1 else first), last


def _code_chars(text: str) -> Iterator[tuple[int, str]]:
    """Characters of C# code outside string, char literals and comments."""
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if text.startswith('@"', i):
            i += 2
            while i < n and not (text[i] == '"' and not text.startswith('""', i)):
                i += 2 if text.startswith('""', i) else 1
            i += 1
            continue
        if char in "\"'":
            i += 1
            while i < n and text[i] != char and text[i] != "\n":
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        yield i, char
        i += 1


def _unescape(literal: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), literal)


def _strings(text: str) -> list[str]:
    return [_unescape(m.group(1)) for m in re.finditer(STRING, text)]


def _unique(names: list[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _span(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans sorted, joined when only one line separates them."""
    merged: list[tuple[int, int]] = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 2:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return merged


def source_ref(*parts: tuple[str, list[tuple[int, int]]]) -> str:
    """path:a-b, c-d for each file, a later range of one file left bare."""
    refs = []
    for path, spans in parts:
        ranges = [_span(*s) for s in _merge(spans)]
        refs.append(f"{path}:{', '.join(ranges)}")
    return ", ".join(refs)


def _array_at(src: Source, offset: int) -> tuple[list[str], tuple[int, int]]:
    """String literals of the array initializer opened at offset."""
    block = src.block_at(offset)
    return _strings(src.text[block[0] : block[1]]), src.lines(*block)


def _call_args(src: Source, open_paren: int) -> str:
    depth = 0
    for offset, char in _code_chars(src.text[open_paren:]):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return src.text[open_paren + 1 : open_paren + offset]
    raise LauncherPatternMissing(src.path, f"closing parenthesis after line {src.line(open_paren)}")


def render_concat(expression: str) -> str:
    """A C# string concatenation as text, each non-literal operand as <operand>."""
    parts, depth, current, in_string = [], 0, "", False
    i = 0
    while i < len(expression):
        char = expression[i]
        if in_string:
            current += char
            if char == "\\":
                current += expression[i + 1]
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            current += char
        elif char in "([":
            depth += 1
            current += char
        elif char in ")]":
            depth -= 1
            current += char
        elif char == "+" and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
        i += 1
    parts.append(current.strip())
    rendered = []
    for part in parts:
        literal = re.fullmatch(STRING, part)
        rendered.append(_unescape(literal.group(1)) if literal else f"<{part}>")
    return "".join(rendered)


def _throw_message(src: Source, start: int, end: int) -> tuple[str, int]:
    found = src.find(r"throw new ApplicationException\(", "ApplicationException", start, end)
    message = render_concat(_call_args(src, found.end() - 1))
    return f"ApplicationException '{message}'", src.line(found.start())


def feature_choices(
    src: Source, emulator: str, option: str, core: str | None = None
) -> tuple[list[str], tuple[int, int]]:
    """Values es_features.cfg offers for an option of one emulator or core."""
    text = src.text
    scope_start, scope_end = 0, len(text)
    for found in re.finditer(r'<emulator name="([^"]*)"', text):
        if emulator in [name.strip() for name in found.group(1).split(",")]:
            scope_start = found.start()
            scope_end = text.find("</emulator>", scope_start)
            break
    else:
        raise LauncherPatternMissing(src.path, f"emulator {emulator}")
    if core is not None:
        found = src.find(
            rf'<core name="{re.escape(core)}"', f"core {core} of {emulator}", scope_start, scope_end
        )
        scope_start, scope_end = found.start(), text.find("</core>", found.start())
    feature = src.find(
        rf'<feature [^>]*value="{re.escape(option)}"',
        f"feature {option} of {emulator}",
        scope_start,
        scope_end,
    )
    close = text.find("</feature>", feature.end())
    values = re.findall(r'<choice [^>]*value="([^"]*)"', text[feature.end() : close])
    if not values:
        raise LauncherPatternMissing(src.path, f"choices of {option}")
    return values, src.lines(feature.start(), close + len("</feature>"))


def _record(
    emulator: str, build: str, kind: str, names: list[str], ref: str, **fields: object
) -> Record:
    record: Record = {"emulator": emulator, "build": build, "kind": kind, "names": names}
    record.update({key: value for key, value in fields.items() if value is not None})
    record["source_ref"] = ref
    return record


# Each extractor takes the sources by path and returns its records.


def ares(sources: dict[str, Source]) -> list[Record]:
    """Fallback chains: x = Path.Combine(dir, a), then if (!File.Exists(x)) x = b."""
    src = sources[f"{GENERATORS}/Ares.Generator.cs"]
    records = []
    head = r'string (\w+) = Path\.Combine\((\w+), "([^"]+)"\);'
    for found in src.find_all(head, "Path.Combine fallback chain"):
        var, folder = found.group(1), found.group(2)
        names, end = [found.group(3)], found.end()
        step = re.compile(
            rf'\s*if \(!File\.Exists\({var}\)\)\s*{var} = Path\.Combine\({folder}, "([^"]+)"\);'
        )
        while more := step.match(src.text, end):
            names.append(more.group(1))
            end = more.end()
        if len(names) == 1:
            continue
        block = src.enclosing(found.start())
        target = src.find(
            rf'firmware\["([^"]+)"\] = {var}\.', f"firmware key for {var}", end, block[1]
        )
        container = list(
            re.compile(r'GetOrCreateContainer\("(\w+)"\)').finditer(
                src.text, block[0], found.start()
            )
        )
        folder_decl = src.find(
            rf'string {folder} = Path\.Combine\({BIOS}, "([^"]+)"\);',
            f"folder {folder}",
            block[0],
            found.start(),
        )
        sections = "/".join(m.group(1) for m in container)
        records.append(
            _record(
                "ares",
                "standalone",
                "names_searched",
                names,
                source_ref((src.path, [(src.line(found.start()), src.line(target.end()))])),
                case_insensitive=True,
                search_dir=f"bios/{folder_decl.group(1)}",
                passes_to=f"settings.bml {sections}/{target.group(1)} (first found)",
                fails_when_missing=False,
            )
        )
    if not records:
        raise LauncherPatternMissing(src.path, "File.Exists fallback chain")
    return records


def hatari(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Hatari.Generator.cs"]
    features = sources[FEATURES]
    default = src.find(rf'string (\w+) = Path\.Combine\({BIOS}, "([^"]+)"\);', "default TOS path")
    var, name = default.group(1), default.group(2)
    override = src.find(
        rf'isOptSet\("(\w+)"\)[^\n]*\n\s*{var} = Path\.Combine\({BIOS}, ((?:"[^"]+", )+)SystemConfig\["\1"\]\);',
        "TOS option override",
        default.end(),
    )
    option, folders = override.group(1), _strings(override.group(2))
    write = src.find(
        rf'ini\.WriteValue\("(\w+)", "(\w+)", {var}\);', "TOS ini write", override.end()
    )
    message, _ = _throw_message(src, override.end(), write.start())
    passes = f"hatari.cfg [{write.group(1)}] {write.group(2)}"
    choices, choice_lines = feature_choices(features, "hatari", option)
    return [
        _record(
            "hatari",
            "standalone",
            "fixed_name",
            [name],
            source_ref((src.path, [src.lines(default.start(), write.end())])),
            case_insensitive=True,
            search_dir="bios",
            passes_to=f"{passes}=<bios>/{name}",
            fails_when_missing=message,
            depends_on_option=f"{option} unset",
        ),
        _record(
            "hatari",
            "standalone",
            "core_option",
            choices,
            source_ref(
                (src.path, [src.lines(override.start(), write.end())]),
                (features.path, [choice_lines]),
            ),
            case_insensitive=True,
            search_dir="/".join(["bios", *folders]),
            passes_to=f"{passes}=<bios>/{'/'.join(folders)}/<value>",
            fails_when_missing=message,
            depends_on_option=option,
        ),
    ]


def hatari_libretro(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/LibRetro.CoreOptions.cs"]
    features = sources[FEATURES]
    records = []

    tosimage = src.find(
        r'if \(SystemConfig\.isOptSet\("(\w+)"\)[^\n]*\n\s*coreSettings\["(hatari_tosimage)"\] = SystemConfig\["\1"\];'
        r'\s*else\s*coreSettings\["\2"\] = "(\w+)";',
        "hatari_tosimage",
    )
    choices, choice_lines = feature_choices(features, "libretro", tosimage.group(1), core="hatari")
    records.append(
        _record(
            "hatari",
            "libretro",
            "core_option",
            choices,
            source_ref(
                (src.path, [src.lines(tosimage.start(), tosimage.end())]),
                (features.path, [choice_lines]),
            ),
            search_dir="bios",
            passes_to=f"{tosimage.group(2)} = <value> | {tosimage.group(3)}",
            fails_when_missing="not checked by the launcher",
            depends_on_option=tosimage.group(1),
        )
    )

    guard = src.find(r'if \(core != "hatarib"\)', "hatarib guard")
    method = src.enclosing(guard.start())
    found = src.find(
        rf'else if \(File\.Exists\(Path\.Combine\({BIOS}, "([^"]+)"\)\)\)\s*'
        r'coreSettings\["(hatarib_tos)"\] = "([^"]+)";\s*else\s*coreSettings\["hatarib_tos"\] = "([^"]+)";',
        "hatarib tos.img fallback",
        method[0],
        method[1],
    )
    records.append(
        _record(
            "hatarib",
            "libretro",
            "fixed_name",
            [found.group(1)],
            source_ref((src.path, [src.lines(found.start(), found.end())])),
            case_insensitive=True,
            search_dir="bios",
            passes_to=f"{found.group(2)} = {found.group(3)}",
            fails_when_missing=f"falls back to {found.group(2)} = {found.group(4)}",
            depends_on_option=f"{found.group(2)} unset",
        )
    )

    picked = src.find(
        rf'string (\w+) = Path\.Combine\({BIOS}, "([^"]+)", SystemConfig\["(hatarib_tos)"\]\);',
        "hatarib option path",
        method[0],
        method[1],
    )
    write = src.find(
        r'coreSettings\["hatarib_tos"\] = (\w+);', "hatarib option write", picked.end()
    )
    skipped = re.findall(
        r'SystemConfig\["hatarib_tos"\] == "(\w+)"', src.text[method[0] : picked.start()]
    )
    prefixes = re.findall(
        r'SystemConfig\["hatarib_tos"\]\.StartsWith\("(\w+)"\)',
        src.text[method[0] : picked.start()],
    )
    choices, choice_lines = feature_choices(features, "libretro", picked.group(3), core="hatarib")
    files = [c for c in choices if c not in skipped and not c.startswith(tuple(prefixes))]
    records.append(
        _record(
            "hatarib",
            "libretro",
            "core_option",
            files,
            source_ref(
                (src.path, [src.lines(picked.start(), write.end())]),
                (features.path, [choice_lines]),
            ),
            case_insensitive=True,
            search_dir=f"bios/{picked.group(2)}",
            passes_to=f"hatarib_tos = {picked.group(2)}/<value>",
            fails_when_missing="option not written when the file is absent",
            depends_on_option=picked.group(3),
        )
    )
    return records


def mesen(sources: dict[str, Source]) -> list[Record]:
    """Files copied from bios into Mesen's Firmware folder, renamed on the way."""
    src = sources[f"{GENERATORS}/Mesen.Generator.cs"]
    records = []
    for folder in src.find_all(
        r'string targetFirmwarePath = Path\.Combine\(\w+, "Firmware"\);', "Firmware folder"
    ):
        block = src.enclosing(folder.start())
        body = src.text[block[0] : block[1]]
        targets = dict(
            re.findall(
                r'string (targetFirmware\d+) = Path\.Combine\(targetFirmwarePath, "([^"]+)"\);',
                body,
            )
        )
        pairs = re.findall(
            rf'string (sourceFirmware\d+) = Path\.Combine\({BIOS}, "([^"]+)"\);', body
        )
        copies = re.findall(
            r"!File\.Exists\((targetFirmware\d+)\)[^\n]*\n\s*File\.Copy\((sourceFirmware\d+), \1\);",
            body,
        )
        if not pairs or len(copies) != len(pairs):
            raise LauncherPatternMissing(
                src.path, f"guarded File.Copy at line {src.line(folder.start())}"
            )
        by_source = {source: target for target, source in copies}
        names = [name for _, name in pairs]
        destinations = [targets[by_source[var]] for var, _ in pairs]
        try:
            fails: str | bool = _throw_message(src, folder.start(), block[1])[0]
        except LauncherPatternMissing:
            fails = False
        records.append(
            _record(
                "mesen",
                "standalone",
                "copies",
                names,
                source_ref((src.path, [src.statement_span(block)])),
                case_insensitive=True,
                search_dir="bios",
                passes_to="copied to <mesen>/Firmware/"
                + ", ".join(destinations)
                + ", only when the target does not exist",
                fails_when_missing=fails,
            )
        )
    return records


def _kickstart_table(
    src: Source,
    entry: str,
    find_method: str,
) -> tuple[list[tuple[str, list[str], list[tuple[int, int]]]], str, tuple[int, int]]:
    """Kickstart candidate lists of an Amiga model table, identical lists merged."""
    lists: dict[tuple[str, tuple[str, ...]], list[tuple[int, int]]] = {}
    for model in src.find_all(rf'\{{\s*"[\w+]+",\s*new {entry}\s*\{{', f"{entry} table"):
        block = src.block_at(model.start())
        for field in re.finditer(
            r"\b(Kickstart|KickstartExt)\s*=\s*new\s*(?:string)?\[\]\s*\{",
            src.text[block[0] : block[1]],
        ):
            names, (_, last) = _array_at(src, block[0] + field.end() - 1)
            lists.setdefault((field.group(1), tuple(names)), []).append(
                (src.line(model.start()), last)
            )
    method = src.find(find_method, "Kickstart lookup method")
    body = src.block_at(method.end())
    dirs = src.find(
        r'new\s*(?:List<string>\s*)?(?:\[\])?\s*\{\s*biosPath((?:, Path\.Combine\(biosPath, "[^"]+"\))+)\s*\}',
        "Kickstart search directories",
        body[0],
        body[1],
    )
    search = ", ".join(["bios", *(f"bios/{d}" for d in _strings(dirs.group(1)))])
    method_span = (src.line(method.start()), src.lines(*body)[1])
    return (
        [(field, list(names), spans) for (field, names), spans in lists.items()],
        search,
        method_span,
    )


def amiberry(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Amiberry.Generator.cs"]
    lists, search, method_span = _kickstart_table(
        src, "AmigaModel", r"private string FindBios\(string\[\] candidates\)"
    )
    warnings = {
        "Kickstart": src.find(
            r'Warning\("\[Amiberry\] No Kickstart ROM found', "Kickstart warning"
        ),
        "KickstartExt": src.find(
            r'Warning\("\[Amiberry\] No extended ROM found', "extended ROM warning"
        ),
    }
    keys = {"Kickstart": "kickstart_rom_file", "KickstartExt": "kickstart_ext_rom_file"}
    records = []
    for field, names, spans in lists:
        stage = src.find(rf"StageKickstart\(_model\.{field}\)", f"StageKickstart({field})")
        records.append(
            _record(
                "amiberry",
                "standalone",
                "names_searched",
                names,
                source_ref(
                    (
                        src.path,
                        [*spans, src.lines(stage.start(), warnings[field].end()), method_span],
                    )
                ),
                case_insensitive=True,
                search_dir=search,
                passes_to=f"copied to <amiberry roms>/{names[0]}, {keys[field]}",
                fails_when_missing=f"warning, {keys[field]} not written",
            )
        )
    return records


def winuae(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Uae.Generator.cs"]
    lists, search, method_span = _kickstart_table(
        src, "Candidate", r"public static string Find\(string\[\] candidates\)"
    )
    writer = src.find(r"private void WriteKickstartRom\(", "WriteKickstartRom")
    body = src.block_at(writer.end())
    keys = {"Kickstart": "kickstart_rom_file", "KickstartExt": "kickstart_ext_rom_file"}
    records = []
    for field, names, spans in lists:
        lookup = src.find(
            rf"AmigaBios\.Find\(candidate\.{field}\)", f"Find({field})", body[0], body[1]
        )
        message, throw_line = _throw_message(src, lookup.end(), body[1])
        records.append(
            _record(
                "winuae",
                "standalone",
                "names_searched",
                names,
                source_ref(
                    (src.path, [*spans, (src.line(lookup.start()), throw_line + 1), method_span])
                ),
                case_insensitive=True,
                search_dir=search,
                passes_to=f"game .uae {keys[field]}=<found path>",
                fails_when_missing=message,
            )
        )
    table = src.find(
        r"WhdBootKickstartEntry\[\] _whdBootKickstarts = new\[\]", "WHDBoot Kickstart table"
    )
    table_block = src.block_at(table.end())
    targets = re.findall(
        r'new WhdBootKickstartEntry\("([^"]+)", AmigaBios\.BySystem\["[^"]+"\]\.Kickstart\)',
        src.text[table_block[0] : table_block[1]],
    )
    loop = src.find(r"foreach \(var entry in _whdBootKickstarts\)", "WHDBoot copy loop")
    loop_block = src.block_at(loop.end())
    folder = src.find(
        r'Path\.Combine\(whdBoot, ((?:"[^"]+"(?:, )?)+)\)',
        "WHDBoot Kickstarts folder",
        0,
        loop.start(),
    )
    records.append(
        _record(
            "winuae",
            "standalone",
            "copies",
            targets,
            source_ref(
                (
                    src.path,
                    [
                        (src.line(loop.start()), src.lines(*loop_block)[1]),
                        (src.line(table.start()), src.lines(*table_block)[1]),
                    ],
                )
            ),
            case_insensitive=True,
            search_dir=search,
            passes_to=(
                f"copied to <WHDBoot>/{'/'.join(_strings(folder.group(1)))}/<name>"
                " from the system's Kickstart candidates"
            ),
            fails_when_missing=False,
        )
    )
    return records


def pcsx2(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Pcsx2.Generator.cs"]
    records = []

    qt = src.find(r"private void SetupConfigurationQT\(", "SetupConfigurationQT")
    qt_body = src.block_at(qt.end())
    folder = src.find(
        rf'string biosPath = Path\.Combine\({BIOS}, ((?:"[^"]+"(?:, )?)+)\);',
        "Qt BIOS folder",
        qt_body[0],
        qt_body[1],
    )
    src.find(
        r"Directory\.GetFiles\(biosPath\)\.Length == 0\)[^\n]*\n\s*biosPath = " + BIOS,
        "empty folder fallback",
        folder.end(),
        qt_body[1],
    )
    write = src.find(
        r'ini\.WriteValue\("Filenames", "BIOS", biosFile\);',
        "Qt BIOS write",
        folder.end(),
        qt_body[1],
    )
    message, _ = _throw_message(src, folder.end(), write.start())
    src.find(r'SystemConfig\["pcsx2_forcebios"\]', "pcsx2_forcebios", folder.end(), write.start())
    sub = "/".join(_strings(folder.group(1)))
    for var in ("biosList", "arcadeBiosList"):
        decl = src.find(rf"var {var} = new string\[\] \{{", f"Qt {var}", qt_body[0], qt_body[1])
        names, lines = _array_at(src, decl.end() - 1)
        records.append(
            _record(
                "pcsx2",
                "standalone",
                "names_searched",
                names,
                source_ref(
                    (
                        src.path,
                        [
                            (src.line(decl.start()), lines[1]),
                            src.lines(folder.start(), write.end()),
                        ],
                    )
                ),
                case_insensitive=True,
                search_dir=f"bios/{sub} (bios when bios/{sub} holds no file)",
                passes_to="inis/PCSX2.ini [Folders] Bios, [Filenames] BIOS",
                fails_when_missing=message,
                depends_on_option="pcsx2_forcebios",
            )
        )

    legacy = src.find(r"private void SetupPaths\(", "SetupPaths")
    legacy_body = src.block_at(legacy.end())
    decl = src.find(
        r"var biosList = new string\[\] \{", "1.6 biosList", legacy_body[0], legacy_body[1]
    )
    names, lines = _array_at(src, decl.end() - 1)
    case = src.find(r'case "BIOS":', "1.6 BIOS case", legacy_body[0], legacy_body[1])
    default = src.find(
        r'value = "([^"]+)";\s*break;', "1.6 default BIOS", case.end(), legacy_body[1]
    )
    folder = src.find(
        r'File\.Exists\(Path\.Combine\(biosPath, ((?:"[^"]+", )+)b\)\)',
        "1.6 BIOS folder",
        legacy_body[0],
        legacy_body[1],
    )
    sub = "/".join(_strings(folder.group(1)))
    records.append(
        _record(
            "pcsx2",
            "standalone",
            "names_searched",
            names,
            source_ref(
                (
                    src.path,
                    [(src.line(decl.start()), lines[1]), src.lines(case.start(), default.end())],
                )
            ),
            case_insensitive=True,
            search_dir=f"bios/{sub} if any listed name is there, else bios",
            passes_to="inis/PCSX2_ui.ini [Folders] Bios, [Filenames] BIOS",
            fails_when_missing=f"no error: BIOS name defaults to {default.group(1)}",
            depends_on_option="pcsx2_forcebios",
        )
    )
    return records


def pcsx2_libretro(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/LibRetro.CoreOptions.cs"]
    features = sources[FEATURES]
    guard = src.find(r'if \(core != "pcsx2"\)', "pcsx2 guard")
    method = src.enclosing(guard.start())
    folder = src.find(
        r'string ps2BiosFolder = Path\.Combine\(biosRoot, ((?:"[^"]+"(?:, )?)+)\);',
        "LRPS2 folder",
        method[0],
        method[1],
    )
    option = src.find(r'isOptSet\("(lrps2_bios)"\)', "lrps2_bios", folder.end(), method[1])
    write = src.find(
        r'coreSettings\["(pcsx2_bios)"\] = \w+;', "pcsx2_bios write", option.end(), method[1]
    )
    message, _ = _throw_message(src, option.end(), write.start())
    choices, choice_lines = feature_choices(features, "libretro", option.group(1), core="pcsx2")
    sub = "/".join(_strings(folder.group(1)))
    records = [
        _record(
            "pcsx2",
            "libretro",
            "core_option",
            choices,
            source_ref(
                (src.path, [src.lines(folder.start(), write.end())]),
                (features.path, [choice_lines]),
            ),
            case_insensitive=True,
            search_dir=f"<system_directory>/{sub}",
            passes_to=f"{write.group(1)} = <value>",
            fails_when_missing=message,
            depends_on_option=option.group(1),
        )
    ]

    scan = src.find(
        r"private static string GetFirstPS2Bios\(string biosFolder\)", "GetFirstPS2Bios"
    )
    scan_body = src.block_at(scan.end())
    preferred = src.find(
        r'n\.Equals\("([^"]+)", StringComparison\.OrdinalIgnoreCase\)',
        "preferred PS2 BIOS",
        scan_body[0],
        scan_body[1],
    )
    sizes = src.find(
        r"MIN_BIOS_SIZE = (\d+) \* 1024 \* 1024;\s*const long MAX_BIOS_SIZE = (\d+) \* 1024 \* 1024;",
        "BIOS size bounds",
        scan_body[0],
        scan_body[1],
    )
    auto = src.find(
        r"string autoBios = GetFirstPS2Bios\(ps2BiosFolder\);",
        "GetFirstPS2Bios call",
        write.end(),
        method[1],
    )
    auto_write = src.find(
        r'coreSettings\["pcsx2_bios"\] = autoBios;', "autoBios write", auto.end(), method[1]
    )
    auto_message, _ = _throw_message(src, auto.end(), auto_write.start())
    records.append(
        _record(
            "pcsx2",
            "libretro",
            "scans_dir",
            [
                preferred.group(1),
                f"<first file {sizes.group(1)}-{sizes.group(2)} MiB, ordinal-ignore-case>",
            ],
            source_ref(
                (
                    src.path,
                    [
                        (src.line(scan.start()), src.lines(*scan_body)[1]),
                        src.lines(auto.start(), auto_write.end()),
                    ],
                )
            ),
            case_insensitive=True,
            search_dir=f"<system_directory>/{sub}",
            passes_to="pcsx2_bios = <chosen name>",
            fails_when_missing=auto_message,
            depends_on_option=f"{option.group(1)} unset",
        )
    )
    return records


PSX_CASCADES = {
    f"{GENERATORS}/Bizhawk.Generator.cs": ("bizhawk", "standalone"),
    f"{GENERATORS}/Mednafen.Generator.cs": ("mednafen", "standalone"),
    f"{GENERATORS}/LibRetro.CoreOptions.cs": (None, "libretro"),
}


def _top_level_else(src: Source, block: tuple[int, int]) -> int | None:
    """Offset of the last else of an if chain at the top of a block, not an else if."""
    depth, last = 0, None
    for offset, char in _code_chars(src.text[block[0] + 1 : block[1] - 1]):
        absolute = block[0] + 1 + offset
        if char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif (
            depth == 0
            and src.text.startswith("else", absolute)
            and not re.match(r"else\s+if\b", src.text[absolute:])
        ):
            last = absolute
    return last


def psx_cascades(sources: dict[str, Source]) -> list[Record]:
    """psxonpsp660.bin, then ps1_rom.bin, then one file per region."""
    records = []
    for path, (fixed_emulator, build) in PSX_CASCADES.items():
        src = sources[path]
        for cascade in src.find_all(
            r'if \(!SystemConfig\.getOptBoolean\("(\w+_original_bios)"\)\)', "PSX BIOS cascade"
        ):
            emulator = fixed_emulator
            if emulator is None:
                guards = re.findall(r'if \(core != "([^"]+)"\)', src.text[: cascade.start()])
                if not guards:
                    raise LauncherPatternMissing(path, "core guard before the PSX cascade")
                emulator = guards[-1]
            branch = src.block_at(cascade.end())
            after = re.compile(r"\s*else\s*(\{|[^;]*;)").match(src.text, branch[1])
            if after is None:
                raise LauncherPatternMissing(
                    path, f"else after the PSX cascade at line {src.line(cascade.start())}"
                )
            original_end = src.block_at(after.start(1))[1] if after.group(1) == "{" else after.end()
            final_else = _top_level_else(src, branch)
            if final_else is None:
                raise LauncherPatternMissing(
                    path, f"final else of the PSX cascade at line {src.line(cascade.start())}"
                )
            searched = _unique(
                re.findall(rf'Path\.Combine\({BIOS}, "([^"]+)"\)', src.text[branch[0] : final_else])
            )
            if not searched:
                raise LauncherPatternMissing(path, "searched PSX BIOS names")
            keys = _unique(re.findall(r'\w+\["([^"]+)"\]\s*=', src.text[branch[0] : branch[1]]))
            ref = source_ref((path, [src.lines(cascade.start(), original_end)]))
            records.append(
                _record(
                    emulator,
                    build,
                    "names_searched",
                    searched,
                    ref,
                    case_insensitive=True,
                    search_dir="bios",
                    passes_to=", ".join(keys),
                    fails_when_missing=False,
                    depends_on_option=f"{cascade.group(1)} off",
                )
            )
            fallback_text = src.text[final_else : branch[1]]
            fallback = _unique([n for n in _strings(fallback_text) if n.lower().endswith(".bin")])
            if fallback:
                checked = "File.Exists" in fallback_text
                records.append(
                    _record(
                        emulator,
                        build,
                        "fixed_name",
                        fallback,
                        ref,
                        case_insensitive=True,
                        search_dir="bios",
                        passes_to=", ".join(keys),
                        fails_when_missing=(
                            False if checked else "names written without an existence check"
                        ),
                    )
                )
    return records


def xemu(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Xemu.Generator.cs"]
    features = sources[FEATURES]
    records = []

    mcpx = src.find(
        rf'if \(File\.Exists\(Path\.Combine\({BIOS}, "([^"]+)"\)\)\)\s*(\w+) = Path\.Combine',
        "MCPX boot ROM",
    )
    bootrom = src.find(
        rf'ini\.WriteValue\("([\w.]+)", "(bootrom_path)", "\'" \+ {mcpx.group(2)}',
        "bootrom_path write",
    )
    records.append(
        _record(
            "xemu",
            "standalone",
            "fixed_name",
            [mcpx.group(1)],
            source_ref(
                (
                    src.path,
                    [
                        src.lines(mcpx.start(), mcpx.end()),
                        src.lines(bootrom.start(), bootrom.end()),
                    ],
                )
            ),
            case_insensitive=True,
            search_dir="bios",
            passes_to=f"xemu.toml [{bootrom.group(1)}] {bootrom.group(2)}, only if the file exists",
            fails_when_missing=f"{bootrom.group(2)} not written",
        )
    )

    flash = src.find(r'isOptSet\("(xemu_flashrom)"\)', "xemu_flashrom")
    flash_default = src.find(
        r'ini\.WriteValue\("([\w.]+)", "(flashrom_path)", "\'" \+ Path\.Combine\(flashromPath, "([^"]+)"\)',
        "flashrom default",
        flash.end(),
    )
    choices, choice_lines = feature_choices(features, "xemu", flash.group(1))
    records.append(
        _record(
            "xemu",
            "standalone",
            "core_option",
            _unique([flash_default.group(3), *choices]),
            source_ref(
                (src.path, [src.lines(flash.start(), flash_default.end())]),
                (features.path, [choice_lines]),
            ),
            case_insensitive=True,
            search_dir="bios",
            passes_to=(
                f"xemu.toml [{flash_default.group(1)}] {flash_default.group(2)}=<bios>/<value>,"
                f" default {flash_default.group(3)}"
            ),
            fails_when_missing="path written without an existence check",
            depends_on_option=flash.group(1),
        )
    )

    setup = src.find(r"private void SetupChihiroConfiguration\(", "SetupChihiroConfiguration")
    body = src.block_at(setup.end())
    chain = src.find(
        r'string (\w+) = Path\.Combine\(biosPath, ((?:"[^"]+", )*)"([^"]+)"\);\s*'
        r'if \(!File\.Exists\(\1\)\)\s*\1 = Path\.Combine\(biosPath, ((?:"[^"]+", )*)"([^"]+)"\);',
        "Chihiro BIOS chain",
        body[0],
        body[1],
    )
    dirs = [
        "/".join(["bios", *_strings(chain.group(2))]),
        "/".join(["bios", *_strings(chain.group(4))]),
    ]
    bios_write = src.find(
        r'ini\.WriteValue\("([\w.]+)", "(bios_path)"', "Chihiro bios_path", chain.end(), body[1]
    )
    warning = src.find(
        r'else\s*SimpleLogger\.Instance\.Warning\("([^"]+)"\);',
        "Chihiro missing warning",
        bios_write.end(),
        body[1],
    )
    records.append(
        _record(
            "xemu",
            "standalone",
            "names_searched",
            _unique([chain.group(3), chain.group(5)]),
            source_ref((src.path, [src.lines(chain.start(), warning.end())])),
            case_insensitive=True,
            search_dir=", then ".join(dirs),
            passes_to=f"xemu.toml [{bios_write.group(1)}] {bios_write.group(2)}",
            fails_when_missing=f"warning '{warning.group(1)}', {bios_write.group(2)} not written",
        )
    )

    companions = src.find(
        r"foreach \(var f in new\[\] \{", "Chihiro companion files", chain.end(), body[1]
    )
    names, _ = _array_at(src, companions.end() - 1)
    loop = src.block_at(src.block_at(companions.end() - 1)[1])
    records.append(
        _record(
            "xemu",
            "standalone",
            "fixed_name",
            names,
            source_ref((src.path, [(src.line(companions.start()), src.lines(*loop)[1])])),
            case_insensitive=True,
            search_dir="directory of the Chihiro BIOS found",
            passes_to="nothing passed: existence checked only",
            fails_when_missing="warning per missing file",
        )
    )

    board = src.find(
        r'if \((!File\.Exists\(Path\.Combine\(biosDir, "[^"]+"\)\)(?:\s*&&\s*)?)+\)\s*'
        r'SimpleLogger\.Instance\.Warning\("([^"]+)"\);',
        "media board flash names",
        companions.end(),
        body[1],
    )
    records.append(
        _record(
            "xemu",
            "standalone",
            "names_searched",
            re.findall(r'Path\.Combine\(biosDir, "([^"]+)"\)', board.group(0)),
            source_ref((src.path, [src.lines(board.start(), board.end())])),
            case_insensitive=True,
            search_dir="directory of the Chihiro BIOS found",
            passes_to="nothing passed: warning if none exists",
            fails_when_missing=f"warning '{board.group(2)}'",
        )
    )
    return records


def openmsx(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/OpenMSX.Generator.cs"]
    table = src.find(
        r"Dictionary<string, string\[\]> biosFiles = new Dictionary<string, string\[\]>\(\)\s*\{",
        "biosFiles table",
    )
    block = src.block_at(table.end() - 1)
    check = src.find(r"foreach \(var c in biosFileRequired\)", "per-file BIOS check")
    message, throw_line = _throw_message(src, check.end(), len(src.text))
    folder = re.search(r"in '([^']+)' folder", message)
    if folder is None:
        raise LauncherPatternMissing(src.path, "systemroms folder in the BIOS message")
    records = []
    for entry in re.finditer(
        r'\{ "([^"]+)", new string\[\] \{([^}]*)\} \}', src.text[block[0] : block[1]]
    ):
        start = block[0] + entry.start()
        records.append(
            _record(
                "openmsx",
                "standalone",
                "fixed_name",
                _strings(entry.group(2)),
                source_ref(
                    (
                        src.path,
                        [
                            src.lines(start, block[0] + entry.end()),
                            (src.line(check.start()), throw_line),
                        ],
                    )
                ),
                case_insensitive=False,
                search_dir=folder.group(1).replace("\\", "/"),
                passes_to=f"-machine {entry.group(1)}",
                fails_when_missing=message,
            )
        )
    if not records:
        raise LauncherPatternMissing(src.path, "biosFiles entries")
    return records


def duckstation(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Duckstation.Generator.cs"]
    features = sources[FEATURES]
    search = src.find(
        r'ini\.WriteValue\("BIOS", "SearchDirectory", biosPath\);', "SearchDirectory write"
    )
    option = src.find(r'isOptSet\("(duck_bios)"\)', "duck_bios", search.end())
    block = src.block_at(option.end())
    keys = re.findall(
        r'ini\.WriteValue\("BIOS", "(\w+)", SystemConfig\["duck_bios"\]\);',
        src.text[block[0] : block[1]],
    )
    if not keys:
        raise LauncherPatternMissing(src.path, "duck_bios writes")
    cleared = src.block_at(block[1])
    choices, choice_lines = feature_choices(features, "duckstation", option.group(1))
    return [
        _record(
            "duckstation",
            "standalone",
            "passes_path",
            [],
            source_ref((src.path, [src.lines(search.start(), search.end()), src.lines(*cleared)])),
            search_dir="bios",
            passes_to="settings.ini [BIOS] SearchDirectory = bios",
            fails_when_missing=False,
            depends_on_option=f"{option.group(1)} unset",
        ),
        _record(
            "duckstation",
            "standalone",
            "core_option",
            choices,
            source_ref(
                (src.path, [src.lines(option.start(), block[1])]), (features.path, [choice_lines])
            ),
            search_dir="bios",
            passes_to="settings.ini [BIOS] " + " = ".join(keys) + " = <value>",
            fails_when_missing="not checked by the launcher",
            depends_on_option=option.group(1),
        ),
    ]


def flycast(sources: dict[str, Source]) -> list[Record]:
    src = sources[f"{GENERATORS}/Flycast.Generator.cs"]
    folder = src.find(
        r'string biosPath = Path\.Combine\(' + BIOS + r', "(\w+)"\);', "biosPath under bios"
    )
    write = src.find(
        r'ini\.WriteValue\("(\w+)", "(Dreamcast\.BiosPath)", biosPath\);',
        "Dreamcast.BiosPath write",
        folder.end(),
    )
    return [
        _record(
            "flycast",
            "standalone",
            "passes_path",
            [],
            source_ref(
                (
                    src.path,
                    [src.lines(folder.start(), folder.end()), src.lines(write.start(), write.end())],
                )
            ),
            search_dir=f"bios/{folder.group(1)}",
            passes_to=f"emu.cfg [{write.group(1)}] {write.group(2)} = bios/{folder.group(1)}",
            fails_when_missing=False,
        )
    ]


# Generators that write each BIOS path into the emulator's configuration
# only when the file exists: string x = Path.Combine(bios, ..., "name");
# if (File.Exists(x)) ini.WriteValue("section", "key", x).
GUARDED_WRITES = {
    f"{GENERATORS}/Jgenesis.Generator.cs": "jgenesis",
    f"{GENERATORS}/KegaFusion.Generator.cs": "kega-fusion",
    f"{GENERATORS}/MelonDS.Generator.cs": "melonds",
    f"{GENERATORS}/Mupen64.Generator.cs": "mupen64",
    f"{GENERATORS}/Project64.Generator.cs": "project64",
    f"{GENERATORS}/mGBA.Generator.cs": "mgba",
}

_METHOD = re.compile(
    r"(?:(?:public|private|protected|internal|static|override|virtual)\s+)+[\w<>\[\],. ]+\s\w+\s*\("
)
_TRANSPARENT = re.compile(r"(?:using\s*\(.*\)|try)")
_IF = re.compile(r"(?:else\s+)?if\s*\((.*)\)")
_GUARD = re.compile(
    r'if \(File\.Exists\((\w+)\)\)\s*(\w+)\.WriteValue\("([^"]*)", "([^"]+)", ([^;\n]+)\);'
)
_BIOS_ROOT = r'(?:Program\.)?AppConfig\.GetFullPath\("bios"\)'


def _negate(condition: str) -> str:
    """The condition under which an early return is not taken.

    A conjunction of inequalities becomes the disjunction of equalities,
    anything else is wrapped in not (...).
    """
    tests = condition.split(" && ")
    compared = [re.fullmatch(rf"(\w+) != ({STRING})", t) for t in tests]
    if all(compared):
        return " || ".join(f"{m.group(1)} == {m.group(2)}" for m in compared)
    return f"not ({condition})"


Reach = tuple[tuple[int, int], list[str], list[tuple[int, int]]]


def _conditions(src: Source, offset: int) -> Reach | None:
    """The method holding offset, the conditions under which it is reached,
    and the lines of the early returns among them.

    Each enclosing if adds its condition, and an early return at the top of
    the method adds the condition that does not return. None when a block in
    between is not an if, a using or a try: a loop or an else would need its
    own reading.
    """
    enclosing: list[str] = []
    for block in sorted((b for b in src.blocks() if b[0] < offset < b[1]), reverse=True):
        header = src.header(block)
        if _METHOD.match(header):
            returns, spans = [], []
            for found in re.finditer(r"\bif \(", src.text[block[0] : offset]):
                paren = block[0] + found.end() - 1
                if src.enclosing(paren) != block:
                    continue
                cond = _call_args(src, paren)
                tail = re.compile(r"\s*return\b[^;]*;").match(src.text, paren + len(cond) + 2)
                if tail:
                    returns.append(_negate(" ".join(cond.split())))
                    spans.append(src.lines(block[0] + found.start(), tail.end()))
            return block, returns + enclosing[::-1], spans
        condition = _IF.fullmatch(header)
        if condition:
            text = " ".join(condition.group(1).split())
            switch = re.fullmatch(
                r'SystemConfig\.isOptSet\("(\w+)"\) && SystemConfig\.getOptBoolean\("\1"\)', text
            )
            enclosing.append(f"{switch.group(1)} on" if switch else text)
        elif not _TRANSPARENT.fullmatch(header):
            return None
    return None


def _config_name(src: Source, method: tuple[int, int], writer: str) -> str:
    """The file the writer variable opens, read in its method or else in the file."""
    opener = r"(\w+)\s*=\s*(?:\w+\.FromFile|new \w+)\((\w+)"
    declared = {
        m.group(1): "/".join(_strings(m.group(2)))
        for m in re.finditer(
            r'string (\w+) = Path\.Combine\(path, ((?:"[^"]+"(?:, )?)+)\);', src.text
        )
    }
    local = [
        m.group(2)
        for m in re.finditer(opener, src.text[method[0] : method[1]])
        if m.group(1) == writer and m.group(2) in declared
    ]
    if local:
        return declared[local[0]]
    files = {declared[m.group(2)] for m in re.finditer(opener, src.text) if m.group(2) in declared}
    if len(files) != 1:
        raise LauncherPatternMissing(src.path, f"configuration file opened as {writer}")
    return files.pop()


def _root_writes(src: Source, emulator: str) -> list[Record]:
    """Configuration keys that receive the bios folder itself."""
    records = []
    for root in re.finditer(rf"string (\w+) = {_BIOS_ROOT};", src.text):
        root_reach = _conditions(src, root.start())
        if root_reach is None:
            continue
        method = root_reach[0]
        for write in re.compile(
            rf'(\w+)\.WriteValue\("([^"]*)", "([^"]+)", {root.group(1)}\);'
        ).finditer(src.text, root.end(), method[1]):
            reached = _conditions(src, write.start())
            if reached is None:
                continue
            writer, section, key = write.groups()
            target = f"[{section}] {key}" if section else key
            records.append(
                _record(
                    emulator,
                    "standalone",
                    "passes_path",
                    [],
                    source_ref(
                        (
                            src.path,
                            [
                                *reached[2],
                                src.lines(root.start(), root.end()),
                                src.lines(write.start(), write.end()),
                            ],
                        )
                    ),
                    search_dir="bios",
                    passes_to=f"{_config_name(src, method, writer)} {target} = bios",
                    fails_when_missing=False,
                    depends_on_option=", ".join(reached[1]) or None,
                )
            )
    return records


def _guarded_write(src: Source, emulator: str, guard: re.Match[str]) -> Record | None:
    """The record of one guarded write, None when its shape is another one.

    The written value must be the checked path itself (quoted or with its
    separators replaced), the path a literal under the bios folder that
    nothing reassigns before the check, and every block around the write an
    if, a using or a try.
    """
    var, writer, section, key, value = guard.groups()
    if not re.fullmatch(
        rf"(?:{STRING} \+ )?{var}(?:\.Replace\({STRING}, {STRING}\))?(?: \+ {STRING})?",
        value.strip(),
    ):
        return None
    reached = _conditions(src, guard.start())
    if reached is None:
        return None
    method, conditions, returns = reached
    decls = list(
        re.compile(
            rf"string {var} = Path\.Combine\((\w+|{_BIOS_ROOT}), ((?:{STRING}, )*{STRING})\);"
        ).finditer(src.text, method[0], guard.start())
    )
    if not decls or re.search(rf"\b{var}\s*=[^=]", src.text[decls[-1].end() : guard.start()]):
        return None
    decl = decls[-1]
    root = decl.group(1)
    if not re.fullmatch(_BIOS_ROOT, root) and not re.search(
        rf"string {root} = {_BIOS_ROOT};", src.text[method[0] : decl.start()]
    ):
        return None
    *folders, name = _strings(decl.group(2))
    target = f"[{section}] {key}" if section else key
    return _record(
        emulator,
        "standalone",
        "fixed_name",
        [name],
        source_ref(
            (
                src.path,
                [
                    *returns,
                    src.lines(decl.start(), decl.end()),
                    src.lines(guard.start(), guard.end()),
                ],
            )
        ),
        case_insensitive=True,
        search_dir="/".join(["bios", *folders]),
        passes_to=f"{_config_name(src, method, writer)} {target}, only if the file exists",
        fails_when_missing=f"{key} not written",
        depends_on_option=", ".join(conditions) or None,
    )


def guarded_writes_in(src: Source, emulator: str) -> list[Record]:
    """Every guarded write of one generator, then the keys given the bios folder."""
    found = [_guarded_write(src, emulator, guard) for guard in _GUARD.finditer(src.text)]
    records = [record for record in found if record is not None]
    if not records:
        raise LauncherPatternMissing(src.path, "File.Exists guarded WriteValue")
    return records + _root_writes(src, emulator)


def guarded_writes(sources: dict[str, Source]) -> list[Record]:
    """One fixed name per configuration key, written only when the file exists."""
    return [
        record
        for path, emulator in GUARDED_WRITES.items()
        for record in guarded_writes_in(sources[path], emulator)
    ]


EXTRACTORS: tuple[Callable[[dict[str, Source]], list[Record]], ...] = (
    amiberry,
    ares,
    duckstation,
    flycast,
    guarded_writes,
    hatari,
    hatari_libretro,
    mesen,
    openmsx,
    pcsx2,
    pcsx2_libretro,
    psx_cascades,
    winuae,
    xemu,
)


def extract_launchers(texts: dict[str, str]) -> list[Record]:
    """Every launcher record the extractors find, in extractor order."""
    missing = [path for path in SOURCE_FILES if path not in texts]
    if missing:
        raise LauncherPatternMissing(missing[0], "source file")
    sources = {path: Source(path, text) for path, text in texts.items()}
    return [record for extractor in EXTRACTORS for record in extractor(sources)]
