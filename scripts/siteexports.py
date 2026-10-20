"""The site page that publishes each platform's corrected BIOS file.

export_native.py writes the platform's own file, corrected, under
exports/<platform>/ and what it changed beside it as exports/<platform>.json.
This page reads those reports: the reader finds their platform, the file its
maintainers keep, the corrected copy, and every change named.
"""

from __future__ import annotations

import json
from pathlib import Path

EXPORTS_DIR = "exports"
REPO_BLOB = "https://github.com/Abdess/retrobios/blob/main"
REPO_TREE = "https://github.com/Abdess/retrobios/tree/main"
REPO_RAW = "https://raw.githubusercontent.com/Abdess/retrobios/main"

# Report key, then how a line of it reads on the page.
CHANGE_KINDS = (
    ("hashes_corrected", "Hashes corrected"),
    ("hashes_filled", "Hashes filled where the platform had none"),
    ("sizes_corrected", "Sizes corrected"),
    ("sizes_removed", "Sizes removed"),
    ("hashes_cleared", "Hashes cleared"),
    ("withheld", "Corrections withheld"),
)


def load_export_reports(directory: str = EXPORTS_DIR) -> list[dict]:
    """Every platform report the export wrote, by platform name."""
    root = Path(directory)
    if not root.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*.json"))
    ]


def _display(platform: str, names: dict[str, str]) -> str:
    return names.get(platform) or platform


def _corrected_link(report: dict) -> str:
    platform, files = report["platform"], report["files"]
    if len(files) == 1:
        path = f"{EXPORTS_DIR}/{platform}/{files[0]}"
        return f"[`{Path(files[0]).name}`]({REPO_BLOB}/{path})"
    return f"[{len(files)} files]({REPO_TREE}/{EXPORTS_DIR}/{platform})"


def _original_link(report: dict) -> str:
    originals = report.get("originals") or {}
    if len(originals) == 1:
        (name, url), = originals.items()
        return f"[`{Path(name).name}`]({url})"
    return f"{len(originals)} files"


def generate_exports_page(reports: list[dict], names: dict[str, str]) -> str:
    """The page, names mapping each platform key to the name it displays."""
    lines = [
        "# Corrected platform files",
        "",
        "Each platform keeps its own list of the BIOS files it checks: "
        "libretro's `System.dat`, Batocera's `batocera-systems`, Recalbox's "
        "`es_bios.xml` and so on. Those lists are written by hand and drift "
        "from what the emulators actually read. Here is each platform's own "
        "file again, with what the emulator source code proves applied. A "
        "wrong hash is corrected, a file the platform's emulators need and "
        "the list lacks is added, and nothing the platform declares is ever "
        "removed. Code the file carries (a checker script, a C# database, a "
        "shell library) is patched in place and keeps working.",
        "",
        "Each file is taken at the revision our data was transcribed from, "
        "so a maintainer can diff it against theirs and take the changes "
        "line by line.",
        "",
        "A file the emulators read and the list lacks is added only when the "
        "format can carry it. Most platforms check an md5, so a file whose "
        "source states no md5 is left out, and so is one filed under a system "
        "the platform's checker never reads. Those are counted as left out.",
        "",
    ]
    if not reports:
        lines.extend([
            "No corrected file has been published yet. They are written by "
            "`python scripts/export_native.py --all --fetch`.",
            "",
        ])
        return "\n".join(lines)

    lines.extend([
        "| Platform | Their file | Corrected file | What changed |",
        "|----------|------------|----------------|--------------|",
    ])
    for report in reports:
        platform = report["platform"]
        lines.append(
            f"| [{_display(platform, names)}](#{platform}) | "
            f"{_original_link(report)} | {_corrected_link(report)} | "
            f"{report['summary']} |"
        )
    lines.append("")

    for report in reports:
        platform = report["platform"]
        lines.extend([f"## {_display(platform, names)} {{ #{platform} }}", ""])
        lines.extend([report["summary"][:1].upper() + report["summary"][1:] + ".", ""])
        if report.get("invalid"):
            lines.extend([
                '!!! warning "The corrected file fails the platform\'s own checks"',
                "",
                *[f"    - {issue}" for issue in report["invalid"]],
                "",
            ])

        originals = report.get("originals") or {}
        files = report["files"]
        raw = [f"{REPO_RAW}/{EXPORTS_DIR}/{platform}/{name}" for name in files]
        if len(files) == 1:
            lines.extend([
                f"[Download the corrected file]({raw[0]}){{ .md-button .md-button--primary }} "
                f"[See it on GitHub]({REPO_BLOB}/{EXPORTS_DIR}/{platform}/{files[0]}){{ .md-button }}",
                "",
            ])
        else:
            lines.extend([
                f"[Browse the {len(files)} corrected files]"
                f"({REPO_TREE}/{EXPORTS_DIR}/{platform}){{ .md-button .md-button--primary }}",
                "",
            ])
        if len(originals) > 1:
            lines.extend([
                f'??? note "The {len(originals)} files this export patches"',
                "",
                *[f"    - [`{name}`]({url})" for name, url in sorted(originals.items())],
                "",
            ])
        elif originals:
            (name, url), = originals.items()
            lines.extend([f"Patched from [`{name}`]({url}).", ""])

        for key, title in CHANGE_KINDS:
            items = report.get(key) or []
            if not items:
                continue
            lines.extend([
                f'??? note "{title} ({len(items)})"',
                "",
                *[f"    - {item}" if key == "withheld" else f"    - `{item}`" for item in items],
                "",
            ])
    return "\n".join(lines)
