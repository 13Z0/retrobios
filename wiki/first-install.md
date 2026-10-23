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

## 2. Check the setup it found

It opens a menu:

```
 RetroBIOS: Where do you play?
 Found on this machine:
 > <your setup>
   Another setup or folder
 BIOS folder: <its folder>
```

- **Right folder?** Press Enter.
- **Wrong folder?** Choose **Another setup or folder**, then browse to yours.
- **A handheld's SD card in a reader?** It is listed, as **muOS SD card**.

Arrows move, Enter chooses, Esc goes back. A handheld's A and B do the same.

## 3. Install

The next screen counts missing files and free space.
Press Enter on **Install**. **Choose systems** keeps the consoles you
play.

**Old card?** In a card reader, choose **Copy the BIOS files from your old
card or a folder**, then the card. It stays unchanged.

## 4. Wait

Esc or Ctrl+C stops, B on a handheld. Run again to resume.

## 5. Read the result

- **Done**: all files are in place.
- **Finished with errors**: run the command again. It retries only those.
- **Nothing installed**: the screen says why. See [If it stops](#if-it-stops).

For questions line by line (screen readers), add `--plain`.

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

## Undo

Files it replaces go to a `retrobios-backup` folder beside your BIOS folder.
The result says how to restore them.

## If it stops

### Nothing was found here.

Choose **Another setup or folder**, then your setup and its folder.

### Error: Python 3.8 or newer is required.

The message shows the command that installs Python. Run it, then run the
installer again.

RetroPie 4.8 has Python 3.7.3, and its update finds nothing newer. Run it on
a computer, with RetroPie's `bios` share open, `\\RETROPIE\bios` from Windows:

<!-- RetroPie-Setup scriptmodules/supplementary/samba.sh:49-52 (46128cd2) shares
$home/RetroPie/BIOS as "bios", and RetroPie docs/Transferring-Roms.md:29 names
\\RETROPIE. -->

<!-- os-tabs windows: --platform retropie --dest \\RETROPIE\bios\ -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retropie --dest <the bios share>
```

### Error: the downloaded installer does not match its expected fingerprint.

The download was cut short. Nothing was changed. Run the command again.

### Cannot create ...

You cannot write there. Do what the message says.

### The card is full

Nothing was written. Free some space, or choose another folder.

### The game still does not start

See [When a game does not start](troubleshooting.md).

## Next steps

- [Get your files](../which-pack.md)
- [When a game does not start](troubleshooting.md)
- Details: [Reading the installer's output, line by line](installer.md#reading-a-run)
