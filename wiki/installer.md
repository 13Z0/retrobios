---
level: setup
order: 100
---

# All installer options

The one-line command finds your setup and its BIOS folder. It downloads only
the files that are missing or wrong, and checks each one against its
fingerprint before writing it. The options below are for anything else: fewer
files (one console, one region, only the cores you have), another folder, an
SD card, a check without download, or a run from a script.

Example: `--platform retroarch --system sony-playstation --region north-america`
installs only the PlayStation files, and the North American version where
there is one per region.

How the command protects your machine is under
[What the bootstrap does](#what-the-bootstrap-does) and
[Trust boundary](#trust-boundary). The BIOS folder of each setup is on
[Get your files](../which-pack.md).

## Options at a glance

Read from the installer's own option list, the one `--help` prints. The
[Options](#options) table below gives each one in full.

<!-- installer-options -->

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

A package that must not change under its users pins the installer to a
release tag, and the file lists to the same tag with `RETROBIOS_REF`:

```bash
curl -fsSLO https://raw.githubusercontent.com/Abdess/retrobios/<tag>/install.py
RETROBIOS_REF=<tag> python3 install.py
```

A tag lasts until the next release deletes it. What `RETROBIOS_REF` does not
pin is listed under [Environment overrides](#environment-overrides).

## Passing options

The shell bootstrap forwards everything after `-s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

`--dest` is the BIOS folder itself: on a card, `retroarch/cores/system` for
RetroArch on Switch, `RetroArch/system` on Android.

PowerShell needs another form. `iex` takes the script text as its own argument,
so anything appended to `irm ... | iex` is read as an argument to `iex` and
rejected with `A positional parameter cannot be found`. Building a script block
from the text and calling it passes the arguments through:

```powershell
&([scriptblock]::Create((irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1))) `
  --platform retroarch --dest D:\bios
```

## The full-screen interface { #the-full-screen-interface }

Run at a terminal, the installer opens a full screen that a keyboard or a
controller drives. Three presses of A or Enter install everything your setup
reads: the setup found here, the plan, then the result. The plan screen says
how many files are missing, how much they weigh, how many are in place and how
much space is free, before anything is written.

The other choices sit one press away. **Choose systems** ticks the consoles you
play, with the total to download under the list. **Options** sets the
preferred regions, game data, the installed cores only, the cores, the parallel
downloads and the folder. Each row names the option it sets, and **Show the
command line** gives the same choice as one command for a script. **Preview the
file list** shows what would be downloaded and writes nothing. **Copy the BIOS
files from your old card or a folder** opens the folder browser, mounted cards
first, and adds [`--from`](#copy-from-an-old-card): the plan then says how many
files that folder holds and how many are left to download. Files the BIOS
folder holds at the wrong place are counted on the plan, and **Show the files
in the wrong place** lists each one with its reason. Back on the plan
after a change of options, the folder is checked again, so its numbers are the
exact ones for the new choice. Ticked systems get their exact count before the
install starts.

| Action | Keyboard | Controller |
|--------|----------|------------|
| Move | Up and Down, `k` and `j` | D-pad Up and Down |
| Page | PgUp and PgDn | L1 and R1 |
| First, last | Home and End, `g` and `G` | |
| Continue, open, choose | Enter, Space | A, Start |
| Tick, untick | Space, Right, Left | X, D-pad Right and Left |
| Back | Esc, Backspace | B, Select |
| Options | `o`, Tab | Y |
| Filter a long list | `/` then type | the Filter row |
| Keys of the screen | `?`, F1 | |
| Stop an install | Esc, `q`, Ctrl+C | B, Start+Select |
| Quit | `q`, Ctrl+C | B on the first screen |

The bottom line of each screen names what A does on the row under the cursor,
with both spellings (`A/Enter`, `B/Esc`), since A and B swap places between
controller layouts. A narrow screen shortens them to `A`, `B` and `X`. On the
regions screen, Left and Right move the region under the cursor up and down the
order of preference. During the download, a stop waits for the files in
progress, and a second one stops at once. Start+Select on a handheld ends the
installer with a signal (SIGTERM), which stops it the same way. Either way the
result says how many files were installed, and it stays printed in the terminal
once the screen closes, with the command that repeats the run. A run that failed
keeps its log and names it, and every other run leaves nothing behind.

It opens only for an install, at a terminal of at least 40 columns and 12 rows
that is not `TERM=dumb`. A listing, `--check`, `--dry-run`, `--events`,
`--quiet`, `--no-input`, `--yes` and `--restore` keep the plain output. Options
given on the command line fill the screens in advance and are kept as they are.

- `--tui` asks for the full screen, and names the reason when it cannot open
  (code 2).
- `--plain`, `RETROBIOS_PLAIN=1` or `ACCESSIBLE=1` keep the questions line by
  line. This is the mode for a screen reader, which reads a full screen badly.

Behind the screens, the installer runs itself with `--check --events`, then
`--events --yes` and the options chosen: the same
[events](#machine-readable-events) a program driving the installer reads. The
file list is downloaded once and handed to each run.

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
5. Copies from this machine what it already holds: a file misplaced in the
   BIOS folder, and what a folder named with `--from` has. See
   [Copy from an old card](#copy-from-an-old-card).
6. Downloads what is still missing or wrong, up to 8 files at a time. At a
   terminal one line shows the progress, the speed and the time left, while in a
   log each finished file gets its own line. In an interactive run, a
   download of 1 GB or more is confirmed first (`--yes` skips the question).
7. Verifies each download against the declared size, SHA-256 and SHA-1 before
   it touches the destination, then moves it into place with `os.replace` so
   a partial file is never visible. A failed attempt is retried up to three
   times, after 1, 2 then 4 seconds or the delay a `Retry-After` header asks.
   A file of 16 MB or more keeps its partial copy (`.<name>.part`) when a
   transfer stops, and the next attempt or the next run requests only the
   rest. A file already there with other contents is moved aside first, see
   [Files you already had](#files-you-already-had).
8. Copies into standalone emulator directories, only with `--standalone-copies`.

Some cores read their system data below the frontend's save directory, not
below the BIOS folder: Azahar's keys, shared font and NAND files, Citra's
`sysdata`, Dolphin's `User` tree. The file list marks them `root: save`, and
the run writes them below the save directory, which it names before checking
files (`5 files read from RetroArch's save directory go to ...`). For a
RetroArch found by its `retroarch.cfg`, that is `savefile_directory` as
RetroArch expands it (`:` for its own folder, `~` for the home), `saves/`
beside the config when the value is `default`, empty, or names no folder, and
the core's own folder below it when `sort_savefiles_enable` is on, as it is by
default. Without a config, a BIOS folder at its default name (`system`) puts
the save directory beside it (`saves`, `/storage/savefiles` on Lakka).
`--save-dir PATH` names the save directory itself. When none of these
applies, or when `savefiles_in_content_dir` or
`sort_savefiles_by_content_enable` keeps saves beside each game, those files
are left out and the run says why, like the other entries the list cannot
install.

Batocera, KNULLI, Recalbox, RetroBat and RetroDECK write the save directory at
each launch, one folder per system (`by_system` in the file list). Their lists
already name each system's folder (`gamecube/User/GBA/gba_bios.bin`), and the
run writes below the `saves` folder beside the BIOS folder (`/userdata/saves`,
`/recalbox/share/saves`, `~/retrodeck/saves`), never reading a
`retroarch.cfg` that only holds the last launch. EmuDeck's RetroArch config
names `~/Emulation/saves/retroarch/saves`. RetroPie, ROCKNIX, AmberELEC and
EmuELEC keep saves beside each game, and RomM keeps them in the browser: their
lists record these files as omitted.

A RetroArch run into an empty folder printed this on 2026-10-21:

```
Fetching file index for retroarch...
  8069 files (6.5 GB)
  Not installable: 11 files (1 required, not found: 11).

Checking existing files...
  0/8069 present (0 verified, 0 wrong hash)
```

When the manifest leaves entries out (a file the collection cannot supply, or
one that failed its hash check), a `Not installable:` line under the index count
says how many and why. `--check` and the closing report also name the required
ones, grouped by system with the emulators that read them, and point at the
emulator pages. They do not change the exit code of `--check`, since no run of
the installer can fetch them.

## Reading a run { #reading-a-run }

Each line a first install prints, and what it means. The run below is a
Batocera install into an empty folder, from the list as published on
2026-10-21. The figures change with each release.

### Before the run

The size depends on the platform and changes with each release. The run prints
it before downloading, and asks first when the download reaches 1 GB. The
installer also measures the free space and refuses before writing anything if
there is not enough.

`curl` or `wget`, and Python 3.8 or newer, are checked by the command itself:
it stops with a message naming what is absent. PowerShell's execution policy
does not block the Windows line. The policy governs script files, and the
command never writes one.

### The first lines

```
RetroBIOS 8c0e9c762d2f

Detecting platform...
  Found Batocera at /userdata/bios
```

The code after RetroBIOS identifies the installer: it is the start of the
SHA-256 the command checked before running it. The path is the folder it will
write to.

### The file index and the download

Two phases follow. The first reads what is already in the folder. It takes a
few minutes on a memory card, because every file is hashed:

```
Fetching file index for batocera...
  14481 files (9.1 GB)
  Not installable: 399 files (79 required, not found: 399).

Checking existing files...
  0/14481 present (0 verified, 0 wrong hash)
```

`Not installable` counts the files of the list that the collection does not
hold. They are named at the end of the run.

On a first install the present count is zero. On a later run it counts what
is already there, and only the rest is downloaded. A download of 1 GB or more
is confirmed first (`Download 9.1 GB to /userdata/bios? [Y/n]`).

The second phase downloads what is missing, one line per file:

```
Downloading 14481 files (9.1 GB)...
  Press Ctrl+C to stop. Running the same command again carries on from here.
  [1/14481] 3do_arcade_saot.bin ok
  [2/14481] 3dobios.zip ok
```

Ctrl+C is safe at any point. Files already installed stay, and running the
command again picks up where it stopped.

### The end of the run

```
Done. 14481 files installed, 0 files already up to date.
Location: /userdata/bios
399 files were not installed: the collection does not hold them, or its copy fails their check.
  Needed to start an emulator (79 files):
    dark-forces (theforceengine): DARK.GOB, SOUNDS.GOB, SPRITES.GOB, TEXTURES.GOB
    dark-forces-ii (openjkdf2): openjkdf2/episode/JK1.gob, openjkdf2/episode/JK1CTF.gob, openjkdf2/episode/JK1MP.gob, openjkdf2/resource/Res1hi.gob, openjkdf2/resource/Res2.gob
    diablo (devilutionx): devilutionx/diabdat.mpq
    doom3 (dhewm3): base/pak000.pk4, base/pak002.pk4, base/pak003.pk4, base/pak004.pk4
    droidworks (openjkdf2): dwCD.GOB, dwHD.GOB, dwMin.GOB, dwStream.GOB
    fallout (fallout1-ce): fallout1-ce/critter.dat, fallout1-ce/master.dat
    fallout2 (fallout2-ce): fallout2-ce/critter.dat, fallout2-ce/master.dat
    fury (fury): fury.grp
    ... and 9 more systems (--check --verbose lists them all)
  Each emulator's page says what these files are: https://abdess.github.io/retrobios/emulators/
To check this later, run the same command with --check.
```

When most of a download of 100 MB or more comes from one system, or from
one core of a single system, the end of the run says so and what narrowing
would save, once that saving is over half:

```
  mame pulls 336.5 MB of this 349.0 MB download for 3do. --core opera keeps 12.5 MB of it and saves 336.5 MB (96%).
```

`--dry-run` prints the same line under its plan.

When some files came from this machine rather than the network, a line under
`Done` says where each came from (`38 files copied from E:\, 3 fixed from
copies already in the BIOS folder, 4 downloaded.`). See
[Copy from an old card](#copy-from-an-old-card).

The word `Done` and a location are the confirmation. Any other ending
(`Stopped`, `Finished with errors`, `Nothing was installed`) means something
was left undone, and the run says which case it was.

The lines between `Location:` and the `--check` hint name the files of the
list that the collection does not hold. `Needed to start an emulator` gives,
for each system, the emulators that read it and the files they cannot start
without. On the run above, all 79 belong to games (Dark Forces, Diablo, Doom 3,
Tomb Raider and the others): the engine reads them from a copy of the game
itself. The other 320 are optional, and the emulators reading them start
without them.

### A later check

The same command with `--check` reads the folder and writes nothing:

```
Checking existing files...
  14481/14481 present (14481 verified, 0 wrong hash)

  All files up to date.
```

The files the collection does not hold are listed after it again, as long as
they are absent from the folder. A number before `wrong hash` means a file is
present with contents the platform does not accept. Running the install again
replaces it and keeps the old one, see
[Files you already had](#files-you-already-had).

### Why a file is refused { #why-a-file-is-refused }

Each file `--check` flags comes with the reason it found and the place each
core opens it:

```
  3 files to download:
    wrong hash: scph5501.bin
      another region: the contents of scph5500.bin (japan)
      opened at /home/me/.config/retroarch/system/scph5501.bin by beetle_psx, pcsx_rearmed
    missing: gba_bios.bin
      nested folder: it is at system/system/gba_bios.bin, a system folder inside system
      opened at /home/me/.config/retroarch/system/gba_bios.bin by mgba
    missing: pcsx/extra.bin
      still zipped: it is inside PSX BIOS.zip
      opened at /home/me/.config/retroarch/system/pcsx/extra.bin by pcsx_rearmed
```

| Reason | What the folder holds |
|--------|-----------------------|
| `nested folder` | The file inside a folder named like the BIOS folder itself (`bios/bios/`, `system/system/`) |
| `one folder too deep`, `2 folders too deep` | The file below its place, as a ZIP extracted into a folder of its own leaves it |
| `one folder too high` | The file in the folder above the BIOS folder |
| `wrong folder` | The file under its name in another folder |
| `case of the name` | A name that differs only by capital letters, on a system where that makes two names (`the folder holds SCPH1001.BIN, the cores open scph1001.bin`) |
| `still zipped` | The file inside a ZIP in the folder, a `.7z` or `.rar` of the same name, or a ZIP saved under the file's own name |
| `wrong size` | A file of another size than any copy the list accepts |
| `another region`, `another revision`, `another file` | The contents of another file of the list: another region's BIOS, the copy another core reads, or the one the platform's own list declares |
| `a name no core opens` | The right contents under a name no core of the platform reads |
| `same size, other contents` | Another dump, or a damaged copy |
| `only a Mac's ._ file` | Only the `._name` file a Mac writes beside each file it copies to a FAT or exFAT card: Finder data, not the file |

The reasons are read from the folder and the file list, nothing else. When
three files or more sit too deep in the same folder, a line before the list
names that folder once. A file whose exact contents are somewhere in the BIOS
folder (too deep, in capitals, still in its ZIP, one folder too high) is put
in place by the next install: `--check` says so under the file, with `a run
without --check copies it into place`. Files with a reason are always listed. The plain
missing ones stop at ten unless `--verbose` is given.

A pack published in parts leaves a marker beside the files of each part
(`RetroArch_v1.22.2_BIOS_Pack.part1of3.txt`). `--check` reads them and names
a part that was not extracted, or one extracted into a folder of its own:

```
  RetroArch_v1.22.2_BIOS_Pack.zip comes in 3 parts, all needed.
    missing: part 2, RetroArch_v1.22.2_BIOS_Pack.part2of3.zip (1.9 GB)
    Download it and extract into /home/me/.config/retroarch/system, beside the other parts.
```

## Files you already had

A file in the BIOS folder whose contents differ from the expected one (another
revision, a personal dump) is replaced, but never lost. Just before the
verified copy takes its place, the old file is moved to a dated folder beside
the BIOS folder, under the same path:

```
~/.config/retroarch/system/scph5501.bin                    the verified copy
~/.config/retroarch/retrobios-backup/20261022-153000/system/scph5501.bin   yours
```

A file with the expected contents is never copied, and nothing is kept when
nothing is replaced. The end of the run says how many files were kept, their
size, the folder, and the command that puts them back:

```
Kept 1 file you had (512.0 KB), which the collection's copies replaced, in /home/me/.config/retroarch/retrobios-backup/20261022-153000
  To put them back: python3 install.py --restore /home/me/.config/retroarch/retrobios-backup/20261022-153000
```

`--restore FOLDER` copies every file of that folder back to its place and
leaves the folder as it is, to delete once the emulators start as before.
Copying the contents of the dated folder into the folder that holds
`retrobios-backup` does the same by hand. `--dry-run --restore FOLDER` lists
what would go back. `--dry-run` alone counts the files an install would keep.

The folder is beside the BIOS folder, not inside it, because some emulators
find a BIOS by its contents and not by its name. DuckStation, SwanStation and
Rustation read every file of the BIOS folder, and BizHawk walks its firmware
folder with every subfolder: an old copy anywhere inside could be picked
instead of the new one. Only when nothing can be written beside the BIOS folder
(a drive root, a parent folder that refuses writes) does `retrobios-backup` go
inside it, and the same `--restore` puts the files back. A file that cannot be
moved aside is not replaced: it stays, and the run reports it as failed with
`file in place could not be kept aside`.

A BIOS folder can sit on a network share other people write to. So a
`retrobios-backup` that is a link is never written through (the backup goes
inside the BIOS folder instead), and `--restore` only takes a folder the
installer created, marked by a `.retrobios-backup` file, without following
links found inside it.

On one drive the old file is moved, not copied, so keeping it needs no space
beyond what it already used. A link sitting at a file's name is not kept: the
file it points to is never touched. Data trees and `--standalone-copies` keep
their replaced files the same way, beside the folder they were in.

## Copy from an old card { #copy-from-an-old-card }

The card a handheld came with, a backup of an old BIOS folder, a Downloads
folder: `--from FOLDER` copies the BIOS files it holds instead of downloading
them.

```bash
python install.py --platform muos --dest /media/me/SD1 --from /media/me/STOCK
python install.py --from ~/Downloads --from /media/me/backup
python install.py --check --from /media/me/STOCK
```

Every file is recognised by its contents, never by its name:
`PSX BIOS (USA).bin` in a subfolder lands as `scph5501.bin` when it holds those
bytes, and a file of the right name with other bytes (an empty file left by a
CFW, another revision) is not taken. The folder is searched with every
subfolder, and inside each ZIP it holds, for a file the list wants loose and
for an archive it wants whole (`neogeo.zip`). Only files whose size is one the
list expects are hashed, so a large card is read quickly. The `._` files a Mac
writes, the system folders (`System Volume Information`, `.Spotlight-V100`,
`.Trashes`, `$RECYCLE.BIN`) and a link leading out of the folder are skipped.
`--from` also takes a ZIP of an old BIOS folder.

The folder is only read: each file is copied, checked against the list's
hashes once written, and the original stays where it is. A file in place with
other contents is kept aside first, as a download would replace it (see
[Files you already had](#files-you-already-had)). What no folder holds is
downloaded as usual, or taken from the pack with `--from-pack`. Several
`--from` are read in the order given, and the end of the run says where each
file came from:

```
Done. 44 files installed, 0 files already up to date.
  38 files copied from E:\, 4 downloaded, 2 still missing.
```

`--check --from FOLDER` writes nothing and says how many files the folder
would give (`E:\ holds 38 of these files: a run without --check copies
them.`), with each file's place in it. `--dry-run` counts them apart from the
download.

The BIOS folder itself is read the same way. A file the list wants that sits
misplaced in it, in a `bios/bios/` folder, a pack extracted into a folder of
its own, a name in capitals on a system where case counts, a ZIP never
extracted, or one folder above, is copied where the cores open it, with no
`--from` needed. The misplaced copy is left as it is, and the end of the run
counts them (`3 fixed from copies already in the BIOS folder`).

## Choosing what to install

By default the installer fetches everything the platform runs. At a terminal,
after detection, it asks:

```
RetroArch: 8069 files, 6.5 GB.
  Enter) install everything
  i) only what the installed cores read: 100 files, 44.2 MB
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
python install.py --platform emuelec --no-game-data
```

### Firmware without game data

Some platform lists name the files a game reads beside the firmware its
emulator needs (the Doom WADs in EmuELEC's list), and the cores' profiles mark
the data files of the games they run `category: game_data`: MAME's software
lists, a game engine's own data. The manifest marks each of these files
`"category": "game_data"`, and firmware carries no category.

`--no-game-data` leaves them out of the install, `--check` and `--dry-run`,
and names what it left out with its category (ten names, `--verbose` for all):

```text
  Left out as game data (--no-game-data): 2436 files: ports/doom/doom1.wad, ports/doom2/doom2.wad, ...
```

It is the cut `generate_pack.py --no-game-data` makes, and a test holds the
two to the same file list. A file list that records no category is refused
rather than read as having no game data.

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

With `--dest` and no `retroarch.cfg` beside the folder, nothing names the cores
folder: `--cores-dir` names it, and the refusal prints the command with it.

Batocera, Recalbox and the other distributions ship their cores with the
system, so the option is refused there, as it is when no core is found or
when the file list predates the mapping: carrying on would install
everything while the option asked for less.

A name the platform does not have is refused before anything is downloaded,
since carrying on would install something other than what was asked. The
refusal suggests the closest names (`playstation` gives `sony-playstation`) and
the option that lists them all. Names are matched on letters and digits, so
`--platform "MiSTer FPGA"` and `--system "Sony PlayStation"` are read as the
site writes them. A system name that itself contains commas, as Recalbox's
`msx1,msx2,msxturbor` does, is read as one name. The name a platform gives a
system itself is accepted too: on Batocera `--system psx` is
`sony-playstation` and `--system gba` is `nintendo-gba`, the names its
`roms/` folders carry. They come from the platform's own file, and
`--list-systems` prints them beside each system (`nintendo-gba (gba)`).

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
| `--platform NAME` | Install for this platform instead of the detected one. A system `--list-platforms` names under its own files (`muos`, `arkos`) stands for the file list it gets, and the run says which. Unknown names are refused with the closest ones |
| `--dest PATH` | Destination directory, overriding detection. Without `--platform`, the file list is the platform detected here: at a terminal the installer asks, Enter taking the one found, and otherwise it uses the one found, RetroArch's when nothing is found, and refuses when several are. The run prints whose list it is. A folder that does not exist is named with the nearest one that does, then created. When more than its last level is missing (a mistyped path), a run at a terminal asks first, and the answer defaults to no |
| `--target NAME` | Keep only the files the cores of that hardware target need, and the systems they emulate. An unknown target is refused rather than ignored, since carrying on would install everything. On Batocera, Recalbox, ROCKNIX and Lakka the board is read from the system (`batocera.arch`, `recalbox.arch`, `HW_DEVICE`, `LIBREELEC_DEVICE` or `LIBREELEC_PROJECT`) and applied when no `--target` is given |
| `--every-board` | Install the files of every board instead of the one the system names, for a card that moves between devices |
| `--installed-cores` | Keep only what the cores already installed read (RetroArch, Lakka, RetroPie). See [Installed cores](#installed-cores) |
| `--cores-dir PATH` | The folder RetroArch loads its cores from, for `--installed-cores` when it is not found. Implies `--installed-cores` |
| `--save-dir PATH` | The frontend's save directory (RetroArch's `savefile_directory`), for the files some cores read there, when the run cannot find it. The core's own folder is still added below it when the frontend sorts saves by core |
| `--system NAMES` | Install only these systems. Comma-separated or repeated. `--list-systems` prints the names, `other` included. A short name the platform does not use (`gba`, `psx`) is refused with the ids it may stand for, never taken in their place |
| `--core NAMES` | Keep the platform's own list and only the extra files these cores load. `--list-cores` prints the names |
| `--emulator NAMES` | Install what these emulators read on the detected platform, and nothing else: their own files and the platform's files of the systems they run. Each emulator page on the site gives this command. `--list-emulators` prints the names, buildbot names such as `mednafen_psx` included |
| `--region LIST` | Preferred regions, best first (`us,eu,jp`): keep one BIOS per region where a system has several. Accepts the names and aliases of `generate_pack.py --region` |
| `--no-game-data` | Leave out game data (a game's own files, not firmware) and install the firmware only. What it leaves out is named. See [Firmware without game data](#firmware-without-game-data) |
| `--list-systems` | Print a platform's systems with the files and size each holds, the files other systems share too counted apart |
| `--list-cores` | Print the cores whose extra files a platform installs |
| `--list-emulators` | Print every name `--emulator` accepts, each buildbot name beside the emulator it stands for |
| `--romm-firmware TEMPLATE` | RomM: where each platform's firmware goes in the library, as `filesystem.structure.firmware` in RomM's `config.yml` says (`{platform}/bios` for a library laid out as `{platform}/roms`). Read from `config.yml` beside the library when it is there. See [RomM firmware layout](#romm-firmware-layout) |
| `--no-input` | Never ask a question, even at a terminal |
| `--check` | Report and exit without writing |
| `--dry-run` | Show what would be downloaded, by system and with sizes, without writing anything |
| `--yes`, `-y` | Do not ask before a download of 1 GB or more |
| `--quiet`, `-q` | Print only failures and the final summary. Implies `--no-input` |
| `--events` | Write the run as JSON events on standard output, one object per line, and the usual text on standard error. Implies `--no-input`. See [Machine-readable events](#machine-readable-events) |
| `--from FOLDER` | Copy the BIOS files a folder holds (an old SD card, a backup, a ZIP) instead of downloading them, each one recognised by its hash and copied, never moved. Repeat for several folders. See [Copy from an old card](#copy-from-an-old-card) |
| `--from-pack PATH` | Install from a pack already downloaded: its ZIP, one of its parts (the others are found in the same folder), or the folder holding them. Makes no network request and checks every file's hash. See [Without a network](#without-a-network-offline-install) |
| `--manifest FILE` | Read the file list from FILE, a copy of `install/<platform>.json`, instead of downloading it. See [Without a network](#without-a-network-offline-install) |
| `--restore FOLDER` | Put back the files a run replaced and kept in FOLDER, a dated folder inside `retrobios-backup`. Takes no other option but `--dry-run` and `--no-input`. See [Files you already had](#files-you-already-had) |
| `--tui` | Open the full-screen interface, or stop with the reason it cannot open here (code 2) instead of falling back to the questions. Wins over `RETROBIOS_PLAIN` and `ACCESSIBLE`. See [The full-screen interface](#the-full-screen-interface) |
| `--plain` | Ask the questions line by line instead of the full-screen interface, the mode for a screen reader. See [The full-screen interface](#the-full-screen-interface) |
| `--version` | Print the installer's identity, the first 12 hex digits of the SHA-256 the bootstraps pin |
| `--list-platforms` | Print the supported platforms and what was detected here |
| `--list-targets` | Print the hardware targets a platform publishes, with the core count each carries |
| `--jobs N`, `-j N` | Parallel downloads, 1 to 32, default 8 |
| `--verbose`, `-v` | Print per-attempt failures and hash mismatches |
| `--standalone-copies` | Also copy into detected standalone emulator directories |
| `--help`, `-h` | Print the option list, a few examples in the form the run was started with, and the address of this page |

`--check` reads the same file list, hashes the destination and exits, with
code 1 when a file is missing or does not match and 0 when every one does. Files
with a wrong hash are listed first and in full, then the missing ones whose
reason it found, then the first ten other missing ones (`--verbose` lists them
all). See [Why a file is refused](#why-a-file-is-refused). `--dry-run` counts the data trees in its total. Without
it, a file already present with the expected hash is left untouched, and one
whose contents do not match is re-downloaded and replaced by the verified copy,
the old one kept beside the BIOS folder.
Files the list does not name are never read, moved or deleted.

## Platform detection

Detection reads what the system itself writes, never a guess from a directory
name. A machine runs one embedded system, so every rule below is tested. A
system built on another one (KOLOS on JELOS, dArkOS on ArkOS, spruceOS on a
TrimUI) matches both and is the one meant. Two unrelated systems matching
are both named: at a terminal the run asks which one, otherwise it stops and
asks for `--dest`. The run names the system as it names itself, with the file
list it gets: `Found AmberELEC at /storage/roms/bios (RetroArch files)`.
`--list-platforms` lists every system below.

Where a system's RetroArch config is named, its `system_directory` is the
folder, `~` being that system's home for RetroArch. The folder in the last
column is used when no config sets one. ArkOS and dArkOS keep two configs
(64-bit and 32-bit RetroArch), and their "Switch to SD2 for Roms" script
rewrites both to `/roms2/bios`: a second card is followed through them.

| Evidence | System | File list | BIOS directory |
|----------|--------|-----------|----------------|
| `/etc/os-release` `OS_NAME=ROCKNIX` (or `ID=rocknix` on images before 2025) | ROCKNIX | ROCKNIX | `/storage/roms/bios` |
| `/etc/os-release` `OS_NAME=knulli` or `/usr/share/knulli/knulli.version` | KNULLI | KNULLI | `/userdata/bios` |
| `/etc/os-release` `ID=lakka` | Lakka | Lakka | `/storage/system` |
| `/usr/share/batocera/batocera.version` | Batocera | Batocera | `/userdata/bios` |
| `/recalbox/recalbox.version` or `/usr/bin/recalbox-settings` | Recalbox | Recalbox | `/recalbox/share/bios` |
| `/opt/muos` | muOS | RetroArch | `system_directory` of `/opt/muos/share/info/config/retroarch.cfg`, else of the default it is copied from: `/run/muos/storage/bios`, which muOS binds to `MUOS/bios` on the second card when that card holds one, else the first. Cores are read from that config's `libretro_directory` (`/opt/muos/share/core`) |
| `/mnt/mmc/MUOS/` | muOS | RetroArch | `/mnt/mmc/MUOS/bios` |
| `/etc/os-release` `NAME=AmberELEC` (CHAos writes the same) | AmberELEC | AmberELEC | `/storage/roms` (its list names `bios/`, `ports/` and `neogeo/` there) |
| `/usr/bin/batocera-info` naming PAN4ELEC, on AmberELEC | PAN4ELEC | AmberELEC | as AmberELEC |
| `/etc/os-release` `NAME=EmuELEC` | EmuELEC | EmuELEC | as AmberELEC |
| `/etc/os-release` `OS_NAME=JELOS` | JELOS | RetroArch | `/storage/.config/retroarch/retroarch.cfg`, else `/storage/roms/bios` |
| `/usr/lib/systemd/system/jelos-ready.service`, on JELOS | KOLOS | RetroArch | as JELOS |
| `/etc/os-release` `OS_NAME=UnofficialOS` | UnofficialOS | RetroArch | as JELOS |
| `/etc/os-release` `OS_NAME=ArchR` | ArchR | RetroArch | as JELOS |
| `/home/ark` and `/opt/system`, or `title=ArkOS` in `/usr/share/plymouth/themes/text.plymouth` | ArkOS | RetroArch | `/home/ark/.config/retroarch/retroarch.cfg` then `retroarch32/retroarch.cfg`, else `/roms/bios` |
| `title=dArkOS` in the same file (the dArkOSRE, dArkOSen and ArkOS4Clone images keep it) | dArkOS | RetroArch | as ArkOS |
| `title=RetroOZ` in the same file, `/opt/.retrooz` or `/etc/retrooz_version` | RetroOZ | RetroArch | `/home/odroid/.config/retroarch/retroarch.cfg` then `retroarch32/retroarch.cfg`, else `/roms/bios` |
| `/mnt/SDCARD/spruce` | spruceOS | RetroArch | `/mnt/SDCARD/Saves/ra-configs/retroarch-*.cfg`, else `/mnt/SDCARD/BIOS` |
| `/mnt/SDCARD/System/usr/trimui/crossmix-version.txt` | CrossMix-OS | RetroArch | `/mnt/SDCARD/RetroArch/retroarch.cfg`, else `/mnt/SDCARD/BIOS` |
| `/mnt/SDCARD/.tmp_update/onionVersion/version.txt` | Onion | RetroArch | `/mnt/SDCARD/RetroArch/.retroarch/retroarch.cfg`, else `/mnt/SDCARD/BIOS` |
| `/mnt/SDCARD/.allium` | Allium | RetroArch | as Onion |
| `/mnt/SDCARD/Koriki/version.txt` | Koriki | RetroArch | as Onion |
| `/etc/main` and `/usr/bin/miyooctl` | MiyooCFW | RetroArch | `/mnt/.retroarch/retroarch.cfg`, else `/mnt/.retroarch/system` |
| `/mnt/plumos-user` | plumOS | RetroArch | `/mnt/plumos-user/bios`, which its launcher appends to every RetroArch run |
| `/etc/os-release` `ID=plumos` without the folder above | plumOS | none | its builds choose a folder per launch: named, not installed to |
| `/usr/trimui` | TrimUI | none | no published source names the stock firmware's folder: named, not installed to |
| `/mnt/SDCARD/.system/res` or `/mnt/sdcard/.system/res` | MinUI, NextUI or MyMinUI, as the first line of the card's `.system/version.txt` names it | MinUI, NextUI or MyMinUI | the card root, below which the list writes `Bios/<tag>/`. Without `version.txt` the three cannot be told apart: named, not installed to, and the message gives `--platform` and `--dest` to use |
| `/mnt/vendor/bin/dmenu.bin` | Anbernic stock firmware | RetroArch | `/mnt/mmc/bios` |

AmberELEC, EmuELEC and KNULLI install the BIOS list they publish themselves. TheRA is not detected: no
image or source of its Odroid Go builds is published, so neither its marker
files nor its folder could be read.

Batocera, Recalbox, ROCKNIX and Lakka also name the board they were built for, and
that board narrows the install unless `--target` or `--every-board` says
otherwise (see the options above). A board with no core list of its own is
named and gets the files of every board. KNULLI names its board in
`/usr/share/knulli/knulli.arch` but publishes no core list per board, so the
run says so and installs the files of every board.

Some platforms keep their BIOS in a fixed directory, and that directory is the
evidence. They are looked for after the configurations below, for a platform
not already found that way.

| Evidence | Platform | BIOS directory |
|----------|----------|----------------|
| `/media/fat/MiSTer` | MiSTer FPGA | `/media/fat/games` |
| `C:\RetroBat\bios` or `%USERPROFILE%\RetroBat\bios` | RetroBat | the same |
| `~/Emulation/bios` (Linux and macOS) | EmuDeck | the same |
| `~/.var/app/net.retrodeck.retrodeck` | RetroDECK | `~/retrodeck` |
| `/romm/library/bios` | RomM | the same |
| `/romm/config/config.yml` | RomM | `/romm/library/bios`, laid out by its firmware template |
| `~/RetroPie/BIOS` | RetroPie | the same |
| `%USERPROFILE%\BizHawk\Firmware` | BizHawk | the same |
| `EmuHawk.exe` or `EmuHawkMono.sh` in a `BizHawk*` folder of the home folder or one level down (`~/Games/BizHawk-2.11.1`), the newest | BizHawk | `Firmware` beside it |

A `--dest` given without `--platform` is first read as an SD card: the MinUI
family writes the release name as the first line of `.system/version.txt` at
the card root (`MinUI-`, `NextUI-`, `MyMinUI-`), and that line names the file
list.

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
| `HKCU\SOFTWARE\RetroBat` `LatestKnownInstallPath`, which `RetroBat.exe` writes on every start (read through `reg.exe` under WSL) | RetroBat | that folder + `\bios`, before the fixed folders above |
| `~/.config/EmuDeck/settings.sh` (Linux and macOS) | EmuDeck | `emulationPath` + `/bios` |
| `%APPDATA%\EmuDeck\settings.ps1`, else `%USERPROFILE%\EmuDeck\settings.ps1` | EmuDeck | `$emulationPath` + `\bios`, `C:\Emulation\bios` when it names none (a network install: the `$VolName` share + `\Emulation\bios`) |
| `~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json` | RetroDECK | `paths.rd_home_path`, falling back to the pre-migration `retrodeck.cfg` and its `rdhome`. A folder RetroDECK moved on its own is followed: files for `bios/` go to `paths.bios_path`, files for `roms/` to `paths.roms_path`, and the run names the new place |
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
| `<shared storage>/Android/data/<package>/files/RetroArch/` | RetroArch (Android, Google Play build) | `<shared storage>/Android/data/<package>/files/RetroArch/system` |
| `<shared storage>/RetroArch/` | RetroArch (Android, retroarch.com build) | `<shared storage>/RetroArch/system` |

The packages are `com.retroarch.aarch64`, `com.retroarch` and
`com.retroarch.ra32`, the flavours the frontend publishes. The first two are
published both on Google Play and on retroarch.com, and the two builds keep
their folders in different places (see [Android](#android)). Both config
paths are unreadable from Termux on Android 11 and later, so the directories
themselves are what answers on current devices.

ES-DE and LaunchBox are reported when present and nothing is written to them:
they reference emulators rather than owning a BIOS directory. ES-DE is looked
up at `$ESDE_APPDATA_DIR` or `~/ES-DE`, the two locations its own
`getAppDataDirectory` resolves.

When no platform is found but standalone emulators are (an ES-DE setup with
DuckStation and PCSX2, for one), the installer fills their own BIOS folders:
the folders of the [standalone copies](#standalone-copies) list that exist on
the machine, with the files that list names, taken from the EmuDeck file list
and then RetroArch's. At a terminal it asks first. `--check` and `--dry-run`
work as for a platform. With `--system`, `--core`, `--emulator`, `--region` or
`--target` the run asks for a platform as before, since those name a
platform's lists.

When a platform named with `--platform` is not found on the machine, the
destination falls back to the directory its detection looks for:
`/userdata/bios` for Batocera, `/recalbox/share/bios` for Recalbox,
`/storage/system` for Lakka, `~/retrodeck` for RetroDECK, `~/Emulation/bios`
for EmuDeck (`C:\Emulation\bios` on Windows), `/storage/roms/bios` for ROCKNIX,
`/userdata/bios` for KNULLI, `/storage/roms` for AmberELEC and EmuELEC, `/media/fat/games` for MiSTer,
`~/RetroPie/BIOS` for RetroPie, `/romm/library/bios` for RomM,
`C:\RetroBat\bios` for RetroBat, and RetroArch's own default system
directory for RetroArch. BizHawk has no such folder: it reads `Firmware`
beside its executable wherever the archive was extracted, so a forced
`--platform bizhawk` that finds no install stops and asks for `--dest`. MinUI,
NextUI and MyMinUI keep their files on the SD card. Run on the device, the
installer finds the card root. From a computer the card is mounted on, a
forced `--platform minui` stops and asks for the card's root as `--dest`, and a `--dest` that is a folder inside such a
card (its `Bios/`) is refused with the root to name instead. On Android a forced `--platform retroarch` falls back to
`<shared storage>/RetroArch/system` instead, a home directory there belonging
to Termux rather than to the frontend. Pass `--dest` to say where instead of
relying on that.

With nothing detected, the installer lists the platforms and asks. With several
detected, it asks which one. Both prompts need a terminal: piped into a script
with no platform to install for, it prints the manual invocation, in the form
the run was started with (the one-line command for a one-liner user), and
exits 1.

## Standalone copies

Some files are read by a standalone emulator from its own data directory
rather than from the platform's BIOS tree. `--standalone-copies` copies them
there after the install, and only then: without the flag nothing is written
outside the selected tree, apart from the files kept in `retrobios-backup`.

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
that is not installed. A destination that is already a symbolic link is
replaced by the file rather than written through: a write through the link
would land outside the folder the user opted into. The summary counts each
copy as copied, skipped (folder absent) or failed, and names every failure.
A run without the flag lists the emulator folders of this list that exist on
the machine, and says nothing when there are none.

## RomM firmware layout

RomM 5.4 places each platform's firmware where `filesystem.structure.firmware`
in its `config.yml` says, relative to the library: `bios/{platform}` by
default, the layout of the pack. A library laid out as `{platform}/roms` makes
RomM stop until `config.yml` declares its templates, and RomM suggests
`firmware: "{platform}/bios"` for it.

The installer reads that value from `config.yml` in the `config` folder beside
the library, `/romm/config/config.yml` inside the container, and places each
file accordingly: with `{platform}/bios`, `psx/scph5501.bin` goes to
`library/psx/bios/scph5501.bin`. `--dest` still names the library's `bios`
folder, whose parent is the library. Run from the host of a container, where
`config.yml` sits elsewhere, `--romm-firmware` gives the same value. A
library laid out as `{platform}/roms` with no template found is refused
rather than filled in the wrong place, and the message gives the command.

## Android

Android reports itself as Linux, so the runtime is named from its own
variables instead: `ANDROID_ROOT` and `ANDROID_DATA`, which Termux inherits,
with `/system/build.prop` covering a shell started without them.

Detection then looks for each installed RetroArch package. Its
`retroarch.cfg` is read from the app external files directory and then from
the internal one, the order the frontend itself follows (RetroArch v1.22.2,
`UserPreferences.java:36-89`), and a `system_directory` it names is used as
written. Left at `default`, it resolves against `<storage>/RetroArch`
(`platform_unix.c:1670-1697`), where `<storage>` depends on the build
(`MainMenuActivity.java:182-183`):

| Build | Packages | `<storage>` |
|-------|----------|-------------|
| Google Play | `com.retroarch`, `com.retroarch.aarch64` | `<shared storage>/Android/data/<package>/files` |
| retroarch.com | `com.retroarch`, `com.retroarch.aarch64`, `com.retroarch.ra32` | `<shared storage>` |

The two builds share their package ids (`pkg/android/phoenix/build.gradle:46-95`),
so the build is told by what it wrote. The version code the launcher records
in `retroarch.cfg` (`bundle_assets_extract_version_current`) is below
1609632000 for a Google Play build and the build time in seconds for the
others (`build.gradle:42, 168-175`). Without a readable config, a
`RetroArch` folder under `Android/data/<package>/files` is the Google Play
build, and `<shared storage>/RetroArch` is the retroarch.com build, the
Google Play build never asking for shared storage
(`MainMenuActivity.java:199-202`). When nothing tells them apart, the run
names both folders and asks for `--dest`. The Google Play folder sits in
`Android/data`, which Termux cannot write on Android 11 and later: the run
then names it, as it does for Flycast below.

Two Android emulators keep their BIOS elsewhere. DraStic reads
`/data/data/com.dsemu.drastic/files/DraStic/system`, private to the app, and
Flycast reads `Android/data/com.flycast.emulator/files/data` in shared
storage, which Android 11 and later keep from other apps. No app can write
the first without root, and Termux cannot write the second on Android 11 and
later. When either emulator is installed and its folder is out of reach, the
run names it and the files that are not copied. Where the Flycast folder is
writable (Android 10 and earlier), it is a standalone copy target.

Under scoped storage the two RetroArch config paths are unreadable from Termux on
Android 11 and later, so on current devices the folders are what answers.
Running `termux-setup-storage` once is what makes the shared one reachable:

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

## On a handheld { #on-a-handheld-the-retrobios-port }

RetroBIOS also runs on the handheld itself, in the
[full-screen interface](#the-full-screen-interface), with the controller.
Take the file for your system from the
[latest release](https://github.com/Abdess/retrobios/releases/latest), copy
it to the SD card, then start RetroBIOS from the menu.

| System | File | Copy it to | Start it from |
|--------|------|------------|---------------|
| Any system with [PortMaster](https://portmaster.games/): ArkOS, dArkOS, ROCKNIX, AmberELEC, JELOS, muOS, Knulli | `RetroBIOS.zip` | PortMaster's `autoinstall` folder: `/roms/ports/PortMaster/autoinstall/` on ROCKNIX and AmberELEC, `/roms/tools/PortMaster/autoinstall/` on ArkOS, `/mmc/MUOS/PortMaster/autoinstall/` on muOS, `/userdata/system/.local/share/PortMaster/autoinstall/` on Knulli | Ports, after opening PortMaster once |
| Batocera, Knulli | `RetroBIOS-batocera.zip`, extracted | `roms/ports` on the SHARE partition | Ports, after updating the game list |
| muOS | `RetroBIOS.muxapp` | `ARCHIVE` at the root of the card, then install it from the Archive Manager | Applications |
| ArkOS, dArkOS | `RetroBIOS-tools.zip`, extracted | `tools` on the EASYROMS partition | Options, then Tools |
| ROCKNIX, JELOS | `RetroBIOS-tools.zip`, extracted | `/storage/.config/modules`, over the network share or SSH | Tools |
| AmberELEC | `RetroBIOS-tools.zip`, extracted | `/storage/.config/distribution/modules` | Tools |

A release cut before these files existed carries none of them. From a clone
of the repository, `python scripts/build_port.py` writes all four into
`dist/`.

| Button | Action |
|--------|--------|
| D-pad | Move |
| A, Start | Choose, continue |
| B, Select | Back, and quit on the first screen |
| X | Tick or untick a system |
| Y | Options |
| L1, R1 | Page up, page down |
| Start + Select | Quit at once (PortMaster port, Tools) |

On muOS the interface runs in muxterm, which already reads Start, B, Y (tick)
and the D-pad. A, X, L1 and R1 come from PortMaster's gptokeyb2 when
PortMaster is installed, and Menu quits.

**Without Wi-Fi.** On a computer, download the BIOS pack whose name contains
your system (every part, for a pack in parts) and copy it next to RetroBIOS:
into `ports/retrobios` for the PortMaster port and on Batocera, to the root
of the card on muOS, into the `retrobios` folder of the Tools entry. With no
connection, RetroBIOS installs from it, as
[`--from-pack`](#without-a-network-offline-install) does. With no pack there,
it names the pack and the folder.

On a two-card device, RetroBIOS without Wi-Fi first asks to **copy the BIOS
files from your other card**: muOS's `MUOS/bios` on the card the system does
not read, or the other of `/roms/bios` and `/roms2/bios` on ArkOS and dArkOS.
A says yes, as [`--from`](#copy-from-an-old-card) does. The list of files
still comes from the pack, so the pack is needed too.

**What it needs.** Python 3.8 or newer: the system's, else PortMaster's
`python_3.11` runtime on aarch64, which the PortMaster port downloads on
first use and the Tools entry then reuses. That runtime uses the system's
OpenSSL 1.1, so on an image with OpenSSL 3 only it may not download (not
tried on a device). The BIOS folder must be one the installer detects, see
[Platform detection](#platform-detection).

**Where no terminal shows.** The PortMaster port draws on the console, or in
foot or xterm under a desktop session. Where neither can, it opens
PortMaster's own screens instead: the same check, update and choice of
systems, one list at a time. On Batocera and Knulli the visible terminal is
not verified on a device yet: RetroBIOS tries xterm or foot, then console 4,
and `retrobios/log.txt` names the route it took.

**From a computer.** Onion, MinUI, NextUI, Allium, GarlicOS and the Switch
have no Python 3 to run the installer. Put the card in a computer and run the
[one-line command](#one-line) there with
`--platform <platform> --dest <BIOS folder on the card>`. This works for
every system above too.

**Which installer runs.** Each file carries `install.py`, the one the
one-line command of the same commit downloads: `scripts/build_port.py`
refuses any other (it compares it with the SHA-256 `install.sh` pins). The
file lists come from `main`. The launchers in `ports/` start it with `--tui`,
and read it through [Machine-readable events](#machine-readable-events) for
the rest.

## Without a network (offline install) { #without-a-network-offline-install }

<span id="without-a-network"></span>

A pack downloaded once installs again with no network, on this machine or on
one that has none:

```bash
python install.py --from-pack ~/Downloads/RetroArch_v1.22.2_BIOS_Pack.zip
python install.py --from-pack ~/Downloads/RetroArch_v1.22.2_BIOS_Pack.part1of3.zip
python install.py --from-pack ~/Downloads --dest /media/card/retroarch/system
```

One part is enough to name a split pack: the others are taken from the same
folder, and a part that is not there is named before anything is written.
The platform is read from the pack's name when `--platform` is not given. The
file list is the one the pack carries (`manifest.json`), with each file's
SHA-1 and size, and every file is checked against it before it takes its
place. A data tree (Dolphin's `Sys/`, PPSSPP's assets) comes from the pack
too, not from its source archive. `--target` reads a core list from the
network and is refused with `--from-pack`. A board found on the device is not
applied either: every board's files are installed.

`--manifest FILE` reads the file list from a copy of `install/<platform>.json`
instead of downloading it. Alone, the files still come from the repository.
With `--from-pack`, the run reads that list and takes every file from the
pack: `--system`, `--core`, `--region` and `--no-game-data` then apply as
they do online, and a file of the list the pack does not hold fails as
`not in the pack`. The list's platform has to be the one installed for.

## Where the files come from

Each manifest entry names either `repo_path`, served from the repository at the
pinned revision, or `release_asset`, served from the `large-files` release for
the files above the size GitHub carries in a tree. Everything else in the entry
(`dest`, `size`, `sha1`, `sha256`, `cores`) describes what must land where and
what it must hash to.

Entries the collection cannot satisfy are not silently dropped: they travel in
`omitted_files` with a reason, and the run prints the count before downloading
anything. `not_found` means no file in the collection matches the entry, which
is what the gap analysis tracks (Reference: [What the packs do not hold](../gaps.md)).

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
POSIX, `py -3` then `python3` then `python` on Windows). Without one,
`install.sh` names the command that installs it on the system it runs on
(`pkg install python` in Termux, which it offers to run since Termux installs
without root, `xcode-select --install` on macOS, apt, dnf, pacman, zypper or apk
from `/etc/os-release`), and `install.ps1` at a console offers to run `winget
install Python.Python.3.12`, then reads the new PATH and carries on.
`install.sh` needs `curl` or `wget` (GNU or BusyBox) and `sha256sum` or `shasum`.
Windows PowerShell 5.1 still negotiates SSL 3.0 and TLS 1.0 by default and
GitHub refuses both, so `install.ps1` raises the floor to TLS 1.2 before
downloading, and `install.sh` pins the same floor with `curl --tlsv1.2`.

They delete the temporary installer on the way out, including on interrupt.
Run from a clone, `install.sh` and `install.ps1` reuse the `install.py` sitting
next to them and skip the download entirely. Piped from stdin, `install.sh`
never treats the working directory as a trusted location for it.

Piped from `curl`, the shell's standard input is the script itself, so
`install.sh` hands the installer the terminal (`/dev/tty`) when there is one.
That keeps the installer's questions working when the script is piped from
`curl`.

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
selected: a symlink already sitting at the destination is replaced by the
file rather than followed.

## Changing the installer

`install.py` runs on the user's machine, often an older handheld, so it stays
on Python 3.8 and the standard library alone. The RetroPie 4.8 images
(`retropie-buster-4.8-*.img.gz`) are Debian Buster, whose `python3` is 3.7.3:
there the bootstrap stops before running anything, and the way to install is
the command run on a computer with the Pi's network folder as `--dest`
([first install](first-install.md#error-python-38-or-newer-is-required)).
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
| `1` | a failure: the one-liner could not download or verify `install.py`, or found no Python 3.8 (nothing was run), an unusable base URL, an unknown platform or target, `--installed-cores` with no core found, a manifest that fails validation, a fetch that never succeeded, no platform to install for in a non-interactive run, some files failed while others installed, `--check` found files to fetch, a `--from-pack` pack that cannot be read, lacks a part or is for another platform, a `--from` folder that does not exist, or `--restore` could not put a file back |
| `2` | nothing was installed: every file the run tried failed (a BIOS folder that cannot be written, for one), the drive has too little free space for the download (checked before any folder is created), or the question about a folder that does not exist, named with `--dest` or typed at the folder question, was answered no. Also an option the installer refused before any download: an unknown option, `--jobs` outside 1 to 32, `--target` with `--every-board` or `--from-pack`. The message then starts with `install.py: error:`. With `--restore`, a folder that is not a dated folder inside `retrobios-backup` |
| `130` | stopped with Ctrl+C, or with `q`, Ctrl+C or Ctrl+D at a question. Run the same command again to carry on |
| `141` | the output was piped to a reader that closed early (`--list-platforms \| head`) |

`install.ps1` passes the installer's code on unchanged: run as a file it exits
with it, run through `irm \| iex` it sets `$LASTEXITCODE` (an `exit` there
would close the PowerShell window). It declares no parameter of its own, so
every argument reaches `install.py` as typed, short forms such as `-v`
included. Its body runs in a script block of its own,
so `irm \| iex` leaves neither its variables nor its `$ErrorActionPreference`
in the session.

Failures that are per-file, such as a download exhausting its three attempts,
are counted and reported at the end.

To tell missing files from a failure, read the `check` event of
[Machine-readable events](#machine-readable-events): `missing` and `wrong_hash`
count what is left.

## Machine-readable events

With `--events`, standard output carries only JSON objects, one per line, and
the text a person reads goes to standard error. A program driving the installer
(the [handheld port](#on-a-handheld-the-retrobios-port) is one) reads the
objects and never parses the text. Every object has an `event` key naming its
kind, and each kind has exactly the keys below, in this order. The schema is
`EVENT_KEYS` in `install.py`, and a test holds this table to it.

| Kind | Keys | When |
|------|------|------|
| `start` | `schema`, `installer`, `mode` | First line. `schema` is 1, `installer` the identity `--version` prints, `mode` one of `install`, `check`, `dry-run`, `list` |
| `detect` | `os`, `os_name`, `platforms` | Once the platforms are known. `os_name` comes from `/etc/os-release`. `platforms` lists `{platform, name, bios_dir}`, and is empty when nothing supported was found |
| `error` | `stage`, `reason`, `platform`, `message` | The file list could not be read (`stage` is `manifest`): `reason` is `network`, `not_found` or `invalid`, `clock` when the server's certificate was refused and the system clock reads a date before the installer was written, or `no_ca` when the machine holds no list of trusted certificates (`SSL_CERT_FILE` names one). A folder named with `--from` is not there (`stage` is `from`, `reason` `not_found`, or `not_a_folder` for a file that is no ZIP). `message` says it in words |
| `checking` | `files_done`, `files_total` | While the files already in the folder are read and hashed, at most every half second and once at the end. `files_total` counts the files found in place |
| `file` | `platform`, `dest`, `systems`, `size`, `state` | Each file of the list after the check. `state` is `present` (hash verified), `wrong_hash` or `missing` |
| `system` | `platform`, `system`, `name`, `files`, `present`, `missing`, `wrong_hash`, `bytes_needed` | Each system, sorted by id, and with `--system` only the systems named. Files with no system count under `other`. `name` is the platform's own name for it |
| `check` | `platform`, `name`, `bios_dir`, `files`, `present`, `missing`, `wrong_hash`, `bytes_needed`, `data_missing`, `not_installable` | The platform's totals. `data_missing` counts data-tree files to fetch, `not_installable` the files the collection cannot supply |
| `found` | `platform`, `dest`, `folder`, `path`, `member`, `reason` | Each file to install that this machine already holds, after `check` and before anything is written, in `--check` and `--dry-run` too. `folder` is the `--from` folder as given, or the `bios_dir` of the check when the BIOS folder holds the file at another place, and then `reason` says where in words (`nested folder: ...` for a misplaced file, `the same file is at ...` for one the list installs there too). `path` is the file, and `member` the ZIP member holding the bytes when `path` is an archive, an empty string otherwise. Its copy is reported by a `downloaded` event |
| `download` | `platform`, `files`, `bytes` | Before the downloads start. Counts the files copied from this machine too |
| `progress` | `files_done`, `files_total`, `bytes_done`, `bytes_total` | At most every half second while bytes arrive |
| `downloaded` | `dest`, `ok`, `reason`, `size`, `files_done`, `files_total`, `bytes_done`, `bytes_total` | Each finished file, downloaded or copied from where a `found` event named it. `reason` says why one failed, and is empty otherwise |
| `summary` | `mode`, `installed`, `up_to_date`, `pending`, `failed`, `skipped`, `left_out`, `kept`, `interrupted` | The outcome. `pending` counts what a next run would still fetch, `failed` lists `{dest, reason}`, `skipped` lists `{reason, label, files, required}` for the files the collection cannot supply, `left_out` lists `{category, label, files, dests}` for what `--no-game-data` left out, and is empty without it. `kept` lists `{folder, files, bytes}`, one per backup folder, for the files in place the run moved aside before replacing them (with `--dry-run`, would move), and is empty when none. Not written for a listing |
| `end` | `code` | Last line, with the exit status the process returns (see [Exit status](#exit-status)) |

Schema 1 can gain a kind (`found` came with `--from`), never a key of an
existing kind: a reader skips a kind it does not know. An option refused
before the run starts (`install.py: error:`, code 2) writes
no event at all. A program reads a missing `end` as that case.

## Environment overrides

| Variable | Effect |
|----------|--------|
| `RETROBIOS_REF` | Branch or tag the file list and the files committed to the repository are read from. Defaults to `main`. Files the manifest marks `release_asset` always come from the `large-files` release, whatever the ref, and that release replaces an asset in place when it is rebuilt. Each release deletes the previous release tags, so a tag holds a file list until the next release. For contributors: [release process](release-process.md#cutting-a-release) |
| `RETROBIOS_BASE_URL` | Base URL for the manifest and the files it declares. HTTPS only, plain HTTP allowed to loopback for end-to-end tests. Whoever controls this base controls the expected hashes, hence the check. A file list that cannot be fetched names the address, and this variable when it is set |
| `RETROBIOS_OS` | Names the host outright (`linux`, `wsl`, `windows`, `darwin`, `android`) instead of detecting it |
| `EXTERNAL_STORAGE` | Android only, the shared storage root RetroArch builds its default directories from. Defaults to `/storage/emulated/0` |
| `RETROBIOS_INSTALL_URL` | Where the bootstrap fetches `install.py`. HTTPS only |
| `RETROBIOS_INSTALL_SHA256` | The SHA-256 the bootstrap requires of that installer, 64 hex characters. When either of these two is set and the fingerprint does not match, the error names the variable instead of suggesting a retry |
| `RETROBIOS_BOOTSTRAP` | Set by `install.sh` (`sh`) and `install.ps1` (`ps1`) for the temporary installer they run, so that its hints give back the one-line command rather than `python3 install.py` |
| `NO_COLOR` | Plain text, no colour, even at a terminal, when set to any value but an empty one. `TERM=dumb` also turns the live progress line off |
| `FORCE_COLOR`, `CLICOLOR_FORCE` | Colour even when the output is a pipe or a file, set to any value but `0` or an empty one. `NO_COLOR` wins over both, and the progress line stays one line per file |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | The trusted certificates to check servers with, for a Python that finds none of its own |
| `RETROBIOS_PLAIN`, `ACCESSIBLE` | Set to anything but `0`: the questions come line by line, as with `--plain`, instead of the full-screen interface |
| `HTTPS_PROXY`, `NO_PROXY` | Honoured for every download through the standard library's default proxy handling: one names the proxy, the other the hosts to reach directly |

## When something goes wrong

The common cases, with the short answer, are on
[When a game does not start](troubleshooting.md). The details follow.

### Network

The installer fetches its manifest, then downloads only the repository or
`large-files` release objects it needs. When a download fails, check that
`https://github.com` opens. Behind a proxy, set `HTTPS_PROXY` in the
environment. A file that fails three attempts is counted and reported at the
end, and the next run fetches only what is still missing.

The reason is said in words: no internet connection or a name that could not
be looked up, a connection that timed out, was refused or was cut, a
certificate that was rejected. `--verbose` adds the text Python gave. Two
causes are not the network and are not retried:

- `The clock reads 1980-01-01`: a device without a clock battery starts on an
  old date, and every certificate then looks not yet valid. Connect to Wi-Fi
  and let the time sync, or set the date, then run again.
- `This system has no list of trusted certificates`: install the
  `ca-certificates` package, or name a certificate bundle with `SSL_CERT_FILE`.
  When the system keeps one where this Python does not look, the message names
  it. Certificate checks are never turned off.

### Permission denied

The installer needs to write to the BIOS folder. On Linux and macOS, check who
owns it (`ls -la`). On Batocera, Recalbox and ROCKNIX the folder belongs to
root, so the same command with `sudo` in front of it succeeds. On Windows, run
PowerShell as Administrator only for a protected folder.

A file system mounted read-only refuses root too, so the installer does not
suggest `sudo` for it. Linux remounts a card or drive read-only after an error,
an unclean removal for one: check it (`fsck`, or `chkdsk` on Windows) and
mount it read-write again.

A file already in the BIOS folder with the read-only attribute is replaced
like any other: on Windows the installer clears the attribute first.
`file in place is read-only` appears only when Windows refuses that too, which
a file owned by another account can cause.

### Platform not detected

The installer finds a platform from the config files it knows. When it finds
none, name the platform and its BIOS folder:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retroarch --dest ~/.config/retroarch/system
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform batocera --dest /userdata/bios
```

```powershell
&([scriptblock]::Create((irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1))) `
  --platform retroarch --dest C:\RetroArch-Win64\system
```

`--list-platforms` lists the platforms the installer knows and the ones it
finds here. `--help` lists the options with examples.

### No file list

The installer reads its manifests from the revision the bootstrap checked it
against. A platform name is checked before anything is downloaded, so a
"No file list" message comes from the revision: `RETROBIOS_REF` naming a tag
that does not exist, or one older than the platform. Unset it, or pick a tag
from the releases page ([Environment overrides](#environment-overrides)).

### Upper and lower case

Linux file systems tell `Bios.ROM` and `bios.rom` apart. Windows and macOS do
not, by default.

The RetroArch pack is built for every system RetroArch runs on, so it holds
one name per file: `BIOS.col`, never `bios.col` beside it, since unzipping both
on Windows or macOS would overwrite one with the other. On Linux, Mesen-CE
opens `bios.col` and NP2kai opens the lowercase `np2kai/2608_*.wav`, which the
pack only holds as `BIOS.col` and `2608_*.WAV`. The installer checks whether
the BIOS folder keeps case and, when it does, also fetches these names. On
Linux, use the installer rather than unzipping the pack by hand.

The reverse happens on Batocera and Recalbox, whose lists hold both spellings
because their systems keep case: NP2kai reads `np2kai/bios.rom` and
`np2kai/BIOS.ROM` as two files. Written to a folder that folds case (the share
opened from Windows, an exFAT card), one overwrites the other. The installer
checks the folder first, keeps one file per name there and lists the ones it
left out.

### Packs and their parts

`scripts/download.py --list` and `scripts/download.sh --list` print the packs of
a release. A pack's name lists every platform it serves. Releases up to
v2026.10.10 ship RetroArch and Lakka as one `RetroArch_Lakka` pack. Later
releases publish packs only for the setups most people download, so Lakka,
which runs on Linux where two names that differ by case are two files, is
served by the installer. The download scripts name the installer for a setup without a pack.

Parts named `Pack.part1of2.zip` are ordinary ZIPs: unzip each one into the same
folder, and check that all of them were downloaded. The name says how many
there are, and each part leaves a marker file naming every part, which
`--check` reads to name a part that is missing. `scripts/download.sh` and `scripts/download.py` download every part
and check it against the release's checksums before unzipping. Both stage
inside the destination folder, never in `/tmp`, which is a RAM disk on most of
these systems.
