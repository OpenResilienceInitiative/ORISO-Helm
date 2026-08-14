#!/usr/bin/env python3
"""Contract tests for exact before/target MatrixRTC rollback artifacts."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "capture-cutover-rollback.py"
)
DIGEST = "1" * 64


def load_capture_module():
    spec = importlib.util.spec_from_file_location("capture_cutover_rollback", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image(repository: str) -> str:
    return f"{repository}@sha256:{DIGEST}"


class CaptureCutoverRollbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capture = load_capture_module()

    def test_collects_ready_before_state_and_writes_bounded_rollback(self) -> None:
        deployments = []
        for name, repository in self.capture.DEPLOYMENT_REPOSITORIES.items():
            pod_spec = {"containers": [{"image": image(repository)}]}
            if name == "matrix-synapse":
                pod_spec["initContainers"] = [{"image": image("busybox") }]
            deployments.append(
                {
                    "metadata": {"name": name},
                    "spec": {"replicas": 1, "template": {"spec": pod_spec}},
                    "status": {"readyReplicas": 1, "updatedReplicas": 1},
                }
            )

        def run(command: list[str]) -> str:
            if command[:2] == ["helm", "status"]:
                return json.dumps({"name": "oriso-platform", "version": 7})
            return json.dumps({"items": deployments})

        before = self.capture.collect_before_state("oriso-platform", "caritas", run)
        self.assertEqual(before["helmRevision"], 7)
        self.assertEqual(len(before["images"]), 9)

        target = {key: image(repository) for key, repository in self.capture.IMAGE_REPOSITORIES.items()}
        with tempfile.TemporaryDirectory() as parent:
            output = pathlib.Path(parent) / "evidence"
            self.capture.write_artifacts(output, before, target, "15m")
            self.assertEqual(
                (output / "rollback-command.txt").read_text(encoding="utf-8").strip(),
                "helm rollback oriso-platform 7 --namespace caritas --wait --timeout 15m",
            )
            self.assertEqual(
                json.loads((output / "target-images.json").read_text(encoding="utf-8"))["images"],
                target,
            )

    def test_rejects_mutable_or_unready_before_state(self) -> None:
        deployment = {
            "metadata": {"name": "frontend"},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"image": "frontend:dev"}]}},
            },
            "status": {"readyReplicas": 0, "updatedReplicas": 1},
        }
        with self.assertRaisesRegex(ValueError, "frontend"):
            self.capture.extract_deployed_images([deployment])


if __name__ == "__main__":
    unittest.main()
