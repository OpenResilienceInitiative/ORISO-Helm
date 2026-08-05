#!/usr/bin/env python3
"""Render guard for chart-owned PVC StorageClass behavior."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PVC_NAMES = {
    "matrix-synapse-data",
    "matrix-postgres-pvc",
    "matrix-postgres-backup-pvc",
    "userservice-report",
    "redis-pvc",
}
STATEFULSET_CLAIMS = {
    "mariadb": "mariadb-data",
    "mongodb": "mongodb-data",
}


def render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "storage-class",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def chart_owned_pvcs(documents: list[dict]) -> dict[str, dict]:
    return {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "PersistentVolumeClaim"
        and document.get("metadata", {}).get("name") in PVC_NAMES
    }


def statefulset_claims(documents: list[dict]) -> dict[str, dict]:
    statefulsets = {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "StatefulSet"
        and document.get("metadata", {}).get("name") in STATEFULSET_CLAIMS
    }
    return {
        name: next(
            claim
            for claim in statefulset["spec"]["volumeClaimTemplates"]
            if claim["metadata"]["name"] == STATEFULSET_CLAIMS[name]
        )
        for name, statefulset in statefulsets.items()
    }


def main() -> None:
    default_documents = render()
    default_pvcs = chart_owned_pvcs(default_documents)
    default_claims = statefulset_claims(default_documents)
    assert set(default_pvcs) == PVC_NAMES
    assert set(default_claims) == set(STATEFULSET_CLAIMS)
    for claim in list(default_pvcs.values()) + list(default_claims.values()):
        assert "storageClassName" not in claim["spec"]

    custom_documents = render(
        "--set-string", "global.storageClass=hcloud-volumes"
    )
    custom_pvcs = chart_owned_pvcs(custom_documents)
    custom_claims = statefulset_claims(custom_documents)
    assert set(custom_pvcs) == PVC_NAMES
    assert set(custom_claims) == set(STATEFULSET_CLAIMS)
    for claim in list(custom_pvcs.values()) + list(custom_claims.values()):
        assert claim["spec"]["storageClassName"] == "hcloud-volumes"

    rendered = yaml.safe_dump_all(render())
    assert "block-storage" not in rendered
    assert "local-path" not in rendered

    print("PASS: default PVCs do not hardcode a StorageClass")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
