#!/usr/bin/env python3
"""Verify that availability credentials are referenced, never rendered as plaintext."""
import subprocess

import yaml

from render_public_entry_rate_limit_test import CHART_DIR, VALUES


def environment(*overrides):
    rendered = subprocess.run(
        ["helm", "template", "identity-lookup", CHART_DIR, *VALUES, *overrides],
        capture_output=True, text=True, check=True,
    )
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]
    deployment = next(doc for doc in documents
                      if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "userservice")
    return {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}


def main():
    name = "MATRIX_AVAILABILITY_ADMIN_ACCESS_TOKEN"
    assert name not in environment(), "existing installations must not acquire a new required secret implicitly"
    configured = environment(
        "--set", "userService.identitySuggestions.matrixAdminTokenSecret.name=lookup-credential",
        "--set", "userService.identitySuggestions.matrixAdminTokenSecret.key=lookup-token",
    )[name]
    assert "value" not in configured
    assert configured["valueFrom"]["secretKeyRef"] == {
        "name": "lookup-credential", "key": "lookup-token",
    }
    print("PASS: availability credential uses an explicit existing secret reference")


if __name__ == "__main__":
    main()
