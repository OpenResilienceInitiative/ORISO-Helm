#!/usr/bin/env python3
"""Render guard for value-driven image pull policy."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}


def render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "image-pull-policy",
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
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def assert_pull_policy(documents: list[dict], expected: str) -> None:
    failures = []
    for document in documents:
        kind = document.get("kind")
        if kind not in WORKLOAD_KINDS:
            continue

        name = document.get("metadata", {}).get("name")
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        for field in ("initContainers", "containers"):
            for container in pod_spec.get(field, []) or []:
                pull_policy = container.get("imagePullPolicy")
                if pull_policy != expected:
                    failures.append(
                        f"{kind}/{name} {field}/{container.get('name')} has {pull_policy!r}"
                    )

    assert not failures, "\n".join(failures)


def main() -> None:
    with open(os.path.join(CHART_DIR, "values.yaml.default"), encoding="utf-8") as values_file:
        default_values = yaml.safe_load(values_file)

    assert default_values["matrix"]["image"] == "matrixdotorg/synapse:v1.158.0"
    assert default_values["matrix"]["initImage"] == "busybox:1.38.0"
    assert default_values["livekit"]["image"] == (
        "docker.io/livekit/livekit-server@sha256:"
        "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
    )

    assert_pull_policy(render(), "Always")
    assert_pull_policy(
        render("--set-string", "global.imagePullPolicy=IfNotPresent"),
        "IfNotPresent",
    )
    print("PASS: all rendered workload containers use configured imagePullPolicy")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
