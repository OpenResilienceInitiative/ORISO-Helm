"""Actual rendered realm fixture for semantic contracts over Helm's JSON template."""

import base64
from functools import lru_cache
import json
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _rendered_realm_bytes():
    command = ["helm", "template", "realm-contract", str(ROOT)]
    for path in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
                 "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        command.extend(["-f", str(ROOT / path)])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    resource = next(
        doc for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("kind") == "Secret"
        and doc.get("metadata", {}).get("name") == "keycloak-realm-import"
    )
    return base64.b64decode(resource["data"]["realm.json"])


def rendered_realm():
    # Give each semantic test a fresh object; cache only the external render bytes.
    return json.loads(_rendered_realm_bytes())
