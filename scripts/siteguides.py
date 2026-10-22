"""The guide part of the site: where each setup keeps its BIOS files.

A platform page opens on what someone holding the device needs: the folder,
the command, the pack, how to check. The registry says which group of
devices a platform belongs to (`device_group`), so the device picker and the
navigation list every registered platform. The prose below is per platform,
and a test holds that every registered platform has an entry.

Every path here is one the installer detects, the registry records or the
platform documents. Menu labels of third-party software are named only where
this project already relies on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

INSTALL_BASE = "https://raw.githubusercontent.com/Abdess/retrobios/main"

# Order of the groups on the picker, in the navigation and on the home page.
DEVICE_GROUPS: dict[str, tuple[str, str]] = {
    "computer": (
        "Computers and Steam Deck",
        "An emulator app on a computer or a Steam Deck.",
    ),
    "gaming-os": (
        "Game systems for PCs and small boards",
        "A system that starts straight into a game menu, on a PC or a Raspberry Pi.",
    ),
    "handheld": (
        "Handhelds and TV boxes",
        "The system your handheld or TV box starts.",
    ),
    "fpga": (
        "MiSTer FPGA",
        "A board that recreates old consoles in hardware, not in software.",
    ),
    "server": (
        "Self-hosted game library",
        "A RomM server. It plays games in your web browser.",
    ),
}

# Section anchors on the device picker. Two keep the anchors the page had
# before it was grouped by device, which links outside the site may use.
GROUP_ANCHORS = {
    "computer": "computers-and-steam-deck",
    "gaming-os": "game-systems",
    "handheld": "handhelds",
    "fpga": "fpga",
    "server": "self-hosted",
}

# Setups that are not platforms of their own: they run one, and use its pack.
# (setup, what it is, platform it uses, runs on, BIOS folder)
ALSO_RUNS: dict[str, tuple[tuple[str, str, str, str, str], ...]] = {
    "computer": (
        ("LaunchBox", "A game library and launcher. It starts RetroArch or "
         "standalone emulators.", "retroarch", "Windows",
         "The folder of the RetroArch it starts"),
    ),
    "handheld": (
        ("Android (Retroid Pocket, AYN Odin and others)", "RetroArch for Android.",
         "retroarch", "Android", "`RetroArch/system/` on the internal storage"),
    ),
}

# Ways to run the installer. Each tab names how to open a terminal there,
# the shell the command is written for, and the options it needs.
TERMINALS: dict[str, tuple[str, str, str]] = {
    "windows": (
        "Windows",
        "Open the Start menu. Type **PowerShell** and open it. Paste the line. "
        "Press Enter.",
        "powershell",
    ),
    "macos": (
        "macOS",
        "Open **Terminal**, in Applications, then Utilities. Paste the line. "
        "Press Return.",
        "sh",
    ),
    "linux": (
        "Linux",
        "Open a terminal. Paste the line. Press Enter.",
        "sh",
    ),
    "steamdeck": (
        "Steam Deck",
        "Switch to Desktop Mode. Open **Konsole**. Paste with Ctrl+Shift+V. "
        "Press Enter.",
        "sh",
    ),
    "android": (
        "Android (Termux)",
        "Install **Termux**, a terminal app. Run `pkg install python` and "
        "`termux-setup-storage` once. Then paste the line. Press Enter.",
        "sh",
    ),
    "device": (
        "On the device",
        "Open a terminal on the device. Or connect from a computer with SSH, a "
        "terminal over the network. Paste the line. Press Enter.",
        "sh",
    ),
    "server": (
        "On the server",
        "Open a terminal on the machine that holds your RomM library. Put the "
        "path of its `bios` folder after `--dest`.",
        "sh",
    ),
}

# Windows users of a card frontend run the PowerShell form with a drive letter.
CARD_TERMINALS = ("macos", "linux", "windows")


@dataclass(frozen=True)
class Guide:
    """What a platform page says before its file reference.

    `folders` are the common cases, `more_folders` the rarer builds shown
    folded under them. `notes` stay in the guide, one plain fact each.
    `reference_notes` carry the source citations and the details a reader
    checking the work wants, and open the Reference part of the page.
    `download_hint` is one more bullet under the pack download.
    """

    summary: str
    folders: tuple[tuple[str, str], ...]
    run: tuple[str, ...]
    notes: tuple[str, ...] = ()
    dest: str = ""
    on_card: bool = False
    extra_args: dict[str, str] = field(default_factory=dict)
    more_folders: tuple[tuple[str, str], ...] = ()
    reference_notes: tuple[str, ...] = ()
    download_hint: str = ""


NETWORK_HINT = "From a computer, use the network folder above."

GUIDES: dict[str, Guide] = {
    "retroarch": Guide(
        summary="An app that runs many consoles. It uses a different **core**, "
        "a small emulator, for each one.",
        folders=(
            ("Windows", "`system\\` next to `retroarch.exe`"),
            ("macOS", "`~/Documents/RetroArch/system/`"),
            ("Linux", "`~/.config/retroarch/system/`"),
            ("Steam Deck and Linux Flatpak", "`~/.var/app/org.libretro.RetroArch/config/retroarch/system/`"),
            ("Android", "`RetroArch/system/` on the internal storage"),
            ("Nintendo Switch", "`retroarch/cores/system/` on the SD card"),
        ),
        more_folders=(
            ("Windows, Microsoft Store build",
             "`%LOCALAPPDATA%\\Packages\\1e4cf179-f3c2-404f-b9f3-cb2070a5aad8_*\\LocalState\\system\\`"),
            ("macOS, Steam or portable build", "`~/Library/Application Support/RetroArch/system/`"),
        ),
        run=("windows", "macos", "linux", "steamdeck", "android"),
        notes=(
            "**Not sure?** In RetroArch, open **Settings > Directory > "
            "System/BIOS**. It shows the folder.",
        ),
        reference_notes=(
            "RetroArch reads its BIOS folder from the `system_directory` line of "
            "`retroarch.cfg`, the value **Settings > Directory > System/BIOS** "
            "shows.",
            "On Windows, `retroarch.cfg` is read next to `retroarch.exe` first, "
            "then from `%APPDATA%\\RetroArch\\`. A `system_directory` left at its "
            "default is the `system` folder next to `retroarch.exe` (`:\\system` "
            "in RetroArch's `frontend/drivers/platform_win32.c`, where `:` stands "
            "for the folder of the program).",
        ),
    ),
    "batocera": Guide(
        summary="A system that starts straight into a game menu.",
        folders=(
            ("On Batocera", "`/userdata/bios/`"),
            ("From Windows, over your home network", "`\\\\BATOCERA\\share\\bios\\`"),
            ("From a Mac or Linux, over your home network", "`smb://batocera/share/bios/`"),
        ),
        run=("device",),
        download_hint=NETWORK_HINT,
    ),
    "recalbox": Guide(
        summary="A system that starts straight into a game menu and sets up its "
        "emulators.",
        folders=(
            ("On Recalbox", "`/recalbox/share/bios/`"),
            ("From Windows, over your home network", "`\\\\RECALBOX\\share\\bios\\`"),
        ),
        run=("device",),
        download_hint=NETWORK_HINT,
    ),
    "retrobat": Guide(
        summary="A game menu for Windows. It comes with RetroArch and other "
        "emulators.",
        folders=(
            ("Inside the RetroBat folder", "`bios\\`"),
            ("Default install", "`C:\\RetroBat\\bios\\`"),
        ),
        run=("windows",),
        notes=("The command finds RetroBat wherever you installed it.",),
        reference_notes=(
            "RetroBat records its own folder each time it starts, and the "
            "installer reads it there.",
        ),
    ),
    "emudeck": Guide(
        summary="Installs your emulators and adds your games to Steam.",
        folders=(
            ("Steam Deck and Linux", "`~/Emulation/bios/`"),
            ("Windows, by default", "`C:\\Emulation\\bios\\`"),
            ("macOS", "`~/Emulation/bios/`"),
        ),
        run=("steamdeck", "linux", "windows", "macos"),
        notes=(
            "The `bios` folder is inside the Emulation folder you chose in "
            "EmuDeck. If you chose the SD card, it is on the card.",
        ),
        reference_notes=(
            "The installer reads the Emulation folder from EmuDeck's settings "
            "(`emulationPath` in `~/.config/EmuDeck/settings.sh`).",
        ),
    ),
    "lakka": Guide(
        summary="A system that starts straight into RetroArch.",
        folders=(("On Lakka", "`/storage/system/`"),),
        run=("device",),
        notes=("You can also reach the folder from a computer, over your home network.",),
        reference_notes=("The folder is reachable over SSH and over the network share.",),
    ),
    "retrodeck": Guide(
        summary="One app with its emulators inside. Install it from Flathub or "
        "Discover.",
        folders=(
            ("Steam Deck and Linux", "`~/retrodeck/bios/`"),
            ("Installed on a microSD card", "for example `/run/media/mmcblk0p1/retrodeck/bios/`"),
        ),
        run=("steamdeck", "linux"),
    ),
    "romm": Guide(
        summary="A game library on your own server. It plays games in the web "
        "browser.",
        folders=(("In the RomM library", "`bios/`, one subfolder per platform"),),
        more_folders=(
            ("With `firmware: \"{platform}/bios\"` in `config.yml`",
             "`<platform>/bios/` for each platform"),
        ),
        run=("server",),
        dest="/path/to/library/bios",
        notes=("You can also add BIOS files in RomM's web page.",),
    ),
    "rocknix": Guide(
        summary="A system for handhelds.",
        folders=(("On ROCKNIX", "`/storage/roms/bios/`"),),
        run=("device",),
        notes=("You can also reach the folder from a computer, over your home network.",),
        reference_notes=(
            "ROCKNIX runs on ARM and x86 handhelds, among them the RG35XX (H700) "
            "and the RG552 (RK3399). The folder is reachable over SSH and over "
            "the network share.",
        ),
    ),
    "amberelec": Guide(
        summary="A system for Anbernic handhelds.",
        folders=(("On AmberELEC", "`/storage/roms/`"),),
        run=("device",),
        reference_notes=(
            "AmberELEC runs on the RG351P, RG351V, RG351MP, RG353, RG503 and "
            "RG552. Its list names files under `/storage/roms`: `bios/` for most, "
            "`neogeo/` for the rest. The pack holds those folders.",
        ),
    ),
    "emuelec": Guide(
        summary="A system for TV boxes and some handhelds.",
        folders=(("On EmuELEC", "`/storage/roms/`"),),
        run=("device",),
        reference_notes=(
            "EmuELEC runs on Amlogic TV boxes, the Odroid Go Advance, GameForce "
            "and RK356x devices. Its list names files under `/storage/roms`: "
            "`bios/` for most, `neogeo/` and `ports/` for the rest. The pack "
            "holds those folders.",
        ),
    ),
    "knulli": Guide(
        summary="A system for handhelds, based on Batocera.",
        folders=(("On KNULLI", "`/userdata/bios/`"),),
        run=("device",),
        reference_notes=(
            "KNULLI runs on handhelds among them the RG35XX family (H700) and the "
            "TrimUI Smart Pro.",
        ),
    ),
    "retropie": Guide(
        summary="Installs RetroArch and other emulators on a Raspberry Pi.",
        folders=(("On the Raspberry Pi", "`~/RetroPie/BIOS/`"),),
        run=("device",),
        notes=(
            "Use the RetroPie pack, not the RetroArch one. On RetroPie 4.8, "
            "Python is too old for the command: use the pack there.",
        ),
        reference_notes=(
            "RetroPie also installs standalone emulators (openMSX, PCSX2, XRoar, "
            "Amiberry and others) whose files only the RetroPie pack carries.",
            "RetroPie is archived in this project: its configuration is kept and "
            "its pack is still built, but its upstream list is no longer read "
            "again on a schedule.",
        ),
    ),
    "bizhawk": Guide(
        summary="An emulator app for many consoles, from TASVideos.",
        folders=(
            ("Inside the BizHawk folder", "`Firmware\\`"),
            ("Default place the installer looks", "`%USERPROFILE%\\BizHawk\\Firmware\\`"),
        ),
        run=("windows", "linux"),
        reference_notes=("BizHawk carries rerecording and debugging tools.",),
    ),
    "misterfpga": Guide(
        summary="A board that recreates old consoles in hardware, not in software.",
        folders=(("On the MiSTer SD card", "`/media/fat/games/`, one subfolder per core"),),
        run=("device",),
        notes=(
            "Each file goes in its console's folder, like "
            "`/media/fat/games/3DO/boot.rom`.",
        ),
        reference_notes=(
            "MiSTer runs FPGA cores on a DE10-Nano board. Only the files the "
            "MiSTer BIOS database lists with a download address are in the pack. "
            "The others come with the MiSTer distribution and install themselves.",
        ),
    ),
    "minui": Guide(
        summary="A simple game menu for handhelds. It runs from the SD card.",
        folders=(("On the SD card", "`Bios/<TAG>/`, one folder per console"),),
        run=CARD_TERMINALS,
        on_card=True,
        notes=(
            "Each console has its own folder, named after the tag in its Roms "
            "folder. `Roms/Sony PlayStation (PS)/` uses `Bios/PS/`.",
        ),
        reference_notes=(
            "MinUI gives each console its own folder, named after the tag in "
            "parentheses of the Roms folder the game sits in. A game in "
            "`Roms/Game Boy (GB)/` runs with `Bios/GB/` as its BIOS folder (MinUI "
            "`workspace/all/minarch/minarch.c:2943`).",
            "Without `--platform`, the installer reads the release name MinUI "
            "writes in `.system/version.txt` on the card. A console started by a "
            "standalone program instead of a libretro core (DraStic for NDS, "
            "native PICO-8) is not covered.",
        ),
    ),
    "nextui": Guide(
        summary="A game menu for handhelds, based on MinUI. It runs from the SD card.",
        folders=(("On the SD card", "`Bios/<TAG>/`, one folder per console"),),
        run=CARD_TERMINALS,
        on_card=True,
        notes=(
            "Each console has its own folder, named after the tag in its Roms "
            "folder. `Roms/Sony PlayStation (PS)/` uses `Bios/PS/`.",
        ),
        reference_notes=(
            "NextUI gives each console its own folder, named after the tag in "
            "parentheses of the Roms folder the game sits in (NextUI "
            "`workspace/all/minarch/ma_core.c:76`).",
        ),
    ),
    "myminui": Guide(
        summary="A game menu for handhelds, based on MinUI. It runs from the SD card.",
        folders=(("On the SD card", "`Bios/<TAG>/`, one folder per console"),),
        run=CARD_TERMINALS,
        on_card=True,
        notes=(
            "Each console has its own folder, named after the tag in its Roms "
            "folder. `Roms/Sony PlayStation (PS)/` uses `Bios/PS/`.",
        ),
        reference_notes=(
            "MyMinUI gives each console its own folder, named after the tag in "
            "parentheses of the Roms folder the game sits in (MyMinUI "
            "`workspace/all/minarch/minarch.c:3532`).",
        ),
    ),
}


def install_command(shell: str, args: str = "") -> str:
    """The one-liner for a shell, with installer options when given.

    PowerShell passes options only through a scriptblock: `irm ... | iex`
    takes none.
    """
    if shell == "sh":
        return f"curl -fsSL {INSTALL_BASE}/install.sh | sh" + (f" -s -- {args}" if args else "")
    if args:
        return f"&([scriptblock]::Create((irm {INSTALL_BASE}/install.ps1))) {args}"
    return f"irm {INSTALL_BASE}/install.ps1 | iex"


def _fence(shell: str, command: str, indent: str) -> list[str]:
    return [f"{indent}```{shell}", f"{indent}{command}", f"{indent}```"]


def terminal_tabs(
    tabs: tuple[str, ...], args: dict[str, str], indent: str = "", steps: bool = True,
) -> list[str]:
    """Content tabs, one per place the command runs, each with how to open
    the terminal there. Tab labels are shared across the site, so the reader's
    choice carries from page to page."""
    lines: list[str] = []
    for key in tabs:
        label, how, shell = TERMINALS[key]
        lines += [f'{indent}=== "{label}"', ""]
        if steps:
            lines += [f"{indent}    {how}", ""]
        lines += _fence(shell, install_command(shell, args.get(key, "")), indent + "    ")
        lines.append("")
    return lines


def platform_args(name: str, guide: Guide) -> dict[str, str]:
    """The options of the command on a platform page, per tab."""
    args: dict[str, str] = {}
    for key in guide.run:
        if guide.on_card:
            card = "E:\\" if key == "windows" else "/path/to/card"
            args[key] = f"--platform {name} --dest {card}"
        elif guide.dest:
            args[key] = f"--platform {name} --dest {guide.dest}"
        else:
            args[key] = f"--platform {name}"
    return args


TRUST_ITEMS = (
    "It checks each file before saving it.",
    "It writes only in your BIOS folder.",
    "It never touches games or saves.",
    "It never moves or deletes files it does not know.",
    "It keeps any file it replaces in a `retrobios-backup` folder, beside your "
    "BIOS folder. The same command with `--restore` puts it back.",
)


def trust_box(prefix: str, indent: str = "") -> list[str]:
    """Is it safe, under every command on a guide page: five plain facts."""
    return [
        f'{indent}??? note "Is it safe?"',
        "",
        *(f"{indent}    - {item}" for item in TRUST_ITEMS),
        "",
        f"{indent}    [Read the script first]({prefix}wiki/faq.md#is-piping-the-installer-into-a-shell-safe)",
        "",
    ]
