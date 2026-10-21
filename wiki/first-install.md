# First install

One path, from an empty BIOS folder to files the emulator can read. Every step
shows what the screen looks like when it worked.

If something on this page does not match what happens, the last section covers
the cases that come up.

## Before starting

Three things, and a few minutes:

- The BIOS folder the emulator reads. The installer finds it by itself on most
  systems. On a handheld, that usually means plugging the SD card into a
  computer.
- `curl` or `wget`, and Python 3.8 or newer. The command below checks for both
  and stops with a message if either is absent.
- Room on the drive. A full RetroArch set is 5.5 GB, Batocera 4.0 GB, MiSTer
  FPGA 24 MB. The installer measures the free space and refuses before writing
  anything if there is not enough.

Nothing is written outside the BIOS folder, and games and saves are never
touched.

## Step 1. Run the command

On Linux, macOS, a Steam Deck, or a handheld running Batocera, Recalbox,
KNULLI, ROCKNIX or Lakka:

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

## Step 3. Let it work

Two phases follow. The first reads what is already in the folder, which takes a
few minutes on a memory card because every file is hashed:

```
Fetching file index for batocera...
  12893 files (8.6 GB)

Checking existing files...
  0/12893 present (0 verified, 0 wrong hash)
```

On a first install the present count is zero, which is expected, not a fault.
On a later run it counts what is already there, and only the rest is
downloaded. A download of 1 GB or more is confirmed first
(`Download 8.6 GB to /userdata/bios? [Y/n]`).

The second phase downloads what is missing, one line per file:

```
Downloading 12893 files (8.6 GB)...
  Press Ctrl+C to stop. Running the same command again carries on from here.
  [1/12893] 3do_arcade_saot.bin ok
  [2/12893] 3dobios.zip ok
```

Ctrl+C is safe at any point. Files already installed stay, and running the
command again picks up where it stopped.

## Step 4. Read the last three lines

This is what a finished run looks like:

```
Done. 12893 files installed, 0 files already up to date.
Location: /userdata/bios
To check this later, run the same command with --check.
```

The word `Done` and a location are the confirmation. Any other ending means
something was left undone, and the run says which case it was.

## Step 5. Confirm it later

The same command with `--check` reads the folder and writes nothing:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

```
Checking existing files...
  72/72 present (72 verified, 0 wrong hash)

  All files up to date.
```

`0 wrong hash` is the line that matters. A number there means a file is present
whose contents the platform will reject, and running the install again replaces
it.

## When the screen says something else

### No supported platform detected.

The command was piped from the internet, so it cannot stop and ask a question.
Name the platform and the folder instead:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/system
```

The accepted names come from `--list-platforms`. For where each platform keeps
its BIOS folder, see [Getting started](getting-started.md).

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
[Troubleshooting](troubleshooting.md) goes through that case.

## What comes next

- [Troubleshooting](troubleshooting.md), when a game does not start
- [FAQ](faq.md), for what the words mean and why a pack is this large
- [Installer](installer.md), for every option, what it detects and what it is
  allowed to write
