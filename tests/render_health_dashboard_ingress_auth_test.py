#!/usr/bin/env python3
"""Render the real chart to enforce the HealthDashboard ingress auth boundary."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART_DIR = Path(__file__).resolve().parents[1]
DEFAULT_AUTH_SECRET = "health-dashboard-basic-auth"
AUTH_ERROR = (
    "healthDashboard.ingress.authSecret must name an existing basic-auth Secret "
    "when HealthDashboard ingress is enabled"
)


def render(*overlays: str, health_dashboard: dict | None = None,
           global_values: dict | None = None,
           expect_error: bool = False, domain: str = "health.oriso.internal") -> list[dict] | str:
    command = [
        "helm", "template", "health-auth-test", str(CHART_DIR),
        "--namespace", "render-test",
        "-f", str(CHART_DIR / "values.yaml.default"),
        "-f", str(CHART_DIR / "tests" / "fixtures" / "values-render-domain.yaml"),
        "-f", str(CHART_DIR / "secrets.yaml.default"),
        "-f", str(CHART_DIR / "tests" / "fixtures" / "render-required-secrets.yaml"),
        "--set-string", f"global.domainName={domain}",
        # Pre-Dev enables SigNoz, whose validation ties this URL to domainName.
        "--set-string", f"signoz.signoz.env.signoz_global_external__url=https://{domain}/signoz",
        "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
        "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "--set-string", "userService.smtpUser=smtp-canary-user",
        "--set-string", "userService.smtpPassword=smtp-canary-password",
    ]
    for overlay in overlays:
        command.extend(["-f", str(CHART_DIR / overlay)])
    with tempfile.TemporaryDirectory(prefix="health-auth-render-") as directory:
        values = {}
        if health_dashboard is not None:
            values["healthDashboard"] = health_dashboard
        if global_values is not None:
            values["global"] = global_values
        if values:
            override = Path(directory) / "override.yaml"
            override.write_text(yaml.safe_dump(values))
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
    def test_operator_access_configures_only_the_dashboard_client_and_role(self) -> None:
        origin = "https://health.oriso.internal"
        docs = render(health_dashboard={"ingress": {"basicAuth": False}}, global_values={
            "healthDashboardOperatorAccess": True,
            "keycloak": {"realm": "online-beratung"},
        })
        resources = health_resources(docs)
        self.assertNotIn("nginx.ingress.kubernetes.io/auth-type", resources["Ingress"]["metadata"]["annotations"])
        container = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["ORISO_DASHBOARD_OIDC_ISSUER"], f"{origin}/auth/realms/online-beratung")
        self.assertEqual(env["ORISO_DASHBOARD_OIDC_CLIENT_ID"], "health-dashboard")

        config = next(doc for doc in docs if doc["kind"] == "ConfigMap" and
                      doc["metadata"]["name"] == "keycloak-configmap-data")
        realm = json.loads(config["data"]["realm.json"])
        client = next(client for client in realm["clients"] if client["clientId"] == "health-dashboard")
        self.assertTrue(client["publicClient"])
        self.assertTrue(client["standardFlowEnabled"])
        for flag in ("implicitFlowEnabled", "directAccessGrantsEnabled", "serviceAccountsEnabled", "fullScopeAllowed"):
            self.assertFalse(client[flag])
        self.assertEqual(client["redirectUris"], [f"{origin}/health/auth/callback"])
        self.assertEqual(client["webOrigins"], [origin])
        self.assertEqual(client["attributes"]["post.logout.redirect.uris"], f"{origin}/health/")
        self.assertEqual(client["attributes"]["pkce.code.challenge.method"], "S256")
        mappers = {entry["protocolMapper"]: entry["config"] for entry in client["protocolMappers"]}
        self.assertEqual(mappers["oidc-audience-mapper"]["included.client.audience"], "health-dashboard")
        self.assertEqual(mappers["oidc-usermodel-client-role-mapper"]["claim.name"], "resource_access.health-dashboard.roles")
        roles = realm["roles"]["client"]["health-dashboard"]
        self.assertEqual([role["name"] for role in roles], ["view-helm-images"])
        self.assertEqual(realm["clientScopeMappings"]["health-dashboard"], [{
            "client": "health-dashboard",
            "roles": ["view-helm-images"],
        }])

    def test_operator_access_requires_a_public_dashboard_on_https(self) -> None:
        flags = {"healthDashboardOperatorAccess": True, "keycloak": {"realm": "online-beratung"}}
        for values in ({"ingress": {"basicAuth": True}}, {"ingress": {"enabled": False}}):
            with self.subTest(values=values):
                self.assertIn("HealthDashboard operator access requires a public ingress",
                              render(health_dashboard=values, global_values=flags, expect_error=True))
        self.assertIn("HealthDashboard operator access requires global.enableTls",
                      render(health_dashboard={"ingress": {"basicAuth": False}},
                             global_values={**flags, "enableTls": False}, expect_error=True))
        self.assertIn("global.healthDashboardOperatorAccess must be a boolean",
                      render(health_dashboard={"ingress": {"basicAuth": False}},
                             global_values={**flags, "healthDashboardOperatorAccess": "false"},
                             expect_error=True))

    def test_default_chart_has_no_dashboard_oidc_configuration(self) -> None:
        docs = render()
        deployment = health_resources(docs)["Deployment"]
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertFalse(any(item["name"].startswith("ORISO_DASHBOARD_OIDC_") for item in env))
        config = next(doc for doc in docs if doc["kind"] == "ConfigMap" and
                      doc["metadata"]["name"] == "keycloak-configmap-data")
        realm = json.loads(config["data"]["realm.json"])
        self.assertFalse(any(client["clientId"] == "health-dashboard" for client in realm["clients"]))

    def test_explicit_off_disables_only_external_ingress(self) -> None:
        resources = health_resources(render(health_dashboard={"ingress": {"enabled": False}}))
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

    def test_default_ingress_and_inherited_secret_require_basic_auth(self) -> None:
        for override in (None, {"ingress": {"enabled": True}}):
            with self.subTest(override=override):
                resources = health_resources(render(health_dashboard=override))
                self.assertEqual(set(resources), {"Deployment", "Service", "Ingress"})
                annotations = resources["Ingress"]["metadata"]["annotations"]
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], "health-dashboard-basic-auth")

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
            "tls": [{"hosts": ["health.oriso.internal"], "secretName": "health-oriso-internal-tls"}],
            "rules": [{"host": "health.oriso.internal", "http": {"paths": [{
                "path": "/health(/|$)(.*)", "pathType": "ImplementationSpecific",
                "backend": {"service": {"name": resources["Service"]["metadata"]["name"],
                                         "port": {"number": 9100}}},
            }]}}],
        })

    def test_enabled_requires_nonblank_secret(self) -> None:
        for name, ingress in (
            ("null", {"enabled": True, "authSecret": None}),
            ("empty", {"enabled": True, "authSecret": ""}),
            ("whitespace", {"enabled": True, "authSecret": " \t\n "}),
        ):
            with self.subTest(secret=name):
                self.assertIn(AUTH_ERROR, render(health_dashboard={"ingress": ingress}, expect_error=True))

    def test_absent_secret_uses_the_shipped_default_name(self) -> None:
        # Only an omitted key falls back to values.yaml.default; an explicit
        # null, empty or blank value is rejected above.
        resources = health_resources(render(health_dashboard={"ingress": {"enabled": True}}))
        annotations = resources["Ingress"]["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], DEFAULT_AUTH_SECRET)
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")

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

    def test_dev_is_public_with_operator_login_and_can_be_disabled(self) -> None:
        # Product decision 25 Nov 2026: /health is public on Dev only; the Helm
        # image inventory needs the Keycloak operator login (HealthDashboard#58).
        resources = health_resources(render("values-dev.yaml", domain="dev.oriso.org"))
        annotations = resources["Ingress"]["metadata"].get("annotations") or {}
        self.assertNotIn("nginx.ingress.kubernetes.io/auth-type", annotations)
        self.assertNotIn("nginx.ingress.kubernetes.io/auth-secret", annotations)
        # Without an ingress there is no operator login either.
        disabled = health_resources(render("values-dev.yaml", domain="dev.oriso.org", health_dashboard={"ingress": {
            "enabled": False, "authSecret": None,
        }}, global_values={"healthDashboardOperatorAccess": False}))
        self.assertEqual(set(disabled), {"Deployment", "Service"})

    def test_other_overlays_keep_ingress_guarded_and_allow_explicit_off(self) -> None:
        for overlay in ("values-pre-dev.yaml", "values-prod.yaml"):
            # Pre-Dev is deployed as an overlay on top of Dev.
            layers = ("values-dev.yaml", overlay) if overlay == "values-pre-dev.yaml" else (overlay,)
            with self.subTest(overlay=overlay):
                resources = health_resources(render(*layers))
                self.assertEqual(set(resources), {"Deployment", "Service", "Ingress"})
                annotations = resources["Ingress"]["metadata"]["annotations"]
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")
                self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], "health-dashboard-basic-auth")
                disabled = health_resources(render(*layers, health_dashboard={"ingress": {"enabled": False}}))
                self.assertEqual(set(disabled), {"Deployment", "Service"})

    def test_basic_auth_true_matches_default(self) -> None:
        default = health_resources(render())
        explicit = health_resources(render(health_dashboard={"ingress": {"enabled": True, "basicAuth": True}}))
        self.assertEqual(explicit, default)

    def test_basic_auth_false_serves_public_route_without_auth(self) -> None:
        # HealthDashboard#57: public view without login; the image's public mode withholds internals.
        auth = health_resources(render())["Ingress"]
        for secret in (DEFAULT_AUTH_SECRET, None, ""):
            with self.subTest(secret=repr(secret)):
                docs = render(health_dashboard={"ingress": {
                    "enabled": True, "basicAuth": False, "authSecret": secret, "htpasswd": "admin:$apr1$x$y",
                }})
                public = health_resources(docs)["Ingress"]
                annotations = public["metadata"]["annotations"]
                self.assertFalse([k for k in annotations if k.startswith("nginx.ingress.kubernetes.io/auth-")])
                kept = {k: v for k, v in auth["metadata"]["annotations"].items()
                        if not k.startswith("nginx.ingress.kubernetes.io/auth-")}
                self.assertEqual(annotations, kept)
                self.assertEqual(public["spec"], auth["spec"])
                self.assertFalse(
                    [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == DEFAULT_AUTH_SECRET],
                    "basicAuth=false must not render the Basic Auth Secret",
                )

    def test_basic_auth_false_requires_public_mode(self) -> None:
        for value in (False, "false", "FALSE", " False "):
            with self.subTest(value=value):
                error = render(health_dashboard={
                    "ingress": {"enabled": True, "basicAuth": False},
                    "env": {"ORISO_PUBLIC_MODE": value},
                }, expect_error=True)
                self.assertIn("healthDashboard.ingress.basicAuth=false makes /health public and requires public mode", error)
        for value in (True, "true"):
            with self.subTest(value=value):
                resources = health_resources(render(health_dashboard={
                    "ingress": {"enabled": True, "basicAuth": False},
                    "env": {"ORISO_PUBLIC_MODE": value},
                }))
                self.assertNotIn("nginx.ingress.kubernetes.io/auth-type",
                                 resources["Ingress"]["metadata"]["annotations"])

    def test_basic_auth_allows_private_application_mode(self) -> None:
        resources = health_resources(render(health_dashboard={
            "ingress": {"basicAuth": True}, "env": {"ORISO_PUBLIC_MODE": False},
        }))
        self.assertEqual(resources["Ingress"]["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/auth-type"], "basic")

    def test_basic_auth_rejects_non_boolean(self) -> None:
        for value in ("false", "no", 0, None):
            with self.subTest(value=repr(value)):
                error = render(health_dashboard={"ingress": {"enabled": True, "basicAuth": value}}, expect_error=True)
                self.assertIn("healthDashboard.ingress.basicAuth must be a boolean", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
