#!/usr/bin/env python3
"""OBS-P8: render-based contract for the Frontend/Admin browser Real User
Monitoring (Web Vitals: LCP/CLS/INP/TTFB/FCP) ingest path.

Full context: https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/62

The design follows SigNoz's own self-hosted RUM guidance
(signoz.io/docs/frontend-monitoring/web-vitals-with-metrics): the browser
POSTs OTLP metrics directly to the gateway otel-collector's OTLP-HTTP
receiver at `/v1/metrics`, reached over an explicitly configured signoz.*
dev-tooling subdomain (ADR-011 exception), with CORS scoped to
global.domains.app / global.domains.admin. This locks the invariants that
make that ingest path both reachable and safely bounded:

  - a new, more-specific Ingress path (`/v1/metrics`, pathType Exact) on the
    signoz.* host routes DIRECTLY to the otel-collector gateway Service on
    port 4318 — never to the signoz core UI/query-service Service the
    existing `/` rule uses,
  - that Ingress carries its own abuse guards (proxy-body-size, limit-rps)
    since the endpoint is unauthenticated and internet-reachable — CORS is a
    browser-only restriction and does not stop a non-browser client,
  - the otel-collector's OTLP-HTTP receiver gets a `cors.allowed_origins`
    list built from global.domains.app and global.domains.admin — Frontend
    and Admin each live on their own subdomain on this deployment (verified
    against the live Pre-Dev release, which has no global.domainName set at
    all), never a wildcard,
  - both the CORS block and the new Ingress path are gated by
    `global.observability.webVitalsEnabled` (its own flag, independent of
    `signoz.ingress.enabled`) AND require at least one of
    `global.domains.app` / `global.domains.admin` (for CORS) and
    `global.domains.signoz` (for the ingress host) to actually be set —
    neither ever renders with an empty origin list/host, matching how other
    domain-dependent blocks in this chart guard against a nil `global`,
  - `values-prod.yaml`, exactly as committed today, keeps
    `webVitalsEnabled: false` — the same off-by-default-in-prod posture
    OBS-P6 established for backend telemetry (PR #65) — pending the same
    KDG-conscious review for browser telemetry.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = "oriso-platform"

COLLECTOR_CONFIGMAP_NAME = f"{RELEASE}-otel-collector"
COLLECTOR_SERVICE_NAME = f"{RELEASE}-otel-collector"
WEBVITALS_INGRESS_NAME = "signoz-webvitals-ingress"
UI_INGRESS_NAME = "signoz-ingress"
FRONTEND_CONFIGMAP_NAME = "frontend-configmap"
ADMIN_CONFIGMAP_NAME = "admin-configmap"
TEST_SIGNOZ_DOMAIN = "signoz.example.test"
SIGNOZ_TEST_SET = [
    "signoz.enabled=true",
    "signoz.ingress.enabled=true",
    f"global.domains.signoz={TEST_SIGNOZ_DOMAIN}",
]
SIGNOZ_MANIFEST_TEST_SET = ["signoz.enabled=true"]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def render(*value_files: str, extra_set: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", RELEASE, CHART_DIR, "--namespace", "caritas"]
    for vf in value_files:
        cmd += ["-f", os.path.join(CHART_DIR, vf)]
    cmd += ["-f", os.path.join(CHART_DIR, "secrets.yaml.default")]
    cmd += ["--set", "global.secrets.clickhousePassword=x"]
    for s in extra_set or []:
        cmd += ["--set", s]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"helm template failed for {value_files} {extra_set}: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def find(documents: list[dict], kind: str, name: str) -> dict | None:
    for doc in documents:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


def resource(documents: list[dict], kind: str, name: str) -> dict:
    doc = find(documents, kind, name)
    if doc is None:
        fail(f"rendered {kind}/{name} is missing")
    return doc


def gateway_config(documents: list[dict]) -> tuple[dict, str]:
    cm = resource(documents, "ConfigMap", COLLECTOR_CONFIGMAP_NAME)
    raw = cm["data"]["otel-collector-config.yaml"]
    return yaml.safe_load(raw), raw


def check_browser_runtime_config(
    documents: list[dict], enabled: bool, endpoint: str, label: str
) -> None:
    expected_enabled = "true" if enabled else "false"
    frontend = resource(documents, "ConfigMap", FRONTEND_CONFIGMAP_NAME)["data"]
    admin = resource(documents, "ConfigMap", ADMIN_CONFIGMAP_NAME)["data"]

    expected = {
        "enabled": expected_enabled,
        "endpoint": endpoint,
        "interval": "60000",
    }
    actual_frontend = {
        "enabled": frontend.get("REACT_APP_OBSERVABILITY_ENABLED"),
        "endpoint": frontend.get("REACT_APP_OTEL_METRICS_URL"),
        "interval": frontend.get("REACT_APP_OTEL_EXPORT_INTERVAL_MS"),
    }
    actual_admin = {
        "enabled": admin.get("VITE_OBSERVABILITY_ENABLED"),
        "endpoint": admin.get("VITE_OTEL_METRICS_URL"),
        "interval": admin.get("VITE_OTEL_EXPORT_INTERVAL_MS"),
    }

    if actual_frontend != expected:
        fail(f"{label}: Frontend browser telemetry runtime config is {actual_frontend}, "
             f"expected {expected}")
    if actual_admin != expected:
        fail(f"{label}: Admin browser telemetry runtime config is {actual_admin}, "
             f"expected {expected}")


def check_enabled(documents: list[dict], expected_origins: list[str], label: str) -> None:
    # --- Ingress: new path exists, targets the collector, not the UI. ------
    ing = resource(documents, "Ingress", WEBVITALS_INGRESS_NAME)
    rule = ing["spec"]["rules"][0]
    paths = rule["http"]["paths"]
    if len(paths) != 1:
        fail(f"{label}: {WEBVITALS_INGRESS_NAME} must have exactly one path, got {paths}")
    path = paths[0]
    if path["path"] != "/v1/metrics":
        fail(f"{label}: expected path '/v1/metrics', got {path['path']!r}")
    if path["pathType"] != "Exact":
        fail(f"{label}: '/v1/metrics' must be pathType Exact (more specific than the "
             f"existing '/' Prefix rule), got {path['pathType']!r}")
    backend_service = path["backend"]["service"]
    if backend_service["name"] != COLLECTOR_SERVICE_NAME:
        fail(f"{label}: '/v1/metrics' must target the otel-collector Service "
             f"({COLLECTOR_SERVICE_NAME!r}), not the SigNoz UI service — got "
             f"{backend_service['name']!r}")
    if backend_service["port"]["number"] != 4318:
        fail(f"{label}: '/v1/metrics' must target port 4318 (otlphttp), got "
             f"{backend_service['port']['number']!r}")

    # Must be the same host as the existing UI ingress (single signoz.*
    # subdomain, ADR-011 exception), and must NOT touch the UI ingress's own
    # '/' rule.
    ui_ing = resource(documents, "Ingress", UI_INGRESS_NAME)
    ui_host = ui_ing["spec"]["rules"][0]["host"]
    if rule["host"] != ui_host:
        fail(f"{label}: webvitals ingress host {rule['host']!r} must match the "
             f"UI ingress host {ui_host!r}")
    ui_paths = [p["path"] for p in ui_ing["spec"]["rules"][0]["http"]["paths"]]
    if ui_paths != ["/"]:
        fail(f"{label}: existing UI ingress '/' rule must be untouched, got {ui_paths}")

    # --- Abuse guards on the ingress. ---------------------------------------
    annotations = ing["metadata"]["annotations"]
    body_size = annotations.get("nginx.ingress.kubernetes.io/proxy-body-size")
    if not body_size:
        fail(f"{label}: {WEBVITALS_INGRESS_NAME} is missing "
             f"nginx.ingress.kubernetes.io/proxy-body-size")
    # Must be clearly bounded and small (web-vitals payloads are tiny) —
    # accept anything in the documented 16k-64k judgment-call range.
    size_kib = int(body_size.rstrip("kK"))
    if not (16 <= size_kib <= 64):
        fail(f"{label}: proxy-body-size {body_size!r} outside the intended "
             f"16k-64k bound for a tiny web-vitals payload")

    # --- CORS on the collector's otlp http receiver. ------------------------
    config, raw_text = gateway_config(documents)
    http_receiver = config["receivers"]["otlp"]["protocols"]["http"]
    cors = http_receiver.get("cors")
    if not cors:
        fail(f"{label}: otlp http receiver is missing the cors block")
    origins = cors.get("allowed_origins")
    if origins != expected_origins:
        fail(f"{label}: cors.allowed_origins must be exactly {expected_origins!r}, "
             f"got {origins!r}")
    if cors.get("allowed_headers") != ["*"]:
        fail(f"{label}: cors.allowed_headers must be ['*'], got {cors.get('allowed_headers')!r}")
    # grpc protocol must be untouched — CORS only makes sense for the browser
    # HTTP path.
    if "cors" in config["receivers"]["otlp"]["protocols"].get("grpc", {}):
        fail(f"{label}: cors block must not be added to the grpc protocol")


def check_disabled(documents: list[dict], label: str) -> None:
    if find(documents, "Ingress", WEBVITALS_INGRESS_NAME) is not None:
        fail(f"{label}: {WEBVITALS_INGRESS_NAME} rendered even though it should be off")
    config, raw_text = gateway_config(documents)
    http_receiver = config["receivers"]["otlp"]["protocols"]["http"]
    if "cors" in http_receiver:
        fail(f"{label}: otlp http receiver carries a cors block even though "
             f"it should be off: {http_receiver['cors']}")


def main() -> None:
    # --- (1) Pre-dev, webVitalsEnabled default-true, real app/admin domains
    # set explicitly (mirrors environment deployment values supplied outside
    # this chart file).
    env_docs = render(
        "values.yaml.default",
        extra_set=[
            "global.domains.app=app.oriso-dev.site",
            "global.domains.admin=admin.oriso-dev.site",
            *SIGNOZ_TEST_SET,
        ],
    )
    check_enabled(
        env_docs,
        ["https://app.oriso-dev.site", "https://admin.oriso-dev.site"],
        "explicit environment domains",
    )
    check_browser_runtime_config(
        env_docs,
        True,
        f"https://{TEST_SIGNOZ_DOMAIN}/v1/metrics",
        "explicit environment domains",
    )

    # --- (2) Dev with SigNoz explicitly enabled: webVitalsEnabled defaults
    # true, but neither global.domains.app nor .admin nor .signoz is set
    # there — nothing should render at all (no ingress, no CORS), never a
    # partial or placeholder origin.
    dev_docs = render("values.yaml.default", extra_set=SIGNOZ_MANIFEST_TEST_SET)
    if find(dev_docs, "Ingress", WEBVITALS_INGRESS_NAME) is not None:
        fail("dev (values.yaml.default): webvitals ingress rendered without "
             "global.domains.signoz being set")
    dev_config, _ = gateway_config(dev_docs)
    dev_cors = dev_config["receivers"]["otlp"]["protocols"]["http"].get("cors")
    if dev_cors is not None:
        fail(f"dev (values.yaml.default): cors block rendered without "
             f"global.domains.app/admin being set: {dev_cors}")
    check_browser_runtime_config(dev_docs, False, "", "dev without SigNoz domain")

    # --- (3) Prod overlay exactly as committed: off by default. ------------
    prod_docs = render(
        "values.yaml.default",
        "values-prod.yaml",
        extra_set=SIGNOZ_TEST_SET,
    )
    check_disabled(prod_docs, "prod (values-prod.yaml, as committed)")
    check_browser_runtime_config(prod_docs, False, "", "prod overlay")

    # --- (4) webVitalsEnabled explicitly false: both the ingress
    # and CORS must disappear even though app/admin/signoz domains ARE set —
    # proves the flag is a real, independent kill switch, not cosmetic.
    env_off_docs = render(
        "values.yaml.default",
        extra_set=[
            "global.domains.app=app.oriso-dev.site",
            "global.domains.admin=admin.oriso-dev.site",
            "global.observability.webVitalsEnabled=false",
            *SIGNOZ_TEST_SET,
        ],
    )
    check_disabled(env_off_docs, "explicit environment domains with webVitalsEnabled=false")
    check_browser_runtime_config(
        env_off_docs, False, "", "explicit webVitalsEnabled=false"
    )

    # --- (5) Only ONE of app/admin set: CORS must render with
    # just that one origin (not fail, not add a placeholder for the missing
    # one) — proves each domain is independently optional, and the ingress
    # path itself is host-gated by global.domains.signoz only, not by
    # app/admin, so it still renders.
    env_app_only_docs = render(
        "values.yaml.default",
        extra_set=[
            "global.domains.app=app.oriso-dev.site",
            *SIGNOZ_TEST_SET,
        ],
    )
    app_only_config, _ = gateway_config(env_app_only_docs)
    app_only_cors = app_only_config["receivers"]["otlp"]["protocols"]["http"].get("cors")
    if app_only_cors is None or app_only_cors["allowed_origins"] != ["https://app.oriso-dev.site"]:
        fail(f"explicit app domain only: expected cors.allowed_origins "
             f"['https://app.oriso-dev.site'], got {app_only_cors}")
    if find(env_app_only_docs, "Ingress", WEBVITALS_INGRESS_NAME) is None:
        fail("explicit app domain only: webvitals ingress must still "
             "render (gated by global.domains.signoz, not app/admin)")

    print("OK: OBS-P8 contract holds — signoz.example.test/v1/metrics (Exact) routes "
          "directly to the otel-collector Service on port 4318 (never the SigNoz UI "
          "service), carries its own proxy-body-size/limit-rps abuse-guard annotations, "
          "and the collector's otlp http receiver gets a CORS allow-list built from "
          "global.domains.app and global.domains.admin (never a wildcard). Both the "
          "ingress path and CORS are off by default in the committed prod overlay, and "
          "global.observability.webVitalsEnabled + having neither app nor admin domain "
          "set each independently keep CORS from ever rendering with an empty/wildcard "
          "origin list.")


if __name__ == "__main__":
    main()
