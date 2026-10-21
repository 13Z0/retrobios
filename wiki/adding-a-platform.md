# Adding a platform

How to add support for a new retrogaming platform (e.g. a frontend like Batocera,
a manager like EmuDeck, or a firmware database like BizHawk).

## Prerequisites

Before starting, gather the following from the upstream project:

- **Where does it define BIOS requirements?** Each platform has a canonical source:
  a DAT file, a JSON fixture, an XML manifest, a Bash script, a C# database, etc.
- **What verification mode does it use?** Read the platform source code to determine
  how it checks BIOS files at runtime: file existence only (`existence`), MD5 hash
  matching (`md5`), SHA1 matching (`sha1`), or a combination of size and hash.
- **What is the base destination?** The directory name where BIOS files are placed
  on disk (e.g. `system` for RetroArch, `bios` for Batocera, `Firmware` for BizHawk).
- **What hash type does it store?** The primary hash format used in the platform's
  own data files (SHA1 for RetroArch/BizHawk, MD5 for Batocera/Recalbox/EmuDeck).

## Step 1: Create the scraper

Scrapers live in `scripts/scraper/` and are auto-discovered by the plugin system.
Any file matching `*_scraper.py` in that directory is loaded at import time via
`pkgutil.iter_modules`. No registration step is needed beyond placing the file.

### Module contract

The module must export two names:

```python
PLATFORM_NAME = "myplatform"  # matches the key in _registry.yml

class Scraper(BaseScraper):
    ...
```

### Inheriting BaseScraper

`BaseScraper` provides:

- `_fetch_raw() -> str` - HTTP GET with 50 MB response limit, cached after first call.
  Uses `urllib.request` with a `retrobios-scraper/1.0` user-agent and 30s timeout.
- `compare_with_config(config) -> ChangeSet` - diffs scraped requirements against
  an existing platform YAML, returning added/removed/modified entries.
- `test_connection() -> bool` - checks if the source URL is reachable.

Two abstract methods must be implemented:

```python
def fetch_requirements(self) -> list[BiosRequirement]:
    """Parse the upstream source and return one BiosRequirement per file."""

def validate_format(self, raw_data: str) -> bool:
    """Return False if the upstream format has changed unexpectedly."""
```

### BiosRequirement fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Filename as the platform expects it |
| `system` | `str` | Retrobios system ID (e.g. `sony-playstation`) |
| `sha1` | `str \| None` | SHA1 hash if available |
| `md5` | `str \| None` | MD5 hash if available |
| `crc32` | `str \| None` | CRC32 if available |
| `size` | `int \| None` | Expected file size in bytes |
| `destination` | `str` | Relative path within the BIOS directory |
| `required` | `bool` | Whether the platform considers this file mandatory |
| `zipped_file` | `str \| None` | If set, the hash refers to a ROM inside a ZIP |
| `native_id` | `str \| None` | The platform's own system id, written as `native_system` |
| `sha256` | `str \| None` | SHA256 hash if available |
| `alt_md5` | `str \| None` | A second accepted MD5 (Batocera and ROCKNIX `altmd5`) |
| `native_path` | `str \| None` | The file reference as the platform writes it, kept when it differs from `name`. libretro writes `iplromco.dat` bare although the file lives under `keropi/`, and `ep128emu/roms/cpc464.rom` with its directory |
| `mirror_of` | `str \| None` | The native system whose entry this one repeats under another system. The platform's own file does not carry the copy, so the export skips it |
| `native` | `dict` | Fields the platform declares that the model has no place for, written back unchanged by the export: `native_name`, `core`, `note`, `description`, `hash_match_mandatory`, `mandatory_declared`, `alt_paths`, `component`, `required_label` |

`requirement_entry()` in `base_scraper.py` is the single serializer from a
requirement to a platform YAML entry, and every scraper goes through it.
Anything an export writes back is recorded by the scraper, never rebuilt by the
exporter: several native systems can fall onto one slug (Recalbox files
`pcengine`, `pcenginecd` and `supergrafx` under `nec-pc-engine`), and a path
or a label the YAML does not hold cannot be derived. When an export needs a
value the YAML lacks, the scraper and `schemas/platform.schema.json` gain it,
then the platform is scraped again.

### System ID mapping

Every scraper needs a mapping from the platform's native system identifiers to
retrobios system IDs. Define this as a module-level dict:

```python
SLUG_MAP: dict[str, str] = {
    "psx": "sony-playstation",
    "saturn": "sega-saturn",
    ...
}
```

Warn on unmapped slugs so new systems are surfaced during scraping.

### generate_platform_yaml (optional)

If the scraper defines a `generate_platform_yaml() -> dict` method, the shared
CLI will use it instead of the generic YAML builder. This allows the scraper to
include platform metadata (homepage, version, inherits, cores list) in the output.

### CLI entry point

Add a `main()` function and `__main__` guard:

```python
def main():
    from scripts.scraper.base_scraper import scraper_cli
    scraper_cli(Scraper, "Scrape MyPlatform BIOS requirements")

if __name__ == "__main__":
    main()
```

`scraper_cli` provides `--dry-run`, `--json`, and `--output` flags automatically.

### Test the scraper

```bash
python -m scripts.scraper.myplatform_scraper --dry-run
```

This fetches from upstream and prints a summary without writing anything.

## Step 2: Register the platform

Add an entry to `platforms/_registry.yml` under the `platforms:` key.

The registry describes *where the data comes from*, and the platform YAML
holds what the upstream file declares, `verification_mode` and
`base_destination` included. A re-scrape rewrites the YAML without touching the
registry. `load_platform_config` copies every registry key the YAML does not
carry into the config that `verify.py` and `generate_pack.py` read, so a runtime
rule that no upstream file states (`zip_digest`, `firmware_folder_flat`) is
written in the registry, where the next scrape leaves it in place.

### Required fields

```yaml
platforms:
  myplatform:
    config: myplatform.yml           # platform YAML filename in platforms/
    status: active                   # active or archived
    scraper: myplatform              # matches PLATFORM_NAME, null if hand-maintained
    schedule: weekly                 # re-scrape cadence, run by hand: weekly, monthly, or null
    source_url: https://...          # upstream data URL
    source_format: json              # json, xml, clrmamepro_dat, python_dict,
                                     # bash_script+csv, csharp_firmware_database,
                                     # github_component_manifests, mister_downloader_db
    hash_type: md5                   # primary hash in the upstream data
    cores:                           # which emulator profiles apply
    - core_a
    - core_b
```

A platform without a scraper (`scraper: null`, `schedule: null`) keeps its YAML
as a hand-maintained file. Every registered platform has a scraper today, so
none is edited by hand: RetroPie inherits RetroArch's file set and its scraper
writes the packages it builds, read from the `rp_module_id` of each
scriptmodule, and any manual change is overwritten on the next scrape.

The `cores` field determines which emulator profiles are resolved for this
platform (`resolve_platform_cores` in `common.py`). Alias profiles are never
resolved. Three strategies exist:

- **Explicit list**: `cores: [beetle_psx, dolphin, ...]` - match by profile key name
  or by any name in a profile's `cores:`. When a profile's `built_by` names this
  platform, that profile answers for the name. Used by Batocera, Recalbox, RetroBat,
  RomM, RetroPie, EmuDeck, RetroDECK, ROCKNIX. A list that names `retroarch` (the last
  four) also takes every libretro profile, as `all_libretro` does. An empty list
  (BizHawk, MiSTer FPGA) resolves no profile, and the pack holds the platform's own
  files.
- **all_libretro**: `cores: all_libretro` - every profile whose `type` contains
  `libretro`, except those that carry `built_by`: a build a platform compiles itself
  is absent from the buildbot and is reached only by its name (Recalbox's `mame0258`
  and `mame0278`). Used by RetroArch and Lakka.
- **Omitted**: fallback to system ID intersection, `built_by` profiles excluded.

Two lists say how each resolved emulator is laid out. `standalone_cores` names the
ones the platform launches as standalone apps: their files go to `standalone_path`,
under the folder the platform hands the emulator when its `launchers:` say so. A
`links` or `passes_path` record of a whole directory that names one of the profile's
`standalone_directories` gives that folder (`launchers.standalone_root`): RetroPie
links Flycast's `~/.local/share/flycast` to `BIOS/dc`, EmuDeck the Flatpak's data
directory to `bios/flycast/bios`, and ROCKNIX's patched data directory is followed
through the link its start script makes. Two records giving two folders leave the
profile's layout.
`dual_mode_cores` names those it launches both ways (RetroPie packages `lr-hatari`
and `hatari`): both layouts are packed. A profile without a standalone build keeps
its libretro layout whatever the platform calls it.

### Optional fields

```yaml
    logo: https://...                # SVG or PNG for UI/docs
    inherits_from: retroarch         # inherit systems/cores from another platform
    case_insensitive_fs: true        # if the platform runs on case-insensitive filesystems
    extract_to: /userdata/bios/      # folder the pack extracts into, shown in the README,
                                     # the site and the release notes
    zip_digest: composite            # the frontend hashes a .zip by its members only
    firmware_folder_flat: true       # the frontend lists each system folder without
                                     # descending into it
    source_wiki: https://...         # human-readable upstream reference
    target_scraper: myplatform_targets  # hardware target scraper name, null if static
    target_source: https://...       # target data source URL
    contributed_by:                  # credited in README, site, and packs
    - username: someone
      contribution: platform support
      pr: 50
    install:
      detect:                        # auto-detection for install.py
      - os: linux
        method: config_file
        config: $HOME/.config/myplatform/config.ini
        parse_key: bios_directory
```

`zip_digest: composite` is set where the frontend's own check takes
`Zip::Md5Composite` for every `.zip` and never the container's MD5 (Recalbox,
`Bios.cpp:64-68`). Each `.zip` entry is then checked against its composite alone,
by the resolver and by the pack check, as
[verification modes](verification-modes.md#md5_composite-recalbox-zip-verification)
describes.

`firmware_folder_flat: true` is set where the frontend reads a system's firmware
folder without descending into it (RomM 5.4.0,
`backend/handler/filesystem/base_handler.py:1002`, `recursive=False`). On such a platform, a core file whose destination has a
subfolder is left out of the pack (`nested` in `scripts/packextras.py`), since no
core would receive it.

### Platform YAML header

The runtime behavior lives at the top of `platforms/myplatform.yml`:

```yaml
platform: MyPlatform
verification_mode: md5   # existence, md5, or sha1
hash_type: md5           # primary hash stored in the file entries
base_destination: bios   # root directory the pack extracts into
inherits: retroarch      # optional, merges the parent's systems
systems:
  sony-playstation:
    files: [...]
```

`base_destination` is what the pack prefixes every entry with. Leave it empty
when the upstream destinations already carry their own root, as RetroDECK does
with `bios/` and `roms/`.

### Launchers

Between the file a user drops in the BIOS folder and the emulator, a
platform's own launch code often decides which name is read and where it is
handed over. Batocera's DuckStation generator tries 22 names per region, first
found wins, and writes the hit into `settings.ini`. That behaviour belongs to
the platform, not to the emulator, so the scraper records it from the
platform's code under `launchers:`, never as an alias in an emulator profile.

```yaml
launchers:
- emulator: duckstation        # as the platform names it
  build: standalone            # standalone or libretro
  kind: names_searched
  slot: NTSCU                  # the key the code files this list under
  names: [scph101.bin, scph1001.bin, scph5501.bin, scph7001.bin, scph7501.bin]
  case_insensitive: true
  search_dir: /userdata/bios
  passes_to: "[BIOS] PathNTSCU"
  fails_when_missing: No PSX1 BIOS found
  fails_scope: emulator        # raised only when every slot found nothing
  source_ref: "package/.../duckstationGenerator.py:115-136, 501-523"
```

| Field | Meaning |
|-------|---------|
| `emulator` | Emulator or core as the platform names it |
| `build` | `standalone` or `libretro` |
| `kind` | `names_searched` (tried in order, first found wins), `fixed_name` (one path per name), `core_option` (an option value names the file), `passes_path`, `links`, `copies`, `moves` (a file left in one folder is moved to the one the launcher links from), `scans_dir`, `package` (no file: what a package builds and registers) |
| `package` | The platform package whose script the record comes from (RetroPie's `rp_module_id`) |
| `repo` | What the package fetches and builds, as the script writes it (`rp_module_repo`: method, URL, branch or commit) |
| `systems` | Systems the package registers, under the platform's names (`addSystem`, `addPort` for ports) |
| `slot` | The key the code files the list under: a region, a machine |
| `names` | In the order the code tries or writes them, relative to `search_dir` |
| `expands` | Values a `{X}` placeholder in `names` takes, in the order tried (Hatari tries each TOS in ten languages) |
| `case_insensitive` | Whether the launcher matches names regardless of case |
| `search_dir` | Directory the launcher reads, as the code resolves it |
| `passes_to` | Config key, command-line flag or core option that receives the file |
| `fails_when_missing` | `false`, or the message the launcher raises |
| `fails_scope` | `emulator` when the failure needs every record of the emulator to come up empty |
| `depends_on_option` | Options or conditions that decide whether the record applies |
| `device` | Devices whose shipped config the record reads, set only where a package ships configs that differ between devices |
| `source_ref` | `path:lines` at the revision `source` pins |

Batocera's generators are read with `ast`, RetroBat's C# generators as text.
ROCKNIX and EmuDeck launch from shell scripts and configs shipped with each
package, so `scripts/scraper/shell_launchers.py` reads assignments, `for`
loops, `ln -s` commands and `key = value` lines. A ROCKNIX package can ship
one config per device: the configs are grouped by the value read, and
`device` appears only when there is more than one group. melonDS writes its
seven BIOS paths on every device but AMD64, where the keys are left empty:

```yaml
- emulator: melonds
  build: standalone
  kind: fixed_name
  names: []
  passes_to: melonDS.ini BIOS9Path, BIOS7Path, FirmwarePath, DSiBIOS9Path,
    DSiBIOS7Path, DSiFirmwarePath, DSiNANDPath left empty
  device: AMD64
```

An extractor that no longer recognises the shape of the code raises: an
empty list would read as a launcher that looks for nothing.

A libretro frontend that picks the system directory per core records each
choice as a `passes_path` entry with no `names`. Recalbox gives gpsp
`bios/gba` and geargrafx `bios/pcenginecd`, so a core that opens `gba_bios.bin`
reads it from there. `emulator: libretro` stands for every core Recalbox runs,
for the default directory and for the per-system overrides applied after the
per-core choice, and `<system>` in `search_dir` is the system's own name.

Shell platforms are read as text. RetroPie's scriptmodules are walked the way
RetroPie runs them, one shell for every package: local assignments, arrays,
`for` loops, brace expansion, `$md_id` and calls into another package's
functions (`dosbox-sdl2` runs `configure_dosbox`). Paths keep the variables
the scripts write, `$biosdir` being `~/RetroPie/BIOS` (`retropie_packages.sh:48-49`).
Each package also gets one `kind: package` record, and that record is what
maps a package to the profile of the build it compiles: `repo` names the
project (`minivmac` builds github.com/minivmac/minivmac, Mini vMac 37, not the
libretro port), so the mapping never rests on stripping `lr-` from a name.
RetroDECK's records come from each component's `component_prepare.sh`,
`component_launcher.sh` and `component_functions.sh`, and from the presets
they copy, with `$bios_path` as the framework names it. In both, every line
that names the BIOS directory must match a known construct, else the scrape
stops with `LauncherPatternMissing`.

`verify.py` and the pack builder read the records through one function,
`LauncherLayer.settle()` in `scripts/launchers.py`, applied inside the
cross-reference both of them consume. A `names_searched` list is one place:
the names are tried in order and one held file fills it, so a core file the
launcher would never open once another name of the list is there is not
reported missing. A `fixed_name` or `core_option` name is a place of its own.
Only a record that applies without condition settles the core's own name: a
core option the user may never set, or a record tied to one system, leaves
the core's default in use elsewhere.
A place that no declared file and no packed core file fills receives, under
the launcher's name, a file the core accepts: the file the collection knows
under that name when the core takes its content, otherwise the copy held for
the core entry read under that name. Records that only hand over a directory
(`passes_path`, `scans_dir`) or copy a file the platform already names
(`links`, `copies`) place nothing, and a `search_dir` the code describes
rather than names ("directory of the Chihiro BIOS found") is skipped.

### Inheritance

If the new platform inherits from an existing one (e.g. Lakka inherits RetroArch),
set `inherits_from` in the registry AND add `inherits: retroarch` in the platform
YAML itself. `load_platform_config()` reads the `inherits:` field from the YAML to
merge parent systems and shared groups into the child. The child YAML only needs to
declare overrides.

## Step 3: Generate the platform YAML

Run the scraper with `--output` to produce the initial platform configuration:

```bash
python -m scripts.scraper.myplatform_scraper --output platforms/myplatform.yml
```

If a file already exists at the output path, the CLI preserves fields that the
scraper does not generate (e.g. `data_directories`, manually added metadata).
Only the `systems` section is replaced.

The YAML is the scraper's output and is never edited by hand: the next scrape
would undo the edit, and the file would stop describing the platform. Anything
an export has to write back (the platform's own system id, path, name) is
recorded by the scraper and never reconstructed later, because several of the
platform's systems can fold into one of ours. A rescrape can add systems, so
read `verify.py --platform` before committing the regenerated YAML.

Verify the result:

```bash
python scripts/verify.py --platform myplatform
python scripts/verify.py --platform myplatform --verbose
```

## Step 4: Add verification logic

Check how the platform verifies BIOS files by reading its source code.
The `verification_mode` in the platform YAML tells `verify.py` which strategy
to use:

| Mode | Behavior | Example platforms |
|------|----------|-------------------|
| `existence` | File must exist, no hash check | RetroArch, Lakka, RetroPie |
| `md5` | MD5 must match the declared hash | Batocera, Recalbox, RetroBat, EmuDeck, RetroDECK, RomM, ROCKNIX, MiSTer FPGA |
| `sha1` | SHA1 must match | BizHawk |

Inheriting platforms take the parent's mode: Lakka and RetroPie do not declare
one, they get `existence` from RetroArch.

If the platform has unique verification behavior (e.g. Batocera's `checkInsideZip`,
Recalbox's multi-hash comma-separated MD5, RomM's size + any-hash), add the logic
to `verify.py` in the platform-specific verification path.

Read the platform's source code to understand its exact verification behavior before writing any logic. Batocera's `checkInsideZip` uses `casefold()` for case-insensitive matching. Recalbox supports comma-separated MD5 lists. RomM checks file size before hashing. These details matter: the project replicates native behavior, not an approximation of it.

## Step 5: Create an exporter (optional)

Exporters convert truth data back to the platform's native format. They live in
`scripts/exporter/` and follow the same auto-discovery pattern (`*_exporter.py`).

### Module contract

The module must export an `Exporter` class inheriting `BaseExporter`:

```python
from scripts.exporter.base_exporter import BaseExporter

class Exporter(BaseExporter):
    @staticmethod
    def platform_name() -> str:
        return "myplatform"

    def export(self, truth_data: dict, output_path: str, scraped_data: dict | None = None) -> None:
        # Write truth_data in the platform's native format to output_path
        ...

    def validate(self, truth_data: dict, output_path: str) -> list[str]:
        # Return a list of issues (empty = valid)
        ...
```

`BaseExporter` provides helper methods:

- `_is_pattern(name)` - True if the filename contains wildcards or placeholders.
- `_dest(fe)` - resolve destination path from a file entry dict.
- `_display_name(sys_id, scraped_sys)` - convert a system slug to a display name.

### Round-trip validation

The scraper and the exporter are checked against each other as
[round-trip testing](adding-a-scraper.md#round-trip-testing) describes. The
export must not lose anything the platform already declares. If your
format carries code as well as data (a script, a C# database, a manifest
whose BIOS list sits beside launch configuration), say so with
`needs_original()` and patch the platform's original file rather than writing
a new one. An
exporter that regenerates such a file from BIOS data alone hands the
maintainer something that no longer runs.

## Step 6: Create a target scraper (optional)

Target scrapers determine which emulator cores are available on each hardware
target (e.g. which RetroArch cores exist for Switch, RPi4, or x86_64).
They live in `scripts/scraper/targets/` and are auto-discovered by filename
(`*_targets_scraper.py`).

### Module contract

```python
from scripts.scraper.targets import BaseTargetScraper

PLATFORM_NAME = "myplatform_targets"

class Scraper(BaseTargetScraper):
    def fetch_targets(self) -> dict:
        return {
            "platform": "myplatform",
            "source": "https://...",
            "scraped_at": "2026-03-30T00:00:00Z",
            "targets": {
                "x86_64": {
                    "architecture": "x86_64",
                    "cores": ["beetle_psx", "dolphin", "..."],
                },
                "rpi4": {
                    "architecture": "aarch64",
                    "cores": ["pcsx_rearmed", "mgba", "..."],
                },
            },
        }
```

Add `target_scraper` and `target_source` to the platform's registry entry.

### Overrides

Hardware-specific overrides go in `platforms/targets/_overrides.yml`. This file
defines aliases (e.g. `arm64` maps to `aarch64`) and per-platform core
additions/removals that the scraper cannot determine automatically.

### Single-target platforms

For platforms that only run on one target (e.g. RetroBat on Windows, RomM in the
browser), create a static YAML file in `platforms/targets/` instead of a scraper.
Set `target_scraper: null` in the registry.

Its target writes `cores: null`, which means every core the platform declares.
A core list written by hand there falls behind the scraped platform and drops
cores the pack ships.

## Step 7: Add install detection (optional)

The `install` section in `_registry.yml` tells `install.py` how to detect
the platform on the user's machine and locate its BIOS directory.

Three detection methods are available:

| Method | Description | Fields |
|--------|-------------|--------|
| `config_file` | Parse a key from a config file | `config`, `parse_key`, optionally `bios_subdir` |
| `path_exists` | Check if a directory exists | `path`, optionally `bios_path` |
| `file_exists` | Check if a file exists | `file`, optionally `bios_path` |

Each entry is scoped to an OS (`linux`, `darwin`, `windows`). Multiple entries
per OS are tried in order.

## Step 8: Validate the full pipeline

After all pieces are in place, run the full pipeline:

```bash
python scripts/pipeline.py --offline
```

This executes in sequence:

0. `validate_schemas.py --source-only` - the schema check PR validation runs
1. `generate_db.py` - rebuild `database.json` from `bios/`
1b. `provenance_report.py` - dump-catalog coverage from `provenance/`
2. `refresh_data_dirs.py` - update data directories (skipped with `--offline`)
3. `verify.py --all` - verify all platforms including the new one
4. `generate_pack.py --all` - build ZIP packs + install and target manifests
5. Consistency check: verify and the pack agree on counts and on the paths each archive holds
6. Pack integrity - extract ZIPs and verify hashes per platform mode
7. `generate_readme.py` - regenerate README and CONTRIBUTING
8. `generate_site.py` - regenerate documentation site and `mkdocs.yml`

A new platform is archived-by-default nowhere: `status: active` puts it in
`list_platforms.py`, the CI matrix, and the scrapers. Set
`status: archived` to keep the configuration without the automation. Archived
platforms are excluded unless `--all` or `--include-archived` is passed.

Check the output for:

- The new platform appears in verify results
- No unexpected CRITICAL or WARNING entries
- Pack generation succeeds and includes the expected files
- Consistency check passes (verify file counts match pack file counts)

Verification is not optional. A platform that passes `pipeline.py` today may break tomorrow if upstream changes its data format. Run the full pipeline on every change, even if the modification seems trivial. The consistency check (verify and the pack must agree on counts and paths) catches subtle issues where files resolve during verification but fail during pack generation, or vice versa.

## Checklist

- [ ] Scraper file in `scripts/scraper/<name>_scraper.py`
- [ ] `PLATFORM_NAME` and `Scraper` class exported
- [ ] `fetch_requirements()` and `validate_format()` implemented
- [ ] System ID mapping covers all upstream systems
- [ ] Entry added to `platforms/_registry.yml`
- [ ] Platform YAML generated and verified
- [ ] `python scripts/pipeline.py --offline` passes
- [ ] Exporter in `scripts/exporter/<name>_exporter.py` (if applicable)
- [ ] Target scraper in `scripts/scraper/targets/<name>_targets_scraper.py` (if applicable)
- [ ] Install detection entries in `_registry.yml` (if applicable)
