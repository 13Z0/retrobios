"""Small renderings shared by every page."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from pathlib import Path


WIKI_SRC_DIR = "wiki"  # manually maintained wiki sources
SYSTEM_ICON_BASE = "https://raw.githubusercontent.com/libretro/retroarch-assets/master/xmb/systematic/png"
# Local copies of the upstream icons, named as upstream names them.
ICON_DIR = Path(__file__).resolve().parent.parent / "docs_assets" / "system-icons"

# Icon names the site holds a copy of. A name mapped to False has none.
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

def prime_system_icons(names: set[str]) -> None:
    """Record which system icons the site holds a copy of.

    The icons are copied once into ``docs_assets/system-icons/`` by
    ``scripts/fetch_site_images.py``: the build reads that folder and never
    the network. A system without a copy gets no icon, because a heading
    with a broken image reads worse than a heading without one.
    """
    held = {path.stem for path in ICON_DIR.glob("*.png")}
    _icon_available.update({name: name in held for name in names})
    missing = sum(1 for name in names if name not in held)
    if missing:
        print(f"  {missing} systems have no icon in {ICON_DIR.relative_to(ICON_DIR.parents[1])}")

def system_icon_markdown(manufacturer: str, console_name: str) -> str:
    """Icon image for a system heading, empty when the site holds none.

    The link names the upstream file: ``sitediagrams.localize_images``
    points it at the local copy, whose source is recorded beside it.
    """
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


# A manufacturer page too large for one page is a summary, each console on
# pages of its own: (slug, console) -> its page stems under systems/, and
# (slug, console, sha1) -> the stem holding that card. A console of a page
# kept whole has no entry, its section is on the manufacturer's page.
_console_pages: dict[tuple[str, str], list[str]] = {}
_card_pages: dict[tuple[str, str, str], str] = {}


def reset_console_pages() -> None:
    _console_pages.clear()
    _card_pages.clear()


def record_console_pages(
    slug: str, console: str, stems: list[str], sha1s: list[list[str]]
) -> None:
    """Where a split manufacturer page put one console: its page stems, in
    order, and the sha1 of each card on each of them."""
    _console_pages[(slug, console)] = list(stems)
    for stem, part in zip(stems, sha1s):
        for sha1 in part:
            _card_pages.setdefault((slug, console, sha1), stem)


def split_system_stems() -> set[str]:
    """Every page stem of a console moved to a page of its own."""
    return {stem for stems in _console_pages.values() for stem in stems}


def console_href(slug: str, console: str, prefix: str = "") -> str:
    """Where a console's files are: its own page, or its section of the
    manufacturer's page (also where a console cut into parts lists them)."""
    stems = _console_pages.get((slug, console))
    if stems and len(stems) == 1:
        return f"{prefix}systems/{urllib.parse.quote(stems[0])}.md"
    return f"{prefix}systems/{urllib.parse.quote(slug)}.md#{_slugify_anchor(console)}"


def card_page(path: str, sha1: str) -> str:
    """The systems/ page stem holding the card of the copy at ``path``
    (bios/<Manufacturer>/<Console>/...)."""
    parts = path.split("/")
    slug = parts[1].lower().replace(" ", "-")
    console = parts[2] if len(parts) > 2 else ""
    return _card_pages.get((slug, console, sha1), slug)


# Emulator cards moved off a page too long for one: (emulator, anchor) ->
# the page stem under emulators/ that holds the card.
_emulator_card_pages: dict[tuple[str, str], str] = {}


def record_emulator_cards(emulator: str, stem: str, anchors: list[str]) -> None:
    for anchor in anchors:
        _emulator_card_pages[(emulator, anchor)] = stem


def emulator_card_page(emulator: str, anchor: str) -> str:
    """The emulators/ page stem holding a card: the emulator's own page, or
    the page its cards went to."""
    return _emulator_card_pages.get((emulator, anchor), emulator)


def _system_link(sys_id: str, prefix: str = "") -> str:
    """Generate a markdown link to a system page with anchor."""
    page = _system_page_map.get(sys_id) or _page_by_norm(sys_id)
    if page:
        slug, console = page
        return f"[{sys_id}]({console_href(slug, console, prefix)})"
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
