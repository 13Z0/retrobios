---
level: start
order: 50
---

# Common questions

## Do I need BIOS files?

- **Yes** for PlayStation 1 and 2, Saturn and Neo Geo.
- **Better with it** for Dreamcast: some games need it.
- **Usually not** for NES or Super Nintendo.

[What is a BIOS?](what-are-bios-files.md)

## How do I know which BIOS I need?

Add `--dry-run`. It lists the files, changing nothing.

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --dry-run
```

## Do I need every file of a console?

No. To keep only your region's BIOS, add it, like `--region us`.

## Does the BIOS set the game's language?

Rarely. It comes from the game. Some games follow the console's setting, like Flycast's **Language**
option.

## Zip or unzip? { #zip-or-unzip }

Unzip it into the folder your setup's page names. Leave
the ZIPs inside zipped, like `neogeo.zip`.

## Does a BIOS work on every setup?

Usually. The command installs each setup's own version, like its `neogeo.zip`.

## My arcade game does not start

The game set must match the emulator's version, and `neogeo.zip` stays zipped.
Reference: [Which version each emulator reads](technical-faq.md#which-mame-version-do-the-arcade-bios-sets-match).

## Keys, firmware or BIOS?

- **BIOS** or **firmware**: the console's own program, like `scph5501.bin`.
- Newer consoles need a system update, like `PS3UPDAT.PUP`, installed from the
  emulator's menu.
- **Keys** unlock encrypted games, like `prod.keys` for Switch.

## Can I play without the BIOS?

Sometimes, with an emulator's stand-in, but some games fail.

## Is the command safe? { #is-piping-the-installer-into-a-shell-safe }

- It stops if the installer does not match its expected fingerprint, and checks
  each file.
- It writes in your BIOS folder, and for some emulators your save folder.
- It never touches a game or a save.
- It keeps each file it replaces in `retrobios-backup`. `--restore` puts it
  back.

Read them first: [install.sh](https://github.com/Abdess/retrobios/blob/main/install.sh),
[install.ps1](https://github.com/Abdess/retrobios/blob/main/install.ps1),
[install.py](https://github.com/Abdess/retrobios/blob/main/install.py).

## Windows or my antivirus blocks the command

Use your setup's ZIP, if it has one: [Get your files](../which-pack.md).

## Why is there no ZIP for my setup?

ZIPs are made for the setups most people download. The command installs every
setup's files and checks each one. `--dry-run` shows the list first.

## Fill an SD card from a computer?

Yes. Name your setup and the card's BIOS folder. For RetroArch on a Switch
card in drive `E:`:

<!-- os-tabs windows: --platform retroarch --dest E:\retroarch\cores\system -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

## No internet on the device?

Fill its folder from a computer, as above, or unzip your ZIP and check it with
`--from-pack`: [Without a network](installer.md#without-a-network-offline-install).

## My setup is not in the list

- Name your emulator's BIOS folder with `--dest`.
- ES-DE, LaunchBox: the command fills the folders of the emulators they start.
- iPhone, iPad: no terminal. Use the ZIP.

## Why is a file missing from the ZIP?

Some come with the emulator, so the ZIP skips them.

## My game shows a black screen

Often a missing BIOS: [When a game does not start](troubleshooting.md).

## Is this legal?

Laws differ by country. [Is it legal?](legal.md)

## Next steps

- [Get your files](../which-pack.md)
- [Words used on this site](glossary.md)
