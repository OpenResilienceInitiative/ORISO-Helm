#!/usr/bin/env python3
"""Read-only SigNoz/ClickHouse conformance gate for ORISO PreDev."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml


def evaluate_snapshot(contract: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    expected_environment = contract["deploymentEnvironment"]
    max_signal_age = contract["signals"]["maxAgeMinutes"]

    for signal in ("logs", "metrics", "traces"):
        observed = snapshot.get("signals", {}).get(signal, {})
        for service in contract["signals"]["requiredServices"]:
            item = observed.get(service)
            prefix = f"{signal}/{service}"
            if not item:
                failures.append(f"{prefix}: no live data")
                continue
            if item.get("environment") != expected_environment:
                failures.append(
                    f"{prefix}: deployment.environment is "
                    f"{item.get('environment')!r}, expected {expected_environment!r}"
                )
            if item.get("ageMinutes", float("inf")) > max_signal_age:
                failures.append(
                    f"{prefix}: freshest data is {item.get('ageMinutes')} minutes old"
                )

    if contract["correlation"]["requireLogToTrace"]:
        minimum = contract["correlation"]["minimumCorrelatedLogsPerService"]
        for service in contract["signals"]["requiredServices"]:
            count = snapshot.get("correlatedLogs", {}).get(service, 0)
            if count < minimum:
                failures.append(
                    f"correlation/{service}: {count} correlated logs, need {minimum}"
                )

    web_vitals = contract["webVitals"]
    observed_vitals = snapshot.get("webVitals", {})
    for service in web_vitals["services"]:
        for metric in web_vitals["metrics"]:
            age = observed_vitals.get(service, {}).get(metric, float("inf"))
            if age > web_vitals["maxAgeMinutes"]:
                failures.append(
                    f"web-vitals/{service}/{metric}: freshest data is {age} minutes old"
                )

    for dashboard in contract["dashboards"]:
        rows = snapshot.get("dashboards", {}).get(dashboard["id"], 0)
        if rows <= 0:
            failures.append(f"dashboard/{dashboard['id']}: live query returned no data")

    alert_contract = contract["alerts"]
    observed_alerts = snapshot.get("alerts", {})
    if not snapshot.get("alertReadbackAvailable", False):
        failures.append(
            "alerts/readback: authenticated SigNoz rule readback is unavailable"
        )
    else:
        for alert_id in alert_contract["required"]:
            alert = observed_alerts.get(alert_id)
            if not alert or not alert.get("enabled"):
                failures.append(f"alert/{alert_id}: required enabled rule is missing")
                continue
            route_age = alert.get("routeTestAgeHours", float("inf"))
            if (
                alert_contract["routeTestRequired"]
                and route_age > alert_contract["routeTestMaxAgeHours"]
            ):
                failures.append(
                    f"route/{alert_id}: route proof is {route_age} hours old or missing"
                )

    expected_ttl = contract["clickhouse"]["internalLogRetentionDays"]
    observed_ttl = snapshot.get("internalLogTtlDays", {})
    for table in contract["clickhouse"]["internalLogTables"]:
        if observed_ttl.get(table) != expected_ttl:
            failures.append(
                f"ttl/{table}: {observed_ttl.get(table, 0)} days, "
                f"expected {expected_ttl}"
            )

    if snapshot.get("diskUsedPercent", 100) > contract["capacity"][
        "maxDiskUsedPercent"
    ]:
        failures.append(
            f"capacity/disk: {snapshot.get('diskUsedPercent')}% used exceeds "
            f"{contract['capacity']['maxDiskUsedPercent']}%"
        )
    if snapshot.get("internalLogsGiB", float("inf")) > contract["capacity"][
        "maxInternalLogsGiB"
    ]:
        failures.append(
            f"capacity/internal-logs: {snapshot.get('internalLogsGiB')} GiB exceeds "
            f"{contract['capacity']['maxInternalLogsGiB']} GiB"
        )

    if contract["backup"]["required"]:
        if snapshot.get("backupStatus") not in contract["backup"]["acceptedStatuses"]:
            failures.append(
                f"backup/status: {snapshot.get('backupStatus')!r} is not accepted"
            )
        if snapshot.get("backupAgeHours", float("inf")) > contract["backup"][
            "maxAgeHours"
        ]:
            failures.append(
                f"backup/freshness: latest backup is "
                f"{snapshot.get('backupAgeHours', 'missing')} hours old"
            )
    return failures


class ClickHouse:
    def __init__(
        self, namespace: str, release: str, ssh_host: str | None = None
    ) -> None:
        self.namespace = namespace
        self.release = release
        self.ssh_host = ssh_host
        pod_result = self._run_kubectl(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "pod",
                "-l",
                "app=clickhouse",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ]
        )
        self.pod = pod_result.stdout.strip()
        if not self.pod:
            raise RuntimeError("ClickHouse pod was not found")

    def query(self, sql: str) -> list[dict[str, Any]]:
        result = self._run_kubectl(
            [
                "kubectl",
                "-n",
                self.namespace,
                "exec",
                self.pod,
                "--",
                "clickhouse-client",
                "--format",
                "JSONEachRow",
                "--query",
                sql,
            ]
        )
        return [json.loads(line) for line in result.stdout.splitlines() if line]

    def _run_kubectl(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if self.ssh_host:
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.ssh_host,
                shlex.join(command),
            ]
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=True,
        )


def _service_snapshot(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row["service"]): {
            "environment": str(row.get("environment", "")),
            "ageMinutes": int(row["ageMinutes"]),
        }
        for row in rows
        if row.get("service")
    }


def _scalar_count(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    value = next(iter(rows[0].values()))
    return int(value)


def _extract_ttl_days(engine: str) -> int:
    for pattern in (
        r"INTERVAL\s+(\d+)\s+DAY",
        r"toIntervalDay\((\d+)\)",
    ):
        match = re.search(pattern, engine, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _walk_rule_objects(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("alert", "ruleId", "rule_id")):
            found.append(value)
        for child in value.values():
            found.extend(_walk_rule_objects(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_rule_objects(child))
    return found


def _load_alerts(
    signoz_url: str, api_key: str | None, route_evidence: Path | None
) -> tuple[dict[str, dict[str, Any]], bool]:
    alerts: dict[str, dict[str, Any]] = {}
    if api_key:
        request = urllib.request.Request(
            f"{signoz_url.rstrip('/')}/api/v2/rules",
            headers={"SIGNOZ-API-KEY": api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        for rule in _walk_rule_objects(payload):
            name = rule.get("alert") or rule.get("name")
            if name:
                alerts[str(name)] = {"enabled": not bool(rule.get("disabled", False))}

    if route_evidence and route_evidence.exists():
        evidence = yaml.safe_load(route_evidence.read_text()) or {}
        now = dt.datetime.now(dt.UTC)
        for item in evidence.get("deliveries", []):
            alert_id = item["alertId"]
            delivered = dt.datetime.fromisoformat(item["deliveredAt"].replace("Z", "+00:00"))
            alerts.setdefault(alert_id, {"enabled": False})["routeTestAgeHours"] = round(
                (now - delivered).total_seconds() / 3600, 2
            )
    return alerts, bool(api_key)


def collect_snapshot(
    contract: dict[str, Any],
    clickhouse: ClickHouse,
    signoz_url: str,
    api_key: str | None,
    route_evidence: Path | None,
) -> dict[str, Any]:
    signal_queries = {
        "logs": """
            SELECT resources_string['service.name'] AS service,
                   resources_string['deployment.environment'] AS environment,
                   dateDiff('minute', max(toDateTime(timestamp / 1000000000)), now()) AS ageMinutes
            FROM signoz_logs.logs_v2
            WHERE timestamp > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000000000
            GROUP BY service, environment
        """,
        "traces": """
            SELECT resources_string['service.name'] AS service,
                   resources_string['deployment.environment'] AS environment,
                   dateDiff('minute', max(timestamp), now()) AS ageMinutes
            FROM signoz_traces.signoz_index_v3
            WHERE timestamp > now() - INTERVAL 24 HOUR
            GROUP BY service, environment
        """,
        "metrics": """
            SELECT resource_attrs['service.name'] AS service,
                   resource_attrs['deployment.environment'] AS environment,
                   dateDiff('minute', toDateTime(max(inserted_at_unix_milli) / 1000), now()) AS ageMinutes
            FROM signoz_metrics.time_series_v4
            WHERE inserted_at_unix_milli > toUnixTimestamp64Milli(now64() - INTERVAL 24 HOUR)
            GROUP BY service, environment
        """,
    }
    snapshot: dict[str, Any] = {
        "signals": {
            signal: _service_snapshot(clickhouse.query(query))
            for signal, query in signal_queries.items()
        }
    }

    correlation_rows = clickhouse.query(
        f"""
        SELECT l.resources_string['service.name'] AS service, count() AS correlated
        FROM signoz_logs.logs_v2 AS l
        INNER JOIN signoz_traces.signoz_index_v3 AS t ON l.trace_id = t.trace_id
        WHERE l.timestamp > toUnixTimestamp(now() - INTERVAL
          {int(contract['correlation']['lookbackMinutes'])} MINUTE) * 1000000000
          AND l.trace_id != ''
        GROUP BY service
        """
    )
    snapshot["correlatedLogs"] = {
        str(row["service"]): int(row["correlated"]) for row in correlation_rows
    }

    vital_names = ",".join(
        f"'{name}'" for name in contract["webVitals"]["metrics"]
    )
    vital_rows = clickhouse.query(
        f"""
        SELECT resource_attrs['service.name'] AS service,
               splitByChar('.', metric_name)[1] AS metric,
               dateDiff('minute', toDateTime(max(inserted_at_unix_milli) / 1000), now()) AS ageMinutes
        FROM signoz_metrics.time_series_v4
        WHERE splitByChar('.', metric_name)[1] IN ({vital_names})
        GROUP BY service, metric
        """
    )
    snapshot["webVitals"] = {}
    for row in vital_rows:
        snapshot["webVitals"].setdefault(str(row["service"]), {})[
            str(row["metric"])
        ] = int(row["ageMinutes"])

    snapshot["dashboards"] = {
        dashboard["id"]: _scalar_count(clickhouse.query(dashboard["liveQuery"]))
        for dashboard in contract["dashboards"]
    }
    snapshot["alerts"], snapshot["alertReadbackAvailable"] = _load_alerts(
        signoz_url, api_key=api_key, route_evidence=route_evidence
    )

    ttl_rows = clickhouse.query(
        """
        SELECT name, engine_full
        FROM system.tables
        WHERE database = 'system'
          AND name IN ('query_log','text_log','trace_log','metric_log','asynchronous_metric_log')
        """
    )
    snapshot["internalLogTtlDays"] = {
        str(row["name"]): _extract_ttl_days(str(row["engine_full"]))
        for row in ttl_rows
    }

    disk = clickhouse.query(
        """
        SELECT round((sum(total_space) - sum(free_space)) / sum(total_space) * 100, 2)
          AS diskUsedPercent
        FROM system.disks
        """
    )
    snapshot["diskUsedPercent"] = float(disk[0]["diskUsedPercent"])
    internal = clickhouse.query(
        """
        SELECT round(sum(bytes_on_disk) / 1024 / 1024 / 1024, 2) AS internalLogsGiB
        FROM system.parts
        WHERE active AND database = 'system'
          AND match(table, '^(query_log|text_log|trace_log|metric_log|asynchronous_metric_log)(_[0-9]+)?$')
        """
    )
    snapshot["internalLogsGiB"] = float(internal[0]["internalLogsGiB"])

    try:
        backups = clickhouse.query(
            """
            SELECT status AS backupStatus,
                   dateDiff('hour', end_time, now()) AS backupAgeHours
            FROM system.backups
            WHERE status IN ('BACKUP_CREATED','RESTORED')
            ORDER BY end_time DESC
            LIMIT 1
            """
        )
    except subprocess.CalledProcessError:
        backups = []
    if backups:
        snapshot.update(backups[0])
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--namespace", default="caritas")
    parser.add_argument("--release", default="oriso-platform")
    parser.add_argument(
        "--ssh-host",
        help="Run kubectl through this SSH target, for example root@predev-host",
    )
    parser.add_argument("--signoz-url", required=True)
    parser.add_argument("--route-evidence", type=Path)
    parser.add_argument("--snapshot-out", type=Path)
    args = parser.parse_args()

    contract = yaml.safe_load(args.contract.read_text())
    snapshot = collect_snapshot(
        contract,
        ClickHouse(args.namespace, args.release, ssh_host=args.ssh_host),
        args.signoz_url,
        os.environ.get("SIGNOZ_API_KEY"),
        args.route_evidence,
    )
    if args.snapshot_out:
        args.snapshot_out.write_text(yaml.safe_dump(snapshot, sort_keys=True))

    failures = evaluate_snapshot(contract, snapshot)
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        print(f"FAIL SigNoz conformance: {len(failures)} requirement(s) unmet")
        return 1
    print("PASS SigNoz conformance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
