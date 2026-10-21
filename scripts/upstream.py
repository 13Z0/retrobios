"""Access to upstream source repositories.

Resolves revisions, fetches files by sha, and compares trees across the
forge families the emulator profiles point at. Knows nothing about profile
structure.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from artifacts import set_plain_mode

USER_AGENT = "retrobios-profile-sync/1.0"
# Where fetched upstream files live, shared by every reader of this cache.
CACHE_DIR = ".cache/upstream"
ABSENT = "\0absent\0"
GITHUB_COMPARE_CAP = 300
RETRIES = 3
RETRY_BACKOFF = (0.5, 2.0)
GITHUB_HOSTS = frozenset(
    {"github.com", "api.github.com", "raw.githubusercontent.com"}
)

_HOSTS: dict[str, tuple[str, str, str]] = {
    "github.com": (
        "github",
        "https://api.github.com",
        "https://raw.githubusercontent.com",
    ),
    "gitlab.com": ("gitlab", "https://gitlab.com/api/v4", "https://gitlab.com"),
    # libretro builds a growing share of its cores from its own GitLab, and
    # several ports (wqxemu, cemu-libretro, libretro-radio) live only there.
    "git.libretro.com": (
        "gitlab",
        "https://git.libretro.com/api/v4",
        "https://git.libretro.com",
    ),
    # EDuke32 and its forks (eduke32, fury) are developed on this GitLab.
    "voidpoint.io": (
        "gitlab",
        "https://voidpoint.io/api/v4",
        "https://voidpoint.io",
    ),
    "codeberg.org": (
        "forgejo",
        "https://codeberg.org/api/v1",
        "https://codeberg.org",
    ),
    # The Ryujinx repository left GitHub in October 2024. This Gitea pull
    # mirror kept its history up to 1.1.1402.
    "git.ngram.ca": (
        "forgejo",
        "https://git.ngram.ca/api/v1",
        "https://git.ngram.ca",
    ),
    "git.citron-emu.org": (
        "forgejo",
        "https://git.citron-emu.org/api/v1",
        "https://git.citron-emu.org",
    ),
    "git.eden-emu.dev": (
        "forgejo",
        "https://git.eden-emu.dev/api/v1",
        "https://git.eden-emu.dev",
    ),
    "git.ryujinx.app": (
        "forgejo",
        "https://git.ryujinx.app/api/v1",
        "https://git.ryujinx.app",
    ),
}


_sleep = time.sleep


class UpstreamError(Exception):
    """Any failure while talking to a forge."""


class RateLimitError(UpstreamError):
    """The forge refused the request for quota reasons."""


class NotCachedError(UpstreamError):
    """An offline read of something the cache never stored.

    Distinct from absence: returning None made profile_sync report a file
    that exists as GONE, "absent at HEAD, no rename found".
    """


class GoneError(UpstreamError):
    """The upstream is not coming back.

    A legal takedown, a resource the forge reports as gone, or a host that
    no longer resolves. Retrying costs time and ends in the same place, and
    a caller sweeping every profile wants this told apart from a forge
    having a bad minute: one is a fact about the project, the other is
    weather.
    """


@dataclass(frozen=True)
class Repo:
    host: str
    family: str
    api_base: str
    raw_base: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Release:
    tag: str
    date: str
    is_prerelease: bool


@dataclass(frozen=True)
class FileChange:
    status: str
    path: str
    previous_path: str | None


@dataclass(frozen=True)
class CompareResult:
    files: list[FileChange]
    truncated: bool
    # The forge reports the two revisions on separate lines of history: the
    # base is no ancestor of the head. A pin in that state survives only
    # while the forge keeps the orphaned object (advancemame, 2026-10-05).
    diverged: bool = False


def parse_repo(url: str) -> Repo | None:
    """Build a Repo from a forge URL, or None when the host is unknown."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url.strip())
    entry = _HOSTS.get(parts.netloc)
    if entry is None:
        return None
    segments = [s for s in parts.path.split("/") if s]
    family, api_base, raw_base = entry
    if family == "gitlab" and "-" in segments:
        # GitLab nests projects in subgroups; "/-/" opens the project's pages.
        segments = segments[: segments.index("-")]
    if len(segments) < 2:
        return None
    if family == "gitlab":
        owner, name = "/".join(segments[:-1]), segments[-1]
    else:
        owner, name = segments[0], segments[1]
    if name.endswith(".git"):
        name = name[:-4]
    return Repo(parts.netloc, family, api_base, raw_base, owner, name)


def make_repo(host: str, owner: str, name: str) -> Repo | None:
    """Rebuild a Repo from a host and slug already known to be supported."""
    entry = _HOSTS.get(host)
    if entry is None:
        return None
    family, api_base, raw_base = entry
    return Repo(host, family, api_base, raw_base, owner, name)


def raw_url(repo: Repo, sha: str, path: str) -> str:
    """URL serving the raw bytes of one path at one revision."""
    quoted = urllib.parse.quote(path)
    if repo.family == "github":
        return f"{repo.raw_base}/{repo.owner}/{repo.name}/{sha}/{quoted}"
    if repo.family == "gitlab":
        return f"{repo.raw_base}/{repo.owner}/{repo.name}/-/raw/{sha}/{quoted}"
    return f"{repo.raw_base}/{repo.owner}/{repo.name}/raw/commit/{sha}/{quoted}"


def content_url(repo: Repo, sha: str, path: str) -> str:
    """URL the tooling fetches one path from.

    Forgejo and Gitea hosts put their web routes, /raw/commit included,
    behind a proof-of-work page (git.ngram.ca answers 200 with an HTML
    challenge), while the API raw route is not challenged. raw_url stays the
    link a reader opens.
    """
    if repo.family != "forgejo":
        return raw_url(repo, sha, path)
    quoted = urllib.parse.quote(path)
    return f"{repo.api_base}/repos/{repo.owner}/{repo.name}/raw/{quoted}?ref={sha}"


def _headers(url: str, accept_json: bool = False) -> dict[str, str]:
    """Request headers. GITHUB_TOKEN is only ever sent to GitHub."""
    headers = {"User-Agent": USER_AGENT}
    if accept_json:
        headers["Accept"] = "application/json"
    if urllib.parse.urlsplit(url).netloc in GITHUB_HOSTS:
        from common import github_token  # noqa: PLC0415

        # GITHUB_TOKEN, else the one gh is logged in with.
        token = github_token()
        if token:
            headers["Authorization"] = f"token {token}"
    return headers


def _http_failure(url: str, exc: urllib.error.HTTPError) -> UpstreamError:
    """Classify an HTTP error. Only a real quota signal is fatal.

    A forge answering 403 is usually refusing the request, not reporting a
    quota: small Forgejo instances behind anti-bot filters do it routinely.
    GitHub reports exhaustion with X-RateLimit-Remaining: 0, and 429 is the
    standard quota status everywhere, so those two are the only fatal cases.
    """
    if exc.code == 429:
        return RateLimitError(f"{url}: HTTP 429")
    if exc.code in (410, 451):
        reason = "withdrawn for legal reasons" if exc.code == 451 else "gone"
        return GoneError(f"{url}: HTTP {exc.code}, {reason}")
    if exc.code == 403 and exc.headers is not None:
        remaining = exc.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip() == "0":
            return RateLimitError(f"{url}: HTTP 403, quota exhausted")
    return UpstreamError(f"{url}: HTTP {exc.code}")


def _host_is_unresolvable(exc: BaseException) -> bool:
    """Whether a connection failure is the name itself, not the network.

    URLError carries the cause in `reason`, and a wrapped one carries it in
    `__cause__`. The walk is bounded because either chain can be cyclic.
    """
    seen: BaseException | None = exc
    for _ in range(8):
        if seen is None:
            break
        if isinstance(seen, socket.gaierror):
            # A resolver that answers nothing at all is the local network:
            # every profile would read as gone, and the sweep would report
            # nothing to review while it reviewed nothing.
            return seen.errno == socket.EAI_NONAME and _resolver_answers()
        nested = getattr(seen, "reason", None)
        seen = nested if isinstance(nested, BaseException) else seen.__cause__
    return False


# A name every forge call depends on: if it does not resolve, the fault is
# local and no host can be declared gone.
RESOLVER_PROBE = "github.com"


def _resolver_answers() -> bool:
    """Whether name resolution works here at all. Replaced in tests."""
    try:
        socket.getaddrinfo(RESOLVER_PROBE, 443)
    except OSError:
        return False
    return True


def _fetch(url: str, accept_json: bool = False) -> bytes | None:
    """Body of a GET, or None on 404.

    A pass over every profile issues thousands of requests, so a dropped
    connection or a transient 5xx is a certainty rather than an accident.
    Those are retried, a definitive answer from the forge is not.
    """
    failure: UpstreamError | None = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, headers=_headers(url, accept_json))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            failure = _http_failure(url, exc)
            if isinstance(failure, RateLimitError) or exc.code < 500:
                raise failure from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            if _host_is_unresolvable(exc):
                raise GoneError(f"{url}: host does not resolve") from exc
            failure = UpstreamError(f"{url}: {exc}")
        if attempt + 1 < RETRIES:
            _sleep(RETRY_BACKOFF[attempt])
    raise failure


def _http_text(url: str) -> str | None:
    """Text body of a GET, or None on 404. Replaced in tests."""
    body = _fetch(url)
    return None if body is None else body.decode("utf-8", errors="replace")


def _http_json(url: str) -> object | None:
    """Parsed JSON body of a GET, or None on 404. Replaced in tests."""
    body = _fetch(url, accept_json=True)
    return None if body is None else json.loads(body.decode())


def cache_path(cache_dir: str, repo: Repo, sha: str, path: str) -> Path:
    """Content-addressed location for one path at one revision."""
    root = Path(cache_dir) / repo.host / repo.owner / repo.name / sha
    safe = path.replace("\\", "/").strip("/")
    target = (root / safe).resolve()
    base = root.resolve()
    if not str(target).startswith(str(base) + os.sep) and target != base:
        target = base / safe.replace("/", "_").replace("..", "_")
    return target


def write_cache(target: Path, text: str) -> None:
    """Atomic write: unique scratch in the target directory, then replace.

    A profile can cite both a directory and files inside it, and the first
    citation caches `vice/data/C64DTV` as a file, which then blocks the
    directory the second one needs. The cache is derived data, so a collision
    skips the write rather than failing the read.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        return
    fd, scratch = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        set_plain_mode(scratch)
        os.replace(scratch, target)
    except OSError:
        Path(scratch).unlink(missing_ok=True)
        raise


def fetch_file(
    repo: Repo,
    sha: str,
    path: str,
    cache_dir: str,
    offline: bool = False,
) -> list[str] | None:
    """Lines of one path at one revision, or None when absent.

    Offline, a path the cache never stored raises NotCachedError: nothing
    is known about it, which is not the same as knowing it is absent.
    """
    target = cache_path(cache_dir, repo, sha, path)
    if target.is_file():
        text = target.read_text(encoding="utf-8")
        return None if text == ABSENT else text.splitlines()
    if offline:
        raise NotCachedError(f"{repo.slug}@{sha[:8]}:{path} is not in the offline cache")
    text = _http_text(content_url(repo, sha, path))
    write_cache(target, ABSENT if text is None else text)
    return None if text is None else text.splitlines()


# How long an answer about a moving reference stays trusted. A branch tip, a
# tag list or a release list changes under the profile: libretro/hatari grew
# a new main branch after its tip was first cached, and every comparison
# then ran against the old branch. Content addressed by a sha or a date
# never changes and is cached for good.
MOVING_TTL = 6 * 3600


def _api(
    url: str, cache_dir: str, offline: bool, max_age: float | None = None,
    absent_max_age: float | None = None,
) -> object | None:
    """Cached API call. The cache stores absence as well as payloads.

    ``max_age`` bounds the trust in a cached answer, in seconds. A stale
    answer is refreshed unless the run is offline, when it still serves.
    ``absent_max_age`` bounds a cached absence alone, for a resource that
    can appear but, once there, does not change.
    """
    key = hashlib.sha256(url.encode()).hexdigest()
    target = Path(cache_dir) / "_api" / f"{key}.json"
    if target.is_file():
        raw = target.read_text(encoding="utf-8")
        bound = (
            absent_max_age
            if raw == ABSENT and absent_max_age is not None
            else max_age
        )
        fresh = bound is None or time.time() - target.stat().st_mtime <= bound
        if fresh or offline:
            return None if raw == ABSENT else json.loads(raw)
    if offline:
        return None
    payload = _http_json(url)
    write_cache(target, ABSENT if payload is None else json.dumps(payload))
    return payload


def fetch_page(url: str, cache_dir: str, offline: bool) -> str | None:
    """A web page's text, cached like a moving API answer (MOVING_TTL).

    A project published outside a forge (Altirra, mednafen, ePSXe) says its
    version on a download page: that page is the only release feed there is.
    """
    key = hashlib.sha256(url.encode()).hexdigest()
    target = Path(cache_dir) / "_pages" / f"{key}.txt"
    if target.is_file():
        fresh = time.time() - target.stat().st_mtime <= MOVING_TTL
        if fresh or offline:
            raw = target.read_text(encoding="utf-8")
            return None if raw == ABSENT else raw
    if offline:
        return None
    text = _http_text(url)
    write_cache(target, ABSENT if text is None else text)
    return text


def _project(repo: Repo) -> str:
    return urllib.parse.quote(f"{repo.owner}/{repo.name}", safe="")


def _commits_url(repo: Repo, date: str | None, branch: str | None = None) -> str:
    if repo.family == "github":
        base = f"{repo.api_base}/repos/{repo.slug}/commits?per_page=1"
        if branch:
            base += f"&sha={urllib.parse.quote(branch, safe='')}"
    elif repo.family == "gitlab":
        base = (
            f"{repo.api_base}/projects/{_project(repo)}"
            f"/repository/commits?per_page=1"
        )
        if branch:
            base += f"&ref_name={urllib.parse.quote(branch, safe='')}"
    else:
        base = f"{repo.api_base}/repos/{repo.slug}/commits?limit=1"
        if branch:
            base += f"&sha={urllib.parse.quote(branch, safe='')}"
    return f"{base}&until={date}T23:59:59Z" if date else base


def _first_sha(payload: object) -> str | None:
    if isinstance(payload, list) and payload:
        head = payload[0]
        if isinstance(head, dict):
            return head.get("sha") or head.get("id")
    return None


def current_slug(repo: Repo, cache_dir: str, offline: bool = False) -> str | None:
    """The owner/name the forge answers for a repository today.

    A renamed or transferred repository keeps answering under its old name:
    GitHub redirects (AloysHF/NicaiEmu answers as AloysHF/CoolbarEmu), GitLab
    and Gitea do the same. Every later call follows the redirect, so only the
    name in the payload tells the project moved.
    """
    if repo.family == "gitlab":
        url, key = f"{repo.api_base}/projects/{_project(repo)}", "path_with_namespace"
    else:
        url, key = f"{repo.api_base}/repos/{repo.slug}", "full_name"
    payload = _api(url, cache_dir, offline, MOVING_TTL)
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return str(value) if value else None


def resolve_head(
    repo: Repo, cache_dir: str, offline: bool = False, branch: str | None = None
) -> str | None:
    """Sha of a branch tip, the default branch unless one is named.

    A libretro port often lives on a branch of a fork rather than on the
    default branch, so the code a profile documents can be absent from the
    tip the forge serves by default.
    """
    return _first_sha(
        _api(_commits_url(repo, None, branch), cache_dir, offline, MOVING_TTL)
    )


def resolve_commit_at(
    repo: Repo, date: str, cache_dir: str, offline: bool = False,
    branch: str | None = None,
) -> str | None:
    """Last commit on or before a date, on the default branch or a named one."""
    if repo.family in ("github", "gitlab"):
        return _first_sha(_api(_commits_url(repo, date, branch), cache_dir, offline))
    return _gitea_commit_at(repo, date, cache_dir, offline, branch)


# Gitea and Forgejo serve 50 commits a page at most.
GITEA_PAGE = 50


def _gitea_commit_at(
    repo: Repo, date: str, cache_dir: str, offline: bool,
    branch: str | None,
) -> str | None:
    """Walk the history newest first to the first commit dated by the cutoff.

    The Gitea and Forgejo commits API ignores ``until`` and answers with the
    tip: gam4980's profiled_date of 2026-03-23 resolved to a commit of July,
    a revision the profile never read.
    """
    cutoff = datetime.fromisoformat(f"{date}T23:59:59+00:00")
    base = (
        f"{repo.api_base}/repos/{repo.slug}/commits?limit={GITEA_PAGE}"
        "&stat=false&verification=false&files=false"
    )
    if branch:
        base += f"&sha={urllib.parse.quote(branch, safe='')}"
    page = 1
    while True:
        payload = _api(f"{base}&page={page}", cache_dir, offline, MOVING_TTL)
        if not isinstance(payload, list) or not payload:
            return None
        for entry in payload:
            stamp = _gitea_commit_date(entry)
            if stamp is not None and stamp <= cutoff:
                return entry.get("sha")
        page += 1


def _gitea_commit_date(entry: object) -> datetime | None:
    if not isinstance(entry, dict):
        return None
    commit = entry.get("commit")
    committer = commit.get("committer") if isinstance(commit, dict) else None
    raw = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tags_url(repo: Repo) -> str:
    if repo.family == "gitlab":
        return (
            f"{repo.api_base}/projects/{_project(repo)}"
            f"/repository/tags?per_page=100"
        )
    return f"{repo.api_base}/repos/{repo.slug}/tags?per_page=100"


def list_tags(repo: Repo, cache_dir: str, offline: bool = False) -> list[str]:
    """Tag names, newest first as the forge orders them."""
    payload = _api(_tags_url(repo), cache_dir, offline, MOVING_TTL)
    if not isinstance(payload, list):
        return []
    return [t["name"] for t in payload if isinstance(t, dict) and t.get("name")]


def resolve_tag_commit(
    repo: Repo, tag: str, cache_dir: str, offline: bool = False
) -> str | None:
    """Commit a tag points at."""
    payload = _api(_tags_url(repo), cache_dir, offline, MOVING_TTL)
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("name") != tag:
            continue
        commit = entry.get("commit")
        if isinstance(commit, dict):
            return commit.get("sha") or commit.get("id")
    return None


def tag_commit(
    repo: Repo, tag: str, cache_dir: str, offline: bool = False
) -> str | None:
    """Commit a named tag points at, looked up directly.

    Listing tags is paginated, and a repository publishing nightly tags pushes
    an old release far past the first page, so the tag is asked for by name.
    An annotated tag points at a tag object, which is dereferenced.
    """
    if repo.family != "github":
        return resolve_tag_commit(repo, tag, cache_dir, offline)
    quoted = urllib.parse.quote(tag)
    # A tag pushed after the first lookup must be seen: the 404 expires,
    # the commit a tag names does not.
    payload = _api(
        f"{repo.api_base}/repos/{repo.slug}/git/ref/tags/{quoted}",
        cache_dir,
        offline,
        absent_max_age=MOVING_TTL,
    )
    if not isinstance(payload, dict):
        return None
    obj = payload.get("object")
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "tag":
        return obj.get("sha")
    annotated = _api(
        f"{repo.api_base}/repos/{repo.slug}/git/tags/{obj.get('sha')}",
        cache_dir,
        offline,
    )
    target = annotated.get("object") if isinstance(annotated, dict) else None
    return target.get("sha") if isinstance(target, dict) else None


def _releases_url(repo: Repo) -> str:
    if repo.family == "gitlab":
        return f"{repo.api_base}/projects/{_project(repo)}/releases"
    return f"{repo.api_base}/repos/{repo.slug}/releases/latest"


def latest_release(
    repo: Repo, cache_dir: str, offline: bool = False
) -> Release | None:
    """Most recent release the forge exposes."""
    payload = _api(_releases_url(repo), cache_dir, offline, MOVING_TTL)
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag_name") or payload.get("tag") or ""
    stamp = payload.get("published_at") or payload.get("released_at") or ""
    if not tag:
        return None
    return Release(str(tag), str(stamp)[:10], bool(payload.get("prerelease")))


def _compare_url(repo: Repo, base: str, head: str) -> str:
    if repo.family == "gitlab":
        return (
            f"{repo.api_base}/projects/{_project(repo)}"
            f"/repository/compare?from={base}&to={head}"
        )
    return f"{repo.api_base}/repos/{repo.slug}/compare/{base}...{head}"


def _changes_from_github(payload: dict) -> list[FileChange]:
    return [
        FileChange(
            entry.get("status", "modified"),
            entry.get("filename", ""),
            entry.get("previous_filename"),
        )
        for entry in (payload.get("files") or [])
        if isinstance(entry, dict)
    ]


def _changes_from_gitlab(payload: dict) -> list[FileChange]:
    changes = []
    for entry in payload.get("diffs") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("renamed_file"):
            status = "renamed"
        elif entry.get("new_file"):
            status = "added"
        elif entry.get("deleted_file"):
            status = "removed"
        else:
            status = "modified"
        previous = entry.get("old_path") if status == "renamed" else None
        changes.append(FileChange(status, entry.get("new_path", ""), previous))
    return changes


def compare(
    repo: Repo, base: str, head: str, cache_dir: str, offline: bool = False
) -> CompareResult:
    """Tree difference between two revisions."""
    payload = _api(_compare_url(repo, base, head), cache_dir, offline)
    if not isinstance(payload, dict):
        return CompareResult([], True)
    if repo.family == "gitlab":
        files = _changes_from_gitlab(payload)
    else:
        files = _changes_from_github(payload)
    truncated = bool(payload.get("truncated")) or len(files) >= GITHUB_COMPARE_CAP
    return CompareResult(files, truncated, payload.get("status") == "diverged")


def find_renamed(result: CompareResult, path: str) -> str | None:
    """New path of a file the comparison reports as renamed."""
    for change in result.files:
        if change.status == "renamed" and change.previous_path == path:
            return change.path
    return None


def _tree_url(repo: Repo, sha: str) -> str | None:
    if repo.family == "gitlab":
        return None
    return f"{repo.api_base}/repos/{repo.slug}/git/trees/{sha}?recursive=1"


def list_directory(
    repo: Repo, sha: str, path: str, cache_dir: str, offline: bool = False
) -> list[str] | None:
    """Names of the blobs directly under one directory at one revision.

    One request for the directory alone, where the whole tree of a large
    repository comes back truncated. None when the forge has no such call
    or the directory does not exist there.
    """
    if repo.family != "github":
        return None
    url = f"{repo.api_base}/repos/{repo.slug}/git/trees/{sha}:{path.strip('/')}"
    payload = _api(url, cache_dir, offline)
    if not isinstance(payload, dict) or payload.get("truncated"):
        return None
    return sorted(
        entry["path"]
        for entry in payload.get("tree") or []
        if isinstance(entry, dict) and entry.get("type") == "blob" and entry.get("path")
    )


TREE_PAGE_SIZE = 100
MAX_TREE_PAGES = 50


def _gitlab_tree(
    repo: Repo, sha: str, cache_dir: str, offline: bool
) -> tuple[list[str], bool]:
    """GitLab serves its tree in pages, each page cached on its own URL.

    A page that cannot be read mid-walk reports the listing as truncated:
    the paths already collected are real, but their absence proves nothing.
    """
    paths: list[str] = []
    for page in range(1, MAX_TREE_PAGES + 1):
        url = (
            f"{repo.api_base}/projects/{_project(repo)}/repository/tree"
            f"?ref={sha}&recursive=true&per_page={TREE_PAGE_SIZE}&page={page}"
        )
        payload = _api(url, cache_dir, offline)
        if not isinstance(payload, list):
            return paths, True
        paths.extend(
            entry["path"]
            for entry in payload
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and entry.get("path")
        )
        if len(payload) < TREE_PAGE_SIZE:
            return paths, False
    return paths, True


def list_tree(
    repo: Repo, sha: str, cache_dir: str, offline: bool = False
) -> tuple[list[str], bool]:
    """Every blob path at one revision, and whether the forge truncated it."""
    if repo.family == "gitlab":
        return _gitlab_tree(repo, sha, cache_dir, offline)
    url = _tree_url(repo, sha)
    if url is None:
        return [], True
    payload = _api(url, cache_dir, offline)
    if not isinstance(payload, dict):
        return [], True
    paths = [
        entry["path"]
        for entry in payload.get("tree") or []
        if isinstance(entry, dict) and entry.get("type") == "blob" and entry.get("path")
    ]
    return paths, bool(payload.get("truncated"))


def list_gitlinks(
    repo: Repo, sha: str, cache_dir: str, offline: bool = False
) -> dict[str, str]:
    """Submodule path -> the commit it points at, at one revision.

    Read from the same tree listing as list_tree, so the call costs nothing
    once the tree is cached.
    """
    if repo.family == "gitlab":
        links: dict[str, str] = {}
        for page in range(1, MAX_TREE_PAGES + 1):
            url = (
                f"{repo.api_base}/projects/{_project(repo)}/repository/tree"
                f"?ref={sha}&recursive=true&per_page={TREE_PAGE_SIZE}&page={page}"
            )
            payload = _api(url, cache_dir, offline)
            if not isinstance(payload, list):
                break
            links.update(
                (entry["path"], entry["id"])
                for entry in payload
                if isinstance(entry, dict) and entry.get("type") == "commit"
                and entry.get("path") and entry.get("id")
            )
            if len(payload) < TREE_PAGE_SIZE:
                break
        return links
    url = _tree_url(repo, sha)
    payload = _api(url, cache_dir, offline) if url else None
    if not isinstance(payload, dict):
        return {}
    return {
        entry["path"]: entry["sha"]
        for entry in payload.get("tree") or []
        if isinstance(entry, dict) and entry.get("type") == "commit"
        and entry.get("path") and entry.get("sha")
    }


def commit_date(repo: Repo, sha: str, cache_dir: str, offline: bool = False) -> str | None:
    """When a revision was committed, as the forge reports it (ISO 8601)."""
    if repo.family == "gitlab":
        payload = _api(
            f"{repo.api_base}/projects/{_project(repo)}/repository/commits/{sha}",
            cache_dir, offline,
        )
        value = payload.get("committed_date") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None
    if repo.family == "github":
        url = f"{repo.api_base}/repos/{repo.slug}/commits/{sha}"
    else:
        url = f"{repo.api_base}/repos/{repo.slug}/git/commits/{sha}"
    stamp = _gitea_commit_date(_api(url, cache_dir, offline))
    return stamp.isoformat() if stamp is not None else None


def commits_touching(
    repo: Repo, path: str, base: str, cache_dir: str, offline: bool = False,
    head: str | None = None,
) -> int:
    """Commits touching one path since a revision, on the default branch.

    Listing from the pin counted the history before it: a profile pinned on
    HEAD scored the age of its files as drift. The count is of the commits
    after the pin's date that touch the path, the pin itself excluded.
    """
    if head is not None and head == base:
        return 0
    since = commit_date(repo, base, cache_dir, offline)
    if since is None:
        return 0
    quoted = urllib.parse.quote(path)
    stamp = urllib.parse.quote(since)
    if repo.family == "gitlab":
        url = (
            f"{repo.api_base}/projects/{_project(repo)}"
            f"/repository/commits?path={quoted}&since={stamp}&per_page=100"
        )
    elif repo.family == "github":
        url = (
            f"{repo.api_base}/repos/{repo.slug}/commits"
            f"?path={quoted}&since={stamp}&per_page=100"
        )
    else:
        url = (
            f"{repo.api_base}/repos/{repo.slug}/commits"
            f"?path={quoted}&since={stamp}&limit=100"
        )
    # The list grows with every push touching the path: cached for good, the
    # triage drift score stayed at whatever the first run counted.
    payload = _api(url, cache_dir, offline, MOVING_TTL)
    if not isinstance(payload, list):
        return 0
    return sum(
        1 for entry in payload
        if isinstance(entry, dict) and (entry.get("sha") or entry.get("id")) != base
    )
