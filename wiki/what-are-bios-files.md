---
level: start
---

# What BIOS files are

Many consoles have a small program built into them. It runs when the console
is switched on, sets up the hardware and then starts the game. That program is
called the BIOS, or the firmware.

An emulator recreates a console in software. To start the games of many
consoles, it needs a copy of that built-in program. The copy is a file, and
that file is what people call a BIOS file.

<!-- image: bios-chain -->

## How the pieces fit together

1. You pick a game in the **frontend**, the menu you play from (EmulationStation,
   the RetroArch menu, MinUI and others).
2. The frontend starts an **emulator** for that console.
3. The emulator reads the **BIOS file** from the **BIOS folder** of your setup.
4. The game starts.

When the file is not in the folder, or is not the version the emulator expects,
step 3 fails. The emulator then says that a BIOS is missing, or the game stays
on a black screen.

## Do I need them?

It depends on the console.

- **Usually yes** for consoles that start every game through their own
  program: PlayStation, PlayStation 2, Saturn, Mega-CD, PC Engine CD, Neo Geo,
  Dreamcast, and many home computers.
- **Usually not** for cartridge consoles such as the NES, the Super Nintendo,
  the Mega Drive, the Game Boy and the Game Boy Advance. Their emulators start
  the games without one. Some can use one to show the original start-up screen.
- **Add-ons need their own**, even on those consoles: the Famicom Disk System
  and the Mega-CD each have a BIOS.

Some emulators can stand in for the BIOS with code of their own. This is called
HLE, for high-level emulation. It works for many games, but some glitch or do
not start. The real file gives the best results, so the packs include it.

Details: [the files each console needs](../systems/index.md), with the
emulators that read them.

## Where they go

Each setup keeps its BIOS files in a folder of its own: `system` for RetroArch,
`/userdata/bios` on Batocera, `Emulation/bios` with EmuDeck. The page of your
setup gives the exact folder.

The name matters. An emulator looks for an exact name, such as `scph5501.bin`
for a PlayStation, and a file with another name is not found.

## Why the right version matters

Two files with the same name can hold different versions: one per region, or
a newer revision of the same chip. Some setups, among them Batocera, Recalbox
and EmuDeck, compare a fingerprint of the file's contents, called a hash, and
refuse a version they do not list. RetroBIOS gives each setup the versions it
expects.

## Is it legal?

Laws on copying console firmware differ by country, and the question is not
settled. The [project's position](faq.md#is-this-legal) explains where it
stands, where its arguments are weakest, and how to ask for a file to be
removed.

!!! tip "Next"

    - [Get your files](../which-pack.md), for the setup you play on
    - [Glossary](glossary.md), every word these pages use
