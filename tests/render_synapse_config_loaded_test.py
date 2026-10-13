#!/usr/bin/env python3
"""Render guard: every Synapse config the chart ships is actually loaded.

Synapse reads only the files passed as --config-path. A config file that sits in
a ConfigMap but is never mounted and passed is dead: it looks active in review
and silently does nothing. The Keycloak oidc_providers block was such a file
(matrix-oidc-config, never mounted since the initial commit; ORISO-Helm#282).
"""

from __future__ import annotations

import json
import os
import subprocess

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def loaded_files(synapse: dict) -> dict[str, tuple[str, str]]:
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

    files = {}
    for path in paths:
        for mount in container["volumeMounts"]:
            volume = volumes[mount["name"]]
            source = volume.get("configMap") or volume.get("secret")
            if not source:
                continue
            name = source.get("name") or source.get("secretName")
            if mount.get("subPath") and mount["mountPath"] == path:
                files[path] = (name, mount["subPath"])
            for item in source.get("items", []):
                if os.path.join(mount["mountPath"], item["path"]) == path:
                    files[path] = (name, item["key"])
        assert path in files, f"--config-path {path} is not backed by a mounted ConfigMap/Secret key"
    return files


def main() -> None:
    documents = render()
    synapse = next(
        d for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "matrix-synapse"
    )
    loaded = set(loaded_files(synapse).values())

    # Every ConfigMap/Secret key that holds Synapse homeserver config must be loaded.
    synapse_keys = {"oidc_providers", "server_name", "database", "listeners"}
    for doc in documents:
        if doc["kind"] not in ("ConfigMap", "Secret"):
            continue
        for field in ("data", "stringData"):
            for key, value in (doc.get(field) or {}).items():
                if not key.endswith(".yaml") or not isinstance(value, str):
                    continue
                try:
                    parsed = yaml.safe_load(value)
                except yaml.YAMLError:
                    continue
                if isinstance(parsed, dict) and synapse_keys & parsed.keys():
                    assert (doc["metadata"]["name"], key) in loaded, (
                        f"{doc['kind']}/{doc['metadata']['name']} key {key} holds Synapse "
                        f"config but is not passed to Synapse via --config-path"
                    )

    # Matrix SSO is not offered: accounts log in with m.login.password (ADR-005)
    # and no Keycloak client "matrix-synapse" exists in the realm.
    assert "oidc_providers" not in json.dumps(documents), "dead oidc_providers block rendered"
    print("synapse config loaded: OK")


if __name__ == "__main__":
    main()
