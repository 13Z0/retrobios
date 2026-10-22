---
level: start
---

# FAQ

The questions people ask before installing anything. Questions about how files
are identified and checked are in the
Reference: [technical questions](technical-faq.md).

## Do I need BIOS files?

For many consoles, yes: PlayStation, PlayStation 2, Saturn, Mega-CD, Neo Geo,
Dreamcast and many home computers. For cartridge consoles such as the NES or
the Super Nintendo, usually not. [What BIOS files are](what-are-bios-files.md)
explains why, console by console.

## How do I know which BIOS I need?

Two approaches:

1. **Ask the installer.** `--dry-run` lists what it would install for your platform and writes nothing, `--list-systems` gives the file count per system, and `--emulator` narrows to what one core reads:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --emulator beetle_psx --dry-run
    ```

2. **Check the project site.** Each platform page lists all required and optional BIOS files per system, and each emulator page the files that core loads, with the source references and expected values.

## Is piping the installer into a shell safe?

The one-liner runs a small bootstrap, not the installer. That bootstrap fetches
`install.py` over HTTPS only, refuses anything over 2 MB, and requires its
SHA-256 to equal the value written inside the bootstrap itself. A substituted
installer aborts with "the downloaded installer does not match its expected
fingerprint" before a single line of it runs.

The installer then verifies every file it downloads against the size, SHA-256
and SHA-1 the manifest declares, and moves it into place only once those match.
The manifest is treated as untrusted: a destination cannot be absolute, carry a
drive letter or climb out of the BIOS directory.

Reading the bootstrap before running it is two lines:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh -o install.sh
less install.sh && sh install.sh
```

Details: [All installer options](installer.md), with the full behaviour.

## Will the installer change my other files?

No. It writes only inside the BIOS folder and `retrobios-backup` beside it, and games and saves are never
touched. Inside that folder, a BIOS file whose contents differ from the
expected one is replaced with a checked copy, and your old file is moved to a
`retrobios-backup` folder beside the BIOS folder. Files the installer does not
know are never read, moved or deleted. Copies into the folders of standalone
emulators happen only when you ask for them with `--standalone-copies`.

## What if Windows or an antivirus blocks the command?

The PowerShell execution policy governs script files, and the PowerShell line
writes none, so the policy does not block it. Where a security tool still
blocks it, download the pack of your setup from
[Get your files](../which-pack.md) and extract it into the BIOS folder: it holds
the same files.

## Can I run the installer again?

Yes, and re-running is the normal way to update. A file already present with
the expected hash is left untouched, one whose contents do not match is
replaced by the verified copy (the old one is kept), and files the manifest does not name are never
read, moved or deleted. `--check` does the same inspection and exits without
writing.

## Can I undo it?

Yes. A file you had that the installer replaced is kept in a dated folder
inside `retrobios-backup`, beside the BIOS folder. The end of the run prints
that folder and the command that puts the files back, the same command with
`--restore` and the folder. Copying the contents of the dated folder into the folder that holds
`retrobios-backup` does the same. Files that were not there before can be
deleted from the BIOS folder. The installer changed nothing outside it, apart
from the backup folder.
[More on kept files](installer.md#files-you-already-had).

## Can I install onto an SD card or another machine's drive?

Yes, `--dest` takes any path and the one-liner forwards arguments:

```bash
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh \
  | sh -s -- --platform retroarch --dest /path/to/sdcard/retroarch/cores/system
```

`--dest` is the BIOS folder itself: on a card, `retroarch/cores/system` for
RetroArch on Switch, `RetroArch/system` on Android.

On Windows the arguments need a script block, since `iex` would read them as
its own. Details: [the Windows form](installer.md#passing-options).

## Can I use BIOS from one platform on another?

Yes. BIOS files are console-specific, not platform-specific. A PlayStation BIOS works in RetroArch, Batocera, Recalbox, and any other platform that emulates PlayStation. The only differences between platforms are:

- **Where the file goes** (each platform has its own BIOS directory)
- **What filename is expected** (usually the same, occasionally different)
- **How verification works** (MD5 check vs. existence check)

The packs differ per platform because each platform declares its own set of supported systems and expected files.

## Why is a file the emulator reads not in the pack?

A pack brings what the user has to obtain. A file the emulator installs itself, a
font, a shader, a game database, a free replacement BIOS, is already on the
machine once the emulator is installed, so the pack leaves it out. This is decided
per build and per platform: the PPSSPP standalone release ships its `assets/`
folder, the PPSSPP libretro core does not, so RetroArch packs carry those files and
a standalone PPSSPP pack does not. When a platform's own package installs data
beside a core (Batocera with blueMSX), that platform's pack leaves it out too. The
emulator's page on the site marks these files as installed with the emulator.

## How often are packs updated?

Upstream sources (libretro System.dat, batocera-systems, etc.) are re-scraped by hand and the diff is reviewed before it lands. Releases happen as needed when new BIOS files are added or profiles are updated.

## What's the difference between required and optional?

**Required** means the emulator will not start games for that system without
the file. **Optional** means the emulator works without it, but with reduced
accuracy or missing features (e.g., boot screen animation, wrong font
rendering, or degraded audio).

## What's HLE?

HLE (High-Level Emulation) is a software reimplementation of what the original
BIOS does. Some cores can boot games without a real BIOS file by using their
built-in HLE fallback. The trade-off is lower accuracy: some games may have
glitches or fail to boot entirely. The file is still included in packs because
the real BIOS gives better results.

## What's a hash?

A hash is a fingerprint computed from a file's contents. If even one byte
differs, the hash changes completely. Batocera, Recalbox, EmuDeck and several
other setups compare the MD5 hash of each BIOS file with their list and refuse
a file that does not match. RetroArch only checks that the file is there. The
kinds of hash and where each is used are in the
Reference: [technical questions](technical-faq.md#whats-a-hashchecksum).

## Are these files verified against original hardware dumps?

The hashes here come from what emulator code loads and accepts. Whether a file
matches a catalogued dump of the original chip is a separate question, and the
system pages mark the files that do. The full answer is in the
Reference: [technical questions](technical-faq.md#are-these-files-verified-against-original-hardware-dumps).

## My game shows a black screen

Most often a BIOS file is missing, or is not the version the emulator expects.
[When a game does not start](troubleshooting.md) goes through it step by step,
starting with a check of what is in your BIOS folder.

## Is this legal?

Redistributing firmware is not settled law, and this page does not pretend otherwise. What follows is the reasoning the project acts on, with the strength of each argument stated plainly so anyone can weigh it. None of it is legal advice, and none of it has been tested in court.

### Emulation and BIOS redistribution

- **Writing an emulator is lawful.** *Sony v. Connectix* (2000) and *Sega v. Accolade* (1992) held that copying a work during reverse engineering, to reach the functional elements needed for interoperability, is fair use. Both concern the act of copying while developing, and *Connectix* specifically involved copying Sony's BIOS in the course of that work. Neither decision addresses distributing firmware to third parties, so they support the legality of emulation itself rather than of this collection.
- **Fair use (US, 17 USC 107) is a defence, argued case by case.** The project's position is that non-commercial redistribution for personal backup and archival weighs favourably on purpose and on market effect, since these files are not sold separately. It weighs poorly on the amount used: each file is copied whole. Manufacturers do still monetise some of this firmware indirectly, through re-releases, subscription services and mini consoles, so the market factor is contestable rather than clear.
- **Interoperability in the EU.** The Software Directive (2009/24/EC, Art. 5-6) permits decompilation and use to achieve interoperability, subject to conditions, and Art. 6 restricts passing the resulting information to others. It supports the emulation use case, but it is not a general redistribution permission.
- **Discontinued hardware.** Most firmware here is for hardware no longer sold or supported. This bears on the fair-use market factor and on the practical likelihood of complaint. "Abandonware" is not a legal doctrine in any jurisdiction, and nothing here rests on it.

### Encryption keys (Switch prod.keys, 3DS AES keys, Wii U keys)

This is the most contested area, and the weakest part of the project's position.

- **Keys are unlikely to be copyrightable.** A bare numeric value has no author's creative expression, so copyright is a poor fit. This says nothing about the DMCA, which is the provision that actually applies.
- **DMCA 1201(f) is narrow.** Section 1201(f) allows a person who has lawfully obtained the right to use a copy to circumvent for the sole purpose of achieving interoperability with an independently created program, and 1201(f)(3) allows the means to be made available to others only for that same purpose. Publishing keys openly is not obviously within that allowance, and the trafficking prohibitions in 1201(a)(2) and 1201(b) remain in play. This is the clearest legal risk the project carries.
- **Library of Congress exemptions do not cover this repository.** The triennial exemptions at 37 CFR 201.40 let an eligible library, archive or museum circumvent access controls to preserve a lawfully acquired video game whose server support has ended, with access limited to the institution's premises. The Ninth Triennial Proceeding (2024 cycle) renewed them and declined to extend them to off-premises remote access. A public repository is neither an eligible institution nor an on-premises reading room, so these exemptions show which way preservation policy is moving without authorising what is done here.
- **Keys come from consumer hardware.** They are extracted from retail devices their owners bought. *Chamberlain v. Skylink* (2004) held that a 1201 claim requires some nexus to copyright infringement, in a dispute over a garage-door opener. Later cases have narrowed its reach, and it is a weak foundation for a general post-sale right.
- **Trade secret is not a live issue.** Keys embedded in millions of shipped devices and widely republished are not kept secret by reasonable measures.

### Recent firmware (Switch 19.0.0, PS3UPDAT, PSVUPDAT)

- **Free download is not a licence.** Manufacturers publish these updates on open CDNs without authentication, which makes them easy to obtain but does not grant a right to redistribute them, and PlayStation firmware ships under an end-user licence agreement. The practical argument is that mirroring a file the manufacturer already gives away for free causes no identifiable loss, not that doing so is expressly permitted.
- **Functional necessity.** Emulators cannot run the software these systems were built for without this firmware. That is why the files are collected. It is a statement of purpose, not a legal defence in itself.
- **Yuzu context.** The Yuzu settlement (2024) concerned the emulator and its alleged facilitation of piracy. Yuzu settled without admitting liability, so it set no precedent either way on firmware or key redistribution. It does show that rights holders in this space litigate.

### Summary

This project preserves BIOS files, firmware and keys for personal use, archival and interoperability. The strongest ground is the older, discontinued firmware, where fair use and the absence of a market both point the same way. The weakest is the encryption keys, where DMCA 1201 applies and the interoperability exemption is narrower than the use made of it here. The project accepts that risk deliberately, in the belief that these files are worth preserving while they can still be obtained.

This reflects the project's good-faith understanding, not legal advice.

### Asking for a file to be removed

A rights holder, or anyone acting for one, can [open an issue](https://github.com/Abdess/retrobios/issues) identifying the file by path or hash. A file whose removal is requested by its rights holder is removed from the repository and from the next release. No formal notice is required and none will be demanded. State the file and the basis of the claim. The same channel is open to anyone else with a concern about a specific file.

## Isn't the Switch or PS3 too recent for a retro project?

Age is not what decides inclusion here: availability is. Firmware distribution depends on manufacturer servers, and those disappear: Nintendo closed the 3DS and Wii U eShops in March 2023, Sony shut the PSP store in 2021. Once a CDN goes offline, the files only survive where someone archived them.

The project tries to archive files while they are still available rather than after they have become rare. "Retro" is a moving window: the PlayStation 2 was current hardware when its BIOS was first preserved, and today's consoles will be tomorrow's retro systems. A file is included for the same reason at any age: an emulator needs it, and nothing guarantees it stays available.

!!! tip "Next"

    - [Get your files](../which-pack.md)
    - [When a game does not start](troubleshooting.md)
    - [Glossary](glossary.md)
