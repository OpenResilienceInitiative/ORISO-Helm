#!/usr/bin/env python3
"""Render-test the upload limit on the path a chat attachment takes.

The composer promises 10 MB (ORISO-Frontend ATTACHMENT_MAX_SIZE_IN_MB, which
compares against 10,000,000 bytes). The upload goes to
POST /_matrix/media/v3/upload through matrix-client-ingress. Without an explicit
annotation ingress-nginx allows 1m, and every larger file ends in HTTP 413.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BODY_SIZE = "nginx.ingress.kubernetes.io/proxy-body-size"
UPLOAD_PATH = "/_matrix/media/v3/upload"
MATRIX_PATH = "/_matrix"
PROMISED_BYTES = 10_000_000
SYNAPSE_DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


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
    return [doc for doc in yaml.safe_load_all(proc.stdout) if doc]


def matrix_ingress(docs: list[dict]) -> dict:
    return next(
        doc
        for doc in docs
        if doc.get("kind") == "Ingress"
        and doc["metadata"]["name"] == "matrix-client-ingress"
    )


def nginx_size_in_bytes(value: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    unit = value[-1].lower()
    return int(value[:-1]) * units[unit] if unit in units else int(value)


def assert_limit_covers_the_promise(docs: list[dict], source: str) -> None:
    ingress = matrix_ingress(docs)
    value = ingress["metadata"]["annotations"].get(BODY_SIZE)
    assert value == "10m", f"{source}: {BODY_SIZE} is {value!r}, expected '10m'"
    size = nginx_size_in_bytes(value)
    assert PROMISED_BYTES <= size <= SYNAPSE_DEFAULT_MAX_UPLOAD_BYTES, (
        f"{source}: {size} bytes must lie between the promised {PROMISED_BYTES} "
        f"and Synapse's {SYNAPSE_DEFAULT_MAX_UPLOAD_BYTES}"
    )
    paths = {
        path["path"]: path["pathType"]
        for rule in ingress["spec"]["rules"]
        for path in rule["http"]["paths"]
    }
    # Exact would send the upload to another route, and its limit.
    assert paths.get(MATRIX_PATH) == "Prefix", (
        f"{source}: {MATRIX_PATH} must stay a Prefix path on the ingress: {paths}"
    )


def takes_the_upload_from_matrix(path: dict) -> bool:
    """Whether nginx would pick this path over /_matrix for an upload."""
    value = path.get("path") or ""
    if path.get("pathType") == "Exact":
        return value == UPLOAD_PATH
    prefix = value.rstrip("/")
    matches = UPLOAD_PATH == prefix or UPLOAD_PATH.startswith(f"{prefix}/")
    return matches and len(prefix) > len(MATRIX_PATH)


def assert_nothing_narrower_shadows_the_upload(docs: list[dict]) -> None:
    """A more specific /_matrix path would win in nginx and carry its own limit."""
    matrix_host = next(
        rule.get("host")
        for rule in matrix_ingress(docs)["spec"]["rules"]
        if any(path.get("path") == MATRIX_PATH for path in rule["http"]["paths"])
    )
    for doc in docs:
        if doc.get("kind") != "Ingress" or doc["metadata"]["name"] == "matrix-client-ingress":
            continue
        for rule in doc["spec"].get("rules", []):
            # A rule without a host applies to every host, this one included.
            if rule.get("host") not in (matrix_host, None):
                continue
            for path in rule.get("http", {}).get("paths", []):
                assert not takes_the_upload_from_matrix(path), (
                    f"{doc['metadata']['name']} shadows the upload route "
                    f"with {path.get('path')}"
                )


def assert_homeserver_keeps_synapse_default(docs: list[dict]) -> None:
    configs = [
        data
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        for data in (doc.get("data") or {}).values()
        if "media_store_path" in data
    ]
    assert configs, "homeserver.yaml not found in the render"
    for config in configs:
        parsed = yaml.safe_load(config)
        assert "max_upload_size" not in parsed, (
            "homeserver.yaml now sets max_upload_size: update this test and "
            "make sure it stays at or above matrix.maxUploadSize"
        )
        assert "media_upload_limits" not in parsed, (
            "homeserver.yaml now sets media_upload_limits: a per-user cap can "
            "undercut the promised 10 MB"
        )


def assert_annotation_follows_setting(value: str) -> None:
    annotation = matrix_ingress(render("--set", f"matrix.maxUploadSize={value}"))[
        "metadata"
    ]["annotations"].get(BODY_SIZE)
    assert annotation == value, f"{BODY_SIZE} is {annotation!r}, expected {value!r}"


def main() -> int:
    base = render()
    assert_limit_covers_the_promise(base, "base")
    assert_nothing_narrower_shadows_the_upload(base)
    assert_homeserver_keeps_synapse_default(base)
    assert_annotation_follows_setting("25m")

    # An older private copy of values.yaml has no matrix.maxUploadSize.
    assert_limit_covers_the_promise(
        render("--set", "matrix.maxUploadSize=null"), "values without the key"
    )

    for overlay in ("values-dev.yaml", "values-pre-dev.yaml"):
        # values-dev.yaml and values-pre-dev.yaml need the render-domain fixture
        # and secrets layered after them, as in a real install.
        docs = render(
            "-f",
            os.path.join(CHART_DIR, overlay),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
        )
        assert_limit_covers_the_promise(docs, overlay)

    print("matrix upload limit render contract: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
