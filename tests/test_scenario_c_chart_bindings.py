"""Render the real chart: five public bindings and least-needed secret references."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTORS = ("SETUP", "ACCOUNT_CREATE", "ACCOUNT_ADMIN", "MAIL", "RUNTIME")
AUDIENCES = {
    "SETUP": {"tenantservice", "agencyservice", "consultingtypeservice", "userservice"},
    "ACCOUNT_CREATE": {"userservice", "oriso-account-lifecycle"},
    "ACCOUNT_ADMIN": {"userservice", "oriso-account-lifecycle", "oriso-otp"},
    "MAIL": {
        "tenantservice",
        "agencyservice",
        "consultingtypeservice",
        "userservice",
        "oriso-task-commands",
    },
    "RUNTIME": {"tenantservice", "agencyservice", "appointmentservice"},
}


def bindings():
    return {
        "model": "FIVE_ACTORS",
        "realmId": "10000000-0000-4000-8000-000000000001",
        "contextKeyVersion": "1",
        "humanClientIds": ["app", "admin"],
        "contextKeySecretRef": {"name": "shared-task-context", "key": "context-key"},
        "ordinaryGroups": {
            "/human-roles/asker": ["user"],
            "/human-roles/consultant": ["consultant"],
        },
        "actors": {
            actor: {
                "clientId": "backend-" + actor.lower().replace("_", "-"),
                "serviceSubject": f"20000000-0000-4000-8000-{i:012d}",
                "secretRef": {"name": "task-credentials", "key": actor.lower()},
            }
            for i, actor in enumerate(ACTORS, 1)
        },
    }


def render(config=None):
    with tempfile.TemporaryDirectory() as folder:
        values = Path(folder) / "scenario-c.yaml"
        values.write_text(
            yaml.safe_dump(
                {"global": {"taskIdentities": config or {"model": "LEGACY"}}}
            )
        )
        command = ["helm", "template", "scenario-c", str(ROOT)]
        for source in (
            "values.yaml.default",
            "tests/fixtures/values-render-domain.yaml",
            "secrets.yaml.default",
            "tests/fixtures/render-required-secrets.yaml",
        ):
            command.extend(("-f", str(ROOT / source)))
        command.extend(
            (
                "-f",
                str(values),
                "--set-string",
                "global.secrets.redisdefaultPass=render-redis",
                "--set-string",
                "tenantService.smtpPasswordEncryptionSecret=render-encryption",
                "--set-string",
                "global.keycloak.realm=render-realm",
            )
        )
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        docs = list(yaml.safe_load_all(result.stdout)) if result.returncode == 0 else []
        return result, [doc for doc in docs if doc]


def container(docs, service):
    deployment = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == service
    )
    return next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == service
    )


class ScenarioCChartBindingsTest(unittest.TestCase):
    def test_five_actor_legacy_hook_has_no_legacy_client_credentials(self):
        result, docs = render(bindings())
        self.assertEqual(0, result.returncode, result.stderr)
        job = next(doc for doc in docs if doc.get('kind') == 'Job'
                   and doc['metadata']['name'] == 'keycloak-reconcile-service-identities')
        env = {row['name']: row for row in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual('FIVE_ACTORS', env.get('TASK_IDENTITY_MODEL', {}).get('value'))
        for name in ('TECHNICAL_CLIENT_SECRET', 'ADMIN_CLIENT_SECRET', 'SMTP_SYNC_CLIENT_SECRET'):
            self.assertNotIn(name, env)

    def test_account_mutation_ingress_allows_client_intent_correlation_header(self):
        for config in ({"model": "LEGACY"}, bindings()):
            with self.subTest(model=config["model"]):
                result, docs = render(config)
                self.assertEqual(0, result.returncode, result.stderr)
                ingress = next(
                    doc
                    for doc in docs
                    if doc.get("kind") == "Ingress"
                    and doc["metadata"]["name"] == "userservice-rewrite-ingress"
                )
                annotations = ingress["metadata"]["annotations"]
                self.assertEqual(
                    "true", annotations["nginx.ingress.kubernetes.io/enable-cors"]
                )
                headers = annotations["nginx.ingress.kubernetes.io/cors-allow-headers"]
                self.assertIn(
                    "idempotency-key",
                    {header.strip().lower() for header in headers.split(",")},
                )

    def test_enabled_real_chart_passes_one_shared_key_and_exact_actor_secret_ownership(
        self,
    ):
        result, docs = render(bindings())
        self.assertEqual(0, result.returncode, result.stderr)
        for service in (
            "userservice",
            "tenantservice",
            "agencyservice",
            "consultingtypeservice",
            "keycloak",
        ):
            with self.subTest(service=service):
                rows = container(docs, service)["env"]
                env = {row["name"]: row for row in rows}
                self.assertEqual(
                    len(rows),
                    len(env),
                    "Duplicate env entries may override validated values",
                )
                self.assertTrue(
                    "TASK_IDENTITY_MODEL" in env, "Missing explicit scenario C mode"
                )
                self.assertEqual("FIVE_ACTORS", env["TASK_IDENTITY_MODEL"]["value"])
                for actor in ACTORS:
                    self.assertEqual(
                        bindings()["actors"][actor]["serviceSubject"],
                        env[f"IDENTITY_{actor}_SERVICE_SUBJECT"]["value"],
                    )
                expected = (
                    set(ACTORS)
                    if service == "userservice"
                    else {"SETUP"} if service == "tenantservice" else set()
                )
                actual = {
                    actor
                    for actor in ACTORS
                    if f"IDENTITY_{actor}_CLIENT_SECRET" in env
                }
                self.assertEqual(expected, actual)
                if service == "agencyservice":
                    self.assertNotIn("ORISO_TASK_CONTEXT_KEY", env)
                else:
                    self.assertEqual(
                        bindings()["contextKeySecretRef"],
                        env["ORISO_TASK_CONTEXT_KEY"]["valueFrom"]["secretKeyRef"],
                    )
        keycloak = {row["name"]: row for row in container(docs, "keycloak")["env"]}
        self.assertTrue(
            "ORISO_ACCOUNT_LIFECYCLE_ENABLED" in keycloak,
            "Missing lifecycle provider configuration",
        )
        self.assertEqual("true", keycloak["ORISO_ACCOUNT_LIFECYCLE_ENABLED"]["value"])
        self.assertEqual(
            "render-realm", keycloak["ORISO_ACCOUNT_LIFECYCLE_REALM"]["value"]
        )
        self.assertTrue(
            "ORISO_SMTP_CONTROL_ENABLED" in keycloak,
            "Missing SMTP receiver enable binding",
        )
        self.assertEqual("true", keycloak["ORISO_SMTP_CONTROL_ENABLED"]["value"])
        self.assertEqual("render-realm", keycloak["ORISO_SMTP_CONTROL_REALM"]["value"])
        self.assertEqual(
            bindings()["realmId"], keycloak["KEYCLOAK_REALM_INTERNAL_ID"]["value"]
        )
        user = {row["name"]: row for row in container(docs, "userservice")["env"]}
        self.assertTrue(
            "ORISO_IDENTITY_HUMAN_CLIENT_IDS" in user,
            "Missing reviewed human client pins",
        )
        self.assertEqual(
            bindings()["humanClientIds"],
            json.loads(user["ORISO_IDENTITY_HUMAN_CLIENT_IDS"]["value"]),
        )
        self.assertEqual(
            bindings()["ordinaryGroups"],
            json.loads(keycloak["ORISO_ACCOUNT_LIFECYCLE_GROUPS"]["value"]),
        )

    def test_enabled_services_do_not_receive_legacy_backend_credentials(self):
        result, docs = render(bindings())
        self.assertEqual(0, result.returncode, result.stderr)
        forbidden = {
            "KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET",
            "KEYCLOAK_BACKEND_ADMIN_CLIENT_SECRET",
        }
        for service in (
            "userservice",
            "tenantservice",
            "agencyservice",
            "consultingtypeservice",
        ):
            names = {row["name"] for row in container(docs, service)["env"]}
            self.assertFalse(
                names & forbidden, "Legacy credential mounted in " + service
            )

    def test_import_policy_guard_is_required_only_by_its_tenant_receiver(self):
        result, docs = render(bindings())
        self.assertEqual(0, result.returncode, result.stderr)
        for service in ("tenantservice", "agencyservice", "consultingtypeservice"):
            env = {row["name"]: row for row in container(docs, service)["env"]}
            purposes = env["TASK_IDENTITY_REQUIRED_TASKS"]["value"].split(",")
            self.assertEqual(
                service == "tenantservice", "CONSULTANT_IMPORT" in purposes
            )
            self.assertEqual(len(purposes), len(set(purposes)))

    def test_legacy_chart_does_not_enable_or_mount_task_credentials(self):
        result, docs = render()
        self.assertEqual(0, result.returncode, result.stderr)
        for service in (
            "userservice",
            "tenantservice",
            "agencyservice",
            "consultingtypeservice",
            "keycloak",
        ):
            names = {row["name"] for row in container(docs, service)["env"]}
            self.assertNotIn("TASK_IDENTITY_MODEL", names)
            self.assertNotIn("ORISO_TASK_CONTEXT_KEY", names)

    def test_incomplete_or_colliding_bindings_refuse_render_without_secret_output(self):
        variants = []
        for field in (
            "realmId",
            "contextKeyVersion",
            "contextKeySecretRef",
            "ordinaryGroups",
            "humanClientIds",
        ):
            value = bindings()
            value[field] = ""
            variants.append((field, value))
        for field in ("clientId", "serviceSubject", "secretRef"):
            value = bindings()
            value["actors"]["MAIL"][field] = copy.deepcopy(
                value["actors"]["SETUP"][field]
            )
            variants.append(("duplicate " + field, value))
        value = bindings()
        value["actors"]["MAIL"]["clientId"] = "admin-cli"
        variants.append(("human client", value))
        value = bindings()
        value["actors"]["MAIL"]["serviceSubject"] = "not-a-uuid"
        variants.append(("subject", value))
        value = bindings()
        value["contextKeySecretRef"] = value["actors"]["MAIL"]["secretRef"].copy()
        variants.append(("same key/credential ref", value))
        value = bindings()
        value["actors"]["SIXTH"] = value["actors"]["MAIL"].copy()
        variants.append(("sixth actor", value))
        value = bindings()
        value["model"] = "FIVE_ACTOR"
        variants.append(("unknown model", value))
        for name, value in variants:
            with self.subTest(name=name):
                result, _ = render(value)
                self.assertNotEqual(
                    0,
                    result.returncode,
                    "Unsafe scenario C configuration rendered: " + name,
                )
                self.assertIn("taskIdentities", result.stderr)
                self.assertEqual("", result.stdout)

    def test_registry_has_one_final_bearer_audience_union_per_actor(self):
        registry = json.loads(
            (ROOT / "files/scenario-c-task-identities.json").read_text()
        )
        self.assertEqual(set(ACTORS), {actor["key"] for actor in registry["actors"]})
        for actor in registry["actors"]:
            self.assertEqual(
                AUDIENCES[actor["key"]], set(actor.get("bearerAudiences", []))
            )
            self.assertEqual(
                len(actor["bearerAudiences"]), len(set(actor["bearerAudiences"]))
            )


if __name__ == "__main__":
    unittest.main()
