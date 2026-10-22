---
level: reference
---

# Technical questions

Questions about how the files are identified and checked. The questions asked
before installing are in the [FAQ](faq.md).

## How is a missing file ranked?

In verification output, missing required files appear as CRITICAL or WARNING
depending on the platform. Missing optional files appear as WARNING or INFO.

When a core has HLE support, the verification tool lowers the severity of a
missing BIOS to INFO. The file is still included in packs because the real BIOS
gives better results. The full table is the
[severity matrix](verification-modes.md#severity-matrix).

## Why are there multiple hashes for the same file?

Two main reasons:

1. **Regional variants.** The same filename (e.g., `IPL.bin` for GameCube) exists in different versions for USA, Europe, and Japan. Each region has a different hash.
2. **Revision differences.** Console manufacturers released updated BIOS versions over time. A PlayStation SCPH-5501 BIOS differs from a SCPH-7001.

Platforms that verify by MD5 accept specific hashes. If yours doesn't match any known hash, it may be a bad dump or an uncommon revision.

A filename is never allowed to stand in for a declared hash. When a file entry
carries a hash, resolution matches on content and reports `hash_mismatch` if the
only same-named copy disagrees. What happens next depends on the platform: an
MD5 or SHA1 platform would reject the file, so the pack omits it and says why,
while an existence platform only checks presence, so the file ships and the divergence
is reported instead.

## Why are there files that aren't BIOS?

A file earns its place when an emulator loads it from disk and does not bundle it. That covers actual BIOS ROMs, console firmware, arcade BIOS sets, and also game data like `prboom.wad` for the PrBoom core or soundfonts for EasyRPG.

This follows the platforms themselves: Batocera lists `prboom.wad` in its BIOS checker, and libretro declares these files in the `firmware` field of its core info. The word "BIOS" is their vocabulary for "external file the emulator needs". Files are tagged by category (`bios`, `game_data`, `bios_zip`) in the emulator profiles, so the distinction stays visible.

## Are these files verified against original hardware dumps?

No, and that is a deliberate boundary. The hashes here document what
emulator code loads and accepts, read from the source. When several
revisions or regions pass, all of them are kept (`.variants/` in the
repository, per-emulator pages list each one), and the pack ships the
variant satisfying the most checks, logging any conflict it cannot
resolve.

Whether a file byte-matches the original hardware ROM is a different
question, answered by dump-preservation catalogs such as No-Intro,
Redump, and TOSEC, not by emulator code. The collection is
cross-referenced against their DATs: files matching a catalog entry
carry a verified dump badge on the system pages, and catalog entries
absent from the collection are tracked as acquisition targets. When
the two views differ, this project follows the code, because that is
what decides whether your emulator boots.

The [dump provenance](../provenance.md) page has the current coverage
per catalog and the full list of catalogued dumps still missing.

## Which MAME version do the arcade BIOS sets match?

Arcade BIOS sets are coupled to the romset version, so there is one
profile per MAME core generation: MAME 2000 (0.37b5), MAME 2003 (0.78),
MAME 2003-Plus, MAME 2009 (0.135u4), MAME 2010 (0.139), MAME 2015
(0.160), MAME 2016 (0.174), and current MAME (0.289), each read from that
version's own source tree, because the BIOS root sets differ between
versions. Derivatives and ports carry their own profiles on top of those
generations: `mamearcade`, `mamemess`, `mame2003_midway`, the three
`mame4droid` drops, `advancemame`, `groovymame` and `hbmame`. Recalbox
compiles two MAME versions of its own, 0.258 and 0.278, profiled as
`mame0258` and `mame0278`. The pack
ships the sets matching the core you run, and a generic unversioned arcade
pack cannot do that.

## What's a hash/checksum?

A hash is a fixed-length fingerprint computed from a file's contents. If even one byte differs, the hash changes completely. Every file in the database carries these:

| Type | Length | Example | Used for |
|------|--------|---------|----------|
| SHA1 | 40 hex chars | `10155d8d6e6e832d8ea1571511e40dfb15fede05` | database primary key, BizHawk, installer downloads |
| MD5 | 32 hex chars | `924e392ed05558ffdb115408c263dccf` | most platform verification |
| SHA256 | 64 hex chars | `9a1c...` | emulator profiles whose upstream publishes SHA256 |
| CRC32 | 8 hex chars | `2F468B96` | ROM-set matching, arcade DATs |

Emulator profiles add Adler-32 where the code checks it, which is how Dolphin
checks its DSP ROMs (`dsp_rom.bin` and `dsp_coef.bin`, hashed byte-swapped).
`IPL.bin` is not one of them: Dolphin loads it without a hash check.

Verification uses whichever one the platform itself uses: MD5 for Batocera,
RetroBat, Recalbox, EmuDeck, RetroDECK, RomM, ROCKNIX, KNULLI, AmberELEC, EmuELEC
and MiSTer FPGA, SHA1 for
BizHawk, and nothing at all for RetroArch, Lakka and RetroPie, which only check
that the file exists.

## Why does my verification report say UNTESTED?

`untested` means the file exists on disk but its hash does not match the expected value. This happens on MD5 and SHA1 platforms (Batocera, Recalbox, BizHawk, ROCKNIX, MiSTer FPGA, and the rest) when the file is present but contains different data than what the platform declares.

On existence-mode platforms (RetroArch, Lakka, RetroPie, MinUI, NextUI, MyMinUI), files are never `untested` because the platform only checks presence, not content. Those files show as `ok` if present, whatever they contain.

Running `verify.py --emulator <core> --verbose` shows the emulator-level ground truth, which can confirm whether the file's hash matches what the source code expects. The platform report applies the same check on its own: `verify.py --platform retroarch` prints a `DISCREPANCY` line for every file the platform accepts and a profiled emulator rejects.

It reaches only as far as the profiles do. Files no profile states a value for are platform-only, and nothing can be said about their content. The footer of the platform report gives that ratio as `Ground truth: N/M files have emulator validation`.

!!! tip "Next"

    - [Reference index](index.md), every reference page in one list
    - For contributors: [How to contribute](../contributing.md)
