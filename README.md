<p align="center">
  <img src=".github/assets/banner.png" alt="RetroBIOS" width="400">
</p>

<p align="center">
  <a href="https://github.com/Abdess/retrobios/actions/workflows/deploy-site.yml"><img src="https://github.com/Abdess/retrobios/actions/workflows/deploy-site.yml/badge.svg" alt="Site"></a>
</p>

BIOS and firmware files for AmberELEC, Batocera, BizHawk, EmuDeck, EmuELEC, KNULLI, Lakka, MiSTer FPGA, MinUI, MyMinUI, NextUI, ROCKNIX, Recalbox, RetroArch, RetroBat, RetroDECK, RetroPie, and RomM, one pack per platform.

A pack holds the files its emulators read, as their source code names them, and leaves out what the emulators install themselves.

## Quick install

Copy one command into your terminal:

```bash
# Linux / macOS / Steam Deck
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex

# Android (Termux, after pkg install python and termux-setup-storage)
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh

# SD card mounted on a PC: --dest is the BIOS folder on the card
# (RetroArch on Switch: retroarch/cores/system, on Android: RetroArch/system)
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system

# Options in PowerShell: anything after | iex goes to iex, so the script runs as a block
&([scriptblock]::Create((irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1))) --check
```

The script auto-detects your platform, downloads only missing files, and verifies checksums.
Every option, and what the installer detects and writes, is on the [installer page](https://abdess.github.io/retrobios/wiki/installer/#passing-options).

## Download BIOS packs

One pack per platform, and it holds everything the platform runs: its own BIOS list plus every file its emulator cores load. Pick your platform, download the ZIP, extract to the BIOS path. The installer above does the same file by file. At a terminal it asks whether to install everything or to choose systems, cores and regions, and `--system`, `--core`, `--region` and `--target` make the same choice without questions. For a bare minimum, build your own pack below.

A pack over 2 GB comes in several parts (`Pack.part1of2.zip`, `Pack.part2of2.zip`), and every part is needed. Each part is an ordinary ZIP: extract them all into the same folder.

Every release ships `SHA256SUMS.txt` and a detached signature of it, checkable against `allowed_signers` in this repository: [verifying a release](https://abdess.github.io/retrobios/wiki/release-process/#verifying-a-release).

The Files column counts what a pack holds once extracted, the figure a file manager shows for the folder. It differs from one platform to the next because a pack carries only what that platform's emulators load. The size is what the files occupy once extracted, and the ZIP itself downloads smaller.

| Platform | Files | Extracted size | Extract to | Download |
|----------|------:|---------------:|-----------|----------|
| AmberELEC | - | - | `/storage/roms/` | Not in v2026.10.10: [use the installer](https://abdess.github.io/retrobios/wiki/installer/) |
| Batocera | 12,810 | 7.5 GB | `/userdata/bios/` | Download: [part 1 of 3](../../releases/download/v2026.10.10/Batocera_43.1_BIOS_Pack.part1of3.zip), [part 2 of 3](../../releases/download/v2026.10.10/Batocera_43.1_BIOS_Pack.part2of3.zip), [part 3 of 3](../../releases/download/v2026.10.10/Batocera_43.1_BIOS_Pack.part3of3.zip) |
| BizHawk | 950 | 2.4 GB | `Firmware/` | [Download](../../releases/download/v2026.10.10/BizHawk_2.11.1_BIOS_Pack.zip) |
| EmuDeck | 620 | 3.2 GB | `~/Emulation/bios/` | [Download](../../releases/download/v2026.10.10/EmuDeck_2.7EA_BIOS_Pack.zip) |
| EmuELEC | - | - | `/storage/roms/` | Not in v2026.10.10: [use the installer](https://abdess.github.io/retrobios/wiki/installer/) |
| KNULLI | - | - | `/userdata/bios/` | Not in v2026.10.10: [use the installer](https://abdess.github.io/retrobios/wiki/installer/) |
| Lakka | 8,504 | 6.0 GB | `/storage/system/` | Download: [part 1 of 2](../../releases/download/v2026.10.10/RetroArch_Lakka_v1.22.2_BIOS_Pack.part1of2.zip), [part 2 of 2](../../releases/download/v2026.10.10/RetroArch_Lakka_v1.22.2_BIOS_Pack.part2of2.zip) |
| MiSTer FPGA | 83 | 29 MB | `/media/fat/games/` | [Download](../../releases/download/v2026.10.10/MiSTer_FPGA_2026-09-29_BIOS_Pack.zip) |
| MinUI | - | - | `<SD card>/` | Not in v2026.10.10: [use the installer](https://abdess.github.io/retrobios/wiki/installer/) |
| MyMinUI | - | - | `<SD card>/` | Not in v2026.10.10: [use the installer](https://abdess.github.io/retrobios/wiki/installer/) |
| NextUI | - | - | `<SD card>/` | Not in v2026.10.10: [use the installer](https://abdess.github.io/retrobios/wiki/installer/) |
| ROCKNIX | 5,804 | 6.1 GB | `/storage/roms/bios/` | Download: [part 1 of 2](../../releases/download/v2026.10.10/ROCKNIX_20261001_BIOS_Pack.part1of2.zip), [part 2 of 2](../../releases/download/v2026.10.10/ROCKNIX_20261001_BIOS_Pack.part2of2.zip) |
| Recalbox | 3,222 | 3.9 GB | `/recalbox/share/bios/` | Download: [part 1 of 2](../../releases/download/v2026.10.10/Recalbox_10.1.1_BIOS_Pack.part1of2.zip), [part 2 of 2](../../releases/download/v2026.10.10/Recalbox_10.1.1_BIOS_Pack.part2of2.zip) |
| RetroArch | 8,504 | 6.0 GB | `system/` | Download: [part 1 of 2](../../releases/download/v2026.10.10/RetroArch_Lakka_v1.22.2_BIOS_Pack.part1of2.zip), [part 2 of 2](../../releases/download/v2026.10.10/RetroArch_Lakka_v1.22.2_BIOS_Pack.part2of2.zip) |
| RetroBat | 12,399 | 7.9 GB | `bios/` | Download: [part 1 of 3](../../releases/download/v2026.10.10/RetroBat_8.2.1_BIOS_Pack.part1of3.zip), [part 2 of 3](../../releases/download/v2026.10.10/RetroBat_8.2.1_BIOS_Pack.part2of3.zip), [part 3 of 3](../../releases/download/v2026.10.10/RetroBat_8.2.1_BIOS_Pack.part3of3.zip) |
| RetroDECK | 7,793 | 8.0 GB | `~/retrodeck/` | Download: [part 1 of 3](../../releases/download/v2026.10.10/RetroDECK_0.10.10b_BIOS_Pack.part1of3.zip), [part 2 of 3](../../releases/download/v2026.10.10/RetroDECK_0.10.10b_BIOS_Pack.part2of3.zip), [part 3 of 3](../../releases/download/v2026.10.10/RetroDECK_0.10.10b_BIOS_Pack.part3of3.zip) |
| RetroPie * | 12,533 | 8.1 GB | `~/RetroPie/BIOS/` | Download: [part 1 of 3](../../releases/download/v2026.10.10/RetroPie_4.8.12_BIOS_Pack.part1of3.zip), [part 2 of 3](../../releases/download/v2026.10.10/RetroPie_4.8.12_BIOS_Pack.part2of3.zip), [part 3 of 3](../../releases/download/v2026.10.10/RetroPie_4.8.12_BIOS_Pack.part3of3.zip) |
| RomM | 1,087 | 1.5 GB | `library/bios/` | [Download](../../releases/download/v2026.10.10/RomM_5.3.1_BIOS_Pack.zip) |

The AmberELEC pack already carries its own `bios/` folder, so it extracts into `/storage/roms/` rather than into the BIOS folder.

The EmuELEC pack already carries its own `bios/` folder, so it extracts into `/storage/roms/` rather than into the BIOS folder.

The MinUI pack already carries its own `Bios/` folder, so it extracts into `<SD card>/` rather than into the BIOS folder.

The MyMinUI pack already carries its own `Bios/` folder, so it extracts into `<SD card>/` rather than into the BIOS folder.

The NextUI pack already carries its own `Bios/` folder, so it extracts into `<SD card>/` rather than into the BIOS folder.

The RetroDECK pack already carries its own `bios/` folder, so it extracts into `~/retrodeck/` rather than into the BIOS folder.

\* Archived: the configuration is kept and packs are still built, but upstream is no longer scraped on a schedule.

## What's included

BIOS, firmware, and system files for consoles from Atari to PlayStation 3.
These are the files an emulator loads from disk instead of carrying inside itself. Some are required to boot a system, others improve accuracy or unlock a feature, and the packs carry both.

Each file is checked the way your platform checks it. Most compare a checksum, the fingerprint of a file's contents, which catches a corrupt or unexpected copy. RetroArch, Lakka and RetroPie check that the file is present under its name without reading its contents (`path_is_valid` in RetroArch's `core_info.c`), and MinUI, NextUI and MyMinUI hand each core its folder without opening a file in it: the Coverage table says which applies to you. Independently of that, the collection records five fingerprints per file, and wherever an emulator's code states an expected size or hash, that value is read from its source and rechecked here.

- **938 files** the platforms' emulators load are still to be found, and 137 more cannot be sourced at all (per-user keys, user-filled slots, dumps nobody has made). Both are named in the [gap analysis](https://abdess.github.io/retrobios/gaps/)
- **18 platforms** supported with platform-specific verification
- **609 emulators** profiled from source (RetroArch cores + standalone)
- **534 systems** handled by those emulators (NES, SNES, PlayStation, Saturn, Dreamcast, ...)
- **19,591 files in the collection**, each with its SHA1, MD5, SHA256, CRC32 and Adler-32 fingerprints: 5,780 system files, 2,872 arcade ROM sets, 10,939 game and engine data files. That is every platform and emulator together, so no pack holds them all: each carries what its own emulators load, counted in the download table
- **555 of 5,780 system files** matched to dump-preservation catalogs (No-Intro, Redump, TOSEC). Arcade sets and engine data fall outside what those catalogs index
- **15.7 GB** for the whole collection

## Supported systems

NES, SNES, Nintendo 64, GameCube, Wii, Game Boy, Game Boy Advance, Nintendo DS, Nintendo 3DS, Switch, PlayStation, PlayStation 2, PlayStation 3, PSP, PS Vita, Mega Drive, Saturn, Dreamcast, Game Gear, Master System, Neo Geo, Atari 2600, Atari 7800, Atari Lynx, Atari ST, MSX, PC Engine, TurboGrafx-16, ColecoVision, Intellivision, Commodore 64, Amiga, ZX Spectrum, Arcade (MAME), and 500+ more.

Full list with per-file details: **[https://abdess.github.io/retrobios/](https://abdess.github.io/retrobios/)**

## Coverage

| Platform | On its BIOS list | Files its emulators load | Checked by |
|----------|-----------------:|-------------------------:|------------|
| AmberELEC | 414/434 | 4,550/4,647 | MD5 hash |
| Batocera | 353/353 | 13,941/15,029 | MD5 hash |
| BizHawk | 118/118 | - | SHA1 hash |
| EmuDeck | 168/168 | 8,173/8,249 | MD5 hash |
| EmuELEC | 544/572 | 4,593/9,911 | MD5 hash |
| KNULLI | 290/290 | 8,897/15,085 | MD5 hash |
| Lakka | 532/532 | 7,600/7,659 | file presence |
| MiSTer FPGA | 81/81 | - | MD5 hash |
| MinUI | 0/0 | 78/79 | file presence |
| MyMinUI | 0/0 | 347/354 | file presence |
| NextUI | 0/0 | 343/348 | file presence |
| ROCKNIX | 38/38 | 7,092/8,360 | MD5 hash |
| Recalbox | 351/351 | 2,746/2,910 | MD5 hash |
| RetroArch | 526/526 | 7,609/7,650 | file presence |
| RetroBat | 348/348 | 4,729/9,194 | MD5 hash |
| RetroDECK | 2,007/2,008 | 6,273/6,524 | MD5 hash |
| RetroPie * | 532/532 | 7,877/12,395 | file presence |
| RomM | 383/383 | 83/846 | MD5 hash |

Each fraction is what the pack has over what is needed, counting required and optional files alike since both ship. The first column is the BIOS list the platform publishes. The second counts files its emulators load that this list never mentions, found by reading their source code, and it is routinely several times larger. A short fraction means files are still missing, and they are named in the [gap analysis](https://abdess.github.io/retrobios/gaps/).
That second number is a floor, not a ceiling: an emulator that accepts any file handed to it names none in its code, so nothing there can be counted.
Checked by is the test your platform runs on its own, replicated here from its source code, so the result matches what you would see in the frontend ([how each one works](https://abdess.github.io/retrobios/wiki/verification-modes/)).

## Build your own pack

Clone the repo and generate packs for any platform, emulator, or system:

```bash
# The pack of a platform, as released
python scripts/generate_pack.py --platform retroarch --output-dir dist/
python scripts/generate_pack.py --platform batocera --output-dir dist/

# Single emulator or system
python scripts/generate_pack.py --emulator dolphin
python scripts/generate_pack.py --system sony-playstation-2

# One region, best first, one file per system and region
python scripts/generate_pack.py --platform retroarch --region us,eu,jp
python scripts/generate_pack.py --platform retroarch --region us --one-per-slot

# Firmware only: the game data a list or a core names is left out, each file named
python scripts/generate_pack.py --platform emuelec --no-game-data

# List available emulators and systems
python scripts/generate_pack.py --list-emulators
python scripts/generate_pack.py --list-systems

# Coverage of this repository's collection per platform, emulator or system
# (it reads database.json and bios/: for your own BIOS folder, run the installer with --check)
python scripts/verify.py --all
python scripts/verify.py --platform batocera
python scripts/verify.py --emulator flycast
python scripts/verify.py --platform retroarch --verbose  # emulator ground truth
```

Requires Python 3.9 or newer and `pyyaml`. The one-line installer alone runs on Python 3.8.

## Documentation site

The [documentation site](https://abdess.github.io/retrobios/) provides:

- **Per-platform pages** with file-by-file verification status and hashes
- **Per-emulator profiles** with source code references for every file
- **Per-system pages** showing which emulators and platforms cover each console
- **Gap analysis** identifying missing files and undeclared core requirements
- **Cross-reference** mapping files across 18 platforms and 609 emulators
- **Versioned data access** through JSON, CSV and SQLite exports with published SHA-256 checksums

## How it works

A platform's BIOS list and an emulator's code are maintained separately. Each list is compared with the emulator source code, file by file where a profile exists, and where the two differ the code decides.

Hashes document what emulator code loads and accepts, not dump provenance. That boundary, and how it relates to preservation catalogs such as No-Intro, is drawn in the [FAQ](https://abdess.github.io/retrobios/wiki/faq/#are-these-files-verified-against-original-hardware-dumps).

1. **Read emulator source code**: trace every file the code loads, its expected hash and size
2. **Cross-reference with platforms**: match against what each platform declares
3. **Build packs**: include baseline files plus what each platform's cores need
4. **Verify**: run platform-native checks and emulator-level validation

## Contributors

<a href="https://github.com/PixNyb"><img src="https://avatars.githubusercontent.com/u/40770831?v=4" width="50" alt="PixNyb" title="PixNyb"></a>
<a href="https://github.com/Takiiiiiii"><img src="https://avatars.githubusercontent.com/u/40776277?v=4" width="50" alt="Takiiiiiii" title="Takiiiiiii"></a>
<a href="https://github.com/Takiiiiiiii"><img src="https://avatars.githubusercontent.com/u/43725718?v=4" width="50" alt="Takiiiiiiii" title="Takiiiiiiii"></a>
<a href="https://github.com/habib256"><img src="https://avatars.githubusercontent.com/u/1260156?v=4" width="50" alt="habib256" title="habib256"></a>
<a href="https://github.com/monster-penguin"><img src="https://avatars.githubusercontent.com/u/266009589?v=4" width="50" alt="monster-penguin" title="monster-penguin"></a>
<a href="https://github.com/zjl88858"><img src="https://avatars.githubusercontent.com/u/29473998?v=4" width="50" alt="zjl88858" title="zjl88858"></a>


## Community tools

- [BIOS Preservation Tool](https://github.com/monster-penguin/BIOS-Preservation-Tool) by [monster-penguin](https://github.com/monster-penguin) - scan, verify, and stage your own BIOS collection using RetroBIOS hash metadata

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

The scripts and tooling are released under the [MIT License](LICENSE).
The BIOS and firmware files are not covered by that license: they are third-party system software, preserved and provided for personal backup, archival, and interoperability with emulation software. [NOTICE](NOTICE) sets out their status and how to ask for a file to be removed.
The reasoning, and where it is weakest, is in the [FAQ](https://abdess.github.io/retrobios/wiki/faq/#is-this-legal).

*Auto-generated on 2026-10-22T08:57:23Z*
