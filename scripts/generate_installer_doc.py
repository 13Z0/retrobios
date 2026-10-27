#!/usr/bin/env python3
"""Rewrite the marked tables of wiki/installer.md from install.py.

    python scripts/generate_installer_doc.py           rewrite the tables
    python scripts/generate_installer_doc.py --check   exit 1 when one is stale

Each table sits between `<!-- generated: NAME -->` and
`<!-- /generated: NAME -->`. What install.py knows is written from it: the
options and their arguments from its parser, the event kinds and their keys
from EVENT_KEYS, the exit codes from its sys.exit calls, the RETROBIOS_PAD
values and each route's keys from TUI_ROUTES. The prose cells (what an option
does, when an event comes) are kept as written, matched by their row. A row
install.py gains has no prose yet: the generator names it and writes nothing,
and the row is added by hand inside the markers, its first cell holding the
option, kind, code or value alone.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "wiki" / "installer.md"
INSTALLER = ROOT / "install.py"

_spec = importlib.util.spec_from_file_location("install", INSTALLER)
_install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_install)

BLOCK = re.compile(
    r"(?P<open><!-- generated: (?P<name>[a-z-]+) -->\n)(?P<body>.*?)"
    r"(?P<close><!-- /generated: (?P=name) -->)", re.S)
_CELL = re.compile(r"(?<!\\)\|")

# The help screen's actions, as the page names them.
ACTIONS = {
    "move": "Move",
    "page": "Page",
    "ok": "Continue, open, choose",
    "tick": "Tick, untick",
    "options": "Options",
    "back": "Back, and quit on the first screen",
    "stop": "Stop an install",
    "kill": "Quit at once",
    "enter": "Use the folder typed",
    "esc": "Clear a filter, leave the folder typed",
}
# The two routes no launcher names, as the page heads their columns.
ROUTE_HEADINGS = {"keys": "Computer (`keys`)", "both": "Unknown (`both`)"}


class MissingProse(Exception):
    """Rows install.py has that the page does not describe yet."""


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in _CELL.split(line.strip().strip("|"))]


def _rows(body: str) -> list[list[str]]:
    lines = [line for line in body.splitlines() if line.startswith("|")]
    return [_cells(line) for line in lines[2:]]


def _prose(body: str, key: re.Pattern[str], width: int) -> dict[str, list[str]]:
    """The prose cells of each row, by the key its first cell holds."""
    kept = {}
    for cells in _rows(body):
        found = key.search(cells[0])
        if found:
            kept[found.group(1)] = (cells[1:] + [""] * width)[:width]
    return kept


def _table(heading: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(heading) + " |",
             "|" + "|".join("-" * (len(cell) + 2) for cell in heading) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def _with_prose(name: str, facts: list[tuple[str, str]], body: str, key: str,
                width: int) -> list[list[str]]:
    """Each row's first cell from install.py, the rest from the page, whose
    last cell must be written."""
    prose = _prose(body, re.compile(key), width)
    missing = [fact for fact, _cell in facts if not prose.get(fact, [""])[-1]]
    if missing:
        raise MissingProse(f"{name}: no text for {', '.join(missing)}")
    return [[cell, *prose[fact]] for fact, cell in facts]


def option_rows(body: str) -> list[list[str]]:
    facts = []
    # argparse's own --help last, after the options of the run.
    for action in sorted(_install.build_parser()._actions,
                         key=lambda action: isinstance(action, argparse._HelpAction)):
        flags = sorted(action.option_strings, key=lambda flag: not flag.startswith("--"))
        metavar = "" if action.nargs == 0 else (action.metavar or action.dest.upper())
        shown = ", ".join(f"`{flag} {metavar}`" if metavar else f"`{flag}`" for flag in flags)
        facts.append((flags[0], f'<span id="option-{flags[0][2:]}"></span>{shown}'))
    return _with_prose("options", facts, body, r"`(--[a-z-]+)", 1)


def exit_codes() -> list[int]:
    """The codes install.py exits with: its sys.exit calls, 0 for a run
    that returns, 2 for an option argparse refuses."""
    codes = {0, 2}
    for node in ast.walk(ast.parse(INSTALLER.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "exit" and getattr(node.func.value, "id", "") == "sys"
                and node.args):
            continue
        given = node.args[0]
        for value in ((given.body, given.orelse) if isinstance(given, ast.IfExp) else (given,)):
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                codes.add(value.value)
    return sorted(codes)


def exit_rows(body: str) -> list[list[str]]:
    facts = [(str(code), f"`{code}`") for code in exit_codes()]
    return _with_prose("exit-codes", facts, body, r"`(\d+)`", 1)


def event_rows(body: str) -> list[list[str]]:
    facts = [(kind, f"`{kind}`") for kind in _install.EVENT_KEYS]
    rows = _with_prose("events", facts, body, r"`([a-z_]+)`", 2)
    for row, keys in zip(rows, _install.EVENT_KEYS.values()):
        row[1] = ", ".join(f"`{key}`" for key in keys)
    return rows


def pad_rows(body: str) -> list[list[str]]:
    facts = [(route, f"`{route}`") for route in _install.TUI_PAD_ROUTES]
    return _with_prose("pad-routes", facts, body, r"`([a-z_]+)`", 2)


def route_keys() -> str:
    routes = _install.TUI_ROUTES
    actions = list(next(iter(routes.values())).help)
    unnamed = [action for action in actions if action not in ACTIONS]
    if unnamed or any(list(route.help) != actions for route in routes.values()):
        raise MissingProse(f"route-keys: TUI_ROUTES actions {unnamed or 'differ by route'} "
                           "need a name in ACTIONS")
    heading = ["Action", *(ROUTE_HEADINGS.get(name, f"`{name}`") for name in routes)]
    rows = [[ACTIONS[action], *(route.help[action] or "none" for route in routes.values())]
            for action in actions]
    return _table(heading, rows)


def _marker(rule: _install._OsRule) -> str:
    """A card rule's evidence in words: the file or folder, and the line it
    holds for a rule that reads one."""
    if rule.method == "file_matches":
        path, pattern = rule.values
        line = re.sub(r"\\s\*\$$", "", re.sub(r"^(\^|\\A)", "", pattern)).replace("\\", "")
        return f"`{path}` holding the line `{line}`"
    return ", ".join(f"`{value}`" if PurePosixPath(value).suffix else f"`{value}/`"
                     for value in rule.values)


def _list_words(plat: str) -> str:
    name = _install.display_name(plat)
    return f"{name}'{'' if name.endswith('s') else 's'} list"


def card_marker_rows() -> list[list[str]]:
    """The files that name a card or a SHARE drive, in the order they are
    read: install.py's card rules, then its SHARE drive files."""
    rows = [[_marker(rule), rule.name, f"`{rule.bios}`, {_list_words(rule.manifest)}"]
            for rule in _install._card_rules()]
    hostname = "system.hostname=BATOCERA"
    for plat, marker in _install._SHARE_DRIVES:
        name = _install.display_name(plat)
        evidence = (f"`{marker}` holding `{hostname}` or no hostname line, and no KNULLI file "
                    "above" if plat == "batocera" else f"`{marker}`")
        rows.append([evidence, f"{name} SHARE drive", f"`bios`, {_list_words(plat)}"])
    rows.append([f"`{_install._SHARE_DRIVES[0][1]}` with any other hostname, and no KNULLI "
                 "file above, or the files of two SHARE drives",
                 "named by neither: the run says which systems it could be",
                 "the folder of the system named with `--platform`"])
    return rows


def render(page: str) -> str:
    """The page with each marked table written from install.py."""
    writers = {
        "options": lambda body: _table(["Option", "Effect"], option_rows(body)),
        "exit-codes": lambda body: _table(["Code", "Meaning"], exit_rows(body)),
        "events": lambda body: _table(["Kind", "Keys", "When"], event_rows(body)),
        "pad-routes": lambda body: _table(["Value", "Input", "Set by"], pad_rows(body)),
        "route-keys": lambda _body: route_keys(),
        "card-markers": lambda _body: _table(["On the card", "System", "Files go to"],
                                             card_marker_rows()),
    }
    seen = []

    def write(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in writers:
            raise MissingProse(f"no table is written for the marker {name!r}")
        seen.append(name)
        table = writers[name](match.group("body"))
        return f"{match.group('open')}\n{table}\n{match.group('close')}"

    text = BLOCK.sub(write, page)
    absent = sorted(set(writers) - set(seen))
    if absent:
        raise MissingProse(f"markers missing from the page: {', '.join(absent)}")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when a table differs from install.py, writing nothing")
    parser.add_argument("--page", type=Path, default=PAGE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    current = args.page.read_text(encoding="utf-8")
    try:
        text = render(current)
    except MissingProse as exc:
        print(f"{args.page}: {exc}. Add the row inside its markers, then run "
              "python scripts/generate_installer_doc.py", file=sys.stderr)
        return 1
    if text == current:
        return 0
    if args.check:
        print(f"{args.page} differs from install.py: run "
              "python scripts/generate_installer_doc.py", file=sys.stderr)
        return 1
    args.page.write_text(text, encoding="utf-8")
    print(f"Wrote {args.page}")
    return 0


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
