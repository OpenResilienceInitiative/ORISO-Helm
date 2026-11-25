"""Test the SigNoz migration Job's security and replacement contract."""

import base64
import unittest

from render_signoz_dev_observability_test import by_key, render


def migrator(docs):
    jobs = [doc for (kind, _), doc in docs.items() if kind == "Job"
            and doc["metadata"].get("labels", {}).get("app.kubernetes.io/component")
            == "signoz-telemetrystore-migrator"]
    assert len(jobs) == 1, len(jobs)
    return jobs[0]


def dev(*args):
    return by_key(render("values-dev.yaml", helm_args=args))


def argo(*args):
    return by_key(render(
        "values-prod.yaml",
        helm_args=("--set", "signoz.enabled=true", *args),
    ))


class MigratorSecurityTest(unittest.TestCase):
    def test_all_four_phases_run_with_restricted_settings(self):
        job = migrator(dev())
        spec = job["spec"]["template"]["spec"]
        self.assertEqual(job["metadata"]["labels"]["oriso.org/signoz-migrator"], "managed")
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual(spec["securityContext"], {
            "runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        })
        phases = spec["initContainers"] + spec["containers"]
        self.assertEqual([c["args"] for c in phases], [
            ["migrate", "ready"], ["migrate", "bootstrap"],
            ["migrate", "sync", "up"], ["migrate", "async", "up"],
        ])
        for phase in phases:
            self.assertEqual(phase["securityContext"], {
                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
            })

    def test_normal_upgrade_replaces_only_changed_pod_templates(self):
        original = migrator(dev())
        name = original["metadata"]["name"]
        self.assertRegex(name, r"^signoz-telemetrystore-migrator-[0-9a-f]{16}$")
        self.assertEqual(original, migrator(dev("--is-upgrade")))
        image_change = migrator(dev("--is-upgrade", "--set-string", "signoz.otelCollector.image.tag=fixture"))
        self.assertNotEqual(name, image_change["metadata"]["name"])
        self.assertEqual(image_change["spec"]["template"]["metadata"], original["spec"]["template"]["metadata"])

    def test_argocd_uses_one_sync_hook_identity_across_revisions(self):
        default_prod = by_key(render("values-prod.yaml"))
        self.assertFalse(any(
            kind == "Job" and "telemetrystore-migrator" in name for kind, name in default_prod
        ))
        first = argo()
        previous = migrator(first)
        changed = migrator(argo("--set-string", "signoz.otelCollector.image.tag=fixture-new"))
        self.assertEqual(previous["metadata"]["name"], changed["metadata"]["name"])
        self.assertNotEqual(previous["spec"]["template"], changed["spec"]["template"])
        self.assertEqual(previous["metadata"]["labels"]["oriso.org/signoz-migrator"], "argocd")
        self.assertEqual(previous["metadata"]["annotations"], {
            "argocd.argoproj.io/hook": "Sync",
            "argocd.argoproj.io/hook-delete-policy": "HookSucceeded",
        })
        pod = previous["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        password = next(env for env in pod["initContainers"][0]["env"] if env["name"] == "CLICKHOUSE_PASSWORD")
        ref = password["valueFrom"]["secretKeyRef"]
        password_secret = first["Secret", ref["name"]]
        self.assertNotIn("argocd.argoproj.io/hook", password_secret["metadata"].get("annotations", {}))
        self.assertIn(ref["key"], password_secret["data"])

    def test_argocd_protects_hook_lifecycle_annotations(self):
        with self.assertRaisesRegex(AssertionError, "Argo CD migrator manages annotation"):
            argo("--set-string", r"signoz.telemetryStoreMigrator.annotations.argocd\.argoproj\.io/sync-options=Force=true")

    def test_inline_password_rotation_updates_a_secret_without_renaming_the_job(self):
        first = dev("--set-string", "signoz.clickhouse.password=fixture-first")
        rotated = dev("--set-string", "signoz.clickhouse.password=fixture-second")
        job = migrator(first)
        self.assertEqual(job, migrator(rotated))
        self.assertNotIn("fixture-first", repr(job))
        spec = job["spec"]["template"]["spec"]
        for phase in spec["initContainers"] + spec["containers"]:
            password = next(e for e in phase["env"] if e["name"] == "CLICKHOUSE_PASSWORD")
            ref = password["valueFrom"]["secretKeyRef"]
            self.assertEqual(base64.b64decode(first["Secret", ref["name"]]["data"][ref["key"]]), b"fixture-first")
            self.assertEqual(base64.b64decode(rotated["Secret", ref["name"]]["data"][ref["key"]]), b"fixture-second")

    def test_external_secret_and_hook_lifecycle(self):
        external = (
            "--set", "signoz.clickhouse.enabled=false",
            "--set-string", "signoz.externalClickhouse.host=fixture-clickhouse.invalid",
            "--set-string", "signoz.externalClickhouse.cluster=fixture",
        )
        docs = dev(*external,
            "--set-string", "signoz.externalClickhouse.existingSecret=external-clickhouse-auth",
            "--set-string", "signoz.externalClickhouse.existingSecretPasswordKey=password",
        )
        job = migrator(docs)
        password = next(e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
                        if e["name"] == "CLICKHOUSE_PASSWORD")
        self.assertEqual(password["valueFrom"]["secretKeyRef"]["name"], "external-clickhouse-auth")
        self.assertFalse(any("migrator-auth" in name for kind, name in docs if kind == "Secret"))
        with_inline_password = dev(*external, "--set-string", "signoz.externalClickhouse.password=external-fixture")
        phase = migrator(with_inline_password)["spec"]["template"]["spec"]["containers"][0]
        password = next(e for e in phase["env"] if e["name"] == "CLICKHOUSE_PASSWORD")
        ref = password["valueFrom"]["secretKeyRef"]
        self.assertEqual(ref["key"], "clickhouse-password")
        self.assertEqual(base64.b64decode(with_inline_password["Secret", ref["name"]]["data"][ref["key"]]), b"external-fixture")
        self.assertFalse(any("migrator-auth" in name for kind, name in with_inline_password if kind == "Secret"))
        hook = migrator(dev("--is-upgrade", "--set", "signoz.telemetryStoreMigrator.upgradeHelmHooks=true"))
        self.assertEqual(hook["metadata"]["name"], "signoz-telemetrystore-migrator-pre-upgrade")
        self.assertEqual(hook["metadata"]["labels"]["oriso.org/signoz-migrator"], "hook")
        self.assertEqual(hook["metadata"]["annotations"]["helm.sh/hook"], "pre-upgrade")
        self.assertEqual(hook["metadata"]["annotations"]["helm.sh/hook-delete-policy"], "before-hook-creation")


if __name__ == "__main__":
    unittest.main()
