#!/usr/bin/env python3
"""The cluster collector scrapes a cluster-wide ingress-nginx only when asked to."""

from __future__ import annotations

import sys

import render_signoz_k8s_infra_test as base

COLLECTOR = "caritas-k8s-infra-otel-deployment"
INGRESS_RECEIVER = "prometheus/oriso-ingress-nginx"


def collector_config(items: list[dict]) -> dict:
    return base.parse_config(
        base.find(items, "ConfigMap", COLLECTOR),
        "otel-deployment-config.yaml",
    )


def cluster_config(*extra: str) -> dict:
    return collector_config(
        base.documents(base.render(signoz_enabled=True, infra_enabled=True, extra=extra))
    )


def dev_cluster_docs() -> list[dict]:
    return base.documents(
        base.render(
            signoz_enabled=True,
            infra_enabled=True,
            environment="dev",
            cluster_name="oriso-dev",
            overlay="values-dev.yaml",
        )
    )


def predev_cluster_docs() -> list[dict]:
    return base.documents(
        base.render(
            signoz_enabled=True,
            infra_enabled=True,
            environment="pre-dev",
            cluster_name="oriso-predev",
            overlay="values-pre-dev.yaml",
        )
    )


def assert_ingress_scrape(config: dict) -> None:
    job = config["receivers"][INGRESS_RECEIVER]["config"]["scrape_configs"][0]
    assert job["job_name"] == "ingress-nginx"
    discovery = job["kubernetes_sd_configs"][0]
    assert discovery["role"] == "pod"
    assert discovery["namespaces"]["names"] == ["ingress-nginx"]
    assert INGRESS_RECEIVER in config["service"]["pipelines"]["metrics/apps"]["receivers"]


def main() -> None:
    metrics = ("--set", "global.observability.appMetrics=true")
    external_ingress = (
        "--set-string",
        "global.observability.externalIngressNginxNamespace=ingress-nginx",
    )
    external_only = cluster_config(*external_ingress)
    assert_ingress_scrape(external_only)
    assert "prometheus/oriso-apps" not in external_only["receivers"]
    pipeline = external_only["service"]["pipelines"]["metrics/apps"]
    assert pipeline["receivers"] == [INGRESS_RECEIVER]
    assert pipeline["processors"] == ["resource/deployenv", "resourcedetection", "batch"]
    assert pipeline["exporters"] == ["otlphttp"]

    on = cluster_config(*metrics, *external_ingress)
    assert_ingress_scrape(on)
    assert on["service"]["pipelines"]["metrics/apps"]["receivers"] == [
        "prometheus/oriso-apps", INGRESS_RECEIVER,
    ]

    off = cluster_config(*metrics)
    assert INGRESS_RECEIVER not in off["receivers"]
    assert INGRESS_RECEIVER not in off["service"]["pipelines"]["metrics/apps"]["receivers"]

    database_and_ingress = cluster_config(
        "--set", "global.observability.dbMetrics=true",
        *external_ingress,
    )
    assert_ingress_scrape(database_and_ingress)
    assert "metrics/dbs" in database_and_ingress["service"]["pipelines"]

    stock = cluster_config()
    assert INGRESS_RECEIVER not in stock["receivers"]
    assert "metrics/apps" not in stock["service"]["pipelines"]

    disabled = base.documents(
        base.render(signoz_enabled=True, infra_enabled=False, extra=external_ingress)
    )
    assert not any(item["metadata"]["name"] == COLLECTOR for item in disabled)

    # Dev turns the scrape on, and the cluster collector may list/watch pods in
    # every namespace (the scrape discovers pods in ingress-nginx).
    docs = dev_cluster_docs()
    assert_ingress_scrape(collector_config(docs))
    predev_config = collector_config(predev_cluster_docs())
    assert INGRESS_RECEIVER not in predev_config["receivers"]
    assert INGRESS_RECEIVER not in predev_config["service"]["pipelines"]["metrics/apps"][
        "receivers"
    ]
    deployment = base.find(docs, "Deployment", COLLECTOR)
    namespace = deployment["metadata"]["namespace"]
    role_name = f"{COLLECTOR}-{namespace}"
    role = base.find(docs, "ClusterRole", role_name)
    assert any(
        "" in rule.get("apiGroups", [])
        and "pods" in rule.get("resources", [])
        and {"list", "watch"} <= set(rule["verbs"])
        for rule in role["rules"]
    ), "collector cannot list/watch pods"
    binding = base.find(docs, "ClusterRoleBinding", role_name)
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": role_name,
    }
    assert {
        "kind": "ServiceAccount",
        "name": deployment["spec"]["template"]["spec"]["serviceAccountName"],
        "namespace": namespace,
    } in binding["subjects"], "collector service account is missing from the binding"
    print("PASS: Dev attaches the opt-in ingress-nginx scrape to its bound cluster collector")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
