"""The grammar of a profile's source_ref: where a reference points.

Read by the site to link citations and by profile_sync to check them. Kept
apart so that building the site does not depend on the comparison tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REF_RE = re.compile(r"^(?P<path>[^:]+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")
_BARE_RANGE_RE = re.compile(r"^:?(\d+(?:-\d+)?)$")
_SPLIT_RE = re.compile(r"[,;]")
_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)?")
_LOCATED_RE = re.compile(r"^[^\s:]+:\d+(?:-\d+)?$")


@dataclass(frozen=True)
class RefPart:
    path: str
    start: int | None
    end: int | None
    raw: str = ""

    @property
    def last(self) -> int | None:
        """Last cited line: the end of a range, or the single line."""
        return self.end or self.start


def parse_source_ref(ref: str) -> tuple[str, int | None, int | None]:
    """Split 'path/file.cpp:123-129' into (path, start, end)."""
    m = _REF_RE.match(ref.strip())
    if not m:
        return ref.strip(), None, None
    start = int(m.group("start")) if m.group("start") else None
    end = int(m.group("end")) if m.group("end") else start
    return m.group("path"), start, end


def source_ref_values(value: str | dict | None) -> list[tuple[str, str]]:
    """Flatten a source_ref into (label, refs) pairs.

    Most entries hold a plain string. `ymir` keys its refs by mode instead,
    since its standalone and libretro builds read different files.
    """
    if isinstance(value, dict):
        return [(str(k), str(v)) for k, v in value.items() if v]
    return [("", str(value))] if value else []


def split_source_ref(ref: str) -> list[RefPart]:
    """A source_ref may carry several references.

    Separators are the comma and the semicolon. A part reduced to a line or a
    line range continues the previous part's file: `geo.c:234-243, 273-285`
    cites two ranges of the same file, a form used by 430 parts across 69
    profiles. Parenthetical annotations are prose and are dropped: 285 entries
    across 50 profiles carry them.
    """
    parts: list[RefPart] = []
    for raw in split_outside_parentheses(str(ref or "")):
        chunk = _trim_prose(_ANNOTATION_RE.sub("", raw).strip())
        if not chunk:
            continue
        bare = _BARE_RANGE_RE.match(chunk)
        if bare and parts:
            start, _, end = bare.group(1).partition("-")
            parts.append(
                RefPart(parts[-1].path, int(start), int(end or start), raw)
            )
            continue
        path, start_line, end_line = parse_source_ref(chunk)
        parts.append(RefPart(path, start_line, end_line, raw))
    return parts


def ref_prose_text(ref: str) -> str:
    """The text of a source_ref its parts do not read, positions kept.

    split_source_ref keeps the located head of each chunk and drops the
    rest: annotations, and prose after the head. A citation written there
    (`(MACHINE_IS_SPECTRUM_128_P2 covers Pentagon, src/cpu.h:545)`, or the
    second half of `pce.cpp:477-483 + huc.cpp:284-304`) was never anchored.
    Every character a part reads is blanked, so the prose scanner finds
    the rest at the offsets it has in the scalar.
    """
    masked = list(ref)
    depth = 0
    outside: list[int] = []

    def settle() -> None:
        text = "".join(ref[i] for i in outside)
        head = text.strip().split(" ", 1)[0] if text.strip() else ""
        located = bool(head) and bool(
            _LOCATED_RE.match(head) or _BARE_RANGE_RE.match(head)
        )
        blank = outside
        if located:
            first = next(k for k, i in enumerate(outside) if not ref[i].isspace())
            blank = outside[first:first + len(head)]
        for i in blank:
            masked[i] = " "
        outside.clear()

    for index, char in enumerate(ref):
        if char == "(":
            depth += 1
        if depth == 0:
            if char in ",;":
                settle()
            else:
                outside.append(index)
        if char == ")":
            depth = max(0, depth - 1)
    settle()
    return "".join(masked)


def split_outside_parentheses(text: str) -> list[str]:
    """Split on commas and semicolons, ignoring those inside an annotation.

    Each chunk keeps its annotation, so a rewrite can put the prose back.
    """
    chunks: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char in ",;" and depth == 0:
            chunks.append("".join(current))
            current = []
        else:
            current.append(char)
    chunks.append("".join(current))
    return [c for c in (chunk.strip() for chunk in chunks) if c]


def _trim_prose(chunk: str) -> str:
    """Drop a trailing comment written without parentheses.

    `libretro.cpp:1520-1521 candidates_a1200` cites a location followed by a
    note. The leading token is kept only when it already reads as a reference,
    so `munt ROMInfo.cpp`, which names an external project, stays intact and
    is reported rather than silently truncated to `munt`.
    """
    if " " not in chunk:
        return chunk
    head = chunk.split(" ", 1)[0]
    if _LOCATED_RE.match(head) or _BARE_RANGE_RE.match(head):
        return head
    return chunk
