#!/usr/bin/env python3
"""Render-test the public frontend error-report route to UserService."""

from __future__ import annotations

import os
import re
import subprocess
import sys

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERROR_REPORT_PATH = "/service/error-reports"


def render() -> str:
    result = subprocess.run(
        [
            "helm",
            "template",
            "error-reports-test",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"helm template failed:\n{result.stderr}")
    return result.stdout


def ingress(manifest: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^kind: Ingress\nmetadata:\n.*?  name: {re.escape(name)}\n.*?(?=^---$|\Z)",
        manifest,
    )
    assert match is not None, f"Ingress/{name} was not rendered"
    return match.group(0)


def main() -> None:
    manifest = render()
    main = ingress(manifest, "main-ingress")
    assert re.search(
        rf"(?ms)name: userservice\n\s+port:\n\s+number: 8080\n"
        rf"\s+path: {re.escape(ERROR_REPORT_PATH)}\n\s+pathType: Prefix",
        main,
    ), (
        f"{ERROR_REPORT_PATH} must be a Prefix path targeting userservice:8080 "
        "in main-ingress"
    )

    rewrite = ingress(manifest, "userservice-rewrite-ingress")
    assert "nginx.ingress.kubernetes.io/rewrite-target: /$1$2$3" in rewrite
    assert (
        "/service/(users|conversations|liveproxy|useradmin|appointments|matrix|"
        "error-reports)(/|$)(.*)"
    ) in rewrite
    assert re.search(
        r"(?ms)name: userservice\n\s+port:\n\s+number: 8080", rewrite
    ), "userservice-rewrite-ingress must target userservice:8080"

    print("PASS: /service/error-reports routes and rewrites to UserService")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
