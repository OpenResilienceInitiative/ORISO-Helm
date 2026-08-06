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
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "matrixrtcAuth.callPolicyToken=test-only-call-policy-token-with-at-least-48-characters",
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
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
    userservice = find(documents, "Deployment", "userservice")
    livekit = find(documents, "Deployment", "livekit")
    ingress = find(documents, "Ingress", "livekit-jwt-ingress")
    auth_secret = find(documents, "Secret", "matrixrtc-auth-secrets")
    find(documents, "Secret", "livekit-config")

    gateway_container = gateway["spec"]["template"]["spec"]["containers"][0]
    upstream_container = upstream["spec"]["template"]["spec"]["containers"][0]
    userservice_container = userservice["spec"]["template"]["spec"]["containers"][0]
    livekit_container = livekit["spec"]["template"]["spec"]["containers"][0]

    for container in (gateway_container, upstream_container, livekit_container):
        assert "@sha256:" in container["image"]
        assert not container["image"].endswith(":latest")

    assert gateway_container["securityContext"]["runAsNonRoot"] is True
    assert upstream_container["securityContext"]["runAsNonRoot"] is True
    assert (
        upstream_container["image"].split("@", maxsplit=1)[0]
        == "ghcr.io/openresilienceinitiative/matrixrtc-authorization-service"
    )
    upstream_env = {
        entry["name"]: entry
        for entry in upstream_container["env"]
    }
    assert upstream_env["LIVEKIT_LOG_LEVEL"]["value"] == "off"
    gateway_env = {entry["name"]: entry for entry in gateway_container["env"]}
    assert gateway_env["MATRIXRTC_CALL_POLICY_URL"]["value"] == (
        "http://userservice:8080/internal/matrixrtc/call-policy"
    )
    assert gateway_env["MATRIXRTC_CALL_POLICY_TOKEN_FILE"]["value"] == (
        "/run/secrets/call-policy-token"
    )
    gateway_secret_items = gateway["spec"]["template"]["spec"]["volumes"][0][
        "secret"
    ]["items"]
    assert {
        "key": "call-policy-token",
        "path": "call-policy-token",
    } in gateway_secret_items

    userservice_env = {
        entry["name"]: entry for entry in userservice_container["env"]
    }
    assert userservice_env["MATRIXRTC_CALL_POLICY_TOKEN"]["valueFrom"] == {
        "secretKeyRef": {
            "name": "matrixrtc-auth-secrets",
            "key": "call-policy-token",
        }
    }
    call_policy_token = auth_secret["stringData"]["call-policy-token"]
    assert len(call_policy_token) >= 48
    assert call_policy_token not in {"", "changeme"}

    # The ingress controller lives in its own namespace. A bare podSelector
    # would silently deny it and every /livekit/jwt request would 502, so the
    # namespace must be selected explicitly and ANDed with the pod labels in
    # one rule (never a namespace-wide allow).
    gateway_policy = next(
        doc
        for doc in documents
        if doc.get("kind") == "NetworkPolicy"
        and doc["metadata"]["name"] == "matrixrtc-auth-policy-gateway"
    )
    controller_rule = next(
        entry
        for entry in gateway_policy["spec"]["ingress"][0]["from"]
        if "namespaceSelector" in entry
    )
    assert controller_rule["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "ingress-nginx"
    }
    assert controller_rule["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "ingress-nginx",
        "app.kubernetes.io/component": "controller",
    }
    assert {
        "to": [{"podSelector": {"matchLabels": {"app": "userservice"}}}],
        "ports": [{"protocol": "TCP", "port": 8082}],
    } in gateway_policy["spec"]["egress"]

    # lk-jwt-service validates the OpenID token against the homeserver over
    # HTTPS. Without egress 443 every request is a 401 that the gateway proxies
    # through silently, which is indistinguishable from a rejected caller.
    upstream_policy = next(
        doc
        for doc in documents
        if doc.get("kind") == "NetworkPolicy"
        and doc["metadata"]["name"] == "matrixrtc-authorization-service"
    )
    upstream_egress_ports = {
        port.get("port")
        for rule in upstream_policy["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    assert 443 in upstream_egress_ports
    assert livekit["spec"]["replicas"] == 1
    assert livekit["spec"]["strategy"] == {"type": "Recreate"}
    assert livekit["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 60
    assert livekit["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] == (
        "livekit-config"
    )

    ingress_backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert ingress_backend["service"]["name"] == "matrixrtc-auth-policy-gateway"
    assert ingress["metadata"]["annotations"][
        "nginx.ingress.kubernetes.io/limit-rps"
    ] == "10"
    assert ingress["metadata"]["annotations"][
        "nginx.ingress.kubernetes.io/cors-allow-origin"
    ] == "https://your-domain.example.com"

    rendered = yaml.safe_dump_all(documents)
    assert "LIVEKIT_FULL_ACCESS_HOMESERVERS" in rendered
    assert "MATRIX_MEMBERSHIP_TOKEN_FILE" in rendered
    assert "MATRIX_ADMIN_TOKEN_FILE" not in rendered
    assert "matrix-admin-token" not in rendered
    assert "LIVEKIT_API_SECRET" not in rendered
    assert "kind: NetworkPolicy" in rendered
    assert "kind: PodDisruptionBudget" in rendered

    print("PASS: MatrixRTC auth renders one public policy gateway and external secret references")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
