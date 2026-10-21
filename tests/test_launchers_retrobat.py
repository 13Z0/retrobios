"""RetroBat's launcher layer read from emulatorLauncher's generators.

Each fixture is a cut-down copy of the construct at emulatorLauncher
48cdc61623c5943bb17647b80a60d32d395c3725, kept to the lines an extractor
reads. The records must carry the names in the launcher's order and point at
the fixture's own lines, and a construct that is gone must stop the scrape.
"""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scraper import retrobat_launchers as rl  # noqa: E402
from scraper.retrobat_scraper import launcher_revision  # noqa: E402

G = rl.GENERATORS


def _sources(**texts: str) -> dict[str, rl.Source]:
    return {
        path: rl.Source(path, textwrap.dedent(text).lstrip("\n")) for path, text in texts.items()
    }


ARES = """
    if (system == "laseractive" && core.ToLowerInvariant() == "megald")
    {
        var sys = bml.GetOrCreateContainer("LaserActiveSEGAPAC");
        var firmware = sys.GetOrCreateContainer("Firmware");
        string biosfolderPioneer = Path.Combine(AppConfig.GetFullPath("bios"), "laseractive");

        string bios_japan = Path.Combine(biosfolderPioneer, "[BIOS] LaserActive PAC-S1 (Japan) (v1.05).bin");
        if (!File.Exists(bios_japan))
            bios_japan = Path.Combine(biosfolderPioneer, "[BIOS] LaserActive PAC-S1 (Japan) (v1.02).bin");
        if (!File.Exists(bios_japan))
            bios_japan = Path.Combine(biosfolderPioneer, "[BIOS] LaserActive PAC-S1 (Japan) (v1.01).bin");

        if (File.Exists(bios_japan))
            firmware["BIOS.Japan"] = bios_japan.Replace("\\\\", "/");
    }
"""

MESEN = """
    private void ConfigureColeco(JObject section, string mesenSystem, string system, string path)
    {
        string targetFirmwarePath = Path.Combine(path, "Firmware");
        string targetFirmware1 = Path.Combine(targetFirmwarePath, "bios.col");

        string sourceFirmware1 = Path.Combine(AppConfig.GetFullPath("bios"), "colecovision.rom");

        if (File.Exists(sourceFirmware1) && !File.Exists(targetFirmware1) && Directory.Exists(targetFirmwarePath))
            File.Copy(sourceFirmware1, targetFirmware1);

        if (!File.Exists(targetFirmware1))
            throw new ApplicationException("Colecovision firmware is missing (colecovision.rom)");
    }
"""

AMIBERRY = """
    private static readonly Dictionary<string, AmigaModel> _models = new Dictionary<string, AmigaModel>()
    {
        { "CD32", new AmigaModel {
            Id = "CD32",
            Kickstart = new[] { "kick40060.CD32", "kick40060.CD32.rom" },
            KickstartExt = new[] { "kick40060.CD32.ext", "amiga-os-310-cd32-ext.rom" } } },

        { "CD32FR", new AmigaModel {
            Id = "CD32FR",
            Kickstart = new[] { "kick40060.CD32", "kick40060.CD32.rom" } } },
    };

    void Configure()
    {
        string kickstart = StageKickstart(_model.Kickstart);
        if (kickstart == null)
            SimpleLogger.Instance.Warning("[Amiberry] No Kickstart ROM found for " + _model.Id);
        string kickstartExt = StageKickstart(_model.KickstartExt);
        if (kickstartExt == null)
            SimpleLogger.Instance.Warning("[Amiberry] No extended ROM found for " + _model.Id);
    }

    private string FindBios(string[] candidates)
    {
        var searchDirs = new List<string> { biosPath, Path.Combine(biosPath, "amiga"), Path.Combine(biosPath, "Amiga") };
        return null;
    }
"""

MEDNAFEN = """
    if (!SystemConfig.getOptBoolean("mednafen_psx_original_bios"))
    {
        string biosFile = Path.Combine(AppConfig.GetFullPath("bios"), "psxonpsp660.bin");
        if (File.Exists(biosFile))
        {
            cfg["psx.bios_eu"] = "psxonpsp660.bin";
        }
        else if (File.Exists(Path.Combine(AppConfig.GetFullPath("bios"), "ps1_rom.bin")))
        {
            cfg["psx.bios_eu"] = "ps1_rom.bin";
        }
        else
        {
            cfg["psx.bios_eu"] = "scph5502.bin";
            cfg["psx.bios_jp"] = "scph5500.bin";
        }
    }
    else
    {
        cfg["psx.bios_eu"] = "scph5502.bin";
    }
"""

LIBRETRO_PSX = """
    if (core != "mednafen_psx_hw")
        return;
    if (!SystemConfig.getOptBoolean("beetle_psx_original_bios"))
    {
        string biosFile = Path.Combine(AppConfig.GetFullPath("bios"), "psxonpsp660.bin");
        if (File.Exists(biosFile))
            coreSettings["beetle_psx_hw_override_bios"] = "psxonpsp";
        else
            coreSettings["beetle_psx_hw_override_bios"] = "disabled";
    }
    else
        coreSettings["beetle_psx_hw_override_bios"] = "disabled";
"""

OPENMSX = """
    foreach (var c in biosFileRequired)
    {
        if (!systemRomsContent.Contains(c))
            throw new ApplicationException(machine + " machine has missing BIOS file '" + c + "' in 'bios\\\\openmsx\\\\share\\\\systemroms' folder.");
    }

    static readonly Dictionary<string, string[]> biosFiles = new Dictionary<string, string[]>()
    {
        { "Philips_VG_8020", new string[] { "vg8020_basic-bios1.rom" } },
        { "Pioneer_PX-7", new string[] { "px-7_basic-bios1.rom", "px-7_pbasic.rom" } }
    };
"""

DUCKSTATION = """
    ini.WriteValue("BIOS", "SearchDirectory", biosPath);

    if (SystemConfig.isOptSet("duck_bios") && !string.IsNullOrEmpty(SystemConfig["duck_bios"]))
    {
        ini.WriteValue("BIOS", "PathNTSCJ", SystemConfig["duck_bios"]);
        ini.WriteValue("BIOS", "PathPAL", SystemConfig["duck_bios"]);
    }
    else
    {
        ini.WriteValue("BIOS", "PathNTSCJ", null);
    }
"""

FEATURES = """
    <features>
      <emulator name="libretro, angle">
        <cores>
          <core name="duckstation">
            <feature value="duck_bios">
              <choice name="WRONG" value="libretro.bin"/>
            </feature>
          </core>
        </cores>
      </emulator>
      <emulator name="duckstation" features="cheevos">
        <feature submenu="EMULATION" name="FORCE BIOS" value="duck_bios" order="102">
          <choice name="PSX ON PSP BIOS" value="psxonpsp660.bin"/>
          <choice name="PS1 US (SCPH5501)" value="scph5501.bin"/>
        </feature>
      </emulator>
    </features>
"""

HATARI = """
    string tosfile = Path.Combine(AppConfig.GetFullPath("bios"), "tos.img");

    if (SystemConfig.isOptSet("hatari_tos") && !string.IsNullOrEmpty(SystemConfig["hatari_tos"]))
        tosfile = Path.Combine(AppConfig.GetFullPath("bios"), "hatari", "tos", SystemConfig["hatari_tos"]);

    if (!File.Exists(tosfile))
        throw new ApplicationException("Missing TOS file " + tosName + " in 'bios' folder.");

    ini.WriteValue("ROM", "szTosImageFileName", tosfile);
"""

HATARI_FEATURES = """
    <emulator name="hatari" features="padtokeyboard">
      <feature name="TOS" value="hatari_tos" order="101">
        <choice name="TOS 2.06" value="tos206.img"/>
        <choice name="TOS 1.02" value="tos102.img"/>
      </feature>
    </emulator>
"""


KEGA = """
    private void SetupConfiguration(string path, string system)
    {
        string iniFile = Path.Combine(path, "Fusion.ini");

        using (var ini = IniFile.FromFile(iniFile))
        {
            string biosPath = AppConfig.GetFullPath("bios");
            if (!Directory.Exists(biosPath)) try { Directory.CreateDirectory(biosPath); }
                catch { }

            ini.WriteValue("", "GenesisBIOS", biosPath);

            // BIOS
            string euBios = Path.Combine(biosPath, "bios_CD_E.bin");
            if (File.Exists(euBios))
                ini.WriteValue("", "SCDEURBIOS", euBios);

            string bios32Xg = Path.Combine(biosPath, "32X_G_BIOS.BIN");
            if (File.Exists(bios32Xg))
                ini.WriteValue("", "32XM68KBIOS", bios32Xg);
        }
    }
"""

JGENESIS = """
    private void SetupTomlConfiguration(string path)
    {
        string settingsFile = Path.Combine(path, "jgenesis-config.toml");
        using (IniFileJGenesis ini = new IniFileJGenesis(settingsFile, IniOptionsJGenesis.UseSpaces))
        {
            ConfigureGenesis(ini, system);
        }
    }

    private void ConfigureGenesis(IniFileJGenesis ini, string system)
    {
        if (system != "genesis" && system != "sega_cd")
            return;

        if (system == "sega_cd")
        {
            string segaCdBios = Path.Combine(AppConfig.GetFullPath("bios"), "bios_CD_U.bin");

            ini.WriteValue("sega_cd", "per_region_bios", "true");

            if (File.Exists(segaCdBios))
                ini.WriteValue("sega_cd", "bios_path", "'" + segaCdBios + "'");
        }
    }
"""

MELONDS = """
    private void SetupConfiguration(string path, string rom)
    {
        string settingsFile = Path.Combine(path, "melonDS.toml");

        using (IniTomlFile ini = IniTomlFile.FromFile(settingsFile, IniTomlOptions.UseSpaces))
        {
            if (SystemConfig.isOptSet("melonds_externalBIOS") && SystemConfig.getOptBoolean("melonds_externalBIOS"))
            {
                string bios7 = Path.Combine(AppConfig.GetFullPath("bios"), "bios7.bin");

                if (File.Exists(bios7))
                    ini.WriteValue("DS", "BIOS7Path", "\\"" + bios7.Replace("\\\\", "/") + "\\"");
            }
        }
    }
"""

# Kronos: the path is replaced by an option before the check, so the name
# checked is not the literal.
REASSIGNED = """
    private void SetupConfiguration(string path)
    {
        string conf = Path.Combine(path, "kronos.ini");
        using (var ini = IniFile.FromFile(conf))
        {
            string bios = Path.Combine(AppConfig.GetFullPath("bios"), "saturn_bios.bin");
            if (SystemConfig.isOptSet("kronos_force_bios"))
                bios = Path.Combine(AppConfig.GetFullPath("bios"), SystemConfig["kronos_force_bios"]);
            if (File.Exists(bios))
                ini.WriteValue("1.0", "General\\\\Bios", bios.Replace("\\\\", "/"));
        }
    }
"""


def _psx_sources() -> dict[str, rl.Source]:
    return _sources(
        **{
            f"{G}/Mednafen.Generator.cs": MEDNAFEN,
            f"{G}/Bizhawk.Generator.cs": MEDNAFEN.replace("mednafen_psx", "bizhawk_psx"),
            f"{G}/LibRetro.CoreOptions.cs": LIBRETRO_PSX,
        }
    )


class NamesInLauncherOrder(unittest.TestCase):
    def test_ares_fallback_chain_keeps_its_order(self):
        [record] = rl.ares(_sources(**{f"{G}/Ares.Generator.cs": ARES}))
        self.assertEqual(
            record["names"],
            [
                "[BIOS] LaserActive PAC-S1 (Japan) (v1.05).bin",
                "[BIOS] LaserActive PAC-S1 (Japan) (v1.02).bin",
                "[BIOS] LaserActive PAC-S1 (Japan) (v1.01).bin",
            ],
        )
        self.assertEqual(record["kind"], "names_searched")
        self.assertEqual(record["search_dir"], "bios/laseractive")
        self.assertEqual(
            record["passes_to"], "settings.bml LaserActiveSEGAPAC/Firmware/BIOS.Japan (first found)"
        )
        self.assertEqual(record["source_ref"], f"{G}/Ares.Generator.cs:7-14")

    def test_mesen_copy_names_source_and_renamed_target(self):
        [record] = rl.mesen(_sources(**{f"{G}/Mesen.Generator.cs": MESEN}))
        self.assertEqual(record["names"], ["colecovision.rom"])
        self.assertEqual(record["kind"], "copies")
        self.assertIn("<mesen>/Firmware/bios.col", record["passes_to"])
        self.assertEqual(
            record["fails_when_missing"],
            "ApplicationException 'Colecovision firmware is missing (colecovision.rom)'",
        )
        self.assertEqual(record["source_ref"], f"{G}/Mesen.Generator.cs:1-13")

    def test_amiberry_identical_lists_are_one_record(self):
        records = rl.amiberry(_sources(**{f"{G}/Amiberry.Generator.cs": AMIBERRY}))
        self.assertEqual(
            [r["names"] for r in records],
            [
                ["kick40060.CD32", "kick40060.CD32.rom"],
                ["kick40060.CD32.ext", "amiga-os-310-cd32-ext.rom"],
            ],
        )
        self.assertEqual(records[0]["search_dir"], "bios, bios/amiga, bios/Amiga")
        self.assertEqual(
            records[0]["source_ref"], f"{G}/Amiberry.Generator.cs:3-5, 8-10, 15-17, 23-27"
        )
        self.assertEqual(
            records[1]["passes_to"],
            "copied to <amiberry roms>/kick40060.CD32.ext, kickstart_ext_rom_file",
        )

    def test_psx_cascade_splits_searched_from_fallback(self):
        records = rl.psx_cascades(_psx_sources())
        searched, fallback = [r for r in records if r["emulator"] == "mednafen"]
        self.assertEqual(searched["names"], ["psxonpsp660.bin", "ps1_rom.bin"])
        self.assertEqual(searched["depends_on_option"], "mednafen_psx_original_bios off")
        self.assertEqual(fallback["names"], ["scph5502.bin", "scph5500.bin"])
        self.assertEqual(fallback["kind"], "fixed_name")
        self.assertEqual(fallback["fails_when_missing"], "names written without an existence check")
        self.assertEqual(searched["source_ref"], f"{G}/Mednafen.Generator.cs:1-21")

    def test_libretro_cascade_takes_the_core_from_its_guard(self):
        records = rl.psx_cascades(_psx_sources())
        [beetle] = [r for r in records if r["build"] == "libretro"]
        self.assertEqual(beetle["emulator"], "mednafen_psx_hw")
        self.assertEqual(beetle["names"], ["psxonpsp660.bin"])
        self.assertEqual(beetle["source_ref"], f"{G}/LibRetro.CoreOptions.cs:3-12")

    def test_openmsx_one_record_per_machine(self):
        records = rl.openmsx(_sources(**{f"{G}/OpenMSX.Generator.cs": OPENMSX}))
        self.assertEqual(
            [(r["passes_to"], r["names"]) for r in records],
            [
                ("-machine Philips_VG_8020", ["vg8020_basic-bios1.rom"]),
                ("-machine Pioneer_PX-7", ["px-7_basic-bios1.rom", "px-7_pbasic.rom"]),
            ],
        )
        self.assertEqual(records[0]["search_dir"], "bios/openmsx/share/systemroms")
        self.assertFalse(records[0]["case_insensitive"])
        self.assertEqual(records[1]["source_ref"], f"{G}/OpenMSX.Generator.cs:1-4, 10")

    def test_duckstation_reads_the_standalone_feature_not_the_core(self):
        search, option = rl.duckstation(
            _sources(**{f"{G}/Duckstation.Generator.cs": DUCKSTATION, rl.FEATURES: FEATURES})
        )
        self.assertEqual(search["kind"], "passes_path")
        self.assertEqual(search["names"], [])
        self.assertEqual(option["names"], ["psxonpsp660.bin", "scph5501.bin"])
        self.assertEqual(option["passes_to"], "settings.ini [BIOS] PathNTSCJ = PathPAL = <value>")
        self.assertEqual(
            option["source_ref"], f"{G}/Duckstation.Generator.cs:3-7, {rl.FEATURES}:12-15"
        )

    def test_hatari_default_and_option(self):
        fixed, option = rl.hatari(
            _sources(**{f"{G}/Hatari.Generator.cs": HATARI, rl.FEATURES: HATARI_FEATURES})
        )
        self.assertEqual(fixed["names"], ["tos.img"])
        self.assertEqual(
            fixed["fails_when_missing"],
            "ApplicationException 'Missing TOS file <tosName> in 'bios' folder.'",
        )
        self.assertEqual(fixed["source_ref"], f"{G}/Hatari.Generator.cs:1-9")
        self.assertEqual(option["names"], ["tos206.img", "tos102.img"])
        self.assertEqual(option["search_dir"], "bios/hatari/tos")


def _one(text: str, emulator: str, path: str = f"{G}/X.Generator.cs") -> list[dict]:
    return rl.guarded_writes_in(_sources(**{path: text})[path], emulator)


class GuardedConfigWrites(unittest.TestCase):
    def test_kega_fusion_one_record_per_key(self):
        records = _one(KEGA, "kega-fusion")
        fixed = [r for r in records if r["kind"] == "fixed_name"]
        self.assertEqual(
            [(r["names"], r["passes_to"], r["fails_when_missing"]) for r in fixed],
            [
                (["bios_CD_E.bin"], "Fusion.ini SCDEURBIOS, only if the file exists",
                 "SCDEURBIOS not written"),
                (["32X_G_BIOS.BIN"], "Fusion.ini 32XM68KBIOS, only if the file exists",
                 "32XM68KBIOS not written"),
            ],
        )
        self.assertEqual(fixed[0]["source_ref"], f"{G}/X.Generator.cs:14-16")
        self.assertTrue(all(r["search_dir"] == "bios" and r["case_insensitive"] for r in fixed))
        self.assertNotIn("depends_on_option", fixed[0])

    def test_kega_fusion_genesis_key_receives_the_folder(self):
        (folder,) = [r for r in _one(KEGA, "kega-fusion") if r["kind"] == "passes_path"]
        self.assertEqual(folder["names"], [])
        self.assertEqual(folder["passes_to"], "Fusion.ini GenesisBIOS = bios")
        self.assertEqual(folder["source_ref"], f"{G}/X.Generator.cs:7, 11")

    def test_early_return_and_enclosing_if_are_conditions(self):
        (record,) = _one(JGENESIS, "jgenesis")
        self.assertEqual(
            record["depends_on_option"],
            'system == "genesis" || system == "sega_cd", system == "sega_cd"',
        )
        self.assertEqual(
            record["passes_to"], "jgenesis-config.toml [sega_cd] bios_path, only if the file exists"
        )
        self.assertEqual(record["source_ref"], f"{G}/X.Generator.cs:12-13, 17, 21-22")

    def test_option_switch_is_named(self):
        (record,) = _one(MELONDS, "melonds")
        self.assertEqual(record["names"], ["bios7.bin"])
        self.assertEqual(record["depends_on_option"], "melonds_externalBIOS on")
        self.assertEqual(
            record["passes_to"], "melonDS.toml [DS] BIOS7Path, only if the file exists"
        )

    def test_reassigned_path_is_not_a_fixed_name(self):
        with self.assertRaises(rl.LauncherPatternMissing):
            _one(REASSIGNED, "kronos")

    def test_write_inside_a_loop_is_not_read(self):
        looped = KEGA.replace("// BIOS", "foreach (var x in xs)\n            {").replace(
            '"32XM68KBIOS", bios32Xg);', '"32XM68KBIOS", bios32Xg);\n            }'
        )
        with self.assertRaises(rl.LauncherPatternMissing):
            _one(looped, "kega-fusion")

    def test_pinned_kega_fusion_generator_is_read(self):
        self.assertIn(f"{G}/KegaFusion.Generator.cs", rl.SOURCE_FILES)
        self.assertEqual(rl.GUARDED_WRITES[f"{G}/KegaFusion.Generator.cs"], "kega-fusion")


class AbsentConstructStopsTheScrape(unittest.TestCase):
    def test_each_extractor_raises_on_an_empty_source(self):
        empty = {path: rl.Source(path, "") for path in rl.SOURCE_FILES}
        for extractor in rl.EXTRACTORS:
            with self.subTest(extractor=extractor.__name__):
                with self.assertRaises(rl.LauncherPatternMissing):
                    extractor(empty)

    def test_chain_without_fallback_is_not_a_search(self):
        single = ARES.split("        if (!File.Exists(bios_japan))")[0] + "    }\n"
        with self.assertRaises(rl.LauncherPatternMissing):
            rl.ares(_sources(**{f"{G}/Ares.Generator.cs": single}))

    def test_missing_source_file_is_named(self):
        with self.assertRaises(rl.LauncherPatternMissing) as caught:
            rl.extract_launchers({})
        self.assertIn(rl.SOURCE_FILES[0], str(caught.exception))

    def test_the_error_is_a_scrape_error(self):
        self.assertTrue(issubclass(rl.LauncherPatternMissing, ValueError))


class Helpers(unittest.TestCase):
    def test_concatenation_renders_operands(self):
        self.assertEqual(
            rl.render_concat('"BIOS file " + name + " does not exist in \'" + dir + "\'."'),
            "BIOS file <name> does not exist in '<dir>'.",
        )

    def test_braces_inside_strings_do_not_close_blocks(self):
        src = rl.Source("x.cs", 'void f()\n{\n    s = "}";\n}\n')
        self.assertEqual(src.lines(*src.block_at(0)), (2, 4))

    def test_revision_comes_from_the_pinned_url(self):
        url = (
            "https://raw.githubusercontent.com/RetroBat-Official/emulatorlauncher/"
            "48cdc61623c5943bb17647b80a60d32d395c3725/batocera-systems/Resources/batocera-systems.json"
        )
        self.assertEqual(launcher_revision(url), "48cdc61623c5943bb17647b80a60d32d395c3725")
        with self.assertRaises(ValueError):
            launcher_revision("https://example.test/master/batocera-systems.json")


if __name__ == "__main__":
    unittest.main()
