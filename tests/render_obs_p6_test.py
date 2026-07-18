#!/usr/bin/env python3
"""OBS-P6: render-based contract for the KDG-safe prod telemetry
pseudonymization pipeline.

Full design rationale: docs/observability-prod-pseudonymization.md.

This locks the invariants that make the pipeline actually safe to eventually
turn on for prod, and — just as importantly — that it stays OFF by default
everywhere today (this is infrastructure for a future human/legal decision,
not something this chart enables on its own):

  - default values (values.yaml.default) and the prod overlay
    (values-prod.yaml) both keep telemetryPseudonymizationEnabled=false;
    no transform/pseudonymize_* processor, no PSEUDONYM_HMAC_KEY env var,
    and no pseudonymization Secret render in either case,
  - when the flag IS turned on (--set, simulating a future decision), the
    gateway collector config gains transform/pseudonymize_traces,
    transform/pseudonymize_metrics and transform/pseudonymize_logs, each
    wired into its pipeline immediately after memory_limiter (before
    k8sattributes or anything else touches the data) and each with
    error_mode: propagate (a processing error drops the data — the
    alternative, "ignore", would pass the ORIGINAL unpseudonymized record
    through on error, which must never happen),
  - the fail-closed allow-list (keep_keys) for every pipeline never lists a
    raw identifying field verbatim — only its `*.hash` counterpart. This is
    the render-time proxy for "a raw identifier value never leaves the
    pod": a Helm render only produces the collector's PROCESSING RULES, it
    never contains live runtime telemetry data, so the strongest check
    available at this layer is structural — confirm the rule set itself
    can never pass an identifying field through un-hashed,
  - every raw identifying field that gets hashed is also explicitly
    delete_key'd (belt-and-suspenders alongside the keep_keys drop),
  - prod metrics carry no high-cardinality per-user/session label in the
    allow-list — only aggregate/low-cardinality dimensions,
  - the HMAC salt reaches the collector via secretKeyRef (never a
    plaintext env value or ConfigMap), mirroring the ClickHouse credential
    pattern from OBS-P1 (LC-M03).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLLECTOR_CONFIGMAP_NAME = "oriso-platform-otel-collector"
COLLECTOR_DEPLOYMENT_NAME = "oriso-platform-otel-collector"
COLLECTOR_SECRET_NAME = "oriso-platform-otel-collector-pseudonymization"
SIGNOZ_TEST_SET = [
    "signoz.enabled=true",
    "signoz.otelAgent.enabled=true",
    "signoz.otelAgent.logsNamespace=caritas",
]

TRANSFORM_PROCESSORS = (
    "transform/pseudonymize_traces",
    "transform/pseudonymize_metrics",
    "transform/pseudonymize_logs",
)

# Raw identifier field names that must NEVER appear un-hashed in an
# allow-list (keep_keys) anywhere in the rendered config. Mirrors the actual
# OBS-P2 correlation surface (CorrelationIdFilter: header X-Correlation-ID,
# MDC key CID) plus the standard OTel semantic-convention identifier names
# a future custom attribute would plausibly use.
RAW_IDENTIFIER_KEYS = (
    "user.id",
    "enduser.id",
    "agency.id",
    "tenant.id",
    "session.id",
    "cid",
)

# A concrete "sample raw identifier value" fixture: stands in for a real
# user/session ID that must never appear verbatim in the rendered config.
# (The collector config only ever contains PROCESSING RULES, never live
# telemetry data, so this constant is used to make explicit in the test
# itself what "a raw identifier value" would look like — see module
# docstring for why the enforceable check is structural, not textual.)
SAMPLE_RAW_IDENTIFIER_VALUE = "user-12345-real-identity"

# High-cardinality / identifying label names that must never appear in the
# prod metrics datapoint allow-list.
HIGH_CARDINALITY_METRIC_LABELS = (
    "user.id",
    "user_id",
    "userId",
    "enduser.id",
    "session.id",
    "session_id",
    "sessionId",
    "agency.id",
    "agency_id",
    "tenant.id",
    "tenant_id",
    "cid",
    "correlation.id",
    "correlation_id",
    "net.peer.ip",
    "client.address",
)


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def render(*value_files: str, extra_set: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "oriso-platform", CHART_DIR, "--namespace", "caritas"]
    for vf in value_files:
        cmd += ["-f", os.path.join(CHART_DIR, vf)]
    cmd += ["-f", os.path.join(CHART_DIR, "secrets.yaml.default")]
    cmd += ["--set", "global.secrets.clickhousePassword=x"]
    for kv in SIGNOZ_TEST_SET:
        cmd += ["--set", kv]
    if extra_set:
        for kv in extra_set:
            cmd += ["--set", kv]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"helm template failed for {value_files} {extra_set}: {result.stderr}")
    return list(yaml.safe_load_all(result.stdout))


def find(docs: list[dict], kind: str, name: str) -> dict | None:
    for doc in docs:
        if doc and doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


def get_collector_config(docs: list[dict]) -> dict:
    cm = find(docs, "ConfigMap", COLLECTOR_CONFIGMAP_NAME)
    if cm is None:
        fail(f"ConfigMap {COLLECTOR_CONFIGMAP_NAME} not found in render output")
        raise AssertionError  # unreachable
    raw = cm["data"]["otel-collector-config.yaml"]
    return yaml.safe_load(raw), raw


def check_off_by_default(docs: list[dict], label: str) -> None:
    config, raw_text = get_collector_config(docs)
    for proc in TRANSFORM_PROCESSORS:
        if proc in config.get("processors", {}):
            fail(f"{label}: processor {proc!r} rendered even though "
                 f"telemetryPseudonymizationEnabled defaults to false")
        for pipeline_name, pipeline in config["service"]["pipelines"].items():
            if proc in pipeline.get("processors", []):
                fail(f"{label}: pipeline {pipeline_name!r} references {proc!r} "
                     f"even though the flag is off")
    if "PSEUDONYM_HMAC_KEY" in raw_text:
        fail(f"{label}: PSEUDONYM_HMAC_KEY env-var reference leaked into the "
             f"collector config while the flag is off")
    secret = find(docs, "Secret", COLLECTOR_SECRET_NAME)
    if secret is not None:
        fail(f"{label}: pseudonymization Secret {COLLECTOR_SECRET_NAME!r} "
             f"rendered even though the flag is off — it must only exist "
             f"when telemetryPseudonymizationEnabled=true")
    deployment = find(docs, "Deployment", COLLECTOR_DEPLOYMENT_NAME)
    if deployment is not None:
        env_names = {
            e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
        }
        if "PSEUDONYM_HMAC_KEY" in env_names:
            fail(f"{label}: otel-collector Deployment wires PSEUDONYM_HMAC_KEY "
                 f"even though the flag is off")


def extract_keep_keys_lists(raw_text: str) -> list[str]:
    """Return the raw text of every keep_keys(...) call's key-list argument."""
    return re.findall(r"keep_keys\([^,]+,\s*(\[[^\]]*\])\)", raw_text)


def check_enabled_pipeline(docs: list[dict]) -> None:
    config, raw_text = get_collector_config(docs)
    processors = config.get("processors", {})

    for proc in TRANSFORM_PROCESSORS:
        if proc not in processors:
            fail(f"processor {proc!r} missing when telemetryPseudonymizationEnabled=true")
        if processors[proc].get("error_mode") != "propagate":
            fail(f"{proc}: error_mode must be 'propagate' (fail-closed — 'ignore' "
                 f"would pass the original unpseudonymized record through on error), "
                 f"got {processors[proc].get('error_mode')!r}")

    expected_wiring = {
        "traces": "transform/pseudonymize_traces",
        "metrics": "transform/pseudonymize_metrics",
        "logs": "transform/pseudonymize_logs",
    }
    for pipeline_name, proc_name in expected_wiring.items():
        pipeline_processors = config["service"]["pipelines"][pipeline_name]["processors"]
        if proc_name not in pipeline_processors:
            fail(f"pipeline {pipeline_name!r} does not reference {proc_name!r}")
        mem_idx = pipeline_processors.index("memory_limiter")
        proc_idx = pipeline_processors.index(proc_name)
        if proc_idx != mem_idx + 1:
            fail(f"pipeline {pipeline_name!r}: {proc_name!r} must run immediately "
                 f"after memory_limiter (position {mem_idx + 1}), so nothing else "
                 f"in the pipeline ever sees the raw identifiers — got position {proc_idx}")

    # (b) A raw identifier field must never appear un-hashed in any
    # allow-list — only its ".hash" counterpart may appear. This is the
    # render-time proxy for "a sample raw identifier value never leaves the
    # pod": since a Helm render only contains processing RULES, not live
    # data, we assert the rule set structurally cannot pass one through.
    keep_keys_lists = extract_keep_keys_lists(raw_text)
    if not keep_keys_lists:
        fail("no keep_keys(...) allow-list found anywhere in the rendered "
             "config — the fail-closed allow-list mechanism is missing")
    for key_list_text in keep_keys_lists:
        for raw_key in RAW_IDENTIFIER_KEYS:
            # Match the exact quoted key, not a ".hash"-suffixed variant
            # (e.g. must not match "user.id" as a substring of "user.id.hash").
            if re.search(rf'"{re.escape(raw_key)}"(?!\.hash")', key_list_text):
                fail(f"raw identifier key {raw_key!r} appears verbatim in an "
                     f"allow-list ({key_list_text}) — it must only ever be "
                     f"exported as its hashed counterpart")
    if SAMPLE_RAW_IDENTIFIER_VALUE in raw_text:
        fail(f"literal sample identifier value {SAMPLE_RAW_IDENTIFIER_VALUE!r} "
             f"found in rendered config — should never appear, config carries "
             f"only processing rules")

    # Every raw identifier that gets a ".hash" companion must also be
    # explicitly delete_key'd (belt-and-suspenders alongside keep_keys).
    for raw_key in RAW_IDENTIFIER_KEYS:
        if f'delete_key(attributes, "{raw_key}")' not in raw_text:
            fail(f"no delete_key(attributes, \"{raw_key}\") statement found — "
                 f"the raw field is only protected by keep_keys, not also "
                 f"explicitly removed")

    # (c) Prod metrics: no high-cardinality / identifying label anywhere in
    # the metrics datapoint allow-list.
    metrics_proc = processors["transform/pseudonymize_metrics"]
    metrics_text = yaml.dump(metrics_proc)
    for label in HIGH_CARDINALITY_METRIC_LABELS:
        if re.search(rf'"{re.escape(label)}"', metrics_text) and "keep_keys" in metrics_text:
            # Only a problem if it shows up inside a keep_keys allow-list
            # rather than a delete_key statement.
            for key_list_text in extract_keep_keys_lists(yaml.dump(metrics_proc)):
                if f'"{label}"' in key_list_text:
                    fail(f"prod metrics allow-list includes high-cardinality/"
                         f"identifying label {label!r}: {key_list_text}")

    # HMAC salt must come from a Secret via secretKeyRef, never a literal.
    secret = find(docs, "Secret", COLLECTOR_SECRET_NAME)
    if secret is None:
        fail(f"Secret {COLLECTOR_SECRET_NAME!r} not rendered even though the "
             f"flag is on")
    if "hmacKey" not in secret.get("data", {}):
        fail(f"Secret {COLLECTOR_SECRET_NAME!r} missing data key 'hmacKey'")

    deployment = find(docs, "Deployment", COLLECTOR_DEPLOYMENT_NAME)
    if deployment is None:
        fail(f"Deployment {COLLECTOR_DEPLOYMENT_NAME!r} not found")
    env_list = deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
    hmac_env = next((e for e in env_list if e["name"] == "PSEUDONYM_HMAC_KEY"), None)
    if hmac_env is None:
        fail("otel-collector Deployment does not wire PSEUDONYM_HMAC_KEY")
    if "value" in hmac_env:
        fail("PSEUDONYM_HMAC_KEY is set as a plaintext env value, not via secretKeyRef "
             "— the HMAC salt must never be a literal in the Deployment spec")
    secret_ref = hmac_env.get("valueFrom", {}).get("secretKeyRef", {})
    if secret_ref.get("name") != COLLECTOR_SECRET_NAME or secret_ref.get("key") != "hmacKey":
        fail(f"PSEUDONYM_HMAC_KEY secretKeyRef does not point at "
             f"{COLLECTOR_SECRET_NAME}/hmacKey, got {secret_ref}")


def main() -> None:
    dev_docs = render("values.yaml.default")
    check_off_by_default(dev_docs, "dev (values.yaml.default)")

    prod_docs = render("values.yaml.default", "values-prod.yaml")
    check_off_by_default(prod_docs, "prod (values-prod.yaml) — must stay off pending sign-off")

    # Simulates a future human decision turning the pipeline on. Not
    # something values-prod.yaml does by itself today.
    enabled_docs = render(
        "values.yaml.default",
        "values-prod.yaml",
        extra_set=[
            "global.observability.telemetryPseudonymizationEnabled=true",
            "global.secrets.telemetryPseudonymizationHmacKey=test-hmac-salt-not-a-real-secret",
        ],
    )
    check_enabled_pipeline(enabled_docs)

    print("OK: OBS-P6 contract holds — pseudonymization pipeline stays off by default "
          "on default/prod; when explicitly enabled, all three pipelines (traces/"
          "metrics/logs) fail-closed (error_mode: propagate), pseudonymize immediately "
          "after memory_limiter, never allow-list a raw identifier verbatim, keep prod "
          "metrics free of high-cardinality labels, and source the HMAC salt from a "
          "Secret via secretKeyRef.")


if __name__ == "__main__":
    main()
