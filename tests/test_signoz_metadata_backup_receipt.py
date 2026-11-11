"""Check the handoff from the newest scheduled backup to isolated recovery."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/signoz_metadata_backup_receipt.py"
SPEC = importlib.util.spec_from_file_location("signoz_metadata_backup_receipt", SCRIPT)
assert SPEC and SPEC.loader
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)

NAMESPACE = "caritas"
CLAIM = "signoz-backup-recovery"
CRON = "caritas-signoz-data-protection-backup"
JOB = f"{CRON}-00000001"
JOB_UID = "75a198df-bb28-4e9e-9b0b-9e038b5d713f"
POD_UID = "cf829cd6-8339-4b58-a912-81a3ef109a57"
SINCE = "2026-11-08T00:00:00Z"
SCHEDULED = "2026-11-09T02:30:00Z"


def owner(kind, name, uid):
    return {"apiVersion": "batch/v1", "kind": kind, "name": name, "uid": uid, "controller": True}


class BackupReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        pod_spec = {
            "volumes": [{"name": "backup", "persistentVolumeClaim": {"claimName": CLAIM}}],
            "containers": [{"name": "backup", "volumeMounts": [{"name": "backup", "mountPath": "/backup"}]}],
        }
        job_spec = {"template": {"spec": pod_spec}}
        self.cron = {
            "metadata": {"name": CRON, "uid": "cron-uid"},
            "spec": {"jobTemplate": {"spec": deepcopy(job_spec)}},
        }
        self.pvc = {
            "metadata": {"name": CLAIM, "namespace": NAMESPACE, "uid": "pvc-uid", "creationTimestamp": SINCE},
            "spec": {"volumeName": "backup-pv", "accessModes": ["ReadWriteOnce"]},
            "status": {"phase": "Bound"},
        }
        self.pv = {
            "metadata": {"name": "backup-pv"},
            "spec": {"persistentVolumeReclaimPolicy": "Retain",
                     "claimRef": {"namespace": NAMESPACE, "name": CLAIM, "uid": "pvc-uid"}},
            "status": {"phase": "Bound"},
        }
        self.job = {
            "metadata": {
                "name": JOB, "uid": JOB_UID, "creationTimestamp": SCHEDULED,
                "annotations": {"batch.kubernetes.io/cronjob-scheduled-timestamp": SCHEDULED},
                "ownerReferences": [owner("CronJob", CRON, "cron-uid")],
            },
            "spec": deepcopy(job_spec),
            "status": {"conditions": [{"type": "Complete", "status": "True"}],
                       "completionTime": "2026-11-09T02:30:42Z"},
        }
        digest = "e" * 64
        self.receipt = {
            "version": 1, "archive": f"signoz-20261109T023034Z-{JOB_UID}-{POD_UID}-{digest}.db.gz",
            "jobUid": JOB_UID, "podUid": POD_UID, "sha256": digest, "bytes": 4422,
        }
        self.terminated = {"exitCode": 0, "message": json.dumps(self.receipt)}
        self.pod = {
            "metadata": {
                "name": f"{JOB}-worker", "uid": POD_UID,
                "ownerReferences": [owner("Job", JOB, JOB_UID)],
                "labels": {"batch.kubernetes.io/controller-uid": JOB_UID},
            },
            "spec": deepcopy(pod_spec),
            "status": {"phase": "Succeeded", "containerStatuses": [
                {"name": "backup", "state": {"terminated": self.terminated}},
            ]},
        }

    def select(self, *jobs):
        return RECEIPT.select_job(self.cron, self.pvc, list(jobs), CLAIM, SINCE)

    def test_selects_latest_schedule_and_reads_exact_successful_pod_receipt(self) -> None:
        failed_attempt = deepcopy(self.pod)
        failed_attempt["status"]["phase"] = "Failed"
        selected = self.select(self.job)
        result = RECEIPT.backup_receipt(selected, [failed_attempt, self.pod], CLAIM)
        self.assertEqual(result, {
            **self.receipt, "claim": CLAIM, "job": JOB,
            "pod": self.pod["metadata"]["name"], "completed": self.job["status"]["completionTime"],
        })

    def test_a_failed_new_schedule_takes_precedence_over_an_older_success(self) -> None:
        older = deepcopy(self.job)
        older_time = "2026-11-08T02:30:00Z"
        older["metadata"]["creationTimestamp"] = older_time
        older["metadata"]["annotations"]["batch.kubernetes.io/cronjob-scheduled-timestamp"] = older_time
        self.job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        manual = deepcopy(older)
        manual["metadata"]["creationTimestamp"] = "2026-11-09T03:30:00Z"
        manual["metadata"]["annotations"] = {"cronjob.kubernetes.io/instantiate": "manual"}
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "newest scheduled Job has not completed"):
            self.select(older, self.job, manual)

    def test_ignores_a_job_older_than_the_bound_claim(self) -> None:
        self.pvc["metadata"]["creationTimestamp"] = "2026-11-09T02:31:00Z"
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "No scheduled"):
            self.select(self.job)

    def test_refuses_a_different_job_claim(self) -> None:
        self.job["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "old-claim"
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "newest scheduled Job uses a different"):
            self.select(self.job)

    def test_requires_real_pod_ownership_and_a_single_successful_pod(self) -> None:
        wrong_owner = deepcopy(self.pod)
        wrong_owner["metadata"]["ownerReferences"][0]["uid"] = "another-job"
        for pods in ([wrong_owner], [self.pod, deepcopy(self.pod)]):
            with self.subTest(pods=len(pods)), self.assertRaisesRegex(RECEIPT.ReceiptError, "one successful"):
                RECEIPT.backup_receipt(self.job, pods, CLAIM)

    def test_refuses_a_failed_backup_container_or_an_unbound_receipt(self) -> None:
        cases = (
            {**self.terminated, "exitCode": 1},
            {"exitCode": 0, "message": "backups:\n signoz-20261109T023000Z.db.gz"},
            {"exitCode": 0, "message": json.dumps({**self.receipt, "sha256": "a" * 64})},
            {"exitCode": 0, "message": json.dumps({**self.receipt, "archive": "../" + self.receipt["archive"]})},
        )
        for termination in cases:
            with self.subTest(termination=termination), self.assertRaises(RECEIPT.ReceiptError):
                self.pod["status"]["containerStatuses"][0]["state"]["terminated"] = termination
                RECEIPT.backup_receipt(self.job, [self.pod], CLAIM)

    def test_requires_the_same_bound_retain_volume_for_a_destination_switch(self) -> None:
        RECEIPT.require_retained_volume(self.pvc, self.pv)
        self.pv["spec"]["claimRef"]["uid"] = "different-pvc"
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "Bound Retain PV"):
            RECEIPT.require_retained_volume(self.pvc, self.pv)


if __name__ == "__main__":
    unittest.main()
