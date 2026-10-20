#!/usr/bin/env python3
"""Write the notes of a release from its record.

The notes carried the pack table, typed by hand from figures release.json
already held: a copied cell or an old collection total at the top is how
issue 81 happened. Install commands, the parts sentence and the table come
from the record and the registry. The maintainer writes what changed, and
that text is appended as given.

Usage:
    python scripts/release_notes.py --whats-new tmp/whats-new.md --output notes.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import release_record
from common import load_platform_registry, write_text_atomic

REPO = "Abdess/retrobios"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"


def _size(count: int) -> str:
    if count >= 1024**3:
        return f"{count / 1024**3:.1f} GB"
    return f"{count / 1024**2:.0f} MB"


def _display(pack: str) -> str:
    """RetroArch_Lakka_v1.22.2_BIOS_Pack.zip reads as RetroArch Lakka v1.22.2."""
    return pack.split("_BIOS_Pack", 1)[0].replace("_", " ")


def _extract_folders(record: dict, platforms_dir: str) -> dict[str, str]:
    """Pack name to the folder the registry says it extracts into."""
    folders: dict[str, str] = {}
    for key, entry in sorted(load_platform_registry(platforms_dir).items()):
        if not entry.get("extract_to"):
            continue
        needle = release_record._match_key(key)
        for name in sorted(record.get("packs", {})):
            # The name a pack starts with: RetroArch_Lakka is RetroArch's.
            if needle and release_record._match_key(name).startswith(needle):
                folders.setdefault(name, entry["extract_to"])
    return folders


def _links(tag: str, assets: list[str]) -> str:
    base = f"https://github.com/{REPO}/releases/download/{tag}"
    if len(assets) == 1:
        return f"[{assets[0]}]({base}/{assets[0]})"
    return " + ".join(f"[part {n}]({base}/{asset})" for n, asset in enumerate(assets, 1))


def render(record: dict, platforms_dir: str, whats_new: str) -> str:
    tag = record["tag"]
    packs = record.get("packs", {})
    folders = _extract_folders(record, platforms_dir)
    lines = [
        "### Install in one command",
        "",
        ("The installer detects the platform and its BIOS folder, downloads only"
         " what is missing and checks every file. Running it again is safe."),
        "",
        "**Linux, macOS, Steam Deck**",
        "```sh",
        f"curl -fsSL {RAW}/install.sh | sh",
        "```",
        "",
        "**Windows (PowerShell)**",
        "```powershell",
        f"irm {RAW}/install.ps1 | iex",
        "```",
        "",
        "### Download a pack",
        "",
        ("One pack per platform, the complete one: the BIOS list the platform"
         " publishes plus every file its emulators open. Files is what the pack"
         " holds once extracted, the count a file manager shows. Extracted size"
         " is the space those files take, and the download is smaller."),
        "",
    ]
    if any(len(pack["assets"]) > 1 for pack in packs.values()):
        lines += [
            ("A pack over 2 GB comes as parts that are each an ordinary ZIP of whole"
             " files (`Pack.part1of2.zip`, `Pack.part2of2.zip`). Download every part"
             " and extract them all into the same folder. Nothing needs joining."),
            "",
        ]
    lines += [
        "| Platform | Download | Extracted size | Files | Extract to |",
        "|----------|----------|---------------:|------:|-----------|",
    ]
    for name, pack in sorted(packs.items(), key=lambda item: -item[1]["extracted_size"]):
        folder = folders.get(name)
        lines.append(
            f"| {_display(name)} | {_links(tag, pack['assets'])} |"
            f" {_size(pack['extracted_size'])} | {pack['files']:,} |"
            f" {f'`{folder}`' if folder else '-'} |"
        )
    lines += ["", whats_new.strip(), ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Write a release's notes")
    parser.add_argument("--record", default=release_record.RECORD, type=Path,
                        help="release record (default: %(default)s)")
    parser.add_argument("--whats-new", required=True, type=Path,
                        help="what changed since the previous release, written by hand")
    parser.add_argument("--platforms-dir", default="platforms",
                        help="registry read for each pack's extract folder (default: %(default)s)")
    parser.add_argument("--output", required=True, type=Path,
                        help="notes file to write, passed to gh release create --notes-file")
    args = parser.parse_args()

    record = release_record.load_record(args.record)
    if not record.get("packs"):
        print(f"Error: {args.record} records no pack", file=sys.stderr)
        return 1
    whats_new = args.whats_new.read_text(encoding="utf-8")
    if not whats_new.strip():
        print(f"Error: {args.whats_new} is empty", file=sys.stderr)
        return 1
    write_text_atomic(str(args.output), render(record, args.platforms_dir, whats_new))
    print(f"Wrote {args.output} for {record['tag']}, {len(record['packs'])} packs")
    return 0


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
