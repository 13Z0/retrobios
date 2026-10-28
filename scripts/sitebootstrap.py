"""The one-line bootstraps the site serves at its own address.

`curl -fsSL https://abdess.github.io/retrobios/i | sh` and
`irm https://abdess.github.io/retrobios/i.ps1 | iex` fetch copies of
install.sh and install.ps1 written when the site is built. Each copy
downloads install.py from the commit the site is built from, the commit
whose bootstrap pins that installer's SHA-256. The site deploys minutes
after a push: until then it serves the previous commit's pair, never the
bootstrap of one commit with the installer of the next.

GitHub Pages serves both names as application/octet-stream (no type in
mime-db for .ps1 or for a bare name). curl ignores the type, and
Invoke-RestMethod reads any type that is neither JSON nor XML as text.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from artifacts import write_if_changed

REPO_RAW = "https://raw.githubusercontent.com/Abdess/retrobios"
SH_NAME = "i"
PS1_NAME = "i.ps1"

# Served name: the bootstrap it copies and the one line naming the
# installer's address there.
SOURCES: dict[str, tuple[str, re.Pattern[str]]] = {
    SH_NAME: ("install.sh", re.compile(r'^(DEFAULT_INSTALL_URL=")[^"]*(")', re.M)),
    PS1_NAME: ("install.ps1", re.compile(r'^(\s*\$defaultInstallUrl = ")[^"]*(")', re.M)),
}


def build_commit(root: Path) -> str:
    """The commit the site is built from: GITHUB_SHA in CI, HEAD locally."""
    commit = os.environ.get("GITHUB_SHA", "").strip()
    if not commit:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"not a commit id: {commit!r}")
    return commit


def installer_url(commit: str) -> str:
    return f"{REPO_RAW}/{commit}/install.py"


def served_text(name: str, root: Path, commit: str) -> str:
    """The bootstrap served as name, its installer read from commit."""
    source, line = SOURCES[name]
    text = (root / source).read_text(encoding="utf-8")
    served, count = line.subn(
        lambda m: f"{m.group(1)}{installer_url(commit)}{m.group(2)}", text
    )
    if count != 1:
        raise ValueError(f"{source}: {count} installer address lines, expected one")
    return served


def write_bootstraps(public: Path, root: Path, commit: str | None = None) -> list[Path]:
    """Write every served bootstrap at the root of the site's public folder."""
    commit = commit or build_commit(root)
    public.mkdir(parents=True, exist_ok=True)
    written = []
    for name in SOURCES:
        path = public / name
        write_if_changed(str(path), served_text(name, root, commit))
        written.append(path)
    return written
