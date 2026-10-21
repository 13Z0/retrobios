"""Permalinks into the sources a profile cites.

A citation is only checkable if it points at the revision the profile
was written against, so a permalink is pinned, never guessed."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request

import upstream
from siterender import _by_mode
from sourceref import _prose_runs, ref_role, source_ref_values, split_source_ref


def _repo_pins(profile: dict, field: str, label: str) -> list[tuple[str, str, str]]:
    """Pair each declared repository URL with its own revision.

    A profile whose builds live in separate repositories keys both the URL
    and the revision by build mode. Reading them apart would pin a libretro
    fork to the standalone revision and produce a permalink into a tree that
    never held the cited line. Each tuple is (url, pin, source_commit
    fallback for that same mode).
    """
    raw = profile.get(field, "")
    pin_field = profile.get(f"{field}_commit") or ""
    source_field = profile.get("source_commit") or ""

    if not isinstance(raw, dict):
        if not raw:
            return []
        return [
            (
                str(raw),
                _by_mode(pin_field, label),
                _by_mode(source_field, label),
            )
        ]

    # The requested build mode first, so it wins when several repositories
    # would otherwise be equally good candidates.
    ordered = ([label] if label and label in raw else []) + [
        key for key in raw if key != label
    ]
    return [
        (str(raw[key]), _by_mode(pin_field, key), _by_mode(source_field, key))
        for key in ordered
        if raw.get(key)
    ]

# Forges the pages link into but upstream.py does not read from. Each is
# reached by browser only, so the Repo carries no API or raw base.
_BROWSE_ONLY = {
    "voidpoint.io": "gitlab",
    "bitbucket.org": "bitbucket",
    "sourceforge.net": "sourceforge",
    "archive.softwareheritage.org": "swh",
}


def _browse_repo(url: str) -> upstream.Repo | None:
    """The repository a profile URL names, on any forge the pages can link.

    Profiles pinned on SourceForge, voidpoint.io, Bitbucket or Software
    Heritage showed no revision and no linked citation: parse_repo knows
    only the forges profile_sync fetches from.
    """
    repo = upstream.parse_repo(url)
    if repo is not None or not url:
        return repo
    parts = urllib.parse.urlsplit(url.strip())
    family = _BROWSE_ONLY.get(parts.netloc)
    segments = [seg for seg in parts.path.split("/") if seg]
    if family == "gitlab":
        if "-" in segments:
            segments = segments[: segments.index("-")]
        if len(segments) < 2:
            return None
        owner, name = "/".join(segments[:-1]), segments[-1]
    elif family == "bitbucket" and len(segments) >= 2:
        owner, name = segments[0], segments[1]
    elif family == "sourceforge" and len(segments) >= 3 and segments[0] == "p":
        owner, name = segments[1], segments[2]
    elif family == "swh":
        # A revision URL names no project: the pin alone addresses the tree.
        owner, name = "softwareheritage", "revision"
    else:
        return None
    return upstream.Repo(parts.netloc, family, "", "", owner, name.removesuffix(".git"))


def _forge_sources(profile: dict, label: str = "") -> list[tuple[upstream.Repo, str]]:
    """Supported source repositories and immutable revisions for a profile."""
    candidates: list[tuple[upstream.Repo, str]] = []
    seen: set[tuple[str, str, str]] = set()
    source_repos: set[tuple[str, str]] = set()

    for field in ("source", "upstream"):
        for value, pin, fallback in _repo_pins(profile, field, label):
            repo = _browse_repo(value)
            if repo is None:
                continue
            repo_key = (repo.host, repo.slug)
            if field == "upstream" and not pin and repo_key in source_repos:
                pin = fallback
            if not pin:
                continue
            key = (repo.host, repo.slug, pin)
            if key not in seen:
                candidates.append((repo, pin))
                seen.add(key)
            if field == "source":
                source_repos.add(repo_key)
    return candidates

def _source_permalink(repo: upstream.Repo, pin: str, path: str,
                      start: int | None, end: int | None) -> str:
    """Forge-specific browser URL for one file at one immutable revision."""
    quoted_path = urllib.parse.quote(path, safe="/@:+-._~")
    base = f"https://{repo.host}/{repo.owner}/{repo.name}"
    if repo.family == "bitbucket":
        url = f"{base}/src/{pin}/{quoted_path}"
        if start is not None:
            url += f"#lines-{start}" + (f":{end}" if end is not None and end != start else "")
        return url
    if repo.family == "sourceforge":
        # The Allura code browser takes no line range: the file at the pin.
        return f"https://{repo.host}/p/{repo.owner}/{repo.name}/ci/{pin}/tree/{quoted_path}"
    if repo.family == "swh":
        url = f"https://{repo.host}/browse/revision/{pin}/?path={quoted_path}"
        if start is not None:
            url += f"#L{start}" + (f"-L{end}" if end is not None and end != start else "")
        return url
    if repo.family == "github":
        url = f"{base}/blob/{pin}/{quoted_path}"
    elif repo.family == "gitlab":
        url = f"{base}/-/blob/{pin}/{quoted_path}"
    else:
        url = f"{base}/src/commit/{pin}/{quoted_path}"
    if start is not None:
        if repo.family == "gitlab":
            url += f"#L{start}"
            if end is not None and end != start:
                url += f"-{end}"
        else:
            url += f"#L{start}"
            if end is not None and end != start:
                url += f"-L{end}"
    return url

def _commit_url(repo: upstream.Repo, pin: str) -> str:
    """Forge-specific browser URL for one commit."""
    base = f"https://{repo.host}/{repo.owner}/{repo.name}"
    if repo.family == "gitlab":
        return f"{base}/-/commit/{pin}"
    if repo.family == "bitbucket":
        return f"{base}/commits/{pin}"
    if repo.family == "sourceforge":
        return f"https://{repo.host}/p/{repo.owner}/{repo.name}/ci/{pin}/"
    if repo.family == "swh":
        return f"https://{repo.host}/browse/revision/{pin}/"
    return f"{base}/commit/{pin}"


def revision_markdown(profile: dict) -> str:
    """Each repository the profile reads, linked at the commit it was read at.

    The page linked the repositories unpinned: the revision every citation
    refers to sat in the YAML alone, for 178 profiles.
    """
    links = [
        f"[{'Software Heritage' if repo.family == 'swh' else repo.slug}@{pin[:12]}]"
        f"({_commit_url(repo, pin)})"
        for repo, pin in _forge_sources(profile)
    ]
    branch = profile.get("source_branch")
    if branch and not isinstance(branch, dict):
        links.append(f"branch `{branch}`")
    elif isinstance(branch, dict):
        links += [f"{mode} branch `{name}`" for mode, name in branch.items()]
    return ", ".join(links)


_GLUE_RE = re.compile(r"(^|/)(libretro|retro_)[^/]*$|(^|/)libretro[^/]*/", re.IGNORECASE)


def role_label(profile: dict, role: str, path: str = "") -> str:
    """Whose code a cited path is, said the way a reader asks it.

    A libretro port carries the emulator's own sources beside its glue: pcem
    cites src/mem.c in the port, which is PCem's code, not the core's.
    """
    if role == "upstream":
        return "upstream emulator"
    if "libretro" not in str(profile.get("type", "")):
        return "this build"
    if _GLUE_RE.search(path):
        return "libretro glue"
    return "emulator code, libretro port"


def role_sources(profile: dict, label: str = "") -> dict[str, tuple[upstream.Repo, str]]:
    """The forge repository and pin behind each role a citation can name."""
    found: dict[str, tuple[upstream.Repo, str]] = {}
    for field in ("source", "upstream"):
        for value, pin, _fallback in _repo_pins(profile, field, label):
            repo = _browse_repo(value)
            if repo is not None and pin and field not in found:
                found[field] = (repo, pin)
    return found


def requested_by(profile: dict, value: str | dict | None) -> str:
    """Whose code the citations of an entry are in, said in words."""
    labels = []
    for _label, refs in source_ref_values(value):
        for part in split_source_ref(refs):
            role, bare = ref_role(part.path)
            if role:
                label = role_label(profile, role, bare)
                if label not in labels:
                    labels.append(label)
    if not labels or not _two_repositories(profile):
        return ""
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" and {labels[-1]}"


def _two_repositories(profile: dict) -> bool:
    source, upstream_url = profile.get("source"), profile.get("upstream")
    return bool(source and upstream_url and source != upstream_url)


def _source_ref_markdown(profile: dict, value: str | dict | None) -> str:
    """Render source_ref values as pinned links when their forge is known.

    A profile can declare two repositories: the libretro port in ``source`` and
    the original emulator in ``upstream``. Which of the two carries a given
    path cannot be decided without reading their trees, and this generator runs
    offline. Rather than guess, an unattributable path is rendered as plain
    code: a citation with no link still names the file and the lines, while a
    link to the wrong repository is a false citation.
    """
    rendered_groups: list[str] = []
    path_re = re.compile(r"^[A-Za-z0-9_.@/+~-]+$")
    for label, refs in source_ref_values(value):
        candidates = _forge_sources(profile, label)
        by_role = role_sources(profile, label)
        rendered: list[str] = []
        current_role = ""
        for part in split_source_ref(refs):
            role, bare = ref_role(part.path)
            if bare != part.path or role:
                current_role = role
            elif part.raw and not part.raw[:1].isdigit():
                current_role = ""
            part = type(part)(bare, part.start, part.end, part.raw)
            display = part.path
            if part.start is not None:
                display += f":{part.start}"
                if part.end is not None and part.end != part.start:
                    display += f"-{part.end}"

            # The path is linked as written. A first segment equal to a
            # repository's name proves nothing: PCSX2 keeps its sources in
            # pcsx2/, hatariB vendors Hatari in hatari/, and stripping it
            # broke 470 links. Two repositories and no tree to read: no link.
            selected: tuple[upstream.Repo, str] | None = None
            real_path = part.path
            if path_re.fullmatch(part.path):
                if current_role:
                    # Only the repository the part names, or no link at all.
                    selected = by_role.get(current_role)
                elif len(candidates) == 1:
                    selected = candidates[0]

            tag = (f" ({role_label(profile, current_role, part.path)})"
                   if current_role and _two_repositories(profile) else "")
            if selected is None:
                rendered.append(f"`{display}`{tag}")
            else:
                repo, pin = selected
                url = _source_permalink(
                    repo, pin, real_path, part.start, part.end
                )
                rendered.append(f"[`{display}`]({url}){tag}")

        text = ", ".join(rendered) if rendered else f"`{refs}`"
        if label:
            text = f"**{label}:** {text}"
        rendered_groups.append(text)
    return ", ".join(rendered_groups)


def link_prose_citations(profile: dict, text: str) -> str:
    """Prose file:line citations as pinned links, where the target is certain.

    profile_sync keeps these citations on their pin, and the page printed
    4,328 of them as plain text. A citation links only when the profile reads
    one repository and the path is rooted (it holds a directory) or is one a
    structured source_ref of the profile already uses: a bare name could be
    anywhere in the tree, and a link to the wrong file is a false citation.
    Text already in backticks or in a link is left alone.
    """
    candidates = _forge_sources(profile)
    if len(candidates) != 1 or ":" not in text:
        return text
    repo, pin = candidates[0]
    structured = {
        part.path
        for entry in profile.get("files") or []
        if isinstance(entry, dict)
        for _label, refs in source_ref_values(entry.get("source_ref"))
        for part in split_source_ref(refs)
    }
    out = []
    for line in text.split("\n"):
        guarded = _guarded_spans(line)
        edits = []
        for _n, path_span, spans, parts, _run, wrapped in _prose_runs(line):
            path = parts[0].path
            if wrapped or " " in path or not _PATH_RE.fullmatch(path):
                continue
            if "/" not in path and path not in structured:
                continue
            if any(lo <= path_span[0] < hi for lo, hi in guarded):
                continue
            for index, ((lo, hi), part) in enumerate(zip(spans, parts)):
                start = path_span[0] if index == 0 else lo
                url = _source_permalink(repo, pin, part.path, part.start, part.end)
                edits.append((start, hi, f"[`{line[start:hi]}`]({url})"))
        for start, end, link in sorted(edits, reverse=True):
            line = line[:start] + link + line[end:]
        out.append(line)
    return "\n".join(out)


_PATH_RE = re.compile(r"^[A-Za-z0-9_.@/+~-]+$")


def _guarded_spans(line: str) -> list[tuple[int, int]]:
    """Code spans and links of a markdown line."""
    return [m.span() for m in re.finditer(r"`[^`]*`|\[[^\]]*\]\([^)]*\)", line)]
