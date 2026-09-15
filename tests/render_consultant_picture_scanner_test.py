#!/usr/bin/env python3
"""Render the opt-in private picture scanner without touching an environment."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PictureScannerRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="picture-scanner-render-")
        cls.chart = pathlib.Path(cls.temp.name)
        (cls.chart / "templates/userservice").mkdir(parents=True)
        (cls.chart / "Chart.yaml").write_text("apiVersion: v2\nname: picture-test\nversion: 0.0.0\n")
        shutil.copy(ROOT / "values.yaml.default", cls.chart / "values.yaml")
        shutil.copy(ROOT / "templates/_helpers.tpl", cls.chart / "templates/_helpers.tpl")
        for source in (ROOT / "templates/userservice").glob("*.yaml"):
            if source.name not in {"userservice-deployment.yaml", "userservice-service.yaml", "userservice-picture-scanner.yaml"}:
                continue
            shutil.copy(source, cls.chart / "templates/userservice" / source.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def render(self, scanner=None, expect_error=False):
        values = self.chart / "test-values.yaml"
        values.write_text(yaml.safe_dump({"userService": {"pictureScanner": scanner}} if scanner is not None else {}))
        result = subprocess.run(["helm", "template", "picture-test", str(self.chart), "-f", str(values)], capture_output=True, text=True)
        if expect_error:
            self.assertNotEqual(result.returncode, 0, "unsafe configuration must refuse to render")
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def pod(self, docs):
        return next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "userservice")["spec"]["template"]

    def scanner(self, docs):
        matches = [c for c in self.pod(docs)["spec"]["containers"] if c["name"] == "consultant-picture-scanner"]
        self.assertEqual(len(matches), 1, "opt-in must provide exactly one local scanner")
        return matches[0]

    def test_disabled_preserves_existing_service(self):
        docs = self.render()
        pod = self.pod(docs)
        self.assertEqual([c["name"] for c in pod["spec"]["containers"]], ["userservice"])
        self.assertFalse(any("picture-scanner" in d["metadata"]["name"] for d in docs))
        self.assertFalse(any(e["name"].startswith("CONSULTANT_PICTURE_SCANNER") for e in pod["spec"]["containers"][0]["env"]))

    def test_enabled_wires_local_scanner_and_bounded_memory(self):
        docs = self.render({"enabled": True})
        scanner = self.scanner(docs)
        self.assertRegex(scanner["image"], r"@sha256:[a-f0-9]{64}$")
        self.assertEqual(scanner["command"], ["/bin/sh", "/picture-scanner/start.sh"])
        self.assertEqual(scanner["securityContext"]["runAsUser"], 100)
        self.assertFalse(scanner["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(scanner["resources"]["limits"]["memory"], "4Gi")
        self.assertEqual(scanner["resources"]["requests"]["memory"], "3Gi")
        pod = self.pod(docs)
        volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
        self.assertEqual(volumes["picture-scanner-tmp"]["emptyDir"], {"medium": "Memory", "sizeLimit": "64Mi"})
        env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
        self.assertEqual(env["CONSULTANT_PICTURE_SCANNER_ENABLED"], "true")
        self.assertEqual(env["CONSULTANT_PICTURE_SCANNER_PORT"], "3310")
        self.assertEqual(env["CONSULTANT_PICTURE_SCANNER_TIMEOUT_MILLIS"], "5000")
        self.assertIn("checksum/picture-scanner", pod["metadata"]["annotations"])

    def test_no_public_scanner_port_or_readiness_coupling(self):
        docs = self.render({"enabled": True})
        scanner = self.scanner(docs)
        self.assertNotIn("ports", scanner)
        self.assertNotIn("readinessProbe", scanner)
        self.assertNotIn("startupProbe", scanner)
        self.assertIn("exec", scanner["livenessProbe"])
        for doc in docs:
            if doc["kind"] == "Service":
                self.assertFalse(any(str(p.get("port")) == "3310" or str(p.get("targetPort")) == "3310" for p in doc["spec"]["ports"]))

    def test_scans_entire_accepted_body_without_retaining_samples(self):
        docs = self.render({"enabled": True})
        config = next((d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "userservice-picture-scanner"), None)
        self.assertIsNotNone(config, "scanner configuration must be rendered")
        settings = dict(line.split(None, 1) for line in config["data"]["clamd.conf"].splitlines() if line and not line.startswith("#"))
        for key, value in {"TCPAddr": "127.0.0.1", "TCPSocket": "3310", "StreamMaxLength": "5M", "MaxFileSize": "6M", "MaxScanSize": "20M", "AlertExceedsMax": "yes", "TemporaryDirectory": "/tmp", "LeaveTemporaryFiles": "no", "LogClean": "no"}.items():
            self.assertEqual(settings[key], value)

    def test_floating_image_refuses_render(self):
        self.assertIn("digest", self.render({"enabled": True, "image": "clamav/clamav:latest"}, expect_error=True))

    def test_unbounded_memory_refuses_render(self):
        self.assertIn("memory", self.render({"enabled": True, "resources": None}, expect_error=True))

    def test_zero_and_invalid_memory_refuses_render(self):
        for amount in ["0", "0Gi", "0.0Gi", "-1Gi", "nonsense"]:
            with self.subTest(amount=amount):
                self.assertIn("memory", self.render({"enabled": True, "resources": {"requests": {"memory": amount}, "limits": {"memory": amount}}}, expect_error=True))

    def test_reload_keeps_command_processing_available(self):
        docs = self.render({"enabled": True})
        config = next(d for d in docs if d["kind"] == "ConfigMap")
        self.assertIn("ConcurrentDatabaseReload yes", config["data"]["clamd.conf"])

    def test_startup_refresh_completes_before_daemon_starts(self):
        docs = self.render({"enabled": True})
        config = next(d for d in docs if d["kind"] == "ConfigMap")
        script = config["data"].get("start.sh")
        self.assertIsNotNone(script, "startup must refresh signatures before starting clamd")
        # Execute the real rendered startup script with deterministic local commands.
        # First update fails, next succeeds; the daemon must not start in between.
        with tempfile.TemporaryDirectory(prefix="scanner-startup-") as directory:
            folder = pathlib.Path(directory)
            events = folder / "events"
            for name, body in {
                "freshclam": 'echo update >> "$EVENTS"; if [ ! -e "$STATE" ]; then touch "$STATE"; exit 1; fi; echo fresh >> "$EVENTS"',
                "sleep": 'echo wait >> "$EVENTS"',
                "init-unprivileged": 'echo daemon >> "$EVENTS"',
            }.items():
                command = folder / name
                command.write_text("#!/bin/sh\n" + body + "\n")
                command.chmod(0o755)
            script_path = folder / "start.sh"
            # Substitute only the fixed absolute official entrypoint for the test stub.
            script_path.write_text(script.replace("/init-unprivileged", str(folder / "init-unprivileged")))
            import os
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ["PATH"], EVENTS=str(events), STATE=str(folder / "state"))
            result = subprocess.run(["/bin/sh", str(script_path)], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events.read_text().splitlines(), ["update", "wait", "update", "fresh", "daemon"])

    def test_string_boolean_refuses_render(self):
        self.assertIn("boolean", self.render({"enabled": "false"}, expect_error=True))


if __name__ == "__main__":
    unittest.main()
