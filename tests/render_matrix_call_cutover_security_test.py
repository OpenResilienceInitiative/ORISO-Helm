#!/usr/bin/env python3
"""Render guard for immutable Matrix/Element Call images and secret-safe backups."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIGEST = "1" * 64


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
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
            "--set",
            "global.requireImmutableImages=true",
            "--set-string",
            f"frontend.image=ghcr.io/openresilienceinitiative/oriso-frontend@sha256:{TEST_DIGEST}",
            "--set-string",
            f"elementCall.image=ghcr.io/openresilienceinitiative/element-call@sha256:{TEST_DIGEST}",
            "--set-string",
            f"elementCall.healthcheckImage=busybox@sha256:{TEST_DIGEST}",
            "--set-string",
            f"userService.image=ghcr.io/openresilienceinitiative/oriso-userservice@sha256:{TEST_DIGEST}",
            "--set-string",
            f"agencyService.image=ghcr.io/openresilienceinitiative/oriso-agencyservice@sha256:{TEST_DIGEST}",
            "--set-string",
            f"matrix.image=matrixdotorg/synapse@sha256:{TEST_DIGEST}",
            "--set-string",
            f"matrix.initImage=busybox@sha256:{TEST_DIGEST}",
            "--set-string",
            f"livekit.image=docker.io/livekit/livekit-server@sha256:{TEST_DIGEST}",
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
    frontend = find(documents, "Deployment", "frontend")
    userservice = find(documents, "Deployment", "userservice")
    agencyservice = find(documents, "Deployment", "agencyservice")
    livekit = find(documents, "Deployment", "livekit")

    synapse_spec = synapse["spec"]["template"]["spec"]
    images = [
        synapse_spec["initContainers"][0]["image"],
        synapse_spec["containers"][0]["image"],
        element_call["spec"]["template"]["spec"]["containers"][0]["image"],
        frontend["spec"]["template"]["spec"]["containers"][0]["image"],
        userservice["spec"]["template"]["spec"]["containers"][0]["image"],
        agencyservice["spec"]["template"]["spec"]["containers"][0]["image"],
        livekit["spec"]["template"]["spec"]["containers"][0]["image"],
    ]
    for image in images:
        assert re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", image)
        assert not image.endswith(":latest")

    expected_cutover_images = {
        f"ghcr.io/openresilienceinitiative/oriso-frontend@sha256:{TEST_DIGEST}",
        f"ghcr.io/openresilienceinitiative/oriso-userservice@sha256:{TEST_DIGEST}",
        f"ghcr.io/openresilienceinitiative/oriso-agencyservice@sha256:{TEST_DIGEST}",
    }
    assert expected_cutover_images.issubset(set(images))
    assert livekit["spec"]["replicas"] == 1
    assert livekit["spec"]["strategy"] == {"type": "Recreate"}
    assert livekit["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 60

    rendered = yaml.safe_dump_all(documents)
    assert "matrix-backup-cronjob-github" not in rendered
    assert "synapse_secure_password_2025" not in rendered
    assert "YOUR_GITHUB_TOKEN" not in rendered
    assert "caritas-matrix-backups" not in rendered

    print("PASS: all chat cutover images are immutable; no unsafe backup job renders")


def test_matrix_call_cutover_security() -> None:
    main()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
