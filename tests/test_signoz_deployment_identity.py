from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "signoz_deployment_identity", ROOT / "files/signoz/signoz_deployment_identity.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

PIN = "sha256:" + "1" * 64
PLATFORM = "sha256:" + "2" * 64
OTHER = "sha256:" + "3" * 64
REPO = "ghcr.io/openresilienceinitiative/oriso-userservice"
IMAGE = f"{REPO}@{PIN}"
NAME = "userservice"
REVISION = "deployment.kubernetes.io/revision"


def owner(kind: str, uid: str) -> dict:
    return {"kind": kind, "uid": uid, "controller": True}


def expected_images() -> dict:
    return {
        workload: {
            "image": f"ghcr.io/openresilienceinitiative/oriso-{workload}@{PIN}",
            "runtimeDigests": [PIN, PLATFORM],
        }
        for workload in ("agencyservice", "consultingtypeservice", "tenantservice", "userservice")
    }


class DeploymentIdentityTest(unittest.TestCase):
    def setUp(self):
        self.expected = MODULE.parse_expectations(expected_images())[NAME]
        self.deployment = {
            "metadata": {
                "uid": "deployment-uid", "generation": 4, "annotations": {REVISION: "7"},
            },
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": NAME, "image": IMAGE},
            ]}}},
            "status": {"observedGeneration": 4, "replicas": 1, "updatedReplicas": 1,
                       "availableReplicas": 1},
        }
        self.replica_set = {
            "metadata": {
                "uid": "replica-set-uid", "ownerReferences": [owner("Deployment", "deployment-uid")],
                "annotations": {REVISION: "7"},
            },
        }
        self.pod = {
            "metadata": {"ownerReferences": [owner("ReplicaSet", "replica-set-uid")]},
            "status": {"phase": "Running", "containerStatuses": [
                {"name": NAME, "imageID": f"docker-pullable://{REPO}@{PIN}", "ready": True,
                 "state": {"running": {}}},
            ]},
        }

    def status(self):
        return MODULE.inspect_workload(
            NAME, self.expected, self.deployment, [self.replica_set], [self.pod]
        )

    def test_exact_image_and_approved_platform_manifest_match(self):
        self.assertEqual(self.status(), "match")
        self.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = f"{REPO}:dev@{PIN}"
        self.pod["status"]["containerStatuses"][0]["imageID"] = f"{REPO}@{PLATFORM}"
        self.assertEqual(self.status(), "match")

    def test_spec_drift_counts_after_rollout_even_if_runtime_matches(self):
        self.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = f"{REPO}@{OTHER}"
        self.assertEqual(self.status(), "mismatch")

    def test_comparable_runtime_drift_counts_after_rollout(self):
        self.pod["status"]["containerStatuses"][0]["imageID"] = f"{REPO}@{OTHER}"
        self.assertEqual(self.status(), "mismatch")

    def test_config_id_and_waiting_main_container_do_not_count(self):
        container = self.pod["status"]["containerStatuses"][0]
        container["imageID"] = f"containerd://{OTHER}"
        self.assertEqual(self.status(), "unverified")
        container["imageID"] = IMAGE
        container["state"] = {"waiting": {}}
        self.assertEqual(self.status(), "unverified")

    def test_old_generation_and_incomplete_rollout_do_not_count(self):
        self.deployment["status"]["observedGeneration"] = 3
        self.assertEqual(self.status(), "unverified")
        self.deployment["status"]["observedGeneration"] = 4
        self.deployment["status"]["updatedReplicas"] = 0
        self.assertEqual(self.status(), "unverified")

    def test_wrong_owner_old_revision_and_terminating_pod_do_not_count(self):
        self.replica_set["metadata"]["ownerReferences"] = [owner("Deployment", "another")]
        self.assertEqual(self.status(), "unverified")
        self.replica_set["metadata"]["ownerReferences"] = [owner("Deployment", "deployment-uid")]
        self.replica_set["metadata"]["annotations"][REVISION] = "6"
        self.assertEqual(self.status(), "unverified")
        self.replica_set["metadata"]["annotations"][REVISION] = "7"
        self.pod["metadata"]["deletionTimestamp"] = "2026-11-08T06:00:00Z"
        self.assertEqual(self.status(), "unverified")

    def test_all_active_replicas_are_required(self):
        self.deployment["spec"]["replicas"] = 2
        self.deployment["status"].update(replicas=2, updatedReplicas=2, availableReplicas=2)
        self.assertEqual(self.status(), "unverified")

    def test_expected_inputs_require_all_workloads_and_immutable_digests(self):
        incomplete = expected_images()
        incomplete.pop(NAME)
        mutable = expected_images()
        mutable[NAME]["image"] = f"{REPO}:dev"
        missing_runtime = expected_images()
        missing_runtime[NAME]["runtimeDigests"] = []
        for invalid in (incomplete, mutable, missing_runtime):
            with self.subTest(invalid=invalid.keys()), self.assertRaises(ValueError):
                MODULE.parse_expectations(invalid)

    def test_metrics_emit_current_state_without_raw_identity(self):
        metrics = MODULE.build_metrics(
            {"agencyservice": "match", "userservice": "mismatch", "tenantservice": "unverified"},
            "dev", "oriso-dev", 123,
        )["resourceMetrics"]
        self.assertEqual(len(metrics), 3)
        observed = {}
        for series in metrics:
            attrs = {a["key"]: a["value"]["stringValue"] for a in series["resource"]["attributes"]}
            self.assertEqual(
                set(attrs), {"deployment.environment", "k8s.cluster.name", "service.name"}
            )
            self.assertEqual(attrs["deployment.environment"], "dev")
            self.assertEqual(attrs["k8s.cluster.name"], "oriso-dev")
            observed[attrs["service.name"]] = {
                metric["name"].rsplit(".", 1)[-1]: metric["gauge"]["dataPoints"][0]["asInt"]
                for metric in series["scopeMetrics"][0]["metrics"]
            }
        self.assertEqual(observed, {
            "agency-service": {"mismatch": "0", "unverified": "0"},
            "user-service": {"mismatch": "1", "unverified": "0"},
            "tenant-service": {"unverified": "1"},
        })

    def test_otlp_requires_success_and_rejects_partial_acknowledgement(self):
        connection = Mock()
        payload = MODULE.build_metrics({NAME: "match"}, "dev", "oriso-dev", 123)
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"partialSuccess": {}}'
        MODULE.publish_metrics(connection, payload)
        method, path = connection.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/metrics"))
        self.assertEqual(json.loads(connection.request.call_args.kwargs["body"]), payload)

        response.read.return_value = b'{"partialSuccess": {"rejectedDataPoints": "1"}}'
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            MODULE.publish_metrics(connection, payload)
        response.status = 302
        with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
            MODULE.publish_metrics(connection, payload)
