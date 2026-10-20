"""The site page that publishes each platform's corrected BIOS file.

export_native.py writes the platform's own file, corrected, under
exports/<platform>/ and what it changed beside it as exports/<platform>.json.
This page reads those reports: the reader finds a platform, the file its
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
    ("hashes_corrected", "Hashes updated to the emulator source"),
    ("hashes_filled", "Hashes added where the original lists none"),
    ("sizes_corrected", "Sizes updated to the emulator source"),
    ("sizes_removed", "Sizes no longer stated"),
    ("hashes_cleared", "Hashes no longer listed"),
    ("withheld", "Changes not applied"),
)


def _plural(count: int, noun: str, plural: str = "") -> str:
    return f"{count:,} {noun if count == 1 else plural or noun + 's'}"


def change_text(report: dict) -> str:
    """What the updated file changes, from the report's numbers.

    The command line prints its own terse summary. The page states each
    kind of change once, in plain terms, and leaves out what did not
    happen. RetroPie's unit is a package, so its own sentence is kept.
    """
    counts = report.get("counts")
    if not counts:
        return report["summary"]
    parts = [f"{_plural(counts['kept'], 'entry', 'entries')} kept"]
    extra = [
        (counts["added"], "file", "files", "added"),
        (len(report.get("hashes_corrected") or []), "hash", "hashes", "updated"),
        (report.get("requirements_corrected") or 0, "required flag", "required flags",
         "aligned with the code"),
        (len(report.get("hashes_filled") or []), "hash", "hashes", "added"),
        (len(report.get("sizes_corrected") or []), "size", "sizes", "updated"),
    ]
    parts += [f"{_plural(n, one, many)} {verb}" for n, one, many, verb in extra if n]
    return ", ".join(parts)


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
        "# Updated platform files",
        "",
        "Each platform keeps its own list of the BIOS files it checks: "
        "libretro's `System.dat`, Batocera's `batocera-systems`, Recalbox's "
        "`es_bios.xml` and so on. This project reads the emulators' source "
        "code file by file, and offers each platform its own file with those "
        "findings applied: a hash is updated where the emulator source states "
        "another, files the platform's emulators read are added, and no entry "
        "the platform declares is removed. Where a value of an entry is "
        "dropped, the platform's section lists it. Code the file carries is "
        "patched in place. With the original files on hand, the tests run "
        "the updated Batocera and ROCKNIX scripts (`--createReadme`), parse "
        "`checkBIOS.sh` and the RetroPie scriptmodules with `bash -n`, and "
        "check that every Recalbox entry carries the attributes the "
        "`es_bios.xsd` Recalbox publishes requires.",
        "",
        "Each file starts from the revision of the original that this project "
        "last read, so it compares cleanly with it, and every change can be "
        "taken or left on its own. Each value comes from an emulator profile, "
        "whose page cites where it was read. If a change does not fit a platform, an "
        "issue on the [repository](https://github.com/Abdess/retrobios/issues) "
        "is welcome: this project's profile is revised, and every platform's "
        "file follows.",
        "",
        "A file is added only when the format can carry it. A platform that "
        "checks an md5 needs one, so a file whose emulator source states no "
        "md5 is not added, and neither is a file under a system the "
        "platform's checker does not read. Those are counted on each "
        "platform's section.",
        "",
    ]
    if not reports:
        lines.extend([
            "No updated file has been published yet. They are written by "
            "`python scripts/export_native.py --all --fetch`.",
            "",
        ])
        return "\n".join(lines)

    lines.extend([
        "| Platform | Original file | Updated file | Changes |",
        "|----------|------------|----------------|--------------|",
    ])
    for report in reports:
        platform = report["platform"]
        lines.append(
            f"| [{_display(platform, names)}](#{platform}) | "
            f"{_original_link(report)} | {_corrected_link(report)} | "
            f"{change_text(report)} |"
        )
    lines.append("")

    for report in reports:
        platform = report["platform"]
        lines.extend([f"## {_display(platform, names)} {{ #{platform} }}", ""])
        text = change_text(report)
        lines.extend([text[:1].upper() + text[1:] + ".", ""])
        counts = report.get("counts") or {}
        if counts.get("not_added"):
            lines.extend([
                f"{_plural(counts['not_added'], 'other file')} the emulators read "
                f"{'is' if counts['not_added'] == 1 else 'are'} not added, for the "
                "reasons above.",
                "",
            ])
        if counts.get("not_kept"):
            lines.extend([
                f"{_plural(counts['not_kept'], 'entry', 'entries')} of the original "
                "cannot be written in this format and should be checked by hand.",
                "",
            ])
        if report.get("invalid"):
            lines.extend([
                '!!! warning "The updated file does not pass the platform\'s own checks"',
                "",
                *[f"    - {issue}" for issue in report["invalid"]],
                "",
            ])

        originals = report.get("originals") or {}
        files = report["files"]
        raw = [f"{REPO_RAW}/{EXPORTS_DIR}/{platform}/{name}" for name in files]
        if len(files) == 1:
            lines.extend([
                f"[Download the updated file]({raw[0]}){{ .md-button .md-button--primary }} "
                f"[See it on GitHub]({REPO_BLOB}/{EXPORTS_DIR}/{platform}/{files[0]}){{ .md-button }}",
                "",
            ])
        else:
            lines.extend([
                f"[Browse the {len(files)} updated files]"
                f"({REPO_TREE}/{EXPORTS_DIR}/{platform}){{ .md-button .md-button--primary }}",
                "",
            ])
        if len(originals) > 1:
            lines.extend([
                f'??? note "The {len(originals)} original files it is based on"',
                "",
                *[f"    - [`{name}`]({url})" for name, url in sorted(originals.items())],
                "",
            ])
        elif originals:
            (name, url), = originals.items()
            lines.extend([f"Based on [`{name}`]({url}).", ""])

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
