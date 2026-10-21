# Installer

Reference for `install.sh`, `install.ps1` and `install.py`: what the one-liner
runs, what it detects, where the bytes come from and what it is allowed to
write. For BIOS directory paths per platform, see
[Getting started](getting-started.md).

## One line

```bash
# Linux / macOS / Steam Deck
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

```powershell
# Windows
irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex
```

From a clone, run the installer itself. Nothing beyond the standard library is
needed, and Python 3.8 is the floor both bootstraps enforce before handing over:

```bash
python install.py
```

## Passing options

The shell bootstrap forwards everything after `-s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard
```

PowerShell needs another form. `iex` takes the script text as its own argument,
so anything appended to `irm ... | iex` is read as an argument to `iex` and
rejected with `A positional parameter cannot be found`. Building a script block
from the text and calling it passes the arguments through:

```powershell
&([scriptblock]::Create((irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1))) `
  --platform retroarch --dest D:\bios
```

## What the bootstrap does

Both wrappers do the same four things before any BIOS file is considered.

They fetch `install.py` over HTTPS only, from `RETROBIOS_INSTALL_URL` or the
pinned raw URL. A URL with any other scheme is refused outright.

They cap the download at 2 MB and require the SHA-256 to equal the 64 hex
characters embedded in the wrapper, `RETROBIOS_INSTALL_SHA256` when set. A
mismatch aborts with "the downloaded installer does not match its expected
fingerprint" and nothing runs. Right after an update of this project the
wrapper and `install.py` can come from caches a few minutes apart, which reads
the same way: waiting five minutes settles it. Any change to `install.py`
therefore means recomputing that pin in both wrappers.

They locate a Python 3.8 or newer interpreter (`python3` then `python` on
POSIX, `py -3` then `python3` then `python` on Windows) and refuse to continue
without one. `install.sh` needs `curl` or `wget` and `sha256sum` or `shasum`.
Windows PowerShell 5.1 still negotiates SSL 3.0 and TLS 1.0 by default and
GitHub refuses both, so `install.ps1` raises the floor to TLS 1.2 before
downloading, and `install.sh` pins the same floor with `curl --tlsv1.2`.

They delete the temporary installer on the way out, including on interrupt.
Run from a clone, `install.sh` and `install.ps1` reuse the `install.py` sitting
next to them and skip the download entirely. Piped from stdin, `install.sh`
never treats the working directory as a trusted location for it.

Piped from `curl`, the shell's standard input is the script itself, so
`install.sh` hands the installer the terminal (`/dev/tty`) when there is one.
Without that, every question the installer can ask was skipped on Linux and
macOS.

## What the installer does

1. Detects the host OS and the platforms installed on it.
2. Fetches `install/<platform>.json` from the same revision the bootstrap
   verified the installer against, so the file list and the code reading it
   always come from one commit.
3. Prints the file count and total size, then a safety notice counting the
   entries the collection cannot serve, how many of those the platform marks
   required, and why. At a terminal, it then offers to install everything or
   to choose systems, cores and regions (see below).
4. Hashes what is already in the destination and reports how many entries are
   present, verified, or present with the wrong contents.
5. Downloads what is missing or wrong, up to 8 files at a time. At a
   terminal one line shows the progress, the speed and the time left, while in a
   log each finished file gets its own line. In an interactive run, a
   download of 1 GB or more is confirmed first (`--yes` skips the question).
6. Verifies each download against the declared size, SHA-256 and SHA-1 before
   it touches the destination, then moves it into place with `os.replace` so
   a partial file is never visible. A failed attempt is retried up to three
   times, after 1, 2 then 4 seconds or the delay a `Retry-After` header asks.
   A file of 16 MB or more keeps its partial copy (`.<name>.part`) when a
   transfer stops, and the next attempt or the next run requests only the
   rest.
7. Copies into standalone emulator directories, only with `--standalone-copies`.

A run looks like this:

```
Fetching file index for retroarch...
  5865 files (5.7 GB)

Checking existing files...
  0/5865 present (0 verified, 0 wrong hash)
```

When the manifest leaves entries out (a file the collection cannot supply, or
one that failed its hash check), a `Not installable:` line under the index count
says how many and why. `--check` also names the required ones missing from the
folder. They do not change its exit code, since no run of the installer can
fetch them.

## Choosing what to install

By default the installer fetches everything the platform runs. At a terminal,
after detection, it asks:

```
Retroarch: 5865 files, 5.7 GB.
  Enter) install everything
  i) only what the installed cores read: 102 files, 44.5 MB
  c) choose systems, cores and regions
  q) quit
```

`i` appears for RetroArch, Lakka and RetroPie when cores are found installed,
and the line above is a RetroArch with Beetle PSX HW, Snes9x, mGBA, Genesis Plus GX
and FCEUmm.
It keeps what those cores read and drops the systems none of them emulates,
the rule `--installed-cores` applies.

`c` walks through three numbered lists, each answered with numbers such as
`1,4,7-9`, or Enter to keep everything on that axis.

- **Systems.** The consoles and computers the platform declares. A last
  entry, `other`, holds what cores load for systems the platform does not
  list, such as game engines and extra computers. Choosing systems leaves it
  out unless it is chosen too, as `generate_pack.py --system` does.
- **Cores.** The platform's own BIOS list for the chosen systems is always
  kept. This narrows only the extra files each emulator core loads beyond it.
  Asked only when the chosen systems have such files.
- **Regions.** Asked only when a chosen system has one BIOS per region. The
  numbers give an order of preference: `2,1` keeps the second region where a
  file for it exists, and the first one elsewhere.

The run then prints the options that repeat the same choice without
questions, for instance
`--platform retroarch --system sony-playstation --region north-america`.

The same choices on the command line:

```bash
python install.py --platform retroarch --list-systems
python install.py --platform retroarch --system sony-playstation,nintendo-gba
python install.py --platform retroarch --system sony-playstation --core pcsx_rearmed
python install.py --platform recalbox --region us,eu,jp
```

### Installed cores

`--installed-cores` reads the cores a RetroArch already has and installs what
they need. The cores are the `<name>_libretro.so` (`.dll`, `.dylib`,
`_libretro_android.so`) files in the `libretro_directory` of the
`retroarch.cfg` detection found, `cores/` beside RetroArch when the setting is
unset, a `retroarch.cfg` next to the system directory for a portable install
given with `--dest`, and the folders RetroPie (`/opt/retropie/libretrocores`)
and Lakka (`/usr/lib/libretro`, `/storage/cores`) install into. A core file
is named the buildbot's way (`mednafen_psx`), and the manifest maps each name to
the profile its files are filed under (`beetle_psx`).

What stays is what a pack built for exactly those cores carries, the rule
`--target` follows too: a platform system stays when an installed core
emulates it or reads its files, or when no core of the platform does. A
file a core loads stays when that core is installed. Where two cores read
one path and want different bytes, the copy of the first installed core in
the builder's order is the one installed. The same holds for a file of the
platform's own list: where a core's source contradicts the platform and the
frontend only checks the path, the core's copy is installed while that core
is, and otherwise the platform's answer, read from the first declaring
system that stays.

Batocera, Recalbox and the other distributions ship their cores with the
system, so the option is refused there, as it is when no core is found or
when the file list predates the mapping: carrying on would install
everything while the option asked for less.

A name the platform does not have is refused with the list of the ones it
has, before anything is downloaded: carrying on would install something other
than what was asked. A system name that itself contains commas, as Recalbox's
`msx1,msx2,msxturbor` does, is read as one name.

The region rule is the one `generate_pack.py --region` applies, and a test
holds the two to the same file list. In each group of regional alternatives
the best-ranked match stays. A file declared for every region beats regional
files that match nothing asked. When nothing matches, every regional file
stays, so no system is left without its BIOS. A file the emulator does not
select by region always stays. The manifest records, for each file, the
systems that declare it, and for a regional file, its regions and the groups
it competes in. The installer reads nothing else.

The questions are asked only when standard input is a terminal. Piped,
scripted or run with `--no-input`, the installer asks nothing and installs
what the options name, everything by default.

## Options

| Option | Effect |
|--------|--------|
| `--platform NAME` | Install for this platform instead of the detected one. Unknown names are refused with the available list |
| `--dest PATH` | Destination directory, overriding detection. With `--dest` alone the file list is RetroArch's |
| `--target NAME` | Keep only the files the cores of that hardware target need, and the systems they emulate. An unknown target is refused rather than ignored, since carrying on would install everything. On Batocera, Recalbox, ROCKNIX and Lakka the board is read from the system (`batocera.arch`, `recalbox.arch`, `HW_DEVICE`, `LIBREELEC_DEVICE` or `LIBREELEC_PROJECT`) and applied when no `--target` is given |
| `--every-board` | Install the files of every board instead of the one the system names, for a card that moves between devices |
| `--installed-cores` | Keep only what the cores already installed read (RetroArch, Lakka, RetroPie). See [Installed cores](#installed-cores) |
| `--system NAMES` | Install only these systems. Comma-separated or repeated. `--list-systems` prints the names, `other` included |
| `--core NAMES` | Keep the platform's own list and only the extra files these cores load. `--list-cores` prints the names |
| `--emulator NAMES` | Install what these emulators read on the detected platform, and nothing else: their own files and the platform's files of the systems they run. Each emulator page on the site gives this command. `--list-cores` prints the names |
| `--region LIST` | Preferred regions, best first (`us,eu,jp`): keep one BIOS per region where a system has several. Accepts the names and aliases of `generate_pack.py --region` |
| `--list-systems` | Print a platform's systems with the files and size each holds |
| `--list-cores` | Print the cores whose extra files a platform installs |
| `--no-input` | Never ask a question, even at a terminal |
| `--check` | Report and exit without writing |
| `--dry-run` | Show what would be downloaded, by system and with sizes, without writing anything |
| `--yes`, `-y` | Do not ask before a download of 1 GB or more |
| `--quiet`, `-q` | Print only failures and the final summary. Implies `--no-input` |
| `--version` | Print the installer's identity, the first 12 hex digits of the SHA-256 the bootstraps pin |
| `--list-platforms` | Print the supported platforms and what was detected here |
| `--list-targets` | Print the hardware targets a platform publishes, with the core count each carries |
| `--jobs N`, `-j N` | Parallel downloads, 1 to 32, default 8 |
| `--verbose`, `-v` | Print per-attempt failures and hash mismatches |
| `--standalone-copies` | Also copy into detected standalone emulator directories |
| `--help`, `-h` | Print the option list and exit |

`--check` reads the same file list, hashes the destination and exits, with
code 1 when a file is missing or does not match and 0 when every one does. Without
it, a file already present with the expected hash is left untouched, and one
whose contents do not match is re-downloaded and replaced by the verified copy.
Files the list does not name are never read, moved or deleted.

## Environment overrides

| Variable | Effect |
|----------|--------|
| `RETROBIOS_REF` | Branch or tag the file list and the payloads are read from. Defaults to `main`, and a release tag pins a reproducible file set |
| `RETROBIOS_BASE_URL` | Base URL for the manifest and the files it declares. HTTPS only, plain HTTP allowed to loopback for end-to-end tests. Whoever controls this base controls the expected hashes, hence the check |
| `RETROBIOS_OS` | Names the host outright (`linux`, `wsl`, `windows`, `darwin`, `android`) instead of detecting it |
| `EXTERNAL_STORAGE` | Android only, the shared storage root RetroArch builds its default directories from. Defaults to `/storage/emulated/0` |
| `RETROBIOS_INSTALL_URL` | Where the bootstrap fetches `install.py`. HTTPS only |
| `RETROBIOS_INSTALL_SHA256` | The SHA-256 the bootstrap requires of that installer, 64 hex characters |
| `NO_COLOR` | Plain text, no colour, even at a terminal. `TERM=dumb` also turns the live progress line off |
| `HTTPS_PROXY`, `NO_PROXY` | Honoured for every download through the standard library's default proxy handling: one names the proxy, the other the hosts to reach directly |

## Platform detection

Detection reads what the platform itself writes, never a guess from a directory
name. The embedded systems are tested in the order below and the first match
wins, since a machine is only one of them.

| Evidence | Platform | BIOS directory |
|----------|----------|----------------|
| `/etc/os-release` `OS_NAME=ROCKNIX` (or `ID=rocknix` on images before 2025) | ROCKNIX | `/storage/roms/bios` |
| `/etc/knulli-release` | Batocera | `/userdata/bios` |
| `/etc/os-release` `ID=lakka` | Lakka | `/storage/system` |
| `/etc/batocera-version` | Batocera | `/userdata/bios` |
| `/recalbox/recalbox.version` or `/usr/bin/recalbox-settings` | Recalbox | `/recalbox/share/bios` |
| `/opt/muos` or `/mnt/mmc/MUOS/` | RetroArch | `/mnt/mmc/MUOS/bios` |
| `/home/ark` and `/opt/system` | RetroArch | `/roms/bios` |
| `/mnt/vendor/bin/dmenu.bin` | RetroArch | `/mnt/mmc/bios` |

Batocera, Recalbox, ROCKNIX and Lakka also name the board they were built for, and
that board narrows the install unless `--target` or `--every-board` says
otherwise (see the options above).

Some platforms keep their BIOS in a fixed directory, and that directory is the
evidence. They are looked for after the configurations below, for a platform
not already found that way.

| Evidence | Platform | BIOS directory |
|----------|----------|----------------|
| `/media/fat/MiSTer` | MiSTer FPGA | `/media/fat/games` |
| `C:\RetroBat\bios` or `%USERPROFILE%\RetroBat\bios` | RetroBat | the same |
| `~/Emulation/bios` | EmuDeck | the same |
| `~/.var/app/net.retrodeck.retrodeck` | RetroDECK | `~/retrodeck` |
| `/romm/library/bios` | RomM | the same |
| `~/RetroPie/BIOS` | RetroPie | the same |
| `%USERPROFILE%\BizHawk\Firmware` | BizHawk | the same |
| `EmuHawk.exe` or `EmuHawkMono.sh` in a `BizHawk*` folder of the home folder or one level down (`~/Games/BizHawk-2.11.1`), the newest | BizHawk | `Firmware` beside it |

Desktop installs are read from their own configuration, and several can be
found on one machine.

Under WSL both sides are searched. The Windows one is reached through its
`/mnt/<drive>` mount: a `C:\` path from a rule or a config becomes
`/mnt/c/...`, and the Windows variables the rules use (`USERPROFILE`,
`APPDATA`, `LOCALAPPDATA`, `ProgramFiles`) are read through `cmd.exe` when
WSL does not pass them. A forced `--platform retrobat` falls back to
`/mnt/c/RetroBat/bios`.

| Evidence | Platform | BIOS directory |
|----------|----------|----------------|
| `~/.config/EmuDeck/settings.sh` | EmuDeck | `emulationPath` + `/bios` |
| `%APPDATA%\EmuDeck\settings.ps1` | EmuDeck | `$emulationPath` + `\bios` |
| `~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json` | RetroDECK | `paths.rd_home_path`, falling back to the pre-migration `retrodeck.cfg` and its `rdhome` |
| `~/.var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg` | RetroArch (Flatpak) | `system_directory` |
| `~/snap/retroarch/current/.config/retroarch/retroarch.cfg` | RetroArch (Snap) | `system_directory` |
| `~/.config/retroarch/retroarch.cfg` | RetroArch (native) | `system_directory` |
| `~/Library/Application Support/RetroArch/config/retroarch.cfg` | RetroArch (macOS) | `system_directory`. At default, `~/Documents/RetroArch/system` (portable, Steam build included: `Application Support/RetroArch/system`) |
| `retroarch.cfg` beside `retroarch.exe` (`C:\RetroArch-Win64`, `C:\RetroArch`, `Program Files\RetroArch`) | RetroArch (Windows) | `system_directory`. At default or without a config, `system` beside the exe |
| `%APPDATA%\RetroArch\retroarch.cfg` | RetroArch (Windows) | `system_directory`, read when the exe folder has no config |
| `%ProgramFiles(x86)%\Steam\steamapps\common\RetroArch\retroarch.cfg` | RetroArch (Steam) | `system_directory` |
| `%LOCALAPPDATA%\Packages\1e4cf179-f3c2-404f-b9f3-cb2070a5aad8_*\LocalState` | RetroArch (UWP/MSIX package) | `system_directory` of the `retroarch.cfg` there, `~` meaning that folder. By default `LocalState\system` |
| LaunchBox `Data\Emulators.xml` | RetroArch (portable) | the system directory that entry points at |
| LaunchBox `Data\Emulators.xml` naming `EmuHawk.exe` | BizHawk (portable) | `Firmware` beside `EmuHawk.exe` |
| `$BIZHAWK_DATA_HOME`, or `$XDG_DATA_HOME/emuhawk-monort-<version>` (Nix package) | BizHawk (Unix) | `Firmware` under it |

`system_directory` is read the way RetroArch expands it: a leading `~` is the
home directory, a leading `:` is the application directory, and `default` means
`system` next to the application. The LaunchBox installation is located through
its Start menu shortcut, the only record of where its installer was pointed.

Android has one frontend with a shared BIOS directory, and the app-private
directories of the standalone emulators are unreachable from outside the app
under scoped storage.

| Evidence | Platform | BIOS directory |
|----------|----------|----------------|
| `<shared storage>/Android/data/<package>/files/retroarch.cfg` | RetroArch (Android) | `system_directory` |
| `/data/data/<package>/files/retroarch.cfg` | RetroArch (Android) | `system_directory` |
| `<shared storage>/RetroArch/` | RetroArch (Android) | `<shared storage>/RetroArch/system` |

The packages are `com.retroarch.aarch64`, `com.retroarch` and
`com.retroarch.ra32`, the flavours the frontend publishes. Both config paths
are unreadable from Termux on Android 11 and later, so the directory itself is
what answers on current devices.

ES-DE and LaunchBox are reported when present and nothing is written to them:
they reference emulators rather than owning a BIOS directory. ES-DE is looked
up at `$ESDE_APPDATA_DIR` or `~/ES-DE`, the two locations its own
`getAppDataDirectory` resolves.

When a platform named with `--platform` is not found on the machine, the
destination falls back to the directory its detection looks for:
`/userdata/bios` for Batocera, `/recalbox/share/bios` for Recalbox,
`/storage/system` for Lakka, `~/retrodeck` for RetroDECK, `~/Emulation/bios`
for EmuDeck, `/storage/roms/bios` for ROCKNIX, `/media/fat/games` for MiSTer,
`~/RetroPie/BIOS` for RetroPie, `/romm/library/bios` for RomM,
`C:\RetroBat\bios` for RetroBat, and RetroArch's own default system
directory for RetroArch. BizHawk has no such folder: it reads `Firmware`
beside its executable wherever the archive was extracted, so a forced
`--platform bizhawk` that finds no install stops and asks for `--dest`. On Android a forced `--platform retroarch` falls back to
`<shared storage>/RetroArch/system` instead, a home directory there belonging
to Termux rather than to the frontend. Pass `--dest` to say where instead of
relying on that.

With nothing detected, the installer lists the platforms and asks. With several
detected, it asks which one. Both prompts need a terminal: piped into a script
with no platform to install for, it prints the manual invocation and exits 1.

## Standalone copies

Some files are read by a standalone emulator from its own data directory
rather than from the platform's BIOS tree. `--standalone-copies` copies them
there after the install, and only then: without the flag nothing is written
outside the selected tree.

The manifest carries the list, fifteen entries on the six platforms that
declare them (Batocera, EmuDeck, Recalbox, RetroArch, RetroBat, RetroDECK):

| Files | Copied to |
|-------|-----------|
| `prod.keys`, `title.keys` | yuzu, eden, citron, suyu and Ryujinx key directories |
| `Citra/sysdata/aes_keys.txt`, `Citra/sysdata/boot9.bin` | Azahar `sysdata` |
| `scph*.bin` | DuckStation `bios` |
| `ps2-*.bin` | PCSX2 `bios`, native and Flatpak |
| `GC/USA/IPL.bin`, `GC/EUR/IPL.bin`, `GC/JAP/IPL.bin`, `dsp_rom.bin`, `dsp_coef.bin` | Dolphin, per region for the IPL |
| `PPSSPP/ppge_atlas.zim` | PPSSPP `PSP/SYSTEM` |
| `dc/dc_boot.bin`, `dc/dc_nvmem.bin` | Flycast `data`, native and Flatpak |

An entry names one file or a glob, and one entry carries no file at all: when
an RPCS3 configuration directory is present it prints that `PS3UPDAT.PUP` is
installed through RPCS3's own File menu, since that firmware is an installer to
run rather than a file to copy.

Targets are per OS, WSL falling back to the Linux ones. A directory that does
not exist is skipped rather than created, so nothing is copied for an emulator
that is not installed. A destination that is already a symbolic link is skipped
too: the copy leaves the tree the user opted into, and a link there would
redirect the write somewhere else again.

## Android

Android reports itself as Linux, so the runtime is named from its own
variables instead: `ANDROID_ROOT` and `ANDROID_DATA`, which Termux inherits,
with `/system/build.prop` covering a shell started without them.

Detection then looks for RetroArch, the only Android frontend keeping a shared
BIOS directory. `retroarch.cfg` is read from the app external files directory
and then from the internal one, the order the frontend itself follows
(`platform_unix.c:1327-1395`), and a `system_directory` left at `default`
resolves against `<shared storage>/RetroArch`, where the directory also sits
when no config has been written yet (`platform_unix.c:2836-2851`).

Under scoped storage those two config paths are unreadable from Termux on
Android 11 and later, so on current devices the shared directory is what
answers. Running `termux-setup-storage` once is what makes it reachable:

```bash
pkg install python
termux-setup-storage
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

`EXTERNAL_STORAGE` overrides the storage root for a device that mounts it
elsewhere. A custom BIOS directory is still named directly:

```bash
python install.py --platform retroarch --dest /storage/emulated/0/RetroArch/system
```

## Where the files come from

Each manifest entry names either `repo_path`, served from the repository at the
pinned revision, or `release_asset`, served from the `large-files` release for
the files above the size GitHub carries in a tree. Everything else in the entry
(`dest`, `size`, `sha1`, `sha256`, `cores`) describes what must land where and
what it must hash to.

Entries the collection cannot satisfy are not silently dropped: they travel in
`omitted_files` with a reason, and the run prints the count before downloading
anything. `not_found` means no file in the collection matches the entry, which
is what the [gap analysis](../gaps.md) tracks.

Some cores read a whole tree of data rather than a few files: Dolphin's `Sys/`,
PPSSPP's assets, blueMSX's machines. These are not files of the collection, so
`data_directories` names each tree's source archive (the libretro buildbot, an
upstream release) and the sha1 and size of every file the pack carries. The
installer downloads the archive once, maps its members the way the pack was
built (`strip_components` for a ZIP, `source_path` for a tarball, `exclude`),
and writes a file only when its hash matches the list. A file the source has
changed since is left out and counted, and the pack still holds it. `--check`
counts the data files missing, `--dry-run` names each tree it would fetch, and
narrowing with `--system`, `--core` or `--target` keeps only the trees of the
systems that stay.

## Trust boundary

The manifest is fetched over the network and treated as untrusted input. Every
path it carries goes through one validator that refuses an absolute path, a
drive letter, a backslash, a null byte, a doubled separator, a trailing slash
and any `..` component, so a destination cannot climb out of the BIOS
directory. Manifest, target list and payloads each have a size cap, the file
list a count cap, and the sum of the declared sizes is checked against the
total the manifest claims.

Symbolic links met under the BIOS root are followed on purpose: they are the
user's own layout, and EmuDeck links `bios/shadps4/sys_modules` into shadPS4's
data directory where the emulator actually reads it. The boundary is lexical,
not a resolved-path comparison, which is what keeps that case working.
Standalone copies are stricter, since they write outside the tree the user
selected: a symlink already sitting at the destination is skipped rather than
followed.

## Changing the installer

`install.py` runs on the user's machine, often an older handheld or a stock
RetroPie image, so it stays on Python 3.8 and the standard library alone.
Tests refuse any 3.9 syntax or API, and one runs the installer end to end under
a real 3.8 interpreter, which CI installs beside 3.12 so the check cannot skip.
The build scripts need 3.9 and are not bound by this.

Every change to `install.py` recomputes the SHA-256 pinned in `install.sh` and
`install.ps1` in the same commit. The tests compare the pins with the file.

Narrowing by system, core, region or target replays the pack builder's rules
from fields the manifest carries, and a parity test compares an installer
selection with the pack built the same way. Given a manifest without those
fields, the installer refuses to narrow rather than install everything.

## Exit status

| Code | Meaning |
|------|---------|
| `0` | everything asked for is installed or already up to date |
| `1` | a failure: the one-liner could not download or verify `install.py`, or found no Python 3.8 (nothing was run), an unusable base URL, an unknown platform or target, `--installed-cores` with no core found, a manifest that fails validation, a fetch that never succeeded, no platform to install for in a non-interactive run, some files failed while others installed, or `--check` found files to fetch |
| `2` | nothing was installed: every file the run tried failed (a BIOS folder that cannot be written, for one) |
| `130` | stopped with Ctrl+C. Run the same command again to carry on |
| `141` | the output was piped to a reader that closed early (`--list \| head`) |

`install.ps1` passes the installer's code on unchanged: run as a file it exits
with it, run through `irm \| iex` it sets `$LASTEXITCODE` (an `exit` there
would close the PowerShell window).

Failures that are per-file, such as a download exhausting its three attempts,
are counted and reported at the end.

## When something goes wrong

See [Troubleshooting](troubleshooting.md#installation-script-fails) for network,
permission and detection failures.
