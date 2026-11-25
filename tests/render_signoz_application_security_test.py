"""Verify the SigNoz application and backup agree on SQLite volume access."""

import unittest

from render_signoz_dev_observability_test import by_key, render


def application(docs):
    return docs[("StatefulSet", "caritas-signoz")]["spec"]


def backup(docs):
    return docs[("CronJob", "caritas-signoz-data-protection-backup")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]


class ApplicationSecurityTest(unittest.TestCase):
    def test_dev_app_runs_without_root_on_existing_pvc(self):
        docs = by_key(render("values-dev.yaml"))
        workload = application(docs)
        pod = workload["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"], {
            "runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
            "fsGroup": 65532, "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        })
        main = pod["containers"][0]
        self.assertEqual(main["securityContext"], {
            "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        })
        self.assertEqual(workload["volumeClaimTemplates"][0]["metadata"]["name"], "signoz-db")
        self.assertIn({"name": "signoz-db", "mountPath": "/var/lib/signoz/"}, main["volumeMounts"])

    def test_backup_inherits_source_group_without_reassigning_volume_ownership(self):
        docs = by_key(render("values-dev.yaml", helm_args=(
            "--set", "signoz.signoz.podSecurityContext.fsGroup=2401",
        )))
        pod = backup(docs)
        self.assertEqual(pod["securityContext"]["supplementalGroups"], [2401])
        self.assertNotIn("fsGroup", pod["securityContext"])
        claims = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]
                  if "persistentVolumeClaim" in v}
        self.assertEqual(claims, {
            "signoz-db": "signoz-db-caritas-signoz-0", "backup": "caritas-signoz-data-protection-backup",
        })

    def test_non_dev_profile_keeps_the_images_identity_even_with_an_inherited_dev_value(self):
        docs = by_key(render(helm_args=(
            "--set", "signoz.enabled=true",
            "--set", "signoz.signoz.devSQLiteFilesystemGroup=65532",
            "--set-string", "signoz.signoz.env.signoz_global_external__url=https://render.oriso.internal/signoz",
        )))
        pod = application(docs)["template"]["spec"]
        self.assertEqual(pod["securityContext"], {"seccompProfile": {"type": "RuntimeDefault"}})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])


if __name__ == "__main__":
    unittest.main()
