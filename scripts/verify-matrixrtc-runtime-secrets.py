#!/usr/bin/env python3
"""Validate external MatrixRTC/LiveKit Secrets without printing their values."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import pathlib
import subprocess
import sys
from collections.abc import Mapping
from urllib.parse import urlparse

import yaml

AUTH_KEYS = {
    "matrix-membership-token",
    "matrix-call-policy-token",
    "livekit-api-key",
    "livekit-api-secret",
    "redis-url",
}
PLACEHOLDERS = {"", "changeme", "pending-bootstrap"}


def require_external(secret: Mapping, expected_name: str) -> None:
    metadata = secret.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("name") != expected_name:
        raise ValueError(f"expected Secret {expected_name}")
    annotations = metadata.get("annotations", {})
    labels = metadata.get("labels", {})
    if (
        isinstance(annotations, Mapping) and "meta.helm.sh/release-name" in annotations
    ) or (
        isinstance(labels, Mapping)
        and labels.get("app.kubernetes.io/managed-by") == "Helm"
    ):
        raise ValueError(f"Secret {expected_name} is Helm-owned")


def decode_secret_data(secret: Mapping, required: set[str]) -> dict[str, str]:
    data = secret.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("Secret data is missing")
    missing = sorted(required - set(data))
    if missing:
        raise ValueError("Secret is missing keys: " + ", ".join(missing))
    decoded: dict[str, str] = {}
    for key in required:
        value = data.get(key)
        if not isinstance(value, str):
            raise ValueError(f"Secret key {key} is not a base64 string")
        try:
            decoded[key] = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ValueError(f"Secret key {key} is not valid base64 UTF-8") from error
    return decoded


def require_credential(name: str, value: str, minimum: int) -> None:
    if value.lower() in PLACEHOLDERS or len(value) < minimum:
        raise ValueError(f"Secret key {name} is blank, placeholder, or too short")


def verify_secrets(auth_secret: Mapping, config_secret: Mapping) -> dict:
    require_external(auth_secret, "matrixrtc-auth-runtime")
    require_external(config_secret, "livekit-config-runtime")
    auth = decode_secret_data(auth_secret, AUTH_KEYS)
    config_data = decode_secret_data(config_secret, {"config.yaml"})

    require_credential("matrix-membership-token", auth["matrix-membership-token"], 20)
    require_credential("matrix-call-policy-token", auth["matrix-call-policy-token"], 48)
    require_credential("livekit-api-key", auth["livekit-api-key"], 8)
    require_credential("livekit-api-secret", auth["livekit-api-secret"], 32)

    redis_url = urlparse(auth["redis-url"])
    if redis_url.scheme not in {"redis", "rediss"} or not redis_url.hostname:
        raise ValueError("Secret key redis-url is not a complete Redis URL")

    config = yaml.safe_load(config_data["config.yaml"])
    if not isinstance(config, Mapping):
        raise ValueError("LiveKit config.yaml must be a mapping")
    keys = config.get("keys")
    webhook = config.get("webhook")
    if not isinstance(keys, Mapping) or not isinstance(webhook, Mapping):
        raise ValueError("LiveKit config.yaml is missing keys or webhook")
    livekit_key = auth["livekit-api-key"]
    if (
        keys.get(livekit_key) != auth["livekit-api-secret"]
        or webhook.get("api_key") != livekit_key
    ):
        raise ValueError("LiveKit credentials differ between the external Secrets")

    return {
        "apiVersion": "oriso.org/v1alpha1",
        "kind": "MatrixRtcRuntimeSecretEvidence",
        "authSecret": "matrixrtc-auth-runtime",
        "authKeys": sorted(AUTH_KEYS),
        "livekitConfigSecret": "livekit-config-runtime",
        "livekitConfigKeys": ["config.yaml"],
        "externallyManaged": True,
        "livekitCredentialsMatch": True,
    }


def get_secret(namespace: str, name: str) -> dict:
    result = subprocess.run(
        ["kubectl", "--namespace", namespace, "get", "secret", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot read Secret {name}: {result.stderr.strip()}")
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError(f"Secret {name} did not return an object")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="caritas")
    parser.add_argument("--output", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = verify_secrets(
            get_secret(args.namespace, "matrixrtc-auth-runtime"),
            get_secret(args.namespace, "livekit-config-runtime"),
        )
        if args.output:
            if args.output.exists():
                raise ValueError(f"refusing to overwrite {args.output}")
            args.output.write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    ) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    suffix = f"; wrote {args.output}" if args.output else ""
    print(
        f"PASS: external MatrixRTC/LiveKit Secrets are complete and consistent{suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
