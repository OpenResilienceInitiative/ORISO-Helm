from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_predev_render_is_reproducible_and_contains_no_latest_images(tmp_path: Path) -> None:
    subprocess.run(
        [ROOT / "scripts/predev/render-release.sh", tmp_path],
        cwd=ROOT,
        check=True,
        text=True,
    )

    manifest = (tmp_path / "manifest.yaml").read_text()
    provenance = json.loads((tmp_path / "provenance.json").read_text())

    assert ":latest" not in manifest
    assert provenance["release"] == "oriso-platform"
    assert provenance["namespace"] == "caritas"
    assert provenance["source"]["branch"] == "pre-dev"
    assert len(provenance["source"]["commit"]) == 40
    assert provenance["manifest"]["sha256"]
    assert provenance["images"]
    assert all(":latest" not in image for image in provenance["images"])


def test_predev_image_lock_contains_no_floating_latest_values() -> None:
    lock = (ROOT / "deploy/predev/images.lock.yaml").read_text()
    assert "latest" not in lock.lower()


def test_deploy_workflow_is_manual_commit_bound_and_environment_gated() -> None:
    workflow_path = ROOT / ".github/workflows/predev-deploy.yml"
    workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}
    deploy = workflow["jobs"]["predev"]
    assert deploy["environment"] == "predev"
    assert deploy["concurrency"]["group"] == "predev-deployment"

    text = workflow_path.read_text()
    assert "ref: pre-dev" in text
    assert "inputs.source_sha" in text
    assert "refs/heads/pre-dev" in text
    assert "scripts/predev/render-release.sh" in text
    assert "scripts/predev/drift-report.sh" in text
    assert "continue-on-error" not in text


def test_plan_workflow_enforces_predev_render_on_pull_requests() -> None:
    workflow_path = ROOT / ".github/workflows/predev-plan.yml"
    text = workflow_path.read_text()

    assert "pull_request:" in text
    assert "pre-dev" in text
    assert "scripts/predev/render-release.sh" in text
    assert "helm lint" in text
    assert "continue-on-error" not in text


def test_drift_report_classifies_missing_extra_and_changed(tmp_path: Path) -> None:
    expected = tmp_path / "expected.yaml"
    live = tmp_path / "live.yaml"
    previous = tmp_path / "previous.yaml"
    report = tmp_path / "report.json"

    expected.write_text(
        """
apiVersion: v1
kind: ConfigMap
metadata: {name: same, namespace: caritas}
data: {value: expected}
---
apiVersion: v1
kind: Service
metadata: {name: missing, namespace: caritas}
spec: {selector: {app: expected}}
"""
    )
    live.write_text(
        """
apiVersion: v1
kind: ConfigMap
metadata:
  name: same
  namespace: caritas
  resourceVersion: "123"
data: {value: live}
---
apiVersion: v1
kind: Secret
metadata: {name: extra, namespace: caritas}
data: {password: c2VjcmV0}
"""
    )
    previous.write_text(
        """
apiVersion: v1
kind: Secret
metadata: {name: extra, namespace: caritas}
data: {password: b2xk}
"""
    )

    result = subprocess.run(
        [
            "python3",
            ROOT / "scripts/predev/manifest_drift.py",
            "--expected",
            expected,
            "--live",
            live,
            "--previous",
            previous,
            "--output",
            report,
        ],
        cwd=ROOT,
        text=True,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text())
    assert payload["summary"] == {"changed": 1, "extra": 1, "missing": 1}
    assert payload["changed"] == ["ConfigMap/caritas/same"]
    assert payload["missing"] == ["Service/caritas/missing"]
    assert payload["extra"] == ["Secret/caritas/extra"]


def test_drift_report_ignores_api_server_defaults_and_secret_values(tmp_path: Path) -> None:
    expected = tmp_path / "expected.yaml"
    live = tmp_path / "live.yaml"
    report = tmp_path / "report.json"
    expected.write_text(
        """
apiVersion: v1
kind: Secret
metadata: {name: credentials, namespace: caritas}
stringData: {password: placeholder}
"""
    )
    live.write_text(
        """
apiVersion: v1
kind: Secret
metadata:
  name: credentials
  namespace: caritas
  uid: server-generated
data: {password: cHJvZHVjdGlvbg==}
type: Opaque
"""
    )

    result = subprocess.run(
        [
            "python3",
            ROOT / "scripts/predev/manifest_drift.py",
            "--expected",
            expected,
            "--live",
            live,
            "--output",
            report,
        ],
        cwd=ROOT,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(report.read_text())["summary"] == {
        "changed": 0,
        "extra": 0,
        "missing": 0,
    }
