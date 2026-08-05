#!/usr/bin/env python3
"""Render guard that every workload container always pulls images."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}


def render() -> list[dict]:
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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def main() -> None:
    failures = []
    for document in render():
        kind = document.get("kind")
        if kind not in WORKLOAD_KINDS:
            continue

        name = document.get("metadata", {}).get("name")
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        for field in ("initContainers", "containers"):
            for container in pod_spec.get(field, []) or []:
                pull_policy = container.get("imagePullPolicy")
                if pull_policy != "Always":
                    failures.append(
                        f"{kind}/{name} {field}/{container.get('name')} has {pull_policy!r}"
                    )

    assert not failures, "\n".join(failures)
    print("PASS: all rendered workload containers use imagePullPolicy Always")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
