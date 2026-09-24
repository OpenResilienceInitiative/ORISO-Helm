#!/usr/bin/env python3
"""Render guard: Keycloak must be addressed by its public URL end to end.

Two misconfigurations broke /admin login on a fresh install (#331):

* admin-configmap prefixed `https://` onto `global.domains.auth`, which is
  the cluster-internal `http://keycloak:8080/`. The browser parsed
  `https://http://keycloak:8080/` with `http` as the hostname.
* KC_HOSTNAME was the bare domain, so Keycloak derived scheme and port from
  X-Forwarded-* headers. Where the ingress does not send them, the OIDC
  discovery document advertised `http://<domain>:8080/...` and every backend
  rejected the tokens on issuer mismatch.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN = "predev.example.org"


def render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "keycloak-public-url", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
            "--set-string", "consultingTypeService.smtpPasswordEncryptionSecret=render-test-secret",
            "--set", f"global.domainName={DOMAIN}",
            "--set", f"global.external.keycloakURL=https://{DOMAIN}/auth/",
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


def configmap(documents: list[dict], name: str) -> dict:
    return next(
        doc["data"]
        for doc in documents
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == name
    )


def main() -> None:
    documents = render()

    admin = configmap(documents, "admin-configmap")
    frontend = configmap(documents, "frontend-configmap")
    assert admin["REACT_APP_KEYCLOAK_URL"] == f"https://{DOMAIN}/auth/", admin
    # Admin and app must agree on where Keycloak lives.
    assert admin["REACT_APP_KEYCLOAK_URL"] == frontend["REACT_APP_KEYCLOAK_URL"]
    assert "https://http" not in admin["REACT_APP_KEYCLOAK_URL"]
    assert "keycloak:8080" not in admin["REACT_APP_KEYCLOAK_URL"]

    keycloak = configmap(documents, "keycloak-configmap-env")
    assert keycloak["KEYCLOAK_HOSTNAME"] == f"https://{DOMAIN}/auth", keycloak

    # Non-TLS installs keep working; only the scheme flips.
    plain = configmap(render("--set", "global.enableTls=false"), "keycloak-configmap-env")
    assert plain["KEYCLOAK_HOSTNAME"] == f"http://{DOMAIN}/auth", plain

    print("PASS: admin and Keycloak use the public Keycloak URL")


if __name__ == "__main__":
    main()
