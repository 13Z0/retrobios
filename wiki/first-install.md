---
level: start
order: 30
nav_title: First install, step by step
---

# First install

From an empty BIOS folder to games that start.

**You need:** internet and some free space. The installer says how much.

## 1. Open a terminal and paste

A **terminal** is a window where you type commands. Paste the line for your
system, then press Enter.

=== "Windows"

    Open the Start menu. Type **PowerShell** and open it.

    ```powershell
    irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex
    ```

=== "Mac"

    Open **Terminal**. It is in Applications, then Utilities.

    ```sh
    curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
    ```

=== "Linux and Steam Deck"

    On a Steam Deck, switch to Desktop Mode and open **Konsole**.

    ```sh
    curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
    ```

=== "Android"

    Install **Termux**, a terminal app. Run these three lines in it.

    ```sh
    pkg install python
    termux-setup-storage
    curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
    ```

On Batocera, Recalbox and other game systems, follow
[your setup's page](../which-pack.md).

<!-- image: install-flow -->

## 2. Check the folder it found

It shows your setup and its BIOS folder:

```
  Found Batocera at /userdata/bios
```

- **Right folder?** Go on.
- **Wrong folder?** Press Ctrl+C to stop. Then see [If it stops](#if-it-stops).

## 3. Choose what to install

```
  Enter) install everything
  i) only what the installed cores read
  c) choose systems, cores and regions
  q) quit
```

**Enter** installs everything. **c** lets you pick consoles.

## 4. Wait

It shows one line per file. You can stop with Ctrl+C at any time. Run the
command again to carry on.

## 5. Look for "Done."

```
Done. 14481 files installed, 0 files already up to date.
Location: /userdata/bios
```

`Done.` means it worked. It may list files it could not install.

## Check it later

Add `--check` to the same command. It changes nothing.

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

All good when you see `0 wrong hash`.

## Undo

It keeps every file it replaces in a `retrobios-backup` folder, beside your
BIOS folder. Nothing is deleted.

To put your files back, run the command with `--restore` and that folder. The
end of the run prints the exact line.

## If it stops

### No supported platform detected.

At a terminal, a menu follows. Pick your setup by its number, then type its
BIOS folder.

With `--no-input`, or without a terminal, it stops and prints the command to
use.

### Error: Python 3.8 or newer is required.

The message shows the command that installs Python. Run it, then run the
installer again.

A RetroPie 4.8 image has Python 3.7.3, and its update command finds nothing
newer. Use the [RetroPie ZIP](../platforms/retropie.md#download) there,
unzipped into `~/RetroPie/BIOS/`.

### Error: the downloaded installer does not match its expected fingerprint.

The download was cut short. Nothing was changed. Run the command again.

### Cannot create ...

You do not have the right to write there. On Batocera, Recalbox and ROCKNIX,
put `sudo` and a space before the command. It then runs as the administrator.

### Not enough space on the drive holding ...

It stopped before writing. Free some space, or use `--dest` to pick another
drive.

### The game still does not start

See [When a game does not start](troubleshooting.md).

!!! tip "Next"

    - [Your setup's page](../which-pack.md): its folder and how it checks files
    - [When a game does not start](troubleshooting.md)
    - Details: [Reading the installer's output, line by line](installer.md#reading-a-run)
