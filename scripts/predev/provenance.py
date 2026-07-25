#!/usr/bin/env python3
"""Write commit, values, manifest and image provenance for a PreDev render."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def images(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                found.add(child)
            else:
                found.update(images(child))
    elif isinstance(value, list):
        for child in value:
            found.update(images(child))
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--redacted-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--values", type=Path, action="append", required=True)
    args = parser.parse_args()

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    documents = [
        document
        for document in yaml.safe_load_all(args.manifest.read_text())
        if isinstance(document, dict)
    ]
    payload = {
        "schema": "oriso.predev-release.v1",
        "release": "oriso-platform",
        "namespace": "caritas",
        "source": {
            "repository": "OpenResilienceInitiative/ORISO-Helm",
            "branch": "pre-dev",
            "commit": commit,
        },
        "values": [
            {"path": str(path), "sha256": sha256(path)}
            for path in args.values
        ],
        "manifest": {
            "sha256": sha256(args.redacted_manifest),
            "secret_values_redacted": True,
        },
        "images": sorted(images(documents)),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
