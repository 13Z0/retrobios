"""Regression tests for the holistic reliability and security audit."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

import yaml

try:
    import jsonschema  # noqa: F401
except ImportError:
    HAS_JSONSCHEMA = False
else:
    HAS_JSONSCHEMA = True

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = ROOT / "tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import install
from scripts import pipeline, region, region_audit
from scripts.common import resolve_local_file, safe_extract_zip
from scripts.generate_pack import (
    _emulator_region_group,
    generate_target_manifests,
    verify_pack_against_platform,
)


class ReadmeRegressions(unittest.TestCase):
    """The README advertises commands; these must stay executable.

    Wording is the maintainer's, so nothing here asserts prose. What is
    asserted is that every flag and URL the README hands a reader is one the
    shipped scripts actually accept.
    """

    def _quick_install(self) -> str:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        return readme.split("## Quick Install", 1)[1].split(
            "## Download BIOS packs", 1
        )[0]

    def test_advertised_bootstrap_urls_are_the_shipped_ones(self):
        quick_install = self._quick_install()
        for script in ("install.sh", "install.ps1"):
            url = f"https://raw.githubusercontent.com/Abdess/retrobios/main/{script}"
            self.assertIn(url, quick_install, f"{script} bootstrap URL missing")
        self.assertIn(
            "https://raw.githubusercontent.com/Abdess/retrobios/main/install.py",
            (ROOT / "install.sh").read_text(encoding="utf-8"),
            "install.sh must fetch install.py from the ref the README advertises",
        )

    def test_advertised_installer_flags_exist(self):
        quick_install = self._quick_install()
        advertised = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", quick_install))
        parser_source = (ROOT / "install.py").read_text(encoding="utf-8")
        supported = set(re.findall(r'add_argument\(\s*"(--[a-z][a-z-]+)"', parser_source))
        unknown = advertised - supported
        self.assertEqual(unknown, set(), f"README advertises unknown flags: {unknown}")

    def test_the_build_version_is_the_one_the_pipeline_requires(self):
        """"Python 3 + pyyaml" sent a 3.8 user to a SyntaxError in
        generate_pack, and a 3.10 one through six steps to fail at step 7."""
        floor = re.search(
            r"sys\.version_info < \((\d+), (\d+)\)",
            (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(floor, "pipeline.py checks no interpreter version")
        version = f"Python {floor.group(1)}.{floor.group(2)}"
        for page in ("README.md", "wiki/tools.md", "wiki/testing-guide.md"):
            with self.subTest(page=page):
                self.assertIn(version, (ROOT / page).read_text(encoding="utf-8"))
        self.assertIn(
            version, (ROOT / "scripts" / "generate_readme.py").read_text(encoding="utf-8")
        )

    def test_an_older_interpreter_is_refused_before_the_first_step(self):
        import shutil  # noqa: PLC0415

        python38 = shutil.which("python3.8")
        if python38 is None:
            if os.environ.get("CI"):
                self.fail("CI must provide python3.8")
            self.skipTest("no python3.8 on PATH")
        # --help: without the guard the run must stop at argparse, not
        # rebuild database.json under an interpreter missing pyyaml.
        proc = subprocess.run(
            [python38, "-I", "scripts/pipeline.py", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("needs Python 3.12", proc.stderr)


class ReleaseSigningRegressions(unittest.TestCase):
    """A checksum list published beside its own artifacts proves nothing.

    SHA256SUMS.txt answers corruption; whoever can rewrite a release rewrites
    the list with it. The signature is what a third party checks, and it is
    only checkable while allowed_signers, the signing step and the upload
    list stay in agreement.
    """

    PRINCIPAL = "releases@retrobios"

    def test_allowed_signers_names_one_usable_key(self):
        path = ROOT / "allowed_signers"
        self.assertTrue(path.exists(), "allowed_signers is the published trust root")
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(lines, "allowed_signers carries no key")
        for line in lines:
            principal, keytype, blob = line.split()[:3]
            self.assertEqual(principal, self.PRINCIPAL)
            self.assertTrue(keytype.startswith(("ssh-", "sk-", "ecdsa-")), keytype)
            self.assertTrue(len(blob) > 40, "key material looks truncated")

    def test_the_release_signs_the_list_and_ships_the_signature(self):
        process = (ROOT / "wiki" / "release-process.md").read_text(encoding="utf-8")
        self.assertIn(
            "ssh-keygen -Y sign", process, "the release no longer signs the list"
        )
        upload = next(
            line for line in process.splitlines()
            if line.startswith("for f in dist/SHA256SUMS.txt")
        )
        self.assertIn(
            "dist/SHA256SUMS.txt.sig", upload,
            "the signature is produced but never uploaded",
        )

    def test_the_documented_verification_matches_the_published_principal(self):
        process = (ROOT / "wiki" / "release-process.md").read_text(encoding="utf-8")
        self.assertIn("ssh-keygen -Y verify", process)
        self.assertIn(f"-I {self.PRINCIPAL}", process)
        self.assertIn("allowed_signers", process)


class WorkflowRegressions(unittest.TestCase):
    """The test suite must sit on every road into main.

    run-tests lived in a pull_request-only workflow while the work landed by
    direct push, so 1,318 cases guarded a road almost nothing took. What is
    asserted here is reachability, not the workflow's shape.
    """

    @staticmethod
    def _workflow(name: str) -> dict:
        with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as f:
            document = yaml.safe_load(f)
        # PyYAML resolves the bare `on:` key to the boolean True.
        document["on"] = document.pop(True, document.get("on"))
        return document

    def test_no_validation_step_discards_its_exit_code(self):
        """A check whose result is thrown away is not a check.

        The BIOS validation step ended in `|| true`, so validate_pr.py could
        exit 1 on a file that failed its hash and the job stayed green. The
        report still has to reach the pull request, so the code is recorded
        and acted on afterwards rather than swallowed. A best-effort side
        action such as adding a label is not a check and keeps its `|| true`.
        """
        checks = ("python scripts/", "unittest", "mkdocs build")
        for name in ("validate.yml", "deploy-site.yml"):
            workflow = self._workflow(name)
            for job_name, job in workflow["jobs"].items():
                for step in job.get("steps", []):
                    # A shell continuation puts the command and its `|| true`
                    # on different lines, so they are rejoined before scanning.
                    body = str(step.get("run", "")).replace("\\\n", " ")
                    for line in body.splitlines():
                        if not any(marker in line for marker in checks):
                            continue
                        self.assertNotIn(
                            "|| true",
                            line,
                            f"{name}:{job_name}:{step.get('name', '?')} runs a "
                            f"check and discards its exit code: {line.strip()}",
                        )

        workflow = self._workflow("validate.yml")
        steps = workflow["jobs"]["validate-bios"]["steps"]
        gate = [s for s in steps if "rc != " in str(s.get("if", ""))]
        self.assertTrue(
            gate,
            "nothing in validate-bios acts on the validation exit code",
        )

    def test_the_suite_runs_on_a_direct_push_to_main(self):
        workflow = self._workflow("validate.yml")
        triggers = workflow["on"]
        self.assertIn("push", triggers, "validate.yml no longer runs on push")
        self.assertIn("main", triggers["push"]["branches"])
        job = next(
            (
                name
                for name, body in workflow["jobs"].items()
                if any(
                    "unittest discover" in str(step.get("run", ""))
                    for step in body.get("steps", [])
                )
            ),
            None,
        )
        self.assertIsNotNone(job, "no job runs the test suite")
        self.assertIsNone(
            workflow["jobs"][job].get("if"),
            f"{job} carries a condition; the suite must run on push and on PR",
        )

    def test_added_profiles_meet_their_upstream_on_both_roads(self):
        """A new profile's refs were confronted only if its author remembered."""
        workflow = self._workflow("validate.yml")
        runs = [
            (name, str(step.get("run", "")))
            for name, body in workflow["jobs"].items()
            for step in body.get("steps", [])
            if "profile_sync.py" in str(step.get("run", ""))
        ]
        self.assertTrue(runs, "no job confronts profiles with their upstream")
        for name, body in runs:
            self.assertIn("--strict", body)
            self.assertIn("--diff-filter=A", body)
            self.assertIsNone(workflow["jobs"][name].get("if"), f"{name} must run on push too")

    def test_both_events_watch_the_same_paths(self):
        triggers = self._workflow("validate.yml")["on"]
        self.assertEqual(
            triggers["pull_request"]["paths"],
            triggers["push"]["paths"],
            "the two path lists have drifted; a push would skip what a PR checks",
        )

    def test_pull_request_only_jobs_are_guarded(self):
        workflow = self._workflow("validate.yml")
        for name, body in workflow["jobs"].items():
            uses_pr_context = "github.event.pull_request" in yaml.dump(body)
            if uses_pr_context:
                self.assertEqual(
                    body.get("if"),
                    "github.event_name == 'pull_request'",
                    f"{name} reads pull request context and would run on a push",
                )

    def test_no_workflow_relies_on_a_yaml_anchor(self):
        """The workflow parser reads no anchor; PyYAML would hide the break."""
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                self.assertIsNone(
                    re.search(r"(?:^|\s)[&*][A-Za-z_]", line),
                    f"{path.name}:{number} uses a YAML anchor: {line.strip()}",
                )


class UnreachableCitationRegressions(unittest.TestCase):
    """A citation must name a place a declared repository can hold.

    kenji-nx carried tmp/es-de/ANDROID.md:470-474, a path from the machine
    of whoever profiled it. No revision of any declared repository holds it,
    so profile_sync could only report it missing, every pass, forever. The
    check is offline and runs on every push.
    """

    @unittest.skipUnless(HAS_JSONSCHEMA, "validating a schema needs jsonschema")
    def _hits(self, text: str) -> bool:
        import validate_schemas

        return bool(validate_schemas._UNREACHABLE_REF.search(" " + text))

    def test_a_scratch_directory_is_rejected(self):
        self.assertTrue(self._hits("tmp/es-de/ANDROID.md:470-474"))

    def test_an_absolute_path_is_rejected(self):
        self.assertTrue(self._hits("/home/someone/src/a.c:12"))
        self.assertTrue(self._hits(r"C:\work\src\a.c:12"))

    def test_a_path_climbing_out_of_the_tree_is_rejected(self):
        self.assertTrue(self._hits("../outside/src/a.c:12"))

    def test_ordinary_citations_pass(self):
        for good in (
            "src/core/main.cpp:210",
            "libretro.c:5293-5337",
            "PCE.emu/src/main/Main.cc:80-91",
            "es-de ANDROID.md:470-474",
            "see tmp files for details",
        ):
            with self.subTest(citation=good):
                self.assertFalse(self._hits(good))

    @unittest.skipUnless(HAS_JSONSCHEMA, "validating a schema needs jsonschema")
    def test_the_corpus_carries_none(self):
        import validate_schemas

        errors = []
        for path in sorted((ROOT / "emulators").glob("*.yml")):
            with path.open(encoding="utf-8") as handle:
                errors.extend(
                    validate_schemas._unreachable_citations(path, yaml.safe_load(handle))
                )
        self.assertEqual(errors, [])


@unittest.skipUnless(HAS_JSONSCHEMA, "validate_schemas needs jsonschema")
class DoubledCitationPaths(unittest.TestCase):
    """primehack cited Source/Core/Core/IOS/USB/Bluetooth/ twice in a row in a
    comment: no revision holds that path, and the parsed document never
    showed it to the citation checks."""

    def _hits(self, text: str) -> bool:
        import validate_schemas

        return bool(validate_schemas._DOUBLED_PATH.search(text))

    def test_a_repeated_directory_is_rejected(self):
        self.assertTrue(self._hits(
            "# ref: Source/Core/Core/IOS/USB/Bluetooth/Source/Core/Core/IOS/USB/"
            "Bluetooth/RealtekFirmwareLoader.cpp:373-416"
        ))

    def test_a_directory_named_once_passes(self):
        for good in (
            "Source/Core/Core/IOS/USB/Bluetooth/RealtekFirmwareLoader.cpp:383",
            "src/src/main.c:10",
            "a/b/c/a/b.c:1",
        ):
            with self.subTest(citation=good):
                self.assertFalse(self._hits(good))

    def test_the_corpus_carries_none(self):
        import validate_schemas

        errors = []
        for path in sorted((ROOT / "emulators").glob("*.yml")):
            errors.extend(validate_schemas._doubled_paths(path))
        self.assertEqual(errors, [])


class VerboseAppliesToJson(unittest.TestCase):
    """verify accepted --verbose beside --json in every mode and dropped it:
    the output was byte for byte the same, and the passing entries a script
    asked for never came."""

    def _run(self, *args: str) -> dict:
        import json
        import subprocess

        completed = subprocess.run(
            [sys.executable, "scripts/verify.py", *args, "--json"],
            capture_output=True, text=True, cwd=ROOT, timeout=900, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        return json.loads(completed.stdout)

    def test_emulator_and_system_modes_list_the_passing_entries(self):
        for mode in (("--emulator", "handy"), ("--system", "atari-lynx")):
            with self.subTest(mode=mode[0]):
                plain = self._run(*mode)
                verbose = self._run(*mode, "--verbose")
                self.assertGreater(len(verbose["details"]), len(plain["details"]))
                self.assertIn("ok", {d["status"] for d in verbose["details"]})

    def test_every_json_branch_reads_the_flag(self):
        source = (ROOT / "scripts" / "verify.py").read_text()
        self.assertEqual(source.count("_json_details("), 4)
        self.assertNotIn('if d["status"] != Status.OK\n            ]', source)


class OnePlatformPerFlag(unittest.TestCase):
    """generate_pack validated --platform as a comma list, then used the whole
    string as one name: `--platform bizhawk,misterfpga` passed and ended in a
    FileNotFoundError traceback. Every script now says it takes one name."""

    def test_a_list_is_refused_with_a_message(self):
        import subprocess

        runs = {
            "generate_pack.py": ["--list-systems"],
            "verify.py": [],
            "cross_reference.py": [],
        }
        for script, extra in runs.items():
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, f"scripts/{script}", "--platform",
                     "bizhawk,misterfpga", *extra],
                    capture_output=True, text=True, cwd=ROOT, timeout=600, check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("takes one platform", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)

    def test_unknown_names_still_get_a_suggestion(self):
        from common import require_platform

        with self.assertRaises(SystemExit) as caught:
            require_platform("batocra", str(ROOT / "platforms"))
        self.assertIn("batocera", str(caught.exception))


class APackIsSwappedInWhole(unittest.TestCase):
    """Packs were opened on their final name: a rebuild truncated the good
    pack at once, and a build that raised half way left a readable ZIP of
    part of the files, which the run then verified and summed."""

    def _dir(self) -> Path:
        import shutil
        import tempfile

        (ROOT / "tmp").mkdir(exist_ok=True)
        directory = Path(tempfile.mkdtemp(dir=ROOT / "tmp"))
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def test_a_failed_build_keeps_the_previous_pack(self):
        import zipfile

        from generate_pack import _writing_pack

        directory = self._dir()
        pack = directory / "Pack.zip"
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("old.bin", b"complete")
        with self.assertRaises(OSError):
            with _writing_pack(str(pack)) as zf:
                zf.writestr("new.bin", b"half")
                raise OSError("disk full")
        with zipfile.ZipFile(pack) as zf:
            self.assertEqual(zf.namelist(), ["old.bin"])
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["Pack.zip"])

    def test_a_finished_build_replaces_it(self):
        import zipfile

        from generate_pack import _writing_pack

        directory = self._dir()
        pack = directory / "Pack.zip"
        pack.write_bytes(b"stale")
        with _writing_pack(str(pack)) as zf:
            zf.writestr("new.bin", b"whole")
        with zipfile.ZipFile(pack) as zf:
            self.assertEqual(zf.namelist(), ["new.bin"])
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["Pack.zip"])

    def test_no_pack_is_opened_on_its_final_name(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text()
        self.assertNotIn('ZipFile(zip_path, "w"', source)


class EverySharedGroupServesAPlatform(unittest.TestCase):
    """Six of the eight shared groups injected nothing: fuse, mt32 and
    jiffydos were included by no platform, and kronos, keropi and ep128emu
    repeated System.dat entry for entry. load_platform_config keeps the
    platform's own entry, so a hash corrected there changed no pack, no
    verify and no manifest, and nothing said so."""

    def test_each_group_contributes_an_entry_somewhere(self):
        from common import list_registered_platforms, load_platform_config

        platforms_dir = str(ROOT / "platforms")
        with (ROOT / "platforms" / "_shared.yml").open(encoding="utf-8") as handle:
            groups = set((yaml.safe_load(handle) or {}).get("shared_groups") or {})
        served = {
            entry.get("shared_group")
            for name in list_registered_platforms(platforms_dir, include_archived=True)
            for system in load_platform_config(name, platforms_dir).get("systems", {}).values()
            for entry in system.get("files", [])
        }
        self.assertEqual(sorted(groups - served), [])


class PatchesTargetWhatRuns(unittest.TestCase):
    """mock.patch.object with create set to True builds an attribute on a
    module when the name is missing: packextras imported resolve_platform_cores
    inside the function, the patch created a global nobody read, and the
    real function walked the repository while the test claimed to isolate
    it."""

    def test_no_test_creates_the_attribute_it_patches(self):
        needle = "create" + "=True"
        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if needle in code:
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])


class StandaloneCopiesNameTheirSource(unittest.TestCase):
    """The registry's anchor names one path per file for every platform;
    each manifest places the file its own way, and the installer copied
    nothing where the two differed (EmuDeck's aes_keys.txt, Batocera's
    switch/prod.keys)."""

    def _placed(self, copies, dests):
        from generate_pack import _placed_copies

        return _placed_copies(copies, dests)

    def test_a_path_tail_finds_the_file(self):
        (copy,) = self._placed(
            [{"file": "GC/USA/IPL.bin"}],
            ["dolphin-emu/Sys/GC/USA/IPL.bin", "dolphin-emu/Sys/GC/EUR/IPL.bin"],
        )
        self.assertEqual(copy["source"], "dolphin-emu/Sys/GC/USA/IPL.bin")

    def test_the_tail_with_least_on_top_wins(self):
        (copy,) = self._placed(
            [{"file": "GC/USA/IPL.bin"}],
            ["bios/GC/USA/IPL.bin", "bios/dolphin-emu/Sys/GC/USA/IPL.bin"],
        )
        self.assertEqual(copy["source"], "bios/GC/USA/IPL.bin")

    def test_a_unique_name_finds_the_file_elsewhere(self):
        (copy,) = self._placed(
            [{"file": "Citra/sysdata/aes_keys.txt"}], ["aes_keys.txt", "bios.bin"]
        )
        self.assertEqual(copy["source"], "aes_keys.txt")

    def test_an_ambiguous_or_present_file_is_left_as_written(self):
        copies = [{"file": "prod.keys"}, {"file": "IPL.bin"}]
        placed = self._placed(copies, ["prod.keys", "a/IPL.bin", "b/IPL.bin"])
        self.assertEqual(placed, copies)


class ADamagedArchiveIsNotPacked(unittest.TestCase):
    """The deterministic rebuild raises on a member whose bytes fail their
    CRC; the builder caught it and copied the damaged archive into the pack
    "for verify to flag", which nothing did."""

    def test_the_rebuild_refusal_reaches_the_run(self):
        import zipfile

        from generate_pack import DamagedArchiveError, _normalize_zip_for_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            source = Path(directory) / "set.zip"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("rom.bin", b"A" * 64)
            data = bytearray(source.read_bytes())
            data[data.index(b"A" * 64)] ^= 0xFF
            source.write_bytes(bytes(data))
            pack = Path(directory) / "pack.zip"
            with zipfile.ZipFile(pack, "w") as target:
                with self.assertRaises(DamagedArchiveError) as caught:
                    _normalize_zip_for_pack(str(source), "set.zip", target)
                self.assertNotIn("set.zip", target.namelist())
            self.assertIsInstance(caught.exception, OSError)
            self.assertIn("set.zip", str(caught.exception))


class PackReadmesSayWhatTheInstallerDoes(unittest.TestCase):
    """The EmuDeck README promised keys set up automatically (they need
    --standalone-copies) and gave a Citra path the installer never uses; the
    RetroArch README offered only `| sh`, which Windows cannot run."""

    def _guide(self, platform: str) -> str:
        from packreadme import _build_readme

        text = _build_readme(platform, platform, "", 1, 1)
        return text[text.index("INSTALLATION"):text.index("TROUBLESHOOTING")]

    def test_retroarch_offers_the_windows_installer(self):
        guide = self._guide("retroarch")
        self.assertIn("install.sh | sh", guide)
        self.assertIn("install.ps1 | iex", guide)

    def test_emudeck_keys_match_the_registry_and_the_flag(self):
        import re

        guide = self._guide("emudeck")
        self.assertIn("--standalone-copies", guide)
        self.assertNotIn("emulator keys automatically", guide)
        self.assertNotIn("handles this for you", guide)
        with (ROOT / "platforms" / "_registry.yml").open(encoding="utf-8") as handle:
            registry = yaml.safe_load(handle)
        targets = {
            target.rstrip("/").replace("$HOME", "~")
            for copy in registry["_standalone_copies"]
            for target in copy.get("targets", {}).get("linux", [])
        }
        named = {
            path.rstrip("/") for path in re.findall(r"-> (~/\S+)", guide)
        }
        self.assertTrue(named)
        self.assertEqual(sorted(named - targets), [])

    def test_retropie_never_sends_a_pc_to_the_linux_partition(self):
        guide = self._guide("retropie")
        self.assertNotIn("Put the SD card in your PC", guide)
        self.assertIn("install.sh | sh", guide)


class AutoFetchReadsListsAndPlacesWithoutClobbering(unittest.TestCase):
    """A profile hash list (daedalusx64's n64.psh) stopped auto_fetch with
    an unhashable-type error, and a fetched file went to the first folder
    sharing one word of the system slug (nintendo-64 into Nintendo/SNES),
    overwriting a same-named file there."""

    def test_a_hash_list_is_every_accepted_revision(self):
        import hashlib

        from scripts import auto_fetch

        data = b"second revision"
        entry = {"sha1": ["0" * 40, hashlib.sha1(data).hexdigest()]}
        self.assertTrue(auto_fetch.verify_content(data, entry))
        db = {"files": {hashlib.sha1(data).hexdigest(): {"path": "bios/x/n64.psh"}},
              "indexes": {"by_md5": {}}}
        self.assertEqual(auto_fetch.step1_crossref_db(entry, db), "bios/x/n64.psh")

    def test_the_whole_slug_picks_the_folder_and_nothing_is_overwritten(self):
        from scripts import auto_fetch

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            bios = Path(directory)
            for folder in ("Nintendo/SNES", "Nintendo/N64", "Nintendo/Nintendo 64DD"):
                (bios / folder).mkdir(parents=True)
            held = bios / "Nintendo" / "N64" / "n64.psh"
            held.write_bytes(b"other revision")
            placed = Path(auto_fetch.place_file(
                b"fetched", {"name": "n64.psh", "system": "nintendo-64"}, str(bios), {}
            ))
            self.assertEqual(placed.parent.parent, bios / "Nintendo" / "N64")
            self.assertEqual(placed.parent.name, ".variants")
            self.assertEqual(held.read_bytes(), b"other revision")


class NoParserAcceptsAnAbbreviation(unittest.TestCase):
    """argparse completes a prefix: `slots.py --emulator dolphin` became
    --emulators-dir dolphin and reported 0 contested destinations, exit 0;
    `pipeline.py --all` became --all-variants and built six packs per
    platform. The flags read like other scripts' and were never refused."""

    def test_every_parser_refuses_prefixes(self):
        import ast

        lax = []
        for path in sorted([*(ROOT / "scripts").rglob("*.py"), ROOT / "install.py"]):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "ArgumentParser":
                    continue
                if not any(
                    kw.arg == "allow_abbrev"
                    and isinstance(kw.value, ast.Constant) and kw.value.value is False
                    for kw in node.keywords
                ):
                    lax.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(lax, [])


class FaqRegressions(unittest.TestCase):
    """The FAQ states facts the code owns; these tie it back to the source.

    Three of its claims had drifted: a pinned MAME version, Adler-32
    attributed to Dolphin's IPL instead of its DSP ROMs, and a verbose
    per-emulator report described as the only way to catch a bad file on an
    existence platform. Readers act on all three.
    """

    _MAME_GENERATIONS = {
        "MAME 2000": "mame2000",
        "MAME 2003": "mame2003",
        "MAME 2009": "mame2009",
        "MAME 2010": "mame2010",
        "MAME 2015": "mame2015",
        "MAME 2016": "mame2016",
        "current MAME": "mame",
    }

    @staticmethod
    def _faq() -> str:
        return (ROOT / "wiki" / "faq.md").read_text(encoding="utf-8")

    @staticmethod
    def _profile(key: str) -> dict:
        with (ROOT / "emulators" / f"{key}.yml").open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def test_mame_versions_match_their_profiles(self):
        # The list is wrapped, so a label and its version can straddle a line.
        faq = " ".join(self._faq().split())
        for label, key in self._MAME_GENERATIONS.items():
            match = re.search(rf"{re.escape(label)} \(([^)]+)\)", faq)
            self.assertIsNotNone(match, f"FAQ no longer states a version for {label}")
            self.assertEqual(
                match.group(1),
                self._profile(key)["core_version"],
                f"FAQ version for {label} has drifted from emulators/{key}.yml",
            )

    def test_adler32_is_attributed_to_the_files_that_carry_it(self):
        dolphin = self._profile("dolphin")
        carriers = {
            f["name"] for f in dolphin["files"] if f.get("known_hash_adler32")
        }
        self.assertTrue(carriers, "dolphin.yml declares no Adler-32 hash")
        sentence = self._faq().split("Adler-32", 1)[1].split("\n\n", 1)[0]
        for name in carriers:
            self.assertIn(name, sentence, f"FAQ omits the Adler-32 file {name}")
        self.assertNotIn(
            "IPL.bin", carriers,
            "IPL.bin gained an Adler-32 hash; the docs say it has none",
        )
        for page in ("faq.md", "profiling.md"):
            text = (ROOT / "wiki" / page).read_text(encoding="utf-8")
            for line in text.splitlines():
                if "Adler-32" in line or "adler32" in line:
                    self.assertNotIn(
                        "IPL", line,
                        f"wiki/{page} ties Adler-32 back to IPL: {line.strip()}",
                    )

    def test_existence_platforms_are_not_told_the_verbose_report_is_the_only_check(self):
        self.assertIn(
            "DISCREPANCY",
            self._faq(),
            "the FAQ must name the check the platform report performs itself",
        )
        self.assertIn(
            'result["discrepancy"]',
            (ROOT / "scripts" / "verify.py").read_text(encoding="utf-8"),
            "verify.py no longer raises the discrepancy the FAQ advertises",
        )


class CatalogRatioRegressions(unittest.TestCase):
    """The catalog-matched count must read the same on every surface.

    The home page, the provenance page, the README and the stats export each
    used to count matches on their own. Two of them dropped the systems
    scope, so the site published 566 and 553 for the same quantity, one click
    apart, and the export paired the wider number with composition.systems as
    its denominator.
    """

    _SURFACES = ("scripts/generate_site.py", "scripts/generate_readme.py")

    def test_no_surface_counts_matches_on_its_own(self):
        for relative in self._SURFACES:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            inline = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "sum"
                and "provenance" in ast.dump(node)
            ]
            self.assertEqual(
                inline,
                [],
                f"{relative} counts provenance matches inline at {inline}; "
                "call common.count_catalog_matched instead",
            )

    def test_arcade_and_engine_data_stay_out_of_the_ratio(self):
        from scripts.common import compute_composition, count_catalog_matched

        db = {
            "files": {
                "a": {"path": "bios/Sony/PlayStation/scph5501.bin", "size": 1,
                      "provenance": {"redump": {}}},
                "b": {"path": "bios/Arcade/Arcade/neogeo.zip", "size": 1,
                      "provenance": {"no-intro": {}}},
                "c": {"path": "bios/ScummVM/soundfonts/Roland.sf2", "size": 1,
                      "provenance": {"tosec": {}}},
                "d": {"path": "bios/Sega/Saturn/sega_101.bin", "size": 1},
            }
        }
        self.assertEqual(count_catalog_matched(db), 1)
        self.assertEqual(compute_composition(db)["systems"]["files"], 2)

    def test_engine_and_publisher_trees_are_game_data(self):
        """Engine data collected under `Game Engines/` and under a game
        publisher counted as console and computer system files: 7 697 of
        them made the README announce 13 564 system files."""
        from scripts.common import composition_tier

        for path in (
            "bios/Game Engines/C-Dogs SDL/cdogs/data/guns.json",
            "bios/Id Software/Quake III Arena/baseq3/pak1.pk3",
            "bios/Epic MegaGames/Jazz Jackrabbit 2/jazz2/Source/share.j2e",
            "bios/RPG Maker/easyrpg/rtp/2000/Backdrop/Bridge.png",
        ):
            self.assertEqual(composition_tier(path), "game_data", path)
        self.assertEqual(composition_tier("bios/Sony/PlayStation/scph5501.bin"), "systems")
        self.assertEqual(composition_tier("bios/Microsoft/MSX/MSX2.ROM"), "systems")

    def test_readme_ratio_matches_the_database(self):
        from scripts.common import compute_composition, count_catalog_matched, load_database

        database = ROOT / "database.json"
        if not database.exists():
            self.skipTest("database.json not generated")
        db = load_database(str(database))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(
            r"\*\*([\d,]+) of ([\d,]+) system files\*\* matched to", readme
        )
        self.assertIsNotNone(match, "README no longer states the catalog ratio")
        matched, total = (int(g.replace(",", "")) for g in match.groups())
        self.assertEqual(matched, count_catalog_matched(db))
        self.assertEqual(total, compute_composition(db)["systems"]["files"])


class PipelineRegressions(unittest.TestCase):
    def test_pack_parser_removes_source_metadata(self):
        output = "\n".join(
            [
                "Generating pack for RetroArch [source=full]...",
                "  3 files packed (2 baseline + 1 from cores), 2/2 files OK",
            ]
        )
        self.assertEqual(pipeline.parse_pack_counts(output), {"RetroArch": (2, 2)})

    def test_missing_pack_is_a_consistency_failure(self):
        verify = "RetroArch: 2/2 OK"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pipeline.check_consistency(verify, ""))

    def test_display_name_matches_compact_registry_id(self):
        verify = "MiSTer FPGA: 65/65 OK [md5]"
        pack = "\n".join(
            [
                "Generating pack for misterfpga [source=full]...",
                "  pack.zip: 65 files packed (65 baseline + 0 from cores), "
                "65/65 files OK [md5]",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(pipeline.check_consistency(verify, pack))

    def test_an_exclusion_never_covers_a_file_verify_counts_ok(self):
        """An existence platform packs a file whose hash disagrees and a hash
        platform's verify reports it untested: exclusions that make up the
        difference hid the very divergence this step exists to catch."""
        verify = "RetroArch: 3/3 present [existence]"
        pack = "\n".join(
            [
                "Generating pack for RetroArch [source=full]...",
                "  pack.zip: 2 files packed (2 baseline + 0 from cores), "
                "2/3 files OK, 1 unsafe excluded [existence]",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pipeline.check_consistency(verify, pack))
        self.assertEqual(pipeline.parse_pack_exclusions(pack), {"RetroArch": 1})

    def test_unaccounted_pack_omission_is_a_consistency_failure(self):
        verify = "RetroArch: 3/3 present [existence]"
        pack = "\n".join(
            [
                "Generating pack for RetroArch [source=full]...",
                "  pack.zip: 1 files packed (1 baseline + 0 from cores), "
                "1/3 files OK, 1 unsafe excluded, 1 missing [existence]",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pipeline.check_consistency(verify, pack))

    def test_a_pack_more_optimistic_than_verify_is_a_failure(self):
        """The invariant held one way only: a pack counting more OK files
        than the report passed as 'pack resolves more'."""
        verify = "DupTest: 0/1 OK, 1 untested [md5]"
        pack = "\n".join(
            [
                "Generating pack for DupTest [source=full]...",
                "  x/DupTest_1_BIOS_Pack.zip: 1 files packed (1 baseline + 0 from cores), "
                "1/1 files OK [md5]",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pipeline.check_consistency(verify, pack))

    def test_nothing_parsed_is_not_consistent(self):
        """A changed output format must not pass with no platform compared."""
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pipeline.check_consistency("RetroArch — 2/2 OK", ""))
            self.assertFalse(pipeline.check_consistency("", ""))

    def test_pack_without_verify_result_is_a_failure(self):
        verify = "RetroArch: 2/2 OK"
        pack = "\n".join(
            [
                "Generating pack for RetroArch [source=full]...",
                "  a.zip: 2 files packed (2 baseline + 0 from cores), 2/2 files OK",
                "Generating pack for Batocera [source=full]...",
                "  b.zip: 2 files packed (2 baseline + 0 from cores), 2/2 files OK",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pipeline.check_consistency(verify, pack))

    def test_only_the_full_variant_feeds_the_comparison(self):
        """--all-variants printed six packs under one label; the last won."""
        output = "\n".join([
            "Generating pack for RetroArch [source=full]...",
            "  a.zip: 5 files packed (5 baseline + 0 from cores), 535/535 files OK",
            "Generating pack for RetroArch [source=platform, required]...",
            "  b.zip: 5 files packed (5 baseline + 0 from cores), 530/530 files OK",
        ])
        self.assertEqual(pipeline.parse_pack_counts(output), {"RetroArch": (535, 535)})

    def test_exclusions_read_only_the_full_variant(self):
        """--all-variants printed six packs under one label; the last won."""
        output = "\n".join([
            "Generating pack for Batocera [source=full]...",
            "  a.zip: 5 files packed (5 baseline + 0 from cores), 5/5 files OK, 3 unsafe excluded",
            "Generating pack for Batocera [source=truth, required]...",
            "  b.zip: 2 files packed (0 baseline + 2 from cores), 2/2 files OK",
        ])
        self.assertEqual(pipeline.parse_pack_exclusions(output), {"Batocera": 3})

    def test_slot_step_reaches_the_summary(self):
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn('results["slots"] = ok', source)

    def test_every_refresh_failure_reaches_pipeline_exit_status(self):
        for failed_label in (
            "2/8 refresh data directories",
            "2a refresh MAME hashes",
            "2a2 refresh FBNeo hashes",
        ):
            with self.subTest(failed_label=failed_label):
                def fake_run(_command, label):
                    return label != failed_label, "RetroArch: 1/1 OK\n"

                argv = ["pipeline.py", "--skip-packs", "--skip-docs"]
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(pipeline, "run", side_effect=fake_run),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    pipeline.main()
                self.assertEqual(raised.exception.code, 1)


    def test_a_targeted_export_reads_a_full_model(self):
        """--target wrote truth/<target>/ only; the export read truth/."""
        commands: list[list[str]] = []

        def fake_run(command, _label):
            commands.append([str(part) for part in command])
            return True, "RetroArch: 1/1 OK\n"

        argv = ["pipeline.py", "--offline", "--skip-packs", "--skip-docs",
                "--with-export", "--target", "switch"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            pipeline, "run", side_effect=fake_run
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
            pipeline.main()
        truth_runs = [c for c in commands if "scripts/generate_truth.py" in c]
        export = next(c for c in commands if "scripts/export_native.py" in c)
        read_dir = export[export.index("--truth-dir") + 1]
        full_models = [
            c for c in truth_runs
            if "--target" not in c and c[c.index("--output-dir") + 1] == read_dir
        ]
        self.assertTrue(full_models, truth_runs)


class SchemaFormatsAreChecked(unittest.TestCase):
    """date-time and uri need jsonschema's format extras; without them the
    published format constraints were accepted unread."""

    @unittest.skipUnless(HAS_JSONSCHEMA, "validating a schema needs jsonschema")
    def test_a_missing_checker_is_named(self):
        import validate_schemas  # noqa: PLC0415

        empty = mock.Mock(checkers={})
        with mock.patch.object(validate_schemas, "FormatChecker", return_value=empty):
            self.assertEqual(validate_schemas.unchecked_formats(), ["date-time", "uri"])

    def test_every_install_asks_for_the_extras(self):
        for path in (".github/workflows/validate.yml", ".github/workflows/deploy-site.yml",
                     "pyproject.toml"):
            text = (ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotRegex(text, r"jsonschema(?!\[format)[=>]")


class ResolverRegressions(unittest.TestCase):
    def _database(self, entries: dict[str, Path], suffix: str | None = None) -> dict:
        files = {}
        by_name: dict[str, list[str]] = {}
        by_suffix: dict[str, list[str]] = {}
        for name, path in entries.items():
            payload = path.read_bytes()
            sha1 = hashlib.sha1(payload).hexdigest()
            md5 = hashlib.md5(payload).hexdigest()
            sha256 = hashlib.sha256(payload).hexdigest()
            files[sha1] = {
                "path": str(path),
                "name": name,
                "size": len(payload),
                "md5": md5,
                "sha256": sha256,
                "crc32": "00000000",
            }
            by_name.setdefault(name, []).append(sha1)
            if suffix and name == "firmware.bin":
                by_suffix.setdefault(suffix, []).append(sha1)
        return {
            "files": files,
            "indexes": {
                "by_name": by_name,
                "by_md5": {entry["md5"]: sha1 for sha1, entry in files.items()},
                "by_sha256": {
                    entry["sha256"]: sha1 for sha1, entry in files.items()
                },
                "by_crc32": {},
                "by_path_suffix": by_suffix,
            },
        }

    def test_hash_identity_wins_over_wrong_destination_hint(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            wrong = root / "wrong" / "firmware.bin"
            correct = root / "correct" / "firmware.bin"
            wrong.parent.mkdir()
            correct.parent.mkdir()
            wrong.write_bytes(b"wrong")
            correct.write_bytes(b"correct")
            db = self._database(
                {"firmware.bin": wrong, "correct-name.bin": correct},
                suffix="Console/USA/firmware.bin",
            )
            expected = hashlib.sha1(b"correct").hexdigest()
            path, status = resolve_local_file(
                {"name": "firmware.bin", "sha1": expected},
                db,
                dest_hint="Console/USA/firmware.bin",
            )
            self.assertEqual(path, str(correct))
            self.assertEqual(status, "sha1_exact")

    def test_name_cannot_mask_a_declared_hash_mismatch(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = Path(directory) / "firmware.bin"
            path.write_bytes(b"wrong")
            db = self._database({"firmware.bin": path})
            resolved, status = resolve_local_file(
                {"name": "firmware.bin", "sha1": "f" * 40}, db
            )
            self.assertEqual(resolved, str(path))
            self.assertEqual(status, "hash_mismatch")


class OneBaselineVerdict(unittest.TestCase):
    """The builder and the manifest each wrote the platform baseline.

    The builder looked inside a zipped_file archive whatever the outer
    resolution said; the manifest only after a hash_mismatch. An archive
    found by name without the member Batocera checks left the pack and
    stayed in the manifest. A slot override replaced a release asset in the
    manifest and not in the pack.
    """

    def _archive(self, directory: Path, member: str) -> str:
        path = directory / "dragon32.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, b"rom")
        return str(path)

    def test_a_missing_member_is_refused_whatever_the_outer_status(self):
        from scripts.validation import baseline_verdict

        entry = {"name": "dragon32.zip", "zipped_file": "d32.rom", "md5": ""}
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = self._archive(Path(directory), "other.rom")
            status, refusal = baseline_verdict(entry, path, "name_exact", "md5")
            self.assertEqual(refusal, "d32.rom not found inside ZIP")
            # An existence frontend never opens it.
            self.assertEqual(
                baseline_verdict(entry, path, "name_exact", "existence"),
                ("name_exact", None),
            )

    def test_the_pack_check_reads_the_same_verdict(self):
        """--verify-packs called the archive missing: its own rule looked
        inside only after a hash_mismatch, and an archive found by name
        without d32.rom is one the builder left out on purpose."""
        import hashlib

        from scripts.packverify import _intentional_hash_exclusion

        entry = {"name": "dragon32.zip", "zipped_file": "d32.rom", "md5": ""}
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = self._archive(Path(directory), "other.rom")
            sha1 = hashlib.sha1(Path(path).read_bytes()).hexdigest()
            db = {"files": {sha1: {"path": path, "name": "dragon32.zip", "sha1": sha1}},
                  "indexes": {"by_name": {"dragon32.zip": [sha1]}, "by_md5": {},
                              "by_path_suffix": {}, "by_crc32": {}}}
            self.assertTrue(_intentional_hash_exclusion([entry], db, verification_mode="md5"))
            self.assertFalse(
                _intentional_hash_exclusion([entry], db, verification_mode="existence")
            )

    def test_the_builder_accepts_what_verify_accepts(self):
        """verify took a 29-character md5 by prefix and a ZIP by its
        composite; the builder compared exact md5s and refused both when
        another declared hash disagreed."""
        import hashlib

        from scripts.common import md5_composite
        from scripts.validation import baseline_verdict

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            loose = Path(directory) / "abc.bin"
            loose.write_bytes(b"abc")
            truncated = {"name": "abc.bin", "md5": hashlib.md5(b"abc").hexdigest()[:29],
                         "crc32": "deadbeef"}
            self.assertEqual(
                baseline_verdict(truncated, str(loose), "hash_mismatch", "md5"),
                ("frontend_digest_exact", None),
            )
            archive = self._archive(Path(directory), "rom.bin")
            composite = {"name": "dragon32.zip", "md5": md5_composite(archive),
                         "crc32": "deadbeef"}
            self.assertEqual(
                baseline_verdict(composite, archive, "hash_mismatch", "md5"),
                ("frontend_digest_exact", None),
            )

    def test_a_release_asset_keeps_its_answer(self):
        from scripts.slots import apply_override

        self.assertEqual(
            apply_override("bios/x.bin", "/cache/big.bin", "release_asset"),
            ("/cache/big.bin", "release_asset"),
        )
        self.assertEqual(
            apply_override("bios/x.bin", "bios/y.bin", "name_exact"),
            ("bios/x.bin", "slot_arbitrated"),
        )

    def test_the_builder_and_the_manifest_read_the_same_rules(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name in ("generate_pack", "generate_manifest"):
            function = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            body = ast.get_source_segment(source, function) or ""
            with self.subTest(function=name):
                self.assertIn("baseline_verdict(", body)
                self.assertIn("slots.apply_override(", body)
                self.assertNotIn('"slot_arbitrated"', body)


class PackExclusionRegressions(unittest.TestCase):
    def test_slug_platform_core_requirement_uses_the_generated_destination(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            platforms = root / "platforms"
            platforms.mkdir()
            core_payload = root / "extra.bin"
            core_payload.write_bytes(b"mapped core payload")
            core_sha1 = hashlib.sha1(core_payload.read_bytes()).hexdigest()
            core_md5 = hashlib.md5(core_payload.read_bytes()).hexdigest()
            database = {
                "files": {
                    core_sha1: {
                        "name": "extra.bin",
                        "path": str(core_payload),
                        "md5": core_md5,
                        "size": core_payload.stat().st_size,
                    }
                },
                "indexes": {
                    "by_name": {"extra.bin": [core_sha1]},
                    "by_md5": {core_md5: core_sha1},
                    "by_sha256": {},
                    "by_crc32": {},
                    "by_path_suffix": {},
                },
            }
            config = {
                "platform": "Slug platform",
                "base_destination": "bios",
                "verification_mode": "existence",
                "cores": ["core_a"],
                "systems": {
                    "console-a": {
                        "files": [
                            {
                                "name": "base-a.bin",
                                "destination": "slug-a/base-a.bin",
                            }
                        ]
                    },
                    "console-b": {
                        "files": [
                            {
                                "name": "base-b.bin",
                                "destination": "slug-b/base-b.bin",
                            }
                        ]
                    },
                },
            }
            (platforms / "slug.yml").write_text(
                yaml.safe_dump(config), encoding="utf-8"
            )
            profiles = {
                "core_a": {
                    "emulator": "Core A",
                    "type": "libretro",
                    "systems": ["console-a"],
                    "files": [
                        {
                            "name": "extra.bin",
                            "path": "extra.bin",
                            "sha1": core_sha1,
                        }
                    ],
                }
            }
            pack = root / "pack.zip"
            with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("slug-a/base-a.bin", b"baseline a")
                archive.writestr("slug-b/base-b.bin", b"baseline b")
                archive.writestr("slug-a/extra.bin", core_payload.read_bytes())

            result = verify_pack_against_platform(
                str(pack),
                "slug",
                str(platforms),
                db=database,
                emu_profiles=profiles,
            )
            self.assertTrue(result[0], result[3])
            self.assertEqual(result[3], [])
            self.assertEqual(result[6:8], (1, 1))

    def _mismatch_fixture(self, root: Path, mode: str) -> tuple[dict, Path]:
        """A platform declaring a hash the only local payload contradicts."""
        platforms = root / "platforms"
        platforms.mkdir()
        payload = root / "firmware.bin"
        payload.write_bytes(b"wrong local variant")
        sha1 = hashlib.sha1(payload.read_bytes()).hexdigest()
        md5 = hashlib.md5(payload.read_bytes()).hexdigest()
        database = {
            "files": {
                sha1: {
                    "name": "firmware.bin",
                    "path": str(payload),
                    "md5": md5,
                    "size": payload.stat().st_size,
                }
            },
            "indexes": {
                "by_name": {"firmware.bin": [sha1]},
                "by_md5": {md5: sha1},
                "by_sha256": {},
                "by_crc32": {},
                "by_path_suffix": {},
            },
        }
        config = {
            "platform": f"Platform {mode}",
            "verification_mode": mode,
            "systems": {
                "console": {
                    "files": [
                        {
                            "name": "firmware.bin",
                            "destination": "firmware.bin",
                            "sha1": "f" * 40,
                        }
                    ]
                }
            },
        }
        (platforms / "plat.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        return database, platforms

    def test_hash_platform_accounts_for_an_unsafe_exclusion(self):
        """A platform that reads the bytes would reject the local payload."""
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            database, platforms = self._mismatch_fixture(root, "md5")
            pack = root / "pack.zip"
            with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("README.txt", "safe subset")

            result = verify_pack_against_platform(
                str(pack),
                "plat",
                str(platforms),
                db=database,
                emu_profiles={},
            )
            self.assertTrue(result[0], result[3])
            self.assertEqual(result[3], [])
            self.assertEqual(result[8], 1)

    def test_existence_platform_never_withholds_over_a_declared_hash(self):
        """RetroArch and friends only look for the filename.

        An upstream hash the local dump contradicts must not remove a file the
        frontend would have loaded, so its absence stays a conformance error.
        """
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            database, platforms = self._mismatch_fixture(root, "existence")
            pack = root / "pack.zip"
            with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("README.txt", "no firmware")

            result = verify_pack_against_platform(
                str(pack),
                "plat",
                str(platforms),
                db=database,
                emu_profiles={},
            )
            self.assertFalse(result[0])
            self.assertTrue(any("baseline missing" in e for e in result[3]), result[3])
            self.assertEqual(result[8], 0)

    def test_unexplained_missing_file_still_fails_conformance(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            platforms = root / "platforms"
            platforms.mkdir()
            config = {
                "platform": "Missing",
                "verification_mode": "existence",
                "systems": {
                    "console": {
                        "files": [
                            {"name": "absent.bin", "destination": "absent.bin"}
                        ]
                    }
                },
            }
            (platforms / "missing.yml").write_text(
                yaml.safe_dump(config), encoding="utf-8"
            )
            pack = root / "pack.zip"
            with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("README.txt", "incomplete")
            database = {
                "files": {},
                "indexes": {
                    "by_name": {},
                    "by_md5": {},
                    "by_sha256": {},
                    "by_crc32": {},
                    "by_path_suffix": {},
                },
            }

            result = verify_pack_against_platform(
                str(pack),
                "missing",
                str(platforms),
                db=database,
                emu_profiles={},
            )
            self.assertFalse(result[0])
            self.assertTrue(any("baseline missing" in error for error in result[3]))


class RegionRegressions(unittest.TestCase):
    def test_tagged_and_untagged_same_path_is_preserved(self):
        profiles = {
            "tagged": {
                "type": "libretro",
                "files": [
                    {"name": "bios.bin", "path": "sys/bios.bin", "region": ["japan"]}
                ],
            },
            "untagged": {
                "type": "libretro",
                "files": [{"name": "bios.bin", "path": "sys/bios.bin"}],
            },
        }
        index = region.build_region_index(profiles)
        self.assertEqual(region.lookup_regions(index, "sys/bios.bin", "bios.bin"), set())
        drops = region.resolve_region_drops(
            {"system": [("sys/bios.bin", "bios.bin")]},
            index,
            ["north-america"],
        )
        self.assertEqual(drops, set())

    def test_world_candidate_beats_unmatched_regional_fallback(self):
        profiles = {
            "core": {
                "type": "libretro",
                "files": [
                    {"name": "ntsc.bin", "region": ["world"]},
                    {"name": "pal.bin", "region": ["europe"]},
                ],
            }
        }
        index = region.build_region_index(profiles)
        groups = {"system": [("ntsc.bin", "ntsc.bin"), ("pal.bin", "pal.bin")]}
        self.assertEqual(
            region.resolve_region_drops(groups, index, ["north-america"]),
            {"pal.bin"},
        )
        self.assertEqual(region.resolve_region_drops(groups, index, ["europe"]), set())

    def test_multi_system_emulator_uses_separate_region_groups(self):
        profile = {"systems": ["odyssey2", "videopac"]}
        north_america = _emulator_region_group(
            "o2em", profile, {"name": "o2rom.bin", "system": "odyssey2"}
        )
        europe = _emulator_region_group(
            "o2em", profile, {"name": "c52.bin", "system": "videopac"}
        )
        self.assertNotEqual(north_america, europe)

    def test_region_audit_resolves_the_way_the_pack_does(self):
        """A private resolver read sha1, md5 and a lone name: an entry
        identified by its path (Dolphin's three IPL.bin) was never checked."""
        import hashlib  # noqa: PLC0415

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            previous = os.getcwd()
            os.chdir(directory)
            self.addCleanup(os.chdir, previous)
            files, shas = {}, {}
            for rel, payload in (("bios/GC/USA/IPL.bin", b"usa"), ("bios/GC/JAP/IPL.bin", b"jap")):
                Path(rel).parent.mkdir(parents=True, exist_ok=True)
                Path(rel).write_bytes(payload)
                sha1 = hashlib.sha1(payload).hexdigest()
                shas[rel] = sha1
                files[sha1] = {"path": rel, "name": "IPL.bin", "size": len(payload), "sha1": sha1,
                               "md5": hashlib.md5(payload).hexdigest(),
                               "sha256": hashlib.sha256(payload).hexdigest(), "crc32": "00000001"}
            from scripts import generate_db  # noqa: PLC0415

            db = {"files": files, "indexes": generate_db.build_indexes(files, {})}
            by_md5 = {e["md5"]: s for s, e in files.items()}
            self.assertEqual(
                region_audit.resolve_sha1({"name": "IPL.bin", "md5": [files[shas["bios/GC/USA/IPL.bin"]]["md5"]]}, db),
                shas["bios/GC/USA/IPL.bin"],
            )
            self.assertEqual(
                region_audit.resolve_sha1({"name": "IPL.bin", "path": "GC/JAP/IPL.bin"}, db),
                shas["bios/GC/JAP/IPL.bin"],
            )
            self.assertTrue(by_md5)


class ArchiveSecurityRegressions(unittest.TestCase):
    def _zip_path(self, directory: Path, name: str = "archive.zip") -> Path:
        return directory / name

    def test_member_count_limit_is_enforced(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("one.bin", b"1")
                handle.writestr("two.bin", b"2")
            with self.assertRaisesRegex(ValueError, "members"):
                safe_extract_zip(str(archive), str(root / "out"), max_members=1)

    def test_member_modes_follow_the_archive_and_the_umask(self):
        """Every member came out 0600, the mode of its temporary file: the
        pico8 and vox launchers the RetroDECK pack keeps 0755 lost their
        execute bit once download.py extracted them."""
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            program = zipfile.ZipInfo("pico8")
            program.create_system = 3
            program.external_attr = (stat.S_IFREG | 0o755) << 16
            plain = zipfile.ZipInfo("bios.bin")
            plain.create_system = 0
            plain.external_attr = 0x20  # MS-DOS archive bit, no Unix mode
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(program, b"\x7fELF")
                handle.writestr(plain, b"data")
            umask = os.umask(0o022)
            try:
                safe_extract_zip(str(archive), str(root / "out"))
            finally:
                os.umask(umask)
            self.assertEqual((root / "out" / "pico8").stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / "out" / "bios.bin").stat().st_mode & 0o777, 0o644)

    def test_symlink_member_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(link, "target")
            with self.assertRaisesRegex(ValueError, "link or special"):
                safe_extract_zip(str(archive), str(root / "out"))

    def test_windows_separator_is_a_path_not_a_rejection(self):
        """Archives written on Windows store a backslash separator.

        download.py feeds third-party archives to this function, so a legal
        Windows path must extract into a subdirectory instead of failing.
        """
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("sub\\rom.bin", b"payload")
            out = root / "out"
            safe_extract_zip(str(archive), str(out))
            self.assertEqual((out / "sub" / "rom.bin").read_bytes(), b"payload")

    def test_windows_separator_cannot_smuggle_traversal(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("..\\escaped.bin", b"payload")
            with self.assertRaisesRegex(ValueError, "traversal"):
                safe_extract_zip(str(archive), str(root / "out"))

    def test_high_ratio_method_is_bounded_by_size_not_ratio(self):
        """bzip2 legitimately exceeds the DEFLATE ceiling."""
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_BZIP2) as handle:
                handle.writestr("zeros.bin", bytes(4_000_000))
            out = root / "out"
            safe_extract_zip(str(archive), str(out))
            self.assertEqual((out / "zeros.bin").stat().st_size, 4_000_000)

    def test_compression_ratio_limit_is_enforced(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._zip_path(root)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("zeros.bin", bytes(16_384))
            with self.assertRaisesRegex(ValueError, "compression ratio"):
                safe_extract_zip(
                    str(archive), str(root / "out"), max_compression_ratio=2
                )


class NoMd5IsNotNothingChecked(unittest.TestCase):
    """A declared hash that is not an md5 still contradicts.

    verify_entry_md5 returned OK the moment the entry declared no md5, so an
    entry whose sha256 the local file contradicts read as covered while the
    builder was already excluding it. RetroDECK's dsifirmware.bin declares a
    sha256 and no md5.
    """

    def setUp(self):
        import verify

        self.verify = verify

    def _entry(self):
        return {"name": "dsifirmware.bin", "sha256": "b" * 64}

    def test_a_contradicted_sha256_is_not_reported_ok(self):
        result = self.verify.verify_entry_md5(
            self._entry(), "bios/whatever.bin", "hash_mismatch"
        )
        self.assertNotEqual(result["status"], self.verify.Status.OK)
        self.assertIn("contradicted", result.get("reason", ""))

    def test_a_clean_resolution_is_still_ok(self):
        result = self.verify.verify_entry_md5(
            self._entry(), "bios/whatever.bin", "sha256_exact"
        )
        self.assertEqual(result["status"], self.verify.Status.OK)


class InstallerBoundaryRegressions(unittest.TestCase):
    def _manifest(self, dest: str) -> dict:
        return {
            "manifest_version": 2,
            "platform": "retroarch",
            "files": [
                {
                    "dest": dest,
                    "size": 1,
                    "sha1": "a" * 40,
                    "sha256": "b" * 64,
                    "repo_path": "bios/test.bin",
                    "cores": None,
                }
            ],
            "standalone_copies": [],
        }

    def test_manifest_destination_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            install._validate_manifest(self._manifest("../escape"), "retroarch")

    def test_manifest_repo_source_is_confined_to_bios(self):
        manifest = self._manifest("safe.bin")
        manifest["files"][0]["repo_path"] = "scripts/pipeline.py"
        with self.assertRaisesRegex(ValueError, "outside bios"):
            install._validate_manifest(manifest, "retroarch")

    def test_omitted_destination_cannot_overlap_a_download(self):
        manifest = self._manifest("safe.bin")
        manifest["omitted_files"] = [
            {
                "dest": "safe.bin",
                "name": "safe.bin",
                "system": "console",
                "required": True,
                "reason": "hash_mismatch",
                "cores": None,
            }
        ]
        manifest["total_omitted"] = 1
        with self.assertRaisesRegex(ValueError, "conflicting omitted"):
            install._validate_manifest(manifest, "retroarch")

    def test_omitted_destination_traversal_is_rejected(self):
        manifest = self._manifest("safe.bin")
        manifest["omitted_files"] = [
            {
                "dest": "../unsafe.bin",
                "name": "unsafe.bin",
                "system": "console",
                "required": True,
                "reason": "hash_mismatch",
                "cores": None,
            }
        ]
        manifest["total_omitted"] = 1
        with self.assertRaisesRegex(ValueError, "unsafe"):
            install._validate_manifest(manifest, "retroarch")

    @unittest.skipUnless(HAS_JSONSCHEMA, "validating a schema needs jsonschema")
    def test_target_schema_accepts_the_null_the_generator_emits(self):
        """The schema and generate_target_manifests must agree on null.

        A target with no core list is written as null; a schema that rejects
        it turns a valid manifest into a CI failure.
        """
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "target-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        document = {"windows": None, "switch": ["a5200"]}
        self.assertEqual(list(validator.iter_errors(document)), [])

    @unittest.skipUnless(HAS_JSONSCHEMA, "validating a schema needs jsonschema")
    def test_pack_manifests_are_read_from_inside_the_archive(self):
        """generate_pack writes manifest.json into the ZIP, not beside it.

        A filesystem glob over dist/ matches nothing and reports success
        without validating a single document.
        """
        import validate_schemas

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            dist = Path(directory)
            broken = {
                "schema_version": 1,
                "version": 1,
                "generator": "retrobios generate_pack.py",
                "generated": "2026-08-10T00:00:00Z",
                "files": [{"path": "a.bin", "sha1": "nope", "md5": "nope",
                           "size": 1, "status": "verified", "name": "a.bin"}],
                "summary": {"total_files": 1, "verified": 1, "untracked": 0,
                            "errors": 0},
                "errors": [],
            }
            with zipfile.ZipFile(dist / "Pack.zip", "w") as archive:
                archive.writestr("manifest.json", json.dumps(broken))

            errors = validate_schemas._validate_pack_manifests(dist)
            self.assertTrue(errors, "an invalid in-archive manifest must be reported")
            self.assertTrue(any("sha1" in message for message in errors), errors)

    def test_null_core_list_keeps_the_other_targets(self):
        """A target without a core inventory must not void the manifest.

        generate_target_manifests emits null for a target that publishes no
        core list; rejecting the document would silently disable --target for
        every target on that platform.
        """
        normalized = install._validate_targets({"windows": None, "switch": ["a5200"]})
        self.assertIsNone(normalized["windows"]["cores"])
        self.assertEqual(normalized["switch"]["cores"], ["a5200"])

    def test_legacy_target_lists_are_normalized(self):
        self.assertEqual(
            install._validate_targets({"rpi4": ["core-a", "core-b"]}),
            {"rpi4": {"cores": ["core-a", "core-b"]}},
        )


class TargetManifestRegressions(unittest.TestCase):
    def test_yaml_scalar_core_is_rejected_instead_of_leaking_to_json(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "platform.yml").write_text(
                "targets:\n  device:\n    cores: [81, valid-core]\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-empty strings"):
                generate_target_manifests(str(source), str(output))


class CheckoutCompletenessRegressions(unittest.TestCase):
    """Coverage is resolved against the disk, so the checkout must be whole.

    Files over 50 MB and the data directory caches are gitignored. A job that
    regenerates the README or the site without restoring them counts those
    files as missing and publishes a coverage the collection does not have.
    """

    def _steps(self, workflow: str, job: str) -> list[dict]:
        data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text())
        return data["jobs"][job]["steps"]

    def _index(self, steps: list[dict], needle: str) -> int:
        for position, step in enumerate(steps):
            if needle in step.get("name", "") or needle in str(step.get("run", "")):
                return position
        self.fail(f"no step matching {needle!r}")

    def test_site_deploy_completes_the_checkout_before_generating(self):
        steps = self._steps("deploy-site.yml", "build")
        generate = self._index(steps, "Generate site")
        self.assertLess(self._index(steps, "restore_large_files.py"), generate)
        self.assertLess(self._index(steps, "refresh_data_dirs.py"), generate)

    def test_restore_matches_assets_by_content_not_by_name(self):
        from scripts.restore_large_files import restore

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            payload = b"firmware bytes"
            (cache / "renamed-asset.bin").write_bytes(payload)
            sha1 = hashlib.sha1(payload).hexdigest()
            (root / ".gitignore").write_text("bios/Sony/big.pup\n", encoding="utf-8")
            (root / "database.json").write_text(
                json.dumps({"files": {sha1: {"path": "bios/Sony/big.pup"}}}),
                encoding="utf-8",
            )
            cwd = os.getcwd()
            os.chdir(root)
            try:
                restored, unsatisfied = restore(
                    str(cache), "database.json", ".gitignore"
                )
                self.assertEqual((restored, unsatisfied), (1, []))
                self.assertEqual((root / "bios/Sony/big.pup").read_bytes(), payload)
                # A path already in the checkout is never overwritten.
                restored, unsatisfied = restore(
                    str(cache), "database.json", ".gitignore"
                )
                self.assertEqual((restored, unsatisfied), (0, []))
            finally:
                os.chdir(cwd)

    def test_restore_leaves_tracked_paths_alone(self):
        from scripts.restore_large_files import restore

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            payload = b"tracked bytes"
            (cache / "asset.bin").write_bytes(payload)
            sha1 = hashlib.sha1(payload).hexdigest()
            (root / ".gitignore").write_text("bios/other.bin\n", encoding="utf-8")
            (root / "database.json").write_text(
                json.dumps({"files": {sha1: {"path": "bios/tracked.bin"}}}),
                encoding="utf-8",
            )
            cwd = os.getcwd()
            os.chdir(root)
            try:
                restored, unsatisfied = restore(
                    str(cache), "database.json", ".gitignore"
                )
                self.assertEqual((restored, unsatisfied), (0, []))
                self.assertFalse((root / "bios/tracked.bin").exists())
            finally:
                os.chdir(cwd)

    def test_restore_without_a_cache_names_what_is_missing(self):
        """A job that skipped `mkdir .cache/large` exited 0 with every
        gitignored file absent: the early return ran before the check."""
        from scripts.restore_large_files import restore

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            (root / ".gitignore").write_text("bios/big.bin\n", encoding="utf-8")
            (root / "database.json").write_text(
                json.dumps({"files": {"a" * 40: {"path": "bios/big.bin"}}}),
                encoding="utf-8",
            )
            cwd = os.getcwd()
            os.chdir(root)
            try:
                self.assertEqual(
                    restore("no-cache", "database.json", ".gitignore"),
                    (0, ["bios/big.bin"]),
                )
            finally:
                os.chdir(cwd)


class EveryManifestEntryIsFetchable(unittest.TestCase):
    """An install manifest may not list a file the installer cannot get.

    install.py fetches a file either from its repo_path or from a release
    asset. An entry carrying neither is a line in the download list that can
    only ever fail. It happened when resolution landed on a file the database
    does not index, so the hash was computed but the repo lookup came back
    empty and the entry shipped anyway.
    """

    def test_committed_manifests_all_have_a_source(self):
        manifests = sorted((ROOT / "install").glob("*.json"))
        self.assertTrue(manifests, "no install manifests to check")
        for path in manifests:
            with self.subTest(manifest=path.name):
                data = json.loads(path.read_text())
                orphans = [
                    entry["dest"]
                    for entry in data.get("files", []) + data.get("case_variants", [])
                    if not entry.get("repo_path") and not entry.get("release_asset")
                ]
                self.assertEqual(
                    orphans, [], f"{path.name} lists unfetchable files: {orphans[:3]}"
                )

    def test_an_unresolvable_file_is_recorded_as_omitted(self):
        """The reason must be one install.py knows how to report."""
        for path in sorted((ROOT / "install").glob("*.json")):
            with self.subTest(manifest=path.name):
                data = json.loads(path.read_text())
                for entry in data.get("omitted_files", []):
                    self.assertIn(entry.get("reason"), install.OMISSION_REASONS)

    def test_the_installer_accepts_every_published_manifest(self):
        """A reason the schema allowed and the installer did not made the
        installer refuse the whole RomM manifest: no RomM install ran."""
        for path in sorted((ROOT / "install").glob("*.json")):
            with self.subTest(manifest=path.name):
                data = json.loads(path.read_text())
                install._validate_manifest(data, data["platform"])

    def test_schema_and_installer_name_the_same_reasons(self):
        schema = json.loads(
            (ROOT / "schemas" / "install-manifest.schema.json").read_text()
        )
        reasons = schema["properties"]["omitted_files"]["items"]["properties"][
            "reason"
        ]["enum"]
        self.assertEqual(set(reasons), set(install.OMISSION_REASONS))

    def _manifests(self) -> list[Path]:
        paths = sorted((ROOT / "install").glob("*.json"))
        paths += sorted((ROOT / "install" / "targets").glob("*.json"))
        self.assertTrue(paths, "no install manifests to check")
        return paths

    def test_repo_paths_name_the_file_the_database_holds(self):
        """A manifest written before a file moved sends the installer to 404.

        database.json is regenerated whenever a file is refiled; the manifests
        are a separate artefact and were left behind once (the two DS
        firmware dumps of the 7 September refile). The installer fetches
        repo_path from raw.githubusercontent.com, so a stale one is a
        download that can never succeed.
        """
        database = ROOT / "database.json"
        if not database.is_file():
            self.skipTest("database.json not generated")
        files = json.loads(database.read_text(encoding="utf-8"))["files"]
        for path in self._manifests():
            with self.subTest(manifest=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                stale = [
                    (entry["dest"], entry["repo_path"], files.get(entry["sha1"], {}).get("path"))
                    for entry in data.get("files", []) + data.get("case_variants", [])
                    if entry.get("repo_path")
                    and files.get(entry.get("sha1", ""), {}).get("path") != entry["repo_path"]
                ]
                self.assertEqual(stale, [], f"{path.name} names moved files: {stale[:3]}")

    def test_release_entries_are_exactly_the_files_git_does_not_hold(self):
        """The installer has two sources, and the manifest must pick the right one.

        A committed file is served by the repository; a gitignored one by
        the large-files release. Marking a committed file as a release asset
        sends the installer to an asset nobody uploaded (sdlpal/5.avi, 83 MB
        in git, was announced as release asset `5.avi`). Marking a gitignored
        file as a repo file sends it to a raw URL that has no content.
        """
        tracked = set(
            subprocess.run(
                ["git", "ls-files", "-z", "--", "bios"],
                capture_output=True, text=True, cwd=ROOT, check=True,
            ).stdout.split("\0")
        )
        if not tracked:
            self.skipTest("not a git checkout")
        for path in self._manifests():
            with self.subTest(manifest=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                wrong = []
                for entry in data.get("files", []) + data.get("case_variants", []):
                    repo_path = entry.get("repo_path")
                    if not repo_path:
                        continue
                    committed = repo_path in tracked
                    if bool(entry.get("release_asset")) == committed:
                        wrong.append((entry["dest"], repo_path, "committed" if committed else "gitignored"))
                self.assertEqual(wrong, [], f"{path.name} picks the wrong source: {wrong[:3]}")


class ReleaseAssetCriterion(unittest.TestCase):
    """What decides whether the installer fetches from the release is git, not size.

    Files over 50 MB are supposed to be gitignored and uploaded to the
    large-files release, but four committed files break that rule and one of
    them reached a manifest as a release asset that does not exist. The
    truthful question is whether the repository serves the bytes, and the
    ledger of what it does not serve is .gitignore.
    """

    def _repo(self, directory: str, ignored: str) -> str:
        Path(directory, ".gitignore").write_text(ignored + "\n", encoding="utf-8")
        return directory

    def test_a_large_committed_file_is_served_by_the_repository(self):
        from scripts import generate_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = self._repo(directory, "bios/elsewhere.bin")
            big = Path(root, "bios", "big.avi")
            big.parent.mkdir()
            with big.open("wb") as handle:
                handle.truncate(60_000_000)
            generate_pack._registered.cache_clear()
            try:
                self.assertFalse(generate_pack._is_release_asset(str(big), root))
            finally:
                generate_pack._registered.cache_clear()

    def test_the_manifest_names_the_asset_as_github_serves_it(self):
        # GitHub publishes a space as a dot; the site linked the dotted
        # name while the manifest gave install.py the spaced one, a 404.
        from scripts import generate_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = self._repo(directory, "bios/MAME/MAME 0.174 Arcade XML.dat")
            target = Path(root, "bios", "MAME", "MAME 0.174 Arcade XML.dat")
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")
            generate_pack._registered.cache_clear()
            try:
                self.assertEqual(
                    generate_pack._release_asset_name(str(target), root),
                    "MAME.0.174.Arcade.XML.dat",
                )
            finally:
                generate_pack._registered.cache_clear()

    def test_a_gitignored_file_is_served_by_the_release_whatever_its_size(self):
        from scripts import generate_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = self._repo(directory, "bios/small.zip")
            small = Path(root, "bios", "small.zip")
            small.parent.mkdir()
            small.write_bytes(b"PK")
            generate_pack._registered.cache_clear()
            try:
                self.assertTrue(generate_pack._is_release_asset(str(small), root))
            finally:
                generate_pack._registered.cache_clear()


class ReleaseAssetsMatchTheCollection(unittest.TestCase):
    """The bytes the release serves must be the bytes the manifests describe.

    install.py checks Content-Length against the manifest size before it
    downloads, and the manifest size is that of the local copy. Both FBNeo
    sample archives were rebuilt locally after their upload and never
    re-uploaded: same members, different container, every install of them
    failed. The comparison is offline here on a fixture; the script runs it
    against the live release.
    """

    def test_missing_and_resized_assets_are_reported_and_nothing_else(self):
        from scripts import check_release_assets

        expected = {
            "bios/Other/fbneo/fbneo/samples/twotiger.zip": 154985212,
            "bios/Arcade/MAME/MAME 0.174 Arcade XML.dat": 53677408,
            "bios/Other/sdlpal/sdlpal/5.avi": 82833972,
        }
        assets = {
            "twotiger.zip": 154888877,
            "MAME.0.174.Arcade.XML.dat": 53677408,
            "unrelated.zip": 1,
        }
        findings = check_release_assets.compare(expected, assets)
        self.assertEqual(
            findings,
            [
                ("missing", "bios/Other/sdlpal/sdlpal/5.avi", 82833972, None),
                ("size", "bios/Other/fbneo/fbneo/samples/twotiger.zip", 154985212, 154888877),
            ],
        )

    def test_expected_assets_are_the_gitignored_database_entries(self):
        from scripts import check_release_assets

        db = {
            "files": {
                "a" * 40: {"path": "bios/Other/x/big.zip", "size": 10},
                "b" * 40: {"path": "bios/Other/x/small.bin", "size": 2},
            }
        }
        gitignore = "# large\nbios/Other/x/big.zip\ntmp/\n"
        self.assertEqual(
            check_release_assets.expected_assets(db, gitignore),
            {"bios/Other/x/big.zip": 10},
        )

    def test_release_notes_are_rendered_from_the_collection(self):
        """The release page is written by hand and drifts; it is rendered instead.

        Three assets had no row, three rows carried the SHA1 of an asset
        that had since been replaced, and the footer counted 41 files for 44.
        Hand-written descriptions survive by name; a new row takes its
        description from the profile that declares the file; an asset the
        collection does not index is listed apart, with its size only.
        """
        from scripts import check_release_assets

        db = {"files": {
            "a" * 40: {"path": "bios/Nintendo/Wii/nand.bin", "size": 553649152},
            "b" * 40: {"path": "bios/Other/fbneo/fbneo/samples/twotiger.zip", "size": 154985212},
            "c" * 40: {"path": "bios/Other/x/small.bin", "size": 2},
        }}
        gitignore = "bios/Nintendo/Wii/nand.bin\nbios/Other/fbneo/fbneo/samples/twotiger.zip\n"
        assets = {"nand.bin": 553649152, "twotiger.zip": 154985212,
                  "sdlpal-data.zip": 140773720, "whoopee.zip": 222665810}
        previous = (
            "## Arcade\n\n| File | Description | Size | SHA1 |\n|---|---|---|---|\n"
            "| [twotiger.zip](u) | Two Tigers samples | 148 MB | `" + "0" * 40 + "` |\n"
            "| [whoopee.zip](u) | Toaplan Whoopee samples | 212 MB | `" + "9" * 40 + "` |\n"
        )
        descriptions = {"nand.bin": "BootMii NAND backup"}
        bundles = {"sdlpal-data.zip": "SDLPAL Chinese Paladin game data (.mkf archives)"}
        body = check_release_assets.render_notes(
            db, gitignore, assets, previous, descriptions, bundles, {"sdlpal-data.zip": "d" * 40}
        )
        self.assertIn("| [twotiger.zip](https://github.com/Abdess/retrobios/releases/download/large-files/twotiger.zip) | Two Tigers samples | 148 MB | `" + "b" * 40 + "` |", body)
        self.assertIn("| [nand.bin](https://github.com/Abdess/retrobios/releases/download/large-files/nand.bin) | BootMii NAND backup | 528 MB | `" + "a" * 40 + "` |", body)
        self.assertIn("| [sdlpal-data.zip](https://github.com/Abdess/retrobios/releases/download/large-files/sdlpal-data.zip) | SDLPAL Chinese Paladin game data (.mkf archives) | 134 MB | `" + "d" * 40 + "` |", body)
        self.assertIn("## Not indexed", body)
        self.assertIn("| [whoopee.zip](https://github.com/Abdess/retrobios/releases/download/large-files/whoopee.zip) | Toaplan Whoopee samples | 212 MB |", body)
        self.assertNotIn("9" * 40, body)
        self.assertNotIn("small.bin", body)
        self.assertIn("4 files, 1.0 GB total", body)
        self.assertEqual(body.index("## Console firmware"), min(
            body.index(h) for h in ("## Console firmware", "## Arcade", "## Not indexed")
        ))

    def test_stale_notes_are_a_finding(self):
        from scripts import check_release_assets

        findings = check_release_assets.compare({}, {}, notes_current=False)
        self.assertEqual(findings, [("notes", "release description", 0, None)])

    def test_pipeline_runs_the_check_online_and_marks_it_skipped_offline(self):
        """Run with each step recorded instead of executed: reading the
        source for two strings stayed green with the guard inverted."""
        import contextlib
        import io
        from unittest import mock

        from scripts import pipeline

        def steps(*flags: str) -> list[str]:
            ran: list[str] = []

            def record(cmd, label):
                ran.append(label)
                return True, ""

            argv = ["pipeline.py", "--skip-packs", "--skip-docs", *flags]
            with mock.patch.object(pipeline, "run", record), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    self.assertRaises(SystemExit) as done:
                pipeline.main()
            self.assertEqual(done.exception.code, 0)
            return ran, out.getvalue()

        online, _ = steps()
        self.assertIn("2b2 check release assets", online)
        offline, printed = steps("--offline")
        self.assertNotIn("2b2 check release assets", offline)
        self.assertIn("check_release_assets.................... SKIPPED", printed)


class OneRefreshPerOnlineRun(unittest.TestCase):
    """An online run refreshed the data trees at step 2, again in the pack
    build and again in the manifest build: a moving tree (fbneo-cheats on
    master) could change between the two, and the pack and the manifest
    described different revisions."""

    def test_the_builds_reuse_step_twos_trees(self):
        import contextlib
        import io
        from unittest import mock

        from scripts import pipeline

        commands: dict[str, list[str]] = {}

        def record(cmd, label):
            commands[label] = cmd
            return True, ""

        # A scratch output directory: step 4 purges the ZIPs of the one it
        # is given, and this run must never touch dist/.
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as out, \
                mock.patch.object(pipeline, "run", record), \
                mock.patch.object(
                    sys, "argv", ["pipeline.py", "--skip-docs", "--output-dir", out]
                ), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            pipeline.main()
        self.assertIn("2/8 refresh data directories", commands)
        for label in ("4/8 generate packs", "4b/8 generate install manifests"):
            with self.subTest(step=label):
                self.assertIn("--no-refresh-data", commands[label])
                self.assertNotIn("--offline", commands[label])


class PreservedBytesAreNeverNormalised(unittest.TestCase):
    """git must not rewrite a preserved file's line endings.

    SHA1 is the primary key of this collection. Git for Windows sets
    core.autocrlf=true by default, so without an attribute saying otherwise a
    clone there rewrites every file git guesses is text -- shaders, .ini,
    .txt and .dat assets under bios/ -- and each one arrives with a different
    hash from the one published here.
    """

    def _attr(self, path: str) -> str:
        import subprocess

        out = subprocess.run(
            ["git", "check-attr", "text", "--", path],
            capture_output=True, text=True, cwd=ROOT,
        ).stdout
        return out.rsplit(":", 1)[-1].strip()

    def test_gitattributes_exists(self):
        self.assertTrue(
            (ROOT / ".gitattributes").is_file(),
            "without it git guesses, and guesses wrong on Windows",
        )

    def test_collection_paths_are_exempt_from_normalisation(self):
        for path in (
            "bios/Sony/PlayStation/scph5501.bin",
            "bios/Other/j2me-loader/color.fsh",
            "data/anything.txt",
            "data/dolphin-sys/config.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self._attr(path), "unset", f"{path} may be rewritten")

    def test_generated_artefacts_stay_lf(self):
        for path in (
            "database.json",
            "scripts/dedup.py",
            "README.md",
            "provenance/redump.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self._attr(path), "set")


class FreshnessGuardMechanics(unittest.TestCase):
    """write_if_changed is what makes `git diff --exit-code` a real check.

    deploy-site.yml regenerates README.md and CONTRIBUTING.md and then fails
    if git sees a change. That is only a staleness check because a run which
    moves nothing but the clock leaves the file untouched; if the comparison
    missed a timestamp form, the guard would fail on every run and stop
    meaning anything.
    """

    def setUp(self):
        from common import write_if_changed

        self.write_if_changed = write_if_changed
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "page.md")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_new_file_is_written(self):
        self.assertTrue(self.write_if_changed(self.path, "body\n"))
        with open(self.path) as handle:
            self.assertEqual(handle.read(), "body\n")

    def test_identical_content_is_not_rewritten(self):
        self.write_if_changed(self.path, "body\n")
        self.assertFalse(self.write_if_changed(self.path, "body\n"))

    def test_real_change_is_written(self):
        self.write_if_changed(self.path, "body\n")
        self.assertTrue(self.write_if_changed(self.path, "other\n"))

    def test_every_timestamp_form_is_ignored_on_its_own(self):
        forms = [
            '{{"generated_at": "{}"}}',
            '{{"imported_at": "{}"}}',
            "*Auto-generated on {}*",
            "*Generated on {}*",
            '<div class="rb-timestamp">Generated on {}.</div>',
        ]
        for form in forms:
            with self.subTest(form=form):
                first = form.format("2026-01-01T00:00:00Z")
                second = form.format("2026-09-09T09:09:09Z")
                self.write_if_changed(self.path, first)
                self.assertFalse(
                    self.write_if_changed(self.path, second),
                    f"a clock-only change rewrote the file for {form!r}",
                )

    def test_a_change_beside_a_moving_timestamp_is_still_written(self):
        self.write_if_changed(self.path, "count: 1\n*Generated on A*\n")
        self.assertTrue(
            self.write_if_changed(self.path, "count: 2\n*Generated on B*\n")
        )

    def test_the_written_file_keeps_the_new_timestamp_when_content_changed(self):
        self.write_if_changed(self.path, "count: 1\n*Generated on A*\n")
        self.write_if_changed(self.path, "count: 2\n*Generated on B*\n")
        with open(self.path) as handle:
            self.assertIn("Generated on B", handle.read())



class AMissingDependencyStopsTheBuild(unittest.TestCase):
    """generate_db read every alias inside `try: import yaml ... except
    ImportError: pass`: on an interpreter without pyyaml the database lost
    3186 alias lines and the run exited 0."""

    def test_generate_db_without_pyyaml_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "database.json"
            proc = subprocess.run(
                [sys.executable, "-S", "scripts/generate_db.py", "--force",
                 "--bios-dir", "bios", "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("PyYAML required", proc.stderr)
            self.assertFalse(output.exists())

    def test_no_script_swallows_an_import_error(self):
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ExceptHandler)
                    and isinstance(node.type, ast.Name)
                    and node.type.id in ("ImportError", "ModuleNotFoundError")
                    and all(isinstance(stmt, ast.Pass) for stmt in node.body)
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])


class TestsCleanUpTheirScratch(unittest.TestCase):
    """Sixteen classes called mkdtemp() and kept the directory: each suite
    run left about 400 of them on the RAM-backed /tmp."""

    def test_every_class_that_makes_a_directory_removes_it(self):
        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            for node in ast.parse(source).body:
                if not isinstance(node, ast.ClassDef):
                    continue
                body = ast.get_source_segment(source, node) or ""
                if "mkdtemp(" in body and not any(
                    word in body for word in ("rmtree", "addCleanup", "tearDown")
                ):
                    offenders.append(f"{path.name}:{node.name}")
        self.assertEqual(offenders, [])


class PipelineReportsWhatItDid(unittest.TestCase):
    """A step that did not run must not be summarised as OK.

    --offline skips the network steps and --with-export gates three more.
    Reporting them as OK claims work nobody did, and a run whose export
    never happened read exactly like one where it had.
    """

    def test_a_skipped_step_is_not_reported_as_done(self):
        import pipeline

        self.assertTrue(bool(pipeline.SKIPPED), "a skip must not fail the run")
        self.assertEqual(repr(pipeline.SKIPPED), "SKIPPED")
        self.assertIsNot(pipeline.SKIPPED, True)

    def test_every_skip_branch_uses_the_sentinel(self):
        source = (ROOT / "scripts" / "pipeline.py").read_text()
        stale = re.findall(r'results\["(\w+)"\] = True', source)
        self.assertEqual(stale, [], f"steps still claiming OK when skipped: {stale}")

class DeclaredDependenciesMatchTheDocumentedWorkflows(unittest.TestCase):
    """What the workflows install has to be what the project declares.

    mkdocs-material and pymdown-extensions were hard requirements of the site
    build in CI and in the release guide while pyproject declared neither, so
    a contributor following either had to read the workflow to find out.
    """

    @staticmethod
    def _pyproject() -> dict:
        import tomllib

        with (ROOT / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)

    def test_every_package_ci_installs_is_declared(self):
        extras = self._pyproject()["project"]["optional-dependencies"]
        declared = {
            name.split(">")[0].split("=")[0].split("<")[0].strip('"')
            for group in extras.values()
            for name in group
        }
        declared |= set(self._pyproject()["project"]["dependencies"])
        installed: set[str] = set()
        for workflow in ("validate.yml", "deploy-site.yml"):
            body = (ROOT / ".github" / "workflows" / workflow).read_text(
                encoding="utf-8"
            )
            for line in body.splitlines():
                if "pip install" not in line:
                    continue
                for token in line.split("pip install", 1)[1].split():
                    name = token.strip('"').split(">")[0].split("=")[0]
                    name = name.split("<")[0].strip()
                    if name:
                        installed.add(name)
        self.assertEqual(
            installed - declared,
            set(),
            "CI installs packages pyproject does not declare",
        )


class ACheckThatCannotAnswerDoesNotPass(unittest.TestCase):
    """Exiting zero says the question was answered and the answer was yes.

    Four scripts said that without answering: a refresh that reached no
    remote, a freshness check whose upstream was unreachable, a restore whose
    cache could not supply a declared path, and a pack verification asked
    about one platform whose pack was not there.
    """

    def test_naming_a_platform_with_no_pack_is_not_a_pass(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            named = subprocess.run(
                [sys.executable, "scripts/generate_pack.py", "--platform",
                 "retroarch", "--verify-packs", "--output-dir", directory],
                capture_output=True, text=True, cwd=str(ROOT), timeout=300,
            )
            self.assertNotEqual(
                named.returncode, 0,
                "a named platform with no pack reported success:\n"
                + named.stdout + named.stderr,
            )
            # --all is a sweep: a platform nobody built is out of scope.
            swept = subprocess.run(
                [sys.executable, "scripts/generate_pack.py", "--all",
                 "--verify-packs", "--output-dir", directory],
                capture_output=True, text=True, cwd=str(ROOT), timeout=300,
            )
            self.assertEqual(swept.returncode, 0, swept.stdout + swept.stderr)

    def test_an_unsatisfiable_declared_path_is_reported(self):
        from scripts.restore_large_files import restore

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            (root / ".gitignore").write_text("bios/absent.bin\n", encoding="utf-8")
            (root / "database.json").write_text(
                json.dumps({"files": {"a" * 40: {"path": "bios/absent.bin"}}}),
                encoding="utf-8",
            )
            cwd = os.getcwd()
            os.chdir(root)
            try:
                restored, unsatisfied = restore(
                    str(cache), "database.json", ".gitignore"
                )
            finally:
                os.chdir(cwd)
            self.assertEqual(restored, 0)
            self.assertEqual(unsatisfied, ["bios/absent.bin"])

    def test_an_unreachable_buildbot_is_not_a_fresh_verdict(self):
        source = (ROOT / "scripts" / "check_buildbot_system.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'if report.get("error") or pending(report):',
            source.split("def main(")[-1],
            "main() ignores the error the report carries",
        )

    def test_a_missing_profile_directory_says_so(self):
        import io as _io

        from scripts import common as _common

        stderr = _io.StringIO()
        with contextlib.redirect_stderr(stderr):
            profiles = _common.load_emulator_profiles(
                str(ROOT / "no-such-emulator-dir")
            )
        self.assertEqual(profiles, {})
        self.assertIn("no emulator profile directory", stderr.getvalue())


class TestEntryPointsRunEveryClass(unittest.TestCase):
    """`unittest.main()` has to sit after the last test class.

    In this file it sat in the middle, so running it directly discovered only
    the classes defined above it: eight cases never ran that way, while
    `python -m unittest discover` ran all of them. The two roads have to agree.
    """

    def test_no_module_calls_main_before_its_last_class(self):
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            # Column zero only: the same text appears inside this very test as
            # a string literal, and a substring search matched that instead.
            entry = next(
                (n for n, line in enumerate(lines)
                 if line.startswith("if __name__ ==")),
                None,
            )
            if entry is None:
                continue
            last_class = max(
                (n for n, line in enumerate(lines) if line.startswith("class ")),
                default=-1,
            )
            self.assertGreater(
                entry,
                last_class,
                f"{path.name} calls unittest.main() before its last class, so "
                "running the file directly skips what follows",
            )


class ScriptsImportThreeWays(unittest.TestCase):
    """A script is run directly, run as a module, and imported as a package.

    The three do not agree on what is on the path: only the first form adds
    the scripts directory. Adding the package marker without the bootstrap
    made `import scripts.common` fail on the first sibling import it reached.
    """

    def _run(self, *args: str):
        import subprocess

        return subprocess.run(
            [sys.executable, *args],
            capture_output=True, text=True, cwd=str(ROOT), timeout=300,
        )

    def test_imported_as_a_package(self):
        result = self._run(
            "-c", "import scripts.common, scripts.verify, scripts.generate_pack"
        )
        self.assertEqual(result.returncode, 0, result.stderr[-400:])

    def test_run_as_a_module(self):
        result = self._run("-m", "scripts.scraper.libretro_scraper", "--help")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])

    def test_run_as_a_script(self):
        result = self._run("scripts/list_platforms.py")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])


class ContributorsSurviveAFailedRequest(unittest.TestCase):
    """The one part of the README that comes from the network.

    A refused or rate-limited request left the list empty, which deleted the
    section from a published README. It happened during a pipeline run and
    the result was committed, after which the freshness check regenerated the
    section and failed on the difference. Losing the list is a worse answer
    than publishing a stale one.
    """

    def _block(self, text: str):
        import tempfile

        sys.path.insert(0, str(ROOT / "scripts"))
        from generate_readme import _existing_contributor_block

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(text)
            name = handle.name
        try:
            return _existing_contributor_block(name)
        finally:
            os.unlink(name)

    def test_a_published_list_is_read_back(self):
        text = (
            "# Title\n\n## Contributors\n\n"
            '<a href="https://github.com/a"><img src="x" alt="a"></a>\n'
            '<a href="https://github.com/b"><img src="y" alt="b"></a>\n\n'
            "## Community tools\n\nsomething else\n"
        )
        block = self._block(text)
        self.assertEqual(block[0], "## Contributors")
        self.assertIn('alt="a"', "\n".join(block))
        self.assertIn('alt="b"', "\n".join(block))
        self.assertNotIn("Community tools", "\n".join(block))
        self.assertNotIn("something else", "\n".join(block))

    def test_a_readme_without_the_section_yields_nothing(self):
        self.assertEqual(self._block("# Title\n\n## Other\n\ntext\n"), [])

    def test_an_unreadable_file_yields_nothing(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from generate_readme import _existing_contributor_block

        self.assertEqual(_existing_contributor_block("/nonexistent/README.md"), [])

    def test_the_committed_readme_still_carries_its_contributors(self):
        """Regenerating offline must never be the reason this disappears."""
        text = (ROOT / "README.md").read_text()
        # Missing is the failure this guards, never a reason to skip.
        self.assertIn("## Contributors", text)
        self.assertGreater(
            text.count('<a href="https://github.com/'), 0,
            "the section is present but empty",
        )


class APackNameFitsAFileName(unittest.TestCase):
    """--system snes joined 23 emulator names and died on ENAMETOOLONG
    before writing anything, while verify --system snes reported."""

    def test_a_long_selection_is_bounded_and_told_apart(self):
        from scripts.generate_pack import MAX_PACK_NAME_BYTES, _bounded_pack_name

        names = [f"Emulator_Number_{i}" for i in range(23)]
        long_name = _bounded_pack_name(names, names, "_BIOS_Pack.zip")
        self.assertLessEqual(len(long_name.encode()), MAX_PACK_NAME_BYTES)
        self.assertTrue(long_name.startswith("Emulator_Number_0_and_22_more_"))
        other = _bounded_pack_name(names, names[:-1] + ["x"], "_BIOS_Pack.zip")
        self.assertNotEqual(long_name, other)
        self.assertEqual(_bounded_pack_name(["A", "B"], ["a", "b"], ".zip"), "A_B.zip")

    def test_a_long_system_list_is_bounded(self):
        """Thirty systems joined made a 261-byte platform pack name."""
        from scripts.generate_pack import MAX_SYSTEM_TAG_BYTES, _system_tag

        systems = [f"vendor-console-number-{i}" for i in range(30)]
        tag = _system_tag(systems)
        self.assertLessEqual(len(tag.encode()), MAX_SYSTEM_TAG_BYTES + 1)
        self.assertNotEqual(tag, _system_tag(systems[:-1] + ["other-console"]))
        self.assertEqual(_system_tag(["sony-playstation"]), "_Playstation")

    def test_two_hash_lists_get_two_names(self):
        """--from-md5 named its pack after the context only: a second list
        overwrote the first pack in the same directory."""
        source = (ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        self.assertIn('_Custom_{selection}', source)

    def test_the_system_pack_names_itself(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        self.assertIn("pack_name=final_name", source)


class AGroupPackIsWrittenUnderItsOwnName(unittest.TestCase):
    """The RetroArch + Lakka pack was written as RetroArch_..._BIOS_Pack.zip
    and renamed: a `--platform retroarch` pack already in the directory was
    overwritten then carried away, and os.rename refuses an existing target
    on Windows."""

    def test_no_pack_is_renamed_after_its_build(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        self.assertNotIn("os.rename(", source)
        self.assertIn("pack_name=group_name", source)


class ARegionDroppedExtraBlocksNothing(unittest.TestCase):
    """The builder skips a region-dropped core extra before placing it;
    the report placed it anyway, so on a case-folding platform it hid the
    next core's FOO.ROM that the pack does carry."""

    def test_the_report_skips_what_the_region_pass_drops(self):
        from unittest import mock

        from scripts import packextras

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            other = Path(directory) / "FOO.ROM"
            other.write_bytes(b"other bytes")
            extras = [
                {"name": "foo.rom", "destination": "foo.rom", "source_emulator": "a"},
                {"name": "FOO.ROM", "destination": "FOO.ROM", "source_emulator": "b"},
            ]
            config = {"case_insensitive_fs": True, "base_destination": ""}

            def report(region_drops):
                with mock.patch.object(packextras, "_collect_emulator_extras",
                                       return_value=extras), \
                        mock.patch.object(packextras, "resolve_local_file",
                                          return_value=(str(other), "name_exact")):
                    return packextras.hidden_extras(
                        config, "emulators", {"files": {}}, {}, {}, None, region_drops
                    )

            self.assertTrue(report(None))
            self.assertEqual(report({"foo.rom"}), {})


class AnotherEmulatorsTreeProvesNothing(unittest.TestCase):
    """lrps2 names no hash for pcsx2/resources/GameIndex.yaml; the only
    full-tail copy was ARMSX2's, which counted as path proof and replaced
    the GameIndex RetroArch pins on desktop targets, where ARMSX2 does not
    exist. Upstream PCSX2 took ARMSX2's patches.zip by name the same way."""

    def _db(self, root, files):
        import hashlib

        db = {"files": {}, "indexes": {"by_name": {}, "by_md5": {}, "by_crc32": {},
                                       "by_path_suffix": {}}}
        for relative, payload in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            sha1 = hashlib.sha1(payload).hexdigest()
            db["files"][sha1] = {"path": relative, "name": path.name, "sha1": sha1,
                                 "size": len(payload)}
            db["indexes"]["by_name"].setdefault(path.name, []).append(sha1)
            parts = relative.split("/")
            for start in range(1, len(parts) - 1):
                db["indexes"]["by_path_suffix"].setdefault(
                    "/".join(parts[start:]), []).append(sha1)
        return db

    def test_neither_the_full_tail_nor_the_name_lands_in_a_foreign_tree(self):
        from scripts.common import resolve_local_file

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            # The collection's layout, read relative to its root.
            previous = os.getcwd()
            os.chdir(root)
            self.addCleanup(os.chdir, previous)
            db = self._db(root, {
                "bios/Other/armsx2/pcsx2/resources/GameIndex.yaml": b"fork",
                "bios/Other/armsx2/pcsx2/resources/patches.zip": b"fork patches",
                "bios/Sony/PlayStation 2/patches.zip": b"upstream patches",
            })
            index, status = resolve_local_file(
                {"name": "GameIndex.yaml", "path": "pcsx2/resources/GameIndex.yaml",
                 "source_profile": "lrps2"},
                db, dest_hint="pcsx2/resources/GameIndex.yaml",
            )
            # Held only in the fork's tree, the file answers by name and
            # proves nothing about this entry.
            self.assertEqual(status, "name_exact")
            patches, _status = resolve_local_file(
                {"name": "patches.zip", "path": "resources/patches.zip",
                 "source_profile": "pcsx2"},
                db, dest_hint="resources/patches.zip",
            )
            self.assertTrue(patches.endswith("PlayStation 2/patches.zip"))

    def test_no_desktop_retroarch_pack_serves_armsx2s_tree(self):
        from scripts import slots
        from scripts.common import (
            load_data_dir_registry,
            load_database,
            load_emulator_profiles,
            load_platform_config,
            load_target_config,
        )

        if not (ROOT / "database.json").exists():
            self.skipTest("database.json is not generated")
        db = load_database(str(ROOT / "database.json"))
        config = load_platform_config("retroarch", str(ROOT / "platforms"))
        profiles = load_emulator_profiles(str(ROOT / "emulators"))
        cores = set(load_target_config("retroarch", "x86_64", str(ROOT / "platforms")))
        overrides = slots.pack_overrides(
            config, profiles, db, None, load_data_dir_registry(str(ROOT / "platforms")),
            target_cores=cores,
        )
        self.assertFalse([d for d, p in overrides.items() if "/armsx2/" in p])


class APinnedCoreFileTakesTheSlotFirst(unittest.TestCase):
    """hypseus-singe asks for gamecontrollerdb.txt by name and took the BIOS
    root of Batocera, RetroBat and RetroPie with C-Dogs' 26 KB table, where
    Solarus pins its own by sha1."""

    def test_the_content_naming_claim_is_placed_first(self):
        from unittest import mock

        from scripts import packextras

        undeclared = [
            {"profile": "hypseus", "emulator": "hypseus", "name": "db.txt",
             "path": "db.txt", "in_repo": True, "systems": ["a"]},
            {"profile": "solarus", "emulator": "solarus", "name": "db.txt",
             "path": "db.txt", "in_repo": True, "systems": ["b"], "sha1": "a" * 40},
        ]
        config = {"systems": {}, "base_destination": "bios"}
        with mock.patch("verify.find_undeclared_files", return_value=undeclared), \
                mock.patch.object(packextras, "resolve_platform_cores",
                                  return_value=set()) as cores:
            extras = packextras._collect_emulator_extras(
                config, "emulators", {"files": {}, "indexes": {}}, set(), "bios", {}
            )
        self.assertEqual([e["source_profile"] for e in extras][:1], ["solarus"])
        # The stub is what ran: patched with create=True on a name the module
        # imported inside the function, it was never read and the real
        # resolve_platform_cores walked the repository's profiles.
        cores.assert_called_once()


class ANameWithItsDirectoryIsTheDestination(unittest.TestCase):
    """gam4980 declares gam4980/E.BIN with no path: resolved by its basename,
    verify --emulator and the emulator pack took the A4980's OS while the
    platform pack served the GAM4980's."""

    def test_the_directory_in_the_name_picks_the_copy(self):
        import hashlib

        from scripts.common import resolve_local_file

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            previous = os.getcwd()
            os.chdir(root)
            self.addCleanup(os.chdir, previous)
            files, by_name, by_suffix = {}, {}, {}
            for relative, payload in (("bios/BBK/A4980/A4980/E.BIN", b"a4980"),
                                      ("bios/BBK/GAM4980/gam4980/E.BIN", b"gam4980")):
                Path(relative).parent.mkdir(parents=True, exist_ok=True)
                Path(relative).write_bytes(payload)
                sha1 = hashlib.sha1(payload).hexdigest()
                files[sha1] = {"path": relative, "name": "E.BIN", "sha1": sha1}
                by_name.setdefault("E.BIN", []).append(sha1)
                parent = relative.split("/")[-2]
                by_suffix.setdefault(f"{parent}/E.BIN", []).append(sha1)
            db = {"files": files, "indexes": {"by_name": by_name, "by_md5": {},
                                              "by_crc32": {}, "by_path_suffix": by_suffix}}
            path, status = resolve_local_file(
                {"name": "gam4980/E.BIN", "source_profile": "gam4980"}, db
            )
            self.assertEqual((path, status), ("bios/BBK/GAM4980/gam4980/E.BIN", "path_exact"))


class AnUnreadableDatabaseStopsTheScan(unittest.TestCase):
    """A database.json left with merge markers read as an absent one: every
    release-asset entry silently left the rewritten database, then the
    manifests."""

    def test_a_corrupt_database_is_not_an_empty_one(self):
        from unittest import mock

        from scripts import generate_db

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = Path(directory) / "database.json"
            path.write_text("<<<<<<< HEAD\n{}\n")
            with mock.patch.object(generate_db, "_load_gitignored_large_files",
                                   return_value={"bios/big.bin"}):
                with self.assertRaises(SystemExit):
                    generate_db._preserve_large_file_entries({}, str(path))
                self.assertEqual(
                    generate_db._preserve_large_file_entries(
                        {}, str(Path(directory) / "absent.json")
                    ),
                    0,
                )


class TheZipIndexCacheKnowsItsInputs(unittest.TestCase):
    """build_zip_contents_index cached on (path, mtime) alone: a smaller
    max_entry_size, or a database giving the same archive another sha1,
    got the first call's index back."""

    def test_the_bound_and_the_sha1_are_part_of_the_key(self):
        from scripts import ziptools

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = Path(directory) / "a.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("big.bin", b"x" * 4096)
            ziptools._zip_contents_cache = None
            db = {"files": {"a" * 40: {"path": str(path)}}}
            self.assertEqual(len(ziptools.build_zip_contents_index(db)), 1)
            self.assertEqual(ziptools.build_zip_contents_index(db, max_entry_size=10), {})
            other = {"files": {"b" * 40: {"path": str(path)}}}
            self.assertEqual(
                set(ziptools.build_zip_contents_index(other).values()), {"b" * 40}
            )
            ziptools._zip_contents_cache = None


class GeneratedModelsAreWrittenWhole(unittest.TestCase):
    """The truth model and the cumulative MAME clone map were written by
    truncate-then-write; a corrupt clone map also read as empty and was
    then overwritten, losing what earlier runs recorded."""

    def test_both_writers_replace_their_file_atomically(self):
        for module in ("generate_truth.py", "dedup.py"):
            source = (ROOT / "scripts" / module).read_text(encoding="utf-8")
            with self.subTest(module=module):
                self.assertIn("write_text_atomic(", source)
                self.assertNotIn('open(out_path, "w")', source)
                self.assertNotIn('open(clone_path, "w")', source)

    def test_an_unreadable_clone_map_stops_the_run(self):
        from scripts import dedup

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            bios = root / "bios"
            # A device clone: two archive names, the same ROMs, in MAME's tree.
            (bios / "Arcade" / "MAME").mkdir(parents=True)
            for name in ("set.zip", "setclone.zip"):
                with zipfile.ZipFile(bios / "Arcade" / "MAME" / name, "w") as archive:
                    archive.writestr("rom.bin", b"rom")
            data = (bios / "Arcade" / "MAME" / "set.zip").read_bytes()
            (bios / "Arcade" / "MAME" / "setclone.zip").write_bytes(data)
            (root / "_mame_clones.json").write_text("<<<<<<< HEAD")
            with self.assertRaises(SystemExit):
                dedup.deduplicate(str(bios), dry_run=False)
            # Stopped before removing anything.
            self.assertTrue((bios / "Arcade" / "MAME" / "setclone.zip").exists())


class APackNoteBesideItsSlotIsANote(unittest.TestCase):
    """The builder writes INSTRUCTIONS_<name>.txt beside the slot it
    explains; verify_pack recognised notes at the ZIP root only and failed
    a pack holding games/keen6/INSTRUCTIONS_egagraph.ck6.txt."""

    def test_a_nested_note_is_not_hashed_against_the_collection(self):
        from scripts.packverify import verify_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = Path(directory) / "Pack.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("games/keen6/INSTRUCTIONS_egagraph.ck6.txt", "File needed")
                archive.writestr("bios/RENAMED_x.bin.txt", "renamed")
            result = verify_pack(str(path), {"files": {}, "indexes": {}})
        self.assertTrue(result[0], result)

    def test_verify_reads_the_rule_where_it_is_defined(self):
        source = (ROOT / "scripts" / "verify.py").read_text(encoding="utf-8")
        self.assertNotIn("from generate_pack import _preferred_entries", source)


class NoHandlerSwallowsEverything(unittest.TestCase):
    """CLAUDE.md forbids `except Exception: pass`; the edit hook missed the
    two-line form and nothing in CI checked it."""

    def test_no_catch_all_handler_only_passes(self):
        import ast

        offenders = []
        for path in [*sorted((ROOT / "scripts").rglob("*.py")), ROOT / "install.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                broad = node.type is None or (
                    isinstance(node.type, ast.Name)
                    and node.type.id in ("Exception", "BaseException")
                )
                if broad and all(isinstance(stmt, ast.Pass) for stmt in node.body):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])


class AnArchiveShipsWholeUnderARegion(unittest.TestCase):
    """verify --emulator dropped kronos' regional stvbios.zip members before
    rebuilding the archive, so its archive held 4 of 14 members and its
    required changed, while the emulator pack ships the archive whole."""

    def test_verify_filters_loose_files_only(self):
        source = (ROOT / "scripts" / "verify.py").read_text(encoding="utf-8")
        self.assertIn(
            'if fe.get("archive")\n                or _resolve_destination(fe, standalone) not in region_drops',
            source,
        )


class APathConflictKeepsTheMainVerdict(unittest.TestCase):
    """The builder's file/directory conflict branch judged a declaration by
    the raw resolver status: a zipped_file archive whose member is right was
    counted excluded while verify counted it OK."""

    def test_the_branch_reads_baseline_verdict(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text(encoding="utf-8")
        branch = source[source.index("Content ships under the conflicting shape"):]
        branch = branch[: branch.index("storage = file_entry.get")]
        self.assertIn("baseline_verdict(", branch)
        self.assertNotIn('conflict_status == "hash_mismatch"', branch)


class TheDatabaseCacheIsWrittenWhole(unittest.TestCase):
    """A crash while serializing left a truncated db_cache.json behind, and
    the next run rehashed the whole collection from scratch."""

    def test_a_failed_save_keeps_the_previous_cache(self):
        import json
        from unittest import mock

        from scripts import generate_db

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = os.path.join(directory, "cache", "db_cache.json")
            generate_db.save_cache(path, {"a": 1})
            with mock.patch.object(json, "dumps", side_effect=TypeError("miscomputed")):
                with self.assertRaises(TypeError):
                    generate_db.save_cache(path, {"a": 2})
            self.assertEqual(generate_db.load_cache(path), {"a": 1})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["db_cache.json"])


class KidVidTapesStayWithTheContent(unittest.TestCase):
    """Stella opens its KidVid tapes by bare name (stella2014, from the
    working directory) or from its base directory (stella2023, the content
    folder): never the system directory. Without load_from every Linux pack
    put about 68 MB of WAVs at the BIOS root, in two cases."""

    def test_no_pack_places_them_in_the_system_directory(self):
        from scripts.common import load_emulator_profiles
        from scripts.validation import outside_gap_scope

        profiles = load_emulator_profiles(str(ROOT / "emulators"))
        for name in ("stella2014", "stella2023"):
            tapes = [f for f in profiles[name]["files"]
                     if f["name"].lower().endswith(".wav")]
            with self.subTest(profile=name):
                self.assertEqual(len(tapes), 7)
                self.assertTrue(all(outside_gap_scope(f, False) for f in tapes))


class ModuleConstantsDeclaredOnce(unittest.TestCase):
    """Splitting common.py into modules emitted some constants twice.

    The values matched, so nothing broke, but the second assignment orphans
    the comment written above the first and leaves two lines to keep in step
    the day a value changes.
    """

    @staticmethod
    def _redeclared(path: Path) -> list[str]:
        seen: dict[str, str] = {}
        again = []
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            value = ast.unparse(node.value)
            if seen.get(target.id) == value:
                again.append(target.id)
            seen[target.id] = value
        return again

    def test_no_module_assigns_the_same_constant_twice(self):
        offenders = {}
        for path in sorted(ROOT.glob("scripts/**/*.py")) + [ROOT / "install.py"]:
            again = self._redeclared(path)
            if again:
                offenders[path.name] = again
        self.assertEqual(offenders, {})


class OnlyTheSchemaValidatorNeedsJsonschema(unittest.TestCase):
    """Build scripts and install.py run on stdlib plus pyyaml; jsonschema is
    tooling for validate_schemas and CI. An import anywhere else turns a
    contributor's pipeline run into an ImportError."""

    @staticmethod
    def _imports_jsonschema(source: str) -> bool:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "jsonschema" for name in names):
                return True
        return False

    def test_no_other_module_imports_it(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in sorted(ROOT.glob("scripts/**/*.py")) + [ROOT / "install.py"]
            if path.name != "validate_schemas.py"
            and self._imports_jsonschema(path.read_text())
        ]
        self.assertEqual(offenders, [])

    def test_a_nested_import_is_seen(self):
        self.assertTrue(
            self._imports_jsonschema("def f():\n    from jsonschema import validate\n")
        )


class HashMergeBackupsAreNotSources(unittest.TestCase):
    """The hash merge copies a profile to <name>.old.yml before rewriting it.

    Every loader skipped the copy except validate_schemas, which then failed
    on a stale MAME profile nobody would ever ship.
    """

    def test_the_validator_skips_a_backup(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from validate_schemas import _validate_yaml_directory
        except ImportError as exc:
            self.skipTest(f"validate_schemas needs jsonschema: {exc}")

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            (Path(directory) / "mame.old.yml").write_text("emulator: [\n")
            self.assertEqual(
                _validate_yaml_directory(Path(directory), "emulator.schema.json"), []
            )

    def test_the_backup_suffix_is_written_once(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in sorted(ROOT.glob("scripts/**/*.py"))
            if path.name != "common.py" and '".old.yml"' in path.read_text()
        ]
        self.assertEqual(offenders, [])



class AnUnreadableArchiveIsSaidOutLoud(unittest.TestCase):
    """resolve_local_file skipped a ZIP it could not open without a word: a
    truncated archive read as a missing dump (not_found) or a wrong one."""

    def test_the_resolver_names_the_archive(self):
        import common  # noqa: PLC0415

        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            broken = Path(tmp) / "neogeo.zip"
            broken.write_bytes(b"PK\x03\x04 truncated")
            sha1 = hashlib.sha1(broken.read_bytes()).hexdigest()
            db = {"files": {sha1: {"path": str(broken), "name": "neogeo.zip", "sha1": sha1,
                                   "md5": "0" * 32, "size": broken.stat().st_size}},
                  "indexes": {"by_name": {"neogeo.zip": [sha1]}, "by_md5": {},
                              "by_crc32": {}, "by_path_suffix": {}}}
            entry = {"name": "neogeo.zip", "md5": "1" * 32, "zipped_file": "sp-s2.sp1"}
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                common.resolve_local_file(entry, db)
        self.assertIn(f"unreadable archive {broken}", stderr.getvalue())


class GeneralUserGSReachesEveryCoreThatNamesIt(unittest.TestCase):
    """The SoundFont was held as dosbox_pure's DOSBOX.SF2 alone: dosbox-staging
    and dosbox-x, which name it GeneralUser-GS.sf2 and GeneralUser_GS.sf2,
    shipped no General MIDI bank."""

    def test_each_name_resolves_to_the_one_file(self):
        import common  # noqa: PLC0415

        if not (ROOT / "database.json").exists():
            self.skipTest("database.json is not generated")
        db = json.loads((ROOT / "database.json").read_text())
        profiles = common.load_emulator_profiles(str(ROOT / "emulators"))
        held = "bios/Other/GeneralUser GS/GeneralUser-GS.sf2"
        for emulator, name in (("dosbox_pure", "DOSBOX.SF2"),
                               ("dosbox-staging", "GeneralUser-GS.sf2"),
                               ("dosbox-x", "GeneralUser_GS.sf2")):
            with self.subTest(emulator=emulator):
                entry = next(f for f in profiles[emulator]["files"] if f["name"] == name)
                path, _status = common.resolve_local_file(
                    {**entry, "source_profile": emulator}, db, dest_hint=entry.get("path", ""))
                self.assertEqual(path, held)


class APlatformPinDoesNotReplaceTheCoresRevision(unittest.TestCase):
    """RetroArch pins ScummVM's kyra.dat and FBNeo's hiscore.dat at older
    revisions. Collected as variants with profiles that claimed no content,
    the platform's md5 became the only proof and the pack shipped the old
    files to cores built against newer ones."""

    def test_the_profile_revision_serves_the_core(self):
        import common  # noqa: PLC0415

        if not (ROOT / "database.json").exists():
            self.skipTest("database.json is not generated")
        db = json.loads((ROOT / "database.json").read_text())
        profiles = common.load_emulator_profiles(str(ROOT / "emulators"))
        for emulator, name, pinned in (("scummvm", "kyra.dat", "21a57f79886e46f410cfb50186ed8d77"),
                                       ("fbneo", "hiscore.dat", "37fc4e050b53a1ef2c2e7f3042553025")):
            with self.subTest(emulator=emulator):
                entry = next(f for f in profiles[emulator]["files"] if f["name"] == name)
                self.assertTrue(entry.get("sha1"), "the profile claims its revision")
                self.assertIn(pinned, db["indexes"]["by_md5"], "the platform's pin is held")
                path, status = common.resolve_local_file(
                    {**entry, "source_profile": emulator}, db, dest_hint=entry["path"])
                self.assertEqual(status, "sha1_exact")
                self.assertEqual(db["files"][entry["sha1"]]["path"], path)


class PipelineStepsSpeakAsTheyGo(unittest.TestCase):
    """run() captured a step and printed it at the end, stderr after stdout:
    a twenty-minute pack build was silent and a warning lost its place."""

    def test_output_keeps_its_order_and_is_returned(self):
        script = ("import sys; print('one', flush=True); "
                  "print('two', file=sys.stderr, flush=True); print('three')")
        echoed = io.StringIO()
        with contextlib.redirect_stdout(echoed):
            ok, output = pipeline.run([sys.executable, "-c", script], "probe")
        self.assertTrue(ok)
        self.assertEqual(output, "one\ntwo\nthree\n")
        text = echoed.getvalue()
        self.assertLess(text.index("one"), text.index("two"))
        self.assertLess(text.index("two"), text.index("three"))
        self.assertLess(text.index("three"), text.index("--- probe: OK"))


class ADataDirectoryIsWalkedOnce(unittest.TestCase):
    """Each unresolved name walked every data-directory cache again: the
    resolution cost names x tree on every pack build."""

    def test_two_lookups_share_one_walk_and_keep_its_order(self):
        import common  # noqa: PLC0415

        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            cache = Path(tmp) / "cache"
            (cache / "a").mkdir(parents=True)
            (cache / "b").mkdir()
            (cache / "a" / "Font.bin").write_bytes(b"1")
            (cache / "b" / "font.bin").write_bytes(b"2")
            (cache / "b" / "other.bin").write_bytes(b"3")
            registry = {"x": {"local_cache": str(cache)}}
            common.forget_data_dir_index()
            walks = []
            real_walk = os.walk

            def counting_walk(top, *a, **k):
                walks.append(top)
                return real_walk(top, *a, **k)

            with mock.patch.object(common.os, "walk", counting_walk):
                first, _ = common._resolve_in_data_dirs(["font.bin"], registry,
                                                        lambda _p: "name_exact")
                second, _ = common._resolve_in_data_dirs(["other.bin"], registry,
                                                         lambda _p: "name_exact")
            common.forget_data_dir_index()
        self.assertEqual(len(walks), 1)
        expected = sorted(str(p) for p in (cache / "a" / "Font.bin", cache / "b" / "font.bin"))
        self.assertIn(first[0], expected)
        self.assertEqual(second[0], str(cache / "b" / "other.bin"))


class ADuplicateYamlKeyIsRefused(unittest.TestCase):
    """PyYAML kept the last of two equal keys: the registry carried two
    target_source keys for Recalbox and nothing noticed."""

    def test_the_loader_names_the_repeated_key(self):
        from safeparse import yaml_load  # noqa: PLC0415

        with self.assertRaises(yaml.constructor.ConstructorError) as caught:
            yaml_load("platform: x\ntarget_source: a\ntarget_source: b\n")
        self.assertIn("duplicate key 'target_source'", str(caught.exception))
        self.assertEqual(yaml_load("a: &x {b: 1}\nc: *x\n"), {"a": {"b": 1}, "c": {"b": 1}})


class AnAcceptedVersionIsNoCoreDiscrepancy(unittest.TestCase):
    """eduke32 lists the retail and demo NWINTER.GRP under one name: the extra
    carries the first, and a held demo copy is a version the code takes."""

    PROFILE = {"files": [
        {"name": "NWINTER.GRP", "size": 9, "crc32": "00000000",
         "validation": ["size", "crc32"]},
        {"name": "NWINTER.GRP", "size": 4, "crc32": "",
         "validation": ["size", "crc32"]},
    ]}

    def _extra(self) -> dict:
        # The shape find_undeclared_files gives a core extra: source_path
        # defaults to the name when the profile declares no path.
        return {"name": "NWINTER.GRP", "source_name": "NWINTER.GRP",
                "source_path": "NWINTER.GRP", "source_profile": "eduke32"}

    def test_a_sibling_version_matches(self):
        from scripts.generate_pack import _matches_an_alternative

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
            held = Path(tmp) / "NWINTER.GRP"
            held.write_bytes(b"demo")
            profile = json.loads(json.dumps(self.PROFILE))
            profile["files"][1]["crc32"] = f"{zlib.crc32(b'demo'):08x}"
            self.assertTrue(_matches_an_alternative(
                self._extra(), str(held), {"eduke32": profile}))

    def test_a_copy_no_entry_declares_still_disagrees(self):
        from scripts.generate_pack import _matches_an_alternative

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
            held = Path(tmp) / "NWINTER.GRP"
            held.write_bytes(b"other")
            profile = json.loads(json.dumps(self.PROFILE))
            profile["files"][1]["crc32"] = f"{zlib.crc32(b'demo'):08x}"
            self.assertFalse(_matches_an_alternative(
                self._extra(), str(held), {"eduke32": profile}))

    def test_the_pack_consults_the_alternatives(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text()
        self.assertRegex(source, r'status == "hash_mismatch" and not _matches_an_alternative\(')


class ADataDirectoryCopyPassesTheNameChecks(unittest.TestCase):
    """The name steps over bios/ refuse a candidate outside the size or the
    Adler-32 the entry's code checks; a cache copy found by the same name
    was returned without either check."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        cache = self.tmp / "cache"
        cache.mkdir()
        (cache / "wiitdb-pt.txt").write_bytes(b"12345")
        self.registry = {"dolphin-sys": {"local_cache": str(cache)}}
        self.db = {"files": {}, "indexes": {"by_name": {}, "by_md5": {},
                                            "by_path_suffix": {}, "by_crc32": {}}}

    def resolve(self, **fields):
        entry = {"name": "wiitdb-pt.txt", **fields}
        return resolve_local_file(entry, self.db, data_dir_registry=self.registry)

    def test_a_size_the_code_accepts_resolves(self):
        self.assertEqual(self.resolve(size=5, validation=["size"])[1], "data_dir")

    def test_another_size_is_a_contradiction(self):
        self.assertEqual(self.resolve(size=1, validation=["size"])[1], "hash_mismatch")

    def test_another_adler32_is_a_contradiction(self):
        status = self.resolve(validation=["adler32"], known_hash_adler32="0xdeadbeef")[1]
        self.assertEqual(status, "hash_mismatch")


class AMameCloneNameIsANameStep(unittest.TestCase):
    """The clone fallback relaunched resolution on the bare canonical name:
    an unsourceable entry and a size the code checks were both served the
    parent set."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        bios = self.tmp / "bios" / "Arcade"
        bios.mkdir(parents=True)
        parent = bios / "parent.zip"
        parent.write_bytes(b"PARENTSET")
        (self.tmp / "_mame_clones.json").write_text(
            json.dumps({"parent.zip": {"clones": ["clone.zip"]}})
        )
        sha1 = hashlib.sha1(parent.read_bytes()).hexdigest()
        self.db = {
            "bios_dir": str(self.tmp / "bios"),
            "files": {sha1: {"path": str(parent), "name": "parent.zip",
                             "sha1": sha1, "size": 9}},
            "indexes": {"by_name": {"parent.zip": [sha1]}, "by_md5": {},
                        "by_path_suffix": {}, "by_crc32": {}},
        }

    def resolve(self, **fields):
        return resolve_local_file({"name": "clone.zip", **fields}, self.db)

    def test_a_clone_resolves_to_its_parent(self):
        self.assertEqual(self.resolve()[1], "mame_clone")

    def test_an_unsourceable_entry_is_not_served_the_parent(self):
        self.assertEqual(self.resolve(unsourceable="not held"), (None, "not_found"))

    def test_a_size_the_parent_contradicts_is_not_served(self):
        self.assertEqual(
            self.resolve(size=5, validation=["size"]), (None, "not_found")
        )


class ProseCitationsUseTheTrackedForm(unittest.TestCase):
    """profile_sync tracks `path:line` citations only. `libretro.c
    I_FindFile, line 1742` escaped it, and sixteen such citations in six
    profiles stayed behind when their pins moved, four already pointing at
    other code."""

    UNTRACKED = re.compile(
        r"[\w./+-]+\.(?:c|cc|cpp|cxx|h|hh|hpp|inc|cs|py|rs|go|java|kt|js|ts"
        r"|m|mm|s|asm|lua|sh)\b[^\n:]{0,80}?\blines? \d+"
    )

    def test_the_pattern_reads_the_old_forms(self):
        for text in ("libretro.c I_FindFile, line 1742",
                     "memory.c LoadBios() line 1353",
                     "libretro_core_options.h lines 52-64"):
            self.assertRegex(text, self.UNTRACKED)
        self.assertNotRegex("I_FindFile, libretro/libretro.c:4176-4252", self.UNTRACKED)

    def test_no_profile_writes_a_line_number_profile_sync_cannot_see(self):
        found = [
            f"{path.name}:{number}: {match.group(0)}"
            for path in sorted((ROOT / "emulators").glob("*.yml"))
            for number, line in enumerate(path.read_text().splitlines(), 1)
            for match in self.UNTRACKED.finditer(line)
        ]
        self.assertEqual(found, [])


class ACitationInsideASourceRefIsTracked(unittest.TestCase):
    """split_source_ref reads each chunk's located head and drops the rest.
    zesarux cited src/cpu.h:545 inside an annotation, four lines off at its
    pin, and pce-emu joined two citations with ` + `: neither was anchored."""

    def citations(self, ref: str) -> list[str]:
        from scripts.profile_sync import collect_citations

        document = {"files": [{"name": "x.rom", "source_ref": ref}]}
        return [(c.kind, c.ref) for c in collect_citations(document)]

    def test_an_annotation_citation_is_read_as_prose(self):
        found = self.citations(
            "src/cpu.c:3381-3383, src/utils.c:4100 "
            "(MACHINE_IS_SPECTRUM_128_P2 covers Pentagon, src/cpu.h:549)"
        )
        self.assertIn(("prose", "src/cpu.h:549"), found)
        self.assertNotIn(("prose", "src/cpu.c:3381-3383"), found)

    def test_a_joined_citation_is_read_as_prose(self):
        found = self.citations(
            "src/pce_fast/pce.cpp:477-483 + src/pce_fast/huc.cpp:284-304"
        )
        self.assertIn(("prose", "src/pce_fast/huc.cpp:284-304"), found)
        self.assertEqual([c for c in found if c[0] == "prose"],
                         [("prose", "src/pce_fast/huc.cpp:284-304")])

    def test_offsets_match_the_scalar(self):
        from scripts.profile_sync import ref_prose_text

        ref = "a.c:1-2, 5 (see b.c:7)"
        masked = ref_prose_text(ref)
        self.assertEqual(len(masked), len(ref))
        self.assertEqual(masked.index("b.c:7"), ref.index("b.c:7"))
        self.assertNotIn("a.c", masked)


class TriageSurvivesAForgeRefusal(unittest.TestCase):
    """git.eden-emu.dev answers 403 to commit lookups: `--all --triage`
    died on the traceback after half an hour and printed no ranking."""

    def test_the_ranking_is_printed_with_the_profile_marked(self):
        from types import SimpleNamespace

        from scripts import profile_sync

        reports = [
            profile_sync.ProfileReport(name="eden", pin="a" * 40, head="b" * 40),
            profile_sync.ProfileReport(name="vice", pin="c" * 40, head="d" * 40),
        ]
        selected = {
            "eden": {"source": "https://git.eden-emu.dev/eden-emu/eden"},
            "vice": {"source": "https://github.com/libretro/vice-libretro"},
        }

        def check_version(profile, *_args):
            if "eden" in profile["source"]:
                raise profile_sync.upstream.UpstreamError("HTTP 403")
            return None

        args = SimpleNamespace(cache_dir="", offline=True)
        out = io.StringIO()
        with mock.patch.object(profile_sync, "check_version", check_version), \
                mock.patch.object(profile_sync, "select_repo", lambda p: object()), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            profile_sync._print_triage(args, selected, reports)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("eden" in l and "unreachable" in l for l in lines))


class VerifyStrictAppliesInEveryMode(unittest.TestCase):
    """--strict was read only by the platform branch: `--emulator x --strict`
    printed MISSING (required) and exited 0, and the listings accepted it."""

    @classmethod
    def setUpClass(cls):
        if not (ROOT / "database.json").exists():
            raise unittest.SkipTest("database.json is not built")
        cls.dir = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        (cls.dir / "fakeemu.yml").write_text(
            "emulator: FakeEmu\ntype: libretro\nsystems: [zz-console]\nfiles:\n"
            "  - name: does_not_exist_strict.bin\n    required: true\n"
            "    md5: 0123456789abcdef0123456789abcdef\n    validation: [md5]\n"
        )

    @classmethod
    def tearDownClass(cls):
        __import__("shutil").rmtree(cls.dir, True)

    def run_verify(self, *flags: str) -> int:
        return subprocess.run(
            [sys.executable, "scripts/verify.py", *flags], cwd=ROOT,
            capture_output=True, text=True,
        ).returncode

    def test_emulator_and_system_modes_fail_on_a_missing_required_file(self):
        for mode in (["--emulator", "fakeemu"], ["--system", "zz-console"]):
            with self.subTest(mode=mode[0]):
                flags = [*mode, "--emulators-dir", str(self.dir)]
                self.assertEqual(self.run_verify(*flags), 0)
                self.assertEqual(self.run_verify(*flags, "--strict"), 1)

    def test_a_listing_refuses_strict(self):
        self.assertEqual(self.run_verify("--list-emulators", "--strict"), 2)


class OpenMsxRomsSitInItsSystemroms(unittest.TestCase):
    """openMSX entries had no path and landed at the BIOS root, where no
    frontend points FilePool. RetroBat declares its ROMs under
    openMSX/share/systemroms; the profile's paths must agree."""

    def test_every_entry_is_under_share_systemroms(self):
        profile = yaml.safe_load((ROOT / "emulators" / "openmsx.yml").read_text())
        retrobat = yaml.safe_load((ROOT / "platforms" / "retrobat.yml").read_text())
        declared_dirs = {
            fe["destination"].rsplit("/", 1)[0].lower()
            for system in retrobat["systems"].values()
            for fe in system.get("files", [])
            if "systemroms" in fe.get("destination", "")
        }
        self.assertEqual(declared_dirs, {"openmsx/share/systemroms"})
        for entry in profile["files"]:
            with self.subTest(name=entry["name"]):
                self.assertEqual(
                    entry.get("path", "").lower(),
                    f"openmsx/share/systemroms/{entry['name']}".lower(),
                )


class ASameNamedMemberIsNotTheFile(unittest.TestCase):
    """The gap report counted touchhle's LiberationMono-Regular.ttf as held
    because idtech4a++'s extras.zip has a member of that name, another font;
    verify --emulator said missing."""

    def test_a_declared_hash_decides_a_member_or_data_hit(self):
        from scripts import cross_reference as xr

        tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        (tmp / "data").mkdir()
        (tmp / "data" / "font.ttf").write_bytes(b"DATA FONT")
        (tmp / "bios").mkdir()
        with zipfile.ZipFile(tmp / "bios" / "extras.zip", "w") as zf:
            zf.writestr("files/other.ttf", b"MEMBER FONT")
        names = xr._build_supplemental_index(str(tmp / "data"), str(tmp / "bios"))
        locations = xr._supplemental_locations(str(tmp / "data"), str(tmp / "bios"))
        with mock.patch.object(xr, "_supplemental_locations", lambda: locations):
            for fname, data in (("font.ttf", b"DATA FONT"), ("other.ttf", b"MEMBER FONT")):
                with self.subTest(name=fname):
                    held = {"name": fname, "sha1": hashlib.sha1(data).hexdigest()}
                    other = {"name": fname, "sha1": "0" * 40}
                    self.assertTrue(xr._data_hit(fname, None, held, names))
                    self.assertFalse(xr._data_hit(fname, None, other, names))
                    self.assertTrue(xr._data_hit(fname, None, {"name": fname}, names))


class AnEmptyTargetIsNotEveryCore(unittest.TestCase):
    """A board scraped with `cores: []` and `add_cores: [gpsp]`: the pack
    served gpsp's files only, the published list said null (every core)."""

    def test_the_published_list_is_the_builder_reading(self):
        from scripts.common import load_target_config
        from scripts.generate_pack import generate_target_manifests

        tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        targets = tmp / "platforms" / "targets"
        targets.mkdir(parents=True)
        (targets / "demo.yml").write_text(
            "platform: demo\ntargets:\n"
            "  board:\n    architecture: aarch64\n    cores: []\n"
            "  full:\n    architecture: x86_64\n    cores: null\n"
        )
        (targets / "_overrides.yml").write_text(
            "demo:\n  targets:\n    board:\n      add_cores: [gpsp]\n"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            generate_target_manifests(str(targets), str(tmp / "out"))
        published = json.loads((tmp / "out" / "demo.json").read_text())
        builder = load_target_config("demo", "board", str(tmp / "platforms"))
        self.assertEqual(published["board"], sorted(builder))
        self.assertEqual(published["board"], ["gpsp"])
        self.assertIsNone(published["full"])


class ADataDirectoryIsHeldWhileRead(unittest.TestCase):
    """A pack resolved names against data/bluemsx, then a concurrent refresh
    swapped the tree: the index named files that were gone and the copy
    died on FileNotFoundError, or shipped bytes nobody had verified."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def test_the_index_follows_a_swapped_tree(self):
        from scripts import common

        cache = self.tmp / "cache"
        cache.mkdir()
        (cache / "old.rom").write_bytes(b"old")
        self.assertIn("old.rom", common._data_dir_basenames(str(cache)))
        cache.rename(self.tmp / "gone")
        cache.mkdir()
        (cache / "new.rom").write_bytes(b"new")
        index = common._data_dir_basenames(str(cache))
        self.assertIn("new.rom", index)
        self.assertNotIn("old.rom", index)

    def test_a_holder_makes_the_refresh_lock_wait(self):
        import fcntl

        from scripts import refresh_data_dirs

        cache = self.tmp / "bluemsx"
        cache.mkdir()
        lock = cache.with_name(f".{cache.name}.lock")

        def exclusive_free() -> bool:
            with open(lock, "a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                fcntl.flock(handle, fcntl.LOCK_UN)
                return True

        with contextlib.ExitStack() as held:
            refresh_data_dirs.hold_data_dirs(held, {"bluemsx": {"local_cache": str(cache)}})
            self.assertFalse(exclusive_free())
        self.assertTrue(exclusive_free())

    def test_both_entry_points_hold_the_trees(self):
        pack = (ROOT / "scripts" / "generate_pack.py").read_text()
        verify_src = (ROOT / "scripts" / "verify.py").read_text()
        self.assertIn("hold_data_dirs(held,", verify_src)
        refresh = pack.index("results = refresh_all(")
        self.assertIn("hold_data_dirs(held, data_registry)", pack[refresh:])

    def test_verify_packs_alone_holds_the_trees(self):
        # --verify-packs returned before the hold and resolved core files
        # against data/ while a refresh could swap a tree.
        pack = (ROOT / "scripts" / "generate_pack.py").read_text()
        branch = pack.index("if args.verify_packs and not args.all_variants:")
        run = pack.index("_run_verify_packs(args)", branch)
        self.assertIn("hold_data_dirs(held,", pack[branch:run])


class EachRootReadsItsOwnRegistry(unittest.TestCase):
    """The release-asset registry was cached once per process whatever root
    was asked: a second tree got the first tree's .gitignore."""

    def test_two_roots_in_one_process(self):
        from scripts import generate_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as a, \
                tempfile.TemporaryDirectory(dir=TMP_ROOT) as b:
            Path(a, ".gitignore").write_text("bios/one.bin\n")
            Path(b, ".gitignore").write_text("bios/two.bin\n")
            self.assertTrue(generate_pack._is_release_asset(str(Path(a, "bios/one.bin")), a))
            self.assertFalse(generate_pack._is_release_asset(str(Path(b, "bios/one.bin")), b))
            self.assertTrue(generate_pack._is_release_asset(str(Path(b, "bios/two.bin")), b))

    def test_one_parser_reads_the_registry(self):
        for module in ("generate_pack.py", "generate_db.py"):
            source = (ROOT / "scripts" / module).read_text()
            with self.subTest(module=module):
                self.assertIn("load_registered_paths", source)
                self.assertNotIn('startswith("bios/")\n    }', source)



class TheSiteCountsSlotsNotVersions(unittest.TestCase):
    """raze lists every GRP its code accepts under one name: the site said
    59 files, 46 missing, where verify --emulator said 23 slots, 12 missing."""

    def test_one_held_version_fills_the_slot(self):
        from scripts.generate_site import _emulator_file_summary

        files = [
            {"name": "DUKE3D.GRP", "crc32": "1"},
            {"name": "DUKE3D.GRP", "crc32": "2"},
            {"name": "BLOOD.RFF", "crc32": "3"},
        ]
        line = _emulator_file_summary(files, lambda f: f.get("crc32") == "2")[0]
        self.assertIn("**2 files** (3 accepted versions)", line)
        self.assertIn("1 in repo, 1 missing", line)



class EveryReadmeNamesItsFolder(unittest.TestCase):
    """The Lakka pack's README reused RetroArch's guide and never named
    /storage/system, the folder the registry and the installer use."""

    def test_the_registry_folder_is_in_the_guide(self):
        from scripts.packreadme import _build_readme

        registry = yaml.safe_load((ROOT / "platforms" / "_registry.yml").read_text())
        for name, entry in registry["platforms"].items():
            folder = (entry.get("extract_to") or "").rstrip("/")
            if not folder:
                continue
            with self.subTest(platform=name):
                text = _build_readme(name, name, "", 1, 1, bios_paths=[folder])
                self.assertIn(folder, text)
        lakka = _build_readme("lakka", "Lakka", "", 1, 1)
        self.assertNotIn("retroarch/system", lakka)



class NoSourceWarnsWhenCompiled(unittest.TestCase):
    """A docstring naming C:\\RetroArch-Win64 with one backslash compiled to
    an invalid escape: a SyntaxWarning on every run of the installer."""

    def test_every_script_compiles_clean(self):
        import warnings

        sources = [ROOT / "install.py", *sorted((ROOT / "scripts").rglob("*.py"))]
        for path in sources:
            with self.subTest(path=path.name), warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                warnings.simplefilter("error", DeprecationWarning)
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


class TheInstallerNamesWhatItSkipped(unittest.TestCase):
    def test_count_agrees_and_required_files_are_named(self):
        one = install._omission_lines(1, [{"name": "diabdat.mpq", "required": True}])
        self.assertTrue(one[0].startswith("1 file was not installed"))
        self.assertIn("diabdat.mpq", one[1])
        many = install._omission_lines(
            12, [{"name": f"f{i:02}", "required": True} for i in range(12)]
        )
        self.assertTrue(many[0].startswith("12 files were not installed"))
        self.assertIn("and 2 more", many[1])



class EveryToolRefusesAFlagItWouldDrop(unittest.TestCase):
    """The mode x flag matrix existed for generate_pack only: verify took
    --strict in --emulator mode and ignored it, pipeline took --source with
    --skip-packs and built nothing. Each pair here must exit non-zero
    before doing any work."""

    REFUSED = {
        "scripts/verify.py": [
            ["--list-emulators", "--strict"],
            ["--list-emulators", "--verbose"],
            ["--list-emulators", "--json"],
            ["--list-systems", "--region", "us"],
            ["--emulator", "handy", "--target", "switch"],
            ["--platform", "retroarch", "--standalone"],
            ["--platform", "retroarch", "--include-archived"],
        ],
        "scripts/pipeline.py": [
            ["--offline", "--skip-packs", "--source", "truth"],
            ["--offline", "--skip-packs", "--all-variants"],
            ["--offline", "--all-variants", "--source", "platform"],
        ],
    }

    def test_each_pair_is_refused(self):
        for script, cases in self.REFUSED.items():
            for argv in cases:
                with self.subTest(script=script, argv=argv):
                    proc = subprocess.run(
                        [sys.executable, script, *argv], cwd=ROOT,
                        capture_output=True, text=True, timeout=120,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)



class TestsRunOffline(unittest.TestCase):
    def test_the_package_forbids_downloads(self):
        self.assertTrue(os.environ.get("RETROBIOS_OFFLINE"))

    def test_every_documented_run_imports_the_package(self):
        """`discover tests` makes tests/ the top level, so tests/__init__.py,
        which sets RETROBIOS_OFFLINE, never ran: a local run of the documented
        command queried the release and wrote into the shared cache. Only CI,
        which sets the variable itself, was offline."""
        import subprocess

        listed = subprocess.run(
            ["git", "grep", "-n", "unittest discover", "--", ":!tests"],
            capture_output=True, text=True, cwd=ROOT, check=False,
        ).stdout.splitlines()
        self.assertTrue(listed)
        self.assertEqual([line for line in listed if "-t ." not in line], [])



class TheWantedPageCountsRomsetMembers(unittest.TestCase):
    """The site called build_report without archive_members: the public
    Wanted page asked for dumps the collection holds inside romsets."""

    def test_members_are_read_unless_given(self):
        from scripts import provenance_report

        calls = []

        def members(db):
            calls.append(db)
            return {}

        with mock.patch.object(provenance_report, "archive_members", members):
            provenance_report.build_report({"files": {}}, {})
            provenance_report.build_report({"files": {}}, {}, {})
        self.assertEqual(len(calls), 1)



class AutoFetchAsksVerifyWhatIsMissing(unittest.TestCase):
    """auto_fetch judged presence by sha1 or md5 in the database: Batocera's
    pc6001.zip, checked inside the zip and held, was a target, and no core
    file was ever one. 721 announced where verify saw none critical."""

    def test_the_targets_are_verify_verdicts(self):
        from scripts import auto_fetch
        import verify as verify_mod

        config = {"verification_mode": "md5", "systems": {"s": {"files": [
            {"name": "ok.bin", "md5": "1" * 32},
            {"name": "gone.bin", "md5": "2" * 32},
            {"name": "bad.bin", "md5": "3" * 32},
        ]}}}
        result = {
            "details": [
                {"system": "s", "name": "ok.bin", "status": verify_mod.Status.OK},
                {"system": "s", "name": "gone.bin", "status": verify_mod.Status.MISSING},
                {"system": "s", "name": "bad.bin", "status": verify_mod.Status.UNTESTED},
            ],
            "undeclared_files": [
                {"name": "diabdat.mpq", "in_repo": False, "required": True,
                 "system": "diablo", "emulator": "DevilutionX"},
                {"name": "held.bin", "in_repo": True, "required": True},
                {"name": "extra.bin", "in_repo": False, "required": False},
            ],
        }
        with mock.patch.object(verify_mod, "verify_platform", lambda c, d: result):
            names = [m["name"] for m in auto_fetch.find_missing(config, {})]
        self.assertEqual(names, ["gone.bin", "bad.bin", "diabdat.mpq"])



class MessagesSeparateWithAColon(unittest.TestCase):
    """Em-dashes were replaced by " -" in messages: "bios/BLOOD.RFF -raze
    says size mismatch" reads as a flag glued to a file name."""

    PATTERN = re.compile(r"\} -[{a-z]")

    def test_no_message_uses_a_dash_separator(self):
        found = []
        for path in [ROOT / "install.py", *sorted((ROOT / "scripts").rglob("*.py"))]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if self.PATTERN.search(line):
                    found.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(found, [])



class AReaderThatLeavesEarlyIsNoCrash(unittest.TestCase):
    """`verify.py --list-emulators | head -1` ended on a BrokenPipeError
    traceback; every listing and report did."""

    def test_a_closed_pipe_ends_quietly(self):
        proc = subprocess.Popen(
            [sys.executable, "scripts/verify.py", "--list-emulators"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # The listing fits a 64 KiB pipe: a child that wrote it all before
        # the close below never met the broken pipe and exited 0. A 4 KiB
        # pipe keeps it writing when the reader leaves.
        try:
            import fcntl  # noqa: PLC0415

            fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        except (ImportError, AttributeError, OSError):
            pass
        proc.stdout.readline()
        proc.stdout.close()
        _, err = proc.communicate(timeout=120)
        self.assertNotIn(b"Traceback", err)
        self.assertEqual(proc.returncode, 141)

    def test_every_entry_point_goes_through_run_cli(self):
        plain = re.compile(r'if __name__ == "__main__":\n    (main\(\)|sys\.exit\(main\(\)\))')
        found = [p.name for p in sorted((ROOT / "scripts").glob("*.py"))
                 if plain.search(p.read_text())]
        self.assertEqual(found, [])



class TheToolsPageSaysWhatDedupDoes(unittest.TestCase):
    """tools.md said duplicates move to .variants/ (they are deleted) and
    named 2 of the 11 directories dedup leaves alone."""

    def test_every_protected_tree_is_named(self):
        from scripts import dedup

        line = next(
            l for l in (ROOT / "wiki" / "tools.md").read_text().splitlines()
            if l.startswith("| `dedup.py`")
        )
        self.assertNotIn(".variants", line)
        for name in dedup.NODEDUP_DIRS:
            self.assertIn(name, line)



class ThePipelineTableNamesEveryStep(unittest.TestCase):
    """tools.md listed the steps without 1c, 2b2, 3a and 3b."""

    def test_every_labelled_step_is_documented(self):
        labels = set(re.findall(
            r'"(\d[a-z]?\d?)(?:/8)? [a-z]', (ROOT / "scripts" / "pipeline.py").read_text()
        ))
        doc = (ROOT / "wiki" / "tools.md").read_text()
        documented = set(re.findall(r"^\| (\d[a-z]?\d?)(?:/8)? \|", doc, re.MULTILINE))
        self.assertEqual(labels - documented, set())



class TheIssueFormListsEveryPlatform(unittest.TestCase):
    """The missing-BIOS form offered 7 of the 12 registered platforms."""

    def test_every_registered_platform_is_an_option(self):
        from scripts.common import list_registered_platforms, load_platform_config

        form = yaml.safe_load(
            (ROOT / ".github" / "ISSUE_TEMPLATE" / "missing-bios.yml").read_text()
        )
        options = next(
            item["attributes"]["options"] for item in form["body"]
            if item.get("id") == "platform"
        )
        for name in list_registered_platforms(str(ROOT / "platforms"), include_archived=True):
            display = load_platform_config(name, str(ROOT / "platforms"))["platform"]
            self.assertIn(display, options)



class TheTestJobHasTheDataTrees(unittest.TestCase):
    """Packs built without data directories exit non-zero; the CI test job
    had none, and two pack parity tests failed there only."""

    def test_the_trees_are_fetched_before_the_suite(self):
        steps = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "validate.yml").read_text()
        )["jobs"]["run-tests"]["steps"]
        runs = [step.get("run", "") for step in steps]
        fetch = next(i for i, run in enumerate(runs) if "refresh_data_dirs.py" in run)
        suite = next(i for i, run in enumerate(runs) if "unittest discover" in run)
        self.assertLess(fetch, suite)



class VerifyPacksSaysWhatItExcluded(unittest.TestCase):
    """--verify-packs read the result tuple by position, dropped the unsafe
    exclusions (352/353 shown as OK with no reason) and printed 5 errors."""

    def test_the_result_is_unpacked_whole_and_every_error_printed(self):
        source = (ROOT / "scripts" / "generate_pack.py").read_text()
        body = source[source.index("def _run_verify_packs"):]
        body = body[:body.index("\ndef ")]
        self.assertIn("bl_excluded) = result", body)
        self.assertIn("unsafe excluded", body)
        self.assertNotIn("errors[:5]", body)
        self.assertNotRegex(body, r"result\[\d\]")



class TheGapReportKeepsItsShape(unittest.TestCase):
    """Multi-line notes fell to column 0 and read as emulator headings, a
    profile without systems printed `Abuse ()`, `*` had no legend, and
    --platform totals said "across all emulators"."""

    def test_layout(self):
        from scripts.cross_reference import print_report

        report = {"abuse": {
            "emulator": "Abuse", "systems": [], "gaps": 1, "total_files": 1,
            "platform_covered": 0, "gap_missing": 1,
            "gap_details": [{"name": "a.spe", "required": True, "source": "missing",
                             "note": "first line\nsecond line"}],
        }}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_report(report, "the emulators batocera runs")
        text = out.getvalue()
        self.assertIn("* marks a file the emulator requires", text)
        self.assertIn("\nAbuse\n", text)
        self.assertIn("a.spe [MISSING]: first line second line", text)
        self.assertIn("across the emulators batocera runs", text)



class AFileToSourceIsAFileNotAName(unittest.TestCase):
    """The gaps page keyed acquisition targets by name: Fallout 2's
    master.dat disappeared behind Fallout 1's."""

    def test_rows_are_keyed_by_name_and_hash(self):
        source = (ROOT / "scripts" / "generate_site.py").read_text()
        self.assertIn('key = (g["name"], g.get("identity") or f"emulator:{emu_name}")', source)
        from scripts.cross_reference import _first_hash

        self.assertEqual(_first_hash({"md5": ["AB", "cd"]}), "md5:ab")
        self.assertEqual(_first_hash({"crc32": "1", "sha1": "F" * 40}), "sha1:" + "f" * 40)
        self.assertEqual(_first_hash({}), "")



class GeneratedArtifactsMeetTheirSchemas(unittest.TestCase):
    """A manifest field added without its schema passed every local test
    and failed the site deploy, which is the first run of the full check."""

    @unittest.skipUnless(HAS_JSONSCHEMA, "needs jsonschema")
    def test_the_full_schema_check_passes(self):
        proc = subprocess.run(
            [sys.executable, "scripts/validate_schemas.py"], cwd=ROOT,
            capture_output=True, text=True, timeout=600,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)



class AFileCardSaysAllItKnows(unittest.TestCase):
    """Emulator pages cut a set at 10 members without their hashes, never
    showed standalone_path, and gave no file an anchor to link to."""

    def test_members_standalone_path_and_anchor(self):
        from scripts.generate_site import _render_emulator_file

        entry = {
            "name": "neogeo.zip", "path": "neogeo.zip", "category": "bios_zip",
            "standalone_path": "Firmware/neogeo.zip",
            "contents": [{"name": f"m{i}.bin", "size": 1024, "crc32": f"{i:08x}"}
                         for i in range(12)],
        }
        text = "\n".join(_render_emulator_file(
            entry, {"files": [entry]}, None, [entry], lambda f: True, "file-neogeo-zip",
        ))
        self.assertIn('id="file-neogeo-zip"', text)
        self.assertIn('href="#file-neogeo-zip"', text)
        self.assertIn("Standalone path: `Firmware/neogeo.zip`", text)
        self.assertIn("`m11.bin`", text)
        self.assertIn("CRC32 `0000000b`", text)
        self.assertNotIn("more", text)



class EmulatorPagesStateTheirDataForMachines(unittest.TestCase):
    """Every emulator page's JSON-LD differed only by name and url."""

    def test_source_pin_date_and_files(self):
        from scripts.generate_site import emulator_structured_data

        data = emulator_structured_data({
            "emulator": "DuckStation", "source": "https://github.com/stenzek/duckstation",
            "source_commit": "4122fed9", "profiled_date": "2026-03-23",
            "files": [{"name": "scph5501.bin", "md5": "490F666E", "sha1": ["a" * 40]},
                      {"name": "free.bin"}],
        })
        self.assertEqual(data["about"]["codeRepository"], "https://github.com/stenzek/duckstation")
        self.assertEqual(data["about"]["version"], "4122fed9")
        self.assertEqual(data["dateModified"], "2026-03-23")
        self.assertEqual(data["hasPart"][0]["identifier"], ["sha1:" + "a" * 40, "md5:490f666e"])
        self.assertNotIn("identifier", data["hasPart"][1])



class AManifestHashesAFileOnce(unittest.TestCase):
    """Every manifest entry rehashed its file with five algorithms: 70 476
    calls for 15 011 files, 163 s of a 218 s step."""

    def test_verified_hashes_are_reused(self):
        from scripts import generate_pack

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
            path = Path(tmp, "a.bin")
            path.write_bytes(b"abc")
            sha1 = hashlib.sha1(b"abc").hexdigest()
            db = {"files": {sha1: {"path": str(path)}}}
            calls = []
            real = generate_pack.compute_hashes

            def counting(file_path, algorithms=None):
                calls.append(algorithms)
                return real(file_path, algorithms)

            with mock.patch.object(generate_pack, "compute_hashes", counting):
                first = generate_pack._stable_hashes(str(path), db)
                second = generate_pack._stable_hashes(str(path), db)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], frozenset({"sha1", "sha256"}))



class AZipMemberIsHashedOnce(unittest.TestCase):
    """check_inside_zip decompressed a member for every md5 candidate and
    every step: 4 788 times for 355 members in verify --all."""

    def test_repeated_questions_read_the_archive_once(self):
        from scripts import ziptools

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
            archive = Path(tmp, "set.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("ROM.bin", b"abc")
            good = hashlib.md5(b"abc").hexdigest()
            opened = []
            real = ziptools.zipfile.ZipFile

            def counting(*args, **kwargs):
                opened.append(args[0])
                return real(*args, **kwargs)

            with mock.patch.object(ziptools.zipfile, "ZipFile", counting):
                answers = [ziptools.check_inside_zip(str(archive), "rom.bin", md5)
                           for md5 in (good, "0" * 32, good)]
        self.assertEqual(answers, ["ok", "untested", "ok"])
        self.assertEqual(len(opened), 1)



class CompanionsComeFromADirectoryIndex(unittest.TestCase):
    """_companions walked the whole database per scanned image."""

    def test_same_answer_one_walk(self):
        from scripts import packextras

        files_db = {
            "a": {"path": "bios/Sony/PS2/ps2.bin", "name": "ps2.bin"},
            "b": {"path": "bios/Sony/PS2/ps2.bin.rom1", "name": "ps2.bin.rom1"},
            "c": {"path": "bios/Sony/PS2/ps2.nvm", "name": "ps2.nvm"},
            "d": {"path": "bios/Other/ps2.nvm", "name": "ps2.nvm"},
        }
        found = packextras._companions("bios/Sony/PS2/ps2.bin", "ps2.bin", {"rom1", "nvm"}, files_db)
        self.assertEqual([sha1 for sha1, _ in found], ["b", "c"])
        index = packextras._files_by_directory(files_db)
        self.assertIs(packextras._files_by_directory(files_db), index)



class TargetListsReadThePinnedRelease(unittest.TestCase):
    """Each target scraper asked upstream for its latest tag: a Recalbox
    10.2 board list could filter 10.1.1 systems, and Batocera's target file
    did not even record the tag it read."""

    def test_the_revision_comes_from_the_platform_yaml(self):
        from scripts.scraper.targets import BaseTargetScraper

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
            for name, source, tag in (
                ("a", "https://raw.githubusercontent.com/o/r/batocera-43.1/x/y", "batocera-43.1"),
                ("b", "https://gitlab.com/recalbox/recalbox/-/raw/10.1.1/a/b.xml", "10.1.1"),
                ("c", "https://github.com/o/r/blob/v2/f.cs", "v2"),
            ):
                Path(tmp, f"{name}.yml").write_text(f"source: {source}\n")
                self.assertEqual(BaseTargetScraper.pinned_revision(name, tmp), tag)
            Path(tmp, "d.yml").write_text("source: https://example.test/x\n")
            with self.assertRaises(RuntimeError):
                BaseTargetScraper.pinned_revision("d", tmp)

    def test_no_target_scraper_asks_for_the_latest_tag(self):
        for name in ("batocera", "rocknix", "recalbox"):
            source = (ROOT / "scripts" / "scraper" / "targets" / f"{name}_targets_scraper.py").read_text()
            with self.subTest(scraper=name):
                self.assertIn("self.pinned_revision(PLATFORM_NAME)", source)
                self.assertNotIn("fetch_stable_tag()", source)
                self.assertNotIn("fetch_github_latest_version(", source)



class SignaturesCarryTheirTypes(unittest.TestCase):
    """Type hints on every signature is a written rule; 177 had none, the
    shared API among them (artifact locks, the data-dir resolver, the region
    priority list of _select_variants)."""

    # Lowered as modules are annotated; never raised.
    CEILING = 0
    COMPLETE = ("artifacts.py", "common.py", "validation.py", "generate_pack.py")

    @staticmethod
    def _missing(path: Path) -> list[str]:
        found = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            params = [a for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                      if a.arg not in ("self", "cls")]
            params += [a for a in (args.vararg, args.kwarg) if a]
            if node.returns is None or any(a.annotation is None for a in params):
                found.append(f"{path.name}:{node.lineno} {node.name}")
        return found

    def test_the_count_only_goes_down(self):
        paths = [ROOT / "install.py", *sorted((ROOT / "scripts").rglob("*.py"))]
        missing = [m for path in paths for m in self._missing(path)]
        self.assertLessEqual(len(missing), self.CEILING, missing[:20])
        for name in self.COMPLETE:
            with self.subTest(module=name):
                self.assertEqual(self._missing(ROOT / "scripts" / name), [])



class ATreeNobodyHoldsIsMissingNotAnAsset(unittest.TestCase):
    """julius's caesar3/ directory, unexpanded, went to the asset store,
    which answered with .cache/large/ itself: the emulator pack died on
    IsADirectoryError where verify said MISSING."""

    def test_directory_entries_never_reach_the_asset_store(self):
        from scripts import largefiles, packresolve

        db = {"files": {}, "indexes": {"by_name": {}, "by_md5": {}, "by_crc32": {},
                                      "by_path_suffix": {}}}
        for entry in ({"name": "caesar3/", "type": "directory"}, {"name": "caesar3/"}):
            with self.subTest(entry=entry):
                self.assertEqual(
                    packresolve.resolve_file(entry, db, "bios", {}, offline=True),
                    (None, "not_found"),
                )
        self.assertIsNone(largefiles.fetch_large_file("caesar3/", offline=True))
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
            Path(tmp, "sub").mkdir()
            self.assertIsNone(largefiles._fetch_asset("sub", tmp, "", "", offline=True))



class EmuDeckRunsItsEmulatorsStandalone(unittest.TestCase):
    """EmuDeck declared no standalone_cores: Dolphin, Cemu, xemu, ARMSX2 and
    Supermodel, installed as apps by EmuScripts, got the libretro layout and
    lost their standalone-only files."""

    def test_every_emuscripts_emulator_is_standalone(self):
        from scripts.common import load_platform_config, runs_standalone, load_emulator_profiles

        config = load_platform_config("emudeck", str(ROOT / "platforms"))
        self.assertEqual(sorted(config["standalone_cores"]), sorted(config["cores"]))
        profiles = load_emulator_profiles(str(ROOT / "emulators"))
        standalone = set(config["standalone_cores"])
        for name in ("dolphin", "cemu", "armsx2", "supermodel"):
            with self.subTest(emulator=name):
                self.assertTrue(runs_standalone(name, profiles[name], standalone))
        source = (ROOT / "scripts" / "scraper" / "emudeck_scraper.py").read_text()
        self.assertIn('"standalone_cores": list(cores)', source)



class DocumentedHashLookupsFindTheirFile(unittest.TestCase):
    """The --from-md5 example was a test fixture hash: copied from the docs,
    it answered NOT FOUND, which then exited 0."""

    def test_every_documented_lookup_is_in_the_database(self):
        db = json.loads((ROOT / "database.json").read_text(encoding="utf-8"))
        by_md5 = db["indexes"]["by_md5"]
        found = 0
        for page in sorted((ROOT / "wiki").glob("*.md")):
            text = page.read_text(encoding="utf-8")
            for md5 in re.findall(r"--from-md5 ([0-9a-f]{32})\b", text):
                found += 1
                with self.subTest(page=page.name, md5=md5):
                    self.assertIn(md5, by_md5)
        self.assertGreater(found, 0)



class ToolReferenceListsEveryModule(unittest.TestCase):
    """The exporter table described retropie_exporter as a System.dat writer
    and left out bizhawk, misterfpga and rocknix; the target table missed
    recalbox and rocknix. A contributor reads these tables as the map."""

    def _rows(self, heading: str) -> set[str]:
        text = (ROOT / "wiki" / "tools.md").read_text(encoding="utf-8")
        section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
        return set(re.findall(r"^\| `(\w+)` \|", section, re.M))

    def test_every_exporter_has_a_row(self):
        modules = {p.stem for p in (ROOT / "scripts" / "exporter").glob("*_exporter.py")}
        self.assertEqual(self._rows("Exporters"), modules - {"base_exporter"})

    def test_every_target_scraper_has_a_row(self):
        modules = {
            p.stem for p in (ROOT / "scripts" / "scraper" / "targets").glob("*_targets_scraper.py")
        }
        self.assertEqual(self._rows("Target scrapers"), modules)



class ToolReferenceCountsMatchTheirArtifacts(unittest.TestCase):
    """tools.md said 67 derivable entries for 79 and 1,113 identified
    archives for 1,181: counts written by hand drift from the tools."""

    def setUp(self):
        self.text = (ROOT / "wiki" / "tools.md").read_text(encoding="utf-8")

    def test_recipe_store_counts(self):
        store = json.loads((ROOT / "recipes" / "mame.json").read_text(encoding="utf-8"))
        versions = {dat for entry in store["entries"] for dat in entry.get("dats") or [entry["dat"]]}
        listed = sum(len(entry.get("dats") or [entry["dat"]]) for entry in store["entries"])
        match = re.search(
            r"(\d+) MAME versions produce ([\d,]+) entries but only ([\d,]+)\s+distinct",
            self.text,
        )
        self.assertIsNotNone(match)
        stated = [int(group.replace(",", "")) for group in match.groups()]
        self.assertEqual(stated, [len(versions), listed, len(store["entries"])])

    def test_derivable_count(self):
        derivable = json.loads((ROOT / "derivable.json").read_text(encoding="utf-8"))
        match = re.search(r"(\d+) entries on the current snapshots", self.text)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), len(derivable))



class WikiProseHasNoTornSentence(unittest.TestCase):
    """tools.md read "replaces each destination atomically. The shell and /
    Both one-liners verify...": half of an old sentence left in front of
    its rewrite, on the page that explains the installer's trust boundary."""

    STARTERS = {"Both", "The", "This", "It", "A", "An", "Each", "Every", "When",
                "If", "They", "These", "That", "There"}

    def test_no_line_ends_on_a_function_word_before_a_new_sentence(self):
        for page in sorted((ROOT / "wiki").glob("*.md")):
            lines = page.read_text(encoding="utf-8").splitlines()
            in_code = False
            for number, (line, following) in enumerate(zip(lines, lines[1:]), 1):
                if line.startswith("```"):
                    in_code = not in_code
                if in_code:
                    continue
                if (re.search(r"\b(and|or|the|a|an|of|to)$", line.rstrip())
                        and following.split(" ")[0] in self.STARTERS):
                    self.fail(f"{page.name}:{number} ends mid-sentence: {line.strip()!r}")



class EveryOptionIsDocumented(unittest.TestCase):
    """profile_sync's --system, --stale-before and --ref, auto_fetch
    --create-issues, download.py --info and forty more were in no page: a
    maintainer ran --all and spent the API quota that --system would have
    saved. --help lists a flag; only a page says when to reach for it."""

    def test_every_long_option_of_a_tracked_script_is_in_the_docs(self):
        tracked = subprocess.run(
            ["git", "ls-files", "scripts"], capture_output=True, text=True,
            cwd=str(ROOT), check=True,
        ).stdout.split()
        docs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [*sorted((ROOT / "wiki").glob("*.md")),
                         ROOT / "README.md", ROOT / "CONTRIBUTING.md"]
        )
        option = re.compile(r'add_argument\(\s*(?:"-\w",\s*)?"(--[\w-]+)"')
        undocumented = sorted(
            f"{name}: {flag}"
            for name in tracked if name.endswith(".py")
            for flag in set(option.findall((ROOT / name).read_text(encoding="utf-8")))
            if not re.search(re.escape(flag) + r"(?![\w-])", docs)
        )
        self.assertEqual(undocumented, [])



class OfflineHelpSaysWhatItCuts(unittest.TestCase):
    """--offline read "Skip data directory freshness check" while it also
    stops release assets from downloading: on a fresh clone the gitignored
    files were missing from the pack and nothing had announced it."""

    def test_both_helps_name_the_release_assets(self):
        for script in ("generate_pack.py", "pipeline.py"):
            with self.subTest(script=script):
                out = subprocess.run(
                    [sys.executable, f"scripts/{script}", "--help"],
                    capture_output=True, text=True, cwd=str(ROOT), check=True,
                ).stdout
                block = out.split("--offline", 2)[-1].split("\n  --", 1)[0]
                self.assertIn("release", " ".join(block.split()))



class PipelineDocstringNamesThePrintedSteps(unittest.TestCase):
    """The docstring numbered 1 to 9 while the run prints 1/8 to 8/8, and
    had no 2a, 2b, 3a, 3b or 6: a reader of a failed `6/8 pack integrity`
    found nothing under 6."""

    def test_the_two_lists_hold_the_same_steps(self):
        import ast

        source = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
        doc = ast.get_docstring(ast.parse(source))
        listed = set(re.findall(r"^  (\d[a-z0-9]*)\s{2,}", doc, re.M))
        # A step label is "N/8 ..." or a lettered sub-step "2b2 ...".
        printed = {
            step
            for step, of_eight in re.findall(r'(?:"|--- )(\d+[a-z0-9]*)(/8)? [a-zA-Z]', source)
            if of_eight or not step.isdigit()
        }
        self.assertEqual(listed, printed)



class VerifyAllSummaryNamesWhatIsWrong(unittest.TestCase):
    """The closing line counted platform severities only: a required core
    file missing on ROCKNIX was absent from it, and the one warning did not
    say which platform carried it."""

    def test_counts_carry_platforms_and_core_misses(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import verify

        clean = {"severity_counts": {}, "undeclared_files": []}
        warned = {"severity_counts": {verify.Severity.WARNING: 1}, "undeclared_files": []}
        core_miss = {
            "severity_counts": {},
            "undeclared_files": [{
                "name": "n64.psh", "required": True, "in_repo": False,
                "category": "bios", "in_pack": True,
            }],
        }
        summary = verify._overall_summary(
            [(clean, ["RetroArch"]), (warned, ["RetroDECK"]), (core_miss, ["ROCKNIX"])]
        )
        self.assertIn("1 warning (RetroDECK)", summary)
        self.assertIn("ROCKNIX: n64.psh", summary)
        self.assertIn("0 critical,", summary)



class OneOutputFormPerRun(unittest.TestCase):
    """`diff_truth.py --json --format markdown` printed JSON with rc 0, the
    markdown asked for dropped without a word."""

    def test_json_and_markdown_are_refused_together(self):
        for script, argv in (
            ("diff_truth.py", ["--platform", "romm", "--json", "--format", "markdown"]),
            ("diff_truth.py", ["--platform", "romm", "--json", "--markdown"]),
            ("validate_pr.py", ["--json", "--markdown", "bios/Sony/PlayStation/scph5501.bin"]),
        ):
            with self.subTest(script=script, argv=argv):
                proc = subprocess.run(
                    [sys.executable, f"scripts/{script}", *argv], capture_output=True,
                    text=True, cwd=str(ROOT), timeout=120, check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stdout[-300:])
                self.assertIn("not allowed with", proc.stderr)



class BundledFilesAreNotToSource(unittest.TestCase):
    """DaedalusX64's n64.psh, the shader library its renderer installs
    beside the binary, was reported as a required core file missing and
    listed among the files to source, with 19 more like it."""

    def test_verify_does_not_count_it_missing(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import verify

        entry = {"name": "n64.psh", "required": True, "in_repo": False,
                 "category": "bios", "in_pack": True, "bundled": True}
        self.assertEqual(verify._core_required_missing([entry]), [])
        summary = verify._overall_summary(
            [({"severity_counts": {}, "undeclared_files": [entry]}, ["ROCKNIX"])]
        )
        self.assertNotIn("n64.psh", summary)

    def test_the_site_gives_it_its_own_status(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import generate_site

        rows = generate_site._gap_export_rows({}, {"daedalusx64": {
            "emulator": "DaedalusX64", "systems": ["nintendo-64"],
            "gap_details": [{"name": "n64.psh", "source": "missing", "required": True,
                             "in_repo": False, "bundled": True}],
        }})
        self.assertEqual([row["status"] for row in rows], ["bundled"])



class AdlerValuesReadLikeTheDownloads(unittest.TestCase):
    """The Dolphin card read Adler32: `['0x66f334fe', '0xe789b5a5']`, a list
    repr no download spells, and never said the values are taken over the
    byte-swapped ROM, so the raw ea3734fe in files.csv matched neither."""

    def test_values_and_the_swap_are_stated(self):
        from scripts.generate_site import _render_emulator_file

        entry = {"name": "dsp_rom.bin", "path": "dsp_rom.bin",
                 "known_hash_adler32": ["0x66f334fe", "0xE789B5A5"],
                 "adler32_byteswap": True}
        text = "\n".join(_render_emulator_file(
            entry, {"files": [entry]}, None, [entry], lambda f: True, "file-dsp",
        ))
        self.assertIn("`66f334fe`, `e789b5a5`", text)
        self.assertIn("byte-swapped", text)
        self.assertNotIn("['", text)



class GapsPageCountsOneWay(unittest.TestCase):
    """The stats block said 1 untested (by destination) while Problem Files
    listed 4 untested entries, three of them at paths another declaration
    of the same platform already holds."""

    def test_settled_declarations_are_listed_apart(self):
        from scripts.generate_site import _render_problem_files

        problems = [{"platform": "RetroDECK", "name": "dsifirmware.bin", "status": "untested",
                     "required": True, "reason": "x", "discrepancy": ""}]
        settled = [{"platform": "RetroDECK", "name": "cdibios.zip", "status": "untested",
                    "held_by": "arcade"}]
        text = "\n".join(_render_problem_files(problems, settled))
        self.assertIn("0 missing, 1 untested", text)
        self.assertIn("Other revisions of a held destination", text)
        self.assertIn("`cdibios.zip`", text)

    def test_verify_marks_the_settled_entry(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import verify

        result = {"name": "cdibios.zip", "status": verify.Status.UNTESTED}
        self.assertTrue(verify._twin_unmet(result, "arcade"))
        self.assertEqual(result["settled_by"], "arcade")



class UnsourceableCardSaysWhy(unittest.TestCase):
    """DraStic's nds_firmware_modified.bin carried an 'unsourceable' badge
    and nothing else; the reason was only on the gaps page."""

    def test_reason_and_link(self):
        from scripts.generate_site import _render_emulator_file

        entry = {"name": "nds_firmware_modified.bin", "path": "x.bin",
                 "unsourceable": "written by the firmware editor, so no dump of it exists"}
        text = "\n".join(_render_emulator_file(
            entry, {"files": [entry]}, None, [entry], lambda f: False, "file-x",
        ))
        self.assertIn("Not collectable: written by the firmware editor", text)
        self.assertIn("../gaps.md#acknowledged-gaps", text)



class CollectionCardsSayWhereToGetTheFile(unittest.TestCase):
    """A system page card gave the hashes, readers and provenance of
    scph5501.bin and no path or link to the bytes."""

    def test_raw_and_release_asset_urls(self):
        import scripts.generate_site as site

        site._RELEASE_ASSET_NAMES = None
        self.addCleanup(setattr, site, "_RELEASE_ASSET_NAMES", None)
        self.assertEqual(
            site._fetch_url("bios/Sony/PlayStation/scph5501.bin"),
            "https://raw.githubusercontent.com/Abdess/retrobios/main/bios/Sony/PlayStation/scph5501.bin",
        )
        self.assertTrue(site._fetch_url("bios/Arcade/MAME/MAME 0.174 Arcade XML.dat").endswith(
            "/releases/download/large-files/MAME.0.174.Arcade.XML.dat"
        ))



class CollectionCardsCarryFullHashes(unittest.TestCase):
    """A pasted md5 or crc32 found nothing: cards showed 12 characters of
    SHA1 and MD5 (the rest in a title the search does not index) and no
    CRC32 or SHA256."""

    def test_every_hash_in_full(self):
        from scripts.generate_site import FileConsumers, _system_file_card

        entry = {"name": "DUKE.RTS", "path": "bios/x/DUKE.RTS", "size": 10,
                 "sha1": "a" * 40, "md5": "9d29f9673bbdb56068acf7645c13749c",
                 "crc32": "504086c1", "sha256": "b" * 64}
        text = "\n".join(_system_file_card(entry, FileConsumers({}, {})))
        for value in (entry["sha1"], entry["md5"], entry["crc32"], entry["sha256"]):
            self.assertIn(f"`{value}`", text)



class NullReadsAsNone(unittest.TestCase):
    """`upstream: ~` printed `| Upstream | None |` and `bios_filename: null`
    printed `bios_filename: None`: a stated "there is none" read as a gap."""

    def test_page_and_values(self):
        from scripts.generate_site import _plain_value, generate_emulator_page

        self.assertEqual(_plain_value(None), "none")
        page = generate_emulator_page(
            "own", {"emulator": "Own", "type": "standalone", "source": "https://x/own",
                    "upstream": None, "core_version": None, "files": []},
            {"files": {}, "indexes": {}},
        )
        self.assertNotIn("None", page)
        self.assertIn("none: the source is the project itself", page)

    def test_the_rendered_site_has_no_python_none(self):
        docs = ROOT / "docs"
        if not docs.is_dir():
            self.skipTest("docs/ not generated")
        pattern = re.compile(r"\| None \||: None$|: None \|", re.M)
        from scripts.generate_site import LOCAL_NOTE_DIRS

        found = [p.relative_to(docs).as_posix() for p in sorted(docs.rglob("*.md"))
                 if not set(p.relative_to(docs).parts[:-1]) & set(LOCAL_NOTE_DIRS)
                 and pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])



class EmulatorCardsLinkTheCopyAndTheSection(unittest.TestCase):
    """Dolphin's IPL.bin (GC/USA) read "in repo" without naming the copy,
    did not link its collection card, and its platform links landed on the
    page top instead of the GameCube section."""

    def test_copy_and_sections(self):
        from types import SimpleNamespace

        from scripts.generate_site import _render_emulator_file

        sha1 = "ef9194ab4804aa0aa8540d846caf291b28331165"
        consumers = SimpleNamespace(
            sha1_of=lambda entry, dest="", owner="": sha1,
            path_of=lambda value: "bios/Nintendo/GameCube/GC/USA/IPL.bin",
            platforms={sha1: {"batocera"}},
            platform_systems={sha1: {"batocera": {"nintendo-gamecube"}}},
        )
        entry = {"name": "IPL.bin", "path": "GC/USA/IPL.bin"}
        text = "\n".join(_render_emulator_file(
            entry, {"files": [entry]}, consumers, [entry], lambda f: True, "file-ipl", "dolphin",
        ))
        self.assertIn("../systems/nintendo.md#sha1-ef9194ab4804", text)
        self.assertIn("../platforms/batocera.md#nintendo-gamecube", text)



class VersionAnchorsFollowContent(unittest.TestCase):
    """raze's nine DUKE3D.GRP were anchored -1 to -9 by declaration rank: a
    version declared before another moved every later link to another
    edition."""

    @staticmethod
    def _anchors(files):
        from scripts.generate_site import generate_emulator_page

        page = generate_emulator_page(
            "raze", {"emulator": "Raze", "type": "standalone", "files": files},
            {"files": {}, "indexes": {}},
        )
        return re.findall(r'id="(file-[^"]+)"', page)

    def test_inserting_a_version_keeps_the_others(self):
        a = {"name": "DUKE3D.GRP", "path": "DUKE3D.GRP", "crc32": "bbc9ce44"}
        b = {"name": "DUKE3D.GRP", "path": "DUKE3D.GRP", "crc32": "aa4f6a40"}
        c = {"name": "DUKE3D.GRP", "path": "DUKE3D.GRP", "crc32": "982afe4a"}
        before = self._anchors([a, b])
        after = self._anchors([c, a, b])
        self.assertTrue(set(before) <= set(after))

    def test_same_size_versions_are_told_by_description(self):
        v1 = {"name": "AUDIO.ABS", "path": "AUDIO.ABS", "size": 6904, "description": "v1.12"}
        v2 = {"name": "AUDIO.ABS", "path": "AUDIO.ABS", "size": 6904, "description": "v1.13"}
        self.assertEqual(self._anchors([v1, v2]),
                         ["file-audio-abs-6904b-v1-12", "file-audio-abs-6904b-v1-13"])



class SitemapDatesComeFromThePage(unittest.TestCase):
    """All 664 sitemap URLs carried the build date: eduke32, re-read on
    2026-10-04, was announced as changed on the day of every build."""

    def test_the_hook_takes_the_page_date_or_none(self):
        from types import SimpleNamespace

        from scripts.mkdocs_hooks import on_page_markdown

        dated = SimpleNamespace(meta={"date_modified": "2026-10-04"}, update_date="today")
        undated = SimpleNamespace(meta={}, update_date="today")
        on_page_markdown("", dated, None, None)
        on_page_markdown("", undated, None, None)
        self.assertEqual((dated.update_date, undated.update_date), ("2026-10-04", ""))
        config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        self.assertIn("hooks:\n  - scripts/mkdocs_hooks.py", config)


class SiteCodeSpansHoldTheirValue(unittest.TestCase):
    """Values from profiles, platform files and file names went into code
    spans as they were: a backtick in one closed the span and what followed
    was read as markup."""

    def test_a_backtick_cannot_close_the_span(self):
        from scripts.siterender import _code

        span = _code("a`<img src=x onerror=alert(1)>`")
        self.assertEqual(span.count("`"), 2)
        self.assertTrue(span.startswith("`") and span.endswith("`"))



class TheDatasetDescribesItsDownloads(unittest.TestCase):
    """data.md was a Dataset named "Data & API" with no distribution,
    licence, date or creator: nothing a dataset harvester could index."""

    def test_distribution_licence_and_date(self):
        from scripts.generate_site import dataset_structured_data

        exports = [{"title": "Content database", "url": "api/v1/database.json",
                    "media_type": "application/json", "bytes": 10, "sha256": "ab" * 32}]
        data = dataset_structured_data(exports, {"generated_at": "2026-10-18T07:00:00Z"})
        self.assertEqual(data["dateModified"], "2026-10-18")
        self.assertIn("MIT", data["license"])
        (download,) = data["distribution"]
        self.assertEqual(download["@type"], "DataDownload")
        self.assertTrue(download["contentUrl"].endswith("/api/v1/database.json"))
        self.assertEqual(download["sha256"], "ab" * 32)
        self.assertNotEqual(data["name"], "Data & API")



class SystemPagesDescribeTheirFiles(unittest.TestCase):
    """systems/* published a generic TechArticle: no file, hash or
    provenance for the pages that carry a card per file."""

    def test_parts_carry_hashes_anchor_and_provenance(self):
        from scripts.generate_site import system_structured_data

        entry = {"name": "scph5501.bin", "sha1": "0555c6fae8906f3f09baf5988f00e55f88e9f30b",
                 "md5": "490f666e1afb15b7362b406ed1cea246", "crc32": "8d8cb7e4", "size": 524288,
                 "provenance": {"redump": {"dat": "Sony - PlayStation - BIOS Images",
                                           "name": "ps-30a.bin"}}}
        (part,) = system_structured_data("sony", {"PlayStation": [entry]})["hasPart"]
        self.assertTrue(part["url"].endswith("systems/sony/#sha1-0555c6fae890"))
        self.assertIn("md5:490f666e1afb15b7362b406ed1cea246", part["identifier"])
        self.assertIn("redump: Sony - PlayStation - BIOS Images (ps-30a.bin)", part["citation"])

    def test_a_huge_page_lists_its_verified_files_and_points_at_the_table(self):
        from scripts.generate_site import SYSTEM_PARTS_IN_FULL, system_structured_data

        plain = [{"name": f"f{i}", "sha1": f"{i:040x}"} for i in range(SYSTEM_PARTS_IN_FULL)]
        cited = {"name": "v", "sha1": "f" * 40,
                 "provenance": {"tosec": {"dat": "d", "name": "n"}}}
        data = system_structured_data("engines", {"x": [*plain, cited]})
        self.assertEqual([p["name"] for p in data["hasPart"]], ["v"])
        self.assertTrue(data["isBasedOn"].endswith("downloads/files.csv"))



class TheCollectionShipsADat(unittest.TestCase):
    """The exports had JSON, CSV and SQLite and no DAT, the one format
    clrmamepro and RomVault read: auditing a BIOS tree meant writing a
    converter from files.csv."""

    def test_round_trip_through_the_logiqx_parser(self):
        sys.path.insert(0, str(ROOT / "scripts" / "scraper"))
        from logiqx_parser import parse_logiqx
        from scripts.generate_site import collection_dat

        db = {"files": {
            "a" * 40: {"path": "bios/Sony/PlayStation/scph5501.bin", "size": 4,
                       "crc32": "8D8CB7E4", "md5": "1" * 32, "sha1": "a" * 40},
            "b" * 40: {"path": "bios/Nintendo/GameCube/GC/USA/IPL.bin", "size": 8,
                       "crc32": "00000001", "md5": "2" * 32, "sha1": "b" * 40},
        }}
        dat = parse_logiqx(collection_dat(db, "2026-10-18T07:00:00Z"))
        self.assertEqual(dat.version, "20261018")
        roms = {(r.game, r.name): r for r in dat.roms}
        self.assertIn(("Sony - PlayStation", "scph5501.bin"), roms)
        self.assertIn(("Nintendo - GameCube", "GC\\USA\\IPL.bin"), roms)
        self.assertEqual(roms[("Sony - PlayStation", "scph5501.bin")].crc32, "8d8cb7e4")



class ZippedFileMd5IsParsedOnce(unittest.TestCase):
    """verify lowercased a comma md5 list, the builder and packverify did
    not: an uppercase Recalbox-style md5 on a zipped_file entry was OK for
    verify and a mismatch for the pack and the manifest."""

    def test_an_uppercase_member_md5_is_accepted_everywhere(self):
        import hashlib
        import zipfile as zf_mod

        from scripts.validation import inner_rom_check
        from scripts.verify import verify_entry_md5

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            archive = Path(directory, "x.zip")
            with zf_mod.ZipFile(archive, "w") as zf:
                zf.writestr("rom.bin", b"rom")
            member = hashlib.md5(b"rom").hexdigest().upper()
            for md5 in (member, f"{member},{'0' * 32}"):
                with self.subTest(md5=md5):
                    entry = {"name": "x.zip", "zipped_file": "rom.bin", "md5": md5}
                    self.assertEqual(inner_rom_check(entry, str(archive)), "ok")
                    self.assertEqual(
                        verify_entry_md5(entry, str(archive))["status"], "ok"
                    )

    def test_the_three_sites_share_the_parser(self):
        for module in ("validation.py", "packverify.py", "verify.py"):
            source = (ROOT / "scripts" / module).read_text()
            self.assertIn("parse_md5_list(", source, module)



class AHashListIsReadOneWay(unittest.TestCase):
    """mesence declares four sha256 for BS-X.bin as one comma-joined string.
    The resolver split it and shipped the file; the gap report and
    profile_sync looked the whole string up and called it missing."""

    def test_every_reader_splits_the_list(self):
        from scripts.cross_reference import _declared_content
        from scripts.hashing import parse_hash_list
        from scripts.profile_sync import entry_hashes

        a, b = "a" * 64, "B" * 64
        entry = {"name": "BS-X.bin", "sha256": f"{a}, {b}", "crc32": "0xDEADBEEF"}
        self.assertEqual(parse_hash_list(entry["sha256"]), [a, b.lower()])
        self.assertEqual(_declared_content(entry)["sha256"], {a, b.lower()})
        self.assertEqual(_declared_content(entry)["crc32"], {"deadbeef"})
        self.assertEqual(set(entry_hashes(entry)), {a, b.lower(), "deadbeef"})



class RommNeoGeoFoldersGetTheirBios(unittest.TestCase):
    """RomM runs fbneo for neogeoaes and neogeomvs as for arcade and reads
    firmware per platform folder; the fixture gives neogeo.zip to arcade
    alone, so a Neo Geo game in its own folder started without it."""

    def test_the_mirror_is_per_file(self):
        from scripts.scraper.romm_scraper import FIRMWARE_MIRRORS, SLUG_MAP

        arcade = FIRMWARE_MIRRORS["arcade"]
        self.assertEqual(arcade["neogeoaes"], ("neogeo.zip",))
        self.assertEqual(arcade["neogeomvs"], ("neogeo.zip",))
        self.assertIn("neogeoaes", SLUG_MAP)

    def test_a_file_files_under_its_own_system_only(self):
        # Adding the Neo Geo slugs sent MAME 2003's ZN-1 BIOS and data files
        # into neogeoaes/, and FBNeo's CPS3 sets already sat in colecovision/:
        # a system the platform lacks has no folder, and a mirror-only
        # folder attracts no system-less file.
        from scripts.packextras import _slug_for

        config_systems = {"arcade", "snk-neogeo-aes", "nintendo-ds"}
        sys_to_slug = {"arcade": "arcade", "snk-neogeo-aes": "neogeoaes",
                       "nintendo-ds": "nds"}
        profiles = {"mame2003": {"systems": ["snk-neogeo-aes", "zn1-acclaim"]},
                    "melonds": {"systems": ["nintendo-ds", "nintendo-dsi"]}}
        args = (profiles, config_systems, {}, sys_to_slug, {"arcade", "nintendo-ds"})
        self.assertEqual(_slug_for({"profile": "mame2003", "system": "zn1-acclaim"}, *args), "")
        self.assertEqual(_slug_for({"profile": "mame2003"}, *args), "")
        self.assertEqual(_slug_for({"profile": "melonds", "system": "nintendo-dsi"}, *args), "")
        self.assertEqual(_slug_for({"profile": "melonds"}, *args), "nds")

    def test_the_platform_file_carries_it(self):
        text = (ROOT / "platforms" / "romm.yml").read_text(encoding="utf-8")
        self.assertIn("destination: neogeoaes/neogeo.zip", text)
        self.assertIn("destination: neogeomvs/neogeo.zip", text)
        self.assertNotIn("neogeoaes/pgm.zip", text)



class AProfilePathIsTheEmulatorsNotOurs(unittest.TestCase):
    """No guard stopped a profile `path:` naming .variants/, our storage:
    the pack would write the file where no emulator reads."""

    def test_a_variants_path_is_refused(self):
        from unittest import mock

        from scripts import validate_schemas

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            emulators = Path(directory) / "emulators"
            emulators.mkdir()
            (emulators / "x.yml").write_text(
                "emulator: X\nfiles:\n  - name: scph1001.bin\n"
                "    path: .variants/scph1001.bin.abc12345\n"
            )
            with mock.patch.object(validate_schemas, "ROOT", Path(directory)):
                errors = validate_schemas._repository_layout_in_profiles()
        self.assertEqual(len(errors), 1)
        self.assertIn(".variants/", errors[0])


if __name__ == "__main__":
    unittest.main()
