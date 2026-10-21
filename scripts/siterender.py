"""Small renderings shared by every page."""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from artifacts import write_text_atomic

WIKI_SRC_DIR = "wiki"  # manually maintained wiki sources
SYSTEM_ICON_BASE = "https://raw.githubusercontent.com/libretro/retroarch-assets/master/xmb/systematic/png"
ICON_CACHE_PATH = Path(".cache") / "system_icons.json"

# Icon names confirmed to exist upstream. A name absent from this map has not
# been checked yet. A name mapped to False has no icon and gets none rendered,
# because a heading with a broken image reads worse than a heading without one.
_icon_available: dict[str, bool] = {}

_CODE_SPAN = re.compile(r"(`+).+?\1", re.S)


def _code(value: object) -> str:
    """A value as an inline code span it cannot leave.

    Markdown shows HTML inside a code span as text, so the one way out is a
    backtick in the value closing the span early. Data from a profile, a
    platform file or a file name is not ours to trust that far.
    """
    return "`" + str(value).replace("`", "'") + "`"


def _count(number: int, noun: str, plural: str = "") -> str:
    """A count and its noun, singular for one: the site printed "1 files" 234 times."""
    return f"{number:,} {noun if number == 1 else plural or noun + 's'}"


def _escape_tags(text: str) -> str:
    """Show a placeholder such as ``<system_dir>`` as it was written.

    Profile prose names paths with angle brackets. Passed through as HTML
    they are unknown tags and vanish from the page, and ``<title id>`` opens
    a title element that swallows everything after it. Code spans keep their
    brackets, and an autolink stays a link.
    """
    def outside(chunk: str) -> str:
        return re.sub(r"<(?!https?://|mailto:)", "&lt;", chunk)

    out: list[str] = []
    position = 0
    for match in _CODE_SPAN.finditer(text):
        out.append(outside(text[position:match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(outside(text[position:]))
    return "".join(out)


def _admonition_body(text: str) -> str:
    """Indent prose without turning source tokens such as ``#if`` into H1s."""
    escaped = re.sub(r"(?m)^(\s*)#", r"\1\\#", _escape_tags(text))
    return escaped.replace("\n", "\n    ")

def _icon_name(manufacturer: str, console_name: str) -> str:
    return f"{manufacturer} - {console_name}".replace("/", " ")

def _icon_url(icon_name: str) -> str:
    return f"{SYSTEM_ICON_BASE}/{urllib.parse.quote(icon_name)}.png"

def _read_icon_cache() -> dict[str, bool]:
    """The recorded icon checks, empty when none were recorded.

    An unreadable file is reported, not taken for an empty cache in silence.
    """
    try:
        text = ICON_CACHE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        print(f"  WARNING: cannot read {ICON_CACHE_PATH}: {exc}", file=sys.stderr)
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  WARNING: {ICON_CACHE_PATH} is not valid JSON ({exc}), "
              "icons are checked again", file=sys.stderr)
        return {}
    return {str(k): bool(v) for k, v in data.items()} if isinstance(data, dict) else {}


def prime_system_icons(names: set[str]) -> None:
    """Record which system icons upstream actually serves.

    Results persist in ``.cache`` so later runs skip the network. Names that
    cannot be checked stay unavailable: the site never links an image it has
    not seen answer.
    """
    cached = _read_icon_cache()
    unknown = sorted(n for n in names if n not in cached)
    if unknown:
        def check(name: str) -> tuple[str, bool | None]:
            """True when served, False when upstream says it is gone.

            None on a transient failure, so a flaky run never records an
            icon as absent for every later build.
            """
            req = urllib.request.Request(_icon_url(name), method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return name, resp.status == 200
            except urllib.error.HTTPError as exc:
                return name, False if exc.code == 404 else None
            except (urllib.error.URLError, OSError):
                return name, None

        print(f"Checking {len(unknown)} system icons...")
        learned: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for name, ok in pool.map(check, unknown):
                if ok is not None:
                    learned[name] = ok
        cached.update(learned)
        if learned:
            # Another run may have recorded names meanwhile: merge with what
            # the file holds now, and replace it whole so a reader never sees
            # a half-written cache.
            merged = {**_read_icon_cache(), **learned}
            ICON_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(
                str(ICON_CACHE_PATH),
                json.dumps(merged, indent=2, sort_keys=True) + "\n",
            )

    _icon_available.update(cached)
    missing = sum(1 for n in names if not cached.get(n))
    if missing:
        print(f"  {missing} systems have no upstream icon")

def system_icon_markdown(manufacturer: str, console_name: str) -> str:
    """Icon image for a system heading, empty when upstream serves none."""
    name = _icon_name(manufacturer, console_name)
    if not _icon_available.get(name):
        return ""
    return f"![{console_name}]({_icon_url(name)}){{ width=24 }} "

# Global index: maps system_id -> (manufacturer_slug, console_name) for cross-linking
_system_page_map: dict[str, tuple[str, str]] = {}

def _slugify_anchor(text: str) -> str:
    """Slugify text for MkDocs anchor compatibility."""
    import re

    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = slug.strip("-")
    return slug

_norm_page_map: dict[str, tuple[str, str] | None] = {}


def set_system_page_map(mapping: dict[str, tuple[str, str]]) -> None:
    """Install the system id -> page table every link on the site reads.

    The table replaces the previous one, and the index of other spellings is
    derived from it here. Kept anywhere else, it outlived a table of the same
    size and linked to the pages of the previous one.
    """
    from common import _norm_system_id

    _system_page_map.clear()
    _system_page_map.update(mapping)
    _norm_page_map.clear()
    for known, page in _system_page_map.items():
        key = _norm_system_id(known)
        if _norm_page_map.get(key, page) != page:
            _norm_page_map[key] = None
        else:
            _norm_page_map[key] = page


def _page_by_norm(sys_id: str) -> tuple[str, str] | None:
    """The page an id leads to under another spelling of the same system.

    Emulator cards name sega-megadrive or sinclair-zxspectrum, the pages are
    keyed by the platforms' spellings: 2,431 of 4,195 cards had no link. A
    spelling two pages share leads nowhere rather than to either.
    """
    from common import _norm_system_id

    return _norm_page_map.get(_norm_system_id(sys_id))


def _system_link(sys_id: str, prefix: str = "") -> str:
    """Generate a markdown link to a system page with anchor."""
    page = _system_page_map.get(sys_id) or _page_by_norm(sys_id)
    if page:
        slug, console = page
        anchor = _slugify_anchor(console)
        return f"[{sys_id}]({prefix}systems/{slug}.md#{anchor})"
    return sys_id

def _field_text(key: object, value: object) -> str:
    """A scalar of a structured block. A source reference reads as code."""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif value is None:
        text = "none"
    else:
        # One line: a folded note's second line started at column 0 and
        # left the admonition it was written in. Never cut: 34 notes lost
        # their end, and a cut citation links to another line.
        text = " ".join(str(value).split())
    return f"`{text}`" if key == "source_ref" else text


def _field_label(key: object) -> str:
    """A YAML key as the page shows it: words, not an identifier."""
    return str(key).replace("_", " ")


FieldText = Callable[[object, object], str]


def _render_yaml_value(
    lines: list[str], val: object, indent: int = 4,
    field_text: FieldText = _field_text,
) -> None:
    """Render any YAML value as a nested markdown list.

    Every level is a list item and its children a sublist four spaces in.
    A key written as a paragraph with its children indented below it read as
    an indented code block: the clownmdemu page showed the raw asterisks.
    ``field_text`` renders each scalar, so a page can link a source_ref.
    """
    pad = " " * indent
    if isinstance(val, dict):
        for k, v in val.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}- **{_field_label(k)}:**")
                lines.append("")
                _render_yaml_value(lines, v, indent + 4, field_text)
            else:
                lines.append(f"{pad}- **{_field_label(k)}:** {field_text(k, v)}")
        lines.append("")
    elif isinstance(val, list):
        for item in val:
            if isinstance(item, dict):
                # Scalars on the item's line, lists and maps as its children:
                # cemu's firmware titles lost the files each title carries.
                scalars = {ik: iv for ik, iv in item.items()
                           if not (isinstance(iv, (dict, list)) and iv)}
                nested = {ik: iv for ik, iv in item.items() if ik not in scalars}
                parts = [f"{_field_label(ik)}: {field_text(ik, iv)}"
                         for ik, iv in scalars.items()]
                lines.append(f"{pad}- {', '.join(parts)}".rstrip())
                if nested:
                    lines.append("")
                    _render_yaml_value(lines, nested, indent + 4, field_text)
            else:
                lines.append(f"{pad}- {field_text(None, item)}")
        lines.append("")
    elif isinstance(val, str) and "\n" in val:
        for line in val.split("\n"):
            lines.append(f"{pad}{line}")
    else:
        lines.append(f"{pad}{field_text(None, val)}")


def _platform_link(name: str, display: str, prefix: str = "") -> str:
    """Generate a markdown link to a platform page."""
    return f"[{display}]({prefix}platforms/{name}.md)"

def _emulator_link(name: str, prefix: str = "") -> str:
    """Generate a markdown link to an emulator page."""
    return f"[{name}]({prefix}emulators/{name}.md)"

def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024**3):.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024**2):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"

def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{n / total * 100:.1f}%"

def _by_mode(value: str | dict, mode: str) -> str:
    """Read a profile field that may be keyed by build mode."""
    if isinstance(value, dict):
        return str(value.get(mode) or "") if mode else ""
    return str(value or "")
