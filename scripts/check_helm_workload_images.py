#!/usr/bin/env python3
"""Check an upgrade or rollback manifest against live Helm workload image references."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

import yaml

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob"}
KUBERNETES_NAME = re.compile(r"[a-z0-9][a-z0-9.-]*")


def _name(value: str) -> str:
    if not KUBERNETES_NAME.fullmatch(value):
        raise ValueError("invalid resource name")
    return value


def _images(workload: dict[str, Any]) -> dict[str, str]:
    spec = workload["spec"]
    if workload["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    pod = spec["template"]["spec"]
    images = {}
    for category in ("containers", "initContainers"):
        containers = pod.get(category)
        if category == "initContainers" and containers is None:
            continue
        if not isinstance(containers, list) or (category == "containers" and not containers):
            raise ValueError("invalid container list")
        for container in containers:
            key = f"{category}/{_name(container['name'])}"
            image = container["image"]
            if not isinstance(image, str) or not image.strip() or key in images:
                raise ValueError("invalid or duplicate image reference")
            images[key] = image
    return images


def _workloads(
    documents: Iterable[dict[str, Any]], namespace: str, *, release: str | None = None
) -> dict[str, dict[str, str]]:
    workloads = {}
    for document in documents:
        kind = document["kind"]
        if kind not in WORKLOAD_KINDS:
            continue
        metadata = document["metadata"]
        document_namespace = metadata.get("namespace")
        if release is None and not document_namespace:
            document_namespace = namespace
        if document_namespace != namespace:
            continue
        if release is not None:
            annotations = metadata.get("annotations") or {}
            if (
                annotations.get("meta.helm.sh/release-name") != release
                or annotations.get("meta.helm.sh/release-namespace") != namespace
                or any(owner.get("controller") is True for owner in metadata.get("ownerReferences") or [])
            ):
                continue
        key = f"{kind}/{_name(metadata['name'])}"
        if key in workloads:
            raise ValueError("duplicate workload")
        workloads[key] = _images(document)
    return workloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument(
        "--allow-remove-infra-collectors",
        action="store_true",
        help="Allow the named infrastructure collector DaemonSet and Deployment to be absent",
    )
    args = parser.parse_args()
    try:
        target = _workloads((doc for doc in yaml.safe_load_all(sys.stdin) if doc is not None), args.namespace)
        if not target:
            raise ValueError("empty manifest workload scope")
    except (AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError):
        print("Cannot read the proposed Helm workloads.", file=sys.stderr)
        return 1

    try:
        response = subprocess.run(
            ["kubectl", "-n", args.namespace, "get", "deployment,statefulset,daemonset,cronjob",
             "-o", "json", "--request-timeout=20s"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        items = json.loads(response.stdout)["items"]
        if not isinstance(items, list):
            raise ValueError("invalid workload list")
        live = _workloads(items, args.namespace, release=args.release)
    except (OSError, AttributeError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        print("Cannot read live workloads for the Helm release.", file=sys.stderr)
        return 1

    retained = live.keys() & target.keys()
    if not retained:
        print("No retained Helm workload images to compare.", file=sys.stderr)
        return 1
    allowed = set()
    if args.allow_remove_infra_collectors:
        allowed = {
            f"DaemonSet/{args.release}-k8s-infra-otel-agent",
            f"Deployment/{args.release}-k8s-infra-otel-deployment",
        }
    missing = live.keys() - target.keys()
    failures = [f"{key} is missing" for key in sorted(missing - allowed)]
    failures.extend(
        f"{key}/{container} has a different or missing image reference"
        for key in sorted(retained)
        for container, image in live[key].items()
        if target[key].get(container) != image
    )
    if failures:
        print(*failures, sep="\n", file=sys.stderr)
        return 1
    print(f"PASS. {sum(len(live[key]) for key in retained)} existing image references match.")
    for key in sorted(missing & allowed):
        print(f"Collector removal allowed for {key}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
