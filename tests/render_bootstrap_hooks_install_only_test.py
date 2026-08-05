#!/usr/bin/env python3
"""Guard that bootstrap Jobs do not rerun on Helm upgrades."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_ONLY_BOOTSTRAP_JOBS = {
    "create-mongo-users",
    "matrixrtc-bootstrap-token",
    "keycloak-bootstrap-users",
    "tenant-bootstrap",
    "topic-bootstrap",
}


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "bootstrap-hooks",
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
    jobs = {
        document["metadata"]["name"]: document
        for document in render()
        if document.get("kind") == "Job"
        and document.get("metadata", {}).get("name") in INSTALL_ONLY_BOOTSTRAP_JOBS
    }
    assert set(jobs) == INSTALL_ONLY_BOOTSTRAP_JOBS

    for name, job in jobs.items():
        hook = job["metadata"]["annotations"]["helm.sh/hook"]
        assert hook == "post-install", f"{name} hook should be post-install only"
        assert "post-upgrade" not in hook, f"{name} must not rerun on upgrades"

    print("PASS: bootstrap Jobs are install-only")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
