"""CONTRIBUTING.md and the site's contributing page say the same thing.

Two generators wrote the page and drifted: four steps to add a platform in
the repository, five on the site, under two different titles.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_readme  # noqa: E402
import generate_site  # noqa: E402


def _normalized(text: str) -> str:
    text = re.sub(r"\]\([^)]*\)", "](link)", text)
    return "\n".join(text.splitlines()[1:]).replace("on this site", "on the documentation site")


class OneContributingText(unittest.TestCase):
    def test_repository_and_site_pages_match(self):
        self.assertEqual(
            _normalized(generate_readme.generate_contributing()),
            _normalized(generate_site.generate_contributing()),
        )



class CoverageReadsTheGivenProfiles(unittest.TestCase):
    """generate_site --emulators-dir changed the emulator pages but not the
    platform coverage, which read ./emulators whatever was given."""

    def test_the_directory_reaches_verify(self):
        seen: list[str] = []

        def fake_verify(_config, _db, emulators_dir, **_kwargs):
            seen.append(emulators_dir)
            return {"status_counts": {}, "total_files": 0, "undeclared_files": [], "details": []}

        with mock.patch.object(generate_readme, "verify_platform", fake_verify), \
                mock.patch.object(generate_readme, "load_emulator_profiles", return_value={}):
            generate_readme.compute_coverage(
                "retroarch", str(REPO_ROOT / "platforms"), {"files": {}}, emulators_dir="elsewhere"
            )
        self.assertEqual(seen, ["elsewhere"])


class BeforeOpeningAPullRequest(unittest.TestCase):
    """The section asked for the full pipeline: on a fresh fork, without
    data/, it rebuilt 35 GB of packs and then failed on the data
    directories, while CI asks for three checks."""

    def test_it_lists_the_checks_ci_runs_and_not_the_pipeline(self):
        import re as _re

        text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        section = text.split("## Before opening a PR", 1)[1].split("\n## ", 1)[0]
        block = _re.search(r"```bash\n(.*?)```", section, _re.S).group(1)
        for command in ("validate_pr.py --changed", "validate_schemas.py --source-only",
                        "unittest discover"):
            self.assertIn(command, block)
        self.assertNotIn("pipeline.py", block)
        workflow = (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("validate_schemas.py --source-only", workflow)

    def test_it_installs_the_packages_the_site_render_needs(self):
        """Without mkdocs-material and pymdown-extensions the pipeline's
        render step (8b) is skipped for every contributor."""
        deploy = (REPO_ROOT / ".github" / "workflows" / "deploy-site.yml").read_text(
            encoding="utf-8"
        )
        specs = re.findall(r'"((?:mkdocs-material|pymdown-extensions)[^"]*)"', deploy)
        self.assertEqual(len(specs), 2)
        text = generate_readme.generate_contributing()
        for spec in specs:
            self.assertIn(f'"{spec}"', text)


if __name__ == "__main__":
    unittest.main()
