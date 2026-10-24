"""Contract for the live-chat observability specification (ORISO-Helm#117).

The files under observability/livechat-stabilization/ are build
specifications, not native SigNoz exports. The provisioned, native dashboards
and alerts live in files/signoz/observability-catalog.json; the last test class
keeps the two from drifting apart.
"""

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "observability" / "livechat-stabilization"
CATALOG = ROOT / "files" / "signoz" / "observability-catalog.json"

REQUIRED_BOUNDARIES = {
    "admin.provisioning",
    "live_chat.availability.store",
    "live_chat.routing",
    "live_chat.queue",
    "matrix.room.creation",
    "matrix.event.processing",
    "matrix.notification.side_effect",
    "runtime.identity",
}
REQUIRED_CAUSES = {
    "no_demand",
    "no_eligible_consultant",
    "expired_availability",
    "relation_failure",
    "matrix_processing_failure",
    "unencrypted_room_creation",
}
REQUIRED_ALERTS = {
    "provisioning_rollback_failure",
    "redis_availability_store_failure",
    "matrix_notification_exception_rate",
    "unencrypted_room_created",
}
FORBIDDEN_ATTRIBUTES = {
    "message.body",
    "message.preview",
    "access_token",
    "mobile_token",
    "email",
    "username",
    "matrix_access_token",
}
REQUIRED_SERVICE_IDS = {
    "frontend",
    "admin",
    "user-service",
    "agency-service",
    "tenant-service",
    "consulting-type-service",
    "matrix-synapse",
    "redis",
    "mariadb",
    "mongodb",
    "keycloak",
    "signoz-clickhouse",
}
REQUIRED_MANAGEMENT_PANELS = {
    "live-chat-outcome",
    "provisioning-integrity",
    "e2ee-posture",
    "service-slo",
    "replica-safety",
    "stateful-platform-health",
    "deployment-drift",
    "telemetry-pipeline-health",
}


def load_yaml(name: str):
    return yaml.safe_load((OBSERVABILITY / name).read_text(encoding="utf-8"))


class LivechatObservabilityContractTest(unittest.TestCase):
    def test_every_repaired_boundary_has_a_privacy_reviewed_signal(self):
        catalog = load_yaml("telemetry-catalog.yaml")
        by_boundary = {signal["boundary"]: signal for signal in catalog["signals"]}

        assert REQUIRED_BOUNDARIES <= by_boundary.keys()
        for boundary in REQUIRED_BOUNDARIES:
            signal = by_boundary[boundary]
            assert signal["success"]
            assert signal["failure"]
            assert signal["cardinality"] in {"low", "bounded"}
            assert signal["privacyDecision"]
            assert not (set(signal.get("attributes", [])) & FORBIDDEN_ATTRIBUTES)

    def test_dashboard_separates_the_required_failure_causes(self):
        dashboard = json.loads(
            (OBSERVABILITY / "dashboard-livechat-diagnostics.json").read_text(
                encoding="utf-8"
            )
        )
        assert dashboard["artifactType"] == "signoz-dashboard-build-specification"
        causes = {
            cause
            for panel in dashboard["panels"]
            for cause in panel.get("diagnoses", [])
        }
        assert REQUIRED_CAUSES <= causes

    def test_alerts_cover_urgent_failures_without_sensitive_dimensions(self):
        alerts = load_yaml("alerts.yaml")["alerts"]
        assert REQUIRED_ALERTS <= {alert["id"] for alert in alerts}
        for alert in alerts:
            assert alert["query"]
            assert alert["for"]
            assert not (set(alert.get("groupBy", [])) & FORBIDDEN_ATTRIBUTES)

    def test_runbook_maps_every_required_cause_to_a_query(self):
        runbook = (OBSERVABILITY / "RUNBOOK.md").read_text(encoding="utf-8")
        for cause in REQUIRED_CAUSES:
            assert f"`{cause}`" in runbook

    def test_service_state_map_separates_workload_kind_from_verified_statelessness(
        self,
    ):
        state_map = load_yaml("service-state-map.yaml")
        services = {service["id"]: service for service in state_map["services"]}

        assert REQUIRED_SERVICE_IDS <= services.keys()
        assert (
            services["user-service"]["stateClass"] == "stateless-target-replica-coupled"
        )
        for service in services.values():
            assert service["workloadKind"] in {"Deployment", "StatefulSet"}
            assert service["stateClass"] in {
                "stateless-verified",
                "stateless-target-replica-coupled",
                "stateful-platform",
            }
            assert service["evidence"]
            assert service["qualitySignals"]

    def test_management_dashboard_has_owned_before_after_quality_panels(self):
        dashboard = json.loads(
            (OBSERVABILITY / "dashboard-management-quality.json").read_text(
                encoding="utf-8"
            )
        )
        assert dashboard["artifactType"] == "signoz-dashboard-build-specification"
        panels = {panel["id"]: panel for panel in dashboard["panels"]}
        assert REQUIRED_MANAGEMENT_PANELS <= panels.keys()
        for panel in panels.values():
            assert panel["managementQuestion"]
            assert panel["owner"]
            assert panel["beforeAfter"]["baseline"]
            assert panel["beforeAfter"]["target"]
            assert panel["signalStatus"] in {"live", "partial", "blocked-on-emitter"}

    def test_baseline_names_measurement_gaps_instead_of_claiming_success(self):
        baseline = load_yaml("baseline-2026-07-22.yaml")
        assert baseline["environmentEvidence"]["preDev"]["metricsIngesting"] is True
        assert baseline["environmentEvidence"]["dev"]["metricsIngesting"] is False
        assert "runtime identity signals are not emitted" in baseline["limitations"]
        assert (
            "live-chat diagnostic emitters are not emitted" in baseline["limitations"]
        )

    def test_tracking_event_decisions_capture_reason_privacy_and_cardinality(self):
        decision_log = (OBSERVABILITY / "TRACKING-EVENT-DECISIONS.md").read_text(
            encoding="utf-8"
        )
        assert decision_log.count("## 2026-07-22") >= 4
        for required in ("Decision:", "Why:", "Privacy:", "Cardinality:"):
            assert required in decision_log


# Specification alert id -> provisioned catalog alert slug.
SPEC_TO_CATALOG_ALERTS = {
    "provisioning_rollback_failure": "provisioning-compensation-failure",
    "redis_availability_store_failure": "availability-store-failure",
    "matrix_notification_exception_rate": "notification-failure",
    "unencrypted_room_created": "unencrypted-room",
    "clickhouse_capacity_warning": "clickhouse-capacity",
}


class ProvisionedCatalogCoversSpecificationTest(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    def test_live_chat_dashboard_provisions_every_live_chat_and_matrix_signal(self):
        spec = json.loads(
            (OBSERVABILITY / "dashboard-livechat-diagnostics.json").read_text(
                encoding="utf-8"
            )
        )
        spec_signals = {
            panel["signal"]
            for panel in spec["panels"]
            if panel["signal"].startswith(("oriso.live_chat.", "oriso.matrix."))
        }
        live_chat = next(
            item
            for item in self.catalog["dashboards"]
            if item["slug"] == "oriso-live-chat"
        )
        provisioned = {panel["metric"] for panel in live_chat["panels"]}
        self.assertTrue(spec_signals)
        self.assertLessEqual(spec_signals, provisioned)

    def test_every_urgent_specification_alert_is_provisioned(self):
        slugs = {item["slug"] for item in self.catalog["alerts"]}
        self.assertLessEqual(REQUIRED_ALERTS, SPEC_TO_CATALOG_ALERTS.keys())
        for spec_id, slug in SPEC_TO_CATALOG_ALERTS.items():
            self.assertIn(slug, slugs, spec_id)

    def test_provisioned_grouping_never_uses_a_forbidden_attribute(self):
        for item in self.catalog["dashboards"]:
            for panel in item["panels"]:
                self.assertFalse(
                    set(panel.get("groupBy", [])) & FORBIDDEN_ATTRIBUTES,
                    panel["id"],
                )


if __name__ == "__main__":
    unittest.main()
