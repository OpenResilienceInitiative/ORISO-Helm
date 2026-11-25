"""Verify that Synapse and its chart peers use the same Secret-backed credentials."""

from base64 import b64decode
import json
from pathlib import PurePosixPath
import unittest

import yaml

from render_synapse_config_loaded_test import loaded_files, render


CANARY = "render-only: matrix registration # secret"
SOURCE = ("Secret", "matrix-synapse-credentials")
KEY = "registration-shared-secret"
JOB = ("Job", "matrixrtc-bootstrap-token")


def chart(value=CANARY, *args):
    documents = render("--set-string", f"global.secrets.matrixRegistrationSharedSecret={value}", *args)
    return documents, {(d["kind"], d["metadata"]["name"]): d for d in documents}


def synapse_pod(resources):
    return resources["Deployment", "matrix-synapse"]["spec"]["template"]


class SynapseCredentialsTest(unittest.TestCase):
    def test_loaded_secret_exists_in_both_database_modes(self):
        for engine in ("sqlite3", "postgres"):
            with self.subTest(engine=engine):
                documents, resources = chart(CANARY, "--set-string", f"matrix.database.engine={engine}")
                exposed = [
                    f"{resource['kind']}/{resource['metadata']['name']}"
                    for resource in documents
                    if resource["kind"] != "Secret" and CANARY in json.dumps(resource)
                ]
                self.assertEqual(exposed, [])

                deployment = resources["Deployment", "matrix-synapse"]
                files = loaded_files(deployment, documents)
                self.assertEqual(set(files), {
                    "/data/homeserver.yaml",
                    "/secrets/synapse/homeserver-database.yaml",
                    "/secrets/synapse-credentials/homeserver-redis.yaml",
                })
                kind, name, key = files["/data/homeserver.yaml"]
                homeserver = yaml.safe_load(resources[kind, name]["data"][key])
                self.assertNotIn("registration_shared_secret", homeserver)
                path = PurePosixPath(homeserver["registration_shared_secret_path"])
                self.assertTrue(path.is_absolute())

                spec = synapse_pod(resources)["spec"]
                container, = [c for c in spec["containers"] if c["name"] == "synapse"]
                mount, = [m for m in container["volumeMounts"] if PurePosixPath(m["mountPath"]) == path.parent]
                self.assertTrue(mount["readOnly"])
                volume, = [v["secret"] for v in spec["volumes"] if v["name"] == mount["name"]]
                self.assertEqual(volume["secretName"], SOURCE[1])
                self.assertEqual(volume["items"], [
                    {"key": KEY, "path": path.name},
                    {"key": "homeserver-redis.yaml", "path": "homeserver-redis.yaml"},
                ])
                self.assertTrue(volume.get("defaultMode", 0o644) & 0o004)
                self.assertEqual(b64decode(resources[SOURCE]["data"][KEY]).decode(), CANARY)
                for service in ("userservice", "agencyservice", "tenantservice"):
                    data = resources["Secret", f"{service}-secret"]["data"]
                    self.assertEqual(b64decode(data["MATRIX_REGISTRATION_SHARED_SECRET"]).decode(), CANARY)
                args = container["args"]
                self.assertEqual(args[args.index("--keys-directory") + 1], "/data")

    def test_redis_config_uses_the_same_exact_password_as_the_server(self):
        password = " render-only: synapse Redis # credential "
        for engine in ("sqlite3", "postgres"):
            with self.subTest(engine=engine):
                documents, resources = chart(
                    CANARY,
                    "--set-string", f"global.secrets.redisdefaultPass={password}",
                    "--set-string", f"matrix.database.engine={engine}",
                )
                exposed = [
                    f"{doc['kind']}/{doc['metadata']['name']}"
                    for doc in documents
                    if doc["kind"] != "Secret" and password in json.dumps(doc)
                ]
                self.assertEqual(exposed, [])
                deployment = resources["Deployment", "matrix-synapse"]
                files = loaded_files(deployment, documents)
                configmap = resources["ConfigMap", "matrix-homeserver-oidc"]
                self.assertNotIn("redis", yaml.safe_load(configmap["data"]["homeserver.yaml"]))
                kind, name, key = files["/secrets/synapse-credentials/homeserver-redis.yaml"]
                self.assertEqual((kind, name, key), (*SOURCE, "homeserver-redis.yaml"))
                config = yaml.safe_load(resources[kind, name]["stringData"][key])
                server_password = resources["Secret", "redis-secret"]["data"]["REDIS_DEFAULT_PASS"]
                self.assertEqual(b64decode(server_password).decode(), password)
                self.assertEqual(config["redis"], {
                    "enabled": True, "host": "redis", "port": 6379, "password": password, "dbid": 0,
                })

    def test_each_credential_change_rolls_the_pod_without_changing_the_public_config(self):
        _, before = chart()
        _, next_registration = chart(CANARY + "-replacement")
        _, next_redis = chart(CANARY, "--set-string", "global.secrets.redisdefaultPass=render-only-new-password")
        key = "checksum/synapse-credentials"
        old = synapse_pod(before)["metadata"]["annotations"][key]
        self.assertRegex(old, r"^[a-f0-9]{64}$")
        for after in (next_registration, next_redis):
            self.assertEqual(before["ConfigMap", "matrix-homeserver-oidc"], after["ConfigMap", "matrix-homeserver-oidc"])
            self.assertNotEqual(old, synapse_pod(after)["metadata"]["annotations"][key])

    def test_bootstrap_uses_the_secret_only_for_provisioning(self):
        for mode, flags in (
            ("provisioning", ("--set-string", "matrixrtcAuth.membershipToken=")),
            ("manual", ("--set-string", "matrixrtcAuth.membershipToken=render-only-manual-membership-token")),
            ("off", ("--set", "matrixrtcAuth.bootstrap.enabled=false")),
        ):
            with self.subTest(mode=mode):
                _, resources = chart(CANARY, *flags)
                if mode == "off":
                    self.assertNotIn(JOB, resources)
                    continue
                job = resources[JOB]
                self.assertEqual(job["metadata"]["annotations"]["helm.sh/hook"], "post-install")
                container = job["spec"]["template"]["spec"]["containers"][0]
                env = {entry["name"]: entry for entry in container["env"]}
                if mode == "manual":
                    self.assertNotIn("MATRIX_REGISTRATION_SHARED_SECRET", env)
                else:
                    reference = env["MATRIX_REGISTRATION_SHARED_SECRET"]
                    self.assertEqual(set(reference), {"name", "valueFrom"})
                    self.assertEqual(reference["valueFrom"], {
                        "secretKeyRef": {"name": SOURCE[1], "key": KEY},
                    })
                role = resources["Role", "matrixrtc-bootstrap"]
                target = env["TARGET_SECRET_NAME"]["value"]
                self.assertNotEqual(target, SOURCE[1])
                self.assertEqual(role["rules"], [{
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "resourceNames": [target],
                    "verbs": ["get", "patch"],
                }])

    def test_boundary_whitespace_cannot_make_synapse_and_callers_disagree(self):
        with self.assertRaisesRegex(AssertionError, "matrixRegistrationSharedSecret must not start or end with whitespace"):
            chart(CANARY + " ")

    def test_chart_managed_livekit_secrets_cannot_overlap_the_registration_secret(self):
        for setting in ("matrixrtcAuth.existingSecret.name", "livekit.existingConfigSecret.name"):
            with self.subTest(setting=setting):
                with self.assertRaisesRegex(AssertionError, f"{setting} must not use {SOURCE[1]}"):
                    chart(CANARY, "--set-string", f"{setting}={SOURCE[1]}")


if __name__ == "__main__":
    unittest.main()
