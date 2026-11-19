"""Exercise the host backup command against a disposable kubectl fixture."""

import gzip
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_mariadb.py"
KUBECTL_FIXTURE = """
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["BACKUP_TEST_CALLS"], "a") as calls:
    calls.write(json.dumps(args) + "\\n")

action = args[6]
scenario = os.environ["BACKUP_TEST_SCENARIO"]
if action == "wait":
    sys.exit(1 if scenario == "missing-pod" else 0)
if action == "get":
    if scenario != "missing-uid":
        print("synthetic-pod-uid", "False" if scenario == "replaced-pod" else "True")
    sys.exit(0)
if any("SHOW DATABASES" in arg for arg in args):
    if scenario == "discovery-failure":
        print("userservice")
        sys.exit(1)
    print("information_schema\\nmysql\\nperformance_schema\\nsys")
    if scenario != "no-app-schemas":
        print("tenantservice\\nuserservice")
    sys.exit(0)

schema = args[-1]
if scenario == "empty-dump":
    sys.exit(0)
print(f"-- synthetic dump of {schema}", flush=True)
if scenario in {"hanging-dump", "closed-output-dump"}:
    if scenario == "closed-output-dump":
        os.close(sys.stdout.fileno())
    Path(os.environ["BACKUP_TEST_CHILD"]).write_text(str(os.getpid()))
    time.sleep(30)
sys.exit(1 if scenario == "partial-dump" and schema == "userservice" else 0)
"""


class MariaDbBackupTest(unittest.TestCase):
    def backup_invocation(self, scenario, *options, with_output=True):
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_TEST_CALLS": str(self.calls),
            "BACKUP_TEST_SCENARIO": scenario,
            "BACKUP_TEST_CHILD": str(self.child),
        }
        command = [sys.executable, str(SCRIPT), str(self.kubeconfig), "audit-dev", "caritas"]
        if with_output:
            command.append(str(self.output))
        return command + list(options), env

    def run_backup(self, scenario, *options, with_output=True):
        command, env = self.backup_invocation(scenario, *options, with_output=with_output)
        return subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)

    def stop_fixture_if_running(self):
        if self.child.is_file():
            try:
                os.kill(int(self.child.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait_for_fixture(self, process):
        for _ in range(100):
            if self.child.is_file():
                return int(self.child.read_text())
            if process.poll() is not None:
                self.fail("Backup process exited before the synthetic dump started")
            time.sleep(0.02)
        self.fail("Synthetic dump did not start")


    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(scratch)
        self.output = root / "backups"
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.calls = root / "calls.jsonl"
        self.child = root / "child.pid"
        self.addCleanup(self.stop_fixture_if_running)
        self.kubeconfig = root / "synthetic-kubeconfig"
        self.kubeconfig.touch()
        fake = self.bin_dir / "kubectl"
        fake.write_text(f"#!{sys.executable}\n" + KUBECTL_FIXTURE)
        fake.chmod(0o700)

    def calls_recorded(self):
        return [json.loads(row) for row in self.calls.read_text().splitlines()]

    def test_target_check_only_reads_pod_metadata_and_reports_context(self):
        result = self.run_backup("success", "--check-target", with_output=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")
        self.assertIn("context=audit-dev namespace=caritas pod/mariadb-0 uid=synthetic-pod-uid", result.stdout)
        self.assertFalse(self.output.exists())
        calls = self.calls_recorded()
        self.assertEqual([call[6] for call in calls], ["wait", "get"])
        for call in calls:
            self.assertEqual(call[:6], ["--kubeconfig", str(self.kubeconfig), "--context", "audit-dev", "--namespace", "caritas"])
            self.assertIn("pod/mariadb-0", call)
        self.assertIn('jsonpath={.metadata.uid} {.status.conditions[?(@.type=="Ready")].status}', calls[-1])

    def test_target_check_requires_readiness_and_pod_identity(self):
        for scenario in ("missing-pod", "missing-uid", "replaced-pod"):
            with self.subTest(scenario=scenario):
                self.calls.unlink(missing_ok=True)
                result = self.run_backup(scenario, "--check-target")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("uid=", result.stdout)
                self.assertFalse(self.output.exists())
                self.assertNotIn("exec", [call[6] for call in self.calls_recorded()])

    def test_lookup_failure_never_creates_an_output_directory(self):
        result = self.run_backup("missing-pod")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.calls_recorded()), 1)

    def test_failed_or_empty_discovery_never_creates_an_output_directory(self):
        for scenario in ["discovery-failure", "no-app-schemas"]:
            with self.subTest(scenario=scenario):
                result = self.run_backup(scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(self.output.exists())

    def test_failed_dump_hides_partial_and_earlier_successful_files(self):
        self.output.mkdir(mode=0o700)
        old = self.output / "existing-backup"
        old.mkdir()
        for scenario in ["partial-dump", "empty-dump"]:
            with self.subTest(scenario=scenario):
                result = self.run_backup(scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(list(self.output.iterdir()), [old])

    def test_timed_out_dump_is_reaped_and_partial_files_are_removed(self):
        for scenario in ("hanging-dump", "closed-output-dump"):
            with self.subTest(scenario=scenario):
                result = self.run_backup(scenario, "--dump-timeout-seconds", "1")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("timed out", result.stderr)
                self.assertEqual(list(self.output.iterdir()), [])
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(self.child.read_text()), 0)
                self.child.unlink()

    def test_sigterm_reaps_the_dump_and_removes_staging(self):
        command, env = self.backup_invocation("hanging-dump")
        with subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as process:
            try:
                child = self.wait_for_fixture(process)
                self.assertTrue(any(path.name.startswith(".mariadb-") for path in self.output.iterdir()))
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 1)
                self.assertEqual(list(self.output.iterdir()), [])
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)
                self.child.unlink()
            finally:
                if process.poll() is None:
                    process.kill()

    def test_two_complete_runs_are_private_and_pin_every_kubernetes_call(self):
        for _ in range(2):
            result = self.run_backup("success")
            self.assertEqual(result.returncode, 0, result.stderr)
        runs = list(self.output.iterdir())
        self.assertEqual(len(runs), 2)
        for directory in runs:
            self.assertTrue(directory.name.startswith("mariadb-"))
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(
                {file.name for file in directory.iterdir()},
                {"tenantservice.sql.gz", "userservice.sql.gz"},
            )
            for schema in ("tenantservice", "userservice"):
                file = directory / f"{schema}.sql.gz"
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                with gzip.open(file, "rt") as restored:
                    self.assertEqual(restored.read(), f"-- synthetic dump of {schema}\n")
        calls = self.calls_recorded()
        self.assertEqual(len(calls), 8)
        for args in calls:
            self.assertEqual(
                args[:6],
                ["--kubeconfig", str(self.kubeconfig), "--context", "audit-dev",
                 "--namespace", "caritas"],
            )
            self.assertIn("pod/mariadb-0", args)
            if "--databases" in " ".join(args):
                self.assertEqual(args[6:10], ["exec", "pod/mariadb-0", "--container", "mariadb"])


if __name__ == "__main__":
    unittest.main()
