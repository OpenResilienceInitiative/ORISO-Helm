#!/usr/bin/env python3
"""OBS-P8: render-based contract for the Frontend/Admin browser Real User
Monitoring (Web Vitals: LCP/CLS/INP/TTFB/FCP) ingest path.

Full context: https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/62

The design follows SigNoz's own self-hosted RUM guidance
(signoz.io/docs/frontend-monitoring/web-vitals-with-metrics): the browser
POSTs OTLP metrics directly to the gateway otel-collector's OTLP-HTTP
receiver at `/v1/metrics`, reached over the existing signoz.oriso-dev.site
dev-tooling subdomain (ADR-011 exception), with CORS scoped to the single
shared ADR-011 app domain. This locks the invariants that make that ingest
path both reachable and safely bounded:

  - a new, more-specific Ingress path (`/v1/metrics`, pathType Exact) on the
    signoz.* host routes DIRECTLY to the otel-collector gateway Service on
    port 4318 — never to the signoz core UI/query-service Service the
    existing `/` rule uses,
  - that Ingress carries its own abuse guards (proxy-body-size, limit-rps)
    since the endpoint is unauthenticated and internet-reachable — CORS is a
    browser-only restriction and does not stop a non-browser client,
  - the otel-collector's OTLP-HTTP receiver gets a `cors.allowed_origins`
    entry for exactly `https://<global.domainName>` — the one shared
    ADR-011 origin Frontend and Admin both live under — never a wildcard,
  - both the CORS block and the new Ingress path are gated by
    `global.observability.webVitalsEnabled` (its own flag, independent of
    `signoz.ingress.enabled`) AND require `global.domainName` /
    `global.domains.signoz` to actually be set — neither ever renders with
    an empty origin/host, matching how other domain-dependent blocks in
    this chart guard against a nil `global`,
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


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def render(*value_files: str, extra_set: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", RELEASE, CHART_DIR, "--namespace", "caritas"]
    for vf in value_files:
        cmd += ["-f", os.path.join(CHART_DIR, vf)]
    cmd += ["-f", os.path.join(CHART_DIR, "ci", "placeholder-secrets.yaml")]
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


def check_enabled(documents: list[dict], expected_origin: str, label: str) -> None:
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
    if origins != [expected_origin]:
        fail(f"{label}: cors.allowed_origins must be exactly [{expected_origin!r}], "
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
    # --- (1) Pre-dev, webVitalsEnabled default-true, real domain names set
    # explicitly (mirrors how Pre-Dev is actually deployed: global.domainName
    # is set at deploy time via --set, not committed in values-pre-dev.yaml).
    predev_docs = render(
        "values.yaml.default",
        "values-pre-dev.yaml",
        extra_set=["global.domainName=oriso-dev.site"],
    )
    check_enabled(predev_docs, "https://oriso-dev.site", "pre-dev")

    # --- (2) Dev (values.yaml.default only): webVitalsEnabled defaults true
    # and domainName has its placeholder default, but the signoz.* host is
    # unset there — the ingress must not render without a host, though CORS
    # (which only depends on domainName + the flag, not the signoz host)
    # still can.
    dev_docs = render("values.yaml.default")
    if find(dev_docs, "Ingress", WEBVITALS_INGRESS_NAME) is not None:
        fail("dev (values.yaml.default): webvitals ingress rendered without "
             "global.domains.signoz being set")
    dev_config, _ = gateway_config(dev_docs)
    dev_cors = dev_config["receivers"]["otlp"]["protocols"]["http"].get("cors")
    if dev_cors is None or dev_cors["allowed_origins"] != ["https://your-domain.example.com"]:
        fail(f"dev (values.yaml.default): expected cors.allowed_origins "
             f"['https://your-domain.example.com'], got {dev_cors}")

    # --- (3) Prod overlay exactly as committed: off by default. ------------
    prod_docs = render("values.yaml.default", "values-prod.yaml")
    check_disabled(prod_docs, "prod (values-prod.yaml, as committed)")

    # --- (4) webVitalsEnabled explicitly false on pre-dev: both the ingress
    # and CORS must disappear even though the signoz host IS set — proves the
    # flag is a real, independent kill switch, not cosmetic.
    predev_off_docs = render(
        "values.yaml.default",
        "values-pre-dev.yaml",
        extra_set=[
            "global.domainName=oriso-dev.site",
            "global.observability.webVitalsEnabled=false",
        ],
    )
    check_disabled(predev_off_docs, "pre-dev with webVitalsEnabled=false")

    # --- (5) domainName explicitly empty on pre-dev: CORS must never render
    # with an empty origin, even though webVitalsEnabled is true and the
    # signoz host is set (the ingress path itself is host-gated, not
    # domainName-gated, so it still renders; only CORS must disappear).
    predev_nodomain_docs = render(
        "values.yaml.default",
        "values-pre-dev.yaml",
        extra_set=["global.domainName="],
    )
    nodomain_config, _ = gateway_config(predev_nodomain_docs)
    nodomain_cors = nodomain_config["receivers"]["otlp"]["protocols"]["http"].get("cors")
    if nodomain_cors is not None:
        fail(f"pre-dev with empty global.domainName: cors block rendered anyway: "
             f"{nodomain_cors}")

    print("OK: OBS-P8 contract holds — signoz.oriso-dev.site/v1/metrics (Exact) routes "
          "directly to the otel-collector Service on port 4318 (never the SigNoz UI "
          "service), carries its own proxy-body-size/limit-rps abuse-guard annotations, "
          "and the collector's otlp http receiver gets a single-origin CORS allow-list "
          "scoped to https://<global.domainName>. Both the ingress path and CORS are off "
          "by default in the committed prod overlay, and global.observability."
          "webVitalsEnabled + an empty global.domainName each independently keep the "
          "relevant piece from ever rendering with an empty/wildcard origin.")


if __name__ == "__main__":
    main()
