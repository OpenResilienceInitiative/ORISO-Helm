#!/usr/bin/env python3
"""Render guard for LiveKit hostNetwork rollout safety."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(*extra_args: str) -> list[dict]:
    result = run_helm(*extra_args)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def run_helm(*extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "helm",
            "template",
            "livekit-rollout",
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


def find_livekit(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "livekit"
    )


def main() -> None:
    livekit = find_livekit(render())

    assert livekit["spec"]["replicas"] == 1
    assert livekit["spec"]["strategy"] == {"type": "Recreate"}
    assert (
        livekit["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]
        == 60
    )

    rolling = find_livekit(
        render(
            "--set",
            "livekit.replicas=2",
            "--set-string",
            "livekit.deploymentStrategy=RollingUpdate",
        )
    )
    assert rolling["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }

    unsafe = run_helm(
        "--set-string", "livekit.deploymentStrategy=RollingUpdate"
    )
    assert unsafe.returncode != 0
    assert "requires livekit.replicas >= 2" in unsafe.stderr

    unbounded = run_helm(
        "--set", "livekit.terminationGracePeriodSeconds=18000"
    )
    assert unbounded.returncode != 0
    assert "must be between 1 and 300 seconds" in unbounded.stderr

    invalid_strategy = run_helm(
        "--set-string", "livekit.deploymentStrategy=Replace"
    )
    assert invalid_strategy.returncode != 0
    assert "must be Recreate or RollingUpdate" in invalid_strategy.stderr

    zero_grace = run_helm(
        "--set", "livekit.terminationGracePeriodSeconds=0"
    )
    assert zero_grace.returncode != 0
    assert "must be between 1 and 300 seconds" in zero_grace.stderr

    decimal_replicas = run_helm("--set-string", "livekit.replicas=2.5")
    assert decimal_replicas.returncode != 0
    assert "livekit.replicas must be an integer" in decimal_replicas.stderr

    decimal_grace = run_helm(
        "--set-string", "livekit.terminationGracePeriodSeconds=60.5"
    )
    assert decimal_grace.returncode != 0
    assert (
        "livekit.terminationGracePeriodSeconds must be an integer"
        in decimal_grace.stderr
    )

    print("PASS: single-node LiveKit rollout is serialized and bounded")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
