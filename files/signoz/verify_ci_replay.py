#!/usr/bin/env python3
"""Read back the approved Dev CI replay from SigNoz."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sys

from signoz_observability import (
    SignozClient,
    _e2e_dashboard_names,
    _list,
    _unwrap,
    build_query_range_payload,
    load_assets,
    materialize_assets,
    validate_live_dashboard,
)


DEV_API = "https://dev.oriso.org/signoz"
CI_SERVICE = "oriso-ci-tests"
CI_SCOPE = "oriso.ci.test-results"
CI_MARKER = "oriso.signal.source"
WINDOW_START = "test.evidence.window.start"
WINDOW_END = "test.evidence.window.end"
OBSERVED_MS = "test.evidence.observed_at_ms"
DURATION = "test.duration_ms"
STRING_FIELDS = (
    "test.repository", "test.framework", "test.project", "test.commit", "test.run.id",
    "test.job.id", "test.file", "test.case", "test.id", "test.status",
    "test.evidence.origin", WINDOW_START, WINDOW_END,
)
INTEGER_FIELDS = ("test.run.attempt", "test.retry", OBSERVED_MS)
REQUIRED_FIELDS = set(STRING_FIELDS + INTEGER_FIELDS)
RAW_FIELDS = REQUIRED_FIELDS | {DURATION, CI_MARKER, "service.name", "scope_name", "id", "timestamp", "observed_timestamp"}
JOB_FIELDS = ("test.repository", "test.run.id", "test.run.attempt", "test.commit")
ROW_COUNTS = ("matched", "missing", "duplicates", "conflicts", "unexpected")
SELECT_COLUMNS = (
    ["timestamp", "observed_timestamp", "id", "scope_name", "resources_string['service.name'] AS `service.name`"]
    + [f"attributes_string['{field}'] AS `{field}`" for field in (*STRING_FIELDS, CI_MARKER)]
    + [
        f"if(mapContains(attributes_number, '{field}'), toString(attributes_number['{field}']), NULL) AS `{field}`"
        for field in (*INTEGER_FIELDS, DURATION)
    ]
)
MAX_PAYLOAD = 256 * 1024
MAX_FILES = 16
MAX_JOB_ROWS = 255
MAX_JOB_WINDOW_MS = 6 * 60 * 60 * 1000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def nanos(value):
    match = re.fullmatch(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?Z", value)
    require(match is not None, "Invalid CI observation window")
    whole, fraction = match.groups()
    elapsed = datetime.fromisoformat(whole).replace(tzinfo=timezone.utc) - EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 10**9 + int((fraction or "").ljust(9, "0"))


def positive_id(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def decode_record(record):
    require(set(record) == {"attributes", "observedTimeUnixNano"}, "Expected a sanitized CI log record")
    fields = {}
    for entry in record["attributes"]:
        key = entry["key"]
        require(key not in fields and set(entry) == {"key", "value"}, "Ambiguous CI attribute")
        kind, = entry["value"]
        value = entry["value"][kind]
        if key in STRING_FIELDS:
            require(kind == "stringValue" and isinstance(value, str) and value, "Invalid CI string attribute")
        elif key in INTEGER_FIELDS:
            require(kind == "intValue" and isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", value),
                    "Invalid CI integer attribute")
            value = int(value)
        elif key == DURATION:
            require(kind == "doubleValue" and type(value) in (int, float) and math.isfinite(value) and value >= 0,
                    "Invalid CI duration")
        else:
            raise RuntimeError("Unapproved attribute in CI payload")
        fields[key] = value
    require(REQUIRED_FIELDS.issubset(fields), "Missing CI attributes")
    require(positive_id(fields["test.job.id"]) and positive_id(fields["test.run.id"])
            and fields["test.run.attempt"] > 0 and fields["test.evidence.origin"] == "github-actions",
            "Expected a hosted CI job and run")
    require(re.fullmatch(r"[0-9a-f]{64}", fields["test.id"])
            and re.fullmatch(r"[0-9a-f]{40}", fields["test.commit"])
            and fields["test.status"] in ("passed", "failed", "skipped", "todo"),
            "Invalid CI identity or outcome")
    timestamp = record["observedTimeUnixNano"]
    require(isinstance(timestamp, str) and re.fullmatch(r"[1-9][0-9]{0,18}", timestamp), "Invalid CI timestamp")
    timestamp = int(timestamp)
    require(nanos(fields[WINDOW_START]) <= timestamp == nanos(fields[WINDOW_END])
            and fields[OBSERVED_MS] == timestamp // 1_000_000, "Inconsistent CI observation window")
    return {
        **fields, "timestamp": timestamp, "observed_timestamp": timestamp, "scope_name": CI_SCOPE,
        "service.name": CI_SERVICE, CI_MARKER: "ci",
    }


def load_jobs(paths):
    require(0 < len(paths) <= MAX_FILES, "Expected 1 to 16 sanitized CI payload files")
    jobs = defaultdict(dict)
    for path in paths:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PAYLOAD + 1)
        require(len(raw) <= MAX_PAYLOAD, "CI payload exceeds 256 KiB")
        payload = json.loads(raw)
        require(set(payload) == {"resourceLogs"}, "Expected a sanitized OTLP logs payload")
        group, = payload["resourceLogs"]
        require(set(group) == {"resource", "scopeLogs"} and group["resource"] == {
            "attributes": [{"key": "service.name", "value": {"stringValue": CI_SERVICE}}],
        }, "Unexpected CI resource")
        scope, = group["scopeLogs"]
        require(set(scope) == {"scope", "logRecords"} and scope["scope"] == {"name": CI_SCOPE}
                and scope["logRecords"], "Unexpected or empty CI log scope")
        records = [decode_record(record) for record in scope["logRecords"]]
        job_ids = {record["test.job.id"] for record in records}
        require(len(job_ids) == 1, "Each CI payload must describe one job")
        expected = jobs[job_ids.pop()]
        for row in records:
            identity = row["test.id"]
            require(identity not in expected, "Duplicate expected CI identity")
            expected[identity] = row
            require(len(expected) <= MAX_JOB_ROWS, "Too many records for one CI job")
    for rows in jobs.values():
        require(len({tuple(row[field] for field in JOB_FIELDS) for row in rows.values()}) == 1,
                "Conflicting metadata for one CI job in the replay files")
    return jobs


def read_job(client, job_id, expected):
    start = min(nanos(row[WINDOW_START]) for row in expected.values()) // 1_000_000
    end = max(row[OBSERVED_MS] for row in expected.values()) + 1
    require(0 < end - start <= MAX_JOB_WINDOW_MS and positive_id(job_id), "Invalid CI job query window")
    limit = len(expected) + 1
    # V5 rounds floating-point columns for display. ClickHouse's string form
    # round-trips the stored Float64 duration and keeps absent values distinct.
    columns = ", ".join(SELECT_COLUMNS)
    statement = f"""
SELECT {columns}
FROM signoz_logs.distributed_logs_v2
WHERE timestamp >= {start * 1_000_000} AND timestamp < {end * 1_000_000}
  AND resources_string['service.name'] = 'oriso-ci-tests'
  AND attributes_string['oriso.signal.source'] = 'ci'
  AND attributes_string['test.job.id'] = '{job_id}'
LIMIT {limit + 1}
SETTINGS max_threads=1, max_execution_time=8, max_rows_to_read=500000, max_bytes_to_read=67108864
"""
    request = build_query_range_payload(
        {"name": "A", "query": statement}, start_ms=start, end_ms=end,
        request_type="raw", query_type="clickhouse_sql",
    )
    response = client.request("POST", "/api/v5/query_range", request)
    data = response["data"]
    require(response.get("status") == "success" and data.get("type") == "raw"
            and not response.get("warning") and not data.get("warning"), "CI log query returned an incomplete response")
    result, = data["data"]["results"]
    require(result["queryName"] == "A" and result["nextCursor"] == "", "CI log query returned a paginated response")
    rows = result["rows"] if result["rows"] is not None else []
    require(isinstance(rows, list) and len(rows) <= limit, "CI log query exceeded the requested row limit")

    stored = []
    for result_row in rows:
        row = result_row["data"].copy()
        require(set(row) == RAW_FIELDS and isinstance(row.get("id"), str) and row["id"]
                and type(row.get("timestamp")) is int and type(row.get("observed_timestamp")) is int
                and isinstance(row.get("test.id"), str), "Invalid raw CI log row")
        del row["id"]
        for field in INTEGER_FIELDS:
            if row.get(field) is not None:
                require(isinstance(row[field], str), "Invalid numeric CI log field")
                row[field] = Decimal(row[field])
                require(row[field].is_finite(), "Invalid numeric CI log field")
        duration = row.pop(DURATION, None)
        if duration is not None:
            require(isinstance(duration, str), "Invalid stored CI duration")
            row[DURATION] = float(duration)
            require(math.isfinite(row[DURATION]), "Invalid stored CI duration")
        stored.append(row)
    return stored


def reconcile(expected, rows):
    remaining = set(expected)
    exact = set()
    summary = dict.fromkeys(ROW_COUNTS, 0)
    for row in rows:
        identity = row["test.id"]
        if identity not in expected:
            summary["unexpected"] += 1
        elif row != expected[identity]:
            summary["conflicts"] += 1
        elif identity in exact:
            summary["duplicates"] += 1
        else:
            exact.add(identity)
        remaining.discard(identity)
    summary["matched"] = len(exact)
    summary["missing"] = len(remaining)
    return summary


def verify_dashboards(client):
    dashboards, alerts = load_assets(str(Path(__file__).resolve().parent), include_e2e=True)
    dashboards, _ = materialize_assets(
        dashboards, alerts, environment="dev", cluster_name="oriso-dev", channel_name="oriso-alerts-slack",
    )
    titles = _e2e_dashboard_names("dev")
    expected = [dashboard for dashboard in dashboards if dashboard["name"] in titles]
    require(len(expected) == len(titles) and {dashboard["name"] for dashboard in expected} == titles,
            "Missing or ambiguous CI dashboard catalog")
    stored = _list(client, "/api/v2/dashboards", "dashboards")
    for dashboard in expected:
        matches = [item for item in stored if item.get("name") == dashboard["name"]]
        require(len(matches) == 1, "Expected exactly one stored dashboard for each managed CI title")
        identity = matches[0]["id"]
        require(isinstance(identity, str) and re.fullmatch(r"[0-9a-f-]{36}", identity), "Invalid stored dashboard ID")
        current = _unwrap(client.request("GET", f"/api/v2/dashboards/{identity}"))
        validate_live_dashboard(current, dashboard, environment="dev")
    return len(expected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", nargs="+", type=Path, help="Approved sanitized OTLP JSON file")
    parser.add_argument("--api-url", default=DEV_API, help="Exact Dev SigNoz URL or explicit local tunnel")
    args = parser.parse_args(argv)

    try:
        url = args.api_url.rstrip("/")
        local = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", url)
        require(url == DEV_API or (local and int(local[1]) <= 65535), "Use Dev SigNoz or a loopback tunnel")
        jobs = load_jobs(args.payload)
        key = os.environ.get("SIGNOZ_API_KEY")
        require(key, "SIGNOZ_API_KEY environment variable is required")
        client = SignozClient(url, key, read_only=True)
        summary = dict.fromkeys(ROW_COUNTS, 0)
        for job_id, expected in jobs.items():
            for field, count in reconcile(expected, read_job(client, job_id, expected)).items():
                summary[field] += count
        summary["expected"] = sum(map(len, jobs.values()))
        summary["dashboard_specs"] = 0
        if any(summary[field] for field in ("missing", "duplicates", "conflicts", "unexpected")):
            print(json.dumps(summary, sort_keys=True))
            return 1
        try:
            summary["dashboard_specs"] = verify_dashboards(client)
        finally:
            print(json.dumps(summary, sort_keys=True))
        return 0
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
    except (OSError, ValueError, KeyError, AttributeError, TypeError, ArithmeticError, HTTPException):
        print("FAIL: invalid replay input or incomplete SigNoz response", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
