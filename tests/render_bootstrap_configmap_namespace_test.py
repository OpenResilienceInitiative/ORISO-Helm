#!/usr/bin/env python3
"""Guard that bootstrap ConfigMaps render into the release namespace."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTSTRAP_CONFIGMAPS = {
    "tenant-bootstrap-sql",
    "topic-bootstrap-sql",
}


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "bootstrap-configmaps",
            CHART_DIR,
            "--namespace",
            "caritas",
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
    configmaps = {
        document["metadata"]["name"]: document
        for document in render()
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") in BOOTSTRAP_CONFIGMAPS
    }
    assert set(configmaps) == BOOTSTRAP_CONFIGMAPS

    for name, configmap in configmaps.items():
        namespace = configmap["metadata"].get("namespace")
        assert namespace == "caritas", f"{name} should render in release namespace"

    print("PASS: bootstrap ConfigMaps render in the release namespace")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
