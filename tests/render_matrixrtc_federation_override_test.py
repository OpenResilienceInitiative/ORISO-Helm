#!/usr/bin/env python3
"""Render guard for MatrixRTC OpenID validation through the environment ingress."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "matrixrtc-federation",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string",
            "global.domainName=dev.oriso.org",
            "--set-string",
            "matrix.matrixServerName=matrix.oriso.org",
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
    assert result.returncode == 0, result.stderr

    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    deployment = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name")
        == "matrixrtc-authorization-service"
    )
    environment = {
        entry["name"]: entry.get("value")
        for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert environment["LIVEKIT_FEDERATION_URL_OVERRIDES"] == (
        "matrix.oriso.org=https://dev.oriso.org"
    )

    print("PASS: MatrixRTC OpenID validation uses the environment federation ingress")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
