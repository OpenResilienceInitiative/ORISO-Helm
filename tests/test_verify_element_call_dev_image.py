"""Exercise the Element Call deploy preflight through its command-line entry point."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_element_call_dev_image.py"
DEPLOY = ROOT / "scripts" / "deploy_element_call_dev_image.py"
REPOSITORY = "ghcr.io/openresilienceinitiative/element-call"
IMAGE = REPOSITORY + "@sha256:" + "a" * 64
COMMIT = "b" * 40


class ElementCallDevImageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.call_arguments = directory / "arguments"
        self.gh = directory / "gh"
        self.gh.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$GH_TEST_ARGUMENTS\"\n"
            "printf '%s\\n' \"$GH_TEST_RESPONSE\"\n"
            "exit \"$GH_TEST_STATUS\"\n"
        )
        self.gh.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": str(directory),
            "GH_TEST_ARGUMENTS": str(self.call_arguments),
            "GH_TEST_STATUS": "0",
            "GH_TEST_RESPONSE": self.verification_output(REPOSITORY, "a" * 64),
        }

    def prepare_deployment(self, image=IMAGE, commit=COMMIT):
        directory = self.gh.parent
        request = directory / "approved-build.json"
        request.write_text(json.dumps({"image": image, "source_commit": commit}))
        self.kubeconfig = directory / "kubeconfig"
        self.kubeconfig.touch()
        self.deploy_calls = directory / "kubectl-arguments"
        kubectl = directory / "kubectl"
        kubectl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$KUBECTL_TEST_ARGUMENTS\"\n"
            "case \" $* \" in *' get '*) printf '%s' \"$KUBECTL_TEST_IMAGE\" ;; esac\n"
        )
        kubectl.chmod(0o700)
        self.env.update({
            "KUBECTL_TEST_ARGUMENTS": str(self.deploy_calls),
            "KUBECTL_TEST_IMAGE": IMAGE,
        })
        return [
            sys.executable, str(DEPLOY), "--approved-build", str(request),
            "--kubeconfig", str(self.kubeconfig), "--context", "dev-context",
        ]

    def run_deployment(self, command):
        return subprocess.run(
            command, env=self.env, capture_output=True, text=True, check=False,
        )

    def test_guarded_deploy_passes_the_verified_pin_and_explicit_target(self):
        result = self.run_deployment(self.prepare_deployment())
        self.assertEqual(result.returncode, 0, result.stderr)
        target = f"--kubeconfig {self.kubeconfig} --context dev-context --namespace caritas"
        self.assertEqual(
            self.deploy_calls.read_text().splitlines(),
            [
                f"{target} set image deployment/element-call element-call={IMAGE}",
                f"{target} rollout status deployment/element-call --timeout=300s",
                f'{target} get deployment/element-call -o '
                'jsonpath={.spec.template.spec.containers[?(@.name=="element-call")].image}',
            ],
        )
        self.assertEqual(result.stdout, IMAGE + "\n")

    def test_verify_only_leaves_kubernetes_untouched(self):
        result = self.run_deployment([*self.prepare_deployment(), "--verify-only"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")
        self.assertFalse(self.deploy_calls.exists())

    def test_attestation_failure_stops_before_any_kubernetes_command(self):
        command = self.prepare_deployment()
        self.env["GH_TEST_STATUS"] = "1"
        result = self.run_deployment(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance verification failed", result.stderr)
        self.assertFalse(self.deploy_calls.exists())

    def test_mutable_image_stops_before_any_kubernetes_command(self):
        result = self.run_deployment(self.prepare_deployment(image=REPOSITORY + ":dev"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable image", result.stderr)
        self.assertFalse(self.deploy_calls.exists())

    def test_guarded_deploy_fails_if_the_rollout_uses_a_different_pin(self):
        command = self.prepare_deployment()
        self.env["KUBECTL_TEST_IMAGE"] = REPOSITORY + "@sha256:" + "c" * 64
        result = self.run_deployment(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the verified pin", result.stderr)

    @staticmethod
    def verification_output(name, digest):
        return json.dumps([{"verificationResult": {"statement": {"subject": [
            {"name": name, "digest": {"sha256": digest}}
        ]}}}])

    def run_preflight(self, image=IMAGE, commit=COMMIT):
        return subprocess.run(
            [sys.executable, str(SCRIPT), image, commit],
            env=self.env, capture_output=True, text=True, check=False,
        )

    def test_verified_image_is_the_only_stdout_value(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")
        self.assertEqual(
            self.call_arguments.read_text().splitlines(),
            [
                "attestation", "verify", f"oci://{IMAGE}",
                "--hostname", "github.com",
                "--repo", "OpenResilienceInitiative/ORISO-ElementCall",
                "--cert-identity",
                "https://github.com/OpenResilienceInitiative/ORISO-ElementCall/"
                ".github/workflows/ci-main.yml@refs/heads/dev",
                "--source-ref", "refs/heads/dev",
                "--source-digest", COMMIT,
                "--predicate-type", "https://slsa.dev/provenance/v1",
                "--deny-self-hosted-runners",
                "--format", "json",
            ],
        )

    def test_mutable_or_unexpected_image_is_rejected_before_verification(self):
        for image in (
            REPOSITORY + ":dev",
            REPOSITORY + ":dev@sha256:" + "a" * 64,
            "example.test/element-call@sha256:" + "a" * 64,
            REPOSITORY + "@sha256:" + "0" * 64,
        ):
            with self.subTest(image=image):
                result = self.run_preflight(image=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("immutable image", result.stderr)
        self.assertFalse(self.call_arguments.exists())

    def test_commit_must_be_complete_and_nonzero(self):
        for revision in ("dev", COMMIT[:8], "0" * 40):
            with self.subTest(revision=revision):
                result = self.run_preflight(commit=revision)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("source commit", result.stderr)
        self.assertFalse(self.call_arguments.exists())

    def test_failed_verification_cannot_return_a_deployment_pin(self):
        self.env["GH_TEST_STATUS"] = "1"
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("provenance verification failed", result.stderr)
        self.assertTrue(self.call_arguments.exists())

    def test_verification_of_a_different_subject_cannot_return_a_pin(self):
        for response in (
            self.verification_output("example.test/element-call", "a" * 64),
            self.verification_output(REPOSITORY, "c" * 64),
            "[]",
            "invalid JSON",
        ):
            with self.subTest(response=response):
                self.env["GH_TEST_RESPONSE"] = response
                result = self.run_preflight()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("does not name the expected Element Call image", result.stderr)

    def test_missing_github_cli_fails_closed(self):
        self.gh.unlink()
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("GitHub CLI", result.stderr)


if __name__ == "__main__":
    unittest.main()
