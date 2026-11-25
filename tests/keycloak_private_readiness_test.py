"""Exercise the Dev Keycloak start command against a disposable MariaDB."""

import json
import subprocess
import time
import unittest
import uuid

from _keycloak_container import require_docker


KEYCLOAK_IMAGE = (
    "ghcr.io/openresilienceinitiative/oriso-keycloak@"
    "sha256:6b75ebfc476fe2acc261e18369507843f6a6d5d3fad914722ef6fe9039bbf1af"
)
MARIADB_IMAGE = "mariadb@sha256:9f56b2ac10055b68d07e3dcf2a17109fef77f1f08d504b1f028eb874457963b6"
DATABASE_CHECK = "Keycloak database connections async health check"
SOCKET_GET = r"""
exec 3<>"/dev/tcp/127.0.0.1/$1" || exit 1
printf 'GET %s HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n' "$2" >&3
cat <&3
"""


def docker(*args, check=True, timeout=30):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=timeout,
    )


def http_get(container, port, path):
    try:
        result = docker(
            "exec", container, "bash", "-c", SOCKET_GET, "probe", str(port), path,
            check=False, timeout=4,
        )
    except subprocess.TimeoutExpired:
        return None, {}
    if not result.stdout.startswith("HTTP/"):
        return None, {}
    header, _, body = result.stdout.partition("\n\n")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    return int(header.splitlines()[0].split()[1]), payload


class KeycloakPrivateReadinessTest(unittest.TestCase):
    def test_chart_start_waits_for_application_and_database_on_private_port(self):
        require_docker()
        prefix = f"oriso-keycloak-readiness-{uuid.uuid4().hex[:8]}"
        network, database, server = (f"{prefix}-{name}" for name in ("net", "db", "server"))

        docker("network", "create", "--internal", network)
        self.addCleanup(docker, "network", "rm", network, check=False)
        self.addCleanup(docker, "rm", "-f", database, check=False)
        docker(
            "run", "-d", "--name", database, "--network", network, "--network-alias", "mariadb",
            "--cpus", "0.75", "--memory", "640m",
            "-e", "MARIADB_ROOT_PASSWORD=fixture-root", "-e", "MARIADB_DATABASE=keycloak",
            "-e", "MARIADB_USER=keycloak", "-e", "MARIADB_PASSWORD=fixture-db", MARIADB_IMAGE, timeout=180,
        )
        deadline = time.monotonic() + 90
        while docker("exec", database, "healthcheck.sh", "--connect", "--innodb_initialized",
                     check=False).returncode:
            self.assertLess(time.monotonic(), deadline, "Disposable MariaDB did not initialise")
            time.sleep(0.5)

        self.addCleanup(docker, "rm", "-f", server, check=False)
        docker(
            "run", "-d", "--platform", "linux/amd64", "--name", server, "--network", network,
            "--cpus", "1", "--memory", "1500m", "-e", "JAVA_OPTS_APPEND=-XX:ActiveProcessorCount=1",
            "-e", "KC_DB=mariadb", "-e", "KC_DB_URL_HOST=mariadb", "-e", "KC_DB_URL_DATABASE=keycloak",
            "-e", "KC_DB_USERNAME=keycloak", "-e", "KC_DB_PASSWORD=fixture-db",
            "-e", "KC_HTTP_RELATIVE_PATH=/auth", "-e", "KC_HEALTH_ENABLED=true",
            "-e", "KC_METRICS_ENABLED=true", "-e", "KC_EVENT_METRICS_USER_ENABLED=true",
            "-e", "KC_HTTP_ENABLED=true", "-e", "KC_PROXY_HEADERS=xforwarded",
            "-e", "KC_HOSTNAME=http://localhost:8080/auth",
            "-e", "ORISO_APP_BASE_URL=http://localhost:8080",
            "-e", "EMAIL_BRANDING_NAME=Readiness Test",
            "-e", "EMAIL_LEGAL_ORGANISATION_NAME=Readiness Test Operator",
            "--entrypoint", "/opt/keycloak/bin/kc.sh", KEYCLOAK_IMAGE, "start", "--import-realm",
            timeout=180,
        )
        started = time.monotonic()
        first_seen = {}
        while True:
            status, readiness = http_get(server, 9000, "/auth/health/ready")
            first_seen.setdefault(status, round(time.monotonic() - started, 2))
            if status == 200:
                break
            if status == 503:
                self.assertEqual(readiness.get("status"), "DOWN")
            elif status is None:
                state = docker("inspect", "--format", "{{.State.Status}}", server).stdout.strip()
                self.assertEqual(state, "running", "Keycloak exited before answering readiness")
            else:
                self.fail(f"Unexpected private readiness response HTTP {status}")
            self.assertLess(time.monotonic() - started, 270, f"Readiness states: {first_seen}")
            time.sleep(0.2)

        self.assertEqual(readiness.get("status"), "UP")
        checks = {item["name"]: item.get("status") for item in readiness.get("checks", [])}
        self.assertEqual(checks.get(DATABASE_CHECK), "UP")
        for path in ("/auth/health/started", "/auth/health/live"):
            self.assertEqual(http_get(server, 9000, path)[0], 200)
        self.assertEqual(http_get(server, 8080, "/auth/health/ready")[0], 404)
        status, discovery = http_get(server, 8080, "/auth/realms/master/.well-known/openid-configuration")
        self.assertEqual(status, 200)
        self.assertEqual(discovery.get("issuer"), "http://localhost:8080/auth/realms/master")
        print(f"Keycloak readiness states and first observation in seconds: {first_seen}; database UP")


if __name__ == "__main__":
    unittest.main()
