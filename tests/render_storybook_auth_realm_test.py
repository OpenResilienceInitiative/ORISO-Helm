#!/usr/bin/env python3
"""Keep Storybook Basic Auth enabled regardless of legacy realm overrides."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from render_storybook_dev_test import render


STORYBOOK_NAMES = {"storybook-admin", "storybook-frontend", "storybook-dev-ingress"}


def storybook_resources(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in docs if doc.get("metadata", {}).get("name") in STORYBOOK_NAMES
    }


def render_override(storybook: dict) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="storybook-auth-realm-") as directory:
        override = Path(directory) / "override.yaml"
        override.write_text(yaml.safe_dump({"storybook": storybook}))
        return render("values-dev.yaml", str(override))


class StorybookAuthRealmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dev = render("values-dev.yaml")

    def assert_fixed_realm(self, realm: str | None) -> None:
        docs = render_override({"ingress": {"authRealm": realm}})
        ingress = storybook_resources(docs)[("Ingress", "storybook-dev-ingress")]
        annotations = ingress["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-realm"], "ORISO Storybook")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-type"], "basic")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-secret"], "storybook-basic-auth")
        self.assertEqual(docs, self.dev, "Ignoring a realm override must not change any resources")

    def test_unsafe_overrides_cannot_disable_auth(self) -> None:
        for realm in ("off", "OFF", "$host", "$http_x_auth_realm"):
            with self.subTest(realm=realm):
                self.assert_fixed_realm(realm)

    def test_safe_and_empty_legacy_overrides_use_constant_realm(self) -> None:
        for realm in ("ORISO Storybook", "Custom operator label", "", None):
            with self.subTest(realm=realm):
                self.assert_fixed_realm(realm)

    def test_default_dev_preserves_both_routes_and_workloads(self) -> None:
        resources = storybook_resources(self.dev)
        self.assertEqual(set(resources), {
            ("Ingress", "storybook-dev-ingress"),
            ("Service", "storybook-admin"), ("Service", "storybook-frontend"),
            ("Deployment", "storybook-admin"), ("Deployment", "storybook-frontend"),
        })
        ingress = resources[("Ingress", "storybook-dev-ingress")]
        self.assertEqual(ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/auth-realm"],
                         "ORISO Storybook")
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertEqual({path["path"] for path in paths}, {
            "/storybook-admin(/|$)(.*)", "/storybook-frontend(/|$)(.*)",
        })
        for path in paths:
            backend = path["backend"]["service"]
            service = resources[("Service", backend["name"])]
            self.assertEqual(backend["port"]["number"], service["spec"]["ports"][0]["port"])

    def test_disabled_storybook_stays_disabled(self) -> None:
        self.assertEqual(storybook_resources(render()), {})
        self.assertEqual(storybook_resources(render_override({
            "enabled": False, "ingress": {"authRealm": "off"},
        })), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
