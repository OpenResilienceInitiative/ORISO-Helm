#!/usr/bin/env python3
"""Prevent removed MatrixRTC cutover compatibility contracts from rendering."""

from __future__ import annotations

import pathlib
import subprocess

import yaml

CHART_DIR = pathlib.Path(__file__).resolve().parents[1]


def test_removed_runtime_contracts_do_not_render() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "matrixrtc-runtime-residue",
            str(CHART_DIR),
            "-f",
            str(CHART_DIR / "values.yaml.default"),
            "-f",
            str(CHART_DIR / "secrets.yaml.default"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    rendered = result.stdout
    assert "LIVE_SERVICE_API_URL" not in rendered
    assert not any(
        document.get("kind") == "Ingress"
        and document.get("metadata", {}).get("name")
        in {"element-call-ingress", "element-call-room-compat-ingress"}
        for document in documents
    )
    assert "acmepredev" not in rendered
    assert "/sessions/.*/room" not in rendered
