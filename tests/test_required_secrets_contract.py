#!/usr/bin/env python3
"""Contracts on the shipped secret defaults.

The shipped defaults must not declare a mapping key twice.

`secrets.yaml.default` once declared `tenantService:` twice. YAML keeps the last
mapping, so the second block silently discarded `smtpPasswordEncryptionSecret`
from the first, together with the comment documenting it as required. Reading
the file the key was plainly there; resolving the file it was gone, and nothing
downstream could report it because the parser accepts the duplicate.

The check parses with a loader that refuses duplicates, so it follows real YAML
semantics at every nesting level instead of matching lines.
"""

from __future__ import annotations

import base64
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULTS = ("values.yaml.default", "secrets.yaml.default")


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that raises on a repeated key instead of keeping the last."""


def _construct_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r}: every earlier value is silently discarded",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


class DefaultsDeclareEachKeyOnce(unittest.TestCase):
    def test_no_mapping_key_is_declared_twice(self):
        for name in DEFAULTS:
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                try:
                    yaml.load(text, Loader=StrictLoader)
                except yaml.constructor.ConstructorError as error:
                    self.fail(f"{name}: {error.problem} (line {error.problem_mark.line + 1})")

    def test_the_loader_does_catch_the_original_defect(self):
        """Guards the guard: a loader that never raises would pass the test above."""
        duplicated = "tenantService:\n  a: 1\ntenantService:\n  b: 2\n"
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load(duplicated, Loader=StrictLoader)

        nested = "outer:\n  inner: 1\n  inner: 2\n"
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load(nested, Loader=StrictLoader)


def render(*overrides: str, show_only: str | None = None) -> subprocess.CompletedProcess:
    command = ["helm", "template", "caritas", str(ROOT)]
    for name in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml", "secrets.yaml.default"):
        command += ["-f", str(ROOT / name)]
    command += list(overrides)
    if show_only:
        command += ["--show-only", show_only]
    return subprocess.run(command, capture_output=True, text=True, check=False)


FIXTURE = ("-f", str(ROOT / "tests" / "fixtures" / "render-required-secrets.yaml"))


class PlaceholderCredentialsFailTheRender(unittest.TestCase):
    """ORISO-Helm#540: a credential still at its shipped value must stop the install."""

    def assert_fails_naming(self, key: str, *overrides: str) -> None:
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0, f"render passed with a bad {key}")
        self.assertIn(key, result.stderr)

    def test_unchanged_secrets_default_fails_on_the_first_key(self):
        # Keep the fixture's identity values, so only the shipped credentials remain.
        fixture = yaml.safe_load((ROOT / FIXTURE[1]).read_text(encoding="utf-8"))
        secrets = fixture["global"]["secrets"]
        for key in [k for k in secrets if k.endswith(("Password", "Pass", "SharedSecret"))]:
            del secrets[key]
        del fixture["global"]["matrix"], fixture["online-counseling-mariadb"], fixture["online-counseling-mongodb"]
        with tempfile.TemporaryDirectory() as directory:
            identities = pathlib.Path(directory) / "identities.yaml"
            identities.write_text(yaml.safe_dump(fixture), encoding="utf-8")
            self.assert_fails_naming("global.secrets.liquibasePassword", "-f", str(identities))

    def test_render_fixture_satisfies_every_rule(self):
        result = render(*FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_rule_names_the_key(self):
        cases = {
            "empty": ("global.secrets.redisdefaultPass", "--set-string", "global.secrets.redisdefaultPass="),
            "placeholder, any case": (
                "global.secrets.keycloakAdminPassword",
                "--set-string", "global.secrets.keycloakAdminPassword=MyCHANGEMEpassword",
            ),
            "shorter than 12": ("global.secrets.rabbitmqPassword", "--set-string", "global.secrets.rabbitmqPassword=elevenchars"),
            "empty username": ("global.secrets.liquibaseUser", "--set-string", "global.secrets.liquibaseUser="),
            "matrix admin": ("global.matrix.matrixAdminPassword", "--set-string", "global.matrix.matrixAdminPassword=changeme"),
            "mariadb root": (
                "online-counseling-mariadb.dbRootPassword",
                "--set-string", "online-counseling-mariadb.dbRootPassword=changeme",
            ),
            "mongodb root": (
                "online-counseling-mongodb.mongoInitDbRootPassword",
                "--set-string", "online-counseling-mongodb.mongoInitDbRootPassword=short",
            ),
        }
        for rule, (key, *override) in cases.items():
            with self.subTest(rule=rule):
                self.assert_fails_naming(key, *FIXTURE, *override)

    def test_every_value_is_checked_for_the_placeholder(self):
        """The catch-all walks all values, subcharts and lists included."""
        cases = {
            "service key": "agencyService.serviceEncryptionAppkey=changeme",
            "nested, any case": "online-counseling-mongodb.mongoSharedKey=ChangeMeMongoReplicaKey2026",
            "unused engine": "postgres.postgresPassword=changeme",
            "list item": "matrixrtcAuth.extra[1]=prefix-CHANGEME",
            "outside global.secrets": "global.secrets.keycloakServiceAdminPassword=changeme",
        }
        for rule, assignment in cases.items():
            key = assignment.split("=", 1)[0]
            with self.subTest(rule=rule):
                self.assert_fails_naming(key, *FIXTURE, "--set-string", assignment)

    def test_every_shipped_placeholder_fails(self):
        """Each "changeme" line of secrets.yaml.default fails on its own, by its path."""
        shipped = yaml.safe_load((ROOT / "secrets.yaml.default").read_text(encoding="utf-8"))

        def placeholders(node, path=()):
            if isinstance(node, dict):
                for key, child in node.items():
                    yield from placeholders(child, path + (key,))
            elif isinstance(node, str) and "changeme" in node.lower():
                yield ".".join(path), node

        found = list(placeholders(shipped))
        self.assertGreaterEqual(len(found), 35)
        for key, value in found:
            with self.subTest(key=key):
                self.assert_fails_naming(key, *FIXTURE, "--set-string", f"{key}={value}")

    def test_twelve_characters_is_enough(self):
        result = render(*FIXTURE, "--set-string", "global.secrets.rabbitmqPassword=twelve-chars")
        self.assertEqual(result.returncode, 0, result.stderr)


class RedisSecretEncoding(unittest.TestCase):
    def test_http_user_decodes_to_the_configured_user(self):
        for overrides, expected in (((), "default"), (("--set-string", "online-counseling-redis.user=metrics"), "metrics")):
            with self.subTest(user=expected):
                result = render(*FIXTURE, *overrides, show_only="charts/online-counseling-redis/templates/redis-secret.yaml")
                self.assertEqual(result.returncode, 0, result.stderr)
                data = yaml.safe_load(result.stdout)["data"]
                self.assertEqual(base64.b64decode(data["HTTP_USER"], validate=True).decode(), expected)


if __name__ == "__main__":
    unittest.main()
