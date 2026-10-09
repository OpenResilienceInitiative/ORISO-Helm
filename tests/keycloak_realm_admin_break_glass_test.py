#!/usr/bin/env python3
"""Real Keycloak seam for Helm#422; starts and removes its own isolated container.

Fresh import: the ORISO-realm realmadmin is disabled. Existing realm: the
reconcile Job leaves it alone by default, and with the opt-in flag disables it,
ends its sessions and stays idempotent. Synthetic credentials only. Run separately:
    python3 tests/keycloak_realm_admin_break_glass_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keycloak_smtp_sync_client_test as kc  # noqa: E402

PASSWORD = "Synthetic-break-glass-credential-0001"


def realm_admin(base, realm):
    users = kc.request(base, "GET", "/admin/realms/" + realm + "/users?exact=true&username=realmadmin",
                       token=kc.master_admin(base))
    assert len(users) == 1, users
    return users[0]


def person_login(base, realm):
    return kc.status_of(lambda: kc.request(base, "POST", "/realms/" + realm + "/protocol/openid-connect/token",
                                           {"grant_type": "password", "client_id": "admin-cli",
                                            "username": "realmadmin", "password": PASSWORD}, form=True))


def sessions(base, realm, uid):
    return kc.request(base, "GET", "/admin/realms/" + realm + "/users/" + uid + "/sessions", token=kc.master_admin(base))


def reconcile(base, realm, subjects, **extra):
    env = {"KEYCLOAK_URL": base, "KEYCLOAK_REALM": realm, "POD_NAMESPACE": "fixture",
           "KEYCLOAK_ADMIN_USERNAME": kc.INSTALLER[0], "KEYCLOAK_ADMIN_PASSWORD": kc.INSTALLER[1],
           "BOOTSTRAP_ADMIN_USERNAME": kc.INSTALLER[0], "TECHNICAL_CLIENT_ID": "backend-technical",
           "ADMIN_CLIENT_ID": "backend-admin", "HUMAN_CLIENT_ID": "app", "SMTP_SYNC_CLIENT_ID": "smtp-sync",
           "RETIRE_LEGACY_USERS": "false", "LEGACY_TECHNICAL_USERNAME": "technical",
           "LEGACY_ADMIN_USERNAME": "svc-keycloak-admin", "REALM_ADMIN_USERNAME": "realmadmin",
           **kc.SECRETS, **subjects, **extra}
    return kc.run_script("keycloak-reconcile-service-clients.py", env)


def main():
    realm = kc.rendered_realm()
    with kc.isolated_keycloak() as base:
        fresh = dict(realm, realm="bg-fresh")
        fresh.pop("id", None)
        kc.request(base, "POST", "/admin/realms", fresh, kc.master_admin(base), timeout=90)
        assert realm_admin(base, "bg-fresh")["enabled"] is False

        # Existing realm as it looks today: realmadmin enabled with a password and a live session.
        existing = kc.strip_ids(realm)
        existing["realm"] = "bg-existing"
        for user in existing["users"]:
            if user["username"] == "realmadmin":
                user.update(enabled=True, requiredActions=[], email="realmadmin@synthetic.example", emailVerified=True,
                            credentials=[{"type": "password", "value": PASSWORD, "temporary": False}])
        kc.request(base, "POST", "/admin/realms", existing, kc.master_admin(base), timeout=90)
        assert person_login(base, "bg-existing") == 200
        uid = realm_admin(base, "bg-existing")["id"]
        assert sessions(base, "bg-existing", uid)

        out = reconcile(base, "bg-existing", kc.SUBJECTS, PREPARE_ONLY="true")
        subjects = dict(line.split("=", 1) for line in out.splitlines() if "_SERVICE_SUBJECT=" in line)
        out = reconcile(base, "bg-existing", subjects, PREPARE_ONLY="false", DISABLE_REALM_ADMIN="false")
        assert "REALM_ADMIN" not in out and realm_admin(base, "bg-existing")["enabled"] is True

        for _ in range(2):
            out = reconcile(base, "bg-existing", subjects, PREPARE_ONLY="false", DISABLE_REALM_ADMIN="true")
            assert "REALM_ADMIN_DISABLED" in out, out
            assert realm_admin(base, "bg-existing")["enabled"] is False
            assert sessions(base, "bg-existing", uid) == []
            assert person_login(base, "bg-existing") in (400, 401)
        # Master bootstrap admin is untouched.
        assert kc.master_admin(base)
    print("PASS: realmadmin disabled on fresh import; opt-in reconcile disables it on an existing realm, idempotent")


if __name__ == "__main__":
    main()
