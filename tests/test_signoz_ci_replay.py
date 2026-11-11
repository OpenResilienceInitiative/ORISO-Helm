from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files" / "signoz"))
from signoz_observability import SignozClient, SignozHttpError
from verify_ci_replay import INTEGER_FIELDS, load_jobs, read_job, reconcile


NANOSECONDS = 1794223892305079000
JOB_ID = "129132446123"
CASE_ID = "a" * 64


def payload(job=JOB_ID, case=CASE_ID):
    strings = {
        "test.repository": "OpenResilienceInitiative/ORISO-UserService", "test.framework": "surefire",
        "test.project": "unit", "test.commit": "b" * 40, "test.run.id": "42609068508", "test.job.id": job,
        "test.file": "src/test/java/ExampleTest.java", "test.case": "example", "test.id": case, "test.status": "passed",
        "test.evidence.origin": "github-actions", "test.evidence.window.start": "2026-11-09T11:26:41.493199Z",
        "test.evidence.window.end": "2026-11-09T11:31:32.305079Z",
    }
    attrs = [{"key": key, "value": {"stringValue": value}} for key, value in strings.items()]
    attrs.extend([
        {"key": "test.run.attempt", "value": {"intValue": "1"}},
        {"key": "test.retry", "value": {"intValue": "0"}},
        {"key": "test.evidence.observed_at_ms", "value": {"intValue": "1794223892305"}},
    ])
    return {"resourceLogs": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "oriso-ci-tests"}}]},
        "scopeLogs": [{"scope": {"name": "oriso.ci.test-results"}, "logRecords": [{
            "observedTimeUnixNano": str(NANOSECONDS), "attributes": attrs,
        }]}],
    }]}


class QueryClient:
    def __init__(self, rows, *, warning=None, cursor=""):
        self.rows, self.warning, self.cursor = rows, warning, cursor

    def request(self, method, path, request):
        self.call = (method, path, request)
        data = {"type": "raw", "data": {"results": [{
            "queryName": "A", "nextCursor": self.cursor, "rows": [{"data": row} for row in self.rows],
        }]}}
        if self.warning:
            data["warning"] = self.warning
        return {"status": "success", "data": data}


class VerifyCiReplayTest(TestCase):
    def load(self, *inputs):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index, body in enumerate(inputs):
                path = Path(tmp) / f"{index}.json"
                path.write_text(json.dumps(body))
                paths.append(path)
            return load_jobs(paths)

    def test_exact_rows_include_the_full_nanosecond_timestamp_and_optional_duration(self):
        job = self.load(payload())[JOB_ID]
        expected = job[CASE_ID]
        row = {**expected, "id": "native-id", "test.duration_ms": None}
        row.update({field: str(row[field]) for field in INTEGER_FIELDS})
        client = QueryClient([row])
        rows = read_job(client, JOB_ID, job)
        self.assertEqual(rows, [expected])
        self.assertEqual(expected["timestamp"], NANOSECONDS)
        method, path, query = client.call
        self.assertEqual((method, path), ("POST", "/api/v5/query_range"))
        self.assertEqual((query["start"], query["end"]), (1794223601493, 1794223892306))
        raw = query["compositeQuery"]["queries"][0]
        self.assertEqual(raw["type"], "clickhouse_sql")
        statement = raw["spec"]["query"]
        self.assertIn("LIMIT 3", statement)
        self.assertIn("attributes_string['test.job.id'] = '129132446123'", statement)
        self.assertIn("attributes_string['oriso.signal.source'] = 'ci'", statement)
        self.assertIn("toString(attributes_number['test.duration_ms'])", statement)
        self.assertNotIn("body", statement)

    def test_missing_duplicate_conflicting_and_unexpected_rows_are_distinct(self):
        other = "c" * 64
        third = "d" * 64
        job = self.load(payload(), payload(case=other), payload(case=third))[JOB_ID]
        good = job[CASE_ID]
        mismatch = {**job[other], "timestamp": NANOSECONDS - 1}
        extra = {**good, "test.id": "e" * 64}
        self.assertEqual(reconcile(job, [good, good, mismatch, extra]), {
            "matched": 1, "missing": 1, "duplicates": 1, "conflicts": 1, "unexpected": 1,
        })

    def test_durations_compare_at_the_stored_float_precision(self):
        expected_payload = payload()
        attrs = expected_payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]
        duration = 685.9806420000023
        attrs.append({"key": "test.duration_ms", "value": {"doubleValue": duration}})
        job = self.load(expected_payload)[JOB_ID]
        stored = {**job[CASE_ID], "id": "native-id"}
        stored.update({field: str(stored[field]) for field in (*INTEGER_FIELDS, "test.duration_ms")})
        self.assertEqual(reconcile(job, read_job(QueryClient([stored]), JOB_ID, job))["matched"], 1)
        stored["test.duration_ms"] = "685.981"
        self.assertEqual(reconcile(job, read_job(QueryClient([stored]), JOB_ID, job))["conflicts"], 1)

    def test_warning_cursor_or_excess_response_is_inconclusive(self):
        job = self.load(payload())[JOB_ID]
        for client in (
            QueryClient([], warning={"message": "unregistered field"}),
            QueryClient([], cursor="has-next-page"),
            QueryClient([{}, {}, {}]),
        ):
            with self.subTest(client=client), self.assertRaises(RuntimeError):
                read_job(client, JOB_ID, job)

    def test_empty_response_with_a_valid_envelope_proves_no_rows(self):
        job = self.load(payload())[JOB_ID]
        client = QueryClient([])
        self.assertEqual(read_job(client, JOB_ID, job), [])
        self.assertEqual(reconcile(job, [])["missing"], 1)

    def test_unapproved_payload_shape_and_duplicate_input_fail_before_querying(self):
        with self.assertRaises(RuntimeError):
            self.load(payload(), payload())
        with self.assertRaises(RuntimeError):
            self.load(payload(job="1' OR TRUE"))
        invalid = payload()
        invalid["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"] = {"stringValue": "private"}
        with self.assertRaises(RuntimeError):
            self.load(invalid)


    def test_bounded_client_rejects_redirects_unapproved_requests_and_error_details(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/unexpected")
                self.end_headers()
                self.wfile.write(b"do not print private diagnostics")

            def do_POST(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"01234567890")

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = SignozClient(f"http://127.0.0.1:{server.server_port}", "fixture-key", read_only=True)
                with self.assertRaises(SignozHttpError) as caught:
                    client.request("GET", "/api/v2/dashboards")
                self.assertEqual(caught.exception.status, 302)
                self.assertNotIn("private diagnostics", str(caught.exception))
                with self.assertRaises(RuntimeError):
                    client.request("POST", "/api/v1/testRule", {})
                client.READ_LIMIT = 8
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    client.request("POST", "/api/v5/query_range", {})
            finally:
                server.shutdown()
                thread.join(timeout=2)
        self.assertEqual(seen, ["/api/v2/dashboards", "/api/v5/query_range"])
