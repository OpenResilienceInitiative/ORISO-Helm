#!/usr/bin/env python3
"""Check a proposed Helm manifest against the current SigNoz ClickHouse storage."""

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, localcontext
import json
import re
import subprocess
import sys

import yaml

from scripts.signoz_runtime_acceptance import Runner, _current_clickhouse_statefulsets, _kubectl


CHI_API = "clickhouse.altinity.com/v1"
STORAGE_QUANTITY = re.compile(r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))((?:[KMGTPE]i|[numkMGTPE]|[eE][+-]?[0-9]+)?)")
DECIMAL_EXPONENTS = dict(zip(("n", "u", "m", "", "k", "M", "G", "T", "P", "E"), range(-9, 19, 3)))


def byte_count(quantity):
    value = str(quantity)
    match = STORAGE_QUANTITY.fullmatch(value) if len(value) <= 64 else None
    if match is None:
        raise ValueError("invalid storage quantity")
    amount, suffix = match.groups()
    with localcontext() as context:
        context.prec = 128
        if suffix.endswith("i"):
            multiplier = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
        else:
            exponent = DECIMAL_EXPONENTS.get(suffix)
            if exponent is None:
                exponent = int(suffix[1:])
            if not -64 <= exponent <= 64:
                raise ValueError("storage quantity exponent exceeds the supported range")
            multiplier = Decimal(10) ** exponent
        size = (Decimal(amount) * multiplier).to_integral_value(rounding=ROUND_CEILING)
    if not 0 < size <= 2**63 - 1:
        raise ValueError("storage quantity must be a positive byte count")
    return int(size)


def _data_template_names(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "dataVolumeClaimTemplate":
                yield item
            else:
                yield from _data_template_names(item)
    elif isinstance(value, list):
        for item in value:
            yield from _data_template_names(item)


def _data_template(installation):
    spec = installation["spec"]
    name = spec.get("defaults", {}).get("templates", {}).get("dataVolumeClaimTemplate")
    if not name or any(item != name for item in _data_template_names(spec)):
        raise ValueError("a single ClickHouse data claim template is required")
    matching = [item for item in spec["templates"]["volumeClaimTemplates"] if item["name"] == name]
    if len(matching) != 1:
        raise ValueError("missing or duplicate ClickHouse data claim template")
    template = deepcopy(matching[0])
    if template.get("reclaimPolicy") != "Retain":
        raise ValueError("ClickHouse data claim template must retain data")
    size = byte_count(template["spec"]["resources"]["requests"].pop("storage"))
    return template, size


def data_claim_names(template, statefulsets, pods):
    """Read the actual PVC reference of every Pod of the owned StatefulSets."""
    sets = {obj["metadata"]["uid"]: obj for obj in statefulsets}
    observed = Counter()
    names = set()
    for pod in pods:
        metadata = pod["metadata"]
        owners = [
            ref for ref in metadata.get("ownerReferences", [])
            if ref.get("controller") is True and ref.get("kind") == "StatefulSet"
            and ref.get("uid") in sets and ref.get("name") == sets[ref["uid"]]["metadata"]["name"]
        ]
        if not owners:
            continue
        if len(owners) != 1 or metadata.get("deletionTimestamp"):
            raise ValueError("ClickHouse data Pods are being reconciled")
        volumes = [volume for volume in pod["spec"]["volumes"] if volume["name"] == template]
        if len(volumes) != 1:
            raise ValueError("ClickHouse data Pods lack a unique data claim reference")
        claim = volumes[0]["persistentVolumeClaim"]["claimName"]
        if not claim:
            raise ValueError("ClickHouse data claim reference is empty")
        names.add(claim)
        observed[owners[0]["uid"]] += 1
    if not names or any(observed[uid] != item["spec"].get("replicas", 1) for uid, item in sets.items()):
        raise ValueError("cannot identify all ClickHouse data Pods")
    return sorted(names)


def _statefulset_storage_failures(statefulsets, template_name, planned_bytes):
    failures = []
    for statefulset in statefulsets:
        name, spec = statefulset["metadata"]["name"], statefulset["spec"]
        templates = [
            template for template in spec.get("volumeClaimTemplates", [])
            if template["metadata"]["name"] == template_name
        ]
        if not templates:
            # Operator-provisioned claims can be referenced without a StatefulSet template.
            continue
        template, = templates
        current_bytes = byte_count(template["spec"]["resources"]["requests"]["storage"])
        if planned_bytes < current_bytes:
            failures.append(
                f"Planned {planned_bytes} bytes is smaller than the StatefulSet {name} "
                f"data claim template of {current_bytes} bytes."
            )
        if planned_bytes <= current_bytes:
            continue
        # An immutable template change can send Altinity through scale-down and recreation.
        policy = spec.get("persistentVolumeClaimRetentionPolicy") or {}
        unsafe = [key for key in ("whenScaled", "whenDeleted") if policy.get(key, "Retain") != "Retain"]
        if unsafe:
            failures.append(f"StatefulSet {name} must retain data claims on recreation ({', '.join(unsafe)}).")
    return failures


def storage_failures(current, target, claims, statefulsets=()):
    old_template, old_bytes = _data_template(current)
    new_template, planned_bytes = _data_template(target)
    if old_template != new_template:
        raise ValueError("the plan changes the ClickHouse data claim definition")
    if current["spec"]["configuration"]["clusters"] != target["spec"]["configuration"]["clusters"]:
        raise ValueError("the plan changes ClickHouse cluster topology")
    if not claims:
        raise ValueError("no live ClickHouse data claims")
    failures = _statefulset_storage_failures(statefulsets, old_template["name"], planned_bytes)
    if planned_bytes < old_bytes:
        failures.append(f"Planned {planned_bytes} bytes is smaller than the installation request of {old_bytes} bytes.")
    for claim in claims:
        name = claim["metadata"]["name"]
        status, spec = claim["status"], claim["spec"]
        if status["phase"] != "Bound" or not spec.get("volumeName") or claim["metadata"].get("deletionTimestamp"):
            raise ValueError(f"ClickHouse data claim {name} is not stably bound")
        floor = max(
            byte_count(spec["resources"]["requests"]["storage"]),
            byte_count(status["capacity"]["storage"]),
        )
        if planned_bytes < floor:
            failures.append(f"Planned {planned_bytes} bytes is smaller than the data claim {name} floor of {floor} bytes.")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--ssh-host")
    args = parser.parse_args()
    chi_name = f"{args.release}-clickhouse"
    try:
        target, = [
            doc for doc in yaml.safe_load_all(sys.stdin)
            if doc and doc.get("kind") == "ClickHouseInstallation" and doc.get("apiVersion") == CHI_API
            and doc["metadata"]["name"] == chi_name
            and (doc["metadata"].get("namespace") or args.namespace) == args.namespace
        ]
        runner = Runner(args.ssh_host)
        current = json.loads(_kubectl(runner, args.namespace, "get", "chi", chi_name, "-o", "json"))
        annotations = current["metadata"].get("annotations", {})
        if (annotations.get("meta.helm.sh/release-name") != args.release
                or annotations.get("meta.helm.sh/release-namespace") != args.namespace):
            raise ValueError("ClickHouseInstallation does not belong to the selected release")
        template, _ = _data_template(current)
        statefulsets = _current_clickhouse_statefulsets(runner, args.namespace, args.release, chi=current)
        pods = json.loads(_kubectl(
            runner, args.namespace, "get", "pods", "-l", f"clickhouse.altinity.com/chi={chi_name}", "-o", "json",
        ))["items"]
        names = data_claim_names(template["name"], statefulsets, pods)
        readback = json.loads(_kubectl(runner, args.namespace, "get", "pvc", *names, "-o", "json"))
        claims = readback["items"] if readback["kind"] == "List" else [readback]
        if sorted(item["metadata"]["name"] for item in claims) != names:
            raise ValueError("ClickHouse data claim readback is incomplete")
        failures = storage_failures(current, target, claims, statefulsets)
    except (AttributeError, KeyError, TypeError, OSError, RecursionError, RuntimeError,
            ValueError, subprocess.SubprocessError, yaml.YAMLError) as error:
        if type(error) in {ValueError, RuntimeError}:
            print(f"Cannot verify ClickHouse storage: {error}", file=sys.stderr)
        else:
            print("Cannot verify the planned and current ClickHouse storage.", file=sys.stderr)
        return 1
    if failures:
        print(*failures, sep="\n", file=sys.stderr)
        return 1
    print(f"PASS. ClickHouse storage is preserved for {len(claims)} data claim(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
