# Verification modes

Each platform verifies BIOS files differently. `verify.py` replicates the native behavior
of each platform so that verification results match what the platform itself would report.


## Existence mode

**Platforms**: RetroArch, Lakka, RetroPie

**Source**: RetroArch `core_info.c`, function `path_is_valid()`

The most straightforward mode. A file is OK if it exists at the expected path. No hash is checked.
Any file with the correct name passes, regardless of content.

| Condition | Status | Severity (required) | Severity (optional) |
|-----------|--------|---------------------|---------------------|
| File present | OK | OK | OK |
| File missing | MISSING | WARNING | INFO |

RetroArch does not distinguish between a correct and an incorrect BIOS at the verification
level. A corrupt or wrong-region file still shows as present. This is by design in the
upstream code: `core_info.c` only calls `path_is_valid()` and does not open or hash the file.

Lakka and RetroPie inherit this behavior through platform config inheritance
(`inherits: retroarch` in the platform YAML).


## MD5 mode

**Platforms**: Batocera, RetroBat, Recalbox, EmuDeck, RetroDECK, RomM, ROCKNIX, MiSTer FPGA

All MD5-mode platforms compute a hash of the file and compare it against an expected value.
The details vary by platform.

### Standard MD5 (Batocera, RetroBat)

`verify.py` replicates Batocera's `md5sum()` function. The file is read in binary mode,
hashed with MD5, and compared case-insensitively against the expected value.

| Condition | Status | Severity (required) | Severity (optional) |
|-----------|--------|---------------------|---------------------|
| Hash matches | OK | OK | OK |
| File present, hash differs | UNTESTED | WARNING | WARNING |
| File missing | MISSING | CRITICAL | WARNING |

If the `resolve_local_file` step already confirmed the MD5 match (status `md5_exact`),
`verify.py` skips re-hashing and returns OK directly.

### Truncated MD5 (Batocera)

Some entries in Batocera's system data carry 29-character MD5 strings instead of
the usual 32. Batocera's script compares the full string (`batocera-systems:1088`), so
those entries report the file as untested. `verify.py` compares the prefix instead, to
find the file the entry designates: if the expected hash is shorter than 32 characters,
the actual hash is compared against only its first N characters.

### md5_composite (Recalbox ZIP verification)

Recalbox hashes a ZIP with `Zip::Md5Composite` (`es-core/src/utils/Zip.cpp:71-96` in
recalbox-emulationstation at `f4cd0ae4`): one MD5 over the contents of every member,
read in filename order. The members' own MD5s are not involved. `verify.py` computes
the same value with `md5_composite()` (`scripts/hashing.py`). When a ZIP file's direct
MD5 does not match, the composite is tried before reporting a mismatch.

### Multi-hash (Recalbox)

Recalbox allows comma-separated MD5 values for a single file entry, accepting any one
of them as valid. `verify.py` splits on commas and tries each hash. A match against any
listed hash is OK.

### Mandatory levels (Recalbox)

Recalbox colours each entry of `es_bios.xml` from two attributes, `mandatory` and
`hashMatchMandatory`, both true unless written `"false"`
(`es-app/src/bios/Bios.cpp:162-163` in recalbox-emulationstation at `f4cd0ae4`,
colours set by `Bios::Scan` at lines 84-108). The platform YAML carries them as
`required` and, when the attribute is written, `hash_match_mandatory`.

| File | mandatory | hashMatchMandatory | Recalbox | verify.py |
|------|-----------|--------------------|----------|-----------|
| hash matches | (any) | (any) | GREEN | OK |
| missing | true | (any) | RED | CRITICAL |
| missing | false | (any) | YELLOW | WARNING |
| hash differs | true | true | RED | WARNING |
| hash differs | true | false | YELLOW | WARNING |
| hash differs | false | (any) | YELLOW | WARNING |

`verify.py` applies the MD5-mode table above and does not read
`hash_match_mandatory`, so a present file whose hash differs is a WARNING where
Recalbox shows RED for a mandatory entry with `hashMatchMandatory`.

### checkInsideZip (Batocera zippedFile)

When a platform entry has a `zipped_file` field, the expected MD5 is not the hash of the
ZIP container but of a specific ROM file inside the ZIP. `verify.py` replicates Batocera's
`checkInsideZip()`:

1. Open the ZIP.
2. Find the inner file by name (case-insensitive via `casefold()`).
3. Read its contents and compute MD5.
4. Compare against the expected hash.

If the inner file is not found inside the ZIP, the status is UNTESTED with a reason string.

### RomM verification

**Source**: `backend/models/firmware.py`, `Firmware.verify_file_hashes`

RomM reads the file's bytes (`verification_mode: md5`) but does not compare one
digest. Its platform YAML carries `hash_rule: size_and_any_hash`: the file's
size must equal the declared size, then any one of MD5, SHA1 and CRC32 must
match. An entry without a size never verifies, since RomM reads it as 0. A
wrong hash beside a right one does not withhold the file. `verify.py`, the
pack builder and the install manifest apply that rule. ZIP files are not
opened, only the container is checked.

`verification_mode` answers whether the frontend reads the bytes, and
`hash_rule` answers which comparison it makes. A frontend whose acceptance is
not a single digest gets a new `hash_rule` value, never a new mode.

### ROCKNIX verification

**Source**: `rocknix-systems`, function `checkBios`

ROCKNIX derives its BIOS checker from Batocera's, so the logic has the same
shape: `md5sum()` on the file, `checkInsideZip()` for archive entries, and an
`altmd5` field that accepts a second hash for the same entry. Every entry the
platform declares is mandatory in its own data, so the platform YAML marks all
38 files `required: true` and none of them use `zippedFile`.

### MiSTer FPGA verification

**Source**: `Downloader_MiSTer`, `jobs/process_db_index_worker.py`

The MiSTer downloader hashes the file at its destination path and compares it
against the MD5 recorded in the BIOS database. There is no ZIP inspection and
no required/optional distinction: an entry either matches or it does not.

Scope note: only the BiosDB entries are in scope. They carry an explicit `url`
because MiSTer cannot host them, which is exactly what the user has to supply.
The 208 `games/` files shipped by the `Distribution_MiSTer` repository carry no
URL and install themselves, so they are not part of the pack.


## SHA1 mode

**Platforms**: BizHawk

BizHawk firmware entries use SHA1 as the primary hash. `verify.py` computes SHA1
via `compute_hashes()` and compares case-insensitively.

| Condition | Status | Severity (required) | Severity (optional) |
|-----------|--------|---------------------|---------------------|
| SHA1 matches | OK | OK | OK |
| File present, SHA1 differs | UNTESTED | WARNING | WARNING |
| File missing | MISSING | CRITICAL | WARNING |


## Emulator-level validation

Independent of platform verification mode, `verify.py` runs emulator-level validation
from `validation.py`. This layer uses data from emulator profiles (YAML files in
`emulators/`), which are source-verified against emulator code.

### Validation index

`_build_validation_index()` (`validation.py`) builds a per-filename index of
validation rules from the profiles it is given. A platform report, its pack and
its install manifest pass only the profiles `resolve_platform_cores` resolves for
that platform (`verify.py`, `verify_platform`). A profile that does not run on the
platform has nothing to say about its pack: standalone ZEsarUX reads a 48 KB
`cpc6128.rom` of its own, and its size check reported cap32's 32 KB file in the
RetroArch pack as a mismatch while every profile was indexed. The same holds for
`hle_fallback`, which is read from the same set of profiles. A report or pack for `--emulator` or `--system` indexes the
profiles it selected. When multiple emulators reference the same file, checks
are merged (union of all check types). Conflicting expected values are kept as sets
(e.g., multiple accepted CRC32 values for different ROM versions).

Each entry in the index tracks:

- `checks`: list of validation types (e.g., `["size", "crc32"]`)
- `sizes`: set of accepted exact sizes
- `min_size`, `max_size`: bounds when the code accepts a range
- `crc32`, `md5`, `sha1`, `sha256`: sets of accepted hash values
- `adler32`: set of accepted Adler-32 values
- `crypto_only`: non-reproducible checks (see below)
- `per_emulator`: per-core detail with source references

### Check categories

Validation checks fall into two categories:

**Reproducible** (`_HASH_CHECKS`): `crc32`, `md5`, `sha1`, `adler32`. These can be
computed from the file alone. `verify.py` calculates hashes and compares against
accepted values from the index.

**Non-reproducible** (`_CRYPTO_CHECKS`): `signature`, `crypto`. These require
console-specific cryptographic keys (e.g., RSA-2048 for 3DS, AES-128-CBC for certain
firmware). `verify.py` reports these as informational but cannot verify them without
the keys. Size checks still apply if combined with crypto.

### Size validation

Three forms:

- **Exact size**: `size: 524288` with `validation: [size]`. File must be exactly this many bytes.
- **Range**: `min_size: 40`, `max_size: 131076` with `validation: [size]`. File size must fall within bounds.
- **Informational**: `size: 524288` without `validation: [size]`. The size is documented but the emulator does not check it at runtime.

### Complement to platform checks

Emulator validation runs after platform verification. When a file passes platform checks
(e.g., existence-mode OK) but fails emulator validation (e.g., wrong CRC32), the result
includes a `discrepancy` field:

```
file present (OK) but handy says size mismatch: got 256, accepted [512]
```

This catches cases where a file has the right name but wrong content, which existence-mode
platforms cannot detect.


## Severity matrix

`compute_severity()` maps the combination of status, required flag, verification mode,
and HLE fallback to a severity level.

| Mode | Status | required | hle_fallback | Severity |
|------|--------|----------|--------------|----------|
| any | OK | any | any | OK |
| any | MISSING | any | true | INFO |
| existence | MISSING | true | false | WARNING |
| existence | MISSING | false | false | INFO |
| md5/sha1 | MISSING | true | false | CRITICAL |
| md5/sha1 | MISSING | false | false | WARNING |
| md5/sha1 | UNTESTED | any | false | WARNING |

**HLE fallback**: when an emulator profile marks a file with `hle_fallback: true`, the
core has a built-in high-level emulation path and functions without the file. Missing
files are downgraded to INFO regardless of platform mode or required status. The file
is still included in packs (better accuracy with the real BIOS), but its absence is not
actionable.


## File resolution chain

Before verification, each file entry is resolved to a local path by `resolve_local_file()`.
The function tries these steps in order, returning the first
evidence-compatible match:

| Step | Method | Returns | When it applies |
|------|--------|---------|-----------------|
| 1 | SHA1 | `sha1_exact` | A declared SHA1 identifies a database record, and every other declared hash must agree with that same record |
| 2 | SHA256 | `sha256_exact` | A declared SHA256 identifies a record, again requiring all declarations to agree |
| 3 | CRC32 plus size | `crc32_exact` | Used only when no stronger hash is declared. Size confirms the weaker checksum when available |
| 4 | MD5 | `md5_exact` | Direct lookup. An explicitly supported truncated MD5 also needs a compatible name |
| 5 | Path suffix | `path_exact` or a hash-exact status | Disambiguates regional paths. With any declared hash, the path is accepted only when the content matches it |
| 6 | Name or alias | `name_exact` | Existence/size resolution only when no content hash is declared. Prefers primary files and supports casefold matching |
| 7 | Named candidate inspection | `md5_composite_exact`, `md5_exact`, or `hash_mismatch` | Checks composite ZIP MD5 or direct MD5. A matching name with wrong content is surfaced, never treated as exact |
| 8 | ZIP contents index | `zip_exact` | `zipped_file` with MD5. Searches the inner-ROM index only after name-based resolution fails |
| 9 | MAME clone | `mame_clone` | Resolves a deduplicated clone to its canonical set only when no content hash was declared |
| 10 | Data directory | `data_dir` or `data_dir_hash_exact` | Searches exact path then case-insensitive basename, then computes every declared hash before accepting an unindexed candidate |
| 11 | Agnostic fallback | `agnostic_fallback` | Size/path constrained lookup only when no content hash was declared |

If no step matches, the result is `(None, "not_found")`.

The `hash_mismatch` status means a file with the right name or path exists but its hash
does not match. This still resolves to a local path (the file is present), but verification
will report it as UNTESTED with a reason string showing the expected vs actual hash prefix.

A path or filename is never allowed to mask a declared strong hash: the
identity steps run first, and a path or name is accepted only when no content
hash was declared or when the content agrees with the one that was.

What the pack does with a `hash_mismatch` is set by the platform's mode, as
[discrepancy detection](#discrepancy-detection) describes.


## Discrepancy detection

When platform verification passes but emulator validation fails, the file has a discrepancy.
This happens most often in existence-mode platforms where any file with the right name is
accepted.

A local file that contradicts a declared hash is handled by the platform's
own mode, never by one global rule. An existence frontend never reads the
bytes, so the file is packed and the difference reported: a hash in System.dat
that differs from the local dump must not remove a file RetroArch would have
loaded. An md5 or sha1 frontend would reject it, so it is left out of the pack
and counted as `unsafe excluded`, which is distinct from `missing`. The pack
report and the installer manifest both record it. The pack builder and the
pack verification apply the same rule.

### Variant search

`validated_choice()` (`scripts/validation.py`) decides the file a platform
destination ships when the emulators reading that destination check it. The
pack, verify and the install manifest all read it, so they name the same file.

1. Only the cores that read this destination judge it: a core checking the
   same name at another path has no say (ZEsarUX's 48 KB `cpc6128.rom` check
   does not judge `ep128emu/roms/cpc6128.rom`).
2. The resolved file is kept when every check of those cores passes.
3. Otherwise `find_validated_variant()` looks for a held file that passes them
   all, warning-level checks included. Candidates come first from the hashes
   the emulator declares, which finds a dump stored under another name, then
   from the files sharing the name, `.variants/` included, in a stable order.
   On a md5 or sha1 platform a candidate must also carry a value the platform
   entry declares, or the frontend would refuse it.
4. When only a warning-level check fails (`on_mismatch: warn`), the file
   itself stands: the core runs it.
5. Then a held file the core at least runs.
6. Failing all of these, the platform-verified file is kept and the
   disagreement is reported ("<core> says ..."), never as compatible.

### Practical example

A `scph5501.bin` file passes Batocera MD5 verification (hash matches upstream
declaration) but fails an emulator profile's size check because the profile
was verified against a different revision. The search looks for a held file
carrying a Batocera-accepted md5 that also passes the size check, under that
name or any name the emulator's hashes reach. If found, it ships. If not, the
Batocera-verified file ships and the discrepancy stays visible in verification
and gap reporting.

