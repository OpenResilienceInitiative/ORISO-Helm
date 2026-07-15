#!/usr/bin/env python3
"""Cold-backup and isolated-restore guard for the in-chart SigNoz stack.

This operator tool is intentionally independent from Helm. It runs on the
single K3s node that owns the local-path PVCs, pauses every writer in a fixed
order, archives both persistent stores, and restores the workloads even when a
backup step fails. Secret values are never read; the restore-verification pod
references the existing ClickHouse Secret by name and key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time
from typing import Any

DEFAULT_BACKUP_ROOT = Path("/var/backups/oriso/signoz")


def _workloads(release: str) -> list[tuple[str, str, str]]:
    return [
        ("deployment", f"{release}-otel-collector", "collector"),
        ("statefulset", f"{release}-signoz", "signoz"),
        ("statefulset", f"{release}-clickhouse", "clickhouse"),
    ]


def _restore_workloads(operator: Any, states: list[tuple[str, str, int]]) -> None:
    errors: list[Exception] = []
    for kind, name, replicas in reversed(states):
        try:
            operator.scale(kind, name, replicas)
            operator.wait_for_replicas(kind, name, replicas)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("workload restoration failed", errors)


def cold_backup(
    operator: Any,
    *,
    namespace: str,
    release: str,
    backup_root: Path,
    timestamp: str,
) -> dict[str, Any]:
    """Create a consistent cold backup and always restore prior replica counts."""

    states: list[tuple[str, str, int]] = []
    images: dict[str, str] = {}
    for kind, name, component in _workloads(release):
        states.append((kind, name, operator.replicas(kind, name)))
        images[component] = operator.workload_image(kind, name)

    signoz_pvc = f"signoz-data-{release}-signoz-0"
    clickhouse_pvc = f"clickhouse-data-{release}-clickhouse-0"
    sources = {
        "signoz": operator.pvc_path(signoz_pvc),
        "clickhouse": operator.pvc_path(clickhouse_pvc),
    }
    resolved_backup_root = backup_root.resolve()
    for component, source in sources.items():
        resolved_source = source.resolve()
        if (
            resolved_backup_root == resolved_source
            or resolved_source in resolved_backup_root.parents
        ):
            raise ValueError(
                f"backup root must not be inside PVC source for {component}"
            )
    operator.ensure_backup_capacity(sources.values(), backup_root)
    helm_release = operator.helm_release_metadata(release)

    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(backup_dir, 0o700)

    archives: dict[str, dict[str, str]] = {}
    metadata: dict[str, Any] | None = None
    backup_error: BaseException | None = None
    try:
        for kind, name, _replicas in states:
            operator.scale(kind, name, 0)
            operator.wait_for_replicas(kind, name, 0)

        for component in ("signoz", "clickhouse"):
            destination = backup_dir / f"{component}-data.tar.gz"
            digest = operator.archive(sources[component], destination)
            archives[component] = {
                "file": destination.name,
                "sha256": digest,
            }

        metadata = {
            "schema_version": 1,
            "created_at": timestamp,
            "namespace": namespace,
            "release": release,
            "helm_release": helm_release,
            "images": images,
            "replicas": {
                component: replicas
                for (_kind, _name, component), (
                    _state_kind,
                    _state_name,
                    replicas,
                ) in zip(_workloads(release), states, strict=True)
            },
            "archives": archives,
        }
        manifest = backup_dir / "manifest.json"
        manifest.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        os.chmod(manifest, 0o600)
    except BaseException as error:
        backup_error = error

    restore_error: BaseException | None = None
    try:
        _restore_workloads(operator, states)
    except BaseException as error:
        restore_error = error

    if backup_error is not None and restore_error is not None:
        raise BaseExceptionGroup(
            "backup and workload restoration failed",
            [backup_error, restore_error],
        )
    if backup_error is not None:
        raise backup_error
    if restore_error is not None:
        raise restore_error
    assert metadata is not None
    return metadata


def build_restore_verification_pod(
    *,
    namespace: str,
    name: str,
    node_name: str,
    clickhouse_image: str,
    restored_path: Path,
    configmap_name: str,
    secret_name: str,
    secret_key: str,
) -> dict[str, Any]:
    """Build a no-Service ClickHouse pod against an isolated restored copy."""

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/component": "signoz-restore-verification"},
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeName": node_name,
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "clickhouse",
                    "image": clickhouse_image,
                    "imagePullPolicy": "IfNotPresent",
                    "env": [
                        {"name": "CLICKHOUSE_DB", "value": "default"},
                        {"name": "CLICKHOUSE_USER", "value": "default"},
                        {
                            "name": "CLICKHOUSE_PASSWORD",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": secret_name,
                                    "key": secret_key,
                                }
                            },
                        },
                    ],
                    "volumeMounts": [
                        {"name": "clickhouse-data", "mountPath": "/var/lib/clickhouse"},
                        {
                            "name": "clickhouse-config",
                            "mountPath": "/etc/clickhouse-server/config.d/cluster.xml",
                            "subPath": "cluster.xml",
                            "readOnly": True,
                        },
                    ],
                    "readinessProbe": {
                        "httpGet": {"path": "/ping", "port": 8123},
                        "initialDelaySeconds": 10,
                        "periodSeconds": 5,
                        "timeoutSeconds": 2,
                        "failureThreshold": 24,
                    },
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "512Mi"},
                        "limits": {"cpu": "1", "memory": "4Gi"},
                    },
                }
            ],
            "volumes": [
                {
                    "name": "clickhouse-data",
                    "hostPath": {"path": str(restored_path), "type": "Directory"},
                },
                {
                    "name": "clickhouse-config",
                    "configMap": {"name": configmap_name},
                },
            ],
        },
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = (root / member.name).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"archive contains an unsafe path: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive contains an unsupported link: {member.name}")
        archive.extractall(destination, filter="data")


def verify_backup(
    operator: Any,
    *,
    backup_dir: Path,
    restore_root: Path,
    expected_namespace: str,
    expected_release: str,
    trusted_clickhouse_image: str,
    secret_name: str,
    secret_key: str,
) -> dict[str, Any]:
    """Verify checksums, SQLite integrity, and an isolated ClickHouse boot."""

    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported backup manifest schema")
    if manifest.get("namespace") != expected_namespace:
        raise ValueError("backup manifest namespace does not match selected target")
    if manifest.get("release") != expected_release:
        raise ValueError("backup manifest release does not match selected target")
    if manifest.get("images", {}).get("clickhouse") != trusted_clickhouse_image:
        raise ValueError(
            "backup manifest ClickHouse image does not match live workload"
        )

    archive_paths: dict[str, Path] = {}
    for component in ("signoz", "clickhouse"):
        entry = manifest["archives"][component]
        filename = entry["file"]
        if Path(filename).name != filename:
            raise ValueError(f"unsafe archive filename for {component}")
        archive_path = backup_dir / filename
        if _sha256(archive_path) != entry["sha256"]:
            raise ValueError(f"checksum mismatch for {component} archive")
        archive_paths[component] = archive_path

    if restore_root.exists():
        raise FileExistsError(f"restore root already exists: {restore_root}")

    signoz_restore = restore_root / "signoz"
    clickhouse_restore = restore_root / "clickhouse"
    try:
        restore_root.mkdir(parents=True, mode=0o700)
        _safe_extract(archive_paths["signoz"], signoz_restore)
        _safe_extract(archive_paths["clickhouse"], clickhouse_restore)

        database = signoz_restore / "signoz.db"
        if not database.is_file():
            raise ValueError("restored SigNoz metadata has no signoz.db")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            sqlite_integrity = connection.execute("PRAGMA integrity_check").fetchone()[
                0
            ]
        if sqlite_integrity != "ok":
            raise ValueError(
                f"restored SigNoz SQLite integrity failed: {sqlite_integrity}"
            )

        table_count = operator.verify_clickhouse_restore(
            namespace=expected_namespace,
            name=f"signoz-restore-verify-{int(time.time())}",
            image=trusted_clickhouse_image,
            restored_path=clickhouse_restore,
            configmap_name=f"{expected_release}-clickhouse-config",
            secret_name=secret_name,
            secret_key=secret_key,
        )
        if table_count <= 0:
            raise ValueError(
                "restored ClickHouse contains no SigNoz application tables"
            )
        return {
            "sqlite_integrity": sqlite_integrity,
            "clickhouse_table_count": table_count,
        }
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)


class KubernetesOperator:
    def __init__(
        self,
        namespace: str,
        context: str,
        expected_node: str,
        *,
        timeout_seconds: int = 600,
    ):
        self.namespace = namespace
        self.context = context
        self.expected_node = expected_node
        self.timeout_seconds = timeout_seconds

    def _run(self, *args: str, input_text: str | None = None) -> str:
        try:
            result = subprocess.run(
                ["kubectl", "--context", self.context, *args],
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("kubectl command timed out") from error
        if result.returncode != 0:
            raise RuntimeError(
                f"kubectl --context {self.context} {' '.join(args)} failed: "
                f"{result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _helm(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [
                    "helm",
                    "--kube-context",
                    self.context,
                    "--namespace",
                    self.namespace,
                    *args,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("helm command timed out") from error
        if result.returncode != 0:
            raise RuntimeError(f"helm {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def assert_target(self) -> None:
        payload = json.loads(
            self._run("get", f"node/{self.expected_node}", "-o", "json")
        )
        if payload.get("metadata", {}).get("name") != self.expected_node:
            raise RuntimeError(
                "the selected Kubernetes context is not the expected cluster"
            )
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in payload.get("status", {}).get("conditions", [])
        )
        if not ready:
            raise RuntimeError(f"expected node {self.expected_node} is not Ready")

    def replicas(self, kind: str, name: str) -> int:
        value = self._run(
            "-n",
            self.namespace,
            "get",
            f"{kind}/{name}",
            "-o",
            "jsonpath={.spec.replicas}",
        )
        return int(value or "0")

    def workload_image(self, kind: str, name: str) -> str:
        return self._run(
            "-n",
            self.namespace,
            "get",
            f"{kind}/{name}",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        )

    def pvc_path(self, pvc_name: str) -> Path:
        volume = self._run(
            "-n",
            self.namespace,
            "get",
            f"pvc/{pvc_name}",
            "-o",
            "jsonpath={.spec.volumeName}",
        )
        payload = json.loads(self._run("get", f"pv/{volume}", "-o", "json"))
        spec = payload.get("spec", {})
        raw_path = spec.get("local", {}).get("path") or spec.get("hostPath", {}).get(
            "path"
        )
        if not raw_path:
            raise RuntimeError(
                f"PVC {pvc_name} volume has no local.path or hostPath.path"
            )
        path = Path(raw_path).resolve()
        if not path.is_absolute() or not path.is_dir():
            raise RuntimeError(f"PVC {pvc_name} does not resolve to a local directory")
        return path

    def ensure_backup_capacity(self, sources: Any, backup_root: Path) -> None:
        source_bytes = 0
        for source in sources:
            source_bytes += sum(
                path.stat().st_size
                for path in Path(source).rglob("*")
                if path.is_file()
            )

        existing = backup_root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        free_bytes = shutil.disk_usage(existing).free
        required_bytes = (source_bytes * 2) + (1024**3)
        if free_bytes < required_bytes:
            raise RuntimeError(
                "insufficient free space for backup plus isolated restore: "
                f"need {required_bytes} bytes, have {free_bytes} bytes on {existing}"
            )

    def helm_release_metadata(self, release: str) -> dict[str, Any]:
        history = json.loads(self._helm("history", release, "-o", "json"))
        deployed = [entry for entry in history if entry.get("status") == "deployed"]
        if not deployed:
            raise RuntimeError(f"Helm release {release} has no deployed revision")
        current = deployed[-1]
        return {
            "revision": int(current["revision"]),
            "chart": current["chart"],
        }

    def scale(self, kind: str, name: str, replicas: int) -> None:
        self._run(
            "-n",
            self.namespace,
            "scale",
            f"{kind}/{name}",
            f"--replicas={replicas}",
        )

    def wait_for_replicas(self, kind: str, name: str, replicas: int) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            payload = json.loads(
                self._run("-n", self.namespace, "get", f"{kind}/{name}", "-o", "json")
            )
            status = payload.get("status", {})
            actual = int(status.get("replicas") or 0)
            ready = int(status.get("readyReplicas") or 0)
            if actual == replicas and ready == replicas:
                return
            time.sleep(2)
        raise TimeoutError(f"{kind}/{name} did not reach {replicas} ready replicas")

    def archive(self, source: Path, destination: Path) -> str:
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(source, arcname=".")
        os.chmod(destination, 0o600)
        return _sha256(destination)

    def verify_clickhouse_restore(
        self,
        *,
        namespace: str,
        name: str,
        image: str,
        restored_path: Path,
        configmap_name: str,
        secret_name: str,
        secret_key: str,
    ) -> int:
        pod = build_restore_verification_pod(
            namespace=namespace,
            name=name,
            node_name=self.expected_node,
            clickhouse_image=image,
            restored_path=restored_path,
            configmap_name=configmap_name,
            secret_name=secret_name,
            secret_key=secret_key,
        )
        try:
            self._run("apply", "-f", "-", input_text=json.dumps(pod))
            self._run(
                "-n",
                namespace,
                "wait",
                "--for=condition=Ready",
                f"pod/{name}",
                f"--timeout={self.timeout_seconds}s",
            )
            count = self._run(
                "-n",
                namespace,
                "exec",
                f"pod/{name}",
                "--",
                "sh",
                "-c",
                'clickhouse-client --user "$CLICKHOUSE_USER" '
                '--password "$CLICKHOUSE_PASSWORD" '
                '--query "SELECT count() FROM system.tables '
                "WHERE database IN ('signoz_metrics','signoz_traces','signoz_logs')\"",
            )
            return int(count)
        finally:
            self._run(
                "-n",
                namespace,
                "delete",
                f"pod/{name}",
                "--ignore-not-found=true",
                "--wait=true",
            )


def _timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify"))
    parser.add_argument("--namespace", default="caritas")
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-node", required=True)
    parser.add_argument("--release", default="oriso-platform")
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--secret-name", default="clickhouse-secret")
    parser.add_argument("--secret-key", default="password")
    parser.add_argument("--confirm-maintenance-window", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "backup" and not args.confirm_maintenance_window:
        parser.error("backup requires --confirm-maintenance-window")
    if args.command == "verify" and args.backup_dir is None:
        parser.error("verify requires --backup-dir")
    return args


def main() -> int:
    args = parse_args()

    operator = KubernetesOperator(
        args.namespace,
        args.context,
        args.expected_node,
    )
    operator.assert_target()
    if args.command == "backup":
        result = cold_backup(
            operator,
            namespace=args.namespace,
            release=args.release,
            backup_root=args.backup_root,
            timestamp=_timestamp(),
        )
    else:
        trusted_clickhouse_image = operator.workload_image(
            "statefulset", f"{args.release}-clickhouse"
        )
        result = verify_backup(
            operator,
            backup_dir=args.backup_dir,
            restore_root=args.backup_root / f".restore-verify-{_timestamp()}",
            expected_namespace=args.namespace,
            expected_release=args.release,
            trusted_clickhouse_image=trusted_clickhouse_image,
            secret_name=args.secret_name,
            secret_key=args.secret_key,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
