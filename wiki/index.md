---
level: reference
order: 0
---

# Reference

These pages describe how the files are checked, what the data holds and how the
toolchain works. To set up BIOS files on a device, start with
[Get your files](../which-pack.md).

## How the files are checked

- **[How each platform checks files](verification-modes.md)** - existence, MD5 and SHA1 modes, severity matrix, resolution chain, reading `verify.py` output
- **[What the packs do not hold](../gaps.md)** - files emulators load that no pack can carry yet, by platform and by emulator
- **[Cores by platform](../cross-reference.md)** - every core each platform runs, the systems it emulates, the upstream project it comes from
- **[Dump provenance](../provenance.md)** - how the collection lines up against the No-Intro, Redump and TOSEC catalogs
- **[Updated platform files](../exports.md)** - each platform's own file, with the changes the emulator source supports
- **[Technical questions](technical-faq.md)** - multiple hashes, MAME versions, UNTESTED, dump verification

## Data

- **[Data and API](../data.md)** - JSON, CSV and SQLite exports with their checksums
- **[Find a file](../files/index.md)** - any SHA1, MD5, CRC32 or SHA256: the file, the platforms that declare it, the emulators that check it
- **[Data model](data-model.md)** - database.json structure, indexes, file resolution order, YAML formats

## The toolchain

- **[Architecture](architecture.md)** - directory structure, data flow, platform inheritance, pack grouping, security, edge cases, CI workflows
- **[Build your own packs](advanced-usage.md)** - custom packs, region filtering, one file per slot, target filtering, truth generation, emulator verification, offline workflow
- **[Command-line tools](tools.md)** - every script, pipeline usage, scrapers
- **[Projects using this data](community-tools.md)** - projects built on RetroBIOS data

## Methodology

Platform lists are checked against emulator source code, file by file where a
profile exists. Documentation and metadata can differ from what a program does
at run time, so the source is the primary reference:

1. **Upstream emulator source**: what the original project loads (Dolphin,
   PCSX2, Mednafen and others)
2. **Libretro core source**: the RetroArch port, which may adapt paths or add
   files
3. **`.info` declarations**: metadata that platforms rely on, checked against
   the two above

## For contributors

- **[Profiling an emulator](profiling.md)** - create an emulator profile from source code, YAML field reference
- **[Adding a platform](adding-a-platform.md)** - scraper, registry, YAML config, exporter, target scraper, install detection
- **[Adding a scraper](adding-a-scraper.md)** - plugin architecture, BaseScraper, parsers, target scrapers
- **[Testing](testing-guide.md)** - run tests, fixture pattern, how to add tests, CI integration
- **[Release process](release-process.md)** - CI workflows, large files, manual release

See [contributing](../contributing.md) for submission guidelines.

## Terms

The words these pages use, from profile to provenance, are in
[Terms of the reference pages](reference-terms.md). The everyday words are in
the [glossary](glossary.md).
