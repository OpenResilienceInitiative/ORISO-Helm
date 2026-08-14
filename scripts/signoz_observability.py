#!/usr/bin/env python3
"""Provision and prove ORISO-managed SigNoz observability assets.

The implementation uses only the Python standard library so the Helm hook has
no runtime package installation. It never sends application content or
protected identifiers to notification routes.
"""

from __future__ import annotations

import argparse
import copy
import json
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

REQUIRED_DASHBOARD_SLUGS = {
    "oriso-live-chat",
    "oriso-service-reliability",
    "oriso-platform-health",
}
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
    "workflow",
}
FORBIDDEN_TERMS = {
    "access_token",
    "email",
    "message.body",
    "password",
    "room.id",
    "room_id",
    "user.id",
    "user_id",
    "username",
}


def _scope_filter(definition: dict[str, Any]) -> str:
    parts = [f"deployment.environment = '{ENVIRONMENT_TOKEN}'"]
    if definition.get("clusterScoped"):
        parts.append(f"k8s.cluster.name = '{CLUSTER_TOKEN}'")
    if definition.get("filter"):
        parts.append(str(definition["filter"]))
    return " AND ".join(parts)


def _group_by(names: Iterable[str]) -> list[dict[str, str]]:
    return [
        {"name": name, "fieldContext": "tag", "fieldDataType": "string"}
        for name in names
    ]


def _builder_query(definition: dict[str, Any]) -> dict[str, Any]:
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
        panels[panel_id] = {
            "kind": "Panel",
            "spec": {
                "display": {"name": panel["title"], "description": ""},
                "plugin": {"kind": "signoz/TimeSeriesPanel", "spec": {}},
                "links": [],
                "queries": [
                    {
                        "kind": "time_series",
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
            "duration": "1h",
            "refreshInterval": "1m",
            "variables": [],
            "panels": panels,
            "layouts": [{"kind": "Grid", "spec": {"items": items}}],
            "links": [],
        },
    }


def _alert(definition: dict[str, Any]) -> dict[str, Any]:
    renotify_states = ["firing"]
    if definition.get("notifyOnNoData"):
        renotify_states.append("nodata")
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
    asset_dir: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    catalog_path = Path(asset_dir) / "observability-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if {item["slug"] for item in catalog["dashboards"]} != REQUIRED_DASHBOARD_SLUGS:
        raise RuntimeError("SigNoz dashboard catalog is incomplete")
    if {item["slug"] for item in catalog["alerts"]} != REQUIRED_ALERT_SLUGS:
        raise RuntimeError("SigNoz alert catalog is incomplete")
    return (
        [_dashboard(item) for item in catalog["dashboards"]],
        [_alert(item) for item in catalog["alerts"]],
    )


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


def validate_asset_contract(
    dashboards: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    environment: str,
    cluster_name: str,
    channel_name: str,
) -> None:
    if len(dashboards) != 3 or len(alerts) != 6:
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
            if environment_filter not in query.get("filter", {}).get("expression", ""):
                raise RuntimeError(f"dashboard query is not scoped to {environment}")
            groups = {item.get("name") for item in query.get("groupBy", [])}
            if not groups.issubset(ALLOWED_GROUPS):
                raise RuntimeError(
                    f"dashboard query has unbounded grouping: {sorted(groups)}"
                )

    for alert in alerts:
        if alert.get("schemaVersion") != "v2alpha1" or alert.get("version") != "v5":
            raise RuntimeError(f"alert {alert.get('alert')} uses an obsolete schema")
        if alert.get("disabled"):
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
    query: dict[str, Any], *, start_ms: int, end_ms: int
) -> dict[str, Any]:
    return {
        "schemaVersion": "v1",
        "start": start_ms,
        "end": end_ms,
        "requestType": "time_series",
        "compositeQuery": {"queries": [{"type": "builder_query", "spec": query}]},
        "formatOptions": {"formatTableResultForUI": False, "fillGaps": False},
        "noCache": True,
    }


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


def build_slack_receiver(
    *, channel_name: str, webhook_url: str, environment: str, cluster_name: str
) -> dict[str, Any]:
    return {
        "name": channel_name,
        "slack_configs": [
            {
                "send_resolved": True,
                "api_url": webhook_url,
                "channel": "",
                "title": f"ORISO platform alert [{environment}]",
                "text": (
                    f"Environment: {environment}\nCluster: {cluster_name}\n"
                    "Alert: {{ .CommonAnnotations.summary }}\n"
                    "Details: {{ .CommonAnnotations.description }}\n"
                    "State: {{ .Status }}"
                ),
            }
        ],
    }


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


class SignozClient:
    def __init__(self, api_url: str, api_key: str, timeout: int = 20) -> None:
        self.api_url = api_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
        except urllib.error.URLError as error:
            raise RuntimeError(f"SigNoz API {method} {path} is unreachable") from error
        if status not in expected:
            detail = raw.decode("utf-8", errors="replace")[:1000]
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


def upsert_channel(client: SignozClient, receiver: dict[str, Any]) -> dict[str, Any]:
    existing = next(
        (
            item
            for item in _list(client, "/api/v1/channels")
            if item.get("name") == receiver["name"]
        ),
        None,
    )
    if existing:
        client.request(
            "PUT",
            f"/api/v1/channels/{existing['id']}",
            receiver,
            expected=(204,),
        )
        return existing
    created = _unwrap(
        client.request("POST", "/api/v1/channels", receiver, expected=(201,))
    )
    if not isinstance(created, dict):
        raise RuntimeError("SigNoz channel create returned an unexpected shape")
    return created


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
        if current:
            client.request(
                "PUT", f"/api/v2/rules/{current['id']}", payload, expected=(204,)
            )
        else:
            client.request("POST", "/api/v2/rules", payload, expected=(201,))


def _execute_query(
    client: SignozClient, query: dict[str, Any], *, minutes: int = 15
) -> dict[str, Any]:
    end_ms = int(time.time() * 1000)
    response = client.request(
        "POST",
        "/api/v5/query_range",
        build_query_range_payload(
            query, start_ms=end_ms - minutes * 60_000, end_ms=end_ms
        ),
    )
    if not isinstance(response, dict) or response.get("status") != "success":
        raise RuntimeError("SigNoz query range did not return success")
    return response


def _wait_for_signal_data(
    client: SignozClient,
    query: dict[str, Any],
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = _execute_query(client, query)
        if response_has_positive_signal_data(response):
            return response
        if time.monotonic() >= deadline:
            return response
        time.sleep(10)


def _query_contract(query: dict[str, Any]) -> dict[str, Any]:
    aggregations = query.get("aggregations", [])
    if len(aggregations) != 1:
        raise RuntimeError("managed SigNoz query must contain exactly one aggregation")
    aggregation = aggregations[0]
    return {
        "metricName": aggregation.get("metricName"),
        "timeAggregation": aggregation.get("timeAggregation"),
        "spaceAggregation": aggregation.get("spaceAggregation"),
        "filter": query.get("filter", {}).get("expression", ""),
        "groupBy": sorted(item.get("name") for item in query.get("groupBy", [])),
    }


def _assert_no_protected_fields(value: Any, label: str) -> None:
    encoded = json.dumps(value, sort_keys=True).lower()
    leaked = sorted(term for term in FORBIDDEN_TERMS if term in encoded)
    if leaked:
        raise RuntimeError(f"{label} contains protected fields: {leaked}")


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
    expected_queries = dashboard_builder_queries(expected)
    current_contracts = sorted(
        (_query_contract(query) for query in current_queries),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    expected_contracts = sorted(
        (_query_contract(query) for query in expected_queries),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    if current_contracts != expected_contracts:
        raise RuntimeError(f"stored dashboard query drift: {expected['name']}")
    environment_filter = f"deployment.environment = '{environment}'"
    for query in current_queries:
        contract = _query_contract(query)
        if environment_filter not in contract["filter"]:
            raise RuntimeError(f"stored dashboard query drift: {expected['name']}")
        if not set(contract["groupBy"]).issubset(ALLOWED_GROUPS):
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
        or current.get("disabled")
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

    current_threshold = current["condition"]["thresholds"]["spec"][0]
    expected_threshold = expected["condition"]["thresholds"]["spec"][0]
    for key in ("name", "target", "matchType", "op"):
        if current_threshold.get(key) != expected_threshold.get(key):
            raise RuntimeError(f"stored alert threshold drift: {expected['alert']}")
    if current_threshold.get("channels") != [channel_name]:
        raise RuntimeError(f"stored alert route drift: {expected['alert']}")

    current_query = current["condition"]["compositeQuery"]["queries"][0]["spec"]
    expected_query = expected["condition"]["compositeQuery"]["queries"][0]["spec"]
    if _query_contract(current_query) != _query_contract(expected_query):
        raise RuntimeError(f"stored alert query drift: {expected['alert']}")
    if f"deployment.environment = '{environment}'" not in current_query.get(
        "filter", {}
    ).get("expression", ""):
        raise RuntimeError(f"stored alert query drift: {expected['alert']}")
    _assert_no_protected_fields(current, f"stored alert {expected['alert']}")
    return current_query


def validate_managed_routes(
    routes: list[dict[str, Any]],
    *,
    rule_ids: list[str],
    channel_name: str,
) -> None:
    for rule_id in rule_ids:
        matching = [
            route
            for route in routes
            if route.get("kind") == "rule"
            and route.get("name") == rule_id
            and rule_id in route.get("expression", "")
            and route.get("channels") == [channel_name]
        ]
        if len(matching) != 1:
            raise RuntimeError(
                f"managed alert route is missing or ambiguous: {rule_id}"
            )


def prove_conformance(
    client: SignozClient,
    dashboards: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    environment: str,
    cluster_name: str,
    channel_name: str,
) -> None:
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
    for expected in dashboards:
        listed = listed_dashboards.get(expected["name"])
        if not listed:
            raise RuntimeError(f"managed dashboard is missing: {expected['name']}")
        current = _unwrap(client.request("GET", f"/api/v2/dashboards/{listed['id']}"))
        if not isinstance(current, dict):
            raise RuntimeError(
                f"managed dashboard is not readable as v6: {expected['name']}"
            )
        for query in validate_live_dashboard(
            current, expected, environment=environment
        ):
            _execute_query(client, query)

    listed_alerts = {item.get("alert"): item for item in _list(client, "/api/v2/rules")}
    managed_rule_ids: list[str] = []
    freshness_query: dict[str, Any] | None = None
    for expected in alerts:
        current = listed_alerts.get(expected["alert"])
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
        _execute_query(client, query)
        managed_rule_ids.append(str(current["id"]))
        if expected.get("_orisoSlug") == "collector-staleness":
            freshness_query = query

    channels = _list(client, "/api/v1/channels")
    if not any(
        item.get("name") == channel_name and item.get("type") == "slack"
        for item in channels
    ):
        raise RuntimeError("managed Slack notification channel is missing")
    routes = _list(client, "/api/v1/route_policies")
    validate_managed_routes(
        routes, rule_ids=managed_rule_ids, channel_name=channel_name
    )
    if freshness_query is None:
        raise RuntimeError("managed collector-staleness alert is missing")
    expected_response = _wait_for_signal_data(client, freshness_query)
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("provision", "check"), default="check")
    parser.add_argument("--api-url", default=os.environ.get("SIGNOZ_API_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("SIGNOZ_API_KEY", ""))
    parser.add_argument("--assets-dir", default=os.environ.get("ORISO_ASSETS_DIR", ""))
    parser.add_argument(
        "--environment", default=os.environ.get("ORISO_ENVIRONMENT", "")
    )
    parser.add_argument(
        "--cluster-name", default=os.environ.get("ORISO_CLUSTER_NAME", "")
    )
    parser.add_argument(
        "--channel-name",
        default=os.environ.get("SIGNOZ_CHANNEL_NAME", "ORISO Platform Alerts"),
    )
    parser.add_argument(
        "--slack-webhook-url", default=os.environ.get("SLACK_WEBHOOK_URL", "")
    )
    parser.add_argument(
        "--test-notification-route",
        action="store_true",
        default=os.environ.get("TEST_NOTIFICATION_ROUTE", "false").lower() == "true",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    for name in ("api_url", "api_key", "assets_dir", "environment", "cluster_name"):
        if not getattr(args, name):
            raise RuntimeError(f"--{name.replace('_', '-')} is required")
    if args.mode == "provision" and not args.slack_webhook_url:
        raise RuntimeError("--slack-webhook-url is required in provision mode")

    dashboards, alerts = load_assets(args.assets_dir)
    dashboards, alerts = materialize_assets(
        dashboards,
        alerts,
        environment=args.environment,
        cluster_name=args.cluster_name,
        channel_name=args.channel_name,
    )
    client = SignozClient(args.api_url, args.api_key)
    client.wait_ready()

    if args.mode == "provision":
        receiver = build_slack_receiver(
            channel_name=args.channel_name,
            webhook_url=args.slack_webhook_url,
            environment=args.environment,
            cluster_name=args.cluster_name,
        )
        upsert_channel(client, receiver)
        upsert_dashboards(client, dashboards)
        upsert_alerts(client, alerts)
        if args.test_notification_route:
            client.request("POST", "/api/v1/channels/test", receiver, expected=(204,))

    prove_conformance(
        client,
        dashboards,
        alerts,
        environment=args.environment,
        cluster_name=args.cluster_name,
        channel_name=args.channel_name,
    )
    print(
        f"PASS: SigNoz dashboards, alerts, routes, queries, freshness, and environment separation for {args.environment}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
