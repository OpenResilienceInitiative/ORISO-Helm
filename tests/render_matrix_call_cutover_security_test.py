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


def render(*, use_canary_images: bool = True) -> list[dict]:
    command = [
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
        "userService.smtpUser=smtp-test-user",
        "--set-string",
        "userService.smtpPassword=smtp-test-password",
    ]
    if use_canary_images:
        command.extend(["--set", "global.requireImmutableImages=true"])
        canary_images = {
            "frontend.image": "ghcr.io/openresilienceinitiative/oriso-frontend",
            "elementCall.image": "ghcr.io/openresilienceinitiative/element-call",
            "elementCall.healthcheckImage": "docker.io/curlimages/curl",
            "matrixrtcAuth.redisCheckImage": "docker.io/library/redis",
            "userService.image": "ghcr.io/openresilienceinitiative/oriso-userservice",
            "agencyService.image": "ghcr.io/openresilienceinitiative/oriso-agencyservice",
            "matrix.image": "matrixdotorg/synapse",
            "matrix.initImage": "busybox",
            "livekit.image": "docker.io/livekit/livekit-server",
        }
        for value_name, repository in canary_images.items():
            command.extend(
                ["--set-string", f"{value_name}={repository}@sha256:{TEST_DIGEST}"]
            )

    result = subprocess.run(
        command,
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


def verify_default_image_contract(documents: list[dict]) -> None:
    with open(os.path.join(CHART_DIR, "values.yaml.default"), encoding="utf-8") as source:
        defaults = yaml.safe_load(source)

    synapse = find(documents, "Deployment", "matrix-synapse")
    rendered_images = {
        "matrix.initImage": synapse["spec"]["template"]["spec"]["initContainers"][0][
            "image"
        ],
        "matrix.image": synapse["spec"]["template"]["spec"]["containers"][0][
            "image"
        ],
        "elementCall.image": find(documents, "Deployment", "element-call")["spec"][
            "template"
        ]["spec"]["containers"][0]["image"],
        "frontend.image": find(documents, "Deployment", "frontend")["spec"][
            "template"
        ]["spec"]["containers"][0]["image"],
        "userService.image": find(documents, "Deployment", "userservice")["spec"][
            "template"
        ]["spec"]["containers"][0]["image"],
        "agencyService.image": find(documents, "Deployment", "agencyservice")[
            "spec"
        ]["template"]["spec"]["containers"][0]["image"],
        "livekit.image": find(documents, "Deployment", "livekit")["spec"]["template"]
        ["spec"]["containers"][0]["image"],
    }
    expected_images = {
        "matrix.initImage": defaults["matrix"]["initImage"],
        "matrix.image": defaults["matrix"]["image"],
        "elementCall.image": defaults["elementCall"]["image"],
        "frontend.image": defaults["frontend"]["image"],
        "userService.image": defaults["userService"]["image"],
        "agencyService.image": defaults["agencyService"]["image"],
        "livekit.image": defaults["livekit"]["image"],
    }
    assert rendered_images == expected_images


def main() -> None:
    documents = render()
    synapse = find(documents, "Deployment", "matrix-synapse")
    element_call = find(documents, "Deployment", "element-call")
    frontend = find(documents, "Deployment", "frontend")
    userservice = find(documents, "Deployment", "userservice")
    agencyservice = find(documents, "Deployment", "agencyservice")
    livekit = find(documents, "Deployment", "livekit")
    gateway = find(documents, "Deployment", "matrixrtc-auth-policy-gateway")
    authorization = find(documents, "Deployment", "matrixrtc-authorization-service")

    synapse_spec = synapse["spec"]["template"]["spec"]
    images = [
        synapse_spec["initContainers"][0]["image"],
        synapse_spec["containers"][0]["image"],
        element_call["spec"]["template"]["spec"]["containers"][0]["image"],
        frontend["spec"]["template"]["spec"]["containers"][0]["image"],
        userservice["spec"]["template"]["spec"]["containers"][0]["image"],
        agencyservice["spec"]["template"]["spec"]["containers"][0]["image"],
        livekit["spec"]["template"]["spec"]["containers"][0]["image"],
        element_call["spec"]["template"]["spec"]["initContainers"][0]["image"],
        gateway["spec"]["template"]["spec"]["initContainers"][0]["image"],
        authorization["spec"]["template"]["spec"]["initContainers"][0]["image"],
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

    # The LiveKit single-node rollout shape (replicas, strategy, grace period)
    # is asserted by tests/render_livekit_rollout_test.py, which also covers the
    # multi-replica guard rails. This test only guards image immutability.

    rendered = yaml.safe_dump_all(documents)
    assert "matrix-backup-cronjob-github" not in rendered
    assert "synapse_secure_password_2025" not in rendered
    assert "YOUR_GITHUB_TOKEN" not in rendered
    assert "caritas-matrix-backups" not in rendered
    for secret_name in ("matrixrtc-auth-runtime", "livekit-config-runtime"):
        assert not any(
            document.get("kind") == "Secret"
            and document.get("metadata", {}).get("name") == secret_name
            for document in documents
        )
        assert secret_name in rendered

    print("PASS: all chat cutover images are immutable; no unsafe backup job renders")


def test_matrix_call_cutover_security() -> None:
    main()


def test_default_image_values_are_rendered_exactly() -> None:
    verify_default_image_contract(render(use_canary_images=False))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
