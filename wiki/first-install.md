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
- **Wrong folder?** Press Ctrl+C. Run the command again with your folder
  after `--dest`:

<!-- os-tabs windows: --dest D:\path\to\bios -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --dest ~/path/to/bios
```

## 3. Choose what to install

```
  Enter) install everything
  i) only what the installed cores read
  c) choose systems, cores and regions
  q) quit
```

**Enter** installs everything. **c** lets you pick consoles.

## 4. Wait

Ctrl+C stops it. Run the command again to carry on.

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

All good:

```
  109/109 present (109 verified, 0 wrong hash)
```

An empty folder also shows `0 wrong hash`, with `0/109 present`.

## Undo

Every file it replaces goes to a `retrobios-backup` folder next to your BIOS
folder. The last lines say how to put them back.

## If it stops

### No supported platform detected.

At a terminal, a menu follows. Pick your setup by its number, then type its
BIOS folder, or press Enter for the usual one. A folder pasted with quotes
around it is fine.

With `--no-input`, or without a terminal, it stops and prints the command to
use.

### Error: Python 3.8 or newer is required.

The message shows the command that installs Python. Run it, then run the
installer again.

A RetroPie 4.8 image has Python 3.7.3, and its update command finds nothing
newer. Run the command on a computer, with
`--platform retropie --dest \\RETROPIE\bios\`.

### Error: the downloaded installer does not match its expected fingerprint.

The download was cut short. Nothing was changed. Run the command again.

### Cannot create ...

You cannot write there. Do what the message says.

### Not enough space on the drive holding ...

Nothing was written. Free some space, or pick another drive with `--dest`.

### The game still does not start

See [When a game does not start](troubleshooting.md).

## Next steps

- [Get your files](../which-pack.md)
- [When a game does not start](troubleshooting.md)
- Details: [Reading the installer's output, line by line](installer.md#reading-a-run)
