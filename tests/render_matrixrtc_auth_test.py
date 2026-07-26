#!/usr/bin/env python3
"""Render guard for the single-issuer MatrixRTC authorization boundary."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "matrixrtc-auth",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string",
            "livekit.api.key=test-livekit-key",
            "--set-string",
            "livekit.api.secret=test-livekit-secret",
            "--set-string",
            "livekit.matrixAdminToken=test-matrix-admin-token",
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def find(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def main() -> None:
    documents = render()
    names = {
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in documents
    }

    assert ("ConfigMap", "livekit-token-service-script") not in names
    assert ("ConfigMap", "livekit-token-service-configmap-env") not in names
    assert ("Deployment", "livekit-token-service") not in names

    gateway = find(documents, "Deployment", "matrixrtc-auth-policy-gateway")
    upstream = find(documents, "Deployment", "matrixrtc-authorization-service")
    livekit = find(documents, "Deployment", "livekit")
    ingress = find(documents, "Ingress", "livekit-jwt-ingress")
    livekit_secret = find(documents, "Secret", "livekit-config")
    auth_secret = find(documents, "Secret", "matrixrtc-auth-secrets")

    gateway_container = gateway["spec"]["template"]["spec"]["containers"][0]
    upstream_container = upstream["spec"]["template"]["spec"]["containers"][0]
    livekit_container = livekit["spec"]["template"]["spec"]["containers"][0]

    for container in (gateway_container, upstream_container, livekit_container):
        assert "@sha256:" in container["image"]
        assert not container["image"].endswith(":latest")

    assert gateway_container["securityContext"]["runAsNonRoot"] is True
    assert upstream_container["securityContext"]["runAsNonRoot"] is True
    assert livekit["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] == (
        "livekit-config"
    )

    livekit_config = livekit_secret["stringData"]["config.yaml"]
    assert "auto_create: false" in livekit_config
    assert "matrixrtc-authorization-service:8080/sfu_webhook" in livekit_config
    assert "changeme" not in yaml.safe_dump(livekit_secret)
    assert "changeme" not in yaml.safe_dump(auth_secret)

    ingress_backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert ingress_backend["service"]["name"] == "matrixrtc-auth-policy-gateway"
    assert ingress["metadata"]["annotations"][
        "nginx.ingress.kubernetes.io/limit-rps"
    ] == "10"

    rendered = yaml.safe_dump_all(documents)
    assert "LIVEKIT_FULL_ACCESS_HOMESERVERS" in rendered
    assert "MATRIX_ADMIN_TOKEN_FILE" in rendered
    assert "LIVEKIT_API_SECRET" not in rendered

    print("PASS: MatrixRTC auth renders one public policy gateway and secret-only credentials")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
