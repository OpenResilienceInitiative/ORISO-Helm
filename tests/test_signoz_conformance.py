from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from signoz_conformance import evaluate_snapshot  # noqa: E402


class SignozConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = yaml.safe_load(
            (ROOT / "observability/signoz-conformance/contract.yaml").read_text()
        )
        services = self.contract["signals"]["requiredServices"]
        vitals = self.contract["webVitals"]
        self.healthy = {
            "signals": {
                signal: {
                    service: {"ageMinutes": 1, "environment": "pre-dev"}
                    for service in services
                }
                for signal in ("logs", "metrics", "traces")
            },
            "correlatedLogs": {service: 2 for service in services},
            "webVitals": {
                service: {metric: 1 for metric in vitals["metrics"]}
                for service in vitals["services"]
            },
            "dashboards": {
                item["id"]: 1 for item in self.contract["dashboards"]
            },
            "alerts": {
                alert: {"enabled": True, "routeTestAgeHours": 1}
                for alert in self.contract["alerts"]["required"]
            },
            "alertReadbackAvailable": True,
            "internalLogTtlDays": {
                table: 3 for table in self.contract["clickhouse"]["internalLogTables"]
            },
            "diskUsedPercent": 20,
            "internalLogsGiB": 2,
            "backupAgeHours": 1,
            "backupStatus": "BACKUP_CREATED",
        }

    def test_healthy_snapshot_passes(self) -> None:
        self.assertEqual(evaluate_snapshot(self.contract, self.healthy), [])

    def test_missing_identity_freshness_route_ttl_capacity_and_backup_fail(self) -> None:
        self.healthy["signals"]["logs"]["user-service"]["environment"] = ""
        self.healthy["signals"]["traces"]["agency-service"]["ageMinutes"] = 99
        self.healthy["correlatedLogs"]["tenant-service"] = 0
        self.healthy["webVitals"]["frontend"]["lcp"] = 120
        self.healthy["dashboards"]["clickhouse-capacity"] = 0
        self.healthy["alerts"]["deployment_identity_mismatch"]["enabled"] = False
        self.healthy["alerts"]["clickhouse_capacity_warning"][
            "routeTestAgeHours"
        ] = 99
        self.healthy["internalLogTtlDays"]["trace_log"] = 0
        self.healthy["diskUsedPercent"] = 90
        self.healthy["internalLogsGiB"] = 30
        self.healthy["backupAgeHours"] = 99

        failures = "\n".join(evaluate_snapshot(self.contract, self.healthy))

        for expected in (
            "logs/user-service",
            "traces/agency-service",
            "correlation/tenant-service",
            "web-vitals/frontend/lcp",
            "dashboard/clickhouse-capacity",
            "alert/deployment_identity_mismatch",
            "route/clickhouse_capacity_warning",
            "ttl/trace_log",
            "capacity/disk",
            "capacity/internal-logs",
            "backup/freshness",
        ):
            self.assertIn(expected, failures)


if __name__ == "__main__":
    unittest.main()
