#!/usr/bin/env python3
"""Helm render contract: the E2E dashboards are off by default and opt-in (ORISO-E2E#97)."""

from __future__ import annotations

import json
import os
import pathlib
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_signoz_observability_test import CHART_DIR, find, render  # noqa: E402


def job_env(items):
    job = find(items, "Job", "caritas-signoz-observability")
    return {item["name"]: item for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}


def main() -> None:
    default = render(enabled=True, environment="dev", cluster_name="oriso-dev")
    assert job_env(default)["ORISO_E2E_DASHBOARDS"]["value"] == "false"

    on = render(
        enabled=True,
        environment="dev",
        cluster_name="oriso-dev",
        extra_args=("--set", "signoz.orisoObservability.e2eDashboards=true"),
    )
    assert job_env(on)["ORISO_E2E_DASHBOARDS"]["value"] == "true"

    # The catalog that ships in the ConfigMap carries both dashboards, so the
    # switch is the only thing standing between a release and the new dashboards.
    config = find(on, "ConfigMap", "caritas-signoz-observability")
    catalog = json.loads(config["data"]["observability-catalog.json"])
    assert {item["slug"] for item in catalog["e2eDashboards"]} == {
        "oriso-e2e-test-health",
        "oriso-e2e-flaky",
    }
    assert len(catalog["dashboards"]) == 3

    # Off in every shipped overlay.
    for overlay in ("values.yaml.default", "values-dev.yaml", "values-pre-dev.yaml", "values-prod.yaml"):
        values = yaml.safe_load(pathlib.Path(CHART_DIR, overlay).read_text(encoding="utf-8")) or {}
        managed = ((values.get("signoz") or {}).get("orisoObservability")) or {}
        assert managed.get("e2eDashboards", False) is False, overlay

    print("PASS: E2E SigNoz dashboards render off by default and opt in with one switch")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
