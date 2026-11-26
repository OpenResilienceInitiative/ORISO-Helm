#!/usr/bin/env python3
"""Guard the opt-in PriorityClass for the MariaDB data store (Helm#576).

Without a priority, MariaDB competes with stateless pods at priority 0. The
chart can create one cluster-scoped PriorityClass and give it to MariaDB, so
the scheduler places MariaDB first (preempting stateless pods when needed)
and the kubelet evicts it last. Dev opts in; Production keeps the default.
"""

from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(*overlays, extra=()):
    command = [
        "helm", "template", "datastore-priority", str(ROOT),
        "--namespace", "render-test",
        "-f", str(ROOT / "values.yaml.default"),
        "-f", str(ROOT / "secrets.yaml.default"),
        "-f", str(ROOT / "tests/fixtures/values-render-domain.yaml"),
        "-f", str(ROOT / "tests/fixtures/render-required-secrets.yaml"),
        "--set-string", "global.secrets.redisdefaultPass=test-redis-password",
        "--set-string", "userService.smtpUser=smtp-validation-user",
        "--set-string", "userService.smtpPassword=smtp-validation-password",
        "--set-string", "tenantService.smtpPasswordEncryptionSecret=render-test-secret",
        "--set-string",
        "consultingTypeService.smtpPasswordEncryptionSecret="
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    ]
    for overlay in overlays:
        command.extend(["-f", str(ROOT / overlay)])
    command.extend(extra)
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def mariadb_pod(docs):
    sts = next(d for d in docs if d.get("kind") == "StatefulSet" and d["metadata"]["name"] == "mariadb")
    return sts["spec"]["template"]["spec"]


def priority_classes(docs):
    return [d for d in docs if d.get("kind") == "PriorityClass"]


class DatastorePriorityTest(unittest.TestCase):
    def test_default_renders_no_priority(self):
        docs = render()
        self.assertNotIn("priorityClassName", mariadb_pod(docs))
        self.assertEqual(priority_classes(docs), [])

    def test_production_overlay_keeps_default(self):
        docs = render("values-prod.yaml")
        self.assertNotIn("priorityClassName", mariadb_pod(docs))
        self.assertEqual(priority_classes(docs), [])

    def test_dev_overlay_creates_and_uses_the_class(self):
        docs = render("values-dev.yaml")
        self.assertEqual(mariadb_pod(docs)["priorityClassName"], "oriso-datastore")
        [pc] = priority_classes(docs)
        self.assertEqual(pc["metadata"]["name"], "oriso-datastore")
        self.assertEqual(pc["value"], 1000000)
        self.assertFalse(pc["globalDefault"])
        self.assertEqual(pc["preemptionPolicy"], "PreemptLowerPriority")
        # Stays below the Kubernetes system classes (2000000000+).
        self.assertLess(pc["value"], 1000000000)

    def test_existing_class_can_be_referenced_without_creating_it(self):
        docs = render(extra=("--set-string", "global.datastorePriorityClass.name=shared-datastore"))
        self.assertEqual(mariadb_pod(docs)["priorityClassName"], "shared-datastore")
        self.assertEqual(priority_classes(docs), [])

    def test_only_mariadb_gets_the_priority(self):
        docs = render("values-dev.yaml")
        named = [
            d["metadata"]["name"]
            for d in docs
            if d.get("kind") in {"Deployment", "StatefulSet"}
            and d["spec"]["template"]["spec"].get("priorityClassName") == "oriso-datastore"
        ]
        self.assertEqual(named, ["mariadb"])


if __name__ == "__main__":
    unittest.main()
