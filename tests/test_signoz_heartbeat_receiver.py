"""Persistence and webhook contracts for the external Dev heartbeat."""

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "signoz_heartbeat_receiver",
    Path(__file__).parents[1] / "scripts" / "signoz_heartbeat_receiver.py",
)
assert SPEC and SPEC.loader
receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver)


class HeartbeatReceiverTest(unittest.TestCase):
    boot = "test-boot"
    wall = 1_795_000_000
    cluster = "oriso-dev"
    password = b"test-only-heartbeat-password"
    authorization = "Basic " + b64encode(b"heartbeat:" + password).decode("ascii")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "heartbeat.sqlite3"
        self.clock = receiver.Clock(self.boot, self.wall, 1000)
        self.app = self.make_app()

    def make_app(self):
        return receiver.create_app(self.db, self.cluster, self.password, clock=lambda: self.clock)

    def payload(self, stamp=None):
        return {
            "version": "4",
            "truncatedAlerts": 0,
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "ORISO dev | SigNoz heartbeat",
                    "environment": "dev",
                    "deployment.environment": "dev",
                    "k8s.cluster.name": self.cluster,
                    "managed_by": "oriso_helm",
                },
                "annotations": {
                    "oriso_evaluated_at": str(self.wall - 120) if stamp is None else stamp,
                    "fixture_marker": "body-must-not-be-persisted",
                },
            }],
        }

    def post(self, payload=None, *, body=None, **env):
        raw = json.dumps(self.payload() if payload is None else payload).encode() if body is None else body
        request = {
            "PATH_INFO": "/pulse",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json; charset=utf-8",
            "HTTP_AUTHORIZATION": self.authorization,
            "wsgi.input": io.BytesIO(raw),
            **env,
        }
        responses = []
        answer = self.app(request, lambda status, headers: responses.append(int(status.split()[0])))
        self.assertEqual(b"".join(answer), b"")
        return responses[0]

    def state(self, *, now=None, cluster=None):
        return receiver.read_status(self.db, cluster or self.cluster, lambda: now or self.clock)

    def test_restart_preserves_deadline_and_a_fresh_pulse_recovers(self):
        self.assertEqual(self.state()["reason"], "no_pulse")
        self.assertEqual(self.post(), 204)
        self.app = self.make_app()
        self.clock = receiver.Clock(self.boot, self.wall - 300, 1599.99)
        self.assertEqual(self.state()["state"], "OK")
        self.clock = self.clock._replace(monotonic=1600)
        self.assertEqual(self.state(), {"state": "CRITICAL", "reason": "silence", "seconds_since_pulse": 600})
        self.clock = receiver.Clock(self.boot, self.wall + 600, 1600)
        self.assertEqual(self.post(self.payload(str(self.clock.wall - 110))), 204)
        self.assertEqual(self.state()["state"], "OK")
        self.assertNotIn(b"body-must-not-be-persisted", self.db.read_bytes())
        self.assertNotIn(self.password, self.db.read_bytes())

    def test_foreign_stale_and_replayed_messages_cannot_refresh_deadline(self):
        self.assertEqual(self.post(), 204)
        self.clock = receiver.Clock(self.boot, self.wall + 30, 1300)
        duplicate = self.payload()
        self.assertEqual(self.post(duplicate), 204)
        for field in ("status", "cluster", "truncation", "foreign", "old", "future", "version"):
            with self.subTest(field=field):
                payload = self.payload(str(self.clock.wall - 100))
                if field == "status":
                    payload["alerts"][0]["status"] = "resolved"
                elif field == "cluster":
                    payload["alerts"][0]["labels"]["k8s.cluster.name"] = "other-cluster"
                elif field == "truncation":
                    payload["truncatedAlerts"] = False
                elif field == "foreign":
                    payload["alerts"][0]["labels"]["deployment.environment"] = "pre-dev"
                elif field in {"old", "future"}:
                    payload["alerts"][0]["annotations"]["oriso_evaluated_at"] = str(
                        self.clock.wall + (-181 if field == "old" else 6),
                    )
                else:
                    payload["version"] = "unexpected"
                self.assertEqual(self.post(payload), 204)
        self.assertEqual(self.state()["seconds_since_pulse"], 300)

    def test_webhook_validates_auth_method_media_and_message_size(self):
        for expected, settings in (
            (401, {"HTTP_AUTHORIZATION": "bäd"}),
            (404, {"PATH_INFO": "/elsewhere"}),
            (405, {"REQUEST_METHOD": "GET"}),
            (415, {"CONTENT_TYPE": "text/plain"}),
            (400, {"CONTENT_LENGTH": ""}),
            (400, {"CONTENT_LENGTH": "10000"}),
            (413, {"CONTENT_LENGTH": "16385"}),
        ):
            with self.subTest(settings=settings):
                self.assertEqual(self.post(**settings), expected)
        self.assertEqual(self.post(body=b"{bad JSON}"), 400)
        self.assertEqual(self.post(body=b'{"version":"4","version":"4"}'), 400)
        self.assertEqual(self.post(body=b'{"unexpected":NaN}'), 400)
        self.assertEqual(self.post(self.payload("1e99999999")), 204)
        self.assertEqual(self.state()["reason"], "no_pulse")

    def test_exact_timestamp_range_and_scientific_notation(self):
        for stamp in (str(self.wall - 181), str(self.wall + 6), "NaN", "1795000000.1", self.wall):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.post(self.payload(stamp)), 204)
                self.assertEqual(self.state()["reason"], "no_pulse")
        self.assertEqual(self.post(self.payload("1.794999820e9")), 204)
        self.assertEqual(self.state()["state"], "OK")
        self.assertEqual(self.post(self.payload(str(self.wall + 5))), 204)

    def test_database_is_read_only_on_missing_state_or_new_boot(self):
        missing = self.db.with_name("missing.sqlite3")
        self.assertEqual(receiver.read_status(missing, self.cluster, lambda: self.clock)["reason"], "no_pulse")
        self.assertFalse(missing.exists())
        self.assertEqual(self.post(), 204)
        self.assertEqual(self.state(cluster="other-cluster")["reason"], "no_pulse")
        self.assertEqual(self.state(now=self.clock._replace(monotonic=999))["reason"], "clock_error")
        self.clock = receiver.Clock("next-boot", self.wall + 300, 20)
        self.app = self.make_app()
        self.assertEqual(self.state()["reason"], "host_restarted")
        self.assertEqual(self.post(self.payload(str(self.clock.wall - 100))), 204)
        self.assertEqual(self.state()["state"], "OK")

    def test_storage_failure_signals_sig_noz_and_fails_the_probe(self):
        with closing(sqlite3.connect(self.db)) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with patch.object(receiver, "DB_TIMEOUT", 0.01):
                self.assertEqual(self.post(), 503)
                self.assertEqual(self.state()["reason"], "state_unavailable")
        self.assertEqual(self.state()["reason"], "no_pulse")
        self.db.write_bytes(b"fixture-corruption")
        self.assertEqual(self.state(), {"state": "CRITICAL", "reason": "state_unavailable"})

    def test_status_reads_clock_after_selecting_the_latest_pulse(self):
        self.assertEqual(self.post(), 204)
        moments = []

        def clock():
            self.db.unlink()
            moments.append(self.clock)
            return self.clock

        self.assertEqual(receiver.read_status(self.db, self.cluster, clock)["state"], "OK")
        self.assertEqual(moments, [self.clock])

    def test_concurrent_pulses_keep_the_newest_evaluation(self):
        with ThreadPoolExecutor(max_workers=5) as workers:
            responses = workers.map(lambda stamp: self.post(self.payload(str(stamp))), range(self.wall - 90, self.wall))
            self.assertEqual(set(responses), {204})
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT evaluated_at FROM heartbeat").fetchone(), (self.wall - 1,))

    def test_status_command_uses_no_secret(self):
        with patch.dict("os.environ", {
            "SIGNOZ_HEARTBEAT_DB": str(self.db),
            "SIGNOZ_HEARTBEAT_CLUSTER": self.cluster,
        }, clear=True), patch.object(receiver, "current_clock", return_value=self.clock):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(receiver.main([]), 2)
            self.assertEqual(json.loads(output.getvalue())["reason"], "no_pulse")
            self.assertEqual(self.post(), 204)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(receiver.main([]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "OK")
            with patch.object(receiver, "current_clock", side_effect=OSError), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(receiver.main([]), 2)
            self.assertEqual(json.loads(output.getvalue())["reason"], "probe_configuration")


if __name__ == "__main__":
    unittest.main()
