#!/usr/bin/env python3
"""Strict joined-native test adapter: one broker-verified bearer arrives through stdin only."""

import importlib.util
import json
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 2:
        raise ValueError("Private native fixture is required")
    fixture = json.loads(Path(sys.argv[1]).read_text())
    token = sys.stdin.read(32769).strip()
    if not token or len(token) > 32768:
        raise ValueError("Bounded broker token input is required")
    spec = importlib.util.spec_from_file_location(
        "smtp_reconciler", Path(__file__).resolve().parents[1] / "files/keycloak-reconcile-smtp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.configuration(fixture["mailTransportEnvironment"])
    if config.get("TASK_IDENTITY_MODEL") != "FIVE_ACTORS":
        raise ValueError("Native C transport is required")
    http = module.HttpClient()
    snapshot, revision = module.read_snapshot(http, config, token)
    if revision != 7:
        raise ValueError("Exact synthetic saved revision required")
    status = module.apply_mail_snapshot(http, config, token, snapshot, revision)
    if status != "APPLIED" or module.acknowledge(http, config, token, revision, status) != "SMTP_RECONCILE_ACKNOWLEDGED":
        raise ValueError("Exact known-response acknowledgement required")
    print("NATIVE_MAIL_BROKER_CTS_SMTP_ACK_PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No fixture, body, bearer, provider diagnostics or passwords leave this adapter.
        print("NATIVE_MAIL_JOINED_GATE_FAILED", file=sys.stderr)
        sys.exit(2)
