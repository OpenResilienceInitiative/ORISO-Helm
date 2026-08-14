#!/usr/bin/env python3
"""Tests for the external MatrixRTC/LiveKit Secret preflight."""

from __future__ import annotations

import base64
import importlib.util
import pathlib
import unittest

import yaml

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify-matrixrtc-runtime-secrets.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_matrixrtc_runtime_secrets", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class VerifyMatrixRtcRuntimeSecretsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verify = load_module()

    def auth_secret(self) -> dict:
        return {
            "metadata": {"name": "matrixrtc-auth-runtime"},
            "data": {
                "matrix-membership-token": encoded("matrix-token-long-enough"),
                "matrix-call-policy-token": encoded("p" * 48),
                "livekit-api-key": encoded("oriso-livekit-key"),
                "livekit-api-secret": encoded("s" * 32),
                "redis-url": encoded("redis://:password@redis:6379"),
            },
        }

    def config_secret(self) -> dict:
        config = yaml.safe_dump(
            {
                "port": 7880,
                "redis": {"address": "redis:6379"},
                "keys": {"oriso-livekit-key": "s" * 32},
                "webhook": {"api_key": "oriso-livekit-key"},
            }
        )
        return {
            "metadata": {"name": "livekit-config-runtime"},
            "data": {"config.yaml": encoded(config)},
        }

    def test_valid_external_secrets_return_only_redacted_evidence(self) -> None:
        result = self.verify.verify_secrets(self.auth_secret(), self.config_secret())

        self.assertEqual(result["authSecret"], "matrixrtc-auth-runtime")
        self.assertEqual(result["livekitConfigSecret"], "livekit-config-runtime")
        self.assertTrue(result["externallyManaged"])
        self.assertTrue(result["livekitCredentialsMatch"])
        self.assertNotIn("values", result)
        rendered = str(result)
        self.assertNotIn("matrix-token-long-enough", rendered)
        self.assertNotIn("redis://", rendered)

    def test_missing_placeholder_or_helm_owned_secret_fails(self) -> None:
        missing = self.auth_secret()
        del missing["data"]["redis-url"]
        with self.assertRaisesRegex(ValueError, "redis-url"):
            self.verify.verify_secrets(missing, self.config_secret())

        placeholder = self.auth_secret()
        placeholder["data"]["matrix-membership-token"] = encoded("pending-bootstrap")
        with self.assertRaisesRegex(ValueError, "matrix-membership-token"):
            self.verify.verify_secrets(placeholder, self.config_secret())

        helm_owned = self.auth_secret()
        helm_owned["metadata"]["annotations"] = {"meta.helm.sh/release-name": "caritas"}
        with self.assertRaisesRegex(ValueError, "Helm-owned"):
            self.verify.verify_secrets(helm_owned, self.config_secret())

    def test_mismatched_livekit_credentials_fail(self) -> None:
        auth = self.auth_secret()
        auth["data"]["livekit-api-secret"] = encoded("x" * 32)

        with self.assertRaisesRegex(ValueError, "LiveKit credentials"):
            self.verify.verify_secrets(auth, self.config_secret())


if __name__ == "__main__":
    unittest.main()
