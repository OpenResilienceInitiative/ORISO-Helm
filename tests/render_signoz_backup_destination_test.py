#!/usr/bin/env python3
"""Exercise the backup destination through Helm's Kubernetes lookup client."""

from __future__ import annotations

import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit

import yaml

from render_signoz_dev_observability_test import CHART_DIR, render_dev_fails, render_dev_with, retain_claims


ORIGINAL = "caritas-signoz-data-protection-backup"
REPLACEMENT = "signoz-backup-recovery"
PVC_API = "/api/v1/namespaces/caritas/persistentvolumeclaims"
ORIGINAL_PATH = f"{PVC_API}/{ORIGINAL}"
REPLACEMENT_PATH = f"{PVC_API}/{REPLACEMENT}"
PV_PATH = "/api/v1/persistentvolumes/signoz-backup-recovery-pv"
BINDING = {"namespace": "caritas", "name": REPLACEMENT, "uid": "recovery-pvc-uid"}
OBJECTS = {
    ORIGINAL_PATH: {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "namespace": "caritas",
            "name": ORIGINAL,
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "helm.sh/resource-policy": "keep",
                "meta.helm.sh/release-name": "caritas",
                "meta.helm.sh/release-namespace": "caritas",
            },
        },
        "spec": {"storageClassName": "hcloud-volumes"},
    },
    REPLACEMENT_PATH: {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": BINDING,
        "spec": {
            "volumeMode": "Filesystem",
            "accessModes": ["ReadWriteOnce"],
            "volumeName": "signoz-backup-recovery-pv",
        },
        "status": {"phase": "Bound"},
    },
    PV_PATH: {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": "signoz-backup-recovery-pv"},
        "spec": {"persistentVolumeReclaimPolicy": "Retain", "claimRef": BINDING},
        "status": {"phase": "Bound"},
    },
}


def overlay(claim: object) -> dict:
    return {"signoz": {"orisoDataProtection": {"backup": {"replacementClaim": claim}}}}


def exercise_cluster(*helm_args: str, chart: str, objects: dict) -> None:
    default = render_dev_with({}, *helm_args, chart=chart)
    selected = render_dev_with(overlay(REPLACEMENT), *helm_args, chart=chart)

    pvc_key = ("PersistentVolumeClaim", ORIGINAL)
    cron_key = ("CronJob", ORIGINAL)
    assert selected[pvc_key] == default[pvc_key]
    assert ("PersistentVolumeClaim", REPLACEMENT) not in selected
    assert retain_claims(selected) == retain_claims(default)
    expected = deepcopy(default[cron_key])
    volumes = expected["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    destination = next(volume for volume in volumes if volume["name"] == "backup")
    assert destination["persistentVolumeClaim"]["claimName"] == ORIGINAL
    destination["persistentVolumeClaim"]["claimName"] = REPLACEMENT
    assert selected[cron_key] == expected

    original_metadata = OBJECTS[ORIGINAL_PATH]["metadata"]
    original_annotations = original_metadata["annotations"]
    target_spec = OBJECTS[REPLACEMENT_PATH]["spec"]
    cases = (
        (ORIGINAL_PATH, None, "existing Helm-owned backup PVC"),
        (ORIGINAL_PATH, {"metadata": {**original_metadata, "annotations": {
            **original_annotations, "meta.helm.sh/release-name": "other",
        }}}, "existing Helm-owned backup PVC"),
        (ORIGINAL_PATH, {"metadata": {**original_metadata, "annotations": {
            **original_annotations, "helm.sh/resource-policy": "",
        }}}, "existing Helm-owned backup PVC"),
        (REPLACEMENT_PATH, None, "a Bound, writable Filesystem PVC"),
        (REPLACEMENT_PATH, {"status": {"phase": "Pending"}}, "a Bound, writable Filesystem PVC"),
        (REPLACEMENT_PATH, {"metadata": {**BINDING, "deletionTimestamp": "2026-01-01T00:00:00Z"}},
         "a Bound, writable Filesystem PVC"),
        (REPLACEMENT_PATH, {"spec": {**target_spec, "volumeMode": "Block"}}, "a Bound, writable Filesystem PVC"),
        (REPLACEMENT_PATH, {"spec": {**target_spec, "accessModes": ["ReadOnlyMany"]}},
         "a Bound, writable Filesystem PVC"),
        (PV_PATH, None, "a Bound Retain PV matching the replacement PVC"),
        (PV_PATH, {"spec": {"persistentVolumeReclaimPolicy": "Delete", "claimRef": BINDING}},
         "a Bound Retain PV matching the replacement PVC"),
        (PV_PATH, {"spec": {"persistentVolumeReclaimPolicy": "Retain", "claimRef": {**BINDING, "uid": "old-uid"}}},
         "a Bound Retain PV matching the replacement PVC"),
    )
    for path, update, message in cases:
        objects.clear()
        objects.update(deepcopy(OBJECTS))
        if update is None:
            del objects[path]
        else:
            objects[path].update(update)
        error = render_dev_fails(overlay(REPLACEMENT), *helm_args, chart=chart)
        assert message in error, error


def main() -> None:
    error = render_dev_fails(overlay(REPLACEMENT))
    assert "requires an existing Helm release upgrade" in error, error
    error = render_dev_fails(overlay(REPLACEMENT), "--is-upgrade")
    assert "cluster-connected Helm render" in error, error
    disabled = overlay(REPLACEMENT)
    disabled["signoz"]["orisoDataProtection"]["backup"]["enabled"] = False
    assert ("CronJob", ORIGINAL) not in render_dev_with(disabled)
    for claim, message in (
        (False, "must be a string"),
        ("different-namespace/backup", "same namespace"),
        (ORIGINAL, "must differ from the original backup and SigNoz data claims"),
        ("signoz-db-caritas-signoz-0", "must differ from the original backup and SigNoz data claims"),
    ):
        error = render_dev_fails(overlay(claim), "--is-upgrade")
        assert message in error, error

    objects = deepcopy(OBJECTS)
    batch_v1 = {"groupVersion": "batch/v1", "version": "v1"}
    discovery = {
        "/version": {"major": "1", "minor": "31", "gitVersion": "v1.31.0"},
        "/api": {"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"]},
        "/apis": {
            "kind": "APIGroupList", "apiVersion": "v1",
            "groups": [{"name": "batch", "versions": [batch_v1], "preferredVersion": batch_v1}],
        },
    }
    for group_version, resources in (
        ("v1", (
            ("configmaps", "ConfigMap", True),
            ("persistentvolumeclaims", "PersistentVolumeClaim", True),
            ("persistentvolumes", "PersistentVolume", False),
        )),
        ("batch/v1", (("cronjobs", "CronJob", True),)),
    ):
        prefix = "/api" if group_version == "v1" else "/apis"
        discovery[f"{prefix}/{group_version}"] = {
            "kind": "APIResourceList",
            "apiVersion": "v1",
            "groupVersion": group_version,
            "resources": [
                {"name": name, "kind": kind, "namespaced": namespaced, "verbs": ["get", "list"]}
                for name, kind, namespaced in resources
            ],
        }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            response = discovery.get(path) or objects.get(path)
            self.send_response(200 if response else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response or {
                "apiVersion": "v1", "kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404,
            }).encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, TemporaryDirectory() as tmp:
        # Use the production template and values without mocking discovery for unrelated subcharts.
        chart = Path(tmp) / "chart"
        (chart / "templates/signoz").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: backup-destination-fixture\nversion: 0.1.0\n")
        for template in ("templates/_helpers.tpl", "templates/signoz/signoz-data-protection.yaml"):
            copy2(Path(CHART_DIR) / template, chart / template)

        config = Path(tmp) / "kubeconfig"
        config.write_text(yaml.safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "fixture",
            "clusters": [{"name": "fixture", "cluster": {"server": f"http://127.0.0.1:{server.server_port}"}}],
            "contexts": [{"name": "fixture", "context": {"cluster": "fixture", "user": "fixture"}}],
            "users": [{"name": "fixture", "user": {}}],
        }))
        config.chmod(0o600)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise_cluster(
                "--is-upgrade", "--dry-run=server", "--disable-openapi-validation", "--kubeconfig", str(config),
                chart=str(chart), objects=objects,
            )
        finally:
            server.shutdown()
            thread.join()

    print("PASS: SigNoz backup destination changes only after the replacement binding is verified")


if __name__ == "__main__":
    main()
