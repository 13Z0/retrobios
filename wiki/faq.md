---
level: start
order: 50
---

# Common questions

## Do I need BIOS files?

For many consoles, yes: PlayStation 1 and 2, Saturn, Dreamcast, Neo Geo and
many home computers. For NES or Super Nintendo cartridges, usually not.
[What is a BIOS?](what-are-bios-files.md)

## How do I know which BIOS I need?

Run the command with `--dry-run`. It lists what it would install and changes
nothing.

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --dry-run
```

Your setup's page lists every file.

## Do I need every file of a console?

No. A console often has one BIOS per region. The ZIP holds them all. To keep
only yours, add your region, like `--region us`.

## Zip or unzip? { #zip-or-unzip }

Unzip the ZIP you downloaded once, into the folder your setup's page names.
Leave the ZIP files inside it as they are, like `neogeo.zip`. Arcade emulators
read them zipped.

## Does a BIOS work on every setup?

The same file usually works everywhere. Some setups also check the version and
report one they do not expect. Arcade files like `neogeo.zip` change with the
emulator. The command installs the version your setup expects.

## Can I play without the BIOS?

Sometimes. Some emulators carry a stand-in for it. Some games then glitch or
do not start.

## Is the command safe? { #is-piping-the-installer-into-a-shell-safe }

- It checks the installer's fingerprint first. If it does not match its expected
  fingerprint, it stops.
- It checks each file before saving it.
- It writes in your BIOS folder. For some emulators it also adds files to your
  save folder.
- It never changes or deletes a game or a save.
- It keeps any file it replaces in a `retrobios-backup` folder, next to your
  BIOS folder. `--restore` puts it back
  ([details](installer.md#files-you-already-had)).

Read the script first: [install.sh](https://github.com/Abdess/retrobios/blob/main/install.sh),
[install.ps1](https://github.com/Abdess/retrobios/blob/main/install.ps1) and
[install.py](https://github.com/Abdess/retrobios/blob/main/install.py).
Details: [How the installer checks itself](installer.md#what-the-bootstrap-does).

## Windows or my antivirus blocks the command

Download the ZIP of your setup instead, from [Get your files](../which-pack.md).
It holds the same files.

## Can I run it again?

Yes, to update. It downloads only what is missing or wrong.

## Fill an SD card from a computer?

Yes. Name your setup and the BIOS folder on the card. On Windows, `E:` stands
for the card.

<!-- os-tabs windows: --platform retroarch --dest E:\retroarch\cores\system -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

## Why is a file missing from the ZIP?

Some files come with the emulator. The ZIP leaves those out.

## Required or optional?

- **Required:** the emulator does not start games without it.
- **Optional:** games start, with small differences, like no start-up screen.

## My game shows a black screen

Most of the time, a BIOS file is missing or is the wrong version.
[When a game does not start](troubleshooting.md) shows how to check.

## Is this legal?

Laws differ by country, and nothing here is legal advice. Details, and how to
ask for a file to be removed: [Is it legal?](legal.md)

## Next steps

- [Get your files](../which-pack.md)
- [Words used on this site](glossary.md)
