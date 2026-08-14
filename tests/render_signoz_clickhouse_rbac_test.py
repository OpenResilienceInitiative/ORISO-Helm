#!/usr/bin/env python3
"""Least-privilege RBAC contract for the bundled ClickHouse operator."""

from __future__ import annotations

import pathlib
import subprocess

import yaml

CHART_DIR = pathlib.Path(__file__).resolve().parents[1]
READ_VERBS = {"get", "list", "watch"}
MUTATION_VERBS = {"create", "delete", "deletecollection", "patch", "update"}


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "caritas",
            str(CHART_DIR),
            "--namespace",
            "caritas",
            "-f",
            str(CHART_DIR / "values.yaml.default"),
            "-f",
            str(CHART_DIR / "secrets.yaml.default"),
            "--set",
            "signoz.enabled=true",
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
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_disabled() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "caritas",
            str(CHART_DIR),
            "--namespace",
            "caritas",
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
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def find(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def resources_for(role: dict, api_group: str) -> set[str]:
    return {
        resource
        for rule in role["rules"]
        if api_group in rule.get("apiGroups", [])
        for resource in rule.get("resources", [])
    }


def test_clickhouse_operator_has_read_only_cluster_discovery() -> None:
    documents = render()
    role_name = "caritas-clickhouse-operator-cluster-read"
    cluster_role = find(documents, "ClusterRole", role_name)
    binding = find(documents, "ClusterRoleBinding", role_name)
    namespace_role = find(documents, "Role", "caritas-clickhouse-operator")
    operator = find(documents, "Deployment", "caritas-clickhouse-operator")
    operator_service_account = "caritas-clickhouse-operator"

    assert resources_for(cluster_role, "apiextensions.k8s.io") == {
        "customresourcedefinitions"
    }
    assert resources_for(cluster_role, "clickhouse.altinity.com") == {
        "clickhouseinstallations",
        "clickhouseinstallationtemplates",
        "clickhouseoperatorconfigurations",
    }
    assert {"namespaces", "persistentvolumes"}.issubset(resources_for(cluster_role, ""))
    for rule in cluster_role["rules"]:
        verbs = set(rule["verbs"])
        assert verbs == READ_VERBS
        assert not verbs.intersection(MUTATION_VERBS)
        assert "secrets" not in rule.get("resources", [])

    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": role_name,
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": operator_service_account,
            "namespace": "caritas",
        }
    ]
    assert (
        operator["spec"]["template"]["spec"]["serviceAccountName"]
        == operator_service_account
    )
    find(documents, "ServiceAccount", operator_service_account)

    assert "persistentvolumes" not in resources_for(namespace_role, "")
    assert "customresourcedefinitions" not in resources_for(
        namespace_role, "apiextensions.k8s.io"
    )
    assert any(
        MUTATION_VERBS.intersection(rule["verbs"]) for rule in namespace_role["rules"]
    )


def test_clickhouse_cluster_permissions_do_not_render_when_signoz_is_disabled() -> None:
    assert not any(
        document.get("metadata", {}).get("name")
        == "caritas-clickhouse-operator-cluster-read"
        for document in render_disabled()
    )
