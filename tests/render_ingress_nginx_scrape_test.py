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
    print("PASS: ingress-nginx scrape is opt-in and needs appMetrics")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
