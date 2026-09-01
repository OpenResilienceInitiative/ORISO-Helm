#!/usr/bin/env python3
"""Prove the pre-cutover chart can describe one immutable rollback baseline."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import yaml

CHART_DIR = pathlib.Path(__file__).resolve().parents[1]
DIGEST = "1" * 64


def image(repository: str) -> str:
    return f"{repository}@sha256:{DIGEST}"


EXPECTED_IMAGES = {
    "frontend": image("ghcr.io/openresilienceinitiative/oriso-frontend"),
    "elementCall": image("ghcr.io/openresilienceinitiative/element-call"),
    "userService": image("ghcr.io/openresilienceinitiative/oriso-userservice"),
    "agencyService": image("ghcr.io/openresilienceinitiative/oriso-agencyservice"),
    "matrixrtcPolicyGateway": image(
        "ghcr.io/openresilienceinitiative/matrixrtc-auth-policy-gateway"
    ),
    "matrixrtcAuthorizationService": image(
        "ghcr.io/openresilienceinitiative/matrixrtc-authorization-service"
    ),
    "livekit": image("docker.io/livekit/livekit-server"),
    "synapse": image("matrixdotorg/synapse"),
    "synapseInit": image("busybox"),
    "healthcheck": image("docker.io/curlimages/curl"),
    "redisCheck": image("docker.io/library/redis"),
}


def render() -> list[dict]:
    values = {
        "frontend.image": EXPECTED_IMAGES["frontend"],
        "elementCall.image": EXPECTED_IMAGES["elementCall"],
        "elementCall.healthcheckImage": EXPECTED_IMAGES["healthcheck"],
        "userService.image": EXPECTED_IMAGES["userService"],
        "agencyService.image": EXPECTED_IMAGES["agencyService"],
        "matrixrtcAuth.gateway.image": EXPECTED_IMAGES["matrixrtcPolicyGateway"],
        "matrixrtcAuth.upstream.image": EXPECTED_IMAGES[
            "matrixrtcAuthorizationService"
        ],
        "matrixrtcAuth.redisCheckImage": EXPECTED_IMAGES["redisCheck"],
        "livekit.image": EXPECTED_IMAGES["livekit"],
        "matrix.image": EXPECTED_IMAGES["synapse"],
        "matrix.initImage": EXPECTED_IMAGES["synapseInit"],
    }
    command = [
        "helm",
        "template",
        "caritas",
        str(CHART_DIR),
        "--namespace",
        "caritas",
        "-f",
        str(CHART_DIR / "values.yaml.default"),
        "-f",
        str(CHART_DIR / "secrets.yaml.default"),
        "--set-string",
        "global.secrets.redisdefaultPass=test-redis-password",
        "--set-string",
        "userService.smtpUser=smtp-canary-user",
        "--set-string",
        "userService.smtpPassword=smtp-canary-password",
    ]
    for name, value in values.items():
        command.extend(["--set-string", f"{name}={value}"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def deployment(documents: list[dict], name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == name
    )


def test_complete_matrixrtc_baseline_accepts_only_exact_images() -> None:
    documents = render()
    workloads = {
        "frontend": deployment(documents, "frontend"),
        "elementCall": deployment(documents, "element-call"),
        "userService": deployment(documents, "userservice"),
        "agencyService": deployment(documents, "agencyservice"),
        "matrixrtcPolicyGateway": deployment(
            documents, "matrixrtc-auth-policy-gateway"
        ),
        "matrixrtcAuthorizationService": deployment(
            documents, "matrixrtc-authorization-service"
        ),
        "livekit": deployment(documents, "livekit"),
        "synapse": deployment(documents, "matrix-synapse"),
    }
    actual = {
        name: workload["spec"]["template"]["spec"]["containers"][0]["image"]
        for name, workload in workloads.items()
    }
    actual["healthcheck"] = workloads["elementCall"]["spec"]["template"]["spec"][
        "initContainers"
    ][0]["image"]
    assert (
        workloads["matrixrtcPolicyGateway"]["spec"]["template"]["spec"][
            "initContainers"
        ][0]["image"]
        == actual["healthcheck"]
    )
    actual["redisCheck"] = workloads["matrixrtcAuthorizationService"]["spec"][
        "template"
    ]["spec"]["initContainers"][0]["image"]
    actual["synapseInit"] = workloads["synapse"]["spec"]["template"]["spec"][
        "initContainers"
    ][0]["image"]

    assert actual == EXPECTED_IMAGES
    for rendered_image in actual.values():
        assert re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", rendered_image)


if __name__ == "__main__":
    try:
        test_complete_matrixrtc_baseline_accepts_only_exact_images()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
    print("PASS: pre-cutover MatrixRTC baseline renders only immutable images")
