import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "observability" / "livechat-stabilization"

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


def load_yaml(name: str):
    return yaml.safe_load((OBSERVABILITY / name).read_text(encoding="utf-8"))


def test_every_repaired_boundary_has_a_privacy_reviewed_signal():
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


def test_dashboard_separates_the_required_failure_causes():
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


def test_alerts_cover_urgent_failures_without_sensitive_dimensions():
    alerts = load_yaml("alerts.yaml")["alerts"]
    assert REQUIRED_ALERTS <= {alert["id"] for alert in alerts}
    for alert in alerts:
        assert alert["query"]
        assert alert["for"]
        assert not (set(alert.get("groupBy", [])) & FORBIDDEN_ATTRIBUTES)


def test_runbook_maps_every_required_cause_to_a_query():
    runbook = (OBSERVABILITY / "RUNBOOK.md").read_text(encoding="utf-8")
    for cause in REQUIRED_CAUSES:
        assert f"`{cause}`" in runbook
