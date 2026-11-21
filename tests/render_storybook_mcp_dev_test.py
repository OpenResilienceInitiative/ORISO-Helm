#!/usr/bin/env python3
"""Render-test the authenticated Dev Storybook MCP routes."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from render_storybook_dev_test import render  # noqa: E402

NAMES = {"storybook-mcp-frontend", "storybook-mcp-admin", "storybook-mcp-dev-ingress"}


def main() -> None:
    """Assert the MCP is off by default and authenticated when enabled for Dev."""
    assert not any(doc.get("metadata", {}).get("name") in NAMES for doc in render()), (
        "Storybook MCP must be disabled by default"
    )

    dev = render("values-dev.yaml")
    resources = {
        (doc.get("kind"), doc["metadata"]["name"]): doc
        for doc in dev
        if doc.get("metadata", {}).get("name") in NAMES
    }
    assert set(resources) == {
        ("Deployment", "storybook-mcp-frontend"),
        ("Deployment", "storybook-mcp-admin"),
        ("Service", "storybook-mcp-frontend"),
        ("Service", "storybook-mcp-admin"),
        ("Ingress", "storybook-mcp-dev-ingress"),
    }, sorted(resources)

    for app, repo in (("frontend", "ORISO-Frontend"), ("admin", "ORISO-Admin")):
        pod = resources[("Deployment", f"storybook-mcp-{app}")]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        script = container["args"][0]
        assert f"-b dev https://github.com/OpenResilienceInitiative/{repo}.git" in script, script
        assert "storybook dev -p 6006 --host 0.0.0.0 --ci" in script, script
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["automountServiceAccountToken"] is False
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["resources"]["limits"]["memory"] == "3Gi"

    annotations = resources[("Ingress", "storybook-mcp-dev-ingress")]["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert annotations["nginx.ingress.kubernetes.io/auth-secret"] == "storybook-mcp-basic-auth"
    assert annotations["nginx.ingress.kubernetes.io/upstream-vhost"] == "localhost"
    assert annotations["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    paths = resources[("Ingress", "storybook-mcp-dev-ingress")]["spec"]["rules"][0]["http"]["paths"]
    assert {p["path"] for p in paths} == {
        "/storybook-frontend-mcp(/|$)(.*)",
        "/storybook-admin-mcp(/|$)(.*)",
    }
    print("PASS: Dev renders authenticated Frontend and Admin Storybook MCP routes")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
