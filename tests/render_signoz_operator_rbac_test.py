#!/usr/bin/env python3
"""Render contract for the ClickHouse operator's cluster discovery RBAC."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(signoz_enabled: bool) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "caritas",
            CHART_DIR,
            "--namespace",
            "caritas",
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
            "--set-string",
            "global.observability.deploymentEnvironment=predev",
            "--set-string",
            "signoz.signoz.env.signoz_global_external__url=https://your-domain.example.com/signoz",
            "--set",
            f"signoz.enabled={'true' if signoz_enabled else 'false'}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def find(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def main() -> None:
    enabled = render(True)
    operator = find(enabled, "Deployment", "caritas-clickhouse-operator")
    cluster_role = find(
        enabled,
        "ClusterRole",
        "caritas-clickhouse-operator-cluster-discovery",
    )
    cluster_role_binding = find(
        enabled,
        "ClusterRoleBinding",
        "caritas-clickhouse-operator-cluster-discovery",
    )

    rules = {
        (tuple(rule["apiGroups"]), tuple(rule["resources"])): set(rule["verbs"])
        for rule in cluster_role["rules"]
    }
    assert rules[(
        ("apiextensions.k8s.io",),
        ("customresourcedefinitions",),
    )] >= {"get", "list"}
    assert rules[(
        ("clickhouse.altinity.com",),
        (
            "clickhouseinstallations",
            "clickhouseinstallationtemplates",
            "clickhouseoperatorconfigurations",
        ),
    )] >= {"get", "list", "watch"}

    # Cluster scope is discovery-only. All ClickHouse resource mutation remains
    # under the namespace Role rendered by the dependency chart.
    assert not {
        "create",
        "delete",
        "deletecollection",
        "patch",
        "update",
    } & {verb for verbs in rules.values() for verb in verbs}

    operator_service_account = operator["spec"]["template"]["spec"][
        "serviceAccountName"
    ]
    assert cluster_role_binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": cluster_role["metadata"]["name"],
    }
    assert cluster_role_binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": operator_service_account,
            "namespace": "caritas",
        }
    ]

    disabled_names = {
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in render(False)
    }
    assert (
        "ClusterRole",
        "caritas-clickhouse-operator-cluster-discovery",
    ) not in disabled_names
    assert (
        "ClusterRoleBinding",
        "caritas-clickhouse-operator-cluster-discovery",
    ) not in disabled_names

    print("PASS: SigNoz ClickHouse operator has least-privilege cluster discovery RBAC")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
