#!/usr/bin/env python3
"""Run six actual installer cases in one bounded, owned hosted-CI container.

This runner is deliberately restricted to fresh Linux GitHub jobs. It does not
replace or relax the resource guards used for native tests on developer Macs.
Only sanitized outcomes and immutable source/image bindings are exported.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import signal
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
SUPPORT_REF = "a8f5eec02a6de4faaa36a709f0ce4b42e8926c80"
RESOURCE_SHA = "37d29adc5e8e31aac9467e1ca1c1587b2d84db6239ea79f016cd0442f7916fec"
BOOTSTRAP_SHA = "0cf44ec7e99edaac9916f8f9dd2a17bca295e3348b8ab0fac2bb28c5a10e5949"
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
    return subprocess.check_output(args, cwd=ROOT, text=True, timeout=30).strip()


def source_hashes(root=ROOT):
    paths = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, text=True, timeout=30
    ).split("\0")
    result = {}
    for name in sorted(filter(None, paths)):
        path = root / name
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
    recorded["provisionerBoundary"] = (
        native.NativeScenarioCReconcileTest.provisioner_observations
    )
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


def load_support(root):
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() != SUPPORT_REF:
        raise RuntimeError("Exact reviewed native support ref is required")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise RuntimeError("Native support checkout must be clean")
    for name, digest in (("native_context_resources.py", RESOURCE_SHA),
                         ("test-bootstrap-context-roundtrip.py", BOOTSTRAP_SHA)):
        if hashlib.sha256((root / "scripts" / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Reviewed native resource source does not match")
    spec = importlib.util.spec_from_file_location(
        "installer_native_support", root / "scripts/test-bootstrap-context-roundtrip.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_accepted(cleanup, groups, name):
    return (
        len(cleanup) == 1
        and cleanup[0].get("kind") == "container"
        and cleanup[0].get("name") == name
        and cleanup[0].get("confirmedAbsent") is True
        and isinstance(cleanup[0].get("anonymousVolumesConfirmedAbsent"), list)
        and isinstance(cleanup[0].get("runtime"), dict)
        and cleanup[0]["runtime"].get("oomKilled") is False
        and type(cleanup[0]["runtime"].get("restartCount")) is int
        and cleanup[0]["runtime"]["restartCount"] == 0
        and len(groups) == 1
        and type(groups[0].get("processGroup")) is int
        and groups[0]["processGroup"] > 0
        and groups[0].get("confirmedAbsent") is True
    )


def hosted(report, support_root):
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native installer execution requires fresh hosted Linux")
    head = output("git", "rev-parse", "HEAD")
    if head != os.environ.get("GITHUB_SHA") or not re.fullmatch("[a-f0-9]{40}", head):
        raise RuntimeError("Native installer source must match the exact hosted ref")
    if output("git", "status", "--porcelain"):
        raise RuntimeError("Native installer source must be exact and clean")
    support = load_support(support_root)
    sys.path.insert(0, str(ROOT / "tests"))
    import _keycloak_container as fixture

    report.parent.mkdir(parents=True, exist_ok=True)
    roots = {"helm": ROOT, "support": support_root}
    before = {role: source_hashes(root) for role, root in roots.items()}
    refs_before = {"helm": head, "support": SUPPORT_REF}
    write(report.parent / "source-before.json", before)
    report.unlink(missing_ok=True)
    report.with_suffix(".xml").unlink(missing_ok=True)
    run_id = uuid.uuid4().hex
    name = "oriso-c-installer-" + run_id[:12]
    label = "oriso-c-installer-run=" + run_id
    owned = support.resources.OwnedDocker("oriso-c-installer-run", run_id)
    lease = support.configure_memory(2896, report.parent)
    overall = time.monotonic() + 900
    receipt = {
        "commit": output("git", "rev-parse", "HEAD"),
        "supportCommit": SUPPORT_REF,
        "refsBefore": refs_before,
        "resourceHelperSHA256": RESOURCE_SHA,
        "sourceCount": {role: len(values) for role, values in before.items()},
        "runId": run_id,
        "budgetMiB": 2896,
        "plannedContainer": name,
        "plannedProcesses": ["installer-native-cases"],
        "nativeImage": fixture.IMAGE,
        "nativeCasesAccepted": False,
        "containerRemoved": False,
        "localMacRuntime": False,
        "scope": "six stock-Keycloak installer cases; not combined providers or full Scenario C",
    }
    def deadline(signum, frame):
        raise RuntimeError("Native installer total deadline exceeded")
    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(900)
    try:
        lease.check(starting=True)
        # Syntax is checked before any actor/container; heavy Java is not used here.
        for path in (ROOT / "scripts/reconcile-keycloak-scenario-c.py",
                     ROOT / "tests/keycloak_scenario_c_reconcile_test.py"):
            compile(path.read_text(), str(path), "exec")
        owned.plan("container", name)
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "--label", label,
             "--memory", "768m", "--cpus", "2", "-p", "127.0.0.1::8080",
             "-e", "JAVA_OPTS_APPEND=-Xmx384m -XX:MaxMetaspaceSize=192m -XX:ActiveProcessorCount=2",
             "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=" + fixture.INSTALLER[0],
             "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=" + fixture.INSTALLER[1],
             fixture.IMAGE, "start-dev", "--http-relative-path=/auth"],
            check=True, capture_output=True, timeout=60,
        )
        inspected = json.loads(output("docker", "inspect", name))[0]
        receipt["nativeImageId"] = inspected["Image"]
        base = "http://" + output("docker", "port", name, "8080/tcp").splitlines()[0] + "/auth"
        ready_by = min(time.monotonic() + 120, overall)
        while True:
            support.free_guard()
            try:
                fixture.master_admin(base)
                break
            except Exception:
                if time.monotonic() >= ready_by:
                    raise RuntimeError("Owned native readiness deadline") from None
                time.sleep(1)
        env = {**os.environ, "SCENARIO_C_KEYCLOAK_URL": base,
               "ORISO_REQUIRE_KEYCLOAK_DOCKER": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryDirectory(prefix="oriso-installer-private-") as private:
            receipt["caseExitCode"] = support.bounded(
                [sys.executable, "-B", str(Path(__file__).resolve()), "cases",
                 "--report", str(report), "--run-id", run_id, "--container", name],
                ROOT, env, Path(private) / "cases.log", 480, overall,
            )
        child = json.loads(report.read_text())
        receipt["nativeCasesAccepted"] = receipt["caseExitCode"] == 0 and verified_child(
            child, run_id, receipt["commit"]
        )
        receipt["provisionerBoundary"] = child.get("provisionerBoundary", {})
        support.free_guard()
    except Exception as error:
        receipt["runnerErrorClass"] = type(error).__name__
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        try:
            receipt["cleanup"] = owned.cleanup()
        except support.resources.CleanupRefusal as error:
            receipt["cleanup"] = error.completed
            receipt["cleanupErrors"] = error.errors
        group_path = report.parent / "process-cleanup.jsonl"
        groups = [json.loads(line) for line in group_path.read_text().splitlines()] if group_path.exists() else []
        receipt["processCleanup"] = groups
        receipt["runtimeAccepted"] = runtime_accepted(receipt.get("cleanup", []), groups, name)
        receipt["containerRemoved"] = bool(receipt.get("cleanup")) and all(
            value.get("confirmedAbsent") is True for value in receipt["cleanup"]
        )
        after = {role: source_hashes(root) for role, root in roots.items()}
        write(report.parent / "source-after.json", after)
        receipt["refsAfter"] = {
            role: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=30).strip()
            for role, root in roots.items()
        }
        receipt["sourceStable"] = before == after and refs_before == receipt["refsAfter"]
        receipt["totalDeadlineRetained"] = time.monotonic() <= overall
        receipt["accepted"] = all(receipt.get(key) is True for key in (
            "nativeCasesAccepted", "runtimeAccepted", "sourceStable", "totalDeadlineRetained")) and not receipt.get("cleanupErrors")
        write(report.parent / "receipt.json", receipt)
    print(json.dumps(receipt))
    return 0 if receipt["accepted"] else 1



def offline(report, docs_root):
    docs_ref = "18d7fe0bf52028bb554ec7c3d209a2ae367219ce"
    roots = {"helm": ROOT, "docs": docs_root}
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=docs_root, text=True).strip() != docs_ref:
        raise RuntimeError("Reviewed exact offline docs fixture is required")
    refs_before = {role: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=30).strip()
                   for role, root in roots.items()}
    if refs_before["helm"] != os.environ.get("GITHUB_SHA"):
        raise RuntimeError("Offline source must match the exact hosted ref")
    for root in roots.values():
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True, timeout=30).strip():
            raise RuntimeError("Offline inputs must be exact and clean")
    before = {role: source_hashes(root) for role, root in roots.items()}
    receipt = {"helmCommit": refs_before["helm"], "docsCommit": docs_ref,
               "refsBefore": refs_before, "sourceBefore": before, "checks": []}
    report.parent.mkdir(parents=True, exist_ok=True)
    class OfflineResult(SanitizedResult):
        def record(self, test, status):
            self.cases.setdefault(test.id(), []).append(status)
    sys.path.insert(0, str(ROOT / "tests"))
    result = OfflineResult()
    with tempfile.TemporaryFile(mode="w+") as private:
        with contextlib.redirect_stdout(private), contextlib.redirect_stderr(private):
            unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py").run(result)
    cases = result.cases
    counts = {"tests": result.testsRun,
              "failures": sum("failure" in v for v in cases.values()),
              "errors": sum("error" in v for v in cases.values()),
              "skipped": sum("skipped" in v for v in cases.values())}
    xml = ET.Element("testsuite", name="scenario-c-installer-offline", **{k: str(v) for k, v in counts.items()})
    for name, statuses in cases.items():
        node = ET.SubElement(xml, "testcase", name=name)
        for status in ("failure", "error", "skipped"):
            if status in statuses:
                ET.SubElement(node, status, message="Offline outcome; payload omitted")
    ET.ElementTree(xml).write(report.with_suffix(".xml"), encoding="utf-8", xml_declaration=True)
    receipt["unitCounts"], receipt["cases"] = counts, cases
    with tempfile.TemporaryDirectory(prefix="oriso-installer-offline-") as temporary:
        lint = ["helm", "lint", ".", "-f", "values.yaml.default", "-f", "secrets.yaml.default",
                "-f", "tests/fixtures/values-render-domain.yaml", "-f", "tests/fixtures/render-required-secrets.yaml",
                "--set-string", "global.emailBrandingName=Render Test Platform",
                "--set-string", "global.emailLegalOrganisationName=Render Test Foundation",
                "--set-string", "global.secrets.redisdefaultPass=synthetic-redis",
                "--set-string", "userService.smtpUser=synthetic-smtp",
                "--set-string", "userService.smtpPassword=synthetic-password"]
        commands = [("helm-lint", lint), ("helm-package", ["helm", "package", ".", "--destination", temporary])]
        commands += [(str(path.relative_to(ROOT)), [sys.executable, str(path)])
                     for path in sorted((ROOT / "tests").glob("render_*_test.py"))]
        commands += [(name, ["bash", name]) for name in
                     ("tests/check-no-bare-ip-servername.sh", "tests/guardrail_selftest.sh")]
        for name, command in commands:
            try:
                status = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=300).returncode
            except subprocess.TimeoutExpired:
                status = 124
            receipt["checks"].append({"name": name, "exitCode": status})
    after = {role: source_hashes(root) for role, root in roots.items()}
    receipt["refsAfter"] = {role: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=30).strip()
                            for role, root in roots.items()}
    receipt["sourceAfter"] = after
    receipt["sourceStable"] = before == after and refs_before == receipt["refsAfter"]
    receipt["accepted"] = (receipt["sourceStable"] and counts["tests"] > 0
                            and len(cases) == counts["tests"]
                            and all(statuses == ["passed"] for statuses in cases.values())
                            and not any(counts[k] for k in ("failures", "errors", "skipped"))
                            and all(value["exitCode"] == 0 for value in receipt["checks"]))
    write(report, receipt)
    print(json.dumps({"accepted": receipt["accepted"], "unitCounts": counts, "checks": receipt["checks"]}))
    return 0 if receipt["accepted"] else 1

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hosted", "cases", "offline"))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--container")
    parser.add_argument("--support", type=Path)
    parser.add_argument("--docs", type=Path)
    args = parser.parse_args()
    if args.mode == "offline":
        return offline(args.report, args.docs.resolve())
    return (
        hosted(args.report, args.support.resolve())
        if args.mode == "hosted"
        else cases(args.report, args.run_id, args.container)
    )


if __name__ == "__main__":
    sys.exit(main())
