#!/usr/bin/env python3
"""Render guard: Keycloak mails get the platform logo from this environment.

The `oriso` email theme (ORISO-Keycloak) shows the recipient's Träger logo
from TenantService, and for users without a Träger the platform logo from
`ORISO_LOGO_URL`. The theme admits only an address beneath the HTTPS
`ORISO_APP_BASE_URL`, so both must come from the same public origin, and no
host may be written into the chart.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN = "predev.oriso.internal"


def render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "keycloak-mail-logo", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
            "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret",
            "--set", f"global.domainName={DOMAIN}",
            "--set-string", "userService.smtpUser=smtp-canary-user",
            "--set-string", "userService.smtpPassword=smtp-canary-password",
            *extra_args,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit("helm template failed")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def keycloak_env(documents: list[dict]) -> dict:
    return next(
        doc["data"]
        for doc in documents
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "keycloak-configmap-env"
    )


def keycloak_container_env_names(documents: list[dict]) -> set[str]:
    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "keycloak"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"] for entry in container.get("env", [])}


def main() -> None:
    documents = render()
    env = keycloak_env(documents)

    origin = f"https://{DOMAIN}"
    assert env["ORISO_APP_BASE_URL"] == origin, env
    # Host-resolved route: the logo of the tenant this domain belongs to, else
    # the platform tenant's. Beneath the app origin, as the theme requires.
    assert env["ORISO_LOGO_URL"] == f"{origin}/service/tenant/public/branding/logo", env
    assert "app.oriso.org" not in env["ORISO_LOGO_URL"], env

    names = keycloak_container_env_names(documents)
    # `${env.X}` in theme.properties reads the container environment.
    assert {"ORISO_APP_BASE_URL", "ORISO_LOGO_URL"} <= names, names

    plain = keycloak_env(render("--set", "global.enableTls=false"))
    assert plain["ORISO_LOGO_URL"] == f"http://{DOMAIN}/service/tenant/public/branding/logo", plain

    print("PASS: Keycloak gets ORISO_APP_BASE_URL and ORISO_LOGO_URL from global.domainName")


if __name__ == "__main__":
    main()
