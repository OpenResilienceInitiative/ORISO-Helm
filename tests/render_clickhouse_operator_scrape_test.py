#!/usr/bin/env python3
"""The cluster collector scrapes the ClickHouse operator's metrics exporter (#669)."""

from __future__ import annotations

import sys

import render_signoz_k8s_infra_test as base

OPERATOR = "caritas-clickhouse-operator"
COLLECTOR = "caritas-k8s-infra-otel-deployment"


def operator_annotations(docs: list[dict]) -> dict:
    return base.find(docs, "Deployment", OPERATOR)["spec"]["template"]["metadata"]["annotations"]


def main() -> None:
    on = base.documents(base.render(
        signoz_enabled=True, infra_enabled=True,
        extra=("--set", "global.observability.appMetrics=true"),
    ))
    annotations = operator_annotations(on)
    assert annotations["oriso.io/scrape"] == "true"
    assert annotations["oriso.io/job"] == "clickhouse-operator"
    assert annotations["oriso.io/port"] == "8888"
    assert annotations["oriso.io/path"] == "/metrics"
    # The operator runs where the oriso-apps job looks.
    operator_ns = base.find(on, "Deployment", OPERATOR)["metadata"]["namespace"]
    config = base.parse_config(base.find(on, "ConfigMap", COLLECTOR), "otel-deployment-config.yaml")
    job = config["receivers"]["prometheus/oriso-apps"]["config"]["scrape_configs"][0]
    assert job["kubernetes_sd_configs"][0]["namespaces"]["names"] == [operator_ns]
    assert job["relabel_configs"][0]["source_labels"] == ["__meta_kubernetes_pod_annotation_oriso_io_scrape"]

    off = base.documents(base.render(signoz_enabled=True, infra_enabled=True))
    assert not any(k.startswith("oriso.io/") for k in operator_annotations(off))

    dev = base.documents(base.render(
        signoz_enabled=True, infra_enabled=True, environment="dev",
        cluster_name="oriso-dev", overlay="values-dev.yaml",
    ))
    assert operator_annotations(dev)["oriso.io/job"] == "clickhouse-operator"
    print("PASS: the ClickHouse operator exporter is scraped when app metrics are on")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
