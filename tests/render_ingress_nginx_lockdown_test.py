#!/usr/bin/env python3
"""Helm#536: the bundled ingress-nginx controller accepts no snippets and reads
only its own namespace.

Snippet annotations let anyone who can write an Ingress run nginx config inside
the controller (its TLS keys, its Secret access, every host's traffic). The
chart therefore renders no snippet anywhere, with every optional route on, and
the controller only sees this release namespace.
"""
from __future__ import annotations

import os
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMESPACE = "caritas"
PREFIX = "nginx.ingress.kubernetes.io/"
FIXTURES = os.path.join(CHART_DIR, "tests", "fixtures")
BASE = [
    "-f", os.path.join(CHART_DIR, "values.yaml.default"),
    "-f", os.path.join(FIXTURES, "values-render-domain.yaml"),
    "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
    "-f", os.path.join(FIXTURES, "render-required-secrets.yaml"),
    "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
    "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
    "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    "--set-string", "userService.smtpUser=smtp-canary-user",
    "--set-string", "userService.smtpPassword=smtp-canary-password",
]
# Every route that used to carry a snippet: Dev (Storybook, SigNoz RUM and E2E
# ingest), the media scanner block, the token rate limit and the inactivity gate.
ALL_ROUTES = [
    "-f", os.path.join(CHART_DIR, "values-dev.yaml"),
    "-f", os.path.join(FIXTURES, "values-media-scanner-clamav.yaml"),
    "--set-string", "global.domainName=dev.oriso.org",
    "--set", "global.keycloak.tokenRateLimit.enabled=true",
    "--set", "global.accountInactivity.accessGateEnabled=true",
]


def helm(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "caritas", CHART_DIR, "--namespace", NAMESPACE, *BASE, *extra],
                          capture_output=True, text=True)


def render(*extra: str) -> list[dict]:
    result = helm(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def find(docs: list[dict], kind: str, name: str) -> dict:
    return next(doc for doc in docs if doc.get("kind") == kind and doc["metadata"]["name"] == name)


def assert_no_snippets(docs: list[dict]) -> None:
    ingresses = [doc for doc in docs if doc.get("kind") == "Ingress"]
    assert ingresses
    for ingress in ingresses:
        name = ingress["metadata"]["name"]
        annotations = ingress["metadata"].get("annotations") or {}
        assert not [key for key in annotations if "snippet" in key], (name, annotations)
        assert "kubernetes.io/ingress.class" not in annotations, name
        assert ingress["spec"].get("ingressClassName") == "nginx", name
        # --watch-namespace: an Ingress elsewhere would never be served.
        assert ingress["metadata"].get("namespace", NAMESPACE) == NAMESPACE, name


def assert_custom_headers_resolve(docs: list[dict], allowed: set[str]) -> None:
    references = [doc["metadata"]["annotations"][PREFIX + "custom-headers"]
                  for doc in docs if doc.get("kind") == "Ingress"
                  and PREFIX + "custom-headers" in (doc["metadata"].get("annotations") or {})]
    assert references, "the Permissions-Policy routes must use custom-headers"
    for reference in references:
        namespace, name = reference.split("/")
        assert namespace == NAMESPACE, reference
        headers = find(docs, "ConfigMap", name)["data"]
        # ingress-nginx denies the whole location for a header it does not allow.
        assert set(headers) <= allowed, (reference, headers)


def assert_controller(docs: list[dict]) -> None:
    config = find(docs, "ConfigMap", "ingress-nginx-controller")["data"]
    assert config["allow-snippet-annotations"] == "false"
    assert config["annotations-risk-level"] == "High"
    assert config["limit-req-status-code"] == "429"
    assert config["hsts"] == "true" and int(config["hsts-max-age"]) >= 31536000
    allowed = {header.strip() for header in config["global-allowed-response-headers"].split(",")}
    assert "Permissions-Policy" in allowed
    assert_custom_headers_resolve(docs, allowed)

    container = find(docs, "Deployment", "ingress-nginx-controller")["spec"]["template"]["spec"]["containers"][0]
    assert "--watch-namespace=$(POD_NAMESPACE)" in container["args"], container["args"]

    cluster_roles = [doc for doc in docs if doc.get("kind") == "ClusterRole"
                     and doc["metadata"]["name"].startswith("ingress-nginx")]
    assert [role["metadata"]["name"] for role in cluster_roles] == [f"ingress-nginx-{NAMESPACE}"]
    for rule in cluster_roles[0]["rules"]:
        assert "secrets" not in rule["resources"], rule
        if "namespaces" in rule["resources"]:
            assert rule.get("resourceNames") == [NAMESPACE], rule
    binding = find(docs, "ClusterRoleBinding", f"ingress-nginx-{NAMESPACE}")
    assert binding["roleRef"]["name"] == f"ingress-nginx-{NAMESPACE}"

    role = find(docs, "Role", "ingress-nginx")
    assert role["metadata"]["namespace"] == NAMESPACE
    assert any("secrets" in rule["resources"] for rule in role["rules"])
    role_binding = find(docs, "RoleBinding", "ingress-nginx")
    assert role_binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "ingress-nginx"}
    assert role_binding["subjects"] == [{"kind": "ServiceAccount", "name": "ingress-nginx", "namespace": NAMESPACE}]


def assert_acme_email() -> None:
    docs = render()
    issuer = find(docs, "ClusterIssuer", "letsencrypt-prod")
    assert issuer["spec"]["acme"]["email"] == "acme@render.oriso.internal"
    for bad in ("", "not-an-email"):
        result = helm("--set-string", f"cert-manager.acmeEmail={bad}")
        assert result.returncode != 0 and "cert-manager.acmeEmail" in result.stderr, (bad, result.stderr)
    without = render("--set", "cert-manager.enabled=false", "--set-string", "cert-manager.acmeEmail=")
    assert not any(doc.get("kind") == "ClusterIssuer" for doc in without)


def main() -> None:
    for docs in (render(), render(*ALL_ROUTES)):
        assert_no_snippets(docs)
        assert_controller(docs)
    everything = render(*ALL_ROUTES)
    names = {doc["metadata"]["name"] for doc in everything if doc.get("kind") == "Ingress"}
    # The routes that used snippets really are part of this render.
    assert {"storybook-dev-ingress", "media-scanner-direct-media-block-ingress", "keycloak-token-rate-limit-ingress",
            "account-inactivity-api-ingress", "caritas-signoz-rum-ingress", "caritas-signoz-e2e-ingress",
            "caritas-signoz-slash-redirect"} <= names, names
    assert_acme_email()
    print("PASS: no snippets, namespace-scoped controller, required ACME contact")


if __name__ == "__main__":
    main()
