---
level: start
order: 50
---

# Common questions

## Do I need BIOS files?

- **Yes** for PlayStation 1 and 2, Saturn and Neo Geo.
- **Better with it** for Dreamcast: some games need it.
- **Usually not** for NES or Super Nintendo cartridges.

[What is a BIOS?](what-are-bios-files.md)

## How do I know which BIOS I need?

Run the command with `--dry-run`. It lists the files and changes nothing.

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --dry-run
```

## Do I need every file of a console?

No. A console often has one BIOS per region. To keep only yours, add your
region, like `--region us`.

## Does the BIOS set the game's language?

Rarely. It comes from the game. Some games follow the console's setting, like
Flycast's **Language** option.

## Zip or unzip? { #zip-or-unzip }

Unzip the ZIP you downloaded into the folder your setup's page names. Leave
the ZIPs inside as they are, like `neogeo.zip`.

## Does a BIOS work on every setup?

Usually. The command installs the version each setup expects, like its
`neogeo.zip`.

## My arcade game does not start

The game set must match the emulator's version, and `neogeo.zip` stays zipped.
Reference: [Which version each emulator reads](technical-faq.md#which-mame-version-do-the-arcade-bios-sets-match).

## Keys, firmware or BIOS?

- **BIOS** or **firmware**: the console's own program, like `scph5501.bin`.
- Some newer consoles need a system update, like `PS3UPDAT.PUP`, installed from the
  emulator's menu.
- **Keys** unlock encrypted games, like `prod.keys` for Switch.

## Can I play without the BIOS?

Sometimes. Some emulators carry a stand-in, and some games then fail.

## Is the command safe? { #is-piping-the-installer-into-a-shell-safe }

- It checks the installer's fingerprint, then each file before saving it.
- It writes in your BIOS folder, and for some emulators in your save folder.
- It never changes or deletes a game or a save.
- It keeps any file it replaces in `retrobios-backup`, next to your BIOS
  folder. `--restore` puts it back.

Read them first: [install.sh](https://github.com/Abdess/retrobios/blob/main/install.sh),
[install.ps1](https://github.com/Abdess/retrobios/blob/main/install.ps1),
[install.py](https://github.com/Abdess/retrobios/blob/main/install.py).
[How the installer checks itself](installer.md#what-the-bootstrap-does).

## Windows or my antivirus blocks the command

Download your setup's ZIP from [Get your files](../which-pack.md) instead.

## Fill an SD card from a computer?

Yes. Name your setup and the BIOS folder on the card. For RetroArch on a Switch
card in drive `E:`:

<!-- os-tabs windows: --platform retroarch --dest E:\retroarch\cores\system -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

## No internet on the device?

Unzip your setup's ZIP into the folder its page names. To check each file, use
`--from-pack`: [Without a network](installer.md#without-a-network-offline-install).

## My setup is not in the list

- Name your emulator's BIOS folder with `--dest`.
- ES-DE, LaunchBox: the command fills the folders of the emulators they start.
- iPhone, iPad: no terminal. Use the ZIP.

## Why is a file missing from the ZIP?

Some come with the emulator, so the ZIP leaves them out.

## My game shows a black screen

A BIOS file is often missing: [When a game does not start](troubleshooting.md).

## Is this legal?

Laws differ by country. [Is it legal?](legal.md)

## Next steps

- [Get your files](../which-pack.md)
- [Words used on this site](glossary.md)
