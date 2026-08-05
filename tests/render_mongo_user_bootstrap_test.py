#!/usr/bin/env python3
"""Render guard for idempotent Mongo user bootstrap."""

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
            "mongo-user-bootstrap",
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


def find(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def main() -> None:
    documents = render()
    secret = find(documents, "Secret", "mongodb-secret")
    job = find(documents, "Job", "create-mongo-users")

    assert job["metadata"]["annotations"]["helm.sh/hook"] == "post-install"
    assert job["metadata"]["annotations"]["helm.sh/hook-delete-policy"] == (
        "before-hook-creation,hook-succeeded"
    )
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert job["spec"]["template"]["spec"]["activeDeadlineSeconds"] == 600

    init_container = job["spec"]["template"]["spec"]["initContainers"][0]
    init_script = "\n".join(init_container["command"])
    assert "for attempt in $(seq 1 150)" in init_script
    assert "timed out waiting for mongodb auth to work" in init_script

    secret_keys = set(secret["data"])
    assert "MONGO_CONSULTING_TYPES_USER" in secret_keys
    assert "MONGO_CONSULTING_TYPES_PASSWORD" in secret_keys

    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["MONGO_CONSULTING_TYPES_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "mongodb-secret",
        "key": "MONGO_CONSULTING_TYPES_USER",
    }
    assert env["MONGO_CONSULTING_TYPES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "mongodb-secret",
        "key": "MONGO_CONSULTING_TYPES_PASSWORD",
    }

    script = "\n".join(container["command"])
    assert 'create_or_update_user "consulting_types"' in script
    assert "$MONGO_CONSULTING_TYPES_USER" in script
    assert "$MONGO_CONSULTING_TYPES_PASSWORD" in script

    print("PASS: Mongo user bootstrap includes ConsultingType user on install only")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
