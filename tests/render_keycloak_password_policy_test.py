#!/usr/bin/env python3
"""Minimum password length for the online-beratung realm (ORISO-Admin#1272).

realm.json is imported only when the realm is absent, so this pins the value a fresh
or re-created realm starts with. The live realm is changed by the operator steps in
runbooks/keycloak-password-policy.md.
"""
from __future__ import annotations

import json
import os

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REALM = os.path.join(CHART_DIR, "charts", "keycloak", "keycloak-resources", "realm.json")


def main():
    with open(REALM, encoding="utf-8") as handle:
        realm = json.load(handle)
    assert realm["passwordPolicy"] == "length(16)", realm.get("passwordPolicy")
    # Imported users must not carry a password hash that bypasses the policy silently.
    for user in realm.get("users", []):
        assert not user.get("credentials"), f"user {user.get('username')} ships credentials in realm.json"
    print("keycloak password policy: ok")


if __name__ == "__main__":
    main()
