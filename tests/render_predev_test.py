#!/usr/bin/env python3
"""Render the committed PreDev overlay and lock its runtime contract."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDEV_KEYCLOAK_URL = "https://auth.oriso-dev.site"
PREDEV_KEYCLOAK_REALM = "online-beratung"
PREDEV_KEYCLOAK_JWK_SET_URI = (
    "http://keycloak:8080/realms/online-beratung/protocol/openid-connect/certs"
)


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def render() -> list[dict]:
    overlay = os.path.join(CHART_DIR, "values-pre-dev.yaml")
    if not os.path.isfile(overlay):
        fail("values-pre-dev.yaml is missing")

    command = [
        "helm",
        "template",
        "oriso",
        CHART_DIR,
        "-f",
        os.path.join(CHART_DIR, "values.yaml.default"),
        "-f",
        overlay,
        "-f",
        os.path.join(CHART_DIR, "secrets.yaml.default"),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"PreDev chart render failed:\n{result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    for document in documents:
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    fail(f"rendered {kind}/{name} is missing")


def main() -> None:
    documents = render()

    tenantservice_config = resource(documents, "ConfigMap", "tenantservice-configmap-env")["data"]
    expected_tenant_auth_config = {
        "KEYCLOAK_AUTH_SERVER_URL": "http://keycloak:8080",
        "KEYCLOAK_REALM": PREDEV_KEYCLOAK_REALM,
        "SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_ISSUER_URI": (
            f"{PREDEV_KEYCLOAK_URL}/realms/{PREDEV_KEYCLOAK_REALM}"
        ),
        "SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_JWK_SET_URI": PREDEV_KEYCLOAK_JWK_SET_URI,
    }
    for key, expected_value in expected_tenant_auth_config.items():
        if tenantservice_config.get(key) != expected_value:
            fail(f"TenantService {key} is not configured for the PreDev Keycloak realm")

    userservice_config = resource(documents, "ConfigMap", "userservice-configmap-env")["data"]

    # Magic Link login and password-reset emails link back to this frontend URL.
    # The chart default (https://app.oriso.org) does not resolve, so PreDev must
    # override it — password reset otherwise fails closed (no email sent) and
    # Magic Link emails a broken link. See ORISO-UserService PasswordResetService
    # / MagicLinkLoginService.
    PREDEV_FRONTEND_URL = "https://app.oriso-dev.site"
    if userservice_config.get("MAGIC_LINK_FRONTEND_BASE_URL") != PREDEV_FRONTEND_URL:
        fail("UserService MAGIC_LINK_FRONTEND_BASE_URL is not the PreDev frontend URL")
    if userservice_config.get("PASSWORD_RESET_FRONTEND_BASE_URL") != PREDEV_FRONTEND_URL:
        fail("UserService PASSWORD_RESET_FRONTEND_BASE_URL is not the PreDev frontend URL")

    print("PASS: committed PreDev Helm overlay renders the expected runtime contract")


if __name__ == "__main__":
    main()
