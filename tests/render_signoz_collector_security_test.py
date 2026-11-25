"""Verify the SigNoz receiver can use a restricted Pod security context."""

import unittest

from render_signoz_dev_observability_test import by_key, render
from render_signoz_private_otlp_test import render_private


def collector(docs):
    deployment = docs[("Deployment", "caritas-signoz-otel-collector")]
    return deployment["spec"]["template"]["spec"]


class CollectorSecurityTest(unittest.TestCase):
    def assert_restricted_receiver(self, pod):
        self.assertEqual(
            pod["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        init = next(c for c in pod["initContainers"] if c["name"] == "check")
        receiver = next(c for c in pod["containers"] if c["name"] == "collector")
        for container in (init, receiver):
            self.assertEqual(
                container["securityContext"],
                {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
            )
        self.assertIn("--copy-path=/var/tmp/collector-config.yaml", receiver["args"])
        mount = next(m for m in receiver["volumeMounts"] if m["mountPath"] == "/var/tmp")
        volume = next(v for v in pod["volumes"] if v["name"] == mount["name"])
        self.assertEqual(volume["emptyDir"], {"medium": "Memory", "sizeLimit": "16Mi"})

    def test_dev_receiver_and_migration_check(self):
        self.assert_restricted_receiver(collector(by_key(render("values-dev.yaml"))))

    def test_private_receiver_uses_the_same_context(self):
        self.assert_restricted_receiver(collector(render_private()))

    def test_existing_temporary_mount_is_preserved(self):
        pod = collector(by_key(render("values-dev.yaml", helm_args=(
            "--set", "signoz.otelCollector.extraVolumeMounts[0].name=existing-tmp",
            "--set", "signoz.otelCollector.extraVolumeMounts[0].mountPath=/var/tmp",
            "--set", "signoz.otelCollector.extraVolumes[0].name=existing-tmp",
            "--set-json", "signoz.otelCollector.extraVolumes[0].emptyDir={}",
        ))))
        receiver = pod["containers"][0]
        temporary_mounts = [m for m in receiver["volumeMounts"] if m["mountPath"] == "/var/tmp"]
        self.assertEqual(temporary_mounts, [{"name": "existing-tmp", "mountPath": "/var/tmp"}])
        self.assertNotIn("otel-collector-config-copy", {v["name"] for v in pod["volumes"]})

    def test_explicit_writable_root_does_not_add_a_temporary_volume(self):
        pod = collector(by_key(render("values-dev.yaml", helm_args=(
            "--set", "signoz.otelCollector.securityContext.readOnlyRootFilesystem=false",
        ))))
        self.assertFalse(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        self.assertNotIn("otel-collector-config-copy", {v["name"] for v in pod["volumes"]})


if __name__ == "__main__":
    unittest.main()
