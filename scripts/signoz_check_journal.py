#!/usr/bin/env python3
"""Read completed Dev monitoring checks from the systemd manager journal."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import islice
import json
from pathlib import Path
import re
import subprocess
import sys
import time


UPTIME = "oriso-signoz-uptime.service"
HEARTBEAT = "oriso-signoz-heartbeat-check.service"
DEV_NIGHTLY = "oriso-dev-nightly-monitor.service"
BACKUP_PUSH = "oriso-offsite-backup.service"
BACKUP_CHECK = "oriso-offsite-check.service"
BACKUP_STORES = ("mariadb", "mongodb", "matrix-postgres")
BACKUP_CHECK_STATUS = Path("/var/lib/oriso-offsite-backup/check.json")
BACKUP_PUSH_STATUS = Path("/var/lib/oriso-offsite-backup/push.json")
MAX_BACKUP_STATUS_BYTES = 16 * 1024
MAX_BACKUP_AGE = timedelta(hours=26)
UTC_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)"
)
JOB_DONE = "39f53479d3a045ac8e11786248231fbf"
JOB_FAILED = "be02cf6855d2428ba40df7e9d022f03d"
UNIT_SUCCEEDED = "7ad2d189f7e94e70a38c781354912448"
UNIT_FAILED = "d9b373ed55a64feb8242e02dbe79a49c"
PROCESS_EXIT = "98e322203f7a4ed290d09fe03c09fe15"
PAGE_SIZE = 24
MAX_COMPLETION_AGE_SECONDS = 180
UNIT_MATCHES = ("_PID=1", f"UNIT={UPTIME}", f"UNIT={HEARTBEAT}")
SCOPE_MATCHES = {
    "signoz": UNIT_MATCHES,
    "offsite-backup": ("_PID=1", f"UNIT={BACKUP_PUSH}", f"UNIT={BACKUP_CHECK}"),
    "dev-nightly": ("_PID=1", f"UNIT={DEV_NIGHTLY}"),
}
CADENCE_CHECKS = {
    "uptime": ("signoz", UPTIME),
    "heartbeat": ("signoz", HEARTBEAT),
    "dev-nightly": ("dev-nightly", DEV_NIGHTLY),
}
CADENCE_EXIT = {"OK": 0, "CRITICAL": 2, "UNKNOWN": 3}
PLUGIN_STATES = {
    UPTIME: {1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"},
    HEARTBEAT: {2: "CRITICAL"},
    DEV_NIGHTLY: {1: "PENDING", 2: "CRITICAL", 3: "UNKNOWN"},
    BACKUP_PUSH: {1: "CRITICAL"},
    BACKUP_CHECK: {1: "CRITICAL"},
}
JOURNAL_FIELDS = (
    "UNIT,_PID,_MACHINE_ID,INVOCATION_ID,JOB_TYPE,JOB_RESULT,MESSAGE_ID,"
    "UNIT_RESULT,EXIT_CODE,EXIT_STATUS"
)
DEV_NIGHTLY_STDOUT_FIELDS = "_TRANSPORT,_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,_MACHINE_ID,_BOOT_ID,MESSAGE"
DEV_NIGHTLY_REPOSITORY = "OpenResilienceInitiative/ORISO-E2E"
DEV_NIGHTLY_TARGET = "https://dev.oriso.org"
DEV_NIGHTLY_RUN_URL = f"https://github.com/{DEV_NIGHTLY_REPOSITORY}/actions/runs/"
DEV_NIGHTLY_VERDICTS = {
    "green": ("OK", 0),
    "within-grace": ("PENDING", 1), "running": ("PENDING", 1),
    "absent": ("CRITICAL", 2), "red": ("CRITICAL", 2), "stalled": ("CRITICAL", 2),
    "unregistered": ("CRITICAL", 2), "disabled": ("CRITICAL", 2),
    "lookup-failed": ("UNKNOWN", 3),
}
RUNNING_ACTION_STATES = {"queued", "requested", "waiting", "pending", "in_progress"}
ACTION_CONCLUSIONS = {
    "success", "failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale", "skipped", "neutral",
}
NIGHTLY_JOBS = {
    "Workflow and reporter contract", "Dev nightly specs", "Public Dev call readiness", "Dev Test Access readiness",
}
# Known names stay legible. New or unreviewed names use the step number until reviewed here.
NIGHTLY_STEPS = {
    "Set up job", "Resolve the reviewed Dev source", "Verify the checked-out workflow matches the launcher",
    "Prove the Dev nightly cannot reach another environment or the cluster",
    "Prove the trace headers and the authenticated export are well formed",
    "Check public Dev video call prerequisites",
    "Verify the tested Dev source before accessing credentials",
    "Fail fast when secrets are absent or the hub rejects the token",
    "Check Dev telemetry prerequisites", "Check scoped Dev Test Access",
    "Resolve the reviewed Test Access source", "Install reviewed Test Access and Chromium",
    "Check encrypted Dev chat history",
    "Typecheck the E2E suite", "Run the Dev spec set", "Verify stored Dev traces",
    "Job summary with counts and trace ids of failures", "Upload Playwright report and exported spans",
    "Prepare the sanitized Dev nightly JUnit artifact", "Upload the sanitized Dev nightly JUnit artifact", "Complete job",
}
NIGHTLY_DETAILS = {
    "unregistered": {"Launcher is absent from the default branch.", "No matching Actions workflow registration."},
    "lookup-failed": {"Multiple scheduled run IDs matched this slot."} | {
        f"Could not verify GitHub {phase}." for phase in (
            "runner credential", "repository metadata", "default branch revision", "default branch workflow files",
            "Actions workflow registration", "scheduled run history",
        )
    },
}
NIGHTLY_DIAGNOSTIC_DETAILS = {
    "GitHub did not report a matching job for this attempt.",
    "Could not verify GitHub attempt jobs. The workflow verdict is unchanged.",
}
GITHUB_NUMBER = r"[1-9][0-9]{0,15}"
GIT_SHA = re.compile(r"[0-9a-f]{40}")
MAX_NIGHTLY_MESSAGE_BYTES = 2048


def read_journal(options, matches, limit, *, fields=JOURNAL_FIELDS, timeout="8s"):
    """Read one bounded journal page in forward order."""
    machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    if not re.fullmatch("[0-9a-f]{32}", machine):
        raise ValueError("invalid local machine ID")
    command = [
        "/usr/bin/timeout", "--kill-after=1s", timeout,
        "/usr/bin/journalctl", "--system", "--quiet", "--no-pager",
        "--output=json", f"--output-fields={fields}", *options,
        f"_MACHINE_ID={machine}", *matches,
    ]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding="utf-8") as process:
        try:
            lines = list(islice(process.stdout, limit + 1))
            more = len(lines) > limit
            if not more and process.wait() != 0:
                raise ValueError("system journal read failed")
            return [json.loads(line) for line in lines[:limit]], more
        finally:
            if process.poll() is None:
                process.terminate()


def classify(completion, details):
    identity = ("_PID", "_MACHINE_ID", "_BOOT_ID", "UNIT", "INVOCATION_ID")
    entries = [
        row for row in details
        if all(row.get(key) == completion.get(key) for key in identity)
    ]
    successes = [row for row in entries if row["MESSAGE_ID"] == UNIT_SUCCEEDED]
    failures = [row for row in entries if row["MESSAGE_ID"] == UNIT_FAILED]
    exits = [row for row in entries if row["MESSAGE_ID"] == PROCESS_EXIT]
    clean_exit = not exits or (
        len(exits) == 1 and exits[0]["EXIT_CODE"] == "exited" and exits[0]["EXIT_STATUS"] == "0"
    )
    if completion["JOB_RESULT"] == "done" and len(successes) == 1 and not failures and clean_exit:
        # These oneshots use the default successful exit status of zero.
        return "OK", "success", 0
    result = failures[0]["UNIT_RESULT"] if len(failures) == 1 else None
    status = None
    if len(exits) == 1 and exits[0]["EXIT_CODE"] == "exited":
        status = int(exits[0]["EXIT_STATUS"])
    state = "UNKNOWN"
    if completion["JOB_RESULT"] == "failed" and result == "exit-code" and not successes:
        state = PLUGIN_STATES[completion["UNIT"]].get(status, "UNKNOWN")
    return state, result, status


def invocation_ids(rows):
    values = {row.get("INVOCATION_ID") for row in rows}
    return sorted(value for value in values if isinstance(value, str) and re.fullmatch("[0-9a-f]{32}", value))


def completion_details(ids, *, options=(), limit=PAGE_SIZE * 4, unit_matches=UNIT_MATCHES):
    if not ids:
        return []
    details, extra = read_journal(options, (
        *unit_matches, *(f"INVOCATION_ID={value}" for value in ids),
        f"MESSAGE_ID={UNIT_SUCCEEDED}", f"MESSAGE_ID={UNIT_FAILED}", f"MESSAGE_ID={PROCESS_EXIT}",
    ), limit)
    if extra:
        raise ValueError("too many matching systemd result records")
    return details


def action_state(observation):
    status, conclusion = observation["status"], observation["conclusion"]
    if status == "completed" and conclusion in ACTION_CONCLUSIONS:
        return {"status": status, "conclusion": conclusion}
    if status in RUNNING_ACTION_STATES and conclusion is None:
        return {"status": status, "conclusion": None}
    raise ValueError("invalid Actions state")


def github_number(value):
    return type(value) is int and 0 < value <= 9_007_199_254_740_991


def nightly_problem(problem, run_url, state):
    job, url = problem["job"], problem["url"]
    if job not in NIGHTLY_JOBS or not re.fullmatch(re.escape(run_url) + r"/job/" + GITHUB_NUMBER, url):
        raise ValueError("invalid nightly job identity")
    projected = {"job": job, "url": url, **action_state(problem)}
    active = projected["status"] != "completed"
    if (active and state != "stalled") or projected["conclusion"] == "success":
        raise ValueError("job state disagrees with the nightly result")
    if "step" in problem:
        step = problem["step"]
        number, name = step["number"], step["name"]
        if not github_number(number) or not isinstance(name, str):
            raise ValueError("invalid nightly step")
        status = action_state(step)
        if (active and status["status"] != "in_progress") or (
            not active and (status["status"] != "completed" or status["conclusion"] in {"success", "skipped"})
        ):
            raise ValueError("step state disagrees with the nightly result")
        projected["step"] = {"number": number, "name": name if name in NIGHTLY_STEPS else f"Step {number}", **status}
    return projected


def nightly_problems(run, run_url, state):
    primary = nightly_problem(run["problem"], run_url, state)
    if "problems" not in run:
        return [primary]
    records = run["problems"]
    if not isinstance(records, list) or not 2 <= len(records) <= len(NIGHTLY_JOBS):
        raise ValueError("invalid number of nightly problems")
    problems = [nightly_problem(record, run_url, state) for record in records]
    if (
        primary != problems[0] or len({item["job"] for item in problems}) != len(problems)
        or len({item["url"] for item in problems}) != len(problems)
    ):
        raise ValueError("inconsistent nightly problem identities")
    return problems


def describe_nightly_problem(problem):
    step = problem.get("step")
    subject = f"{problem['job']} / {step['name']}" if step else problem["job"]
    status = step if step else problem
    return f"{subject} ({status['conclusion'] or status['status']})."


def project_nightly_probe(message, completion):
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_NIGHTLY_MESSAGE_BYTES:
        raise ValueError("invalid nightly report size")
    report = json.loads(message, object_pairs_hook=unique_object)
    state = report["state"]
    if (
        report["repository"] != DEV_NIGHTLY_REPOSITORY or report["target"] != DEV_NIGHTLY_TARGET
        or DEV_NIGHTLY_VERDICTS.get(state) != (completion["state"], completion["exit_status"])
    ):
        raise ValueError("nightly report disagrees with manager result")
    scheduled = utc_timestamp(report["scheduledAt"])
    if utc_timestamp(completion["completed_at"]) < scheduled:
        raise ValueError("nightly slot follows completion")
    probe = {"repository": DEV_NIGHTLY_REPOSITORY, "target": DEV_NIGHTLY_TARGET,
             "state": state, "scheduledAt": report["scheduledAt"]}
    if "defaultBranchSha" in report:
        if not GIT_SHA.fullmatch(report["defaultBranchSha"]):
            raise ValueError("invalid nightly default branch SHA")
        probe["defaultBranchSha"] = report["defaultBranchSha"]
    detail = report.get("detail")
    if state in {"running", "stalled", "green", "red"}:
        run = report["run"]
        url, attempt, sha = run["url"], run["attempt"], run["launcherSha"]
        if not re.fullmatch(re.escape(DEV_NIGHTLY_RUN_URL) + GITHUB_NUMBER, url) or not github_number(attempt):
            raise ValueError("invalid nightly run identity")
        if sha is not None and not GIT_SHA.fullmatch(sha):
            raise ValueError("invalid nightly launcher SHA")
        projected = {"url": url, "attempt": attempt, "launcherSha": sha, **action_state(run)}
        if (
            (state in {"running", "stalled"}) != (projected["status"] != "completed")
            or (state == "green") != (projected["conclusion"] == "success")
        ):
            raise ValueError("run state disagrees with the nightly result")
        if "problem" in run or "problems" in run:
            if state not in {"red", "stalled"} or sha is None:
                raise ValueError("unattributed nightly problem")
            problems = nightly_problems(run, url, state)
            projected["problem"] = problems[0]
            if len(problems) > 1:
                projected["problems"] = problems
            probe["detail"] = " ".join(map(describe_nightly_problem, problems))
        elif state in {"red", "stalled"} and isinstance(detail, str) and detail in NIGHTLY_DIAGNOSTIC_DETAILS:
            probe["detail"] = detail
        probe["run"] = projected
    elif "run" in report:
        raise ValueError("nightly report unexpectedly has a run")
    elif isinstance(detail, str) and detail in NIGHTLY_DETAILS.get(state, ()):
        probe["detail"] = detail
    return probe


def add_nightly_probes(completed, ids):
    if not ids:
        return
    try:
        stdout, extra = read_journal((), (
            "_TRANSPORT=stdout", f"_SYSTEMD_UNIT={DEV_NIGHTLY}",
            *(f"_SYSTEMD_INVOCATION_ID={value}" for value in ids),
        ), PAGE_SIZE * 2, fields=DEV_NIGHTLY_STDOUT_FIELDS, timeout="2s")
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    if extra:
        return
    messages = defaultdict(list)
    for row in stdout:
        if not isinstance(row, dict) or row.get("_TRANSPORT") != "stdout":
            continue
        identity = tuple(row.get(key) for key in ("_MACHINE_ID", "_BOOT_ID", "_SYSTEMD_UNIT", "_SYSTEMD_INVOCATION_ID"))
        if all(isinstance(value, str) for value in identity):
            messages[identity].append(row.get("MESSAGE"))
    for completion in completed:
        identity = tuple(completion[key] for key in ("machine_id", "boot_id", "unit", "invocation_id"))
        if not all(isinstance(value, str) for value in identity):
            continue
        matching = messages[identity]
        if len(matching) == 1:
            try:
                completion["probe"] = project_nightly_probe(matching[0], completion)
            except (KeyError, TypeError, ValueError, RecursionError):
                # Optional service stdout cannot change the manager verdict or the page cursor.
                pass


def read_page(*, cursor=None, since=None, scope="signoz"):
    unit_matches = SCOPE_MATCHES[scope]
    options = [f"--cursor={cursor}"] if cursor is not None else [f"--since={since}"]
    match = (*unit_matches, "JOB_TYPE=start", f"MESSAGE_ID={JOB_DONE}", f"MESSAGE_ID={JOB_FAILED}")
    rows, more = read_journal(options, match, PAGE_SIZE + int(cursor is not None))
    if cursor is not None:
        if not rows or rows[0]["__CURSOR"] != cursor:
            raise ValueError("cannot resume cursor in system journal")
        rows = rows[1:]

    ids = invocation_ids(rows)
    details = completion_details(ids, unit_matches=unit_matches)

    completed = []
    for row in rows:
        if row.get("_PID") != "1" or f"UNIT={row.get('UNIT')}" not in unit_matches:
            raise ValueError("unexpected systemd completion record")
        identity = row.get("INVOCATION_ID")
        state, result, status = classify(row, details) if identity in ids else ("UNKNOWN", None, None)
        timestamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=int(row["__REALTIME_TIMESTAMP"])
        )
        completed.append({
            "unit": row["UNIT"], "state": state, "service_result": result,
            "exit_status": status, "invocation_id": identity,
            "machine_id": row["_MACHINE_ID"], "boot_id": row["_BOOT_ID"],
            "completed_at": timestamp.isoformat(),
            "cursor": row["__CURSOR"],
        })
    if scope == "dev-nightly":
        add_nightly_probes(completed, ids)
    return {"completed": completed, "next_cursor": rows[-1]["__CURSOR"] if rows else cursor, "more": more}


def utc_timestamp(value):
    if not isinstance(value, str) or not UTC_ISO.fullmatch(value):
        raise ValueError("expected UTC ISO timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def unique_object(fields):
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("duplicate status fields")
    return result


def read_backup_status(path, command, now):
    with path.open("rb") as file:
        data = file.read(MAX_BACKUP_STATUS_BYTES + 1)
    if len(data) > MAX_BACKUP_STATUS_BYTES:
        raise ValueError("backup status exceeds size limit")
    status = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    if (
        not isinstance(status, dict) or status.get("command") != command
        or type(status.get("ok")) is not bool or not isinstance(status.get("report"), dict)
    ):
        raise ValueError("invalid backup status shape")
    timestamp = utc_timestamp(status.get("at"))
    if timestamp > now:
        raise ValueError("future backup status")
    return timestamp, status["ok"], status["report"]


def read_archive(report, store, observed_at):
    archive = report.get(store)
    if not isinstance(archive, dict) or type(archive.get("complete")) is not bool:
        return None, None
    try:
        timestamp = utc_timestamp(archive.get("archiveAt"))
    except ValueError:
        return None, None
    if timestamp > observed_at:
        return None, None
    return timestamp, archive["complete"]


def read_verified_upload(report, store, pushed_at, checked_at):
    entry = report.get(store)
    if not isinstance(entry, dict) or type(entry.get("uploaded")) is not int or entry["uploaded"] < 0:
        return None
    try:
        if "verifiedAt" in entry:
            verified_at = utc_timestamp(entry["verifiedAt"])
        elif entry["uploaded"] > 0:
            verified_at = pushed_at
        else:
            return None
    except ValueError:
        return None
    if not checked_at < verified_at <= pushed_at:
        return None
    archive_at, complete = read_archive(report, store, verified_at)
    return archive_at if complete else None


def read_backup_freshness(path=BACKUP_CHECK_STATUS, *, push_path=BACKUP_PUSH_STATUS, now=None):
    now = now or datetime.now(timezone.utc)
    checked_at, probe_state, probe_reason, check_report = None, "UNKNOWN", "invalid-probe", {}
    try:
        checked_at, ok, check_report = read_backup_status(path, "check", now)
    except FileNotFoundError:
        probe_reason = "missing-probe"
    except PermissionError:
        # A reader without access to the root-only receipt is a setup fault, not missing evidence.
        probe_reason = "unreadable-probe"
    except (OSError, ValueError, RecursionError):
        pass
    else:
        if now - checked_at > MAX_BACKUP_AGE:
            probe_state, probe_reason = "CRITICAL", "stale-probe"
        elif ok:
            probe_state, probe_reason = "OK", "fresh-probe"
        else:
            probe_state, probe_reason = "CRITICAL", "probe-failed"

    probe = {
        "state": probe_state, "reason": probe_reason,
        "checked_at": checked_at.isoformat() if checked_at else None,
    }
    stores = {
        store: {"state": "UNKNOWN", "reason": "unverified-archive", "archive_at": None}
        for store in BACKUP_STORES
    }
    if probe_reason in {"missing-probe", "unreadable-probe", "invalid-probe", "stale-probe"}:
        return {"probe": probe, "stores": stores}

    pushed_at, push_report = None, {}
    try:
        pushed_at, _, report = read_backup_status(push_path, "push", now)
        if pushed_at > checked_at:
            push_report = report
    except (OSError, ValueError, RecursionError):
        pass

    for store in BACKUP_STORES:
        archive_at, complete = read_archive(check_report, store, checked_at)
        pushed_archive_at = read_verified_upload(push_report, store, pushed_at, checked_at)
        if pushed_archive_at is not None and (archive_at is None or pushed_archive_at > archive_at):
            archive_at, complete = pushed_archive_at, True

        if complete is False:
            state, reason = "CRITICAL", "incomplete-archive"
        elif complete and now - archive_at > MAX_BACKUP_AGE:
            state, reason = "CRITICAL", "stale-archive"
        elif complete:
            state, reason = "OK", "fresh-archive"
        else:
            continue
        stores[store] = {"state": state, "reason": reason, "archive_at": archive_at.isoformat()}

    return {"probe": probe, "stores": stores}


def timer_state(timer):
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "--no-pager", "--property=ActiveState", "--value", timer],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", timeout=4,
    )
    state = result.stdout.strip()
    if state not in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance"}:
        raise ValueError("invalid systemd timer state")
    return state


def local_boot_id():
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().replace("-", "")
    if not re.fullmatch("[0-9a-f]{32}", boot_id):
        raise ValueError("invalid current boot ID")
    return boot_id


def check_cadence(unit):
    result = {"unit": unit}
    unit_matches = ("_PID=1", f"UNIT={unit}")
    try:
        if timer_state(unit.removesuffix(".service") + ".timer") != "active":
            return dict(result, state="CRITICAL", reason="timer-not-active")
        boot_id = local_boot_id()
        rows, extra = read_journal((f"--boot={boot_id}", "--lines=1"), (
            *unit_matches, "JOB_TYPE=start", f"MESSAGE_ID={JOB_DONE}", f"MESSAGE_ID={JOB_FAILED}",
        ), 1)
        if not rows and not extra:
            return dict(result, state="CRITICAL", reason="missing-completion")
        if extra or len(rows) != 1 or rows[0]["UNIT"] != unit or rows[0]["_BOOT_ID"] != boot_id:
            raise ValueError("unexpected completion record")
        completion = rows[0]
        timestamp = int(completion["__MONOTONIC_TIMESTAMP"])
        age = time.monotonic_ns() // 1000 - timestamp
        if timestamp <= 0 or age < 0:
            raise ValueError("invalid current-boot timestamp")
        result["age_seconds"] = age / 1_000_000
        if age >= MAX_COMPLETION_AGE_SECONDS * 1_000_000:
            return dict(result, state="CRITICAL", reason="stale-completion")
        ids = invocation_ids(rows)
        details = completion_details(ids, options=(f"--boot={boot_id}",), limit=4, unit_matches=unit_matches)
        probe_state, service_result, _ = classify(completion, details) if ids else ("UNKNOWN", None, None)
        if service_result is None:
            return dict(result, state="UNKNOWN", reason="unattributed-completion")
        return dict(result, state="OK", reason="recent-completion", last_probe_state=probe_state)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return dict(result, state="UNKNOWN", reason="invalid-runner-state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=SCOPE_MATCHES, help="which manager units to read (default signoz for pages)")
    start = parser.add_mutually_exclusive_group(required=True)
    start.add_argument("--since", help="Journal start time for the first page")
    start.add_argument("--cursor", help="Last completion cursor accepted by the consumer")
    start.add_argument("--check-cadence", choices=CADENCE_CHECKS, help="Check a running timer and its latest completion")
    args = parser.parse_args()
    if args.check_cadence:
        scope, unit = CADENCE_CHECKS[args.check_cadence]
        if args.scope is not None and args.scope != scope:
            parser.error(f"--check-cadence {args.check_cadence} requires --scope {scope} when a scope is specified")
        result = check_cadence(unit)
        print(json.dumps(result, sort_keys=True))
        return CADENCE_EXIT[result["state"]]
    scope = args.scope or "signoz"
    try:
        page = read_page(since=args.since, cursor=args.cursor, scope=scope)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Cannot read SigNoz check completions: {error}", file=sys.stderr)
        return 3
    if scope == "offsite-backup":
        page["freshness"] = read_backup_freshness()
    print(json.dumps(page, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
