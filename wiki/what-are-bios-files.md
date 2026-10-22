---
level: start
order: 10
nav_title: What BIOS files are
---

# What is a BIOS?

A console has a small program inside it. It starts first, then starts the game.
This program is the **BIOS**, also called **firmware**.

<a class="rb-button rb-button-primary" href="../which-pack.md">Get your files</a>

An **emulator** is an app that acts like a console. For many consoles, it needs
a copy of the BIOS. That copy is a file: a **BIOS file**.

<!-- image: bios-chain -->

## How it works

1. You pick a game in your game menu.
2. The menu starts the emulator.
3. The emulator opens the BIOS file in your **BIOS folder**.
4. The game starts.

If step 3 fails, you see "BIOS missing" or a black screen.

## Do I need one?

- **Yes:** PlayStation 1 and 2, Saturn, Dreamcast, Neo Geo, PC Engine CD and
  many home computers.
- **Usually not:** NES, Super Nintendo, Mega Drive, Game Boy and Game Boy
  Advance.
- **Add-ons need their own:** the Famicom Disk System and the Mega-CD.

Details: [Every console, one by one](../systems/index.md)

## The name must match

An emulator looks for one exact name. A PlayStation emulator looks for
`scph5501.bin`. A file with another name is not found.

## The version must match

Two files can have the same name and different contents. One may be for Japan,
another for Europe.

Some setups check each file's **hash**, a fingerprint of its contents.
Batocera, Recalbox and EmuDeck report a version they do not expect.

Our files are the versions each setup expects.

## Can a game start without it?

Sometimes. Some emulators carry their own code in place of the BIOS. Some games
then glitch or do not start. The real file works best, so we include it.

## Is it legal?

Laws differ by country. [Read the project's position](faq.md#is-this-legal).

:::tip[Next]
- [Get your files](../which-pack.md)
- [Words used on this site](glossary.md)
:::

