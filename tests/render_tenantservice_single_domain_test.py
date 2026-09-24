#!/usr/bin/env python3
"""Regression guard for TenantService access on a shared application host."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = (
    "templates/_helpers.tpl",
    "templates/tenantservice/tenantservice-configmap-env.yaml",
    "templates/tenantservice/tenantservice-deployment.yaml",
)
FEATURE_FLAG = "FEATURE_MULTITENANCY_WITH_SINGLE_DOMAIN_ENABLED"


def build_minimal_chart(destination: str) -> None:
    with open(os.path.join(destination, "Chart.yaml"), "w", encoding="utf-8") as chart:
        chart.write("apiVersion: v2\nname: tenant-single-domain-test\nversion: 0.0.0\n")
    shutil.copyfile(
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(destination, "values.yaml"),
    )
    for relative_path in TEMPLATES:
        output = os.path.join(destination, relative_path)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        shutil.copyfile(os.path.join(CHART_DIR, relative_path), output)


def render(chart: str, enabled: bool) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "tenant-single-domain-test",
            chart,
            "--set-string",
            f"global.multitenancyWithSingleDomainEnabled={str(enabled).lower()}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def named(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def verify(documents: list[dict], expected: str) -> None:
    config_map = named(documents, "ConfigMap", "tenantservice-configmap-env")
    deployment = named(documents, "Deployment", "tenantservice")
    assert config_map["data"][FEATURE_FLAG] == expected

    environment = {
        entry["name"]: entry
        for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment[FEATURE_FLAG]["valueFrom"]["configMapKeyRef"] == {
        "name": "tenantservice-configmap-env",
        "key": FEATURE_FLAG,
    }


def main() -> None:
    temporary_directory = tempfile.mkdtemp(prefix="tenant-single-domain-test-")
    try:
        chart = os.path.join(temporary_directory, "chart")
        os.makedirs(chart)
        build_minimal_chart(chart)
        verify(render(chart, True), "true")
        verify(render(chart, False), "false")
        print("PASS: TenantService consumes the chart single-domain feature flag")
    finally:
        shutil.rmtree(temporary_directory)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
