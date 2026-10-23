"""The guide part of the site: where each setup keeps its BIOS files.

A platform page opens on what someone holding the device needs: the folder,
the command, the pack, how to check. The words live in
``platforms/_guides.yml``, one guide per registered platform, beside the
device groups, the terminal tabs and the safety notes every guide shares.
This module loads that file, checks it against the registry and renders the
parts the pages share. The registry keeps the folder the pack unzips into
(``extract_to``) and the group a platform belongs to (``device_group``).
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, fields
from pathlib import Path

import sitemarkup as markup
from safeparse import require_yaml

PLATFORMS_DIR = Path(__file__).resolve().parent.parent / "platforms"
GUIDES_FILE = PLATFORMS_DIR / "_guides.yml"


class GuideError(ValueError):
    """The guides file does not say what a page needs, or contradicts the registry."""


@dataclass(frozen=True)
class Guide:
    """What a platform page says before its file reference.

    `folders` are the common cases, `more_folders` the rarer builds shown
    folded under them, under `more_folders_title`. `more_folders_detected`
    adds a row for each named system the registry detects the platform on.
    `notes` stay in the guide, one plain fact each. `reference_notes` carry
    the source citations and the details a reader checking the work wants,
    and open the Reference part of the page. `download_hint` is one more
    bullet under the pack download. `pack_manual` is the manual part of the
    README inside the pack. `logo_owner` and `logo_licence` credit the
    logo the registry links. `runs_on` names the hardware, in the project's
    own words, for its tile and its row. `terminal` replaces the shared
    "On the device" text with how this system opens a terminal. `lead` comes
    before the folder table, `finish` closes the guide. `{folders}` in
    `pack_manual` is the folder list of the page, so the README and the page
    cannot name two folders for one device.
    """

    summary: str
    folders: tuple[tuple[str, str], ...]
    run: tuple[str, ...]
    notes: tuple[str, ...] = ()
    dest: str = ""
    on_card: bool = False
    more_folders: tuple[tuple[str, str], ...] = ()
    more_folders_title: str = "Other layouts"
    more_folders_detected: bool = False
    more_folders_note: str = ""
    reference_notes: tuple[str, ...] = ()
    download_hint: str = ""
    pack_manual: str = ""
    logo_owner: str = ""
    logo_licence: str = ""
    runs_on: str = ""
    terminal: str = ""
    lead: str = ""
    finish: str = ""


_GUIDE_KEYS = {f.name for f in fields(Guide)}
_REQUIRED = ("summary", "folders", "run")
_TOP_KEYS = {"install_base", "trust_items", "device_groups", "terminals", "guides"}
_GROUP_KEYS = {"label", "description", "anchor", "also_runs"}
_ALSO_KEYS = ("setup", "summary", "uses", "runs_on", "folder")
_TERMINAL_KEYS = ("label", "how", "shell")


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuideError(f"{where}: expected text, found {value!r}")
    return value


def _texts(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GuideError(f"{where}: expected a list, found {value!r}")
    return tuple(_text(item, f"{where}[{i}]") for i, item in enumerate(value))


def _unknown(data: dict, allowed: set[str] | tuple[str, ...], where: str) -> None:
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise GuideError(f"{where}: unknown key {', '.join(extra)} "
                         f"(known: {', '.join(sorted(allowed))})")


def _folder_rows(value: object, extract_to: str, where: str) -> tuple[tuple[str, str], ...]:
    """Folder rows as (where, path). A row with no path is the registry's
    extract_to, so that folder is written once."""
    if not isinstance(value, list) or not value:
        raise GuideError(f"{where}: expected a list of folder rows")
    rows = []
    for i, row in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(row, dict):
            raise GuideError(f"{at}: expected a mapping with `where` and `path`")
        _unknown(row, ("where", "path"), at)
        label = _text(row.get("where"), f"{at}.where")
        if "path" in row:
            rows.append((label, _text(row["path"], f"{at}.path")))
        elif extract_to:
            rows.append((label, f"`{extract_to}`"))
        else:
            raise GuideError(f"{at}: no `path`, and the registry has no extract_to for it")
    return tuple(rows)


def _guide(name: str, data: object, extract_to: str, terminals: dict) -> Guide:
    where = f"{GUIDES_FILE.name}: guides.{name}"
    if not isinstance(data, dict):
        raise GuideError(f"{where}: expected a mapping")
    _unknown(data, _GUIDE_KEYS, where)
    missing = [key for key in _REQUIRED if key not in data]
    if missing:
        raise GuideError(f"{where}: missing {', '.join(missing)}")
    run = _texts(data["run"], f"{where}.run")
    unknown_tabs = [key for key in run if key not in terminals]
    if unknown_tabs:
        raise GuideError(f"{where}.run: no terminal named {', '.join(unknown_tabs)}")
    values: dict = {
        "summary": _text(data["summary"], f"{where}.summary"),
        "folders": _folder_rows(data["folders"], extract_to, f"{where}.folders"),
        "run": run,
    }
    if "more_folders" in data:
        values["more_folders"] = _folder_rows(
            data["more_folders"], extract_to, f"{where}.more_folders")
    for key in ("notes", "reference_notes"):
        if key in data:
            values[key] = _texts(data[key], f"{where}.{key}")
    for key in ("dest", "download_hint", "more_folders_title", "more_folders_note",
                "logo_owner", "logo_licence", "runs_on", "terminal", "lead", "finish"):
        if key in data:
            values[key] = _text(data[key], f"{where}.{key}")
    for key in ("on_card", "more_folders_detected"):
        if key in data:
            if not isinstance(data[key], bool):
                raise GuideError(f"{where}.{key}: expected true or false")
            values[key] = data[key]
    if "pack_manual" in data:
        values["pack_manual"] = _text(data["pack_manual"], f"{where}.pack_manual")
    return Guide(**values)


def load_guides(path: Path = GUIDES_FILE, registry: dict | None = None) -> dict:
    """The guides file, checked against the registry.

    Every registered platform, archived ones included, has a guide and a
    device group the file describes. A mistake raises GuideError naming the
    key, so the site is never built from half a guide.
    """
    yaml = require_yaml()
    if registry is None:
        with open(PLATFORMS_DIR / "_registry.yml", encoding="utf-8") as handle:
            registry = (yaml.safe_load(handle) or {}).get("platforms", {})
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise GuideError(f"{path.name}: expected a mapping")
    _unknown(data, _TOP_KEYS, path.name)

    terminals: dict[str, tuple[str, str, str]] = {}
    for key, entry in (data.get("terminals") or {}).items():
        at = f"{path.name}: terminals.{key}"
        if not isinstance(entry, dict):
            raise GuideError(f"{at}: expected a mapping")
        _unknown(entry, _TERMINAL_KEYS, at)
        terminals[key] = tuple(_text(entry.get(k), f"{at}.{k}") for k in _TERMINAL_KEYS)
        if entry["shell"] not in ("sh", "powershell"):
            raise GuideError(f"{at}.shell: sh or powershell, found {entry['shell']!r}")

    raw_guides = data.get("guides") or {}
    groups: dict[str, tuple[str, str]] = {}
    anchors: dict[str, str] = {}
    also_runs: dict[str, tuple[tuple[str, str, str, str, str], ...]] = {}
    for key, entry in (data.get("device_groups") or {}).items():
        at = f"{path.name}: device_groups.{key}"
        if not isinstance(entry, dict):
            raise GuideError(f"{at}: expected a mapping")
        _unknown(entry, _GROUP_KEYS, at)
        groups[key] = (_text(entry.get("label"), f"{at}.label"),
                       _text(entry.get("description"), f"{at}.description"))
        anchors[key] = _text(entry.get("anchor"), f"{at}.anchor")
        rows = []
        for i, row in enumerate(entry.get("also_runs") or []):
            row_at = f"{at}.also_runs[{i}]"
            if not isinstance(row, dict):
                raise GuideError(f"{row_at}: expected a mapping")
            _unknown(row, _ALSO_KEYS, row_at)
            values = tuple(_text(row.get(k), f"{row_at}.{k}") for k in _ALSO_KEYS)
            if values[2] not in raw_guides:
                raise GuideError(f"{row_at}.uses: {values[2]!r} is not a platform with a guide")
            rows.append(values)
        if rows:
            also_runs[key] = tuple(rows)

    unregistered = sorted(set(raw_guides) - set(registry))
    if unregistered:
        raise GuideError(f"{path.name}: guides for unregistered platforms: "
                         f"{', '.join(unregistered)}")
    guides = {}
    for name, entry in registry.items():
        if name not in raw_guides:
            raise GuideError(f"{path.name}: {name} is registered and has no guide "
                             f"(add guides.{name})")
        group = entry.get("device_group")
        if group not in groups:
            raise GuideError(f"_registry.yml: {name}.device_group {group!r} is not "
                             f"one of {', '.join(groups)} in {path.name}")
        guides[name] = _guide(name, raw_guides[name], str(entry.get("extract_to") or ""),
                              terminals)
    # The order the file is written in, which the pages do not depend on.
    guides = {name: guides[name] for name in raw_guides}
    return {
        "registry": registry,
        "install_base": _text(data.get("install_base"), f"{path.name}: install_base"),
        "trust_items": _texts(data.get("trust_items"), f"{path.name}: trust_items"),
        "device_groups": groups,
        "group_anchors": anchors,
        "also_runs": also_runs,
        "terminals": terminals,
        "guides": guides,
    }


_LOADED = load_guides()
INSTALL_BASE: str = _LOADED["install_base"]
DEVICE_GROUPS: dict[str, tuple[str, str]] = _LOADED["device_groups"]
GROUP_ANCHORS: dict[str, str] = _LOADED["group_anchors"]
ALSO_RUNS: dict[str, tuple[tuple[str, str, str, str, str], ...]] = _LOADED["also_runs"]
TERMINALS: dict[str, tuple[str, str, str]] = _LOADED["terminals"]
TRUST_ITEMS: tuple[str, ...] = _LOADED["trust_items"]
GUIDES: dict[str, Guide] = _LOADED["guides"]
_REGISTRY: dict = _LOADED["registry"]


def detected_systems(registry: dict, platform: str) -> list[tuple[str, str]]:
    """Named systems a platform runs on, handhelds that run RetroArch for one,
    with the BIOS folder the installer writes on each, read from the
    detection rules of the registry."""
    seen: dict[str, str] = {}
    for rule in (registry.get(platform, {}).get("install") or {}).get("detect") or []:
        name, folder = rule.get("name"), rule.get("bios_path")
        if name and folder and rule.get("installs", True) and name not in seen:
            seen[name] = folder
    return sorted(seen.items(), key=lambda item: item[0].casefold())


def folder_list(name: str, registry: dict | None = None) -> list[tuple[str, str]]:
    """Every folder a guide names, as (where, path): the common ones, the
    rarer builds, and the systems the registry detects when the guide says so."""
    guide = GUIDES[name]
    rows = [*guide.folders, *guide.more_folders]
    if guide.more_folders_detected:
        rows += [(system, f"`{folder}`")
                 for system, folder in detected_systems(registry or _REGISTRY, name)]
    return rows


def _plain_folders(name: str) -> str:
    rows = folder_list(name)
    width = max(len(where) for where, _path in rows) + 1
    return "\n".join(f"{where + ':':<{width}} {path.replace('`', '')}" for where, path in rows)


def setup_count() -> int:
    """Setups the site covers: every registered platform, archived ones
    included, since each keeps its page, its pack and its command. The one
    count every page states."""
    return len(GUIDES)


def pack_manual(name: str) -> str:
    """The manual part of a pack README, indented as the README is, or empty."""
    guide = GUIDES.get(name)
    if not guide or not guide.pack_manual:
        return ""
    text = guide.pack_manual.rstrip("\n")
    if "{folders}" in text:
        indent = next(line[: len(line) - len(line.lstrip())]
                      for line in text.splitlines() if "{folders}" in line)
        text = text.replace("{folders}", textwrap.indent(_plain_folders(name), indent).lstrip())
    return textwrap.indent(text, "  ") + "\n\n"


def install_command(shell: str, args: str = "") -> str:
    """The one-liner for a shell, with installer options when given.

    PowerShell passes options only through a scriptblock: `irm ... | iex`
    takes none.
    """
    if shell == "sh":
        return f"curl -fsSL {INSTALL_BASE}/install.sh | sh" + (f" -s -- {args}" if args else "")
    if args:
        return f"&([scriptblock]::Create((irm {INSTALL_BASE}/install.ps1))) {args}"
    return f"irm {INSTALL_BASE}/install.ps1 | iex"


# The title over each command block, by the shell it is written for.
FENCE_TITLES = {"sh": "Terminal", "powershell": "PowerShell"}


def fence(shell: str, command: str, indent: str = "") -> list[str]:
    """A command block with its title, as every command on the site is shown."""
    return [f'{indent}```{shell} title="{FENCE_TITLES[shell]}"', f"{indent}{command}", f"{indent}```"]


def terminal_tabs(
    tabs: tuple[str, ...], args: dict[str, str], indent: str = "", steps: bool = True,
    how: dict[str, str] | None = None,
) -> list[str]:
    """Content tabs, one per place the command runs, each with how to open
    the terminal there. Tab labels are shared across the site, so the reader's
    choice carries from page to page. *how* replaces the shared text of a tab
    for one setup."""
    items = []
    for key in tabs:
        label, shared, shell = TERMINALS[key]
        text = (how or {}).get(key) or shared
        body = [text, ""] if steps else []
        items.append((label, body + fence(shell, install_command(shell, args.get(key, "")))))
    return markup.tabs(items, indent)


def platform_args(name: str, guide: Guide) -> dict[str, str]:
    """The options of the command on a platform page, per tab."""
    args: dict[str, str] = {}
    for key in guide.run:
        if guide.on_card:
            card = "E:\\" if key == "windows" else "/path/to/card"
            args[key] = f"--platform {name} --dest {card}"
        elif guide.dest:
            args[key] = f"--platform {name} --dest {guide.dest}"
        else:
            args[key] = f"--platform {name}"
    return args


def trust_box(prefix: str, indent: str = "") -> list[str]:
    """Is it safe, under every command on a guide page: the trust items."""
    return markup.details("note", "Is it safe?", [
        *(f"- {item}" for item in TRUST_ITEMS),
        "",
        f"[Read the script first]({prefix}wiki/faq.md#is-piping-the-installer-into-a-shell-safe)",
    ], indent)
