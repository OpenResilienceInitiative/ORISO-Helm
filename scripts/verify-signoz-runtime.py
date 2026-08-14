#!/usr/bin/env python3
"""Read-only SigNoz and ClickHouse readiness/RBAC acceptance gate."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

OPERATOR_FAILURE = re.compile(
    r"forbidden|cannot\s+(?:get|list|watch).*clickhouse|treat.*as.*delet",
    re.IGNORECASE,
)
COLLECTOR_FAILURE = re.compile(
    r"connection refused|exporting failed|no.?op.*(?:trace|metric|log|pipeline)",
    re.IGNORECASE,
)


def run_text(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(
            f"command failed ({' '.join(command)}): {result.stderr.strip()}"
        )
    return result.stdout


def require_can_i(
    run: Callable[[list[str]], str],
    service_account: str,
    namespace: str,
    verb: str,
    resource: str,
    all_namespaces: bool,
    expected: bool,
) -> None:
    command = [
        "kubectl",
        "auth",
        "can-i",
        verb,
        resource,
        "--as",
        f"system:serviceaccount:{namespace}:{service_account}",
    ]
    if all_namespaces:
        command.append("--all-namespaces")
    else:
        command.extend(["--namespace", namespace])
    actual = run(command).strip().lower() == "yes"
    if actual is not expected:
        qualifier = "cluster-wide" if all_namespaces else f"in {namespace}"
        raise ValueError(
            f"operator RBAC for {verb} {resource} {qualifier} was {actual}, "
            f"expected {expected}"
        )


def require_ready_endpoint(
    run: Callable[[list[str]], str],
    namespace: str,
    service: str,
    expected_ports: set[int] | None = None,
) -> None:
    endpoint = json.loads(
        run(
            [
                "kubectl",
                "--namespace",
                namespace,
                "get",
                "endpoints",
                service,
                "-o",
                "json",
            ]
        )
    )
    addresses = [
        address
        for subset in endpoint.get("subsets", [])
        for address in subset.get("addresses", [])
    ]
    if not addresses:
        raise ValueError(f"service {service} has no ready endpoint")
    if expected_ports:
        actual_ports = {
            port.get("port")
            for subset in endpoint.get("subsets", [])
            for port in subset.get("ports", [])
        }
        missing_ports = expected_ports - actual_ports
        if missing_ports:
            raise ValueError(
                f"service {service} has no ready endpoint for ports "
                f"{sorted(missing_ports)}"
            )


def pvc_uids(payload: dict) -> dict[str, str]:
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("PVC evidence has no item list")
    result = {
        item.get("metadata", {}).get("name"): item.get("metadata", {}).get("uid")
        for item in items
        if isinstance(item, dict)
    }
    if not result or any(not name or not uid for name, uid in result.items()):
        raise ValueError("PVC evidence contains no complete name/UID set")
    return result


def verify_runtime(
    release: str,
    namespace: str,
    service_account: str,
    expected_pvc_uids: dict[str, str],
    run: Callable[[list[str]], str] = run_text,
) -> dict:
    for resource in (
        "customresourcedefinitions.apiextensions.k8s.io",
        "clickhouseinstallations.clickhouse.altinity.com",
        "clickhouseinstallationtemplates.clickhouse.altinity.com",
        "clickhouseoperatorconfigurations.clickhouse.altinity.com",
    ):
        require_can_i(
            run,
            service_account,
            namespace,
            "list",
            resource,
            all_namespaces=True,
            expected=True,
        )
    require_can_i(
        run,
        service_account,
        namespace,
        "delete",
        "clickhouseinstallations.clickhouse.altinity.com",
        all_namespaces=True,
        expected=False,
    )
    require_can_i(
        run,
        service_account,
        namespace,
        "update",
        "clickhouseinstallations.clickhouse.altinity.com",
        all_namespaces=False,
        expected=True,
    )

    operator = f"{release}-clickhouse-operator"
    collector = f"{release}-signoz-otel-collector"
    signoz = f"{release}-signoz"
    clickhouse = f"{release}-clickhouse"

    current_pvc_uids = pvc_uids(
        json.loads(
            run(
                [
                    "kubectl",
                    "--namespace",
                    namespace,
                    "get",
                    "pvc",
                    "--selector",
                    f"clickhouse.altinity.com/chi={clickhouse}",
                    "-o",
                    "json",
                ]
            )
        )
    )
    if current_pvc_uids != expected_pvc_uids:
        raise ValueError(
            "ClickHouse PVC continuity does not match the pre-upgrade snapshot"
        )

    for command in (
        [
            "kubectl",
            "--namespace",
            namespace,
            "rollout",
            "status",
            f"deployment/{operator}",
            "--timeout=5m",
        ],
        [
            "kubectl",
            "--namespace",
            namespace,
            "rollout",
            "status",
            f"deployment/{collector}",
            "--timeout=5m",
        ],
        [
            "kubectl",
            "--namespace",
            namespace,
            "rollout",
            "status",
            f"statefulset/{signoz}",
            "--timeout=5m",
        ],
        [
            "kubectl",
            "--namespace",
            namespace,
            "wait",
            "--for=condition=complete",
            "job/signoz-telemetrystore-migrator",
            "--timeout=5m",
        ],
    ):
        run(command)

    installation = json.loads(
        run(
            [
                "kubectl",
                "--namespace",
                namespace,
                "get",
                "clickhouseinstallation",
                clickhouse,
                "-o",
                "json",
            ]
        )
    )
    status = installation.get("status", {})
    if status.get("status") != "Completed":
        raise ValueError(f"ClickHouse installation status is {status.get('status')!r}")
    if status.get("hostsCount") != status.get("hostsCompletedCount"):
        raise ValueError("ClickHouse installation has incomplete hosts")

    services = (clickhouse, collector, signoz)
    for service in services:
        expected_ports = {4317, 4318} if service == collector else None
        require_ready_endpoint(run, namespace, service, expected_ports)

    logs = run(
        [
            "kubectl",
            "--namespace",
            namespace,
            "logs",
            f"deployment/{operator}",
            "--since=10m",
        ]
    )
    match = OPERATOR_FAILURE.search(logs)
    if match:
        raise ValueError(f"ClickHouse operator log still contains {match.group(0)!r}")

    collector_logs = run(
        [
            "kubectl",
            "--namespace",
            namespace,
            "logs",
            f"deployment/{collector}",
            "--since=10m",
        ]
    )
    collector_match = COLLECTOR_FAILURE.search(collector_logs)
    if collector_match:
        raise ValueError(
            f"SigNoz collector log still contains {collector_match.group(0)!r}"
        )

    return {
        "release": release,
        "namespace": namespace,
        "clickhouseStatus": status["status"],
        "readyServices": len(services),
        "clusterMutationDenied": True,
        "pvcContinuityVerified": True,
        "otlpPortsReady": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="caritas")
    parser.add_argument("--namespace", default="caritas")
    parser.add_argument("--service-account", default="caritas-clickhouse-operator")
    parser.add_argument(
        "--pvc-snapshot",
        required=True,
        help="pre-upgrade kubectl PVC JSON used to prove UID continuity",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with open(args.pvc_snapshot, encoding="utf-8") as snapshot_file:
            expected_pvc_uids = pvc_uids(json.load(snapshot_file))
        result = verify_runtime(
            args.release,
            args.namespace,
            args.service_account,
            expected_pvc_uids,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
