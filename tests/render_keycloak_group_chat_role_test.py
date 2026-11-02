#!/usr/bin/env python3
"""Realm role group-chat-consultant ships with the online-beratung realm (ORISO-UserService#1386).

UserService assigns `consultant` plus `group-chat-consultant` when a counsellor is created
with group chats enabled. A missing realm role makes the assignment fail and the create saga
rolls back with HTTP 424. realm.json is imported only when the realm is absent, so this pins
what a fresh realm starts with; existing realms get the role through
runbooks/keycloak-group-chat-consultant-role.md.
"""
from __future__ import annotations

import json
import os

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REALM = os.path.join(CHART_DIR, "charts", "keycloak", "keycloak-resources", "realm.json")
ROLE = "group-chat-consultant"


def main():
    with open(REALM, encoding="utf-8") as handle:
        realm = json.load(handle)
    roles = {role["name"]: role for role in realm["roles"]["realm"]}
    for name in ("consultant", ROLE):
        assert name in roles, f"realm role {name!r} missing from realm.json"
        role = roles[name]
        # Same shape as consultant: a plain realm role that grants nothing else.
        assert role.get("composite") is False, f"{name} must not be composite"
        assert role.get("clientRole") is False, f"{name} must be a realm role"
        assert role.get("containerId") == realm["realm"], f"{name} belongs to another realm"
    ids = [role.get("id") for role in realm["roles"]["realm"]]
    assert len(ids) == len(set(ids)), "realm role ids must be unique"

    # Assigned per counsellor by UserService, never to every account or a technical identity.
    default_role = roles["default-roles-online-beratung"]
    assert ROLE not in default_role.get("composites", {}).get("realm", []), "must not be a default role"
    for user in realm.get("users", []):
        assert ROLE not in user.get("realmRoles", []), f"{user.get('username')} must not ship {ROLE}"
    for group in realm.get("groups", []):
        assert ROLE not in group.get("realmRoles", []), f"group {group.get('name')} must not ship {ROLE}"
    print("keycloak group-chat-consultant realm role: ok")


if __name__ == "__main__":
    main()
