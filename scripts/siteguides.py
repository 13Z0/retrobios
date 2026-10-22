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
        "An emulator app on a Windows, Mac or Linux computer, or on a Steam Deck.",
    ),
    "gaming-os": (
        "Game systems for PCs and small boards",
        "A system that starts straight into a game menu, installed on a PC, a "
        "Raspberry Pi or another single-board computer.",
    ),
    "handheld": (
        "Handhelds and TV boxes",
        "A handheld console or a TV box, and the system it starts when you turn it on.",
    ),
    "fpga": (
        "MiSTer FPGA",
        "A MiSTer board, which recreates console hardware in a chip instead of "
        "emulating it in software.",
    ),
    "server": (
        "Self-hosted game library",
        "A RomM server, which plays your games in a web browser.",
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
        "Open the Start menu, type **PowerShell** and open it. Paste the line "
        "and press Enter.",
        "powershell",
    ),
    "macos": (
        "macOS",
        "Open **Terminal**, in Applications then Utilities. Paste the line and "
        "press Return.",
        "sh",
    ),
    "linux": (
        "Linux",
        "Open a terminal. Paste the line and press Enter.",
        "sh",
    ),
    "steamdeck": (
        "Steam Deck",
        "Switch to Desktop Mode and open **Konsole**, the terminal. Paste the "
        "line with Ctrl+Shift+V and press Enter.",
        "sh",
    ),
    "android": (
        "Android (Termux)",
        "Install **Termux**, then run `pkg install python` and "
        "`termux-setup-storage` once. Paste the line and press Enter.",
        "sh",
    ),
    "device": (
        "On the device",
        "Open a terminal on the device, or connect to it from a computer on the "
        "same network with SSH. Paste the line and press Enter.",
        "sh",
    ),
    "server": (
        "On the server",
        "Open a terminal on the machine that holds the RomM library. Give "
        "`--dest` the `bios` folder of that library.",
        "sh",
    ),
}

# Windows users of a card frontend run the PowerShell form with a drive letter.
CARD_TERMINALS = ("macos", "linux", "windows")


@dataclass(frozen=True)
class Guide:
    """What a platform page says before its file reference."""

    summary: str
    folders: tuple[tuple[str, str], ...]
    run: tuple[str, ...]
    notes: tuple[str, ...] = ()
    dest: str = ""
    on_card: bool = False
    extra_args: dict[str, str] = field(default_factory=dict)


GUIDES: dict[str, Guide] = {
    "retroarch": Guide(
        summary="A multi-system emulator. It loads a different core for each console.",
        folders=(
            ("Windows, installer or portable build", "`system\\` next to `retroarch.exe`"),
            ("Windows, Microsoft Store build",
             "`%LOCALAPPDATA%\\Packages\\1e4cf179-f3c2-404f-b9f3-cb2070a5aad8_*\\LocalState\\system\\`"),
            ("macOS", "`~/Documents/RetroArch/system/`"),
            ("macOS, Steam or portable build", "`~/Library/Application Support/RetroArch/system/`"),
            ("Linux", "`~/.config/retroarch/system/`"),
            ("Linux Flatpak and Steam Deck", "`~/.var/app/org.libretro.RetroArch/config/retroarch/system/`"),
            ("Android", "`RetroArch/system/` on the internal storage"),
            ("Nintendo Switch", "`retroarch/cores/system/` on the SD card"),
        ),
        run=("windows", "macos", "linux", "steamdeck", "android"),
        notes=(
            "RetroArch shows the folder it reads in **Settings > Directory > "
            "System/BIOS**. The same value is the `system_directory` line of "
            "`retroarch.cfg`.",
            "On Windows, `retroarch.cfg` is read next to `retroarch.exe` first, "
            "then from `%APPDATA%\\RetroArch\\`. A `system_directory` left at its "
            "default is the `system` folder next to `retroarch.exe` (`:\\system` "
            "in RetroArch's `frontend/drivers/platform_win32.c`, where `:` stands "
            "for the folder of the program).",
            "LaunchBox and other launchers that start RetroArch use the folder "
            "RetroArch itself reads.",
        ),
    ),
    "batocera": Guide(
        summary="A system that starts straight into a game menu. It runs on PCs, "
        "Raspberry Pi boards and handhelds.",
        folders=(
            ("On Batocera", "`/userdata/bios/`"),
            ("From Windows, over the network", "`\\\\BATOCERA\\share\\bios\\`"),
            ("From macOS or Linux, over the network", "`smb://batocera/share/bios/`"),
        ),
        run=("device",),
        notes=(
            "From a computer on the same network, the network folder is the "
            "easiest way in: download the pack and extract it there.",
        ),
    ),
    "recalbox": Guide(
        summary="A system that starts straight into a game menu and sets up its "
        "emulators itself.",
        folders=(
            ("On Recalbox", "`/recalbox/share/bios/`"),
            ("From Windows, over the network", "`\\\\RECALBOX\\share\\bios\\`"),
        ),
        run=("device",),
        notes=(
            "From a computer on the same network, download the pack and extract "
            "it into the network folder.",
        ),
    ),
    "retrobat": Guide(
        summary="A Windows frontend built on EmulationStation. It comes with "
        "RetroArch and standalone emulators.",
        folders=(
            ("Inside the RetroBat folder", "`bios\\`"),
            ("Default install", "`C:\\RetroBat\\bios\\`"),
        ),
        run=("windows",),
        notes=(
            "RetroBat records its own folder each time it starts, and the "
            "installer reads it there, wherever RetroBat was installed.",
        ),
    ),
    "emudeck": Guide(
        summary="Installs and sets up many emulators, and adds each game to the "
        "Steam library.",
        folders=(
            ("Steam Deck and Linux", "`~/Emulation/bios/`"),
            ("Windows, by default", "`C:\\Emulation\\bios\\`"),
            ("macOS", "`~/Emulation/bios/`"),
        ),
        run=("steamdeck", "linux", "windows", "macos"),
        notes=(
            "The `bios` folder sits inside the Emulation folder chosen in "
            "EmuDeck. When EmuDeck was set up on an SD card, that folder is on "
            "the card. The installer reads the location from EmuDeck's settings "
            "(`emulationPath` in `~/.config/EmuDeck/settings.sh`).",
        ),
    ),
    "lakka": Guide(
        summary="A system built around RetroArch. It starts straight into the "
        "RetroArch menu.",
        folders=(("On Lakka", "`/storage/system/`"),),
        run=("device",),
        notes=("The folder is reachable over SSH and over the network share.",),
    ),
    "retrodeck": Guide(
        summary="One app with its emulators included, installed from Flathub "
        "or Discover.",
        folders=(
            ("Steam Deck and Linux", "`~/retrodeck/bios/`"),
            ("Installed on a microSD card", "for example `/run/media/mmcblk0p1/retrodeck/bios/`"),
        ),
        run=("steamdeck", "linux"),
    ),
    "romm": Guide(
        summary="A web-based game library. It plays games in the browser "
        "through EmulatorJS.",
        folders=(
            ("In the RomM library", "`bios/`, one subfolder per platform"),
            ("With `firmware: \"{platform}/bios\"` in `config.yml`",
             "`<platform>/bios/` for each platform"),
        ),
        run=("server",),
        dest="/path/to/library/bios",
        notes=("BIOS files can also be managed in RomM's web interface.",),
    ),
    "rocknix": Guide(
        summary="A Linux system for ARM and x86 handhelds, among them the "
        "RG35XX (H700) and the RG552 (RK3399).",
        folders=(("On ROCKNIX", "`/storage/roms/bios/`"),),
        run=("device",),
        notes=("The folder is reachable over SSH and over the network share.",),
    ),
    "amberelec": Guide(
        summary="A Linux system for Anbernic handhelds: RG351P, RG351V, "
        "RG351MP, RG353, RG503 and RG552.",
        folders=(("On AmberELEC", "`/storage/roms/`"),),
        run=("device",),
        notes=(
            "The AmberELEC list names files under `/storage/roms`: `bios/` for "
            "most, `neogeo/` for the rest. The pack holds those folders, so it "
            "is extracted into `/storage/roms/` itself.",
        ),
    ),
    "emuelec": Guide(
        summary="A Linux system for Amlogic TV boxes, the Odroid Go Advance, "
        "GameForce and RK356x devices.",
        folders=(("On EmuELEC", "`/storage/roms/`"),),
        run=("device",),
        notes=(
            "The EmuELEC list names files under `/storage/roms`: `bios/` for "
            "most, `neogeo/` and `ports/` for the rest. The pack holds those "
            "folders, so it is extracted into `/storage/roms/` itself.",
        ),
    ),
    "knulli": Guide(
        summary="A system for handhelds based on Batocera, among them the "
        "RG35XX family (H700) and the TrimUI Smart Pro.",
        folders=(("On KNULLI", "`/userdata/bios/`"),),
        run=("device",),
    ),
    "retropie": Guide(
        summary="Scripts that install RetroArch and standalone emulators on "
        "Raspberry Pi OS.",
        folders=(("On the Raspberry Pi", "`~/RetroPie/BIOS/`"),),
        run=("device",),
        notes=(
            "Use the RetroPie pack, not the RetroArch one. RetroPie also "
            "installs standalone emulators (openMSX, PCSX2, XRoar, Amiberry and "
            "others) whose files only the RetroPie pack carries.",
            "A stock RetroPie 4.8 image ships Python 3.7, older than the "
            "installer needs. On it, download the RetroPie pack and extract it "
            "into `~/RetroPie/BIOS/`.",
            "RetroPie is archived in this project: its configuration is kept and "
            "its pack is still built, but its upstream list is no longer read "
            "again on a schedule.",
        ),
    ),
    "bizhawk": Guide(
        summary="A multi-system emulator with rerecording and debugging tools, "
        "hosted by TASVideos.",
        folders=(
            ("Inside the BizHawk folder", "`Firmware\\`"),
            ("Default place the installer looks", "`%USERPROFILE%\\BizHawk\\Firmware\\`"),
        ),
        run=("windows", "linux"),
    ),
    "misterfpga": Guide(
        summary="FPGA cores that recreate console hardware on a DE10-Nano board.",
        folders=(("On the MiSTer SD card", "`/media/fat/games/`, one subfolder per core"),),
        run=("device",),
        notes=(
            "Each file goes in the subfolder of its core, for example "
            "`/media/fat/games/3DO/boot.rom`. Only the files the MiSTer BIOS "
            "database lists with a download address are in the pack. The others "
            "come with the MiSTer distribution and install themselves.",
        ),
    ),
    "minui": Guide(
        summary="A frontend for handhelds that runs from the SD card.",
        folders=(("On the SD card", "`Bios/<TAG>/`, one folder per console"),),
        run=CARD_TERMINALS,
        on_card=True,
        notes=(
            "MinUI gives each console its own folder, named after the tag in "
            "parentheses of the Roms folder the game sits in. A game in "
            "`Roms/Sony PlayStation (PS)/` runs with `Bios/PS/` as its BIOS "
            "folder, one in `Roms/Game Boy (GB)/` with `Bios/GB/` (MinUI "
            "`workspace/all/minarch/minarch.c:2943`). The pack holds the `Bios/` "
            "folder: extract it at the root of the card.",
            "Without `--platform`, the installer reads the release name MinUI "
            "writes in `.system/version.txt` on the card. A console started by a "
            "standalone program instead of a libretro core (DraStic for NDS, "
            "native PICO-8) is not covered.",
        ),
    ),
    "nextui": Guide(
        summary="A frontend for handhelds derived from MinUI. It runs from the SD card.",
        folders=(("On the SD card", "`Bios/<TAG>/`, one folder per console"),),
        run=CARD_TERMINALS,
        on_card=True,
        notes=(
            "NextUI gives each console its own folder, named after the tag in "
            "parentheses of the Roms folder the game sits in: `Bios/PS/` for "
            "`Roms/Sony PlayStation (PS)/` (NextUI "
            "`workspace/all/minarch/ma_core.c:76`). The pack holds the `Bios/` "
            "folder: extract it at the root of the card.",
        ),
    ),
    "myminui": Guide(
        summary="A frontend for handhelds derived from MinUI. It runs from the SD card.",
        folders=(("On the SD card", "`Bios/<TAG>/`, one folder per console"),),
        run=CARD_TERMINALS,
        on_card=True,
        notes=(
            "MyMinUI gives each console its own folder, named after the tag in "
            "parentheses of the Roms folder the game sits in: `Bios/PS/` for "
            "`Roms/Sony PlayStation (PS)/` (MyMinUI "
            "`workspace/all/minarch/minarch.c:3532`). The pack holds the `Bios/` "
            "folder: extract it at the root of the card.",
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


TRUST_NOTE = (
    "The line downloads a small script from this project on GitHub. The script "
    "checks the installer against a fingerprint it carries, then the installer "
    "downloads the files your setup needs and checks each one before saving it. "
    "It writes only inside your BIOS folder. A BIOS file whose contents differ "
    "from the expected one is replaced with a checked copy, and files it does "
    "not know are never moved or deleted. Games and saves are never touched."
)


def trust_box(prefix: str, indent: str = "") -> list[str]:
    """What the command does, under every command on a guide page."""
    return [
        f'{indent}??? note "What this command does"',
        "",
        f"{indent}    {TRUST_NOTE} "
        f"[Read the script first]({prefix}wiki/faq.md#is-piping-the-installer-into-a-shell-safe).",
        "",
    ]
