"""What a contributor gets back after adding a BIOS file.

A pull request from a fork runs with a read-only token, so the hash report
has to reach the contributor without one. And the advice for a file git
cannot hold has to be an action the contributor can take.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_pr  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare `on:` key to the boolean True.
    document["on"] = document.pop(True, document.get("on"))
    return document


def _steps(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["steps"]


class ForkPullRequestsGetTheReport(unittest.TestCase):
    def test_the_report_goes_to_the_job_summary_whatever_the_outcome(self):
        summary = [
            s for s in _steps("validate-bios")
            if "GITHUB_STEP_SUMMARY" in str(s.get("run", ""))
        ]
        self.assertTrue(summary, "the report never reaches the job summary")
        self.assertIn("report.md", str(summary[0]["run"]))
        self.assertIn("always()", str(summary[0].get("if", "")))

    def test_the_summary_is_written_before_the_failing_gate(self):
        steps = _steps("validate-bios")
        names = [str(s.get("run", "")) for s in steps]
        summary = next(i for i, r in enumerate(names) if "GITHUB_STEP_SUMMARY" in r)
        gate = next(i for i, s in enumerate(steps) if "rc != " in str(s.get("if", "")))
        self.assertLess(summary, gate)

    def test_the_comment_is_posted_only_where_the_token_can_write(self):
        for step in _steps("validate-bios"):
            if "gh pr comment" in str(step.get("run", "")):
                self.assertIn(SAME_REPO, str(step.get("if", "")))

    def test_labels_are_not_attempted_from_a_fork(self):
        for step in _steps("label-pr"):
            run = str(step.get("run", ""))
            if "gh pr edit" in run:
                self.assertIn(SAME_REPO, str(step.get("if", "")))
                self.assertNotIn("|| true", run, "a refused label vanishes in silence")

    def test_no_run_gets_a_write_token_for_fork_code(self):
        triggers = _workflow()["on"]
        self.assertNotIn("pull_request_target", triggers)
        self.assertNotIn("workflow_run", triggers)
        for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            # PyYAML reads the bare key `on` as True.
            events = data.get("on", data.get(True)) or {}
            self.assertNotIn("pull_request_target", events, path.name)

    def test_the_workflow_uses_no_third_party_action(self):
        for job in _workflow()["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    self.assertTrue(uses.startswith("actions/"), uses)


class OversizedFileAdvice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)
        self._max = validate_pr.MAX_FILE_SIZE
        self.addCleanup(setattr, validate_pr, "MAX_FILE_SIZE", self._max)

    def _message(self) -> str:
        path = self.tmp / "bios" / "Sony" / "PlayStation 3" / "PS3UPDAT.PUP"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"PUP" * 4)
        validate_pr.MAX_FILE_SIZE = 4
        result = validate_pr.validate_file(
            str(path.relative_to(self.tmp)), None,
            {"sha1": set(), "md5": set(), "names": set()},
        )
        self.assertFalse(result.passed)
        return next(m for s, m in result.checks if s == "FAIL" and "too large" in m)

    def test_the_advice_is_an_issue_for_the_large_files_release(self):
        message = self._message()
        self.assertNotIn("storage: external", message)
        self.assertIn("large-files", message)
        self.assertIn("open an issue", message)
        self.assertIn(validate_pr.LARGE_FILE_ISSUE_URL, message)

    def test_the_linked_issue_template_exists(self):
        template = validate_pr.LARGE_FILE_ISSUE_URL.rsplit("template=", 1)[1]
        self.assertTrue(
            (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / template).is_file(), template
        )


if __name__ == "__main__":
    unittest.main()
