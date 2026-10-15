#!/usr/bin/env python3
"""Render guard: every Synapse config the chart ships is actually loaded.

Synapse reads only the files passed as --config-path. A config file that sits in
a ConfigMap but is never mounted and passed is dead: it looks active in review
and silently does nothing. The Keycloak oidc_providers block was such a file
(matrix-oidc-config, never mounted since the initial commit; ORISO-Helm#282).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNAPSE_CONFIG_RESOURCE_PREFIX = "matrix-"
SYNAPSE_CONFIG_KEYS = {
    "admin_contact",
    "database",
    "enable_authenticated_media",
    "enable_registration",
    "federation_domain_whitelist",
    "listeners",
    "macaroon_secret_key",
    "media_store_path",
    "oidc_providers",
    "pid_file",
    "presence",
    "public_baseurl",
    "rc_message",
    "redis",
    "registration_shared_secret",
    "report_stats",
    "server_name",
    "signing_key_path",
}


def render() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "synapse-config",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]


def resource_documents(documents: list[dict]) -> dict[tuple[str, str], dict]:
    """Index rendered ConfigMaps and Secrets by their Kubernetes identity."""
    return {
        (document["kind"], document["metadata"]["name"]): document
        for document in documents
        if document.get("kind") in ("ConfigMap", "Secret")
    }


def mounted_key_source(
    source_kind: str, source_name: str, key: str, resources: dict[tuple[str, str], dict]
) -> tuple[str, str, str]:
    """Return a mounted config identity after ensuring its rendered key exists."""
    source = resources.get((source_kind, source_name))
    assert source, f"{source_kind}/{source_name} is mounted but not rendered"
    values = {**(source.get("data") or {}), **(source.get("stringData") or {})}
    assert key in values, f"{source_kind}/{source_name} key {key} is mounted but not rendered"
    return source_kind, source_name, key


def loaded_files(
    synapse: dict, documents: list[dict]
) -> dict[str, tuple[str, str, str]]:
    """Map each --config-path to the (volume source name, key) mounted there."""
    spec = synapse["spec"]["template"]["spec"]
    container = spec["containers"][0]
    args = container.get("args", [])
    paths = [args[i + 1] for i, a in enumerate(args) if a == "--config-path"]
    if not paths:
        # The Synapse image's start.py falls back to SYNAPSE_CONFIG_PATH or /data/homeserver.yaml.
        env = {e["name"]: e.get("value") for e in container.get("env", [])}
        paths = [env.get("SYNAPSE_CONFIG_PATH") or "/data/homeserver.yaml"]
    volumes = {v["name"]: v for v in spec["volumes"]}
    resources = resource_documents(documents)

    files = {}
    for path in paths:
        for mount in container["volumeMounts"]:
            volume = volumes[mount["name"]]
            source_kind = "ConfigMap" if "configMap" in volume else "Secret"
            source = volume.get("configMap") or volume.get("secret")
            if not source:
                continue
            name = source.get("name") or source.get("secretName")
            if mount.get("subPath") and mount["mountPath"] == path:
                files[path] = mounted_key_source(
                    source_kind, name, mount["subPath"], resources
                )
            for item in source.get("items", []):
                if os.path.join(mount["mountPath"], item["path"]) == path:
                    files[path] = mounted_key_source(
                        source_kind, name, item["key"], resources
                    )
        assert path in files, f"--config-path {path} is not backed by a mounted ConfigMap/Secret key"
    return files


def parse_config_value(document: dict, field: str, value: str) -> dict | None:
    """Parse plain or base64-encoded configuration without exposing secret values."""
    if document["kind"] == "Secret" and field == "data":
        try:
            value = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def main() -> None:
    documents = render()
    synapse = next(
        d for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "matrix-synapse"
    )
    loaded = set(loaded_files(synapse, documents).values())

    # Every ConfigMap/Secret key that holds Synapse homeserver config must be loaded.
    for doc in documents:
        if doc["kind"] not in ("ConfigMap", "Secret"):
            continue
        # Only Matrix-owned resources can be Synapse configuration. Other
        # services also use generic YAML keys such as server_name.
        if not doc["metadata"]["name"].startswith(SYNAPSE_CONFIG_RESOURCE_PREFIX):
            continue
        for field in ("data", "stringData"):
            for key, value in (doc.get(field) or {}).items():
                if not isinstance(value, str):
                    continue
                parsed = parse_config_value(doc, field, value)
                if parsed and SYNAPSE_CONFIG_KEYS & parsed.keys():
                    identity = (doc["kind"], doc["metadata"]["name"], key)
                    assert identity in loaded, (
                        f"{doc['kind']}/{doc['metadata']['name']} key {key} holds Synapse "
                        f"config but is not passed to Synapse via --config-path"
                    )

    # Matrix SSO is not offered: accounts log in with m.login.password (ADR-005)
    # and no Keycloak client "matrix-synapse" exists in the realm.
    assert "oidc_providers" not in json.dumps(documents), "dead oidc_providers block rendered"
    print("synapse config loaded: OK")


if __name__ == "__main__":
    main()
