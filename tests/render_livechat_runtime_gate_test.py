#!/usr/bin/env python3
"""Render guard for Redis-backed availability and Matrix room encryption."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = [
    "templates/userservice/userservice-configmap-env.yaml",
    "templates/userservice/userservice-deployment.yaml",
]
GATE_VALUES = os.path.join(
    CHART_DIR, "tests", "fixtures", "values-pre-dev-livechat-gate.yaml"
)


def build_minimal_chart(dst: str) -> None:
    os.makedirs(os.path.join(dst, "templates"))
    with open(os.path.join(dst, "Chart.yaml"), "w", encoding="utf-8") as chart:
        chart.write("apiVersion: v2\nname: livechat-runtime-gate\nversion: 0.0.0\n")
    shutil.copyfile(
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(dst, "values.yaml"),
    )
    for relative_path in TEMPLATES:
        destination = os.path.join(dst, relative_path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(os.path.join(CHART_DIR, relative_path), destination)


def render(chart: str, *values: str) -> list[dict]:
    command = ["helm", "template", "runtime-gate", chart]
    for values_file in values:
        command.extend(["-f", values_file])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def by_kind_and_name(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def env_by_name(deployment: dict) -> dict[str, dict]:
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return {entry["name"]: entry for entry in env}


def main() -> None:
    temporary_directory = tempfile.mkdtemp(prefix="livechat-runtime-gate-")
    try:
        chart = os.path.join(temporary_directory, "chart")
        build_minimal_chart(chart)

        gate_documents = render(chart, GATE_VALUES)
        config_map = by_kind_and_name(
            gate_documents, "ConfigMap", "userservice-configmap-env"
        )
        deployment = by_kind_and_name(gate_documents, "Deployment", "userservice")
        data = config_map["data"]
        env = env_by_name(deployment)

        assert data["MATRIX_ENCRYPTION_ENABLED"] == "true"
        assert data["SPRING_DATA_REDIS_HOST"] == "redis"
        assert data["SPRING_DATA_REDIS_PORT"] == "6379"
        assert data["CONSULTANT_AVAILABILITY_REDIS_TTL_SECONDS"] == "120"

        for name in (
            "MATRIX_ENCRYPTION_ENABLED",
            "SPRING_DATA_REDIS_HOST",
            "SPRING_DATA_REDIS_PORT",
            "CONSULTANT_AVAILABILITY_REDIS_TTL_SECONDS",
        ):
            assert env[name]["valueFrom"]["configMapKeyRef"] == {
                "name": "userservice-configmap-env",
                "key": name,
            }

        assert env["SPRING_DATA_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
            "name": "redis-secret",
            "key": "REDIS_DEFAULT_PASS",
        }
        assert "redisdefaultPass" not in yaml.safe_dump(deployment)

        baseline_documents = render(chart)
        baseline = by_kind_and_name(
            baseline_documents, "ConfigMap", "userservice-configmap-env"
        )
        assert baseline["data"]["MATRIX_ENCRYPTION_ENABLED"] == "true"

        print("PASS: Pre-Dev Live Chat runtime gate renders Redis and E2EE safely")
    finally:
        shutil.rmtree(temporary_directory)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
