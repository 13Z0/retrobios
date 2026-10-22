---
level: start
---

# Glossary

The first part holds the words anyone meets when setting up BIOS files. The
second part holds the words this project uses in its reference pages.

## Words you will meet

**BIOS, or firmware**
:   A small program built into a console. Many emulators need a copy of it to
    start games. [What BIOS files are](what-are-bios-files.md) says more.

**Emulator**
:   A program that lets a computer, a phone or a handheld act like an older
    console.

**Core**
:   One emulator inside RetroArch. RetroArch loads a different core for each
    console.

**Frontend**
:   The menu you pick and start games from, such as EmulationStation, MinUI or
    the RetroArch menu. It starts the emulator for you.

**Setup, or platform**
:   The software you play with: RetroArch, Batocera, EmuDeck, KNULLI and the
    others. Each one keeps its BIOS files in a folder of its own.

**BIOS folder**
:   The folder where your setup looks for BIOS files. Its place depends on the
    setup, and the page of each setup gives it.

**Pack**
:   One ZIP file with every BIOS file a setup uses. A pack over 2 GB comes in
    several parts, and every part is needed.

**Installer**
:   A small program started by one line pasted into a terminal. It finds your
    BIOS folder and downloads only the files that are missing.

**Terminal, or command line**
:   A window where you type commands. On Windows it is PowerShell, on a Mac it
    is Terminal, on a Steam Deck it is Konsole.

**Hash, or checksum (MD5, SHA1)**
:   A fingerprint of a file's contents. Two files with the same hash hold the
    same bytes. Some setups use it to refuse a file that is not the expected
    version.

**Region**
:   The market a console was sold in, such as Japan, North America or Europe.
    Some emulators pick the BIOS of the same region as the game.

**Required and optional**
:   Required: the emulator does not start games without the file. Optional:
    games start without it, with small differences such as a missing start-up
    screen.

**HLE**
:   High-level emulation: code in the emulator that stands in for the BIOS.
    Many games work with it, some glitch or do not start.

**Game data**
:   Files of a game that a game engine reads, such as the data of Doom or
    Diablo. They come from your own copy of the game, and their absence does not
    affect any console.

## Project terms

**Profile**
:   A YAML file in `emulators/` that documents what one emulator loads, read
    from its source code.

**Ground truth**
:   The emulator's source code, the reference every profile is checked against.

**System**
:   A console or computer being emulated, named by an id such as
    `sony-playstation` or `nintendo-gba`.

**Verification mode**
:   How a setup checks a file: by its name only (existence), by MD5 or by SHA1.
    Reference: [How each platform checks files](verification-modes.md).

**Status**
:   The outcome of one file check: `ok`, `untested` (present, but not the
    expected hash) or `missing`.

**Severity**
:   How much a check result matters: OK, INFO (negligible), WARNING (degraded),
    CRITICAL (the emulator cannot start games).

**Discrepancy**
:   A file that passes the setup's own check but fails the emulator's size or
    hash check.

**hle_fallback**
:   A flag on a profile entry: the emulator has HLE for this file, so its
    absence is reported at INFO.

**Variant**
:   Another version of a BIOS file (a revision, a region or another dump), kept
    in `.variants/` in the repository.

**Cross-reference**
:   The comparison of emulator profiles with a setup's own list, which finds the
    files the setup's emulators load and its list does not name.

**Shared group**
:   A group of files in `_shared.yml` that several setups include, with the
    folder an emulator expects.

**Data directory**
:   A whole folder an emulator needs, such as Dolphin's `Sys/` or the PPSSPP
    assets, kept in `data/` and not indexed in the database.

**Storage tier**
:   Where a file comes from: `embedded` (in `bios/`), `external` (downloaded at
    build time) or `user_provided`.

**Target**
:   A hardware architecture a setup runs on, such as `switch`, `rpi4` or
    `x86_64`.

**Manifest**
:   The JSON list of files per setup in `install/`, which the installer reads.

**Scraper**
:   A script that reads a setup's own BIOS list (System.dat, es_bios.xml and
    others) into this project's format.

**Exporter**
:   A script that writes the project's data back in a setup's own format.

**Truth**
:   A setup's file list generated from the emulator profiles, compared with the
    list the setup publishes.

**Dump catalog**
:   A preservation project (Redump, No-Intro, TOSEC) that publishes lists of
    verified hardware dumps.

**Provenance**
:   The catalogs that list a file, joined to the database by hash. It informs,
    it never overrides the emulator code.

!!! tip "Next"

    - [What BIOS files are](what-are-bios-files.md)
    - [Get your files](../which-pack.md)
