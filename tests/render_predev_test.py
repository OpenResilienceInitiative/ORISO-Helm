#!/usr/bin/env python3
"""Render the committed PreDev overlay and lock its runtime contract."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDEV_DOMAIN = "matrix.oriso-dev.site"
PREDEV_PUBLIC_IP = "46.224.170.69"
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
        os.path.join(CHART_DIR, "ci/placeholder-secrets.yaml"),
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
    if userservice_config.get("MATRIX_SERVER_NAME") != PREDEV_DOMAIN:
        fail("UserService MATRIX_SERVER_NAME is not the PreDev Matrix identity")
    if userservice_config.get("MATRIX_ENCRYPTION_ENABLED") != "true":
        fail("UserService MATRIX_ENCRYPTION_ENABLED is not true in PreDev")

    userservice = resource(documents, "Deployment", "userservice")
    environment = userservice["spec"]["template"]["spec"]["containers"][0]["env"]
    encryption_variable = next(
        (entry for entry in environment if entry.get("name") == "MATRIX_ENCRYPTION_ENABLED"),
        None,
    )
    expected_reference = {
        "key": "MATRIX_ENCRYPTION_ENABLED",
        "name": "userservice-configmap-env",
    }
    if encryption_variable is None or encryption_variable.get("valueFrom", {}).get(
        "configMapKeyRef"
    ) != expected_reference:
        fail("UserService Deployment does not consume MATRIX_ENCRYPTION_ENABLED from its ConfigMap")

    matrix_config = resource(documents, "ConfigMap", "matrix-homeserver-oidc")["data"]
    homeserver = yaml.safe_load(matrix_config["homeserver.yaml"])
    if homeserver.get("server_name") != PREDEV_DOMAIN:
        fail("Synapse server_name is not the PreDev Matrix identity")
    if homeserver.get("public_baseurl") != f"https://{PREDEV_DOMAIN}":
        fail("Synapse public_baseurl is not the public PreDev Matrix URL")
    if homeserver.get("federation_domain_whitelist") != []:
        fail("Synapse federation whitelist is not closed")
    if homeserver.get("allow_public_rooms_over_federation") is not False:
        fail("Synapse public-room federation is not disabled")
    if PREDEV_PUBLIC_IP not in homeserver.get("exempt_from_ratelimiting", []):
        fail("PreDev server public IP is not present in the Synapse rate-limit exemptions")

    discovery = resource(documents, "ConfigMap", "matrix-discovery-data")["data"]
    for filename in ("caritas.local", "caritas2.local"):
        delegated_host = yaml.safe_load(discovery[filename])["m.server"].rsplit(":", 1)[0]
        if delegated_host != PREDEV_DOMAIN or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", delegated_host):
            fail(f"{filename} does not delegate to the domain-based PreDev Matrix identity")

    print("PASS: committed PreDev Helm overlay renders the expected Matrix and encryption contract")


if __name__ == "__main__":
    main()
