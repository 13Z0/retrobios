#!/usr/bin/env python3
"""Build the PortMaster port in ports/RetroBIOS into dist/RetroBIOS.zip.

The archive has the layout PortMaster's own release build gives a port
(PortMaster-New tools/build_release.py): the launch script at the top, and
one folder holding port.json, gameinfo.xml, the README as <name>.md, the
licenses and the port's files. install.py is copied from the repository
root, and only when it is the installer the bootstraps pin, so the port
runs the same bytes as the one-line command of the same commit. Members are
written as the packs write theirs: fixed timestamps, sorted names, the
executable bit read from the source, so the same tree gives the same zip.

Usage:
    python scripts/build_port.py
    python scripts/build_port.py --output-dir tmp/port
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

from artifacts import ArtifactLockBusy, artifact_lock

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT_SOURCE = REPO_ROOT / "ports" / "RetroBIOS"
PORT_DIR = "retrobios"
PORT_SCRIPT = "RetroBIOS.sh"
PORT_ZIP = "RetroBIOS.zip"
_PIN = re.compile(r'^DEFAULT_INSTALL_SHA256="([0-9a-f]{64})"$', re.MULTILINE)


def pinned_installer_sha256(root: Path = REPO_ROOT) -> str:
    """The SHA-256 install.sh accepts for install.py."""
    found = _PIN.search((root / "install.sh").read_text(encoding="utf-8"))
    if found is None:
        raise ValueError("install.sh pins no installer SHA-256")
    return found.group(1)


def port_members(root: Path = REPO_ROOT) -> list[tuple[str, Path]]:
    """(name in the archive, source file) for every member, sorted by name."""
    source = root / "ports" / "RetroBIOS"
    members = [
        (PORT_SCRIPT, source / PORT_SCRIPT),
        (f"{PORT_DIR}/port.json", source / "port.json"),
        (f"{PORT_DIR}/gameinfo.xml", source / "gameinfo.xml"),
        (f"{PORT_DIR}/{PORT_DIR}.md", source / "README.md"),
        (f"{PORT_DIR}/install.py", root / "install.py"),
        (f"{PORT_DIR}/licenses/LICENSE-retrobios.txt", root / "LICENSE"),
        (f"{PORT_DIR}/licenses/NOTICE-retrobios.txt", root / "NOTICE"),
    ]
    members += [
        (f"{PORT_DIR}/{path.relative_to(source / PORT_DIR).as_posix()}", path)
        for path in sorted((source / PORT_DIR).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    names = [name for name, _path in members]
    if len(set(names)) != len(names):
        raise ValueError("two port members share a name")
    return sorted(members)


def build_port(output: Path, root: Path = REPO_ROOT) -> Path:
    """Write the port archive to *output* and return it."""
    pinned = pinned_installer_sha256(root)
    actual = hashlib.sha256((root / "install.py").read_bytes()).hexdigest()
    if actual != pinned:
        raise ValueError(
            f"install.py is {actual[:12]}, install.sh pins {pinned[:12]}: "
            "re-pin the bootstraps before building the port"
        )
    # The packs' member writer: imported here, as it loads the whole
    # pack builder, which a caller reading PORT_ZIP does not need.
    from generate_pack import _add_pack_member  # noqa: PLC0415

    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = output.with_name(f".{output.name}.tmp")
    with zipfile.ZipFile(scratch, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, path in port_members(root):
            _add_pack_member(zf, str(path), name)
    scratch.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False, description="Build the RetroBIOS PortMaster port"
    )
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist",
                        help="where RetroBIOS.zip is written (default: dist/)")
    args = parser.parse_args()
    try:
        with artifact_lock(str(args.output_dir)):
            path = build_port(args.output_dir / PORT_ZIP)
    except (ArtifactLockBusy, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{path} ({path.stat().st_size} bytes, sha256 {digest})")
    return 0


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
