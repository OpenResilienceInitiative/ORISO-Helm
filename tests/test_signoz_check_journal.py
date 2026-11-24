"""Exercise completion, failure detail and cursor handling at the reader boundary."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import signoz_check_journal as reader


BOOT = "a" * 32
INVOCATION = "b" * 32
MACHINE = "d" * 32
UPTIME = "oriso-signoz-uptime.service"
HEARTBEAT = "oriso-signoz-heartbeat-check.service"
DEV_NIGHTLY = "oriso-dev-nightly-monitor.service"
PUSH = "oriso-offsite-backup.service"
CHECK = "oriso-offsite-check.service"


def event(message, **fields):
    return {
        "_PID": "1", "_MACHINE_ID": MACHINE, "_BOOT_ID": BOOT, "UNIT": UPTIME,
        "INVOCATION_ID": INVOCATION, "MESSAGE_ID": message, **fields,
    }


class CheckJournalTest(TestCase):
    def test_success_requires_a_clean_completion_for_this_invocation(self):
        completed = event(reader.JOB_DONE, JOB_RESULT="done")
        self.assertEqual(reader.classify(completed, []), ("UNKNOWN", None, None))
        unrelated = event(reader.UNIT_SUCCEEDED, INVOCATION_ID="c" * 32)
        self.assertEqual(reader.classify(completed, [unrelated]), ("UNKNOWN", None, None))
        foreign = event(reader.UNIT_SUCCEEDED, _MACHINE_ID="e" * 32)
        self.assertEqual(reader.classify(completed, [foreign]), ("UNKNOWN", None, None))
        clean = event(reader.UNIT_SUCCEEDED)
        self.assertEqual(reader.classify(completed, [clean]), ("OK", "success", 0))

    def test_plugin_exit_and_runner_failures_remain_distinct(self):
        completed = event(reader.JOB_FAILED, JOB_RESULT="failed")
        exited = event(reader.PROCESS_EXIT, EXIT_CODE="exited", EXIT_STATUS="2")
        failed = event(reader.UNIT_FAILED, UNIT_RESULT="exit-code")
        self.assertEqual(reader.classify(completed, [exited, failed]), ("CRITICAL", "exit-code", 2))
        missing = {**exited, "EXIT_STATUS": "203"}
        self.assertEqual(reader.classify(completed, [missing, failed]), ("UNKNOWN", "exit-code", 203))
        timed_out = {**failed, "UNIT_RESULT": "timeout"}
        killed = {**exited, "EXIT_CODE": "killed", "EXIT_STATUS": "9"}
        self.assertEqual(reader.classify(completed, [killed, timed_out]), ("UNKNOWN", "timeout", None))

    def test_pages_keep_failed_runs_and_resume_after_the_last_returned_record(self):
        first = event(
            reader.JOB_FAILED, JOB_TYPE="start", JOB_RESULT="failed",
            __CURSOR="run-a", __REALTIME_TIMESTAMP="5000000",
        )
        second = event(
            reader.JOB_DONE, JOB_TYPE="start", JOB_RESULT="done", UNIT=HEARTBEAT,
            INVOCATION_ID="c" * 32, __CURSOR="run-b", __REALTIME_TIMESTAMP="6000000",
        )
        failed = event(reader.UNIT_FAILED, UNIT_RESULT="timeout")
        clean = event(reader.UNIT_SUCCEEDED, UNIT=HEARTBEAT, INVOCATION_ID="c" * 32)
        with patch.object(reader, "read_journal", side_effect=[
            ([first, second], False), ([failed, clean], False),
        ]):
            page = reader.read_page(since="10 minutes ago")
        self.assertEqual([run["state"] for run in page["completed"]], ["UNKNOWN", "OK"])
        self.assertEqual(page["next_cursor"], "run-b")

        with patch.object(reader, "read_journal", return_value=([first], False)):
            with self.assertRaisesRegex(ValueError, "resume cursor"):
                reader.read_page(cursor="removed-record")

        with patch.object(reader, "read_journal", return_value=([second], False)):
            page = reader.read_page(cursor="run-b")
        self.assertEqual(page, {"completed": [], "next_cursor": "run-b", "more": False})


class CheckCadenceTest(TestCase):
    def test_recent_failed_probe_keeps_its_result_and_checks_heartbeat_only(self):
        completed = event(reader.JOB_FAILED, UNIT=HEARTBEAT, JOB_RESULT="failed",
                          __MONOTONIC_TIMESTAMP="100000000")
        failed = event(reader.UNIT_FAILED, UNIT=HEARTBEAT, UNIT_RESULT="exit-code")
        exited = event(reader.PROCESS_EXIT, UNIT=HEARTBEAT, EXIT_CODE="exited", EXIT_STATUS="2")
        with (
            patch.object(reader, "timer_state", return_value="active") as timer,
            patch.object(reader, "local_boot_id", return_value=BOOT),
            patch.object(reader.time, "monotonic_ns", return_value=140_000_000_000),
            patch.object(reader, "read_journal", side_effect=[([completed], False), ([failed, exited], False)]) as journal,
        ):
            result = reader.check_cadence(HEARTBEAT)
        timer.assert_called_once_with("oriso-signoz-heartbeat-check.timer")
        options, matches, limit = journal.call_args_list[0].args
        self.assertIn(f"--boot={BOOT}", options)
        self.assertIn(f"UNIT={HEARTBEAT}", matches)
        self.assertNotIn(f"UNIT={UPTIME}", matches)
        self.assertEqual(limit, 1)
        self.assertEqual(result, {
            "unit": HEARTBEAT, "state": "OK", "reason": "recent-completion",
            "age_seconds": 40.0, "last_probe_state": "CRITICAL",
        })

    def test_completion_at_deadline_is_stale(self):
        completed = event(reader.JOB_DONE, JOB_RESULT="done", __MONOTONIC_TIMESTAMP="100000000")
        with (
            patch.object(reader, "timer_state", return_value="active"),
            patch.object(reader, "local_boot_id", return_value=BOOT),
            patch.object(reader.time, "monotonic_ns", return_value=280_000_000_000),
            patch.object(reader, "read_journal", return_value=([completed], False)),
        ):
            result = reader.check_cadence(UPTIME)
        self.assertEqual((result["state"], result["reason"]), ("CRITICAL", "stale-completion"))
        self.assertEqual(result["age_seconds"], 180)

    def test_stopped_timer_is_critical_before_journal_lookup(self):
        with (
            patch.object(reader, "timer_state", return_value="inactive"),
            patch.object(reader, "read_journal") as journal,
        ):
            result = reader.check_cadence(UPTIME)
        journal.assert_not_called()
        self.assertEqual((result["state"], result["reason"]), ("CRITICAL", "timer-not-active"))

    def test_no_current_run_and_unverifiable_current_run_fail_closed(self):
        completed = event(reader.JOB_DONE, JOB_RESULT="done", __MONOTONIC_TIMESTAMP="100000000")
        for label, records, expected in (
            ("empty", [], ("CRITICAL", "missing-completion")),
            ("old boot", [{**completed, "_BOOT_ID": "c" * 32}], ("UNKNOWN", "invalid-runner-state")),
            ("wrong unit", [{**completed, "UNIT": HEARTBEAT}], ("UNKNOWN", "invalid-runner-state")),
            ("future timestamp", [{**completed, "__MONOTONIC_TIMESTAMP": "200000000"}],
             ("UNKNOWN", "invalid-runner-state")),
            ("invalid timestamp", [{**completed, "__MONOTONIC_TIMESTAMP": "beep"}],
             ("UNKNOWN", "invalid-runner-state")),
            ("skipped condition", [{**completed, "INVOCATION_ID": None}],
             ("UNKNOWN", "unattributed-completion")),
            ("no service result", [completed], ("UNKNOWN", "unattributed-completion")),
        ):
            with (
                self.subTest(label=label),
                patch.object(reader, "timer_state", return_value="active"),
                patch.object(reader, "local_boot_id", return_value=BOOT),
                patch.object(reader.time, "monotonic_ns", return_value=140_000_000_000),
                patch.object(reader, "read_journal", side_effect=[(records, False), ([], False)]),
            ):
                result = reader.check_cadence(UPTIME)
                self.assertEqual((result["state"], result["reason"]), expected)


class DevNightlyJournalTest(TestCase):
    run_url = "https://github.com/OpenResilienceInitiative/ORISO-E2E/actions/runs/123"

    def report(self):
        return {
            "repository": "OpenResilienceInitiative/ORISO-E2E",
            "target": "https://dev.oriso.org", "state": "red",
            "scheduledAt": "2026-11-24T02:30:00.000Z", "defaultBranchSha": "c" * 40,
            "run": {
                "url": self.run_url, "launcherSha": "e" * 40, "attempt": 2,
                "status": "completed", "conclusion": "failure",
                "problem": {
                    "job": "Dev nightly specs", "url": f"{self.run_url}/job/456",
                    "status": "completed", "conclusion": "failure",
                    "step": {"number": 3, "name": "Run the Dev spec set",
                             "status": "completed", "conclusion": "failure"},
                },
            },
        }

    def stdout_record(self, message, **overrides):
        return {
            "_TRANSPORT": "stdout", "_SYSTEMD_UNIT": DEV_NIGHTLY,
            "_MACHINE_ID": MACHINE, "_BOOT_ID": BOOT,
            "_SYSTEMD_INVOCATION_ID": INVOCATION, "MESSAGE": message, **overrides,
        }

    def incident_page(self, observations):
        completion = event(
            reader.JOB_FAILED, UNIT=DEV_NIGHTLY, JOB_RESULT="failed",
            __CURSOR="incident", __REALTIME_TIMESTAMP="1795496400000000",
        )
        details = [
            event(reader.UNIT_FAILED, UNIT=DEV_NIGHTLY, UNIT_RESULT="exit-code"),
            event(reader.PROCESS_EXIT, UNIT=DEV_NIGHTLY, EXIT_CODE="exited", EXIT_STATUS="2"),
        ]
        with patch.object(reader, "read_journal", side_effect=[
            ([completion], False), (details, False), observations,
        ]) as journal:
            page = reader.read_page(since="10 minutes ago", scope="dev-nightly")
        self.assertEqual(page["next_cursor"], "incident")
        self.assertFalse(page["more"])
        self.assertEqual(page["completed"][0]["state"], "CRITICAL")
        self.assertEqual(page["completed"][0]["exit_status"], 2)
        self.assertEqual(journal.call_count, 3)
        return page["completed"][0], journal.call_args_list[2]

    def test_nightly_scope_preserves_each_plugin_exit_when_stdout_is_missing(self):
        outcomes = [(0, "OK"), (1, "PENDING"), (2, "CRITICAL"), (3, "UNKNOWN")]
        completions = []
        details = []
        for status, _ in outcomes:
            fields = {"UNIT": DEV_NIGHTLY, "INVOCATION_ID": f"{status:032x}"}
            message = reader.JOB_FAILED if status else reader.JOB_DONE
            completions.append(event(
                message, **fields, JOB_TYPE="start", JOB_RESULT="failed" if status else "done",
                __CURSOR=f"nightly-{status}", __REALTIME_TIMESTAMP=str((status + 1) * 1000000),
            ))
            if status:
                details.extend([
                    event(reader.UNIT_FAILED, **fields, UNIT_RESULT="exit-code"),
                    event(reader.PROCESS_EXIT, **fields, EXIT_CODE="exited", EXIT_STATUS=str(status)),
                ])
            else:
                details.append(event(reader.UNIT_SUCCEEDED, **fields, MESSAGE="untrusted service output"))

        with (
            patch.object(reader.sys, "argv", ["monitor", "--scope", "dev-nightly", "--since", "10 minutes ago"]),
            patch.object(reader, "read_journal", side_effect=[
                (completions, False), (details, False), ([], False),
            ]) as journal,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(reader.main(), 0)

        page = json.loads(output.getvalue())
        self.assertEqual([(row["exit_status"], row["state"]) for row in page["completed"]], outcomes)
        self.assertTrue(all("probe" not in row for row in page["completed"]))
        self.assertEqual(page["next_cursor"], "nightly-3")
        self.assertNotIn("untrusted service output", output.getvalue())
        for call in journal.call_args_list[:2]:
            self.assertIn("_PID=1", call.args[1])
            self.assertIn(f"UNIT={DEV_NIGHTLY}", call.args[1])
            self.assertNotIn(f"UNIT={UPTIME}", call.args[1])
            self.assertNotIn(f"UNIT={PUSH}", call.args[1])
        for status, _ in outcomes:
            self.assertIn(f"_SYSTEMD_INVOCATION_ID={status:032x}", journal.call_args_list[2].args[1])

    def test_nightly_scope_attaches_only_the_validated_probe(self):
        expected = self.report()
        raw = self.report()
        raw["detail"] = "should not be relayed"
        raw["run"]["problem"]["runner_name"] = "should not be relayed"
        raw["repository_secret"] = "should not be relayed"
        valid = self.stdout_record(json.dumps(raw))
        foreign = self.stdout_record(json.dumps(raw), _MACHINE_ID="c" * 32)
        wrong_boot = self.stdout_record(json.dumps(raw), _BOOT_ID="c" * 32)
        wrong_unit = self.stdout_record(json.dumps(raw), _SYSTEMD_UNIT=UPTIME)
        wrong_invocation = self.stdout_record(json.dumps(raw), _SYSTEMD_INVOCATION_ID="c" * 32)
        result, read = self.incident_page(([foreign, wrong_boot, wrong_unit, wrong_invocation, valid], False))
        self.assertEqual(result["probe"], dict(expected, detail="Dev nightly specs / Run the Dev spec set (failure)."))
        self.assertEqual(read.args[1], (
            "_TRANSPORT=stdout", f"_SYSTEMD_UNIT={DEV_NIGHTLY}",
            f"_SYSTEMD_INVOCATION_ID={INVOCATION}",
        ))
        self.assertLessEqual(read.args[2], 2 * reader.PAGE_SIZE)
        self.assertIn("MESSAGE", read.kwargs["fields"].split(","))
        self.assertNotIn("should not be relayed", json.dumps(result))

    def test_duplicate_invalid_or_unavailable_stdout_does_not_affect_the_incident(self):
        valid = self.stdout_record(json.dumps(self.report()))
        wrong_verdict = self.stdout_record(json.dumps(dict(self.report(), state="green")))
        bad_url = self.report()
        bad_url["run"]["problem"]["url"] = self.run_url + "/job/456?private=canary"
        messages = (
            "unparseable canary", "[123]", '{"state":"red","state":"green"}',
            " " * 4096, json.dumps(bad_url),
        )
        for observations in (
            *[([self.stdout_record(message)], False) for message in messages],
            ([valid, valid], False), ([valid], True), ([wrong_verdict], False),
            PermissionError("private journal error"),
        ):
            with self.subTest(observations=type(observations).__name__):
                result, _ = self.incident_page(observations)
                self.assertNotIn("probe", result)

    def test_unreviewed_step_name_is_replaced_and_stalled_job_is_attributed(self):
        report = self.report()
        report["state"] = "stalled"
        run = report["run"]
        run.update(status="in_progress", conclusion=None)
        run["problem"].update(status="in_progress", conclusion=None)
        run["problem"]["step"].update(name="unreviewed name", status="in_progress", conclusion=None)
        result, _ = self.incident_page(([self.stdout_record(json.dumps(report))], False))
        self.assertEqual(result["probe"]["state"], "stalled")
        self.assertEqual(result["probe"]["run"]["problem"]["step"]["name"], "Step 3")
        self.assertEqual(result["probe"]["detail"], "Dev nightly specs / Step 3 (in_progress).")

    def test_recent_pending_probe_has_healthy_cadence_without_recovering_the_probe(self):
        completed = event(reader.JOB_FAILED, UNIT=DEV_NIGHTLY, JOB_RESULT="failed",
                          __MONOTONIC_TIMESTAMP="100000000")
        failed = event(reader.UNIT_FAILED, UNIT=DEV_NIGHTLY, UNIT_RESULT="exit-code")
        exited = event(reader.PROCESS_EXIT, UNIT=DEV_NIGHTLY, EXIT_CODE="exited", EXIT_STATUS="1")
        for arguments in ([], ["--scope", "dev-nightly"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(reader.sys, "argv", ["monitor", *arguments, "--check-cadence", "dev-nightly"]),
                patch.object(reader, "timer_state", return_value="active") as timer,
                patch.object(reader, "local_boot_id", return_value=BOOT),
                patch.object(reader.time, "monotonic_ns", return_value=140_000_000_000),
                patch.object(reader, "read_journal", side_effect=[
                    ([completed], False), ([failed, exited], False),
                ]) as journal,
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(reader.main(), 0)
            timer.assert_called_once_with("oriso-dev-nightly-monitor.timer")
            for call in journal.call_args_list:
                self.assertIn(f"--boot={BOOT}", call.args[0])
                self.assertIn(f"UNIT={DEV_NIGHTLY}", call.args[1])
                self.assertNotIn(f"UNIT={HEARTBEAT}", call.args[1])
            self.assertEqual(json.loads(output.getvalue()), {
                "unit": DEV_NIGHTLY, "state": "OK", "reason": "recent-completion",
                "age_seconds": 40.0, "last_probe_state": "PENDING",
            })

    def test_explicit_cadence_scope_cannot_read_a_different_unit(self):
        with (
            patch.object(reader.sys, "argv", ["monitor", "--scope", "dev-nightly", "--check-cadence", "uptime"]),
            patch.object(reader, "check_cadence") as check,
            redirect_stderr(StringIO()) as output,
            self.assertRaises(SystemExit) as raised,
        ):
            reader.main()
        self.assertEqual(raised.exception.code, 2)
        check.assert_not_called()
        self.assertIn("--check-cadence uptime requires --scope signoz", output.getvalue())


class BackupJournalTest(TestCase):
    def test_backup_scope_keeps_only_attributable_backups_and_tracks_recovery(self):
        push = event(
            reader.JOB_FAILED, UNIT=PUSH, JOB_RESULT="failed",
            __CURSOR="push-failure", __REALTIME_TIMESTAMP="5000000",
        )
        recovered = event(
            reader.JOB_DONE, UNIT=CHECK, JOB_RESULT="done", INVOCATION_ID="c" * 32,
            __CURSOR="check-recovered", __REALTIME_TIMESTAMP="6000000",
        )
        failed = event(reader.UNIT_FAILED, UNIT=PUSH, UNIT_RESULT="exit-code")
        exited = event(reader.PROCESS_EXIT, UNIT=PUSH, EXIT_CODE="exited", EXIT_STATUS="1")
        foreign = event(reader.UNIT_SUCCEEDED, UNIT=CHECK, _PID="2", INVOCATION_ID="c" * 32)
        success = event(reader.UNIT_SUCCEEDED, UNIT=CHECK, INVOCATION_ID="c" * 32)
        with patch.object(reader, "read_journal", side_effect=[
            ([push, recovered], False), ([failed, exited, foreign, success], False),
        ]) as journal:
            page = reader.read_page(since="10 minutes ago", scope="offsite-backup")

        self.assertEqual(
            [(row["unit"], row["state"]) for row in page["completed"]],
            [(PUSH, "CRITICAL"), (CHECK, "OK")],
        )
        self.assertEqual(page["next_cursor"], "check-recovered")
        for call in journal.call_args_list:
            matches = call.args[1]
            self.assertIn("_PID=1", matches)
            self.assertIn(f"UNIT={PUSH}", matches)
            self.assertIn(f"UNIT={CHECK}", matches)
            self.assertNotIn(f"UNIT={UPTIME}", matches)

    def test_backup_timeout_and_failed_start_do_not_fake_probe_failure(self):
        completion = event(reader.JOB_FAILED, UNIT=CHECK, JOB_RESULT="failed")
        timeout = event(reader.UNIT_FAILED, UNIT=CHECK, UNIT_RESULT="timeout")
        failed = event(reader.UNIT_FAILED, UNIT=CHECK, UNIT_RESULT="exit-code")
        missing_command = event(reader.PROCESS_EXIT, UNIT=CHECK, EXIT_CODE="exited", EXIT_STATUS="203")
        self.assertEqual(reader.classify(completion, [timeout]), ("UNKNOWN", "timeout", None))
        self.assertEqual(reader.classify(completion, [failed, missing_command]), ("UNKNOWN", "exit-code", 203))

    def test_cli_adds_freshness_only_for_explicit_backup_scope(self):
        page = {"completed": [], "next_cursor": "last", "more": False}
        for arguments, scope in (([], "signoz"), (["--scope", "offsite-backup"], "offsite-backup")):
            with (
                self.subTest(scope=scope),
                patch.object(reader.sys, "argv", ["monitor", *arguments, "--cursor", "last"]),
                patch.object(reader, "read_page", side_effect=lambda **_: page.copy()) as journal,
                patch.object(reader, "read_backup_freshness", return_value={"example": True}) as freshness,
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(reader.main(), 0)
            journal.assert_called_once_with(since=None, cursor="last", scope=scope)
            expected = dict(page, freshness={"example": True}) if arguments else page
            self.assertEqual(json.loads(output.getvalue()), expected)
            self.assertEqual(freshness.call_count, bool(arguments))


class BackupFreshnessTest(TestCase):
    now = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)

    def snapshot(self, data, push=None):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "check.json"
            push_path = Path(directory) / "push.json"
            if data is not None:
                path.write_text(json.dumps(data), encoding="utf-8")
            if push is not None:
                push_path.write_text(json.dumps(push), encoding="utf-8")
            return reader.read_backup_freshness(path=path, push_path=push_path, now=self.now)

    def producer_status(self, **fields):
        return {
            "command": "check", "at": self.now.isoformat(), "ok": True,
            "report": {}, **fields,
        }

    def test_archive_cutoff_is_inclusive_and_output_does_not_leak_producer_details(self):
        cutoff = self.now - timedelta(hours=26)
        sample = self.snapshot(self.producer_status(
            ok=False,
            report={
                "mariadb": {"archiveAt": cutoff.isoformat(), "complete": True, "detail": "private"},
                "mongodb": {
                    "archiveAt": (cutoff - timedelta(microseconds=1)).isoformat(), "complete": True,
                },
                "matrix-postgres": {"archiveAt": cutoff.isoformat(), "complete": False},
                "other": {"archiveAt": cutoff.isoformat(), "complete": True},
            },
            problems=["private"],
        ))
        self.assertEqual(sample["probe"], {
            "state": "CRITICAL", "reason": "probe-failed", "checked_at": self.now.isoformat(),
        })
        self.assertEqual(sample["stores"], {
            "mariadb": {"state": "OK", "reason": "fresh-archive", "archive_at": cutoff.isoformat()},
            "mongodb": {"state": "CRITICAL", "reason": "stale-archive",
                        "archive_at": (cutoff - timedelta(microseconds=1)).isoformat()},
            "matrix-postgres": {"state": "CRITICAL", "reason": "incomplete-archive",
                                "archive_at": cutoff.isoformat()},
        })
        self.assertNotIn("private", json.dumps(sample))

    def test_untyped_future_failed_or_absent_status_never_certifies_a_backup(self):
        data = self.producer_status(
            ok=False,
            report={
                "mariadb": {"run": "old-release", "ageHours": 1},
                "mongodb": {"archiveAt": (self.now + timedelta(seconds=1)).isoformat(), "complete": True},
            },
        )
        sample = self.snapshot(data)
        self.assertEqual(sample["probe"]["state"], "CRITICAL")
        self.assertEqual(
            {key: value["state"] for key, value in sample["stores"].items()},
            {"mariadb": "UNKNOWN", "mongodb": "UNKNOWN", "matrix-postgres": "UNKNOWN"},
        )
        for payload in (
            None,
            self.producer_status(at="2026-11-22T13:00:00+01:00"),
            self.producer_status(at=(self.now + timedelta(seconds=1)).isoformat()),
            self.producer_status(at=(self.now - timedelta(hours=26, seconds=1)).isoformat()),
            self.producer_status(command="push"),
        ):
            with self.subTest(payload=payload):
                sample = self.snapshot(payload)
                self.assertNotEqual(sample["probe"]["state"], "OK")
                self.assertTrue(all(row["state"] != "OK" for row in sample["stores"].values()))

    def test_verified_new_push_covers_gap_before_morning_check(self):
        now = datetime(2026, 11, 22, 6, tzinfo=timezone.utc)
        self.now = now
        old_archive = datetime(2026, 11, 21, 2, tzinfo=timezone.utc).isoformat()
        new_archive = datetime(2026, 11, 22, 2, tzinfo=timezone.utc).isoformat()
        last_check = datetime(2026, 11, 21, 8, tzinfo=timezone.utc).isoformat()
        latest = {"archiveAt": new_archive, "complete": True, "uploaded": 1}
        check = self.producer_status(
            at=last_check,
            report={
                "mariadb": {"archiveAt": old_archive, "complete": True},
                "mongodb": {"archiveAt": old_archive, "complete": True},
                "matrix-postgres": {"archiveAt": old_archive, "complete": True},
            },
        )
        push = self.producer_status(
            command="push", at="2026-11-22T03:00:00+00:00",
            report={
                "mariadb": latest,
                "mongodb": dict(latest, uploaded=0),
                "matrix-postgres": dict(latest, archiveAt="2026-11-22T03:00:01Z"),
            },
        )
        sample = self.snapshot(check, push)
        self.assertEqual(sample["probe"]["checked_at"], last_check)
        self.assertEqual(sample["stores"]["mariadb"], {
            "state": "OK", "reason": "fresh-archive", "archive_at": new_archive,
        })
        self.assertEqual(sample["stores"]["mongodb"]["reason"], "stale-archive")
        self.assertEqual(sample["stores"]["matrix-postgres"]["reason"], "stale-archive")

        subsequent = self.producer_status(
            at="2026-11-22T05:00:00+00:00", ok=False,
            report={"mongodb": {"archiveAt": new_archive, "complete": False}},
        )
        sample = self.snapshot(subsequent, push)
        self.assertEqual(sample["probe"]["state"], "CRITICAL")
        self.assertEqual(sample["stores"]["mariadb"]["state"], "UNKNOWN")
        self.assertEqual(sample["stores"]["mongodb"]["state"], "CRITICAL")
        self.assertTrue(all(row["state"] == "UNKNOWN" for row in self.snapshot(None, push)["stores"].values()))

    def test_carried_upload_keeps_its_verification_time_and_cannot_override_a_later_check(self):
        check = self.producer_status(at="2026-11-22T05:00:00Z")
        push = self.producer_status(command="push", at="2026-11-22T06:00:00Z")
        evidence = {
            "archiveAt": "2026-11-22T02:00:00Z",
            "complete": True,
            "uploaded": 0,
            "verifiedAt": "2026-11-22T03:00:00Z",
        }
        for verified in (evidence["verifiedAt"], "2026-11-22T06:00:01Z", "invalid"):
            with self.subTest(verified=verified):
                push["report"] = {"mariadb": dict(evidence, verifiedAt=verified)}
                self.assertEqual(self.snapshot(check, push)["stores"]["mariadb"]["state"], "UNKNOWN")

        earlier = dict(check, at="2026-11-22T02:30:00Z")
        push["report"] = {"mariadb": evidence}
        self.assertEqual(self.snapshot(earlier, push)["stores"]["mariadb"]["reason"], "fresh-archive")

    def test_unreadable_status_names_the_permission_fault(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "check.json"
            path.write_text(json.dumps(self.producer_status()), encoding="utf-8")
            with patch.object(Path, "open", side_effect=PermissionError):
                result = reader.read_backup_freshness(path, push_path=path, now=self.now)
        self.assertEqual(result["probe"], {"state": "UNKNOWN", "reason": "unreadable-probe", "checked_at": None})
        self.assertTrue(all(row["state"] == "UNKNOWN" for row in result["stores"].values()))

    def test_duplicate_or_oversized_status_is_unknown(self):
        valid = json.dumps(self.producer_status())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "check.json"
            push_path = Path(directory) / "push.json"
            for data in (valid[:-1] + ', "ok": false}', valid + " " * (16 * 1024)):
                with self.subTest(size=len(data)):
                    path.write_text(data, encoding="utf-8")
                    result = reader.read_backup_freshness(path, push_path=push_path, now=self.now)
                    self.assertEqual(result["probe"]["reason"], "invalid-probe")
                    self.assertTrue(all(row["state"] == "UNKNOWN" for row in result["stores"].values()))
