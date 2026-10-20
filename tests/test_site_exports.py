"""Regression tests for the public website data and citation contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_site import (  # noqa: E402
    _admonition_body,
    _browser_title,
    _forge_sources,
    _source_ref_markdown,
    decorate_markdown_pages,
    generate_data_exports,
)


class SiteReferenceContracts(unittest.TestCase):
    def setUp(self) -> None:
        scratch = ROOT / "tmp" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.docs = Path(self.temp.name) / "docs"
        self.docs.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _fixtures() -> tuple[dict, dict, dict, dict]:
        payload = b"firmware"
        sha1 = hashlib.sha1(payload).hexdigest()
        md5 = hashlib.md5(payload).hexdigest()
        sha256 = hashlib.sha256(payload).hexdigest()
        db = {
            "schema_version": 1,
            "generated_at": "2026-08-09T00:00:00Z",
            "total_files": 1,
            "total_size": len(payload),
            "files": {
                sha1: {
                    "path": "bios/Test/fw.bin",
                    "name": "fw.bin",
                    "size": len(payload),
                    "sha1": sha1,
                    "md5": md5,
                    "sha256": sha256,
                    "crc32": "0f00ba11",
                    "adler32": "0f00ba11",
                }
            },
            "indexes": {
                "by_md5": {md5: sha1},
                "by_name": {"fw.bin": [sha1]},
                "by_crc32": {"0f00ba11": sha1},
                "by_sha256": {sha256: sha1},
                "by_path_suffix": {"fw.bin": [sha1]},
            },
        }
        coverages = {
            "test": {
                "platform": "Test Platform",
                "config": {
                    "platform": "Test Platform",
                    "cores": ["testemu"],
                    "systems": {
                        "test-system": {
                            "files": [
                                {
                                    "name": "fw.bin",
                                    "destination": "fw.bin",
                                    "required": True,
                                    "sha1": sha1,
                                    "md5": "C87A52C277F7952B41C639FC7BF0A43B,"
                                           "d0ed892a5e8d47e0cb5f8aa5f4dd7c5a",
                                    "crc32": "3F9F95A4",
                                    "size": len(payload),
                                }
                            ]
                        }
                    },
                },
                "total": 1,
                "present": 1,
                "verified": 1,
                "untested": 0,
                "missing": 0,
                "core_present": 1,
                "core_missing": 0,
                "core_unsourceable": 0,
                "details": [],
            }
        }
        profiles = {
            "testemu": {
                "emulator": "Test Emulator",
                "type": "libretro",
                "source": "https://github.com/example/emulator",
                "source_commit": "a" * 40,
                "cores": ["testemu"],
                "systems": ["test-system"],
                "files": [
                    {
                        "name": "fw.bin",
                        "system": "test-system",
                        "required": True,
                        "md5": ["5c124e7948ada85da603a522782940d0",
                                "4921d5f2165dee6e2496f4388c4c81b2"],
                        "source_ref": "src/firmware.cpp:10-12",
                    }
                ],
            }
        }
        stats = {
            "schema_version": 1,
            "generated_at": "2026-08-09T00:00:00Z",
            "files": 1,
            "size_bytes": len(payload),
            "platforms": 1,
            "emulators": 1,
            "systems": 1,
            "catalog_matched": 0,
            "source": "https://github.com/Abdess/retrobios",
            "downloads": "https://github.com/Abdess/retrobios/releases/tag/v2026.08.06",
            "composition": {
                "systems": {"files": 1, "size_bytes": len(payload)},
                "arcade": {"files": 0, "size_bytes": 0},
                "game_data": {"files": 0, "size_bytes": 0},
            },
        }
        return db, coverages, profiles, stats

    def test_source_references_are_revision_pinned_links(self):
        _, _, profiles, _ = self._fixtures()
        rendered = _source_ref_markdown(
            profiles["testemu"], "src/firmware.cpp:10-12"
        )
        self.assertIn("/blob/" + "a" * 40 + "/src/firmware.cpp#L10-L12", rendered)

    def test_ambiguous_two_repository_path_is_never_linked(self):
        """A path that could live in either declared repository stays plain.

        The libretro port and the original emulator are two different trees.
        Linking an unattributable path to whichever came first publishes a
        citation that resolves to nothing.
        """
        profile = {
            "source": "https://github.com/libretro/example-libretro",
            "source_commit": "a" * 40,
            "upstream": "https://gitlab.com/vendor/example",
            "upstream_commit": "b" * 40,
        }
        rendered = _source_ref_markdown(profile, "jg.c:213-214")
        self.assertEqual(rendered, "`jg.c:213-214`")
        self.assertNotIn("http", rendered)

        # A first segment equal to a repository's name proves nothing: PCSX2
        # keeps its sources in pcsx2/, hatariB vendors Hatari in hatari/.
        # Stripped, 470 links pointed at nothing or at another file.
        prefixed = _source_ref_markdown(profile, "example/jg.c:213-214")
        self.assertEqual(prefixed, "`example/jg.c:213-214`")

    def test_a_single_repository_links_the_path_as_written(self):
        profile = {"source": "https://github.com/PCSX2/pcsx2", "source_commit": "c" * 40}
        rendered = _source_ref_markdown(profile, "pcsx2/ImGui/FullscreenUI.cpp:755-759")
        self.assertIn("/blob/" + "c" * 40 + "/pcsx2/ImGui/FullscreenUI.cpp#L755-L759", rendered)

    def test_preprocessor_tokens_cannot_become_page_headings(self):
        rendered = _admonition_body("First paragraph.\n\n#if FEATURE\nEnabled")
        self.assertIn("\\#if FEATURE", rendered)
        self.assertNotIn("\n    #if FEATURE", rendered)

    def test_browser_titles_distinguish_emulators_from_system_pages(self):
        self.assertEqual(
            _browser_title(Path("emulators/qemu.md"), "QEMU"),
            "QEMU emulator firmware",
        )
        self.assertEqual(
            _browser_title(Path("systems/qemu.md"), "QEMU"),
            "QEMU systems",
        )

    ACCEPTED = (
        ("md5", "c87a52c277f7952b41c639fc7bf0a43b"),
        ("md5", "d0ed892a5e8d47e0cb5f8aa5f4dd7c5a"),
        ("crc32", "3f9f95a4"),
        ("md5", "4921d5f2165dee6e2496f4388c4c81b2"),
    )

    def test_exports_have_catalog_hashes_and_queryable_sqlite(self):
        db, coverages, profiles, stats = self._fixtures()
        exports = generate_data_exports(
            self.docs, db, coverages, profiles, stats
        )
        catalog = json.loads((self.docs / "api/v1/catalog.json").read_text())
        self.assertEqual(catalog["count"], len(exports))
        # Every reference resolves against the catalog's own URL, as an
        # RFC 3986 client resolves it: the url was relative to the site root
        # and the schema to api/v1/, so neither base resolved both.
        import urllib.parse  # noqa: PLC0415

        site = "https://example.org/site/"
        base = site + "api/v1/catalog.json"
        for item in catalog["items"]:
            for key in ("url", "schema"):
                if key in item:
                    target = urllib.parse.urljoin(base, item[key]).removeprefix(site)
                    self.assertTrue((self.docs / target).is_file(), (key, item[key]))
            path = self.docs / urllib.parse.urljoin(base, item["url"]).removeprefix(site)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])

        with sqlite3.connect(self.docs / "downloads/retrobios.sqlite") as connection:
            # One lowercase row per accepted value: a comma-joined Recalbox
            # md5, a JSON array of accepted revisions and an uppercase crc32
            # each missed an exact lookup.
            for algorithm, value in self.ACCEPTED:
                with self.subTest(algorithm=algorithm, value=value):
                    self.assertTrue(connection.execute(
                        "SELECT count(*) FROM accepted_hashes WHERE algorithm = ? AND value = ?",
                        (algorithm, value)).fetchone()[0])
            self.assertEqual(connection.execute("SELECT count(*) FROM files").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM platforms").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM emulators").fetchone()[0], 1)

    def test_page_metadata_is_specific_and_idempotent(self):
        page = self.docs / "gaps.md"
        page.write_text("# Gap Analysis - RetroBIOS\n\nCurrent unresolved entries.\n")
        decorate_markdown_pages(self.docs)
        decorate_markdown_pages(self.docs)
        content = page.read_text()
        self.assertEqual(content.count('type="application/ld+json"'), 1)
        self.assertIn("Current RetroBIOS verification gaps", content)
        self.assertIn('"@type":"TechArticle"', content)


class PerModeSourcePins(unittest.TestCase):
    """A profile whose builds live in separate repositories.

    `source`, `upstream` and `source_commit` may each be keyed by build mode.
    Reading the URL from one mode and the revision from another produces a
    permalink into a tree that never held that line, and binding the object
    form straight into SQLite fails outright.
    """

    PROFILE = {
        "emulator": "twobuilds",
        "source": {
            "standalone": "https://github.com/vendor/emu",
            "libretro": "https://github.com/porter/emu-libretro",
        },
        "upstream": "https://github.com/vendor/emu",
        "source_commit": {"standalone": "a" * 40, "libretro": "b" * 40},
    }

    def test_each_repository_keeps_its_own_revision(self):
        pairs = {
            (repo.slug, pin) for repo, pin in _forge_sources(self.PROFILE)
        }
        self.assertIn(("vendor/emu", "a" * 40), pairs)
        self.assertIn(("porter/emu-libretro", "b" * 40), pairs)
        # The libretro fork must never be pinned to the standalone revision.
        self.assertNotIn(("porter/emu-libretro", "a" * 40), pairs)
        self.assertNotIn(("vendor/emu", "b" * 40), pairs)

    def test_a_build_mode_label_is_resolved_first(self):
        repo, pin = _forge_sources(self.PROFILE, "libretro")[0]
        self.assertEqual((repo.slug, pin), ("porter/emu-libretro", "b" * 40))

    def test_the_plain_string_form_is_unchanged(self):
        profile = {
            "source": "https://github.com/vendor/emu",
            "source_commit": "c" * 40,
        }
        self.assertEqual(
            [(repo.slug, pin) for repo, pin in _forge_sources(profile)],
            [("vendor/emu", "c" * 40)],
        )

    def test_sqlite_export_accepts_a_per_mode_commit(self):
        from generate_site import _json_text

        self.assertEqual(
            _json_text(self.PROFILE["source_commit"]),
            '{"libretro": "' + "b" * 40 + '", "standalone": "' + "a" * 40 + '"}',
        )
        self.assertEqual(_json_text("c" * 40), "c" * 40)



class AnEmulatorPageNamesItsRevision(unittest.TestCase):
    """The page linked the repositories unpinned. The commit every citation
    refers to sat in the YAML alone, for 178 profiles."""

    def test_each_repository_links_its_pinned_commit(self):
        import generate_site  # noqa: PLC0415

        profile = {
            "emulator": "Caprice32", "type": "libretro",
            "source": "https://github.com/libretro/libretro-cap32",
            "upstream": "https://github.com/ColinPitrat/caprice32",
            "source_commit": "c" * 40, "upstream_commit": "6" * 40,
            "source_branch": "main", "systems": ["amstrad-cpc"], "files": [],
        }
        page = generate_site.generate_emulator_page("cap32", profile, {"files": {}})
        self.assertIn(
            "| Revision | [libretro/libretro-cap32@cccccccccccc]"
            "(https://github.com/libretro/libretro-cap32/commit/" + "c" * 40 + "), "
            "[ColinPitrat/caprice32@666666666666]"
            "(https://github.com/ColinPitrat/caprice32/commit/" + "6" * 40 + "), "
            "branch `main` |",
            page,
        )


class APlatformPageSaysWhereEachFileGoes(unittest.TestCase):
    """Three GameCube IPL.bin were listed by name alone: nothing told which
    goes to GC/USA, GC/EUR or GC/JAP."""

    def test_same_named_files_show_their_destinations(self):
        import generate_site  # noqa: PLC0415

        regions = ("USA", "EUR", "JAP")
        declared = [{"name": "IPL.bin", "destination": f"dolphin-emu/Sys/GC/{r}/IPL.bin",
                     "sha1": str(i) * 40} for i, r in enumerate(regions)]
        files = [{"name": "IPL.bin", "status": "ok"} for _ in regions]
        lines = generate_site._render_platform_systems(
            {"nintendo-gamecube": files}, {("nintendo-gamecube", "IPL.bin"): declared}, {})
        for region in regions:
            self.assertIn(f"- `IPL.bin` at `dolphin-emu/Sys/GC/{region}/IPL.bin` - OK",
                          "\n".join(lines))


class AVariantShowsWhoPinsIt(unittest.TestCase):
    """A .variants/ file showed a name and a truncated MD5: the advision.zip
    Batocera and RetroBat pin named no platform."""

    def test_the_variant_card_names_its_readers(self):
        import types  # noqa: PLC0415

        import generate_site  # noqa: PLC0415

        primary = {"name": "advision.zip", "path": "bios/Entex/Adventure Vision/advision.zip",
                   "sha1": "a" * 40, "md5": "b" * 32, "size": 3000}
        variant = {"name": "advision.zip",
                   "path": "bios/Entex/Adventure Vision/.variants/advision.zip.1c086064",
                   "sha1": "c" * 40, "md5": "1c086064" + "0" * 24, "size": 3000,
                   "provenance": {"tosec": {"name": "x"}}}
        consumers = types.SimpleNamespace(
            platforms={"c" * 40: {"batocera", "retrobat"}}, emulators={"a" * 40: {"mame"}})
        page = generate_site.generate_system_page(
            "Entex", {"Adventure Vision": [primary, variant]}, consumers)
        block = page[page.index('??? note "Variants (1)"'):]
        self.assertIn("    - Collection path: `bios/Entex/Adventure Vision/.variants/advision.zip.1c086064`", block)
        self.assertIn("[batocera](../platforms/batocera.md)", block)
        self.assertIn("[retrobat](../platforms/retrobat.md)", block)
        self.assertIn('`' + "c" * 40 + '`', block)
        self.assertIn("Catalogued", block)


class AFlaggedDumpIsNotVerified(unittest.TestCase):
    """scph5000.bin, which TOSEC names "...(JP)[b].bin", wore a green
    "Verified dump: TOSEC" badge. The catalog name lived only in a title
    attribute, outside the search and invisible on a touch screen."""

    def test_flags_and_name(self):
        from generate_site import _provenance_lines

        lines = _provenance_lines({
            "tosec": {"name": "Sony PlayStation BIOS (v2.2)(Sony)(JP)[b].bin"},
            "redump": {"name": "SCPH-5000 BIOS v2.2"},
        }, "../")
        tosec = next(l for l in lines if "TOSEC" in l)
        redump = next(l for l in lines if "Redump" in l)
        self.assertIn("rb-badge-warning", tosec)
        self.assertIn("bad dump", tosec)
        self.assertIn("(JP)[b].bin", tosec)
        self.assertIn("rb-badge-success", redump)
        self.assertIn("SCPH-5000 BIOS v2.2", redump)

    def test_an_alternate_is_not_a_flag(self):
        from generate_site import _dump_flags

        self.assertEqual(_dump_flags("tosec", {"name": "x (JP)[a].bin"}), [])
        self.assertEqual(_dump_flags("tosec", {"name": "x [o][b].bin"}), ["overdump", "bad dump"])



class EmulatorPagesPrintWholeHashes(unittest.TestCase):
    """Emulator pages cut every hash to 12 characters, the rest in a title
    attribute the search does not index: bstone's AUDIOHED.BS1 sha1 pasted
    whole found nothing."""

    def test_full_and_split(self):
        from generate_site import _hash_spans

        sha1 = "0c3de403b524107809fa9308b730d60e8e41ba93"
        self.assertIn(f"`{sha1}`", _hash_spans(sha1))
        both = _hash_spans(f"{'a' * 64}, {'b' * 64}")
        self.assertIn("`" + "a" * 64 + "`", both)
        self.assertIn("`" + "b" * 64 + "`", both)
        self.assertNotIn("...", both)



class TheGapsSayWhatToSupplyAndWhere(unittest.TestCase):
    """The Files to Source table, gaps.csv and gaps.json gave neither the
    expected hash nor the destination: EANIM.BS6's sha1 af5af59a... was on
    the emulator page only, and Data.rsdk's three rows looked identical."""

    def test_export_rows(self):
        from generate_site import _gap_export_rows

        report = {"bstone": {"emulator": "BStone", "systems": ["blake-stone"], "gap_details": [
            {"name": "EANIM.BS6", "path": "AOG/EANIM.BS6", "source": "missing",
             "required": False, "identity": "sha1:" + "a" * 40}]}}
        row = next(r for r in _gap_export_rows({}, report) if r["name"] == "EANIM.BS6")
        self.assertEqual(row["destination"], "AOG/EANIM.BS6")
        self.assertEqual(row["expected"], "sha1:" + "a" * 40)



class AGoneSourceIsSaid(unittest.TestCase):
    """citron, yuzu and suyu linked sources answering 451 or a domain now
    owned by someone else, and the page said nothing."""

    def test_the_status_row(self):
        import generate_site

        profile = {"emulator": "Citron", "type": "standalone", "systems": [],
                   "source": "https://git.citron-emu.org/citron-emu/Citron",
                   "upstream_gone": "git.citron-emu.org no longer resolves. Observed 2026-09-04.",
                   "source_mirror": "https://example.org/mirror/Citron", "files": []}
        page = generate_site.generate_emulator_page(
            "citron", profile, {"files": {}, "indexes": {}}, {})
        self.assertIn("| Source status | gone: git.citron-emu.org no longer resolves", page)
        self.assertIn("| Mirror | [https://example.org/mirror/Citron]", page)



class ASystemCardLandsOnThePlatformsSection(unittest.TestCase):
    """From scph5000.bin's card, [retrodeck] opened the top of a 2,000-file
    page, while the emulator card already landed on #sony-playstation."""

    def test_section_and_destination(self):
        import types

        from generate_site import _platform_links

        consumers = types.SimpleNamespace(
            platforms={"s": {"retrodeck"}},
            platform_systems={"s": {"retrodeck": {"sony-playstation"}}},
            platform_destinations={"s": {"retrodeck": {"bios/scph5000.bin"}}},
        )
        self.assertEqual(
            _platform_links("s", consumers),
            ["[retrodeck](../platforms/retrodeck.md#sony-playstation) `bios/scph5000.bin`"],
        )



class ArchiveMembersAreAcceptedHashes(unittest.TestCase):
    """accepted_hashes read only an entry's own hashes: 4,673 member crc32
    values of MAME and FBNeo sets were absent from the table the Data page
    calls the one to ask."""

    def test_member_rows(self):
        from generate_site import _accepted_hash_rows

        item = {"id": "fbalpha2019", "profile": {"files": [
            {"name": "nmk004.zip", "system": "arcade",
             "contents": [{"name": "nmk004.bin", "crc32": "83b6f611"}]}]}}
        rows = _accepted_hash_rows([], [item])
        self.assertIn(
            ("nmk004.bin", "nmk004.zip/nmk004.bin", "crc32", "83b6f611"),
            {(r["name"], r["path"], r["algorithm"], r["value"]) for r in rows},
        )



class PlatformPagesCarryTheirDeclarations(unittest.TestCase):
    """Platform pages were the only content pages without structured data:
    5,640 md5 and 2,047 sha1 the platforms declare lived in the prose only."""

    def test_parts_and_version(self):
        from generate_site import platform_structured_data

        data = platform_structured_data("batocera", {"platform": "Batocera", "config": {
            "version": "43.1", "systems": {"sony-playstation": {"files": [
                {"name": "scph5501.bin", "destination": "scph5501.bin", "md5": "490f666e"}]}}}})
        self.assertEqual(data["about"]["softwareVersion"], "43.1")
        part = data["hasPart"][0]
        self.assertEqual(part["identifier"], ["md5:490f666e"])
        self.assertTrue(part["url"].endswith("platforms/batocera/#sony-playstation"))

    def test_a_declared_sha256_is_published(self):
        """RetroDECK's dsifirmware.bin sha256 lived in the CSV exports only."""
        from generate_site import platform_structured_data

        data = platform_structured_data("retrodeck", {"platform": "RetroDECK", "config": {
            "systems": {"nds": {"files": [
                {"name": "dsifirmware.bin", "sha256": "11A150E3"}]}}}})
        self.assertEqual(data["hasPart"][0]["identifier"], ["sha256:11a150e3"])



class AnEmulatorPageLinksItsProfile(unittest.TestCase):
    """A page rendered a profile without linking it: reusing raze.yml meant
    guessing its path or filtering the 5.9 MB emulators.json."""

    def test_links(self):
        import generate_site

        page = generate_site.generate_emulator_page(
            "raze", {"emulator": "Raze", "type": "standalone", "systems": [], "files": []},
            {"files": {}, "indexes": {}}, {})
        self.assertIn("/blob/main/emulators/raze.yml", page)
        self.assertIn("(../api/v1/emulators/raze.json)", page)


class AVersionPerBuildReadsAsVersions(unittest.TestCase):
    """MesenCE's page showed {'standalone': '2.2.1', 'libretro': '2.0.0'}."""

    def test_each_build_is_named(self):
        from generate_site import generate_emulator_page  # noqa: PLC0415

        page = generate_emulator_page(
            "mesence",
            {"emulator": "MesenCE", "type": "standalone + libretro", "systems": [],
             "core_version": {"standalone": "2.2.1", "libretro": "2.0.0"}, "files": []},
            {"files": {}, "indexes": {}},
        )
        self.assertIn("2.2.1 (standalone), 2.0.0 (libretro)", page)
        self.assertNotIn("{'standalone'", page)


class EmulatorPartsCarryTheirIdentity(unittest.TestCase):
    """The emulator JSON-LD published 'sha256:a,b,c,d' as one identifier,
    gave archives none though their members carry crc32 and sha1, and gave
    no part the url of its card."""

    def test_identifiers_members_and_url(self):
        from generate_site import emulator_file_anchors, emulator_structured_data  # noqa: PLC0415

        files = [
            {"name": "BS-X.bin", "sha256": "aa,bb"},
            {"name": "neogeo.zip", "contents": [
                {"name": "sp-s2.sp1", "crc32": "9036D879", "sha1": "ff" * 20}]},
        ]
        data = emulator_structured_data({"files": files})
        bsx, neogeo = data["hasPart"]
        self.assertEqual(bsx["identifier"], ["sha256:aa", "sha256:bb"])
        self.assertEqual(
            neogeo["hasPart"][0]["identifier"], ["sha1:" + "ff" * 20, "crc32:9036d879"]
        )
        self.assertEqual(
            [bsx["url"], neogeo["url"]], [f"#{a}" for a in emulator_file_anchors(files)]
        )


class OnlyAPinnedProfileClaimsAPin(unittest.TestCase):
    """Redream, with no revision at all, was described as source-pinned."""

    def test_the_description_follows_the_pin(self):
        from unittest import mock  # noqa: PLC0415

        import generate_site  # noqa: PLC0415

        pages = {
            "emulators/pinned.md": {"about": {"version": "a" * 40}},
            "emulators/redream.md": {},
        }
        with mock.patch.dict(generate_site.PAGE_DATA, pages, clear=True):
            pinned = generate_site._page_description("", Path("emulators/pinned.md"), "P")
            loose = generate_site._page_description("", Path("emulators/redream.md"), "R")
        self.assertIn("source-pinned", pinned)
        self.assertNotIn("source-pinned", loose)


class ADataDirectorySaysWhatItIs(unittest.TestCase):
    """The page said `dolphin-sys` > `Sys` and nothing of the tree it packs."""

    def test_description_origin_mode_and_source(self):
        from generate_site import generate_emulator_page  # noqa: PLC0415

        profile = {
            "emulator": "Cemu", "type": "standalone + libretro", "systems": [],
            "source": "https://github.com/cemu-project/Cemu", "source_commit": "c" * 40,
            "files": [{"name": "keys.txt"}],
            "data_directories": [{"ref": "packs", "destination": "graphicPacks",
                                  "mode": "standalone", "source_ref": "src/main.cpp:10"}],
        }
        registry = {"packs": {"description": "Community graphic packs",
                              "source_url": "https://example.test/packs.zip"}}
        page = generate_emulator_page("cemu", profile, {"files": {}, "indexes": {}},
                                      None, set(), registry)
        self.assertIn("`packs` > `graphicPacks`: Community graphic packs", page)
        self.assertIn("<https://example.test/packs.zip>", page)
        self.assertIn("standalone build only", page)
        self.assertIn("src/main.cpp#L10", page)


class APlatformPageNamesTheFileItTranscribes(unittest.TestCase):
    """Batocera's page gave 'Version 43.1' and a homepage, never the
    batocera-systems revision every md5 on it comes from."""

    def test_pinned_and_moving_sources(self):
        import generate_site  # noqa: PLC0415

        def page(source: str) -> str:
            cov = {"platform": "P", "config": {"source": source, "systems": {}},
                   "details": [], "total": 0, "verified": 0, "present": 0,
                   "missing": 0, "untested": 0, "mode": "md5"}
            return generate_site.generate_platform_page("p", cov)

        tag = "https://raw.githubusercontent.com/o/r/batocera-43.1/x/batocera-systems"
        self.assertIn(f"| Transcribed from | <{tag}> |", page(tag))
        branch = "https://raw.githubusercontent.com/o/r/master/x.json"
        self.assertIn("revision read is not recorded", page(branch))
        self.assertIn("revision read is not recorded", page("https://github.com/RetroDECK/components"))


class APerCoreDocumentValidates(unittest.TestCase):
    """api/v1/emulators/<id>.json had no schema_version, and its "id" beside
    the profile's fields failed the schema published next to it."""

    def test_shape(self):
        try:
            import jsonschema  # noqa: PLC0415
        except ImportError:
            self.skipTest("jsonschema is not installed")
        import yaml  # noqa: PLC0415

        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "generate_site.py").read_text(encoding="utf-8")
        self.assertNotIn('json.dumps({"id": name, **profile}', source)
        envelope = json.loads((root / "schemas" / "site-api-envelope.schema.json").read_text())
        schema = json.loads((root / "schemas" / "emulator.schema.json").read_text())
        profile = yaml.safe_load((root / "emulators" / "handy.yml").read_text())
        document = {"schema_version": 1, "generated_at": "2026-10-19T00:00:00Z",
                    "kind": "emulators", "count": 1,
                    "items": [{"id": "handy", "profile": profile}]}
        jsonschema.validate(document, envelope)
        jsonschema.validate(document["items"][0]["profile"], schema)


class AFileHasAnAddressByContent(unittest.TestCase):
    """mt32_control.rom moved from the Commodore page to the Roland page and
    every link to its card broke: the anchor was content-based, the page
    was not."""

    def test_index_page_and_resolver(self):
        import generate_site  # noqa: PLC0415

        db = {"files": {
            "ab" * 20: {"path": "bios/Roland/mt32-roms/mt32_control.rom"},
            "cd" * 20: {"path": "data/elsewhere.bin"},
        }}
        self.assertEqual(
            generate_site.file_location_items(db),
            [{"sha1": "ab" * 20, "url": "systems/roland/#sha1-" + "ab" * 6}],
        )
        self.assertIn('id="rb-file-lookup-status"', generate_site.generate_files_page())
        script = (Path(__file__).resolve().parents[1] / "docs_assets" / "site.js").read_text()
        self.assertIn("file-locations.json", script)
        from cross_reference import FileConsumers  # noqa: PLC0415

        card = "\n".join(generate_site._system_file_card(
            {"name": "x.rom", "sha1": "ab" * 20, "size": 1}, FileConsumers({"files": {}}),
        ))
        self.assertIn("../../files/?sha1=" + "ab" * 20, card)


class ACardNamesThePacksThatInstallIt(unittest.TestCase):
    """A file card named only the platforms whose own list declares the
    file: a RetroBat user found no RetroBat on scph5501.bin, which its pack
    installs for six cores."""

    def test_a_manifest_destination_reaches_the_card(self):
        import json as _json

        from cross_reference import FileConsumers
        from generate_site import _installed_links

        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            Path(tmp, "retrobat.json").write_text(_json.dumps({
                "platform": "retrobat",
                "files": [{"dest": "scph5501.bin", "sha1": "b" * 40,
                           "alternatives": [{"sha1": "a" * 40}]}],
            }))
            consumers = FileConsumers({"files": {}})
            consumers.add_manifests(tmp)
        links = _installed_links("a" * 40, consumers, set())
        self.assertEqual(links, ["[retrobat](../platforms/retrobat.md) `scph5501.bin`"])
        self.assertEqual(_installed_links("a" * 40, consumers, {"retrobat"}), [])


if __name__ == "__main__":
    unittest.main()
