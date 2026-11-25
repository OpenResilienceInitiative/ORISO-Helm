#!/usr/bin/env python3
"""Check that the Keycloak server starts with the upstream Base64 mitigation."""

from pathlib import Path
import subprocess

import yaml


CHART = Path(__file__).resolve().parent.parent


def main():
    rendered = subprocess.check_output(
        [
            "helm", "template", "keycloak-jvm-mitigation", str(CHART),
            "-f", str(CHART / "values.yaml.default"),
            "-f", str(CHART / "tests/fixtures/values-render-domain.yaml"),
            "-f", str(CHART / "tests/fixtures/values-public-entry-render.yaml"),
        ],
        text=True,
    )
    deployments = (
        document for document in yaml.safe_load_all(rendered)
        if document and document.get("kind") == "Deployment"
    )
    keycloak = next(
        deployment for deployment in deployments
        if deployment["metadata"]["name"] == "keycloak"
    )
    containers = keycloak["spec"]["template"]["spec"]["containers"]
    server = next(container for container in containers if container["name"] == "keycloak")
    extra_options = [
        entry["value"] for entry in server["env"]
        if entry["name"] == "JAVA_OPTS_APPEND"
    ]
    assert len(extra_options) == 1, "Keycloak must declare JAVA_OPTS_APPEND once"
    assert "-XX:-UseBASE64Intrinsics" in extra_options[0].split()
    assert all(entry["name"] != "JAVA_OPTS" for entry in server["env"])
    print("PASS: Keycloak appends the Base64 mitigation to its default JVM options")


if __name__ == "__main__":
    main()
