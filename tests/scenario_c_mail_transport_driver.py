#!/usr/bin/env python3
"""Strict joined-native test adapter: one broker-verified bearer arrives through stdin only."""

import importlib.util
import json
from pathlib import Path
import sys


def main():
    if len(sys.argv) not in (2, 3) or len(sys.argv) == 3 and sys.argv[2] != "mail-refused":
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
    if len(sys.argv) == 3:
        # Independent SETUP status readback observes native revision without granting MAIL a read.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
        from scenario_c_activation import command_proof
        setup = module.setup_login(http, config)
        url = config["KEYCLOAK_URL"] + "/realms/" + config["KEYCLOAK_REALM"] + "/oriso-smtp-control/v1/smtp"
        def status_read():
            proof = command_proof(config["context_key"], {}, "SETUP", config["IDENTITY_SETUP_SERVICE_SUBJECT"],
                config["KEYCLOAK_REALM"], config["KEYCLOAK_REALM_INTERNAL_ID"], "SMTP_STATUS", "GET", smtp=True)
            return http.request("GET", url, headers={"Authorization": "Bearer " + setup,
                "X-ORISO-Task-Context": proof})
        before = status_read()
        try:
            module.apply_setup_snapshot(http, config, token, snapshot, revision + 1)
        except module.ReconcileError as refused:
            if refused.status != 403:
                raise ValueError("Actual MAIL request must reach explicit receiver refusal") from None
        else:
            raise ValueError("MAIL unexpectedly configured SMTP")
        if before != status_read() or (snapshot, revision) != module.read_snapshot(http, config, token):
            raise ValueError("Rejected MAIL changed native or saved CTS state")
        # No acknowledgement function is called in this path.
        print("NATIVE_MAIL_SMTP_REFUSED_PASSED")
        return
    status = module.apply_setup_snapshot(http, config, token, snapshot, revision)
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
