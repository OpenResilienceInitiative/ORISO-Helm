#!/usr/bin/env python3
"""The cluster collector scrapes a cluster-wide ingress-nginx only when asked to."""

from __future__ import annotations

import sys

import yaml

import render_signoz_k8s_infra_test as base


def cluster_config(*extra: str) -> dict:
    items = base.documents(
        base.render(signoz_enabled=True, infra_enabled=True, extra=extra)
    )
    maps = [
        item
        for item in items
        if item.get("kind") == "ConfigMap"
        and "prometheus/oriso-apps" in "".join((item.get("data") or {}).values())
    ]
    assert maps, "no rendered cluster collector ConfigMap"
    text = next(v for v in maps[0]["data"].values() if "prometheus/oriso-apps" in v)
    return yaml.safe_load(text)


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


def main() -> None:
    metrics = ("--set", "global.observability.appMetrics=true")
    on = cluster_config(
        *metrics,
        "--set-string",
        "global.observability.externalIngressNginxNamespace=ingress-nginx",
    )
    job = on["receivers"]["prometheus/oriso-ingress-nginx"]["config"]["scrape_configs"][0]
    assert job["job_name"] == "ingress-nginx"
    assert job["kubernetes_sd_configs"][0]["namespaces"]["names"] == ["ingress-nginx"]
    assert "prometheus/oriso-ingress-nginx" in on["service"]["pipelines"]["metrics/apps"]["receivers"]

    off = cluster_config(*metrics)
    assert "prometheus/oriso-ingress-nginx" not in off["receivers"]
    assert "prometheus/oriso-ingress-nginx" not in off["service"]["pipelines"]["metrics/apps"]["receivers"]

    without_app_metrics = cluster_config(
        "--set", "global.observability.dbMetrics=true",
        "--set-string", "global.observability.externalIngressNginxNamespace=ingress-nginx",
    )
    assert "prometheus/oriso-ingress-nginx" not in without_app_metrics["receivers"]
    # Dev turns the scrape on, and the cluster collector may list/watch pods in
    # every namespace (the scrape discovers pods in ingress-nginx).
    docs = dev_cluster_docs()
    dev_maps = [
        d for d in docs
        if d.get("kind") == "ConfigMap"
        and "prometheus/oriso-ingress-nginx" in "".join((d.get("data") or {}).values())
    ]
    assert dev_maps, "values-dev.yaml must enable the ingress-nginx scrape"
    roles = [
        d for d in docs
        if d.get("kind") == "ClusterRole" and d["metadata"]["name"] == "caritas-k8s-infra-otel-deployment-caritas"
    ]
    assert roles, "cluster collector ClusterRole missing"
    pod_rules = [r for r in roles[0]["rules"] if "pods" in r.get("resources", [])]
    assert pod_rules and {"list", "watch"} <= set(pod_rules[0]["verbs"]), "collector cannot list/watch pods"
    print("PASS: ingress-nginx scrape is opt-in and needs appMetrics")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
