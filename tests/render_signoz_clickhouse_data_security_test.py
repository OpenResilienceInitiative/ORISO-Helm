#!/usr/bin/env python3
"""Protect the ClickHouse data Pods reconciled by the Altinity operator."""

from render_signoz_dev_observability_test import by_key, render

INSTALLATION = ("ClickHouseInstallation", "caritas-clickhouse")
LOG_PATH = "/var/log/clickhouse-server"
CONTEXT = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}


def installation(*settings):
    flags = tuple(argument for setting in settings for argument in ("--set", setting))
    return by_key(render("values-dev.yaml", helm_args=flags))[INSTALLATION]["spec"]


def pod_spec(spec):
    return spec["templates"]["podTemplates"][0]["spec"]


def mounts(container):
    return {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}


def main():
    spec = installation()
    pod = pod_spec(spec)
    assert pod["securityContext"] == {
        "runAsUser": 101,
        "runAsGroup": 101,
        "fsGroup": 101,
        "fsGroupChangePolicy": "OnRootMismatch",
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod["automountServiceAccountToken"] is False

    server, = pod["containers"]
    udf, = pod["initContainers"]
    assert server["securityContext"] == udf["securityContext"] == CONTEXT
    assert spec["defaults"]["templates"]["dataVolumeClaimTemplate"] == "data-volumeclaim-template"
    assert spec["templates"]["volumeClaimTemplates"][0]["reclaimPolicy"] == "Retain"
    assert mounts(server)["/var/lib/clickhouse"] == "data-volumeclaim-template"
    assert mounts(server)["/var/lib/clickhouse/user_scripts"] == mounts(udf)["/var/lib/clickhouse/user_scripts"]
    assert mounts(server)[LOG_PATH] == "clickhouse-runtime-logs"
    assert mounts(udf)["/tmp"] == "clickhouse-udf-tmp"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    for name in ("clickhouse-runtime-logs", "clickhouse-udf-tmp"):
        assert volumes[name]["emptyDir"] == {}

    optional = pod_spec(installation(
        "signoz.clickhouse.initContainers.init.enabled=true",
        "signoz.clickhouse.logs.system.enabled=true",
        "signoz.clickhouse.automountServiceAccountToken=true",
    ))
    assert optional["automountServiceAccountToken"] is True
    exporter, = [item for item in optional["containers"] if item["name"] == "logs-system-exporter"]
    initializer, = [item for item in optional["initContainers"] if item["name"] == "caritas-clickhouse-init"]
    assert exporter["securityContext"] == initializer["securityContext"] == CONTEXT

    custom = pod_spec(installation(
        "signoz.clickhouse.additionalVolumes[0].name=operator-logs",
        "signoz.clickhouse.additionalVolumes[0].emptyDir.sizeLimit=64Mi",
        "signoz.clickhouse.additionalVolumeMounts[0].name=operator-logs",
        f"signoz.clickhouse.additionalVolumeMounts[0].mountPath={LOG_PATH}",
        "signoz.clickhouse.initContainers.udf.enabled=false",
    ))
    log_mounts = [
        mount["name"]
        for mount in custom["containers"][0]["volumeMounts"]
        if mount["mountPath"] == LOG_PATH
    ]
    assert log_mounts == ["operator-logs"]
    volume_names = {volume["name"] for volume in custom["volumes"]}
    assert volume_names.isdisjoint({"clickhouse-runtime-logs", "clickhouse-udf-tmp"})

    writable = pod_spec(installation("signoz.clickhouse.containerSecurityContext.readOnlyRootFilesystem=false"))
    assert writable["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is False
    assert LOG_PATH not in mounts(writable["containers"][0])
    assert "/tmp" not in mounts(writable["initContainers"][0])
    print("OK: ClickHouse data Pod security, existing storage and optional mounts")


if __name__ == "__main__":
    main()
