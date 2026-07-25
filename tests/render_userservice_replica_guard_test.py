#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def render(*extra_values: Path) -> subprocess.CompletedProcess[str]:
    command = [
        "helm",
        "template",
        "oriso-platform",
        str(ROOT),
        "--namespace",
        "caritas",
        "-f",
        str(ROOT / "values.yaml.default"),
        "-f",
        str(ROOT / "secrets.yaml.default"),
    ]
    for path in extra_values:
        command.extend(["-f", str(path)])
    return subprocess.run(command, text=True, capture_output=True)


class UserServiceReplicaGuardTest(unittest.TestCase):
    def test_default_and_predev_fixture_render_exactly_one_replica(self) -> None:
        for values in (
            (),
            (ROOT / "tests/fixtures/values-pre-dev-livechat-gate.yaml",),
        ):
            result = render(*values)
            self.assertEqual(result.returncode, 0, result.stderr)
            documents = [
                item
                for item in yaml.safe_load_all(result.stdout)
                if isinstance(item, dict)
            ]
            deployment = next(
                item
                for item in documents
                if item.get("kind") == "Deployment"
                and item.get("metadata", {}).get("name")
                == "userservice"
            )
            self.assertEqual(deployment["spec"]["replicas"], 1)

    def test_zero_and_two_replicas_fail_with_dependency_handoff(self) -> None:
        for replicas in (0, 2):
            with self.subTest(replicas=replicas), tempfile.TemporaryDirectory() as directory:
                override = Path(directory) / "replicas.yaml"
                override.write_text(f"userService:\n  replicas: {replicas}\n")
                result = render(override)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("UserService supports exactly one replica", result.stderr)
                self.assertIn("UserService#543", result.stderr)
                self.assertIn("UserService#379", result.stderr)
                self.assertIn("UserService#216", result.stderr)


if __name__ == "__main__":
    unittest.main()
