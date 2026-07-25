#!/usr/bin/env python3
"""Compare rendered Helm intent with live Kubernetes resources.

The comparison is deterministic and intentionally asymmetric: API-server
defaults that were not present in the rendered manifest are ignored, while
every rendered field must have the same live value. Secret values are never
compared or written; only their key sets are checked.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DYNAMIC_METADATA = {
    "creationTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}


def load_documents(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists() or not path.read_text().strip():
        return []
    return [
        document
        for document in yaml.safe_load_all(path.read_text())
        if isinstance(document, dict) and document.get("kind")
    ]


def identity(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") or {}
    namespace = metadata.get("namespace") or "_cluster"
    return f"{document['kind']}/{namespace}/{metadata['name']}"


def normalize(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    result.pop("status", None)
    metadata = result.setdefault("metadata", {})
    for field in DYNAMIC_METADATA:
        metadata.pop(field, None)

    if result.get("kind") == "Secret":
        keys = set((result.pop("data", None) or {}).keys())
        keys.update((result.pop("stringData", None) or {}).keys())
        result["secretKeys"] = sorted(keys)

    return result


def project_live(expected: Any, live: Any) -> Any:
    """Return only the live fields that the rendered intent controls."""
    if isinstance(expected, dict) and isinstance(live, dict):
        return {
            key: project_live(value, live.get(key))
            for key, value in expected.items()
        }
    if isinstance(expected, list) and isinstance(live, list):
        return [
            project_live(expected_value, live[index] if index < len(live) else None)
            for index, expected_value in enumerate(expected)
        ]
    return live


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    expected = {identity(doc): normalize(doc) for doc in load_documents(args.expected)}
    live = {identity(doc): normalize(doc) for doc in load_documents(args.live)}
    previous_ids = {identity(doc) for doc in load_documents(args.previous)}

    expected_ids = set(expected)
    live_ids = set(live)
    missing = sorted(expected_ids - live_ids)
    extra = sorted((live_ids | previous_ids) - expected_ids)
    changed = sorted(
        resource_id
        for resource_id in expected_ids & live_ids
        if project_live(expected[resource_id], live[resource_id]) != expected[resource_id]
    )

    payload = {
        "schema": "oriso.predev-drift.v1",
        "summary": {
            "changed": len(changed),
            "extra": len(extra),
            "missing": len(missing),
        },
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["summary"], sort_keys=True))
    return 1 if missing or extra or changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
