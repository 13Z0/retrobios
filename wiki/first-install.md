---
level: start
order: 30
---

# First install, step by step

From an empty BIOS folder to games that start.

**You need:** internet and some free space. The installer says how much.

## 1. Open a terminal and paste

A **terminal** is a window where you type commands. Paste the line for your
system, then press Enter.

<Tabs syncKey="os">
<TabItem label="Windows">

Open the Start menu. Type **PowerShell** and open it.

```powershell title="PowerShell"
irm https://raw.githubusercontent.com/Abdess/retrobios/main/install.ps1 | iex
```

</TabItem>
<TabItem label="macOS">

Open **Terminal**. It is in Applications, then Utilities.

```sh title="Terminal"
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

</TabItem>
<TabItem label="Linux">

Open a terminal.

```sh title="Terminal"
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

</TabItem>
<TabItem label="Steam Deck">

Switch to Desktop Mode and open **Konsole**.

```sh title="Terminal"
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

</TabItem>
<TabItem label="Android (Termux)">

Install **Termux**, a terminal app. Run these three lines in it.

```sh title="Terminal"
pkg install python
termux-setup-storage
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh
```

</TabItem>
</Tabs>

On Batocera, Recalbox and similar setups, see
[Get your files](../which-pack.md).

## 2. Check the folder it found

It shows your setup and its BIOS folder:

```
  Found <your setup> at <its folder>
```

- **Right folder?** Go on.
- **Wrong folder?** Press Ctrl+C. Run the command again with `--dest` and your
  folder.

## 3. Choose what to install

```
  Enter) install everything
  i) only what the installed cores read
  c) choose systems, cores and regions
  q) quit
```

**Enter** installs everything. **c** lets you pick consoles.

## 4. Wait

You can stop with Ctrl+C at any time. Run the command again to carry on.

## 5. Read the last line

- **`Done.`** `N files installed, M already up to date.` All files are in place.
- **`Finished with errors.`** Some files failed. Run the same command again.
  It retries only those.
- **`Nothing was installed.`** The lines above it say why. See
  [If it stops](#if-it-stops).

## What the command does

<!-- image: install-flow -->

## Check it later

Add `--check` to the same command. It changes nothing.

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

All good when you see `0 wrong hash`.

## Undo

It keeps every file it replaces in a `retrobios-backup` folder, next to your
BIOS folder. The end of the run prints the line that puts them back.

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

You do not have the right to write there. Do what the message says. It names
what to run.

### Not enough space on the drive holding ...

It stopped before writing. Free some space, or use `--dest` to pick another
drive.

### The game still does not start

See [When a game does not start](troubleshooting.md).

## Next steps

- [Get your files](../which-pack.md)
- [When a game does not start](troubleshooting.md)
- Details: [Reading the installer's output, line by line](installer.md#reading-a-run)
