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
    paths = [
        path["path"]
        for rule in ingress["spec"]["rules"]
        for path in rule["http"]["paths"]
    ]
    assert "/_matrix" in paths, f"{source}: /_matrix left the ingress: {paths}"


def assert_nothing_narrower_shadows_the_upload(docs: list[dict]) -> None:
    """A more specific /_matrix path would win in nginx and carry its own limit."""
    for doc in docs:
        if doc.get("kind") != "Ingress" or doc["metadata"]["name"] == "matrix-client-ingress":
            continue
        for rule in doc["spec"].get("rules", []):
            for path in rule.get("http", {}).get("paths", []):
                value = path.get("path") or ""
                assert not value.startswith("/_matrix/media/v3/upload"), (
                    f"{doc['metadata']['name']} shadows the upload route with {value}"
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


def main() -> int:
    base = render()
    assert_limit_covers_the_promise(base, "base")
    assert_nothing_narrower_shadows_the_upload(base)
    assert_homeserver_keeps_synapse_default(base)

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
