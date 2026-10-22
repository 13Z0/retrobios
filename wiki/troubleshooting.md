---
level: start
---

# When a game does not start

Start here when a game does not start, or says that a BIOS file is missing.
Each section is one check, in the order that finds the cause soonest.

## Game won't start / black screen

Most launch failures are caused by a missing or incorrect BIOS file.

### Check what is in your BIOS folder

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

It hashes what is there and writes nothing. Anything counted as needing
downloading is absent, and a required file that is absent means the core cannot
start games for that system at all. `--system` narrows the check to one
system (`--list-systems` names them):

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --system sony-playstation --verbose
```

### Check if the hash matches

The count `--check` prints says how many files are present with contents the
platform does not accept:

```
Checking existing files...
  1/109 present (0 verified, 1 wrong hash)
```

With `--verbose` the list under it names each one (`wrong hash: scph5501.bin`).
The check hashes every file, on RetroArch too, where the frontend itself only
looks for the name. Common causes:

- Wrong region (a PAL BIOS instead of NTSC, or vice versa)
- Wrong hardware revision (e.g. SCPH-5501 vs SCPH-1001 for PlayStation)
- Corrupted download

Running the same command without `--check` replaces each file with a wrong
hash by a verified copy, and keeps yours in `retrobios-backup` beside the BIOS
folder ([putting it back](installer.md#files-you-already-had)). Each system page on the site also lists the expected
hashes, to compare against your file's MD5 or SHA1 by hand.

### Wrong region BIOS

Some cores require region-specific BIOS files. A Japanese BIOS won't boot
North American games on cores that enforce region matching. Check the emulator
profile for your core to see which regions are supported and which files
correspond to each.

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
RetroArch, Lakka, RetroPie, MinUI, NextUI and MyMinUI only check that a file
exists, while Batocera, RetroBat, Recalbox, EmuDeck, RetroDECK, RomM, ROCKNIX,
KNULLI, AmberELEC, EmuELEC and MiSTer FPGA compare
MD5 and BizHawk compares SHA1.

For a single system, or the files a single emulator core reads (`--list-systems`
and `--list-cores` give the names):

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --system sony-playstation
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --emulator beetle_psx
```

`--verbose` names every file still to fetch, wrong hashes included.
Details: [All installer options](installer.md).

## BIOS not found by emulator

The file exists on disk, but the emulator reports it as missing.

### Wrong directory

Each platform expects BIOS files in a specific base directory:

- RetroArch: `system/` inside the RetroArch directory
- Lakka: `/storage/system/`
- Batocera: `/userdata/bios/`
- Recalbox: `/recalbox/share/bios/`
- RetroPie: `~/RetroPie/BIOS/`
- ROCKNIX: `/storage/roms/bios/`
- KNULLI: `/userdata/bios/`
- AmberELEC and EmuELEC: `/storage/roms/` (the pack holds `bios/` and the other folders their lists name)
- MiSTer FPGA: `/media/fat/games/`
- BizHawk: `Firmware/` inside the BizHawk directory
- EmuDeck: `~/Emulation/bios/`
- RetroDECK: `~/retrodeck/bios/`
- RetroBat: `bios/` inside the RetroBat directory
- MinUI, NextUI, MyMinUI: `Bios/<TAG>/` at the root of the SD card, one folder
  per tag in parentheses of the Roms folders (`Bios/PS/` for
  `Roms/Sony PlayStation (PS)/`)

Some cores expect files in subdirectories (e.g. `dc/` for Dreamcast, `pcsx2/bios/`
for PlayStation 2). Check the `path:` field in the emulator profile for the exact
expected location relative to the base directory.

### Wrong filename

Cores match BIOS files by exact filename. If a core expects `scph5501.bin` and your
file is named `SCPH-5501.BIN`, it won't be found on platforms that do exact name matching.

Check the emulator profile for the expected filename and any aliases listed under
`aliases:`. Aliases are alternative names that the core also accepts.

### Case sensitivity

Linux filesystems are case-sensitive. A file named `Bios.ROM` won't match a lookup
for `bios.rom`. Windows and macOS are case-insensitive by default, so the same
file works there but fails on Linux.

Batocera's verification uses `casefold()` for case-insensitive matching, but
the actual emulator may still require exact case. When in doubt, use the exact
filename from the emulator profile.

The RetroArch pack is built for every system RetroArch runs on, so it holds one
name per file: `BIOS.col`, never `bios.col` beside it, since extracting both on
Windows or macOS would overwrite one with the other. On Linux, Mesen-CE opens
`bios.col` and NP2kai opens the lowercase `np2kai/2608_*.wav`, which the pack
only holds as `BIOS.col` and `2608_*.WAV`. The installer checks whether the
BIOS directory keeps case and, if it does, also fetches these names. On Linux, use the installer
rather than extracting the ZIP by hand.

The reverse happens on Batocera and Recalbox, whose lists hold both spellings
because their systems keep case: NP2kai reads `np2kai/bios.rom` and
`np2kai/BIOS.ROM` as two different files. Written to a folder that folds case
(the share opened from Windows, an exFAT card), one overwrites the other. The
installer checks the folder first, keeps one file per name there and lists
the ones it left out.

## Installation script fails

Problems with `install.py`, `install.sh`, or `download.sh`.

### Network issues

The installer fetches its manifest, then downloads only the repository or
`large-files` release objects it needs. If a download fails:

- Check your internet connection
- Verify that `https://github.com` is reachable
- If behind a proxy, set `HTTPS_PROXY` in your environment
- Try again later if GitHub is experiencing issues

### Permission denied

The installer needs write access to the target directory.

- On Linux/macOS: check directory ownership (`ls -la`) and run with appropriate
  permissions. Avoid running as root unless the target directory requires it.
- On Windows: run PowerShell as Administrator if installing to a protected directory.
- A file system mounted read-only refuses root too, so the installer does not
  suggest sudo for it. Linux remounts a card or drive read-only after an error
  (an unclean removal, for one): check it (`fsck`, or `chkdsk` on Windows) and
  mount it read-write again.

A file already in the BIOS folder with the read-only attribute is replaced
like any other: on Windows the installer clears the attribute before putting
the verified copy in place. `file in place is read-only` only appears when
Windows refuses that too, which a file owned by another account can cause.

### Platform not detected

The installer finds your platform from the config files it knows. If it finds
none, name the platform and its BIOS folder.

Linux, macOS, Android (Termux):

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retroarch --dest ~/.config/retroarch/system
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform batocera --dest /userdata/bios
```

Windows (PowerShell):

```powershell
&([scriptblock]::Create((irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1))) `
  --platform retroarch --dest C:\RetroArch-Win64\system
```

`--list-platforms` lists the platforms the installer knows and the ones it
finds here, and `--help` lists the options with examples.

### Manifest or file not found

The one-line bootstrap verifies the downloaded installer before running it, and
the installer reads its manifests from the same revision. A platform name is
checked before anything is downloaded, so a "No file list" message comes from
the revision: `RETROBIOS_REF` naming a tag that does not exist, or one older
than the platform. Unset it, or pick a tag from the releases page. What a tag
holds and for how long is under
Details: [environment overrides](installer.md#environment-overrides).

### Manual pack not found in release

If the installer reports that no pack exists for your platform, check available
releases:

```bash
python scripts/download.py --list
# or
bash scripts/download.sh --list
```

A pack's name lists every platform it serves. Releases up to v2026.10.10
ship RetroArch and Lakka as one `RetroArch_Lakka` pack, and the next ones give
Lakka its own, since Lakka runs on Linux where two names that differ by case
are two files. The installer and the download scripts find the right pack
either way.

### A pack in several parts will not extract

Parts named `Pack.part1of2.zip` are ordinary ZIPs: extract each one into the
same folder, and check that all of them were downloaded, the name says how
many there are.

`scripts/download.sh` and `scripts/download.py` download every part and check
it against the release's checksums before extracting. Both stage inside the
destination directory, never in `/tmp`, which is a RAM disk on most of these
systems.
## Checks of the collection itself

<span id="hash-mismatch-untested"></span><span id="pack-is-missing-files"></span><span id="verifypy-reports-errors"></span>

`scripts/verify.py` checks this project's own collection against each
platform, from a clone of the repository. It never reads your BIOS folder.
Reference: [Reading verify.py output](verification-modes.md#hash-mismatch-untested).

!!! tip "Next"

    - The page of your setup, from [Get your files](../which-pack.md): its folder
      and how to check it
    - [FAQ](faq.md), for the questions people ask before installing
