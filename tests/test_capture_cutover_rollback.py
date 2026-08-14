#!/usr/bin/env python3
"""Contract tests for exact before/target MatrixRTC rollback artifacts."""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest

import yaml

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "capture-cutover-rollback.py"
)
DIGEST = "1" * 64


def load_capture_module():
    spec = importlib.util.spec_from_file_location(
        "capture_cutover_rollback", SCRIPT_PATH
    )
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
                pod_spec["initContainers"] = [{"image": image("busybox")}]
            deployments.append(
                {
                    "metadata": {"name": name},
                    "spec": {"replicas": 1, "template": {"spec": pod_spec}},
                    "status": {"readyReplicas": 1, "updatedReplicas": 1},
                }
            )

        def run(command: list[str]) -> str:
            if command[:2] == ["helm", "status"]:
                return json.dumps({"name": "caritas", "version": 7})
            if command[:3] == ["helm", "get", "manifest"]:
                manifests = [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": deployment["metadata"],
                        "spec": deployment["spec"],
                    }
                    for deployment in deployments
                ]
                return yaml.safe_dump_all(manifests)
            return json.dumps({"items": deployments})

        before = self.capture.collect_before_state("caritas", "caritas", run)
        self.assertEqual(before["helmRevision"], 7)
        self.assertEqual(len(before["images"]), 9)
        self.assertTrue(before["helmManifestMatchesLive"])

        target = {
            key: image(repository)
            for key, repository in self.capture.IMAGE_REPOSITORIES.items()
        }
        with tempfile.TemporaryDirectory() as parent:
            output = pathlib.Path(parent) / "evidence"
            self.capture.write_artifacts(output, before, target, "15m")
            self.assertEqual(
                (output / "rollback-command.txt").read_text(encoding="utf-8").strip(),
                "helm rollback caritas 7 --namespace caritas --wait --timeout 15m",
            )
            self.assertEqual(
                json.loads((output / "target-images.json").read_text(encoding="utf-8"))[
                    "images"
                ],
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

    def test_rejects_a_helm_revision_that_does_not_describe_live_images(self) -> None:
        deployments = []
        for name, repository in self.capture.DEPLOYMENT_REPOSITORIES.items():
            pod_spec = {"containers": [{"image": image(repository)}]}
            if name == "matrix-synapse":
                pod_spec["initContainers"] = [{"image": image("busybox")}]
            deployments.append(
                {
                    "metadata": {"name": name},
                    "spec": {"replicas": 1, "template": {"spec": pod_spec}},
                    "status": {"readyReplicas": 1, "updatedReplicas": 1},
                }
            )

        manifests = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": copy.deepcopy(deployment["metadata"]),
                "spec": copy.deepcopy(deployment["spec"]),
            }
            for deployment in deployments
        ]
        manifests[0]["spec"]["template"]["spec"]["containers"][0]["image"] = image(
            "ghcr.io/openresilienceinitiative/oriso-frontend"
        ).replace(DIGEST, "2" * 64)

        def run(command: list[str]) -> str:
            if command[:2] == ["helm", "status"]:
                return json.dumps({"name": "caritas", "version": 1})
            if command[:3] == ["helm", "get", "manifest"]:
                return yaml.safe_dump_all(manifests)
            return json.dumps({"items": deployments})

        with self.assertRaisesRegex(ValueError, "baseline normalization"):
            self.capture.collect_before_state("caritas", "caritas", run)


if __name__ == "__main__":
    unittest.main()
