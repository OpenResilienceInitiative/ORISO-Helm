#!/usr/bin/env python3
"""Collect the live objects named by current and previous Helm manifests."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.read_text().strip():
        return []
    return [
        document
        for document in yaml.safe_load_all(path.read_text())
        if isinstance(document, dict) and document.get("kind")
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    resources: dict[tuple[str, str, str], dict[str, Any]] = {}
    for document in load(args.expected) + load(args.previous):
        metadata = document.get("metadata") or {}
        key = (
            document["kind"],
            metadata.get("namespace") or "",
            metadata["name"],
        )
        resources[key] = document

    live: list[dict[str, Any]] = []
    for kind, namespace, name in sorted(resources):
        command = ["kubectl", "get", kind, name, "-o", "json"]
        if namespace:
            command.extend(["--namespace", namespace])
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            live.append(json.loads(result.stdout))
        elif "NotFound" not in result.stderr and "not found" not in result.stderr:
            raise RuntimeError(
                f"kubectl failed for {kind}/{namespace or '_cluster'}/{name}: "
                f"{result.stderr.strip()}"
            )

    args.output.write_text(yaml.safe_dump_all(live, sort_keys=True))


if __name__ == "__main__":
    main()
