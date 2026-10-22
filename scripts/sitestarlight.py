"""The site as an Astro Starlight project: pages, sidebar and data files.

generate_site.py writes each page body; this module gives it the form
Starlight reads. A page's links are written as the docs tree holds them
(``../systems/sony.md``, MkDocs' convention, the one a Markdown editor
follows), and the site serves them as addresses under /retrobios/, so
each target is resolved against the page's own place here. Every heading
keeps the id the MkDocs site gave it, so the anchors cited since stay
valid.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
import urllib.parse
from html import unescape
from pathlib import Path, PurePosixPath

BASE = "/retrobios/"
SITE_URL = "https://abdess.github.io/retrobios/"

# Components a page body may use, and where each comes from.
COMPONENTS = {
    "Tabs": "@astrojs/starlight/components",
    "TabItem": "@astrojs/starlight/components",
    "FilePicker": "components/FilePicker.astro",
}

# Pages of file cards: level 3 headings by the hundred, so the table of
# contents stops at level 2.
CARD_PAGES = ("systems/", "emulators/", "platforms/")

FENCE = re.compile(r"(^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\2[ \t]*$)", re.M | re.S)
CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
MD_LINK = re.compile(r"(?P<bang>!?)\[(?P<text>(?:[^\[\]\n]|\[[^\]\n]*\])*)\]\((?P<url>[^)\s]+)(?P<title> \"[^\"]*\")?\)")
HTML_URL = re.compile(r"(?P<attr>\b(?:href|src))=\"(?P<url>[^\"]*)\"")
ATTR_BLOCK = re.compile(r"[ \t]*\{(?P<attrs>[^{}]*)\}[ \t]*$")
ID_ATTR = re.compile(r"\bid=\"(?P<id>[^\"]+)\"")
IDCOUNT = re.compile(r"^(.*)_([0-9]+)$")


# Addresses


def page_url(rel: str) -> str:
    """The address of a page, by its path under the docs tree."""
    stem = rel.rsplit(".", 1)[0] if rel.endswith((".md", ".mdx")) else rel
    if stem == "index":
        return BASE
    if stem.endswith("/index"):
        stem = stem[: -len("/index")]
    return f"{BASE}{stem}/"


def resolve(rel_page: str, target: str) -> str:
    """A link target written relative to the page's file, as a site address."""
    if not target or re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", target):
        return target
    path, hash_mark, fragment = target.partition("#")
    path, question, query = path.partition("?")
    parts: list[str] = []
    for piece in (PurePosixPath(rel_page).parent / path).as_posix().split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
        else:
            parts.append(piece)
    joined = "/".join(parts)
    if joined.endswith((".md", ".mdx")):
        url = page_url(joined)
    elif path.endswith("/") or not path:
        url = f"{BASE}{joined}/" if joined else BASE
    else:
        url = f"{BASE}{joined}"
    if question:
        url += f"?{query}"
    return url + (f"#{fragment}" if hash_mark else "")


# Text outside code


def outside_fences(text: str, fn) -> str:
    parts = FENCE.split(text)
    # FENCE has two groups: text, fence, marker, text, ...
    out = []
    for index in range(0, len(parts), 3):
        out.append(fn(parts[index]))
        if index + 1 < len(parts):
            out.append(parts[index + 1])
    return "".join(out)


def outside_code(text: str, fn) -> str:
    def chunk(part: str) -> str:
        out, position = [], 0
        for match in CODE_SPAN.finditer(part):
            out.append(fn(part[position:match.start()]))
            out.append(match.group(0))
            position = match.end()
        out.append(fn(part[position:]))
        return "".join(out)
    return outside_fences(text, chunk)


def rewrite_links(text: str, rel: str) -> str:
    def markdown(match: re.Match) -> str:
        return (f"{match['bang']}[{match['text']}]({resolve(rel, match['url'])}"
                f"{match['title'] or ''})")

    def attribute(match: re.Match) -> str:
        return f'{match["attr"]}="{resolve(rel, match["url"])}"'

    text = outside_fences(text, lambda chunk: MD_LINK.sub(markdown, chunk))
    return outside_fences(text, lambda chunk: HTML_URL.sub(attribute, chunk))


# Heading ids, as Python-Markdown's toc extension gives them


def toc_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)(\{[^}]*\})?", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = unescape(text.replace("`", "").replace("**", "").replace("\\", ""))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _unique(ident: str, used: set[str]) -> str:
    while ident in used or not ident:
        match = IDCOUNT.match(ident)
        ident = f"{match[1]}_{int(match[2]) + 1}" if match else f"{ident}_1"
    used.add(ident)
    return ident


def pin_heading_ids(text: str, title_id: str = "") -> str:
    """Every heading with the id the MkDocs build gave it. *title_id* is
    the page title's, taken first."""
    used: set[str] = {title_id} if title_id else set()
    explicit = set()
    for match in re.finditer(r"\{[^{}\n]*#([^\s}]+)[^{}\n]*\}[ \t]*$", text, re.M):
        explicit.add(match[1])
    for match in ID_ATTR.finditer(text):
        explicit.add(match["id"])
    used |= explicit

    def markdown(match: re.Match) -> str:
        heading = match["mdtext"]
        attrs = ATTR_BLOCK.search(heading)
        body = heading[: attrs.start()] if attrs else heading
        tokens = attrs["attrs"].split() if attrs else []
        if any(token.startswith("#") for token in tokens):
            return match.group(0)
        ident = _unique(toc_slug(body), used)
        return f"{match['hashes']} {body.rstrip()} {{ {' '.join(['#' + ident, *tokens])} }}"

    def raw(match: re.Match) -> str:
        if ID_ATTR.search(match["attrs"]):
            return match.group(0)
        level, text = match["level"], match["htmltext"]
        ident = _unique(toc_slug(text), used)
        return f'<h{level} id="{ident}"{match["attrs"]}>{text}</h{level}>'

    def pick(match: re.Match) -> str:
        return markdown(match) if match["hashes"] else raw(match)

    # In document order: one pass over either kind of heading.
    return outside_fences(text, lambda chunk: _ANY_HEADING.sub(pick, chunk))


_ANY_HEADING = re.compile(
    r"^(?P<hashes>#{2,6})[ \t]+(?P<mdtext>.+?)[ \t]*$"
    r"|<h(?P<level>[2-6])(?P<attrs>(?:\s[^>]*)?)>(?P<htmltext>.*?)</h(?P=level)>",
    re.M | re.S,
)


# Glossary tooltips


def glossary_terms(path: Path) -> dict[str, str]:
    """Each term the glossary defines, with its definition as plain text."""
    terms: dict[str, str] = {}
    if not path.is_file():
        return terms
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"^(?P<term>\*\*.+)\n:   (?P<body>.+(?:\n    .+)*)", text, re.M):
        definition = re.sub(r"\s+", " ", match["body"]).strip()
        definition = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", definition).replace("`", "")
        for term in re.findall(r"\*\*([^*]+)\*\*", match["term"]):
            terms.setdefault(term, definition)
    return terms


_SKIP_TAGS = re.compile(r"<(a|code|pre|abbr|h[1-6]|script|style|summary|svg|figure)\b.*?</\1>", re.S | re.I)


def add_tooltips(text: str, terms: dict[str, str]) -> str:
    """The first use of each glossary term on a page, with its definition
    a hover or a tap away. Headings, links, code and HTML are left as they are."""
    if not terms:
        return text
    pending = dict(terms)
    pattern = re.compile(
        r"(?<![\w/.-])(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        + r")(?![\w/-])"
    )

    def prose(chunk: str) -> str:
        if not pending:
            return chunk
        out = []
        for line in chunk.split("\n"):
            stripped = line.lstrip()
            if (not pending or stripped.startswith(("#", "|", "<", ":::", "[", "!"))
                    or not stripped):
                out.append(line)
                continue
            out.append(_tooltip_line(line, pattern, pending))
        return "\n".join(out)

    return outside_code(text, prose)


def _tooltip_line(line: str, pattern: re.Pattern, pending: dict[str, str]) -> str:
    # Inline HTML, links and emphasis markers stay whole.
    protected = re.compile(r"<[^>]+>|\[[^\]]*\]\([^)]*\)|\*\*[^*]+\*\*|\{[^}]*\}")
    out, position = [], 0
    for match in protected.finditer(line):
        out.append(_tooltip_text(line[position:match.start()], pattern, pending))
        out.append(match.group(0))
        position = match.end()
    out.append(_tooltip_text(line[position:], pattern, pending))
    return "".join(out)


def _tooltip_text(text: str, pattern: re.Pattern, pending: dict[str, str]) -> str:
    def swap(match: re.Match) -> str:
        term = match.group(1)
        if term not in pending:
            return term
        definition = pending.pop(term)
        return f'<abbr class="rb-term" title="{html.escape(definition)}">{term}</abbr>'
    return pattern.sub(swap, text)


# MDX


_VOID = re.compile(r"<(br|img|input|hr|source|meta|link|wbr)\b([^>]*?)(?<!/)>")
_HEADING_ATTRS = re.compile(r"(?m)(?<=\S)[ \t]+\{[ \t]*#[^{}\n]*\}[ \t]*$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def mdx_safe(text: str) -> str:
    """What MDX would read as code in prose: braces, a stray ``<``,
    comments, unclosed void tags and ``<https://...>`` links."""
    def fix(chunk: str) -> str:
        chunk = _COMMENT.sub("", chunk)
        chunk = re.sub(r"<(https?://[^>\s]+)>", r"[\1](\1)", chunk)
        held: list[str] = []

        def hold(match: re.Match) -> str:
            held.append(match.group(0))
            return f"\x00{len(held) - 1}\x00"
        chunk = _HEADING_ATTRS.sub(hold, chunk)
        chunk = chunk.replace("{", "&#123;").replace("}", "&#125;")
        chunk = re.sub(r"\x00(\d+)\x00", lambda m: held[int(m[1])], chunk)
        chunk = re.sub(r"<(?![A-Za-z/])", "&lt;", chunk)
        return _VOID.sub(lambda m: f"<{m[1]}{m[2].rstrip()} />", chunk)
    return outside_code(text, fix)


def used_components(body: str) -> list[str]:
    return [name for name in COMPONENTS if re.search(rf"<{name}[\s/>]", body)]


# Pages


def _front_matter(data: dict) -> str:
    import yaml

    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n"


def og_section(rel: str, level: str) -> str:
    """The sharing card a page takes: its level, or what its address says."""
    url = page_url(rel)[len(BASE):]
    if not url:
        return "home"
    if level in ("start", "setup", "explore", "reference", "contribute"):
        return level
    if url.startswith(("which-pack", "wiki/first-install", "wiki/faq", "wiki/getting-started")):
        return "start"
    if url.startswith("platforms"):
        return "setup"
    if url.startswith(("systems", "emulators", "files")):
        return "explore"
    if url.startswith(("contributing", "wiki/adding", "wiki/profiling", "wiki/testing", "wiki/release")):
        return "contribute"
    return "reference"


def page_head(rel: str, level: str, title: str, browser_title: str, structured: dict) -> list[dict]:
    """What a page adds to its head: its own window title, the sharing
    card of its section, and its structured data."""
    section = og_section(rel, level)
    image = f"{SITE_URL}assets/og/{section}.png"
    head: list[dict] = []
    if browser_title != title:
        head.append({"tag": "title", "content": f"{browser_title} - RetroBIOS"})
    head += [
        {"tag": "meta", "attrs": {"property": "og:image", "content": image}},
        {"tag": "meta", "attrs": {"property": "og:image:width", "content": "1200"}},
        {"tag": "meta", "attrs": {"property": "og:image:height", "content": "630"}},
        {"tag": "meta", "attrs": {"property": "og:image:alt", "content": f"RetroBIOS: {browser_title}"}},
        {"tag": "meta", "attrs": {"name": "twitter:image", "content": image}},
        {"tag": "script", "attrs": {"type": "application/ld+json"},
         "content": json.dumps(structured, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")},
    ]
    return head


def split_title(markdown: str) -> tuple[str, str, str]:
    """The page's H1 as plain text, the id MkDocs gave it, and the body
    without it."""
    match = re.search(r"^# (.+?)[ \t]*$\n?", markdown, re.M)
    if not match:
        return "", "", markdown
    explicit = re.search(r"\{[^}]*#([^\s}]+)[^}]*\}\s*$", match[1])
    title = re.sub(r"\s*\{[^}]*\}\s*$", "", match[1])
    ident = explicit[1] if explicit else toc_slug(title)
    title = re.sub(r"<[^>]+>", "", title)
    title = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", title)
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "")
    return unescape(title).strip(), ident, markdown[: match.start()] + markdown[match.end():]


def finalize_page(
    rel: str,
    markdown: str,
    *,
    level: str,
    boost: int | None,
    browser_title: str,
    description: str,
    structured: dict,
    terms: dict[str, str],
    search: bool = True,
) -> tuple[str, str]:
    """The Starlight file for a page body: its path under the content
    collection and its text."""
    title, title_id, body = split_title(markdown)
    body = body.lstrip("\n")
    body = rewrite_links(body, rel)
    body = pin_heading_ids(body, title_id)
    if title_id:
        # The title is the theme's, with an id of its own: the one MkDocs
        # gave it stays an anchor at the top of the text.
        body = f'<div id="{html.escape(title_id)}" class="rb-anchor"></div>\n\n' + body
    if rel not in ("wiki/glossary.md", "wiki/reference-terms.md"):
        body = add_tooltips(body, terms)
    components = used_components(body)
    stem = rel.rsplit(".", 1)[0]
    if components:
        body = mdx_safe(body)
        imports: dict[str, list[str]] = {}
        for name in components:
            imports.setdefault(COMPONENTS[name], []).append(name)
        # Our own components are imported from src/, two levels above the
        # content collection plus the page's own folders.
        up = "../" * (rel.count("/") + 2)
        body = "".join(f"import {{ {', '.join(names)} }} from '{source}';\n"
                       if source.startswith("@") else f"import {names[0]} from '{up}{source}';\n"
                       for source, names in imports.items()) + "\n" + body
    front: dict = {"title": title or browser_title, "description": description}
    # Starlight's slugger drops dots and plus signs: the address stays MkDocs'.
    if re.search(r"[^a-z0-9_/-]", stem):
        front["slug"] = stem
    if level:
        front["level"] = level
    if boost:
        front["boost"] = boost
    if rel.startswith(CARD_PAGES):
        front["tableOfContents"] = {"minHeadingLevel": 2, "maxHeadingLevel": 2}
    if not search:
        front["pagefind"] = False
    front["head"] = page_head(rel, level, front["title"], browser_title, structured)
    if structured.get("dateModified"):
        front["lastmod"] = str(structured["dateModified"])
    return stem + (".mdx" if components else ".md"), _front_matter(front) + body


# Material syntax in the hand-written wiki pages, read until they are
# written for Starlight.

_WIKI_ADMONITION = re.compile(r'^(?P<indent> *)(?P<kind>!!!|\?\?\?\+?) *(?P<type>[a-z]+)(?: +"(?P<title>[^"]*)")? *$')
_WIKI_TAB = re.compile(r'^(?P<indent> *)=== +"(?P<label>[^"]+)" *$')
_WIKI_BUTTON = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+)\)\{ *(?P<attrs>[^}]*)\}")


def _indented_block(lines: list[str], start: int, indent: int) -> tuple[list[str], int]:
    body: list[str] = []
    index = start
    pad = " " * (indent + 4)
    while index < len(lines) and (not lines[index].strip() or lines[index].startswith(pad)):
        body.append(lines[index][indent + 4:] if lines[index].startswith(pad) else "")
        index += 1
    while body and not body[-1].strip():
        body.pop()
        index -= 1
    return body, index


def wiki_page(text: str) -> str:
    """A wiki page in Material syntax, its blocks written for Starlight."""
    import sitemarkup as markup

    def blocks(lines: list[str]) -> list[str]:
        out: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            admonition = _WIKI_ADMONITION.match(line)
            tab = _WIKI_TAB.match(line)
            if admonition:
                indent = len(admonition["indent"])
                body, index = _indented_block(lines, index + 1, indent)
                title = admonition["title"] if admonition["title"] is not None else admonition["type"].capitalize()
                pad = " " * indent
                if admonition["kind"] == "!!!":
                    out += markup.aside(admonition["type"], title, blocks(body), pad)
                else:
                    out += markup.details(admonition["type"], title, blocks(body), pad,
                                          opened=admonition["kind"].endswith("+"))
                continue
            if tab:
                indent = len(tab["indent"])
                items = []
                while index < len(lines):
                    tab = _WIKI_TAB.match(lines[index])
                    if not tab or len(tab["indent"]) != indent:
                        break
                    body, index = _indented_block(lines, index + 1, indent)
                    items.append((tab["label"], blocks(body)))
                    while index < len(lines) and not lines[index].strip() and index + 1 < len(lines) \
                            and _WIKI_TAB.match(lines[index + 1]):
                        index += 1
                out += markup.tabs(items, " " * indent)
                continue
            out.append(line)
            index += 1
        return out

    def button(match: re.Match) -> str:
        classes = match["attrs"].split()
        if ".md-button" not in classes:
            return match.group(0)
        return markup.button(match["text"], match["url"], primary=".md-button--primary" in classes)

    text = "\n".join(blocks(text.split("\n")))
    return outside_fences(text, lambda chunk: _WIKI_BUTTON.sub(button, chunk))


# Navigation


def _sidebar_link(rel: str) -> str:
    return page_url(rel)[len(BASE) - 1:]


def sidebar(nav: list, present: set[str]) -> dict:
    """The navigation as Starlight topics, one per tab.

    A page the navigation does not list (the parts of a split page) joins
    the topic that holds most pages of its directory.
    """
    owner: dict[str, str] = {}

    def item(entry, topic: str) -> dict | None:
        if isinstance(entry, str):
            owner[entry] = topic
            return {"label": "Overview", "link": _sidebar_link(entry)} if entry in present else None
        (label, value), = entry.items()
        if isinstance(value, str):
            if value.startswith("http"):
                return {"label": label, "link": value}
            owner[value] = topic
            return {"label": label, "link": _sidebar_link(value)} if value in present else None
        children = [child for child in (item(v, topic) for v in value) if child]
        return {"label": label, "items": children, "collapsed": True} if children else None

    topics = []
    for tab in nav:
        (label, value), = tab.items()
        topic = re.sub(r"[^a-z]+", "-", label.lower()).strip("-")
        children = [child for child in (item(v, topic) for v in value) if child]
        if children:
            topics.append({"id": topic, "label": label, "link": next(_links(children))["link"],
                           "items": children})

    votes: dict[str, dict[str, int]] = {}
    for rel, topic in owner.items():
        tally = votes.setdefault(rel.rpartition("/")[0], {})
        tally[topic] = tally.get(topic, 0) + 1
    extra: dict[str, list[str]] = {}
    # The page a stale link lands on belongs to no tab.
    exclude: list[str] = ["/404"]
    for rel in sorted(present - owner.keys()):
        tally = votes.get(rel.rpartition("/")[0])
        path = "/" + (_sidebar_link(rel).strip("/") or "index")
        if tally:
            extra.setdefault(max(tally, key=tally.get), []).append(path)
        else:
            exclude.append(path)
    return {"topics": topics, "options": {"topics": extra, "exclude": exclude}}


def _links(items: list[dict]):
    for entry in items:
        if "link" in entry:
            yield entry
        else:
            yield from _links(entry["items"])


# The picker's data


def picker_data(install: Path, out: Path) -> int:
    """Each manifest, compacted to what the file picker reads: per file its
    destination, size and systems, the systems install.py --system accepts.
    """
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for manifest in sorted(install.glob("*.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "files" not in data or "platform" not in data:
            continue
        systems = sorted({s for f in data["files"] for s in f.get("systems") or []})
        position = {s: k for k, s in enumerate(systems)}
        names = data.get("system_names") or {}
        payload = {
            "platform": data["platform"],
            "name": data.get("display_name", data["platform"]),
            "folder": (data.get("detect") or [{}])[0].get("bios_path", ""),
            "generated": data.get("generated", ""),
            "systems": [{"id": s, "native": names.get(s, [])} for s in systems],
            "other": any(not f.get("systems") for f in data["files"]),
            "files": [[f["dest"], f.get("size", 0), [position[s] for s in f.get("systems") or []]]
                      for f in sorted(data["files"], key=lambda f: f["dest"])],
        }
        (out / f"{data['platform']}.json").write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        index.append({"platform": data["platform"], "name": payload["name"],
                      "files": len(payload["files"])})
    index.sort(key=lambda entry: entry["name"].casefold())
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    return len(index)


def quote_url(url: str) -> str:
    return urllib.parse.quote(url, safe="/:#?=&%")
