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


def extract_location_block(snippet: str, path: str) -> str:
    """Return the body of the active `location = <path>` block, or "".

    Lines commented out with `#` are ignored, so a disabled handler cannot
    satisfy the assertions below.
    """
    lines = [line for line in snippet.splitlines() if not line.strip().startswith("#")]
    opener = f"location = {path}"
    for index, line in enumerate(lines):
        if opener not in line:
            continue
        depth = 0
        body: list[str] = []
        for current in lines[index:]:
            depth += current.count("{") - current.count("}")
            body.append(current)
            if depth == 0 and body:
                return "\n".join(body)
    return ""


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

    # Assert on the active handler itself, not on substrings of the whole
    # snippet: a commented-out or differently-scoped block must not satisfy
    # this guard.
    block = extract_location_block(snippet, "/.well-known/matrix/server")
    assert block, "no active `location = /.well-known/matrix/server` block"
    assert "return 200" in block
    assert "default_type application/json" in block
    assert "Access-Control-Allow-Origin *" in block
    # The delegation must name port 443, never the unreachable federation
    # default 8448 that caused the outage.
    assert f'"m.server": "{DOMAIN}:443"' in block
    assert "8448" not in block
    print("PASS: matrix server delegation is published on :443")


if __name__ == "__main__":
    main()
