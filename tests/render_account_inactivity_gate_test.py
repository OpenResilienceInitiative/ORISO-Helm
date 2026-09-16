#!/usr/bin/env python3
"""Public Helm seam: lifecycle auth covers API ingresses without changing routes."""

from pathlib import Path
import shutil
import json
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
AUTH = "nginx.ingress.kubernetes.io/auth-url"
API_SERVICES = {
    "userservice",
    "tenantservice",
    "agencyservice",
    "consultingtypeservice",
}


def render(enabled=None, runtime=None):
    with tempfile.TemporaryDirectory(prefix="inactivity-ingress-") as tmp:
        chart = Path(tmp)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: inactivity-test\nversion: 0.0.0\n"
        )
        shutil.copy(ROOT / "values.yaml.default", chart / "values.yaml")
        for source in (ROOT / "templates").rglob("*"):
            if source.is_file() and (
                source.suffix == ".tpl"
                or "ingress" in source.name
                or source.name
                in {
                    "account-inactivity-auth-headers.yaml",
                    "userservice-configmap-env.yaml",
                    "userservice-deployment.yaml",
                }
            ):
                dest = chart / source.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, dest)
        extra = (
            []
            if runtime is None
            else [
                "--set",
                f"userService.accountInactivity.enabled={str(runtime[0]).lower()}",
                "--set",
                f"userService.accountInactivity.dryRun={str(runtime[1]).lower()}",
            ]
        )
        if enabled is not None:
            extra += [
                "--set",
                f"global.accountInactivity.accessGateEnabled={str(enabled).lower()}",
            ]
        result = subprocess.run(
            [
                "helm",
                "template",
                "inactivity-test",
                str(chart),
                "--namespace",
                "gate-test",
            ]
            + extra,
            text=True,
            capture_output=True,
            check=True,
        )
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


class InactivityIngressTest(unittest.TestCase):
    def test_enabled_gate_covers_api_paths_but_not_other_protocols(self):
        for doc in render(True):
            if doc.get("kind") != "Ingress":
                continue
            annotations = doc["metadata"].get("annotations", {})
            for rule in doc.get("spec", {}).get("rules", []):
                for path in rule.get("http", {}).get("paths", []):
                    service = path["backend"]["service"]["name"]
                    # Legal-text editor media is public static content.
                    requires_gate = service in API_SERVICES and not path[
                        "path"
                    ].startswith("/media/")
                    if requires_gate:
                        self.assertEqual(
                            annotations.get(AUTH),
                            "http://userservice.gate-test.svc.cluster.local:8080/users/account-inactivity/access",
                            (doc["metadata"]["name"], path["path"]),
                        )
                    else:
                        self.assertNotIn(
                            AUTH, annotations, (doc["metadata"]["name"], path["path"])
                        )

    def test_auth_subrequest_preserves_identity_and_does_not_cache_allow_decisions(
        self,
    ):
        docs = render(True)
        headers = next(
            (
                d
                for d in docs
                if d.get("kind") == "ConfigMap"
                and d["metadata"]["name"] == "account-inactivity-auth-headers"
            ),
            None,
        )
        self.assertIsNotNone(headers)
        self.assertEqual(
            headers["data"],
            {
                "Authorization": "$http_authorization",
                "Cookie": "$http_cookie",
                "X-Tenant-Id": "$http_x_tenant_id",
                "tenantId": "$http_tenantid",
            },
        )
        for d in docs:
            annotations = d.get("metadata", {}).get("annotations", {})
            if AUTH in annotations:
                self.assertEqual(
                    annotations["nginx.ingress.kubernetes.io/auth-method"], "GET"
                )
                self.assertEqual(
                    annotations["nginx.ingress.kubernetes.io/auth-proxy-set-headers"],
                    "gate-test/account-inactivity-auth-headers",
                )
                self.assertNotIn(
                    "nginx.ingress.kubernetes.io/auth-cache-key", annotations
                )

    def test_default_off_preserves_existing_routes_and_gate_only_moves_api_paths(self):
        defaults = yaml.safe_load((ROOT / "values.yaml.default").read_text())
        self.assertIs(
            defaults["global"].get("accountInactivity", {}).get("accessGateEnabled"),
            False,
        )
        expected = json.loads(
            (ROOT / "tests/fixtures/account-inactivity-routes.json").read_text()
        )
        for enabled in (None, False, True):
            docs = render(enabled)
            routes = []
            for d in docs:
                if d.get("kind") != "Ingress":
                    continue
                annotations = d["metadata"].get("annotations", {})
                if not enabled:
                    self.assertNotIn(AUTH, annotations)
                    self.assertNotEqual(
                        d["metadata"]["name"], "account-inactivity-api-ingress"
                    )
                for r in d["spec"].get("rules", []):
                    for p in r.get("http", {}).get("paths", []):
                        routes.append(
                            dict(
                                path=p["path"],
                                pathType=p["pathType"],
                                backend=p["backend"],
                                rewrite=annotations.get(
                                    "nginx.ingress.kubernetes.io/rewrite-target"
                                ),
                            )
                        )
            self.assertEqual(sorted(routes, key=lambda x: x["path"]), expected)

    def test_scheduler_defaults_are_safe_and_overrides_reach_userservice(self):
        for runtime, expected in (
            (None, ("false", "true")),
            ((True, False), ("true", "false")),
        ):
            docs = render(True, runtime)
            config = next(
                d
                for d in docs
                if d.get("kind") == "ConfigMap"
                and d["metadata"]["name"] == "userservice-configmap-env"
            )
            deployment = next(
                d
                for d in docs
                if d.get("kind") == "Deployment"
                and d["metadata"]["name"] == "userservice"
            )
            env = {
                e["name"]: e
                for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            for key, value in zip(
                ("ACCOUNT_INACTIVITY_ENABLED", "ACCOUNT_INACTIVITY_DRY_RUN"), expected
            ):
                self.assertEqual(config["data"].get(key), value)
                self.assertEqual(
                    env[key]["valueFrom"]["configMapKeyRef"],
                    {"name": "userservice-configmap-env", "key": key},
                )

    def test_lifecycle_flag_changes_roll_userservice_pods(self):
        pods = []
        for flags in ((False, True), (True, True), (True, False)):
            deployment = next(
                d
                for d in render(True, flags)
                if d.get("kind") == "Deployment"
                and d["metadata"]["name"] == "userservice"
            )
            pods.append(deployment["spec"]["template"])
        self.assertNotEqual(
            pods[0], pods[1], "Enabling scheduler must update the pod template"
        )
        self.assertNotEqual(
            pods[1], pods[2], "Turning off dry-run must update the pod template"
        )


if __name__ == "__main__":
    unittest.main()
