---
level: start
---

# FAQ

## Do I need BIOS files?

For many consoles, yes: PlayStation 1 and 2, Saturn, Dreamcast, Neo Geo and
many home computers. For NES or Super Nintendo cartridges, usually not.
[What is a BIOS?](what-are-bios-files.md)

## How do I know which BIOS I need?

Run the installer with `--dry-run`. It lists what it would install and changes
nothing.

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --dry-run
```

Your setup's page also lists every file, console by console.

## Is the command safe? { #is-piping-the-installer-into-a-shell-safe }

- It checks the installer's fingerprint before running it. If the download
  does not match its expected fingerprint, it stops.
- It checks every file before saving it.
- It writes only in your BIOS folder.

To read the script before you run it:

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh -o install.sh
less install.sh && sh install.sh
```

Details: [How the installer checks itself](installer.md#what-the-bootstrap-does).

## Will it change my other files?

No. It never touches games or saves, or a file it does not know. It replaces a
BIOS file with the wrong contents, and keeps yours.

## Windows or my antivirus blocks the command

Download the ZIP of your setup instead, from [Get your files](../which-pack.md).
It holds the same files.

## Can I run it again?

Yes. That is how you update. It downloads only what is missing or wrong.

## Can I undo it?

Yes. It keeps every file it replaces in a `retrobios-backup` folder, beside
your BIOS folder. The end of the run prints the line that puts them back: the
same command with `--restore` and that folder.

Details: [Files you already had](installer.md#files-you-already-had).

## Fill an SD card from a computer?

Yes. Name your setup and the BIOS folder on the card:

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

On Windows the line is different. Details: [Passing options](installer.md#passing-options).

## Does a BIOS work on every setup?

Yes. Only the folder changes, and sometimes the name.

## Why is a file missing from the ZIP?

Some files come with the emulator when you install it. The ZIP leaves those
out.

## Required or optional?

- **Required:** the emulator does not start games without it.
- **Optional:** games start, with small differences, like no start-up screen.

## My game shows a black screen

Most of the time, a BIOS file is missing or is the wrong version.
[When a game does not start](troubleshooting.md) shows how to check.

## Is this legal?

Laws on copying console firmware differ by country. The question is not
settled, and nothing here is legal advice. Details: [The project's legal position](legal.md).

To ask for a file to be removed, [open an issue](https://github.com/Abdess/retrobios/issues)
and name the file. A file is removed when the owner of its rights asks.

## Why keep recent consoles too?

Files disappear when stores close, like the 3DS and Wii U shops in 2023. We keep
them while they can still be found.

!!! tip "Next"

    - [Get your files](../which-pack.md)
    - [Words used on this site](glossary.md)
