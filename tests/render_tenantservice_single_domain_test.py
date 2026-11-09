#!/usr/bin/env python3
"""Regression guard for TenantService environment wiring."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import yaml


CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = (
    "templates/_helpers.tpl",
    "templates/tenantservice/tenantservice-configmap-env.yaml",
    "templates/tenantservice/tenantservice-deployment.yaml",
)
FEATURE_FLAG = "FEATURE_MULTITENANCY_WITH_SINGLE_DOMAIN_ENABLED"
DPA_SIGNING_ENV = {
    "ORISO_DPA_LEGACY_SIGNING_ZONE": "UTC",
    "ORISO_DPA_LEGACY_SIGNING_FROM": "2026-11-07T00:00:00",
    "ORISO_DPA_LEGACY_SIGNING_TO": "2026-11-07T23:59:59",
}


def build_minimal_chart(destination: str) -> None:
    with open(os.path.join(destination, "Chart.yaml"), "w", encoding="utf-8") as chart:
        chart.write("apiVersion: v2\nname: tenant-single-domain-test\nversion: 0.0.0\n")
    shutil.copyfile(
        os.path.join(CHART_DIR, "values.yaml.default"),
        os.path.join(destination, "values.yaml"),
    )
    for relative_path in TEMPLATES:
        output = os.path.join(destination, relative_path)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        shutil.copyfile(os.path.join(CHART_DIR, relative_path), output)


def render(
    chart: str,
    enabled: bool,
    overlays: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
) -> list[dict]:
    """Render TenantService with the supplied layers and final Helm overrides."""
    overlay_args = [
        argument
        for overlay in overlays
        for argument in ("-f", os.path.join(CHART_DIR, overlay))
    ]
    result = subprocess.run(
        [
            "helm",
            "template",
            "tenant-single-domain-test",
            chart,
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            # The chart requires a real public host (ORISO-Helm#366).
            "--set-string",
            "global.domainName=render.oriso.internal",
            "--set-string",
            f"global.multitenancyWithSingleDomainEnabled={str(enabled).lower()}",
            *overlay_args,
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def named(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def verify(documents: list[dict], expected: str) -> None:
    config_map = named(documents, "ConfigMap", "tenantservice-configmap-env")
    deployment = named(documents, "Deployment", "tenantservice")
    assert config_map["data"][FEATURE_FLAG] == expected

    environment = {
        entry["name"]: entry
        for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment[FEATURE_FLAG]["valueFrom"]["configMapKeyRef"] == {
        "name": "tenantservice-configmap-env",
        "key": FEATURE_FLAG,
    }


def verify_legacy_signing_environment(chart: str) -> None:
    """Check the environment defaults and the complete-window opt-in contract."""
    def assert_environment(documents: list[dict], expected: dict[str, str]) -> None:
        deployment = named(documents, "Deployment", "tenantservice")
        entries = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        selected = [entry for entry in entries if entry["name"] in DPA_SIGNING_ENV]
        assert selected == [
            {"name": name, "value": value} for name, value in expected.items()
        ], f"expected {expected!r}, found {selected!r}"

    assert_environment(render(chart, True, ("values-dev.yaml",)), DPA_SIGNING_ENV)
    for overlays in (
        (),
        ("values-dev.yaml", "values-pre-dev.yaml"),
        ("values-prod.yaml",),
    ):
        assert_environment(render(chart, True, overlays), {})

    assert_environment(
        render(
            chart,
            True,
            ("values-prod.yaml",),
            extra_args=(
                "--set-string", "tenantService.legacyDpaSigning.zone=Europe/Berlin",
                "--set-string", "tenantService.legacyDpaSigning.from=2026-09-16T04:30:00",
                "--set-string", "tenantService.legacyDpaSigning.to=2026-09-16T06:30:00",
            ),
        ),
        {
            "ORISO_DPA_LEGACY_SIGNING_ZONE": "Europe/Berlin",
            "ORISO_DPA_LEGACY_SIGNING_FROM": "2026-09-16T04:30:00",
            "ORISO_DPA_LEGACY_SIGNING_TO": "2026-09-16T06:30:00",
        },
    )

    assignments = ("zone=UTC", "from=2026-11-07T00:00:00", "to=2026-11-07T23:59:59")
    for missing in assignments:
        args = tuple(
            arg for assignment in assignments if assignment != missing
            for arg in ("--set-string", f"tenantService.legacyDpaSigning.{assignment}")
        )
        assert_environment(
            render(
                chart,
                True,
                extra_args=args,
            ),
            {},
        )


def main() -> None:
    temporary_directory = tempfile.mkdtemp(prefix="tenant-single-domain-test-")
    try:
        chart = os.path.join(temporary_directory, "chart")
        os.makedirs(chart)
        build_minimal_chart(chart)
        verify(render(chart, True), "true")
        verify(render(chart, False), "false")
        verify_legacy_signing_environment(chart)
        print("PASS: TenantService uses the single-domain flag and requires a complete DPA signing window")
    finally:
        shutil.rmtree(temporary_directory)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
