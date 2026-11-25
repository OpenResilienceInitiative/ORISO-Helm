"""Exercise the host backup command against a disposable kubectl fixture."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
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
import random
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
if scenario in {"hanging-dump", "closed-output-dump", "bulk-dump"}:
    if scenario == "closed-output-dump":
        os.close(sys.stdout.fileno())
    pid_file = Path(os.environ["BACKUP_TEST_CHILD"])
    pending_pid = pid_file.with_suffix(".tmp")
    pending_pid.write_text(str(os.getpid()))
    pending_pid.replace(pid_file)
    if scenario == "bulk-dump":
        fixture = random.Random(912)
        for _ in range(64):
            sys.stdout.buffer.write(fixture.randbytes(65536))
        sys.stdout.buffer.flush()
    else:
        time.sleep(30)
sys.exit(1 if scenario == "partial-dump" and schema == "userservice" else 0)
"""


AGE_FIXTURE = """
import os
from pathlib import Path
import sys
import time

pid_file = Path(os.environ["BACKUP_TEST_AGE_CHILD"])
pending = pid_file.with_suffix(".tmp")
pending.write_text(str(os.getpid()))
pending.replace(pid_file)
scenario = os.environ["BACKUP_TEST_AGE_SCENARIO"]
if scenario == "fail":
    sys.stdin.buffer.read(32)
    sys.exit(12)
if scenario == "empty":
    sys.stdin.buffer.read()
    sys.exit(0)
if scenario == "hang-after-eof":
    sys.stdin.buffer.read()
time.sleep(30)
"""


class MariaDbBackupTest(unittest.TestCase):
    def backup_invocation(self, scenario, *options, with_output=True,
                          context="audit-dev", namespace="caritas"):
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_TEST_CALLS": str(self.calls),
            "BACKUP_TEST_SCENARIO": scenario,
            "BACKUP_TEST_CHILD": str(self.child),
            "BACKUP_TEST_AGE_CHILD": str(self.age_child),
            "BACKUP_TEST_AGE_SCENARIO": self.age_scenario,
        }
        command = [sys.executable, str(SCRIPT), str(self.kubeconfig), context, namespace]
        if with_output:
            command.append(str(self.output))
        return command + list(options), env

    def run_backup(self, scenario, *options, with_output=True):
        command, env = self.backup_invocation(scenario, *options, with_output=with_output)
        return subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)

    def stop_fixture_if_running(self):
        for pid_file in (self.child, self.age_child):
            if pid_file.is_file():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def wait_for_fixture(self, process, pid_file=None):
        pid_file = self.child if pid_file is None else pid_file
        for _ in range(150):
            if pid_file.is_file():
                return int(pid_file.read_text())
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
        self.age_child = root / "age-child.pid"
        self.age_scenario = ""
        self.recipients = root / "recipients.txt"
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

    def test_backup_and_target_check_reject_empty_or_blank_target_before_kubectl(self):
        for options in ((), ("--check-target",)):
            for context, namespace in (("", "caritas"), ("  ", "caritas"),
                                       ("audit-dev", ""), ("audit-dev", "  ")):
                with self.subTest(context=context, namespace=namespace, options=options):
                    command, env = self.backup_invocation(
                        "success", *options, context=context, namespace=namespace,
                    )
                    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("context and namespace must be non-blank", result.stderr)
                    self.assertFalse(self.calls.exists())
                    self.assertFalse(self.output.exists())

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

    def synthetic_age(self, scenario):
        self.age_scenario = scenario
        self.recipients.write_text("age1" + "q" * 58 + "\n")
        fake = self.bin_dir / "age"
        fake.write_text(f"#!{sys.executable}\n" + AGE_FIXTURE)
        fake.chmod(0o700)

    def encrypted_backup(self, scenario, *options):
        return self.run_backup(scenario, "--age-recipients-file", str(self.recipients), *options)

    def assert_reaped(self, pid_file):
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        pid_file.unlink()

    def publish_encrypted_fixture(self):
        identities = []
        recipients = []
        for index in range(2):
            identity = self.recipients.with_name(f"synthetic-identity-{index}")
            subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True, timeout=5)
            recipients.append(subprocess.check_output(["age-keygen", "-y", str(identity)], text=True, timeout=5))
            identities.append(identity)
        self.recipients.write_text("# Synthetic test recipients\n\n" + "".join(recipients))

        result = self.encrypted_backup("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        directory, = self.output.iterdir()
        return directory, identities, result.stdout

    def verify_encrypted_receipt(self, directory, digest):
        _, env = self.backup_invocation("success", with_output=False)
        command = [
            sys.executable, str(SCRIPT), "--verify-encrypted-receipt", str(directory),
            "--receipt-sha256", digest,
        ]
        return subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "requires age and age-keygen")
    def test_encrypted_backup_publishes_receipt_and_decrypts_for_both_recipients(self):
        directory, identities, stdout = self.publish_encrypted_fixture()
        archive_names = {"tenantservice.sql.gz.age", "userservice.sql.gz.age"}
        receipt = directory / "backup-receipt.json"
        self.assertTrue(directory.name.startswith("mariadb-"))
        self.assertEqual({file.name for file in directory.iterdir()}, archive_names | {receipt.name})
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        receipt_bytes = receipt.read_bytes()
        self.assertEqual(json.loads(receipt_bytes), {
            "version": 1,
            "run": directory.name,
            "archives": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                         for name in archive_names},
        })
        digest = hashlib.sha256(receipt_bytes).hexdigest()
        self.assertIn(f"Encrypted backup receipt SHA-256: {digest}", stdout)
        self.assertEqual(stdout.splitlines()[-1], f"Completed local MariaDB backup: {directory}")
        calls = self.calls_recorded()
        verification = self.verify_encrypted_receipt(directory, digest)
        self.assertEqual(verification.returncode, 0, verification.stderr)
        self.assertEqual(self.calls_recorded(), calls)
        for schema in ("tenantservice", "userservice"):
            artifact = directory / f"{schema}.sql.gz.age"
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            self.assertTrue(artifact.read_bytes().startswith(b"age-encryption.org/v1\n"))
            for identity in identities:
                compressed = subprocess.check_output(
                    ["age", "--decrypt", "-i", str(identity), str(artifact)], timeout=5,
                )
                with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as restored:
                    self.assertEqual(restored.read(), f"-- synthetic dump of {schema}\n".encode())

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "requires age and age-keygen")
    def test_encrypted_receipt_rejects_archive_and_receipt_changes(self):
        directory, _, _ = self.publish_encrypted_fixture()
        receipt_bytes = (directory / "backup-receipt.json").read_bytes()
        digest = hashlib.sha256(receipt_bytes).hexdigest()
        calls = self.calls_recorded()

        for scenario in ("missing", "extra", "swapped", "symlink", "altered receipt"):
            with self.subTest(scenario=scenario):
                copy = self.output.parent / scenario / directory.name
                shutil.copytree(directory, copy)
                tenant = copy / "tenantservice.sql.gz.age"
                user = copy / "userservice.sql.gz.age"
                if scenario == "missing":
                    user.unlink()
                elif scenario == "extra":
                    shutil.copyfile(user, copy / "reporting.sql.gz.age")
                elif scenario == "swapped":
                    tenant_ciphertext, user_ciphertext = tenant.read_bytes(), user.read_bytes()
                    tenant.write_bytes(user_ciphertext)
                    user.write_bytes(tenant_ciphertext)
                elif scenario == "symlink":
                    user.unlink()
                    user.symlink_to(directory / user.name)
                elif scenario == "altered receipt":
                    (copy / "backup-receipt.json").write_bytes(receipt_bytes + b"\n")

                result = self.verify_encrypted_receipt(copy, digest)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.calls_recorded(), calls)

        for untrusted_digest in ("not-a-digest", "0" * 64):
            with self.subTest(digest=untrusted_digest):
                result = self.verify_encrypted_receipt(directory, untrusted_digest)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.calls_recorded(), calls)

    def test_invalid_recipient_input_is_rejected_before_kubectl(self):
        for invalid in ("# Empty list\n", "not-a-public-recipient\n", "age1plugin1sample\n"):
            with self.subTest(invalid=invalid):
                self.recipients.write_text(invalid)
                result = self.encrypted_backup("success")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("age recipients", result.stderr)
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.output.exists())

    def test_target_check_preserves_metadata_only_mode_with_an_unused_recipients_file(self):
        result = self.run_backup(
            "success", "--check-target", "--age-recipients-file", str(self.recipients), with_output=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[6] for call in self.calls_recorded()], ["wait", "get"])
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "requires age and age-keygen")
    def test_encrypted_failed_or_empty_dump_publishes_nothing(self):
        identity = self.recipients.with_name("synthetic-identity")
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True, timeout=5)
        self.recipients.write_text(subprocess.check_output(["age-keygen", "-y", str(identity)], text=True, timeout=5))
        self.output.mkdir(mode=0o700)
        old = self.output / "existing-backup"
        old.mkdir()
        for scenario, error in (("partial-dump", "exit status"), ("empty-dump", "was empty")):
            with self.subTest(scenario=scenario):
                result = self.encrypted_backup(scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(error, result.stderr)
                self.assertEqual(list(self.output.iterdir()), [old])

    def test_failed_or_empty_age_output_publishes_nothing(self):
        for age_scenario, dump_scenario, error in (
            ("fail", "bulk-dump", "age exited"),
            ("empty", "success", "age produced no ciphertext"),
        ):
            with self.subTest(age_scenario=age_scenario):
                self.synthetic_age(age_scenario)
                result = self.encrypted_backup(dump_scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(error, result.stderr)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assert_reaped(self.age_child)
                if self.child.exists():
                    self.assert_reaped(self.child)

    def test_nonreading_age_cannot_block_the_schema_deadline(self):
        self.synthetic_age("stall")
        result = self.encrypted_backup("bulk-dump", "--dump-timeout-seconds", "1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assert_reaped(self.child)
        self.assert_reaped(self.age_child)

    def test_age_exit_after_eof_shares_the_schema_deadline(self):
        self.synthetic_age("hang-after-eof")
        result = self.encrypted_backup("success", "--dump-timeout-seconds", "1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assert_reaped(self.age_child)

    def test_sigterm_reaps_both_children_and_removes_ciphertext_staging(self):
        self.synthetic_age("stall")
        command, env = self.backup_invocation("bulk-dump", "--age-recipients-file", str(self.recipients))
        with subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as process:
            try:
                self.wait_for_fixture(process)
                self.wait_for_fixture(process, self.age_child)
                stage, = self.output.iterdir()
                self.assertTrue(stage.name.startswith(".mariadb-"))
                self.assertEqual({file.name for file in stage.iterdir()},
                                 {"tenantservice.sql.gz.age"})
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 1)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assert_reaped(self.child)
                self.assert_reaped(self.age_child)
            finally:
                if process.poll() is None:
                    process.kill()


if __name__ == "__main__":
    unittest.main()
