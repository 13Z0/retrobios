---
level: contribute
---

# Testing guide

The suite is discovered dynamically across focused modules, so its exact count
is reported by the test runner instead of being hand-maintained here. Tests do
not require network access. Most build synthetic fixtures under the repository's
`tmp/`. Modules that read real artifacts skip cleanly when those inputs are absent.

## Running tests

Run the full suite:

```bash
python -m unittest discover -s tests -t . -v
```

Run a single module, or several:

```bash
python -m unittest tests.test_e2e -v
python -m unittest tests.test_install tests.test_install_cores -v
```

The table below says which module covers what.

Running the build needs Python 3.9 or newer and `pyyaml` alone. CI runs the suite on 3.12,
and one test imports every module under `scripts/` with a real `python3.9`, which
needs `pyyaml` and `jsonschema` installed for that interpreter. The
schema contract tests also need `jsonschema[format-nongpl]`, and one site test
renders a wiki line with `markdown`. The CI installs both: without them those
tests are reported as skipped, so install them before a pull request
(`pip install "jsonschema[format-nongpl]" markdown`). No test framework beyond
the standard library `unittest` module.

## Modules at a glance

Every module in `tests/`, by the area it guards.

| Area | Fixtures | Modules |
|------|----------|---------|
| Core regression suites | synthetic | `test_e2e`, `test_audit_regressions`, `test_tool_messages` |
| File resolution and identity | synthetic + collection | `test_adler32_resolution`, `test_agnostic_scan`, `test_agnostic_scan_mode`, `test_agnostic_scope`, `test_agnostic_substitute`, `test_archive_choice`, `test_bios_companions`, `test_bundled_identity`, `test_casefold_cache`, `test_db_aliases`, `test_declared_names`, `test_derivations`, `test_directory_entries`, `test_extra_identity`, `test_foreign_tree`, `test_foreign_tree_names`, `test_frontend_digest`, `test_game_data_homonyms`, `test_hashing`, `test_identity`, `test_md5_pack_context`, `test_name_tiebreak`, `test_nec_filing`, `test_owner_resolution`, `test_path_suffix`, `test_resolution_evidence`, `test_sega_filing`, `test_shipped_content`, `test_twin_declarations`, `test_validated_choice`, `test_variant_choice`, `test_variant_identity`, `test_zipped_member` |
| Verification, cross-reference and profile checks | synthetic + profiles | `test_case_insensitive_platforms`, `test_checks_have_values`, `test_core_file_fills_declaration`, `test_core_size_refusal`, `test_cross_reference_sources`, `test_crypto_aes`, `test_crypto_unique_data`, `test_crypto_verify`, `test_entry_categories`, `test_file_consumers`, `test_fileless_audit`, `test_flycast_layout`, `test_gap_settlement`, `test_hidden_extras`, `test_launcher_aliases`, `test_launcher_layer`, `test_native_mode`, `test_no_case_collisions`, `test_path_conflict_status`, `test_path_null_copies`, `test_profile_checks`, `test_profile_repositories`, `test_romm_hash_rule`, `test_shared_groups`, `test_standalone_links`, `test_standalone_mode`, `test_undeclared_dedup`, `test_unplaced_extras`, `test_validation_scope`, `test_verify_all_dir`, `test_verify_emulator_evidence`, `test_verify_listing`, `test_verify_pack_choice`, `test_verify_release_asset` |
| Packs and manifests | synthetic + real packs | `test_artifact_lock`, `test_atomic_artifacts`, `test_batocera_cores`, `test_deterministic_zip`, `test_emulator_pack_counts`, `test_finalize_pack_names`, `test_large_file_cache`, `test_libretro_layout`, `test_manifest_entry`, `test_minui_platform`, `test_no_game_data`, `test_pack_conformance_dirs`, `test_pack_core_extras`, `test_pack_counts`, `test_pack_destinations`, `test_pack_failures`, `test_pack_file_names`, `test_pack_grouping`, `test_pack_integrity`, `test_pack_member_identity`, `test_pack_readme`, `test_platform_inheritance`, `test_platform_keys`, `test_required_only_archives`, `test_required_only_twins`, `test_split_extras`, `test_split_names`, `test_split_pack`, `test_system_pack_extras`, `test_system_pack_parity`, `test_torrentzip`, `test_which_pack` |
| Slots and regions | synthetic + profiles | `test_region`, `test_region_agnostic_scan`, `test_region_groups`, `test_region_held`, `test_region_platform_index`, `test_region_required_only`, `test_slot`, `test_slots`, `test_slots_shipped_file` |
| Hardware targets and systems | synthetic + target files | `test_extract_targets`, `test_lakka_targets`, `test_libretro_slugs`, `test_system_ids`, `test_system_selection`, `test_target_names`, `test_target_scrapers`, `test_target_systems`, `test_target_verify` |
| Installer and downloads | loopback server | `test_check_release`, `test_download`, `test_install`, `test_install_cores`, `test_install_experience`, `test_install_selection` |
| Handheld port | stubbed PortMaster dialogs, fake installer | `test_port` |
| Truth and native export | synthetic + cached originals | `test_export_counts`, `test_export_native_layer`, `test_export_report`, `test_exporters`, `test_truth_data_dir_readers`, `test_truth_diff`, `test_truth_layout` |
| Scrapers, upstream and provenance | inline sources, synthetic | `test_check_freshness`, `test_dedup`, `test_elec_scrapers`, `test_fbneo_parser`, `test_find_pinned_revision`, `test_hash_merge`, `test_launchers`, `test_launchers_emudeck`, `test_launchers_recalbox`, `test_launchers_retrobat`, `test_launchers_retrodeck`, `test_launchers_retropie`, `test_launchers_rocknix`, `test_mame_devices`, `test_mame_parser`, `test_mame_ref_audit`, `test_profile_sync`, `test_provenance`, `test_refresh_data_dirs`, `test_refresh_failures`, `test_refresh_stale`, `test_retrodeck_scraper_errors`, `test_romset_recipes`, `test_scraper_contract`, `test_upstream`, `test_upstream_index` |
| Site, docs and repository contracts | rendered pages, sources | `test_contributing_text`, `test_data_dir_refs`, `test_dataclass_defaults`, `test_doc_pages`, `test_doc_surfaces`, `test_optional_dependencies`, `test_prose_style`, `test_site_availability`, `test_site_cores`, `test_site_emulator_page`, `test_site_exports`, `test_site_funnel`, `test_site_platform_hashes`, `test_site_render`, `test_site_system_links`, `test_site_validation`, `test_site_write`, `test_validate_pr`, `test_validate_pr_flow`, `test_validate_pr_hashes`, `test_workflow_paths` |

## Test architecture

### test_e2e.py

The main regression suite. `TestE2E` exercises the resolution, verification,
pack generation and cross-reference logic, and `StandaloneModeRequiresAStandaloneBuild`
sits beside it for the per-emulator build mode.

**Fixture pattern.** `setUp` creates a temporary directory tree with:

- Fake BIOS files (deterministic content for hash computation)
- Platform YAML configs (existence mode, MD5 mode, inheritance, shared groups)
- Emulator profile YAMLs (required/optional files, aliases, HLE, standalone)
- A synthetic `database.json` keyed by SHA1

`tearDown` removes the temporary tree.

**Test numbering.** Tests are grouped by category:

| Range | Category |
|-------|----------|
| `test_01`--`test_14` | File resolution (SHA1, MD5, name, alias, truncated MD5, composite, zip contents, variants, hash mismatch) |
| `test_20`--`test_31` | Verification (existence mode, MD5 mode, required/optional severity, zipped file, multi-hash) |
| `test_40`--`test_51` | Cross-reference and platform grouping (undeclared files, standalone skip, alias profiles, data dir suppression, exclusion notes) |
| `test_60`--`test_61` | Storage tiers (external, user-provided) |
| `test_70`--`test_84` | Emulator-level validation (index build, size, CRC32, MD5, SHA1, crypto) |
| `test_90`--`test_125` | Per-emulator and per-system verification, `dest_hint` resolution, registry metadata |
| `test_130`--`test_183` | Pack generation (required-only, split, `--from-md5`, path conflicts, archive extras, truth generation, exporters) |
| `test_200`--`test_227` | Pack source variants, deterministic ZIPs, SHA256/CRC32 resolution, MiSTer scraper, manifests |

Numbers are stable anchors, not an execution order. When a range fills up, a
letter suffix keeps a new test next to the behavior it covers
(`test_130b_existence_pack_reports_hash_mismatch`).

Each test calls the same functions that `verify.py` and `generate_pack.py` use
in production, against the synthetic fixtures.

### Parser tests

**test_mame_parser.** Tests the MAME C source parser that extracts BIOS root
sets from driver files. Fixtures are inline C source snippets containing
`ROM_START`, `ROM_LOAD`, `GAME()`/`COMP()` macros with
`MACHINE_IS_BIOS_ROOT`. Tests cover:

- Standard `GAME` macro detection
- `COMP` macro detection
- `ROM_LOAD` / `ROMX_LOAD` parsing (name, size, CRC32, SHA1)
- `ROM_SYSTEM_BIOS` variant extraction
- Multi-region ROM blocks
- Macro expansion and edge cases

**test_mame_devices.** Tests the device ROM sets read from a MAME `-listxml`
and merged into a profile, on an inline listxml and driver source. Covers:

- Only `isdevice="yes"` sets with a dumped ROM kept, from a plain or zipped listxml
- The `ROM_START` block cited for a set: found through a ROM macro, a block
  inside a comment ignored, one dump under two names told apart, the label
  `device_rom_region` returns, not found and ambiguous
- Merge: a set added under the generated section in name order, a written
  entry's contents refreshed with its manual fields kept, BIOS root sets left
  as written, a second run changing nothing, a new BIOS root placed before the
  section
- Systems: the links the scan keeps (`romof`, `device_ref`, `slotoption`, a
  device's parent ROM device), a set reached through a clone, its parent and
  a slot card, one set under two systems, a machine no entry lists placing
  nothing, seeds read above the generated section only, `system:` or
  `systems:` written and rewritten by a refresh, a written entry's system
  kept, and a `--system` pick reading the entry's own systems through the gap
  report and the core extras
- Seeds a platform file states: a machine of the release under one of the
  profile's systems, a device, an unlisted system or a set stated two ways
  seeding nothing
- Support files: each software list filed under the systems whose machines
  and slot cards declare it, one entry per list of the delivered `hash/`
  tree, a pick keeping its lists and the unfiled ones, floppy samples filed
  through the form factor each drive selects (3" falls back to 3.5", the
  rest to 5.25"), a refresh rewriting the section and a second run changing
  nothing
- An entry's `system:` or `systems:` outside its profile's list refused by
  the semantic check

**test_fbneo_parser.** Tests the FBNeo C source parser that identifies
`BDF_BOARDROM` sets. Same inline fixture approach.

**test_hash_merge.** Tests the text-based YAML patching module used to merge
upstream BIOS hashes into emulator profiles. Covers:

- Merge operations (add new hashes, update existing)
- Diff computation (detect what changed)
- Formatting preservation (comments, ordering, flow style)

Fixtures are programmatically generated YAML/JSON files written to a temp
directory.

### Provenance and installer tests

**test_provenance.** Covers the dump-catalog pipeline end to end on synthetic
DATs: the Logiqx XML parser, the Redump scraper's parsing path, the DAT pack
importer, the SHA1-then-MD5+size join performed by `generate_db.py`, and the
coverage report's covered/uncovered DAT accounting.

**test_install.** Covers `install.py` without touching the network: OS
detection, each registry detection method (`config_file`, `path_exists`,
`file_exists`), config-file key parsing, manifest loading, target filtering,
and destination resolution.

**test_profile_sync.** Covers ref anchoring end to end: the six statuses,
widening a one-line anchor until it is unique, refusing to rebase an ambiguous
one, following a rename, and the guarded YAML writes. Network access is
replaced by an injected fetch function.

**test_upstream.** Covers forge URL parsing for GitHub, GitLab and Forgejo,
the content-addressed cache and its atomic write, revision and tag resolution,
tree comparison and the listing of one directory. The HTTP layer is replaced at
module level, so nothing leaves the machine.

### Tests that read the working tree

Three modules assert on real repository data instead of fixtures. Each skips
when its input is missing, so a partial checkout still runs green.

**test_torrentzip.** Rebuilds real MAME romsets through the TorrentZip builder
and asserts the output is byte-identical, which is what keeps arcade ZIP hashes
stable across rebuilds.

**test_no_case_collisions.** Walks `bios/` and fails on two paths that differ
only by case. On Windows and macOS, git can only check out one of them, which
silently corrupts the clone. `.variants/` is exempt: those names are
disambiguated by a hash suffix on purpose.

**test_pack_integrity.** Described below. Needs packs in `dist/`.

## How to add a test

1. **Pick the right category.** Find the number range that matches the
   subsystem you are testing. If none fits, start a new range after the last
   existing one.

2. **Create synthetic fixtures.** Write the minimum YAML configs and fake
   files needed to isolate the behavior. Use `tempfile.mkdtemp` for a clean
   workspace. Avoid depending on the repo's real `bios/` or `platforms/`
   directories.

3. **Call production functions.** Import from `common`, `verify`, `validation`,
   or `truth` and call the same entry points that the CLI scripts use. Do not
   re-implement logic in tests.

4. **Assert specific outcomes.** Check `Status`, `Severity`, resolution
   method, file counts, or pack contents. Avoid brittle assertions on log
   output or formatting.

5. **Run the full suite.** After adding your test, run `python -m unittest
   discover -s tests -t . -v` to verify nothing else broke. `-t .` imports
   `tests/__init__.py`, which keeps the suite offline.

A fix comes with a test that fails without it. Write the test first, watch it
fail on the current code, then fix: a test written after the fix may never
have exercised the case it names. When a reading of the code finds a fact a
script could have established, the script gains that check in the same change.

Example skeleton:

```python
def test_my_new_behavior(self):
    # Minimal fixtures under self.root
    profile = {"emulator": "test_core", "type": "libretro", "files": [...]}
    with open(os.path.join(self.emulators_dir, "test_core.yml"), "w") as f:
        yaml.dump(profile, f)

    # The entry point the CLI uses
    config = load_platform_config("test_md5", self.platforms_dir)
    result = verify_platform(config, self.db, self.emulators_dir)

    # One outcome, read from the per-file details
    by_name = {d["name"]: d for d in result["details"]}
    self.assertEqual(by_name["my_file.bin"]["status"], Status.OK)
```

### test_pack_integrity.py

End-to-end pack verification. Extracts each platform ZIP to `tmp/` (in the
repo, not `/tmp` which is tmpfs on WSL) and verifies that every file declared
in the platform YAML:

1. Exists at the correct path on disk after extraction
2. Has the correct hash per the platform's native verification mode

Handles inner ZIP verification for MAME/FBNeo ROM sets (checkInsideZip,
md5_composite, inner ROM MD5) and path collision deduplication.

One test per distinct pack contract: RetroArch, Batocera, BizHawk, EmuDeck,
Recalbox, RetroBat, RetroDECK, RomM. Inherited aliases such as Lakka reuse the
same artifact and are not counted twice.

```bash
python -m unittest tests.test_pack_integrity -v
# or via CLI:
python scripts/generate_pack.py --all --verify-packs --output-dir dist/
```

Integrated as pipeline step 6/8 (runs after consistency check, before
README generation). Requires packs in `dist/`, skip with `--skip-packs`.

### test_port.py

The PortMaster port in `ports/RetroBIOS/`. `bash -n` on its scripts, the
archive `scripts/build_port.py` writes (layout, reproducibility, the bundled
`install.py` equal to the one `install.sh` pins), and the event reader. The
screens run in bash with PortMasterDialog's functions replaced by stubs that
record each command and answer from a script, against a fake `install.py`
whose event streams follow `EVENT_KEYS`: all files present, some missing
(update everything, choose systems), no network, no BIOS folder detected,
Python too old, and an answer that arrives after the 30-second wait of the
older PortMaster releases. Drawing, the pad, `mount` and the network belong
to a device and are not exercised.

```bash
python -m unittest tests.test_port -v
```

## Verification discipline

The test suite is one layer of verification. The full quality gate is:

1. All unit tests pass (`python -m unittest discover -s tests -t .`)
2. The full pipeline completes without error (`python scripts/pipeline.py --offline`)
3. No unexpected CRITICAL entries in the verify output
4. Pack file counts match verification file counts (consistency check)
5. Pack integrity passes (every declared file extractable with correct hash)
6. `mkdocs build --strict` and `python scripts/validate_site.py` pass

A change that passes the tests but breaks the pipeline is not merged. A new
CRITICAL entry in the verify output after a change is explained before the
change lands. The steps agree with each other: when verify reports N files for
a platform, the pack holds exactly N files.

Tests, code, profiles, platform configs and documentation ship in the same
change.

A run that dies with a segmentation fault or an exception the code cannot
raise is run again once, alone, before anything is investigated. Only a
failure that reproduces is a defect in the code.

## Prose and tone

`tests/test_prose_style.py` holds every tracked file, and the generated site,
to the way the project writes:

- No em-dash or en-dash, no semicolon joining two clauses, no unicode arrows,
  no ellipsis character, no box-drawing banners, no emoji or check marks.
  Rewrite the sentence rather than swapping the character.
- None of the marketing vocabulary the test lists, and no superlatives. A
  number with its criterion says more.
- Headings in sentence case, never repeating the site name.
- In emulator profiles, no verdict on another project's file: "phantom",
  "misleading", "bogus", "typo", "has errors", "incorrectly says", and on a
  line naming a .info, core-info or System.dat no "is correct", "is
  accurate", "still declares", "omits" or "missing". The note states what
  that file declares and what the code reads.

Formatting stays: bold labels, lists, tables and admonitions make a page
easier to use. When markup shows raw on the site, the rendering is fixed and
the formatting kept.

Beyond those words the test cannot read tone. Text that names another
project says what changes, not who erred: "hash updated to the emulator source", "files added",
"required flags aligned with the code". No "wrong", "bogus" or "phantom" about
someone else's work. Notes in a profile say what the core does, nothing about
what it is not. A reply on an issue or a pull request thanks once, gives the
facts, describes the script or the file rather than the person, and leaves
the door open.

## Landing a change

A commit subject is English, 50 characters at most, `type: description` with a
lowercase word after the colon. The types are `feat`, `refactor`, `chore`,
`docs` and `fix`. The message does not use bug, regression, revert, dead code,
broken, wrong, hack, workaround or implement (derived forms included), nor the
marketing vocabulary above, and carries no Co-Authored-By trailer. A body
explains the why when the subject cannot: the scenario that failed, the
upstream `file:line` that decides it, what users will see.

```
feat: add retrobat platform support
refactor: extract shared utils to common.py
```

Files are staged by name, never with `git add -A` or `git add .`, which sweep in
deletions and work that is not yours. Generated artifacts (`database.json`,
`install/*.json`, `README.md`, `CONTRIBUTING.md`, `exports/`) are committed
with the data they derive from, never alone: a manifest committed before the
files it points at gives the installer a 404.

## CI integration

The `validate.yml` workflow runs `python -m unittest discover -s tests -t . -v` on both
roads into main: every pull request, and every direct push, that touches
`bios/`, `platforms/`, `emulators/`, `schemas/`, `scripts/`, `tests/`,
`install.py` or an input of the site build such as `wiki/`. The same job then
builds the site with `mkdocs build --strict`, so a page that would fail the
deploy fails the pull request. Work reaches main by push as often as by pull request, so a suite
wired to pull requests alone would guard the road nobody takes. The test job
(`run-tests`) runs in parallel with schema validation, and on a pull request
with BIOS validation and auto-labeling too. Those two read pull request context
and stay behind an event guard.

A push series collapses to the tip, so what a green run states is that the head
of main passes, not every commit under it.

Modules that need real artifacts skip themselves when those artifacts are absent,
so `test_pack_integrity` is a no-op in CI (no `dist/`) and a real check locally.
Run `python -m unittest discover -s tests -t .` before pushing.

Tests must pass before merge. If a test fails in CI, reproduce locally with:

```bash
python -m unittest discover -s tests -t . -v
```
