"""The site redeploys whenever a script it runs changes.

Deploy Site listed five scripts by hand while the build imports forty:
a change to siterender.py, which writes every page, published nothing
until some unrelated file moved.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _module_file(name: str) -> Path | None:
    name = name.removeprefix("scripts.")
    for candidate in (
        SCRIPTS / (name.replace(".", "/") + ".py"),
        SCRIPTS / name.replace(".", "/") / "__init__.py",
    ):
        if candidate.exists():
            return candidate
    return None


def _imports(path: Path) -> set[Path]:
    found: set[Path] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.ImportFrom):
            base = path.parent / (node.module or "").replace(".", "/")
            candidates = [base / f"{a.name}.py" for a in node.names]
            candidates += [base.with_suffix(".py"), base / "__init__.py"]
            found.update(c for c in candidates if c.is_file())
            continue
        else:
            continue
        found.update(f for f in map(_module_file, names) if f)
    return found


def closure(entry_points: list[str]) -> set[str]:
    seen: set[Path] = set()
    stack = [SCRIPTS / name for name in entry_points]
    while stack:
        path = stack.pop()
        if path not in seen:
            seen.add(path)
            stack.extend(_imports(path))
    return {str(p.relative_to(REPO_ROOT)) for p in seen}


class DeploySiteTriggers(unittest.TestCase):
    def test_every_script_the_build_imports_triggers_it(self):
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/deploy-site.yml").read_text(encoding="utf-8")
        )
        on = workflow.get("on", workflow.get(True))
        patterns = on["push"]["paths"]
        run = "\n".join(
            step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"]
        )
        entry_points = sorted(
            {
                word.removeprefix("scripts/")
                for word in run.split()
                if word.startswith("scripts/") and word.endswith(".py")
            }
        )
        self.assertIn("generate_site.py", entry_points)
        # mkdocs build runs the hooks mkdocs.yml names: a script of the build.
        # The file carries Python tags safe_load refuses, but the list is plain.
        mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        hooks = re.search(r"^hooks:\n((?:  - .+\n)+)", mkdocs, re.M)
        entry_points += [
            line.strip()[2:].removeprefix("scripts/")
            for line in (hooks.group(1).splitlines() if hooks else [])
            if line.strip()[2:].startswith("scripts/")
        ]
        uncovered = sorted(
            path
            for path in closure(entry_points)
            if not any(fnmatch.fnmatch(path, p.replace("**", "*")) for p in patterns)
        )
        self.assertEqual(uncovered, [])
        # And no script the build no longer reaches: generate_pack.py and six
        # others stayed listed after the site stopped importing the builder,
        # and a push touching only them rebuilt the site for nothing.
        listed = {p for p in patterns if p.startswith("scripts/") and p.endswith(".py")}
        self.assertEqual(sorted(listed - closure(entry_points)), [])
        self.assertNotIn("tests/**", patterns)

    def test_the_site_reads_the_ref_grammar_not_the_checker(self):
        """sitesources imported two parsing functions from profile_sync, so
        every change to the comparison tool rebuilt an unchanged site."""
        self.assertNotIn("scripts/profile_sync.py", closure(["generate_site.py"]))
        self.assertIn("scripts/sourceref.py", closure(["generate_site.py"]))


class DocumentedSteps(unittest.TestCase):
    def test_the_profiling_reference_names_every_profile_field(self):
        """built_by, source_branch, bios_mode, standalone_path and
        on_mismatch change what the tools do and no page named them."""
        import json  # noqa: PLC0415

        schema = json.loads((REPO_ROOT / "schemas/emulator.schema.json").read_text(encoding="utf-8"))
        item = schema["properties"]["files"]["items"]
        if "$ref" in item:
            item = schema.get("$defs", schema.get("definitions", {}))[item["$ref"].split("/")[-1]]
        page = (REPO_ROOT / "wiki/profiling.md").read_text(encoding="utf-8")
        fields = [*schema["properties"], *item.get("properties", {})]
        self.assertEqual(sorted(f for f in fields if f"`{f}`" not in page), [])

    def test_every_option_says_what_it_does(self):
        """--include-archived and --all-variants, and 94 others, showed in
        --help with no text: their constraints surfaced only as errors."""
        sources = [*SCRIPTS.rglob("*.py"), REPO_ROOT / "install.py"]
        silent = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "add_argument"
                    and not any(k.arg == "help" for k in node.keywords)
                ):
                    first = next((a.value for a in node.args if isinstance(a, ast.Constant)), "?")
                    silent.append(f"{path.relative_to(REPO_ROOT)}: {first}")
        self.assertEqual(silent, [])

    def test_the_scraper_recipe_names_every_abstract_method(self):
        """tools.md said implement fetch_requirements(), but validate_format() is
        abstract too, and a scraper written from the page could not be built."""
        source = (SCRIPTS / "scraper" / "base_scraper.py").read_text(encoding="utf-8")
        abstract = re.findall(r"@abstractmethod\s+def (\w+)", source)
        self.assertTrue(abstract)
        for page in ("tools.md", "adding-a-scraper.md"):
            text = (REPO_ROOT / "wiki" / page).read_text(encoding="utf-8")
            with self.subTest(page=page):
                self.assertEqual([m for m in abstract if f"{m}(" not in text], [])

    def test_every_function_the_pages_name_exists(self):
        """verification-modes described _find_best_variant(), deleted on
        2026-10-05, and an algorithm the code no longer follows."""
        # Names the pages cite from the platforms' and emulators' own code,
        # or from Python itself.
        upstream = {"casefold", "checkInsideZip", "path_is_valid", "GAME", "COMP"}
        sources = [*SCRIPTS.rglob("*.py"), REPO_ROOT / "install.py"]
        defined = {
            match
            for path in sources
            for match in re.findall(
                r"^\s*(?:def|class)\s+(\w+)", path.read_text(encoding="utf-8", errors="replace"), re.M
            )
        }
        stale = sorted(
            {
                (page.name, name)
                for page in (REPO_ROOT / "wiki").glob("*.md")
                for name in re.findall(r"`(\w+)\(\)`", page.read_text(encoding="utf-8"))
                if name not in defined and name not in upstream
            }
        )
        self.assertEqual(stale, [])

    def test_the_release_page_names_every_validation_job(self):
        """check-new-profiles ran on every pull request and push while the
        page said four jobs and named none of it."""
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        )
        page = (REPO_ROOT / "wiki/release-process.md").read_text(encoding="utf-8")
        missing = sorted(job for job in workflow["jobs"] if f"**{job}" not in page)
        self.assertEqual(missing, [])
        words = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven"}
        self.assertIn(f"{words[len(workflow['jobs'])]} jobs", page)

    def test_the_release_page_names_every_installed_package(self):
        """The page listed pyyaml and mkdocs and left out jsonschema: a
        maintainer following it could not run the contract check."""
        sys.path.insert(0, str(SCRIPTS))
        from check_freshness import parse_pip_pins  # noqa: PLC0415

        workflow = (REPO_ROOT / ".github/workflows/deploy-site.yml").read_text(encoding="utf-8")
        page = (REPO_ROOT / "wiki/release-process.md").read_text(encoding="utf-8")
        missing = sorted(name for name in parse_pip_pins(workflow) if name not in page)
        self.assertEqual(missing, [])
        self.assertIn("validate_schemas.py", page)


if __name__ == "__main__":
    unittest.main()
