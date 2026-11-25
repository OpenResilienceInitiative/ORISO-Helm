#!/usr/bin/env python3
"""Render-test the authenticated Dev Storybook route."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORYBOOK_APPS = ("storybook-admin", "storybook-frontend")
STORYBOOK_NAMES = {*STORYBOOK_APPS, "storybook-dev-ingress", *(f"{app}-slash-redirect" for app in STORYBOOK_APPS)}


def render(*values_files: str) -> list[dict]:
    """Render the chart with the requested values files and return YAML docs."""
    command = [
        "helm",
        "template",
        "storybook-test",
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f",
        os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
        "--set-string",
        "global.domainName=dev.oriso.org",
        "--set-string",
        "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
        "--set-string",
        "consultingTypeService.smtpPasswordEncryptionSecret=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "--set-string",
        "userService.smtpUser=smtp-canary-user",
        "--set-string",
        "userService.smtpPassword=smtp-canary-password",
    ]
    for values_file in values_files:
        command.extend(["-f", os.path.join(CHART_DIR, values_file)])
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed:\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def main() -> None:
    """Assert Storybook is disabled by default and correctly enabled for Dev."""
    baseline = render()
    assert not any(doc.get("metadata", {}).get("name") in STORYBOOK_NAMES for doc in baseline), (
        "Storybook routes and workloads must be disabled by default"
    )

    dev = render("values-dev.yaml")
    resources = {
        (doc.get("kind"), doc.get("metadata", {}).get("name")): doc
        for doc in dev
        if doc.get("metadata", {}).get("name") in STORYBOOK_NAMES
    }
    assert set(resources) == {
        ("Deployment", "storybook-frontend"),
        ("Deployment", "storybook-admin"),
        ("Service", "storybook-frontend"),
        ("Service", "storybook-admin"),
        ("Ingress", "storybook-dev-ingress"),
        ("Ingress", "storybook-admin-slash-redirect"),
        ("Ingress", "storybook-frontend-slash-redirect"),
    }, "Dev must render two snippet-free Storybook redirects and the content route"

    container = resources[("Deployment", "storybook-frontend")]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/openresilienceinitiative/oriso-storybook:dev"
    assert resources[("Deployment", "storybook-frontend")]["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "registry-secret"}
    ]
    admin_container = resources[("Deployment", "storybook-admin")]["spec"]["template"]["spec"]["containers"][0]
    assert admin_container["image"] == "ghcr.io/openresilienceinitiative/oriso-storybook:dev"
    assert resources[("Deployment", "storybook-admin")]["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "registry-secret"}
    ]

    ingress = resources[("Ingress", "storybook-dev-ingress")]
    annotations = ingress["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert annotations["nginx.ingress.kubernetes.io/auth-secret"] == "storybook-basic-auth"
    assert annotations["nginx.ingress.kubernetes.io/use-regex"] == "true"
    assert annotations["nginx.ingress.kubernetes.io/rewrite-target"] == "/$1"
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert {path["path"] for path in paths} == {f"/{app}/(.*)" for app in STORYBOOK_APPS}
    assert ingress["spec"]["rules"][0]["host"] == "dev.oriso.org"
    assert ingress["spec"]["tls"] == [{"hosts": ["dev.oriso.org"], "secretName": "dev-oriso-org-tls"}]
    for app in STORYBOOK_APPS:
        redirect = resources[("Ingress", f"{app}-slash-redirect")]
        redirection = redirect["metadata"]["annotations"]
        assert redirection["nginx.ingress.kubernetes.io/permanent-redirect"] == f"https://dev.oriso.org/{app}/"
        assert redirection["nginx.ingress.kubernetes.io/permanent-redirect-code"] == "308"
        assert redirection["nginx.ingress.kubernetes.io/use-regex"] == "true"
        assert redirection["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
        rule = redirect["spec"]["rules"][0]
        assert rule["host"] == "dev.oriso.org"
        assert rule["http"]["paths"] == [{
            "path": f"/{app}$",
            "pathType": "ImplementationSpecific",
            "backend": {"service": {"name": app, "port": {"number": 80}}},
        }]

    for (kind, _), resource in resources.items():
        if kind == "Ingress":
            assert resource["spec"]["ingressClassName"] == "nginx"
            assert not any(key.endswith("-snippet") for key in resource["metadata"]["annotations"])
    print("PASS: Dev renders authenticated Frontend and Admin Storybook routes")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
