#!/usr/bin/env python3
"""Chart render contract for separate required product and legal mail names."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(overlay: str | None = None, *set_values: str) -> subprocess.CompletedProcess[str]:
    args = [
        "helm", "template", "email-branding-test", CHART_DIR,
        "-f", os.path.join(CHART_DIR, "values.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/values-render-domain.yaml"),
        "-f", os.path.join(CHART_DIR, "secrets.yaml.default"),
        "-f", os.path.join(CHART_DIR, "tests/fixtures/render-required-secrets.yaml"),
        "--set-string", "userService.smtpUser=render-only-smtp-user",
        "--set-string", "userService.smtpPassword=render-only-smtp-password",
    ]
    if overlay:
        args.extend(["-f", os.path.join(CHART_DIR, f"values-{overlay}.yaml")])
    for value in set_values:
        args.extend(["--set-string", value])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def resources(result: subprocess.CompletedProcess[str]) -> tuple[dict, dict, dict, dict]:
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]

    def named(kind: str, name: str) -> dict:
        return next(doc for doc in docs if doc.get("kind") == kind
                    and doc.get("metadata", {}).get("name") == name)

    return (named("ConfigMap", "userservice-configmap-env"),
            named("ConfigMap", "keycloak-configmap-env"),
            named("Deployment", "userservice"),
            named("Deployment", "keycloak"))


def test_required_email_branding() -> None:
    base = yaml.safe_load((Path(CHART_DIR) / "values.yaml.default").read_text())
    assert base["global"]["emailBrandingName"] == ""
    assert base["global"]["emailLegalOrganisationName"] == ""
    assert "emailBrandingName" not in base["userService"]

    for field in ("emailBrandingName", "emailLegalOrganisationName"):
        for blank in ("", "   "):
            result = render(None, f"global.{field}={blank}")
            assert result.returncode != 0, (field, blank)
            assert f"global.{field}" in result.stderr, result.stderr

    legacy = render(None, "userService.emailBrandingName=Old Product")
    assert legacy.returncode != 0
    assert "userService.emailBrandingName is obsolete" in legacy.stderr

    current = render(None, "global.emailBrandingName=Care Portal",
                     "global.emailLegalOrganisationName=Care Foundation")
    user_cm, keycloak_cm, user_deploy, keycloak_deploy = resources(current)
    assert user_cm["data"]["EMAIL_BRANDING_NAME"] == "Care Portal"
    assert keycloak_cm["data"]["EMAIL_BRANDING_NAME"] == "Care Portal"
    assert keycloak_cm["data"]["EMAIL_LEGAL_ORGANISATION_NAME"] == "Care Foundation"
    for deployment, cm_name, names in (
        (user_deploy, "userservice-configmap-env", ("EMAIL_BRANDING_NAME",)),
        (keycloak_deploy, "keycloak-configmap-env",
         ("EMAIL_BRANDING_NAME", "EMAIL_LEGAL_ORGANISATION_NAME")),
    ):
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        for name in names:
            assert {"name": name, "valueFrom": {"configMapKeyRef": {
                "name": cm_name, "key": name}}} in env

    checksum_key = "checksum/email-identity"
    original_checksums = [deploy["spec"]["template"]["metadata"]["annotations"][checksum_key]
                          for deploy in (user_deploy, keycloak_deploy)]
    assert original_checksums[0] == original_checksums[1]
    for value in ("global.emailBrandingName=Other Portal",
                  "global.emailLegalOrganisationName=Other Foundation"):
        changed = render(None, value)
        _, _, changed_user, changed_keycloak = resources(changed)
        for original, deployment in zip(original_checksums, (changed_user, changed_keycloak)):
            assert deployment["spec"]["template"]["metadata"]["annotations"][checksum_key] != original

    # Overlays retain the existing product, but a real legal name remains an
    # operator-supplied deployment prerequisite (the fixture is synthetic).
    for overlay in ("dev", "pre-dev"):
        user, keycloak, _, _ = resources(render(overlay))
        assert user["data"]["EMAIL_BRANDING_NAME"] == "ORISO"
        assert keycloak["data"]["EMAIL_LEGAL_ORGANISATION_NAME"] == "Render Test Foundation"


if __name__ == "__main__":
    try:
        test_required_email_branding()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
