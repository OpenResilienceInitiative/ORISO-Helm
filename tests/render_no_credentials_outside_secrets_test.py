#!/usr/bin/env python3
"""Render guard: no credential value appears outside a Secret (ORISO-Helm#303).

A ConfigMap is not a secret store: anyone with namespace read access sees it in
plain `kubectl get -o yaml`, and it lands in cluster backups unencrypted. The
MariaDB init SQL was such a case: it rendered every service database password
into the ConfigMap `mariadb-configmap-initsql`.

Every credential value the chart takes is replaced by a unique canary. The chart
is rendered with the shipped defaults and with the Dev overlay, and no resource
other than a Secret may contain any canary.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import unittest

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_VALUES = (
    "values.yaml.default",
    "tests/fixtures/values-render-domain.yaml",
    "secrets.yaml.default",
    "tests/fixtures/render-required-secrets.yaml",
)
CREDENTIAL_KEY = re.compile(r"(?i)(password|pass|secret|token)$")


def credential_paths() -> list[str]:
    """Dotted paths of every credential value in the example and render secrets."""
    paths: set[str] = set()

    def walk(node: object, prefix: list[str]) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, prefix + [key])
            elif CREDENTIAL_KEY.search(key) and not isinstance(value, bool):
                paths.add(".".join(prefix + [key]))

    for name in ("secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        with open(os.path.join(CHART_DIR, name), encoding="utf-8") as handle:
            walk(yaml.safe_load(handle), [])
    return sorted(paths)


def canary(index: int, path: str) -> str:
    """A unique value that also passes the chart's own format checks."""
    if path.endswith("EncryptionSecret"):
        # Must be a 32-byte Base64 key.
        return base64.b64encode(f"render-canary-{index:03d}".ljust(32, "q").encode()).decode()
    # Long and printable so length checks such as the 32-character client secrets pass.
    return f"render-canary-{index:03d}-Zq7Lw2Vr8Nb6Yh3Sd1Fg5Jk9Mx4Pt0"


def render(*extra_values: str) -> tuple[list[dict], dict[str, str]]:
    canaries = {canary(i, path): path for i, path in enumerate(credential_paths())}
    args = ["helm", "template", "credential-guard", CHART_DIR]
    for name in (*BASE_VALUES, *extra_values):
        args += ["-f", os.path.join(CHART_DIR, name)]
    for value, path in canaries.items():
        args += ["--set-string", f"{path}={value}"]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    documents = [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]
    return documents, canaries


class NoCredentialsOutsideSecretsTest(unittest.TestCase):
    def test_guard_covers_the_database_passwords(self):
        paths = credential_paths()
        for path in (
            "global.secrets.keycloakDbPassword",
            "global.secrets.userServiceDbPassword",
            "global.secrets.liquibasePassword",
        ):
            self.assertIn(path, paths)

    def test_no_credential_outside_a_secret(self):
        for overlay in ((), ("values-dev.yaml",)):
            with self.subTest(overlay=overlay or "defaults"):
                documents, canaries = render(*overlay)
                exposed = {}
                for document in documents:
                    if document.get("kind") == "Secret":
                        continue
                    text = json.dumps(document)
                    leaked = sorted(path for value, path in canaries.items() if value in text)
                    if leaked:
                        exposed[f"{document['kind']}/{document['metadata']['name']}"] = leaked
                self.assertEqual(exposed, {})


if __name__ == "__main__":
    unittest.main()
