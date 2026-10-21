#!/usr/bin/env python3
"""Run six actual installer cases in one bounded, owned hosted-CI container.

This runner is deliberately restricted to fresh Linux GitHub jobs. It does not
replace or relax the resource guards used for native tests on developer Macs.
Only sanitized outcomes and immutable source/image bindings are exported.
"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = frozenset(
    {
        "test_fresh_chart_realm_installer_setup_and_second_reconcile_converge",
        "test_five_prepared_actors_issue_exact_native_subject_role_audience_tokens",
        "test_existing_humans_and_managed_dry_run_are_preserved",
        "test_real_actor_tokens_keep_group_commit_and_protected_denials",
        "test_receiver_only_policies_refuse_stock_mutation_with_real_actor_tokens",
        "test_unmanaged_existing_actor_is_refused_without_adoption",
    }
)


def accepted(cases, executed, errors):
    return (
        executed == 6
        and errors == 0
        and set(cases) == EXPECTED
        and all(outcomes == ["passed"] for outcomes in cases.values())
    )


def verified_child(report, run_id, commit):
    return (
        report.get("runId") == run_id
        and report.get("commit") == commit
        and accepted(report.get("cases", {}), report.get("tests"), report.get("errors"))
    )


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def output(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def source_hashes():
    paths = output("git", "ls-files", "-z").split("\0")
    result = {}
    for name in sorted(filter(None, paths)):
        path = ROOT / name
        data = (
            b"symlink\0" + os.readlink(path).encode()
            if path.is_symlink()
            else path.read_bytes()
        )
        result[name] = hashlib.sha256(data).hexdigest()
    return result


class SanitizedResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.cases = {}
        self.error_count = 0

    def record(self, test, status):
        self.cases.setdefault(test.id().rsplit(".", 1)[-1], []).append(status)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        self.record(test, "failure")
        self.failures.append((test, "Native assertion failed; payload omitted"))

    def addError(self, test, err):
        self.error_count += 1
        self.record(test, "error")
        self.errors.append((test, "Native setup or execution error; payload omitted"))

    def addSkip(self, test, reason):
        super().addSkip(test, "A skipped native case is not accepted")
        self.record(test, "skipped")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            if issubclass(err[0], test.failureException):
                self.addFailure(test, err)
            else:
                self.addError(test, err)


def require_child(run_id, container):
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native cases require the current hosted parent")
    base = os.environ.get("SCENARIO_C_KEYCLOAK_URL", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id or "") or not re.fullmatch(
        r"http://127\.0\.0\.1:[0-9]{1,5}/auth", base
    ):
        raise RuntimeError("Native cases require an owned loopback fixture binding")
    expected_name = "oriso-c-installer-" + run_id[:12]
    if container != expected_name or available_bytes() < 2 * 1024**3:
        raise RuntimeError("Native cases lack owned resource reserve")
    actual = json.loads(output("docker", "inspect", container))[0]
    if (
        actual.get("Config", {}).get("Labels", {}).get("oriso-c-installer-run")
        != run_id
        or actual.get("HostConfig", {}).get("Memory") != 768 * 1024**2
        or actual.get("HostConfig", {}).get("NanoCpus") != 2_000_000_000
        or "http://" + output("docker", "port", container, "8080/tcp") + "/auth" != base
    ):
        raise RuntimeError(
            "Native cases lack exact parent container ownership and limits"
        )


def serialize_result(result, report, run_id, commit):
    failures = sum("failure" in statuses for statuses in result.cases.values())
    errors = sum("error" in statuses for statuses in result.cases.values())
    skipped = sum("skipped" in statuses for statuses in result.cases.values())
    green = accepted(result.cases, result.testsRun, result.error_count)
    write(
        report,
        {
            "runId": run_id,
            "commit": commit,
            "cases": result.cases,
            "tests": result.testsRun,
            "failures": failures,
            "errors": errors,
            "skipped": skipped,
            "subtestFailureObservations": len(result.failures),
            "accepted": green,
        },
    )
    xml = ET.Element(
        "testsuite",
        name="scenario-c-installer-native",
        tests=str(len(result.cases)),
        failures=str(failures),
        errors=str(errors),
        skipped=str(skipped),
    )
    for name, outcomes in result.cases.items():
        case = ET.SubElement(
            xml, "testcase", classname="NativeScenarioCReconcileTest", name=name
        )
        for status in ("failure", "error", "skipped"):
            if status in outcomes:
                ET.SubElement(case, status, message="Native outcome; payload omitted")
    ET.ElementTree(xml).write(
        report.with_suffix(".xml"), encoding="utf-8", xml_declaration=True
    )
    return green


def cases(report, run_id=None, container=None):
    require_child(run_id, container)
    sys.path.insert(0, str(ROOT / "tests"))
    import keycloak_scenario_c_reconcile_test as native

    result = SanitizedResult()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        native.NativeScenarioCReconcileTest
    )
    # Raw assertion messages can contain native representations. Keep them in a
    # disposable local file, never in the uploaded report or job output.
    with tempfile.TemporaryFile(mode="w+") as private_log:
        with contextlib.redirect_stdout(private_log), contextlib.redirect_stderr(
            private_log
        ):
            suite.run(result)
    green = serialize_result(result, report, run_id, output("git", "rev-parse", "HEAD"))
    recorded = json.loads(report.read_text())
    recorded["cliDiagnostics"] = native.NativeScenarioCReconcileTest.cli_diagnostics
    write(report, recorded)
    return 0 if green else 1


def available_bytes():
    values = dict(
        line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
    )
    available = int(values["MemAvailable"].split()[0]) * 1024
    limit = Path("/sys/fs/cgroup/memory.max")
    used = Path("/sys/fs/cgroup/memory.current")
    if limit.exists() and used.exists() and limit.read_text().strip() != "max":
        available = min(available, int(limit.read_text()) - int(used.read_text()))
    return available


def hosted(report):
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError(
            "Use the existing guarded local runner outside hosted Linux CI"
        )
    if available_bytes() < 4 * 1024**3:
        raise RuntimeError("Fresh hosted job lacks required 4GiB startup headroom")
    sys.path.insert(0, str(ROOT / "tests"))
    import _keycloak_container as fixture

    before = source_hashes()
    write(report.parent / "source-before.json", before)
    # A zero-exit child without a fresh report must not reuse an earlier green.
    report.unlink(missing_ok=True)
    report.with_suffix(".xml").unlink(missing_ok=True)
    run_id = uuid.uuid4().hex
    name = "oriso-c-installer-" + run_id[:12]
    label = "oriso-c-installer-run=" + run_id
    process = None
    started_container = False
    receipt = {
        "commit": output("git", "rev-parse", "HEAD"),
        "sourceCount": len(before),
        "runId": run_id,
        "nativeImage": fixture.IMAGE,
        "nativeCasesAccepted": False,
        "containerRemoved": False,
        "localMacRuntime": False,
        "scope": "six stock-Keycloak installer cases; not combined providers or full Scenario C",
    }
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                label,
                "--memory",
                "768m",
                "--cpus",
                "2",
                "-p",
                "127.0.0.1::8080",
                "-e",
                "JAVA_OPTS_APPEND=-Xmx384m -XX:MaxMetaspaceSize=192m -XX:ActiveProcessorCount=2",
                "-e",
                "KC_BOOTSTRAP_ADMIN_USERNAME=" + fixture.INSTALLER[0],
                "-e",
                "KC_BOOTSTRAP_ADMIN_PASSWORD=" + fixture.INSTALLER[1],
                fixture.IMAGE,
                "start-dev",
                "--http-relative-path=/auth",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        started_container = True
        inspected = json.loads(output("docker", "inspect", name))[0]
        receipt["nativeImageId"] = inspected["Image"]
        port = output("docker", "port", name, "8080/tcp").splitlines()[0]
        base = "http://" + port + "/auth"
        deadline = time.monotonic() + 300
        while True:
            if available_bytes() < 2 * 1024**3:
                raise RuntimeError("Hosted job lost required 2GiB reserve")
            try:
                fixture.master_admin(base)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned native readiness deadline") from None
                time.sleep(1)
        env = {
            **os.environ,
            "SCENARIO_C_KEYCLOAK_URL": base,
            "ORISO_REQUIRE_KEYCLOAK_DOCKER": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "cases",
                "--report",
                str(report),
                "--run-id",
                run_id,
                "--container",
                name,
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 480
        while process.poll() is None:
            if available_bytes() < 2 * 1024**3 or time.monotonic() >= deadline:
                raise RuntimeError("Owned native execution resource/deadline gate")
            time.sleep(1)
        receipt["caseExitCode"] = process.returncode
        child = json.loads(report.read_text())
        receipt["nativeCasesAccepted"] = process.returncode == 0 and verified_child(
            child, run_id, receipt["commit"]
        )
    except Exception as error:
        # No native exception text, URL, credential, assertion payload or token.
        receipt["runnerErrorClass"] = type(error).__name__
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        if started_container:
            actual = json.loads(output("docker", "inspect", name))[0]
            if (
                actual.get("Config", {}).get("Labels", {}).get("oriso-c-installer-run")
                != run_id
            ):
                raise RuntimeError("Owned native container label mismatch")
            subprocess.run(
                ["docker", "rm", "-fv", name],
                check=True,
                capture_output=True,
                timeout=60,
            )
            receipt["containerRemoved"] = True
        after = source_hashes()
        write(report.parent / "source-after.json", after)
        receipt["sourceStable"] = before == after
        write(report.parent / "receipt.json", receipt)
    print(json.dumps(receipt))
    return (
        0
        if all(
            receipt.get(key) is True
            for key in ("nativeCasesAccepted", "containerRemoved", "sourceStable")
        )
        else 1
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hosted", "cases"))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--container")
    args = parser.parse_args()
    return (
        hosted(args.report)
        if args.mode == "hosted"
        else cases(args.report, args.run_id, args.container)
    )


if __name__ == "__main__":
    sys.exit(main())
