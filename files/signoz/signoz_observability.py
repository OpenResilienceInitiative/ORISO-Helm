#!/usr/bin/env python3
"""Provision and prove ORISO-managed SigNoz observability assets.

The implementation uses only the Python standard library so the Helm hook has
no runtime package installation. It never sends application content or
protected identifiers to notification routes, and it never creates or changes
notification channels: the Slack channel is made by hand in SigNoz and only
referenced here by name.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ENVIRONMENT_TOKEN = "__ORISO_ENVIRONMENT__"
ENVIRONMENT_SLUG_TOKEN = "__ORISO_ENVIRONMENT_SLUG__"
CLUSTER_TOKEN = "__ORISO_CLUSTER_NAME__"
CHANNEL_TOKEN = "__ORISO_CHANNEL_NAME__"
DEPLOYMENT_IDENTITY_SLUG = "deployment-identity-mismatch"

BROWSER_VITALS_SERVICES = ("frontend", "admin")
# SigNoz stores histogram observations under .count and the CLS gauge under cls.
BROWSER_VITALS_METRICS = {
    "lcp": "lcp.count",
    "cls": "cls",
    "inp": "inp.count",
    "ttfb": "ttfb.count",
    "fcp": "fcp.count",
}
BROWSER_VITALS_LOOKBACK_MS = 30 * 60_000

REQUIRED_DASHBOARD_SLUGS = {
    "oriso-live-chat",
    "oriso-service-reliability",
    "oriso-platform-health",
}
# Opt-in dashboards over the oriso-e2e test spans (ORISO-E2E#97). They are only
# provisioned with ORISO_E2E_DASHBOARDS=true and are all-or-nothing.
E2E_DASHBOARD_SLUGS = {
    "oriso-e2e-test-health",
    "oriso-e2e-flaky",
}
E2E_SERVICE = "oriso-e2e"
REQUIRED_ALERT_SLUGS = {
    "provisioning-compensation-failure",
    "availability-store-failure",
    "notification-failure",
    "unencrypted-room",
    "clickhouse-capacity",
    "collector-staleness",
}
ALLOWED_GROUPS = {
    "condition",
    "demand",
    "dependency",
    "encryption",
    "event_type",
    "k8s.namespace.name",
    "operation",
    "outcome",
    "resource",
    "service.name",
    "side_effect",
    "stage",
    "status",
    "test.case",
    "test.commit",
    "test.file",
    "test.id",
    "test.project",
    "test.repository",
    "test.run.id",
    "test.status",
    "workflow",
}
CI_TEST_LOG_FILTER = (
    "service.name = 'oriso-ci-tests' AND oriso.signal.source = 'ci'"
    " AND test.evidence.origin = 'github-actions'"
    " AND test.status IN ('passed', 'failed') AND test.evidence.observed_at_ms > 0"
    " AND test.id EXISTS AND test.id != '' AND test.job.id EXISTS AND test.job.id != ''"
    " AND ((test.repository = 'OpenResilienceInitiative/ORISO-Frontend'"
    " AND test.framework = 'vitest' AND test.project IN ('unit', 'storybook (chromium)'))"
    " OR (test.repository = 'OpenResilienceInitiative/ORISO-Admin'"
    " AND test.framework = 'vitest' AND test.project = 'unit')"
    " OR (test.repository = 'OpenResilienceInitiative/ORISO-UserService'"
    " AND test.framework = 'surefire' AND test.project = 'unit')"
    " OR (test.repository = 'OpenResilienceInitiative/ORISO-E2E'"
    " AND test.framework = 'playwright' AND test.project = 'dev'"
    " AND test.file = 'tests/smoke-crash.spec.ts'))"
)
FORBIDDEN_TERMS = {
    "access_token",
    "client.address",
    "email",
    "host.ip",
    "k8s.pod.ip",
    "message.body",
    "message.content",
    "net.peer.ip",
    "password",
    "room.id",
    "room_id",
    "source.address",
    "user.id",
    "user_id",
    "username",
}


def _scope_filter(definition: dict[str, Any]) -> str:
    if definition.get("signal") == "logs":
        selector = definition.get("filter")
        return f"{CI_TEST_LOG_FILTER} AND ({selector})" if selector else CI_TEST_LOG_FILTER
    parts = [f"deployment.environment = '{ENVIRONMENT_TOKEN}'"]
    if definition.get("clusterScoped"):
        parts.append(f"k8s.cluster.name = '{CLUSTER_TOKEN}'")
    if definition.get("filter"):
        parts.append(str(definition["filter"]))
    return " AND ".join(parts)


def _group_by(names: Iterable[str], context: str = "attribute") -> list[dict[str, str]]:
    return [
        {"name": name, "fieldContext": context, "fieldDataType": "string"}
        for name in names
    ]


def _event_query(definition: dict[str, Any]) -> dict[str, Any]:
    """Builder query over test spans or CI result logs."""
    query: dict[str, Any] = {
        "name": "A",
        "signal": definition["signal"],
        "source": "",
        "stepInterval": definition.get("stepInterval", 60),
        "aggregations": [
            {key: value for key, value in aggregation.items() if value}
            for aggregation in definition["aggregations"]
        ],
        "disabled": False,
        "filter": {"expression": _scope_filter(definition)},
        "groupBy": _group_by(definition.get("groupBy", []), "attribute"),
        "having": {"expression": definition.get("having", "")},
        "legend": "",
    }
    if definition.get("limit"):
        query["limit"] = definition["limit"]
    return query


def _builder_query(definition: dict[str, Any]) -> dict[str, Any]:
    if definition.get("signal") in {"traces", "logs"}:
        return _event_query(definition)
    return {
        "name": "A",
        "signal": "metrics",
        "source": "",
        "stepInterval": 60,
        "aggregations": [
            {
                "metricName": definition["metric"],
                "temporality": "",
                "timeAggregation": definition["timeAggregation"],
                "spaceAggregation": definition["spaceAggregation"],
            }
        ],
        "disabled": False,
        "filter": {"expression": _scope_filter(definition)},
        "groupBy": _group_by(definition.get("groupBy", [])),
        "having": {"expression": ""},
        "legend": "",
    }


def _dashboard(definition: dict[str, Any]) -> dict[str, Any]:
    panels: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    for index, panel in enumerate(definition["panels"]):
        panel_id = panel["id"]
        plugin_spec = {}
        if "columnUnits" in panel:
            plugin_spec["formatting"] = {"columnUnits": panel["columnUnits"]}
        panels[panel_id] = {
            "kind": "Panel",
            "spec": {
                "display": {
                    "name": panel["title"],
                    "description": panel.get("description", ""),
                },
                "plugin": {
                    "kind": "signoz/TablePanel"
                    if panel.get("panelType") == "table"
                    else "signoz/TimeSeriesPanel",
                    "spec": plugin_spec,
                },
                "links": [],
                "queries": [
                    {
                        "kind": "scalar"
                        if panel.get("panelType") == "table"
                        else "time_series",
                        "spec": {
                            "plugin": {
                                "kind": "signoz/BuilderQuery",
                                "spec": _builder_query(panel),
                            }
                        },
                    }
                ],
            },
        }
        items.append(
            {
                "x": 0 if index % 2 == 0 else 6,
                "y": (index // 2) * 6,
                "width": 6,
                "height": 6,
                "content": {"$ref": f"#/spec/panels/{panel_id}"},
            }
        )

    return {
        "schemaVersion": "v6",
        "name": f"{definition['slug']}-{ENVIRONMENT_SLUG_TOKEN}",
        "generateName": False,
        "tags": [
            {"key": "managed-by", "value": "oriso-helm"},
            {"key": "environment", "value": ENVIRONMENT_TOKEN},
        ],
        "spec": {
            "display": {
                "name": f"{definition['title']} [{ENVIRONMENT_TOKEN}]",
                "description": definition["description"],
            },
            "duration": definition.get("duration", "1h"),
            "refreshInterval": "1m",
            "variables": [],
            "panels": panels,
            "layouts": [{"kind": "Grid", "spec": {"items": items}}],
            "links": [],
        },
    }


def _alert(definition: dict[str, Any]) -> dict[str, Any]:
    renotify_states = ["firing"]
    absence_condition = {}
    if definition.get("notifyOnNoData"):
        renotify_states.append("nodata")
        # SigNoz measures absence in minutes since the last evaluation with data.
        absence_condition = {"alertOnAbsent": True, "absentFor": 1}
    query = _builder_query(definition)
    query.pop("groupBy", None)
    return {
        "alert": f"ORISO {ENVIRONMENT_TOKEN} | {definition['title']}",
        "alertType": "METRIC_BASED_ALERT",
        "description": definition["description"],
        "ruleType": "threshold_rule",
        "schemaVersion": "v2alpha1",
        "version": "v5",
        "disabled": False,
        "source": "oriso-helm",
        # Ignored by SigNoz for schemaVersion v2alpha1, which routes through
        # condition.thresholds[].channels; kept so the target is obvious.
        "preferredChannels": [CHANNEL_TOKEN],
        "labels": {
            "environment": ENVIRONMENT_TOKEN,
            "managed_by": "oriso_helm",
            "severity": definition["severity"],
        },
        "annotations": {
            "summary": f"{definition['title']} in {ENVIRONMENT_TOKEN}",
            "description": definition["description"],
        },
        "evaluation": {
            "kind": "rolling",
            "spec": {"evalWindow": "5m", "frequency": "1m"},
        },
        "notificationSettings": {
            "groupBy": ["environment"],
            "renotify": {
                "enabled": True,
                "interval": "2h",
                "alertStates": renotify_states,
            },
            "usePolicy": False,
        },
        "condition": {
            **absence_condition,
            "selectedQueryName": "A",
            "compositeQuery": {
                "panelType": "graph",
                "queryType": "builder",
                "queries": [{"type": "builder_query", "spec": query}],
            },
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": "critical",
                        "target": definition["target"],
                        "targetUnit": "",
                        "recoveryTarget": None,
                        "matchType": "at_least_once",
                        "op": definition["op"],
                        "channels": [CHANNEL_TOKEN],
                    }
                ],
            },
        },
        "_orisoSlug": definition["slug"],
    }


def load_assets(
    asset_dir: str | Path, *, deployment_identity: bool | None = None, include_e2e: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    catalog_path = Path(asset_dir) / "observability-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if {item["slug"] for item in catalog["dashboards"]} != REQUIRED_DASHBOARD_SLUGS:
        raise RuntimeError("SigNoz dashboard catalog is incomplete")
    if {item["slug"] for item in catalog["alerts"]} != REQUIRED_ALERT_SLUGS:
        raise RuntimeError("SigNoz alert catalog is incomplete")
    definitions = list(catalog["dashboards"])
    if include_e2e:
        e2e = catalog.get("e2eDashboards", [])
        if {item["slug"] for item in e2e} != E2E_DASHBOARD_SLUGS:
            raise RuntimeError("SigNoz E2E dashboard catalog is incomplete")
        definitions += e2e
    alerts = [_alert(item) for item in catalog["alerts"]]
    if deployment_identity is not None:
        definition = catalog.get("deploymentIdentityAlert")
        if isinstance(definition, dict) and definition.get("slug") == DEPLOYMENT_IDENTITY_SLUG:
            identity_alert = _alert(definition)
            identity_alert["disabled"] = not deployment_identity
            alerts.append(identity_alert)
        elif deployment_identity:
            raise RuntimeError("SigNoz deployment identity alert is missing from the catalog")
    return [_dashboard(item) for item in definitions], alerts


def _replace_tokens(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tokens(item, replacements) for key, item in value.items()}
    return value


def materialize_assets(
    dashboards: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    environment: str,
    cluster_name: str,
    channel_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    replacements = {
        ENVIRONMENT_TOKEN: environment,
        ENVIRONMENT_SLUG_TOKEN: environment.lower().replace("_", "-"),
        CLUSTER_TOKEN: cluster_name,
        CHANNEL_TOKEN: channel_name,
    }
    return (
        _replace_tokens(copy.deepcopy(dashboards), replacements),
        _replace_tokens(copy.deepcopy(alerts), replacements),
    )


def dashboard_builder_queries(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("kind") == "signoz/BuilderQuery" and isinstance(
                value.get("spec"), dict
            ):
                queries.append(value["spec"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(dashboard)
    return queries


def dashboard_panel_queries(
    dashboard: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Builder queries keyed by panel id, so panel/query association is preserved."""
    panels = dashboard.get("spec", {}).get("panels")
    if not isinstance(panels, dict) or not panels:
        raise RuntimeError("dashboard exposes no panel map")
    return {
        str(panel_id): dashboard_builder_queries(panel)
        for panel_id, panel in panels.items()
    }


def dashboard_layout_refs(dashboard: dict[str, Any]) -> list[str]:
    """Panel references placed on the dashboard grid, in layout order."""
    refs: list[str] = []
    for layout in dashboard.get("spec", {}).get("layouts", []) or []:
        if not isinstance(layout, dict):
            continue
        for item in layout.get("spec", {}).get("items", []) or []:
            if isinstance(item, dict):
                refs.append(str(item.get("content", {}).get("$ref", "")))
    return refs


def _panel_title(panel: Any) -> str:
    if not isinstance(panel, dict):
        return ""
    return str(panel.get("spec", {}).get("display", {}).get("name", ""))


def _e2e_dashboard_names(environment: str) -> set[str]:
    env_slug = environment.lower().replace("_", "-")
    return {f"{slug}-{env_slug}" for slug in E2E_DASHBOARD_SLUGS}


def _dashboard_filter_is_scoped(
    dashboard: dict[str, Any], query: dict[str, Any], environment: str
) -> bool:
    expression = query.get("filter", {}).get("expression", "")
    if query.get("signal") == "logs":
        return dashboard.get("name") in _e2e_dashboard_names(environment) and (
            expression == CI_TEST_LOG_FILTER
            or (expression.startswith(f"{CI_TEST_LOG_FILTER} AND (") and expression.endswith(")"))
        )
    return f"deployment.environment = '{environment}'" in expression


def validate_asset_contract(
    dashboards: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    environment: str,
    cluster_name: str,
    channel_name: str,
) -> None:
    slugs = {alert.get("_orisoSlug") for alert in alerts}
    required = REQUIRED_ALERT_SLUGS | (slugs & {DEPLOYMENT_IDENTITY_SLUG})
    e2e_count = sum(
        1
        for item in dashboards
        if any(item.get("name", "").startswith(f"{slug}-") for slug in E2E_DASHBOARD_SLUGS)
    )
    if e2e_count not in (0, len(E2E_DASHBOARD_SLUGS)):
        raise RuntimeError("SigNoz E2E dashboards must be provisioned together")
    if (
        len(dashboards) != 3 + e2e_count
        or len(alerts) != len(required)
        or slugs != required
    ):
        raise RuntimeError("SigNoz asset count does not match the ORISO contract")
    encoded = json.dumps(
        {"dashboards": dashboards, "alerts": alerts}, sort_keys=True
    ).lower()
    leaked = sorted(term for term in FORBIDDEN_TERMS if term in encoded)
    if leaked:
        raise RuntimeError(
            f"protected identifier or content fields in SigNoz assets: {leaked}"
        )
    for token in (
        ENVIRONMENT_TOKEN,
        ENVIRONMENT_SLUG_TOKEN,
        CLUSTER_TOKEN,
        CHANNEL_TOKEN,
    ):
        if token.lower() in encoded:
            raise RuntimeError(f"unresolved SigNoz asset token: {token}")

    environment_filter = f"deployment.environment = '{environment}'"
    for dashboard in dashboards:
        if dashboard.get("schemaVersion") != "v6":
            raise RuntimeError(f"dashboard {dashboard.get('name')} is not schema v6")
        if dashboard.get("spec", {}).get("variables"):
            raise RuntimeError(
                f"dashboard {dashboard.get('name')} has unconstrained variables"
            )
        queries = dashboard_builder_queries(dashboard)
        if not queries:
            raise RuntimeError(
                f"dashboard {dashboard.get('name')} has no executable query"
            )
        for query in queries:
            if not _dashboard_filter_is_scoped(dashboard, query, environment):
                if query.get("signal") == "logs":
                    raise RuntimeError("CI log query is outside the allowed dashboard scope")
                raise RuntimeError(f"dashboard query is not scoped to {environment}")
            groups = {item.get("name") for item in query.get("groupBy", [])}
            if not groups.issubset(ALLOWED_GROUPS):
                raise RuntimeError(
                    f"dashboard query has unbounded grouping: {sorted(groups)}"
                )

    for alert in alerts:
        if alert.get("schemaVersion") != "v2alpha1" or alert.get("version") != "v5":
            raise RuntimeError(f"alert {alert.get('alert')} uses an obsolete schema")
        if alert.get("disabled") and alert["_orisoSlug"] != DEPLOYMENT_IDENTITY_SLUG:
            raise RuntimeError(f"alert {alert.get('alert')} is disabled")
        if alert.get("notificationSettings", {}).get("groupBy") != ["environment"]:
            raise RuntimeError(
                f"alert {alert.get('alert')} has unbounded notification grouping"
            )
        query = alert["condition"]["compositeQuery"]["queries"][0]["spec"]
        if environment_filter not in query.get("filter", {}).get("expression", ""):
            raise RuntimeError(
                f"alert {alert.get('alert')} is not scoped to {environment}"
            )
        channels = alert["condition"]["thresholds"]["spec"][0]["channels"]
        if channels != [channel_name]:
            raise RuntimeError(
                f"alert {alert.get('alert')} is not routed to the managed channel"
            )
        if alert.get("labels", {}).get("environment") != environment:
            raise RuntimeError(
                f"alert {alert.get('alert')} has incorrect environment label"
            )

    cluster_text = json.dumps(dashboards + alerts)
    if cluster_name not in cluster_text:
        raise RuntimeError("cluster-scoped SigNoz queries are missing cluster identity")


def build_query_range_payload(
    query: dict[str, Any], *, start_ms: int, end_ms: int,
    request_type: str = "time_series", query_type: str = "builder_query",
) -> dict[str, Any]:
    return {
        "schemaVersion": "v1",
        "start": start_ms,
        "end": end_ms,
        "requestType": request_type,
        "compositeQuery": {"queries": [{"type": query_type, "spec": query}]},
        "formatOptions": {"formatTableResultForUI": False, "fillGaps": False},
        "noCache": True,
    }


def _finite_number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _current_panel_point(point: Any, start_ms: int, end_ms: int) -> bool:
    if not isinstance(point, dict):
        return False
    timestamp = point.get("timestamp")
    return (
        type(timestamp) is int
        and start_ms <= timestamp < end_ms
        and point.get("partial", False) is False
        and _finite_number(point.get("value"))
    )


def _dimension_has_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return type(value) is bool or _finite_number(value)


def _response_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError
    return value


def _validate_optional_series(series: dict[str, Any]) -> None:
    for label in _response_list(series.get("labels")):
        if not isinstance(label["key"]["name"], str) or "value" not in label:
            raise ValueError
    for point in _response_list(series["values"]):
        value = point["value"]
        if (
            type(point["timestamp"]) is not int
            or type(point.get("partial", False)) is not bool
            or not (_finite_number(value) or value in ("NaN", "Inf", "-Inf"))
        ):
            raise ValueError


def _validate_dashboard_result(
    response: dict[str, Any], query: dict[str, Any], panel: str, start_ms: int, end_ms: int,
    *, require_samples: bool,
) -> None:
    dimensions = {field["name"] for field in query.get("groupBy", [])}
    try:
        envelope = response["data"]
        results = envelope["data"]["results"]
        if envelope["type"] != "time_series" or not isinstance(results, list):
            raise ValueError
        result, = results
        if result["queryName"] != query["name"]:
            raise ValueError
        buckets = _response_list(result["aggregations"])
        if require_samples and not buckets:
            raise RuntimeError(f"{panel}: no fresh numeric samples")

        indexes = [bucket["index"] for bucket in buckets]
        aggregation_count = len(query["aggregations"])
        if (
            any(type(index) is not int or not 0 <= index < aggregation_count for index in indexes)
            or len(set(indexes)) != len(indexes)
            or (require_samples and len(indexes) != aggregation_count)
        ):
            raise ValueError
        for bucket in buckets:
            series_list = _response_list(bucket["series"])
            if not require_samples:
                for series in series_list:
                    _validate_optional_series(series)
                continue
            found = False
            for series in series_list:
                if not any(
                    _current_panel_point(point, start_ms, end_ms)
                    for point in _response_list(series["values"])
                ):
                    continue
                found = True
                present = {
                    label["key"]["name"]
                    for label in _response_list(series.get("labels"))
                    if _dimension_has_value(label["value"])
                }
                missing = sorted(dimensions - present)
                if missing:
                    raise RuntimeError(f"{panel}: missing series dimensions {', '.join(missing)}")
            if not found:
                raise RuntimeError(f"{panel}: no fresh numeric samples")
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"{panel}: unexpected SigNoz time-series result") from error


def response_has_signal_data(response: Any) -> bool:
    if isinstance(response, dict):
        for key, value in response.items():
            if key in {"rows", "values"} and isinstance(value, list) and value:
                return True
            if response_has_signal_data(value):
                return True
    elif isinstance(response, list):
        return any(response_has_signal_data(item) for item in response)
    return False


def response_has_positive_signal_data(response: Any) -> bool:
    def positive(value: Any) -> bool:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False

    def visit(value: Any, *, inside_values: bool = False) -> bool:
        if isinstance(value, dict):
            if inside_values and positive(value.get("value")):
                return True
            return any(
                visit(child, inside_values=key == "values")
                for key, child in value.items()
            )
        if isinstance(value, list):
            if inside_values:
                for point in value:
                    if isinstance(point, (list, tuple)) and len(point) >= 2:
                        if positive(point[-1]):
                            return True
                    elif isinstance(point, dict) and positive(point.get("value")):
                        return True
            return any(visit(child, inside_values=inside_values) for child in value)
        return False

    return visit(response)


def opposite_environment_query(
    query: dict[str, Any], *, environment: str, cluster_name: str
) -> dict[str, Any]:
    other_environment = "dev" if environment != "dev" else "pre-dev"
    other_cluster = "oriso-dev" if other_environment == "dev" else "oriso-predev"
    opposite = copy.deepcopy(query)
    expression = opposite["filter"]["expression"]
    expression = expression.replace(
        f"k8s.cluster.name = '{cluster_name}'",
        f"k8s.cluster.name = '{other_cluster}'",
    )
    expression = expression.replace(
        f"deployment.environment = '{environment}'",
        f"deployment.environment = '{other_environment}'",
    )
    opposite["filter"]["expression"] = expression
    return opposite


def _unwrap(response: Any) -> Any:
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def redact_sensitive(value: str) -> str:
    value = re.sub(
        r'("(?:api_url|token|apiKey|password)"\s*:\s*")[^"]*(")',
        r"\1[REDACTED]\2",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"https://hooks\.slack\.com/services/[^\s\"']+",
        "[REDACTED]",
        value,
        flags=re.IGNORECASE,
    )


class SignozHttpError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(
            f"SigNoz API {method} {path} returned {status}: {redact_sensitive(detail)}"
        )
        self.status = status


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class SignozClient:
    READ_LIMIT = 2 * 1024 * 1024

    def __init__(
        self,
        api_url: str,
        api_key: str,
        timeout: int = 20,
        *,
        read_only: bool = False,
        reject_redirects: bool = False,
    ) -> None:
        self.api_url = api_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout = timeout
        self.reader = read_only
        self._opener = (
            urllib.request.build_opener(_RejectRedirect())
            if read_only or reject_redirects
            else None
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if self.reader and not (
            (method == "POST" and path == "/api/v5/query_range")
            or (method == "GET" and re.fullmatch(r"/api/v2/dashboards(?:/[0-9a-f-]{36})?", path))
        ):
            raise RuntimeError("Only the query and dashboard reads are allowed")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            urllib.parse.urljoin(self.api_url, path.lstrip("/")),
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "SIGNOZ-API-KEY": self.api_key,
            },
        )
        open_url = self._opener.open if self._opener is not None else urllib.request.urlopen
        try:
            with open_url(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(self.READ_LIMIT + 1) if self.reader else response.read()
                if self.reader and len(raw) > self.READ_LIMIT:
                    raise RuntimeError("SigNoz read response exceeds the size limit")
        except urllib.error.HTTPError as error:
            if self.reader:
                error.close()
                raise SignozHttpError(method, path, error.code, "response body omitted") from None
            status = error.code
            raw = error.read()
        except urllib.error.URLError as error:
            raise RuntimeError(f"SigNoz API {method} {path} is unreachable") from error
        if status not in expected:
            detail = "response body omitted" if self.reader else raw.decode("utf-8", errors="replace")[:1000]
            raise SignozHttpError(method, path, status, detail)
        if not raw:
            return None
        return json.loads(raw)

    def wait_ready(self, timeout_seconds: int = 300) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                self.request("GET", "/api/v2/dashboards")
                return
            except SignozHttpError as error:
                if error.status < 500:
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("SigNoz API did not become ready before timeout")
                time.sleep(5)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("SigNoz API did not become ready before timeout")
                time.sleep(5)


def _list(
    client: SignozClient, path: str, key: str | None = None
) -> list[dict[str, Any]]:
    value = _unwrap(client.request("GET", path))
    if key and isinstance(value, dict):
        value = value.get(key, [])
    if not isinstance(value, list):
        raise RuntimeError(f"SigNoz API {path} returned an unexpected list shape")
    return value


def require_channel(client: SignozClient, channel_name: str) -> None:
    """Fail unless the hand-made Slack channel exists. Never writes channels."""
    if not any(
        item.get("name") == channel_name and item.get("type") == "slack"
        for item in _list(client, "/api/v1/channels")
    ):
        raise RuntimeError(
            f"notification channel {channel_name!r} (type slack) is missing; "
            "it is created by hand in SigNoz, never by this program"
        )


def build_test_rule(alerts: list[dict[str, Any]], slug: str) -> dict[str, Any]:
    """Payload for POST /api/v1/testRule for one catalog alert.

    SigNoz stores the notification config under the posted alert name, so the
    test uses its own name and never touches the persisted managed rule.
    """
    match = next((item for item in alerts if item.get("_orisoSlug") == slug), None)
    if match is None:
        known = sorted(str(item.get("_orisoSlug")) for item in alerts)
        raise RuntimeError(f"unknown catalog alert {slug!r}; known: {known}")
    if match.get("disabled"):
        raise RuntimeError(f"catalog alert {slug!r} is disabled")
    payload = copy.deepcopy(match)
    payload.pop("_orisoSlug", None)
    payload["alert"] = f"{payload['alert']} [test]"
    return payload


def upsert_dashboards(client: SignozClient, dashboards: list[dict[str, Any]]) -> None:
    existing = {
        item.get("name"): item
        for item in _list(client, "/api/v2/dashboards", "dashboards")
    }
    for dashboard in dashboards:
        current = existing.get(dashboard["name"])
        if current:
            update = copy.deepcopy(dashboard)
            update.pop("generateName", None)
            client.request(
                "PUT",
                f"/api/v2/dashboards/{current['id']}",
                update,
                expected=(200,),
            )
        else:
            client.request("POST", "/api/v2/dashboards", dashboard, expected=(201,))


def upsert_alerts(client: SignozClient, alerts: list[dict[str, Any]]) -> None:
    existing = {item.get("alert"): item for item in _list(client, "/api/v2/rules")}
    for alert in alerts:
        payload = copy.deepcopy(alert)
        payload.pop("_orisoSlug", None)
        current = existing.get(alert["alert"])
        if current and alert.get("_orisoSlug") == DEPLOYMENT_IDENTITY_SLUG:
            if (current.get("labels") or {}).get("managed_by") != "oriso_helm":
                if alert.get("disabled"):
                    continue
                raise RuntimeError("deployment identity alert name is already in use")
        if current:
            client.request(
                "PUT", f"/api/v2/rules/{current['id']}", payload, expected=(204,)
            )
        elif not alert.get("disabled"):
            client.request("POST", "/api/v2/rules", payload, expected=(201,))


def _execute_query(
    client: SignozClient, query: dict[str, Any], *, minutes: int = 15,
    dashboard_panel: str | None = None, require_samples: bool = True,
) -> dict[str, Any]:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - minutes * 60_000
    response = client.request(
        "POST",
        "/api/v5/query_range",
        build_query_range_payload(query, start_ms=start_ms, end_ms=end_ms),
    )
    if not isinstance(response, dict) or response.get("status") != "success":
        raise RuntimeError("SigNoz query range did not return success")
    if dashboard_panel is not None:
        _validate_dashboard_result(
            response, query, dashboard_panel, start_ms, end_ms, require_samples=require_samples,
        )
    return response


def _browser_vitals_sql(environment: str, start_ms: int, end_ms: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", environment):
        raise RuntimeError("Invalid browser vitals environment")
    metrics = ", ".join(f"'{name}'" for name in BROWSER_VITALS_METRICS.values())
    services = ", ".join(f"'{name}'" for name in BROWSER_VITALS_SERVICES)
    # The exporter stores one series identity per hour. Only sample time proves freshness.
    return f"""WITH identities AS (
  SELECT DISTINCT env, temporality, metric_name, fingerprint,
    resource_attrs['service.name'] AS service_name
  FROM signoz_metrics.distributed_time_series_v4
  WHERE metric_name IN ({metrics})
    AND unix_milli >= {start_ms - 3_600_000} AND unix_milli < {end_ms}
    AND resource_attrs['deployment.environment'] = '{environment}'
    AND resource_attrs['oriso.signal.source'] = 'browser'
    AND service_name IN ({services})
)
SELECT i.service_name AS service_name, s.metric_name AS metric_name,
  count() AS samples, max(s.unix_milli) AS latest_ms
FROM signoz_metrics.distributed_samples_v4 AS s
GLOBAL INNER JOIN identities AS i USING (env, temporality, metric_name, fingerprint)
WHERE s.metric_name IN ({metrics}) AND s.unix_milli >= {start_ms} AND s.unix_milli < {end_ms}
  AND bitAnd(s.flags, 1) = 0 AND isFinite(s.value)
  AND (s.value > 0 OR (s.metric_name = 'cls' AND s.value = 0))
GROUP BY i.service_name, s.metric_name
ORDER BY i.service_name, s.metric_name
SETTINGS max_execution_time = 8, max_rows_to_read = 2000000, max_bytes_to_read = 134217728,
  timeout_overflow_mode = 'throw', read_overflow_mode = 'throw'"""


def _check_browser_vitals(
    client: SignozClient, environment: str, since_unix_ms: int | None
) -> int:
    end_ms = int(time.time() * 1000)
    if since_unix_ms is not None and not 0 <= since_unix_ms < end_ms:
        raise RuntimeError("--since-unix-ms must be a nonnegative timestamp before now")
    start_ms = max(end_ms - BROWSER_VITALS_LOOKBACK_MS, since_unix_ms or 0)
    query_name = "BrowserVitals"
    payload = {
        "start": start_ms,
        "end": end_ms,
        "requestType": "raw",
        "noCache": True,
        "compositeQuery": {"queries": [{
            "type": "clickhouse_sql",
            "spec": {
                "name": query_name,
                "query": _browser_vitals_sql(environment, start_ms, end_ms),
            },
        }]},
    }
    services: dict[str, dict[str, Any]] = {
        name: dict.fromkeys(BROWSER_VITALS_METRICS) for name in BROWSER_VITALS_SERVICES
    }
    names = {stored: metric for metric, stored in BROWSER_VITALS_METRICS.items()}
    try:
        response = client.request("POST", "/api/v5/query_range", payload)
        envelope = response["data"]
        inner = envelope["data"]
        result, = inner["results"]
        rows = result["rows"]
        if rows is None:
            rows = []
        if (
            response["status"] != "success" or envelope["type"] != "raw"
            or result["queryName"] != query_name or result["nextCursor"] != ""
            or not isinstance(rows, list)
            or any(part.get("warning") or part.get("warnings") for part in (response, envelope, inner, result))
        ):
            raise ValueError
        for row in rows:
            data = row["data"]
            readings = services[data["service_name"]]
            metric = names[data["metric_name"]]
            samples, latest = data["samples"], data["latest_ms"]
            if (
                readings[metric] is not None or type(samples) is not int or samples < 1
                or type(latest) is not int or not start_ms <= latest < end_ms
            ):
                raise ValueError
            readings[metric] = {"samples": samples, "latestUnixMs": latest}
    except SignozHttpError as error:
        raise RuntimeError(f"SigNoz browser vitals query returned {error.status}") from None
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("SigNoz returned incomplete browser vitals evidence") from error

    missing = [
        f"{name}/{metric}" for name, readings in services.items()
        for metric, evidence in readings.items() if evidence is None
    ]
    print(json.dumps({
        "environment": environment, "startUnixMs": start_ms, "endUnixMs": end_ms,
        "services": services, "missing": missing,
    }, indent=2, sort_keys=True))
    return int(bool(missing))


def eval_window_minutes(alert: dict[str, Any]) -> int:
    """Minutes in an alert's rolling evaluation window, e.g. `5m` -> 5."""
    window = str(alert.get("evaluation", {}).get("spec", {}).get("evalWindow", ""))
    match = re.fullmatch(r"(\d+)([smh])", window.strip())
    if not match:
        raise RuntimeError(f"unsupported alert evaluation window: {window!r}")
    amount, unit = int(match.group(1)), match.group(2)
    minutes = {"s": amount // 60, "m": amount, "h": amount * 60}[unit]
    if minutes < 1:
        raise RuntimeError(
            f"alert evaluation window is shorter than a minute: {window!r}"
        )
    return minutes


def _wait_for_signal_data(
    client: SignozClient,
    query: dict[str, Any],
    *,
    minutes: int = 15,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = _execute_query(client, query, minutes=minutes)
        if response_has_positive_signal_data(response):
            return response
        if time.monotonic() >= deadline:
            return response
        time.sleep(10)


def _query_contract(query: dict[str, Any]) -> dict[str, Any]:
    aggregations = query.get("aggregations", [])
    if query.get("signal") in {"traces", "logs"}:
        if not aggregations:
            raise RuntimeError("managed SigNoz query must contain an aggregation")
        return {
            "signal": query["signal"],
            "disabled": query.get("disabled"),
            "aggregations": [
                (str(item.get("expression")), str(item.get("alias", "")))
                for item in aggregations
            ],
            "filter": query.get("filter", {}).get("expression", ""),
            "having": query.get("having", {}).get("expression", ""),
            "limit": query.get("limit"),
            "groupBy": sorted(
                (
                    str(item.get("name")),
                    str(item.get("fieldContext", "")),
                    str(item.get("fieldDataType", "")),
                )
                for item in query.get("groupBy", [])
            ),
        }
    if len(aggregations) != 1:
        raise RuntimeError("managed SigNoz query must contain exactly one aggregation")
    aggregation = aggregations[0]
    return {
        "disabled": query.get("disabled"),
        "metricName": aggregation.get("metricName"),
        "timeAggregation": aggregation.get("timeAggregation"),
        "spaceAggregation": aggregation.get("spaceAggregation"),
        "filter": query.get("filter", {}).get("expression", ""),
        # A restrictive HAVING can filter out every aggregate while the query
        # still returns a successful (empty) response, so it is part of the
        # contract, not cosmetics.
        "having": query.get("having", {}).get("expression", ""),
        # Field context and type are part of the contract: a tag key read as a
        # resource attribute (or another type) silently returns no groups.
        "groupBy": sorted(
            (
                str(item.get("name")),
                str(item.get("fieldContext", "")),
                str(item.get("fieldDataType", "")),
            )
            for item in query.get("groupBy", [])
        ),
    }


def _assert_no_protected_fields(value: Any, label: str) -> None:
    encoded = json.dumps(value, sort_keys=True).lower()
    leaked = sorted(term for term in FORBIDDEN_TERMS if term in encoded)
    if leaked:
        raise RuntimeError(f"{label} contains protected fields: {leaked}")


def _panel_column_units(panel: dict[str, Any]) -> dict[str, str]:
    plugin = panel.get("spec", {}).get("plugin", {})
    return plugin.get("spec", {}).get("formatting", {}).get("columnUnits") or {}


def validate_live_dashboard(
    current: dict[str, Any],
    expected: dict[str, Any],
    *,
    environment: str,
) -> list[dict[str, Any]]:
    if current.get("schemaVersion") != "v6" or current.get("name") != expected["name"]:
        raise RuntimeError(f"stored dashboard identity drift: {expected['name']}")
    if current.get("spec", {}).get("variables"):
        raise RuntimeError(
            f"stored dashboard has unconstrained variables: {expected['name']}"
        )
    tags = {(item.get("key"), item.get("value")) for item in current.get("tags", [])}
    required_tags = {("managed-by", "oriso-helm"), ("environment", environment)}
    if not required_tags.issubset(tags):
        raise RuntimeError(f"stored dashboard tag drift: {expected['name']}")

    current_queries = dashboard_builder_queries(current)
    # Compare panel by panel. A dashboard-wide multiset stays identical when two
    # panels have their queries swapped, which leaves every label misleading
    # while every query still executes successfully.
    current_panels = dashboard_panel_queries(current)
    expected_panels = dashboard_panel_queries(expected)
    if set(current_panels) != set(expected_panels):
        raise RuntimeError(f"stored dashboard panel drift: {expected['name']}")
    # A panel removed from the grid stays in spec.panels, so its query still
    # executes while nobody can see it. Compare the placed references too.
    if sorted(dashboard_layout_refs(current)) != sorted(
        dashboard_layout_refs(expected)
    ):
        raise RuntimeError(f"stored dashboard layout drift: {expected['name']}")
    current_panel_specs = current.get("spec", {}).get("panels", {})
    expected_panel_specs = expected.get("spec", {}).get("panels", {})
    for panel_id, expected_panel_queries in expected_panels.items():
        if _panel_title(current_panel_specs.get(panel_id)) != _panel_title(
            expected_panel_specs.get(panel_id)
        ):
            raise RuntimeError(
                f"stored dashboard panel title drift: {expected['name']} / {panel_id}"
            )
        if _panel_column_units(current_panel_specs[panel_id]) != _panel_column_units(
            expected_panel_specs[panel_id]
        ):
            raise RuntimeError(
                f"stored dashboard formatting drift: {expected['name']} / {panel_id}"
            )
        current_panel_queries = current_panels[panel_id]
        if len(current_panel_queries) != len(expected_panel_queries):
            raise RuntimeError(
                f"stored dashboard query drift: {expected['name']} / {panel_id}"
            )
        for current_query, expected_query in zip(
            current_panel_queries, expected_panel_queries
        ):
            if _query_contract(current_query) != _query_contract(expected_query):
                raise RuntimeError(
                    f"stored dashboard query drift: {expected['name']} / {panel_id}"
                )
    for query in current_queries:
        contract = _query_contract(query)
        if not _dashboard_filter_is_scoped(current, query, environment):
            raise RuntimeError(f"stored dashboard query drift: {expected['name']}")
        if not {name for name, _, _ in contract["groupBy"]}.issubset(ALLOWED_GROUPS):
            raise RuntimeError(
                f"stored dashboard has unbounded grouping: {expected['name']}"
            )
    _assert_no_protected_fields(current, f"stored dashboard {expected['name']}")
    return current_queries


def validate_live_alert(
    current: dict[str, Any],
    expected: dict[str, Any],
    *,
    environment: str,
    channel_name: str,
) -> dict[str, Any]:
    if (
        current.get("alert") != expected["alert"]
        or current.get("schemaVersion") != "v2alpha1"
        or current.get("version") != "v5"
        or bool(current.get("disabled")) != bool(expected.get("disabled"))
    ):
        raise RuntimeError(f"stored alert identity or state drift: {expected['alert']}")
    settings = current.get("notificationSettings", {})
    if settings.get("groupBy") != ["environment"] or settings.get("usePolicy"):
        raise RuntimeError(f"stored alert grouping drift: {expected['alert']}")
    labels = current.get("labels", {})
    if (
        labels.get("environment") != environment
        or labels.get("managed_by") != "oriso_helm"
    ):
        raise RuntimeError(f"stored alert label drift: {expected['alert']}")

    current_condition = current["condition"]
    expected_condition = expected["condition"]
    for key, default in (("alertOnAbsent", False), ("absentFor", 0)):
        actual = current_condition.get(key, default)
        desired = expected_condition.get(key, default)
        if type(actual) is not type(desired) or actual != desired:
            raise RuntimeError(f"stored alert absence drift ({key}): {expected['alert']}")

    current_threshold = current_condition["thresholds"]["spec"][0]
    expected_threshold = expected_condition["thresholds"]["spec"][0]
    for key in ("name", "target", "matchType", "op"):
        if current_threshold.get(key) != expected_threshold.get(key):
            raise RuntimeError(f"stored alert threshold drift: {expected['alert']}")
    if current_threshold.get("channels") != [channel_name]:
        raise RuntimeError(f"stored alert route drift: {expected['alert']}")

    current_query = current_condition["compositeQuery"]["queries"][0]["spec"]
    expected_query = expected_condition["compositeQuery"]["queries"][0]["spec"]
    if _query_contract(current_query) != _query_contract(expected_query):
        raise RuntimeError(f"stored alert query drift: {expected['alert']}")
    if f"deployment.environment = '{environment}'" not in current_query.get(
        "filter", {}
    ).get("expression", ""):
        raise RuntimeError(f"stored alert query drift: {expected['alert']}")
    _assert_no_protected_fields(current, f"stored alert {expected['alert']}")
    return current_query


def prove_conformance(
    client: SignozClient,
    dashboards: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    environment: str,
    cluster_name: str,
    channel_name: str,
    telemetry: bool = True,
) -> None:
    """Read every managed object back and compare it with the catalog.

    With ``telemetry`` the proof requires fresh grouped data on the core dashboards,
    validates E2E query results, executes the alert queries, and verifies collector
    freshness and environment separation. Without it the proof is structural only.
    """
    validate_asset_contract(
        dashboards,
        alerts,
        environment=environment,
        cluster_name=cluster_name,
        channel_name=channel_name,
    )
    listed_dashboards = {
        item.get("name"): item
        for item in _list(client, "/api/v2/dashboards", "dashboards")
    }
    optional_dashboards = _e2e_dashboard_names(environment)
    for expected in dashboards:
        listed = listed_dashboards.get(expected["name"])
        if not listed:
            raise RuntimeError(f"managed dashboard is missing: {expected['name']}")
        current = _unwrap(client.request("GET", f"/api/v2/dashboards/{listed['id']}"))
        if not isinstance(current, dict):
            raise RuntimeError(
                f"managed dashboard is not readable as v6: {expected['name']}"
            )
        validate_live_dashboard(current, expected, environment=environment)
        if telemetry:
            for panel_id, queries in dashboard_panel_queries(current).items():
                for query in queries:
                    _execute_query(
                        client, query, dashboard_panel=f"{expected['name']} / {panel_id}",
                        require_samples=expected["name"] not in optional_dashboards,
                    )

    listed_alerts = {item.get("alert"): item for item in _list(client, "/api/v2/rules")}
    freshness_query: dict[str, Any] | None = None
    freshness_window_minutes = 0
    for expected in alerts:
        current = listed_alerts.get(expected["alert"])
        if expected.get("disabled") and (
            not current or (current.get("labels") or {}).get("managed_by") != "oriso_helm"
        ):
            continue
        if not current:
            raise RuntimeError(
                f"managed alert is missing, obsolete, or disabled: {expected['alert']}"
            )
        query = validate_live_alert(
            current,
            expected,
            environment=environment,
            channel_name=channel_name,
        )
        if telemetry and not expected.get("disabled"):
            _execute_query(client, query)
        if expected.get("_orisoSlug") == "collector-staleness":
            freshness_query = query
            freshness_window_minutes = eval_window_minutes(expected)

    require_channel(client, channel_name)
    if freshness_query is None:
        raise RuntimeError("managed collector-staleness alert is missing")
    if not telemetry:
        return
    # Probe the same window the alert evaluates. Over the default 15 minutes a
    # collector that stopped 10 minutes ago still shows an earlier positive
    # rate, so the gate would print PASS while the staleness alert is already
    # firing or in no-data.
    expected_response = _wait_for_signal_data(
        client, freshness_query, minutes=freshness_window_minutes
    )
    if not response_has_positive_signal_data(expected_response):
        raise RuntimeError(f"collector telemetry is stale or missing in {environment}")

    other_environment = "dev" if environment != "dev" else "pre-dev"
    other_query = opposite_environment_query(
        freshness_query, environment=environment, cluster_name=cluster_name
    )
    if response_has_signal_data(_execute_query(client, other_query)):
        raise RuntimeError(
            f"environment separation failed: {other_environment} collector data is visible in {environment}"
        )


def default_assets_dir(script_path: str | Path = __file__) -> str:
    """Catalog next to the script (hook ConfigMap) or in the chart checkout."""
    script_dir = Path(script_path).resolve().parent
    for candidate in (script_dir, script_dir.parent / "files" / "signoz"):
        if (candidate / "observability-catalog.json").is_file():
            return str(candidate)
    return ""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "The API key is read only from the SIGNOZ_API_KEY environment "
            "variable, never from the command line, and is never printed. "
            "Read-only workstation check: python3 files/signoz/signoz_observability.py "
            "--api-url <signoz base URL including /signoz> "
            "--mode check --environment dev --cluster-name oriso-dev. "
            "One synthetic notification for one catalog alert, no stored "
            "rule: --mode test-rule --rule <slug>."
        ),
    )
    parser.add_argument(
        "--mode", choices=("provision", "check", "test-rule", "check-browser-vitals"), default="check"
    )
    parser.add_argument(
        "--rule", default="", help="Catalog alert slug for --mode test-rule"
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("SIGNOZ_API_URL") or os.environ.get("SIGNOZ_URL", ""),
        help="SigNoz base URL (env SIGNOZ_API_URL or SIGNOZ_URL)",
    )
    parser.add_argument(
        "--assets-dir",
        default=os.environ.get("ORISO_ASSETS_DIR") or default_assets_dir(),
    )
    parser.add_argument(
        "--environment", default=os.environ.get("ORISO_ENVIRONMENT", "")
    )
    parser.add_argument(
        "--since-unix-ms", type=int,
        help="Limit the browser vitals check to samples at or after this Unix timestamp in milliseconds",
    )
    parser.add_argument(
        "--cluster-name", default=os.environ.get("ORISO_CLUSTER_NAME", "")
    )
    parser.add_argument(
        "--channel-name",
        default=os.environ.get("SIGNOZ_CHANNEL_NAME", "oriso-alerts-slack"),
        help="Existing SigNoz notification channel the alert rules route to",
    )
    parser.add_argument(
        "--conformance",
        action="store_true",
        default=os.environ.get("ORISO_CONFORMANCE_CHECK", "false").lower() == "true",
        help="In provision mode also prove queries, freshness, and separation",
    )
    parser.add_argument(
        "--deployment-identity",
        action="store_true",
        default=os.environ.get("ORISO_DEPLOYMENT_IDENTITY_ENABLED", "false").lower() == "true",
        help="Include the deployment identity rule for an enabled Dev or Pre-Dev observer",
    )
    parser.add_argument(
        "--e2e-dashboards",
        action="store_true",
        default=os.environ.get("ORISO_E2E_DASHBOARDS", "false").lower() == "true",
        help="Also manage the E2E test health and flaky dashboards (oriso-e2e spans)",
    )
    parser.add_argument(
        "--skip-telemetry",
        action="store_true",
        help="In check mode only compare stored objects; run no queries",
    )
    args = parser.parse_args(argv)
    args.api_key = os.environ.get("SIGNOZ_API_KEY", "")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.api_key:
        raise RuntimeError("SIGNOZ_API_KEY environment variable is required")
    browser_check = args.mode == "check-browser-vitals"
    required = ("api_url", "environment") if browser_check else (
        "api_url", "assets_dir", "environment", "cluster_name"
    )
    for name in required:
        if not getattr(args, name):
            raise RuntimeError(f"--{name.replace('_', '-')} is required")
    if args.mode in {"provision", "check-browser-vitals"} and args.skip_telemetry:
        raise RuntimeError("--skip-telemetry applies to --mode check only")
    if (args.mode == "test-rule") != bool(args.rule):
        raise RuntimeError("--rule is required with, and only with, --mode test-rule")
    if args.since_unix_ms is not None and not browser_check:
        raise RuntimeError("--since-unix-ms applies to --mode check-browser-vitals only")
    if browser_check:
        client = SignozClient(args.api_url, args.api_key, reject_redirects=True)
        return _check_browser_vitals(client, args.environment, args.since_unix_ms)

    if args.deployment_identity and args.environment not in ("dev", "pre-dev"):
        raise RuntimeError("deployment identity alerts require Dev or Pre-Dev")

    dashboards, alerts = load_assets(
        args.assets_dir,
        deployment_identity=args.deployment_identity,
        include_e2e=args.e2e_dashboards,
    )
    dashboards, alerts = materialize_assets(
        dashboards,
        alerts,
        environment=args.environment,
        cluster_name=args.cluster_name,
        channel_name=args.channel_name,
    )
    client = SignozClient(args.api_url, args.api_key)
    client.wait_ready()

    if args.mode == "test-rule":
        require_channel(client, args.channel_name)
        payload = build_test_rule(alerts, args.rule)
        result = _unwrap(client.request("POST", "/api/v1/testRule", payload))
        count = result.get("alertCount") if isinstance(result, dict) else None
        if not count:
            print(
                f"NOT SENT: the {args.rule!r} query returned no series in "
                f"{args.environment} (alertCount={count}), so nothing reached "
                f"{args.channel_name!r}"
            )
            return 2
        print(
            f"PASS: test notification for {args.rule!r} in {args.environment} "
            f"sent to {args.channel_name!r} (alertCount={count})"
        )
        return 0

    if args.mode == "provision":
        # Channels are hand-made; fail before writing anything if it is absent.
        require_channel(client, args.channel_name)
        upsert_dashboards(client, dashboards)
        upsert_alerts(client, alerts)

    if args.mode == "check":
        telemetry = not args.skip_telemetry
    else:
        telemetry = args.conformance
    prove_conformance(
        client,
        dashboards,
        alerts,
        environment=args.environment,
        cluster_name=args.cluster_name,
        channel_name=args.channel_name,
        telemetry=telemetry,
    )
    if telemetry:
        print(
            f"PASS: SigNoz dashboards, alerts, routes, queries, freshness, and environment separation for {args.environment}"
        )
    else:
        print(
            f"PASS: SigNoz dashboards, alerts, channel, and routes stored as catalogued for {args.environment} (telemetry not checked)"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
