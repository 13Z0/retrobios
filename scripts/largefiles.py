"""Files too large for the repository.

They live as release assets and are fetched at build time, verified
against the hash the caller declares."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import sys
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from hashing import compute_hashes


LARGE_FILES_RELEASE = "large-files"
LARGE_FILES_REPO = "Abdess/retrobios"
LARGE_FILES_CACHE = ".cache/large"
GITIGNORE = Path(__file__).resolve().parent.parent / ".gitignore"

_UNSAFE_ASSET_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def registered_paths(gitignore_text: str) -> list[str]:
    """The bios/ paths .gitignore lists, which are the release assets."""
    return [
        line.strip()
        for line in gitignore_text.splitlines()
        if line.strip().startswith("bios/")
    ]


def load_registered_paths(gitignore: str | Path = GITIGNORE) -> list[str]:
    try:
        return registered_paths(Path(gitignore).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def asset_names(registered: Iterable[str]) -> dict[str, str]:
    """Map each registered path to the name of its release asset.

    A path whose basename no other registered path shares is published
    under that basename. A path whose basename is shared is published under
    its location below bios/, segments joined by "--" and every character
    outside [A-Za-z0-9._-] replaced by "_":
    bios/Id Software/Wolfenstein Enemy Territory/etmain/pak0.pk3 becomes
    Id_Software--Wolfenstein_Enemy_Territory--etmain--pak0.pk3. The name
    depends on the path only, never on the bytes, so rebuilding a file
    keeps its asset.
    """
    paths = sorted(set(registered))
    counts = Counter(os.path.basename(p) for p in paths)
    names: dict[str, str] = {}
    for registered_path in paths:
        base = os.path.basename(registered_path)
        if counts[base] == 1:
            names[registered_path] = base
            continue
        rel = registered_path.removeprefix("bios/")
        names[registered_path] = "--".join(
            _UNSAFE_ASSET_CHARS.sub("_", part) for part in rel.split("/")
        )
    owners: dict[str, str] = {}
    for registered_path, name in names.items():
        # GitHub publishes a space as a dot, so both spellings are taken.
        for spelling in {name, name.replace(" ", ".")}:
            other = owners.setdefault(spelling, registered_path)
            if other != registered_path:
                raise ValueError(
                    f"release asset {spelling!r} named by both {other!r} "
                    f"and {registered_path!r}"
                )
    return names


def asset_name(path: str, registered: Iterable[str]) -> str:
    """The release asset name of *path* among the *registered* paths."""
    return asset_names([*registered, path])[path]


def published_asset_name(name: str) -> str:
    """The name GitHub serves an asset under: it publishes a space as a dot."""
    return name.replace(" ", ".")


def asset_candidates(name: str, registered: Iterable[str]) -> list[str]:
    """Assets that may hold *name*, a registered path or a bare file name.

    A bare name shared by several registered paths has one asset per path,
    and the caller's hash tells them apart.
    """
    registered = list(registered)
    if "/" in name:
        return [asset_name(name, registered)]
    names = asset_names(registered)
    matches = [
        names[p] for p in sorted(names) if os.path.basename(p) == name
    ]
    # A pinned revision is stored as .variants/<name>.<md5 prefix>: ROCKNIX
    # pins PS3UPDAT.PUP to the variant, and asking for the bare name never
    # reached its asset.
    matches += [
        names[p] for p in sorted(names)
        if "/.variants/" in p and os.path.basename(p).startswith(name + ".")
    ]
    return matches or [name]


def fetch_large_file(
    name: str,
    dest_dir: str = LARGE_FILES_CACHE,
    expected_sha1: str = "",
    expected_md5: str = "",
    *,
    offline: bool = False,
    registered: Iterable[str] | None = None,
) -> str | None:
    """Return a verified cached large file, downloading it only when allowed.

    *name* is a registered bios/ path or a bare file name. asset_names()
    turns it into the asset to fetch, and a bare name shared by several
    registered paths tries each of their assets until one verifies.
    """
    if not name or name.endswith("/"):
        # A directory entry names a tree, never one asset: asked for
        # "caesar3/", the store answered with .cache/large/ itself.
        return None
    if registered is None:
        registered = load_registered_paths()
    for asset in asset_candidates(name, registered):
        cached = _fetch_asset(
            asset, dest_dir, expected_sha1, expected_md5, offline=offline
        )
        if cached:
            return cached
    return None


def _fetch_asset(
    name: str,
    dest_dir: str,
    expected_sha1: str,
    expected_md5: str,
    *,
    offline: bool,
) -> str | None:
    cached = os.path.join(dest_dir, name)
    # Between the existence test and the hash, a concurrent run can drop the
    # same stale entry: the file is gone by the time this one reads it, and
    # both of them try to unlink it.
    def _drop(path: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    if os.path.isdir(cached):
        return None
    if os.path.exists(cached):
        try:
            hashes = compute_hashes(cached) if (expected_sha1 or expected_md5) else {}
        except FileNotFoundError:
            hashes = None
        if hashes is None:
            pass
        elif not (expected_sha1 or expected_md5) or _matches(
            hashes, expected_sha1, expected_md5
        ):
            return cached
        elif not _replaced_on_release(name, cached, offline):
            # A copy of this asset that answers another hash: the caller
            # wants a different revision under the same name, and the
            # release still serves this one. Keeping it is what lets the
            # next caller for the primary find it.
            return None
        # Otherwise the release was re-uploaded since this copy was cached
        # (gh release upload --clobber): only a download can answer, and
        # it replaces the copy only if it verifies.

    # RETROBIOS_OFFLINE forbids every download whatever a caller passes: the
    # CI test job sets it, since tests fetching 252 MB assets into the shared
    # cache filled the runner's disk and changed what later tests resolved.
    if offline or os.environ.get("RETROBIOS_OFFLINE"):
        return None

    os.makedirs(dest_dir, exist_ok=True)
    # A per-process scratch name: two runs fetching the same asset into one
    # shared path interleave their writes into a full-size, corrupt file.
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=dest_dir, prefix=os.path.basename(cached) + ".", suffix=".tmp"
    )
    os.close(tmp_fd)
    try:
        downloaded = False
        for candidate, url in _asset_urls(name):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "retrobios/1.0"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    with open(tmp_path, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                downloaded = True
                break
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                # A stalled or cut stream is a timeout or IncompleteRead, not
                # a URLError: it escaped this handler and left its scratch.
                print(f"  large file {candidate}: {exc}", file=sys.stderr)
        if not downloaded:
            return None
        if (expected_sha1 or expected_md5) and not _matches(
            compute_hashes(tmp_path), expected_sha1, expected_md5
        ):
            return None
        os.replace(tmp_path, cached)
        return cached
    finally:
        _drop(tmp_path)


def _asset_urls(name: str) -> list[tuple[str, str]]:
    """Release URLs an asset may be published under, with their names.

    GitHub rewrites spaces to dots in release asset names, so a file whose
    name contains spaces is published under a dotted name.
    """
    candidates = [name]
    if " " in name:
        candidates.append(name.replace(" ", "."))
    return [
        (
            candidate,
            (
                f"https://github.com/{LARGE_FILES_REPO}/releases/download/"
                f"{LARGE_FILES_RELEASE}/{urllib.parse.quote(candidate)}"
            ),
        )
        for candidate in candidates
    ]


def _replaced_on_release(name: str, cached: str, offline: bool) -> bool:
    """Whether the release now serves other bytes than the cached copy.

    The release lists a sha256 per asset: a dump rebuilt at the same size
    (the eight 251658304-byte DSi NANDs) differs only there, and judged by
    size the stale copy was kept for good. Without a digest the sizes are
    compared, as check_release_assets does. No download either way:
    offline or unreadable, the copy is assumed current.
    """
    if offline:
        return False
    digest = next(
        (d for candidate, _url in _asset_urls(name)
         if (d := _served_digests().get(candidate))),
        None,
    )
    if digest:
        return compute_hashes(cached, frozenset({"sha256"}))["sha256"] != digest
    served = _served_size(name)
    return served is not None and served != os.path.getsize(cached)


_digests_cache: dict[str, str] | None = None


def _served_digests() -> dict[str, str]:
    """sha256 of each asset the release serves, by asset name, empty when
    the API cannot be read."""
    global _digests_cache
    if _digests_cache is not None:
        return _digests_cache
    url = (
        f"https://api.github.com/repos/{LARGE_FILES_REPO}/releases/tags/"
        f"{LARGE_FILES_RELEASE}"
    )
    headers = {"User-Agent": "retrobios/1.0", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    digests: dict[str, str] = {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=60
        ) as resp:
            release = json.load(resp)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        print(f"  large files release listing: {exc}", file=sys.stderr)
        release = {}
    for asset in release.get("assets") or []:
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:"):
            digests[asset.get("name", "")] = digest.removeprefix("sha256:").lower()
    _digests_cache = digests
    return digests


def _served_size(name: str) -> int | None:
    """Size of the asset the release serves now, or None when unreadable."""
    for candidate, url in _asset_urls(name):
        try:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "retrobios/1.0"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                length = resp.headers.get("Content-Length")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            print(f"  large file {candidate}: {exc}", file=sys.stderr)
            continue
        if length and length.isdigit():
            return int(length)
    return None


def _matches(hashes: dict, expected_sha1: str, expected_md5: str) -> bool:
    """Whether computed hashes answer the caller's declaration."""
    if expected_sha1 and hashes["sha1"].lower() != expected_sha1.lower():
        return False
    if expected_md5:
        md5_list = [m.strip().lower() for m in expected_md5.split(",") if m.strip()]
        return hashes["md5"].lower() in md5_list
    return True
