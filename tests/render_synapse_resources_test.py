#!/usr/bin/env python3
"""Render guard: Synapse CPU comes from values. Dev keeps the 500m/1500m CPU
that ended CFS throttling of Synapse on 2026-11-21 (MatrixRTC token checks
timed out behind it), and a values file without matrix.resources keeps the
previous fixed values."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM_TIMEOUT_SECONDS = 180


def render(*extra: str) -> dict:
    result = subprocess.run(
        [
            "helm",
            "template",
            "synapse-resources",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=HELM_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    for document in yaml.safe_load_all(result.stdout):
        if (
            document
            and document.get("kind") == "Deployment"
            and document["metadata"]["name"] == "matrix-synapse"
        ):
            containers = document["spec"]["template"]["spec"]["containers"]
            return next(c for c in containers if c["name"] == "synapse")["resources"]
    raise AssertionError("matrix-synapse Deployment not rendered")


def main() -> int:
    default = render()
    assert default == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "500m", "memory": "1Gi"},
    }, default

    dev = render("-f", os.path.join(CHART_DIR, "values-dev.yaml"))
    assert dev["requests"]["cpu"] == "500m", dev
    assert dev["limits"]["cpu"] == "1500m", dev
    assert dev["limits"]["memory"] == "1Gi", dev

    fallback = render("--set", "matrix.resources=null")
    assert fallback == default, fallback

    print("synapse resources render: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
