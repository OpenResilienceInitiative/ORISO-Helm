"""Exercise the installed entry point with synthetic per-service deploy commands."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "oriso-dev-deploy-all"
SERVICES = (
    "agencyservice",
    "consultingtypeservice",
    "userservice",
    "tenantservice",
    "livekit-token-service",
    "element-call",
    "admin",
    "frontend",
    "health-dashboard",
    "status-page",
    "storybook-admin",
    "storybook-frontend",
)
USER_SERVICE = """
# Prove this dependency group still runs alongside unrelated backends.
for _ in {1..100}; do
  if grep -q '^agencyservice complete$' "$EVENTS"; then break; fi
  sleep 0.03
done
grep -q '^agencyservice complete$' "$EVENTS" || exit 3
[[ "$SCENARIO" != provider_failure ]] || exit 4
printf '%s\\n' 'userservice complete' >> "$EVENTS"
"""
BEHAVIOR = {
    "agencyservice": """printf '%s\\n' 'agencyservice complete' >> "$EVENTS" """,
    "userservice": USER_SERVICE,
    "tenantservice": """
grep -q '^userservice complete$' "$EVENTS" || exit 5
sleep 0.05
printf '%s\\n' 'tenantservice complete' >> "$EVENTS"
[[ "$SCENARIO" != tenant_failure ]] || exit 6
""",
    "element-call": '[[ "$SCENARIO" != unrelated_failure ]] || exit 7',
}


def executable(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o700)


class DevNightlyDeployTest(unittest.TestCase):
    def test_orders_and_gates_the_tenantservice_rollout(self):
        scenarios = {
            "success": (0, True),
            "provider_failure": (1, False),
            "provider_missing": (1, False),
            "provider_timeout": (1, False),
            "provider_blocked": (0, False),
            "tenant_blocked": (0, False),
            "tenant_failure": (1, True),
            "unrelated_failure": (1, True),
            "dry_run": (0, False),
        }
        for scenario, (exit_status, starts_tenant) in scenarios.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                scripts = folder / "deploy-script"
                scripts.mkdir()
                events = folder / "events"
                blocklist = folder / "blocklist"
                blocklist.write_text({
                    "provider_blocked": "userservice\n",
                    "tenant_blocked": "tenantservice\n",
                }.get(scenario, ""))
                source = RUNNER.read_text()
                for line, path in {
                    "readonly SCRIPT_DIR=/root/deploy-script": scripts,
                    "readonly BLOCKLIST=/etc/oriso/dev-image-deploy.blocklist": blocklist,
                    "readonly LOG_DIR=/var/log/oriso": folder / "log",
                    "readonly LOCK_FILE=/var/lock/oriso-dev-image-deploy.lock": folder / "lock",
                }.items():
                    self.assertEqual(source.count(line), 1)
                    source = source.replace(line, line.split("=")[0] + "=" + shlex.quote(str(path)))
                runner = folder / RUNNER.name
                runner.write_text(source)
                for service in SERVICES:
                    if scenario == "provider_missing" and service == "userservice":
                        continue
                    event = f"""printf '%s\\n' '{service} start' >> "$EVENTS"\n"""
                    executable(scripts / f"deploy-{service}-dev.sh", event + BEHAVIOR.get(service, "exit 0"))
                executable(folder / "flock", "exit 0")
                executable(folder / "timeout", """
shift
if [[ "$SCENARIO" == provider_timeout && "$1" == */deploy-userservice-dev.sh ]]; then exit 124; fi
exec "$@"
""")
                args = ["bash", str(runner)]
                if scenario == "dry_run":
                    args.append("--dry-run")
                result = subprocess.run(
                    args,
                    env={
                        **os.environ,
                        "PATH": f"{folder}:{os.environ['PATH']}",
                        "SCENARIO": scenario,
                        "EVENTS": str(events),
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                output = result.stdout + result.stderr
                recorded = events.read_text().splitlines() if events.exists() else []
                self.assertEqual(result.returncode, exit_status, output)
                self.assertEqual("tenantservice start" in recorded, starts_tenant, output)
                if starts_tenant:
                    self.assertLess(recorded.index("userservice complete"), recorded.index("tenantservice start"))
                    self.assertLess(recorded.index("tenantservice complete"), recorded.index("admin start"))
                if scenario == "dry_run":
                    self.assertEqual(recorded, [])
                    self.assertLess(output.index("DRY RUN userservice"), output.index("DRY RUN tenantservice"))
                else:
                    self.assertIn("agencyservice complete", recorded)
                    self.assertIn("admin start", recorded)
                if scenario == "provider_blocked":
                    self.assertNotIn("userservice start", recorded)
                    self.assertIn("UserService was blocked", output)
                if scenario == "provider_timeout":
                    self.assertIn("exit=124", output)
                    self.assertIn("UserService rollout did not succeed", output)
