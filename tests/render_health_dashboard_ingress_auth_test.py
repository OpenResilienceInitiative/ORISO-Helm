#!/usr/bin/env python3
"""Render the real chart to enforce the HealthDashboard ingress auth boundary."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART_DIR = Path(__file__).resolve().parents[1]
AUTH_ERROR = (
    "healthDashboard.ingress.authSecret must name an existing basic-auth Secret "
    "when HealthDashboard ingress is enabled"
)


def render(*overlays: str, health_dashboard: dict | None = None,
           expect_error: bool = False) -> list[dict] | str:
    command = [
        "helm", "template", "health-auth-test", str(CHART_DIR),
        "--namespace", "render-test",
        "-f", str(CHART_DIR / "values.yaml.default"),
        "-f", str(CHART_DIR / "secrets.yaml.default"),
        "--set-string", "global.domainName=health.example.test",
        "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
        "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret",
        "--set-string", "userService.smtpUser=smtp-canary-user",
        "--set-string", "userService.smtpPassword=smtp-canary-password",
    ]
    for overlay in overlays:
        command.extend(["-f", str(CHART_DIR / overlay)])
    with tempfile.TemporaryDirectory(prefix="health-auth-render-") as directory:
        if health_dashboard is not None:
            override = Path(directory) / "override.yaml"
            override.write_text(yaml.safe_dump({"healthDashboard": health_dashboard}))
            command.extend(["-f", str(override)])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    if expect_error:
        if result.returncode == 0:
            raise AssertionError("Helm accepted enabled HealthDashboard ingress with invalid authentication configuration")
        return result.stderr
    if result.returncode != 0:
        raise AssertionError(f"helm template failed:\n{result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def health_resources(docs: list[dict]) -> dict[str, dict]:
    return {
        doc["kind"]: doc for doc in docs
        if doc.get("metadata", {}).get("name")
        in {"health-auth-test-health-dashboard", "health-auth-test-health-dashboard-ingress"}
        and doc["kind"] in {"Deployment", "Service", "Ingress"}
    }


class HealthDashboardIngressAuthTest(unittest.TestCase):
    def test_default_disables_only_external_ingress(self) -> None:
        resources = health_resources(render())
        self.assertEqual(set(resources), {"Deployment", "Service"})
        service = resources["Service"]
        self.assertEqual(service["metadata"]["namespace"], "render-test")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"], [{
            "port": 9100, "targetPort": 9100, "protocol": "TCP", "name": "http",
        }])
        self.assertEqual(service["spec"]["selector"], {"app": "health-dashboard"})
        deployment = resources["Deployment"]
        self.assertEqual(deployment["metadata"]["namespace"], "render-test")
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], service["spec"]["selector"])

    def test_enabled_ingress_preserves_route_and_internal_resources(self) -> None:
        baseline = render()
        enabled = render(health_dashboard={"ingress": {
            "enabled": True, "authSecret": "health-auth-fixture",
        }})
        resources = health_resources(enabled)
        for kind in ("Deployment", "Service"):
            self.assertEqual(resources[kind], health_resources(baseline)[kind])
        self.assertEqual(
            [doc for doc in enabled if doc["kind"] == "Secret"],
            [doc for doc in baseline if doc["kind"] == "Secret"],
            "Enabling ingress must not generate credentials or a Secret",
        )
        ingress = resources["Ingress"]
        self.assertEqual(ingress["metadata"]["namespace"], "render-test")
        annotations = ingress["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], "health-auth-fixture")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-realm"], "HealthDashboard Authentication")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/ssl-redirect"], "true")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/rewrite-target"], "/$2")
        self.assertEqual(annotations["cert-manager.io/cluster-issuer"], "letsencrypt-prod")
        self.assertEqual(ingress["spec"], {
            "ingressClassName": "nginx",
            "tls": [{"hosts": ["health.example.test"], "secretName": "health-example-test-tls"}],
            "rules": [{"host": "health.example.test", "http": {"paths": [{
                "path": "/health(/|$)(.*)", "pathType": "ImplementationSpecific",
                "backend": {"service": {"name": resources["Service"]["metadata"]["name"],
                                         "port": {"number": 9100}}},
            }]}}],
        })

    def test_enabled_requires_nonblank_secret(self) -> None:
        for name, ingress in (
            ("absent", {"enabled": True}),
            ("null", {"enabled": True, "authSecret": None}),
            ("empty", {"enabled": True, "authSecret": ""}),
            ("whitespace", {"enabled": True, "authSecret": " \t\n "}),
        ):
            with self.subTest(secret=name):
                self.assertIn(AUTH_ERROR, render(health_dashboard={"ingress": ingress}, expect_error=True))

    def test_rejects_namespace_qualified_secret(self) -> None:
        for namespace in ("", "health-operators"):
            with self.subTest(namespace=namespace):
                error = render(health_dashboard={
                    "namespace": namespace,
                    "ingress": {"enabled": True, "authSecret": "other-namespace/health-auth-fixture"},
                }, expect_error=True)
                self.assertIn(
                    "healthDashboard.ingress.authSecret must be a local Secret name without '/' "
                    "in the effective HealthDashboard namespace", error,
                )

    def test_rejects_invalid_kubernetes_secret_names(self) -> None:
        for secret in (
            "health_auth", "Health-auth", "health auth", "-health", "health-",
            "health..auth", "health-.auth", "health.-auth", ".health", "health.",
            "a" * 254,
        ):
            with self.subTest(secret=secret):
                error = render(health_dashboard={"ingress": {
                    "enabled": True, "authSecret": secret,
                }}, expect_error=True)
                self.assertIn(
                    "healthDashboard.ingress.authSecret must be a valid Kubernetes Secret name "
                    "(DNS subdomain, at most 253 characters)", error,
                )

    def test_accepts_kubernetes_secret_name_boundaries(self) -> None:
        # Secret names use DNS subdomain validation, not the stricter label helper.
        # Kubernetes imposes only the 253-character total limit here.
        for secret in ("123", "1health.auth-fixture", "a" * 64, "a" * 253):
            with self.subTest(secret=secret):
                resources = health_resources(render(health_dashboard={"ingress": {
                    "enabled": True, "authSecret": secret,
                }}))
                annotations = resources["Ingress"]["metadata"]["annotations"]
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], secret)
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")

    def test_disabled_does_not_require_secret(self) -> None:
        for secret in (None, "", " \t\n ", "health_auth"):
            with self.subTest(secret=repr(secret)):
                resources = health_resources(render(health_dashboard={"ingress": {
                    "enabled": False, "authSecret": secret,
                }}))
                self.assertEqual(set(resources), {"Deployment", "Service"})

    def test_trims_secret(self) -> None:
        resources = health_resources(render(health_dashboard={"ingress": {
            "enabled": True, "authSecret": " \t health-auth-fixture \n",
        }}))
        annotations = resources["Ingress"]["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], "health-auth-fixture")

    def test_realm_is_constant_despite_unsafe_overrides(self) -> None:
        for realm in ("off", "OFF", "$host", "Health $http_x_auth_realm"):
            with self.subTest(realm=realm):
                resources = health_resources(render(health_dashboard={"ingress": {
                    "enabled": True, "authSecret": "health-auth-fixture", "authRealm": realm,
                }}))
                annotations = resources["Ingress"]["metadata"]["annotations"]
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-realm"],
                                 "HealthDashboard Authentication")

    def test_namespace_and_service_port_override(self) -> None:
        resources = health_resources(render(health_dashboard={
            "namespace": "health-operators", "service": {"port": 9200, "targetPort": 9300},
            "ingress": {"enabled": True, "authSecret": "health-auth-fixture"},
        }))
        for resource in resources.values():
            self.assertEqual(resource["metadata"]["namespace"], "health-operators")
        backend = resources["Ingress"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
        self.assertEqual(backend["name"], resources["Service"]["metadata"]["name"])
        self.assertEqual(backend["port"]["number"], resources["Service"]["spec"]["ports"][0]["port"])
        self.assertEqual(resources["Service"]["spec"]["ports"][0]["targetPort"], 9300)

    def test_dev_enables_auth_and_can_be_disabled(self) -> None:
        resources = health_resources(render("values-dev.yaml"))
        annotations = resources["Ingress"]["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], "health-dashboard-basic-auth")
        disabled = health_resources(render("values-dev.yaml", health_dashboard={"ingress": {
            "enabled": False, "authSecret": None,
        }}))
        self.assertEqual(set(disabled), {"Deployment", "Service"})

    def test_other_overlays_keep_ingress_disabled(self) -> None:
        for overlay in ("values-pre-dev.yaml", "values-prod.yaml"):
            with self.subTest(overlay=overlay):
                self.assertEqual(set(health_resources(render(overlay))), {"Deployment", "Service"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
