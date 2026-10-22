---
level: start
---

# First install

One path, from an empty BIOS folder to files the emulator can read. Every step
shows what the screen looks like when it worked.

If something on this page does not match what happens, the last section covers
the cases that come up.

<!-- image: install-flow -->

## Before starting

Three things, and a few minutes:

- The BIOS folder the emulator reads. The installer finds it by itself on most
  systems. On a handheld, that usually means plugging the SD card into a
  computer.
- `curl` or `wget`, and Python 3.8 or newer. The command below checks for both
  and stops with a message if either is absent.
- Room on the drive. The size depends on the platform and changes with each
  release: the run prints it before downloading (Step 3), and asks first when it
  reaches 1 GB. The installer also measures the free space and refuses before
  writing anything if there is not enough.

Nothing is written outside the BIOS folder, and games and saves are never
touched.

## Step 1. Run the command

The command runs in a terminal. On Windows, open the Start menu, type
PowerShell and open it. On a Mac, open Terminal from Applications, then
Utilities. On a Steam Deck, switch to Desktop Mode and open Konsole. Paste the
line and press Enter.

On Linux, macOS, a Steam Deck, or a handheld running Batocera, Recalbox,
KNULLI, ROCKNIX, AmberELEC, EmuELEC or Lakka:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex
```

PowerShell's execution policy does not block that line. The policy governs
script files, and this command never writes one. Changing the policy is not
part of installing anything here.

On Android, from Termux, three commands instead of one:

```bash
pkg install python
termux-setup-storage
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

Before running anything, that first line downloads the installer, compares it
against the SHA-256 written inside the command's own script, and refuses to
continue if the two differ. To read the script first:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | less
```

## Step 2. Watch it find the platform

The first lines name what it found and where it will write:

```
RetroBIOS 8c0e9c762d2f

Detecting platform...
  Found Batocera at /userdata/bios
```

The code after RetroBIOS identifies the installer: it is the start of the
SHA-256 the command checked before running it.

That path is the one thing worth reading twice. If it is not where the emulator
keeps its BIOS files, stop with Ctrl+C and see the last section.

At a terminal, the installer then asks what to install:

```
  Enter) install everything
  i) only what the installed cores read
  c) choose systems, cores and regions
  q) quit
```

Enter installs everything, which is what the rest of this page shows. **c**
lets you pick consoles, emulator cores and regions. The **i** line appears
only when the installer finds emulator cores installed.

## Step 3. Let it work

The examples on this page follow the Batocera list as published on 2026-10-21.
The figures change with each release.

Two phases follow. The first reads what is already in the folder, which takes a
few minutes on a memory card because every file is hashed:

```
Fetching file index for batocera...
  14481 files (9.1 GB)
  Not installable: 399 files (79 required, not found: 399).

Checking existing files...
  0/14481 present (0 verified, 0 wrong hash)
```

`Not installable` counts the files of the list that the collection does not
hold. They are named at the end of the run (Step 4).

On a first install the present count is zero, which is expected, not a fault.
On a later run it counts what is already there, and only the rest is
downloaded. A download of 1 GB or more is confirmed first
(`Download 9.1 GB to /userdata/bios? [Y/n]`).

The second phase downloads what is missing, one line per file:

```
Downloading 14481 files (9.1 GB)...
  Press Ctrl+C to stop. Running the same command again carries on from here.
  [1/14481] 3do_arcade_saot.bin ok
  [2/14481] 3dobios.zip ok
```

Ctrl+C is safe at any point. Files already installed stay, and running the
command again picks up where it stopped.

## Step 4. Read the end of the run

This is what a finished run looks like:

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

The word `Done` and a location are the confirmation. Any other ending (`Stopped`,
`Finished with errors`, `Nothing was installed`) means something was left undone,
and the run says which case it was.

The lines between `Location:` and the `--check` hint name the files of the list
that the collection does not hold. `Needed to start an emulator` gives, for each
system, the emulators that read it and the files they cannot start without. On
the run above, all 79 belong to games (Dark Forces, Diablo, Doom 3, Tomb Raider
and the others): the engine reads them from a copy of the game itself. The
other 320 are optional, and the emulators reading them start without them.

## Step 5. Confirm it later

The same command with `--check` reads the folder and writes nothing:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

```
Checking existing files...
  14481/14481 present (14481 verified, 0 wrong hash)

  All files up to date.
```

The files the collection does not hold are listed after it again, as long as
they are absent from the folder. `0 wrong hash` is the line that matters. A number there means a file is present
whose contents the platform will reject, and running the install again replaces
it.

## When the screen says something else

### No supported platform detected.

At a terminal, a menu follows that line. The platform is picked by its number,
then the run asks for the BIOS folder, offering the platform's usual folder on
this system as the default when it has one.

Without a terminal, or with `--no-input`, the run stops there and prints the
command to use instead, with the platform and the folder named:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/system
```

The accepted names come from `--list-platforms`. The page of each setup,
listed on [Get your files](../which-pack.md), gives its BIOS folder.

### Error: Python 3.8 or newer is required.

No Python 3.8 or newer was found: Python is missing, or older. The message
names the command that installs it on the system it runs on: `sudo apt install
python3` on Debian and Ubuntu, `sudo dnf install python3` on Fedora, `sudo
pacman -S python` on Arch, `xcode-select --install` on macOS and `pkg install
python` in Termux, where it offers to run that command itself (Termux installs
without root). On Windows, `install.ps1` offers to run `winget install
Python.Python.3.12`. On a handheld running Batocera or Recalbox it is already there, so
this message usually means the command ran on the computer rather than on the
console.

A stock RetroPie 4.8 image is Debian Buster, where `python3` is 3.7.3 and apt
has nothing newer, so the same command changes nothing. The RetroPie pack from
the [releases page](https://github.com/Abdess/retrobios/releases/latest),
extracted into `~/RetroPie/BIOS/`, installs the same files.

### Error: the downloaded installer does not match its expected fingerprint.

Nothing was run and nothing was written. The download did not match the
fingerprint the script carries, which is almost always a transfer cut short.
Running the command again, on another network if possible, is the answer.

### Cannot create ...

The user running the command cannot write there. On Batocera, Recalbox and
ROCKNIX the BIOS folder belongs to root, so the same command with `sudo` in
front of it succeeds.

### Not enough space on the drive holding ...

The run stopped before writing. The message names how much is needed and how
much is free. `--dest` installs to another drive.

### The game still shows a black screen

A file can be in place and still be the wrong one for that game, or the console
may need a file that nobody has dumped yet.
[When a game does not start](troubleshooting.md) goes through that case.

## What comes next

- The page of your setup, from [Get your files](../which-pack.md): where its
  folder is and how the setup itself checks the files
- [When a game does not start](troubleshooting.md)
- [FAQ](faq.md), for what the words mean and why a pack is this large
- Details: [All installer options](installer.md), for every option, what it
  detects and what it is allowed to write

To remove what the installer wrote, delete the files from the BIOS folder.
Nothing outside it was changed.
