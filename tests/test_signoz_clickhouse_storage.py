"""Cover the ClickHouse storage guard at its live-to-manifest comparison."""

from copy import deepcopy
from unittest import TestCase

from scripts import check_signoz_clickhouse_storage as storage


def installation(size="20Gi"):
    return {
        "metadata": {"name": "caritas-clickhouse", "namespace": "caritas", "uid": "chi-uid"},
        "spec": {
            "defaults": {"templates": {"dataVolumeClaimTemplate": "data"}},
            "configuration": {"clusters": [{"name": "cluster", "layout": {"replicasCount": 1}}]},
            "templates": {"volumeClaimTemplates": [{
                "name": "data", "reclaimPolicy": "Retain",
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": size}},
                },
            }]},
        },
    }


def claim(name, request, capacity):
    return {
        "metadata": {"name": name},
        "spec": {"volumeName": "pv-" + name, "resources": {"requests": {"storage": request}}},
        "status": {"phase": "Bound", "capacity": {"storage": capacity}},
    }


def statefulset(size, retention=None):
    specification = {"volumeClaimTemplates": [{
        "metadata": {"name": "data"},
        "spec": {"resources": {"requests": {"storage": size}}},
    }]}
    if retention is not None:
        specification["persistentVolumeClaimRetentionPolicy"] = retention
    return {"metadata": {"name": "chi-0"}, "spec": specification}


class ClickHouseStorageGuardTest(TestCase):
    def test_candidate_must_preserve_request_and_capacity_across_all_claims(self):
        claims = [
            claim("primary", "30Gi", "20Gi"),  # Expansion requested, CSI has not finished.
            claim("secondary", "20Gi", "33285996544"),  # CSI capacity is already larger.
        ]
        live = installation()
        self.assertIn("primary", " ".join(storage.storage_failures(live, installation("20Gi"), claims)))
        failures = storage.storage_failures(live, installation("30Gi"), claims)
        self.assertEqual(len(failures), 1)
        self.assertIn("secondary", failures[0])
        self.assertEqual(storage.storage_failures(live, installation("31Gi"), claims), [])

    def test_stale_helm_rollback_cannot_lower_installation_request(self):
        live = installation("30Gi")
        failures = storage.storage_failures(live, installation("20Gi"), [claim("data", "20Gi", "20Gi")])
        self.assertIn("installation", " ".join(failures))

    def test_recreation_requires_retention_when_live_statefulset_template_grows(self):
        current = installation("30Gi")
        claims = [claim("data", "30Gi", "30Gi")]
        for trigger in ("whenScaled", "whenDeleted"):
            with self.subTest(trigger=trigger):
                failures = storage.storage_failures(current, current, claims, [
                    statefulset("20Gi", {trigger: "Delete"}),
                ])
                self.assertEqual(len(failures), 1)
                self.assertIn("chi-0", failures[0])
                self.assertIn(trigger, failures[0])

    def test_statefulset_resize_accepts_default_retention_and_equivalent_sizes(self):
        claims = [claim("data", "20Gi", "20Gi")]
        self.assertEqual(storage.storage_failures(
            installation(), installation("30Gi"), claims, [statefulset("20Gi")],
        ), [])
        self.assertEqual(storage.storage_failures(
            installation(), installation(), claims, [statefulset("21474836480", {"whenScaled": "Delete"})],
        ), [])

    def test_statefulset_template_remains_a_floor_during_partial_reconciliation(self):
        current = installation("20Gi")
        failures = storage.storage_failures(
            current, current, [claim("data", "20Gi", "20Gi")], [statefulset("30Gi")],
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("StatefulSet chi-0", failures[0])

    def test_removal_rebinding_or_topology_change_is_rejected(self):
        live = installation()
        candidate = deepcopy(live)
        candidate["spec"]["configuration"]["clusters"][0]["layout"]["replicasCount"] = 0
        with self.assertRaisesRegex(ValueError, "topology"):
            storage.storage_failures(live, candidate, [claim("data", "20Gi", "20Gi")])
        candidate = deepcopy(live)
        candidate["spec"]["templates"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] = "other"
        with self.assertRaisesRegex(ValueError, "data claim definition"):
            storage.storage_failures(live, candidate, [claim("data", "20Gi", "20Gi")])
        candidate = deepcopy(live)
        candidate["spec"]["templates"]["volumeClaimTemplates"].clear()
        with self.assertRaisesRegex(ValueError, "data claim template"):
            storage.storage_failures(live, candidate, [claim("data", "20Gi", "20Gi")])

    def test_unready_owned_pod_is_included_and_foreign_pod_is_ignored(self):
        owned = [{"metadata": {"uid": "set-uid", "name": "chi-0"}, "spec": {"replicas": 1}}]
        pod = {
            "metadata": {
                "name": "chi-0-0",
                "ownerReferences": [{"kind": "StatefulSet", "name": "chi-0", "uid": "set-uid", "controller": True}],
            },
            "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "actual-pvc"}}]},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        }
        foreign = deepcopy(pod)
        foreign["metadata"]["ownerReferences"][0]["uid"] = "wrong"
        self.assertEqual(storage.data_claim_names("data", owned, [foreign, pod]), ["actual-pvc"])
        with self.assertRaisesRegex(ValueError, "data Pods"):
            storage.data_claim_names("data", owned, [foreign])

    def test_kubernetes_quantity_units_compare_numerically(self):
        self.assertEqual(storage.byte_count("30Gi"), 32212254720)
        self.assertLess(storage.byte_count("30G"), storage.byte_count("30Gi"))
        self.assertEqual(storage.byte_count("32212254720"), storage.byte_count("30Gi"))
        self.assertEqual(storage.byte_count("2e3"), 2000)
