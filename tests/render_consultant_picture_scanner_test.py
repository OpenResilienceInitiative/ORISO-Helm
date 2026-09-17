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

    def test_early_ready_daemon_failure_is_checked_without_initial_blind_window(self):
        probe = self.scanner(self.render({"enabled": True}))["livenessProbe"]
        # health.sh owns refresh/loading grace. Kubernetes must start calling it early,
        # so the first successful PING can end grace and later failures are observable.
        first_recovery_bound = (probe.get("initialDelaySeconds", 0)
                                + probe["periodSeconds"] * probe["failureThreshold"]
                                + probe["timeoutSeconds"])
        self.assertLessEqual(first_recovery_bound, 120,
                             "early-ready daemon failures must not be blind for 30 minutes")

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
                "freshclam": '[ -e "$MARKER" ] || echo missing-marker >> "$EVENTS"; echo update >> "$EVENTS"; if [ ! -e "$STATE" ]; then touch "$STATE"; exit 1; fi; echo fresh >> "$EVENTS"',
                "sleep": 'echo wait >> "$EVENTS"',
                "init-unprivileged": '[ ! -e "$MARKER" ] || echo stale-marker >> "$EVENTS"; echo daemon >> "$EVENTS"',
            }.items():
                command = folder / name
                command.write_text("#!/bin/sh\n" + body + "\n")
                command.chmod(0o755)
            script_path = folder / "start.sh"
            # Substitute only the fixed absolute official entrypoint for the test stub.
            script_path.write_text(script.replace("/init-unprivileged", str(folder / "init-unprivileged")).replace("/tmp/picture-scanner-refreshing", str(folder / "refreshing")).replace("/tmp/picture-scanner-startup-deadline", str(folder / "deadline")))
            import os
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ["PATH"], EVENTS=str(events), STATE=str(folder / "state"), MARKER=str(folder / "refreshing"))
            result = subprocess.run(["/bin/sh", str(script_path)], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events.read_text().splitlines(), ["update", "wait", "update", "fresh", "daemon"])

    def test_refresh_wait_is_live_but_failed_daemon_is_not(self):
        """A prolonged update outage must not reset progress through liveness restarts."""
        docs = self.render({"enabled": True})
        config = next(d for d in docs if d["kind"] == "ConfigMap")
        health = config["data"].get("health.sh")
        self.assertIsNotNone(health, "liveness must distinguish active refresh from a failed daemon")
        self.assertEqual(self.scanner(docs)["livenessProbe"]["exec"]["command"],
                         ["/bin/sh", "/picture-scanner/health.sh"])
        with tempfile.TemporaryDirectory(prefix="scanner-health-") as directory:
            folder = pathlib.Path(directory)
            marker = folder / "refreshing"
            ping = folder / "clamdscan"
            ping.write_text("#!/bin/sh\nexit 1\n")
            ping.chmod(0o755)
            script = folder / "health.sh"
            script.write_text(health.replace("/tmp/picture-scanner-refreshing", str(marker)))
            import os
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ["PATH"])
            marker.touch()
            for _ in range(6):
                self.assertEqual(subprocess.run(["/bin/sh", str(script)], env=env).returncode, 0)
            marker.unlink()
            self.assertNotEqual(subprocess.run(["/bin/sh", str(script)], env=env).returncode, 0)
            ping.write_text("#!/bin/sh\nexit 0\n")
            self.assertEqual(subprocess.run(["/bin/sh", str(script)], env=env).returncode, 0)

    def test_late_refresh_has_bounded_loading_grace_and_ends_on_first_ping(self):
        """Grace starts after refresh, expires, and never hides a later daemon failure."""
        docs = self.render({"enabled": True})
        config = next(d for d in docs if d["kind"] == "ConfigMap")
        with tempfile.TemporaryDirectory(prefix="scanner-loading-") as directory:
            folder = pathlib.Path(directory)
            marker = folder / "refreshing"
            deadline = folder / "deadline"
            now = folder / "now"
            ping_status = folder / "ping-status"
            for name, body in {
                "date": 'cat "$NOW_FILE"',
                "clamdscan": 'exit "$(cat "$PING_STATUS")"',
                "timeout": 'shift; exec "$@"',
            }.items():
                binary = folder / name
                binary.write_text("#!/bin/sh\n" + body + "\n")
                binary.chmod(0o755)
            script = folder / "health.sh"
            script.write_text(config["data"]["health.sh"].replace(
                "/tmp/picture-scanner-refreshing", str(marker)).replace(
                "/tmp/picture-scanner-startup-deadline", str(deadline)))
            import os
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ["PATH"],
                       NOW_FILE=str(now), PING_STATUS=str(ping_status))
            def probe():
                return subprocess.run(["/bin/sh", str(script)], env=env).returncode
            deadline.write_text("1900\n")
            now.write_text("100\n")
            ping_status.write_text("1\n")
            self.assertEqual(probe(), 0, "late refresh must allow the daemon time to load")
            ping_status.write_text("124\n")
            self.assertEqual(probe(), 0, "bounded PING timeout must not cancel loading grace")
            now.write_text("1900\n")
            self.assertNotEqual(probe(), 0, "a daemon that never loads must eventually fail")
            now.write_text("200\n")
            ping_status.write_text("0\n")
            self.assertEqual(probe(), 0)
            self.assertFalse(deadline.exists(), "first successful PING ends loading grace")
            ping_status.write_text("1\n")
            self.assertNotEqual(probe(), 0, "a later crash must not reenter loading grace")

    def test_string_boolean_refuses_render(self):
        self.assertIn("boolean", self.render({"enabled": "false"}, expect_error=True))


if __name__ == "__main__":
    unittest.main()
