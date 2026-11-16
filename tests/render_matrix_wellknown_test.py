#!/usr/bin/env python3
"""Render guard: the homeserver must publish its own delegation.

Without `/.well-known/matrix/server`, federation discovery falls back to port
8448. Nothing listens there, so lk-jwt-service's OpenID validation over
`matrix://` fails with "connection refused" and every LiveKit token request
answers 401 — with no component logging a denial, because none of them denied
anything (ORISO-ElementCall#35, ORISO-Livekit#20).
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN = "predev.oriso.internal"


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "matrix-wellknown", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            "--set", f"global.domainName={DOMAIN}",
            "--set-string", "userService.smtpUser=smtp-canary-user",
            "--set-string", "userService.smtpPassword=smtp-canary-password",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit("helm template failed")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def main() -> None:
    documents = render()
    ingress = next(
        doc
        for doc in documents
        if doc.get("kind") == "Ingress"
        and doc["metadata"]["name"] == "matrix-client-ingress"
    )
    # Helm#536: no snippet answers it any more; the controller disables them.
    assert not any("snippet" in key for key in ingress["metadata"]["annotations"])

    # The exact path reaches Synapse's client listener ...
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    (route,) = [path for path in paths if path["path"] == "/.well-known/matrix/server"]
    assert route["pathType"] == "Exact"
    assert route["backend"]["service"] == {"name": "matrix-synapse", "port": {"number": 8008}}

    # ... where Synapse serves {"m.server": "<server_name>:443"} itself.
    homeserver = next(
        yaml.safe_load(doc["data"]["homeserver.yaml"])
        for doc in documents
        if doc.get("kind") == "ConfigMap" and "homeserver.yaml" in (doc.get("data") or {})
    )
    assert homeserver["serve_server_wellknown"] is True
    client_listener = next(listener for listener in homeserver["listeners"] if listener["port"] == 8008)
    assert any("client" in resource["names"] for resource in client_listener["resources"])
    # Synapse only advertises :443 when server_name carries no port of its own;
    # with one, it would advertise that port (e.g. the unreachable 8448).
    assert ":" not in str(homeserver["server_name"]), homeserver["server_name"]
    print("PASS: matrix server delegation is published on :443")


if __name__ == "__main__":
    main()
