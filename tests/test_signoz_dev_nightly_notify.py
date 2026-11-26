"""Verify delivery, proofs and replay using the nightly journal contract."""

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase

from scripts import signoz_check_journal as journal
from scripts import signoz_dev_nightly_notify as notify


RUN_URL = journal.DEV_NIGHTLY_RUN_URL + "91"
SLOT = "2026-11-24T02:30:00Z"
LAUNCHER = "c" * 40
SOURCE = "d" * 40
WEBHOOK_PATH = "/services/fixture/team/secret"


def completion(cursor, result):
    state, code = journal.DEV_NIGHTLY_VERDICTS[result]
    entry = {
        "unit": journal.DEV_NIGHTLY, "state": state, "exit_status": code, "cursor": cursor,
        "completed_at": "2026-11-24T04:31:00Z",
    }
    probe = {
        "repository": journal.DEV_NIGHTLY_REPOSITORY,
        "target": journal.DEV_NIGHTLY_TARGET,
        "state": result, "scheduledAt": SLOT, "defaultBranchSha": LAUNCHER,
    }
    if result in {"red", "green"}:
        run = {
            "url": RUN_URL, "attempt": 1, "launcherSha": LAUNCHER, "status": "completed",
            "conclusion": "failure" if result == "red" else "success",
        }
        if result == "red":
            run.update(testSourceSha=SOURCE, problem={
                "job": "Dev Test Access readiness", "url": RUN_URL + "/job/9",
                "status": "completed", "conclusion": "failure",
                "step": {
                    "number": 6, "name": "Check encrypted Dev chat history",
                    "status": "completed", "conclusion": "failure",
                },
            })
        probe["run"] = run
    entry["probe"] = journal.project_nightly_probe(json.dumps(probe), entry)
    return entry


class Runner:
    def __init__(self):
        self.entries = []
        self.cadence = "recent-completion"
        self.fail_page = False

    def read(self, *args):
        if args == ("--check-cadence", "dev-nightly"):
            state = "OK" if self.cadence == "recent-completion" else "CRITICAL"
            return journal.CADENCE_EXIT[state], {"unit": journal.DEV_NIGHTLY, "state": state, "reason": self.cadence}
        if self.fail_page:
            raise OSError("unreviewed runner response")
        cursor = args[-1] if args[-2] == "--cursor" else None
        known = [entry["cursor"] for entry in self.entries]
        items = self.entries[known.index(cursor) + 1:] if cursor else self.entries
        return 0, {"completed": items, "next_cursor": items[-1]["cursor"] if items else cursor, "more": False}


@contextmanager
def local_receiver():
    accepted = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            accepted.append((self.path, json.loads(body)))
            status, text = responses.pop(0) if responses else (200, b"ok")
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(text)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def send(message):
                notify.send_slack(
                    WEBHOOK_PATH, message,
                    connect=lambda host, timeout: HTTPConnection("127.0.0.1", server.server_port, timeout=timeout),
                )

            yield send, accepted, responses
        finally:
            server.shutdown()
            thread.join()


class NightlyNotificationTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.runner = Runner()

    def test_failed_and_absent_checks_reach_receiver_and_replay_is_suppressed(self):
        with local_receiver() as (send, received, _):
            self.runner.entries += [
                completion("s=1", "red"), completion("s=2", "red"), completion("s=3", "within-grace"),
            ]
            self.runner.entries[0]["probe"]["detail"] = "unreviewed data"
            notify.relay(self.runner.read, send, self.path)
            notify.relay(self.runner.read, send, self.path)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], WEBHOOK_PATH)
            text = received[0][1]["text"]
            for expected in ("Check encrypted Dev chat history", SLOT, RUN_URL + "/job/9", LAUNCHER, SOURCE):
                self.assertIn(expected, text)
            self.assertNotIn("unreviewed data", text)
            self.assertFalse(received[0][1]["mrkdwn"])

            self.runner.entries += [completion("s=4", "absent"), completion("s=5", "green")]
            notify.relay(self.runner.read, send, self.path)
            self.assertEqual(len(received), 3)
            self.assertIn("absent", received[1][1]["text"])
            self.assertIn(LAUNCHER, received[1][1]["text"])
            self.assertIn("Dev nightly recovered", received[2][1]["text"])
            self.assertEqual(notify.load_state(self.path), {"cursor": "s=5", "nightly": None, "watcher": None})

    def test_public_only_success_reports_each_missing_proof(self):
        entry = completion("s=1", "red")
        entry["probe"]["run"] = {
            "url": RUN_URL, "attempt": 1, "launcherSha": LAUNCHER, "status": "completed",
            "conclusion": "success", "testSourceSha": SOURCE,
            "missingProofs": [
                "Dev Test Access readiness / Check encrypted Dev chat history", "Protected Dev video call",
            ],
        }
        self.runner.entries.append(entry)
        seen = []
        notify.relay(self.runner.read, seen.append, self.path)
        self.assertEqual(len(seen), 1)
        self.assertIn("CRITICAL", seen[0])
        self.assertIn("Check encrypted Dev chat history", seen[0])
        self.assertIn("Protected Dev video call", seen[0])
        self.assertIn(RUN_URL, seen[0])
        self.assertIn(SOURCE, seen[0])

    def test_declined_webhook_cannot_advance_the_cursor(self):
        self.runner.entries.append(completion("s=1", "red"))
        with local_receiver() as (send, received, responses):
            responses.extend([(302, b"redirect"), (200, b"rejected")])
            for attempt in range(2):
                with self.assertRaises(OSError):
                    notify.relay(self.runner.read, send, self.path)
                self.assertIsNone(notify.load_state(self.path)["cursor"])
                self.assertEqual(len(received), attempt + 1)
            notify.relay(self.runner.read, send, self.path)
            self.assertEqual(notify.load_state(self.path)["cursor"], "s=1")
            self.assertEqual(len(received), 3)

    def test_runner_recovery_and_unverified_ok_do_not_resolve_nightly(self):
        seen = []
        self.runner.entries.append(completion("s=1", "absent"))
        notify.relay(self.runner.read, seen.append, self.path)
        self.runner.cadence = "timer-not-active"
        self.runner.fail_page = True
        notify.relay(self.runner.read, seen.append, self.path)
        self.assertIn("timer is inactive", seen[-1])
        self.runner.cadence = "recent-completion"
        self.runner.fail_page = False
        unknown = completion("s=2", "green")
        del unknown["probe"]
        self.runner.entries.append(unknown)
        notify.relay(self.runner.read, seen.append, self.path)
        self.assertIn("monitor runner recovered", seen[-2])
        self.assertIn("no verified workflow evidence", seen[-1])
        self.assertIsNotNone(notify.load_state(self.path)["nightly"])
        self.runner.entries.append(completion("s=3", "green"))
        notify.relay(self.runner.read, seen.append, self.path)
        self.assertIn("Dev nightly recovered", seen[-1])
        self.assertIsNone(notify.load_state(self.path)["nightly"])
