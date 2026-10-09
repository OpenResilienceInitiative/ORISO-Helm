#!/usr/bin/env python3
"""The render-contract step must run every contract, not stop at the first.

With `set -e` a bare loop aborts on the first failing contract, which hides
every contract after it. This runs the step's real shell, lifted out of the
workflow file, against three stand-in contracts, so a regression in the script
itself is caught rather than a copy of it.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate-helm-chart.yml"


def render_step_script() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    for job in jobs.values():
        for step in job["steps"]:
            if step.get("name") == "Run render contracts":
                return step["run"]
    raise AssertionError("workflow has no 'Run render contracts' step")


class RenderStepReportsEverything(unittest.TestCase):
    def run_step(self, outcomes: dict[str, bool]):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            (work / "tests").mkdir()
            for name, passes in outcomes.items():
                (work / "tests" / f"render_{name}_test.py").write_text(
                    f'print("RAN {name}")\nraise SystemExit({0 if passes else 1})\n'
                )
            # The runner provides `python`; make the same name resolve here.
            bin_dir = work / "bin"
            bin_dir.mkdir()
            (bin_dir / "python").symlink_to(sys.executable)
            summary = work / "summary.md"
            result = subprocess.run(
                ["bash", "-c", render_step_script()],
                cwd=work,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            )
            return result, (summary.read_text() if summary.exists() else "")

    def test_a_failure_in_the_middle_does_not_hide_the_rest(self):
        result, summary = self.run_step({"a": True, "b": False, "c": True})

        for name in ("a", "b", "c"):
            self.assertIn(f"RAN {name}", result.stdout, f"contract {name} never ran")
        self.assertNotEqual(result.returncode, 0, "a failing contract must fail the step")
        self.assertIn("1 of 3 failed", summary)
        self.assertIn("render_b_test.py", summary)

    def test_every_failure_is_listed_not_just_the_first(self):
        result, summary = self.run_step({"a": False, "b": True, "c": False})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2 of 3 failed", summary)
        self.assertIn("render_a_test.py", summary)
        self.assertIn("render_c_test.py", summary)

    def test_all_green_passes(self):
        result, summary = self.run_step({"a": True, "b": True})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("All 2 contracts passed", summary)


if __name__ == "__main__":
    unittest.main()
