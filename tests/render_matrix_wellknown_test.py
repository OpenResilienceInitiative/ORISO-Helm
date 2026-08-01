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
DOMAIN = "predev.example.org"


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "matrix-wellknown", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
            "--set", f"global.domainName={DOMAIN}",
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
    snippet = ingress["metadata"]["annotations"].get(
        "nginx.ingress.kubernetes.io/server-snippet", ""
    )
    assert "/.well-known/matrix/server" in snippet
    # The delegation must name port 443, never the unreachable federation
    # default 8448 that caused the outage.
    assert f'"m.server": "{DOMAIN}:443"' in snippet
    assert "8448" not in snippet
    print("PASS: matrix server delegation is published on :443")


if __name__ == "__main__":
    main()
