#!/usr/bin/env python3
"""agencyService.deletionAuditOutboxEnabled must be a real boolean (#459).

AgencyService and UserService read the same switch. A quoted "false" is truthy in a template `if`
but reaches UserService as the string false, so AgencyService would require DELETION_AUDIT_READ
while UserService never pulls. The chart has to refuse such a value at render time.
"""
from render_task_identity_activation_test import RECEIVERS, render

AGENCY = ("ConfigMap", "agencyservice-configmap-env")
USER = ("ConfigMap", "userservice-configmap-env")
KEY = "agencyService.deletionAuditOutboxEnabled"


def flags(docs):
    return (
        docs[AGENCY]["data"]["TASK_IDENTITY_REQUIRED_TASKS"].endswith(",DELETION_AUDIT_READ"),
        docs[USER]["data"]["DELETION_AUDIT_AGENCYOUTBOX_ENABLED"],
    )


def main():
    # Booleans, and an unset value: both services agree.
    for args, expected in (
        ((), (False, "false")),
        (("--set", f"{KEY}=false"), (False, "false")),
        (("--set", f"{KEY}=true"), (True, "true")),
        (("--set", f"{KEY}=null"), (False, "false")),
        (("--set-string", f"{KEY}="), (False, "false")),
    ):
        result, docs = render(*args)
        assert result.returncode == 0, (args, result.stderr)
        assert flags(docs) == expected, (args, flags(docs))
        if not expected[0]:
            assert docs[AGENCY]["data"]["TASK_IDENTITY_REQUIRED_TASKS"] == RECEIVERS["agencyservice"]["tasks"]

    # Strings, including the dangerous quoted "false": rendering stops with a clear message.
    for value in ("false", "true", "yes"):
        result, _ = render("--set-string", f"{KEY}={value}")
        assert result.returncode != 0, f"--set-string {KEY}={value!r} must not render"
        assert f"{KEY} must be true or false without quotes" in result.stderr, result.stderr

    # The legacy model (no task identities) reads the same switch for UserService.
    legacy, _ = render("--set-string", f"{KEY}=false", task_identity=False)
    assert legacy.returncode != 0 and "without quotes" in legacy.stderr, legacy.stderr

    legacy_empty, legacy_docs = render("--set-string", f"{KEY}=", task_identity=False)
    assert legacy_empty.returncode == 0, legacy_empty.stderr
    assert legacy_docs[USER]["data"]["DELETION_AUDIT_AGENCYOUTBOX_ENABLED"] == "false"

    print("PASS: the deletion-log outbox switch renders only as a boolean, the same for both services")


if __name__ == "__main__":
    main()
