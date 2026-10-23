#!/usr/bin/env python3
"""Turn install.py --events into tab-separated records for the launchers.

The launchers are bash, so they read one record per line with
`IFS=$'\\t' read`: tui.txt reads the system and its BIOS folder to name the
pack an offline run needs, and the PortMaster screens draw through
PortMaster's dialog FIFO. Text fields are cleaned for that FIFO:
PortMasterDialog sends its arguments through an unquoted `echo $(printf ...)`,
where a tab or newline splits a field and `*`, `?` or `[` expand as a file
pattern.

    rbevents.py status EVENTS        records of a --check run
    rbevents.py follow SUMMARY       an install run on stdin: progress
                                     records on stdout, the outcome in SUMMARY

Python 3.8 and the standard library, like install.py.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator, TextIO

_UNSAFE = re.compile(r"[\x00-\x1f\x7f*?\[\]\\]+")
_SPACES = re.compile(r" {2,}")
# A system id becomes a bash array subscript and a menu key in the launcher.
# Every manifest uses slugs, and anything else is dropped, never cleaned into one.
_SYSTEM_ID = re.compile(r"[A-Za-z0-9._-]+")


def clean(value: object) -> str:
    """One FIFO-safe field: no control character, pattern character or run of spaces."""
    text = _UNSAFE.sub(" ", str(value))
    return _SPACES.sub(" ", text).strip()


def size_text(count: int) -> str:
    """12 KB, 3.4 MB, 1.2 GB."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def events(lines: Iterable[str]) -> Iterator[dict]:
    """The JSON objects of an event stream. A line that is not one is skipped."""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("event"), str):
            yield event


def record(out: TextIO, *fields: object) -> None:
    # A tab is whitespace to `read`, so two in a row are one separator: an
    # empty field would shift the ones after it. "-" stands for it.
    out.write("\t".join(clean(field) or "-" for field in fields) + "\n")
    out.flush()


def status(stream: Iterable[str], out: TextIO) -> None:
    """The records the start screen and the menus read.

    Only the first detected platform is described: a handheld runs one.
    """
    platform = None
    ended = False
    for event in events(stream):
        kind = event["event"]
        if kind == "start":
            record(out, "start", event.get("installer", ""))
        elif kind == "detect":
            found = event.get("platforms") or []
            record(out, "detect", event.get("os_name", ""), len(found))
            if found:
                first = found[0]
                platform = first.get("platform")
                record(out, "platform", platform, first.get("name", ""),
                       first.get("bios_dir", ""))
        elif kind == "error":
            record(out, "error", event.get("reason", ""), event.get("message", ""))
        elif kind == "system" and event.get("platform") == platform:
            if not _SYSTEM_ID.fullmatch(str(event.get("system", ""))):
                continue
            needed = event.get("missing", 0) + event.get("wrong_hash", 0)
            system = event.get("system", "")
            # install.py files what belongs to no single system under "other".
            name = "Files of no single system" if system == "other" else event.get("name", "")
            record(out, "system", system, name,
                   event.get("files", 0), event.get("present", 0), event.get("missing", 0),
                   event.get("wrong_hash", 0), size_text(event.get("bytes_needed", 0)),
                   "1" if needed else "0")
        elif kind == "check" and event.get("platform") == platform:
            record(out, "check", event.get("files", 0), event.get("present", 0),
                   event.get("missing", 0), event.get("wrong_hash", 0),
                   size_text(event.get("bytes_needed", 0)), event.get("data_missing", 0),
                   event.get("not_installable", 0))
        elif kind == "end":
            ended = True
            record(out, "end", event.get("code", 1))
    if not ended:
        # Refused before the run started, or stopped without a word.
        record(out, "end", "none")


def follow(stream: Iterable[str], out: TextIO, summary: TextIO) -> None:
    """Progress records as an install runs, and its outcome in *summary*."""
    ended = False
    for event in events(stream):
        kind = event["event"]
        if kind == "download":
            record(out, "download", event.get("files", 0), size_text(event.get("bytes", 0)))
        elif kind == "downloaded":
            record(out, "file", event.get("files_done", 0), event.get("files_total", 0),
                   "ok" if event.get("ok") else "failed", event.get("dest", ""),
                   size_text(event.get("bytes_done", 0)),
                   size_text(event.get("bytes_total", 0)))
        elif kind == "progress":
            record(out, "progress", event.get("files_done", 0), event.get("files_total", 0),
                   size_text(event.get("bytes_done", 0)),
                   size_text(event.get("bytes_total", 0)))
        elif kind == "error":
            record(out, "error", event.get("reason", ""), event.get("message", ""))
            record(summary, "error", event.get("reason", ""), event.get("message", ""))
        elif kind == "summary":
            record(summary, "summary", event.get("installed", 0), event.get("up_to_date", 0),
                   event.get("pending", 0), "1" if event.get("interrupted") else "0")
            for failure in event.get("failed") or []:
                record(summary, "failed", failure.get("dest", ""), failure.get("reason", ""))
            for kept in event.get("kept") or []:
                record(summary, "kept", kept.get("folder", ""), kept.get("files", 0),
                       size_text(kept.get("bytes", 0)))
            for group in event.get("skipped") or []:
                record(summary, "skipped", group.get("label", ""), group.get("files", 0),
                       group.get("required", 0))
        elif kind == "end":
            ended = True
            record(summary, "end", event.get("code", 1))
    if not ended:
        record(summary, "end", "none")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "status":
        with open(argv[1], encoding="utf-8", errors="replace") as stream:
            status(stream, sys.stdout)
        return 0
    if len(argv) == 2 and argv[0] == "follow":
        with Path(argv[1]).open("w", encoding="utf-8") as summary:
            follow(sys.stdin, sys.stdout, summary)
        return 0
    sys.stderr.write("usage: rbevents.py status EVENTS | follow SUMMARY\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
