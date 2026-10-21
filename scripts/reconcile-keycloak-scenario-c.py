#!/usr/bin/env python3
"""Standalone, default-off preparation of scenario C native permissions.

This is not chart/runtime wiring or approval of native lifecycle/SMTP gaps.
No client secrets, tokens or user profile data are printed or persisted.
"""

import argparse
import importlib.util
import hashlib
import json
import os
import re
from pathlib import Path
import sys
from urllib.parse import quote, urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PURPOSES = {
    "SETUP": {"CONFIG_WIZARD", "INVITE_RESERVATIONS", "CONSULTANT_IMPORT"},
    "ACCOUNT_CREATE": {"ACCOUNT_PROVISIONING"},
    "ACCOUNT_ADMIN": {"ACCOUNT_MAINTENANCE", "OTP", "SESSION_EXCHANGE"},
    "MAIL": {"SMTP_SYNC", "SYSTEM_EMAIL_DELIVERY", "NOTIFICATION_DISPATCH"},
    "RUNTIME": {
        "RUNTIME_POLICY",
        "MATRIX_AGENCY",
        "APPOINTMENT_SYNC",
        "APPOINTMENT_CLEANUP",
    },
}
EXPECTED_ROLES = {
    "SETUP": {"config-wizard", "invitation-reservations", "consultant-import"},
    "ACCOUNT_CREATE": {"account-provisioning", "account-read"},
    "ACCOUNT_ADMIN": {
        "account-maintenance",
        "account-read",
        "otp-config-admin",
        "session-exchange",
    },
    "MAIL": {
        "smtp-sync",
        "system-email-delivery",
        "notification-dispatch",
        "notifications-technical",
    },
    "RUNTIME": {
        "runtime-policy",
        "matrix-agency",
        "matrix-agency-provision",
        "appointment-sync",
        "appointment-participant-cleanup",
    },
}
EXPECTED_AUDIENCES = {
    "SETUP": {"tenantservice", "agencyservice", "consultingtypeservice", "userservice"},
    "ACCOUNT_CREATE": {"userservice", "oriso-account-lifecycle"},
    "ACCOUNT_ADMIN": {"userservice", "oriso-account-lifecycle", "oriso-otp"},
    "MAIL": {"tenantservice", "agencyservice", "consultingtypeservice", "userservice", "oriso-task-commands"},
    "RUNTIME": {"tenantservice", "agencyservice", "appointmentservice"},
}


class ReconcileError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pending_receipt(actors, mapping, realm, key):
    if not isinstance(realm.get("id"), str) or not realm["id"]:
        raise ReconcileError("Actual realm ID is required for pending ownership")
    return canonical({
        "version": 1, "actor": key, "clientId": actors[key]["clientId"],
        "realmId": realm["id"], "realm": realm["realm"],
        "registrySha256": hashlib.sha256(canonical(actors).encode()).hexdigest(),
        "mappingSha256": hashlib.sha256(canonical(mapping).encode()).hexdigest(),
    })


def native_secret_timestamp_valid(value):
    # Pinned26.6.3 KeycloakModelUtils.generateSecret adds this native metadata.
    # It conveys no preparation authority and must survive without rotation.
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value) is not None
            and int(value) <= 2147483647)


def pending_attributes_valid(attributes, receipt):
    return (isinstance(attributes, dict)
            and attributes.get("oriso-c.pending-receipt") == receipt
            and not set(attributes) - {"oriso-c.pending-receipt", "client.secret.creation.time"}
            and ("client.secret.creation.time" not in attributes
                 or native_secret_timestamp_valid(attributes["client.secret.creation.time"])))


def inspect_pending(admin, client_id, actor, receipt, baseline):
    """Read-only ownership/shape gate; never calls the lazy native subject API."""
    client = admin.request("GET", "/clients/" + quote(client_id, safe=""))
    if (client.get("clientId") != actor["clientId"]
            or not pending_attributes_valid(client.get("attributes"), receipt)
            or client.get("protocol") != "openid-connect"
            or client.get("enabled") is not False
            or client.get("publicClient") is not False
            or client.get("serviceAccountsEnabled") is not True
            or client.get("bearerOnly", False) is not False
            or client.get("authorizationServicesEnabled", False) is not False
            or client.get("consentRequired", False) is not False
            or client.get("clientAuthenticatorType", "client-secret") != "client-secret"
            or any(client.get(field) is not False for field in
                   ("standardFlowEnabled", "directAccessGrantsEnabled", "implicitFlowEnabled", "fullScopeAllowed"))):
        raise ReconcileError("Pending ownership receipt or inactive client shape does not match")
    assigned, mappers = inspect_token_scope(admin, client_id, actor, prepare_native_scopes=True)
    if assigned or mappers:
        raise ReconcileError("Pending client already has custom token grants")
    users = admin.paged("/users", {"username": "service-account-" + actor["clientId"], "exact": "true"})
    if (len(users) != 1 or not isinstance(users[0].get("id"), str) or not users[0]["id"]
            or users[0].get("username") != "service-account-" + actor["clientId"]):
        raise ReconcileError("Pending service account has no exact read-only match")
    uid = users[0]["id"]
    path = "/users/" + quote(uid, safe="")
    grants = admin.request("GET", path + "/role-mappings")
    roles = admin.request("GET", path + "/role-mappings/realm/composite")
    if (grants.get("clientMappings") or not {r["name"] for r in roles}.issubset(baseline)
            or admin.paged(path + "/groups")):
        raise ReconcileError("Pending service account has custom grants or membership")
    return {"client": client, "subject": uid}


def finalize_pending(admin, client_id, actor, receipt, baseline, before):
    actual = admin.request("GET", "/clients/" + quote(client_id, safe="") + "/service-account-user")
    if (actual.get("id") != before["subject"]
            or actual.get("username") != "service-account-" + actor["clientId"]
            or inspect_pending(admin, client_id, actor, receipt, baseline) != before):
        raise ReconcileError("Pending native subject or stable shape changed before receipt")
    attributes = dict(before["client"]["attributes"])
    attributes.pop("oriso-c.pending-receipt")
    attributes.update({"oriso-c.preparation": "v1", "oriso-c.prepared-subject": actual["id"]})
    admin.request("PUT", "/clients/" + quote(client_id, safe=""), {"attributes": attributes})
    return actual["id"]


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "human_role_migration", ROOT / "scripts/migrate-keycloak-human-role-groups.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def group(admin, path):
    value = admin.request("GET", "/group-by-path/" + quote(path.lstrip("/"), safe="/"))
    if not isinstance(value, dict) or not value.get("id") or value.get("path") != path:
        raise ReconcileError("Reviewed group path has no exact native match")
    return value


def token_mappers(actor):
    """Direct mappers avoid inherited scopes or an automatic audience resolver.

    The subject mapper is pinned to Keycloak26.6.3 SubMapper.PROVIDER_ID;
    https://github.com/keycloak/keycloak/blob/26.6.3/services/src/main/java/org/keycloak/protocol/oidc/mappers/SubMapper.java
    """
    values = [
        {
            "name": "oriso-c-subject",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-sub-mapper",
            "config": {"access.token.claim": "true", "introspection.token.claim": "true"},
        },
        {
            "name": "oriso-c-realm-roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-realm-role-mapper",
            "config": {
                "claim.name": "realm_access.roles",
                "multivalued": "true",
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "false",
                "introspection.token.claim": "true",
            },
        },
    ]
    return values + [
        {
            "name": "oriso-c-audience-" + audience,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": audience,
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "false",
                "introspection.token.claim": "true",
            },
        }
        for audience in sorted(actor["bearerAudiences"])
    ]


def native_scope_baseline(admin, kind):
    scopes = {scope["id"] for scope in admin.request("GET", "/default-" + kind + "-client-scopes")}
    if kind == "default":
        # Pinned ClientManager.enableServiceAccount also adds this scope even
        # when it is absent from the realm's default-default scope list.
        service = [scope for scope in admin.paged("/client-scopes") if scope.get("name") == "service_account"]
        if len(service) > 1 or any(scope.get("protocol") != "openid-connect" for scope in service):
            raise ReconcileError("Native service-account scope is ambiguous")
        scopes.update(scope["id"] for scope in service)
    return scopes


def inspect_token_scope(admin, client_id, actor, exact=False, prepare_native_scopes=False):
    path = "/clients/" + quote(client_id, safe="")
    for kind in ("default", "optional"):
        scopes = admin.request("GET", path + "/" + kind + "-client-scopes")
        if scopes and (not prepare_native_scopes or not {scope["id"] for scope in scopes}.issubset(native_scope_baseline(admin, kind))):
            raise ReconcileError("Actor must not inherit default or optional client scopes")
    grants = admin.request("GET", path + "/scope-mappings")
    assigned = {role["name"] for role in grants.get("realmMappings", [])}
    if grants.get("clientMappings") or not assigned.issubset(set(actor["roles"])):
        raise ReconcileError("Actor token scope has undeclared grants")
    desired = {value["name"]: value for value in token_mappers(actor)}
    mappers = admin.request("GET", path + "/protocol-mappers/models")
    names = [value.get("name") for value in mappers]
    if len(names) != len(set(names)) or any(
        value.get("name") not in desired
        or any(value.get(field) != desired[value["name"]][field] for field in ("protocol", "protocolMapper", "config"))
        for value in mappers
    ):
        raise ReconcileError("Actor token mapper is not an exact reviewed mapper")
    if exact and (assigned != set(actor["roles"]) or set(names) != set(desired)):
        raise ReconcileError("Actor token scope did not converge to the exact contract")
    return assigned, set(names)


def reconcile_token_scope(admin, client_id, actor):
    path = "/clients/" + quote(client_id, safe="")
    client = admin.request("GET", path)
    attributes = client.get("attributes", {})
    preparing = attributes.get("oriso-c.token-contract") is None
    assigned, existing = inspect_token_scope(admin, client_id, actor, prepare_native_scopes=preparing)
    changes = 0
    if preparing:
        # Keycloak adds realm defaults even when creation supplied empty scope
        # arrays. Only known native defaults on our disabled managed client may
        # be removed during this one-time preparation; later drift is refused.
        for kind in ("default", "optional"):
            base = path + "/" + kind + "-client-scopes"
            for scope in admin.request("GET", base):
                admin.request("DELETE", base + "/" + quote(scope["id"], safe=""))
                changes += 1
    missing = set(actor["roles"]) - assigned
    if missing:
        admin.request("POST", path + "/scope-mappings/realm", [
            admin.request("GET", "/roles/" + quote(name, safe="")) for name in sorted(missing)
        ])
        changes += 1
    for mapper in token_mappers(actor):
        if mapper["name"] not in existing:
            admin.request("POST", path + "/protocol-mappers/models", mapper)
            changes += 1
    inspect_token_scope(admin, client_id, actor, exact=True)
    if preparing:
        admin.request("PUT", path, {"attributes": {**attributes, "oriso-c.token-contract": "exact-v1"}})
        changes += 1
    if admin.request("GET", path).get("attributes", {}).get("oriso-c.token-contract") != "exact-v1":
        raise ReconcileError("Actor token preparation receipt did not converge")
    return changes


def inspect(admin, actors, mapping, verify_service_accounts=False, exact_grants=False):
    realm_state = admin.request("GET", "")
    baseline = {"offline_access", "uma_authorization"}
    if realm_state.get("defaultRole", {}).get("name"):
        baseline.add(realm_state["defaultRole"]["name"])
    for name in sorted({role for actor in actors.values() for role in actor["roles"]}):
        role = admin.request("GET", "/roles/" + quote(name, safe=""), missing=True)
        if role and (role.get("name") != name or role.get("composite")):
            raise ReconcileError("Actor role must not inherit other grants")
    groups = {}
    for entry in [
        *mapping["groups"],
        *(
            {"path": p, "roles": []}
            for p in (
                "/human-roles",
                "/protected-admins",
                "/pending-provisioning",
                "/protected-admins/technical-identities",
            )
        ),
    ]:
        value = group(admin, entry["path"])
        gid = quote(value["id"], safe="")
        grants = admin.request("GET", "/groups/" + gid + "/role-mappings")
        effective = admin.request(
            "GET", "/groups/" + gid + "/role-mappings/realm/composite"
        )
        if grants.get("clientMappings") or {r["name"] for r in effective} != set(
            entry["roles"]
        ):
            raise ReconcileError(
                "Reviewed group role grants do not match native readback"
            )
        groups[entry["path"]] = value["id"]
    # Native group permissions cascade. A root grant must not silently reach an
    # unknown child carrying a different privilege combination.
    for path, gid in groups.items():
        expected = {
            child
            for child_path, child in groups.items()
            if child_path.rsplit("/", 1)[0] == path
        }
        children = admin.paged(
            "/groups/" + quote(gid, safe="") + "/children",
            {"briefRepresentation": "false"},
        )
        if {child["id"] for child in children} != expected:
            raise ReconcileError(
                "Unreviewed group descendants would inherit native permissions"
            )
    clients = {}
    subjects = {}
    pending = {}
    receiver_policy_prepared = False
    for key, actor in actors.items():
        cid = actor["clientId"]
        found = admin.paged("/clients", {"clientId": cid})
        if len(found) > 1:
            raise ReconcileError("Actor client is ambiguous")
        if not found:
            continue
        client = admin.request("GET", "/clients/" + quote(found[0]["id"], safe=""))
        if (
            client.get("protocol") != "openid-connect"
            or client.get("enabled") is not False
            or client.get("publicClient") is not False
            or client.get("serviceAccountsEnabled") is not True
            or client.get("standardFlowEnabled") is not False
            or client.get("directAccessGrantsEnabled") is not False
            or client.get("fullScopeAllowed") is not False
        ):
            raise ReconcileError("Existing actor client is active or has unsafe grants")
        attrs = client.get("attributes", {})
        if "oriso-c.pending-receipt" in attrs:
            pending[key] = inspect_pending(admin, client["id"], actor,
                pending_receipt(actors, mapping, realm_state, key), baseline)
            clients[key] = client["id"]
            continue
        policy = attrs.get("oriso-c.native-policy")
        if policy not in (None, "receiver-only-v1"):
            raise ReconcileError("Unknown managed native policy preparation")
        receiver_policy_prepared |= policy == "receiver-only-v1"
        uid = attrs.get("oriso-c.prepared-subject")
        if attrs.get("oriso-c.preparation") != "v1" or not uid:
            raise ReconcileError(
                "Existing actor has no managed subject receipt; adoption is not authorized"
            )
        token_contract = attrs.get("oriso-c.token-contract")
        if token_contract not in (None, "exact-v1"):
            raise ReconcileError("Unknown managed actor token preparation")
        if exact_grants and token_contract != "exact-v1":
            raise ReconcileError("Actor token preparation receipt is missing")
        inspect_token_scope(admin, client["id"], actor, exact=exact_grants, prepare_native_scopes=token_contract is None and not exact_grants)
        user = admin.request("GET", "/users/" + quote(uid, safe=""))
        if user.get("id") != uid or user.get("username") != "service-account-" + cid:
            raise ReconcileError(
                "Managed subject receipt does not match existing user readback"
            )
        if verify_service_accounts:
            actual = admin.request(
                "GET",
                "/clients/" + quote(client["id"], safe="") + "/service-account-user",
            )
            if actual.get("id") != uid:
                raise ReconcileError(
                    "Managed subject receipt is not the native service account"
                )
        path = "/users/" + quote(uid, safe="")
        grants = admin.request("GET", path + "/role-mappings")
        roles = admin.request("GET", path + "/role-mappings/realm/composite")
        if grants.get("clientMappings") or not {r["name"] for r in roles}.issubset(
            set(actor["roles"]) | baseline
        ):
            raise ReconcileError(
                "Actor has broad or undeclared native administration grants"
            )
        if exact_grants and {r["name"] for r in roles} != set(actor["roles"]):
            raise ReconcileError(
                "Native actor grants did not converge to the exact reviewed union"
            )
        memberships = admin.paged(path + "/groups")
        if any(
            g["id"] != groups["/protected-admins/technical-identities"]
            for g in memberships
        ):
            raise ReconcileError("Actor has an undeclared group membership")
        clients[key], subjects[key] = client["id"], uid
    permission_client = admin.paged("/clients", {"clientId": "admin-permissions"})
    return {
        "groups": groups,
        "clients": clients,
        "subjects": subjects,
        "pending": pending,
        "realm": realm_state,
        "baseline_roles": baseline,
        "permission_client": permission_client[0]["id"] if permission_client else None,
        "receiver_policy_prepared": receiver_policy_prepared,
    }


POLICY_NAMES = {
    "oriso-c-is-provisioner",
    "oriso-c-is-maintainer",
    "oriso-c-deny-everyone",
}
PERMISSION_NAMES = {
    "oriso-c-pending",
    "oriso-c-human-join",
    "oriso-c-human-members",
    "oriso-c-protected",
}


def authorization_base(client):
    return "/clients/" + quote(client, safe="") + "/authz/resource-server"


def permission_inventory(admin, client):
    if not client:
        return {}
    base = authorization_base(client)
    policies = admin.paged(base + "/policy", {"fields": "*"})
    permissions = admin.paged(base + "/permission", {"fields": "*"})
    found = {}
    for value in policies:
        name, kind = value.get("name"), value.get("type")
        if name in POLICY_NAMES:
            if kind != "user":
                raise ReconcileError(
                    "Managed actor policy has an incompatible native type"
                )
            found[name] = value
    for value in permissions:
        if (
            value.get("name") not in PERMISSION_NAMES
            or value.get("type") != "scope"
            or value.get("resourceType") != "Groups"
        ):
            raise ReconcileError(
                "Unreviewed or broad native Users/Roles/Groups permission exists"
            )
        if value["name"] in found:
            raise ReconcileError("Native policy names are ambiguous")
        found[value["name"]] = value
    return found


def desired_policies(snapshot, mapping, receivers_only=False):
    groups, subjects = snapshot["groups"], snapshot["subjects"]
    ordinary = [
        groups["/human-roles"],
        *(
            groups[g["path"]]
            for g in mapping["groups"]
            if g["path"].startswith("/human-roles/")
        ),
    ]
    common = {"logic": "POSITIVE", "decisionStrategy": "UNANIMOUS"}
    users = [
        {
            "name": "oriso-c-is-provisioner",
            "users": [subjects["ACCOUNT_CREATE"]],
        },
        {"name": "oriso-c-is-maintainer", "users": [subjects["ACCOUNT_ADMIN"]]},
        {"name": "oriso-c-deny-everyone", "users": []},
    ]
    permissions = [
        {
            "name": "oriso-c-pending",
            "resources": [groups["/pending-provisioning"]],
            "scopes": [
                "view",
                "view-members",
                "manage-members",
                "manage-membership",
                "manage-membership-of-members",
            ],
            "policies": ["oriso-c-is-provisioner"],
        },
        {
            "name": "oriso-c-human-join",
            "resources": ordinary,
            "scopes": ["view", "manage-membership"],
            "policies": ["oriso-c-is-provisioner"],
        },
        {
            "name": "oriso-c-human-members",
            "resources": ordinary,
            "scopes": [
                "view-members",
                "manage-members",
                "manage-membership-of-members",
            ],
            "policies": ["oriso-c-is-maintainer"],
        },
        {
            "name": "oriso-c-protected",
            "resources": [groups["/protected-admins"]],
            "scopes": [
                "view",
                "view-members",
                "manage-members",
                "impersonate-members",
                "manage-membership",
                "manage-membership-of-members",
            ],
            "policies": ["oriso-c-deny-everyone"],
        },
    ]
    if receivers_only:
        # Removing the native paths is necessary even when the bounded receiver
        # refuses them: direct stock Admin calls never pass through its guard.
        for policy in users:
            policy["users"] = []
        for permission in permissions:
            permission["policies"] = ["oriso-c-deny-everyone"]
    return [("policy/user", {**common, **p}) for p in users] + [
        ("permission/scope", {**common, "resourceType": "Groups", **p})
        for p in permissions
    ]


def policy_matches(admin, base, route, actual, desired, ids):
    if not actual or any(
        actual.get(k) != desired[k] for k in ("name", "logic", "decisionStrategy")
    ):
        return False
    path = base + "/" + route + "/" + quote(actual["id"], safe="")
    value = admin.request("GET", path + "?fields=*")
    if route == "policy/user":
        return set(value.get("users", [])) == set(desired["users"])
    resources = admin.request("GET", path + "/resources")
    scopes = admin.request("GET", path + "/scopes")
    policies = admin.request("GET", path + "/associatedPolicies")
    return (
        value.get("resourceType") == "Groups"
        # Native authz `_id` identifies the authorization resource. Its
        # `name` resolves the actual GroupModel ID (26.6.3 schema L169/403).
        and {r["name"] for r in resources} == set(desired["resources"])
        and {s["name"] for s in scopes} == set(desired["scopes"])
        and {p["id"] for p in policies}
        == {ids[name]["id"] for name in desired["policies"]}
    )


def reconcile(admin, actors, mapping, snapshot, receivers_only=False):
    if snapshot.get("receiver_policy_prepared") and not receivers_only:
        raise ReconcileError(
            "Receiver-only preparation cannot restore native account permissions"
        )
    changes = 0
    for key, before in snapshot.get("pending", {}).items():
        snapshot["subjects"][key] = finalize_pending(admin, snapshot["clients"][key], actors[key],
            pending_receipt(actors, mapping, snapshot["realm"], key), snapshot["baseline_roles"], before)
        changes += 1
    if not snapshot["realm"].get("adminPermissionsEnabled"):
        # Do not re-import realm exports: native re-import may conflict with FGAP resources.
        admin.request("PUT", "", {"adminPermissionsEnabled": True})
        changes += 1
    for key, actor in actors.items():
        cid = snapshot["clients"].get(key)
        if key not in snapshot["clients"]:
            admin.request(
                "POST",
                "/clients",
                {
                    "clientId": actor["clientId"],
                    "protocol": "openid-connect",
                    "enabled": False,
                    "publicClient": False,
                    "serviceAccountsEnabled": True,
                    "standardFlowEnabled": False,
                    "directAccessGrantsEnabled": False,
                    "fullScopeAllowed": False,
                    "implicitFlowEnabled": False,
                    "bearerOnly": False,
                    "attributes": {"oriso-c.pending-receipt": pending_receipt(actors, mapping, snapshot["realm"], key)},
                    "defaultClientScopes": [],
                    "optionalClientScopes": [],
                },
            )
            cid = admin.paged("/clients", {"clientId": actor["clientId"]})[0]["id"]
            receipt = pending_receipt(actors, mapping, snapshot["realm"], key)
            before = inspect_pending(admin, cid, actor, receipt, snapshot["baseline_roles"])
            snapshot["subjects"][key] = finalize_pending(admin, cid, actor, receipt,
                snapshot["baseline_roles"], before)
            changes += 2
        path = "/users/" + quote(snapshot["subjects"][key], safe="")
        direct = admin.request("GET", path + "/role-mappings/realm")
        baseline = snapshot["baseline_roles"]
        defaults = [r for r in direct if r["name"] in baseline]
        if defaults:
            admin.request("DELETE", path + "/role-mappings/realm", defaults)
            direct = [r for r in direct if r["name"] not in baseline]
            changes += 1
        missing = set(actor["roles"]) - {r["name"] for r in direct}
        for name in sorted(missing):
            role = admin.request("GET", "/roles/" + quote(name, safe=""), missing=True)
            if role is None:
                admin.request("POST", "/roles", {"name": name, "composite": False})
                role = admin.request("GET", "/roles/" + quote(name, safe=""))
                changes += 1
            if role.get("composite"):
                raise ReconcileError("Actor role must not inherit other grants")
            admin.request("POST", path + "/role-mappings/realm", [role])
            changes += 1
        group_id = snapshot["groups"]["/protected-admins/technical-identities"]
        if not any(g["id"] == group_id for g in admin.paged(path + "/groups")):
            admin.request("PUT", path + "/groups/" + quote(group_id, safe=""))
            changes += 1
        changes += reconcile_token_scope(admin, cid, actor)
    snapshot = inspect(
        admin, actors, mapping, verify_service_accounts=True, exact_grants=True
    )
    client = snapshot["permission_client"]
    if not client or not snapshot["realm"].get("adminPermissionsEnabled"):
        raise ReconcileError("Native authorization client was not created")
    base = authorization_base(client)
    found = permission_inventory(admin, client)
    for route, desired in desired_policies(snapshot, mapping, receivers_only):
        actual = found.get(desired["name"])
        if not policy_matches(admin, base, route, actual, desired, found):
            body = dict(desired)
            if actual:
                body["id"] = actual["id"]
            admin.request(
                "PUT" if actual else "POST",
                base
                + "/"
                + route
                + ("/" + quote(actual["id"], safe="") if actual else ""),
                body,
            )
            changes += 1
            found = permission_inventory(admin, client)
        if not policy_matches(
            admin, base, route, found.get(desired["name"]), desired, found
        ):
            raise ReconcileError(
                "Native policy readback does not match intended boundary"
            )
    if len(found) != 7:
        raise ReconcileError("Native policy set did not converge")
    if receivers_only:
        # Write receipts only after all native permissions were read back as
        # denied. A partially written receipt still prevents an unsafe downgrade.
        for key in actors:
            path = "/clients/" + quote(snapshot["clients"][key], safe="")
            current = admin.request("GET", path)
            attributes = current.get("attributes", {})
            if attributes.get("oriso-c.native-policy") != "receiver-only-v1":
                admin.request(
                    "PUT",
                    path,
                    {"attributes": {**attributes, "oriso-c.native-policy": "receiver-only-v1"}},
                )
                changes += 1
            if admin.request("GET", path).get("attributes", {}).get("oriso-c.native-policy") != "receiver-only-v1":
                raise ReconcileError("Receiver-only preparation receipt did not converge")
    return changes


def registry(path):
    data = json.loads(path.read_text())
    if data.get("version") != 1 or data.get("runtimeCutover") is not False:
        raise ReconcileError("Use the default-off version 1 scenario C registry")
    actors = data.get("actors", [])
    if len(actors) != 5 or {a.get("key") for a in actors} != set(EXPECTED_PURPOSES):
        raise ReconcileError("Exactly five scenario C actors are required")
    for actor in actors:
        key = actor["key"]
        audiences = actor.get("bearerAudiences")
        if (
            actor.get("clientId") != "backend-" + key.lower().replace("_", "-")
            or set(actor.get("purposes", [])) != EXPECTED_PURPOSES[key]
            or len(actor.get("purposes", [])) != len(EXPECTED_PURPOSES[key])
            or set(actor.get("roles", [])) != EXPECTED_ROLES[key]
            or len(actor.get("roles", [])) != len(EXPECTED_ROLES[key])
            or not isinstance(audiences, list)
            or any(not isinstance(audience, str) for audience in audiences)
            or set(audiences) != EXPECTED_AUDIENCES[key]
            or len(audiences) != len(EXPECTED_AUDIENCES[key])
        ):
            raise ReconcileError("Actor contract does not match selected scenario C")
    return {actor["key"]: actor for actor in actors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry", type=Path, default=ROOT / "files/scenario-c-task-identities.json"
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--realm")
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-isolated-realm", action="store_true")
    parser.add_argument(
        "--receivers-only",
        action="store_true",
        help="Prepare disabled actors without native account paths; does not enable receivers or runtime",
    )
    args = parser.parse_args()
    actors = registry(args.registry)
    if args.validate_only:
        print(
            json.dumps(
                {
                    "clients": {
                        key: actor["clientId"] for key, actor in actors.items()
                    },
                    "purposes": {
                        purpose: key
                        for key, actor in actors.items()
                        for purpose in actor["purposes"]
                    },
                    "runtime_cutover": False,
                },
                sort_keys=True,
            )
        )
        return
    parsed = urlparse(args.url or "")
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme == "http"
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
        )
        or not args.realm
        or not args.mapping
    ):
        raise ReconcileError(
            "Use an explicit secure Admin URL, realm and reviewed mapping"
        )
    token = os.environ.get("ORISO_KEYCLOAK_INSTALLER_TOKEN")
    if not token:
        raise ReconcileError("A short-lived installer token is required")
    migration = migration_module()
    mapping = json.loads(args.mapping.read_text())
    migration.validate_mapping(mapping)
    if args.apply and not args.confirm_isolated_realm:
        raise ReconcileError(
            "Apply requires explicit confirmation of an isolated realm copy"
        )
    admin = migration.Admin(args.url, args.realm, token)
    snapshot = inspect(admin, actors, mapping, verify_service_accounts=args.apply)
    existing = permission_inventory(admin, snapshot["permission_client"])
    changes = (
        reconcile(admin, actors, mapping, snapshot, args.receivers_only)
        if args.apply
        else 0
    )
    print(
        json.dumps(
            {
                "actor_count": 5,
                "applied": args.apply,
                "changes": changes,
                "native_policy_count": 7 if args.apply else len(existing),
                "runtime_cutover": False,
                "policy_model": "RECEIVER_ONLY" if args.receivers_only or snapshot.get("receiver_policy_prepared") else "NATIVE_PREPARATION",
                "native_model_verified": args.apply,
                "native_service_accounts_verified": args.apply,
                "missing_actor_count_before": 5 - len(snapshot["clients"]),
                "clients_disabled": True,
                "native_lifecycle_approval": False,
                "smtp_boundary_approval": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        reason = (
            str(error)
            if isinstance(error, ReconcileError)
            or type(error).__name__ == "MigrationError"
            else "inspect the reviewed inputs and Admin API state"
        )
        print("Scenario C preparation refused: " + reason, file=sys.stderr)
        sys.exit(1)
