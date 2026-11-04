#!/usr/bin/env python3
"""Render-test the Matrix ingress upload limit."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BODY_SIZE = "nginx.ingress.kubernetes.io/proxy-body-size"


def render(*extra: str) -> list[dict]:
    proc = subprocess.run(
        [
            "helm",
            "template",
            "upload-limit-test",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(proc.stdout) if document]


def matrix_ingress(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "Ingress"
        and document["metadata"]["name"] == "matrix-client-ingress"
    )


def upload_limit(*extra: str) -> str:
    return matrix_ingress(render(*extra))["metadata"]["annotations"][BODY_SIZE]


def main() -> int:
    ingress = matrix_ingress(render())
    assert ingress["metadata"]["annotations"][BODY_SIZE] == "10m"

    paths = {
        path["path"]: path["pathType"]
        for rule in ingress["spec"]["rules"]
        for path in rule["http"]["paths"]
    }
    assert paths.get("/_matrix") == "Prefix"

    assert upload_limit("--set", "matrix.maxUploadSize=25m") == "25m"
    assert upload_limit("--set", "matrix.maxUploadSize=null") == "10m"

    print("matrix upload limit render contract: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
