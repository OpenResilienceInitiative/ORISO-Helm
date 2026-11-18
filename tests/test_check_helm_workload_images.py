"""Test the Dev Helm image check through its command-line interface."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_helm_workload_images.py"
PIN = "test.registry/application@sha256:" + "a" * 64


def workload(kind, name):
    template = {"spec": {
        "containers": [{"name": "application", "image": PIN}],
        "initContainers": [{"name": "bootstrap", "image": "test.registry/bootstrap:2"}],
    }}
    spec = {"template": template}
    if kind == "CronJob":
        spec = {"jobTemplate": {"spec": spec}}
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "caritas",
            "annotations": {
                "meta.helm.sh/release-name": "caritas",
                "meta.helm.sh/release-namespace": "caritas",
            },
        },
        "spec": spec,
    }


class HelmWorkloadImagesTest(unittest.TestCase):
    def setUp(self):
        self.live = [workload("Deployment", "userservice"), workload("CronJob", "backup")]
        self.manifest = deepcopy(self.live)
        self.kubectl_failed = False

    def run_check(self, *flags, manifest=None):
        with tempfile.TemporaryDirectory() as temporary:
            kubectl = Path(temporary) / "kubectl"
            command = (
                "echo DO-NOT-PRINT >&2\nexit 7\n" if self.kubectl_failed
                else "cat <<'FIXTURE'\n" + json.dumps({"items": self.live}) + "\nFIXTURE\n"
            )
            kubectl.write_text(
                "#!/bin/sh\n"
                'test "$*" = "-n caritas get deployment,statefulset,daemonset,cronjob -o json --request-timeout=20s" || exit 8\n'
                + command,
                encoding="utf-8",
            )
            kubectl.chmod(0o700)
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--namespace", "caritas", "--release", "caritas",
                 *flags],
                input=yaml.safe_dump_all(self.manifest) if manifest is None else manifest,
                env={**os.environ, "PATH": temporary + os.pathsep + os.environ["PATH"]},
                text=True,
                capture_output=True,
                timeout=10,
            )

    def test_matches_direct_workloads_and_ignores_external_and_operator_children(self):
        child = workload("StatefulSet", "operator-child")
        child["metadata"]["ownerReferences"] = [{"kind": "ClickHouseInstallation", "controller": True}]
        external = workload("Deployment", "external")
        external["metadata"]["annotations"] = {}
        self.live.extend((child, external))
        del self.manifest[0]["metadata"]["namespace"]
        self.manifest.append(workload("DaemonSet", "new-agent"))

        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 existing image references", result.stdout)

    def test_rejects_saved_tags_and_missing_cronjob_init_images_without_printing_them(self):
        self.manifest[0]["spec"]["template"]["spec"]["containers"][0]["image"] = "DO-NOT-PRINT:old-tag"
        cron_pod = self.manifest[1]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        cron_pod.pop("initContainers")

        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Deployment/userservice/containers/application", result.stderr)
        self.assertIn("CronJob/backup/initContainers/bootstrap", result.stderr)
        self.assertNotIn("DO-NOT-PRINT", result.stderr + result.stdout)
        self.assertNotIn(PIN, result.stderr + result.stdout)

    def test_requires_workloads_in_the_same_namespace(self):
        self.manifest[0]["metadata"]["namespace"] = "different"
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Deployment/userservice is missing", result.stderr)

    def test_requires_an_explicit_opt_out_for_only_the_two_collectors(self):
        self.live.extend((
            workload("DaemonSet", "caritas-k8s-infra-otel-agent"),
            workload("Deployment", "caritas-k8s-infra-otel-deployment"),
        ))
        self.assertEqual(self.run_check().returncode, 1)
        allowed = self.run_check("--allow-remove-infra-collectors")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.manifest.pop(0)
        blocked = self.run_check("--allow-remove-infra-collectors")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("Deployment/userservice is missing", blocked.stderr)

    def test_rejects_ambiguous_and_unusable_inputs_without_echoing_payloads(self):
        for manifest in ("", "Secret: [DO-NOT-PRINT"):
            with self.subTest(manifest=manifest):
                result = self.run_check(manifest=manifest)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("DO-NOT-PRINT", result.stderr + result.stdout)
        self.manifest.append(self.manifest[0])
        self.assertEqual(self.run_check().returncode, 1)
        self.manifest.pop()
        self.live.clear()
        self.assertEqual(self.run_check().returncode, 1)

    def test_rejects_failed_kubectl_without_echoing_output(self):
        self.kubectl_failed = True
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read live workloads", result.stderr)
        self.assertNotIn("DO-NOT-PRINT", result.stderr + result.stdout)
