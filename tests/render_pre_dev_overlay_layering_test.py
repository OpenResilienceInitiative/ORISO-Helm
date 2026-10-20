#!/usr/bin/env python3
"""Guard the Pre-Dev deployment shape: values-dev.yaml, then values-pre-dev.yaml.

values-pre-dev.yaml holds only Pre-Dev's own settings and is loaded after
values-dev.yaml, so a setting added to Dev reaches Pre-Dev on the next upgrade.
This test renders that exact stack and checks that Dev's host never leaks in and
that the deliberate Pre-Dev deltas win.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render_stack() -> tuple[str, list[dict]]:
    proc = subprocess.run(
        [
            "helm", "template", "pre-dev-layering", CHART_DIR,
            "-f", os.path.join(CHART_DIR, "values.yaml.default"),
            "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f", os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "-f", os.path.join(CHART_DIR, "values-dev.yaml"),
            "-f", os.path.join(CHART_DIR, "values-pre-dev.yaml"),
            "--set-string", "global.emailLegalOrganisationName=Render Test Foundation",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"helm template failed for the Pre-Dev stack:\n{proc.stderr}"
    return proc.stdout, [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


def configmap(docs: list[dict], name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == name:
            return doc.get("data") or {}
    raise AssertionError(f"ConfigMap {name} was not rendered")


def main() -> None:
    text, docs = render_stack()

    # "predev.oriso.org" contains "dev.oriso.org", so match Dev's host only.
    leaked = re.findall(r"(?<!pre)dev\.oriso\.org|matrix\.oriso\.org", text)
    assert not leaked, f"Dev host leaked into the Pre-Dev render ({len(leaked)} hits)"

    user = configmap(docs, "userservice-configmap-env")
    assert user["EMAIL_ALLOW_UNREVIEWED_LOCALES"] == "false", user["EMAIL_ALLOW_UNREVIEWED_LOCALES"]
    assert user["MAGIC_LINK_FRONTEND_BASE_URL"] == "https://predev.oriso.org"
    assert user["PASSWORD_RESET_ADMIN_FRONTEND_BASE_URL"] == "https://predev.oriso.org/admin"
    assert user["SPRING_PROFILES_ACTIVE"] == "prod", user["SPRING_PROFILES_ACTIVE"]

    assert not any(
        d.get("kind") == "Deployment" and "storybook" in d["metadata"]["name"] for d in docs
    ), "Pre-Dev Storybooks are deployed outside the chart"

    assert re.search(r"name: ORISO_DEPLOYMENT_ENVIRONMENT\n\s+value: pre-dev", text), (
        "SigNoz collector must label Pre-Dev data as pre-dev"
    )
    print("PASS: Dev + Pre-Dev stack renders with Pre-Dev's own host, labels and gates")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
