# Getting started

## What are BIOS files?

BIOS files are firmware dumps from original console hardware. Emulators need them to boot games for systems that relied on built-in software (PlayStation, Saturn, Dreamcast, etc.). Without the correct BIOS, the emulator either refuses to start the game or falls back to less accurate software emulation.

## Installation

Three ways to get BIOS files in place. The installer is the one to use,
and the two sections after it are for when it cannot run. For a walkthrough of a
first install with what each step prints, see
[First install](first-install.md).

### Option 1: the installer (recommended)

One command, nothing to clone. The bootstrap verifies `install.py` against the
SHA-256 embedded in it, then the installer detects the platform and its BIOS
directory, downloads only what is missing or incorrect, checks size and
SHA-256/SHA-1 before writing, and installs each file atomically.

```bash
# Linux / macOS / Steam Deck
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex

# Android, from Termux, after pkg install python and termux-setup-storage
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

Running `install.py` directly works the same way and needs nothing beyond
Python 3.8+, the floor both bootstraps enforce before they hand over:

```bash
python install.py
```

Override detection when needed:

```bash
python install.py --platform retroarch --dest ~/custom/bios
python install.py --target switch      # keep only files for that hardware
python install.py --list-systems       # systems a platform holds, with sizes
python install.py --system sony-playstation,nintendo-gba
python install.py --region us,eu,jp    # one BIOS per region, best first
python install.py --no-input           # ask nothing, even at a terminal
python install.py --check              # verify existing files, download nothing
python install.py --list-platforms     # supported platforms and what was detected
python install.py --list-targets       # hardware targets for a platform
python install.py --jobs 4             # parallel downloads (default 8)
python install.py --verbose
python install.py --standalone-copies  # opt in to extra standalone-emulator paths
```

The default flow writes inside the detected platform tree only. Copies into
separate standalone-emulator directories are opt-in, so discovery cannot cause
unexpected writes elsewhere on the machine. Entries the collection cannot
satisfy are reported as safely omitted and the run continues. The installer
never substitutes a same-named file for one a hash-verifying platform would
reject.

Arguments pass through the one-liner too, which is how you target an SD card
mounted on another machine:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

`--dest` is the BIOS folder itself, so on a card it names that folder on the
card: `retroarch/cores/system` for RetroArch on Switch, `RetroArch/system` on
Android.

PowerShell needs another form, the environment overrides and the detection
rules are listed per platform, and the trust boundary is described in full on
the [Installer](installer.md) page.

### Option 2: download.sh (Linux/macOS, from a clone)

Downloads a whole pack rather than the missing files. Needs `curl` and `unzip`:

```bash
bash scripts/download.sh retroarch ~/RetroArch/system/
bash scripts/download.sh --list  # show available packs
```

A pack published in several parts is downloaded part by part and checked
against the SHA-256 the release publishes before anything is extracted.
`python scripts/download.py` does the same on Windows.

### Option 3: manual download

1. Go to the [releases page](https://github.com/Abdess/retrobios/releases)
2. Download the ZIP pack for your platform
3. Extract to the BIOS directory listed below

A pack over 2 GB comes in several parts (`Pack.part1of2.zip`,
`Pack.part2of2.zip`), and every part is needed. Each part is an ordinary ZIP:
extract them all into the same folder. See [Download](../which-pack.md) for
the per-setup instructions.

## BIOS directory by platform

### RetroArch

RetroArch uses the `system_directory` setting in `retroarch.cfg`. Default locations:

| OS | Default path |
|----|-------------|
| Windows (installer) | `%APPDATA%\RetroArch\system\` |
| Windows (portable .7z) | `system\` next to `retroarch.exe` |
| Linux | `~/.config/retroarch/system/` |
| Linux (Flatpak) | `~/.var/app/org.libretro.RetroArch/config/retroarch/system/` |
| macOS | `~/Library/Application Support/RetroArch/system/` |
| Steam Deck (Flatpak) | `~/.var/app/org.libretro.RetroArch/config/retroarch/system/` |
| Android | `/storage/emulated/0/RetroArch/system/` |

Windows has two layouts because the installer stores its data under `%APPDATA%`
while the portable archive keeps everything inside the folder you extracted it
to. If both exist on the machine, the one RetroArch actually reads is the one
shown in the UI.

To check your actual path: open RetroArch, go to **Settings > Directory > System/BIOS**, or look for `system_directory` in `retroarch.cfg`.

### Batocera

```
/userdata/bios/
```

Accessible via network share at `\\BATOCERA\share\bios\` (Windows) or `smb://batocera/share/bios/` (macOS/Linux).

### Recalbox

```
/recalbox/share/bios/
```

Accessible via network share at `\\RECALBOX\share\bios\`.

### RetroBat

```
bios/
```

Relative to the RetroBat installation directory (e.g., `C:\RetroBat\bios\`).

### RetroDECK

```
~/retrodeck/bios/
```

On a MicroSD install the root moves, e.g. `/run/media/mmcblk0p1/retrodeck/bios/`.

The RetroDECK pack is the exception to "extract into the BIOS directory": its
entries already start with `bios/` (and one with `roms/`), so extract it into
`~/retrodeck/` and the files land in the right place. Extracting into
`~/retrodeck/bios/` would create `~/retrodeck/bios/bios/`.

### EmuDeck

```
Emulation/bios/
```

Located inside your Emulation folder. On Steam Deck, typically `~/Emulation/bios/`.

### Lakka

```
/storage/system/
```

Accessible via SSH or Samba.

### RetroPie

```
~/RetroPie/BIOS/
```

RetroPie is archived in this project: its configuration is kept and packs are
still built, but the upstream data is no longer scraped on a schedule. Use the
RetroPie pack, not the RetroArch one: RetroPie inherits RetroArch's systems
but also installs standalone emulators (openMSX, PCSX2, XRoar, Amiberry and
others) whose files only the RetroPie pack carries, laid out for
`~/RetroPie/BIOS/` rather than RetroArch's `system/`.

### ROCKNIX

```
/storage/roms/bios/
```

Accessible via SSH or Samba, like Lakka.

### MiSTer FPGA

```
/media/fat/games/
```

Files go under the per-core subdirectory the MiSTer BIOS database declares
(e.g. `/media/fat/games/3DO/boot.rom`). Only the entries the BIOS database
lists with a download URL are in scope: the rest ship with the MiSTer
distribution and install themselves.

### BizHawk

```
Firmware/
```

Relative to the BizHawk installation directory.

### RomM

BIOS files live in the RomM library under `bios/{platform_slug}/`, one
subfolder per system, and are managed through the web interface. Check the
[RomM documentation](https://github.com/rommapp/romm) for setup details.

## Checking what you installed

`--check` reads the BIOS folder, hashes what is there and writes nothing:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

It ends on a count:

```
Checking existing files...
  5865/5865 present (5865 verified, 0 wrong hash)

  All files up to date.
```

`0 wrong hash` is the line that matters. A number there means a file is present
whose contents the platform will reject, which is how a wrong revision or a bad
dump shows up.

The installer's `--check` hashes every file against the list on every
platform, so it catches a wrong revision even where the frontend would not:
RetroArch, Lakka and RetroPie only check that a file exists, while Batocera,
RetroBat, Recalbox, EmuDeck, RetroDECK, RomM, ROCKNIX and MiSTer FPGA compare
MD5 and BizHawk compares SHA1.

For a single system, or the files a single emulator core reads (`--list-systems`
and `--list-cores` give the names):

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --system sony-playstation
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --emulator beetle_psx
```

`--verbose` names every file still to fetch, wrong hashes included. See [Installer](installer.md)
for every option, and [Tools](tools.md) for the repository's own scripts.

## Next steps

- [FAQ](faq.md) - common questions and troubleshooting
- [Tools](tools.md) - all available scripts and options
- [Architecture](architecture.md) - how the project works internally
