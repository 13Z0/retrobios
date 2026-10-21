# Troubleshooting

Diagnosis guide organized by symptom. Each section describes what to check and how to fix it.

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
hash by a verified copy. Each system page on the site also lists the expected
hashes, to compare against your file's MD5 or SHA1 by hand.

### Wrong region BIOS

Some cores require region-specific BIOS files. A Japanese BIOS won't boot
North American games on cores that enforce region matching. Check the emulator
profile for your core to see which regions are supported and which files
correspond to each.

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
- MiSTer FPGA: `/media/fat/games/`
- BizHawk: `Firmware/` inside the BizHawk directory
- EmuDeck: `~/Emulation/bios/`
- RetroDECK: `~/retrodeck/bios/`
- RetroBat: `bios/` inside the RetroBat directory

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
BIOS directory keeps case and, if it does, also fetches these names. If you
extract the ZIP on Linux by hand, take the Lakka pack instead: it is the
RetroArch pack with both spellings. Releases up to v2026.10.10 have no Lakka
pack of their own (`RetroArch_Lakka_*_BIOS_Pack.zip` serves both and holds one
spelling), so with those use the installer.

The reverse happens on Batocera and Recalbox, whose lists hold both spellings
because their systems keep case: NP2kai reads `np2kai/bios.rom` and
`np2kai/BIOS.ROM` as two different files. Written to a folder that folds case
(the share opened from Windows, an exFAT card), one overwrites the other. The
installer checks the folder first, keeps one file per name there and lists
the ones it left out.

## Hash mismatch / UNTESTED

This section and the two after it are about this repository's own tools, run
from a clone. `scripts/verify.py` checks the collection in `bios/` against each
platform, never your BIOS folder: for that, use `--check` above.

`verify.py` reports `UNTESTED` for a file of the collection that was hashed
and does not match what the platform declares. The line gives the reason:

```
  UNTESTED (required): nintendo-ds/dsifirmware.bin: declared hash contradicted by the local file
```

The system page on the site lists every known version of the file with its
expected MD5 and SHA1.

### Existence-mode platforms never report UNTESTED

On RetroArch, Lakka and RetroPie, the platform check only asks whether the file
is there, so the only outcomes are OK and MISSING. `verify.py` adds the emulator
ground truth from the profiles and flags a `DISCREPANCY` when a file passes the
platform check but fails the emulator's own size or hash validation.

### The .variants/ directory

When multiple versions of the same BIOS exist (different revisions, regions, or
dumps), the primary version lives in the main directory and alternatives live in
`.variants/`. `verify.py` checks the primary file first, then falls back to
variants when resolving by hash.

If your file matches a variant hash but not the primary, it's a valid BIOS --
just not the preferred version. Some cores accept multiple versions.

## Pack is missing files

A generated pack doesn't contain all the files you expected.

### Severity levels

`verify.py` assigns a severity to each issue, counted under `severity_counts`
in its `--json` output. Not all missing files are equally important:

| Severity | Meaning | Action needed |
|----------|---------|---------------|
| CRITICAL | Required file missing on an MD5 or SHA1 platform | Must fix. Core won't function. |
| WARNING | Optional file missing on an MD5 or SHA1 platform, hash mismatch (UNTESTED) on any hash platform, or a required file missing on an existence platform | Core works but with reduced functionality, or runs on content nobody verified. |
| INFO | Optional file missing on an existence-only platform, or a missing file the core covers with an HLE fallback | Core works fine, BIOS improves accuracy. |
| OK | File present and verified | No action needed. |

The full mapping, including how `hle_fallback` overrides the platform mode, is
in [verification modes](verification-modes.md#severity-matrix).

Focus on CRITICAL issues first. WARNING files improve the experience but aren't
strictly necessary. INFO files are nice to have.

### Large files (release assets)

Files like PS3UPDAT.PUP, PSVUPDAT.PUP, and Switch firmware are too large for the
git repository. They are stored as GitHub release assets under the `large-files`
release and downloaded at build time.

If a pack build fails to include these, check your network connection. In offline
mode (`--offline`), large files are only included if already cached locally in
`.cache/large/`.

### Data directories

Some cores need entire directory trees rather than individual files (e.g. Dolphin's
`Sys/` directory, PPSSPP's `assets/`). These are fetched by `refresh_data_dirs.py`
from upstream repositories.

In offline mode, data directories are only included if already cached in `data/`.
Run `python scripts/refresh_data_dirs.py` to fetch them.

## verify.py reports errors

How to read and interpret `verify.py` output.

### Status codes

| Status | Meaning |
|--------|---------|
| `OK` | File present, hash matches (or existence check passed) |
| `UNTESTED` | File present, hash not confirmed against the expected value |
| `MISSING` | File not found in the repository |
| `DISCREPANCY` | The platform check passes, an emulator's own check fails |

### Reading the output

The first line counts the platform's files and names its check. Under it come
the entries that need attention, one per line as `STATUS (required|optional):
system/file`, followed by a reason where there is one, then the counts of the
files the emulator cores add. From `python scripts/verify.py --platform retrodeck`:

```
RetroDECK: 2007/2008 OK, 1 untested [md5]
  UNTESTED (required): nintendo-ds/dsifirmware.bin: declared hash contradicted by the local file
  DISCREPANCY: nintendo-snes/sgb2.boot.rom: RetroDECK says OK but supersnes9x says size mismatch: got 524288, accepted [256]
    supersnes9x [size]
  Core files: 1495 in pack, 0 required missing, 1 optional missing
```

`--verbose` also lists the passing entries that an emulator validates, with
the check and the source lines behind it:

```
Recalbox: 351/351 OK [md5]
  OK (required): macintosh/MacII.ROM
    minivmac validates size=262144 [minivmac/cfg/CNFGRAPI.h:32, minivmac/src/OSGLUERETRO.c:781]
```

### Cross-reference section

After per-file results, `verify.py` prints a cross-reference report. This lists
files that emulator cores need but that the platform YAML doesn't declare. These
files are still included in packs automatically, but the report helps identify
gaps in platform coverage data.

The cross-reference uses `resolve_platform_cores()` to determine which emulator
profiles are relevant for each platform, then checks whether each profile's files
appear in the platform config.

### Filtering output

```bash
# By platform
python scripts/verify.py --platform batocera

# By emulator core
python scripts/verify.py --emulator beetle_psx

# By system
python scripts/verify.py --system sony-playstation

# By hardware target
python scripts/verify.py --platform retroarch --target switch

# JSON for scripted processing: the entries that need attention
python scripts/verify.py --platform retroarch --json
# every entry, passing ones included, with its ground truth
python scripts/verify.py --platform retroarch --json --verbose
```

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

A file already in the BIOS folder with the read-only attribute is replaced
like any other: on Windows the installer clears the attribute before putting
the verified copy in place. `file in place is read-only` only appears when
Windows refuses that too, which a file owned by another account can cause.

### Platform not detected

`install.py` auto-detects your platform by checking for known config files. If
detection fails, specify the platform manually:

```bash
python install.py --platform retroarch --dest ~/RetroArch/system/
python install.py --platform batocera --dest /userdata/bios/
```

`python install.py --list-platforms` lists the platforms the installer knows
and the ones it detects here, and `--help` lists the options.

### Manifest or file not found

The one-line bootstrap verifies the downloaded installer before running it, and
the installer reads its manifests from the same revision. A platform name is
checked before anything is downloaded, so a "No file list" message comes from
the revision: `RETROBIOS_REF` naming a tag that does not exist, or one older
than the platform. Unset it, or pick a tag from the releases page.
`RETROBIOS_REF` pins an installation to a published tag when a reproducible
file set matters more than the newest one.

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
