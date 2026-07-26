#!/usr/bin/env python3
"""Render guard for immutable Matrix/Element Call images and secret-safe backups."""

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
            "matrix-call-security",
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
    synapse = find(documents, "Deployment", "matrix-synapse")
    element_call = find(documents, "Deployment", "element-call")

    synapse_spec = synapse["spec"]["template"]["spec"]
    images = [
        synapse_spec["initContainers"][0]["image"],
        synapse_spec["containers"][0]["image"],
        element_call["spec"]["template"]["spec"]["containers"][0]["image"],
    ]
    for image in images:
        assert "@sha256:" in image
        assert not image.endswith(":latest")

    rendered = yaml.safe_dump_all(documents)
    assert "matrix-backup-cronjob-github" not in rendered
    assert "synapse_secure_password_2025" not in rendered
    assert "YOUR_GITHUB_TOKEN" not in rendered
    assert "caritas-matrix-backups" not in rendered

    print("PASS: Matrix and Element Call images are immutable; no unsafe backup job renders")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
