#!/usr/bin/env python3
"""Send independent Dev nightly incidents to the operations Slack channel."""

import argparse
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

if __package__:
    from . import signoz_check_journal as journal
else:
    import signoz_check_journal as journal


WATCH_REASONS = {
    "timer-not-active": "The runner timer is inactive.",
    "missing-completion": "The runner has no completion on this boot.",
    "stale-completion": "The runner's last completion is too old.",
    "unattributed-completion": "The runner's last completion cannot be attributed to the monitor.",
    "invalid-runner-state": "The runner state could not be verified.",
}
SLACK_WEBHOOK = re.compile(r"https://hooks\.slack\.com(/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+)")
SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CURSOR = re.compile(r"[A-Za-z0-9=;:+-]{1,512}")
READ_LIMIT = 128 * 1024


def read_runner(alias, *args):
    command = shlex.join((
        "/usr/bin/python3", "-I", "/opt/oriso/signoz-uptime/signoz_check_journal.py", *args,
    ))
    result = subprocess.run(
        ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=5", alias, command],
        capture_output=True, timeout=25, check=False,
    )
    if len(result.stdout) > READ_LIMIT:
        raise ValueError("oversized runner response")
    return result.returncode, json.loads(result.stdout, object_pairs_hook=journal.unique_object)


def delivery_path(webhook_file):
    with webhook_file.open(encoding="ascii") as config:
        match = SLACK_WEBHOOK.fullmatch(config.read(512).strip())
    if not match:
        raise ValueError("invalid Slack webhook credential")
    return match[1]


def send_slack(path, message, *, connect=http.client.HTTPSConnection):
    connection = connect("hooks.slack.com", timeout=8)
    try:
        payload = {
            "text": message, "mrkdwn": False, "link_names": False,
            "unfurl_links": False, "unfurl_media": False,
        }
        connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        with connection.getresponse() as result:
            if result.status != 200 or result.read(32).strip() != b"ok":
                raise OSError("Slack did not accept the notification")
    finally:
        connection.close()


def load_state(path):
    if not path.exists():
        return {"cursor": None, "nightly": None, "watcher": None}
    saved = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=journal.unique_object)
    if set(saved) != {"cursor", "nightly", "watcher"} or (
        saved["cursor"] is not None and not CURSOR.fullmatch(saved["cursor"])
    ) or any(saved[key] is not None and not isinstance(saved[key], str) for key in ("nightly", "watcher")):
        raise ValueError("invalid saved cursor or incident state")
    return saved


def save_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        pending = Path(handle.name)
        try:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def cadence_result(read):
    try:
        code, result = read("--check-cadence", "dev-nightly")
        state, reason = result["state"], result["reason"]
        if result["unit"] != journal.DEV_NIGHTLY or journal.CADENCE_EXIT[state] != code:
            raise ValueError("unexpected runner response")
        if state == "OK" and reason == "recent-completion":
            return "OK", "The monitor runner is reporting again."
        return state, WATCH_REASONS[reason]
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return "UNKNOWN", "The monitor runner could not be reached or verified."


def completion_page(read, cursor):
    start = ("--cursor", cursor) if cursor else ("--since", "10 minutes ago")
    code, page = read("--scope", "dev-nightly", *start)
    entries = page["completed"]
    if code or not isinstance(entries, list) or len(entries) > journal.PAGE_SIZE or type(page["more"]) is not bool:
        raise ValueError("invalid journal page")
    cursors = [entry["cursor"] for entry in entries]
    if (
        any(not CURSOR.fullmatch(value) or value == cursor for value in cursors)
        or len(set(cursors)) != len(cursors)
        or page["next_cursor"] != (cursors[-1] if cursors else cursor)
        or (page["more"] and not cursors)
    ):
        raise ValueError("invalid or repeated journal cursor")
    for entry in entries:
        if entry["unit"] != journal.DEV_NIGHTLY or entry["state"] not in {"OK", "PENDING", "CRITICAL", "UNKNOWN"}:
            raise ValueError("unexpected journal completion")
        journal.utc_timestamp(entry["completed_at"])
    return entries


def nightly_result(completion):
    state = completion["state"]
    if state == "PENDING":
        return state, ""
    try:
        probe = journal.project_nightly_probe(json.dumps(completion["probe"]), completion)
    except (KeyError, TypeError, ValueError):
        if state == "OK":
            state = "UNKNOWN"
        return state, "The monitor produced no verified workflow evidence."

    rows = [f"{journal.DEV_NIGHTLY_TARGET} · {probe['state']} · {probe['scheduledAt']}"]
    if "defaultBranchSha" in probe:
        rows.append(f"Default branch revision {probe['defaultBranchSha']}")
    if "detail" in probe:
        rows.append(probe["detail"])
    if run := probe.get("run"):
        if run["launcherSha"]:
            rows.append(f"Launcher revision {run['launcherSha']}")
        rows.append(f"GitHub attempt {run['attempt']} {run['url']}")
        for problem in run.get("problems", [run["problem"]] if "problem" in run else []):
            rows.append(f"{problem['job']} {problem['url']}")
    return state, "\n".join(rows)


def relay(read, send, path):
    saved = load_state(path)

    def apply(key, level, evidence, cursor=None):
        previous = saved[key]
        label = "Dev nightly" if key == "nightly" else "Dev nightly monitor runner"
        if level == "OK" and previous:
            send(f"{label} recovered\n{evidence}")
            saved[key] = None
        elif level in {"CRITICAL", "UNKNOWN"}:
            message = f"{label} · {level}\n{evidence}"
            if previous != message:
                send(message)
                saved[key] = message
        if cursor is not None:
            saved["cursor"] = cursor
        if cursor is not None or previous != saved[key]:
            save_state(path, saved)

    health, evidence = cadence_result(read)
    try:
        entries = completion_page(read, saved["cursor"])
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        if health == "OK":
            health, evidence = "UNKNOWN", "The journal could not be read from the last accepted cursor."
        entries = []
    apply("watcher", health, evidence)
    for entry in entries:
        state, detail = nightly_result(entry)
        apply("nightly", state, detail, entry["cursor"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", default="sig-check-runner", help="Configured SSH host alias")
    parser.add_argument("--webhook-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()
    if not SSH_ALIAS.fullmatch(args.runner):
        parser.error("runner must be an SSH host alias")
    try:
        path = delivery_path(args.webhook_file)
        lock_path = args.state_file.with_name(args.state_file.name + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            relay(lambda *options: read_runner(args.runner, *options),
                  lambda message: send_slack(path, message), args.state_file)
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        print("Could not relay the Dev nightly incident or save its cursor.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
