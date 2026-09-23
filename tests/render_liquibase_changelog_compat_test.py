#!/usr/bin/env python3
"""Render guard for legacy Liquibase changelog overrides."""

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
            "liquibase-compat",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string",
            "agencyService.liquibaseChangeLog=classpath:db/changelog/db.changelog-master.xml",
            "--set-string",
            "consultingTypeService.liquibaseChangeLog=classpath:db/changelog/db.changelog-master.xml",
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


def configmap(documents: list[dict], name: str) -> dict:
    for document in documents:
        if document.get("kind") == "ConfigMap" and document.get("metadata", {}).get("name") == name:
            return document
    raise AssertionError(f"ConfigMap/{name} was not rendered")


def main() -> None:
    documents = render()

    agency = configmap(documents, "agencyservice-configmap-env")["data"]
    assert agency["SPRING_LIQUIBASE_CHANGE_LOG"] == "classpath:db/changelog/agencyservice-master.xml"
    assert agency["SPRING_LIQUIBASE_CHANGELOG"] == "classpath:db/changelog/agencyservice-master.xml"

    consulting_type = configmap(documents, "consultingtypeservice-configmap-env")["data"]
    assert (
        consulting_type["SPRING_LIQUIBASE_CHANGE_LOG"]
        == "classpath:db/changelog/consultingtypeservice-master.xml"
    )
    assert (
        consulting_type["SPRING_LIQUIBASE_CHANGELOG"]
        == "classpath:db/changelog/consultingtypeservice-master.xml"
    )

    print("PASS: legacy Liquibase changelog overrides render service-specific changelogs")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
