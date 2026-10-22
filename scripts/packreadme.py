"""The notes shipped inside a pack.

A pack has to explain itself offline: what it holds, where each file
goes, and which ones the user has to rename."""

from __future__ import annotations

from siteguides import INSTALL_BASE, pack_manual

INSTALL_SH = f"{INSTALL_BASE}/install.sh"
INSTALL_PS1 = f"{INSTALL_BASE}/install.ps1"

def _detect_systems(detect: list[dict]) -> list[str]:
    """The operating systems the registry's detection rules cover, in order."""
    order = ("linux", "darwin", "windows")
    seen = {str(rule.get("os", "")) for rule in detect}
    if "any" in seen:
        return list(order)
    return [name for name in order if name in seen]


def _card_section(platform_name: str, display: str) -> str:
    """The one-line install for a frontend whose files live on its SD card.

    The installer runs on the computer the card is mounted on, and the card
    root is named: no folder on that computer is one the frontend reads.
    """
    return (
        "  Option A: Automatic (recommended)\n"
        "  ---------------------------------\n"
        "  With the SD card mounted on a computer, name its root with --dest.\n"
        "  Linux and macOS: run this in a terminal:\n\n"
        f"    curl -fsSL {INSTALL_SH} | sh -s -- --platform {platform_name} --dest /path/to/card\n\n"
        "  Windows: run this in PowerShell (E: being the card):\n\n"
        f"    &([scriptblock]::Create((irm {INSTALL_PS1}))) --platform {platform_name} --dest E:\\\n\n"
        f"  The installer writes each file where {display} hands it to the core\n"
        "  and checks every file.\n\n"
    )


def _card_manual(display: str) -> str:
    """Where the files of a per-tag Bios/ layout go, copied by hand."""
    return (
        "  Option B: Manual\n"
        "  ----------------\n"
        "  1. Put the SD card in your PC\n"
        "  2. Extract all files from this archive directly into the root of the\n"
        "     card: the archive holds the Bios/ folder, with one subfolder per\n"
        "     launch tag (Bios/PS/, Bios/GBA/, ...)\n"
        "  3. Overwrite if asked\n\n"
        f"  {display} hands each core the folder named after the tag in\n"
        "  parentheses of its Roms folder (\"Sony PlayStation (PS)\" reads\n"
        "  Bios/PS/). A Roms folder can be renamed as long as the tag stays.\n\n"
    )


def _automatic_section(
    display: str, detect: list[dict], standalone_copies: list[dict] | None,
) -> str:
    """The one-line install, for every system the registry detects the
    platform on. Written per platform by hand, it left ROCKNIX and MiSTer
    without it and told EmuDeck's Windows edition to open Konsole."""
    systems = _detect_systems(detect)
    if not systems:
        return ""
    # A platform detected on Linux only is a system of its own (Batocera,
    # ROCKNIX, MiSTer) or runs on one machine (RetroDECK, a RomM server):
    # the command runs there.
    on_device = systems == ["linux"]
    lines = [
        "  Option A: Automatic (recommended)",
        "  ---------------------------------",
    ]
    posix = [name for name in systems if name != "windows"]
    if posix:
        if on_device:
            where = f"On the machine running {display} (its terminal, or SSH), run:"
        else:
            labels = {"linux": "Linux", "darwin": "macOS"}
            where = f"{' and '.join(labels[name] for name in posix)}: run this in a terminal:"
        lines += [f"  {where}", "", f"    curl -fsSL {INSTALL_SH} | sh", ""]
    if "windows" in systems:
        lines += ["  Windows: run this in PowerShell:", "", f"    irm {INSTALL_PS1} | iex", ""]
    lines += [
        f"  The installer finds the {display} BIOS folder on this machine and",
        "  installs there, checking every file. --dest names another folder.",
        "",
    ]
    # On a desktop: an emulator folder under the home of a console's own
    # system is not one its frontend reads.
    if standalone_copies and not on_device:
        lines += [
            "  Standalone emulators read some files from folders of their own",
            "  (Switch and 3DS keys, PlayStation and GameCube BIOS). To copy them",
            "  into the emulator folders the installer finds, add --standalone-copies:",
            "",
        ]
        if posix:
            lines.append(f"    curl -fsSL {INSTALL_SH} | sh -s -- --standalone-copies")
        if "windows" in systems:
            lines.append(f"    &([scriptblock]::Create((irm {INSTALL_PS1}))) --standalone-copies")
        lines.append("")
    return "\n".join(lines) + "\n"


def _generic_manual(bios_paths: list[str]) -> str:
    """Where the files go, from the registry's paths, for a platform with no
    manual guide of its own."""
    targets = [path for path in bios_paths if path]
    where = " or ".join(f"{path}/" for path in dict.fromkeys(targets)) or "your BIOS directory"
    return (
        "  Option B: Manual\n"
        "  ----------------\n"
        f"  1. Extract all files from this archive directly into {where}\n"
        "  2. Overwrite if asked\n\n"
    )


def _build_readme(
    platform_name: str,
    platform_display: str,
    base_dest: str,
    total_files: int,
    num_systems: int,
    source: str = "full",
    contributors: list[dict] | None = None,
    regions: list[str] | None = None,
    fallback_systems: list[str] | None = None,
    one_per_slot: bool = False,
    undecidable_slots: list[str] | None = None,
    narrowings: list[tuple[str, str]] | None = None,
    system_filter: list[str] | None = None,
    detect: list[dict] | None = None,
    standalone_copies: list[dict] | None = None,
    launch_folders: int = 0,
) -> str:
    """Build a personalized step-by-step README for each platform pack.

    ``launch_folders`` counts the folders a platform that hands each core
    its own system directory gives them, where it names no system.
    """
    narrowings = narrowings or []
    sep = "=" * 50
    scope = (
        f"{launch_folders} launch folders" if launch_folders and not num_systems
        else f"{num_systems} systems"
    )
    header = (
        f"{sep}\n"
        f"  RETROBIOS - {platform_display} BIOS Pack\n"
        f"  {total_files} files for {scope}\n"
        f"{sep}\n\n"
    )

    detect = list(detect or [])
    bios_paths = [str(rule.get("bios_path", "")) for rule in detect]
    if any(rule.get("method") == "card_marker" for rule in detect):
        guide = (
            "INSTALLATION GUIDE\n\n"
            + _card_section(platform_name, platform_display)
            + _card_manual(platform_display)
        )
    else:
        guide = (
            "INSTALLATION GUIDE\n\n"
            + _automatic_section(platform_display, detect, standalone_copies)
            + (pack_manual(platform_name) or _generic_manual(bios_paths))
        )

    if regions:
        region_help = (
            "  - Wrong region? This pack was filtered. Only the\n"
            "    best-matching BIOS was kept per system. Use the\n"
            "    unfiltered pack to play imports.\n"
        )
    else:
        region_help = (
            "  - Wrong region? Some systems have regional BIOS\n"
            "    variants (USA/EUR/JAP). All are included.\n"
        )
    footer = (
        "TROUBLESHOOTING\n\n"
        "  - Core says BIOS missing? Check the exact filename\n"
        "    and make sure it's in the right subfolder.\n"
        f"{region_help}"
        "  - Need help? https://github.com/Abdess/retrobios/issues\n\n"
        f"{sep}\n"
        f"  https://github.com/Abdess/retrobios\n"
        f"{sep}\n"
    )

    source_info = ""
    if source == "platform":
        source_info = (
            "PACK TYPE: Platform Only\n\n"
            f"  This pack contains only files declared by {platform_display}.\n"
            "  Core extras from emulator profiles are not included.\n"
            "  Use the Full pack for maximum coverage.\n\n"
        )
    elif source == "truth":
        source_info = (
            "PACK TYPE: Ground Truth\n\n"
            "  This pack contains files that emulators actually load,\n"
            "  based on source code analysis of emulator profiles.\n"
            "  Independent of platform scraper accuracy.\n\n"
        )

    region_info = ""
    if regions:
        pretty = ", ".join(
            " ".join(w.title() for w in slug.split("-")) for slug in regions
        )
        region_info = (
            "PACK TYPE: Region Filtered\n\n"
            f"  Region priority: {pretty}\n\n"
            "  Only the best-matching BIOS was kept for each system.\n"
        )
        if fallback_systems:
            listed = "\n".join(f"    {_group_label(s)}" for s in fallback_systems)
            region_info += (
                "\n  These systems have no BIOS in those regions, so all\n"
                "  of theirs were kept:\n"
                f"{listed}\n"
            )
        region_info += (
            "\n  This shrinks the pack. It does not change how cores pick\n"
            "  a BIOS: most already select per region from fixed filename\n"
            "  lists driven by the game's region. Loading imports from\n"
            "  another region may need the unfiltered pack.\n\n"
        )

    slot_info = ""
    if one_per_slot:
        slot_info = (
            "  Where a core declares which BIOS it prefers, only that one was\n"
            "  kept for each system and region. Systems whose cores declare no\n"
            "  order keep all of theirs.\n"
        )
        if undecidable_slots:
            slot_info += (
                f"  {len(undecidable_slots)} slot(s) had no declared order.\n"
            )
        slot_info += "\n"

    narrowed = ""
    labels = [label for _tag, label in narrowings]
    if system_filter:
        labels.append(f"systems {', '.join(system_filter)}")
    if labels:
        listed = "".join(f"    {label}\n" for label in labels)
        narrowed = (
            "PACK TYPE: Narrowed\n\n"
            "  This pack holds fewer files than the full one:\n"
            f"{listed}"
            "\n  The unfiltered pack is the one to use when in doubt.\n\n"
        )

    credits = ""
    if contributors:
        credits = "\nCONTRIBUTORS\n\n"
        for cb in contributors:
            username = cb.get("username", "")
            credits += f"  @{username}\n"
        credits += "\n"

    return (
        header + narrowed + source_info + region_info + slot_info
        + guide + credits + footer
    )

def _build_agnostic_rename_readme(
    destination: str,
    original: str,
    alternatives: list[str],
) -> str:
    """Build a README explaining an agnostic file rename."""
    lines = [
        "This file was renamed for compatibility:",
        f"  {destination} <- {original}",
        "",
    ]
    if alternatives:
        lines.append("All variants included in this pack:")
        for alt in sorted(alternatives):
            lines.append(f"  {alt}")
        lines.append("")
        lines.append(f"To use a different variant, rename it to: {destination}")
    return "\n".join(lines) + "\n"


def _group_label(group_id: str) -> str:
    """A region group as a reader names it: the system, or one core's slot."""
    emulator, kind, rest = (group_id.split(":", 2) + ["", ""])[:3]
    if kind == "variant" and rest:
        return f"{rest} ({emulator})"
    return group_id
