"""A UserService rollout must never leave the platform without a UserService.

With one replica and no probe, the new pod counted as ready seconds after its
container started, long before Spring Boot served anything, and the rollout
stopped the old pod. Every /service/* request and the MatrixRTC call-policy
check failed until the JVM was up; on dev.oriso.org (17 Nov 2026) a call start
in that window answered HTTP 503. Pure source checks, no helm.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "templates" / "userservice" / "userservice-deployment.yaml"
SERVICE = ROOT / "templates" / "userservice" / "userservice-service.yaml"


def probe(kind):
    match = re.search(
        rf"^\s*{kind}:\n\s*httpGet:\n\s*path: (\S+)\n\s*port: (\d+)$",
        DEPLOYMENT.read_text(),
        re.M,
    )
    return match.groups() if match else None


class UserServiceRolloutProbes(unittest.TestCase):
    def test_old_pod_stays_until_the_new_one_is_ready(self):
        text = DEPLOYMENT.read_text()
        self.assertRegex(text, r"rollingUpdate:\n\s*maxSurge: 1\n\s*maxUnavailable: 0")

    def test_readiness_waits_for_spring_boot(self):
        self.assertEqual(("/actuator/health/readiness", "8082"), probe("readinessProbe"))
        self.assertEqual(("/actuator/health/liveness", "8082"), probe("startupProbe"))

    def test_probes_use_the_port_the_service_targets(self):
        self.assertIn("targetPort: 8082", SERVICE.read_text())


if __name__ == "__main__":
    unittest.main()
