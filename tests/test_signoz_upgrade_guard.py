from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "signoz_upgrade_guard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("signoz_upgrade_guard", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOperator:
    def __init__(self, *, fail_archive: bool = False, fail_restore_names=()):
        self.calls: list[tuple] = []
        self.fail_archive = fail_archive
        self.fail_restore_names = set(fail_restore_names)

    def replicas(self, kind: str, name: str) -> int:
        self.calls.append(("replicas", kind, name))
        return {"deployment": 1, "statefulset": 1}[kind]

    def workload_image(self, kind: str, name: str) -> str:
        self.calls.append(("image", kind, name))
        return {
            "oriso-platform-signoz": "signoz/signoz:v0.132.2",
            "oriso-platform-clickhouse": "clickhouse/clickhouse-server:25.12.5",
            "oriso-platform-otel-collector": "signoz/signoz-otel-collector:v0.144.5",
        }[name]

    def pvc_path(self, pvc_name: str) -> Path:
        self.calls.append(("pvc_path", pvc_name))
        return Path("/var/lib/rancher/k3s/storage") / pvc_name

    def ensure_backup_capacity(self, sources, backup_root):
        self.calls.append(("capacity", tuple(sources), backup_root))

    def helm_release_metadata(self, release: str):
        self.calls.append(("helm_release", release))
        return {"revision": 42, "chart": "online-counseling-2.0.1"}

    def scale(self, kind: str, name: str, replicas: int) -> None:
        self.calls.append(("scale", kind, name, replicas))
        if replicas > 0 and name in self.fail_restore_names:
            raise RuntimeError(f"restore failed for {name}")

    def wait_for_replicas(self, kind: str, name: str, replicas: int) -> None:
        self.calls.append(("wait", kind, name, replicas))

    def archive(self, source: Path, destination: Path) -> str:
        self.calls.append(("archive", source.name, destination.name))
        if self.fail_archive:
            raise RuntimeError("archive failed")
        return f"sha256:{source.name}"


def test_cold_backup_stops_writers_before_archiving_and_restores_in_reverse_order(
    tmp_path,
):
    guard = load_module()
    operator = FakeOperator()

    metadata = guard.cold_backup(
        operator,
        namespace="caritas",
        release="oriso-platform",
        backup_root=tmp_path,
        timestamp="20260715T151500Z",
    )

    pause_and_archive = [
        call for call in operator.calls if call[0] in {"scale", "wait", "archive"}
    ]
    assert pause_and_archive == [
        ("scale", "deployment", "oriso-platform-otel-collector", 0),
        ("wait", "deployment", "oriso-platform-otel-collector", 0),
        ("scale", "statefulset", "oriso-platform-signoz", 0),
        ("wait", "statefulset", "oriso-platform-signoz", 0),
        ("scale", "statefulset", "oriso-platform-clickhouse", 0),
        ("wait", "statefulset", "oriso-platform-clickhouse", 0),
        (
            "archive",
            "signoz-data-oriso-platform-signoz-0",
            "signoz-data.tar.gz",
        ),
        (
            "archive",
            "clickhouse-data-oriso-platform-clickhouse-0",
            "clickhouse-data.tar.gz",
        ),
        ("scale", "statefulset", "oriso-platform-clickhouse", 1),
        ("wait", "statefulset", "oriso-platform-clickhouse", 1),
        ("scale", "statefulset", "oriso-platform-signoz", 1),
        ("wait", "statefulset", "oriso-platform-signoz", 1),
        ("scale", "deployment", "oriso-platform-otel-collector", 1),
        ("wait", "deployment", "oriso-platform-otel-collector", 1),
    ]
    assert metadata["images"]["signoz"] == "signoz/signoz:v0.132.2"
    assert metadata["helm_release"] == {
        "revision": 42,
        "chart": "online-counseling-2.0.1",
    }
    assert metadata["archives"]["clickhouse"]["sha256"].startswith("sha256:")
    assert (tmp_path / "20260715T151500Z" / "manifest.json").is_file()
    assert next(
        i for i, call in enumerate(operator.calls) if call[0] == "capacity"
    ) < next(i for i, call in enumerate(operator.calls) if call[0] == "scale")


def test_cold_backup_restores_workloads_even_when_archiving_fails(tmp_path):
    guard = load_module()
    operator = FakeOperator(fail_archive=True)

    with pytest.raises(RuntimeError, match="archive failed"):
        guard.cold_backup(
            operator,
            namespace="caritas",
            release="oriso-platform",
            backup_root=tmp_path,
            timestamp="20260715T151500Z",
        )

    assert operator.calls[-6:] == [
        ("scale", "statefulset", "oriso-platform-clickhouse", 1),
        ("wait", "statefulset", "oriso-platform-clickhouse", 1),
        ("scale", "statefulset", "oriso-platform-signoz", 1),
        ("wait", "statefulset", "oriso-platform-signoz", 1),
        ("scale", "deployment", "oriso-platform-otel-collector", 1),
        ("wait", "deployment", "oriso-platform-otel-collector", 1),
    ]


def test_cold_backup_attempts_every_restore_and_preserves_the_backup_failure(tmp_path):
    guard = load_module()
    operator = FakeOperator(
        fail_archive=True,
        fail_restore_names={
            "oriso-platform-clickhouse",
            "oriso-platform-signoz",
        },
    )

    with pytest.raises(BaseExceptionGroup) as error:
        guard.cold_backup(
            operator,
            namespace="caritas",
            release="oriso-platform",
            backup_root=tmp_path,
            timestamp="20260715T151500Z",
        )

    messages = str(error.value)
    assert "backup and workload restoration failed" in messages
    assert any("archive failed" in str(item) for item in error.value.exceptions)
    assert ("scale", "deployment", "oriso-platform-otel-collector", 1) in operator.calls


def test_restore_verification_pod_uses_isolated_copy_and_secret_reference():
    guard = load_module()

    pod = guard.build_restore_verification_pod(
        namespace="caritas",
        name="signoz-restore-verify-20260715",
        node_name="hassan-dev",
        clickhouse_image="clickhouse/clickhouse-server:25.12.5",
        restored_path=Path("/var/backups/signoz/restore/clickhouse"),
        configmap_name="oriso-platform-clickhouse-config",
        secret_name="clickhouse-secret",
        secret_key="password",
    )

    container = pod["spec"]["containers"][0]
    assert pod["metadata"]["namespace"] == "caritas"
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["spec"]["nodeName"] == "hassan-dev"
    assert container["image"] == "clickhouse/clickhouse-server:25.12.5"
    assert container["volumeMounts"][0]["mountPath"] == "/var/lib/clickhouse"
    assert pod["spec"]["volumes"][0]["hostPath"] == {
        "path": "/var/backups/signoz/restore/clickhouse",
        "type": "Directory",
    }
    password = next(
        item for item in container["env"] if item["name"] == "CLICKHOUSE_PASSWORD"
    )
    assert "value" not in password
    assert password["valueFrom"]["secretKeyRef"] == {
        "name": "clickhouse-secret",
        "key": "password",
    }


class FakeRestoreVerifier:
    def __init__(self):
        self.calls: list[tuple] = []

    def verify_clickhouse_restore(self, **kwargs):
        self.calls.append(("verify_clickhouse_restore", kwargs))
        assert (kwargs["restored_path"] / "store" / "part.bin").read_text() == "data"
        return 17


def _archive(source: Path, destination: Path) -> str:
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(source, arcname=".")
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _valid_backup(tmp_path: Path) -> Path:
    backup = tmp_path / "backup"
    source = tmp_path / "source"
    signoz = source / "signoz"
    clickhouse = source / "clickhouse"
    signoz.mkdir(parents=True)
    (clickhouse / "store").mkdir(parents=True)
    (clickhouse / "store" / "part.bin").write_text("data")
    with sqlite3.connect(signoz / "signoz.db") as connection:
        connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO proof VALUES (1)")

    backup.mkdir()
    signoz_digest = _archive(signoz, backup / "signoz-data.tar.gz")
    clickhouse_digest = _archive(clickhouse, backup / "clickhouse-data.tar.gz")
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "namespace": "caritas",
                "release": "oriso-platform",
                "images": {
                    "clickhouse": "clickhouse/clickhouse-server:25.12.5",
                },
                "archives": {
                    "signoz": {"file": "signoz-data.tar.gz", "sha256": signoz_digest},
                    "clickhouse": {
                        "file": "clickhouse-data.tar.gz",
                        "sha256": clickhouse_digest,
                    },
                },
            }
        )
    )
    return backup


def test_verify_backup_checks_sqlite_and_starts_clickhouse_from_isolated_copy(tmp_path):
    guard = load_module()
    backup = _valid_backup(tmp_path)
    verifier = FakeRestoreVerifier()

    result = guard.verify_backup(
        verifier,
        backup_dir=backup,
        restore_root=tmp_path / "restore",
        expected_namespace="caritas",
        expected_release="oriso-platform",
        trusted_clickhouse_image="clickhouse/clickhouse-server:25.12.5",
        secret_name="clickhouse-secret",
        secret_key="password",
    )

    assert result == {"sqlite_integrity": "ok", "clickhouse_table_count": 17}
    call = verifier.calls[0][1]
    assert call["namespace"] == "caritas"
    assert call["image"] == "clickhouse/clickhouse-server:25.12.5"
    assert call["configmap_name"] == "oriso-platform-clickhouse-config"
    assert not (tmp_path / "restore").exists()


def test_verify_backup_rejects_a_tampered_archive_before_restore(tmp_path):
    guard = load_module()
    backup = _valid_backup(tmp_path)
    with (backup / "signoz-data.tar.gz").open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        guard.verify_backup(
            FakeRestoreVerifier(),
            backup_dir=backup,
            restore_root=tmp_path / "restore",
            expected_namespace="caritas",
            expected_release="oriso-platform",
            trusted_clickhouse_image="clickhouse/clickhouse-server:25.12.5",
            secret_name="clickhouse-secret",
            secret_key="password",
        )


def test_verify_backup_rejects_manifest_target_mismatch_before_starting_a_pod(tmp_path):
    guard = load_module()
    backup = _valid_backup(tmp_path)
    verifier = FakeRestoreVerifier()

    with pytest.raises(ValueError, match="namespace does not match"):
        guard.verify_backup(
            verifier,
            backup_dir=backup,
            restore_root=tmp_path / "restore",
            expected_namespace="other-namespace",
            expected_release="oriso-platform",
            trusted_clickhouse_image="clickhouse/clickhouse-server:25.12.5",
            secret_name="clickhouse-secret",
            secret_key="password",
        )

    assert verifier.calls == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("release", "other-release", "release does not match"),
        ("image", "clickhouse/clickhouse-server:tampered", "image does not match"),
    ],
)
def test_verify_backup_rejects_other_untrusted_manifest_execution_fields(
    tmp_path, field, value, message
):
    guard = load_module()
    backup = _valid_backup(tmp_path)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if field == "image":
        manifest["images"]["clickhouse"] = value
    else:
        manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))
    verifier = FakeRestoreVerifier()

    with pytest.raises(ValueError, match=message):
        guard.verify_backup(
            verifier,
            backup_dir=backup,
            restore_root=tmp_path / "restore",
            expected_namespace="caritas",
            expected_release="oriso-platform",
            trusted_clickhouse_image="clickhouse/clickhouse-server:25.12.5",
            secret_name="clickhouse-secret",
            secret_key="password",
        )

    assert verifier.calls == []


def test_verify_backup_rejects_an_empty_clickhouse_restore(tmp_path):
    guard = load_module()
    backup = _valid_backup(tmp_path)
    verifier = FakeRestoreVerifier()
    verifier.verify_clickhouse_restore = lambda **_kwargs: 0

    with pytest.raises(ValueError, match="no SigNoz application tables"):
        guard.verify_backup(
            verifier,
            backup_dir=backup,
            restore_root=tmp_path / "restore",
            expected_namespace="caritas",
            expected_release="oriso-platform",
            trusted_clickhouse_image="clickhouse/clickhouse-server:25.12.5",
            secret_name="clickhouse-secret",
            secret_key="password",
        )


def test_cold_backup_rejects_a_backup_root_inside_a_source_pvc(tmp_path):
    guard = load_module()
    operator = FakeOperator()
    source = tmp_path / "pvc"
    source.mkdir()
    operator.pvc_path = lambda _name: source

    with pytest.raises(ValueError, match="must not be inside PVC source"):
        guard.cold_backup(
            operator,
            namespace="caritas",
            release="oriso-platform",
            backup_root=source / "backups",
            timestamp="20260715T151500Z",
        )


def test_backup_cli_requires_an_explicit_maintenance_window_confirmation():
    guard = load_module()

    with pytest.raises(SystemExit):
        guard.parse_args(["backup"])

    args = guard.parse_args(
        [
            "backup",
            "--context",
            "oriso-predev",
            "--expected-node",
            "hassan-dev",
            "--confirm-maintenance-window",
        ]
    )
    assert args.command == "backup"
    assert args.context == "oriso-predev"


def test_kubernetes_operator_pins_every_command_to_the_selected_context(monkeypatch):
    guard = load_module()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="1", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    operator = guard.KubernetesOperator("caritas", "oriso-predev", "hassan-dev")

    assert operator.replicas("deployment", "oriso-platform-otel-collector") == 1
    assert commands == [
        [
            "kubectl",
            "--context",
            "oriso-predev",
            "-n",
            "caritas",
            "get",
            "deployment/oriso-platform-otel-collector",
            "-o",
            "jsonpath={.spec.replicas}",
        ]
    ]


def test_kubernetes_operator_bounds_subprocess_runtime(monkeypatch):
    guard = load_module()

    def timeout(*_args, **_kwargs):
        raise guard.subprocess.TimeoutExpired(cmd="kubectl", timeout=3)

    monkeypatch.setattr(guard.subprocess, "run", timeout)
    operator = guard.KubernetesOperator(
        "caritas", "oriso-predev", "hassan-dev", timeout_seconds=3
    )

    with pytest.raises(TimeoutError, match="kubectl command timed out"):
        operator.replicas("deployment", "oriso-platform-otel-collector")


def test_kubernetes_operator_bounds_helm_subprocess_runtime(monkeypatch):
    guard = load_module()

    def timeout(*_args, **_kwargs):
        raise guard.subprocess.TimeoutExpired(cmd="helm", timeout=3)

    monkeypatch.setattr(guard.subprocess, "run", timeout)
    operator = guard.KubernetesOperator(
        "caritas", "oriso-predev", "hassan-dev", timeout_seconds=3
    )

    with pytest.raises(TimeoutError, match="helm command timed out"):
        operator.helm_release_metadata("oriso-platform")


def test_pvc_path_accepts_hostpath_and_rejects_an_empty_volume_path(tmp_path):
    guard = load_module()
    operator = guard.KubernetesOperator("caritas", "oriso-predev", "hassan-dev")
    responses = iter(
        [
            "pvc-id",
            json.dumps({"spec": {"hostPath": {"path": str(tmp_path)}}}),
        ]
    )
    operator._run = lambda *_args: next(responses)

    assert operator.pvc_path("clickhouse-data") == tmp_path.resolve()
